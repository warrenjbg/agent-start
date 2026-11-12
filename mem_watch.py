"""Memory-pressure watcher for Agent Start (observe only: never kills, closes, signals or suspends anything).

While Strata is serving, two things are reported, each once per app, so the UI can offer (or do) the same safe
turn-off it uses for games:
  * EVICTED (the firm rule): Strata's own memory in RAM fell below half of its peak, the memory it lost did not
    come back as available memory (so it was taken, not given up by an unload), and a new app is running. Measured
    on a 64 GB PC: a game pushed a 49 GB model into the pagefile within ~10 s of starting, long before the game
    itself looked large. This fires after the fact - it rescues the PC and the app, not the loaded model.
  * PRESSURE: a NEW app (started after Strata became ready) holds a lot of RAM while the PC's available memory has
    fallen well below what it was when Strata became ready.

It runs inside the game watcher's 3-second pass (game_watch.GameWatcher(co_scan=MemWatcher(...))): the watcher takes
ONE process snapshot per tick and both checks read it. The memory check adds one NtQuerySystemInformation call
(parent pid, working set and session of every process in a single kernel call, ~2 ms) instead of asking psutil for
each process's memory (~250 ms on a typical PC, because every access-denied process costs a full system query).

Rules (all reused from detector.py, never re-implemented):
  * grouping: detector.build_apps() over detector.ProcInfo graphs -> one App per process tree (a browser and its
    children are one app; memory = the tree's summed working sets);
  * protection: the same GraphContext the detector builds (own tree, Strata, TREE_PROTECTED + the user's protected
    list, SYSTEM_PROTECTED, %SystemRoot%, other sessions) - protected apps are never candidates.
The pure core is decide() (and evaluate() = group_apps() + decide()); tests drive it with fake process lists.

  * arm(baseline_avail_mb=None) -> O(1), safe from the Tk thread. Call it once Strata is READY (model loaded). The
                 baseline (available RAM and Strata's own RAM) is taken on the first pass after arm(), unless given.
  * on_app      -> called on the 'game-watch' thread, at most ONCE per app per arm().
  * disarm()    -> no further reports.
"""
from __future__ import annotations

import ctypes
import dataclasses
import logging
import math
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, NamedTuple

import psutil

from detector import (SYSTEM_PROTECTED, TREE_PROTECTED, GraphContext, ProcGraph, ProcInfo, build_apps,
                      is_strata_server_cmdline, norm, session_id_of, under_dir)
from game_watch import install_of

if TYPE_CHECKING:
    from game_watch import GameWatcher

log = logging.getLogger("agent_start")

_MB = 1024.0 * 1024.0
CT_TOLERANCE_S = 1.0             # psutil vs kernel create_time of the same process (measured: identical)
MAX_GAP_S = 10.0                 # two observations further apart than this do not make a sustained period
PROVIDER_REFRESH_S = 30.0        # how often the protected-pids provider (Strata's own pids) is asked again

# ---------------------------------------------------------------------------------------------------- config

# config.json keys (missing key = default; bad value = default; numbers are clamped). The evict_* defaults come
# from two 5-7 minute measurements on a 64 GB PC serving a 49 GB model (idle, then a game starting); the pressure
# defaults are estimates - on that PC the pressure rule did not fire, the eviction rule would have at ~+13 s.
CONFIG_DEFAULTS: dict[str, float] = {
    "mem_watch_app_mb": 4096,                 # the new app's tree must hold at least this much RAM
    "mem_watch_sustain_s": 15,                # ... and both conditions must hold this long without a break
    "mem_watch_floor_baseline_pct": 50,       # floor = this % of the available RAM measured when Strata was ready
    "mem_watch_floor_total_pct": 6,           # ... but never above this % of physical RAM (64 GB -> ~3.9 GB)
    "mem_watch_evict_pct": 50,                # evicted = Strata's RAM below this % of its peak since it was ready
    "mem_watch_evict_sustain_s": 6,           # ... for this long (3 passes), with the lost memory not available
    "mem_watch_evict_app_mb": 512,            # ... and a new app at least this large to name as the cause
}
EVICT_MIN_PEAK_MB = 1024.0       # an engine that never held this much is not judged (nothing worth rescuing)
EVICT_RETURNED_SHARE = 0.5       # an unload gives its memory back: >= this share of the loss shows up as available
_LIMITS: dict[str, tuple[float, float]] = {
    "mem_watch_app_mb": (1024, 1024 * 1024),  # >= 1 GB: build_apps only lists windowless trees above 1000 MB
    "mem_watch_sustain_s": (3, 3600),
    "mem_watch_floor_baseline_pct": (1, 100),
    "mem_watch_floor_total_pct": (0.5, 50),
    "mem_watch_evict_pct": (5, 95),
    "mem_watch_evict_sustain_s": (3, 3600),
    "mem_watch_evict_app_mb": (64, 1024 * 1024),
}


