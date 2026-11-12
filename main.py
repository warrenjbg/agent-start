"""Agent Start — CustomTkinter widget that frees RAM and starts/stops the local Strata endpoint."""
from __future__ import annotations

import ctypes
import dataclasses
import gc
import logging
import os
import re
import queue
import subprocess
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
import engine_profiles
import engines
import load_progress
import ram_plan
import reopen
import theme as T
from watch_controller import (  # noqa: F401  (re-exported: the settings' choices and limits)
    GAME_ACTIONS,
    GAME_ALERT_VALID_S,
    PROTOCOL,
    RESUME_ACTIONS,
    RESUME_ALERT_VALID_S,
    RESUME_AUTO_GAP_S,
    RESUME_TARGETS_MAX,
    WatchController,
    WatchHost,
    action_labels,
    resume_labels,
)
from engines.base import (
    CAP_UNLOAD,
    CAP_FIND_INSTALL,
    CAP_LAUNCHER_FILES,
    CAP_LIVE_ACTIVITY,
    CAP_LOAD_PROGRESS,
    CAP_RAM_MODES,
    CAP_REQUEST_END,
    CAP_REQUEST_HISTORY,
    LoadReport,
    MetricsSample,
    RequestEnd,
)
from graphs import BarGraph, LineGraph, StatRow, fmt_rate
from metrics_store import MetricsStore
from busy_dialog import BusyDialog
from customtkinter.windows.widgets.core_widget_classes import DropdownMenu
from settings_dialog import SettingsDialog
from update_controller import UpdateController, UpdateHost
from sweep_dialog import SweepDialog
from version import __version__
from vram_sampler import VramSampler
from whitelist_modal import WhitelistModal

log = logging.getLogger("agent_start")

APP_TITLE = "Agent Start"
# Logical layout heights. BASE: status line + button (detail hidden). Panels below: the memory card (MEM_CARD_H) and
# the graph card (82 px per row + 1 px hairlines), then the expand bar (BAR_BLOCK) or, with everything pinned, a 16 px
# margin. HEIGHT = the default look: memory pinned, collapsed.
BASE_H = 110
MEM_CARD_H = 73
BAR_BLOCK = 24                           # BAR_PAD + BAR_H + BAR_PAD
WIDTH, HEIGHT = 360, BASE_H + 14 + MEM_CARD_H + BAR_BLOCK
DETAIL_H = 18                            # added while the detail line shows (16 px line + spacing)
PROGRESS_H = 14                          # added while the load progress bar shows
STATS_H = 3 * 82 + 2                     # three StatRows + two hairlines (logical px)
PANELS = ("memory", "vram", "decode", "prompt")   # top to bottom; pinned ones stay visible while collapsed
DEFAULT_PINNED = ("memory",)                       # = the v1.x collapsed look
PIN_GLYPH, PINNED_GLYPH = "\uE718", "\uE842"    # Segoe Fluent Icons "Pin" (outline) / "PinnedFill"
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
PENDING_GAP = 7                          # button -> the pending-stop line under it
PENDING_H = PENDING_GAP + 16             # added while that line shows
CARD_TOP = 14                            # button -> first panel
BAR_H = 14                               # the expand bar at the bottom
BAR_PAD = 5                              # above and below the bar
STATE_SAVE_DEBOUNCE_MS = 300
REOPEN_STAGGER_S = 1.0                   # pause between two reopened apps
IDLE_GRACE_S = 6.0                       # "when it's done": stop after the model has been idle this long (an agent
                                         # usually sends its next request within a few seconds of a tool call)
GAME_STOP_METRICS_FAILS = 5              # a game stop runs anyway after this many failed /metrics polls
GAME_ACTION_LABELS = {"off": "Turn Strata off", "ask": "Ask me", "none": "Do nothing"}   # the Strata wording
TOOL_WAIT_S = 600.0                      # after a request that ended in a tool call the agent comes back with the tool's
                                         # result; "when it's done" waits up to this long for that next request
SWITCH_GLYPH = "\uE8AB"                  # Segoe Fluent Icons "Switch"
USAGE_FLUSH_S = 60.0                     # usage history: serving time is added and the file saved this often
USAGE_RETRY_S = 30.0                     # ...and while idle a save that failed is retried this often
LIVE_POLL_S = 1.0                        # /metrics poll while serving (model state on the status line), as Strata's
                                         # own Monitor; stopped while the window is minimised
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


def _tail(path: str, n: int) -> str:
    """The end of a path, which is the informative part ("…\\Strata-main\\Strata-0.2.10")."""
    path = str(path or "")
    return path if len(path) <= n else "…" + path[-(n - 1):]


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


def _released(result: dict) -> bool:
    """A stop result's verdict: the engine no longer holds the model. 'released' is the engine-neutral field;
    'port_released' (the process is gone and its port is free) is what it means for engines we end."""
    return bool(result.get("released", result.get("port_released")))


def _stop_info(title: str, reason: str = "game") -> dict:
    """What a pending stop carries when an app caused it: the app's name, plus why when it was not a game start."""
    return {"game": title} if reason == "game" else {"game": title, "reason": reason}


def _toast_worker(title: str, msg: str, buttons: list[tuple[str, str]] | None = None) -> None:
    """A Windows toast. buttons: (label, protocol URL), e.g. agentstart://game-off. toasts.py keeps every text as
    data (a game folder named $(...) must not run PowerShell)."""
    try:
        import toasts
        icon = config_store.APP_DIR / "assets" / "app.png"
        app_id = "warrenjbg.AgentStart" if config_store.FROZEN else APP_TITLE
        toasts.show(title, msg, app_id, icon=str(icon) if icon.exists() else "", buttons=buttons)
    except Exception:  # noqa: BLE001
        log.debug("toast failed", exc_info=True)


def _save_backend_worker(old_id: str, engine_id: str, post: Callable, done: Callable) -> None:
    """Worker: make `engine_id` the engine in config.json (re-read first: keep whatever else changed on disk).
    The running engine's launch settings are parked under its name and the new engine's own come back
    (engine_profiles). Posts done(engine_id, ok)."""
    ok = False
    try:
        disk = config_store.load_config()
        config_store.save_config(engine_profiles.switch(disk, old_id, engine_id, config_store.DEFAULTS))
        ok = True
    except Exception:  # noqa: BLE001
        log.exception("saving the engine choice failed")
    post(done, engine_id, ok)


AFTER_EXIT_ARG = "--after-exit"          # --after-exit <pid>: a restart; wait for that Agent Start to be gone first


