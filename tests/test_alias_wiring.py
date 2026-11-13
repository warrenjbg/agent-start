"""Headless tests for the app's glue around hermes_alias.py (Settings > Keep Hermes' "llama" alias in step):
main.AgentStartApp._alias_check / _alias_result / set_alias_pref / _alias_target / _hermes_path, the worker and the
note text, called on a stand-in object. No window, no Tk root, no network, no engine, and NO process: subprocess.run
and subprocess.Popen are replaced by a guard before any app module is imported, the worker launcher is a recorder,
and hermes_alias.sync_alias is only ever a recorder here. The data folder is a temp folder.

Run:  .venv\\Scripts\\python.exe tests\\test_alias_wiring.py
"""
from __future__ import annotations

import functools
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

os.environ["AGENT_START_HOME"] = tempfile.mkdtemp(prefix="agentstart-alias-test-")   # before any app module
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GUARD_HITS: list[str] = []


def _forbidden(*args, **kwargs):
    GUARD_HITS.append(repr(args[:1])[:80])
    raise AssertionError("test_alias_wiring must never start a process")


subprocess.run = _forbidden  # type: ignore[assignment]
subprocess.Popen = _forbidden  # type: ignore[assignment, misc]

import main as M  # noqa: E402

PASSED = FAILED = 0
EXE = r"C:\Apps\hermes\bin\hermes.exe"
URL = "http://127.0.0.1:9931/v1"
METHODS = ("_alias_supported", "_hermes_path", "_alias_target", "_alias_check", "_alias_result", "set_alias_pref")


