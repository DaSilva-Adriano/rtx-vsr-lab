"""Win32-style tkinter UI for RTX VSR Lab."""

from __future__ import annotations

import ctypes
import queue
import threading
import tkinter as tk
from ctypes import wintypes
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
_INPUT_SEP = ";"


def _existing_dir(path: str) -> str:
    p = Path(path) if path else DEFAULT_OUT
    try:
        p = p.expanduser()
    except Exception:
        p = DEFAULT_OUT
    for candidate in (p, *p.parents):
        try:
            if candidate.is_dir():
                return str(candidate)
        except OSError:
            continue
    home = Path.home()
    return str(home) if home.is_dir() else "C:\\"


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    )


def _guid(value: str) -> _GUID:
    g = _GUID()
    CLSIDFromString = ctypes.windll.ole32.CLSIDFromString
    CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_GUID)]
    CLSIDFromString.restype = ctypes.c_long
    hr = CLSIDFromString(value, ctypes.byref(g))
    if hr:
        raise OSError(hr, f"CLSIDFromString failed for {value}")
    return g


def _pick_folder_com(title: str, initial: str) -> str | None:
    """Vista+ folder picker on this thread.

    Tk `askdirectory` uses SHBrowseForFolder and deadlocks after NGX/D3D11
    initializes COM on the UI thread. IFileOpenDialog on a fresh STA thread
    does not.
    """
    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    HRESULT = ctypes.c_long
    CLSCTX_INPROC_SERVER = 1
    FOS_PICKFOLDERS = 0x20
    FOS_FORCEFILESYSTEM = 0x40
    FOS_NOCHANGEDIR = 0x8
    SIGDN_FILESYSPATH = 0x80058000
    COINIT_APARTMENTTHREADED = 0x2
    COINIT_DISABLE_OLE1DDE = 0x4

    CoInitializeEx = ole32.CoInitializeEx
    CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    CoInitializeEx.restype = HRESULT
    init_hr = CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    uninit = init_hr in (0, 1)

    class _Vtbl(ctypes.Structure):
        _fields_ = [("fns", ctypes.c_void_p * 28)]

    class _COM(ctypes.Structure):
        _fields_ = [("lpVtbl", ctypes.POINTER(_Vtbl))]

    def vcall(obj: ctypes.c_void_p, index: int, restype, *argtypes):
        com = ctypes.cast(obj, ctypes.POINTER(_COM)).contents
        fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(com.lpVtbl.contents.fns[index])

        def _call(*args):
            return fn(obj, *args)

        return _call

    def release(obj: ctypes.c_void_p) -> None:
        if obj:
            vcall(obj, 2, wintypes.ULONG)()

    dlg = ctypes.c_void_p()
    try:
        clsid = _guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")
        iid_dlg = _guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")
        iid_item = _guid("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")
        CoCreateInstance = ole32.CoCreateInstance
        CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        CoCreateInstance.restype = HRESULT
        hr = CoCreateInstance(
            ctypes.byref(clsid),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(iid_dlg),
            ctypes.byref(dlg),
        )
        if hr or not dlg:
            return None

        get_opts = vcall(dlg, 10, HRESULT, ctypes.POINTER(wintypes.DWORD))
        set_opts = vcall(dlg, 9, HRESULT, wintypes.DWORD)
        set_title = vcall(dlg, 17, HRESULT, wintypes.LPCWSTR)
        set_folder = vcall(dlg, 12, HRESULT, ctypes.c_void_p)
        show = vcall(dlg, 3, HRESULT, wintypes.HWND)
        get_result = vcall(dlg, 20, HRESULT, ctypes.POINTER(ctypes.c_void_p))

        opts = wintypes.DWORD()
        get_opts(ctypes.byref(opts))
        set_opts(opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_NOCHANGEDIR)
        if title:
            set_title(title)

        if initial:
            item = ctypes.c_void_p()
            SHCreateItemFromParsingName = shell32.SHCreateItemFromParsingName
            SHCreateItemFromParsingName.argtypes = [
                wintypes.LPCWSTR,
                ctypes.c_void_p,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            SHCreateItemFromParsingName.restype = HRESULT
            if SHCreateItemFromParsingName(
                initial, None, ctypes.byref(iid_item), ctypes.byref(item)
            ) == 0 and item:
                set_folder(item)
                release(item)

        user32 = ctypes.windll.user32
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        WS_EX_TOPMOST = 0x00000008
        WS_EX_TOOLWINDOW = 0x00000080
        WS_POPUP = 0x80000000
        owner = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            "STATIC",
            None,
            WS_POPUP,
            0,
            0,
            0,
            0,
            None,
            None,
            None,
            None,
        )
        try:
            try:
                user32.AllowSetForegroundWindow(-1)
            except Exception:
                pass
            if owner:
                user32.SetForegroundWindow(owner)
            hr = show(owner)
        finally:
            if owner:
                user32.DestroyWindow(owner)
        if hr:
            return ""

        result = ctypes.c_void_p()
        if get_result(ctypes.byref(result)) or not result:
            return ""
        try:
            get_name = vcall(result, 5, HRESULT, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
            ppsz = wintypes.LPWSTR()
            if get_name(SIGDN_FILESYSPATH, ctypes.byref(ppsz)) or not ppsz:
                return ""
            path = ppsz.value or ""
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree(ppsz)
            return path
        finally:
            release(result)
    finally:
        release(dlg)
        if uninit:
            ole32.CoUninitialize()


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
        self._picking_out = False
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
        ttk.Label(row, text="Input file(s)").pack(side=tk.LEFT)
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

    def _input_paths(self) -> list[Path]:
        raw = self.var_in.get().strip()
        if not raw:
            return []
        parts = [p.strip().strip('"') for p in raw.split(_INPUT_SEP) if p.strip()]
        return [Path(p) for p in parts]

    def _browse_in(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Input video(s) — hold Ctrl to pick more than one",
            filetypes=[("MP4", "*.mp4"), ("All", "*.*")],
            parent=self,
        )
        if not paths:
            return
        if isinstance(paths, (str, Path)):
            paths = (paths,)
        files = [str(Path(p)) for p in paths if str(p).strip()]
        if not files:
            return
        self.var_in.set(f"{_INPUT_SEP} ".join(files))
        try:
            first = files[0]
            m = probe_media(first)
            self.src_w, self.src_h = m.width, m.height
            if len(files) == 1:
                self._log(f"source {m.width}x{m.height} {m.fps_text} fps")
            else:
                self._log(f"{len(files)} files selected; first source {m.width}x{m.height} {m.fps_text} fps")
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
        if self._picking_out:
            return
        initial = _existing_dir(self.var_out_dir.get().strip() or str(DEFAULT_OUT))
        self._picking_out = True
        holder: list[str | None] = []
        err: list[str] = []
        done = threading.Event()

        def worker() -> None:
            try:
                holder.append(_pick_folder_com("Output folder", initial))
            except Exception as e:
                holder.append(None)
                err.append(str(e))
            finally:
                done.set()

        threading.Thread(target=worker, name="vsr-folder-pick", daemon=True).start()

        def poll() -> None:
            if not done.is_set():
                self.after(50, poll)
                return
            self._picking_out = False
            result = holder[0] if holder else None
            if result is None:
                if err:
                    self._log(f"native folder picker failed: {err[0]}")
                p = filedialog.asksaveasfilename(
                    parent=self,
                    title="Output folder — pick any name in the destination folder (the file is not created)",
                    initialdir=initial,
                    initialfile="output.mp4",
                    defaultextension=".mp4",
                    filetypes=[("MP4", "*.mp4"), ("All", "*.*")],
                )
                if p:
                    self.var_out_dir.set(str(Path(p).parent))
                return
            if result:
                self.var_out_dir.set(result)

        poll()

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
        sources = self._input_paths()
        if not sources:
            messagebox.showerror(__app_name__, "Pick an input file")
            return
        missing = [p for p in sources if not p.is_file()]
        if missing:
            messagebox.showerror(
                __app_name__,
                "Input not found:\n" + "\n".join(str(p) for p in missing),
            )
            return
        try:
            w, h = self._target_wh()
        except JobError as e:
            messagebox.showerror(__app_name__, str(e))
            return
        out_dir = Path(self.var_out_dir.get().strip() or DEFAULT_OUT)
        fallback = bool(self.var_fallback.get())
        quality = int(self.var_q.get())
        jobs = [
            Job(
                inp=src,
                out=default_outfile(src, out_dir, w, h, quality, fallback),
                width=w,
                height=h,
                quality=quality,
                truehdr=bool(self.var_thdr.get()),
                stretch=bool(self.var_stretch.get()),
                fallback=fallback,
                crf=int(self.var_crf.get()),
                keep_picture_aspect=bool(self.var_keep.get()),
                out_dir=out_dir,
            )
            for src in sources
        ]
        self.running = True
        self.run_btn.configure(state=tk.DISABLED)
        if len(jobs) == 1:
            self._log(f"Run {jobs[0].inp.name} → {jobs[0].out.name}")
        else:
            self._log(f"Run {len(jobs)} files → {out_dir}")

        def work():
            written: list[Path] = []
            errors: list[str] = []
            n = len(jobs)
            try:
                for i, job in enumerate(jobs, 1):
                    if n > 1:
                        self.q.put(("log", f"=== [{i}/{n}] {job.inp.name} → {job.out.name} ==="))
                    try:
                        path = run_job(self.engine, job, log=lambda m: self.q.put(("log", m)))
                        written.append(path)
                    except Exception as e:
                        errors.append(f"{job.inp.name}: {e}")
                        self.q.put(("log", f"FAILED {job.inp.name}: {e}"))
                if errors and not written:
                    self.q.put(("err", "\n\n".join(errors)))
                elif errors:
                    msg = (
                        f"Wrote {len(written)} file(s), {len(errors)} failed:\n"
                        + "\n".join(str(p) for p in written)
                        + "\n\n"
                        + "\n\n".join(errors)
                    )
                    self.q.put(("err", msg))
                elif len(written) == 1:
                    self.q.put(("done", f"Wrote {written[0]}"))
                else:
                    self.q.put(
                        ("done", f"Wrote {len(written)} files:\n" + "\n".join(str(p) for p in written))
                    )
            except Exception as e:
                self.q.put(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()


def launch(engine: VsrEngine) -> None:
    app = App(engine)
    app.mainloop()
