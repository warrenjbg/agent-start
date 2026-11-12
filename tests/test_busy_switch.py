"""Safe "Turn off" during a reply (BusyDialog / stop-when-idle) + the quick model switch button.

Stub backends only (the ones of test_ui_smoke.py): never touches real processes, port 8080 or the real data dir.

Run:  .venv\\Scripts\\python.exe tests\\test_busy_switch.py
Exit code 0 = all checks passed. Screenshots land in tests\\ui_busy_*.png.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

# FIRST: redirects the data dir (AGENT_START_HOME) to a temp folder and fakes reopen.launch / webbrowser.open.
import test_ui_smoke as U  # noqa: E402

import customtkinter as ctk  # noqa: E402

import main  # noqa: E402
import config_store  # noqa: E402
import launcher_bat  # noqa: E402
import reopen  # noqa: E402
from busy_dialog import BusyDialog, describe  # noqa: E402
from sweep_dialog import SweepDialog  # noqa: E402

assert config_store.DATA_DIR == U.HOME, "tests must not write to the real data dir"
assert U.TEST_PORT != 8080

FAILS: list[str] = []
PASSES = 0

GRACE = 0.8          # main.IDLE_GRACE_S during the run
POLL = 0.2           # main.LIVE_POLL_S during the run

pump, pump_until, status, pill, to_active, fire = U.pump, U.pump_until, U.status, U.pill, U.to_active, U.fire
IDLE_STATUS, active_status, stop_result = U.IDLE_STATUS, U.active_status, U.stop_result


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


def info(msg: str) -> None:
    print(f"INFO: {msg}")


# ---------------------------------------------------------------- helpers
def busy_dialog_open(app) -> bool:
    return app._dialog_open() and isinstance(app.dialog, BusyDialog)


def go_busy(app, kind: str = "generating", queued: int = 0) -> bool:
    """The fake /metrics reports a running request; wait until the app has seen it."""
    f = app._fetch_metrics
    f.fail, f.live, f.queued = False, kind, queued
    return pump_until(app, lambda: (app.metrics.activity() or {}).get("kind") == kind, 5)


def go_model_idle(app) -> bool:
    f = app._fetch_metrics
    f.fail, f.live, f.queued = False, "idle", 0
    return pump_until(app, lambda: app.metrics.activity() is None, 5)


def serving(app, ep, model: str = "swift-test-1") -> None:
    """ACTIVE with at least one /metrics document in (model idle)."""
    app._fetch_metrics.live, app._fetch_metrics.queued, app._fetch_metrics.fail = "idle", 0, False
    ep.status_value = active_status(True, model)
    app.on_status(ep.status())
    pump(app, 0.05)
    n = app.metrics.polls
    pump_until(app, lambda: app.metrics.polls > n, 5)


def finish_stop(app, ep, **kw) -> bool:
    """The endpoint manager reports the stop finished."""
    ep.status_value = dict(IDLE_STATUS)
    fire(ep.stop_calls[-1][0], stop_result(**kw))
    return pump_until(app, lambda: app.state_name != "STOPPING", 4)


def settle_reopen(app) -> None:
    pump_until(app, lambda: not app._reopening, 5)


def pending(app):
    return getattr(app, "_stop_pending", "<no _stop_pending attribute>")


def press_busy_toggle(app, ep) -> BusyDialog | None:
    """Toggle while the model is busy; returns the BusyDialog (or None, with a failed check)."""
    n = len(ep.stop_calls)
    app.on_toggle()
    pump(app, 0.5)                               # past busy_dialog.ARM_S: clicks before it are ignored
    ok = busy_dialog_open(app)
    check(ok, f"busy + toggle opens a BusyDialog as app.dialog (dialog={type(app.dialog).__name__}, "
              f"state={app.state_name})")
    check(len(ep.stop_calls) == n and app.state_name == "ACTIVE", "busy + toggle: nothing stopped yet, still ACTIVE")
    return app.dialog if ok else None


def close(app) -> None:
    try:
        if not app._destroyed:
            if app._dialog_open():
                try:
                    d = app.dialog
                    d._decide("cancel", user=False) if isinstance(d, BusyDialog) else d._decide("cancel")
                except Exception:  # noqa: BLE001
                    pass
            app.destroy()
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def same_path(a, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def disk_config() -> dict:
    return json.loads((U.HOME / "config.json").read_text(encoding="utf-8"))


# ================================================================ 1) safe Turn off
def test_toggle_model_idle() -> None:
    """Model idle -> Turn off works exactly as before (no dialog)."""
    app, det, ep = U.new_app()
    try:
        serving(app, ep)
        check(app.state_name == "ACTIVE" and app.metrics.activity() is None, "ACTIVE, model idle")
        check(pending(app) is None, f"_stop_pending starts as None ({pending(app)!r})")
        n = len(ep.stop_calls)
        app.on_toggle()
        pump(app, 0.2)
        check(not busy_dialog_open(app), "idle model: no BusyDialog")
        check(len(ep.stop_calls) == n + 1 and app.state_name == "STOPPING", "idle model: toggle stops right away")
        check(ep.stop_calls[n:] and ep.stop_calls[-1][1] is False, "idle model: the stop is not forced")
        check(finish_stop(app, ep) and app.state_name == "IDLE", f"stop result -> IDLE ({app.state_name})")
        settle_reopen(app)
    finally:
        close(app)


def test_double_click_guard() -> None:
    """The dialog opens centred over Turn off: the 2nd click of a double-click must not pick a button."""
    app, det, ep = U.new_app()
    try:
        serving(app, ep)
        check(go_busy(app, "generating"), "busy (guard test)")
        n = len(ep.stop_calls)
        app.on_toggle()
        pump(app, 0.05)
        dlg = app.dialog
        check(busy_dialog_open(app), "dialog open")
        if busy_dialog_open(app):
            dlg.now_btn.invoke()                 # within ARM_S of opening
            pump(app, 0.1)
            check(busy_dialog_open(app) and len(ep.stop_calls) == n and app.state_name == "ACTIVE",
                  "a click right after opening is ignored (dialog stays, nothing stopped)")
            pump(app, 0.5)
            dlg.cancel_btn.invoke()
            pump(app, 0.3)
            check(not app._dialog_open() and app.state_name == "ACTIVE", "after the guard, clicks work")
    finally:
        close(app)


def test_busy_dialog_decisions() -> None:
    """Busy model: BusyDialog; now / cancel / idle decisions; live activity text."""
    app, det, ep = U.new_app()
    try:
        serving(app, ep)
        check(go_busy(app, "generating"), "the app sees the model generating")
        dlg = press_busy_toggle(app, ep)
        if dlg is None:
            return
        # ---- the dialog itself
        pump(app, 0.3)
        scale = ctk.ScalingTracker.get_window_scaling(dlg)
        w, h = dlg.winfo_width(), dlg.winfo_height()
        check(abs(w - BusyDialog.WIDTH * scale) <= 2 and abs(h - BusyDialog.HEIGHT * scale) <= 2,
              f"BusyDialog size {w}x{h} != {BusyDialog.WIDTH}x{BusyDialog.HEIGHT}*{scale}")
        check(dlg.grab_status() == "local", f"BusyDialog grab {dlg.grab_status()}")
        check(dlg.activity.cget("text").startswith("Writing a reply") and "412 tokens" in dlg.activity.cget("text"),
              f"activity line describes the reply ({dlg.activity.cget('text')!r})")
        check(dlg.activity.cget("text") == describe(app.metrics.activity()), "activity line == describe(activity())")
        texts = (dlg.idle_btn.cget("text"), dlg.now_btn.cget("text"), dlg.cancel_btn.cget("text"))
        check(texts == ("Turn off when it's done", "Turn off now", "Keep running"), f"button texts {texts}")
        dlg.update_idletasks()
        bottom, right = dlg.winfo_rooty() + h, dlg.winfo_rootx() + w
        for b, nm in ((dlg.idle_btn, "when-done"), (dlg.now_btn, "now"), (dlg.cancel_btn, "keep")):
            check(b.winfo_rooty() + b.winfo_height() <= bottom - 6 * scale and b.winfo_rootx() + b.winfo_width()
                  <= right - 6 * scale and b.winfo_reqwidth() <= b.winfo_width() + 1,
                  f"BusyDialog button '{nm}' inside the dialog, text fits")
        free = bottom - (dlg.cancel_btn.winfo_rooty() + dlg.cancel_btn.winfo_height())
        info(f"BusyDialog: {free / scale:.0f} logical px below the buttons")
        U.shot(dlg, "busy_dialog")
        # ---- metrics updates reach the open dialog
        app._fetch_metrics.live = "reading"
        ok = pump_until(app, lambda: dlg.winfo_exists() and dlg.activity.cget("text").startswith("Reading a prompt"), 4)
        check(ok, f"open dialog follows /metrics (update_activity): {dlg.activity.cget('text') if dlg.winfo_exists() else 'closed'!r}")
        # ---- cancel
        n = len(ep.stop_calls)
        dlg.cancel_btn.invoke()
        pump(app, 0.3)
        check(not app._dialog_open() and app.state_name == "ACTIVE" and len(ep.stop_calls) == n
              and pending(app) is None and app.toggle.cget("text") == "Turn off",
              f"cancel: stays ACTIVE, no stop, nothing pending ({app.state_name}, {pending(app)!r})")
        pump(app, GRACE + 3 * POLL)
        check(len(ep.stop_calls) == n and app.state_name == "ACTIVE", "cancel: nothing stops later either")
        # ---- close-X = cancel
        go_busy(app, "generating")
        dlg = press_busy_toggle(app, ep)
        if dlg is not None:
            dlg.tk.call(dlg.protocol("WM_DELETE_WINDOW"))
            pump(app, 0.2)
            check(not app._dialog_open() and app.state_name == "ACTIVE" and len(ep.stop_calls) == n
                  and pending(app) is None, "close-X = cancel")
        # ---- now -> forced stop
        dlg = press_busy_toggle(app, ep)
        if dlg is not None:
            dlg.now_btn.invoke()
            pump(app, 0.2)
            check(len(ep.stop_calls) == n + 1 and app.state_name == "STOPPING", f"now: stops ({app.state_name})")
            check(ep.stop_calls[n:] and ep.stop_calls[-1][1] is True, "now: turn_off(force=True) (force flag recorded)")
            check(not app._dialog_open() and pending(app) is None, "now: dialog closed, nothing pending")
            check(finish_stop(app, ep) and app.state_name == "IDLE", "forced stop -> IDLE")
            settle_reopen(app)
        # ---- idle (direct): the model finished while the dialog was open -> pending, a normal stop after the
        # idle grace (v1.7: never instantly - an agent may send its next request within seconds)
        serving(app, ep)
        go_busy(app, "generating")
        n = len(ep.stop_calls)
        dlg = press_busy_toggle(app, ep)
        if dlg is not None:
            app._fetch_metrics.live = "idle"
            dlg.update_activity(None)
            pump(app, 0.1)
            check(len(ep.stop_calls) == n and not app._dialog_open() and pending(app) is not None,
                  f"decision 'idle': pending, not instant ({app.state_name}, stops +{len(ep.stop_calls) - n})")
            pump_until(app, lambda: len(ep.stop_calls) > n, main.IDLE_GRACE_S + 4)
            check(len(ep.stop_calls) == n + 1 and app.state_name == "STOPPING",
                  f"decision 'idle': stops after the grace ({app.state_name}, stops +{len(ep.stop_calls) - n})")
            check(ep.stop_calls[n:] and ep.stop_calls[-1][1] is False, "decision 'idle': a normal (unforced) stop")
            if app.state_name == "STOPPING":
                finish_stop(app, ep)
                settle_reopen(app)
        # ---- idle (through /metrics): the app itself tells the dialog
        serving(app, ep)
        go_busy(app, "reading")
        n = len(ep.stop_calls)
        dlg = press_busy_toggle(app, ep)
        if dlg is not None:
            check(dlg.activity.cget("text").startswith("Reading a prompt"), f"reading text ({dlg.activity.cget('text')!r})")
            app._fetch_metrics.live = "idle"
            ok = pump_until(app, lambda: len(ep.stop_calls) > n, 4)
            check(ok and app.state_name == "STOPPING" and not app._dialog_open() and ep.stop_calls[-1][1] is False,
                  f"model went idle under the open dialog: dialog closes, normal stop ({app.state_name})")
            if app.state_name == "STOPPING":
                finish_stop(app, ep)
                settle_reopen(app)
    finally:
        close(app)


def pend_off(app, ep) -> bool:
    """ACTIVE + generating + 'Turn off when it's done'."""
    serving(app, ep)
    go_busy(app, "generating")
    dlg = press_busy_toggle(app, ep)
    if dlg is None:
        return False
    dlg.idle_btn.invoke()
    pump(app, 0.3)
    return True


