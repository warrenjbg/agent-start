"""Runs an engine's GPU-fit pre-check program (engines.base.FitCheck) - the ONLY place that executes it.

The program is a read-only calculator that ships with the engine (llama.cpp: llama-fit-params.exe next to
llama-server.exe). It is run only when ALL of these hold, checked again here at run time whatever the engine said:
  * check.program is an absolute path to an existing file whose name ends in .exe (CreateProcess runs a .bat/.cmd
    through cmd.exe, which would reinterpret the arguments);
  * its folder IS the engine program's folder (normcase + abspath + realpath, compared both before and after
    resolving links; a parent or child folder does not count) and it is not the engine program itself (that would
    start a server);
  * every argument is a str without NUL.
It runs with list argv and no shell, stdin closed, stderr discarded, no window, in that folder, for at most
min(check.timeout_s, 60) seconds; subprocess.run kills the child it started when the time runs out. Returns the
first 4 KB of stdout (UTF-8, undecodable bytes replaced) or None: refused, timed out, non-zero exit, any error. Never
raises. Blocking: call it from a worker thread. No Tk, no import of main.
"""
from __future__ import annotations

import logging
import math
import os
import subprocess
from typing import Any, Callable

from engines.base import FitCheck

log = logging.getLogger("agent_start")

MAX_TIMEOUT_S = 60.0
MAX_STDOUT_BYTES = 4096
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _canon(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _plain(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _refuse(why: str, program: Any) -> str | None:
    """Log why a check is not run; always None (typed str | None so `return _refuse(...)` reads naturally)."""
    log.warning("fit check refused (%s): %r", why, program)
    return None


def run(check: FitCheck, engine_program: str, *, popen: Callable[..., Any] = subprocess.run,
        exists: Callable[[str], bool] = os.path.isfile) -> str | None:
    """Run `check` if it passes every rule in the module docstring; its stdout (<= 4 KB) or None. Never raises."""
    try:
        return _run(check, engine_program, popen, exists)
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        log.warning("fit check failed: %s", type(exc).__name__)
        return None


ALLOWED_TOOLS = frozenset({"llama-fit-params.exe"})    # the only file names this module will ever run
_ENV_BLOCK = ("HF_", "_URL", "RPC", "LOG", "DOCKER", "API_KEY", "TOKEN")


def _tool_env() -> dict[str, str]:
    """The environment for the tool: the app's own, minus llama.cpp settings that would make a CHECK download a
    model, contact another machine or write a file (LLAMA_ARG_HF_REPO, _MODEL_URL, _RPC, _LOG_FILE ...) and minus
    keys / tokens. Sizing settings (LLAMA_ARG_CTX_SIZE ...) stay, so the answer matches the server's."""
    env = {}
    for k, v in os.environ.items():
        up = k.upper()
        if up == "HF_TOKEN" or (up.startswith("LLAMA_") and any(b in up for b in _ENV_BLOCK)):
            continue
        env[k] = v
    return env


def _run(check: Any, engine_program: Any, popen: Callable[..., Any], exists: Callable[[str], bool]) -> str | None:
    program = getattr(check, "program", None)
    args = getattr(check, "args", None)
    timeout_s = getattr(check, "timeout_s", None)
    if not isinstance(program, str) or not program or "\x00" in program:
        return _refuse("no program", program)
    if not os.path.isabs(program):
        return _refuse("not an absolute path", program)
    if not program.casefold().endswith(".exe"):
        return _refuse("not an .exe", program)
    if os.path.basename(_plain(program)).casefold() not in ALLOWED_TOOLS:
        return _refuse("not a known fit tool", program)
    if not isinstance(engine_program, str) or not engine_program or "\x00" in engine_program \
            or not os.path.isabs(engine_program):
        return _refuse("no absolute engine program to compare with", program)
    folder = os.path.dirname(_plain(program))
    if _plain(os.path.dirname(program)) != _plain(os.path.dirname(engine_program)) \
            or os.path.dirname(_canon(program)) != os.path.dirname(_canon(engine_program)) \
            or _canon(os.path.dirname(program)) != _canon(os.path.dirname(engine_program)):
        return _refuse("not in the engine program's folder", program)
    if _canon(program) == _canon(engine_program) or _plain(program) == _plain(engine_program):
        return _refuse("it is the engine program itself", program)
    if not isinstance(args, (tuple, list)) or not all(isinstance(a, str) and "\x00" not in a for a in args):
        return _refuse("arguments must all be text", program)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) \
            or timeout_s <= 0:
        return _refuse(f"bad time limit {timeout_s!r}", program)
    if not exists(program):
        return _refuse("no such file", program)
    timeout = min(float(timeout_s), MAX_TIMEOUT_S)
    argv = [program, *args]
    try:
        res = popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    cwd=folder, creationflags=CREATE_NO_WINDOW, timeout=timeout, check=False, env=_tool_env())
    except subprocess.TimeoutExpired:
        log.info("fit check timed out after %.0f s: %s", timeout, os.path.basename(program))
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.info("fit check could not run %s: %s", os.path.basename(program), exc)
        return None
    code = getattr(res, "returncode", None)
    if code != 0:
        log.info("fit check %s exited with %r", os.path.basename(program), code)
        return None
    out = getattr(res, "stdout", None)
    if not isinstance(out, (bytes, bytearray)):
        return None
    return bytes(out[:MAX_STDOUT_BYTES]).decode("utf-8", errors="replace")
