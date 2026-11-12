"""Self-check for game_watch.py. Run: .venv\\Scripts\\python.exe tests\\test_game_watch.py

Safe by construction:
  * game_watch only observes; this test never kills anything except the dummy `fakegame.exe` processes it
    starts itself: TEMP COPIES of the venv's python.exe redirector placed under
    <tmp>\\steamapps\\common\\<Game>\\, running a sleep script whose command line carries this run's unique
    MARKER. Cleanup kills only processes with that marker. No windows, no GUI, no network.
  * The watcher's game dir is the unique '<tmpname>\\steamapps' segment, so a real game the user starts
    meanwhile cannot leak into the assertions.
"""
from __future__ import annotations

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
from game_watch import GameStart, GameWatcher, baseline_keys, install_of, scan_once  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
CREATE_NO_WINDOW = 0x08000000
MARKER = f"hc-gamewatch-test-{uuid.uuid4().hex[:12]}"
TMP = Path(tempfile.mkdtemp(prefix=MARKER + "-"))
VENV = ROOT / ".venv"
TEST_DIR = TMP.name + "\\steamapps"            # unique game-dir segment for the real-thread tests
DIRS = ["steamapps", "Epic Games", "Riot Games", "XboxGames", "GOG Galaxy", "Battle.net"]


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


NOW = 1_000_000.0


def fp(pid: int, exe: str, age: float = 10.0, name: str | None = None, ram: float = 100.0, **kw) -> dict:
    d = {"pid": pid, "name": name if name is not None else exe.replace("/", "\\").split("\\")[-1],
         "exe": exe, "create_time": NOW - age, "ram_mb": ram}
    d.update(kw)
    return d


STEAM = r"C:\Program Files (x86)\Steam\steamapps\common"


# ----------------------------------------------------------------------------------------- pure tests

