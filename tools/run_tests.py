"""Run the test scripts in tests/ one by one and summarise their RESULT lines.

Each tests/test_*.py is a standalone script (exit code 0 = pass) - there is no pytest. This runner just starts them
with the current interpreter, streams their output and prints one summary table at the end.

    python tools/run_tests.py --ci            # the headless suites CI runs (see CI_SUITES)
    python tools/run_tests.py test_stats test_ram_plan
    python tools/run_tests.py --list

--ci also sets AGENTSTART_TEST_OFFLINE=1 and AGENTSTART_TEST_NO_INNO=1 (unless already set), like the workflow.
Without --ci a name is required: the GUI suites open real windows and take screenshots, so "run everything" is not
a default. tests/run_*.py (they drive the REAL Strata / the installed app) are never run by this script.
Exit code: 0 if every selected suite exited 0, else 1.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# Headless suites: no window, no screenshot, no GPU/Strata/network needed. Keep in sync with .github/workflows/ci.yml
# and the "Tests" section of README.md.
CI_SUITES = [
    "test_config_store", "test_identity", "test_load_progress", "test_launcher_bat", "test_engine_log",
    "test_ram_plan", "test_stats", "test_reopen", "test_game_watch", "test_strata_finder", "test_autostart",
    "test_toasts", "test_updater", "test_server_manager", "test_mem_watch", "test_resume_watch", "test_usage_history",
    "test_llama_cpp", "test_fit_check", "test_fit_watch", "test_llama_finder", "test_engine_profiles",
    "test_strata_tuning", "test_llama_models", "test_update_sig", "test_hf_download", "test_hermes_alias",
    "test_alias_wiring", "test_download_flow", "test_idle_watch", "test_kill_path", "test_engines",
    "test_metrics_sample", "test_protected_changes", "test_metrics_worker",
]
# Run in CI as advisory (non-blocking): they depend on what the runner's desktop/hardware happens to provide.
CI_ADVISORY = ["test_detector", "test_tray"]

CI_ENV = {"AGENTSTART_TEST_OFFLINE": "1", "AGENTSTART_TEST_NO_INNO": "1"}


def all_suites() -> list[str]:
    return sorted(p.stem for p in TESTS.glob("test_*.py"))


def run(name: str, env: dict[str, str], timeout: float) -> tuple[int, str, float]:
    """Run tests/<name>.py; return (exit code, last RESULT line (else last 'passed' line), seconds)."""
    t = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-u", str(TESTS / f"{name}.py")], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace")
    result, last = "", ""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            print(line, end="", flush=True)
            if "passed" in line.lower():
                last = line.strip()
            if line.lstrip().upper().startswith("RESULT"):
                result = line.strip()
        result = result or last         # a few suites print 'N/M passed' / 'N passed, 0 failed' instead
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()                     # only the test script this runner started
        proc.wait()
        code, result = 1, f"TIMEOUT after {timeout:g} s"
    return code, result, time.monotonic() - t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("suites", nargs="*", help="suite names (test_x or x); see --list")
    ap.add_argument("--ci", action="store_true", help="the headless CI set + the CI env switches")
    ap.add_argument("--advisory", action="store_true", help="the advisory CI set (test_detector, test_tray)")
    ap.add_argument("--list", action="store_true", help="list the suites and exit")
    ap.add_argument("--timeout", type=float, default=900.0, help="per-suite timeout in seconds (default 900)")
    a = ap.parse_args()

    known = all_suites()
    if a.list:
        for n in known:
            tag = "ci" if n in CI_SUITES else "ci-advisory" if n in CI_ADVISORY else "local only"
            print(f"{n:<22} {tag}")
        return 0
    names = [n if n.startswith("test_") else f"test_{n}" for n in a.suites]
    if a.ci:
        names += CI_SUITES
    if a.advisory:
        names += CI_ADVISORY
    names = list(dict.fromkeys(names))
    if not names:
        ap.error("name at least one suite, or use --ci / --advisory (see --list)")
    unknown = [n for n in names if n not in known]
    if unknown:
        ap.error(f"unknown suite(s): {', '.join(unknown)}")

    env = dict(os.environ)
    if a.ci or a.advisory:
        for k, v in CI_ENV.items():
            env.setdefault(k, v)
    env["PYTHONIOENCODING"] = "utf-8"

    rows = []
    for n in names:
        print(f"\n##### {n}", flush=True)
        code, result, secs = run(n, env, a.timeout)
        rows.append((n, code, result, secs))

    print("\n" + "=" * 100)
    for n, code, result, secs in rows:
        print(f"{'PASS' if code == 0 else 'FAIL':<5} {n:<22} exit={code:<3} {secs:6.1f}s  {result}")
    failed = [n for n, code, _, _ in rows if code != 0]
    print(f"{len(rows) - len(failed)}/{len(rows)} suites passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
