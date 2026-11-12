"""Engine metrics -> the series the stats section draws, plus Strata's side of it.

Two layers:
  engine-neutral   `MetricsStore` only ever sees `engines.base.MetricsSample` objects: it keeps the last 30 finished
                   requests by key, the live values of the latest poll, `activity()` for the status line.
  Strata           `strata_sample()` turns one Strata `GET /metrics` JSON document into a MetricsSample (decode_rate /
                   prompt_rate clean the per-request speeds); `StrataPoller` fetches it (+ GET /status when a request
                   just ended) and is what `engines.strata.StrataEngine.metrics_poller()` returns.

Strata returns only its newest 12 finished requests per poll (newest first), so `MetricsStore` accumulates them by
request id across polls and keeps the last 30. `fetch_metrics(..., all_requests=True)` asks for every request Strata
keeps (`?requests=all`, up to 500) - the catch-up after the section was collapsed. `fetch_metrics` / `fetch_status`
are the only I/O and are always called from a worker thread.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.request
from collections import OrderedDict
from dataclasses import replace
from typing import Any, Callable

from engines.base import LiveSample, MetricsPoller, MetricsSample, RequestEnd, RequestSample

log = logging.getLogger("agent_start")

MAX_REQUESTS = 30
MIN_PROMPT_TOKENS = 256       # fewer tokens read = a cache hit / fixed-overhead-dominated read: noisy, low bars
MIN_DECODE_TOKENS = 16        # shorter answers (titles, 1-10 tokens) report inflated decode rates (1 token: 1,250 tok/s)
MIN_PROMPT_MS = 50.0
FETCH_TIMEOUT_S = 1.5
_MAX_BODY = 8 * 1024 * 1024
_MB = 1024.0 * 1024.0

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a system proxy


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def decode_rate(req: dict[str, Any]) -> float | None:
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


def prompt_rate(req: dict[str, Any]) -> float | None:
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


# ------------------------------------------------------------------------------------------------ Strata converter
_TOOL_WRITING = "writing a tool call"     # Strata phase while the model writes a tool call ("...: <name>" when known)
_TOOL_DONE = "tool call complete"         # Strata phase once that call is written (the request ends in it)


def _req_key(req: dict[str, Any]) -> str | None:
    """A Strata request's identity across polls: its id, else (time, output_tokens); None = cannot be tracked."""
    rid = req.get("id")
    if rid not in (None, ""):
        return str(rid)
    t = _num(req.get("time"))
    return None if t is None else f"t{t!r}/{req.get('output_tokens')!r}"


def _phase_tool(phase: Any) -> tuple[bool, str | None]:
    """(ends in a tool call, tool name) from one Strata phase string: 'writing a tool call[: <name>]' or
    'tool call complete' -> True; the name is the text after the first ':' (None when empty / absent)."""
    if not isinstance(phase, str) or not (phase.startswith(_TOOL_WRITING) or phase == _TOOL_DONE):
        return False, None
    name = phase.split(":", 1)[1].strip() if ":" in phase else ""
    return True, name or None


def strata_request_end(phase: Any, tool: Any = None) -> RequestEnd:
    """How a request ended, from Strata's GET /status `phase` / `tool` (read while idle: the last request's final
    phase). A named `tool` wins over the name in the phase text."""
    tool_call, name = _phase_tool(phase)
    if not tool_call:
        return RequestEnd(tool_call=False)
    return RequestEnd(tool_call=True, tool=tool if isinstance(tool, str) and tool else name)


