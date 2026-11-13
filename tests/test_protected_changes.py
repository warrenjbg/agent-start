"""tools/protected_changes.py: which changed files need a stronger reviewer, and AGENTS.md shows the same list.

No window, no process, no network: the tool's git calls are not made here.

    .venv\\Scripts\\python.exe tests\\test_protected_changes.py
"""
from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import protected_changes as pc  # noqa: E402

PASSED = FAILED = 0


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {msg}")
    else:
        FAILED += 1
        print(f"  [FAIL] {msg}")


def eq(got, want, msg: str) -> None:
    check(got == want, msg if got == want else f"{msg}: got {got!r}, want {want!r}")


def test_matching() -> None:
    hit = [p for p, _why in pc.protected([
        "detector.py", "main.py", "engines/strata.py", "engines\\llama_cpp.py", "tests/test_identity.py",
        "tools/sign_release.py", "tools/run_tests.py", "installer/AgentStart.iss", "installer/version.iss",
        ".github/workflows/ci.yml", "README.md", "update_sig.py", "update_sig.py", "", "docs/engines.md",
        "old/detector.py", "detector.py.bak", "history_dialog.py"])]
    eq(hit, ["detector.py", "engines/strata.py", "engines/llama_cpp.py", "tools/sign_release.py",
             "installer/AgentStart.iss", "installer/version.iss", ".github/workflows/ci.yml", "update_sig.py"],
       "protected files are found (either slash), once each; look-alikes, tests and docs are not")
    eq(pc.protected([]), [], "no change, nothing to review")
    check(all(why and why[0].islower() and not why.endswith(".") for _p, why in pc.PROTECTED),
          "every entry says why, as a phrase")
    eq(len({p for p, _w in pc.PROTECTED}), len(pc.PROTECTED), "no pattern is listed twice")


def test_patterns_name_real_files() -> None:
    tracked = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
               if p.is_file() and not ({".venv", "build", "dist", ".git", "__pycache__"} & set(p.relative_to(ROOT).parts))]
    dead = [pat for pat, _w in pc.PROTECTED if not any(fnmatch.fnmatchcase(t, pat) for t in tracked)]
    eq(dead, [], "every pattern matches a file that exists (a renamed file must be renamed here too)")
    must = ["detector.py", "server_manager.py", "engines/base.py", "engines/strata.py", "engines/llama_cpp.py",
            "launcher_bat.py", "updater.py", "update_sig.py", "hf_download.py", "hermes_alias.py", "toasts.py"]
    eq([m for m in must if not pc.protected([m])], [], "the stop path, the updater, the downloader and the signer are covered")


def test_agents_md_in_step() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `([^`]+)` \| (.+?) \|$", text, flags=re.M)
    eq(rows, [(p, w) for p, w in pc.PROTECTED], "AGENTS.md shows the same files and reasons, in the same order")
    check("tools\\protected_changes.py" in text, "AGENTS.md names the tool")


def main() -> int:
    for fn in (test_matching, test_patterns_name_real_files, test_agents_md_in_step):
        print(f"-- {fn.__name__}")
        fn()
    print(f"\nRESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
