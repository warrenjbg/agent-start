"""The engine interface: registry, the Strata engine's answers, and what a DIFFERENT engine can and cannot make
the supervisor do. Pure - fake processes, fake HTTP, no network, nothing real is started or killed.

The second half drives `EndpointManager` with a minimal fake engine shaped like llama.cpp's llama-server (an
exe, no unload call, no launcher file, identity from a response header) to prove that a new engine module needs
no change in the stop path, and that an engine - even a buggy one - cannot widen a kill beyond the verified
root's own tree and direct parent.

Run: .venv\\Scripts\\python.exe tests\\test_engines.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AGENT_START_HOME", tempfile.mkdtemp(prefix="agentstart-engines-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import detector  # noqa: E402
import engines  # noqa: E402
import server_manager as sm  # noqa: E402
from detector import GraphContext, ProcInfo, build_apps  # noqa: E402
from engines import base  # noqa: E402
from engines.base import Engine, HttpReply, LaunchError, LaunchPlan, ServiceState  # noqa: E402
from engines.strata import StrataEngine, is_strata_cmd  # noqa: E402
from server_manager import EndpointManager  # noqa: E402
from test_kill_path import PORT, FakeProc, Spy, names, reset, run_stop  # noqa: E402

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


S = r"C:\AI\Strata"
PY = S + r"\.venv\Scripts\python.exe"


def http_of(routes: dict):
    """http() over {(method, path): HttpReply}; anything else = no answer."""
    calls: list[tuple[str, str]] = []

    def _http(method: str, path: str, timeout: float) -> HttpReply:
        calls.append((method, path))
        return routes.get((method, path), HttpReply())
    _http.calls = calls  # type: ignore[attr-defined]
    return _http


# --------------------------------------------------------------------------------------------- registry

def t_registry() -> None:
    check(isinstance(engines.get_engine({"backend": "strata"}), StrataEngine), "backend 'strata' -> the Strata engine")
    check(isinstance(engines.get_engine({}), StrataEngine), "a config without the key (older config.json) -> Strata")
    check(isinstance(engines.get_engine(None), StrataEngine), "no config -> Strata")
    check(isinstance(engines.get_engine({"backend": "  STRATA "}), StrataEngine), "case and spaces do not matter")
    for junk in ("", None, 7, ["strata"], "llama-cpp-not-there"):
        check(isinstance(engines.get_engine({"backend": junk}), StrataEngine), f"backend {junk!r} falls back to Strata")
    check(engines.backend_of({"backend": "Other"}) == "other" and engines.backend_of({}) == "strata", "backend_of normalises")
    check("strata" in engines.available(), "the registry lists strata")
    e = engines.get_engine({})
    check(e.id == "strata" and e.name == "Strata" and e.default_port == 8080 and e.log_prefix == "strata",
          "Strata keeps its name, port 8080 and strata-*.log files")
    check(e.capabilities == base.ALL_CAPABILITIES, "Strata supports every capability")
    check(all(isinstance(c, str) for c in base.ALL_CAPABILITIES) and len(base.ALL_CAPABILITIES) == 9, "nine capabilities")


# --------------------------------------------------------------------------------------------- Strata answers

def t_strata_probe() -> None:
    e = StrataEngine()
    st = e.probe(http_of({("GET", "/health"): HttpReply(200, {"service": "strata", "loaded": True, "model": "m1"})}), 1)
    check(st == ServiceState(True, True, True, "m1"), "ready Strata: answered, identified, loaded, model")
    st = e.probe(http_of({("GET", "/health"): HttpReply(200, {"service": "strata", "loaded": False})}), 1)
    check(st.is_engine and st.loaded is False and st.model is None, "Strata still loading: identified, not loaded")
    st = e.probe(http_of({("GET", "/health"): HttpReply(200, {"service": "other", "loaded": True})}), 1)
    check(st.answered and not st.is_engine, "another service's /health answers but is not identified")
    st = e.probe(http_of({("GET", "/health"): HttpReply(200, {"loaded": "yes"})}), 1)
    check(not st.is_engine and st.loaded is False, "loaded must be exactly true")
    for reply in (HttpReply(), HttpReply(503, {"service": "strata"}), HttpReply(200, ["x"]), HttpReply(200, None)):
        check(e.probe(http_of({("GET", "/health"): reply}), 1) == base.NO_ANSWER, f"no usable answer: {reply.code} {reply.json!r}")

    h = http_of({("GET", "/v1/models"): HttpReply(200, {"data": [{"id": "served-1"}]})})
    check(e.fetch_model_id(h, ServiceState(True, True, True, "from-health")) == "served-1", "model id from /v1/models")
    check(e.fetch_model_id(http_of({}), ServiceState(True, True, True, "from-health")) == "from-health",
          "...else the one /health named")

    h = http_of({("POST", "/unload?wait=10"): HttpReply(200, {}), ("POST", "/unload"): HttpReply(409, {})})
    check(e.unload(h, False) == "ok" and h.calls == [("POST", "/unload?wait=10")], "polite unload waits (?wait=10)")
    check(e.unload(h, True) == "busy", "409 = busy")
    check(e.unload(http_of({}), True) == "unavailable", "no answer = unavailable")
    h = http_of({("POST", "/load"): HttpReply(200, {})})
    check(e.nudge_load(h) == 200 and h.calls == [("POST", "/load")], "load nudge = POST /load")


def t_strata_identity() -> None:
    e = StrataEngine()
    check(sm._is_strata_cmd is is_strata_cmd, "server_manager still exposes the strict rule under its old name")
    check(e.is_server_process("python.exe", [PY, S + r"\serve\server.py", "--port", "8080"]), "the server is the server")
    check(not e.is_server_process("Code.exe", ["Code.exe", S + r"\serve\server.py"]), "an editor with the file open is not")
    cfg = {"strata_dir": S}
    cmd = [PY, S + r"\serve\server.py"]
    check(e.claims_port(cmd + ["--port", "9000"], 9000, cfg), "claims the port its --port names")
    check(not e.claims_port(cmd + ["--port", "9001"], 9000, cfg), "not another port")
    check(e.claims_port(cmd, 8080, cfg) and not e.claims_port(cmd, 9000, cfg), "no --port: only the default port, only under strata_dir")
    check(not e.claims_port([PY, r"C:\Other\serve\server.py"], 8080, cfg), "no --port outside strata_dir: never")

    reset()
    inside = FakeProc(1, "python.exe", cmd, PY)
    outside = FakeProc(2, "python.exe", ["python", r"C:\Other\serve\server.py"], r"C:\Py\python.exe")
    rel = FakeProc(3, "python.exe", ["python", r"serve\server.py"], PY, cwd=S)
    check(e.verified_by_process(inside, cfg) and not e.verified_by_process(outside, cfg), "verified = script under strata_dir")
    check(e.verified_by_process(rel, cfg), "a relative script is resolved against the process cwd")
    check(not e.verified_by_process(inside, {}), "no strata_dir configured: process facts prove nothing")


def t_strata_launch() -> None:
    e = StrataEngine()
    tmp = Path(tempfile.mkdtemp(prefix="agentstart-eng-"))
    exe = tmp / "python.exe"
    exe.write_bytes(b"")
    sub = tmp / "serve"
    sub.mkdir()

    def err(cfg) -> str:
        try:
            e.plan_launch(cfg)
        except LaunchError as exc:
            return str(exc)
        return ""

    check("No Strata launcher chosen yet" in err({}), "no launch_cmd: tells the user to pick a run-*.bat")
    check("must start with python.exe" in err({"launch_cmd": [str(tmp / "run-x.bat")], "strata_dir": str(tmp)}),
          "a .bat as the command is refused (never run through cmd.exe)")
    check("launcher not found" in err({"launch_cmd": [str(tmp / "gone" / "python.exe")], "strata_dir": str(tmp)}), "missing exe")
    check("strata_dir not found" in err({"launch_cmd": [str(exe)], "strata_dir": str(tmp / "nope")}), "missing strata_dir")
    other = tmp / "llama-server.exe"             # another engine's command left in a shared config.json
    other.write_bytes(b"")
    msg = err({"launch_cmd": [str(other), "-m", "x.gguf"], "strata_dir": str(tmp)})
    check("must start with python.exe" in msg and "llama-server.exe" in msg,
          f"a program that is not a python interpreter is never started as Strata ({msg!r})")
    check(not e.is_configured({"launch_cmd": [str(other), "-m", "x.gguf"]}) and not e.is_configured({})
          and e.is_configured({"launch_cmd": [str(exe), "serve\\server.py"]}),
          "configured = a python launch command (another engine's command does not count)")
    for name in ("pythonw.exe", "py.exe", "python3.14.exe"):
        p2 = tmp / name
        p2.write_bytes(b"")
        check(not err({"launch_cmd": [str(p2), "serve\\server.py"], "strata_dir": str(tmp)}), f"{name} is accepted")
    plan = e.plan_launch({"launch_cmd": [str(exe), "serve\\server.py"], "strata_dir": str(tmp)})
    check(isinstance(plan, LaunchPlan) and plan.cmd == [str(exe), "serve\\server.py"] and plan.cwd == str(tmp),
          "valid settings: the command as configured, cwd = strata_dir")
    check(plan.env == {"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}, "Strata's two environment variables")
    plan = e.plan_launch({"launch_cmd": [str(exe)], "strata_dir": str(tmp), "launch_cwd": str(sub)})
    check(plan.cwd == str(sub), "launch_cwd (the .bat's cd /d) wins when it exists")
    plan = e.plan_launch({"launch_cmd": [str(exe)], "strata_dir": str(tmp), "launch_cwd": str(tmp / "gone")})
    check(plan.cwd == str(tmp), "a launch_cwd that no longer exists falls back to strata_dir")


def t_sweep_protection() -> None:
    """The engine's loose 'protect' rule equals the detector's built-in Strata rule, case by case."""
    e = StrataEngine()
    cases = [
        ([PY, S + r"\serve\server.py"], PY, S),
        (["hermes", "chat", "-q", r"fix C:\x\serve\server.py"], r"C:\Tools\hermes.exe", S),    # loose on purpose
        (["strata.exe"], S + r"\engine\strata.exe", S),
        (["x.exe"], r"C:\AI\StrataX\x.exe", S),
        (["x.exe"], r"C:\Other\x.exe", S),
        (["x.exe"], S + r"\x.exe", ""),
        (None, None, S),
        ([], "", S),
        (["x.exe"], S, S),
        (["X.EXE"], S.upper() + r"\ENGINE\X.EXE", S),
    ]
    same = True
    for cmd, exe, sdir in cases:
        builtin = detector.is_strata_server_cmdline(cmd) or detector.under_dir(exe, sdir)
        got = e.protects_process(cmd, exe, {"strata_dir": sdir})
        if got != builtin:
            same = False
            print(f"   mismatch: {cmd!r} {exe!r} {sdir!r}: engine {got} vs built-in {builtin}")
    check(same, f"engine.protects_process == the detector's built-in rule on {len(cases)} cases")

    def P(pid, name, ppid, exe, cmd=None, rss_mb=3000.0):
        return ProcInfo(pid=pid, name=name, exe=exe, ppid=ppid, create_time=float(pid),
                        rss=int(rss_mb * 1024 * 1024), cmdline=cmd or [exe])

    procs = {801: P(801, "python.exe", 0, r"C:\Py\python.exe", [r"C:\Py\python.exe", r"C:\Strata\serve\server.py"]),
             802: P(802, "strata.exe", 801, r"C:\Strata\engine\strata.exe"),
             900: P(900, "llama-server.exe", 0, r"C:\llama\llama-server.exe"),
             901: P(901, "editor.exe", 0, r"C:\Apps\editor.exe")}

    def ctx(**kw):
        base_kw = dict(self_pid=None, strata_dir="C:\\Strata", system_root="C:\\Windows", game_dirs=[],
                       min_ram_mb=250, min_vram_mb=200, windows={}, vram_mb={}, vram_cap_mb=16000.0,
                       tree_protected=frozenset())
        base_kw.update(kw)
        return GraphContext(**base_kw)

    def pids(c):
        return sorted(p for a in build_apps(procs, c) for p in a.member_pids)

    builtin = pids(ctx())
    check(builtin == [900, 901], f"built-in rule: the Strata tree is never a sweep candidate ({builtin})")
    cfg = {"strata_dir": "C:\\Strata"}
    via_engine = pids(ctx(engine_protects=lambda i: e.protects_process(i.cmdline, i.exe, cfg)))
    check(via_engine == builtin, "the same candidates when the rule comes from the Strata engine")
    other = pids(ctx(strata_dir="", engine_protects=lambda i: (i.name or "").casefold() == "llama-server.exe"))
    check(900 not in other and 901 in other, "another engine's rule protects ITS server instead")

    def boom(_i):
        raise RuntimeError("engine bug")
    check(pids(ctx(engine_protects=boom)) == builtin, "a failing engine rule falls back to the built-in protection")

    d = detector.ProcessDetector({"strata_dir": "C:\\Strata"}, engine=e)
    try:
        fn = d._engine_protects()
        check(fn is not None and fn(procs[802]) and not fn(procs[901]), "ProcessDetector passes the engine's rule on")
        check(detector.ProcessDetector({})._engine_protects() is None, "no engine given: the built-in rule")
    finally:
        d.close()


# --------------------------------------------------------------------------------------------- another engine

LL = r"C:\llama"


class FakeLlama(Engine):
    """Shaped like llama-server: one exe, identity by exe name + a `Server` header, no unload, no launcher."""
    id = "fake-llama"
    name = "llama.cpp"
    default_port = 9931
    log_prefix = "llama"
    capabilities = frozenset()

    def is_server_process(self, name, cmdline):
        return (name or "").casefold() == "llama-server.exe"

    def verified_by_process(self, proc, cfg):
        return os.path.normcase(proc.exe()).startswith(os.path.normcase(LL) + os.sep)

    def claims_port(self, cmdline, port, cfg):
        return base.explicit_port(cmdline) == port

    def probe(self, http, timeout):
        r = http("GET", "/health", timeout)
        if r.code is None:
            return base.NO_ANSWER
        ours = r.header("Server") == "llama.cpp"
        return ServiceState(answered=True, is_engine=ours, loaded=r.code == 200 if ours else None)

    def plan_launch(self, cfg):
        raise LaunchError("no llama-server chosen")

    def protects_process(self, cmdline, exe, cfg):
        return os.path.basename(exe or "").casefold() == "llama-server.exe"


def lmgr(engine: Engine | None = None, **cfg) -> EndpointManager:
    c = {"port": PORT, "endpoint_url": f"http://127.0.0.1:{PORT}/v1", "user_whitelist": []}
    c.update(cfg)
    m = EndpointManager(c, notify=False, engine=engine or FakeLlama())
    m._http = lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP used in a pure test"))  # type: ignore
    return m


def llama_tree():
    reset()
    FakeProc(40, "cmd.exe", ["cmd.exe", "/c", r"C:\llama\start.bat"], r"C:\Windows\System32\cmd.exe", None, ctime=40)
    root = FakeProc(100, "llama-server.exe", [LL + r"\llama-server.exe", "--port", str(PORT)], LL + r"\llama-server.exe", 40)
    FakeProc(101, "llama-server.exe", [LL + r"\llama-server.exe", "--port", "5001"], LL + r"\llama-server.exe", 100)
    FakeProc(102, "conhost.exe", ["conhost"], r"C:\Windows\System32\conhost.exe", 101)
    FakeProc(103, "helper.exe", [LL + r"\helper.exe"], LL + r"\helper.exe", 100)
    FakeProc(104, "chrome.exe", ["chrome"], r"C:\Program Files\Chrome\chrome.exe", 100)
    FakeProc(200, "python.exe", [PY, S + r"\serve\server.py", "--port", str(PORT)], PY, None)   # a Strata, elsewhere
    FakeProc(300, "llama-server.exe", [LL + r"\llama-server.exe", "--port", "7000"], LL + r"\llama-server.exe", None)
    return root


def t_other_engine_stop() -> None:
    root = llama_tree()
    m = lmgr()
    check(m.engine.id == "fake-llama" and m._port == PORT, "the manager uses the engine it is given")
    got = names(m._kill_targets(root, log_skips=False))
    check(got == sorted(["llama-server.exe:100", "llama-server.exe:101", "conhost.exe:102"]),
          f"targets = the server, its server children (router mode) and their conhost ({got})")
    check("helper.exe:103" not in got, "a helper is spared unless the engine claims it (default owns_child = no)")
    check(not any(n.endswith(":200") or n.endswith(":300") for n in got), "other servers outside the tree are never targets")
    check(m._launcher_cmd(root) is None, "no launcher capability: the parent cmd.exe is never killed")

    spy = Spy(m, root=root, listening=True)
    m._unload = EndpointManager._unload.__get__(m)  # type: ignore[method-assign]  # the real one: asks the engine
    res = run_stop(m)
    check(res["ok"] and res["unload"] == "unsupported" and res["port_released"],
          f"no unload capability: unload reported 'unsupported', the stop still succeeds ({res['unload']})")
    check(spy.killed == [sorted(["llama-server.exe:100", "llama-server.exe:101", "conhost.exe:102"])],
          f"killed exactly the engine's part of the tree ({spy.killed})")
    check(spy.health_calls == 0, "a root verified by process facts needs no HTTP at all")

    # the full worker adds the engine-neutral verdict the UI reads
    got_res: list[dict] = []
    spy = Spy(m, root=root, listening=True)
    real_sleep = sm.time.sleep
    sm.time.sleep = lambda s: None  # type: ignore[assignment]
    try:
        m._stop_worker(got_res.append, False, False, None)
    finally:
        sm.time.sleep = real_sleep  # type: ignore[assignment]
    check(len(got_res) == 1 and got_res[0]["released"] is True and got_res[0]["port_released"] is True,
          "a finished stop reports released = True (the model is no longer held)")
    got_res.clear()
    spy = Spy(m, foreign="port held by x, not llama.cpp", listening=True)
    m._stop_worker(got_res.append, False, False, None)
    check(len(got_res) == 1 and got_res[0]["released"] is False and not got_res[0]["ok"],
          "a refused stop reports released = False")

    # a Strata process holding the port is FOREIGN to this engine
    m = lmgr()
    strata = FakeProc.table[200]
    check(not m._is_engine_proc(strata), "a Strata server is not this engine's process")
    import psutil
    real = psutil.Process
    m._listener_pids = lambda: [200]  # type: ignore[method-assign]
    psutil.Process = lambda pid=None: FakeProc.table[pid]  # type: ignore[assignment,misc]
    try:
        r, foreign = m._locate_root()
    finally:
        psutil.Process = real  # type: ignore[misc]
    check(r is None and foreign is not None and foreign.endswith("not llama.cpp"),
          f"...the message names the configured engine ({foreign})")

    # a look-alike exe outside the install folder: refused unless the port's own answer identifies the engine
    reset()
    stray = FakeProc(100, "llama-server.exe", [r"C:\Downloads\llama-server.exe", "--port", str(PORT)],
                     r"C:\Downloads\llama-server.exe")
    m = lmgr()
    spy = Spy(m, root=stray, listening=True)
    m._http = http_of({("GET", "/health"): HttpReply(200, {"status": "ok"}, {"server": "nginx"})})  # type: ignore
    res = run_stop(m)
    check(not res["ok"] and "not llama.cpp" in (res["error"] or "") and not spy.killed, "unverified look-alike: refused")
    spy = Spy(m, root=stray, listening=True)
    m._http = http_of({("GET", "/health"): HttpReply(503, {"error": "Loading model"}, {"server": "llama.cpp"})})  # type: ignore
    res = run_stop(m)
    check(res["ok"] and spy.killed == [["llama-server.exe:100"]], "identified by its Server header (even while loading): stopped")

    # launch errors come from the engine, word for word
    out = lmgr()._do_start(__import__("threading").Event()) if not lmgr().is_running() else None
    check(out == ("error", "no llama-server chosen"), f"the engine's own launch message reaches the user ({out})")


class Hostile(FakeLlama):
    """An engine with bugs that would be dangerous if the supervisor trusted it blindly."""
    capabilities = frozenset({base.CAP_UNLOAD})

    def owns_child(self, proc, cfg):
        if proc.pid == 103:
            raise RuntimeError("boom")
        return True                                  # claims EVERYTHING it is asked about

    def launcher_process(self, root, cfg):
        return FakeProc.table.get(cfg.get("_fake_launcher"))

    def unload(self, http, force):
        raise RuntimeError("boom")

    def plan_launch(self, cfg):
        return LaunchPlan(cmd=[cfg.get("_cmd", "")], cwd=".")


class Crashing(FakeLlama):
    def is_server_process(self, name, cmdline):
        raise RuntimeError("boom")

    def verified_by_process(self, proc, cfg):
        raise RuntimeError("boom")

    def probe(self, http, timeout):
        raise RuntimeError("boom")


def t_engine_cannot_widen() -> None:
    root = llama_tree()
    FakeProc(105, "keepme.exe", ["keepme"], r"C:\Apps\keepme.exe", 100)
    m = lmgr(Hostile(), user_whitelist=["keepme"], _fake_launcher=300)
    got = names(m._kill_targets(root, log_skips=False))
    check(not any(n.endswith(":200") or n.endswith(":300") or n.endswith(":40") for n in got),
          f"an engine that claims everything still only gets members of the root's tree ({got})")
    check("keepme.exe:105" not in got, "...and never a user_whitelist name")
    check("helper.exe:103" not in got, "a child whose ownership check crashes is spared")
    check("chrome.exe:104" in got, "(it does get the tree members it claimed - claiming is the engine's job)")
    check(m._launcher_cmd(root) is None, "a 'launcher' that is not the root's direct parent is ignored")
    m = lmgr(Hostile(), _fake_launcher=40)
    check(m._launcher_cmd(root) is FakeProc.table[40], "the direct, older parent is accepted")
    FakeProc.table[40]._ctime = 9999.0
    check(m._launcher_cmd(root) is None, "...but not when it was created after the server (pid reuse)")
    check(m._unload(False) == "unavailable", "a crashing unload is reported as unavailable")

    for cmd in ("", r"C:\x\run.bat", r"C:\x\start.cmd", r"C:\x\gone.exe"):
        m = lmgr(Hostile(), _cmd=cmd)
        m.is_running = lambda: False  # type: ignore[method-assign]
        out = m._do_start(__import__("threading").Event())
        check(out is not None and out[0] == "error" and "not an existing .exe" in out[1],
              f"whatever an engine plans, {cmd!r} is never handed to CreateProcess")

    root = llama_tree()
    m = lmgr(Crashing())
    check(not m._is_engine_proc(root), "an identity check that crashes identifies nothing")
    check(not m._verified_engine(root, probe=False), "a verification that crashes verifies nothing")
    check(m._probe() == base.NO_ANSWER, "a probe that crashes is no answer")
    spy = Spy(m, root=root, listening=True)
    res = run_stop(m, force=True)
    check(not res["ok"] and not spy.killed and not spy.unloads, "so a stop with that engine kills and unloads nothing")


def t_api_key_headers() -> None:
    """An engine's own request headers (an API key) go to an identified port only - never with the identity probe."""
    seen: list[tuple[str, str, dict]] = []

    class Keyed(FakeLlama):
        capabilities = frozenset({base.CAP_UNLOAD, base.CAP_LOAD_NUDGE})

        def request_headers(self, cfg):
            return {"Authorization": f"Bearer {cfg.get('api_key')}"}

        def unload(self, http, force):
            return "ok" if http("POST", "/models/unload", 1.0).code == 200 else "unavailable"

        def nudge_load(self, http):
            return http("POST", "/models/load", 1.0).code

    m = EndpointManager({"port": PORT, "endpoint_url": f"http://127.0.0.1:{PORT}/v1", "api_key": "s3cret"},
                        notify=False, engine=Keyed())

    def fake_http(method, path, timeout, extra_headers=None):
        seen.append((method, path, dict(extra_headers or {})))
        return HttpReply(200, {"data": [{"id": "m"}]}, {"server": "llama.cpp"})
    m._http = fake_http  # type: ignore[method-assign]
    st = m._probe()
    check(st.is_engine and seen[-1] == ("GET", "/health", {}), f"the identity probe carries no key ({seen[-1]})")
    check(m._fetch_model_id(st) == "m" and seen[-1][2] == {"Authorization": "Bearer s3cret"},
          "the model lookup (after identification) carries it")
    check(m._unload(False) == "ok" and seen[-1][1:] == ("/models/unload", {"Authorization": "Bearer s3cret"}),
          "so does the unload")
    check(Engine().request_headers({}) == {} and StrataEngine().request_headers({"api_key": "x"}) == {},
          "engines send no extra headers unless they say so (Strata: none)")
    check(Engine.idle_grace_s is None and StrataEngine().idle_grace_s is None, "idle wait: the app's default for Strata")
    p = StrataEngine().metrics_poller({"port": 1}, fetch_metrics=lambda r, **k: None)
    check(p.reset(log_path="x.log") is None and p.poll("http://127.0.0.1:9") is None,
          "pollers take the config and each run's log path")


