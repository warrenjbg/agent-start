"""usage_history: the local per-day / per-model usage store behind the History window.

Run: .venv\\Scripts\\python.exe tests\\test_usage_history.py
No network, no ports, no processes: every file lives in a temp dir (AGENT_START_HOME is redirected too, so the real
data folder is never touched).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AGENT_START_HOME"] = tempfile.mkdtemp(prefix="as_usage_home_")   # never the real %APPDATA%

import usage_history as uh  # noqa: E402
from engines.base import MetricsSample, RequestSample  # noqa: E402
from usage_history import UsageHistory  # noqa: E402

PASSES, FAILS = 0, []
TMP = Path(tempfile.mkdtemp(prefix="as_usage_"))


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"[PASS] {msg}")
    else:
        FAILS.append(msg)
        print(f"[FAIL] {msg}")


def local(y: int, mo: int, d: int, h: int = 12, mi: int = 0, s: int = 0) -> float:
    """Epoch seconds of a LOCAL wall time (whatever the machine's time zone is)."""
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


NOW = local(2026, 11, 13, 15, 0)            # the fixed "now" of most tests


def clock() -> float:
    return NOW


_n = [0]


def fresh(name: str | None = None) -> Path:
    _n[0] += 1
    d = TMP / f"case{_n[0]:03d}"
    d.mkdir()
    return d / (name or "usage.json")


def rq(key: str, t: float | None = None, tps: float | None = None, p: int | None = None, g: int | None = None):
    return RequestSample(key=key, time=t, decode_tps=tps, prompt_tokens=p, generated_tokens=g)


def sample(model: str | None, *reqs: RequestSample) -> MetricsSample:
    return MetricsSample(model=model, requests=tuple(reqs))


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def warnings(self) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]


CAP = Capture()
logging.getLogger("agent_start").addHandler(CAP)
logging.getLogger("agent_start").setLevel(logging.DEBUG)


# ------------------------------------------------------------------------------------------------ tests
def test_pure_helpers() -> None:
    days: dict = {}
    t = local(2026, 11, 12, 10)
    check(uh.merge_request(days, rq("a", t, 50.0, 1000, 100), "m1", NOW) == "2026-11-12", "merge -> request's day")
    uh.merge_request(days, rq("b", t, None, None, None), "m1", NOW)
    row = days["2026-11-12"]["m1"]
    check(row == {"requests": 2, "prompt_tokens": 1000, "generated_tokens": 100, "decode_s": 2.0,
                  "decode_tokens": 100, "served_s": 0.0, "gpu_tokens": 0, "gpu_wh": 0.0, "gpu_busy_wh": 0.0},
          f"merge sums one row without a file: {row}")
    uh.merge_request(days, rq("c", None), None, t)
    check(days["2026-11-12"]["(unknown)"]["requests"] == 1, "model None -> '(unknown)', time None -> fallback day")
    check(uh.model_key("  ") == "(unknown)" and uh.model_key(" x ") == "x", "model_key normalises")
    check(uh.request_ident(rq("")) is None and uh.request_ident(rq("k", 5)) == ("k", 5.0), "request_ident")
    d2 = {"2025-01-01": {}, "2026-11-13": {}, "2025-10-20": {}}
    check(uh.prune(d2, dt.date(2026, 11, 13), 390) == 1 and "2025-10-20" in d2 and "2025-01-01" not in d2,
          "prune keeps exactly retention_days calendar days")
    days, recent = uh.parse_doc(uh.build_doc(days, [("a", 1.5), ("b", None)]))
    check(days["2026-11-12"]["m1"]["requests"] == 2 and recent == [("a", 1.5), ("b", None)], "build/parse round trip")


def test_totals_per_day_and_model() -> None:
    h = UsageHistory(fresh(), clock=clock)
    d12, d13 = local(2026, 11, 12, 9), local(2026, 11, 13, 9)
    n = h.record(sample("alpha", rq("1", d12, 100.0, 1000, 200), rq("2", d12, 50.0, 500, 100)), now=NOW)
    n += h.record(sample("beta", rq("3", d13, 80.0, 300, 80)), now=NOW)
    n += h.record(sample("alpha", rq("4", d13, None, 10, 5)), now=NOW)
    check(n == 4, f"4 new requests ({n})")
    rows = h.days()
    check([r.date for r in rows] == [dt.date(2026, 11, 13), dt.date(2026, 11, 12)], "days() newest first")
    t13 = rows[0]
    check(t13.total.requests == 2 and t13.total.prompt_tokens == 310 and t13.total.generated_tokens == 85,
          "day 13 totals")
    check({m.model: m.requests for m in t13.models} == {"beta": 1, "alpha": 1}, "day 13 per-model rows")
    a12 = rows[1].models[0]
    check(a12.model == "alpha" and a12.requests == 2 and a12.prompt_tokens == 1500 and a12.generated_tokens == 300,
          "day 12 alpha row")
    check(len(h.days(limit=1)) == 1 and h.days(limit=0) == [], "days(limit)")
    s = h.totals()
    check(s.total.requests == 4 and s.active_days == 2 and s.first == dt.date(2026, 11, 12)
          and s.last == dt.date(2026, 11, 13), "totals() over everything")
    check({m.model: m.requests for m in s.models} == {"alpha": 3, "beta": 1} and s.models[0].model == "alpha",
          "totals per model, most requests first")
    s1 = h.totals(days=1)
    check(s1.total.requests == 2 and s1.active_days == 1, "totals(days=1) = today only")
    check(h.totals(days=2).total.requests == 4, "totals(days=2) = today + yesterday")
    e = UsageHistory(fresh(), clock=clock).totals()
    check(e.total.requests == 0 and e.first is None and e.models == () and e.total.decode_tps is None, "empty totals")


