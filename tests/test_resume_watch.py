"""Self-check for resume_watch.py. Run: .venv\\Scripts\\python.exe tests\\test_resume_watch.py

Safe by construction: resume_watch only observes, and this test spawns no processes at all. Every watcher here
uses a fake snapshot function (fake process dicts); the one real snapshot (section 5) is read-only and only looks
for this test's own process. No windows, no GUI, no network.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

import game_watch  # noqa: E402
import resume_watch  # noqa: E402
from game_watch import GameStart, install_of  # noqa: E402
from resume_watch import (ResumeConfig, ResumeState, ResumeTarget, ResumeWatcher, decide, is_running,  # noqa: E402
                          target_for_app, target_for_game)

RESULTS: list[tuple[str, bool, str]] = []
DIRS = ["steamapps", "Epic Games", "Riot Games", "XboxGames", "GOG Galaxy", "Battle.net"]
STEAM = r"C:\Program Files (x86)\Steam\steamapps\common"
EPIC = r"C:\Program Files\Epic Games"
BLENDER = r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"
NOW = 1_000_000.0


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def fp(pid: int, exe: str | None, age: float = 10.0, name: str | None = None, **kw) -> dict:
    d = {"pid": pid, "name": name if name is not None else (exe or "").split("\\")[-1], "exe": exe,
         "create_time": NOW - age}
    d.update(kw)
    return d


def wait_for(cond, timeout: float, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


# ----------------------------------------------------------------------------------------- 1. targets + is_running

def test_targets_and_running() -> None:
    section("1. targets and is_running() over fake process lists")
    cp_exe = STEAM + r"\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe"
    gs = GameStart(name="Cyberpunk2077.exe", title="Cyberpunk 2077", pid=100, create_time=NOW - 50, exe=cp_exe,
                   ram_mb=3000.0)
    t = target_for_game(gs, DIRS, now=123.0)
    check("target_for_game: kind/title/key from game_watch.install_of",
          t.kind == "game" and t.title == "Cyberpunk 2077" and t.game_key == install_of(cp_exe, DIRS)[0]
          and t.members == frozenset({(100, NOW - 50)}) and t.since == 123.0, str(t))
    check("ResumeTarget is frozen", _frozen(t))

    game = fp(100, cp_exe, age=50)
    check("game: its own process -> running", is_running(t, [game], DIRS))
    other = fp(101, STEAM + r"\Cyberpunk 2077\REDprelauncher.exe", age=5)
    check("game: another exe of the same install -> running", is_running(t, [other], DIRS))
    leftovers = [fp(102, STEAM + r"\Cyberpunk 2077\bin\x64\CrashReportClient.exe"),
                 fp(103, STEAM + r"\Cyberpunk 2077\UnityCrashHandler64.exe"),
                 fp(104, STEAM + r"\Cyberpunk 2077\_CommonRedist\vcredist\VC_redist.x64.exe")]
    check("game: only crash handler / redist of the install left -> NOT running",
          not is_running(t, leftovers, DIRS))
    check("game: user game_exclude applies too",
          not is_running(t, [other], DIRS, game_exclude=["REDprelauncher.exe"]))
    check("game: a different game -> not running",
          not is_running(t, [fp(105, STEAM + r"\Hades\Hades.exe")], DIRS))
    check("game: session filter applies to install matches",
          not is_running(t, [other], DIRS, session_ok=lambda pid: pid != 101))
    check("game: pid reused by a non-game process -> not running",
          not is_running(t, [fp(100, r"C:\Windows\notepad.exe", age=1)], DIRS))

    fort_exe = EPIC + r"\Fortnite\FortniteGame\Binaries\Win64\FortniteClient-Win64-Shipping.exe"
    ft = target_for_game(GameStart("f.exe", "Fortnite", 200, NOW - 30, fort_exe, 1.0), DIRS)
    launcher = [fp(201, EPIC + r"\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"),
                fp(202, EPIC + r"\Launcher\Engine\Binaries\Win64\EpicWebHelper.exe")]
    check("game: only the Epic launcher left -> NOT running", not is_running(ft, launcher, DIRS))
    check("game: relaunched through the launcher (new pid) -> running",
          is_running(ft, launcher + [fp(203, fort_exe, age=2)], DIRS))

    tg = target_for_game(gs, ["SomeOtherDir"])
    check("game outside the game dirs: game_key None, app rules apply",
          tg.game_key is None and is_running(tg, [game], ["SomeOtherDir"])
          and not is_running(tg, [other], ["SomeOtherDir"]), str(tg))
    check("game dirs changed after recording: own process still counts (member rule)",
          is_running(t, [game], ["SomethingElse"]))

    rep = SimpleNamespace(name="blender.exe", title="Blender", pid=300, create_time=NOW - 100, exe=BLENDER,
                          ram_mb=9000.0)
    a = target_for_app(rep, members=[(301, NOW - 90), (302, "bad"), ("x", 1.0)], now=5.0)
    check("target_for_app from a MemReport-like object",
          a.kind == "app" and a.title == "Blender" and a.game_key is None and a.exe == os.path.normcase(BLENDER)
          and a.members == frozenset({(300, NOW - 100), (301, NOW - 90)}) and a.since == 5.0, str(a))
    a2 = target_for_app(pid=7, create_time=NOW, exe=None, title="X")
    check("target_for_app from fields", a2.members == frozenset({(7, NOW)}) and a2.exe is None and a2.title == "X")

    child = fp(301, r"C:\Program Files\Blender Foundation\Blender 4.2\blender-launcher-helper.exe", age=90)
    check("app: a recorded member alive -> running", is_running(a, [child], DIRS))
    check("app: member create_time within tolerance -> running",
          is_running(a, [fp(301, r"C:\x\y.exe", age=90 - 0.5)], DIRS))
    check("app: pid reused with a different create_time -> NOT running",
          not is_running(a, [fp(300, r"C:\Windows\System32\svchost.exe", age=3),
                             fp(301, r"C:\x\other.exe", age=60)], DIRS))
    check("app: member with unknown create_time -> not counted",
          not is_running(a, [{"pid": 300, "name": "?", "exe": None, "create_time": None}], DIRS))
    check("app: same exe path relaunched (new pid) -> running",
          is_running(a, [fp(999, BLENDER.upper(), age=1)], DIRS))
    check("app: same exe in another session -> not running",
          not is_running(a, [fp(999, BLENDER, age=1)], DIRS, session_ok=lambda pid: False))
    check("app: same exe but it is our own pid -> not running",
          not is_running(a, [fp(999, BLENDER, age=1)], DIRS, self_pid=999))
    check("app: nothing related -> not running",
          not is_running(a, [fp(1, r"C:\Windows\explorer.exe"), fp(2, None)], DIRS))
    check("garbage entries never raise", not is_running(a, [{}, {"pid": None}, {"pid": "x", "exe": 5},
                                                            {"pid": 300, "create_time": "x"}], DIRS))


def _frozen(t: ResumeTarget) -> bool:
    try:
        t.title = "x"  # type: ignore[misc]
    except Exception:
        return True
    return False


# ----------------------------------------------------------------------------------------- 2. decide()

def run(seq, start: float = 0.0, **kw):
    """Feed (t, running) observations; returns ([t of closed reports], final state, [closed_for values])."""
    st = ResumeState(start=start)
    fired, held = [], []
    for t, r in seq:
        closed, st = decide(st, r, t, **kw)
        if closed:
            fired.append(t)
            held.append(st.closed_for)
    return fired, st, held


def test_decide() -> None:
    section("2. decide(): confirm window, gaps, expiry")
    f, st, held = run([(0, True), (5, False), (10, False), (15, False), (20, False), (25, False)])
    check("fires once after confirm_s (10 s) of consecutive misses", f == [15] and held == [10.0] and st.done,
          f"{f} {held}")
    f, _, _ = run([(0, True), (5, False), (10, True), (15, False), (20, True)])
    check("a single missed scan never fires", f == [], str(f))
    f, _, _ = run([(0, True), (5, False), (10, False), (12, True), (15, False), (20, False), (25, False)])
    check("running again resets the window", f == [25], str(f))
    f, st, _ = run([(0, False), (5, False), (10, False)])
    check("never seen running (already gone): closed after confirm_s", f == [10] and not st.seen_running, str(f))
    f, _, _ = run([(0, True), (5, False), (30, False), (35, False), (40, False)], max_gap_s=15)
    check("gap larger than max_gap_s restarts the window", f == [40], str(f))
    f, _, _ = run([(0, True), (100, False), (95, False), (100, False), (105, False)])
    check("clock going back restarts the window", f == [105], str(f))
    f, st, _ = run([(0, True), (3000, True), (6 * 3600, True), (6 * 3600 + 5, False)])
    check("expiry: ends silently (done, not closed)", f == [] and st.done, f"{f} {st}")
    f, st, _ = run([(0, True), (5, False), (10, False), (15, False), (20, False)], expire_s=12)
    check("expiry wins over a window that would complete later", f == [] and st.done, f"{f} {st}")
    s0 = ResumeState(start=0)
    closed, s1 = decide(s0, False, 5)
    check("decide() never mutates its input", s0 == ResumeState(start=0) and s1 is not s0 and not closed)
    closed, s2 = decide(ResumeState(start=0, done=True), False, 1000)
    check("done state stays silent", not closed and s2.done)
    f, _, _ = run([(0, True), (1, False), (2, False), (3, False)], confirm_s=2)
    check("custom confirm_s honoured", f == [3], str(f))

    check("ResumeConfig: defaults", ResumeConfig.from_config({}) == ResumeConfig(10.0, 21600.0))
    check("ResumeConfig: bad values -> defaults",
          ResumeConfig.from_config({"resume_confirm_s": "x", "resume_expire_s": True}) == ResumeConfig())
    check("ResumeConfig: NaN -> default", ResumeConfig.from_config({"resume_confirm_s": float("nan")}).confirm_s == 10)
    check("ResumeConfig: clamped", ResumeConfig.from_config({"resume_confirm_s": 0, "resume_expire_s": 10 ** 9})
          == ResumeConfig(3.0, 7 * 24 * 3600.0))
    check("ResumeConfig: None config", ResumeConfig.from_config(None) == ResumeConfig())


# ----------------------------------------------------------------------------------------- 3. watcher, stepped by hand

class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Snap:
    """Fake snapshot function: returns self.procs, counts calls, can raise."""
    def __init__(self, procs=None) -> None:
        self.procs = list(procs or [])
        self.calls = 0
        self.raise_next = 0

    def __call__(self):
        self.calls += 1
        if self.raise_next:
            self.raise_next -= 1
            raise RuntimeError("snapshot failure (expected in test)")
        return list(self.procs)


BG = [fp(4, r"C:\Windows\System32\svchost.exe")]      # a process that is always there (non-empty table)


def test_stepped() -> None:
    section("3. ResumeWatcher._tick() stepped with a fake clock (no thread)")
    clock, snap, got = FakeClock(), Snap(BG), []
    w = ResumeWatcher(DIRS, lambda t, s: got.append((t.title, s)), snapshot_fn=snap, clock=clock, interval_s=5)
    rep = SimpleNamespace(title="Blender", pid=300, create_time=NOW - 100, exe=BLENDER)
    tgt = target_for_app(rep)
    w.watch(tgt)
    check("watching after watch()", w.watching and w.target is tgt)
    snap.procs = BG + [fp(300, BLENDER, age=100)]
    w._tick()
    snap.procs = list(BG)
    for t in (5, 10):
        clock.t = t
        w._tick()
    check("not yet after 5 s gone", got == [] and w.watching, str(got))
    clock.t = 15
    w._tick()
    check("closed reported once with seconds closed", got == [("Blender", 10.0)] and not w.watching, str(got))
    clock.t = 20
    w._tick()
    check("no second report", len(got) == 1)

    got.clear()
    w.watch(tgt)
    clock.t = 100
    snap.raise_next = 3
    for _ in range(3):
        w._tick()
        clock.t += 5
    check("snapshot failures are not 'not running' observations", got == [] and w._state.missing_since is None,
          str(w._state))
    snap.procs = []
    for _ in range(4):
        w._tick()
        clock.t += 5
    check("empty snapshots ignored (never 'closed')", got == [] and w.watching, str(got))
    w.cancel()
    check("cancel(): not watching", not w.watching)

    # late report after cancel()/re-watch(): the generation check drops it
    got.clear()
    w.watch(tgt)
    snap.procs = list(BG)
    orig = w._snapshot

    def cancelling_snapshot():
        w.cancel()                                   # cancelled while this pass is mid-scan
        return orig()
    clock.t = 200
    w._tick()
    w.watch(tgt)
    clock.t = 300
    w._tick()
    clock.t = 315
    w._snapshot = cancelling_snapshot
    w._tick()
    check("cancel() during a pass suppresses its report", got == [] and not w.watching, str(got))
    w._snapshot = orig

    other = target_for_app(pid=55, create_time=NOW, exe=r"C:\other\o.exe", title="Other")
    w.watch(tgt)
    clock.t = 400
    w._tick()

    def rewatching_snapshot():
        w.watch(other)                               # a new target arrives mid-pass
        return orig()
    clock.t = 415
    w._snapshot = rewatching_snapshot
    w._tick()
    check("re-watch() during a pass suppresses the old target's report", got == [] and w.target is other, str(got))
    w._snapshot = orig
    check("... and the new target starts a fresh state", w._state is not None and w._state.missing_since is None,
          str(w._state))

    # expiry through the watcher
    got.clear()
    w2 = ResumeWatcher(DIRS, lambda t, s: got.append(t), snapshot_fn=Snap(BG + [fp(300, BLENDER, age=1)]),
                       clock=clock, cfg={"resume_expire_s": 60})
    clock.t = 0
    w2.watch(tgt)
    w2._tick()
    clock.t = 61
    w2._tick()
    check("expire via config: watch ends silently", got == [] and not w2.watching)

    # callback raising
    def boom(t, s):
        got.append(t)
        raise RuntimeError("callback failure (expected in test)")
    w3 = ResumeWatcher(DIRS, boom, snapshot_fn=Snap(BG), clock=clock, confirm_s=0)
    w3.watch(tgt)
    try:
        w3._tick()
        ok = True
    except Exception:
        ok = False
    check("raising on_closed is caught", ok and len(got) == 1)

    # config callable read each pass + confirm from config
    cfg = {"resume_confirm_s": 3}
    got.clear()
    w4 = ResumeWatcher(lambda: DIRS, lambda t, s: got.append(s), snapshot_fn=Snap(BG), clock=clock,
                       cfg=lambda: cfg)
    clock.t = 0
    w4.watch(tgt)
    w4._tick()
    clock.t = 3
    w4._tick()
    check("cfg callable: resume_confirm_s honoured", got == [3.0], str(got))

    # game target via the watcher with a game_dirs provider
    gt = target_for_game(GameStart("g.exe", "G", 10, NOW, STEAM + r"\G\g.exe", 1.0), DIRS)
    s5 = Snap(BG + [fp(11, STEAM + r"\G\other.exe")])
    got.clear()
    w5 = ResumeWatcher(lambda: DIRS, lambda t, s: got.append(t.title), snapshot_fn=s5, clock=clock, confirm_s=1)
    clock.t = 0
    w5.watch(gt)
    for t in (0, 1, 2, 3):
        clock.t = t
        w5._tick()
    check("game: another exe of the install keeps it running", got == [] and w5.watching)
    s5.procs = BG + [fp(12, STEAM + r"\G\UnityCrashHandler64.exe")]
    for t in (4, 5, 6):
        clock.t = t
        w5._tick()
    check("game: crash handler alone -> closed", got == ["G"], str(got))
    check("fake snapshot: no session filter by default", w5._session_ok is None)
    check("real snapshot: current-session filter by default", ResumeWatcher(DIRS)._session_ok is not None)
    check("default snapshot is game_watch.snapshot", ResumeWatcher(DIRS)._snapshot is game_watch.snapshot)


# ----------------------------------------------------------------------------------------- 4. real thread

def test_thread() -> None:
    section("4. real 'resume-watch' thread with a fake snapshot function")
    snap = Snap(BG)
    got: list[tuple[str, float, str]] = []
    lock = threading.Lock()

    def on_closed(t: ResumeTarget, s: float) -> None:
        with lock:
            got.append((t.title, s, threading.current_thread().name))

    w = ResumeWatcher(DIRS, on_closed, snapshot_fn=snap, interval_s=0.05, confirm_s=0.3, max_gap_s=1.0)
    w.start()
    t1 = w._thread
    w.start()
    check("start(): daemon thread named 'resume-watch', idempotent",
          t1 is not None and t1.is_alive() and t1.daemon and t1.name == "resume-watch" and w._thread is t1)
    time.sleep(0.4)
    check("nothing watched -> snapshot_fn never called", snap.calls == 0 and w.scans == 0, f"calls={snap.calls}")

    tgt = target_for_app(pid=300, create_time=NOW - 100, exe=BLENDER, title="Blender")
    snap.procs = BG + [fp(300, BLENDER, age=100)]
    w.watch(tgt)
    check("watch() wakes the thread at once", wait_for(lambda: snap.calls >= 1, 0.5))
    time.sleep(0.5)
    check("still running -> no report", got == [] and w.watching, str(got))
    t0 = time.monotonic()
    snap.procs = list(BG)
    ok = wait_for(lambda: len(got) >= 1, 3.0)
    el = time.monotonic() - t0
    time.sleep(0.4)
    check("closed reported once, after ~confirm_s", ok and len(got) == 1 and 0.25 <= el < 2.0, f"{got} {el:.2f}s")
    check("on_closed ran on the 'resume-watch' thread", bool(got) and got[0][2] == "resume-watch", str(got))
    check("not watching after the report", not w.watching)
    n = snap.calls
    time.sleep(0.4)
    check("idle again: no scanning after the report", snap.calls == n, f"{n} -> {snap.calls}")

    got.clear()
    snap.raise_next = 3
    w.watch(tgt)
    ok = wait_for(lambda: len(got) >= 1, 3.0)
    check("snapshot raising does not kill the thread", ok and t1.is_alive(), str(got))

    got.clear()

    def boom(t, s):
        got.append((t.title, s, "boom"))
        raise RuntimeError("callback failure (expected in test)")
    w.on_closed = boom
    w.watch(tgt)
    wait_for(lambda: len(got) >= 1, 3.0)
    w.on_closed = on_closed
    w.watch(tgt)
    ok = wait_for(lambda: len(got) >= 2, 3.0)
    check("raising on_closed is logged, thread keeps running", ok and t1.is_alive(), str(got))

    got.clear()
    w.watch(tgt)
    time.sleep(0.15)
    w.cancel()
    time.sleep(0.6)
    check("cancel() before confirm: no report", got == [], str(got))

    w.watch(tgt)
    time.sleep(0.15)
    w.watch(target_for_app(pid=301, create_time=NOW, exe=None, title="Second"))
    ok = wait_for(lambda: len(got) >= 1, 3.0)
    time.sleep(0.4)
    check("re-watch(): only the new target is reported", ok and [g[0] for g in got] == ["Second"], str(got))

    w.watch(tgt)
    time.sleep(0.1)
    t = time.monotonic()
    w.stop()
    check("stop() joins within 2 s while watching", not t1.is_alive() and time.monotonic() - t < 2.0)
    w.stop()
    check("stop() idempotent", True)
    w2 = ResumeWatcher(DIRS, snapshot_fn=snap)
    w2.stop()
    check("stop() before start() is harmless", True)
    w3 = ResumeWatcher(DIRS, snapshot_fn=snap, interval_s=60)
    w3.start()
    w3.watch(tgt)
    time.sleep(0.2)
    t = time.monotonic()
    w3.stop()
    check("stop() interrupts a long interval promptly", time.monotonic() - t < 1.0 and not w3._thread.is_alive())


# ----------------------------------------------------------------------------------------- 5. real snapshot (read-only)

def test_real_snapshot() -> None:
    section("5. is_running() over one real snapshot (read-only)")
    me = psutil.Process(os.getpid())
    procs = game_watch.snapshot()
    tgt = target_for_app(pid=me.pid, create_time=me.create_time(), exe=None, title="this test")
    check("this test process counts as running (member rule, real create_time)", is_running(tgt, procs, DIRS))
    fake = target_for_app(pid=me.pid, create_time=me.create_time() - 1000, exe=None, title="reused pid")
    check("same pid, other create_time -> not running (real snapshot)", not is_running(fake, procs, DIRS))


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="  log %(levelname)s %(message)s")
    for fn in (test_targets_and_running, test_decide, test_stepped, test_thread, test_real_snapshot):
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(f"{fn.__name__} raised", False, "exception")
    check("module never imports a kill/close helper",
          not any(s in Path(resume_watch.__file__).read_text(encoding="utf-8")
                  for s in (".kill(", ".terminate(", ".suspend(", "sweep_processes", "TerminateProcess")))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
