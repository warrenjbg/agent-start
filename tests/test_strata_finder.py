r"""Self-check for strata_finder.py. Run: .venv\Scripts\python.exe tests\test_strata_finder.py

Safe by construction: every fake Strata tree lives in a fresh temp dir that is removed at exit. Nothing is
executed (the .bat / python.exe files are empty stubs that launcher_bat only reads). The only subprocess is
`cmd /c mklink /J` creating a junction INSIDE the temp dir. The real-PC smoke is a read-only find_strata() walk.
Hostile UNC / device paths (\\evilhost\...) are only ever parsed in safe mode with every filesystem entry point
monkeypatched to record calls; the tests assert that none of them was called with such a path.
Wall-clock asserts keep >= 3x margin over the expected time (AV scanners slow file I/O).
"""
from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launcher_bat  # noqa: E402
import strata_finder  # noqa: E402
from launcher_bat import LauncherError  # noqa: E402
from strata_finder import Found, best_launcher, default_roots, find_strata  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="hc-strata-finder-"))
CREATE_NO_WINDOW = 0x08000000


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def bat_text(sdir: Path, n: str, port: int = 8080, python: Path | None = None) -> str:
    py = python or sdir / ".venv" / "Scripts" / "python.exe"
    return (f'@echo off\r\ntitle Strata {n}\r\ncd /d "{sdir}"\r\n"{py}" "{sdir}\\serve\\server.py" "--engine" "strata" '
            f'"--config" "{sdir}\\strata-{n}.json" "--port" "{port}" "--open"\r\nif errorlevel 1 pause\r\n')


