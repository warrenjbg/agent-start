"""Behaviour-level helpers for the GUI tests: what the user SEES in the Agent Start window and what they can DO.

Point UI checks at these instead of the app's private attributes (``app._xxx``), so a refactor that changes no
behaviour (the UpdateController split, the engine interface) breaks at most one helper here, not hundreds of checks.

IMPORT ORDER (important)
    ``import test_ui_smoke`` FIRST, then ``import ui_helpers``. test_ui_smoke redirects the data dir
    (AGENT_START_HOME) before ``config_store`` is imported and fakes reopen.launch / webbrowser.open. This module
    imports nothing of the app at import time (only customtkinter); app modules (``main``, ``busy_dialog``,
    ``settings_dialog``) and test_ui_smoke's pump helpers are looked up lazily, and a helper that needs them raises a
    clear error if test_ui_smoke has not been imported yet.

WIDGETS EACH HELPER RELIES ON (the only coupling to the app's layout - a rename breaks the helper named here)
    app attribute / lookup              helpers
    ----------------------------------  ---------------------------------------------------------------------------
    app.pill (CTkLabel)                 status_title, click_status_line
    app.led (CTkFrame)                  status_led
    app.summary (CTkLabel)              status_summary, status_text
    app.preview (CTkLabel)              status_detail, detail_visible, detail_is_link, status_text, idle_offer_text,
                                        click_detail_line, click_idle_offer
    app.state_name (public str)         idle_offer_text (offer = clickable detail line while IDLE)
    app.elapsed (CTkLabel)              elapsed_text
    app.progress (CTkProgressBar)       progress_visible, progress_value
    app.act_track / app.act_fill        activity_bar (2 px bar under the status row; tkinter Frames, place())
    app.toggle (CTkButton)              main_button_text, main_button_enabled, click_main_button
    app.pending_box / pending_text /    pending_line_visible, pending_line_text, click_pending_cancel
      pending_cancel
    app.gear (CTkButton)                gear_enabled, open_settings
    app.switch_btn (CTkButton)          switch_button_visible, switch_button_enabled, model_menu_entries, pick_model
    the DropdownMenu child of app       model_menu_entries, pick_model (found by walking app.winfo_children())
    app.mem_card, app.row_vram,         panel_visible, panel_card, pin_glyph, panel_pinned, pinned_panels, click_pin,
      app.row_decode, app.row_prompt    hover_panel (the pin = the only CTkButton inside each panel)
    app.pin_card / app.more_card        panel_card
    app.chevron (CTkButton)             more_bar_visible, click_more_bar
    app.dialog (public)                 open_dialog
    app.geometry() (CTk, logical px)    window_height, window_width
    SettingsDialog: walked by text      settings_switch, toggle_settings_switch, settings_option,
                                        choose_settings_option, settings_launcher, settings_launcher_detail,
                                        settings_message, settings_button, settings_button_note,
                                        click_settings_button,
                                        version_label_text, update_link_text, click_update_link, close_settings
    SettingsDialog tabs: the tab bar's  settings_tabs, settings_tab, show_settings_tab, settings_tab_texts,
      CTkButtons by text; dlg.tabs      settings_tab_of, settings_tab_req_heights
      (name -> frame); LinkRow rows     settings_button / click_settings_button also find LinkRow rows
                                        ('Protected apps', 'Usage history'; .text / .enabled / .invoke())
    any dialog: CTkButton by text       dialog_buttons, click_dialog_button

CTk internals used (customtkinter, not the app): ``CTkLabel._label`` (a CTkLabel click must be generated on its inner
tkinter.Label), ``CTkOptionMenu._dropdown_callback`` (the dropdown's own pick path; the real dropdown is a blocking
native menu), ``CTkButton.invoke`` / ``CTkSwitch.toggle`` (both respect the disabled state, like a real click).

Never: keyboard focus, screenshots, sleeps beyond the app's own guards (TOGGLE_DEBOUNCE_S, busy_dialog.ARM_S).
"""
from __future__ import annotations

