"""The app's worker functions and small pure helpers, moved verbatim out of main.py (module-level functions).

Each function here is the old main.py function, character for character. The app hands the workers to its worker
threads (they post their result back with `post(done, ...)` and make no Tk call); the helpers turn a result into
the text the window shows. main.py re-exports every name, so main.format_preview, main._fit_check_worker and the
others are these same objects.

FIT_PRECHECK_WAIT_S moved with _fit_check_worker, the one function that reads it: a test that shortens the wait
sets app_workers.FIT_PRECHECK_WAIT_S. Workers that read a name the app also reads (_reopen_clicked and
REOPEN_STAGGER_S) or that the tests replace on main (_toast_worker, _relaunch_app, _protocol_registered) stay in
main.py. This module never imports main.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from typing import Any, Callable

import config_store
import engine_profiles
import engines
import reopen
from text_util import gb as _gb, short as _short

log = logging.getLogger("agent_start")


def format_preview(res: dict) -> str:
    """Two-line IDLE preview from a sweep_processes(dry_run=True) result (what ON would offer to close)."""
    n = int(res.get("candidates") or 0)
    m = int(res.get("preselected") or 0)
    if n == 0:
        return "Nothing to close"
    # line 1 sits on the status line (short); line 2 is the hidden detail (which apps)
    if m > 0:
        line1 = f"{m} app{'s' if m != 1 else ''} to close · {_gb(res.get('preselected_ram_mb'))}"
    else:
        line1 = f"{n} app{'s' if n != 1 else ''} could close · {_gb(res.get('ram_freed_mb'))}"
    # name the apps the RAM figure refers to: the preselected set if any, else everything
    pool = res.get("preselected_names") if m > 0 and res.get("preselected_names") else res.get("names")
    names = [_short(str(x)) for x in (pool or [])]
    shown: list[str] = []
    for nm in names:                            # fit one line (~46 chars at 12 px in 328 px)
        if shown and len(", ".join(shown + [nm])) + 8 > 46:
            break
        shown.append(nm)
    line2 = ", ".join(shown)
    if len(names) > len(shown):
        line2 += f" +{len(names) - len(shown)} more"
    return f"{line1}\n{line2}" if line2 else line1


def _released(result: dict) -> bool:
    """A stop result's verdict: the engine no longer holds the model. 'released' is the engine-neutral field;
    'port_released' (the process is gone and its port is free) is what it means for engines we end."""
    return bool(result.get("released", result.get("port_released")))


def _stop_info(title: str, reason: str = "game") -> dict:
    """What a pending stop carries when an app caused it: the app's name, plus why when it was not a game start."""
    return {"game": title} if reason == "game" else {"game": title, "reason": reason}


def _save_backend_worker(old_id: str, engine_id: str, post: Callable, done: Callable) -> None:
    """Worker: make `engine_id` the engine in config.json (re-read first: keep whatever else changed on disk).
    The running engine's launch settings are parked under its name and the new engine's own come back
    (engine_profiles). Posts done(engine_id, ok)."""
    ok = False
    try:
        disk = config_store.load_config()
        config_store.save_config(engine_profiles.switch(disk, old_id, engine_id, config_store.DEFAULTS))
        ok = True
    except Exception:  # noqa: BLE001
        log.exception("saving the engine choice failed")
    post(done, engine_id, ok)


def _park_foreign_config() -> None:
    """Start-up: launch settings that belong to another engine (the engine was changed by 1.11, which did not keep
    them apart, or by hand) are parked under that engine, so they come back when the user switches to it."""
    try:
        cfg = config_store.load_config()
        all_engines = [engines.get_engine({"backend": i}) for i in engines.available()]
        fixed = engine_profiles.park_foreign(cfg, engines.get_engine(cfg), all_engines, config_store.DEFAULTS)
        if fixed is not None:
            config_store.save_config(fixed)
            log.info("launch settings of another engine were found and kept for it (%s)",
                     ", ".join(sorted(fixed.get(engine_profiles.PROFILES_KEY) or {})))
    except Exception:  # noqa: BLE001
        log.exception("could not sort the launch settings by engine")


FIT_PRECHECK_WAIT_S = 6.0                # the launch waits at most this long for the GPU-fit pre-check


