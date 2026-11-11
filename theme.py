"""Design tokens for Hermes Controller — "PC front panel": quiet graphite chrome, one status-LED accent.

Direction: exact, calm, a bit mechanical. The only rich element is the memory gauge; everything else is neutral.
All spacing is on a 4 px grid. Two weights (400 / 600 for the one status line).
"""
from __future__ import annotations

import ctypes
import logging
import tkinter.font as tkfont

# --- graphite ramp (neutral; surfaces never carry hue) -------------------------------------------------------------
BG = "#141415"          # window
SURFACE = "#1C1C1E"     # cards, lists, gauge track
RAISED = "#26262A"      # secondary buttons, hover rows
HAIRLINE = "#2C2C30"    # 1 px separators / borders
TEXT = "#EDEDEB"        # primary text, primary button fill
TEXT_2 = "#A1A1A6"      # secondary text
TEXT_3 = "#6E6E73"      # muted text, idle LED
ON_PRIMARY = "#141415"  # text on the off-white primary button

# --- the one accent: status-LED green; semantic tones toned into the palette ---------------------------------------
ACCENT = "#3CC77E"      # active LED, Strata segment, progress fill
AMBER = "#E0A33A"       # busy LED (starting / stopping / loading)
RED = "#E5484D"         # error LED + error text
RED_TEXT = "#F2A7A9"    # error copy on dark (contrast-safe)
APPS_SEG = "#55555B"    # "other apps" gauge segment

LED = {"IDLE": TEXT_3, "STARTING": AMBER, "STOPPING": AMBER, "LOADING": AMBER, "ACTIVE": ACCENT, "ERROR": RED}

# --- radii (nested: card 12 → control 10; small icon buttons 8) ----------------------------------------------------------------------------
R_CARD = 12
R_CONTROL = 10

GEAR_GLYPH = "\uE713"   # Segoe Fluent Icons "Settings"

_FAMILY_CACHE: dict[str, str] = {}


def _pick(*candidates: str) -> str:
    """First installed family (Tk needs a root to exist); falls back to the last candidate."""
    key = "|".join(candidates)
    if key in _FAMILY_CACHE:
        return _FAMILY_CACHE[key]
    try:
        have = set(tkfont.families())
    except Exception:  # noqa: BLE001 - no root yet
        return candidates[-1]
    choice = next((c for c in candidates if c in have), candidates[-1])
    _FAMILY_CACHE[key] = choice
    return choice


def ui_family() -> str:
    return _pick("Segoe UI Variable Text", "Segoe UI")


def mono_family() -> str:
    return _pick("Cascadia Mono", "Consolas")


def icon_family() -> str:
    return _pick("Segoe Fluent Icons", "Segoe MDL2 Assets")


def fmt_gb(mb: float | None, digits: int = 1) -> str:
    return f"{(mb or 0.0) / 1024.0:.{digits}f} GB"


def dark_caption(win) -> None:
    """Windows 11: paint a window's title bar in the window colour (seamless panel). No-op elsewhere."""
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dark = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)   # immersive dark mode
        color = ctypes.c_int(int(BG[5:7] + BG[3:5] + BG[1:3], 16))                  # COLORREF 0x00BBGGRR
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)  # DWMWA_CAPTION_COLOR
    except Exception:  # noqa: BLE001
        logging.getLogger("hermes_controller").debug("caption colour not applied", exc_info=True)
