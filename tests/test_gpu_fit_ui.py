"""GPU-fit warning wiring in main.py: the amber line, the pre-check worker and the live probe. Window test (not CI).

Fakes only: no engine process, no port, no real llama tool.
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_ui_smoke as U  # noqa: E402  (first: it redirects the data dir)

import main  # noqa: E402
import theme as T  # noqa: E402
from engines.base import FitCheck, FitReport  # noqa: E402

PASSES = 0
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}")


def _amber(app) -> bool:
    return str(app.preview.cget("text_color")) == str(T.AMBER)


def test_lines() -> None:
    app, det, ep = U.new_app()
    try:
        eng = app.engine
        U.to_active(app, ep)
        U.pump(app, 0.3)
        check(eng.gpu_fit_serving not in U.status(app), "serving: no warning by default")
        # live verdict for another server run is ignored
        app._set_gpu_fit_live(app._metrics_epoch - 1, True)
        U.pump(app, 0.2)
        check(app._gpu_fit_live is False, "a verdict from an earlier server run is dropped")
        app._set_gpu_fit_live(app._metrics_epoch, True)
        U.pump(app, 0.2)
        check(eng.gpu_fit_serving in U.status(app) and U.detail_shown(app) and _amber(app),
              f"live verdict: amber line shown ({U.status(app)!r})")
        app._set_gpu_fit_live(app._metrics_epoch, False)
        U.pump(app, 0.2)
        check(eng.gpu_fit_serving not in U.status(app), "live verdict cleared: line gone")
        app._gpu_fit_pre = True                  # the pre-check said "does not fit" (test_sequence covers how)
        app._apply_state()
        U.pump(app, 0.2)
        check(eng.gpu_fit_serving in U.status(app) and _amber(app), "pre-check 'does not fit': line shown when serving")
        app._set_gpu_fit_live(app._metrics_epoch, None, True)       # enough clean generating
        U.pump(app, 0.2)
        check(app._gpu_fit_pre is False and eng.gpu_fit_serving not in U.status(app),
              "live check overrules the pre-check")
        # a stop and an adopted server carry nothing over
        app._gpu_fit_pre = app._gpu_fit_live = True
        app._set_state(main.IDLE)
        app._set_state(main.ACTIVE)
        check(app._gpu_fit_pre is False and app._gpu_fit_live is False, "adopted server: no verdict carried over")
    finally:
        app.destroy()


class _Eng:
    def __init__(self, chk, rep):
        self.chk, self.rep, self.parsed = chk, rep, []

    def fit_check(self, cfg):
        return self.chk

    def parse_fit(self, out, cfg):
        self.parsed.append(out)
        return self.rep


def test_worker() -> None:
    import fit_check
    posted: list = []
    ran: list = []

    def done(token, fits):                       # only its identity matters: the worker posts it
        return None

    real, real_wait = fit_check.run, main.FIT_PRECHECK_WAIT_S
    fit_check.run = lambda chk, prog: (ran.append((chk.program, prog, chk.timeout_s)), "-c 8192 -ngl 10")[1]
    try:
        post = lambda fn, *a: posted.append((fn, a))  # noqa: E731
        cfg = {"launch_cmd": ['"C:\\x\\llama-server.exe"', "-m", "a.gguf"]}
        chk = FitCheck(program="C:\\x\\llama-fit-params.exe")
        main._fit_check_worker(_Eng(chk, FitReport(False, "d")), cfg, chk, post, done, 7)
        check(ran == [("C:\\x\\llama-fit-params.exe", "C:\\x\\llama-server.exe", main.FIT_PRECHECK_WAIT_S - 1.0)]
              and posted == [(done, (7, False))],
              f"does not fit: posted once with the start token; quotes stripped; time limit capped ({ran}, {posted})")
        posted.clear()
        main._fit_check_worker(_Eng(chk, FitReport(None, "d")), cfg, chk, post, done, 7)
        check(posted == [(done, (7, None))], "cannot tell: still posted (the launch follows it), as None")
        posted.clear()
        main._fit_check_worker(_Eng(chk, FitReport(True, "d")), cfg, chk, post, done, 8)
        check(posted == [(done, (8, True))], "fits: posted")
        posted.clear()
        fit_check.run = lambda chk, prog: None
        e = _Eng(chk, FitReport(None, ""))
        main._fit_check_worker(e, cfg, chk, post, done, 7)
        check(e.parsed == [""] and posted == [(done, (7, None))], "the tool gave nothing: read as cannot tell")
        posted.clear()

        def boom(chk, prog):
            raise OSError("x")
        fit_check.run = boom
        main._fit_check_worker(_Eng(chk, FitReport(False, "")), cfg, chk, post, done, 7)
        check(posted == [(done, (7, None))], "a failing pre-check never raises; the launch still follows")
        posted.clear()
        # a tool that hangs: the launch follows after the wait, with no verdict (and a late answer is not used)
        import threading
        import time
        release = threading.Event()
        fit_check.run = lambda chk, prog: (release.wait(5), "-c 8192 -ngl 10")[1]
        main.FIT_PRECHECK_WAIT_S = 0.4
        t0 = time.monotonic()
        main._fit_check_worker(_Eng(chk, FitReport(False, "late")), cfg, chk, post, done, 9)
        dt = time.monotonic() - t0
        release.set()
        time.sleep(0.2)
        check(posted == [(done, (9, None))] and dt < 2.0, f"a hanging tool: launch after the wait, no verdict ({dt:.1f}s)")
    finally:
        fit_check.run, main.FIT_PRECHECK_WAIT_S = real, real_wait


class _SeqEngine:
    """Wraps the app's engine: adds a pre-check, to see the order pre-check -> launch."""

    def __init__(self, inner, chk):
        self._inner, self._chk = inner, chk

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def fit_check(self, cfg):
        return self._chk

    def parse_fit(self, out, cfg):
        return FitReport(False, "test")


