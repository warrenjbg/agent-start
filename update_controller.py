"""UpdateController: the in-app update flow (daily check, manual check, offer, download, install), moved out of
AgentStartApp unchanged apart from the manual "Check for updates".

No Tk and no import of main: the app is reached only through UpdateHost (plain callables), and the updater module is
injected (default: the real `updater`, imported lazily as before). Every method runs on the Tk thread; the workers get
values only and report back through host.post.

Daily check (unchanged): at most once per updater.CHECK_INTERVAL_S (24 h, remembered in state.json
"update_checked_at" and in memory), an hour (updater.RETRY_AFTER_ERROR_S) after a failed check, never while the
"Check for updates daily" switch is off, never from source. Silent on "up to date" and on errors; a NEW version gets
a toast and the idle-line / Settings offer.

Manual check (check_now): ignores the interval, the back-off and the switch; still only in the installed app. At most
one GitHub request runs at a time - a click during a running check (daily or manual) receives that check's result.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

log = logging.getLogger("agent_start")

APP_TITLE = "Agent Start"
NOT_DUE_RECHECK_S = 600.0               # not due yet: look again in 10 min (cheap: no disk, no network)
PENDING_REPORT_S = 7 * 86400            # a failed update is reported on the next start only within a week
RETRY_INSTALL_MS = 1000                 # installer ready but Strata starting/stopping or a question open
EXIT_AFTER_LAUNCH_MS = 400

# check_now() return values
STARTED, JOINED, BUSY, UNAVAILABLE = "started", "joined", "busy", "unavailable"
# on_result(status, version) statuses (version: the current one for "current", the new one for "newer")
CURRENT, NEWER, ERROR = "current", "newer", "error"
UNSIGNED = "unsigned"                   # a newer release whose signature does not verify (version: that release)

ResultCallback = Callable[[str, "str | None"], Any]


def _short(name: str, limit: int = 22) -> str:
    """Same as main._short (the failure notes must read exactly as before)."""
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name if len(name) <= limit else name[: limit - 1] + "…"


# ------------------------------------------------------------------------------------------------ workers (values only)
def _check_update_worker(upd: Any, current: str, post: Callable, done: Callable) -> None:
    """Worker: one GitHub check. Posts (status, UpdateInfo as dict or None); status 'newer' | 'current' | 'error'."""
    status, info = "error", None
    try:
        status, info = upd.check(current)
    except Exception:  # noqa: BLE001
        log.debug("update check failed", exc_info=True)
    post(done, status, asdict(info) if info is not None else None)


def _number(value: Any) -> float:
    """A number from state.json, 0.0 for anything else (a hand-edited or damaged entry must not raise)."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _download_update_worker(upd: Any, current: str, post: Callable, progress: Callable, done: Callable,
                            stop_evt: Any, expected: str | None = None) -> None:
    """Worker: re-check GitHub (the URL and digest used are this minute's, not a cached copy), download, verify.
    expected: the version the user said yes to. If GitHub lists another one by now, nothing is downloaded: the
    new offer goes back (as info, with an error) and the user is asked again."""
    try:
        status, info = upd.check(current)
        if status == "unsigned":
            post(done, None, None, "The update's signature could not be verified - not installed")
            return
        if status != "newer" or info is None:
            post(done, None, None, "GitHub did not confirm the update" if status == "error" else "no newer version")
            return
        if expected is not None and info.version != expected:
            post(done, None, asdict(info), f"{info.version} is out now - click again")
            return
        last = [0.0]

        def prog(n: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] > 0.25 or n >= total:
                last[0] = now
                post(progress, n, total)

        path = upd.download(info, upd.default_download_dir(), progress=prog, stop_evt=stop_evt)
        post(done, path, asdict(info), None)
    except Exception as exc:  # noqa: BLE001
        post(done, None, None, str(exc) or exc.__class__.__name__)


def _cleanup_updates_worker(upd: Any, current: str) -> None:
    try:
        n = upd.cleanup_old(upd.default_download_dir(), current)
        if n:
            log.info("removed %d old update file(s)", n)
    except Exception:  # noqa: BLE001
        log.debug("update cleanup failed", exc_info=True)


