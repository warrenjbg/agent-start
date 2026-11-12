r"""Turn a Strata `run-*.bat` into the argv Hermes Controller launches itself.

The .bat is only READ, never executed. The parser understands what those launchers use in practice:
`cd /d "<dir>"` / `pushd`, `set VAR=value`, `%VAR%`, `%~dp0`, `^` line continuations, `call` / `start [/D dir]`
prefixes, quoted arguments and redirections. From the line that runs `python ... serve\server.py ...` it keeps the
argv (minus `--open`, which would pop a browser tab), the port and the `--config` file.

Because that argv is run directly, the parser is strict about what it accepts: the interpreter must be a python.exe
/ py.exe (never a .bat/.cmd shim, which Popen would hand to cmd.exe), only interpreter options may precede
serve\server.py (no -c / -m / other scripts), exactly one unconditional launch line may exist, and launchers that
jump (goto / call :label) before it or launch inside if / for / ( ) blocks are refused with a LauncherError.

All functions are pure apart from reading the .bat / config file (and a PATH lookup for a bare `python`).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
from dataclasses import dataclass

DEFAULT_PORT = 8080
MAX_BAT_BYTES = 256 * 1024
# config.json keys derived from the chosen launcher (written together, applied together)
LAUNCHER_KEYS = ("launcher_bat", "launch_cmd", "launch_cwd", "strata_dir", "port", "endpoint_url", "model_name")

_MARK = os.path.normcase("serve\\server.py")
# python / python3 / python3.12 / pythonw / py / pyw (+ .exe) - nothing else may be the interpreter
_PY_RE = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?w?|pyw?)", re.IGNORECASE)
_PY_LAUNCHERS = frozenset({"py", "pyw"})
# %~dp0-style (group 1 = modifiers), %~1..%~9 (group 2), %*, %N, %%, %VAR%
_VAR_RE = re.compile(r"%(?:~([a-zA-Z]*)0|(~[a-zA-Z]*[1-9])|(\*)|(\d)|(%)|([^%\r\n]+)%)")
_SET_RE = re.compile(r'^set\s+(?!/)(")?([^=\s"]+)=(.*)$', re.IGNORECASE)
_CD_RE = re.compile(r"^(?:cd|chdir|pushd)(?=[\s\"/])(?:\s*/d)?\s*(.*)$", re.IGNORECASE)
_SKIP_RE = re.compile(r"^(?:rem(?:\s|$)|::|:[A-Za-z_])", re.IGNORECASE)
_DELIM = r"(?=$|[\s.:;,=/+\[\]\\(])"                      # what may follow an internal command name
_REM_RE = re.compile(r"^(?:rem" + _DELIM + r"|::)", re.IGNORECASE)
_TEXT_RE = re.compile(r"^(?:echo|title)" + _DELIM, re.IGNORECASE)     # prints text, never runs it
_COND_RE = re.compile(r"^(?:if|for|else)(?=$|[\s(\"])", re.IGNORECASE)
_JUMP_RE = re.compile(r"^(?:goto(?=$|[\s:])|call\s*:)", re.IGNORECASE)
_JUMP_ANY_RE = re.compile(r"(?:^|[\s(])(?:goto(?=$|[\s:])|call\s*:)", re.IGNORECASE)
_EXIT_RE = re.compile(r"^exit(?=$|[\s/])", re.IGNORECASE)
# interpreter options allowed before serve\server.py
_FLAG_OPT_RE = re.compile(r"-[uBOEIsSq]+")                # -u -B -O -OO -E -I -s -S -q (and -uB style groups)
_VALUE_OPTS = frozenset({"-X", "-W"})                     # -X utf8 / -W ignore, or joined: -Xutf8 / -Wignore
_PY_VERSION_RE = re.compile(r"-(?:[23](?:\.\d+)?(?:-(?:32|64))?|V:\S+)")   # py.exe launcher: -3, -3.12, -V:3.12


class LauncherError(ValueError):
    """The .bat cannot be used as a Strata launcher; str(exc) is a short message for the Settings dialog."""


@dataclass(frozen=True)
class LauncherInfo:
    bat_path: str                 # absolute path of the .bat
    argv: tuple[str, ...]         # python, serve\server.py, args... (absolute paths, no --open)
    cwd: str                      # directory the .bat runs the server from
    strata_dir: str               # Strata install root (parent of serve\)
    port: int
    config_path: str | None       # --config file, absolute, or None
    model_name: str

    @property
    def endpoint_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def bat_name(self) -> str:
        return os.path.basename(self.bat_path)

    def summary(self) -> str:
        """One read-only line for the Settings dialog: model · config file · port."""
        parts = [self.model_name]
        if self.config_path:
            parts.append(os.path.basename(self.config_path))
        parts.append(f"port {self.port}")
        return " · ".join(parts)


# ------------------------------------------------------------------------------------------- text helpers
def read_bat_text(path: str) -> str:
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BAT_BYTES + 1)
    except OSError as exc:
        raise LauncherError(f"Cannot read {os.path.basename(path)}: {exc.strerror or exc}") from exc
    if len(raw) > MAX_BAT_BYTES:
        raise LauncherError(f"{os.path.basename(path)} is too large to be a launcher")
    for enc in ("utf-8-sig", "mbcs", "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def logical_lines(text: str) -> list[str]:
    """Physical lines joined across `^` continuations (handles \\r\\r\\n, which some launchers contain)."""
    lines = re.sub(r"\r+\n?", "\n", text).split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        while line.endswith("^") and not line.endswith("^^") and i + 1 < len(lines):
            i += 1
            line = line[:-1] + lines[i]
        out.append(line)
        i += 1
    return out


def expand_vars(s: str, bat_path: str, variables: dict[str, str], cwd: str) -> str:
    """%~dp0 and friends, %VAR% (bat-local `set` first, then the environment; unknown -> empty), %% -> %.

    Batch arguments (%1..%9, %~1..%~9, %*) are empty: Hermes launches the server without any.
    """
    drive, tail = os.path.splitdrive(bat_path)
    folder, base = os.path.split(tail)
    stem, ext = os.path.splitext(base)
    if not folder.endswith(("\\", "/")):
        folder += os.sep

    def repl(m: re.Match) -> str:
        mods, arg, star, digit, pct, name = m.groups()
        if arg is not None:
            return ""
        if mods is not None:
            mods = mods.lower()
            if not mods or "f" in mods:
                return bat_path
            return "".join(part for flag, part in (("d", drive), ("p", folder), ("n", stem), ("x", ext))
                           if flag in mods)
        if star is not None:
            return ""
        if digit is not None:
            return bat_path if digit == "0" else ""
        if pct is not None:
            return "%"
        key = name.upper()
        if key == "CD":
            return cwd
        if key in variables:
            return variables[key]
        for k, v in os.environ.items():
            if k.upper() == key:
                return v
        return ""

    return _VAR_RE.sub(repl, s)


def _split(line: str, keep_parens: bool = False) -> list[tuple[str, str]]:
    """(operator, segment) pairs: the line split at unquoted & && | ||, with redirections removed.

    The operator is the one that precedes the segment ("" for the first). A redirection consumes only its target
    word (`> log.txt`, `2>&1`, `>>"a b.log"`), so arguments after it are kept. `^` escapes are resolved and,
    unless keep_parens, unquoted ( ) become spaces.
    """
    segs: list[tuple[str, str]] = []
    cur: list[str] = []
    op = ""
    in_q = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == '"':
            in_q = not in_q
            cur.append(ch)
        elif in_q:
            cur.append(ch)
        elif ch == "^" and i + 1 < n:
            i += 1
            cur.append(line[i])
        elif ch in "&|":
            nxt = ch
            if i + 1 < n and line[i + 1] == ch:
                nxt = ch * 2
                i += 1
            segs.append((op, "".join(cur)))
            cur, op = [], nxt
        elif ch in "<>":
            if cur and cur[-1].isdigit() and (len(cur) == 1 or cur[-2].isspace()):
                cur.pop()                                   # the handle number of "2>"
            i += 1
            if ch == ">" and i < n and line[i] == ">":
                i += 1
            while i < n and line[i] in " \t":
                i += 1
            if i < n and line[i] == "&":                    # >&1 / 2>&1: a handle duplication
                i += 1
                while i < n and line[i].isdigit():
                    i += 1
            else:                                           # exactly one target word
                q = False
                while i < n:
                    c = line[i]
                    if c == '"':
                        q = not q
                    elif not q and c in " \t&|<>)":
                        break
                    elif not q and c == "^":
                        i += 1
                    i += 1
            cur.append(" ")
            continue
        elif ch in "()" and not keep_parens:
            cur.append(" ")
        else:
            cur.append(ch)
        i += 1
    segs.append((op, "".join(cur)))
    return [(o, s.strip()) for o, s in segs if s.strip()]


def split_commands(line: str) -> list[str]:
    """Split one logical line at unquoted & | and drop redirections; unquoted ( ) and ^ escapes are removed."""
    return [seg for _, seg in _split(line)]


def _unparen(seg: str) -> str:
    """Unquoted ( ) -> spaces (block / if-body punctuation, not arguments)."""
    out, in_q = [], False
    for ch in seg:
        if ch == '"':
            in_q = not in_q
        out.append(" " if ch in "()" and not in_q else ch)
    return "".join(out)


def _paren_delta(seg: str) -> int:
    depth, in_q = 0, False
    for ch in seg:
        if ch == '"':
            in_q = not in_q
        elif not in_q and ch == "(":
            depth += 1
        elif not in_q and ch == ")":
            depth -= 1
    return depth


def tokenize(cmdline: str) -> list[str]:
    """Split a command line the way the Microsoft C runtime (and so python.exe) does."""
    args: list[str] = []
    cur: list[str] = []
    in_q = False
    have = False
    i, n = 0, len(cmdline)
    while i < n:
        ch = cmdline[i]
        if ch == "\\":
            j = i
            while j < n and cmdline[j] == "\\":
                j += 1
            count = j - i
            if j < n and cmdline[j] == '"':
                cur.append("\\" * (count // 2))
                if count % 2:
                    cur.append('"')
                    i = j + 1
                else:
                    i = j
            else:
                cur.append("\\" * count)
                i = j
            have = True
            continue
        if ch == '"':
            in_q = not in_q
            have = True
        elif ch in " \t" and not in_q:
            if have:
                args.append("".join(cur))
                cur, have = [], False
        else:
            cur.append(ch)
            have = True
        i += 1
    if have:
        args.append("".join(cur))
    return args


# ------------------------------------------------------------------------------------------- parsing
def _is_server_script(tok: str) -> bool:
    t = os.path.normcase(tok)
    return t == _MARK or t.endswith(os.sep + _MARK)


def _exe_name(tok: str) -> str:
    return os.path.basename(tok.replace("/", "\\"))


def _is_python(tok: str) -> bool:
    """python[N[.N]][w] / py / pyw, with no extension or .exe (never .bat/.cmd/.com or another program)."""
    name = _exe_name(tok)
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return bool(_PY_RE.fullmatch(name))


def _looks_like_python(tok: str) -> bool:
    return _exe_name(tok).lower().startswith("py")


def _abs(path: str, cwd: str) -> str:
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _shim_error(path: str) -> LauncherError:
    return LauncherError(f"The launcher must run python.exe directly, not a .bat/.cmd shim: {path}")


def _resolve_interpreter(tok: str, cwd: str) -> str:
    """Absolute path of the interpreter, always ending in .exe (Popen would run a .bat/.cmd through cmd.exe)."""
    has_exe = tok.lower().endswith(".exe")
    if os.path.isabs(tok) or "\\" in tok or "/" in tok:
        full = _abs(tok, cwd)
        if has_exe:
            return full
        if not os.path.isfile(full + ".exe"):
            for ext in (".com", ".bat", ".cmd"):
                if os.path.isfile(full + ext):
                    raise _shim_error(full + ext)
        return full + ".exe"
    exe = tok if has_exe else tok + ".exe"
    local = os.path.normpath(os.path.join(cwd, exe))
    if os.path.isfile(local):                     # cmd looks in the current directory first
        return local
    for d in os.environ.get("PATH", "").split(os.pathsep):    # then PATH - for <name>.exe only
        d = d.strip().strip('"')
        if d and os.path.isfile(os.path.join(d, exe)):
            return os.path.normpath(os.path.abspath(os.path.join(d, exe)))
    if not has_exe:
        shim = shutil.which(tok)
        if shim and not shim.lower().endswith(".exe"):
            raise _shim_error(shim)
    return local                                  # not on disk: missing_files() reports it


def _next_word(s: str) -> tuple[str, str]:
    """First whitespace-delimited word of s (quotes kept; spaces inside quotes allowed) and the rest."""
    s = s.lstrip()
    in_q = False
    for i, ch in enumerate(s):
        if ch == '"':
            in_q = not in_q
        elif ch in " \t" and not in_q:
            return s[:i], s[i:]
    return s, ""


def _strip_start(s: str, cwd: str) -> tuple[str, str]:
    """`start ["title"] [/D dir] [/b /min ...] cmd...` (after the word start) -> (cmd..., directory cmd runs in)."""
    title_seen = False
    while True:
        word, rest = _next_word(s)
        if not word:
            return "", cwd
        low = word.lower()
        if word.startswith('"') and not title_seen:
            title_seen = True
        elif low == "/d":
            target, rest = _next_word(rest)
            if target.strip('"'):
                cwd = _abs(target.strip('"'), cwd)
        elif low.startswith("/d"):
            cwd = _abs(word[2:].strip('"'), cwd)
        elif low in ("/node", "/affinity"):
            _, rest = _next_word(rest)
        elif not low.startswith("/"):
            return s.lstrip(), cwd
        s = rest


def _strip_prefixes(seg: str, cwd: str) -> tuple[str, str]:
    """Remove `call` / `start ...` command prefixes; `start /D dir` sets the directory the command runs in."""
    s = seg.strip()
    while True:
        s = s.lstrip("@").lstrip()
        m = re.match(r"call(?=\s)\s*", s, re.IGNORECASE)
        if m and not s[m.end():].startswith(":"):
            s = s[m.end():]
            continue
        m = re.match(r'start(?=$|[\s"/])', s, re.IGNORECASE)
        if m:
            s, cwd = _strip_start(s[m.end():], cwd)
            continue
        return s, cwd


def _server_argv(tokens: list[str], bat_name: str) -> int | None:
    """tokens[0] is a python interpreter: the index of serve\\server.py if this command runs it, else None.

    Only interpreter options may come between the two; -c / -m / another script raise LauncherError because the
    command would run code other than Strata's server.
    """
    if not any(_is_server_script(t) for t in tokens[1:]):
        return None
    launcher = os.path.splitext(_exe_name(tokens[0]))[0].lower() in _PY_LAUNCHERS
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if _is_server_script(tok):
            return i
        if _FLAG_OPT_RE.fullmatch(tok):
            i += 1
            continue
        if tok[:2] in _VALUE_OPTS:
            value = tok[2:]
            if not value:
                if i + 1 >= len(tokens):
                    break
                i += 1
                value = tokens[i]
            if tok[:2] == "-W" and "." in (value.split(":") + ["", "", ""])[2]:
                # python imports a dotted warning category: that would run a module before server.py
                raise LauncherError(f"{bat_name}: python -W {value} imports a module before serve\\server.py "
                                    f"(not allowed)")
            i += 1
            continue
        if launcher and i == 1 and _PY_VERSION_RE.fullmatch(tok):
            i += 1
            continue
        if re.match(r"-[A-Za-z]*[cm]", tok):
            raise LauncherError(f"{bat_name}: python {tok} runs other code instead of serve\\server.py "
                                f"(-c / -m are not allowed)")
        raise LauncherError(f'{bat_name}: "{tok}" before serve\\server.py is not allowed (only python options '
                            f"such as -u, -B, -X utf8 may come first)")
    return None


def _launches_server(tokens: list[str]) -> bool:
    """Loose check for conditional code: some python-like word is followed by serve\\server.py."""
    return any(_is_server_script(t) and any(_looks_like_python(x) for x in tokens[:i])
               for i, t in enumerate(tokens))


def _port_value(text: str, bat_name: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 1 <= port <= 65535:
        raise LauncherError(f'{bat_name}: --port "{text}" is not a valid port')
    return port


def model_name_for(config_path: str | None, bat_path: str) -> str:
    """The config json's model name if it has one, else the config / .bat file name."""
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in ("model", "model_name"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except (OSError, ValueError):
            pass
        stem = os.path.splitext(os.path.basename(config_path))[0]
        return stem[7:] if stem.lower().startswith("strata-") and len(stem) > 7 else stem
    stem = os.path.splitext(os.path.basename(bat_path))[0]
    return stem[4:] if stem.lower().startswith("run-") and len(stem) > 4 else stem


def parse_bat_text(text: str, bat_path: str) -> LauncherInfo:
    """Parse the launcher text (bat_path gives %~dp0 and the starting directory). Raises LauncherError."""
    bat_path = os.path.abspath(bat_path)
    bat_name = os.path.basename(bat_path)
    cwd = os.path.dirname(bat_path)
    variables: dict[str, str] = {}
    found: tuple[list[str], int, str] | None = None   # (argv tokens, index of server.py, directory it runs in)
    jumped: str | None = None                          # a goto / call :label seen before the launch line
    depth = 0                                          # ( blocks still open from earlier lines
    stop = False
    for raw in logical_lines(text):
        line = raw.strip().lstrip("@").strip()
        if not line or _SKIP_RE.match(line):
            continue
        line = expand_vars(line, bat_path, variables, cwd)
        in_block = depth > 0
        cond_rest = False                              # the rest of this line is an if / for / else body
        for op, seg in _split(line, keep_parens=True):
            cmd = seg.lstrip("()@ \t")
            if not cmd:
                depth = max(0, depth + _paren_delta(seg))
                continue
            if _REM_RE.match(cmd):
                break                                  # rem / :: comments out the rest of the line
            if _TEXT_RE.match(cmd):
                continue                               # echo / title text is printed, never run
            set_m = _SET_RE.match(cmd)
            if not set_m:
                depth = max(0, depth + _paren_delta(seg))
            if _COND_RE.match(cmd):
                cond_rest = True
            conditional = cond_rest or in_block or op == "||"
            chained = op in ("&&", "||")
            flat = _unparen(cmd).strip()
            if conditional:
                if _launches_server(tokenize(flat)):
                    raise LauncherError(f"{bat_name}: conditional launch lines are not supported "
                                        f"(python … serve\\server.py inside if / for / ( ) or after ||)")
                if found is None and _JUMP_ANY_RE.search(flat):
                    jumped = jumped or flat
                continue
            if set_m:
                value = set_m.group(3)
                if set_m.group(1):                     # set "VAR=value"
                    value = value.rsplit('"', 1)[0] if '"' in value else value
                variables[set_m.group(2).upper()] = value.strip()
                continue
            cd_m = _CD_RE.match(flat)
            if cd_m:
                target = cd_m.group(1).strip().strip('"').strip()
                if target:
                    cwd = _abs(target, cwd)
                continue
            if _JUMP_RE.match(flat):
                if found is None:
                    jumped = jumped or flat
                elif not chained:
                    stop = True                        # nothing after an unconditional goto runs in order
                    break
                continue
            if _EXIT_RE.match(flat):
                if not chained:
                    stop = True
                    break
                continue
            rest, run_cwd = _strip_prefixes(flat, cwd)
            tokens = tokenize(rest)
            if not tokens:
                continue
            if _is_python(tokens[0]):
                if jumped is not None and any(_is_server_script(t) for t in tokens[1:]):
                    raise LauncherError(f'{bat_name} jumps ("{jumped}") before the Strata launch line; '
                                        f"launchers that use goto / call :label are not supported")
                script_i = _server_argv(tokens, bat_name)
                if script_i is None:
                    continue
                if found is not None:
                    raise LauncherError(f"{bat_name}: the launcher starts Strata more than once (ambiguous)")
                found = (tokens, script_i, run_cwd)
            elif _looks_like_python(tokens[0]) and any(_is_server_script(t) for t in tokens[1:]):
                raise _shim_error(tokens[0])
        if stop:
            break
    if found is None:
        raise LauncherError(f"{bat_name} does not start Strata (no python … serve\\server.py line)")

    argv, script_i, cwd = found
    out = [_resolve_interpreter(argv[0], cwd)] + argv[1:script_i] + [_abs(argv[script_i], cwd)]
    port, config_path = DEFAULT_PORT, None
    rest = argv[script_i + 1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else None
        if tok == "--open":
            i += 1
            continue
        if tok == "--port" and nxt is not None:
            port = _port_value(nxt, bat_name)
            out += [tok, str(port)]
            i += 2
            continue
        if tok.startswith("--port="):
            port = _port_value(tok.split("=", 1)[1], bat_name)
            out.append(f"--port={port}")
            i += 1
            continue
        if tok == "--config" and nxt is not None:
            config_path = _abs(nxt, cwd)
            out += [tok, config_path]
            i += 2
            continue
        if tok.startswith("--config="):
            config_path = _abs(tok.split("=", 1)[1], cwd)
            out.append(f"--config={config_path}")
            i += 1
            continue
        out.append(tok)
        i += 1
    script = out[script_i]
    strata_dir = os.path.dirname(os.path.dirname(script))
    return LauncherInfo(bat_path=bat_path, argv=tuple(out), cwd=cwd, strata_dir=strata_dir, port=port,
                        config_path=config_path, model_name=model_name_for(config_path, bat_path))


def parse_bat(path: str) -> LauncherInfo:
    """Read + parse a launcher .bat. Raises LauncherError with a message fit for inline display."""
    if not str(path).lower().endswith((".bat", ".cmd")):
        raise LauncherError("Choose a .bat file")
    return parse_bat_text(read_bat_text(path), path)


def missing_files(info: LauncherInfo) -> str | None:
    """A short message when something the launcher needs is not on disk, else None."""
    for label, p in (("Python", info.argv[0]), ("server.py", next(t for t in info.argv[1:] if _is_server_script(t))),
                     ("Config", info.config_path)):
        if p and not os.path.isfile(p):
            return f"{label} not found: {p}"
    return None


# ------------------------------------------------------------------------------------------- config glue
def list_launchers(strata_dir: str | None) -> list[str]:
    """Absolute paths of every run-*.bat directly in strata_dir (sorted by name); [] if the folder is unusable."""
    if not strata_dir or not os.path.isdir(strata_dir):
        return []
    found = [p for p in glob.glob(os.path.join(glob.escape(strata_dir), "run-*.bat")) if os.path.isfile(p)]
    return sorted((os.path.normpath(p) for p in found), key=lambda p: os.path.basename(p).casefold())


def configured_bat(cfg: dict) -> str | None:
    """Absolute path of config['launcher_bat']; a v1.0 relative name resolves against strata_dir."""
    bat = str(cfg.get("launcher_bat") or "").strip()
    if not bat:
        return None
    if not os.path.isabs(bat):
        sdir = str(cfg.get("strata_dir") or "").strip()
        if not sdir:
            return None
        bat = os.path.join(sdir, bat)
    return os.path.normpath(bat)


def config_values(info: LauncherInfo) -> dict:
    return {"launcher_bat": info.bat_path, "launch_cmd": list(info.argv), "launch_cwd": info.cwd,
            "strata_dir": info.strata_dir, "port": info.port, "endpoint_url": info.endpoint_url,
            "model_name": info.model_name}


def apply_to_config(cfg: dict, info: LauncherInfo) -> dict:
    """Write the launcher-derived keys into cfg (in place) and return it."""
    cfg.update(config_values(info))
    return cfg


def same_launch(cfg: dict, info: LauncherInfo) -> bool:
    """True when cfg already launches exactly what `info` describes (nothing to apply)."""
    vals = config_values(info)
    cur_bat = configured_bat(cfg)
    if cur_bat is None or os.path.normcase(cur_bat) != os.path.normcase(info.bat_path):
        return False
    return all(cfg.get(k) == vals[k] for k in ("launch_cmd", "strata_dir", "port", "endpoint_url", "model_name")) \
        and str(cfg.get("launch_cwd") or cfg.get("strata_dir") or "") == info.cwd