def make_strata(parent: Path, name: str, models=("a",), with_python: bool = True, with_server: bool = True) -> Path:
    """Fake Strata install (same layout as tests/test_ui_smoke.py make_strata)."""
    sdir = parent / name
    (sdir / "serve").mkdir(parents=True, exist_ok=True)
    (sdir / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
    if with_server:
        (sdir / "serve" / "server.py").write_text("", encoding="utf-8")
    if with_python:
        (sdir / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    for n in models:
        (sdir / f"strata-{n}.json").write_text(json.dumps({"model_name": f"model-{n}"}), encoding="utf-8")
        (sdir / f"run-{n}.bat").write_text(bat_text(sdir, n), encoding="utf-8", newline="")
    (sdir / "run-broken.bat").write_text("@echo off\r\necho nothing to see\r\npause\r\n", encoding="utf-8",
                                         newline="")
    (sdir / "SETUP.bat").write_text("@echo off\r\n", encoding="utf-8", newline="")
    return sdir


def nest(base: Path, depth: int, prefix: str) -> Path:
    """base/<prefix>1/<prefix>2/... with depth-1 intermediate dirs; the Strata goes at the returned parent."""
    p = base
    for i in range(1, depth):
        p = p / f"{prefix}{i}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def keys(found: list[Found]) -> set[str]:
    return {os.path.normcase(os.path.realpath(f.strata_dir)) for f in found}


def k(p: Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


# ------------------------------------------------------------------------------------------- tests
def test_depths() -> None:
    section("depth 1..4 found, depth 5 not (max_depth=4)")
    base = TMP / "depths"
    base.mkdir()
    want = {}
    for d in range(1, 6):
        sdir = make_strata(nest(base / f"tree{d}", d, "lvl"), f"Strata-d{d}")
        want[d] = sdir
    # each Strata sits at depth d below `base` + 1 for the tree<d> folder -> make depth relative to tree roots
    roots = [str(base / f"tree{d}") for d in range(1, 6)]
    t0 = time.monotonic()
    found = find_strata(roots, time_budget_s=10, max_depth=4)
    dt = time.monotonic() - t0
    got = keys(found)
    for d in range(1, 5):
        check(f"Strata at depth {d} found", k(want[d]) in got, f"got {sorted(got)}")
    check("Strata at depth 5 NOT found (max_depth=4)", k(want[5]) not in got)
    check("max_depth=1 finds only the depth-1 Strata",
          keys(find_strata(roots, time_budget_s=10, max_depth=1)) == {k(want[1])})
    f1 = next((f for f in found if k(Path(f.strata_dir)) == k(want[1])), None)
    if check("Found entry has launchers", f1 is not None and len(f1.launchers) == 1):
        check("launcher is run-a.bat (run-broken.bat excluded)", os.path.basename(f1.launchers[0]) == "run-a.bat")
        check("newest_mtime == launcher mtime", abs(f1.newest_mtime - os.path.getmtime(f1.launchers[0])) < 1e-6)
    check("small tree walk is fast", dt < 3, f"{dt:.2f}s")


def test_drive_root_depth_cap() -> None:
    section("drive roots are capped at depth 3")
    check("DRIVE_ROOT_MAX_DEPTH is 3", strata_finder.DRIVE_ROOT_MAX_DEPTH == 3)
    check("_is_drive_root('C:\\\\')", strata_finder._is_drive_root("C:\\"))
    check("not _is_drive_root(temp dir)", not strata_finder._is_drive_root(str(TMP)))


def test_decoys() -> None:
    section("decoys are not reported")
    base = TMP / "decoys"
    base.mkdir()
    # 1) serve/server.py but no run-*.bat
    d1 = base / "NoBat"
    (d1 / "serve").mkdir(parents=True)
    (d1 / "serve" / "server.py").write_text("", encoding="utf-8")
    (d1 / "START-HERE.bat").write_text("@echo off\r\n", encoding="utf-8")
    # 2) run-*.bat that does not launch serve/server.py
    d2 = base / "WrongBat"
    (d2 / "serve").mkdir(parents=True)
    (d2 / "serve" / "server.py").write_text("", encoding="utf-8")
    (d2 / ".venv" / "Scripts").mkdir(parents=True)
    (d2 / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (d2 / "run-x.bat").write_text(f'@echo off\r\n"{d2}\\.venv\\Scripts\\python.exe" "{d2}\\other.py"\r\n',
                                  encoding="utf-8", newline="")
    # 3) bat whose python.exe is missing
    d3 = make_strata(base, "NoPython", with_python=False)
    # 4) a run-*.bat with a valid launch line but no serve/server.py in this folder (and no serve dir)
    d4 = base / "NoServe"
    d4.mkdir()
    (d4 / "run-a.bat").write_text(bat_text(d4, "a"), encoding="utf-8", newline="")
    # 5) bat in folder A that launches folder B's server (B is real, but outside the search roots)
    other_root = TMP / "outside"
    other_root.mkdir()
    real_b = make_strata(other_root, "RealB")
    d5 = base / "Foreign"
    (d5 / "serve").mkdir(parents=True)
    (d5 / "serve" / "server.py").write_text("", encoding="utf-8")
    (d5 / "run-b.bat").write_text(bat_text(real_b, "a"), encoding="utf-8", newline="")
    good = make_strata(base, "Good")
    found = find_strata([str(base)], time_budget_s=10)
    got = keys(found)
    check("serve/server.py without run-*.bat NOT found", k(d1) not in got)
    check("run-*.bat not launching serve/server.py NOT found", k(d2) not in got)
    check("bat with missing python.exe NOT found", k(d3) not in got)
    check("run-*.bat without serve/server.py NOT found", k(d4) not in got)
    check("bat launching another folder's server NOT found", k(d5) not in got)
    check("the real one IS found", got == {k(good)}, f"got {sorted(got)}")


def test_skips_and_junction() -> None:
    section("skip-list, hidden dirs and junctions are not entered")
    base = TMP / "skips"
    base.mkdir()
    skipped = {}
    for name in ("node_modules", ".git", ".venv", "venv", "site-packages", "__pycache__", ".cache", "AppData",
                 "Program Files", "Windows", "ProgramData", "$Recycle.Bin", "NODE_MODULES_X"):
        if name == "NODE_MODULES_X":
            continue
        skipped[name] = make_strata(base / name, "Strata")
    # case-insensitive skip
    skipped["APPDATA-case"] = make_strata(base / "Cases" / "appdata", "Strata")
    # hidden dir
    hidden = base / "HiddenDir"
    sdir_h = make_strata(hidden, "Strata")
    try:
        import ctypes
        ok_h = ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
    except Exception:
        ok_h = 0
    good = make_strata(base, "Visible")
    # junction pointing at a Strata that lives OUTSIDE the root
    target_parent = TMP / "junction-target"
    target = make_strata(target_parent, "Linked")
    link = base / "Link"
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target_parent)], capture_output=True,
                           text=True, creationflags=CREATE_NO_WINDOW, timeout=20)
        junction_ok = r.returncode == 0 and link.exists()
    except Exception:
        junction_ok = False
    found = find_strata([str(base)], time_budget_s=10)
    got = keys(found)
    for name, sdir in skipped.items():
        check(f"Strata inside skipped dir '{name}' NOT found", k(sdir) not in got)
    if ok_h:
        check("Strata inside a hidden dir NOT found", k(sdir_h) not in got)
    else:
        print("  NOTE: could not set hidden attribute; hidden-dir check skipped")
    if junction_ok:
        reported = {os.path.normcase(os.path.normpath(f.strata_dir)) for f in found}
        check("junction not followed (Strata behind it NOT found)",
              k(target) not in got and os.path.normcase(str(link / "Linked")) not in reported,
              f"got {sorted(reported)}")
    else:
        print("  NOTE: mklink /J failed; junction check skipped")
    check("the visible Strata IS found", k(good) in got, f"got {sorted(got)}")
    # remove the junction itself (rmdir on a junction never touches the target)
    if junction_ok:
        try:
            os.rmdir(link)
        except OSError:
            pass


def test_nested_once() -> None:
    section("nested Strata inside a qualifying one is not reported (no descent)")
    base = TMP / "nested"
    outer = make_strata(base, "Outer")
    inner = make_strata(outer / "sub", "Inner")
    found = find_strata([str(base), str(outer), str(base)], time_budget_s=10)
    got = [os.path.normcase(os.path.realpath(f.strata_dir)) for f in found]
    check("outer found exactly once (overlapping roots deduped)", got.count(k(outer)) == 1, f"got {got}")
    check("inner (inside a qualifying folder) NOT reported when walking from above", k(inner) not in got[:1] and
          all(g != k(inner) for g in keys(find_strata([str(base)], time_budget_s=10))))
    check("no duplicates in results", len(got) == len(set(got)), f"got {got}")


def test_time_budget_and_stop() -> None:
    section("time budget and stop_evt")
    base = TMP / "wide"
    n = 0
    for a in range(20):
        for b in range(15):
            for c in range(10):
                (base / f"a{a}" / f"b{b}" / f"c{c}").mkdir(parents=True)
                n += 1
    print(f"  (built {n} leaf dirs)")
    t0 = time.monotonic()
    find_strata([str(base)], time_budget_s=0.05, max_depth=4)
    dt = time.monotonic() - t0
    # budget 0.05 + grace 0.25 = 0.3 s worst case expected -> 3x+ margin
    check("time_budget_s=0.05 on a ~3300-dir tree returns quickly", dt < 1.0, f"{dt:.3f}s")
    t0 = time.monotonic()
    full = find_strata([str(base)], time_budget_s=30, max_depth=4)
    full_dt = time.monotonic() - t0
    print(f"  (full walk of the tree: {full_dt:.2f}s, {len(full)} found)")
    # a Strata placed at the very end of the BFS order is missed with a tiny budget but found with a big one
    late = make_strata(base / "z-last" / "y" / "x", "Late")
    check("Strata deep in a big tree found with a generous budget",
          k(late) in keys(find_strata([str(base)], time_budget_s=30, max_depth=4)))
    zero = find_strata([str(base)], time_budget_s=0)
    check("time_budget_s=0 returns [] without raising", zero == [])
    evt = threading.Event()
    evt.set()
    t0 = time.monotonic()
    res = find_strata([str(base)], time_budget_s=30, stop_evt=evt)
    check("pre-set stop_evt aborts at once", res == [] and time.monotonic() - t0 < 1.0)
    evt2 = threading.Event()
    threading.Timer(0.02, evt2.set).start()
    t0 = time.monotonic()
    find_strata([str(base)], time_budget_s=30, stop_evt=evt2)
    dt = time.monotonic() - t0
    check("stop_evt set mid-walk aborts quickly", dt < 1.5, f"{dt:.3f}s")      # expected ~0.07 s
    # a single blocking step (slow disk / OneDrive listing) must not hold the caller past budget + grace
    slow_root = TMP / "slow"
    first = make_strata(slow_root, "First")
    orig = strata_finder._usable_launchers
    release = threading.Event()

    def slow_usable(path, bats, out_of_time=None):
        res = orig(path, bats, out_of_time)
        release.wait(6)                     # simulate a 6 s stall after the folder qualified
        return res

    strata_finder._usable_launchers = slow_usable
    try:
        t0 = time.monotonic()
        res = find_strata([str(slow_root)], time_budget_s=0.2)
        dt = time.monotonic() - t0
    finally:
        release.set()
        strata_finder._usable_launchers = orig
    # expected 0.2 + 0.25 grace = 0.45 s; the stall is 6 s
    check("blocked walker: returns within budget + grace", dt < 1.5, f"{dt:.3f}s")
    check("blocked walker: result is a list (partial, possibly empty)", isinstance(res, list) and
          all(k(Path(f.strata_dir)) == k(first) for f in res))
    evt3 = threading.Event()
    release2 = threading.Event()

    def slow_usable2(path, bats, out_of_time=None):
        release2.wait(6)
        return orig(path, bats, out_of_time)

    strata_finder._usable_launchers = slow_usable2
    try:
        threading.Timer(0.1, evt3.set).start()
        t0 = time.monotonic()
        find_strata([str(slow_root)], time_budget_s=30, stop_evt=evt3)
        dt = time.monotonic() - t0
    finally:
        release2.set()
        strata_finder._usable_launchers = orig
    check("blocked walker: stop_evt still returns promptly", dt < 1.5, f"{dt:.3f}s")   # expected ~0.15 s
    time.sleep(0.2)                         # let the released daemon walkers finish before temp cleanup


def test_sorting_and_best() -> None:
    section("results sorted newest first; best_launcher picks newest .bat")
    base = TMP / "sorting"
    old = make_strata(base, "Old", models=("a", "b"))
    mid = make_strata(base, "Mid", models=("a",))
    new = make_strata(base, "New", models=("a", "b", "c"))
    now = time.time()
    for bat in old.glob("run-*.bat"):
        os.utime(bat, (now - 3000, now - 3000))
    for bat in mid.glob("run-*.bat"):
        os.utime(bat, (now - 2000, now - 2000))
    os.utime(new / "run-a.bat", (now - 1000, now - 1000))
    os.utime(new / "run-b.bat", (now - 10, now - 10))         # newest in New
    os.utime(new / "run-c.bat", (now - 500, now - 500))
    os.utime(new / "run-broken.bat", (now, now))             # newest file but not a usable launcher
    found = find_strata([str(base)], time_budget_s=10)
    order = [Path(f.strata_dir).name for f in found]
    check("order is New, Mid, Old", order == ["New", "Mid", "Old"], f"got {order}")
    fn = found[0] if found else None
    if check("New has 3 launchers sorted by name", fn is not None and
             [os.path.basename(p) for p in fn.launchers] == ["run-a.bat", "run-b.bat", "run-c.bat"]):
        check("New.newest_mtime is run-b.bat's mtime", abs(fn.newest_mtime - (now - 10)) < 2)
        check("best_launcher(New) == run-b.bat (newest)", os.path.basename(best_launcher(fn)) == "run-b.bat")
    fo = found[-1] if found else None
    if fo is not None:
        check("best_launcher tie -> first by name (run-a.bat)", os.path.basename(best_launcher(fo)) == "run-a.bat")
    check("best_launcher on empty Found -> ''", best_launcher(Found(strata_dir=str(base), launchers=[])) == "")


def test_never_raises() -> None:
    section("bad input never raises")
    try:
        r1 = find_strata([str(TMP / "does-not-exist"), "", "Z:\\nope\\nope"], time_budget_s=2)
        r2 = find_strata([str(TMP / "sorting" / "Old" / "run-a.bat")], time_budget_s=2)   # a file as root
        check("missing / empty / file roots -> []", r1 == [] and r2 == [])
    except Exception as exc:
        check("missing / empty / file roots -> []", False, repr(exc))


def test_default_roots() -> None:
    section("default_roots()")
    roots = default_roots()
    print("  roots: " + " | ".join(roots))
    norm = [os.path.normcase(os.path.realpath(r)) for r in roots]
    check("default_roots non-empty, existing dirs", bool(roots) and all(os.path.isdir(r) for r in roots))
    check("default_roots deduplicated", len(norm) == len(set(norm)))
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    check("home folder included", home in norm)
    check("C:\\ (fixed drive) included", os.path.normcase("C:\\") in norm)
    if home in norm and os.path.normcase("C:\\") in norm:
        check("order: user folders, home, then drives", norm.index(home) < norm.index(os.path.normcase("C:\\")))
    dl = os.path.normcase(os.path.realpath(os.path.join(os.path.expanduser("~"), "Downloads")))
    if os.path.isdir(dl):
        check("Downloads is first", norm[0] == dl, f"first = {roots[0]}")
    od_desktop = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")
    if os.path.isdir(od_desktop):
        check("OneDrive-redirected Desktop resolved", os.path.normcase(os.path.realpath(od_desktop)) in norm)


EVIL = "evilhost"


@contextlib.contextmanager
def fs_recorder():
    """Record every filesystem entry point launcher_bat / strata_finder use; yields the list of (fn, args)."""
    calls: list[tuple[str, tuple]] = []
    targets = [(os.path, "isfile"), (os.path, "isdir"), (os.path, "exists"), (os.path, "realpath"),
               (os.path, "getmtime"), (os.path, "islink"), (os, "stat"), (os, "lstat"), (os, "scandir"),
               (os, "listdir"), (os, "readlink"), (builtins, "open"), (io, "open"), (shutil, "which")]
    saved = []
    for mod, name in targets:
        orig = getattr(mod, name)
        saved.append((mod, name, orig))

        def wrap(*a, __orig=orig, __name=name, **kw):
            calls.append((__name, a))
            return __orig(*a, **kw)
        setattr(mod, name, wrap)
    try:
        yield calls
    finally:
        for mod, name, orig in saved:
            setattr(mod, name, orig)


def touched(calls, needle: str = EVIL) -> list:
    return [(n, a) for n, a in calls if any(needle.casefold() in str(x).casefold() for x in a)]


def non_fixed_letter() -> str | None:
    """An UNUSED drive letter (GetDriveTypeW == DRIVE_NO_ROOT_DIR): never a mapped network drive, so even the
    normal-mode checks below cannot reach the network through it."""
    try:
        import ctypes
        for letter in "QRSTUVWXYZPONMLKJIHGFEB":
            if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(f"{letter}:\\")) == 1:
                return letter
    except Exception:
        pass
    return None


