"""Rolling GPU-memory history for the stats section (device-level NVML; no per-process data on WDDM).

`VramSampler` reads `nvmlDeviceGetMemoryInfo` every 2 s on its own daemon thread and keeps 5 minutes. If NVML is
missing or fails, `snapshot().available` is False and the UI shows "VRAM unavailable"; init is retried now and then
(driver updates restart NVML). `push_external` lets the app feed Strata's own GPU telemetry as a fallback.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("hermes_controller")

INTERVAL_S = 2.0
WINDOW_S = 300.0
RETRY_S = 60.0
_MB = 1024.0 * 1024.0


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
        return True

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