def test_when_idle_line_and_cancel() -> None:
    app, det, ep = U.new_app()
    try:
        if not pend_off(app, ep):
            return
        n = len(ep.stop_calls)
        check(pending(app) == ("off", None), f"when_idle: _stop_pending == ('off', None) ({pending(app)!r})")
        check(app.state_name == "ACTIVE" and len(ep.stop_calls) == n and not app._dialog_open(),
              "when_idle: still ACTIVE, nothing stopped, dialog closed")
        check(app.toggle.cget("text") == "Stop now" and str(app.toggle.cget("state")) == "normal",
              f"when_idle: toggle says 'Stop now' ({app.toggle.cget('text')!r})")
        txt = app.preview.cget("text")
        check(U.detail_shown(app) and app._detail_link is True, f"pending line forced visible + a link "
              f"(shown={U.detail_shown(app)}, link={app._detail_link})")
        check("after this reply" in txt and "click to cancel" in txt, f"pending line text ({txt!r})")
        check(str(app.preview.cget("cursor")) == "hand2", "pending line has the hand cursor")
        scale = ctk.ScalingTracker.get_window_scaling(app)
        check(abs(app.winfo_height() - U.expected_h(app) * scale) <= 2,
              f"window grew for the detail line ({app.winfo_height()} vs {U.expected_h(app)}*{scale})")
        check(app.preview.winfo_reqwidth() <= app.winfo_width() - (main.DETAIL_INDENT + 8) * scale
              and "\n" not in txt, f"pending line fits on one line ({app.preview.winfo_reqwidth()} px, {txt!r})")
        check(pill(app)[0] == "Generating", f"status title still shows the model state ({pill(app)})")
        U.shot(app, "busy_pending_stop")
        # the line survives the status refreshes of the following polls; nothing stops while the model is busy
        p0 = app.metrics.polls
        pump(app, GRACE + 4 * POLL)
        txt2 = app.preview.cget("text")
        check(app.metrics.polls > p0 and "after this reply" in txt2 and "click to cancel" in txt2 and app._detail_link
              and app.toggle.cget("text") == "Stop now", f"pending line survives metrics refreshes ({txt2!r})")
        check(len(ep.stop_calls) == n and pending(app) == ("off", None) and app.state_name == "ACTIVE",
              f"busy for longer than the grace: still waiting ({app.state_name}, {pending(app)!r})")
        # other model states keep it too
        go_busy(app, "reading")
        pump(app, 2 * POLL)
        check("after this reply" in app.preview.cget("text") and app._detail_link,
              f"reading: pending line still there ({app.preview.cget('text')!r})")
        # ---- click the line = cancel
        app.preview._label.event_generate("<Button-1>")
        pump(app, 0.3)
        check(pending(app) is None, f"click cancels: _stop_pending None ({pending(app)!r})")
        check(app.toggle.cget("text") == "Turn off" and app.state_name == "ACTIVE" and len(ep.stop_calls) == n,
              f"click cancels: toggle back to 'Turn off', nothing stopped ({app.toggle.cget('text')!r})")
        check("after this reply" not in app.preview.cget("text") and not app._detail_link,
              f"click cancels: the line is gone ({app.preview.cget('text')!r})")
        info(f"right after the cancel click the status area says {status(app)!r}")
        go_model_idle(app)
        pump(app, GRACE + 4 * POLL)
        check(len(ep.stop_calls) == n and app.state_name == "ACTIVE", "cancelled: going idle later stops nothing")
        # the cancel shows a short note ("Keeps running"); once its hold time is over the detail closes again
        ok = pump_until(app, lambda: not U.detail_shown(app), main.NOTE_HOLD_S + 3)
        check(ok or app.detail_open, f"cancelled: detail line no longer forced open ({status(app)!r})")
        check(abs(app.winfo_height() - U.expected_h(app) * scale) <= 2, "cancelled: window back to its normal height")
    finally:
        close(app)


