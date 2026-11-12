"""'The app has closed' watcher for Agent Start (observe only: this module never kills, closes, signals or suspends
anything).

When Strata is turned off for a game (game_watch.GameStart) or a memory-hungry app (mem_watch.MemReport), the app
records a ResumeTarget and hands it to a ResumeWatcher. Once that game/app has been gone for a while the watcher
reports it, so the UI can offer to turn Strata back on.

  * watch(target) -> O(1), safe from the Tk thread; replaces any earlier target.
  * on_closed     -> called on the 'resume-watch' thread, at most ONCE per watch(), with (target, seconds closed).
  * cancel()      -> no further reports; once it returns, on_closed is never called for the old target.
  * While nothing is watched the thread sleeps without taking any process snapshot.

Pure core (fake process lists in tests):
  * is_running(target, procs, game_dirs) - is the target still running in this one snapshot?
  * decide(state, running, now)          - has it been gone long enough (confirm window, gaps, expiry)?
Rules for 'which process belongs to a game install' are game_watch's own (install_of, launcher/ignored exclusions,
session filter) - reused, never re-implemented.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import game_watch
from detector import norm_path
from game_watch import _exclusions, _game_procs, _info_of, current_session_filter, install_of

log = logging.getLogger("agent_start")

INTERVAL_S = 5.0                 # one process snapshot per this many seconds while a target is watched
CT_TOLERANCE_S = 1.0             # (pid, create_time) match: psutil create_time jitter of the same process
MAX_GAP_S = 15.0                 # two 'not running' observations further apart than this restart the confirm window

# config.json keys (missing key = default; bad value = default; numbers are clamped), read like mem_watch does.
CONFIG_DEFAULTS: dict[str, float] = {
    "resume_confirm_s": 10,                   # gone for this long (consecutive scans) before 'closed' is reported
    "resume_expire_s": 6 * 3600,              # give up silently after this long (the user moved on)
}
_LIMITS: dict[str, tuple[float, float]] = {
    "resume_confirm_s": (3, 600),
    "resume_expire_s": (60, 7 * 24 * 3600),
}


def _num(cfg: Mapping | None, key: str) -> float:
    """cfg[key] as a finite float clamped to its limits; the default for a missing/bad value (bool, str, NaN...)."""
    dflt = float(CONFIG_DEFAULTS[key])
    v = cfg.get(key, dflt) if isinstance(cfg, Mapping) else dflt
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        if v is not dflt:
            log.debug("resume_watch: config %s=%r is not a number; using %g", key, v, dflt)
        return dflt
    lo, hi = _LIMITS[key]
    return min(hi, max(lo, float(v)))


@dataclass(frozen=True)
class ResumeConfig:
    confirm_s: float = float(CONFIG_DEFAULTS["resume_confirm_s"])
    expire_s: float = float(CONFIG_DEFAULTS["resume_expire_s"])

    @classmethod
    def from_config(cls, cfg: Mapping | None) -> ResumeConfig:
        """Never raises: a hand-edited or old config.json falls back to the defaults key by key."""
        return cls(confirm_s=_num(cfg, "resume_confirm_s"), expire_s=_num(cfg, "resume_expire_s"))


# ---------------------------------------------------------------------------------------------------- data

Member = tuple[int, float]       # (pid, create_time): survives pid reuse


@dataclass(frozen=True)
class ResumeTarget:
    """The game/app Strata was turned off for."""
    title: str                   # for the toast ('ELDEN RING', 'Blender')
    kind: str                    # 'game' | 'app'
    game_key: str | None         # game_watch.install_of(exe, game_dirs)[0] (kind 'game'); None = app rules only
    exe: str | None              # detector.norm_path() of the root exe
    members: frozenset[Member]   # processes known at report time
    since: float                 # wall-clock time the target was recorded (time.time())


def _float(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        return None
    return float(v)


def _members(pid: Any, create_time: Any, extra: Iterable | None = None) -> frozenset[Member]:
    out: set[Member] = set()
    ct = _float(create_time)
    if isinstance(pid, int) and not isinstance(pid, bool) and ct is not None:
        out.add((int(pid), ct))
    for m in extra or ():
        try:
            p, c = m
            cf = _float(c)
            if isinstance(p, int) and not isinstance(p, bool) and cf is not None:
                out.add((int(p), cf))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


def target_for_game(game_start: Any, game_dirs: Iterable[str], now: float | None = None) -> ResumeTarget:
    """ResumeTarget from a game_watch.GameStart (title/pid/create_time/exe). The install key is computed with
    game_watch.install_of over the same game dirs the GameWatcher uses; when it does not map (game dirs changed),
    game_key is None and the app rules (member / same exe) apply."""
    exe = getattr(game_start, "exe", None)
    inst = install_of(exe, list(game_dirs or [])) if exe else None
    return ResumeTarget(title=str(getattr(game_start, "title", "") or getattr(game_start, "name", "") or "game"),
                        kind="game", game_key=inst[0] if inst else None, exe=norm_path(exe) or None,
                        members=_members(getattr(game_start, "pid", None), getattr(game_start, "create_time", None)),
                        since=time.time() if now is None else float(now))


def target_for_app(report: Any = None, *, pid: int | None = None, create_time: float | None = None,
                   exe: str | None = None, title: str | None = None, members: Iterable[Member] | None = None,
                   now: float | None = None) -> ResumeTarget:
    """ResumeTarget from a mem_watch.MemReport / AppView (pid, create_time, exe, title[, members]) or from the
    keyword fields (keywords win over the report's attributes). The root (pid, create_time) is always a member."""
    pid = pid if pid is not None else getattr(report, "pid", None)
    create_time = create_time if create_time is not None else getattr(report, "create_time", None)
    exe = exe if exe is not None else getattr(report, "exe", None)
    title = title if title is not None else (getattr(report, "title", None) or getattr(report, "name", None))
    if members is None:
        members = getattr(report, "members", None)
    return ResumeTarget(title=str(title or "app"), kind="app", game_key=None, exe=norm_path(exe) or None,
                        members=_members(pid, create_time, members),
                        since=time.time() if now is None else float(now))


# ---------------------------------------------------------------------------------------------------- pure core

def _app_running(target: ResumeTarget, procs: list[dict], tolerance_s: float,
                 session_ok: Callable[[int], bool] | None, self_pid: int | None) -> bool:
    by_pid = {int(m[0]): float(m[1]) for m in target.members}
    for info in procs:
        try:
            pid = info.get("pid")
            if pid is None:
                continue
            pid = int(pid)
            want = by_pid.get(pid)
            if want is not None:
                ct = _float(info.get("create_time"))
                if ct is not None and abs(ct - want) <= tolerance_s:
                    return True                     # a recorded member is alive (same pid AND same start)
            if target.exe and pid != self_pid:
                exe = info.get("exe")
                if exe and norm_path(str(exe)) == target.exe and (session_ok is None or session_ok(pid)):
                    return True                     # same program running again (restart / relaunch via a stub)
        except Exception as exc:  # one bad entry never breaks the check
            log.debug("resume_watch: skipped a process entry: %s", exc)
    return False


def is_running(target: ResumeTarget, procs: Iterable, game_dirs: Iterable[str], tolerance_s: float = CT_TOLERANCE_S,
               *, game_exclude: Iterable[str] = (), session_ok: Callable[[int], bool] | None = None,
               self_pid: int | None = None) -> bool:
    """Is the target running in this snapshot (dicts like game_watch.snapshot(): pid/name/exe/create_time)?

    * Any target: a recorded member is alive (pid AND create_time within tolerance_s, so a reused pid does not
      count), or a process with the same normalised exe path exists (session_ok-filtered; self_pid never counts).
    * kind 'game' with a game_key, additionally: any process game_watch maps to the same install key - with
      game_watch's own exclusions (LAUNCHER_NAMES, game_exclude, ignored installs/segments), so a leftover
      launcher or crash handler of the install does not keep the game 'running'.
    Fake procs: no session filter unless session_ok is given (like game_watch.scan_once)."""
    plist = []
    for raw in procs or ():
        try:
            plist.append(_info_of(raw))
        except Exception as exc:  # noqa: BLE001
            log.debug("resume_watch: skipped a process entry: %s", exc)
    if target.kind == "game" and target.game_key:
        dirs = [str(d) for d in (game_dirs or []) if d and str(d).strip()]
        for _info, key, _title in _game_procs(plist, dirs, _exclusions(game_exclude), session_ok):
            if key == target.game_key:
                return True
    return _app_running(target, plist, float(tolerance_s), session_ok, self_pid)


@dataclass(frozen=True)
class ResumeState:
    """Per-watch state of decide(). Times are on the watcher's clock (monotonic by default)."""
    start: float                             # when watch() began
    seen_running: bool = False               # the target was observed running at least once
    missing_since: float | None = None       # first 'not running' observation of the current window
    last_obs: float | None = None            # the previous observation
    closed_for: float = 0.0                  # seconds not running when 'closed' was reported
    done: bool = False                       # reported or expired: nothing more to say


def decide(state: ResumeState, running: bool, now: float, *, confirm_s: float = CONFIG_DEFAULTS["resume_confirm_s"],
           max_gap_s: float = MAX_GAP_S, expire_s: float = CONFIG_DEFAULTS["resume_expire_s"]
           ) -> tuple[bool, ResumeState]:
    """Pure decision over one observation. Returns (closed, new_state); never mutates state.

    'closed' is True exactly once: when the target was NOT running in consecutive observations (at most max_gap_s
    apart) spanning at least confirm_s. Running again restarts the window; a larger gap (PC asleep, stalled thread)
    or the clock going back restarts it too. A target never seen running counts the same way (it was already gone).
    After expire_s since state.start the watch ends silently: (False, done=True)."""
    if state.done:
        return False, state
    if now - state.start >= expire_s:
        return False, dataclasses.replace(state, done=True)
    if running:
        return False, dataclasses.replace(state, seen_running=True, missing_since=None, last_obs=now)
    since, last = state.missing_since, state.last_obs
    if since is None or last is None or now - last > max_gap_s or now < last:
        since = now
    held = now - since
    if held + 1e-6 >= confirm_s:
        return True, dataclasses.replace(state, missing_since=since, last_obs=now, closed_for=held, done=True)
    return False, dataclasses.replace(state, missing_since=since, last_obs=now)


# ---------------------------------------------------------------------------------------------------- watcher

_REAL = object()                  # session_ok default: current-session filter for the real snapshot, none for fakes


class ResumeWatcher:
    """Background 'resume-watch' thread around is_running() + decide(). Observe only.

    game_dirs / game_exclude / cfg may be values or zero-argument callables (read on every pass, so settings edits
    apply live). cfg supplies resume_confirm_s / resume_expire_s; confirm_s / expire_s / max_gap_s arguments
    override it (tests). clock() drives decide() (monotonic by default); wait(event, seconds) is the sleep seam."""

    def __init__(self, game_dirs: Iterable[str] | Callable[[], Iterable[str]] | None,
                 on_closed: Callable[[ResumeTarget, float], None] | None = None, *,
                 game_exclude: Iterable[str] | Callable[[], Iterable[str]] = (),
                 cfg: Mapping | Callable[[], Mapping] | None = None,
                 interval_s: float = INTERVAL_S,
                 snapshot_fn: Callable[[], Iterable] | None = None,
                 session_ok: Any = _REAL,
                 confirm_s: float | None = None, expire_s: float | None = None, max_gap_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[threading.Event, float | None], Any] | None = None,
                 self_pid: int | None = None) -> None:
        self._game_dirs = game_dirs
        self._game_exclude = game_exclude
        self._cfg = cfg
        self.on_closed = on_closed
        self.interval_s = max(0.01, float(interval_s))
        self._snapshot = snapshot_fn if snapshot_fn is not None else game_watch.snapshot
        if session_ok is _REAL:
            session_ok = current_session_filter() if snapshot_fn is None else None
        self._session_ok: Callable[[int], bool] | None = session_ok
        self._confirm_s, self._expire_s, self._max_gap_s = confirm_s, expire_s, max_gap_s
        self._clock = clock
        self._wait = wait or (lambda ev, t: ev.wait(t))
        self.self_pid = os.getpid() if self_pid is None else self_pid
        self._lock = threading.Lock()
        self._cb_lock = threading.RLock()        # held around on_closed; cancel()/watch() take it first
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gen = 0                            # bumped by every watch()/cancel()
        self._target: ResumeTarget | None = None
        self._state: ResumeState | None = None
        self.last_scan_ms: float | None = None
        self.scans = 0

    # ---- public API ---------------------------------------------------------------------------------

    @property
    def watching(self) -> bool:
        return self._target is not None

    @property
    def target(self) -> ResumeTarget | None:
        return self._target

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="resume-watch", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            t = self._thread
            self._stop.set()
            self._wake.set()
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
            if t.is_alive():
                log.warning("resume_watch: thread did not stop within %.1fs", timeout)

    def watch(self, target: ResumeTarget) -> None:
        """Report when `target` has closed. Replaces any earlier target. O(1) (waits only for a callback that is
        running right now, which must be quick - main.py's on_closed just posts to the Tk thread)."""
        with self._cb_lock, self._lock:
            self._gen += 1
            self._target = target
            self._state = ResumeState(start=self._clock())
            self._wake.set()
        log.info("resume_watch: watching %s (%s%s)", target.title, target.kind,
                 f", {len(target.members)} process(es)" if target.members else "")

    def cancel(self) -> None:
        """No further reports. When it returns, on_closed is not running and will not run for the old target."""
        with self._cb_lock, self._lock:
            was = self._target
            self._gen += 1
            self._target = None
            self._state = None
            self._wake.set()
        if was is not None:
            log.info("resume_watch: stopped watching %s", was.title)

    # ---- thread -------------------------------------------------------------------------------------

    def _value(self, src: Any) -> Any:
        try:
            return src() if callable(src) else src
        except Exception:  # noqa: BLE001
            log.debug("resume_watch: settings provider failed", exc_info=True)
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._target is None:
                    self._wait(self._wake, None)        # idle: no scanning while nothing is watched
                    self._wake.clear()
                    continue
                delay = self._tick()
            except Exception:
                log.exception("resume_watch: pass failed")
                delay = self.interval_s
            if self._stop.is_set():
                break
            self._wait(self._wake, delay)
            self._wake.clear()

    def _tick(self) -> float:
        """One watched pass: one snapshot, one decision, at most one report. Returns the delay before the next."""
        with self._lock:
            target, gen, st = self._target, self._gen, self._state
        if target is None or st is None:
            return 0.0
        t0 = time.perf_counter()
        try:
            procs = list(self._snapshot() or ())
        except Exception:
            log.exception("resume_watch: process snapshot failed")
            return self.interval_s                      # no observation: never counts as 'not running'
        if not procs:                                   # an empty process table is a failed snapshot, not 'closed'
            log.debug("resume_watch: empty process snapshot ignored")
            return self.interval_s
        dirs = [str(d) for d in (self._value(self._game_dirs) or []) if d and str(d).strip()]
        excl = [n for n in (self._value(self._game_exclude) or []) if isinstance(n, str) and n]
        running = is_running(target, procs, dirs, CT_TOLERANCE_S, game_exclude=excl, session_ok=self._session_ok,
                             self_pid=self.self_pid)
        c = self._value(self._cfg)
        rc = ResumeConfig.from_config(c if isinstance(c, Mapping) else None)
        confirm = rc.confirm_s if self._confirm_s is None else float(self._confirm_s)
        expire = rc.expire_s if self._expire_s is None else float(self._expire_s)
        gap = max(MAX_GAP_S, 2.0 * self.interval_s) if self._max_gap_s is None else float(self._max_gap_s)
        closed, new = decide(st, running, self._clock(), confirm_s=confirm, max_gap_s=gap, expire_s=expire)
        self.last_scan_ms = (time.perf_counter() - t0) * 1000.0
        self.scans += 1
        with self._lock:
            if self._gen != gen:
                return 0.0                              # cancel()/watch() meanwhile: drop this pass
            self._state = new
            if new.done:
                self._target = None
                self._state = None
        if new.done and not closed:
            log.info("resume_watch: %s still not closed after %.0f s; watch ended", target.title, expire)
            return 0.0
        if closed:
            with self._cb_lock:
                with self._lock:
                    if self._gen != gen or self._stop.is_set():
                        return 0.0                      # a late report after cancel()/watch()/stop(): suppressed
                log.info("resume_watch: %s closed (not running for %.0f s)", target.title, new.closed_for)
                cb = self.on_closed
                if cb is not None:
                    try:
                        cb(target, new.closed_for)
                    except Exception:
                        log.exception("resume_watch: on_closed callback failed")
            return 0.0
        return self.interval_s
