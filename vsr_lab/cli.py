"""CLI and GUI entry: vsr_lab / python -m vsr_lab."""

from __future__ import annotations

import argparse
import atexit
import sys
from pathlib import Path

from vsr_lab import __app_name__
from vsr_lab.engine import VsrEngine
from vsr_lab.pipeline import Job, JobError, default_outfile, run_job
from vsr_lab.sizes import MAX_H, MAX_W

DEFAULT_OUT_DIR = Path(r"C:\VSR\out")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vsr_lab",
        description="RTX VSR Lab — offline NVIDIA RTX Video Super Resolution (SDK 1.1).",
    )
    p.add_argument("--in", dest="inp", help="input mp4")
    p.add_argument("--out", dest="out", help="output mp4 path or folder")
    p.add_argument("--width", type=int, default=3840)
    p.add_argument("--height", type=int, default=2160)
    p.add_argument("--quality", type=int, default=4, help="VSR quality 1-4 (NVIDIA App scale, default 4)")
    p.add_argument("--no-thdr", action="store_true", default=False, help="disable TrueHDR (default)")
    p.add_argument("--thdr", action="store_true", default=False, help="enable TrueHDR")
    p.add_argument("--crf", type=int, default=12)
    p.add_argument("--stretch", action="store_true", help="ignore aspect ratio (fills the canvas; stretches film)")
    p.add_argument(
        "--no-detect-bars",
        action="store_true",
        help="do not detect baked-in letterbox/pillarbox (default: detect and keep picture aspect)",
    )
    p.add_argument(
        "--fallback",
        action="store_true",
        help="VSR to nearest supported size, then scale to target (filename contains vsrthen_scale)",
    )
    p.add_argument("--gui", action="store_true", help="open the Windows UI")
    p.add_argument("--probe", action="store_true", help="print SDK size probe and exit")
    p.add_argument("--dll-dir", default=r"C:\VSR", help="extra search dir for nvngx_vsr.dll")
    return p


def _engine(dll_dir: str) -> VsrEngine:
    eng = VsrEngine()
    eng.init(dll_dir)
    atexit.register(eng.shutdown)
    return eng


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    want_gui = args.gui or (not args.inp and not args.probe)

    try:
        engine = _engine(args.dll_dir)
    except Exception as e:
        print(f"{__app_name__}: {e}", file=sys.stderr)
        print("Put nvngx_vsr.dll next to the exe or in C:\\VSR", file=sys.stderr)
        return 2

    if want_gui:
        from vsr_lab.gui import launch

        launch(engine)
        return 0

    print("Probing VSR size pairs…")
    engine.probe(quality=4, log=lambda m: None)
    print(engine.capabilities_text())
    if args.probe:
        return 0

    if args.width > MAX_W or args.height > MAX_H or args.width < 2 or args.height < 2:
        print(f"output {args.width}x{args.height} not allowed (max {MAX_W}x{MAX_H})", file=sys.stderr)
        return 2
    if args.quality < 1 or args.quality > 4:
        print("quality must be 1-4", file=sys.stderr)
        return 2

    src = Path(args.inp)
    fallback = bool(args.fallback)
    if args.out:
        out = Path(args.out)
        if out.suffix.lower() == "" or out.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            out = default_outfile(src, out, args.width, args.height, args.quality, fallback)
    else:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = default_outfile(src, DEFAULT_OUT_DIR, args.width, args.height, args.quality, fallback)

    truehdr = bool(args.thdr) and not bool(args.no_thdr)
    job = Job(
        inp=src,
        out=out,
        width=int(args.width),
        height=int(args.height),
        quality=int(args.quality),
        truehdr=truehdr,
        stretch=bool(args.stretch),
        fallback=fallback,
        crf=int(args.crf),
        keep_picture_aspect=not bool(args.no_detect_bars),
    )
    try:
        path = run_job(engine, job, log=print)
    except JobError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
