"""Reopen the apps a sweep closed, once Strata is turned off.

detector.ProcessDetector.sweep_processes(dry_run=False, targets=...) returns
result["launch_records"]: one dict per app it actually closed, captured from the app's ROOT process
before it was terminated. This module turns those dicts into LaunchRecords, decides what may be started
again automatically, persists them across a controller restart and launches them.

Safety rules (all enforced here, not by the caller):
  * Games are never relaunched automatically: classify() puts them in `manual` so the UI can offer a
    one-click "Reopen" (reopen_one()).
  * Never relaunched: elevated processes ("needs administrator" - a non-elevated controller cannot and
    should not start them), programs whose exe is gone ("program not found"), anything under %WINDIR%
    ("system program"), packaged background apps ("Windows restarts it itself") and records with neither
    an exe nor an AUMID ("nothing to launch"). launch() re-applies the same rules, so a direct call
    cannot bypass them.
  * No shell, ever: no shell=True, no cmd.exe, no .bat/.cmd (CreateProcess would route those through
    cmd.exe). The binary is always rec.exe (passed as `executable`), it must be an existing .exe, and the
    recorded argv[0] must resolve to that same file or the launch is refused.
  * Packaged (Store/MSIX) apps are started through their AppUserModelID:
    explorer.exe shell:AppsFolder\\<aumid> - the only supported way to activate them.
  * An app the user already reopened by hand (same exe running) is reported "already running" and not
    started twice.

Thread-safety: no Tk, no globals mutated except through config_store (which locks). Every public
function may run on a worker thread; reopen() blocks for ~stagger_s per launched app.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

import psutil

import config_store

log = logging.getLogger("agent_start")

STATE_KEY = "reopen_pending"

SKIP_ELEVATED = "needs administrator"
SKIP_MISSING = "program not found"
SKIP_SYSTEM = "system program"
SKIP_PACKAGED_BG = "Windows restarts it itself"
SKIP_NOTHING = "nothing to launch"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# Package family + "!" + app id, e.g. Microsoft.WindowsCalculator_8wekyb3d8bbwe!App. No spaces, quotes,
# slashes or shell metacharacters can get through to explorer's argument.
_AUMID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*_[A-Za-z0-9]+![A-Za-z0-9][A-Za-z0-9._\-]*\Z")
# environment the venv redirector injects into us; must not leak into relaunched apps
_STRIP_ENV = ("__PYVENV_LAUNCHER__",)


# --------------------------------------------------------------------------------------------- record

@dataclass
class LaunchRecord:
    """How to start a swept app again (mirrors one detector launch-record dict)."""
    name: str = ""
    exe: str = ""
    cmdline: list[str] = field(default_factory=list)
    cwd: str = ""
    category: str = "background"
    create_time: float = 0.0
    elevated: bool = False
    aumid: str | None = None
    reason_skip: str | None = None

    @property
    def packaged(self) -> bool:
        return bool(self.aumid)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LaunchRecord":
        """Tolerant: missing keys get defaults, wrong types are coerced (state.json may be hand-edited)."""
        d = d if isinstance(d, dict) else {}
        cmd = d.get("cmdline")
        try:
            ct = float(d.get("create_time") or 0.0)
        except (TypeError, ValueError):
            ct = 0.0
        aumid = d.get("aumid")
        skip = d.get("reason_skip")
        return cls(
            name=str(d.get("name") or ""),
            exe=str(d.get("exe") or ""),
            cmdline=[str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [],
            cwd=str(d.get("cwd") or ""),
            category=str(d.get("category") or "background"),
            create_time=ct,
            elevated=bool(d.get("elevated", False)),
            aumid=str(aumid) if aumid else None,
            reason_skip=str(skip) if skip else None,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cmdline"] = list(self.cmdline)
        return d


def _coerce(r: LaunchRecord | dict) -> LaunchRecord:
    return r if isinstance(r, LaunchRecord) else LaunchRecord.from_dict(r)


# --------------------------------------------------------------------------------------------- rules

def _norm_path(p: str | None) -> str:
    if not p:
        return ""
    try:
        return os.path.normcase(os.path.abspath(p))
    except (OSError, ValueError):
        return ""


def _windir() -> str:
    return os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"


def _under(path: str, directory: str) -> bool:
    p, d = _norm_path(path), _norm_path(directory)
    return bool(p and d) and p.startswith(d.rstrip(os.sep) + os.sep)


def skip_reason(rec: LaunchRecord) -> str | None:
    """The reason `rec` must never be relaunched, or None. Shared by classify() and launch()."""
    if not rec.exe and not rec.aumid:
        return SKIP_NOTHING
    if rec.elevated:
        return SKIP_ELEVATED
    if rec.aumid and rec.category == "background":
        return SKIP_PACKAGED_BG
    if rec.exe and _under(rec.exe, _windir()):
        return SKIP_SYSTEM
    if rec.exe and not os.path.isfile(rec.exe):
        return SKIP_MISSING
    if rec.aumid and not _AUMID_RE.match(rec.aumid) and not rec.exe:
        return SKIP_NOTHING
    return None


def classify(records: Iterable[LaunchRecord | dict]
             ) -> tuple[list[LaunchRecord], list[LaunchRecord], list[LaunchRecord]]:
    """Split records into (auto, manual, skipped), keeping input order.

    skipped: any skip_reason() hit (reason_skip set) - checked first, so an elevated or uninstalled game
             is skipped rather than offered.
    manual:  category 'game' (offered with a one-click Reopen, never started automatically).
    auto:    everything else (reason_skip None).
    """
    auto: list[LaunchRecord] = []
    manual: list[LaunchRecord] = []
    skipped: list[LaunchRecord] = []
    for r in records or ():
        try:
            rec = _coerce(r)
            rec.reason_skip = skip_reason(rec)
        except Exception:  # noqa: BLE001 - a malformed record is skipped, not fatal
            log.exception("reopen: bad launch record %r", r)
            continue
        if rec.reason_skip:
            skipped.append(rec)
        elif rec.category == "game":
            manual.append(rec)
        else:
            auto.append(rec)
    return auto, manual, skipped


# --------------------------------------------------------------------------------------------- persistence

def save_pending(records: Iterable[LaunchRecord | dict]) -> None:
    """Persist (replace) the pending list in state.json: {'reopen_pending': {'saved_at', 'records'}}.

    Best effort: config_store.save_state is atomic, locked and never raises.
    """
    recs = []
    for r in records or ():
        try:
            recs.append(_coerce(r).to_dict())
        except Exception:  # noqa: BLE001
            log.exception("reopen: cannot save record %r", r)
    config_store.save_state({STATE_KEY: {"saved_at": time.time(), "records": recs}})


def load_pending(max_age_s: float | None = None) -> list[LaunchRecord]:
    """Pending records from state.json ([] if none, malformed, or older than max_age_s)."""
    try:
        raw = config_store.load_state().get(STATE_KEY)
        if isinstance(raw, list):                  # tolerate a bare list
            items, saved_at = raw, None
        elif isinstance(raw, dict):
            items, saved_at = raw.get("records") or [], raw.get("saved_at")
        else:
            return []
        if max_age_s is not None:
            try:
                if saved_at is None or time.time() - float(saved_at) > max_age_s:
                    return []
            except (TypeError, ValueError):
                return []
        return [LaunchRecord.from_dict(d) for d in items if isinstance(d, dict)]
    except Exception:  # noqa: BLE001
        log.exception("reopen: load_pending failed")
        return []


def pending_saved_at() -> float | None:
    """Epoch seconds the pending list was saved, or None."""
    raw = config_store.load_state().get(STATE_KEY)
    try:
        return float(raw["saved_at"]) if isinstance(raw, dict) and raw.get("saved_at") is not None else None
    except (TypeError, ValueError):
        return None


def clear_pending() -> None:
    """Forget the pending list (the key stays in state.json with value None)."""
    config_store.save_state({STATE_KEY: None})


# --------------------------------------------------------------------------------------------- launching

def _running_exes() -> set[str]:
    out: set[str] = set()
    for p in psutil.process_iter(["exe"], ad_value=None):
        exe = p.info.get("exe")
        if exe:
            out.add(_norm_path(exe))
    return out


def already_running(rec: LaunchRecord | dict) -> bool:
    """True if any running process has the same exe path (normcase) - e.g. the user reopened it by hand."""
    try:
        rec = _coerce(rec)
        target = _norm_path(rec.exe)
        return bool(target) and target in _running_exes()
    except Exception:  # noqa: BLE001
        log.exception("reopen: already_running failed for %s", getattr(rec, "name", rec))
        return False


def _same_file(a: str, b: str) -> bool:
    if _norm_path(a) == _norm_path(b):
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _resolves_to_exe(arg0: str, rec: LaunchRecord) -> bool:
    """Does the recorded argv[0] name the same file as rec.exe? Tries it as-is, relative to the recorded
    cwd and to the exe's folder (with and without '.exe'), and on PATH."""
    arg0 = (arg0 or "").strip().strip('"')
    if not arg0:
        return False
    names = [arg0] if os.path.splitext(arg0)[1] else [arg0, arg0 + ".exe"]
    cands: list[str] = []
    for n in names:
        if os.path.isabs(n):
            cands.append(n)
        else:
            for base in (rec.cwd, os.path.dirname(rec.exe)):
                if base:
                    cands.append(os.path.join(base, n))
    w = shutil.which(arg0)
    if w:
        cands.append(w)
    return any(_same_file(c, rec.exe) for c in cands)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in _STRIP_ENV:
        env.pop(k, None)
    return env