# ------------------------------------------------------------------------------------------------ host interface
@dataclass
class UpdateHost:
    """What the controller needs from the app. All callables are called on the Tk thread, except post (any thread)."""
    post: Callable[..., None]                    # thread-safe: run fn(*args) on the Tk thread
    run_worker: Callable[..., None]              # run_worker(name, fn, *args) on a daemon thread
    call_later: Callable[[int, Callable[[], Any]], Any]   # Tk after(ms, fn)
    set_note: Callable[[str], None]              # one-off result line on the main window
    request_preview: Callable[[], None]          # redraw the idle line (shows / hides the offer)
    save_state_later: Callable[[str, Any], None]  # debounced state.json write
    save_state: Callable[[dict], Any]            # synchronous state.json merge-write (config_store.save_state)
    load_state: Callable[[], dict]               # config_store.load_state
    ask: Callable[[str], Any]                    # yes/no box owned by the main window (marks the box open)
    toast: Callable[[str, str], None]            # (title, message): a Windows toast, off the Tk thread
    engine_state: Callable[[], str]              # "IDLE" | "STARTING" | "ACTIVE" | "STOPPING" | "ERROR"
    dialog_open: Callable[[], bool]
    box_open: Callable[[], bool]
    release_singleton: Callable[[], None]
    finish_exit: Callable[[], None]
    available: Callable[[], bool]                # installed (frozen) build with the extras: the updater may run
    frozen: Callable[[], bool]                   # installed build: the installer gets /DIR=<this exe's folder>
    engine_name: Callable[[], str] = lambda: "Strata"   # the inference engine's name, for the texts
    stop_evt: Any = None                         # threading.Event set on app exit (cancels a download)
    acquire_singleton: Callable[[], None] = lambda: None   # undo release_singleton (the installer did not start)


