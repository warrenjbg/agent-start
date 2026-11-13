r"""Per-model settings for llama.cpp: each model file remembers its own arguments.

The arguments of the launch in config.json (`launch_cmd`) stay the single source of truth for the model that runs.
What this module adds is a memory, `llama_models` in config.json: {normalised model path: {"args": [...]}}, the
arguments each model had when it was last the configured one. Switching model (Settings, the switch button, the
tray) saves the outgoing model's own arguments there and brings the incoming model's back.

Own versus shared:
  * shared (they stay when the model changes): --port, --host, --api-key, --api-key-file, --ssl-key-file,
    --ssl-cert-file (SHARED_FLAGS). The program (the build) is not an argument and is shared too.
  * the model itself: -m / --model (and -hf, --model-url ...: a launch that names its model that way has no file
    to remember settings for).
  * own: everything else, in the order it was written (context, vision file, sampling, slots ...).

A model picked for the first time gets a copy of the current model's own arguments WITHOUT context (-c), vision
(--mmproj ...) and --alias: those three describe one model and are wrong for another.

Short names: when this module builds a model's arguments (a switch, a first set-up) and they have no --alias, it
adds `--alias <file name without .gguf>` once per model - llama-server otherwise reports the full file path as the
model's name, to every client. "Once" is the entry's "alias" mark: an alias the user removed is not put back.

An entry may also carry "fit_context": the context size the GPU-fit check found for this model (see
engines.llama_cpp); it is dropped as soon as the model's arguments name another context.

A config.json without `llama_models` (every version before 1.13) works as it is: the current arguments are the
current model's settings, and its entry is written at the first save or switch.

Pure functions on lists and dicts: no processes, no network. One os.path.isfile per remembered vision file when a
model's arguments are recalled. Never raises (a broken table reads as empty).
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

import llama_finder

KEY = "llama_models"
MAX_MODELS = 200                       # entries kept; the oldest are dropped first
MAX_ARGS = 400                         # arguments kept per model
SHARED_FLAGS = ("--port", "--host", "--api-key", "--api-key-file", "--ssl-key-file", "--ssl-cert-file")
ALIAS_FLAGS = ("--alias", "-a")
_MODEL_FLAGS = llama_finder._MODEL_VALUE_FLAGS | llama_finder._MODEL_SOURCE_FLAGS

Snapshot = tuple[tuple[str, str], ...]     # ((model key, JSON of its entry), ...): hashable, travels in LlamaLaunch


@dataclass(frozen=True)
class Parts:
    model: str | None                  # the model file the arguments name (the last -m, as the server reads it)
    shared: tuple[str, ...]            # SHARED_FLAGS options with their values, in order
    own: tuple[str, ...]               # everything else, in order


def split(args: Any) -> Parts:
    """The arguments as (model, shared, own). Pure; junk gives Parts(None, (), ())."""
    try:
        if args is None or isinstance(args, (str, bytes)):
            return Parts(None, (), ())
        toks = [str(a) for a in args]
    except Exception:  # noqa: BLE001
        return Parts(None, (), ())
    model: str | None = None
    shared: list[str] = []
    own: list[str] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        # only the option's NAME is normalised ('_' = '-'): a value after '=' is a path or a key and stays as typed
        head, eq, val = tok.partition("=") if tok.startswith("--") else (tok, "", "")
        name = llama_finder._flag(head)
        if name in _MODEL_FLAGS:
            if name in llama_finder._MODEL_VALUE_FLAGS:
                if eq:
                    model = val
                elif i + 1 < len(toks):
                    model = toks[i + 1]
            i += 1 if eq else 2
            continue
        if name in SHARED_FLAGS:
            if eq:
                shared.append(tok)
                i += 1
            else:
                shared.extend(toks[i:i + 2])
                i += 2
            continue
        own.append(tok)
        i += 1
    return Parts(model.strip('"') if model else None, tuple(shared), tuple(own))


def model_key(model: Any, base_dir: str = "") -> str:
    """The table key for a model file: its absolute path, normalised (a relative path counts from base_dir, the
    program's folder, where the server starts). '' when there is nothing to key on."""
    try:
        text = str(model or "").strip().strip('"')
        if not text:
            return ""
        if not os.path.isabs(text) and base_dir:
            text = os.path.join(base_dir, text)
        return os.path.normcase(os.path.normpath(text))
    except Exception:  # noqa: BLE001
        return ""


def short_name(model: Any) -> str:
    """The alias offered for a model file: its name without .gguf and without a split suffix; '' when that would not
    survive as one --alias value (empty, a comma - the option takes a comma list - or a leading dash)."""
    try:
        base = os.path.basename(str(model or "").strip().strip('"').replace("/", os.sep))
        name, _part = llama_finder._model_name(base)
        name = name.strip()
        if not name or "," in name or name.startswith("-") or any(ord(c) < 32 for c in name):
            return ""
        return name
    except Exception:  # noqa: BLE001
        return ""


def first_visit(own: Iterable[str]) -> tuple[str, ...]:
    """The own arguments a never-configured model starts with: the current ones without context, vision and alias."""
    out = llama_finder.with_context(tuple(own), None)
    out = llama_finder.with_vision(out, None)
    return llama_finder.with_option(out, ALIAS_FLAGS, None)


def with_short_name(own: Sequence[str], model: Any) -> tuple[str, ...]:
    """`own` plus `--alias <short name>` when it names no alias and the model has a usable short name."""
    own = tuple(str(a) for a in own)
    if llama_finder.option_value(own, ALIAS_FLAGS) is not None:
        return own
    name = short_name(model)
    return own + (ALIAS_FLAGS[0], name) if name else own


def _clean_entry(raw: Any) -> dict | None:
    """A table entry as this module writes it, or None for junk."""
    if not isinstance(raw, dict):
        return None
    args = raw.get("args")
    if not isinstance(args, list) or len(args) > MAX_ARGS or not all(isinstance(a, str) and "\x00" not in a
                                                                     for a in args):
        return None
    out: dict = {"args": list(args)}
    if raw.get("alias") is True:
        out["alias"] = True
    fit = raw.get("fit_context")
    if isinstance(fit, int) and not isinstance(fit, bool) and fit > 0 and llama_finder.context_of(args) == fit:
        out["fit_context"] = fit
    return out


def table_of(cfg: Any) -> dict[str, dict]:
    """A validated COPY of the config's table ({} for none / junk). Never the config's own dict."""
    try:
        raw = cfg.get(KEY) if isinstance(cfg, Mapping) else None
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for k, v in raw.items():
            ent = _clean_entry(v)
            if isinstance(k, str) and k and ent is not None:
                out[k] = ent
        return out
    except Exception:  # noqa: BLE001
        return {}


def entry_of(cfg: Any, model: Any, base_dir: str = "") -> dict | None:
    """The remembered entry for a model file (a copy), or None."""
    return table_of(cfg).get(model_key(model, base_dir))


def _put(table: dict[str, dict], key: str, own: Iterable[str], *, alias: bool | None = None,
         fit_context: int | None = None) -> None:
    """Write one entry. `alias` None keeps the mark the entry has; fit_context None keeps the stored value while
    the arguments still name that context."""
    if not key:
        return
    old = table.get(key) or {}
    args = [str(a) for a in own][:MAX_ARGS]
    ent: dict = {"args": args}
    if alias or (alias is None and old.get("alias") is True):
        ent["alias"] = True
    fit = fit_context if fit_context is not None else old.get("fit_context")
    if isinstance(fit, int) and not isinstance(fit, bool) and fit > 0 and llama_finder.context_of(args) == fit:
        ent["fit_context"] = fit
    table.pop(key, None)                 # re-insert: the newest entry is the last one
    table[key] = ent
    while len(table) > MAX_MODELS:
        table.pop(next(iter(table)))


def _usable(own: Sequence[str], exists: Callable[[str], bool]) -> tuple[str, ...]:
    """Remembered arguments as they can be used now: a vision file that is gone (absolute path, no such file) is
    taken out, so a deleted projector does not stop the server from starting."""
    own = tuple(str(a) for a in own)
    try:
        v = llama_finder.vision_of(own)
        if v and os.path.isabs(v) and not exists(v):
            return llama_finder.with_vision(own, None)
    except Exception:  # noqa: BLE001
        pass
    return own


def _snapshot(table: Mapping[str, dict], keys: Iterable[str]) -> Snapshot:
    return tuple((k, json.dumps(table[k], sort_keys=True, ensure_ascii=False)) for k in sorted(set(keys))
                 if k in table)


def _read_snapshot(models: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        for item in models or ():
            if not (isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], str) and item[0]
                    and isinstance(item[1], str)):
                continue
            ent = _clean_entry(json.loads(item[1]))
            if ent is not None:
                out[item[0]] = ent
    except Exception:  # noqa: BLE001
        return {}
    return out


