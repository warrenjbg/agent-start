"""Self-check for detector.py. Run: .venv\\Scripts\\python.exe tests\\test_detector.py

Safe by construction: real-system checks are read-only (scan + dry_run). The only real kill targets a
throwaway child spawned here, delivered through a _scan_apps() override. config.json is never written
(the whitelist round-trip uses a temp copy via config_store.CONFIG_PATH monkeypatch).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

import config_store  # noqa: E402
import detector  # noqa: E402
from detector import (App, GraphContext, ProcGraph, ProcInfo, ProcessDetector,  # noqa: E402
                      build_apps, norm)

RESULTS: list[tuple[str, bool, str]] = []
MB = 1024 * 1024
CREATE_NO_WINDOW = 0x08000000
SWEEP_KEYS = {"closed_apps", "ram_freed_mb", "vram_freed_mb", "failed", "dry_run", "candidates",
              "preselected", "preselected_ram_mb", "preselected_vram_mb", "names", "preselected_names",
              "launch_records"}


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------------------------- 1. real scan

def print_table(apps: list[dict]) -> None:
    hdr = f"{'name':<40} {'pid':>6} {'category':<10} {'ram_mb':>8} {'vram_mb':>8} {'pre':<5} {'mem':>3}  reason"
    print(hdr)
    print("-" * len(hdr))
    for a in apps:
        v = "None" if a["vram_mb"] is None else f"{a['vram_mb']:.1f}"
        print(f"{a['name'][:40]:<40} {a['pid']:>6} {a['category']:<10} {a['ram_mb']:>8.1f} {v:>8} "
              f"{str(a['preselect']):<5} {a['member_count']:>3}  {a['reason']}")


def test_real_scan() -> None:
    section("1. real-system scan (read-only) + dry-run sweep")
    cfg = config_store.load_config()
    d = ProcessDetector(cfg)
    try:
        t = time.perf_counter()
        first = d.scan_killable_processes()
        cold = time.perf_counter() - t
        times = []
        for _ in range(3):
            t = time.perf_counter()
            pub = d.scan_killable_processes()
            times.append(time.perf_counter() - t)
        warm = min(times)
        print(f"  scan time: cold(first) {cold:.3f}s, warm {', '.join(f'{x:.3f}' for x in times)}s")
        check("warm scan < 1.5 s", warm < 1.5, f"{warm:.3f}s")
        if cold >= 1.5:
            print(f"  NOTE: first (cold) scan {cold:.3f}s exceeds 1.5s target (psutil first-pass cost)")
        print()
        print_table(pub)
        print()

        check("scan returns list", isinstance(pub, list) and isinstance(first, list))
        for a in pub:
            if set(a) != set(detector.PUBLIC_KEYS):
                check("candidate keys exactly match contract", False, f"{sorted(a)}")
                break
        else:
            check("candidate keys exactly match contract", True)
        types_ok = all(isinstance(a["pid"], int) and isinstance(a["name"], str) and isinstance(a["exe"], str)
                       and isinstance(a["create_time"], float) and isinstance(a["ram_mb"], float)
                       and (a["vram_mb"] is None or isinstance(a["vram_mb"], float))
                       and isinstance(a["reason"], str) and a["category"] in detector.CATEGORY_PRIORITY
                       and isinstance(a["preselect"], bool) and isinstance(a["member_count"], int)
                       and a["preselect"] == (a["category"] == "game") and a["ram_mb"] == round(a["ram_mb"], 1)
                       for a in pub)
        check("candidate value types/preselect rule", types_ok)
        check("sorted by ram_mb desc", all(pub[i]["ram_mb"] >= pub[i + 1]["ram_mb"] for i in range(len(pub) - 1)))

        # internal apps (same scan path) for member-level protection asserts
        with d._lock:
            apps = d._scan_apps()
        procs = detector.collect_processes()
        graph = ProcGraph(procs)
        me = os.getpid()
        ancestors = set(graph.ancestors(me))
        print(f"  self pid {me}; validated ancestors: "
              + ", ".join(f"{procs[a].name}({a})" for a in sorted(ancestors, key=lambda x: graph.depth(x))))
        strata = {p for p, i in procs.items() if detector.is_strata_server_cmdline(i.cmdline)}
        strata_desc = set(strata)
        for s in strata:
            strata_desc |= graph.descendants(s)
        print(f"  Strata server processes running: {sorted(strata)} (+desc {sorted(strata_desc - strata)})")
        wl = {norm(n) for n in cfg.get("user_whitelist", [])}
        sysroot = os.environ.get("SystemRoot", "")
        bad: list[str] = []
        seen_members: set[int] = set()
        for app in apps:
            pids = set(app.member_pids) | {app.pid}
            if pids & seen_members:
                bad.append(f"{app.name}: overlaps another app")
            seen_members |= pids
            for p in pids:
                info = procs.get(p)
                nm = norm(info.name) if info else "?"
                if p == me or p in ancestors:
                    bad.append(f"{app.name}: contains self/ancestor {p}")
                if p in strata_desc:
                    bad.append(f"{app.name}: contains Strata process {p}")
                if info is None:
                    continue
                if nm in detector.TREE_PROTECTED or nm in wl:
                    bad.append(f"{app.name}: contains protected name {info.name} ({p})")
                if nm in detector.SYSTEM_PROTECTED:
                    bad.append(f"{app.name}: contains system name {info.name} ({p})")
                if detector.under_dir(info.exe, sysroot):
                    bad.append(f"{app.name}: contains SystemRoot exe {info.exe} ({p})")
                if detector.under_dir(info.exe, cfg.get("strata_dir")):
                    bad.append(f"{app.name}: contains exe under strata_dir ({p})")
                # Rule A: no member may descend from a TREE_PROTECTED/whitelisted process
                for anc in graph.ancestors(p):
                    an = procs[anc].nname
                    if an in detector.TREE_PROTECTED or an in wl:
                        bad.append(f"{app.name}: member {info.name}({p}) descends from {procs[anc].name}")
                        break
        check("no app tree contains self/ancestor/TREE_PROTECTED/whitelisted/Strata/SystemRoot/system",
              not bad, "; ".join(bad[:8]))
        check("internal apps match public scan (roots)",
              {a.pid for a in apps} == {a["pid"] for a in pub} or abs(len(apps) - len(pub)) <= 1,
              f"{len(apps)} vs {len(pub)}")

        dr = d.sweep_processes(dry_run=True)
        print(f"  dry-run (targets=None): {json.dumps(dr, indent=None)}")
        check("dry-run keys exactly match contract", set(dr) == SWEEP_KEYS, f"{sorted(dr)}")
        check("dry-run flag + nothing failed", dr["dry_run"] is True and dr["failed"] == [])
        check("dry-run candidates ~ scan", abs(dr["candidates"] - len(pub)) <= 1,
              f"{dr['candidates']} vs {len(pub)}")
        check("dry-run closed_apps == names (all)", dr["closed_apps"] == dr["names"])
        check("dry-run preselected count", dr["preselected"] == sum(1 for a in pub if a["preselect"])
              or abs(dr["candidates"] - len(pub)) > 0)
        if pub:
            tgt = [(pub[0]["pid"], pub[0]["create_time"])]
            dr1 = d.sweep_processes(dry_run=True, targets=tgt)
            print(f"  dry-run (targets=[{pub[0]['name']}]): names={dr1['names']} ram={dr1['ram_freed_mb']}")
            check("dry-run single target selects exactly it", dr1["names"] == [pub[0]["name"]], str(dr1["names"]))
            dr2 = d.sweep_processes(dry_run=True, targets=[(pub[0]["pid"], pub[0]["create_time"] + 1.0)])
            check("dry-run stale create_time selects nothing", dr2["names"] == [] and dr2["ram_freed_mb"] == 0.0)
            # provider-protected pid disappears from candidates (read-only)
            prot = ProcessDetector(cfg, protected_pids_provider=lambda: {pub[0]["pid"]})
            try:
                pids = {a["pid"] for a in prot.scan_killable_processes()}
                check("protected_pids_provider pid excluded", pub[0]["pid"] not in pids)
            finally:
                prot.close()
        raising = ProcessDetector(cfg, protected_pids_provider=lambda: 1 / 0)
        try:
            check("provider exception swallowed", isinstance(raising.scan_killable_processes(), list))
        finally:
            raising.close()
    finally:
        d.close()
        d.close()
        check("close() idempotent", True)


# ----------------------------------------------------------------------------------------- 2. synthetic

def P(pid, name, ppid, ct, rss_mb=10, exe=None, cmd=None) -> ProcInfo:
    if exe is None:
        exe = f"C:\\Apps\\{name}"
    return ProcInfo(pid=pid, name=name, exe=exe or None, ppid=ppid, create_time=float(ct),
                    rss=int(rss_mb * MB), cmdline=cmd)


def ctx(**kw) -> GraphContext:
    base = dict(self_pid=None, strata_dir="C:\\Strata", system_root="C:\\Windows",
                game_dirs=["steamapps", "Epic Games"], min_ram_mb=250, min_vram_mb=200,
                windows={}, vram_mb={}, vram_cap_mb=16000.0)
    base.update(kw)
    return GraphContext(**base)


def by_pid(apps: list[App]) -> dict[int, App]:
    return {a.pid: a for a in apps}


def test_synthetic() -> None:
    section("2. synthetic graph: PID reuse, family roots, disjoint trees, protection rules")
    # (a) PID reuse: child older than its "parent" (dead parent's pid reused by hermes) => edge invalid
    procs = {1: P(1, "explorer.exe", 0, 1, exe="C:\\Windows\\explorer.exe"),
             10: P(10, "hermes.exe", 1, 100),
             20: P(20, "bigapp.exe", 10, 50, rss_mb=2000)}
    apps = by_pid(build_apps(procs, ctx()))
    check("PID reuse: older child not protected by reused-pid parent", 20 in apps and apps[20].category == "background")
    procs[20].create_time = 150.0
    apps = by_pid(build_apps(procs, ctx()))
    check("valid edge: child of hermes protected (Rule A)", 20 not in apps)
    g = ProcGraph({1: P(1, "a.exe", 2, 10), 2: P(2, "b.exe", 1, 10)})   # equal-ctime cycle must not hang
    check("cycle-safe ancestor/descendant walks", len(g.ancestors(1)) <= 1 and len(g.descendants(1)) <= 2)
    g2 = ProcGraph({5: P(5, "p.exe", 4, 100), 4: P(4, "q.exe", 1, 200)})
    check("ancestor walk honours create_time", g2.ancestors(5) == [])

    # (b) Steam -> steamwebhelper chain + game (nested app carved out deepest-first)
    procs = {
        1: P(1, "explorer.exe", 0, 1, exe="C:\\Windows\\explorer.exe"),
        100: P(100, "steam.exe", 1, 10, rss_mb=1200, exe="C:\\Steam\\steam.exe"),
        101: P(101, "steamwebhelper.exe", 100, 11, rss_mb=100, exe="C:\\Steam\\bin\\steamwebhelper.exe"),
        102: P(102, "steamwebhelper.exe", 101, 12, rss_mb=200, exe="C:\\Steam\\bin\\steamwebhelper.exe"),
        103: P(103, "steamwebhelper.exe", 101, 13, rss_mb=100, exe="C:\\Steam\\bin\\steamwebhelper.exe"),
        200: P(200, "game.exe", 100, 20, rss_mb=3000, exe="C:\\Steam\\steamapps\\common\\G\\game.exe"),
        201: P(201, "crashhandler.exe", 200, 21, rss_mb=5, exe="C:\\Steam\\steamapps\\common\\G\\ch.exe"),
        202: P(202, "game.exe", 200, 22, rss_mb=50, exe="C:\\Steam\\steamapps\\common\\G\\game.exe"),
    }
    apps = by_pid(build_apps(procs, ctx(windows={102: [9001], 200: [9002]})))
    check("game carved out of Steam with its same-name members",
          200 in apps and apps[200].category == "game" and set(apps[200].member_pids) == {200, 201, 202}
          and apps[200].preselect and apps[200].reason == "game dir: steamapps" and apps[200].ram_mb == 3055.0,
          str(apps.get(200)))
    # integrator change: a helper in the SAME game install folder folds into its parent (no 5 MB crash-handler rows)
    check("same-install-folder game helper folded into the game",
          201 not in apps and 201 in apps[200].member_pids, str(apps.get(201)))
    procs[201].exe = "C:\\Tools\\crashhandler.exe"   # not under a game dir => absorbed into game tree
    apps2 = by_pid(build_apps(procs, ctx(windows={102: [9001], 200: [9002]})))
    check("non-eligible differently-named child absorbed into parent app",
          201 not in apps2 and set(apps2[200].member_pids) == {200, 201, 202})
    check("same-name chain is one family (steamwebhelper x3)",
          101 in apps and set(apps[101].member_pids) == {101, 102, 103} and apps[101].category == "window"
          and apps[101].reason == "open window", str(apps.get(101)))
    check("Steam root keeps only its own tree (background 1.2 GB)",
          100 in apps and apps[100].member_pids == [100] and apps[100].category == "background"
          and apps[100].ram_mb == 1200.0, str(apps.get(100)))
    allm = [m for a in apps.values() for m in a.member_pids]
    check("trees disjoint", len(allm) == len(set(allm)))
    check("explorer (system) never a candidate", 1 not in apps)
    check("hwnds collected from members", apps[200].hwnds == [9002] and apps[101].hwnds == [9001])

    # (b2) integrator rules: helper under the parent's install folder folds in (real Steam layout);
    #      game launched by Steam stays its own app; tiny game-dir trees and game_exclude names are not games
    S = "C:\\Program Files (x86)\\Steam"
    procs = {
        1: P(1, "explorer.exe", 0, 1, exe="C:\\Windows\\explorer.exe"),
        100: P(100, "steam.exe", 1, 10, rss_mb=150, exe=S + "\\steam.exe"),
        101: P(101, "steamwebhelper.exe", 100, 11, rss_mb=400, exe=S + "\\bin\\cef\\cef.win64\\steamwebhelper.exe"),
        200: P(200, "game.exe", 100, 20, rss_mb=3000, exe=S + "\\steamapps\\common\\G\\game.exe"),
        300: P(300, "wallpaper64.exe", 1, 30, rss_mb=80, exe=S + "\\steamapps\\common\\wallpaper_engine\\wallpaper64.exe"),
        400: P(400, "RiotClientCrashHandler.exe", 1, 40, rss_mb=10, exe="C:\\Riot Games\\Riot Client\\RiotClientCrashHandler.exe"),
        500: P(500, "EpicGamesLauncher.exe", 1, 50, rss_mb=95, exe="C:\\Epic Games\\Launcher\\Portal\\Binaries\\Win64\\EpicGamesLauncher.exe"),
        501: P(501, "EpicWebHelper.exe", 500, 51, rss_mb=45, exe="C:\\Epic Games\\Launcher\\Engine\\Binaries\\Win64\\EpicWebHelper.exe"),
        502: P(502, "Fortnite.exe", 500, 52, rss_mb=4000, exe="C:\\Epic Games\\Fortnite\\FortniteClient.exe"),
    }
    c2 = ctx(game_dirs=["steamapps", "Epic Games", "Riot Games"], windows={101: [7001], 200: [7002]},
             game_exclude=frozenset({"wallpaper64"}))
    apps = by_pid(build_apps(procs, c2))
    check("steamwebhelper (under Steam's folder) folded into steam.exe",
          100 in apps and 101 not in apps and set(apps[100].member_pids) == {100, 101}
          and apps[100].category == "window", str(apps.get(100)))
    check("Steam-launched game stays its own preselected app",
          200 in apps and apps[200].category == "game" and apps[200].preselect and apps[200].member_pids == [200])
    check("game_exclude name never a game (wallpaper64)", 300 not in apps or apps[300].category != "game")
    check("game-dir tree below min_game_ram_mb not a game", 400 not in apps)
    check("EpicWebHelper (same install folder) folded into launcher",
          500 in apps and 501 not in apps and set(apps[500].member_pids) == {500, 501} and apps[500].ram_mb == 140.0,
          str(apps.get(500)))
    check("Epic-launched game (different install folder) stays separate",
          502 in apps and apps[502].category == "game" and 502 not in apps[500].member_pids)
    check("game_install_key: steam common depth 2",
          detector.game_install_key(S + "\\steamapps\\common\\G\\bin\\x.exe", ["steamapps"])
          == os.path.normcase(S + "\\steamapps\\common\\g"))

    # (c) Rule B: app hosting cmd.exe is dropped; cmd's children protected (Rule A)
    procs = {1: P(1, "explorer.exe", 0, 1, exe="C:\\Windows\\explorer.exe"),
             300: P(300, "ide.exe", 1, 10, rss_mb=1500),
             301: P(301, "cmd.exe", 300, 11, exe="C:\\Windows\\System32\\cmd.exe"),
             302: P(302, "worker.exe", 301, 12, rss_mb=5000)}
    apps = by_pid(build_apps(procs, ctx(windows={300: [1]})))
    check("Rule B: app containing TREE_PROTECTED dropped", 300 not in apps)
    check("Rule A: descendant of cmd protected", 302 not in apps)
    # whitelist name acts as TREE_PROTECTED
    procs2 = {300: P(300, "Brave.exe", 0, 10, rss_mb=1500), 302: P(302, "helper.exe", 300, 12, rss_mb=5000)}
    apps = by_pid(build_apps(procs2, ctx(tree_protected=detector.TREE_PROTECTED | {"brave"})))
    check("whitelisted root + its descendants protected", not apps)

    # (d) Rule C: system-protected member excluded, its descendants still members
    procs = {400: P(400, "tool.exe", 0, 10, rss_mb=900),
             401: P(401, "conhost.exe", 400, 11, rss_mb=500, exe="C:\\Windows\\System32\\conhost.exe"),
             402: P(402, "sub.exe", 401, 12, rss_mb=200)}
    apps = by_pid(build_apps(procs, ctx()))
    check("Rule C: conhost excluded from members and RAM, grandchild kept",
          400 in apps and set(apps[400].member_pids) == {400, 402} and apps[400].ram_mb == 1100.0,
          str(apps.get(400)))
    # SystemRoot root not a candidate, but its children are (UWP under svchost)
    procs = {500: P(500, "svchost.exe", 0, 1, exe="C:\\Windows\\System32\\svchost.exe"),
             501: P(501, "weird.exe", 0, 1, rss_mb=3000, exe="C:\\Windows\\weird.exe"),
             502: P(502, "uwpgame.exe", 500, 5, rss_mb=3000, exe="C:\\XboxGames\\G\\uwpgame.exe")}
    apps = by_pid(build_apps(procs, ctx(game_dirs=["XboxGames"])))
    check("SystemRoot exe never a candidate; svchost child eligible as game",
          501 not in apps and 500 not in apps and 502 in apps and apps[502].category == "game")

    # (e) self + ancestors hard; siblings under explorer still eligible
    procs = {1: P(1, "explorer.exe", 0, 1, exe="C:\\Windows\\explorer.exe"),
             600: P(600, "launcher.exe", 1, 10, rss_mb=2000),
             601: P(601, "pythonw.exe", 600, 11, exe="C:\\Py\\pythonw.exe"),
             602: P(602, "kid.exe", 601, 12, rss_mb=2000),
             700: P(700, "sibling.exe", 1, 10, rss_mb=2000)}
    apps = by_pid(build_apps(procs, ctx(self_pid=601, tree_protected=frozenset())))
    check("self ancestor (launcher) not a candidate", 600 not in apps)
    check("self descendant not a candidate", 602 not in apps)
    check("sibling under explorer (ancestor) still eligible", 700 in apps)

    # (f) Strata: server process + engine + exe under strata_dir; launcher hosting it is dropped
    procs = {800: P(800, "wrapper.exe", 0, 10, rss_mb=2000),
             801: P(801, "python.exe", 800, 11, exe="C:\\Py\\python.exe",
                    cmd=["C:\\Py\\python.exe", "C:\\Strata\\serve\\server.py", "--port", "8080"]),
             802: P(802, "strata.exe", 801, 12, rss_mb=60000, exe="C:\\Strata\\engine\\strata.exe"),
             803: P(803, "other.exe", 0, 10, rss_mb=3000, exe="C:\\Strata\\tools\\other.exe")}
    apps = by_pid(build_apps(procs, ctx(tree_protected=frozenset())))
    check("Strata server tree + exe under strata_dir never candidates; host dropped (Rule B)", not apps,
          str(list(apps)))
    # empty strata_dir disables the rule (does not match everything)
    procs = {900: P(900, "x.exe", 0, 10, rss_mb=3000)}
    check("empty strata_dir/system_root match nothing", 900 in by_pid(build_apps(procs, ctx(strata_dir="", system_root=""))))

    # (g) session mismatch / unknown exe / provider pid
    procs = {1000: P(1000, "a.exe", 0, 10, rss_mb=3000), 1001: P(1001, "b.exe", 0, 10, rss_mb=3000, exe=""),
             1002: P(1002, "c.exe", 0, 10, rss_mb=3000), 1003: P(1003, "d.exe", 1002, 11, rss_mb=3000)}
    apps = by_pid(build_apps(procs, ctx(session_ok=lambda pid: pid != 1000, extra_hard_pids={1002})))
    check("foreign session not a candidate", 1000 not in apps)
    check("empty exe not a candidate", 1001 not in apps)
    check("provider pid + its descendants protected", 1002 not in apps and 1003 not in apps)

    # (h) GPU / clamp / priority / thresholds
    procs = {1100: P(1100, "render.exe", 0, 10, rss_mb=100), 1101: P(1101, "render.exe", 1100, 11, rss_mb=100),
             1200: P(1200, "g.exe", 0, 10, rss_mb=100, exe="C:\\Epic Games\\G\\g.exe"),
             1300: P(1300, "small.exe", 0, 10, rss_mb=200), 1400: P(1400, "mid.exe", 0, 10, rss_mb=600)}
    apps = by_pid(build_apps(procs, ctx(vram_mb={1100: 3000.0, 1101: 3000.0, 1200: 5000.0}, vram_cap_mb=4000.0,
                                        windows={1300: [5], 1400: [6]})))
    check("gpu category, tree VRAM clamped to adapter usage",
          1100 in apps and apps[1100].category == "gpu" and apps[1100].vram_mb == 4000.0
          and apps[1100].reason == "GPU memory", str(apps.get(1100)))
    check("game beats gpu", apps.get(1200) is not None and apps[1200].category == "game")
    check("window below min_ram_mb not a candidate", 1300 not in apps)
    check("window above min_ram_mb is a candidate", 1400 in apps and apps[1400].category == "window")
    apps = by_pid(build_apps(procs, ctx(vram_mb=None)))
    check("PDH failure => vram_mb None and no gpu category",
          all(a.vram_mb is None and a.category != "gpu" for a in apps.values()))
    check("background needs > 1000 MB", 1400 not in apps)
    check("dict keys exact", all(set(a.to_dict()) == set(detector.PUBLIC_KEYS) for a in apps.values()))
    check("norm strips one .exe", norm("Foo.EXE.exe") == "foo.exe" and norm("Code - Insiders.exe") == "code - insiders")


# ----------------------------------------------------------------------------------------- 3. real kill

class OneChildDetector(ProcessDetector):
    """_scan_apps returns ONLY an App built from processes this test spawned."""

    def __init__(self, members: list[psutil.Process]) -> None:
        super().__init__(config_store.load_config())
        self._members = members

    def _scan_apps(self) -> list[App]:
        root = self._members[0]
        return [App(root=root, create_time=root.create_time(), members=list(self._members), hwnds=[],
                    pid=root.pid, name=root.name(), exe=root.exe(), ram_mb=1.0, vram_mb=0.0,
                    reason="test child", category="background", preselect=False,
                    member_count=len(self._members), member_pids=[m.pid for m in self._members])]


def spawn_child() -> tuple[subprocess.Popen, list[psutil.Process]]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             creationflags=CREATE_NO_WINDOW)
    root = psutil.Process(child.pid)
    time.sleep(1.0)          # venv python.exe is a redirector: let it spawn the real interpreter
    members = [root] + root.children(recursive=True)
    return child, members


def test_real_kill() -> None:
    section("3. real kill via _scan_apps seam (throwaway child only)")
    real_strata = config_store.load_config().get("strata_dir") or ""
    critical = {p.pid: p for p in psutil.Process().parents()}
    labels = {p.pid: f"ancestor {p.name()}" for p in critical.values()}
    for p in psutil.process_iter(["name", "cmdline", "exe"]):
        cmd = " ".join(p.info["cmdline"] or [])
        is_real_strata = (detector.is_strata_server_cmdline(p.info["cmdline"])
                          and os.path.normcase(real_strata) in os.path.normcase(cmd)) \
            or detector.under_dir(p.info["exe"], real_strata)
        if norm(p.info["name"]) == "hermes" or is_real_strata:   # (other agents' fake Stratas excluded)
            critical[p.pid] = p
            labels[p.pid] = f"{p.info['name']}"
    print(f"  watching {len(critical)} critical processes: {sorted(set(labels.values()))}")
    child, members = spawn_child()
    child2, members2 = None, []
    try:
        print(f"  spawned test tree: {[(m.pid, m.name()) for m in members]}")
        det = OneChildDetector(members)
        try:
            ct = members[0].create_time()
            r = det.sweep_processes(dry_run=False, targets=[(members[0].pid, ct + 5.0)])
            print(f"  mismatch run: {r}")
            check("wrong create_time => nothing closed", r["closed_apps"] == [] and r["names"] == [])
            check("wrong create_time => child still alive", all(m.is_running() for m in members))
            r = det.sweep_processes(dry_run=False, targets=[(members[0].pid, 12345.0)])
            check("unknown target ignored", r["closed_apps"] == [] and members[0].is_running())
            r = det.sweep_processes(dry_run=True)
            check("dry_run on seam lists child, kills nothing", r["names"] == [members[0].name()]
                  and r["candidates"] == 1 and all(m.is_running() for m in members))
            t = time.perf_counter()
            r = det.sweep_processes(dry_run=False, targets=[(members[0].pid, ct + 0.005)])
            print(f"  kill run ({time.perf_counter() - t:.2f}s): {r}")
            check("real sweep keys exact", set(r) == SWEEP_KEYS)
            check("child tree closed", r["closed_apps"] == [members[0].name()] and r["failed"] == [], str(r))
            check("all child members gone", not any(m.is_running() for m in members))
            check("vram_freed_mb float|None", r["vram_freed_mb"] is None or isinstance(r["vram_freed_mb"], float))
        finally:
            det.close()
        # targets=None selects all seam candidates
        child2, members2 = spawn_child()
        det2 = OneChildDetector(members2)
        try:
            r = det2.sweep_processes(dry_run=False)
            check("real sweep with targets=None is REFUSED (no mass kill)",
                  not r["closed_apps"] and r["failed"] and all(m.is_running() for m in members2), str(r))
            cand = det2.scan_killable_processes()
            r = det2.sweep_processes(dry_run=False, targets=[(c["pid"], c["create_time"]) for c in cand])
            check("explicit targets close the seam candidates", r["closed_apps"] and not any(m.is_running() for m in members2),
                  str(r))
        finally:
            det2.close()
        dead = [f"{labels.get(p.pid)}({p.pid})" for p in critical.values() if not p.is_running()]
        check("self ancestors / Hermes / Strata untouched", not dead, ",".join(dead))
    finally:
        for m in list(members) + list(members2):   # cleanup on failure: only processes spawned above
            try:
                if m.is_running():
                    m.kill()
            except psutil.Error:
                pass
        for c in (child, child2):
            if c is not None:
                c.wait(timeout=5)


# ----------------------------------------------------------------------------------------- 4. whitelist

def test_whitelist() -> None:
    section("4. update_whitelist round-trip on a TEMP copy of config.json")
    real = config_store.CONFIG_PATH
    real_hash = hashlib.sha256(Path(real).read_bytes()).hexdigest()
    tmpdir = Path(tempfile.mkdtemp(prefix="hc-detector-test-"))
    tmp = tmpdir / "config.json"
    shutil.copyfile(real, tmp)
    old = config_store.CONFIG_PATH
    config_store.CONFIG_PATH = tmp
    try:
        cfg = config_store.load_config()
        d = ProcessDetector(cfg)
        try:
            check("get_whitelist is a copy", d.get_whitelist() == cfg["user_whitelist"]
                  and d.get_whitelist() is not cfg["user_whitelist"])
            disk = json.loads(tmp.read_text(encoding="utf-8"))
            disk["min_ram_mb"] = 777                    # changed on disk behind the detector's back
            tmp.write_text(json.dumps(disk), encoding="utf-8")
            d.update_whitelist(["b.exe", "A.EXE", "a.exe", "", "notexe", "  c.exe ", 5, "Zed.Exe", ".exe"])
            want = ["A.EXE", "b.exe", "c.exe", "Zed.Exe"]
            disk = json.loads(tmp.read_text(encoding="utf-8"))
            check("saved list filtered/deduped/sorted", disk["user_whitelist"] == want, str(disk["user_whitelist"]))
            check("other disk keys preserved (re-loaded before save)", disk["min_ram_mb"] == 777
                  and disk["launch_cmd"] == cfg["launch_cmd"])
            check("shared in-memory dict updated in place", cfg["user_whitelist"] == want and d.config is cfg)
            check("in-memory non-whitelist keys untouched", cfg["min_ram_mb"] != 777)
            check("get_whitelist reflects update", d.get_whitelist() == want)
        finally:
            d.close()
    finally:
        config_store.CONFIG_PATH = old
        shutil.rmtree(tmpdir, ignore_errors=True)
    check("real config.json unchanged", hashlib.sha256(Path(real).read_bytes()).hexdigest() == real_hash)


# ----------------------------------------------------------------------------------------- 5/6

def test_candidates_and_gpu() -> None:
    section("5. list_whitelist_candidates() shape")
    cfg = config_store.load_config()
    d = ProcessDetector(cfg)
    try:
        t = time.perf_counter()
        lst = d.list_whitelist_candidates()
        print(f"  {len(lst)} entries in {time.perf_counter() - t:.3f}s")
        for e in lst:
            print(f"    {e['name']:<42} whitelisted={e['whitelisted']!s:<5} running={e['running']}")
        check("entries have exactly name/whitelisted/running",
              all(set(e) == {"name", "whitelisted", "running"} and isinstance(e["name"], str)
                  and isinstance(e["whitelisted"], bool) and isinstance(e["running"], bool) for e in lst))
        names = [e["name"] for e in lst]
        check("sorted case-insensitively", names == sorted(names, key=str.casefold))
        check("de-duped by normalised name", len({norm(n) for n in names}) == len(names))
        wl = {norm(n) for n in cfg["user_whitelist"]}
        check("every whitelist entry present + flagged", wl <= {norm(e["name"]) for e in lst if e["whitelisted"]})
        builtin_hit = [e["name"] for e in lst if norm(e["name"]) in detector.TREE_PROTECTED and norm(e["name"]) not in wl]
        check("no built-in TREE_PROTECTED names offered", not builtin_hit, str(builtin_hit))

        section("6. gpu_dedicated_used_mb()")
        v = d.gpu_dedicated_used_mb()
        smi = None
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
            smi = float(out.stdout.strip().splitlines()[0])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
            print(f"  nvidia-smi unavailable: {exc}")
        print(f"  PDH adapter dedicated: {v} MB; nvidia-smi: {smi} MiB")
        check("returns float", isinstance(v, float))
        if smi is not None and isinstance(v, float):
            check("within 15% / 500 MB of nvidia-smi", abs(v - smi) <= max(500.0, 0.15 * smi), f"{v} vs {smi}")
    finally:
        d.close()


# ----------------------------------------------------------------------------------------- 7. launch records

class AppsSeamDetector(ProcessDetector):
    """_scan_apps returns exactly the given Apps (built only from processes this test spawned)."""

    def __init__(self, apps: list[App]) -> None:
        super().__init__(config_store.load_config())
        self._apps = apps

    def _scan_apps(self) -> list[App]:
        return list(self._apps)


LR_KEYS = {"name", "exe", "cmdline", "cwd", "category", "create_time", "elevated", "aumid"}


def _spawn_tree(tmpdir: Path, marker: str) -> tuple[subprocess.Popen, psutil.Process, list[psutil.Process]]:
    """venv python parent.py (spawns a child python) -> root + all descendants (conhost excluded)."""
    script = tmpdir / "parent.py"
    script.write_text("import subprocess, sys, time\n"
                      f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', {marker!r}])\n"
                      "time.sleep(120)\n", encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script), marker], cwd=str(tmpdir), creationflags=CREATE_NO_WINDOW)
    root = psutil.Process(p.pid)
    deadline = time.time() + 6
    kids: list[psutil.Process] = []
    while time.time() < deadline:
        kids = [c for c in root.children(recursive=True) if norm(c.name()) != "conhost"]
        if sum(1 for c in kids if any(marker in a for a in c.cmdline())) >= 2:   # parent interp + child
            break
        time.sleep(0.1)
    return p, root, [root] + kids


def _seam_app(root: psutil.Process, members: list[psutil.Process], category: str = "window") -> App:
    return App(root=root, create_time=root.create_time(), members=list(members), hwnds=[], pid=root.pid,
               name=root.name(), exe=root.exe(), ram_mb=1.0, vram_mb=0.0, reason="test tree", category=category,
               preselect=False, member_count=len(members), member_pids=[m.pid for m in members])


def _kill_marked(marker: str) -> None:
    me = os.getpid()
    procs = [p for p in psutil.process_iter(["cmdline"], ad_value=None)
             if p.pid != me and any(marker in str(c) for c in (p.info["cmdline"] or []))]
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


def test_launch_records() -> None:
    section("7. launch records captured by a real sweep (own dummy tree only)")
    marker = f"hc-lr-test-{os.getpid()}-{int(time.time())}"
    tmpdir = Path(tempfile.mkdtemp(prefix=marker + "-"))
    popens: list[subprocess.Popen] = []
    try:
        p1, root, members = _spawn_tree(tmpdir, marker)
        popens.append(p1)
        print(f"  dummy tree: {[(m.pid, m.name()) for m in members]}")
        check("dummy tree has root + >= 2 descendants", len(members) >= 3, str(len(members)))
        want_exe, want_cmd, want_ct = root.exe(), root.cmdline(), root.create_time()
        app = _seam_app(root, members)

        t = time.perf_counter()
        rec = detector.capture_launch_record(app)
        ms = (time.perf_counter() - t) * 1000
        print(f"  capture_launch_record: {ms:.1f} ms -> {rec}")
        check("capture < 50 ms", ms < 50, f"{ms:.1f} ms")
        check("capture keys", rec is not None and set(rec) == LR_KEYS, str(rec))

        det = AppsSeamDetector([app])
        try:
            dr = det.sweep_processes(dry_run=True, targets=[(root.pid, want_ct)])
            check("dry_run: launch_records == [] and nothing killed",
                  dr["launch_records"] == [] and dr["names"] == [app.name] and all(m.is_running() for m in members),
                  str(dr))
            dr = det.sweep_processes(dry_run=False, targets=[(root.pid, want_ct + 5.0)])
            check("non-matching target: launch_records == []", dr["launch_records"] == [] and root.is_running())
            r = det.sweep_processes(dry_run=False, targets=[(root.pid, want_ct)])
            print(f"  sweep result: closed={r['closed_apps']} failed={r['failed']} records={r['launch_records']}")
            check("sweep closed the dummy app", r["closed_apps"] == [app.name] and not r["failed"], str(r))
            check("all dummy members gone", not any(m.is_running() for m in members))
            lr = r["launch_records"]
            check("exactly one launch record (the ROOT, not its children)", len(lr) == 1, str(lr))
            if lr:
                x = lr[0]
                check("record keys exact", set(x) == LR_KEYS, str(sorted(x)))
                check("record exe = root exe (absolute)", os.path.normcase(x["exe"]) == os.path.normcase(want_exe)
                      and os.path.isabs(x["exe"]), x["exe"])
                check("record cmdline = root cmdline", x["cmdline"] == want_cmd, str(x["cmdline"]))
                check("record cwd = spawn cwd", os.path.normcase(x["cwd"].rstrip("\\"))
                      == os.path.normcase(str(tmpdir)), x["cwd"])
                check("record elevated False, aumid None", x["elevated"] is False and x["aumid"] is None, str(x))
                check("record name/category/create_time", x["name"] == app.name and x["category"] == "window"
                      and abs(x["create_time"] - want_ct) < 0.01)
        finally:
            det.close()

        # an app that is NOT closed (empty kill set -> root survives) must get no record
        p2, root2, members2 = _spawn_tree(tmpdir, marker)
        popens.append(p2)
        app2 = _seam_app(root2, [], category="game")
        det2 = AppsSeamDetector([app2])
        try:
            r = det2.sweep_processes(dry_run=False, targets=[(root2.pid, root2.create_time())])
            check("failed app: listed in failed, no launch record",
                  r["failed"] and r["launch_records"] == [] and r["closed_apps"] == [] and root2.is_running(), str(r))
        finally:
            det2.close()
        _kill_marked(marker)
        check("capture on a dead root -> None", detector.capture_launch_record(app2) is None)
        check("capture on App with no root/members -> None",
              detector.capture_launch_record(App(root=None, create_time=0.0)) is None)
        rootless = App(root=None, create_time=0.0, members=[])
        check("_group_root(no members) -> None", detector._group_root(rootless) is None)

        # read-only probes of real processes (nothing is closed): protected process -> elevated True;
        # a packaged (WindowsApps) process -> AUMID string, when one happens to be running
        prot = next((p for p in psutil.process_iter(["name"]) if norm(p.info["name"]) in ("lsass", "services")), None)
        if prot is not None:
            pr = detector.capture_launch_record(App(root=prot, create_time=prot.create_time(), name=prot.name()))
            check("protected system process reads as elevated=True", pr is not None and pr["elevated"] is True,
                  str(pr))
        pkg = None
        my_sid = detector.session_id_of(os.getpid())
        for p in psutil.process_iter(["exe"], ad_value=None):
            if (p.info["exe"] and "\\program files\\windowsapps\\" in p.info["exe"].casefold()
                    and detector.session_id_of(p.pid) == my_sid):     # session-0 packaged services are unreadable
                pkg = p
                break
        if pkg is not None:
            pr = detector.capture_launch_record(App(root=pkg, create_time=pkg.create_time(), name=pkg.name()))
            print(f"  packaged probe: {pkg.name()} -> aumid={pr and pr['aumid']!r} elevated={pr and pr['elevated']}")
            check("packaged process yields an AUMID ('...!...')", pr is not None and isinstance(pr["aumid"], str)
                  and "!" in pr["aumid"], str(pr))
        else:
            print("  NOTE: no WindowsApps process running; real AUMID not exercised")
    finally:
        _kill_marked(marker)
        for p in popens:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    for fn in (test_synthetic, test_real_scan, test_real_kill, test_whitelist, test_candidates_and_gpu,
               test_launch_records):
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(f"{fn.__name__} raised", False, "exception")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
