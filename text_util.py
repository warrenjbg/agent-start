"""Small text helpers shared by the app window and its controllers (moved out of main.py unchanged)."""
from __future__ import annotations


def gb(mb: float | None, digits: int = 1) -> str:
    return f"{(mb or 0.0) / 1024.0:.{digits}f} GB"


def tail(path: str, n: int) -> str:
    """The end of a path, which is the informative part ("…\\Strata-main\\Strata-0.2.10")."""
    path = str(path or "")
    return path if len(path) <= n else "…" + path[-(n - 1):]


def short(name: str, limit: int = 22) -> str:
    """Keep the preview to two lines: long exe names (EOSOverlayRenderer-Win64-Shipping.exe) get an ellipsis."""
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name if len(name) <= limit else name[: limit - 1] + "…"
