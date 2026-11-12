"""Hermes Controller — CustomTkinter widget that frees RAM and starts/stops the local Strata endpoint."""
from __future__ import annotations

import ctypes
import gc
import logging
import os
import re
import queue
import sys
import threading
import time
import tkinter
from logging.handlers import RotatingFileHandler
from tkinter import messagebox
from typing import Any, Callable

import customtkinter as ctk
import psutil

import config_store
import engine_log
import launcher_bat
import load_progress
import ram_plan
import reopen
import metrics_store
import theme as T
from graphs import BarGraph, LineGraph, StatRow, fmt_rate
from metrics_store import MetricsStore
from settings_dialog import SettingsDialog
from sweep_dialog import SweepDialog
from version import __version__
from vram_sampler import VramSampler
from whitelist_modal import WhitelistModal

log = logging.getLogger("hermes_controller")

APP_TITLE = "Hermes Controller"
WIDTH, HEIGHT = 360, 213                 # compact: status line, button, memory card (detail line hidden)
DETAIL_H = 18                            # added while the detail line shows (16 px line + spacing)
PROGRESS_H = 14                          # added while the load progress bar shows
STATS_H = 3 * 82 + 2                     # three StatRows + two hairlines (logical px)
STATS_GAP = 8                            # between the memory card and the stats card when expanded
HEIGHT_EXPANDED = HEIGHT + STATS_GAP + STATS_H

IDLE, STARTING, ACTIVE, STOPPING, ERROR = "IDLE", "STARTING", "ACTIVE", "STOPPING", "ERROR"

# Status line: (title, LED colour) per state. Sentence case, no shouting pill.
STATUS = {
    IDLE: ("Idle", T.LED["IDLE"]),
    STARTING: ("Starting", T.LED["STARTING"]),
    ACTIVE: ("Serving", T.LED["ACTIVE"]),
    STOPPING: ("Stopping", T.LED["STOPPING"]),
    ERROR: ("Error", T.LED["ERROR"]),
}
LOADING_STATUS = ("Loading", T.LED["LOADING"])
LOW_RAM_STATUS = ("Serving", T.AMBER)   # Strata fell back to reading experts from the SSD (amber LED + summary)
LOW_RAM_NOTE = "Not enough free RAM for full speed: will load slower"
TOGGLE_DEBOUNCE_S = 0.4
# Compact header (v1.1): status line + optional one-line detail. Spacing is measured from the rendered text so the
# detail line sits 10 px from the status text above and 10 px from the button / progress bar below.
DETAIL_INDENT = 32                       # window margin 16 + LED 8 + gap 8: the detail aligns with the state text
DETAIL_WRAP = 360 - DETAIL_INDENT - 16
GAP_HEAD_BUTTON = 12                     # status line -> button when no detail is shown
GAP_DETAIL_TOP = 4                       # status row (28 px, title centred) -> detail line
GAP_AFTER_DETAIL = 10                    # detail line -> button / progress bar
CARD_TOP = 14                            # button -> memory card
STATE_SAVE_DEBOUNCE_MS = 300
REOPEN_STAGGER_S = 1.0                   # pause between two reopened apps
DETAIL_POLL_S = 10.0                     # /metrics poll for the serving line's "last reply" when stats are closed
# Kept for tests/back-compat: the LED colour per state.
PILL_COLORS = {k: v[1] for k, v in STATUS.items()}
LOADING_COLOR = LOADING_STATUS[1]

# Toggle styles: (fg, hover, text colour). Primary = off-white; everything else is a quiet raised surface.
TOGGLE_ON = (T.TEXT, T.PRIMARY_HOVER, T.ON_PRIMARY)
TOGGLE_OFF = (T.RAISED, T.RAISED_HOVER, T.TEXT)
TOGGLE_CANCEL = (T.RAISED, T.RAISED_HOVER, T.TEXT)
TOGGLE_WAIT = (T.SURFACE, T.SURFACE, T.TEXT_3)

BG = T.BG
MUTED = T.TEXT_2
TEXT = T.TEXT

PUMP_MS = 50
TICK_MS = 5000
PROGRESS_MS = 500
MEMORY_S = 2.0
STATS_MS = 2000                          # stats redraw + /metrics poll period
METRICS_FAILS_SHOWN = 3                  # consecutive failed polls before "Metrics unavailable"
NOTE_HOLD_S = 4.0                        # a one-off result line stays this long before the sweep preview returns
ERROR_HOLD_S = 15.0

_SINGLETON_MUTEX = None  # kept referenced for process life (see main())


def _gb(mb: float | None, digits: int = 1) -> str:
    return f"{(mb or 0.0) / 1024.0:.{digits}f} GB"


def _short(name: str, limit: int = 22) -> str:
    """Keep the preview to two lines: long exe names (EOSOverlayRenderer-Win64-Shipping.exe) get an ellipsis."""
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name if len(name) <= limit else name[: limit - 1] + "…"


def format_preview(res: dict) -> str:
    """Two-line IDLE preview from a sweep_processes(dry_run=True) result (what ON would offer to close)."""
    n = int(res.get("candidates") or 0)
    m = int(res.get("preselected") or 0)
    if n == 0:
        return "Nothing to close"
    # line 1 sits on the status line (short); line 2 is the hidden detail (which apps)
    if m > 0:
        line1 = f"{m} app{'s' if m != 1 else ''} to close · {_gb(res.get('preselected_ram_mb'))}"
    else:
        line1 = f"{n} app{'s' if n != 1 else ''} could close · {_gb(res.get('ram_freed_mb'))}"
    # name the apps the RAM figure refers to: the preselected set if any, else everything
    pool = res.get("preselected_names") if m > 0 and res.get("preselected_names") else res.get("names")
    names = [_short(str(x)) for x in (pool or [])]
    shown: list[str] = []
    for nm in names:                            # fit one line (~46 chars at 12 px in 328 px)
        if shown and len(", ".join(shown + [nm])) + 8 > 46:
            break
        shown.append(nm)
    line2 = ", ".join(shown)
    if len(names) > len(shown):
        line2 += f" +{len(names) - len(shown)} more"
    return f"{line1}\n{line2}" if line2 else line1


def _save_reopen_records(records: list[dict]) -> None:
    """Worker: add this sweep's apps to the pending list (dedupe by program, newest wins)."""
    try:
        merged: dict[str, Any] = {}
        for r in list(reopen.load_pending()) + [reopen.LaunchRecord.from_dict(d) for d in records]:
            key = os.path.normcase(r.exe or r.aumid or r.name)
            merged[key] = r
        reopen.save_pending(list(merged.values()))
        log.info("reopen: %d app(s) will be reopened after Turn off", len(merged))
    except Exception:  # noqa: BLE001
        log.exception("saving reopen records failed")


def _reopen_clicked(recs: list, post: Callable, done: Callable) -> None:
    """Worker for the one-click offer: launch exactly these (games included - the user asked), then clear."""
    results = []
    for r in recs:
        if results:
            time.sleep(REOPEN_STAGGER_S)
        results.append(reopen.reopen_one(r))
    try:
        reopen.clear_pending()
    except Exception:  # noqa: BLE001
        log.debug("clear_pending failed", exc_info=True)
    post(done, results, [])


def _build_real_deps(cfg: dict) -> tuple[Any, Any]:
    """Real EndpointManager + ProcessDetector over ONE shared config dict (imported lazily)."""
    from server_manager import EndpointManager
    from detector import ProcessDetector

    endpoint = EndpointManager(cfg)
    detector = ProcessDetector(cfg, protected_pids_provider=endpoint.protected_pids)
    return detector, endpoint


