"""SettingsDialog: choose the Strata launcher (.bat) and reach the protected-apps list."""
from __future__ import annotations

import logging
import os
import webbrowser
from tkinter import filedialog
from typing import Callable

import customtkinter as ctk

import config_store
import launcher_bat
import theme as T
from launcher_bat import LauncherError, LauncherInfo
from sweep_dialog import apply_modal, center_over, later, release_modal, style_dialog
from version import __version__
from whitelist_modal import WhitelistModal

log = logging.getLogger("agent_start")

NO_LAUNCHERS = "No run-*.bat found"


class SettingsDialog(ctk.CTkToplevel):
    WIDTH = 340
    HEIGHT = 362                                 # with one switch; each further switch adds SWITCH_STEP
    SWITCH_STEP = 34                             # switch 24 px + 10 px gap
    EXT_GLYPH = "\uE8A7"   # Segoe Fluent Icons "Open in new window"

    def __init__(self, master, cfg: dict, detector,
                 on_save: Callable[[LauncherInfo, Callable[[str | None], None]], None],
                 on_closed: Callable[[], None] | None = None,
                 current_bat: str | None = None, running: bool = False,
                 on_whitelist_saved: Callable[[], None] | None = None,
                 reopen_after: bool | None = None,
                 on_reopen_changed: Callable[[bool], None] | None = None,
                 update_version: str | None = None,
                 on_update: Callable[[], None] | None = None,
                 switches: list[dict] | None = None) -> None:
        """switches: more on/off settings under the reopen switch, applied at once (like it):
        {"key", "label", "value": bool, "on_change": callable(bool), "enabled": bool = True}.

        on_save(info, done) persists the launcher and calls done(error | None) on the Tk thread.

        current_bat overrides the configured launcher (a saved change that is waiting for the next start);
        running=True adds the "applies next start" hint when another launcher is picked.
        """
        super().__init__(master, fg_color=T.BG)
        style_dialog(self)
        self._app = master
        self._detector = detector
        self._on_save = on_save
        self._on_closed = on_closed
        self._on_whitelist_saved = on_whitelist_saved
        self._on_reopen_changed = on_reopen_changed
        self._on_update = on_update
        self._running = bool(running)
        self._closed = False
        self._saving = False
        self._child: WhitelistModal | None = None
        self._info: LauncherInfo | None = None
        self._paths: dict[str, str] = {}                 # dropdown label -> absolute .bat path
        self._strata_dir = str(cfg.get("strata_dir") or "")
        self._initial = current_bat or launcher_bat.configured_bat(cfg)
        self._has_launch_cmd = bool(cfg.get("launch_cmd"))

        self.title("Settings")
        self.resizable(False, False)
        self._extra_switches = [sw for sw in (switches or []) if isinstance(sw, dict) and sw.get("key")]
        center_over(self, master, self.WIDTH, self.HEIGHT + self.SWITCH_STEP * len(self._extra_switches))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.grid_columnconfigure(0, weight=1)

        small = ctk.CTkFont(family=T.ui_family(), size=11)
        body = ctk.CTkFont(family=T.ui_family(), size=12)
        mono = ctk.CTkFont(family=T.mono_family(), size=11)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Settings", anchor="w", height=20,
                     font=ctk.CTkFont(family=T.ui_family(), size=14, weight="bold")).grid(row=0, column=0, sticky="w")
        if update_version and on_update is not None:     # a newer release: the version label becomes the way in
            self._update_link = ctk.CTkLabel(head, text=f"v{__version__} · Update to {update_version}", anchor="e",
                                             height=20, font=ctk.CTkFont(family=T.ui_family(), size=12,
                                                                         underline=True),
                                             text_color=T.ACCENT, cursor="hand2")
            self._update_link.grid(row=0, column=1, sticky="e")
            self._update_link.bind("<Button-1>", lambda _e: self._update_clicked())
        else:
            self._update_link = None
            ctk.CTkLabel(head, text=f"v{__version__}", anchor="e", height=20, font=mono,
                         text_color=T.TEXT_3).grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(self, text="Strata launcher", anchor="w", text_color=T.TEXT_2, font=small,
                     height=16).grid(row=1, column=0, sticky="ew", padx=12, pady=(8, 4))

        pick = ctk.CTkFrame(self, fg_color="transparent")
        pick.grid(row=2, column=0, sticky="ew", padx=12)
        pick.grid_columnconfigure(0, weight=1)
        self._choice = ctk.StringVar(value=NO_LAUNCHERS)
        self._menu = ctk.CTkOptionMenu(
            pick, variable=self._choice, values=[NO_LAUNCHERS], command=self._on_pick, height=28, corner_radius=8,
            dynamic_resizing=False, font=body, dropdown_font=body, fg_color=T.RAISED, button_color=T.RAISED,
            button_hover_color=T.RAISED_HOVER, text_color=T.TEXT, text_color_disabled=T.TEXT_3,
            dropdown_fg_color=T.SURFACE, dropdown_hover_color=T.RAISED, dropdown_text_color=T.TEXT)
        self._menu.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._browse_btn = ctk.CTkButton(pick, text="Browse…", width=76, height=28, corner_radius=8, font=body,
                                         fg_color=T.RAISED, hover_color=T.RAISED_HOVER, text_color=T.TEXT,
                                         command=self.browse)
        self._browse_btn.grid(row=0, column=1)

        # read-only: what the chosen .bat will run (model · config · port) or why it cannot be used
        self._detail = ctk.CTkLabel(self, text="", anchor="nw", justify="left", font=mono, text_color=T.TEXT_2,
                                    wraplength=self.WIDTH - 24, height=30)
        self._detail.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 0))

        # ---- Endpoint: the running server's own web app (chat, Monitor, its settings) + the API address
        root = config_store.server_root(str(cfg.get("endpoint_url") or ""))
        self._dashboard_url = root + "/" if root else ""
        self._api_url = str(cfg.get("endpoint_url") or "")
        ctk.CTkLabel(self, text="Endpoint", anchor="w", text_color=T.TEXT_2, font=small, height=16).grid(
            row=4, column=0, sticky="ew", padx=12, pady=(2, 4))
        live = self._running and bool(self._dashboard_url)
        self._link = ctk.CTkFrame(self, fg_color=T.SURFACE, corner_radius=8, height=30)
        self._link.grid(row=5, column=0, sticky="ew", padx=12)
        self._link.grid_columnconfigure(2, weight=1)
        self._link_font = ctk.CTkFont(family=T.ui_family(), size=12)
        self._link_font_hover = ctk.CTkFont(family=T.ui_family(), size=12, underline=True)
        self._link_text = ctk.CTkLabel(self._link, text="Open Strata dashboard", font=self._link_font, height=28,
                                       text_color=T.TEXT if live else T.TEXT_3, anchor="w")
        self._link_text.grid(row=0, column=0, padx=(10, 0), sticky="w")
        self._link_ext = ctk.CTkLabel(self._link, text=self.EXT_GLYPH, height=28, width=14,
                                      font=ctk.CTkFont(family=T.icon_family(), size=10),
                                      text_color=T.TEXT_2 if live else T.TEXT_3)
        self._link_ext.grid(row=0, column=1, padx=(6, 0))
        host = self._dashboard_url.replace("http://", "").rstrip("/")
        self._link_host = ctk.CTkLabel(self._link, text=host if live else "not running", font=mono, height=28,
                                       text_color=T.TEXT_2 if live else T.TEXT_3, anchor="e")
        self._link_host.grid(row=0, column=2, padx=(8, 10), sticky="e")
        if live:
            for w in (self._link, self._link_text, self._link_ext, self._link_host):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda _e: self.open_dashboard())
                w.bind("<Enter>", lambda _e: self._link_hover(True))
                w.bind("<Leave>", lambda _e: self._link_hover(False))

        api = ctk.CTkFrame(self, fg_color="transparent")
        api.grid(row=6, column=0, sticky="ew", padx=12, pady=(4, 0))
        api.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(api, text=f"API {self._api_url}", font=mono, text_color=T.TEXT_2, anchor="w", height=18).grid(
            row=0, column=0, sticky="w")
        self._copy_btn = ctk.CTkButton(api, text="Copy", width=44, height=18, corner_radius=6, font=small,
                                       fg_color="transparent", hover_color=T.RAISED, text_color=T.TEXT_2,
                                       command=self.copy_api)
        self._copy_btn.grid(row=0, column=1, sticky="e")

        ctk.CTkFrame(self, height=1, fg_color=T.HAIRLINE, bg_color=T.HAIRLINE, corner_radius=0).grid(
            row=7, column=0, sticky="ew", padx=12, pady=(10, 10))

        self._protected_btn = ctk.CTkButton(self, text="Protected apps…", height=30, corner_radius=8, font=body,
                                            fg_color=T.RAISED, hover_color=T.RAISED_HOVER, text_color=T.TEXT,
                                            anchor="w", command=self.open_protected)
        self._protected_btn.grid(row=8, column=0, sticky="ew", padx=12)

        # Reopen the apps closed for Strata after Turn off (also offered in the start dialog). Applies at once.
        self._reopen_var = ctk.BooleanVar(value=bool(reopen_after))
        self._reopen_switch = ctk.CTkSwitch(self, text="Reopen closed apps after Turn off", variable=self._reopen_var,
                                            font=body, text_color=T.TEXT, progress_color=T.ACCENT,
                                            button_color=T.TEXT, button_hover_color=T.PRIMARY_HOVER,
                                            fg_color=T.RAISED, switch_width=34, switch_height=18,
                                            command=self._reopen_toggled)
        self._reopen_switch.grid(row=9, column=0, sticky="w", padx=12, pady=(10, 0))
        # further switches (tray, updates, start with Windows, game alert): one frame in row 9b, same look
        self.switches: dict[str, tuple] = {"reopen": (self._reopen_var, self._reopen_switch)}
        if self._extra_switches:
            box = ctk.CTkFrame(self, fg_color="transparent")
            box.grid(row=10, column=0, sticky="ew", padx=12)
            for i, sw in enumerate(self._extra_switches):
                var = ctk.BooleanVar(value=bool(sw.get("value")))
                w = ctk.CTkSwitch(box, text=str(sw.get("label")), variable=var, font=body, text_color=T.TEXT,
                                  text_color_disabled=T.TEXT_3, progress_color=T.ACCENT, button_color=T.TEXT,
                                  button_hover_color=T.PRIMARY_HOVER, fg_color=T.RAISED, switch_width=34,
                                  switch_height=18, command=lambda sw=sw, var=var: self._switch_toggled(sw, var))
                if not sw.get("enabled", True):
                    w.configure(state="disabled")
                w.grid(row=i, column=0, sticky="w", pady=(10, 0))
                self.switches[str(sw["key"])] = (var, w)

        self._msg = ctk.CTkLabel(self, text="", anchor="w", text_color=T.TEXT_2, font=small, height=16)
        self._msg.grid(row=11, column=0, sticky="ew", padx=12, pady=(8, 2))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=12, column=0, sticky="ew", padx=12, pady=(0, 12))
        btns.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self._cancel_btn = ctk.CTkButton(btns, text="Cancel", height=30, fg_color="transparent", border_width=1,
                                         border_color=T.RAISED_HOVER, hover_color=T.RAISED, command=self.close)
        self._cancel_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._save_btn = ctk.CTkButton(btns, text="Save", height=30, fg_color=T.RAISED, hover_color=T.PRIMARY_HOVER,
                                       text_color=T.ON_PRIMARY, text_color_disabled=T.TEXT_3, command=self.save,
                                       state="disabled")
        self._save_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        self._load_launchers()
        apply_modal(self, master)

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
        for p in launcher_bat.list_launchers(self._strata_dir):
            self._add_path(p)
        current = None
        if self._initial:
            current = self._add_path(self._initial)
        if not self._paths:
            self._menu.configure(values=[NO_LAUNCHERS], state="disabled")
            self._choice.set(NO_LAUNCHERS)
            hint = "Using launch_cmd from config.json." if self._has_launch_cmd else "No launcher configured."
            self._show_detail(f"{hint} Browse… to pick a run-*.bat.", error=False)
            return
        self._menu.configure(values=list(self._paths), state="normal")
        if current is None:
            self._choice.set("Choose a launcher…")
            self._show_detail("Using launch_cmd from config.json." if self._has_launch_cmd
                              else "Pick the .bat that starts Strata.", error=False)
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
        """Parse the chosen .bat (read only - it is never run) and show what it would launch, or why not."""
        self._info = None
        try:
            info = launcher_bat.parse_bat(path)
            problem = launcher_bat.missing_files(info)
            if problem:
                raise LauncherError(problem)
        except LauncherError as exc:
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
        self._msg.configure(text="Applies next start — Strata keeps running" if self._running and changed else "",
                            text_color=T.TEXT_2)

    def selected_path(self) -> str | None:
        return self._paths.get(self._choice.get())

    def choose(self, path: str) -> None:
        """Select a .bat by path (adds it to the dropdown if needed). Used by Browse… and tests."""
        label = self._add_path(os.path.normpath(path))
        self._menu.configure(values=list(self._paths), state="normal")
        self._choice.set(label)
        self._select(self._paths[label])

    def browse(self) -> None:
        if self._saving:
            return
        start = self._strata_dir if os.path.isdir(self._strata_dir) else ""
        cur = self.selected_path() or self._initial
        if cur and os.path.isdir(os.path.dirname(cur)):
            start = os.path.dirname(cur)
        path = filedialog.askopenfilename(
            parent=self, title="Choose a Strata launcher", initialdir=start or None,
            filetypes=[("Batch files", "*.bat *.cmd"), ("All files", "*.*")])
        if path and self._alive():
            self.choose(path)

    # ------------------------------------------------------------------ protected apps (nested modal)
    def _update_clicked(self) -> None:
        cb = self._on_update
        if cb is None:
            return
        master = self.master
        self.close()                             # the confirmation box belongs to the main window
        master.after(50, cb)

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
        for b in (self._cancel_btn, self._browse_btn, self._protected_btn):
            b.configure(state=state)
        self._enable_save(not busy and self._info is not None)
        if self._paths:
            self._menu.configure(state=state)

    def save(self) -> None:
        if self._saving or self._info is None:
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

    def close(self) -> None:
        # X / Escape during a save would drop the save's error (the user would think it saved): wait for it.
        if self._saving or getattr(self._child, "_saving", False):
            return
        if self._closed:
            return
        self._closed = True
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
