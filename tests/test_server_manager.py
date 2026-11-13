"""Self-check for server_manager.EndpointManager against a FAKE Strata server.

Run:  .venv\\Scripts\\python.exe tests\\test_server_manager.py

Safety: never touches port 8080 or the real Strata install. Every config is a temp dict; every process spawned
(by the test or by the manager) has the unique temp dir in its cmdline, and the final check kills/flags only those.
The log dir is injected with EndpointManager(..., log_dir=<tempdir>\\logs). One test shows one real toast.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

import server_manager  # noqa: E402
from server_manager import EndpointManager  # noqa: E402

PY = sys.executable
FIX = HERE / "fixtures"
TMP = Path(tempfile.mkdtemp(prefix="hc-smtest-"))
SERVE = TMP / "serve" / "server.py"
LAUNCHER = TMP / "launcher.py"
FOREIGN = TMP / "foreign.py"
LOGDIR = TMP / "logs"
MARK = os.path.normcase(str(TMP))
NOWIN = subprocess.CREATE_NO_WINDOW

SERVE.parent.mkdir(parents=True)
shutil.copy(FIX / "fake_strata_server.py", SERVE)
shutil.copy(FIX / "fake_launcher.py", LAUNCHER)
shutil.copy(FIX / "foreign_server.py", FOREIGN)
KIDS_SERVE = TMP / "kids" / "serve" / "server.py"           # fake Strata that spawns children (under strata_dir)
KIDS_SERVE.parent.mkdir(parents=True)
shutil.copy(FIX / "fake_strata_with_children.py", KIDS_SERVE)
shutil.copy(FIX / "fake_strata_server.py", KIDS_SERVE.parent / "fake_impl.py")
OUTSIDE = Path(str(TMP) + "-outside")                       # NOT under strata_dir, but still carries MARK
SYS_PING = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE"

logging.basicConfig(filename=str(TMP / "test.log"), level=logging.DEBUG,
                    format="%(asctime)s %(threadName)s %(levelname)s %(message)s")

RESULT_KEYS = {"ok", "port_released", "killed_pids", "ram_freed_mb", "unload", "error", "cancelled_start", "released"}
STATUS_KEYS = {"state", "pid", "model_id", "adopted", "log_path", "loaded"}
ALL_CBS: list["CB"] = []          # (CB, expected_calls) registry for the exactly-one-callback invariant


# ---------------------------------------------------------------------------- helpers
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    assert p != 8080
    return p


def listening(port: int) -> bool:
    """Independent of the manager: plain connect (closed loopback ports answer within the 0.5 s timeout)."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_until(pred, timeout: float, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def listener_pid(port: int, timeout: float = 3.0) -> int | None:
    """PID of the socket listening on port. Skips pid 0/None rows (Windows reports phantom System-owned
    entries), prefers LISTEN over NONE, and retries briefly because the table can lag a fresh bind."""
    deadline = time.monotonic() + timeout
    while True:
        rows = [c for c in psutil.net_connections(kind="tcp")
                if c.pid and c.laddr and c.laddr.port == port and not c.raddr
                and c.status in (psutil.CONN_LISTEN, psutil.CONN_NONE)]
        rows.sort(key=lambda c: c.status != psutil.CONN_LISTEN)
        if rows:
            return rows[0].pid
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def make_cfg(port: int, server_args: list[str] = (), launcher: bool = False, ready_timeout: float = 30,
             stop_timeout: float = 10) -> dict:
    assert port != 8080
    base = [PY, str(LAUNCHER), str(SERVE)] if launcher else [PY, str(SERVE)]
    return {"strata_dir": str(TMP), "launch_cmd": base + ["--port", str(port), *server_args],
            "endpoint_url": f"http://127.0.0.1:{port}/v1", "port": port, "model_name": "cfg-model",
            "ready_timeout_s": ready_timeout, "stop_timeout_s": stop_timeout}


def mgr_for(cfg: dict, notify: bool = False, **kw) -> EndpointManager:
    assert "8080" not in cfg["endpoint_url"] and cfg["port"] != 8080
    kw.setdefault("log_dir", LOGDIR)
    kw.setdefault("poll_interval_s", 0.25)
    return EndpointManager(config=cfg, notify=notify, **kw)