def t_review_fixes() -> None:
    """Independent review: the key never goes to a port that did not identify itself; engines without unload can
    still say "busy"; the registry finds engine modules by itself."""
    class Keyed(FakeLlama):
        capabilities = frozenset({base.CAP_UNLOAD})

        def request_headers(self, cfg):
            return {"Authorization": "Bearer s3cret"}

        def unload(self, http, force):
            return "ok" if http("POST", "/models/unload", 1.0).code == 200 else "unavailable"

    # listener PID hidden: the process is verified from process facts, but something else answers on the port
    root = llama_tree()
    seen: list[tuple[str, str, dict]] = []

    def nginx(method, path, timeout, extra_headers=None):
        seen.append((method, path, dict(extra_headers or {})))
        return HttpReply(200, {"status": "ok"}, {"server": "nginx"})
    m = EndpointManager({"port": PORT, "endpoint_url": f"http://127.0.0.1:{PORT}/v1"}, notify=False, engine=Keyed())
    spy = Spy(m, root=root, listening=True)
    m._unload = EndpointManager._unload.__get__(m)  # type: ignore[method-assign]
    m._http = nginx  # type: ignore[method-assign]
    run_stop(m)
    check(seen and all(h == {} for _m, _p, h in seen),
          f"a port that does not identify itself never receives the key ({[(p, bool(h)) for _m, p, h in seen]})")
    check(spy.killed == [sorted(["llama-server.exe:100", "llama-server.exe:101", "conhost.exe:102"])],
          "the verified process is still stopped")
    seen.clear()

    def llama(method, path, timeout, extra_headers=None):
        seen.append((method, path, dict(extra_headers or {})))
        return HttpReply(200, {"status": "ok"}, {"server": "llama.cpp"})
    m._http = llama  # type: ignore[method-assign]
    Spy(m, root=root, listening=True)
    m._unload = EndpointManager._unload.__get__(m)  # type: ignore[method-assign]
    m._http = llama  # type: ignore[method-assign]
    run_stop(m)
    unloads = [h for _m, p, h in seen if p == "/models/unload"]
    probes = [h for _m, p, h in seen if p == "/health"]
    check(unloads == [{"Authorization": "Bearer s3cret"}] and all(h == {} for h in probes),
          "an identified port gets the key with the unload, and never with the probe")

    # no unload call: the engine can still say a reply is running
    class Busy(FakeLlama):
        busy: bool | None = True

        def is_busy(self, http):
            if self.busy == "boom":
                raise RuntimeError("boom")
            return self.busy

    root = llama_tree()
    eng = Busy()
    m = lmgr(eng)
    spy = Spy(m, root=root, listening=True)
    m._unload = EndpointManager._unload.__get__(m)  # type: ignore[method-assign]
    m._http = llama  # type: ignore[method-assign]
    res = run_stop(m)
    check(res["error"] == "busy" and res["unload"] == "busy" and not spy.killed,
          "no unload call, engine busy: a normal stop answers busy and kills nothing")
    res = run_stop(m, force=True)
    check(res["ok"] and len(spy.killed) == 1, "force still stops it")
    for value, why in ((False, "idle"), (None, "cannot tell"), ("boom", "the check crashes")):
        eng.busy = value  # type: ignore[assignment]
        spy = Spy(m, root=root, listening=True)
        m._unload = EndpointManager._unload.__get__(m)  # type: ignore[method-assign]
        m._http = llama  # type: ignore[method-assign]
        res = run_stop(m)
        check(res["ok"] and res["unload"] == "unsupported" and len(spy.killed) == 1, f"{why}: the stop goes ahead")
    check(Engine().is_busy(http_of({})) is None and StrataEngine().is_busy(http_of({})) is None,
          "engines cannot tell unless they implement it (Strata's unload call answers busy itself)")

    # a buggy claims_port in the fallback scan is "no", not a crash
    class BadClaims(FakeLlama):
        def claims_port(self, cmdline, port, cfg):
            raise RuntimeError("boom")
    import psutil
    m = lmgr(BadClaims())
    m._listener_pids = lambda: []  # type: ignore[method-assign]
    real_iter = psutil.process_iter
    psutil.process_iter = lambda *a, **k: iter(list(FakeProc.table.values()))  # type: ignore[assignment]
    try:
        r, foreign = m._locate_root(fallback=True)
    finally:
        psutil.process_iter = real_iter  # type: ignore[assignment]
    check(r is None and foreign is None, "a port check that crashes claims nothing")

    # registry discovery: a module dropped into engines/ is found without editing the registry
    import importlib
    pkg_dir = Path(engines.__file__).parent
    mod = pkg_dir / "zz_test_only_engine.py"
    mod.write_text("from .base import Engine\n\n\nclass ZzEngine(Engine):\n    id = 'zz-test-only'\n    name = 'Zz'\n",
                   encoding="utf-8")
    try:
        importlib.invalidate_caches()
        engines._discovered = False
        check("zz-test-only" in engines.available() and engines.get_engine({"backend": "ZZ-Test-Only"}).name == "Zz",
              "an engine module dropped into engines/ is registered by its id")
        check(isinstance(engines.get_engine({}), StrataEngine), "the default stays Strata")
    finally:
        mod.unlink(missing_ok=True)
        engines._FACTORIES.pop("zz-test-only", None)
        sys.modules.pop("engines.zz_test_only_engine", None)
        engines._discovered = False
    bad = pkg_dir / "zz_broken_engine.py"
    bad.write_text("raise RuntimeError('broken engine module')\n", encoding="utf-8")
    try:
        importlib.invalidate_caches()
        engines._discovered = False
        check("strata" in engines.available() and isinstance(engines.get_engine({"backend": "nope"}), StrataEngine),
              "a broken engine module is skipped; the app still gets Strata")
    finally:
        bad.unlink(missing_ok=True)
        sys.modules.pop("engines.zz_broken_engine", None)
        engines._discovered = False
    e = Engine()
    check(e.is_configured({"launch_cmd": ["x.exe"]}) and not e.is_configured({}) and not e.is_configured({"launch_cmd": []}),
          "configured = a launch command is set")
    check(StrataEngine().low_ram_serving == "Reading from SSD (slower)" and Engine.low_ram_label == "Low-RAM mode",
          "the Low-RAM wording is the engine's")


