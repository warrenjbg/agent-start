"""Headless tests for metrics_worker.py: the hc-metrics thread's body (MetricsWorker.loop / poller / run_log_path /
flush_served), wired to a stand-in for the app the way main.py wires it (MetricsHost of lambdas over the stand-in's
attributes, so a value swapped between two turns is the one the next turn reads).
No window, no Tk root, no thread, no network, no engine and NO process: subprocess.run and subprocess.Popen are
replaced by a guard before any app module is imported. The loop runs on THIS thread for a set number of turns: a
stand-in for the app's stop event sets itself after the last wait. The clock is a stand-in too (metrics_worker.time),
so every gap is exact. The data folder is a temp folder.

Run:  .venv\\Scripts\\python.exe tests\\test_metrics_worker.py
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import tempfile
import types
from pathlib import Path

os.environ["AGENT_START_HOME"] = tempfile.mkdtemp(prefix="agentstart-metrics-test-")   # before any app module
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Python before 3.12 runs `ver` once to answer platform.uname() and keeps the answer; a library asks at import.
# Ask now, so the guard below only ever sees what the app itself would start.
platform.uname()

GUARD_HITS: list[str] = []


def _forbidden(*args, **kwargs):
    GUARD_HITS.append(repr(args[:1])[:80])
    raise AssertionError("test_metrics_worker must never start a process")


subprocess.run = _forbidden  # type: ignore[assignment]
subprocess.Popen = _forbidden  # type: ignore[assignment, misc]

import metrics_worker as MW  # noqa: E402
import main as M  # noqa: E402
from engines.base import (  # noqa: E402
    CAP_LIVE_ACTIVITY, CAP_REQUEST_END, CAP_REQUEST_HISTORY, LiveSample, MetricsSample)

PASSED = FAILED = 0
URL = "http://127.0.0.1:50999/v1"               # never contacted: the poller is a stand-in
ROOT_URL = "http://127.0.0.1:50999"
REAL_TIME = MW.time
REAL_PROBE = MW._FitProbe


def check(cond: bool, name: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


class Logs(logging.Handler):
    """Collects the app logger's messages (the loop's two debug lines are what a user's log shows)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.msgs: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.msgs.append(record.getMessage())


LOGS = Logs()
_log = logging.getLogger("agent_start")
_log.addHandler(LOGS)
_log.setLevel(logging.DEBUG)