def test_multi_root_fairness() -> None:
    section("multi-root: a big slow first root does not starve later roots")
    base = TMP / "multiroot"
    big = base / "big"
    for i in range(40):
        (big / f"d{i:02d}").mkdir(parents=True)
    second = base / "second"
    s1 = make_strata(second, "StrataD1")                     # depth 1 of the 2nd root
    s2 = make_strata(second / "sub", "StrataD2")             # depth 2 of the 2nd root
    third = base / "third"
    s3 = make_strata(third, "StrataThird")
    big_key = os.path.normcase(str(big))
    order: list[str] = []
    orig = os.scandir

    def slow_scandir(path=".", *a, **kw):
        sp = os.path.normcase(os.fspath(path))
        order.append(sp)
        if sp.startswith(big_key):
            time.sleep(0.15)                                  # 41 listings x 0.15 s = 6 s > the 2 s budget
        return orig(path, *a, **kw)

    os.scandir = slow_scandir
    try:
        t0 = time.monotonic()
        found = find_strata([str(big), str(second), str(third)], time_budget_s=2.0)
        dt = time.monotonic() - t0
    finally:
        os.scandir = orig
    time.sleep(0.3)                                           # let the walker finish its last slow listing
    got = keys(found)
    check("install at depth 1 of the 2nd root found despite a slow 1st root", k(s1) in got, f"got {sorted(got)}")
    check("install at depth 2 of the 2nd root found too (fair share per root)", k(s2) in got, f"got {sorted(got)}")
    check("install in the 3rd root found", k(s3) in got, f"got {sorted(got)}")
    roots_k = [os.path.normcase(str(r)) for r in (big, second, third)]
    check("depth 0 of every root is listed before any depth-1 folder", order[:3] == roots_k, f"order {order[:5]}")
    check("multi-root walk stays within budget + grace (2.25 s; assert < 4 s)", dt < 4.0, f"{dt:.2f}s")
    check("the slow root was not walked to completion (budget really was the limit)",
          sum(1 for o in order if o.startswith(big_key)) < 41)
    # overlapping roots are still deduped: second listed both as itself and through base
    found2 = find_strata([str(base), str(second), str(second)], time_budget_s=10)
    names = [os.path.normcase(os.path.realpath(f.strata_dir)) for f in found2]
    check("overlapping roots: every install reported exactly once", len(names) == len(set(names)) and
          {k(s1), k(s2), k(s3)} <= set(names), f"got {names}")


