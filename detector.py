"""Process detection and sweeping for Agent Start (Contract 1 of SPEC.md).

ProcessDetector finds RAM/VRAM-hungry user apps ("candidates") that may be force-closed before
Strata starts, and closes the ones the user selected.

Pipeline (one scan):
  1. one psutil.process_iter pass            -> {pid: ProcInfo}           (collect_processes)
  2. one EnumWindows pass ('real' windows)    -> {pid: [hwnd, ...]}        (enum_real_windows)
  3. PDH GPU counters (lazily opened, reused) -> {pid: dedicated MB}       (_PdhGpu)
  4. pure graph builder over that data        -> [App]                     (build_apps)
build_apps() takes only plain data (ProcInfo dict + GraphContext), so unit tests feed it synthetic graphs.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Iterable

import psutil
import pywintypes
import win32con
import win32gui
import win32pdh
import win32process

import config_store

log = logging.getLogger("agent_start")

# --------------------------------------------------------------------------------------------- constants

TREE_PROTECTED: frozenset[str] = frozenset({
    "windowsterminal", "wt", "openconsole", "powershell", "pwsh", "cmd", "bash", "sh", "mintty",
    "wsl", "wslhost", "ssh", "sshd", "alacritty", "wezterm-gui", "tabby", "conemu64", "code",
    "code - insiders", "cursor", "windsurf", "zed", "devenv", "idea64", "pycharm64", "rider64",
    "clion64", "webstorm64", "goland64", "claude", "node", "hermes", "python", "pythonw", "py",
    "pyw", "strata", "fancontrol", "librehardwaremonitor", "tailscale-ipn", "tailscaled",
})

SYSTEM_PROTECTED: frozenset[str] = frozenset({
    "system", "idle", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass", "lsaiso",
    "svchost", "fontdrvhost", "dwm", "explorer", "sihost", "ctfmon", "taskhostw", "runtimebroker",
    "dllhost", "conhost", "shellexperiencehost", "startmenuexperiencehost", "searchhost", "searchapp",
    "searchindexer", "textinputhost", "applicationframehost", "systemsettings", "lockapp",
    "securityhealthsystray", "securityhealthservice", "msmpeng", "nissrv", "audiodg", "spoolsv",
    "wmiprvse", "taskmgr", "memory compression", "secure system", "vmmem", "vmmemwsl", "wslservice",
    "wudfhost", "widgets", "smartscreen", "backgroundtaskhost", "nvcontainer", "nvdisplay.container",
    "nvsphelper64", "nvidia overlay", "nvidia app",
})

CATEGORY_PRIORITY = ("game", "gpu", "window", "background")
BACKGROUND_MIN_RAM_MB = 1000.0
CREATE_TIME_TOLERANCE = 0.01
PUBLIC_KEYS = ("pid", "name", "exe", "create_time", "ram_mb", "vram_mb", "reason", "category",
               "preselect", "member_count")

_PROC_ATTRS = ["pid", "name", "exe", "ppid", "create_time", "memory_info", "cmdline"]
_PDH_PROC_COUNTER = r"\GPU Process Memory(*)\Dedicated Usage"
_PDH_ADAPTER_COUNTER = r"\GPU Adapter Memory(*)\Dedicated Usage"
_PDH_PID_RE = re.compile(r"pid_(\d+)_", re.IGNORECASE)
_STRATA_MARK = os.path.normcase("serve\\server.py")
_MB = 1024.0 * 1024.0
_DWMWA_CLOAKED = 14


# --------------------------------------------------------------------------------------------- helpers

def norm(name: str | None) -> str:
    """casefold + strip ONE trailing '.exe'."""
    n = (name or "").casefold()
    return n[:-4] if n.endswith(".exe") else n


def norm_path(p: str | None) -> str:
    """normcase(abspath(p)); '' for empty/None."""
    if not p:
        return ""
    try:
        return os.path.normcase(os.path.abspath(p))
    except (OSError, ValueError):
        return ""


def under_dir(path: str | None, directory: str | None) -> bool:
    """True iff path lies strictly under directory. Empty directory or path never matches."""
    p, d = norm_path(path), norm_path(directory)
    if not p or not d:
        return False
    return p.startswith(d.rstrip(os.sep) + os.sep)


def is_strata_server_cmdline(cmdline: Iterable[str] | None) -> bool:
    """Strata identity: joined, normcased cmdline contains serve\\server.py."""
    if not cmdline:
        return False
    try:
        joined = " ".join(str(c) for c in cmdline)
    except TypeError:
        return False
    return _STRATA_MARK in os.path.normcase(joined)


def game_dir_match(exe: str | None, game_dirs: Iterable[str]) -> str | None:
    """Return the configured game_dir whose '\\<dir>\\' segment appears in exe, else None."""
    p = norm_path(exe)
    if not p:
        return None
    for gd in game_dirs or ():
        if not gd or not str(gd).strip():
            continue
        seg = os.sep + os.path.normcase(str(gd).strip().strip("\\/")) + os.sep
        if seg != os.sep + os.sep and seg in p:
            return str(gd)
    return None


def game_install_key(exe: str | None, game_dirs: Iterable[str]) -> str | None:
    """Install folder of a game-dir exe: '<...>\\<game_dir>\\<install>' (Steam: '...\\steamapps\\common\\<game>').

    Used to fold helpers (EpicWebHelper, crash handlers, overlay renderers) into the launcher/game that
    spawned them when both live in the same install folder, instead of listing each as its own row.
    """
    p = norm_path(exe)
    if not p:
        return None
    for gd in game_dirs or ():
        if not gd or not str(gd).strip():
            continue
        seg = os.sep + os.path.normcase(str(gd).strip().strip("\\/")) + os.sep
        i = p.find(seg)
        if seg == os.sep + os.sep or i < 0:
            continue
        rest = p[i + len(seg):].split(os.sep)
        depth = 2 if rest and rest[0] == "common" else 1
        if len(rest) <= depth:                     # exe sits directly in the game dir / 'common'
            return p[: i + len(seg)] + os.sep.join(rest[:-1])
        return p[: i + len(seg)] + os.sep.join(rest[:depth])
    return None


def fmt_gb(mb: float) -> str:
    return f"{mb / 1024.0:.1f} GB"


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
try:
    _dwmapi = ctypes.WinDLL("dwmapi")
    _dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
except OSError:
    _dwmapi = None


def session_id_of(pid: int) -> int | None:
    """Terminal-services session of pid, or None on failure."""
    sid = wintypes.DWORD(0)
    try:
        ok = _kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))
    except (OSError, ctypes.ArgumentError):
        return None
    return int(sid.value) if ok else None


def _is_cloaked(hwnd: int) -> bool:
    if _dwmapi is None:
        return False
    val = ctypes.c_int(0)
    hr = _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return hr == 0 and val.value != 0


def is_real_window(hwnd: int) -> bool:
    """Visible, titled, not a tool window, unowned, not DWM-cloaked, non-empty rect."""
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if not win32gui.GetWindowText(hwnd):
        return False
    if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
        return False
    if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
        return False
    if _is_cloaked(hwnd):
        return False
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return right > left and bottom > top


def enum_real_windows() -> dict[int, list[int]]:
    """One EnumWindows pass -> {pid: [real top-level hwnds]}."""
    found: dict[int, list[int]] = {}

    def _cb(hwnd: int, _extra: object) -> bool:
        try:
            if is_real_window(hwnd):
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                found.setdefault(int(pid), []).append(hwnd)
        except pywintypes.error:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except pywintypes.error as exc:
        log.warning("EnumWindows failed: %s", exc)
    return found


# --------------------------------------------------------------------------------------------- data

@dataclass
class ProcInfo:
    """One process as captured by a scan (proc is None for synthetic test data)."""
    pid: int
    name: str
    exe: str | None
    ppid: int | None
    create_time: float | None
    rss: int = 0
    cmdline: list[str] | None = None
    proc: psutil.Process | None = None

    @property
    def nname(self) -> str:
        return norm(self.name)


@dataclass
class App:
    """A candidate app: family root + disjoint kill set captured in one scan."""
    root: psutil.Process | None
    create_time: float
    members: list[psutil.Process] = field(default_factory=list)
    hwnds: list[int] = field(default_factory=list)
    pid: int = 0
    name: str = ""
    exe: str = ""
    ram_mb: float = 0.0
    vram_mb: float | None = None
    reason: str = ""
    category: str = "background"
    preselect: bool = False
    member_count: int = 0
    member_pids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pid": int(self.pid),
            "name": str(self.name),
            "exe": str(self.exe),
            "create_time": float(self.create_time),
            "ram_mb": round(float(self.ram_mb), 1),
            "vram_mb": None if self.vram_mb is None else round(float(self.vram_mb), 1),
            "reason": str(self.reason),
            "category": str(self.category),
            "preselect": bool(self.preselect),
            "member_count": int(self.member_count),
        }


@dataclass
class GraphContext:
    """Everything build_apps needs besides the process table (all plain data)."""
    self_pid: int | None = None
    extra_hard_pids: set[int] = field(default_factory=set)
    tree_protected: frozenset[str] = TREE_PROTECTED
    system_protected: frozenset[str] = SYSTEM_PROTECTED
    strata_dir: str = ""
    system_root: str = ""
    game_dirs: list[str] = field(default_factory=list)
    min_ram_mb: float = 250.0
    min_vram_mb: float = 200.0
    min_game_ram_mb: float = 50.0                  # game-dir trees below this are noise (crash handlers)
    game_exclude: frozenset[str] = frozenset()     # normalised names never treated as "game" (Wallpaper Engine)
    windows: dict[int, list[int]] = field(default_factory=dict)
    vram_mb: dict[int, float] | None = None       # per-pid, already clamped; None = PDH failed
    vram_cap_mb: float | None = None              # adapter-wide dedicated usage
    session_ok: Callable[[int], bool] = lambda pid: True
    # engine_protects(info) -> True if the process belongs to the inference engine (never a sweep candidate).
    # None = the built-in Strata rule (serve\server.py in the cmdline, or an exe under strata_dir).
    engine_protects: Callable[["ProcInfo"], bool] | None = None


# --------------------------------------------------------------------------------------------- graph

class ProcGraph:
    """Validated parent/child relations over a process table (create_time rule)."""

    def __init__(self, procs: dict[int, ProcInfo]) -> None:
        self.procs = procs
        self.children: dict[int, list[int]] = {}
        self.parent: dict[int, int] = {}
        for pid, info in procs.items():
            par = self.valid_parent(pid)
            if par is not None:
                self.parent[pid] = par
                self.children.setdefault(par, []).append(pid)

    def valid_parent(self, pid: int) -> int | None:
        """Parent pid iff it exists, differs, and was created no later than the child."""
        info = self.procs.get(pid)
        if info is None or info.ppid is None or info.ppid == pid:
            return None
        par = self.procs.get(info.ppid)
        if par is None or par.create_time is None or info.create_time is None:
            return None
        return info.ppid if par.create_time <= info.create_time else None

    def ancestors(self, pid: int) -> list[int]:
        out: list[int] = []
        seen = {pid}
        cur = self.parent.get(pid)
        while cur is not None and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = self.parent.get(cur)
        return out

    def descendants(self, pid: int) -> set[int]:
        out: set[int] = set()
        stack = list(self.children.get(pid, ()))
        while stack:
            c = stack.pop()
            if c in out or c == pid:
                continue
            out.add(c)
            stack.extend(self.children.get(c, ()))
        return out

    def depth(self, pid: int) -> int:
        return len(self.ancestors(pid))

    def is_family_root(self, pid: int) -> bool:
        par = self.parent.get(pid)
        return par is None or self.procs[par].nname != self.procs[pid].nname


def compute_protection(procs: dict[int, ProcInfo], graph: ProcGraph,
                       ctx: GraphContext) -> tuple[set[int], set[int]]:
    """Return (hard_pids, rule_a_pids).

    hard: self + validated ancestors + descendants of self, provider pids, the engine's processes (for Strata:
    server processes + descendants, exe under strata_dir).
    rule_a: validated descendants of TREE_PROTECTED processes and of HARD pids. Self's ancestors are HARD
    (never killed, Rule B) but do NOT seed Rule A — otherwise explorer.exe (ancestor when launched by
    double-click) would protect every desktop app.
    """
    hard: set[int] = set()
    seeds: set[int] = set()
    if ctx.self_pid is not None and ctx.self_pid in procs:
        hard.add(ctx.self_pid)
        seeds.add(ctx.self_pid)
        hard.update(graph.ancestors(ctx.self_pid))
    elif ctx.self_pid is not None:
        hard.add(ctx.self_pid)
    for pid in ctx.extra_hard_pids:
        hard.add(pid)
        seeds.add(pid)
    for pid, info in procs.items():
        if _engine_process(info, ctx):
            hard.add(pid)
            seeds.add(pid)
        if _tree_name(info.nname, ctx):
            seeds.add(pid)
    rule_a: set[int] = set()
    for s in seeds:
        if s in procs:
            rule_a |= graph.descendants(s)
    # descendants of self / provider / Strata are themselves HARD
    for s in seeds:
        if s in hard and s in procs:
            hard |= graph.descendants(s)
    return hard, rule_a


def _engine_process(info: ProcInfo, ctx: GraphContext) -> bool:
    """Engine-owned = always protected. An engine predicate that RAISES falls back to the built-in Strata rule.
    (A predicate that wrongly answers no is the engine's own bug: its processes are then ordinary candidates,
    still only closed if the user ticks them.)"""
    builtin = is_strata_server_cmdline(info.cmdline) or under_dir(info.exe, ctx.strata_dir)
    if ctx.engine_protects is None:
        return builtin
    try:
        return bool(ctx.engine_protects(info))
    except Exception:  # noqa: BLE001
        return builtin


def _tree_name(nname: str, ctx: GraphContext) -> bool:
    """TREE_PROTECTED by name; any python* interpreter (python3, python3.14, pythonw3) counts too."""
    return nname in ctx.tree_protected or nname.startswith("python")


def _system_protected(info: ProcInfo, ctx: GraphContext) -> bool:
    """Process-only protection (Rule C): system names, %SystemRoot%, unknown exe, foreign session."""
    if info.nname in ctx.system_protected:
        return True
    if not info.exe:
        return True
    if under_dir(info.exe, ctx.system_root):
        return True
    if info.create_time is None:
        return True
    return not ctx.session_ok(info.pid)


def build_apps(procs: dict[int, ProcInfo], ctx: GraphContext) -> list[App]:
    """Pure graph builder: family roots deepest-first -> disjoint candidate App trees (RAM desc)."""
    graph = ProcGraph(procs)
    hard, rule_a = compute_protection(procs, graph, ctx)

    def protected_tree(pid: int) -> bool:          # Rule A/B trigger
        return pid in hard or pid in rule_a or _tree_name(procs[pid].nname, ctx)

    assigned: set[int] = set()                     # roots of accepted apps (carved out)
    apps: list[App] = []
    def is_root(pid: int) -> bool:
        if not graph.is_family_root(pid):
            return False
        par = graph.parent.get(pid)
        if par is None:
            return True
        cexe, pexe = procs[pid].exe, procs[par].exe
        ckey = game_install_key(cexe, ctx.game_dirs)
        if ckey is not None:
            # game-dir exe: fold into the parent only when both share the same install folder
            # (EpicWebHelper -> EpicGamesLauncher); a game launched by Steam/Epic stays its own app
            return ckey != game_install_key(pexe, ctx.game_dirs)
        # helper installed under the parent's own folder (steam.exe -> bin\cef\steamwebhelper.exe)
        pdir = os.path.dirname(norm_path(pexe)) if pexe else ""
        if (pdir and len(pdir.rstrip(os.sep).split(os.sep)) >= 3          # never a drive/Program Files root
                and not under_dir(pexe, ctx.system_root) and under_dir(cexe, pdir)):
            return False
        return True

    roots = [p for p in procs if is_root(p)]
    roots.sort(key=lambda p: (-graph.depth(p), p))
    for rpid in roots:
        rinfo = procs[rpid]
        if protected_tree(rpid) or _system_protected(rinfo, ctx):
            continue
        members: list[int] = []
        dropped = False
        stack = [rpid]
        seen: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if pid != rpid and pid in assigned:
                continue                           # nested eligible app, already carved out
            if protected_tree(pid):
                dropped = True                     # Rule B
                break
            if not _system_protected(procs[pid], ctx):
                members.append(pid)                # Rule C excludes system-protected from kill set
            stack.extend(graph.children.get(pid, ()))
        if dropped or not members:
            continue
        ram = sum(max(0, procs[m].rss or 0) for m in members) / _MB
        vram: float | None = None
        if ctx.vram_mb is not None:
            vram = sum(ctx.vram_mb.get(m, 0.0) for m in members)
            if ctx.vram_cap_mb is not None:
                vram = min(vram, ctx.vram_cap_mb)
        hwnds = [h for m in members for h in ctx.windows.get(m, ())]
        gd = None if rinfo.nname in ctx.game_exclude else game_dir_match(rinfo.exe, ctx.game_dirs)
        if gd is not None and ram >= ctx.min_game_ram_mb:
            category, reason = "game", f"game dir: {gd}"
        elif vram is not None and vram > ctx.min_vram_mb:
            category, reason = "gpu", "GPU memory"
        elif hwnds and ram > ctx.min_ram_mb:
            category, reason = "window", "open window"
        elif not hwnds and ram > BACKGROUND_MIN_RAM_MB:
            category, reason = "background", "background / tray"
        else:
            continue
        assigned.add(rpid)
        apps.append(App(
            root=rinfo.proc, create_time=float(rinfo.create_time),
            members=[procs[m].proc for m in members if procs[m].proc is not None],
            hwnds=hwnds, pid=rpid, name=rinfo.name, exe=rinfo.exe or "",
            ram_mb=round(ram, 1), vram_mb=None if vram is None else round(vram, 1),
            reason=reason, category=category, preselect=(category == "game"),
            member_count=len(members), member_pids=sorted(members),
        ))
    apps.sort(key=lambda a: (-a.ram_mb, a.name.casefold(), a.pid))
    return apps


def collect_processes() -> dict[int, ProcInfo]:
    """ONE psutil.process_iter pass -> {pid: ProcInfo}."""
    out: dict[int, ProcInfo] = {}
    for p in psutil.process_iter(_PROC_ATTRS, ad_value=None):
        i = p.info
        pid = p.pid                                # not i["pid"]: .info can be shared with another iterator
        mi = i.get("memory_info")
        out[pid] = ProcInfo(
            pid=pid, name=i.get("name") or "", exe=i.get("exe") or None, ppid=i.get("ppid"),
            create_time=i.get("create_time"), rss=int(getattr(mi, "rss", 0) or 0),
            cmdline=i.get("cmdline") or None, proc=p,
        )
    return out


# --------------------------------------------------------------------------------------------- PDH

class _PdhGpu:
    """Lazily opened, reused PDH queries for GPU dedicated memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._q_adapter = None
        self._c_adapter = None
        self._q_proc = None
        self._c_proc = None
        self._closed = False
        self._warned: set[str] = set()

    @staticmethod
    def _open(path: str) -> tuple:
        q = win32pdh.OpenQuery()
        try:
            c = win32pdh.AddEnglishCounter(q, path)
        except pywintypes.error:
            win32pdh.CloseQuery(q)
            raise
        return q, c

    def _ensure_adapter(self) -> bool:
        """Open the adapter query only (independent of the per-process counter). False once closed."""
        if self._closed:
            return False
        if self._q_adapter is None:
            self._q_adapter, self._c_adapter = self._open(_PDH_ADAPTER_COUNTER)
        return True

    def _ensure_proc(self) -> bool:
        if self._closed:
            return False
        if self._q_proc is None:
            self._q_proc, self._c_proc = self._open(_PDH_PROC_COUNTER)
        return True

    def _warn(self, what: str, exc: Exception) -> None:
        """Log a PDH failure once per counter at WARNING, then at DEBUG (the preview polls every 5 s)."""
        if what in self._warned:
            log.debug("PDH %s query failed: %s", what, exc)
        else:
            self._warned.add(what)
            log.warning("PDH %s query failed (further failures logged at DEBUG): %s", what, exc)

    def adapter_used_mb(self) -> float | None:
        with self._lock:
            try:
                if not self._ensure_adapter():
                    return None
                win32pdh.CollectQueryData(self._q_adapter)
                arr = win32pdh.GetFormattedCounterArray(self._c_adapter, win32pdh.PDH_FMT_LARGE)
            except pywintypes.error as exc:
                self._warn("adapter", exc)
                return None
        if not arr:
            return None
        return max(float(v) for v in arr.values()) / _MB

    def process_used_mb(self) -> dict[int, float] | None:
        """{pid: dedicated MB} summed over instances (unclamped), or None on failure."""
        with self._lock:
            try:
                if not self._ensure_proc():
                    return None
                win32pdh.CollectQueryData(self._q_proc)
                arr = win32pdh.GetFormattedCounterArray(self._c_proc, win32pdh.PDH_FMT_LARGE)
            except pywintypes.error as exc:
                self._warn("process", exc)
                return None
        out: dict[int, float] = {}
        for inst, val in (arr or {}).items():
            m = _PDH_PID_RE.search(inst)
            if m:
                pid = int(m.group(1))
                out[pid] = out.get(pid, 0.0) + max(0.0, float(val)) / _MB
        return out

    def close(self) -> None:
        with self._lock:
            for attr in ("_q_adapter", "_q_proc"):
                q = getattr(self, attr)
                if q is not None:
                    try:
                        win32pdh.CloseQuery(q)
                    except pywintypes.error as exc:
                        log.debug("PDH CloseQuery failed: %s", exc)
                    setattr(self, attr, None)
            self._c_adapter = self._c_proc = None
            self._closed = True                    # a straggling preview worker must not reopen the queries


