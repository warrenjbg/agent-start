"""System-tray (notification-area) icon for Agent Start, built directly on pywin32 + ctypes (no pystray).

THREADING RULES - READ BEFORE WIRING THIS INTO TK
-------------------------------------------------
* The icon lives on its OWN daemon thread ("tray"), which owns a hidden top-level window and a Win32 message loop.
  Every Shell_NotifyIcon call happens on that thread only.
* on_open / on_toggle / on_quit / on_lost are called ON THE TRAY THREAD, with no arguments. They must NOT touch Tk.
  Marshal to Tk yourself, e.g. `on_open=lambda: app.post(app.show_from_tray)` (a queue drained by the Tk pump).
  Keep the callbacks short: the tray's menu/clicks are unresponsive while one runs.
* on_switch(key: str) and on_balloon_click(key: str | None) are called on the tray thread too, with ONE argument
  (same rules). on_switch gets the key of the model picked in the "Switch model" submenu; on_balloon_click gets the
  key passed to the notify() whose balloon was clicked, or None (no key given, or the balloon already timed out /
  was replaced). Without on_balloon_click a balloon click calls on_open instead.
* set_status() and notify() are thread-safe and non-blocking: they store the wanted state and PostMessage a
  private WM_APP message to the tray thread, which re-renders. Call them from the Tk thread as often as you like;
  identical states are a no-op and bursts coalesce into one render. set_models() is thread-safe too; it only
  stores the model list (the menu is built from it on each right-click), identical calls are a no-op.
* stop() is idempotent and joins the thread (<= 2 s). Do NOT call stop() from inside a tray callback expecting a
  join (it is detected and the join is skipped); call it from Tk (e.g. in destroy()). start() refuses (returns
  False) while a previous run's thread is still alive (a stop() whose join timed out).
* start() blocks the caller until the icon is added (<= 3 s). From Tk use start_async(on_result) instead: it does the
  waiting on a helper thread ("tray-start") and calls on_result(bool) THERE (marshal to Tk yourself). A stop() issued
  before the helper got going cancels it (on_result(False)).
* on_lost (optional) means "the icon is gone and could not be brought back - show your window": it fires once per
  loss when re-adding after an Explorer restart keeps failing for READD_TIMEOUT_S, and when the tray thread ends for
  any reason other than stop(). It never fires for a failed start() (that returns False instead). After a loss the
  tray keeps retrying slowly; a later successful re-add makes is_running True again.
* Window-close requests aimed at the hidden window (WM_CLOSE from taskkill without /F, an installer's Restart
  Manager, WM_ENDSESSION at logoff) call on_quit and never destroy the window; only stop() does that.

Behaviour: left click / double click -> on_open. Right click -> popup menu at the cursor:
    "Open <title>" (default, bold) | toggle item (hidden when label None, greyed when disabled) |
    "Switch model" > submenu (only with >= 2 set_models() items; current one checked + greyed; all greyed when
    set_models(enabled=False)) | --- | "Quit"
A pick in the submenu is ignored when its key is no longer in the latest set_models() items (changed while the
menu was open), is the current one, or the submenu is disabled now. Clicking a notify() balloon -> on_balloon_click.
The icon = assets/app.png scaled to the small-icon size with a status dot (bottom-right, 1 px dark outline).
The icon is re-added automatically when Explorer restarts ("TaskbarCreated"), retrying every READD_INTERVAL_S.
notify() balloons requested while the icon is not in the shell are kept (the latest one only) and shown once it is.
NIM_SETVERSION / NOTIFYICON_VERSION_4 is deliberately not used: the legacy callback layout (lParam = mouse message)
is what the click handling below expects.
"""
from __future__ import annotations

import ctypes
import itertools
import logging
import os
import struct
import threading
import time
from ctypes import wintypes
from typing import Callable

import win32api
import win32con
import win32gui
from PIL import Image, ImageColor, ImageDraw

log = logging.getLogger("agent_start")   # the app's logger: tray warnings reach the app log

# --- private messages / ids -----------------------------------------------------------------------------------------
WM_TRAY = win32con.WM_APP + 1          # Shell callback message (lParam = mouse message, wParam = uID)
WM_TRAY_UPDATE = win32con.WM_APP + 2   # set_status() -> re-render on the tray thread
WM_TRAY_NOTIFY = win32con.WM_APP + 3   # notify() -> balloon on the tray thread
WM_TRAY_STOP = win32con.WM_APP + 4     # stop() -> delete icon, destroy window, quit loop
WM_TRAY_SYNC = win32con.WM_APP + 5     # test hook: wParam is recorded in _sync_seen (proves earlier posts ran)
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_TIMER = 0x0113
TIMER_READD = 1

