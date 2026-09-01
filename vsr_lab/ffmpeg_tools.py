"""Locate bundled ffmpeg/ffprobe and probe input files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
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
    return MediaInfo(
        path=path,
        width=w,
        height=h,
        fps=rate,
        fps_text=f"{rate.numerator}/{rate.denominator}",
        has_audio=a is not None,
        duration=duration,
        frames=frames,
        pix_fmt=str(v.get("pix_fmt") or ""),
        codec=str(v.get("codec_name") or ""),
    )


def write_png(path: Path, rgba: bytes, w: int, h: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg_exe()),
        "-hide_banner", "-loglevel", "error",
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