# --------------------------------------------------------------------------------------------- detector

class ProcessDetector:
    def __init__(self, config: dict | None = None,
                 protected_pids_provider: Callable[[], set[int]] | None = None,
                 engine: object | None = None) -> None:
        self.config = config if config is not None else config_store.load_config()
        self._engine = engine                      # engines.Engine or None (None = built-in Strata rule)
        self._provider = protected_pids_provider
        self._lock = threading.Lock()
        self._pdh = _PdhGpu()
        self._self_pid = os.getpid()
        self._session = session_id_of(self._self_pid)

    # ---- internals --------------------------------------------------------------------------------

    def _provider_pids(self) -> set[int]:
        if self._provider is None:
            return set()
        try:
            return {int(p) for p in (self._provider() or ())}
        except Exception as exc:  # contract: provider exceptions are swallowed
            log.warning("protected_pids_provider failed: %s", exc)
            return set()

    def _whitelist_norms(self) -> set[str]:
        return {norm(n) for n in (self.config.get("user_whitelist") or []) if isinstance(n, str)}

    def _session_ok_factory(self) -> Callable[[int], bool]:
        cache: dict[int, bool] = {}
        ours = self._session

        def ok(pid: int) -> bool:
            if pid not in cache:
                sid = session_id_of(pid)
                cache[pid] = ours is not None and sid is not None and sid == ours
            return cache[pid]
        return ok

    def _engine_protects(self) -> Callable[[ProcInfo], bool] | None:
        eng, cfg = self._engine, self.config
        fn = getattr(eng, "protects_process", None)
        if fn is None:
            return None
        return lambda info: bool(fn(info.cmdline, info.exe, cfg))

    def _vram_snapshot(self) -> tuple[dict[int, float] | None, float | None]:
        cap = self._pdh.adapter_used_mb()
        per = self._pdh.process_used_mb()
        if per is None or cap is None:
            return None, cap
        return {pid: min(v, cap) for pid, v in per.items()}, cap

    def _build(self, ignore_user_whitelist: bool) -> tuple[list[App], dict[int, ProcInfo]]:
        t0 = time.perf_counter()
        procs = collect_processes()
        t1 = time.perf_counter()
        windows = enum_real_windows()
        vram, cap = self._vram_snapshot()
        t2 = time.perf_counter()
        tree_prot = TREE_PROTECTED if ignore_user_whitelist else TREE_PROTECTED | self._whitelist_norms()
        ctx = GraphContext(
            self_pid=self._self_pid,
            extra_hard_pids=self._provider_pids(),
            tree_protected=frozenset(tree_prot),
            system_protected=SYSTEM_PROTECTED,
            strata_dir=str(self.config.get("strata_dir") or ""),
            system_root=os.environ.get("SystemRoot") or os.environ.get("windir") or "",
            game_dirs=list(self.config.get("game_dirs") or []),
            min_ram_mb=float(self.config.get("min_ram_mb", 250)),
            min_vram_mb=float(self.config.get("min_vram_mb", 200)),
            min_game_ram_mb=float(self.config.get("min_game_ram_mb", 50)),
            game_exclude=frozenset(norm(n) for n in (self.config.get("game_exclude") or []) if n),
            windows=windows, vram_mb=vram, vram_cap_mb=cap,
            session_ok=self._session_ok_factory(),
            engine_protects=self._engine_protects(),
        )
        apps = build_apps(procs, ctx)
        t3 = time.perf_counter()
        log.debug("scan: %d procs, %d apps; iter %.3fs, win+pdh %.3fs, graph %.3fs",
                  len(procs), len(apps), t1 - t0, t2 - t1, t3 - t2)
        return apps, procs

    def _scan_apps(self) -> list[App]:
        """Test seam: the ONLY source of sweep/scan candidates. Caller holds self._lock."""
        apps, _procs = self._build(ignore_user_whitelist=False)
        return apps

    def _locked_scan(self) -> list[App]:
        with self._lock:
            return list(self._scan_apps())

    # ---- public API -------------------------------------------------------------------------------

    def scan_killable_processes(self) -> list[dict]:
        try:
            apps = self._locked_scan()
        except Exception:
            log.exception("scan_killable_processes failed")
            return []
        apps = sorted(apps, key=lambda a: (-a.ram_mb, a.name.casefold(), a.pid))
        return [a.to_dict() for a in apps]

    def sweep_processes(self, dry_run: bool = False,
                        targets: list[tuple[int, float]] | None = None) -> dict:
        result: dict = {"closed_apps": [], "ram_freed_mb": 0.0, "vram_freed_mb": None, "failed": [],
                        "dry_run": bool(dry_run), "candidates": 0, "preselected": 0,
                        "preselected_ram_mb": 0.0, "preselected_vram_mb": None, "names": [],
                        "preselected_names": [], "launch_records": []}
        try:
            apps = sorted(self._locked_scan(), key=lambda a: (-a.ram_mb, a.name.casefold(), a.pid))
            pre = [a for a in apps if a.preselect]
            result["candidates"] = len(apps)
            result["preselected"] = len(pre)
            result["preselected_ram_mb"] = round(float(sum(a.ram_mb for a in pre)), 1)
            result["preselected_vram_mb"] = _sum_or_none(a.vram_mb for a in pre)
            result["preselected_names"] = [a.name for a in pre]
            if targets is None and not dry_run:
                # a forgotten argument must never become "close everything"
                log.error("sweep_processes(dry_run=False) called without targets; refusing")
                result["failed"].append({"name": "*", "pid": 0,
                                         "error": "real sweep requires explicit targets"})
                return result
            if targets is None:
                selected = list(apps)
            else:
                tlist = []
                for t in targets:
                    try:
                        tlist.append((int(t[0]), float(t[1])))
                    except (TypeError, ValueError, IndexError):
                        log.warning("ignoring malformed sweep target %r", t)
                selected = [a for a in apps
                            if any(a.pid == tp and abs(a.create_time - tc) < CREATE_TIME_TOLERANCE
                                   for tp, tc in tlist)]
            result["names"] = [a.name for a in selected]
            if dry_run:
                result["closed_apps"] = [a.name for a in selected]
                result["ram_freed_mb"] = round(float(sum(a.ram_mb for a in selected)), 1)
                result["vram_freed_mb"] = _sum_or_none(a.vram_mb for a in selected)
                return result
            if not selected:
                result["vram_freed_mb"] = 0.0
                return result
            self._kill_apps(selected, result)
        except Exception as exc:
            log.exception("sweep_processes failed")
            result["failed"].append({"name": "sweep", "pid": 0, "error": f"{type(exc).__name__}: {exc}"})
        return result

    def _kill_apps(self, selected: list[App], result: dict) -> None:
        """Real sweep (no lock held): WM_CLOSE -> terminate -> kill, only captured Process objects.

        Launch records (for reopen.py) are captured BEFORE anything is closed, because the processes are
        gone afterwards; result['launch_records'] then keeps only the apps that really closed.
        """
        failed: dict[int, dict] = {}
        records: dict[int, dict] = {}
        for app in selected:
            rec = capture_launch_record(app)       # never raises
            if rec is not None:
                records[id(app)] = rec
        before = self.gpu_dedicated_used_mb()
        for app in selected:
            for h in app.hwnds:
                try:
                    win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
                except pywintypes.error as exc:
                    log.debug("WM_CLOSE to %s (%s) failed: %s", h, app.name, exc)
        all_members = _unique_procs(m for a in selected for m in a.members)
        roots = _unique_procs(a.root for a in selected if a.root is not None)
        if any(a.hwnds for a in selected):
            _wait(all_members, 3.0)
        name_of = {m.pid: a.name for a in selected for m in a.members}

        def act(procs: list[psutil.Process], how: str) -> None:
            for p in procs:
                try:
                    if p.is_running():
                        getattr(p, how)()
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied as exc:
                    failed[p.pid] = {"name": name_of.get(p.pid, ""), "pid": p.pid,
                                     "error": f"access denied ({how}): {exc}"}
                except psutil.Error as exc:
                    failed[p.pid] = {"name": name_of.get(p.pid, ""), "pid": p.pid,
                                     "error": f"{how} failed: {exc}"}

        alive = _alive(all_members)
        if alive:
            root_ids = {id(r) for r in roots}
            act([p for p in alive if id(p) in root_ids], "terminate")
            act([p for p in alive if id(p) not in root_ids], "terminate")
            _wait(alive, 3.0)
            still = _alive(alive)
            if still:
                act(still, "kill")
                _wait(still, 2.0)
        closed, freed, closed_records = [], 0.0, []
        for app in selected:
            gone = app.root is None or not _running(app.root)
            if gone:
                closed.append(app.name)
                freed += app.ram_mb
                if id(app) in records:
                    closed_records.append(records[id(app)])
            elif app.pid not in failed:
                failed[app.pid] = {"name": app.name, "pid": app.pid, "error": "still running after kill"}
        result["closed_apps"] = closed
        result["launch_records"] = closed_records
        result["ram_freed_mb"] = round(float(freed), 1)
        result["failed"] = list(failed.values())
        time.sleep(1.0)
        after = self.gpu_dedicated_used_mb()
        result["vram_freed_mb"] = (None if before is None or after is None
                                   else round(max(0.0, before - after), 1))
        log.info("sweep closed %s, freed %.1f MB RAM, failed %d", closed, freed, len(result["failed"]))

    def get_whitelist(self) -> list[str]:
        return list(self.config.get("user_whitelist") or [])

    def update_whitelist(self, apps: list[str]) -> None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for a in apps or []:
            if not isinstance(a, str):
                continue
            s = a.strip()
            if not s or not s.casefold().endswith(".exe") or len(s) <= 4:
                continue
            k = norm(s)
            if k in seen:
                continue
            seen.add(k)
            cleaned.append(s)
        cleaned.sort(key=str.casefold)
        with self._lock:
            disk = config_store.load_config()
            disk["user_whitelist"] = cleaned
            config_store.save_config(disk)
            self.config["user_whitelist"] = list(cleaned)
        log.info("user_whitelist saved (%d entries)", len(cleaned))

    def list_whitelist_candidates(self) -> list[dict]:
        try:
            with self._lock:
                apps, procs = self._build(ignore_user_whitelist=True)
        except Exception:
            log.exception("list_whitelist_candidates scan failed")
            apps, procs = [], {}
        running = {i.nname for i in procs.values()}
        wl = self.get_whitelist()
        wl_norms = {norm(n) for n in wl}
        out: dict[str, dict] = {}
        for n in list(wl) + [a.name for a in apps]:
            k = norm(n)
            if not k or k in out:
                continue
            out[k] = {"name": n, "whitelisted": k in wl_norms, "running": k in running}
        return sorted(out.values(), key=lambda d: d["name"].casefold())

    def gpu_dedicated_used_mb(self) -> float | None:
        try:
            v = self._pdh.adapter_used_mb()
        except Exception:
            log.exception("gpu_dedicated_used_mb failed")
            return None
        return None if v is None else round(v, 1)

    def close(self) -> None:
        self._pdh.close()


