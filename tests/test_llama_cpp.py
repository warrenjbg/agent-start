"""engines/llama_cpp.py: llama.cpp's llama-server (single-model mode) as an engine.

Pure: every process is a fake, every HTTP reply is a fake, the only files are empty stand-ins in a temp folder (a
zero-byte "llama-server.exe" that is never run, a zero-byte "model.gguf", metadata-only GGUF files and a zero-byte "llama-fit-params.exe"). Nothing is started, inspected,
signalled or contacted: sockets, subprocess and psutil's process functions are replaced by guards that fail the
test if anything reaches them.

The supervisor-level part drives the REAL EndpointManager (its _locate_root / _topmost_engine / _kill_targets /
_verified_engine / _http_id / _do_stop / _adopt) with the fakes from tests/test_kill_path.py and tests/test_engines.py.

Run: .venv\\Scripts\\python.exe tests\\test_llama_cpp.py
"""
from __future__ import annotations

import ast
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("AGENT_START_HOME", tempfile.mkdtemp(prefix="agentstart-llama-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil  # noqa: E402

import engines  # noqa: E402
from engines import base  # noqa: E402
from engines.base import CommandLaunch, HttpReply, LaunchError, LaunchPlan, LiveSample, ServiceState  # noqa: E402
from engines.llama_cpp import (  # noqa: E402
    EVAL_RE,
    PROMPT_RE,
    LlamaCppEngine,
    LlamaCppPoller,
    LogRequests,
    effective_port,
    live_from_slots,
)
from engines.strata import StrataEngine  # noqa: E402
from server_manager import EndpointManager  # noqa: E402
from test_engines import http_of, lmgr  # noqa: E402
from test_kill_path import PORT, FakeProc, Spy, reset, run_stop  # noqa: E402

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


# ------------------------------------------------------------------------------------------------ guards
def _forbidden(what: str):
    def _f(*_a, **_k):
        raise AssertionError(f"{what} used in a pure test")
    return _f


def install_guards() -> None:
    """Nothing in this file may reach a socket, start a process or touch a real one."""
    socket.socket.connect = _forbidden("socket.connect")  # type: ignore[method-assign]
    socket.socket.connect_ex = _forbidden("socket.connect_ex")  # type: ignore[method-assign]
    subprocess.Popen = _forbidden("subprocess.Popen")  # type: ignore[misc, assignment]
    psutil.net_connections = _forbidden("psutil.net_connections")  # type: ignore[assignment]
    psutil.wait_procs = _forbidden("psutil.wait_procs")  # type: ignore[assignment]
    psutil.virtual_memory = lambda: SimpleNamespace(available=8 * 1024 ** 3)  # type: ignore[assignment]
    psutil.Process = lambda pid=None: FakeProc.table[pid]  # type: ignore[assignment, misc]
    psutil.process_iter = lambda *a, **k: iter(list(FakeProc.table.values()))  # type: ignore[assignment]


# ------------------------------------------------------------------------------------------------ fixtures
LL = r"C:\AI\llama"                                     # the configured install (does not exist on disk)
PROG = LL + r"\llama-server.exe"
MODEL = r"C:\AI\models\qwen.gguf"
OTHER = r"C:\Downloads\llama-b1\llama-server.exe"       # a second llama.cpp copy somewhere else
S = r"C:\AI\Strata"
PY = S + r"\.venv\Scripts\python.exe"
ENG = LlamaCppEngine()


def cfg_of(**kw) -> dict:
    c = {"backend": "llama_cpp", "launch_cmd": [PROG, "-m", MODEL, "--port", str(PORT)], "launch_cwd": LL,
         "port": PORT, "endpoint_url": f"http://127.0.0.1:{PORT}/v1", "user_whitelist": []}
    c.update(kw)
    return c


def strata_cfg() -> dict:
    """A Strata user's config.json as Agent Start writes it (launcher_bat parsed into launch_cmd)."""
    return {"backend": "strata", "model_name": "swift-1.5-iq3_s", "strata_dir": S,
            "launcher_bat": S + r"\run-swift.bat",
            "launch_cmd": [PY, S + r"\serve\server.py", "--engine", "strata", "--config",
                           S + r"\strata-swift.json", "--port", "8080"],
            "launch_cwd": S, "endpoint_url": "http://127.0.0.1:8080/v1", "port": 8080, "user_whitelist": []}


def srv(pid, ppid=None, exe=PROG, port=PORT, model=True, extra=(), name="llama-server.exe", ctime=None) -> FakeProc:
    cmd = [exe] + (["-m", MODEL] if model else []) + (["--port", str(port)] if port is not None else []) + list(extra)
    return FakeProc(pid, name, cmd, exe, ppid, ctime=ctime)


def LLAMA(code=200, body=None, server="llama.cpp") -> HttpReply:
    return HttpReply(code, body, {"server": server} if server else {})


READY = LLAMA(200, {"status": "ok"})
LOADING = LLAMA(503, {"error": {"message": "Loading model", "type": "unavailable_error", "code": 503}})
IDLE_SLOTS = [{"id": 0, "is_processing": False}, {"id": 1, "is_processing": False}]


def busy_slots(n_decoded=12):
    return [{"id": 0, "is_processing": True, "id_task": 7, "n_prompt_tokens_processed": 900,
             "next_token": [{"has_next_token": True, "n_remain": -1, "n_decoded": n_decoded}]},
            {"id": 1, "is_processing": False}]


def err(cfg) -> str:
    try:
        ENG.plan_launch(cfg)
    except LaunchError as exc:
        return str(exc)
    return ""


# ------------------------------------------------------------------------------------------------ identity
def t_identity() -> None:
    e = ENG
    good = [PROG, "-m", MODEL, "--port", "9931"]
    check(e.is_server_process("llama-server.exe", good), "llama-server.exe with a model is the server")
    check(e.is_server_process("LLAMA-SERVER.EXE", good), "the image name is compared case-insensitively")
    check(e.is_server_process("llama-server.exe", [PROG, "-hf", "ggml-org/x-GGUF:Q4_K_M"]), "-hf names a model too")
    check(e.is_server_process("llama-server.exe", [PROG, "--hf_repo", "u/m"]), "--hf_repo (llama.cpp reads _ as -)")
    check(e.is_server_process("llama-server.exe", [PROG, "-dr", "gemma3"]), "-dr (docker repo) names a model")
    for nm, cmd, why in (
            ("llama-server.exe", [PROG, "--port", "9931"], "no model = router mode: not this engine's server"),
            ("llama-server.exe", [PROG, "--models-dir", r"C:\models"], "a router with a models folder"),
            ("llama-server.exe", [PROG, "--port", "9931", "-m"], "-m as the last token (no value)"),
            ("llama-server.exe", [PROG, "-m", ""], "-m with an empty value"),
            ("llama-server.exe", [MODEL, "-m"], "the model path as argv[0] does not count"),
            ("my-llama-server.exe", good, "my-llama-server.exe"),
            ("llama-server.exe.bak", good, "llama-server.exe.bak"),
            ("llama-server", good, "no .exe (not a Windows image name)"),
            ("python.exe", ["python", r"C:\x\llama-server.py", "-m", MODEL], "python running llama-server.py"),
            ("cmd.exe", ["cmd", "/c", PROG, "-m", MODEL], "a shell that merely mentions llama-server.exe"),
            ("Code.exe", ["Code.exe", PROG], "an editor with the exe open"),
            ("llama.exe", ["llama", "serve", "-m", MODEL], "`llama.exe serve` (llama.app) is not counted"),
            ("python.exe", strata_cfg()["launch_cmd"], "Strata's server is not llama-server"),
            (None, good, "unknown name"), ("llama-server.exe", None, "unreadable command line")):
        check(not e.is_server_process(nm, cmd), f"not the server: {why}")

    cfg = cfg_of()
    reset()
    mine = srv(1)
    elsewhere = srv(2, exe=OTHER)
    other_port = srv(3, port=7000)
    twice = FakeProc(4, "llama-server.exe", [PROG, "-m", MODEL, "--port", str(PORT), "--port", "7000"], PROG)
    twice_ok = FakeProc(5, "llama-server.exe", [PROG, "-m", MODEL, "--port", "7000", "--port", str(PORT)], PROG)
    spelled = FakeProc(6, "llama-server.exe", [PROG, "-m", MODEL, "--port", str(PORT)],
                       (LL + r"\sub\..\LLAMA-SERVER.EXE").replace("\\", "/"))
    router = srv(7, model=False)
    eq = FakeProc(8, "llama-server.exe", [PROG, "-m", MODEL, f"--port={PORT}"], PROG)
    denied = srv(9)
    denied.deny = ("exe",)
    check(e.verified_by_process(mine, cfg), "verified: the configured exe serving the configured port")
    check(e.verified_by_process(spelled, cfg), "the same file spelled differently (case, / and ..) is the same exe")
    check(not e.verified_by_process(elsewhere, cfg), "not verified: llama-server.exe in another folder")
    check(not e.verified_by_process(other_port, cfg), "not verified: the configured exe on another port")
    check(not e.verified_by_process(twice, cfg) and e.verified_by_process(twice_ok, cfg),
          "--port given twice: the LAST one counts, as in llama.cpp")
    check(not e.verified_by_process(router, cfg), "not verified: a router (no model)")
    check(not e.verified_by_process(eq, cfg), "not verified: --port=N (llama-server rejects that form)")
    check(not e.verified_by_process(denied, cfg), "not verified: the exe path cannot be read")
    check(not e.verified_by_process(mine, cfg_of(launch_cmd=[])), "no program configured: process facts prove nothing")
    check(not e.verified_by_process(mine, cfg_of(launch_cmd=["llama-server.exe", "-m", MODEL])),
          "a relative program path proves nothing")
    check(not e.verified_by_process(mine, cfg_of(port="junk")), "no usable configured port: nothing verified")
    check(not e.verified_by_process(mine, strata_cfg()), "a Strata config verifies no llama-server")

    for cmd, port, want, why in (
            ([PROG, "-m", MODEL, "--port", str(PORT)], PORT, True, "--port N"),
            ([PROG, "-m", MODEL, "--port", "7000"], PORT, False, "another port"),
            ([PROG, "-m", MODEL, f"--port={PORT}"], PORT, False, "--port=N is not accepted by llama-server"),
            ([PROG, "-m", MODEL, "--port", "7000", "--port", str(PORT)], PORT, True, "twice: the last one"),
            ([PROG, "-m", MODEL, "--port", str(PORT), "--port", "7000"], PORT, False, "twice: not the first one"),
            ([PROG, "-m", MODEL, "--port"], PORT, False, "--port as the last token, no value"),
            ([PROG, "-m", MODEL, "--port", "abc"], PORT, False, "a non-numeric port"),
            ([PROG, "-m", MODEL, "--port", "99999"], 99999, False, "out of range"),
            ([PROG, "-m", MODEL], 9931, False, "no --port: no claim even for the default 9931"),
            ([PROG, "-p", str(PORT)], PORT, False, "-p is --prompt, not a port"),
            ([], PORT, False, "empty command line")):
        check(e.claims_port(cmd, port, cfg) is want, f"claims_port: {why}")
    check(effective_port(["--port", "1", "--port", "2"]) == 2 and effective_port(["--port", " 81 "]) == 81,
          "effective_port: last wins, spaces trimmed")


def t_probe() -> None:
    e = ENG
    st = e.probe(http_of({("GET", "/health"): READY}), 1)
    check(st == ServiceState(True, True, True, None), f"ready: answered, identified, loaded ({st})")
    st = e.probe(http_of({("GET", "/health"): LOADING}), 1)
    check(st == ServiceState(True, True, False, None), f"loading (503 + header): identified, not loaded ({st})")
    for reply, why in (
            (HttpReply(200, {"status": "ok"}), "a 200 {status: ok} WITHOUT the Server header"),
            (LLAMA(200, {"status": "ok"}, server="nginx"), "Server: nginx"),
            (LLAMA(200, {"status": "ok"}, server="llama.cpp-fork"), "a lookalike header value"),
            (LLAMA(200, None), "an HTML page (no JSON) with the header"),
            (LLAMA(200, {"status": "error"}), "200 with another status"),
            (LLAMA(401, {"error": {"message": "Invalid API Key", "type": "authentication_error"}}), "401"),
            (LLAMA(404, {"error": {"message": "File Not Found"}}), "404 (--api-prefix moved /health)"),
            (LLAMA(503, None), "503 without the loading body"),
            (HttpReply(503, {"error": {"message": "Loading model"}}), "the loading body without the header")):
        st = e.probe(http_of({("GET", "/health"): reply}), 1)
        check(st.answered and not st.is_engine and st.loaded is None, f"not identified: {why}")
    check(e.probe(http_of({}), 1) == base.NO_ANSWER, "no answer (timeout / refused) = NO_ANSWER")

    def boom(*_a):
        raise OSError("reset")
    check(e.probe(boom, 1) == base.NO_ANSWER, "an http callable that raises = NO_ANSWER")
    h = http_of({("GET", "/health"): READY})
    e.probe(h, 1)
    check(h.calls == [("GET", "/health")], "the probe asks GET /health only")
    h = http_of({("GET", "/v1/models"): LLAMA(200, {"object": "list", "data": [{"id": "qwen", "owned_by": "llamacpp"}]})})
    check(e.fetch_model_id(h, ServiceState(True, True, True)) == "qwen", "model id from /v1/models")


def t_children_and_sweep() -> None:
    e = ENG
    cfg = cfg_of()
    reset()
    conn = FakeProc(1, "llama-connect.exe", [LL + r"\llama-connect.exe"], LL + r"\llama-connect.exe")
    conn_else = FakeProc(2, "llama-connect.exe", [r"C:\x\llama-connect.exe"], r"C:\x\llama-connect.exe")
    cli = FakeProc(3, "llama-cli.exe", [LL + r"\llama-cli.exe"], LL + r"\llama-cli.exe")
    node = FakeProc(4, "node.exe", ["node", "mcp.js"], r"C:\Program Files\nodejs\node.exe")
    denied = FakeProc(5, "llama-connect.exe", [], LL + r"\llama-connect.exe")
    denied.deny = ("exe",)
    check(e.owns_child(conn, cfg), "owns llama-connect.exe from the program's folder (--connect)")
    check(not e.owns_child(conn_else, cfg), "not llama-connect.exe from another folder")
    conn_sub = FakeProc(6, "llama-connect.exe", [LL + r"\sub\llama-connect.exe"], LL + r"\sub\llama-connect.exe")
    check(not e.owns_child(conn_sub, cfg), "not llama-connect.exe from a subfolder of the program's folder")
    from engines.llama_cpp import configured_program
    check(configured_program({"launch_cmd": [r"llama b1\llama-server.exe", "-m", MODEL]}) is None
          and configured_program({"launch_cmd": ["llama-server.exe"]}) is None
          and configured_program(cfg) == PROG, "configured_program: only an absolute path counts")
    check(not e.owns_child(cli, cfg), "not other llama.cpp tools in the folder")
    check(not e.owns_child(node, cfg), "not an MCP tool server the user configured")
    check(not e.owns_child(denied, cfg) and not e.owns_child(conn, {}), "unknown exe / no program: no")
    check(e.launcher_process(srv(9), cfg) is None, "no launcher parent: a terminal that started it is never ended")

    p = e.protects_process
    check(p([PROG], PROG, cfg) and p(None, OTHER, cfg) and p([OTHER, "-m", "x"], None, cfg),
          "sweep: any llama-server.exe is protected (exe or argv[0])")
    check(p(["x"], LL + r"\ggml-vulkan-helper.exe", cfg), "sweep: anything under the program's folder")
    check(not p(["x"], r"C:\AI\llamaX\x.exe", cfg) and not p(["chrome"], r"C:\Chrome\chrome.exe", cfg),
          "sweep: a sibling folder with the same prefix / an unrelated app are not protected")
    check(not p(None, None, cfg) and not p([], "", {}), "sweep: nothing known = not protected")
    root_cfg = cfg_of(launch_cmd=[r"C:\llama-server.exe", "-m", MODEL, "--port", str(PORT)])
    check(p(None, r"C:\llama-server.exe", root_cfg) and not p(["x"], r"C:\Tools\x.exe", root_cfg)
          and not p(["x"], r"C:\x.exe", root_cfg), "program in a drive root: only the program itself, not the drive")
    check(not p([PY, S + r"\serve\server.py"], PY, cfg), "Strata's processes are not llama.cpp's")


# ------------------------------------------------------------------------------------------------ launch
def t_launch(tmp: Path) -> None:
    e = ENG
    d = tmp / "llama b1"
    d.mkdir()
    exe = d / "llama-server.exe"
    exe.write_bytes(b"")                     # a stand-in file: never executed
    model = tmp / "model.gguf"
    model.write_bytes(b"")
    fake_py = d / "python.exe"
    fake_py.write_bytes(b"")
    (d / "start.bat").write_bytes(b"")

    def c(*args, **kw):
        out = cfg_of(launch_cmd=[str(exe), *args], launch_cwd=str(d), port=9931)
        out.update(kw)
        return out

    ok_args = ("-m", str(model), "--port", "9931")
    plan = e.plan_launch(c(*ok_args, "-c", "8192", "-ngl", "99"))
    check(isinstance(plan, LaunchPlan) and plan.cmd == [str(exe), *ok_args, "-c", "8192", "-ngl", "99"]
          and plan.cwd == str(d) and plan.env == {}, "a valid command is launched as typed, from its folder")
    plan = e.plan_launch(cfg_of(launch_cmd=[str(exe), *ok_args], launch_cwd=str(tmp / "gone"), port=9931))
    check(plan.cwd == str(d), "a launch_cwd that does not exist falls back to the program's folder")
    plan = e.plan_launch(c("-m", "model.gguf", "--port", "9931", launch_cwd=str(tmp)))
    check(plan.cwd == str(tmp), "a relative -m is checked against the working folder")
    check(e.plan_launch(c("-hf", "ggml-org/gemma-3-1b-it-GGUF", "--port", "9931")).cmd[1] == "-hf",
          "-hf needs no local file")
    check(e.plan_launch(c(*ok_args, "--host", "0.0.0.0")).cmd[-1] == "0.0.0.0", "--host 0.0.0.0 includes loopback")
    check(e.plan_launch(c(*ok_args, "--host", "127.0.0.1,192.168.1.5")) is not None, "a host list with loopback")

    cases = [
        ({"launch_cmd": []}, "No llama-server chosen yet"),
        ({}, "No llama-server chosen yet"),
        (strata_cfg(), "The program must be llama-server.exe, not python.exe"),
        (cfg_of(launch_cmd=[str(fake_py), "-m", str(model), "--port", "9931"]), "must be llama-server.exe"),
        (cfg_of(launch_cmd=[str(d / "start.bat")]), "must be llama-server.exe, not start.bat"),
        (cfg_of(launch_cmd=[str(d / "llama-server.exe.bak")]), "must be llama-server.exe"),
        (cfg_of(launch_cmd=[str(d / "my-llama-server.exe")]), "must be llama-server.exe"),
        (cfg_of(launch_cmd=[str(d / "llama.exe"), "serve"]), "must be llama-server.exe, not llama.exe"),
        (cfg_of(launch_cmd=["llama-server.exe", "-m", str(model)]), "with its full path"),
        (cfg_of(launch_cmd=[str(tmp / "nope" / "llama-server.exe")]), "llama-server.exe not found"),
        (c("-m", str(model)), "Add --port 9931 to the arguments"),
        (c("-m", str(model), "--port=9931"), "Write the port as --port 9931"),
        (c("-m", str(model), "--port", "9931", "--port", "9932"), "--port is given more than once"),
        (c("-m", str(model), "--port"), "--port needs a number"),
        (c("-m", str(model), "--port", "abc"), "--port needs a number"),
        (c("-m", str(model), "--port", "0"), "--port needs a number"),
        (c("-m", str(model), "--port", "9931", port=8080), "does not match the port in config.json (8080)"),
        (c("--port", "9931"), "router mode"),
        (c("--port", "9931", "--models-dir", str(tmp)), "router mode"),
        (c("--port", "9931", "-m"), "-m needs a model file"),
        (c("-m", str(tmp / "missing.gguf"), "--port", "9931"), "Model file not found"),
        (c(*ok_args, "--host", "192.168.1.5"), "use --host 127.0.0.1"),
        (c(*ok_args, "--host", "/tmp/llama.sock"), "use --host 127.0.0.1"),
        (c(*ok_args, "--api-prefix", "/v2"), "Remove --api-prefix"),
    ]
    for cfg, want in cases:
        got = err(cfg)
        check(want in got, f"refused: {want!r} ({got!r})")
    short = [err(cfg) for cfg, w in cases if "not found" not in w]       # those two end with the path itself
    check(all(len(m) <= 130 for m in short), f"every message is short (Settings shows it verbatim): "
          f"{max(short, key=len)!r}")

    # Settings flow: trial = dict(cfg); apply_launcher(trial, info); plan_launch(trial)
    old = strata_cfg()
    info = CommandLaunch(program=str(exe), args=("-m", str(model), "--port", "9931", "--api-key", "k1,k2"))
    trial = dict(old)
    e.apply_launcher(trial, info)
    check(trial["launch_cmd"] == info.argv and trial["launch_cwd"] == str(d), "apply: launch_cmd and launch_cwd")
    check(trial["port"] == 9931 and trial["endpoint_url"] == "http://127.0.0.1:9931/v1",
          "apply: port and endpoint_url from --port")
    check(trial["api_key"] == "k1", "apply: the first --api-key is kept for /slots and /v1/models")
    untouched = ("strata_dir", "launcher_bat", "model_name", "backend", "user_whitelist")
    check(all(trial[k] == old[k] for k in untouched), "apply: every unrelated key is left alone")
    check(e.plan_launch(trial).cmd == info.argv, "the applied settings pass plan_launch (Save is offered)")
    check(e.same_launch(trial, info) and not e.same_launch(old, info), "same_launch after applying, not before")
    check(not e.same_launch(dict(trial, endpoint_url="http://127.0.0.1:8080/v1"), info),
          "same_launch is false while endpoint_url still points elsewhere (Save fixes it)")
    no_port = CommandLaunch(program=str(exe), args=("-m", str(model)))
    t2 = dict(old)
    e.apply_launcher(t2, no_port)
    check(t2["port"] == 8080 and t2["endpoint_url"] == old["endpoint_url"] and "api_key" not in t2,
          "apply without --port leaves port and endpoint_url as they were")
    check("Add --port 9931" in err(t2), "...and plan_launch then asks for --port")
    for args in (("--port", "9931", "--port", "9932"), ("--port=9931",), ("--port", "x")):
        t3 = dict(old)
        e.apply_launcher(t3, CommandLaunch(str(exe), ("-m", str(model), *args)))
        check(t3["port"] == 8080, f"apply: an unusable --port {args} does not change the port")
    t4 = dict(old)
    e.apply_launcher(t4, object())
    check(t4 == old, "apply: anything that is not a CommandLaunch is ignored")
    check(not e.is_configured(old) and e.is_configured(trial) and not e.is_configured({}),
          "is_configured: a Strata launch command left in config.json does not count")


def t_busy_and_headers() -> None:
    e = ENG
    check(e.is_busy(http_of({("GET", "/slots"): LLAMA(200, busy_slots())})) is True, "a processing slot = busy")
    check(e.is_busy(http_of({("GET", "/slots"): LLAMA(200, IDLE_SLOTS)})) is False, "every slot idle = not busy")
    for reply, why in (
            (LLAMA(501, {"error": {"message": "This server does not support slots endpoint."}}), "--no-slots (501)"),
            (LLAMA(401, {"error": {"message": "Invalid API Key"}}), "no key (401)"),
            (HttpReply(), "no answer"),
            (HttpReply(200, busy_slots()), "a reply without the Server header"),
            (LLAMA(200, None), "an HTML page"),
            (LLAMA(200, []), "an empty list"),
            (LLAMA(200, {"slots": []}), "not a list")):
        check(e.is_busy(http_of({("GET", "/slots"): reply})) is None, f"cannot tell: {why}")

    def boom(*_a):
        raise TimeoutError
    check(e.is_busy(boom) is None, "cannot tell: the call raises")
    check(e.request_headers({"api_key": "s3cret"}) == {"Authorization": "Bearer s3cret"}, "api_key -> Bearer header")
    for v in (None, "", "  ", 7, ["k"], "bad\r\nX-Evil: 1"):
        check(e.request_headers({"api_key": v}) == {}, f"no header for api_key {v!r}")
    check(e.request_headers({}) == {}, "no api_key, no header")
    check(e.idle_grace_s == 60.0, "idle wait 60 s (tool-call endings are not visible)")


# ------------------------------------------------------------------------------------------------ logs
def slt(func: str, slot: int, task: int, msg: str) -> str:
    """tools/server/server-common.h SLT_INF prefix: "slot %12.*s: id %2d | task %d | " + msg."""
    return ("slot %12.*s: id %2d | task %d | " % (12, func, slot, task)) + msg


def srvl(func: str, msg: str) -> str:
    """tools/server/server-common.h SRV_INF prefix: "srv  %12.*s: " + msg."""
    return ("srv  %12.*s: " % (12, func)) + msg


def timing(slot: int, task: int, p_ms: float, p_n: int, g_ms: float, g_n: int) -> list[str]:
    """The three lines of tools/server/server-context.cpp server_slot::print_timings (~L763-L773)."""
    def per(ms, n):
        return (ms / n, 1000.0 * n / ms) if n else (float("nan"), float("nan"))
    pt, ps = per(p_ms, p_n)
    gt, gs = per(g_ms, g_n)
    return [
        slt("print_timings", slot, task, "prompt eval time = %10.2f ms / %5d tokens (%8.2f ms per token, %8.2f tokens "
            "per second)" % (p_ms, p_n, pt, ps)),
        slt("print_timings", slot, task, "       eval time = %10.2f ms / %5d tokens (%8.2f ms per token, %8.2f tokens "
            "per second)" % (g_ms, g_n, gt, gs)),
        slt("print_timings", slot, task, "      total time = %10.2f ms / %5d tokens" % (p_ms + g_ms, p_n + g_n)),
    ]


def realistic_log() -> list[str]:
    lines = [
        "main: n_parallel is set to auto, using n_parallel = 4 and kv_unified = true",
        srvl("load_model", r"loading model 'C:\AI\models\qwen.gguf'"),
        "llama_model_loader: loaded meta data with 40 key-value pairs and 579 tensors from qwen.gguf (version GGUF V3)",
        "load_tensors: loading model tensors, this can take a while... (load_mode = mmap)",
        "load_tensors:        CUDA0 model buffer size =  4683.75 MiB",
        "llama_kv_cache:      CUDA0 KV buffer size =   512.00 MiB",
        srvl("server_run", "model loaded"),
        srvl("server_on_li", "listening on http://127.0.0.1:9931"),
        slt("launch_slot_", 0, 0, "processing task, is_child = 0"),
        slt("launch_slot_", 1, 3, "processing task, is_child = 0"),
        slt("print_timings_pp", 0, 0, "prompt processing, n_tokens =   1024, progress = 0.50, t =   3.10 s / 330.32 "
            "tokens per second"),
    ]
    a = timing(0, 0, 812.33, 1532, 4210.10, 256)
    b = timing(1, 3, 20.5, 12, 300.0, 9)            # short: both speeds are dropped by the cleaning rules
    lines += [a[0], b[0], "W srv  log_server_r: request: POST /v1/chat/completions 127.0.0.1 200", a[1], a[2],
              slt("release", 0, 0, "stop processing: n_tokens = 1788, truncated = 0"), b[1], b[2]]
    return lines


def t_log_parsing(tmp: Path) -> None:
    lines = realistic_log()
    data = ("\n".join(lines) + "\n").encode("utf-8")
    lr = LogRequests(str(tmp / "llama-20261113-101500.log"))
    got = lr.feed(data, 0)
    check(len(got) == 2, f"two finished requests in a log with interleaved slots ({len(got)})")
    r0 = next((r for r in got if r.generated_tokens == 256), None)
    check(r0 is not None and r0.prompt_tokens == 1532 and abs((r0.prompt_tps or 0) - 1532 / 0.81233) < 0.01
          and abs((r0.decode_tps or 0) - 60.81) < 0.01, f"request 1: tokens and speeds ({r0})")
    r1 = next((r for r in got if r.generated_tokens == 9), None)
    check(r1 is not None and r1.prompt_tokens == 12 and r1.prompt_tps is None and r1.decode_tps is None,
          f"request 2 (12 prompt / 9 generated tokens): counted, speeds not plotted ({r1})")
    check(len({r.key for r in got}) == 2 and all(r.key.startswith("llama-20261113-101500.log#") for r in got),
          f"keys are unique and name the run ({[r.key for r in got]})")
    check(sum(1 for ln in lines if PROMPT_RE.search(ln)) == 2 and sum(1 for ln in lines if EVAL_RE.search(ln)) == 2,
          "a prompt eval line is never read as an eval line, nor the total time line")
    again = LogRequests(lr.path).feed(data, 0)
    check([r.key for r in again] == [r.key for r in got], "the same log read again gives the same keys")
    check(sorted(r.key for r in got) == [r.key for r in got], "keys of one run sort in finishing order")

    # partial last line, non-UTF-8 bytes, CRLF, prefixes, colours, JSONL
    path = tmp / "llama-run2.log"
    a = timing(0, 4, 1500.0, 3000, 2000.0, 120)
    blob = ("\r\n".join(lines[:3]) + "\r\n").encode() + b"\xff\xfe garbage \x80\x81\n" + (a[0] + "\n").encode()
    half = a[1].encode()[:30]
    path.write_bytes(blob + half)
    lr = LogRequests(str(path))
    check(lr.read() == [], "a half-written eval line is not parsed yet (and non-UTF-8 bytes do not break the read)")
    with open(path, "ab") as fh:
        fh.write(a[1].encode()[30:] + b"\n")
    got = lr.read()
    check(len(got) == 1 and got[0].generated_tokens == 120 and got[0].prompt_tokens == 3000,
          f"...it is parsed once the line is complete ({got})")
    check(lr.read() == [], "nothing new: nothing returned")
    b = timing(2, 9, 900.0, 600, 1000.0, 50)
    with open(path, "ab") as fh:
        fh.write(("0.01.234.567 I " + b[0] + "\n").encode())                     # --log-prefix --log-timestamps
        fh.write(("\x1b[32mI \x1b[0m" + b[1] + "\n").encode())                   # --log-colors on
    got = lr.read()
    check(len(got) == 1 and got[0].prompt_tokens == 600 and got[0].generated_tokens == 50,
          f"timestamp / level / colour prefixes are skipped ({got})")
    import json as _json
    c = timing(3, 11, 800.0, 400, 500.0, 40)
    with open(path, "ab") as fh:
        for ln in c[:2]:
            fh.write((_json.dumps({"type": "log", "time": 1, "level": "info", "msg": ln + "\n"}) + "\n").encode())
    got = lr.read()
    check(len(got) == 1 and got[0].generated_tokens == 40, f"--log-jsonl records are read ({got})")
    with open(path, "ab") as fh:
        fh.write((slt("print_timings", 0, 99, "       eval time =       0.00 ms /     0 tokens (     nan ms per "
                      "token,      nan tokens per second)") + "\n").encode())
        fh.write((slt("print_timings", 0, 98, "       eval time =       5.00 ms /    20 tokens (-nan(ind) ms per "
                      "token, -nan(ind) tokens per second)") + "\n").encode())
    got = lr.read()
    check(len(got) == 2 and all(r.decode_tps is None and r.prompt_tokens is None for r in got),
          "an eval line without its prompt line, and nan / -nan(ind) speeds: counted, nothing plotted")
    path.write_bytes(b"")
    check(lr.read() == [], "a log that was replaced (shorter) starts over without crashing")
    check(LogRequests(str(tmp / "missing.log")).read() == [] and LogRequests(str(tmp)).read() == [],
          "a missing file or a folder: nothing, no exception")


def t_poller(tmp: Path) -> None:
    path = tmp / "llama-poll.log"
    path.write_bytes(("\n".join(realistic_log()) + "\n").encode())
    p = ENG.metrics_poller(cfg_of())
    check(isinstance(p, LlamaCppPoller), "the engine's poller")
    p.reset(log_path=str(path))
    s = p.poll("http://127.0.0.1:1")
    check(s is not None and len(s.requests) == 2 and s.live is None and s.model is None,
          "without an http seam: the log's requests, no live state, no model, no network")
    with open(path, "ab") as fh:
        for i in range(40):
            fh.write(("\n".join(timing(i % 4, 100 + i, 900.0, 512, 1000.0, 64)) + "\n").encode())
    s = p.poll("http://127.0.0.1:1")
    check(s is not None and len(s.requests) == 30, f"a normal poll returns the newest 30 ({len(s.requests) if s else s})")
    s = p.poll("http://127.0.0.1:1", catch_up=True)
    check(s is not None and len(s.requests) == 42, "a catch-up poll returns all it kept")
    p.reset(log_path=None)
    s = p.poll("http://127.0.0.1:1")
    check(s is not None and s.requests == () and s.live is None, "an adopted server (no log): an empty sample")

    calls: list[str] = []
    replies = {"/slots": LLAMA(200, busy_slots()),
               "/v1/models": LLAMA(200, {"data": [{"id": "qwen.gguf", "owned_by": "llamacpp"}]})}

    def http(method, path_, timeout):
        calls.append(path_)
        return replies.get(path_, HttpReply())
    p = ENG.metrics_poller(cfg_of(), http=http, fetch_metrics=lambda *a, **k: None)   # a foreign seam is ignored
    p.reset(log_path=str(path))
    s = p.poll("http://127.0.0.1:1")
    check(s is not None and s.live == LiveSample(state="generating", generated=12.0) and s.model == "qwen",
          f"with the seam: generating from /slots, the model from /v1/models ({s.live if s else s})")
    p.poll("http://127.0.0.1:1")
    check(calls.count("/v1/models") == 1 and calls.count("/slots") == 2, f"the model is asked once ({calls})")
    replies["/slots"] = LLAMA(200, busy_slots(n_decoded=0))
    s = p.poll("x")
    check(s is not None and s.live == LiveSample(state="reading", prompt_read=900.0), "n_decoded 0 = reading the prompt")
    replies["/slots"] = LLAMA(200, IDLE_SLOTS)
    s = p.poll("x")
    check(s is not None and s.live == LiveSample(state="idle"), "no slot processing = idle")
    replies["/slots"] = LLAMA(501, {"error": {"message": "not supported"}})
    s = p.poll("x")
    check(s is not None and s.live is None and len(s.requests) == 30, "--no-slots: no live state, the log still counts")
    replies["/slots"] = HttpReply(200, busy_slots())
    s = p.poll("x")
    check(s is not None and s.live is None, "a /slots reply without the Server header is not used")
    replies["/slots"] = HttpReply()
    check(p.poll("x") is None, "the port does not answer: a failed poll (None)")
    check(live_from_slots([{"is_processing": True}]) == LiveSample(state="generating"),
          "busy with an unreadable phase is still busy, never idle")
    check(live_from_slots(None) is None and live_from_slots([1, 2]) is None, "garbage /slots: no live state")

    def boom(*_a):
        raise RuntimeError("boom")
    p = LlamaCppPoller(http=boom)
    p.reset(log_path=str(tmp))
    check(p.poll("x") is None, "a raising http seam and a folder as log: None, no exception")
    p = LlamaCppPoller()
    p._log = None  # type: ignore[assignment]   # an internal bug
    check(p.poll("x") is None, "poll never raises, even on an internal error")


def t_load_progress() -> None:
    e = ENG
    steps = [("", "Starting llama-server"),
             (srvl("load_model", "loading model 'x.gguf'"), "Loading model"),
             ("load_tensors: loading model tensors, this can take a while... (load_mode = mmap)",
              "Loading model weights"),
             ("llama_kv_cache:      CUDA0 KV buffer size =   512.00 MiB", "Setting up the context"),
             ("\x1b[32m" + srvl("server_run", "model loaded") + "\x1b[0m", "Model loaded")]
    text = ""
    labels = []
    for line, _want in steps:
        text += line + "\n"
        labels.append(e.load_progress(text, 30.0, 60.0)[0])
    check(labels == [w for _l, w in steps], f"phase labels follow the documented log lines ({labels})")
    check(e.load_progress("", 30.0, 60.0)[1] == 0.5 and e.load_progress("", 300.0, 60.0)[1] == 0.95
          and e.load_progress("", 3.0, None)[1] == 0.0, "the bar stays time-based (no percentage is invented)")
    check(e.load_progress("loaded meta data with 3 key-value pairs", 1, 10)[0] == "Starting llama-server",
          "'loaded meta data' is not 'model loaded'")


# ------------------------------------------------------------------------------------------------ supervisor
class Net:
    """The supervisor's _http (method, path, timeout, extra_headers) over a route table; records every call."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, path, timeout, extra_headers=None):
        self.calls.append((method, path, dict(extra_headers or {})))
        return self.routes.get(path, HttpReply())


def manager(listeners, net: Net, **cfg) -> tuple[EndpointManager, Spy]:
    m = lmgr(ENG, **cfg_of(**cfg))
    spy = Spy(m, listening=True)
    del m._locate_root                       # the REAL root search (Spy replaced it)
    m._listener_pids = lambda: list(listeners)  # type: ignore[method-assign]
    m._http = net  # type: ignore[method-assign]
    m._unload = EndpointManager._unload.__get__(m)  # type: ignore[method-assign]
    return m, spy


def world():
    """A hand-started llama-server (from a terminal), its helpers and some neighbours."""
    reset()
    FakeProc(30, "explorer.exe", ["explorer"], r"C:\Windows\explorer.exe", None, ctime=1)
    FakeProc(40, "cmd.exe", ["cmd.exe"], r"C:\Windows\System32\cmd.exe", 30, ctime=40)       # the user's terminal
    FakeProc(41, "conhost.exe", ["conhost"], r"C:\Windows\System32\conhost.exe", 40, ctime=41)
    root = srv(100, 40)
    FakeProc(101, "conhost.exe", ["conhost"], r"C:\Windows\System32\conhost.exe", 100)
    FakeProc(102, "llama-connect.exe", [LL + r"\llama-connect.exe"], LL + r"\llama-connect.exe", 100)
    FakeProc(103, "node.exe", ["node", "mcp-files.js"], r"C:\Program Files\nodejs\node.exe", 100)   # MCP server
    FakeProc(104, "keepme.exe", ["keepme"], r"C:\Apps\keepme.exe", 100)
    FakeProc(105, "my-llama-server.exe", [LL + r"\my-llama-server.exe"], LL + r"\my-llama-server.exe", 100)
    srv(200, None, exe=OTHER, port=7000)                                                       # another server
    FakeProc(201, "python.exe", [PY, S + r"\serve\server.py", "--port", "8080"], PY, None)     # a Strata
    return root


TREE = sorted(["llama-server.exe:100", "conhost.exe:101", "llama-connect.exe:102"])


def t_supervisor_stop() -> None:
    root = world()
    net = Net({"/health": READY, "/slots": LLAMA(200, IDLE_SLOTS)})
    m, spy = manager([100], net, user_whitelist=["keepme", "llama-server"])
    r, foreign = m._locate_root()
    check(r is root and foreign is None, "the listener is the root (the terminal above it is not an engine process)")
    res = run_stop(m)
    check(res["ok"] and res["unload"] == "unsupported" and spy.killed == [TREE],
          f"stop: exactly the verified server, its conhost and llama-connect ({spy.killed})")
    check(not any(n.split(":")[1] in ("30", "40", "41", "103", "104", "105", "200", "201") for k in spy.killed for n in k),
          "never the terminal that started it, an MCP tool server, a whitelisted app, a lookalike exe in the folder, "
          "another llama-server or Strata")
    check([p for _m, p, _h in net.calls] == ["/slots"], f"verified from process facts: only the busy check ({net.calls})")

    # busy: a normal stop answers busy and kills nothing; force stops
    world()
    net = Net({"/health": READY, "/slots": LLAMA(200, busy_slots())})
    m, spy = manager([100], net)
    res = run_stop(m)
    check(res["error"] == "busy" and res["unload"] == "busy" and not spy.killed, "a reply is running: busy, no kill")
    res = run_stop(m, force=True)
    check(res["ok"] and spy.killed == [TREE] and "/slots" not in [p for _m, p, _h in net.calls[1:]],
          "force: stopped without asking /slots again")
    world()
    net = Net({"/health": READY, "/slots": LLAMA(501, {"error": {"message": "no slots"}})})
    m, spy = manager([100], net)
    check(run_stop(m)["ok"] and spy.killed == [TREE], "/slots disabled: cannot tell, the stop goes ahead")

    # API key: never with the identity probe, only after the port identified itself
    world()
    net = Net({"/health": READY, "/slots": LLAMA(200, IDLE_SLOTS)})
    m, spy = manager([100], net, api_key="s3cret")
    run_stop(m)
    health = [h for _m, p, h in net.calls if p == "/health"]
    slots = [(i, h) for i, (_m, p, h) in enumerate(net.calls) if p == "/slots"]
    first_health = next(i for i, (_m, p, _h) in enumerate(net.calls) if p == "/health")
    check(health and all(h == {} for h in health), "the identity probe never carries the key")
    check(slots and slots[0][1] == {"Authorization": "Bearer s3cret"} and slots[0][0] > first_health,
          f"/slots carries it, after the probe identified the port ({net.calls})")
    world()
    nginx = {"server": "nginx"}
    net = Net({"/health": HttpReply(200, {"status": "ok"}, nginx), "/slots": HttpReply(200, busy_slots(), nginx)})
    m, spy = manager([100], net, api_key="s3cret")
    res = run_stop(m)
    check(all(h == {} for _m, _p, h in net.calls), "a port that does not identify itself never receives the key")
    check(res["ok"] and spy.killed == [TREE], "...the process verified by its own facts is still stopped")


def t_supervisor_lookalikes() -> None:
    # llama-server.exe from another folder on our port: only the port's own answer can identify it
    reset()
    FakeProc(40, "cmd.exe", ["cmd.exe"], r"C:\Windows\System32\cmd.exe", None, ctime=1)
    stray = srv(100, 40, exe=OTHER)
    for reply, why in ((HttpReply(200, {"status": "ok"}), "a 200 without the header"),
                       (LLAMA(200, {"status": "ok"}, server="nginx"), "Server: nginx"), (HttpReply(), "no answer")):
        net = Net({"/health": reply})
        m, spy = manager([100], net)
        res = run_stop(m, force=True)
        check(not res["ok"] and "not llama.cpp" in (res["error"] or "") and not spy.killed,
              f"unverified llama-server.exe elsewhere + {why}: refused even with force")
    net = Net({"/health": READY, "/slots": LLAMA(200, IDLE_SLOTS)})
    m, spy = manager([100], net)
    res = run_stop(m)
    check(res["ok"] and spy.killed == [["llama-server.exe:100"]] and stray.pid == 100,
          "an externally started llama-server that answers as llama.cpp is stopped (not its terminal)")

    for pid, name, cmd, exe, why in (
            (100, "my-llama-server.exe", [LL + r"\my-llama-server.exe", "-m", MODEL, "--port", str(PORT)],
             LL + r"\my-llama-server.exe", "my-llama-server.exe"),
            (100, "llama-server.exe.bak", [PROG + ".bak", "-m", MODEL, "--port", str(PORT)], PROG + ".bak",
             "llama-server.exe.bak"),
            (100, "python.exe", ["python", r"C:\x\llama-server.py", "--port", str(PORT)], r"C:\Py\python.exe",
             "python running llama-server.py"),
            (100, "python.exe", [PY, S + r"\serve\server.py", "--port", str(PORT)], PY, "Strata")):
        reset()
        FakeProc(pid, name, cmd, exe)
        net = Net({"/health": READY, "/slots": LLAMA(200, IDLE_SLOTS)})    # even a llama.cpp-looking answer
        m, spy = manager([pid], net)
        res = run_stop(m, force=True)
        check(not res["ok"] and (res["error"] or "").endswith("not llama.cpp") and not spy.killed,
              f"foreign listener ({why}): refused, nothing killed ({res['error']})")

    # router mode on our port: not this engine's server (refused); a router's child on our port: only the child
    reset()
    srv(100, None, model=False)
    m, spy = manager([100], Net({"/health": READY}))
    res = run_stop(m, force=True)
    check(not res["ok"] and not spy.killed, f"a router (no model) on the port is refused ({res['error']})")
    reset()
    srv(100, None, port=7000, model=False)                                    # the user's router, port 7000
    child = srv(101, 100, port=PORT)                                          # its child happens to hold our port
    srv(102, 100, port=50123)                                                 # another child
    m, spy = manager([101], Net({"/health": READY, "/slots": LLAMA(200, IDLE_SLOTS)}))
    r, _f = m._locate_root()
    res = run_stop(m)
    check(r is child and spy.killed == [["llama-server.exe:101"]],
          f"the root walk stops below a router: only the child on our port ({spy.killed})")

    # whitelist: the server itself is still a server; a protected app in its tree is not killed
    world()
    m, spy = manager([100], Net({"/slots": LLAMA(200, IDLE_SLOTS)}), user_whitelist=["llama-server", "conhost"])
    run_stop(m)
    check(spy.killed == [sorted(["llama-server.exe:100", "llama-connect.exe:102"])],
          f"a protected name is never killed unless it is the server itself ({spy.killed})")


def t_supervisor_hidden_pid_and_adopt() -> None:
    # listener PID hidden: the process scan finds the server by its own --port
    world()
    m, spy = manager([], Net({"/health": READY, "/slots": LLAMA(200, IDLE_SLOTS)}))
    r, _f = m._locate_root()
    check(r is not None and r.pid == 100, "hidden listener: found by its explicit --port")
    res = run_stop(m)
    check(res["ok"] and spy.killed == [TREE], "...and stopped")
    reset()
    srv(200, None, exe=OTHER, port=7000)
    srv(201, None, port=None)                                                 # no --port: build default, no claim
    m, spy = manager([], Net({}))
    res = run_stop(m, force=True)
    check(not spy.killed and m._locate_root() == (None, None),
          "llama-servers on other ports / without --port are never touched")

    # adoption of a server started by hand: identified by its answer, model from /v1/models
    reset()
    srv(100, None, exe=OTHER)
    net = Net({"/health": READY, "/v1/models": LLAMA(200, {"data": [{"id": "qwen-adopted"}]})})
    m, _spy = manager([100], net)
    out = m._adopt(threading.Event())
    check(out == ("ready", "") and m._adopted and m._model_id == "qwen-adopted", f"adopted ({out}, {m._model_id})")
    net = Net({"/health": LOADING})
    m, _spy = manager([100], net, ready_timeout_s=0)
    out = m._adopt(threading.Event())
    check(out is not None and out[0] == "error" and "left running" in out[1], f"still loading at the deadline: "
          f"left running ({out})")
    reset()
    FakeProc(100, "nginx.exe", ["nginx"], r"C:\nginx\nginx.exe")
    m, _spy = manager([100], Net({"/health": HttpReply(200, {"status": "ok"})}))
    out = m._adopt(threading.Event())
    check(out is not None and out[0] == "error" and out[1].endswith("not llama.cpp"), f"a foreign port is not adopted ({out})")

    # a start refuses anything that is not llama-server.exe (before any process could be created)
    for cfg, want in ((strata_cfg(), "must be llama-server.exe"), ({"launch_cmd": []}, "No llama-server chosen yet"),
                      ({"launch_cmd": [r"C:\x\run.bat"]}, "must be llama-server.exe")):
        m = lmgr(ENG, **dict(cfg_of(), **cfg))
        m.is_running = lambda: False  # type: ignore[method-assign]
        out = m._do_start(threading.Event())
        check(out is not None and out[0] == "error" and want in out[1], f"start refused: {out}")


# ------------------------------------------------------------------------------------------------ registry / compat
def t_registry_and_compat() -> None:
    check("llama_cpp" in engines.available() and "strata" in engines.available(), "the registry lists both engines")
    check(isinstance(engines.get_engine({"backend": "llama_cpp"}), LlamaCppEngine)
          and isinstance(engines.get_engine({"backend": " LLAMA_CPP "}), LlamaCppEngine), "backend llama_cpp")
    check(isinstance(engines.get_engine({}), StrataEngine) and isinstance(engines.get_engine(strata_cfg()), StrataEngine),
          "no backend / a Strata config: still Strata")
    ch = engines.choices()
    check(("llama_cpp", "llama.cpp") in ch and ("strata", "Strata") in ch, f"choices name both ({ch})")
    e = ENG
    check(e.id == "llama_cpp" and e.name == "llama.cpp" and e.default_port == 9931 and e.log_prefix == "llama",
          "id, name, default port 9931, llama-*.log")
    check(e.capabilities == frozenset({base.CAP_LIVE_ACTIVITY, base.CAP_REQUEST_HISTORY, base.CAP_LOAD_PROGRESS,
                                         base.CAP_FIND_INSTALL})
          and not e.can(base.CAP_UNLOAD) and not e.can(base.CAP_LAUNCHER_FILES), "declared capabilities")
    check(e.unload(http_of({}), False) == base.UNLOAD_UNSUPPORTED, "no unload call")
    check(e.has_dashboard and ("llama-server", "llama-server.exe") in e.program_filetypes and "llama-server.exe"
          in e.setup_hint, "Settings texts")

    sc = strata_cfg()
    check("must be llama-server.exe, not python.exe" in err(sc), "a Strata config.json: plan_launch refuses clearly")
    reset()
    procs = [FakeProc(1, "python.exe", sc["launch_cmd"], PY), FakeProc(2, "python.exe", [PY] + sc["launch_cmd"][1:],
                                                                     r"C:\Py\python.exe", 1),
             FakeProc(3, "strata.exe", [S + r"\engine\strata.exe"], S + r"\engine\strata.exe", 2),
             FakeProc(4, "cmd.exe", ["cmd", "/c", sc["launcher_bat"]], r"C:\Windows\System32\cmd.exe")]
    check(not any(e.is_server_process(p.name(), p.cmdline()) for p in procs), "Strata's processes are never llama-server")
    check(not any(e.verified_by_process(p, sc) or e.owns_child(p, sc) for p in procs), "...nor verified or owned")
    check(not e.claims_port(sc["launch_cmd"], 8080, sc) or not e.is_server_process("python.exe", sc["launch_cmd"]),
          "a Strata --port 8080 cannot make a python a llama-server")

    src = Path(engines.__file__).with_name("llama_cpp.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add("." + (node.module or "") if node.level else (node.module or "").split(".")[0])
    allowed = {"__future__", "json", "math", "os", "re", "struct", "collections", "typing", ".base", "psutil",
               "dataclasses", "llama_finder",       # llama_finder: the bounded, read-only search (imported lazily)
               "llama_models"}                      # llama_models: pure per-model settings (imported lazily)
    check(mods <= allowed, f"imports: standard library, engines.base, llama_finder and llama_models only "
                           f"({sorted(mods)})")
    check(not any(w in src for w in ("Popen(", "urlopen(", ".connect(", ".terminate(", ".kill(", "send_signal(",
                                      "process_iter(", "psutil.Process(")),
          "no process start, connection or process action in the module")


# ------------------------------------------------------------------------------------------------ GPU fit pre-check
def _gs(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _gval(vt: int, val) -> bytes:
    if vt == 8:
        return _gs(val)
    if vt == 4:
        return struct.pack("<I", val)
    if vt == 10:
        return struct.pack("<Q", val)
    if vt == 6:
        return struct.pack("<f", val)
    if vt == 9:
        et, items = val
        return struct.pack("<I", et) + struct.pack("<Q", len(items)) + b"".join(_gval(et, x) for x in items)
    raise ValueError(vt)


def write_gguf(path: Path, kvs, version: int = 3, magic: bytes = b"GGUF", n_kv=None, cut: int = 0) -> Path:
    """A metadata-only GGUF file (no tensors), as llama.cpp's gguf writer lays it out."""
    b = magic + struct.pack("<I", version) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs) if n_kv is None
                                                                                  else n_kv)
    for key, vt, val in kvs:
        b += _gs(key) + struct.pack("<I", vt) + _gval(vt, val)
    path.write_bytes(b[:-cut] if cut else b)
    return path


