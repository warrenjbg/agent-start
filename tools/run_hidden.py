"""Run window suites on a separate, hidden Windows desktop: their windows are never shown on the user's desktop
and cannot take the keyboard focus.

    python tools/run_hidden.py                      every window suite (WINDOW_SUITES below)
    python tools/run_hidden.py test_settings_tabs   the named suites
    python tools/run_hidden.py --out DIR ...        where the per-suite output goes (default: a temp folder)

How: CreateDesktop makes a second desktop in the same window station; each suite's python is started with
STARTUPINFO.lpDesktop naming it. The child refuses to import anything if it finds itself on the user's own desktop
("Default"). AGENTSTART_TEST_HIDDEN=1 tells tests/shots.py to make its pictures with PrintWindow (saved as
tests/ui_hidden_*.png), because a screen grab is not possible there. Only the processes this script starts are
ever waited for or ended. Needs pywin32 (already a dependency of the app).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = "AgentStartTests"
TIMEOUT_MS = 900_000
WINDOW_SUITES = [
    "test_ui_smoke", "test_app_extras", "test_v110", "test_busy_switch", "test_e2e_fake", "test_gpu_fit_ui",
    "test_history_dialog", "test_engine_switch", "test_engine_find", "test_settings_tabs", "test_strata_tune_ui",
    "test_review_fixes", "test_update_controller", "test_download_dialog",
]


def _desktop_name() -> str:
    import ctypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetThreadDesktop.restype = ctypes.c_void_p
    u32.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                                              ctypes.POINTER(ctypes.c_uint)]
    buf = ctypes.create_unicode_buffer(256)
    need = ctypes.c_uint(0)
    ok = u32.GetUserObjectInformationW(u32.GetThreadDesktop(k32.GetCurrentThreadId()), 2, buf, ctypes.sizeof(buf),
                                       ctypes.byref(need))
    return buf.value if ok else ""


def child(suite: str, out_path: str) -> int:
    """Runs inside the hidden desktop: the suite as __main__, its output in out_path."""
    import runpy
    name = _desktop_name()
    with open(out_path, "w", encoding="utf-8", errors="replace", buffering=1) as fh:
        fh.write(f"desktop: {name!r}\n")
        if not name or name.casefold() == "default":
            fh.write("REFUSED: this is the user's desktop\n")
            return 3
        sys.stdout = sys.stderr = fh
        sys.argv = [suite]
        sys.path.insert(0, os.path.dirname(suite))
        try:
            runpy.run_path(suite, run_name="__main__")
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            fh.flush()
            os._exit(code)                       # daemon threads of a finished suite must not hold the exit
        fh.flush()
        os._exit(0)


def main(argv: list[str]) -> int:
    if argv[:1] == ["--child"]:
        return child(argv[1], argv[2])
    import win32con
    import win32event
    import win32process
    import win32service
    out_dir = Path(tempfile.gettempdir()) / "agentstart-hidden"
    if argv[:1] == ["--out"]:
        out_dir, argv = Path(argv[1]), argv[2:]
    suites = argv or WINDOW_SUITES
    missing = [s for s in suites if not (ROOT / "tests" / f"{s}.py").is_file()]
    if missing:
        print(f"no such suite: {', '.join(missing)}")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, AGENTSTART_TEST_OFFLINE="1", AGENTSTART_TEST_NO_INNO="1", AGENTSTART_TEST_HIDDEN="1")
    hdesk = win32service.CreateDesktop(DESKTOP, 0, win32con.GENERIC_ALL, None)
    fail = False
    try:
        for suite in suites:
            out = out_dir / f"{suite}.txt"
            si = win32process.STARTUPINFO()
            si.lpDesktop = f"WinSta0\\{DESKTOP}"
            cmd = (f'"{sys.executable}" -u -X faulthandler "{Path(__file__).resolve()}" --child '
                   f'"{ROOT / "tests" / (suite + ".py")}" "{out}"')
            t0 = time.monotonic()
            hproc, hthread, _pid, _tid = win32process.CreateProcess(
                None, cmd, None, None, False, win32con.CREATE_NO_WINDOW, env, str(ROOT), si)
            try:
                if win32event.WaitForSingleObject(hproc, TIMEOUT_MS) != win32event.WAIT_OBJECT_0:
                    win32process.TerminateProcess(hproc, 124)      # our own child, after 15 minutes
                    win32event.WaitForSingleObject(hproc, 10_000)
                rc = win32process.GetExitCodeProcess(hproc)
            finally:
                hthread.Close()
                hproc.Close()
            text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
            last = [ln for ln in text.splitlines() if "RESULT" in ln or "passed" in ln]
            fail = fail or rc != 0
            print(f"{'PASS' if rc == 0 else 'FAIL'}  {suite:<24} exit={rc:<3} {time.monotonic() - t0:5.0f}s  "
                  f"{last[-1].strip() if last else '(no result line)'}", flush=True)
    finally:
        hdesk.CloseDesktop()
    print(f"output: {out_dir}")
    print("ALL PASSED" if not fail else "SOME FAILED")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