# --------------------------------------------------------------------------------------------- sweep utils

def _sum_or_none(vals: Iterable[float | None]) -> float | None:
    total = 0.0
    for v in vals:
        if v is None:
            return None
        total += v
    return round(total, 1)


def _unique_procs(procs: Iterable[psutil.Process]) -> list[psutil.Process]:
    seen, out = set(), []
    for p in procs:
        if id(p) not in seen:
            seen.add(id(p))
            out.append(p)
    return out


def _running(p: psutil.Process) -> bool:
    try:
        return p.is_running()
    except psutil.Error:
        return False


def _alive(procs: list[psutil.Process]) -> list[psutil.Process]:
    return [p for p in procs if _running(p)]


def _wait(procs: list[psutil.Process], timeout: float) -> None:
    live = _alive(procs)
    if not live:
        return
    try:
        psutil.wait_procs(live, timeout=timeout)
    except psutil.Error as exc:
        log.debug("wait_procs: %s", exc)


# --------------------------------------------------------------------------------------------- launch records
# Captured by _kill_apps BEFORE closing an app so reopen.py can start it again when Strata turns off.
# Everything here is best effort: any failure yields a partial record (or None), never an exception.

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION_CLASS = 20                        # TOKEN_INFORMATION_CLASS.TokenElevation
_AUMID_MAX = 130                                   # APPLICATION_USER_MODEL_ID_MAX_LENGTH
_ERROR_INSUFFICIENT_BUFFER = 122

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
try:
    _GetAumid = _kernel32.GetApplicationUserModelId          # Windows 8+
    _GetAumid.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32), wintypes.LPWSTR]
    _GetAumid.restype = ctypes.c_long