def test_sequence() -> None:
    """The server is launched only after the pre-check answered; a cancel meanwhile launches nothing; an adopted
    server drops the answer."""
    import fit_check
    import threading
    real = fit_check.run
    gate = threading.Event()
    fit_check.run = lambda chk, prog: (gate.wait(10), "x")[1]
    app, det, ep = U.new_app()
    try:
        starts: list = []
        real_start = ep.start_endpoint
        ep.start_endpoint = lambda *a, **k: (starts.append(1), real_start(*a, **k))[1]
        app.turn_on(skip_sweep=True)
        U.pump(app, 0.4)
        check(app.state_name == main.STARTING and len(starts) == 1, "engine without a pre-check: launched at once")
        ep.status_value = U.active_status(True)
        app.on_status(ep.status())
        U.pump(app, 0.2)
        app._set_state(main.IDLE)
        U.pump(app, 0.2)
        # now with a pre-check
        starts.clear()
        app.engine = _SeqEngine(app.engine, FitCheck(program="C:\\x\\llama-fit-params.exe"))
        app.turn_on(skip_sweep=True)
        U.pump(app, 0.5)
        check(app.state_name == main.STARTING and not starts, f"pre-check running: not launched yet ({starts})")
        gate.set()
        U.pump_until(app, lambda: bool(starts), 3.0)
        check(len(starts) == 1 and app._gpu_fit_pre is True, "pre-check answered: launched once, verdict kept")
        U.pump(app, 0.6)
        check(app.engine.gpu_fit_loading in U.status(app), f"loading: the amber line is shown ({U.status(app)!r})")
        # adopted: the answer is dropped at ready
        ep.status_value = dict(U.active_status(True), adopted=True)
        app._on_ready(app._start_token)
        U.pump(app, 0.2)
        check(app._gpu_fit_pre is False, "adopted server: the pre-check's answer is dropped")
        app._set_state(main.IDLE)
        U.pump(app, 0.2)
        # cancelled while the pre-check runs: nothing is launched afterwards
        starts.clear()
        gate.clear()
        app.turn_on(skip_sweep=True)
        U.pump(app, 0.4)
        tok = app._start_token
        app.cancel_start()
        U.pump(app, 0.3)
        gate.set()
        U.pump(app, 1.0)
        check(not starts, f"cancelled during the pre-check: nothing launched ({starts}, {app.state_name})")
        app._after_fit_check(tok, False)
        check(not starts and app._gpu_fit_pre is False, "a stale answer launches nothing and sets nothing")
    finally:
        gate.set()
        fit_check.run = real
        app.destroy()


