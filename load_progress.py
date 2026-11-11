"""Turn Strata's startup log into a phase label + progress fraction for the UI.

Phases verified against Strata 0.2.10 logs (logs\\strata-*.log written by EndpointManager):
    loading the model (the first start takes a minute or two) ...
    [strata] starting the engine: reading the model's weights ...
    [strata] loading the experts into RAM (about 45 GB) and locking part of them for the GPU.
    [strata] filling the GPU's expert cache (1095 experts, 2.11 GiB of VRAM) ...
    [strata] almost ready ...
    ready: http://127.0.0.1:8080/v1 ...
Progress blends the phase floor with elapsed time against the last measured cold load, so the bar keeps moving
inside a long phase but never passes the next phase's floor.
"""
from __future__ import annotations

import os
import re

# (regex, label, floor, ceiling) — floor is where the bar jumps when the phase line appears.
PHASES: list[tuple[re.Pattern, str, float, float]] = [
    (re.compile(r"loading the model", re.I), "Starting engine", 0.04, 0.12),
    (re.compile(r"reading the model's weights", re.I), "Reading weights", 0.12, 0.25),
    (re.compile(r"loading the experts into RAM(?: \(about ([\d.]+) GB\))?", re.I), "Loading experts into RAM",
     0.25, 0.78),
    (re.compile(r"filling the GPU's expert cache", re.I), "Filling GPU cache", 0.78, 0.92),
    (re.compile(r"almost ready", re.I), "Almost ready", 0.92, 0.98),
    (re.compile(r"^ready:", re.I | re.M), "Ready", 1.0, 1.0),
]
DEFAULT_EXPECTED_S = 60.0
_TAIL_BYTES = 64 * 1024


def read_tail(path: str | None) -> str:
    """Last 64 KB of the log (the start sequence is short); '' if unreadable."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse(text: str, elapsed_s: float, expected_s: float | None = None) -> tuple[str, float, str | None]:
    """Return (phase label, fraction 0..1, detail) for the log text so far.

    detail carries the expert size when the log states it ("about 45 GB").
    """
    expected = expected_s if expected_s and expected_s > 5 else DEFAULT_EXPECTED_S
    label, floor, ceil, detail = "Launching", 0.0, 0.04, None
    for rx, name, lo, hi in PHASES:
        m = rx.search(text)
        if m:
            label, floor, ceil, detail = name, lo, hi, None
            if m.groups() and m.group(1):
                detail = f"about {m.group(1)} GB"
    time_frac = min(0.99, max(0.0, elapsed_s / expected))
    frac = min(ceil, max(floor, time_frac))
    return label, frac, detail


def fmt_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"
