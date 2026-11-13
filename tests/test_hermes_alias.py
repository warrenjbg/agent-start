"""Self-check for hermes_alias.py (keeps Hermes' `llama` model alias in step with the app's llama.cpp server). Run:
    .venv\\Scripts\\python.exe tests\\test_hermes_alias.py

Safe by construction: NO process is ever started. Before hermes_alias is imported, subprocess.run, subprocess.Popen
and os.system are replaced by a guard that records the attempt and raises, so even the module's default `run` seam is
the guard; every check drives the module through a recorder (FakeHermes) instead. Nothing reads or writes Hermes'
files. The YAML read-back fixtures are the exact text PyYAML 6.0.3 safe_dump (the formatter of `hermes config get`)
printed for each name, generated once outside this suite; the first one is the real command's observed output.
"""
from __future__ import annotations

import ast
import inspect
import json
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GUARD_HITS: list[str] = []


def _forbidden(*args, **kwargs):
    GUARD_HITS.append(repr(args[:1])[:80])
    raise AssertionError("test_hermes_alias must never start a process")


subprocess.run = _forbidden  # type: ignore[assignment]
subprocess.Popen = _forbidden  # type: ignore[assignment, misc]
os.system = _forbidden  # type: ignore[assignment]

import hermes_alias as ha  # noqa: E402

PASSED = 0
FAILED = 0
URL = "http://127.0.0.1:9931/v1"
EXE_DIR = r"C:\Apps\hermes\bin"
EXE = EXE_DIR + r"\hermes.exe"
def YES(p: str) -> bool:
    return True


REAL_GET = b"model: Qwen3.8-27B-UD-IQ4_XS\r\nprovider: custom\r\nbase_url: http://127.0.0.1:9931/v1\r\n"   # observed

# name -> the `model:` line(s) PyYAML safe_dump printed for it (generated with PyYAML 6.0.3, see the module docstring)
MODEL_LINES = {
    "Qwen3.8-27B-UD-IQ4_XS": "model: Qwen3.8-27B-UD-IQ4_XS",
    "-leading": "model: -leading",
    "--profile=x": "model: --profile=x",
    "-p": "model: -p",
    "[Q4] model": "model: '[Q4] model'",
    "{x}": "model: '{x}'",
    "[a, b]": "model: '[a, b]'",
    "true": "model: 'true'",
    "True": "model: 'True'",
    "123": "model: '123'",
    "007": "model: '007'",
    "1.10": "model: '1.10'",
    "1e3": "model: 1e3",
    "null": "model: 'null'",
    "~": "model: '~'",
    "yes": "model: 'yes'",
    'say "hi"': 'model: say "hi"',
    "it's": "model: it's",
    "back\\slash": "model: back\\slash",
    "C:\\m\\x.gguf": "model: C:\\m\\x.gguf",
    "\xfc\U0001f600": 'model: "\\xFC\\U0001F600"',
    "\u6a21\u578b": 'model: "\\u6A21\\u578B"',
    "a b  c": "model: a b  c",
    "model (copy) #2": "model: 'model (copy) #2'",
    "a: b": "model: 'a: b'",
    "a #b": "model: 'a #b'",
    "x" * 200: "model: " + "x" * 200,
    "word " * 39 + "end": "model: " + " ".join(["word"] * 15) + "\n  " + " ".join(["word"] * 16) + "\n  "
                         + " ".join(["word"] * 8) + " end",
    "\xe9" * 120: 'model: "' + "\\xE9" * 19 + "\\\n" + ("  " + "\\xE9" * 20 + "\\\n") * 5 + '  \\xE9"',
    "=": "model: '='",
    "<<": "model: '<<'",
    ".inf": "model: '.inf'",
    "'quoted'": "model: '''quoted'''",
    '"dq"': "model: '\"dq\"'",
    "%pct": "model: '%pct'",
    "&anchor": "model: '&anchor'",
    "*star": "model: '*star'",
    "!tag": "model: '!tag'",
    "|pipe": "model: '|pipe'",
    ">gt": "model: '>gt'",
    "@at": "model: '@at'",
    "`bt": "model: '`bt'",
    ",comma": "model: ',comma'",
    "?q": "model: ?q",
    ":colon": "model: :colon",
    "- dash": "model: '- dash'",
    "a\xa0b": 'model: "a\\_b"',
    "2024-01-01": "model: '2024-01-01'",
    "0x1F": "model: '0x1F'",
    "12:30": "model: '12:30'",
}
HOSTILE = list(MODEL_LINES)


def get_text(model: str, url: str = URL, provider: str = "custom", eol: str = "\r\n") -> bytes:
    text = MODEL_LINES[model] + "\nprovider: " + provider + "\nbase_url: " + url + "\n"
    return text.replace("\n", eol).encode("utf-8")


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {msg}")
    else:
        FAILED += 1
        print(f"[FAIL] {msg}")


class FakeHermes:
    """A subprocess.run stand-in: records every call and answers like hermes.exe would, never starts anything.

    SET: exit `set_code` (or raises `set_exc`). GET: `get_out` bytes if given, else the YAML for the model the SET sent
    (exit `get_code`, or raises `get_exc`)."""

    def __init__(self, set_code=0, set_exc=None, set_stderr=b"", get_out=None, get_code=0, get_exc=None,
                 get_stderr=b""):
        self.calls: list[tuple[list, dict]] = []
        self.set_code, self.set_exc, self.set_stderr = set_code, set_exc, set_stderr
        self.get_out, self.get_code, self.get_exc, self.get_stderr = get_out, get_code, get_exc, get_stderr
        self.sent: dict | None = None

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), dict(kw)))
        if argv[1:3] == ["config", "set"]:
            if self.set_exc is not None:
                raise self.set_exc
            self.sent = json.loads(argv[-1])
            return SimpleNamespace(returncode=self.set_code, stdout=b"\xe2\x9c\x93 Set ...", stderr=self.set_stderr)
        if self.get_exc is not None:
            raise self.get_exc
        out = self.get_out
        if out is None:
            out = get_text(self.sent["model"], self.sent["base_url"]) if self.sent else b""
        return SimpleNamespace(returncode=self.get_code, stdout=out, stderr=self.get_stderr)


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