def test_roots_none_in_thread() -> None:
    section("roots=None: default_roots() runs on the bounded walker thread")
    base = TMP / "rootsnone"
    sdir = make_strata(base, "ViaDefault")
    seen: list[str] = []
    orig = strata_finder.default_roots

    def fake_roots():
        seen.append(threading.current_thread().name)
        return [str(base)]

    strata_finder.default_roots = fake_roots
    try:
        found = find_strata(None, time_budget_s=10)
    finally:
        strata_finder.default_roots = orig
    check("default_roots() called once, on the 'strata-finder' thread", seen == ["strata-finder"], f"{seen}")
    check("roots from the in-thread default_roots() are walked", k(sdir) in keys(found))
    release = threading.Event()

    def blocking_roots():
        release.wait(6)                    # a redirected/offline Documents folder that hangs
        return [str(base)]

    strata_finder.default_roots = blocking_roots
    try:
        t0 = time.monotonic()
        res = find_strata(None, time_budget_s=0.2)
        dt = time.monotonic() - t0
    finally:
        release.set()
        strata_finder.default_roots = orig
    check("a blocking default_roots() cannot hold the caller (expected 0.45 s; assert < 1.5 s)",
          dt < 1.5 and res == [], f"{dt:.2f}s, {res}")
    time.sleep(0.1)