def test_local_midnight() -> None:
    h = UsageHistory(fresh(), clock=clock)
    before = local(2026, 11, 12, 23, 59, 59)
    after = local(2026, 11, 13, 0, 0, 1)
    h.record(sample("m", rq("late", before), rq("early", after)), now=NOW)
    got = {r.date.isoformat(): r.total.requests for r in h.days()}
    check(got == {"2026-11-12": 1, "2026-11-13": 1}, f"23:59:59 / 00:00:01 local go to their own days: {got}")
    h.record(sample("m", rq("notime", None)), now=local(2026, 11, 11, 8))
    check({r.date.isoformat(): r.total.requests for r in h.days()}.get("2026-11-11") == 1,
          "time=None -> the poll's wall clock (now=)")
    h.record(sample("m", rq("notime2", None)))
    check(h.days()[0].total.requests == 2 and h.days()[0].date == dt.date(2026, 11, 13),
          "time=None and no now -> the clock")


def test_repeats_and_restart() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock)
    reqs = [rq(f"r{i}", NOW - 100 + i, 90.0, 1000, 100) for i in range(12)]
    check(h.record(sample("m", *reqs), now=NOW) == 12, "first poll: 12 new")
    check(h.record(sample("m", *reqs), now=NOW) == 0, "same 12 again: 0 new")
    check(h.record(sample("m", *reqs[6:], rq("r12", NOW, 90.0, 1000, 100)), now=NOW) == 1, "overlap: only the new one")
    check(h.totals().total.requests == 13, "13 counted once each")
    check(h.flush() is True, "flushed")
    # the app restarts while the same server keeps serving: the first poll is a catch-up returning everything
    h2 = UsageHistory(path, clock=clock)
    allr = reqs + [rq("r12", NOW, 90.0, 1000, 100), rq("r13", NOW + 1, 90.0, 1000, 100)]
    check(h2.record(sample("m", *allr), now=NOW) == 1, "after restart only the really new request counts")
    check(h2.totals().total.requests == 14, "restart did not double-count")
    # same key but another time (a later server run reusing ids, adopted without begin_session)
    check(h2.record(sample("m", rq("r0", NOW + 50, 90.0, 10, 20))) == 1, "same key with another time = new request")
    # begin_session: a new server run, the same key may count again
    h2.begin_session("m")
    check(h2.record(sample("m", rq("r1", NOW - 99, 90.0, 1000, 100))) == 1, "begin_session lets the same key count again")
    h2.flush()
    h3 = UsageHistory(path, clock=clock)
    check(h3.record(sample("m", rq("r2", NOW - 98, 90.0, 1000, 100))) == 1,
          "begin_session also forgot the saved keys (a restart after it does not block old-run keys)")
    check(h3.record(sample("m", rq("r1", NOW - 99, 90.0, 1000, 100))) == 0, "keys of the new session are saved")
    check(h3.record(None) == 0 and h3.record(MetricsSample()) == 0, "None / empty sample -> 0")
    check(h3.record(sample("m", RequestSample(key=""), rq("", NOW))) == 0, "requests without a key are skipped")


def test_speed() -> None:
    h = UsageHistory(fresh(), clock=clock)
    t = NOW - 60
    h.record(sample("m",
                    rq("a", t, 100.0, 100, 1000),       # 10 s
                    rq("b", t, 10.0, 100, 10),          # 1 s
                    rq("c", t, None, 100, 500),         # no speed: tokens count, speed does not
                    rq("d", t, 50.0, None, None),       # speed without tokens: ignored for speed
                    rq("e", t, 0.0, 5, 50),             # zero speed: ignored
                    rq("f", t)))                         # nothing known: still a request
    u = h.totals().total
    check(u.requests == 6 and u.generated_tokens == 1560 and u.prompt_tokens == 305, f"counts {u}")
    check(u.decode_tokens == 1010 and abs(u.decode_s - 11.0) < 1e-9, "only requests with speed AND tokens")
    check(u.decode_tps is not None and abs(u.decode_tps - 1010 / 11.0) < 1e-9,
          f"token-weighted average {u.decode_tps:.2f} (simple mean would be 55)")
    h2 = UsageHistory(fresh(), clock=clock)
    h2.record(sample("m", rq("x", t, None, 10, 10)))
    check(h2.totals().total.decode_tps is None and h2.days()[0].models[0].decode_tps is None, "no usable speed -> None")


def test_add_served() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock)
    h.add_served("m", 60.0)
    h.add_served("m", 30.5, now=NOW + 10)
    h.add_served(None, 12.0)
    h.add_served("m", 40.0, now=local(2026, 11, 12, 22))
    h.add_served("m", -5)
    h.add_served("m", float("nan"))
    h.add_served("m", 0)
    rows = {r.date.isoformat(): r for r in h.days()}
    m13 = {m.model: m for m in rows["2026-11-13"].models}
    check(abs(m13["m"].served_s - 90.5) < 1e-9 and m13["m"].requests == 0, "served time adds up, no requests")
    check(abs(m13["(unknown)"].served_s - 12.0) < 1e-9, "model None -> '(unknown)' row")
    check(abs(rows["2026-11-12"].total.served_s - 40.0) < 1e-9, "served time goes to the day of now")
    check(abs(h.totals().total.served_s - 142.5) < 1e-9, "negative / NaN / zero ignored")
    h.flush()
    check(abs(UsageHistory(path, clock=clock).totals().total.served_s - 142.5) < 1e-6, "served time persisted")