def t_command_launch() -> None:
    """Engines without launcher files: Settings saves program + arguments as a CommandLaunch."""
    sa, ja = base.split_args, base.join_args
    check(sa('-m "C:\\My Models\\a.gguf" --port 9931  -c 8192') == ["-m", "C:\\My Models\\a.gguf", "--port", "9931", "-c", "8192"],
          "arguments: spaces separate, double quotes group")
    check(sa("") == [] and sa("   ") == [] and sa('""') == [""] and sa('a"b c"d') == ["ab cd"], "arguments: edge cases")
    for args in (["-m", "C:\\My Models\\a.gguf", "--alias", ""], [], ["--port", "1"]):
        check(sa(ja(args)) == args, f"what Settings shows splits back to the same arguments ({args})")
    info = base.CommandLaunch(program="C:\\llama\\llama-server.exe", args=("-m", "x.gguf"))
    check(info.path == info.program and info.file_name == "llama-server.exe" and info.model_name == "llama-server"
          and info.summary() == "llama-server.exe · 2 arguments", "a CommandLaunch offers what every launcher info offers")
    check(base.CommandLaunch("C:\\a.exe", ("x",)).summary() == "a.exe · 1 argument", "singular")
    e = FakeLlama()
    cfg: dict = {"port": 9931, "launch_cmd": ["old.exe"], "strata_dir": "C:\\keep"}
    check(not e.same_launch(cfg, info), "a different command is not the same launch")
    e.apply_launcher(cfg, info)
    check(cfg["launch_cmd"] == ["C:\\llama\\llama-server.exe", "-m", "x.gguf"] and cfg["launch_cwd"] == "C:\\llama"
          and cfg["port"] == 9931 and cfg["strata_dir"] == "C:\\keep",
          "applying it sets launch_cmd and launch_cwd and leaves every other key alone")
    check(e.same_launch(cfg, info), "...after which it is the same launch")
    check(not e.same_launch(cfg, object()) and e.apply_launcher(cfg, object()) is None and cfg["launch_cmd"][0].endswith(".exe"),
          "anything that is not a CommandLaunch is ignored by the defaults")
    s = StrataEngine()
    check(s.launcher_kind == "run-*.bat" and s.has_dashboard and ("Batch files", "*.bat *.cmd") in s.launcher_filetypes,
          "Strata keeps its launcher wording and its dashboard link")
    check(Engine.has_dashboard is False and Engine.launcher_kind == "launcher file", "neutral defaults for other engines")


