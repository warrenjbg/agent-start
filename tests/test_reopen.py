"""Self-check for reopen.py. Run: .venv\\Scripts\\python.exe tests\\test_reopen.py

Safe by construction:
  * state.json is never touched: config_store.STATE_PATH is pointed at a temp file.
  * The only processes started or killed are dummy `python <tmp>\\dummy_sleep.py <marker>` processes run
    from a TEMP COPY of the venv's python.exe (a unique exe path, so already_running() cannot be fooled by
    this test's own interpreter). Cleanup kills only processes whose command line carries this run's
    unique marker. No windows, no GUI apps, no network.
  * Refusal / rules tests use empty fake .exe/.bat files that are never executed.
"""
from __future__ import annotations

import hashlib
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

import config_store  # noqa: E402
import reopen  # noqa: E402
from reopen import LaunchRecord  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
CREATE_NO_WINDOW = 0x08000000
MARKER = f"hc-reopen-test-{uuid.uuid4().hex[:12]}"
TMP = Path(tempfile.mkdtemp(prefix=MARKER + "-"))
VENV = ROOT / ".venv"


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def fake_file(name: str) -> str:
    p = TMP / "fake" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return str(p)


def rec(**kw) -> LaunchRecord:
    base = dict(name="App", exe=fake_file("app.exe"), cmdline=[], cwd="", category="window",
                create_time=1.0, elevated=False, aumid=None)
    base.update(kw)
    return LaunchRecord(**base)


# ----------------------------------------------------------------------------------------- dummy processes

def dummy_python() -> str:
    """Temp copy of the venv redirector (+ pyvenv.cfg one level up, like a real venv)."""
    exe = TMP / "venv" / "Scripts" / "python.exe"
    if not exe.exists():
        exe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(VENV / "pyvenv.cfg", TMP / "venv" / "pyvenv.cfg")
        shutil.copyfile(VENV / "Scripts" / "python.exe", exe)
    return str(exe)


def dummy_script() -> str:
    s = TMP / "dummy_sleep.py"
    if not s.exists():
        s.write_text("import sys, time\ntime.sleep(60)\n", encoding="utf-8")
    return str(s)


def ours() -> list[psutil.Process]:
    """Processes started by this test run (cmdline carries MARKER), excluding self."""
    out = []
    me = os.getpid()
    for p in psutil.process_iter(["cmdline"], ad_value=None):
        if p.pid == me:
            continue
        if any(MARKER in str(c) for c in (p.info.get("cmdline") or [])):
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


def spawn_dummy(tag: str) -> tuple[subprocess.Popen, psutil.Process]:
    p = subprocess.Popen([dummy_python(), dummy_script(), MARKER, tag], cwd=str(TMP),
                         creationflags=CREATE_NO_WINDOW)
    root = psutil.Process(p.pid)
    time.sleep(0.8)                                # redirector spawns the real interpreter
    return p, root


def record_of(root: psutil.Process, name: str = "dummy", category: str = "window") -> LaunchRecord:
    return LaunchRecord(name=name, exe=root.exe(), cmdline=root.cmdline(), cwd=root.cwd(), category=category,
                        create_time=root.create_time(), elevated=False, aumid=None)


# ----------------------------------------------------------------------------------------- tests

def test_record_roundtrip() -> None:
    section("1. LaunchRecord from_dict / to_dict")
    r = rec(cmdline=["a", "b"], aumid=None)
    d = r.to_dict()
    check("to_dict has all fields", set(d) == {"name", "exe", "cmdline", "cwd", "category", "create_time",
                                               "elevated", "aumid", "reason_skip"}, str(sorted(d)))
    check("round trip equal", LaunchRecord.from_dict(d) == r)
    g = LaunchRecord.from_dict({"name": 5, "cmdline": "notalist", "create_time": "x", "elevated": 1})
    check("tolerant from_dict", g.name == "5" and g.cmdline == [] and g.create_time == 0.0 and g.elevated is True
          and g.aumid is None and g.category == "background")
    check("from_dict of non-dict -> defaults", LaunchRecord.from_dict(None) == LaunchRecord())  # type: ignore[arg-type]