LOG = Records()
_logger = logging.getLogger("agent_start")
_logger.addHandler(LOG)
_logger.setLevel(logging.DEBUG)
_logger.propagate = False


def go(model, url=URL, fake=None, exe: str | None = EXE, exists=YES, timeout_s=ha.TIMEOUT_S):
    fake = fake if fake is not None else FakeHermes()
    return ha.set_alias(model, url, exe=exe, run=fake, exists=exists, timeout_s=timeout_s), fake


# ------------------------------------------------------------------------------------------------ find_hermes
def t_find_hermes() -> None:
    la = r"C:\Profiles\u1\AppData\Local"
    la_exe = la + r"\hermes\bin\hermes.exe"
    tools_exe = r"C:\Tools\hermes.exe"
    env = {"LOCALAPPDATA": la, "PATH": r"C:\Tools;C:\Windows"}
    check(ha.find_hermes(env, lambda p: p in (la_exe, tools_exe)) == la_exe, "LOCALAPPDATA\\hermes\\bin wins")
    check(ha.find_hermes(env, lambda p: p == tools_exe) == tools_exe, "falls back to a PATH folder")
    probes: list[str] = []
    out = ha.find_hermes({"LOCALAPPDATA": la, "PATH": r"C:\Tools;C:\Other"}, lambda p: probes.append(p) or False)
    check(out is None and bool(probes) and all(os.path.basename(p) == "hermes.exe" for p in probes),
          f"only hermes.exe is ever looked for ({len(probes)} probes)")
    def only_cmd(p: str) -> bool:
        return p.casefold().endswith((".cmd", ".bat", ".ps1"))

    check(ha.find_hermes(env, only_cmd) is None, "only a hermes.cmd / .bat / .ps1 present -> None")
    check(ha.find_hermes({"PATH": r"bin;.\tools;..\x;C:tools;\rooted"}, YES) is None,
          "relative, drive-relative and rooted-without-drive PATH entries are ignored")
    check(ha.find_hermes({"LOCALAPPDATA": r"AppData\Local", "PATH": ""}, YES) is None, "a relative LOCALAPPDATA: ignored")
    check(ha.find_hermes({"PATH": r'"C:\Program Files\Hermes\bin";C:\Windows'}, YES)
          == r"C:\Program Files\Hermes\bin\hermes.exe", "a quoted PATH entry is unquoted")
    check(ha.find_hermes({"PATH": r'C:\Pro"gram\bin'}, YES) is None, "an entry with a stray quote is skipped")
    check(ha.find_hermes({"PATH": ' ; ;"";' + r"C:\Tools"}, lambda p: p == tools_exe) == tools_exe,
          "empty and blank entries are skipped")
    check(ha.find_hermes({"LocalAppData": la, "Path": r"C:\Tools"}, lambda p: p == la_exe) == la_exe,
          "variable names in other letter case")
    check(ha.find_hermes({"Path": r"C:\Tools"}, lambda p: p == tools_exe) == tools_exe, "'Path' spelling")
    check(ha.find_hermes({}, YES) is None, "empty environment -> None")

    def boom(p):
        raise OSError("disk gone")
    try:
        out = ha.find_hermes(env, boom)
        check(out is None, "an exists() that raises -> None, no exception")
    except Exception as exc:  # noqa: BLE001
        check(False, f"find_hermes raised {exc!r}")
    check(ha.find_hermes({"PATH": 5}, YES) is None, "a non-text PATH -> None")  # type: ignore[dict-item]


# ------------------------------------------------------------------------------------------------ validators
def t_validators() -> None:
    for name in HOSTILE + ["a", "x" * 200, "\U0001f600", "Q4_K_M (2)", "\u00ad", "model-\u200b-zw"]:
        check(ha.validate_model(name) is None, f"accepted model name {ascii(name)[:60]}")
    refused = [
        (None, "None"), (123, "an int"), (b"bytes", "bytes"), (["x"], "a list"), ("", "empty"), ("   ", "spaces"),
        (" lead", "leading space"), ("trail ", "trailing space"), ("\u3000x", "leading ideographic space"),
        ("x\xa0", "trailing no-break space"), ("a\nb", "a newline"), ("a\rb", "a CR"), ("a\tb", "a tab"),
        ("a\x00b", "a NUL"), ("\x00", "only a NUL"), ("a\x1bb", "ESC"), ("a\x7fb", "DEL"), ("a\x85b", "NEL (C1)"),
        ("a\x9fb", "C1 control"), ("a\u2028b", "LINE SEPARATOR"), ("a\u2029b", "PARAGRAPH SEPARATOR"),
        ("a\ud800b", "a lone surrogate"), ("\ufeffx", "a BOM"), ("a\uffffb", "U+FFFF"), ("x" * 201, "201 characters"),
        ("x" * 5000, "5000 characters"),
    ]
    for value, why in refused:
        reason = ha.validate_model(value)
        check(isinstance(reason, str) and bool(reason), f"refused model ({why}): {reason}")
    check("NUL" in (ha.validate_model("a\x00b") or ""), "the NUL reason names NUL")
    good_urls = [URL, "http://127.0.0.1:1/v1", "http://127.0.0.1:65535/v1", "http://127.0.0.1:8081/v1"]
    for url in good_urls:
        check(ha.validate_base_url(url) is None, f"accepted base_url {url}")
    bad_urls = [None, 9931, b"http://127.0.0.1:9931/v1", "", "http://127.0.0.1:0/v1", "http://127.0.0.1:65536/v1",
                "http://127.0.0.1:99999/v1", "http://127.0.0.1:09931/v1", "http://127.0.0.1:-1/v1",
                "http://127.0.0.1/v1", "http://127.0.0.1:/v1", "http://127.0.0.1:9931/v1/", "http://127.0.0.1:9931",
                "http://127.0.0.1:9931/v2", "https://127.0.0.1:9931/v1", "HTTP://127.0.0.1:9931/v1",
                "http://localhost:9931/v1", "http://10.0.0.5:9931/v1", "http://127.0.0.2:9931/v1",
                "http://[::1]:9931/v1", "http://127.0.0.1:9931/v1\n", " http://127.0.0.1:9931/v1",
                "http://127.0.0.1:9931/v1?x=1", "http://127.0.0.1:9931/v1#x", "http://user@127.0.0.1:9931/v1",
                "http://127.0.0.1:\u0669\u0669\u0663\u0661/v1", "http://127.0.0.1:\uff19\uff19\uff13\uff11/v1", "http://127.0.0.1:9931/v1\x00"]
    for url in bad_urls:
        reason = ha.validate_base_url(url)
        check(isinstance(reason, str) and bool(reason), f"refused base_url {ascii(url)[:50]}")


