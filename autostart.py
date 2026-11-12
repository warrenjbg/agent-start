"""'Start with Windows' switch: a per-user HKCU Run value that launches the app hidden in the tray (--tray).

Respects Task Manager's / Settings' "Startup apps" toggle, which never touches the Run value itself but writes a
12-byte REG_BINARY of the same name under Explorer\\StartupApproved\\Run:

    byte 0      state; bit 0 set = disabled. Explorer writes 0x02 (enabled) / 0x03 (disabled); 0x06 / 0x07 are the
                same states seen on some builds - so even = enabled, odd = disabled.
    bytes 1-3   zero
    bytes 4-11  FILETIME (UTC) of when the entry was disabled; all zero when enabled.
    no value    enabled (Windows' default for a Run entry it has never been asked about).

Verified on this PC (Windows 11, read-only reg query): entries switched off in Task Manager hold
03 00 00 00 <8-byte FILETIME>, entries switched on hold 02 00 00 00 00 00 00 00 00 00 00 00.
Sources: https://www.nutanix.com/blog/windows-os-optimization-essentials-part-4-startup-items ("02 followed by 11
pairs of zeros" = enabled, "03 00 00 00 + 8 bytes" = disabled, even/odd rule);
https://www.windowsforensics.app/en/artifacts/run-keys-startup-folders (first byte 0x02/0x03, bytes 4-11 FILETIME
"zeroed when the entry is re-enabled"); https://renenyffenegger.ch/notes/Windows/registry/tree/HKEY_CURRENT_USER/
Software/Microsoft/Windows/CurrentVersion/Explorer/StartupApproved/Run (02.. or 06.. = enabled).

Re-enabling: Task Manager does NOT delete the StartupApproved value when you switch an entry back on, it overwrites it
with 02 + eleven zero bytes (that is what the switched-on entries on this PC hold). enable() does the same, so the
Startup apps list shows exactly what Task Manager itself would have produced.

All public functions take the key paths / value name as overrides (tests point them at a throwaway HKCU key) and
never raise anything but AutostartError. The readers (is_enabled, current_command) never raise at all: on an
unexpected registry error they log it and report "off" / None.
"""
from __future__ import annotations

import logging
import ntpath
import os
import sys
import winreg

log = logging.getLogger("agent_start")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE = "Agent Start"
TRAY_ARG = "--tray"

# What Task Manager writes when an entry is switched on: state 0x02, then 11 zero bytes (no FILETIME).
APPROVED_ENABLED = bytes([0x02]) + bytes(11)

_HIVE = winreg.HKEY_CURRENT_USER


class AutostartError(Exception):
    """Reading or changing the Start-with-Windows registry entries failed."""


# ----------------------------------------------------------------------------------------------------------- helpers
def _clean_exe(exe) -> str:
    try:
        s = os.fspath(exe)
    except TypeError as e:
        raise AutostartError(f"invalid executable path: {exe!r}") from e
    if isinstance(s, bytes):
        s = os.fsdecode(s)
    s = s.strip().strip('"').strip()
    if not s:
        raise AutostartError("empty executable path")
    return s


def _check_key(path: str) -> None:
    # An empty sub key would make winreg open HKCU itself and write the value there.
    if not isinstance(path, str) or not path.strip("\\ "):
        raise AutostartError(f"invalid registry key path: {path!r}")


def _read(key_path: str, value: str):
    """(data, type) or None if the key or the value does not exist. Raises OSError on anything else."""
    try:
        with winreg.OpenKey(_HIVE, key_path, 0, winreg.KEY_QUERY_VALUE) as k:
            return winreg.QueryValueEx(k, value)
    except FileNotFoundError:
        return None


def _delete(key_path: str, value: str) -> bool:
    """Delete one value; True if it existed. Missing key / value is fine. Raises OSError on anything else."""
    try:
        with winreg.OpenKey(_HIVE, key_path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, value)
            return True
    except FileNotFoundError:
        return False


def _approved_disabled(data, kind) -> bool:
    """StartupApproved verdict: bit 0 of the first byte set = switched off in Task Manager. Anything unreadable
    (wrong type, empty) counts as enabled, like a missing value."""
    if kind != winreg.REG_BINARY or not data:
        return False
    return bool(data[0] & 0x01)


