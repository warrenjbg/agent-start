"""Which files of a change need a second, stronger reviewer?

    .venv\\Scripts\\python.exe tools\\protected_changes.py            changes since main (committed or not)
    .venv\\Scripts\\python.exe tools\\protected_changes.py v1.13.0    ... since another branch, tag or commit
    .venv\\Scripts\\python.exe tools\\protected_changes.py --strict   exit 1 when there is any

PROTECTED is the one list (AGENTS.md shows it; tests/test_protected_changes.py keeps the two in step). A file is on
it when a plausible-looking mistake there ends the wrong process, deletes a file the app did not make, runs
something it was only meant to read, or installs something nobody signed. The tool only reads git; it changes
nothing and decides nothing: it tells the person (or model) merging a change what to read line by line.
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (pattern, why). Patterns are fnmatch on the path with forward slashes.
PROTECTED: tuple[tuple[str, str], ...] = (
    ("detector.py", "decides which processes may be closed, and closes them"),
    ("server_manager.py", "starts the engine and decides which process is the engine before stopping it"),
    ("engines/*.py", "each engine's identity check for the stop path, and its launch plan"),
    ("launcher_bat.py", "reads a launcher file the user picked; it must be parsed, never run"),
    ("reopen.py", "starts apps again after a turn off"),
    ("mem_watch.py", "acts by itself when memory runs low"),
    ("game_watch.py", "acts by itself when a game starts or closes"),
    ("idle_watch.py", "turns the engine off by itself"),
    ("watch_controller.py", "carries out what the watchers decide"),
    ("updater.py", "downloads and starts an installer"),
    ("update_sig.py", "decides whether a release is signed by the publisher"),
    ("update_controller.py", "drives the in-app update"),
    ("hf_download.py", "writes, resumes and deletes files in a folder the user also uses"),
    ("hermes_alias.py", "the one place that runs hermes.exe and changes its configuration"),
    ("toasts.py", "passes text to PowerShell"),
    ("tools/sign_release.py", "signs a release"),
    ("tools/check_release_keys.py", "the build's guard against shipping without release keys"),
    ("build.bat", "builds what users install"),
    ("installer/*.iss", "what the installer does on a user's PC"),
    (".github/workflows/*", "what runs on every push"),
)


def protected(paths) -> list[tuple[str, str]]:
    """(path, why) for every path that matches PROTECTED, in the order given, each path once."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in paths:
        p = str(raw).strip().replace("\\", "/")
        if not p or p in seen:
            continue
        seen.add(p)
        for pattern, why in PROTECTED:
            if fnmatch.fnmatchcase(p, pattern):
                out.append((p, why))
                break
    return out


def _git(*args: str) -> list[str]:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()[:200]}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def changed_since(base: str) -> list[str]:
    """Paths that differ from the point where HEAD left `base`: committed, staged, unstaged and new files."""
    paths = _git("diff", "--name-only", f"{base}...HEAD")
    paths += _git("diff", "--name-only", "HEAD")
    paths += _git("ls-files", "--others", "--exclude-standard")
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("base", nargs="?", default="main", help="branch, tag or commit to compare with (default: main)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a protected file changed")
    args = ap.parse_args(argv)
    hits = protected(changed_since(args.base))
    if not hits:
        print(f"No protected file changed since {args.base}.")
        return 0
    print(f"{len(hits)} protected file(s) changed since {args.base}; each needs a line-by-line review by a stronger"
          " reviewer before it is merged:")
    for path, why in hits:
        print(f"  {path}: {why}")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
