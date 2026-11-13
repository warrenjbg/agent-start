"""Headless tests for idle_watch.py (idle auto-off: when is the engine idle long enough?) and for the app's glue
around it (main.AgentStartApp._idle_check / _idle_off / the stop answer), called on a stand-in object: no window,
no Tk root, no process, no network, no engine. The data folder is a temp folder.

Run:  .venv\\Scripts\\python.exe tests\\test_idle_watch.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

os.environ["AGENT_START_HOME"] = tempfile.mkdtemp(prefix="agentstart-idle-test-")   # before any app module
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import idle_watch as iw  # noqa: E402
from idle_watch import IdleWatch  # noqa: E402

PASSED = FAILED = 0


def check(cond: bool, name: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


def run(w: IdleWatch, t0: float, seconds: int, minutes: int = 15, **kw) -> tuple[float, bool]:
    """Poll once a second for `seconds`; returns (the time after the last poll, whether any poll said 'due')."""
    due = False
    t = t0
    for _ in range(seconds):
        t += 1.0
        due = w.observe(t, minutes=minutes, known=True, busy=False, **kw) or due
    return t, due


def t_labels() -> None:
    check(iw.CHOICES_MIN == (0, 15, 30, 60, 120), "choices: never, 15 min, 30 min, 1 hour, 2 hours")
    check(iw.labels() == ["Keep running", "Off after 15 min", "Off after 30 min", "Off after 1 hour",
                          "Off after 2 hours"], "Settings entries")
    check([iw.span(m) for m in (15, 30, 60, 120, 90)] == ["15 minutes", "30 minutes", "1 hour", "2 hours", "90 minutes"],
          "toast wording")
    for m in iw.CHOICES_MIN:
        check(iw.parse(m) == m and iw.parse(iw.label(m)) == m, f"parse round trip: {m}")
    for junk in (None, True, False, 1, 14, 16, -15, 15.0, "15", "off after 15 min", "", [15], {"m": 15}, 10 ** 9):
        check(iw.parse(junk) == 0, f"parse {junk!r}: never (a damaged state file must not turn it on)")


def t_idle_time() -> None:
    w = IdleWatch()
    check(w.observe(0.0, minutes=15, known=True, busy=False) is False and w.idle_s == 0.0,
          "the first poll counts as activity: a start begins at zero")
    t, due = run(w, 0.0, 899)
    check(not due and abs(w.idle_s - 899.0) < 1e-6, "899 idle seconds of 15 minutes: not yet")
    check(w.observe(t + 1, minutes=15, known=True, busy=False) is True, "900 idle seconds: due")
    check(w.observe(t + 2, minutes=15, known=True, busy=False) is True, "...and it stays due (the app may be waiting)")
    check(w.observe(t + 3, minutes=30, known=True, busy=False) is False, "the setting is read at every poll: 30 min not yet")
    check(w.observe(t + 4, minutes=0, known=True, busy=False) is False, "Never: never due")
    for junk in (None, True, "15", 14, -15, 15.5):
        check(w.observe(t + 5, minutes=junk, known=True, busy=False) is False, f"minutes {junk!r}: never due")
    w.reset()
    check(w.idle_s == 0.0 and w.observe(t + 6, minutes=15, known=True, busy=False) is False, "reset(): from zero")


def t_activity() -> None:
    for why, kw in (("a running request", {"busy": True}),
                    ("a poll that does not know (no answer / model not loaded)", {"known": False})):
        w = IdleWatch()
        w.observe(0.0, minutes=15, known=True, busy=False)
        t, _ = run(w, 0.0, 800)
        args = {"minutes": 15, "known": True, "busy": False}
        args.update(kw)
        check(w.observe(t + 1, **args) is False and w.idle_s == 0.0, f"{why}: back to zero")
        t, due = run(w, t + 1, 899)
        check(not due, f"{why}: the full time must pass again")
    w = IdleWatch()
    w.observe(0.0, minutes=15, known=True, busy=False, request_ids=[("a", 1.0), ("b", 2.0)])
    t, due = run(w, 0.0, 800, request_ids=[("a", 1.0), ("b", 2.0)])
    check(not due and w.idle_s == 800.0, "the same finished requests listed again and again are not activity")
    check(w.observe(t + 1, minutes=15, known=True, busy=False, request_ids=[("b", 2.0), ("c", 3.0)]) is False
          and w.idle_s == 0.0, "a finished request that was not there before (a reply shorter than a poll): zero")
    t, due = run(w, t + 1, 100, request_ids=[("c", 3.0)])
    check(w.observe(t + 1, minutes=15, known=True, busy=False, request_ids=[("a", 1.0), ("c", 3.0)]) is False
          and w.idle_s == 101.0, "an old request listed again (a catch-up poll) is not new")
    check(w.observe(t + 2, minutes=15, known=True, busy=False, request_ids=[("a", 9.0)]) is False and w.idle_s == 0.0,
          "the same key at another time is another request (a later server run)")
    unhashable: list = [["unhashable"]]
    check(w.observe(t + 3, minutes=15, known=True, busy=False, request_ids=unhashable) is False
          and w.idle_s == 0.0, "an id that cannot be remembered counts as activity")
    w = IdleWatch()
    w.observe(0.0, minutes=15, known=True, busy=False)
    run(w, 0.0, 10)
    w.observe(11.0, minutes=15, known=False, busy=False, request_ids=[("z", 1.0)])
    check(w.observe(12.0, minutes=15, known=True, busy=False, request_ids=[("z", 1.0)]) is False and w.idle_s == 0.0
          and w.observe(13.0, minutes=15, known=True, busy=False, request_ids=[("z", 1.0)]) is False
          and w.idle_s == 1.0, "requests seen by a poll that did not know are remembered, and idle time restarts")


def t_gaps() -> None:
    w = IdleWatch()
    w.observe(0.0, minutes=15, known=True, busy=False)
    t, _ = run(w, 0.0, 600)
    check(w.observe(t + 7200.0, minutes=15, known=True, busy=False) is False and w.idle_s == 600.0,
          "a 2-hour gap between polls (the PC slept) adds nothing: waking up does not turn the engine off")
    t2, due = run(w, t + 7200.0, 299)
    check(not due, "after the gap the remaining time still has to pass")
    check(w.observe(t2 + 1, minutes=15, known=True, busy=False) is True, "...and then it is due")
    w = IdleWatch()
    w.observe(0.0, minutes=15, known=True, busy=False)
    check(w.observe(5.0, minutes=15, known=True, busy=False) is False and w.idle_s == 5.0, "exactly 5 s counts")
    check(w.observe(10.01, minutes=15, known=True, busy=False) is False and w.idle_s == 5.0, "just over 5 s does not")
    check(w.observe(9.0, minutes=15, known=True, busy=False) is False and w.idle_s == 5.0, "time going backwards adds nothing")
    for junk in (None, True, "5", float("nan"), float("inf")):
        w.observe(10.0, minutes=15, known=True, busy=False)
        w.observe(11.0, minutes=15, known=True, busy=False)
        check(w.observe(junk, minutes=15, known=True, busy=False) is False and w.idle_s == 0.0,
              f"a junk time ({junk!r}): zero")


def t_bounded() -> None:
    w = IdleWatch()
    w.observe(0.0, minutes=15, known=True, busy=False, request_ids=[(str(i), 0.0) for i in range(iw.SEEN_MAX + 500)])
    check(len(w._seen) == iw.SEEN_MAX, "the remembered requests are bounded")
    newest = [(str(i), 0.0) for i in range(iw.SEEN_MAX + 470, iw.SEEN_MAX + 500)]
    w.observe(1.0, minutes=15, known=True, busy=False, request_ids=newest)
    check(w.idle_s == 1.0 and w.observe(2.0, minutes=15, known=True, busy=False, request_ids=newest) is False
          and w.idle_s == 2.0, "the newest ones are the ones kept (listed again: not new)")
    w.observe(3.0, minutes=15, known=True, busy=False, request_ids=[("0", 0.0)])
    check(w.idle_s == 0.0, "one that was pushed out counts as new again (on the side of keeping the engine on)")
    w.reset()
    check(len(w._seen) == 0, "reset() forgets them (another server run)")


def _app(**over):
    """A stand-in for the app with only what the idle glue reads. turn_off / _set_state are recorders."""
    import main as M
    from engines.base import CAP_LIVE_ACTIVITY, CAP_REQUEST_HISTORY
    a = types.SimpleNamespace(
        _destroyed=False, _exit_pending=False, state_name=M.ACTIVE, loaded=True, _stop_pending=None,
        _switching=False, _switch_to=None, _metrics_fails=0, _skip_reopen_once=False, _last_stop_game="old",
        _idle_stop=False, _stop_toast=None, _idle=IdleWatch(), _idle_epoch=-1,
        watch=types.SimpleNamespace(idle_minutes=15),
        engine=types.SimpleNamespace(name="Strata", can=lambda c: c in (CAP_LIVE_ACTIVITY, CAP_REQUEST_HISTORY)),
        calls=[], busy=None, blocked=False)
    a._busy_activity = lambda: a.busy
    a._game_blocked = lambda: a.blocked
    a.turn_off = lambda force=False: a.calls.append(("turn_off", force))
    a._set_state = lambda st: (a.calls.append(("state", st)), setattr(a, "state_name", st))
    a._apply_state = lambda: a.calls.append(("apply",))
    a._ask = lambda *x, **k: a.calls.append(("ask",)) or False
    a._idle_supported = lambda: M.AgentStartApp._idle_supported(a)
    a._idle_off = lambda minutes: M.AgentStartApp._idle_off(a, minutes)
    for k, v in over.items():
        setattr(a, k, v)
    return a


def t_glue_idle_off() -> None:
    import main as M
    real_pending = M.reopen.load_pending
    try:
        M.reopen.load_pending = lambda *x, **k: []                 # type: ignore[assignment]
        a = _app()
        check(M.AgentStartApp._idle_off(a, 30) is True and a.calls == [("turn_off", False)],
              "idle time up, nothing in the way: the normal Turn off, never forced")
        check(a._skip_reopen_once is True and a._last_stop_game is None and a._idle_stop is True,
              "closed apps become a Reopen offer (not started unattended); no app to turn back on for")
        check(a._stop_toast == ("Strata turned off", "No requests for 30 minutes · freed {freed}."),
              f"the toast text rides with the stop ({a._stop_toast})")
        M.reopen.load_pending = lambda *x, **k: [{"x": 1}]         # type: ignore[assignment]
        a = _app()
        M.AgentStartApp._idle_off(a, 60)
        check(a._stop_toast is not None and a._stop_toast[1] == "No requests for 1 hour · freed {freed}. The apps "
              "closed for Strata are in Agent Start (Reopen).", f"with closed apps the toast says where they are ({a._stop_toast})")
        for why, over in (("a dialog / message box / update is open", {"blocked": True}),
                          ("a request is running or an agent is between tool calls", {"busy": {"kind": "gen"}}),
                          ("the user's own 'when it's done' stop is pending", {"_stop_pending": ("off", None)}),
                          ("a model switch is pending", {"_stop_pending": ("switch", object())}),
                          ("a model switch is under way", {"_switching": True}),
                          ("an engine switch is under way", {"_switch_to": "llama_cpp"}),
                          ("the engine is not answering", {"_metrics_fails": 1}),
                          ("the model is not loaded", {"loaded": False}),
                          ("the app is quitting", {"_exit_pending": True}),
                          ("the app is closing", {"_destroyed": True}),
                          ("it is starting", {"state_name": M.STARTING}),
                          ("it is already stopping", {"state_name": M.STOPPING}),
                          ("it is off", {"state_name": M.IDLE})):
            a = _app(**over)
            got = M.AgentStartApp._idle_off(a, 15)
            check(got is False and a.calls == [] and a._idle_stop is False and a._skip_reopen_once is False
                  and a._stop_toast is None and a._last_stop_game == "old", f"{why}: nothing happens, nothing changes")
    finally:
        M.reopen.load_pending = real_pending                       # type: ignore[assignment]


def t_glue_idle_check() -> None:
    import main as M
    from engines.base import LiveSample, MetricsSample, RequestSample
    real_mono, real_pending = M.time.monotonic, M.reopen.load_pending
    clock = [1000.0]
    idle = MetricsSample(model="m", live=LiveSample(state="idle"))

    def polls(a, n, sample=idle, epoch=1):
        for _ in range(n):
            clock[0] += 1.0
            M.AgentStartApp._idle_check(a, epoch, sample)

    try:
        M.time.monotonic = lambda: clock[0]                        # type: ignore[assignment]
        M.reopen.load_pending = lambda *x, **k: []                 # type: ignore[assignment]
        a = _app()
        polls(a, 900)
        check(a.calls == [], "15 min setting: 899 idle seconds after the first poll, still serving")
        polls(a, 1)
        check(a.calls == [("turn_off", False)], "at 15 minutes: one Turn off")
        check(a._idle.idle_s == 0.0, "the idle time starts over once the stop began")
        a = _app()
        a.watch.idle_minutes = 0
        polls(a, 8000)
        check(a.calls == [], "Never (the default): hours of idling, nothing happens")
        a.watch.idle_minutes = 120
        polls(a, 1)
        check(a.calls == [("turn_off", False)], "the setting applies to the running engine from the next poll")
        for why, sample in (("a poll with no answer", None),
                            ("a reply being written", MetricsSample(model="m", live=LiveSample(state="generating"))),
                            ("a prompt being read", MetricsSample(model="m", live=LiveSample(state="reading"))),
                            ("a request waiting in the queue", MetricsSample(model="m", live=LiveSample(state="idle", queued=1))),
                            ("an engine that does not say what the model does", MetricsSample(model="m")),
                            ("a state the app does not know", MetricsSample(model="m", live=LiveSample(state=None))),
                            ("a finished request that is new", MetricsSample(
                                model="m", live=LiveSample(state="idle"), requests=(RequestSample(key="r1", time=5.0),)))):
            a = _app()
            polls(a, 890)
            polls(a, 1, sample)
            polls(a, 898)
            check(a.calls == [], f"{why} at 14:50 puts the idle time back to zero")
        a = _app()
        polls(a, 890)
        a.busy = {"kind": "tool"}
        polls(a, 1)
        a.busy = None
        polls(a, 898)
        check(a.calls == [], "an agent between tool calls counts as activity")
        a = _app()
        polls(a, 890)
        a.loaded = False
        polls(a, 1)
        a.loaded = True
        polls(a, 898)
        check(a.calls == [], "the model not loaded: not counted as idle")
        a = _app()
        polls(a, 890)
        polls(a, 20, epoch=2)
        check(a.calls == [], "another server run starts at zero")
        a = _app(blocked=True)
        polls(a, 1200)
        check(a.calls == [], "time up while a dialog is open: it waits")
        a.blocked = False
        polls(a, 1)
        check(a.calls == [("turn_off", False)], "the dialog closed and the model is still idle: now it turns off")
        a = _app()
        polls(a, 600)
        clock[0] += 3 * 3600.0                                       # the PC slept
        polls(a, 290)
        check(a.calls == [], "the PC slept for three hours: waking up does not turn the engine off")
        polls(a, 20)
        check(a.calls == [("turn_off", False)], "...the rest of the 15 minutes still has to pass awake")
        a = _app()
        a.engine.can = lambda c: False
        polls(a, 2000)
        check(a.calls == [], "an engine that reports no activity is never turned off by the app")
    finally:
        M.time.monotonic, M.reopen.load_pending = real_mono, real_pending   # type: ignore[assignment]


def t_glue_busy_answer() -> None:
    """The engine answered the idle stop with 'busy' (a request arrived in that second): keep serving, no box."""
    import main as M
    a = _app(state_name=M.STOPPING, _idle_stop=True, _skip_reopen_once=True, _last_stop_game=None)
    a._idle.observe(1.0, minutes=15, known=True, busy=False)
    a._idle.observe(2.0, minutes=15, known=True, busy=False)
    M.AgentStartApp._on_stopped(a, {"ok": False, "unload": "busy"})
    check(a.calls == [("state", M.ACTIVE), ("apply",)] and a._idle_stop is False and a._skip_reopen_once is False
          and a._idle.idle_s == 0.0, f"busy answer to an idle stop: serving again, no question, idle time from zero ({a.calls})")
    a = _app(state_name=M.STOPPING, _idle_stop=False, _skip_reopen_once=False, _last_stop_game=None)
    M.AgentStartApp._on_stopped(a, {"ok": False, "unload": "busy"})
    check(("ask",) in a.calls, "the user's own Turn off still asks 'force stop?' as before")


def main() -> int:
    for t in (t_labels, t_idle_time, t_activity, t_gaps, t_bounded, t_glue_idle_off, t_glue_idle_check,
              t_glue_busy_answer):
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"{t.__name__} raised {exc!r}")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
