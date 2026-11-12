"""Characterisation tests for the stop path: WHAT `EndpointManager` may kill, and when it refuses.

Pure: every process is a fake (no real process is started, signalled or inspected, no port is contacted), so the
rules can be pinned exactly. The cases were written and run against v1.8.1 BEFORE the engine refactor; after it
only the names of the manager's internals were updated (this file does not run on v1.8.1 any more). The rule
they pin is unchanged: the stop path may only kill processes it has positively identified as the engine.

Run: .venv\\Scripts\\python.exe tests\\test_kill_path.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AGENT_START_HOME", tempfile.mkdtemp(prefix="agentstart-killpath-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil  # noqa: E402

import server_manager as sm  # noqa: E402
from engines.base import HttpReply, ServiceState  # noqa: E402
from engines.strata import StrataEngine  # noqa: E402
from server_manager import EndpointManager  # noqa: E402

PASSED = 0
FAILED = 0


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {msg}")
    else:
        FAILED += 1
        print(f"[FAIL] {msg}")


S = r"C:\AI\Strata"                      # the configured strata_dir (nothing here exists on disk)
PY = S + r"\.venv\Scripts\python.exe"
SERVE = S + r"\serve\server.py"
PORT = 18999                             # never contacted: every network call is replaced below


class FakeProc:
    """Duck-typed psutil.Process over a table of fakes."""
    table: dict[int, "FakeProc"] = {}

    def __init__(self, pid: int, name: str, cmdline=(), exe: str = "", ppid: int | None = None,
                 ctime: float | None = None, cwd: str = "", rss_mb: float = 10.0) -> None:
        self.pid = pid
        self._name, self._cmd, self._exe, self._ppid, self._cwd = name, list(cmdline), exe, ppid, cwd
        self._ctime = float(pid) if ctime is None else ctime
        self._rss = rss_mb
        self.terminated = False
        FakeProc.table[pid] = self

    deny: tuple = ()                       # method names that raise (a process this user may not inspect)
    gone: tuple = ()                       # method names that raise NoSuchProcess (it exited meanwhile)

    def _guard(self, what: str) -> None:
        if what in self.deny:
            raise psutil.AccessDenied(self.pid)
        if what in self.gone:
            raise psutil.NoSuchProcess(self.pid)

    def name(self):
        self._guard("name")
        return self._name

    def cmdline(self):
        self._guard("cmdline")
        return list(self._cmd)

    def exe(self):
        self._guard("exe")
        return self._exe

    def ppid(self):
        self._guard("ppid")
        return self._ppid
    def cwd(self): return self._cwd
    def create_time(self): return self._ctime
    def is_running(self): return not self.terminated
    def parent(self): return FakeProc.table.get(self._ppid) if self._ppid is not None else None

    def memory_info(self):
        class _MI:
            rss = int(self._rss * 1024 * 1024)
        return _MI()

    def children(self, recursive: bool = False):
        kids = [p for p in FakeProc.table.values() if p._ppid == self.pid]
        if not recursive:
            return kids
        out = []
        for k in kids:
            out.append(k)
            out.extend(k.children(recursive=True))
        return out

    def __eq__(self, other):
        return isinstance(other, FakeProc) and (self.pid, self._ctime) == (other.pid, other._ctime)

    def __hash__(self):
        return hash((self.pid, self._ctime))

    def __repr__(self):
        return f"<{self._name}:{self.pid}>"


def reset() -> None:
    FakeProc.table = {}


def mgr(**cfg) -> EndpointManager:
    base = {"strata_dir": S, "port": PORT, "endpoint_url": f"http://127.0.0.1:{PORT}/v1", "user_whitelist": []}
    base.update(cfg)
    m = EndpointManager(base, notify=False)
    # belt and braces: nothing in this file may reach the network or a real process
    m._http = lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP used in a pure test"))  # type: ignore
    return m


def health_http(body):
    """An http() that answers GET /health with `body` (None = no answer) and fails the test on anything else."""
    def _http(method: str, path: str, timeout: float) -> HttpReply:
        if method == "GET" and path == "/health":
            return HttpReply(200, body) if body is not None else HttpReply()
        raise AssertionError(f"unexpected HTTP {method} {path} in a pure test")
    return _http


def state(body) -> ServiceState:
    """What the Strata engine reads from a /health body (the real probe code, no network)."""
    return StrataEngine().probe(health_http(body), 1.0)


def is_server(m: EndpointManager, proc) -> bool:
    return m._is_engine_proc(proc)


def server(pid: int, ppid: int | None = None, script: str = SERVE, port: int | None = PORT, **kw) -> FakeProc:
    cmd = [PY, script, "--engine", "strata"] + (["--port", str(port)] if port is not None else [])
    return FakeProc(pid, "python.exe", cmd, PY, ppid, **kw)


def names(procs) -> list[str]:
    return sorted(f"{p._name}:{p.pid}" for p in procs)


# --------------------------------------------------------------------------------------------- kill targets

def t_kill_targets() -> None:
    reset()
    root = server(100)
    server(101, 100)                                                        # base-interpreter child: Strata
    FakeProc(102, "strata.exe", [S + r"\engine\strata.exe"], S + r"\engine\strata.exe", 101)
    FakeProc(103, "conhost.exe", ["conhost"], r"C:\Windows\System32\conhost.exe", 102)
    FakeProc(104, "chrome.exe", ["chrome", "http://127.0.0.1"], r"C:\Program Files\Chrome\chrome.exe", 100)
    FakeProc(105, "chrome.exe", ["chrome", "--type=renderer"], r"C:\Program Files\Chrome\chrome.exe", 104)
    FakeProc(106, "conhost.exe", ["conhost"], r"C:\Windows\System32\conhost.exe", 104)
    FakeProc(107, "python.exe", ["python", "-c", "import time"], r"C:\Python\python.exe", 100)
    FakeProc(108, "keepme.exe", [S + r"\tools\keepme.exe"], S + r"\tools\keepme.exe", 100)
    FakeProc(109, "sibling.exe", [r"C:\AI\StrataX\sibling.exe"], r"C:\AI\StrataX\sibling.exe", 100)
    FakeProc(110, "helper.exe", [S + r"\engine\helper.exe"], S + r"\engine\helper.exe", 104)
    FakeProc(111, "noexe.exe", ["noexe"], "", 100)
    # outside the tree entirely: must never appear, whatever it looks like
    server(200)
    FakeProc(201, "strata.exe", [S + r"\engine\strata.exe"], S + r"\engine\strata.exe", 200)

    m = mgr(user_whitelist=["KeepMe.EXE"])
    got = names(m._kill_targets(root, log_skips=False))
    check(got == sorted(["python.exe:100", "python.exe:101", "strata.exe:102", "conhost.exe:103", "helper.exe:110"]),
          f"kill targets = the root, Strata pythons, exes under strata_dir, conhost of a kept process ({got})")
    check("chrome.exe:104" not in got and "chrome.exe:105" not in got, "a non-Strata child (browser) and its child survive")
    check("conhost.exe:106" not in got, "the conhost of a spared process is spared")
    check("python.exe:107" not in got, "a python child that does not run serve\\server.py is spared")
    check("keepme.exe:108" not in got, "a user_whitelist name is spared even under strata_dir")
    check("sibling.exe:109" not in got, "an exe in a sibling folder with the same prefix (StrataX) is spared")
    check("noexe.exe:111" not in got, "a child whose exe path is unknown is spared")
    check(not any(n.endswith(":200") or n.endswith(":201") for n in got), "nothing outside the root's tree is ever a target")

    # strict identity beats the whitelist: the server itself is killed even if the user whitelisted 'python'
    m2 = mgr(user_whitelist=["python"])
    got2 = names(m2._kill_targets(root, log_skips=False))
    check("python.exe:101" in got2 and "python.exe:107" not in got2,
          "whitelisting 'python' spares other pythons but not a process running serve\\server.py")

    # no strata_dir configured: only cmdline-identified servers (+ their conhosts) are targets
    m3 = mgr(strata_dir="")
    got3 = names(m3._kill_targets(root, log_skips=False))
    check(got3 == sorted(["python.exe:100", "python.exe:101"]),
          f"without strata_dir only serve\\server.py pythons are targets ({got3})")


def t_unreadable_and_lookalikes() -> None:
    """Processes whose facts cannot be read, and pythons that only MENTION the server script, are never targets
    and never the root."""
    reset()
    root = server(100)
    base_py = r"C:\Py\python.exe"                         # the base interpreter: not under strata_dir
    hidden_name = FakeProc(101, "python.exe", [base_py, SERVE, "--port", str(PORT)], base_py, 100)
    hidden_name.deny = ("name",)                         # a server child, but its name cannot be read
    hidden_cmd = FakeProc(102, "python.exe", [base_py, SERVE, "--port", str(PORT)], base_py, 100)
    hidden_cmd.deny = ("cmdline",)
    no_exe = FakeProc(103, "strata.exe", [S + r"\engine\strata.exe"], S + r"\engine\strata.exe", 100)
    no_exe.deny = ("exe",)
    gone = FakeProc(104, "strata.exe", [S + r"\engine\strata.exe"], S + r"\engine\strata.exe", 100)
    gone.gone = ("name", "cmdline", "exe")
    orphan_con = FakeProc(105, "conhost.exe", ["conhost"], r"C:\Windows\System32\conhost.exe", 102)
    con_noppid = FakeProc(106, "conhost.exe", ["conhost"], r"C:\Windows\System32\conhost.exe", 100)
    con_noppid.deny = ("ppid",)
    # pythons that mention serve\server.py without running it
    FakeProc(110, "python.exe", ["python", "-c", "print(r'" + SERVE + "')"], r"C:\Py\python.exe", 100)
    FakeProc(111, "python.exe", ["python", "tool.py", "--watch", SERVE + ".bak"], r"C:\Py\python.exe", 100)
    FakeProc(112, "python.exe", ["python", "tool.py", "--note", "restart " + SERVE + " later"], r"C:\Py\python.exe", 100)
    FakeProc(113, "python.exe", ["python", r"C:\x\myserve\server.py"], r"C:\Py\python.exe", 100)
    FakeProc(114, "python.exe", ["python", r"C:\x\serve\server.pyc"], r"C:\Py\python.exe", 100)
    FakeProc(115, "pythonista.exe", ["pythonista", "run"], r"C:\Apps\pythonista.exe", 100)
    m = mgr()
    got = names(m._kill_targets(root, log_skips=False))
    check(got == ["python.exe:100"],
          f"unreadable children and look-alike pythons: only the root is a target ({got})")
    for p, why in ((hidden_name, "name unreadable"), (hidden_cmd, "command line unreadable")):
        check(not m._is_engine_proc(p), f"a process is not identified as the server when its {why}")
    for pid, why in ((110, "the path inside a -c string"), (111, "a longer file name starting with it"),
                     (112, "the path inside a sentence"), (113, "another folder ending in ...serve"),
                     (114, "server.pyc")):
        check(not m._is_engine_proc(FakeProc.table[pid]), f"not the server: {why}")
    del no_exe, gone, orphan_con

    # the same look-alikes can never become the root: not as the listener, not through the process-table fallback
    real_proc, real_iter = psutil.Process, psutil.process_iter
    try:
        for pid in (110, 112, 101, 102):
            mm = mgr()
            mm._listener_pids = lambda pid=pid: [pid]  # type: ignore[method-assign]
            psutil.Process = lambda p=None: FakeProc.table[p]  # type: ignore[assignment,misc]
            r, foreign = mm._locate_root(fallback=True)
            check(r is None and foreign is not None, f"listener pid {pid} (look-alike / unreadable) is foreign, never a root")
        psutil.Process = real_proc  # type: ignore[misc]
        mm = mgr()
        mm._listener_pids = lambda: []  # type: ignore[method-assign]
        others = [p for p in FakeProc.table.values() if p.pid != 100]
        psutil.process_iter = lambda *a, **k: iter(others)  # type: ignore[assignment]
        r, _ = mm._locate_root(fallback=True)
        check(r is None, "the fallback scan finds no root among look-alikes and unreadable processes")
    finally:
        psutil.Process, psutil.process_iter = real_proc, real_iter  # type: ignore[misc,assignment]

    # an unreadable parent is never followed and never a launcher
    reset()
    par = FakeProc(50, "cmd.exe", ["cmd.exe", "/c", S + r"\run-x.bat"], r"C:\Windows\System32\cmd.exe", None, ctime=50)
    par.deny = ("name", "cmdline")
    root = server(100, 50, ctime=100)
    m = mgr()
    check(m._launcher_cmd(root) is None, "a parent whose name / command line cannot be read is never the launcher")
    check(m._topmost_engine(root) is root, "...and is never climbed to as the root")


# --------------------------------------------------------------------------------------------- launcher cmd.exe

def t_launcher_cmd() -> None:
    def case(parent_name, parent_cmd, *, cwd="", parent_ctime=50.0, **cfg):
        reset()
        FakeProc(50, parent_name, parent_cmd, r"C:\Windows\System32\cmd.exe", None, ctime=parent_ctime, cwd=cwd)
        root = server(100, 50, ctime=100.0)
        return mgr(**cfg)._launcher_cmd(root)

    bat = S + r"\run-swift.bat"
    check(case("cmd.exe", ["cmd.exe", "/c", f'"{bat}"']) is not None, "cmd.exe /c \"<strata_dir>\\run-x.bat\" is the launcher")
    check(case("cmd.exe", ["cmd.exe", "/c", bat]) is not None, "unquoted absolute run-x.bat under strata_dir counts")
    check(case("cmd.exe", ["cmd.exe", "/c", "run-swift.bat"], cwd=S) is not None, "relative run-x.bat resolved against the cmd's cwd")
    check(case("cmd.exe", ["cmd.exe", "/k", f'"{bat}"']) is None, "cmd /k (an interactive console) is never killed")
    check(case("cmd.exe", ["cmd.exe"]) is None, "a plain cmd.exe parent is never killed")
    check(case("powershell.exe", ["powershell", "-c", bat]) is None, "a non-cmd parent is never killed")
    check(case("cmd.exe", ["cmd.exe", "/c", r"C:\Other\run-x.bat"]) is None, "a run-x.bat outside strata_dir does not count")
    check(case("cmd.exe", ["cmd.exe", "/c", r"C:\AI\StrataX\run-x.bat"]) is None, "a sibling folder with the same prefix does not count")
    check(case("cmd.exe", ["cmd.exe", "/c", "run-swift.bat"], cwd=r"C:\Other") is None, "relative run-x.bat in another cwd does not count")
    check(case("cmd.exe", ["cmd.exe", "/c", r"C:\Mine\run-own.bat"], launcher_bat=r"C:\Mine\run-own.bat") is not None,
          "the configured launcher .bat counts even outside strata_dir")
    check(case("cmd.exe", ["cmd.exe", "/c", f'"{bat}"'], parent_ctime=500.0) is None,
          "a 'parent' created after the server (pid reuse) is never killed")
    check(case("cmd.exe", ["cmd.exe", "/c", f'"{bat}"'], strata_dir="") is None,
          "with no strata_dir and no configured launcher nothing is a launcher")


# --------------------------------------------------------------------------------------------- identity

def t_verified() -> None:
    reset()
    inside = server(100)
    outside = server(101, script=r"C:\Other\proj\serve\server.py")
    m = mgr()
    check(m._verified_engine(inside, probe=False), "serve\\server.py under strata_dir is verified without HTTP")
    check(not m._verified_engine(outside, probe=False), "another project's serve\\server.py is not verified by cmdline")
    check(m._verified_engine(outside, state({"service": "strata"}), probe=False), "...but is when /health says service == strata")
    check(not m._verified_engine(outside, state({"service": "other"}), probe=False), "/health naming another service does not verify")
    check(not m._verified_engine(outside, state({}), probe=False), "/health without a service field does not verify")
    m._spawn_proc = outside  # type: ignore[assignment]
    check(m._verified_engine(outside, probe=False), "the process this app spawned is verified")
    m._spawn_proc = None
    m._http = health_http({"service": "strata"})  # type: ignore[method-assign]
    check(m._verified_engine(outside), "probe_health=True asks /health when the process facts are not enough")

    # strict process identity (argv token ends with serve\server.py, python interpreter only)
    check(is_server(m, inside), "python + serve\\server.py token = server process")
    reset()
    ed = FakeProc(1, "Code.exe", [r"C:\VSCode\Code.exe", SERVE], r"C:\VSCode\Code.exe")
    sh = FakeProc(2, "bash.exe", ["bash", "-c", "python serve/server.py"], r"C:\Git\bash.exe")
    check(not is_server(m, ed) and not is_server(m, sh), "an editor or a shell that mentions the path is not")


def t_topmost() -> None:
    reset()
    FakeProc(10, "cmd.exe", ["cmd.exe", "/c", S + r"\run-x.bat"], r"C:\Windows\System32\cmd.exe", None, ctime=10)
    top = server(20, 10, ctime=20)
    mid = server(30, 20, ctime=30)
    leaf = server(40, 30, ctime=40)
    check(mgr()._topmost_engine(leaf) is top, "the root is the top-most Strata python, not the cmd.exe above it")
    reset()
    FakeProc(20, "python.exe", [PY, SERVE], PY, None, ctime=999)          # 'parent' newer than the child: pid reuse
    child = server(30, 20, ctime=30)
    check(mgr()._topmost_engine(child) is child, "a parent created after the child is not followed")
    del mid


def t_locate_fallback() -> None:
    """No listener visible: the process-table fallback only accepts a server that claims OUR port."""
    def locate(procs, port=PORT, **cfg):
        m = mgr(port=port, **cfg)
        m._listener_pids = lambda: []  # type: ignore[method-assign]
        real = psutil.process_iter
        psutil.process_iter = lambda *a, **k: iter(procs)  # type: ignore[assignment]
        try:
            return m._locate_root(fallback=True)
        finally:
            psutil.process_iter = real  # type: ignore[assignment]

    reset()
    ours = server(100, port=PORT)
    other_port = server(101, port=PORT + 1)
    no_port = server(102, port=None)
    not_server = FakeProc(103, "python.exe", ["python", "x.py", "--port", str(PORT)], r"C:\Python\python.exe")
    root, foreign = locate([other_port, no_port, not_server, ours])
    check(root is ours and foreign is None, "fallback picks the server whose --port is ours")
    root, _ = locate([other_port, no_port, not_server])
    check(root is None, "a server on another port, one without --port (non-default port), and a non-server are not ours")
    root, _ = locate([no_port], port=8080)
    check(root is no_port, "on the default port a server under strata_dir without --port is accepted")
    reset()
    stray = server(104, script=r"C:\Other\serve\server.py", port=None)
    root, _ = locate([stray], port=8080)
    check(root is None, "...but not one outside strata_dir")

    # a listener that is not a Strata process is reported as foreign and is never a root
    reset()
    web = FakeProc(300, "nginx.exe", ["nginx"], r"C:\nginx\nginx.exe")
    m = mgr()
    m._listener_pids = lambda: [300]  # type: ignore[method-assign]
    real = psutil.Process
    psutil.Process = lambda pid=None: FakeProc.table[pid]  # type: ignore[assignment,misc]
    try:
        root, foreign = m._locate_root(fallback=True)
    finally:
        psutil.Process = real  # type: ignore[misc]
    check(root is None and foreign is not None and "nginx.exe" in foreign and "not Strata" in foreign,
          f"a foreign listener is named and never becomes a root ({foreign})")
    del web


# --------------------------------------------------------------------------------------------- _do_stop

class Spy:
    """Replaces the manager's side-effecting pieces and records what the stop asked for."""

    def __init__(self, m: EndpointManager, *, root=None, foreign=None, health=None, unload="ok", listening=False):
        self.unloads: list[bool] = []
        self.killed: list[list[str]] = []
        self.health_calls = 0
        m._locate_root = lambda fallback=True: (root, foreign)  # type: ignore[method-assign]

        inner = health_http(health)

        def _http(method: str, path: str, timeout: float) -> HttpReply:
            self.health_calls += 1
            return inner(method, path, timeout)
        m._http = _http  # type: ignore[method-assign]

        def _unload(force):
            self.unloads.append(force)
            return unload
        m._unload = _unload  # type: ignore[method-assign]

        def _kill(procs):
            self.killed.append(names(procs))
            return [p.pid for p in procs], []
        m._kill = _kill  # type: ignore[method-assign]
        m.is_running = lambda: listening  # type: ignore[method-assign]
        m._wait_port_released = lambda timeout: True  # type: ignore[method-assign]