import re
import sys
import time
import tkinter
import weakref
from typing import Any, Callable, Iterator

import customtkinter as ctk

# ---------------------------------------------------------------------------------------------------- plumbing
_FAKE_METRICS: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()
_LAST_TOGGLE: "weakref.WeakKeyDictionary[Any, float]" = weakref.WeakKeyDictionary()


def _u():
    """test_ui_smoke (stubs + pump helpers); it must already be imported - see IMPORT ORDER."""
    mod = sys.modules.get("test_ui_smoke") or sys.modules.get("tests.test_ui_smoke")
    if mod is None:
        raise RuntimeError("import test_ui_smoke before using ui_helpers (it redirects the data dir first)")
    return mod


def _main():
    """The app module, imported lazily (after test_ui_smoke redirected the data dir)."""
    _u()
    import main
    return main


def pump(app, seconds: float = 0.1) -> None:
    """test_ui_smoke.pump: run the Tk loop for `seconds`."""
    _u().pump(app, seconds)


def pump_until(app, cond: Callable[[], bool], timeout: float = 3.0) -> bool:
    """test_ui_smoke.pump_until: run the Tk loop until cond() or timeout."""
    return _u().pump_until(app, cond, timeout)


def _walk(w) -> Iterator[Any]:
    """`w` and every descendant widget (depth first, creation order)."""
    yield w
    try:
        kids = w.winfo_children()
    except tkinter.TclError:
        return
    for c in kids:
        yield from _walk(c)


def _text(w) -> str:
    try:
        return str(w.cget("text"))
    except (tkinter.TclError, ValueError, AttributeError):
        return ""


def _enabled(w) -> bool:
    return str(w.cget("state")) != "disabled"


def _click_label(label) -> None:
    label._label.event_generate("<Button-1>")      # CTkLabel binds clicks on its inner tkinter.Label


def _find(root, cls, text: str | None = None, prefix: bool = True):
    """First descendant of type `cls` whose text equals `text` (else starts with it, if prefix). None if absent."""
    cands = [w for w in _walk(root) if isinstance(w, cls)]
    if text is None:
        return cands[0] if cands else None
    for w in cands:
        if _text(w) == text:
            return w
    if prefix:
        for w in cands:
            if _text(w).startswith(text):
                return w
    return None


def is_closed(app) -> bool:
    """The window is gone (destroyed / exited)."""
    try:
        return not bool(app.winfo_exists())
    except tkinter.TclError:
        return True


# ---------------------------------------------------------------------------------------------------- app factory
def make_app(cfg: dict | None = None, topmost: bool = True):
    """test_ui_smoke.new_app twin -> (app, det, ep); also remembers the FakeMetrics so fake_metrics(app) needs no
    private attribute."""
    U = _u()
    det, ep, fm = U.StubDetector(), U.StubEndpoint(), U.FakeMetrics()
    app = _main().AgentStartApp(detector=det, endpoint=ep, config=dict(cfg or U.CFG), metrics_fetcher=fm,
                                vram_sampler=U.FakeVram())
    _FAKE_METRICS[app] = fm
    if topmost:
        app.attributes("-topmost", True)
    pump(app, 0.4)
    return app, det, ep


def fake_metrics(app):
    """The FakeMetrics the app polls (test hook). Apps from make_app: no private read; from U.new_app: the one
    remaining read of app._fetch_metrics lives here."""
    fm = _FAKE_METRICS.get(app)
    return fm if fm is not None else app._fetch_metrics


# ---------------------------------------------------------------------------------------------------- status area
def status_title(app) -> str:
    """The bold state word on the status line ('Idle', 'Starting', 'Serving', 'Generating', ...)."""
    return _text(app.pill)


