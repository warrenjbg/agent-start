"""Keeps Hermes' `llama` model alias in step with the app's llama.cpp server - the ONLY place that runs `hermes`.

Hermes resolves `/model llama` through the top-level config block `model_aliases:` -> `llama:` with the keys `model`,
`provider` (always `custom` here) and `base_url` (the app's own loopback server, http://127.0.0.1:PORT/v1). This
module changes that entry only through Hermes' own command, never by touching Hermes' files:

  SET  [hermes.exe, "config", "set", "--", "model_aliases.llama", JSON]   (the whole entry as ONE argument)
  GET  [hermes.exe, "config", "get", "model_aliases.llama"]               (read-back; prints the entry as YAML)

Why the whole entry as JSON: `hermes config set` type-converts a plain value (a model called 123, true, null or one
starting with a bracket would be stored as a number, a bool, nothing or a list, with exit code 0); inside a JSON
mapping every value stays a string. The `--` keeps a value starting with a dash from being read as an option (a value
like `--profile=x` would otherwise redirect the write to another Hermes profile). Cost: the whole `llama` entry is
replaced, so any other key in it (an api_key added by hand) is dropped. An empty model is never written: Hermes would
then silently resolve `/model llama` to its built-in Meta Llama alias.

Rules, checked again here at run time whatever the caller passed:
  * the model name and base_url pass validate_model / validate_base_url (otherwise nothing is run);
  * the program is an absolute path (with a drive or UNC share) to an existing file named exactly hermes.exe, case
    ignored - never hermes.cmd / .bat / .ps1, which CreateProcess would run through cmd.exe, re-parsing the arguments;
  * list argv, shell=False, stdin closed, stdout and stderr captured (<= 8 KB each, UTF-8 with replacement), no
    window, cwd = the program's folder, at most min(timeout_s, 60) s per run (subprocess.run kills the child it
    started when the time runs out), check=False;
  * the environment is the app's own minus LLAMA_* and AGENTSTART_* variables; nothing is added, so HERMES_HOME and
    profile settings stay whatever the user has (the user's own Hermes home is the one changed).
Every refusal is logged with its reason. set_alias, read_alias and find_hermes never raise. Each hermes run takes about
1.4 s: they are BLOCKING, call them from a worker thread. No Tk, no import of main, library code never prints.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping

log = logging.getLogger("agent_start")

ALIAS = "llama"
KEY = "model_aliases." + ALIAS
PROVIDER = "custom"
TIMEOUT_S = 20.0
MAX_TIMEOUT_S = 60.0
EXE_NAME = "hermes.exe"
MAX_MODEL_LEN = 200
MAX_OUTPUT_BYTES = 8192
MAX_STDERR_NOTE = 200
CHECK_COMMAND = "hermes config get " + KEY        # what a "may be wrong" note tells the user to run
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_ENV_DROP_PREFIXES = ("LLAMA_", "AGENTSTART_")
_BASE_URL_RE = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/v1")   # ASCII digits only, no leading zero
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class AliasResult:
    ok: bool            # the alias is confirmed to hold what was asked
    changed: bool       # a set command was run and the read-back confirmed it
    note: str           # one plain sentence for the user


# ------------------------------------------------------------------------------------------------ validation
def validate_model(model: Any) -> str | None:
    """None when `model` may be written as the alias' model name, else a short reason.

    Refused: not text, empty or only whitespace, whitespace at either end, more than 200 characters, NUL, any control
    character (C0, DEL and C1), the YAML line breaks U+2028 / U+2029 (Hermes' YAML reader would fold them into a
    space), unpaired surrogates (not encodable as UTF-8) and U+FEFF / U+FFFE / U+FFFF. Dashes, brackets, braces,
    quotes, backslashes, `true`, `123`, inner spaces, other unicode and emoji are fine: the JSON entry keeps them as
    text.
    """
    if not isinstance(model, str):
        return "the model name is not text"
    if not model:
        return "the model name is empty"
    if len(model) > MAX_MODEL_LEN:
        return f"the model name is longer than {MAX_MODEL_LEN} characters"
    for ch in model:
        o = ord(ch)
        if o == 0:
            return "the model name contains a NUL character"
        if o < 32 or o == 127 or 0x80 <= o <= 0x9F:
            return "the model name contains a control character"
        if ch in "\u2028\u2029":
            return "the model name contains a line break"
        if 0xD800 <= o <= 0xDFFF:
            return "the model name contains an unpaired surrogate"
        if o in (0xFEFF, 0xFFFE, 0xFFFF):
            return "the model name contains a character Hermes cannot store"
    if not model.strip():
        return "the model name is empty"
    if model != model.strip():
        return "the model name starts or ends with a space"
    return None


def validate_base_url(url: Any) -> str | None:
    """None for exactly http://127.0.0.1:PORT/v1 (PORT 1-65535 in plain ASCII digits, no leading zero), else a reason.

    The app only ever points the alias at its own loopback server."""
    if not isinstance(url, str):
        return "the server address is not text"
    m = _BASE_URL_RE.fullmatch(url)
    if m is None:
        return "the server address is not of the form http://127.0.0.1:PORT/v1"
    if not 1 <= int(m.group(1)) <= 65535:
        return "the server port is out of range"
    return None


def entry_json(model: str, base_url: str) -> str:
    """The whole alias entry as JSON text: {"model": ..., "provider": "custom", "base_url": ...}, in that order.

    ensure_ascii=False (with \\uXXXX escapes an emoji would come back as lone surrogates); json's default separators,
    the form the round-trip research tested. Raises ValueError for a value validate_model / validate_base_url refuse."""
    why = validate_model(model) or validate_base_url(base_url)
    if why:
        raise ValueError(why)
    return json.dumps({"model": model, "provider": PROVIDER, "base_url": base_url}, ensure_ascii=False)


def needs_update(current: Mapping[str, str] | None, model: str, base_url: str) -> bool:
    """False only when the parsed entry `current` already holds exactly `model`, provider custom and `base_url`."""
    if current is None or not isinstance(current, Mapping):
        return True
    return not (current.get("model") == model and current.get("base_url") == base_url
                and current.get("provider") == PROVIDER)


# ------------------------------------------------------------------------------------------------ locating hermes.exe
def _env_get(env: Mapping[str, str], name: str) -> str | None:
    """env[name], else the first key equal to it ignoring case (Windows variable names are case-insensitive)."""
    value = env.get(name)
    if value is None:
        for k, v in env.items():
            if isinstance(k, str) and k.upper() == name:
                return v
    return value


def _unquote(entry: Any) -> str:
    """A PATH entry without surrounding whitespace and double quotes; '' for one that still holds a quote or NUL."""
    if not isinstance(entry, str):
        return ""
    s = entry.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1].strip()
    if '"' in s or "\x00" in s:
        return ""
    return s


def _exe_problem(exe: Any, exists: Callable[[str], bool]) -> str | None:
    """None when `exe` may be run as hermes, else the reason (the rules in the module docstring)."""
    if not isinstance(exe, str) or not exe or "\x00" in exe:
        return "no hermes program was given"
    if not os.path.isabs(exe) or not os.path.splitdrive(exe)[0]:
        return "the hermes program path is not absolute"
    if os.path.basename(exe).casefold() != EXE_NAME:
        return f"the program is not {EXE_NAME}"
    if not exists(exe):
        return f"{EXE_NAME} was not found"
    return None


def find_hermes(env: Mapping[str, str] = os.environ, exists: Callable[[str], bool] = os.path.isfile) -> str | None:
    """Absolute path of hermes.exe: LOCALAPPDATA\\hermes\\bin\\hermes.exe first, then each absolute PATH folder that
    holds a file named exactly hermes.exe. Relative PATH entries are skipped; a .cmd / .bat / .ps1 is never returned.
    None when there is none. Never raises."""
    try:
        candidates: list[str] = []
        local = _unquote(_env_get(env, "LOCALAPPDATA"))
        if local:
            candidates.append(os.path.join(local, "hermes", "bin", EXE_NAME))
        path = _env_get(env, "PATH")
        if isinstance(path, str):
            for entry in path.split(os.pathsep):
                folder = _unquote(entry)
                if folder:
                    candidates.append(os.path.join(folder, EXE_NAME))
        for cand in candidates:
            if _exe_problem(cand, exists) is None:
                return cand
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        log.warning("hermes alias: looking for %s failed: %s", EXE_NAME, type(exc).__name__)
    return None


# ------------------------------------------------------------------------------------------------ running hermes
def _hermes_env() -> dict[str, str]:
    """The app's environment minus LLAMA_* and AGENTSTART_* variables; nothing added or overridden."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(_ENV_DROP_PREFIXES)}