ICON_UID = 1
ID_OPEN = 1001
ID_TOGGLE = 1002
ID_QUIT = 1003
ID_MODEL_BASE = 2000                   # "Switch model" items: ID_MODEL_BASE + index (index < MAX_MODELS)
MAX_MODELS = 100
ID_MODELS_POPUP = -1                   # marker in _menu_spec() for the submenu entry (popups have no command id)
MODELS_TEXT = "Switch model"
MODEL_LABEL_MAX = 40

NIN_SELECT = win32con.WM_USER + 0
NIN_KEYSELECT = NIN_SELECT | 0x1
NIN_BALLOONSHOW = win32con.WM_USER + 2
NIN_BALLOONHIDE = win32con.WM_USER + 3
NIN_BALLOONTIMEOUT = win32con.WM_USER + 4
NIN_BALLOONUSERCLICK = win32con.WM_USER + 5
WM_CONTEXTMENU = 0x007B
NIIF_INFO = 0x1
NIIF_NOSOUND = 0x10
SM_CXSMICON = 49
TIP_MAX = 127                          # szTip is 128 WCHARs incl. the terminator
OUTLINE_HEX = "#141415"                # theme.BG: the dot's 1 px dark outline
START_TIMEOUT_S = 3.0
JOIN_TIMEOUT_S = 2.0
READD_INTERVAL_S = 1.0                 # NIM_ADD retry period after TaskbarCreated (Explorer may still be starting)
READD_TIMEOUT_S = 60.0                 # ... for this long; then on_lost
READD_SLOW_INTERVAL_S = 30.0           # keep trying this often after on_lost (Explorer may come up late)
MSGFLT_ALLOW = 1

_CLASS_SEQ = itertools.count(1)        # per-run window class suffix (a shared class would share the first wndproc)

_user32 = ctypes.WinDLL("user32", use_last_error=True)   # own instance: argtypes don't leak into other modules
_user32.CreateIconFromResourceEx.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
_user32.CreateIconFromResourceEx.restype = wintypes.HANDLE
_user32.DestroyIcon.argtypes = [wintypes.HANDLE]
_user32.DestroyIcon.restype = wintypes.BOOL
_user32.EndMenu.restype = wintypes.BOOL
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
_user32.KillTimer.restype = wintypes.BOOL
try:
    _user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
    _user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
except AttributeError:  # pragma: no cover - pre-Win7
    pass
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
try:
    _user32.GetDpiForSystem.restype = wintypes.UINT
    _user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    _user32.GetSystemMetricsForDpi.restype = ctypes.c_int
    _HAVE_DPI_API = True
except AttributeError:  # pre-1607 Windows
    _HAVE_DPI_API = False


def small_icon_size() -> int:
    """Small-icon edge in px for the system DPI (what the notification area uses), DPI-awareness independent."""
    size = 0
    try:
        if _HAVE_DPI_API:
            size = int(_user32.GetSystemMetricsForDpi(SM_CXSMICON, int(_user32.GetDpiForSystem())))
    except Exception:  # noqa: BLE001
        size = 0
    if size <= 0:
        size = int(_user32.GetSystemMetrics(SM_CXSMICON)) or 16
    return max(16, min(size, 256))


def dot_geometry(size: int) -> tuple[int, int, int]:
    """(x0, y0, diameter) of the status dot incl. its outline, flush with the bottom-right corner."""
    d = max(7, round(size * 0.5))
    return size - d, size - d, d


