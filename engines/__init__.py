"""Engine registry. `get_engine(cfg)` picks the engine named by config.json's `backend` key.

Adding an engine: write engines/<name>.py with an `Engine` subclass that sets `id` (see docs/engines.md). The
registry finds every module in this package by itself; nothing else in the app needs to change.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Callable, Mapping

from .base import Engine

log = logging.getLogger("agent_start")

DEFAULT_BACKEND = "strata"


def _strata() -> Engine:
    from .strata import StrataEngine
    return StrataEngine()


_FACTORIES: dict[str, Callable[[], Engine]] = {
    "strata": _strata,          # always known, even if discovery fails
}
_discovered = False


def _discover() -> None:
    """Import every module of this package once and register its Engine subclasses by their `id`. A module
    that fails to import is logged and skipped: one broken engine must not take the app down."""
    global _discovered
    if _discovered:
        return
    _discovered = True
    for mod in pkgutil.iter_modules(__path__):
        if mod.name in ("base",) or mod.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception:  # noqa: BLE001
            log.exception("engine module %s could not be loaded", mod.name)
            continue
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Engine) and obj is not Engine \
                    and obj.__module__ == module.__name__ and isinstance(getattr(obj, "id", None), str) and obj.id:
                _FACTORIES.setdefault(obj.id.strip().casefold(), obj)


def available() -> list[str]:
    _discover()
    return sorted(_FACTORIES)


def choices() -> list[tuple[str, str]]:
    """(id, display name) of every available engine, for the Settings "Engine" row."""
    out: list[tuple[str, str]] = []
    for eid in available():
        try:
            out.append((eid, str(_FACTORIES[eid]().name)))
        except Exception:  # noqa: BLE001 - a broken engine is not offered
            log.exception("engine %s could not be created", eid)
    return out


def backend_of(cfg: Mapping[str, Any] | None) -> str:
    """The configured backend id; a missing, empty or non-text value means the default (configs written before
    the key existed keep working)."""
    raw = (cfg or {}).get("backend")
    return raw.strip().casefold() if isinstance(raw, str) and raw.strip() else DEFAULT_BACKEND


def get_engine(cfg: Mapping[str, Any] | None = None) -> Engine:
    """The engine for this config. An unknown `backend` falls back to the default with a warning in the log:
    the app must still open so the user can fix the setting."""
    name = backend_of(cfg)
    factory = _FACTORIES.get(name)
    if factory is None:
        _discover()
        factory = _FACTORIES.get(name)
    if factory is None:
        log.warning("config: unknown backend %r - using %r (known: %s)", name, DEFAULT_BACKEND, ", ".join(available()))
        factory = _FACTORIES[DEFAULT_BACKEND]
    return factory()