def run_stop(m: EndpointManager, force: bool = False) -> dict:
    res = m._new_result(False)
    real_sleep = sm.time.sleep
    sm.time.sleep = lambda s: None  # type: ignore[assignment]
    try:
        m._do_stop(res, force)
    finally:
        sm.time.sleep = real_sleep  # type: ignore[assignment]
    return res


def t_do_stop() -> None:
    # 1. a foreign listener: nothing is unloaded, nothing is killed
    reset()
    m = mgr()
    spy = Spy(m, foreign="port 18999 held by nginx.exe (pid 300), not Strata", listening=True)
    res = run_stop(m)
    check(not res["ok"] and "not Strata" in (res["error"] or "") and not spy.unloads and not spy.killed,
          "foreign listener: refused, no /unload, no kill")

    # 2. a python serve\server.py that is not verified as Strata: refused
    reset()
    stranger = server(100, script=r"C:\Other\proj\serve\server.py")
    m = mgr()
    spy = Spy(m, root=stranger, health={"service": "other"}, listening=True)
    res = run_stop(m, force=True)
    check(not res["ok"] and "not Strata" in (res["error"] or "") and not spy.unloads and not spy.killed
          and res["port_released"] is False,
          "unverified serve\\server.py: refused even with force, no /unload, no kill")

    # 3. no process found and /health is not Strata: never POST /unload at an unknown service
    reset()
    m = mgr()
    spy = Spy(m, root=None, health={"service": "other"}, listening=True)
    res = run_stop(m)
    check(not res["ok"] and res["unload"] == "unavailable" and not spy.unloads and not spy.killed
          and "could not identify" in (res["error"] or ""),
          "no process + /health not Strata: no /unload, no kill, error says it could not identify Strata")
    spy = Spy(m, root=None, health=None, listening=False)
    res = run_stop(m)
    check(res["ok"] and res["port_released"] and not spy.unloads and not spy.killed,
          "no process + nothing listening: reported as already released, nothing touched")

    # 4. busy without force: unload asked, nothing killed
    reset()
    root = server(100)
    m = mgr()
    spy = Spy(m, root=root, unload="busy", listening=True)
    res = run_stop(m)
    check(res["error"] == "busy" and res["unload"] == "busy" and spy.unloads == [False] and not spy.killed,
          "busy reply without force: nothing is killed")

    # 5. the normal stop kills exactly the kill targets plus the launcher cmd.exe
    reset()
    FakeProc(50, "cmd.exe", ["cmd.exe", "/c", S + r"\run-x.bat"], r"C:\Windows\System32\cmd.exe", None, ctime=50)
    FakeProc(60, "explorer.exe", ["explorer"], r"C:\Windows\explorer.exe", None, ctime=1)
    root = server(100, 50, ctime=100)
    FakeProc(102, "strata.exe", [S + r"\engine\strata.exe"], S + r"\engine\strata.exe", 100, ctime=102)
    FakeProc(104, "chrome.exe", ["chrome"], r"C:\Program Files\Chrome\chrome.exe", 100, ctime=104)
    m = mgr()
    spy = Spy(m, root=root, unload="ok", listening=True)
    res = run_stop(m)
    check(res["ok"] and res["port_released"] and res["unload"] == "ok" and spy.unloads == [False],
          "verified Strata: unload, then stop succeeds")
    check(spy.killed == [sorted(["python.exe:100", "strata.exe:102", "cmd.exe:50"])],
          f"killed exactly the server, the engine exe and the run-x.bat cmd.exe ({spy.killed})")
    check(sorted(res["killed_pids"]) == [50, 100, 102], "killed_pids lists the same three")

    # 6. force on a busy server still kills only the same set
    spy = Spy(m, root=root, unload="busy", listening=True)
    res = run_stop(m, force=True)
    check(spy.unloads == [True] and spy.killed == [sorted(["python.exe:100", "strata.exe:102", "cmd.exe:50"])],
          "force stop: same kill set, never the browser")

    # 7. process hidden but /health says Strata: unload is allowed, kill has no target
    reset()
    m = mgr()
    spy = Spy(m, root=None, health={"service": "strata"}, listening=True)
    res = run_stop(m)
    check(spy.unloads == [False] and not spy.killed, "hidden process + /health == strata: /unload only, nothing killed")


def t_result_shape() -> None:
    m = mgr()
    res = m._new_result(False)
    for key in ("ok", "port_released", "killed_pids", "ram_freed_mb", "unload", "error", "cancelled_start"):
        check(key in res, f"stop result keeps the '{key}' field")


def main() -> int:
    for t in (t_kill_targets, t_unreadable_and_lookalikes, t_launcher_cmd, t_verified, t_topmost, t_locate_fallback, t_do_stop, t_result_shape):
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"{t.__name__} raised {exc!r}")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