def _num(cfg: Mapping | None, key: str) -> float:
    """cfg[key] as a finite float clamped to its limits; the default for a missing/bad value (bool, str, NaN...)."""
    dflt = float(CONFIG_DEFAULTS[key])
    v = cfg.get(key, dflt) if isinstance(cfg, Mapping) else dflt
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        if v is not dflt:
            log.debug("mem_watch: config %s=%r is not a number; using %g", key, v, dflt)
        return dflt
    lo, hi = _LIMITS[key]
    return min(hi, max(lo, float(v)))


@dataclass(frozen=True)
class MemConfig:
    app_mb: float = float(CONFIG_DEFAULTS["mem_watch_app_mb"])
    sustain_s: float = float(CONFIG_DEFAULTS["mem_watch_sustain_s"])
    floor_baseline_pct: float = float(CONFIG_DEFAULTS["mem_watch_floor_baseline_pct"])
    floor_total_pct: float = float(CONFIG_DEFAULTS["mem_watch_floor_total_pct"])
    evict_pct: float = float(CONFIG_DEFAULTS["mem_watch_evict_pct"])
    evict_sustain_s: float = float(CONFIG_DEFAULTS["mem_watch_evict_sustain_s"])
    evict_app_mb: float = float(CONFIG_DEFAULTS["mem_watch_evict_app_mb"])

    @classmethod
    def from_config(cls, cfg: Mapping | None) -> MemConfig:
        """Never raises: a hand-edited or old config.json falls back to the defaults key by key."""
        return cls(app_mb=_num(cfg, "mem_watch_app_mb"), sustain_s=_num(cfg, "mem_watch_sustain_s"),
                   floor_baseline_pct=_num(cfg, "mem_watch_floor_baseline_pct"),
                   floor_total_pct=_num(cfg, "mem_watch_floor_total_pct"),
                   evict_pct=_num(cfg, "mem_watch_evict_pct"),
                   evict_sustain_s=_num(cfg, "mem_watch_evict_sustain_s"),
                   evict_app_mb=_num(cfg, "mem_watch_evict_app_mb"))


def floor_mb(baseline_avail_mb: float, total_mb: float, mc: MemConfig) -> float:
    """Available-RAM floor: min(baseline x floor_baseline_pct, total x floor_total_pct), never below 0.

    The baseline share makes the floor follow each PC (a 64 GB PC with a big model may have ~3 GB available when
    Strata is ready, one with a small model ~20 GB); the total-RAM cap stops a large baseline from calling 10 GB of
    free RAM "tight". total_mb <= 0 (unknown) drops the cap."""
    f = max(0.0, float(baseline_avail_mb)) * mc.floor_baseline_pct / 100.0
    if total_mb > 0:
        f = min(f, float(total_mb) * mc.floor_total_pct / 100.0)
    return max(0.0, f)


# ---------------------------------------------------------------------------------------------------- data

AppKey = tuple[int, float]       # (root pid, root create_time): survives pid reuse


@dataclass(frozen=True)
class AppView:
    """One candidate app tree as grouped by detector.build_apps (only unprotected apps exist here)."""
    key: AppKey
    pid: int
    create_time: float
    name: str                    # the root's process name, as the detector shows it ('Blender.exe')
    title: str                   # for messages: game install folder, else the exe stem ('Blender')
    exe: str
    ram_mb: float                # summed working sets of the tree (detector rules: system members excluded)
    members: frozenset[AppKey]   # (pid, create_time) of every member
    game_keys: frozenset[str]    # game_watch install keys of members that live in a game dir


