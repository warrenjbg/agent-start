"""UI smoke test for Hermes Controller (stub backends only — never touches real processes or port 8080).

Run:  .venv\\Scripts\\python.exe tests\\test_ui_smoke.py
Exit code 0 = all checks passed. Screenshots land in tests\\ui_<state>.png.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import tkinter.messagebox as tk_messagebox  # noqa: E402

import customtkinter as ctk  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import main  # noqa: E402  (must import without detector.py / server_manager.py)
from main import HermesControllerApp, PILL_COLORS, LOADING_COLOR  # noqa: E402
from sweep_dialog import SweepDialog  # noqa: E402
from whitelist_modal import WhitelistModal  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


# ---------------------------------------------------------------- stubs
CANDS = [
    {"pid": 41001, "name": "eldenring.exe", "exe": r"D:\SteamLibrary\steamapps\common\ELDEN RING\eldenring.exe",
     "create_time": 1731370000.25, "ram_mb": 6144.0, "vram_mb": 3072.0, "reason": "game dir: steamapps",
     "category": "game", "preselect": True, "member_count": 2},
    {"pid": 41002, "name": "Battle.net.exe", "exe": r"C:\Program Files (x86)\Battle.net\Battle.net.exe",
     "create_time": 1731370001.5, "ram_mb": 820.0, "vram_mb": 210.0, "reason": "game dir: Battle.net",
     "category": "game", "preselect": True, "member_count": 5},
    {"pid": 41003, "name": "chrome.exe", "exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
     "create_time": 1731370002.75, "ram_mb": 2300.0, "vram_mb": None, "reason": "open window",
     "category": "window", "preselect": False, "member_count": 14},
    {"pid": 41004, "name": "wallpaper64.exe", "exe": r"D:\SteamLibrary\x\wallpaper64.exe",
     "create_time": 1731370003.0, "ram_mb": 1400.0, "vram_mb": 120.0, "reason": "background / tray",
     "category": "background", "preselect": False, "member_count": 1},
]


class StubDetector:
    def __init__(self) -> None:
        self.cands = [dict(c) for c in CANDS]
        self.preselected_mode = True
        self.sweep_calls: list[tuple[bool, object]] = []
        self.whitelist_saves: list[list[str]] = []
        self.close_calls = 0
        self.scan_calls = 0

    def scan_killable_processes(self) -> list[dict]:
        self.scan_calls += 1
        time.sleep(0.05)
        return [dict(c) for c in self.cands]

    def sweep_processes(self, dry_run: bool = False, targets=None) -> dict:
        time.sleep(0.05)
        self.sweep_calls.append((dry_run, None if targets is None else list(targets)))
        cands = self.cands
        if not self.preselected_mode:
            cands = [dict(c, preselect=False) for c in cands]
        pre = [c for c in cands if c["preselect"]]
        if targets is None:
            sel = cands
        else:
            sel = [c for c in cands if any(c["pid"] == p and abs(c["create_time"] - t) < 0.01 for p, t in targets)]
        sel = sorted(sel, key=lambda c: -c["ram_mb"])
        vr = [c["vram_mb"] for c in sel]
        return {
            "closed_apps": [c["name"] for c in sel], "ram_freed_mb": round(sum(c["ram_mb"] for c in sel), 1),
            "vram_freed_mb": None if any(v is None for v in vr) else sum(vr), "failed": [], "dry_run": dry_run,
            "candidates": len(cands), "preselected": len(pre),
            "preselected_ram_mb": round(sum(c["ram_mb"] for c in pre), 1),
            "preselected_vram_mb": sum(c["vram_mb"] for c in pre) if pre else None,
            "names": [c["name"] for c in sel],
        }

    def get_whitelist(self) -> list[str]:
        return ["code.exe", "Discord.exe"]

    def update_whitelist(self, apps: list[str]) -> None:
        time.sleep(0.05)
        self.whitelist_saves.append(list(apps))

    def list_whitelist_candidates(self) -> list[dict]:
        time.sleep(0.1)
        return [
            {"name": "Battle.net.exe", "whitelisted": False, "running": True},
            {"name": "chrome.exe", "whitelisted": False, "running": True},
            {"name": "code.exe", "whitelisted": True, "running": True},
            {"name": "Discord.exe", "whitelisted": True, "running": False},
            {"name": "notepad.exe", "whitelisted": False, "running": True},
        ]

    def gpu_dedicated_used_mb(self):
        return 4096.0

    def close(self) -> None:
        self.close_calls += 1


IDLE_STATUS = {"state": "idle", "pid": None, "model_id": None, "adopted": False, "log_path": None, "loaded": None}


def active_status(loaded=True, model="swift-test-1"):
    return {"state": "active", "pid": 5555, "model_id": model, "adopted": False, "log_path": None, "loaded": loaded}


class StubEndpoint:
    def __init__(self) -> None:
        self.status_value = dict(IDLE_STATUS)
        self.start_calls: list[tuple] = []
        self.stop_calls: list[tuple] = []
        self.start_return = True
        self.stop_return = True

    def start_endpoint(self, on_ready, on_error) -> bool:
        self.start_calls.append((on_ready, on_error))
        return self.start_return

    def stop_endpoint(self, on_stopped, force: bool = False) -> bool:
        self.stop_calls.append((on_stopped, force))
        return self.stop_return

    def is_running(self) -> bool:
        return self.status_value["state"] == "active"

    def status(self) -> dict:
        return dict(self.status_value)

    def protected_pids(self) -> set[int]:
        return set()


def fire(fn, *args) -> None:
    """Invoke a manager callback on a worker thread, as the real EndpointManager does."""
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    t.join(2)


def stop_result(**kw) -> dict:
    r = {"ok": True, "port_released": True, "killed_pids": [5555], "ram_freed_mb": 2048.0, "unload": "ok",
         "error": None, "cancelled_start": False}
    r.update(kw)
    return r


# ---------------------------------------------------------------- helpers
def pump(app, seconds: float = 0.1) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if getattr(app, "_destroyed", False):
            return
        app.update()
        time.sleep(0.01)


def pump_until(app, cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        if app._destroyed:
            return bool(cond())
        app.update()
        time.sleep(0.01)
    return bool(cond())


def shot(win, name: str) -> Path:
    win.attributes("-topmost", True)
    win.lift()
    win.update_idletasks()
    win.update()
    time.sleep(0.35)
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    path = HERE / f"ui_{name}.png"
    img.save(path)
    print(f"screenshot {path} ({w}x{h})")
    return path


class Dialogs:
    """Monkeypatch tkinter.messagebox (it blocks); records prompts."""

    def __init__(self) -> None:
        self.yesno = True
        self.yesnocancel: bool | None = None
        self.asked: list[str] = []
        self._orig = (tk_messagebox.askyesno, tk_messagebox.askyesnocancel)

    def install(self) -> None:
        tk_messagebox.askyesno = lambda title, msg, **k: (self.asked.append(msg), self.yesno)[1]
        tk_messagebox.askyesnocancel = lambda title, msg, **k: (self.asked.append(msg), self.yesnocancel)[1]

    def restore(self) -> None:
        tk_messagebox.askyesno, tk_messagebox.askyesnocancel = self._orig


CFG = {"endpoint_url": "http://127.0.0.1:8080/v1", "port": 8080, "model_name": "swift-1.5-iq3_s",
       "user_whitelist": [], "game_dirs": ["steamapps"]}


def new_app():
    det, ep = StubDetector(), StubEndpoint()
    app = HermesControllerApp(detector=det, endpoint=ep, config=dict(CFG))
    app.attributes("-topmost", True)
    pump(app, 0.4)
    return app, det, ep


def pill(app):
    """(status title, LED colour) — the front-panel status line that replaced the pill."""
    return app.pill.cget("text"), app.led.cget("fg_color")


def to_active(app, ep, loaded=True):
    ep.status_value = active_status(loaded)
    app.on_status(ep.status())
    pump(app, 0.05)


# ---------------------------------------------------------------- tests
def test_layout_idle_preview(app, det, ep) -> None:
    scale = ctk.ScalingTracker.get_window_scaling(app)
    wscale = ctk.ScalingTracker.get_widget_scaling(app)
    print(f"CTk window scaling {scale}, widget scaling {wscale}")
    w, h = app.winfo_width(), app.winfo_height()
    check(abs(w - 360 * scale) <= 2 and abs(h - 268 * scale) <= 2, f"window size {w}x{h} != 360x268*{scale}")
    check(app.resizable() == (False, False), "window must be non-resizable")
    tw, th = app.toggle.winfo_width(), app.toggle.winfo_height()
    check(abs(tw - 180 * wscale) <= 2 and abs(th - 56 * wscale) <= 2, f"toggle {tw}x{th} != 180x56*{wscale}")
    check(pill(app) == ("Idle", PILL_COLORS["IDLE"]), f"idle status {pill(app)}")
    check(app.toggle.cget("text") == "Turn on", "IDLE toggle text 'Turn on'")

    ok = pump_until(app, lambda: "pre-selected" in app.preview.cget("text"))
    txt = app.preview.cget("text")
    print("IDLE preview:", txt.replace("\n", " | "))
    check(ok and "2 of 4 apps pre-selected to close · 6.8 GB" in txt, f"preselected preview: {txt!r}")
    check("if all ticked" not in txt, "preselected preview must not say 'if all ticked'")
    check("eldenring, chrome, wallpaper64 +1 more" in txt, f"names line: {txt!r}")
    check(all(c[0] is True and c[1] is None for c in det.sweep_calls), "preview only uses dry_run=True, targets=None")

    # geometry: nothing clipped / overlapping
    app.update_idletasks()
    bottom = app.winfo_rooty() + app.winfo_height()
    right = app.winfo_rootx() + app.winfo_width()
    legend = [lbl for lbl, _ in app.legend.values()]
    for wdg, nm in [(app.telemetry, "telemetry"), (app.gear, "gear"), (app.pill, "status"), (app.gauge, "gauge")] + \
            [(l, "legend") for l in legend]:
        check(wdg.winfo_rooty() + wdg.winfo_height() <= bottom and wdg.winfo_rootx() + wdg.winfo_width() <= right,
              f"{nm} clipped")
    check(app.preview.winfo_rooty() + app.preview.winfo_height() <= app.progress.winfo_rooty(),
          "preview overlaps progress")
    check(app.toggle.winfo_rooty() + app.toggle.winfo_height() <= app.gauge.winfo_rooty() - 16,
          "button overlaps memory card")
    check(app.telemetry.cget("text") == "", f"telemetry empty until something is freed ({app.telemetry.cget('text')!r})")
    check(pump_until(app, lambda: "Free" in legend[2].cget("text") and "GB" in legend[2].cget("text")),
          f"memory legend filled ({legend[2].cget('text')!r})")
    shot(app, "idle")

    det.preselected_mode = False
    app._request_preview()
    ok = pump_until(app, lambda: "if all ticked" in app.preview.cget("text"))
    txt = app.preview.cget("text")
    print("IDLE preview (none preselected):", txt.replace("\n", " | "))
    check(ok and "4 apps could be closed · 10.4 GB if all ticked" in txt, f"all-ticked: {txt!r}")
    check(app.preview.winfo_rooty() + app.preview.winfo_height() <= app.progress.winfo_rooty(),
          "long preview overlaps progress")
    shot(app, "idle_all_ticked")
    det.preselected_mode = True


def test_on_flow(app, det, ep) -> None:
    app.on_toggle()
    check(app.state_name == "STARTING" and app.toggle.cget("text") == "Cancel", "ON -> STARTING/Cancel")
    check(str(app.gear.cget("state")) == "disabled", "gear disabled while STARTING")
    ok = pump_until(app, lambda: isinstance(app.dialog, SweepDialog))
    check(ok, "SweepDialog appears")
    dlg = app.dialog
    pump(dlg, 0.4)
    ticks = {c["pid"]: v.get() for v, c in dlg._rows}
    check(ticks == {41001: True, 41002: True, 41003: False, 41004: False}, f"preselected ticks {ticks}")
    dsw, dsh = dlg.winfo_width(), dlg.winfo_height()
    scale = ctk.ScalingTracker.get_window_scaling(dlg)
    check(abs(dsw - 372 * scale) <= 2 and abs(dsh - 320 * scale) <= 2, f"SweepDialog size {dsw}x{dsh}")
    check(dlg.grab_status() == "local", f"SweepDialog grab {dlg.grab_status()}")
    shot(dlg, "sweepdialog")
    n_sweeps = len(det.sweep_calls)
    dlg._close_btn.invoke()
    ok = pump_until(app, lambda: len(ep.start_calls) == 1)
    real = [c for c in det.sweep_calls[n_sweeps:] if c[0] is False]
    check(ok and len(real) == 1, f"one real sweep then start ({det.sweep_calls[n_sweeps:]}, starts={len(ep.start_calls)})")
    check(real and real[0][1] == [(41001, 1731370000.25), (41002, 1731370001.5)], f"targets {real}")
    check(abs(app.freed_mb - 6964.0) < 0.01, f"freed after sweep {app.freed_mb}")
    check(abs(app.freed_mb - 6.8 * 1024) < 1 and app.telemetry.cget("text") == "",
          f"sweep counted; 'Freed' hidden while Strata refills RAM ({app.telemetry.cget('text')!r})")
    check(app.state_name == "STARTING" and app.toggle.cget("text") == "Cancel", "STARTING after sweep")
    check(pill(app) == ("Starting", PILL_COLORS["STARTING"]), f"starting status {pill(app)}")
    shot(app, "starting")

    ep.status_value = active_status(True, "swift-test-1")
    fire(ep.start_calls[-1][0])
    ok = pump_until(app, lambda: app.state_name == "ACTIVE" and app.preview.cget("text").startswith("swift-test-1\n"))
    check(ok, f"on_ready -> ACTIVE 'swift-test-1' (got {app.state_name}, {app.preview.cget('text')!r})")
    check(pill(app) == ("Serving", PILL_COLORS["ACTIVE"]), f"active status {pill(app)}")
    check(app.toggle.cget("text") == "Turn off", "ACTIVE toggle 'Turn off'")
    check(str(app.gear.cget("state")) == "normal", "gear enabled while ACTIVE")
    shot(app, "active")

    # LOADING variant
    ep.status_value = active_status(False, "swift-test-1")
    app.on_status(ep.status())
    check(pill(app) == ("Loading", LOADING_COLOR), f"loading status {pill(app)}")
    check(app.state_name == "ACTIVE" and app.toggle.cget("text") == "Turn off", "LOADING is ACTIVE with Turn off")
    shot(app, "loading")
    ep.status_value = active_status(True, "swift-test-1")
    app.on_status(ep.status())
    check(pill(app) == ("Serving", PILL_COLORS["ACTIVE"]), "loaded again -> Serving")


def test_off_flow(app, det, ep, dialogs: Dialogs) -> None:
    freed0 = app.freed_mb
    app.on_toggle()
    check(app.state_name == "STOPPING" and app.toggle.cget("text") == "Stopping…", "OFF -> STOPPING")
    check(str(app.toggle.cget("state")) == "disabled", "toggle disabled while STOPPING")
    check(pill(app) == ("Stopping", PILL_COLORS["STOPPING"]), f"stopping status {pill(app)}")
    check(ep.stop_calls[-1][1] is False, "first stop is not forced")
    shot(app, "stopping")
    ep.status_value = dict(IDLE_STATUS)
    fire(ep.stop_calls[-1][0], stop_result())
    ok = pump_until(app, lambda: app.state_name == "IDLE")
    check(ok and abs(app.freed_mb - freed0 - 2048.0) < 0.01, f"OFF -> IDLE + freed (freed={app.freed_mb})")
    check(app.telemetry.cget("text").startswith(f"Freed {(freed0 + 2048) / 1024:.1f} GB"), "telemetry after OFF")

    # busy -> force yes
    to_active(app, ep)
    check(app.state_name == "ACTIVE", "monitor reconcile IDLE -> ACTIVE")
    dialogs.yesno = True
    dialogs.asked.clear()
    app.on_toggle()
    fire(ep.stop_calls[-1][0], stop_result(ok=False, port_released=False, unload="busy", error="busy",
                                           ram_freed_mb=0.0))
    ok = pump_until(app, lambda: ep.stop_calls[-1][1] is True)
    check(ok and dialogs.asked == ["Model is mid-turn — force stop?"], f"busy -> force prompt ({dialogs.asked})")
    check(app.state_name == "STOPPING", "forced stop keeps STOPPING")
    ep.status_value = dict(IDLE_STATUS)
    fire(ep.stop_calls[-1][0], stop_result())
    check(pump_until(app, lambda: app.state_name == "IDLE"), "forced stop -> IDLE")

    # busy -> force no
    to_active(app, ep)
    dialogs.yesno = False
    n = len(ep.stop_calls)
    app.on_toggle()
    fire(ep.stop_calls[-1][0], stop_result(ok=False, port_released=False, unload="busy", error="busy",
                                           ram_freed_mb=0.0))
    ok = pump_until(app, lambda: app.state_name == "ACTIVE")
    check(ok and len(ep.stop_calls) == n + 1, "busy + decline force -> back to ACTIVE, no forced stop")

    # stop failure -> ERROR
    app.on_toggle()
    fire(ep.stop_calls[-1][0], stop_result(ok=False, port_released=False, error="port still in use"))
    check(pump_until(app, lambda: app.state_name == "ERROR"), "stop failure -> ERROR")
    check("port still in use" in app.preview.cget("text"), "stop failure text")
    dialogs.yesno = True


def test_error_and_cancel(app, det, ep) -> None:
    det.cands = []  # empty scan => start directly
    ep.status_value = dict(IDLE_STATUS)
    app._set_state("IDLE")
    n = len(ep.start_calls)
    app.on_toggle()
    check(pump_until(app, lambda: len(ep.start_calls) == n + 1), "empty scan -> start directly")
    check(app.dialog is None, "no SweepDialog for empty scan")
    msg = "Strata exited (code 1) — see C:\\logs\\strata-20261112-050000.log"
    fire(ep.start_calls[-1][1], msg)
    check(pump_until(app, lambda: app.state_name == "ERROR"), "on_error -> ERROR")
    check(pill(app) == ("Error", PILL_COLORS["ERROR"]), f"error status {pill(app)}")
    short = "Strata exited (code 1) · open strata-20261112-050000.log"
    check(app.preview.cget("text") == short and app.toggle.cget("text") == "Turn on",
          f"ERROR text shortened to the log file name + Turn on ({app.preview.cget('text')!r})")
    check(app._error_log == "C:\\logs\\strata-20261112-050000.log" and str(app.preview.cget("cursor")) == "hand2",
          "error line links to the full log path")
    app._error_hold_until = 0.0              # even after the hold expires...
    app._request_preview()
    pump(app, 0.3)
    check(app.preview.cget("text") == short, "ERROR keeps its message until the user acts (no preview overwrite)")
    shot(app, "error")

    # cancel during STARTING (server phase)
    app.on_toggle()
    check(pump_until(app, lambda: len(ep.start_calls) == n + 2), "second start")
    old_ready = ep.start_calls[-1][0]
    ns = len(ep.stop_calls)
    app.on_toggle()  # Cancel
    check(len(ep.stop_calls) == ns + 1 and ep.stop_calls[-1][1] is False, "Cancel -> stop_endpoint")
    check(app.state_name == "STOPPING", "cancel -> STOPPING")
    fire(ep.stop_calls[-1][0], stop_result(cancelled_start=True, unload="skipped", port_released=True))
    check(pump_until(app, lambda: app.state_name == "IDLE"), "cancelled_start -> IDLE")
    fire(old_ready)  # late callback of the cancelled start must be ignored
    pump(app, 0.2)
    check(app.state_name == "IDLE", "late on_ready after cancel ignored")

    # cancel during scan phase (local, no stop_endpoint)
    det.cands = [dict(c) for c in CANDS]
    ns = len(ep.stop_calls)
    app.on_toggle()
    app.on_toggle()
    pump(app, 0.3)
    check(app.state_name == "IDLE" and app.dialog is None and len(ep.stop_calls) == ns,
          "cancel during scan is local; late scan result ignored")

    # start_endpoint returns False => reconcile from status()
    ep.start_return = False
    det.cands = []
    ep.status_value = active_status(True, "adopted-x")
    app.on_toggle()
    check(pump_until(app, lambda: app.state_name == "ACTIVE"), "start False -> reconcile to ACTIVE from status")
    ep.start_return = True
    ep.status_value = dict(IDLE_STATUS)
    app.on_status(ep.status())
    pump(app, 0.05)
    check(app.state_name == "ACTIVE", "one stray idle reading does not flip ACTIVE -> IDLE")
    app.on_status(ep.status())
    pump(app, 0.05)
    check(app.state_name == "IDLE", "two consecutive idle readings -> IDLE")
    det.cands = [dict(c) for c in CANDS]


def test_sweep_dialog_close_x(app, det, ep) -> None:
    rec = []
    dlg = SweepDialog(app, [dict(c) for c in CANDS], lambda d, t: rec.append((d, t)))
    pump(app, 0.3)
    cmd = dlg.protocol("WM_DELETE_WINDOW")
    dlg.tk.call(cmd)
    dlg._decide("start_only")
    dlg._decide("close_and_start")
    pump(app, 0.1)
    check(rec == [("cancel", [])], f"close-X => cancel exactly once ({rec})")

    rec.clear()
    dlg = SweepDialog(app, [dict(c) for c in CANDS], lambda d, t: rec.append((d, t)))
    pump(app, 0.3)
    dlg._rows[2][0].set(True)
    dlg._decide("start_only")
    check(rec == [("start_only", [])], f"start_only => empty targets ({rec})")

    # in-app flow: close-X during ON => IDLE
    n = len(ep.start_calls)
    app.on_toggle()
    check(pump_until(app, lambda: isinstance(app.dialog, SweepDialog)), "dialog for close-X flow")
    app.dialog.tk.call(app.dialog.protocol("WM_DELETE_WINDOW"))
    pump(app, 0.2)
    check(app.state_name == "IDLE" and len(ep.start_calls) == n and app.dialog is None, "close-X in flow -> IDLE")

    # start_only in-app => start without sweep
    nsw = len([c for c in det.sweep_calls if c[0] is False])
    app.on_toggle()
    check(pump_until(app, lambda: isinstance(app.dialog, SweepDialog)), "dialog for start_only flow")
    app.dialog._decide("start_only")
    check(pump_until(app, lambda: len(ep.start_calls) == n + 1), "start_only -> start")
    check(len([c for c in det.sweep_calls if c[0] is False]) == nsw, "start_only does not sweep")
    fire(ep.start_calls[-1][1], "x")
    pump_until(app, lambda: app.state_name == "ERROR")


def test_whitelist_modal(app, det, ep) -> None:
    app._set_state("IDLE")
    app.open_whitelist()
    dlg = app.dialog
    check(isinstance(dlg, WhitelistModal), "gear opens WhitelistModal")
    check(dlg._loading_label.cget("text") == "Loading…", "Loading… label")
    ok = pump_until(app, lambda: dlg._loaded)
    names = [n for n, _ in dlg._rows]
    checked = dlg.checked_names()
    check(ok and names == ["Battle.net.exe", "chrome.exe", "code.exe", "Discord.exe", "notepad.exe"], f"rows {names}")
    check(checked == ["code.exe", "Discord.exe"], f"checked {checked}")
    texts = [w.cget("text") for w in dlg._list.winfo_children() if isinstance(w, ctk.CTkCheckBox)]
    check("Discord.exe (not running)" in texts and "code.exe" in texts, f"suffixes {texts}")
    w, h = dlg.winfo_width(), dlg.winfo_height()
    scale = ctk.ScalingTracker.get_window_scaling(dlg)
    check(abs(w - 320 * scale) <= 2 and abs(h - 320 * scale) <= 2, f"WhitelistModal size {w}x{h}")
    pump(app, 0.3)
    check(dlg.grab_status() == "local", "WhitelistModal grab")

    dlg._entry.insert(0, "foo")
    check(dlg.add_name() is False and "ending in .exe" in dlg._msg.cget("text"), "Add rejects non-.exe")
    dlg._entry.delete(0, "end")
    dlg._entry.insert(0, "NOTEPAD.EXE")
    check(dlg.add_name() is False and len(dlg._rows) == 5, "Add ignores casefold duplicate")
    dlg._entry.delete(0, "end")
    dlg._entry.insert(0, ".exe")
    check(dlg.add_name() is False, "Add rejects bare .exe")
    dlg._entry.delete(0, "end")
    dlg._entry.insert(0, "  Spotify.exe ")
    check(dlg.add_name() is True and dlg._rows[-1][0] == "Spotify.exe" and dlg._rows[-1][1].get(), "Add appends ticked")
    check(dlg._msg.cget("text") == "", "message cleared after valid add")
    pump(app, 0.1)
    shot(dlg, "whitelist")

    dlg._rows[0][1].set(True)  # tick Battle.net
    n_before = len(det.sweep_calls)
    dlg._save_btn.invoke()
    ok = pump_until(app, lambda: not app._dialog_open() and det.whitelist_saves)
    check(ok and det.whitelist_saves == [["Battle.net.exe", "code.exe", "Discord.exe", "Spotify.exe"]],
          f"Save -> update_whitelist({det.whitelist_saves})")
    check(pump_until(app, lambda: len(det.sweep_calls) > n_before), "on_saved refreshes preview")

    # Cancel closes without saving
    app.open_whitelist()
    dlg = app.dialog
    pump_until(app, lambda: dlg._loaded)
    dlg._cancel_btn.invoke()
    pump(app, 0.1)
    check(not app._dialog_open() and len(det.whitelist_saves) == 1, "Cancel closes without saving")

    # gear disabled while STARTING
    app.state_name = "STARTING"
    app.open_whitelist()
    check(app.dialog is None, "gear ignored while STARTING")
    app.state_name = "IDLE"
    app._apply_state()


def test_delete_window(dialogs: Dialogs) -> None:
    # IDLE => exit, detector.close()
    app, det, ep = new_app()
    app.on_close_request()
    check(app._destroyed and det.close_calls == 1, "IDLE close -> exit + detector.close")

    # ERROR => exit
    app, det, ep = new_app()
    app.show_error("boom")
    app.on_close_request()
    check(app._destroyed and det.close_calls == 1, "ERROR close -> exit")

    # STOPPING => ignored
    app, det, ep = new_app()
    to_active(app, ep)
    app.turn_off()
    app.on_close_request()
    check(not app._destroyed and "please wait" in app.preview.cget("text"), "STOPPING close ignored + note")
    ep.status_value = dict(IDLE_STATUS)
    fire(ep.stop_calls[-1][0], stop_result())
    pump_until(app, lambda: app.state_name == "IDLE")
    check(not app._destroyed, "stop finished without exit request -> still open")

    # STARTING => no stays; yes cancels then exits on callback
    det.cands = []
    app.on_toggle()
    pump_until(app, lambda: len(ep.start_calls) == 1)
    dialogs.yesno = False
    dialogs.asked.clear()
    app.on_close_request()
    check(not app._destroyed and dialogs.asked == ["Cancel start and exit?"] and not ep.stop_calls[1:],
          "STARTING close + no -> stay")
    dialogs.yesno = True
    app.on_close_request()
    check(not app._destroyed and len(ep.stop_calls) == 2 and app.state_name == "STOPPING", "STARTING yes -> cancel")
    fire(ep.stop_calls[-1][0], stop_result(cancelled_start=True, unload="skipped"))
    check(pump_until(app, lambda: app._destroyed) and det.close_calls == 1, "exit after cancel callback")

    # ACTIVE => cancel stays; no exits leaving Strata; yes stops then exits
    app, det, ep = new_app()
    to_active(app, ep)
    dialogs.yesnocancel = None
    app.on_close_request()
    check(not app._destroyed and app.state_name == "ACTIVE", "ACTIVE close + cancel -> stay")
    dialogs.yesnocancel = False
    app.on_close_request()
    check(app._destroyed and not ep.stop_calls and det.close_calls == 1, "ACTIVE close + no -> exit, Strata left")

    app, det, ep = new_app()
    to_active(app, ep)
    dialogs.yesnocancel = True
    app.on_close_request()
    check(len(ep.stop_calls) == 1 and not app._destroyed, "ACTIVE close + yes -> stop")
    # busy and user declines force => stay open
    dialogs.yesno = False
    fire(ep.stop_calls[-1][0], stop_result(ok=False, port_released=False, unload="busy", error="busy"))
    pump_until(app, lambda: app.state_name == "ACTIVE")
    pump(app, 0.1)
    check(not app._destroyed and app.state_name == "ACTIVE", "exit-stop busy + decline force -> stay open")
    # yes again, busy, accept force => exits when forced stop completes
    dialogs.yesno = True
    app.on_close_request()
    fire(ep.stop_calls[-1][0], stop_result(ok=False, port_released=False, unload="busy", error="busy"))
    pump_until(app, lambda: ep.stop_calls[-1][1] is True)
    ep.status_value = dict(IDLE_STATUS)
    fire(ep.stop_calls[-1][0], stop_result())
    check(pump_until(app, lambda: app._destroyed) and det.close_calls == 1, "ACTIVE yes + force -> exit after stop")


def test_threading_and_import() -> None:
    check("detector" not in sys.modules and "server_manager" not in sys.modules,
          "import main must not import detector/server_manager")
    app, det, ep = new_app()
    hits = []
    threading.Thread(target=lambda: app.post(lambda: hits.append(threading.current_thread() is threading.main_thread())),
                     daemon=True).start()
    app.post(lambda: 1 / 0)  # failing item must not stop the pump
    app.post(lambda: hits.append("after-error"))
    pump_until(app, lambda: len(hits) >= 2)
    check(hits[:1] == [True] and "after-error" in hits, f"post runs on main thread, survives errors ({hits})")
    pid_before = app._pump_id
    app.on_close_request()
    check(app._destroyed and app._stop_evt.is_set() and pid_before, "destroy cancels pump/monitor")


def test_load_progress_view(app, det, ep) -> None:
    """STARTING (server phase) shows phase + elapsed + bar from the Strata log tail."""
    import tempfile
    logf = Path(tempfile.mkdtemp(prefix="hc-ui-")) / "strata-test.log"
    logf.write_text("loading the model (the first start takes a minute or two) ...\n"
                    "[strata] starting the engine: reading the model's weights ...\n"
                    "[strata] loading the experts into RAM (about 45 GB) and locking part of them for the GPU.\n",
                    encoding="utf-8")
    det.cands = []
    ep.status_value = dict(IDLE_STATUS)
    app._set_state("IDLE")
    real_status = ep.status
    ep.status = lambda: {**real_status(), "log_path": str(logf)}
    try:
        app.on_toggle()
        ok = pump_until(app, lambda: "Loading experts into RAM" in app.preview.cget("text"), 5)
        txt = app.preview.cget("text")
        check(ok and "about 45 GB" in txt, f"phase from log shown ({txt!r})")
        check(app.elapsed.cget("text").count(":") == 1, f"elapsed timer shown ({app.elapsed.cget('text')!r})")
        check(0.25 <= app.progress.get() <= 0.78, f"progress within the experts phase ({app.progress.get():.2f})")
        check(app.progress.cget("progress_color") == PILL_COLORS["ACTIVE"], "progress drawn in the accent")
        shot(app, "loading_progress")
        with open(logf, "a", encoding="utf-8") as f:
            f.write("[strata] filling the GPU's expert cache (1095 experts, 2.11 GiB of VRAM) ...\n")
        ok = pump_until(app, lambda: "Filling GPU cache" in app.preview.cget("text"), 5)
        check(ok and app.progress.get() >= 0.78, f"next phase advances the bar ({app.progress.get():.2f})")
        fire(ep.start_calls[-1][0])                  # on_ready
        check(pump_until(app, lambda: app.state_name == "ACTIVE"), "ready -> ACTIVE")
        check(app.elapsed.cget("text") == "" and app.progress.get() == 0, "progress + timer cleared when ACTIVE")
    finally:
        ep.status = real_status
        ep.status_value = dict(IDLE_STATUS)
        app.on_status(ep.status()); app.on_status(ep.status())
        pump(app, 0.1)
        det.cands = [dict(c) for c in CANDS]


def test_strata_estimate(app, det, ep) -> None:
    """Memory bar attribution (Fable final #3-#5): live while loading, latched when serving, shrinks on stop."""
    keep = (app.state_name, app._avail_at_launch, app._strata_est)
    try:
        app.state_name, app._avail_at_launch, app._strata_est = "STARTING", 40000.0, None
        check(app.strata_estimate(30000.0, 200.0) == 10000.0, "STARTING: live drop in free RAM since launch")
        app.state_name, app._strata_est = "ACTIVE", 14000.0
        check(app.strata_estimate(20000.0, 200.0) == 14000.0, "ACTIVE: latched value; apps opened later not booked")
        app.state_name = "STOPPING"
        check(app.strata_estimate(34000.0, 200.0) == 6000.0, "STOPPING: shrinks as memory returns")
        check(app.strata_estimate(45000.0, 200.0) == 0.0, "STOPPING: never negative")
        app.state_name, app._avail_at_launch, app._strata_est = "ACTIVE", None, None
        check(app.strata_estimate(30000.0, 200.0) is None, "adopted, tiny RSS: unknown ('–'), not a wrong 0.2 GB")
        check(app.strata_estimate(30000.0, 5000.0) == 5000.0, "adopted, real RSS: use it")
        app._set_state("IDLE")
        check(app._avail_at_launch is None and app._strata_est is None, "IDLE clears the baselines")
    finally:
        app.state_name, app._avail_at_launch, app._strata_est = keep
        app._set_state("IDLE")


def run() -> int:
    dialogs = Dialogs()
    dialogs.install()
    try:
        test_threading_and_import()
        app, det, ep = new_app()
        try:
            for fn, args in ((test_layout_idle_preview, (app, det, ep)), (test_on_flow, (app, det, ep)),
                             (test_off_flow, (app, det, ep, dialogs)), (test_error_and_cancel, (app, det, ep)),
                             (test_sweep_dialog_close_x, (app, det, ep)), (test_whitelist_modal, (app, det, ep)),
                             (test_load_progress_view, (app, det, ep)), (test_strata_estimate, (app, det, ep))):
                print(f"-- {fn.__name__}")
                try:
                    fn(*args)
                except Exception:
                    FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
                    print(FAILS[-1])
        finally:
            if not app._destroyed:
                app.destroy()
        print("-- test_delete_window")
        try:
            test_delete_window(dialogs)
        except Exception:
            FAILS.append(f"test_delete_window raised:\n{traceback.format_exc()}")
            print(FAILS[-1])
    finally:
        dialogs.restore()
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