class HermesControllerApp(ctk.CTk):
    def __init__(self, detector=None, endpoint=None, config: dict | None = None,
                 metrics_fetcher: Callable[[str], dict | None] | None = None, vram_sampler=None) -> None:
        """metrics_fetcher(root_url) and vram_sampler are test seams (defaults: Strata /metrics over HTTP, NVML)."""
        # Tk + threads: closed dialogs leave Tk objects (CTkFont, images) in reference cycles. If the automatic
        # collector fires on a worker thread (any allocation can trigger it), their __del__ makes a Tk call off the
        # main thread, which can block that worker forever - observed: the log reader stalled mid re.compile, and a
        # state.json save stalled while holding config_store's lock. So cyclic GC runs ONLY on the Tk thread: off
        # here, gc.collect() in _tick every TICK_MS. Plain refcount cleanup is unaffected; workers hold no Tk objects.
        gc.disable()
        cfg = config if config is not None else config_store.load_config()
        if detector is None and endpoint is None:
            detector, endpoint = _build_real_deps(cfg)
        elif detector is None or endpoint is None:
            raise ValueError("pass both detector and endpoint, or neither")
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=BG)
        self.cfg = cfg
        self.detector = detector
        self.endpoint = endpoint

        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._destroyed = False
        self._pump_id: str | None = None
        self._tick_id: str | None = None
        self._first_id: str | None = None
        self._stats_id: str | None = None
        self._note_id: str | None = None
        self._note_until = 0.0
        self._stop_evt = threading.Event()

        self.state_name = IDLE
        self.loaded: bool | None = None
        self.model_id: str | None = None
        self.error_text: str | None = None
        self._error_hold_until = 0.0
        self.freed_mb = 0.0
        self._preview_busy = False
        self._start_token = 0
        self._start_phase: str | None = None   # "scan" | "dialog" | "sweep" | "server"
        self._exit_pending = False
        self.dialog = None                      # open SweepDialog / WhitelistModal
        self._progress_id: str | None = None
        self._server_t0 = 0.0
        self._log_text = ""
        saved = config_store.load_state()
        try:
            self._expected_s = float(saved.get("last_cold_load_s") or 0.0)
        except (TypeError, ValueError):           # a hand-edited state.json must not stop the app
            self._expected_s = 0.0
        self.expanded = bool(saved.get("stats_expanded"))
        self._pending_launcher: launcher_bat.LauncherInfo | None = None   # saved while Strata runs
        self._fetch_metrics = metrics_fetcher or metrics_store.fetch_metrics
        self.metrics = MetricsStore()
        self._metrics_fails = 0
        self._metrics_epoch = 0                  # bumped per start: a late poll of the old server is dropped
        self.vram = vram_sampler if vram_sampler is not None else VramSampler()
        self._spans: list[list[float | None]] = []   # [start, end|None] (monotonic) while Strata was running
        self._avail_at_launch: float | None = None
        self._launch_wall = 0.0                  # time.time() at launch: rejects a previous run's log
        # Engine-log verdict for the current run (engine_log.EngineLoad): full-RAM vs slow Low-RAM (SSD) mode.
        # Written by worker threads as one atomic attribute swap, read on the Tk thread.
        self._engine_load: engine_log.EngineLoad | None = None
        self._toggle_at = 0.0                    # monotonic time of the last accepted ON/OFF click
        self._state_save_id = None               # pending debounced state.json save
        self._state_pending: dict = {}           # keys waiting for that save
        self.detail_open = bool(saved.get("detail_open"))   # user revealed the optional detail line
        # Reopen swept apps after Turn off. Preference from the start dialog's checkbox (default on).
        self._reopen_pref = bool(saved.get("reopen_after", True))
        self._reopen_this = False                # this start's choice (read from the sweep dialog)
        self._reopen_offer: list = []            # records offered as a one-click "Reopen …" line while idle
        self._reopening = False
        self._summary_text = ""
        self._detail_text = ""
        self._detail_color = T.TEXT_2
        self._detail_link = False                # detail line is a clickable action (open log, reopen apps)
        self._ready_mono: float | None = None    # when Strata became ready (uptime on the detail line)
        self._layout_key: tuple | None = None
        self._fit_h = 0
        self._engine_checked = False
        self._strata_est: float | None = None    # latched at ready: RAM Strata took (free-RAM drop)
        self._prog_best: tuple[str, float, str | None] = ("Launching", 0.0, None)
        self._mem: tuple | None = None
        self._iconic = False
        self._start_note = ""
        self._sweep_note = ""
        self._error_log: str | None = None

        self._idle_streak = 0                   # consecutive "idle" status readings while ACTIVE
        self._state_seq = 0                     # bumped on every state change: stale monitor readings are dropped
        self.report_callback_exception = (
            lambda exc, val, tb: log.error("Tk callback failed", exc_info=(exc, val, tb)))
        self.title(APP_TITLE)
        icon = config_store.APP_DIR / "assets" / "app.ico"
        if icon.exists():
            try:
                self.iconbitmap(default=str(icon))   # default= also applies to the dialogs
            except Exception:  # noqa: BLE001
                log.debug("iconbitmap failed", exc_info=True)
        self.geometry(f"{WIDTH}x{HEIGHT_EXPANDED if self.expanded else HEIGHT}")
        self.after(50, self._dark_caption)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.on_close_request)
        self._build_ui()
        self._set_status("Checking what is running…")
        self._apply_state()
        self._update_telemetry()
        self._apply_expanded()

        self._pump_id = self.after(PUMP_MS, self._pump)
        self._tick_id = self.after(TICK_MS, self._tick)
        self._first_id = self.after(300, self._first_refresh)
        threading.Thread(target=self._monitor_loop, name="hc-monitor", daemon=True).start()
        threading.Thread(target=self._memory_loop, name="hc-memory", daemon=True).start()
        threading.Thread(target=self._metrics_loop, name="hc-metrics", daemon=True).start()
        self.vram.start()

    # ================================================================== layout
    def _font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=T.ui_family(), size=size, weight=weight)

    def _mono(self, size: int) -> ctk.CTkFont:
        return ctk.CTkFont(family=T.mono_family(), size=size)

    def _build_ui(self) -> None:
        """Front-panel layout on a 4 px grid: status line, detail, progress, power button, memory card."""
        self.configure(fg_color=T.BG)
        self.grid_columnconfigure(0, weight=1)

        # Status line: LED · state · short summary · caret … timer · gear. The status part (not the gear) is one
        # click target that reveals the optional detail line; it is drawn on a frame that tints on hover.
        top = ctk.CTkFrame(self, fg_color="transparent", height=28, corner_radius=8)
        top.grid(row=0, column=0, sticky="ew", padx=(10, 12), pady=(14, 0))
        top.grid_columnconfigure(2, weight=1)
        self.head = top
        self.led = ctk.CTkFrame(top, width=8, height=8, corner_radius=4, fg_color=T.LED["IDLE"])
        self.led.grid(row=0, column=0, padx=(6, 8))
        self.pill = ctk.CTkLabel(top, text="Idle", font=self._font(15, "bold"), text_color=T.TEXT,
                                 anchor="w", height=20)
        self.pill.grid(row=0, column=1, sticky="w")
        self._summary_font = self._font(12)
        self.summary = ctk.CTkLabel(top, text="", font=self._summary_font, text_color=T.TEXT_2, anchor="w",
                                    height=20)
        self.summary.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.caret = ctk.CTkLabel(top, text="", font=ctk.CTkFont(family=T.icon_family(), size=8),
                                  text_color=T.TEXT_3, width=12, height=20)
        self.caret.grid(row=0, column=3, padx=(4, 0))
        self.elapsed = ctk.CTkLabel(top, text="", font=self._mono(12), text_color=T.TEXT_2, height=20)
        self.elapsed.grid(row=0, column=4, padx=(6, 4))
        self.gear = ctk.CTkButton(top, text=T.GEAR_GLYPH, width=28, height=28, corner_radius=8,
                                  font=ctk.CTkFont(family=T.icon_family(), size=14),
                                  fg_color="transparent", hover_color=T.RAISED, text_color=T.TEXT_2,
                                  text_color_disabled=T.HAIRLINE, command=self.open_settings)
        self.gear.grid(row=0, column=5)
        for w in (top, self.led, self.pill, self.summary, self.caret, self.elapsed):
            w.bind("<Button-1>", self._on_head_click)
            w.bind("<Enter>", lambda _e: self._head_hover(True))
            w.bind("<Leave>", lambda _e: self._head_hover(False))

        # The detail line: hidden unless the user opened it or the state needs it (loading, warnings, errors,
        # notes). Indented to the state text (LED 8 + gap 8). Wraps to a second line only for long errors.
        self._detail_font = self._font(12)
        self._detail_link_font = ctk.CTkFont(family=T.ui_family(), size=12, underline=True)
        self.preview = ctk.CTkLabel(self, text="", font=self._detail_font, text_color=T.TEXT_2, anchor="w",
                                    justify="left", wraplength=DETAIL_WRAP, height=16)
        self.preview.bind("<Button-1>", self._on_preview_click)

        # Progress: gridded only while Strata loads.
        self.progress = ctk.CTkProgressBar(self, height=4, corner_radius=2, fg_color=T.SURFACE,
                                           progress_color=T.ACCENT, border_width=0)
        self.progress.set(0)

        self.toggle = ctk.CTkButton(self, text="Turn on", width=180, height=56, corner_radius=T.R_CARD,
                                    font=self._font(16, "bold"), command=self.on_toggle,
                                    text_color_disabled=T.TEXT_3)
        self.toggle.grid(row=3, column=0, pady=(GAP_HEAD_BUTTON, 0))

        card = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=T.R_CARD)
        card.grid(row=4, column=0, sticky="ew", padx=16, pady=(CARD_TOP, 16))
        card.grid_columnconfigure(0, weight=1)
        self.mem_card = card
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        head.grid_columnconfigure(0, weight=1)
        self.mem_title = ctk.CTkLabel(head, text="Memory", font=self._font(12), text_color=T.TEXT_2,
                                      anchor="w", height=16)
        self.mem_title.grid(row=0, column=0, sticky="w")
        self.telemetry = ctk.CTkLabel(head, text="", font=self._mono(11), text_color=T.TEXT_3, anchor="e",
                                      height=16)
        self.telemetry.grid(row=0, column=1, sticky="e")
        # Chevron: expands the window downward to the stats section. Same height as the header row, so the
        # collapsed layout is pixel-identical to v1.0.
        self.chevron = ctk.CTkButton(head, text=T.CHEVRON_DOWN, width=20, height=16, corner_radius=4,
                                     font=ctk.CTkFont(family=T.icon_family(), size=10),
                                     fg_color="transparent", hover_color=T.RAISED, text_color=T.TEXT_3,
                                     command=self.toggle_stats)
        self.chevron.grid(row=0, column=2, padx=(6, 0))

        self.gauge = ctk.CTkFrame(card, height=8, corner_radius=4, fg_color=T.RAISED)
        self.gauge.grid(row=1, column=0, sticky="ew", padx=12)
        self.gauge.grid_propagate(False)
        self._seg_apps = ctk.CTkFrame(self.gauge, height=8, corner_radius=4, fg_color=T.APPS_SEG)
        self._seg_strata = ctk.CTkFrame(self.gauge, height=8, corner_radius=4, fg_color=T.ACCENT)

        legend = ctk.CTkFrame(card, fg_color="transparent")
        legend.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 10))
        self.legend = {}
        for col, (key, color, label) in enumerate((("apps", T.APPS_SEG, "Apps"), ("strata", T.ACCENT, "Strata"),
                                                   ("free", T.RAISED, "Free"))):
            cell = ctk.CTkFrame(legend, fg_color="transparent")
            cell.grid(row=0, column=col, sticky="w", padx=(0, 14))
            ctk.CTkFrame(cell, width=8, height=8, corner_radius=2, fg_color=color).grid(row=0, column=0,
                                                                                        padx=(0, 6))
            lbl = ctk.CTkLabel(cell, text=f"{label} –", font=self._mono(11), text_color=T.TEXT_2, height=14)
            lbl.grid(row=0, column=1)
            self.legend[key] = (lbl, label)

        # Stats section (gridded only while expanded): one card, three rows split by hairlines.
        self.stats = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=T.R_CARD, height=STATS_H)
        self.stats.grid_propagate(False)
        self.stats.grid_columnconfigure(0, weight=1)
        self.row_vram = StatRow(self.stats, "VRAM · 5 min", lambda m: LineGraph(m, window_s=self.vram.window_s))
        self.row_decode = StatRow(self.stats, "Decode · per request", BarGraph)
        self.row_prompt = StatRow(self.stats, "Prompt reading · per request", BarGraph)
        for i, row in enumerate((self.row_vram, self.row_decode, self.row_prompt)):
            if i:
                ctk.CTkFrame(self.stats, height=1, fg_color=T.HAIRLINE, bg_color=T.HAIRLINE, corner_radius=0).grid(
                    row=2 * i - 1, column=0, sticky="ew", padx=12)
            row.grid(row=2 * i, column=0, sticky="ew", padx=12)

    def _style_toggle(self, text: str, colors: tuple[str, str, str], state: str) -> None:
        fg, hover, txt = colors
        self.toggle.configure(text=text, fg_color=fg, hover_color=hover, text_color=txt, state=state)

    def _apply_state(self) -> None:
        st = self.state_name
        title, led = STATUS[st]
        if st == ACTIVE and self.loaded is False:
            title, led = LOADING_STATUS
        low = st == ACTIVE and self._engine_load is not None and self._engine_load.mode == "low_ram"
        if low and self.loaded is not False:
            title, led = LOW_RAM_STATUS
        self.pill.configure(text=title)
        self.led.configure(fg_color=led)
        if st in (IDLE, ERROR):
            self._style_toggle("Turn on", TOGGLE_ON, "normal")
        elif st == ACTIVE:
            self._style_toggle("Turn off", TOGGLE_OFF, "normal")
        elif st == STARTING:
            self._style_toggle("Cancel", TOGGLE_CANCEL, "normal")
        else:
            self._style_toggle("Stopping…", TOGGLE_WAIT, "disabled")
        self.gear.configure(state="disabled" if st in (STARTING, STOPPING) else "normal")
        busy = st == STARTING and self._start_phase == "server"
        if not busy:
            self.progress.set(0)
            self.elapsed.configure(text="")
        self._track_span()
        if st == ACTIVE:
            if self._ready_mono is None:
                self._ready_mono = time.monotonic()      # adopted servers: uptime counts from when we saw it
            self._show_serving()
            if not self._engine_checked:
                self._engine_checked = True
                self._run_worker("hc-engine-log", self._check_engine_log, self._start_token)
        elif st == ERROR and self.error_text:
            head, sep, rest = self.error_text.partition(" · ")
            if not sep or not self._fits_summary(head):
                head, rest = "", self.error_text
            self._set_status(head, rest, color=T.RED_TEXT, link=bool(self._error_log))
        self._layout()

    def _show_serving(self) -> None:
        """ACTIVE: model on the status line; the detail line carries uptime + last reply speed (optional), or a
        warning that must stay visible (Low-RAM mode, a launcher waiting for the next start, still loading)."""
        model = self.model_id or self.cfg.get("model_name") or "model"
        if self.loaded is False:
            self._set_status(model, "Still loading the model", color=T.TEXT_2)
        elif self._pending_launcher is not None:      # a launcher saved while running waits for the next start
            self._set_status(model, f"{_short(self._pending_launcher.bat_name, 28)} applies next start")
        elif self._is_low_ram():
            self._set_status("Low-RAM mode", self._low_ram_line(), color=T.AMBER)
        else:
            self._set_status(model, self._serving_line())

    def _serving_line(self) -> str:
        parts = []
        if self._ready_mono is not None:
            mins = int((time.monotonic() - self._ready_mono) // 60)
            parts.append("Up <1 min" if mins < 1 else f"Up {mins} min" if mins < 60
                         else f"Up {mins // 60} h {mins % 60:02d} min")
        series = self.metrics.decode_series() if hasattr(self, "metrics") else []
        if series:
            parts.append(f"last reply {series[-1]:.0f} tok/s")
        return " · ".join(parts)

    def _is_low_ram(self) -> bool:
        return (self.state_name == ACTIVE and self._engine_load is not None
                and self._engine_load.mode == "low_ram" and self.loaded is not False)

    # ------------------------------------------------------------------ status line + detail line
    def _fits_summary(self, text: str) -> bool:
        return self._summary_font.measure(text) <= self._summary_budget()

    def _summary_budget(self) -> int:
        """Logical px left for the summary on the status line. CTkFont.measure is unscaled, like the layout."""
        used = self.pill.cget("font").measure(self.pill.cget("text"))
        fixed = WIDTH - 22 - 6 - 8 - 8 - 16 - 28                          # margins, LED, gaps, caret, gear
        timer = (self.elapsed.cget("font").measure(self.elapsed.cget("text")) + 10) if self.elapsed.cget("text") else 0
        return int(fixed - used - timer)

    def _ellipsize(self, text: str) -> str:
        budget = self._summary_budget()
        if self._summary_font.measure(text) <= budget:
            return text
        while text and self._summary_font.measure(text + "…") > budget:
            text = text[:-1]
        return text.rstrip(" ·,") + "…"

    def _set_status(self, summary: str, detail: str = "", color: str = T.TEXT_2, link: bool = False) -> None:
        """Status-line summary (short) + the detail line (shown when opened or when the state needs it)."""
        self._summary_text = summary or ""
        self.summary.configure(text=self._ellipsize(self._summary_text) if summary else "", text_color=color)
        self._detail_text = detail or ""
        self._detail_color = color
        self._detail_link = bool(link and detail)
        self.preview.configure(text=self._detail_text, text_color=color,
                               font=self._detail_link_font if self._detail_link else self._detail_font,
                               cursor="hand2" if self._detail_link else "")
        self._layout()

    def _detail_forced(self) -> bool:
        """States whose detail the user must see: loading, stopping, errors, a fresh note, warnings."""
        st = self.state_name
        if st in (STARTING, STOPPING, ERROR) or self._detail_link:
            return True
        if time.monotonic() < self._note_until:
            return True
        return st == ACTIVE and (self.loaded is False or self._pending_launcher is not None or self._is_low_ram())

    def _layout(self) -> None:
        """Grid/hide the detail line, progress bar and caret for the current texts, then fit the window height."""
        if getattr(self, "_destroyed", False):
            return
        has = bool(self._detail_text)
        forced = self._detail_forced()
        show = has and (forced or self.detail_open)
        busy = self.state_name == STARTING and self._start_phase == "server"
        clickable = has and not forced
        self.caret.configure(text=(T.CHEVRON_UP if show else T.CHEVRON_DOWN) if clickable else "")
        for w in (self.head, self.led, self.pill, self.summary, self.caret, self.elapsed):
            try:
                w.configure(cursor="hand2" if clickable else "")
            except Exception:  # noqa: BLE001 - CTkFrame without cursor support
                pass
        if not clickable:
            self._head_hover(False)
        key = (show, busy)
        if key == self._layout_key:
            return
        self._layout_key = key
        if show:
            self.preview.grid(row=1, column=0, sticky="ew", padx=(DETAIL_INDENT, 16), pady=(GAP_DETAIL_TOP, 0))
        else:
            self.preview.grid_remove()
        if busy:
            self.progress.grid(row=2, column=0, sticky="ew", padx=16,
                               pady=(GAP_AFTER_DETAIL if show else GAP_HEAD_BUTTON, 0))
        else:
            self.progress.grid_remove()
        self.toggle.grid(row=3, column=0, pady=(GAP_AFTER_DETAIL if (show or busy) else GAP_HEAD_BUTTON, 0))
        self._fit_height()

    def _fit_height(self, force: bool = False) -> None:
        """Size the window to its content (the header grows/shrinks by the detail line; stats add their card)."""
        try:
            self.update_idletasks()
            scale = ctk.ScalingTracker.get_window_scaling(self)
            h = int(round(self.winfo_reqheight() / scale))
        except Exception:  # noqa: BLE001
            return
        if h != self._fit_h or force:
            self._fit_h = h
            self.geometry(f"{WIDTH}x{h}")

    def _on_head_click(self, _event=None) -> None:
        if not self._detail_text or self._detail_forced():
            return
        self.detail_open = not self.detail_open
        self._save_state_later("detail_open", self.detail_open)
        self._layout()

    def _head_hover(self, on: bool) -> None:
        clickable = bool(self._detail_text) and not self._detail_forced()
        self.head.configure(fg_color=T.SURFACE if (on and clickable) else "transparent")

    def _dark_caption(self) -> None:
        T.dark_caption(self)

    # ------------------------------------------------------------------ reopen swept apps
    def _reopen_worker(self) -> None:
        """Worker, after Turn off: relaunch the auto apps one by one; games stay pending as a one-click offer."""
        try:
            records = reopen.load_pending()
            if not records:
                self.post(self._reopen_done, [], [])
                return
            auto, manual, skipped = reopen.classify(records)
            for r in skipped:
                log.info("reopen: skipping %s (%s)", r.name, r.reason_skip)
            results = reopen.reopen(records, stagger_s=REOPEN_STAGGER_S, stop_evt=self._stop_evt,
                                    on_progress=lambda d, t, n: self.post(self._reopen_progress, d, t, n))
            if manual:
                reopen.save_pending(manual)
            else:
                reopen.clear_pending()
            self.post(self._reopen_done, results, [r.to_dict() for r in manual])
        except Exception:  # noqa: BLE001
            log.exception("reopen failed")
            self.post(self._reopen_done, [], [])

    def _reopen_progress(self, done: int, total: int, name: str) -> None:
        if self.state_name == IDLE and done < total:
            self._set_note(f"Reopening apps {done}/{total}\n{_short(name)} started")

    def _reopen_done(self, results: list, manual: list) -> None:
        self._reopening = False
        started = [r["name"] for r in results if r.get("ok")]
        failed = [r for r in results if not r.get("ok") and r.get("status") != "cancelled"]
        for r in failed:
            log.warning("reopen: %s not started: %s", r.get("name"), r.get("reason"))
        self._reopen_offer = [reopen.LaunchRecord.from_dict(d) for d in manual]
        if self.state_name != IDLE or not (results or manual):
            return                               # nothing was pending: leave the "Stopped · freed" note alone
        if started or failed:
            names = ", ".join(_short(n, 16) for n in started[:2]) + (f" +{len(started) - 2}" if len(started) > 2 else "")
            line = f"Reopened {len(started)} app{'s' if len(started) != 1 else ''}" + (f"\n{names}" if names else "")
            if failed:
                line = f"Reopened {len(started)} · {len(failed)} failed\n" + ", ".join(
                    f"{_short(str(r.get('name')), 14)}: {r.get('reason')}" for r in failed[:2])
            self._set_note(line)
        self._request_preview()

    def _load_reopen_offer(self) -> None:
        """Worker, at startup: apps closed in an earlier session (Strata stopped another way, or games)."""
        try:
            recs = reopen.load_pending()
        except Exception:  # noqa: BLE001
            log.debug("load_pending failed", exc_info=True)
            return
        if recs:
            self.post(self._set_reopen_offer, [r.to_dict() for r in recs])

    def _set_reopen_offer(self, dicts: list) -> None:
        if self.state_name == IDLE and not self._reopening:
            self._reopen_offer = [reopen.LaunchRecord.from_dict(d) for d in dicts]
            self._request_preview()

    def _offer_text(self) -> str:
        offer = self._reopen_offer
        if len(offer) == 1:
            return f"Reopen {_short(offer[0].name.removesuffix('.exe'), 26)}"
        return f"Reopen {len(offer)} closed apps"

    def _click_reopen_offer(self) -> None:
        recs, self._reopen_offer = list(self._reopen_offer), []
        self._set_note(f"Reopening {len(recs)} app{'s' if len(recs) != 1 else ''}…")
        self._run_worker("hc-reopen-click", _reopen_clicked, recs, self.post, self._reopen_done)

    def _on_preview_click(self, _event=None) -> None:
        if self.state_name == IDLE and self._reopen_offer and self._detail_link:
            self._click_reopen_offer()
            return
        if self.state_name == ERROR and self._error_log and os.path.exists(self._error_log):
            try:
                os.startfile(self._error_log)          # opens in the default .log/.txt viewer
            except OSError:
                log.exception("could not open %s", self._error_log)

    def _set_preview(self, text: str, color: str = T.TEXT_2) -> None:
        """A message for the status area: its first line goes on the status line when it fits, the rest (or the
        whole message, when it is too long for the status line) becomes the detail line."""
        head, _, rest = (text or "").partition("\n")
        if head and not self._fits_summary(head):
            head, rest = "", head + (f" · {rest}" if rest else "")
        self._set_status(head, rest.replace("\n", " · "), color=color)

    def _strata_config_path(self) -> str | None:
        """The Strata config JSON from launch_cmd's --config (its "log" field names the engine log)."""
        cmd = list(self.cfg.get("launch_cmd") or [])
        for i, a in enumerate(cmd[:-1]):
            if str(a) == "--config":
                return str(cmd[i + 1])
        return None

    def _low_ram_line(self) -> str:
        ld = self._engine_load
        short = f" · ~{ld.short_gib:.1f} GB short" if ld and ld.short_gib else ""
        return f"Reading from SSD (slower){short}"

    def _check_engine_log(self, token: int) -> None:
        """Worker: one engine-log verdict once serving. Our own start: the run since launch; adopted: the latest run."""
        cfg_path = self._strata_config_path()
        if not cfg_path:
            return
        since = self._launch_wall if self._launch_wall and token == self._start_token else None
        try:
            load = engine_log.current(cfg_path, since)
        except Exception:  # noqa: BLE001
            log.debug("engine log check failed", exc_info=True)
            return
        if load.mode != "unknown":
            self.post(self._set_engine_load, load)

    def _set_engine_load(self, load) -> None:
        if self.state_name not in (STARTING, ACTIVE):
            return
        changed = (self._engine_load is None) or (self._engine_load.mode != load.mode)
        self._engine_load = load
        if changed:
            log.info("engine load mode: %s (arena %s GiB, available %s GiB, short %s GiB, GPU slots %s%s)",
                     load.mode, load.arena_gib, load.available_gib, load.short_gib, load.gpu_slots,
                     ", GPU cache shrunk" if load.gpu_shrunk else "")
            if self.state_name == ACTIVE:
                self._apply_state()

    def _set_note(self, text: str) -> None:
        """A one-off result line ("Stopped · freed 13.5 GB", "Start cancelled") that stays readable for a few
        seconds. Without the hold the idle sweep preview, requested right after, replaced it within ~0.3 s."""
        self._note_until = time.monotonic() + NOTE_HOLD_S      # first: the layout shows a held note's detail
        self._set_preview(text)
        if self._note_id is not None:
            try:
                self.after_cancel(self._note_id)
            except Exception:  # noqa: BLE001
                log.debug("after_cancel failed", exc_info=True)
        self._note_id = self.after(int(NOTE_HOLD_S * 1000) + 50, self._note_expired)

    def _note_expired(self) -> None:
        self._note_id = None
        self._note_until = 0.0
        self._layout()
        self._request_preview()

    def _set_state(self, st: str) -> None:
        if st == ACTIVE and getattr(self, "state_name", None) in (IDLE, ERROR) and hasattr(self, "metrics"):
            # Adopted a Strata we did not start (or one restarted outside the app): its request history is not ours
            self._metrics_epoch += 1
            self.metrics.reset()
        self.state_name = st
        self._state_seq += 1
        self._note_until = 0.0                   # a note belongs to the state it was shown in
        if st != ERROR:
            self._error_hold_until = 0.0
        if st in (IDLE, ERROR):                  # no Strata of ours any more: drop memory baselines
            self._ready_mono = None
            self._avail_at_launch = None
            self._strata_est = None
            self._engine_load = None
            self._engine_checked = False
            self._apply_pending_launcher()
        log.info("state -> %s", st)
        self._apply_state()
        self._update_telemetry()

    def _update_telemetry(self) -> None:
        """Header of the memory card: session total freed (only once something was freed)."""
        show = self.freed_mb > 0 and self.state_name == IDLE      # not while Strata is refilling that RAM
        self.telemetry.configure(text=f"Freed {_gb(self.freed_mb)} this session" if show else "")

    def _apply_memory(self, total_mb: float, avail_mb: float, strata_mb: float | None) -> None:
        """Gauge: apps (everything else in use) · Strata · free, as fractions of physical RAM."""
        if total_mb <= 0:
            return
        used = max(0.0, total_mb - avail_mb)
        strata = min(used, max(0.0, strata_mb or 0.0))
        apps = used - strata
        self._mem = (total_mb, avail_mb, strata_mb)
        fa, fs = apps / total_mb, strata / total_mb
        self._seg_apps.place(relx=0, rely=0, relwidth=max(fa, 0.0), relheight=1)
        if fs > 0.002:
            self._seg_strata.place(relx=min(fa, 0.98), rely=0, relwidth=max(fs, 0.02), relheight=1)
        else:
            self._seg_strata.place_forget()
        self.mem_title.configure(text=f"Memory · {_gb(total_mb, 0)}")
        vals = {"apps": apps, "strata": strata if strata_mb is not None else None, "free": avail_mb}
        for key, (lbl, label) in self.legend.items():
            v = vals[key]
            approx = "~" if key == "strata" and v is not None else ""     # Strata is an estimate
            lbl.configure(text=f"{label} {'–' if v is None else approx + _gb(v)}")

    def add_freed(self, mb: float | None) -> None:
        try:
            self.freed_mb += max(0.0, float(mb or 0.0))
        except (TypeError, ValueError):
            return
        self._update_telemetry()

    # ================================================================== stats section
    def toggle_stats(self) -> None:
        self.expanded = not self.expanded
        self._apply_expanded()
        self._save_state_later("stats_expanded", self.expanded)

    def _save_state_later(self, key: str, value) -> None:
        """Debounced on the Tk thread, then saved off it with VALUES only. Never hand a worker a closure over
        self: if that thread drops the last reference to a destroyed app, tkinter Font.__del__ runs there, and a
        Tk call off the main thread can block forever - here while holding config_store's lock (found in tests)."""
        self._state_pending[key] = value
        if self._state_save_id is not None:
            self.after_cancel(self._state_save_id)
        self._state_save_id = self.after(STATE_SAVE_DEBOUNCE_MS, self._flush_state)

    def _flush_state(self) -> None:
        self._state_save_id = None
        pending, self._state_pending = dict(self._state_pending), {}
        if pending:
            self._run_worker("hc-state", config_store.save_state, pending)

    def _apply_expanded(self) -> None:
        """Grow the window downward to show the stats card, or shrink back to the exact v1.0 size."""
        self.chevron.configure(text=T.CHEVRON_UP if self.expanded else T.CHEVRON_DOWN)
        if self.expanded:
            self.mem_card.grid(pady=(CARD_TOP, STATS_GAP))   # .grid(), not grid_configure(): only .grid() is DPI-scaled
            self.stats.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 16))
            self._fit_height(force=True)
            self._keep_on_screen()
            self._refresh_stats()
            if self._stats_id is None and not self._destroyed:
                self._stats_id = self.after(STATS_MS, self._stats_tick)
        else:
            self.stats.grid_remove()
            self.mem_card.grid(pady=(CARD_TOP, 16))
            self._fit_height(force=True)
            if self._stats_id is not None:
                try:
                    self.after_cancel(self._stats_id)
                except Exception:  # noqa: BLE001
                    log.debug("after_cancel failed", exc_info=True)
                self._stats_id = None

    def _keep_on_screen(self) -> None:
        """Expanding near the bottom of the screen would push the graphs under the taskbar: slide the window up."""
        try:
            self.update_idletasks()
            if self.state() != "normal":
                return
            scale = ctk.ScalingTracker.get_window_scaling(self)
            mon = _work_area(self)
            if mon is None:
                return
            _left, top, _right, bottom = mon
            x, y = self.winfo_x(), self.winfo_y()
            frame_h = (self.winfo_rooty() - y) + round((self._fit_h or HEIGHT_EXPANDED) * scale) + 8
            if y + frame_h > bottom:
                tkinter.Wm.wm_geometry(self, f"+{x}+{max(top, bottom - frame_h)}")   # position only, unscaled
        except Exception:  # noqa: BLE001 - cosmetic
            log.debug("keep-on-screen failed", exc_info=True)

    def _track_span(self) -> None:
        """Remember when Strata was running (server launch -> stopped) to tint that stretch of the VRAM line."""
        st = self.state_name
        alive = st in (ACTIVE, STOPPING) or (st == STARTING and self._start_phase == "server")
        open_span = bool(self._spans) and self._spans[-1][1] is None
        now = time.monotonic()
        if alive and not open_span:
            self._spans.append([now, None])
        elif not alive and open_span:
            self._spans[-1][1] = now
        cutoff = now - self.vram.window_s
        self._spans = [sp for sp in self._spans if sp[1] is None or sp[1] >= cutoff]

    def _stats_tick(self) -> None:
        self._stats_id = None
        if self._destroyed or not self.expanded:
            return
        try:
            if not self._iconic:
                self._refresh_stats()
        finally:
            if not self._destroyed and self.expanded:
                self._stats_id = self.after(STATS_MS, self._stats_tick)

    def _refresh_stats(self) -> None:
        """Main thread: redraw the three graphs from the sampler / metrics store."""
        if not self.expanded:
            return
        polling = self.state_name in (ACTIVE, STOPPING)
        if not polling:
            self.metrics.clear_live()                     # nothing is generating once Strata is not serving
        snap = self.vram.snapshot()
        row = self.row_vram
        if snap.available and snap.total_mb and snap.samples:
            used = snap.samples[-1][1]
            spans = [(a, snap.now if b is None else b) for a, b in self._spans]
            row.graph.set_data(snap.samples, snap.total_mb, snap.now, spans=spans,
                               max_label=f"{snap.total_mb / 1024.0:.0f} GB",
                               empty=None if len(snap.samples) > 1 else "Collecting…")
            row.set_value(f"{used / 1024.0:.1f} / {snap.total_mb / 1024.0:.1f} GB")
        else:
            row.graph.set_data((), 1.0, snap.now, empty="VRAM unavailable" if snap.available is False
                               else "Collecting…")
            row.set_value("–", T.TEXT_3)
        if polling and self._metrics_fails >= METRICS_FAILS_SHOWN:
            empty_d = empty_p = "Metrics unavailable"
        elif polling:
            empty_d, empty_p = "No requests yet", "No prompt reads yet"
        else:
            empty_d = empty_p = "Strata is off"
        self._fill_rate_row(self.row_decode, self.metrics.decode_series(), self.metrics.live_decode, empty_d)
        self._fill_rate_row(self.row_prompt, self.metrics.prompt_series(), self.metrics.live_prompt, empty_p)

    @staticmethod
    def _fill_rate_row(row: StatRow, series: list[float], live: float | None, empty: str) -> None:
        row.graph.set_data(series, live=live, empty=empty)
        if live is not None:
            row.set_value(f"{fmt_rate(live)} tok/s", T.ACCENT, "live")
        elif series:
            row.set_value(f"{fmt_rate(series[-1])} tok/s", T.TEXT, "last")
        else:
            row.set_value("–", T.TEXT_3)

    def _metrics_loop(self) -> None:
        """Worker: poll Strata's /metrics every 2 s, only while it is serving AND the stats section is open."""
        last_poll = 0.0
        last_epoch = None
        while not self._stop_evt.is_set():
            try:
                # Stats open: every STATS_MS. Only the serving detail line open: every ~10 s, for "last reply".
                slow_ok = (self.detail_open and self.state_name == ACTIVE
                           and time.monotonic() - last_poll >= DETAIL_POLL_S)
                if (self.expanded or slow_ok) and not self._iconic and self.state_name in (ACTIVE, STOPPING):
                    epoch = self._metrics_epoch
                    root = config_store.server_root(str(self.cfg.get("endpoint_url") or ""))
                    # /metrics returns only the newest 12 requests: after a gap (section collapsed, window minimised,
                    # a new server) ask once for all of them so the 30 bars are complete.
                    now = time.monotonic()
                    catch_up = epoch != last_epoch or now - last_poll > 3 * STATS_MS / 1000.0
                    data = None
                    if root:
                        data = (self._fetch_metrics(root, all_requests=True) if catch_up
                                else self._fetch_metrics(root))
                    last_poll, last_epoch = now, epoch
                    self.post(self._apply_metrics, epoch, data)
            except Exception:  # noqa: BLE001
                log.debug("metrics poll failed", exc_info=True)
            self._stop_evt.wait(STATS_MS / 1000.0)

    def _apply_metrics(self, epoch: int, data: dict | None) -> None:
        if epoch != self._metrics_epoch or self.state_name not in (ACTIVE, STOPPING):
            return                                        # answer of a previous server / arrived after the stop
        if not isinstance(data, dict):
            self._metrics_fails += 1
            self.metrics.clear_live()
        else:
            self._metrics_fails = 0
            self.metrics.ingest(data, now=time.monotonic())
            if self.metrics.hw_vram is not None:          # fallback VRAM source when NVML is unavailable
                self.vram.push_external(*self.metrics.hw_vram)
        self._refresh_stats()

    # ================================================================== progress / memory
    def _progress_tick(self) -> None:
        """Main thread, every 500 ms while the server is starting: elapsed timer + bar from the last parse."""
        self._progress_id = None
        if self._destroyed or not (self.state_name == STARTING and self._start_phase == "server"):
            return
        elapsed = time.monotonic() - self._server_t0
        label, frac, detail = load_progress.parse(self._log_text, elapsed, self._expected_s)
        if frac < self._prog_best[1]:            # a phase line scrolled out of the tail: never go backwards
            label, frac, detail = self._prog_best
        self._prog_best = (label, frac, detail)
        ld = self._engine_load                   # the engine log knows what stdout does not
        warn = False
        if ld is not None and ld.mode == "waiting_for_ram":
            label, warn = "Waiting for RAM", True
            if ld.arena_gib and ld.available_gib:
                detail = f"needs {ld.arena_gib:.0f} GB, {ld.available_gib:.0f} GB free"
        elif ld is not None and ld.mode == "low_ram":
            label, detail, warn = "Low-RAM mode", "reading experts from SSD · slower", True
        self.progress.set(frac)
        self.elapsed.configure(text=load_progress.fmt_elapsed(elapsed))
        note = self._start_note
        if note == LOW_RAM_NOTE and ld is not None and ld.mode in ("low_ram", "waiting_for_ram"):
            note = ""                            # the phase line already says it, with the engine's numbers
        usual = f"usually {load_progress.fmt_elapsed(self._expected_s)}" if self._expected_s else ""
        self._set_status(label, " · ".join(x for x in (detail, note or usual) if x), color=T.AMBER if warn else T.TEXT_2)
        self._progress_id = self.after(PROGRESS_MS, self._progress_tick)

    def _log_reader(self, token: int) -> None:
        """Worker: re-read the Strata log tail every second while this start is live."""
        while not self._stop_evt.is_set() and token == self._start_token and self._start_phase == "server":
            path = None
            try:
                path = self.endpoint.status().get("log_path")
                # status() may still name the PREVIOUS run's log (ends in "ready:") until the new one exists
                if path and os.path.getmtime(path) < self._launch_wall - 2:
                    path = None
            except Exception:  # noqa: BLE001
                path = None
            if path:
                self._log_text = load_progress.read_tail(path)
            cfg_path = self._strata_config_path()
            if cfg_path:
                try:
                    load = engine_log.current(cfg_path, self._launch_wall)
                    if load.mode != "unknown":
                        self._engine_load = load     # atomic swap; the progress tick reads it on the Tk thread
                except Exception:  # noqa: BLE001
                    log.debug("engine log read failed", exc_info=True)
            self._stop_evt.wait(1.0)

    def strata_estimate(self, avail: float, rss: float | None) -> float | None:
        """Best estimate of the RAM Strata holds (MB), or None when unknown.

        Strata's expert pages are mostly NOT charged to its processes (verified: tree RSS ~0.2 GB while free RAM
        drops ~14 GB), so the drop in free RAM since we launched it is the real signal:
          STARTING  live drop since launch (it is loading right now)
          ACTIVE    the drop latched at ready, so apps opened later are not booked to Strata
          STOPPING  latched value shrinking as memory comes back
        Adopted (no baseline): process RSS only if it is meaningful, else unknown.
        """
        st, base, latched = self.state_name, self._avail_at_launch, self._strata_est
        if st == STARTING and base is not None:
            return max(rss or 0.0, base - avail)
        if st == ACTIVE and latched is not None:
            return max(rss or 0.0, latched)
        if st == STOPPING and latched is not None and base is not None:
            return min(latched, max(0.0, base - avail))
        if st in (ACTIVE, STOPPING) and rss is not None and rss >= 1024:
            return rss
        return None

    def _memory_loop(self) -> None:
        n = 0
        rss: float | None = None
        while not self._stop_evt.is_set():
            try:
                if not self._iconic:
                    vm = psutil.virtual_memory()
                    total, avail = vm.total / 1048576.0, vm.available / 1048576.0
                    if self.state_name in (ACTIVE, STARTING, STOPPING):
                        if n % 5 == 0:                   # process-tree walk every ~10 s, not every tick
                            fn = getattr(self.endpoint, "strata_memory_mb", None)
                            rss = fn() if fn else None
                        n += 1
                    else:
                        n, rss = 0, None
                    self.post(self._apply_memory, total, avail, self.strata_estimate(avail, rss))
            except Exception:  # noqa: BLE001
                log.debug("memory sample failed", exc_info=True)
            self._stop_evt.wait(MEMORY_S)

    # ================================================================== threading
    def post(self, fn: Callable, *args) -> None:
        """Thread-safe: run fn(*args) on the Tk main thread."""
        self._q.put((fn, args))

    def _pump(self) -> None:
        try:
            while not self._destroyed:
                try:
                    fn, args = self._q.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn(*args)
                except Exception:
                    log.exception("posted callback %r failed", fn)
        finally:
            if not self._destroyed:
                try:
                    self._pump_id = self.after(PUMP_MS, self._pump)
                except Exception:
                    log.debug("pump reschedule failed", exc_info=True)

    def _run_worker(self, name: str, fn: Callable, *args) -> None:
        threading.Thread(target=fn, args=args, name=name, daemon=True).start()

    def _monitor_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                seq = self._state_seq
                status = self.endpoint.status()         # can take seconds (identity probe)
                self.post(self._on_monitor_status, seq, status)
            except Exception:
                log.exception("endpoint.status() failed")
            self._stop_evt.wait(TICK_MS / 1000.0)

    def _on_monitor_status(self, seq: int, status: dict) -> None:
        """A reading taken before the last state change is stale (e.g. 'active' probed just before OFF finished
        would flip the idle UI back to Serving for ten seconds): drop it, the next tick brings a fresh one."""
        if seq == self._state_seq:
            self.on_status(status)

    def _dialog_open(self) -> bool:
        d = self.dialog
        if d is None:
            return False
        try:
            if d.winfo_exists():
                return True
        except Exception:
            pass
        self.dialog = None
        return False

    def on_status(self, status: dict) -> None:
        """Reconcile ACTIVE<->IDLE from live endpoint state (main thread)."""
        if self.state_name in (STARTING, STOPPING) or self._exit_pending or self._dialog_open():
            return
        if self.state_name == ERROR and time.monotonic() < self._error_hold_until:
            return                              # keep the error visible for its full hold time
        live = (status or {}).get("state")
        if live != "idle":
            self._idle_streak = 0
        if live == "active":
            changed = self.state_name != ACTIVE or self.loaded != status.get("loaded") \
                or (status.get("model_id") and status.get("model_id") != self.model_id)
            self.loaded = status.get("loaded")
            if status.get("model_id"):
                self.model_id = status.get("model_id")
            if changed:
                if self.state_name != ACTIVE:
                    self._metrics_fails = 0     # failed polls of the previous server say nothing about this one
                self._set_state(ACTIVE)
        elif self.state_name == ACTIVE and live == "idle":
            self._idle_streak += 1
            if self._idle_streak < 2:           # one stray reading (probe race) must not flip the UI
                return
            self._idle_streak = 0
            self.model_id, self.loaded = None, None
            self._set_state(IDLE)
            self._set_note("Endpoint is no longer running")
            self._request_preview()

    def _reconcile_now(self, note: str | None = None) -> None:
        """start/stop_endpoint returned False: show IDLE, then reconcile from a status() fetched on a worker."""
        self._start_phase = None
        self._set_state(IDLE)
        if note:
            self._set_note(note)

        def work() -> None:
            try:
                st = self.endpoint.status()
            except Exception:
                log.exception("endpoint.status() failed")
                st = {}                           # still reconcile: a pending exit must not hang forever
            self.post(self._after_reconcile, st)

        self._run_worker("hc-reconcile", work)

    def _after_reconcile(self, status: dict) -> None:
        live = (status or {}).get("state")
        if live in ("starting", "stopping") and self.state_name == IDLE:
            self._set_note(f"Endpoint is busy ({live}) — try again shortly")
        self.on_status(status)
        if self._exit_pending and self.state_name in (IDLE, ERROR):
            self._finish_exit()

    # ================================================================== periodic
    def _first_refresh(self) -> None:
        self._first_id = None
        self._request_preview()
        self._run_worker("hc-reopen-load", self._load_reopen_offer)

    def _tick(self) -> None:
        try:
            try:
                self._iconic = self.state() == "iconic"
            except Exception:  # noqa: BLE001
                self._iconic = False
            self._update_telemetry()
            self._request_preview()
            if self.state_name == ACTIVE and not self._detail_forced():
                self._show_serving()             # uptime / last reply speed
            # Collect garbage HERE, on the Tk thread: closed dialogs leave Tk objects (CTkFont, images) in reference
            # cycles, and if the collector happens to run on a worker instead, their __del__ makes a Tk call off the
            # main thread (blocks until the main loop services it; forever once it has stopped). Cheap: ~1 ms.
            gc.collect()
        finally:
            if not self._destroyed:
                self._tick_id = self.after(TICK_MS, self._tick)

    def _request_preview(self) -> None:
        # ERROR keeps its message (and log link) until the user acts; only IDLE shows the sweep preview
        if self.state_name != IDLE or self._preview_busy:
            return
        if time.monotonic() < max(self._error_hold_until, self._note_until):
            return
        try:
            if self.state() == "iconic":
                return
        except Exception:
            return
        self._preview_busy = True
        self._run_worker("hc-preview", self._preview_worker)

    def _preview_worker(self) -> None:
        try:
            res = self.detector.sweep_processes(dry_run=True)
        except Exception as exc:
            log.exception("dry-run sweep failed")
            res = {"_error": str(exc)}
        self.post(self._apply_preview, res)

    def _apply_preview(self, res: dict) -> None:
        self._preview_busy = False
        if self.state_name != IDLE or time.monotonic() < self._note_until:
            return
        if "_error" in res:
            self._set_preview(f"Scan failed: {res['_error']}")
        elif self._reopen_offer and not self._reopening:
            head = format_preview(res).partition("\n")[0]
            self._set_status(head if self._fits_summary(head) else "", self._offer_text(), color=T.TEXT_2, link=True)
        else:
            self._set_preview(format_preview(res))

    # ================================================================== ON flow
    def on_toggle(self) -> None:
        # CTk fires the command on every ButtonRelease: the 2nd click of a double-click would cancel the start it
        # just began. Ignore clicks closer than TOGGLE_DEBOUNCE_S.
        now = time.monotonic()
        if now - self._toggle_at < TOGGLE_DEBOUNCE_S:
            return
        self._toggle_at = now
        if self._dialog_open() and not isinstance(self.dialog, SweepDialog):
            try:                                  # a click slipped past a dialog's grab: bring the dialog back
                self.dialog.lift()
                self.dialog.focus_force()
            except Exception:  # noqa: BLE001
                log.debug("dialog lift failed", exc_info=True)
            return
        st = self.state_name
        if st in (IDLE, ERROR):
            self.turn_on()
        elif st == ACTIVE:
            self.turn_off()
        elif st == STARTING:
            self.cancel_start()

    def turn_on(self) -> None:
        self._sweep_note = ""
        self._reopen_this = False
        if self._reopen_offer:                   # starting again: the old "Reopen …" offer is stale
            self._reopen_offer = []
            self._run_worker("hc-reopen-clear", reopen.clear_pending)
        self._apply_pending_launcher()
        self._start_token += 1
        token = self._start_token
        self.error_text = None
        self._error_hold_until = 0.0
        self._start_phase = "scan"
        self._set_state(STARTING)
        self._set_preview("Scanning for apps to close…")

        def work() -> None:
            try:
                cands = self.detector.scan_killable_processes()
            except Exception:
                log.exception("scan_killable_processes failed; starting without a sweep")
                cands = []
            self.post(self._after_scan, token, cands, self._ram_plan(cands))

        self._run_worker("hc-scan", work)

    def _token_live(self, token: int) -> bool:
        return token == self._start_token and self.state_name == STARTING

    def _ram_plan(self, cands: list[dict]):
        """Worker: does Strata get its fast full-RAM mode, and which apps would get it there? None if unknown."""
        try:
            cfg_path = self._strata_config_path()
            log_path = engine_log.config_log_path(cfg_path) if cfg_path else None
            arena = engine_log.learned_arena_gib(log_path) if log_path else None
            if not arena:
                return None
            avail = psutil.virtual_memory().available / 1048576.0
            protected = {str(n) for n in (self.cfg.get("user_whitelist") or [])}
            plan = ram_plan.plan(cands, avail, arena, protected_names=protected)
            log.info("RAM plan: need %.0f MB, available %.0f MB, short %.0f MB, pre-tick %s, reachable %s",
                     plan.need_mb, plan.available_mb, plan.short_mb, plan.preticked_pids, plan.reachable)
            return plan
        except Exception:  # noqa: BLE001
            log.exception("RAM plan failed; using the detector's pre-ticks")
            return None

    def _after_scan(self, token: int, cands: list[dict], plan=None) -> None:
        if not self._token_live(token):
            return
        if not cands:
            if plan is not None and not plan.enough_now:
                self._sweep_note = LOW_RAM_NOTE
            self._begin_server_start(token)
            return
        self._start_phase = "dialog"
        self._set_preview(f"{len(cands)} app(s) could be closed — choose in the dialog")
        ref: dict = {}
        self.dialog = SweepDialog(self, cands, lambda d, t: self._on_sweep_decision(token, d, t, ref.get("dlg")),
                                  ram=plan, reopen_default=self._reopen_pref)
        ref["dlg"] = self.dialog

    def _on_sweep_decision(self, token: int, decision: str, targets: list[tuple[int, float]], dlg=None) -> None:
        self.dialog = None
        if dlg is not None and decision != "cancel":
            choice = bool(getattr(dlg, "reopen_choice", self._reopen_pref))
            self._reopen_this = choice and decision == "close_and_start" and bool(targets)
            if choice != self._reopen_pref:
                self._reopen_pref = choice
                self._save_state_later("reopen_after", choice)
        if not self._token_live(token):
            return
        if decision == "cancel":
            self._start_phase = None
            self._set_state(IDLE)
            self._set_note("Start cancelled")
            if self._exit_pending:
                self._finish_exit()
            return
        if decision == "close_and_start" and targets:
            self._start_phase = "sweep"
            self._set_preview(f"Closing {len(targets)} app(s)…")

            def work() -> None:
                try:
                    res = self.detector.sweep_processes(dry_run=False, targets=list(targets))
                except Exception as exc:
                    log.exception("sweep failed")
                    res = {"closed_apps": [], "ram_freed_mb": 0.0, "failed": [], "error": str(exc)}
                self.post(self._after_sweep, token, res)

            self._run_worker("hc-sweep", work)
            return
        self._begin_server_start(token)

    def _after_sweep(self, token: int, res: dict) -> None:
        self.add_freed(res.get("ram_freed_mb"))
        records = list(res.get("launch_records") or [])
        if records and self._reopen_this:
            self._run_worker("hc-reopen-save", _save_reopen_records, records)
        failed = res.get("failed") or []
        if failed:
            log.warning("sweep could not close: %s", failed)
        if self._exit_pending and token == self._start_token and self._start_phase == "sweep":
            self._start_phase = None
            self._set_state(IDLE)
            self._finish_exit()
            return
        if not self._token_live(token):
            return                                # a cancelled start's late result must not leak a note into the next
        self._sweep_note = ""
        if failed or res.get("error"):
            names = ", ".join(_short(str(f.get("name", "?")), 18) for f in failed[:2]) or "some apps"
            if len(failed) > 2:
                names += f" +{len(failed) - 2}"
            self._sweep_note = f"Could not close: {names} — starting anyway"
        self._begin_server_start(token)

    def _begin_server_start(self, token: int) -> None:
        self._start_phase = "server"
        self._start_note = self._sweep_note
        self._sweep_note = ""
        self._server_t0 = time.monotonic()
        self._launch_wall = time.time()
        self._log_text = ""
        self._prog_best = ("Launching", 0.0, None)
        self._engine_load = None
        self._engine_checked = False
        self._metrics_epoch += 1                 # a new server: its request history starts empty
        self._metrics_fails = 0
        self.metrics.reset()
        try:
            self._avail_at_launch = psutil.virtual_memory().available / 1048576.0
        except Exception:  # noqa: BLE001
            self._avail_at_launch = None
        self._apply_state()                      # shows the progress bar now that the server phase began
        self._set_preview("Launching Strata…")
        self._run_worker("hc-log-reader", self._log_reader, token)
        if self._progress_id is None:
            self._progress_id = self.after(PROGRESS_MS, self._progress_tick)
        ok = self.endpoint.start_endpoint(
            lambda: self.post(self._on_ready, token),
            lambda msg: self.post(self._on_start_error, token, msg),
        )
        if not ok:
            log.info("start_endpoint returned False; reconciling")
            self._reconcile_now("Endpoint busy — state refreshed")

    def _on_ready(self, token: int) -> None:
        if not self._token_live(token):
            return
        elapsed = time.monotonic() - self._server_t0 if self._server_t0 else 0.0
        self._start_phase = None
        self.loaded = True
        self.model_id = None
        self._ready_mono = time.monotonic()
        if elapsed > 15:                         # a real cold load (adopting a running server is ~instant)
            old = self._expected_s
            self._expected_s = elapsed if not old else max(elapsed, 0.5 * old + 0.5 * elapsed)
            self._run_worker("hc-state", config_store.save_state, {"last_cold_load_s": round(self._expected_s, 1)})
        if self._avail_at_launch is not None:
            try:
                self._strata_est = max(0.0, self._avail_at_launch - psutil.virtual_memory().available / 1048576.0)
            except Exception:  # noqa: BLE001
                self._strata_est = None
        self._set_state(ACTIVE)

        def work() -> None:  # model_id comes from status(); status() must run off the main thread
            try:
                st = self.endpoint.status()
            except Exception:
                log.exception("endpoint.status() failed")
                return
            self.post(self.on_status, st)

        self._run_worker("hc-ready-status", work)

    def _on_start_error(self, token: int, msg: str) -> None:
        if not self._token_live(token):
            return
        self._start_phase = None
        self.show_error(msg or "Start failed")

    def show_error(self, msg: str) -> None:
        # Long log paths wrap to 4 lines in a 36 px label; show the file name and make the line open it.
        m = re.search(r"\s*—\s*see\s+(.+\.log)\s*$", msg or "")
        self._error_log = m.group(1) if m else None
        if m:
            msg = msg[: m.start()] + f" · Open {os.path.basename(m.group(1))}"
        self.error_text = msg
        self._error_hold_until = time.monotonic() + ERROR_HOLD_S
        self._set_state(ERROR)
        if self._exit_pending:
            self._exit_pending = False  # stay open so the user sees what went wrong

    def cancel_start(self) -> None:
        phase = self._start_phase
        if phase in ("scan", "sweep", "dialog", None):
            # No server launch requested yet: cancel locally (a running sweep finishes on its own,
            # and the bumped token stops it from starting the server afterwards).
            self._start_token += 1
            self._start_phase = None
            if self._dialog_open() and isinstance(self.dialog, SweepDialog):
                self.dialog._decide("cancel")
            self._set_state(IDLE)
            self._set_note("Start cancelled")
            return
        self._start_token += 1
        self._start_phase = None
        self._set_state(STOPPING)
        self._set_preview("Cancelling start…")
        if not self.endpoint.stop_endpoint(lambda r: self.post(self.on_stopped, r)):
            self._reconcile_now()

    # ================================================================== OFF flow
    def turn_off(self, force: bool = False) -> None:
        self._set_state(STOPPING)
        self._set_preview("Unloading model and stopping Strata…" if not force else "Force stopping Strata…")
        if not self.endpoint.stop_endpoint(lambda r: self.post(self.on_stopped, r), force=force):
            log.info("stop_endpoint returned False; reconciling")
            self._reconcile_now()

    def on_stopped(self, result: dict) -> None:
        try:
            self._on_stopped(result or {})
        except Exception:
            log.exception("on_stopped failed")
            self.show_error("Stop handling failed — see logs\\controller.log")

    def _on_stopped(self, result: dict) -> None:
        if not result.get("ok") and result.get("unload") == "busy":
            if messagebox.askyesno(APP_TITLE, "Model is mid-turn — force stop?", parent=self):
                self.turn_off(force=True)
            else:
                self._exit_pending = False
                self._set_state(ACTIVE)
            return
        if result.get("cancelled_start") and not (result.get("ok") or result.get("port_released")):
            self.show_error(result.get("error") or "Cancel failed — Strata may still be running")
            return
        if result.get("cancelled_start"):
            self.model_id, self.loaded = None, None
            self._set_state(IDLE)
            self._set_note("Start cancelled")
            self._request_preview()
        elif result.get("port_released"):
            self.model_id, self.loaded = None, None
            self._avail_at_launch = None
            self.add_freed(result.get("ram_freed_mb"))
            self._set_state(IDLE)
            self._set_note(f"Stopped · freed {_gb(result.get('ram_freed_mb'))}")
            self._request_preview()
            if not self._exit_pending:
                self._reopening = True
                self._run_worker("hc-reopen", self._reopen_worker)
        else:
            self.show_error(result.get("error") or "Stop failed — port still in use")
            return
        if self._exit_pending:
            self._finish_exit()

    # ================================================================== dialogs / exit
    def open_settings(self) -> None:
        """Gear: launcher choice + protected apps. Not while STARTING/STOPPING (the launch config is in use)."""
        if self.state_name in (STARTING, STOPPING) or self._dialog_open():
            return
        pending = self._pending_launcher
        self.dialog = SettingsDialog(
            self, self.cfg, self.detector, on_save=self.save_launcher, on_closed=self._on_settings_closed,
            current_bat=pending.bat_path if pending is not None else None,
            running=self.state_name == ACTIVE, on_whitelist_saved=self._request_preview)

    def _on_settings_closed(self) -> None:
        if isinstance(self.dialog, SettingsDialog):   # only the dialog that closed
            self.dialog = None
        self._request_preview()

    def save_launcher(self, info: launcher_bat.LauncherInfo, done: Callable[[str | None], None]) -> None:
        """Persist the chosen launcher to config.json on a worker, then apply it (now, or at the next start)."""
        def work() -> None:
            err = None
            try:
                disk = config_store.load_config()         # re-read: keep whatever else changed on disk
                launcher_bat.apply_to_config(disk, info)
                config_store.save_config(disk)
            except Exception as exc:  # noqa: BLE001
                log.exception("saving the launcher failed")
                err = f"Save failed: {exc}"
            self.post(self._launcher_saved, info, err, done)

        self._run_worker("hc-save-launcher", work)

    def _launcher_saved(self, info: launcher_bat.LauncherInfo, err: str | None,
                        done: Callable[[str | None], None]) -> None:
        if err is None:
            if launcher_bat.same_launch(self.cfg, info):
                self._pending_launcher = None             # back to what is already loaded: nothing to apply
            else:
                self._pending_launcher = info
                self._apply_pending_launcher(note=True)
            log.info("launcher saved: %s (%s)", info.bat_path,
                     "applies next start" if self._pending_launcher is not None else "applied")
        done(err)
        if err is None and self.state_name == ACTIVE:
            self._apply_state()                           # shows / clears the "applies next start" line

    def _apply_pending_launcher(self, note: bool = False) -> bool:
        """Switch the shared config to the saved launcher - only while no Strata is using the old one.

        The endpoint manager reads port / endpoint_url / strata_dir live from this dict; changing them under a
        running server would make OFF look at the wrong port. So: IDLE/ERROR and nothing listening, else wait.
        """
        info = self._pending_launcher
        if info is None or self.state_name not in (IDLE, ERROR):
            return False
        try:
            # Wait only while STRATA holds the old port. A foreign program on it (ERROR "port held by X") must not
            # block the switch forever: the new launcher may well use another port. (Review finding, v1.1.)
            if self.endpoint.is_running() and self.endpoint.protected_pids():
                if note:                          # at save time; never over an ERROR message
                    self._set_note(f"{_short(info.bat_name, 28)} applies when Strata on port "
                                   f"{self.cfg.get('port', '?')} has stopped")
                return False
        except Exception:  # noqa: BLE001
            log.debug("is_running/protected_pids failed", exc_info=True)
            return False
        launcher_bat.apply_to_config(self.cfg, info)
        self._pending_launcher = None
        log.info("launcher applied: %s -> %s on port %s", info.bat_name, info.model_name, info.port)
        if note and self.state_name == IDLE:
            self._set_note(f"Launcher set to {_short(info.bat_name, 30)}\n{info.summary()}")
        return True

    def open_whitelist(self) -> None:
        """Protected apps directly (the Settings dialog has a button for it)."""
        if self.state_name in (STARTING, STOPPING) or self._dialog_open():
            return
        self.dialog = WhitelistModal(self, self.detector, on_saved=self._on_whitelist_saved)

    def _on_whitelist_saved(self) -> None:
        self.dialog = None
        self._request_preview()

    def on_close_request(self) -> None:
        if self._dialog_open():
            try:
                child = getattr(self.dialog, "_child", None)   # Protected apps opened from Settings holds the grab
                target = child if child is not None and child.winfo_exists() else self.dialog
                target.lift()
                target.focus_force()
            except Exception:
                log.debug("dialog lift failed", exc_info=True)
            return
        st = self.state_name
        if st == STOPPING:
            self._set_preview("Stopping — please wait before closing…")
            return
        if st == STARTING:
            if not messagebox.askyesno(APP_TITLE, "Cancel start and exit?", parent=self):
                return
            if self.state_name != STARTING:     # the start finished while the box was open: ask again
                return self.on_close_request()
            self._exit_pending = True
            if self._start_phase == "sweep":
                self._set_preview("Finishing app sweep, then exiting…")
                return
            if self._start_phase == "server":
                self.cancel_start()  # exit happens when on_stopped fires
                return
            self._finish_exit()
            return
        if st == ACTIVE:
            ans = messagebox.askyesnocancel(APP_TITLE, "Stop Strata before exit?", parent=self)
            if ans is None:
                return
            if self.state_name != ACTIVE:       # state changed under the modal box (monitor/pump kept running)
                return self.on_close_request()
            if ans:
                self._exit_pending = True
                self.turn_off()
                return
            self._finish_exit()
            return
        self._finish_exit()

    def _finish_exit(self) -> None:
        self._exit_pending = False
        try:
            self.detector.close()
        except Exception:
            log.exception("detector.close() failed")
        self.destroy()

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._stop_evt.set()
        try:
            self.vram.stop()
        except Exception:  # noqa: BLE001
            log.debug("vram sampler stop failed", exc_info=True)
        gc.collect()                             # finalize dialog leftovers here, before workers wind down
        if self._state_save_id is not None and self._state_pending:   # a click just before exit: save it now
            try:
                config_store.save_state(dict(self._state_pending))
            except Exception:  # noqa: BLE001
                log.debug("final state save failed", exc_info=True)
        for aid in (self._pump_id, self._tick_id, self._first_id, self._progress_id, self._stats_id, self._note_id,
                    self._state_save_id):
            if aid:
                try:
                    self.after_cancel(aid)
                except Exception:
                    log.debug("after_cancel failed", exc_info=True)
        try:  # also drop CustomTkinter's own pending after-scripts so none fire into a dead interpreter
            for aid in self.tk.splitlist(self.tk.call("after", "info")):
                self.after_cancel(aid)
        except Exception:
            log.debug("after info cleanup failed", exc_info=True)
        super().destroy()


