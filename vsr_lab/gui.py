"""Win32-style tkinter UI for RTX VSR Lab."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from vsr_lab import __app_name__
from vsr_lab.engine import VsrEngine
from vsr_lab.ffmpeg_tools import probe_media
from vsr_lab.pipeline import Job, JobError, default_outfile, run_job
from vsr_lab.sizes import PRESETS, MAX_H, MAX_W, preset_enabled

BG = "#1b1b1b"
BG2 = "#111111"
FG = "#e8e8e8"
ACCENT = "#76b900"
MUTED = "#9a9a9a"
BTN_BG = "#2c2c2c"
DEFAULT_OUT = Path(r"C:\VSR\out")


class App(tk.Tk):
    def __init__(self, engine: VsrEngine) -> None:
        super().__init__()
        self.engine = engine
        self.title(__app_name__)
        self.configure(bg=BG)
        self.geometry("920x780")
        self.minsize(820, 680)
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self.src_w = 0
        self.src_h = 0

        self.var_in = tk.StringVar()
        self.var_out_dir = tk.StringVar(value=str(DEFAULT_OUT))
        self.var_preset = tk.StringVar(value="3840x2160")
        self.var_cw = tk.StringVar(value="3840")
        self.var_ch = tk.StringVar(value="2160")
        self.var_q = tk.IntVar(value=4)
        self.var_thdr = tk.BooleanVar(value=False)
        self.var_stretch = tk.BooleanVar(value=False)
        self.var_keep = tk.BooleanVar(value=True)
        self.var_fallback = tk.BooleanVar(value=False)
        self.var_crf = tk.IntVar(value=12)

        self._style()
        self._build()
        self.after(50, self._drain)
        self.after(100, self._probe_async)

    def _style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=BG2)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("TFrame", background=BG)
        st.configure("TLabelframe", background=BG, foreground=ACCENT)
        st.configure("TLabelframe.Label", background=BG, foreground=ACCENT)
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.configure("TRadiobutton", background=BG, foreground=FG)
        st.configure("TButton", background=BTN_BG, foreground=FG)
        st.configure("Accent.TButton", background=ACCENT, foreground="#111")
        st.map("TButton", background=[("active", "#3a3a3a")])
        st.configure("TSpinbox", fieldbackground=BG2, foreground=FG)
        st.configure("TEntry", fieldbackground=BG2, foreground=FG)

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}
        top = ttk.Frame(self)
        top.pack(fill=tk.BOTH, expand=True)

        ttk.Label(top, text=__app_name__, font=("Segoe UI", 16, "bold"), foreground=ACCENT).pack(
            anchor="w", **pad
        )
        ttk.Label(
            top,
            text="Offline RTX Video Super Resolution via nvngx_vsr.dll / SDK 1.1. Not RTXVideoProcessor.exe.",
            foreground=MUTED,
        ).pack(anchor="w", padx=10)

        capf = ttk.LabelFrame(top, text="Capabilities")
        capf.pack(fill=tk.BOTH, padx=10, pady=6)
        self.caps = tk.Text(capf, height=10, bg=BG2, fg=FG, insertbackground=FG, relief=tk.FLAT, wrap=tk.WORD)
        self.caps.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.caps.insert("1.0", "Probing local nvngx_vsr.dll input/output sizes…")
        self.caps.configure(state=tk.DISABLED)

        row = ttk.Frame(top)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="Input file").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.var_in).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(row, text="Browse", command=self._browse_in).pack(side=tk.LEFT)

        row = ttk.Frame(top)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="Output folder").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.var_out_dir).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(row, text="Browse", command=self._browse_out).pack(side=tk.LEFT)

        sizef = ttk.LabelFrame(top, text="Output size (max 3840×2160, never downscale)")
        sizef.pack(fill=tk.X, padx=10, pady=6)
        rf = ttk.Frame(sizef)
        rf.pack(fill=tk.X, padx=6, pady=4)
        for w, h, label in PRESETS:
            ttk.Radiobutton(
                rf,
                text=f"{w}×{h}  {label}" + ("  (default)" if (w, h) == (3840, 2160) else ""),
                value=f"{w}x{h}",
                variable=self.var_preset,
                command=self._on_preset,
            ).pack(side=tk.LEFT, padx=6)
        self.preset_btns = list(rf.winfo_children())
        cf = ttk.Frame(sizef)
        cf.pack(fill=tk.X, padx=6, pady=4)
        ttk.Radiobutton(cf, text="Custom", value="custom", variable=self.var_preset, command=self._on_preset).pack(
            side=tk.LEFT
        )
        ttk.Entry(cf, textvariable=self.var_cw, width=8).pack(side=tk.LEFT, padx=4)
        ttk.Label(cf, text="×").pack(side=tk.LEFT)
        ttk.Entry(cf, textvariable=self.var_ch, width=8).pack(side=tk.LEFT, padx=4)

        opt = ttk.Frame(top)
        opt.pack(fill=tk.X, **pad)
        ttk.Label(opt, text="Quality 1–4").pack(side=tk.LEFT)
        ttk.Spinbox(opt, from_=1, to=4, textvariable=self.var_q, width=4).pack(side=tk.LEFT, padx=8)
        ttk.Label(opt, text="CRF").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Spinbox(opt, from_=0, to=28, textvariable=self.var_crf, width=4).pack(side=tk.LEFT, padx=8)
        ttk.Checkbutton(opt, text="TrueHDR (default OFF)", variable=self.var_thdr).pack(side=tk.LEFT, padx=12)
        ttk.Checkbutton(
            opt, text="Stretch (fill canvas)", variable=self.var_stretch, command=self._on_stretch
        ).pack(side=tk.LEFT)

        ttk.Checkbutton(
            top,
            text="Keep picture aspect — detect film letterbox/pillarbox inside the frame (default ON)",
            variable=self.var_keep,
            command=self._on_keep,
        ).pack(anchor="w", padx=10)

        ttk.Checkbutton(
            top,
            text='Advanced: Fallback — VSR to nearest supported size, then scale to target (filename will contain "vsrthen_scale")',
            variable=self.var_fallback,
        ).pack(anchor="w", padx=10)

        self.run_btn = ttk.Button(top, text="Run", command=self._run, style="Accent.TButton")
        self.run_btn.pack(anchor="e", padx=10, pady=8)

        logf = ttk.LabelFrame(top, text="Log")
        logf.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log = tk.Text(logf, height=12, bg=BG2, fg=FG, insertbackground=FG, relief=tk.FLAT, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    def _set_caps(self, text: str) -> None:
        self.caps.configure(state=tk.NORMAL)
        self.caps.delete("1.0", tk.END)
        self.caps.insert("1.0", text)
        self.caps.configure(state=tk.DISABLED)

    def _on_keep(self) -> None:
        if self.var_keep.get():
            self.var_stretch.set(False)

    def _on_stretch(self) -> None:
        if self.var_stretch.get():
            self.var_keep.set(False)

    def _on_preset(self) -> None:
        v = self.var_preset.get()
        if "x" in v and v != "custom":
            w, h = v.split("x")
            self.var_cw.set(w)
            self.var_ch.set(h)

    def _browse_in(self) -> None:
        p = filedialog.askopenfilename(
            title="Input video",
            filetypes=[("MP4", "*.mp4"), ("All", "*.*")],
        )
        if not p:
            return
        self.var_in.set(p)
        try:
            m = probe_media(p)
            self.src_w, self.src_h = m.width, m.height
            self._log(f"source {m.width}x{m.height} {m.fps_text} fps")
            self._refresh_presets()
        except Exception as e:
            messagebox.showerror(__app_name__, str(e))

    def _refresh_presets(self) -> None:
        if not self.src_w:
            return
        for btn, (w, h, _label) in zip(self.preset_btns, PRESETS):
            on = preset_enabled(self.src_w, self.src_h, w, h)
            try:
                btn.configure(state=tk.NORMAL if on else tk.DISABLED)
            except tk.TclError:
                pass
        cur = self.var_preset.get()
        if "x" in cur and cur != "custom":
            w, h = map(int, cur.split("x"))
            if not preset_enabled(self.src_w, self.src_h, w, h):
                self.var_preset.set("3840x2160")
                self._on_preset()

    def _browse_out(self) -> None:
        p = filedialog.askdirectory(title="Output folder", initialdir=self.var_out_dir.get() or str(DEFAULT_OUT))
        if p:
            self.var_out_dir.set(p)

    def _log(self, msg: str) -> None:
        self.log.insert(tk.END, msg.rstrip() + "\n")
        self.log.see(tk.END)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "caps":
                    self._set_caps(payload)
                elif kind == "done":
                    self.running = False
                    self.run_btn.configure(state=tk.NORMAL)
                    if payload:
                        messagebox.showinfo(__app_name__, payload)
                elif kind == "err":
                    self.running = False
                    self.run_btn.configure(state=tk.NORMAL)
                    messagebox.showerror(__app_name__, payload)
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _probe_async(self) -> None:
        def work():
            try:
                def log(m):
                    self.q.put(("log", m))

                log("SDK init / size probe…")
                self.engine.probe(quality=4, log=None)
                self.q.put(("caps", self.engine.capabilities_text()))
                log("probe complete")
            except Exception as e:
                self.q.put(("caps", f"Probe failed:\n{e}"))
                self.q.put(("log", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _target_wh(self) -> tuple[int, int]:
        try:
            w = int(self.var_cw.get())
            h = int(self.var_ch.get())
        except ValueError as e:
            raise JobError("custom width/height must be integers") from e
        if w < 2 or h < 2:
            raise JobError("output size too small")
        if w > MAX_W or h > MAX_H:
            raise JobError(f"output {w}x{h} exceeds {MAX_W}x{MAX_H}")
        return w, h

    def _run(self) -> None:
        if self.running:
            return
        inp = self.var_in.get().strip()
        if not inp:
            messagebox.showerror(__app_name__, "Pick an input file")
            return
        try:
            w, h = self._target_wh()
        except JobError as e:
            messagebox.showerror(__app_name__, str(e))
            return
        out_dir = Path(self.var_out_dir.get().strip() or DEFAULT_OUT)
        src = Path(inp)
        fallback = bool(self.var_fallback.get())
        out = default_outfile(src, out_dir, w, h, int(self.var_q.get()), fallback)
        job = Job(
            inp=src,
            out=out,
            width=w,
            height=h,
            quality=int(self.var_q.get()),
            truehdr=bool(self.var_thdr.get()),
            stretch=bool(self.var_stretch.get()),
            fallback=fallback,
            crf=int(self.var_crf.get()),
            keep_picture_aspect=bool(self.var_keep.get()),
            out_dir=out_dir,
        )
        self.running = True
        self.run_btn.configure(state=tk.DISABLED)
        self._log(f"Run {src.name} → {out.name}")

        def work():
            try:
                path = run_job(self.engine, job, log=lambda m: self.q.put(("log", m)))
                self.q.put(("done", f"Wrote {path}"))
            except Exception as e:
                self.q.put(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()


def launch(engine: VsrEngine) -> None:
    app = App(engine)
    app.mainloop()