def t_entry_json() -> None:
    for name in HOSTILE:
        text = ha.entry_json(name, URL)
        obj = json.loads(text)
        check(obj == {"model": name, "provider": "custom", "base_url": URL} and list(obj) == ["model", "provider",
              "base_url"] and all(type(v) is str for v in obj.values()),
              f"entry_json round-trips exactly, keys in order: {ascii(name)[:50]}")
    text = ha.entry_json("\xfc\U0001f600", URL)
    check("\U0001f600" in text and "\\u" not in text, f"ensure_ascii=False: the emoji stays literal ({ascii(text)})")
    check(ha.entry_json("m", URL) == '{"model": "m", "provider": "custom", "base_url": "' + URL + '"}',
          "exact text for a simple name")
    check(text.startswith("{") and "\n" not in ha.entry_json("x" * 200, URL), "one line, starts with a brace")
    for model, url in (("", URL), (" x", URL), ("a\nb", URL), ("x" * 201, URL), ("m", "http://localhost:1/v1"),
                       (None, URL), ("m", None)):
        try:
            ha.entry_json(model, url)  # type: ignore[arg-type]
            check(False, f"entry_json({ascii(model)[:20]}, {ascii(url)[:30]}) should raise ValueError")
        except ValueError:
            check(True, f"entry_json({ascii(model)[:20]}, {ascii(url)[:30]}) raises ValueError")


# ------------------------------------------------------------------------------------------------ set_alias
def t_set_happy() -> None:
    saved = dict(os.environ)
    try:
        os.environ["LLAMA_ARG_MODEL"] = "x"
        os.environ["Llama_Cache"] = "y"
        os.environ["AGENTSTART_TEST_FLAG"] = "1"
        os.environ["HC_TEST_KEEP_ME"] = "kept"
        os.environ.pop("HERMES_HOME", None)
        res, fake = go("Qwen3.8-27B-UD-IQ4_XS")
        check(res.ok and res.changed, f"happy path: ok and changed ({res})")
        check(res.note == "Hermes alias llama now points at Qwen3.8-27B-UD-IQ4_XS", f"note: {res.note!r}")
        check(len(fake.calls) == 2, f"exactly two runs ({len(fake.calls)})")
        sargv, skw = fake.calls[0]
        gargv, gkw = fake.calls[1]
        want = [EXE, "config", "set", "--", "model_aliases.llama", ha.entry_json("Qwen3.8-27B-UD-IQ4_XS", URL)]
        check(len(sargv) == 6, f"SET argv has six elements ({len(sargv)})")
        for i, (got, exp) in enumerate(zip(sargv, want)):
            check(got == exp, f"SET argv[{i}] = {exp[:60]!r}")
        check(sargv[3] == "--" and sargv.index("--") < sargv.index("model_aliases.llama"), "'--' before the key")
        check(json.loads(sargv[5]) == {"model": "Qwen3.8-27B-UD-IQ4_XS", "provider": "custom", "base_url": URL},
              "the JSON entry is ONE argument")
        check(gargv == [EXE, "config", "get", "model_aliases.llama"], f"GET argv pinned ({gargv})")
        for name, kw in (("SET", skw), ("GET", gkw)):
            check(kw.get("shell") is False, f"{name}: shell=False")
            check(kw.get("stdin") is subprocess.DEVNULL, f"{name}: stdin=DEVNULL")
            check(kw.get("stdout") is subprocess.PIPE and kw.get("stderr") is subprocess.PIPE,
                  f"{name}: stdout and stderr captured")
            check("text" not in kw and "encoding" not in kw and "universal_newlines" not in kw,
                  f"{name}: bytes, decoded by the module")
            check(kw.get("creationflags") == 0x08000000, f"{name}: CREATE_NO_WINDOW")
            check(kw.get("timeout") == 20.0 and kw.get("check") is False, f"{name}: timeout 20 s, check=False")
            check(os.path.normcase(kw.get("cwd") or "") == os.path.normcase(EXE_DIR), f"{name}: cwd = the exe's folder")
            env = kw.get("env")
            check(isinstance(env, dict), f"{name}: an explicit environment")
            env = env or {}
            check(not any(k.upper().startswith("LLAMA_") for k in env), f"{name}: no LLAMA_ variable")
            check(not any(k.upper().startswith("AGENTSTART_") for k in env), f"{name}: no AGENTSTART_ variable")
            check("HERMES_HOME" not in {k.upper() for k in env}, f"{name}: no HERMES_HOME injected")
            check(env.get("HC_TEST_KEEP_ME") == "kept", f"{name}: the rest of the app's environment is passed on")
            check(set(env) == {k for k in os.environ if not k.upper().startswith(("LLAMA_", "AGENTSTART_"))},
                  f"{name}: nothing added")
        os.environ["HERMES_HOME"] = r"C:\Somewhere\profiles\mine"
        _, fake = go("m")
        check(all(c[1]["env"].get("HERMES_HOME") == r"C:\Somewhere\profiles\mine" for c in fake.calls),
              "the user's own HERMES_HOME is passed on unchanged, not overridden")
    finally:
        os.environ.clear()
        os.environ.update(saved)
    for name in HOSTILE:
        res, fake = go(name)
        ok = res.ok and res.changed and len(fake.calls) == 2 and fake.calls[0][0][5] == ha.entry_json(name, URL)
        check(ok, f"hostile name written and confirmed: {ascii(name)[:50]} ({res.note[:60]!r})")
    res, fake = go("m", fake=FakeHermes(get_out=get_text("Qwen3.8-27B-UD-IQ4_XS")))
    check(not res.ok, "a read-back showing the old model is not taken as success")
    res, fake = go("Qwen3.8-27B-UD-IQ4_XS", fake=FakeHermes(get_out=REAL_GET))
    check(res.ok and res.changed, "the observed real output confirms the matching write")
    for t_in, t_out in ((600.0, 60.0), (60.0, 60.0), (0.5, 0.5), (5, 5.0)):
        _, fake = go("m", timeout_s=t_in)
        check(all(c[1]["timeout"] == t_out for c in fake.calls) and len(fake.calls) == 2,
              f"timeout_s {t_in} -> {t_out} per run")
    _, fake = go("m", exe=r"C:\Apps\HERMES\BIN\HERMES.EXE")
    check(len(fake.calls) == 2, "HERMES.EXE in upper case is accepted")
    seen: list[str] = []
    _, fake = go("m", exists=lambda p: seen.append(p) or True)
    check(seen == [EXE], f"the existence check is made on the program itself ({len(seen)})")
    res, fake = go("m", exe=None, exists=YES)
    found = ha.find_hermes(exists=YES)
    check(found is not None and len(fake.calls) == 2 and fake.calls[0][0][0] == found,
          "exe=None: the program find_hermes resolves is used")