def _timeout(timeout_s: Any) -> float | None:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) \
            or timeout_s <= 0:
        return None
    return min(float(timeout_s), MAX_TIMEOUT_S)


def _text(data: Any) -> str:
    """Captured output as text: at most 8 KB, UTF-8 with undecodable bytes replaced; '' when nothing usable."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data[:MAX_OUTPUT_BYTES]).decode("utf-8", errors="replace")
    if isinstance(data, str):
        return data[:MAX_OUTPUT_BYTES]
    return ""


def _first_line(text: str) -> str:
    """The first non-blank line, colour codes and control characters removed, at most 200 characters."""
    for line in _ANSI_RE.sub("", text).splitlines():
        clean = "".join(ch for ch in line if ord(ch) >= 32 and ord(ch) != 127 and not 0x80 <= ord(ch) <= 0x9F)
        clean = clean.strip()
        if clean:
            return clean[:MAX_STDERR_NOTE]
    return ""


def _run(run: Callable[..., Any], exe: str, args: list[str], timeout: float) -> Any:
    """One hermes run with the fixed safe options. Exceptions propagate to the caller."""
    return run([exe, *args], shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               cwd=os.path.dirname(os.path.abspath(exe)), creationflags=CREATE_NO_WINDOW, timeout=timeout,
               check=False, env=_hermes_env())


def _resolve(exe: str | None, exists: Callable[[str], bool]) -> tuple[str | None, str]:
    """(the program to run, '') or (None, why not)."""
    path = exe if exe is not None else find_hermes(exists=exists)
    if path is None:
        return None, f"{EXE_NAME} was not found"
    why = _exe_problem(path, exists)
    if why:
        return None, why
    return path, ""


def _get(exe: str, run: Callable[..., Any], timeout: float) -> tuple[dict[str, str] | None, str]:
    """Run the GET; (parsed entry, '') or (None, why). Exceptions from `run` propagate."""
    res = _run(run, exe, ["config", "get", KEY], timeout)
    code = getattr(res, "returncode", None)
    if code != 0:
        line = _first_line(_text(getattr(res, "stderr", None)))
        return None, f"reading it back gave exit code {code}" + (f" ({line})" if line else "")
    parsed = parse_get_output(_text(getattr(res, "stdout", None)))
    if parsed is None:
        return None, "Hermes' answer could not be read"
    return parsed, ""


def _refused(why: str, model: Any) -> AliasResult:
    log.warning("hermes alias not updated (%s): %r", why, model)
    return AliasResult(False, False, f"Hermes alias not updated: {why}")


def _maybe_wrong(why: str) -> AliasResult:
    log.warning("hermes alias may be wrong: %s", why)
    return AliasResult(False, False, f"Hermes alias {ALIAS} may be wrong: {why}; check it with: {CHECK_COMMAND}")


def set_alias(model: str, base_url: str, *, exe: str | None = None, run: Callable[..., Any] = subprocess.run,
              exists: Callable[[str], bool] = os.path.isfile, timeout_s: float = TIMEOUT_S) -> AliasResult:
    """Point Hermes' `llama` alias at `model` on `base_url`: one SET of the whole entry, then a GET that must show
    exactly the strings sent. Never raises. BLOCKING (two hermes runs, about 3 s): call from a worker thread."""
    try:
        return _set_alias(model, base_url, exe, run, exists, timeout_s)
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        log.warning("hermes alias: unexpected %s", type(exc).__name__)
        return _maybe_wrong(f"an unexpected error occurred ({type(exc).__name__})")


def _set_alias(model: Any, base_url: Any, exe: Any, run: Callable[..., Any], exists: Callable[[str], bool],
               timeout_s: Any) -> AliasResult:
    why = validate_model(model) or validate_base_url(base_url)
    if why:
        return _refused(why, model)
    timeout = _timeout(timeout_s)
    if timeout is None:
        return _refused(f"bad time limit {timeout_s!r}", model)
    path, why = _resolve(exe, exists)
    if path is None:
        return _refused(why, model)
    text = entry_json(model, base_url)
    log.info("hermes alias: setting %s to %s at %s", KEY, model, base_url)
    try:
        res = _run(run, path, ["config", "set", "--", KEY, text], timeout)
    except subprocess.TimeoutExpired:
        return _maybe_wrong(f"hermes did not finish within {timeout:.0f} s")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return _refused(f"hermes could not be started ({type(exc).__name__})", model)
    code = getattr(res, "returncode", None)
    if code != 0:
        line = _first_line(_text(getattr(res, "stderr", None)))
        return _refused(f"hermes exited with code {code}" + (f" ({line})" if line else ""), model)
    try:
        current, why = _get(path, run, timeout)
    except subprocess.TimeoutExpired:
        return _maybe_wrong(f"reading it back did not finish within {timeout:.0f} s")
    except Exception as exc:  # noqa: BLE001 - the set ran; any failure here only means "not confirmed"
        return _maybe_wrong(f"reading it back failed ({type(exc).__name__})")
    if current is None:
        return _maybe_wrong(why)
    if needs_update(current, model, base_url):
        return _maybe_wrong("Hermes does not show the values that were sent")
    log.info("hermes alias: %s confirmed", KEY)
    return AliasResult(True, True, f"Hermes alias {ALIAS} now points at {model}")


def read_alias(*, exe: str | None = None, run: Callable[..., Any] = subprocess.run,
               exists: Callable[[str], bool] = os.path.isfile, timeout_s: float = TIMEOUT_S) -> dict[str, str] | None:
    """The current `llama` entry as parsed by parse_get_output (GET only), or None when it is not set, cannot be read
    or hermes cannot be run. Never raises. BLOCKING (about 1.4 s): call from a worker thread."""
    try:
        timeout = _timeout(timeout_s)
        if timeout is None:
            log.warning("hermes alias not read (bad time limit %r)", timeout_s)
            return None
        path, why = _resolve(exe, exists)
        if path is None:
            log.warning("hermes alias not read (%s)", why)
            return None
        current, why = _get(path, run, timeout)
        if current is None:
            log.info("hermes alias not read: %s", why)
        return current
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        log.info("hermes alias not read: %s", type(exc).__name__)
        return None


# ------------------------------------------------------------------------------------------------ parsing the GET
# `hermes config get model_aliases.llama` prints a mapping with PyYAML's safe_dump (block style, sort_keys=False,
# allow_unicode=False, width 80), e.g. (CRLF line ends on Windows):
#     model: Qwen3.8-27B-UD-IQ4_XS
#     provider: custom
#     base_url: http://127.0.0.1:9931/v1
# Strings that would read back as another type are single-quoted ('123', 'true'), strings with non-ASCII or special
# characters are double-quoted with escapes ("\xFC\U0001F600"), long ones are folded over indented lines. The parser
# below reads exactly that subset of YAML (and a JSON object, the --json form). A value it cannot read as a string -
# a plain scalar YAML resolves to a number / bool / null, a nested block, a flow collection, an unterminated quote - is
# left out of the result, so a comparison with what was sent fails safe.
_KEY_LINE_RE = re.compile(r"""(?:([A-Za-z_][A-Za-z0-9_-]*)|"([A-Za-z_][A-Za-z0-9_-]*)"|'([A-Za-z_][A-Za-z0-9_-]*)')"""
                          r""":(?:[ \t]+(.*))?""")
# PyYAML's implicit resolvers (yaml/resolver.py, YAML 1.1): a plain scalar matching one of these is NOT a string.
_NON_STR_RES = [re.compile(p) for p in (
    r"(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)",
    r"(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9_]+(?:[eE][-+][0-9]+)?"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))",
    r"(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)",
    r"(?:<<)",
    r"(?:~|null|Null|NULL|)",
    r"(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)"
    r"[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)",
    r"(?:=)",
)]
_ESCAPES = {"0": "\x00", "a": "\x07", "b": "\x08", "t": "\t", "\t": "\t", "n": "\n", "v": "\x0b", "f": "\x0c",
            "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
            "L": "\u2028", "P": "\u2029"}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _fold_break(s: str, i: int, out: list[str], protected: int) -> int:
    """At s[i] == '\\n' inside a flow scalar: drop unprotected trailing blanks from `out`, skip the break, blank lines
    and the next line's indentation, and append one space (or one newline per blank line). Returns the new index."""
    while len(out) > protected and out[-1] in " \t":
        out.pop()
    i += 1
    breaks = 0
    while True:
        while i < len(s) and s[i] in " \t":
            i += 1
        if i < len(s) and s[i] == "\n":
            breaks += 1
            i += 1
            continue
        break
    out.append("\n" * breaks if breaks else " ")
    return i


