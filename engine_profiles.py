"""Each engine keeps its own launch settings in config.json, so switching engines and back restores them.

The app reads the launch settings from the top level of the config (launch_cmd, port, ...) - that does not change.
What this module adds is `engine_profiles`: {engine id: {those keys}}, the parked settings of the engines that are
NOT in use. Pure functions on dicts: no files, no processes, never raises. A config.json without `engine_profiles`
(every version before 1.12) works as it is.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

PROFILES_KEY = "engine_profiles"
# Everything that says how ONE engine is started and reached. Nothing else in config.json belongs to an engine.
PROFILE_KEYS = ("launch_cmd", "launch_cwd", "launcher_bat", "strata_dir", "model_name", "endpoint_url", "port",
                "api_key")


def _profiles(cfg: Mapping[str, Any]) -> dict[str, dict]:
    raw = cfg.get(PROFILES_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def profile_of(cfg: Mapping[str, Any]) -> dict:
    """The live launch settings (top level), as a profile."""
    return {k: copy.deepcopy(cfg[k]) for k in PROFILE_KEYS if k in cfg}


def has_profile(cfg: Mapping[str, Any], engine_id: str) -> bool:
    return bool(_profiles(cfg).get(str(engine_id)))


def switch(cfg: Mapping[str, Any], old_id: str, new_id: str, defaults: Mapping[str, Any] | None = None) -> dict:
    """A copy of cfg for running `new_id`: the live settings are parked under `old_id`, `new_id`'s parked settings
    (if any) become live, else the launch settings go back to `defaults` (nothing configured). `backend` is set."""
    out = copy.deepcopy(dict(cfg))
    try:
        profiles = _profiles(out)
        old_id, new_id = str(old_id), str(new_id)
        if old_id != new_id:
            profiles[old_id] = profile_of(out)
            incoming = profiles.pop(new_id, None)
            for k in PROFILE_KEYS:
                out.pop(k, None)
            base = {k: copy.deepcopy(defaults[k]) for k in PROFILE_KEYS if defaults is not None and k in defaults}
            if not incoming:
                base["launch_cmd"], base["model_name"] = [], ""       # nothing configured for this engine yet
                for k in ("launch_cwd", "launcher_bat", "strata_dir"):
                    if k in base:
                        base[k] = ""
            out.update(base)
            if incoming:
                out.update({k: v for k, v in incoming.items() if k in PROFILE_KEYS})
        out["backend"] = new_id
        if profiles:
            out[PROFILES_KEY] = profiles
        else:
            out.pop(PROFILES_KEY, None)
    except Exception:  # noqa: BLE001 - contract: never raises; an unreadable profile table only loses the parking
        out = copy.deepcopy(dict(cfg))
        out["backend"] = str(new_id)
    return out


def port_choices(cfg: Mapping[str, Any], engine_id: str, own_port: Any,
                 installed: Iterable[tuple[str, str, Any]]) -> list[tuple[str, int]]:
    """The Port dropdown of an engine whose port is an argument: [(label, port)]. First the port each OTHER
    installed engine uses - its parked settings here, else its default - so clients set up for that engine reach
    this one without changes (only one engine runs at a time); last the engine's own default port.
    installed: (engine id, display name, default port). Pure; never raises ([] on junk)."""
    out: list[tuple[str, int]] = []
    try:
        own = int(own_port)
        parked = _profiles(cfg)
        for eid, name, default in installed:
            if str(eid) == str(engine_id):
                continue
            port = parked.get(str(eid), {}).get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                port = int(default)
            if 1 <= port <= 65535 and port != own and all(p != port for _l, p in out):
                out.append((f"Same as {name} · {port}", port))
        out.append((f"Own port · {own}", own))
    except Exception:  # noqa: BLE001
        return []
    return out


def park_foreign(cfg: Mapping[str, Any], engine: Any, others: Iterable[Any],
                 defaults: Mapping[str, Any] | None = None) -> dict | None:
    """The live settings belong to ANOTHER engine (the engine was changed by a version that did not park them, or
    by hand): park them under that engine and leave this one unconfigured. Returns the new config, or None when
    there is nothing to do. `engine` / `others`: objects with .id and .is_configured(cfg)."""
    try:
        if engine.is_configured(cfg):
            return None
        for other in others:
            oid = str(getattr(other, "id", "") or "")
            if not oid or oid == getattr(engine, "id", None) or has_profile(cfg, oid):
                continue
            if other.is_configured(cfg):
                out = switch(cfg, oid, str(engine.id), defaults)
                return out
    except Exception:  # noqa: BLE001
        return None
    return None