def spawn_external(args: list[str], script: Path = SERVE) -> subprocess.Popen:
    return subprocess.Popen([PY, str(script), *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=NOWIN)


def kill_own(popen: subprocess.Popen) -> None:
    """Kill a process the TEST spawned plus its descendants."""
    try:
        p = psutil.Process(popen.pid)
        procs = [p] + p.children(recursive=True)
    except psutil.Error:
        return
    for q in procs:
        try:
            q.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


def tree_pids(pid: int) -> set[int]:
    p = psutil.Process(pid)
    return {pid} | {c.pid for c in p.children(recursive=True)}


def alive(pids) -> set[int]:
    out = set()
    for pid in pids:
        try:
            if psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                out.add(pid)
        except psutil.Error:
            pass
    return out


def records(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


class CB:
    def __init__(self, expected: int = 1, raise_in_cb: bool = False):
        self.calls: list[tuple[str, object, str]] = []
        self.ev = threading.Event()
        self.raise_in_cb = raise_in_cb
        self.expected = expected
        ALL_CBS.append(self)

    def _rec(self, kind, arg):
        self.calls.append((kind, arg, threading.current_thread().name))
        self.ev.set()
        if self.raise_in_cb:
            raise RuntimeError("callback boom (intentional)")

    def ready(self):
        self._rec("ready", None)

    def error(self, msg):
        self._rec("error", msg)

    def stopped(self, res):
        self._rec("stopped", res)

    def wait(self, timeout) -> tuple[str, object]:
        assert self.ev.wait(timeout), f"no callback within {timeout} s"
        k, a, th = self.calls[0]
        assert th != "MainThread", "callback ran on the main thread"
        return k, a


def check_result_shape(res: dict) -> None:
    assert set(res) == RESULT_KEYS, f"result keys {sorted(res)}"
    assert isinstance(res["ok"], bool) and isinstance(res["port_released"], bool)
    assert res["released"] is res["port_released"], "released (engine-neutral) mirrors port_released for Strata"
    assert isinstance(res["killed_pids"], list) and all(isinstance(x, int) for x in res["killed_pids"])
    assert isinstance(res["ram_freed_mb"], float)
    assert res["unload"] in ("ok", "busy", "unavailable", "skipped")
    assert res["error"] is None or isinstance(res["error"], str)
    assert isinstance(res["cancelled_start"], bool)


def check_status_shape(st: dict) -> None:
    assert set(st) == STATUS_KEYS, f"status keys {sorted(st)}"
    assert st["state"] in ("idle", "starting", "active", "stopping")


NOTES: list[str] = []


def note(msg: str) -> None:
    NOTES.append(msg)
    print("      " + msg)


# ---------------------------------------------------------------------------- tests
def t_launcher_start_stop_toast_logs():
    """start->ready via launcher layer (load delay), model_id, status, protected_pids, is_running timing,
    in-progress False returns, log rotation, stop (tree killed, port released, exact result), ONE real toast."""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    for i in range(7):
        f = LOGDIR / f"strata-2020010{i}-000000.log"
        f.write_text("old")
        os.utime(f, (1_577_836_800 + i, 1_577_836_800 + i))
    (LOGDIR / "controller.log").write_text("not a strata log")
    port = free_port()
    rec = TMP / f"rec-{port}.txt"
    cfg = make_cfg(port, ["--load-delay", "1.5", "--model", "fake-A", "--record", str(rec),
                          "--unload-delay", "0.8"], launcher=True)
    m = mgr_for(cfg, notify=True)
    st = m.status()
    check_status_shape(st)
    assert st["state"] == "idle" and st["pid"] is None, st
    cb = CB()
    t0 = time.monotonic()
    assert m.start_endpoint(cb.ready, cb.error) is True
    dt_call = (time.monotonic() - t0) * 1000
    cb_dup = CB(expected=0)
    assert m.start_endpoint(cb_dup.ready, cb_dup.error) is False, "second start while starting must be False"
    st = m.status()
    assert st["state"] == "starting", st
    kind, arg = cb.wait(30)
    t_ready = time.monotonic() - t0
    assert kind == "ready", (kind, arg)
    assert t_ready >= 1.5, f"ready too early: {t_ready:.2f}s"
    note(f"start_endpoint() returned in {dt_call:.1f} ms; ready after {t_ready:.2f} s (load delay 1.5 s)")
    st = m.status()
    check_status_shape(st)
    assert st["state"] == "active" and st["model_id"] == "fake-A" and st["adopted"] is False \
        and st["loaded"] is True, st
    assert st["log_path"] and Path(st["log_path"]).is_file(), st
    assert os.path.normcase(str(Path(st["log_path"]).parent)) == os.path.normcase(str(LOGDIR))
    logs = sorted(p.name for p in LOGDIR.glob("strata-*.log"))
    assert len(logs) == 5 and Path(st["log_path"]).name in logs, logs
    assert (LOGDIR / "controller.log").exists(), "non-strata log must not be pruned"
    assert "fake strata listening" in Path(st["log_path"]).read_text(encoding="utf-8", errors="replace")
    note(f"log rotation: 7 old + 1 new -> kept {len(logs)}: {logs}")

    # topology + protected_pids
    launcher_pid = m._popen.pid
    lpid = listener_pid(port)
    tree = tree_pids(launcher_pid)
    assert lpid in tree and lpid != launcher_pid, (lpid, launcher_pid, tree)
    t1 = time.perf_counter()
    prot = m.protected_pids()
    note(f"protected_pids() took {(time.perf_counter() - t1) * 1000:.1f} ms -> {sorted(prot)}")
    assert tree <= prot, f"missing {tree - prot}"
    note(f"tree: launcher {launcher_pid} -> ... listener {lpid}; {len(tree)} procs, all protected")

    # is_running timing
    times = []
    for _ in range(30):
        t1 = time.perf_counter()
        assert m.is_running() is True
        times.append((time.perf_counter() - t1) * 1000)
    closed = mgr_for(make_cfg(free_port()))
    ctimes = []
    for _ in range(10):
        t1 = time.perf_counter()
        assert closed.is_running() is False
        ctimes.append((time.perf_counter() - t1) * 1000)
    note(f"is_running listening: median {statistics.median(times):.2f} ms max {max(times):.2f} ms; "
         f"closed port: median {statistics.median(ctimes):.2f} ms")
    assert statistics.median(times) < 50 and statistics.median(ctimes) < 50

    # stop
    cbs = CB()
    t0 = time.monotonic()
    assert m.stop_endpoint(cbs.stopped) is True
    cb_dup2, cb_dup3 = CB(expected=0), CB(expected=0)
    assert m.stop_endpoint(cb_dup2.stopped) is False, "stop while stopping must be False"
    assert m.start_endpoint(cb_dup3.ready, cb_dup3.error) is False, "start while stopping must be False"
    assert m.status()["state"] == "stopping"
    kind, res = cbs.wait(40)
    note(f"stop finished in {time.monotonic() - t0:.2f} s: {res}")
    check_result_shape(res)
    assert res["ok"] is True and res["port_released"] is True and res["unload"] == "ok", res
    assert res["error"] is None and res["cancelled_start"] is False, res
    assert tree <= set(res["killed_pids"]), f"not reported killed: {tree - set(res['killed_pids'])}"
    assert res["ram_freed_mb"] > 0
    assert not alive(tree), f"survivors {alive(tree)}"
    assert not listening(port)
    assert "POST /unload?wait=10" in records(rec), records(rec)
    assert m.last_toast and "fake-A" in m.last_toast, m.last_toast
    note(f"toast shown: {m.last_toast!r}")
    st = m.status()
    assert st["state"] == "idle" and st["pid"] is None, st
    assert m.stop_endpoint(CB(expected=0).stopped) is False, "stop when idle + port free must be False"


def t_early_exit():
    port = free_port()
    m = mgr_for(make_cfg(port, ["--exit-early"]))
    cb = CB()
    t0 = time.monotonic()
    assert m.start_endpoint(cb.ready, cb.error)
    kind, msg = cb.wait(20)
    assert kind == "error" and "Strata exited (code 3)" in msg and "strata-" in msg, (kind, msg)
    note(f"on_error after {time.monotonic() - t0:.2f} s: {msg!r}")
    assert m.status()["state"] == "idle"


def t_validation_errors():
    port = free_port()
    cfg = make_cfg(port)
    cfg["launch_cmd"] = []
    cb = CB()
    assert mgr_for(cfg).start_endpoint(cb.ready, cb.error)
    k, msg = cb.wait(5)
    assert k == "error" and "No Strata launcher chosen" in msg and "Settings" in msg, msg
    cfg2 = make_cfg(port)
    cfg2["launch_cmd"] = [str(TMP / "nope" / "python.exe"), str(SERVE)]
    cb2 = CB()
    assert mgr_for(cfg2).start_endpoint(cb2.ready, cb2.error)
    k, msg2 = cb2.wait(5)
    assert k == "error" and "not found" in msg2, msg2
    note(f"empty launch_cmd -> {msg!r}; missing exe -> {msg2!r}")


def t_ready_timeout_kills_spawned():
    port = free_port()
    rec = TMP / f"rec-{port}.txt"
    m = mgr_for(make_cfg(port, ["--never-ready", "--record", str(rec)], launcher=True, ready_timeout=3),
                load_nudge_s=0.5)
    cb = CB()
    t0 = time.monotonic()
    assert m.start_endpoint(cb.ready, cb.error)
    assert wait_until(lambda: listening(port), 10), "fake never listened"
    tree = tree_pids(m._popen.pid)
    kind, msg = cb.wait(30)
    el = time.monotonic() - t0
    assert kind == "error" and "Strata not ready after 3 s" in msg, (kind, msg)
    assert not alive(tree), f"spawned survivors {alive(tree)}"
    assert wait_until(lambda: not listening(port), 5)
    loads = [r for r in records(rec) if r.startswith("POST /load")]
    assert len(loads) == 1, records(rec)
    note(f"on_error after {el:.2f} s: {msg!r}; killed tree {sorted(tree)}; POST /load sent {len(loads)}x")


def t_lazy_load_and_callback_exception():
    port = free_port()
    rec = TMP / f"rec-{port}.txt"
    m = mgr_for(make_cfg(port, ["--lazy", "--load-delay", "0.5", "--model", "lazy-M", "--record", str(rec)]),
                load_nudge_s=1.0)
    cb = CB(raise_in_cb=True)
    t0 = time.monotonic()
    assert m.start_endpoint(cb.ready, cb.error)
    kind, _ = cb.wait(30)
    assert kind == "ready"
    loads = [r for r in records(rec) if r.startswith("POST /load")]
    assert len(loads) == 1, records(rec)
    note(f"lazy server ready after {time.monotonic() - t0:.2f} s with exactly one POST /load; "
         f"on_ready raised intentionally")
    time.sleep(0.3)
    st = m.status()
    assert st["state"] == "active" and st["model_id"] == "lazy-M", st
    tree = tree_pids(m._popen.pid)
    cbs = CB()
    assert m.stop_endpoint(cbs.stopped)
    _, res = cbs.wait(30)
    check_result_shape(res)
    assert res["ok"] and res["port_released"] and not alive(tree), res


def t_adopt():
    # adopt a ready external server, then stop it through the manager
    port = free_port()
    rec = TMP / f"rec-{port}.txt"
    ext = spawn_external(["--port", str(port), "--model", "ext-M", "--record", str(rec)])
    try:
        assert wait_until(lambda: listening(port), 10)
        m = mgr_for(make_cfg(port))
        cb = CB()
        t0 = time.monotonic()
        assert m.start_endpoint(cb.ready, cb.error)
        kind, _ = cb.wait(15)
        assert kind == "ready"
        assert m._popen is None, "must never launch when the port is listening"
        st = m.status()
        assert st["state"] == "active" and st["adopted"] is True and st["model_id"] == "ext-M", st
        assert st["pid"] == ext.pid, (st["pid"], ext.pid)
        note(f"adopted in {time.monotonic() - t0:.2f} s; status={st}")
        assert not any(r.startswith("POST") for r in records(rec)), records(rec)
        tree = tree_pids(ext.pid)
        cbs = CB()
        assert m.stop_endpoint(cbs.stopped)
        _, res = cbs.wait(30)
        check_result_shape(res)
        assert res["ok"] and res["unload"] == "ok" and tree <= set(res["killed_pids"]), res
        assert not alive(tree)
    finally:
        kill_own(ext)
    # adopted never-ready server: timeout must NOT kill it
    port = free_port()
    rec = TMP / f"rec-{port}.txt"
    ext = spawn_external(["--port", str(port), "--never-ready", "--record", str(rec)])
    try:
        assert wait_until(lambda: listening(port), 10)
        tree = tree_pids(ext.pid)
        m = mgr_for(make_cfg(port, ready_timeout=2), load_nudge_s=0.5)
        cb = CB()
        assert m.start_endpoint(cb.ready, cb.error)
        kind, msg = cb.wait(15)
        assert kind == "error" and "not ready" in msg, (kind, msg)
        assert alive(tree) == tree, "adopted server must survive the timeout"
        assert listening(port)
        st = m.status()
        assert st["state"] == "active" and st["loaded"] is False and st["adopted"] is True, st
        note(f"adopted never-ready -> {msg!r}; server alive; status active+loaded False")
    finally:
        kill_own(ext)


def t_busy_then_force():
    port = free_port()
    m = mgr_for(make_cfg(port, ["--busy-unload", "--busy-wait-cap", "1"], launcher=True))
    cb = CB()
    assert m.start_endpoint(cb.ready, cb.error)
    assert cb.wait(30)[0] == "ready"
    tree = tree_pids(m._popen.pid)
    cbs = CB()
    t0 = time.monotonic()
    assert m.stop_endpoint(cbs.stopped, force=False)
    _, res = cbs.wait(30)
    check_result_shape(res)
    assert res == {"ok": False, "released": False, "port_released": False, "killed_pids": [], "ram_freed_mb": 0.0,
                   "unload": "busy", "error": "busy", "cancelled_start": False}, res
    assert alive(tree) == tree and listening(port)
    note(f"busy (no force) in {time.monotonic() - t0:.2f} s -> {res}")
    assert m.status()["state"] == "active"
    cbf = CB()
    assert m.stop_endpoint(cbf.stopped, force=True)
    _, res = cbf.wait(30)
    check_result_shape(res)
    assert res["ok"] and res["unload"] == "busy" and res["port_released"] and tree <= set(res["killed_pids"])
    assert not alive(tree)
    note(f"force -> killed {len(res['killed_pids'])} pids, port released")


def t_cancel_during_start():
    port = free_port()
    rec = TMP / f"rec-{port}.txt"
    m = mgr_for(make_cfg(port, ["--load-delay", "30", "--record", str(rec)], launcher=True))
    cb = CB(expected=0)
    assert m.start_endpoint(cb.ready, cb.error)
    assert wait_until(lambda: listening(port), 10)
    tree = tree_pids(m._popen.pid)
    cbs = CB()
    t0 = time.monotonic()
    assert m.stop_endpoint(cbs.stopped)
    _, res = cbs.wait(30)
    check_result_shape(res)
    assert res["cancelled_start"] is True and res["unload"] == "skipped" and res["ok"] and res["port_released"]
    assert tree <= set(res["killed_pids"]) and not alive(tree), res
    assert not any("unload" in r for r in records(rec))
    time.sleep(1.5)
    assert not cb.calls, f"start callbacks fired after cancel: {cb.calls}"
    note(f"cancel (listening) in {time.monotonic() - t0:.2f} s -> {res}")
    assert m.status()["state"] == "idle"
    # immediate cancel (before/around Popen)
    port2 = free_port()
    m2 = mgr_for(make_cfg(port2, ["--load-delay", "30"], launcher=True))
    cb2 = CB(expected=0)
    assert m2.start_endpoint(cb2.ready, cb2.error)
    cbs2 = CB()
    assert m2.stop_endpoint(cbs2.stopped)
    _, res2 = cbs2.wait(30)
    check_result_shape(res2)
    assert res2["cancelled_start"] and res2["ok"], res2
    time.sleep(1.5)
    assert not cb2.calls and not listening(port2)
    note(f"immediate cancel -> {res2}")


def t_foreign_listener():
    port = free_port()
    fp = spawn_external([str(port)], script=FOREIGN)
    try:
        assert wait_until(lambda: listening(port), 10)
        fpids = tree_pids(fp.pid)
        lpid = listener_pid(port)
        m = mgr_for(make_cfg(port))
        cb = CB()
        assert m.start_endpoint(cb.ready, cb.error)
        kind, msg = cb.wait(15)
        assert kind == "error" and "not Strata" in msg and f"pid {lpid}" in msg, (kind, msg)
        assert m._popen is None
        cbs = CB()
        assert m.stop_endpoint(cbs.stopped) is True
        _, res = cbs.wait(15)
        check_result_shape(res)
        assert res["ok"] is False and res["killed_pids"] == [] and "not Strata" in (res["error"] or ""), res
        assert alive(fpids) == fpids and listening(port)
        assert not (m.protected_pids() & fpids)
        st = m.status()
        assert st["state"] == "idle", st
        note(f"start -> {msg!r}; stop -> {res}")
    finally:
        kill_own(fp)


def t_cmd_bat_launcher():
    """Externally double-clicked run-*.bat topology: cmd.exe /c run-fake.bat -> launcher -> listener."""
    port = free_port()
    bat = TMP / "run-fake.bat"
    bat.write_text(f'@echo off\r\n"{PY}" "{LAUNCHER}" "{SERVE}" --port {port}\r\n', encoding="ascii")
    cmd = subprocess.Popen(["cmd.exe", "/c", str(bat)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=NOWIN)
    try:
        assert wait_until(lambda: listening(port), 15)
        tree = tree_pids(cmd.pid)
        m = mgr_for(make_cfg(port))
        assert wait_until(lambda: m.status()["state"] == "active", 10)
        prot = m.protected_pids()
        assert cmd.pid in prot, (cmd.pid, prot)
        cbs = CB()
        assert m.stop_endpoint(cbs.stopped)
        _, res = cbs.wait(30)
        check_result_shape(res)
        assert res["ok"] and cmd.pid in res["killed_pids"], res
        assert not alive(tree), alive(tree)
        note(f"cmd.exe {cmd.pid} protected and killed; killed={res['killed_pids']}")
    finally:
        kill_own(cmd)


def t_parsed_bat_launch():
    """v1.1: the config derived from a run-*.bat by launcher_bat is what gets launched - argv without --open,
    in the .bat's `cd /d` directory - and a double-clicked launcher .bat OUTSIDE strata_dir is still recognised."""
    import launcher_bat
    port = free_port()
    work = TMP / "work dir"
    work.mkdir(exist_ok=True)
    ext = Path(tempfile.mkdtemp(prefix="hc-smtest-ext-"))        # a launcher kept outside the Strata folder
    try:
        bat = ext / "run-parsed.bat"
        bat.write_text(f'@echo off\r\ntitle fake\r\ncd /d "{work}"\r\n"{PY}" "{SERVE}" "--port" "{port}" '
                       f'"--model" "bat-model" "--open"\r\nif errorlevel 1 pause\r\n', encoding="utf-8", newline="")
        info = launcher_bat.parse_bat(str(bat))
        assert info.port == port and "--open" not in info.argv and info.cwd == str(work), info
        assert os.path.normcase(info.strata_dir) == os.path.normcase(str(TMP)), info.strata_dir
        cfg = make_cfg(free_port())                               # start from another port: the .bat must win
        launcher_bat.apply_to_config(cfg, info)
        assert cfg["port"] == port and cfg["endpoint_url"] == f"http://127.0.0.1:{port}/v1"
        m = mgr_for(cfg)
        cb = CB()
        assert m.start_endpoint(cb.ready, cb.error)
        kind, arg = cb.wait(30)
        assert kind == "ready", (kind, arg)
        lp = psutil.Process(listener_pid(port))
        cmdline = lp.cmdline()
        assert "--open" not in cmdline and "bat-model" in cmdline, cmdline
        assert os.path.normcase(lp.cwd()) == os.path.normcase(str(work)), lp.cwd()
        assert m.status()["model_id"] == "bat-model"
        note(f"launched from parsed .bat: cwd={lp.cwd()!r}, argv tail={cmdline[2:]}")
        cbs = CB()
        assert m.stop_endpoint(cbs.stopped)
        _, res = cbs.wait(30)
        assert res["ok"] and res["port_released"], res

        # the same .bat double-clicked by the user (cmd.exe /c) - it is not under strata_dir, but it IS the
        # configured launcher, so its console is protected and closed together with the server
        cmd = subprocess.Popen(["cmd.exe", "/c", str(bat)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=NOWIN)
        try:
            assert wait_until(lambda: listening(port), 15)
            tree = tree_pids(cmd.pid)
            assert wait_until(lambda: m.status()["state"] == "active", 10)
            assert cmd.pid in m.protected_pids()
            other = mgr_for(dict(cfg, launcher_bat=str(ext / "run-other.bat")))
            assert cmd.pid not in other.protected_pids(), "a foreign cmd.exe must not be treated as our launcher"
            cbs = CB()
            assert m.stop_endpoint(cbs.stopped)
            _, res = cbs.wait(30)
            assert res["ok"] and cmd.pid in res["killed_pids"], res
            assert not alive(tree), alive(tree)
            note(f"external launcher outside strata_dir: cmd.exe {cmd.pid} protected and closed")
        finally:
            kill_own(cmd)
    finally:
        shutil.rmtree(ext, ignore_errors=True)


def _start_children_fixture(tag: str) -> tuple[Path, Path, dict]:
    """children.json for fake_strata_with_children: a NON-Strata ping.exe (the 'browser'), a non-Strata python
    sleeper, a whitelisted exe under strata_dir, and an engine exe under strata_dir (the only one OFF may kill)."""
    eng = TMP / "engine"
    eng.mkdir(exist_ok=True)
    strata_exe, keep_exe = eng / "strata.exe", eng / "keepme.exe"
    for dst in (strata_exe, keep_exe):
        if not dst.exists():
            shutil.copy(SYS_PING, dst)
    kids = [[str(SYS_PING), "-n", "90", "127.0.0.1"],
            [PY, "-c", "import time; time.sleep(90)", str(TMP / "sleeper-marker")],
            [str(keep_exe), "-n", "90", "127.0.0.1"],
            [str(strata_exe), "-n", "90", "127.0.0.1"]]
    cj, pj = TMP / f"children-{tag}.json", TMP / f"pids-{tag}.json"
    cj.write_text(json.dumps(kids), encoding="utf-8")
    roles = {"ping": 0, "sleeper": 1, "keepme": 2, "engine": 3}
    return cj, pj, roles


def _child_procs(pj: Path, roles: dict) -> dict[str, list[psutil.Process]]:
    """role -> [child Process + its descendants], identity-captured (psutil checks create time on kill)."""
    assert wait_until(pj.exists, 15), "children never started"
    pairs = json.loads(pj.read_text(encoding="utf-8"))
    out = {}
    for role, idx in roles.items():
        p = psutil.Process(pairs[idx][1])
        out[role] = [p] + p.children(recursive=True)
    return out


def _kill_captured(groups: dict[str, list[psutil.Process]]) -> None:
    procs = [p for g in groups.values() for p in g]
    for q in procs:
        try:
            q.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


def _alive_procs(procs: list[psutil.Process]) -> list[psutil.Process]:
    return [p for p in procs if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]


def t_stop_spares_non_strata_children():
    """OFF kills only Strata: serve\\server.py pythons + exes under strata_dir. A non-Strata child (the browser
    `--open` starts), a python sleeper, and a user_whitelist'ed exe (even under strata_dir) stay alive."""
    # (1) adopted external Strata (the double-clicked run-*.bat --open case) -> stop_endpoint
    port = free_port()
    cj, pj, roles = _start_children_fixture("adopt")
    ext = spawn_external(["--port", str(port), "--model", "kids-M", "--children", str(cj), "--pids", str(pj)],
                         script=KIDS_SERVE)
    groups: dict = {}
    try:
        assert wait_until(lambda: listening(port), 15)
        groups = _child_procs(pj, roles)
        assert all(_alive_procs(g) for g in groups.values()), groups
        strata_tree = tree_pids(ext.pid) - {p.pid for g in groups.values() for p in g}
        cfg = dict(make_cfg(port), user_whitelist=["KeepMe.EXE"])
        m = mgr_for(cfg)
        cb = CB()
        assert m.start_endpoint(cb.ready, cb.error)
        assert cb.wait(15)[0] == "ready"
        cbs = CB()
        assert m.stop_endpoint(cbs.stopped)
        _, res = cbs.wait(30)
        check_result_shape(res)
        assert res["ok"] and res["port_released"] and res["unload"] == "ok", res
        killed = set(res["killed_pids"])
        for role in ("ping", "sleeper", "keepme"):
            g = groups[role]
            assert _alive_procs(g) == g, f"{role} was killed: {[(p.pid) for p in g]} res={res}"
            assert not ({p.pid for p in g} & killed), (role, res)
        assert not _alive_procs(groups["engine"]), "engine\\strata.exe (under strata_dir) must be killed"
        assert groups["engine"][0].pid in killed, res
        assert strata_tree <= killed and not alive(strata_tree), (strata_tree, res)
        assert not listening(port)
        note(f"adopted stop: killed {sorted(killed)}; ping/sleeper/whitelisted keepme.exe survived; "
             f"engine exe killed")
    finally:
        kill_own(ext)
        _kill_captured(groups)

    # (2) our own spawn hitting the ready timeout: the start-path kill is filtered the same way
    port = free_port()
    cj, pj, roles = _start_children_fixture("spawn")
    cfg = make_cfg(port, ["--never-ready", "--children", str(cj), "--pids", str(pj)], ready_timeout=4)
    cfg["launch_cmd"][1] = str(KIDS_SERVE)
    cfg["user_whitelist"] = ["keepme"]
    m = mgr_for(cfg, load_nudge_s=60)
    groups = {}
    try:
        cb = CB()
        assert m.start_endpoint(cb.ready, cb.error)
        assert wait_until(lambda: listening(port), 15)
        groups = _child_procs(pj, roles)
        spawn_tree = tree_pids(m._popen.pid) - {p.pid for g in groups.values() for p in g}
        kind, msg = cb.wait(30)
        assert kind == "error" and "not ready" in msg, (kind, msg)
        assert wait_until(lambda: not alive(spawn_tree), 10), alive(spawn_tree)
        assert wait_until(lambda: not _alive_procs(groups["engine"]), 5)
        for role in ("ping", "sleeper", "keepme"):
            assert _alive_procs(groups[role]) == groups[role], f"{role} killed by the ready-timeout cleanup"
        note("ready-timeout cleanup of our spawn: Strata + engine exe killed, non-Strata children spared")
    finally:
        if m._popen is not None:
            kill_own(m._popen)
        _kill_captured(groups)


def t_foreign_serve_py_refused():
    """Another project's serve\\server.py (outside strata_dir, /health service != strata) on the port: not ACTIVE,
    start refuses to adopt, stop refuses - no POST /unload, nothing killed. A genuine Strata outside strata_dir
    (/health service == strata) is still adopted and stopped."""
    other = OUTSIDE / "proj" / "serve" / "server.py"
    other.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / "foreign_serve_server.py", other)
    port = free_port()
    rec = TMP / f"rec-foreign-{port}.txt"
    fp = spawn_external(["--port", str(port), "--record", str(rec)], script=other)
    try:
        assert wait_until(lambda: listening(port), 10)
        fpids = tree_pids(fp.pid)
        lp = psutil.Process(listener_pid(port))
        assert server_manager._is_strata_cmd(lp.name(), lp.cmdline()), "fixture must match the cmdline marker"
        m = mgr_for(make_cfg(port))
        st = m.status()
        assert st["state"] == "idle", f"foreign serve/server.py shown as {st}"
        cb = CB()
        assert m.start_endpoint(cb.ready, cb.error)
        kind, msg = cb.wait(15)
        assert kind == "error" and "not Strata" in msg, (kind, msg)
        assert m._popen is None
        cbs = CB()
        assert m.stop_endpoint(cbs.stopped) is True
        _, res = cbs.wait(15)
        check_result_shape(res)
        assert res["ok"] is False and res["killed_pids"] == [] and res["unload"] == "skipped" \
            and "not Strata" in (res["error"] or ""), res
        assert alive(fpids) == fpids and listening(port)
        assert not any(r.startswith("POST") for r in records(rec)), records(rec)
        note(f"foreign serve\\server.py: start -> {msg!r}; stop -> {res['error']!r}; no POST, nothing killed")
    finally:
        kill_own(fp)
    # genuine Strata whose server.py is NOT under strata_dir: identified via /health, still adopted + stopped
    real = OUTSIDE / "strata2" / "serve" / "server.py"
    real.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / "fake_strata_server.py", real)
    port = free_port()
    ext = spawn_external(["--port", str(port), "--model", "outside-M"], script=real)
    try:
        assert wait_until(lambda: listening(port), 10)
        tree = tree_pids(ext.pid)
        m = mgr_for(make_cfg(port))
        cb = CB()
        assert m.start_endpoint(cb.ready, cb.error)
        assert cb.wait(15)[0] == "ready"
        assert m.status()["state"] == "active"
        cbs = CB()
        assert m.stop_endpoint(cbs.stopped)
        _, res = cbs.wait(30)
        assert res["ok"] and res["unload"] == "ok" and tree <= set(res["killed_pids"]), res
        note("genuine Strata outside strata_dir (health service=strata): adopted and stopped")
    finally:
        kill_own(ext)
        shutil.rmtree(OUTSIDE, ignore_errors=True)


def t_launch_cmd_must_be_exe():
    """A hand-edited launch_cmd whose argv[0] is a .bat/.cmd/script is refused before anything is spawned."""
    msgs = []
    for argv0 in (TMP / "run-evil.bat", TMP / "run-evil.CMD", LAUNCHER):   # LAUNCHER: an EXISTING non-.exe file
        port = free_port()
        cfg = make_cfg(port)
        cfg["launch_cmd"] = [str(argv0), str(SERVE), "--port", str(port)]
        m = mgr_for(cfg)
        cb = CB()
        assert m.start_endpoint(cb.ready, cb.error)
        kind, msg = cb.wait(5)
        assert kind == "error" and msg.startswith("Launch command must start with python.exe"), (kind, msg)
        assert m._popen is None and not listening(port)
        msgs.append(msg)
    note(f"non-.exe launch_cmd refused: {msgs[0]!r}")


def t_callback_invariant():
    time.sleep(0.5)
    bad = [(c.expected, c.calls) for c in ALL_CBS if len(c.calls) != c.expected]
    assert not bad, bad
    note(f"{len(ALL_CBS)} callback sets checked: each True-returning call fired exactly once, "
         f"False-returning calls fired none")


TESTS = [t_launcher_start_stop_toast_logs, t_early_exit, t_validation_errors, t_ready_timeout_kills_spawned,
         t_lazy_load_and_callback_exception, t_adopt, t_busy_then_force, t_cancel_during_start,
         t_foreign_listener, t_cmd_bat_launcher, t_parsed_bat_launch, t_stop_spares_non_strata_children,
         t_foreign_serve_py_refused, t_launch_cmd_must_be_exe, t_callback_invariant]


def leaked_processes() -> list[psutil.Process]:
    me = os.getpid()
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.pid != me and MARK in os.path.normcase(" ".join(p.info["cmdline"] or [])):
                out.append(p)
        except psutil.Error:
            continue
    return out


def main() -> int:
    print(f"server_manager tests  (python {sys.version.split()[0]}, temp {TMP})")
    results = []
    for t in TESTS:
        t0 = time.monotonic()
        print(f"[RUN ] {t.__name__}")
        try:
            t()
            ok, err = True, ""
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
            traceback.print_exc()
            # a failed assert skips the test's own stop: end what it left running now, so it cannot disturb
            # the tests after it or show up as a second failure in the leak check at the end
            left = leaked_processes()
            for p in left:
                try:
                    p.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(left, timeout=5)
            if left:
                err += f" (ended {len(left)} process(es) the failed test left running)"
        dt = time.monotonic() - t0
        results.append((t.__name__, ok, dt, err))
        print(f"[{'PASS' if ok else 'FAIL'}] {t.__name__}  ({dt:.2f} s) {err}")
    leaks = leaked_processes()
    if leaks:
        for p in leaks:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(leaks, timeout=5)
    results.append(("no_leaked_processes", not leaks, 0.0, f"leaked: {[p.pid for p in leaks]}" if leaks else ""))
    print(f"[{'PASS' if not leaks else 'FAIL'}] no_leaked_processes (psutil scan for temp-dir cmdlines)"
          f"{' ' + str([p.pid for p in leaks]) if leaks else ''}")
    failed = [r for r in results if not r[1]]
    print("\n==== SUMMARY ====")
    for name, ok, dt, err in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:42s} {dt:6.2f} s  {err}")
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    if not failed:
        logging.shutdown()                       # release test.log before deleting the temp dir
        shutil.rmtree(TMP, ignore_errors=True)
        print(f"temp dir removed: {not TMP.exists()}")
    else:
        print(f"artifacts kept in {TMP} (test.log has manager logging)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
