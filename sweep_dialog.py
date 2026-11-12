"""SweepDialog: pick which RAM-hungry apps to force-close before starting Strata."""
from __future__ import annotations

import ctypes
import logging
from typing import Callable

import customtkinter as ctk

import config_store
import theme as T

log = logging.getLogger("agent_start")

DECISIONS = ("close_and_start", "start_only", "cancel")

_BG = T.BG
_ROW_BG = T.SURFACE
_MUTED = T.TEXT_2
_WARN = T.AMBER
_DANGER = T.DANGER
_DANGER_HOVER = T.DANGER_HOVER
_NEUTRAL = T.RAISED
_NEUTRAL_HOVER = T.RAISED_HOVER


def _gb(mb: float | None) -> str:
    return f"{(mb or 0.0) / 1024.0:.1f} GB"


def row_detail(cand: dict) -> str:
    """'1.2 GB RAM · VRAM ~0.3 GB|n/a · reason' (second, muted line of a row)."""
    vram = cand.get("vram_mb")
    vram_txt = "VRAM n/a" if vram is None else f"VRAM ~{_gb(vram)}"
    return f"{_gb(cand.get('ram_mb'))} RAM · {vram_txt} · {cand.get('reason', '')}"


def row_text(cand: dict) -> str:
    """'name — 1.2 GB RAM · VRAM ~0.3 GB|n/a · reason' for one candidate dict (single-line form)."""
    return f"{cand.get('name', '?')} — {row_detail(cand)}"


def _virtual_screen(win) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the whole desktop across monitors (physical px)."""
    try:
        gm = ctypes.windll.user32.GetSystemMetrics
        x, y, w, h = gm(76), gm(77), gm(78), gm(79)          # SM_X/Y/CX/CYVIRTUALSCREEN
        if w > 0 and h > 0:
            return x, y, x + w, y + h
    except Exception:  # noqa: BLE001
        log.debug("GetSystemMetrics failed", exc_info=True)
    return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()


def center_over(win, master, width: int, height: int) -> None:
    """Place a CTkToplevel of logical size width x height centred over master (positions are physical px).

    Clamped to the virtual desktop, not the primary monitor: a controller sitting on a second screen keeps its
    dialogs on that screen.
    """
    try:
        scale = ctk.ScalingTracker.get_window_scaling(win)
        master.update_idletasks()
        pw, ph = round(width * scale), round(height * scale)
        x = master.winfo_rootx() + (master.winfo_width() - pw) // 2
        y = master.winfo_rooty() + (master.winfo_height() - ph) // 2
        left, top, right, bottom = _virtual_screen(win)
        x = max(left, min(x, right - pw))
        y = max(top, min(y, bottom - ph - 40))
        win.geometry(f"{width}x{height}+{x}+{y}")
    except Exception:  # geometry is cosmetic; never fail the dialog over it
        log.exception("centering dialog failed")
        win.geometry(f"{width}x{height}")


def later(win, ms: int, fn: Callable[[], None]) -> None:
    """win.after() whose id is remembered, so release_modal() can cancel it if the dialog closes first
    (a fired after-script of a destroyed window is an 'invalid command name' background error)."""
    ids = win.__dict__.setdefault("_hc_after_ids", [])
    ids.append(win.after(ms, fn))


def style_dialog(win) -> None:
    """Shared dialog chrome: dark caption + the app icon.

    CTkToplevel swaps in CustomTkinter's own icon 200 ms after creation unless iconbitmap() was called on that
    window, so the root's default icon alone is not enough.
    """
    later(win, 50, lambda: T.dark_caption(win))
    icon = config_store.APP_DIR / "assets" / "app.ico"
    try:
        if icon.exists():
            win.iconbitmap(str(icon))
    except Exception:  # noqa: BLE001
        log.debug("dialog icon not applied", exc_info=True)


def apply_modal(win, master) -> None:
    """Modal recipe: transient now, lift/focus/grab 200 ms later (guarded), no wait_window."""
    win.transient(master)

    def _grab() -> None:
        try:
            if win.winfo_exists():
                win.lift()
                win.focus_force()
                win.grab_set()
        except Exception:
            log.exception("modal grab failed")

    later(win, 200, _grab)


def release_modal(win) -> None:
    """Call right before destroy(): drop the grab and cancel the dialog's pending after-scripts."""
    for aid in win.__dict__.pop("_hc_after_ids", []):
        try:
            win.after_cancel(aid)
        except Exception:  # noqa: BLE001
            log.debug("after_cancel failed", exc_info=True)
    try:
        win.grab_release()
    except Exception:
        log.debug("grab_release failed", exc_info=True)


