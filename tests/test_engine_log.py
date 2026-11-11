"""engine_log: full-RAM vs low-RAM (SSD) detection from Strata's engine log. Real-log fixtures + edge cases."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine_log as E  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "engine_logs")
LIVE = r"C:\Users\<user>\Downloads\Strata-main\Strata-0.2.10\strata-swift-iq3_s.log"
passed = failed = 0


def check(cond, name):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8", errors="replace") as fh:
        return fh.read()


# --- real blocks
full = E.parse(fx("full_ram_iq3_s.log"))
check(full.mode == "full_ram" and full.arena_gib == 46.84 and not full.slow, f"full iq3_s {full}")
check(full.available_gib == 52.0 and full.gpu_slots == 1700 and full.gpu_cache_gib == 3.27 and not full.gpu_shrunk,
      f"full iq3_s details {full}")
low = E.parse(fx("low_ram_iq3_s.log"))
check(low.mode == "low_ram" and low.slow and low.arena_gib == 46.84, f"low iq3_s {low}")
check(low.short_gib == 7.53 and low.available_gib == 39.31 and low.resident_gib == 13.67, f"low iq3_s numbers {low}")
check(low.gpu_slots == 795 and low.gpu_shrunk, f"low iq3_s gpu {low}")
check(low.started_at == time.mktime((2026, 11, 12, 6, 12, 42, 0, 0, -1)), "started_at parsed (local time)")
lx = E.parse(fx("low_ram_iq3_xxs.log"))
check(lx.mode == "low_ram" and lx.arena_gib == 39.97, f"low iq3_xxs {lx}")
fxx = E.parse(fx("full_ram_iq3_xxs.log"))
check(fxx.mode == "full_ram" and fxx.arena_gib == 39.97, f"full iq3_xxs {fxx}")

# --- partial blocks never crash; verdict only moves forward sensibly
for name, final in (("full_ram_iq3_s.log", "full_ram"), ("low_ram_iq3_s.log", "low_ram")):
    lines = fx(name).splitlines(keepends=True)
    seen, ok = [], True
    for i in range(0, len(lines) + 1, 3):
        try:
            seen.append(E.parse("".join(lines[:i])).mode)
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("raised at", i, exc)
    check(ok, f"{name}: partial parses never raise")
    check(set(seen) <= {"unknown", "waiting_for_ram", final}, f"{name}: partial modes {sorted(set(seen))}")
    check(seen[-1] == final, f"{name}: final verdict {final}")
check("waiting_for_ram" in {E.parse("".join(fx("low_ram_iq3_s.log").splitlines(True)[:i])).mode
                            for i in range(1, 30)}, "waiting_for_ram phase seen before the fallback")

# --- --mmap-experts on the command line = SSD mode by choice
mm = fx("low_ram_iq3_s.log").replace("engine started: ", "engine started: --mmap-experts ", 1)
check(E.parse(mm).mode == "mmap_by_config", "--mmap-experts on the command line -> mmap_by_config")

# --- read_run: last block only, since-filter, tail reads
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "eng.log")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(fx("full_ram_iq3_s.log") + fx("low_ram_iq3_s.log"))
    blk = E.read_run(p)
    check(E.parse(blk).mode == "low_ram" and blk.count("engine started") == 1, "read_run returns the last run only")
    t_low = time.mktime((2026, 11, 12, 6, 12, 42, 0, 0, -1))
    check(E.read_run(p, since=t_low + 3) != "", "since within 5 s accepts the run")
    check(E.read_run(p, since=t_low + 60) == "", "since after the last run -> '' (new run not written yet)")
    check(E.parse(E.read_run(p, max_bytes=3000)).mode in ("unknown", "low_ram"), "tiny tail read does not crash")
    check(E.learned_arena_gib(p) == 46.84, "learned_arena_gib from a log")
    with open(p, "ab") as fh:
        fh.write(b"\xff\xfe\x00garbage\x81\n")
    check(E.parse(E.read_run(p)).mode == "low_ram", "garbage bytes tolerated")
    cfg = os.path.join(d, "strata.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write('{"log": "%s"}' % p.replace("\\", "\\\\"))
    check(E.config_log_path(cfg) == p, "config_log_path reads the 'log' field")
    check(E.current(cfg).mode == "low_ram", "current() end to end")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    check(E.config_log_path(cfg) is None and E.current(cfg).mode == "unknown", "broken config -> unknown")
check(E.read_run(r"C:\nope\missing.log") == "" and E.learned_arena_gib(r"C:\nope\missing.log") is None, "missing file")
check(E.parse("").mode == "unknown", "empty block")

# --- the live log, if present
if os.path.exists(LIVE):
    check(E.learned_arena_gib(LIVE) == 46.84, "live log: arena 46.84 GiB")
    check(E.parse(E.read_run(LIVE)).mode in E.MODES, "live log: last run parses")

print(f"RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
