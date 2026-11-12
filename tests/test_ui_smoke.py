"""UI smoke test for Hermes Controller (stub backends only — never touches real processes or port 8080).

Run:  .venv\\Scripts\\python.exe tests\\test_ui_smoke.py
Exit code 0 = all checks passed. Screenshots land in tests\\ui_<state>.png.
"""
from __future__ import annotations

import ctypes
import gc
import json
import math
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# Settings/state written by the UI under test go to a throwaway folder - never the project's config.json /
# state.json and never %APPDATA%\HermesController. Must be set before config_store is imported.
HOME = Path(tempfile.mkdtemp(prefix="hc-ui-home-"))
os.environ["HERMES_CONTROLLER_HOME"] = str(HOME)

import tkinter.messagebox as tk_messagebox  # noqa: E402

import customtkinter as ctk  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import main  # noqa: E402  (must import without detector.py / server_manager.py)
import config_store  # noqa: E402
import settings_dialog  # noqa: E402
import theme as T  # noqa: E402
from main import HermesControllerApp, PILL_COLORS, LOADING_COLOR  # noqa: E402
from settings_dialog import SettingsDialog  # noqa: E402
from sweep_dialog import SweepDialog  # noqa: E402
from vram_sampler import VramSnapshot  # noqa: E402
from whitelist_modal import WhitelistModal  # noqa: E402
import reopen  # noqa: E402
import webbrowser  # noqa: E402

LAUNCHED: list[str] = []                         # reopen.launch is faked for the whole run: nothing real is started
OPENED_URLS: list[str] = []
reopen.launch = lambda rec: (LAUNCHED.append(reopen._coerce(rec).name) or True, "started (test)")
reopen.already_running = lambda rec: False
webbrowser.open = lambda url, *a, **k: OPENED_URLS.append(url) or True

assert config_store.DATA_DIR == HOME, "tests must not write to the real data dir"

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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    assert p != 8080
    return p


# Never 8080: even though the metrics fetcher below is a stub, nothing in this test may be ABLE to reach the real
# Strata port. The port is free (nothing listens), and the URL is only ever displayed.
TEST_PORT = _free_port()
CFG = {"endpoint_url": f"http://127.0.0.1:{TEST_PORT}/v1", "port": TEST_PORT, "model_name": "swift-1.5-iq3_s",
       "user_whitelist": [], "game_dirs": ["steamapps"]}


class FakeVram:
    """Stands in for VramSampler: no thread, no NVML; the test sets what the GPU 'reports'."""
    window_s = 300.0

    def __init__(self) -> None:
        self.available: bool | None = False
        self.total_mb = 16384.0
        self.samples: list[tuple[float, float]] = []
        self.pushed: list[tuple[float, float]] = []
        self.started = self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def push_external(self, used_mb: float, total_mb: float) -> bool:
        self.pushed.append((used_mb, total_mb))
        return False

    def snapshot(self) -> VramSnapshot:
        return VramSnapshot(tuple(self.samples), self.total_mb if self.available else None, self.available,
                            "nvml" if self.available else "", time.monotonic())

    def fill(self, strata_since_s: float = 150.0) -> None:
        """5 minutes of plausible data: ~3 GB of desktop use, then Strata's cache fills to ~12 GB."""
        now = time.monotonic()
        self.samples = []
        for i in range(150):
            age = (149 - i) * 2.0
            base = 3000 + 260 * math.sin(i / 9.0) + 90 * math.sin(i / 2.3)
            ramp = min(1.0, max(0.0, (strata_since_s - age) / 40.0))
            self.samples.append((now - age, base + ramp * 9100 + (120 * math.sin(i / 4.0) if ramp >= 1 else 0)))
        self.available = True