def test_classify() -> None:
    section("2. classify rules")
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    recs = [
        rec(name="auto1"),
        rec(name="game1", category="game"),
        rec(name="elev", elevated=True),
        rec(name="missing", exe=str(TMP / "nope" / "gone.exe")),
        rec(name="sys", exe=os.path.join(windir, "System32", "notepad.exe")),
        rec(name="pkgbg", category="background", aumid="Microsoft.Foo_8wekyb3d8bbwe!App"),
        rec(name="nothing", exe="", aumid=None),
        rec(name="pkgwin", exe="", category="window", aumid="Microsoft.Bar_8wekyb3d8bbwe!App"),
        rec(name="elevgame", category="game", elevated=True),
        rec(name="bg-unpackaged", category="background"),
    ]
    auto, manual, skipped = reopen.classify(recs)
    names = lambda lst: [r.name for r in lst]  # noqa: E731
    reasons = {r.name: r.reason_skip for r in skipped}
    check("auto = plain + packaged window + unpackaged background (order kept)",
          names(auto) == ["auto1", "pkgwin", "bg-unpackaged"], str(names(auto)))
    check("games -> manual", names(manual) == ["game1"], str(names(manual)))
    check("elevated -> 'needs administrator'", reasons.get("elev") == "needs administrator")
    check("exe missing -> 'program not found'", reasons.get("missing") == "program not found")
    check("exe under %WINDIR% -> 'system program'", reasons.get("sys") == "system program")
    check("packaged background -> 'Windows restarts it itself'", reasons.get("pkgbg") == "Windows restarts it itself")
    check("no exe and no aumid -> skipped", reasons.get("nothing") == reopen.SKIP_NOTHING)
    check("elevated game is skipped, not offered", reasons.get("elevgame") == "needs administrator")
    check("auto/manual have reason_skip None", all(r.reason_skip is None for r in auto + manual))
    a2, m2, s2 = reopen.classify([recs[0].to_dict(), {"garbage": True}])
    check("dicts accepted; garbage dict skipped", names(a2) == ["auto1"] and len(s2) == 1 and not m2)
    check("empty input", reopen.classify([]) == ([], [], []))


def test_persistence() -> None:
    section("3. save/load/clear pending on a TEMP state file")
    real = config_store.STATE_PATH
    real_hash = hashlib.sha256(Path(real).read_bytes()).hexdigest() if Path(real).exists() else None
    old = config_store.STATE_PATH
    tmp_state = TMP / "state.json"
    config_store.STATE_PATH = tmp_state
    try:
        config_store.save_state({"other_key": 42})
        check("load_pending empty when nothing saved", reopen.load_pending() == [])
        recs = [rec(name="a", cmdline=["x", "y"]), rec(name="b", category="game", aumid="P.Q_abc!App")]
        t0 = time.time()
        reopen.save_pending([recs[0], recs[1].to_dict()])
        st = config_store.load_state()
        blob = st.get("reopen_pending")
        check("stored under 'reopen_pending' as {saved_at, records}",
              isinstance(blob, dict) and isinstance(blob.get("records"), list) and len(blob["records"]) == 2
              and abs(blob.get("saved_at", 0) - t0) < 5)
        check("other state keys preserved", st.get("other_key") == 42)
        back = reopen.load_pending()
        check("round trip", back == recs, str(back))
        check("pending_saved_at", abs((reopen.pending_saved_at() or 0) - t0) < 5)
        check("max_age_s fresh -> kept", len(reopen.load_pending(max_age_s=3600)) == 2)
        blob["saved_at"] = time.time() - 7200
        config_store.save_state({"reopen_pending": blob})
        check("max_age_s stale -> []", reopen.load_pending(max_age_s=3600) == [])
        reopen.clear_pending()
        check("clear -> []", reopen.load_pending() == [] and reopen.pending_saved_at() is None)
        check("clear keeps other keys", config_store.load_state().get("other_key") == 42)
        config_store.save_state({"reopen_pending": "junk"})
        check("junk value -> []", reopen.load_pending() == [])
        config_store.save_state({"reopen_pending": [recs[0].to_dict(), 7]})
        check("bare list tolerated", reopen.load_pending() == [recs[0]])
    finally:
        config_store.STATE_PATH = old
    check("real state.json unchanged",
          (hashlib.sha256(Path(real).read_bytes()).hexdigest() if Path(real).exists() else None) == real_hash)


