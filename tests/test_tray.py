"""Self-check for tray.py. Run: .venv\\Scripts\\python.exe tests\\test_tray.py

Safe by construction:
  * Uses its own window class base ("AgentStartTrayTest_<pid>...") and only ever posts/sends messages to the hidden
    windows this test created (or to the tray threads it started); never touches other windows (the real Agent Start
    tray icon may be running).
  * Never opens a real popup menu (the WM_COMMAND path is driven directly; the menu test fakes TrackPopupMenu).
    No processes, no network. "Explorer restarts" are simulated by posting TaskbarCreated to OUR window only.
  * One balloon notification is shown (title "Agent Start tray test").
  * Negative checks ("callback did NOT fire") use sync(): a sentinel message posted after the stimulus; once the
    tray thread has handled it, everything posted before it has been handled too.
"""
from __future__ import annotations

import ctypes
import itertools
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:  # the app runs per-monitor DPI aware (CustomTkinter); mimic it before any window exists
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    pass

import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

import tray  # noqa: E402
from tray import TrayIcon  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
ICON_PNG = str(ROOT / "assets" / "app.png")
CLASS = f"AgentStartTrayTest_{os.getpid()}"
GREEN, AMBER, RED, BLUE, GREY = "#3CC77E", "#E0A33A", "#E5484D", "#4DB6F5", "#6E6E73"
SHOT = Path(tempfile.gettempdir()) / "agentstart_tray_area.png"   # outside the repo
_SYNC = itertools.count(1)


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def errors(self, *allowed: str) -> list[str]:
        out = []
        for r in self.records:
            msg = r.getMessage()
            if r.levelno >= logging.ERROR and not any(a in msg for a in allowed):
                out.append(msg)
        return out


LOG = LogCapture()
_app_log = logging.getLogger("agent_start")
_app_log.addHandler(LOG)
_app_log.setLevel(logging.DEBUG)


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def wait_until(pred, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def sync(t: TrayIcon, timeout: float = 2.0) -> bool:
    """Post a sentinel to t's window and wait until the tray thread handled it (=> all earlier posts were handled)."""
    n = next(_SYNC)
    hwnd = t._hwnd
    if not hwnd:
        return False
    try:
        win32gui.PostMessage(hwnd, tray.WM_TRAY_SYNC, n, 0)
    except Exception:  # noqa: BLE001
        return False
    return wait_until(lambda: t._sync_seen >= n, timeout)


def class_registered(name: str) -> bool:
    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                    ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
                    ("hIconSm", ctypes.c_void_p)]
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    fn = ctypes.windll.user32.GetClassInfoExW
    fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(WNDCLASSEXW)]
    fn.restype = ctypes.c_int
    return bool(fn(ctypes.c_void_p(win32api.GetModuleHandle(None)), name, ctypes.byref(wc)))


def tray_threads() -> list[str]:
    return [th.name for th in threading.enumerate() if th.name in ("tray", "tray-start") and th.is_alive()]


def rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def close(a, b, tol: int = 6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def icon_alive(hwnd: int) -> bool:
    """NIM_MODIFY with no flags succeeds only while (hwnd, uID) is registered with the shell."""
    try:
        win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (hwnd, tray.ICON_UID, 0, 0, 0, ""))
        return True
    except Exception:  # noqa: BLE001
        return False


def gui_counts() -> tuple[int, int]:
    """(GDI, USER) object counts of this process. GetGuiResources rejects the pseudo handle -> real handle."""
    import win32process
    h = win32api.OpenProcess(0x1000, False, os.getpid())    # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        return win32process.GetGuiResources(h, 0), win32process.GetGuiResources(h, 1)
    finally:
        win32api.CloseHandle(h)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def cb(self, name: str):  # noqa: D401
        return lambda: self.calls.append((name, threading.current_thread().name))

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


def make(rec: Recorder, **kw) -> TrayIcon:
    return TrayIcon("Agent Start", ICON_PNG, rec.cb("open"), rec.cb("toggle"), rec.cb("quit"),
                    class_name=kw.pop("class_name", CLASS), on_lost=rec.cb("lost"))


def applied(t: TrayIcon) -> bool:
    return t._applied == t._want