class FakeMetrics:
    """Stands in for metrics_store.fetch_metrics: returns a Strata-shaped /metrics document, counts the polls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.requests: list[dict] = []          # oldest first
        self.live = "idle"
        self.queued = 0
        self.fail = False

    def add(self, n: int) -> None:
        for _ in range(n):
            i = len(self.requests)
            hit = i % 4 == 3
            total = 1800 + 350 * (i % 7)
            read = 12 if hit else total - 200 * (i % 3)
            gen = 0 if i % 9 == 8 else 120 + 40 * (i % 6)
            tok_s = round(88.0 + 4.1 * ((i * 7) % 6) + (i % 3), 1) if gen >= 2 else None
            self.requests.append({
                "id": f"req{i:09d}", "time": 1000.0 + i, "duration_s": 2.4, "finish": "stop", "prompt_tokens": total,
                "reused": total - read, "output_tokens": gen, "prompt_total": total, "prompt_read": read,
                "prompt_ms": 9.0 if hit else round(read / (3150.0 + 130 * (i % 5)) * 1000, 1),
                "decode_ms": round(gen / tok_s * 1000, 1) if tok_s else None, "decode_tok_s": tok_s, "hit_rate": 0.93})

    def __call__(self, root_url: str, all_requests: bool = False) -> dict | None:
        self.calls.append(root_url)
        self.all_calls = getattr(self, "all_calls", 0) + (1 if all_requests else 0)
        if self.fail:
            return None
        st = self.live
        busy = st in ("generating", "reading")
        # a real read advances between polls (the app ignores a stale prefill mean until it does)
        self._read = min(2400, getattr(self, "_read", 0) + 900) if st == "reading" else 0
        live = {"state": st, "queued": self.queued, "tok_s": 101.3 if st == "generating" else None,
                "generated": 412 if st == "generating" else None, "max_tokens": 32768 if busy else None,
                "phase": "thinking" if st == "generating" else None, "prompt_tokens": 2400 if busy else None,
                "tok_s_mean": 99.8 if st == "generating" else None,
                "prefill_tok_s_mean": 3650.0 if busy else None, "prompt_read": self._read if st == "reading" else None,
                "prompt_total": 2400 if st == "reading" else None}
        return {"engine": {"model": "swift-test-1"}, "live": live,
                "requests": self.requests[::-1] if all_requests else self.requests[::-1][:12],
                "requests_kept": len(self.requests), "totals": {},
                "hardware": {"gpu_mem_used": 11 * 1024 ** 3, "gpu_mem_total": 16 * 1024 ** 3}, "time": time.time()}


def new_app(cfg: dict | None = None):
    det, ep = StubDetector(), StubEndpoint()
    app = HermesControllerApp(detector=det, endpoint=ep, config=dict(cfg or CFG), metrics_fetcher=FakeMetrics(),
                              vram_sampler=FakeVram())
    app.attributes("-topmost", True)
    pump(app, 0.4)
    return app, det, ep


def status(app) -> str:
    """Everything the status area says: the status-line summary, then the detail line (shown or not)."""
    head, detail = app.summary.cget("text"), app.preview.cget("text")
    return head + ("\n" + detail if detail else "") if head else detail


def detail_shown(app) -> bool:
    return app.preview.winfo_manager() == "grid"


def expected_h(app) -> int:
    """Logical window height for what is currently laid out (the window sizes itself to its content)."""
    h = main.HEIGHT + (main.DETAIL_H if detail_shown(app) else 0)
    h += main.PROGRESS_H if app.progress.winfo_manager() == "grid" else 0
    return h + (main.STATS_GAP + main.STATS_H if app.expanded else 0)


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
    check(not detail_shown(app) and abs(w - 360 * scale) <= 2 and abs(h - main.HEIGHT * scale) <= 2,
          f"compact window {w}x{h} != 360x{main.HEIGHT}*{scale}")
    check(app.resizable() == (False, False), "window must be non-resizable")
    tw, th = app.toggle.winfo_width(), app.toggle.winfo_height()
    check(abs(tw - 180 * wscale) <= 2 and abs(th - 56 * wscale) <= 2, f"toggle {tw}x{th} != 180x56*{wscale}")
    check(pill(app) == ("Idle", PILL_COLORS["IDLE"]), f"idle status {pill(app)}")
    check(app.toggle.cget("text") == "Turn on", "IDLE toggle text 'Turn on'")

    ok = pump_until(app, lambda: "to close" in status(app))
    txt = status(app)
    print("IDLE preview:", txt.replace("\n", " | "))
    check(ok and app.summary.cget("text") == "2 apps to close · 6.8 GB", f"preselected summary: {txt!r}")
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
    check(not detail_shown(app) and app.caret.cget("text") == T.CHEVRON_DOWN,
          f"idle detail hidden behind a caret ({app.caret.cget('text')!r})")
    check(app.summary.winfo_rootx() + app.summary.winfo_width() <= app.gear.winfo_rootx(), "summary runs into the gear")
    # click the status line: the detail line opens, 10 px from the button, and the window grows by DETAIL_H
    app._on_head_click()
    pump(app, 0.2)
    wscale = ctk.ScalingTracker.get_widget_scaling(app)
    gap = app.toggle.winfo_rooty() - (app.preview.winfo_rooty() + app.preview.winfo_height())
    check(detail_shown(app) and abs(app.winfo_height() - (main.HEIGHT + main.DETAIL_H) * scale) <= 2,
          f"opened detail: window {app.winfo_height()}")
    check(abs(gap - main.GAP_AFTER_DETAIL * wscale) <= 2, f"detail -> button gap {gap}")
    check(abs(app.preview.winfo_rootx() - app.pill.winfo_rootx()) <= 2,
          f"detail aligned with the state text ({app.preview.winfo_rootx()} vs {app.pill.winfo_rootx()})")
    shot(app, "idle_detail_open")
    app._flush_state() if app._state_save_id else None
    check(pump_until(app, lambda: load_state_file().get("detail_open") is True, 3), "detail_open saved")
    app._on_head_click()
    pump(app, 0.2)
    check(not detail_shown(app) and abs(app.winfo_height() - main.HEIGHT * scale) <= 2, "click again hides it")
    check(app.toggle.winfo_rooty() + app.toggle.winfo_height() <= app.gauge.winfo_rooty() - 16,
          "button overlaps memory card")
    check(app.telemetry.cget("text") == "", f"telemetry empty until something is freed ({app.telemetry.cget('text')!r})")
    check(pump_until(app, lambda: "Free" in legend[2].cget("text") and "GB" in legend[2].cget("text")),
          f"memory legend filled ({legend[2].cget('text')!r})")
    shot(app, "idle")

    det.preselected_mode = False
    app._request_preview()
    ok = pump_until(app, lambda: "could close" in status(app))
    txt = status(app)
    print("IDLE preview (none preselected):", txt.replace("\n", " | "))
    check(ok and app.summary.cget("text") == "4 apps could close · 10.4 GB", f"all-ticked: {txt!r}")
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
    check(abs(dsw - 372 * scale) <= 2 and abs(dsh - (320 + SweepDialog.REOPEN_H) * scale) <= 2, f"SweepDialog size {dsw}x{dsh}")
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
    ok = pump_until(app, lambda: app.state_name == "ACTIVE" and status(app).startswith("swift-test-1\n"))
    check(ok, f"on_ready -> ACTIVE 'swift-test-1' (got {app.state_name}, {status(app)!r})")
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
    check("port still in use" in status(app), "stop failure text")
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
    short = "Strata exited (code 1)\nOpen strata-20261112-050000.log"
    check(status(app) == short and app.toggle.cget("text") == "Turn on",
          f"ERROR text shortened to the log file name + Turn on ({status(app)!r})")
    check(app._error_log == "C:\\logs\\strata-20261112-050000.log" and str(app.preview.cget("cursor")) == "hand2",
          "error line links to the full log path")
    app._error_hold_until = 0.0              # even after the hold expires...
    app._request_preview()
    pump(app, 0.3)
    check(status(app) == short, "ERROR keeps its message until the user acts (no preview overwrite)")
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
    check(not app._destroyed and "please wait" in status(app), "STOPPING close ignored + note")
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
        ok = pump_until(app, lambda: "Loading experts into RAM" in status(app), 5)
        txt = status(app)
        check(ok and "about 45 GB" in txt, f"phase from log shown ({txt!r})")
        check(app.elapsed.cget("text").count(":") == 1, f"elapsed timer shown ({app.elapsed.cget('text')!r})")
        check(0.25 <= app.progress.get() <= 0.78, f"progress within the experts phase ({app.progress.get():.2f})")
        check(app.progress.cget("progress_color") == PILL_COLORS["ACTIVE"], "progress drawn in the accent")
        shot(app, "loading_progress")
        with open(logf, "a", encoding="utf-8") as f:
            f.write("[strata] filling the GPU's expert cache (1095 experts, 2.11 GiB of VRAM) ...\n")
        ok = pump_until(app, lambda: "Filling GPU cache" in status(app), 5)
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


def test_low_ram(app, det, ep) -> None:
    """Full-speed RAM check in the start dialog + Low-RAM (SSD) warnings while loading and serving."""
    import shutil
    import tempfile
    import ram_plan
    import theme as TH
    from main import LOW_RAM_NOTE as LOW_RAM_NOTE_TXT
    d = Path(tempfile.mkdtemp(prefix="hc-lowram-"))
    eng = d / "strata-test.log"
    shutil.copy(Path(__file__).parent / "fixtures" / "engine_logs" / "low_ram_iq3_s.log", eng)  # learned arena 46.84
    cfg_json = d / "strata-test.json"
    cfg_json.write_text('{"log": "%s"}' % str(eng).replace("\\", "\\\\"), encoding="utf-8")
    old_cmd = app.cfg.get("launch_cmd")
    app.cfg["launch_cmd"] = ["python.exe", "server.py", "--config", str(cfg_json), "--port", str(TEST_PORT)]
    try:
        check(app._strata_config_path() == str(cfg_json), "config path from launch_cmd --config")
        plan = app._ram_plan([dict(c) for c in CANDS])
        check(plan is not None and abs(plan.need_mb - (46.84 + ram_plan.DEFAULT_HEADROOM_GIB) * 1024) < 1, f"plan uses the learned arena ({plan})")

        cands = [dict(c) for c in CANDS]
        total = sum(c["ram_mb"] for c in cands)
        need = (46.84 + ram_plan.DEFAULT_HEADROOM_GIB) * 1024
        # 1) short but reachable: the plan's pre-ticks replace the detector's, the line confirms full speed
        pl = ram_plan.plan(cands, need - total + 100, 46.84)
        rec = []
        dlg = SweepDialog(app, cands, lambda dd, t: rec.append((dd, t)), ram=pl)
        pump(app, 0.3)
        ticked = {int(c["pid"]) for v, c in dlg._rows if v.get()}
        check(ticked == set(pl.preticked_pids), f"dialog pre-ticks follow the RAM plan ({ticked} vs {pl.preticked_pids})")
        check(dlg._ram_head.cget("text").startswith("Full speed needs ~"), f"RAM summary ({dlg._ram_head.cget('text')!r})")
        check(dlg._ram_live.cget("text").startswith("Ticked apps free ~"), f"enough once closed ({dlg._ram_live.cget('text')!r})")
        shot(dlg, "sweep_ram_check")
        for v, c in dlg._rows:
            if v.get():
                v.set(False)
                break
        dlg._update_buttons()
        check(dlg._ram_live.cget("text").startswith("Still ~") and "Low-RAM mode" in dlg._ram_live.cget("text"),
              f"unticking shows the gap ({dlg._ram_live.cget('text')!r})")
        dlg._decide("cancel")
        # 2) enough already
        dlg = SweepDialog(app, cands, lambda dd, t: None, ram=ram_plan.plan(cands, 60 * 1024, 46.84))
        pump(app, 0.2)
        check(dlg._ram_head.cget("text") == "Enough RAM for full speed" and "full-RAM mode" in dlg._ram_live.cget("text"),
              "enough RAM wording")
        check({int(c["pid"]) for v, c in dlg._rows if v.get()} == {41001, 41002}, "enough: detector pre-ticks kept")
        dlg._decide("cancel")
        # 3) unreachable
        dlg = SweepDialog(app, cands, lambda dd, t: None, ram=ram_plan.plan(cands, 10 * 1024, 46.84))
        pump(app, 0.2)
        check("will load slower" in dlg._ram_live.cget("text"), f"unreachable wording ({dlg._ram_live.cget('text')!r})")
        dlg._decide("cancel")
        # 4) no plan (requirement unknown) = v1.0 dialog, no extra rows
        dlg = SweepDialog(app, cands, lambda dd, t: None, ram=None)
        pump(app, 0.2)
        check(dlg._ram_head is None, "no RAM line without a plan")
        dlg._decide("cancel")

        # 5) live load: waiting for RAM -> Low-RAM fallback -> serving with the amber indicator
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        eng.write_text(f"[strata] {stamp} engine started: C:\\x\\strata.exe --serve\n"
                       "strata generate: the expert arena needs 46.84 GiB of RAM and 39.53 GiB is available; waiting "
                       "up to 20 s for it (another engine still exiting?) - 0 s so far (X)\n", encoding="utf-8")
        logf = d / "server-stdout.log"
        logf.write_text("[strata] loading the experts into RAM (about 45 GB) and locking part of them for the GPU.\n",
                        encoding="utf-8")
        det.cands = []
        ep.status_value = dict(IDLE_STATUS)
        app._set_state("IDLE")
        real_status = ep.status
        ep.status = lambda: {**real_status(), "log_path": str(logf)}
        try:
            app.on_toggle()
            ok = pump_until(app, lambda: "Waiting for RAM" in status(app), 6)
            txt = status(app)
            check(ok and "needs 47 GB, 40 GB free" in txt, f"waiting phase from the engine log ({txt!r})")
            check(app.preview.cget("text_color") == TH.AMBER, "waiting shown in amber")
            with open(eng, "a", encoding="utf-8") as fh:
                fh.write("strata generate: not enough RAM for the expert arena (it needs 46.84 GiB and 39.52 GiB is "
                         "available (7.32 GiB short, after waiting)): this run reads the experts from the file\n"
                         "strata generate: experts via mmap (not enough RAM for the expert arena)\n")
            ok = pump_until(app, lambda: status(app).startswith(
                "Low-RAM mode\nreading experts from SSD · slower"), 6)
            check(LOW_RAM_NOTE_TXT not in status(app), "no duplicate RAM note under the fallback line")
            check(ok, f"fallback shown while loading ({status(app)!r})")
            shot(app, "loading_low_ram")
            ep.status_value = active_status()
            fire(ep.start_calls[-1][0])                  # on_ready
            check(pump_until(app, lambda: app.state_name == "ACTIVE"), "ready -> ACTIVE")
            ok = pump_until(app, lambda: app.summary.cget("text") == "Low-RAM mode", 4)
            check(ok and pill(app) == ("Serving", TH.AMBER) and detail_shown(app),
                  f"serving shows Low-RAM in amber, detail forced open ({pill(app)}, {status(app)!r})")
            txt = status(app)
            check("Reading from SSD (slower) · ~7.3 GB short" in txt, f"serving detail ({txt!r})")
            shot(app, "serving_low_ram")
        finally:
            ep.status = real_status
            ep.status_value = dict(IDLE_STATUS)
            app.on_status(ep.status()); app.on_status(ep.status())
            pump(app, 0.2)
        check(app.state_name == "IDLE" and app._engine_load is None and pill(app)[0] != "Serving · Low-RAM mode",
              "IDLE clears the Low-RAM verdict")

        # 6) a full-RAM run never shows the warning
        eng.write_text(f"[strata] {time.strftime('%Y-%m-%d %H:%M:%S')} engine started: C:\\x\\strata.exe --serve\n"
                       "strata generate: loaded 46.84 GiB at 6.68 GiB/s\n", encoding="utf-8")
        ep.status = lambda: {**real_status(), "log_path": str(logf)}
        try:
            app.on_toggle()
            pump_until(app, lambda: len(ep.start_calls) > 0 and app._start_phase == "server", 4)
            ep.status_value = active_status()            # the stub server now reports it is up
            fire(ep.start_calls[-1][0])
            check(pump_until(app, lambda: app.state_name == "ACTIVE"), "full run -> ACTIVE")
            pump(app, 1.5)
            check(pill(app)[0] == "Serving" and "Low-RAM" not in status(app),
                  f"full-RAM run: plain Serving ({pill(app)}, {status(app)!r})")
        finally:
            ep.status = real_status
            ep.status_value = dict(IDLE_STATUS)
            app.on_status(ep.status()); app.on_status(ep.status())
            pump(app, 0.2)
    finally:
        if old_cmd is None:
            app.cfg.pop("launch_cmd", None)
        else:
            app.cfg["launch_cmd"] = old_cmd
        det.cands = [dict(c) for c in CANDS]


def test_review_fixes(app, det, ep) -> None:
    """v1.1 review: double-click debounce, launcher behind a foreign port, stale sweep note, metrics catch-up."""
    from types import SimpleNamespace
    # 1) double-click on Turn on must not cancel the start it just began
    main.TOGGLE_DEBOUNCE_S = 0.4
    try:
        ep.status_value = dict(IDLE_STATUS)
        app._set_state("IDLE")
        app._toggle_at = 0.0
        det.cands = [dict(c) for c in CANDS]
        app.on_toggle()
        app.on_toggle()                              # 2nd click of a double-click, same instant
        pump(app, 0.2)
        check(app.state_name == "STARTING" and "cancelled" not in status(app).lower(),
              f"double-click keeps the start ({app.state_name}, {status(app)!r})")
        if pump_until(app, lambda: isinstance(app.dialog, SweepDialog), 3):
            app.dialog._decide("cancel")
        pump_until(app, lambda: app.state_name == "IDLE", 3)
    finally:
        main.TOGGLE_DEBOUNCE_S = 0.0
    # 2) a launcher saved while ANOTHER program holds the old port is applied (not stuck forever)
    applied = []
    real_apply = main.launcher_bat.apply_to_config
    main.launcher_bat.apply_to_config = lambda cfg, info: applied.append(info.bat_name)
    info = SimpleNamespace(bat_name="run-x.bat", bat_path=r"C:\x\run-x.bat", model_name="m", port=8081,
                           summary=lambda: "m · port 8081")
    try:
        ep.status_value = {**IDLE_STATUS, "state": "active"}          # port answers...
        real_prot = ep.protected_pids
        ep.protected_pids = lambda: set()                               # ...but it is not Strata
        app.state_name = "ERROR"
        app._pending_launcher = info
        check(app._apply_pending_launcher(note=True) and applied == ["run-x.bat"] and app._pending_launcher is None,
              f"foreign listener does not block the new launcher ({applied})")
        ep.protected_pids = lambda: {4242}                              # Strata itself holds the old port
        app.state_name = "IDLE"
        app._pending_launcher = info
        check(not app._apply_pending_launcher(note=True) and app._pending_launcher is info,
              "Strata on the old port: the launcher waits")
        check("applies when Strata on port" in status(app), f"and says so ({status(app)!r})")
    finally:
        main.launcher_bat.apply_to_config = real_apply
        ep.protected_pids = real_prot
        app._pending_launcher = None
        ep.status_value = dict(IDLE_STATUS)
        app._set_state("IDLE")
    # 3) a cancelled start's late sweep result does not leak "Could not close" into the next start
    app._sweep_note = ""
    app._after_sweep(app._start_token - 1, {"failed": [{"name": "stuck.exe"}], "ram_freed_mb": 0.0})
    check(app._sweep_note == "", f"stale sweep result ignored ({app._sweep_note!r})")
    # 4) the first poll after a gap asks Strata for ALL kept requests
    fm = app._fetch_metrics
    before = getattr(fm, "all_calls", 0)
    was = app.expanded
    to_active(app, ep)
    if not app.expanded:
        app.toggle_stats()
    pump(app, 2.6)
    check(getattr(fm, "all_calls", 0) > before, f"catch-up fetch with all_requests ({getattr(fm, 'all_calls', 0)})")
    if app.expanded != was:
        app.toggle_stats()
    ep.status_value = dict(IDLE_STATUS)
    app.on_status(ep.status()); app.on_status(ep.status())
    pump(app, 0.2)


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

# ---------------------------------------------------------------- v1.1: stats section
def test_model_state(app, det, ep) -> None:
    """Strata's "Model state" on the status line: Reading (blue, prompt bar), Generating (green), Queued, back to Serving."""
    fetch = app._fetch_metrics
    old_poll = main.LIVE_POLL_S
    main.LIVE_POLL_S = 0.2
    try:
        if app.expanded:
            app.chevron.invoke()
            pump(app, 0.3)
        app.detail_open = False
        fetch.fail, fetch.live, fetch.queued = False, "idle", 0
        to_active(app, ep)
        check(pump_until(app, lambda: pill(app)[0] == "Serving", 3), f"idle model: Serving ({pill(app)})")
        h0 = app.winfo_height()
        fetch.live = "reading"
        ok = pump_until(app, lambda: pill(app)[0] == "Reading" and "/ 2,400" in app.summary.cget("text"), 4)
        check(ok and pill(app)[1] == T.BLUE, f"reading: blue LED + progress text ({pill(app)}, {status(app)!r})")
        check(pump_until(app, lambda: app._act_frac is not None and app.act_track.winfo_ismapped(), 2),
              "reading: activity bar shown")
        pump(app, 0.5)
        check(app.winfo_height() == h0 and not detail_shown(app), "reading adds no height, detail stays closed")
        wscale = ctk.ScalingTracker.get_widget_scaling(app)
        tb = app.act_track.winfo_rooty() + app.act_track.winfo_height()
        check(tb <= app.toggle.winfo_rooty() - 8 * wscale and app.act_track.winfo_rootx() >= app.pill.winfo_rootx() - 1
              and app.act_track.winfo_rootx() + app.act_track.winfo_width() <= app.gear.winfo_rootx(),
              "activity bar under the state text, clear of the gear and the button")
        check(app.summary.winfo_rooty() + app.summary.winfo_height() <= app.act_track.winfo_rooty() + 1,
              "bar does not cover the text")
        fill_w = app.act_fill.winfo_width() / max(1, app.act_track.winfo_width())
        check(abs(fill_w - app._act_frac) < 0.05, f"bar width = progress ({fill_w:.2f} vs {app._act_frac})")
        shot(app, "model_reading")
        app._on_head_click()
        pump(app, 0.3)
        check(detail_shown(app) and "swift-test-1" in app.preview.cget("text"), f"detail: model ({status(app)!r})")
        app._on_head_click()
        fetch.live, fetch.queued = "generating", 1
        ok = pump_until(app, lambda: pill(app)[0] == "Generating", 4)
        check(ok and pill(app)[1] == T.ACCENT and app.summary.cget("text") == "412 tokens · 101.3 tok/s"
              and app.preview.cget("text").endswith("· 1 queued"),
              f"generating: tokens + live speed; a queue that does not fit moves to the detail ({status(app)!r})")
        check(app.summary.winfo_rootx() + app.summary.winfo_width() <= app.caret.winfo_rootx() + 1,
              f"long summary is ellipsized before the caret ({app.summary.cget('text')!r})")
        check(pump_until(app, lambda: app._act_frac is None, 2) and not app.act_track.winfo_ismapped(),
              "generating: no bar")
        check("thinking" in app.preview.cget("text"), f"detail carries the phase ({app.preview.cget('text')!r})")
        shot(app, "model_generating")
        fetch.live, fetch.queued = "idle", 2
        check(pump_until(app, lambda: pill(app) == ("Queued", T.AMBER), 4) and app.summary.cget("text") == "2 waiting",
              f"queued ({pill(app)}, {status(app)!r})")
        fetch.queued = 0
        check(pump_until(app, lambda: pill(app)[0] == "Serving" and app.summary.cget("text") == "swift-test-1", 4),
              f"back to Serving ({pill(app)}, {status(app)!r})")
        fetch.live = "reading"
        check(pump_until(app, lambda: pill(app)[0] == "Reading", 4), "reading again")
        ep.status_value = dict(IDLE_STATUS)
        app.on_status(ep.status()); app.on_status(ep.status())
        pump(app, 0.3)
        check(app.state_name == "IDLE" and pill(app)[0] == "Idle" and not app.act_track.winfo_ismapped()
              and app.metrics.activity() is None, "Strata gone: activity and bar cleared")
        fetch.live = "idle"
    finally:
        main.LIVE_POLL_S = old_poll