def _strata_live(live: Any) -> LiveSample | None:
    if not isinstance(live, dict):
        return None
    state, phase = live.get("state"), live.get("phase")
    phase = phase if isinstance(phase, str) else None
    tool_call, tool = _phase_tool(phase)
    tok_s = _num(live.get("tok_s"))
    return LiveSample(
        state=state if isinstance(state, str) else None,
        queued=int(_num(live.get("queued")) or 0),
        phase=phase, in_tool_call=tool_call, tool=tool,
        prompt_read=_num(live.get("prompt_read")), prompt_total=_num(live.get("prompt_total")),
        prompt_tokens=_num(live.get("prompt_tokens")), generated=_num(live.get("generated")),
        max_tokens=_num(live.get("max_tokens")),
        decode_tps=tok_s if tok_s is not None and tok_s > 0 else None,
        decode_tps_mean=_num(live.get("tok_s_mean")),
        prompt_tps_mean=_num(live.get("prefill_tok_s_mean")))


def _count(*values: Any) -> int | None:
    """The first value that is a whole, non-negative token count (None when none is)."""
    for v in values:
        n = _num(v)
        if n is not None and n >= 0:
            return int(n)
    return None


def strata_sample(doc: Any) -> MetricsSample | None:
    """One Strata GET /metrics JSON document -> MetricsSample (None for a non-dict). Pure; never raises on junk.

    A `_last_end` key ({"phase", "tool"} from GET /status, put there by a caller) becomes `last_end`."""
    if not isinstance(doc, dict):
        return None
    engine = doc.get("engine")
    model = engine.get("model") if isinstance(engine, dict) else None
    requests: list[RequestSample] = []
    reqs = doc.get("requests")
    if isinstance(reqs, list):
        for req in reqs:
            if not isinstance(req, dict):
                continue
            key = _req_key(req)
            if key is None:
                continue
            requests.append(RequestSample(key=key, time=_num(req.get("time")), decode_tps=decode_rate(req),
                                          prompt_tps=prompt_rate(req), prompt_tokens=_count(req.get("prompt_total")),
                                          generated_tokens=_count(req.get("engine_generated"),
                                                                  req.get("output_tokens"))))
    hw = doc.get("hardware")
    used = _num(hw.get("gpu_mem_used")) if isinstance(hw, dict) else None
    total = _num(hw.get("gpu_mem_total")) if isinstance(hw, dict) else None
    end = doc.get("_last_end")
    return MetricsSample(
        model=model if isinstance(model, str) else None,
        requests=tuple(requests),
        live=_strata_live(doc.get("live")),
        clock=_num(doc.get("time")),
        vram_mb=(used / _MB, total / _MB) if used is not None and total and total > 0 else None,
        last_end=strata_request_end(end.get("phase"), end.get("tool")) if isinstance(end, dict) else None)


