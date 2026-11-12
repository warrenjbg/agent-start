"""Game-start watcher for Agent Start (observe only: this module never kills, closes or signals anything).

While Strata is serving, the app arms a GameWatcher. It notices when the user STARTS a game (an exe under a
configured game dir, e.g. ...\\steamapps\\common\\<Game>\\game.exe) so the UI can offer to turn Strata off and
free its RAM.

  * arm()     -> O(1), safe from the Tk thread. The watcher thread takes the baseline on its next pass: every
                 game install with a process created at or before the arm() moment never alerts.
  * on_game   -> called on the 'game-watch' thread, at most ONCE per game install per arm().
  * disarm()  -> no further alerts; the thread then sleeps without scanning until armed again or stopped.

scan_once() is the pure core (fake process lists in tests); GameWatcher is the thread around it.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import psutil

from detector import game_install_key, norm, norm_path, session_id_of

log = logging.getLogger("agent_start")

_MB = 1024.0 * 1024.0
_ITER_ATTRS = ["pid", "name", "exe", "create_time"]

# Launchers, store clients and helpers that live inside game dirs but are not "the user started a game".
# (A game they then launch lives in its own install folder and alerts normally.)
LAUNCHER_NAMES: frozenset[str] = frozenset({
    # Epic
    "epicgameslauncher", "epicwebhelper", "epiconlineservices", "epiconlineserviceshost",
    "epiconlineservicesuserhelper", "eosoverlayrenderer-win64-shipping", "eosoverlayrenderer-win32-shipping",
    "unrealcefsubprocess", "crashreportclient",
    # Riot
    "riotclientservices", "riotclientux", "riotclientuxrender", "riotclientcrashhandler",
    # Battle.net
    "battle.net", "battle.net launcher", "battle.net helper", "agent", "blizzardbrowser", "blizzarderror",
    # GOG Galaxy
    "galaxyclient", "galaxyclient helper", "galaxycommunication", "gog galaxy notifications renderer",
    "galaxyclientservice", "galaxyupdater",
    # generic crash reporters / redistributable installers
    "unitycrashhandler64", "unitycrashhandler32", "crashpad_handler", "vc_redist.x64", "vc_redist.x86",
    "vcredist_x64", "vcredist_x86", "dxsetup", "dotnetfx",
    # tools that live in game folders (SteamVR runtime, Unreal editor)
    "vrmonitor", "vrserver", "vrcompositor", "vrdashboard", "vrstartup", "unrealeditor", "unrealeditor-cmd",
})
# Install folders (casefolded) that are never a game; path segments that are never a game.
IGNORED_INSTALLS: frozenset[str] = frozenset({"steamworks shared", "wallpaper_engine", "steamvr"})
IGNORED_SEGMENTS: frozenset[str] = frozenset({"_commonredist"})
# Container folders that game_install_key may stop at (GOG Galaxy\\Games\\<Game>): go one level deeper.
_GENERIC_FOLDERS: frozenset[str] = frozenset({"games", "common"})


@dataclass
class GameStart:
    name: str           # exe file name, e.g. 'eldenring.exe'
    title: str          # install folder name, e.g. 'ELDEN RING' (else exe stem)
    pid: int
    create_time: float
    exe: str
    ram_mb: float       # summed RSS of this install's reportable processes at report time


# --------------------------------------------------------------------------------------------- pure helpers

def install_of(exe: str | None, game_dirs: Iterable[str]) -> tuple[str, str] | None:
    """(key, title) for a game-dir exe, else None.

    key is detector.game_install_key(exe) (normcased), refined so it identifies one game: one level deeper
    when the key stops at a generic container ('GOG Galaxy\\Games'), plus the exe name when the exe sits
    directly in the game dir. title keeps the original case of the folder (or the exe stem).
    """
    key = game_install_key(exe, game_dirs)
    if not key:
        return None
    try:
        orig = os.path.abspath(str(exe))
    except (OSError, ValueError):
        return None
    sep = os.sep
    parts = orig.split(sep)
    stem = os.path.splitext(parts[-1])[0] or parts[-1]
    if key.endswith(sep):                                  # exe directly in the game dir / 'common'
        return key + norm(parts[-1]), stem
    n = len(key.split(sep))
    if n >= len(parts):                                    # defensive: key must be a strict prefix dir
        return key, stem
    title = parts[n - 1]
    if title.casefold() in _GENERIC_FOLDERS:
        if n + 1 < len(parts):                             # a folder below the container exists
            return key + sep + os.path.normcase(parts[n]), parts[n]
        return key + sep + norm(parts[-1]), stem
    return key, title or stem


def _ignored(exe: str, key: str, title: str) -> bool:
    if title.casefold() in IGNORED_INSTALLS:
        return True
    segs = norm_path(exe).split(os.sep)
    return any(s in IGNORED_SEGMENTS for s in segs)


def _info_of(p) -> dict:
    """Normalise one process entry: psutil.Process (with .info) or a plain dict (tests)."""
    if isinstance(p, dict):
        return p
    i = getattr(p, "info", None) or {}
    return {"pid": p.pid, "name": i.get("name"), "exe": i.get("exe"), "create_time": i.get("create_time"),
            "proc": p}


def snapshot() -> list[dict]:
    """ONE psutil.process_iter pass -> [{'pid','name','exe','create_time','proc'}]. Never raises per process."""
    out: list[dict] = []
    try:
        it = psutil.process_iter(_ITER_ATTRS, ad_value=None)
        for p in it:
            try:
                out.append(_info_of(p))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as exc:  # pragma: no cover - psutil enumeration failure
        log.warning("game_watch: process_iter failed: %s", exc)
    return out


def _game_procs(procs: Iterable, game_dirs: list[str], excl: frozenset[str],
                session_ok: Callable[[int], bool] | None):
    """Yield (info, key, title) for in-session, non-excluded game-dir processes."""
    for raw in procs:
        try:
            info = _info_of(raw)
            exe, name = info.get("exe"), info.get("name") or ""
            if not exe:
                continue
            if not name:
                name = os.path.basename(str(exe))
            if norm(name) in excl or norm(os.path.basename(str(exe))) in excl:
                continue
            inst = install_of(exe, game_dirs)
            if inst is None:
                continue
            key, title = inst
            if _ignored(str(exe), key, title):
                continue
            pid = info.get("pid")
            if pid is None:
                continue
            if session_ok is not None and not session_ok(int(pid)):
                continue
            yield info, key, title
        except Exception as exc:  # one bad entry never breaks the scan
            log.debug("game_watch: skipped a process entry: %s", exc)


def _exclusions(game_exclude: Iterable[str] | None) -> frozenset[str]:
    return frozenset(norm(n) for n in (game_exclude or ()) if isinstance(n, str) and n) | LAUNCHER_NAMES


def current_session_filter() -> Callable[[int], bool]:
    """pid -> True iff pid is in this process's terminal-services session (unknown session -> False)."""
    ours = session_id_of(os.getpid())

    def ok(pid: int) -> bool:
        if ours is None:
            return False
        sid = session_id_of(pid)
        return sid is not None and sid == ours
    return ok