def _work_area(win) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) work area of the monitor the window is on (physical px), or None."""
    class _Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long)]

    class _MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _Rect), ("rcWork", _Rect),
                    ("dwFlags", ctypes.c_ulong)]

    try:
        user32 = ctypes.WinDLL("user32")                         # private handle: prototypes stay local
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        mon = user32.MonitorFromWindow(hwnd, 2)                  # MONITOR_DEFAULTTONEAREST
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return None
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        log.debug("work area lookup failed", exc_info=True)
        return None


def _setup_logging() -> None:
    config_store.ensure_data_dir()
    handler = RotatingFileHandler(config_store.LOG_DIR / "controller.log", maxBytes=1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _check_config() -> None:
    """A corrupt config.json must not end in a raw traceback: move it aside, reseed, tell the user where.
    If it still cannot be read afterwards (locked / no permission), say so and exit instead of crashing."""
    try:
        config_store.load_config()
        return
    except (ValueError, OSError) as exc:             # ValueError includes JSONDecodeError / UnicodeDecodeError
        first = exc
    bad = config_store.CONFIG_PATH.with_name("config.bad.json")
    try:
        os.replace(config_store.CONFIG_PATH, bad)
        config_store.ensure_data_dir()
        config_store.load_config()
    except (ValueError, OSError):
        log.exception("config.json unreadable (%s) and could not be reset", first)
        ctypes.windll.user32.MessageBoxW(
            None, f"Your settings file could not be read or reset:\n{config_store.CONFIG_PATH}\n\n{first}",
            APP_TITLE, 0x10)
        sys.exit(1)
    log.error("config.json unreadable (%s); moved to %s", first, bad)
    ctypes.windll.user32.MessageBoxW(
        None, f"Your settings file could not be read and was reset.\n\nThe old file was saved as:\n{bad}",
        APP_TITLE, 0x30)


def main() -> None:
    global _SINGLETON_MUTEX
    import win32api
    import win32event
    import winerror

    _SINGLETON_MUTEX = win32event.CreateMutex(None, False, "HermesController.Singleton")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        user32 = ctypes.windll.user32
        # class + title: an Explorer window of a folder called "Hermes Controller" has the same title
        hwnd = user32.FindWindowW("TkTopLevel", APP_TITLE)
        if hwnd:                                     # bring the running window forward instead of a dialog
            user32.ShowWindow(hwnd, 9)                # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        else:
            user32.MessageBoxW(None, "Hermes Controller is already running.", APP_TITLE, 0x40)
        sys.exit(0)
    _setup_logging()
    _check_config()
    try:  # own taskbar identity/icon instead of grouping under python(w).exe
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("warrenjbg.HermesController")
    except Exception:  # noqa: BLE001
        pass
    log.info("Hermes Controller %s starting (frozen=%s, data=%s)", __version__, config_store.FROZEN,
             config_store.DATA_DIR)
    try:
        app = HermesControllerApp()
    except Exception:
        log.exception("startup failed")
        raise
    app.mainloop()
    log.info("Hermes Controller exited")


if __name__ == "__main__":
    main()
