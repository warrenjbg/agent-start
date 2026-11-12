"""Live 'running partly on the CPU' check (pure: no psutil, no I/O, no threads).

A model that does not fully fit in video memory runs some layers on the CPU and is much slower. Measured on an RTX
5070 Ti 16 GB with a 27B model (llama.cpp CUDA build, one run each; video memory used/total, GPU load, the server's
CPU as a share of ALL logical cores, speed):

    all layers on the GPU, 8K / 16K ctx     14.6 / 15.8 of 16.3 GB, GPU 97 / 95 %, CPU 6 %, 56 / 42 tok/s
    -ngl 65 (another file), 8K ctx          15.7 of 16.3 GB, GPU 97 %, CPU 6 %, ~55 tok/s       (not noticeably slow)
    -ngl 63, 32K ctx                        16.0 of 16.3 GB, GPU 98 %, CPU 33 %
    -ngl 63 (another file) 16K / -ngl 54 64K  15.7 / 15.9 of 16.3 GB, GPU 73 / 32 %, CPU 49 %, 35 / 16 tok/s
    4 parallel slots, 16K each              15.7 of 16.3 GB, GPU 54 %, CPU 49 %, 29 tok/s

So the symptom is: video memory nearly full AND the engine's processes using a large share of the CPU WHILE
GENERATING (a model fully on the GPU stays near 6 %). Video memory alone does not separate the rows (a model that
fits can fill 97 % of it); the CPU share does. Only generating time is judged: every figure above was taken while
generating, and the CPU share while a prompt is read was not measured.

decide(state, now, generating=..., vram_used_mb=..., vram_total_mb=..., cpu_share_pct=...) -> (changed, new_state).
state.slow is the verdict. It turns True after the symptoms held for `sustain_s` seconds of GENERATING time, counted
over intervals between two consecutive observations that both saw the symptoms while generating; it turns False after
`clear_s` seconds of generating time without them, counted the same way. Between two such observations:
  * idle / reading-a-prompt / unknown (None) observations neither count nor reset (the next interval starts from the
    next generating observation), so the verdict and the running windows survive idle time;
  * a generating observation WITHOUT the symptoms resets the window towards slow (and vice versa);
  * a gap longer than MAX_GAP_S between ANY two observations, or the clock going backwards, restarts both windows.
Feed it about once a second; the caller measures (no helper for the CPU share here, on purpose).
"""
from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass
from typing import Any, Mapping

log = logging.getLogger("agent_start")

MAX_GAP_S = 5.0          # observations further apart than this do not make a continuous period (fed every ~1 s)

# config.json keys (missing key = default; bad value = default; numbers are clamped to the limits)
CONFIG_DEFAULTS: dict[str, float] = {
    "fit_watch_vram_pct": 90,     # video memory used >= this % of the total (fits: 89.6-96.9 %, slow: 96.3-98.2 %)
    "fit_watch_cpu_pct": 25,      # the engine's processes use >= this % of ALL logical cores (fits: 6 %, slow: 33-49 %)
    "fit_watch_sustain_s": 8,     # seconds of generating with both symptoms before the verdict turns 'slow'
    "fit_watch_clear_s": 20,      # seconds of generating without them before it turns back
}
_LIMITS: dict[str, tuple[float, float]] = {
    "fit_watch_vram_pct": (50, 100),
    "fit_watch_cpu_pct": (5, 100),
    "fit_watch_sustain_s": (2, 600),
    "fit_watch_clear_s": (2, 3600),
}

_SYM, _CLEAN, _OTHER = "sym", "clean", "other"


def _num(cfg: Mapping | None, key: str) -> float:
    """cfg[key] as a finite float clamped to its limits; the default for a missing/bad value (bool, str, NaN...)."""
    dflt = float(CONFIG_DEFAULTS[key])
    v = cfg.get(key, dflt) if isinstance(cfg, Mapping) else dflt
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        if v is not dflt:
            log.debug("fit_watch: config %s=%r is not a number; using %g", key, v, dflt)
        return dflt
    lo, hi = _LIMITS[key]
    return min(hi, max(lo, float(v)))