def baseline_keys(game_dirs: list[str], game_exclude: Iterable[str] = (), before: float | None = None,
                  procs: Iterable | None = None,
                  session_ok: Callable[[int], bool] | None = None) -> set[str]:
    """Install keys of game processes created at or before `before` (None = all). Unknown create_time counts
    as 'before' (conservative: never alert on something we cannot date). Fake procs: no session filter
    unless session_ok is given; real snapshot (procs=None): current-session filter by default."""
    if procs is None:
        procs = snapshot()
        if session_ok is None:
            session_ok = current_session_filter()
    out: set[str] = set()
    for info, key, _title in _game_procs(procs, list(game_dirs or []), _exclusions(game_exclude), session_ok):
        ct = info.get("create_time")
        if before is None or ct is None or float(ct) <= before:
            out.add(key)
    return out


def scan_once(baseline_keys: Iterable[str], game_dirs: list[str], game_exclude: Iterable[str],
              now: float, min_age_s: float, procs: Iterable | None = None, *,
              session_ok: Callable[[int], bool] | None = None,
              pending: list | None = None) -> list[GameStart]:
    """Games to report now: one GameStart per install key not in baseline_keys whose earliest live process
    is at least min_age_s old (now - create_time). Pure for fake procs (dicts with pid/name/exe/create_time
    and optional 'ram_mb' / 'proc').

    * procs=None -> one real psutil snapshot, current-session filter by default.
    * Fake procs -> no session filter unless session_ok is given.
    * Entries with a 'proc' (psutil.Process) are re-checked: if it died (NoSuchProcess) it is not reported;
      ram_mb comes from its RSS. Plain dicts use their 'ram_mb' (default 0).
    * If `pending` is a list, the keys of candidates that are still too young are appended to it.
    Sorted by create_time. The caller adds the reported keys to its baseline (once per install per arm).
    """
    if procs is None:
        procs = snapshot()
        if session_ok is None:
            session_ok = current_session_filter()
    base = set(baseline_keys or ())
    groups: dict[str, list[tuple[dict, str]]] = {}
    for info, key, title in _game_procs(procs, list(game_dirs or []), _exclusions(game_exclude), session_ok):
        if key in base:
            continue
        groups.setdefault(key, []).append((info, title))

    out: list[GameStart] = []
    for key, members in groups.items():
        dated = [(float(i["create_time"]), i, t) for i, t in members if i.get("create_time") is not None]
        if not dated:
            continue
        old = [(ct, i, t) for ct, i, t in dated if now - ct >= min_age_s]
        if not old:
            if pending is not None:
                pending.append(key)
            continue
        old.sort(key=lambda x: (x[0], int(x[1].get("pid") or 0)))
        alive: list[tuple[float, dict, str, float]] = []
        for ct, info, title in old:
            ram = _ram_mb(info)
            if ram is None:                                # died since the snapshot
                continue
            alive.append((ct, info, title, ram))
        if not alive:
            continue
        ct, info, title, _ = alive[0]
        exe = str(info.get("exe"))
        name = info.get("name") or os.path.basename(exe)
        out.append(GameStart(name=name, title=title, pid=int(info["pid"]), create_time=ct, exe=exe,
                             ram_mb=round(sum(a[3] for a in alive), 1)))
    out.sort(key=lambda g: (g.create_time, g.pid))
    return out


