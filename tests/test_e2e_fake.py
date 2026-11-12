r"""End-to-end check with the REAL EndpointManager / ProcessDetector / VramSampler against a FAKE Strata.

Run: .venv\Scripts\python.exe tests\test_e2e_fake.py

What is real: the app, the endpoint manager (process launch, health polling, stop + tree kill), the detector's
read-only scan, the NVML sampler, the HTTP /metrics poller. What is fake: "Strata" is tests/fixtures/
fake_strata_server.py copied to <temp>\serve\server.py and started from a run-*.bat that the app parses - on free
random ports (never 8080). Safety: the sweep dialog is always answered "Start without closing" and a real sweep is
made impossible; the only processes stopped are the fake servers this test's manager launched; settings and logs go
to a temp AGENT_START_HOME; no toast.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="hc-e2e-home-"))
os.environ["AGENT_START_HOME"] = str(HOME)

import psutil  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import config_store  # noqa: E402
import launcher_bat  # noqa: E402
import main  # noqa: E402
import busy_dialog  # noqa: E402
from busy_dialog import BusyDialog  # noqa: E402
from detector import ProcessDetector  # noqa: E402
from server_manager import EndpointManager  # noqa: E402
from sweep_dialog import SweepDialog  # noqa: E402

assert config_store.DATA_DIR == HOME
PASSES, FAILS = 0, []
TMP = Path(tempfile.mkdtemp(prefix="hc-e2e-strata-"))
MARK = os.path.normcase(str(TMP))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"[PASS] {msg}")
    else:
        FAILS.append(msg)
        print(f"[FAIL] {msg}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    assert p != 8080
    return p


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pump_until(app, cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        app.update()
        time.sleep(0.01)
    return bool(cond())


def shot(app, name: str) -> None:
    app.attributes("-topmost", True)
    app.lift()
    app.update()
    time.sleep(0.35)
    app.update()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    path = HERE / f"ui_{name}.png"
    ImageGrab.grab(bbox=(x, y, x + app.winfo_width(), y + app.winfo_height()), all_screens=True).save(path)
    print(f"screenshot {path}")


def write_bat(name: str, port: int, model: str, extra: str = "") -> Path:
    bat = TMP / name
    bat.write_text(f'@echo off\r\ntitle fake Strata {model}\r\ncd /d "{TMP}"\r\n"{sys.executable}" '
                   f'"{TMP}\\serve\\server.py" "--engine" "strata" "--port" "{port}" "--model" "{model}" {extra} '
                   f'"--open"\r\nif errorlevel 1 pause\r\n', encoding="utf-8", newline="")
    return bat


def turn_on(app, port: int) -> bool:
    app.on_toggle()

    def answered() -> bool:
        if isinstance(app.dialog, SweepDialog):
            app.dialog._decide("start_only")          # NEVER close anything
        return app.state_name in ("ACTIVE", "ERROR")
    ok = pump_until(app, answered, 40)
    return ok and app.state_name == "ACTIVE" and listening(port)


def turn_off(app, port: int) -> bool:
    app.on_toggle()
    # The fake Strata reports "generating" in /metrics, so Turn off asks first (BusyDialog): answer "Turn off now"
    # once the dialog accepts clicks (busy_dialog.ARM_S).
    if pump_until(app, lambda: isinstance(app.dialog, BusyDialog), 2):
        pump_until(app, lambda: False, busy_dialog.ARM_S + 0.2)
        app.dialog.now_btn.invoke()
    return pump_until(app, lambda: app.state_name in ("IDLE", "ERROR"), 40) and app.state_name == "IDLE" \
        and not listening(port)


def run() -> None:
    (TMP / "serve").mkdir()
    shutil.copy(HERE / "fixtures" / "fake_strata_server.py", TMP / "serve" / "server.py")
    pa, pb = free_port(), free_port()
    bat_a = write_bat("run-e2e-a.bat", pa, "e2e-model-a", '"--metrics-requests" "20" "--metrics-live" "generating"')
    bat_b = write_bat("run-e2e-b.bat", pb, "e2e-model-b")
    cfg = config_store.load_config()                  # defaults (temp home has no config.json)
    cfg.update(user_whitelist=[], ready_timeout_s=30, stop_timeout_s=10)
    launcher_bat.apply_to_config(cfg, launcher_bat.parse_bat(str(bat_a)))
    config_store.save_config(cfg)
    assert cfg["port"] == pa != 8080

    endpoint = EndpointManager(cfg, notify=False, log_dir=HOME / "logs", poll_interval_s=0.25)
    detector = ProcessDetector(cfg, protected_pids_provider=endpoint.protected_pids)
    real_sweep = detector.sweep_processes

    def safe_sweep(dry_run: bool = False, targets=None):
        assert dry_run is True, "this test must never run a real sweep"
        return real_sweep(dry_run=True, targets=targets)
    detector.sweep_processes = safe_sweep

    config_store.save_state({"stats_expanded": True})
    app = main.AgentStartApp(detector=detector, endpoint=endpoint, config=cfg)   # real poller + real NVML
    try:
        pump_until(app, lambda: False, 0.5)
        check(app.expanded and app.state_name == "IDLE", "starts expanded + IDLE on the fake config")

        check(turn_on(app, pa), f"ON: fake Strata launched from the parsed .bat and ready (state {app.state_name}, "
              f"{app.preview.cget('text')!r})")
        lp = [p for p in psutil.process_iter() if MARK in os.path.normcase(" ".join(_cmd(p)))]
        check(lp and all("--open" not in _cmd(p) for p in lp), f"server processes run without --open ({len(lp)})")
        check(pump_until(app, lambda: app.model_id == "e2e-model-a", 10), f"model id from the server ({app.model_id})")
        # the first poll of a new server is a catch-up (?requests=all): every kept request, not just the newest 12
        check(pump_until(app, lambda: len(app.metrics) == 20, 10), f"real HTTP /metrics catch-up poll: all 20 requests "
              f"({len(app.metrics)})")
        check(app.row_decode.tag.cget("text") == "live" and app.row_decode.value.cget("text") == "101.3 tok/s",
              f"live decode from the fake ({app.row_decode.value.cget('text')})")
        for _ in range(3):
            with OPENER.open(f"http://127.0.0.1:{pa}/_fake/add?n=9", timeout=3) as r:
                r.read()
            n = len(app.metrics)
            pump_until(app, lambda: len(app.metrics) > n, 8)
        check(len(app.metrics) == 30, f"accumulated to 30 across polls ({len(app.metrics)})")
        snap = app.vram.snapshot()
        pump_until(app, lambda: len(app.vram.snapshot().samples) >= 3, 8)
        snap = app.vram.snapshot()
        print(f"real VRAM sampler: available={snap.available} source={snap.source!r} samples={len(snap.samples)} "
              f"total={snap.total_mb}")
        if snap.available:
            check(snap.total_mb > 1024 and app.row_vram.value.cget("text").endswith("GB"),
                  f"VRAM row shows the real GPU ({app.row_vram.value.cget('text')}, via {snap.source})")
        else:
            check(app.row_vram.graph._empty == "VRAM unavailable", "no NVML here -> 'VRAM unavailable'")
        shot(app, "e2e_fake_active")

        # launcher change while running: saved, deferred, old server still stoppable on its own port
        done = []
        app.save_launcher(launcher_bat.parse_bat(str(bat_b)), done.append)
        check(pump_until(app, lambda: bool(done), 5) and done == [None], f"launcher saved while ACTIVE ({done})")
        check(cfg["port"] == pa and app._pending_launcher is not None
              and config_store.load_config()["port"] == pb, "running config untouched; disk has the new launcher")
        check(turn_off(app, pa), f"OFF stops the fake on its original port (state {app.state_name}, "
              f"{app.preview.cget('text')!r})")
        check(cfg["port"] == pb and cfg["model_name"] == "e2e-b" and app._pending_launcher is None,
              "pending launcher applied after the stop")
        check(not [p for p in psutil.process_iter() if MARK in os.path.normcase(" ".join(_cmd(p)))],
              "no fake server process left")

        check(turn_on(app, pb), f"ON again: the other launcher's server on its port (state {app.state_name})")
        check(pump_until(app, lambda: app.model_id == "e2e-model-b", 10) and not listening(pa),
              f"serving the newly chosen model ({app.model_id})")
        check(pump_until(app, lambda: app.metrics.polls >= 1 and len(app.metrics) == 0, 8)
              and app.row_decode.graph._empty == "No requests yet", "new server: history reset, 'No requests yet'")
        check(turn_off(app, pb), "OFF again")
    finally:
        try:
            if listening(pa) or listening(pb):        # a failed check left a fake running: stop OUR children only
                for p in psutil.process_iter():
                    if MARK in os.path.normcase(" ".join(_cmd(p))):
                        p.kill()
        finally:
            if not app._destroyed:
                app._finish_exit()


def _cmd(p: psutil.Process) -> list[str]:
    try:
        return p.cmdline() or []
    except (psutil.Error, OSError):
        return []


if __name__ == "__main__":
    os.chdir(ROOT)
    try:
        run()
    except Exception:  # noqa: BLE001
        FAILS.append(f"run raised:\n{traceback.format_exc()}")
        print(FAILS[-1])
    import logging
    logging.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(HOME, ignore_errors=True)
    print(f"\nRESULT: {PASSES} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
