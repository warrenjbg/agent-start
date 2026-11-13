"""The app's side of the tray icon, moved verbatim out of main.AgentStartApp (class AppTray, a mixin).

AppTray starts the icon, reacts to it (open, turn on/off, switch model, icon lost) and keeps its tooltip, button and
model list in step with the window. Its methods are the old AgentStartApp methods, character for character;
main.AgentStartApp inherits them (class AgentStartApp(AppView, AppTray, ctk.CTk)), so `self` is always the app.
tray.py stays the icon itself; `import tray as tray_mod` stays inside _start_tray (the tests put a stand-in in place
before the app starts the tray).
"""
from __future__ import annotations

import logging
import time

import config_store
from app_consts import ACTIVE, APP_TITLE, ERROR, IDLE

log = logging.getLogger("agent_start")


class AppTray:
    """The app's side of the tray icon, moved verbatim out of main.AgentStartApp.

    A mixin: it only works as a base of AgentStartApp (class AgentStartApp(AppView, AppTray, ctk.CTk)). It has no
    state and no __init__ of its own; every attribute it uses is created in AgentStartApp.__init__ or in _build_ui.
    """

    # ------------------------------------------------------------------ system tray
    def _start_tray(self) -> None:
        if self.tray is not None or self._no_tray:
            return
        try:
            import tray as tray_mod
            icon = str(config_store.APP_DIR / "assets" / "app.png")
            post = self.post                       # tray callbacks run on the tray thread: marshal to Tk
            t = tray_mod.TrayIcon(APP_TITLE, icon, on_open=lambda: post(self.show_window),
                                  on_toggle=lambda: post(self._tray_toggle), on_quit=lambda: post(self.quit_app),
                                  on_lost=lambda: post(self._tray_lost),
                                  on_switch=lambda key: post(self._tray_switch, key))
            self.tray = t                          # X keeps quitting until the icon is really there (is_running)
            t.start_async(lambda ok: post(self._tray_started, t, ok))
        except Exception:  # noqa: BLE001
            self.tray = None
            log.exception("tray start failed; the window X quits instead")
            self._ensure_reachable()

    def _tray_started(self, t, ok: bool) -> None:
        if self._destroyed or self.tray is not t:
            return
        if not ok:
            self.tray = None
            if self._tray_retry_until == 0.0:      # Explorer may still be starting (logon): retry for 2 min
                self._tray_retry_until = time.monotonic() + 120.0
            log.warning("tray icon could not be added%s", "; retrying" if time.monotonic() < self._tray_retry_until
                        else "; the window X quits instead")
            self._ensure_reachable()             # meanwhile: minimised (started with Windows) or shown
            return
        self._tray_last = None
        self._tray_models_last = None
        self._sync_tray()

    def _ensure_reachable(self) -> None:
        """No tray icon: a window hidden to the tray must come back. Started with Windows: minimised to the taskbar,
        else shown."""
        try:
            if self._destroyed or self.state() != "withdrawn":
                return
            if self._started_hidden:
                self.deiconify()
                self.iconify()
            else:
                self.show_window()
        except Exception:  # noqa: BLE001
            log.debug("ensure_reachable failed", exc_info=True)

    def _tray_lost(self) -> None:
        """The icon is gone (Explorer did not come back): the tray is no way back to a hidden window - show it."""
        log.warning("tray icon lost; showing the window")
        self.show_window()

    def _tray_toggle(self) -> None:
        self.show_window()                       # Turn on may open the start dialog; Turn off may ask first
        if self._box_open or self._dialog_open():
            return                               # a question is up: answer that first (the window is in front)
        self.on_toggle()

    def _sync_tray(self) -> None:
        t = self.tray
        if t is None or self._destroyed:
            return
        try:
            led = self.led.cget("fg_color")
            tip = f"{APP_TITLE} · {self.pill.cget('text')}" + (f" · {self._summary_text}" if self._summary_text else "")
            label = self.toggle.cget("text")
            enabled = str(self.toggle.cget("state")) == "normal" and not label.endswith("…")
            key = (led, tip, label, enabled)
            if key != self._tray_last:
                self._tray_last = key
                t.set_status(led if isinstance(led, str) else None, tip, label, enabled)
            models = [(lbl.replace("  ✓", ""), path) for lbl, path in self._launchers]
            cur = next((path for lbl, path in self._launchers if lbl.endswith("✓")), None)
            mkey = (tuple(models), cur, self.state_name in (IDLE, ERROR, ACTIVE) and not self._box_open)
            if mkey != getattr(self, "_tray_models_last", None) and hasattr(t, "set_models"):
                self._tray_models_last = mkey
                t.set_models(models if len(models) >= 2 else None, cur, mkey[2])
        except Exception:  # noqa: BLE001
            log.debug("tray sync failed", exc_info=True)

    def _tray_switch(self, bat_path: str) -> None:
        """Tray menu > Switch model: the same as the ⇄ menu (asks first while a reply runs)."""
        self.show_window()
        if self._box_open or self._dialog_open():
            return
        self.switch_model(bat_path)
