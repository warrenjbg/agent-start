"""Regression checks for the fixes that followed the v1.12 code review. Window test (not CI).

Fakes only: stub detector / endpoint, no engine process, no port, config.json writes captured, the relaunch replaced by
a recorder, reopen's launches replaced by recorders.
"""
from __future__ import annotations

import gc
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_ui_smoke as U  # noqa: E402  (first: it redirects the data dir)

import config_store  # noqa: E402
import main  # noqa: E402
import reopen  # noqa: E402
import sweep_dialog  # noqa: E402
from test_engine_switch import Env  # noqa: E402

PASSES = 0
FAILS: list[str] = []


def check(cond: object, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


def _app():
    app, det, ep = U.new_app()
    exits: list = []
    app._finish_exit = lambda: exits.append(1)
    app._can_relaunch = True
    return app, det, ep, exits


def _arm_recorder(app) -> list[bool]:
    calls: list[bool] = []
    app.watch.arm_memory = lambda want: calls.append(bool(want))
    return calls


# ------------------------------------------------------------------------------------------------ memory watch
def test_memory_watch_arming() -> None:
    app, det, ep = U.new_app()
    try:
        U.to_active(app, ep)
        U.pump(app, 0.2)
        check(app.state_name == main.ACTIVE and app.loaded is True, "serving, model loaded")
        calls = _arm_recorder(app)
        # the health check did not answer: loaded comes back as None
        ep.status_value = dict(U.active_status(), loaded=None)
        app.on_status(ep.status())
        U.pump(app, 0.1)
        check(app.loaded is True and app.state_name == main.ACTIVE,
              f"an unanswered health check does not change what is known (loaded={app.loaded})")
        check(False not in calls, f"...and never disarms the memory watch ({calls})")
        # a real change still gets through
        ep.status_value = U.active_status(loaded=False)
        app.on_status(ep.status())
        U.pump(app, 0.1)
        check(app.loaded is False and calls and calls[-1] is False, f"loaded False is a change: disarmed ({calls})")
        # unknown while NOT known (nothing to keep): stays unknown
        app.loaded = None
        app.on_status(dict(U.active_status(), loaded=None))
        check(app.loaded is None, "unknown stays unknown when nothing was known")

        # the tick arms only while serving, whatever a left-over `loaded` says
        calls.clear()
        app.loaded = True
        app.state_name = main.STOPPING
        app._tick()
        check(calls and calls[-1] is False, f"tick while STOPPING with loaded=True: not armed ({calls})")
        app.state_name = main.IDLE
        calls.clear()
        app._tick()
        check(calls and calls[-1] is False, f"tick while IDLE with a stale loaded=True: not armed ({calls})")
        app.state_name = main.ACTIVE
        calls.clear()
        app._tick()
        check(calls and calls[-1] is True, f"tick while serving and loaded: armed ({calls})")

        # start/stop returned False: nothing is known about the model any more
        app.loaded = True
        ep.status_value = dict(U.IDLE_STATUS)
        app._reconcile_now()
        check(app.loaded is None and app.state_name == main.IDLE, "reconcile: loaded is unknown again")
        U.pump(app, 0.4)
    finally:
        app.destroy()


# ------------------------------------------------------------------------------------------------ tick + gc
def test_tick_collects_after_an_error() -> None:
    app, det, ep = U.new_app()
    orig = main.gc.collect
    n = [0]
    try:
        def boom() -> None:
            raise RuntimeError("telemetry broke")
        app._update_telemetry = boom
        main.gc.collect = lambda *a, **k: (n.__setitem__(0, n[0] + 1), 0)[1]
        before = app._tick_id
        try:
            app._tick()
            raised = False
        except RuntimeError:
            raised = True
        check(raised and n[0] == 1, f"a tick step that raises: garbage is still collected ({raised}, {n[0]})")
        check(app._tick_id != before, "...and the next tick is still scheduled")
    finally:
        main.gc.collect = orig
        app.destroy()
        gc.collect()


# ------------------------------------------------------------------------------------------------ ready
def test_ready_status_off_the_tk_thread() -> None:
    app, det, ep = U.new_app()
    try:
        threads: list[str] = []
        orig_status = ep.status

        def status() -> dict:
            threads.append(threading.current_thread().name)
            return orig_status()
        ep.status = status
        det.cands = []                                   # nothing to close: Turn on goes straight to the server
        app.on_toggle()
        check(U.pump_until(app, lambda: len(ep.start_calls) == 1, 4.0), "the start was requested")
        threads.clear()
        ep.status_value = dict(U.active_status(), adopted=True)
        app._gpu_fit_pre = True
        U.fire(ep.start_calls[-1][0])                    # on_ready, from a worker as the manager does
        check(U.pump_until(app, lambda: app.state_name == main.ACTIVE and app.model_id == "swift-test-1", 4.0),
              f"ready -> serving, model id from the status ({app.state_name}, {app.model_id})")
        check(threads and "MainThread" not in threads, f"status() ran on workers only ({threads})")
        check(app._gpu_fit_pre is False, "an adopted server: the GPU pre-check's answer is dropped")
        app._gpu_fit_pre = True
        app._ready_status(U.active_status())
        check(app._gpu_fit_pre is True, "a server we started: the answer is kept")
    finally:
        app.destroy()


# ------------------------------------------------------------------------------------------------ engine switch
def test_switch_waits_for_pending_stop() -> None:
    with Env() as env:
        app, det, ep, exits = _app()
        try:
            U.to_active(app, ep)
            U.pump(app, 0.3)
            app._stop_pending, app._idle_since = ("off", None), None      # "Turns off after this reply"
            app._apply_state()
            stops = len(ep.stop_calls)
            note = app.set_backend("llama_cpp")
            U.pump(app, 0.6)
            check(isinstance(note, str) and len(ep.stop_calls) == stops and app.state_name == main.ACTIVE
                  and app._stop_pending == ("off", None) and app._switch_to == "llama_cpp",
                  f"the switch waits with the pending turn off: no stop yet, nothing forced "
                  f"({len(ep.stop_calls) - stops} stops, pending {app._stop_pending})")
            app._switch_at -= 10.0
            app._engine_switch_check()
            check(app._switch_to == "llama_cpp" and not env.saved, "still waiting after the grace: not dropped")
            app._run_pending(force=False)                # the reply finished
            check(U.pump_until(app, lambda: len(ep.stop_calls) == stops + 1, 3.0) and ep.stop_calls[-1][1] is False,
                  "the reply finished: the normal (not forced) Turn off")
            ep.status_value = dict(U.IDLE_STATUS)
            U.fire(ep.stop_calls[-1][0], U.stop_result())
            check(U.pump_until(app, lambda: bool(exits), 8.0) and len(env.saved) == 1
                  and env.saved[0]["backend"] == "llama_cpp" and env.relaunched == 1,
                  f"then saved and restarted ({len(env.saved)}, {env.relaunched}, {exits})")
        finally:
            app.destroy()
    # a queued MODEL switch is replaced by a plain turn off; cancelling the wait drops the engine switch
    with Env() as env:
        app, det, ep, exits = _app()
        try:
            U.to_active(app, ep)
            U.pump(app, 0.3)
            app._stop_pending, app._idle_since = ("switch", object()), None
            app._apply_state()
            stops = len(ep.stop_calls)
            app.set_backend("llama_cpp")
            U.pump(app, 0.6)
            check(app._stop_pending == ("off", None) and len(ep.stop_calls) == stops,
                  f"a queued model switch becomes a turn off, still waiting ({app._stop_pending})")
            app.cancel_pending_stop()
            app._switch_at -= 10.0
            app._engine_switch_check()
            check(app._switch_to is None and app.state_name == main.ACTIVE and not env.saved and not exits,
                  "Cancel on the waiting line: the engine switch is dropped, still serving")
        finally:
            app.destroy()


def test_switch_restarts_after_reopen() -> None:
    with Env() as env:
        app, det, ep, exits = _app()
        try:
            U.to_active(app, ep)
            U.pump(app, 0.3)
            started: list = []
            app._reopen_worker = lambda auto_reopen=True: started.append(auto_reopen)   # a reopen still running
            app.set_backend("llama_cpp")
            U.pump_until(app, lambda: app.state_name == main.STOPPING, 3.0)
            ep.status_value = dict(U.IDLE_STATUS)
            U.fire(ep.stop_calls[-1][0], U.stop_result())
            U.pump_until(app, lambda: bool(started), 3.0)
            U.pump(app, 2.0)
            check(started and not env.saved and not exits and app._switch_after_reopen,
                  f"while the closed apps are still being reopened: no save, no restart ({len(env.saved)}, {exits})")
            app._reopen_done([], [])
            check(U.pump_until(app, lambda: bool(exits), 6.0) and len(env.saved) == 1 and env.relaunched == 1
                  and not app._switch_after_reopen, f"reopen finished: saved and restarted ({len(env.saved)}, {exits})")
        finally:
            app.destroy()


# ------------------------------------------------------------------------------------------------ reopen
def test_reopen_keeps_what_was_not_started() -> None:
    app, det, ep = U.new_app()
    orig = (reopen.load_pending, reopen.classify, reopen.reopen, reopen.save_pending, reopen.clear_pending)
    saved: list[list[str]] = []
    cleared = [0]
    done: list[tuple] = []
    try:
        recs = [reopen.LaunchRecord(name=n, exe=f"C:\\Apps\\{n}.exe") for n in ("a", "b", "c")]
        game = reopen.LaunchRecord(name="g", exe="C:\\Games\\g.exe", category="game")
        reopen.load_pending = lambda: recs + [game]
        reopen.classify = lambda records: (list(recs), [game], [])
        reopen.save_pending = lambda rs: saved.append([r.name for r in rs])
        reopen.clear_pending = lambda: cleared.__setitem__(0, cleared[0] + 1)
        app._reopen_done = lambda results, manual: done.append((results, manual))
        # interrupted after the first app (Quit / restart sets the stop event)
        reopen.reopen = lambda records, **k: [
            {"name": "a", "ok": True, "reason": "started", "status": "started"},
            {"name": "b", "ok": False, "reason": "cancelled", "status": "cancelled"},
            {"name": "c", "ok": False, "reason": "cancelled", "status": "cancelled"}]
        app._reopen_worker(True)
        U.pump(app, 0.2)
        check(saved == [["b", "c", "g"]] and not cleared[0],
              f"interrupted: the apps not started yet stay pending, with the game ({saved}, cleared {cleared[0]})")
        check(done and [d["name"] for d in done[-1][1]] == ["b", "c", "g"], "...and are offered")
        # a finished run with no game leaves nothing
        saved.clear()
        reopen.classify = lambda records: (list(recs), [], [])
        reopen.reopen = lambda records, **k: [{"name": n, "ok": True, "reason": "started", "status": "started"}
                                              for n in ("a", "b", "c")]
        app._reopen_worker(True)
        U.pump(app, 0.2)
        check(not saved and cleared[0] == 1, f"finished: the pending file is cleared ({saved}, {cleared[0]})")
    finally:
        (reopen.load_pending, reopen.classify, reopen.reopen, reopen.save_pending, reopen.clear_pending) = orig
        del app._reopen_done
        app.destroy()


# ------------------------------------------------------------------------------------------------ sweep dialog
def test_sweep_dialog_clicks() -> None:
    app, det, ep = U.new_app()
    try:
        rec: list = []
        dlg = sweep_dialog.SweepDialog(app, [dict(c) for c in U.CANDS], lambda d, t: rec.append((d, t)))
        U.pump(app, 0.05)
        dlg.tk.call(dlg.protocol("WM_DELETE_WINDOW"))
        dlg._close_btn.invoke()
        U.pump(app, 0.1)
        check(not rec and dlg.winfo_exists(), f"clicks in the first {sweep_dialog.ARM_S} s are ignored ({rec})")
        U.pump(app, sweep_dialog.ARM_S + 0.2)
        dlg.tk.call(dlg.protocol("WM_DELETE_WINDOW"))
        check(not rec, "the decision does not run inside the click itself")
        dlg._close_btn.invoke()                          # a second click before the first one ran
        U.pump(app, 0.2)
        check(rec == [("cancel", [])], f"one loop turn later: decided exactly once, the first click wins ({rec})")
        # called by the app itself (cancel during a start): at once, as before
        rec.clear()
        dlg = sweep_dialog.SweepDialog(app, [dict(c) for c in U.CANDS], lambda d, t: rec.append((d, t)))
        U.pump(app, 0.6)
        dlg._decide("start_only")
        check(rec == [("start_only", [])], f"_decide() from code is immediate ({rec})")
        U.pump(app, 0.2)
    finally:
        app.destroy()


def run() -> int:
    for t in (test_memory_watch_arming, test_tick_collects_after_an_error, test_ready_status_off_the_tk_thread,
              test_switch_waits_for_pending_stop, test_switch_restarts_after_reopen,
              test_reopen_keeps_what_was_not_started, test_sweep_dialog_clicks):
        print(f"-- {t.__name__}")
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    assert config_store.DATA_DIR is not None
    sys.exit(run())
