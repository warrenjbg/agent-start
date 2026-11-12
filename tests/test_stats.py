r"""Unit tests for the stats data path: metrics_store (accumulation, rate math, fetch), vram_sampler, graph helpers.

Run: .venv\Scripts\python.exe tests\test_stats.py
The HTTP part talks ONLY to tests/fixtures/fake_strata_server.py on a free random port (never 8080).
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import metrics_store as ms  # noqa: E402
import vram_sampler as vs  # noqa: E402
from metrics_store import MetricsStore  # noqa: E402

PASSES, FAILS = 0, []


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"[PASS] {msg}")
    else:
        FAILS.append(msg)
        print(f"[FAIL] {msg}")


def req(i: int, **kw) -> dict:
    r = {"id": f"r{i:04d}", "time": 1000.0 + i, "duration_s": 2.0, "finish": "stop", "prompt_tokens": 2000,
         "reused": 0, "output_tokens": 200, "prompt_total": 2000, "prompt_read": 2000, "prompt_ms": 500.0,
         "decode_ms": 2000.0, "decode_tok_s": 100.0 + i}
    r.update(kw)
    return r


def doc(reqs: list[dict], live: dict | None = None, **kw) -> dict:
    d = {"engine": {"model": "m"}, "live": live or {"state": "idle"}, "requests": reqs[::-1][:12],
         "requests_kept": len(reqs), "totals": {}, "hardware": {}, "time": 5000.0}
    d.update(kw)
    return d


# ------------------------------------------------------------------------------------------ rate math
def test_rates() -> None:
    check(ms.prompt_rate(req(0)) == 4000.0, "prompt rate = prompt_read / (prompt_ms / 1000)")
    check(ms.prompt_rate(req(0, prompt_read=1000, prompt_total=9000)) == 2000.0, "prompt_read wins over prompt_total")
    check(ms.prompt_rate(req(0, prompt_read=None, prompt_total=3000)) == 6000.0, "fallback: prompt_total")
    check(ms.prompt_rate(req(0, prompt_read=None, prompt_total=3000, reused=2000)) == 2000.0,
          "fallback subtracts the reused (cached) prefix")
    check(ms.prompt_rate(req(0, prompt_read=None, prompt_total=3000, reused=2990)) is None,
          "fallback cache hit (10 tokens read) skipped")
    check(ms.prompt_rate(req(0, prompt_read=12, prompt_ms=9.0)) is None, "cache hit: tiny read + tiny time skipped")
    check(ms.MIN_PROMPT_TOKENS == 256 and ms.prompt_rate(req(0, prompt_read=255)) is None
          and ms.prompt_rate(req(0, prompt_read=256)) == 512.0, "threshold: < 256 tokens read skipped, 256 kept")
    check(ms.prompt_rate(req(0, prompt_read=120, prompt_ms=80.0)) is None
          and ms.prompt_rate(req(0, prompt_read=None, prompt_total=2400, reused=2200)) is None,
          "small fresh reads (overhead-dominated) skipped, also via the prompt_total - reused fallback")
    check(ms.prompt_rate(req(0, prompt_ms=None)) is None and ms.prompt_rate(req(0, prompt_ms=0)) is None
          and ms.prompt_rate(req(0, prompt_ms=3.0)) is None, "missing / zero / tiny prompt_ms skipped (no spike)")
    check(ms.prompt_rate(req(0, prompt_read=None, prompt_total=None)) is None, "no token counts -> None")
    check(ms.prompt_rate(req(0, prompt_ms="500", prompt_read=True)) is None, "non-numeric fields -> None")
    check(ms.prompt_rate({}) is None and ms.decode_rate({}) is None, "empty record -> None")
    check(ms.decode_rate(req(3)) == 103.0 and ms.decode_rate(req(0, decode_tok_s=None)) is None
          and ms.decode_rate(req(0, decode_tok_s=0)) is None and ms.decode_rate(req(0, decode_tok_s=float("nan"))) is None,
          "decode rate: Strata's figure; None / 0 / NaN skipped")
    check(ms.MIN_DECODE_TOKENS == 16 and ms.decode_rate(req(0, engine_generated=2, output_tokens=2, decode_tok_s=1250.0))
          is None and ms.decode_rate(req(0, engine_generated=15, decode_tok_s=400.0)) is None
          and ms.decode_rate(req(0, engine_generated=16, decode_tok_s=96.0)) == 96.0,
          "decode: tiny answers (< 16 generated, e.g. title generation) skipped, 16 kept")
    check(ms.decode_rate(req(0, engine_generated=None, output_tokens=8, decode_tok_s=300.0)) is None
          and ms.decode_rate(req(0, engine_generated=None, output_tokens=40, decode_tok_s=97.0)) == 97.0
          and ms.decode_rate(req(0, engine_generated=40, output_tokens=8, decode_tok_s=97.0)) == 97.0,
          "decode: engine_generated preferred, output_tokens as fallback")
    check(ms.decode_rate({"decode_tok_s": 88.0}) == 88.0, "decode: unknown token count keeps Strata's figure")


# ------------------------------------------------------------------------------------------ accumulation
def test_accumulation() -> None:
    st = MetricsStore()
    allr = [req(i) for i in range(5)]
    check(st.ingest(doc(allr)) == 5 and st.decode_series() == [100.0, 101.0, 102.0, 103.0, 104.0],
          "first poll: oldest -> newest order")
    check(st.ingest(doc(allr)) == 0 and len(st) == 5, "same poll again adds nothing (dedupe by id)")
    allr += [req(i) for i in range(5, 20)]                    # 15 new, but Strata only returns the newest 12
    added = st.ingest(doc(allr))
    check(added == 12 and len(st) == 17, f"only the 12 returned are new ({added}, kept {len(st)})")
    check(st.decode_series()[:5] == [100.0, 101.0, 102.0, 103.0, 104.0] and st.decode_series()[5] == 108.0,
          "the 3 requests that fell out of Strata's window between polls are simply absent")
    for n in range(20, 60, 6):                                # steady polling, 6 new per poll
        allr += [req(i) for i in range(n, n + 6)]
        st.ingest(doc(allr))
    series = st.decode_series()
    check(len(st) == 30 and series[-1] == 100.0 + 61 and series == sorted(series),
          f"accumulates past Strata's 12 and keeps the last 30 ({len(st)}, newest {series[-1]})")
    check(series[0] == 100.0 + 32, f"oldest kept is the 30th newest ({series[0]})")

    st = MetricsStore(max_requests=4)
    mixed = [req(0), req(1, decode_tok_s=None), req(2, prompt_read=10, prompt_ms=5.0), req(3)]
    st.ingest(doc(mixed))
    check(st.decode_series() == [100.0, 102.0, 103.0] and st.prompt_series() == [4000.0, 4000.0, 4000.0],
          "per-series skipping: no-decode answers and cache hits leave no gap")
    st.ingest(doc(mixed + [req(4), {"time": 2000.0, "output_tokens": 50, "decode_tok_s": 50.0}, "junk", {"x": 1}]))
    check(len(st) == 4 and st.decode_series()[-1] == 50.0, "id-less record keyed by time; junk entries ignored")
    check(st.ingest(None) == 0 and st.ingest("nope") == 0 and st.ingest({"requests": "x"}) == 0,
          "malformed documents never raise")
    st.reset()
    check(len(st) == 0 and st.decode_series() == [] and st.polls == 0, "reset()")


def test_live() -> None:
    st = MetricsStore()
    st.ingest(doc([], live={"state": "generating", "tok_s": 101.3, "tok_s_mean": 99.0, "prefill_tok_s_mean": 3600.0}))
    check(st.live_decode == 101.3 and st.live_prompt is None and st.live_state == "generating",
          "generating: live decode = live.tok_s; no prompt value")
    st.ingest(doc([], live={"state": "generating", "tok_s": 0.0, "tok_s_mean": 88.0}))
    check(st.live_decode == 88.0, "tok_s still 0 at the first token -> tok_s_mean")
    st.ingest(doc([], live={"state": "idle", "tok_s": 55.0}))
    check(st.live_decode is None, "tok_s ignored unless state == generating")
    st.ingest(doc([], live={"state": "reading", "prefill_tok_s_mean": 3650.0, "prompt_read": 900, "prompt_total": 2400}))
    check(st.live_prompt is None and st.live_decode is None, "reading, first poll: the mean may be stale -> blank")
    st.ingest(doc([], live={"state": "reading", "prefill_tok_s_mean": 3650.0, "prompt_read": 1800, "prompt_total": 2400}))
    check(st.live_prompt == 3650.0 and st.live_decode is None, "reading, progress moved: prefill_tok_s_mean")
    st.ingest(doc([], live={"state": "reading", "prefill_tok_s_mean": 3640.0, "prompt_read": 1800, "prompt_total": 2400}))
    check(st.live_prompt == 3640.0, "reading, progress stalled but already fresh: mean still shown")
    st = MetricsStore()
    st.ingest(doc([], live={"state": "reading", "prefill_tok_s_mean": None, "prompt_read": 1000, "prompt_total": 9000}),
              now=10.0)
    check(st.live_prompt is None, "reading without a mean: first poll has no rate yet")
    st.ingest(doc([], live={"state": "reading", "prefill_tok_s_mean": 0.0, "prompt_read": 7000, "prompt_total": 9000}),
              now=12.0)
    check(st.live_prompt == 3000.0, f"reading: rate from prompt_read progress between polls ({st.live_prompt})")
    st.ingest(doc([], live={"state": "reading", "prompt_read": 500, "prompt_total": 4000}), now=14.0)
    check(st.live_prompt is None, "a new prompt (different total) restarts the progress rate")
    no_live = doc([])
    no_live["live"] = None
    st.ingest(no_live, now=16.0)
    check(st.live_state is None and st.live_prompt is None, "missing live block")
    st.ingest(doc([], hardware={"gpu_mem_used": 8 * 1024 ** 3, "gpu_mem_total": 16 * 1024 ** 3}))
    check(st.hw_vram == (8192.0, 16384.0) and st.model == "m", "hardware block -> VRAM fallback in MB; model")
    st.clear_live()
    check(st.hw_vram is None and st.live_state is None, "clear_live()")


def test_stale_prefill_mean() -> None:
    """Batched path: the engine keeps the previous request's prefill_tok_s_mean and progress until the new prompt's
    first progress line - ignore the mean until prompt_read/total moved between two reading polls."""
    st = MetricsStore()
    gen = {"state": "generating", "tok_s": 98.0, "tok_s_mean": 97.0}
    stale = {"state": "reading", "prefill_tok_s_mean": 5100.0, "prompt_read": 3000, "prompt_total": 3000}
    st.ingest(doc([], live=gen), now=1.0)
    st.ingest(doc([], live=stale), now=3.0)
    check(st.live_prompt is None, "new request, stale progress + mean from the previous request: blank")
    st.ingest(doc([], live=stale), now=5.0)
    check(st.live_prompt is None, "progress unchanged since the previous poll: still blank")
    st.ingest(doc([], live={"state": "reading", "prefill_tok_s_mean": 3300.0, "prompt_read": 2000,
                            "prompt_total": 9000}), now=7.0)
    check(st.live_prompt == 3300.0, f"first real progress line of the new prompt: mean shown ({st.live_prompt})")
    st.ingest(doc([], live=gen), now=9.0)
    st.ingest(doc([], live={"state": "reading", "prefill_tok_s_mean": 3300.0, "prompt_read": None,
                            "prompt_total": None}), now=11.0)
    st.ingest(doc([], live={"state": "reading", "prefill_tok_s_mean": 3300.0, "prompt_read": None,
                            "prompt_total": None}), now=13.0)
    check(st.live_prompt is None, "leaving 'reading' re-arms the check; no progress at all -> mean never trusted")


def test_parallel_live() -> None:
    """Strata --parallel: live.state is forced to 'generating' while tok_s / tok_s_mean are None."""
    st = MetricsStore()
    par = {"state": "generating", "queued": 0, "phase": None, "tok_s": None, "tok_s_mean": None,
           "prefill_tok_s_mean": None, "prompt_read": None, "prompt_total": None, "parallel": 4, "running": 2,
           "slots": [{"state": "decode"}, {"state": "prefill"}, {"state": "idle"}], "outside_slots": 0,
           "waiting": 0, "admitting": 0}
    try:
        added = st.ingest(doc([req(1)], live=par), now=1.0)
        st.ingest(doc([req(1)], live=dict(par, tok_s="n/a", tok_s_mean=float("nan"))), now=3.0)
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok, added = False, repr(exc)
    check(ok and added == 1, f"parallel live block ingests without raising ({added})")
    check(st.live_state == "generating" and st.live_decode is None and st.live_prompt is None,
          "parallel: live decode stays blank when tok_s and tok_s_mean are None / junk")


def test_catchup_and_model_change() -> None:
    allr = [req(i) for i in range(100)]
    st = MetricsStore()
    st.ingest(doc(allr[:40]))                                  # the newest 12 of the first 40: r28..r39
    long = doc(allr)
    long["requests"] = allr[::-1]                              # ?requests=all: every kept request, newest first
    added = st.ingest(long)
    series = st.decode_series()
    check(len(st) == 30 and series == [100.0 + i for i in range(70, 100)],
          f"catch-up list of 100: the newest 30 kept, oldest -> newest ({series[0]}..{series[-1]})")
    check(added == 30, f"added counts only the new requests that stayed in the window ({added})")
    check(st.ingest(long) == 0 and len(st) == 30 and st.decode_series() == series,
          "the same long list again: no duplicates, evicted old ones not re-added")

    st = MetricsStore()
    st.ingest(doc([req(i) for i in range(20, 30)]))           # polled the newest; r0..r19 fell out meanwhile
    shuffled = doc(allr[:30])
    shuffled["requests"] = [allr[i] for i in (5, 29, 0, 17, 22, 3, 11, 28, 8, 14, 1, 26, 19, 9, 2, 25, 13, 7, 21,
                                              16, 4, 27, 10, 18, 12, 24, 6, 23, 15, 20)]
    added = st.ingest(shuffled)
    check(added == 20 and st.decode_series() == [100.0 + i for i in range(30)],
          f"older requests from a catch-up slot in by time, any input order ({added})")
    st = MetricsStore(max_requests=5)
    ties = [{"id": "b", "time": 50.0, "output_tokens": 99, "decode_tok_s": 2.0},
            {"id": "a", "time": 50.0, "output_tokens": 99, "decode_tok_s": 1.0},
            {"id": "c", "time": 40.0, "output_tokens": 99, "decode_tok_s": 0.5}]
    st.ingest({"engine": {"model": "m"}, "requests": ties})
    check(st.decode_series() == [0.5, 1.0, 2.0], "equal start times ordered by id")

    st = MetricsStore()
    st.ingest(doc(allr[:12]))
    check(len(st) == 12 and st.model == "m", "model m: 12 bars")
    other = doc([req(i, decode_tok_s=50.0) for i in range(3)])
    other["engine"] = {"model": "other-model"}
    added = st.ingest(other)
    check(st.model == "other-model" and len(st) == 3 and added == 3 and st.decode_series() == [50.0] * 3
          and st.polls == 1, f"engine.model changed: history reset before ingesting ({len(st)} bars)")
    no_model = doc([req(5, decode_tok_s=60.0)])
    no_model["engine"] = {"model": None}
    st.ingest(no_model)
    check(st.model == "other-model" and len(st) == 4, "engine.model missing (unloaded): no reset")
    st.reset()
    st.ingest(doc(allr[:2]))
    check(len(st) == 2 and st.model == "m", "after reset() the next model is just recorded (no spurious reset)")


# ------------------------------------------------------------------------------------------ HTTP against the fake
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    assert p != 8080
    return p


def test_fetch() -> None:
    port = free_port()
    root = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, str(HERE / "fixtures" / "fake_strata_server.py"), "--port", str(port),
                             "--metrics-requests", "20", "--metrics-live", "generating"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        data = None
        end = time.monotonic() + 10
        while time.monotonic() < end:
            data = ms.fetch_metrics(root, timeout=1.0)
            if data and data.get("live", {}).get("state") == "generating":
                break
            time.sleep(0.1)
        check(isinstance(data, dict) and set(data) >= {"engine", "live", "requests", "totals", "hardware", "time"},
              "fetch_metrics returns Strata's JSON document")
        check(len(data["requests"]) == 12 and data["requests_kept"] == 20 and data["requests"][0]["id"] == "req000000019",
              "only the newest 12 requests come back, newest first")
        with opener.open(urllib.request.Request(root + "/metrics", headers={"Accept": "text/plain"}), timeout=2) as r:
            text = r.read().decode()
        check(text.startswith("# TYPE"), "the fake answers Prometheus text for Accept: text/plain (as Strata does) "
              "- so the JSON above proves our header selects JSON")
        st = MetricsStore()
        st.ingest(data)
        check(len(st) == 12 and st.live_decode == 101.3, f"ingest of the fetched document ({len(st)}, {st.live_decode})")
        for _ in range(4):                                     # 4 polls x 8 new requests
            with opener.open(root + "/_fake/add?n=8", timeout=2) as r:
                r.read()
            st.ingest(ms.fetch_metrics(root, timeout=1.0))
        check(len(st) == 30, f"accumulated across polls to 30 of {20 + 32} ({len(st)})")
        dec, pr = st.decode_series(), st.prompt_series()
        check(len(dec) < 30 and len(pr) < len(dec) and all(90 <= v <= 115 for v in dec),
              f"answers without a decode figure are skipped ({len(dec)} bars, {min(dec)}-{max(dec)} tok/s)")
        check(all(3300 <= v <= 3700 for v in pr), f"cache-hit requests do not spike the prompt scale "
              f"({len(pr)} bars, {min(pr):.0f}-{max(pr):.0f} tok/s)")
        with opener.open(root + "/_fake/live?state=reading", timeout=2) as r:
            r.read()
        st.ingest(ms.fetch_metrics(root, timeout=1.0))
        check(st.live_state == "reading" and st.live_prompt is None, "live reading: first poll blank (mean unproven)")
        st.ingest(ms.fetch_metrics(root, timeout=1.0))
        check(st.live_state == "reading" and st.live_prompt == 3650.0 and st.live_decode is None,
              "live reading value once prompt_read advanced")
        check(st.hw_vram is not None and abs(st.hw_vram[1] - 16384.0) < 1, "hardware GPU memory parsed")
        full = ms.fetch_metrics(root, timeout=1.0, all_requests=True)
        check(isinstance(full, dict) and len(full["requests"]) == full["requests_kept"] == 52
              and full["requests"][0]["id"] == "req000000051", "all_requests=True: ?requests=all returns every kept "
              f"request as JSON ({len(full['requests']) if full else None})")
        with opener.open(root + "/_fake/add?n=20", timeout=2) as r:     # 20 finish while 'collapsed'
            r.read()
        before = st.decode_series()
        added = st.ingest(ms.fetch_metrics(root, timeout=1.0, all_requests=True))
        check(added == 20 and len(st) == 30, f"catch-up fetch recovers all 20 missed requests, not just 12 ({added})")
        ids = [k for k in st._reqs]
        check(ids[-1] == "req000000071" and ids[0] == "req000000042" and len(set(ids)) == 30 and before,
              f"catch-up keeps the newest 30 by time, no duplicates ({ids[0]}..{ids[-1]})")
    finally:
        proc.kill()                                            # our own child only
        proc.wait(5)
    t0 = time.monotonic()
    check(ms.fetch_metrics(root, timeout=1.0) is None, "server gone -> None, no exception")
    check(time.monotonic() - t0 < 3.5, f"dead endpoint fails fast ({time.monotonic() - t0:.2f} s)")
    check(ms.fetch_metrics("not a url") is None, "garbage URL -> None")


# ------------------------------------------------------------------------------------------ VRAM sampler
class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def fake_nvml(used_gb: list[float], total_gb: float = 16.0, fail_after: int | None = None):
    calls = {"init": 0, "shutdown": 0, "read": 0}

    def init():
        calls["init"] += 1

    def shutdown():
        calls["shutdown"] += 1

    def mem(_h):
        if _h == "small":
            return types.SimpleNamespace(used=1, total=2 * 1024 ** 3)
        calls["read"] += 1
        if fail_after is not None and calls["read"] > fail_after + 1:      # +1: the read during init
            raise RuntimeError("NVML_ERROR_GPU_IS_LOST")
        v = used_gb[min(calls["read"] - 2, len(used_gb) - 1)] if calls["read"] > 1 else 0.0
        return types.SimpleNamespace(used=int(v * 1024 ** 3), total=int(total_gb * 1024 ** 3))

    mod = types.SimpleNamespace(nvmlInit=init, nvmlShutdown=shutdown, nvmlDeviceGetCount=lambda: 2,
                                nvmlDeviceGetHandleByIndex=lambda i: "small" if i == 0 else "big",
                                nvmlDeviceGetMemoryInfo=mem)
    return mod, calls


def test_vram_sampler() -> None:
    # NVML missing entirely (module whose init raises, like pynvml without nvml.dll)
    def boom():
        raise OSError("nvml.dll not found")
    clock = Clock()
    s = vs.VramSampler(nvml=types.SimpleNamespace(nvmlInit=boom, nvmlShutdown=lambda: None), clock=clock)
    check(s.snapshot().available is None, "before the first sample: available is None (not 'unavailable')")
    check(s.sample_once() is False and s.snapshot().available is False and s.snapshot().samples == (),
          "NVML missing -> available False, no samples, no exception")
    check(s.sample_once() is False, "repeated sampling while unavailable stays quiet")

    # import failure path (no injected module): simulate pynvml not installed
    saved = sys.modules.get("pynvml")
    sys.modules["pynvml"] = None                               # makes `import pynvml` raise ImportError
    try:
        s2 = vs.VramSampler(clock=Clock())
        check(s2.sample_once() is False and s2.snapshot().available is False, "pynvml not importable -> unavailable")
        s2.start()
        time.sleep(0.15)
        s2.stop()
        check(s2.snapshot().available is False and not s2._thread.is_alive(), "thread runs and stops cleanly without NVML")
    finally:
        if saved is None:
            sys.modules.pop("pynvml", None)
        else:
            sys.modules["pynvml"] = saved

    # fallback feed from Strata's hardware block while NVML is unavailable
    check(s.push_external(11000.0, 16384.0) is True, "push_external accepted while NVML is unavailable")
    snap = s.snapshot()
    check(snap.available is True and snap.source == "strata" and snap.samples[-1][1] == 11000.0
          and snap.total_mb == 16384.0, "fallback sample visible")
    clock.t += 2
    s.sample_once()
    check(s.snapshot().available is True, "a failed NVML retry does not hide fresh fallback data")
    clock.t += 60
    check(s.snapshot().available is False, "fallback data going stale -> unavailable again")

    # working NVML: picks the larger card, converts to MB, keeps a rolling window
    mod, calls = fake_nvml([4.0, 5.0, 6.0])
    clock = Clock()
    s = vs.VramSampler(nvml=mod, clock=clock, window_s=10.0, interval_s=2.0)
    for _ in range(3):
        check(s.sample_once() is True, "sample stored") if _ == 0 else s.sample_once()
        clock.t += 2
    snap = s.snapshot()
    check([round(v) for _t, v in snap.samples] == [4096, 5120, 6144] and snap.total_mb == 16384.0
          and snap.available is True and snap.source == "nvml", f"used/total in MB from the 16 GB card ({snap.samples})")
    check(calls["init"] == 1, "NVML initialised once")
    check(s.push_external(1.0, 2.0) is False, "push_external ignored while NVML delivers")
    clock.t += 7
    s.sample_once()
    snap = s.snapshot()
    check(len(snap.samples) == 2 and snap.samples[0][0] >= clock.t - 10.0, f"samples older than the window dropped "
          f"({len(snap.samples)})")

    # NVML dies mid-run: unavailable, shutdown called, re-init only after the retry delay
    mod, calls = fake_nvml([4.0], fail_after=1)
    clock = Clock()
    s = vs.VramSampler(nvml=mod, clock=clock, retry_s=30.0)
    check(s.sample_once() is True, "first read ok")
    clock.t += 2
    check(s.sample_once() is False and s.snapshot().available is False and calls["shutdown"] == 1,
          "read failure -> unavailable + nvmlShutdown")
    clock.t += 2
    s.sample_once()
    check(calls["init"] == 1, "no re-init before the retry delay")
    clock.t += 31
    s.sample_once()
    check(calls["init"] == 2, "re-init after the retry delay")

    # thread lifecycle with a working NVML
    mod, calls = fake_nvml([3.0])
    s = vs.VramSampler(nvml=mod, interval_s=0.05)
    s.start()
    s.start()                                                  # idempotent
    time.sleep(0.3)
    s.stop()
    check(len(s.snapshot().samples) >= 3 and not s._thread.is_alive() and calls["shutdown"] == 1,
          f"thread samples on its interval and shuts NVML down on stop ({len(s.snapshot().samples)} samples)")


def test_real_nvml_smoke() -> None:
    """Read-only smoke test of the real NVML path on this machine (skipped if there is no NVIDIA driver)."""
    s = vs.VramSampler()
    ok = s.sample_once()
    snap = s.snapshot()
    s.stop()
    if not ok:
        print(f"[SKIP] real NVML not available here (available={snap.available})")
        return
    check(snap.total_mb and snap.total_mb > 1024 and 0 <= snap.samples[-1][1] <= snap.total_mb,
          f"real NVML: {snap.samples[-1][1] / 1024:.1f} / {snap.total_mb / 1024:.1f} GB")


def test_graph_helpers() -> None:
    import graphs
    cases = {102.0: 120.0, 120.0: 120.0, 121.0: 150.0, 3712.0: 4000.0, 0.7: 0.8, 9.9: 10.0, 1.0: 1.0, 45.0: 50.0}
    got = {v: graphs.nice_ceil(v) for v in cases}
    check(all(abs(got[v] - want) < 1e-9 for v, want in cases.items()), f"nice_ceil ({got})")
    check(graphs.nice_ceil(0) == 1.0 and graphs.nice_ceil(-5) == 1.0 and graphs.nice_ceil(float("nan")) == 1.0,
          "nice_ceil of nothing -> 1")
    check([graphs.fmt_scale(v) for v in (120, 4000, 1500, 0.8, 50)] == ["120", "4k", "1.5k", "0.8", "50"], "fmt_scale")
    check([graphs.fmt_rate(v) for v in (101.34, 3712.4, 9.96)] == ["101.3", "3,712", "10.0"], "fmt_rate")
    rp = graphs.robust_peak
    check(rp([95, 97, 92, 300, 96]) == 97 and rp([95, 97, 92, 96, 1250]) == 97, "robust_peak drops a single spike")
    check(rp([1000, 1000, 1000, 4000, 4000]) == 4000 and rp([100, 300]) == 300 and rp([]) == 0.0
          and rp([0, -3, float("nan")]) == 0.0, "robust_peak: a real spread / < 4 values / nothing -> plain max")
    many = [100.0] * 28 + [900.0, 950.0]
    check(rp(many) == 100.0 and rp([100.0] * 17 + [900.0, 950.0, 990.0]) == 990.0,
          "robust_peak: up to 1 outlier per 10 values excluded, more are a real spread")


def test_bar_graph_render() -> None:
    """Probe the drawn bars: one spike must not squash the others; off-scale bars are capped at full height."""
    import tkinter as tk
    import graphs
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"[SKIP] no Tk display ({exc})")
        return
    try:
        root.withdraw()
        g = graphs.BarGraph(root)

        def heights(values, live=None):
            g.set_data(values, live=live)
            g.delete("all")
            g._paint(330, 40, 1.0)
            bars = [g.coords(i) for i in g.find_withtag("bar")]
            caps = [g.coords(i) for i in g.find_withtag("cap")]
            return [round(c[3] - c[1], 1) for c in bars], caps

        normal, caps0 = heights([95, 97, 92, 96])
        h, caps = heights([95, 97, 92, 300, 96])
        check(g.scale_max == 100.0 and g._max_label == "100" and g.clipped == 1,
              f"spike: scale = nice(robust peak 97) = 100, label '100', 1 bar clipped ({g.scale_max}, {g._max_label})")
        check(min(h[0], h[1], h[2], h[4]) > 25 and h[:3] == normal[:3],
              f"normal bars keep their height next to a spike ({h}; without spike {normal})")
        full = max(h)
        check(h[3] == full and len(caps) == 1 and not caps0 and abs(caps[0][1] - (40 - 1 - full)) < 0.01
              and caps[0][3] - caps[0][1] == 2, f"the spike is drawn full height with a 2 px cap ({caps})")
        h, caps = heights([95, 97, 92, 96, 98], live=1250.0)
        check(g.scale_max == 100.0 and len(caps) == 1 and h[-1] == max(h), "a live spike is capped too")
        h, caps = heights([1000, 1000, 1000, 4000, 4000])
        check(g.scale_max == 4000.0 and not caps and g.clipped == 0, "a genuine spread is not clipped")
        g.set_data([])
        check(g._max_label == "" and g.clipped == 0, "empty series: no label, nothing clipped")
    finally:
        root.destroy()
    import theme
    check(theme.mix("#FFFFFF", "#000000", 0.5) == "#808080" and theme.mix(theme.ACCENT, theme.SURFACE, 0) == theme.SURFACE
          and theme.mix(theme.ACCENT, theme.SURFACE, 1) == theme.ACCENT, "theme.mix blends two tokens")


def test_version_consistency() -> None:
    import re
    import version
    root = HERE.parent
    v = version.__version__
    check(re.fullmatch(r"\d+\.\d+\.\d+", v) is not None and v == "1.1.0", f"version.py = {v}")
    quad = "(" + ", ".join(v.split(".") + ["0"]) + ")"
    info = (root / "installer" / "version_info.txt").read_text(encoding="utf-8")
    check(info.count(quad) == 2 and info.count(f'"{v}"') == 2, "installer/version_info.txt matches version.py")
    inc = (root / "installer" / "version.iss").read_text(encoding="utf-8")
    check(f'#define AppVersion "{v}"' in inc, "installer/version.iss matches version.py")
    iss = (root / "installer" / "HermesController.iss").read_text(encoding="utf-8")
    check('#include "version.iss"' in iss and "#define AppVersion" not in iss,
          "HermesController.iss takes AppVersion from the generated include")
    stale = [p.name for p in (root / "README.md", root / "build.bat", root / "HermesController.spec")
             if "1.0.0" in p.read_text(encoding="utf-8")]
    check(not stale, f"no hardcoded 1.0.0 left in README / build.bat / spec ({stale})")


def main() -> int:
    for fn in (test_rates, test_accumulation, test_live, test_stale_prefill_mean, test_parallel_live,
               test_catchup_and_model_change, test_fetch, test_vram_sampler, test_real_nvml_smoke,
               test_graph_helpers, test_bar_graph_render, test_version_consistency):
        print(f"-- {fn.__name__}")
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
            print(FAILS[-1])
    print(f"\nRESULT: {PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
