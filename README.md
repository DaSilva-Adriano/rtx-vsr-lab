Put nvngx_vsr.dll next to the exe or in C:\VSR

```
vsr_lab.cmd
python -m vsr_lab --in clip-720p-24fps.mp4 --out clip-720p-24fps_vsr-3840x2160-q4.mp4 --width 3840 --height 2160 --quality 4 --no-thdr --crf 12
```

Build the NGX engine once: `powershell -File native\build.ps1`
ffmpeg/ffprobe: `C:\VSR\ffmpeg-9.0.1-full_build\bin`