def t_refusals() -> None:
    bad = [("", URL), ("   ", URL), (" x", URL), ("a\nb", URL), ("a\x00b", URL), ("x" * 201, URL), (None, URL),
           (5, URL), ("m", "http://localhost:9931/v1"), ("m", "http://127.0.0.1:0/v1"), ("m", ""), ("m", None),
           ("m", "http://127.0.0.1:9931/v1 --profile=x")]
    for model, url in bad:
        res, fake = go(model, url)
        check(not res.ok and not res.changed and not fake.calls and res.note.startswith("Hermes alias not updated: "),
              f"refused, nothing run: {ascii(model)[:20]} {ascii(url)[:40]} ({res.note!r})")
    for t in (0, -1, float("nan"), float("inf"), True, "20", None):
        res, fake = go("m", timeout_s=t)  # type: ignore[arg-type]
        check(not res.ok and not fake.calls, f"bad time limit {t!r}: refused, nothing run")
    exes = [r"C:\Apps\hermes\bin\hermes.cmd", r"C:\Apps\hermes\bin\hermes.bat", r"C:\Apps\hermes\bin\hermes.ps1",
            r"C:\Apps\hermes\bin\hermes.exe.cmd", r"C:\Apps\hermes\bin\hermes.exe ", r"C:\Apps\hermes\bin\hermes",
            r"C:\Apps\hermes\bin\hermes-acp.exe", r"C:\Apps\hermes\bin\python.exe", "hermes.exe", r"bin\hermes.exe",
            r".\hermes.exe", "C:hermes.exe", r"\Apps\hermes.exe", "", "C:\\Apps\\hermes\\bin\\", EXE + "\x00",
            5, b"C:\\x\\hermes.exe"]
    for exe in exes:
        res, fake = go("m", exe=exe)
        check(not res.ok and not fake.calls and "not updated" in res.note, f"exe {ascii(exe)[:50]}: nothing runs")
    res, fake = go("m", exists=lambda p: False)
    check(not res.ok and not fake.calls and "not found" in res.note, f"missing hermes.exe: nothing runs ({res.note})")
    res, fake = go("m", exe=None, exists=lambda p: False)
    check(not res.ok and not fake.calls and "not found" in res.note, "exe=None and none found: nothing runs")
    n = len(LOG.records)
    go("", URL)
    check(any(r.levelno >= logging.WARNING and "not updated" in r.getMessage() for r in LOG.records[n:]),
          "a refusal is logged as a warning with its reason")


def t_set_failures() -> None:
    for code in (1, 2):
        stderr = (b"\x1b[31m\xe2\x9c\x97 Your settings file has a formatting error\x07, so this change was not saved"
                  b"\x1b[0m\r\nsecond line\r\n")
        res, fake = go("m", fake=FakeHermes(set_code=code, set_stderr=stderr))
        check(len(fake.calls) == 1 and not res.ok and not res.changed, f"set exit {code}: one run, ok False")
        check(f"code {code}" in res.note and "Your settings file has a formatting error" in res.note
              and "second line" not in res.note, f"note has the code and the first stderr line ({res.note!r})")
        check(not any(ord(c) < 32 or ord(c) == 127 for c in res.note) and "[31m" not in res.note,
              "control characters and colour codes removed")
    res, _ = go("m", fake=FakeHermes(set_code=1, set_stderr=b"E" * 500 + b"\r\nmore"))
    stderr_part = res.note.split("(", 1)[1] if "(" in res.note else ""
    check(stderr_part.count("E") == 200, f"at most 200 characters of stderr ({stderr_part.count('E')})")
    res, _ = go("m", fake=FakeHermes(set_code=1, set_stderr=b"\xff\xfebad\r\n"))
    check("code 1" in res.note and "bad" in res.note, f"undecodable stderr is replaced, not fatal ({res.note!r})")
    res, _ = go("m", fake=FakeHermes(set_code=1, set_stderr=b""))
    check(res.note == "Hermes alias not updated: hermes exited with code 1", f"no stderr: just the code ({res.note!r})")
    res, _ = go("m", fake=FakeHermes(set_code=1, set_stderr=b"x" * 100_000))
    check(not res.ok and len(res.note) < 400, "a huge stderr does not make a huge note")
    for exc, why, wording in ((subprocess.TimeoutExpired(["x"], 20), "timeout", "may be wrong"),
                              (FileNotFoundError(2, "gone"), "missing exe", "not updated"),
                              (PermissionError(13, "denied"), "access denied", "not updated"),
                              (OSError(193, "not a win32 app"), "bad exe", "not updated"),
                              (ValueError("embedded null"), "ValueError", "not updated"),
                              (RuntimeError("boom"), "any other error", "")):
        try:
            res, fake = go("m", fake=FakeHermes(set_exc=exc))
        except Exception as e:  # noqa: BLE001
            check(False, f"set raising {why}: set_alias raised {e!r}")
            continue
        check(not res.ok and not res.changed and len(fake.calls) == 1 and wording in res.note,
              f"set raising {why}: ok False, one run, no exception ({res.note!r})")
    res, _ = go("m", fake=lambda *a, **k: 5)
    check(not res.ok, f"a run seam returning junk -> ok False ({res.note!r})")