def _split_command(cmd: str) -> tuple[str, str]:
    """Split a Run command into (exe, args). Handles quoted and unquoted exe paths."""
    cmd = cmd.strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end == -1:
            return cmd[1:], ""
        return cmd[1:end], cmd[end + 1:].strip()
    low = cmd.lower()
    i = low.find(".exe")
    if i != -1:
        return cmd[: i + 4], cmd[i + 4:].strip()
    exe, _, args = cmd.partition(" ")
    return exe, args.strip()


def _same_path(a: str, b: str) -> bool:
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def frozen_exe() -> str | None:
    """The installed AgentStart.exe when running as the PyInstaller build, else None (a dev run from python.exe
    has no single exe to register)."""
    return sys.executable if getattr(sys, "frozen", False) else None


# ------------------------------------------------------------------------------------------------------------ public
def command_for(exe) -> str:
    """'"<exe>" --tray' - always quoted, so paths with spaces work."""
    return f'"{_clean_exe(exe)}" {TRAY_ARG}'


def current_command(value: str = VALUE, run_key: str = RUN_KEY) -> str | None:
    """The command stored in the Run value, or None if there is none (or it can't be read)."""
    try:
        _check_key(run_key)
        got = _read(run_key, value)
    except (OSError, AutostartError) as e:
        log.warning("autostart: cannot read Run value %r: %s", value, e)
        return None
    if got is None:
        return None
    data, kind = got
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(data, str):
        return None
    return data


def is_enabled(value: str = VALUE, run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY) -> bool:
    """True only if the Run value exists AND Task Manager's Startup toggle has not switched it off."""
    if current_command(value, run_key) is None:
        return False
    try:
        _check_key(approved_key)
        got = _read(approved_key, value)
    except (OSError, AutostartError) as e:
        log.warning("autostart: cannot read StartupApproved value %r: %s", value, e)
        return True  # the Run value exists; an unreadable approval record is treated like a missing one (enabled)
    return got is None or not _approved_disabled(*got)


def enable(exe, value: str = VALUE, run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY) -> None:
    """Write the Run value and, if Task Manager had switched it off, switch it back on the way Task Manager does."""
    cmd = command_for(exe)
    _check_key(run_key)
    _check_key(approved_key)
    try:
        with winreg.CreateKeyEx(_HIVE, run_key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, value, 0, winreg.REG_SZ, cmd)
        got = _read(approved_key, value)
        if got is not None and _approved_disabled(*got):
            with winreg.CreateKeyEx(_HIVE, approved_key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, value, 0, winreg.REG_BINARY, APPROVED_ENABLED)
            log.info("autostart: re-enabled %r in StartupApproved (was switched off in Task Manager)", value)
    except OSError as e:
        raise AutostartError(f"could not enable Start with Windows: {e}") from e
    log.info("autostart: enabled %r -> %s", value, cmd)


def disable(value: str = VALUE, run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY) -> None:
    """Remove the Run value and our StartupApproved record. Already absent is fine."""
    _check_key(run_key)
    _check_key(approved_key)
    try:
        had_run = _delete(run_key, value)
        had_appr = _delete(approved_key, value)
    except OSError as e:
        raise AutostartError(f"could not disable Start with Windows: {e}") from e
    log.info("autostart: disabled %r (Run value %s, StartupApproved value %s)", value,
             "removed" if had_run else "absent", "removed" if had_appr else "absent")


def refresh(exe, value: str = VALUE, run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY) -> bool:
    """If Start with Windows is on but the stored command is not command_for(exe) (app moved / reinstalled elsewhere,
    old argument format), rewrite it. Returns True if the Run value was changed.

    Only the Run value is rewritten; the StartupApproved record is left alone. A Task-Manager-disabled entry is not
    touched (is_enabled() is False), so refresh never silently turns autostart back on."""
    want = command_for(exe)
    if not is_enabled(value, run_key, approved_key):
        return False
    cur = current_command(value, run_key)
    if cur is None:
        return False
    if cur == want:
        return False
    cur_exe, cur_args = _split_command(cur)
    if cur.lstrip().startswith('"') and _same_path(cur_exe, _clean_exe(exe)) and cur_args == TRAY_ARG:
        return False  # same file, quoted, only differs in case / separators (an unquoted one is rewritten)
    _check_key(run_key)
    try:
        with winreg.CreateKeyEx(_HIVE, run_key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, value, 0, winreg.REG_SZ, want)
    except OSError as e:
        raise AutostartError(f"could not update the Start with Windows command: {e}") from e
    log.info("autostart: refreshed %r: %s -> %s", value, cur, want)
    return True