def t_base_defaults() -> None:
    e = Engine()
    check(e.capabilities == frozenset() and not e.can(base.CAP_UNLOAD), "the base engine has no capabilities")
    check(e.unload(http_of({}), True) == "unsupported" and e.nudge_load(http_of({})) is None, "no unload, no nudge")
    check(e.owns_child(None, {}) is False and e.launcher_process(None, {}) is None, "owns nothing, has no launcher")  # type: ignore[arg-type]
    check(e.find_installs() == [], "finds nothing")
    p = e.metrics_poller()
    check(p.poll("http://x") is None and p.reset() is None, "its metrics poller reports nothing")
    for meth, args in (("is_server_process", ("a", [])), ("verified_by_process", (None, {})), ("claims_port", ([], 1, {})),
                       ("probe", (http_of({}), 1)), ("plan_launch", ({},)), ("protects_process", ([], "", {}))):
        try:
            getattr(e, meth)(*args)
            ok = False
        except NotImplementedError:
            ok = True
        check(ok, f"a new engine must implement {meth}")
    r = HttpReply(200, None, {"server": "llama.cpp"})
    check(r.header("Server") == "llama.cpp" and r.header("x-none") == "", "reply headers are read case-insensitively")
    check(base.explicit_port(["x", "--port", "81"]) == 81 and base.explicit_port(["x", "--port=82"]) == 82
          and base.explicit_port(["x"]) is None and base.explicit_port(["x", "--port", "abc"]) is None, "explicit_port")
    check(base.norm_name("Chrome.EXE") == "chrome" and base.dir_prefix("") == "" and not base.path_under("", "c:\\x\\"),
          "path helpers")


def main() -> int:
    for t in (t_registry, t_strata_probe, t_strata_identity, t_strata_launch, t_sweep_protection, t_other_engine_stop,
              t_engine_cannot_widen, t_api_key_headers, t_review_fixes, t_command_launch, t_base_defaults):
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
