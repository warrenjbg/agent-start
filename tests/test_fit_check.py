"""Self-check for fit_check.py (the runner of an engine's GPU-fit pre-check). Run:
    .venv\\Scripts\\python.exe tests\\test_fit_check.py

Safe by construction:
  * Every rule is driven through the `popen` seam with a fake that records the call and never starts anything.
  * ONE real run: a copy of this venv's python.exe renamed to llama-fit-params.exe in a temp folder (with the venv's
    pyvenv.cfg next to its Scripts folder, so the venv redirector still works) prints the measured llama-fit-params
    answer. Its command line carries this run's unique MARKER; at the end only processes carrying the MARKER are
    killed (none should be left), and the temp folder is removed. No llama.cpp program, no model, no network, no port.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

import fit_check  # noqa: E402
from engines.base import FitCheck  # noqa: E402
from engines.llama_cpp import LlamaCppEngine  # noqa: E402

PASSED = 0
FAILED = 0
MARKER = f"hc-fitcheck-test-{uuid.uuid4().hex[:12]}"
TMP = Path(tempfile.mkdtemp(prefix=MARKER + "-"))

ENG_DIR = r"C:\AI\llama b1"
ENGINE = ENG_DIR + r"\llama-server.exe"
TOOL = ENG_DIR + r"\llama-fit-params.exe"


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {msg}")
    else:
        FAILED += 1
        print(f"[FAIL] {msg}")


class FakeRun:
    """A subprocess.run stand-in: records every call, returns `result` or raises `exc`."""

    def __init__(self, stdout: bytes = b"-c 8192 -ngl -1\r\n", code: int = 0, exc: BaseException | None = None):
        self.calls: list[tuple[list, dict]] = []
        self.stdout, self.code, self.exc = stdout, code, exc

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), dict(kw)))
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(returncode=self.code, stdout=self.stdout)


def go(check_obj, engine=ENGINE, fake: FakeRun | None = None, exists=lambda p: True):
    fake = fake if fake is not None else FakeRun()
    return fit_check.run(check_obj, engine, popen=fake, exists=exists), fake


# ------------------------------------------------------------------------------------------------ tests
def t_call_shape() -> None:
    out, fake = go(FitCheck(TOOL, ("-m", r"C:\m.gguf", "-c", "8192", "-np", "1"), 20.0))
    check(out == "-c 8192 -ngl -1\r\n", f"stdout returned as text ({out!r})")
    check(len(fake.calls) == 1, "run once")
    argv, kw = fake.calls[0]
    check(argv == [TOOL, "-m", r"C:\m.gguf", "-c", "8192", "-np", "1"], f"list argv, program first ({argv})")
    check(kw.get("shell") is False, "shell=False")
    check(kw.get("stdin") is subprocess.DEVNULL and kw.get("stdout") is subprocess.PIPE
          and kw.get("stderr") is subprocess.DEVNULL, "stdin DEVNULL, stdout PIPE, stderr DEVNULL")
    check(kw.get("creationflags") == 0x08000000, "CREATE_NO_WINDOW")
    check(os.path.normcase(kw.get("cwd") or "") == os.path.normcase(ENG_DIR), f"cwd = the engine's folder ({kw})")
    check(kw.get("timeout") == 20.0 and kw.get("check") is False, "timeout = check.timeout_s, check=False")
    _, fake = go(FitCheck(TOOL, (), 600.0))
    check(bool(fake.calls) and fake.calls[0][1]["timeout"] == 60.0, "a long time limit is capped at 60 s")
    _, fake = go(FitCheck(TOOL, (), 0.5))
    check(bool(fake.calls) and fake.calls[0][1]["timeout"] == 0.5, "a short one is kept")
    out, _ = go(FitCheck(TOOL, ["-c", "8192"], 20.0))  # type: ignore[arg-type]
    check(out is not None, "args as a list are accepted too")


def t_refusals() -> None:
    cases = [
        (FitCheck("llama-fit-params.exe", (), 20.0), ENGINE, "a bare name (not absolute)"),
        (FitCheck(ENG_DIR + r"\evil.exe", (), 20.0), ENGINE, "another .exe in the engine's folder"),
        (FitCheck(ENG_DIR + r"\llama-cli.exe", (), 20.0), ENGINE, "another llama.cpp tool"),
        (FitCheck(r"llama b1\llama-fit-params.exe", (), 20.0), ENGINE, "a relative path"),
        (FitCheck(ENG_DIR + r"\fit.bat", (), 20.0), ENGINE, "a .bat"),
        (FitCheck(ENG_DIR + r"\fit.cmd", (), 20.0), ENGINE, "a .cmd"),
        (FitCheck(ENG_DIR + r"\fit.exe.bat", (), 20.0), ENGINE, "a .exe.bat"),
        (FitCheck(ENG_DIR + r"\fit.exe ", (), 20.0), ENGINE, "'.exe ' with a trailing space"),
        (FitCheck(ENG_DIR + r"\fit", (), 20.0), ENGINE, "no extension"),
        (FitCheck(r"C:\AI\llama-fit-params.exe", (), 20.0), ENGINE, "the parent folder"),
        (FitCheck(ENG_DIR + r"\sub\llama-fit-params.exe", (), 20.0), ENGINE, "a child folder"),
        (FitCheck(r"C:\AI\llama b2\llama-fit-params.exe", (), 20.0), ENGINE, "a sibling folder"),
        (FitCheck(r"D:\AI\llama b1\llama-fit-params.exe", (), 20.0), ENGINE, "another drive"),
        (FitCheck(ENG_DIR + r"\..\llama b1x\llama-fit-params.exe", (), 20.0), ENGINE, "'..' leading elsewhere"),
        (FitCheck(ENGINE, ("-m", "x"), 20.0), ENGINE, "the engine program itself (would start a server)"),
        (FitCheck(ENG_DIR.upper() + r"\LLAMA-SERVER.EXE", (), 20.0), ENGINE, "...also in other letter case"),
        (FitCheck(TOOL, (), 20.0), "llama-server.exe", "a relative engine program"),
        (FitCheck(TOOL, (), 20.0), "", "no engine program"),
        (FitCheck(TOOL, (), 20.0), None, "engine program None"),
        (FitCheck(TOOL, ("-m", 5), 20.0), ENGINE, "a non-text argument"),  # type: ignore[arg-type]
        (FitCheck(TOOL, ("-m", b"x"), 20.0), ENGINE, "a bytes argument"),  # type: ignore[arg-type]
        (FitCheck(TOOL, ("-m", "a\x00b"), 20.0), ENGINE, "an argument with NUL"),
        (FitCheck(TOOL, "-m x", 20.0), ENGINE, "args as one string"),  # type: ignore[arg-type]
        (FitCheck(TOOL, None, 20.0), ENGINE, "args None"),  # type: ignore[arg-type]
        (FitCheck(TOOL, (), 0.0), ENGINE, "time limit 0"),
        (FitCheck(TOOL, (), -5.0), ENGINE, "a negative time limit"),
        (FitCheck(TOOL, (), float("nan")), ENGINE, "time limit NaN"),
        (FitCheck(TOOL, (), float("inf")), ENGINE, "time limit inf"),
        (FitCheck(TOOL, (), True), ENGINE, "time limit True"),  # type: ignore[arg-type]
        (FitCheck(TOOL, (), "20"), ENGINE, "time limit as text"),  # type: ignore[arg-type]
        (FitCheck(TOOL + "\x00.exe", (), 20.0), ENGINE, "NUL in the program"),
        (FitCheck(5, (), 20.0), ENGINE, "program not text"),  # type: ignore[arg-type]
        (None, ENGINE, "no check at all"),
        (SimpleNamespace(), ENGINE, "an object without fields"),
    ]
    for chk, engine, why in cases:
        fake = FakeRun()
        try:
            out = fit_check.run(chk, engine, popen=fake, exists=lambda p: True)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            check(False, f"{why}: raised {exc!r}")
            continue
        check(out is None and not fake.calls, f"refused, nothing run: {why}")
    fake = FakeRun()
    out = fit_check.run(FitCheck(TOOL, (), 20.0), ENGINE, popen=fake, exists=lambda p: False)
    check(out is None and not fake.calls, "refused, nothing run: the file does not exist")
    fake = FakeRun()
    seen: list[str] = []
    out = fit_check.run(FitCheck(TOOL, (), 20.0), ENGINE, popen=fake, exists=lambda p: seen.append(p) or True)
    check(out is not None and seen == [TOOL], "the existence check is made on the program itself")
    for prog in (ENG_DIR.upper() + r"\Llama-Fit-Params.EXE", ENG_DIR + r"\.\llama-fit-params.exe",
                 r"C:/AI/llama b1/llama-fit-params.exe", ENG_DIR + r"\sub\..\llama-fit-params.exe"):
        out, fake = go(FitCheck(prog, (), 20.0))
        check(out is not None and len(fake.calls) == 1, f"same folder, other spelling: allowed ({prog})")


def t_failures() -> None:
    for exc, why in [(subprocess.TimeoutExpired(["x"], 20), "timeout"), (FileNotFoundError(2, "gone"), "missing"),
                     (PermissionError(13, "denied"), "access denied"), (OSError(193, "not a win32 app"), "bad exe"),
                     (ValueError("embedded null"), "ValueError"), (RuntimeError("boom"), "any other error"),
                     (KeyboardInterrupt(), None)]:
        if why is None:
            continue                    # a KeyboardInterrupt is not swallowed (BaseException): not tested here
        out, fake = go(FitCheck(TOOL, (), 20.0), fake=FakeRun(exc=exc))
        check(out is None and len(fake.calls) == 1, f"{why} -> None, no exception")
    for code in (1, -1, 3221225477, None):
        out, _ = go(FitCheck(TOOL, (), 20.0), fake=FakeRun(code=code))  # type: ignore[arg-type]
        check(out is None, f"exit code {code} -> None")
    out, _ = go(FitCheck(TOOL, (), 20.0), fake=FakeRun(stdout=b"x" * 100_000))
    check(out == "x" * 4096, f"stdout truncated to 4 KB ({len(out or '')})")
    out, _ = go(FitCheck(TOOL, (), 20.0), fake=FakeRun(stdout=b"\xff\xfe-ngl -1"))
    check(out == "\ufffd\ufffd-ngl -1", f"undecodable bytes replaced ({out!r})")
    out, _ = go(FitCheck(TOOL, (), 20.0), fake=FakeRun(stdout=b""))
    check(out == "", "empty stdout -> '' (the engine's parser says 'cannot tell')")
    fake = FakeRun()
    fake.stdout = None  # type: ignore[assignment]
    out, _ = go(FitCheck(TOOL, (), 20.0), fake=fake)
    check(out is None, "no stdout captured -> None")
    out = fit_check.run(FitCheck(TOOL, (), 20.0), ENGINE, popen=lambda *a, **k: 5, exists=lambda p: True)
    check(out is None, "a popen seam returning junk -> None")


def t_engine_contract() -> None:
    """What the llama.cpp engine builds passes these rules unchanged (its program sits next to the server)."""
    d = TMP / "llama b1"
    d.mkdir()
    (d / "llama-server.exe").write_bytes(b"")
    (d / "llama-fit-params.exe").write_bytes(b"")
    model = TMP / "m.gguf"
    model.write_bytes(b"")
    cfg = {"launch_cmd": [str(d / "llama-server.exe"), "-m", str(model), "--port", "9931", "--api-key", "k-123456"],
           "launch_cwd": str(d), "port": 9931}
    chk = LlamaCppEngine().fit_check(cfg)
    fake = FakeRun()
    out = fit_check.run(chk, cfg["launch_cmd"][0], popen=fake) if chk else None  # real os.path.isfile
    check(out is not None and len(fake.calls) == 1, "the engine's FitCheck passes the runner's rules")
    check(bool(fake.calls) and not any("k-123456" in a for a in fake.calls[0][0]), "no API key on the command line")
    out = fit_check.run(chk, str(TMP / "elsewhere" / "llama-server.exe"), popen=FakeRun()) if chk else "x"
    check(out is None, "...but not against another engine folder")


def t_junction() -> None:
    try:
        import _winapi
    except ImportError:
        check(True, "junction test skipped (not Windows)")
        return
    real = TMP / "real"
    real.mkdir()
    alias = TMP / "alias"
    try:
        _winapi.CreateJunction(str(real), str(alias))  # type: ignore[attr-defined]
    except OSError as exc:
        check(True, f"junction test skipped ({exc})")
        return
    try:
        (real / "llama-fit-params.exe").write_bytes(b"")
        out, fake = go(FitCheck(str(alias / "llama-fit-params.exe"), (), 20.0), engine=str(real / "llama-server.exe"),
                       exists=os.path.isfile)
        check(out is None and not fake.calls, "a program reached through a junction to the engine folder: refused")
        out, fake = go(FitCheck(str(alias / "llama-fit-params.exe"), (), 20.0), engine=str(alias / "llama-server.exe"),
                       exists=os.path.isfile)
        check(out is not None and len(fake.calls) == 1, "engine and program both via the same junction: allowed")
        out, fake = go(FitCheck(str(alias / "llama-server.exe"), (), 20.0), engine=str(real / "llama-server.exe"))
        check(out is None and not fake.calls, "the engine itself under another name (junction): refused")
    finally:
        os.rmdir(alias)


def ours() -> list[psutil.Process]:
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.pid != os.getpid() and any(MARKER in str(a) for a in (p.info.get("cmdline") or [])):
                out.append(p)
        except (psutil.Error, OSError):
            continue
    return out


def kill_ours() -> int:
    n = 0
    for p in ours():
        try:
            p.kill()
            n += 1
        except (psutil.Error, OSError):
            pass
    return n


def t_real_process() -> None:
    venv = ROOT / ".venv"
    scripts = TMP / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    tool = scripts / "llama-fit-params.exe"
    shutil.copy2(venv / "Scripts" / "python.exe", tool)
    shutil.copy2(venv / "pyvenv.cfg", TMP / "venv" / "pyvenv.cfg")
    engine = scripts / "llama-server.exe"           # never created, never run: only its folder matters
    before = {c.pid for c in psutil.Process().children(recursive=True)}
    t0 = time.monotonic()
    out = fit_check.run(FitCheck(str(tool), ("-c", f"print('-c 8192 -ngl -1')  # {MARKER}"), 30.0), str(engine))
    dt = time.monotonic() - t0
    check(out is not None and out.strip() == "-c 8192 -ngl -1", f"a real .exe ran and its stdout came back ({out!r})")
    r = LlamaCppEngine().parse_fit(out or "", {})
    check(r.fits is True, f"...and the engine reads it as 'fits' ({r})")
    check(dt < 30, f"finished well inside the limit ({dt:.1f} s)")
    time.sleep(0.3)
    left = [c.pid for c in psutil.Process().children(recursive=True) if c.pid not in before]
    check(not left and not ours(), f"no process left behind (children {left}, marked {[p.pid for p in ours()]})")


def t_module_rules() -> None:
    src = (ROOT / "fit_check.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    check(not mods & {"main", "tkinter", "customtkinter", "psutil", "socket", "urllib"},
          f"no Tk, main, psutil or network imports ({sorted(mods)})")
    check("shell=True" not in src and "os.system" not in src and "Popen(" not in src,
          "no shell, no os.system, only the subprocess.run seam")


def main() -> int:
    try:
        for t in (t_call_shape, t_refusals, t_failures, t_engine_contract, t_junction, t_real_process,
                  t_module_rules):
            try:
                t()
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                check(False, f"{t.__name__} raised {exc!r}")
    finally:
        killed = kill_ours()
        time.sleep(0.2)
        check(not ours(), f"cleanup: no marked process alive (killed {killed} at exit)")
        shutil.rmtree(TMP, ignore_errors=True)
        check(not TMP.exists(), "cleanup: temp folder removed")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
