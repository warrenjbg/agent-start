"""Hermes Controller — CustomTkinter widget that frees RAM and starts/stops the local Strata endpoint."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import queue
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from tkinter import messagebox
from typing import Any, Callable

import customtkinter as ctk
import psutil

import config_store
import load_progress
import theme as T
from sweep_dialog import SweepDialog
from whitelist_modal import WhitelistModal

log = logging.getLogger("hermes_controller")

APP_TITLE = "Hermes Controller"
WIDTH, HEIGHT = 360, 268

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
# Kept for tests/back-compat: the LED colour per state.
PILL_COLORS = {k: v[1] for k, v in STATUS.items()}
LOADING_COLOR = LOADING_STATUS[1]

# Toggle styles: (fg, hover, text colour). Primary = off-white; everything else is a quiet raised surface.
TOGGLE_ON = (T.TEXT, "#D9D9D6", T.ON_PRIMARY)
TOGGLE_OFF = (T.RAISED, "#303035", T.TEXT)
TOGGLE_CANCEL = (T.RAISED, "#303035", T.TEXT)
TOGGLE_WAIT = (T.SURFACE, T.SURFACE, T.TEXT_3)

BG = T.BG
MUTED = T.TEXT_2
TEXT = T.TEXT

PUMP_MS = 50
TICK_MS = 5000
PROGRESS_MS = 500
MEMORY_S = 2.0
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
        return "Nothing to close — memory is clear"
    if m > 0:
        line1 = f"{m} of {n} apps pre-selected to close · {_gb(res.get('preselected_ram_mb'))}"
    else:
        line1 = f"{n} apps could be closed · {_gb(res.get('ram_freed_mb'))} if all ticked"
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


def _build_real_deps(cfg: dict) -> tuple[Any, Any]:
    """Real EndpointManager + ProcessDetector over ONE shared config dict (imported lazily)."""
    from server_manager import EndpointManager
    from detector import ProcessDetector

    endpoint = EndpointManager(cfg)
    detector = ProcessDetector(cfg, protected_pids_provider=endpoint.protected_pids)
    return detector, endpoint


class HermesControllerApp(ctk.CTk):
    def __init__(self, detector=None, endpoint=None, config: dict | None = None) -> None:
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
        self._expected_s = float(config_store.load_state().get("last_cold_load_s") or 0.0)
        self._avail_at_launch: float | None = None
        self._launch_wall = 0.0                  # time.time() at launch: rejects a previous run's log
        self._strata_est: float | None = None    # latched at ready: RAM Strata took (free-RAM drop)
        self._prog_best: tuple[str, float, str | None] = ("Launching", 0.0, None)
        self._mem: tuple | None = None
        self._iconic = False
        self._start_note = ""
        self._sweep_note = ""
        self._error_log: str | None = None

        self._idle_streak = 0                   # consecutive "idle" status readings while ACTIVE
        self.report_callback_exception = (
            lambda exc, val, tb: log.error("Tk callback failed", exc_info=(exc, val, tb)))
        self.title(APP_TITLE)
        icon = config_store.APP_DIR / "assets" / "app.ico"
        if icon.exists():
            try:
                self.iconbitmap(default=str(icon))   # default= also applies to the dialogs
            except Exception:  # noqa: BLE001
                log.debug("iconbitmap failed", exc_info=True)
        self.geometry(f"{WIDTH}x{HEIGHT}")
        self.after(50, self._dark_caption)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.on_close_request)
        self._build_ui()
        self._apply_state()
        self._update_telemetry()

        self._pump_id = self.after(PUMP_MS, self._pump)
        self._tick_id = self.after(TICK_MS, self._tick)
        self._first_id = self.after(300, self._first_refresh)
        threading.Thread(target=self._monitor_loop, name="hc-monitor", daemon=True).start()
        threading.Thread(target=self._memory_loop, name="hc-memory", daemon=True).start()

    # ================================================================== layout
    def _font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=T.ui_family(), size=size, weight=weight)

    def _mono(self, size: int) -> ctk.CTkFont:
        return ctk.CTkFont(family=T.mono_family(), size=size)

    def _build_ui(self) -> None:
        """Front-panel layout on a 4 px grid: status line, detail, progress, power button, memory card."""
        self.configure(fg_color=T.BG)
        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent", height=28)
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        top.grid_columnconfigure(1, weight=1)
        self.led = ctk.CTkFrame(top, width=8, height=8, corner_radius=4, fg_color=T.LED["IDLE"])
        self.led.grid(row=0, column=0, padx=(0, 8))
        self.pill = ctk.CTkLabel(top, text="Idle", font=self._font(15, "bold"), text_color=T.TEXT,
                                 anchor="w", height=20)
        self.pill.grid(row=0, column=1, sticky="w")
        self.elapsed = ctk.CTkLabel(top, text="", font=self._mono(12), text_color=T.TEXT_2, height=20)
        self.elapsed.grid(row=0, column=2, padx=(0, 4))
        self.gear = ctk.CTkButton(top, text=T.GEAR_GLYPH, width=28, height=28, corner_radius=8,
                                  font=ctk.CTkFont(family=T.icon_family(), size=14),
                                  fg_color="transparent", hover_color=T.RAISED, text_color=T.TEXT_2,
                                  text_color_disabled=T.HAIRLINE, command=self.open_whitelist)
        self.gear.grid(row=0, column=3)

        self.preview = ctk.CTkLabel(self, text="Checking what is running…", font=self._font(12),
                                    text_color=T.TEXT_2, anchor="nw", justify="left", wraplength=320,
                                    height=36)
        self.preview.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 0))
        self.preview.bind("<Button-1>", self._on_preview_click)

        # Progress is always gridded (no layout jump); idle it is drawn in the track colour = invisible.
        self.progress = ctk.CTkProgressBar(self, height=4, corner_radius=2, fg_color=T.BG,
                                           progress_color=T.BG, border_width=0)
        self.progress.set(0)
        self.progress.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 0))

        self.toggle = ctk.CTkButton(self, text="Turn on", width=180, height=56, corner_radius=T.R_CARD,
                                    font=self._font(16, "bold"), command=self.on_toggle,
                                    text_color_disabled=T.TEXT_3)
        self.toggle.grid(row=3, column=0, pady=(14, 0))

        card = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=T.R_CARD)
        card.grid(row=4, column=0, sticky="ew", padx=16, pady=(16, 16))
        card.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        head.grid_columnconfigure(0, weight=1)
        self.mem_title = ctk.CTkLabel(head, text="Memory", font=self._font(12), text_color=T.TEXT_2,
                                      anchor="w", height=16)
        self.mem_title.grid(row=0, column=0, sticky="w")
        self.telemetry = ctk.CTkLabel(head, text="", font=self._mono(11), text_color=T.TEXT_3, anchor="e",
                                      height=16)
        self.telemetry.grid(row=0, column=1, sticky="e")

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

    def _style_toggle(self, text: str, colors: tuple[str, str, str], state: str) -> None:
        fg, hover, txt = colors
        self.toggle.configure(text=text, fg_color=fg, hover_color=hover, text_color=txt, state=state)

    def _apply_state(self) -> None:
        st = self.state_name
        title, led = STATUS[st]
        if st == ACTIVE and self.loaded is False:
            title, led = LOADING_STATUS
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
        self.progress.configure(progress_color=T.ACCENT if busy else T.BG, fg_color=T.SURFACE if busy else T.BG)
        if not busy:
            self.progress.set(0)
            self.elapsed.configure(text="")
        if st == ACTIVE:
            model = self.model_id or self.cfg.get("model_name") or "model"
            url = str(self.cfg.get("endpoint_url", "")).replace("http://", "")
            extra = " · still loading" if self.loaded is False else ""
            self._set_preview(f"{model}{extra}\n{url}")
        elif st == ERROR and self.error_text:
            self._set_preview(self.error_text, color=T.RED_TEXT)
        self.preview.configure(cursor="hand2" if st == ERROR and self._error_log else "")

    def _dark_caption(self) -> None:
        T.dark_caption(self)

    def _on_preview_click(self, _event=None) -> None:
        if self.state_name == ERROR and self._error_log and os.path.exists(self._error_log):
            try:
                os.startfile(self._error_log)          # opens in the default .log/.txt viewer
            except OSError:
                log.exception("could not open %s", self._error_log)

    def _set_preview(self, text: str, color: str = T.TEXT_2) -> None:
        self.preview.configure(text=text, text_color=color)

    def _set_state(self, st: str) -> None:
        self.state_name = st
        if st != ERROR:
            self._error_hold_until = 0.0
        if st in (IDLE, ERROR):                  # no Strata of ours any more: drop memory baselines
            self._avail_at_launch = None
            self._strata_est = None
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
        self.progress.set(frac)
        self.elapsed.configure(text=load_progress.fmt_elapsed(elapsed))
        line2 = self._start_note or (
            f"usually about {load_progress.fmt_elapsed(self._expected_s)}" if self._expected_s else "")
        self._set_preview(f"{label}{' · ' + detail if detail else ''}" + (f"\n{line2}" if line2 else ""))
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
                status = self.endpoint.status()
                self.post(self.on_status, status)
            except Exception:
                log.exception("endpoint.status() failed")
            self._stop_evt.wait(TICK_MS / 1000.0)

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
                self._set_state(ACTIVE)
        elif self.state_name == ACTIVE and live == "idle":
            self._idle_streak += 1
            if self._idle_streak < 2:           # one stray reading (probe race) must not flip the UI
                return
            self._idle_streak = 0
            self.model_id, self.loaded = None, None
            self._set_state(IDLE)
            self._set_preview("Endpoint is no longer running")
            self._request_preview()

    def _reconcile_now(self, note: str | None = None) -> None:
        """start/stop_endpoint returned False: show IDLE, then reconcile from a status() fetched on a worker."""
        self._start_phase = None
        self._set_state(IDLE)
        if note:
            self._set_preview(note)

        def work() -> None:
            try:
                st = self.endpoint.status()
            except Exception:
                log.exception("endpoint.status() failed")
                return
            self.post(self._after_reconcile, st)

        self._run_worker("hc-reconcile", work)

    def _after_reconcile(self, status: dict) -> None:
        live = (status or {}).get("state")
        if live in ("starting", "stopping") and self.state_name == IDLE:
            self._set_preview(f"Endpoint is busy ({live}) — try again shortly")
        self.on_status(status)
        if self._exit_pending and self.state_name in (IDLE, ERROR):
            self._finish_exit()

    # ================================================================== periodic
    def _first_refresh(self) -> None:
        self._first_id = None
        self._request_preview()

    def _tick(self) -> None:
        try:
            try:
                self._iconic = self.state() == "iconic"
            except Exception:  # noqa: BLE001
                self._iconic = False
            self._update_telemetry()
            self._request_preview()
        finally:
            if not self._destroyed:
                self._tick_id = self.after(TICK_MS, self._tick)

    def _request_preview(self) -> None:
        # ERROR keeps its message (and log link) until the user acts; only IDLE shows the sweep preview
        if self.state_name != IDLE or self._preview_busy:
            return
        if time.monotonic() < self._error_hold_until:
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
        if self.state_name != IDLE:
            return
        if "_error" in res:
            self._set_preview(f"Scan failed: {res['_error']}")
        else:
            self._set_preview(format_preview(res))

    # ================================================================== ON flow
    def on_toggle(self) -> None:
        st = self.state_name
        if st in (IDLE, ERROR):
            self.turn_on()
        elif st == ACTIVE:
            self.turn_off()
        elif st == STARTING:
            self.cancel_start()

    def turn_on(self) -> None:
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
            self.post(self._after_scan, token, cands)

        self._run_worker("hc-scan", work)

    def _token_live(self, token: int) -> bool:
        return token == self._start_token and self.state_name == STARTING

    def _after_scan(self, token: int, cands: list[dict]) -> None:
        if not self._token_live(token):
            return
        if not cands:
            self._begin_server_start(token)
            return
        self._start_phase = "dialog"
        self._set_preview(f"{len(cands)} app(s) could be closed — choose in the dialog")
        self.dialog = SweepDialog(self, cands, lambda d, t: self._on_sweep_decision(token, d, t))

    def _on_sweep_decision(self, token: int, decision: str, targets: list[tuple[int, float]]) -> None:
        self.dialog = None
        if not self._token_live(token):
            return
        if decision == "cancel":
            self._start_phase = None
            self._set_state(IDLE)
            self._set_preview("Start cancelled")
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
        failed = res.get("failed") or []
        if failed:
            log.warning("sweep could not close: %s", failed)
        self._sweep_note = ""
        if failed or res.get("error"):
            names = ", ".join(_short(str(f.get("name", "?")), 18) for f in failed[:2]) or "some apps"
            if len(failed) > 2:
                names += f" +{len(failed) - 2}"
            self._sweep_note = f"Could not close: {names} — starting anyway"
        if self._exit_pending and token == self._start_token and self._start_phase == "sweep":
            self._start_phase = None
            self._set_state(IDLE)
            self._finish_exit()
            return
        if not self._token_live(token):
            return
        self._begin_server_start(token)

    def _begin_server_start(self, token: int) -> None:
        self._start_phase = "server"
        self._start_note = self._sweep_note
        self._sweep_note = ""
        self._server_t0 = time.monotonic()
        self._launch_wall = time.time()
        self._log_text = ""
        self._prog_best = ("Launching", 0.0, None)
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
        if elapsed > 15:                         # a real cold load (adopting a running server is ~instant)
            old = self._expected_s
            self._expected_s = elapsed if not old else max(elapsed, 0.5 * old + 0.5 * elapsed)
            config_store.save_state({"last_cold_load_s": round(self._expected_s, 1)})
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
            msg = msg[: m.start()] + f" · open {os.path.basename(m.group(1))}"
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
            self._set_preview("Start cancelled")
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
            self._set_preview("Start cancelled")
            self._request_preview()
        elif result.get("port_released"):
            self.model_id, self.loaded = None, None
            self._avail_at_launch = None
            self.add_freed(result.get("ram_freed_mb"))
            self._set_state(IDLE)
            self._set_preview(f"Stopped · freed {_gb(result.get('ram_freed_mb'))}")
            self._request_preview()
        else:
            self.show_error(result.get("error") or "Stop failed — port still in use")
            return
        if self._exit_pending:
            self._finish_exit()

    # ================================================================== dialogs / exit
    def open_whitelist(self) -> None:
        if self.state_name in (STARTING, STOPPING) or self._dialog_open():
            return
        self.dialog = WhitelistModal(self, self.detector, on_saved=self._on_whitelist_saved)

    def _on_whitelist_saved(self) -> None:
        self.dialog = None
        self._request_preview()

    def on_close_request(self) -> None:
        if self._dialog_open():
            try:
                self.dialog.lift()
                self.dialog.focus_force()
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
        for aid in (self._pump_id, self._tick_id, self._first_id, self._progress_id):
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


def _setup_logging() -> None:
    config_store.ensure_data_dir()
    handler = RotatingFileHandler(config_store.LOG_DIR / "controller.log", maxBytes=1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _check_config() -> None:
    """A corrupt config.json must not end in a raw traceback: move it aside, reseed, tell the user where."""
    try:
        config_store.load_config()
    except ValueError as exc:                        # includes json.JSONDecodeError
        bad = config_store.CONFIG_PATH.with_name("config.bad.json")
        try:
            os.replace(config_store.CONFIG_PATH, bad)
        except OSError:
            log.exception("could not move the broken config aside")
        config_store.ensure_data_dir()
        log.error("config.json unreadable (%s); moved to %s", exc, bad)
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
        hwnd = user32.FindWindowW(None, APP_TITLE)
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
    log.info("Hermes Controller starting (frozen=%s, data=%s)", config_store.FROZEN, config_store.DATA_DIR)
    try:
        app = HermesControllerApp()
    except Exception:
        log.exception("startup failed")
        raise
    app.mainloop()
    log.info("Hermes Controller exited")


if __name__ == "__main__":
    main()
