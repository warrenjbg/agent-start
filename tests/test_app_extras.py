"""Integration of the v1.7 extras in the app window: tool-call-aware safe stop, system tray, update offer, first-start
Strata search, the 'reopen closed apps' switch in Settings. Stubs only: never contacts Strata or GitHub, never runs an
installer, never closes apps."""
from __future__ import annotations

import gc
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_ui_smoke as U  # noqa: E402  (first: redirects the data dir, fakes reopen.launch / webbrowser.open)
import config_store  # noqa: E402
import launcher_bat  # noqa: E402
import main  # noqa: E402
import reopen  # noqa: E402
import strata_finder  # noqa: E402
import updater  # noqa: E402
from busy_dialog import BusyDialog  # noqa: E402
from settings_dialog import SettingsDialog  # noqa: E402

PASSES, FAILS = 0, []


def check(cond, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


pump, pump_until, status = U.pump, U.pump_until, U.status


class PhaseMetrics:
    """FakeMetrics plus a settable generating phase (Strata's live.phase) and finished-request count (totals)."""

    def __init__(self, inner) -> None:
        self.inner, self.phase, self.done = inner, "thinking", 0
        self.calls = inner.calls

    def __call__(self, root, all_requests=False):
        doc = self.inner(root, all_requests)
        if doc:
            if doc["live"]["state"] == "generating":
                doc["live"]["phase"] = self.phase
            doc["totals"] = {"requests": self.done}
        return doc

    def __getattr__(self, k):
        return getattr(self.inner, k)


class FakeStatus:
    """GET /status: while idle Strata keeps the last request's final phase."""

    def __init__(self) -> None:
        self.doc: dict | None = None
        self.calls = 0

    def __call__(self, root):
        self.calls += 1
        return dict(self.doc) if self.doc is not None else None


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


def _setup_active(app, ep):
    pm = PhaseMetrics(app._fetch_metrics)
    fs = FakeStatus()
    app._fetch_metrics, app._fetch_status = pm, fs
    pump(app, 0.5)
    U.to_active(app, ep)
    pump(app, 0.6)
    return pm, fs


def _request(app, pm, fs, final_phase: str | None, tool: str | None = None, sampled: str = "thinking",
             gen_s: float = 0.7) -> None:
    """One request: generating (the poll sees `sampled`), then it ends; /status then reports final_phase."""
    pm.inner.live, pm.phase = "generating", sampled
    pump(app, gen_s)
    pm.done += 1
    fs.doc = {"busy": False, "phase": final_phase, "tool": tool} if final_phase is not None else None
    pm.inner.live = "idle"


# ------------------------------------------------------------------------------------------------ agent tool gap
def test_tool_gap() -> None:
    """The bug: 'turn off when done' stopped Strata while the agent ran web_search between two requests."""
    app, det, ep = U.new_app()
    try:
        pm, fs = _setup_active(app, ep)
        # the short tool-call phase is NOT seen by the 1 s sampling ('thinking' sampled): /status tells
        _request(app, pm, fs, "tool call complete", None, sampled="thinking")
        check(pump_until(app, lambda: app._tool_wait is not None, 2), f"tool-call ending found via /status ({app._tool_wait})")
        check(fs.calls >= 1, "/status asked after the request ended")
        n = len(ep.stop_calls)
        app.on_toggle()
        pump(app, 0.6)
        check(isinstance(app.dialog, BusyDialog) and len(ep.stop_calls) == n, "Turn off in the tool gap asks first")
        if isinstance(app.dialog, BusyDialog):
            txt = app.dialog.activity.cget("text")
            check(txt.startswith("Agent is running"), f"dialog: the agent is running a tool ({txt!r})")
            app.dialog.idle_btn.invoke()
            pump(app, 0.3)
        check(app._stop_pending == ("off", None), "stop pending")
        pump(app, main.IDLE_GRACE_S * 3 + 0.5)
        check(len(ep.stop_calls) == n and app.state_name == "ACTIVE", "waits through the tool gap (longer than the grace)")
        ptxt = app.pending_text.cget("text") + app.pending_cancel.cget("text")
        check(app._pending_shown and ptxt == "Turns off after the agent's reply · Cancel",
              f"pending line under the button ({ptxt!r})")
        U.shot(app, "extras_toolgap_pending")
        # the agent's next request is the answer
        _request(app, pm, fs, "answering", sampled="answering", gen_s=0.8)
        check(len(ep.stop_calls) == n, "not during the answer")
        check(pump_until(app, lambda: len(ep.stop_calls) > n, main.IDLE_GRACE_S + 3),
              "answer finished -> turns off after the grace")
        nt = getattr(ep, "next_toast", None)
        check(isinstance(nt, tuple) and "after the reply" in nt[1] and "{freed}" in nt[1],
              f"the stop toast says why ({nt})")
    finally:
        close(app)
    # named tool from /status, fallback sampling without /status, stale phase, expiry, dialog auto-idle
    app, det, ep = U.new_app()
    try:
        pm, fs = _setup_active(app, ep)
        _request(app, pm, fs, "writing a tool call: web_search", None)
        check(pump_until(app, lambda: app._tool_wait is not None, 2) and app._tool_wait[0] == "web_search",
              f"tool name from the /status phase ({app._tool_wait})")
        check(pump_until(app, lambda: status(app).startswith("Agent running web_search"), 2), f"status line ({status(app)!r})")
        # Strata with an API key: /status unavailable -> the sampled phase is used
        _request(app, pm, fs, None, sampled="writing a tool call: read_file")
        check(pump_until(app, lambda: app._tool_wait is not None and app._tool_wait[0] == "read_file", 2),
              f"fallback: sampled tool phase ({app._tool_wait})")
        # finding 2: request A sampled as a tool call, request B (an answer) never sampled while generating
        pm.inner.live = "reading"
        pump(app, 0.5)
        pm.done += 1
        fs.doc = None
        pm.inner.live = "idle"
        check(pump_until(app, lambda: app._was_busy is False, 2) and app._tool_wait is None,
              f"a new request resets the old tool phase ({app._tool_wait})")
        # expiry
        app._tool_wait = ("web_search", time.monotonic() - main.TOOL_WAIT_S - 1)
        app._track_request_end(None)
        check(app._tool_wait is None, "a tool wait expires after TOOL_WAIT_S")
        # the model goes idle while the busy dialog is open: no instant stop, the grace still applies
        pm.inner.live, pm.phase = "generating", "answering"
        pump(app, 0.6)
        n = len(ep.stop_calls)
        app.on_toggle()
        pump(app, 0.5)
        check(isinstance(app.dialog, BusyDialog), "busy: dialog")
        pm.done += 1
        fs.doc = {"busy": False, "phase": "answering"}
        pm.inner.live = "idle"
        check(pump_until(app, lambda: app.dialog is None and app._stop_pending is not None, 3),
              "idle while the dialog is open -> pending (not an instant stop)")
        check(len(ep.stop_calls) == n, "no instant stop")
        check(pump_until(app, lambda: len(ep.stop_calls) > n, main.IDLE_GRACE_S + 3), "then stops after the grace")
    finally:
        close(app)


# ------------------------------------------------------------------------------------------------ tray
class FakeTray:
    instances: list = []

    def __init__(self, title, icon_png, on_open, on_toggle, on_quit, class_name="AgentStartTray", on_lost=None,
                 on_switch=None, on_balloon_click=None):
        self.title, self.icon_png, self.on_lost = title, icon_png, on_lost
        self.on_switch, self.on_balloon_click = on_switch, on_balloon_click
        self.models: list = []
        self.on_open, self.on_toggle, self.on_quit = on_open, on_toggle, on_quit
        self.status: list = []
        self.notes: list = []
        self.is_running = False
        self.stopped = False
        FakeTray.instances.append(self)

    def start(self):
        self.is_running = True
        return True

    def start_async(self, on_result=None):
        import threading
        threading.Thread(target=lambda: on_result and on_result(self.start())).start()

    def set_status(self, led_hex, tooltip, toggle_label, toggle_enabled=True):
        self.status.append((led_hex, tooltip, toggle_label, toggle_enabled))

    def notify(self, title, message, key=None):
        self.notes.append((title, message, key))

    def set_models(self, items, current, enabled=True):
        self.models.append((items, current, enabled))

    def stop(self):
        self.stopped, self.is_running = True, False


def test_tray() -> None:
    import tray as tray_mod
    real = tray_mod.TrayIcon
    tray_mod.TrayIcon = FakeTray
    FakeTray.instances.clear()
    app, det, ep = U.new_app()
    try:
        app._no_tray = False
        app._start_tray()
        t = app.tray
        check(pump_until(app, lambda: t is not None and t.status, 3), "tray started off the Tk thread")
        check(isinstance(t, FakeTray) and t.is_running, "tray started")
        check(t and t.icon_png.endswith("app.png") and t.title == main.APP_TITLE, "tray: title + app icon")
        check(t and t.status and t.status[-1][2] == "Turn on" and t.status[-1][0] == main.T.LED["IDLE"],
              f"tray mirrors IDLE ({t.status[-1] if t and t.status else None})")
        # X hides to the tray, first time with a hint
        app.on_close_request()
        pump(app, 0.3)
        check(app.state() == "withdrawn" and not app._destroyed, f"X hides the window ({app.state()})")
        check(len(t.notes) == 1 and "tray" in t.notes[0][1].lower(), f"first hide: a hint balloon ({t.notes})")
        check(pump_until(app, lambda: config_store.load_state().get("tray_hint_shown") is True, 3), "hint shown once (saved)")
        app.on_close_request()
        check(len(t.notes) == 1, "no second hint")
        # tray callbacks come from another thread: they must be marshalled
        import threading
        threading.Thread(target=t.on_open).start()
        check(pump_until(app, lambda: app.state() == "normal", 3), f"tray click shows the window ({app.state()})")
        app.withdraw()
        threading.Thread(target=t.on_lost).start()
        check(pump_until(app, lambda: app.state() == "normal", 3), "icon lost -> the window comes back")
        U.to_active(app, ep)
        pump(app, 0.3)
        check(t.status[-1][2] == "Turn off" and t.status[-1][0] == main.T.LED["ACTIVE"], f"tray follows ACTIVE ({t.status[-1]})")
        check("Serving" in t.status[-1][1], f"tooltip has the state ({t.status[-1][1]!r})")
        app.withdraw()
        n = len(ep.stop_calls)
        threading.Thread(target=t.on_toggle).start()
        check(pump_until(app, lambda: len(ep.stop_calls) > n, 3), "tray Turn off works (model idle)")
        check(app.state() == "normal", "tray toggle brings the window up (dialogs need it)")
        ep.status_value = dict(U.IDLE_STATUS)
        U.fire(ep.stop_calls[-1][0], U.stop_result())
        pump(app, 0.5)
        # Quit from the tray while IDLE: exits, stops the tray
        threading.Thread(target=t.on_quit).start()
        check(pump_until(app, lambda: app._destroyed, 3), "tray Quit exits")
        check(t.stopped, "tray icon removed on exit")
    finally:
        tray_mod.TrayIcon = real
        close(app)
    # no tray: X quits as before
    app, det, ep = U.new_app()
    try:
        check(app.tray is None, "stubs: no tray by default")
        app.on_close_request()
        check(pump_until(app, lambda: app._destroyed, 3), "without a tray the X quits")
    finally:
        close(app)


# ------------------------------------------------------------------------------------------------ updates
INFO = None


def _info(ver="9.9.0"):
    return updater.UpdateInfo(version=ver, tag=f"v{ver}", asset_name=f"AgentStart-Setup-{ver}.exe",
                              asset_url=f"https://github.com/{updater.REPO}/releases/download/v{ver}/"
                                        f"AgentStart-Setup-{ver}.exe",
                              size=14_000_000, sha256="ab" * 32, page_url=f"https://github.com/{updater.REPO}")


def test_update_offer() -> None:
    saved = (updater.check, updater.download, updater.launch_installer)
    calls = {"check": 0, "download": [], "launch": []}
    result = {"v": ("newer", _info())}

    def fake_check(current, timeout=8.0, api_url=None):
        calls["check"] += 1
        return result["v"]

    def fake_download(i, dest, progress=None, stop_evt=None, **kw):
        for k in range(1, 5):
            progress and progress(k * i.size // 4, i.size)
            time.sleep(0.05)
        calls["download"].append(i.version)
        return os.path.join(dest, i.asset_name)

    def fake_launch(path, sha256, **kw):
        calls["launch"].append((path, sha256, kw))

    updater.check, updater.download, updater.launch_installer = fake_check, fake_download, fake_launch
    config_store.save_state({"update_checked_at": None, "update_pending": None})
    app, det, ep = U.new_app()
    asked = []
    real_ask = main.messagebox.askyesno
    try:
        check(app._no_update_check, "not offered when run from source / with test stubs")
        app._no_update_check = False
        # an error keeps nothing, retries in an hour, does not count as a check
        result["v"] = ("error", None)
        app._maybe_check_update()
        check(pump_until(app, lambda: calls["check"] == 1 and not app._update_checking, 3), "check ran (error)")
        check(app._update is None and app._update_checked_at is None, "error: not recorded as checked")
        check(app._update_next_at - time.monotonic() > updater.RETRY_AFTER_ERROR_S - 60, "error: retry in about an hour")
        app._maybe_check_update()
        pump(app, 0.2)
        check(calls["check"] == 1, "no retry before that (in memory, not state.json)")
        app._update_next_at = 0.0
        result["v"] = ("newer", _info())
        app._maybe_check_update()
        check(pump_until(app, lambda: app._update is not None, 3) and calls["check"] == 2, "daily check found 9.9.0")
        check(pump_until(app, lambda: "Update to 9.9.0 available" in status(app), 6) and app._detail_link,
              f"idle line offers the update ({status(app)!r})")
        U.shot(app, "extras_update_offer")
        check(pump_until(app, lambda: isinstance(config_store.load_state().get("update_checked_at"), float), 3),
              "check time remembered")
        app._update_next_at = 0.0
        app._maybe_check_update()
        pump(app, 0.3)
        check(calls["check"] == 2, "not checked again the same day")
        # Settings shows the update as a link
        app.open_settings()
        pump(app, 0.4)
        d = app.dialog
        link = getattr(d, "_update_link", None)
        check(link is not None and "Update to 9.9.0" in link.cget("text"), "Settings: update link")
        U.shot(d, "extras_settings_update")
        main.messagebox.askyesno = lambda *a, **k: asked.append(a) or False
        if link is not None:
            link._label.event_generate("<Button-1>")
        check(pump_until(app, lambda: asked and app.dialog is None, 3), "the link closes Settings and asks")
        check(asked and "9.9.0" in asked[-1][1] and not calls["download"], "No = nothing downloaded")
        # the idle line
        main.messagebox.askyesno = lambda *a, **k: asked.append(a) or True
        pump_until(app, lambda: "Update to" in status(app), 6)
        app.preview._label.event_generate("<Button-1>")
        check(pump_until(app, lambda: calls["launch"], 5), f"Yes: re-checked, downloaded, installer started ({calls})")
        check(calls["check"] == 3, "GitHub re-checked at install time (fresh URL + digest)")
        if calls["launch"]:
            path, sha, kw = calls["launch"][0]
            check(path.endswith("AgentStart-Setup-9.9.0.exe") and sha == "ab" * 32, "the verified installer + its sha256")
            check(kw.get("wait_pid") == os.getpid() and str(kw.get("log_path", "")).endswith("install.log"),
                  f"installer waits for this process, logs ({kw})")
        st = config_store.load_state().get("update_pending") or {}
        check(st.get("version") == "9.9.0" and st.get("from") == main.__version__, f"update marked pending ({st})")
        check(pump_until(app, lambda: app._destroyed, 3), "the app exits so the installer can replace it")
    finally:
        main.messagebox.askyesno = real_ask
        close(app)
    # next start: the update did not go in
    app, det, ep = U.new_app()
    try:
        app._report_update_result()
        check("did not install" in status(app), f"failed update reported ({status(app)!r})")
        check(config_store.load_state().get("update_pending") is None, "reported once")
    finally:
        close(app)
    config_store.save_state({"update_pending": {"version": main.__version__, "from": "1.0.0", "at": time.time()}})
    app, det, ep = U.new_app()
    try:
        app._report_update_result()
        check(f"Updated to {main.__version__}" in status(app), f"successful update reported ({status(app)!r})")
    finally:
        close(app)
    # a failed download: note, app keeps running, retry possible; never exits mid-start
    def bad_download(*a, **k):
        raise updater.UpdateError("size mismatch")
    updater.download = bad_download
    result["v"] = ("newer", _info())
    app, det, ep = U.new_app()
    try:
        main.messagebox.askyesno = lambda *a, **k: True
        app._update = _info()
        app.install_update()
        check(pump_until(app, lambda: "Update failed" in status(app), 4) and not app._updating and not app._destroyed,
              f"failed download: note, still running, retry possible ({status(app)!r})")
        updater.download = fake_download
        n = len(calls["launch"])
        app.install_update()
        pump_until(app, lambda: app._updating, 1)
        app.state_name = main.STARTING         # Turn on raced the download: the exit must wait
        pump(app, 1.5)
        check(len(calls["launch"]) == n and not app._destroyed, "installer not started while STARTING")
        app.state_name = main.IDLE
        check(pump_until(app, lambda: len(calls["launch"]) > n, 4), "started once STARTING is over")
        pump_until(app, lambda: app._destroyed, 3)
    finally:
        main.messagebox.askyesno = real_ask
        updater.check, updater.download, updater.launch_installer = saved
        config_store.save_state({"update_checked_at": None, "update_pending": None})
        close(app)


# ------------------------------------------------------------------------------------------------ first start
def test_first_start_search(tmp: Path) -> None:
    pa, pb = U._free_port(), U._free_port()
    sdir = U.make_strata(tmp, "Strata-F", {"a": pa, "b": pb})
    found = strata_finder.Found(strata_dir=str(sdir), launchers=[str(sdir / "run-a.bat"), str(sdir / "run-b.bat")],
                                newest_mtime=time.time())
    real = strata_finder.find_strata
    strata_finder.find_strata = lambda roots=None, time_budget_s=8.0, max_depth=4, stop_evt=None: [found]
    cfg = dict(U.CFG, strata_dir="", launcher_bat="", launch_cmd=[], launch_cwd="")
    app, det, ep = U.new_app(cfg)
    try:
        app._no_finder = False
        app._maybe_find_strata()
        check(pump_until(app, lambda: app._found_strata is not None, 3), "search result arrives")
        check(pump_until(app, lambda: "Strata found" in status(app) and app._detail_link, 6),
              f"idle line offers the found Strata ({status(app)!r})")
        U.shot(app, "extras_found_strata")
        app.preview._label.event_generate("<Button-1>")
        pump(app, 0.5)
        d = app.dialog
        check(isinstance(d, SettingsDialog), "click opens Settings")
        if isinstance(d, SettingsDialog):
            vals = list(d._menu.cget("values"))
            check("run-a.bat" in vals and "run-b.bat" in vals, f"Settings lists the found launchers ({vals})")
            d.close()
            pump(app, 0.3)
        n = len(ep.start_calls)
        app.on_toggle()
        pump(app, 0.4)
        check(isinstance(app.dialog, SettingsDialog) and len(ep.start_calls) == n and app.state_name == "IDLE",
              "Turn on without a launcher opens Settings instead of failing")
        if isinstance(app.dialog, SettingsDialog):
            app.dialog.close()
            pump(app, 0.2)
    finally:
        strata_finder.find_strata = real
        close(app)
    # nothing found
    strata_finder.find_strata = lambda *a, **k: []
    app, det, ep = U.new_app(cfg)
    try:
        app._no_finder = False
        app._maybe_find_strata()
        check(pump_until(app, lambda: "No Strata found" in status(app), 3), f"nothing found: says so ({status(app)!r})")
    finally:
        strata_finder.find_strata = real
        close(app)
    # a configured launcher: no search
    app, det, ep = U.new_app()
    try:
        app._no_finder = False
        app._maybe_find_strata()
        check(not app._finder_ran, "launcher configured: no search")
    finally:
        close(app)


# ------------------------------------------------------------------------------------------------ settings switch
def test_reopen_switch() -> None:
    app, det, ep = U.new_app()
    try:
        app.open_settings()
        pump(app, 0.4)
        d = app.dialog
        check(isinstance(d, SettingsDialog) and d._reopen_var.get() is True, "Settings: reopen switch on by default")
        U.shot(d, "extras_settings_switch")
        rec = {"name": "notes.exe", "exe": sys.executable, "cmdline": [sys.executable, "-c", "pass"], "cwd": "",
               "category": "app", "create_time": 1.0, "elevated": False, "aumid": None}
        main._save_reopen_records([rec])
        d._reopen_switch.toggle()
        pump(app, 0.5)
        check(app._reopen_pref is False, "switch off -> preference off")
        check(pump_until(app, lambda: config_store.load_state().get("reopen_after") is False, 3), "saved")
        check(len(reopen.load_pending()) == 1, "switching off keeps the list (Turn off decides)")
        d._reopen_switch.toggle()
        pump(app, 0.3)
        check(app._reopen_pref is True and len(reopen.load_pending()) == 1, "switch back on: nothing lost")
        d._reopen_switch.toggle()
        pump(app, 0.3)
        d.close()
        pump(app, 0.3)
        from sweep_dialog import SweepDialog
        sd = SweepDialog(app, det.cands, lambda *_: None, reopen_default=app._reopen_pref)
        pump(app, 0.3)
        check(sd._reopen_var.get() is False, "the start dialog follows the Settings switch")
        sd._decide("cancel")
        pump(app, 0.3)
        # Turn off with the switch off: nothing starts by itself, the apps are offered as one click
        n = len(U.LAUNCHED)
        app._reopening = True
        app._run_worker("t-reopen", app._reopen_worker, False)
        check(pump_until(app, lambda: not app._reopening, 4), "reopen step finished")
        check(len(U.LAUNCHED) == n, "switch off: nothing reopened by itself")
        check(len(app._reopen_offer) == 1 and len(reopen.load_pending()) == 1, f"offered as one click ({app._reopen_offer})")
        app.open_settings()
        pump(app, 0.4)
        app.dialog._reopen_switch.toggle()
        pump(app, 0.3)
        check(app._reopen_pref is True, "switch back on")
        app.dialog.close()
        pump(app, 0.2)
    finally:
        reopen.clear_pending()
        close(app)


# ------------------------------------------------------------------------------------------------ v1.8
class FakeWatcher:
    def __init__(self):
        self.armed = 0
        self.disarmed = 0
        self.stopped = False

    def arm(self):
        self.armed += 1

    def disarm(self):
        self.disarmed += 1

    def stop(self):
        self.stopped = True


def test_game_alert_and_tray_models(tmp: Path) -> None:
    import threading
    import tray as tray_mod
    real = tray_mod.TrayIcon
    tray_mod.TrayIcon = FakeTray
    pa, pb = U._free_port(), U._free_port()
    sdir = U.make_strata(tmp, "Strata-M", {"a": pa, "b": pb})
    info = launcher_bat.parse_bat(str(sdir / "run-a.bat"))
    cfg = dict(U.CFG, strata_dir=str(sdir), launcher_bat=str(sdir / "run-a.bat"), launch_cmd=info.argv,
               launch_cwd=info.cwd, port=pa, endpoint_url=f"http://127.0.0.1:{pa}/v1")
    app, det, ep = U.new_app(cfg)
    try:
        app._extras, app._no_tray = True, False
        app._start_tray()
        t = app.tray
        pump_until(app, lambda: t.status, 3)
        app._refresh_launchers()
        app._sync_tray()
        check(t.models and t.models[-1][0] and len(t.models[-1][0]) == 2, f"tray gets the two launchers ({t.models[-1:]})")
        if t.models and t.models[-1][0]:
            items, cur, en = t.models[-1]
            check(cur and cur.endswith("run-a.bat") and en, f"current launcher marked, enabled ({cur}, {en})")
            check(all("✓" not in lbl for lbl, _ in items), "labels without the ✓")
        # tray switch -> same as the ⇄ menu
        picked = []
        app.switch_model = lambda path: picked.append(path)
        threading.Thread(target=t.on_switch, args=(str(sdir / "run-b.bat"),)).start()
        check(pump_until(app, lambda: picked, 3) and picked[0].endswith("run-b.bat"), "tray Switch model -> switch_model")
        del app.switch_model
        # game alert: armed only while serving
        w = FakeWatcher()
        app.games, app._games_armed = w, False
        app._arm_games(app.state_name == "ACTIVE")
        check(w.armed == 0, "idle: not armed")
        U.to_active(app, ep)
        pump(app, 0.5)
        check(w.armed == 1 and app._games_armed, "serving: armed")
        app._mem = (65536.0, 12000.0, 50000.0)
        threading.Thread(target=lambda: app.post(app._on_game_started, "EldenRing", 9000.0)).start()
        check(pump_until(app, lambda: any(n[2] == "game" for n in t.notes), 3), f"tray balloon about the game ({t.notes[-1:]})")
        note = [n for n in t.notes if n[2] == "game"][-1]
        check("EldenRing" in note[1] and "~49 GB" in note[1], f"balloon text ({note[1]!r})")
        U.shot(app, "extras_game_alert")
        # clicking the balloon turns Strata off (model idle -> straight away)
        app.withdraw()
        n = len(ep.stop_calls)
        threading.Thread(target=t.on_balloon_click, args=("game",)).start()
        check(pump_until(app, lambda: len(ep.stop_calls) > n, 3), "balloon click -> Turn off")
        check(app.state() == "normal", "and the window is shown")
        ep.status_value = dict(U.IDLE_STATUS)
        U.fire(ep.stop_calls[-1][0], U.stop_result())
        pump(app, 0.5)
        check(w.disarmed >= 1 and not app._games_armed, "stopped: disarmed")
        # switch off -> not armed when serving again
        app.set_game_pref(False)
        U.to_active(app, ep)
        pump(app, 0.4)
        check(w.armed == 1, "game alert off: not armed while serving")
        t.notes.clear()
        app._on_game_started("Doom", 1.0)
        check(not t.notes, "game alert off: no balloon")
        app.set_game_pref(True)
        check(w.armed == 2, "switched back on while serving: armed again")
    finally:
        tray_mod.TrayIcon = real
        close(app)


def test_settings_switches() -> None:
    import autostart
    import tray as tray_mod
    real = tray_mod.TrayIcon
    tray_mod.TrayIcon = FakeTray
    calls = []
    saved_auto = (autostart.enable, autostart.disable, autostart.is_enabled)
    autostart.enable = lambda exe, *a, **k: calls.append(("enable", exe))
    autostart.disable = lambda *a, **k: calls.append(("disable",))
    autostart.is_enabled = lambda *a, **k: False
    real_frozen = config_store.FROZEN
    app, det, ep = U.new_app()
    try:
        app._extras, app._no_tray = True, False
        app._start_tray()
        t = app.tray
        pump_until(app, lambda: t.status, 3)
        app.open_settings()
        pump(app, 0.4)
        d = app.dialog
        keys = list(getattr(d, "switches", {}))
        check(keys == ["reopen", "tray", "autostart", "games", "updates"], f"Settings switches ({keys})")
        U.shot(d, "extras_settings_switches")
        if "tray" in keys:
            check(d.switches["tray"][0].get() is True, "tray switch on")
            check(str(d.switches["autostart"][1].cget("state")) == "disabled", "from source: Start with Windows greyed")
            check(str(d.switches["updates"][1].cget("state")) == "disabled", "from source: updates greyed")
            d.switches["tray"][1].toggle()
            pump(app, 0.3)
            check(app.tray is None and t.stopped and not app._tray_pref, "tray switch off: icon removed")
            check(pump_until(app, lambda: config_store.load_state().get("tray_enabled") is False, 3), "saved")
            d.switches["games"][1].toggle()
            pump(app, 0.2)
            check(app._game_pref is False and pump_until(app, lambda: config_store.load_state().get("game_alert") is False, 3),
                  "game alert switch off + saved")
            d.switches["games"][1].toggle()
            d.switches["tray"][1].toggle()
            pump(app, 0.4)
            check(app._tray_pref and isinstance(app.tray, FakeTray) and app.tray is not t, "tray switch on: a new icon")
            d.close()
            pump(app, 0.3)
        # with the tray off, X quits
        app.set_tray_pref(False)
        app.on_close_request()
        check(pump_until(app, lambda: app._destroyed, 3), "tray off: X quits")
    finally:
        close(app)
    # installed app: Start with Windows + updates are live
    config_store.FROZEN = True
    app, det, ep = U.new_app()
    try:
        app._extras = True
        sw = {x["key"]: x for x in app._settings_switches()}
        check(sw["autostart"]["enabled"] and sw["updates"]["enabled"], "installed: both switches usable")
        check(app.set_autostart_pref(True) is True and calls[-1][0] == "enable", f"autostart on -> enable ({calls[-1:]})")
        check(app.set_autostart_pref(False) is True and calls[-1] == ("disable",), "autostart off -> disable")

        def boom(*a, **k):
            raise autostart.AutostartError("access denied")
        autostart.enable = boom
        check(app.set_autostart_pref(True) is False and "Start with Windows failed" in status(app),
              f"a failed registry write is refused + shown ({status(app)!r})")
        upd = []
        app._maybe_check_update = lambda: upd.append(1)
        app.set_update_pref(False)
        check(app._no_update_check and app._update is None, "updates off: no checks")
        app.set_update_pref(True)
        check(not app._no_update_check and upd, "updates on (installed): checks again")
    finally:
        config_store.FROZEN = real_frozen
        autostart.enable, autostart.disable, autostart.is_enabled = saved_auto
        tray_mod.TrayIcon = real
        config_store.save_state({"tray_enabled": True, "game_alert": True, "update_check": True})
        close(app)


def test_review_fixes() -> None:
    """Fable v1.8 review: reachable windows without a tray, stale balloons, one-shot stop toast, tray models."""
    import threading
    import tray as tray_mod
    # 1. --tray with the tray off: iconify after the loop runs; CTk's mainloop must not leave it withdrawn
    app, det, ep = U.new_app()
    try:
        seen = {}
        app._started_hidden = True
        app.after(300, app.iconify)
        app.after(1500, lambda: (seen.setdefault("state", app.state()), app.quit()))
        app.mainloop()
        check(seen.get("state") == "iconic", f"--tray without a tray: minimised to the taskbar ({seen})")
    finally:
        close(app)
    # 2. --tray, the icon fails: minimised (not withdrawn), retried
    app, det, ep = U.new_app()
    try:
        app._started_hidden, app._no_tray = True, False
        app.withdraw()
        pump(app, 0.2)
        app._tray_started(None, False)          # self.tray is None -> stale check passes only for t == self.tray
        app.tray = object()
        t = app.tray
        app._tray_started(t, False)
        pump(app, 0.3)
        check(app.state() == "iconic", f"tray failed at logon: minimised, reachable ({app.state()})")
        check(app._tray_retry_until > time.monotonic() + 60, "and the icon is retried for 2 min")
    finally:
        close(app)
    # 3. stale game balloon: only opens the window
    real = tray_mod.TrayIcon
    tray_mod.TrayIcon = FakeTray
    app, det, ep = U.new_app()
    try:
        app._extras, app._no_tray = True, False
        app._start_tray()
        t = app.tray
        pump_until(app, lambda: t.status, 3)
        U.to_active(app, ep)
        pump(app, 0.4)
        app._on_game_started("Hades", 1.0)
        app._game_alert = (time.monotonic() - main.GAME_ALERT_CLICK_S - 1, app._start_token)
        n = len(ep.stop_calls)
        app._balloon_clicked("game")
        pump(app, 0.4)
        check(len(ep.stop_calls) == n and app.state_name == "ACTIVE", "an old game balloon does not turn Strata off")
        app._on_game_started("Hades", 1.0)
        app._box_open = True
        app._balloon_clicked("game")
        pump(app, 0.3)
        check(len(ep.stop_calls) == n, "not while a question box is open")
        app._box_open = False
        app._balloon_clicked("other")
        check(len(ep.stop_calls) == n, "another balloon's click only opens the window")
        # 4. tray off -> on: the new icon gets the model list again
        app.set_tray_pref(False)
        app.set_tray_pref(True)
        t2 = app.tray
        pump_until(app, lambda: t2.status, 3)
        app._launchers = [("m-a  ·  run-a.bat  ✓", "a.bat"), ("m-b  ·  run-b.bat", "b.bat")]
        app._sync_tray()
        check(t2.models and t2.models[-1][1] == "a.bat", f"re-created tray gets the models ({t2.models[-1:]})")
        # 5. game started while loading: told once ready
        app.loaded = False
        t2.notes.clear()
        app._on_game_started("Celeste", 1.0)
        check(not t2.notes and app._game_queued == ("Celeste", 1.0), "game during the load: queued")
        app.loaded = True
        app._apply_state()
        check(any(n_[2] == "game" and "Celeste" in n_[1] for n_ in t2.notes), "delivered once the model is ready")
    finally:
        tray_mod.TrayIcon = real
        close(app)
    # 6. the safe-stop toast wording is one-shot: a later manual stop uses the default
    app, det, ep = U.new_app()
    try:
        U.to_active(app, ep)
        pump(app, 0.3)
        app._stop_toast = ("Strata turned off", "x {freed}")
        app.turn_off()
        check(getattr(ep, "next_toast", None) == ("Strata turned off", "x {freed}"), "safe stop: explained toast")
        ep.status_value = dict(U.IDLE_STATUS)
        U.fire(ep.stop_calls[-1][0], U.stop_result())
        pump(app, 0.4)
        U.to_active(app, ep)
        pump(app, 0.3)
        app.turn_off()
        check(getattr(ep, "next_toast", "unset") is None, "next manual stop: default toast")
    finally:
        close(app)


def run() -> int:
    import tempfile
    main.TOGGLE_DEBOUNCE_S = 0.0
    old = (main.LIVE_POLL_S, main.IDLE_GRACE_S)
    main.LIVE_POLL_S, main.IDLE_GRACE_S = 0.2, 0.8
    dialogs = U.Dialogs()
    dialogs.install()
    tmp = Path(tempfile.mkdtemp(prefix="as_extras_"))
    try:
        for fn, args in ((test_tool_gap, ()), (test_tray, ()), (test_update_offer, ()),
                         (test_first_start_search, (tmp,)), (test_reopen_switch, ()),
                         (test_game_alert_and_tray_models, (tmp,)), (test_settings_switches, ()),
                         (test_review_fixes, ())):
            gc.collect()
            print(f"-- {fn.__name__}", flush=True)
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
                print(f"FAIL: {FAILS[-1]}")
    finally:
        dialogs.restore()
        main.LIVE_POLL_S, main.IDLE_GRACE_S = old
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