class UpdateController:
    def __init__(self, host: UpdateHost, version: str, daily: bool = True, checked_at: Any = None,
                 updater_mod: Any = None) -> None:
        """daily: the "Check for updates daily" switch (state.json "update_check").
        checked_at: state.json "update_checked_at" as loaded (anything that is not a number counts as never)."""
        self.host = host
        self.version = version
        self._upd = updater_mod
        self.daily = bool(daily)
        self.no_check = not host.available() or not self.daily   # = the old AgentStartApp._no_update_check
        self.offer: Any = None                   # updater.UpdateInfo of a newer release (idle line, Settings link)
        self.unsigned: Any = None                # UpdateInfo of a newer release that was refused (no valid signature)
        self._unsigned_told: str | None = None   # that release's version once the user was told (one toast each)
        self.updating = False                    # confirm said yes: downloading / about to launch the installer
        self.checking = False                    # a GitHub check runs (daily or manual)
        self.next_at = 0.0                       # monotonic: no daily GitHub check before this
        self.checked_at: float | None = float(checked_at) if isinstance(checked_at, (int, float)) else None
        self.installing = False                  # the installer runs; the app is exiting
        self._manual = False                     # the running check was started (or joined) by check_now
        self._listeners: list[ResultCallback] = []

    @property
    def upd(self) -> Any:
        if self._upd is None:
            import updater
            self._upd = updater
        return self._upd

    # ------------------------------------------------------------------ queries for the app
    def offer_version(self) -> str | None:
        """Settings' update link / the idle line: the offered version, None while none (or one is installing)."""
        return self.offer.version if self.offer is not None and not self.updating else None

    def available(self) -> bool:
        return bool(self.host.available())

    # ------------------------------------------------------------------ first tick / periodic
    def first_tick(self) -> None:
        """The app's first refresh: report the previous run's update, clean old installers, maybe check."""
        self.report_result()
        if not self.no_check:
            self.host.run_worker("hc-update-clean", _cleanup_updates_worker, self.upd, self.version)
        self.maybe_check()

    def maybe_check(self) -> None:
        """Once a day (and an hour after a failed check): ask GitHub for the latest release, on a worker. The time
        is kept in memory too, so an unwritable state.json cannot turn this into a request every tick."""
        if self.no_check or self.updating or self.checking:
            return
        now_m = time.monotonic()
        if now_m < self.next_at:
            return
        upd = self.upd
        if not upd.due(self.checked_at, time.time()):
            self.next_at = now_m + NOT_DUE_RECHECK_S
            return
        self.checking = True
        self.next_at = now_m + upd.RETRY_AFTER_ERROR_S   # until the result says otherwise
        self._start_check()

    def check_now(self, on_result: ResultCallback | None = None) -> str:
        """Settings > Check for updates: check GitHub now, whatever the interval, back-off or daily switch say.
        Returns STARTED (a request went out), JOINED (one was already running; on_result gets its result), BUSY (an
        update is being downloaded / installed: no check) or UNAVAILABLE (running from source). After STARTED or
        JOINED, on_result(status, version) is called once on the Tk thread: (CURRENT, this version), (NEWER, the new
        version) or (ERROR, None). Its return value says whether the result was shown to the user (a dialog that has
        been closed returns False); a NEW version nobody was shown still gets the daily check's toast."""
        if not self.available():
            return UNAVAILABLE
        if self.updating or self.installing:
            return BUSY
        if on_result is not None and on_result not in self._listeners:
            self._listeners.append(on_result)
        self._manual = True
        if self.checking:
            log.info("update check requested; one is already running")
            return JOINED
        self.checking = True
        log.info("update check requested")
        self._start_check()
        return STARTED

    def _start_check(self) -> None:
        self.host.run_worker("hc-update-check", _check_update_worker, self.upd, self.version, self.host.post,
                             self._on_checked)

    def _on_checked(self, status: str, info_dict: dict | None) -> None:
        self.checking = False
        manual, self._manual = self._manual, False
        listeners, self._listeners = self._listeners, []
        if self.no_check and not manual:         # switched off while the daily check ran
            return
        upd = self.upd
        if status == "error":                    # offline / rate limited: keep what we knew
            if manual:
                # never sooner than a failed daily check would retry, never later than the schedule already was
                self.next_at = max(self.next_at, time.monotonic() + upd.RETRY_AFTER_ERROR_S)
                log.info("update check failed (requested); next daily check unchanged")
            else:
                log.info("update check failed; retrying in %d min", upd.RETRY_AFTER_ERROR_S // 60)
            self._notify(listeners, ERROR, None)
            return
        self.checked_at = time.time()
        self.next_at = time.monotonic() + upd.CHECK_INTERVAL_S
        new = upd.UpdateInfo(**info_dict) if info_dict else None
        if status == UNSIGNED:
            # a newer release without a valid signature: never offered, never downloaded; say so once
            self.offer, self.unsigned = None, new
            self.host.save_state_later("update_checked_at", self.checked_at)
            ver = new.version if new is not None else None
            shown = self._notify(listeners, UNSIGNED, ver)
            if ver and ver != self._unsigned_told:
                self._unsigned_told = ver
                log.warning("update %s is not offered: its signature could not be verified", ver)
                if not shown:
                    self.host.toast(APP_TITLE, f"Version {ver} is on GitHub, but its signature could not be "
                                               "verified, so it is not installed from here. See Settings > About.")
            self.host.request_preview()
            return
        self.unsigned = None
        known = self.offer.version if self.offer is not None else None
        self.offer = new
        self.host.save_state_later("update_checked_at", self.checked_at)
        shown = self._notify(listeners, NEWER if new is not None else CURRENT,
                             new.version if new is not None else self.version)
        if new is not None and new.version != known:
            log.info("update available: %s", new.version)
            if not shown:                        # Settings already says so where the user clicked
                self.host.toast(APP_TITLE, f"Version {new.version} is available. Install it from Settings, or "
                                           f"click the update line while {self.host.engine_name()} is off.")
        self.host.request_preview()

    @staticmethod
    def _notify(listeners: list[ResultCallback], status: str, version: str | None) -> bool:
        shown = False
        for cb in listeners:
            try:
                shown = bool(cb(status, version)) or shown
            except Exception:  # noqa: BLE001
                log.exception("update check result callback failed")
        return shown

    def open_unsigned_page(self) -> bool:
        """Settings: open the GitHub page of the release that was refused (its signature did not verify), so the
        user can read what is there. Only a page of this app's own repository is ever opened."""
        info = self.unsigned
        page = getattr(info, "page_url", None)
        repo = getattr(self.upd, "REPO", "")
        if not repo or not (isinstance(page, str) and page.startswith(f"https://github.com/{repo}/")):
            return False
        import webbrowser
        try:
            return bool(webbrowser.open(page))
        except Exception:  # noqa: BLE001
            log.debug("could not open the release page", exc_info=True)
            return False

    # ------------------------------------------------------------------ settings switch
    def set_daily(self, on: bool) -> bool:
        """The "Check for updates daily" switch. Off also withdraws the current offer (as before)."""
        self.daily = bool(on)
        self.host.save_state_later("update_check", self.daily)
        self.no_check = not self.host.available() or not on
        if not on:
            self.offer = None
            self.host.request_preview()
        else:
            self.next_at = 0.0
            self.maybe_check()
        log.info("daily update check: %s", "on" if on else "off")
        return True

    # ------------------------------------------------------------------ install
    def install(self) -> None:
        """Settings / the idle line: confirm, then re-check GitHub (fresh URL + digest), download, verify, run the
        installer and exit. Strata keeps running."""
        info = self.offer
        h = self.host
        if info is None or self.updating or h.box_open():
            return
        if h.engine_state() in ("STARTING", "STOPPING"):
            h.set_note(f"Update waits until {h.engine_name()} has started or stopped")
            return
        msg = (f"Install Agent Start {info.version}?\n\nIt downloads the installer from GitHub "
               f"(about {info.size / 1e6:.0f} MB), checks it, closes this app, upgrades it and starts it again.")
        if h.engine_state() == "ACTIVE":
            msg += f"\n\n{h.engine_name()} keeps running; the new version picks it up."
        if not h.ask(msg):
            return
        if h.engine_state() in ("STARTING", "STOPPING") or self.updating:
            return
        self.updating = True
        h.set_note(f"Downloading update {info.version}…")
        h.run_worker("hc-update-download", _download_update_worker, self.upd, self.version, h.post,
                     self._progress, self._downloaded, h.stop_evt, info.version)

    def _progress(self, done: int, total: int) -> None:
        if self.updating and total:
            self.host.set_note(f"Downloading update · {100 * done / total:.0f}%")

    def _downloaded(self, path: str | None, info_dict: dict | None, err: str | None) -> None:
        h = self.host
        if err or not path or not info_dict:
            self.updating = False
            if info_dict and not path:           # another version than the one confirmed: offer that one
                try:
                    self.offer = self.upd.UpdateInfo(**info_dict)
                    h.request_preview()
                except Exception:  # noqa: BLE001
                    log.debug("could not refresh the offer", exc_info=True)
            log.warning("update download failed: %s", err)
            h.set_note(f"Update failed\n{_short(err or 'download failed', 40)}")
            return
        # never exit mid-start / mid-stop / with a dialog waiting for an answer: try again in a second
        if h.engine_state() in ("STARTING", "STOPPING") or h.dialog_open() or h.box_open():
            h.call_later(RETRY_INSTALL_MS, lambda: self._downloaded(path, info_dict, err))
            return
        upd = self.upd
        info = upd.UpdateInfo(**info_dict)
        log_path = os.path.join(upd.default_download_dir(), "install.log")
        install_dir = os.path.dirname(sys.executable) if h.frozen() else None
        h.save_state({"update_pending": {"version": info.version, "from": self.version,
                                         "at": time.time(), "log": log_path}})
        h.release_singleton()                    # the installer's AppMutex check must not see us; it waits for our pid
        try:
            upd.launch_installer(path, info.sha256, install_dir=install_dir, log_path=log_path,
                                 wait_pid=os.getpid())
        except Exception as exc:  # noqa: BLE001
            self.updating = False
            h.acquire_singleton()                # we keep running: be the single instance again
            h.save_state({"update_pending": None})
            log.exception("could not start the installer")
            h.set_note(f"Update failed\n{_short(str(exc), 40)}")
            return
        self.installing = True
        log.info("update installer started (%s); exiting so it can replace the app", path)
        h.set_note("Installing update · Agent Start restarts in a moment")
        h.call_later(EXIT_AFTER_LAUNCH_MS, h.finish_exit)   # Strata (if running) keeps running; the installer waits

    def report_result(self) -> None:
        """First tick after a start: did the update the previous run started go in?"""
        pend = self.host.load_state().get("update_pending")
        if not isinstance(pend, dict):
            return
        self.host.save_state({"update_pending": None})
        if pend.get("version") == self.version:
            log.info("updated from %s to %s", pend.get("from"), self.version)
            self.host.set_note(f"Updated to {self.version}")
        elif time.time() - _number(pend.get("at")) < PENDING_REPORT_S:
            log.warning("the update to %s did not install (log: %s)", pend.get("version"), pend.get("log"))
            self.host.set_note(f"Update to {pend.get('version')} did not install\nDetails: updates\\install.log")
