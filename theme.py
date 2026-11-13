"""Design tokens for Agent Start — "PC front panel": quiet graphite chrome, one status-LED accent.

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
TEXT_3 = "#6E6E73"      # muted marks: idle LED, placeholders, disabled text (3.6:1 on BG: not for small text
                        # that has to be read)
TEXT_MUTED = "#8E8E93"  # muted text that is read: captions, notes, dashes in tables (5.6:1 on BG)
ON_PRIMARY = "#141415"  # text on the off-white primary button
PRIMARY_HOVER = "#D9D9D6"  # hover of the off-white primary button / ticked checkbox
RAISED_HOVER = "#303035"   # hover of RAISED controls, outline of ghost buttons
DANGER = "#B4363B"         # destructive action (sweep dialog), toned into the palette
DANGER_HOVER = "#9C2F33"

# --- the one accent: status-LED green; semantic tones toned into the palette ---------------------------------------
ACCENT = "#3CC77E"      # active LED, Strata segment, progress fill
AMBER = "#E0A33A"       # busy LED (starting / stopping / loading)
BLUE = "#4DB6F5"        # model reading a prompt (LED + activity bar), as Strata's Monitor
RED = "#E5484D"         # error LED + error text
RED_TEXT = "#F2A7A9"    # error copy on dark (contrast-safe)
APPS_SEG = "#55555B"    # "other apps" gauge segment

LED = {"IDLE": TEXT_3, "STARTING": AMBER, "STOPPING": AMBER, "LOADING": AMBER, "ACTIVE": ACCENT, "ERROR": RED}

# --- radii (nested: card 12 → control 10; small icon buttons 8) ----------------------------------------------------------------------------
R_CARD = 12
R_CONTROL = 10

GEAR_GLYPH = "\uE713"   # Segoe Fluent Icons "Settings"
CHEVRON_DOWN = "\uE70D"  # stats section collapsed
CHEVRON_UP = "\uE70E"    # stats section expanded

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


def semibold() -> tuple[str, str]:
    """(family, weight) for the 600 weight: Windows' own Semibold family when it is installed (Tk only knows
    normal and bold, so the weight comes from the family), else the UI family in bold."""
    family = _pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold", "")
    return (family, "normal") if family else (ui_family(), "bold")


def mono_family() -> str:
    return _pick("Cascadia Mono", "Consolas")


def icon_family() -> str:
    return _pick("Segoe Fluent Icons", "Segoe MDL2 Assets")


def mix(fg: str, bg: str, alpha: float) -> str:
    """`fg` at `alpha` opacity over `bg` as a solid #RRGGBB.

    tk.Canvas has no alpha, so graph area fills are pre-blended tints of existing tokens (no new palette colours).
    """
    a = min(1.0, max(0.0, float(alpha)))
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(fc * a + bc * (1 - a)):02X}" for fc, bc in zip(f, b))


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
        logging.getLogger("agent_start").debug("caption colour not applied", exc_info=True)