def t_readback() -> None:
    cases = [
        ("123", FakeHermes(get_out=b"model: 123\r\nprovider: custom\r\nbase_url: " + URL.encode() + b"\r\n"),
         "model coerced to a number"),
        ("true", FakeHermes(get_out=b"model: true\nprovider: custom\nbase_url: " + URL.encode()), "model coerced to bool"),
        ("null", FakeHermes(get_out=b"model: null\nprovider: custom\nbase_url: " + URL.encode()), "model became null"),
        ("m", FakeHermes(get_out=b"model: m\nprovider: custom\nbase_url: http://127.0.0.1:9932/v1"), "other base_url"),
        ("m", FakeHermes(get_out=b"model: m\nbase_url: " + URL.encode()), "provider missing"),
        ("m", FakeHermes(get_out=b"model: m\nprovider: openai\nbase_url: " + URL.encode()), "another provider"),
        ("m", FakeHermes(get_out=b"model: m \nprovider: custom\nbase_url: " + URL.encode() + b"\n"),
         "trailing blanks are not part of the value: still m -> matches"),
        ("m", FakeHermes(get_out=b"model: M\nprovider: custom\nbase_url: " + URL.encode()), "model in other case"),
        ("m", FakeHermes(get_out=b"Traceback (most recent call last):\n  File x\nKeyError: 'model'\n"), "junk"),
        ("m", FakeHermes(get_out=b""), "empty output"),
        ("m", FakeHermes(get_code=1, get_stderr=b"Config key not set: model_aliases.llama\r\n"), "GET exit 1"),
        ("m", FakeHermes(get_exc=subprocess.TimeoutExpired(["x"], 20)), "GET timed out"),
        ("m", FakeHermes(get_exc=OSError(5, "denied")), "GET could not start"),
        ("m", FakeHermes(get_exc=RuntimeError("boom")), "GET raised something else"),
    ]
    for model, fake, why in cases:
        res, _ = go(model, fake=fake)
        if why.endswith("matches"):
            check(res.ok and res.changed, f"read-back: {why}")
            continue
        check(not res.ok and not res.changed and len(fake.calls) == 2, f"read-back {why}: ok False after two runs")
        check(res.note.startswith("Hermes alias llama may be wrong: ") and ha.CHECK_COMMAND in res.note,
              f"read-back {why}: 'may be wrong' note naming the check command ({res.note!r})")
    check(ha.CHECK_COMMAND == "hermes config get model_aliases.llama", "the check command shown to the user")