def test_when_idle_stop_now() -> None:
    """Pressing the toggle ('Stop now') while a stop is pending = force stop."""
    app, det, ep = U.new_app()
    try:
        if not pend_off(app, ep):
            return
        n = len(ep.stop_calls)
        app.on_toggle()
        pump(app, 0.2)
        check(not busy_dialog_open(app), "Stop now: no second BusyDialog")
        check(len(ep.stop_calls) == n + 1 and app.state_name == "STOPPING", f"Stop now: stops ({app.state_name})")
        check(ep.stop_calls[n:] and ep.stop_calls[-1][1] is True, "Stop now: forced")
        check(pending(app) is None, f"Stop now: pending cleared ({pending(app)!r})")
        check(app.toggle.cget("text") == "Stopping…", f"Stop now: toggle shows Stopping… ({app.toggle.cget('text')!r})")
        if app.state_name == "STOPPING":
            check(finish_stop(app, ep) and app.state_name == "IDLE" and app.toggle.cget("text") == "Turn on",
                  f"-> IDLE / Turn on ({app.state_name}, {app.toggle.cget('text')!r})")
            settle_reopen(app)
        pump(app, GRACE + 3 * POLL)
        check(len(ep.stop_calls) == n + 1, "no second stop after the forced one")
    finally:
        close(app)


def test_when_idle_grace() -> None:
    """The pending stop runs once the model has been idle for IDLE_GRACE_S; a new request resets the wait."""
    app, det, ep = U.new_app()
    try:
        if not pend_off(app, ep):
            return
        n = len(ep.stop_calls)
        # ---- idle shorter than the grace, then a new request: the wait starts over
        check(go_model_idle(app), "model idle seen")
        check(len(ep.stop_calls) == n and app.state_name == "ACTIVE", "idle just now: not stopped yet (grace)")
        pump(app, 0.2)
        check(go_busy(app, "reading"), "next request seen within the grace")
        check(len(ep.stop_calls) == n, "a request within the grace: not stopped")
        pump(app, GRACE + 4 * POLL)                 # an un-reset timer from the first idle would fire in here
        check(len(ep.stop_calls) == n and app.state_name == "ACTIVE" and pending(app) == ("off", None),
              f"new request resets the wait: still pending while it runs ({app.state_name}, {pending(app)!r})")
        check("after this reply" in app.preview.cget("text"), "line still shown after the reset")
        # ---- a "queued" model (idle + waiting requests) is not idle
        f = app._fetch_metrics
        f.live, f.queued = "idle", 2
        pump_until(app, lambda: (app.metrics.activity() or {}).get("kind") == "queued", 4)
        pump(app, GRACE + 3 * POLL)
        check(len(ep.stop_calls) == n and pending(app) == ("off", None), "queued requests: keeps waiting")
        # ---- really idle: stops after the grace, unforced
        go_model_idle(app)
        t0 = time.monotonic()
        ok = pump_until(app, lambda: len(ep.stop_calls) > n, GRACE + 3.0)
        dt = time.monotonic() - t0
        info(f"stop came {dt:.2f} s after the model was seen idle (grace {GRACE}, poll {POLL})")
        check(ok and app.state_name == "STOPPING", f"idle for the grace: stopped ({app.state_name}, {dt:.2f} s)")
        check(dt >= GRACE - POLL - 0.1, f"not before the grace ({dt:.2f} s < {GRACE} s)")
        check(dt <= GRACE + 4 * POLL + 0.5, f"not long after the grace ({dt:.2f} s)")
        check(ep.stop_calls[n:] and ep.stop_calls[-1][1] is False, "stop-when-idle is a normal (unforced) stop")
        check(pending(app) is None, f"pending cleared once it ran ({pending(app)!r})")
        check(len(ep.stop_calls) == n + 1, f"exactly one stop ({len(ep.stop_calls) - n})")
        if app.state_name == "STOPPING":
            check(finish_stop(app, ep) and app.state_name == "IDLE" and app.toggle.cget("text") == "Turn on",
                  f"-> IDLE / Turn on ({app.state_name})")
            settle_reopen(app)
            check("after this reply" not in status(app) and "Stopped" in status(app),
                  f"normal 'Stopped' note afterwards ({status(app)!r})")
    finally:
        close(app)


def test_when_idle_metrics_down() -> None:
    """Observation only: /metrics failing while a stop is pending (activity() is None then, but nothing was 'shown')."""
    app, det, ep = U.new_app()
    try:
        if not pend_off(app, ep):
            return
        n = len(ep.stop_calls)
        app._fetch_metrics.fail = True
        pump(app, GRACE + 6 * POLL)
        info(f"/metrics unreachable for {GRACE + 6 * POLL:.1f} s with a pending stop: "
             f"{'STOPPED (treated as idle)' if len(ep.stop_calls) > n else 'kept waiting'} "
             f"(state {app.state_name}, pending {pending(app)!r})")
    finally:
        close(app)


