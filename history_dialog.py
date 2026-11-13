"""HistoryDialog: the "Usage history" window (opened from Settings) - a per-day bar chart and one table for the last
7 days, 30 days or everything kept, read from usage_history.UsageHistory.

Layout (top to bottom, 744 logical px wide, fixed height). Everything in the window shares ONE left and ONE right
margin (PAD), and the table's seven number columns are equally wide (COL_W), so the headings, the totals and every
row end on the same seven lines:
  title + range switch; tokens written per day as bars on a base line, with the first date, a caption (the hovered
  day's figure) and "Today" under it; the table: column headings, the range's totals as its first row ("Last 30
  days"), then newest day first with its models indented under it (days without energy figures, e.g. from before
  energy metering, show a dash); a hairline; one muted note (kept on this PC, what GPU energy is) beside Close.
Every vertical distance is a multiple of 4 px.

The chart and the table are drawn on plain tk.Canvas widgets (like graphs.py): one canvas holds every table row, so
even a year of history opens in a few ms and scrolls without hundreds of label widgets. tk.Canvas is not scaled by
CustomTkinter, so every size and font on them is multiplied by the widget scaling at draw time.

Data is read on the Tk thread, once per range change (days() + totals(): ~2 ms). No threads. The formatters and the
view model (`build_view`) are pure module-level functions, testable without Tk.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import time
import tkinter as tk
from dataclasses import dataclass, field
from typing import Any, Callable

import customtkinter as ctk

import theme as T
from sweep_dialog import apply_modal, center_over, later, release_modal, style_dialog

log = logging.getLogger("agent_start")

# Closing a CTkToplevel while CustomTkinter is still styling it (dark caption / icon) can crash Tk: user closes in
# the first moment are deferred until the window is armed (busy_dialog.ARM_S).
ARM_S = 0.4

DASH = "–"
RANGES: tuple[int | None, ...] = (7, 30, None)          # None = everything kept
RANGE_LABELS = {7: "7 days", 30: "30 days", None: "All"}
TOTAL_LABELS = {7: "Last 7 days", 30: "Last 30 days", None: "All days"}     # the totals row of each range
DEFAULT_RANGE = 30
ALL_MAX_BARS = 90                                        # 'All': the newest 90 days of the chart at most
COLUMNS = ("Day", "Requests", "Tokens in", "Tokens out", "Tok/s", "Served", "GPU Wh", "Tok/Wh")
EMPTY_CHART = "No requests recorded yet"
EMPTY_TABLE = "History starts with the first request after this version was installed."
FOOTER = "Kept on this PC only"
AXIS_CAPTION = "Tokens out per day"
ENERGY_NOTE = ("GPU energy is the whole graphics card while the engine was serving, not only the engine. "
               "Tok/Wh counts the energy used while a reply was running.")
NOTE = f"{FOOTER}. {ENERGY_NOTE}"                         # the one muted paragraph beside Close

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")   # fixed English, not the locale's %a / %b
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# ---- layout (logical px; every distance on a 4 px grid) -------------------------------------------------------
WIDTH = 744
PAD = 24             # the one left and right margin of everything in the window
EDGE = 20            # above the title and under the footer
HEAD_H = 32          # title / range switch row
CHART_H = 88         # bars on a base line
AXIS_H = 20
TABLE_HEAD_H = 32    # column headings
TOTAL_H = 40         # the range's totals, between two hairlines
TABLE_BODY_H = 300   # longer tables scroll
NOTE_H = 36          # NOTE: two wrapped lines of 12 px text
NOTE_W = 520
CLOSE_H = 32
GAP_CHART = 20       # title -> chart
GAP_AXIS = 8         # chart -> its dates
GAP_TABLE = 24       # chart block -> table
GAP_FOOT = 16        # table -> hairline -> footer
HEIGHT = (EDGE + HEAD_H + GAP_CHART + CHART_H + GAP_AXIS + AXIS_H + GAP_TABLE + TABLE_HEAD_H + TOTAL_H
          + TABLE_BODY_H + GAP_FOOT + 1 + GAP_FOOT + NOTE_H + EDGE)
SCROLLBAR_W = 6      # the scrollbar lives in the right margin (PAD = SCROLL_GAP + SCROLLBAR_W + 10), so the
SCROLL_GAP = 8       # numbers still end on the window's right margin
COL_W = 76           # every number column; the day / model column takes the rest (164 px)
DAY_ROW_H = 32
MODEL_ROW_H = 24
GROUP_PAD = 8        # above a day row and under its last row (half of it above the first day)
MODEL_INDENT = 12


# ================================================================================================ pure formatters
def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def fmt_count(n: Any) -> str:
    """Counts / tokens: '1,902' below 10,000, then 3 significant digits: '10.0 K', '186 K', '3.42 M', '1.37 B'."""
    v = _finite(n)
    if v is None or v <= 0:
        return "0"
    v = round(v)
    if v < 10_000:
        return f"{int(v):,}"
    units = ((1e3, "K"), (1e6, "M"), (1e9, "B"))
    i = 0
    while i + 1 < len(units) and v >= units[i + 1][0]:
        i += 1
    while True:
        x = v / units[i][0]
        text = f"{x:.0f}" if x >= 99.95 else f"{x:.1f}" if x >= 9.995 else f"{x:.2f}"
        if float(text) >= 1000 and i + 1 < len(units):      # 999,999 -> '1.00 M', not '1000 K'
            i += 1
            continue
        return f"{text} {units[i][1]}"


def fmt_tps(v: Any) -> str:
    """Average generation speed: one decimal ('98.4'); '–' when unknown."""
    f = _finite(v)
    if f is None or f <= 0:
        return DASH
    return f"{f:,.1f}"


def fmt_served(seconds: Any) -> str:
    """Serving time: '18 m', '5 h 12 m', '61 h 05 m'; '< 1 m' for a few seconds; '–' for none."""
    s = _finite(seconds)
    if s is None or s <= 0:
        return DASH
    m = int(s / 60.0 + 0.5)
    if m < 1:
        return "< 1 m"
    if m < 60:
        return f"{m} m"
    h, mm = divmod(m, 60)
    return f"{h} h {mm:02d} m"


def _sig3(x: float) -> str:
    """Three significant digits: '0.42', '12.3', '842', '1,234'."""
    return f"{x:,.0f}" if x >= 99.95 else f"{x:.1f}" if x >= 9.995 else f"{x:.2f}"


def fmt_wh(wh: Any) -> str:
    """A 'GPU Wh' table cell: '0.42', '12.3', '842', '1,234'; '< 0.01' for a trace; '–' for none."""
    v = _finite(wh)
    if v is None or v <= 0:
        return DASH
    return "< 0.01" if v < 0.005 else _sig3(v)


def fmt_tok_per_wh(v: Any) -> str:
    """A 'Tok/Wh' table cell: a whole number with thousands separators ('8,412'); '–' when unknown."""
    f = _finite(v)
    if f is None or f <= 0:
        return DASH
    return f"{round(f):,}"


def fmt_date(d: _dt.date) -> str:
    """'14 Oct' (the chart axis)."""
    return f"{d.day} {_MONTHS[d.month - 1]}"


def fmt_day(d: _dt.date, today: _dt.date) -> str:
    """A table day: 'Today', 'Yesterday', else 'Wed 11 Nov' (English, whatever the Windows locale)."""
    if d == today:
        return "Today"
    if d == today - _dt.timedelta(days=1):
        return "Yesterday"
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def usage_cells(u: Any) -> tuple[str, str, str, str, str, str, str]:
    """(requests, tokens in, tokens out, tok/s, served, GPU Wh, Tok/Wh) for one Usage. A span with 0 requests
    (served time only) shows '–' for its tokens instead of 0; a span without energy figures (e.g. a day from before
    energy metering) shows '–' for both energy cells."""
    req = int(_finite(getattr(u, "requests", 0)) or 0)
    if req <= 0:
        prompt = gen = DASH
    else:
        prompt = fmt_count(getattr(u, "prompt_tokens", 0))
        gen = fmt_count(getattr(u, "generated_tokens", 0))
    if getattr(u, "has_energy", False):
        wh, tpw = fmt_wh(getattr(u, "gpu_wh", 0)), fmt_tok_per_wh(getattr(u, "tokens_per_wh", None))
    else:
        wh = tpw = DASH
    return (f"{max(0, req):,}", prompt, gen, fmt_tps(getattr(u, "decode_tps", None)),
            fmt_served(getattr(u, "served_s", 0)), wh, tpw)


def total_label(range_days: int | None) -> str:
    """The name of the totals row: 'Last 7 days', 'Last 30 days', 'All days'."""
    return TOTAL_LABELS.get(range_days) or f"Last {range_days} days"


def range_start(today: _dt.date, range_days: int | None, first: _dt.date | None) -> _dt.date | None:
    """The first calendar day of the chart: today-6 / today-29, or for 'All' the first day with data (the newest
    ALL_MAX_BARS days at most; None when there is no data)."""
    if range_days is not None:
        return today - _dt.timedelta(days=max(1, int(range_days)) - 1)
    if first is None:
        return None
    oldest = today - _dt.timedelta(days=ALL_MAX_BARS - 1)
    return min(today, max(first, oldest))


@dataclass(frozen=True)
class TableRow:
    kind: str                       # "day" | "model"
    label: str                      # 'Today' / 'Wed 11 Nov' / model id
    cells: tuple[str, ...]          # requests, tokens in, tokens out, tok/s, served, GPU Wh, Tok/Wh


@dataclass(frozen=True)
class View:
    """Everything one range shows."""
    range_days: int | None
    total_label: str                # 'Last 30 days'
    total: tuple[str, ...]          # the range's totals, the same seven cells as a row
    bar_days: tuple[_dt.date, ...]
    bars: tuple[int, ...]
    rows: tuple[TableRow, ...]
    has_data: bool                  # anything recorded at all (else the empty state)
    table_note: str                 # the muted line shown instead of rows ('' when there are rows)
    axis: tuple[str, str, str] = field(default=("", AXIS_CAPTION, "Today"))


def build_view(history: Any, range_days: int | None, today: _dt.date) -> View:
    """Read the history once for one range: days() (all) + totals(range). A failing store reads as empty."""
    try:
        day_rows = list(history.days())
        summary = history.totals(range_days)
    except Exception:  # noqa: BLE001 - the window must open whatever the store does
        log.exception("usage history could not be read")
        day_rows, summary = [], None
    has_data = bool(day_rows)
    total = usage_cells(getattr(summary, "total", None))

    first = min((r.date for r in day_rows), default=None)
    start = range_start(today, range_days, first)
    if start is None:
        bar_days: tuple[_dt.date, ...] = ()
    else:
        bar_days = tuple(start + _dt.timedelta(days=i) for i in range((today - start).days + 1))
    per_day = {r.date: int(_finite(getattr(r.total, "generated_tokens", 0)) or 0) for r in day_rows}
    bars = tuple(max(0, per_day.get(d, 0)) for d in bar_days)

    cutoff = today - _dt.timedelta(days=range_days - 1) if range_days is not None else None
    rows: list[TableRow] = []
    for r in sorted(day_rows, key=lambda r: r.date, reverse=True):
        if cutoff is not None and r.date < cutoff:
            continue
        rows.append(TableRow("day", fmt_day(r.date, today), usage_cells(r.total)))
        for m in r.models:
            rows.append(TableRow("model", str(getattr(m, "model", "") or "(unknown)"), usage_cells(m)))
    if not has_data:
        note = EMPTY_TABLE
    elif not rows:
        note = f"No requests in the last {range_days} days" if range_days else EMPTY_TABLE
    else:
        note = ""
    left = ""
    if bar_days:
        left = "Today" if bar_days[0] == today else fmt_date(bar_days[0])
        if len(bar_days) == 1:
            left = ""                                 # one bar: 'Today' on the right says it all
    return View(range_days=range_days, total_label=total_label(range_days), total=total, bar_days=bar_days,
                bars=bars, rows=tuple(rows), has_data=has_data, table_note=note, axis=(left, AXIS_CAPTION, "Today"))


# ================================================================================================ the window
def _font(family: str, px: float, s: float, weight: str = "normal") -> tuple:
    size = -max(7, round(px * s))                     # negative = pixels, so it tracks the scaling
    return (family, size) if weight == "normal" else (family, size, weight)


class HistoryDialog(ctk.CTkToplevel):
    """The usage history window. `history`: anything with days() / totals() like usage_history.UsageHistory."""

    def __init__(self, master, history, on_closed: Callable[[], None] | None = None,
                 today: _dt.date | None = None) -> None:
        super().__init__(master, fg_color=T.BG)
        style_dialog(self)
        self._master = master
        self._history = history
        self._on_closed = on_closed
        self._today = today or _dt.date.today()
        self._armed_at = time.monotonic() + ARM_S
        self._closed = False
        self._closing = False                  # close() is destroying us (it fires on_closed itself)
        self._close_pending = False
        self._closed_fired = False
        self._destroyed = False
        self._hover: int | None = None
        self.logical_width = WIDTH
        self.logical_height = HEIGHT
        self.range_days: int | None = DEFAULT_RANGE
        self._view = build_view(history, self.range_days, self._today)

        self.title("Usage history")
        self.resizable(False, False)
        center_over(self, master, WIDTH, HEIGHT)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<MouseWheel>", self._on_wheel)
        self.grid_columnconfigure(0, weight=1)

        ui = T.ui_family()
        strong, strong_weight = T.semibold()
        small = ctk.CTkFont(family=ui, size=12)
        body = ctk.CTkFont(family=ui, size=13)

        # ---- 1. title + range switch
        head = ctk.CTkFrame(self, fg_color="transparent", height=HEAD_H)
        head.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(EDGE, 0))
        head.grid_propagate(False)
        head.grid_columnconfigure(0, weight=1)
        head.grid_rowconfigure(0, weight=1)
        self._head = head
        self._title = ctk.CTkLabel(head, text="Usage history", anchor="w", height=24,
                                   font=ctk.CTkFont(family=strong, size=16, weight=strong_weight))
        self._title.grid(row=0, column=0, sticky="w")
        seg = ctk.CTkFrame(head, fg_color=T.SURFACE, corner_radius=8)
        seg.grid(row=0, column=1, sticky="e")
        self._seg = seg
        self._range_btns: dict[int | None, ctk.CTkButton] = {}
        for i, days in enumerate(RANGES):
            b = ctk.CTkButton(seg, text=RANGE_LABELS[days], width=48 if days is None else 68, height=24,
                              corner_radius=6, font=body, border_width=0,
                              command=lambda d=days: self.set_range(d))
            b.grid(row=0, column=i, padx=(2 if i == 0 else 0, 2), pady=2)
            self._range_btns[days] = b

        # ---- 2. chart: bars on a base line, straight on the window (no panel: the same margins as the table)
        self._chart_box = ctk.CTkFrame(self, fg_color="transparent", height=CHART_H, corner_radius=0)
        self._chart_box.grid(row=1, column=0, sticky="ew", padx=PAD, pady=(GAP_CHART, 0))
        self._chart = tk.Canvas(self._chart_box, bg=T.BG, highlightthickness=0, bd=0, takefocus=0, width=1, height=1)
        self._chart.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._chart.bind("<Configure>", lambda _e: self._draw_chart())
        self._chart.bind("<Motion>", self._chart_motion)
        self._chart.bind("<Leave>", lambda _e: self._set_hover(None))

        axis = ctk.CTkFrame(self, fg_color="transparent", height=AXIS_H)
        axis.grid(row=2, column=0, sticky="ew", padx=PAD, pady=(GAP_AXIS, 0))
        axis.grid_columnconfigure((0, 2), weight=1, uniform="axis")
        axis.grid_columnconfigure(1, weight=0)
        self._axis_box = axis
        self._axis = (ctk.CTkLabel(axis, text="", anchor="w", height=AXIS_H, font=small, text_color=T.TEXT_MUTED),
                      ctk.CTkLabel(axis, text="", anchor="center", height=AXIS_H, font=small, text_color=T.TEXT_2),
                      ctk.CTkLabel(axis, text="", anchor="e", height=AXIS_H, font=small, text_color=T.TEXT_MUTED))
        self._axis[0].grid(row=0, column=0, sticky="w")
        self._axis[1].grid(row=0, column=1)
        self._axis[2].grid(row=0, column=2, sticky="e")

        # ---- 3. table: a fixed canvas (headings + the totals row) over a scrolling one (one canvas for all rows).
        # The scrollbar sits in the right margin, so both canvases are exactly as wide as the chart.
        table = ctk.CTkFrame(self, fg_color="transparent", height=TABLE_HEAD_H + TOTAL_H + TABLE_BODY_H)
        table.grid(row=3, column=0, sticky="ew", padx=(PAD, PAD - SCROLL_GAP - SCROLLBAR_W), pady=(GAP_TABLE, 0))
        table.grid_propagate(False)
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(1, weight=1)
        head_box = ctk.CTkFrame(table, fg_color="transparent", height=TABLE_HEAD_H + TOTAL_H, corner_radius=0)
        head_box.grid(row=0, column=0, sticky="ew")
        self._thead = tk.Canvas(head_box, bg=T.BG, highlightthickness=0, bd=0, takefocus=0, width=1, height=1)
        self._thead.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._thead.bind("<Configure>", lambda _e: self._draw_head())
        self._tbody = tk.Canvas(table, bg=T.BG, highlightthickness=0, bd=0, takefocus=0, width=1, height=1)
        self._tbody.grid(row=1, column=0, sticky="nsew")
        self._tbody.bind("<Configure>", lambda _e: self._draw_table())
        self._scroll = ctk.CTkScrollbar(table, width=SCROLLBAR_W, border_spacing=0, fg_color=T.BG,
                                        button_color=T.BG, button_hover_color=T.BG, command=self._tbody.yview)
        self._scroll.grid(row=1, column=1, sticky="ns", padx=(SCROLL_GAP, 0))
        self._tbody.configure(yscrollcommand=self._scroll.set)
        self._scrollable = False

        # ---- 4. footer: a hairline, then the one muted note beside Close
        # (bg_color too: a 1 px CTkFrame has no room to draw its own fill, only its canvas background shows)
        self._rule = ctk.CTkFrame(self, fg_color=T.HAIRLINE, bg_color=T.HAIRLINE, height=1, corner_radius=0)
        self._rule.grid(row=4, column=0, sticky="ew", padx=PAD, pady=(GAP_FOOT, 0))
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=5, column=0, sticky="ew", padx=PAD, pady=(GAP_FOOT, EDGE))
        foot.grid_columnconfigure(0, weight=1)
        self._note = ctk.CTkLabel(foot, text=NOTE, anchor="w", justify="left", height=NOTE_H, wraplength=NOTE_W,
                                  font=small, text_color=T.TEXT_MUTED)
        self._note.grid(row=0, column=0, sticky="w")
        self._close_btn = ctk.CTkButton(foot, text="Close", width=84, height=CLOSE_H, corner_radius=8, font=body,
                                        fg_color=T.RAISED, hover_color=T.RAISED_HOVER, text_color=T.TEXT,
                                        command=self.close)
        self._close_btn.grid(row=0, column=1, sticky="e")

        self._apply_view()
        apply_modal(self, master)

    # ------------------------------------------------------------------ public read-only helpers (tests)
    def total_row(self) -> tuple[str, ...]:
        """The totals row as shown: ('Last 30 days', requests, tokens in, tokens out, tok/s, served, GPU Wh,
        Tok/Wh)."""
        return (self._view.total_label, *self._view.total)

    def table_rows(self) -> list[tuple[str, ...]]:
        """Table rows in display order: ('day' | 'model', label, requests, tokens in, tokens out, tok/s, served,
        GPU Wh, Tok/Wh). The label is the full model id (the canvas may shorten a very long one with '…')."""
        return [(r.kind, r.label, *r.cells) for r in self._view.rows]

    def note_text(self) -> str:
        """The muted note beside Close, as shown."""
        return str(self._note.cget("text"))

    def table_note(self) -> str:
        """The muted line shown instead of rows ('' when there are rows)."""
        return self._view.table_note

    def bar_values(self) -> list[int]:
        """Generated tokens per calendar day of the chart, oldest first (0 = empty slot)."""
        return list(self._view.bars)

    def bar_days(self) -> list[_dt.date]:
        return list(self._view.bar_days)

    def chart_message(self) -> str:
        """The centred text on the chart ('' when it shows bars)."""
        return "" if self._view.has_data else EMPTY_CHART

    def axis_texts(self) -> tuple[str, str, str]:
        """(left, centre, right) of the line under the chart, as shown (the centre shows a hovered day)."""
        left, centre, right = (str(lbl.cget("text")) for lbl in self._axis)
        return left, centre, right

    def range_buttons(self) -> dict[str, bool]:
        """Range switch: label -> selected."""
        return {RANGE_LABELS[d]: d == self.range_days for d in RANGES}

    def table_view(self) -> tuple[float, float]:
        """The table body's visible fraction (top, bottom); (0.0, 1.0) when everything fits."""
        try:
            a, b = self._tbody.yview()
            return float(a), float(b)
        except tk.TclError:
            return 0.0, 1.0

    def scroll_table(self, fraction: float) -> None:
        """Scroll the table body to `fraction` (0 = top, 1 = bottom), as the scrollbar would."""
        if self._scrollable:
            self._tbody.yview_moveto(max(0.0, min(1.0, float(fraction))))

    # ------------------------------------------------------------------ range
    def set_range(self, days: int | None) -> None:
        """Show the last 7 / 30 days or everything kept (None). Reads the history once."""
        if days not in RANGES:
            raise ValueError(f"range must be one of {RANGES}, not {days!r}")
        if self._closed or days == self.range_days:
            return
        self.range_days = days
        self._view = build_view(self._history, days, self._today)
        self._apply_view()

    def _apply_view(self) -> None:
        for days, b in self._range_btns.items():
            on = days == self.range_days
            b.configure(fg_color=T.RAISED if on else T.SURFACE, hover_color=T.RAISED_HOVER if on else T.RAISED,
                        text_color=T.TEXT if on else T.TEXT_2)
        self._hover = None
        self._show_axis()
        self._draw_chart()
        self._draw_head()
        self._tbody.yview_moveto(0.0)
        self._draw_table()

    # ------------------------------------------------------------------ drawing helpers
    def _scale(self) -> float:
        try:
            return float(ctk.ScalingTracker.get_widget_scaling(self))
        except Exception:  # noqa: BLE001
            return 1.0

    def _size(self, c: tk.Canvas) -> tuple[int, int] | None:
        try:
            w, h = c.winfo_width(), c.winfo_height()
        except tk.TclError:
            return None
        return (w, h) if w >= 8 and h >= 8 else None

    def _fonts(self, s: float) -> dict[str, tuple]:
        """The table's three fonts at this scaling: headings (12 px), cells (13 px) and the semibold of the totals
        row and the day names (13 px)."""
        ui = T.ui_family()
        strong, weight = T.semibold()
        return {"head": _font(ui, 12, s), "cell": _font(ui, 13, s), "strong": _font(strong, 13, s, weight)}

    def _measure(self, font: tuple, text: str) -> int:
        try:
            return int(self.tk.call("font", "measure", font, text))   # no tkinter.font.Font object (see theme)
        except tk.TclError:
            return len(text) * abs(font[1]) // 2

    def _fit(self, font: tuple, text: str, width: int) -> str:
        if width <= 0 or self._measure(font, text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:                                  # longest prefix that fits with the ellipsis
            mid = (lo + hi + 1) // 2
            if self._measure(font, text[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

    # ------------------------------------------------------------------ chart
    def _slot_geometry(self, w: int, n: int, s: float) -> tuple[float, float]:
        slot = w / max(1, n)
        bar_w = max(1.0, min(slot * 0.6, slot - max(1.0, round(2 * s)), 32 * s))
        return slot, bar_w

    def _draw_chart(self) -> None:
        if self._destroyed:
            return
        size = self._size(self._chart)
        if size is None:
            return
        w, h = size
        s = self._scale()
        c = self._chart
        c.delete("all")
        v = self._view
        base = max(1, round(s))                          # the base line the bars stand on: the canvas' last rows
        y_base = h - (base + 1) // 2                     # (a line centred on h - 0.5 lands outside the canvas)
        c.create_line(0, y_base, w, y_base, fill=T.HAIRLINE, width=base, tags=("base",))
        if not v.has_data:
            c.create_text(w / 2, (h - base) / 2, text=EMPTY_CHART, fill=T.TEXT_MUTED, font=_font(T.ui_family(), 12, s))
            return
        n = len(v.bars)
        if n == 0:
            return
        slot, bar_w = self._slot_geometry(w, n, s)
        peak = max(v.bars) or 1
        y_bot = h - base
        room = y_bot - 4 * s                             # the tallest bar stops 4 px under the top
        dim = T.mix(T.ACCENT, T.BG, 0.4)
        for i, val in enumerate(v.bars):
            if val <= 0:                                 # an empty day is a gap on the base line
                continue
            x = i * slot + (slot - bar_w) / 2
            top = y_bot - max(3.0 * s, val / peak * room)
            color = dim if self._hover is not None and self._hover != i else T.ACCENT
            c.create_rectangle(x, top, x + bar_w, y_bot, fill=color, outline="", tags=("bar",))

    def _chart_motion(self, event) -> None:
        size = self._size(self._chart)
        n = len(self._view.bars)
        if size is None or n == 0 or not self._view.has_data:
            return
        slot, _bw = self._slot_geometry(size[0], n, self._scale())
        i = int(event.x // slot) if slot > 0 else -1
        self._set_hover(i if 0 <= i < n else None)

    def _set_hover(self, i: int | None) -> None:
        if i == self._hover or self._destroyed:
            return
        self._hover = i
        self._show_axis()
        self._draw_chart()

    def _show_axis(self) -> None:
        left, centre, right = self._view.axis
        if self._hover is not None and 0 <= self._hover < len(self._view.bars):
            d = self._view.bar_days[self._hover]
            val = self._view.bars[self._hover]
            centre = f"{fmt_day(d, self._today)}: " + (f"{fmt_count(val)} tokens out" if val else "no requests")
        for lbl, text in zip(self._axis, (left, centre, right)):
            if lbl.cget("text") != text:
                lbl.configure(text=text)

    # ------------------------------------------------------------------ table
    def _col_rights(self, w: int, s: float) -> list[float]:
        """The right edge of each number column: seven equal columns ending at the canvas' right edge."""
        n = len(COLUMNS) - 1
        return [w - (n - 1 - i) * COL_W * s for i in range(n)]

    def _draw_head(self) -> None:
        if self._destroyed:
            return
        size = self._size(self._thead)
        if size is None:
            return
        w, h = size
        s = self._scale()
        c = self._thead
        c.delete("all")
        f = self._fonts(s)
        v = self._view
        rights = self._col_rights(w, s)
        top = round(TABLE_HEAD_H * s)                    # headings above this line, the totals row under it
        y = top / 2
        c.create_text(0, y, text=COLUMNS[0], anchor="w", fill=T.TEXT_2, font=f["head"])
        for x, name in zip(rights, COLUMNS[1:]):
            c.create_text(x, y, text=name, anchor="e", fill=T.TEXT_2, font=f["head"])
        cy = top + (h - top) / 2
        c.create_text(0, cy, text=v.total_label, anchor="w", fill=T.TEXT, font=f["strong"], tags=("total",))
        for x, cell in zip(rights, v.total):
            c.create_text(x, cy, text=cell, anchor="e", fill=T.TEXT_MUTED if cell == DASH else T.TEXT,
                          font=f["strong"], tags=("total",))
        c.create_line(0, top, w, top, fill=T.HAIRLINE)
        c.create_line(0, h - 1, w, h - 1, fill=T.HAIRLINE)

    def _draw_table(self) -> None:
        if self._destroyed:
            return
        size = self._size(self._tbody)
        if size is None:
            return
        w, h = size
        s = self._scale()
        c = self._tbody
        c.delete("all")
        v = self._view
        f = self._fonts(s)
        if not v.rows:
            c.create_text(0, (GROUP_PAD / 2 + DAY_ROW_H / 2) * s, text=v.table_note, anchor="w", fill=T.TEXT_MUTED,
                          font=f["cell"])
            self._set_scrollable(False, w, h)
            return
        rights = self._col_rights(w, s)
        name_w = int(rights[0] - COL_W * s - MODEL_INDENT * s)             # the first column, less the indent
        soft = T.mix(T.HAIRLINE, T.BG, 0.6)              # between days: quieter than the lines around the totals
        y = 0.0
        first = True
        for row in v.rows:
            quiet = row.cells[0] == "0"                  # served, but no requests: the whole row steps back
            if row.kind == "day":
                if first:
                    y += GROUP_PAD / 2 * s
                    first = False
                else:
                    y += GROUP_PAD * s
                    c.create_line(0, round(y), w, round(y), fill=soft)
                    y += GROUP_PAD * s
                rh = DAY_ROW_H * s
                cy = y + rh / 2
                c.create_text(0, cy, text=row.label, anchor="w", fill=T.TEXT_2 if quiet else T.TEXT,
                              font=f["cell"] if quiet else f["strong"])
                for x, cell in zip(rights, row.cells):
                    c.create_text(x, cy, text=cell, anchor="e",
                                  fill=T.TEXT_MUTED if quiet or cell == DASH else T.TEXT, font=f["cell"])
            else:
                rh = MODEL_ROW_H * s
                cy = y + rh / 2
                c.create_text(MODEL_INDENT * s, cy, text=self._fit(f["cell"], row.label, name_w), anchor="w",
                              fill=T.TEXT_2, font=f["cell"])
                for x, cell in zip(rights, row.cells):
                    c.create_text(x, cy, text=cell, anchor="e",
                                  fill=T.TEXT_MUTED if quiet or cell == DASH else T.TEXT_2, font=f["cell"])
            y += rh
        total_h = math.ceil(y + GROUP_PAD * s)
        self._set_scrollable(total_h > h, w, max(total_h, h))

    def _set_scrollable(self, on: bool, w: int, h: int) -> None:
        self._tbody.configure(scrollregion=(0, 0, w, h), yscrollincrement=max(1, round(MODEL_ROW_H * self._scale())))
        if on != self._scrollable:
            self._scrollable = on
            color = T.RAISED_HOVER if on else T.BG
            self._scroll.configure(button_color=color, button_hover_color=T.TEXT_3 if on else T.BG)
        if not on:
            self._tbody.yview_moveto(0.0)

    def _on_wheel(self, event) -> None:
        if self._scrollable and not self._destroyed:
            steps = -int(event.delta / 120) if event.delta else 0
            if steps:
                self._tbody.yview_scroll(steps * 2, "units")

    # ------------------------------------------------------------------ closing
    def close(self) -> None:
        """Close the window (Close / X / Escape). In the first ARM_S seconds the close waits until the window is
        armed instead of being dropped. on_closed fires once, one event-loop turn later."""
        if self._closed:
            return
        wait = self._armed_at - time.monotonic()
        if wait > 0:
            if not self._close_pending:
                self._close_pending = True
                later(self, int(wait * 1000) + 20, self.close)
            return
        self._closed = True
        self._closing = True
        release_modal(self)
        try:
            self.destroy()
        except Exception:  # noqa: BLE001
            log.exception("HistoryDialog destroy failed")
        self._fire_closed(deferred=True)

    def destroy(self) -> None:
        """Also runs when the master is destroyed under us: cancel every pending after-script first."""
        if not self._destroyed:
            self._destroyed = True
            self._closed = True
            release_modal(self)
        super().destroy()
        if not self._closing:                    # destroyed from outside (master closing): tell the owner now
            self._fire_closed(deferred=False)

    def _fire_closed(self, deferred: bool) -> None:
        if self._closed_fired or self._on_closed is None:
            self._closed_fired = True
            return
        self._closed_fired = True
        cb = self._on_closed

        def fire() -> None:
            try:
                cb()
            except Exception:  # noqa: BLE001
                log.exception("HistoryDialog on_closed callback failed")

        if deferred:
            # Not synchronously: close() may run inside the Close button's own command while Tk unwinds the
            # destroyed window (busy_dialog.py: a re-layout from there crashed Tk). One loop turn later is safe.
            try:
                if self._master.winfo_exists():
                    self._master.after(1, fire)
                    return
            except Exception:  # noqa: BLE001 - master gone too: call it now
                pass
        fire()