QWEN_KVS = [("general.architecture", 8, "qwen3"), ("general.name", 8, "Some 27B"),
            ("tokenizer.ggml.tokens", 9, (8, [f"tok{i}" for i in range(300)])),
            ("tokenizer.ggml.scores", 9, (6, [0.5] * 300)),
            ("qwen3.context_length", 4, 262144), ("qwen3.block_count", 4, 64)]


def t_fit(tmp: Path) -> None:
    e = ENG
    from engines.llama_cpp import FIT_EXE, fit_args, gguf_block_count, judge_fit, parse_fit_line
    d = tmp / "fit llama"
    d.mkdir()
    exe = d / "llama-server.exe"
    exe.write_bytes(b"")                       # stand-ins: never executed
    tool = d / FIT_EXE
    model = write_gguf(tmp / "fit-model.gguf", QWEN_KVS)
    secret = "sk-very-secret-123"

    def c(*args, **kw):
        out = cfg_of(launch_cmd=[str(exe), *args], launch_cwd=str(d), port=9931)
        out.update(kw)
        return out

    base_args = ("-m", str(model), "--port", "9931")
    check(e.watch_gpu_fit is True and base.Engine.watch_gpu_fit is False and StrataEngine().watch_gpu_fit is False,
          "watch_gpu_fit: on for llama.cpp only")
    # --- no tool: no check
    check(e.fit_check(c(*base_args, "-c", "8192")) is None, "llama-fit-params.exe missing next to the server: None")
    tool.write_bytes(b"")
    fc = e.fit_check(c(*base_args, "-c", "8192", "-np", "1"))
    check(isinstance(fc, base.FitCheck) and fc.program == str(tool) and fc.timeout_s == 20.0,
          f"the tool from the server's own folder, 20 s ({fc})")
    check(fc is not None and fc.args == ("-m", os.path.abspath(str(model)), "-c", "8192", "-np", "1"),
          f"the measured command: -m <model> -c <ctx> -np 1 ({fc and fc.args})")
    check(e.fit_check({}) is None and e.fit_check({"launch_cmd": []}) is None and e.fit_check(strata_cfg()) is None,
          "nothing configured / a Strata config: None")
    check(e.fit_check(cfg_of(launch_cmd=["llama-server.exe", "-m", str(model)])) is None, "a relative program: None")
    check(e.fit_check(c("--port", "9931", "-m", str(tmp / "missing.gguf"))) is None, "model file not on disk: None")
    check(e.fit_check(c("--port", "9931")) is None, "no model at all: None")
    check(e.fit_check({"launch_cmd": 5}) is None and e.fit_check(c(*base_args, "-c")) is None,
          "garbage config / an option without its value: None, no exception")

    # --- server-only and side-effect options are removed; the API key never goes along
    hostile = c(*base_args, "--host", "127.0.0.1", "--api-key", secret, "--api-key-file", r"C:\keys.txt",
                "-a", "my-alias", "--jinja", "--metrics", "--log-file", r"C:\x\log.txt", "-t", "8", "--mmproj",
                str(tmp / "mmproj.gguf"), "-kvu", "--reasoning-format", "deepseek", "--chat-template-kwargs",
                '{"enable_thinking": false}', "--list-devices", "--version", "--help", "-fitp", "on",
                "--slot-save-path", r"C:\slots", "--no-webui", "--threads-http", "4", "-c", "16384", "-fa", "on",
                "-ctk", "q8_0", "-ctv", "q8_0", "-b", "2048", "-ub", "512", "--swa-full", "-nkvo", "-fitt", "512",
                "-fitc", "8192", "-dev", "CUDA0", "-mg", "0", "-sm", "layer", "--override-kv", "x.y=int:3", "-fit",
                "on", "--no-op-offload", "-ngl", "auto", api_key=secret)
    fc = e.fit_check(hostile)
    args = list(fc.args) if fc else []
    want = ["-m", os.path.abspath(str(model)), "-c", "16384", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "-b",
            "2048", "-ub", "512", "--swa-full", "-nkvo", "-fitt", "512", "-fitc", "8192", "-dev", "CUDA0", "-mg", "0",
            "-sm", "layer", "--override-kv", "x.y=int:3", "-fit", "on", "--no-op-offload"]
    check(args == want, f"only the options that change the fit are passed on, in order ({args})")
    for gone in ("--port", "9931", "--host", "--api-key", "--api-key-file", "-a", "--jinja", "--metrics", "--log-file",
                 "-t", "--mmproj", "-kvu", "--list-devices", "--version", "--help", "-fitp", "--slot-save-path",
                 "-ngl", "--chat-template-kwargs"):
        check(gone not in args, f"{gone} is not passed on")
    check(not any(secret in a for a in args), "the API key is in no argument")
    check(e.fit_check(c(*base_args, "-c", "8192", api_key="8192")) is None,
          "an API key equal to a passed-on value: no check at all (never hand it over)")
    check(e.fit_check(c("-m", str(model), "--port", "9931", api_key=os.path.basename(str(tmp)))) is None,
          "an API key inside the model path: no check")
    fc = e.fit_check(c(*base_args, "--api-key", "-sk-secret"))
    check(fc is not None and fc.args == ("-m", os.path.abspath(str(model))),
          "--api-key's value is consumed even when it looks like an option")
    fc = e.fit_check(c(*base_args, "--ctx_size", "4096", "--flash_attn", "on"))
    check(fc is not None and fc.args[2:] == ("--ctx-size", "4096", "--flash-attn", "on"),
          "'_' in a long option is '-' (as llama.cpp reads it)")
    fc = e.fit_check(c("-m", "fit-model.gguf", "--port", "9931", launch_cwd=str(tmp)))
    check(fc is not None and fc.args[1] == os.path.abspath(str(model)), "a relative -m resolves against launch_cwd")
    fc = e.fit_check(c("-m", "fit-model.gguf", "--port", "9931", launch_cwd=str(tmp / "gone")))
    check(fc is None, "...and against the program's folder when launch_cwd does not exist (no model there: None)")
    fc = e.fit_check(c("-m", str(tmp / "other.gguf"), "-m", str(model), "--port", "9931"))
    check(fc is not None and fc.args == ("-m", os.path.abspath(str(model))), "the last -m wins, passed once")

    # --- arguments that make the check meaningless
    meaningless = [
        (("-ngl", "99"), "-ngl 99 (fit.cpp aborts when the user set the layers)"),
        (("--gpu-layers", "40"), "--gpu-layers 40"), (("--n_gpu_layers", "0"), "--n_gpu_layers 0"),
        (("-ngl", "all"), "-ngl all"), (("--fit", "off"), "--fit off"), (("-fit", "0"), "-fit 0"),
        (("-dev", "none"), "--device none"), (("-ot", "exps=CPU"), "-ot"), (("-cmoe",), "--cpu-moe"),
        (("-ncmoe", "4"), "--n-cpu-moe"), (("-ts", "1,1"), "--tensor-split"), (("-sm", "row"), "-sm row"),
        (("-sm", "tensor"), "-sm tensor"), (("-md", str(model)), "a draft model"),
        (("--spec-type", "draft-mtp"), "an MTP speculative context"), (("-hf", "org/repo"), "-hf (download)"),
        (("-mu", "https://example.invalid/m.gguf"), "--model-url"), (("--rpc", "rpc-host.invalid:50052"), "--rpc"),
        (("--gpt-oss-20b-default",), "a preset that downloads"), (("-np", "0"), "-np 0"), (("-np", "x"), "-np x"),
        (("--kv-unified-per-slot", "abc"), "--kv-unified-per-slot abc"),
    ]
    for extra, why in meaningless:
        check(e.fit_check(c(*base_args, *extra)) is None, f"{why}: no check")
    for extra in (("-ngl", "auto"), ("-ngl", "-1"), ("-fit", "on"), ("-sm", "none"), ("-dev", "CUDA0,CUDA1")):
        check(e.fit_check(c(*base_args, *extra)) is not None, f"{' '.join(extra)}: still checked")

    # --- slots: the server's auto (4 slots, one unified cache) = the tool's default of one sequence
    def a(*extra):
        fc = e.fit_check(c(*base_args, *extra))
        return list(fc.args[2:]) if fc else None
    check(a() == [], "no -np: not passed (server auto = 4 slots with one unified KV cache = 1 stream)")
    check(a("-np", "4") == ["-np", "4"], "-np 4 without -kvu: 4 sequences")
    check(a("-np", "4", "-kvu") == [] and a("-kvu", "-np", "4", "-no-kvu") == ["-np", "4"],
          "-np with a unified cache: not passed (last -kvu/-no-kvu wins)")
    check(a("-np", "-1") == [], "-np -1 (auto): not passed")
    check(a("--kv-unified-per-slot", "8192") == ["-c", "32768"], "--kv-unified-per-slot 8192, auto slots: -c 4*8192")
    check(a("--kv-unified-per-slot", "8192", "-np", "2") == ["-np", "2", "-c", "16384"], "... with -np 2: -c 2*8192")
    check(a("--kv-unified-per-slot", "8192", "-c", "4096") == ["-c", "4096"], "... -c given: the user's -c stands")
    check(fit_args(c(*base_args)) == ("-m", os.path.abspath(str(model))), "fit_args is what fit_check passes")

    # --- the GGUF header
    check(gguf_block_count(str(model)) == 64, "block_count read from the header (after string/float arrays)")
    m2 = write_gguf(tmp / "early.gguf", [("llama.block_count", 10, 40), ("general.architecture", 8, "llama")])
    check(gguf_block_count(str(m2)) == 40, "block_count before the architecture, uint64")
    other_arch = write_gguf(tmp / "oa.gguf", [("general.architecture", 8, "qwen3"), ("clip.block_count", 4, 27)])
    check(gguf_block_count(str(other_arch)) is None, "another architecture's block_count does not count")
    bad = [write_gguf(tmp / "v1.gguf", QWEN_KVS, version=1), write_gguf(tmp / "magic.gguf", QWEN_KVS, magic=b"GGML"),
           write_gguf(tmp / "cut.gguf", QWEN_KVS, cut=3), write_gguf(tmp / "nkv.gguf", QWEN_KVS, n_kv=10 ** 9),
           write_gguf(tmp / "nest.gguf", [("general.architecture", 8, "qwen3"), ("x", 9, (9, [])),
                                          ("qwen3.block_count", 4, 64)]),
           write_gguf(tmp / "zero.gguf", [("general.architecture", 8, "qwen3"), ("qwen3.block_count", 4, 0)])]
    empty = tmp / "empty.gguf"
    empty.write_bytes(b"")
    huge = tmp / "hugestr.gguf"
    huge.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2 ** 62))
    for p in [*bad, empty, huge, tmp / "no-such.gguf", tmp]:
        check(gguf_block_count(str(p)) is None, f"unusable header -> None ({p.name})")
    check(gguf_block_count(None) is None and gguf_block_count("") is None, "no path -> None")

    # --- the tool's answer (exact strings measured with build b12314 on a 27B model)
    cfg = c(*base_args)
    for out, fits, frag in [
        ("-c 8192 -ngl -1\n", True, "fits on the GPU at -c 8192"),
        ("-c 8192 -ngl -1\r\n", True, "fits"),
        ("-c 65536 -ngl 54", False, "54 of 65 layers on the GPU at -c 65536, 11 on the CPU"),
        ("-c 32768 -ngl 63", False, "63 of 65 layers"),
        ("-c 16384 -ngl 63", False, "2 on the CPU"),
        ("-c 8192 -ngl 65", None, "all 65 layers on the GPU"),
        ("-c 8192 -ngl 70", None, "not judged"),
        ("-c 8192 -ngl 0", False, "0 of 65"),
        ("--ctx-size 8192 --gpu-layers -1", True, "fits"),
        ("--ctx-size 8192 --n-gpu-layers -1", True, "fits"),
        ("-ngl auto", True, "fits on the GPU"),
        ("-c 8192 -ngl 40 -ts 20,20", False, "40 of 65"),
    ]:
        r = e.parse_fit(out, cfg)
        check(isinstance(r, base.FitReport) and r.fits is fits and frag in r.detail, f"{out!r} -> {r}")
    unknown = c("-m", str(empty), "--port", "9931")
    r = e.parse_fit("-c 8192 -ngl 65", unknown)
    check(r.fits is False and "unknown" in r.detail, f"layer count unknown: any count = partly on the CPU ({r})")
    check(e.parse_fit("-c 8192 -ngl 54", {}).fits is False, "...also without a usable config")
    check(e.parse_fit("-c 8192 -ngl -1", {}).fits is True, "-1 needs no model file")
    junk = ["", "   \r\n", "hello", "-c 8192", "-c 8192 -ngl", "-ngl -1 -ngl 5", "-c 8192 -ngl -1\n-c 4096 -ngl 3",
            "error: invalid argument: --port", "-ngl 1e3", "-ngl -5", "-ngl all", "-ngl 12abc", "-c 8192 -ngl -1 x",
            "-ngl\x00 -1", "-c " + "9" * 2000 + " -ngl -1", "load_backend: loaded CUDA backend\n-c 8192 -ngl -1",
            "ngl -1", "-c 8192 -ngl -1 -ot", "-c -ngl -1 5", "-ngl 9999999", "\u202e-ngl -1"]
    for out in junk:
        r = e.parse_fit(out, cfg)
        check(isinstance(r, base.FitReport) and r.fits is None and r.detail, f"junk {out[:40]!r} -> None ({r.detail})")
    for out in (None, b"-c 8192 -ngl -1", 5, ["-ngl", "-1"], object()):
        r = e.parse_fit(out, cfg)  # type: ignore[arg-type]
        check(isinstance(r, base.FitReport) and r.fits is None, f"a non-text answer {type(out).__name__} -> None")
    check(parse_fit_line("-c 8192 -ngl -1") == {"-c": "8192", "-ngl": "-1"}, "parse_fit_line folds nothing extra")
    check(judge_fit("-c 8192 -ngl 10", 9).fits is None and judge_fit("-c 8192 -ngl 9", 9).fits is None
          and judge_fit("-c 8192 -ngl 8", 9).fits is False,
          "judge_fit: total = block_count + 1 (the output layer)")


