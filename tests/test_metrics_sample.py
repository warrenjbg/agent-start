r"""Unit tests for the engine-neutral metrics path: strata_sample (Strata /metrics -> MetricsSample), MetricsStore fed
with MetricsSample objects, activity()'s tool-call keys, and StrataPoller (the /metrics + /status follow-up logic).

Run: .venv\Scripts\python.exe tests\test_metrics_sample.py
No network, no ports: every fetcher is a fake, and the module's HTTP opener is replaced by one that fails loudly.
"""
from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import metrics_store as ms  # noqa: E402
from engines.base import LiveSample, MetricsPoller, MetricsSample, RequestEnd, RequestSample  # noqa: E402
from metrics_store import MetricsStore, StrataPoller, strata_sample  # noqa: E402

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


class NoNetwork:
    """Stands in for metrics_store._OPENER: any HTTP attempt is recorded and fails."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, req, timeout=None):
        self.urls.append(getattr(req, "full_url", str(req)))
        raise OSError("network disabled in this test")


# ------------------------------------------------------------------------------------------ strata_sample
def test_strata_sample_mapping() -> None:
    d = doc([req(1), req(2, decode_tok_s=None), {"time": 77.0, "output_tokens": 50, "decode_tok_s": 50.0},
             {"x": 1}, "junk"],
            live={"state": "generating", "queued": 2, "phase": "thinking", "prompt_read": 10, "prompt_total": 20,
                  "prompt_tokens": 20, "generated": 412, "max_tokens": 32768, "tok_s": 98.4, "tok_s_mean": 97.0,
                  "prefill_tok_s_mean": 3600.0},
            hardware={"gpu_mem_used": 8 * 1024 ** 3, "gpu_mem_total": 16 * 1024 ** 3}, time=1234.5)
    s = strata_sample(d)
    check(isinstance(s, MetricsSample) and s.model == "m" and s.clock == 1234.5 and s.vram_mb == (8192.0, 16384.0)
          and s.last_end is None, f"top-level fields: model, clock, vram MB, no last_end ({s and s.vram_mb})")
    keys = [r.key for r in s.requests]
    check(keys == ["t77.0/50", "r0002", "r0001"], f"requests keyed by id, else time/output_tokens; junk dropped ({keys})")
    r1 = next(r for r in s.requests if r.key == "r0001")
    r2 = next(r for r in s.requests if r.key == "r0002")
    check(r1.time == 1001.0 and r1.decode_tps == ms.decode_rate(req(1)) == 101.0
          and r1.prompt_tps == ms.prompt_rate(req(1)) == 4000.0 and r2.decode_tps is None,
          "request speeds = decode_rate / prompt_rate (noise thresholds unchanged)")
    lv = s.live
    check(isinstance(lv, LiveSample) and lv.state == "generating" and lv.queued == 2 and lv.phase == "thinking"
          and lv.prompt_read == 10 and lv.prompt_total == 20 and lv.prompt_tokens == 20 and lv.generated == 412
          and lv.max_tokens == 32768 and lv.decode_tps == 98.4 and lv.decode_tps_mean == 97.0
          and lv.prompt_tps_mean == 3600.0 and lv.in_tool_call is False and lv.tool is None,
          f"live block mapped field by field ({lv})")
    lv0 = strata_sample(doc([], live={"state": "generating", "tok_s": 0.0, "tok_s_mean": 88.0})).live
    check(lv0.decode_tps is None and lv0.decode_tps_mean == 88.0, "tok_s 0 -> decode_tps None, the mean still there")
    for phase, want in (("writing a tool call: web_search", (True, "web_search")),
                        ("writing a tool call", (True, None)), ("writing a tool call:  ", (True, None)),
                        ("tool call complete", (True, None)), ("answering", (False, None)), (None, (False, None)),
                        (5, (False, None))):
        lv = strata_sample(doc([], live={"state": "generating", "phase": phase})).live
        check((lv.in_tool_call, lv.tool) == want and lv.phase == (phase if isinstance(phase, str) else None),
              f"live phase {phase!r} -> in_tool_call/tool {want}")
    for end, want in (({"phase": "tool call complete", "tool": "read_file"}, RequestEnd(True, "read_file")),
                      ({"phase": "tool call complete", "tool": None}, RequestEnd(True, None)),
                      ({"phase": "writing a tool call: web_search", "tool": None}, RequestEnd(True, "web_search")),
                      ({"phase": "writing a tool call: a", "tool": "b"}, RequestEnd(True, "b")),
                      ({"phase": "writing a tool call: a", "tool": ""}, RequestEnd(True, "a")),
                      ({"phase": "answering", "tool": "x"}, RequestEnd(False, None)),
                      ({"phase": None, "tool": None}, RequestEnd(False, None)),
                      ({"phase": 7}, RequestEnd(False, None)), ("nonsense", None), (None, None)):
        got = strata_sample(doc([], _last_end=end)).last_end
        check(got == want, f"_last_end {end!r} -> {want} ({got})")
    check(strata_sample(doc([])).last_end is None, "no _last_end key -> last_end None")


def test_strata_sample_garbage() -> None:
    for junk in (None, "x", 5, [1, 2], True):
        check(strata_sample(junk) is None, f"non-dict document {junk!r} -> None")
    weird = {"engine": "x", "requests": "x", "live": "x", "hardware": [1], "time": float("nan"), "totals": 5}
    try:
        s = strata_sample(weird)
        ok = s == MetricsSample()
    except Exception as exc:  # noqa: BLE001
        ok, s = False, exc
    check(ok, f"wrong types everywhere -> an empty sample, no exception ({s})")
    bad_live = {"state": 5, "queued": "x", "phase": [1], "prompt_read": True, "prompt_total": float("inf"),
                "generated": "bad", "tok_s": float("nan"), "tok_s_mean": "x", "prefill_tok_s_mean": False}
    lv = strata_sample(doc([], live=bad_live, engine={"model": 3},
                           hardware={"gpu_mem_used": True, "gpu_mem_total": 0})).live
    check(lv == LiveSample(), f"junk live values (bools, NaN, inf, strings) -> all None / 0 ({lv})")
    s = strata_sample(doc([], engine={"model": 3}, hardware={"gpu_mem_used": 1, "gpu_mem_total": 0}))
    check(s.model is None and s.vram_mb is None, "non-text model / zero total VRAM -> None")
    s = strata_sample({"requests": [{"id": "", "time": None}, {"id": None, "time": "x"}, {"id": 0}]})
    check([r.key for r in s.requests] == ["0"] and s.live is None and s.clock is None,
          f"untrackable requests dropped, id 0 kept ({[r.key for r in s.requests]})")


# ------------------------------------------------------------------------------------------ ingest equivalence
def _snap(st: MetricsStore) -> tuple:
    return (dict(st.live), st.live_state, st.live_decode, st.live_prompt, st.hw_vram, st.polls, st.model, len(st),
            st.decode_series(), st.prompt_series(), list(st._reqs.items()), st.activity(), st._read_prev,
            st._read_seen, st._read_fresh)


def _sequence() -> list[tuple[dict | None, float | None]]:
    """(document, now) pairs covering the cases test_stats.py exercises, in one long run."""
    allr = [req(i) for i in range(100)]
    gen = {"state": "generating", "tok_s": 98.0, "tok_s_mean": 97.0, "phase": "thinking", "generated": 40}
    stale = {"state": "reading", "prefill_tok_s_mean": 5100.0, "prompt_read": 3000, "prompt_total": 3000}
    par = {"state": "generating", "queued": 0, "phase": None, "tok_s": None, "tok_s_mean": None,
           "prefill_tok_s_mean": None, "prompt_read": None, "prompt_total": None, "parallel": 4}
    no_live = doc(allr[:5])
    no_live["live"] = None
    long = doc(allr)
    long["requests"] = allr[::-1]
    other = doc([req(i, decode_tok_s=50.0) for i in range(3)])
    other["engine"] = {"model": "other-model"}
    no_model = doc([req(5, decode_tok_s=60.0)])
    no_model["engine"] = {"model": None}
    seq: list[tuple[dict | None, float | None]] = [
        (doc(allr[:5]), None), (doc(allr[:5]), None), (doc(allr[:20]), None),
        (doc(allr[:26], live=gen), 1.0), (doc(allr[:26], live=stale), 3.0), (doc(allr[:26], live=stale), 5.0),
        (doc(allr[:26], live={"state": "reading", "prefill_tok_s_mean": 3300.0, "prompt_read": 2000,
                              "prompt_total": 9000}), 7.0),
        (doc(allr[:26], live=gen), 9.0),
        (doc(allr[:26], live={"state": "reading", "prefill_tok_s_mean": None, "prompt_read": 1000,
                              "prompt_total": 9000}), 10.0),
        (doc(allr[:26], live={"state": "reading", "prefill_tok_s_mean": 0.0, "prompt_read": 7000,
                              "prompt_total": 9000}), 12.0),
        (doc(allr[:26], live={"state": "reading", "prompt_read": 500, "prompt_total": 4000}), 14.0),
        (no_live, 16.0),
        (doc(allr[:30], live={"state": "reading", "prompt_read": 100, "prompt_total": 4000}, time=20.0), None),
        (doc(allr[:30], live={"state": "reading", "prompt_read": 900, "prompt_total": 4000}, time=22.0), None),
        (doc(allr[:30], live={"state": "reading", "prompt_read": 900, "prompt_total": 4000}, time=22.1), None),
        (doc(allr[:30], live=dict(par)), 30.0), (doc(allr[:30], live=dict(par, tok_s="n/a", tok_s_mean=math.nan)), 31.0),
        (doc(allr[:30], live={"state": "generating", "tok_s": 0.0, "tok_s_mean": 88.0,
                              "phase": "writing a tool call: web_search"}), 32.0),
        (doc(allr[:30], live={"state": "idle", "queued": 2}), 33.0),
        (doc(allr[:30], live={"state": "generating", "queued": "x", "generated": "bad", "phase": 5}), 34.0),
        (doc(allr[:40]), None), (long, None), (long, None),
        (doc(allr[:40], hardware={"gpu_mem_used": 8 * 1024 ** 3, "gpu_mem_total": 16 * 1024 ** 3}), None),
        (other, None), (no_model, None), ({"requests": "x"}, None), (doc([], None) | {"live": "nonsense"}, 40.0),
        (doc(allr[:12]), None),
    ]
    return seq


def test_ingest_equivalence() -> None:
    a, b = MetricsStore(), MetricsStore()
    mismatches, added_diff = [], []
    seq = _sequence()
    for i, (d, now) in enumerate(seq):
        na = a.ingest(copy.deepcopy(d), now=now)
        nb = b.ingest(strata_sample(copy.deepcopy(d)), now=now)
        if na != nb:
            added_diff.append((i, na, nb))
        if _snap(a) != _snap(b):
            mismatches.append(i)
        if i == 14:                                          # clear_live mid-run, as the app does on a failed poll
            a.clear_live()
            b.clear_live()
    check(not mismatches and not added_diff, f"ingest(dict) and ingest(strata_sample(dict)) leave identical state at "
          f"every one of {len(seq)} steps (mismatch at {mismatches}, added differs {added_diff})")
    a.reset()
    b.reset()
    check(_snap(a) == _snap(b) and a.polls == 0 and len(b) == 0, "reset() on both: identical empty state")
    check(a.ingest(None) == 0 and b.ingest(None) == 0 and a.ingest("nope") == 0 and a.polls == 0,
          "ingest(None) / junk: nothing counted")


# ------------------------------------------------------------------------------------------ a non-Strata engine
def test_neutral_sample() -> None:
    st = MetricsStore()
    s1 = MetricsSample(requests=(RequestSample("a", decode_tps=40.0, prompt_tps=900.0),
                                 RequestSample("b", decode_tps=41.0), RequestSample("c", prompt_tps=950.0)),
                       live=LiveSample(state="generating", generated=12))
    added = st.ingest(s1)
    check(added == 3 and st.decode_series() == [40.0, 41.0] and st.prompt_series() == [900.0, 950.0],
          f"hand-built sample (no time, no model, no clock, no vram): plotted ({added})")
    a = st.activity()
    check(st.live_decode is None and st.hw_vram is None and st.model is None and a is not None
          and a["kind"] == "generating" and a["tokens"] == 12 and a["rate"] is None and a["tool_call"] is False,
          f"live with only state + generated: activity shown, no rate ({a})")
    s2 = MetricsSample(requests=(RequestSample("c", prompt_tps=1.0), RequestSample("d", decode_tps=42.0)),
                       live=LiveSample(state="generating", generated=30, decode_tps=43.5))
    check(st.ingest(s2) == 1 and st.decode_series() == [40.0, 41.0, 42.0] and st.prompt_series() == [900.0, 950.0]
          and st.live_decode == 43.5, "keys dedupe across polls; instant rate shown")
    st.ingest(MetricsSample(live=LiveSample(state="reading", prompt_read=100, prompt_total=1000)), now=1.0)
    st.ingest(MetricsSample(live=LiveSample(state="reading", prompt_read=600, prompt_total=1000)), now=2.0)
    check(st.live_prompt == 500.0 and st.activity()["frac"] == 0.6,
          f"reading rate from progress with the caller's clock ({st.live_prompt})")
    st.ingest(MetricsSample(live=LiveSample(state="reading", prompt_read=900, prompt_total=1000,
                                            prompt_tps_mean=777.0)), now=3.0)
    check(st.live_prompt == 777.0, "engine-reported prompt mean once progress moved")
    junk = MetricsSample(model=5, requests=(RequestSample(""), RequestSample(None), RequestSample("z", time=math.nan,
                                                                                                 decode_tps=math.inf)),
                         live=LiveSample(state=3, queued=None, generated="x"), clock="t", vram_mb=(1.0,))
    try:
        n = st.ingest(junk)
        ok = n == 1 and st.hw_vram is None and st.live_state is None and st.model is None
    except Exception as exc:  # noqa: BLE001
        ok, n = False, exc
    check(ok, f"badly typed neutral sample tolerated ({n})")
    st.ingest(MetricsSample(vram_mb=(1024.0, 8192.0)))
    check(st.hw_vram == (1024.0, 8192.0), "vram_mb passed through as hw_vram")
    m = MetricsStore()
    m.ingest(MetricsSample(model="x", requests=(RequestSample("1", decode_tps=1.0),)))
    m.ingest(MetricsSample(model="y", requests=(RequestSample("2", decode_tps=2.0),)))
    check(m.decode_series() == [2.0] and m.polls == 1, "model change in a neutral sample resets the history")


def test_activity_tool_keys() -> None:
    m = MetricsStore()
    m.ingest(doc([], {"state": "generating", "phase": "writing a tool call: web_search", "generated": 20}), now=1.0)
    a = m.activity()
    check(a["tool_call"] is True and a["tool"] == "web_search" and a["phase"] == "writing a tool call: web_search",
          f"writing a tool call: tool_call + tool, phase text unchanged ({a})")
    m.ingest(doc([], {"state": "generating", "phase": "tool call complete"}), now=2.0)
    a = m.activity()
    check(a["tool_call"] is True and a["tool"] is None, f"tool call complete: tool_call, no name ({a})")
    m.ingest(doc([], {"state": "generating", "phase": "answering"}), now=3.0)
    a = m.activity()
    check(a["tool_call"] is False and a["tool"] is None, f"answering: no tool call ({a})")
    m.ingest(doc([], {"state": "reading", "prompt_read": 1, "prompt_total": 2, "phase": "writing a tool call: x"}),
             now=4.0)
    a = m.activity()
    check(a["kind"] == "reading" and a["tool_call"] is False and a["tool"] is None and a["phase"] is None,
          "reading: never a tool call")
    m.ingest(doc([], {"state": "idle", "queued": 1}), now=5.0)
    a = m.activity()
    check(a["kind"] == "queued" and a["tool_call"] is False and a["tool"] is None, "queued: no tool call")
    want = {"kind", "queued", "frac", "read", "total", "tokens", "rate", "phase", "tool_call", "tool"}
    check(set(a) == want, f"activity keys = the old 8 + tool_call, tool ({sorted(a)})")
    check({"state", "queued", "phase", "prompt_read", "prompt_total", "prompt_tokens", "generated", "max_tokens",
           "in_tool_call", "tool"} == set(m.live), f"store.live keeps its keys, adds in_tool_call/tool ({sorted(m.live)})")
    m.live = {}                                              # main.py clears it like this on OFF
    check(m.activity() is None, "live = {} -> no activity")


# ------------------------------------------------------------------------------------------ StrataPoller
class FakeFetch:
    """GET /metrics fake: returns `self.doc` (a copy), records how it was called."""

    def __init__(self) -> None:
        self.doc: dict | None = None
        self.calls: list[tuple] = []
        self.boom = False

    def __call__(self, root, **kw):
        self.calls.append((root, kw))
        if self.boom:
            raise RuntimeError("fetch exploded")
        return copy.deepcopy(self.doc)


class FakeStatus:
    def __init__(self, doc: dict | None = None) -> None:
        self.doc, self.calls, self.boom = doc, 0, False

    def __call__(self, root):
        self.calls += 1
        if self.boom:
            raise RuntimeError("status exploded")
        return dict(self.doc) if self.doc is not None else None


def pdoc(n, state="idle", parallel=None, **kw) -> dict:
    live = {"state": state}
    if parallel is not None:
        live["parallel"] = parallel
    return doc([req(1)], live=live, totals={"requests": n}, **kw)


def test_poller() -> None:
    saved = ms._OPENER
    net = NoNetwork()
    ms._OPENER = net
    try:
        _poller_checks(net)
    finally:
        ms._OPENER = saved


def _poller_checks(net: NoNetwork) -> None:
    check(issubclass(StrataPoller, MetricsPoller), "StrataPoller is a MetricsPoller")
    fm, fs = FakeFetch(), FakeStatus({"busy": False, "phase": "tool call complete", "tool": "web_search"})
    p = StrataPoller(fetch_metrics=fm, fetch_status=fs)
    fm.doc = pdoc(3)
    s = p.poll("http://h:1", catch_up=True)
    check(fm.calls[-1] == ("http://h:1", {"all_requests": True}) and isinstance(s, MetricsSample)
          and len(s.requests) == 1, f"catch_up=True -> all_requests=True ({fm.calls[-1]})")
    s = p.poll("http://h:1")
    check(fm.calls[-1] == ("http://h:1", {}), f"plain poll calls fetch_metrics(root) as main.py did ({fm.calls[-1]})")
    check(s.last_end is None and fs.calls == 0, "first poll / counter unchanged: no /status, no last_end")
    fm.doc = pdoc(4)
    s = p.poll("http://h:1")
    check(fs.calls == 1 and s.last_end == RequestEnd(True, "web_search"),
          f"counter grew + idle + not parallel: /status asked, last_end set ({s.last_end})")
    s = p.poll("http://h:1")
    check(fs.calls == 1 and s.last_end is None, "next poll (counter unchanged): last_end gone")
    fm.doc = pdoc(5, state="generating")
    s = p.poll("http://h:1")
    check(fs.calls == 1 and s.last_end is None, "counter grew but generating: no /status")
    fm.doc = pdoc(6, parallel=4)
    s = p.poll("http://h:1")
    check(fs.calls == 1 and s.last_end is None, "counter grew but parallel mode: no /status")
    fm.doc = pdoc(7, parallel=0)
    fs.doc = {"busy": False, "phase": "answering"}
    s = p.poll("http://h:1")
    check(fs.calls == 2 and s.last_end == RequestEnd(False), f"parallel 0 counts as not parallel; answer ({s.last_end})")
    fm.doc = pdoc(8)
    fs.doc = {"busy": True, "phase": "writing a tool call: x"}
    s = p.poll("http://h:1")
    check(fs.calls == 3 and s.last_end is None, "status says busy: ignored")
    fm.doc = pdoc(9)
    fs.doc = None
    s = p.poll("http://h:1")
    check(fs.calls == 4 and s.last_end is None, "status unavailable (API key): no last_end")
    fm.doc = pdoc(9)
    p.poll("http://h:1")
    fm.doc = pdoc(8)
    fs.doc = {"busy": False, "phase": "answering"}
    s = p.poll("http://h:1")
    check(fs.calls == 4 and s.last_end is None, "counter went DOWN (server restarted): no /status")
    fm.doc = pdoc(10)
    s = p.poll("http://h:1")
    check(fs.calls == 5 and s.last_end == RequestEnd(False), "then growing from the new value works")
    fm.doc = pdoc("11")
    p.poll("http://h:1")
    fm.doc = pdoc(12)
    s = p.poll("http://h:1")
    check(fs.calls == 6, "non-int counter keeps the previous one (10 -> 12 grew)")
    p.reset()
    fm.doc = pdoc(20)
    s = p.poll("http://h:1")
    check(fs.calls == 6 and s.last_end is None, "reset(): the first poll after it only records the counter")
    fm.doc = pdoc(21)
    s = p.poll("http://h:1")
    check(fs.calls == 7 and s.last_end is not None, "reset(): growth after that is seen again")
    fm.doc = pdoc(22, _last_end={"phase": "tool call complete", "tool": "t"})
    fs.doc = None
    s = p.poll("http://h:1")
    check(s.last_end == RequestEnd(True, "t"), "a _last_end already in the document is kept when /status says nothing")
    fm.doc = pdoc(23)
    fs.boom = True
    s = p.poll("http://h:1")
    check(isinstance(s, MetricsSample) and s.last_end is None, "fetch_status raising -> sample without last_end")
    fs.boom = False
    fm.boom = True
    check(p.poll("http://h:1") is None, "fetch_metrics raising -> None, no exception")
    fm.boom = False
    for bad in (None, "x", [1]):
        fm.doc = bad
        check(p.poll("http://h:1") is None, f"fetch_metrics returning {bad!r} -> None")
    fm.doc = {"totals": "x", "live": "x"}
    s = p.poll("http://h:1")
    check(isinstance(s, MetricsSample) and s.live is None, "junk totals / live never raise")
    n = len(fm.calls)
    check(p.poll("") is None and len(fm.calls) == n, "no root URL: no fetch, None")

    # fake /metrics, no fetch_status: never the real HTTP /status
    fm2 = FakeFetch()
    p2 = StrataPoller(fetch_metrics=fm2)
    fm2.doc = pdoc(1)
    p2.poll("http://h:1")
    fm2.doc = pdoc(2)
    s = p2.poll("http://h:1")
    check(s is not None and s.last_end is None and net.urls == [],
          f"fake fetch_metrics + no fetch_status: no HTTP at all, no last_end ({net.urls})")
    # the defaults are the real fetchers (here: the disabled opener records the attempts)
    p3 = StrataPoller()
    check(p3.poll("http://127.0.0.1:9", catch_up=True) is None
          and net.urls == ["http://127.0.0.1:9/metrics?requests=all"],
          f"no fakes: the module's fetch_metrics is used ({net.urls})")
    p4 = StrataPoller(fetch_status=FakeStatus())
    net.urls.clear()
    check(p4.poll("http://127.0.0.1:9") is None and net.urls == ["http://127.0.0.1:9/metrics"],
          f"fake /status only: the real fetch_metrics, no catch-up suffix ({net.urls})")


def test_engine_wiring() -> None:
    import engines
    eng = engines.get_engine({})
    fm, fs = FakeFetch(), FakeStatus({"busy": False, "phase": "tool call complete"})
    p = eng.metrics_poller(fetch_metrics=fm, fetch_status=fs)
    fm.doc = pdoc(1)
    p.poll("http://h:1")
    fm.doc = pdoc(2)
    s = p.poll("http://h:1")
    check(isinstance(p, StrataPoller) and s.last_end == RequestEnd(True, None) and fs.calls == 1,
          "StrataEngine.metrics_poller(fetch_metrics=, fetch_status=) -> a StrataPoller with those fakes")
    st = MetricsStore()
    check(st.ingest(s) == 1 and st.decode_series() == [101.0], "its sample feeds the store")


def main() -> int:
    for fn in (test_strata_sample_mapping, test_strata_sample_garbage, test_ingest_equivalence, test_neutral_sample,
               test_activity_tool_keys, test_poller, test_engine_wiring):
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