def _relaunch_app() -> bool:
    """Start a new Agent Start that takes over once this one has exited (it waits for our pid). Nothing else is
    started, and nothing is stopped: the caller exits the normal way afterwards."""
    try:
        argv = [sys.executable] if config_store.FROZEN else [sys.executable, os.path.abspath(__file__)]
        argv += [AFTER_EXIT_ARG, str(os.getpid())]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(argv, close_fds=True, creationflags=flags, cwd=str(config_store.APP_DIR),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        log.exception("restart failed")
        return False


def _wait_for_exit(argv: list[str], timeout_s: float = 20.0) -> None:
    """--after-exit <pid>: wait (bounded) until that process is gone, so the single-instance check does not see
    the Agent Start that asked for the restart. Only waits; a pid that is not Agent Start is not waited for."""
    try:
        if AFTER_EXIT_ARG not in argv:
            return
        pid = int(argv[argv.index(AFTER_EXIT_ARG) + 1])
        p = psutil.Process(pid)
        me = psutil.Process()
        if p.pid == me.pid or os.path.normcase(p.exe()) != os.path.normcase(me.exe()):
            return
        p.wait(timeout_s)
    except Exception:  # noqa: BLE001 - gone already, bad argument, timeout: carry on with the normal start
        return


def _park_foreign_config() -> None:
    """Start-up: launch settings that belong to another engine (the engine was changed by 1.11, which did not keep
    them apart, or by hand) are parked under that engine, so they come back when the user switches to it."""
    try:
        cfg = config_store.load_config()
        all_engines = [engines.get_engine({"backend": i}) for i in engines.available()]
        fixed = engine_profiles.park_foreign(cfg, engines.get_engine(cfg), all_engines, config_store.DEFAULTS)
        if fixed is not None:
            config_store.save_config(fixed)
            log.info("launch settings of another engine were found and kept for it (%s)",
                     ", ".join(sorted(fixed.get(engine_profiles.PROFILES_KEY) or {})))
    except Exception:  # noqa: BLE001
        log.exception("could not sort the launch settings by engine")


FIT_PRECHECK_WAIT_S = 6.0                # the launch waits at most this long for the GPU-fit pre-check


def _fit_check_worker(engine: Any, cfg: dict, chk: Any, post: Callable, done: Callable, token: int) -> None:
    """Worker: the engine's read-only GPU-fit pre-check for this start (fit_check.run is the only place that runs
    the tool, and only from the engine program's own folder). ALWAYS posts done(token, fits) exactly once - the
    launch follows it - with fits None when there is no answer within FIT_PRECHECK_WAIT_S."""
    box: list = []

    def work() -> None:
        try:
            import fit_check
            cmd = cfg.get("launch_cmd") or []
            prog = str(cmd[0]).strip().strip('"') if cmd else ""
            limit = min(float(getattr(chk, "timeout_s", FIT_PRECHECK_WAIT_S) or FIT_PRECHECK_WAIT_S),
                        FIT_PRECHECK_WAIT_S - 1.0)
            out = fit_check.run(dataclasses.replace(chk, timeout_s=limit), prog)
            rep = engine.parse_fit(out or "", cfg)
            log.info("GPU fit pre-check: %s", rep.detail or rep.fits)
            box.append(rep.fits)
        except Exception:  # noqa: BLE001 - optional: a start never depends on it
            log.debug("GPU fit pre-check failed", exc_info=True)

    t0 = time.monotonic()
    th = threading.Thread(target=work, name="hc-fit-run", daemon=True)
    try:
        th.start()
        th.join(FIT_PRECHECK_WAIT_S)
    except Exception:  # noqa: BLE001
        log.debug("GPU fit pre-check thread failed", exc_info=True)
    if th.is_alive():
        log.info("GPU fit pre-check gave no answer in %.0f s; starting without it", time.monotonic() - t0)
    post(done, token, box[0] if box and not th.is_alive() else None)


class _FitProbe:
    """The live GPU-fit check's measurements, kept by the metrics worker (never touched from the Tk thread): the
    engine's CPU share and the video memory, fed to fit_watch.decide once per poll. Read-only on processes."""

    def __init__(self) -> None:
        self.epoch: int | None = None
        self.state: Any = None
        self.procs: list = []
        self.procs_at = 0.0
        self.clean_s = 0.0
        self.last: float | None = None
        self.pre_cleared = False

    def _cpu_pct(self, now: float, endpoint: Any) -> float | None:
        if not self.procs or now - self.procs_at > 30.0:
            self.procs_at = now
            procs = []
            pids = getattr(endpoint, "engine_pids", None) or endpoint.protected_pids
            for pid in pids():
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent(None)          # primes the counter: the first reading is from the next poll
                    procs.append(p)
                except psutil.Error:
                    continue
            known = {p.pid for p in self.procs}
            fresh = {p.pid for p in procs} != known
            self.procs = procs if fresh or not self.procs else self.procs
            if fresh:
                return None
        total, seen = 0.0, False
        for p in self.procs:
            try:
                total += p.cpu_percent(None)
                seen = True
            except psutil.Error:
                continue
        if not seen:
            self.procs = []
            return None
        return total / max(1, psutil.cpu_count() or 1)

    def observe(self, epoch: int, now: float, sample: Any, vram: Any, endpoint: Any, cfg: Any):
        """-> None (nothing to tell) or (slow | None, pre_cleared) for the Tk thread."""
        import fit_watch
        if epoch != self.epoch:
            self.__init__()
            self.epoch = epoch
            self.state = fit_watch.FitState()
        live = getattr(sample, "live", None)
        generating = getattr(live, "state", None) == "generating"
        used = total = None
        try:
            snap = vram.snapshot()
            fresh = snap.samples and getattr(snap, "now", None) is not None and snap.now - snap.samples[-1][0] <= 10.0
            if getattr(snap, "available", True) and fresh and snap.total_mb:
                used, total = snap.samples[-1][1], snap.total_mb
        except Exception:  # noqa: BLE001
            used = total = None
        cpu = self._cpu_pct(now, endpoint) if generating else None
        fcfg = fit_watch.FitConfig.from_config(cfg)
        changed, self.state = fit_watch.decide(self.state, now, generating=generating, vram_used_mb=used,
                                               vram_total_mb=total, cpu_share_pct=cpu, cfg=fcfg)
        cleared = False
        if generating and not self.pre_cleared:
            clean = fit_watch.symptoms(fcfg, used, total, cpu) is False
            if clean and self.last is not None and now - self.last <= fit_watch.MAX_GAP_S:
                self.clean_s += now - self.last
            elif not clean:
                self.clean_s = 0.0
            if self.clean_s >= fcfg.clear_s:     # enough clean generating: the pre-check's "will not fit" is overruled
                self.pre_cleared = cleared = True
        self.last = now if generating else None
        if changed or cleared:
            return (self.state.slow if changed else None, cleared)
        return None


def _protocol_registered() -> bool:
    """agentstart:// opens THIS exe (installer-registered): only then does a toast button work."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\agentstart\shell\open\command") as k:
            cmd = str(winreg.QueryValueEx(k, "")[0])
        return os.path.normcase(os.path.abspath(sys.executable)) in os.path.normcase(cmd)
    except OSError:
        return False


def parse_protocol(argv: list[str]) -> str | None:
    """The action of an agentstart:// argument: 'game-off', 'resume-on', 'show' (any other agentstart: URL), or
    None."""
    from urllib.parse import urlsplit
    for a in argv:
        if not str(a).lower().startswith(PROTOCOL + ":"):
            continue
        try:
            u = urlsplit(str(a))
        except ValueError:
            return "show"
        verb = (u.netloc or u.path.lstrip("/")).strip("/").lower()
        if u.scheme.lower() == PROTOCOL and verb in ("game-off", "resume-on") and not u.query:
            return verb
        return "show"
    return None


def _refresh_autostart_worker(exe: str) -> None:
    """Start with Windows is on but points at another exe (the app moved / was reinstalled elsewhere): fix it."""
    try:
        import autostart
        if autostart.refresh(exe):
            log.info("start with Windows: updated to %s", exe)
    except Exception:  # noqa: BLE001
        log.debug("autostart refresh failed", exc_info=True)


def _release_singleton() -> None:
    global _SINGLETON_MUTEX
    m, _SINGLETON_MUTEX = _SINGLETON_MUTEX, None
    if m is not None:
        try:
            import win32api
            win32api.CloseHandle(m)
        except Exception:  # noqa: BLE001
            log.debug("mutex release failed", exc_info=True)


def _find_install_worker(engine, post: Callable, done: Callable, stop_evt) -> None:
    """Worker: the engine's bounded, read-only search for an install on this PC."""
    found = []
    try:
        found = list(engine.find_installs(stop_evt) or [])
    except Exception:  # noqa: BLE001
        log.exception("searching for %s failed", engine.name)
    post(done, found)


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

    endpoint = EndpointManager(cfg)                 # picks the engine named by cfg["backend"]
    detector = ProcessDetector(cfg, protected_pids_provider=endpoint.protected_pids, engine=endpoint.engine)
    return detector, endpoint


class AgentStartApp(ctk.CTk):
    def __init__(self, detector=None, endpoint=None, config: dict | None = None,
                 metrics_fetcher: Callable[[str], dict | None] | None = None, vram_sampler=None,
                 extras: bool | None = None, status_fetcher: Callable[[str], dict | None] | None = None) -> None:
        """metrics_fetcher(root_url) / status_fetcher(root_url) and vram_sampler are test seams (defaults: the
        engine's own monitoring over HTTP, NVML); the fetchers are handed to the engine's metrics poller.
        extras: tray icon, daily GitHub update check, first-start Strata search. Default: on for the real app, off
        when test stubs are passed (no network, no disk scan, no tray icon from a test run)."""
        if extras is None:
            extras = detector is None and endpoint is None
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
        # Everything that differs between inference engines (identity, launch, launcher files, load progress,
        # metrics, memory needs) is asked of the engine; optional features are checked with engine.can(CAP_...).
        self.engine = getattr(endpoint, "engine", None) or engines.get_engine(cfg)

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
        pins = saved.get("pinned")
        self.pinned: list[str] = ([p for p in PANELS if p in pins] if isinstance(pins, list)
                                  else list(DEFAULT_PINNED))
        self._pin_btns: dict[str, Any] = {}
        self._panel_hover: str | None = None
        self._pending_launcher: Any = None       # launcher info saved while the engine runs (applies next start)
        # Test seams for the engine's metrics poller (None = the engine's own HTTP fetchers). A test with a fake
        # metrics fetcher and no status fetcher gets no 'how did the request end' lookups at all.
        self._fetch_metrics = metrics_fetcher
        self._fetch_status = status_fetcher if status_fetcher is not None or metrics_fetcher is None \
            else (lambda _root: None)
        self.metrics = MetricsStore()
        self.usage = None                        # usage_history.UsageHistory: per-day / per-model totals (local file)
        try:
            import usage_history
            self.usage = usage_history.UsageHistory()
        except Exception:  # noqa: BLE001 - the history is optional: the app works without it
            log.exception("usage history unavailable")
        self._metrics_fails = 0
        self._metrics_epoch = 0                  # bumped per start: a late poll of the old server is dropped
        self._served_epoch = 0
        self.vram = vram_sampler if vram_sampler is not None else VramSampler()
        self._spans: list[list[float | None]] = []   # [start, end|None] (monotonic) while Strata was running
        self._avail_at_launch: float | None = None
        self._launch_wall = 0.0                  # time.time() at launch: rejects a previous run's log
        # The engine's verdict for the current run (engines.base.LoadReport): e.g. full-RAM vs slow Low-RAM mode.
        # Written by worker threads as one atomic attribute swap, read on the Tk thread.
        self._engine_load: LoadReport | None = None
        # GPU fit: the engine's pre-check said the model will not fully fit (this run) / the live check sees a
        # reply running partly on the CPU. Either shows the amber line; the live check has the last word.
        self._gpu_fit_pre = False
        self._gpu_fit_live = False
        self._toggle_at = 0.0                    # monotonic time of the last accepted ON/OFF click
        self._state_save_id = None               # pending debounced state.json save
        self._state_pending: dict = {}           # keys waiting for that save
        self.detail_open = bool(saved.get("detail_open"))   # user revealed the optional detail line
        # Reopen swept apps after Turn off. Preference from the start dialog's checkbox (default on).
        self._reopen_pref = bool(saved.get("reopen_after", True))
        self._reopen_this = False                # this start's choice (read from the sweep dialog)
        self._reopen_offer: list = []            # records offered as a one-click "Reopen …" line while idle
        self._reopening = False
        # Safe Turn off / model switch while Strata answers a request
        self._stop_pending: tuple[str, Any] | None = None     # ("off", None) | ("switch", LauncherInfo)
        self._idle_since: float | None = None
        self._req_end: RequestEnd | None = None  # how the request running now will end, as sampled while it runs
        self._was_busy = False
        self._tool_wait: tuple[str, float] | None = None   # (tool, since): last request ended in a tool call
        self._switching = False                  # a model switch is stopping Strata: no reopen, start again after
        self._launchers: list[tuple[str, str]] = []           # (label, bat path) for the switch menu
        self.tray = None                         # tray.TrayIcon once started (None: X quits as before)
        self._extras = bool(extras)
        self._tray_pref = saved.get("tray_enabled", True) is not False          # Settings switches (state.json)
        # Other apps while the engine serves (a game starts / memory runs low / that app closes): WatchController.
        self.watch = WatchController(WatchHost(
            post=self.post,
            toast=lambda title, msg, buttons: self._run_worker("hc-toast", _toast_worker, title, msg, buttons),
            toast_buttons=lambda: bool(config_store.FROZEN and _protocol_registered()),
            save_state=self._save_state_later,
            cfg=lambda: self.cfg,
            state=lambda: self.state_name,
            loaded=lambda: self.loaded,
            start_phase=lambda: self._start_phase,
            start_token=lambda: self._start_token,
            exit_pending=lambda: self._exit_pending,
            destroyed=lambda: self._destroyed,
            blocked=lambda: self._game_blocked(),
            engine_name=lambda: self.engine.name,
            engine_configured=lambda: self.engine.is_configured(self.cfg) or self._pending_launcher is not None,
            engine_mb=lambda: self._mem[2] if getattr(self, "_mem", None) and self._mem[2] else None,
            reopen_waiting=lambda: bool(self._reopen_offer),
            request_stop=lambda title, reason: self._request_game_stop(title, reason),
            cancel_start=lambda: self.cancel_start(),
            turn_on=lambda **kw: self.turn_on(**kw),
            show_window=lambda: self.show_window(),
            protected_pids=lambda: self.endpoint.protected_pids(),
            engine_protects=lambda i: self.engine.protects_process(i.cmdline, i.exe, self.cfg),
        ), saved, enabled=bool(extras))
        self._switch_to: str | None = None       # engine chosen in Settings: switch (and restart) once it is off
        self._switch_at = 0.0
        self._can_relaunch = bool(extras)        # a test app never starts a second Agent Start
        self._served_acc = 0.0                    # serving seconds not yet in the usage history (metrics worker)
        self._served_model: str | None = None
        self._no_tray = not extras or not self._tray_pref
        self._no_finder = not extras
        self._last_stop_game: str | None = None  # title of the game the running stop is for
        self._last_stop_reason = "game"          # "game" | "memory": why that stop was asked for (toast wording)
        self._skip_reopen_once = False           # Turn off from the game alert: offer the closed apps, don't relaunch
        self._stop_toast: tuple[str, str] | None = None     # wording for the next stop's toast (safe Turn off)
        self._tray_models_last = None
        self._tray_retry_until = 0.0             # keep retrying the tray icon until then (Explorer busy at logon)
        self._started_hidden = False             # launched with --tray (Start with Windows)
        self._tray_hint_shown = bool(saved.get("tray_hint_shown"))
        self._tray_last: tuple | None = None
        self._box_open = False                   # a native message box is up (tray input must not act under it)
        # The update flow (daily + manual check, offer, download, install) lives in its own controller.
        # The updater replaces the installed app: only offered by the packaged build, not when run from source.
        self.updates = UpdateController(
            UpdateHost(post=self.post, run_worker=self._run_worker, call_later=self.after,
                       set_note=self._set_note, request_preview=self._request_preview,
                       save_state_later=self._save_state_later, save_state=config_store.save_state,
                       load_state=config_store.load_state,
                       ask=lambda msg: self._ask(messagebox.askyesno, msg),
                       toast=lambda title, msg: self._run_worker("hc-toast", _toast_worker, title, msg),
                       engine_state=lambda: self.state_name, dialog_open=self._dialog_open,
                       box_open=lambda: self._box_open, release_singleton=_release_singleton,
                       finish_exit=self._finish_exit,
                       available=lambda: self._extras and config_store.FROZEN,
                       frozen=lambda: config_store.FROZEN, stop_evt=self._stop_evt,
                       engine_name=lambda: self.engine.name),
            __version__, daily=saved.get("update_check", True) is not False,
            checked_at=saved.get("update_checked_at"))
        self._found_strata = None                # an install the engine found, offered on first start
        self._finder_ran = False
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
        if st in (IDLE, ERROR):
            self._style_toggle("Turn on", TOGGLE_ON, "normal")
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

    def _set_gpu_fit_live(self, epoch: int, slow: Any, pre_cleared: bool = False) -> None:
        """The live check's verdict changed, or it saw enough clean generating to overrule the pre-check."""
        if epoch != self._metrics_epoch:
            return
        if slow is not None:
            self._gpu_fit_live = bool(slow)
        if pre_cleared:
            self._gpu_fit_pre = False
        if self.state_name == ACTIVE:
            self._apply_state()

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
        key = (show, busy, self._pending_shown)
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
        self.btn_box.grid(row=3, column=0, pady=(GAP_AFTER_DETAIL if (show or busy) else GAP_HEAD_BUTTON, 0))
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

    # ------------------------------------------------------------------ reopen swept apps
    def _reopen_worker(self, auto_reopen: bool = True) -> None:
        """Worker, after Turn off: relaunch the auto apps one by one; games stay pending as a one-click offer.
        auto_reopen False (Settings switch off): nothing starts by itself; every app is offered as one click."""
        try:
            records = reopen.load_pending()
            if not records:
                self.post(self._reopen_done, [], [])
                return
            auto, manual, skipped = reopen.classify(records)
            for r in skipped:
                log.info("reopen: skipping %s (%s)", r.name, r.reason_skip)
            if not auto_reopen:
                offer = list(auto) + list(manual)
                if offer:
                    reopen.save_pending(offer)
                else:
                    reopen.clear_pending()
                log.info("reopen after Turn off is off: offering %d app(s) as one click", len(offer))
                self.post(self._reopen_done, [], [r.to_dict() for r in offer])
                return
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
        if self.state_name == IDLE and self._detail_link and f"{self.engine.name} found" == self._summary_text:
            self.open_settings(found=self._found_strata)
            return
        if self.state_name == IDLE and self._detail_link and self.updates.offer is not None and \
                self._detail_text.startswith("Update to"):
            self.updates.install()
            return
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

    def _low_ram_line(self) -> str:
        ld = self._engine_load
        short = f" · ~{ld.short_gib:.1f} GB short" if ld and ld.short_gib else ""
        return f"{self.engine.low_ram_serving}{short}"

    def _check_engine_log(self, token: int) -> None:
        """Worker: one engine-log verdict once serving. Our own start: the run since launch; adopted: the latest run."""
        if not self.engine.can(CAP_RAM_MODES):
            return
        since = self._launch_wall if self._launch_wall and token == self._start_token else None
        try:
            load = self.engine.load_report(self.cfg, since)
        except Exception:  # noqa: BLE001
            log.debug("engine log check failed", exc_info=True)
            return
        if load is not None and load.mode != "unknown":
            self.post(self._set_engine_load, load)

    def _set_engine_load(self, load) -> None:
        if self.state_name not in (STARTING, ACTIVE):
            return
        changed = (self._engine_load is None) or (self._engine_load.mode != load.mode)
        self._engine_load = load
        if changed:
            log.info("engine load mode: %s (needs %s GiB, available %s GiB, short %s GiB%s)",
                     load.mode, load.need_gib, load.available_gib, load.short_gib,
                     f", {load.detail}" if load.detail else "")
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
        if st != ACTIVE:
            self._stop_pending, self._idle_since = None, None
            self._tool_wait, self._req_end, self._was_busy = None, None, False
        if st == STARTING or (st == ACTIVE and getattr(self, "state_name", None) in (IDLE, ERROR)):
            # a new Strata session: nothing of an earlier stop's wording / reopen choice may leak into it
            self._skip_reopen_once, self._stop_toast, self._last_stop_game = False, None, None
            watch = getattr(self, "watch", None)
            if watch is not None:
                watch.new_session()              # the engine is coming up: alerts / offers of the last session are void
            if st == ACTIVE:                     # adopted: nothing is known about how it was started
                self._gpu_fit_pre = self._gpu_fit_live = False
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

    def _metrics_poller(self):
        """The engine's poller. The test seams are looked up per call (tests swap the fetchers after building the
        app); without seams the engine uses its own HTTP fetchers."""
        seams = {}
        if self._fetch_metrics is not None:
            seams["fetch_metrics"] = lambda r, **kw: self._fetch_metrics(r, **kw)
            seams["fetch_status"] = lambda r: self._fetch_status(r) if self._fetch_status is not None else None
        http = getattr(self.endpoint, "engine_http", None)     # engines that ask the server themselves (live state)
        if http is not None:
            seams["http"] = http
        return self.engine.metrics_poller(self.cfg, **seams)

    def _run_log_path(self) -> str | None:
        """The server log of the run this app started (None for an adopted server): some engines only report
        per-request figures there."""
        try:
            st = self.endpoint.status()
            return None if st.get("adopted") else (st.get("log_path") or None)
        except Exception:  # noqa: BLE001
            return None

    def _metrics_loop(self) -> None:
        """Worker: poll the engine's monitoring data every LIVE_POLL_S while it is serving."""
        last_poll = 0.0
        last_epoch = None
        poller = None
        served_last = None                       # serving time for the usage history, added once a minute
        fit = _FitProbe()                        # GPU fit: the live check (engines with watch_gpu_fit)
        usage_retry = 0.0                        # an unwritable usage file is retried this often, not every poll
        while not self._stop_evt.is_set():
            try:
                # Every LIVE_POLL_S while serving: the status line shows what the model is doing (reading /
                # generating / queued) and the stats graphs use the same samples.
                # Also while minimised / hidden in the tray: Turn off from the tray must still know whether a reply
                # or an agent's tool call is running (the request phases are only seen by polling).
                if self.state_name in (ACTIVE, STOPPING):
                    epoch = self._metrics_epoch
                    root = config_store.server_root(str(self.cfg.get("endpoint_url") or ""))
                    reports = any(self.engine.can(c) for c in (CAP_LIVE_ACTIVITY, CAP_REQUEST_HISTORY,
                                                               CAP_REQUEST_END))
                    if poller is None:
                        poller = self._metrics_poller()
                    # Engines return only their newest finished requests per poll: after a gap (section collapsed,
                    # window minimised, a new server) ask once for all of them so the 30 bars are complete.
                    now = time.monotonic()
                    catch_up = epoch != last_epoch or now - last_poll > 3 * STATS_MS / 1000.0
                    if epoch != last_epoch:          # a new server run: forget the previous one's counters
                        poller.reset(log_path=self._run_log_path())
                    if reports:
                        sample = poller.poll(root, catch_up) if root else None
                    else:
                        # nothing to ask: an empty sample ("nothing reported"), not a failed poll - a pending
                        # stop then waits the engine's idle time instead of being treated as a dead server
                        sample = MetricsSample()
                    last_poll, last_epoch = now, epoch
                    if getattr(self.engine, "watch_gpu_fit", False) and sample is not None:
                        verdict = fit.observe(epoch, now, sample, self.vram, self.endpoint, self.cfg)
                        if verdict is not None:
                            self.post(self._set_gpu_fit_live, epoch, *verdict)
                    usage = self.usage
                    if usage is not None:
                        if epoch != self._served_epoch:  # another server run: its model is not the last one's
                            self._flush_served(usage)
                            self._served_epoch, self._served_model = epoch, None
                        if sample is not None:
                            usage.record(sample, now=time.time())
                            if sample.model:             # the same name the requests are counted under
                                self._served_model = sample.model
                        if self.state_name == ACTIVE and self.loaded is not False:
                            if served_last is not None:      # capped: sleep / hibernation is not serving time
                                self._served_acc += min(max(0.0, now - served_last), 5.0)
                            served_last = now
                        else:
                            served_last = None
                        if self._served_acc >= USAGE_FLUSH_S:
                            self._flush_served(usage)
                            usage.flush()
                            usage_retry = now
                    self.post(self._apply_metrics, epoch, sample)
                else:
                    served_last = None
                    usage = self.usage
                    mono = time.monotonic()
                    if usage is not None and (self._served_acc > 0 or usage.dirty) \
                            and mono - usage_retry >= USAGE_RETRY_S:
                        usage_retry = mono
                        self._flush_served(usage)
                        usage.flush()
            except Exception:  # noqa: BLE001
                log.debug("metrics poll failed", exc_info=True)
            self._stop_evt.wait(LIVE_POLL_S)

    def _flush_served(self, usage: Any) -> None:
        """Move the serving seconds collected so far into the usage history (metrics worker; destroy())."""
        acc, self._served_acc = self._served_acc, 0.0
        if acc > 0:
            usage.add_served(self._served_model or self.model_id, acc, now=time.time())

    def _apply_metrics(self, epoch: int, sample: MetricsSample | None) -> None:
        if epoch != self._metrics_epoch or self.state_name not in (ACTIVE, STOPPING):
            return                                        # answer of a previous server / arrived after the stop
        ok = sample is not None
        if not ok:
            self._metrics_fails += 1
            self.metrics.clear_live()
        else:
            self._metrics_fails = 0
            self.metrics.ingest(sample, now=time.monotonic())
            if self.metrics.hw_vram is not None:          # fallback VRAM source when NVML is unavailable
                self.vram.push_external(*self.metrics.hw_vram)
            self._track_request_end(sample.last_end)
        if self.state_name == ACTIVE:
            self._show_serving()                          # model state on the status line
            if isinstance(self.dialog, BusyDialog) and ok:
                self.dialog.update_activity(self._busy_activity())
            self._check_pending_stop(data_ok=ok)
        if not self._iconic:                              # hidden / minimised: no graph redraws
            self._refresh_stats()

    # ================================================================== progress / memory
    def _progress_tick(self) -> None:
        """Main thread, every 500 ms while the server is starting: elapsed timer + bar from the last parse."""
        self._progress_id = None
        if self._destroyed or not (self.state_name == STARTING and self._start_phase == "server"):
            return
        elapsed = time.monotonic() - self._server_t0
        label, frac, detail = self.engine.load_progress(self._log_text, elapsed, self._expected_s)
        if frac < self._prog_best[1]:            # a phase line scrolled out of the tail: never go backwards
            label, frac, detail = self._prog_best
        self._prog_best = (label, frac, detail)
        ld = self._engine_load                   # the engine log knows what stdout does not
        warn = False
        if ld is not None and ld.mode == "waiting_for_ram":
            label, warn = "Waiting for RAM", True
            if ld.need_gib and ld.available_gib:
                detail = f"needs {ld.need_gib:.0f} GB, {ld.available_gib:.0f} GB free"
        elif ld is not None and ld.mode == "low_ram":
            label, detail, warn = self.engine.low_ram_label, self.engine.low_ram_loading, True
        elif self._gpu_fit_pre:
            detail, warn = self.engine.gpu_fit_loading, True
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
            if path and self.engine.can(CAP_LOAD_PROGRESS):
                self._log_text = load_progress.read_tail(path)
            if self.engine.can(CAP_RAM_MODES):
                try:
                    load = self.engine.load_report(self.cfg, self._launch_wall)
                    if load is not None and load.mode != "unknown":
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
                            fn = getattr(self.endpoint, "engine_memory_mb", None) \
                                or getattr(self.endpoint, "strata_memory_mb", None)
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
        self._start_tray()
        self.watch.start()
        if self._extras and config_store.FROZEN:
            self._run_worker("hc-autostart", _refresh_autostart_worker, sys.executable)
        self.updates.first_tick()
        self._maybe_find_strata()
        self._refresh_launchers()
        self._request_preview()
        self._run_worker("hc-reopen-load", self._load_reopen_offer)

    def _tick(self) -> None:
        try:
            try:
                self._iconic = self.state() in ("iconic", "withdrawn")   # minimised or hidden in the tray
            except Exception:  # noqa: BLE001
                self._iconic = False
            self._update_telemetry()
            self._request_preview()
            self._sync_tray()
            self.watch.arm_memory(self.loaded is True)   # the model finished loading after the state changed
            self._engine_switch_check()
            self.watch.deliver_queued()          # a game that started while a dialog / box was open
            if self.tray is None and not self._no_tray and time.monotonic() < self._tray_retry_until:
                self._start_tray()               # the icon failed at start: try again (every tick, ~5 s)
            self.updates.maybe_check()
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
            if self.state() in ("iconic", "withdrawn"):     # minimised / hidden in the tray: no dry-run scans
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
        elif self._found_strata is not None and not self.engine.is_configured(self.cfg):
            self._set_status(f"{self.engine.name} found",
                             f"{_tail(self.engine.install_dir_of(self._found_strata), 30)} · click to set up",
                             color=T.TEXT_2, link=True)
        elif self._reopen_offer and not self._reopening:
            head = format_preview(res).partition("\n")[0]
            self._set_status(head if self._fits_summary(head) else "", self._offer_text(), color=T.TEXT_2, link=True)
        elif (ver := self.updates.offer_version()) is not None:
            head = format_preview(res).partition("\n")[0]
            self._set_status(head if self._fits_summary(head) else "",
                             f"Update to {ver} available · click to install", color=T.TEXT_2, link=True)
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
            if self._stop_pending is not None:   # "Stop now" while waiting for the reply to finish
                self._run_pending(force=True)
            else:
                self.request_stop(("off", None))
        elif st == STARTING:
            self.cancel_start()

    def turn_on(self, skip_sweep: bool = False) -> None:
        """skip_sweep: start at once, without scanning for apps to close and without the start dialog (the automatic
        turn-on after a game closed: nobody is there to answer it, and nothing may be closed unasked)."""
        if self.updates.updating:
            self._set_note("Installing an update · try again after the restart")
            return
        if not self.engine.is_configured(self.cfg) and self._pending_launcher is None:
            self.open_settings(found=self._found_strata)     # nothing to start yet: pick the Strata launcher first
            return
        self._sweep_note = ""
        self._reopen_this = False
        self.watch.cancel_game = None
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
        if skip_sweep:
            self._begin_server_start(token)
            return
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
            arena = self.engine.memory_need_gib(self.cfg) if self.engine.can(CAP_RAM_MODES) else None
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
        if cands:
            self.show_window()                   # the start dialog needs a visible parent (switch from the tray)
        if not cands:
            if plan is not None and not plan.enough_now:
                self._sweep_note = LOW_RAM_NOTE
            self._begin_server_start(token)
            return
        self._start_phase = "dialog"
        self._set_preview(f"{len(cands)} app(s) could be closed — choose in the dialog")
        ref: dict = {}
        self.dialog = SweepDialog(self, cands, lambda d, t: self._on_sweep_decision(token, d, t, ref.get("dlg")),
                                  ram=plan, reopen_default=self._reopen_pref, engine_name=self.engine.name)
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
        if records:
            # saved whatever the reopen choice: Turn off decides with the preference at that moment (Settings)
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
        self._gpu_fit_pre = self._gpu_fit_live = False
        try:
            self._avail_at_launch = psutil.virtual_memory().available / 1048576.0
        except Exception:  # noqa: BLE001
            self._avail_at_launch = None
        self._apply_state()                      # shows the progress bar now that the server phase began
        self._set_preview(f"Launching {self.engine.name}…")
        self._run_worker("hc-log-reader", self._log_reader, token)
        if self._progress_id is None:
            self._progress_id = self.after(PROGRESS_MS, self._progress_tick)
        chk = None
        try:
            chk = self.engine.fit_check(self.cfg)
        except Exception:  # noqa: BLE001 - optional
            log.debug("GPU fit pre-check unavailable", exc_info=True)
        if chk is None:
            self._launch_engine(token)
            return
        # The engine has a GPU-fit pre-check: it runs first and the launch follows its answer (at most
        # FIT_PRECHECK_WAIT_S later). Side by side, the tool and the server would each see the other's video memory.
        self._run_worker("hc-fit", _fit_check_worker, self.engine, dict(self.cfg), chk, self.post,
                         self._after_fit_check, token)

    def _after_fit_check(self, token: int, fits: Any) -> None:
        if not self._token_live(token) or self.state_name != STARTING or self._start_phase != "server":
            return                               # cancelled meanwhile: nothing is launched
        self._gpu_fit_pre = fits is False
        self._launch_engine(token)

    def _launch_engine(self, token: int) -> None:
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
        try:
            if self.endpoint.status().get("adopted"):   # a server that was already running: the pre-check saw ITS
                self._gpu_fit_pre = False                # memory as used, so its answer says nothing about it
        except Exception:  # noqa: BLE001
            log.debug("adopted check failed", exc_info=True)
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
        note, self._stop_toast = self._stop_toast, None
        try:
            self.endpoint.next_toast = note      # one-shot: every stop sets it (None = the default wording)
        except Exception:  # noqa: BLE001
            pass
        self._set_state(STOPPING)
        name = self.engine.name
        gentle = f"Unloading model and stopping {name}…" if self.engine.can(CAP_UNLOAD) else f"Stopping {name}…"
        self._set_preview(gentle if not force else f"Force stopping {name}…")
        if not self.endpoint.stop_endpoint(lambda r: self.post(self.on_stopped, r), force=force):
            log.info("stop_endpoint returned False; reconciling")
            self._switching = False              # no stop happened: a model switch must not restart Strata later
            self._skip_reopen_once, self._last_stop_game = False, None
            self._reconcile_now()

    # ------------------------------------------------------------------ safe stop (busy model) + model switch
    def request_stop(self, action: tuple[str, Any]) -> None:
        """Turn off / switch: straight away when the model is idle, else ask (BusyDialog) - stopping mid-reply cuts
        off whatever chat is using Strata."""
        act = self._busy_activity() if self._metrics_fails == 0 else None
        if act is None:
            self._execute(action, force=False)
            return
        switch_to = action[1].model_name if action[0] == "switch" else None
        self.dialog = BusyDialog(self, act, lambda d: self._on_busy_decision(action, d), switch_to=switch_to,
                                 engine_name=self.engine.name)

    def _on_busy_decision(self, action: tuple[str, Any], decision: str) -> None:
        if isinstance(self.dialog, BusyDialog):
            self.dialog = None
        if self.state_name != ACTIVE:
            return                               # Strata stopped while the dialog was open
        if decision == "now":
            self._execute(action, force=True)
        elif decision in ("when_idle", "idle"):
            # "idle": the model went quiet while the box was open - still wait the grace (and for an agent's tool)
            self._stop_pending, self._idle_since = action, None
            log.info("%s when the model is idle", "turn off" if action[0] == "off" else "switch model")
            self._apply_state()

    def cancel_pending_stop(self) -> None:
        if self._stop_pending is None:
            return
        self._stop_pending, self._idle_since = None, None
        self._skip_reopen_once, self._stop_toast = False, None
        log.info("pending stop cancelled")
        self._apply_state()                      # the line goes, the toggle reads "Turn off" again

    def _track_request_end(self, last_end: RequestEnd | None = None) -> None:
        """Remember how the last request ended. An agent turn is several requests: the model writes a tool call,
        the request ends, the agent runs the tool (the engine idle meanwhile), then sends the result as a new
        request. A request that ended in a tool call therefore means 'more is coming'.
        last_end: from the engine right after a request finished (authoritative; needs CAP_REQUEST_END). Without it
        the ending sampled while the request was generating is used."""
        act = self.metrics.activity()
        if act is not None:
            if act.get("kind") == "reading":
                self._req_end = None             # a new request: the previous one's sample says nothing about it
            elif act.get("kind") == "generating" and (act.get("phase") or act.get("tool_call")):
                self._req_end = RequestEnd(tool_call=bool(act.get("tool_call")), tool=act.get("tool"))
            self._was_busy = True
            return
        if last_end is not None or self._was_busy:          # a request just finished
            end = last_end if last_end is not None else (self._req_end or RequestEnd())
            if end.tool_call:
                sampled = self._req_end
                tool = end.tool or (sampled.tool if sampled is not None and sampled.tool_call else None)
                self._tool_wait = (tool or "a tool", time.monotonic())
                log.info("request ended in a tool call (%s): the agent should send another", tool or "?")
            else:
                self._tool_wait = None
            self._req_end, self._was_busy = None, False
        wait_s = float(self.cfg.get("tool_wait_s") or TOOL_WAIT_S)
        if self._tool_wait is not None and time.monotonic() - self._tool_wait[1] > wait_s:
            log.info("the agent did not come back within %.0f s after %s", wait_s, self._tool_wait[0])
            self._tool_wait = None               # the agent never came back

    def _busy_activity(self) -> dict | None:
        """What makes stopping now cut something off: a running request, or an agent between tool calls."""
        act = self.metrics.activity()
        if act is not None:
            return act
        if self._tool_wait is not None:
            return {"kind": "tool", "tool": self._tool_wait[0], "queued": 0, "frac": None, "tokens": None,
                    "rate": None, "phase": None}
        return None

    def _check_pending_stop(self, data_ok: bool) -> None:
        """Every /metrics poll while a stop waits: run it once the model has been idle for IDLE_GRACE_S after a
        request that ended in an answer (not a tool call)."""
        if self._stop_pending is None:
            return
        if not data_ok:
            # Strata not answering /metrics (often: a game just pushed it into swap): a game stop must not wait
            # forever for an idle signal that cannot come
            info = self._stop_pending[1]
            if isinstance(info, dict) and info.get("game") and self._metrics_fails >= GAME_STOP_METRICS_FAILS:
                log.info("game stop: /metrics not answering; stopping now")
                self._run_pending(force=False)
            return
        if self._busy_activity() is not None:
            self._idle_since = None              # busy (again) / the agent is running a tool: keep waiting
            return
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= (self.engine.idle_grace_s or IDLE_GRACE_S):   # engines that cannot see tool calls wait longer
            if self._stop_pending is not None and self._stop_pending[0] == "off" and self._stop_toast is None:
                # the toast says why it stopped now (the window may be hidden in the tray)
                self._stop_toast = (f"{self.engine.name} turned off",
                                    "Turned off after the reply finished · freed {freed}.")
            self._run_pending(force=False)

    def _run_pending(self, force: bool) -> None:
        action, self._stop_pending, self._idle_since = self._stop_pending, None, None
        if action is None:
            return
        game = action[1].get("game") if action[0] == "off" and isinstance(action[1], dict) else None
        if game:                                 # a stop because a game started: say so, don't relaunch over it
            self._skip_reopen_once, self._last_stop_game = True, game
            self._last_stop_reason = why = str(action[1].get("reason") or "game")
            closed = bool(reopen.load_pending())
            what = "needed the memory" if why == "memory" else "started"
            name = self.engine.name
            self._stop_toast = (f"{name} turned off", f"{_short(game, 40)} {what} · freed {{freed}} for it."
                                + (f" The apps closed for {name} are in Agent Start (Reopen)." if closed else ""))
        self._execute(("off", None) if game else action, force=force)

    def _execute(self, action: tuple[str, Any], force: bool) -> None:
        kind, info = action
        if kind != "switch":
            self.turn_off(force=force)
            return

        def saved(err: str | None) -> None:     # the launcher is saved only now, so "Keep running" changes nothing
            if err:
                self._set_note(err)
                return
            if self.state_name != ACTIVE:
                return
            self._pending_launcher = info        # applied by turn_on() once Strata has stopped
            self._switching = True
            self.turn_off(force=force)

        self.save_launcher(info, saved)

    def _switch_start(self) -> None:
        if self.state_name in (IDLE, ERROR) and not self._destroyed:
            self.turn_on()

    # model menu
    def _launcher_choices(self) -> list[tuple[str, str]]:
        """(label, launcher path) for every usable launcher file of the install; the current one's label ends with
        a check mark. Empty for an engine without launcher files."""
        eng = self.engine
        if not eng.can(CAP_LAUNCHER_FILES):
            return []
        cur_path = eng.current_launcher(self.cfg)
        cur = os.path.normcase(os.path.abspath(cur_path)) if cur_path else ""
        out = []
        for path in eng.list_launchers(self.cfg):
            try:
                info = eng.read_launcher(path)
                label = f"{info.model_name}  ·  {info.file_name}"
            except Exception:  # noqa: BLE001 - not a usable launcher (Settings explains why); not offered here
                continue
            if os.path.normcase(os.path.abspath(path)) == cur:
                label += "  ✓"
            out.append((label, path))
        return out

    def _refresh_launchers(self) -> None:
        """Listing a folder of a few small launcher files is fast enough for the Tk thread. Shows the switch button for
        two or more launchers."""
        try:
            self._launchers = self._launcher_choices()
        except Exception:  # noqa: BLE001
            log.debug("listing launchers failed", exc_info=True)
            self._launchers = []
        show = len(self._launchers) >= 2
        if show and not self.switch_btn.winfo_manager():
            self.switch_btn.grid(row=0, column=5, padx=(0, 2))
        elif not show and self.switch_btn.winfo_manager():
            self.switch_btn.grid_remove()

    def open_model_menu(self) -> None:
        if self.state_name in (STARTING, STOPPING) or self._dialog_open():
            return
        self._refresh_launchers()
        if not self._launchers:
            return
        self._model_menu.configure(values=[label for label, _ in self._launchers])
        x = self.switch_btn.winfo_rootx()
        y = self.switch_btn.winfo_rooty() + self.switch_btn.winfo_height()
        self._model_menu.open(x, y)

    def _on_model_menu(self, label: str) -> None:
        path = dict(self._launchers).get(label)
        if path:
            self.switch_model(path)

    def switch_model(self, bat_path: str) -> None:
        """Pick another launcher. Idle: just set it. Serving: stop (asking first if a reply is running), then start
        the new model; the apps closed for the old one stay closed."""
        if self.state_name in (STARTING, STOPPING):
            return
        try:
            info = self.engine.read_launcher(bat_path)
        except Exception as exc:  # noqa: BLE001
            self._set_note(f"Can't switch to {_short(os.path.basename(bat_path), 24)}\n{exc}")
            return
        if self.engine.same_launch(self.cfg, info) and self._pending_launcher is None:
            return
        log.info("model switch requested: %s (%s)", info.model_name, info.file_name)

        if self.state_name == ACTIVE:
            self.request_stop(("switch", info))  # saves the launcher only if the switch goes ahead
            return

        def saved(err: str | None) -> None:
            if err:
                self._set_note(err)
                return
            self._refresh_launchers()
            self._set_note(f"Model set to {_short(info.model_name, 26)}\nTurn on to start it")

        self.save_launcher(info, saved)

    def on_stopped(self, result: dict) -> None:
        try:
            self._on_stopped(result or {})
        except Exception:
            log.exception("on_stopped failed")
            self.show_error("Stop handling failed — see logs\\controller.log")

    def _on_stopped(self, result: dict) -> None:
        if not result.get("ok") and result.get("unload") == "busy":
            game, self._last_stop_game = self._last_stop_game, None
            if game and not self._exit_pending:  # a request arrived just now: wait for it, no box over the game
                self._skip_reopen_once = False
                self._set_state(ACTIVE)
                self._stop_pending, self._idle_since = ("off", _stop_info(game, self._last_stop_reason)), None
                log.info("game stop: the model got busy again; waiting for it")
                self._apply_state()
                return
            self._skip_reopen_once = False
            if self._ask(messagebox.askyesno, "Model is mid-turn — force stop?"):
                self.turn_off(force=True)
            else:
                self._exit_pending = False
                self._switching = False
                self._set_state(ACTIVE)
            return
        stop_game, self._last_stop_game = self._last_stop_game, None
        cancel_game = self.watch.take_cancel_game()
        released = _released(result)             # the engine no longer holds the model (however it got there)
        if not (released or result.get("cancelled_start")):
            self._switching = False
        if result.get("cancelled_start") and not (result.get("ok") or released):
            self.show_error(result.get("error") or f"Cancel failed — {self.engine.name} may still be running")
            return
        if result.get("cancelled_start"):
            self.model_id, self.loaded = None, None
            self._set_state(IDLE)
            self._set_note("Start cancelled")
            if not self._exit_pending:
                self.watch.arm_resume(cancel_game)    # the load was cancelled for a game: offer it once that closes
            self._request_preview()
        elif released:
            self.model_id, self.loaded = None, None
            self._avail_at_launch = None
            self.add_freed(result.get("ram_freed_mb"))
            self._set_state(IDLE)
            self._set_note(f"Stopped · freed {_gb(result.get('ram_freed_mb'))}")
            self._request_preview()
            if self._switching and not self._exit_pending:
                self._switching = False          # model switch: keep the closed apps closed, start the new model
                log.info("model switch: starting %s", (self._pending_launcher.model_name
                                                     if self._pending_launcher else self.cfg.get("model_name")))
                self.after(300, self._switch_start)
            elif self._switch_to is not None and not self._exit_pending:
                # turned off for an engine switch: reopen what the sweep closed (it cannot be offered after the
                # restart), then save the choice and restart
                self._run_worker("hc-reopen", self._reopen_worker, self._reopen_pref and not self._skip_reopen_once)
                self.after(1200, self._engine_switch_save)
            elif not self._exit_pending:
                self.watch.arm_resume(stop_game)      # stopped for a game / an app: offer the turn-on once it closes
                self._reopening = True
                auto = self._reopen_pref and not self._skip_reopen_once
                self._run_worker("hc-reopen", self._reopen_worker, auto)
            self._skip_reopen_once = False
        else:
            self._skip_reopen_once = False
            self.show_error(result.get("error") or "Stop failed — port still in use")
            return
        if self._exit_pending:
            self._finish_exit()

    # ================================================================== dialogs / exit
    def open_settings(self, found=None) -> None:
        """Gear: launcher choice + protected apps. Not while STARTING/STOPPING (the launch config is in use).
        found: an install the engine found, to offer (first start, nothing configured yet)."""
        if self.state_name in (STARTING, STOPPING) or self._dialog_open():
            return
        self.show_window()
        pending = self._pending_launcher
        cfg, current = self.cfg, pending.path if pending is not None else None
        if found is None and not self.engine.can(CAP_LAUNCHER_FILES):
            found = self._found_strata           # the dropdowns show what the start-up search found
        options = None
        if found is not None and not self.engine.can(CAP_LAUNCHER_FILES):
            try:
                options = self.engine.launch_options(found, self.cfg)
            except Exception:  # noqa: BLE001
                log.debug("launch options unavailable", exc_info=True)
        if found is not None and not self.engine.is_configured(self.cfg):
            cfg = dict(self.cfg, **self.engine.install_settings(found))
            current = self.engine.preferred_launcher(found)
        self.dialog = SettingsDialog(
            self, cfg, self.detector, on_save=self.save_launcher, on_closed=self._on_settings_closed,
            current_bat=current,
            running=self.state_name == ACTIVE, on_whitelist_saved=self._request_preview,
            reopen_after=self._reopen_pref, on_reopen_changed=self.set_reopen_pref,
            update_version=self.updates.offer_version(),
            on_update=self.updates.install, switches=self._settings_switches(),
            on_check_updates=self.updates.check_now, can_check_updates=self.updates.available(),
            engine=self.engine,
            on_history=self.open_history if self.usage is not None else None,
            engine_choices=engines.choices(), on_engine_change=self.set_backend, launch_options=options)

    # ------------------------------------------------------------------ system tray
    def _start_tray(self) -> None:
        if self.tray is not None or self._no_tray:
            return
        try:
            import tray as tray_mod
            icon = str(config_store.APP_DIR / "assets" / "app.png")
            post = self.post                       # tray callbacks run on the tray thread: marshal to Tk
            t = tray_mod.TrayIcon(APP_TITLE, icon, on_open=lambda: post(self.show_window),
                                  on_toggle=lambda: post(self._tray_toggle), on_quit=lambda: post(self.quit_app),
                                  on_lost=lambda: post(self._tray_lost),
                                  on_switch=lambda key: post(self._tray_switch, key))
            self.tray = t                          # X keeps quitting until the icon is really there (is_running)
            t.start_async(lambda ok: post(self._tray_started, t, ok))
        except Exception:  # noqa: BLE001
            self.tray = None
            log.exception("tray start failed; the window X quits instead")
            self._ensure_reachable()

    def _tray_started(self, t, ok: bool) -> None:
        if self._destroyed or self.tray is not t:
            return
        if not ok:
            self.tray = None
            if self._tray_retry_until == 0.0:      # Explorer may still be starting (logon): retry for 2 min
                self._tray_retry_until = time.monotonic() + 120.0
            log.warning("tray icon could not be added%s", "; retrying" if time.monotonic() < self._tray_retry_until
                        else "; the window X quits instead")
            self._ensure_reachable()             # meanwhile: minimised (started with Windows) or shown
            return
        self._tray_last = None
        self._tray_models_last = None
        self._sync_tray()

    def _ensure_reachable(self) -> None:
        """No tray icon: a window hidden to the tray must come back. Started with Windows: minimised to the taskbar,
        else shown."""
        try:
            if self._destroyed or self.state() != "withdrawn":
                return
            if self._started_hidden:
                self.deiconify()
                self.iconify()
            else:
                self.show_window()
        except Exception:  # noqa: BLE001
            log.debug("ensure_reachable failed", exc_info=True)

    def _tray_lost(self) -> None:
        """The icon is gone (Explorer did not come back): the tray is no way back to a hidden window - show it."""
        log.warning("tray icon lost; showing the window")
        self.show_window()

    def _tray_toggle(self) -> None:
        self.show_window()                       # Turn on may open the start dialog; Turn off may ask first
        if self._box_open or self._dialog_open():
            return                               # a question is up: answer that first (the window is in front)
        self.on_toggle()

    def _sync_tray(self) -> None:
        t = self.tray
        if t is None or self._destroyed:
            return
        try:
            led = self.led.cget("fg_color")
            tip = f"{APP_TITLE} · {self.pill.cget('text')}" + (f" · {self._summary_text}" if self._summary_text else "")
            label = self.toggle.cget("text")
            enabled = str(self.toggle.cget("state")) == "normal" and not label.endswith("…")
            key = (led, tip, label, enabled)
            if key != self._tray_last:
                self._tray_last = key
                t.set_status(led if isinstance(led, str) else None, tip, label, enabled)
            models = [(lbl.replace("  ✓", ""), path) for lbl, path in self._launchers]
            cur = next((path for lbl, path in self._launchers if lbl.endswith("✓")), None)
            mkey = (tuple(models), cur, self.state_name in (IDLE, ERROR, ACTIVE) and not self._box_open)
            if mkey != getattr(self, "_tray_models_last", None) and hasattr(t, "set_models"):
                self._tray_models_last = mkey
                t.set_models(models if len(models) >= 2 else None, cur, mkey[2])
        except Exception:  # noqa: BLE001
            log.debug("tray sync failed", exc_info=True)

    # ------------------------------------------------------------------ native message boxes
    def _ask(self, fn, msg: str):
        """A native message box. While one is open the tray must not change state under it."""
        self._box_open = True
        try:
            return fn(APP_TITLE, msg, parent=self)
        finally:
            self._box_open = False

    # ------------------------------------------------------------------ first start: find an install
    def _maybe_find_strata(self) -> None:
        if self._finder_ran or getattr(self, "_no_finder", False) or not self.engine.can(CAP_FIND_INSTALL):
            return
        configured = self.engine.is_configured(self.cfg)
        if configured and self.engine.can(CAP_LAUNCHER_FILES):
            return                               # its launcher files are listed from the install folder already
        self._finder_ran = True
        if not configured:                       # (configured: a quiet search, only for the Settings dropdowns)
            self._set_note(f"Looking for {self.engine.name} on this PC…")
        self._run_worker("hc-find-install", _find_install_worker, self.engine, self.post, self._on_strata_found,
                         self._stop_evt)

    def _on_strata_found(self, found) -> None:
        self._found_strata = found[0] if found else None
        if found:
            log.info("found %s: %s", self.engine.name, [self.engine.install_dir_of(f) for f in found])
        else:
            log.info("no %s found on this PC", self.engine.name)
        if self.engine.is_configured(self.cfg):
            return                               # the quiet search: nothing to say
        self._note_until = 0.0
        if self._found_strata is None and self.state_name == IDLE:
            self._set_note(f"No {self.engine.name} found\n{self.engine.setup_hint}")
        elif self._found_strata is not None and self.state_name == IDLE and self._pending_launcher is None \
                and not self._dialog_open():
            info = None
            try:
                info = self.engine.auto_launch(list(found), self.cfg)
                if info is not None:             # the same check a choice made in Settings gets
                    trial = dict(self.cfg)
                    self.engine.apply_launcher(trial, info)
                    self.engine.plan_launch(trial)
            except Exception as exc:  # noqa: BLE001 - LaunchError included: leave it to the user
                log.info("found %s but could not set it up automatically: %s", self.engine.name, exc)
                info = None
            if info is not None:
                log.info("one clear choice: setting up %s with %s", self.engine.name, info.summary())
                self.save_launcher(info, lambda err, info=info: self._auto_configured(info, err))
                return
        self._request_preview()

    def _auto_configured(self, info: Any, err: str | None) -> None:
        if self._destroyed:
            return
        if err is None and self.state_name == IDLE:
            self._set_note(f"Found {self.engine.name} · ready to turn on\n{_short(self.engine.launch_title(info), 44)}")
        self._request_preview()

    def _tray_switch(self, bat_path: str) -> None:
        """Tray menu > Switch model: the same as the ⇄ menu (asks first while a reply runs)."""
        self.show_window()
        if self._box_open or self._dialog_open():
            return
        self.switch_model(bat_path)

    # ------------------------------------------------------------------ other apps (watch_controller)
    def game_off_requested(self) -> None:
        """agentstart://game-off (the game / memory toast's button, via a second launch)."""
        self.watch.game_off_requested()

    def resume_on_requested(self) -> None:
        """agentstart://resume-on (the "closed" toast's button, via a second launch)."""
        self.watch.resume_on_requested()

    def _game_blocked(self) -> bool:
        return self._box_open or self.updates.updating or self._dialog_open()

    def _request_game_stop(self, title: str, reason: str = "game") -> None:
        """Safe Turn off because a game started (reason "game") or an app took the memory the engine needs (reason
        "memory"): the current reply (or an agent's tool call) finishes first, no dialog (the user is in the
        game); the closed apps become a one-click Reopen instead of starting over it. The game rides in the
        pending action, so nothing of it outlives that action."""
        if self.state_name != ACTIVE:
            return
        if self._stop_pending is not None:
            if self._stop_pending == ("off", None):
                # the user's own "when it's done" stop: don't reopen
                self._stop_pending = ("off", _stop_info(title, reason))
            return                               # a pending model switch is the user's explicit choice
        self._stop_pending, self._idle_since = ("off", _stop_info(title, reason)), None
        log.info("%s (%s): turning %s off when the model is idle",
                 "memory needed" if reason == "memory" else "game started", title, self.engine.name)
        if self._metrics_fails:                  # no idle signal to wait for
            self._run_pending(force=False)
            return
        self._apply_state()

    # ------------------------------------------------------------------ usage history / engine choice
    def open_history(self) -> Any:
        """Settings > Usage history: the window, or None when there is no history store."""
        if self.usage is None:
            return None
        import history_dialog
        return history_dialog.HistoryDialog(self, self.usage)

    def set_backend(self, engine_id: str) -> str | None:
        """Settings > Engine: asks once, then switches - the running engine is turned off first (the normal, safe
        Turn off), its launch settings are kept for it, and Agent Start restarts into the new engine. Returns a
        note to show, or None when nothing changes (unknown / same engine, the user said no)."""
        names = dict(engines.choices())
        if engine_id not in names:
            return "That engine is not installed"
        running = str(getattr(self.engine, "id", "") or "")
        if engine_id == running or self._switch_to is not None:
            return None
        st = self.state_name
        if st in (STARTING, STOPPING) or self.updates.updating:
            return f"Wait until {self.engine.name} has finished"
        new, cur = names[engine_id], self.engine.name
        question = (f"{cur} is running. Turn it off and switch to {new}?\n\nAgent Start will restart."
                    if st == ACTIVE else f"Switch to {new}?\n\nAgent Start will restart.")
        if not self._ask(messagebox.askyesno, question):
            return None
        if self.state_name != st or self._destroyed:          # it changed under the box: pick again
            return None
        self._switch_to, self._switch_at = engine_id, time.monotonic()
        log.info("engine switch: %s -> %s (state %s)", running, engine_id, st)
        self.after(60, self._engine_switch_begin)             # after the Settings menu's own callback returned
        return f"Turning {cur} off…" if st == ACTIVE else f"Switching to {new}…"

    def _engine_switch_begin(self) -> None:
        if self._switch_to is None or self._destroyed:
            return
        d = self.dialog
        if d is not None:
            try:
                d.close()                         # Settings: the launch config it shows is about to be replaced
            except Exception:  # noqa: BLE001
                log.debug("closing Settings failed", exc_info=True)
        if self.state_name == ACTIVE:
            self.on_toggle()                      # the user's own Turn off, with its "a reply is running" question
            return                                # _on_stopped carries on (or _tick drops the switch if declined)
        self._engine_switch_save()

    def _engine_switch_save(self) -> None:
        target = self._switch_to
        if target is None or self._destroyed or self.state_name not in (IDLE, ERROR):
            self._switch_to = None
            return
        self._set_note(f"Switching to {dict(engines.choices()).get(target, target)}…")
        self._run_worker("hc-backend", _save_backend_worker, str(self.engine.id), target, self.post,
                         self._engine_switch_restart)

    def _engine_switch_restart(self, engine_id: str, ok: bool) -> None:
        self._switch_to = None
        if self._destroyed:
            return
        name = dict(engines.choices()).get(engine_id, engine_id)
        if not ok:
            self._set_note("The engine could not be changed\nconfig.json could not be written")
            return
        if not self._can_relaunch or not _relaunch_app():
            self._set_note(f"{name} is used after Agent Start restarts\nRestart it from the Start menu")
            return
        log.info("engine switch saved: restarting into %s", engine_id)
        self._finish_exit()

    def _engine_switch_check(self) -> None:
        """Tick: a switch that waited for a Turn off the user then declined (or that failed) is dropped."""
        if self._switch_to is None or time.monotonic() - self._switch_at < 3.0:
            return
        st = self.state_name
        waiting = st == STOPPING or (st == ACTIVE and (self._stop_pending is not None or self._dialog_open()
                                                       or self._box_open))
        if st == ERROR or (st == ACTIVE and not waiting):
            log.info("engine switch dropped (state %s)", st)
            self._switch_to = None
            self._set_note("Engine not changed")

    # ------------------------------------------------------------------ settings switches
    def set_tray_pref(self, on: bool) -> bool:
        self._tray_pref = bool(on)
        self._save_state_later("tray_enabled", self._tray_pref)
        self._no_tray = not self._extras or not on
        if on:
            self._start_tray()
        else:
            tray, self.tray = self.tray, None
            if tray is not None:
                try:
                    tray.stop()
                except Exception:  # noqa: BLE001
                    log.debug("tray stop failed", exc_info=True)
        log.info("tray icon: %s", "on" if on else "off")
        return True

    def set_autostart_pref(self, on: bool) -> bool:
        """Start with Windows (installed app only): HKCU Run -> "AgentStart.exe" --tray. False = refused."""
        try:
            import autostart
            if on:
                autostart.enable(sys.executable)
            else:
                autostart.disable()
            log.info("start with Windows: %s", "on" if on else "off")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("start with Windows could not be changed: %s", exc)
            self._set_note(f"Start with Windows failed\n{_short(str(exc), 40)}")
            return False

    def _settings_switches(self) -> list[dict]:
        frozen = config_store.FROZEN
        auto_on = False
        if frozen:
            try:
                import autostart
                auto_on = autostart.is_enabled()
            except Exception:  # noqa: BLE001
                auto_on = False
        return [
            {"key": "tray", "label": "Keep running in the tray", "value": self._tray_pref,
             "on_change": self.set_tray_pref, "enabled": self._extras},
            {"key": "autostart", "label": "Start with Windows (in the tray)" if frozen
             else "Start with Windows (installed app only)", "value": auto_on,
             "on_change": self.set_autostart_pref, "enabled": frozen and self._extras},
            *self.watch.settings_rows(self._extras),
            {"key": "updates", "label": "Check for updates daily" if frozen
             else "Check for updates daily (installed app only)", "value": self.updates.daily,
             "on_change": self.updates.set_daily, "enabled": frozen and self._extras},
        ]

    def set_reopen_pref(self, on: bool) -> None:
        """Settings switch / start-dialog checkbox: reopen the apps closed for Strata after Turn off. Only the
        preference is stored; Turn off decides (off: the closed apps are offered as a one-click Reopen instead)."""
        self._reopen_pref = bool(on)
        self._save_state_later("reopen_after", self._reopen_pref)
        log.info("reopen closed apps after Turn off: %s", "on" if on else "off")

    def _on_settings_closed(self) -> None:
        if isinstance(self.dialog, SettingsDialog):   # only the dialog that closed
            self.dialog = None
        self._refresh_launchers()
        self._request_preview()

    def save_launcher(self, info: Any, done: Callable[[str | None], None]) -> None:
        """Persist the chosen launcher to config.json on a worker, then apply it (now, or at the next start)."""
        def work() -> None:
            err = None
            try:
                disk = config_store.load_config()         # re-read: keep whatever else changed on disk
                self.engine.apply_launcher(disk, info)
                config_store.save_config(disk)
            except Exception as exc:  # noqa: BLE001
                log.exception("saving the launcher failed")
                err = f"Save failed: {exc}"
            self.post(self._launcher_saved, info, err, done)

        self._run_worker("hc-save-launcher", work)

    def _launcher_saved(self, info: Any, err: str | None,
                        done: Callable[[str | None], None]) -> None:
        if err is None:
            if self.engine.same_launch(self.cfg, info):
                self._pending_launcher = None             # back to what is already loaded: nothing to apply
            else:
                self._pending_launcher = info
                self._apply_pending_launcher(note=True)
            log.info("launcher saved: %s (%s)", info.path,
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
                    self._set_note(f"{_short(info.file_name, 28)} applies when {self.engine.name} on port "
                                   f"{self.cfg.get('port', '?')} has stopped")
                return False
        except Exception:  # noqa: BLE001
            log.debug("is_running/protected_pids failed", exc_info=True)
            return False
        self.engine.apply_launcher(self.cfg, info)
        self._pending_launcher = None
        log.info("launcher applied: %s -> %s on port %s", info.file_name, info.model_name, self.cfg.get("port"))
        if note and self.state_name == IDLE:
            self._set_note(f"Launcher set to {_short(info.file_name, 30)}\n{info.summary()}")
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
        """Window X: hide to the tray (Quit is in the tray menu). Without a tray icon: quit as before."""
        if self.tray is not None and getattr(self.tray, "is_running", False) and not self.updates.updating:
            self.hide_to_tray()
            return
        self.quit_app()

    def hide_to_tray(self) -> None:
        if self._dialog_open():                  # a dialog waits for an answer: keep it visible
            try:
                self.dialog.lift()
            except Exception:  # noqa: BLE001
                pass
            return
        self.withdraw()
        self._iconic = True
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self._save_state_later("tray_hint_shown", True)
            self._run_worker("hc-toast", _toast_worker, APP_TITLE,
                             "Still running in the tray. Right-click the icon for Turn off and Quit.")

    def show_window(self) -> None:
        try:
            if self.state() in ("withdrawn", "iconic"):
                self.deiconify()
            self._iconic = False
            self.lift()
            self.focus_force()
        except Exception:  # noqa: BLE001
            log.debug("show_window failed", exc_info=True)

    def quit_app(self) -> None:
        """Quit (tray menu, or X without a tray): asks about a running Strata as before."""
        self.show_window()
        if self._box_open or self.updates.installing:   # a question is already up / the installer is about to replace us
            return
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
            if not self._ask(messagebox.askyesno, "Cancel start and exit?"):
                return
            if self.state_name != STARTING:     # the start finished while the box was open: ask again
                return self.quit_app()
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
            ans = self._ask(messagebox.askyesnocancel, f"Stop {self.engine.name} before exit?")
            if ans is None:
                return
            if self.state_name != ACTIVE:       # state changed under the modal box (monitor/pump kept running)
                return self.quit_app()
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
        tray, self.tray = self.tray, None
        if tray is not None:
            try:
                tray.stop()
            except Exception:  # noqa: BLE001
                log.debug("tray stop failed", exc_info=True)
        self.watch.stop()
        if self.usage is not None:               # the metrics worker is a daemon: it may not get another turn
            try:
                self._flush_served(self.usage)
                self.usage.flush()
            except Exception:  # noqa: BLE001
                log.debug("usage history flush failed", exc_info=True)
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


_EVENTS: list = []                               # the named events below, created right after the mutex
SHOW_EVENT = "AgentStart.Show"                   # a second launch sets it: the running app shows its window
GAME_OFF_EVENT = "AgentStart.GameOff"            # agentstart://game-off (toast button): turn Strata off for a game
RESUME_ON_EVENT = "AgentStart.ResumeOn"          # agentstart://resume-on (toast button): turn it back on
_PROTOCOL_EVENTS = {"game-off": GAME_OFF_EVENT, "resume-on": RESUME_ON_EVENT}


def _selftest() -> int:
    """`AgentStart.exe --selftest` (run by build.bat on the frozen build): every lazily imported module loads and the
    tray icon renders. Exit code 0 = OK. No window, no mutex, no network, no disk scan."""
    try:
        import PIL.Image  # noqa: F401
        import autostart  # noqa: F401
        import game_watch  # noqa: F401
        import mem_watch  # noqa: F401
        import watch_controller  # noqa: F401
        import resume_watch  # noqa: F401
        import usage_history  # noqa: F401
        import history_dialog  # noqa: F401
        import toasts  # noqa: F401
        import engines.strata  # noqa: F401
        if not {"strata", "llama_cpp"} <= set(engines.available()):   # discovery imports every engine module
            return 5
        import fit_check  # noqa: F401
        import llama_finder  # noqa: F401
        import engine_profiles  # noqa: F401
        import fit_watch  # noqa: F401
        import strata_finder  # noqa: F401
        import tray
        import update_controller  # noqa: F401
        import updater  # noqa: F401
        import win32gui  # noqa: F401
        import detector  # noqa: F401
        import server_manager  # noqa: F401
        icon = config_store.APP_DIR / "assets" / "app.png"
        if not icon.is_file():
            return 3
        t = tray.TrayIcon(APP_TITLE, str(icon), on_open=lambda: None, on_toggle=lambda: None, on_quit=lambda: None)
        img = t._render(T.ACCENT)
        return 0 if img is not None and img.size[0] >= 16 else 4
    except Exception:  # noqa: BLE001
        return 2


def _instance_running() -> bool:
    import win32api
    import win32event
    try:
        h = win32event.OpenMutex(win32event.SYNCHRONIZE, False, "AgentStart.Singleton")
        win32api.CloseHandle(h)
        return True
    except Exception:  # noqa: BLE001 - ERROR_FILE_NOT_FOUND: no instance
        return False


def _watch_show_event(app: "AgentStartApp") -> None:
    """Daemon thread: a second launch (Start menu, desktop shortcut) sets SHOW_EVENT; bring this window back, also
    from the tray (FindWindow + ShowWindow from outside would leave Tk thinking it is still withdrawn)."""
    import win32event
    try:
        ev = win32event.CreateEvent(None, False, False, SHOW_EVENT)
        game = win32event.CreateEvent(None, False, False, GAME_OFF_EVENT)
        resume = win32event.CreateEvent(None, False, False, RESUME_ON_EVENT)
    except Exception:  # noqa: BLE001
        log.debug("show event unavailable", exc_info=True)
        return
    while not app._destroyed:
        r = win32event.WaitForMultipleObjects([ev, game, resume], False, 1000)
        if app._destroyed:
            break
        if r == win32event.WAIT_OBJECT_0:
            app.post(app.show_window)
        elif r == win32event.WAIT_OBJECT_0 + 1:
            app.post(app.game_off_requested)
        elif r == win32event.WAIT_OBJECT_0 + 2:
            app.post(app.resume_on_requested)


def main() -> None:
    global _SINGLETON_MUTEX
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    _wait_for_exit(sys.argv)                         # a restart (engine switch): the old one exits first
    import win32api
    import win32event
    import winerror

    if parse_protocol(sys.argv[1:]) is not None and not _instance_running():
        sys.exit(0)                                  # a toast button after Quit: nothing to turn off, open nothing
    _SINGLETON_MUTEX = win32event.CreateMutex(None, False, "AgentStart.Singleton")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        if "--tray" in sys.argv:                     # a second autostart launch: nothing to show
            sys.exit(0)
        action = parse_protocol(sys.argv[1:])
        try:                                         # the running app shows itself (also when hidden in the tray)
            name = _PROTOCOL_EVENTS.get(action or "", SHOW_EVENT)
            ev = win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False, name)
            win32event.SetEvent(ev)
            sys.exit(0)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001          # an older version is running / still starting: the old way
            if action is not None:
                sys.exit(0)                          # a toast-button URL never pops windows or boxes

        user32 = ctypes.windll.user32
        # class + title: an Explorer window of a folder called "Agent Start" has the same title
        hwnd = user32.FindWindowW("TkTopLevel", APP_TITLE)
        if hwnd:                                     # bring the running window forward instead of a dialog
            user32.ShowWindow(hwnd, 9)                # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        else:
            user32.MessageBoxW(None, "Agent Start is already running.", APP_TITLE, 0x40)
        sys.exit(0)
    global _EVENTS
    try:      # before the window exists: a second launch during our start-up must find them (else it shows a box)
        _EVENTS = [win32event.CreateEvent(None, False, False, SHOW_EVENT),
                   win32event.CreateEvent(None, False, False, GAME_OFF_EVENT),
                   win32event.CreateEvent(None, False, False, RESUME_ON_EVENT)]
    except Exception:  # noqa: BLE001
        _EVENTS = []
    _setup_logging()
    _check_config()
    _park_foreign_config()
    try:  # own taskbar identity/icon instead of grouping under python(w).exe
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("warrenjbg.AgentStart")
    except Exception:  # noqa: BLE001
        pass
    log.info("Agent Start %s starting (frozen=%s, data=%s)", __version__, config_store.FROZEN,
             config_store.DATA_DIR)
    try:
        app = AgentStartApp()
    except Exception:
        log.exception("startup failed")
        raise
    threading.Thread(target=_watch_show_event, args=(app,), name="hc-show-event", daemon=True).start()
    if "--tray" in sys.argv:                     # Start with Windows: start hidden in the tray
        app._started_hidden = True
        if app._tray_pref:
            app.withdraw()                       # _ensure_reachable() minimises instead if the icon fails
            app._iconic = True
        else:
            # after the loop runs: CustomTkinter's mainloop() withdraws/restores the window once (title bar
            # colour) and would leave an early iconify() withdrawn - no window, no taskbar button, no tray
            app.after(300, app.iconify)
    app.mainloop()
    log.info("Agent Start exited")


if __name__ == "__main__":
    main()
