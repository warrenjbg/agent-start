"""Strata `GET /metrics` (JSON) -> the series the stats section draws.

Strata returns only its newest 12 finished requests per poll (newest first), so `MetricsStore` accumulates them by
request id across polls and keeps the last 30. Pure data handling; `fetch_metrics` is the only I/O and is always
called from a worker thread.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.request
from collections import OrderedDict

log = logging.getLogger("hermes_controller")

MAX_REQUESTS = 30
MIN_PROMPT_TOKENS = 64        # fewer tokens read = a cache hit; its rate is noise that would blow up the scale
MIN_PROMPT_MS = 50.0
FETCH_TIMEOUT_S = 1.5
_MAX_BODY = 8 * 1024 * 1024
_MB = 1024.0 * 1024.0

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a system proxy


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def decode_rate(req: dict) -> float | None:
    """Decode tok/s of one finished request (Strata computes it; None for 0/1-token answers)."""
    v = _num(req.get("decode_tok_s"))
    return v if v is not None and v > 0 else None


def prompt_rate(req: dict) -> float | None:
    """Prompt-reading tok/s of one finished request: tokens actually read / prompt seconds.

    `prompt_read` is preferred; older engines do not report it, then the whole prompt minus the reused (cached)
    prefix stands in. None when the timing is missing/tiny or fewer than MIN_PROMPT_TOKENS were read.
    """
    ms = _num(req.get("prompt_ms"))
    if ms is None or ms < MIN_PROMPT_MS:
        return None
    read = _num(req.get("prompt_read"))
    if read is None:
        total = _num(req.get("prompt_total"))
        if total is None:
            return None
        read = total - (_num(req.get("reused")) or 0.0)
    if read < MIN_PROMPT_TOKENS:
        return None
    return read / (ms / 1000.0)


class MetricsStore:
    """Accumulated per-request speeds + the live values of the latest poll. Main-thread only (no locking)."""

    def __init__(self, max_requests: int = MAX_REQUESTS) -> None:
        self.max_requests = max(1, int(max_requests))
        self._reqs: "OrderedDict[str, tuple[float | None, float | None]]" = OrderedDict()
        self.live_state: str | None = None
        self.live_decode: float | None = None
        self.live_prompt: float | None = None
        self.model: str | None = None
        self.hw_vram: tuple[float, float] | None = None      # (used MB, total MB) from Strata's hardware block
        self.polls = 0
        self._read_prev: tuple[float, float, float] | None = None      # (clock, prompt_read, prompt_total)

    # ------------------------------------------------------------------ ingest
    def reset(self) -> None:
        self._reqs.clear()
        self.polls = 0
        self.model = None
        self.clear_live()

    def clear_live(self) -> None:
        self.live_state = None
        self.live_decode = None
        self.live_prompt = None
        self.hw_vram = None
        self._read_prev = None

    @staticmethod
    def _key(req: dict) -> str | None:
        rid = req.get("id")
        if rid not in (None, ""):
            return str(rid)
        t = _num(req.get("time"))
        return None if t is None else f"t{t!r}/{req.get('output_tokens')!r}"

    def ingest(self, data: dict | None, now: float | None = None) -> int:
        """Merge one /metrics document. Returns how many new finished requests it contained."""
        if not isinstance(data, dict):
            return 0
        self.polls += 1
        added = 0
        reqs = data.get("requests")
        if isinstance(reqs, list):
            for req in reversed(reqs):                       # Strata sends newest first; keep oldest -> newest
                if not isinstance(req, dict):
                    continue
                key = self._key(req)
                if key is None or key in self._reqs:
                    continue
                self._reqs[key] = (decode_rate(req), prompt_rate(req))
                added += 1
            while len(self._reqs) > self.max_requests:
                self._reqs.popitem(last=False)
        engine = data.get("engine")
        if isinstance(engine, dict) and isinstance(engine.get("model"), str):
            self.model = engine["model"]
        self._ingest_live(data.get("live"), _num(data.get("time")) if now is None else now)
        hw = data.get("hardware")
        used = _num(hw.get("gpu_mem_used")) if isinstance(hw, dict) else None
        total = _num(hw.get("gpu_mem_total")) if isinstance(hw, dict) else None
        self.hw_vram = (used / _MB, total / _MB) if used is not None and total and total > 0 else None
        return added

    def _ingest_live(self, live, clock: float | None) -> None:
        self.live_decode = self.live_prompt = None
        if not isinstance(live, dict):
            self.live_state, self._read_prev = None, None
            return
        state = live.get("state")
        self.live_state = state if isinstance(state, str) else None
        if state == "generating":
            v = _num(live.get("tok_s"))
            if v is None or v <= 0:
                v = _num(live.get("tok_s_mean"))
            self.live_decode = v if v is not None and v > 0 else None
        if state != "reading":
            self._read_prev = None
            return
        v = _num(live.get("prefill_tok_s_mean"))
        read, total = _num(live.get("prompt_read")), _num(live.get("prompt_total"))
        if (v is None or v <= 0) and read is not None and clock is not None:
            prev = self._read_prev                           # rate from the read progress between two polls
            if prev is not None and prev[2] == (total or 0.0) and clock - prev[0] > 0.2 and read > prev[1]:
                v = (read - prev[1]) / (clock - prev[0])
        if read is not None and clock is not None:
            if self._read_prev is None or self._read_prev[2] != (total or 0.0) or read != self._read_prev[1]:
                self._read_prev = (clock, read, total or 0.0)
        self.live_prompt = v if v is not None and v > 0 else None

    # ------------------------------------------------------------------ views
    def __len__(self) -> int:
        return len(self._reqs)

    def decode_series(self) -> list[float]:
        """Decode tok/s per finished request, oldest -> newest (requests without a rate are skipped)."""
        return [d for d, _p in self._reqs.values() if d is not None]

    def prompt_series(self) -> list[float]:
        """Prompt-reading tok/s per finished request, oldest -> newest (cache hits skipped)."""
        return [p for _d, p in self._reqs.values() if p is not None]


def fetch_metrics(root_url: str, timeout: float = FETCH_TIMEOUT_S) -> dict | None:
    """GET <root>/metrics as JSON, or None on any failure. Blocking (<= timeout): worker threads only.

    Strata answers JSON unless the Accept header mentions text/plain or openmetrics (then Prometheus text).
    """
    url = str(root_url).rstrip("/") + "/metrics"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            raw = resp.read(_MAX_BODY + 1)
        if len(raw) > _MAX_BODY:
            return None
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - URLError, timeouts, resets, bad JSON
        log.debug("GET %s failed: %s", url, exc)
        return None
    return data if isinstance(data, dict) else None