# ------------------------------------------------------------------------------------------------ neutral store
class MetricsStore:
    """Accumulated per-request speeds + the live values of the latest poll. Main-thread only (no locking).

    Engine-neutral: it only reads MetricsSample fields (a Strata /metrics dict passed to ingest() is converted
    with strata_sample() first)."""

    def __init__(self, max_requests: int = MAX_REQUESTS) -> None:
        self.max_requests = max(1, int(max_requests))
        # key -> (sort key (time, key), decode rate, prompt rate); kept sorted oldest -> newest
        self._reqs: "OrderedDict[str, tuple[tuple[float, str], float | None, float | None]]" = OrderedDict()
        self.live_state: str | None = None
        self.live: dict = {}                         # what the model is doing now (see activity())
        self.live_decode: float | None = None
        self.live_prompt: float | None = None
        self.model: str | None = None
        self.hw_vram: tuple[float, float] | None = None      # (used MB, total MB) as the engine reports the GPU
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

    def ingest(self, data: MetricsSample | dict | None, now: float | None = None) -> int:
        """Merge one poll: a MetricsSample, or a Strata /metrics document (converted with strata_sample) - the
        default 12 requests or a `?requests=all` list of any length.

        Returns how many new finished requests it contributed to the kept window. A sample from a different model
        than the previous one (another server / a model switch) resets the history first, so stale bars never mix
        with the new model's. `now`: the clock for the live reading rate (default: the sample's own clock).
        """
        sample = data if isinstance(data, MetricsSample) else strata_sample(data)
        if sample is None:
            return 0
        model = sample.model
        if isinstance(model, str):
            if self.model is not None and model != self.model:
                log.debug("metrics: model changed %r -> %r, history reset", self.model, model)
                self.reset()
            self.model = model
        self.polls += 1
        new: set[str] = set()
        for r in sample.requests or ():
            key = getattr(r, "key", None)
            if not isinstance(key, str) or not key or key in self._reqs:
                continue
            t = _num(r.time)
            self._reqs[key] = ((t if t is not None else -math.inf, key), _num(r.decode_tps), _num(r.prompt_tps))
            new.add(key)
        if new:                                              # newest max_requests by (time, key), oldest -> newest
            kept = sorted(self._reqs.items(), key=lambda kv: kv[1][0])[-self.max_requests:]
            self._reqs = OrderedDict(kept)
        added = sum(1 for k in new if k in self._reqs)
        self._ingest_live(sample.live, _num(sample.clock) if now is None else now)
        self.hw_vram = self._vram(sample.vram_mb)
        return added

    @staticmethod
    def _vram(v: Any) -> tuple[float, float] | None:
        if not isinstance(v, tuple) or len(v) != 2:
            return None
        used, total = _num(v[0]), _num(v[1])
        return (used, total) if used is not None and total and total > 0 else None

    def _ingest_live(self, live: LiveSample | None, clock: float | None) -> None:
        self.live_decode = self.live_prompt = None
        if not isinstance(live, LiveSample):
            self.live_state, self._read_prev = None, None
            return
        state = live.state if isinstance(live.state, str) else None
        self.live_state = state
        phase = live.phase if isinstance(live.phase, str) else None
        self.live = {"state": state, "queued": int(_num(live.queued) or 0), "phase": phase,
                     "prompt_read": _num(live.prompt_read), "prompt_total": _num(live.prompt_total),
                     "prompt_tokens": _num(live.prompt_tokens), "generated": _num(live.generated),
                     "max_tokens": _num(live.max_tokens), "in_tool_call": live.in_tool_call is True,
                     "tool": live.tool if isinstance(live.tool, str) and live.tool else None}
        if state == "generating":                            # parallel mode forces this with no rate: stays blank
            v = _num(live.decode_tps)
            if v is None or v <= 0:
                v = _num(live.decode_tps_mean)
            self.live_decode = v if v is not None and v > 0 else None
        if state != "reading":
            self._read_prev = None
            self._read_seen, self._read_fresh = None, False
            return
        read, total = self.live["prompt_read"], self.live["prompt_total"]
        # On Strata's batched path the engine's prefill_tok_s_mean (and progress) keep the previous request's values
        # until the new prompt's first progress line: trust the mean only once prompt_read/total moved between two
        # reading polls (a new progress line arrived, which also set the mean).
        seen = (read, total)
        if read is not None and self._read_seen is not None and seen != self._read_seen:
            self._read_fresh = True
        self._read_seen = seen
        v = _num(live.prompt_tps_mean) if self._read_fresh else None
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
        """What the model is doing right now, from the latest poll's live values (Strata's Monitor "Model state"),
        or None when it is idle / unknown. Keys: kind ('reading' | 'generating' | 'queued'), queued (int),
        frac (0..1 or None: prompt read progress), read, total, tokens, rate (tok/s or None), phase (str or None),
        tool_call (bool: the model is writing a tool call, the request will end in it), tool (its name or None)."""
        lv = self.live or {}
        kind = lv.get("state")
        queued = int(lv.get("queued") or 0)
        if kind == "reading":
            read, total = lv.get("prompt_read"), lv.get("prompt_total")
            frac = min(1.0, max(0.0, read / total)) if read is not None and total else None
            return {"kind": "reading", "queued": queued, "frac": frac, "read": read, "total": total,
                    "tokens": lv.get("prompt_tokens"), "rate": self.live_prompt, "phase": None,
                    "tool_call": False, "tool": None}
        if kind == "generating":
            tool_call = lv.get("in_tool_call") is True
            return {"kind": "generating", "queued": queued, "frac": None, "read": None, "total": None,
                    "tokens": lv.get("generated"), "rate": self.live_decode, "phase": lv.get("phase"),
                    "tool_call": tool_call, "tool": lv.get("tool") if tool_call else None}
        if kind == "idle" and queued > 0:
            return {"kind": "queued", "queued": queued, "frac": None, "read": None, "total": None, "tokens": None,
                    "rate": None, "phase": None, "tool_call": False, "tool": None}
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


