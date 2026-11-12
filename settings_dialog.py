"""SettingsDialog: three tabs under the title row.

Model      what the engine launches: a launcher file (engines with launcher files), or program + arguments with
           friendly Build / Model / Context length / Vision / Port controls (each one rewrites the arguments box,
           which stays the single source of truth). Save / Cancel and the message line live here.
Behaviour  what happens when other apps need the PC, after Turn off, and Agent Start's own switches.
About      version and updates, usage history, the endpoint.
Controls on Behaviour and About apply at once (only a Close button there). Everything engine-specific is asked of
the engine.
"""
from __future__ import annotations

import logging
import math
import os
import time
import webbrowser
from tkinter import filedialog
from typing import Any, Callable

import customtkinter as ctk

import config_store
import engines
import theme as T
from engines.base import CAP_LAUNCHER_FILES, CommandLaunch, LaunchError, join_args, split_args
from sweep_dialog import apply_modal, center_over, later, release_modal, style_dialog
from version import __version__
from whitelist_modal import WhitelistModal

log = logging.getLogger("agent_start")

NO_LAUNCHERS = "No run-*.bat found"          # the Strata wording (tests); built per engine below
CHECK_LABEL = "Check for updates"
CHECK_LABEL_SOURCE = "Check for updates (installed app only)"   # the daily switch's wording when run from source
CHECK_TEXTS = {                                  # the manual check's result, next to the button
    "checking": "Checking GitHub…",
    "current": "Up to date · v{version}",
    "newer": "Version {version} is available",
    "error": "Could not reach GitHub",
    "busy": "An update is being installed",
}

TABS = ("Model", "Behaviour", "About")
ADVANCED_TITLE = "Advanced · program and arguments"
GROUP_OTHER_APPS = "When other apps need the PC"
GROUP_AFTER_OFF = "After Turn off"
GROUP_APP = "Agent Start"
REOPEN_LABEL = "Reopen the apps that were closed"
PROTECTED_LABEL = "Protected apps"
HISTORY_LABEL = "Usage history"
# Behaviour rows: the app's switch keys -> the label shown (the callbacks and values are the app's, unchanged)
ROW_LABELS = {"games": "A game starts", "memory": "Memory runs low", "resume": "That app closes",
              "tray": "Keep running in the tray", "autostart": "Start with Windows"}
SOURCE_SUFFIX = " (installed app only)"         # kept on a relabelled row whose switch only works when installed
VISION_OFF = "Off"
VISION_ON = "On · {label}"
VISION_NONE = "No vision file next to this model"
VISION_OTHER = "Another file (in the arguments)"
CONTEXT_CUSTOM = "{tokens:,} tokens (custom)"
PORT_ERROR = "Port must be a number from 1 to 65535"
FITS_TEXT = "Fits on the GPU"
VISION_DROPPED = "Vision turned off · that vision file belongs to the other model"
FIT_DEBOUNCE_MS = 600                            # the GPU-fit check runs this long after the last change
ARM_S = 0.4                                      # a close in the first moment waits this long (see close())
CHEVRON_RIGHT = "\uE76C"                         # Segoe Fluent Icons "ChevronRight"


class LinkRow(ctk.CTkFrame):
    """A full-width row that opens something (label left, chevron right), hover = raised. invoke() / set_state()
    behave like a CTkButton's (a disabled row ignores clicks)."""

    def __init__(self, master, text: str, command: Callable[[], None], font, glyph_font) -> None:
        super().__init__(master, fg_color=T.SURFACE, corner_radius=8, height=28)
        self.text = text
        self._command = command
        self.enabled = True
        self.grid_columnconfigure(0, weight=1)
        self._text_label = ctk.CTkLabel(self, text=text, font=font, height=28, anchor="w", text_color=T.TEXT)
        self._text_label.grid(row=0, column=0, sticky="w", padx=(10, 0))
        self._glyph_label = ctk.CTkLabel(self, text=CHEVRON_RIGHT, font=glyph_font, width=14, height=28,
                                         text_color=T.TEXT_2)
        self._glyph_label.grid(row=0, column=1, padx=(6, 10))
        for w in (self, self._text_label, self._glyph_label):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda _e: self.invoke())
            w.bind("<Enter>", lambda _e: self._hover(True))
            w.bind("<Leave>", lambda _e: self._hover(False))

    def _hover(self, on: bool) -> None:
        try:
            self.configure(fg_color=T.RAISED if on and self.enabled else T.SURFACE)
        except Exception:  # noqa: BLE001 - closing
            log.debug("link row hover failed", exc_info=True)

    def invoke(self) -> None:
        if self.enabled:
            self._command()

    def set_state(self, state: str) -> None:
        self.enabled = state != "disabled"
        self._text_label.configure(text_color=T.TEXT if self.enabled else T.TEXT_3)
        self._glyph_label.configure(text_color=T.TEXT_2 if self.enabled else T.TEXT_3)
        if not self.enabled:
            self._hover(False)


