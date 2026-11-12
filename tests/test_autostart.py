"""autostart.py against a throwaway HKCU\\Software\\AgentStartTest_<random> key (deleted at the end, verified).
Never writes to the real Run / StartupApproved keys; only reads them (and asserts they are unchanged).

Run: .venv/Scripts/python.exe tests/test_autostart.py"""
from __future__ import annotations

import logging
import sys
import traceback
import uuid
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import autostart as A  # noqa: E402

HKCU = winreg.HKEY_CURRENT_USER
BASE = rf"Software\AgentStartTest_{uuid.uuid4().hex[:12]}"
RUN = BASE + r"\Run"
APPR = BASE + r"\StartupApproved\Run"
VAL = "Agent Start Test"
KW = dict(value=VAL, run_key=RUN, approved_key=APPR)

PASSES, FAILS = 0, []


def check(cond, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print("FAIL:", msg)


def raises_autostart_error(fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except A.AutostartError:
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  unexpected {type(e).__name__}: {e}")
        return False
    return False


def read(key, name):
    try:
        with winreg.OpenKey(HKCU, key) as k:
            return winreg.QueryValueEx(k, name)
    except FileNotFoundError:
        return None


def write(key, name, kind, data):
    with winreg.CreateKeyEx(HKCU, key, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, kind, data)


def key_exists(key) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(HKCU, key))
        return True
    except FileNotFoundError:
        return False


def delete_tree(key) -> None:
    try:
        with winreg.OpenKey(HKCU, key, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                delete_tree(key + "\\" + sub)
        winreg.DeleteKey(HKCU, key)
    except FileNotFoundError:
        pass


def snapshot(key) -> dict:
    out = {}
    try:
        with winreg.OpenKey(HKCU, key) as k:
            i = 0
            while True:
                try:
                    n, d, t = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[n] = (d, t)
                i += 1
    except FileNotFoundError:
        pass
    return out


def real_key_report(before_appr: dict) -> None:
    print("Real HKCU StartupApproved\\Run (read-only):")
    for name, (data, kind) in sorted(before_appr.items()):
        if kind == winreg.REG_BINARY:
            print(f"  {name[:40]:40} {data.hex(' ')}  -> {'disabled' if data and data[0] & 1 else 'enabled'}"
                  f"  (is_enabled={A.is_enabled(name)})")
            # read-only: is_enabled with the REAL keys must agree with the first-byte rule when the Run value exists
            if A.current_command(name) is not None:
                check(A.is_enabled(name) == (not (data and data[0] & 1)), f"real key verdict for {name}")
                check(len(data) == 12, f"real StartupApproved value {name} is 12 bytes")
                if data[0] & 1 == 0:
                    check(data[1:] == bytes(11), f"enabled real value {name} is 02 + 11 zero bytes")
                else:
                    check(data[1:4] == bytes(3) and data[4:] != bytes(8), f"disabled real value {name} has a FILETIME")
    check(A.is_enabled(A.VALUE) in (True, False), "is_enabled on the real 'Agent Start' value reads without error")


def run() -> None:
    exe1 = r"C:\Users\Some User\AppData\Local\Programs\Agent Start\AgentStart.exe"
    exe2 = r"D:\Other Place\Agent Start\AgentStart.exe"

    # --- command_for: always quoted
    check(A.command_for(exe1) == f'"{exe1}" --tray', "command_for quotes a path with spaces")
    check(A.command_for(r"C:\x\a.exe") == r'"C:\x\a.exe" --tray', "command_for quotes a path without spaces")
    check(A.command_for(f'"{exe1}"') == f'"{exe1}" --tray', "command_for does not double-quote")
    check(A.command_for(Path(exe1)) == f'"{exe1}" --tray', "command_for accepts a Path")
    check(raises_autostart_error(A.command_for, ""), "command_for('') -> AutostartError")
    check(raises_autostart_error(A.command_for, None), "command_for(None) -> AutostartError")

    # --- absent
    check(not key_exists(BASE), "test key does not exist before the test")
    check(A.is_enabled(**KW) is False, "is_enabled False when nothing written (key missing)")
    check(A.current_command(VAL, RUN) is None, "current_command None when key missing")
    try:
        A.disable(**KW)
        check(True, "")
    except Exception as e:  # noqa: BLE001
        check(False, f"disable when absent raised {e!r}")
    check(A.refresh(exe1, **KW) is False, "refresh when absent returns False and writes nothing")
    check(A.current_command(VAL, RUN) is None, "refresh when absent wrote nothing")

    # --- enable / is_enabled / disable round trip
    A.enable(exe1, **KW)
    got = read(RUN, VAL)
    check(got == (f'"{exe1}" --tray', winreg.REG_SZ), f"enable wrote REG_SZ quoted command, got {got!r}")
    check(A.current_command(VAL, RUN) == f'"{exe1}" --tray', "current_command returns stored command")
    check(A.is_enabled(**KW) is True, "is_enabled True after enable")
    check(read(APPR, VAL) is None, "enable writes no StartupApproved value when none was there")
    A.enable(exe1, **KW)  # idempotent
    check(A.is_enabled(**KW) is True, "enable twice still enabled")
    A.disable(**KW)
    check(read(RUN, VAL) is None, "disable removed the Run value")
    check(A.is_enabled(**KW) is False, "is_enabled False after disable")
    A.disable(**KW)
    check(True, "")  # second disable did not raise

    # --- Task Manager toggle (StartupApproved)
    A.enable(exe1, **KW)
    filetime = bytes.fromhex("865AB373ECB2DC01")
    for first, want in ((0x02, True), (0x03, False), (0x06, True), (0x07, False)):
        data = bytes([first, 0, 0, 0]) + (filetime if first & 1 else bytes(8))
        write(APPR, VAL, winreg.REG_BINARY, data)
        check(A.is_enabled(**KW) is want, f"StartupApproved first byte 0x{first:02x} -> is_enabled {want}")
    write(APPR, VAL, winreg.REG_BINARY, b"")
    check(A.is_enabled(**KW) is True, "empty StartupApproved value counts as enabled")
    write(APPR, VAL, winreg.REG_SZ, "junk")
    check(A.is_enabled(**KW) is True, "non-binary StartupApproved value counts as enabled")

    # disabled by Task Manager -> enable() switches it back on the way Task Manager does
    write(APPR, VAL, winreg.REG_BINARY, bytes([3, 0, 0, 0]) + filetime)
    check(A.is_enabled(**KW) is False, "disabled-by-Task-Manager -> is_enabled False")
    check(A.current_command(VAL, RUN) is not None, "Task Manager disable keeps the Run value")
    A.enable(exe1, **KW)
    check(read(APPR, VAL) == (bytes([2]) + bytes(11), winreg.REG_BINARY), "enable rewrote StartupApproved to 02+11 zeros")
    check(A.is_enabled(**KW) is True, "enable re-enabled a Task-Manager-disabled entry")
    # an already-enabled StartupApproved record is left as is
    write(APPR, VAL, winreg.REG_BINARY, bytes([6]) + bytes(11))
    A.enable(exe1, **KW)
    check(read(APPR, VAL)[0] == bytes([6]) + bytes(11), "enable leaves an enabled StartupApproved value untouched")

    # disable removes both values
    write(APPR, VAL, winreg.REG_BINARY, bytes([3, 0, 0, 0]) + filetime)
    A.disable(**KW)
    check(read(RUN, VAL) is None and read(APPR, VAL) is None, "disable removed Run and StartupApproved values")

    # --- refresh
    A.enable(exe1, **KW)
    check(A.refresh(exe1, **KW) is False, "refresh leaves a correct command alone")
    check(A.current_command(VAL, RUN) == f'"{exe1}" --tray', "command unchanged after no-op refresh")
    check(A.refresh(exe1.upper(), **KW) is False, "refresh ignores case-only differences in the path")
    check(A.current_command(VAL, RUN) == f'"{exe1}" --tray', "command unchanged after case-only refresh")
    check(A.refresh(exe2, **KW) is True, "refresh rewrites a stale path")
    check(A.current_command(VAL, RUN) == f'"{exe2}" --tray', "refresh wrote the new exe")
    check(A.refresh(exe2, **KW) is False, "second refresh is a no-op")
    write(RUN, VAL, winreg.REG_SZ, f'"{exe2}"')  # old format without --tray
    check(A.refresh(exe2, **KW) is True, "refresh fixes a command missing --tray")
    check(A.current_command(VAL, RUN) == f'"{exe2}" --tray', "refresh restored --tray")
    write(RUN, VAL, winreg.REG_SZ, exe2.replace('"', "") + " --tray")  # unquoted, spaces
    check(A.refresh(exe2, **KW) is True, "refresh re-quotes an unquoted command")
    check(A.current_command(VAL, RUN) == f'"{exe2}" --tray', "refresh wrote the quoted command")
    # refresh does not touch a Task-Manager-disabled entry (would otherwise be a silent no-op anyway; keeps it off)
    write(RUN, VAL, winreg.REG_SZ, f'"{exe1}" --tray')
    write(APPR, VAL, winreg.REG_BINARY, bytes([3, 0, 0, 0]) + filetime)
    check(A.refresh(exe2, **KW) is False, "refresh skips a Task-Manager-disabled entry")
    check(A.is_enabled(**KW) is False, "refresh did not re-enable a Task-Manager-disabled entry")
    A.disable(**KW)

    # --- failures surface only as AutostartError
    check(raises_autostart_error(A.enable, exe1, value=VAL, run_key="", approved_key=APPR),
          "enable with empty run_key -> AutostartError (never writes into HKCU root)")
    check(read("", VAL) is None, "nothing written into the HKCU root")
    bad = BASE + "\\" + "x" * 300  # key name over the 255-char limit
    check(raises_autostart_error(A.enable, exe1, value=VAL, run_key=bad, approved_key=APPR),
          "enable with an invalid key -> AutostartError")
    check(raises_autostart_error(A.enable, None, **KW), "enable(None) -> AutostartError")
    check(raises_autostart_error(A.disable, value=VAL, run_key="", approved_key=APPR),
          "disable with empty run_key -> AutostartError")
    check(raises_autostart_error(A.refresh, "", **KW), "refresh('') -> AutostartError")
    check(A.is_enabled(VAL, bad, APPR) is False, "is_enabled on an invalid key returns False, no raise")
    check(A.current_command(VAL, "") is None, "current_command on an empty key returns None, no raise")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    real_run_before = snapshot(A.RUN_KEY)
    real_appr_before = snapshot(A.APPROVED_KEY)
    try:
        run()
        real_key_report(real_appr_before)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILS.append("exception during test run")
    finally:
        delete_tree(BASE)
    check(not key_exists(BASE), f"test key {BASE} removed")
    check(snapshot(A.RUN_KEY) == real_run_before, "real Run key unchanged")
    check(snapshot(A.APPROVED_KEY) == real_appr_before, "real StartupApproved key unchanged")
    print(f"test key: HKCU\\{BASE} (exists after cleanup: {key_exists(BASE)})")
    print(f"{PASSES} passed, {len(FAILS)} failed")
    for f in FAILS:
        print("FAIL:", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