def test_reopen_flow(app, det, ep) -> None:
    """After Turn off: auto apps relaunch (faked launch), games stay as a one-click offer that relaunches them."""
    py = os.path.join(sys.base_prefix, "python.exe")
    pyw = os.path.join(sys.base_prefix, "pythonw.exe")
    recs = [{"name": "notes.exe", "exe": py, "cmdline": [py], "cwd": sys.base_prefix, "category": "app",
             "create_time": 1.0, "elevated": False, "aumid": None},
            {"name": "eldenring.exe", "exe": pyw, "cmdline": [pyw], "cwd": sys.base_prefix, "category": "game",
             "create_time": 2.0, "elevated": False, "aumid": None},
            {"name": "admin.exe", "exe": sys.executable, "cmdline": [sys.executable], "cwd": "", "category": "app",
             "create_time": 3.0,
             "elevated": True, "aumid": None}]
    old_hold, old_stagger = main.NOTE_HOLD_S, main.REOPEN_STAGGER_S
    main.NOTE_HOLD_S, main.REOPEN_STAGGER_S = 0.6, 0.05
    try:
        # the sweep dialog offers the choice; unticking is remembered
        dlg_ref = {}
        d = SweepDialog(app, det.cands, lambda dec, t: dlg_ref.setdefault("dec", dec), reopen_default=True)
        pump(app, 0.3)
        check(d._reopen_var is not None and d._reopen_var.get() is True, "start dialog: reopen ticked by default")
        shot(d, "sweep_reopen")
        d._reopen_var.set(False)
        app._on_sweep_decision(app._start_token, "cancel", [], d)      # cancel: the preference is not changed
        check(app._reopen_pref is True, "cancel keeps the reopen preference")
        d._decide("cancel")
        pump(app, 0.3)

        app._set_state("IDLE")
        LAUNCHED.clear()
        main._save_reopen_records(recs[:1])
        main._save_reopen_records(recs[1:])                            # a 2nd sweep merges, does not replace
        check(len(reopen.load_pending()) == 3, f"pending merged ({len(reopen.load_pending())})")
        app._reopening = True
        app._run_worker("hc-reopen", app._reopen_worker)
        check(pump_until(app, lambda: not app._reopening, 5), "reopen worker finished")
        check(LAUNCHED == ["notes.exe"], f"only the plain app was relaunched ({LAUNCHED})")
        check("Reopened 1 app" in status(app), f"note says so ({status(app)!r})")
        left = [r.name for r in reopen.load_pending()]
        check(left == ["eldenring.exe"], f"the game stays pending, the admin app is dropped ({left})")
        ok = pump_until(app, lambda: "Reopen eldenring" in status(app), 4)
        check(ok and detail_shown(app) and app._detail_link and str(app.preview.cget("cursor")) == "hand2",
              f"idle shows a clickable 'Reopen eldenring' ({status(app)!r})")
        shot(app, "reopen_offer")
        app.preview._label.event_generate("<Button-1>")
        check(pump_until(app, lambda: "eldenring.exe" in LAUNCHED, 4), f"click relaunches the game ({LAUNCHED})")
        check(pump_until(app, lambda: not reopen.load_pending() and not app._reopen_offer, 3), "offer cleared")
        # an offer left over from an earlier session is shown at startup; turning on clears it
        main._save_reopen_records(recs[1:2])
        app._run_worker("hc-reopen-load", app._load_reopen_offer)
        check(pump_until(app, lambda: bool(app._reopen_offer), 3), "pending apps from earlier are offered")
        reopen.clear_pending()
        app._reopen_offer = []
        app._request_preview()
        pump(app, 0.4)
    finally:
        main.NOTE_HOLD_S, main.REOPEN_STAGGER_S = old_hold, old_stagger