def test_pending_cleared_when_strata_gone() -> None:
    app, det, ep = U.new_app()
    try:
        if not pend_off(app, ep):
            return
        n = len(ep.stop_calls)
        ep.status_value = dict(IDLE_STATUS)
        app.on_status(ep.status())
        pump(app, 0.05)
        check(app.state_name == "ACTIVE" and pending(app) == ("off", None), "one stray idle reading changes nothing")
        app.on_status(ep.status())
        pump(app, 0.2)
        check(app.state_name == "IDLE", f"two idle readings -> IDLE ({app.state_name})")
        check(pending(app) is None, f"Strata gone: _stop_pending cleared ({pending(app)!r})")
        check(app.toggle.cget("text") == "Turn on" and str(app.toggle.cget("state")) == "normal",
              f"Strata gone: toggle 'Turn on' ({app.toggle.cget('text')!r})")
        check("after this reply" not in status(app), f"Strata gone: pending line removed ({status(app)!r})")
        pump(app, GRACE + 4 * POLL)
        check(len(ep.stop_calls) == n and app.state_name == "IDLE", "no stop call for a Strata that is already gone")
        # a new session starts clean
        serving(app, ep)
        check(pending(app) is None and app.toggle.cget("text") == "Turn off", "next ACTIVE: nothing pending")
        pump(app, GRACE + 3 * POLL)
        check(len(ep.stop_calls) == n and app.state_name == "ACTIVE", "next ACTIVE: idle model is not stopped")
    finally:
        close(app)


