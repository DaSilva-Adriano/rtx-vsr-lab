"""Output presets, input limits, aspect-ratio fit, nearest supported size."""

from __future__ import annotations

from dataclasses import dataclass

MAX_W = 3840
MAX_H = 2160
MIN_W = 640
MIN_H = 360
VSR_SRC_REJECT_W = 2560
VSR_SRC_REJECT_H = 1440

PRESETS = (
    (1280, 720, "720p"),
    (1920, 1080, "1080p"),
    (2560, 1440, "1440p"),
    (3840, 2160, "2160p"),
)

PROBE_INPUTS = (
    (640, 360),
    (854, 480),
    (1280, 720),
    (1920, 1080),
)
PROBE_OUTPUTS = (
    (1280, 720),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
)
PROBE_SCALES = (2, 3, 4, 6)


@dataclass(frozen=True)
class Pair:
    in_w: int
    in_h: int
    out_w: int
    out_h: int
    ok: bool
    ngx: int
    message: str

    @property
    def scale_x(self) -> float:
        return self.out_w / self.in_w if self.in_w else 0.0

    @property
    def scale_y(self) -> float:
        return self.out_h / self.in_h if self.in_h else 0.0

    @property
    def scale(self) -> float:
        return (self.scale_x + self.scale_y) / 2.0


def even(v: int) -> int:
    return v - (v % 2)


def reject_source(w: int, h: int) -> str | None:
    if w < MIN_W or h < MIN_H:
        return f"Reject: source {w}x{h} is below {MIN_W}x{MIN_H}"
    if w >= VSR_SRC_REJECT_W and h >= VSR_SRC_REJECT_H:
        return f"Reject: VSR is not run on sources >= {VSR_SRC_REJECT_W}x{VSR_SRC_REJECT_H} ({w}x{h})"
    return None


def display_dims(w: int, h: int, sar_n: int = 1, sar_d: int = 1) -> tuple[int, int]:
    """Coded size → square-pixel display size using sample aspect ratio."""
    n = sar_n if sar_n > 0 else 1
    d = sar_d if sar_d > 0 else 1
    if n == d:
        return int(w), int(h)
    return max(1, int(round(w * n / d))), int(h)


def would_downscale(src_w: int, src_h: int, dst_w: int, dst_h: int, stretch: bool) -> bool:
    if stretch:
        return dst_w < src_w or dst_h < src_h
    return min(dst_w / src_w, dst_h / src_h) < 1.0 - 1e-9


def fit_content(src_w: int, src_h: int, dst_w: int, dst_h: int, stretch: bool) -> tuple[int, int, int, int, float]:
    """Return (x, y, content_w, content_h, scale). Scale is min of x/y unless stretch."""
    if stretch:
        return 0, 0, even(dst_w), even(dst_h), (dst_w / src_w + dst_h / src_h) / 2.0
    scale = min(dst_w / src_w, dst_h / src_h)
    cw = even(int(round(src_w * scale)))
    ch = even(int(round(src_h * scale)))
    if cw > dst_w:
        cw = even(dst_w)
    if ch > dst_h:
        ch = even(dst_h)
    if cw < 2:
        cw = 2
    if ch < 2:
        ch = 2
    x = even((dst_w - cw) // 2)
    y = even((dst_h - ch) // 2)
    return x, y, cw, ch, scale


def clamp_output(w: int, h: int) -> tuple[int, int]:
    w = even(max(2, min(int(w), MAX_W)))
    h = even(max(2, min(int(h), MAX_H)))
    return w, h


def preset_enabled(src_w: int, src_h: int, pw: int, ph: int) -> bool:
    return not would_downscale(src_w, src_h, pw, ph, stretch=False)


def probe_pairs() -> list[tuple[int, int, int, int]]:
    seen: set[tuple[int, int, int, int]] = set()
    out: list[tuple[int, int, int, int]] = []

    def add(iw: int, ih: int, ow: int, oh: int) -> None:
        if ow < iw or oh < ih:
            return
        if ow > MAX_W or oh > MAX_H:
            return
        if iw < MIN_W or ih < MIN_H:
            return
        if iw >= VSR_SRC_REJECT_W and ih >= VSR_SRC_REJECT_H:
            return
        key = (iw, ih, ow, oh)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    for iw, ih in PROBE_INPUTS:
        for ow, oh in PROBE_OUTPUTS:
            add(iw, ih, ow, oh)
        for s in PROBE_SCALES:
            add(iw, ih, min(iw * s, MAX_W), min(ih * s, MAX_H))
    return out


def nearest_pair(
    in_w: int,
    in_h: int,
    out_w: int,
    out_h: int,
    accepted: list[Pair],
) -> Pair | None:
    ok = [p for p in accepted if p.ok]
    if not ok:
        return None

    def score(p: Pair) -> tuple:
        same_in = 0 if (p.in_w == in_w and p.in_h == in_h) else 1
        din = abs(p.in_w - in_w) + abs(p.in_h - in_h)
        dout = abs(p.out_w - out_w) + abs(p.out_h - out_h)
        # Prefer not exceeding the target (then we can scale up in fallback).
        over = 0 if (p.out_w <= out_w and p.out_h <= out_h) else 1
        return (same_in, over, din, dout)

    return min(ok, key=score)


def integer_scales(accepted: list[Pair]) -> list[int]:
    found: set[int] = set()
    for p in accepted:
        if not p.ok:
            continue
        sx = p.out_w / p.in_w
        sy = p.out_h / p.in_h
        if abs(sx - round(sx)) < 0.02 and abs(sy - round(sy)) < 0.02 and abs(sx - sy) < 0.02:
            s = int(round(sx))
            if s >= 2:
                found.add(s)
    return sorted(found)


def non_integer_ok(accepted: list[Pair]) -> bool:
    for p in accepted:
        if not p.ok:
            continue
        sx = p.out_w / p.in_w
        sy = p.out_h / p.in_h
        if abs(sx - round(sx)) > 0.05 or abs(sy - round(sy)) > 0.05:
            return True
    return False
