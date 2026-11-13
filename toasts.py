"""Windows toast notifications without code injection.

winotify (used before 1.8.1) pastes the title and message into a PowerShell here-string, so a game folder named
`$(...)` ran PowerShell when its toast showed. Here the PowerShell script is a CONSTANT; the toast XML (built with
proper XML escaping) and the app id reach it as environment variables, i.e. as data that PowerShell never parses as
code. Fire-and-forget: show() starts a hidden powershell.exe and returns.
"""
from __future__ import annotations

import logging
import os
import subprocess
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger("agent_start")

ENV_XML = "AGENTSTART_TOAST_XML"
ENV_APP = "AGENTSTART_TOAST_APPID"

# Constant: nothing from a title/message/URL is ever part of this text.
SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]"
    " > $null; "
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null; "
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; "
    "$x.LoadXml($env:" + ENV_XML + "); "
    "$t = [Windows.UI.Notifications.ToastNotification]::new($x); "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:" + ENV_APP + ").Show($t)"
)
# Test-only: parse the same XML and print its texts back (proves nothing in them was evaluated). No toast.
ECHO_SCRIPT = (
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null; "
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; "
    "$x.LoadXml($env:" + ENV_XML + "); "
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "foreach ($n in $x.GetElementsByTagName('text')) { [Console]::Out.WriteLine($n.InnerText) }; "
    "foreach ($n in $x.GetElementsByTagName('action')) { [Console]::Out.WriteLine($n.GetAttribute('content') + '|' + "
    "$n.GetAttribute('arguments')) }"
)

MAX_TEXT = 300


def _clean(s: object, limit: int = MAX_TEXT) -> str:
    """Printable text only (control characters break the XML), bounded length."""
    s = "".join(ch if ch.isprintable() or ch == " " else " " for ch in str(s or ""))
    return s if len(s) <= limit else s[: limit - 1] + "…"


def build_xml(title: str, msg: str, icon: str = "", buttons: list[tuple[str, str]] | None = None,
              launch: str = "") -> str:
    """Toast XML; every user-visible string is XML-escaped, every attribute quoted by quoteattr."""
    head = '<toast duration="short"' + (f' activationType="protocol" launch={quoteattr(launch)}' if launch else "") + ">"
    img = f'<image placement="appLogoOverride" src={quoteattr(icon)}/>' if icon and os.path.isfile(icon) else ""
    acts = "".join(f'<action content={quoteattr(_clean(label, 40))} activationType="protocol" '
                   f'arguments={quoteattr(url)}/>' for label, url in (buttons or []))
    return (head + '<visual><binding template="ToastGeneric">' + img
            + f"<text>{escape(_clean(title, 120))}</text><text>{escape(_clean(msg))}</text>"
            + "</binding></visual>" + (f"<actions>{acts}</actions>" if acts else "") + "</toast>")


def _powershell() -> str:
    """Windows PowerShell by its full path. A bare "powershell.exe" is looked up in the application folder and
    the current directory before System32, so a file of that name planted there would run on every toast."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    full = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return full if os.path.isfile(full) else "powershell.exe"


def _env(xml: str, app_id: str) -> dict:
    env = dict(os.environ)
    env[ENV_XML] = xml
    env[ENV_APP] = str(app_id)
    return env


def show(title: str, msg: str, app_id: str, icon: str = "", buttons: list[tuple[str, str]] | None = None,
         launch: str = "") -> bool:
    """Start the toast; True if powershell.exe was started. Never raises."""
    try:
        xml = build_xml(title, msg, icon, buttons, launch)
        subprocess.Popen([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-Command", SCRIPT], env=_env(xml, app_id), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
        return True
    except Exception:  # noqa: BLE001
        log.debug("toast failed", exc_info=True)
        return False


def echo(title: str, msg: str, buttons: list[tuple[str, str]] | None = None, timeout: float = 30.0) -> list[str]:
    """Tests: what PowerShell reads back from the XML (texts, then 'label|url' per button). Shows nothing."""
    xml = build_xml(title, msg, "", buttons)
    r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", ECHO_SCRIPT], env=_env(xml, "test"), capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.decode("utf-8", "replace").replace("\r", "").rstrip("\n").split("\n")