@dataclass(frozen=True)
class MemReport:
    name: str
    title: str
    pid: int
    create_time: float
    exe: str
    ram_mb: float                # the app tree's RAM at report time
    member_count: int
    avail_mb: float              # available RAM at report time (psutil.virtual_memory().available)
    avail_eff_mb: float          # avail_mb + Strata's own growth since the baseline (what the floor is compared to)
    floor_mb: float
    baseline_avail_mb: float
    app_threshold_mb: float
    sustained_s: float
    reason: str = "pressure"     # "pressure" | "evicted"
    strata_mb: float | None = None       # evicted: Strata's RAM now ...
    strata_peak_mb: float | None = None  # ... and its peak since it was ready

    def why(self) -> str:
        if self.reason == "evicted":
            return (f"Strata's memory in RAM fell to {self.strata_mb or 0:.0f} MB "
                    f"(peak {self.strata_peak_mb or 0:.0f}) "
                    f"with {self.avail_mb:.0f} MB available for {self.sustained_s:.0f} s; largest new app: "
                    f"{self.title} ({self.ram_mb:.0f} MB)")
        return (f"{self.title} uses {self.ram_mb:.0f} MB (>= {self.app_threshold_mb:.0f}) and available RAM is "
                f"{self.avail_eff_mb:.0f} MB (< floor {self.floor_mb:.0f}; {self.avail_mb:.0f} MB raw, "
                f"{self.baseline_avail_mb:.0f} MB when Strata was ready) for {self.sustained_s:.0f} s")


@dataclass
class MemState:
    """Per-arm state of decide(). A new MemState (re-arm) forgets every report and timer."""
    arm_time: float
    baseline_avail_mb: float | None = None
    baseline_strata_mb: float | None = None
    peak_strata_mb: float | None = None      # the most RAM Strata held since the baseline
    evict_since: float | None = None         # the eviction condition held since
    evict_last: float | None = None
    evict_reported: bool = False             # the eviction is reported once per arm (one cause is named)
    since: dict[AppKey, float] = field(default_factory=dict)       # both conditions held since
    last_seen: dict[AppKey, float] = field(default_factory=dict)
    reported: set[AppKey] = field(default_factory=set)              # apps reported (or claimed as games) this arm
    reported_members: set[AppKey] = field(default_factory=set)      # their processes (root dies -> children stay)

    def copy(self) -> MemState:
        return dataclasses.replace(self, since=dict(self.since), last_seen=dict(self.last_seen),
                                   reported=set(self.reported), reported_members=set(self.reported_members))


# ---------------------------------------------------------------------------------------------------- pure core

def _is_claimed(game_claimed: Callable[[str], bool] | Iterable[str] | None) -> Callable[[str], bool]:
    if game_claimed is None:
        return lambda _k: False
    if callable(game_claimed):
        return game_claimed
    keys = frozenset(game_claimed)
    return keys.__contains__


def absorb_reported(state: MemState, apps: Iterable[AppView],
                    game_claimed: Callable[[str], bool] | Iterable[str] | None = None) -> list[AppView]:
    """Mark (in place) every app that is the same app as one already reported - same root, or sharing a process
    with it (the root died and its children were regrouped) - or that contains a game the folder watcher already
    reported. Returns the apps that are 'reported' after this step (their game keys must stay claimed)."""
    claimed = _is_claimed(game_claimed)
    out: list[AppView] = []
    for a in apps:
        if (a.key in state.reported or not a.members.isdisjoint(state.reported_members)
                or any(claimed(k) for k in a.game_keys)):
            state.reported.add(a.key)
            state.reported_members |= a.members
            state.since.pop(a.key, None)
            state.last_seen.pop(a.key, None)
            out.append(a)
    return out


