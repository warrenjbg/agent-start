"""AliasController: keeps Hermes' "llama" alias in step with the model this app's engine serves.

Moved out of main.AgentStartApp with its behaviour unchanged. The controller owns the Settings switch, the memory
of what this app run already handled and the one hermes run in flight. The app tells it when a status poll saw a
served model (`check`), builds the Settings row from `supported` / `hermes_path` / `pref`, and shows its notes.
hermes_alias.py stays the one place that runs hermes.exe; this module only decides when.

Everything here runs on the Tk thread except `_alias_worker`, which posts its result back through the host.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import hermes_alias
from text_util import short as _short

log = logging.getLogger("agent_start")

ACTIVE = "ACTIVE"                                # the host's state name while the engine serves

ALIAS_LABEL = "Keep Hermes' \u201cllama\u201d alias in step"
ALIAS_LABEL_MISSING = "Hermes \u201cllama\u201d alias (Hermes not found)"    # short: the row has room for ~40 chars


def _alias_worker(exe: str, model: str, url: str, post: Callable, done: Callable) -> None:
    """Worker: bring Hermes' alias in step (one to three `hermes config` runs, about 1.4 s each)."""
    try:
        res = hermes_alias.sync_alias(model, url, exe=exe)
    except Exception:  # noqa: BLE001
        log.exception("hermes alias: the update failed")
        res = hermes_alias.AliasResult(False, False, "Hermes alias not updated: an unexpected error occurred")
    post(done, (model, url), res)


def _alias_note(res: Any) -> str:
    """The module's sentence as the two short lines of a result note."""
    head, _, why = str(getattr(res, "note", "") or "").partition(": ")
    why = why.split("; check it with", 1)[0].strip()
    head = head.strip() or "Hermes alias not updated"
    return f"{head}\n{_short(why, 40)}" if why else head


@dataclass
class AliasHost:
    """What the controller needs from the app. All callables are called on the Tk thread, except post (any thread)."""
    post: Callable[..., None]                    # thread-safe: run fn(*args) on the Tk thread
    run_worker: Callable[..., None]              # run_worker(name, fn, *args) on a daemon thread
    set_note: Callable[[str], None]              # one-off result line on the main window
    save_state_later: Callable[[str, Any], None]  # debounced state.json write
    state: Callable[[], str]                     # "IDLE" | "STARTING" | "ACTIVE" | "STOPPING" | "ERROR"
    loaded: Callable[[], Any]                    # True once the served model is loaded (None: unknown)
    model_id: Callable[[], Any]                  # the model name the server reports
    port: Callable[[], Any]                      # the port as saved in the config (checked here)
    engine: Callable[[], Any]                    # the current engine object (asked for syncs_hermes_alias)
    destroyed: Callable[[], bool]                # the app is closing


class AliasController:
    def __init__(self, host: AliasHost, pref: bool = False, can_run: bool = False, exe: Any = None) -> None:
        """pref: the Settings switch (state.json "hermes_alias"; off unless it says true).
        can_run: False for a stub-built app (the test suites), which never runs hermes.
        exe: False = hermes.exe not looked for yet; else its path, or None when it was not found."""
        self.host = host
        self.pref = bool(pref)
        self.can_run = bool(can_run)
        self.done: tuple[str, str] | None = None     # (model, address) already handled in this app run
        self.busy = False                        # a hermes run is in flight
        self.exe: Any = exe

    def supported(self) -> bool:
        return bool(getattr(self.host.engine(), "syncs_hermes_alias", False))

    def hermes_path(self) -> str | None:
        """hermes.exe, looked for once per app run (file checks only; nothing is started)."""
        if self.exe is False:
            self.exe = hermes_alias.find_hermes()
        return self.exe or None

    def set_pref(self, on: bool) -> None:
        """Settings switch. On: the alias is brought in step now if the engine serves a model, and from then on
        whenever the served model or the port changes. Off: the alias is left as it is."""
        self.pref = bool(on)
        self.host.save_state_later("hermes_alias", self.pref)
        log.info("keep the Hermes alias in step: %s", "on" if on else "off")
        self.done = None
        self.check()

    def target(self) -> tuple[str, str] | None:
        """(the model name the server reports, its loopback address) while this app's engine serves a loaded
        model; None at any other time - the alias is never pointed at a server that is not up."""
        if self.host.state() != ACTIVE or self.host.loaded() is not True or not self.host.model_id():
            return None
        port = self.host.port()
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        return str(self.host.model_id()), f"http://127.0.0.1:{port}/v1"

    def check(self) -> None:
        """Every status poll while serving, and when the switch changes (Tk thread): start one update when the
        served model or address is one this app run has not handled yet. One attempt per (model, address): a
        failure shows a note and is not retried until the model changes or the switch is turned on again. An
        extra: it must never break the status path, so every error ends here."""
        try:
            if not (self.pref and self.can_run and self.supported()) or self.busy \
                    or self.host.destroyed():
                return
            target = self.target()
            if target is None or target == self.done:
                return
            self.done = target
            exe = self.hermes_path()
            if exe is None:
                log.info("hermes alias: %s not found, nothing updated", hermes_alias.EXE_NAME)
                return
            self.busy = True
            self.host.run_worker("hc-alias", _alias_worker, exe, target[0], target[1], self.host.post, self.result)
        except Exception:  # noqa: BLE001
            self.busy = False
            log.debug("hermes alias check failed", exc_info=True)

    def result(self, target: tuple[str, str], res: Any) -> None:
        self.busy = False
        if self.host.destroyed():
            return
        if getattr(res, "changed", False):
            self.host.set_note(f"Hermes alias {hermes_alias.ALIAS} updated\n{_short(target[0], 40)}")
        elif not getattr(res, "ok", False):
            self.host.set_note(_alias_note(res))
        self.check()                             # the model may have changed while hermes ran