_RELAUNCHED: set[tuple[int, float]] = set()
_RELAUNCHED_LOCK = threading.Lock()


def _remember(pid: int) -> None:
    """Note an app we started ourselves (pid + create time), so the next scan can offer it again: as our child
    it would otherwise count as part of this program. Best effort; unknown = not remembered = stays protected."""
    try:
        ident = (int(pid), float(psutil.Process(pid).create_time()))
    except (psutil.Error, OSError, ValueError):
        return
    with _RELAUNCHED_LOCK:
        _RELAUNCHED.add(ident)


def relaunched() -> set[tuple[int, float]]:
    """(pid, create_time) of the apps launch() started in this run and that still exist (dead ones are dropped;
    a reused pid has another create time and does not match)."""
    with _RELAUNCHED_LOCK:
        idents = set(_RELAUNCHED)
    alive = set()
    for pid, ct in idents:
        try:
            if abs(psutil.Process(pid).create_time() - ct) <= 0.01:
                alive.add((pid, ct))
        except (psutil.Error, OSError):
            continue
    with _RELAUNCHED_LOCK:
        _RELAUNCHED.difference_update(idents - alive)
    return alive


def _popen_detached(argv: list[str], executable: str | None, cwd: str | None) -> subprocess.Popen:
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    kw: dict[str, Any] = dict(executable=executable, cwd=cwd, close_fds=True, shell=False,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=_child_env())
    try:
        # leave our job object (if any) so closing the controller does not take the app with it
        return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:                                # job without JOB_OBJECT_LIMIT_BREAKAWAY_OK
        return subprocess.Popen(argv, creationflags=flags, **kw)


