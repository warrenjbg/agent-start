"""Strata `GET /metrics` (JSON) -> the series the stats section draws.

Strata returns only its newest 12 finished requests per poll (newest first), so `MetricsStore` accumulates them by
request id across polls and keeps the last 30. `fetch_metrics(..., all_requests=True)` asks for every request Strata
keeps (`?requests=all`, up to 500) - the catch-up after the section was collapsed. Pure data handling; `fetch_metrics` is the only I/O and is always
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
MIN_PROMPT_TOKENS = 256       # fewer tokens read = a cache hit / fixed-overhead-dominated read: noisy, low bars
MIN_DECODE_TOKENS = 16        # shorter answers (titles, 1-10 tokens) report inflated decode rates (1 token: 1,250 tok/s)
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
    """Decode tok/s of one finished request (Strata computes it; None for 0/1-token answers).

    None as well when fewer than MIN_DECODE_TOKENS were generated (`engine_generated`, else `output_tokens`): the
    fixed per-request overhead makes such tiny answers' rates meaningless spikes. Unknown count -> Strata's figure.
    """
    v = _num(req.get("decode_tok_s"))
    if v is None or v <= 0:
        return None
    n = _num(req.get("engine_generated"))
    if n is None:
        n = _num(req.get("output_tokens"))
    if n is not None and n < MIN_DECODE_TOKENS:
        return None
    return v


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
        # key -> (sort key (time, id), decode rate, prompt rate); kept sorted oldest -> newest
        self._reqs: "OrderedDict[str, tuple[tuple[float, str], float | None, float | None]]" = OrderedDict()
        self.live_state: str | None = None
        self.live: dict = {}                         # what the model is doing now (see activity())
        self.live_decode: float | None = None
        self.live_prompt: float | None = None
        self.model: str | None = None
        self.hw_vram: tuple[float, float] | None = None      # (used MB, total MB) from Strata's hardware block
        self.polls = 0
        self._read_prev: tuple[float, float, float] | None = None      # (clock, prompt_read, prompt_total)
        self._read_seen: tuple[float | None, float | None] | None = None  # (prompt_read, prompt_total) of the last
        self._read_fresh = False                     # reading poll; fresh = progress moved, so the mean is current

    # ------------------------------------------------------------------ ingest
    def reset(self) -> None:
        self._reqs.clear()
        self.polls = 0
        self.model = None
        self.clear_live()

    def clear_live(self) -> None:
        self.live_state = None
        self.live = {}
        self.live_decode = None
        self.live_prompt = None
        self.hw_vram = None
        self._read_prev = None
        self._read_seen = None
        self._read_fresh = False

    @staticmethod
    def _key(req: dict) -> str | None:
        rid = req.get("id")
        if rid not in (None, ""):
            return str(rid)
        t = _num(req.get("time"))
        return None if t is None else f"t{t!r}/{req.get('output_tokens')!r}"

    @staticmethod
    def _order(req: dict, key: str) -> tuple[float, str]:
        t = _num(req.get("time"))
        return (t if t is not None else -math.inf, key)

    def ingest(self, data: dict | None, now: float | None = None) -> int:
        """Merge one /metrics document (the default 12 or a `?requests=all` list of any length).

        Returns how many new finished requests it contributed to the kept window. A document from a different
        model than the previous one (engine.model changed: another server / a model switch) resets the history
        first, so stale bars never mix with the new model's.
        """
        if not isinstance(data, dict):
            return 0
        engine = data.get("engine")
        model = engine.get("model") if isinstance(engine, dict) else None
        if isinstance(model, str):
            if self.model is not None and model != self.model:
                log.debug("metrics: model changed %r -> %r, history reset", self.model, model)
                self.reset()
            self.model = model
        self.polls += 1
        new: set[str] = set()
        reqs = data.get("requests")
        if isinstance(reqs, list):
            for req in reqs:
                if not isinstance(req, dict):
                    continue
                key = self._key(req)
                if key is None or key in self._reqs:
                    continue
                self._reqs[key] = (self._order(req, key), decode_rate(req), prompt_rate(req))
                new.add(key)
            if new:                                          # newest max_requests by (time, id), oldest -> newest
                kept = sorted(self._reqs.items(), key=lambda kv: kv[1][0])[-self.max_requests:]
                self._reqs = OrderedDict(kept)
        added = sum(1 for k in new if k in self._reqs)
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
        self.live = {"state": self.live_state, "queued": int(_num(live.get("queued")) or 0),
                     "phase": live.get("phase") if isinstance(live.get("phase"), str) else None,
                     **{k: _num(live.get(k)) for k in ("prompt_read", "prompt_total", "prompt_tokens", "generated",
                                                       "max_tokens")}}
        if state == "generating":                            # parallel mode forces this with tok_s None: stays blank
            v = _num(live.get("tok_s"))
            if v is None or v <= 0:
                v = _num(live.get("tok_s_mean"))
            self.live_decode = v if v is not None and v > 0 else None
        if state != "reading":
            self._read_prev = None
            self._read_seen, self._read_fresh = None, False
            return
        read, total = _num(live.get("prompt_read")), _num(live.get("prompt_total"))
        # On Strata's batched path the engine's prefill_tok_s_mean (and progress) keep the previous request's values
        # until the new prompt's first progress line: trust the mean only once prompt_read/total moved between two
        # reading polls (a new progress line arrived, which also set the mean).
        seen = (read, total)
        if read is not None and self._read_seen is not None and seen != self._read_seen:
            self._read_fresh = True
        self._read_seen = seen
        v = _num(live.get("prefill_tok_s_mean")) if self._read_fresh else None
        if (v is None or v <= 0) and read is not None and clock is not None:
            prev = self._read_prev                           # rate from the read progress between two polls
            if prev is not None and prev[2] == (total or 0.0) and clock - prev[0] > 0.2 and read > prev[1]:
                v = (read - prev[1]) / (clock - prev[0])
        if read is not None and clock is not None:
            if self._read_prev is None or self._read_prev[2] != (total or 0.0) or read != self._read_prev[1]:
                self._read_prev = (clock, read, total or 0.0)
        self.live_prompt = v if v is not None and v > 0 else None

    # ------------------------------------------------------------------ views
    def activity(self) -> dict | None:
        """What the model is doing right now, from the latest /metrics `live` block (Strata's Monitor "Model state"),
        or None when it is idle / unknown. Keys: kind ('reading' | 'generating' | 'queued'), queued (int),
        frac (0..1 or None: prompt read progress), read, total, tokens, rate (tok/s or None), phase (str or None)."""
        lv = self.live or {}
        kind = lv.get("state")
        queued = int(lv.get("queued") or 0)
        if kind == "reading":
            read, total = lv.get("prompt_read"), lv.get("prompt_total")
            frac = min(1.0, max(0.0, read / total)) if read is not None and total else None
            return {"kind": "reading", "queued": queued, "frac": frac, "read": read, "total": total,
                    "tokens": lv.get("prompt_tokens"), "rate": self.live_prompt, "phase": None}
        if kind == "generating":
            return {"kind": "generating", "queued": queued, "frac": None, "read": None, "total": None,
                    "tokens": lv.get("generated"), "rate": self.live_decode, "phase": lv.get("phase")}
        if kind == "idle" and queued > 0:
            return {"kind": "queued", "queued": queued, "frac": None, "read": None, "total": None, "tokens": None,
                    "rate": None, "phase": None}
        return None

    def __len__(self) -> int:
        return len(self._reqs)

    def decode_series(self) -> list[float]:
        """Decode tok/s per finished request, oldest -> newest (requests without a rate are skipped)."""
        return [d for _o, d, _p in self._reqs.values() if d is not None]

    def prompt_series(self) -> list[float]:
        """Prompt-reading tok/s per finished request, oldest -> newest (cache hits skipped)."""
        return [p for _o, _d, p in self._reqs.values() if p is not None]


def fetch_metrics(root_url: str, timeout: float = FETCH_TIMEOUT_S, all_requests: bool = False) -> dict | None:
    """GET <root>/metrics as JSON, or None on any failure. Blocking (<= timeout): worker threads only.

    Strata answers JSON unless the Accept header mentions text/plain or openmetrics (then Prometheus text).
    all_requests=True adds `?requests=all` (Strata 0.2.10 serve/server.py: `svc.metrics(all_requests="requests=all"
    in self.path)`): every finished request it keeps (up to 500) instead of the newest 12 - the catch-up poll.
    """
    url = str(root_url).rstrip("/") + "/metrics" + ("?requests=all" if all_requests else "")
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