def t_model_label() -> None:
    from engines.llama_cpp import model_label
    check(model_label("C:/models/Qwen-27B-Q3.gguf") == "Qwen-27B-Q3" and model_label(r"D:\m\a b.GGUF") == "a b",
          "model_label: a model file path is shown by its file name")
    check(model_label("my-alias") == "my-alias" and model_label("org/model") == "org/model"
          and model_label("org/model:Q4") == "org/model:Q4", "model_label: an alias or repo name is kept")
    check(model_label("") == "" and model_label(".gguf") == ".gguf", "model_label: nothing sensible is left alone")
    eng = engines.get_engine({"backend": "llama_cpp"})
    reply = HttpReply(code=200, json={"data": [{"id": "C:/models/Qwen-27B-Q3.gguf"}]})
    got = eng.fetch_model_id(lambda m, p, t: reply, ServiceState())
    check(got == "Qwen-27B-Q3", f"fetch_model_id: file name, not the path ({got!r})")
    check(eng.fetch_model_id(lambda m, p, t: HttpReply(), ServiceState()) is None, "fetch_model_id: no answer -> None")


def main() -> int:
    install_guards()
    tmp = Path(tempfile.mkdtemp(prefix="agentstart-llama-files-"))
    tests = (t_identity, t_probe, t_children_and_sweep, lambda: t_launch(tmp), t_busy_and_headers,
             lambda: t_log_parsing(tmp), lambda: t_poller(tmp), t_load_progress, t_supervisor_stop,
             t_supervisor_lookalikes, t_supervisor_hidden_pid_and_adopt, t_registry_and_compat,
             lambda: t_fit(tmp), t_model_label)
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"{getattr(t, '__name__', t)} raised {exc!r}")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
