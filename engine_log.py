"""Read Strata's ENGINE log: did this load get the fast full-RAM mode or fall back to the slow low-RAM (SSD) mode?

Why a second log: the server's stdout (load_progress.py) prints "loading the experts into RAM (about 45 GB)" even
when the engine then falls back, so only the engine log (the "log" field of the --config JSON) tells the modes apart.
That file is appended across runs; each run starts with "[strata] YYYY-mm-dd HH:MM:SS engine started: <cmdline>".

Lines matched (engine 0.1.56, verified on strata-swift-iq3_s.log / strata-swift-iq3_xxs.log):
  waiting   "the expert arena needs 46.84 GiB of RAM and 39.53 GiB is available; waiting up to 20 s ..."
  low RAM   "not enough RAM for the expert arena (it needs 46.84 GiB and 39.52 GiB is available (7.32 GiB short ..."
            "experts via mmap (not enough RAM for the expert arena ..."
            "resident RAM mode: 13.67 GiB of experts in RAM (page-locked), 795 in the GPU cache; ..."
  full RAM  "loaded 46.84 GiB at 6.68 GiB/s"   (after "expert arena: locked ...")
  GPU       "expert cache 795 slots, 1.54 GiB of VRAM"; "... shrinking the expert cache"
  by choice "--mmap-experts" on the engine-started command line
Pure functions + small file readers; stdlib only, no Tk.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

_START = re.compile(r"^\[strata\] (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) engine started:(.*)$", re.M)
_GUARD = re.compile(r"memory guard: .*?available ([\d.]+) GiB")
_NEEDS = re.compile(r"expert arena needs ([\d.]+) GiB of RAM and ([\d.]+) GiB is available; waiting")
_SHORT = re.compile(r"not enough RAM for the expert arena \(it needs ([\d.]+) GiB and ([\d.]+) GiB is available"
                    r"(?: \(([\d.]+) GiB short)?")
_VIA_MMAP = "experts via mmap"
_RESIDENT = re.compile(r"resident RAM mode: ([\d.]+) GiB of experts in RAM")
_LOADED = re.compile(r"\bloaded ([\d.]+) GiB at [\d.]+ GiB/s")
_SLOTS = re.compile(r"expert cache (\d+) slots, ([\d.]+) GiB of VRAM")
_SHRINK = "shrinking the expert cache"
_ARENA_ANY = re.compile(r"expert arena needs ([\d.]+) GiB|it needs ([\d.]+) GiB and|\bloaded ([\d.]+) GiB at")

MODES = ("unknown", "waiting_for_ram", "full_ram", "low_ram", "mmap_by_config")


@dataclass(frozen=True)
class EngineLoad:
    mode: str = "unknown"
    arena_gib: float | None = None       # RAM the full-RAM mode needs
    available_gib: float | None = None   # latest available RAM the engine reported
    short_gib: float | None = None       # how much was missing (low-RAM fallback)
    resident_gib: float | None = None    # page-locked partial RAM cache in low-RAM mode
    gpu_slots: int | None = None
    gpu_cache_gib: float | None = None
    gpu_shrunk: bool = False             # free VRAM was short: the GPU expert cache was cut down
    started_at: float | None = None      # epoch seconds of the "engine started" line (local time)

    @property
    def slow(self) -> bool:
        return self.mode in ("low_ram", "mmap_by_config")


def config_log_path(config_json_path: str) -> str | None:
    """The engine log path from a Strata config JSON ("log" field), or None."""
    try:
        with open(config_json_path, encoding="utf-8") as fh:
            log = json.load(fh).get("log")
        return str(log) if log else None
    except (OSError, ValueError, AttributeError):
        return None


def _read_tail(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            raw = fh.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if len(raw) == max_bytes and size > max_bytes:        # cut mid-file: drop the partial first line
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text


def _epoch(stamp: str) -> float | None:
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def read_run(log_path: str, since: float | None = None, max_bytes: int = 2_000_000) -> str:
    """Text of the LAST run in the engine log (its "engine started" line to EOF), or ''.

    since: epoch seconds of our launch; a last run that started more than 5 s before it is a previous run (the new
    engine has not written yet), so '' is returned rather than an old verdict.
    """
    text = _read_tail(log_path, max_bytes)
    starts = list(_START.finditer(text))
    if not starts:
        return ""
    last = starts[-1]
    if since is not None:
        t = _epoch(last.group(1))
        if t is None or t < since - 5:
            return ""
    return text[last.start():]


def parse(block: str) -> EngineLoad:
    """Classify one run block. Tolerant of partial text (a load still in progress)."""
    if not block:
        return EngineLoad()
    started_at = None
    by_config = False
    m = _START.search(block)
    if m:
        started_at = _epoch(m.group(1))
        by_config = "--mmap-experts" in m.group(2)

    arena = available = short = resident = cache_gib = None
    slots = None
    mode = "unknown"
    g = _GUARD.search(block)
    if g:
        available = float(g.group(1))
    waits = _NEEDS.findall(block)
    if waits:
        arena, available = float(waits[-1][0]), float(waits[-1][1])
        mode = "waiting_for_ram"
    s = _SHORT.search(block)
    if s:
        arena, available = float(s.group(1)), float(s.group(2))
        short = float(s.group(3)) if s.group(3) else round(arena - available, 2)
        mode = "low_ram"
    elif _VIA_MMAP in block:
        mode = "low_ram"
    loaded = _LOADED.findall(block)
    if loaded and mode != "low_ram":
        arena = float(loaded[-1])
        mode = "full_ram"
    r = _RESIDENT.search(block)
    if r:
        resident = float(r.group(1))
        if mode in ("unknown", "waiting_for_ram"):
            mode = "low_ram"
    sl = _SLOTS.findall(block)
    if sl:
        slots, cache_gib = int(sl[-1][0]), float(sl[-1][1])
    if by_config and mode in ("low_ram", "unknown") and (_VIA_MMAP in block or r or mode == "low_ram"):
        mode = "mmap_by_config"
    return EngineLoad(mode=mode, arena_gib=arena, available_gib=available, short_gib=short, resident_gib=resident,
                      gpu_slots=slots, gpu_cache_gib=cache_gib, gpu_shrunk=_SHRINK in block, started_at=started_at)


def learned_arena_gib(log_path: str, max_bytes: int = 4_000_000) -> float | None:
    """The most recent full-RAM arena size of any run in this log (what this model needs), or None."""
    found = None
    for m in _ARENA_ANY.finditer(_read_tail(log_path, max_bytes)):
        v = next(x for x in m.groups() if x)
        found = float(v)
    return found


def current(config_json_path: str, since: float | None = None) -> EngineLoad:
    """Convenience: the load verdict for the run started at `since` (or the latest run)."""
    path = config_log_path(config_json_path)
    return parse(read_run(path, since)) if path else EngineLoad()