# ================================================================ 2) quick model switch
class Strata:
    """A fake Strata folder with run-a.bat / run-b.bat (+ run-broken.bat) and a v1.0-style config pointing at a."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-bs-strata-"))
        self.pa, self.pb, self.pz = U._free_port(), U._free_port(), U._free_port()
        self.sdir = U.make_strata(self.tmp, "Strata-A", {"a": self.pa, "b": self.pb})
        self.bat_a, self.bat_b = str(self.sdir / "run-a.bat"), str(self.sdir / "run-b.bat")
        self.bat_broken = str(self.sdir / "run-broken.bat")

    def cfg(self) -> dict:
        s, pa = self.sdir, self.pa
        cfg = dict(U.CFG, strata_dir=str(s), launcher_bat="run-a.bat", port=pa, endpoint_url=f"http://127.0.0.1:{pa}/v1",
                   model_name="model-a",
                   launch_cmd=[str(s / ".venv" / "Scripts" / "python.exe"), str(s / "serve" / "server.py"),
                               "--engine", "strata", "--config", str(s / "strata-a.json"), "--port", str(pa)])
        (U.HOME / "config.json").write_text(json.dumps(dict(cfg, user_whitelist=["keep.exe"])), encoding="utf-8")
        return cfg

    def app(self):
        return U.new_app(self.cfg())

    def on_a(self, app) -> bool:
        return same_path(launcher_bat.configured_bat(app.cfg), self.bat_a) and app.cfg.get("port") == self.pa \
            and app.cfg.get("model_name") == "model-a"

    def on_b(self, app) -> bool:
        return app.cfg.get("launcher_bat") == self.bat_b and app.cfg.get("port") == self.pb \
            and app.cfg.get("model_name") == "model-b" and app.cfg.get("endpoint_url") == f"http://127.0.0.1:{self.pb}/v1"


def switch_shown(app) -> bool:
    return app.switch_btn.winfo_manager() == "grid"


def test_switch_button(S: Strata) -> None:
    # ---- hidden: no Strata folder configured
    app, det, ep = U.new_app()
    try:
        check(hasattr(app, "switch_btn"), "app.switch_btn exists")
        check(not switch_shown(app) and not app.switch_btn.winfo_ismapped(), "no strata_dir: switch button hidden")
        check(app._launcher_choices() == [], f"no strata_dir: no choices ({app._launcher_choices()})")
    finally:
        close(app)
    gc.collect()
    # ---- hidden: a single launcher
    one = U.make_strata(S.tmp, "Strata-One", {"z": S.pz})
    cfg1 = dict(U.CFG, strata_dir=str(one), launcher_bat=str(one / "run-z.bat"), port=S.pz,
                endpoint_url=f"http://127.0.0.1:{S.pz}/v1", model_name="model-z",
                launch_cmd=[str(one / ".venv" / "Scripts" / "python.exe"), str(one / "serve" / "server.py"),
                            "--engine", "strata", "--config", str(one / "strata-z.json"), "--port", str(S.pz)])
    app, det, ep = U.new_app(cfg1)
    try:
        info(f"folder with ONE working launcher + run-broken.bat (2 run-*.bat files): switch button "
             f"{'shown' if switch_shown(app) else 'hidden'}; choices {app._launcher_choices()}")
    finally:
        close(app)
    gc.collect()
    (one / "run-broken.bat").unlink()
    app, det, ep = U.new_app(cfg1)
    try:
        check(not switch_shown(app), "one run-*.bat: switch button hidden")
        w0 = app.summary.winfo_rootx(), app.gear.winfo_rootx()
        info(f"header without the button: summary x={w0[0]}, gear x={w0[1]}")
    finally:
        close(app)
    gc.collect()
    # ---- shown: two launchers
    app, det, ep = S.app()
    try:
        pump(app, 0.3)
        check(switch_shown(app) and app.switch_btn.winfo_ismapped(), ">= 2 run-*.bat: switch button shown")
        check(str(app.switch_btn.cget("state")) == "normal", "IDLE: switch button enabled")
        check(callable(getattr(app, "open_model_menu", None)), "app.open_model_menu exists")
        app.update_idletasks()
        sb, g = app.switch_btn, app.gear
        check(sb.winfo_rootx() + sb.winfo_width() <= g.winfo_rootx() + 1 and sb.winfo_rootx() > app.pill.winfo_rootx(),
              f"switch button left of the gear (switch {sb.winfo_rootx()}+{sb.winfo_width()}, gear {g.winfo_rootx()})")
        check(abs((sb.winfo_rooty() + sb.winfo_height() / 2) - (g.winfo_rooty() + g.winfo_height() / 2)) <= 3,
              "switch button vertically aligned with the gear")
        check(sb.winfo_width() <= g.winfo_width() + 6 and sb.winfo_height() <= g.winfo_height() + 2,
              f"a small button ({sb.winfo_width()}x{sb.winfo_height()} vs gear {g.winfo_width()}x{g.winfo_height()})")
        right = app.winfo_rootx() + app.winfo_width()
        check(g.winfo_rootx() + g.winfo_width() <= right, "gear still inside the window")
        pump_until(app, lambda: "to close" in status(app), 3)
        for wdg, nm in ((app.summary, "summary"), (app.caret, "caret"), (app.pill, "state text")):
            check(wdg.winfo_rootx() + wdg.winfo_width() <= sb.winfo_rootx() + 1,
                  f"{nm} does not run into the switch button ({wdg.winfo_rootx() + wdg.winfo_width()} vs {sb.winfo_rootx()})")
        scale = ctk.ScalingTracker.get_window_scaling(app)
        check(abs(app.winfo_height() - U.expected_h(app) * scale) <= 2 and abs(app.winfo_width() - 360 * scale) <= 2,
              f"window size unchanged by the button ({app.winfo_width()}x{app.winfo_height()})")
        U.shot(app, "busy_switch_header_idle")

        # ---- choices
        ch = app._launcher_choices()
        info(f"_launcher_choices: {ch}")
        check(isinstance(ch, list) and all(isinstance(c, tuple) and len(c) == 2 for c in ch), "choices = [(label, path)]")
        by = {os.path.basename(p).lower(): (lbl, p) for lbl, p in ch}
        check("run-a.bat" in by and "run-b.bat" in by, f"both launchers listed ({list(by)})")
        if "run-a.bat" in by and "run-b.bat" in by:
            la, lb = by["run-a.bat"][0], by["run-b.bat"][0]
            check(same_path(by["run-a.bat"][1], S.bat_a) and by["run-b.bat"][1] == S.bat_b, "choices carry the .bat paths")
            check(la.startswith("model-a") and la.endswith(" ✓"), f"current launcher: model name … ✓ ({la!r})")
            check(lb.startswith("model-b") and not lb.endswith("✓"), f"other launcher: model name, no tick ({lb!r})")
        check(len(ch) == 2 and "run-broken.bat" not in by,
              f"one choice per WORKING run-*.bat; a broken one is left out ({len(ch)} choices: {list(by)})")

        # ---- disabled while STARTING / STOPPING
        app.on_toggle()
        check(app.state_name == "STARTING" and str(app.switch_btn.cget("state")) == "disabled",
              f"STARTING: switch button disabled ({app.switch_btn.cget('state')})")
        n0, s0 = len(ep.stop_calls), len(ep.start_calls)
        app.switch_model(S.bat_b)
        pump(app, 0.3)
        check(S.on_a(app) and app.state_name == "STARTING" and len(ep.stop_calls) == n0,
              "switch_model while STARTING is ignored")
        if pump_until(app, lambda: isinstance(app.dialog, SweepDialog), 3):
            app.dialog._decide("cancel")
        pump_until(app, lambda: app.state_name == "IDLE", 3)
        check(str(app.switch_btn.cget("state")) == "normal", "back to IDLE: enabled again")
        info(f"after the ignored switch: disk launcher {disk_config().get('launcher_bat')!r}, "
             f"pending {getattr(app._pending_launcher, 'bat_name', None)!r}")
        serving(app, ep, "model-a")
        check(str(app.switch_btn.cget("state")) == "normal" and switch_shown(app), "ACTIVE: enabled")
        go_busy(app, "generating")
        pump(app, 0.3)
        check(app.summary.winfo_rootx() + app.summary.winfo_width() <= app.switch_btn.winfo_rootx() + 1
              and app.caret.winfo_rootx() + app.caret.winfo_width() <= app.switch_btn.winfo_rootx() + 1,
              f"long 'generating' summary is cut before the switch button ({app.summary.cget('text')!r})")
        U.shot(app, "busy_switch_header")
        go_model_idle(app)
        app.turn_off()
        check(app.state_name == "STOPPING" and str(app.switch_btn.cget("state")) == "disabled",
              f"STOPPING: switch button disabled ({app.switch_btn.cget('state')})")
        finish_stop(app, ep)
        settle_reopen(app)
        check(app.state_name == "IDLE" and str(app.switch_btn.cget("state")) == "normal", "IDLE again: enabled")
    finally:
        close(app)


def test_switch_idle(S: Strata) -> None:
    app, det, ep = S.app()
    try:
        pump_until(app, lambda: "to close" in status(app), 3)
        base = dict(app.cfg)
        disk0 = disk_config()
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        # ---- same launcher: nothing
        before = status(app)
        app.switch_model(S.bat_a)
        pump(app, 0.5)
        check(app.cfg == base and disk_config() == disk0 and app._pending_launcher is None and app.state_name == "IDLE"
              and len(ep.start_calls) == n_start and not app._dialog_open(), "same launcher: no-op")
        check(status(app) == before, f"same launcher: no note either ({status(app)!r})")
        # ---- unparseable .bat: a note, nothing else
        try:
            launcher_bat.parse_bat(S.bat_broken)
            err = ""
        except launcher_bat.LauncherError as exc:
            err = str(exc)
        app.switch_model(S.bat_broken)
        pump_until(app, lambda: status(app) != before, 2)
        txt = status(app)
        info(f"broken launcher note: {txt!r}   (parser says {err!r})")
        check(txt != before and ("run-broken" in txt or "does not start" in txt or (err and err[:20] in txt)),
              f"unparseable .bat: note mentions the problem ({txt!r})")
        check(app.cfg == base and disk_config() == disk0 and app._pending_launcher is None and app.state_name == "IDLE"
              and len(ep.start_calls) == n_start and len(ep.stop_calls) == n_stop,
              "unparseable .bat: nothing else changes")
        check(app.preview.winfo_reqwidth() <= app.winfo_width() and U.detail_shown(app) or "\n" not in txt,
              "broken-launcher note is visible")
        # ---- other launcher while IDLE: saved + applied, no start
        app.switch_model(S.bat_b)
        ok = pump_until(app, lambda: app.cfg.get("launcher_bat") == S.bat_b, 4)
        pump(app, 0.3)
        check(ok and S.on_b(app), f"IDLE: launcher applied (launcher_bat={app.cfg.get('launcher_bat')!r}, "
                                  f"model={app.cfg.get('model_name')!r}, port={app.cfg.get('port')})")
        d = disk_config()
        check(d.get("launcher_bat") == S.bat_b and d.get("port") == S.pb and d.get("model_name") == "model-b",
              f"IDLE: saved to config.json ({d.get('launcher_bat')!r})")
        check(d.get("user_whitelist") == ["keep.exe"], "saving keeps the rest of config.json")
        check(app.state_name == "IDLE" and len(ep.start_calls) == n_start and len(ep.stop_calls) == n_stop
              and app._pending_launcher is None and not app._dialog_open(), f"IDLE: no start, nothing pending ({app.state_name})")
        info(f"note after the idle switch: {status(app)!r}")
        check("model-b" in status(app) or "run-b" in status(app), f"IDLE: a note names the new model ({status(app)!r})")
        by = {os.path.basename(p).lower(): lbl for lbl, p in app._launcher_choices()}
        check(by.get("run-b.bat", "").endswith(" ✓") and not by.get("run-a.bat", "✓").endswith("✓"),
              f"the tick moved to the new launcher ({by})")
        check(switch_shown(app) and str(app.switch_btn.cget("state")) == "normal", "switch button still there")
    finally:
        close(app)


def watch_until(app, cond, timeout: float = 4.0) -> tuple[bool, bool]:
    """pump_until + whether app._reopening was ever True meanwhile."""
    seen = [False]

    def c() -> bool:
        seen[0] = seen[0] or bool(app._reopening)
        return cond()
    ok = pump_until(app, c, timeout)
    end = time.monotonic() + 0.4
    while time.monotonic() < end and not app._destroyed:
        app.update()
        seen[0] = seen[0] or bool(app._reopening)
        time.sleep(0.01)
    return ok, seen[0]


def reopen_record() -> dict:
    py = os.path.join(sys.base_prefix, "python.exe")
    return {"name": "notes.exe", "exe": py, "cmdline": [py], "cwd": sys.base_prefix, "category": "app",
            "create_time": 1.0, "elevated": False, "aumid": None}


def test_switch_active_idle(S: Strata) -> None:
    app, det, ep = S.app()
    try:
        serving(app, ep, "model-a")
        check(app.state_name == "ACTIVE" and app.metrics.activity() is None, "ACTIVE on model-a, idle")
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        U.LAUNCHED.clear()
        main._save_reopen_records([reopen_record()])            # an app swept at the last start
        app.switch_model(S.bat_b)
        ok = pump_until(app, lambda: len(ep.stop_calls) > n_stop, 4)
        check(ok and app.state_name == "STOPPING" and not busy_dialog_open(app),
              f"ACTIVE + idle: switch stops Strata without asking ({app.state_name})")
        check(ep.stop_calls[n_stop:] and ep.stop_calls[-1][1] is False, "switch stop is unforced")
        check(len(ep.stop_calls) == n_stop + 1, f"one stop call ({len(ep.stop_calls) - n_stop})")
        check(S.on_a(app), "while stopping the running config (port) is untouched - OFF must find the old server")
        check(getattr(app._pending_launcher, "bat_path", None) == S.bat_b, "new launcher held as pending")
        check(disk_config().get("launcher_bat") == S.bat_b, f"saved to config.json ({disk_config().get('launcher_bat')!r})")
        check(len(ep.start_calls) == n_start, "no start before the stop finished")
        if not ok:
            return
        # ---- stop result -> no reopen, new launcher applied, turn_on runs by itself
        ep.status_value = dict(IDLE_STATUS)
        fire(ep.stop_calls[-1][0], stop_result())
        ok, reopened = watch_until(app, lambda: isinstance(app.dialog, SweepDialog) or len(ep.start_calls) > n_start, 5)
        check(ok and app.state_name == "STARTING", f"after the stop turn_on runs automatically ({app.state_name}, "
                                                   f"{status(app)!r})")
        check(not reopened and not app._reopening and U.LAUNCHED == [],
              f"the swept-app reopen does not run between stop and restart (_reopening seen={reopened}, {U.LAUNCHED})")
        check(S.on_b(app) and app._pending_launcher is None, f"new launcher applied before the start "
              f"({app.cfg.get('launcher_bat')!r}, port {app.cfg.get('port')})")
        check(len(reopen.load_pending()) == 1, f"the swept apps stay pending for the real Turn off "
                                               f"({len(reopen.load_pending())})")
        check(app.toggle.cget("text") == "Cancel" and str(app.switch_btn.cget("state")) == "disabled",
              "STARTING: Cancel + switch button disabled")
        if isinstance(app.dialog, SweepDialog):
            app.dialog._decide("start_only")
        ok = pump_until(app, lambda: len(ep.start_calls) == n_start + 1, 4)
        check(ok, f"start requested once ({len(ep.start_calls) - n_start})")
        if ok:
            ep.status_value = active_status(True, "model-b")
            fire(ep.start_calls[-1][0])
            ok = pump_until(app, lambda: app.state_name == "ACTIVE", 4)
            check(ok and S.on_b(app), f"ready -> ACTIVE on model-b ({app.state_name})")
            check(pump_until(app, lambda: "model-b" in status(app), 3), f"status shows the new model ({status(app)!r})")
            # ---- a normal Turn off afterwards reopens as usual (the switch flag must not stick)
            go_model_idle(app)
            n = len(ep.stop_calls)
            app.on_toggle()
            check(len(ep.stop_calls) == n + 1 and app.state_name == "STOPPING", "Turn off after a switch")
            s2 = len(ep.start_calls)
            ep.status_value = dict(IDLE_STATUS)
            fire(ep.stop_calls[-1][0], stop_result())
            ok = pump_until(app, lambda: "notes.exe" in U.LAUNCHED, 5)
            check(ok, f"the later Turn off reopens the swept app ({U.LAUNCHED})")
            settle_reopen(app)
            pump(app, 0.5)
            check(app.state_name == "IDLE" and len(ep.start_calls) == s2, f"and does NOT restart ({app.state_name})")

        # ---- switch whose stop fails: ERROR, no automatic start, config untouched
        reopen.clear_pending()
        app._set_state("IDLE")
        serving(app, ep, "model-b")
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        app.switch_model(S.bat_a)
        if pump_until(app, lambda: len(ep.stop_calls) > n_stop, 4):
            fire(ep.stop_calls[-1][0], stop_result(ok=False, port_released=False, error="port still in use"))
            pump_until(app, lambda: app.state_name == "ERROR", 3)
            pump(app, 0.5)
            check(app.state_name == "ERROR" and len(ep.start_calls) == n_start and not app._dialog_open(),
                  f"failed stop: ERROR, no automatic start ({app.state_name})")
            # the stuck "switch" intent must not start Strata by itself after a later, unrelated stop
            app._error_hold_until = 0.0
            ep.status_value = active_status(True, "model-b")
            app.on_status(ep.status())
            pump(app, 0.1)
            if app.state_name == "ACTIVE":
                go_model_idle(app)
                app.on_toggle()
                if app.state_name == "STOPPING":
                    finish_stop(app, ep)
                    pump(app, 0.8)
                    settle_reopen(app)
                    check(app.state_name == "IDLE" and len(ep.start_calls) == n_start and not app._dialog_open(),
                          f"a plain Turn off after a FAILED switch does not auto-start ({app.state_name}, "
                          f"starts +{len(ep.start_calls) - n_start})")
        else:
            check(False, "second switch (b -> a) did not stop Strata")
    finally:
        reopen.clear_pending()
        close(app)


def test_switch_cancelled_start(S: Strata) -> None:
    """Cancel in the sweep dialog of the automatic restart: IDLE on the new launcher, no loops."""
    app, det, ep = S.app()
    try:
        serving(app, ep, "model-a")
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        app.switch_model(S.bat_b)
        if not pump_until(app, lambda: len(ep.stop_calls) > n_stop, 4):
            check(False, "switch did not stop Strata")
            return
        ep.status_value = dict(IDLE_STATUS)
        fire(ep.stop_calls[-1][0], stop_result())
        ok = pump_until(app, lambda: isinstance(app.dialog, SweepDialog), 5)
        check(ok, f"StubDetector candidates: the SweepDialog follows the switch ({type(app.dialog).__name__})")
        check(S.on_b(app), "config switched when the dialog shows")
        if ok:
            app.dialog._decide("cancel")
            pump(app, 0.6)
            check(app.state_name == "IDLE" and len(ep.start_calls) == n_start and S.on_b(app) and not app._dialog_open(),
                  f"cancelled restart: IDLE, new launcher stays selected ({app.state_name})")
            check(app.toggle.cget("text") == "Turn on", "toggle 'Turn on'")
    finally:
        close(app)


def test_switch_stop_refused(S: Strata) -> None:
    """stop_endpoint() returns False during a switch (reconcile path): the 'restart after stop' intent must not
    survive into a later, plain Turn off."""
    app, det, ep = S.app()
    try:
        serving(app, ep, "model-a")
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        ep.stop_return = False
        app.switch_model(S.bat_b)
        ok = pump_until(app, lambda: len(ep.stop_calls) > n_stop, 4)
        pump(app, 0.8)
        ep.stop_return = True
        info(f"switch with a refused stop: state {app.state_name}, launcher "
             f"{os.path.basename(str(app.cfg.get('launcher_bat')))}, starts +{len(ep.start_calls) - n_start}, "
             f"status {status(app)!r}")
        check(ok and len(ep.start_calls) == n_start and not isinstance(app.dialog, SweepDialog),
              f"refused stop: no automatic start ({app.state_name})")
        if app._dialog_open():
            app.dialog._decide("cancel")
            pump(app, 0.3)
        if app.state_name != "ACTIVE":
            serving(app, ep, "model-a")
        if app.state_name != "ACTIVE":
            info(f"could not get back to ACTIVE ({app.state_name}); skipping the follow-up")
            return
        go_model_idle(app)
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        app.on_toggle()                                        # a plain Turn off
        pump(app, 0.2)
        if len(ep.stop_calls) == n_stop + 1 and app.state_name == "STOPPING":
            finish_stop(app, ep)
            pump(app, 1.0)
            settle_reopen(app)
            check(app.state_name == "IDLE" and len(ep.start_calls) == n_start and not isinstance(app.dialog, SweepDialog),
                  f"plain Turn off after a refused switch-stop stays off (state {app.state_name}, dialog "
                  f"{type(app.dialog).__name__}, starts +{len(ep.start_calls) - n_start})")
        else:
            check(False, f"plain Turn off did not stop ({app.state_name})")
    finally:
        ep.stop_return = True
        close(app)


def busy_switch(app, ep, S: Strata, target: str, model: str) -> BusyDialog | None:
    n = len(ep.stop_calls)
    app.switch_model(target)
    pump_until(app, lambda: busy_dialog_open(app), 3)
    pump(app, 0.5)                               # the dialog ignores clicks for its first 0.4 s (double-click guard)
    ok = busy_dialog_open(app)
    check(ok, f"ACTIVE + busy: switch_model opens a BusyDialog ({type(app.dialog).__name__}, {app.state_name})")
    check(len(ep.stop_calls) == n and app.state_name == "ACTIVE", "busy switch: nothing stopped yet")
    return app.dialog if ok else None


def test_switch_busy(S: Strata) -> None:
    app, det, ep = S.app()
    try:
        det.cands = []                                         # restart without the sweep dialog
        serving(app, ep, "model-a")
        check(go_busy(app, "generating"), "model busy")
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        disk0 = disk_config()
        # ---- the dialog
        dlg = busy_switch(app, ep, S, S.bat_b, "model-b")
        if dlg is None:
            return
        pump(app, 0.3)
        texts = (dlg.idle_btn.cget("text"), dlg.now_btn.cget("text"), dlg.cancel_btn.cget("text"))
        check(texts == ("Switch when it's done", "Switch now", "Keep running"), f"switch wording on the buttons {texts}")
        labels = [w.cget("text") for w in dlg.winfo_children() if isinstance(w, ctk.CTkLabel)]
        check(any("model-b" in t for t in labels), f"BusyDialog(switch_to='model-b') names the model ({labels})")
        scale = ctk.ScalingTracker.get_window_scaling(dlg)
        bottom = dlg.winfo_rooty() + dlg.winfo_height()
        check(dlg.cancel_btn.winfo_rooty() + dlg.cancel_btn.winfo_height() <= bottom - 6 * scale,
              "switch dialog: buttons not clipped (2-line question)")
        U.shot(dlg, "busy_switch_dialog")
        # ---- cancel: nothing changes
        dlg.cancel_btn.invoke()
        pump(app, 0.5)
        check(app.state_name == "ACTIVE" and len(ep.stop_calls) == n_stop and pending(app) is None and S.on_a(app),
              f"cancel: still serving model-a ({app.state_name}, {pending(app)!r})")
        check(app._pending_launcher is None and disk_config().get("launcher_bat") == disk0.get("launcher_bat"),
              f"cancel: no launcher left saved/pending for the next start (inferred from 'cancel'; pending="
              f"{getattr(app._pending_launcher, 'bat_name', None)!r}, disk={disk_config().get('launcher_bat')!r})")
        check("applies next start" not in status(app), f"cancel: no 'applies next start' line ({status(app)!r})")

        # ---- when_idle, then cancel through the link
        dlg = busy_switch(app, ep, S, S.bat_b, "model-b")
        if dlg is None:
            return
        dlg.idle_btn.invoke()
        pump(app, 0.4)
        p = pending(app)
        check(isinstance(p, tuple) and len(p) == 2 and p[0] == "switch"
              and isinstance(p[1], launcher_bat.LauncherInfo) and p[1].bat_path == S.bat_b and p[1].model_name == "model-b",
              f"when_idle: _stop_pending == ('switch', LauncherInfo run-b) ({p!r})")
        txt = app.preview.cget("text")
        check("Switches model" in txt and "click to cancel" in txt and U.detail_shown(app) and app._detail_link is True,
              f"pending switch line: forced, link, text ({txt!r}, link={app._detail_link})")
        check(app.preview.winfo_reqheight() <= 18 * ctk.ScalingTracker.get_widget_scaling(app),
              f"pending switch line is one line ({app.preview.winfo_reqheight()})")
        scale = ctk.ScalingTracker.get_window_scaling(app)
        check(app.preview.winfo_reqwidth() <= app.winfo_width() - (main.DETAIL_INDENT + 8) * scale and "\n" not in txt,
              f"pending switch line fits on one line ({app.preview.winfo_reqwidth()} px of {app.winfo_width()})")
        check(app.state_name == "ACTIVE" and len(ep.stop_calls) == n_stop and S.on_a(app), "when_idle: still serving a")
        info(f"toggle while a switch is pending: {app.toggle.cget('text')!r}; switch button "
             f"{app.switch_btn.cget('state')}")
        U.shot(app, "busy_pending_switch")
        pump(app, GRACE + 4 * POLL)
        check(len(ep.stop_calls) == n_stop and "Switches model" in app.preview.cget("text"),
              f"busy longer than the grace: still waiting, line kept ({app.preview.cget('text')!r})")
        app.preview._label.event_generate("<Button-1>")
        pump(app, 0.4)
        check(pending(app) is None and app.state_name == "ACTIVE" and len(ep.stop_calls) == n_stop and S.on_a(app)
              and "Switches model" not in app.preview.cget("text") and app.toggle.cget("text") == "Turn off",
              f"click cancels the pending switch ({pending(app)!r}, {app.preview.cget('text')!r})")
        check(app._pending_launcher is None and disk_config().get("launcher_bat") == disk0.get("launcher_bat"),
              f"cancelled switch leaves no launcher saved/pending (inferred; pending="
              f"{getattr(app._pending_launcher, 'bat_name', None)!r}, disk={disk_config().get('launcher_bat')!r})")

        # ---- when_idle, executes after the grace
        dlg = busy_switch(app, ep, S, S.bat_b, "model-b")
        if dlg is None:
            return
        dlg.idle_btn.invoke()
        pump(app, 0.3)
        go_model_idle(app)
        t0 = time.monotonic()
        check(len(ep.stop_calls) == n_stop, "idle just now: the switch waits for the grace")
        ok = pump_until(app, lambda: len(ep.stop_calls) > n_stop, GRACE + 3.0)
        dt = time.monotonic() - t0
        info(f"pending switch executed {dt:.2f} s after idle")
        check(ok and dt >= GRACE - POLL - 0.1 and ep.stop_calls[-1][1] is False,
              f"pending switch: unforced stop after the grace ({dt:.2f} s, ok={ok})")
        check(pending(app) is None, f"pending cleared when it ran ({pending(app)!r})")
        if ok:
            ep.status_value = dict(IDLE_STATUS)
            fire(ep.stop_calls[-1][0], stop_result())
            ok2, reopened = watch_until(app, lambda: len(ep.start_calls) > n_start, 5)
            check(ok2 and S.on_b(app) and app.state_name == "STARTING" and not reopened,
                  f"pending switch: restart on model-b, no reopen ({app.state_name}, {app.cfg.get('launcher_bat')!r}, "
                  f"reopen={reopened})")
            check(disk_config().get("launcher_bat") == S.bat_b, "pending switch: saved to config.json")
            if ok2:
                ep.status_value = active_status(True, "model-b")
                fire(ep.start_calls[-1][0])
                check(pump_until(app, lambda: app.state_name == "ACTIVE", 4), "ACTIVE on model-b")

        # ---- "Switch now": forced stop, then the restart on the other launcher
        if app.state_name != "ACTIVE" or not S.on_b(app):
            return
        pump_until(app, lambda: app.metrics.polls >= 1, 3)
        check(go_busy(app, "reading"), "model busy again (reading)")
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        dlg = busy_switch(app, ep, S, S.bat_a, "model-a")
        if dlg is None:
            return
        dlg.now_btn.invoke()
        ok = pump_until(app, lambda: len(ep.stop_calls) > n_stop, 3)
        check(ok and ep.stop_calls[-1][1] is True and app.state_name == "STOPPING",
              f"Switch now: forced stop ({app.state_name})")
        if ok:
            ep.status_value = dict(IDLE_STATUS)
            fire(ep.stop_calls[-1][0], stop_result())
            ok2, reopened = watch_until(app, lambda: len(ep.start_calls) > n_start, 5)
            check(ok2 and S.on_a(app) and app.cfg.get("launcher_bat") == S.bat_a and not reopened,
                  f"Switch now: restart on model-a ({app.state_name}, {app.cfg.get('launcher_bat')!r})")
            if ok2:
                ep.status_value = active_status(True, "model-a")
                fire(ep.start_calls[-1][0])
                pump_until(app, lambda: app.state_name == "ACTIVE", 4)

        # ---- pending switch + the toggle ("Stop now"): what does it do?  (contract: toggle while pending = force stop)
        if app.state_name == "ACTIVE":
            pump_until(app, lambda: app.metrics.polls >= 1, 3)
            go_busy(app, "generating")
            n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
            dlg = busy_switch(app, ep, S, S.bat_b, "model-b")
            if dlg is not None:
                dlg.idle_btn.invoke()
                pump(app, 0.3)
                app.on_toggle()
                pump(app, 0.3)
                check(len(ep.stop_calls) == n_stop + 1 and ep.stop_calls[-1][1] is True and pending(app) is None,
                      f"toggle while a switch is pending = force stop now ({app.state_name}, {pending(app)!r})")
                if app.state_name == "STOPPING":
                    ep.status_value = dict(IDLE_STATUS)
                    fire(ep.stop_calls[-1][0], stop_result())
                    pump_until(app, lambda: app.state_name != "STOPPING", 3)
                    pump(app, 0.8)
                    info(f"after 'Stop now' with a pending switch: state {app.state_name}, launcher "
                         f"{os.path.basename(str(app.cfg.get('launcher_bat')))}, starts +{len(ep.start_calls) - n_start}")
    finally:
        close(app)


def test_switch_pending_then_strata_gone(S: Strata) -> None:
    app, det, ep = S.app()
    try:
        det.cands = []
        serving(app, ep, "model-a")
        go_busy(app, "generating")
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        dlg = busy_switch(app, ep, S, S.bat_b, "model-b")
        if dlg is None:
            return
        dlg.idle_btn.invoke()
        pump(app, 0.3)
        ep.status_value = dict(IDLE_STATUS)
        app.on_status(ep.status()); app.on_status(ep.status())
        pump(app, GRACE + 4 * POLL)
        check(pending(app) is None, f"Strata gone: pending switch cleared ({pending(app)!r})")
        check(app.state_name == "IDLE" and len(ep.stop_calls) == n_stop and len(ep.start_calls) == n_start,
              f"Strata gone: no stop, no automatic start ({app.state_name}, starts +{len(ep.start_calls) - n_start})")
        check("Switches model" not in status(app), f"line removed ({status(app)!r})")
        info(f"launcher after 'Strata gone' with a pending switch: {os.path.basename(str(app.cfg.get('launcher_bat')))}")
    finally:
        close(app)


# ================================================================ runner
def run() -> int:
    main.TOGGLE_DEBOUNCE_S = 0.0
    check(hasattr(main, "IDLE_GRACE_S"), "main.IDLE_GRACE_S exists")
    old = (getattr(main, "IDLE_GRACE_S", None), main.LIVE_POLL_S)
    info(f"main.IDLE_GRACE_S default = {old[0]!r}, LIVE_POLL_S = {old[1]!r}")
    main.IDLE_GRACE_S, main.LIVE_POLL_S = GRACE, POLL
    dialogs = U.Dialogs()
    dialogs.install()
    S = Strata()
    try:
        for fn, args in ((test_toggle_model_idle, ()), (test_double_click_guard, ()), (test_busy_dialog_decisions, ()),
                         (test_when_idle_line_and_cancel, ()), (test_when_idle_stop_now, ()),
                         (test_when_idle_grace, ()), (test_when_idle_metrics_down, ()),
                         (test_pending_cleared_when_strata_gone, ()),
                         (test_switch_button, (S,)), (test_switch_idle, (S,)), (test_switch_active_idle, (S,)),
                         (test_switch_cancelled_start, (S,)), (test_switch_stop_refused, (S,)),
                         (test_switch_busy, (S,)),
                         (test_switch_pending_then_strata_gone, (S,))):
            gc.collect()                         # Tk objects of the previous app die on the Tk thread
            print(f"-- {fn.__name__}")
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
                print(f"FAIL: {FAILS[-1]}")
        check(dialogs.asked == [], f"no blocking messagebox was needed ({dialogs.asked})")
        check(U.OPENED_URLS == [], "nothing opened in a browser")
    finally:
        dialogs.restore()
        if old[0] is None:
            if hasattr(main, "IDLE_GRACE_S"):
                del main.IDLE_GRACE_S
        else:
            main.IDLE_GRACE_S = old[0]
        main.LIVE_POLL_S = old[1]
        reopen.clear_pending()
        shutil.rmtree(S.tmp, ignore_errors=True)
        shutil.rmtree(U.HOME, ignore_errors=True)
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