class SweepDialog(ctk.CTkToplevel):
    WIDTH = 372
    HEIGHT = 320
    RAM_LINE_H = 34                              # extra height when the full-speed RAM check is shown
    REOPEN_H = 26                                # extra height for the "Reopen these when Strata stops" checkbox

    def __init__(self, master, candidates: list[dict],
                 on_decision: Callable[[str, list[tuple[int, float]]], None], ram=None,
                 reopen_default: bool | None = None) -> None:
        """ram: an optional ram_plan.RamPlan. When it knows Strata's full-RAM requirement, its pre-ticks replace the
        detector's and a live line says whether the ticked apps free enough for the fast full-RAM mode."""
        super().__init__(master, fg_color=_BG)
        style_dialog(self)
        self._on_decision = on_decision
        self._decided = False
        self._rows: list[tuple[ctk.BooleanVar, dict]] = []
        self._ram = ram if ram is not None and getattr(ram, "need_mb", 0) > 0 else None
        # None = feature off (no checkbox). The choice is read by the caller after the decision.
        self._reopen_var = ctk.BooleanVar(value=bool(reopen_default)) if reopen_default is not None else None
        self.reopen_choice = bool(reopen_default)
        pre = set(self._ram.preticked_pids) if self._ram else None

        self.title("Close apps before start")
        self.resizable(False, False)
        center_over(self, master, self.WIDTH, self.HEIGHT + (self.RAM_LINE_H if self._ram else 0)
                    + (self.REOPEN_H if self._reopen_var is not None else 0))
        self.protocol("WM_DELETE_WINDOW", lambda: self._decide("cancel"))
        self.bind("<Escape>", lambda _e: self._decide("cancel"))

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)       # rows: title, warning, RAM check (opt.), list, reopen (opt.), buttons
        self._ram_head = self._ram_live = None
        if self._ram:
            ram_box = ctk.CTkFrame(self, fg_color="transparent")
            ram_box.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
            self._ram_head = ctk.CTkLabel(ram_box, text=self._ram.summary, anchor="w", height=15,
                                          font=ctk.CTkFont(family=T.ui_family(), size=11, weight="bold"))
            self._ram_head.pack(fill="x")
            self._ram_live = ctk.CTkLabel(ram_box, text="", anchor="w", height=15, text_color=_MUTED,
                                          font=ctk.CTkFont(family=T.ui_family(), size=11))
            self._ram_live.pack(fill="x")

        ctk.CTkLabel(self, text="Close apps to free RAM?", anchor="w",
                     font=ctk.CTkFont(family=T.ui_family(), size=14, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        ctk.CTkLabel(self, text="Force-closes apps — unsaved work is lost.", anchor="w",
                     text_color=_WARN, font=ctk.CTkFont(family=T.ui_family(), size=11)).grid(
            row=1, column=0, sticky="ew", padx=12, pady=(0, 6))

        self._list = ctk.CTkScrollableFrame(self, fg_color=_ROW_BG, corner_radius=8)
        self._list.grid(row=3, column=0, sticky="nsew", padx=12)
        self._list.grid_columnconfigure(1, weight=1)
        small = ctk.CTkFont(family=T.ui_family(), size=11)
        name_font = ctk.CTkFont(family=T.ui_family(), size=12, weight="bold")
        for i, cand in enumerate(candidates):
            ticked = int(cand.get("pid", -1)) in pre if pre is not None else bool(cand.get("preselect"))
            var = ctk.BooleanVar(value=ticked)
            cb = ctk.CTkCheckBox(self._list, fg_color=T.TEXT, hover_color=T.PRIMARY_HOVER, checkmark_color=T.BG, border_color=T.TEXT_3, corner_radius=4, text="", width=22, checkbox_width=18, checkbox_height=18,
                                 variable=var, command=self._update_buttons)
            cb.grid(row=i, column=0, sticky="nw", padx=(6, 4), pady=4)
            cell = ctk.CTkFrame(self._list, fg_color="transparent")
            cell.grid(row=i, column=1, sticky="ew", pady=3)
            name = str(cand.get("name", "?"))
            if len(name) > 30:                     # e.g. EOSOverlayRenderer-Win64-Shipping.exe
                name = name[:28] + "…"
            title = ctk.CTkLabel(cell, text=name, font=name_font, anchor="w", height=16)
            title.pack(fill="x")
            detail = ctk.CTkLabel(cell, text=row_detail(cand), font=small, anchor="w", height=14,
                                  text_color=_MUTED)
            detail.pack(fill="x")
            for w in (cell, title, detail):
                w.bind("<Button-1>", lambda _e, c=cb: c.toggle())
            self._rows.append((var, cand))

        if self._reopen_var is not None:
            ctk.CTkCheckBox(self, text="Reopen these when Strata stops (games: one click)", variable=self._reopen_var,
                            font=small, text_color=_MUTED, fg_color=T.TEXT, hover_color=T.PRIMARY_HOVER,
                            checkmark_color=T.BG, border_color=T.TEXT_3, corner_radius=4,
                            checkbox_width=16, checkbox_height=16).grid(
                row=4, column=0, sticky="w", padx=14, pady=(8, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=5, column=0, sticky="ew", padx=12, pady=(8, 12))
        btns.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self._close_btn = ctk.CTkButton(btns, text="Close selected & start", height=32,
                                        fg_color=_DANGER, hover_color=_DANGER_HOVER,
                                        command=lambda: self._decide("close_and_start"))
        self._close_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        ctk.CTkButton(btns, text="Start without closing", height=28, fg_color=_NEUTRAL,
                      hover_color=_NEUTRAL_HOVER, font=ctk.CTkFont(family=T.ui_family(), size=12),
                      command=lambda: self._decide("start_only")).grid(
            row=1, column=0, sticky="ew", padx=(0, 4), pady=(6, 0))
        ctk.CTkButton(btns, text="Cancel", height=28, fg_color="transparent", border_width=1,
                      border_color=_NEUTRAL_HOVER, hover_color=_NEUTRAL, font=ctk.CTkFont(family=T.ui_family(), size=12),
                      command=lambda: self._decide("cancel")).grid(
            row=1, column=1, sticky="ew", padx=(4, 0), pady=(6, 0))

        self._update_buttons()
        apply_modal(self, master)

    # ------------------------------------------------------------------ helpers
    def selected_targets(self) -> list[tuple[int, float]]:
        return [(int(c["pid"]), float(c["create_time"])) for v, c in self._rows if v.get()]

    def _update_buttons(self) -> None:
        """The destructive button states its cost: how many apps and how much RAM."""
        picked = [c for v, c in self._rows if v.get()]
        ram = sum(float(c.get("ram_mb") or 0.0) for c in picked)
        if picked:
            self._close_btn.configure(state="normal", text=f"Close {len(picked)} & start · {_gb(ram)}")
        else:
            self._close_btn.configure(state="disabled", text="Close selected & start")
        self._update_ram_line(ram)

    def _update_ram_line(self, freed_mb: float) -> None:
        """Live: would closing the ticked apps reach Strata's fast full-RAM mode?"""
        if not self._ram or self._ram_live is None:
            return
        r = self._ram
        if r.enough_now:
            self._ram_head.configure(text_color=T.ACCENT)
            self._ram_live.configure(text="Strata will load in full-RAM mode (fast)", text_color=_MUTED)
            return
        self._ram_head.configure(text_color=_WARN)
        gap = r.need_mb - r.available_mb - freed_mb
        if gap <= 0:
            self._ram_live.configure(text=f"Ticked apps free ~{_gb(freed_mb)} → full speed", text_color=_MUTED)
        elif not r.reachable:
            self._ram_live.configure(text=r.detail + " · will load slower", text_color=_WARN)
        else:
            self._ram_live.configure(text=f"Still ~{_gb(gap)} short → Low-RAM mode (slower)", text_color=_WARN)

    def _decide(self, decision: str) -> None:
        if self._decided:
            return
        self._decided = True
        targets = self.selected_targets() if decision == "close_and_start" else []
        if self._reopen_var is not None:
            self.reopen_choice = bool(self._reopen_var.get())
        release_modal(self)
        try:
            self.destroy()
        except Exception:
            log.exception("SweepDialog destroy failed")
        try:
            self._on_decision(decision, targets)
        except Exception:
            log.exception("SweepDialog on_decision callback failed")
