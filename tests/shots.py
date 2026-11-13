"""Pictures of test windows, on screen or on a hidden desktop.

On screen (the default): the window is put on top and the screen is grabbed, as always.
Hidden (AGENTSTART_TEST_HIDDEN=1, set by tools/run_hidden.py, which runs a suite on a separate Windows desktop so
nothing is shown and nothing takes the focus): nothing of that desktop is on screen, so Windows is asked to draw
the window into a bitmap instead (PrintWindow). Those pictures get their own names (ui_hidden_...), never the
on-screen picture's name. If that gives nothing usable there is no picture; the checks still run.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

HIDDEN = os.environ.get("AGENTSTART_TEST_HIDDEN") == "1"


def print_window(win):
    """The window as a PIL image drawn by Windows itself, or None."""
    try:
        import ctypes

        import win32gui
        import win32ui
        from PIL import Image
        hwnd = int(win.wm_frame(), 16)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return None
        hdc = win32gui.GetWindowDC(hwnd)
        src = win32ui.CreateDCFromHandle(hdc)
        mem = src.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        try:
            bmp.CreateCompatibleBitmap(src, w, h)
            mem.SelectObject(bmp)
            for flag in (2, 0):                      # PW_RENDERFULLCONTENT first, then the classic WM_PRINT way
                if ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), flag):
                    got = Image.frombuffer("RGB", (w, h), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
                    ext = got.getextrema()
                    bands = ext if ext and isinstance(ext[0], tuple) else (ext,)
                    if any(band[1] > 0 for band in bands):      # not an all-black frame
                        return got.copy()
            return None
        finally:
            win32gui.DeleteObject(bmp.GetHandle())
            mem.DeleteDC()
            src.DeleteDC()
            win32gui.ReleaseDC(hwnd, hdc)
    except Exception:  # noqa: BLE001
        return None


def grab(win, path: Path, opaque: bool = False) -> tuple[Path, int, int]:
    """Save a picture of `win`. Returns (the file written, width, height); on a hidden desktop the file is
    ui_hidden_<name> beside `path`, and (path, 0, 0) with nothing written when no picture could be made.
    opaque: the window was built transparent (alpha 0) and must be made visible for the picture."""
    if opaque:
        win.attributes("-alpha", 1.0)
    if HIDDEN:
        win.update_idletasks()
        win.update()
        time.sleep(0.1)
        win.update()
        img = print_window(win)
        if img is None:
            print(f"screenshot skipped (hidden desktop): {path.name}", flush=True)
            return path, 0, 0
        name = path.name[3:] if path.name.startswith("ui_") else path.name
        out = path.with_name("ui_hidden_" + name)
        img.save(out)
        return out, img.size[0], img.size[1]
    from PIL import ImageGrab
    win.attributes("-topmost", True)
    win.lift()
    win.update_idletasks()
    win.update()
    time.sleep(0.35)
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(path)
    return path, w, h