# ------------------------------------------------------------------------------------------------------------ tests
def test_render() -> None:
    section("render")
    t = make(Recorder())
    size = tray.small_icon_size()
    dpi = ctypes.windll.user32.GetDpiForSystem()
    check(f"small icon size sane ({size}px @ {dpi} dpi)", 16 <= size <= 64 and size >= round(16 * dpi / 96) - 1)
    x0, y0, d = tray.dot_geometry(size)
    cx, cy = x0 + d // 2, y0 + d // 2
    base = t._render(None, size)
    check("render(None) is RGBA at the small-icon size", base.size == (size, size) and base.mode == "RGBA")
    imgs = {}
    for col in (GREEN, AMBER, RED, BLUE, GREY):
        img = t._render(col, size)
        imgs[col] = img
        px = img.getpixel((cx, cy))
        check(f"dot centre is {col}", close(px[:3], rgb(col)) and px[3] == 255, f"got {px}")
    right, bottom = imgs[GREEN].getpixel((size - 1, cy)), imgs[GREEN].getpixel((cx, size - 1))
    check("dot is flush with the bottom-right corner (outline on the right/bottom edge)",
          max(right[:3]) < 90 and max(bottom[:3]) < 90 and right[3] > 150 and bottom[3] > 150,
          f"right={right} bottom={bottom}")
    check("dot sits in the bottom-right quadrant", cx >= size // 2 and cy >= size // 2)
    edge = imgs[GREEN].getpixel((x0, cy))
    check("dot has a dark outline on its left edge", max(edge[:3]) < 90 and edge[3] > 150, f"got {edge}")
    check("no dot for None (dot area == plain icon)", base.getpixel((cx, cy)) != imgs[GREEN].getpixel((cx, cy))
          and base.tobytes() == t._render(None, size).tobytes())
    check("different colours -> different images", len({im.tobytes() for im in imgs.values()}) == len(imgs))
    check("top-left of icon untouched by the dot", base.getpixel((1, 1)) == imgs[RED].getpixel((1, 1)))
    check("bad colour -> no dot, no raise", t._render("not-a-colour", size).tobytes() == base.tobytes())
    for s in (16, 20, 24, 32):
        img = t._render(AMBER, s)
        hi = tray.hicon_from_image(img)
        ok = bool(hi)
        try:
            info = win32gui.GetIconInfo(hi)
            for bm in info[3:5]:
                if bm:
                    win32gui.DeleteObject(bm)
        except Exception:  # noqa: BLE001
            ok = False
        tray._user32.DestroyIcon(hi)
        check(f"HICON created at {s}px", ok)
    missing = TrayIcon("X", str(ROOT / "assets" / "nope.png"), None, None, None, class_name=CLASS + "_m")
    check("missing PNG -> fallback tile, no raise", missing._render(GREEN, 16).size == (16, 16))


def test_menu_model() -> None:
    section("menu model")
    t = make(Recorder())
    items = t._menu_items()
    check("default menu: Open(default) / - / Quit",
          items == [("Open Agent Start", True, True), ("-", False, False), ("Quit", True, False)], repr(items))
    t.set_status(GREEN, "Agent Start - active", "Turn off", True)
    items = t._menu_items()
    check("toggle 'Turn off' enabled shown 2nd", items[1] == ("Turn off", True, False), repr(items))
    check("only 'Open' is default", [i[0] for i in items if i[2]] == ["Open Agent Start"])
    t.set_status(AMBER, "starting", "Cancel", False)
    check("toggle 'Cancel' greyed", t._menu_items()[1] == ("Cancel", False, False), repr(t._menu_items()))
    t.set_status(GREY, "idle", None)
    check("toggle hidden when label None", [i[0] for i in t._menu_items()] == ["Open Agent Start", "-", "Quit"])
    t.set_status(GREY, "x" * 300, "Turn on")
    check("tooltip truncated to 127", len(t._want[1]) == 127)


def test_lifecycle_and_callbacks() -> None:
    section("start / status / callbacks")
    rec = Recorder()
    t = make(rec)
    t.stop()
    check("stop() before start is a no-op", not t.is_running)
    t.set_status(AMBER, "Agent Start - starting", "Cancel", True)     # before start: applied on start
    t0 = time.monotonic()
    ok = t.start()
    check("start() returns True", ok, f"took {time.monotonic() - t0:.2f}s")
    check("is_running", t.is_running)
    try:
        hwnd = t._hwnd
        check("hidden window of our (per-run unique) class", bool(hwnd)
              and win32gui.GetClassName(hwnd).startswith(CLASS + "_") and not win32gui.IsWindowVisible(hwnd),
              win32gui.GetClassName(hwnd) if hwnd else "")
        check("tray thread is a daemon named 'tray'", t._thread is not None and t._thread.daemon
              and t._thread.name == "tray")
        check("Shell_NotifyIcon add verified (NIM_MODIFY ok on our uID)", icon_alive(hwnd))
        check("status set before start applied", wait_until(lambda: applied(t)) and t._applied[0] == AMBER)
        check("start() again while running -> True (no second icon)", t.start() and t._hwnd == hwnd)

        # set_status from another thread with several colours / tooltips
        seen_hicons = set()
        errors = []

        def worker():
            try:
                for col, tip, lab, en in ((GREEN, "Agent Start - active", "Turn off", True),
                                          (RED, "Agent Start - error: boom", "Turn on", True),
                                          (BLUE, "Agent Start - reading", "Turn off", True),
                                          (None, "Agent Start", None, True)):
                    t.set_status(col, tip, lab, en)
                    if not wait_until(lambda: applied(t)):
                        errors.append(f"not applied: {col}")
                    seen_hicons.add(t._hicon)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        th = threading.Thread(target=worker)
        th.start()
        th.join(10)
        check("set_status from a worker thread applies every state", not errors, "; ".join(errors))
        check("each colour change made a new HICON", len(seen_hicons) == 4, f"{len(seen_hicons)} distinct")
        check("icon still registered after updates", icon_alive(hwnd))
        h_before = t._hicon
        t.set_status(None, "Agent Start - other tip", None)
        check("tooltip-only change keeps the HICON", wait_until(lambda: applied(t)) and t._hicon == h_before)
        posted = []
        orig_post = t._post
        t._post = lambda *a: posted.append(a) or orig_post(*a)
        t.set_status(None, "Agent Start - other tip", None)
        t._post = orig_post
        check("identical set_status is a no-op (nothing posted)", not posted)
        t.set_status(GREEN, "x" * 400, "Turn off")
        check("long tooltip accepted by the shell", wait_until(lambda: applied(t)) and icon_alive(hwnd))

        # clicks: WM_LBUTTONUP via the tray callback message
        win32gui.PostMessage(hwnd, tray.WM_TRAY, tray.ICON_UID, win32con.WM_LBUTTONUP)
        check("left click -> on_open", wait_until(lambda: rec.count("open") == 1), repr(rec.calls))
        check("on_open ran on the tray thread", rec.calls and rec.calls[-1][1] == "tray")
        win32gui.PostMessage(hwnd, tray.WM_TRAY, tray.ICON_UID, win32con.WM_LBUTTONDBLCLK)
        win32gui.PostMessage(hwnd, tray.WM_TRAY, tray.ICON_UID, win32con.WM_LBUTTONUP)
        sync(t)
        check("double-click right after -> folded into the first open", rec.count("open") == 1, repr(rec.calls))
        t.open_debounce_s = 0
        win32gui.PostMessage(hwnd, tray.WM_TRAY, tray.ICON_UID, tray.NIN_SELECT)
        check("NIN_SELECT -> on_open", wait_until(lambda: rec.count("open") == 2))
        win32gui.PostMessage(hwnd, tray.WM_TRAY, tray.ICON_UID, win32con.WM_MOUSEMOVE)
        check("mouse move -> nothing", sync(t) and len(rec.calls) == 2)

        # WM_COMMAND path (menu items) - no real popup
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_QUIT, 0)
        check("Quit command -> on_quit", wait_until(lambda: rec.count("quit") == 1))
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_OPEN, 0)
        check("Open command -> on_open", wait_until(lambda: rec.count("open") == 3))
        t.set_status(GREEN, "active", "Turn off", True)
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_TOGGLE, 0)
        check("Toggle command (enabled) -> on_toggle", wait_until(lambda: rec.count("toggle") == 1))
        t.set_status(AMBER, "stopping", "Cancel", False)
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_TOGGLE, 0)
        check("Toggle command while disabled -> ignored", sync(t) and rec.count("toggle") == 1)
        t.set_status(AMBER, "starting", "Cancel", True)
        wait_until(lambda: applied(t))
        t._menu_label_shown = "Turn on"            # menu showed 'Turn on', state moved on before the click
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_TOGGLE, 0)
        check("stale toggle (label changed under the open menu) -> ignored", sync(t) and rec.count("toggle") == 1)
        t.set_status(GREY, "idle", None)
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_TOGGLE, 0)
        check("Toggle command while hidden -> ignored", sync(t) and rec.count("toggle") == 1)
        all_on_tray = all(th_name == "tray" for _, th_name in rec.calls)
        check("all callbacks ran on the tray thread", all_on_tray, repr(rec.calls))

        # a raising callback must not kill the loop
        t.on_quit = lambda: 1 / 0
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_QUIT, 0)
        sync(t)
        t.on_quit = rec.cb("quit")
        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_QUIT, 0)
        check("raising callback does not kill the tray thread", wait_until(lambda: rec.count("quit") == 2)
              and t.is_running)

        # Explorer restart
        win32gui.PostMessage(hwnd, t._taskbar_created, 0, 0)
        check("TaskbarCreated -> icon re-added", sync(t) and t.is_running and t._added and icon_alive(hwnd))
        check("no on_lost for a clean re-add", rec.count("lost") == 0)

        t.notify("Agent Start tray test", "Balloon from tests/test_tray.py")
        check("notify() is best-effort and harmless", sync(t) and t.is_running and icon_alive(hwnd)
              and t._balloon is None)

        # GDI / USER handle leak check
        for i in range(10):
            t.set_status((GREEN, RED)[i % 2], f"warm {i}", "Turn off")
            wait_until(lambda: applied(t))
        gdi0, user0 = gui_counts()
        cols = (GREEN, AMBER, RED, BLUE, GREY, None)
        for i in range(200):                       # rapid burst from this thread (coalesced)
            t.set_status(cols[i % 6], f"Agent Start - burst {i}", ("Turn on", "Turn off", None)[i % 3], i % 2 == 0)
        check("burst of 200 converges to the last state", wait_until(lambda: applied(t), 3.0)
              and t._applied[1] == "Agent Start - burst 199")
        for i in range(200):                       # 200 renders, each applied
            t.set_status(cols[i % 6], f"Agent Start - step {i}", "Turn off")
            if not wait_until(lambda: applied(t), 2.0):
                break
        check("200 sequential renders all applied", applied(t) and t._applied[1] == "Agent Start - step 199")
        gdi1, user1 = gui_counts()
        check("GUI resource counts readable", gdi0 > 0 and user0 > 0, f"gdi={gdi0} user={user0}")
        check(f"no GDI leak over 400 set_status (GDI {gdi0}->{gdi1})", gdi1 - gdi0 <= 6)
        check(f"no USER leak over 400 set_status (USER {user0}->{user1})", user1 - user0 <= 6)
        leaked = [tray.hicon_from_image(t._render(GREEN)) for _ in range(20)]
        gdi2, user2 = gui_counts()
        for hi in leaked:
            tray._user32.DestroyIcon(hi)
        gdi3, _ = gui_counts()
        check(f"leak probe is sensitive (20 undestroyed HICONs: GDI {gdi1}->{gdi2}, freed ->{gdi3})",
              gdi2 - gdi1 >= 20 and gdi3 <= gdi1 + 2)

        try:
            t.set_status(GREEN, "Agent Start - tray test (screenshot)", "Turn off")
            wait_until(lambda: applied(t))
            time.sleep(0.6)
            from PIL import ImageGrab
            w, h = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
            ImageGrab.grab(bbox=(max(0, w - 700), max(0, h - 90), w, h), all_screens=False).save(SHOT)
            print(f"  (screenshot of the notification area: {SHOT})")
        except Exception as e:  # noqa: BLE001
            print(f"  (screenshot skipped: {e})")
    finally:
        thread = t._thread
        t.stop()
    check("stop(): is_running False", not t.is_running)
    check("stop(): thread ended", thread is not None and not thread.is_alive())
    check("stop(): icon removed from the shell", not icon_alive(hwnd))
    check("stop(): window destroyed", not win32gui.IsWindow(hwnd))
    check("stop(): HICON released", t._hicon == 0)
    check("stop(): no on_lost", rec.count("lost") == 0)
    t0 = time.monotonic()
    t.stop()
    check("stop() twice is a no-op", time.monotonic() - t0 < 0.1 and not t.is_running)
    t.set_status(RED, "after stop", "Turn on")
    t.notify("x", "y")
    check("set_status/notify after stop do not raise", True)

    # start / stop again on the same object (class re-registration)
    ok2 = t.start()
    hwnd2 = t._hwnd
    check("second start() on the same object -> True", ok2 and t.is_running and icon_alive(hwnd2))
    check("second run shows the latest status", wait_until(lambda: applied(t)) and t._applied[0] == RED)
    win32gui.PostMessage(hwnd2, tray.WM_TRAY, tray.ICON_UID, win32con.WM_LBUTTONUP)
    check("callbacks work in the second run", wait_until(lambda: rec.count("open") == 4))
    t.stop()
    check("second stop(): removed + thread ended", not t.is_running and not icon_alive(hwnd2)
          and t._thread is None)

    # a fresh object reusing the class name
    t3 = make(Recorder())
    check("new object, same class name: start True", t3.start() and t3.is_running)
    t3.stop()
    check("new object stop: removed", not t3.is_running and not icon_alive(hwnd2))


