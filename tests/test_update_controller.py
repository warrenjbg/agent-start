"""Self-check for update_controller.py (+ the Settings "Check for updates" button).

Safe by construction:
  * A stub updater module is injected: no GitHub, no network, no download, no installer. The real updater's network /
    process entry points (check, download, launch_installer, _popen) and urllib/socket connects are replaced by
    recorders that fail the run if anything reaches them.
  * state.json is a dict in a fake host; the one config_store check uses a throwaway AGENT_START_HOME.
  * The Tk part builds SettingsDialog transparent and without the modal grab/focus (another window may be in use),
    measures it, and destroys it. No screenshots.
Run: .venv\\Scripts\\python.exe tests\\test_update_controller.py
"""
from __future__ import annotations

import gc
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="hc-upd-ctrl-"))
os.environ["AGENT_START_HOME"] = str(HOME)      # before config_store is imported (settings_dialog imports it)

import config_store  # noqa: E402
import update_controller as UC  # noqa: E402
import updater  # noqa: E402
from update_controller import UpdateController, UpdateHost  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
CUR = "1.8.1"
FORBIDDEN: list[str] = []


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))


# ------------------------------------------------------------------------------------------------ no network, ever
def _forbid(what: str):
    def f(*a, **k):
        FORBIDDEN.append(what)
        raise RuntimeError(f"test must not call {what}")
    return f


updater.check = _forbid("updater.check")
updater.download = _forbid("updater.download")
updater.launch_installer = _forbid("updater.launch_installer")
updater._popen = _forbid("updater._popen")
urllib.request.urlopen = _forbid("urlopen")
_real_connect = socket.socket.connect
socket.socket.connect = _forbid("socket.connect")   # type: ignore[method-assign]


def info(ver: str = "9.9.0") -> updater.UpdateInfo:
    return updater.UpdateInfo(version=ver, tag=f"v{ver}", asset_name=f"AgentStart-Setup-{ver}.exe",
                              asset_url=f"https://github.com/{updater.REPO}/releases/download/v{ver}/"
                                        f"AgentStart-Setup-{ver}.exe",
                              size=14_000_000, sha256="ab" * 32, page_url=f"https://github.com/{updater.REPO}")