class Session:
    """A working copy of the table for one run of choices (one Settings window, one model menu): model changes made
    here are remembered here, and only `touched()` - what really changed - goes back with the launch that is saved.
    """

    def __init__(self, cfg: Any, base_dir: str = "", exists: Callable[[str], bool] = os.path.isfile) -> None:
        self._table = table_of(cfg)
        self._base = str(base_dir or "")
        self._exists = exists
        self._touched: set[str] = set()

    def _set(self, key: str, own: Iterable[str], alias: bool | None = None) -> None:
        if not key:
            return
        before = self._table.get(key)
        _put(self._table, key, own, alias=alias)
        if self._table.get(key) != before:
            self._touched.add(key)

    def switch(self, args: Any, model: Any) -> tuple[str, ...]:
        """The arguments for `model`, coming from `args`: the shared options stay, the outgoing model's own
        arguments are remembered, the incoming model's are recalled (or it gets its first-visit copy). The same
        model: `args` with only the model path replaced. Never raises."""
        try:
            parts = split(args)
            new_key = model_key(model, self._base)
            if parts.model is not None:
                old_key = model_key(parts.model, self._base)
                if old_key == new_key:
                    return llama_finder.with_model(tuple(str(a) for a in args), str(model))
                self._set(old_key, parts.own)
            ent = self._table.get(new_key)
            own = _usable(ent["args"], self._exists) if ent is not None else first_visit(parts.own)
            if not (ent is not None and ent.get("alias") is True):
                own = with_short_name(own, model)
            self._set(new_key, own, alias=True)
            return ("-m", str(model), *parts.shared, *own)
        except Exception:  # noqa: BLE001
            return llama_finder.with_model(args, str(model))

    def first_setup(self, model: Any, port: int) -> tuple[str, ...]:
        """The arguments for a model when nothing is configured yet: model, port and its short name."""
        own = with_short_name((), model)
        self._set(model_key(model, self._base), own, alias=True)
        return (*llama_finder.default_args(str(model), port), *own)

    def touched(self) -> Snapshot:
        """What this session changed, to travel with the launch (LlamaLaunch.models)."""
        return _snapshot(self._table, self._touched)


