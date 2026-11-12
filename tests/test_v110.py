"""v1.10 in the app window: turn the engine back on when the app that caused a stop closes, the usage history, the
engine picker. Written against the v1.10 contract. Stubs only: never contacts an engine, never closes or starts real
apps, fake resume watcher (no thread), the usage file lives in the test data dir."""
from __future__ import annotations

import gc
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_ui_smoke as U  # noqa: E402  (first: redirects the data dir, fakes reopen.launch / webbrowser.open)
import ui_helpers as H  # noqa: E402
import config_store  # noqa: E402
import engines  # noqa: E402
import main  # noqa: E402
from busy_dialog import BusyDialog  # noqa: E402
from game_watch import GameStart  # noqa: E402
from mem_watch import MemReport  # noqa: E402
from resume_watch import ResumeTarget  # noqa: E402
from settings_dialog import SettingsDialog  # noqa: E402
from sweep_dialog import SweepDialog  # noqa: E402

PASSES, FAILS = 0, []


def check(cond, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


pump, pump_until = U.pump, U.pump_until
TOASTS: list = []
DIALOGS: U.Dialogs | None = None


def _fake_toast(title, msg, buttons=None):
    TOASTS.append((title, msg, list(buttons or [])))


def close(app) -> None:
    try:
        if not app._destroyed:
            d = app.dialog
            if isinstance(d, BusyDialog):
                d._decide("cancel", user=False)
            app.destroy()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    gc.collect()


class FakeResume:
    """Stands in for resume_watch.ResumeWatcher: records calls, no thread, never looks at processes."""

    def __init__(self) -> None:
        self.watched: list = []
        self.cancels = 0
        self.stopped = 0
        self._target = None

    @property
    def watching(self) -> bool:
        return self._target is not None

    def watch(self, target) -> None:
        self.watched.append(target)
        self._target = target

    def cancel(self) -> None:
        self.cancels += 1
        self._target = None

    def stop(self, timeout: float = 2.0) -> None:
        self.stopped += 1
        self._target = None


def _fake_resume(app) -> FakeResume:
    real = getattr(app, "resume", None)
    if real is not None and not isinstance(real, FakeResume):
        try:
            real.stop()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    app.resume = FakeResume()
    return app.resume


GAME_EXE = r"D:\SteamLibrary\steamapps\common\ELDEN RING\eldenring.exe"


def _game(title: str, pid: int = 70001) -> GameStart:
    exe = GAME_EXE.replace("ELDEN RING", title)
    return GameStart(name=os.path.basename(exe), title=title, pid=pid, create_time=1_731_370_000.0 + pid, exe=exe,
                     ram_mb=9000.0)


def _mem_report(title: str = "Blender", pid: int = 70100) -> MemReport:
    return MemReport(name="blender.exe", title=title, pid=pid, create_time=1_731_370_500.0,
                     exe=r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe", ram_mb=6349.0,
                     member_count=1, avail_mb=900.0, avail_eff_mb=900.0, floor_mb=2048.0, baseline_avail_mb=20000.0,
                     app_threshold_mb=2048.0, sustained_s=20.0)


def _target(title: str = "Blender") -> ResumeTarget:
    return ResumeTarget(title=title, kind="app", game_key=None, exe=None, members=frozenset({(70100, 1.0)}),
                        since=time.time())


def _stop_runs(app, ep, trigger, result: dict | None = None, before_fire=None) -> bool:
    """trigger() must lead to a stop (safe stop: after the grace); answer it with `result` (default: released)."""
    n = len(ep.stop_calls)
    trigger()
    ok = pump_until(app, lambda: len(ep.stop_calls) > n, main.IDLE_GRACE_S + 3)
    if ok:
        if before_fire is not None:
            before_fire()
        ep.status_value = dict(U.IDLE_STATUS)
        U.fire(ep.stop_calls[-1][0], result or U.stop_result())
        pump(app, 0.5)
    return ok


def _no_real_sweep(det) -> bool:
    """Nothing was closed: the detector never got a sweep_processes(dry_run=False) (the idle preview's dry runs
    only count apps)."""
    return not any(not dry for dry, _t in det.sweep_calls)


# ------------------------------------------------------------------------------------------------ A. resume
def test_resume() -> None:
    name = "Strata"
    # constants + the protocol verb
    check(main.RESUME_ACTIONS == ("ask", "on", "none"), f"RESUME_ACTIONS ({main.RESUME_ACTIONS})")
    check(main.resume_labels(name) == {"ask": "Ask me", "on": "Turn Strata back on", "none": "Do nothing"},
          f"resume_labels ({main.resume_labels(name)})")
    check(main.RESUME_ALERT_VALID_S == 1800.0, "RESUME_ALERT_VALID_S is 30 min")
    check(main.parse_protocol(["agentstart://resume-on"]) == "resume-on", "protocol: resume-on")
    check(main.parse_protocol(["agentstart://resume-on?x=1"]) == "show", "protocol: resume-on with a query -> show")
    check(main.parse_protocol(["agentstart://resume-off"]) == "show", "protocol: another verb -> show")
    check(main.parse_protocol(["agentstart://game-off"]) == "game-off", "protocol: game-off unchanged")
    labels = main.resume_labels(name)

    # Settings row + the saved choice
    app, det, ep = U.new_app()
    try:
        fake = _fake_resume(app)
        app._extras = True
        check(app._resume_action == "ask", "default: Ask me")
        d = H.open_settings(app)
        check(d is not None, "Settings opens")
        if d is not None:
            keys = list(getattr(d, "switches", {}))
            check(keys == ["reopen", "tray", "autostart", "games", "memory", "resume", "updates"],
                  f"the resume row sits right after the memory row ({keys})")
            row = H.settings_option(d, "When that app closes")
            check(row["value"] == "Ask me" and row["choices"] == [labels[a] for a in main.RESUME_ACTIONS]
                  and row["enabled"], f"Settings row 'When that app closes' ({row})")
            check(H.choose_settings_option(d, "When that app closes", "Turn Strata back on"), "pick Turn Strata back on")
            check(app._resume_action == "on", f"choice applied ({app._resume_action})")
            H.close_settings(d)
            pump(app, 0.3)
        check(pump_until(app, lambda: config_store.load_state().get("resume_action") == "on", 3),
              "saved as state.json resume_action")
        check(app.set_resume_action("Ask me") is True and app._resume_action == "ask", "a label is accepted")
        check(app.set_resume_action("on") is True and app._resume_action == "on", "an id is accepted")
        # "none" ends a running watch and drops a pending alert
        fake.watch(_target())
        app._resume_alert = (time.monotonic(), "Blender")
        n = fake.cancels
        check(app.set_resume_action("none") is True and app._resume_action == "none", "Do nothing set")
        check(fake.cancels > n and not fake.watching and app._resume_alert is None,
              f"Do nothing cancels the watch and the alert ({fake.cancels}, {app._resume_alert})")
        check(pump_until(app, lambda: config_store.load_state().get("resume_action") == "none", 3), "none saved")
        app.set_resume_action("on")
        pump_until(app, lambda: config_store.load_state().get("resume_action") == "on", 3)
    finally:
        close(app)
    app = None
    try:
        app, det, ep = U.new_app()
        check(app._resume_action == "on", f"the choice survives a restart ({app._resume_action})")
    finally:
        if app is not None:
            close(app)
    for junk in (None, "", "bogus", 3, ["on"]):
        a2 = None
        config_store.save_state({"resume_action": junk})
        try:
            a2, _d2, _e2 = U.new_app()
            check(a2._resume_action == "ask", f"state.json resume_action {junk!r} -> Ask me ({a2._resume_action})")
        finally:
            if a2 is not None:
                close(a2)
    config_store.save_state({"resume_action": None})

    # the stop path: watched only after a released stop made for a remembered app
    app, det, ep = U.new_app()
    try:
        fake = _fake_resume(app)
        check(app._resume_action == "ask" and app._game_action == "off", "defaults: ask / game stop")
        U.to_active(app, ep)
        pump(app, 0.4)
        # a. game (Turn Strata off) with its GameStart -> watched once it is off
        app._on_game_started("ELDEN RING", 9000.0, _game("ELDEN RING"))
        check("ELDEN RING" in getattr(app, "_resume_targets", {}), "game with src: target remembered")
        ok = _stop_runs(app, ep, lambda: None)
        check(ok, "game: safe stop ran")
        check(len(fake.watched) == 1 and fake.watched[0].title == "ELDEN RING" and fake.watched[0].kind == "game",
              f"released game stop -> watch(target) once ({fake.watched})")
        pump(app, 0.5)
        check(len(fake.watched) == 1, "watch called once only")
        # adopting a running server cancels the watch and drops an alert
        app._resume_alert = (time.monotonic(), "ELDEN RING")
        n = fake.cancels
        U.to_active(app, ep)
        pump(app, 0.3)
        check(fake.cancels > n and app._resume_alert is None, "engine adopted (-> ACTIVE): watch cancelled, alert gone")
        # b. the user's own Turn off: nothing watched
        w = len(fake.watched)
        ok = _stop_runs(app, ep, lambda: H.click_main_button(app))
        check(ok and len(fake.watched) == w, f"user Turn off: no watch ({ok}, {fake.watched[w:]})")
        # c. a stop for a title with no remembered target (old caller without src)
        U.to_active(app, ep)
        pump(app, 0.3)
        app._on_game_started("Hades", 1.0)
        check("Hades" not in app._resume_targets, "no src: nothing remembered")
        ok = _stop_runs(app, ep, lambda: None)
        check(ok and len(fake.watched) == w, "stop for a game without a target: no watch")
        # d. memory app (Turn Strata off) with its MemReport
        app.set_memory_action("Turn Strata off")
        U.to_active(app, ep)
        pump(app, 0.3)
        ok = _stop_runs(app, ep, lambda: app._on_memory_app("Blender", 6349.0, "why", _mem_report()))
        check(ok and len(fake.watched) == w + 1 and fake.watched[-1].title == "Blender"
              and fake.watched[-1].kind == "app", f"memory stop -> watch the app ({fake.watched[w:]})")
        app.set_memory_action("Ask me")
        # e. game "Ask me": the toast button stops it, then it is watched
        app.set_game_action("Ask me")
        U.to_active(app, ep)
        pump(app, 0.3)
        TOASTS.clear()
        app._on_game_started("Celeste", 1.0, _game("Celeste", 70002))
        pump_until(app, lambda: TOASTS, 3)
        w = len(fake.watched)
        ok = _stop_runs(app, ep, app.game_off_requested)
        check(ok and len(fake.watched) == w + 1 and fake.watched[-1].title == "Celeste",
              f"ask + toast button: watched after the stop ({fake.watched[w:]})")
        app.set_game_action("Turn Strata off")
        # f. "Do nothing": never watched
        app.set_resume_action("Do nothing")
        U.to_active(app, ep)
        pump(app, 0.3)
        w = len(fake.watched)
        ok = _stop_runs(app, ep, lambda: app._on_game_started("Doom", 1.0, _game("Doom", 70003)))
        check(ok and len(fake.watched) == w, "Do nothing: no watch")
        app.set_resume_action("Ask me")
        # g. a model switch finishing a game stop: no watch (simulated: the stop is marked as a switch)
        det.cands = []
        U.to_active(app, ep)
        pump(app, 0.3)
        w, starts = len(fake.watched), len(ep.start_calls)

        def as_switch() -> None:
            app._switching = True

        ok = _stop_runs(app, ep, lambda: app._on_game_started("Doom", 1.0, _game("Doom", 70004)), before_fire=as_switch)
        check(ok and len(fake.watched) == w, "model switch: no watch")
        check(pump_until(app, lambda: len(ep.start_calls) > starts, 3), "(the switch starts the engine again)")
        if len(ep.start_calls) > starts:
            ep.status_value = U.active_status()
            U.fire(ep.start_calls[-1][0])
            pump_until(app, lambda: app.state_name == "ACTIVE", 3)
        # h. a failed stop: no watch
        U.to_active(app, ep)
        pump(app, 0.3)
        w = len(fake.watched)
        bad = U.stop_result(ok=False, port_released=False, killed_pids=[], ram_freed_mb=0.0, unload="error",
                            error="Stop failed (test)")
        ok = _stop_runs(app, ep, lambda: app._on_game_started("Doom", 1.0, _game("Doom", 70005)), result=bad)
        check(ok and len(fake.watched) == w and app.state_name != "IDLE",
              f"failed stop: no watch ({app.state_name}, {fake.watched[w:]})")
    finally:
        close(app)

    # i. an app exit that ends a game stop: no watch, the watcher is stopped
    app, det, ep = U.new_app()
    try:
        fake = _fake_resume(app)
        U.to_active(app, ep)
        pump(app, 0.3)

        def exiting() -> None:
            app._exit_pending = True             # the app exits once this stop is done (e.g. an update installs)

        ok = _stop_runs(app, ep, lambda: app._on_game_started("Doom", 1.0, _game("Doom", 70006)), before_fire=exiting)
        check(ok and pump_until(app, lambda: app._destroyed, 3), "exit after the stop")
        check(not fake.watched, f"app exit: no watch ({fake.watched})")
        check(fake.stopped >= 1, "app exit: the watcher is stopped")
    finally:
        close(app)

    # remembered targets: at most 8, oldest dropped
    app, det, ep = U.new_app()
    try:
        _fake_resume(app)
        app.set_game_action("Ask me")
        U.to_active(app, ep)
        pump(app, 0.3)
        titles = [f"Game {i}" for i in range(10)]
        for i, t in enumerate(titles):
            app._on_game_started(t, 1.0, _game(t, 70200 + i))
        pump(app, 0.3)
        keys = list(getattr(app, "_resume_targets", {}))
        check(len(keys) == 8 and set(keys) == set(titles[2:]), f"at most 8 targets, oldest dropped ({keys})")
        app.set_game_action("Turn Strata off")
    finally:
        close(app)

    # the watcher reports the app closed: Ask me (toast) / the toast's button
    app, det, ep = U.new_app()
    try:
        fake = _fake_resume(app)
        TOASTS.clear()
        app._on_resume_closed(_target("Blender"), 90.0)
        check(pump_until(app, lambda: TOASTS, 3), "ask: a toast")
        if TOASTS:
            title, body, buttons = TOASTS[-1]
            check(title == "Blender closed" and f"Turn {name} back on?" in body, f"ask: toast text ({TOASTS[-1]})")
            check(buttons == [] and body.endswith("Open Agent Start to turn it on."),
                  "from source: no button, says to open Agent Start")
        al = app._resume_alert
        check(al is not None and al[1] == "Blender" and abs(time.monotonic() - al[0]) < 5, f"alert remembered ({al})")
        check(app.state_name == "IDLE" and not ep.start_calls, "ask: nothing starts by itself")
        # long names are shortened like the other toasts
        long_title = "The Elder Scrolls V Skyrim Special Edition Anniversary Launcher"
        TOASTS.clear()
        app._on_resume_closed(_target(long_title), 5.0)
        pump_until(app, lambda: TOASTS, 3)
        check(TOASTS and TOASTS[-1][0] == f"{main._short(long_title, 40)} closed",
              f"long title shortened ({TOASTS[-1:]})")
        # installed app with the protocol registered: the toast has the button
        real_frozen, real_reg = config_store.FROZEN, main._protocol_registered
        config_store.FROZEN, main._protocol_registered = True, lambda: True
        try:
            TOASTS.clear()
            app._on_resume_closed(_target("Blender"), 5.0)
            pump_until(app, lambda: TOASTS, 3)
            check(TOASTS and TOASTS[-1][2] == [(f"Turn on {name}", "agentstart://resume-on")],
                  f"installed: the Turn on button ({TOASTS[-1:]})")
        finally:
            config_store.FROZEN, main._protocol_registered = real_frozen, real_reg
        # the button: stale / no alert -> only shows the window
        app._resume_alert = (time.monotonic() - main.RESUME_ALERT_VALID_S - 1, "Blender")
        app.withdraw()
        pump(app, 0.2)
        app.resume_on_requested()
        pump(app, 0.4)
        check(app.state() == "normal" and app.state_name == "IDLE" and not ep.start_calls
              and not isinstance(app.dialog, SweepDialog), "stale alert: the window opens, nothing starts")
        app._resume_alert = None
        app.withdraw()
        pump(app, 0.2)
        app.resume_on_requested()
        pump(app, 0.4)
        check(app.state() == "normal" and app.state_name == "IDLE" and not ep.start_calls,
              "no alert: the window opens, nothing starts")
        # fresh alert: the normal Turn on, with the close-apps dialog
        app._on_resume_closed(_target("Blender"), 5.0)
        pump(app, 0.2)
        check(app._resume_alert is not None, "fresh alert")
        app.withdraw()
        pump(app, 0.2)
        app.resume_on_requested()
        check(pump_until(app, lambda: isinstance(app.dialog, SweepDialog), 3),
              f"fresh alert: the normal Turn on (close-apps dialog) ({app.state_name}, {app.dialog})")
        check(app.state_name == "STARTING" and app._resume_alert is None, "starting; the alert is used up")
        check(_no_real_sweep(det) and not ep.start_calls, "nothing closed / started before the user decides")
        if isinstance(app.dialog, SweepDialog):
            app.dialog._decide("cancel")
        check(pump_until(app, lambda: app.state_name == "IDLE", 3), "dialog cancelled -> idle")
        # "Do nothing": a report is ignored
        app.set_resume_action("Do nothing")
        TOASTS.clear()
        app._on_resume_closed(_target("Blender"), 5.0)
        pump(app, 0.4)
        check(not TOASTS and app._resume_alert is None, "Do nothing: no toast")
        app.set_resume_action("Ask me")
        # not IDLE: a report and the button do nothing
        U.to_active(app, ep)
        pump(app, 0.3)
        TOASTS.clear()
        app._on_resume_closed(_target("Blender"), 5.0)
        pump(app, 0.4)
        check(not TOASTS and app._resume_alert is None, "serving: a 'closed' report is ignored")
        app._resume_alert = (time.monotonic(), "Blender")
        n = len(ep.start_calls)
        app.withdraw()
        pump(app, 0.2)
        app.resume_on_requested()
        pump(app, 0.4)
        check(app.state() == "normal" and app.state_name == "ACTIVE" and len(ep.start_calls) == n,
              "serving: the button only shows the window")
    finally:
        close(app)

    # "Turn Strata back on": starts without the close-apps dialog; something open -> asks instead
    app, det, ep = U.new_app()
    try:
        fake = _fake_resume(app)
        app.set_resume_action("Turn Strata back on")
        TOASTS.clear()
        n_cancel = fake.cancels
        seen_dialog: list[bool] = []
        app._on_resume_closed(_target("Blender"), 30.0)
        ok = pump_until(app, lambda: seen_dialog.append(isinstance(app.dialog, SweepDialog)) or bool(ep.start_calls), 3)
        pump(app, 0.3)
        seen_dialog.append(isinstance(app.dialog, SweepDialog))
        check(ok and app.state_name == "STARTING", f"on: the engine starts ({app.state_name}, {len(ep.start_calls)})")
        check(not any(seen_dialog) and _no_real_sweep(det), "on: no close-apps dialog, nothing closed")
        check(fake.cancels > n_cancel and app._resume_alert is None, "starting cancels the watch")
        check(pump_until(app, lambda: TOASTS, 3) and TOASTS[-1][0] == f"{name} is starting"
              and "Blender" in TOASTS[-1][1], f"on: a toast names the app ({TOASTS[-1:]})")
        # back to idle (cancel the start)
        if ep.start_calls:
            n = len(ep.stop_calls)
            app.cancel_start()
            if pump_until(app, lambda: len(ep.stop_calls) > n, 2):
                U.fire(ep.stop_calls[-1][0], U.stop_result(cancelled_start=True))
            pump_until(app, lambda: app.state_name == "IDLE", 3)
        check(app.state_name == "IDLE", f"(start cancelled: {app.state_name})")
        # a dialog is open: behaves like Ask me
        d = H.open_settings(app)
        check(d is not None, "Settings open")
        TOASTS.clear()
        n = len(ep.start_calls)
        app._on_resume_closed(_target("Blender"), 30.0)
        check(pump_until(app, lambda: TOASTS, 3) and TOASTS[-1][0] == "Blender closed",
              f"blocked: asks instead ({TOASTS[-1:]})")
        check(len(ep.start_calls) == n and app.state_name == "IDLE" and app._resume_alert is not None,
              "blocked: nothing starts, the alert is kept for the button")
        if d is not None:
            H.close_settings(d)
            pump(app, 0.3)
        # Turn on without skip_sweep: unchanged (the close-apps dialog)
        app.turn_on()
        check(pump_until(app, lambda: isinstance(app.dialog, SweepDialog), 3) and _no_real_sweep(det),
              "turn_on(): the close-apps dialog as before")
        if isinstance(app.dialog, SweepDialog):
            app.dialog._decide("cancel")
        pump_until(app, lambda: app.state_name == "IDLE", 3)
    finally:
        close(app)
    # being destroyed: a late report does nothing
    TOASTS.clear()
    try:
        app._on_resume_closed(_target("Blender"), 1.0)
        time.sleep(0.3)
        check(not TOASTS, "after exit: a late report shows nothing")
    except Exception:  # noqa: BLE001
        check(False, f"after exit: a late report raised\n{traceback.format_exc()}")
    config_store.save_state({"resume_action": None, "game_action": "off", "memory_action": None})


# ------------------------------------------------------------------------------------------------ B. usage
def test_usage() -> None:
    import usage_history
    usage_file = Path(config_store.DATA_DIR) / "usage.json"
    check(Path(config_store.DATA_DIR) == U.HOME, "test data dir in use")
    usage_file.unlink(missing_ok=True)
    app, det, ep = U.new_app()
    try:
        _fake_resume(app)
        u = app.usage
        check(isinstance(u, usage_history.UsageHistory), f"app.usage is a UsageHistory ({type(u).__name__})")
        if not isinstance(u, usage_history.UsageHistory):
            raise RuntimeError("no usage history")
        check(Path(u.path).resolve() == usage_file.resolve(), f"usage.json in the test data dir ({u.path})")
        begins: list = []
        real_begin = u.begin_session
        u.begin_session = lambda *a, **k: (begins.append(1), real_begin(*a, **k))[1]
        base = u.totals().total
        # an adopted server: no new session
        U.to_active(app, ep)
        pump(app, 0.5)
        check(not begins, "adopting a running server: no new usage session")
        # finished requests reported again on every poll are counted once
        fm = H.fake_metrics(app)
        now = time.time()
        fm.add(5)
        for i, r in enumerate(fm.requests):
            r["time"] = now - 120 + i
        want_prompt = sum(int(r["prompt_total"]) for r in fm.requests)
        check(pump_until(app, lambda: u.totals().total.requests == base.requests + 5, 4),
              f"5 finished requests recorded ({u.totals().total.requests - base.requests})")
        pump(app, 6 * main.LIVE_POLL_S + 0.5)        # the same requests come back in every poll
        tot = u.totals().total
        check(tot.requests == base.requests + 5, f"repeated polls count them once ({tot.requests - base.requests})")
        check(tot.prompt_tokens == base.prompt_tokens + want_prompt,
              f"prompt tokens counted ({tot.prompt_tokens - base.prompt_tokens} of {want_prompt})")
        # the app's own launch begins a new session
        ok = _stop_runs(app, ep, lambda: H.click_main_button(app))
        check(ok and app.state_name == "IDLE", "stopped")
        det.cands = []                               # no close-apps dialog
        n = len(ep.start_calls)
        app.turn_on()
        check(pump_until(app, lambda: len(ep.start_calls) > n, 3), "engine start requested")
        check(len(begins) == 1, f"a new engine start begins a usage session ({len(begins)})")
    finally:
        close(app)
    check(usage_file.exists(), "usage.json written on exit")
    try:
        doc = json.loads(usage_file.read_text(encoding="utf-8-sig")) if usage_file.exists() else None
        check(isinstance(doc, dict) and doc.get("days"), "usage.json has the recorded days")
    except ValueError:
        check(False, "usage.json is valid JSON")
    reloaded = usage_history.UsageHistory(usage_file)
    check(reloaded.totals().total.requests >= 5, f"reloaded file keeps the requests ({reloaded.totals().total})")

    # the history window: from Settings (a child of it) and directly
    app, det, ep = U.new_app()
    try:
        _fake_resume(app)
        import history_dialog
        d = H.open_settings(app)
        check(d is not None and getattr(d, "_history_btn", None) is not None, "Settings has 'Usage history…'")
        b = H.settings_button(d, "Usage history") if d is not None else None
        check(b is not None and b["enabled"], f"the button is enabled ({b})")
        if d is not None and b is not None:
            H.click_settings_button(d, "Usage history")
            check(pump_until(app, lambda: isinstance(getattr(d, "_child", None), history_dialog.HistoryDialog), 3),
                  "the history window opens as Settings' child")
            child = getattr(d, "_child", None)
            if child is not None:
                closer = getattr(child, "close", None)
                closer() if callable(closer) else child.destroy()
            check(pump_until(app, lambda: getattr(d, "_child", None) is None, 3), "history closed")
            pump(app, 0.3)
            check(H.open_dialog(app) is d, "Settings still open")
            again = H.settings_button(d, "Usage history")       # this app runs without extras: its dropdowns are off
            check(again is not None and again["enabled"] and H.click_settings_button(d, "Usage history")
                  and pump_until(app, lambda: isinstance(getattr(d, "_child", None), history_dialog.HistoryDialog), 3),
                  "Settings still usable: the history opens a second time")
            child = getattr(d, "_child", None)
            if child is not None:
                pump(app, 0.6)
                child.close()
                pump_until(app, lambda: getattr(d, "_child", None) is None, 3)
            H.close_settings(d)
            check(pump_until(app, lambda: H.open_dialog(app) is None, 3), "Settings closes")
        w = app.open_history()
        check(isinstance(w, history_dialog.HistoryDialog), f"open_history() returns the window ({type(w).__name__})")
        if w is not None:
            pump(app, 0.3)
            w.destroy()
            pump(app, 0.2)
        app.usage = None
        check(app.open_history() is None, "no usage store: open_history() is None")
    finally:
        close(app)


# ------------------------------------------------------------------------------------------------ C. engine picker
class _FakeEngine(engines.Engine):
    id, name, default_port, log_prefix = "v110fake", "Fake Engine", 9932, "fake"
    capabilities = frozenset()

    def is_server_process(self, name, cmdline):
        return False

    def verified_by_process(self, proc, cfg):
        return False

    def claims_port(self, cmdline, port, cfg):
        return False

    def probe(self, http, timeout):
        from engines.base import ServiceState
        return ServiceState()

    def protects_process(self, cmdline, exe, cfg):
        return False


def test_resume_after_cancelled_load() -> None:
    """A game that starts while the engine is still loading cancels the load; that game is watched too."""
    app, det, ep = U.new_app()
    try:
        fake = _fake_resume(app)
        app._game_pref, app._game_action = True, "off"
        cancelled: list[int] = []
        app.cancel_start = lambda: cancelled.append(1)
        app.state_name, app._start_phase = "STARTING", "server"
        app._on_game_started("Doom", 1.0, _game("Doom"))
        check(cancelled == [1] and app._cancel_game == "Doom", "a game during the load cancels the start")
        del app.cancel_start
        app.on_stopped(U.stop_result(cancelled_start=True))
        pump(app, 0.3)
        watched = list(fake.watched)
        check(app.state_name == "IDLE" and app._cancel_game is None, "back to idle after the cancelled load")
        check(len(watched) == 1 and watched[0].title == "Doom" and watched[0].kind == "game",
              f"the game the load was cancelled for is watched ({watched})")
        # a cancelled start with no game behind it watches nothing
        fake.watched.clear()
        app.state_name, app._start_phase = "STARTING", "server"
        app.on_stopped(U.stop_result(cancelled_start=True))
        pump(app, 0.2)
        check(not fake.watched, "a user-cancelled start watches nothing")
    finally:
        close(app)


def test_engine_picker() -> None:
    engines.available()                              # discovery done before the fake is added
    check(engines.choices() == [("strata", "Strata")], f"only Strata ({engines.choices()})")
    try:
        _engine_picker_checks()
    finally:
        engines._FACTORIES.pop("v110fake", None)
    check(engines.choices() == [("strata", "Strata")], "fake engine removed")


def _engine_picker_checks() -> None:
    saved: list = []
    real_save = config_store.save_config
    app = None
    try:
        app, det, ep = U.new_app()
        _fake_resume(app)
        d = H.open_settings(app)
        check(d is not None and d.has_engine_row is False, "one engine: no engine row")
        h1 = None
        if d is not None:
            try:
                H.settings_option(d, "Engine")
                no_row = False
            except LookupError:
                no_row = True
            check(no_row, "one engine: no 'Engine' dropdown")
            h1 = d.logical_height
            want = (SettingsDialog.HEIGHT + SettingsDialog.SWITCH_STEP * (len(d.switches) - 1)
                    + (SettingsDialog.UPDATE_ROW_H if getattr(d, "_check_btn", None) is not None else 0))
            check(h1 == want, f"one engine: height as before ({h1} vs {want})")
            H.close_settings(d)
            pump(app, 0.3)
        engines._FACTORIES["v110fake"] = _FakeEngine
        check(sorted(engines.choices()) == [("strata", "Strata"), ("v110fake", "Fake Engine")],
              f"two engines listed ({engines.choices()})")
        picked: list = []
        real_set = app.set_backend
        app.set_backend = lambda engine_id: (picked.append(engine_id), real_set(engine_id))[1]
        config_store.save_config = lambda cfg, *a, **k: saved.append(dict(cfg))
        eng = app.engine
        d = H.open_settings(app)
        check(d is not None and d.has_engine_row is True, "two engines: the engine row")
        if d is not None:
            check(h1 is not None and d.logical_height == h1 + SettingsDialog.ENGINE_ROW_H,
                  f"height grows by ENGINE_ROW_H ({h1} -> {d.logical_height})")
            row = H.settings_option(d, "Engine")
            check(row["value"] == "Strata" and sorted(row["choices"]) == ["Fake Engine", "Strata"] and row["enabled"],
                  f"the menu lists both, Strata selected ({row})")
            check(H.choose_settings_option(d, "Engine", "Fake Engine"), "pick Fake Engine")
            check(picked == ["v110fake"], f"picking calls set_backend(id) ({picked})")
            check(pump_until(app, lambda: saved and saved[-1].get("backend") == "v110fake", 3),
                  f"saved into config.json ({saved[-1:]})")
            check(app.engine is eng and app.engine.id == "strata", "the running app keeps its engine")
            H.close_settings(d)
            pump(app, 0.3)
    finally:
        config_store.save_config = real_save
        if app is not None:
            close(app)
        app = None
    # set_backend directly
    saved.clear()
    try:
        app, det, ep = U.new_app()
        _fake_resume(app)
        config_store.save_config = lambda cfg, *a, **k: saved.append(dict(cfg))
        eng = app.engine
        msg = app.set_backend("no-such-engine")
        pump(app, 0.5)
        check(isinstance(msg, str) and msg and not saved, f"unknown id: a message, nothing saved ({msg!r}, {saved})")
        res = app.set_backend("strata")
        pump(app, 0.5)
        check(res is None and not saved, f"current id: nothing saved, None ({res!r}, {saved})")
        note = app.set_backend("v110fake")
        check(isinstance(note, str) and "restart" in note.lower(), f"another engine: a restart note ({note!r})")
        check(pump_until(app, lambda: saved and saved[-1].get("backend") == "v110fake", 3),
              f"backend saved ({saved[-1:]})")
        check(app.engine is eng and app.engine.id == "strata", "app.engine unchanged")
    finally:
        config_store.save_config = real_save
        if app is not None:
            close(app)


def run() -> int:
    global DIALOGS
    main.TOGGLE_DEBOUNCE_S = 0.0
    old = (main.LIVE_POLL_S, main.IDLE_GRACE_S)
    main.LIVE_POLL_S, main.IDLE_GRACE_S = 0.2, 0.8
    DIALOGS = U.Dialogs()
    DIALOGS.install()
    real_toast = main._toast_worker
    main._toast_worker = _fake_toast             # never show real Windows toasts from the tests
    try:
        for fn in (test_resume, test_resume_after_cancelled_load, test_usage, test_engine_picker):
            gc.collect()
            print(f"-- {fn.__name__}", flush=True)
            try:
                fn()
            except Exception:  # noqa: BLE001
                FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
                print(f"FAIL: {FAILS[-1]}")
    finally:
        DIALOGS.restore()
        main._toast_worker = real_toast
        main.LIVE_POLL_S, main.IDLE_GRACE_S = old
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