def _end_ok(rest: str) -> bool:
    """After a closing quote only blanks or a comment may follow."""
    rest = rest.strip()
    return not rest or rest.startswith("#")


def _single_quoted(s: str) -> str | None:
    out: list[str] = []
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == "'":
            if s[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out) if _end_ok(s[i + 1:]) else None
        if ch == "\n":
            i = _fold_break(s, i, out, 0)
            continue
        out.append(ch)
        i += 1
    return None                                         # unterminated


def _double_quoted(s: str) -> str | None:
    out: list[str] = []
    protected = 0                                       # out[:protected] came from escapes: never stripped
    i = 1
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            nxt = s[i + 1:i + 2]
            if nxt == "\n":                             # escaped line break: joins the lines, indentation dropped
                i += 2
                while i < len(s) and s[i] in " \t":
                    i += 1
                continue
            if nxt in _ESCAPES and nxt:
                out.append(_ESCAPES[nxt])
                i += 2
            elif nxt in _HEX_ESCAPES:
                n = _HEX_ESCAPES[nxt]
                digits = s[i + 2:i + 2 + n]
                if len(digits) != n or not set(digits) <= _HEX_DIGITS or int(digits, 16) > 0x10FFFF:
                    return None
                out.append(chr(int(digits, 16)))
                i += 2 + n
            else:
                return None
            protected = len(out)
            continue
        if ch == '"':
            return "".join(out) if _end_ok(s[i + 1:]) else None
        if ch == "\n":
            i = _fold_break(s, i, out, protected)
            continue
        out.append(ch)
        i += 1
    return None                                         # unterminated


