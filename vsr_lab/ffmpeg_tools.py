"""Locate bundled ffmpeg/ffprobe and probe input files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

FFMPEG_DIR = Path(r"C:\VSR\ffmpeg-9.0.1-full_build\bin")


def _which(name: str) -> Path | None:
    env = os.environ.get(name.upper() + "_PATH")
    if env and Path(env).is_file():
        return Path(env)
    bundled = FFMPEG_DIR / f"{name}.exe"
    if bundled.is_file():
        return bundled
    found = shutil.which(name)
    return Path(found) if found else None


def ffmpeg_exe() -> Path:
    p = _which("ffmpeg")
    if not p:
        raise FileNotFoundError(r"ffmpeg.exe not found (expected C:\VSR\ffmpeg-9.0.1-full_build\bin)")
    return p


def ffprobe_exe() -> Path:
    p = _which("ffprobe")
    if not p:
        raise FileNotFoundError(r"ffprobe.exe not found (expected C:\VSR\ffmpeg-9.0.1-full_build\bin)")
    return p


def run_ff(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


@dataclass
class MediaInfo:
    path: Path
    width: int
    height: int
    fps: Fraction
    fps_text: str
    has_audio: bool
    duration: float
    frames: int | None
    pix_fmt: str
    codec: str
    sar_num: int = 1
    sar_den: int = 1


@dataclass(frozen=True)
class ActiveRect:
    x: int
    y: int
    w: int
    h: int
    frame_w: int
    frame_h: int

    @property
    def has_bars(self) -> bool:
        return self.x > 0 or self.y > 0 or self.w < self.frame_w or self.h < self.frame_h

    @property
    def aspect(self) -> float:
        return self.w / self.h if self.h else 0.0


def _parse_rate(s: str) -> Fraction:
    if not s or s in ("0/0", "N/A"):
        return Fraction(24, 1)
    try:
        return Fraction(s)
    except (ValueError, ZeroDivisionError):
        return Fraction(24, 1)


def probe_media(path: str | Path) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    cmd = [
        str(ffprobe_exe()),
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "ffprobe failed")
    data = json.loads(r.stdout)
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    w = int(v["width"])
    h = int(v["height"])
    rate = _parse_rate(v.get("avg_frame_rate") or v.get("r_frame_rate") or "24/1")
    if rate <= 0:
        rate = Fraction(24, 1)
    duration = 0.0
    try:
        duration = float(v.get("duration") or data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    frames = None
    if v.get("nb_frames") and str(v["nb_frames"]).isdigit():
        frames = int(v["nb_frames"])
    elif duration > 0:
        frames = int(round(float(rate) * duration))
    sar_num, sar_den = 1, 1
    sar = v.get("sample_aspect_ratio") or "1:1"
    if isinstance(sar, str) and ":" in sar and sar not in ("0:1", "N/A"):
        try:
            a, b = sar.split(":", 1)
            sa, sb = int(a), int(b)
            if sa > 0 and sb > 0:
                sar_num, sar_den = sa, sb
        except ValueError:
            pass
    return MediaInfo(
        path=path,
        width=w,
        height=h,
        fps=rate,
        fps_text=f"{rate.numerator}/{rate.denominator}",
        has_audio=audio is not None,
        duration=duration,
        frames=frames,
        pix_fmt=str(v.get("pix_fmt") or ""),
        codec=str(v.get("codec_name") or ""),
        sar_num=sar_num,
        sar_den=sar_den,
    )


_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def _even_down(v: int) -> int:
    return v - (v % 2)


def _even_up(v: int) -> int:
    return v if v % 2 == 0 else v + 1


def _median_int(vals: list[int]) -> int:
    if not vals:
        return 0
    s = sorted(vals)
    return s[len(s) // 2]


def _bar_from_edge(is_bar: list[bool]) -> int:
    """Count a matte from the start of is_bar; allow 2 stray non-bar rows."""
    n = len(is_bar)
    limit = n // 3
    i = 0
    miss = 0
    last = 0
    while i < limit:
        if is_bar[i]:
            last = i + 1
            miss = 0
        else:
            miss += 1
            if miss >= 3:
                break
        i += 1
    return last


def _bars_on_rgba(rgba: bytes, w: int, h: int, mean_max: float, spread_max: float) -> tuple[int, int, int, int]:
    """Return (top, bottom, left, right) bar sizes in pixels."""
    step = 4 if w >= 640 else 1
    xs = range(0, w, step)
    nx = len(xs)
    row_mean = [0.0] * h
    row_spread = [0.0] * h
    for y in range(h):
        base = y * w * 4
        s = 0
        mn, mx = 255, 0
        for x in xs:
            o = base + x * 4
            yv = (rgba[o] + rgba[o + 1] + rgba[o + 2]) // 3
            s += yv
            if yv < mn:
                mn = yv
            if yv > mx:
                mx = yv
        row_mean[y] = s / nx
        row_spread[y] = mx - mn
    row_bar = [row_mean[y] <= mean_max and row_spread[y] <= spread_max for y in range(h)]
    top = _bar_from_edge(row_bar)
    bot = _bar_from_edge(list(reversed(row_bar)))

    ys = range(0, h, step)
    ny = len(ys)
    col_mean = [0.0] * w
    col_spread = [0.0] * w
    for x in range(w):
        s = 0
        mn, mx = 255, 0
        for y in ys:
            o = (y * w + x) * 4
            yv = (rgba[o] + rgba[o + 1] + rgba[o + 2]) // 3
            s += yv
            if yv < mn:
                mn = yv
            if yv > mx:
                mx = yv
        col_mean[x] = s / ny
        col_spread[x] = mx - mn
    col_bar = [col_mean[x] <= mean_max and col_spread[x] <= spread_max for x in range(w)]
    left = _bar_from_edge(col_bar)
    right = _bar_from_edge(list(reversed(col_bar)))
    return top, bot, left, right


def _grab_rgba_frame(path: Path, seconds: float, w: int, h: int) -> bytes | None:
    cmd = [
        str(ffmpeg_exe()),
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, seconds):.3f}",
        "-i", str(path),
        "-frames:v", "1",
        "-an",
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "pipe:1",
    ]
    r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    need = w * h * 4
    if r.returncode == 0 and r.stdout and len(r.stdout) >= need:
        return r.stdout[:need]
    return None


def _clamp_rect(x: int, y: int, w: int, h: int, frame_w: int, frame_h: int) -> ActiveRect:
    full = ActiveRect(0, 0, frame_w, frame_h, frame_w, frame_h)
    x = max(0, _even_down(x))
    y = max(0, _even_down(y))
    w = _even_down(w)
    h = _even_down(h)
    if x + w > frame_w:
        w = _even_down(frame_w - x)
    if y + h > frame_h:
        h = _even_down(frame_h - y)
    if w < 640 or h < 360:
        return full
    if w * h < int(frame_w * frame_h * 0.40):
        return full
    if (frame_w - w) < 8 and (frame_h - h) < 8:
        return full
    return ActiveRect(x, y, w, h, frame_w, frame_h)


def detect_active_picture(
    path: str | Path,
    frame_w: int,
    frame_h: int,
    duration: float = 0.0,
) -> ActiveRect:
    """Find baked-in letterbox/pillarbox.

    Real film mattes are often TV-range / grainy, not RGB 0. Scan sampled frames for
    flat dark rows/columns and take the median matte. cropdetect is a fallback only.
    """
    path = Path(path)
    full = ActiveRect(0, 0, frame_w, frame_h, frame_w, frame_h)
    if duration and duration > 0:
        # Skip likely 16:9 cards at 0s; sample the body of the clip.
        times = [duration * f for f in (0.12, 0.28, 0.44, 0.60, 0.76)]
        times = [t for t in times if t < duration - 0.05]
    else:
        times = [0.0, 2.0, 8.0, 20.0, 45.0]

    samples: list[bytes] = []
    for t in times:
        fr = _grab_rgba_frame(path, t, frame_w, frame_h)
        if fr:
            samples.append(fr)
    if not samples:
        fr = _grab_rgba_frame(path, 0.0, frame_w, frame_h)
        if fr:
            samples.append(fr)
    if not samples:
        return _detect_cropdetect(path, frame_w, frame_h)

    # Try several luma/flatness gates; keep the strongest matte that still looks like bars.
    best = (0, 0, 0, 0)  # top, bot, left, right
    for mean_max, spread_max in ((22, 10), (32, 14), (42, 18), (52, 22), (64, 28)):
        tops, bots, lefts, rights = [], [], [], []
        for fr in samples:
            t, b, l, rgt = _bars_on_rgba(fr, frame_w, frame_h, mean_max, spread_max)
            tops.append(t)
            bots.append(b)
            lefts.append(l)
            rights.append(rgt)
        top = _even_down(_median_int(tops))
        bot = _even_down(_median_int(bots))
        left = _even_down(_median_int(lefts))
        right = _even_down(_median_int(rights))
        # Film mattes sit on both opposite edges. A single dark edge is usually picture.
        if top < 8 or bot < 8:
            top = bot = 0
        if left < 8 or right < 8:
            left = right = 0
        score = top + bot + left + right
        if score > sum(best):
            pic_h = frame_h - top - bot
            pic_w = frame_w - left - right
            if pic_w >= 640 and pic_h >= 360:
                best = (top, bot, left, right)

    top, bot, left, right = best
    if top < 8 and bot < 8 and left < 8 and right < 8:
        return _detect_cropdetect(path, frame_w, frame_h)
    return _clamp_rect(left, top, frame_w - left - right, frame_h - top - bot, frame_w, frame_h)


def _detect_cropdetect(path: Path, frame_w: int, frame_h: int) -> ActiveRect:
    full = ActiveRect(0, 0, frame_w, frame_h, frame_w, frame_h)
    # Per-frame crops (reset=1). Median, so one 16:9 card does not wipe the matte.
    cmd = [
        str(ffmpeg_exe()),
        "-hide_banner",
        "-nostdin",
        "-ss", "10",
        "-i", str(path),
        "-an",
        "-frames:v", "60",
        "-vf", "cropdetect=limit=32:round=2:reset=1",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    text = (r.stderr or "") + (r.stdout or "")
    matches = _CROP_RE.findall(text)
    if len(matches) < 3:
        cmd[cmd.index("cropdetect=limit=32:round=2:reset=1")] = "cropdetect=limit=48:round=2:reset=1"
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        text = (r.stderr or "") + (r.stdout or "")
        matches = _CROP_RE.findall(text)
    if not matches:
        return full
    hs = [int(m[1]) for m in matches]
    ys = [int(m[3]) for m in matches]
    ws = [int(m[0]) for m in matches]
    xs = [int(m[2]) for m in matches]
    return _clamp_rect(_median_int(xs), _median_int(ys), _median_int(ws), _median_int(hs), frame_w, frame_h)


def write_png(path: Path, rgba: bytes, w: int, h: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg_exe()),
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-y",
        "-f", "rawvideo",
        "-pix_fmt", "rgba",
        "-s", f"{w}x{h}",
        "-i", "pipe:0",
        "-frames:v", "1",
        str(path),
    ]
    r = subprocess.run(cmd, input=bytes(rgba), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", errors="replace") or "ffmpeg png failed")