def launch(rec: LaunchRecord | dict) -> tuple[bool, str]:
    """Start one record. Returns (ok, message). Never raises, never uses a shell.

    Packaged app with AUMID -> explorer.exe shell:AppsFolder\\<aumid>.
    Otherwise -> CreateProcess(rec.exe, recorded argv) detached, cwd = recorded cwd if it still exists
    else the exe's folder. Refused when skip_reason() applies, the exe is not an existing .exe, or the
    recorded argv[0] does not resolve to rec.exe.
    """
    try:
        rec = _coerce(rec)
        why = skip_reason(rec)
        if why:
            return False, why
        if rec.aumid:
            if not _AUMID_RE.match(rec.aumid):
                return False, "invalid app id"
            explorer = os.path.join(_windir(), "explorer.exe")
            if not os.path.isfile(explorer):
                return False, "explorer.exe not found"
            p = _popen_detached([explorer, "shell:AppsFolder\\" + rec.aumid], explorer, None)
            log.info("reopen: started packaged app %s (%s) via explorer pid %s", rec.name, rec.aumid, p.pid)
            return True, "started"
        exe = os.path.abspath(rec.exe)
        if os.path.splitext(exe)[1].casefold() != ".exe":
            return False, "not an .exe"            # .bat/.cmd would run through cmd.exe
        if not os.path.isfile(exe):
            return False, SKIP_MISSING
        argv = list(rec.cmdline) if rec.cmdline else [exe]
        if not _resolves_to_exe(argv[0], rec):
            return False, "command line does not match the program"
        cwd = rec.cwd if rec.cwd and os.path.isdir(rec.cwd) else os.path.dirname(exe)
        p = _popen_detached(argv, exe, cwd)
        _remember(p.pid)
        log.info("reopen: started %s pid %s", rec.name, p.pid)
        return True, "started"
    except Exception as exc:  # noqa: BLE001
        log.warning("reopen: launch of %s failed: %s", getattr(rec, "name", rec), exc)
        return False, f"could not start: {exc}"