# ------------------------------------------------------------------------------------------------ parsing
def t_parse() -> None:
    want = {"model": "Qwen3.8-27B-UD-IQ4_XS", "provider": "custom", "base_url": URL}
    check(ha.parse_get_output(REAL_GET.decode()) == want, "the real observed output (CRLF)")
    check(ha.parse_get_output(REAL_GET) == want, "...given as bytes")
    check(ha.parse_get_output(REAL_GET.decode().replace("\r\n", "\n")) == want, "...with LF line ends")
    check(ha.parse_get_output("\ufeff" + REAL_GET.decode()) == want, "...with a BOM")
    for name in HOSTILE:
        got = ha.parse_get_output(get_text(name).decode())
        check(got == {"model": name, "provider": "custom", "base_url": URL}, f"PyYAML output for {ascii(name)[:50]}")
    variants = [
        ("model: 'Qwen3.8-27B-UD-IQ4_XS'\nprovider: 'custom'\nbase_url: 'http://127.0.0.1:9931/v1'", "single-quoted"),
        ('model: "Qwen3.8-27B-UD-IQ4_XS"\nprovider: "custom"\nbase_url: "http://127.0.0.1:9931/v1"', "double-quoted"),
        ('"model": "Qwen3.8-27B-UD-IQ4_XS"\n\'provider\': custom\nbase_url: http://127.0.0.1:9931/v1', "quoted keys"),
        ('{"model": "Qwen3.8-27B-UD-IQ4_XS", "provider": "custom", "base_url": "http://127.0.0.1:9931/v1"}\r\n',
         "the --json form"),
        ("# comment\nmodel: Qwen3.8-27B-UD-IQ4_XS   # trailing comment\nprovider: custom\n\n"
         "base_url: http://127.0.0.1:9931/v1  \n", "comments, blank lines, trailing blanks"),
        ("model:   Qwen3.8-27B-UD-IQ4_XS\nprovider:\tcustom\nbase_url: http://127.0.0.1:9931/v1", "extra blanks"),
        ("model: \"Qwen3.8-\\\n  27B-UD-IQ4_XS\"\nprovider: custom\nbase_url: http://127.0.0.1:9931/v1",
         "double-quoted escaped line break"),
        ("model: 'Qwen3.8-27B-UD-IQ4_XS'  # c\nprovider: custom\nbase_url: http://127.0.0.1:9931/v1",
         "comment after a quoted value"),
        ("model: Qwen3.8-27B-UD-IQ4_XS\nprovider: custom\nbase_url: http://127.0.0.1:9931/v1\napi_key: '***'\n"
         "key_env: MY_KEY", "extra keys kept"),
    ]
    for text, why in variants:
        got = ha.parse_get_output(text)
        check(got is not None and all(got.get(k) == v for k, v in want.items()), f"variant: {why} ({got})")
    check(ha.parse_get_output('model: "a\\x41\\u00e9\\U0001F600\\t\\"\\\\\\/\\ b"\nprovider: custom')
          == {"model": 'aA\xe9\U0001f600\t"\\/ b', "provider": "custom"}, "double-quoted escapes decoded")
    check(ha.parse_get_output("model: 'it''s'\nprovider: custom") == {"model": "it's", "provider": "custom"},
          "single-quoted '' is one quote")
    check(ha.parse_get_output("model: a\n  b\n\n  c\nprovider: custom") == {"model": "a b\nc", "provider": "custom"},
          "plain folding: a break is a space, a blank line a newline")
    for text, why in [("", "empty"), ("   \r\n", "blank"), ("Config key not set: model_aliases.llama", "error text"),
                      ("- a\n- b", "a list"), ("  model: x\nprovider: custom", "indented first line"),
                      ("model: x\nmodel: y", "a duplicate key"), ("just words", "no key"), ("[1, 2]", "flow list"),
                      ("{not json", "broken JSON"), ('["model"]', "a JSON list"), ("model x", "no colon"),
                      ("model: x\nTraceback (most recent call last):", "junk after the entry"),
                      ("123", "a bare number"), (None, "None"), (5, "an int"), ("null", "null")]:
        check(ha.parse_get_output(text) is None, f"unparseable -> None: {why}")
    dropped = [("model: 123", "an int"), ("model: 1.5", "a float"), ("model: true", "bool true"),
               ("model: Off", "bool Off"), ("model: ~", "tilde"), ("model: null", "null"), ("model:", "no value"),
               ("model: 2024-01-01", "a date"), ("model: 0x1F", "hex"), ("model: 0o7", "...but 0o7 is a string"),
               ("model: 12:30", "sexagesimal"), ("model: .inf", ".inf"), ("model: '=", "unterminated single quote"),
               ('model: "abc', "unterminated double quote"), ('model: "a\\qb"', "a bad escape"),
               ('model: "a\\x4"', "a short hex escape"), ("model: |\n  text", "a block scalar"),
               ("model: [a, b]", "a flow list"), ("model: {a: b}", "a flow map"), ("model: &a x", "an anchor"),
               ("model: *a", "an alias"), ("model: !!str x", "a tag"), ("model:\n  sub: x", "a nested map"),
               ("model:\n- a\n- b", "a block list"), ("model: a: b", "a colon-space in a plain value"),
               ("model: 'x' y", "text after a closing quote"), ("model: - x", "a list marker")]
    for text, why in dropped:
        got = ha.parse_get_output(text + "\nprovider: custom")
        if why.startswith("..."):
            check(got is not None and got.get("model") == "0o7", f"plain value kept: {why}")
            continue
        check(got is not None and "model" not in got and got.get("provider") == "custom",
              f"not a string, left out (the rest still read): {why} ({got})")
    check(ha.parse_get_output("model: ''\nprovider: custom") == {"model": "", "provider": "custom"},
          "an empty quoted string is an empty string")
    check(ha.parse_get_output('{"model": 123, "provider": "custom"}') == {"provider": "custom"},
          "JSON: a non-text value is left out")
    a = ha.parse_get_output(REAL_GET)
    b = ha.parse_get_output(REAL_GET)
    check(a == b and a is not b, "pure: same input, equal fresh results")


def t_needs_update() -> None:
    good = {"model": "m", "provider": "custom", "base_url": URL}
    table = [
        (None, True, "nothing read"), ({}, True, "empty"), (good, False, "same values"),
        ({**good, "api_key": "***"}, False, "an extra key does not matter"),
        ({**good, "model": "n"}, True, "other model"), ({**good, "base_url": "http://127.0.0.1:1/v1"}, True,
                                                         "other base_url"),
        ({**good, "provider": "openai"}, True, "other provider"), ({"model": "m", "base_url": URL}, True,
                                                                    "provider missing"),
        ({"provider": "custom", "base_url": URL}, True, "model missing"), ({**good, "model": "M"}, True, "case"),
        ({**good, "model": "m "}, True, "a trailing space"), ("m", True, "not a mapping"),
    ]
    for current, expected, why in table:
        check(ha.needs_update(current, "m", URL) is expected, f"needs_update: {why} -> {expected}")  # type: ignore[arg-type]