def load_state_file() -> dict:
    try:
        return json.loads((HOME / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def canvas_text_clipped(canvas) -> list[str]:
    """Texts of canvas items whose bounding box leaves the canvas (i.e. would be cut off)."""
    canvas.update_idletasks()
    w, h = canvas.winfo_width(), canvas.winfo_height()
    bad = []
    for item in canvas.find_all():
        if canvas.type(item) != "text":
            continue
        x0, y0, x1, y1 = canvas.bbox(item)
        if x0 < -1 or y0 < -1 or x1 > w + 1 or y1 > h + 1:
            bad.append(f"{canvas.itemcget(item, 'text')!r} bbox {(x0, y0, x1, y1)} in {w}x{h}")
    return bad


def check_stats_fit(app, label: str) -> None:
    """Nothing in the expanded section is clipped: graph texts inside their canvas, header labels inside the row,
    the card inside the window with the same 16 px margin as everywhere else."""
    app.update_idletasks()
    scale = ctk.ScalingTracker.get_window_scaling(app)
    wscale = ctk.ScalingTracker.get_widget_scaling(app)
    w, h = app.winfo_width(), app.winfo_height()
    check(abs(w - 360 * scale) <= 2 and abs(h - expected_h(app) * scale) <= 2,
          f"{label}: expanded window {w}x{h} != 360x{expected_h(app)}*{scale}")
    bottom = app.winfo_rooty() + h
    card_bottom = app.stats.winfo_rooty() + app.stats.winfo_height()
    # same bottom margin as the collapsed v1.0 layout (16 px; its fixed-height rows round up to 3 px slack at 150 %)
    check(-2 <= (bottom - card_bottom) - 16 * wscale <= 4, f"{label}: stats card ends 16 px above the window edge "
          f"({bottom - card_bottom})")
    gap = app.stats.winfo_rooty() - (app.mem_card.winfo_rooty() + app.mem_card.winfo_height())
    check(abs(gap - 8 * wscale) <= 2, f"{label}: 8 px between memory card and stats card ({gap})")
    for name, row in (("vram", app.row_vram), ("decode", app.row_decode), ("prompt", app.row_prompt)):
        clipped = canvas_text_clipped(row.graph)
        check(not clipped, f"{label}: {name} graph text clipped: {clipped}")
        check(row.winfo_rooty() + row.winfo_height() <= card_bottom + 1, f"{label}: {name} row inside the card")
        right = row.winfo_rootx() + row.winfo_width()
        card_right = app.stats.winfo_rootx() + app.stats.winfo_width()
        check(row.value.winfo_rootx() + row.value.winfo_width() <= card_right - 10 * wscale
              and right <= card_right - 10 * wscale and row.title.winfo_rootx() + row.title.winfo_reqwidth() <= row.tag.winfo_rootx() + 1,
              f"{label}: {name} header (title / value) does not overlap or clip")
        ch = row.graph.winfo_height()
        check(abs(ch - 40 * wscale) <= 2, f"{label}: {name} canvas is 40 px * scale ({ch})")


def graph_texts(row) -> list[str]:
    g = row.graph
    return [g.itemcget(i, "text") for i in g.find_all() if g.type(i) == "text"]


def test_stats_section(app, det, ep) -> None:
    vram, fetch = app.vram, app._fetch_metrics
    scale = ctk.ScalingTracker.get_window_scaling(app)
    ep.status_value = dict(IDLE_STATUS)
    app._set_state("IDLE")
    pump(app, main.LIVE_POLL_S + 0.3)        # earlier tests served: let a poll already in flight land
    fetch.calls.clear()
    fetch.all_calls = 0
    check(not app.expanded and not app.stats.winfo_ismapped(), "starts collapsed (no state.json)")
    check(app.chevron.cget("text") == T.CHEVRON_DOWN, "collapsed chevron points down")
    check(abs(app.winfo_height() - expected_h(app) * scale) <= 2,
          f"collapsed height fits the content ({app.winfo_height()} vs {expected_h(app)})")
    check(vram.started == 1, "VRAM sampler started with the app")
    cx = app.chevron.winfo_rootx() + app.chevron.winfo_width()
    check(cx <= app.mem_card.winfo_rootx() + app.mem_card.winfo_width() - 8 * scale
          and app.chevron.winfo_height() <= app.mem_title.winfo_height() + 1,
          "chevron sits inside the memory card header without growing it")
    pump(app, 2.6)
    check(fetch.calls == [], f"IDLE: /metrics is never polled ({len(fetch.calls)})")

    # ---- expand: IDLE, no NVML -> every graph shows its empty state
    app.chevron.invoke()
    pump(app, 0.3)
    check(app.expanded and app.stats.winfo_ismapped() and app.chevron.cget("text") == T.CHEVRON_UP,
          "chevron expands the window downward")
    check(pump_until(app, lambda: load_state_file().get("stats_expanded") is True, 3),
          "expanded state persisted in state.json (debounced save)")
    check_stats_fit(app, "empty")
    check(graph_texts(app.row_vram) == ["VRAM unavailable"], f"NVML missing -> 'VRAM unavailable' "
          f"({graph_texts(app.row_vram)})")
    check(graph_texts(app.row_decode) == ["Strata is off"] and graph_texts(app.row_prompt) == ["Strata is off"],
          f"IDLE empty states ({graph_texts(app.row_decode)}, {graph_texts(app.row_prompt)})")
    check(app.row_decode.value.cget("text") == "–" and app.row_vram.value.cget("text") == "–", "no values yet -> –")
    check(app.toggle.winfo_rooty() == app.toggle.winfo_rooty() and app.gauge.winfo_ismapped(), "v1.0 part untouched")
    shot(app, "stats_empty_idle")
    pump(app, 2.4)
    check(fetch.calls == [], "expanded but IDLE: /metrics is not polled")

    # ---- ACTIVE, server has no finished requests yet
    vram.fill(strata_since_s=6.0)
    to_active(app, ep)
    check(pump_until(app, lambda: app.metrics.polls >= 1, 6), "ACTIVE + expanded -> /metrics polled off the Tk thread")
    check(fetch.calls and fetch.calls[0] == f"http://127.0.0.1:{TEST_PORT}", f"polls the server root ({fetch.calls[:1]})")
    check(graph_texts(app.row_decode) == ["No requests yet"] and graph_texts(app.row_prompt) == ["No prompt reads yet"],
          f"ACTIVE empty states ({graph_texts(app.row_decode)}, {graph_texts(app.row_prompt)})")
    check(app.row_vram.value.cget("text").endswith("/ 16.0 GB") and "16 GB" in graph_texts(app.row_vram),
          f"VRAM current value + max-scale label ({app.row_vram.value.cget('text')}, {graph_texts(app.row_vram)})")
    check(vram.pushed and abs(vram.pushed[-1][1] - 16384.0) < 1, "Strata's hardware block offered as VRAM fallback")
    check_stats_fit(app, "no-requests")
    shot(app, "stats_empty_active")

    # ---- ACTIVE with data: 12 per poll are accumulated to 30; live decode while generating
    vram.fill(strata_since_s=170.0)
    app._spans = [[time.monotonic() - 170.0, None]]           # as if Strata had been started 170 s ago
    fetch.add(12)
    fetch.live = "generating"
    for _ in range(4):
        n = len(app.metrics)
        check(pump_until(app, lambda: len(app.metrics) > n or len(app.metrics) == 30, 6), "poll picked up new requests")
        fetch.add(10)
    check(pump_until(app, lambda: len(app.metrics) == 30, 8), f"accumulated to the last 30 requests ({len(app.metrics)})")
    dec, pr = app.metrics.decode_series(), app.metrics.prompt_series()
    check(0 < len(pr) < len(dec) <= 30, f"cache hits / empty answers skipped per series ({len(dec)} decode, {len(pr)} prompt)")
    check(app.row_decode.value.cget("text") == "101.3 tok/s" and app.row_decode.tag.cget("text") == "live"
          and app.row_decode.value.cget("text_color") == T.ACCENT, f"live decode value in the accent "
          f"({app.row_decode.tag.cget('text')} {app.row_decode.value.cget('text')})")
    check(app.row_prompt.tag.cget("text") == "last" and app.row_prompt.value.cget("text").endswith(" tok/s")
          and "," in app.row_prompt.value.cget("text"), f"prompt value = last finished request "
          f"({app.row_prompt.value.cget('text')})")
    check(app.row_decode.graph.scale_max == 120.0 and graph_texts(app.row_decode) == ["120"],
          f"decode scale label ({app.row_decode.graph.scale_max}, {graph_texts(app.row_decode)})")
    check(max(pr) < 4000 and graph_texts(app.row_prompt) == ["4k"], f"prompt scale not blown up by cache hits "
          f"({max(pr):.0f}, {graph_texts(app.row_prompt)})")
    bars = [i for i in app.row_decode.graph.find_all() if app.row_decode.graph.type(i) == "rectangle"
            and app.row_decode.graph.itemcget(i, "fill") in (T.APPS_SEG, T.TEXT_2, T.ACCENT)]
    check(len(bars) == min(30, len(dec) + 1) and app.row_decode.graph.itemcget(bars[-1], "fill") == T.ACCENT,
          f"one bar per request + an accent live bar ({len(bars)} bars for {len(dec)} requests)")
    check_stats_fit(app, "active")
    shot(app, "stats_active")

    fetch.live = "reading"
    check(pump_until(app, lambda: app.row_prompt.tag.cget("text") == "live", 6)
          and app.row_prompt.value.cget("text") == "3,650 tok/s" and app.row_decode.tag.cget("text") == "last",
          f"reading: live prompt speed, decode back to 'last' ({app.row_prompt.value.cget('text')})")
    shot(app, "stats_reading")
    fetch.live = "idle"
    pump_until(app, lambda: app.row_prompt.tag.cget("text") == "last", 6)

    # ---- metrics endpoint failing: data stays, live value goes
    fetch.fail = True
    check(pump_until(app, lambda: app._metrics_fails >= 3, 12), "failed polls counted")
    check(len(app.metrics) == 30 and "Metrics unavailable" not in graph_texts(app.row_decode),
          "failures keep the bars already collected")
    fetch.fail = False
    pump_until(app, lambda: app._metrics_fails == 0, 6)

    # ---- collapse: exact v1.0 size again, polling stops, state persisted
    app.chevron.invoke()
    pump(app, 0.3)
    check(not app.expanded and not app.stats.winfo_ismapped() and abs(app.winfo_height() - expected_h(app) * scale) <= 2
          and abs(app.winfo_width() - 360 * scale) <= 2,
          f"collapse -> 360x{expected_h(app)} ({app.winfo_width()}x{app.winfo_height()})")
    check(pump_until(app, lambda: load_state_file().get("stats_expanded") is False, 3), "collapsed state persisted")
    card_bottom = app.mem_card.winfo_rooty() + app.mem_card.winfo_height()
    check(abs((app.winfo_rooty() + app.winfo_height() - card_bottom) - 16 * scale) <= 2, "memory card margin restored")
    pump(app, 0.3)
    n = len(fetch.calls)
    pump(app, 2.6)
    check(len(fetch.calls) >= n + 2, f"collapsed + serving: still polled for the model state ({len(fetch.calls) - n})")
    shot(app, "collapsed_active")

    # ---- Strata stops: bars of the finished session stay, live values clear, a new start resets them
    app.chevron.invoke()
    pump(app, 0.2)
    ep.status_value = dict(IDLE_STATUS)
    app.on_status(ep.status()); app.on_status(ep.status())
    pump(app, 0.2)
    check(app.state_name == "IDLE" and len(app.metrics) == 30 and app.row_decode.tag.cget("text") == "last",
          "after OFF the last session's speeds stay readable")
    check(app._spans and app._spans[-1][1] is not None, "Strata-running span closed on IDLE")
    det.cands = []
    n_start = len(ep.start_calls)
    app.on_toggle()
    check(pump_until(app, lambda: len(ep.start_calls) == n_start + 1), "start for the reset check")
    check(len(app.metrics) == 0 and app._spans[-1][1] is None, "a new start clears the old server's requests")
    fire(ep.start_calls[-1][1], "x")
    pump_until(app, lambda: app.state_name == "ERROR")
    app._set_state("IDLE")
    det.cands = [dict(c) for c in CANDS]
    app.chevron.invoke()                                       # leave collapsed for the following tests
    pump(app, 0.2)
    check(not app.expanded, "collapsed again")


def test_notes_and_stale_status(app, det, ep) -> None:
    """Review fixes: result notes stay readable; a monitor reading taken before a state change is dropped."""
    ep.status_value = dict(IDLE_STATUS)
    app._set_state("IDLE")
    to_active(app, ep)
    app.on_toggle()
    ep.status_value = dict(IDLE_STATUS)
    seq_before_stop = app._state_seq
    fire(ep.stop_calls[-1][0], stop_result())
    check(pump_until(app, lambda: app.state_name == "IDLE"), "OFF -> IDLE (note test)")
    n = len(det.sweep_calls)
    pump(app, 1.5)
    check(status(app) == "Stopped · freed 2.0 GB" and len(det.sweep_calls) == n,
          f"'Stopped · freed' note is held, not replaced by the sweep preview at once ({status(app)!r})")
    app._on_monitor_status(seq_before_stop, active_status())      # probed while Strata was still up
    check(app.state_name == "IDLE", "stale 'active' monitor reading after OFF is ignored")
    check(pump_until(app, lambda: "to close" in status(app), 6),
          f"sweep preview returns when the note expires ({status(app)!r})")
    app._on_monitor_status(app._state_seq, active_status())       # a current reading still reconciles
    check(app.state_name == "ACTIVE", "fresh monitor reading IDLE -> ACTIVE")
    ep.status_value = dict(IDLE_STATUS)
    app.on_status(ep.status()); app.on_status(ep.status())
    pump(app, 0.1)
    check(app.state_name == "IDLE" and status(app) == "Endpoint is no longer running",
          "ACTIVE -> IDLE note")
    app._set_state("IDLE")


def test_stats_persist_and_scaling() -> None:
    """A saved 'expanded' flag is honoured at startup; the section fits at 100 % and at 150 % scaling."""
    config_store.save_state({"stats_expanded": True})
    base = ctk.ScalingTracker.get_window_scaling
    for factor, name in ((1.0, "100"), (1.5, "150")):
        ctk.set_widget_scaling(factor)
        ctk.set_window_scaling(factor)
        app, det, ep = new_app()
        try:
            check(app.expanded and app.stats.winfo_ismapped(), f"{name} %: starts expanded from state.json")
            app.vram.fill(strata_since_s=200.0)
            app._spans = [[time.monotonic() - 200.0, None]]
            app._fetch_metrics.add(30)
            app._fetch_metrics.live = "generating"
            to_active(app, ep)
            pump_until(app, lambda: len(app.metrics) >= 12, 6)
            app._fetch_metrics.add(30)
            pump_until(app, lambda: len(app.metrics) == 30, 8)
            pump(app, 0.3)
            print(f"scaling x{factor}: window {app.winfo_width()}x{app.winfo_height()}, "
                  f"effective {base(app)}")
            check_stats_fit(app, f"{name} %")
            f_small = app.row_decode.graph._font(T.mono_family(), 9, base(app))
            check(f_small[1] == -max(8, round(9 * base(app))), f"{name} %: canvas fonts scale with DPI ({f_small})")
            shot(app, f"stats_active_{name}")
            # empty states at this scaling too
            app._fetch_metrics.requests.clear()
            app._fetch_metrics.live = "idle"
            app.metrics.reset()
            app.vram.available, app.vram.samples = False, []
            pump_until(app, lambda: graph_texts(app.row_decode) == ["No requests yet"], 6)
            app._refresh_stats()
            check(graph_texts(app.row_vram) == ["VRAM unavailable"]
                  and graph_texts(app.row_prompt) == ["No prompt reads yet"], f"{name} %: empty states")
            check_stats_fit(app, f"{name} % empty")
            shot(app, f"stats_empty_{name}")
            ep.status_value = dict(IDLE_STATUS)
            app.on_status(ep.status()); app.on_status(ep.status())
            pump(app, 0.1)
            app.on_close_request()
            check(app._destroyed and app.vram.stopped == 1, f"{name} %: exit stops the VRAM sampler")
        finally:
            if not app._destroyed:
                app.destroy()
    ctk.set_widget_scaling(1.0)
    ctk.set_window_scaling(1.0)
    config_store.save_state({"stats_expanded": False})


# ---------------------------------------------------------------- v1.1: settings dialog / launcher
def make_strata(root: Path, name: str, ports: dict[str, int]) -> Path:
    """A fake Strata install: serve\\server.py, a python.exe stub file, run-<n>.bat + strata-<n>.json per model."""
    sdir = root / name
    (sdir / "serve").mkdir(parents=True)
    (sdir / ".venv" / "Scripts").mkdir(parents=True)
    (sdir / "serve" / "server.py").write_text("", encoding="utf-8")
    (sdir / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    for n, port in ports.items():
        (sdir / f"strata-{n}.json").write_text(json.dumps({"model_name": f"model-{n}"}), encoding="utf-8")
        (sdir / f"run-{n}.bat").write_text(
            f'@echo off\r\ntitle Strata {n}\r\ncd /d "{sdir}"\r\n"{sdir}\\.venv\\Scripts\\python.exe" '
            f'"{sdir}\\serve\\server.py" "--engine" "strata" "--config" "{sdir}\\strata-{n}.json" "--port" "{port}" '
            f'"--open"\r\nif errorlevel 1 pause\r\n', encoding="utf-8", newline="")
    (sdir / "run-broken.bat").write_text("@echo off\r\necho nothing to see\r\npause\r\n", encoding="utf-8", newline="")
    for other in ("BENCH.bat", "SETUP.bat", "START-HERE.bat", "UPDATE.bat"):
        (sdir / other).write_text("@echo off\r\n", encoding="utf-8", newline="")
    return sdir


def disk_config() -> dict:
    return json.loads((HOME / "config.json").read_text(encoding="utf-8"))


def test_settings_dialog(dialogs: Dialogs) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="hc-ui-strata-"))
    pa, pb, pz = _free_port(), _free_port(), _free_port()
    sdir = make_strata(tmp, "Strata-A", {"a": pa, "b": pb})
    other = make_strata(tmp, "Strata-Z", {"z": pz})
    # a v1.0-style config: relative launcher_bat + hand-written launch_cmd, no launch_cwd
    cfg = dict(CFG, strata_dir=str(sdir), launcher_bat="run-a.bat", port=pa, endpoint_url=f"http://127.0.0.1:{pa}/v1",
               model_name="model-a",
               launch_cmd=[str(sdir / ".venv" / "Scripts" / "python.exe"), str(sdir / "serve" / "server.py"),
                           "--engine", "strata", "--config", str(sdir / "strata-a.json"), "--port", str(pa)])
    (HOME / "config.json").write_text(json.dumps(dict(cfg, user_whitelist=["keep.exe"])), encoding="utf-8")
    app, det, ep = new_app(cfg)
    orig_ask = settings_dialog.filedialog.askopenfilename
    try:
        hwnd = ctypes.windll.user32.FindWindowW("TkTopLevel", main.APP_TITLE)
        check(hwnd and hwnd == ctypes.windll.user32.GetParent(app.winfo_id()),
              "second-instance lookup (class TkTopLevel + title) finds this window")

        # ---- open from the gear
        app.gear.invoke()
        dlg = app.dialog
        check(isinstance(dlg, SettingsDialog), "gear opens the Settings dialog")
        pump(app, 0.4)
        values = list(dlg._menu.cget("values"))
        check(values == ["run-a.bat", "run-b.bat", "run-broken.bat"], f"dropdown lists exactly the run-*.bat ({values})")
        check(dlg._choice.get() == "run-a.bat", f"v1.0 relative launcher_bat preselected ({dlg._choice.get()})")
        check(dlg._detail.cget("text") == f"model-a · strata-a.json · port {pa}"
              and dlg._detail.cget("text_color") == T.TEXT_2, f"read-only parsed line ({dlg._detail.cget('text')!r})")
        check(str(dlg._save_btn.cget("state")) == "normal", "Save enabled for a valid launcher")
        w, h = dlg.winfo_width(), dlg.winfo_height()
        scale = ctk.ScalingTracker.get_window_scaling(dlg)
        check(abs(w - 340 * scale) <= 2 and abs(h - SettingsDialog.HEIGHT * scale) <= 2, f"SettingsDialog size {w}x{h}")
        check(dlg.grab_status() == "local", f"SettingsDialog grab {dlg.grab_status()}")
        check(dlg._link_host.cget("text") == "not running" and dlg._link_text.cget("text_color") == T.TEXT_3,
              f"dashboard link greyed out while Strata is off ({dlg._link_host.cget('text')!r})")
        dlg.open_dashboard()
        check(OPENED_URLS == [], "dashboard link does nothing while Strata is off")
        dlg.copy_api()
        check(dlg.clipboard_get() == f"http://127.0.0.1:{pa}/v1" and dlg._copy_btn.cget("text") == "Copied",
              "Copy puts the API address on the clipboard")
        shot(dlg, "settings_idle")
        bottom = dlg.winfo_rooty() + h
        check(dlg._save_btn.winfo_rooty() + dlg._save_btn.winfo_height() <= bottom - 10 * scale
              and dlg._detail.winfo_rooty() + dlg._detail.winfo_height() <= dlg._protected_btn.winfo_rooty(),
              "settings layout: nothing clipped or overlapping")
        shot(dlg, "settings")

        # ---- a .bat that does not run serve\\server.py: inline error, cannot be saved
        dlg._menu.set("run-broken.bat")
        dlg._on_pick("run-broken.bat")
        check("does not start Strata" in dlg._detail.cget("text") and dlg._detail.cget("text_color") == T.RED_TEXT
              and str(dlg._save_btn.cget("state")) == "disabled", f"broken launcher rejected inline "
              f"({dlg._detail.cget('text')!r})")
        dlg.save()
        pump(app, 0.2)
        check(app._dialog_open() and app.cfg["launcher_bat"] == "run-a.bat", "Save does nothing for a rejected launcher")
        shot(dlg, "settings_error")

        # ---- Cancel keeps everything
        dlg._on_pick("run-b.bat")
        check(dlg._detail.cget("text") == f"model-b · strata-b.json · port {pb}", "switching shows the other model")
        dlg._cancel_btn.invoke()
        pump(app, 0.2)
        check(not app._dialog_open() and app.cfg["port"] == pa and disk_config()["launcher_bat"] == "run-a.bat",
              "Cancel closes without saving")

        # ---- Save while IDLE: written to config.json and applied immediately
        app.gear.invoke()
        dlg = app.dialog
        pump(app, 0.3)
        dlg._choice.set("run-b.bat")
        dlg._on_pick("run-b.bat")
        check(dlg._msg.cget("text") == "", "no 'applies next start' hint while idle")
        dlg._save_btn.invoke()
        check(pump_until(app, lambda: not app._dialog_open()), "Save closes the dialog")
        disk = disk_config()
        bat_b = str(sdir / "run-b.bat")
        check(disk["launcher_bat"] == bat_b and disk["port"] == pb and disk["model_name"] == "model-b"
              and disk["endpoint_url"] == f"http://127.0.0.1:{pb}/v1" and disk["launch_cwd"] == str(sdir)
              and disk["launch_cmd"][-4:] == ["--config", str(sdir / "strata-b.json"), "--port", str(pb)]
              and "--open" not in disk["launch_cmd"], f"config.json holds the full .bat path + derived launch ({disk['launch_cmd'][-4:]})")
        check(disk["user_whitelist"] == ["keep.exe"], "saving the launcher keeps the rest of config.json")
        check(app.cfg["launcher_bat"] == bat_b and app.cfg["port"] == pb and app.cfg["model_name"] == "model-b"
              and app._pending_launcher is None, "IDLE: the shared config switches immediately")
        check("run-b.bat" in status(app), f"idle note names the new launcher ({status(app)!r})")

        # ---- Browse… to a .bat in another Strata folder
        app.gear.invoke()
        dlg = app.dialog
        pump(app, 0.3)
        check(dlg._choice.get() == "run-b.bat", "reopened dialog shows the saved launcher")
        asked = []
        settings_dialog.filedialog.askopenfilename = lambda **k: (asked.append(k), str(other / "run-z.bat"))[1]
        dlg._browse_btn.invoke()
        pump(app, 0.2)
        check(asked and asked[0].get("initialdir") == str(sdir) and dlg._choice.get() == "run-z.bat"
              and "run-z.bat" in dlg._menu.cget("values"), f"Browse adds + selects the chosen .bat ({dlg._choice.get()})")
        check(dlg._detail.cget("text") == f"model-z · strata-z.json · port {pz}", "browsed launcher parsed")
        settings_dialog.filedialog.askopenfilename = lambda **k: ""
        dlg._browse_btn.invoke()
        check(dlg._choice.get() == "run-z.bat", "cancelled Browse changes nothing")
        settings_dialog.filedialog.askopenfilename = lambda **k: str(sdir / "SETUP.bat")
        dlg._browse_btn.invoke()
        check("does not start Strata" in dlg._detail.cget("text") and str(dlg._save_btn.cget("state")) == "disabled",
              "browsed non-launcher .bat rejected inline")

        # ---- Protected apps… opens the existing whitelist modal on top; closing it returns to Settings
        dlg._protected_btn.invoke()
        child = dlg._child
        check(isinstance(child, WhitelistModal) and app.dialog is dlg, "Protected apps… opens the whitelist modal")
        pump_until(app, lambda: child._loaded)
        pump(app, 0.3)
        check(child.grab_status() == "local", "whitelist modal holds the grab")
        child._rows[0][1].set(True)
        child._save_btn.invoke()
        check(pump_until(app, lambda: det.whitelist_saves and dlg._child is None), "whitelist saved from Settings")
        pump(app, 0.4)
        check(app._dialog_open() and dlg.winfo_exists() and dlg.grab_status() == "local",
              "Settings stays open and takes the grab back")
        dlg._protected_btn.invoke()
        pump(app, 0.3)
        dlg.close()                                             # closing Settings closes a nested modal too
        pump(app, 0.3)
        check(not app._dialog_open() and app.cfg["port"] == pb, "Settings closed (nested modal with it); nothing saved")

        # ---- launcher change while ACTIVE: saved, "applies next start", no restart
        to_active(app, ep)
        check(app.state_name == "ACTIVE", "ACTIVE for the running-change test")
        app.gear.invoke()
        dlg = app.dialog
        check(isinstance(dlg, SettingsDialog), "Settings opens while ACTIVE")
        pump(app, 0.3)
        url = config_store.server_root(app.cfg["endpoint_url"]) + "/"
        check(dlg._link_host.cget("text") == url[len("http://"):-1] and dlg._link_text.cget("text_color") == T.TEXT,
              f"live link shows the host ({dlg._link_host.cget('text')!r})")
        dlg._link_text._label.event_generate("<Button-1>")
        pump(app, 0.1)
        check(OPENED_URLS == [url], f"click opens the Strata dashboard ({OPENED_URLS})")
        shot(dlg, "settings_active")
        dlg._choice.set("run-a.bat")
        dlg._on_pick("run-a.bat")
        check(dlg._msg.cget("text") == "Applies next start — Strata keeps running", f"hint ({dlg._msg.cget('text')!r})")
        shot(dlg, "settings_running")
        n_stop, n_start = len(ep.stop_calls), len(ep.start_calls)
        dlg._save_btn.invoke()
        check(pump_until(app, lambda: not app._dialog_open()), "Save while ACTIVE closes the dialog")
        check(disk_config()["launcher_bat"] == str(sdir / "run-a.bat") and disk_config()["port"] == pa,
              "saved to config.json right away")
        check(app.cfg["port"] == pb and app.cfg["launcher_bat"] == bat_b and app._pending_launcher is not None,
              "the running endpoint keeps its port/config (OFF must still find it)")
        check(len(ep.stop_calls) == n_stop and len(ep.start_calls) == n_start and app.state_name == "ACTIVE",
              "no restart")
        check("run-a.bat applies next start" in status(app), f"main window says so "
              f"({status(app)!r})")
        shot(app, "active_pending_launcher")
        app.gear.invoke()
        dlg = app.dialog
        pump(app, 0.3)
        check(dlg._choice.get() == "run-a.bat", "reopened dialog shows the pending launcher")
        dlg.close()
        pump(app, 0.2)

        # ---- OFF, then the pending launcher becomes the live config
        app.on_toggle()
        ep.status_value = dict(IDLE_STATUS)
        fire(ep.stop_calls[-1][0], stop_result())
        check(pump_until(app, lambda: app.state_name == "IDLE"), "OFF -> IDLE")
        check(app.cfg["port"] == pa and app.cfg["launcher_bat"] == str(sdir / "run-a.bat")
              and app.cfg["model_name"] == "model-a" and app._pending_launcher is None,
              "pending launcher applied once Strata stopped")
        check(app.cfg["launch_cmd"][0] == str(sdir / ".venv" / "Scripts" / "python.exe"), "launch_cmd from the .bat")

        # ---- picking the launcher that is already loaded while ACTIVE -> nothing pending
        to_active(app, ep)
        app.gear.invoke()
        dlg = app.dialog
        pump(app, 0.3)
        check(dlg._msg.cget("text") == "", "unchanged selection: no hint")
        dlg._save_btn.invoke()
        check(pump_until(app, lambda: not app._dialog_open()) and app._pending_launcher is None
              and "applies next start" not in status(app), "re-saving the active launcher is not 'pending'")

        # ---- gear is inert while STARTING / STOPPING
        app.state_name = "STARTING"
        app.open_settings()
        check(app.dialog is None, "Settings ignored while STARTING")
        app.state_name = "IDLE"
        ep.status_value = dict(IDLE_STATUS)
        app._apply_state()
    finally:
        settings_dialog.filedialog.askopenfilename = orig_ask
        if not app._destroyed:
            app.destroy()
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- no strata_dir / nothing configured: usable empty state
    app, det, ep = new_app(dict(CFG, strata_dir="", launcher_bat="", launch_cmd=[]))
    try:
        app.gear.invoke()
        dlg = app.dialog
        pump(app, 0.3)
        check(dlg._choice.get() == settings_dialog.NO_LAUNCHERS and str(dlg._menu.cget("state")) == "disabled"
              and str(dlg._save_btn.cget("state")) == "disabled" and "Browse" in dlg._detail.cget("text"),
              f"empty state: disabled dropdown + hint ({dlg._detail.cget('text')!r})")
        shot(dlg, "settings_empty")
        dlg.tk.call(dlg.protocol("WM_DELETE_WINDOW"))
        pump(app, 0.2)
        check(not app._dialog_open(), "close-X closes Settings")
    finally:
        if not app._destroyed:
            app.destroy()


def run() -> int:
    main.TOGGLE_DEBOUNCE_S = 0.0                 # tests click on purpose faster than a human; test_review_fixes re-enables it
    dialogs = Dialogs()
    dialogs.install()
    try:
        test_threading_and_import()
        app, det, ep = new_app()
        try:
            for fn, args in ((test_layout_idle_preview, (app, det, ep)), (test_on_flow, (app, det, ep)),
                             (test_off_flow, (app, det, ep, dialogs)), (test_error_and_cancel, (app, det, ep)),
                             (test_sweep_dialog_close_x, (app, det, ep)), (test_whitelist_modal, (app, det, ep)),
                             (test_load_progress_view, (app, det, ep)), (test_strata_estimate, (app, det, ep)),
                             (test_stats_section, (app, det, ep)), (test_notes_and_stale_status, (app, det, ep)),
                             (test_low_ram, (app, det, ep)), (test_review_fixes, (app, det, ep)),
                             (test_model_state, (app, det, ep)), (test_reopen_flow, (app, det, ep))):
                print(f"-- {fn.__name__}")
                try:
                    fn(*args)
                except Exception:
                    FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
                    print(FAILS[-1])
                gc.collect()                     # Tk garbage of closed dialogs: finalize on THIS (the Tk) thread
        finally:
            if not app._destroyed:
                app.destroy()
        for fn, args in ((test_delete_window, (dialogs,)), (test_settings_dialog, (dialogs,)),
                         (test_stats_persist_and_scaling, ())):
            gc.collect()                         # the previous app's Tk objects die on the Tk thread, not a worker
            print(f"-- {fn.__name__}")
            try:
                fn(*args)
            except Exception:
                FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
                print(FAILS[-1])
    finally:
        dialogs.restore()
        shutil.rmtree(HOME, ignore_errors=True)
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