def _fit_check_worker(engine: Any, cfg: dict, chk: Any, post: Callable, done: Callable, token: int) -> None:
    """Worker: the engine's read-only GPU-fit pre-check for this start (fit_check.run is the only place that runs
    the tool, and only from the engine program's own folder). ALWAYS posts done(token, fits) exactly once - the
    launch follows it - with fits None when there is no answer within FIT_PRECHECK_WAIT_S."""
    box: list = []

    def work() -> None:
        try:
            import fit_check
            cmd = cfg.get("launch_cmd") or []
            prog = str(cmd[0]).strip().strip('"') if cmd else ""
            limit = min(float(getattr(chk, "timeout_s", FIT_PRECHECK_WAIT_S) or FIT_PRECHECK_WAIT_S),
                        FIT_PRECHECK_WAIT_S - 1.0)
            out = fit_check.run(dataclasses.replace(chk, timeout_s=limit), prog)
            rep = engine.parse_fit(out or "", cfg)
            log.info("GPU fit pre-check: %s", rep.detail or rep.fits)
            box.append(rep.fits)
        except Exception:  # noqa: BLE001 - optional: a start never depends on it
            log.debug("GPU fit pre-check failed", exc_info=True)

    t0 = time.monotonic()
    th = threading.Thread(target=work, name="hc-fit-run", daemon=True)
    try:
        th.start()
        th.join(FIT_PRECHECK_WAIT_S)
    except Exception:  # noqa: BLE001
        log.debug("GPU fit pre-check thread failed", exc_info=True)
    if th.is_alive():
        log.info("GPU fit pre-check gave no answer in %.0f s; starting without it", time.monotonic() - t0)
    post(done, token, box[0] if box and not th.is_alive() else None)


def _fit_largest_worker(engine: Any, cfg: dict, post: Callable, done: Callable) -> None:
    """Worker: the largest context that fits the GPU for this launch (Settings). The engine decides what to run
    and how to read it; fit_check.run stays the only place that runs the tool. ALWAYS posts done(tokens, state)
    exactly once."""
    tokens, state = None, "failed"
    try:
        import fit_check
        cmd = cfg.get("launch_cmd") or []
        prog = str(cmd[0]).strip().strip('"') if cmd else ""
        tokens, state = engine.largest_context(cfg, lambda chk: fit_check.run(chk, prog))
        log.info("largest context that fits the GPU: %s (%s)", tokens, state)
    except Exception:  # noqa: BLE001
        log.debug("largest-context check failed", exc_info=True)
        tokens, state = None, "failed"
    post(done, tokens, state)


def fit_largest_text(tokens: int | None, state: str, used_gb: float | None) -> str:
    """The line Settings shows under Context length after the largest-context check."""
    if state == "ok" and tokens:
        text = f"Fits the GPU at {tokens:,} tokens"
        if used_gb is not None:
            text += f" · measured with {used_gb:.1f} GB of video memory in use by other apps"
        return text
    if state == "full":
        return "The model's whole context fits the GPU · left on Auto"
    if state == "nofit":
        return "Does not fit the GPU even at the smallest context llama.cpp tries"
    if state == "unconfirmed":
        return "Two checks disagreed (video memory in use changed?) · try again"
    return "The check gave no answer"


def _refresh_autostart_worker(exe: str) -> None:
    """Start with Windows is on but points at another exe (the app moved / was reinstalled elsewhere): fix it."""
    try:
        import autostart
        if autostart.refresh(exe):
            log.info("start with Windows: updated to %s", exe)
    except Exception:  # noqa: BLE001
        log.debug("autostart refresh failed", exc_info=True)


def _find_install_worker(engine, post: Callable, done: Callable, stop_evt) -> None:
    """Worker: the engine's bounded, read-only search for an install on this PC."""
    found = []
    try:
        found = list(engine.find_installs(stop_evt) or [])
    except Exception:  # noqa: BLE001
        log.exception("searching for %s failed", engine.name)
    post(done, found)


def _save_reopen_records(records: list[dict]) -> None:
    """Worker: add this sweep's apps to the pending list (dedupe by program, newest wins)."""
    try:
        merged: dict[str, Any] = {}
        for r in list(reopen.load_pending()) + [reopen.LaunchRecord.from_dict(d) for d in records]:
            key = os.path.normcase(r.exe or r.aumid or r.name)
            merged[key] = r
        reopen.save_pending(list(merged.values()))
        log.info("reopen: %d app(s) will be reopened after Turn off", len(merged))
    except Exception:  # noqa: BLE001
        log.exception("saving reopen records failed")
