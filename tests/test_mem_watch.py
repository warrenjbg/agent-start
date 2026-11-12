"""Self-check for mem_watch.py. Run: .venv\\Scripts\\python.exe tests\\test_mem_watch.py

Safe by construction:
  * mem_watch only observes. Every threshold/sustain/pressure rule is driven with FAKE process tables and fake
    available-memory numbers; nothing allocates large memory.
  * The one real-thread test spawns ONE throwaway process of its own (a renamed copy of the Python interpreter that
    sleeps, its command line carrying this run's unique MARKER), reports a fake working set for that pid only, and
    kills only processes carrying the MARKER at the end. No windows, no network, no port 8080.
  * Real-system reads (process table, scan cost) are read-only and print counts/timings only, never process names.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

import game_watch  # noqa: E402
import mem_watch  # noqa: E402
from detector import ProcInfo, session_id_of  # noqa: E402
from game_watch import GameWatcher, install_of  # noqa: E402
from mem_watch import (CONFIG_DEFAULTS, AppView, MemConfig, MemReport, MemState, MemWatcher, SysProc,  # noqa: E402
                       build_procinfos, decide, evaluate, floor_mb, graph_context, group_apps, strata_mb,
                       system_process_table)

RESULTS: list[tuple[str, bool, str]] = []
MB = 1024 * 1024
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
MARKER = f"hc-memwatch-test-{uuid.uuid4().hex[:12]}"
TMP = Path(tempfile.mkdtemp(prefix=MARKER + "-"))


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------------------------- fakes

NOW = 1_000_000.0
ARM = NOW - 100.0                    # Strata became ready 100 s before NOW
TOTAL = 65536.0
SR = r"C:\Windows"
STRATA_DIR = r"D:\AI\Strata"
SELF = 20
STEAM = r"D:\Games\steamapps\common"
CFG: dict = {"strata_dir": STRATA_DIR, "user_whitelist": ["Chatty.exe"],
             "game_dirs": ["steamapps", "Epic Games"], "game_exclude": ["wallpaper64.exe"]}
FAST = MemConfig(app_mb=4096, sustain_s=15, floor_baseline_pct=50, floor_total_pct=6)


def P(pid: int, name: str, ppid: int | None = 10, ct: float = NOW - 50, mb: float = 0.0, exe: str | None = "",
      cmd: list[str] | None = None) -> ProcInfo:
    if exe == "":
        exe = rf"C:\Apps\{name.rsplit('.', 1)[0]}\{name}"
    return ProcInfo(pid=pid, name=name, exe=exe, ppid=ppid, create_time=ct, rss=int(mb * MB), cmdline=cmd)


def base_table() -> dict[int, ProcInfo]:
    """explorer, this app, a serving Strata (venv python -> base python -> strata.exe), a pre-ready app."""
    ps = [P(10, "explorer.exe", ppid=None, ct=ARM - 5000, mb=200, exe=SR + r"\explorer.exe"),
          P(20, "AgentStart.exe", ppid=10, ct=ARM - 300, mb=120),
          P(30, "cmd.exe", ppid=10, ct=ARM - 200, mb=5, exe=SR + r"\System32\cmd.exe"),
          P(31, "python.exe", ppid=30, ct=ARM - 199, mb=60, exe=STRATA_DIR + r"\.venv\Scripts\python.exe",
            cmd=["python.exe", r"serve\server.py", "--port", "8080"]),
          P(32, "python.exe", ppid=31, ct=ARM - 198, mb=900, exe=r"C:\Py\python.exe",
            cmd=["python.exe", r"serve\server.py", "--port", "8080"]),
          P(33, "strata.exe", ppid=32, ct=ARM - 197, mb=48000, exe=STRATA_DIR + r"\engine\strata.exe"),
          P(40, "OldEditor.exe", ppid=10, ct=ARM - 1000, mb=6000)]
    return {p.pid: p for p in ps}


def tbl(*extra: ProcInfo, drop: tuple[int, ...] = ()) -> dict[int, ProcInfo]:
    t = base_table()
    for d in drop:
        t.pop(d, None)
    for p in extra:
        t[p.pid] = p
    return t


def ctx(cfg: dict | None = None, extra: tuple[int, ...] = (), session_ok=None):
    return graph_context(CFG if cfg is None else cfg, self_pid=SELF, extra_hard_pids=extra,
                         session_ok=session_ok or (lambda pid: True), system_root=SR)


def run(state: MemState, procs: dict[int, ProcInfo], now: float, avail: float, *, c=None,
        cfg: MemConfig = FAST, claimed=None):
    return evaluate(state, procs, c or ctx(), now, avail, TOTAL, cfg, game_claimed=claimed)


def ready(baseline: float = 8000.0) -> MemState:
    """A state whose baseline was taken (Strata RAM as in base_table)."""
    st = MemState(arm_time=ARM)
    _r, st = run(st, tbl(), ARM + 1, baseline)
    return st


def series(state: MemState, make, start: float, end: float, avail, step: float = 3.0, **kw):
    """Run evaluate every `step` s from start to end; make(t) -> procs, avail(t) -> MB. Returns (reports, state)."""
    out: list[tuple[float, MemReport]] = []
    t = start
    while t <= end + 1e-9:
        a = avail(t) if callable(avail) else avail
        reps, state = run(state, make(t), t, a, **kw)
        out.extend((t, r) for r in reps)
        t += step
    return out, state


def big(pid: int = 100, name: str = "Blender.exe", ct: float = NOW - 20, mb: float = 6000.0, **kw) -> ProcInfo:
    return P(pid, name, ct=ct, mb=mb, **kw)


# ----------------------------------------------------------------------------------------- 1. config

def test_config() -> None:
    section("1. config keys: defaults, clamping, bad values")
    d = MemConfig.from_config({})
    check("missing keys -> defaults (4096 MB, 15 s, 50 %, 6 %)",
          (d.app_mb, d.sustain_s, d.floor_baseline_pct, d.floor_total_pct) == (4096, 15, 50, 6), str(d))
    check("None / non-dict config -> defaults", MemConfig.from_config(None) == d
          and MemConfig.from_config([1, 2]) == d)  # type: ignore[arg-type]
    c = MemConfig.from_config({"mem_watch_app_mb": 8192, "mem_watch_sustain_s": 30.5,
                               "mem_watch_floor_baseline_pct": 40, "mem_watch_floor_total_pct": 10})
    check("values read from config", (c.app_mb, c.sustain_s, c.floor_baseline_pct, c.floor_total_pct)
          == (8192, 30.5, 40, 10), str(c))
    lo = MemConfig.from_config({"mem_watch_app_mb": 10, "mem_watch_sustain_s": 0, "mem_watch_floor_baseline_pct": -5,
                                "mem_watch_floor_total_pct": 0})
    hi = MemConfig.from_config({"mem_watch_app_mb": 1e12, "mem_watch_sustain_s": 1e9,
                                "mem_watch_floor_baseline_pct": 500, "mem_watch_floor_total_pct": 99})
    check("clamped low (1024 MB, 3 s, 1 %, 0.5 %)", (lo.app_mb, lo.sustain_s, lo.floor_baseline_pct,
                                                     lo.floor_total_pct) == (1024, 3, 1, 0.5), str(lo))
    check("clamped high (1 TB, 3600 s, 100 %, 50 %)", (hi.app_mb, hi.sustain_s, hi.floor_baseline_pct,
                                                       hi.floor_total_pct) == (1024 * 1024, 3600, 100, 50), str(hi))
    bad = [("4096", "string"), (True, "bool"), (None, "null"), (float("nan"), "NaN"), (float("inf"), "inf"),
           ([1], "list"), ({"a": 1}, "dict")]
    ok = all(MemConfig.from_config({k: v for k in CONFIG_DEFAULTS}) == d for v, _ in bad)
    check("bad values (string, bool, null, NaN, inf, list, dict) -> defaults, never raise", ok)
    shipped = json.loads((ROOT / "config.default.json").read_text(encoding="utf-8"))
    check("config.default.json ships every key with the module default",
          all(shipped.get(k) == v for k, v in CONFIG_DEFAULTS.items()), str({k: shipped.get(k) for k in CONFIG_DEFAULTS}))
    ctx_bad = graph_context({"min_game_ram_mb": "x", "user_whitelist": "notalist", "game_dirs": None,
                             "game_exclude": [None, 3]}, self_pid=1)
    check("graph_context survives a garbage config", ctx_bad.min_game_ram_mb == 50 and ctx_bad.game_dirs == []
          and ctx_bad.game_exclude == frozenset())


# ----------------------------------------------------------------------------------------- 2. floor

def test_floor() -> None:
    section("2. available-memory floor = min(baseline x 50 %, total x 6 %)")
    m = MemConfig()
    check("64 GB, 12 GB available at ready -> capped at 6 % of total (3.9 GB)",
          abs(floor_mb(12288, 65536, m) - 3932.16) < 0.01, f"{floor_mb(12288, 65536, m)}")
    check("64 GB, 3 GB available at ready -> half the baseline (1.5 GB)", floor_mb(3072, 65536, m) == 1536)
    check("32 GB, 1 GB available at ready -> 512 MB", floor_mb(1024, 32768, m) == 512)
    check("64 GB, 20 GB available (small model) -> 3.9 GB", abs(floor_mb(20480, 65536, m) - 3932.16) < 0.01)
    check("unknown total -> baseline share only", floor_mb(3000, 0, m) == 1500)
    check("negative baseline -> 0", floor_mb(-5, 65536, m) == 0)


# ----------------------------------------------------------------------------------------- 3. decide()

def av(pid: int = 100, ct: float = NOW - 20, mb: float = 6000.0, name: str = "Blender.exe",
       members: tuple[tuple[int, float], ...] | None = None, game_keys: frozenset[str] = frozenset()) -> AppView:
    mem = frozenset(members) if members is not None else frozenset({(pid, ct)})
    return AppView(key=(pid, ct), pid=pid, create_time=ct, name=name, title=name[:-4], exe=rf"C:\Apps\{name}",
                   ram_mb=mb, members=mem, game_keys=game_keys)


def dseries(st: MemState, apps_at, avail_at, t0: float, t1: float, step: float = 3.0, **kw):
    out = []
    t = t0
    while t <= t1 + 1e-9:
        apps = apps_at(t) if callable(apps_at) else apps_at
        a = avail_at(t) if callable(avail_at) else avail_at
        reps, st = decide(st, apps, t, a, TOTAL, FAST, **kw)
        out.extend((t, r) for r in reps)
        t += step
    return out, st


def test_decide() -> None:
    section("3. decide(): both conditions, sustain, reset, once per session")
    st0 = MemState(arm_time=ARM)
    reps, st = decide(st0, [av()], ARM + 1, 8000, TOTAL, FAST)
    check("first pass after arm only records the baseline", reps == [] and st.baseline_avail_mb == 8000
          and st0.baseline_avail_mb is None, str(st))
    # floor = min(4000, 3932) = 3932
    got, _ = dseries(st, [av()], 3000, NOW, NOW + 30)
    check("big new app + low memory for 15 s -> one report at +15 s", [t - NOW for t, _ in got] == [15.0],
          str([t - NOW for t, _ in got]))
    if got:
        r = got[0][1]
        check("report: name/title/pid/ram/avail/floor/baseline/sustained",
              (r.name, r.title, r.pid, r.ram_mb, r.avail_mb, round(r.floor_mb), r.baseline_avail_mb, r.sustained_s)
              == ("Blender.exe", "Blender", 100, 6000.0, 3000.0, 3932, 8000.0, 15.0), str(r))
        check("why() names both measured values", "6000 MB" in r.why() and "3000 MB" in r.why(), r.why())
    got, _ = dseries(st, [av()], 6000, NOW, NOW + 60)
    check("big app, plenty available (above the floor) -> never", got == [])
    got, _ = dseries(st, [av(mb=4000)], 1000, NOW, NOW + 60)
    check("app below 4096 MB, memory low -> never", got == [])
    got, _ = dseries(st, [av(mb=4096)], 1000, NOW, NOW + 15)
    check("exactly at the thresholds (>= app MB, < floor) -> reported", len(got) == 1)
    got, _ = dseries(st, [av()], lambda t: 5000 if t == NOW + 9 else 3000, NOW, NOW + 30)
    check("a dip of available memory above the floor resets the timer", [t - NOW for t, _ in got] == [27.0],
          str([t - NOW for t, _ in got]))
    got, _ = dseries(st, lambda t: [av(mb=3000 if t == NOW + 9 else 6000)], 3000, NOW, NOW + 30)
    check("a dip of the app below the threshold resets the timer", [t - NOW for t, _ in got] == [27.0],
          str([t - NOW for t, _ in got]))
    got, _ = dseries(st, lambda t: [] if t == NOW + 9 else [av()], 3000, NOW, NOW + 30)
    check("the app missing from one pass resets the timer", [t - NOW for t, _ in got] == [27.0])
    gap = st
    r1, gap = decide(gap, [av()], NOW, 3000, TOTAL, FAST)
    r2, gap = decide(gap, [av()], NOW + 20, 3000, TOTAL, FAST)
    check("two observations > 10 s apart are not a sustained period", r1 == [] and r2 == [])
    got, st_after = dseries(st, [av()], 3000, NOW, NOW + 120)
    check("once per app per session (2 minutes of pressure -> one report)", len(got) == 1)
    st_re = MemState(arm_time=NOW + 200)
    _r, st_re = decide(st_re, [av()], NOW + 201, 8000, TOTAL, FAST)
    got, _ = dseries(st_re, [av()], 3000, NOW + 203, NOW + 300)
    check("re-arm: an app running before the new ready moment never reports", got == [])
    got, _ = dseries(st_re, [av(pid=101, ct=NOW + 202)], 3000, NOW + 203, NOW + 230)
    check("re-arm: a new app after it reports again (fresh state)", len(got) == 1)
    got, _ = dseries(st, [av(ct=ARM - 1, mb=30000)], 500, NOW, NOW + 60)
    check("started BEFORE Strata was ready -> never, however big", got == [])
    got, _ = dseries(st, [av(ct=ARM)], 500, NOW, NOW + 60)
    check("started at the exact ready moment counts as before", got == [])
    got, st2 = dseries(st, [av(pid=100, ct=NOW - 20)], 3000, NOW, NOW + 15)
    got2, _ = dseries(st2, [av(pid=100, ct=NOW + 30)], 3000, NOW + 33, NOW + 50)
    check("pid reuse: same pid, new create_time -> a different app, reported on its own",
          len(got) == 1 and len(got2) == 1 and got2[0][1].create_time == NOW + 30)
    got, _ = dseries(st, [av(game_keys=frozenset({"k1"}))], 3000, NOW, NOW + 60, game_claimed={"k1"})
    check("app containing a game the folder watcher reported -> never", got == [])
    got, _ = dseries(st, [av(game_keys=frozenset({"k1"}))], 3000, NOW, NOW + 60, game_claimed=lambda k: k == "k2")
    check("... a different game key does not block it", len(got) == 1)
    snap = st.copy()
    decide(st, [av()], NOW, 3000, TOTAL, FAST)
    check("decide() never mutates the state it is given", st.since == snap.since and st.reported == snap.reported)
    a, b = av(pid=100, ct=NOW - 20), av(pid=200, ct=NOW - 10, name="Other.exe")
    got, _ = dseries(st, [b, a], 3000, NOW, NOW + 15)
    check("two apps -> two reports, oldest first", [r.pid for _, r in got] == [100, 200])


def test_strata_growth() -> None:
    section("4. Strata's own growth does not count; baseline")
    st = ready(8000)
    check("baseline records available RAM and Strata's RAM", st.baseline_avail_mb == 8000
          and abs((st.baseline_strata_mb or 0) - (60 + 900 + 48000)) < 1, str(st.baseline_strata_mb))

    def grown(t: float):
        t2 = tbl(big())
        t2[33] = P(33, "strata.exe", ppid=32, ct=ARM - 197, mb=48000 + 2000, exe=STRATA_DIR + r"\engine\strata.exe")
        return t2
    # floor = min(8000 x 50 %, 65536 x 6 %) = 3932 MB
    got, _ = series(st, grown, NOW, NOW + 60, 3000)
    check("available 3.0 GB, but Strata itself grew 2 GB -> corrected 5.0 GB > floor -> never", got == [])
    got, _ = series(st, grown, NOW, NOW + 60, 1500)
    check("available 1.5 GB with Strata +2 GB -> corrected 3.5 GB < floor -> reported", len(got) == 1)
    if got:
        check("... the report shows raw and corrected available memory", got[0][1].avail_mb == 1500
              and got[0][1].avail_eff_mb == 3500, str(got[0][1]))
    got, _ = series(st, lambda t: tbl(big()), NOW, NOW + 60, 3500)
    check("same 3.5 GB without Strata growth -> reported", len(got) == 1)
    shrunk = tbl(big())
    shrunk[33] = P(33, "strata.exe", ppid=32, ct=ARM - 197, mb=40000, exe=STRATA_DIR + r"\engine\strata.exe")
    got, _ = series(st, lambda t: shrunk, NOW, NOW + 60, 3500)
    check("Strata shrinking is not subtracted (only growth is added back)", len(got) == 1)
    check("strata_mb(): server.py processes, strata_dir exes and their descendants",
          abs((strata_mb(tbl(), ctx()) or 0) - (60 + 900 + 48000)) < 1, str(strata_mb(tbl(), ctx())))
    check("strata_mb(): provider pids count too", abs((strata_mb(tbl(), ctx(extra=(30,))) or 0) - 48965) < 1)
    check("strata_mb(): no Strata -> None", strata_mb({1: P(1, "x.exe")}, ctx()) is None)


# ----------------------------------------------------------------------------------------- 5. grouping/protection

def test_grouping() -> None:
    section("5. grouping + protection reuse detector.build_apps")
    st = ready(8000)
    t_browser = tbl(big(100, "Browsr.exe", mb=1500), big(101, "Browsr.exe", ppid=100, mb=1500, ct=NOW - 19),
                    big(102, "Browsr.exe", ppid=100, mb=1500, ct=NOW - 19),
                    big(103, "Browsr.exe", ppid=101, mb=1500, ct=NOW - 18))
    apps = [a for a in group_apps(t_browser, ctx()) if a.pid == 100]
    check("browser + 3 children = one app, RAM summed (6000 MB)", len(apps) == 1 and apps[0].ram_mb == 6000
          and len(apps[0].members) == 4, str(apps))
    got, _ = series(st, lambda t: t_browser, NOW, NOW + 15, 3000)
    check("... and it is reported as one app", [r.pid for _, r in got] == [100])
    check("app started before ready (OldEditor, 6 GB) is grouped but never reported",
          any(a.pid == 40 for a in group_apps(tbl(), ctx())))

    def never(procs: dict, c=None, why: str = "") -> None:
        got, _ = series(st, lambda t: procs, NOW, NOW + 60, 500, c=c)
        check(why, got == [], str([r.title for _, r in got]))

    never(tbl(big(100, "Chatty.exe")), why="app on the user's protected list -> never")
    never(tbl(big(100, "chatty.EXE")), why="... case-insensitive")
    never(tbl(big(100, "Chatty.exe"), big(101, "Helper.exe", ppid=100, ct=NOW - 19)),
          why="... nor its child processes")
    never(tbl(big(100, "Blender.exe")), c=ctx(cfg={**CFG, "user_whitelist": ["Blender.exe"]}),
          why="whitelist edits apply (config read per pass)")
    never(tbl(big(100, "svchost.exe")), why="system process name -> never")
    never(tbl(big(100, "Thing.exe", exe=SR + r"\System32\Thing.exe")), why="exe under %SystemRoot% -> never")
    never(tbl(big(100, "Child.exe", ppid=SELF)), why="this app's own child tree -> never")
    never(tbl(big(100, "Tool.exe", ppid=31)), why="child of Strata's server -> never")
    never(tbl(big(100, "Extra.exe", exe=STRATA_DIR + r"\tools\Extra.exe", ppid=10)), why="exe under strata_dir -> never")
    never(tbl(big(100, "Srv.exe", cmd=["Srv.exe", r"x\serve\server.py"])), why="a serve\\server.py command line -> never")
    never(tbl(big(100, "Lnch.exe")), c=ctx(extra=(100,)), why="provider (Strata) pid -> never")
    import dataclasses
    if "engine_protects" in {f.name for f in dataclasses.fields(ctx())}:     # detector with the engine abstraction
        eng_ctx = graph_context(CFG, self_pid=SELF, session_ok=lambda pid: True, system_root=SR,
                                engine_protects=lambda i: i.name == "OtherEngine.exe")
        never(tbl(big(100, "OtherEngine.exe")), c=eng_ctx, why="engine predicate (engine abstraction) -> never")
        check("strata_mb(): uses the engine predicate when given",
              abs((strata_mb(tbl(big(100, "OtherEngine.exe")), eng_ctx) or 0) - 6000) < 1)
    else:
        print("  (detector has no engine_protects yet: engine-predicate checks skipped)")
    never(tbl(big(100, "Remote.exe")), c=ctx(session_ok=lambda pid: pid != 100), why="other session -> never")
    never(tbl(big(100, "Script.exe", ppid=30)), why="started from a terminal (cmd.exe tree) -> never")
    never(tbl(big(100, "Unknown.exe", exe=None)), why="unreadable exe (access denied) -> never")
    never(tbl(big(100, "NoTime.exe", ct=None)), why="unknown create time -> never")  # type: ignore[arg-type]
    never(tbl(big(100, "Zero.exe", mb=0)), why="memory unreadable (0 MB) -> never")

    # root dies, children stay
    fam = [big(100, "Suite.exe", mb=3000), big(101, "Worker.exe", ppid=100, ct=NOW - 19, mb=500,
                                                exe=r"C:\Apps\Suite\bin\Worker.exe"),
           big(102, "Worker.exe", ppid=100, ct=NOW - 19, mb=2500, exe=r"C:\Apps\Suite\bin\Worker.exe")]
    got1, st1 = series(st, lambda t: tbl(*fam), NOW, NOW + 15, 3000)
    orphans = [P(101, "Worker.exe", ppid=100, ct=NOW - 19, mb=5000, exe=r"C:\Apps\Suite\bin\Worker.exe"),
               P(102, "Worker.exe", ppid=100, ct=NOW - 19, mb=5000, exe=r"C:\Apps\Suite\bin\Worker.exe")]
    check("app folds helpers under its own folder into one tree", [r.member_count for _, r in got1] == [3])
    got2, _ = series(st1, lambda t: tbl(*orphans), NOW + 18, NOW + 60, 3000)
    check("root died, children regrouped -> not reported again", got2 == [], str([r.pid for _, r in got2]))

    # launcher before ready, big child after
    launcher = P(90, "Launcher.exe", ct=ARM - 50, mb=300, exe=r"C:\Apps\Launcher\Launcher.exe")
    child_own = big(100, "Sim.exe", ppid=90, exe=r"E:\Sims\Sim\Sim.exe")
    got, _ = series(st, lambda t: tbl(launcher, child_own), NOW, NOW + 15, 3000)
    check("launcher started before ready, big app it launched after -> reported (own app)",
          [r.pid for _, r in got] == [100])
    child_in = big(100, "Engine.exe", ppid=90, exe=r"C:\Apps\Launcher\bin\Engine.exe")
    got, _ = series(st, lambda t: tbl(launcher, child_in), NOW, NOW + 60, 500)
    check("helper under the pre-ready launcher's own folder -> part of the launcher's app -> never", got == [])

    g = big(100, "eldenring.exe", exe=STEAM + r"\ELDEN RING\Game\eldenring.exe", ppid=10)
    apps = [a for a in group_apps(tbl(g), ctx()) if a.pid == 100]
    key = install_of(g.exe, CFG["game_dirs"])
    check("game-dir app: install folder title + game key", len(apps) == 1 and apps[0].title == "ELDEN RING"
          and key is not None and apps[0].game_keys == {key[0]}, str(apps))


# ----------------------------------------------------------------------------------------- 6. inputs

def test_inputs() -> None:
    section("6. snapshot merge + kernel process table")
    snap = [{"pid": 1, "name": "a.exe", "exe": r"C:\A\a.exe", "create_time": 100.0},
            {"pid": 2, "name": "b.exe", "exe": r"C:\B\b.exe", "create_time": 200.0},
            {"pid": 3, "name": "c.exe", "exe": None, "create_time": None},
            {}, {"pid": None}, {"pid": "x"}, {"pid": 4, "create_time": "bad"}]
    table = {1: SysProc(ppid=9, create_time=100.0, session_id=1, wset=5 * MB),
             2: SysProc(ppid=9, create_time=999.0, session_id=1, wset=7 * MB)}
    pi = build_procinfos(snap, table)
    check("merge: ppid + working set from the kernel row", pi[1].ppid == 9 and pi[1].rss == 5 * MB)
    check("merge: create_time mismatch (pid reused) -> ppid None, 0 MB", pi[2].ppid is None and pi[2].rss == 0)
    check("merge: unreadable entries kept as protected-looking rows, garbage skipped",
          pi[3].exe is None and pi[3].create_time is None and set(pi) == {1, 2, 3, 4}, str(sorted(pi)))
    check("merge: no table -> 0 MB rows", build_procinfos(snap[:1], None)[1].rss == 0)

    t0 = time.perf_counter()
    real = system_process_table()
    ms = (time.perf_counter() - t0) * 1000
    me = psutil.Process(os.getpid())
    row = (real or {}).get(os.getpid())
    check("kernel table: one call lists every process", real is not None and len(real) >= len(psutil.pids()) - 5,
          f"{len(real or {})} vs {len(psutil.pids())}")
    check("kernel table: own ppid / session / create_time match psutil",
          row is not None and row.ppid == me.ppid() and row.session_id == session_id_of(os.getpid())
          and row.create_time is not None and abs(row.create_time - me.create_time()) < 0.01, str(row))
    check("kernel table: own working set matches psutil rss within 64 MB",
          row is not None and abs(row.wset - me.memory_info().rss) < 64 * MB)
    print(f"  kernel table: {len(real or {})} processes in {ms:.1f} ms")


# ----------------------------------------------------------------------------------------- 7. shared pass

_TIME = time


class Clock:
    """Stand-in for the `time` module inside game_watch / mem_watch (fake wall clock)."""

    def __init__(self, t: float) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    perf_counter = staticmethod(_TIME.perf_counter)
    monotonic = staticmethod(_TIME.monotonic)
    sleep = staticmethod(_TIME.sleep)


def shared_setup(min_age: float, game_procs: list[dict], tables: dict[int, SysProc], avail: list[float]):
    """GameWatcher + MemWatcher (no thread: _tick() is called directly) over a fake snapshot/table/clock."""
    clock = Clock(NOW)
    game_watch.time = clock  # type: ignore[assignment]
    mem_watch.time = clock  # type: ignore[assignment]
    base = [{"pid": 10, "name": "explorer.exe", "exe": SR + r"\explorer.exe", "create_time": ARM - 5000},
            {"pid": SELF, "name": "AgentStart.exe", "exe": r"C:\Apps\AgentStart\AgentStart.exe",
             "create_time": ARM - 300}]
    game_watch.snapshot = lambda: SNAPS.append(1) or [dict(p) for p in base + game_procs]  # type: ignore[assignment]
    tab = {10: SysProc(0, ARM - 5000, 1, 100 * MB), SELF: SysProc(10, ARM - 300, 1, 100 * MB), **tables}
    reports: list[MemReport] = []
    games: list = []
    mw = MemWatcher({**CFG, "mem_watch_sustain_s": 3}, on_app=reports.append, table_fn=lambda: tab,
                    avail_fn=lambda: (avail[0], TOTAL), self_pid=SELF)
    gw = GameWatcher(["steamapps"], on_game=games.append, min_age_s=min_age, co_scan=mw)
    gw._session_ok = lambda pid: True
    return clock, gw, mw, games, reports


def restore_time() -> None:
    game_watch.time = time  # type: ignore[assignment]
    mem_watch.time = time  # type: ignore[assignment]
    game_watch.snapshot = ORIG_SNAPSHOT  # type: ignore[assignment]


ORIG_SNAPSHOT = game_watch.snapshot
SNAPS: list[int] = []


def test_shared_pass() -> None:
    section("7. one shared pass: game check + memory check, both orders")
    exe = STEAM + r"\Big Game\bin\biggame.exe"
    try:
        gp = [{"pid": 500, "name": "biggame.exe", "exe": exe, "create_time": NOW + 1}]
        avail = [9000.0]
        clock, gw, mw, games, reps = shared_setup(2.0, gp, {500: SysProc(10, NOW + 1, 1, 8000 * MB)}, avail)
        check("GameWatcher(co_scan=) attaches the memory check", mw._watcher is gw)
        gw.arm()
        mw.arm()
        clock.t = NOW + 0.5
        gw._tick()                                     # baselines (game process not started yet: ct NOW+1)
        check("memory baseline taken on the first pass", mw.baseline_avail_mb == 9000.0)
        avail[0] = 1000.0
        for i in range(12):
            clock.t = NOW + 2 + i * 1.5
            gw._tick()
        check("order A: folder watcher reports the game first (one GameStart)",
              [g.title for g in games] == ["Big Game"], str([g.title for g in games]))
        check("order A: memory check never reports it again", reps == [], str([r.title for r in reps]))
        check("ONE snapshot per pass for both checks (13 passes, 13 snapshots)", gw.scans == 13 and len(SNAPS) == 13,
              f"scans={gw.scans} snapshots={len(SNAPS)}")

        avail[0] = 9000.0
        clock, gw, mw, games, reps = shared_setup(10.0, gp, {500: SysProc(10, NOW + 1, 1, 8000 * MB)}, avail)
        gw.arm()
        mw.arm()
        clock.t = NOW + 0.5
        gw._tick()
        avail[0] = 1000.0
        for i in range(15):
            clock.t = NOW + 2 + i * 1.5
            gw._tick()
        check("order B: memory check reports first (game watcher still waiting, min_age 10 s)",
              [r.title for r in reps] == ["Big Game"], str([r.title for r in reps]))
        check("order B: folder watcher then stays silent for that game", games == [], str([g.title for g in games]))
        if reps:
            check("report carries the tree's RAM (8000 MB) and the toast name", reps[0].ram_mb == 8000.0
                  and reps[0].title == "Big Game" and reps[0].name == "biggame.exe", str(reps[0]))

        avail[0] = 9000.0
        clock, gw, mw, games, reps = shared_setup(2.0, gp, {500: SysProc(10, NOW + 1, 1, 8000 * MB)}, avail)
        mw.arm()                                       # game action 'Do nothing': only the memory check runs
        clock.t = NOW + 0.5
        gw._tick()
        avail[0] = 1000.0
        for i in range(8):
            clock.t = NOW + 2 + i * 1.5
            gw._tick()
        check("games not armed: the memory check still runs on the shared pass and reports",
              [r.title for r in reps] == ["Big Game"] and games == [], str(reps))
        mw.disarm()
        n = gw.scans
        check("both disarmed: a pass does nothing", gw._tick() == 0.0 and gw.scans == n)
        mw.arm()
        clock.t += 1
        gw._tick()
        for i in range(8):
            clock.t += 1.5
            gw._tick()
        check("re-arm: the app (running before the new ready moment) is not reported again", len(reps) == 1)

        mwbad = MemWatcher({"mem_watch_app_mb": "lots", "mem_watch_sustain_s": None, "user_whitelist": 5,
                            "game_dirs": "steamapps"}, table_fn=lambda: {}, avail_fn=lambda: (1000.0, TOTAL))
        GameWatcher([], co_scan=mwbad)
        mwbad.arm()
        try:
            mwbad.begin([{"pid": 1, "name": "x.exe", "exe": r"C:\x.exe", "create_time": NOW}], NOW)
            mwbad.decide(NOW)
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("   ", repr(exc))
        check("a garbage config.json never breaks the pass", ok)
        mwnone = MemWatcher(CFG, table_fn=lambda: None)
        mwnone.arm()
        mwnone.begin([], NOW)
        check("kernel table unavailable -> memory check pauses, no exception", mwnone.decide(NOW) == [])
    finally:
        restore_time()


# ----------------------------------------------------------------------------------------- 8. real thread

SPAWNED: list[subprocess.Popen] = []


def bigapp_exe() -> str:
    """A renamed copy of the base Python interpreter (+ its DLLs) at <tmp>\\BigApp\\bigapp.exe."""
    d = TMP / "BigApp"
    exe = d / "bigapp.exe"
    if not exe.exists():
        d.mkdir(parents=True, exist_ok=True)
        base = Path(sys.base_prefix)
        shutil.copyfile(getattr(sys, "_base_executable", sys.executable), exe)
        for dll in base.glob("*.dll"):
            shutil.copyfile(dll, d / dll.name)
    return str(exe)


def spawn_orphan(seconds: float = 60.0) -> None:
    """bigapp.exe starts a detached bigapp.exe that sleeps, then exits: the sleeper has no living parent, so it is
    its own app (not a child of this test's python, which the detector protects)."""
    env = dict(os.environ, PYTHONHOME=sys.base_prefix)
    env.pop("PYTHONPATH", None)
    code = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%s)', %r], "
            "creationflags=0x08000008, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL)" % (seconds, MARKER))
    p = subprocess.Popen([bigapp_exe(), "-c", code, MARKER], cwd=str(TMP), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    SPAWNED.append(p)
    p.wait(15)


def ours() -> list[psutil.Process]:
    me = os.getpid()
    out = []
    for p in psutil.process_iter(["cmdline"], ad_value=None):
        if p.pid != me and any(MARKER in str(c) for c in (p.info.get("cmdline") or [])):
            out.append(p)
    return out


def kill_ours() -> int:
    procs = ours()
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)
    return len(procs)


