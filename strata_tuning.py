"""Context length and picture reading (vision) for a Strata model, without touching Strata's own files.

Strata keeps both inside the run config its launcher names (strata-<model>.json: `--max-context N` and `--vision` in
"args", plus a "vision" block). Agent Start never edits that file. When the user picks another value, the choice is
remembered in Agent Start's config.json (`strata_tuning`, keyed by the run config's path) and, at each start, a COPY
of the run config with only those options changed is written to Agent Start's own data folder and handed to Strata
with --config. Delete the choice and Strata gets its own file again.

Measured with Strata 0.2.10 (one PC): a copy with --max-context 131072 instead of 262144 loads and reports that
context; the same copy without --vision and without the "vision" block loads with pictures off; Strata does not
rewrite either file.

Pure functions plus one atomic file write. No processes, no network; never raises unless documented.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Mapping

TUNING_KEY = "strata_tuning"
CTX_FLAG, VISION_FLAG = "--max-context", "--vision"
COPY_SUFFIX = ".agentstart.json"
CONTEXT_CHOICES: tuple[tuple[str, int], ...] = (
    ("32K tokens", 32768), ("64K tokens", 65536), ("128K tokens", 131072), ("256K tokens", 262144))


@dataclass(frozen=True)
class Tuning:
    """What a run config says. context None = the file names none; vision_possible = the file can read pictures
    (it has --vision and a "vision" block), so vision can be offered."""
    context: int | None
    vision: bool
    vision_possible: bool


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def tuning_of(run_cfg: Mapping[str, Any] | None) -> Tuning | None:
    """What a Strata run config (the parsed JSON) is set to, or None when it has no engine arguments to read."""
    try:
        if not isinstance(run_cfg, Mapping) or not isinstance(run_cfg.get("args"), list):
            return None
        args = run_cfg["args"]
        toks = [str(a) for a in args]
        ctx = None
        for i, t in enumerate(toks[:-1]):
            if t == CTX_FLAG and toks[i + 1].isdigit() and int(toks[i + 1]) > 0:
                ctx = int(toks[i + 1])
        possible = VISION_FLAG in toks and isinstance(run_cfg.get("vision"), dict)
        return Tuning(ctx, possible, possible)
    except Exception:  # noqa: BLE001
        return None


def tuned(run_cfg: Mapping[str, Any], context: int | None, vision: bool | None) -> dict:
    """A deep copy of the run config with only these options changed: context (None = as in the file), vision
    (False = pictures off: the flag and the "vision" block go; True / None = as in the file - vision is never
    switched ON for a file that has none). Strata's own bookkeeping copy of the arguments is dropped."""
    out = copy.deepcopy(dict(run_cfg))
    args = [str(a) for a in (out.get("args") or [])]
    if context:
        new, placed, i = [], False, 0
        while i < len(args):
            if args[i] == CTX_FLAG and i + 1 < len(args):
                if not placed:
                    new += [CTX_FLAG, str(int(context))]
                    placed = True
                i += 2
                continue
            new.append(args[i])
            i += 1
        if not placed:
            new += [CTX_FLAG, str(int(context))]
        args = new
    if vision is False:
        args = [a for a in args if a != VISION_FLAG]
        out.pop("vision", None)
    out["args"] = args
    out.pop("args_written", None)
    return out


def stored(cfg: Mapping[str, Any], run_config_path: str | None) -> dict:
    """The user's choice for that run config from Agent Start's config: {} or {"context": int} / {"vision": bool}."""
    try:
        table = cfg.get(TUNING_KEY)
        entry = table.get(_key(run_config_path or "")) if isinstance(table, dict) and run_config_path else None
        if not isinstance(entry, dict):
            return {}
        out: dict = {}
        c = entry.get("context")
        if isinstance(c, int) and not isinstance(c, bool) and 1024 <= c <= 4_194_304:
            out["context"] = c
        if isinstance(entry.get("vision"), bool):
            out["vision"] = entry["vision"]
        return out
    except Exception:  # noqa: BLE001
        return {}


def store(cfg: dict, run_config_path: str, file_tuning: Tuning | None, context: int | None,
          vision: bool | None) -> None:
    """Remember the choice in Agent Start's config dict (in place). A value equal to what the file already says is
    not stored, and an empty choice removes the entry - then Strata gets its own file."""
    entry: dict = {}
    if context and (file_tuning is None or context != file_tuning.context):
        entry["context"] = int(context)
    if vision is False and (file_tuning is None or file_tuning.vision_possible):
        entry["vision"] = False
    table = cfg.get(TUNING_KEY)
    table = dict(table) if isinstance(table, dict) else {}
    if entry:
        table[_key(run_config_path)] = entry
    else:
        table.pop(_key(run_config_path), None)
    if table:
        cfg[TUNING_KEY] = table
    else:
        cfg.pop(TUNING_KEY, None)


def effective(cfg: Mapping[str, Any], run_config_path: str | None,
              file_tuning: Tuning | None) -> tuple[int | None, bool]:
    """(context, vision) the next start will use: the user's choice over the file's."""
    choice = stored(cfg, run_config_path)
    ctx = choice.get("context") or (file_tuning.context if file_tuning else None)
    vis = bool(file_tuning and file_tuning.vision_possible) and choice.get("vision") is not False
    return ctx, vis


def copy_path(run_config_path: str, out_dir: str) -> str:
    stem = os.path.splitext(os.path.basename(run_config_path))[0]
    return os.path.join(out_dir, stem + COPY_SUFFIX)


def write_copy(run_config_path: str, out_dir: str, choice: Mapping[str, Any]) -> str:
    """Write the tuned copy of `run_config_path` into `out_dir` (created if needed) and return its path. The
    original is only read. Raises OSError / ValueError when the original cannot be read or the copy not written."""
    run_cfg = read_json(run_config_path)
    if run_cfg is None or not isinstance(run_cfg.get("args"), list):
        raise ValueError(f"cannot read {os.path.basename(run_config_path)}")
    new = tuned(run_cfg, choice.get("context"), choice.get("vision"))
    os.makedirs(out_dir, exist_ok=True)
    target = copy_path(run_config_path, out_dir)
    fd, tmp = tempfile.mkstemp(prefix=".tuning-", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(new, fh, indent=2)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def context_label(tokens: int | None) -> str:
    for label, n in CONTEXT_CHOICES:
        if n == tokens:
            return label
    return f"{tokens:,} tokens" if tokens else "As in the launcher"