def test_default_roots_skip_network() -> None:
    section("default_roots() drops UNC / network-redirected known folders without touching them")
    orig_kf, orig_cs = strata_finder._known_folder, strata_finder._csidl_folder
    bad = {"{374DE290-123F-4565-9164-39C4925E467B}": rf"\\{EVIL}\share\Downloads",
           "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}": f"//{EVIL}/share/Desktop",
           "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}": rf"\\?\UNC\{EVIL}\share\Documents"}
    strata_finder._known_folder = lambda guid: bad.get(guid)
    strata_finder._csidl_folder = lambda csidl: None
    try:
        with fs_recorder() as calls:
            roots = default_roots()
    finally:
        strata_finder._known_folder, strata_finder._csidl_folder = orig_kf, orig_cs
    check("no UNC known folder in default_roots()", not any(EVIL in r for r in roots), f"{roots}")
    check("UNC known folders never touched (no isdir/stat/realpath/...)", not touched(calls), f"{touched(calls)}")
    check("home and C:\\ still present", os.path.normcase("C:\\") in [os.path.normcase(r) for r in roots])
    letter = non_fixed_letter()
    if letter:
        check(f"non-fixed drive {letter}: is not a local root", not strata_finder._local_root(f"{letter}:\\Docs"))
    with fs_recorder() as calls:
        res = find_strata([rf"\\{EVIL}\share", f"//{EVIL}/share", rf"\\?\UNC\{EVIL}\s", rf"\\.\{EVIL}"],
                          time_budget_s=2)
    check("explicit UNC/device roots: [] and never touched", res == [] and not touched(calls), f"{touched(calls)}")


def _hostile_bat(sdir: Path, body: str) -> Path:
    bat = sdir / "run-hostile.bat"
    bat.write_text("@echo off\r\n" + body + "\r\n", encoding="utf-8", newline="")
    return bat