def status_led(app) -> str:
    """The status LED colour."""
    return str(app.led.cget("fg_color"))


def status_summary(app) -> str:
    """The short text next to the state word (as drawn, possibly ellipsized)."""
    return _text(app.summary)


def status_detail(app) -> str:
    """The detail line's text (whether shown or not - see detail_visible)."""
    return _text(app.preview)


def detail_visible(app) -> bool:
    """The detail line under the status line is laid out."""
    return app.preview.winfo_manager() == "grid"


def detail_is_link(app) -> bool:
    """The detail line is a clickable action (underlined, hand cursor): offers, error log link."""
    return bool(status_detail(app)) and str(app.preview.cget("cursor")) == "hand2"


def status_text(app) -> str:
    """Everything the status area says: summary, then the detail line (same as test_ui_smoke.status)."""
    head, detail = status_summary(app), status_detail(app)
    return head + ("\n" + detail if detail else "") if head else detail


note_text = status_text
"""Notes ('Stopped · freed 2 GB', 'Start cancelled', ...) have no widget of their own: they ARE the status area."""


def click_status_line(app) -> None:
    """Click the status line (opens / closes the optional detail line)."""
    _click_label(app.pill)


def click_detail_line(app) -> None:
    """Click the detail line (an offer, or the error-log link)."""
    _click_label(app.preview)


def idle_offer_text(app) -> str:
    """While IDLE: the clickable offer line ('Update to X available · click to install', 'Reopen ...',
    '<dir> · click to set up' under 'Strata found'); '' when there is none."""
    return status_detail(app) if app.state_name == "IDLE" and detail_is_link(app) else ""


def click_idle_offer(app) -> None:
    """Click the idle offer line."""
    click_detail_line(app)


def elapsed_text(app) -> str:
    """The timer at the right of the status line (load time while starting)."""
    return _text(app.elapsed)


def progress_visible(app) -> bool:
    """The load progress bar is shown."""
    return app.progress.winfo_manager() == "grid"


def progress_value(app) -> float:
    """The load progress bar's fraction (0..1)."""
    return float(app.progress.get())


def activity_bar(app) -> float | None:
    """The 2 px prompt-reading bar under the status row: its fill fraction, or None when hidden."""
    if app.act_track.winfo_manager() != "place":
        return None
    try:
        return float(app.act_fill.place_info().get("relwidth", 0.0))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------------------------------- main button
def main_button_text(app) -> str:
    """The big button's label ('Turn on', 'Cancel', 'Turn off', 'Stop now', 'Switch now', 'Stopping…')."""
    return _text(app.toggle)


def main_button_enabled(app) -> bool:
    """The big button accepts clicks."""
    return _enabled(app.toggle)


def click_main_button(app) -> None:
    """Click the big button like a user: waits out the app's double-click guard after this helper's last click."""
    gap = _main().TOGGLE_DEBOUNCE_S + 0.02
    last = _LAST_TOGGLE.get(app)
    if last is not None and time.monotonic() - last < gap:
        pump(app, gap - (time.monotonic() - last))
    _LAST_TOGGLE[app] = time.monotonic()
    app.toggle.invoke()                           # no-op while disabled, like a real click


# ---------------------------------------------------------------------------------------------------- pending stop
def pending_line_visible(app) -> bool:
    """The 'Turns off after this reply · Cancel' line under the button is shown."""
    return app.pending_box.winfo_manager() == "pack"


def pending_line_text(app) -> str:
    """The pending-stop line incl. its 'Cancel' link, or '' when not shown."""
    if not pending_line_visible(app):
        return ""
    return _text(app.pending_text) + _text(app.pending_cancel)


def click_pending_cancel(app) -> None:
    """Click 'Cancel' on the pending-stop line."""
    _click_label(app.pending_cancel)