class SettingsDialog(ctk.CTkToplevel):
    WIDTH = 340
    LABEL_W = 96                                 # the label column of the Model tab
    CHOICE_W = 150                               # the dropdowns on the Behaviour tab
    EXT_GLYPH = "\uE8A7"   # Segoe Fluent Icons "Open in new window"

    def __init__(self, master, cfg: dict, detector,
                 on_save: Callable[[Any, Callable[[str | None], None]], None],
                 on_closed: Callable[[], None] | None = None,
                 current_bat: str | None = None, running: bool = False,
                 on_whitelist_saved: Callable[[], None] | None = None,
                 reopen_after: bool | None = None,
                 on_reopen_changed: Callable[[bool], None] | None = None,
                 update_version: str | None = None,
                 on_update: Callable[[], None] | None = None,
                 switches: list[dict] | None = None,
                 on_check_updates: Callable[[Callable[[str, str | None], bool]], str] | None = None,
                 can_check_updates: bool = True, engine=None,
                 on_history: Callable[[], Any] | None = None,
                 launch_options: dict | None = None,
                 on_fit_preview: Callable[[Any, Callable[[str | None], None]], bool] | None = None,
                 engine_choices: list[tuple[str, str]] | None = None,
                 on_engine_change: Callable[[str], str | None] | None = None,
                 tab: str | None = None) -> None:
        """on_check_updates(on_result) adds the "Check for updates" row on About (none when it is None): it
        starts a check and returns "started" / "joined" (on_result(status, version) follows once, on the Tk thread,
        status "current" | "newer" | "error"; it returns True when the dialog showed it) or "busy" / "unavailable".
        can_check_updates=False (run from source): the button is greyed with the switch's "(installed app only)".

        on_history() opens the usage history window and returns it (a "Usage history" row on About; none when it
        is None).

        engine_choices: [(engine id, display name)]. With two or more, an "Engine" row is shown on Model;
        on_engine_change(id) saves the choice and returns the note to show (e.g. when it applies) or None.

        switches: more settings, applied at once (like the reopen switch): {"key", "label", "value",
        "on_change": callable, "enabled": bool = True, "choices": [str] (a dropdown instead of a switch)}. Known keys
        go to their Behaviour group under the labels in ROW_LABELS ("updates" goes to About); others keep their
        label (choices: the first group, switches: the Agent Start group).

        launch_options (engines without launcher files): see Engine.launch_options. on_fit_preview(info, done) ->
        bool: will this launch fit on the GPU? done(text | "" | None) follows on the Tk thread.

        on_save(info, done) persists the launcher and calls done(error | None) on the Tk thread.

        current_bat overrides the configured launcher (a saved change that is waiting for the next start);
        running=True adds the "applies next start" hint when another launcher is picked. tab: the tab to open on.
        """
        super().__init__(master, fg_color=T.BG)
        style_dialog(self)
        self._armed_at = time.monotonic() + ARM_S
        self._close_pending = False
        self._app = master
        self._detector = detector
        self._on_save = on_save
        self._on_closed = on_closed
        self._on_whitelist_saved = on_whitelist_saved
        self._on_reopen_changed = on_reopen_changed
        self._on_update = on_update
        self._on_check_updates = on_check_updates
        self._on_history = on_history
        self._on_engine_change = on_engine_change
        self._on_fit_preview = on_fit_preview
        self._engine_choices = [(str(i), str(n)) for i, n in (engine_choices or [])]
        self.has_engine_row = len(self._engine_choices) >= 2 and on_engine_change is not None
        self._can_check = bool(can_check_updates)
        self._checking = False
        self._running = bool(running)
        self._closed = False
        self._saving = False
        self._child: Any = None                      # the protected-apps list or the usage history window
        self._engine = engine if engine is not None else engines.get_engine(cfg)
        self._cfg = cfg
        self._files = self._engine.can(CAP_LAUNCHER_FILES)   # a launcher-file picker, else program + arguments
        self._no_launchers = f"No {self._engine.launcher_kind} found"
        self._info: Any = None
        self._paths: dict[str, str] = {}                 # dropdown label -> absolute launcher path
        self._initial = current_bat or (self._engine.current_launcher(cfg) if self._files else None)
        self._has_launch_cmd = bool(cfg.get("launch_cmd"))
        # engines without launcher files: what the engine found on this PC + the friendly controls
        opts = launch_options if isinstance(launch_options, dict) and not self._files else {}
        self._opts = opts
        self._opt_programs = [(str(a), str(b)) for a, b in (opts.get("programs") or [])]
        self._opt_models = [(str(a), str(b)) for a, b in (opts.get("models") or [])]
        self.option_rows = int(bool(self._opt_programs)) + int(bool(self._opt_models))
        self._has_context = all(k in opts for k in ("context_choices", "context_of", "with_context"))
        self._has_vision = all(callable(opts.get(k)) for k in ("vision_files", "vision_of", "with_vision"))
        self._has_port = all(callable(opts.get(k)) for k in ("port_of", "with_port"))
        self._context_choices: list[tuple[str, int | None]] = []
        if self._has_context:
            try:
                self._context_choices = [(str(la), (int(n) if n else None)) for la, n in opts["context_choices"]]
            except Exception:  # noqa: BLE001
                log.debug("context choices unusable", exc_info=True)
            self._has_context = bool(self._context_choices)
        self._program_menu: ctk.CTkOptionMenu | None = None
        self._model_menu: ctk.CTkOptionMenu | None = None
        self._program_choice: ctk.StringVar | None = None
        self._model_choice: ctk.StringVar | None = None
        self._context_menu: ctk.CTkOptionMenu | None = None
        self._context_choice: ctk.StringVar | None = None
        self._vision_menu: ctk.CTkOptionMenu | None = None
        self._vision_choice: ctk.StringVar | None = None
        self._vision_paths: dict[str, str] = {}          # vision dropdown label -> projector path
        self._vision_cache: dict[str, list[tuple[str, str]]] = {}
        self._port_entry: ctk.CTkEntry | None = None
        self._port_var: ctk.StringVar | None = None
        self._port_bad = False
        self._port_typing = False
        self._syncing = False
        self._fit_note: ctk.CTkLabel | None = None
        self._fit_row = 0
        self._fit_seq = 0                                # answers of older requests are ignored
        self._fit_after: str | None = None
        self._fit_key: tuple | None = None
        self.fit_calls = 0                               # requests handed to on_fit_preview
        self.fit_state: str | None = None                # None (no note) | "fits" | "warn"
        self.advanced_open = False
        self._adv_body: ctk.CTkFrame | None = None
        self._adv_glyph: ctk.CTkLabel | None = None
        self._adv_row = 0
        self.current_tab = TABS[0]
        self.tab_heights: dict[str, int] = {}

        self.title("Settings")
        self.resizable(False, False)
        self._extra_switches = [sw for sw in (switches or []) if isinstance(sw, dict) and sw.get("key")]
        self.logical_height = 480                        # provisional; measured once everything is built
        center_over(self, master, self.WIDTH, self.logical_height)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._small = ctk.CTkFont(family=T.ui_family(), size=11)
        self._body = ctk.CTkFont(family=T.ui_family(), size=12)
        self._mono = ctk.CTkFont(family=T.mono_family(), size=11)
        self._glyph_font = ctk.CTkFont(family=T.icon_family(), size=10)

        # ---- title row: "Settings", the version (or the update link) at the right
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Settings", anchor="w", height=20,
                     font=ctk.CTkFont(family=T.ui_family(), size=14, weight="bold")).grid(row=0, column=0, sticky="w")
        self._head = head
        self._update_link: ctk.CTkLabel | None = None
        self._version_label: ctk.CTkLabel | None = None
        if update_version and on_update is not None:     # a newer release: the version label becomes the way in
            self._show_update_link(update_version)
        else:
            self._version_label = ctk.CTkLabel(head, text=f"v{__version__}", anchor="e", height=20, font=self._small,
                                               text_color=T.TEXT_3)
            self._version_label.grid(row=0, column=1, sticky="e")

        # ---- tab bar: a SURFACE track, the active tab on RAISED
        bar = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=8, height=32)
        bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(8, 12))
        self._tab_buttons: dict[str, ctk.CTkButton] = {}
        for i, name in enumerate(TABS):
            bar.grid_columnconfigure(i, weight=1, uniform="tabs")
            b = ctk.CTkButton(bar, text=name, height=24, corner_radius=6, font=self._body, fg_color=T.SURFACE,
                              hover_color=T.RAISED, text_color=T.TEXT_2, command=lambda n=name: self.show_tab(n))
            b.grid(row=0, column=i, sticky="ew", padx=(4, 4 if i == len(TABS) - 1 else 0), pady=4)
            self._tab_buttons[name] = b
        self._tab_bar = bar

        # ---- the three tab bodies (one shown at a time, all in row 2)
        self.tabs: dict[str, ctk.CTkFrame] = {}
        for name in TABS:
            f = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
            f.grid_columnconfigure(0, weight=1)
            self.tabs[name] = f
        self.switches: dict[str, tuple] = {}
        self._built_rows: dict[str, tuple] = {}
        self._build_model_tab(self.tabs["Model"])
        self._build_behaviour_tab(self.tabs["Behaviour"], reopen_after)
        self._build_about_tab(self.tabs["About"])
        # the app's switches in the order the app gave them (callers look them up by key)
        self.switches["reopen"] = (self._reopen_var, self._reopen_switch)
        for sw in self._extra_switches:
            k = str(sw["key"])
            if k in self._built_rows:
                self.switches[k] = self._built_rows[k]

        # ---- footer: one message slot + the buttons (Model: Cancel / Save; the other tabs: Close)
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 12))
        foot.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self._msg = ctk.CTkLabel(foot, text="", anchor="w", text_color=T.TEXT_2, font=self._small, height=16)
        self._about_msg = ctk.CTkLabel(foot, text="", anchor="w", text_color=T.TEXT_2, font=self._small, height=16)
        self._msg.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self._cancel_btn = ctk.CTkButton(foot, text="Cancel", height=28, corner_radius=8, font=self._body,
                                         fg_color="transparent", border_width=1, border_color=T.RAISED_HOVER,
                                         hover_color=T.RAISED, text_color=T.TEXT, command=self.close)
        self._cancel_btn.grid(row=1, column=0, sticky="ew", padx=(0, 4))
        self._save_btn = ctk.CTkButton(foot, text="Save", height=28, corner_radius=8, font=self._body,
                                       fg_color=T.RAISED, hover_color=T.PRIMARY_HOVER, text_color=T.ON_PRIMARY,
                                       text_color_disabled=T.TEXT_3, command=self.save, state="disabled")
        self._save_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        self._close_btn = ctk.CTkButton(foot, text="Close", height=28, corner_radius=8, font=self._body,
                                        fg_color="transparent", border_width=1, border_color=T.RAISED_HOVER,
                                        hover_color=T.RAISED, text_color=T.TEXT, command=self.close)
        self._foot = foot

        self._load_launchers()
        self._measure()
        self.show_tab(tab if tab in TABS else TABS[0])
        center_over(self, master, self.WIDTH, self.logical_height)
        apply_modal(self, master)

    # ------------------------------------------------------------------ building blocks
    def _option_menu(self, master, var, values, command, width: int = 0) -> ctk.CTkOptionMenu:
        return ctk.CTkOptionMenu(
            master, variable=var, values=values, command=command, height=28, corner_radius=8,
            dynamic_resizing=False, font=self._body, dropdown_font=self._body, fg_color=T.RAISED,
            button_color=T.RAISED, button_hover_color=T.RAISED_HOVER, text_color=T.TEXT, text_color_disabled=T.TEXT_3,
            dropdown_fg_color=T.SURFACE, dropdown_hover_color=T.RAISED, dropdown_text_color=T.TEXT,
            **({"width": width} if width else {}))

    def _heading(self, master, text: str, row: int, first: bool = False) -> None:
        ctk.CTkLabel(master, text=text, anchor="w", text_color=T.TEXT_2, font=self._small, height=16).grid(
            row=row, column=0, sticky="ew", pady=(0 if first else 8, 4))       # 4 under the row above + 8 = 12

    def _labelled(self, master, row: int, text: str) -> ctk.CTkFrame:
        """A Model-tab row: the label in the label column, the control (column 1) filling the rest."""
        line = ctk.CTkFrame(master, fg_color="transparent", height=28)
        line.grid(row=row, column=0, sticky="ew", pady=(0, 4))
        line.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(line, text=text, font=self._body, text_color=T.TEXT, anchor="w", width=self.LABEL_W,
                     height=28).grid(row=0, column=0, sticky="w")
        return line

    def _side_row(self, master, row: int, text: str) -> tuple[ctk.CTkFrame, ctk.CTkLabel]:
        """A Behaviour / About row: the label left, the control at the right."""
        line = ctk.CTkFrame(master, fg_color="transparent", height=28)
        line.grid(row=row, column=0, sticky="ew", pady=(0, 4))
        line.grid_columnconfigure(0, weight=1)
        lab = ctk.CTkLabel(line, text=text, font=self._body, text_color=T.TEXT, anchor="w", height=28)
        lab.grid(row=0, column=0, sticky="w")
        return line, lab

    def _switch_row(self, master, row: int, text: str, var, command, enabled: bool = True) -> ctk.CTkSwitch:
        line, lab = self._side_row(master, row, text)
        sw = ctk.CTkSwitch(line, text="", variable=var, width=36, switch_width=34, switch_height=18,
                           progress_color=T.ACCENT, button_color=T.TEXT, button_hover_color=T.PRIMARY_HOVER,
                           fg_color=T.RAISED, command=command)
        sw.grid(row=0, column=1, sticky="e")
        if not enabled:
            sw.configure(state="disabled")
            lab.configure(text_color=T.TEXT_3)
        lab.bind("<Button-1>", lambda _e: sw.toggle())       # the label is part of the control (respects disabled)
        return sw

    # ------------------------------------------------------------------ Model tab
    def _build_model_tab(self, tab: ctk.CTkFrame) -> None:
        self._engine_var: ctk.StringVar | None = None
        self._engine_menu: ctk.CTkOptionMenu | None = None
        r = 0
        if self.has_engine_row:
            line = self._labelled(tab, r, "Engine")
            names = [n for _i, n in self._engine_choices]
            current = next((n for i, n in self._engine_choices if i == getattr(self._engine, "id", None)), names[0])
            self._engine_var = ctk.StringVar(value=current)
            self._engine_menu = self._option_menu(line, self._engine_var, names, self._engine_picked)
            self._engine_menu.grid(row=0, column=1, sticky="ew")
            r += 1

        cfg = self._cfg
        name = self._engine.name
        self._choice = ctk.StringVar(value=self._no_launchers)
        cmd = [str(x) for x in (cfg.get("launch_cmd") or [])]
        self._program = ctk.StringVar(value=cmd[0] if cmd else "")
        self._args = ctk.StringVar(value=join_args(cmd[1:]))
        if self._files:
            # the launcher dropdown + Browse, and the read-only line under it (what the file will run)
            ctk.CTkLabel(tab, text=f"{name} launcher", anchor="w", text_color=T.TEXT_2, font=self._small,
                         height=16).grid(row=r, column=0, sticky="ew", pady=(8 if r else 0, 4))
            pick = ctk.CTkFrame(tab, fg_color="transparent")
            pick.grid(row=r + 1, column=0, sticky="ew")
            pick.grid_columnconfigure(0, weight=1)
            self._menu = self._option_menu(pick, self._choice, [self._no_launchers], self._on_pick)
            self._menu.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            self._browse_btn = ctk.CTkButton(pick, text="Browse…", width=76, height=28, corner_radius=8,
                                             font=self._body, fg_color=T.RAISED, hover_color=T.RAISED_HOVER,
                                             text_color=T.TEXT, command=self.browse)
            self._browse_btn.grid(row=0, column=1)
            self._detail = ctk.CTkLabel(tab, text="", anchor="nw", justify="left", font=self._mono,
                                        text_color=T.TEXT_2, wraplength=self.WIDTH - 24, height=32)
            self._detail.grid(row=r + 2, column=0, sticky="ew", pady=(4, 0))
            self._program_entry = ctk.CTkEntry(tab)          # not used with launcher files (never laid out)
            self._args_entry = ctk.CTkEntry(tab)
            return

        # engines without launcher files: labelled rows, then the Advanced fold (program + arguments)
        self._menu = self._option_menu(tab, self._choice, [self._no_launchers], self._on_pick)   # never laid out
        self._detail = ctk.CTkLabel(tab, text="")                                                # never laid out
        opts = self._opts
        for title, items, attr in ((str(opts.get("program_title") or "Program"), self._opt_programs, "program"),
                                   (str(opts.get("model_title") or "Model"), self._opt_models, "model")):
            if not items:
                continue
            line = self._labelled(tab, r, title)
            var = ctk.StringVar(value=f"Choose a {title.lower()}…")
            menu = self._option_menu(line, var, [lab for lab, _p in items],
                                     self._program_picked if attr == "program" else self._model_picked)
            menu.grid(row=0, column=1, sticky="ew")
            if attr == "program":
                self._program_menu, self._program_choice = menu, var
            else:
                self._model_menu, self._model_choice = menu, var
            r += 1
        if self._has_context:
            line = self._labelled(tab, r, "Context length")
            self._context_choice = ctk.StringVar(value=self._context_choices[0][0])
            self._context_menu = self._option_menu(line, self._context_choice,
                                                   [la for la, _n in self._context_choices], self._context_picked)
            self._context_menu.grid(row=0, column=1, sticky="ew")
            r += 1
        # the GPU-fit note: a one-line slot under Context length (under the Model row without one)
        self._fit_note = ctk.CTkLabel(tab, text="", anchor="w", justify="left", font=self._small, height=16,
                                      text_color=T.TEXT_2, wraplength=self.WIDTH - 24)
        self._fit_row = r
        r += 1
        if self._has_vision:
            line = self._labelled(tab, r, "Vision")
            self._vision_choice = ctk.StringVar(value=VISION_OFF)
            self._vision_menu = self._option_menu(line, self._vision_choice, [VISION_OFF], self._vision_picked)
            self._vision_menu.grid(row=0, column=1, sticky="ew")
            r += 1
        if self._has_port:
            line = self._labelled(tab, r, "Port")
            self._port_var = ctk.StringVar(value="")
            self._port_entry = ctk.CTkEntry(line, textvariable=self._port_var, width=80, height=28, corner_radius=8,
                                            font=self._body, fg_color=T.SURFACE, border_color=T.HAIRLINE,
                                            text_color=T.TEXT)
            self._port_entry.grid(row=0, column=1, sticky="w")
            r += 1

        # Advanced · program and arguments (the arguments box is the single source of truth)
        adv = ctk.CTkFrame(tab, fg_color="transparent", height=28, cursor="hand2")
        adv.grid(row=r, column=0, sticky="ew", pady=(4 if r else 0, 0))
        self._adv_glyph = ctk.CTkLabel(adv, text=CHEVRON_RIGHT, font=self._glyph_font, width=14, height=28,
                                       text_color=T.TEXT_2, cursor="hand2")
        self._adv_glyph.grid(row=0, column=0, sticky="w")
        adv_text = ctk.CTkLabel(adv, text=ADVANCED_TITLE, font=self._body, text_color=T.TEXT_2, anchor="w",
                                height=28, cursor="hand2")
        adv_text.grid(row=0, column=1, sticky="w", padx=(4, 0))
        for w in (adv, self._adv_glyph, adv_text):
            w.bind("<Button-1>", lambda _e: self.toggle_advanced())
            w.bind("<Enter>", lambda _e: adv_text.configure(text_color=T.TEXT))
            w.bind("<Leave>", lambda _e: adv_text.configure(text_color=T.TEXT_2))
        self._adv_head = adv
        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.grid_columnconfigure(0, weight=1)
        self._adv_body = body
        self._adv_row = r + 1
        self._program_entry = ctk.CTkEntry(body, textvariable=self._program, height=28, corner_radius=8,
                                           font=self._mono, fg_color=T.SURFACE, border_color=T.HAIRLINE,
                                           text_color=T.TEXT, placeholder_text="Program (.exe)")
        self._program_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(4, 0))
        self._browse_btn = ctk.CTkButton(body, text="Browse…", width=76, height=28, corner_radius=8, font=self._body,
                                         fg_color=T.RAISED, hover_color=T.RAISED_HOVER, text_color=T.TEXT,
                                         command=self.browse)
        self._browse_btn.grid(row=0, column=1, pady=(4, 0))
        self._args_entry = ctk.CTkEntry(body, textvariable=self._args, height=28, corner_radius=8, font=self._mono,
                                        fg_color=T.SURFACE, border_color=T.HAIRLINE, text_color=T.TEXT,
                                        placeholder_text="Arguments")
        self._args_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def set_advanced(self, open_: bool) -> None:
        """Show / hide the program and arguments boxes (engines without launcher files)."""
        if self._adv_body is None or self._adv_glyph is None:
            return
        self.advanced_open = bool(open_)
        if self.advanced_open:
            self._adv_body.grid(row=self._adv_row, column=0, sticky="ew")
        else:
            self._adv_body.grid_remove()
        self._adv_glyph.configure(text=T.CHEVRON_DOWN if self.advanced_open else CHEVRON_RIGHT)

    def toggle_advanced(self) -> None:
        self.set_advanced(not self.advanced_open)

    # ------------------------------------------------------------------ Behaviour tab
    def _build_behaviour_tab(self, tab: ctk.CTkFrame, reopen_after: bool | None) -> None:
        other_apps: list[dict] = []
        own: list[dict] = []
        self._about_switches: list[dict] = []
        for sw in self._extra_switches:
            key = str(sw["key"])
            if key == "updates":
                self._about_switches.append(sw)
            elif key in ("games", "memory", "resume") or (sw.get("choices") and key not in ("tray", "autostart")):
                other_apps.append(sw)
            else:
                own.append(sw)
        self._reopen_var = ctk.BooleanVar(value=bool(reopen_after))
        r = 0
        if other_apps:
            self._heading(tab, GROUP_OTHER_APPS, r, first=True)
            r += 1
            for sw in other_apps:
                self._built_rows[str(sw["key"])] = self._setting_row(tab, r, sw)
                r += 1
        self._heading(tab, GROUP_AFTER_OFF, r, first=r == 0)
        r += 1
        # Reopen the apps closed for the engine after Turn off (also offered in the start dialog). Applies at once.
        self._reopen_switch = self._switch_row(tab, r, REOPEN_LABEL, self._reopen_var, self._reopen_toggled)
        r += 1
        self._protected_btn = LinkRow(tab, PROTECTED_LABEL, self.open_protected, self._body, self._glyph_font)
        self._protected_btn.grid(row=r, column=0, sticky="ew", pady=(0, 4))
        r += 1
        if own:
            self._heading(tab, GROUP_APP, r)
            r += 1
            for sw in own:
                self._built_rows[str(sw["key"])] = self._setting_row(tab, r, sw)
                r += 1

    @staticmethod
    def _row_label(sw: dict) -> str:
        key, label = str(sw.get("key")), str(sw.get("label"))
        new = ROW_LABELS.get(key)
        if new is None:
            return label
        return new + SOURCE_SUFFIX if label.endswith(SOURCE_SUFFIX) else new

    def _setting_row(self, master, row: int, sw: dict) -> tuple:
        """One switch or choice from `switches` -> (variable, widget), as self.switches holds them."""
        text = self._row_label(sw)
        enabled = bool(sw.get("enabled", True))
        if sw.get("choices"):                    # a choice: label left, dark dropdown right (same row height)
            line, lab = self._side_row(master, row, text)
            svar = ctk.StringVar(value=str(sw.get("value")))
            menu = self._option_menu(line, svar, [str(c) for c in sw["choices"]],
                                     lambda v, sw=sw, svar=svar: self._choice_changed(sw, svar, v), width=self.CHOICE_W)
            if not enabled:
                menu.configure(state="disabled")
                lab.configure(text_color=T.TEXT_3)
            menu.grid(row=0, column=1, sticky="e")
            return (svar, menu)
        var = ctk.BooleanVar(value=bool(sw.get("value")))
        w = self._switch_row(master, row, text, var, lambda sw=sw, var=var: self._switch_toggled(sw, var), enabled)
        return (var, w)

    # ------------------------------------------------------------------ About tab
    def _build_about_tab(self, tab: ctk.CTkFrame) -> None:
        r = 0
        self._heading(tab, "Updates", r, first=True)
        r += 1
        line, _lab = self._side_row(tab, r, "Version")
        ctk.CTkLabel(line, text=__version__, font=self._body, text_color=T.TEXT_2, anchor="e", height=28).grid(
            row=0, column=1, sticky="e")
        r += 1
        # Check for updates: the result next to the button (where the user clicked)
        self._check_btn: ctk.CTkButton | None = None
        self._check_msg: ctk.CTkLabel | None = None
        if self._on_check_updates is not None:
            crow = ctk.CTkFrame(tab, fg_color="transparent", height=28)
            crow.grid(row=r, column=0, sticky="ew", pady=(0, 4))
            crow.grid_columnconfigure(0, weight=1)
            self._check_msg = ctk.CTkLabel(crow, text="", anchor="w", height=28, font=self._small,
                                           text_color=T.TEXT_2)
            self._check_msg.grid(row=0, column=0, sticky="w")
            self._check_btn = ctk.CTkButton(
                crow, text=CHECK_LABEL if self._can_check else CHECK_LABEL_SOURCE, width=0, height=28,
                corner_radius=8, font=self._body, fg_color=T.RAISED, hover_color=T.RAISED_HOVER, text_color=T.TEXT,
                text_color_disabled=T.TEXT_3, command=self.check_updates,
                state="normal" if self._can_check else "disabled")
            self._check_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))
            r += 1
        for sw in self._about_switches:
            self._built_rows[str(sw["key"])] = self._setting_row(tab, r, sw)
            r += 1

        self._history_btn: LinkRow | None = None
        if self._on_history is not None:
            self._heading(tab, "Usage", r)
            r += 1
            self._history_btn = LinkRow(tab, HISTORY_LABEL, self.open_history, self._body, self._glyph_font)
            self._history_btn.grid(row=r, column=0, sticky="ew", pady=(0, 4))
            r += 1

        # Endpoint: the running server's own web app (chat, Monitor, its settings) + the API address
        cfg, name = self._cfg, self._engine.name
        root = config_store.server_root(str(cfg.get("endpoint_url") or ""))
        self._dashboard_url = root + "/" if root else ""
        self._api_url = str(cfg.get("endpoint_url") or "")
        self._heading(tab, "Endpoint", r)
        r += 1
        if not self._engine.has_dashboard:
            self._dashboard_url = ""
        live = self._running and bool(self._dashboard_url)
        self._link = ctk.CTkFrame(tab, fg_color=T.SURFACE, corner_radius=8, height=28)
        self._link.grid(row=r, column=0, sticky="ew")
        r += 1
        self._link.grid_columnconfigure(2, weight=1)
        self._link_font = ctk.CTkFont(family=T.ui_family(), size=12)
        self._link_font_hover = ctk.CTkFont(family=T.ui_family(), size=12, underline=True)
        self._link_text = ctk.CTkLabel(self._link, text=f"Open {name} dashboard" if self._engine.has_dashboard
                                       else f"{name} endpoint", font=self._link_font, height=28,
                                       text_color=T.TEXT if live else T.TEXT_3, anchor="w")
        self._link_text.grid(row=0, column=0, padx=(10, 0), sticky="w")
        self._link_ext = ctk.CTkLabel(self._link, text=self.EXT_GLYPH, height=28, width=14, font=self._glyph_font,
                                      text_color=T.TEXT_2 if live else T.TEXT_3)
        self._link_ext.grid(row=0, column=1, padx=(6, 0))
        host = self._dashboard_url.replace("http://", "").rstrip("/")
        self._link_host = ctk.CTkLabel(self._link, text=host if live else "not running", font=self._mono, height=28,
                                       text_color=T.TEXT_2 if live else T.TEXT_3, anchor="e")
        self._link_host.grid(row=0, column=2, padx=(8, 10), sticky="e")
        if live:
            for w in (self._link, self._link_text, self._link_ext, self._link_host):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda _e: self.open_dashboard())
                w.bind("<Enter>", lambda _e: self._link_hover(True))
                w.bind("<Leave>", lambda _e: self._link_hover(False))
        api = ctk.CTkFrame(tab, fg_color="transparent")
        api.grid(row=r, column=0, sticky="ew", pady=(4, 0))
        api.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(api, text=f"API {self._api_url}", font=self._mono, text_color=T.TEXT_2, anchor="w",
                     height=20).grid(row=0, column=0, sticky="w")
        self._copy_btn = ctk.CTkButton(api, text="Copy", width=44, height=20, corner_radius=6, font=self._small,
                                       fg_color="transparent", hover_color=T.RAISED, text_color=T.TEXT_2,
                                       command=self.copy_api)
        self._copy_btn.grid(row=0, column=1, sticky="e")

    # ------------------------------------------------------------------ tabs + size
    def _measure(self) -> None:
        """One fixed height for every tab: the tallest one, with the Advanced fold open and the fit note shown
        (so neither can push anything out of the window). Measured from the laid-out widgets."""
        if self._adv_body is not None:
            self._adv_body.grid(row=self._adv_row, column=0, sticky="ew")
        if self._fit_note is not None:
            self._fit_note.configure(text=str(getattr(self._engine, "gpu_fit_loading", "") or FITS_TEXT))
            self._fit_note.grid(row=self._fit_row, column=0, sticky="ew", pady=(0, 4))
        scale = ctk.ScalingTracker.get_window_scaling(self) or 1.0
        for frame in self.tabs.values():                 # all in the same cell: the cell is as tall as the tallest
            frame.grid(row=2, column=0, sticky="nsew", padx=12)
        self.update_idletasks()
        self.tab_heights = {n: math.ceil(f.winfo_reqheight() / scale) for n, f in self.tabs.items()}
        need = math.ceil(self.winfo_reqheight() / scale)
        for frame in self.tabs.values():
            frame.grid_remove()
        self.logical_height = int(math.ceil(need / 4.0) * 4)       # on the 4 px grid
        if self._fit_note is not None:
            self._fit_note.configure(text="")
            self._fit_note.grid_remove()
        if self._adv_body is not None:
            self.set_advanced(self._needs_advanced(initial=True))

    def show_tab(self, name: str) -> None:
        """Show one tab. Nothing is saved or applied by switching."""
        if name not in self.tabs:
            return
        self.current_tab = name
        for n, frame in self.tabs.items():
            if n == name:
                frame.grid(row=2, column=0, sticky="nsew", padx=12)
            else:
                frame.grid_remove()
            self._tab_buttons[n].configure(fg_color=T.RAISED if n == name else T.SURFACE,
                                           hover_color=T.RAISED if n == name else T.RAISED_HOVER,
                                           text_color=T.TEXT if n == name else T.TEXT_2)
        model = name == TABS[0]
        self._msg.grid_remove()
        self._about_msg.grid_remove()
        (self._msg if model else self._about_msg).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        if model:
            self._close_btn.grid_remove()
            self._cancel_btn.grid(row=1, column=0, sticky="ew", padx=(0, 4))
            self._save_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        else:
            self._cancel_btn.grid_remove()
            self._save_btn.grid_remove()
            self._close_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0))

    # ------------------------------------------------------------------ endpoint
    def _link_hover(self, on: bool) -> None:
        self._link.configure(fg_color=T.RAISED if on else T.SURFACE)
        self._link_text.configure(font=self._link_font_hover if on else self._link_font)

    def open_dashboard(self) -> None:
        """Strata's own web app in the default browser. Only offered while Strata is serving."""
        if not (self._running and self._dashboard_url):
            return
        try:
            webbrowser.open(self._dashboard_url)
        except Exception:  # noqa: BLE001
            log.exception("could not open %s", self._dashboard_url)

    def copy_api(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(self._api_url)
            self._copy_btn.configure(text="Copied")
            later(self, 1500, lambda: self._copy_btn.configure(text="Copy"))
        except Exception:  # noqa: BLE001
            log.debug("copy failed", exc_info=True)

    # ------------------------------------------------------------------ launcher list
    @staticmethod
    def _same(a: str | None, b: str | None) -> bool:
        return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def _label_for(self, path: str) -> str:
        """File name; a second launcher with the same name (another folder) gets its folder appended."""
        name = os.path.basename(path)
        if name in self._paths and not self._same(self._paths[name], path):
            name = f"{name}  ({os.path.basename(os.path.dirname(path)) or os.path.dirname(path)})"
        return name

    def _add_path(self, path: str) -> str:
        for label, known in self._paths.items():
            if self._same(known, path):
                return label
        label = self._label_for(path)
        self._paths[label] = path
        return label

    def _load_launchers(self) -> None:
        if not self._files:                      # program + arguments: check what is there, re-check on edits
            self._command_changed(initial=True)
            self._program.trace_add("write", lambda *_a: self._command_changed())
            self._args.trace_add("write", lambda *_a: self._command_changed())
            if self._port_var is not None:
                self._port_var.trace_add("write", lambda *_a: self._port_changed())
            return
        for p in self._engine.list_launchers(self._cfg):
            self._add_path(p)
        current = None
        if self._initial:
            current = self._add_path(self._initial)
        if not self._paths:
            self._menu.configure(values=[self._no_launchers], state="disabled")
            self._choice.set(self._no_launchers)
            hint = "Using launch_cmd from config.json." if self._has_launch_cmd else "No launcher configured."
            self._show_detail(f"{hint} Browse… to pick a {self._engine.launcher_kind}.", error=False)
            return
        self._menu.configure(values=list(self._paths), state="normal")
        if current is None:
            self._choice.set("Choose a launcher…")
            self._show_detail("Using launch_cmd from config.json." if self._has_launch_cmd
                              else self._engine.launcher_pick_text, error=False)
            return
        self._choice.set(current)
        self._select(self._paths[current])

    def _enable_save(self, enabled: bool) -> None:
        """Primary (off-white) only while it can be pressed; a disabled Save is a quiet raised surface."""
        self._save_btn.configure(state="normal" if enabled else "disabled", fg_color=T.TEXT if enabled else T.RAISED)

    def _show_detail(self, text: str, error: bool) -> None:
        self._detail.configure(text=text, text_color=T.RED_TEXT if error else T.TEXT_2)

    def _on_pick(self, label: str) -> None:
        path = self._paths.get(label)
        if path:
            self._select(path)

    def _select(self, path: str) -> None:
        """Read the chosen launcher file (read only - it is never run) and show what it would launch, or why not."""
        self._info = None
        try:
            info = self._engine.read_launcher(path)
        except LaunchError as exc:
            self._show_detail(str(exc), error=True)
            self._enable_save(False)
            self._msg.configure(text="")
            return
        except Exception as exc:  # noqa: BLE001 - a parser bug must not take the dialog down
            log.exception("parsing %s failed", path)
            self._show_detail(f"Could not read {os.path.basename(path)}: {exc}", error=True)
            self._enable_save(False)
            return
        self._info = info
        self._show_detail(info.summary(), error=False)
        if not self._saving:
            self._enable_save(True)
        changed = not self._same(path, self._initial)
        self._msg.configure(text=f"Applies next start — {self._engine.name} keeps running"
                            if self._running and changed else "",
                            text_color=T.TEXT_2)

    def _command_changed(self, initial: bool = False) -> None:
        """Program + arguments (engines without launcher files): ask the engine whether this would launch; only
        then can it be saved. Nothing is run. Also keeps the friendly controls and the GPU-fit note in step."""
        program = self._program.get().strip().strip('"')
        info = CommandLaunch(program=program, args=tuple(split_args(self._args.get())))
        self._info = None
        self._sync_option_menus(program, info.args)
        if self._port_bad:
            self._enable_save(False)
            self._msg.configure(text=PORT_ERROR, text_color=T.RED_TEXT)
            self._fit_invalid()
            return
        if not program:
            self._enable_save(False)
            self._msg.configure(text="" if initial else "Choose the program to start", text_color=T.TEXT_2)
            self._fit_invalid()
            return
        trial = dict(self._cfg)
        self._engine.apply_launcher(trial, info)
        try:
            self._engine.plan_launch(trial)
        except LaunchError as exc:
            self._enable_save(False)
            self._msg.configure(text=str(exc), text_color=T.RED_TEXT)
            self._fit_invalid()
            return
        except Exception as exc:  # noqa: BLE001 - an engine bug must not take the dialog down
            log.exception("checking the launch command failed")
            self._enable_save(False)
            self._msg.configure(text=f"Could not check the command: {exc}", text_color=T.RED_TEXT)
            self._fit_invalid()
            return
        self._info = info
        changed = not self._engine.same_launch(self._cfg, info)
        if not self._saving:
            self._enable_save(changed)
        self._msg.configure(text=f"Applies next start — {self._engine.name} keeps running"
                            if self._running and changed else "", text_color=T.TEXT_2)
        key = self._fit_key_of(program, info.args)
        if key != self._fit_key:
            self._fit_key = key
            if not initial:
                self._schedule_fit()

    # ---- the friendly controls (each one rewrites the arguments box; typing there updates them)
    @staticmethod
    def _same_file(a: str, b: str) -> bool:
        try:
            return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
        except (TypeError, ValueError):
            return False

    def _call(self, key: str, *a: Any) -> Any:
        fn = self._opts.get(key)
        return fn(*a) if callable(fn) else None

    def _program_now(self) -> str:
        return self._program.get().strip().strip('"')

    def _model_path(self, args: tuple, program: str | None = None) -> str | None:
        """The model file the arguments name (a relative path: from the program's folder, where it starts)."""
        m = self._call("model_of", args)
        if not m:
            return None
        m = str(m)
        prog = self._program_now() if program is None else program
        if not os.path.isabs(m) and prog:
            m = os.path.join(os.path.dirname(prog), m)
        return m

    def _vision_list(self, model: str | None) -> list[tuple[str, str]]:
        if not model or not self._has_vision:
            return []
        key = os.path.normcase(os.path.normpath(model))
        if key not in self._vision_cache:          # one folder listing per model and dialog
            try:
                self._vision_cache[key] = [(str(a), str(b)) for a, b in (self._opts["vision_files"](model) or [])]
            except Exception:  # noqa: BLE001
                log.debug("vision files not listed", exc_info=True)
                self._vision_cache[key] = []
        return self._vision_cache[key]

    def _same_vision(self, listed: str, named: str) -> bool:
        if not os.path.isabs(named):
            prog = self._program_now()
            named = os.path.join(os.path.dirname(prog), named) if prog else named
        return self._same_file(listed, named)

    def _args_now(self) -> tuple:
        return tuple(split_args(self._args.get()))

    def _set_args(self, new: Any) -> None:
        text = join_args([str(a) for a in new])
        if text != self._args.get():
            self._args.set(text)                         # the trace re-checks the command

    def _sync_option_menus(self, program: str, args: tuple) -> None:
        """Show which found build / model, context, vision file and port the boxes name right now."""
        if self._syncing:
            return
        self._syncing = True
        try:
            if self._program_choice is not None:
                title = str(self._opts.get("program_title") or "Program").lower()
                lab = next((la for la, p in self._opt_programs if program and self._same_file(p, program)), None)
                self._program_choice.set(lab or (f"Choose a {title}…" if not program else "Another program (below)"))
            if self._model_choice is not None:
                title = str(self._opts.get("model_title") or "Model").lower()
                cur = self._call("model_of", args)
                lab = next((la for la, p in self._opt_models if cur and self._same_file(p, cur)), None)
                self._model_choice.set(lab or (f"Choose a {title}…" if not cur else "Another model (below)"))
            if self._context_choice is not None:
                tokens = self._call("context_of", args)
                lab = next((la for la, n in self._context_choices if n == tokens), None)
                if lab is None:
                    lab = CONTEXT_CUSTOM.format(tokens=int(tokens)) if tokens else self._context_choices[0][0]
                self._context_choice.set(lab)
            if self._vision_choice is not None and self._vision_menu is not None:
                files = self._vision_list(self._model_path(args, program))
                self._vision_paths = {VISION_ON.format(label=la): p for la, p in files}
                cur = self._call("vision_of", args)
                if not files and not cur:
                    self._vision_menu.configure(values=[VISION_NONE], state="disabled")
                    self._vision_choice.set(VISION_NONE)
                else:
                    self._vision_menu.configure(values=[VISION_OFF, *self._vision_paths],
                                                state="disabled" if self._saving else "normal")
                    if not cur:
                        self._vision_choice.set(VISION_OFF)
                    else:
                        lab = next((la for la, p in self._vision_paths.items() if self._same_vision(p, str(cur))),
                                   None)
                        self._vision_choice.set(lab or VISION_OTHER)
            if self._port_var is not None and not self._port_typing:
                port = self._call("port_of", args)
                self._port_bad = False
                self._port_var.set("" if port is None else str(port))
            self._maybe_expand(program, args)
        except Exception:  # noqa: BLE001 - an engine helper must not take the dialog down
            log.debug("option menus not updated", exc_info=True)
        finally:
            self._syncing = False

    def _needs_advanced(self, initial: bool = False) -> bool:
        """The fold opens by itself when the dropdowns cannot show what will run (or there is nothing to pick)."""
        if self._adv_body is None:
            return False
        if initial and not self._engine.is_configured(self._cfg) and not self._opt_programs and not self._opt_models:
            return True
        return self._unrepresentable(self._program_now(), self._args_now())

    def _unrepresentable(self, program: str, args: tuple) -> bool:
        """True when the program or arguments name something no dropdown can show."""
        try:
            if not self._opt_programs:
                return True                              # no Build row: the program is only in the fold
            if program and not any(self._same_file(p, program) for _l, p in self._opt_programs):
                return True
            cur = self._call("model_of", args)
            if cur and not any(self._same_file(p, str(cur)) for _l, p in self._opt_models):
                return True
            if self._has_vision:
                v = self._call("vision_of", args)
                if v and not any(self._same_vision(p, str(v)) for _l, p in
                                 self._vision_list(self._model_path(args, program))):
                    return True
        except Exception:  # noqa: BLE001
            log.debug("advanced check failed", exc_info=True)
            return True
        return False

    def _maybe_expand(self, program: str, args: tuple) -> None:
        if self._adv_body is not None and not self.advanced_open and self._unrepresentable(program, args):
            self.set_advanced(True)                       # opens by itself, never closes by itself

    def _program_picked(self, label: str) -> None:
        path = next((p for la, p in self._opt_programs if la == label), None)
        if path and not self._saving:
            self._program.set(path)                  # the trace re-checks the command

    def _model_picked(self, label: str) -> None:
        path = next((p for la, p in self._opt_models if la == label), None)
        if not path or self._saving:
            return
        note = ""
        try:
            args = self._args_now()
            if args:
                new = tuple(self._opts["with_model"](args, path))
            else:
                new = tuple(self._opts["default_args"](path))
            old = self._call("vision_of", args) if self._has_vision else None
            if old:                                  # a projector belongs to its model
                base = os.path.basename(str(old)).casefold()
                same = next((p for _l, p in self._vision_list(self._model_path(new)) if
                             os.path.basename(p).casefold() == base), None)
                new = tuple(self._opts["with_vision"](new, same))
                if same is None:
                    note = VISION_DROPPED
            self._set_args(new)
        except Exception:  # noqa: BLE001
            log.exception("could not put the model into the arguments")
            return
        if note and str(self._msg.cget("text_color")) != str(T.RED_TEXT):
            self._msg.configure(text=note, text_color=T.TEXT_2)

    def _context_picked(self, label: str) -> None:
        if self._saving:
            return
        hit = [n for la, n in self._context_choices if la == label]
        if not hit:                                      # the "(custom)" entry: nothing to change
            return
        try:
            self._set_args(self._opts["with_context"](self._args_now(), hit[0]))
        except Exception:  # noqa: BLE001
            log.exception("could not set the context length")

    def _vision_picked(self, label: str) -> None:
        if self._saving:
            return
        if label == VISION_OFF:
            path = None
        elif label in self._vision_paths:
            path = self._vision_paths[label]
        else:
            return
        try:
            self._set_args(self._opts["with_vision"](self._args_now(), path))
        except Exception:  # noqa: BLE001
            log.exception("could not set the vision file")

    def _port_changed(self) -> None:
        if self._syncing or self._port_var is None:
            return
        text = self._port_var.get().strip()
        ok = text.isdigit() and 1 <= int(text) <= 65535
        self._port_typing = True                         # the box keeps what the user typed
        try:
            self._port_bad = not ok
            args = self._args_now()
            if ok and self._call("port_of", args) != int(text):
                self._set_args(self._opts["with_port"](args, int(text)))
            else:
                self._command_changed()
        except Exception:  # noqa: BLE001
            log.exception("could not set the port")
        finally:
            self._port_typing = False

    # ---- the GPU-fit note (one request per pause in the changes; answers to older requests are ignored)
    def _fit_key_of(self, program: str, args: tuple) -> tuple:
        try:
            return (os.path.normcase(program), self._call("model_of", args), self._call("context_of", args),
                    self._call("vision_of", args))
        except Exception:  # noqa: BLE001
            return (program, args)

    def _fit_invalid(self) -> None:
        """Not a launch the engine accepts: no request, no note."""
        self._fit_key = None
        self._cancel_fit()
        self._fit_seq += 1
        self._show_fit(None)

    def _cancel_fit(self) -> None:
        if self._fit_after is not None:
            try:
                self.after_cancel(self._fit_after)
            except Exception:  # noqa: BLE001
                log.debug("after_cancel failed", exc_info=True)
            self._fit_after = None

    def _schedule_fit(self) -> None:
        if self._on_fit_preview is None:
            return
        self._cancel_fit()
        self._fit_seq += 1                               # an answer still on its way is for the old launch
        self._show_fit(None)
        self._fit_after = self.after(FIT_DEBOUNCE_MS, self._run_fit)

    def _run_fit(self) -> None:
        self._fit_after = None
        cb = self._on_fit_preview
        if cb is None or self._info is None or not self._alive():
            return
        self._fit_seq += 1
        seq = self._fit_seq
        self.fit_calls += 1
        def done(text: str | None) -> None:
            self._fit_answer(seq, text)

        try:
            started = bool(cb(self._info, done))
        except Exception:  # noqa: BLE001
            log.exception("GPU-fit preview could not start")
            started = False
        if not started and seq == self._fit_seq:
            self._show_fit(None)

    def _fit_answer(self, seq: int, text: str | None) -> None:
        if seq != self._fit_seq or not self._alive():
            return
        self._show_fit(text)

    def _show_fit(self, text: str | None) -> None:
        note = self._fit_note
        if note is None:
            return
        try:
            if text is None:
                self.fit_state = None
                note.configure(text="")
                note.grid_remove()
                return
            self.fit_state = "warn" if text else "fits"
            note.configure(text=text or FITS_TEXT, text_color=T.AMBER if text else T.TEXT_2)
            note.grid(row=self._fit_row, column=0, sticky="ew", pady=(0, 4))
        except Exception:  # noqa: BLE001 - closing
            log.debug("fit note not shown", exc_info=True)

    def selected_path(self) -> str | None:
        return self._paths.get(self._choice.get())

    def choose(self, path: str) -> None:
        """Select a launcher by path (adds it to the dropdown if needed), or set the program for engines without
        launcher files. Used by Browse… and tests."""
        if not self._files:
            self._program.set(os.path.normpath(path))
            return
        label = self._add_path(os.path.normpath(path))
        self._menu.configure(values=list(self._paths), state="normal")
        self._choice.set(label)
        self._select(self._paths[label])

    def browse(self) -> None:
        if self._saving:
            return
        listed = next(iter(self._paths.values()), "")
        start = os.path.dirname(listed) if listed and os.path.isdir(os.path.dirname(listed)) else ""
        if not start:                            # nothing listed yet: the engine's configured install folder
            guess = str(self._cfg.get("strata_dir") or "") if self._files else ""
            start = guess if os.path.isdir(guess) else ""
        cur = (self.selected_path() or self._initial) if self._files else self._program.get().strip()
        if cur and os.path.isdir(os.path.dirname(cur)):
            start = os.path.dirname(cur)
        path = filedialog.askopenfilename(
            parent=self, initialdir=start or None,
            title=(f"Choose a {self._engine.name} launcher" if self._files
                   else f"Choose the {self._engine.name} program"),
            filetypes=list(self._engine.launcher_filetypes if self._files else self._engine.program_filetypes))
        if path and self._alive():
            self.choose(path)

    # ------------------------------------------------------------------ updates
    def _show_update_link(self, version: str) -> None:
        """The version label becomes "vX · Update to Y" (click: close Settings, the app asks before installing)."""
        if self._on_update is None:
            return
        text = f"v{__version__} · Update to {version}"
        if self._update_link is not None:
            self._update_link.configure(text=text)
            return
        if self._version_label is not None:
            self._version_label.destroy()
            self._version_label = None
        self._update_link = ctk.CTkLabel(self._head, text=text, anchor="e", height=20,
                                         font=ctk.CTkFont(family=T.ui_family(), size=12, underline=True),
                                         text_color=T.ACCENT, cursor="hand2")
        self._update_link.grid(row=0, column=1, sticky="e")
        self._update_link.bind("<Button-1>", lambda _e: self._update_clicked())

    def _show_check(self, key: str, version: str | None = None, color: str = T.TEXT_2) -> None:
        if self._check_msg is not None:
            self._check_msg.configure(text=CHECK_TEXTS[key].format(version=version or ""), text_color=color)

    def check_updates(self) -> None:
        """The button: one check at a time (greyed until its result is in)."""
        cb = self._on_check_updates
        if cb is None or not self._can_check or self._checking or self._check_btn is None or not self._alive():
            return
        self._checking = True
        self._check_btn.configure(state="disabled")
        self._show_check("checking")
        try:
            res = cb(self._check_result)
        except Exception:  # noqa: BLE001
            log.exception("update check could not start")
            res = "error"
        if res in ("started", "joined"):
            return                               # _check_result() follows
        self._checking = False
        if res == "busy":
            self._show_check("busy")             # stays greyed: the app exits for the installer
        elif res == "unavailable":
            self._can_check = False
            self._check_btn.configure(text=CHECK_LABEL_SOURCE)
            if self._check_msg is not None:
                self._check_msg.configure(text="")
        else:
            self._check_btn.configure(state="normal")
            self._show_check("error", color=T.RED_TEXT)

    def _check_result(self, status: str, version: str | None) -> bool:
        """The check's result (Tk thread). False when this dialog is gone: nothing was shown (the app may toast)."""
        if not self._alive() or self._check_btn is None:
            return False
        self._checking = False
        try:
            self._check_btn.configure(state="normal")
            if status == "current":
                self._show_check("current", version)
                if self._version_label is not None:
                    self._version_label.configure(text=f"v{__version__} · up to date")
            elif status == "newer" and version:
                self._show_check("newer", version, color=T.TEXT)
                self._show_update_link(version)
            else:
                self._show_check("error", color=T.RED_TEXT)
        except Exception:  # noqa: BLE001 - closed between the check and the configure
            log.debug("update check result not shown", exc_info=True)
            return False
        return True

    # ------------------------------------------------------------------ protected apps (nested modal)
    def _update_clicked(self) -> None:
        cb = self._on_update
        if cb is None:
            return
        master = self.master
        self.close()                             # the confirmation box belongs to the main window
        master.after(50, cb)

    def _choice_changed(self, sw: dict, svar, value: str) -> None:
        cb = sw.get("on_change")
        prev = str(sw.get("value"))
        try:
            if cb is not None and cb(value) is False:
                svar.set(prev)
                return
            sw["value"] = value
        except Exception:  # noqa: BLE001
            log.exception("setting %s failed", sw.get("key"))
            svar.set(prev)

    def _switch_toggled(self, sw: dict, var) -> None:
        cb = sw.get("on_change")
        if cb is None:
            return
        try:
            res = cb(bool(var.get()))
            if res is False:                     # the change was refused (e.g. the registry write failed)
                var.set(not bool(var.get()))
        except Exception:  # noqa: BLE001
            log.exception("setting %s failed", sw.get("key"))
            var.set(not bool(var.get()))

    def _reopen_toggled(self) -> None:
        if self._on_reopen_changed is not None:
            try:
                self._on_reopen_changed(bool(self._reopen_var.get()))
            except Exception:  # noqa: BLE001
                log.exception("reopen setting callback failed")

    def open_protected(self) -> None:
        if self._saving:
            return
        if self._child is not None and self._child_alive():
            self._child.lift()
            self._child.focus_force()
            return
        try:
            self.grab_release()                            # the whitelist modal takes the grab
        except Exception:  # noqa: BLE001
            log.debug("grab_release failed", exc_info=True)
        self._child = WhitelistModal(self._app, self._detector, on_saved=self._whitelist_saved)
        self._child.bind("<Destroy>", self._on_child_destroy, add="+")

    def open_history(self) -> None:
        """The usage history window, as a child like the protected-apps list (it takes the grab)."""
        if self._saving or self._on_history is None:
            return
        if self._child is not None and self._child_alive():
            self._child.lift()
            self._child.focus_force()
            return
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            log.debug("grab_release failed", exc_info=True)
        try:
            child = self._on_history()
        except Exception:  # noqa: BLE001
            log.exception("usage history could not be opened")
            child = None
        if child is None:
            self._about_msg.configure(text="Usage history is not available", text_color=T.TEXT_2)
            later(self, 50, self._regrab)        # the grab was released for a child that never came
            return
        self._child = child
        child.bind("<Destroy>", self._on_child_destroy, add="+")

    def _engine_picked(self, shown: str) -> None:
        """Engine row: hand the engine id to the app; show its note (when the change applies)."""
        eid = next((i for i, n in self._engine_choices if n == shown), None)
        if eid is None or self._on_engine_change is None:
            return
        try:
            note = self._on_engine_change(eid)
        except Exception:  # noqa: BLE001
            log.exception("engine change failed")
            note = "The engine could not be changed"
        try:
            if not self.winfo_exists():
                return
            if note is None and self._engine_var is not None:    # nothing changes: show the engine in use again
                current = next((n for i, n in self._engine_choices if i == getattr(self._engine, "id", None)), None)
                if current is not None:
                    self._engine_var.set(current)
            self._msg.configure(text=str(note or ""), text_color=T.TEXT_2)
        except Exception:  # noqa: BLE001 - the app may have closed Settings from the callback
            log.debug("engine row update failed", exc_info=True)

    def _child_alive(self) -> bool:
        try:
            return self._child is not None and bool(self._child.winfo_exists())
        except Exception:  # noqa: BLE001
            return False

    def _whitelist_saved(self) -> None:
        if self._on_whitelist_saved is not None:
            try:
                self._on_whitelist_saved()
            except Exception:  # noqa: BLE001
                log.exception("on_whitelist_saved callback failed")

    def _on_child_destroy(self, event) -> None:
        if event.widget is not self._child:               # <Destroy> also arrives for every child widget
            return
        self._child = None
        if self._alive():
            later(self, 50, self._regrab)

    def _regrab(self) -> None:
        try:
            if self._alive() and self._child is None:
                self.lift()
                self.focus_force()
                self.grab_set()
        except Exception:  # noqa: BLE001
            log.debug("regrab failed", exc_info=True)

    # ------------------------------------------------------------------ save / close
    def _alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.winfo_exists())
        except Exception:  # noqa: BLE001
            return False

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for b in (self._cancel_btn, self._close_btn, self._browse_btn):
            b.configure(state=state)
        self._protected_btn.set_state(state)
        if self._history_btn is not None:
            self._history_btn.set_state(state)
        self._enable_save(not busy and self._info is not None)
        if self._paths:
            self._menu.configure(state=state)
        widgets: list[Any] = [self._program_menu, self._model_menu, self._context_menu, self._vision_menu,
                              self._port_entry]
        if not self._files:
            widgets += [self._program_entry, self._args_entry]
        for w in widgets:
            if w is not None:
                w.configure(state=state)
        if not busy and not self._files:
            self._sync_option_menus(self._program_now(), self._args_now())   # e.g. a vision list with no files

    def save(self) -> None:
        if self._saving or self._info is None or self._port_bad:
            return
        self._saving = True
        self._set_busy(True)
        self._msg.configure(text="Saving…", text_color=T.TEXT_2)
        try:
            self._on_save(self._info, self._save_done)
        except Exception as exc:  # noqa: BLE001
            log.exception("saving the launcher failed")
            self._save_done(f"Save failed: {exc}")

    def _save_done(self, error: str | None) -> None:
        self._saving = False
        if error:
            if self._alive():
                self._msg.configure(text=error, text_color=T.RED_TEXT)
                self._set_busy(False)
            return
        self.close()

    def _close_armed(self) -> None:
        self._close_pending = False
        self.close()

    def destroy(self) -> None:
        """Also runs when the app is destroyed under the dialog. The app cancels every pending after-script first
        (main.destroy), which deletes their Tcl commands behind tkinter's back; forget those names so tkinter does not
        try to delete them a second time (TclError "can't delete Tcl command")."""
        if not self._closed:
            self._closed = True
            self._cancel_fit()
            release_modal(self)
        try:
            cmds = getattr(self, "_tclCommands", None)
            if cmds:
                self._tclCommands = [n for n in cmds if self.tk.call("info", "commands", n)]
        except Exception:  # noqa: BLE001
            log.debug("command list not pruned", exc_info=True)
        super().destroy()

    def close(self) -> None:
        """Cancel / Close / X / Escape (and a finished save). In the first ARM_S seconds the close waits until the
        window is armed instead of being dropped (closing a CTkToplevel that is still being styled can crash Tk)."""
        # X / Escape during a save would drop the save's error (the user would think it saved): wait for it.
        if self._saving or getattr(self._child, "_saving", False):
            return
        if self._closed:
            return
        wait = self._armed_at - time.monotonic()
        if wait > 0:
            if not self._close_pending:
                self._close_pending = True
                later(self, int(wait * 1000) + 20, self._close_armed)
            return
        self._closed = True
        self._cancel_fit()
        self._fit_seq += 1                               # a late GPU-fit answer touches nothing
        child = self._child
        self._child = None
        if child is not None:
            try:
                child.close()
            except Exception:  # noqa: BLE001
                log.debug("closing the nested whitelist modal failed", exc_info=True)
        release_modal(self)
        try:
            self.destroy()
        except Exception:
            log.exception("SettingsDialog destroy failed")
        if self._on_closed is not None:
            try:
                self._on_closed()
            except Exception:
                log.exception("SettingsDialog on_closed callback failed")