def test_safe_mode_hostile_paths() -> None:
    section("safe mode: UNC / device / //host / non-fixed paths rejected before ANY filesystem access")
    sdir = make_strata(TMP / "hostile", "Strata")
    py = sdir / ".venv" / "Scripts" / "python.exe"
    srv = sdir / "serve" / "server.py"
    cfg = sdir / "strata-a.json"
    letter = non_fixed_letter()
    cases = {
        "UNC interpreter": f'"\\\\{EVIL}\\share\\python.exe" "{srv}" --config "{cfg}"',
        "//host interpreter": f'"//{EVIL}/share/python.exe" "{srv}" --config "{cfg}"',
        "\\\\?\\UNC interpreter": f'"\\\\?\\UNC\\{EVIL}\\share\\python.exe" "{srv}" --config "{cfg}"',
        "\\\\.\\ device interpreter": f'"\\\\.\\{EVIL}\\python.exe" "{srv}" --config "{cfg}"',
        "\\??\\UNC interpreter": f'"\\??\\UNC\\{EVIL}\\share\\python.exe" "{srv}" --config "{cfg}"',
        "UNC interpreter without .exe": f'"\\\\{EVIL}\\share\\python" "{srv}"',
        "UNC cwd + bare python": f'cd /d "\\\\{EVIL}\\share"\r\npython "{srv}"',
        "start /D UNC + bare python": f'start "" /D "\\\\{EVIL}\\share" python "{srv}"',
        "UNC --config": f'"{py}" "{srv}" --config "\\\\{EVIL}\\share\\c.json"',
        "//host --config=": f'"{py}" "{srv}" --config=//{EVIL}/share/c.json',
        "\\\\?\\UNC --config": f'"{py}" "{srv}" --config "\\\\?\\UNC\\{EVIL}\\s\\c.json"',
        "UNC in another argv element": f'"{py}" "{srv}" --config "{cfg}" --log-file=\\\\{EVIL}\\share\\x.log',
        "UNC -X value before server.py": f'"{py}" -X pycache_prefix=\\\\{EVIL}\\s "{srv}"',
        "UNC server.py": f'"{py}" "\\\\{EVIL}\\share\\serve\\server.py"',
        "reserved device name": f'"{sdir}\\CON\\{EVIL}\\python.exe" "{srv}"',
    }
    if letter:
        cases[f"non-fixed drive {letter}: interpreter"] = f'"{letter}:\\{EVIL}\\python.exe" "{srv}"'
        cases[f"non-fixed drive {letter}: --config"] = f'"{py}" "{srv}" --config "{letter}:\\{EVIL}\\c.json"'
    for name, body in cases.items():
        bat = _hostile_bat(sdir, body)
        err = None
        with fs_recorder() as calls:
            try:
                launcher_bat.parse_bat(str(bat), safe=True)
            except LauncherError as exc:
                err = exc
        check(f"{name}: LauncherError in safe mode", err is not None, "parsed without error")
        check(f"{name}: hostile path never touched", not touched(calls), f"{touched(calls)}")
    # a bare `python` with a UNC directory on PATH: that PATH entry is ignored (never probed) in safe mode
    bat = _hostile_bat(sdir, f'python "{srv}" --config "{cfg}"')
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([f"\\\\{EVIL}\\share\\bin", f"//{EVIL}/share/bin", str(py.parent)])
    try:
        with fs_recorder() as calls:
            info = launcher_bat.parse_bat(str(bat), safe=True)
    finally:
        os.environ["PATH"] = old_path
    check("bare python + UNC PATH entries: UNC entries never probed", not touched(calls), f"{touched(calls)}")
    check("bare python resolved from the local PATH entry", os.path.normcase(info.argv[0]) == os.path.normcase(str(py)),
          info.argv[0])
    # missing_files(safe=True) on a LauncherInfo that names a UNC interpreter: reported, not probed
    bad = launcher_bat.LauncherInfo(bat_path=str(bat), argv=(f"\\\\{EVIL}\\share\\python.exe", str(srv)),
                                    cwd=str(sdir), strata_dir=str(sdir), port=8080,
                                    config_path=f"//{EVIL}/share/c.json", model_name="x")
    with fs_recorder() as calls:
        miss = launcher_bat.missing_files(bad, safe=True)
    check("missing_files(safe=True) reports a UNC interpreter without touching it",
          bool(miss) and "not allowed" in miss and not touched(calls), f"{miss} / {touched(calls)}")
    # the finder never reports such a folder and never touches the hostile host
    fdir = make_strata(TMP / "hostile-find", "Strata", models=())
    (fdir / "run-evil.bat").write_text(f'@echo off\r\n"\\\\{EVIL}\\share\\python.exe" "{fdir}\\serve\\server.py"\r\n',
                                       encoding="utf-8", newline="")
    with fs_recorder() as calls:
        found = find_strata([str(TMP / "hostile-find")], time_budget_s=10)
    check("finder: folder whose only launcher is hostile NOT found", k(fdir) not in keys(found))
    check("finder: hostile host never touched during the walk", not touched(calls), f"{touched(calls)}")