def test_pure() -> None:
    section("1. scan_once with fake process lists")
    elden = fp(10, STEAM + r"\ELDEN RING\Game\eldenring.exe", ram=900)
    other = fp(11, r"C:\Program Files\Mozilla Firefox\firefox.exe")
    res = scan_once(set(), DIRS, [], NOW, 2.0, [elden, other])
    check("new game reported", len(res) == 1 and isinstance(res[0], GameStart), str(res))
    if res:
        g = res[0]
        check("fields: name/title/pid/create_time/exe/ram_mb",
              (g.name, g.title, g.pid, g.create_time, g.exe, g.ram_mb)
              == ("eldenring.exe", "ELDEN RING", 10, NOW - 10.0, elden["exe"], 900.0), str(g))

    key = install_of(elden["exe"], DIRS)[0]
    check("baseline game not reported", scan_once({key}, DIRS, [], NOW, 2.0, [elden]) == [])
    check("baseline_keys() picks it up", baseline_keys(DIRS, [], None, [elden, other]) == {key})
    check("baseline_keys(before=) excludes later processes",
          baseline_keys(DIRS, [], NOW - 20.0, [elden]) == set() and baseline_keys(DIRS, [], NOW - 5.0, [elden])
          == {key})

    wp = fp(12, STEAM + r"\wallpaper_engine\wallpaper64.exe")
    wp2 = fp(13, STEAM + r"\Other\Wallpaper32.EXE")
    check("excluded names not reported (case-insensitive, .exe stripped)",
          scan_once(set(), DIRS, ["wallpaper64.exe", "WALLPAPER32"], NOW, 2.0, [wp, wp2]) == [])

    check("non-game dir not reported", scan_once(set(), DIRS, [], NOW, 2.0, [
        other, fp(14, r"C:\Games\MyGame\game.exe"), fp(15, r"C:\steamapps_backup\x\y.exe")]) == [])

    a = fp(20, STEAM + r"\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe", age=8, ram=3000)
    b = fp(21, STEAM + r"\Cyberpunk 2077\REDprelauncher.exe", age=9, ram=50)
    res = scan_once(set(), DIRS, [], NOW, 2.0, [a, b])
    check("two exes of the same install -> one alert", len(res) == 1, str(res))
    if res:
        check("... earliest process represents it, RAM summed",
              res[0].pid == 21 and res[0].title == "Cyberpunk 2077" and res[0].ram_mb == 3050.0, str(res[0]))

    y = fp(30, STEAM + r"\Hades\Hades.exe", age=0.5)
    pend: list = []
    check("too young -> not yet", scan_once(set(), DIRS, [], NOW, 2.0, [y], pending=pend) == [])
    check("... young key reported via pending", pend == [install_of(y["exe"], DIRS)[0]], str(pend))
    later = scan_once(set(), DIRS, [], NOW + 3.0, 2.0, [y])
    check("... reported on a later scan", [g.pid for g in later] == [30], str(later))
    mixed = scan_once(set(), DIRS, [], NOW, 2.0, [y, fp(31, STEAM + r"\Hades\x64\Hades2.exe", age=5)])
    check("young + old exe of same install -> one alert now (old one)", [g.pid for g in mixed] == [31], str(mixed))

    up = fp(32, r"D:\SteamLibrary\STEAMAPPS\COMMON\Portal 2\portal2.exe")
    res = scan_once(set(), ["SteamApps"], [], NOW, 2.0, [up])
    check("case-insensitive game dirs, title keeps original case",
          [g.title for g in res] == ["Portal 2"], str(res))
    res = scan_once(set(), ["/steamapps/", "", "  "], [], NOW, 2.0, [up])
    check("slashes stripped, blank dirs ignored", len(res) == 1, str(res))
    check("no game dirs -> nothing", scan_once(set(), [], [], NOW, 2.0, [up]) == [])

    check("dead process (alive False) not reported",
          scan_once(set(), DIRS, [], NOW, 2.0, [fp(40, STEAM + r"\Dead\dead.exe", alive=False)]) == [])
    check("session_ok filter applied",
          scan_once(set(), DIRS, [], NOW, 2.0, [elden], session_ok=lambda pid: pid != 10) == [])
    check("exe None / create_time None skipped", scan_once(set(), DIRS, [], NOW, 2.0, [
        {"pid": 41, "name": "x.exe", "exe": None, "create_time": NOW - 9},
        {"pid": 42, "name": "y.exe", "exe": STEAM + r"\Y\y.exe", "create_time": None}]) == [])
    check("garbage entries never raise", scan_once(set(), DIRS, [], NOW, 2.0, [
        {}, {"pid": None, "exe": STEAM + r"\Z\z.exe"}, {"pid": 5, "exe": 123, "create_time": "x"}]) == [])

    epic = r"C:\Program Files\Epic Games"
    launchers = [fp(50, epic + r"\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"),
                 fp(51, epic + r"\Launcher\Engine\Binaries\Win64\EpicWebHelper.exe"),
                 fp(52, r"C:\Riot Games\Riot Client\RiotClientServices.exe"),
                 fp(53, r"C:\Program Files (x86)\Battle.net\Battle.net.exe"),
                 fp(54, STEAM + r"\Steamworks Shared\_CommonRedist\DirectX\Jun2010\DXSETUP.exe"),
                 fp(55, STEAM + r"\SomeGame\_CommonRedist\vcredist\2019\VC_redist.x64.exe")]
    check("launchers / redist installers never alert", scan_once(set(), DIRS, [], NOW, 2.0, launchers) == [])
    fort = fp(56, epic + r"\Fortnite\FortniteGame\Binaries\Win64\FortniteClient-Win64-Shipping.exe")
    res = scan_once(set(), DIRS, [], NOW, 2.0, launchers + [fort])
    check("game launched from Epic alerts with its folder title", [g.title for g in res] == ["Fortnite"], str(res))

    gog = r"C:\Program Files (x86)\GOG Galaxy\Games"
    res = scan_once(set(), DIRS, [], NOW, 2.0, [fp(60, gog + r"\Witcher 3\bin\x64\witcher3.exe"),
                                                  fp(61, gog + r"\Cyberpunk\bin\x64\cp.exe")])
    check("GOG 'Games' container: one alert per game folder", sorted(g.title for g in res)
          == ["Cyberpunk", "Witcher 3"], str(res))
    res = scan_once(set(), ["MyGames"], [], NOW, 2.0, [fp(62, r"D:\MyGames\tetris.exe")])
    check("exe directly in the game dir -> exe stem title", [g.title for g in res] == ["tetris"], str(res))
    res = scan_once(set(), DIRS, [], NOW, 2.0, [fp(63, r"C:\XboxGames\Halo Infinite\Content\HaloInfinite.exe")])
    check("Xbox install folder title", [g.title for g in res] == ["Halo Infinite"], str(res))

    res = scan_once(set(), DIRS, [], NOW, 2.0, [fp(71, STEAM + r"\B\b.exe", age=3), fp(70, STEAM + r"\A\a.exe", age=6)])
    check("several games -> sorted by create_time", [g.title for g in res] == ["A", "B"], str(res))