def reopen_one(rec: LaunchRecord | dict) -> dict[str, Any]:
    """One-click path (games / manual): skip if already running, else launch(). Returns a result dict."""
    rec = _coerce(rec)
    if already_running(rec):
        return {"name": rec.name, "ok": True, "reason": "already running", "status": "already_running"}
    ok, msg = launch(rec)
    return {"name": rec.name, "ok": ok, "reason": msg, "status": "started" if ok else "failed"}


def not_started(auto: Iterable[LaunchRecord], results: Iterable[Mapping[str, Any]]) -> list[LaunchRecord]:
    """The auto records an interrupted reopen() never got to: results (one per auto record, same order) with
    status 'cancelled'. The caller keeps them pending, so they are offered again instead of being forgotten."""
    return [rec for rec, res in zip(auto, results) if res.get("status") == "cancelled"]


def reopen(records: Iterable[LaunchRecord | dict], stagger_s: float = 1.0,
           stop_evt: threading.Event | None = None,
           on_progress: Callable[[int, int, str], None] | None = None) -> list[dict[str, Any]]:
    """Relaunch the AUTO records of `records` one by one (input order), blocking; run it on a worker.

    Manual (games) and skipped records are never launched here. Between two launches it waits
    stagger_s (interruptible by stop_evt). Before each record it honours stop_evt: once set, the
    remaining records are reported {ok: False, reason: 'cancelled'} and nothing else is started.
    on_progress(done, total, name) is called after every auto record (exceptions in it are logged).

    Returns one dict per auto record: {name, ok, reason, status}; status is 'started',
    'already_running' (ok=True, not launched), 'failed' or 'cancelled'.
    """
    auto, _manual, _skipped = classify(records)
    total = len(auto)
    results: list[dict[str, Any]] = []
    launched = False

    def progress(name: str) -> None:
        if on_progress is not None:
            try:
                on_progress(len(results), total, name)
            except Exception:  # noqa: BLE001
                log.exception("reopen: on_progress failed")

    for i, rec in enumerate(auto):
        cancelled = stop_evt is not None and stop_evt.is_set()
        if not cancelled and launched and stagger_s > 0:
            if stop_evt is not None:
                cancelled = stop_evt.wait(stagger_s)
            else:
                time.sleep(stagger_s)
        if cancelled:
            for r in auto[i:]:
                results.append({"name": r.name, "ok": False, "reason": "cancelled", "status": "cancelled"})
            log.info("reopen: cancelled with %d app(s) not started", total - i)
            break
        if already_running(rec):
            res = {"name": rec.name, "ok": True, "reason": "already running", "status": "already_running"}
        else:
            ok, msg = launch(rec)
            launched = True
            res = {"name": rec.name, "ok": ok, "reason": msg, "status": "started" if ok else "failed"}
        results.append(res)
        progress(rec.name)
    return results
