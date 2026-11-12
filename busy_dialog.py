"""BusyDialog: Turn off (or a model switch) was pressed while Strata is answering a request."""
from __future__ import annotations

import logging
import time
from typing import Callable

import customtkinter as ctk

import theme as T
from sweep_dialog import apply_modal, center_over, release_modal, style_dialog

log = logging.getLogger("agent_start")

DECISIONS = ("when_idle", "now", "cancel")
# Clicks in the first moment are ignored: the dialog opens centred over Turn off, so the 2nd click of a double-click
# would otherwise pick a button. Closing a CTkToplevel that early also crashed Tk (access violation) while CTk was
# still styling the new window.
ARM_S = 0.4


def describe(act: dict | None) -> str:
    """One line for what the model is doing (a metrics_store.activity() dict)."""
    if not act:
        return "Strata is working on a request."
    q = act.get("queued") or 0
    queued = f" · {q} more waiting" if q else ""
    if act.get("kind") == "reading":
        frac = act.get("frac")
        total = act.get("total")
        if frac is not None and total:
            return f"Reading a prompt · {100 * frac:.0f}% of {total:,.0f} tokens{queued}"
        return f"Reading a prompt{queued}"
    if act.get("kind") == "generating":
        toks = act.get("tokens")
        rate = act.get("rate")
        parts = ["Writing a reply"] + ([f"{toks:,.0f} tokens"] if toks is not None else []) + \
                ([f"{rate:.0f} tok/s"] if rate else [])
        return " · ".join(parts) + queued
    return f"{q} request{'s' if q != 1 else ''} waiting"


class BusyDialog(ctk.CTkToplevel):
    WIDTH = 340
    HEIGHT = 176

    def __init__(self, master, act: dict | None, on_decision: Callable[[str], None], switch_to: str | None = None) -> None:
        """switch_to: the model being switched to (a model switch), or None (Turn off)."""
        super().__init__(master, fg_color=T.BG)
        style_dialog(self)
        self._master = master
        self._on_decision = on_decision
        self._armed_at = time.monotonic() + ARM_S
        self._decided = False
        verb = "Switch" if switch_to else "Turn off"
        self.title("Strata is busy")
        self.resizable(False, False)
        center_over(self, master, self.WIDTH, self.HEIGHT)
        self.protocol("WM_DELETE_WINDOW", lambda: self._decide("cancel"))
        self.bind("<Escape>", lambda _e: self._decide("cancel"))
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="Strata is busy", anchor="w",
                     font=ctk.CTkFont(family=T.ui_family(), size=14, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        self.activity = ctk.CTkLabel(self, text=describe(act), anchor="w", text_color=T.TEXT_2, height=16,
                                     font=ctk.CTkFont(family=T.ui_family(), size=12))
        self.activity.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 0))
        what = f"to {switch_to}" if switch_to else ""
        ctk.CTkLabel(self, text=(f"Stopping now cuts this reply off. {verb} {what} after it finishes instead?"
                                 ).replace("  ", " "),
                     anchor="w", justify="left", wraplength=self.WIDTH - 24, text_color=T.TEXT_2,
                     font=ctk.CTkFont(family=T.ui_family(), size=11)).grid(
            row=2, column=0, sticky="ew", padx=12, pady=(6, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="ew", padx=12, pady=(10, 12))
        btns.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self.idle_btn = ctk.CTkButton(btns, text=f"{verb} when it's done", height=32, corner_radius=8,
                                      fg_color=T.TEXT, hover_color=T.PRIMARY_HOVER, text_color=T.ON_PRIMARY,
                                      command=lambda: self._decide("when_idle"))
        self.idle_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        small = ctk.CTkFont(family=T.ui_family(), size=12)
        self.now_btn = ctk.CTkButton(btns, text=f"{verb} now", height=28, corner_radius=8, font=small,
                                     fg_color=T.DANGER, hover_color=T.DANGER_HOVER,
                                     command=lambda: self._decide("now"))
        self.now_btn.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(6, 0))
        self.cancel_btn = ctk.CTkButton(btns, text="Keep running", height=28, corner_radius=8, font=small,
                                        fg_color="transparent", border_width=1, border_color=T.RAISED_HOVER,
                                        hover_color=T.RAISED, command=lambda: self._decide("cancel"))
        self.cancel_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(6, 0))
        apply_modal(self, master)

    def update_activity(self, act: dict | None) -> None:
        """Live text while the dialog is open; closes itself (as 'now') once the model went idle - nothing to cut."""
        if self._decided:
            return
        if act is None:
            wait = self._armed_at - time.monotonic()
            if wait > 0:                         # just opened: decide once it can safely close
                self._master.after(int(wait * 1000) + 20, lambda: self._decide("idle", user=False))
            else:
                self._decide("idle", user=False)
            return
        self.activity.configure(text=describe(act))

    def _decide(self, decision: str, user: bool = True) -> None:
        if self._decided or (user and time.monotonic() < self._armed_at):
            return
        self._decided = True
        release_modal(self)
        try:
            self.destroy()
        except Exception:
            log.exception("BusyDialog destroy failed")

        def fire() -> None:
            try:
                self._on_decision(decision)
            except Exception:
                log.exception("BusyDialog on_decision callback failed")

        # Not synchronously: this runs inside the clicked button's own command, and the decision re-lays out the
        # main window (update_idletasks) while Tk is still unwinding the destroyed dialog - that crashed Tk
        # (segfault, found by tests/test_busy_switch.py). One event-loop turn later it is safe.
        try:
            self._master.after(1, fire)
        except Exception:  # noqa: BLE001 - master gone (app closing): nothing left to decide
            log.debug("BusyDialog: master gone, decision %s dropped", decision)
