r"""Turn a Strata `run-*.bat` into the argv Hermes Controller launches itself.

The .bat is only READ, never executed. The parser understands what those launchers use in practice:
`cd /d "<dir>"` / `pushd`, `set VAR=value`, `%VAR%`, `%~dp0`, `^` line continuations, `call` / `start` prefixes,
quoted arguments and trailing redirections. From the line that runs `python ... serve\server.py ...` it keeps the
argv (minus `--open`, which would pop a browser tab), the port and the `--config` file.

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
_PY_NAMES = frozenset({"python", "pythonw", "py", "pyw"})
_VAR_RE = re.compile(r"%(?:~([a-zA-Z]*)0|(\*)|(\d)|(%)|([^%\r\n]+)%)")
_SET_RE = re.compile(r'^set\s+(?!/)(")?([^=\s"]+)=(.*)$', re.IGNORECASE)
_CD_RE = re.compile(r"^(?:cd|chdir|pushd)(?=[\s\"/])(?:\s*/d)?\s*(.*)$", re.IGNORECASE)
_SKIP_RE = re.compile(r"^(?:rem(?:\s|$)|::|:[A-Za-z_])", re.IGNORECASE)


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
    """%~dp0 and friends, %VAR% (bat-local `set` first, then the environment; unknown -> empty), %% -> %."""
    drive, tail = os.path.splitdrive(bat_path)
    folder, base = os.path.split(tail)
    stem, ext = os.path.splitext(base)
    if not folder.endswith(("\\", "/")):
        folder += os.sep

    def repl(m: re.Match) -> str:
        mods, star, digit, pct, name = m.groups()
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


def split_commands(line: str) -> list[str]:
    """Split one logical line at unquoted & | and drop redirections; unquoted ( ) and ^ escapes are removed."""
    segs: list[str] = []
    cur: list[str] = []
    in_q = False
    skipping = False                  # inside a redirection target: ignore until the next & or |
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == '"':
            in_q = not in_q
            if not skipping:
                cur.append(ch)
        elif in_q:
            if not skipping:
                cur.append(ch)
        elif ch == "^" and i + 1 < n:
            i += 1
            if not skipping:
                cur.append(line[i])
        elif ch == "&" and skipping and line[i - 1] in "<>":
            pass                      # "2>&1": a handle duplication, still part of the redirection
        elif ch in "&|":
            segs.append("".join(cur))
            cur, skipping = [], False
        elif ch in "<>":
            if not skipping and cur and cur[-1].isdigit() and (len(cur) == 1 or cur[-2].isspace()):
                cur.pop()             # the handle number of "2>"
            skipping = True
        elif ch in "()":
            if not skipping:
                cur.append(" ")
        elif not skipping:
            cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


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


def _is_python(tok: str) -> bool:
    name = os.path.basename(tok.replace("/", "\\")).casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    return name in _PY_NAMES or name.startswith("python")


def _abs(path: str, cwd: str) -> str:
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _resolve_interpreter(tok: str, cwd: str) -> str:
    if os.path.isabs(tok) or "\\" in tok or "/" in tok:
        full = _abs(tok, cwd)
        if not os.path.splitext(full)[1] and not os.path.isfile(full) and os.path.isfile(full + ".exe"):
            full += ".exe"
        return full
    local = os.path.join(cwd, tok if os.path.splitext(tok)[1] else tok + ".exe")
    if os.path.isfile(local):                     # cmd looks in the current directory first
        return os.path.normpath(local)
    return shutil.which(tok) or tok


def _find_server_argv(tokens: list[str]) -> list[str] | None:
    """tokens[p:] where p is the python interpreter that runs serve\\server.py, or None."""
    for i, tok in enumerate(tokens):
        if i == 0 or not _is_server_script(tok):
            continue
        for j in range(i - 1, max(-1, i - 7), -1):      # skip interpreter options (-u, -X utf8, ...)
            if _is_python(tokens[j]):
                return tokens[j:]
            if _is_server_script(tokens[j]):
                break
    return None


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
    argv: list[str] | None = None
    for raw in logical_lines(text):
        line = raw.strip().lstrip("@").strip()
        if not line or _SKIP_RE.match(line):
            continue
        line = expand_vars(line, bat_path, variables, cwd)
        m = _SET_RE.match(line)
        if m:
            value = m.group(3)
            if m.group(1):                               # set "VAR=value"
                value = value.rsplit('"', 1)[0] if '"' in value else value
            variables[m.group(2).upper()] = value.strip()
            continue
        for seg in split_commands(line):
            m = _CD_RE.match(seg)
            if m:
                target = m.group(1).strip().strip('"').strip()
                if target:
                    cwd = _abs(target, cwd)
                continue
            argv = _find_server_argv(tokenize(seg))
            if argv:
                break
        if argv:
            break
    if not argv:
        raise LauncherError(f"{bat_name} does not start Strata (no python … serve\\server.py line)")

    script_i = next(i for i, t in enumerate(argv) if i > 0 and _is_server_script(t))
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
