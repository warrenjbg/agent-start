"""Engine switch from Settings: one question, safe Turn off first, settings parked, restart. Window test (not CI).

Fakes only: no engine process, no port, config.json writes captured, the relaunch is replaced by a recorder.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_ui_smoke as U  # noqa: E402  (first: it redirects the data dir)

import config_store  # noqa: E402
import engine_profiles  # noqa: E402
import main  # noqa: E402

PASSES = 0
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


class Env:
    """Captures config saves and relaunches; answers the question box."""

    def __init__(self, answer: bool = True, relaunch_ok: bool = True, save_ok: bool = True) -> None:
        self.saved: list[dict] = []
        self.relaunched = 0
        self.asked: list[str] = []
        self.answer, self.relaunch_ok, self.save_ok = answer, relaunch_ok, save_ok
        self._orig = (config_store.save_config, config_store.load_config, main._relaunch_app,
                      main.messagebox.askyesno)

    def __enter__(self) -> "Env":
        def save(cfg, *a, **k):
            if not self.save_ok:
                raise OSError("disk full")
            self.saved.append(dict(cfg))
        config_store.save_config = save
        config_store.load_config = lambda *a, **k: dict(U.CFG, backend="strata", user_whitelist=["keep.exe"])
        main._relaunch_app = lambda: (setattr(self, "relaunched", self.relaunched + 1), self.relaunch_ok)[1]
        main.messagebox.askyesno = lambda title, msg, **k: (self.asked.append(str(msg)), self.answer)[1]
        return self

    def __exit__(self, *exc) -> None:
        (config_store.save_config, config_store.load_config, main._relaunch_app,
         main.messagebox.askyesno) = self._orig


def _app():
    app, det, ep = U.new_app()
    exits: list = []
    app._finish_exit = lambda: exits.append(1)        # the test keeps the window; a real exit is the normal Quit path
    app._can_relaunch = True                          # main._relaunch_app is replaced by Env's recorder
    return app, det, ep, exits


def test_idle() -> None:
    with Env() as env:
        app, det, ep, exits = _app()
        try:
            check(app.set_backend("no-such-engine") == "That engine is not installed" and not env.asked,
                  "unknown engine: a message, no question")
            check(app.set_backend("strata") is None and not env.asked, "the engine in use: nothing happens")
            note = app.set_backend("llama_cpp")
            check(env.asked == ["Switch to llama.cpp?\n\nAgent Start will restart."], f"one question ({env.asked})")
            check(isinstance(note, str) and "llama.cpp" in note, f"a note while it switches ({note!r})")
            U.pump_until(app, lambda: bool(exits), 4.0)
            check(len(env.saved) == 1 and env.saved[0]["backend"] == "llama_cpp" and env.saved[0]["launch_cmd"] == []
                  and env.saved[0][engine_profiles.PROFILES_KEY]["strata"]["launch_cmd"] == U.CFG["launch_cmd"]
                  and env.saved[0]["user_whitelist"] == ["keep.exe"],
                  f"saved once: backend, Strata's settings parked, the rest untouched ({env.saved})")
            check(env.relaunched == 1 and exits == [1], f"restarted once, then the normal exit ({env.relaunched}, {exits})")
            check(app.engine.id == "strata", "the running app never swaps its engine in place")
        finally:
            app.destroy()


def test_declined() -> None:
    with Env(answer=False) as env:
        app, det, ep, exits = _app()
        try:
            check(app.set_backend("llama_cpp") is None and len(env.asked) == 1, "No: nothing changes")
            U.pump(app, 0.6)
            check(not env.saved and not env.relaunched and not exits and app._switch_to is None,
                  "No: nothing saved, no restart")
        finally:
            app.destroy()


def test_failures() -> None:
    with Env(save_ok=False) as env:
        app, det, ep, exits = _app()
        try:
            app.set_backend("llama_cpp")
            U.pump(app, 1.0)
            check(not env.relaunched and not exits and app._switch_to is None and "could not be changed" in U.status(app),
                  f"config.json not writable: no restart, a message ({U.status(app)!r})")
        finally:
            app.destroy()
    with Env(relaunch_ok=False) as env:
        app, det, ep, exits = _app()
        try:
            app.set_backend("llama_cpp")
            U.pump(app, 1.0)
            check(len(env.saved) == 1 and not exits and "restart" in U.status(app).lower(),
                  f"the relaunch failed: the app stays open and says to restart ({U.status(app)!r})")
        finally:
            app.destroy()


def test_serving() -> None:
    with Env() as env:
        app, det, ep, exits = _app()
        try:
            U.to_active(app, ep)
            U.pump(app, 0.3)
            stops_before = len(ep.stop_calls)
            note = app.set_backend("llama_cpp")
            check(env.asked == ["Strata is running. Turn it off and switch to llama.cpp?\n\nAgent Start will restart."],
                  f"serving: the question says it will be turned off ({env.asked})")
            check(isinstance(note, str) and "Turning Strata off" in note, f"note ({note!r})")
            U.pump_until(app, lambda: app.state_name in (main.STOPPING, main.IDLE), 3.0)
            check(not env.saved and not env.relaunched, "nothing is saved before the engine is off")
            check(app.state_name == main.STOPPING and len(ep.stop_calls) == stops_before + 1
                  and ep.stop_calls[-1][1] is False, f"the normal (not forced) Turn off began ({app.state_name})")
            U.pump(app, 1.5)
            check(not env.saved and not exits, "still stopping: still nothing saved")
            ep.status_value = {"state": "idle", "pid": None, "model_id": None, "adopted": False}
            U.fire(ep.stop_calls[-1][0], U.stop_result())
            U.pump_until(app, lambda: bool(exits), 8.0)
            check(app.state_name == main.IDLE and len(env.saved) == 1 and env.saved[0]["backend"] == "llama_cpp"
                  and env.relaunched == 1 and exits == [1],
                  f"once it is off: saved, restarted ({app.state_name}, {len(env.saved)}, {env.relaunched}, {exits})")
        finally:
            app.destroy()


def test_dropped() -> None:
    """A switch whose Turn off does not happen is dropped; nothing is saved later by surprise."""
    with Env() as env:
        app, det, ep, exits = _app()
        try:
            U.to_active(app, ep)
            U.pump(app, 0.3)
            app.on_toggle = lambda: None              # the Turn off is declined (e.g. "a reply is running": Cancel)
            app.set_backend("llama_cpp")
            U.pump(app, 0.5)
            check(app._switch_to == "llama_cpp" and app.state_name == main.ACTIVE, "waiting for the Turn off")
            app._switch_at -= 10.0
            app._engine_switch_check()
            check(app._switch_to is None and "Engine not changed" in U.status(app) and not env.saved,
                  f"still serving with no stop pending: dropped ({U.status(app)!r})")
            # a later, unrelated stop must not switch
            del app.on_toggle
            app.on_toggle()
            U.pump_until(app, lambda: app.state_name == main.STOPPING, 3.0)
            U.fire(ep.stop_calls[-1][0], U.stop_result())
            U.pump_until(app, lambda: app.state_name == main.IDLE, 6.0)
            U.pump(app, 1.6)
            check(app.state_name == main.IDLE, "that stop finished")
            check(not env.saved and not env.relaunched and not exits, "a later Turn off does not switch engines")
        finally:
            app.destroy()


def test_wait_for_exit() -> None:
    import time
    t0 = time.monotonic()
    main._wait_for_exit(["x"])
    main._wait_for_exit(["x", main.AFTER_EXIT_ARG])
    main._wait_for_exit(["x", main.AFTER_EXIT_ARG, "abc"])
    main._wait_for_exit(["x", main.AFTER_EXIT_ARG, "999999"])
    main._wait_for_exit(["x", main.AFTER_EXIT_ARG, str(os.getpid())], timeout_s=5.0)      # ourselves: not waited for
    main._wait_for_exit(["x", main.AFTER_EXIT_ARG, "4"], timeout_s=5.0)                    # System: another program
    check(time.monotonic() - t0 < 2.0, f"bad / foreign / own pids are never waited for ({time.monotonic() - t0:.1f}s)")


def run() -> int:
    for t in (test_wait_for_exit, test_idle, test_declined, test_failures, test_serving, test_dropped):
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
    sys.exit(run())
