# RTX VSR Lab

Offline [NVIDIA RTX Video Super Resolution](https://www.nvidia.com/en-us/geforce/rtx-video/) for existing video files. Decode with FFmpeg, run VSR through the RTX Video SDK 1.1 (`nvngx_vsr.dll`), then encode HEVC with `libx265`.

Windows GUI and CLI. Quality 1–4 matches the NVIDIA App scale. Output is capped at 3840×2160. The pipeline never silently bicubic-scales a failed VSR pass and still calls it VSR.

## Requirements

- Windows 10/11 x64
- NVIDIA GPU that supports RTX Video Super Resolution (RTX 20/30/40/50)
- A current Game Ready or Studio driver
- Python 3.10 or newer
- FFmpeg and ffprobe with `libx265` (looked up in `C:\VSR\ffmpeg-9.0.1-full_build\bin`, then `PATH`)
- `nvngx_vsr.dll` from your NVIDIA driver or the RTX Video SDK — **not redistributed here**

Put `nvngx_vsr.dll` next to the app or in `C:\VSR`. Optional TrueHDR needs `nvngx_truehdr.dll` in the same place.

## Run

```bat
vsr_lab.cmd
```

or:

```bat
python -m vsr_lab
```

With no `--in` argument the Windows UI opens. Pick one or more MP4s, an output folder, a size, and **Run**.

### CLI

```bat
vsr_lab.cmd --in clip.mp4 --out C:\VSR\out --width 3840 --height 2160 --quality 4 --crf 12
```

| Flag | Meaning |
|------|---------|
| `--in` | Input MP4 |
| `--out` | Output file or folder (default `C:\VSR\out`) |
| `--width` / `--height` | Output canvas, max 3840×2160 (default 3840×2160) |
| `--quality` | VSR quality 1–4 (default 4) |
| `--crf` | x265 CRF (default 12) |
| `--thdr` | Enable TrueHDR (off by default) |
| `--stretch` | Fill the canvas; stretches the picture |
| `--no-detect-bars` | Skip letterbox/pillarbox detection |
| `--fallback` | If the exact size is rejected, VSR to the nearest accepted size then scale. The filename contains `vsrthen_scale` |
| `--probe` | Print accepted SDK size pairs and exit |
| `--dll-dir` | Extra search directory for `nvngx_vsr.dll` (default `C:\VSR`) |
| `--gui` | Force the Windows UI |

Default output name: `{stem}_vsr-{width}x{height}-q{quality}.mp4`.

## What it does

1. Probe the source with ffprobe (size, FPS, SAR, audio).
2. Reject sources below 640×360. Sources at or above 2560×1440 skip VSR (the SDK is not used as a downscaler).
3. Optionally detect baked-in letterbox/pillarbox and keep the picture aspect on a 1:1 SAR canvas.
4. Call NGX `Evaluate` once per frame through a small native wrapper (`vsr_lab/_vsr_ngx.dll`).
5. Encode `libx265` Main 8-bit `hvc1` with `+faststart`. Audio is copied when present.

Each job also writes first-frame PNGs under `frames/` and, if `nvidia-smi` is available, a power CSV under `logs/`.

## Build the native engine

Needed only if you change `native/` or do not have `vsr_lab/_vsr_ngx.dll`.

1. Install [Visual Studio 2022](https://visualstudio.microsoft.com/) (or Build Tools) with the C++ x64 workload.
2. Unpack [RTX Video SDK 1.1](https://developer.nvidia.com/rtx-video-sdk) so `include\nvsdk_ngx.h` exists. Default path: `C:\VSR\RTX_Video_SDK_v1.1.0`. Override with `NV_RTX_VIDEO_SDK`.
3. From the repo root:

```powershell
powershell -File native\build.ps1
```

That compiles `native/vsr_ngx.cpp` into `vsr_lab/_vsr_ngx.dll`.

## Limits

- VSR is not applied to sources ≥ 1440p.
- Downscaling is refused.
- If the SDK rejects a size pair, the job fails unless `--fallback` is set.
- TrueHDR stays off unless you pass `--thdr` and `nvngx_truehdr.dll` is present.

## License

This project is free software under the [GNU General Public License v3.0 or later](LICENSE).

NVIDIA’s RTX Video SDK, driver feature DLLs (`nvngx_vsr.dll`, `nvngx_truehdr.dll`), and trademarks (NVIDIA, RTX, NGX, …) remain NVIDIA’s. They are **not** part of this repository and are used under NVIDIA’s own terms.

This project is not affiliated with, endorsed by, or sponsored by NVIDIA Corporation.