def test_start_failure() -> None:
    section("start failure")
    orig_timeout = tray.START_TIMEOUT_S
    tray.START_TIMEOUT_S = 1.0
    try:
        frec = Recorder()
        t = make(frec, class_name=CLASS + "_fail")
        t._add_icon = lambda: False               # shell refuses NIM_ADD
        t0 = time.monotonic()
        ok = t.start()
        took = time.monotonic() - t0
        check("start() returns False when the icon cannot be added (no raise)", ok is False, f"ok={ok}")
        check("failed start returns within the timeout", took < 3.5, f"{took:.2f}s")
        check("failed start: not running, thread gone", not t.is_running and t._thread is None)
        t.stop()
        check("stop() after failed start is safe", not t.is_running)
        t.set_status(GREEN, "x", "Turn on")
        check("set_status after failed start does not raise", True)
        check("failed start never calls on_lost (start() returned False instead)", frec.count("lost") == 0)
    finally:
        tray.START_TIMEOUT_S = orig_timeout


def test_stop_from_callback() -> None:
    section("stop from inside a callback")
    t = make(Recorder(), class_name=CLASS + "_cb")
    done = threading.Event()

    def quit_cb():
        t.stop()                                   # must not self-join / deadlock
        done.set()

    t.on_quit = quit_cb
    check("start", t.start())
    hwnd = t._hwnd
    thread = t._thread
    win32gui.PostMessage(hwnd, win32con.WM_COMMAND, tray.ID_QUIT, 0)
    check("stop() inside on_quit returns", done.wait(2))
    check("loop ends after the callback", wait_until(lambda: not thread.is_alive(), 2.0))
    check("icon removed", not icon_alive(hwnd))
    t.stop()
    check("outer stop() afterwards cleans up", t._thread is None and not t.is_running)


