"""WatchController: what Agent Start does about OTHER apps while the engine serves, moved out of AgentStartApp.

  * a game starts            -> turn the engine off safely / ask / nothing   (game_watch, Settings "When a game starts")
  * an app takes the memory  -> the same, with its own setting               (mem_watch, "When memory runs low")
  * that game / app closes   -> offer or do the turn-on                      (resume_watch, "When that app closes")

No Tk and no import of main: the app is reached only through WatchHost (plain callables), as UpdateController does.
Every method runs on the Tk thread; the watcher threads only post. This controller never stops or starts the engine
itself and never touches a process: it asks the host (request_stop / cancel_start / turn_on), which owns the stop
state machine. The watchers it creates only observe.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

log = logging.getLogger("agent_start")

IDLE, STARTING, ACTIVE = "IDLE", "STARTING", "ACTIVE"      # the host's state names this controller reads

GAME_ACTIONS = ("off", "ask", "none")    # "When a game starts" / "When memory runs low": turn off / ask / do nothing
GAME_ALERT_VALID_S = 1800.0              # the "Turn off" button of a game / memory toast works this long after it
RESUME_ACTIONS = ("ask", "on", "none")   # "When that app closes": ask / turn the engine back on / nothing
RESUME_ALERT_VALID_S = 1800.0            # the "Turn on" button of the "closed" toast works this long
RESUME_TARGETS_MAX = 8                   # remembered causes (game / app) of this session's alerts
RESUME_AUTO_GAP_S = 1800.0               # one automatic turn-on per app in this time; a repeat asks instead
PROTOCOL = "agentstart"                  # agentstart://game-off, ://resume-on (toast buttons)


def action_labels(engine_name: str) -> dict[str, str]:
    """The three choices of the "When a game starts" / "When memory runs low" settings, naming the engine."""
    return {"off": f"Turn {engine_name} off", "ask": "Ask me", "none": "Do nothing"}


def resume_labels(engine_name: str) -> dict[str, str]:
    """The three choices of the "When that app closes" setting, naming the engine."""
    return {"ask": "Ask me", "on": f"Turn {engine_name} back on", "none": "Do nothing"}


def _short(name: str, limit: int = 22) -> str:
    """Same as main._short (the toasts must read exactly as before)."""
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name if len(name) <= limit else name[: limit - 1] + "…"


@dataclass
class WatchHost:
    """How the controller reaches the app. Everything is called on the Tk thread unless noted."""
    post: Callable[..., Any]                      # post(fn, *args): run fn on the Tk thread (from watcher threads)
    toast: Callable[[str, str, list], Any]        # toast(title, message, buttons)
    toast_buttons: Callable[[], bool]             # toast buttons work (installed app + protocol registered)
    save_state: Callable[[str, Any], Any]         # save_state(key, value) -> state.json, debounced
    cfg: Callable[[], dict]                       # the live config dict
    state: Callable[[], str]                      # "IDLE" | "STARTING" | "ACTIVE" | ...
    loaded: Callable[[], Any]                     # True / False / None: the model is loaded
    start_phase: Callable[[], Any]                # "server" while the engine process is loading
    start_token: Callable[[], int]                # changes with every start (an alert belongs to one session)
    exit_pending: Callable[[], bool]
    destroyed: Callable[[], bool]
    blocked: Callable[[], bool]                   # a dialog / message box / update is in the way
    engine_name: Callable[[], str]
    engine_configured: Callable[[], bool]         # there is something to start
    engine_mb: Callable[[], Any]                  # the engine's memory in MB (for the toast), or None
    reopen_waiting: Callable[[], bool]            # a "Reopen" offer is waiting (a start would discard it)
    request_stop: Callable[[str, str], Any]       # request_stop(title, reason): the safe turn-off for that app
    cancel_start: Callable[[], Any]
    turn_on: Callable[..., Any]                   # turn_on(skip_sweep=False)
    show_window: Callable[[], Any]
    protected_pids: Callable[[], Any]             # the engine's pids (mem_watch never reports them)
    engine_protects: Callable[[Any], bool]        # the engine's own sweep-protection rule (mem_watch)


class WatchController:
    def __init__(self, host: WatchHost, saved: Mapping[str, Any] | None = None, enabled: bool = True) -> None:
        saved = saved or {}
        self.host = host
        self.enabled = bool(enabled)             # False: no watcher threads (tests / --no-extras)
        ga = saved.get("game_action")
        if ga not in GAME_ACTIONS:               # v1.8.0 had an on/off switch ("game_alert")
            ga = "none" if saved.get("game_alert") is False else "off"
        self.game_action: str = ga
        self.game_pref = ga != "none"
        ma = saved.get("memory_action")          # a state.json without it means "ask"
        self.mem_action: str = ma if ma in GAME_ACTIONS else "ask"
        ra = saved.get("resume_action")          # a state.json without it means "ask"
        self.resume_action: str = ra if ra in RESUME_ACTIONS else "ask"
        self.games: Any = None                   # game_watch.GameWatcher
        self.memwatch: Any = None                # mem_watch.MemWatcher; runs inside the game scan
        self.resume: Any = None                  # resume_watch.ResumeWatcher
        self.games_armed = False
        self.mem_armed = False
        self.game_queued: tuple[str, float] | None = None   # a game started while the engine was still loading
        self.mem_queued: tuple[str, float] | None = None    # reported while a dialog / box was open
        self.game_alert: tuple | None = None     # (monotonic, start token, title[, "memory"]): last "Ask me"
        self.resume_targets: dict[str, Any] = {}             # alert title -> ResumeTarget (what to watch for)
        self.resume_alert: tuple[float, str] | None = None   # (monotonic, title) of the last "closed" toast
        self.resume_auto_at: dict[str, float] = {}           # title -> monotonic time of its automatic turn-on
        self.cancel_game: str | None = None      # the game a still-loading start was cancelled for

    # ------------------------------------------------------------------ watchers
    def start(self) -> None:
        """Create and start the watcher threads (once). Each is optional: the app works without any of them."""
        h = self.host
        if not self.enabled or self.games is not None:
            return
        try:
            import game_watch
            post = h.post
            cfg = h.cfg()
            try:
                # the memory check rides on the game watcher's thread and its one process snapshot per pass
                import mem_watch
                self.memwatch = mem_watch.MemWatcher(
                    cfg,                               # the dict the detector reads: protected-list edits apply live
                    on_app=lambda r: post(self.on_memory_app, r.title, r.ram_mb, r.why(), r),
                    protected_pids=h.protected_pids,
                    engine_protects=h.engine_protects)
            except Exception:  # noqa: BLE001
                self.memwatch = None
                log.exception("memory watcher unavailable")
            self.games = game_watch.GameWatcher(list(cfg.get("game_dirs") or []),
                                                list(cfg.get("game_exclude") or []),
                                                on_game=lambda g: post(self.on_game_started, g.title, g.ram_mb, g),
                                                co_scan=self.memwatch)
            self.games.start()
            try:
                import resume_watch
                self.resume = resume_watch.ResumeWatcher(
                    lambda: list(cfg.get("game_dirs") or []),
                    on_closed=lambda t, s: post(self.on_resume_closed, t, s),   # watcher thread: only post
                    game_exclude=lambda: list(cfg.get("game_exclude") or []), cfg=cfg)
                self.resume.start()
            except Exception:  # noqa: BLE001
                self.resume = None
                log.exception("resume watcher unavailable")
        except Exception:  # noqa: BLE001
            self.games = None
            self.memwatch = None
            log.exception("game watcher unavailable")
        self.arm()

    def stop(self) -> None:
        """App exit: stop the watcher threads."""
        games, self.games = self.games, None
        if games is not None:
            try:
                games.stop()
            except Exception:  # noqa: BLE001
                log.debug("game watcher stop failed", exc_info=True)
        resume, self.resume = self.resume, None
        if resume is not None:
            try:
                resume.stop()
            except Exception:  # noqa: BLE001
                log.debug("resume watcher stop failed", exc_info=True)

    def arm(self) -> None:
        """Arm / disarm both checks for the host's current state."""
        st = self.host.state()
        self.arm_games(st in (STARTING, ACTIVE))
        self.arm_memory(st == ACTIVE and self.host.loaded() is True)

    def arm_games(self, want: bool) -> None:
        """Watch for game launches only while the engine serves (games already running when it started never
        alert)."""
        want = bool(want and self.game_pref and self.games is not None)
        if want == self.games_armed:
            return
        self.games_armed = want
        if not want:
            self.game_queued = None
        try:
            if want:
                self.games.arm()
            elif self.games is not None:
                self.games.disarm()
        except Exception:  # noqa: BLE001
            log.debug("game watcher arm/disarm failed", exc_info=True)

    def arm_memory(self, want: bool) -> None:
        """Watch for memory pressure from READY (model loaded) until the stop: the baseline of available memory is
        taken then, and only apps started after it can be reported."""
        want = bool(want and self.mem_action != "none" and self.memwatch is not None and self.games is not None)
        if want == self.mem_armed:
            return
        self.mem_armed = want
        if not want:
            self.mem_queued = None
        try:
            if want:
                self.memwatch.arm()
            elif self.memwatch is not None:
                self.memwatch.disarm()
        except Exception:  # noqa: BLE001
            log.debug("memory watcher arm/disarm failed", exc_info=True)

    # ------------------------------------------------------------------ session hooks (called by the host)
    def new_session(self) -> None:
        """The engine is coming up (a start, or an adopted server): alerts of the last session are void."""
        self.game_alert = None
        self.cancel_resume()

    def has_queued(self) -> bool:
        return self.game_queued is not None or self.mem_queued is not None

    def take_cancel_game(self) -> str | None:
        game, self.cancel_game = self.cancel_game, None
        return game

    def _buttons(self, label: str, verb: str) -> list[tuple[str, str]]:
        return [(label, f"{PROTOCOL}://{verb}")] if self.host.toast_buttons() else []

    # ------------------------------------------------------------------ a game started / memory runs low
    def stop_for(self, title: str, reason: str = "game") -> None:
        """Safe Turn off because a game started (reason "game") or an app took the memory the engine needs
        (reason "memory"). Waits here while a dialog / box / update is in the way; the host does the rest."""
        h = self.host
        if h.state() != ACTIVE:
            return
        if h.blocked():                          # retried from the tick once the dialog / box / update is gone
            if reason == "memory":
                self.mem_queued = (title, 0.0)   # ...under its own setting, not the game one
            else:
                self.game_queued = (title, 0.0)
            return
        h.request_stop(title, reason)

    def deliver_queued(self) -> None:
        h = self.host
        if h.state() != ACTIVE or h.loaded() is False or h.blocked():
            return
        if self.game_queued is not None:
            title, ram = self.game_queued
            self.game_queued = None
            self.on_game_started(title, ram)
        if self.mem_queued is not None and not h.blocked():
            title, ram = self.mem_queued
            self.mem_queued = None
            self.on_memory_app(title, ram)

    def on_memory_app(self, title: str, ram_mb: float = 0.0, why: str = "", src: Any = None) -> None:
        """mem_watch reported an app started while serving that holds a lot of memory while the PC runs short.
        Same safe turn-off as for a game (reply / tool call first, no dialog, closed apps become a Reopen offer),
        with its own setting. src: the MemReport (so the app can be watched for closing after the stop)."""
        h = self.host
        self.remember_target(title, src, "app")
        if self.mem_action == "none" or h.destroyed() or h.state() != ACTIVE or h.loaded() is False:
            return
        if h.blocked():
            self.mem_queued = (title, ram_mb)
            return
        log.info("memory needed while serving: %s, %.0f MB (action: %s) %s", title, ram_mb, self.mem_action, why)
        if self.mem_action == "off":
            self.stop_for(title, reason="memory")   # the stop's own toast says why
            return
        name = h.engine_name()
        self.game_alert = (time.monotonic(), h.start_token(), title, "memory")
        buttons = self._buttons(f"Turn off {name}", "game-off")
        h.toast("Memory is running low",
                f"{_short(title, 40)} is using {ram_mb / 1024:.1f} GB while {name} is running."
                + ("" if buttons else " Open Agent Start to turn it off."), buttons)

    def on_game_started(self, title: str, ram_mb: float = 0.0, src: Any = None) -> None:
        h = self.host
        if not self.game_pref or h.destroyed():
            return
        self.remember_target(title, src, "game")
        st = h.state()
        name = h.engine_name()
        if st == STARTING and h.start_phase() == "server" and self.game_action == "off" and not h.exit_pending():
            log.info("game started during the load (%s): cancelling the start", title)
            h.toast(f"{name} start cancelled",
                    f"{_short(title, 40)} started, so {name} stopped loading and left the memory to it.", [])
            self.cancel_game = title             # the cancelled load is offered again once the game closes
            h.cancel_start()
            return
        if st == STARTING or (st == ACTIVE and h.loaded() is False):
            self.game_queued = (title, ram_mb)   # tell once the engine is ready
            return
        if st != ACTIVE:
            return
        if h.blocked():
            self.game_queued = (title, ram_mb)
            return
        mb = h.engine_mb()
        free = f"~{mb / 1024:.0f} GB" if mb else "its memory"
        log.info("game started while serving: %s (action: %s)", title, self.game_action)
        if self.game_action == "off":
            self.stop_for(title)                 # the stop's own toast says why
            return
        # ask: a real Windows toast (tray balloons are not shown on Windows 11, and during a full-screen game toasts
        # wait in the notification centre) with a button that works from there
        self.game_alert = (time.monotonic(), h.start_token(), title)
        buttons = self._buttons(f"Turn off {name}", "game-off")
        h.toast("Game started",
                f"{_short(title, 40)} started while {name} is running ({free})."
                + ("" if buttons else " Open Agent Start to turn it off."), buttons)

    def game_off_requested(self) -> None:
        """The game toast's "Turn off" button (agentstart://game-off, via a second launch). Only for the alert of
        this serving session and within GAME_ALERT_VALID_S - an old notification-centre entry only opens the
        window."""
        h = self.host
        alert = self.game_alert
        fresh = alert is not None and time.monotonic() - alert[0] < GAME_ALERT_VALID_S and alert[1] == h.start_token()
        if not fresh or h.state() != ACTIVE:
            log.info("game toast button: %s; opening the window", "stale" if alert else "no alert")
            h.show_window()
            return
        if h.blocked():
            h.show_window()                      # a question is open: answer it; the button works again after
            return
        assert alert is not None
        self.game_alert = None
        self.stop_for(alert[2], reason=alert[3] if len(alert) > 3 else "game")

    # ------------------------------------------------------------------ back on when that app closes
    def remember_target(self, title: str, src: Any, kind: str) -> None:
        """Keep what to watch for (the game's install / the app's processes) under the alert's title."""
        if src is None or self.host.destroyed():
            return
        try:
            import resume_watch
            if kind == "game":
                target = resume_watch.target_for_game(src, list(self.host.cfg().get("game_dirs") or []))
            else:
                target = resume_watch.target_for_app(src)
        except Exception:  # noqa: BLE001
            log.debug("resume target unavailable for %s", title, exc_info=True)
            return
        self.resume_targets.pop(title, None)
        self.resume_targets[title] = target
        while len(self.resume_targets) > RESUME_TARGETS_MAX:
            self.resume_targets.pop(next(iter(self.resume_targets)))

    def arm_resume(self, title: str | None) -> None:
        """A stop made for `title` (a game / a memory-hungry app) released the model: watch for it to close."""
        if not title or self.resume_action == "none" or self.resume is None:
            return
        target = self.resume_targets.get(title)
        if target is None:
            return
        try:
            self.resume.watch(target)
            log.info("watching for %s to close (then: %s)", title, self.resume_action)
        except Exception:  # noqa: BLE001
            log.exception("resume watch failed")

    def cancel_resume(self) -> None:
        self.resume_alert = None
        if self.resume is not None:
            try:
                self.resume.cancel()
            except Exception:  # noqa: BLE001
                log.debug("resume cancel failed", exc_info=True)

    def on_resume_closed(self, target: Any, closed_for_s: float = 0.0) -> None:
        """The game / app a stop was made for has closed: turn the engine back on, or offer it (Settings)."""
        h = self.host
        if h.destroyed() or self.resume_action == "none" or h.state() != IDLE:
            return
        title = str(getattr(target, "title", "") or "The app")
        name = h.engine_name()
        log.info("%s closed (%.0f s ago); when that app closes: %s", title, closed_for_s, self.resume_action)
        now = time.monotonic()
        last_auto = self.resume_auto_at.get(title)
        repeat = last_auto is not None and now - last_auto < RESUME_AUTO_GAP_S
        # Automatic only when nothing else needs the user: no dialog / box / update, no "Reopen" offer waiting (a
        # start would discard it unseen), the engine is set up, and this app did not already get one automatic
        # turn-on a moment ago (a game that keeps restarting must not load and unload the model in a loop).
        if self.resume_action == "on" and not h.blocked() and not h.reopen_waiting() and not repeat \
                and h.engine_configured():
            self.resume_auto_at.pop(title, None)
            self.resume_auto_at[title] = now
            while len(self.resume_auto_at) > RESUME_TARGETS_MAX:
                self.resume_auto_at.pop(next(iter(self.resume_auto_at)))
            h.turn_on(skip_sweep=True)
            if h.state() == STARTING:
                h.toast(f"{name} is starting",
                        f"{_short(title, 40)} closed, so {name} is turning back on. No apps were closed for it.", [])
                return
        if repeat:
            log.info("%s closed again soon after an automatic turn-on: asking instead", title)
        self.resume_alert = (time.monotonic(), title)
        buttons = self._buttons(f"Turn on {name}", "resume-on")
        h.toast(f"{_short(title, 40)} closed",
                f"Turn {name} back on?" + ("" if buttons else " Open Agent Start to turn it on."), buttons)

    def resume_on_requested(self) -> None:
        """The "closed" toast's button (agentstart://resume-on, via a second launch): the normal Turn on, with its
        start dialog. An old notification-centre entry only opens the window."""
        h = self.host
        alert = self.resume_alert
        fresh = alert is not None and time.monotonic() - alert[0] < RESUME_ALERT_VALID_S
        if not fresh or h.state() != IDLE or h.blocked():
            log.info("resume toast button: %s; opening the window", "not now" if fresh else "no fresh alert")
            h.show_window()
            return
        self.resume_alert = None
        h.show_window()
        h.turn_on()

    # ------------------------------------------------------------------ settings
    def set_game_action(self, action: str) -> bool:
        if action not in GAME_ACTIONS:
            action = {v: k for k, v in action_labels(self.host.engine_name()).items()}.get(action, "off")
        self.game_action = action
        self.host.save_state("game_action", action)
        return self.set_game_pref(action != "none")

    def set_game_pref(self, on: bool) -> bool:
        self.game_pref = bool(on)
        self.arm_games(self.host.state() in (STARTING, ACTIVE))
        log.info("game launch alert: %s", "on" if on else "off")
        return True

    def set_memory_action(self, action: str) -> bool:
        if action not in GAME_ACTIONS:
            action = {v: k for k, v in action_labels(self.host.engine_name()).items()}.get(action, "ask")
        self.mem_action = action
        self.host.save_state("memory_action", action)
        self.arm_memory(self.host.state() == ACTIVE and self.host.loaded() is True)
        log.info("when memory runs low: %s", action)
        return True

    def set_resume_action(self, action: str) -> bool:
        if action not in RESUME_ACTIONS:
            action = {v: k for k, v in resume_labels(self.host.engine_name()).items()}.get(action, "ask")
        self.resume_action = action
        self.host.save_state("resume_action", action)
        if action == "none":
            self.cancel_resume()
        log.info("when that app closes: %s", action)
        return True

    def settings_rows(self, enabled: bool) -> list[dict]:
        """The three choice rows for the Settings dialog."""
        name = self.host.engine_name()
        labels, rlabels = action_labels(name), resume_labels(name)
        return [
            {"key": "games", "label": "When a game starts", "choices": [labels[a] for a in GAME_ACTIONS],
             "value": labels[self.game_action], "on_change": self.set_game_action, "enabled": enabled},
            {"key": "memory", "label": "When memory runs low", "choices": [labels[a] for a in GAME_ACTIONS],
             "value": labels[self.mem_action], "on_change": self.set_memory_action, "enabled": enabled},
            {"key": "resume", "label": "When that app closes", "choices": [rlabels[a] for a in RESUME_ACTIONS],
             "value": rlabels[self.resume_action], "on_change": self.set_resume_action, "enabled": enabled},
        ]
