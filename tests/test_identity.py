"""Regression test for advisor checkpoint-2 BLOCKER #1: the stop/kill path must identify Strata STRICTLY.

A substring match on the joined cmdline used to treat any process that merely *mentions* serve\\server.py as the
Strata server root (and kill it with its whole tree on OFF). Run: .venv\\Scripts\\python.exe tests\\test_identity.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_manager as sm  # noqa: E402

S = r"C:\Users\me\Strata"
CASES = [
    # (description, name, cmdline, expected)
    ("real venv launcher", "python.exe",
     [S + r"\.venv\Scripts\python.exe", S + r"\serve\server.py", "--engine", "strata", "--port", "8080"], True),
    ("real base-interpreter child", "python.exe",
     [r"C:\Users\me\AppData\Local\Python\pythoncore-3.14-64\python.exe", S + r"\serve\server.py", "--port", "8080"],
     True),
    ("pythonw / python3.14 variants", "python3.14.exe", ["python3.14", S + r"\serve\server.py"], True),
    ("forward slashes", "python.exe", ["python", "C:/x/serve/server.py"], True),
    ("hermes chat mentioning the path", "hermes.exe",
     ["hermes", "-p", "korra", "chat", "--oneshot", "-q", r"fix C:\x\serve\server.py please"], False),
    ("bash -c running python", "bash.exe", ["bash", "-c", ".venv/Scripts/python.exe serve/server.py"], False),
    ("cmd /k", "cmd.exe", ["cmd", "/k", r"python C:\x\serve\server.py"], False),
    ("VS Code with the file open", "Code.exe", [r"C:\VSCode\Code.exe", S + r"\serve\server.py"], False),
    ("notepad", "notepad.exe", ["notepad", r"C:\x\serve\server.py"], False),
    ("python but path only inside argv[0]", "python.exe", [r"C:\serve\server.py"], False),
    ("python with server.py elsewhere", "python.exe", ["python", r"C:\x\notserve\server.py"], False),
]


def main() -> int:
    fails = 0
    for desc, name, cmd, want in CASES:
        got = sm._is_strata_cmd(name, cmd)
        ok = got is want
        fails += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {desc}: {got}")
    assert sm._explicit_port(["x", "--port", "8080"]) == 8080 and sm._explicit_port(["x"]) is None
    print("[PASS] _explicit_port")
    print(f"{len(CASES) + 1 - fails}/{len(CASES) + 1} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