def test_readd_retry() -> None:
    section("Explorer restart: NIM_ADD retries, on_lost (#10)")
    saved = (tray.READD_INTERVAL_S, tray.READD_TIMEOUT_S, tray.READD_SLOW_INTERVAL_S)
    tray.READD_INTERVAL_S, tray.READD_TIMEOUT_S, tray.READD_SLOW_INTERVAL_S = 0.05, 0.6, 0.1
    rec = Recorder()
    t = make(rec, class_name=CLASS + "_readd")
    shown: list[tuple[str, str]] = []
    t._show_balloon = lambda title, msg: shown.append((title, msg))
    hwnd = 0
    try:
        check("start", t.start())
        hwnd = t._hwnd
        attempts: list[float] = []

        def flaky() -> bool:                      # Explorer still starting: NIM_ADD fails 3 times
            attempts.append(time.monotonic())
            return False if len(attempts) <= 3 else TrayIcon._add_icon(t)

        t._add_icon = flaky
        win32gui.PostMessage(hwnd, t._taskbar_created, 0, 0)
        check("TaskbarCreated + 3 failed NIM_ADDs -> re-added by the retry timer",
              wait_until(lambda: t.is_running and len(attempts) >= 4, 3.0) and icon_alive(hwnd),
              f"attempts={len(attempts)} running={t.is_running}")
        check("retries were spaced by the timer (not a busy loop)",
              len(attempts) >= 4 and attempts[3] - attempts[0] >= 0.1,
              f"{attempts[3] - attempts[0]:.3f}s" if len(attempts) >= 4 else "")
        check("timer stopped after success; no on_lost", sync(t) and len(attempts) == 4 and rec.count("lost") == 0,
              f"attempts={len(attempts)} lost={rec.count('lost')}")

        fails: list[int] = []
        t._add_icon = lambda: fails.append(1) or False      # Explorer never accepts the icon
        win32gui.PostMessage(hwnd, t._taskbar_created, 0, 0)
        check("NIM_ADD failing for READD_TIMEOUT_S -> on_lost", wait_until(lambda: rec.count("lost") == 1, 3.0))
        check("on_lost ran on the tray thread", [th for n, th in rec.calls if n == "lost"] == ["tray"])
        check("tried repeatedly before giving up", len(fails) >= 5, f"{len(fails)} attempts")
        check("after on_lost: not running, but thread + window kept",
              not t.is_running and t._thread is not None and t._thread.is_alive() and win32gui.IsWindow(hwnd))
        t.notify("held", "while the icon is missing")
        n0 = len(fails)
        check("keeps retrying slowly after on_lost; on_lost not repeated",
              wait_until(lambda: len(fails) >= n0 + 2, 3.0) and sync(t) and rec.count("lost") == 1,
              f"fails {n0}->{len(fails)} lost={rec.count('lost')}")
        check("balloon held while the icon is missing", shown == [], repr(shown))
        t.set_status(RED, "Agent Start - status while lost", "Turn on")
        del t._add_icon                                      # Explorer finally accepts the icon
        check("slow retry brings the icon back", wait_until(lambda: t.is_running, 3.0) and icon_alive(hwnd))
        check("held balloon shown after the re-add (#28)",
              wait_until(lambda: shown == [("held", "while the icon is missing")]), repr(shown))
        check("restored icon has the latest status", wait_until(lambda: applied(t)) and t._applied[0] == RED)
        n1 = len(fails)
        check("no more retries once back", sync(t) and len(fails) == n1)
    finally:
        tray.READD_INTERVAL_S, tray.READD_TIMEOUT_S, tray.READD_SLOW_INTERVAL_S = saved
        t.stop()
    check("stop() afterwards: icon removed, on_lost still 1", not icon_alive(hwnd) and rec.count("lost") == 1)

    # the tray thread ends without stop()
    rec2 = Recorder()
    t2 = make(rec2, class_name=CLASS + "_die")
    check("start (thread-death case)", t2.start())
    hwnd2, th2 = t2._hwnd, t2._thread
    win32api.PostThreadMessage(th2.native_id, win32con.WM_QUIT, 0, 0)     # our own tray thread only
    check("tray thread ending without stop() -> on_lost", wait_until(lambda: rec2.count("lost") == 1)
          and wait_until(lambda: not th2.is_alive()))
    check("... on the tray thread", [th for n, th in rec2.calls if n == "lost"] == ["tray"])
    check("... icon removed, window gone, not running",
          not icon_alive(hwnd2) and not win32gui.IsWindow(hwnd2) and not t2.is_running)
    t2.stop()
    check("stop() afterwards: no second on_lost, cleaned up", rec2.count("lost") == 1 and t2._thread is None)


