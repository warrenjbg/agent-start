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
                  "decode_tokens": 100, "served_s": 0.0}, f"merge sums one row without a file: {row}")
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
                                                        "decode_tokens": 90_000, "served_s": 30000.5}
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
               test_large_file_speed, test_default_path):
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