def decide(state: MemState, apps: Iterable[AppView], now: float, avail_mb: float, total_mb: float,
           cfg: MemConfig, *, strata_mb: float | None = None,
           game_claimed: Callable[[str], bool] | Iterable[str] | None = None,
           max_gap_s: float = MAX_GAP_S) -> tuple[list[MemReport], MemState]:
    """Pure decision: which apps to report now. Never mutates `state`; returns (reports, new_state).

    An app is reported when, continuously for cfg.sustain_s (consecutive observations at most max_gap_s apart):
      * its root started after the arm moment (state.arm_time) - an app running before Strata was ready never
        triggers, even if it grows later;
      * its tree holds >= cfg.app_mb;
      * available RAM, corrected for Strata's own growth since the baseline, is below floor_mb(baseline, total);
    and it is not already reported / claimed as a game this arm. `apps` must already exclude protected apps
    (group_apps does that with the detector's rules). The first call after arm (baseline unknown) only records
    the baseline: available RAM and Strata's RAM now.

    Independently, the largest such new app (>= cfg.evict_app_mb) is reported with reason "evicted" when, for
    cfg.evict_sustain_s, Strata's RAM is below cfg.evict_pct of its peak AND less than half of the memory it lost
    is available (an unload returns its memory; an eviction does not).
    """
    st = state.copy()
    apps = list(apps)
    if st.baseline_avail_mb is None:
        st.baseline_avail_mb = float(avail_mb)
        st.baseline_strata_mb = strata_mb
        st.peak_strata_mb = strata_mb
        return [], st
    absorb_reported(st, apps, game_claimed)
    evicted_reports = _decide_evicted(st, apps, now, float(avail_mb), cfg, strata_mb, max_gap_s)
    eff = float(avail_mb)
    if strata_mb is not None and st.baseline_strata_mb is not None:
        eff += max(0.0, float(strata_mb) - float(st.baseline_strata_mb))
    flo = floor_mb(st.baseline_avail_mb, total_mb, cfg)
    pressure = eff < flo
    reports: list[MemReport] = list(evicted_reports)
    present: set[AppKey] = set()
    for a in sorted(apps, key=lambda x: (x.create_time, x.pid)):
        present.add(a.key)
        if a.key in st.reported:
            continue
        hot = pressure and a.create_time > st.arm_time and a.ram_mb >= cfg.app_mb
        if not hot:
            st.since.pop(a.key, None)
            st.last_seen.pop(a.key, None)
            continue
        last = st.last_seen.get(a.key)
        if a.key not in st.since or last is None or now - last > max_gap_s or now < last:
            st.since[a.key] = now
        st.last_seen[a.key] = now
        held = now - st.since[a.key]
        if held + 1e-6 >= cfg.sustain_s:
            reports.append(MemReport(
                name=a.name, title=a.title, pid=a.pid, create_time=a.create_time, exe=a.exe,
                ram_mb=round(a.ram_mb, 1), member_count=len(a.members), avail_mb=round(float(avail_mb), 1),
                avail_eff_mb=round(eff, 1), floor_mb=round(flo, 1), baseline_avail_mb=round(st.baseline_avail_mb, 1),
                app_threshold_mb=cfg.app_mb, sustained_s=round(held, 1)))
            st.reported.add(a.key)
            st.reported_members |= a.members
            st.since.pop(a.key, None)
            st.last_seen.pop(a.key, None)
    for k in list(st.since):
        if k not in present:
            st.since.pop(k, None)
            st.last_seen.pop(k, None)
    return reports, st


def _decide_evicted(st: MemState, apps: list[AppView], now: float, avail_mb: float, cfg: MemConfig,
                    strata_mb: float | None, max_gap_s: float) -> list[MemReport]:
    """The eviction rule of decide(); updates `st` (decide's private copy) in place."""
    if strata_mb is None or st.evict_reported:   # Strata's processes are not visible / already reported this arm
        st.evict_since = st.evict_last = None
        return []
    cur = float(strata_mb)
    peak = max(cur, float(st.peak_strata_mb or 0.0))
    st.peak_strata_mb = peak
    lost = peak - cur
    base_avail = float(st.baseline_avail_mb or 0.0)
    out_of_ram = (peak >= EVICT_MIN_PEAK_MB and cur < peak * cfg.evict_pct / 100.0
                  and avail_mb < base_avail + lost * EVICT_RETURNED_SHARE)
    if not out_of_ram:
        st.evict_since = st.evict_last = None
        return []
    if st.evict_since is None or st.evict_last is None or now - st.evict_last > max_gap_s or now < st.evict_last:
        st.evict_since = now
    st.evict_last = now
    held = now - st.evict_since
    if held + 1e-6 < cfg.evict_sustain_s:
        return []
    new = [a for a in apps if a.key not in st.reported and a.create_time > st.arm_time and a.ram_mb >= cfg.evict_app_mb]
    if not new:                                  # nobody to name (a protected or older app): stay quiet, keep timing
        return []
    a = max(new, key=lambda x: (x.ram_mb, -x.create_time, -x.pid))
    st.evict_reported = True
    st.reported.add(a.key)
    st.reported_members |= a.members
    st.since.pop(a.key, None)
    st.last_seen.pop(a.key, None)
    return [MemReport(
        name=a.name, title=a.title, pid=a.pid, create_time=a.create_time, exe=a.exe, ram_mb=round(a.ram_mb, 1),
        member_count=len(a.members), avail_mb=round(avail_mb, 1), avail_eff_mb=round(avail_mb, 1), floor_mb=0.0,
        baseline_avail_mb=round(base_avail, 1), app_threshold_mb=cfg.evict_app_mb, sustained_s=round(held, 1),
        reason="evicted", strata_mb=round(cur, 1), strata_peak_mb=round(peak, 1))]