def _ram_mb(info: dict) -> float | None:
    """RSS in MB; None if the process is gone. Plain dicts: their 'ram_mb' (default 0)."""
    proc = info.get("proc")
    if proc is None:
        if info.get("alive") is False:
            return None
        try:
            return float(info.get("ram_mb") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return proc.memory_info().rss / _MB
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied:
        try:
            return 0.0 if proc.is_running() else None
        except psutil.Error:
            return None
    except Exception:
        return None


# --------------------------------------------------------------------------------------------- watcher

class GameWatcher:
    """Background 'game-watch' thread around scan_once(). Observe only."""

    def __init__(self, game_dirs: list[str], game_exclude: Iterable[str] = (), interval_s: float = 3.0,
                 on_game: Callable[[GameStart], None] | None = None, min_age_s: float = 2.0) -> None:
        self.game_dirs = [str(d) for d in (game_dirs or []) if d and str(d).strip()]
        self.game_exclude = frozenset(norm(n) for n in (game_exclude or ()) if isinstance(n, str) and n)
        self.interval_s = max(0.2, float(interval_s))
        self.min_age_s = max(0.0, float(min_age_s))
        self.on_game = on_game
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._armed = False
        self._gen = 0                       # bumped by every arm()/disarm()
        self._arm_time = 0.0
        self._seen: set[str] | None = None  # baseline + reported keys for the current arm (None = no baseline yet)
        self._session_ok = current_session_filter()
        self.last_scan_ms: float | None = None
        self.scans = 0

    # ---- public API ---------------------------------------------------------------------------------

    @property
    def armed(self) -> bool:
        return self._armed

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="game-watch", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            t = self._thread
            self._stop.set()
            self._wake.set()
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
            if t.is_alive():
                log.warning("game_watch: thread did not stop within %.1fs", timeout)

    def arm(self) -> None:
        """Start alerting on games started from now on; anything running now never alerts. O(1)."""
        with self._lock:
            self._gen += 1
            self._armed = True
            self._arm_time = time.time()
            self._seen = None
            self._wake.set()
        log.info("game_watch: armed")

    def disarm(self) -> None:
        with self._lock:
            was = self._armed
            self._gen += 1
            self._armed = False
            self._seen = None
            self._wake.set()
        if was:
            log.info("game_watch: disarmed")

    # ---- thread -------------------------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._armed:
                    self._wake.wait()                       # idle: no scanning while disarmed
                    self._wake.clear()
                    continue
                delay = self._tick()
            except Exception:
                log.exception("game_watch: scan failed")
                delay = self.interval_s
            if self._stop.is_set():
                break
            self._wake.wait(delay)
            self._wake.clear()

    def _tick(self) -> float:
        """One armed pass. Returns the delay before the next one."""
        with self._lock:
            if not self._armed:
                return 0.0
            gen, arm_time, seen = self._gen, self._arm_time, self._seen
        t0 = time.perf_counter()
        procs = snapshot()
        if seen is None:
            seen = baseline_keys(self.game_dirs, self.game_exclude, before=arm_time, procs=procs,
                                 session_ok=self._session_ok)
            with self._lock:
                if self._gen != gen:
                    return 0.0
                self._seen = seen
            log.info("game_watch: baseline %d game install(s)", len(seen))
        pending: list[str] = []
        found = scan_once(seen, self.game_dirs, self.game_exclude, time.time(), self.min_age_s, procs,
                          session_ok=self._session_ok, pending=pending)
        self.last_scan_ms = (time.perf_counter() - t0) * 1000.0
        self.scans += 1
        for g in found:
            key = install_of(g.exe, self.game_dirs)
            with self._lock:
                if self._gen != gen or not self._armed or self._stop.is_set():
                    return 0.0
                if key is not None:
                    if key[0] in self._seen:
                        continue
                    self._seen.add(key[0])
            log.info("game_watch: game started: %s (%s, pid %d, %.0f MB)", g.title, g.name, g.pid, g.ram_mb)
            cb = self.on_game
            if cb is not None:
                try:
                    cb(g)
                except Exception:
                    log.exception("game_watch: on_game callback failed")
        if pending:                                         # re-check young candidates as soon as they age
            return max(0.25, min(self.interval_s, self.min_age_s + 0.1))
        return self.interval_s