def test_flush_only_when_dirty() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock)
    check(h.flush() is False and not path.exists(), "nothing recorded -> no file written")
    h.record(sample("m", rq("a", NOW)))
    check(h.dirty and h.flush() is True and path.exists() and not h.dirty, "dirty -> written, clean afterwards")
    m1 = path.stat().st_mtime_ns
    check(h.flush() is False and path.stat().st_mtime_ns == m1, "second flush writes nothing")
    h.record(sample("m", rq("a", NOW)))
    check(h.flush() is False, "a repeat does not dirty")
    doc = json.loads(path.read_text(encoding="utf-8"))
    check(doc.get("version") == 1 and doc["days"]["2026-11-13"]["m"]["requests"] == 1 and doc["recent"] == [["a", NOW]],
          "file schema version 1")
    h2 = UsageHistory(path, clock=clock)
    check(not h2.dirty and h2.flush() is False, "loading a good file does not dirty it")
    check(not [p for p in path.parent.iterdir() if p.name != "usage.json"], "no temp files left behind")


def test_atomic_write() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock)
    h.record(sample("m", rq("a", NOW)))
    h.flush()
    good = path.read_bytes()
    h.record(sample("m", rq("b", NOW)))
    real = os.replace
    calls = [0]

    def boom(src, dst):
        calls[0] += 1
        raise OSError("simulated replace failure")

    CAP.records.clear()
    os.replace = boom
    try:
        ok = h.flush()
        ok2 = h.flush()
    except Exception as exc:  # noqa: BLE001
        ok = ok2 = None
        check(False, f"flush raised {exc!r}")
    finally:
        os.replace = real
    check(ok is False and ok2 is False and calls[0] == 2, "failed replace -> False, no exception, retried next flush")
    check(path.read_bytes() == good, "old file intact")
    check(not [p for p in path.parent.iterdir() if p.name != "usage.json"], "temp file removed after the failure")
    w = CAP.warnings()
    check(len(w) == 1 and "could not save" in w[0], f"one warning for the failing streak: {w}")
    check(h.dirty and h.flush() is True, "next flush after the fault writes")
    check(UsageHistory(path, clock=clock).totals().total.requests == 2, "nothing lost")
    # PermissionError (a scanner holding the file) is retried within the same flush
    h.record(sample("m", rq("c", NOW)))
    n = [0]

    def flaky(src, dst):
        n[0] += 1
        if n[0] < 3:
            raise PermissionError("locked")
        real(src, dst)

    os.replace = flaky
    try:
        check(h.flush() is True and n[0] == 3, "brief PermissionError retried inside one flush")
    finally:
        os.replace = real
    # a folder that cannot be created: logged, not raised
    blocker = fresh("file")
    blocker.write_text("x")
    hb = UsageHistory(blocker / "sub" / "usage.json", clock=clock)
    hb.record(sample("m", rq("a", NOW)))
    check(hb.flush() is False and hb.dirty, "unwritable location -> False, stays dirty")


def _bad_case(content: bytes | str, label: str) -> None:
    path = fresh()
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    CAP.records.clear()
    try:
        h = UsageHistory(path, clock=clock)
    except Exception as exc:  # noqa: BLE001
        check(False, f"{label}: raised {exc!r}")
        return
    bad = path.with_name("usage.json.bad")
    ok = (h.totals().total.requests == 0 and h.days() == [] and bad.exists() and bad.read_bytes() == content
          and not path.exists() and any("unusable" in w for w in CAP.warnings()))
    check(ok, f"{label}: empty history, .bad kept, warning, no raise")
    h.record(sample("m", rq("a", NOW)))
    check(h.flush() and UsageHistory(path, clock=clock).totals().total.requests == 1, f"{label}: then saves fresh")


def test_corrupt_files() -> None:
    good_row = {"requests": 1, "prompt_tokens": 1, "generated_tokens": 1, "decode_s": 1.0, "decode_tokens": 1,
                "served_s": 1.0}

    def doc(**over):
        d = {"version": 1, "days": {"2026-11-13": {"m": dict(good_row)}}, "recent": [["a", 1.0]]}
        d.update(over)
        return json.dumps(d)

    _bad_case("{not json", "corrupt JSON")
    _bad_case(b"\xff\xfe\x00garbage", "binary junk")
    _bad_case("", "empty file")
    _bad_case("[" * 100000 + "]" * 100000, "deeply nested JSON")
    _bad_case(doc(version=2), "wrong version (2)")
    _bad_case(doc(version="1"), "version as text")
    _bad_case(doc(version=True), "version true")
    _bad_case(json.dumps({"days": {}}), "no version")
    for junk in ([], "x", 5, None):
        _bad_case(json.dumps(junk), f"top level {junk!r}")
        _bad_case(doc(days=junk), f"days = {junk!r}")
        _bad_case(doc(days={"2026-11-13": junk}), f"day value = {junk!r}")
        _bad_case(doc(days={"2026-11-13": {"m": junk}}), f"model row = {junk!r}")
        if junk != 5:                                # 5 is a valid counter
            _bad_case(doc(days={"2026-11-13": {"m": dict(good_row, requests=junk)}}), f"requests = {junk!r}")
            _bad_case(doc(days={"2026-11-13": {"m": dict(good_row, decode_s=junk)}}), f"decode_s = {junk!r}")
        _bad_case(doc(recent=[junk]), f"recent entry = {junk!r}")
    _bad_case(doc(recent={}), "recent = {}")
    _bad_case(doc(days={"13.11.2026": {"m": good_row}}), "bad day key")
    _bad_case(doc(days={"2026-02-30": {"m": good_row}}), "impossible date")
    _bad_case(doc(days={"2026-11-13": {"": good_row}}), "empty model id")
    _bad_case(doc(days={"2026-11-13": {"m": dict(good_row, requests=-1)}}), "negative count")
    _bad_case(doc(days={"2026-11-13": {"m": dict(good_row, requests=1.5)}}), "fractional count")
    _bad_case(doc(days={"2026-11-13": {"m": dict(good_row, requests=True)}}), "bool count")
    _bad_case(doc(days={"2026-11-13": {"m": dict(good_row, served_s="NaN")}}), "text float")
    _bad_case(doc(recent=[["a", "x"]]), "recent time as text")
    _bad_case(doc(recent=[["", 1.0]]), "recent empty key")
    _bad_case(doc(recent=[["a", 1.0, 2]]), "recent triple")
    # a second corruption replaces the older .bad (one copy, not a pile)
    path = fresh()
    path.write_text("junk1")
    UsageHistory(path, clock=clock)
    path.write_text("junk2")
    UsageHistory(path, clock=clock)
    check(path.with_name("usage.json.bad").read_text() == "junk2" and len(list(path.parent.iterdir())) == 1,
          "one .bad copy (the newest)")
    # tolerated: missing counters read as 0, unknown keys ignored, BOM
    path = fresh()
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"version": 1, "days": {"2026-11-13": {"m": {"requests": 3}}},
                                                   "extra": 1}).encode())
    h = UsageHistory(path, clock=clock)
    check(h.totals().total.requests == 3 and not path.with_name("usage.json.bad").exists(),
          "missing fields = 0, extra keys ignored, BOM ok")