def hicon_from_image(img: Image.Image) -> int:
    """32-bpp alpha HICON from a PIL image (DIB icon resource -> CreateIconFromResourceEx). Caller DestroyIcon()s it."""
    img = img.convert("RGBA")
    w, h = img.size
    xor = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")
    mask = b"\x00" * (((w + 31) // 32) * 4 * h)      # alpha channel rules; AND mask all-opaque
    hdr = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
    data = hdr + xor + mask
    buf = ctypes.create_string_buffer(data, len(data))
    hicon = _user32.CreateIconFromResourceEx(buf, len(data), True, 0x00030000, w, h, 0)
    if not hicon:
        raise OSError(ctypes.get_last_error(), "CreateIconFromResourceEx failed")
    return int(hicon)


def _model_label(label: object) -> str:
    """Menu text for a model: single line, at most MODEL_LABEL_MAX chars (ellipsis when cut)."""
    text = " ".join(str(label).split())
    return text if len(text) <= MODEL_LABEL_MAX else text[:MODEL_LABEL_MAX - 1].rstrip() + "\u2026"


def _norm_hex(led_hex: str | None) -> str | None:
    if led_hex is None:
        return None
    try:
        r, g, b = ImageColor.getrgb(str(led_hex))[:3]
        return f"#{r:02X}{g:02X}{b:02X}"
    except (ValueError, TypeError):
        log.warning("tray: bad LED colour %r; drawing no dot", led_hex)
        return None


class TrayIcon:
    """Notification-area icon on a dedicated thread. See the module docstring for the threading rules."""

    def __init__(self, title: str, icon_png: str, on_open: Callable[[], None] | None,
                 on_toggle: Callable[[], None] | None, on_quit: Callable[[], None] | None,
                 class_name: str = "AgentStartTray", on_lost: Callable[[], None] | None = None,
                 on_switch: Callable[[str], None] | None = None,
                 on_balloon_click: Callable[[str | None], None] | None = None) -> None:
        self.title = str(title)
        self.icon_png = icon_png
        self.on_open = on_open
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.on_lost = on_lost
        self.on_switch = on_switch                # (key) on the tray thread
        self.on_balloon_click = on_balloon_click  # (key | None) on the tray thread; None -> on_open
        self.class_name = class_name          # base name; each run registers "<base>_<pid>_<n>" (see _run_class)
        # Seconds during which a second left-click "up" is folded into the first (a double click = 2 ups).
        self.open_debounce_s = max(0.2, min(win32gui.GetDoubleClickTime() / 1000.0, 0.8))

        self._lock = threading.Lock()          # wanted state, _hwnd, _update_posted, _balloon, _stop_requested
        self._life = threading.Lock()          # start/stop bookkeeping (_thread, _gen)
        # wanted state (written by any thread under _lock); applied state (tray thread only)
        self._want: tuple[str | None, str, str | None, bool] = (None, self.title[:TIP_MAX], None, True)
        self._applied: tuple | None = None
        self._update_posted = False
        self._balloon: tuple[str, str, str | None] | None = None   # latest unshown notify() (title, msg, key)
        # set_models() state (any thread, under _lock): ((label, key), ...), current key, enabled
        self._models: tuple[tuple[tuple[str, str], ...], str | None, bool] = ((), None, True)
        # tray thread only
        self._menu_model_ids: dict[int, str] | None = None   # cmd id -> key of the LAST built menu (None: none)
        self._balloon_key_last: str | None = None   # key of the last balloon handed to the shell
        self._balloon_key: str | None = None        # key of the balloon currently up (None after timeout/hide)

        self._thread: threading.Thread | None = None
        self._gen = 0                          # bumped by stop(): cancels a start_async() that has not begun
        self._run_class = ""
        self._hwnd = 0
        self._hinst = 0
        self._hicon = 0
        self._icon_size = 16
        self._added = False
        self._ready = threading.Event()
        self._start_ok = False
        self._stop_requested = False
        self._ever_added = False               # this run had the icon in the shell at least once
        self._lost_reported = False            # on_lost already fired for the current loss
        self._readd_deadline = 0.0
        self._readd_attempts = 0
        self._in_menu = False                  # TrackPopupMenu is on the tray thread's stack
        self._stop_pending = False             # WM_TRAY_STOP arrived during the menu: destroy once it returns
        self._sync_seen = 0
        self._taskbar_created = 0
        self._last_open = 0.0
        self._menu_label_shown: str | None = None
        self._base_cache: dict[int, Image.Image] = {}

    # ----------------------------------------------------------------------------------------------- public API
    @property
    def is_running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive() and self._added and not self._stop_requested)

    def start(self) -> bool:
        """Add the icon on a new tray thread. True once it is in the notification area (<= 3 s). Never raises.
        Blocks the caller; from a UI thread prefer start_async()."""
        return self._start(None)

    def start_async(self, on_result: Callable[[bool], None] | None = None) -> None:
        """Non-blocking start(): waits on a helper thread and calls on_result(ok) there (NOT on the caller's
        thread). A stop() before the helper begins cancels the start (on_result(False)). Never raises."""
        with self._life:
            gen = self._gen

        def run() -> None:
            ok = False
            try:
                ok = self._start(gen)
            finally:
                if on_result is not None:
                    try:
                        on_result(bool(ok))
                    except Exception:  # noqa: BLE001
                        log.exception("tray: start_async on_result raised")

        try:
            threading.Thread(target=run, name="tray-start", daemon=True).start()
        except Exception:  # noqa: BLE001 - cannot spawn a thread: report failure right here
            log.exception("tray: start_async could not spawn its helper")
            if on_result is not None:
                try:
                    on_result(False)
                except Exception:  # noqa: BLE001
                    log.exception("tray: start_async on_result raised")

    def set_status(self, led_hex: str | None, tooltip: str, toggle_label: str | None,
                   toggle_enabled: bool = True) -> None:
        """Thread-safe. Wanted dot colour (None = no dot), tooltip (<= 127 chars) and toggle menu item."""
        want = (_norm_hex(led_hex), str(tooltip or self.title)[:TIP_MAX],
                None if toggle_label is None else str(toggle_label), bool(toggle_enabled))
        with self._lock:
            if want == self._want:
                return
            self._want = want
            post = bool(not self._update_posted and self._hwnd)
            if post:
                self._update_posted = True
        if post and not self._post(WM_TRAY_UPDATE):
            with self._lock:                   # not delivered: let the next set_status() try again
                self._update_posted = False

    def set_models(self, items: list[tuple[str, str]] | None, current: str | None, enabled: bool = True) -> None:
        """Thread-safe. Content of the "Switch model" submenu: items = [(label, key), ...] (labels cut to ~40 chars,
        at most MAX_MODELS shown); `current` is checked and greyed; enabled=False greys the whole submenu. None or
        fewer than 2 items -> no submenu. A pick calls on_switch(key) on the tray thread. Unchanged -> no-op."""
        norm: list[tuple[str, str]] = []
        for item in (items or ())[:MAX_MODELS]:
            label, key = item
            norm.append((_model_label(label), str(key)))
        want = (tuple(norm), None if current is None else str(current), bool(enabled))
        with self._lock:
            if want == self._models:
                return
            self._models = want

    def notify(self, title: str, message: str, key: str | None = None) -> None:
        """Best-effort balloon / toast from the icon. Thread-safe. Only the latest pending one is shown; one requested
        before start() or while the icon is missing is shown once the icon is (back) in the shell. Clicking it calls
        on_balloon_click(key) (key None if none was given or the balloon timed out), or on_open without that."""
        with self._lock:
            self._balloon = (str(title)[:63], str(message)[:255], None if key is None else str(key))
            hwnd = self._hwnd
        if hwnd:
            self._post(WM_TRAY_NOTIFY)

    def stop(self) -> None:
        """Remove the icon, destroy the window, end the loop and join the thread (<= 2 s). Idempotent."""
        with self._life:
            self._gen += 1                     # cancels a pending start_async()
            t = self._thread
            if t is None:
                return
            with self._lock:
                self._stop_requested = True
                hwnd = self._hwnd
        if hwnd and not self._post(WM_TRAY_STOP, hwnd=hwnd) and win32gui.IsWindow(hwnd):
            log.warning("tray: could not post the stop message")
        if t is not threading.current_thread():
            t.join(JOIN_TIMEOUT_S)
            if t.is_alive():
                log.warning("tray: thread did not end within %.1f s", JOIN_TIMEOUT_S)
            else:
                with self._life:
                    if self._thread is t:
                        self._thread = None
        # (called from a tray callback: the loop ends right after the callback returns; no self-join)

    # ------------------------------------------------------------------------------------- start internals
    def _start(self, gen: int | None) -> bool:
        try:
            with self._life:
                if gen is not None and gen != self._gen:
                    log.info("tray: start cancelled by stop()")
                    return False
                t = self._thread
                if t is not None and t.is_alive():
                    if self._stop_requested:
                        log.warning("tray: previous tray thread still alive (stop timed out); start refused")
                        return False
                    ready, fresh = self._ready, False          # already running / starting: share that run
                else:
                    ready, fresh = threading.Event(), True
                    self._reset_run_state()
                    self._ready = ready
                    self._run_class = f"{self.class_name}_{os.getpid()}_{next(_CLASS_SEQ)}"
                    t = threading.Thread(target=self._thread_main, args=(ready, self._run_class),
                                         name="tray", daemon=True)
                    self._thread = t
                    t.start()
            if not fresh:
                ready.wait(START_TIMEOUT_S)
                return self.is_running
            if not ready.wait(START_TIMEOUT_S) or not self._start_ok:
                log.warning("tray: start failed (ready=%s)", ready.is_set())
                self.stop()
                return False
            return True
        except Exception:  # noqa: BLE001
            log.exception("tray: start raised")
            try:
                self.stop()
            except Exception:  # noqa: BLE001
                pass
            return False

    def _reset_run_state(self) -> None:
        with self._lock:
            self._stop_requested = False
            self._hwnd = 0
            self._update_posted = False
        self._start_ok = False
        self._added = False
        self._applied = None
        self._ever_added = False
        self._lost_reported = False
        self._readd_deadline = 0.0
        self._readd_attempts = 0
        self._in_menu = False
        self._stop_pending = False
        self._menu_model_ids = None
        self._balloon_key_last = self._balloon_key = None

    # ------------------------------------------------------------------------------------- helpers for tests
    def _menu_items(self) -> list[tuple[str, bool, bool]]:
        """Menu model as (text, enabled, is_default); the separator is ("-", False, False), the submenu entry is
        ("Switch model", enabled, False) - its items are in _submenu_items()."""
        return [(text, enabled, default) for _id, text, enabled, default in self._menu_spec()]

    def _submenu_items(self) -> list[tuple[str, bool, bool]]:
        """"Switch model" submenu as (label, enabled, checked); [] when there is no submenu."""
        return [(text, enabled, checked) for _id, text, _key, enabled, checked in self._submenu_spec()[1]]

    def _snapshot(self) -> tuple[tuple, tuple]:
        with self._lock:
            return self._want, self._models

    def _menu_spec(self, snap: tuple[tuple, tuple] | None = None) -> list[tuple[int, str, bool, bool]]:
        want, models = snap or self._snapshot()
        _led, _tip, label, enabled = want
        items = [(ID_OPEN, f"Open {self.title}", True, True)]
        if label is not None:
            items.append((ID_TOGGLE, label, enabled, False))
        if len(models[0]) >= 2:
            items.append((ID_MODELS_POPUP, MODELS_TEXT, models[2], False))
        items.append((0, "-", False, False))
        items.append((ID_QUIT, "Quit", True, False))
        return items

    def _submenu_spec(self, snap: tuple[tuple, tuple] | None = None
                      ) -> tuple[bool, list[tuple[int, str, str, bool, bool]]]:
        """(submenu enabled, [(cmd id, label, key, item enabled, checked), ...]); empty list = no submenu."""
        _want, (items, current, enabled) = snap or self._snapshot()
        if len(items) < 2:
            return enabled, []
        return enabled, [(ID_MODEL_BASE + i, label, key, enabled and key != current, key == current)
                         for i, (label, key) in enumerate(items)]

    def _base(self, size: int) -> Image.Image:
        img = self._base_cache.get(size)
        if img is None:
            try:
                with Image.open(self.icon_png) as src:
                    img = src.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
            except Exception:  # noqa: BLE001 - missing asset: plain graphite tile, the tray must still work
                log.warning("tray: cannot load %s; using a plain tile", self.icon_png)
                img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                ImageDraw.Draw(img).rounded_rectangle((0, 0, size - 1, size - 1), radius=max(2, size // 5),
                                                      fill=(38, 38, 42, 255))
            self._base_cache[size] = img
        return img.copy()

    def _render(self, led_hex: str | None, size: int | None = None) -> Image.Image:
        """The tray image: app icon at `size` (default: current small-icon size) + status dot (None = none)."""
        size = int(size or self._icon_size or small_icon_size())
        img = self._base(size)
        led = _norm_hex(led_hex)
        if led is None:
            return img
        x0, y0, d = dot_geometry(size)
        ss = 4                                            # supersample for smooth edges at 16-32 px
        layer = Image.new("RGBA", (d * ss, d * ss), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        draw.ellipse((0, 0, d * ss - 1, d * ss - 1), fill=ImageColor.getrgb(OUTLINE_HEX) + (255,))
        draw.ellipse((ss, ss, (d - 1) * ss - 1, (d - 1) * ss - 1), fill=ImageColor.getrgb(led) + (255,))
        layer = layer.resize((d, d), Image.Resampling.LANCZOS)
        img.alpha_composite(layer, (x0, y0))
        return img

    # ------------------------------------------------------------------------------------------ tray thread
    def _post(self, msg: int, wparam: int = 0, lparam: int = 0, hwnd: int = 0) -> bool:
        hwnd = hwnd or self._hwnd
        if not hwnd:
            return False
        try:
            win32gui.PostMessage(hwnd, msg, wparam, lparam)
            return True
        except Exception:  # noqa: BLE001 - window already gone / queue full
            return False

    def _thread_main(self, ready: threading.Event, cls_name: str) -> None:
        hwnd = 0
        hinst = 0
        registered = False
        try:
            hinst = win32api.GetModuleHandle(None)
            self._hinst = hinst
            self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            wc.lpfnWndProc = self._wndproc
            win32gui.RegisterClass(wc)            # unique per run: never inherits another instance's wndproc
            registered = True
            # Hidden top-level (not HWND_MESSAGE): message-only windows never get the TaskbarCreated broadcast.
            hwnd = win32gui.CreateWindowEx(0, cls_name, self.title, win32con.WS_OVERLAPPED,
                                           0, 0, 0, 0, 0, 0, hinst, None)
            self._allow_taskbar_created(hwnd)
            with self._lock:
                self._hwnd = hwnd                  # stop() reads _hwnd and sets _stop_requested under the same lock
            self._icon_size = small_icon_size()
            deadline = time.monotonic() + START_TIMEOUT_S - 0.5
            while not self._stop_requested:
                if self._add_icon():
                    break
                if time.monotonic() > deadline:
                    break
                time.sleep(0.2)
            self._start_ok = self._added and not self._stop_requested
            ready.set()
            if self._start_ok:
                self._ever_added = True
                self._after_add()                  # set_status()/notify() calls made during start
                win32gui.PumpMessages()
        except Exception:  # noqa: BLE001
            log.exception("tray: thread failed")
        finally:
            ready.set()
            self._teardown(hwnd, registered, cls_name, hinst)
            if self._ever_added and not self._stop_requested and not self._lost_reported:
                self._lost_reported = True
                log.warning("tray: tray thread ended without stop(); icon lost")
                self._call("on_lost", self.on_lost)

    def _allow_taskbar_created(self, hwnd: int) -> None:
        """An elevated process (e.g. launched by the installer) gets no TaskbarCreated from Explorer without this."""
        try:
            _user32.ChangeWindowMessageFilterEx(hwnd, self._taskbar_created, MSGFLT_ALLOW, None)
        except Exception:  # noqa: BLE001
            pass

    def _teardown(self, hwnd: int, registered: bool, cls_name: str, hinst: int) -> None:
        self._delete_icon()
        if hwnd:
            try:
                _user32.KillTimer(hwnd, TIMER_READD)
                if win32gui.IsWindow(hwnd):
                    win32gui.DestroyWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._hwnd = 0
            self._update_posted = False
        if self._hicon:
            _user32.DestroyIcon(self._hicon)
            self._hicon = 0
        if registered:
            try:
                win32gui.UnregisterClass(cls_name, hinst)
            except Exception:  # noqa: BLE001
                log.debug("tray: UnregisterClass(%s) failed", cls_name)
        self._added = False

    def _nid(self, flags: int, tip: str | None = None) -> tuple:
        return (self._hwnd, ICON_UID, flags, WM_TRAY, self._hicon, tip if tip is not None else self._want[1])

    def _add_icon(self) -> bool:
        """(Re)create the HICON at the current size and NIM_ADD it. Tray thread only."""
        try:
            with self._lock:
                want = self._want
            old = self._swap_icon(self._render(want[0], self._icon_size))
            if old:
                _user32.DestroyIcon(old)
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self._nid(
                win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP, want[1]))
            self._added = True
            self._applied = want
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("tray: NIM_ADD failed: %s", e)
            return False

    def _after_add(self) -> None:
        """The icon is (back) in the shell: stop retrying, catch up on status and the pending balloon."""
        self._kill_retry_timer()
        self._readd_attempts = 0
        self._lost_reported = False
        self._apply()
        self._show_balloons()

    def _delete_icon(self) -> None:
        if self._added and self._hwnd:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self._hwnd, ICON_UID))
            except Exception:  # noqa: BLE001
                pass
        self._added = False

    def _swap_icon(self, img: Image.Image) -> int:
        new = hicon_from_image(img)
        old, self._hicon = self._hicon, new
        return old

    def _apply(self) -> None:
        """Bring the shell icon up to the wanted state (tray thread)."""
        with self._lock:
            want = self._want
            self._update_posted = False
        if want == self._applied or not self._added:
            return
        prev = self._applied
        flags = 0
        old_icon = 0
        try:
            if prev is None or prev[0] != want[0]:
                old_icon = self._swap_icon(self._render(want[0], self._icon_size))
                flags |= win32gui.NIF_ICON
            if prev is None or prev[1] != want[1]:
                flags |= win32gui.NIF_TIP
            if flags:
                win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self._nid(flags, want[1]))
            self._applied = want
        except Exception as e:  # noqa: BLE001
            log.warning("tray: update failed: %s", e)
        finally:
            if old_icon:
                _user32.DestroyIcon(old_icon)             # the shell keeps its own copy

    def _show_balloons(self) -> None:
        """Show the latest pending balloon. Kept pending while the icon is not in the shell."""
        if not self._added:
            return
        with self._lock:
            item, self._balloon = self._balloon, None
        if item is not None:
            title, msg, key = item
            self._balloon_key_last = self._balloon_key = key
            self._show_balloon(title, msg)

    def _on_balloon_event(self, event: int) -> None:
        if event == NIN_BALLOONSHOW:
            self._balloon_key = self._balloon_key_last      # (a replaced balloon's TIMEOUT may have come first)
        elif event in (NIN_BALLOONTIMEOUT, NIN_BALLOONHIDE):
            self._balloon_key = None
        elif event == NIN_BALLOONUSERCLICK:
            key, self._balloon_key = self._balloon_key, None
            if self.on_balloon_click is not None:
                self._call("on_balloon_click", self.on_balloon_click, key)
            else:
                self._call("on_open", self.on_open)

    def _show_balloon(self, title: str, msg: str) -> None:
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (
                self._hwnd, ICON_UID, win32gui.NIF_INFO, WM_TRAY, self._hicon, self._want[1],
                msg, 10000, title, NIIF_INFO))
        except Exception as e:  # noqa: BLE001
            log.debug("tray: balloon failed: %s", e)

    def _call(self, name: str, fn: Callable[..., None] | None, *args: object) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("tray: %s callback raised", name)

    # --- Explorer restart: re-add with retries ---
    def _set_retry_timer(self, seconds: float) -> None:
        if not _user32.SetTimer(self._hwnd, TIMER_READD, max(10, int(seconds * 1000)), None):
            log.warning("tray: SetTimer failed (%s); icon re-add will not be retried", ctypes.get_last_error())

    def _kill_retry_timer(self) -> None:
        if self._hwnd:
            _user32.KillTimer(self._hwnd, TIMER_READD)

    def _on_taskbar_created(self, hwnd: int) -> None:
        # Explorer restarted: the old icon is gone. DPI may have changed too -> re-render at the new size.
        self._icon_size = small_icon_size()
        self._base_cache.clear()
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, ICON_UID))
        except Exception:  # noqa: BLE001
            pass
        self._added = False
        self._readd_attempts = 1
        if self._add_icon():
            log.info("tray: TaskbarCreated -> icon re-added")
            self._after_add()
            return
        log.info("tray: TaskbarCreated -> NIM_ADD failed; retrying every %.1f s for %.0f s",
                 READD_INTERVAL_S, READD_TIMEOUT_S)
        self._readd_deadline = time.monotonic() + READD_TIMEOUT_S
        self._set_retry_timer(READD_INTERVAL_S)

    def _on_retry_timer(self) -> None:
        if self._added:
            self._kill_retry_timer()
            return
        self._readd_attempts += 1
        if self._add_icon():
            log.info("tray: icon re-added after %d attempts", self._readd_attempts)
            self._after_add()
            return
        if time.monotonic() >= self._readd_deadline and not self._lost_reported:
            self._lost_reported = True
            log.warning("tray: icon could not be re-added within %.0f s; reporting it lost", READD_TIMEOUT_S)
            self._set_retry_timer(READD_SLOW_INTERVAL_S)
            self._call("on_lost", self.on_lost)

    # --- clicks / menu ---
    def _open_from_click(self) -> None:
        now = time.monotonic()
        if now - self._last_open < self.open_debounce_s:
            return
        self._last_open = now
        self._call("on_open", self.on_open)

    def _show_menu(self) -> None:
        if self._in_menu:                                  # a click dispatched inside the menu's modal loop
            return
        hwnd = self._hwnd
        menu = win32gui.CreatePopupMenu()
        self._in_menu = True
        try:
            snap = self._snapshot()                        # one consistent view for the menu and its submenu
            shown_label = None
            model_ids: dict[int, str] = {}
            for item_id, text, enabled, default in self._menu_spec(snap):
                if item_id == 0:
                    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                    continue
                flags = win32con.MF_STRING | (0 if enabled else win32con.MF_GRAYED)
                if item_id == ID_MODELS_POPUP:
                    sub = self._build_models_menu(snap, model_ids)
                    try:
                        win32gui.AppendMenu(menu, flags | win32con.MF_POPUP, sub, text.replace("&", "&&"))
                    except Exception:  # noqa: BLE001 - not attached: free it ourselves
                        win32gui.DestroyMenu(sub)
                        raise
                    continue                               # (attached: DestroyMenu(menu) frees it)
                win32gui.AppendMenu(menu, flags, item_id, text.replace("&", "&&"))
                if item_id == ID_TOGGLE:
                    shown_label = text
            win32gui.SetMenuDefaultItem(menu, ID_OPEN, False)
            self._menu_label_shown = shown_label
            self._menu_model_ids = model_ids
            self._track_popup(menu, hwnd)
            if not self._stop_pending and win32gui.IsWindow(hwnd):
                win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        finally:
            self._in_menu = False
            try:
                win32gui.DestroyMenu(menu)
            finally:
                if self._stop_pending:                     # stop() arrived while the menu was open
                    self._stop_pending = False
                    self._destroy(hwnd)

    def _build_models_menu(self, snap: tuple[tuple, tuple], model_ids: dict[int, str]) -> int:
        """The "Switch model" popup (caller attaches or destroys it); fills model_ids with cmd id -> key."""
        sub = win32gui.CreatePopupMenu()
        try:
            checked_id = 0
            specs = self._submenu_spec(snap)[1]
            for cmd_id, label, key, enabled, checked in specs:
                flags = win32con.MF_STRING | (0 if enabled else win32con.MF_GRAYED)
                if checked:
                    flags |= win32con.MF_CHECKED
                    checked_id = cmd_id
                win32gui.AppendMenu(sub, flags, cmd_id, label.replace("&", "&&"))
                model_ids[cmd_id] = key
            if checked_id and specs:
                try:                                       # radio bullet instead of a tick (cosmetic)
                    win32gui.CheckMenuRadioItem(sub, specs[0][0], specs[-1][0], checked_id, win32con.MF_BYCOMMAND)
                except Exception:  # noqa: BLE001
                    pass
            return sub
        except Exception:
            win32gui.DestroyMenu(sub)
            raise

    def _track_popup(self, menu: int, hwnd: int) -> None:
        """Modal popup menu at the cursor (returns when it closes). Separate method so tests can fake it."""
        x, y = win32gui.GetCursorPos()
        try:
            win32gui.SetForegroundWindow(hwnd)              # else the menu never dismisses on an outside click
        except Exception:  # noqa: BLE001
            pass
        align = (win32con.TPM_RIGHTALIGN if _user32.GetSystemMetrics(win32con.SM_MENUDROPALIGNMENT)
                 else win32con.TPM_LEFTALIGN)
        win32gui.TrackPopupMenu(menu, align | win32con.TPM_BOTTOMALIGN | win32con.TPM_RIGHTBUTTON,
                                x, y, 0, hwnd, None)

    def _end_menu(self) -> None:
        try:
            _user32.EndMenu()
        except Exception:  # noqa: BLE001
            pass

    def _destroy(self, hwnd: int) -> None:
        """The private stop path: remove the icon and destroy the window (-> WM_DESTROY -> loop ends)."""
        self._kill_retry_timer()
        self._delete_icon()
        if hwnd and win32gui.IsWindow(hwnd):
            win32gui.DestroyWindow(hwnd)

    def _on_command(self, cmd_id: int) -> None:
        if cmd_id == ID_OPEN:
            self._call("on_open", self.on_open)
        elif cmd_id == ID_TOGGLE:
            with self._lock:
                _led, _tip, label, enabled = self._want
            shown, self._menu_label_shown = self._menu_label_shown, None
            if label is None or not enabled:
                log.info("tray: toggle ignored (item hidden/disabled now)")
            elif shown is not None and shown != label:
                # The state changed while the menu was open ("Turn on" clicked, now it would mean "Turn off").
                log.info("tray: toggle ignored (menu said %r, state now %r)", shown, label)
            else:
                self._call("on_toggle", self.on_toggle)
        elif cmd_id == ID_QUIT:
            self._call("on_quit", self.on_quit)
        elif ID_MODEL_BASE <= cmd_id < ID_MODEL_BASE + MAX_MODELS:
            self._on_model_pick(cmd_id)

    def _on_model_pick(self, cmd_id: int) -> None:
        built, self._menu_model_ids = self._menu_model_ids, None
        with self._lock:
            items, current, enabled = self._models
        latest = {key for _label, key in items}
        if built is None:                                  # no menu built (direct WM_COMMAND): current layout
            built = {cmd_id_: key for cmd_id_, _l, key, _e, _c in self._submenu_spec()[1]}
        key = built.get(cmd_id)
        if key is None:
            log.info("tray: model pick %d ignored (not in the menu that was shown)", cmd_id)
        elif len(items) < 2 or not enabled:
            log.info("tray: model pick %r ignored (submenu hidden/disabled now)", key)
        elif key not in latest:
            log.info("tray: model pick %r ignored (no longer offered)", key)
        elif key == current:
            log.info("tray: model pick %r ignored (already current)", key)
        else:
            self._call("on_switch", self.on_switch, key)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK, NIN_SELECT, NIN_KEYSELECT):
                    self._open_from_click()
                elif event in (win32con.WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                elif event in (NIN_BALLOONSHOW, NIN_BALLOONHIDE, NIN_BALLOONTIMEOUT, NIN_BALLOONUSERCLICK):
                    self._on_balloon_event(event)
                return 0
            if msg == WM_TRAY_UPDATE:
                self._apply()
                return 0
            if msg == WM_TRAY_NOTIFY:
                self._show_balloons()
                return 0
            if msg == win32con.WM_COMMAND:
                self._on_command(wparam & 0xFFFF)
                return 0
            if msg == WM_TIMER and wparam == TIMER_READD:
                self._on_retry_timer()
                return 0
            if msg == WM_TRAY_SYNC:
                self._sync_seen = max(self._sync_seen, int(wparam))
                return 0
            if msg == WM_TRAY_STOP:
                if self._in_menu:
                    # Inside TrackPopupMenu's modal loop: close the menu, destroy once it has returned.
                    self._stop_pending = True
                    self._end_menu()
                    return 0
                self._destroy(hwnd)
                return 0
            if msg == win32con.WM_CLOSE:
                # taskkill without /F, Restart Manager, an installer: ask the app to quit; never self-destroy
                # (DefWindowProc would) - the tray is the only way back to a hidden window.
                log.info("tray: WM_CLOSE received -> on_quit")
                self._call("on_quit", self.on_quit)
                return 0
            if msg == WM_QUERYENDSESSION:
                return 1                                   # never veto logoff / shutdown / Restart Manager
            if msg == WM_ENDSESSION:
                if wparam:                                 # the session (or a Restart Manager close) is happening
                    log.info("tray: WM_ENDSESSION (lParam 0x%x) -> on_quit", lparam & 0xFFFFFFFF)
                    self._call("on_quit", self.on_quit)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                self._on_taskbar_created(hwnd)
                return 0
        except Exception:  # noqa: BLE001
            log.exception("tray: wndproc error (msg 0x%x)", msg)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