def test_launch_refusals() -> None:
    section("4. launch() refusals (nothing is executed)")
    other = fake_file("other.exe")
    before = {p.pid for p in psutil.process_iter()}
    ok, why = reopen.launch(rec(cmdline=[other, "--x"]))
    check("cmdline[0] != exe -> refused", not ok and "does not match" in why, why)
    ok, why = reopen.launch(rec(cmdline=["other.exe"]))
    check("relative cmdline[0] naming another file -> refused", not ok, why)
    ok, why = reopen.launch(rec(exe=fake_file("evil.bat"), cmdline=[]))
    check(".bat refused (would run cmd.exe)", not ok and "not an .exe" in why, why)
    ok, why = reopen.launch(rec(elevated=True))
    check("elevated refused", not ok and why == "needs administrator", why)
    ok, why = reopen.launch(rec(exe=str(TMP / "missing.exe")))
    check("missing exe refused", not ok and why == "program not found", why)
    ok, why = reopen.launch(rec(exe="", aumid="x & calc.exe", category="window"))
    check("malformed AUMID refused", not ok, why)
    ok, why = reopen.launch(rec(exe="", aumid="Good.App_8wekyb3d8bbwe!App", category="background"))
    check("packaged background refused", not ok and why == "Windows restarts it itself", why)
    check("resolver: same file via exe dir", reopen._resolves_to_exe("app.exe", rec()))
    check("resolver: same file without .exe", reopen._resolves_to_exe("app", rec()))
    check("resolver: absolute same path, different case", reopen._resolves_to_exe(rec().exe.upper(), rec()))
    real_aumids = ["Microsoft.ScreenSketch_8wekyb3d8bbwe!App", "Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App",
                   "MicrosoftWindows.Client.WebExperience_cw5n1h2txyewy!Global.WidgetBoard",
                   "Microsoft.CommandPalette_8wekyb3d8bbwe!App"]          # captured from this PC's processes
    check("real AUMID shapes accepted", all(reopen._AUMID_RE.match(a) for a in real_aumids))
    bad_aumids = ["a!b", "x & calc.exe", "Foo_abc!App\" /c calc", "..\\x_abc!App", "Foo_abc!App ", "Foo_abc!", "Foo_abc!App\n"]
    check("hostile AUMIDs rejected", not any(reopen._AUMID_RE.match(a) for a in bad_aumids))
    new = [p for p in psutil.process_iter(["cmdline"], ad_value=None)
           if p.pid not in before and any("fake" in str(c) and MARKER in str(c) for c in (p.info["cmdline"] or []))]
    check("no process started by refusals", not new, str(new))


def test_real_launch() -> None:
    section("5. already_running + real launch of a dummy (spawned, recorded, killed, relaunched)")
    popen, root = spawn_dummy("orig")
    try:
        r = record_of(root)
        check("record exe is the temp copy", os.path.normcase(r.exe) == os.path.normcase(dummy_python()), r.exe)
        check("already_running True while dummy runs", reopen.already_running(r))
        check("already_running(dict) works", reopen.already_running(r.to_dict()))
        n = kill_ours()
        popen.wait(timeout=5)
        check("dummy killed (only ours)", n >= 1 and not ours(), str(n))
        check("already_running False after kill", not reopen.already_running(r))
        check("already_running False for unknown exe", not reopen.already_running(rec(exe=str(TMP / "zz.exe"))))
        res = reopen.reopen_one(r)
        check("reopen_one launches it", res["ok"] and res["status"] == "started", str(res))
        deadline = time.time() + 5
        relaunched = []
        while time.time() < deadline and not relaunched:
            relaunched = [p for p in ours() if _exe(p) == os.path.normcase(r.exe)]
            time.sleep(0.1)
        check("relaunched process exists with same exe", bool(relaunched))
        if relaunched:
            p = relaunched[0]
            check("same cmdline", p.cmdline() == r.cmdline, str(p.cmdline()))
            check("same cwd", os.path.normcase(p.cwd()) == os.path.normcase(r.cwd), p.cwd())
            check("venv launcher env var not leaked to the relaunched app",
                  "__PYVENV_LAUNCHER__" not in reopen._child_env())
        check("already_running True after relaunch", reopen.already_running(r))
        rl = reopen.relaunched()
        check("relaunched(): the app we started is remembered as (pid, create_time)",
              len(rl) == 1 and all(psutil.Process(pid).create_time() == ct for pid, ct in rl)
              and {pid for pid, _ in rl} <= {p.pid for p in ours()}, str(rl))
        res2 = reopen.reopen_one(r)
        check("reopen_one on running app -> already running, no 2nd launch",
              res2["status"] == "already_running" and len([p for p in ours() if _exe(p) == os.path.normcase(r.exe)]) == 1,
              str(res2))
        kill_ours()
        time.sleep(0.3)
        check("relaunched(): gone once the app has exited", reopen.relaunched() == set(), str(reopen.relaunched()))
        # cwd gone -> falls back to the exe's folder
        r2 = LaunchRecord.from_dict({**r.to_dict(), "cwd": str(TMP / "gone-dir")})
        ok, why = reopen.launch(r2)
        time.sleep(1.0)
        procs = [p for p in ours() if _exe(p) == os.path.normcase(r.exe)]
        check("missing cwd -> exe folder", ok and procs
              and os.path.normcase(procs[0].cwd()) == os.path.normcase(os.path.dirname(r.exe)), why)
        kill_ours()
    finally:
        kill_ours()
        try:
            popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _exe(p: psutil.Process) -> str:
    try:
        return os.path.normcase(p.exe())
    except psutil.Error:
        return ""


