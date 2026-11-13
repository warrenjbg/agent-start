"""The main window's drawing code, moved verbatim out of main.AgentStartApp (class AppView, a mixin).

AppView builds the window (_build_ui) and redraws it: the status line and its detail line, the power button, the
activity bar, the stats panels and their pins, the window's height and position. Its methods are the old
AgentStartApp methods, character for character; main.AgentStartApp inherits them (class AgentStartApp(AppView,
ctk.CTk)), so `self` is always the app. The on/off logic, the workers and the wiring stay in main.py.

The constants below are the ones only this drawing code reads, with the sizes and colours derived from them that
the tests read (SETUP_H, PENDING_H, PILL_COLORS, LOADING_COLOR); main.py re-exports them under their old names.
Names that main.py's own code also reads (the layout sizes, the panels, the state names, STATS_MS) are in
app_consts.py.
"""
from __future__ import annotations

import ctypes
import logging
import time
import tkinter
from typing import Any

import customtkinter as ctk
from customtkinter.windows.widgets.core_widget_classes import DropdownMenu

import theme as T
from app_consts import (ACTIVE, ERROR, HEIGHT_EXPANDED, IDLE, PANELS, STARTING, STATS_GAP, STATS_MS, STOPPING,
                        WIDTH)
from graphs import BarGraph, LineGraph, StatRow, fmt_rate
from text_util import short as _short

log = logging.getLogger("agent_start")


PIN_GLYPH, PINNED_GLYPH = "\uE718", "\uE842"    # Segoe Fluent Icons "Pin" (outline) / "PinnedFill"

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
# Compact header (v1.1): status line + optional one-line detail. Spacing is measured from the rendered text so the
# detail line sits 10 px from the status text above and 10 px from the button / progress bar below.
DETAIL_INDENT = 32                       # window margin 16 + LED 8 + gap 8: the detail aligns with the state text
DETAIL_WRAP = 360 - DETAIL_INDENT - 16
GAP_HEAD_BUTTON = 12                     # status line -> button when no detail is shown
GAP_DETAIL_TOP = 4                       # status row (28 px, title centred) -> detail line
GAP_AFTER_DETAIL = 10                    # detail line -> button / progress bar
SETUP_CARD_GAP = 12                      # status line -> the set-up card (nothing configured, an install was found)
SETUP_CARD_H = 52
SETUP_HINT_GAP = 8                       # button -> "or open Settings…"
SETUP_H = SETUP_CARD_GAP + SETUP_CARD_H + (GAP_AFTER_DETAIL - GAP_HEAD_BUTTON) + SETUP_HINT_GAP + 16
PENDING_GAP = 7                          # button -> the pending-stop line under it
PENDING_H = PENDING_GAP + 16             # added while that line shows
CARD_TOP = 14                            # button -> first panel
BAR_H = 14                               # the expand bar at the bottom
BAR_PAD = 5                              # above and below the bar
SWITCH_GLYPH = "\uE8AB"                  # Segoe Fluent Icons "Switch"
ACT_BAR_X = 22                           # activity bar under the status line: starts at the state text...
ACT_BAR_RIGHT = 36                       # ...and ends before the gear (logical px)
ACT_STATUS = {"reading": ("Reading", T.BLUE), "generating": ("Generating", T.ACCENT), "queued": ("Queued", T.AMBER)}
# Kept for tests/back-compat: the LED colour per state.
PILL_COLORS = {k: v[1] for k, v in STATUS.items()}
LOADING_COLOR = LOADING_STATUS[1]

# Toggle styles: (fg, hover, text colour). Primary = off-white; everything else is a quiet raised surface.
TOGGLE_ON = (T.TEXT, T.PRIMARY_HOVER, T.ON_PRIMARY)
TOGGLE_OFF = (T.RAISED, T.RAISED_HOVER, T.TEXT)
TOGGLE_CANCEL = (T.RAISED, T.RAISED_HOVER, T.TEXT)
TOGGLE_WAIT = (T.SURFACE, T.SURFACE, T.TEXT_3)

