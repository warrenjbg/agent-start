"""Stats graphs drawn on plain tk.Canvas (no matplotlib) in the theme's palette and fonts.

`StatRow` = header (title · tag · current value) + one graph canvas. Two graph kinds:
  LineGraph  rolling time series against a fixed maximum (VRAM used vs total, last 5 minutes)
  BarGraph   one bar per finished request (last 30) plus an optional live bar in the accent colour; scaled to a
             robust peak (`robust_peak`), so one spike cannot squash the other bars - an off-scale bar is drawn at
             full height with a light cap

tk.Canvas is not scaled by CustomTkinter, so every pixel size and font here is multiplied by the widget scaling at
draw time; the canvas fills a CTkFrame wrapper that CTk does scale, so 100 % and 150 % DPI lay out identically.
"""
from __future__ import annotations

import math
import tkinter as tk

import customtkinter as ctk

import theme as T

ROW_H = 82            # logical px: 10 pad + 16 header + 6 gap + 40 canvas + 10 pad
CANVAS_H = 40
BAR_SLOTS = 30
GUTTER = 34           # left column for the max-scale label, so data is never drawn under it

_NICE = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)


def nice_ceil(v: float) -> float:
    """Smallest 'round' scale maximum >= v (1 / 1.2 / 1.5 / 2 / 2.5 / 3 / 4 / 5 / 6 / 8 x 10^n)."""
    if not v or v <= 0 or not math.isfinite(v):
        return 1.0
    exp = math.floor(math.log10(v))
    base = 10.0 ** exp
    for m in _NICE:
        if m * base >= v * 0.999999:
            return m * base
    return 10.0 * base


OUTLIER_FACTOR = 2.0   # a value above this x the median is an outlier candidate ...
OUTLIER_SHARE = 10     # ... and at most 1 per this many values (min 1) may be excluded from the scale