# ---------------------------------------------------------------------------------------------------- window
def _geometry(app) -> tuple[int, int]:
    m = re.match(r"(\d+)x(\d+)", str(app.geometry()))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def window_height(app) -> int:
    """Window height in logical (unscaled) px, as the app sized itself."""
    return _geometry(app)[1]


def window_width(app) -> int:
    """Window width in logical (unscaled) px."""
    return _geometry(app)[0]


def open_dialog(app):
    """The dialog currently open over the main window (sweep / busy / settings / whitelist), or None."""
    d = getattr(app, "dialog", None)
    try:
        return d if d is not None and d.winfo_exists() else None
    except tkinter.TclError:
        return None


def dialog_buttons(dlg) -> list[str]:
    """Texts of every button in a dialog, top to bottom."""
    return [_text(w) for w in _walk(dlg) if isinstance(w, ctk.CTkButton)]


def click_dialog_button(app, dlg, text: str, timeout: float = 1.0) -> bool:
    """Click the dialog button whose text is (or starts with) `text`; for a BusyDialog, retries until its arming
    guard (busy_dialog.ARM_S) lets the click through. False if no such enabled button."""
    btn = _find(dlg, ctk.CTkButton, text)
    if btn is None or not _enabled(btn):
        return False
    import busy_dialog
    btn.invoke()
    if isinstance(dlg, busy_dialog.BusyDialog):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if not dlg.winfo_exists():
                    break
            except tkinter.TclError:
                break
            pump(app, 0.05)
            try:
                btn.invoke()
            except tkinter.TclError:
                break
    return True


# ---------------------------------------------------------------------------------------------------- header buttons
def gear_enabled(app) -> bool:
    """The Settings gear accepts clicks."""
    return _enabled(app.gear)


def switch_button_visible(app) -> bool:
    """The quick model-switch button is in the header (two or more launchers)."""
    return app.switch_btn.winfo_manager() == "grid"


def switch_button_enabled(app) -> bool:
    """The model-switch button accepts clicks."""
    return _enabled(app.switch_btn)


def _model_menu(app):
    from customtkinter.windows.widgets.core_widget_classes import DropdownMenu
    for w in app.winfo_children():
        if isinstance(w, DropdownMenu):
            return w
    return None


def model_menu_entries(app) -> list[str]:
    """Click the model-switch button and return the menu's entries ('model  ·  run-x.bat  ✓'); [] when the button is
    hidden / disabled or no menu opens. The native popup is not shown (it would block the event loop)."""
    menu = _model_menu(app)
    if menu is None or not switch_button_visible(app) or not switch_button_enabled(app):
        return []
    opened: list = []
    menu.open = lambda x, y: opened.append((x, y))   # instance attribute shadows DropdownMenu.open for one click
    try:
        app.switch_btn.invoke()
    finally:
        del menu.open
    return [str(v) for v in menu.cget("values")] if opened else []


def pick_model(app, text: str) -> str | None:
    """Open the model menu and pick the first entry containing `text`; returns the entry or None."""
    entries = model_menu_entries(app)
    for i, label in enumerate(entries):
        if text in label:
            _model_menu(app).invoke(i)
            return label
    return None


# ---------------------------------------------------------------------------------------------------- panels / pins
def _panel(app, key: str):
    return {"memory": app.mem_card, "vram": app.row_vram, "decode": app.row_decode, "prompt": app.row_prompt}[key]


def _pin(app, key: str):
    return _find(_panel(app, key), ctk.CTkButton)


def panel_visible(app, key: str) -> bool:
    """Panel 'memory' / 'vram' / 'decode' / 'prompt' is laid out."""
    return bool(_panel(app, key).winfo_manager())


def panel_card(app, key: str) -> str | None:
    """'pin' (the always-shown card), 'more' (the expandable card) or None (hidden)."""
    f = _panel(app, key)
    if not f.winfo_manager():
        return None
    inn = str(f.grid_info().get("in"))
    return {str(app.pin_card): "pin", str(app.more_card): "more"}.get(inn, inn)