def t_read_alias() -> None:
    fake = FakeHermes(get_out=REAL_GET)
    got = ha.read_alias(exe=EXE, run=fake, exists=YES)
    check(got == {"model": "Qwen3.8-27B-UD-IQ4_XS", "provider": "custom", "base_url": URL}, f"read_alias ({got})")
    check(len(fake.calls) == 1 and fake.calls[0][0] == [EXE, "config", "get", "model_aliases.llama"],
          "read_alias: one GET, no SET")
    kw = fake.calls[0][1]
    check(kw.get("shell") is False and kw.get("stdin") is subprocess.DEVNULL and kw.get("timeout") == 20.0
          and kw.get("creationflags") == 0x08000000, "read_alias: same safe options")
    for fk, why in ((FakeHermes(get_code=1, get_stderr=b"Config key not set: model_aliases.llama"), "not set"),
                    (FakeHermes(get_out=b"garbage"), "unparseable"),
                    (FakeHermes(get_exc=subprocess.TimeoutExpired(["x"], 20)), "timeout"),
                    (FakeHermes(get_exc=OSError(2, "gone")), "cannot start"),
                    (FakeHermes(get_exc=RuntimeError("boom")), "any error")):
        try:
            got = ha.read_alias(exe=EXE, run=fk, exists=YES)
            check(got is None and len(fk.calls) == 1, f"read_alias {why} -> None")
        except Exception as exc:  # noqa: BLE001
            check(False, f"read_alias {why} raised {exc!r}")
    for exe, exists, why in ((r"C:\x\hermes.cmd", YES, ".cmd"), ("hermes.exe", YES, "relative"),
                             (EXE, lambda p: False, "missing")):
        fk = FakeHermes(get_out=REAL_GET)
        check(ha.read_alias(exe=exe, run=fk, exists=exists) is None and not fk.calls, f"read_alias {why}: nothing runs")
    fk = FakeHermes(get_out=REAL_GET)
    check(ha.read_alias(exe=EXE, run=fk, exists=YES, timeout_s=0) is None and not fk.calls,
          "read_alias bad time limit: nothing runs")
    got = ha.read_alias(exe=EXE, run=FakeHermes(get_out=get_text("123")), exists=YES)
    check(got is not None and got.get("model") == "123" and not ha.needs_update(got, "123", URL),
          "read_alias + needs_update: an up-to-date entry is not rewritten")


class SyncHermes(FakeHermes):
    """GETs before any SET answer `before` (exit code, stdout, stderr) or raise it; after a SET it is FakeHermes."""

    def __init__(self, before, **kw):
        super().__init__(**kw)
        self.before = before

    def __call__(self, argv, **kw):
        if argv[1:3] != ["config", "set"] and self.sent is None:
            self.calls.append((list(argv), dict(kw)))
            if isinstance(self.before, BaseException):
                raise self.before
            code, out, err = self.before
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        return super().__call__(argv, **kw)


NOT_SET = (1, b"", b"Config key not set: model_aliases.llama\r\n")
M1 = "Qwen3.8-27B-UD-IQ4_XS"
GET_ARGV = [EXE, "config", "get", "model_aliases.llama"]


def sync(model, before, url=URL, exe: str | None = EXE, exists=YES, timeout_s=ha.TIMEOUT_S, **kw):
    fake = SyncHermes(before, **kw)
    return ha.sync_alias(model, url, exe=exe, run=fake, exists=exists, timeout_s=timeout_s), fake


def kinds(fake) -> list[str]:
    return [c[0][2] for c in fake.calls]