def _plain(lines: list[str]) -> str | None:
    words: list[str] = []
    for line in lines:
        cut = line.find(" #")
        words.append((line[:cut] if cut >= 0 else line).strip())
    value = words[0]
    breaks = 0
    for w in words[1:]:
        if not w:
            breaks += 1
            continue
        value += ("\n" * breaks if breaks else " ") + w
        breaks = 0
    if not value or value[0] in ",[]{}#&*!|>'\"%@`":
        return None                                     # not a valid plain scalar
    if value[0] in "-?:" and (len(value) == 1 or value[1] in " \t\n"):
        return None                                     # a list item / complex key / value indicator
    if ": " in value or ":\t" in value or value.endswith(":"):
        return None                                     # a nested mapping, not a string
    if any(r.fullmatch(value) for r in _NON_STR_RES):
        return None                                     # YAML reads it as a number / bool / null, not a string
    return value


def _scalar(lines: list[str]) -> str | None:
    """The string value of one `key: ...` entry (its first-line text plus indented continuation lines), or None."""
    first = lines[0].strip()
    if not first:
        return None                                     # null, or a nested block: not a string
    joined = "\n".join([first, *lines[1:]])
    if first[0] == '"':
        return _double_quoted(joined)
    if first[0] == "'":
        return _single_quoted(joined)
    return _plain([first, *lines[1:]])