def pin_glyph(app, key: str) -> str:
    """The panel's pin as drawn: main.PINNED_GLYPH (pinned), main.PIN_GLYPH (hover) or '' (hidden)."""
    return _text(_pin(app, key))


def panel_pinned(app, key: str) -> bool:
    """The panel shows the filled (pinned) pin."""
    return pin_glyph(app, key) == _main().PINNED_GLYPH


def pinned_panels(app) -> list[str]:
    """Panels showing a filled pin, top to bottom."""
    return [k for k in _main().PANELS if panel_pinned(app, k)]


def click_pin(app, key: str) -> None:
    """Click the panel's pin (pins / unpins it)."""
    _pin(app, key).invoke()


def hover_panel(app, key: str, on: bool = True) -> None:
    """Move the pointer onto (on=True) / off a SHOWN panel via <Enter>/<Leave> (a hidden panel gets no events). NOTE:
    the app confirms a leave from the REAL pointer position after 40 ms, so on=False only hides the pin when the real
    pointer is outside the panel - prefer asserting the hover-on state."""
    target = next(w for w in _walk(_panel(app, key)) if type(w).__module__.startswith("tkinter"))
    target.event_generate("<Enter>" if on else "<Leave>")


def more_bar_visible(app) -> bool:
    """The bottom bar that shows / hides the unpinned panels is there (not everything pinned)."""
    return app.chevron.winfo_manager() == "grid"


def click_more_bar(app) -> None:
    """Click the bottom bar (expand / collapse the unpinned panels)."""
    app.chevron.invoke()


# ---------------------------------------------------------------------------------------------------- settings
def open_settings(app, timeout: float = 2.0):
    """Click the gear; returns the SettingsDialog once open, or None (gear disabled / another dialog up)."""
    from settings_dialog import SettingsDialog
    app.gear.invoke()
    pump_until(app, lambda: isinstance(open_dialog(app), SettingsDialog), timeout)
    d = open_dialog(app)
    return d if isinstance(d, SettingsDialog) else None


def _switch_rows(dlg) -> list[tuple[str, Any]]:
    """(caption, CTkSwitch) for every switch row: the caption is the row's label (left), else the switch's text."""
    out = []
    for sw in (w for w in _walk(dlg) if isinstance(w, ctk.CTkSwitch)):
        cap = _text(sw)
        if not cap:
            cap = next((_text(w) for w in sw.master.winfo_children() if isinstance(w, ctk.CTkLabel) and _text(w)), "")
        out.append((cap, sw))
    return out


def _switch(dlg, label: str):
    rows = _switch_rows(dlg)
    for cap, sw in rows:
        if cap == label:
            return sw, cap
    for cap, sw in rows:
        if cap.startswith(label):
            return sw, cap
    raise LookupError(f"no switch {label!r} in Settings (have {[c for c, _s in rows]})")


def settings_switch(dlg, label: str) -> dict:
    """Switch whose caption is (or starts with) `label`: {'value': bool, 'enabled': bool, 'caption': str}."""
    sw, cap = _switch(dlg, label)
    return {"value": bool(sw.get()), "enabled": _enabled(sw), "caption": cap}


def toggle_settings_switch(dlg, label: str) -> None:
    """Click the switch (no-op while disabled, like a real click)."""
    _switch(dlg, label)[0].toggle()


def _option(dlg, label: str):
    for lbl in (w for w in _walk(dlg) if isinstance(w, ctk.CTkLabel)):
        if _text(lbl) == label or _text(lbl).startswith(label):
            menu = _find(lbl.master, ctk.CTkOptionMenu)
            if menu is not None:
                return lbl, menu
    raise LookupError(f"no option row {label!r} in Settings")