def test_unreadable_file_not_clobbered() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock)
    h.record(sample("m", rq("old", NOW - 10, 50.0, 10, 100)))
    h.flush()
    real = Path.read_bytes
    state = {"fail": True}

    def locked(self):
        if state["fail"] and self == path:
            raise PermissionError("locked by a scanner")
        return real(self)

    Path.read_bytes = locked                         # type: ignore[method-assign]
    try:
        h2 = UsageHistory(path, clock=clock)
        h2.record(sample("m", rq("new", NOW, 50.0, 10, 100)))
        check(h2.flush() is False and json.loads(real(path))["days"]["2026-11-13"]["m"]["requests"] == 1,
              "still unreadable -> not saved, the old file is not overwritten")
        state["fail"] = False
        check(h2.flush() is True, "readable again -> saved")
    finally:
        Path.read_bytes = real                       # type: ignore[method-assign]
    h3 = UsageHistory(path, clock=clock)
    check(h3.totals().total.requests == 2 and h3.record(sample("m", rq("old", NOW - 10))) == 0,
          "late read merged: old + new history, old keys still known")


def test_retention() -> None:
    path = fresh()
    days = {(dt.date(2026, 11, 13) - dt.timedelta(days=i)).isoformat(): {"m": {"requests": 1}} for i in range(20)}
    path.write_text(json.dumps({"version": 1, "days": days}))
    h = UsageHistory(path, clock=clock, retention_days=10)
    check(len(h.days()) == 10 and h.days()[-1].date == dt.date(2026, 11, 4), "load drops days older than retention")
    check(h.dirty and h.flush(), "pruned on load -> rewritten")
    check(len(json.loads(path.read_text())["days"]) == 10, "file shrank")
    check(h.record(sample("m", rq("ancient", local(2026, 10, 1)))) == 0 and len(h.days()) == 10,
          "a request older than retention is not added")
    # time moves on: flush prunes too
    t = [NOW]
    h2 = UsageHistory(path, clock=lambda: t[0], retention_days=10)
    t[0] = NOW + 3 * 86400
    h2.add_served("m", 1.0)
    h2.flush()
    check(len(h2.days()) == 8 and min(r.date for r in h2.days()) == dt.date(2026, 11, 7), "flush prunes old days")


def test_bounded_seen() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock, max_seen=50, recent_persist=20)
    h.record(sample("m", *[rq(f"k{i}", NOW - 1000 + i) for i in range(200)]))
    check(len(h._seen) == 50, f"in-memory keys bounded ({len(h._seen)})")
    check(h.record(sample("m", rq("k199", NOW - 801))) == 0, "newest keys still known")
    h.flush()
    check(len(json.loads(path.read_text())["recent"]) == 20, "saved list bounded")
    big = UsageHistory(fresh(), clock=clock)
    big.record(sample("m", *[rq(f"k{i}", NOW - 9000 + i) for i in range(7000)]))
    big.flush()
    check(len(big._seen) == uh.MAX_SEEN and len(json.loads(big.path.read_text())["recent"]) == uh.RECENT_PERSIST,
          f"defaults: {uh.MAX_SEEN} in memory, {uh.RECENT_PERSIST} saved")
    # a key that keeps being reported stays known even after many others pass
    h3 = UsageHistory(fresh(), clock=clock, max_seen=10)
    h3.record(sample("m", rq("sticky", NOW)))
    for i in range(30):
        h3.record(sample("m", rq("sticky", NOW), rq(f"x{i}", NOW)))
    check(h3.record(sample("m", rq("sticky", NOW))) == 0 and h3.totals().total.requests == 31,
          "a still-reported key is refreshed, not evicted")