def test_close_requests() -> None:
    section("WM_CLOSE / session end (#11)")
    rec = Recorder()
    t = make(rec, class_name=CLASS + "_close")
    check("start", t.start())
    hwnd, th = t._hwnd, t._thread
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        check("WM_CLOSE -> on_quit (on the tray thread)", wait_until(lambda: rec.count("quit") == 1)
              and rec.calls[-1][1] == "tray")
        check("WM_CLOSE keeps window, thread and icon", sync(t) and win32gui.IsWindow(hwnd) and th.is_alive()
              and t.is_running and icon_alive(hwnd))
        win32gui.PostMessage(hwnd, win32con.WM_SYSCOMMAND, win32con.SC_CLOSE, 0)
        check("SC_CLOSE (-> WM_CLOSE) -> on_quit, still alive", wait_until(lambda: rec.count("quit") == 2)
              and sync(t) and t.is_running)
        r = win32gui.SendMessage(hwnd, tray.WM_QUERYENDSESSION, 0, 1)     # ENDSESSION_CLOSEAPP
        check("WM_QUERYENDSESSION -> TRUE (never vetoes)", r == 1, f"returned {r}")
        check("WM_QUERYENDSESSION alone -> no on_quit", sync(t) and rec.count("quit") == 2)
        win32gui.PostMessage(hwnd, tray.WM_ENDSESSION, 0, 0)               # session end cancelled
        check("WM_ENDSESSION(FALSE) -> nothing", sync(t) and rec.count("quit") == 2)
        win32gui.PostMessage(hwnd, tray.WM_ENDSESSION, 1, 1)               # Restart Manager close
        check("WM_ENDSESSION(TRUE) -> on_quit", wait_until(lambda: rec.count("quit") == 3))
        check("still alive until stop()", sync(t) and th.is_alive() and win32gui.IsWindow(hwnd)
              and icon_alive(hwnd) and rec.count("lost") == 0)
    finally:
        t.stop()
    check("stop() ends it; no on_lost", not th.is_alive() and not win32gui.IsWindow(hwnd) and not icon_alive(hwnd)
          and rec.count("lost") == 0)