def settings_option(dlg, label: str) -> dict:
    """Dropdown row with caption `label`: {'value', 'choices', 'enabled', 'caption'}."""
    lbl, menu = _option(dlg, label)
    return {"value": str(menu.get()), "choices": [str(v) for v in menu.cget("values")], "enabled": _enabled(menu),
            "caption": _text(lbl)}


def choose_settings_option(dlg, label: str, value: str) -> bool:
    """Pick `value` in the dropdown row `label` (the dropdown's own pick path); False if disabled."""
    _lbl, menu = _option(dlg, label)
    if not _enabled(menu):
        return False
    menu._dropdown_callback(value)
    return True


def _launcher_menu(dlg):
    """The launcher dropdown: the CTkOptionMenu that shares its row with the 'Browse…' button."""
    browse = _find(dlg, ctk.CTkButton, "Browse")
    return None if browse is None else _find(browse.master, ctk.CTkOptionMenu)


def settings_launcher(dlg) -> dict:
    """The launcher dropdown: {'value', 'choices', 'enabled'}."""
    m = _launcher_menu(dlg)
    return {"value": str(m.get()), "choices": [str(v) for v in m.cget("values")], "enabled": _enabled(m)}


def _gridded_at(master, row: int):
    """The CTkLabel laid out (grid) in `master` at `row`, or None."""
    for w in master.winfo_children():
        if isinstance(w, ctk.CTkLabel) and w.winfo_manager() == "grid" and int(w.grid_info().get("row", -1)) == row:
            return w
    return None


def settings_launcher_detail(dlg) -> str:
    """The read-only line under the launcher row (what the .bat runs, or why it can't be used)."""
    pick = _launcher_menu(dlg).master
    return _text(_gridded_at(pick.master, int(pick.grid_info()["row"]) + 1))


def settings_message(dlg) -> str:
    """The message line above the buttons ('Saving…', 'Applies next start …', errors) on the tab shown."""
    save = _find(dlg, ctk.CTkButton, "Save", prefix=False)
    return _text(_gridded_at(save.master, int(save.grid_info().get("row", 1)) - 1))


def _clickable(dlg, text: str):
    """A CTkButton, or a LinkRow (settings_dialog.LinkRow: .text, .enabled, .invoke()), by text / prefix."""
    b = _find(dlg, ctk.CTkButton, text)
    if b is not None:
        return b
    import settings_dialog
    rows = [w for w in _walk(dlg) if isinstance(w, settings_dialog.LinkRow)]
    return next((r for r in rows if r.text == text), None) or next((r for r in rows if r.text.startswith(text)), None)


def _clickable_info(w) -> tuple[str, bool]:
    if isinstance(w, ctk.CTkButton):
        return _text(w), _enabled(w)
    return str(w.text), bool(w.enabled)


def settings_button(dlg, text: str) -> dict | None:
    """A Settings button or link row by text ('Save', 'Browse…', 'Protected apps', 'Usage history',
    'Check for updates'): {'text', 'enabled'} or None."""
    b = _clickable(dlg, text)
    if b is None:
        return None
    t, en = _clickable_info(b)
    return {"text": t, "enabled": en}


def click_settings_button(dlg, text: str) -> bool:
    """Click a Settings button / link row by text; False if absent or disabled."""
    b = _clickable(dlg, text)
    if b is None or not _clickable_info(b)[1]:
        return False
    b.invoke()
    return True


def settings_button_note(dlg, text: str) -> str:
    """The text shown next to a Settings button, in its own row (e.g. the 'Check for updates' result); '' if none."""
    b = _find(dlg, ctk.CTkButton, text)
    if b is None:
        return ""
    return " ".join(_text(w) for w in b.master.winfo_children() if isinstance(w, ctk.CTkLabel) and _text(w))


def version_label_text(dlg) -> str:
    """The version at the top right of Settings ('v1.9.0', or 'v1.9.0 · Update to 9.9.0' when it is a link)."""
    for w in _walk(dlg):
        if isinstance(w, ctk.CTkLabel) and re.match(r"v\d", _text(w)):
            return _text(w)
    return ""


