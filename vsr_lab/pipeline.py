"""Decode → one VSR pass → x265 encode. No silent scale fallback."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from vsr_lab.engine import VsrEngine, VsrEvalDesc, _cstr
from vsr_lab.ffmpeg_tools import ActiveRect, detect_active_picture, ffmpeg_exe, probe_media, write_png
from vsr_lab.power import PowerLogger
from vsr_lab.sizes import (
    MAX_H,
    MAX_W,
    MIN_H,
    MIN_W,
    Pair,
    clamp_output,
    display_dims,
    fit_content,
    nearest_pair,
    would_downscale,
)


@dataclass
class Job:
    inp: Path
    out: Path
    width: int
    height: int
    quality: int = 4
    truehdr: bool = False
    stretch: bool = False
    fallback: bool = False
    crf: int = 12
    keep_picture_aspect: bool = True
    out_dir: Path | None = None


class JobError(RuntimeError):
    pass


def output_stem(src: Path, w: int, h: int, quality: int, fallback: bool) -> str:
    tag = "vsrthen_scale" if fallback else "vsr"
    return f"{src.stem}_{tag}-{w}x{h}-q{quality}"


def default_outfile(src: Path, folder: Path, w: int, h: int, quality: int, fallback: bool) -> Path:
    return folder / f"{output_stem(src, w, h, quality, fallback)}.mp4"


def _crop_at(rgba: bytes, w: int, h: int, x: int, y: int, cw: int, ch: int) -> tuple[bytes, int, int, int, int]:
    cw = min(max(1, cw), w)
    ch = min(max(1, ch), h)
    x = max(0, min(int(x), w - cw))
    y = max(0, min(int(y), h - ch))
    rows = []
    for yy in range(ch):
        o = ((y + yy) * w + x) * 4
        rows.append(rgba[o:o + cw * 4])
    return b"".join(rows), x, y, cw, ch


def _center_crop(rgba: bytes, w: int, h: int, cw: int, ch: int) -> tuple[bytes, int, int, int, int]:
    cw = min(cw, w)
    ch = min(ch, h)
    return _crop_at(rgba, w, h, (w - cw) // 2, (h - ch) // 2, cw, ch)


def _crop_rgba(rgba: bytes, frame_w: int, frame_h: int, box: ActiveRect) -> bytes:
    if not box.has_bars:
        return rgba
    return _crop_at(rgba, frame_w, frame_h, box.x, box.y, box.w, box.h)[0]


def _pad_to_canvas(
    src: bytes,
    sw: int,
    sh: int,
    out_w: int,
    out_h: int,
    cx: int,
    cy: int,
    cw: int,
    ch: int,
    engine: VsrEngine,
) -> bytearray:
    """Place src (optionally resized) onto a black out_w x out_h canvas."""
    canvas = bytearray(out_w * out_h * 4)
    if sw != cw or sh != ch:
        src = bytes(engine.resize(src, sw, sh, cw, ch, filter=1))
        sw, sh = cw, ch
    rowb = cw * 4
    for yy in range(ch):
        dst = ((cy + yy) * out_w + cx) * 4
        so = yy * rowb
        canvas[dst:dst + rowb] = src[so:so + rowb]
    return canvas


def run_job(engine: VsrEngine, job: Job, log=lambda *_: None, progress=None) -> Path:
    src = Path(job.inp)
    media = probe_media(src)
    log(f"input {media.width}x{media.height} {media.fps_text} fps  {media.codec} {media.pix_fmt}  audio={'yes' if media.has_audio else 'no'}")
    if media.sar_num != media.sar_den:
        log(f"sample aspect {media.sar_num}:{media.sar_den} (coded pixels; VSR uses coded size)")

    if media.width < MIN_W or media.height < MIN_H:
        raise JobError(f"Reject: source {media.width}x{media.height} is below {MIN_W}x{MIN_H}")

    out_w, out_h = clamp_output(job.width, job.height)
    if out_w != job.width or out_h != job.height:
        log(f"clamped output {job.width}x{job.height} → {out_w}x{out_h} (max {MAX_W}x{MAX_H}, even)")
    src_too_big = media.width >= 2560 and media.height >= 1440
    skip_vsr = src_too_big
    if src_too_big and (out_w > media.width or out_h > media.height):
        raise JobError(
            f"Reject: VSR is not run on sources >= 2560x1440 ({media.width}x{media.height})"
        )
    if would_downscale(media.width, media.height, out_w, out_h, job.stretch):
        raise JobError(
            f"Do not downscale: source {media.width}x{media.height} → requested {out_w}x{out_h}"
        )
    if job.quality < 1 or job.quality > 4:
        raise JobError("quality must be 1-4 (NVIDIA App scale; 0 is bicubic, not VSR)")
    if skip_vsr:
        log("source is >= 1440p: skipping VSR Evaluate; still applying display-aspect letterbox")

    active = ActiveRect(0, 0, media.width, media.height, media.width, media.height)
    stretch = bool(job.stretch) and not bool(job.keep_picture_aspect)
    if job.keep_picture_aspect:
        detected = detect_active_picture(src, media.width, media.height, media.duration)
        if detected.has_bars:
            active = detected
            log(
                f"pixel letterbox/pillarbox: active {active.w}x{active.h} at ({active.x},{active.y})  "
                f"{active.aspect:.3f}:1  (coded frame {media.width}x{media.height})"
            )
            stretch = False
        else:
            log("no pixel letterbox/pillarbox (mattes may come from sample aspect ratio)")
    if stretch:
        log("stretch: fill the output canvas (picture aspect not preserved)")

    pic_w, pic_h = active.w, active.h
    sar_n, sar_d = media.sar_num, media.sar_den
    disp_w, disp_h = display_dims(pic_w, pic_h, sar_n, sar_d)
    if (disp_w, disp_h) != (pic_w, pic_h):
        log(
            f"sample aspect {sar_n}:{sar_d}  coded {pic_w}x{pic_h}  display {disp_w}x{disp_h}  "
            f"DAR {disp_w}:{disp_h} ({disp_w/disp_h:.3f}:1) — will letterbox, not stretch to 16:9"
        )

    # Uniform VSR on coded pixels, then place using DISPLAY aspect on a square-pixel canvas.
    _ux, _uy, uw, uh, uscale = fit_content(pic_w, pic_h, out_w, out_h, stretch)
    if stretch:
        cx, cy, cw, ch, fit_scale = 0, 0, out_w, out_h, uscale
    else:
        cx, cy, cw, ch, _ds = fit_content(disp_w * uscale, disp_h * uscale, out_w, out_h, False)
        fit_scale = uscale
    used_fallback = False
    nearest: Pair | None = None

    log(
        f"output canvas {out_w}x{out_h}  coded {pic_w}x{pic_h}  "
        f"VSR {uw}x{uh}  place {cw}x{ch} at ({cx},{cy})  scale {fit_scale:.3f}x  q{job.quality}"
    )
    log(f"SDK status: VSR={'yes' if engine.info.vsr_available else 'no'}  TrueHDR={'yes' if engine.info.truehdr_available else 'no'}")
    if job.truehdr and not engine.info.truehdr_available:
        raise JobError("TrueHDR requested but nvngx_truehdr.dll / feature is not available")

    vsr_nw, vsr_nh = uw, uh
    if skip_vsr:
        vsr_nw, vsr_nh = pic_w, pic_h
    else:
        probe = engine.try_size(pic_w, pic_h, uw, uh, quality=job.quality)
        if not probe.ok:
            nearest = nearest_pair(pic_w, pic_h, uw, uh, engine.pairs)
            near_txt = (
                f"{nearest.in_w}x{nearest.in_h} → {nearest.out_w}x{nearest.out_h} ({nearest.scale:.2f}x)"
                if nearest else "none from startup probe"
            )
            msg = (
                f"Evaluate() failed for {pic_w}x{pic_h} → {uw}x{uh} "
                f"(scale {fit_scale:.3f}x)\n"
                f"NGX error: {engine.ngx_name(probe.ngx)} (0x{probe.ngx & 0xFFFFFFFF:08X}) {probe.message}\n"
                f"Nearest supported size: {near_txt}"
            )
            if not job.fallback:
                raise JobError(
                    msg + "\nDo NOT silently bicubic/Lanczos the rest and call it VSR. "
                    "Enable fallback to write a vsrthen_scale file."
                )
            if not nearest:
                raise JobError(msg + "\nFallback requested but no accepted probe pair is available.")
            used_fallback = True
            log(msg)
            log(
                f"Fallback: VSR {pic_w}x{pic_h} → {nearest.out_w}x{nearest.out_h}, "
                f"then scale to content {cw}x{ch} on {out_w}x{out_h} (filename will contain vsrthen_scale)"
            )
            vsr_nw, vsr_nh = nearest.out_w, nearest.out_h

    out_path = Path(job.out)
    if used_fallback and "vsrthen_scale" not in out_path.name:
        out_path = out_path.with_name(
            out_path.name.replace("_vsr-", "_vsrthen_scale-") if "_vsr-" in out_path.name
            else output_stem(src, out_w, out_h, job.quality, True) + out_path.suffix
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = out_path.parent / "frames"
    logs_dir = out_path.parent / "logs"
    frames_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    stem = src.stem

    ff = str(ffmpeg_exe())
    dec_cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(src),
        "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-an", "pipe:1",
    ]
    enc_cmd = [
        ff, "-hide_banner", "-loglevel", "error",
        "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{out_w}x{out_h}",
        "-r", media.fps_text,
        "-i", "pipe:0",
        "-i", str(src),
        "-map", "0:v:0",
    ]
    if media.has_audio:
        enc_cmd += ["-map", "1:a:0", "-c:a", "copy"]
    else:
        enc_cmd += ["-an"]
    enc_cmd += [
        "-c:v", "libx265",
        "-preset", "medium",
        "-crf", str(int(job.crf)),
        "-pix_fmt", "yuv420p",
        "-profile:v", "main",
        "-tag:v", "hvc1",
        "-sar", "1:1",
        "-x265-params", "log-level=error",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]
    log("encode: libx265 yuv420p main hvc1 crf=%s preset=medium (not NVENC) sar=1:1" % job.crf)

    in_bytes = media.width * media.height * 4
    out_bytes = out_w * out_h * 4
    desc = VsrEvalDesc(
        in_w=pic_w, in_h=pic_h,
        out_w=vsr_nw, out_h=vsr_nh,
        content_x=0, content_y=0,
        content_w=vsr_nw, content_h=vsr_nh,
        quality=job.quality, truehdr=1 if job.truehdr else 0,
    )

    power = PowerLogger(logs_dir / f"{stem}_power.csv", gpu_name=_cstr(engine.info.gpu_name))
    power_ok = power.start()
    if power_ok:
        log(f"power log (offline VSR encode power, not live playback): {power.csv_path}")
    else:
        log("nvidia-smi not available; skipping power log")

    dec = subprocess.Popen(
        dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
    )
    enc = subprocess.Popen(
        enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    assert dec.stdout and enc.stdin
    enc_err_buf: list[bytes] = []

    def _drain_enc_err() -> None:
        if enc.stderr:
            enc_err_buf.append(enc.stderr.read() or b"")

    threading.Thread(target=_drain_enc_err, name="vsr-enc-err", daemon=True).start()

    saved_src = False
    n = 0
    try:
        while True:
            buf = dec.stdout.read(in_bytes)
            if not buf:
                break
            if len(buf) != in_bytes:
                raise JobError(f"short decode read ({len(buf)} / {in_bytes})")

            pic = _crop_rgba(buf, media.width, media.height, active)

            if skip_vsr:
                placed = pic
                pw, ph = pic_w, pic_h
            else:
                vsr_out = bytearray(vsr_nw * vsr_nh * 4)
                desc.in_w = pic_w
                desc.in_h = pic_h
                desc.out_w = vsr_nw
                desc.out_h = vsr_nh
                desc.content_x = 0
                desc.content_y = 0
                desc.content_w = vsr_nw
                desc.content_h = vsr_nh
                st = engine.evaluate(pic, vsr_out, desc)
                if not st.ok:
                    nearest = nearest_pair(pic_w, pic_h, vsr_nw, vsr_nh, engine.pairs)
                    near_txt = (
                        f"{nearest.out_w}x{nearest.out_h}" if nearest else "unknown"
                    )
                    raise JobError(
                        f"Evaluate() failed on frame {n} for {pic_w}x{pic_h} → {vsr_nw}x{vsr_nh}: "
                        f"{_cstr(st.message)}. Nearest supported: {near_txt}. "
                        "Not silently scaling."
                    )
                placed = bytes(vsr_out)
                pw, ph = vsr_nw, vsr_nh

            if stretch:
                canvas = engine.resize(placed, pw, ph, out_w, out_h, filter=1)
            elif pw == out_w and ph == out_h and cw == out_w and ch == out_h:
                canvas = bytearray(placed)
            else:
                canvas = _pad_to_canvas(placed, pw, ph, out_w, out_h, cx, cy, cw, ch, engine)

            if not saved_src:
                raw_out = bytes(canvas)
                write_png(frames_dir / f"{stem}_src.png", buf, media.width, media.height)
                write_png(frames_dir / f"{stem}_vsr.png", raw_out, out_w, out_h)
                crop, sx, sy, scw, sch = _center_crop(pic, pic_w, pic_h, 400, 400)
                if used_fallback and stretch:
                    scale_x = out_w / pic_w
                    scale_y = out_h / pic_h
                    ox, oy = 0, 0
                else:
                    scale_x = cw / pic_w
                    scale_y = ch / pic_h
                    ox, oy = cx, cy
                up_w = max(1, int(round(scw * scale_x)))
                up_h = max(1, int(round(sch * scale_y)))
                src_up = engine.resize(crop, scw, sch, up_w, up_h, filter=0)
                write_png(frames_dir / f"{stem}_src_crop.png", bytes(src_up), up_w, up_h)
                mx = ox + int(round(sx * scale_x))
                my = oy + int(round(sy * scale_y))
                vsr_crop, *_ = _crop_at(raw_out, out_w, out_h, mx, my, up_w, up_h)
                write_png(frames_dir / f"{stem}_vsr_crop.png", vsr_crop, up_w, up_h)
                saved_src = True
                log(f"wrote frames/{stem}_src.png frames/{stem}_vsr.png and matching 400x400 crops")

            try:
                enc.stdin.write(bytes(canvas))
            except (BrokenPipeError, OSError) as e:
                extra = b"".join(enc_err_buf).decode("utf-8", errors="replace").strip()
                raise JobError(f"encoder pipe closed on frame {n}: {e}" + (f"\n{extra}" if extra else "")) from e
            n += 1
            if progress:
                progress(n, media.frames)
            if n == 1 or n % 30 == 0:
                tot = f"/{media.frames}" if media.frames else ""
                log(f"frame {n}{tot}  {media.width}x{media.height} → {out_w}x{out_h} q{job.quality}")
    finally:
        try:
            if enc.stdin:
                enc.stdin.close()
        except Exception:
            pass
        if dec.poll() is None:
            dec.kill()
        try:
            dec.wait(timeout=10)
        except subprocess.TimeoutExpired:
            dec.kill()
        try:
            enc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            enc.kill()
        enc_err = b"".join(enc_err_buf).decode("utf-8", errors="replace")
        power.stop()

    if n == 0:
        raise JobError("no frames decoded")
    if enc.returncode not in (0, None):
        raise JobError(enc_err.strip() or f"ffmpeg encode failed ({enc.returncode})")
    if dec.returncode not in (0, None) and n == 0:
        raise JobError(f"ffmpeg decode failed ({dec.returncode})")

    log(f"done {n} frames → {out_path}")
    return out_path