METRICS_FAILS_SHOWN = 3                  # consecutive failed polls before "Metrics unavailable"


class AppView:
    """The main window's drawing code, moved verbatim out of main.AgentStartApp.

    A mixin: it only works as a base of AgentStartApp (class AgentStartApp(AppView, ctk.CTk)). It has no state and
    no __init__ of its own; every attribute it uses is created in AgentStartApp.__init__ or in _build_ui.
    """

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
        self.gear.grid(row=0, column=6)
        self.switch_btn = ctk.CTkButton(top, text=SWITCH_GLYPH, width=28, height=28, corner_radius=8,
                                        font=ctk.CTkFont(family=T.icon_family(), size=13),
                                        fg_color="transparent", hover_color=T.RAISED, text_color=T.TEXT_2,
                                        text_color_disabled=T.HAIRLINE, command=self.open_model_menu)
        self._model_menu = DropdownMenu(self, values=[], command=self._on_model_menu, fg_color=T.SURFACE,
                                        hover_color=T.RAISED, text_color=T.TEXT, font=self._font(12))
        # 2 px activity bar along the bottom of the status row (prompt read progress): no extra height
        self.act_track = tkinter.Frame(top, bg=T.HAIRLINE, height=2, highlightthickness=0, bd=0)
        self.act_fill = tkinter.Frame(self.act_track, bg=T.BLUE, highlightthickness=0, bd=0)
        self._act_frac: float | None = None
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

        # The button, and under it (centred) the pending-stop line: "Turns off after this reply · Cancel".
        self.btn_box = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_box.grid(row=3, column=0, pady=(GAP_HEAD_BUTTON, 0))
        self.toggle = ctk.CTkButton(self.btn_box, text="Turn on", width=180, height=56, corner_radius=T.R_CARD,
                                    font=self._font(16, "bold"), command=self.on_toggle,
                                    text_color_disabled=T.TEXT_3)
        self.toggle.pack()
        # set-up card (nothing configured yet, the engine found an install): what was found, centred; the main
        # button below it is the one action ("Choose model"), with a pointer to Settings under it
        self.setup_card = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=T.R_CARD, height=SETUP_CARD_H)
        self.setup_card.grid_propagate(False)
        self.setup_card.grid_columnconfigure(0, weight=1)
        self.setup_card.grid_rowconfigure((0, 3), weight=1)
        self._setup_head = ctk.CTkFrame(self.setup_card, fg_color="transparent")
        self._setup_head.grid(row=1, column=0)
        self.setup_title = ctk.CTkLabel(self._setup_head, text="", font=self._font(13, "bold"), text_color=T.TEXT,
                                        height=18)
        self.setup_title.pack(side="left")
        self.setup_tag = ctk.CTkLabel(self._setup_head, text="", font=self._font(10), text_color=T.TEXT_2, height=16,
                                      fg_color=T.RAISED, corner_radius=6)
        self.setup_line = ctk.CTkLabel(self.setup_card, text="", font=self._detail_font, text_color=T.TEXT_2,
                                       height=16)
        self.setup_line.grid(row=2, column=0)
        self.setup_hint = ctk.CTkLabel(self.btn_box, text="or open Settings to set it up by hand",
                                       font=self._detail_font, text_color=T.TEXT_2, height=16)
        self._setup_shown = False
        self._launch_infos: dict[str, Any] = {}  # menu key -> launcher info (engines without launcher files)
        self.pending_box = ctk.CTkFrame(self.btn_box, fg_color="transparent")
        self.pending_text = ctk.CTkLabel(self.pending_box, text="", font=self._detail_font, text_color=T.TEXT_2,
                                         height=16)
        self.pending_text.pack(side="left")
        self.pending_cancel = ctk.CTkLabel(self.pending_box, text="Cancel", font=self._detail_link_font,
                                           text_color=T.TEXT, height=16, cursor="hand2")
        self.pending_cancel.pack(side="left")
        self.pending_cancel.bind("<Button-1>", lambda _e: self.cancel_pending_stop())
        self._pending_shown = False

        # Two cards: pinned panels (always shown) and the rest (shown while expanded). The panels are children of
        # the window, gridded into whichever card they belong to (grid in_=), so a pin moves them between cards.
        # The cards are created first so the panels stack above them.
        self.pin_card = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=T.R_CARD)
        self.more_card = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=T.R_CARD)
        for c in (self.pin_card, self.more_card):
            c.grid_columnconfigure(0, weight=1)
        card = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=0)
        card.grid_columnconfigure(0, weight=1)
        self.mem_card = card                     # the Memory panel
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(10, 6))     # the card's 12 px inset comes from the panel
        head.grid_columnconfigure(0, weight=1)
        self.mem_title = ctk.CTkLabel(head, text="Memory", font=self._font(12), text_color=T.TEXT_2,
                                      anchor="w", height=16)
        self.mem_title.grid(row=0, column=0, sticky="w")
        self.telemetry = ctk.CTkLabel(head, text="", font=self._mono(11), text_color=T.TEXT_3, anchor="e",
                                      height=16)
        self.telemetry.grid(row=0, column=1, sticky="e")
        self._add_pin(head, "memory", column=2)

        self.gauge = ctk.CTkFrame(card, height=8, corner_radius=4, fg_color=T.RAISED)
        self.gauge.grid(row=1, column=0, sticky="ew")
        self.gauge.grid_propagate(False)
        self._seg_apps = ctk.CTkFrame(self.gauge, height=8, corner_radius=4, fg_color=T.APPS_SEG)
        self._seg_strata = ctk.CTkFrame(self.gauge, height=8, corner_radius=4, fg_color=T.ACCENT)

        legend = ctk.CTkFrame(card, fg_color="transparent")
        legend.grid(row=2, column=0, sticky="ew", pady=(8, 10))
        self.legend = {}
        for col, (key, color, label) in enumerate((("apps", T.APPS_SEG, "Apps"), ("strata", T.ACCENT, self.engine.name),
                                                   ("free", T.RAISED, "Free"))):
            cell = ctk.CTkFrame(legend, fg_color="transparent")
            cell.grid(row=0, column=col, sticky="w", padx=(0, 14))
            ctk.CTkFrame(cell, width=8, height=8, corner_radius=2, fg_color=color).grid(row=0, column=0,
                                                                                        padx=(0, 6))
            lbl = ctk.CTkLabel(cell, text=f"{label} –", font=self._mono(11), text_color=T.TEXT_2, height=14)
            lbl.grid(row=0, column=1)
            self.legend[key] = (lbl, label)

        # Graph panels
        self.row_vram = StatRow(self, "VRAM · 5 min", lambda m: LineGraph(m, window_s=self.vram.window_s),
                                fg_color=T.SURFACE)
        self.row_decode = StatRow(self, "Decode · per request", BarGraph, fg_color=T.SURFACE)
        self.row_prompt = StatRow(self, "Prompt reading · per request", BarGraph, fg_color=T.SURFACE)
        self._rows = {"vram": self.row_vram, "decode": self.row_decode, "prompt": self.row_prompt}
        # hairlines between panels: up to 3 per card
        self._hairs = {c: [ctk.CTkFrame(self, height=1, fg_color=T.HAIRLINE, bg_color=T.HAIRLINE, corner_radius=0)
                           for _ in range(len(PANELS) - 1)] for c in ("pin", "more")}
        self.stats = self.more_card              # the card that the expand bar opens
        for key, row in self._rows.items():
            self._add_pin(row.head_frame(), key, column=3)
        self._panel_frames = {"memory": self.mem_card, **self._rows}
        for key, frame in self._panel_frames.items():
            self._bind_hover(frame, key)

        self._bottom_pad = ctk.CTkFrame(self, fg_color="transparent", height=1, width=1)
        # Bottom bar: shows / hides the panels that are not pinned (hidden when everything is pinned).
        self.chevron = ctk.CTkButton(self, text=T.CHEVRON_DOWN, height=BAR_H, corner_radius=6,
                                     font=ctk.CTkFont(family=T.icon_family(), size=9),
                                     fg_color="transparent", hover_color=T.SURFACE, text_color=T.TEXT_3,
                                     command=self.toggle_stats)

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
        self._sync_setup(st)
        if st in (IDLE, ERROR):
            self._style_toggle(self.engine.setup_action if self._setup_shown else "Turn on", TOGGLE_ON, "normal")
        elif st == ACTIVE:
            label = "Turn off"
            if self._stop_pending is not None:
                label = "Switch now" if self._stop_pending[0] == "switch" else "Stop now"
            self._style_toggle(label, TOGGLE_OFF, "normal")
        elif st == STARTING:
            self._style_toggle("Cancel", TOGGLE_CANCEL, "normal")
        else:
            self._style_toggle("Stopping…", TOGGLE_WAIT, "disabled")
        self.gear.configure(state="disabled" if st in (STARTING, STOPPING) else "normal")
        self.switch_btn.configure(state="disabled" if st in (STARTING, STOPPING) else "normal")
        busy = st == STARTING and self._start_phase == "server"
        if not busy:
            self.progress.set(0)
            self.elapsed.configure(text="")
        self._track_span()
        if st != ACTIVE:
            self.metrics.live = {}                       # a stale "generating" must not survive OFF / a restart
            self._set_act_bar(None)
        if st != ACTIVE or self._stop_pending is None:
            self._set_pending_line(None)
        if st != getattr(self, "_launchers_state", None) and st in (IDLE, ACTIVE):
            self._launchers_state = st
            self._refresh_launchers()            # the ✓ / tray submenu follow a switch made while serving
        # watch from Turn on (a game started during the load alerts once Strata is ready)
        self.watch.arm_games(st in (STARTING, ACTIVE))
        self.watch.arm_memory(st == ACTIVE and self.loaded is True)
        if st == ACTIVE and self.loaded is not False and self.watch.has_queued():
            self.after_idle(self.watch.deliver_queued)
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
        self._sync_tray()

    def _show_serving(self) -> None:
        """ACTIVE: the status line shows what the model is doing (Strata's Monitor "Model state": Reading with the
        prompt progress bar, Generating with tokens + tok/s, Queued), else Serving + the model. The detail line
        carries a warning that must stay visible (Low-RAM, pending launcher, still loading), else uptime + last reply
        speed, or during a request the model + speed / phase."""
        model = self.model_id or self.cfg.get("model_name") or "model"
        title, led = STATUS[ACTIVE]
        if self.loaded is False:
            title, led = LOADING_STATUS
        elif self._is_low_ram():
            title, led = LOW_RAM_STATUS
        act = self.metrics.activity() if self.loaded is not False else None
        summary, detail, color, frac = model, self._serving_line(), T.TEXT_2, None
        if act is None and self._tool_wait is not None:
            summary = f"Agent running {_short(self._tool_wait[0], 20)}"
        if act is not None:
            title, led = ACT_STATUS[act["kind"]]
            summary, detail, frac = self._activity_texts(act, model)
            self.pill.configure(text=title)              # first: the summary's room depends on the state's width
            q = f" · {act['queued']} queued"
            if act["queued"] and summary.endswith(q) and not self._fits_summary(summary):
                summary, detail = summary[: -len(q)], detail + q     # keep the numbers whole; queue goes below
        link = False
        if self._stop_pending is not None:
            kind, info = self._stop_pending
            what = "Turns off" if kind == "off" else "Switches model"     # the model is named in the dialog
            if self._tool_wait is not None and self.metrics.activity() is None:
                self._set_pending_line(f"{what} after the agent's reply · ")    # the status names the tool
            else:
                self._set_pending_line(f"{what} after this reply · ")
        else:
            self._set_pending_line(None)
        if self.loaded is False:
            detail = "Still loading the model"
        elif self._pending_launcher is not None:      # a launcher saved while running waits for the next start
            detail = f"{_short(self._pending_launcher.file_name, 28)} applies next start"
        elif self._is_low_ram():
            detail, color = self._low_ram_line(), T.AMBER
            if act is None:
                summary = self.engine.low_ram_label
        elif self._gpu_fit_slow():
            detail, color = self.engine.gpu_fit_serving, T.AMBER
        self.pill.configure(text=title)
        self.led.configure(fg_color=led)
        self._set_act_bar(frac)
        self._set_status(summary, detail, color=color, link=link)
        self._sync_tray()

    @staticmethod
    def _activity_texts(act: dict, model: str) -> tuple[str, str, float | None]:
        """(status-line summary, detail line, bar fraction) for one metrics_store.activity() dict."""
        q = f" · {act['queued']} queued" if act["queued"] else ""
        rate = act.get("rate")
        if act["kind"] == "reading":
            read, total = act.get("read"), act.get("total")
            if total and read is not None:
                summary = f"{100 * act['frac']:.0f}% · {read:,.0f} / {total:,.0f}"
            elif act.get("tokens"):
                summary = f"{act['tokens']:,.0f} tokens"
            else:
                summary = "prompt"
            return summary + q, model + (f" · reading {rate:,.0f} tok/s" if rate else ""), act.get("frac") or 0.0
        if act["kind"] == "generating":
            toks = act.get("tokens")
            parts = ([f"{toks:,.0f} tokens"] if toks is not None else []) + ([f"{rate:.1f} tok/s"] if rate else [])
            phase = act.get("phase")
            return (" · ".join(parts) or "…") + q, model + (f" · {phase}" if phase else ""), None
        return f"{act['queued']} waiting", model, None

    def _set_act_bar(self, frac: float | None) -> None:
        """The 2 px bar along the bottom of the status row (None hides it)."""
        if frac is None:
            if self._act_frac is not None:
                self.act_track.place_forget()
                self._act_frac = None
            return
        frac = min(1.0, max(0.0, float(frac)))
        if self._act_frac is None:
            sc = ctk.ScalingTracker.get_widget_scaling(self)
            self.act_track.configure(height=max(1, round(2 * sc)))
            self.act_track.place(x=round(ACT_BAR_X * sc), rely=1.0, anchor="sw", relwidth=1.0,
                                 width=-round((ACT_BAR_X + ACT_BAR_RIGHT) * sc))
        if self._act_frac != frac:
            self.act_fill.place(x=0, y=0, relheight=1.0, relwidth=frac)
        self._act_frac = frac

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

    def _gpu_fit_slow(self) -> bool:
        """Serving, and the model does not (or will not) fully fit on the GPU."""
        return self.state_name == ACTIVE and self.loaded is not False and (self._gpu_fit_pre or self._gpu_fit_live)

    def _sync_setup(self, st: str | None = None) -> bool:
        """The set-up card shows while idle with nothing configured and an install the engine found."""
        st = st or self.state_name
        found = self._found_strata
        want = bool(st == IDLE and found is not None and self._pending_launcher is None
                    and not self.engine.is_configured(self.cfg))
        if want:
            try:
                tag, line = self.engine.found_summary(found)
            except Exception:  # noqa: BLE001
                tag, line = "", ""
            self.setup_title.configure(text=f"{self.engine.name} found")
            self.setup_line.configure(text=_short(line, 48))
            if tag:
                self.setup_tag.configure(text=f" {tag} ")
                if not self.setup_tag.winfo_manager():
                    self.setup_tag.pack(side="left", padx=(8, 0))
            elif self.setup_tag.winfo_manager():
                self.setup_tag.pack_forget()
        changed = want != self._setup_shown
        self._setup_shown = want
        return changed

    def _is_low_ram(self) -> bool:
        return (self.state_name == ACTIVE and self._engine_load is not None
                and self._engine_load.mode == "low_ram" and self.loaded is not False)

    # ------------------------------------------------------------------ status line + detail line
    def _fits_summary(self, text: str) -> bool:
        return self._summary_font.measure(text) <= self._summary_budget()

    def _summary_budget(self) -> int:
        """Logical px left for the summary on the status line. CTkFont.measure is unscaled, like the layout."""
        used = self.pill.cget("font").measure(self.pill.cget("text"))
        # row margins 10+12, LED 8 + pads 6/8, summary pad 8, caret 12 + pad 4, timer pads 6+4, gear 28, 2 spare
        fixed = WIDTH - 22 - 22 - 8 - 16 - 10 - 28 - 2
        if self.switch_btn.winfo_manager():
            fixed -= 28 + 2
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
        return st == ACTIVE and (self.loaded is False or self._pending_launcher is not None or self._is_low_ram()
                                 or self._gpu_fit_slow())

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
        setup = self._setup_shown
        key = (show, busy, self._pending_shown, setup)
        if key == self._layout_key:
            return
        self._layout_key = key
        if setup:
            self.setup_card.grid(row=2, column=0, sticky="ew", padx=16,
                                 pady=(GAP_AFTER_DETAIL if show else SETUP_CARD_GAP, 0))
            self.setup_hint.pack(pady=(SETUP_HINT_GAP, 0))
        else:
            self.setup_card.grid_remove()
            self.setup_hint.pack_forget()
        if show:
            self.preview.grid(row=1, column=0, sticky="ew", padx=(DETAIL_INDENT, 16), pady=(GAP_DETAIL_TOP, 0))
        else:
            self.preview.grid_remove()
        if busy:
            self.progress.grid(row=2, column=0, sticky="ew", padx=16,
                               pady=(GAP_AFTER_DETAIL if show else GAP_HEAD_BUTTON, 0))
        else:
            self.progress.grid_remove()
        self.btn_box.grid(row=3, column=0,
                          pady=(GAP_AFTER_DETAIL if (show or busy or setup) else GAP_HEAD_BUTTON, 0))
        if self._pending_shown:
            self.pending_box.pack(pady=(PENDING_GAP, 0))
        else:
            self.pending_box.pack_forget()
        self._fit_height()

    def _set_pending_line(self, text: str | None) -> None:
        """The line under the button while a stop / switch waits for the reply: '<text>Cancel' (only Cancel clicks)."""
        if text is None:
            if self._pending_shown:
                self._pending_shown = False
                self._layout()
            return
        if self.pending_text.cget("text") != text:
            self.pending_text.configure(text=text)
        if not self._pending_shown:
            self._pending_shown = True
            self._layout()

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

    # ================================================================== stats section
    def toggle_stats(self) -> None:
        self.expanded = not self.expanded
        self._apply_expanded()
        self._save_state_later("stats_expanded", self.expanded)

    def _visible_panels(self) -> list[str]:
        return list(PANELS) if self.expanded else [p for p in PANELS if p in self.pinned]

    def _graphs_shown(self) -> bool:
        return any(p in self._visible_panels() for p in ("vram", "decode", "prompt"))

    def _apply_expanded(self) -> None:
        """Lay out the panels: pinned ones in the first card (always shown), the others in the second card (shown
        while expanded), each in PANELS order and split by hairlines; the bottom bar toggles the second card.
        The window grows / shrinks downward to fit."""
        everything_pinned = all(p in self.pinned for p in PANELS)
        self.chevron.configure(text=T.CHEVRON_UP if self.expanded else T.CHEVRON_DOWN)
        frames = self._panel_frames
        for f in frames.values():
            f.grid_remove()
        for hs in self._hairs.values():
            for h in hs:
                h.grid_remove()
        groups = (("pin", self.pin_card, [p for p in PANELS if p in self.pinned]),
                  ("more", self.more_card, [p for p in PANELS if p not in self.pinned] if self.expanded else []))
        top = CARD_TOP
        for row_no, (name, card, keys) in enumerate(groups, start=4):
            if not keys:
                card.grid_remove()
                continue
            for i, k in enumerate(keys):
                if i:
                    self._hairs[name][i - 1].grid(in_=card, row=2 * i - 1, column=0, sticky="ew", padx=12)
                frames[k].grid(in_=card, row=2 * i, column=0, sticky="ew", padx=12)
                frames[k].lift()
            card.grid(row=row_no, column=0, sticky="ew", padx=16, pady=(top, 0))   # .grid() is DPI-scaled
            top = STATS_GAP
        for hs in self._hairs.values():
            for h in hs:
                h.lift()
        if everything_pinned:
            self.chevron.grid_remove()
            self._bottom_pad.grid(row=6, column=0, pady=(0, 16 - 1))
        else:
            self._bottom_pad.grid_remove()
            self.chevron.grid(row=6, column=0, sticky="ew", padx=16, pady=(BAR_PAD, BAR_PAD))
        for key in PANELS:
            self._style_pin(key)
        self._fit_height(force=True)
        if self.expanded:
            self._keep_on_screen()
        if self._graphs_shown():
            self._refresh_stats()
            if self._stats_id is None and not self._destroyed:
                self._stats_id = self.after(STATS_MS, self._stats_tick)
        elif self._stats_id is not None:
            try:
                self.after_cancel(self._stats_id)
            except Exception:  # noqa: BLE001
                log.debug("after_cancel failed", exc_info=True)
            self._stats_id = None

    # ------------------------------------------------------------------ pins
    def _add_pin(self, parent, key: str, column: int) -> None:
        btn = ctk.CTkButton(parent, text="", width=16, height=16, corner_radius=4,
                            font=ctk.CTkFont(family=T.icon_family(), size=10), fg_color="transparent",
                            hover_color=T.RAISED, text_color=T.TEXT_3, command=lambda k=key: self.toggle_pin(k))
        btn.grid(row=0, column=column, padx=(6, 0))
        self._pin_btns[key] = btn

    def _style_pin(self, key: str) -> None:
        """Pinned: a filled pin, always shown. Not pinned: an outline pin, only while the pointer is on the panel."""
        btn = self._pin_btns.get(key)
        if btn is None:
            return
        if key in self.pinned:
            btn.configure(text=PINNED_GLYPH, text_color=T.TEXT_2)
        elif self._panel_hover == key:
            btn.configure(text=PIN_GLYPH, text_color=T.TEXT_3)
        else:
            btn.configure(text="")

    def toggle_pin(self, key: str) -> None:
        if key in self.pinned:
            self.pinned.remove(key)
        else:
            self.pinned = [p for p in PANELS if p in self.pinned or p == key]
        self._save_state_later("pinned", list(self.pinned))
        self._apply_expanded()

    def _bind_hover(self, frame, key: str) -> None:
        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)
        for w in walk(frame):
            w.bind("<Enter>", lambda _e, k=key: self._set_panel_hover(k), add="+")
            w.bind("<Leave>", lambda _e, k=key: self.after(40, self._check_panel_leave, k), add="+")

    def _set_panel_hover(self, key: str | None) -> None:
        if self._panel_hover != key:
            old, self._panel_hover = self._panel_hover, key
            for k in {old, key} - {None}:
                self._style_pin(k)

    def _check_panel_leave(self, key: str) -> None:
        """<Leave> also fires when the pointer moves onto a child widget: hide only once it is really outside."""
        if self._destroyed or self._panel_hover != key:
            return
        frame = self._panel_frames[key]
        try:
            x, y = self.winfo_pointerxy()
            fx, fy = frame.winfo_rootx(), frame.winfo_rooty()
            inside = frame.winfo_ismapped() and fx <= x < fx + frame.winfo_width() and fy <= y < fy + frame.winfo_height()
        except Exception:  # noqa: BLE001
            inside = False
        if not inside:
            self._set_panel_hover(None)

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
        if self._destroyed or not self._graphs_shown():
            return
        try:
            if not self._iconic:
                self._refresh_stats()
        finally:
            if not self._destroyed and self._graphs_shown():
                self._stats_id = self.after(STATS_MS, self._stats_tick)

    def _refresh_stats(self) -> None:
        """Main thread: redraw the three graphs from the sampler / metrics store."""
        if not self._graphs_shown():
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
            empty_d = empty_p = f"{self.engine.name} is off"
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