def group_apps(procs: Mapping[int, ProcInfo], ctx: GraphContext) -> list[AppView]:
    """detector.build_apps() without the inputs the memory check does not need (window list, GPU counters).

    Without windows/VRAM, build_apps lists a tree as an app when it is a game-dir tree (>= min_game_ram_mb) or holds
    more than BACKGROUND_MIN_RAM_MB (1000 MB) - every tree that could reach mem_watch_app_mb (>= 1024) is listed."""
    lean = dataclasses.replace(ctx, windows={}, vram_mb=None, vram_cap_mb=None)
    pmap = dict(procs)
    out: list[AppView] = []
    for a in build_apps(pmap, lean):
        members = []
        gkeys = set()
        for m in a.member_pids:
            info = pmap.get(m)
            if info is None or info.create_time is None:
                continue
            members.append((int(m), float(info.create_time)))
            inst = install_of(info.exe, ctx.game_dirs) if info.exe else None
            if inst is not None:
                gkeys.add(inst[0])
        root_inst = install_of(a.exe, ctx.game_dirs) if a.exe else None
        stem = os.path.splitext(a.name)[0] if a.name.casefold().endswith(".exe") else a.name
        out.append(AppView(key=(int(a.pid), float(a.create_time)), pid=int(a.pid), create_time=float(a.create_time),
                           name=a.name, title=(root_inst[1] if root_inst else stem) or a.name, exe=a.exe,
                           ram_mb=float(a.ram_mb), members=frozenset(members), game_keys=frozenset(gkeys)))
    return out


def _engine_owned(info: ProcInfo, ctx: GraphContext) -> bool:
    """The detector's engine rule: ctx.engine_protects when the detector provides one (falls back to the built-in
    Strata rule on error, like detector._engine_process), else serve\\server.py in the cmdline / exe under
    strata_dir."""
    builtin = is_strata_server_cmdline(info.cmdline) or under_dir(info.exe, ctx.strata_dir)
    fn = getattr(ctx, "engine_protects", None)
    if fn is None:
        return builtin
    try:
        return bool(fn(info))
    except Exception:  # noqa: BLE001
        return builtin


def strata_mb(procs: Mapping[int, ProcInfo], ctx: GraphContext) -> float | None:
    """RAM of Strata's own processes (server.py processes, exes under strata_dir, provider pids, + descendants);
    None when none is found. Used only to keep Strata's own growth out of the available-RAM condition."""
    pmap = dict(procs)
    seeds = {pid for pid, i in pmap.items() if _engine_owned(i, ctx)}
    seeds |= {p for p in ctx.extra_hard_pids if p in pmap}
    if not seeds:
        return None
    graph = ProcGraph(pmap)
    tree = set(seeds)
    for s in seeds:
        tree |= graph.descendants(s)
    return sum(max(0, pmap[p].rss or 0) for p in tree) / _MB


def evaluate(state: MemState, procs: Mapping[int, ProcInfo], ctx: GraphContext, now: float, avail_mb: float,
             total_mb: float, cfg: MemConfig, *, game_claimed: Callable[[str], bool] | Iterable[str] | None = None,
             max_gap_s: float = MAX_GAP_S) -> tuple[list[MemReport], MemState]:
    """group_apps() + decide() over one process table (the scan_once of this module)."""
    return decide(state, group_apps(procs, ctx), now, avail_mb, total_mb, cfg, strata_mb=strata_mb(procs, ctx),
                  game_claimed=game_claimed, max_gap_s=max_gap_s)


# ---------------------------------------------------------------------------------------------------- inputs

class SysProc(NamedTuple):
    ppid: int
    create_time: float | None
    session_id: int
    wset: int                    # working set, bytes (== psutil memory_info().rss on Windows)


def graph_context(cfg: Mapping | None, *, self_pid: int | None, extra_hard_pids: Iterable[int] = (),
                  session_ok: Callable[[int], bool] = lambda pid: True, system_root: str | None = None,
                  engine_protects: Callable[[ProcInfo], bool] | None = None) -> GraphContext:
    """The GraphContext ProcessDetector._build() assembles (whitelist INCLUDED), minus windows and GPU data.

    Mirrors detector.py field by field; see the report for the proposed shared helper in detector.py.
    engine_protects is set only when the detector's GraphContext has that field (engine abstraction)."""
    c: Mapping = cfg if isinstance(cfg, Mapping) else {}

    def f(key: str, dflt: float) -> float:
        v = c.get(key, dflt)
        try:
            out = float(v)
        except (TypeError, ValueError):
            return dflt
        return out if math.isfinite(out) else dflt

    def names(key: str) -> list[str]:
        v = c.get(key) or []
        return [n for n in v if isinstance(n, str) and n] if isinstance(v, (list, tuple)) else []

    wl = {norm(n) for n in names("user_whitelist")}
    if system_root is None:
        system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    ctx = GraphContext(
        self_pid=self_pid, extra_hard_pids={int(p) for p in extra_hard_pids},
        tree_protected=frozenset(TREE_PROTECTED | wl), system_protected=SYSTEM_PROTECTED,
        strata_dir=str(c.get("strata_dir") or ""), system_root=system_root,
        game_dirs=[str(d) for d in names("game_dirs")],
        min_ram_mb=f("min_ram_mb", 250), min_vram_mb=f("min_vram_mb", 200), min_game_ram_mb=f("min_game_ram_mb", 50),
        game_exclude=frozenset(norm(n) for n in names("game_exclude")), session_ok=session_ok)
    if engine_protects is not None and "engine_protects" in {f_.name for f_ in dataclasses.fields(GraphContext)}:
        ctx = dataclasses.replace(ctx, engine_protects=engine_protects)
    return ctx


