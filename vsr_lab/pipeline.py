"""Decode → one VSR pass → x265 encode. No silent scale fallback."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from vsr_lab.engine import VsrEngine, VsrEvalDesc, _cstr
from vsr_lab.ffmpeg_tools import ffmpeg_exe, probe_media, write_png
from vsr_lab.power import PowerLogger
from vsr_lab.sizes import (
    MAX_H,
    MAX_W,
    Pair,
    clamp_output,
    fit_content,
    nearest_pair,
    reject_source,
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


def run_job(engine: VsrEngine, job: Job, log=lambda *_: None, progress=None) -> Path:
    src = Path(job.inp)
    media = probe_media(src)
    log(f"input {media.width}x{media.height} {media.fps_text} fps  {media.codec} {media.pix_fmt}  audio={'yes' if media.has_audio else 'no'}")

    err = reject_source(media.width, media.height)
    if err:
        raise JobError(err)

    out_w, out_h = clamp_output(job.width, job.height)
    if out_w != job.width or out_h != job.height:
        log(f"clamped output {job.width}x{job.height} → {out_w}x{out_h} (max {MAX_W}x{MAX_H}, even)")
    if would_downscale(media.width, media.height, out_w, out_h, job.stretch):
        raise JobError(
            f"Do not downscale: source {media.width}x{media.height} → requested {out_w}x{out_h}"
        )
    if job.quality < 1 or job.quality > 4:
        raise JobError("quality must be 1-4 (NVIDIA App scale; 0 is bicubic, not VSR)")

    cx, cy, cw, ch, fit_scale = fit_content(media.width, media.height, out_w, out_h, job.stretch)
    used_fallback = False
    nearest: Pair | None = None

    log(f"output canvas {out_w}x{out_h}  content {cw}x{ch} at ({cx},{cy})  scale {fit_scale:.3f}x  q{job.quality}")
    log(f"SDK status: VSR={'yes' if engine.info.vsr_available else 'no'}  TrueHDR={'yes' if engine.info.truehdr_available else 'no'}")
    if job.truehdr and not engine.info.truehdr_available:
        raise JobError("TrueHDR requested but nvngx_truehdr.dll / feature is not available")

    probe = engine.try_size(media.width, media.height, cw, ch, quality=job.quality)
    if not probe.ok:
        nearest = nearest_pair(media.width, media.height, cw, ch, engine.pairs)
        near_txt = (
            f"{nearest.in_w}x{nearest.in_h} → {nearest.out_w}x{nearest.out_h} ({nearest.scale:.2f}x)"
            if nearest else "none from startup probe"
        )
        msg = (
            f"Evaluate() failed for {media.width}x{media.height} → {cw}x{ch} "
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
            f"Fallback: VSR {nearest.in_w}x{nearest.in_h} → {nearest.out_w}x{nearest.out_h}, "
            f"then scale to {out_w}x{out_h} (filename will contain vsrthen_scale)"
        )
        cw, ch = nearest.out_w, nearest.out_h
        cx, cy = 0, 0
        if not job.stretch:
            # After VSR to nearest, letterbox-scale onto the user canvas.
            pass

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
        ff, "-hide_banner", "-loglevel", "error",
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
        "-x265-params", "log-level=error",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]
    log("encode: libx265 yuv420p main hvc1 crf=%s preset=medium (not NVENC)" % job.crf)

    in_bytes = media.width * media.height * 4
    vsr_bytes = cw * ch * 4
    out_bytes = out_w * out_h * 4
    desc = VsrEvalDesc(
        in_w=media.width, in_h=media.height,
        out_w=cw if used_fallback else out_w,
        out_h=ch if used_fallback else out_h,
        content_x=0 if used_fallback else cx,
        content_y=0 if used_fallback else cy,
        content_w=cw, content_h=ch,
        quality=job.quality, truehdr=1 if job.truehdr else 0,
    )

    power = PowerLogger(logs_dir / f"{stem}_power.csv", gpu_name=_cstr(engine.info.gpu_name))
    power_ok = power.start()
    if power_ok:
        log(f"power log (offline VSR encode power, not live playback): {power.csv_path}")
    else:
        log("nvidia-smi not available; skipping power log")

    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert dec.stdout and enc.stdin

    saved_src = False
    n = 0
    try:
        while True:
            buf = dec.stdout.read(in_bytes)
            if not buf:
                break
            if len(buf) != in_bytes:
                raise JobError(f"short decode read ({len(buf)} / {in_bytes})")

            if used_fallback:
                vsr_out = bytearray(vsr_bytes)
                desc.out_w = cw
                desc.out_h = ch
                desc.content_x = 0
                desc.content_y = 0
                desc.content_w = cw
                desc.content_h = ch
                st = engine.evaluate(buf, vsr_out, desc)
                if not st.ok:
                    raise JobError(f"Evaluate() failed on frame {n}: {_cstr(st.message)}")
                scaled = engine.resize(vsr_out, cw, ch, out_w, out_h, filter=1)
                canvas = scaled
            else:
                canvas = bytearray(out_bytes)
                st = engine.evaluate(buf, canvas, desc)
                if not st.ok:
                    nearest = nearest_pair(media.width, media.height, cw, ch, engine.pairs)
                    near_txt = (
                        f"{nearest.out_w}x{nearest.out_h}" if nearest else "unknown"
                    )
                    raise JobError(
                        f"Evaluate() failed on frame {n} for {media.width}x{media.height} → {cw}x{ch}: "
                        f"{_cstr(st.message)}. Nearest supported: {near_txt}. "
                        "Not silently scaling."
                    )

            if not saved_src:
                raw_out = bytes(canvas)
                write_png(frames_dir / f"{stem}_src.png", buf, media.width, media.height)
                write_png(frames_dir / f"{stem}_vsr.png", raw_out, out_w, out_h)
                crop, sx, sy, scw, sch = _center_crop(buf, media.width, media.height, 400, 400)
                if used_fallback:
                    scale_x = out_w / media.width
                    scale_y = out_h / media.height
                    ox, oy = 0, 0
                else:
                    scale_x = cw / media.width
                    scale_y = ch / media.height
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

            enc.stdin.write(canvas)
            n += 1
            if progress:
                progress(n, media.frames)
            if n == 1 or n % 30 == 0:
                tot = f"/{media.frames}" if media.frames else ""
                log(f"frame {n}{tot}  {media.width}x{media.height} → {out_w}x{out_h} q{job.quality}")
    finally:
        try:
            enc.stdin.close()
        except Exception:
            pass
        dec_err = dec.stderr.read().decode("utf-8", errors="replace") if dec.stderr else ""
        enc_err = enc.stderr.read().decode("utf-8", errors="replace") if enc.stderr else ""
        dec.wait(timeout=30)
        enc.wait(timeout=120)
        power.stop()

    if n == 0:
        raise JobError("no frames decoded")
    if enc.returncode not in (0, None):
        raise JobError(enc_err.strip() or f"ffmpeg encode failed ({enc.returncode})")
    if dec.returncode not in (0, None) and n == 0:
        raise JobError(dec_err.strip() or f"ffmpeg decode failed ({dec.returncode})")

    log(f"done {n} frames → {out_path}")
    return out_path