def test_reopen_orchestration() -> None:
    section("6. reopen(): order, stagger, already-running, stop_evt, on_progress (launch faked)")
    orig_launch, orig_running = reopen.launch, reopen.already_running
    launches: list[tuple[str, float]] = []
    running = {"b"}
    reopen.launch = lambda r: (launches.append((r.name, time.monotonic())) or (True, "started"))
    reopen.already_running = lambda r: r.name in running
    try:
        recs = [rec(name="a"), rec(name="g", category="game"), rec(name="b"), rec(name="x", elevated=True),
                rec(name="c"), rec(name="d")]
        prog: list[tuple[int, int, str]] = []
        t = time.monotonic()
        res = reopen.reopen(recs, stagger_s=0.2, on_progress=lambda d, n, name: prog.append((d, n, name)))
        el = time.monotonic() - t
        check("only auto records launched, in order (game/elevated/already-running not)",
              [n for n, _ in launches] == ["a", "c", "d"], str(launches))
        gaps = [launches[i + 1][1] - launches[i][1] for i in range(len(launches) - 1)]
        check("stagger >= stagger_s between launches", all(g >= 0.18 for g in gaps), str(gaps))
        check("total time bounded", el < 2.0, f"{el:.2f}s")
        check("results per auto record", [(r["name"], r["status"]) for r in res]
              == [("a", "started"), ("b", "already_running"), ("c", "started"), ("d", "started")], str(res))
        check("already running reported ok + reason", res[1]["ok"] and res[1]["reason"] == "already running")
        check("on_progress(done, total, name)", prog == [(1, 4, "a"), (2, 4, "b"), (3, 4, "c"), (4, 4, "d")], str(prog))

        launches.clear()
        evt = threading.Event()

        def stop_after_first(d, n, name):
            if d == 1:
                evt.set()
        res = reopen.reopen(recs, stagger_s=0.1, stop_evt=evt, on_progress=stop_after_first)
        check("stop_evt between launches: only first launched", [n for n, _ in launches] == ["a"], str(launches))
        check("rest reported cancelled", [r["status"] for r in res] == ["started", "cancelled", "cancelled", "cancelled"],
              str(res))
        auto_recs = reopen.classify(recs)[0]
        check("not_started(): exactly the records the interrupted run never reached, in order",
              [r.name for r in reopen.not_started(auto_recs, res)] == ["b", "c", "d"],
              str(reopen.not_started(auto_recs, res)))
        check("not_started(): a finished run leaves nothing",
              reopen.not_started(auto_recs, [{"status": s} for s in ("started", "already_running", "failed", "started")]) == [])

        launches.clear()
        running.clear()
        evt2 = threading.Event()
        threading.Timer(0.2, evt2.set).start()
        t = time.monotonic()
        res = reopen.reopen(recs, stagger_s=10.0, stop_evt=evt2)
        el = time.monotonic() - t
        check("stop_evt interrupts the stagger wait promptly", el < 1.5 and [n for n, _ in launches] == ["a"],
              f"{el:.2f}s {launches}")
        evt3 = threading.Event()
        evt3.set()
        launches.clear()
        res = reopen.reopen(recs, stagger_s=0, stop_evt=evt3)
        check("pre-set stop_evt launches nothing", not launches and all(r["status"] == "cancelled" for r in res))
        launches.clear()
        res = reopen.reopen(recs, stagger_s=0, on_progress=lambda *a: 1 / 0)
        check("raising on_progress does not abort", len(launches) == 4)
        check("reopen([]) == []", reopen.reopen([], stagger_s=0) == [])
    finally:
        reopen.launch, reopen.already_running = orig_launch, orig_running


def main() -> int:
    try:
        for fn in (test_record_roundtrip, test_classify, test_persistence, test_launch_refusals, test_real_launch,
                   test_reopen_orchestration):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
    finally:
        left = kill_ours()
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