def test_threads() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock)
    errors: list[BaseException] = []
    n_threads, per = 8, 400

    def worker(tid: int) -> None:
        try:
            for i in range(per):
                # every thread also re-sends a shared set of keys: each must still count once
                h.record(sample(f"m{tid % 2}", rq(f"t{tid}-{i}", NOW - i, 50.0, 10, 20), rq(f"shared{i % 50}", NOW)),
                         now=NOW)
                h.add_served(f"m{tid % 2}", 0.5)
                if i % 50 == 0:
                    h.flush()
                    h.days(3)
                    h.totals(7)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ths = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    h.flush()
    u = h.totals().total
    check(not errors, f"no thread errors {errors[:1]}")
    check(u.requests == n_threads * per + 50, f"exact request count ({u.requests})")
    check(u.generated_tokens == n_threads * per * 20 and abs(u.served_s - n_threads * per * 0.5) < 1e-6,
          "exact token / served totals")
    check(UsageHistory(path, clock=clock).totals().total.requests == u.requests, "file matches memory")


def test_large_file_speed() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock)
    start = dt.date(2026, 11, 13)
    for i in range(400):
        d = start - dt.timedelta(days=i)
        for m in ("swift-1.5-iq3_s", "qwen-iq2_xs", "coder-iq1_m"):
            h._days.setdefault(d.isoformat(), {})[m] = {"requests": 120, "prompt_tokens": 2_000_000,
                                                        "generated_tokens": 90_000, "decode_s": 1000.123,
                                                        "decode_tokens": 90_000, "served_s": 30000.5,
                                                        "gpu_tokens": 90_000, "gpu_wh": 2345.6789,
                                                        "gpu_busy_wh": 300.1234}
    h.record(sample("m", *[rq(f"r{i}", NOW - i) for i in range(1000)]))
    t0 = time.perf_counter()
    h.flush()
    t_flush = (time.perf_counter() - t0) * 1000
    size = path.stat().st_size
    t0 = time.perf_counter()
    h2 = UsageHistory(path, clock=clock)
    t_load = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    rows = h2.days()
    s = h2.totals(30)
    t_read = (time.perf_counter() - t0) * 1000
    h2.add_served("m", 1)
    t0 = time.perf_counter()
    h2.flush()
    t_flush2 = (time.perf_counter() - t0) * 1000
    print(f"     400 days x 3 models + 1000 keys: {size / 1024:.0f} KiB; flush {t_flush:.1f} ms, load {t_load:.1f} ms, "
          f"days()+totals(30) {t_read:.1f} ms, re-flush {t_flush2:.1f} ms")
    check(len(rows) == 400 and s.active_days == 30, "all 400 days loaded")
    check(t_load < 500 and t_flush < 500 and t_flush2 < 500 and t_read < 500, "load / flush / read well under 0.5 s")


def _near(a: float, b: float, eps: float = 1e-9) -> bool:
    return abs(a - b) < eps


def test_energy_old_file() -> None:
    path = fresh()
    old = {"version": 1, "days": {"2026-11-12": {"m": {"requests": 4, "prompt_tokens": 100, "generated_tokens": 50,
                                                         "decode_s": 1.5, "decode_tokens": 40, "served_s": 600.0}}},
           "recent": [["a", 1.0]]}
    path.write_text(json.dumps(old))
    h = UsageHistory(path, clock=clock)
    u = h.totals().total
    check(u.requests == 4 and u.prompt_tokens == 100 and u.generated_tokens == 50 and u.decode_tokens == 40
          and _near(u.decode_s, 1.5) and _near(u.served_s, 600.0), "old-format file: every old number kept")
    check(u.gpu_wh == 0.0 and u.gpu_busy_wh == 0.0 and u.gpu_tokens == 0 and not u.has_energy
          and u.tokens_per_wh is None, "old-format file: energy fields read as zero, no energy, no tok/Wh")
    check(not h.dirty and not path.with_name("usage.json.bad").exists() and h.record(sample("m", rq("a", 1.0))) == 0,
          "old-format file: not set aside, not dirty, its saved keys still known")
    h.add_served("m", 1.0, now=local(2026, 11, 12, 13))
    check(h.flush() is True, "old-format file: saved after a change")
    doc = json.loads(path.read_text(encoding="utf-8"))
    row = doc["days"]["2026-11-12"]["m"]
    check(doc["version"] == 1 and row["requests"] == 4 and row["generated_tokens"] == 50 and row["served_s"] == 601.0
          and row["decode_s"] == 1.5, "saved: version still 1, old numbers kept")
    check(row["gpu_wh"] == 0.0 and row["gpu_busy_wh"] == 0.0 and row["gpu_tokens"] == 0
          and isinstance(row["gpu_tokens"], int), "saved: the new fields are written (as 0)")
    # a row with only some of the new fields: the missing ones read as 0
    path2 = fresh()
    path2.write_text(json.dumps({"version": 1, "days": {"2026-11-13": {"m": {"requests": 1, "gpu_wh": 2.5}}}}))
    u2 = UsageHistory(path2, clock=clock).totals().total
    check(u2.requests == 1 and u2.gpu_wh == 2.5 and u2.gpu_busy_wh == 0.0 and u2.gpu_tokens == 0 and u2.has_energy
          and u2.tokens_per_wh is None, "partial energy fields: the missing ones are 0")


