"""WhitelistModal: edit the user whitelist (apps the sweep must never close)."""
from __future__ import annotations

import logging
import threading
from typing import Callable

import customtkinter as ctk

import theme as T

from sweep_dialog import apply_modal, center_over, release_modal, style_dialog

log = logging.getLogger("hermes_controller")

_BG = T.BG
_ROW_BG = T.SURFACE
_MUTED = T.TEXT_2
_ERR = T.RED_TEXT
_ACCENT = T.TEXT             # primary action = off-white (front-panel style)
_ACCENT_HOVER = T.PRIMARY_HOVER
_NEUTRAL = T.RAISED
_NEUTRAL_HOVER = T.RAISED_HOVER


def norm(name: str) -> str:
    n = name.casefold()
    return n[:-4] if n.endswith(".exe") else n


class WhitelistModal(ctk.CTkToplevel):
    WIDTH = 320
    HEIGHT = 320

    def __init__(self, master, detector, on_saved: Callable[[], None] | None = None) -> None:
        super().__init__(master, fg_color=_BG)
        style_dialog(self)
        self._master = master
        self._detector = detector
        self._on_saved = on_saved
        self._closed = False
        self._loaded = False
        self._saving = False
        self._rows: list[tuple[str, ctk.BooleanVar]] = []

        self.title("Protected apps")
        self.resizable(False, False)
        center_over(self, master, self.WIDTH, self.HEIGHT)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        small = ctk.CTkFont(family=T.ui_family(), size=11)

        ctk.CTkLabel(self, text="Protected apps", anchor="w",
                     font=ctk.CTkFont(family=T.ui_family(), size=14, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        ctk.CTkLabel(self, text="Ticked apps are never closed by the sweep.", anchor="w",
                     text_color=_MUTED, font=small).grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))

        self._list = ctk.CTkScrollableFrame(self, fg_color=_ROW_BG, corner_radius=8)
        self._list.grid(row=2, column=0, sticky="nsew", padx=12)
        self._list.grid_columnconfigure(0, weight=1)
        self._loading_label = ctk.CTkLabel(self._list, text="Loading…", text_color=_MUTED, font=small)
        self._loading_label.grid(row=0, column=0, sticky="w", padx=8, pady=6)

        add_row = ctk.CTkFrame(self, fg_color="transparent")
        add_row.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 0))
        add_row.grid_columnconfigure(0, weight=1)
        self._entry = ctk.CTkEntry(add_row, placeholder_text="app.exe", height=28, font=small)
        self._entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._entry.bind("<Return>", lambda _e: self.add_name())
        self._add_btn = ctk.CTkButton(add_row, text="Add", width=56, height=28, fg_color=_NEUTRAL,
                                      hover_color=_NEUTRAL_HOVER, command=self.add_name, state="disabled")
        self._add_btn.grid(row=0, column=1)

        self._msg = ctk.CTkLabel(self, text="", anchor="w", text_color=_ERR, font=small, height=16)
        self._msg.grid(row=4, column=0, sticky="ew", padx=12)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 12))
        btns.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self._cancel_btn = ctk.CTkButton(btns, text="Cancel", height=30, fg_color="transparent", border_width=1,
                                         border_color=_NEUTRAL_HOVER, hover_color=_NEUTRAL, command=self.close)
        self._cancel_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._save_btn = ctk.CTkButton(btns, text="Save", height=30, fg_color=_ACCENT, hover_color=_ACCENT_HOVER, text_color=T.ON_PRIMARY,
                                       command=self.save, state="disabled")
        self._save_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        apply_modal(self, master)
        threading.Thread(target=self._load_worker, name="whitelist-load", daemon=True).start()

    # ------------------------------------------------------------------ loading
    def _load_worker(self) -> None:
        try:
            items = self._detector.list_whitelist_candidates()
            self._master.post(self._on_loaded, list(items), None)
        except Exception as exc:
            log.exception("list_whitelist_candidates failed")
            self._master.post(self._on_loaded, [], f"Could not list apps: {exc}")

    def _alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def _on_loaded(self, items: list[dict], error: str | None) -> None:
        if not self._alive():
            return
        self._loading_label.destroy()
        if error:
            # Saving now would overwrite the whitelist with an incomplete list, so keep Save disabled.
            self._msg.configure(text=error)
            return
        for item in items:
            name = str(item.get("name", "")).strip()
            if name and not self._has(name):
                self._add_row(name, bool(item.get("whitelisted")), bool(item.get("running", True)))
        self._loaded = True
        self._add_btn.configure(state="normal")
        self._save_btn.configure(state="normal")

    def _has(self, name: str) -> bool:
        key = norm(name)
        return any(norm(n) == key for n, _v in self._rows)

    def _add_row(self, name: str, checked: bool, running: bool) -> None:
        var = ctk.BooleanVar(value=checked)
        text = name if running else f"{name} (not running)"
        cb = ctk.CTkCheckBox(self._list, fg_color=T.TEXT, hover_color=T.PRIMARY_HOVER, checkmark_color=T.BG, border_color=T.TEXT_3, corner_radius=4, text=text, variable=var, font=ctk.CTkFont(family=T.ui_family(), size=12),
                             checkbox_width=18, checkbox_height=18)
        cb.grid(row=len(self._rows), column=0, sticky="w", padx=8, pady=3)
        self._rows.append((name, var))

    # ------------------------------------------------------------------ actions
    def checked_names(self) -> list[str]:
        return [n for n, v in self._rows if v.get()]

    def add_name(self) -> bool:
        """Validate the entry and append a ticked row. Returns True if a row was added."""
        if not self._loaded:
            return False
        name = self._entry.get().strip()
        if not name.lower().endswith(".exe") or len(name) <= 4:
            self._msg.configure(text="Enter an executable name ending in .exe")
            return False
        if self._has(name):
            self._msg.configure(text=f"{name} is already in the list")
            return False
        self._add_row(name, True, True)
        self._entry.delete(0, "end")
        self._msg.configure(text="")
        return True

    def save(self) -> None:
        if not self._loaded or self._saving:
            return
        self._saving = True
        names = self.checked_names()
        for b in (self._save_btn, self._add_btn, self._cancel_btn):
            b.configure(state="disabled")
        self._msg.configure(text="Saving…", text_color=_MUTED)
        threading.Thread(target=self._save_worker, args=(names,), name="whitelist-save", daemon=True).start()

    def _save_worker(self, names: list[str]) -> None:
        try:
            self._detector.update_whitelist(names)
            self._master.post(self._on_save_done, None)
        except Exception as exc:
            log.exception("update_whitelist failed")
            self._master.post(self._on_save_done, f"Save failed: {exc}")

    def _on_save_done(self, error: str | None) -> None:
        if error:
            self._saving = False
            if self._alive():
                self._msg.configure(text=error, text_color=_ERR)
                for b in (self._save_btn, self._add_btn, self._cancel_btn):
                    b.configure(state="normal")
            return
        self.close()
        if self._on_saved is not None:
            try:
                self._on_saved()
            except Exception:
                log.exception("WhitelistModal on_saved callback failed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        release_modal(self)
        try:
            self.destroy()
        except Exception:
            log.exception("WhitelistModal destroy failed")