def test_safe_mode_config_rules() -> None:
    section("safe mode: --config must be a local .json <= 1 MB")
    sdir = make_strata(TMP / "cfgrules", "Strata")
    py = sdir / ".venv" / "Scripts" / "python.exe"
    srv = sdir / "serve" / "server.py"
    outside = TMP / "cfgrules-outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text('{"model": "LEAKED"}', encoding="utf-8")
    bat = _hostile_bat(sdir, f'"{py}" "{srv}" --config "{secret}"')
    err = None
    with fs_recorder() as calls:
        try:
            launcher_bat.parse_bat(str(bat), safe=True)
        except LauncherError as exc:
            err = exc
    check("outside-folder .txt config rejected in safe mode", err is not None and ".json" in str(err), f"{err}")
    check(".txt config never opened or stat'ed", not touched(calls, "secret.txt"), f"{touched(calls, 'secret.txt')}")
    big = sdir / "strata-big.json"
    with open(big, "wb") as f:
        f.write(b'{"model": "BIG", "pad": "' + b"x" * (launcher_bat.MAX_CONFIG_BYTES) + b'"}')
    bat = _hostile_bat(sdir, f'"{py}" "{srv}" --config "{big}"')
    err = None
    with fs_recorder() as calls:
        try:
            launcher_bat.parse_bat(str(bat), safe=True)
        except LauncherError as exc:
            err = exc
    opened = [(n, a) for n, a in touched(calls, "strata-big.json") if n == "open"]
    check("config > 1 MB rejected in safe mode", err is not None and "too large" in str(err), f"{err}")
    check("config > 1 MB stat'ed but never opened", not opened, f"{opened}")
    exact = sdir / "strata-exact.json"
    payload = b'{"model": "EXACT", "pad": "'
    exact.write_bytes(payload + b"x" * (launcher_bat.MAX_CONFIG_BYTES - len(payload) - 2) + b'"}')
    bat = _hostile_bat(sdir, f'"{py}" "{srv}" --config "{exact}"')
    try:
        info = launcher_bat.parse_bat(str(bat), safe=True)
        check("config of exactly 1 MB accepted and its model name read", info.model_name == "EXACT", info.model_name)
    except LauncherError as exc:
        check("config of exactly 1 MB accepted and its model name read", False, str(exc))
    good = launcher_bat.parse_bat(str(sdir / "run-a.bat"), safe=True)
    check("normal local .json config: model name read in safe mode", good.model_name == "model-a", good.model_name)
    check("missing_files(safe=True) is None for a complete local launcher", launcher_bat.missing_files(good, safe=True)
          is None)
    for p in (big, exact):
        p.unlink()


def test_normal_mode_unchanged() -> None:
    section("normal (Settings) parsing is unchanged")
    sdir = make_strata(TMP / "normal", "Strata")
    py = sdir / ".venv" / "Scripts" / "python.exe"
    srv = sdir / "serve" / "server.py"
    bat = sdir / "run-a.bat"
    a, b = launcher_bat.parse_bat(str(bat)), launcher_bat.parse_bat(str(bat), safe=False)
    c = launcher_bat.parse_bat(str(bat), safe=True)
    check("parse_bat(p) == parse_bat(p, safe=False) == parse_bat(p, safe=True) for a local launcher", a == b == c)
    check("missing_files(info) unchanged (None)", launcher_bat.missing_files(a) is None)
    outside = TMP / "normal-outside"
    outside.mkdir()
    txt = outside / "cfg.txt"
    txt.write_text('{"model": "from-txt"}', encoding="utf-8")
    hb = _hostile_bat(sdir, f'"{py}" "{srv}" --config "{txt}"')
    try:
        info = launcher_bat.parse_bat(str(hb))
        check("normal mode still accepts a .txt config outside the folder (and reads its model)",
              info.config_path == os.path.normpath(str(txt)) and info.model_name == "from-txt", info.model_name)
    except LauncherError as exc:
        check("normal mode still accepts a .txt config outside the folder (and reads its model)", False, str(exc))
    letter = non_fixed_letter()
    if letter:
        hb = _hostile_bat(sdir, f'"{letter}:\\Py\\python.exe" "{srv}"')
        try:
            info = launcher_bat.parse_bat(str(hb))
            check(f"normal mode still accepts an interpreter on drive {letter}:",
                  info.argv[0] == f"{letter}:\\Py\\python.exe", info.argv[0])
            check("normal-mode missing_files reports it as not found",
                  (launcher_bat.missing_files(info) or "").startswith("Python not found"))
        except LauncherError as exc:
            check(f"normal mode still accepts an interpreter on drive {letter}:", False, str(exc))
    # the UNC string is only parsed (model_name_for/isfile are patched so nothing can reach the network)
    hb = _hostile_bat(sdir, f'"\\\\{EVIL}\\share\\python.exe" "{srv}"')
    orig_isfile = os.path.isfile
    os.path.isfile = lambda p: False if EVIL in str(p) else orig_isfile(p)
    try:
        info = launcher_bat.parse_bat(str(hb))
        check("normal mode still accepts a UNC interpreter (Settings behaviour)",
              info.argv[0] == f"\\\\{EVIL}\\share\\python.exe", info.argv[0])
    except LauncherError as exc:
        check("normal mode still accepts a UNC interpreter (Settings behaviour)", False, str(exc))
    finally:
        os.path.isfile = orig_isfile
    hb.unlink()