def build_procinfos(snap: Iterable[Mapping], table: Mapping[int, SysProc] | None) -> dict[int, ProcInfo]:
    """Merge the shared psutil snapshot (pid/name/exe/create_time[/proc]) with the kernel table (ppid/working set).

    The kernel row is used only when its create_time matches (pid reuse between the two reads -> ppid None,
    rss 0). Processes psutil could not date or read keep exe/create_time None, which the detector rules treat as
    protected."""
    out: dict[int, ProcInfo] = {}
    tab = table or {}
    for raw in snap:
        try:
            pid = raw.get("pid")
            if pid is None:
                continue
            pid = int(pid)
            ct = raw.get("create_time")
            ct = float(ct) if isinstance(ct, (int, float)) and not isinstance(ct, bool) else None
            row = tab.get(pid)
            ppid: int | None = None
            rss = 0
            if row is not None and ct is not None and row.create_time is not None \
                    and abs(row.create_time - ct) <= CT_TOLERANCE_S:
                ppid, rss = int(row.ppid), int(row.wset)
            exe = raw.get("exe")
            out[pid] = ProcInfo(pid=pid, name=str(raw.get("name") or ""), exe=str(exe) if exe else None, ppid=ppid,
                                create_time=ct, rss=rss, cmdline=None, proc=raw.get("proc"))
        except Exception as exc:  # one bad entry never breaks the scan
            log.debug("mem_watch: skipped a process entry: %s", exc)
    return out


# One NtQuerySystemInformation(SystemProcessInformation) call: every process's parent, create time, session and
# working set without opening any process (works for elevated / protected processes too). x64 layout of
# SYSTEM_PROCESS_INFORMATION; psutil's own proc_info() fallback reads the same structure, one process at a time.
_SPI = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_EPOCH_DELTA_S = 11644473600.0
_OFF_NEXT, _OFF_CT, _OFF_PID, _OFF_PPID, _OFF_SID, _OFF_WSET = 0, 32, 80, 88, 100, 144
_ntq: Callable | None = None
_ntq_size = 1 << 20


def _ntq_fn() -> Callable | None:
    global _ntq
    if _ntq is None and ctypes.sizeof(ctypes.c_void_p) == 8:
        try:
            fn = ctypes.WinDLL("ntdll").NtQuerySystemInformation
            fn.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
            fn.restype = ctypes.c_long
            _ntq = fn
        except (OSError, AttributeError):
            _ntq = None
    return _ntq


def system_process_table() -> dict[int, SysProc] | None:
    """{pid: SysProc} from one kernel call, or None when unavailable (non-x64 Python, call failure). Read-only."""
    global _ntq_size
    fn = _ntq_fn()
    if fn is None:
        return None
    try:
        size = _ntq_size
        for _ in range(6):
            buf = ctypes.create_string_buffer(size)
            ret = ctypes.c_ulong(0)
            status = int(fn(_SPI, buf, size, ctypes.byref(ret))) & 0xFFFFFFFF
            if status == 0:
                break
            if status != _STATUS_INFO_LENGTH_MISMATCH:
                log.debug("mem_watch: NtQuerySystemInformation status 0x%08X", status)
                return None
            size = max(size * 2, int(ret.value) + (64 << 10))
        else:
            return None
        _ntq_size = size
        raw = buf.raw
        out: dict[int, SysProc] = {}
        off = 0
        u32, i64, u64 = struct.Struct("<I"), struct.Struct("<q"), struct.Struct("<Q")
        while off + _OFF_WSET + 8 <= len(raw):
            nxt = u32.unpack_from(raw, off + _OFF_NEXT)[0]
            ct100 = i64.unpack_from(raw, off + _OFF_CT)[0]
            pid = u64.unpack_from(raw, off + _OFF_PID)[0]
            out[int(pid)] = SysProc(ppid=int(u64.unpack_from(raw, off + _OFF_PPID)[0]),
                                    create_time=(ct100 / 1e7 - _EPOCH_DELTA_S) if ct100 > 0 else None,
                                    session_id=int(u32.unpack_from(raw, off + _OFF_SID)[0]),
                                    wset=int(u64.unpack_from(raw, off + _OFF_WSET)[0]))
            if nxt == 0:
                break
            off += nxt
        return out
    except Exception as exc:  # never raise into the watcher
        log.debug("mem_watch: process table failed: %s", exc)
        return None