class StubUpdater:
    """The parts of updater the controller uses. check() can be held open with .gate (a request in flight)."""
    CHECK_INTERVAL_S = updater.CHECK_INTERVAL_S
    RETRY_AFTER_ERROR_S = updater.RETRY_AFTER_ERROR_S
    UpdateInfo = updater.UpdateInfo
    UpdateError = updater.UpdateError

    def __init__(self) -> None:
        self.due = updater.due                    # pure function: no I/O
        self.result: tuple = ("current", None)
        self.gate: threading.Event | None = None
        self.checks = 0
        self.downloads: list[str] = []
        self.launches: list[tuple] = []
        self.cleanups = 0
        self.download_error: str | None = None
        self.launch_error: str | None = None
        self.dir = str(HOME / "updates")

    def check(self, current, timeout=8.0, api_url=None):
        self.checks += 1
        if self.gate is not None:
            self.gate.wait(5)
        return self.result

    def download(self, i, dest, progress=None, stop_evt=None, **kw):
        for k in range(1, 5):
            if progress:
                progress(k * i.size // 4, i.size)
        if self.download_error:
            raise updater.UpdateError(self.download_error)
        self.downloads.append(i.version)
        return os.path.join(dest, i.asset_name)

    def default_download_dir(self):
        return self.dir

    def cleanup_old(self, d, current):
        self.cleanups += 1
        return 0

    def launch_installer(self, path, sha256, dest_dir=None, install_dir=None, log_path=None, *, wait_pid=None):
        if self.launch_error:
            raise updater.UpdateError(self.launch_error)
        self.launches.append((path, sha256, install_dir, log_path, wait_pid))


class FakeHost:
    def __init__(self, available: bool = True) -> None:
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.workers: list[str] = []
        self.later: list[tuple[int, object]] = []
        self.notes: list[str] = []
        self.previews = 0
        self.state_later: dict = {}
        self.state: dict = {}
        self.asked: list[str] = []
        self.answer = True
        self.toasts: list[tuple[str, str]] = []
        self.engine = "IDLE"
        self.dialog = False
        self.box = False
        self.events: list[str] = []
        self.avail = available
        self.is_frozen = False
        self.stop_evt = threading.Event()

    def host(self) -> UpdateHost:
        return UpdateHost(
            post=lambda fn, *a: self.q.put((fn, a)),
            run_worker=self.run_worker,
            call_later=lambda ms, fn: self.later.append((ms, fn)),
            set_note=self.notes.append,
            request_preview=self._preview,
            save_state_later=self.state_later.__setitem__,
            save_state=self._save,
            load_state=lambda: dict(self.state),
            ask=self._ask,
            toast=lambda t, m: self.toasts.append((t, m)),
            engine_state=lambda: self.engine,
            dialog_open=lambda: self.dialog,
            box_open=lambda: self.box,
            release_singleton=lambda: self.events.append("release"),
            acquire_singleton=lambda: self.events.append("acquire"),
            finish_exit=lambda: self.events.append("exit"),
            available=lambda: self.avail,
            frozen=lambda: self.is_frozen,
            stop_evt=self.stop_evt)

    def run_worker(self, name, fn, *args) -> None:
        self.workers.append(name)
        threading.Thread(target=fn, args=args, name=name, daemon=True).start()

    def _preview(self) -> None:
        self.previews += 1

    def _save(self, d: dict) -> None:
        self.state.update(d)
        self.events.append("save_state")

    def _ask(self, msg: str) -> bool:
        self.asked.append(msg)
        return self.answer

    def pump(self, cond=None, timeout: float = 3.0) -> bool:
        end = time.monotonic() + timeout
        while True:
            try:
                while True:
                    fn, a = self.q.get_nowait()
                    fn(*a)
            except queue.Empty:
                pass
            if cond is None:
                if time.monotonic() >= end:
                    return True
            elif cond():
                return True
            elif time.monotonic() >= end:
                return False
            time.sleep(0.01)


def make(available: bool = True, daily: bool = True, checked_at=None):
    fh = FakeHost(available)
    st = StubUpdater()
    c = UpdateController(fh.host(), CUR, daily=daily, checked_at=checked_at, updater_mod=st)
    return fh, st, c


def settle(fh: FakeHost, c: UpdateController) -> bool:
    return fh.pump(lambda: not c.checking and fh.q.empty(), 3)


# ------------------------------------------------------------------------------------------------ daily check
def test_daily() -> None:
    fh, st, c = make(checked_at=None)
    c.maybe_check()
    check("daily: due (never checked) -> one request", c.checking and fh.workers == ["hc-update-check"])
    settle(fh, c)
    check("daily current: check time recorded (memory + state key)",
          isinstance(c.checked_at, float) and fh.state_later.get("update_checked_at") == c.checked_at)
    check("daily current: next check in ~24 h", abs(c.next_at - time.monotonic() - updater.CHECK_INTERVAL_S) < 5)
    check("daily current: silent (no toast, no note)", not fh.toasts and not fh.notes and c.offer is None)

    fh, st, c = make(checked_at=time.time() - 3600)
    c.maybe_check()
    check("daily: not due -> no request, look again in 10 min",
          st.checks == 0 and not fh.workers and abs(c.next_at - time.monotonic() - 600) < 5)

    fh, st, c = make(checked_at=None)
    st.result = ("error", None)
    c.maybe_check()
    settle(fh, c)
    check("daily error: not recorded as checked", c.checked_at is None and "update_checked_at" not in fh.state_later)
    check("daily error: back-off ~1 h", abs(c.next_at - time.monotonic() - updater.RETRY_AFTER_ERROR_S) < 5)
    check("daily error: silent", not fh.toasts and not fh.notes)
    c.maybe_check()
    check("daily: no retry during the back-off", st.checks == 1 and len(fh.workers) == 1)

    fh, st, c = make(daily=False)
    c.maybe_check()
    check("daily switch off: no daily request", c.no_check and st.checks == 0)
    fh, st, c = make(available=False)
    c.first_tick()
    check("from source: no daily request, no cleanup", c.no_check and st.checks == 0 and fh.workers == [])

    fh, st, c = make()
    c.first_tick()
    fh.pump(lambda: st.cleanups == 1 and not c.checking, 3)
    check("first tick (installed): cleanup + daily check", fh.workers == ["hc-update-clean", "hc-update-check"]
          and st.cleanups == 1, f"{fh.workers}")

    # newer from the daily check: offer + one toast; the same version again: no second toast
    fh, st, c = make()
    st.result = ("newer", info())
    c.maybe_check()
    settle(fh, c)
    check("daily newer: offer set + idle line refreshed", c.offer_version() == "9.9.0" and fh.previews >= 1)
    check("daily newer: one toast with the old wording",
          len(fh.toasts) == 1 and fh.toasts[0][1].startswith("Version 9.9.0 is available. Install it from Settings"))
    c.next_at, c.checked_at = 0.0, None
    c.maybe_check()
    settle(fh, c)
    check("daily: same version again -> no second toast", st.checks == 2 and len(fh.toasts) == 1)

    # switched off while a daily check ran: result dropped (as before)
    fh, st, c = make()
    st.gate, st.result = threading.Event(), ("newer", info())
    c.maybe_check()
    c.set_daily(False)
    st.gate.set()
    settle(fh, c)
    check("daily check + switch off meanwhile: result dropped", c.offer is None and c.checked_at is None
          and not fh.toasts)


# ------------------------------------------------------------------------------------------------ manual check
def test_manual() -> None:
    # ignores the interval, the back-off and the switch
    for label, kw, prep in (("interval (checked 1 h ago)", {"checked_at": time.time() - 3600}, None),
                            ("back-off (failed 1 min ago)", {}, "backoff"),
                            ("daily switch off", {"daily": False}, None)):
        fh, st, c = make(**kw)
        if prep == "backoff":
            c.next_at = time.monotonic() + updater.RETRY_AFTER_ERROR_S
        got: list = []
        res = c.check_now(lambda s, v: got.append((s, v)) or True)
        settle(fh, c)
        check(f"manual ignores the {label}", res == UC.STARTED and st.checks == 1 and got == [("current", CUR)],
              f"{res} {st.checks} {got}")

    # result states
    fh, st, c = make()
    got = []
    def cb(s, v):
        got.append((s, v))
        return True
    c.check_now(cb)
    settle(fh, c)
    check("manual current -> ('current', this version)", got == [("current", CUR)])
    check("manual success: check time recorded + next daily in 24 h",
          isinstance(fh.state_later.get("update_checked_at"), float)
          and abs(c.next_at - time.monotonic() - updater.CHECK_INTERVAL_S) < 5)
    check("manual current: no toast", not fh.toasts)
    st.result = ("newer", info("9.9.1"))
    c.check_now(cb)
    settle(fh, c)
    check("manual newer -> ('newer', version) + offer", got[-1] == ("newer", "9.9.1") and c.offer_version() == "9.9.1")
    check("manual newer shown in the dialog: no toast", not fh.toasts)
    check("manual newer: log/offer as the daily one (idle line refreshed)", fh.previews >= 2)
    c.next_at, c.checked_at = 0.0, None
    c.maybe_check()
    settle(fh, c)
    check("daily check later finds the same version: still no toast", not fh.toasts and st.checks == 3)

    # error: nothing recorded, the daily schedule is only ever pushed to the 1 h back-off
    fh, st, c = make(checked_at=None)
    st.result = ("error", None)
    t0 = time.time() - 3600
    c.checked_at = t0
    c.next_at = 0.0
    got = []
    c.check_now(lambda s, v: got.append((s, v)) or True)
    settle(fh, c)
    check("manual error -> ('error', None)", got == [("error", None)])
    check("manual error: check time NOT recorded", c.checked_at == t0 and "update_checked_at" not in fh.state_later)
    check("manual error: a due-soon daily check waits the 1 h back-off",
          abs(c.next_at - time.monotonic() - updater.RETRY_AFTER_ERROR_S) < 5)
    later = time.monotonic() + 20 * 3600
    c.next_at = later
    c.check_now(lambda s, v: True)
    settle(fh, c)
    check("manual error: a later daily schedule is not moved", c.next_at == later)
    check("manual error: no toast, no note", not fh.toasts and not fh.notes)

    # unavailable / busy
    fh, st, c = make(available=False)
    got = []
    check("from source: check_now -> unavailable, no request",
          c.check_now(lambda s, v: got.append(s)) == UC.UNAVAILABLE and st.checks == 0 and not fh.workers)
    fh, st, c = make()
    c.updating = True
    check("while an update installs: busy, no request", c.check_now(lambda s, v: True) == UC.BUSY and st.checks == 0)


def test_one_request() -> None:
    # double click
    fh, st, c = make()
    st.gate = threading.Event()
    got: list = []
    def cb(s, v):
        got.append((s, v))
        return True
    r1, r2 = c.check_now(cb), c.check_now(cb)
    check("double click: started, then joined", (r1, r2) == (UC.STARTED, UC.JOINED))
    c.maybe_check()
    st.gate.set()
    settle(fh, c)
    check("double click: ONE request, one result", st.checks == 1 and fh.workers == ["hc-update-check"]
          and got == [("current", CUR)], f"{st.checks} {fh.workers} {got}")

    # a click while the DAILY check runs gets that check's result
    fh, st, c = make()
    st.gate, st.result = threading.Event(), ("newer", info())
    c.maybe_check()
    got = []
    r = c.check_now(lambda s, v: got.append((s, v)) or True)
    st.gate.set()
    settle(fh, c)
    check("manual during the daily check: joined, one request, result delivered",
          r == UC.JOINED and st.checks == 1 and got == [("newer", "9.9.0")], f"{r} {st.checks} {got}")
    check("manual during daily: shown in the dialog -> no toast", not fh.toasts and c.offer_version() == "9.9.0")

    # the daily tick while a manual check runs
    fh, st, c = make()
    st.gate = threading.Event()
    c.check_now(lambda s, v: True)
    c.maybe_check()
    st.gate.set()
    settle(fh, c)
    check("daily tick during a manual check: no second request", st.checks == 1)

    # a dialog closed before the result: the late result is not "shown", so a new version still toasts
    fh, st, c = make()
    st.gate, st.result = threading.Event(), ("newer", info())
    c.check_now(lambda s, v: False)          # what a destroyed dialog's callback returns
    st.gate.set()
    settle(fh, c)
    check("result nobody saw (dialog closed): the daily toast instead", len(fh.toasts) == 1 and c.offer is not None)

    # a raising listener does not break the flow
    fh, st, c = make()

    def boom(s, v):
        raise RuntimeError("widget gone")
    c.check_now(boom)
    settle(fh, c)
    check("a failing result callback is contained", not c.checking and c.checked_at is not None)


def test_switch_off_install() -> None:
    fh, st, c = make()
    c.set_daily(False)
    check("switch off: saved, no checks", fh.state_later.get("update_check") is False and c.no_check)
    st.result = ("newer", info())
    c.check_now(lambda s, v: True)
    settle(fh, c)
    check("switch off + manual found: offered", c.offer_version() == "9.9.0")
    check("... the daily check stays off", c.no_check and not c.daily and fh.state_later.get("update_check") is False)
    fh.answer = True
    c.install()
    fh.pump(lambda: st.launches, 3)
    check("... and it installs through the confirm step", len(fh.asked) == 1 and st.launches)

    # switched off DURING a manual check: the user asked, so the result still counts
    fh, st, c = make()
    st.gate, st.result = threading.Event(), ("newer", info())
    got: list = []
    c.check_now(lambda s, v: got.append(s) or True)
    c.set_daily(False)
    st.gate.set()
    settle(fh, c)
    check("switch off during a manual check: result shown + offered", got == ["newer"] and c.offer is not None)
    c.set_daily(False)
    check("turning the switch off (again) withdraws the offer, as before", c.offer is None)
    c.set_daily(True)
    settle(fh, c)
    check("switch on after a manual check today: no request (not due)", st.checks == 1 and not c.no_check)


def test_install() -> None:
    fh, st, c = make()
    c.offer = info()
    fh.answer = False
    c.install()
    check("install: confirm asked with version + size", fh.asked and "Install Agent Start 9.9.0?" in fh.asked[0]
          and "about 14 MB" in fh.asked[0])
    check("install declined: nothing downloaded", not c.updating and not fh.workers and not st.downloads)
    fh.engine = "ACTIVE"
    c.install()
    check("install while serving: 'Strata keeps running' in the question", "Strata keeps running" in fh.asked[-1])
    fh.engine = "IDLE"

    for state in ("STARTING", "STOPPING"):
        n = len(fh.asked)
        fh.engine = state
        c.install()
        check(f"install refused while {state}", len(fh.asked) == n and fh.notes[-1]
              == "Update waits until Strata has started or stopped" and not c.updating)
    fh.engine = "IDLE"
    fh.box = True
    n = len(fh.asked)
    c.install()
    check("install ignored while a message box is open", len(fh.asked) == n)
    fh.box = False

    # accepted: re-check, download (progress), verify hand-off, exit
    fh.answer = True
    st.result = ("newer", info())
    c.install()
    check("install accepted: downloading note + updating", c.updating and fh.notes[-1] == "Downloading update 9.9.0…")
    check("updating: offer hidden from Settings/idle line", c.offer_version() is None)
    check("updating: check_now is busy", c.check_now(lambda s, v: True) == UC.BUSY)
    fh.pump(lambda: st.launches, 3)
    check("progress notes shown", any(x.startswith("Downloading update · ") and x.endswith("%") for x in fh.notes))
    check("GitHub re-checked at install time", st.checks == 1)
    if st.launches:
        path, sha, idir, logp, pid = st.launches[0]
        check("installer: verified path + sha256 + waits for this pid + log",
              path.endswith("AgentStart-Setup-9.9.0.exe") and sha == "ab" * 32 and pid == os.getpid()
              and logp == os.path.join(st.dir, "install.log"))
        check("from source: no /DIR", idir is None)
    pend = fh.state.get("update_pending") or {}
    check("update_pending saved (same keys)", set(pend) == {"version", "from", "at", "log"}
          and pend["version"] == "9.9.0" and pend["from"] == CUR)
    check("singleton released before the installer", fh.events[:2] == ["save_state", "release"])
    check("installing + exit scheduled 400 ms later", c.installing and fh.later and fh.later[-1][0] == 400)
    if fh.later:
        fh.later[-1][1]()
    check("... the exit runs", fh.events[-1] == "exit")

    # frozen: /DIR = this exe's folder
    fh, st, c = make()
    fh.is_frozen = True
    c.offer, st.result = info(), ("newer", info())
    c.install()
    fh.pump(lambda: st.launches, 3)
    check("installed: /DIR is the running exe's folder", st.launches and st.launches[0][2]
          == os.path.dirname(sys.executable))

    # download failure
    fh, st, c = make()
    st.download_error = "size mismatch"
    c.offer, st.result = info(), ("newer", info())
    c.install()
    fh.pump(lambda: not c.updating, 3)
    check("download failure: note, not updating, no installer", fh.notes[-1] == "Update failed\nsize mismatch"
          and not c.updating and not st.launches and "release" not in fh.events)
    st.download_error = None
    c.install()
    fh.pump(lambda: st.launches, 3)
    check("retry after a failed download works", len(st.launches) == 1)

    # GitHub no longer confirms at install time
    fh, st, c = make()
    c.offer, st.result = info(), ("error", None)
    c.install()
    fh.pump(lambda: not c.updating, 3)
    check("re-check error at install: 'Update failed' note", fh.notes[-1].startswith("Update failed\nGitHub did not"))

    # installer could not start
    fh, st, c = make()
    st.launch_error = "hash mismatch"
    c.offer, st.result = info(), ("newer", info())
    c.install()
    fh.pump(lambda: not c.updating, 3)
    check("installer refused: pending cleared, note, app stays", fh.state.get("update_pending") is None
          and fh.notes[-1] == "Update failed\nhash mismatch" and "exit" not in fh.events and not c.installing)
    check("installer refused: the single-instance mutex is taken back after it was released",
          "release" in fh.events and "acquire" in fh.events
          and fh.events.index("acquire") > fh.events.index("release"), str(fh.events))

    # a newer release appeared between the question and the download: nothing is installed unasked
    fh, st, c = make()
    c.offer, st.result = info("9.9.0"), ("newer", info("9.9.1"))
    c.install()
    fh.pump(lambda: not c.updating, 3)
    check("confirmed 9.9.0, GitHub now lists 9.9.1: no download, no installer",
          "9.9.0" in fh.asked[-1] and not st.downloads and not st.launches and "release" not in fh.events
          and "exit" not in fh.events, f"{st.downloads} {st.launches} {fh.events}")
    check("...the offer now names 9.9.1 and the note says to click again",
          c.offer is not None and c.offer.version == "9.9.1" and not c.updating
          and fh.notes[-1] == "Update failed\n9.9.1 is out now - click again", f"{c.offer} {fh.notes[-1]!r}")
    c.install()
    fh.pump(lambda: bool(st.launches), 3)
    check("asked again for 9.9.1, then that one installs", "9.9.1" in fh.asked[-1] and st.downloads == ["9.9.1"]
          and len(st.launches) == 1, f"{fh.asked[-1]!r} {st.downloads}")

    # deferred while a dialog / box is open or Strata starts
    for what in ("dialog", "box", "STARTING"):
        fh, st, c = make()
        c.offer, st.result = info(), ("newer", info())
        c.install()                              # confirmed while idle; then Turn on / a dialog / a box races it
        if what == "dialog":
            fh.dialog = True
        elif what == "box":
            fh.box = True
        else:
            fh.engine = "STARTING"
        fh.pump(lambda: fh.later, 3)
        check(f"installer deferred while {what}", not st.launches and fh.later and fh.later[0][0] == 1000)
        fh.dialog = fh.box = False
        fh.engine = "IDLE"
        if fh.later:
            fh.later.pop(0)[1]()
        check(f"... and started after the {what} is gone", len(st.launches) == 1)


def test_report_result() -> None:
    fh, st, c = make()
    fh.state["update_pending"] = {"version": CUR, "from": "1.8.0", "at": time.time(), "log": "x"}
    c.report_result()
    check("pending == this version: 'Updated to'", fh.notes == [f"Updated to {CUR}"]
          and fh.state["update_pending"] is None)
    fh, st, c = make()
    fh.state["update_pending"] = {"version": "9.9.0", "from": CUR, "at": time.time(), "log": "x"}
    c.report_result()
    check("pending other version: 'did not install'",
          fh.notes == ["Update to 9.9.0 did not install\nDetails: updates\\install.log"]
          and fh.state["update_pending"] is None)
    fh, st, c = make()
    fh.state["update_pending"] = {"version": "9.9.0", "from": CUR, "at": time.time() - 8 * 86400}
    c.report_result()
    check("older than a week: cleared silently", not fh.notes and fh.state["update_pending"] is None)
    for bad in ("abc", {"x": 1}, [1], None):
        fh, st, c = make()
        fh.state["update_pending"] = {"version": "9.9.0", "from": CUR, "at": bad}
        try:
            c.report_result()
            raised = False
        except Exception:  # noqa: BLE001
            raised = True
        check(f"a damaged 'at' ({bad!r}) does not raise; cleared silently",
              not raised and not fh.notes and fh.state["update_pending"] is None)
    fh, st, c = make()
    c.report_result()
    check("nothing pending: nothing", not fh.notes and "update_pending" not in fh.state)

    # real config_store in the throwaway dir: an existing state.json keeps working
    import json
    config_store.save_state({"update_pending": {"version": "9.9.0", "from": CUR, "at": time.time(), "log": "l"},
                             "update_checked_at": 1700000000.0, "update_check": False})
    saved = config_store.load_state()
    fh = FakeHost()
    h = fh.host()
    h.save_state, h.load_state = config_store.save_state, config_store.load_state
    c = UpdateController(h, CUR, daily=saved.get("update_check", True) is not False,
                         checked_at=saved.get("update_checked_at"), updater_mod=StubUpdater())
    c.report_result()
    on_disk = json.loads(Path(config_store.STATE_PATH).read_text(encoding="utf-8"))
    check("existing state.json: keys read (switch off, last check)", not c.daily and c.no_check
          and c.checked_at == 1700000000.0)
    check("existing state.json: pending reported + cleared on disk", on_disk.get("update_pending") is None
          and fh.notes and "did not install" in fh.notes[0])


# ------------------------------------------------------------------------------------------------ Settings button (Tk)
def test_settings_button() -> None:
    import customtkinter as ctk
    import settings_dialog
    from settings_dialog import CHECK_LABEL, CHECK_LABEL_SOURCE, SettingsDialog
    from version import __version__

    settings_dialog.apply_modal = lambda win, master: win.transient(master)   # no grab / focus_force here
    root = ctk.CTk()
    root.attributes("-alpha", 0.0)
    root.geometry("360x200")
    dialogs: list = []

    def pump(cond=None, t: float = 0.6, fh: FakeHost | None = None) -> bool:
        end = time.monotonic() + t
        while time.monotonic() < end:
            if fh is not None:
                try:
                    while True:
                        fn, a = fh.q.get_nowait()
                        fn(*a)
                except queue.Empty:
                    pass
            root.update()
            if cond is not None and cond():
                return True
            time.sleep(0.01)
        return cond is None or bool(cond())

    def dialog(**kw):
        d = SettingsDialog(root, {"strata_dir": "", "endpoint_url": "http://127.0.0.1:1/v1"}, None,
                           on_save=lambda i, done: done(None), **kw)
        d.attributes("-alpha", 0.0)
        dialogs.append(d)
        pump(t=0.6)                               # past CTkToplevel's own styling (never close a fresh one)
        return d

    def fits(d) -> tuple[bool, str]:
        scale = ctk.ScalingTracker.get_window_scaling(d)
        avail = (SettingsDialog.WIDTH - 24) * scale
        d.update_idletasks()
        w = d._head.winfo_reqwidth()
        return w <= avail + 1, f"row {w} px > {avail:.0f} px"

    try:
        # no new params: as before (no row, same size)
        d = dialog(switches=[{"key": "updates", "label": "Check for updates daily", "value": True}])
        check("old callers: no button; every tab fits the window", d._check_btn is None
              and all(h <= d.logical_height for h in d.tab_heights.values()), f"{d.tab_heights} / {d.logical_height}")
        about_old = d.tab_heights.get("About", 0)
        d.close()

        # from source: disabled, the switch's wording
        d = dialog(on_check_updates=lambda cb: UC.UNAVAILABLE, can_check_updates=False,
                   switches=[{"key": "updates", "label": "Check for updates daily (installed app only)",
                              "value": True, "enabled": False}])
        check("from source: button greyed with '(installed app only)'",
              str(d._check_btn.cget("state")) == "disabled" and d._check_btn.cget("text") == CHECK_LABEL_SOURCE)
        ok, why = fits(d)
        check("from source: row fits the dialog width", ok, why)
        grow = d.tab_heights.get("About", 0) - about_old
        check("new row: the About tab grows by one control row and still fits (nothing clipped)",
              20 <= grow <= 44 and all(h <= d.logical_height for h in d.tab_heights.values()),
              f"grew {grow}; {d.tab_heights} / {d.logical_height}")
        d.check_updates()
        check("from source: a click does nothing", d._check_msg.cget("text") == "")
        d.close()

        # installed, with a real controller + stub updater
        fh, st, c = make()
        st.gate = threading.Event()
        d = dialog(on_check_updates=c.check_now, can_check_updates=c.available(),
                   on_update=lambda: None, switches=[{"key": "updates", "label": "Check for updates daily",
                                                      "value": True}])
        check("installed: button enabled", str(d._check_btn.cget("state")) == "normal"
              and d._check_btn.cget("text") == CHECK_LABEL)
        d.check_updates()
        check("checking: button greyed + 'Checking GitHub…'", str(d._check_btn.cget("state")) == "disabled"
              and d._check_msg.cget("text") == "Checking GitHub…")
        d.check_updates()
        d._check_btn.invoke()
        check("clicks while checking: still one request", st.checks <= 1 and fh.workers == ["hc-update-check"])
        st.gate.set()
        pump(lambda: not c.checking and d._check_msg.cget("text") != "Checking GitHub…", 3, fh)
        check("up to date shown with the current version", d._check_msg.cget("text") == f"Up to date · v{CUR}"
              and str(d._check_btn.cget("state")) == "normal", d._check_msg.cget("text"))
        st.gate = None
        st.result = ("error", None)
        d.check_updates()
        pump(lambda: not c.checking and "Checking" not in d._check_msg.cget("text"), 3, fh)
        check("error shown: 'Could not reach GitHub'", d._check_msg.cget("text") == "Could not reach GitHub")
        ok, why = fits(d)
        check("error text fits", ok, why)
        st.result = ("newer", info("10.10.10"))
        d.check_updates()
        pump(lambda: not c.checking and "Checking" not in d._check_msg.cget("text"), 3, fh)
        check("update available shown + the update link appears",
              d._check_msg.cget("text") == "Version 10.10.10 is available" and d._update_link is not None
              and d._update_link.cget("text") == f"v{__version__} · Update to 10.10.10")
        ok, why = fits(d)
        check("update texts fit", ok, why)
        check("no toast for the version shown in the dialog", not fh.toasts)
        for key in ("checking", "current", "busy"):
            d._show_check(key, "10.10.10")
            ok, why = fits(d)
            check(f"'{settings_dialog.CHECK_TEXTS[key].format(version='10.10.10')}' fits", ok, why)
        d.close()

        # busy (an update is installing)
        fh, st, c = make()
        c.updating = True
        d = dialog(on_check_updates=c.check_now, can_check_updates=True)
        d.check_updates()
        check("busy: says an update is installing, stays greyed", d._check_msg.cget("text")
              == "An update is being installed" and str(d._check_btn.cget("state")) == "disabled")
        d.close()

        # the dialog closes while the check runs: the late result touches nothing
        fh, st, c = make()
        st.gate, st.result = threading.Event(), ("newer", info())
        d = dialog(on_check_updates=c.check_now, can_check_updates=True, on_update=lambda: None)
        d.check_updates()
        d.close()
        pump(t=0.2)
        errors: list = []
        root.report_callback_exception = lambda *a: errors.append(a)
        st.gate.set()
        pump(lambda: not c.checking and fh.q.empty(), 3, fh)
        pump(t=0.2)
        check("late result after close: no error, offer kept, toast instead", not errors and c.offer is not None
              and len(fh.toasts) == 1)
    finally:
        for d in dialogs:
            try:
                if d.winfo_exists():
                    d.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            root.update()
            time.sleep(0.3)
            root.update()
            root.destroy()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        gc.collect()


def main() -> int:
    try:
        for fn in (test_daily, test_manual, test_one_request, test_switch_off_install, test_install,
                   test_report_result, test_settings_button):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
            gc.collect()
        check("never reached GitHub / the network / an installer", not FORBIDDEN, f"{FORBIDDEN}")
    finally:
        socket.socket.connect = _real_connect   # type: ignore[method-assign]
        shutil.rmtree(HOME, ignore_errors=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