def parse_get_output(text: Any) -> dict[str, str] | None:
    """The string-valued keys of the mapping that `hermes config get model_aliases.llama` printed (see above).

    None for empty output, a line at the left margin that is not `key: value`, an indented line before any key, a
    duplicate key, or JSON that is not an object. Pure: no I/O, never raises."""
    try:
        return _parse(text)
    except Exception:  # noqa: BLE001 - pure parser, contract: never raises
        return None


def _parse(text: Any) -> dict[str, str] | None:
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="replace")
    if not isinstance(text, str):
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not text.strip():
        return None
    if text.strip().startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        return {k: v for k, v in obj.items() if isinstance(k, str) and isinstance(v, str)}
    entries: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if not line.strip():
            if entries:
                entries[-1][1].append("")
            continue
        if line[0] in " \t" or (line[0] == "-" and line[1:2] in ("", " ", "\t") and entries
                                and not entries[-1][1][0].strip()):
            if not entries:          # an indented line, or a block-sequence item of a key with no inline value
                return None
            entries[-1][1].append(line)
            continue
        if line.startswith("#"):
            continue
        m = _KEY_LINE_RE.fullmatch(line)
        if m is None:
            return None
        entries.append((m.group(1) or m.group(2) or m.group(3), [m.group(4) or ""]))
    result: dict[str, str] = {}
    seen: set[str] = set()
    for key, lines in entries:
        if key in seen:
            return None
        seen.add(key)
        while len(lines) > 1 and not lines[-1].strip():
            lines.pop()
        value = _scalar(lines)
        if value is not None:
            result[key] = value
    return result if seen else None