def _avail_total_mb() -> tuple[float, float]:
    vm = psutil.virtual_memory()
    return vm.available / _MB, vm.total / _MB


# ---------------------------------------------------------------------------------------------------- watcher

class MemWatcher:
    """The memory check as a co-scanner of game_watch.GameWatcher (it owns no thread and no process pass).

    cfg is read on every pass (the live config dict main.py shares with the detector, so protected-list edits apply
    at once) or a callable returning it. Inputs are injectable for tests: protected_pids (ServerEndpoint.
    protected_pids), context_factory(cfg, self_pid, extra_hard_pids, session_ok) -> GraphContext (default
    graph_context), avail_fn() -> (available MB, total MB), table_fn() -> {pid: SysProc}."""

    def __init__(self, cfg: Mapping | Callable[[], Mapping] | None = None,
                 on_app: Callable[[MemReport], None] | None = None, *,
                 protected_pids: Callable[[], Iterable[int]] | None = None,
                 context_factory: Callable[..., GraphContext] | None = None,
                 avail_fn: Callable[[], tuple[float, float]] | None = None,
                 table_fn: Callable[[], Mapping[int, SysProc] | None] | None = None,
                 engine_protects: Callable[[ProcInfo], bool] | None = None,
                 self_pid: int | None = None) -> None:
        self._cfg = cfg
        self.on_app = on_app
        self._provider = protected_pids
        self._context_factory = context_factory
        self._engine_protects = engine_protects
        self._avail_fn = avail_fn or _avail_total_mb
        self._table_fn = table_fn or system_process_table
        self.self_pid = os.getpid() if self_pid is None else self_pid
        self._lock = threading.Lock()
        self._armed = False
        self._gen = 0
        self._state: MemState | None = None
        self._given_baseline: float | None = None
        self._watcher: GameWatcher | None = None
        self._pass: tuple | None = None          # (gen, apps, strata_mb) between begin() and decide()
        self._deliver_gen = -1                   # the arm generation decide() last produced reports for
        self._provider_pids: set[int] = set()
        self._provider_at = -math.inf
        self._warned_table = False
        self.last_scan_ms: float | None = None
        self.last_floor_mb: float | None = None
        self.last_avail_mb: float | None = None
        self.scans = 0

    # ---- public API ---------------------------------------------------------------------------------

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def baseline_avail_mb(self) -> float | None:
        st = self._state
        return None if st is None else st.baseline_avail_mb

    def attach(self, watcher: GameWatcher) -> None:
        """Called by GameWatcher(co_scan=self): the shared thread and the shared 'reported games' registry."""
        self._watcher = watcher

    def arm(self, baseline_avail_mb: float | None = None) -> None:
        """Strata is ready: watch apps started from now on. O(1). baseline_avail_mb None = measure on the next pass."""
        b = None
        if isinstance(baseline_avail_mb, (int, float)) and not isinstance(baseline_avail_mb, bool) \
                and math.isfinite(float(baseline_avail_mb)) and baseline_avail_mb >= 0:
            b = float(baseline_avail_mb)
        with self._lock:
            self._gen += 1
            self._armed = True
            self._state = MemState(arm_time=time.time())
            self._given_baseline = b
            self._pass = None
            self._provider_at = -math.inf
        w = self._watcher
        if w is not None:
            w.co_armed()
        log.info("mem_watch: armed")

    def disarm(self) -> None:
        with self._lock:
            was = self._armed
            self._gen += 1
            self._armed = False
            self._state = None
            self._pass = None
        if was:
            log.info("mem_watch: disarmed")

    # ---- co-scanner protocol (called on the game-watch thread) ---------------------------------------

    def _config(self) -> Mapping:
        try:
            c = self._cfg() if callable(self._cfg) else self._cfg
        except Exception:  # noqa: BLE001
            log.debug("mem_watch: config callable failed", exc_info=True)
            c = None
        return c if isinstance(c, Mapping) else {}

    def _extra_pids(self) -> set[int]:
        if self._provider is None:
            return set()
        now = time.monotonic()
        if now - self._provider_at >= PROVIDER_REFRESH_S:
            self._provider_at = now
            try:
                self._provider_pids = {int(p) for p in (self._provider() or ())}
            except Exception as exc:  # provider exceptions are swallowed, as in the detector
                log.warning("mem_watch: protected_pids provider failed: %s", exc)
        return set(self._provider_pids)

    def begin(self, procs: list[dict], now: float) -> None:
        """Group the shared snapshot into apps. Also re-claims the game installs of apps already reported, BEFORE the
        game check of this pass runs, so the folder watcher never reports a game inside an app reported here."""
        t0 = time.perf_counter()
        with self._lock:
            if not self._armed or self._state is None:
                return
            gen, st = self._gen, self._state
        table = self._table_fn()
        if table is None:
            if not self._warned_table:
                self._warned_table = True
                log.warning("mem_watch: process table unavailable; memory check paused")
            return
        sid = table.get(self.self_pid)
        ours = sid.session_id if sid is not None else session_id_of(self.self_pid)

        def session_ok(pid: int) -> bool:
            row = table.get(pid)
            return ours is not None and row is not None and row.session_id == ours

        cfg = self._config()
        pinfos = build_procinfos(procs, table)
        if self._context_factory is not None:
            ctx = self._context_factory(cfg, self_pid=self.self_pid, extra_hard_pids=self._extra_pids(),
                                        session_ok=session_ok)
        else:
            ctx = graph_context(cfg, self_pid=self.self_pid, extra_hard_pids=self._extra_pids(), session_ok=session_ok,
                                engine_protects=self._engine_protects)
        apps = group_apps(pinfos, ctx)
        smb = strata_mb(pinfos, ctx)
        keys: set[str] = set()
        probe = st.copy()
        w = self._watcher
        for a in absorb_reported(probe, apps, w.game_key_claimed if w is not None else None):
            keys |= a.game_keys
        if keys and w is not None:
            w.claim_game_keys(keys)
        with self._lock:
            if self._gen == gen:
                self._pass = (gen, apps, smb)
        self.last_scan_ms = (time.perf_counter() - t0) * 1000.0

    def decide(self, now: float) -> list[MemReport]:
        """Run the pure decision on the apps from begin() (after the game check claimed its games)."""
        t0 = time.perf_counter()
        with self._lock:
            p, st = self._pass, self._state
            self._pass = None
            if p is None or st is None or p[0] != self._gen:
                return []
            gen = self._gen
            if st.baseline_avail_mb is None and self._given_baseline is not None:
                st = st.copy()
                st.baseline_avail_mb = self._given_baseline
                st.baseline_strata_mb = p[2]
        _gen, apps, smb = p
        try:
            avail, total = self._avail_fn()
        except Exception:  # noqa: BLE001
            log.debug("mem_watch: available-memory read failed", exc_info=True)
            return []
        mc = MemConfig.from_config(self._config())
        w = self._watcher
        first = st.baseline_avail_mb is None
        reports, new = decide(st, apps, now, avail, total, mc, strata_mb=smb,
                              game_claimed=w.game_key_claimed if w is not None else None)
        with self._lock:
            if self._gen != gen or not self._armed:
                return []
            self._state = new
            self._deliver_gen = gen
        if first:
            log.info("mem_watch: baseline %.0f MB available (Strata %s MB, %.0f MB total)", avail,
                     "?" if smb is None else f"{smb:.0f}", total)
        self.last_avail_mb = avail
        self.last_floor_mb = floor_mb(new.baseline_avail_mb or 0.0, total, mc)
        if self.last_scan_ms is not None:
            self.last_scan_ms += (time.perf_counter() - t0) * 1000.0
        self.scans += 1
        if reports and w is not None:
            keys: set[str] = set()
            by_key = {a.key: a for a in apps}
            for r in reports:
                a = by_key.get((r.pid, r.create_time))
                if a is not None:
                    keys |= a.game_keys
            if keys:
                w.claim_game_keys(keys)
        return reports

    def deliver(self, report: MemReport) -> None:
        with self._lock:
            if not self._armed or self._gen != self._deliver_gen:
                return                           # disarmed / re-armed since decide(): a stale report
        log.info("mem_watch: memory app: %s (%s, pid %d): %s", report.title, report.name, report.pid, report.why())
        cb = self.on_app
        if cb is not None:
            try:
                cb(report)
            except Exception:
                log.exception("mem_watch: on_app callback failed")