def robust_peak(values) -> float:
    """The scale peak of a bar series, ignoring a few extreme outliers.

    Values above OUTLIER_FACTOR x the median are left out of the peak when they are at most max(1, n // OUTLIER_SHARE)
    of n >= 4 values (a spike, e.g. a tiny answer's inflated rate); more of them are a real spread and count. Fewer
    than 4 values: plain max. Non-positive / non-finite values are ignored; nothing left -> 0.
    """
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v) and v > 0)
    if not vals:
        return 0.0
    n = len(vals)
    if n < 4:
        return vals[-1]
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    inliers = [v for v in vals if v <= OUTLIER_FACTOR * med]
    if inliers and n - len(inliers) <= max(1, n // OUTLIER_SHARE):
        return inliers[-1]
    return vals[-1]


def fmt_scale(v: float) -> str:
    """Compact axis label: 120, 1.5k, 4k."""
    if v >= 1000:
        k = v / 1000.0
        return f"{k:.0f}k" if abs(k - round(k)) < 0.05 else f"{k:.1f}k"
    if v >= 10 or abs(v - round(v)) < 0.05:
        return f"{v:.0f}"
    return f"{v:.1f}"


def fmt_rate(v: float) -> str:
    """Current-value text for a tok/s figure: one decimal below 1000, thousands separator above."""
    return f"{v:,.0f}" if v >= 1000 else f"{v:.1f}"


class _Graph(tk.Canvas):
    """Canvas that redraws itself on resize; subclasses implement _paint(w, h, s)."""

    def __init__(self, master) -> None:
        super().__init__(master, bg=T.SURFACE, highlightthickness=0, bd=0, takefocus=0)
        self._empty: str | None = "…"
        self._max_label = ""
        self.bind("<Configure>", lambda _e: self.redraw())

    def scale(self) -> float:
        try:
            return float(ctk.ScalingTracker.get_widget_scaling(self))
        except Exception:  # noqa: BLE001 - canvas not attached to a CTk window (tests)
            return 1.0

    def _font(self, family: str, px: float, s: float) -> tuple:
        return (family, -max(8, round(px * s)))          # negative size = pixels, so it tracks the scaling

    def redraw(self) -> None:
        try:
            w, h = self.winfo_width(), self.winfo_height()
        except tk.TclError:
            return
        if w < 8 or h < 8:
            return
        s = self.scale()
        self.delete("all")
        if self._empty:
            self.create_text(w / 2, h / 2 - 1, text=self._empty, fill=T.TEXT_3,
                             font=self._font(T.ui_family(), 11, s))
            return
        self._paint(w, h, s)

    def _paint(self, w: int, h: int, s: float) -> None:
        raise NotImplementedError

    def _frame(self, w: int, h: int, s: float) -> tuple[int, int, int]:
        """Dashed line at the scale maximum, solid baseline, and the light max-scale label in the left gutter
        (level with the dashed line it names). Returns (x_left, y_top, y_bottom) of the plot area."""
        font = self._font(T.mono_family(), 9, s)
        x0 = round(GUTTER * s)
        y_top, y_bot = round(abs(font[1]) / 2) + 1, h - 1
        self.create_line(x0, y_top, w, y_top, fill=T.HAIRLINE, dash=(2, 3))
        self.create_line(x0, y_bot, w, y_bot, fill=T.HAIRLINE)
        if self._max_label:
            self.create_text(0, y_top, anchor="w", text=self._max_label, fill=T.TEXT_3, font=font)
        return x0, y_top, y_bot


class LineGraph(_Graph):
    """Time series over a rolling window; `spans` (the time Strata was running) are drawn in the accent."""

    def __init__(self, master, window_s: float = 300.0) -> None:
        super().__init__(master)
        self.window_s = float(window_s)
        self._samples: tuple[tuple[float, float], ...] = ()
        self._max = 1.0
        self._now = 0.0
        self._spans: tuple[tuple[float, float], ...] = ()

    def set_data(self, samples, maximum: float, now: float, spans=(), max_label: str = "",
                 empty: str | None = None) -> None:
        self._samples = tuple(samples)
        self._max = float(maximum) if maximum and maximum > 0 else 1.0
        self._now = float(now)
        self._spans = tuple((float(a), float(b)) for a, b in spans)
        self._max_label = max_label
        self._empty = empty
        self.redraw()

    def _xy(self, t: float, v: float, x0: int, w: int, y_top: int, y_bot: int) -> tuple[float, float]:
        x = w - 1 - (self._now - t) / self.window_s * (w - 1 - x0)
        y = y_bot - min(1.0, max(0.0, v / self._max)) * (y_bot - y_top)
        return x, y

    @staticmethod
    def _clip(samples, t0: float, t1: float) -> list[tuple[float, float]]:
        """The part of the polyline inside [t0, t1], with interpolated end points."""
        out: list[tuple[float, float]] = []
        for i, (t, v) in enumerate(samples):
            if t < t0 or t > t1:
                continue
            if not out and i > 0 and samples[i - 1][0] < t0 < t:
                pt, pv = samples[i - 1]
                out.append((t0, pv + (v - pv) * (t0 - pt) / (t - pt)))
            out.append((t, v))
            if i + 1 < len(samples) and samples[i + 1][0] > t1 > t:
                nt, nv = samples[i + 1]
                out.append((t1, v + (nv - v) * (t1 - t) / (nt - t)))
        return out

    def _series(self, pts, x0, w, y_top, y_bot, line: str, fill: str, width: float) -> None:
        if len(pts) < 2:
            return
        xy = [self._xy(t, v, x0, w, y_top, y_bot) for t, v in pts]
        poly = [c for p in xy for c in p] + [xy[-1][0], y_bot, xy[0][0], y_bot]
        self.create_polygon(*poly, fill=fill, outline="")
        self.create_line(*[c for p in xy for c in p], fill=line, width=width, joinstyle="round", capstyle="round")

    def _paint(self, w: int, h: int, s: float) -> None:
        x0, y_top, y_bot = self._frame(w, h, s)
        t_min = self._now - self.window_s
        samples = [p for p in self._samples if p[0] >= t_min]
        width = max(1.0, 1.5 * s)
        self._series(samples, x0, w, y_top, y_bot, T.TEXT_3, T.mix(T.TEXT_3, T.SURFACE, 0.16), width)
        in_span = False
        for a, b in self._spans:
            self._series(self._clip(samples, max(a, t_min), b), x0, w, y_top, y_bot, T.ACCENT,
                         T.mix(T.ACCENT, T.SURFACE, 0.20), width)
            in_span = in_span or bool(samples and a <= samples[-1][0] <= b)
        self.create_line(x0, y_bot, w, y_bot, fill=T.HAIRLINE)
        if samples:
            x, y = self._xy(*samples[-1], x0, w, y_top, y_bot)
            r = max(2.0, 2.5 * s)
            x, y = min(x, w - r - 1), min(max(y, r), h - r - 1)
            self.create_oval(x - r, y - r, x + r, y + r, fill=T.ACCENT if in_span else T.TEXT_2, outline=T.SURFACE)


class BarGraph(_Graph):
    """One bar per finished request, newest on the right; `live` adds an accent bar for the running request."""

    def __init__(self, master, slots: int = BAR_SLOTS) -> None:
        super().__init__(master)
        self.slots = max(2, int(slots))
        self._values: list[float] = []
        self._live: float | None = None
        self.scale_max = 1.0
        self.clipped = 0                                    # bars above scale_max (drawn full height + cap)

    def set_data(self, values, live: float | None = None, empty: str | None = None) -> None:
        self._values = [float(v) for v in values][-self.slots:]
        self._live = float(live) if live is not None and live > 0 else None
        shown = self._values[-(self.slots - 1):] + [self._live] if self._live is not None else self._values
        peak = robust_peak(shown)
        self.scale_max = nice_ceil(peak)
        self.clipped = sum(1 for v in shown if v > self.scale_max)
        self._max_label = fmt_scale(self.scale_max) if peak > 0 else ""
        self._empty = empty if peak <= 0 else None
        self.redraw()

    def _paint(self, w: int, h: int, s: float) -> None:
        x0, y_top, y_bot = self._frame(w, h, s)
        slot = (w - x0) / self.slots
        bar_w = max(2.0, min(slot - max(1.0, round(2 * s)), slot * 0.72))
        bars: list[tuple[float, str]] = [(v, T.APPS_SEG) for v in self._values]
        if bars:
            bars[-1] = (bars[-1][0], T.TEXT_2)               # the latest finished request reads a step brighter
        if self._live is not None:
            bars = bars[-(self.slots - 1):] + [(self._live, T.ACCENT)]
        first = self.slots - len(bars)
        for i in range(first):                                # empty slots: quiet ticks, so the row reads as a scale
            x = x0 + i * slot + (slot - bar_w) / 2
            self.create_line(x, y_bot - round(2 * s), x + bar_w, y_bot - round(2 * s), fill=T.HAIRLINE)
        cap = max(2.0, round(2 * s))
        for i, (v, color) in enumerate(bars):
            x = x0 + (first + i) * slot + (slot - bar_w) / 2
            top = y_bot - max(2.0, min(1.0, v / self.scale_max) * (y_bot - y_top))
            self.create_rectangle(x, top, x + bar_w, y_bot, fill=color, outline="", tags=("bar",))
            if v > self.scale_max:                            # off-scale: a light cap says "taller than shown"
                self.create_rectangle(x, top, x + bar_w, top + cap, fill=T.mix(T.TEXT, color, 0.6), outline="",
                                      tags=("cap",))


class StatRow(ctk.CTkFrame):
    """Fixed-height row: `title` left, muted `tag` + mono value right, graph underneath.

    No horizontal padding of its own: the owner grids it with padx=12, which also keeps this (square) frame off
    the rounded corners of the card it sits in.
    """

    def __init__(self, master, title: str, graph_factory) -> None:
        super().__init__(master, fg_color="transparent", height=ROW_H, corner_radius=0)
        self.grid_propagate(False)
        self.grid_columnconfigure(0, weight=1)
        ui = ctk.CTkFont(family=T.ui_family(), size=12)
        self.title = ctk.CTkLabel(self, text=title, font=ui, text_color=T.TEXT_2, anchor="w", height=16)
        self.title.grid(row=0, column=0, sticky="w", pady=(10, 6))
        self.tag = ctk.CTkLabel(self, text="", font=ctk.CTkFont(family=T.ui_family(), size=11),
                                text_color=T.TEXT_3, anchor="e", height=16)
        self.tag.grid(row=0, column=1, sticky="e", padx=(0, 6), pady=(10, 6))
        self.value = ctk.CTkLabel(self, text="–", font=ctk.CTkFont(family=T.mono_family(), size=11),
                                  text_color=T.TEXT_3, anchor="e", height=16)
        self.value.grid(row=0, column=2, sticky="e", pady=(10, 6))
        self._wrap = ctk.CTkFrame(self, fg_color=T.SURFACE, height=CANVAS_H, corner_radius=0)
        self._wrap.grid(row=1, column=0, columnspan=4, sticky="ew")   # column 3: the owner's pin button
        self._wrap.grid_propagate(False)
        self.graph = graph_factory(self._wrap)
        self.graph.place(relx=0, rely=0, relwidth=1, relheight=1)

    def head_frame(self):
        """Where the owner adds header controls (row 0, column 3 is free)."""
        return self

    def set_value(self, text: str, color: str = T.TEXT, tag: str = "") -> None:
        if self.value.cget("text") != text or self.value.cget("text_color") != color:
            self.value.configure(text=text, text_color=color)
        if self.tag.cget("text") != tag:
            self.tag.configure(text=tag)
