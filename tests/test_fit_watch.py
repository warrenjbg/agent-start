"""Self-check for fit_watch.py (the live 'running partly on the CPU' verdict). Pure: no processes, no GPU, no files.
Run: .venv\\Scripts\\python.exe tests\\test_fit_watch.py
"""
from __future__ import annotations

import ast
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fit_watch  # noqa: E402
from fit_watch import CONFIG_DEFAULTS, MAX_GAP_S, FitConfig, FitState, decide, symptoms  # noqa: E402

PASSED = 0
FAILED = 0
GB = 1024.0


def check(cond: bool, msg: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {msg}")
    else:
        FAILED += 1
        print(f"[FAIL] {msg}")


# The measured table (RTX 5070 Ti 16 GB, 27B model): (label, used GB, total GB, server CPU % of all cores, slow?)
TABLE = [
    ("ngl -1, 8K ctx (fits)", 14.6, 16.3, 6, False),
    ("ngl -1, 16K ctx (fits)", 15.8, 16.3, 6, False),
    ("ngl 65, 8K ctx (not noticeably slow)", 15.7, 16.3, 6, False),
    ("ngl 63, 32K ctx (borderline: GPU 98 %, CPU 33 %)", 16.0, 16.3, 33, True),
    ("ngl 63, 16K ctx (slow)", 15.7, 16.3, 49, True),
    ("ngl 54, 64K ctx (slow)", 15.9, 16.3, 49, True),
    ("4 parallel slots, 16K each (slow)", 15.7, 16.3, 49, True),
]


def feed(series, state=None, cfg=None):
    """series: (t, generating, used_mb, total_mb, cpu). Returns (final state, [(t, slow) at every change])."""
    st = state if state is not None else FitState()
    flips = []
    for t, gen, used, total, cpu in series:
        changed, st = decide(st, t, generating=gen, vram_used_mb=used, vram_total_mb=total, cpu_share_pct=cpu,
                             cfg=cfg)
        if changed:
            flips.append((t, st.slow))
    return st, flips


def gen_series(t0, t1, used, total, cpu, gen=True, step=1.0):
    out, t = [], float(t0)
    while t <= t1 + 1e-9:
        out.append((t, gen, used, total, cpu))
        t += step
    return out


def t_table() -> None:
    for label, used, total, cpu, slow in TABLE:
        st, flips = feed(gen_series(0, 30, used * GB, total * GB, cpu))
        if slow:
            check(flips == [(8.0, True)] and st.slow, f"{label}: trips at sustain_s = 8 s ({flips})")
        else:
            check(flips == [] and not st.slow, f"{label}: never trips in 30 s ({flips})")
    # the same rows sampled every 2 s / 0.5 s: same moment (8 s of generating with the symptoms)
    for step in (2.0, 0.5):
        _, flips = feed(gen_series(0, 30, 15.9 * GB, 16.3 * GB, 49, step=step))
        check(flips == [(8.0, True)], f"sampled every {step} s: trips at 8 s ({flips})")
    _, flips = feed(gen_series(0, 7.9, 15.9 * GB, 16.3 * GB, 49, step=0.1))
    check(flips == [], "7.9 s of symptoms: not yet")


def t_symptoms() -> None:
    c = FitConfig()
    check(symptoms(c, 90, 100, 25) is True and symptoms(c, 89.9, 100, 25) is False
          and symptoms(c, 90, 100, 24.9) is False, "thresholds are inclusive (>= 90 % VRAM and >= 25 % CPU)")
    for args in ((None, 100, 30), (95, None, 30), (95, 100, None), (95, 0, 30), (95, -1, 30), (-1, 100, 30),
                 (95, 100, -3), (float("nan"), 100, 30), (95, float("inf"), 30), (True, 100, 30), ("95", 100, 30)):
        check(symptoms(c, *args) is None, f"unknown figures {args} -> None")


def t_flapping_and_reset() -> None:
    S, C = (15.9 * GB, 16.3 * GB, 49), (14.6 * GB, 16.3 * GB, 6)
    series = [(float(t), True, *(S if t % 2 == 0 else C)) for t in range(0, 60)]
    _, flips = feed(series)
    check(flips == [], "symptoms every other second for 60 s: never trips")
    series = [(float(t), True, *(S if (t % 8) != 7 else C)) for t in range(0, 60)]
    _, flips = feed(series)
    check(flips == [], "7 s on, 1 s off, repeated: never trips (a clean generating sample resets the window)")
    series = gen_series(0, 6, *S) + gen_series(7, 7, *C) + gen_series(8, 30, *S)
    _, flips = feed(series)
    check(flips == [(16.0, True)], f"a clean sample at 7 s: the window restarts at 8 s, trips at 16 s ({flips})")


def t_idle_gaps() -> None:
    S = (15.9 * GB, 16.3 * GB, 49)
    # 4 s generating with symptoms, 15 s idle (sampled every second), then generating again
    series = gen_series(0, 4, *S) + gen_series(5, 20, *S, gen=False) + gen_series(21, 40, *S)
    _, flips = feed(series)
    check(flips == [(25.0, True)], f"idle time neither counts nor resets: 4 s + 4 s -> trips at 25 s ({flips})")
    # idle samples with harmless figures in between do not reset either
    series = gen_series(0, 4, *S) + gen_series(5, 9, 1 * GB, 16.3 * GB, 0, gen=False) + gen_series(10, 30, *S)
    _, flips = feed(series)
    check(flips == [(14.0, True)], f"idle with low figures: no reset ({flips})")
    # unknown figures while generating: like idle
    series = gen_series(0, 4, *S) + gen_series(5, 9, None, None, None) + gen_series(10, 30, *S)
    _, flips = feed(series)
    check(flips == [(14.0, True)], f"None figures while generating: neither count nor reset ({flips})")
    # a gap in the observations (no samples at all for > MAX_GAP_S) restarts the window
    series = gen_series(0, 5, *S) + gen_series(5 + MAX_GAP_S + 1, 40, *S)
    _, flips = feed(series)
    check(flips == [(19.0, True)], f"a {MAX_GAP_S + 1:.0f} s hole in the samples restarts the window ({flips})")
    series = gen_series(0, 5, *S) + gen_series(5 + MAX_GAP_S, 40, *S)
    _, flips = feed(series)
    check(flips == [(10.0, True)], f"a gap of exactly MAX_GAP_S still counts: 5 s + 5 s -> trips at 10 s ({flips})")


def t_clock() -> None:
    S = (15.9 * GB, 16.3 * GB, 49)
    series = gen_series(100, 105, *S) + gen_series(50, 80, *S)
    _, flips = feed(series)
    check(flips == [(58.0, True)], f"the clock jumps back: the window restarts at the new time ({flips})")
    series = gen_series(0, 5, *S) + gen_series(3600, 3620, *S)
    _, flips = feed(series)
    check(flips == [(3608.0, True)], f"the clock jumps forward an hour: restarts ({flips})")
    st, flips = feed(gen_series(0, 10, *S))
    changed, st2 = decide(st, float("nan"), generating=True, vram_used_mb=S[0], vram_total_mb=S[1], cpu_share_pct=49)
    check(not changed and st2 == st, "a NaN clock is ignored")
    changed, st3 = decide("junk", 1.0, generating=True, vram_used_mb=1, vram_total_mb=2, cpu_share_pct=3)  # type: ignore[arg-type]
    check(not changed and isinstance(st3, FitState), "a junk state starts fresh")


def t_clear_and_persist() -> None:
    S, C = (15.9 * GB, 16.3 * GB, 49), (14.6 * GB, 16.3 * GB, 6)
    st, flips = feed(gen_series(0, 10, *S))
    check(flips == [(8.0, True)] and st.slow, "slow after 8 s")
    st, flips = feed(gen_series(11, 611, *S, gen=False), st)
    check(flips == [] and st.slow, "the verdict persists across 10 min of idle time")
    st, flips = feed(gen_series(612, 700, None, None, None), st)
    check(flips == [] and st.slow, "...and across unknown figures while generating (never cleared by None)")
    st, flips = feed(gen_series(701, 730, *C), st)
    check(flips == [(721.0, False)] and not st.slow, f"clears after clear_s = 20 s of clean generating ({flips})")
    st, flips = feed(gen_series(0, 10, *S))
    series = gen_series(11, 25, *C) + gen_series(26, 26, *S) + gen_series(27, 60, *C)
    st, flips = feed(series, st)
    check(flips == [(47.0, False)], f"a symptomatic sample restarts the clear window ({flips})")
    st, flips = feed(gen_series(0, 10, *S))
    series = gen_series(11, 20, *C) + gen_series(21, 300, *C, gen=False) + gen_series(301, 320, *C)
    st, flips = feed(series, st)
    check(flips == [(312.0, False)], f"idle between clean stretches: 9 s + 11 s -> clears at 312 ({flips})")
    st, flips = feed(gen_series(0, 10, *S))
    st, flips = feed(gen_series(11, 15, *C) + gen_series(30, 60, *C), st)
    check(flips == [(50.0, False)], f"a gap restarts the clear window too ({flips})")
    _, flips = feed(gen_series(0, 10, None, None, None))
    check(flips == [], "None figures never set the verdict")
    st = FitState()
    for t in range(0, 40):
        _, st2 = decide(st, float(t), generating=True, vram_used_mb=15.9 * GB, vram_total_mb=16.3 * GB,
                        cpu_share_pct=49)
        if t == 0:
            check(st == FitState(), "decide never mutates the state it is given (frozen)")
        st = st2
    check(st.slow, "a long slow stretch stays slow")


def t_config() -> None:
    d = FitConfig.from_config({})
    check(d == FitConfig() and (d.vram_pct, d.cpu_pct, d.sustain_s, d.clear_s) == (90, 25, 8, 20),
          "defaults 90 % / 25 % / 8 s / 20 s")
    check(set(CONFIG_DEFAULTS) == {"fit_watch_vram_pct", "fit_watch_cpu_pct", "fit_watch_sustain_s",
                                   "fit_watch_clear_s"}, "the four config.json keys")
    check(FitConfig.from_config(None) == d and FitConfig.from_config("junk") == d  # type: ignore[arg-type]
          and FitConfig.from_config([1, 2]) == d, "no / junk config -> defaults")  # type: ignore[arg-type]
    bad = {"fit_watch_vram_pct": "95", "fit_watch_cpu_pct": True, "fit_watch_sustain_s": float("nan"),
           "fit_watch_clear_s": None}
    check(FitConfig.from_config(bad) == d, "text / bool / NaN / null values -> defaults key by key")
    c = FitConfig.from_config({"fit_watch_vram_pct": 5, "fit_watch_cpu_pct": 500, "fit_watch_sustain_s": 0,
                               "fit_watch_clear_s": 10 ** 9})
    check((c.vram_pct, c.cpu_pct, c.sustain_s, c.clear_s) == (50, 100, 2, 3600), f"clamped to the limits ({c})")
    c = FitConfig.from_config({"fit_watch_vram_pct": 95, "fit_watch_cpu_pct": 40, "fit_watch_sustain_s": 3,
                               "fit_watch_clear_s": 5})
    check((c.vram_pct, c.cpu_pct, c.sustain_s, c.clear_s) == (95, 40, 3, 5), "valid values are used")
    _, flips = feed(gen_series(0, 30, 16.0 * GB, 16.3 * GB, 33), cfg=c)
    check(flips == [], "with cpu 40 %: the borderline row (33 %) no longer trips")
    _, flips = feed(gen_series(0, 30, 15.9 * GB, 16.3 * GB, 49), cfg=c)
    check(flips == [(3.0, True)], f"with sustain 3 s: trips at 3 s ({flips})")
    _, flips = feed(gen_series(0, 30, 15.9 * GB, 16.3 * GB, 49), cfg=None)
    check(flips == [(8.0, True)], "cfg=None = defaults")


def t_module_rules() -> None:
    src = Path(fit_watch.__file__).read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    check(mods <= {"__future__", "dataclasses", "logging", "math", "typing"}, f"pure: stdlib only ({sorted(mods)})")
    check(not hasattr(fit_watch, "cpu_share_pct"), "no CPU-share helper: the caller measures")


def main() -> int:
    for t in (t_table, t_symptoms, t_flapping_and_reset, t_idle_gaps, t_clock, t_clear_and_persist, t_config,
              t_module_rules):
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            check(False, f"{t.__name__} raised {exc!r}")
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