def test_energy_round_trip_and_junk() -> None:
    days: dict = {}
    uh.merge_request(days, rq("a", NOW, 50.0, 10, 400), "m", NOW, metered=True)
    uh.add_wh(days, "m", 1.23456789, 0.98765432, "2026-11-13")
    doc = uh.build_doc(days, [])
    row = doc["days"]["2026-11-13"]["m"]
    check(row["gpu_wh"] == 1.2346 and row["gpu_busy_wh"] == 0.9877 and row["gpu_tokens"] == 400,
          f"build_doc writes the energy fields, watt-hours rounded to 4 decimals ({row})")
    back, _recent = uh.parse_doc(json.loads(json.dumps(doc)))
    r2 = back["2026-11-13"]["m"]
    check(r2["gpu_wh"] == 1.2346 and r2["gpu_busy_wh"] == 0.9877 and r2["gpu_tokens"] == 400
          and isinstance(r2["gpu_tokens"], int), "parse_doc reads them back")
    path = fresh()
    h = UsageHistory(path, clock=clock)
    h.record(sample("m", rq("a", NOW, 50.0, 10, 400)), now=NOW, metered=True)
    h.add_energy("m", 1.23456789, 0.5, now=NOW)
    h.flush()
    u = UsageHistory(path, clock=clock).totals().total
    check(u.gpu_tokens == 400 and _near(u.gpu_wh, 1.2346) and _near(u.gpu_busy_wh, 0.5)
          and u.tokens_per_wh is not None and _near(u.tokens_per_wh, 800.0), "file round trip of the energy fields")
    good_row = {"requests": 1, "prompt_tokens": 1, "generated_tokens": 1, "decode_s": 1.0, "decode_tokens": 1,
                "served_s": 1.0, "gpu_tokens": 1, "gpu_wh": 1.0, "gpu_busy_wh": 0.5}

    def doc_with(**over) -> str:
        return json.dumps({"version": 1, "days": {"2026-11-13": {"m": dict(good_row, **over)}}, "recent": []})

    ok_path = fresh()
    ok_path.write_text(doc_with())
    check(UsageHistory(ok_path, clock=clock).totals().total.gpu_tokens == 1, "a valid row with energy loads")
    for field in ("gpu_wh", "gpu_busy_wh", "gpu_tokens"):
        for junk in (-1, "1", None, True, [], {}):
            _bad_case(doc_with(**{field: junk}), f"{field} = {junk!r}")
    _bad_case(doc_with(gpu_tokens=1.5), "fractional gpu_tokens")
    _bad_case(doc_with(gpu_wh="NaN"), "gpu_wh as text NaN")