def fetch_status(root_url: str, timeout: float = FETCH_TIMEOUT_S) -> dict | None:
    """GET <root>/status as JSON, or None on any failure (401 with an API key, timeouts, ...). Worker threads only.

    Strata 0.2.10 (serve/server.py, `path == "/status"`): what the model is doing. While idle (non-parallel mode) it
    still holds the LAST request's final `phase` ("answering", "thinking", "tool call complete", "writing a tool
    call: <name>") - how that request ended, which tells whether an agent will send another one.
    """
    url = str(root_url).rstrip("/") + "/status"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            raw = resp.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return None
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        log.debug("GET %s failed: %s", url, exc)
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------------------------------------ Strata poller
def _no_status(_root_url: str) -> dict | None:
    return None


class StrataPoller(MetricsPoller):
    """GET /metrics once per poll; when Strata's finished-request counter (totals.requests) grew since the previous
    poll and the (non-parallel) server is idle, GET /status for how that request ended - Strata keeps the last
    request's final phase there while idle ("tool call complete" = the agent comes back), which a 1 s sample of the
    short tool-call phase misses most of the time.

    fetch_metrics(root_url[, all_requests=True]) / fetch_status(root_url) are test seams (None = the real HTTP
    fetchers). A fake fetch_metrics without a fetch_status gets NO /status (never the real HTTP one)."""

    def __init__(self, fetch_metrics: Callable[..., Any] | None = None,
                 fetch_status: Callable[[str], Any] | None = None) -> None:
        self._fetch_metrics = fetch_metrics
        if fetch_status is not None:
            self._fetch_status: Callable[[str], Any] | None = fetch_status
        else:
            self._fetch_status = None if fetch_metrics is None else _no_status   # None = the module's fetch_status
        self._done_n: int | None = None          # Strata's finished-request count at the previous poll

    def reset(self, log_path: str | None = None) -> None:
        self._done_n = None

    def poll(self, root_url: str, catch_up: bool = False) -> MetricsSample | None:
        if not root_url:
            return None
        fm = self._fetch_metrics or fetch_metrics
        try:
            data = fm(root_url, all_requests=True) if catch_up else fm(root_url)
        except Exception:  # noqa: BLE001 - a broken fetcher is "no answer", never an exception in the worker
            log.debug("metrics fetch failed", exc_info=True)
            return None
        if not isinstance(data, dict):
            return None
        end: RequestEnd | None = None
        totals, live = data.get("totals"), data.get("live")
        n = totals.get("requests") if isinstance(totals, dict) else None
        live = live if isinstance(live, dict) else {}
        if isinstance(n, int) and self._done_n is not None and n > self._done_n and not live.get("parallel") \
                and live.get("state") == "idle":
            fs = self._fetch_status or fetch_status
            try:
                st = fs(root_url)
            except Exception:  # noqa: BLE001
                log.debug("status fetch failed", exc_info=True)
                st = None
            if isinstance(st, dict) and not st.get("busy"):
                end = strata_request_end(st.get("phase"), st.get("tool"))
        if isinstance(n, int):
            self._done_n = n
        sample = strata_sample(data)
        if sample is None:                       # unreachable for a dict; keeps the type checker honest
            return None
        return replace(sample, last_end=end) if end is not None else sample