def update_link_text(dlg) -> str:
    """The clickable 'Update to X' version label, or '' when there is no update offer."""
    for w in _walk(dlg):
        if isinstance(w, ctk.CTkLabel) and "Update to" in _text(w) and str(w.cget("cursor")) == "hand2":
            return _text(w)
    return ""


def click_update_link(dlg) -> bool:
    """Click the 'Update to X' link in Settings; False when there is none."""
    for w in _walk(dlg):
        if isinstance(w, ctk.CTkLabel) and "Update to" in _text(w) and str(w.cget("cursor")) == "hand2":
            _click_label(w)
            return True
    return False


def close_settings(dlg) -> bool:
    """Click Cancel in Settings (Close on the Behaviour / About tabs: the button that is shown) and wait until the
    window is gone (a close in the dialog's first ARM_S seconds is carried out once it is armed)."""
    cancel = _find(dlg, ctk.CTkButton, "Cancel", prefix=False)
    if cancel is not None and cancel.winfo_manager() == "grid":
        ok = click_settings_button(dlg, "Cancel")
    else:
        ok = click_settings_button(dlg, "Close")
    if ok and not is_closed(dlg):
        pump_until(dlg.master, lambda: is_closed(dlg), 2.0)
    return ok


# ---- Settings tabs
def _tab_bar_buttons(dlg) -> dict[str, Any]:
    names = list(getattr(dlg, "tabs", {}))
    return {_text(w): w for w in _walk(dlg) if isinstance(w, ctk.CTkButton) and _text(w) in names}


def settings_tabs(dlg) -> list[str]:
    """The tab names, left to right ('Model', 'Behaviour', 'About')."""
    return list(getattr(dlg, "tabs", {}))


def settings_tab(dlg) -> str:
    """The tab shown now (the one whose body is laid out)."""
    return next((n for n, f in dlg.tabs.items() if f.winfo_manager() == "grid"), "")


def show_settings_tab(dlg, name: str) -> bool:
    """Click the tab `name` in the tab bar; False if there is no such tab."""
    b = _tab_bar_buttons(dlg).get(name)
    if b is None:
        return False
    b.invoke()
    return True


def settings_tab_of(dlg, widget) -> str:
    """The tab a widget sits on ('' if it is not inside a tab, e.g. the title row or the footer)."""
    frames = {str(f): n for n, f in dlg.tabs.items()}
    w = widget
    while w is not None:
        if str(w) in frames:
            return frames[str(w)]
        w = getattr(w, "master", None)
    return ""


def settings_tab_texts(dlg, name: str) -> list[str]:
    """Every caption on a tab, top to bottom in creation order: labels, buttons, link rows, switch captions."""
    import settings_dialog
    out: list[str] = []
    for w in _walk(dlg.tabs[name]):
        if isinstance(w, settings_dialog.LinkRow):
            out.append(w.text)
        elif isinstance(w, (ctk.CTkLabel, ctk.CTkButton)) and _text(w):
            if isinstance(w, ctk.CTkLabel) and isinstance(getattr(w, "master", None), settings_dialog.LinkRow):
                continue                                  # a link row's own label: counted once, above
            out.append(_text(w))
    return out


def settings_tab_req_heights(dlg) -> dict[str, float]:
    """Show each tab in turn and measure what the whole window needs for it (logical px); ends on the tab that was
    shown. A tab whose need is above dlg.logical_height would be clipped."""
    scale = ctk.ScalingTracker.get_window_scaling(dlg)
    shown = settings_tab(dlg)
    out: dict[str, float] = {}
    for name in settings_tabs(dlg):
        show_settings_tab(dlg, name)
        dlg.update_idletasks()
        out[name] = dlg.winfo_reqheight() / scale
    show_settings_tab(dlg, shown)
    dlg.update_idletasks()
    return out