def test_post_failure() -> None:
    section("failed PostMessage does not wedge set_status (#22)")
    rec = Recorder()
    t = make(rec, class_name=CLASS + "_post")
    check("start", t.start())
    try:
        t._post = lambda *a, **k: False                   # e.g. message queue full
        t.set_status(RED, "Agent Start - post fails", "Turn on")
        check("failed post clears _update_posted", not t._update_posted)
        del t._post
        t.set_status(GREEN, "Agent Start - post works again", "Turn off")
        check("next set_status is delivered and applied", wait_until(lambda: applied(t))
              and t._applied[1] == "Agent Start - post works again")
    finally:
        t.stop()


def test_unique_classes_and_restart_guard() -> None:
    section("per-instance window classes, start() after a timed-out stop (#23)")
    ra, rb = Recorder(), Recorder()
    a = make(ra, class_name=CLASS + "_dup")
    b = make(rb, class_name=CLASS + "_dup")
    ca = cb = ""
    try:
        check("two instances with the same base class name both start", a.start() and b.start())
        ca, cb = win32gui.GetClassName(a._hwnd), win32gui.GetClassName(b._hwnd)
        check("distinct registered classes sharing the base name", ca != cb and ca.startswith(CLASS + "_dup_")
              and cb.startswith(CLASS + "_dup_"), f"{ca} / {cb}")
        win32gui.PostMessage(b._hwnd, tray.WM_TRAY, tray.ICON_UID, win32con.WM_LBUTTONUP)
        check("click on B -> B.on_open", wait_until(lambda: rb.count("open") == 1))
        check("... and not A.on_open", sync(a) and ra.count("open") == 0)
        win32gui.PostMessage(a._hwnd, tray.WM_TRAY, tray.ICON_UID, win32con.WM_LBUTTONUP)
        check("click on A -> A.on_open only", wait_until(lambda: ra.count("open") == 1) and sync(b)
              and rb.count("open") == 1)
    finally:
        a.stop()
        b.stop()
    check("classes unregistered on stop", bool(ca) and not class_registered(ca) and not class_registered(cb))

    rec = Recorder()
    t = make(rec, class_name=CLASS + "_stuck")
    gate, entered = threading.Event(), threading.Event()

    def stuck_open() -> None:
        entered.set()
        gate.wait(5)

    t.on_open = stuck_open
    saved = tray.JOIN_TIMEOUT_S
    th = None
    try:
        check("start", t.start())
        th = t._thread
        win32gui.PostMessage(t._hwnd, tray.WM_TRAY, tray.ICON_UID, win32con.WM_LBUTTONUP)
        check("callback blocking the tray thread", entered.wait(2))
        tray.JOIN_TIMEOUT_S = 0.2
        t.stop()
        check("stop() join timed out: old thread still alive", th.is_alive() and t._thread is th)
        before = tray_threads().count("tray")
        t0 = time.monotonic()
        ok = t.start()
        check("start() refused while the old thread is alive", ok is False and t._thread is th
              and time.monotonic() - t0 < 0.5 and tray_threads().count("tray") == before)
        gate.set()
        check("old thread ends once the callback returns", wait_until(lambda: not th.is_alive()))
        t.on_open = rec.cb("open")
        ok = t.start()
        check("start() works again afterwards (new thread)", ok and t._thread is not th and t.is_running)
    finally:
        gate.set()
        tray.JOIN_TIMEOUT_S = saved
        t.stop()
    check("stopped", not t.is_running and t._thread is None)