def t_sync() -> None:
    """sync_alias: one GET first; the entry is only written when that GET shows it is safe and needed."""
    res, fk = sync(M1, (0, REAL_GET, b""))
    check(res == ha.AliasResult(True, False, "") and kinds(fk) == ["get"] and fk.calls[0][0] == GET_ARGV,
          f"already in step: one GET, no SET, no note ({res}, {kinds(fk)})")
    kw = fk.calls[0][1]
    check(kw.get("shell") is False and kw.get("stdin") is subprocess.DEVNULL and kw.get("timeout") == 20.0
          and kw.get("creationflags") == 0x08000000 and kw.get("check") is False, "the first GET: same safe options")

    # (the fixture table has no YAML for this name: the read-back is given explicitly)
    back = b"model: Qwen3.8-27B-UD-Q3_K_XL\r\nprovider: custom\r\nbase_url: " + URL.encode() + b"\r\n"
    res, fk = sync("Qwen3.8-27B-UD-Q3_K_XL", (0, REAL_GET, b""), get_out=back)
    check(kinds(fk) == ["get", "set", "get"] and res.ok and res.changed
          and fk.sent == {"model": "Qwen3.8-27B-UD-Q3_K_XL", "provider": "custom", "base_url": URL},
          f"another model: GET, SET, read-back ({res}, {kinds(fk)})")
    set_argv = fk.calls[1][0]
    check(set_argv[:5] == [EXE, "config", "set", "--", "model_aliases.llama"] and len(set_argv) == 6,
          "the SET is set_alias' own: whole entry as one argument after --")

    url8080 = "http://127.0.0.1:8080/v1"
    res, fk = sync(M1, (0, REAL_GET, b""), url=url8080)
    check(kinds(fk) == ["get", "set", "get"] and res.ok and res.changed and fk.sent is not None
          and fk.sent["base_url"] == url8080, f"same model on another port: written ({res})")

    res, fk = sync(M1, (0, get_text(M1, provider="openai"), b""))
    check(kinds(fk) == ["get", "set", "get"] and res.ok and res.changed, "another provider in the entry: written")

    res, fk = sync(M1, NOT_SET)
    check(kinds(fk) == ["get", "set", "get"] and res.ok and res.changed, f"Hermes says the key is not set: created ({res})")
    res, fk = sync(M1, (1, b"", b"CONFIG KEY NOT SET: model_aliases.llama"))
    check(kinds(fk) == ["get", "set", "get"] and res.ok, "...whatever the case of that sentence")

    # a key the app did not write: nothing is written
    for extra, shown in ((b"api_key: sk-secret-value\r\n", "api_key"), (b"key_env: MY_KEY\r\n", "key_env"),
                         (b"api_key: 12345\r\n", "api_key"), (b"headers:\r\n  x: y\r\n", "headers"),
                         (b"api_key:\r\n", "api_key")):
        n = len(LOG.records)
        res, fk = sync("Qwen3.8-27B-UD-Q3_K_XL", (0, REAL_GET + extra, b""))
        check(kinds(fk) == ["get"] and not res.ok and not res.changed and shown in res.note
              and res.note.startswith("Hermes alias not updated: its entry has other settings"),
              f"an entry with {shown} (value kind: {extra[:14]!r}): one GET, NO SET ({res.note})")
        check("secret" not in res.note and "12345" not in res.note and "MY_KEY" not in res.note
              and not any("secret" in r.getMessage() or "MY_KEY" in r.getMessage() for r in LOG.records[n:]),
              "...and the other key's VALUE is neither shown nor logged")
    res, fk = sync(M1, (0, REAL_GET + b"api_key: sk-x\r\n", b""))
    check(kinds(fk) == ["get"] and not res.ok, "...also when model and address are already right (not reported as in step)")
    res, fk = sync(M1, (0, b'{"model": "x", "provider": "custom", "base_url": "u", "we ird\\n": 1}', b""))
    check(kinds(fk) == ["get"] and not res.ok and "another key" in res.note and "we ird" not in res.note,
          f"a key name that is not a plain word is not echoed ({res.note})")

    # the GET did not give a usable answer: what is there is unknown, so nothing is written
    for before, why in (((1, b"", b"Traceback (most recent call last):"), "exit 1 without the not-set sentence"),
                        ((2, b"", b"Config key not set: model_aliases.llama"), "the not-set sentence with exit 2"),
                        ((1, b"Config key not set: model_aliases.llama", b""), "the not-set sentence on stdout"),
                        ((0, b"", b""), "exit 0, empty"), ((0, b"garbage", b""), "exit 0, unreadable"),
                        ((0, b"- a\r\n- b\r\n", b""), "exit 0, a list"),
                        ((None, REAL_GET, b""), "no exit code"),
                        (subprocess.TimeoutExpired(["x"], 20), "timeout"), (OSError(2, "gone"), "cannot start"),
                        (RuntimeError("boom"), "any error")):
        try:
            res, fk = sync("Qwen3.8-27B-UD-Q3_K_XL", before)
            check(kinds(fk) == ["get"] and not res.ok and not res.changed
                  and res.note.startswith("Hermes alias not updated: "), f"GET {why}: NO SET ({res.note})")
        except Exception as exc:  # noqa: BLE001
            check(False, f"sync_alias (GET {why}) raised {exc!r}")

    # refusals before anything runs
    for model, url, exe, exists, ts, why in (
            ("", URL, EXE, YES, 20.0, "empty model"), ("a\nb", URL, EXE, YES, 20.0, "control character"),
            (M1, "http://example.com:80/v1", EXE, YES, 20.0, "not loopback"),
            (M1, "http://127.0.0.1:9931/v1/", EXE, YES, 20.0, "trailing slash"),
            (M1, URL, r"C:\x\hermes.cmd", YES, 20.0, ".cmd"), (M1, URL, "hermes.exe", YES, 20.0, "relative"),
            (M1, URL, EXE, lambda p: False, 20.0, "missing program"), (M1, URL, EXE, YES, 0, "bad time limit")):
        res, fk = sync(model, NOT_SET, url=url, exe=exe, exists=exists, timeout_s=ts)
        check(not fk.calls and not res.ok and not res.changed, f"{why}: nothing runs ({res.note})")

    # the write itself fails or cannot be confirmed: set_alias' own answers come through unchanged
    res, fk = sync(M1, NOT_SET, set_code=1, set_stderr=b"Cannot set: locked")
    check(kinds(fk) == ["get", "set"] and not res.ok and res.note.startswith("Hermes alias not updated: hermes exited"),
          f"SET exit 1: not updated ({res.note})")
    res, fk = sync(M1, NOT_SET, get_out=get_text(M1, url="http://127.0.0.1:1/v1"))
    check(kinds(fk) == ["get", "set", "get"] and not res.ok and "may be wrong" in res.note,
          f"read-back shows other values: may be wrong ({res.note})")


def t_module_rules() -> None:
    src = (ROOT / "hermes_alias.py").read_text(encoding="utf-8")
    check("shell=True" not in src, "tripwire: no shell=True in hermes_alias.py")
    check("os.system" not in src, "tripwire: no os.system")
    check('.cmd"' not in src and ".cmd'" not in src, "tripwire: no '.cmd' program string")
    check("Popen(" not in src and "os.startfile" not in src and "os.spawn" not in src and "os.exec" not in src,
          "tripwire: no Popen / startfile / spawn / exec, only the run seam")
    check("open(" not in src and "write_text" not in src and "read_text" not in src,
          "tripwire: no file reads or writes (Hermes' files are never touched directly)")
    check("print(" not in src, "library code never prints")
    tree = ast.parse(src)
    consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    env_writes = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AugAssign))
                  and any(isinstance(t, ast.Subscript) and "env" in ast.unparse(t.value).lower()
                          for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    check(not any("HERMES_HOME" in c and "HERMES_HOME" == c.strip() for c in consts) and not env_writes
          and "putenv" not in src and "setdefault" not in src, "no environment variable is set for hermes")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    check(not mods & {"main", "tkinter", "customtkinter", "psutil", "socket", "urllib", "yaml", "shutil"},
          f"stdlib only, no Tk / main / network ({sorted(mods)})")
    check(ha.ALIAS == "llama" and ha.TIMEOUT_S == 20.0 and ha.EXE_NAME == "hermes.exe", "contract constants")
    for fn in (ha.set_alias, ha.sync_alias, ha.read_alias):
        p = inspect.signature(fn).parameters
        check(p["run"].default is _forbidden and p["exists"].default is os.path.isfile and p["exe"].default is None,
              f"{fn.__name__}: defaults are subprocess.run (guarded here), os.path.isfile, exe None")
    r = ha.AliasResult(True, True, "x")
    try:
        r.ok = False  # type: ignore[misc]
        check(False, "AliasResult is frozen")
    except Exception:  # noqa: BLE001
        check(True, "AliasResult is frozen")


def main() -> int:
    for t in (t_find_hermes, t_validators, t_entry_json, t_set_happy, t_refusals, t_set_failures, t_readback,
              t_parse, t_needs_update, t_read_alias, t_sync, t_module_rules):
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            check(False, f"{t.__name__} raised {exc!r}")
    check(not GUARD_HITS, f"no process was ever started ({len(GUARD_HITS)} attempts blocked)")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
