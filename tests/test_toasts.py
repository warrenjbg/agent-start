"""toasts.py: titles/messages are data, never code. Runs PowerShell only to PARSE the XML back (no toast shown)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import toasts  # noqa: E402

PASSES, FAILS = 0, []


def check(cond, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


def main() -> int:
    hostile = [
        "$(7*6) PROBE $env:USERNAME",
        "Cash$Grab `whoami` \"@ quotes \"",
        "<b>bold</b> & ]]> <![CDATA[ x ]]>",
        "'; Remove-Item C:\\nope; '",
        "@\"\n$(1+1)\n\"@",
        "ELDEN RING™ — ünïcødé 日本語",
    ]
    for t in hostile:
        msg = f"{t} started while Strata is running (~49 GB)."
        out = toasts.echo(t, msg, [("Turn off Strata", "agentstart://game-off")])
        want_t = toasts._clean(t, 120)
        check(len(out) >= 3 and out[0] == want_t, f"title read back verbatim: {t!r} -> {out[:1]!r}")
        check(len(out) >= 3 and out[1] == toasts._clean(msg), f"message read back verbatim ({t!r})")
        check(len(out) >= 3 and out[2] == "Turn off Strata|agentstart://game-off", f"button intact ({out[2:3]!r})")
        check("42" not in out[0] or "42" in t, f"nothing evaluated ({out[0]!r})")
        check(os.environ.get("USERNAME", "zzz") not in out[0] or os.environ.get("USERNAME", "") in t,
              "no $env expansion")
    x = toasts.build_xml("a & b", "c < d", buttons=[("x\"y", "agentstart://game-off")], launch="agentstart://show")
    check("a &amp; b" in x and "c &lt; d" in x and ("content='x\"y'" in x or 'content="x&quot;y"' in x), f"XML escaping ({x[:200]})")
    check('launch="agentstart://show"' in x and 'activationType="protocol"' in x, "launch attribute")
    check(toasts._clean("a\x00b\x1bc") == "a b c", "control characters removed")
    check(len(toasts._clean("x" * 1000)) == toasts.MAX_TEXT, "length bounded")
    check("$env:" + toasts.ENV_XML in toasts.SCRIPT and "{" not in toasts.SCRIPT.replace("{ ", ""),
          "the shown script is a constant that reads the XML from the environment")
    print(f"RESULT: {PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
