"""ctypes wrapper around vsr_ngx.dll (RTX Video SDK 1.1 / nvngx_vsr.dll)."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from vsr_lab.sizes import Pair, integer_scales, non_integer_ok, probe_pairs

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent


class VsrStatus(ctypes.Structure):
    _fields_ = [
        ("ok", ctypes.c_int),
        ("ngx_result", ctypes.c_int),
        ("message", ctypes.c_char * 512),
    ]


class VsrInfo(ctypes.Structure):
    _fields_ = [
        ("vsr_available", ctypes.c_int),
        ("truehdr_available", ctypes.c_int),
        ("needs_updated_driver", ctypes.c_int),
        ("min_driver_major", ctypes.c_int),
        ("min_driver_minor", ctypes.c_int),
        ("feature_init_result", ctypes.c_int),
        ("gpu_name", ctypes.c_char * 256),
        ("dll_dir", ctypes.c_char * 512),
        ("vsr_dll", ctypes.c_char * 512),
        ("truehdr_dll", ctypes.c_char * 512),
    ]


class VsrEvalDesc(ctypes.Structure):
    _fields_ = [
        ("in_w", ctypes.c_int),
        ("in_h", ctypes.c_int),
        ("out_w", ctypes.c_int),
        ("out_h", ctypes.c_int),
        ("content_x", ctypes.c_int),
        ("content_y", ctypes.c_int),
        ("content_w", ctypes.c_int),
        ("content_h", ctypes.c_int),
        ("quality", ctypes.c_int),
        ("truehdr", ctypes.c_int),
    ]


def _cstr(buf: bytes) -> str:
    return buf.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def find_native_dll() -> Path:
    names = ("_vsr_ngx.dll", "vsr_ngx.dll")
    search = [
        _HERE,
        _HERE / "native",
        _REPO / "native",
        Path(os.getcwd()),
        Path(r"C:\VSR"),
    ]
    exe = os.environ.get("VSR_LAB_NATIVE")
    if exe:
        p = Path(exe)
        if p.is_file():
            return p
    for folder in search:
        for name in names:
            p = folder / name
            if p.is_file():
                return p
    raise FileNotFoundError(
        "vsr_ngx.dll not built. From the repo run: powershell -File native\\build.ps1"
    )


class VsrEngine:
    def __init__(self) -> None:
        self.path = find_native_dll()
        self.lib = ctypes.WinDLL(str(self.path))
        lib = self.lib
        lib.vsr_init.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(VsrStatus)]
        lib.vsr_init.restype = ctypes.c_int
        lib.vsr_shutdown.argtypes = []
        lib.vsr_shutdown.restype = None
        lib.vsr_get_info.argtypes = [ctypes.POINTER(VsrInfo)]
        lib.vsr_get_info.restype = ctypes.c_int
        lib.vsr_try_size.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(VsrStatus),
        ]
        lib.vsr_try_size.restype = ctypes.c_int
        lib.vsr_evaluate.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(VsrEvalDesc), ctypes.POINTER(VsrStatus),
        ]
        lib.vsr_evaluate.restype = ctypes.c_int
        lib.vsr_resize_rgba.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        lib.vsr_resize_rgba.restype = ctypes.c_int
        lib.vsr_ngx_name.argtypes = [ctypes.c_int]
        lib.vsr_ngx_name.restype = ctypes.c_char_p
        self.info = VsrInfo()
        self.pairs: list[Pair] = []
        self._ready = False

    def ngx_name(self, code: int) -> str:
        p = self.lib.vsr_ngx_name(int(code))
        return p.decode("ascii", errors="replace") if p else str(code)

    def init(self, extra_dll_dir: str | None = None) -> VsrStatus:
        st = VsrStatus()
        extra = extra_dll_dir or r"C:\VSR"
        ok = self.lib.vsr_init(extra, ctypes.byref(st))
        self.lib.vsr_get_info(ctypes.byref(self.info))
        self._ready = bool(ok and st.ok)
        if not self._ready:
            raise RuntimeError(_cstr(st.message) or "vsr_init failed")
        return st

    def shutdown(self) -> None:
        try:
            self.lib.vsr_shutdown()
        except OSError:
            pass
        self._ready = False

    def try_size(
        self,
        in_w: int,
        in_h: int,
        out_w: int,
        out_h: int,
        quality: int = 4,
        out_x: int = 0,
        out_y: int = 0,
        content_w: int = 0,
        content_h: int = 0,
    ) -> Pair:
        st = VsrStatus()
        self.lib.vsr_try_size(
            int(in_w), int(in_h), int(out_w), int(out_h), int(quality),
            int(out_x), int(out_y), int(content_w or out_w), int(content_h or out_h),
            ctypes.byref(st),
        )
        return Pair(
            in_w=in_w, in_h=in_h, out_w=out_w, out_h=out_h,
            ok=bool(st.ok), ngx=int(st.ngx_result), message=_cstr(st.message),
        )

    def probe(self, quality: int = 4, log=None) -> list[Pair]:
        pairs: list[Pair] = []
        jobs = probe_pairs()
        n = len(jobs)
        for i, (iw, ih, ow, oh) in enumerate(jobs, 1):
            if log:
                log(f"probe {i}/{n}: {iw}x{ih} -> {ow}x{oh} q{quality}")
            pairs.append(self.try_size(iw, ih, ow, oh, quality=quality))
        self.pairs = pairs
        return pairs

    def evaluate(
        self,
        rgba_in: bytes | bytearray | memoryview,
        rgba_out: bytearray | memoryview,
        desc: VsrEvalDesc,
    ) -> VsrStatus:
        st = VsrStatus()
        in_buf = (ctypes.c_ubyte * len(rgba_in)).from_buffer_copy(rgba_in)
        out_buf = (ctypes.c_ubyte * len(rgba_out)).from_buffer(rgba_out)
        self.lib.vsr_evaluate(in_buf, out_buf, ctypes.byref(desc), ctypes.byref(st))
        return st

    def resize(
        self,
        src: bytes | bytearray | memoryview,
        sw: int,
        sh: int,
        dw: int,
        dh: int,
        filter: int = 1,
    ) -> bytearray:
        dst = bytearray(dw * dh * 4)
        src_buf = (ctypes.c_ubyte * len(src)).from_buffer_copy(src)
        dst_buf = (ctypes.c_ubyte * len(dst)).from_buffer(dst)
        ok = self.lib.vsr_resize_rgba(src_buf, sw, sh, dst_buf, dw, dh, int(filter))
        if not ok:
            raise RuntimeError("vsr_resize_rgba failed")
        return dst

    def capabilities_text(self) -> str:
        inf = self.info
        lines = [
            f"GPU: {_cstr(inf.gpu_name) or '(unknown)'}",
            f"Engine DLL: {self.path}",
            f"nvngx_vsr.dll: {_cstr(inf.vsr_dll) or 'not found'}",
            f"nvngx_truehdr.dll: {_cstr(inf.truehdr_dll) or 'not found'}",
            f"VSR available: {'yes' if inf.vsr_available else 'no'}",
            f"TrueHDR available: {'yes' if inf.truehdr_available else 'no'}",
        ]
        if inf.needs_updated_driver:
            lines.append(
                f"Driver: UPDATE REQUIRED (min {inf.min_driver_major}.{inf.min_driver_minor})"
            )
        elif inf.min_driver_major or inf.min_driver_minor:
            lines.append(f"Driver: OK (min {inf.min_driver_major}.{inf.min_driver_minor})")
        else:
            lines.append("Driver: OK")

        ints = integer_scales(self.pairs)
        ni = non_integer_ok(self.pairs)
        if ints:
            lines.append("")
            kind = "integer only" if not ni else "integer + non-integer"
            lines.append(f"Accepted scale factors: {', '.join(str(s) + 'x' for s in ints)} ({kind})")
            if 2 in ints:
                lines.append("  2x  1080p→4K, 720p→1440p")
            if 3 in ints:
                lines.append("  3x  720p→4K, 360p→1080p")
            if 4 in ints:
                lines.append("  4x  540p→4K, 480p→1920p")
            six = next((p for p in self.pairs if p.in_w == 640 and p.in_h == 360 and p.out_w == 3840 and p.out_h == 2160), None)
            if six is None:
                lines.append("  6x  360p→4K: not probed")
            elif six.ok:
                lines.append("  6x  360p→4K: accepted")
            else:
                lines.append(
                    f"  6x  360p→4K: REJECTED ({self.ngx_name(six.ngx)}). "
                    "Live VSR can do this to a 4K window; this SDK DLL cannot. Not faked."
                )
            if not ni:
                lines.append("Non-integer scales (e.g. 720p→1080p 1.5x) were not accepted.")
        lines.append("")
        lines.append("Probe pairs (quality 4, Evaluate on dummy surfaces):")
        for p in self.pairs:
            mark = "OK" if p.ok else f"FAIL {self.ngx_name(p.ngx)}"
            lines.append(f"  {p.in_w}x{p.in_h} → {p.out_w}x{p.out_h}  {p.scale:.2f}x  {mark}")
        return "\n".join(lines)