def wait_for(cond, timeout: float, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


def test_real_thread() -> None:
    section("8. real thread: own throwaway process, fake working set + fake available memory")
    target: dict[str, int] = {}
    avail = [12000.0]

    def table() -> dict[int, SysProc] | None:
        t = system_process_table()
        if t is not None and target.get("pid") in t:
            r = t[target["pid"]]
            t[target["pid"]] = r._replace(wset=6000 * MB)       # pretend our sleeper holds 6 GB
        return t

    reports: list[MemReport] = []
    threads: list[str] = []
    lock = threading.Lock()

    def on_app(r: MemReport) -> None:
        if r.pid != target.get("pid"):
            return                       # a real app that happened to start meanwhile: ignored (and never printed)
        with lock:
            reports.append(r)
            threads.append(threading.current_thread().name)

    cfg = {"mem_watch_sustain_s": 3, "user_whitelist": [], "game_dirs": [TMP.name + "\\steamapps"]}
    mw = MemWatcher(cfg, on_app=on_app, table_fn=table, avail_fn=lambda: (avail[0], TOTAL))
    gw = GameWatcher([TMP.name + "\\steamapps"], interval_s=0.4, co_scan=mw)
    gw.start()
    try:
        mw.arm()
        check("memory check alone wakes and runs the shared thread", wait_for(lambda: mw.scans >= 1, 5.0),
              f"scans={mw.scans}")
        check("baseline from the real pass", mw.baseline_avail_mb == 12000.0, str(mw.baseline_avail_mb))
        spawn_orphan()
        found = wait_for(lambda: bool(ours()), 5.0)
        procs = ours()
        check("throwaway app runs as its own (orphaned) process", found and len(procs) == 1,
              str([p.pid for p in procs]))
        if procs:
            target["pid"] = procs[0].pid
        time.sleep(1.0)
        check("no report while available memory is above the floor", reports == [])
        avail[0] = 1500.0
        got = wait_for(lambda: len(reports) >= 1, 10.0)
        time.sleep(1.5)
        check("reported once after the sustain period", got and len(reports) == 1, str(len(reports)))
        if reports:
            r = reports[0]
            check("report: our process, its name, 6000 MB", r.pid == target.get("pid") and r.name == "bigapp.exe"
                  and r.title == "bigapp" and r.ram_mb == 6000.0, str(r))
        check("on_app ran on the 'game-watch' thread", threads[:1] == ["game-watch"], str(threads))
        mw.disarm()
        n = gw.scans
        time.sleep(1.0)
        check("disarmed: the shared thread goes idle", gw.scans == n, f"{n} -> {gw.scans}")
    finally:
        t = time.monotonic()
        gw.stop()
        check("stop() joins within 2 s", gw._thread is not None and not gw._thread.is_alive()
              and time.monotonic() - t < 2.0)


def test_scan_cost() -> None:
    section("9. cost of one pass on this PC (real snapshot, read-only)")
    gw0 = GameWatcher(["steamapps", "Epic Games"])
    gw0._session_ok = game_watch.current_session_filter()
    mw = MemWatcher(dict(CFG), on_app=lambda r: None)
    gw1 = GameWatcher(["steamapps", "Epic Games"], co_scan=mw)
    game_watch.snapshot()
    gw0.arm()
    gw1.arm()
    mw.arm()

    def med(w: GameWatcher) -> tuple[float, float]:
        w._tick()
        ts = []
        for _ in range(15):
            w._tick()
            ts.append(w.last_scan_ms or 0.0)
        ts.sort()
        return ts[len(ts) // 2], ts[-1]
    m0, x0 = med(gw0)
    m1, x1 = med(gw1)
    mw.disarm()
    gw0.disarm()
    gw1.disarm()
    print(f"  pass cost, games only: median {m0:.1f} ms (max {x0:.1f}); games + memory: median {m1:.1f} ms "
          f"(max {x1:.1f}); memory part {mw.last_scan_ms or 0:.1f} ms; {len(psutil.pids())} processes")
    check("games + memory pass < 60 ms (median)", m1 < 60.0, f"{m1:.1f} ms")


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="  log %(levelname)s %(message)s")
    try:
        for fn in (test_config, test_floor, test_decide, test_strata_growth, test_grouping, test_inputs,
                   test_shared_pass, test_real_thread, test_scan_cost):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
    finally:
        restore_time()
        left = kill_ours()
        time.sleep(0.2)
        check("no throwaway processes left behind", not ours(), f"killed {left} at exit")
        shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