def test_logger() -> None:
    section("logger (#24)")
    check("tray logs to the app logger 'agent_start'", tray.log is logging.getLogger("agent_start"))
    n = len(LOG.records)
    tray._norm_hex("not-a-colour")
    new = LOG.records[n:]
    check("tray warnings reach 'agent_start' handlers", any(r.levelno == logging.WARNING and r.name == "agent_start"
                                                             and "bad LED colour" in r.getMessage() for r in new))


def test_start_async() -> None:
    section("start_async (#25)")
    rec = Recorder()
    t = make(rec, class_name=CLASS + "_async")
    results: list[tuple[bool, str]] = []
    done = threading.Event()

    def on_result(ok: bool) -> None:
        results.append((ok, threading.current_thread().name))
        done.set()

    try:
        t0 = time.monotonic()
        t.start_async(on_result)
        took = time.monotonic() - t0
        check("start_async returns immediately", took < 0.1, f"{took:.3f}s")
        check("on_result(True) on the 'tray-start' helper thread", done.wait(5) and results == [(True, "tray-start")],
              repr(results))
        check("icon running after start_async", t.is_running and icon_alive(t._hwnd))
        done.clear()
        t.start_async(on_result)
        check("start_async while running -> True, same thread", done.wait(5) and results[-1][0] is True
              and len(tray_threads()) >= 1)
    finally:
        t.stop()
    check("stop after start_async", not t.is_running and t._thread is None)

    orig_timeout = tray.START_TIMEOUT_S
    tray.START_TIMEOUT_S = 1.0
    try:
        f = make(Recorder(), class_name=CLASS + "_asyncfail")
        f._add_icon = lambda: False
        res: list[bool] = []
        ev = threading.Event()
        f.start_async(lambda ok: (res.append(ok), ev.set()))
        check("failing start_async -> on_result(False)", ev.wait(5) and res == [False], repr(res))
        check("... and nothing left running", wait_until(lambda: f._thread is None) and not f.is_running)
    finally:
        tray.START_TIMEOUT_S = orig_timeout

    c = make(Recorder(), class_name=CLASS + "_asynccancel")
    gate = threading.Event()
    real_start = c._start
    c._start = lambda gen: (gate.wait(2), real_start(gen))[1]     # hold the helper before it starts anything
    res2: list[bool] = []
    ev2 = threading.Event()
    c.start_async(lambda ok: (res2.append(ok), ev2.set()))
    c.stop()                                                      # app closing before the tray got going
    gate.set()
    check("stop() before the helper ran cancels start_async -> on_result(False)", ev2.wait(3) and res2 == [False],
          repr(res2))
    check("... no tray thread created", c._thread is None and not c.is_running)


