r"""Unit tests for launcher_bat (the run-*.bat parser). Run: .venv\Scripts\python.exe tests\test_launcher_bat.py

Reads the real Strata run-*.bat files when the install is present (read-only; nothing is executed).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_bat as lb  # noqa: E402
from launcher_bat import LauncherError  # noqa: E402

REAL_DIR = r"C:\Users\<user>\Downloads\Strata-main\Strata-0.2.10"
REAL_BATS = ["run-iq3_s.bat", "run-iq3_xxs.bat", "run-swift-iq2_xs.bat", "run-swift-iq3_s.bat",
             "run-swift-iq3_xxs.bat"]
PASSES, FAILS = 0, []


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"[PASS] {msg}")
    else:
        FAILS.append(msg)
        print(f"[FAIL] {msg}")


def rejects(text: str, bat: str, needle: str, msg: str) -> None:
    try:
        lb.parse_bat_text(text, bat)
    except LauncherError as exc:
        check(needle in str(exc), f"{msg} ({exc})")
    else:
        check(False, f"{msg} (no LauncherError)")


S = r"C:\Strata"
BAT = S + r"\run-x.bat"
PYEXE = S + r"\.venv\Scripts\python.exe"
SERVER = S + r"\serve\server.py"


def test_real_bats() -> None:
    if not os.path.isdir(REAL_DIR):
        print(f"[SKIP] real Strata folder not present: {REAL_DIR}")
        return
    listed = [os.path.basename(p) for p in lb.list_launchers(REAL_DIR)]
    check(listed == sorted(REAL_BATS, key=str.casefold), f"list_launchers = exactly the 5 run-*.bat ({listed})")
    check(not any(n.upper() in ("BENCH.BAT", "SETUP.BAT", "START-HERE.BAT", "UPDATE.BAT") for n in listed),
          "BENCH/SETUP/START-HERE/UPDATE.bat are not listed")
    for name in REAL_BATS:
        info = lb.parse_bat(os.path.join(REAL_DIR, name))
        stem = name[len("run-"):-len(".bat")]
        ok = (info.argv[0].lower() == (REAL_DIR + r"\.venv\Scripts\python.exe").lower()
              and info.argv[1].lower() == (REAL_DIR + r"\serve\server.py").lower()
              and "--open" not in info.argv and info.port == 8080
              and info.config_path and info.config_path.lower() == (REAL_DIR + rf"\strata-{stem}.json").lower()
              and info.strata_dir.lower() == REAL_DIR.lower() and info.cwd.lower() == REAL_DIR.lower()
              and info.endpoint_url == "http://127.0.0.1:8080/v1" and info.model_name
              and list(info.argv[2:]) == ["--engine", "strata", "--config", info.config_path, "--port", "8080"])
        check(bool(ok), f"real {name}: {info.summary()}")
        check(lb.missing_files(info) is None, f"real {name}: python, server.py and config exist")
    info = lb.parse_bat(os.path.join(REAL_DIR, "run-swift-iq3_s.bat"))
    check(info.model_name == "swift-1.5-iq3_s", f"model name comes from the config json ({info.model_name})")
    # v1.0's hand-written launch_cmd must equal what the parser derives from the same .bat
    v1 = [REAL_DIR + r"\.venv\Scripts\python.exe", REAL_DIR + r"\serve\server.py", "--engine", "strata", "--config",
          REAL_DIR + r"\strata-swift-iq3_s.json", "--port", "8080"]
    check(list(info.argv) == v1, "parsed argv == the v1.0 hardcoded launch_cmd")
    for other in ("BENCH.bat", "SETUP.bat", "START-HERE.bat", "UPDATE.bat"):
        path = os.path.join(REAL_DIR, other)
        if os.path.isfile(path):
            try:
                lb.parse_bat(path)
                check(False, f"{other} must be rejected")
            except LauncherError as exc:
                check("does not start Strata" in str(exc), f"{other} rejected ({exc})")


def test_synthetic() -> None:
    plain = f'@echo off\r\r\ntitle x\r\r\ncd /d "{S}"\r\r\n"{PYEXE}" "{SERVER}" "--engine" "strata" "--config" ' \
            f'"{S}\\strata-x.json" "--port" "8123" "--open"\r\r\nif errorlevel 1 pause\r\r\n'
    info = lb.parse_bat_text(plain, BAT)
    check(info.argv == (PYEXE, SERVER, "--engine", "strata", "--config", S + r"\strata-x.json", "--port", "8123"),
          f"quoted args + \\r\\r\\n line ends; --open dropped ({info.argv})")
    check(info.port == 8123 and info.endpoint_url == "http://127.0.0.1:8123/v1", "port + endpoint from --port")
    check(info.cwd == S and info.strata_dir == S, "cd /d honoured; strata_dir = parent of serve\\")
    check(info.model_name == "x" and info.summary() == "x · strata-x.json · port 8123",
          f"model falls back to the config file name ({info.summary()})")

    cont = ("@echo off\n"
            "cd /d \"%~dp0\"\n"
            "\".venv\\Scripts\\python.exe\" serve\\server.py ^\n"
            "    --engine strata ^\n"
            "    --config strata-y.json ^\n"
            "    --open --port=9001\n")
    info = lb.parse_bat_text(cont, BAT)
    check(info.argv == (PYEXE, SERVER, "--engine", "strata", "--config", S + r"\strata-y.json", "--port=9001"),
          f"^ continuation + %~dp0 + relative paths resolved against the bat dir ({info.argv})")
    check(info.port == 9001 and info.config_path == S + r"\strata-y.json", "--port=N form and relative --config")

    setv = ("@echo off\nsetlocal\nrem a comment mentioning python serve\\server.py --port 1\n"
            ":: another comment\n"
            "set ROOT=%~dp0\n"
            "set \"PY=%ROOT%.venv\\Scripts\\python.exe\"\n"
            "set PORT=8765\n"
            "pushd \"%ROOT%\"\n"
            "call \"%PY%\" -u \"%ROOT%serve\\server.py\" --engine strata --config \"%ROOT%strata-z.json\" "
            "--port %PORT% --open > \"%ROOT%out.log\" 2>&1\n")
    info = lb.parse_bat_text(setv, BAT)
    check(info.argv == (PYEXE, "-u", SERVER, "--engine", "strata", "--config", S + r"\strata-z.json", "--port", "8765"),
          f"set VAR= / set \"VAR=\" / %VAR% / call / -u / redirection / comments ({info.argv})")
    check(info.port == 8765 and info.cwd == S, "port via %PORT%, cwd via pushd")

    other_dir = ("@echo off\ncd /d \"D:\\Engines\\Strata-9\"\nstart \"\" /b .venv\\Scripts\\python.exe "
                 "serve\\server.py --config cfg\\m.json\n")
    info = lb.parse_bat_text(other_dir, r"C:\Users\me\Desktop\go.bat")
    check(info.argv == (r"D:\Engines\Strata-9\.venv\Scripts\python.exe", r"D:\Engines\Strata-9\serve\server.py",
                        "--config", r"D:\Engines\Strata-9\cfg\m.json"),
          f"bat outside the Strata folder: paths resolve against its cd /d ({info.argv})")
    check(info.port == 8080 and info.strata_dir == r"D:\Engines\Strata-9" and info.bat_name == "go.bat",
          "default port 8080; strata_dir from server.py, not from the bat's folder")

    chained = f'@cd /d "{S}" && "{PYEXE}" "{SERVER}" --port 8500 --open || pause\n'
    info = lb.parse_bat_text(chained, BAT)
    check(info.argv == (PYEXE, SERVER, "--port", "8500") and info.cwd == S, f"cd && python || pause ({info.argv})")

    paren = f'if exist "{PYEXE}" ("{PYEXE}" "{SERVER}" --open --port 8600)\n'
    check(lb.parse_bat_text(paren, BAT).argv == (PYEXE, SERVER, "--port", "8600"), "if (...) block form")

    spaces = ('cd /d "C:\\Program Files\\My Strata"\n'
              '"C:\\Program Files\\My Strata\\.venv\\Scripts\\python.exe" "C:\\Program Files\\My Strata\\serve\\server.py" '
              '--config "C:\\Program Files\\My Strata\\strata a.json"\n')
    info = lb.parse_bat_text(spaces, BAT)
    check(info.argv[1] == r"C:\Program Files\My Strata\serve\server.py"
          and info.config_path == r"C:\Program Files\My Strata\strata a.json", "paths with spaces stay one argument")

    fwd = 'python.exe C:/Strata/serve/server.py --port 8080\n'
    info = lb.parse_bat_text(fwd, BAT)
    check(info.argv[1] == SERVER and os.path.basename(info.argv[0]).lower().startswith("python"),
          f"forward slashes + bare python ({info.argv[:2]})")


def test_rejections() -> None:
    rejects("@echo off\necho hello\npause\n", BAT, "does not start Strata", "no server line rejected")
    rejects(f'"{PYEXE}" "{S}\\bench\\server.py" --port 8080\n', BAT, "does not start Strata",
            "server.py outside serve\\ rejected")
    rejects(f'"{PYEXE}" "{S}\\notserve\\server.py"\n', BAT, "does not start Strata", "…\\notserve\\server.py rejected")
    rejects(f'rem "{PYEXE}" "{SERVER}"\n', BAT, "does not start Strata", "commented-out launch line rejected")
    rejects(f'notepad.exe "{SERVER}"\n', BAT, "does not start Strata", "server.py opened by a non-python program")
    rejects(f'echo "{PYEXE}" > "{SERVER}"\n', BAT, "does not start Strata", "server.py only as a redirection target")
    rejects(f'"{PYEXE}" "{SERVER}" --port abc\n', BAT, "not a valid port", "non-numeric port rejected")
    rejects(f'"{PYEXE}" "{SERVER}" --port 70000\n', BAT, "not a valid port", "out-of-range port rejected")
    try:
        lb.parse_bat(r"C:\nope\missing-launcher.bat")
        check(False, "missing file must raise")
    except LauncherError as exc:
        check("Cannot read" in str(exc), f"unreadable .bat -> clear message ({exc})")
    try:
        lb.parse_bat(r"C:\Windows\notepad.exe")
        check(False, "non-.bat must raise")
    except LauncherError as exc:
        check("Choose a .bat" in str(exc), "non-.bat path rejected without reading it")


def test_tokenizer() -> None:
    check(lb.tokenize(r'"a b" c "d\\" e') == ["a b", "c", "d\\", "e"], "MSVCRT rules: trailing \\\\ inside quotes")
    check(lb.tokenize(r'x "say \"hi\"" ""') == ["x", 'say "hi"', ""], "escaped quotes + empty argument")
    check(lb.split_commands('a "b & c" & d > out.txt 2>&1 | e') == ['a "b & c"', "d", "e"],
          "split at unquoted & |, redirections dropped")
    check(lb.logical_lines("a ^\r\n b\r\r\nc^^\nd") == ["a  b", "c^^", "d"], "continuations; ^^ is not one")
    check(lb.expand_vars("%~dp0x %~nx0 %~f0 %NOPE_ZZ% 100%% %*", r"C:\d\run.bat", {}, r"C:\d")
          == r"C:\d\x run.bat C:\d\run.bat  100% ", "%~dp0 / %~nx0 / %~f0 / undefined / %% / %*")


def test_files_and_config() -> None:
    with tempfile.TemporaryDirectory(prefix="hc-bat-") as tmp:
        sdir = os.path.join(tmp, "Strata X")
        os.makedirs(os.path.join(sdir, "serve"))
        os.makedirs(os.path.join(sdir, ".venv", "Scripts"))
        for rel in (r"serve\server.py", r".venv\Scripts\python.exe"):
            Path(sdir, rel).write_text("", encoding="utf-8")
        Path(sdir, "strata-a.json").write_text(json.dumps({"model_name": "model-a"}), encoding="utf-8")
        Path(sdir, "strata-b.json").write_text(json.dumps({"model": "model-b", "model_name": "ignored"}),
                                               encoding="utf-8")
        Path(sdir, "strata-c.json").write_text("{not json", encoding="utf-8")
        body = '@echo off\ncd /d "%~dp0"\n.venv\\Scripts\\python serve\\server.py --config strata-{0}.json --port 810{1}\n'
        for i, n in enumerate("abc"):
            Path(sdir, f"run-{n}.bat").write_text(body.format(n, i), encoding="utf-8")
        Path(sdir, "run-missing.bat").write_text(body.format("zzz", 9), encoding="utf-8")
        Path(sdir, "SETUP.bat").write_text("@echo setup\n", encoding="utf-8")
        Path(sdir, "run-notes.txt").write_text("x", encoding="utf-8")
        os.makedirs(os.path.join(sdir, "run-dir.bat"))

        names = [os.path.basename(p) for p in lb.list_launchers(sdir)]
        check(names == ["run-a.bat", "run-b.bat", "run-c.bat", "run-missing.bat"], f"list_launchers ({names})")
        check(lb.list_launchers("") == [] and lb.list_launchers(os.path.join(tmp, "nope")) == [],
              "list_launchers: empty / missing folder -> []")
        a = lb.parse_bat(os.path.join(sdir, "run-a.bat"))
        check(a.model_name == "model-a" and a.port == 8100, f"model_name field ({a.model_name})")
        check(a.argv[0] == os.path.join(sdir, ".venv", "Scripts", "python.exe"),
              f"extension-less relative interpreter resolved to python.exe ({a.argv[0]})")
        check(lb.missing_files(a) is None, "all files present")
        b = lb.parse_bat(os.path.join(sdir, "run-b.bat"))
        check(b.model_name == "model-b", "the config's `model` field wins")
        c = lb.parse_bat(os.path.join(sdir, "run-c.bat"))
        check(c.model_name == "c", f"broken config json -> config file name ({c.model_name})")
        m = lb.parse_bat(os.path.join(sdir, "run-missing.bat"))
        check("Config not found" in (lb.missing_files(m) or ""), f"missing config reported ({lb.missing_files(m)})")

        # config glue + v1.0 backward compatibility
        old = {"strata_dir": sdir, "launcher_bat": "run-a.bat", "launch_cmd": list(a.argv), "port": 8100,
               "endpoint_url": "http://127.0.0.1:8100/v1", "model_name": "model-a", "user_whitelist": ["x.exe"]}
        check(lb.configured_bat(old) == os.path.join(sdir, "run-a.bat"),
              "v1.0 relative launcher_bat resolves against strata_dir")
        check(lb.same_launch(old, a), "v1.0 config (no launch_cwd) already launches the same thing")
        check(not lb.same_launch(old, b), "another launcher is a change")
        check(lb.configured_bat({"launcher_bat": ""}) is None and lb.configured_bat({"launcher_bat": "x.bat"}) is None,
              "no launcher / relative without strata_dir -> None")
        cfg = dict(old)
        lb.apply_to_config(cfg, b)
        check(cfg["launcher_bat"] == b.bat_path and cfg["launch_cmd"] == list(b.argv) and cfg["port"] == 8101
              and cfg["endpoint_url"] == "http://127.0.0.1:8101/v1" and cfg["model_name"] == "model-b"
              and cfg["strata_dir"] == sdir and cfg["launch_cwd"] == sdir and cfg["user_whitelist"] == ["x.exe"],
              "apply_to_config writes the launcher keys and nothing else")
        check(json.loads(json.dumps(cfg)) == cfg, "config stays JSON-serialisable")
        check(lb.configured_bat(cfg) == b.bat_path and lb.same_launch(cfg, b), "round trip")


def main() -> int:
    for fn in (test_real_bats, test_synthetic, test_rejections, test_tokenizer, test_files_and_config):
        print(f"-- {fn.__name__}")
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
            print(FAILS[-1])
    print(f"\nRESULT: {PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