class _Vram:
    def __init__(self):
        self.used, self.total = 15800.0, 16300.0

    def snapshot(self):
        return types.SimpleNamespace(samples=((0.0, self.used),), total_mb=self.total, available=True, now=1.0)


def test_probe() -> None:
    probe = main._FitProbe()
    cpu = {"v": 49.0}
    probe._cpu_pct = lambda now, endpoint: cpu["v"]         # type: ignore[method-assign]
    vram = _Vram()
    gen = types.SimpleNamespace(live=types.SimpleNamespace(state="generating"))
    idle = types.SimpleNamespace(live=types.SimpleNamespace(state="idle"))
    cfg: dict = {}
    out = [probe.observe(1, float(t), gen, vram, None, cfg) for t in range(0, 12)]
    hits = [(i, o) for i, o in enumerate(out) if o is not None]
    check(len(hits) == 1 and hits[0][1] == (True, False) and 7 <= hits[0][0] <= 9,
          f"card full + CPU busy while generating for ~8 s: slow, once ({hits})")
    check(probe.observe(1, 12.0, idle, vram, None, cfg) is None and probe.state.slow, "idle keeps the verdict")
    cpu["v"] = 6.0
    out = [probe.observe(1, 13.0 + t, gen, vram, None, cfg) for t in range(0, 26)]
    hits = [o for o in out if o is not None]
    check(any(o[0] is False for o in hits) and any(o[1] for o in hits),
          f"20 s of clean generating: cleared, and the pre-check is overruled ({hits})")
    # short bursts never add up across idle gaps longer than the allowed gap
    p2 = main._FitProbe()
    cpu["v"] = 49.0
    p2._cpu_pct = lambda now, endpoint: cpu["v"]            # type: ignore[method-assign]
    res = []
    for burst in range(6):
        base = burst * 30.0
        res += [p2.observe(1, base + t, gen, vram, None, cfg) for t in range(0, 4)]
    check(all(r is None for r in res), "3 s bursts far apart: never slow")
    # a new server run starts clean
    check(probe.observe(2, 100.0, gen, vram, None, cfg) is None and probe.state.slow is False and probe.epoch == 2,
          "new server run: state reset")
    # no video-memory figure: never a verdict
    p3 = main._FitProbe()
    p3._cpu_pct = lambda now, endpoint: 49.0                # type: ignore[method-assign]
    none_vram = types.SimpleNamespace(snapshot=lambda: types.SimpleNamespace(samples=(), total_mb=None,
                                                                             available=False, now=1.0))
    check(all(p3.observe(1, float(t), gen, none_vram, None, cfg) is None for t in range(0, 20)),
          "no video-memory reading: no verdict")
    # a stale / unavailable video-memory reading is not used
    p5 = main._FitProbe()
    p5._cpu_pct = lambda now, endpoint: 49.0                # type: ignore[method-assign]
    old = types.SimpleNamespace(snapshot=lambda: types.SimpleNamespace(samples=((0.0, 15800.0),), total_mb=16300.0,
                                                                       available=True, now=120.0))
    off = types.SimpleNamespace(snapshot=lambda: types.SimpleNamespace(samples=((119.0, 15800.0),), total_mb=16300.0,
                                                                       available=False, now=120.0))
    check(all(p5.observe(1, float(t), gen, old, None, cfg) is None for t in range(0, 20))
          and all(p5.observe(2, float(t), gen, off, None, cfg) is None for t in range(0, 20)),
          "an old or unavailable video-memory reading: no verdict")
    # the real CPU reader only reads: pids from the endpoint, nothing else
    p4 = main._FitProbe()
    ep = types.SimpleNamespace(protected_pids=lambda: {1, 2, 3}, engine_pids=lambda: {os.getpid()})
    first = p4._cpu_pct(0.0, ep)
    second = p4._cpu_pct(1.0, ep)
    check(first is None and isinstance(second, float) and 0.0 <= second <= 100.0,
          f"CPU share: primed first, then a share of all cores ({first}, {second})")
    check(main._FitProbe()._cpu_pct(0.0, types.SimpleNamespace(protected_pids=lambda: set())) is None,
          "no engine process known: no figure")


def run() -> int:
    for t in (test_worker, test_probe, test_lines, test_sequence):
        print(f"-- {t.__name__}")
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