def test_cloud_only_skipped() -> None:
    section("cloud-only (OneDrive online-only / offline) folders and .bat files are skipped")
    base = TMP / "cloud"
    in_cloud_dir = make_strata(base / "CloudDir", "Strata")          # parent folder is online-only
    cloud_strata = make_strata(base, "CloudStrata")                   # the Strata folder itself is offline
    mixed = make_strata(base, "Mixed", models=("a", "b"))            # run-a.bat online-only, run-b.bat local
    only_cloud_bat = make_strata(base, "OnlyCloudBat")                 # its only launcher is online-only
    plain = make_strata(base, "Plain")
    marks = {os.path.normcase(str(base / "CloudDir")): 0x40000,         # RECALL_ON_OPEN
             os.path.normcase(str(cloud_strata)): 0x1000,               # OFFLINE
             os.path.normcase(str(mixed / "run-a.bat")): 0x400000,      # RECALL_ON_DATA_ACCESS
             os.path.normcase(str(only_cloud_bat / "run-a.bat")): 0x400000}
    orig = strata_finder._attrs

    def fake_attrs(st, path):
        return orig(st, path) | marks.get(os.path.normcase(path), 0)

    strata_finder._attrs = fake_attrs
    try:
        with fs_recorder() as calls:
            found = find_strata([str(base)], time_budget_s=10)
    finally:
        strata_finder._attrs = orig
    got = keys(found)
    listed = [a for n, a in calls if n == "scandir" and any("clouddir" in str(x).casefold() for x in a)]
    check("Strata inside an online-only folder NOT found (folder never listed)", k(in_cloud_dir) not in got and
          not listed, f"{listed[:3]}")
    check("offline Strata folder NOT found", k(cloud_strata) not in got)
    fm = next((f for f in found if k(Path(f.strata_dir)) == k(mixed)), None)
    check("online-only run-a.bat skipped, local run-b.bat kept",
          fm is not None and [os.path.basename(p) for p in fm.launchers] == ["run-b.bat"],
          f"{fm.launchers if fm else None}")
    check("folder whose only launcher is online-only NOT found", k(only_cloud_bat) not in got)
    opened = [a for n, a in calls if n == "open" and any("run-a.bat" in str(x) and ("Mixed" in str(x) or
              "OnlyCloudBat" in str(x)) for x in a)]
    check("online-only .bat files never opened", not opened, f"{opened}")
    check("plain Strata still found", k(plain) in got)


def test_abort_between_bats() -> None:
    section("budget/stop checked before every .bat")
    sdir = make_strata(TMP / "perbat", "Strata", models=("a", "b", "c", "d"))
    bats = sorted(p.name for p in sdir.glob("run-*.bat"))
    parsed: list[str] = []
    orig = launcher_bat.parse_bat

    def counting(path, *, safe=False):
        parsed.append(os.path.basename(path))
        return orig(path, safe=safe)

    launcher_bat.parse_bat = counting
    try:
        res = strata_finder._usable_launchers(str(sdir), bats, lambda: len(parsed) >= 2)
        all_res = strata_finder._usable_launchers(str(sdir), bats)
    finally:
        launcher_bat.parse_bat = orig
    check("out_of_time() after 2 .bats: no further .bat parsed", len(parsed) - len(bats) == 2, f"{parsed}")
    check("partial result keeps what was checked", len(res) <= 2)
    check("without out_of_time every usable .bat is returned", len(all_res) == 4, f"{all_res}")


def smoke_real_pc() -> None:
    section("real-PC smoke (read-only): find_strata(default_roots(), time_budget_s=10)")
    t0 = time.monotonic()
    found = find_strata(default_roots(), time_budget_s=10)
    dt = time.monotonic() - t0
    print(f"  took {dt:.2f}s, found {len(found)} Strata folder(s)")
    for f in found:
        print(f"    {f.strata_dir}  ({len(f.launchers)} launchers, newest {time.ctime(f.newest_mtime)}, "
              f"best {os.path.basename(best_launcher(f))})")
    check("real-PC smoke stays within budget (10 s + grace)", dt < 11.5, f"{dt:.2f}s")
    # set STRATA_DIR to your Strata folder to also check that the search finds it
    expected = os.path.normcase(os.path.realpath(os.environ["STRATA_DIR"])) if os.environ.get("STRATA_DIR") else ""
    if expected and os.path.isdir(expected):
        check(f"real-PC smoke finds STRATA_DIR ({expected})", expected in keys(found), f"got {sorted(keys(found))}")
    t0 = time.monotonic()
    found2 = find_strata(None, time_budget_s=10)
    dt2 = time.monotonic() - t0
    print(f"  find_strata(None): took {dt2:.2f}s, found {len(found2)} Strata folder(s)")
    check("find_strata(None) == find_strata(default_roots()) on this PC", keys(found2) == keys(found),
          f"{sorted(keys(found2))} vs {sorted(keys(found))}")


def main() -> int:
    try:
        for fn in (test_depths, test_drive_root_depth_cap, test_decoys, test_skips_and_junction, test_nested_once,
                   test_time_budget_and_stop, test_sorting_and_best, test_never_raises, test_default_roots,
                   test_multi_root_fairness, test_roots_none_in_thread, test_default_roots_skip_network,
                   test_safe_mode_hostile_paths, test_safe_mode_config_rules, test_normal_mode_unchanged,
                   test_cloud_only_skipped, test_abort_between_bats, smoke_real_pc):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
    finally:
        link = TMP / "skips" / "Link"
        try:
            if os.path.lexists(link):
                os.rmdir(link)                # drop the junction before rmtree so nothing outside TMP is touched
        except OSError:
            pass
        shutil.rmtree(TMP, ignore_errors=True)
        check("temp dir removed", not TMP.exists(), str(TMP))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
