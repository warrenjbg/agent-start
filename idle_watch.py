"""Idle auto-off: has the engine gone long enough without a request?

Pure: no clock, no Tk, no I/O, no process. The app feeds one observation per metrics poll and does the turn-off
itself (its normal Turn off); this module only answers "idle for the chosen time by now?".

Rules, all on the side of keeping the engine running:
- Idle time only grows between two consecutive polls that both KNOW the model is idle, at most MAX_GAP_S apart.
  A longer gap (the PC slept, the app stalled) adds nothing: waking the PC never turns the engine off at once.
- A poll that does not know (no answer, an engine that does not say what the model is doing, model not loaded)
  starts over from zero. So does a running or queued request, an agent between tool calls, and a finished request
  that was not there at the poll before (a reply shorter than one poll is still seen).
- The first poll after a reset counts as activity: a start or an adopted server begins at zero.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from typing import Any, Hashable, Iterable

CHOICES_MIN = (0, 15, 30, 60, 120)        # minutes; 0 = never (the default)
STATE_KEY = "idle_off_min"                # state.json
MAX_GAP_S = 5.0                           # the same cap as serving time in the usage history
SEEN_MAX = 2048                           # finished requests remembered (engines report their newest few dozen)
NEVER = "Keep running"


def span(minutes: int) -> str:
    """15 -> '15 minutes', 60 -> '1 hour', 120 -> '2 hours' (toast and log wording)."""
    if minutes >= 60 and minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour" + ("" if h == 1 else "s")
    return f"{minutes} minutes"


def label(minutes: int) -> str:
    """The Settings entry for one choice: 'Keep running', 'Off after 15 min', 'Off after 1 hour'."""
    if minutes <= 0:
        return NEVER
    return "Off after " + (span(minutes) if minutes >= 60 else f"{minutes} min")


def labels() -> list[str]:
    return [label(m) for m in CHOICES_MIN]


def parse(value: Any) -> int:
    """Minutes from a saved number or a Settings entry. Anything that is not one of CHOICES_MIN -> 0 (never):
    a damaged state.json must not turn the feature on."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value in CHOICES_MIN else 0
    if isinstance(value, str):
        for m in CHOICES_MIN:
            if value == label(m):
                return m
    return 0


class IdleWatch:
    """Idle seconds of the current serving run. One thread (the app's Tk thread) calls it."""

    def __init__(self) -> None:
        self._idle_s = 0.0
        self._last: float | None = None                       # time of the previous poll that knew
        self._seen: OrderedDict[Hashable, None] = OrderedDict()
        self._primed = False

    def reset(self) -> None:
        """A new serving run, or the app just acted: start over."""
        self._idle_s, self._last, self._primed = 0.0, None, False
        self._seen.clear()

    @property
    def idle_s(self) -> float:
        return self._idle_s

    def _fresh(self, request_ids: Iterable[Hashable]) -> bool:
        fresh = False
        try:
            for rid in request_ids or ():
                if rid in self._seen:
                    self._seen.move_to_end(rid)
                else:
                    self._seen[rid] = None
                    fresh = True
            while len(self._seen) > SEEN_MAX:
                self._seen.popitem(last=False)
        except TypeError:                                     # an unhashable id: treat as activity
            return True
        return fresh

    def observe(self, now: Any, *, minutes: Any, known: bool, busy: bool,
                request_ids: Iterable[Hashable] = ()) -> bool:
        """One poll. known: the engine answered and said what the model is doing; busy: something is running,
        queued, or an agent is between tool calls; request_ids: the finished requests the poll listed.
        Returns True when the model has now been idle for `minutes` (never for 0 / junk)."""
        fresh = self._fresh(request_ids)
        ok = not isinstance(now, bool) and isinstance(now, (int, float)) and math.isfinite(now)
        if not known or not ok:
            self._idle_s, self._last = 0.0, None
            return False
        t = float(now)
        if busy or fresh or not self._primed:
            self._primed = True
            self._idle_s, self._last = 0.0, t
            return False
        if self._last is not None:
            dt = t - self._last
            if 0 < dt <= MAX_GAP_S:
                self._idle_s += dt
        self._last = t
        m = parse(minutes)
        return m > 0 and self._idle_s >= m * 60.0