# ----------------------------------------------------------------------------------------- dummy processes

def game_exe(game: str, exe_name: str) -> str:
    """Copy of the venv redirector at <tmp>\\steamapps\\common\\<game>\\<exe_name> (+ pyvenv.cfg next to it)."""
    d = TMP / "steamapps" / "common" / game
    exe = d / exe_name
    if not exe.exists():
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(VENV / "pyvenv.cfg", d / "pyvenv.cfg")
        shutil.copyfile(VENV / "Scripts" / "python.exe", exe)
    return str(exe)


def sleep_script() -> str:
    s = TMP / "sleep.py"
    if not s.exists():
        s.write_text("import sys, time\ntime.sleep(float(sys.argv[2]))\n", encoding="utf-8")
    return str(s)


SPAWNED: list[subprocess.Popen] = []


def spawn(game: str, exe_name: str, seconds: float = 60.0) -> subprocess.Popen:
    p = subprocess.Popen([game_exe(game, exe_name), sleep_script(), MARKER, str(seconds)], cwd=str(TMP),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW)
    SPAWNED.append(p)
    return p


def ours() -> list[psutil.Process]:
    """Processes started by this run: our Popen pids (+ their descendants) and anything carrying MARKER."""
    me = os.getpid()
    out: dict[int, psutil.Process] = {}
    for p in psutil.process_iter(["cmdline", "exe"], ad_value=None):
        if p.pid == me:
            continue
        cl = p.info.get("cmdline") or []
        exe = str(p.info.get("exe") or "")
        if any(MARKER in str(c) for c in cl) or MARKER in exe:
            out[p.pid] = p
    return list(out.values())


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


# ----------------------------------------------------------------------------------------- thread tests

def test_thread_lifecycle() -> None:
    section("2. thread lifecycle (no processes)")
    w = GameWatcher([TEST_DIR], interval_s=0.2, min_age_s=0.5)
    w.start()
    t1 = w._thread
    w.start()
    check("start(): daemon thread named 'game-watch', idempotent",
          t1 is not None and t1.is_alive() and t1.daemon and t1.name == "game-watch" and w._thread is t1)
    time.sleep(0.6)
    check("disarmed: no scanning", w.scans == 0, f"scans={w.scans}")
    w.arm()
    check("armed scans", wait_for(lambda: w.scans >= 2, 3.0), f"scans={w.scans}")
    check("armed property", w.armed)
    w.disarm()
    time.sleep(0.3)
    n = w.scans
    time.sleep(0.8)
    check("disarm stops scanning", w.scans == n and not w.armed, f"{n} -> {w.scans}")
    t = time.monotonic()
    w.stop()
    el = time.monotonic() - t
    check("stop() joins within 2 s", not t1.is_alive() and el < 2.0, f"{el:.2f}s")
    w.stop()
    check("stop() idempotent", True)
    w2 = GameWatcher([TEST_DIR], interval_s=5.0)
    w2.start()
    w2.arm()
    time.sleep(0.3)
    t = time.monotonic()
    w2.stop()
    check("stop() interrupts a long armed interval promptly", time.monotonic() - t < 1.0 and not w2._thread.is_alive())
    w3 = GameWatcher([TEST_DIR])
    w3.stop()
    check("stop() before start() is harmless", True)


