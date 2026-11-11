"""ram_plan: which apps to pre-tick so Strata reaches its full-RAM mode."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ram_plan as R  # noqa: E402

passed = failed = 0
GB = 1024.0


def check(cond, name):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL:", name)


def app(pid, name, gb, cat="window", pre=None):
    return {"pid": pid, "name": name, "ram_mb": gb * GB, "category": cat,
            "preselect": (cat == "game") if pre is None else pre, "reason": cat}


REAL = [app(11, "Spotify.exe", 0.5, "background"), app(12, "chrome.exe", 3.0), app(13, "Game.exe", 6.0, "game"),
        app(14, "Photos.exe", 0.4), app(15, "PhoneExperienceHost.exe", 0.3, "background")]

p = R.plan(REAL, 60 * GB, 46.84)
check(p.enough_now and p.preticked_pids == (13,) and p.summary == "Enough RAM for full speed",
      f"enough now keeps baseline only {p}")
p = R.plan(REAL, 40.0 * GB, 46.84)            # need 49.84, short 9.84, these apps total 10.2
check(not p.enough_now and p.reachable, f"real numbers reachable {p}")
check(p.preticked_pids == (11, 12, 13, 14, 15) or set(p.preticked_pids) >= {12, 13}, f"real picks {p.preticked_pids}")
check(p.available_mb + p.freed_mb >= p.need_mb, "real plan reaches the target")
check(p.summary == "Full speed needs ~50 GB free · you have 40 GB", f"summary {p.summary!r}")
check(p.detail.startswith("Pre-ticked ") and "frees ~" in p.detail, f"detail {p.detail!r}")
check(len(p.summary) <= 60 and len(p.detail) <= 60, "lines fit the dialog")

big = [app(1, "a.exe", 1.0), app(2, "big.exe", 9.0), app(3, "c.exe", 2.0)]
p = R.plan(big, 42 * GB, 46.84)              # short 7.84 -> the 9 GB app alone
check(p.preticked_pids == (2,), f"one big app suffices {p.preticked_pids}")
p = R.plan(big, 38 * GB, 46.84)              # short 11.84 -> 9 + 2 + 1 = 12
check(p.preticked_pids == (1, 2, 3), f"needs several {p.preticked_pids}")
p = R.plan(big, 30 * GB, 46.84)              # short 19.84 > 12 total
check(not p.reachable and p.preticked_pids == () and "still leaves ~7.8 GB short" in p.detail, f"unreachable {p}")
p = R.plan(REAL, 39.3 * GB, 46.84)            # the real failing launch: 10.5 GB short, apps total 10.2
check(not p.reachable and p.preticked_pids == (13,) and "~0.3 GB short" in p.detail, f"near miss wording {p.detail!r}")

prot = [app(1, "Obsidian.exe", 20.0), app(2, "b.exe", 3.0)]
p = R.plan(prot, 45 * GB, 46.84, protected_names={"obsidian.exe"})
check(1 not in p.preticked_pids, "protected app never ticked, even if biggest")
check(not p.reachable, "protected app's RAM does not count toward reachable")

p = R.plan(REAL, 39.3 * GB, None)
check(p.preticked_pids == (13,) and p.summary == "" and p.enough_now, "unknown arena = v1.0 behaviour")
p = R.plan(REAL, 60 * GB, 46.84, baseline_preticked={11})
check(p.preticked_pids == (11,), "explicit baseline kept as given")

tie = [app(5, "win.exe", 4.1, "window"), app(6, "tray.exe", 4.2, "background"), app(7, "gpu.exe", 4.0, "gpu")]
p = R.plan(tie, 44 * GB, 46.84)              # short 5.84: any one 4 GB app is not enough, two are
check(p.preticked_pids == (6, 7), f"similar sizes prefer gpu/background over a window {p.preticked_pids}")

prune = [app(1, "x.exe", 5.0), app(2, "y.exe", 4.9), app(3, "z.exe", 0.3)]
p = R.plan(prune, 45 * GB, 46.84)            # short 4.84: x alone (5.0) suffices
check(p.preticked_pids == (1,), f"prunes unneeded additions {p.preticked_pids}")

bad = [{"pid": 9, "name": "n.exe", "ram_mb": None, "category": "window"},
       {"pid": 8, "name": "m.exe", "ram_mb": "oops", "category": None}, app(10, "ok.exe", 8.0)]
p = R.plan(bad, 44 * GB, 46.84)
check(p.preticked_pids == (10,), f"missing/garbage ram_mb counts as 0 {p.preticked_pids}")

a = R.plan(list(reversed(REAL)), 40.0 * GB, 46.84)
b = R.plan(REAL, 40.0 * GB, 46.84)
check(a == b, "deterministic regardless of input order")
check(b.enough_with(b.freed_mb) and not b.enough_with(0), "enough_with re-check")
check(R.format_gb(6963.2) == "6.8 GB" and R.format_gb(None) == "0.0 GB", "format_gb")

print(f"RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