def test_add_energy() -> None:
    path = fresh()
    h = UsageHistory(path, clock=clock)
    h.add_energy("m", 2.0, 1.5)                                 # now = the clock
    h.add_energy("m", 1.0, 0.25, now=NOW + 10)
    h.add_energy(None, 0.5, 0.1, now=NOW)
    h.add_energy("m", 3.0, 1.0, now=local(2026, 11, 12, 22))
    h.add_energy("other", 0.4, 0.4, now=local(2026, 11, 12, 23, 59, 59))
    h.add_energy("m", 1.0, 5.0, now=NOW)                         # busy > wh: clamped to wh
    h.add_energy("m", 1.0, float("nan"), now=NOW)                # bad busy: wh counts, busy 0
    h.add_energy("m", 1.0, -2.0, now=NOW)
    rows = {r.date.isoformat(): r for r in h.days()}
    m13 = {m.model: m for m in rows["2026-11-13"].models}
    check(_near(m13["m"].gpu_wh, 6.0) and _near(m13["m"].gpu_busy_wh, 2.75),
          f"energy adds up per day and model, busy clamped ({m13['m'].gpu_wh}, {m13['m'].gpu_busy_wh})")
    check(_near(m13["(unknown)"].gpu_wh, 0.5) and m13["(unknown)"].requests == 0, "model None -> '(unknown)' row")
    m12 = {m.model: m for m in rows["2026-11-12"].models}
    check(_near(m12["m"].gpu_wh, 3.0) and _near(m12["other"].gpu_busy_wh, 0.4) and _near(rows["2026-11-12"].total.gpu_wh,
          3.4), "energy goes to the LOCAL day of now (23:59:59 stays on its day)")
    before = h.totals().total
    for wh, busy in ((-1.0, 0.5), (float("nan"), 0.5), (float("inf"), 0.0), (float("-inf"), 0.0), (0.0, 0.0),
                     (0.0, 1.0), ("1", 0.5), (True, 0.5), (None, 0.5)):
        h.add_energy("m", wh, busy, now=NOW)
    after = h.totals().total
    check(after.gpu_wh == before.gpu_wh and after.gpu_busy_wh == before.gpu_busy_wh,
          "negative / non-finite / zero / non-number wh ignored")
    check(all(m.gpu_busy_wh <= m.gpu_wh + 1e-12 for r in h.days() for m in r.models), "busy <= wh in every row")
    h2 = UsageHistory(fresh(), clock=clock)
    h2.add_energy("m", float("nan"), 1.0, now=NOW)
    h2.add_energy("m", 0.0, 0.0, now=NOW)
    check(not h2.dirty, "ignored calls do not dirty the history")
    h2.add_energy("m", 0.001, 0.0, now=NOW)
    check(h2.dirty, "a valid add_energy dirties it")
    h.flush()
    u = UsageHistory(path, clock=clock).totals().total
    check(_near(u.gpu_wh, 9.9, 1e-6) and _near(u.gpu_busy_wh, 4.25, 1e-6), f"energy persisted ({u.gpu_wh})")
    # threads: exact sum under the lock
    h3 = UsageHistory(fresh(), clock=clock)

    def worker() -> None:
        for _ in range(500):
            h3.add_energy("m", 0.25, 0.125, now=NOW)

    ths = [threading.Thread(target=worker) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    u3 = h3.totals().total
    check(u3.gpu_wh == 500.0 and u3.gpu_busy_wh == 250.0, f"4 threads x 500 add_energy: exact sums ({u3.gpu_wh})")


def test_record_metered() -> None:
    h = UsageHistory(fresh(), clock=clock)
    n = h.record(sample("m", rq("a", NOW - 60, 50.0, 100, 1000), rq("b", NOW - 50, None, 10, 200)), now=NOW)
    n += h.record(sample("m", rq("e", NOW - 45, 50.0, 100, 7)), now=NOW, metered=False)
    u = h.totals().total
    check(n == 3 and u.generated_tokens == 1207 and u.gpu_tokens == 0, "default / metered=False: no gpu_tokens")
    n = h.record(sample("m", rq("c", NOW - 40, 50.0, 100, 300), rq("d", NOW - 30, None, None, None),
                        rq("f", NOW - 20, None, 5, 20)), now=NOW, metered=True)
    u = h.totals().total
    check(n == 3 and u.generated_tokens == 1527 and u.gpu_tokens == 320 and u.requests == 6,
          "metered=True: new requests' generated tokens also go to gpu_tokens (with or without a speed)")
    h.record(sample("m", rq("a", NOW - 60, 50.0, 100, 1000), rq("c", NOW - 40, 50.0, 100, 300)), now=NOW, metered=True)
    check(h.totals().total.gpu_tokens == 320, "a repeat of counted requests adds nothing, metered or not")
    h.record(sample("m", rq("old", local(2026, 11, 12, 9), None, 1, 50)), now=NOW, metered=True)
    rows = {r.date.isoformat(): r.total for r in h.days()}
    check(rows["2026-11-12"].gpu_tokens == 50 and rows["2026-11-13"].gpu_tokens == 320,
          "gpu_tokens go to the request's own day, like generated_tokens")
    days: dict = {}
    uh.merge_request(days, rq("x", NOW, None, 1, 9), "m", NOW)
    uh.merge_request(days, rq("y", NOW, None, 1, 11), "m", NOW, metered=True)
    check(days["2026-11-13"]["m"]["gpu_tokens"] == 11, "merge_request(metered=...)")


def test_energy_meter() -> None:
    m = uh.EnergyMeter()
    check(not m.metering and m.take() == (0.0, 0.0), "a new meter: nothing, not metering")
    check(m.step(1000.0, 10.0, True, False) == 0.0 and m.metering, "the first serving poll only sets the baseline")
    check(abs(m.step(1360.0, 11.0, True, False) - 0.1) < 1e-9, "360 J between two serving polls = 0.1 Wh")
    m.step(1720.0, 12.0, True, True)             # the reply started in this interval
    m.step(2080.0, 13.0, True, False)            # ... and ended in this one: both count as busy
    m.step(2440.0, 14.0, True, False)
    wh, busy = m.take()
    check(abs(wh - 0.4) < 1e-9 and abs(busy - 0.2) < 1e-9, f"serving 0.4 Wh, of it 0.2 Wh with a reply ({wh}, {busy})")
    check(m.take() == (0.0, 0.0) and m.metering, "take() empties the totals and keeps the baseline")
    check(m.step(2440.0, 15.0, True, True) == 0.0, "a total that did not grow (the sampler is slower) adds nothing")
    check(abs(m.step(2800.0, 16.0, True, False) - 0.1) < 1e-9 and abs(m.take()[1] - 0.1) < 1e-9,
          "...and the next growth counts as busy: the reply was running at the poll before")
    check(m.step(999999.0, 30.0, True, True) == 0.0 and m.take() == (0.0, 0.0),
          "a gap longer than 5 s (sleep, a stalled worker) adds nothing")
    check(abs(m.step(999999.0 + 36.0, 35.0, True, False) - 0.01) < 1e-9, "exactly 5 s still counts")
    m.take()
    check(m.step(5.0, 36.0, True, False) == 0.0 and m.step(41.0, 37.0, True, False) > 0,
          "a total that went backwards adds nothing and becomes the new baseline")
    m.take()
    check(m.step(100.0, 38.0, False, True) == 0.0 and not m.metering, "not serving: nothing, baseline dropped")
    check(m.step(460.0, 39.0, True, True) == 0.0 and m.metering and m.take() == (0.0, 0.0),
          "serving again: the energy used while it was not serving is never counted")
    for junk in (None, "5", True, float("nan"), float("inf")):
        m.step(500.0, 40.0, True, False)
        check(m.step(junk, 41.0, True, False) == 0.0 and not m.metering, f"no reading ({junk!r}): baseline dropped")
        m.step(500.0, 40.0, True, False)
        check(m.step(900.0, junk, True, False) == 0.0 and not m.metering, f"junk time ({junk!r}): baseline dropped")
    m.take()                                     # the loop's first step followed a serving poll: it counted
    check(m.step(900.0, 39.0, True, False) == 0.0 and m.step(1000.0, 38.0, True, False) == 0.0 and m.take() == (0.0, 0.0),
          "time going backwards adds nothing")
    m.pause()
    check(not m.metering, "pause()")
    # into the history, the way the app does it
    h = UsageHistory(fresh(), clock=clock)
    m = uh.EnergyMeter()
    for i, (j, busy) in enumerate(((0.0, False), (3600.0, True), (7200.0, False), (10800.0, False))):
        metered = m.metering
        m.step(j, 100.0 + i, True, busy)
        if i == 2:
            h.record(sample("m", rq("r", NOW, 50.0, 10, 500)), now=NOW, metered=metered)
    h.add_energy("m", *m.take(), now=NOW)
    u = h.totals().total
    check(abs(u.gpu_wh - 3.0) < 1e-9 and abs(u.gpu_busy_wh - 2.0) < 1e-9 and u.gpu_tokens == 500
          and u.tokens_per_wh == 250.0, f"meter -> history: 3 Wh, 2 Wh busy, 250 tok/Wh ({u.gpu_wh}, {u.gpu_busy_wh})")


def test_energy_read_model() -> None:
    U = uh.Usage
    check(U(gpu_tokens=1000, gpu_busy_wh=0.5, gpu_wh=1.0).tokens_per_wh == 2000.0
          and U(gpu_wh=1.0).has_energy, "tokens_per_wh = gpu_tokens / gpu_busy_wh; has_energy = gpu_wh > 0")
    check(U(gpu_wh=1.0, gpu_busy_wh=0.0, gpu_tokens=10).tokens_per_wh is None
          and U(gpu_wh=1.0, gpu_busy_wh=0.5).tokens_per_wh is None and U(gpu_tokens=10).tokens_per_wh is None,
          "tokens_per_wh None without busy energy or without tokens")
    check(not U().has_energy and not U(gpu_tokens=5, gpu_busy_wh=0.0).has_energy and U().tokens_per_wh is None,
          "no energy: has_energy False")
    mu = uh.ModelUsage(model="x", gpu_wh=2.0, gpu_busy_wh=1.0, gpu_tokens=300)
    check(mu.has_energy and mu.tokens_per_wh == 300.0 and mu.model == "x", "ModelUsage carries the energy fields")
    # sums over days and models
    h = UsageHistory(fresh(), clock=clock)
    d12, d13 = local(2026, 11, 12, 9), local(2026, 11, 13, 9)
    h.record(sample("a", rq("1", d12, 50.0, 10, 100)), now=NOW, metered=True)
    h.record(sample("b", rq("2", d12, 50.0, 10, 300)), now=NOW, metered=True)
    h.record(sample("a", rq("3", d13, 50.0, 10, 500)), now=NOW, metered=True)
    h.record(sample("a", rq("4", d13, 50.0, 10, 1000)), now=NOW)               # not metered
    h.add_energy("a", 1.0, 0.5, now=d12)
    h.add_energy("b", 2.0, 1.0, now=d12)
    h.add_energy("a", 4.0, 2.0, now=d13)
    rows = {r.date.isoformat(): r for r in h.days()}
    t12 = rows["2026-11-12"].total
    check(_near(t12.gpu_wh, 3.0) and _near(t12.gpu_busy_wh, 1.5) and t12.gpu_tokens == 400
          and t12.tokens_per_wh is not None and _near(t12.tokens_per_wh, 400 / 1.5), "day total sums its models")
    s = h.totals()
    per = {m.model: m for m in s.models}
    check(_near(s.total.gpu_wh, 7.0) and _near(s.total.gpu_busy_wh, 3.5) and s.total.gpu_tokens == 900
          and s.total.generated_tokens == 1900, "totals() sums energy over days")
    check(_near(per["a"].gpu_wh, 5.0) and per["a"].gpu_tokens == 600 and _near(per["b"].gpu_wh, 2.0)
          and per["b"].tokens_per_wh is not None and _near(per["b"].tokens_per_wh, 300.0),
          "totals() per model across days")
    s1 = h.totals(days=1)
    check(_near(s1.total.gpu_wh, 4.0) and s1.total.gpu_tokens == 500 and s1.total.tokens_per_wh == 250.0,
          "totals(days=1): today's energy only")
    e = UsageHistory(fresh(), clock=clock).totals().total
    check(not e.has_energy and e.tokens_per_wh is None and e.gpu_tokens == 0, "empty history: no energy")
    check(uh.joules_to_wh(3600) == 1.0 and uh.joules_to_wh(277) == 277 / 3600 and uh.joules_to_wh(0) == 0.0
          and uh.joules_to_wh(-5) == 0.0 and uh.joules_to_wh(float("nan")) == 0.0 and uh.joules_to_wh(None) == 0.0
          and uh.joules_to_wh(True) == 0.0, "joules_to_wh (bad values -> 0.0)")


def test_energy_retention() -> None:
    path = fresh()
    days = {(dt.date(2026, 11, 13) - dt.timedelta(days=i)).isoformat():
            {"m": {"requests": 1, "gpu_wh": 1.0, "gpu_busy_wh": 0.5, "gpu_tokens": 10}} for i in range(20)}
    path.write_text(json.dumps({"version": 1, "days": days}))
    h = UsageHistory(path, clock=clock, retention_days=10)
    u = h.totals().total
    check(len(h.days()) == 10 and _near(u.gpu_wh, 10.0) and u.gpu_tokens == 100,
          "retention drops whole days, energy of the kept days intact")
    h.add_energy("m", 1.0, 0.5, now=local(2026, 10, 1))         # older than retention: dropped at the next flush
    h.flush()
    doc = json.loads(path.read_text())
    check(len(doc["days"]) == 10 and "2026-10-01" not in doc["days"]
          and _near(UsageHistory(path, clock=clock, retention_days=10).totals().total.gpu_wh, 10.0),
          "an add_energy for a day outside retention is pruned like add_served")


def test_default_path() -> None:
    import config_store
    h = UsageHistory(clock=clock)
    check(h.path == Path(config_store.DATA_DIR) / "usage.json"
          and str(h.path).startswith(os.environ["AGENT_START_HOME"]), "default path is <DATA_DIR>/usage.json")
    check(not h.path.exists(), "constructing does not create the file")


def main() -> int:
    for fn in (test_pure_helpers, test_totals_per_day_and_model, test_local_midnight, test_repeats_and_restart,
               test_speed, test_add_served, test_flush_only_when_dirty, test_atomic_write, test_corrupt_files,
               test_unreadable_file_not_clobbered, test_retention, test_bounded_seen, test_threads,
               test_large_file_speed, test_energy_old_file, test_energy_round_trip_and_junk, test_add_energy,
               test_record_metered, test_energy_meter, test_energy_read_model, test_energy_retention, test_default_path):
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