@dataclass(frozen=True)
class FitConfig:
    vram_pct: float = float(CONFIG_DEFAULTS["fit_watch_vram_pct"])
    cpu_pct: float = float(CONFIG_DEFAULTS["fit_watch_cpu_pct"])
    sustain_s: float = float(CONFIG_DEFAULTS["fit_watch_sustain_s"])
    clear_s: float = float(CONFIG_DEFAULTS["fit_watch_clear_s"])

    @classmethod
    def from_config(cls, cfg: Mapping | None) -> FitConfig:
        """Never raises: a hand-edited or old config.json falls back to the defaults key by key."""
        return cls(vram_pct=_num(cfg, "fit_watch_vram_pct"), cpu_pct=_num(cfg, "fit_watch_cpu_pct"),
                   sustain_s=_num(cfg, "fit_watch_sustain_s"), clear_s=_num(cfg, "fit_watch_clear_s"))


@dataclass(frozen=True)
class FitState:
    """The verdict plus the running windows. A fresh FitState() = not slow, nothing counted (a new server run).

    slow       the current verdict
    slow_s     generating seconds with the symptoms counted towards 'slow' (while not slow)
    clear_s    generating seconds without them counted towards 'not slow' (while slow)
    last_t     time of the previous observation of any kind (gap / clock checks)
    last_kind  what the previous observation was: 'sym' / 'clean' (generating, inputs known) / 'other' / None
    """
    slow: bool = False
    slow_s: float = 0.0
    clear_s: float = 0.0
    last_t: float | None = None
    last_kind: str | None = None


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def symptoms(cfg: FitConfig, vram_used_mb: Any, vram_total_mb: Any, cpu_share_pct: Any) -> bool | None:
    """Both symptoms present (True), not both (False), or unknown (None: a missing or unusable figure)."""
    used, total, cpu = _finite(vram_used_mb), _finite(vram_total_mb), _finite(cpu_share_pct)
    if used is None or total is None or cpu is None or total <= 0 or used < 0 or cpu < 0:
        return None
    return used * 100.0 / total >= cfg.vram_pct and cpu >= cfg.cpu_pct


def decide(state: FitState, now: float, *, generating: bool, vram_used_mb: float | None,
           vram_total_mb: float | None, cpu_share_pct: float | None, cfg: FitConfig | None = None,
           max_gap_s: float = MAX_GAP_S) -> tuple[bool, FitState]:
    """One observation. Pure: never mutates `state`; returns (verdict changed, new state). See the module docstring.
    A non-finite `now` is ignored (state unchanged)."""
    cfg = cfg if cfg is not None else FitConfig()
    t = _finite(now)
    if not isinstance(state, FitState):
        state = FitState()
    if t is None:
        return False, state
    if generating is True:
        sym = symptoms(cfg, vram_used_mb, vram_total_mb, cpu_share_pct)
        kind = _OTHER if sym is None else (_SYM if sym else _CLEAN)
    else:
        kind = _OTHER
    slow, slow_s, clear_s = state.slow, state.slow_s, state.clear_s
    last = state.last_t
    if last is not None and (t < last or t - last > max_gap_s):
        slow_s = clear_s = 0.0                    # a break in the observations: start both windows again
        interval_ok = False
    else:
        interval_ok = last is not None
    dt = (t - last) if (interval_ok and last is not None) else 0.0
    if kind == _SYM:
        clear_s = 0.0
        if not slow:
            if interval_ok and state.last_kind == _SYM:
                slow_s += dt
            if slow_s + 1e-6 >= cfg.sustain_s:
                slow, slow_s = True, 0.0
    elif kind == _CLEAN:
        slow_s = 0.0
        if slow:
            if interval_ok and state.last_kind == _CLEAN:
                clear_s += dt
            if clear_s + 1e-6 >= cfg.clear_s:
                slow, clear_s = False, 0.0
    # _OTHER (idle, reading a prompt, unknown figures): the windows and the verdict stay as they are
    new = dataclasses.replace(state, slow=slow, slow_s=slow_s, clear_s=clear_s, last_t=t, last_kind=kind)
    return new.slow != state.slow, new