def switch_args(cfg: Any, args: Any, model: Any, base_dir: str = "") -> tuple[tuple[str, ...], Snapshot]:
    """One switch outside a Settings window: (the arguments for `model`, the entries to save with them)."""
    s = Session(cfg, base_dir)
    out = s.switch(args, model)
    return out, s.touched()


def apply(cfg: dict, old_args: Any, new_args: Any, models: Any = (), base_dir: str = "") -> None:
    """Bring the config's table up to date for a launch that is being written: the outgoing arguments are
    remembered under their model (old_args: the arguments the config had, None when they were not llama.cpp's),
    then the entries the launch carries, then the new arguments under theirs. Writes cfg[KEY] as a NEW dict (a
    shallow copy of the config keeps its table). Never raises."""
    try:
        table = table_of(cfg)
        before = json.dumps(table, sort_keys=True)
        if old_args is not None:
            old = split(old_args)
            if old.model is not None:
                _put(table, model_key(old.model, base_dir), old.own)
        for key, ent in _read_snapshot(models).items():
            _put(table, key, ent["args"], alias=ent.get("alias") is True or None, fit_context=ent.get("fit_context"))
        new = split(new_args)
        if new.model is not None:
            _put(table, model_key(new.model, base_dir), new.own)
        if table:
            if json.dumps(table, sort_keys=True) != before or not isinstance(cfg.get(KEY), dict):
                cfg[KEY] = table
        else:
            cfg.pop(KEY, None)
    except Exception:  # noqa: BLE001 - the launch itself is saved by the caller; only the memory is lost
        return


def same(cfg: Any, models: Any) -> bool:
    """True when every entry a launch carries is already in the config's table as it is."""
    try:
        table = table_of(cfg)
        return all(table.get(k) == ent for k, ent in _read_snapshot(models).items())
    except Exception:  # noqa: BLE001
        return True


def set_fit_context(models: Any, cfg: Any, args: Any, tokens: int, base_dir: str = "") -> Snapshot:
    """The launch's entries plus the mark that `tokens` is the size the GPU-fit check found for the arguments'
    model (the arguments must already carry -c tokens)."""
    try:
        parts = split(args)
        key = model_key(parts.model, base_dir) if parts.model else ""
        if not key:
            return tuple(models or ())
        table = table_of(cfg)
        table.update(_read_snapshot(models))
        _put(table, key, parts.own, fit_context=int(tokens))
        keys = {k for k, _v in (models or ())} | {key}
        return _snapshot(table, keys)
    except Exception:  # noqa: BLE001
        return tuple(models or ())


def fit_context_of(cfg: Any, models: Any, args: Any, base_dir: str = "") -> int | None:
    """The context the fit check found for the arguments' model, while the arguments still name it."""
    try:
        parts = split(args)
        key = model_key(parts.model, base_dir) if parts.model else ""
        ent = _read_snapshot(models).get(key) or table_of(cfg).get(key)
        fit = ent.get("fit_context") if ent else None
        return fit if isinstance(fit, int) and llama_finder.context_of(parts.own) == fit else None
    except Exception:  # noqa: BLE001
        return None