except AttributeError:
    _GetAumid = None
try:
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
except (OSError, AttributeError):
    _advapi32 = None


def _token_elevated(hproc: int | None) -> bool:
    """True if the process token is elevated. Anything we cannot read counts as elevated (safe side:
    an elevated app cannot be relaunched from a non-elevated controller anyway)."""
    if not hproc or _advapi32 is None:
        return True
    htok = wintypes.HANDLE()
    try:
        if not _advapi32.OpenProcessToken(hproc, _TOKEN_QUERY, ctypes.byref(htok)):
            return True                            # typically ERROR_ACCESS_DENIED -> elevated / protected
        try:
            val = wintypes.DWORD(0)
            ret = wintypes.DWORD(0)
            if not _advapi32.GetTokenInformation(htok, _TOKEN_ELEVATION_CLASS, ctypes.byref(val),
                                                 ctypes.sizeof(val), ctypes.byref(ret)):
                return True
            return bool(val.value)
        finally:
            _kernel32.CloseHandle(htok)
    except (OSError, ctypes.ArgumentError):
        return True


def _process_aumid(hproc: int | None) -> str | None:
    """AppUserModelID of a packaged (Store/MSIX) process; None when not packaged or on any error."""
    if not hproc or _GetAumid is None:
        return None
    try:
        size = ctypes.c_uint32(_AUMID_MAX + 1)
        buf = ctypes.create_unicode_buffer(size.value)
        rc = _GetAumid(hproc, ctypes.byref(size), buf)
        if rc == _ERROR_INSUFFICIENT_BUFFER and 0 < size.value <= 4096:
            buf = ctypes.create_unicode_buffer(size.value)
            rc = _GetAumid(hproc, ctypes.byref(size), buf)
        if rc != 0:                                # 15703 APPMODEL_ERROR_NO_APPLICATION_MODEL_ID = unpackaged
            return None
        return buf.value or None
    except (OSError, ctypes.ArgumentError, ValueError):
        return None