def test_real_thread() -> None:
    section("3. real thread with dummy game processes")
    alerts: list[GameStart] = []
    cb_threads: list[str] = []
    lock = threading.Lock()

    def on_game(g: GameStart) -> None:
        with lock:
            alerts.append(g)
            cb_threads.append(threading.current_thread().name)

    pre = spawn("PreGame", "pregame.exe")
    try:
        pre_ps = psutil.Process(pre.pid)
        check("dummy game runs from the steamapps copy", MARKER in pre_ps.exe(), pre_ps.exe())
    except psutil.Error as exc:
        check("dummy game runs from the steamapps copy", False, repr(exc))
    time.sleep(1.0)
    w = GameWatcher([TEST_DIR], game_exclude=["excluded.exe"], interval_s=0.4, on_game=on_game, min_age_s=1.5)
    w.start()
    w.arm()
    time.sleep(0.2)
    t0 = time.monotonic()
    spawn("FakeGame", "fakegame.exe")
    time.sleep(0.3)
    spawn("FakeGame", "fakegame_helper.exe")           # same install: must not alert twice
    spawn("Updater", "updater.exe", seconds=0.1)        # short-lived: exits before min_age
    spawn("Excluded", "excluded.exe")                   # excluded name
    got = wait_for(lambda: len(alerts) >= 1, 8.0)
    el = time.monotonic() - t0
    time.sleep(2.5)                                     # give it time to (wrongly) report more
    titles = [g.title for g in alerts]
    check("exactly one GameStart, title 'FakeGame'", got and titles == ["FakeGame"], f"{titles}")
    check("alert within a few seconds", got and el < 5.0, f"{el:.2f}s")
    if alerts:
        g = alerts[0]
        check("GameStart fields are real", g.name.lower() in ("fakegame.exe", "fakegame_helper.exe")
              and psutil.pid_exists(g.pid) and MARKER in g.exe and g.ram_mb > 0 and g.create_time > 0, str(g))
    check("on_game ran on the 'game-watch' thread", cb_threads[:1] == ["game-watch"], str(cb_threads))
    check("pre-arm game never alerts", "PreGame" not in titles)
    check("short-lived updater never alerts", "Updater" not in titles)
    check("excluded exe never alerts", "Excluded" not in titles)
    check("scan cost recorded", w.last_scan_ms is not None and w.last_scan_ms < 200, f"{w.last_scan_ms}")

    w.disarm()
    alerts.clear()
    spawn("LaterGame", "latergame.exe")
    time.sleep(2.5)
    check("disarmed: a new game does not alert", alerts == [], str(alerts))
    w.arm()                                             # LaterGame is running now -> baseline
    time.sleep(2.5)
    check("re-arm: games already running are baseline", alerts == [], str(alerts))

    def boom(g: GameStart) -> None:
        alerts.append(g)
        raise RuntimeError("callback failure (expected in test)")
    w.on_game = boom
    spawn("CrashCb", "crashcb.exe")
    got = wait_for(lambda: len(alerts) >= 1, 8.0)
    spawn("AfterCrash", "aftercrash.exe")
    got2 = wait_for(lambda: len(alerts) >= 2, 8.0)
    check("raising on_game is logged, thread keeps running",
          got and got2 and w._thread.is_alive() and [g.title for g in alerts] == ["CrashCb", "AfterCrash"],
          str([g.title for g in alerts]))
    t = time.monotonic()
    w.stop()
    check("stop() joins within 2 s", not w._thread.is_alive() and time.monotonic() - t < 2.0)


def test_real_scan_cost() -> None:
    section("4. scan cost on this PC")
    game_watch.snapshot()                               # warm psutil's per-process cache, like the thread does
    ok = game_watch.current_session_filter()
    ts = []
    for _ in range(10):
        t = time.perf_counter()
        scan_once(set(), DIRS, [], time.time(), 2.0, session_ok=ok)
        ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    med = ts[len(ts) // 2]
    print(f"  scan_once (real, warm): median {med:.1f} ms, max {ts[-1]:.1f} ms, {len(psutil.pids())} processes")
    check("typical scan < 60 ms", med < 60.0, f"{med:.1f} ms")


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="  log %(levelname)s %(message)s")
    try:
        for fn in (test_pure, test_thread_lifecycle, test_real_thread, test_real_scan_cost):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
    finally:
        left = kill_ours()
        time.sleep(0.2)
        check("no dummy processes left behind", not ours(), f"killed {left} at exit")
        shutil.rmtree(TMP, ignore_errors=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
