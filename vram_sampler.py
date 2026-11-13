"""Rolling GPU-memory history for the stats section (device-level NVML; no per-process data on WDDM).

`VramSampler` reads `nvmlDeviceGetMemoryInfo` every 2 s on its own daemon thread and keeps 5 minutes. If NVML is
missing or fails, `snapshot().available` is False and the UI shows "VRAM unavailable"; init is retried now and then
(driver updates restart NVML). `push_external` lets the app feed Strata's own GPU telemetry as a fallback.

GPU energy is metered on the same thread and the same NVML handle: `energy_j()` is the joules the WHOLE card used
since this sampler started (not only the engine; the caller decides which spans count, e.g. only while serving).
Source, decided at the first reading and again after every NVML re-init: the card's own energy counter
(nvmlDeviceGetTotalEnergyConsumption, millijoules) when it works, else power readings (nvmlDeviceGetPowerUsage,
milliwatts) times the time since the previous sample. Nothing is invented: a first reading, a counter that went
backwards (driver restart), a power gap longer than GAP_INTERVALS intervals (sleep, a stalled thread) and any step
implying more than MAX_PLAUSIBLE_W add nothing. A read that fails restarts the baseline and re-decides the source.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("agent_start")

INTERVAL_S = 2.0
WINDOW_S = 300.0
RETRY_S = 60.0
_MB = 1024.0 * 1024.0
GAP_INTERVALS = 3            # power fallback: a longer gap between samples adds nothing for that gap
MAX_PLAUSIBLE_W = 2000.0     # a step implying more than this is a glitch, not a graphics card
_COUNTER_FN = "nvmlDeviceGetTotalEnergyConsumption"     # millijoules since the driver loaded
_POWER_FN = "nvmlDeviceGetPowerUsage"                   # milliwatts


@dataclass(frozen=True)
class VramSnapshot:
    samples: tuple[tuple[float, float], ...]    # (monotonic time, used MB), oldest -> newest
    total_mb: float | None
    available: bool | None                      # None = not sampled yet, False = no NVML and no fallback data
    source: str                                 # "nvml" | "strata" | ""
    now: float


class VramSampler:
    def __init__(self, interval_s: float = INTERVAL_S, window_s: float = WINDOW_S, nvml=None,
                 clock=time.monotonic, retry_s: float = RETRY_S) -> None:
        self.interval_s = max(0.05, float(interval_s))
        self.window_s = float(window_s)
        self._retry_s = float(retry_s)
        self._nvml = nvml                        # injected module (tests); None = import pynvml lazily
        self._clock = clock
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, float]] = deque()
        self._total_mb: float | None = None
        self._available: bool | None = None
        self._source = ""
        self._handle = None
        self._inited = False
        self._next_init = 0.0
        self._warned = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # energy (all under _lock): the running total, the source, the baselines of the current source
        self._energy_j: float | None = None      # None until an energy reading was possible at least once
        self._energy_source = ""                 # "counter" | "power" | "" (not decided / none works)
        self._power_ok = False                   # power readings worked when the source was decided
        self._last_counter: tuple[float, float] | None = None   # (time, millijoules) of the previous reading
        self._last_power_t: float | None = None  # time of the previous power reading (fallback integration)
        self._power: tuple[float, float] | None = None          # (time, watts) of the latest power figure
        self._energy_logged = "?"                # the last decision logged (each change is logged once)

    # ------------------------------------------------------------------ NVML
    def _init(self) -> bool:
        now = self._clock()
        if now < self._next_init:
            return False
        self._next_init = now + self._retry_s
        try:
            nv = self._nvml
            if nv is None:
                import pynvml as nv                 # nvidia-ml-py; needs the NVIDIA driver's nvml.dll
                self._nvml = nv
            nv.nvmlInit()
            self._inited = True
            best, best_total = None, -1
            for i in range(int(nv.nvmlDeviceGetCount())):
                h = nv.nvmlDeviceGetHandleByIndex(i)
                total = int(nv.nvmlDeviceGetMemoryInfo(h).total)
                if total > best_total:              # the card with the most memory is the one Strata uses
                    best, best_total = h, total
            if best is None:
                raise RuntimeError("NVML reports no GPU")
            self._handle = best
            return True
        except Exception as exc:  # noqa: BLE001 - ImportError, NVMLError, missing DLL (OSError)
            if not self._warned:
                self._warned = True
                log.info("VRAM sampling unavailable (NVML): %s", exc)
            self._shutdown()
            return False

    def _shutdown(self) -> None:
        self._handle = None
        self._reset_energy()                    # a re-init re-decides the source and starts a new baseline
        if self._inited and self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                log.debug("nvmlShutdown failed", exc_info=True)
        self._inited = False

    def sample_once(self) -> bool:
        """Take one reading (called by the thread; tests call it directly). True if a sample was stored."""
        if self._handle is None and not self._init():
            with self._lock:
                if self._source != "strata" or not self._fresh_locked():
                    self._available = False
            return False
        try:
            mem = self._nvml.nvmlDeviceGetMemoryInfo(self._handle)
            used_mb, total_mb = float(mem.used) / _MB, float(mem.total) / _MB
        except Exception as exc:  # noqa: BLE001
            log.info("NVML read failed, will retry: %s", exc)
            self._shutdown()
            self._next_init = self._clock() + self._retry_s
            with self._lock:
                self._available = False
            return False
        self._store(used_mb, total_mb, "nvml")
        self._meter_energy()
        return True

    # ------------------------------------------------------------------ energy
    def _reset_energy(self) -> None:
        with self._lock:
            self._energy_source = ""
            self._power_ok = False
            self._last_counter = None
            self._last_power_t = None

    def _read(self, name: str) -> float | None:
        """One NVML energy / power reading; None when the call is missing, raises or returns junk."""
        fn = getattr(self._nvml, name, None)
        if fn is None or self._handle is None:
            return None
        try:
            v = float(fn(self._handle))
        except Exception:  # noqa: BLE001 - NVMLError_NotSupported, a lost GPU, a junk return value
            return None
        return v if math.isfinite(v) and v >= 0 else None

    def _meter_energy(self) -> None:
        """One energy step after a good memory read (sampling thread only). Never raises."""
        try:
            with self._lock:
                source, power_ok = self._energy_source, self._power_ok
            counter = self._read(_COUNTER_FN) if source in ("", "counter") else None
            power_mw = self._read(_POWER_FN) if source in ("", "power") or power_ok else None
            now = self._clock()
            with self._lock:
                self._step_locked(now, counter, power_mw)
        except Exception:  # noqa: BLE001 - energy is an extra: it must never cost the VRAM sample
            log.debug("GPU energy step failed", exc_info=True)

    def _step_locked(self, now: float, counter_mj: float | None, power_mw: float | None) -> None:
        src = self._energy_source
        if (src == "counter" and counter_mj is None) or (src == "power" and power_mw is None):
            self._energy_source, self._last_counter, self._last_power_t = "", None, None
            return                                  # the source stopped working: re-decided on the next sample
        if not self._energy_source:
            self._energy_source = "counter" if counter_mj is not None else "power" if power_mw is not None else ""
            self._power_ok = power_mw is not None
            self._last_counter, self._last_power_t = None, None
            if self._energy_source != self._energy_logged:
                self._energy_logged = self._energy_source
                log.info("GPU energy metering: %s", {"counter": "energy counter", "power": "power readings"}.get(
                    self._energy_source, "unavailable"))
        if not self._energy_source:
            return
        watts = power_mw / 1000.0 if power_mw is not None else None
        if watts is not None and watts > MAX_PLAUSIBLE_W:
            watts = None
        added = 0.0
        if self._energy_source == "counter" and counter_mj is not None:
            prev, self._last_counter = self._last_counter, (now, counter_mj)
            if prev is not None:
                dt, d_j = now - prev[0], (counter_mj - prev[1]) / 1000.0
                if d_j > 0 and dt > 0 and d_j / dt <= MAX_PLAUSIBLE_W:   # backwards / implausible: nothing
                    added = d_j
                    if watts is None:
                        watts = d_j / dt                    # no power call: the counter's average stands in
        elif self._energy_source == "power":
            prev_t, self._last_power_t = self._last_power_t, now
            if prev_t is not None and watts is not None:
                dt = now - prev_t
                if 0 < dt <= GAP_INTERVALS * self.interval_s:
                    added = watts * dt
        if watts is not None:
            self._power = (now, watts)
        self._energy_j = (self._energy_j or 0.0) + added

    def energy_j(self) -> float | None:
        """Joules the whole card used since this sampler started sampling (monotonic); None while no energy reading
        has ever been possible. Any thread."""
        with self._lock:
            return self._energy_j

    def power_w(self) -> float | None:
        """The latest power figure in watts if taken within the last GAP_INTERVALS intervals, else None."""
        with self._lock:
            p = self._power
            if p is None or self._clock() - p[0] > GAP_INTERVALS * self.interval_s:
                return None
            return p[1]

    @property
    def energy_source(self) -> str:
        """'counter' | 'power' | '' (not decided yet, or neither call works on this card / NVML is gone)."""
        with self._lock:
            return self._energy_source

    # ------------------------------------------------------------------ storage
    def _fresh_locked(self) -> bool:
        return bool(self._samples) and self._clock() - self._samples[-1][0] <= 3 * self.interval_s + 5.0

    def _store(self, used_mb: float, total_mb: float, source: str) -> None:
        now = self._clock()
        with self._lock:
            if source != self._source:
                self._samples.clear()               # never splice two sources into one line
                self._source = source
            self._samples.append((now, max(0.0, used_mb)))
            cutoff = now - self.window_s
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            self._total_mb = total_mb if total_mb > 0 else None
            self._available = self._total_mb is not None

    def push_external(self, used_mb: float, total_mb: float) -> bool:
        """Fallback feed (Strata's /metrics hardware block). Ignored while NVML itself is delivering."""
        if self._handle is not None:
            return False
        self._store(float(used_mb), float(total_mb), "strata")
        return True

    def snapshot(self) -> VramSnapshot:
        now = self._clock()
        with self._lock:
            cutoff = now - self.window_s
            samples = tuple(s for s in self._samples if s[0] >= cutoff)
            available = self._available
            if available and self._source == "strata" and not self._fresh_locked():
                available = False                   # Strata stopped feeding and NVML is still missing
            return VramSnapshot(samples, self._total_mb, available, self._source, now)

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hc-vram", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:  # noqa: BLE001 - the sampler must never die
                log.exception("VRAM sample failed")
            self._stop.wait(self.interval_s)
        self._shutdown()                            # NVML is released on the thread that used it

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        elif t is None:
            self._shutdown()