class Clock:
    """Stands in for the time module inside metrics_worker: monotonic() and time() move only when told."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return 1.7e9 + self.t


class StopEvt:
    """Stands in for the app's threading.Event. The loop gets `turns` turns: wait() sets the flag after the last.
    hook(turn) runs inside each wait, i.e. between two turns (where a Tk-thread change would land)."""

    def __init__(self, turns: int = 1, hook=None, preset: bool = False) -> None:
        self.turns, self.hook, self.flag = turns, hook, preset
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.flag

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if self.hook is not None:
            self.hook(len(self.waits))
        if len(self.waits) >= self.turns:
            self.flag = True
        return self.flag


class Poller:
    def __init__(self, samples=None) -> None:
        self.samples = list(samples or [])
        self.resets: list = []
        self.polls: list = []

    def reset(self, log_path=None) -> None:
        self.resets.append(log_path)

    def poll(self, root, catch_up):
        self.polls.append((root, catch_up))
        return self.samples.pop(0) if self.samples else MetricsSample(model="m-polled")


class Engine:
    def __init__(self, caps=(CAP_LIVE_ACTIVITY,), watch_gpu_fit=False, poller=None, raises=False) -> None:
        self.caps, self.watch_gpu_fit, self.raises = set(caps), watch_gpu_fit, raises
        self.poller = poller if poller is not None else Poller()
        self.made: list = []
        self.asked = 0

    def can(self, cap) -> bool:
        self.asked += 1
        if self.raises:
            raise RuntimeError("engine broke")
        return cap in self.caps

    def metrics_poller(self, cfg, **seams):
        self.made.append((cfg, seams))
        return self.poller


class Usage:
    def __init__(self, fresh: int = 0, dirty: bool = False) -> None:
        self.fresh, self.dirty = fresh, dirty
        self.records: list = []
        self.served: list = []
        self.energy: list = []
        self.flushes = 0

    def record(self, sample, now, metered):
        self.records.append((sample, metered))
        return self.fresh

    def add_served(self, model, seconds, now) -> None:
        self.served.append((model, seconds))

    def add_energy(self, model, wh, busy_wh, now) -> None:
        self.energy.append((model, wh, busy_wh))

    def flush(self) -> None:
        self.flushes += 1


class Meter:
    def __init__(self, take=(0.0, 0.0), raises=False) -> None:
        self.metering, self.raises, self._take = True, raises, take
        self.steps: list = []
        self.pauses = 0

    def step(self, joules, now, serving, busy) -> None:
        if self.raises:
            raise RuntimeError("meter broke")
        self.steps.append((joules, serving, busy))

    def pause(self) -> None:
        self.pauses += 1

    def take(self):
        t, self._take = self._take, (0.0, 0.0)
        return t


class Endpoint:
    def __init__(self, status=None, raises=False, http=None) -> None:
        self._status = status if status is not None else {"adopted": False, "log_path": r"C:\logs\run.log"}
        self.raises = raises
        if http is not None:
            self.engine_http = http
        self.engine_pids = lambda: set()

    def status(self):
        if self.raises:
            raise RuntimeError("status broke")
        return dict(self._status)


class Probe:
    """Stands in for _FitProbe (the loop makes one per run): records observe() and answers `verdict`."""
    verdict: object = None
    made = 0
    seen: list = []

    def __init__(self) -> None:
        Probe.made += 1

    def observe(self, epoch, now, sample, vram, endpoint, cfg):
        Probe.seen.append((epoch, sample, vram, endpoint, cfg))
        return Probe.verdict


def _app(**over):
    """A stand-in for the app with only what the worker reads, wired to a real MetricsWorker the way main.py wires
    it: every host callable looks the attribute up when it is called. Serving a loaded model by default."""
    a = types.SimpleNamespace(
        state_name=MW.ACTIVE, _metrics_epoch=1, cfg={"endpoint_url": URL}, engine=Engine(), endpoint=Endpoint(),
        vram=types.SimpleNamespace(), usage=Usage(), _energy=None, loaded=True, model_id="Qwen-App",
        _fetch_metrics=None, _fetch_status=None, _stop_evt=StopEvt(), posted=[])
    for k, v in over.items():
        setattr(a, k, v)
    a.post = lambda fn, *args: a.posted.append((fn, args))
    a._apply_metrics = "APPLY"
    a._set_gpu_fit_live = "FIT"
    a.w = MW.MetricsWorker(MW.MetricsHost(
        stop_evt=lambda: a._stop_evt, state=lambda: a.state_name,
        metrics_epoch=lambda: a._metrics_epoch, cfg=lambda: a.cfg, engine=lambda: a.engine,
        endpoint=lambda: a.endpoint, vram=lambda: a.vram, usage=lambda: a.usage,
        energy=lambda: a._energy, loaded=lambda: a.loaded, model_id=lambda: a.model_id,
        fetch_metrics=lambda: a._fetch_metrics, fetch_status=lambda: a._fetch_status,
        apply_metrics=lambda epoch, sample: a.post(a._apply_metrics, epoch, sample),
        set_gpu_fit_live=lambda epoch, *verdict: a.post(a._set_gpu_fit_live, epoch, *verdict),
        stats_ms=lambda: M.STATS_MS))
    return a


def _run(a, turns: int = 1, hook=None, clock: Clock | None = None, step: float = 1.0) -> Clock:
    """Run the loop for `turns` turns on this thread. The clock moves `step` seconds per wait, then hook runs."""
    clk = clock or Clock()

    def tick(n: int) -> None:
        clk.t += step
        if hook is not None:
            hook(n)

    a._stop_evt = StopEvt(turns, tick)
    MW.time = clk  # type: ignore[assignment]
    MW._FitProbe = Probe  # type: ignore[misc]
    try:
        a.w.loop()
    finally:
        MW.time = REAL_TIME
        MW._FitProbe = REAL_PROBE  # type: ignore[misc]
    return clk


def _applied(a) -> list:
    return [args for fn, args in a.posted if fn == "APPLY"]


def _fits(a) -> list:
    return [args for fn, args in a.posted if fn == "FIT"]


def t_stop_event() -> None:
    a = _app()
    a._stop_evt = StopEvt(preset=True)
    a.w.loop()
    check(not a.posted and not a._stop_evt.waits and not a.engine.made, "stop event already set: not one turn")
    a = _app()
    _run(a, turns=3)
    check(a._stop_evt.waits == [MW.LIVE_POLL_S] * 3 and len(_applied(a)) == 3,
          f"one poll per turn, then a wait of LIVE_POLL_S on the app's stop event ({a._stop_evt.waits})")


def t_idle_does_nothing() -> None:
    for state in (M.IDLE, M.STARTING, M.ERROR):
        a = _app(state_name=state, _energy=Meter())
        _run(a)
        check(not a.posted and not a.engine.made and not a.engine.asked and not a.usage.records,
              f"{state}: no poll, nothing posted, the engine is not asked")
        check(a._energy.pauses == 1 and not a._energy.steps, f"{state}: the energy meter is paused, not stepped")
        check(a.usage.flushes == 0 and not a.usage.served, f"{state}: nothing to save: no flush")
    a = _app(state_name=M.STOPPING)
    _run(a)
    check(len(_applied(a)) == 1, "STOPPING: still polled (a pending stop needs the request phases)")


def t_idle_retry() -> None:
    a = _app(state_name=M.IDLE, usage=Usage(dirty=True))
    _run(a, turns=3, step=10.0)
    check(a.usage.flushes == 1, f"idle, a save is due: saved once, not again within USAGE_RETRY_S ({a.usage.flushes})")
    a = _app(state_name=M.IDLE, usage=Usage(dirty=True))
    _run(a, turns=3, step=MW.USAGE_RETRY_S)
    check(a.usage.flushes == 3, f"...and retried every USAGE_RETRY_S ({a.usage.flushes})")
    a = _app(state_name=M.IDLE, usage=Usage(dirty=False))
    a.w.served_acc = 7.0
    _run(a)
    check(a.usage.flushes == 1 and a.usage.served == [("Qwen-App", 7.0)] and a.w.served_acc == 0.0,
          f"idle with unsaved serving time: added and saved ({a.usage.served})")
    a = _app(state_name=M.IDLE, usage=None, _energy=Meter())
    _run(a)
    check(not a.posted and a._energy.pauses == 1, "idle without a usage history: nothing else happens")


def t_one_poll() -> None:
    s = MetricsSample(model="Qwen-Served")
    a = _app(engine=Engine(poller=Poller([s])))
    _run(a)
    p = a.engine.poller
    check(len(a.engine.made) == 1 and a.engine.made[0][0] is a.cfg and a.engine.made[0][1] == {},
          f"the engine's poller, made with the app's config and no seams ({a.engine.made})")
    check(p.resets == [r"C:\logs\run.log"], f"first poll of a run: reset with the run's log ({p.resets})")
    check(p.polls == [(ROOT_URL, True)], f"polled at the server root, as a catch-up ({p.polls})")
    check(_applied(a) == [(1, s)], f"the sample is handed to the app's _apply_metrics with the epoch ({a.posted})")
    check(a.usage.records == [(s, False)] and a.w.served_model == "Qwen-Served",
          "recorded in the usage history; the served model is the sample's")


def t_turns_and_epochs() -> None:
    a = _app()
    _run(a, turns=3)
    p = a.engine.poller
    check(len(a.engine.made) == 1 and len(p.resets) == 1, "same run: one poller, one reset")
    check([c for _, c in p.polls] == [True, False, False], f"polls 1 s apart: only the first is a catch-up ({p.polls})")
    a = _app()
    _run(a, turns=2, step=3 * M.STATS_MS / 1000.0 + 0.5)
    check([c for _, c in a.engine.poller.polls] == [True, True], "a gap of more than 3 stats periods: catch up again")

    def new_run(n: int) -> None:
        if n == 1:
            a._metrics_epoch = 2
    a = _app()
    _run(a, turns=2, hook=new_run)
    p = a.engine.poller
    check(len(p.resets) == 2 and [c for _, c in p.polls] == [True, True] and [e for e, _ in _applied(a)] == [1, 2],
          f"a new server run (epoch read every turn): reset and catch up, posted under the new epoch ({p.resets})")
    a = _app()
    a.w.served_epoch, a.w.served_acc, a.w.served_model = 0, 12.0, "Old-Model"
    a.engine.poller = Poller([MetricsSample(model=None)])
    _run(a)
    check(a.usage.served == [("Old-Model", 12.0)] and a.w.served_epoch == 1 and a.w.served_model is None,
          f"another run: the last run's serving time is booked under its model, then forgotten ({a.usage.served})")


def t_values_read_per_turn() -> None:
    e2 = Engine(caps=())

    def swap(n: int) -> None:
        if n == 1:
            a.engine = e2
    a = _app()
    _run(a, turns=2, hook=swap)
    check(e2.asked > 0 and isinstance(_applied(a)[1][1], MetricsSample) and not e2.made,
          "the engine is read every turn (a swapped engine is asked; the poller is kept for the run)")

    def stop_serving(n: int) -> None:
        if n == 1:
            a.state_name = M.IDLE
    a = _app()
    _run(a, turns=2, hook=stop_serving)
    check(len(_applied(a)) == 1, "the state is read every turn: no poll after the engine stopped")


def t_no_reports() -> None:
    a = _app(engine=Engine(caps=()))
    _run(a)
    got = _applied(a)
    check(not a.engine.poller.polls, "an engine that reports nothing is not polled")
    check(len(got) == 1 and got[0][1] == MetricsSample() and got[0][1] is not None,
          "...and the app gets an empty sample (nothing reported), not None (a failed poll)")
    for cap in (CAP_LIVE_ACTIVITY, CAP_REQUEST_HISTORY, CAP_REQUEST_END):
        a = _app(engine=Engine(caps=(cap,)))
        _run(a)
        check(len(a.engine.poller.polls) == 1, f"one of the three capabilities is enough to poll ({cap})")


def t_no_root() -> None:
    a = _app(cfg={"endpoint_url": ""}, engine=Engine(watch_gpu_fit=True))
    Probe.seen = []
    _run(a)
    check(not a.engine.poller.polls and _applied(a) == [(1, None)],
          "no server address: not polled, a failed poll (None) is handed on")
    check(not a.usage.records and not Probe.seen, "...and nothing is recorded or checked for GPU fit")


def t_gpu_fit() -> None:
    s = MetricsSample(model="m", live=LiveSample(state="generating"))
    Probe.verdict, Probe.seen = (True, False), []
    a = _app(engine=Engine(poller=Poller([s])))
    _run(a)
    check(not Probe.seen and not _fits(a), "an engine without watch_gpu_fit: no fit check")
    a = _app(engine=Engine(watch_gpu_fit=True, poller=Poller([s])))
    Probe.made = 0
    _run(a, turns=2)
    check(Probe.made == 1, "one probe per worker run, kept across polls")
    check(len(Probe.seen) == 2 and Probe.seen[0][:2] == (1, s) and Probe.seen[0][2] is a.vram
          and Probe.seen[0][3] is a.endpoint and Probe.seen[0][4] is a.cfg,
          "watch_gpu_fit: each sample goes to the probe with the app's VRAM sampler, endpoint and config")
    check(_fits(a) == [(1, True, False)] * 2, f"a verdict goes to the app's _set_gpu_fit_live ({a.posted})")
    Probe.verdict, Probe.seen = None, []
    a = _app(engine=Engine(watch_gpu_fit=True))
    _run(a)
    check(len(Probe.seen) == 1 and not _fits(a) and len(_applied(a)) == 1, "no verdict: nothing posted, the poll goes on")


def t_usage_none() -> None:
    a = _app(usage=None, _energy=Meter())
    _run(a, turns=2)
    check(len(_applied(a)) == 2 and a.w.served_acc == 0.0 and not a._energy.steps,
          "no usage history: polls go on, no serving time, no energy step")


def t_served_time() -> None:
    a = _app()
    _run(a, turns=3, step=2.0)
    check(a.w.served_acc == 4.0, f"serving: the time between polls is counted from the second poll ({a.w.served_acc})")
    a = _app()
    _run(a, turns=2, step=3600.0)
    check(a.w.served_acc == 5.0, f"a long gap (sleep) counts 5 s at most ({a.w.served_acc})")
    for name, over in (("loading", {"loaded": False}), ("stopping", {"state_name": M.STOPPING})):
        a = _app(**over)
        _run(a, turns=3, step=2.0)
        check(a.w.served_acc == 0.0, f"{name}: no serving time")
    a = _app(loaded=None)
    _run(a, turns=2, step=2.0)
    check(a.w.served_acc == 2.0, "loaded unknown (adopted server): counted as serving")

    def pause_serving(n: int) -> None:
        a.loaded = False if n == 1 else True
    a = _app()
    _run(a, turns=4, hook=pause_serving, step=2.0)
    check(a.w.served_acc == 2.0, f"a pause restarts the count instead of bridging it ({a.w.served_acc})")


def t_minute_flush() -> None:
    a = _app()
    a.w.served_epoch, a.w.served_acc = 1, 55.0
    _run(a, turns=2, step=2.0)
    check(a.usage.flushes == 0 and a.w.served_acc == 57.0, "under USAGE_FLUSH_S: nothing saved")
    a = _app()
    a.w.served_epoch, a.w.served_acc = 1, 59.0
    _run(a, turns=2, step=2.0)
    check(a.usage.flushes == 1 and a.usage.served == [("m-polled", 61.0)] and a.w.served_acc == 0.0,
          f"a minute of serving: booked under the served model and saved ({a.usage.served})")


def t_energy() -> None:
    a = _app(_energy=Meter())
    a.vram.energy_j = lambda: 123.0
    a.engine.poller = Poller([MetricsSample(live=LiveSample(state="reading"))])
    _run(a)
    check(a._energy.steps == [(123.0, True, True)],
          f"serving and reading: the meter steps with the sampler's energy, busy ({a._energy.steps})")
    a = _app(_energy=Meter(), usage=Usage(fresh=2))
    _run(a)
    check(a._energy.steps == [(None, True, True)],
          f"a sampler without energy_j: stepped with None; a fresh request counts as busy ({a._energy.steps})")
    check(a.usage.records[0][1] is True, "requests seen while metering are marked metered")
    a = _app(_energy=Meter(), loaded=False)
    _run(a)
    check(a._energy.steps == [(None, False, False)],
          f"loading, nothing running: stepped as not serving, not busy ({a._energy.steps})")
    LOGS.msgs.clear()
    a = _app(_energy=Meter(raises=True))
    _run(a, turns=2)
    check(len(_applied(a)) == 2 and a._energy.pauses == 2, "a failing energy step pauses the meter; the poll goes on")
    check(LOGS.msgs.count("GPU energy step failed") == 2 and "metrics poll failed" not in LOGS.msgs,
          "...logged as an energy failure, not a poll failure")


def t_raising_line() -> None:
    LOGS.msgs.clear()
    good = Engine()

    def repair(n: int) -> None:
        if n == 1:
            a.engine = good
    a = _app(engine=Engine(raises=True))
    _run(a, turns=2, hook=repair)
    check(a._stop_evt.waits == [MW.LIVE_POLL_S] * 2 and len(_applied(a)) == 1,
          "a raising line skips that poll only: the loop waits and polls again")
    check(LOGS.msgs.count("metrics poll failed") == 1, f"...logged as 'metrics poll failed' ({LOGS.msgs})")
    LOGS.msgs.clear()
    a = _app(endpoint=Endpoint(raises=True))
    _run(a)
    check(len(_applied(a)) == 1 and a.engine.poller.resets == [None] and not LOGS.msgs,
          "an endpoint whose status fails: no log path, the poll goes on")


def t_flush_served() -> None:
    a = _app()
    u = Usage()
    a.w.flush_served(u)
    check(not u.served and not u.energy, "nothing collected, no meter: nothing added")
    a.w.served_acc = 30.0
    a.w.flush_served(u)
    check(u.served == [("Qwen-App", 30.0)] and a.w.served_acc == 0.0, "no served model: booked under the app's model")
    a.w.served_acc, a.w.served_model = 5.0, "Qwen-Seen"
    a._energy = Meter(take=(0.0, 0.0))
    a.w.flush_served(u)
    check(u.served[-1] == ("Qwen-Seen", 5.0) and not u.energy, "the served model wins; no energy: none added")
    a._energy = Meter(take=(2.5, 1.0))
    a.w.flush_served(u)
    check(u.energy == [("Qwen-Seen", 2.5, 1.0)] and len(u.served) == 2, "energy taken from the meter and booked")


def t_poller_seams() -> None:
    a = _app()
    a.w.poller()
    check(a.engine.made[-1][1] == {}, "no test seams, no engine_http: the engine's own fetchers")
    calls: list = []
    a._fetch_metrics = lambda r, **kw: calls.append(("old", r))
    a.w.poller()
    seams = a.engine.made[-1][1]
    check(set(seams) == {"fetch_metrics", "fetch_status"}, f"a metrics seam brings both seams ({sorted(seams)})")
    a._fetch_metrics = lambda r, **kw: calls.append(("new", r, kw))
    seams["fetch_metrics"]("R", all_requests=True)
    check(calls == [("new", "R", {"all_requests": True})], "the metrics seam is looked up when it is called")
    check(seams["fetch_status"]("R") is None, "no status seam: no status lookups")
    a._fetch_status = lambda r: ("status", r)
    check(seams["fetch_status"]("R") == ("status", "R"), "a status seam set later is used")
    http = object()
    a = _app(endpoint=Endpoint(http=http))
    a.w.poller()
    check(a.engine.made[-1][1] == {"http": http}, "the endpoint's engine_http is handed to the engine")


def t_run_log_path() -> None:
    cases = ((Endpoint({"adopted": False, "log_path": r"C:\l.log"}), r"C:\l.log"),
             (Endpoint({"adopted": True, "log_path": r"C:\l.log"}), None),
             (Endpoint({"adopted": False, "log_path": ""}), None),
             (Endpoint(raises=True), None))
    got = [_app(endpoint=ep).w.run_log_path() for ep, _ in cases]
    check(got == [want for _, want in cases], f"the run's log; none for an adopted server or on an error ({got})")


def t_main_names() -> None:
    check(M.LIVE_POLL_S is MW.LIVE_POLL_S and M.USAGE_FLUSH_S is MW.USAGE_FLUSH_S
          and M.USAGE_RETRY_S is MW.USAGE_RETRY_S and M._FitProbe is MW._FitProbe,
          "main still offers the periods and the probe under their old names")
    check((MW.LIVE_POLL_S, MW.USAGE_FLUSH_S, MW.USAGE_RETRY_S) == (1.0, 60.0, 30.0), "the three periods")
    check((MW.ACTIVE, MW.STOPPING) == (M.ACTIVE, M.STOPPING), "the worker's state names are the app's")
    check(not hasattr(M.AgentStartApp, "_metrics_loop") and not hasattr(M.AgentStartApp, "_flush_served"),
          "the app keeps no second copy of the loop")


def main() -> int:
    for t in (t_stop_event, t_idle_does_nothing, t_idle_retry, t_one_poll, t_turns_and_epochs, t_values_read_per_turn,
              t_no_reports, t_no_root, t_gpu_fit, t_usage_none, t_served_time, t_minute_flush, t_energy,
              t_raising_line, t_flush_served, t_poller_seams, t_run_log_path, t_main_names):
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"{t.__name__} raised {exc!r}")
        finally:
            MW.time = REAL_TIME
            MW._FitProbe = REAL_PROBE  # type: ignore[misc]
    check(not GUARD_HITS, f"no process was ever started ({len(GUARD_HITS)} attempts blocked)")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