def test_stop_with_menu_open() -> None:
    section("stop() while the popup menu is open (#26, fake menu)")
    rec = Recorder()
    t = make(rec, class_name=CLASS + "_menu")
    entered = threading.Event()
    ended: list[str] = []

    def fake_track(menu, hwnd) -> None:            # stands in for TrackPopupMenu's modal loop; no real menu
        entered.set()
        end = time.monotonic() + 3.0
        while not ended and time.monotonic() < end:
            win32gui.PumpWaitingMessages()         # the modal loop dispatches posted messages, like WM_TRAY_STOP
            time.sleep(0.005)

    t._track_popup = fake_track
    t._end_menu = lambda: ended.append(threading.current_thread().name)
    n_err = len(LOG.errors())
    check("start", t.start())
    hwnd, th = t._hwnd, t._thread
    try:
        win32gui.PostMessage(hwnd, tray.WM_TRAY, tray.ICON_UID, win32con.WM_RBUTTONUP)
        check("menu 'open' (fake TrackPopupMenu running on the tray thread)", entered.wait(2) and t._in_menu)
        t0 = time.monotonic()
        t.stop()
        took = time.monotonic() - t0
    finally:
        t.stop()
    check("stop path called EndMenu on the tray thread", ended == ["tray"], repr(ended))
    check("thread ended within the join", not th.is_alive() and took < tray.JOIN_TIMEOUT_S, f"{took:.2f}s")
    check("icon removed, window destroyed after the menu returned", not icon_alive(hwnd) and not win32gui.IsWindow(hwnd))
    errs = LOG.errors()[n_err:]
    check("no 'wndproc error' / exceptions logged", not errs, repr(errs))
    check("no on_lost, menu flag cleared", rec.count("lost") == 0 and not t._in_menu and not t._stop_pending)


def test_notify_before_start() -> None:
    section("notify() before the icon exists (#28)")
    rec = Recorder()
    t = make(rec, class_name=CLASS + "_notify")
    shown: list[tuple[str, str]] = []
    t._show_balloon = lambda title, msg: shown.append((title, msg))
    t.notify("a", "first")
    t.notify("b", "second")
    t.notify("c", "latest")
    try:
        check("start", t.start())
        check("queued balloon shown after the icon was added (latest only)",
              wait_until(lambda: bool(shown)) and sync(t) and shown == [("c", "latest")], repr(shown))
        t.notify("d", "later")
        check("later notify() shown once", wait_until(lambda: len(shown) == 2) and sync(t)
              and shown[-1] == ("d", "later") and len(shown) == 2, repr(shown))
    finally:
        t.stop()


def main() -> int:
    for fn in (test_render, test_menu_model, test_lifecycle_and_callbacks, test_start_failure,
               test_stop_from_callback, test_readd_retry, test_close_requests, test_post_failure,
               test_unique_classes_and_restart_guard, test_logger, test_start_async, test_stop_with_menu_open,
               test_notify_before_start):
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(f"{fn.__name__} raised", False, "exception")
    alive = tray_threads()
    check("no tray / tray-start threads left behind", wait_until(lambda: not tray_threads()), repr(alive))
    errs = LOG.errors("callback raised")              # the deliberately raising on_quit is expected
    check("no unexpected ERROR log records", not errs, repr(errs[:5]))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