def _group_root(app: App) -> psutil.Process | None:
    """The app's root process: App.root, else the member whose parent is not itself a member."""
    if app.root is not None:
        return app.root
    pids = {m.pid for m in app.members}
    for m in app.members:
        try:
            if m.ppid() not in pids:
                return m
        except psutil.Error:
            continue
    return app.members[0] if app.members else None


def _safe_call(fn: Callable[[], object], default: object) -> object:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - psutil.Error, OSError, anything: record capture must never raise
        return default


def capture_launch_record(app: App) -> dict | None:
    """Describe how to start `app` again (its group ROOT process) while it is still running.

    Returns {name, exe, cmdline, cwd, category, create_time, elevated, aumid} or None when the root is
    already gone / unknown. Never raises; costs a few ms (psutil exe/cmdline/cwd + one OpenProcess).
    """
    t0 = time.perf_counter()
    try:
        root = _group_root(app)
        if root is None or not _running(root):
            return None
        hproc = None
        try:
            hproc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(root.pid))
        except (OSError, ctypes.ArgumentError):
            hproc = None
        try:
            # holding the handle pins the pid; is_running() (pid + create_time) proves it is still our process
            if not _running(root):
                return None
            exe = _safe_call(root.exe, None) or app.exe or ""
            cmdline = _safe_call(root.cmdline, None)
            cwd = _safe_call(root.cwd, None)
            ctime = _safe_call(root.create_time, None)
            rec = {
                "name": str(app.name or _safe_call(root.name, "") or ""),
                "exe": os.path.abspath(exe) if exe else "",
                "cmdline": [str(c) for c in cmdline] if isinstance(cmdline, list) else [],
                "cwd": str(cwd) if cwd else "",
                "category": str(app.category),
                "create_time": float(ctime if ctime is not None else app.create_time),
                "elevated": _token_elevated(hproc),
                "aumid": _process_aumid(hproc),
            }
        finally:
            if hproc:
                _kernel32.CloseHandle(hproc)
        log.debug("launch record for %s captured in %.1f ms", rec["name"], (time.perf_counter() - t0) * 1000)
        return rec
    except Exception:  # noqa: BLE001
        log.exception("capture_launch_record failed for %s", getattr(app, "name", "?"))
        return None