def check(cond: bool, name: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


def _app(**over):
    """A stand-in for the app with only what the alias glue reads; the worker launcher and the note are recorders."""
    a = types.SimpleNamespace(
        _alias_pref=True, _can_alias=True, _alias_done=None, _alias_busy=False, _destroyed=False, _hermes_exe=EXE,
        state_name=M.ACTIVE, loaded=True, model_id="Qwen-A", cfg={"port": 9931},
        engine=types.SimpleNamespace(name="llama.cpp", syncs_hermes_alias=True),
        workers=[], notes=[], saved=[], posted=[])
    a._run_worker = lambda name, fn, *args: a.workers.append((name, fn, args))
    a._set_note = a.notes.append
    a._save_state_later = lambda k, v: a.saved.append((k, v))
    a.post = lambda fn, *args: a.posted.append((fn, args))
    for name in METHODS:
        setattr(a, name, functools.partial(getattr(M.AgentStartApp, name), a))
    for k, v in over.items():
        setattr(a, k, v)
    return a


def _res(ok: bool, changed: bool, note: str = ""):
    return M.hermes_alias.AliasResult(ok, changed, note)


def t_starts_one_update() -> None:
    a = _app()
    a._alias_check()
    check(len(a.workers) == 1 and a.workers[0][0] == "hc-alias" and a.workers[0][1] is M._alias_worker
          and a.workers[0][2][:3] == (EXE, "Qwen-A", URL) and a.workers[0][2][3] is a.post
          and a.workers[0][2][4] is a._alias_result,
          f"serving a loaded model with the switch on: one worker with hermes.exe, the model and the address ({a.workers})")
    check(a._alias_busy is True and a._alias_done == ("Qwen-A", URL), "marked busy and handled")
    for _ in range(5):
        a._alias_check()
    check(len(a.workers) == 1, "later polls while hermes runs: no second run")
    a._alias_result(("Qwen-A", URL), _res(True, True, "Hermes alias llama now points at Qwen-A"))
    check(a._alias_busy is False and a.notes == ["Hermes alias llama updated\nQwen-A"],
          f"the write was confirmed: one note ({a.notes})")
    for _ in range(5):
        a._alias_check()
    check(len(a.workers) == 1, "the same model on the same address is handled once per app run")
    a.model_id = "Qwen-B"
    a._alias_check()
    check(len(a.workers) == 2 and a.workers[1][2][:3] == (EXE, "Qwen-B", URL), "another model: another update")
    a._alias_result(("Qwen-B", URL), _res(True, False))
    check(len(a.notes) == 1, "already in step (nothing written): no note")
    a.cfg = {"port": 8080}
    a._alias_check()
    check(len(a.workers) == 3 and a.workers[2][2][:3] == (EXE, "Qwen-B", "http://127.0.0.1:8080/v1"),
          "the same model on another port: another update")


def t_does_nothing() -> None:
    """Every condition that must keep hermes from running: no worker, nothing marked, nothing noted."""
    cases: list[tuple[str, dict]] = [
        ("the switch is off", {"_alias_pref": False}),
        ("a stub-built app (the test gate)", {"_can_alias": False}),
        ("an engine Hermes has no alias for", {"engine": types.SimpleNamespace(name="Strata")}),
        ("the engine says no", {"engine": types.SimpleNamespace(name="x", syncs_hermes_alias=False)}),
        ("the app is closing", {"_destroyed": True}),
        ("a run is already in flight", {"_alias_busy": True}),
        ("the model is still loading", {"loaded": False}),
        ("whether a model is loaded is unknown", {"loaded": None}),
        ("no model name yet", {"model_id": None}),
        ("an empty model name", {"model_id": ""}),
        ("no port", {"cfg": {}}),
        ("port 0", {"cfg": {"port": 0}}),
        ("port 70000", {"cfg": {"port": 70000}}),
        ("a port given as text", {"cfg": {"port": "9931"}}),
        ("a port given as True", {"cfg": {"port": True}}),
    ]
    for st in (M.IDLE, M.STARTING, M.STOPPING, M.ERROR):
        cases.append((f"state {st}", {"state_name": st}))
    for why, over in cases:
        a = _app(**over)
        before = a._alias_busy
        for _ in range(3):
            a._alias_check()
        check(not a.workers and a._alias_done is None and not a.notes and a._alias_busy is before, f"{why}: nothing runs")


def t_hermes_missing_and_lookup() -> None:
    a = _app(_hermes_exe=None)
    for _ in range(4):
        a._alias_check()
    check(not a.workers and a._alias_busy is False and a._alias_done == ("Qwen-A", URL) and not a.notes,
          "hermes.exe not found: nothing runs, and it is not looked at again on every poll")
    looked: list[int] = []
    saved = M.hermes_alias.find_hermes
    M.hermes_alias.find_hermes = lambda *x, **k: looked.append(1) or EXE          # type: ignore[assignment]
    try:
        a = _app(_hermes_exe=False)
        got = [a._hermes_path(), a._hermes_path(), a._hermes_path()]
        check(got == [EXE] * 3 and len(looked) == 1, f"hermes.exe is looked for once per app run ({len(looked)})")
        M.hermes_alias.find_hermes = lambda *x, **k: looked.append(1) or None     # type: ignore[assignment]
        a = _app(_hermes_exe=False)
        got = [a._hermes_path(), a._hermes_path()]
        check(got == [None, None] and len(looked) == 2, "...also when it was not found")
    finally:
        M.hermes_alias.find_hermes = saved                                        # type: ignore[assignment]


def t_failure_and_retry() -> None:
    a = _app()
    a._alias_check()
    a._alias_result(("Qwen-A", URL), _res(False, False, "Hermes alias not updated: hermes exited with code 1 (locked)"))
    check(a.notes == ["Hermes alias not updated\nhermes exited with code 1 (locked)"] and a._alias_busy is False,
          f"a failure shows its reason as a note ({a.notes})")
    for _ in range(5):
        a._alias_check()
    check(len(a.workers) == 1, "a failure is not retried on every poll")
    a.set_alias_pref(False)
    check(a.saved == [("hermes_alias", False)] and a._alias_pref is False and len(a.workers) == 1,
          "switch off: saved, nothing runs")
    a.set_alias_pref(True)
    check(a.saved[-1] == ("hermes_alias", True) and a._alias_pref is True and len(a.workers) == 2
          and a.workers[1][2][:3] == (EXE, "Qwen-A", URL), "switch on again: saved, and the update is tried again now")
    a = _app(state_name=M.IDLE, _alias_pref=False)
    a.set_alias_pref(True)
    check(a.saved == [("hermes_alias", True)] and not a.workers, "switch on while the engine is off: saved, nothing runs yet")
    a.state_name = M.ACTIVE
    a._alias_check()
    check(len(a.workers) == 1, "...and the update runs once the engine serves")

    a = _app()
    a._alias_check()
    a.model_id = "Qwen-B"                         # the model changed while hermes ran
    a._alias_result(("Qwen-A", URL), _res(True, True, "x"))
    check(len(a.workers) == 2 and a.workers[1][2][:3] == (EXE, "Qwen-B", URL) and a._alias_busy is True,
          "a model change during a run is picked up when the run ends")

    a = _app()
    a._alias_check()
    a._destroyed = True
    a._alias_result(("Qwen-A", URL), _res(False, False, "Hermes alias not updated: x"))
    check(not a.notes and len(a.workers) == 1, "a result after the app closed: no note, nothing more runs")

    a = _app()

    def boom(*x, **k):
        raise RuntimeError("no threads")

    a._run_worker = boom
    try:
        a._alias_check()
        check(a._alias_busy is False and not a.notes, "an error while starting the worker ends in the check (not busy forever)")
    except Exception as exc:  # noqa: BLE001
        check(False, f"_alias_check raised {exc!r}")


def t_worker_and_note() -> None:
    calls: list = []
    posted: list = []
    saved = M.hermes_alias.sync_alias
    try:
        M.hermes_alias.sync_alias = lambda model, url, **kw: calls.append((model, url, kw)) or _res(True, True, "ok")  # type: ignore[assignment]
        M._alias_worker(EXE, "Qwen-A", URL, lambda fn, *args: posted.append((fn, args)), "DONE")
        check(calls == [("Qwen-A", URL, {"exe": EXE})], f"the worker calls sync_alias with the found hermes.exe ({calls})")
        check(posted == [("DONE", (("Qwen-A", URL), _res(True, True, "ok")))], "and posts the target and the result back")

        def boom(*x, **k):
            raise RuntimeError("x")

        M.hermes_alias.sync_alias = boom           # type: ignore[assignment]
        posted.clear()
        M._alias_worker(EXE, "Qwen-A", URL, lambda fn, *args: posted.append((fn, args)), "DONE")
        check(len(posted) == 1 and posted[0][1][1].ok is False and posted[0][1][1].changed is False,
              "an error in the worker is posted as 'not updated'")
    finally:
        M.hermes_alias.sync_alias = saved          # type: ignore[assignment]
    n = M._alias_note
    check(n(_res(False, False, "Hermes alias not updated: hermes.exe was not found"))
          == "Hermes alias not updated\nhermes.exe was not found", "note: head and reason on two lines")
    check(n(_res(False, False, "Hermes alias llama may be wrong: Hermes does not show the values that were sent; "
                               "check it with: hermes config get model_aliases.llama"))
          .split("\n")[0] == "Hermes alias llama may be wrong", "note: the 'may be wrong' sentence keeps its head")
    long = n(_res(False, False, "Hermes alias not updated: " + "x" * 300))
    check(len(long.split("\n")) == 2 and len(long.split("\n")[1]) <= 40, "note: a long reason is shortened")
    check(n(_res(False, False, "")) == "Hermes alias not updated" and n(None) == "Hermes alias not updated",
          "note: no sentence at all still says what happened")


def t_status_path() -> None:
    """on_status (every status poll) is where the check hangs; it must not run under a dialog or while stopping."""
    def app(**over):
        a = _app(model_id=None, _exit_pending=False, _error_hold_until=0.0, _idle_streak=0, _metrics_fails=0,
                 states=[], **over)
        a._dialog_open = lambda: False
        a._set_state = lambda st: (a.states.append(st), setattr(a, "state_name", st))
        return a

    st = {"state": "active", "loaded": True, "model_id": "Qwen-A"}
    a = app()
    M.AgentStartApp.on_status(a, st)
    check(len(a.workers) == 1 and a.workers[0][2][:3] == (EXE, "Qwen-A", URL),
          "the status that brings the model name starts the update")
    M.AgentStartApp.on_status(a, st)
    check(len(a.workers) == 1, "the next polls do not repeat it")
    a = app()
    M.AgentStartApp.on_status(a, {"state": "active", "loaded": False, "model_id": "Qwen-A"})
    check(not a.workers, "a server that is up but still loading: not yet")
    a = app()
    a._dialog_open = lambda: True
    M.AgentStartApp.on_status(a, st)
    check(not a.workers, "while a dialog is open the status is not applied, so nothing runs")
    a = app(state_name=M.STOPPING)
    M.AgentStartApp.on_status(a, st)
    check(not a.workers, "while stopping: nothing runs")
    a2 = app(_run_worker=lambda *x, **k: (_ for _ in ()).throw(RuntimeError("x")))
    try:
        M.AgentStartApp.on_status(a2, st)
        check(a2.state_name == M.ACTIVE and a2.model_id == "Qwen-A", "an error inside the check does not break the status path")
    except Exception as exc:  # noqa: BLE001
        check(False, f"on_status raised {exc!r}")


def t_defaults() -> None:
    check(M.ALIAS_LABEL == "Keep Hermes' \u201cllama\u201d alias in step"
          and M.ALIAS_LABEL_MISSING == "Hermes \u201cllama\u201d alias (Hermes not found)", "the Settings row's two labels")
    check(max(len(M.ALIAS_LABEL), len(M.ALIAS_LABEL_MISSING)) <= 40,
          "both fit beside the switch (a longer one was cut off by it in the window)")
    import engines as E
    import engines.base as EB
    check(EB.Engine.syncs_hermes_alias is False, "engines do not offer it unless they say so")
    flags = {name: getattr(E.get_engine({"backend": name}), "syncs_hermes_alias", None) for name in E.available()}
    check(flags == {"strata": False, "llama_cpp": True}, f"of the engines, only llama.cpp offers it ({flags})")


def main() -> int:
    for t in (t_starts_one_update, t_does_nothing, t_hermes_missing_and_lookup, t_failure_and_retry,
              t_worker_and_note, t_status_path, t_defaults):
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"{t.__name__} raised {exc!r}")
    check(not GUARD_HITS, f"no process was ever started ({len(GUARD_HITS)} attempts blocked)")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
