r"""Find Strata install folders on this PC: a bounded, read-only, breadth-first directory walk.

A directory qualifies as a Strata install when it contains serve\server.py AND at least one run-*.bat that
launcher_bat.parse_bat(safe=True) accepts, whose serve\server.py is this very directory's, and for which
launcher_bat.missing_files(safe=True) is None (python.exe, server.py and the --config file all exist).

Safety / cost rules:
  * Only os.scandir() listings are used to walk; the only files ever read are the candidate run-*.bat files (and
    their --config .json, via launcher_bat), and only in directories that already contain serve\server.py.
  * The .bat files come from wherever the user unpacked something, so they are parsed in launcher_bat's safe mode:
    a launcher naming a UNC / device / non-fixed-drive path (interpreter, cwd, --config, any argv path) is rejected
    before anything is touched (no outbound SMB), and --config must be a local .json of at most 1 MB.
  * Roots that are not on a local fixed drive (UNC, network-redirected folders, mapped drives) are skipped with a
    string check + GetDriveTypeW only.
  * Never follows symlinks, junctions or other name-surrogate reparse points; skips hidden/system directories,
    cloud-only placeholders (OneDrive "online-only": RECALL_ON_OPEN / RECALL_ON_DATA_ACCESS / OFFLINE, which would
    be downloaded when listed or read) and a fixed list of noisy folders (Windows, Program Files, AppData, ...).
  * All roots share the walk fairly: every root's depth 0 is listed first, then the next listing always goes to the
    root that has used the least walk time so far (BFS within each root). A big or slow first root (home, OneDrive)
    therefore cannot starve a later root such as a drive.
  * Bounded by depth (max_depth below each root; drive roots at most 3), by an overall time budget and by an
    optional threading.Event (also checked before every .bat). When a bound is hit the folders found so far are
    returned. The walk - including default_roots() when roots is None - runs on a private daemon thread, so a
    single blocking call cannot keep the caller past the budget (+0.25 s).
  * Never raises; problems are logged at DEBUG on the 'agent_start' logger. No Tk: safe on a worker thread.
"""
from __future__ import annotations

import ctypes
import logging
import os
import string
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import launcher_bat

log = logging.getLogger("agent_start")

SKIP_NAMES = frozenset(n.casefold() for n in (
    "Windows", "Program Files", "Program Files (x86)", "ProgramData", "AppData", "$Recycle.Bin",
    "System Volume Information", "Recovery", "node_modules", ".git", ".venv", "venv", "site-packages",
    "__pycache__", ".cache"))
DRIVE_ROOT_MAX_DEPTH = 3

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ONLY = _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_NAME_SURROGATE_BIT = 0x20000000         # IsReparseTagNameSurrogate: symlinks, junctions/mount points, ...
_DRIVE_FIXED = 3

# KNOWNFOLDERID GUIDs and CSIDL fallbacks
_FOLDERS = (
    ("Downloads", "{374DE290-123F-4565-9164-39C4925E467B}", None),
    ("Desktop", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", 0x0010),     # CSIDL_DESKTOPDIRECTORY
    ("Documents", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", 0x0005),   # CSIDL_PERSONAL
)


@dataclass
class Found:
    strata_dir: str
    launchers: list[str] = field(default_factory=list)   # usable run-*.bat paths, sorted by name
    newest_mtime: float = 0.0                             # max mtime of the launchers


# ------------------------------------------------------------------------------------------- roots
def _known_folder(guid: str) -> str | None:
    try:
        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                        ("Data4", ctypes.c_ubyte * 8)]
        g = GUID()
        if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(g)) != 0:
            return None
        buf = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(buf))
        try:
            return buf.value if hr == 0 and buf.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(buf)
    except Exception as exc:                      # non-Windows, missing API, ...
        log.debug("strata_finder: SHGetKnownFolderPath %s failed: %s", guid, exc)
        return None


def _csidl_folder(csidl: int) -> str | None:
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception as exc:
        log.debug("strata_finder: SHGetFolderPathW %#x failed: %s", csidl, exc)
    return None


def _fixed_drives() -> list[str]:
    out: list[str] = []
    try:
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == _DRIVE_FIXED:
                    out.append(root)
    except Exception as exc:
        log.debug("strata_finder: drive enumeration failed: %s", exc)
    return out


def _key(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(path))


def _local_root(path: str) -> bool:
    """May `path` be walked? Absolute, on a local fixed drive (string check + GetDriveTypeW: the path itself is not
    touched), and - if it is itself a symlink - pointing at a local fixed-drive path (read with lstat/readlink only,
    the target is never opened). Junctions can only point at local volumes."""
    try:
        p = os.path.abspath(path)
        if not launcher_bat.is_local_fixed_path(p):
            return False
        st = os.lstat(p)
        if getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_SYMLINK or os.path.islink(p):
            target = os.readlink(p)
            if target.startswith("\\\\?\\") and target[4:5].isalpha() and target[5:6] == ":":
                target = target[4:]                 # \\?\C:\x -> C:\x (a local long-path prefix)
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(p), target)
            return launcher_bat.is_local_fixed_path(os.path.normpath(target))
        return True
    except (OSError, ValueError):
        return False


def default_roots() -> list[str]:
    """Downloads, Desktop, Documents (real locations, incl. OneDrive redirection), home, every fixed drive root.

    Deduplicated by normcase(realpath), existing directories on local fixed drives only, in that order: a folder
    redirected to a network share (UNC or mapped drive) is dropped without being touched. Never raises.
    """
    home = os.path.expanduser("~")
    cands: list[str] = []
    try:
        for name, guid, csidl in _FOLDERS:
            p = _known_folder(guid) or (_csidl_folder(csidl) if csidl is not None else None)
            cands.append(p or os.path.join(home, name))
        cands.append(home)
        cands.extend(_fixed_drives())
    except Exception as exc:
        log.debug("strata_finder: default_roots failed: %s", exc)
    out: list[str] = []
    seen: set[str] = set()
    for p in cands:
        try:
            if not p or not _local_root(p):
                log.debug("strata_finder: root %r skipped (not on a local fixed drive)", p)
                continue
            if not os.path.isdir(p):
                continue
            k = _key(p)
            if k not in seen:
                seen.add(k)
                out.append(os.path.normpath(p) if not _is_drive_root(p) else p)
        except Exception:
            continue
    return out


def _is_drive_root(path: str) -> bool:
    drive, tail = os.path.splitdrive(os.path.abspath(path))
    return bool(drive) and tail in ("\\", "/", "")


# ------------------------------------------------------------------------------------------- walk
def _skip_entry(entry: os.DirEntry) -> bool:
    """True for anything that must not be entered: non-dirs, skip-list names, hidden/system, link-like reparse."""
    if entry.name.casefold() in SKIP_NAMES:
        return True
    try:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            return True
        if hasattr(entry, "is_junction") and entry.is_junction():
            return True
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    attrs = _attrs(st, entry.path)
    if attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM | _CLOUD_ONLY):
        return True                                 # hidden/system, or a cloud-only folder that listing would fetch
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(st, "st_reparse_tag", 0)
        # Hydrated cloud placeholders (OneDrive) are not name surrogates and are walked; anything that redirects is not.
        if not tag or tag & _NAME_SURROGATE_BIT:
            return True
    return False


def _attrs(st, path: str) -> int:
    """Windows file attributes of a stat result (0 elsewhere). `path` is unused: a seam for tests."""
    return getattr(st, "st_file_attributes", 0) or 0


def _cloud_only(entry: os.DirEntry) -> bool:
    """True for an online-only (not downloaded) file/folder; uses the cached scandir data, no extra I/O."""
    try:
        return bool(_attrs(entry.stat(follow_symlinks=False), entry.path) & _CLOUD_ONLY)
    except OSError:
        return True


def _usable_launchers(dirpath: str, bat_names: list[str], out_of_time=None) -> list[tuple[str, float]]:
    """(bat path, mtime) for each run-*.bat in dirpath that parses, runs dirpath's own server.py, has no missing files.

    Parsing uses launcher_bat's safe mode. out_of_time() (optional) is checked before every .bat."""
    out: list[tuple[str, float]] = []
    here = os.path.normcase(os.path.normpath(dirpath))
    for name in bat_names:
        if out_of_time is not None and out_of_time():
            log.debug("strata_finder: budget/stop reached while checking %s", dirpath)
            break
        path = os.path.join(dirpath, name)
        try:
            info = launcher_bat.parse_bat(path, safe=True)
            if os.path.normcase(os.path.normpath(info.strata_dir)) != here:
                log.debug("strata_finder: %s launches another folder's server (%s)", path, info.strata_dir)
                continue
            miss = launcher_bat.missing_files(info, safe=True)
            if miss:
                log.debug("strata_finder: %s unusable: %s", path, miss)
                continue
            out.append((os.path.normpath(path), os.path.getmtime(path)))
        except launcher_bat.LauncherError as exc:
            log.debug("strata_finder: %s rejected: %s", path, exc)
        except Exception as exc:
            log.debug("strata_finder: %s check failed: %s", path, exc)
    return out


_GRACE_S = 0.25          # how long find_strata waits past the budget for the walker to notice it


def find_strata(roots: list[str] | None = None, time_budget_s: float = 8.0, max_depth: int = 4,
                stop_evt: threading.Event | None = None) -> list[Found]:
    """Strata installs under `roots` (default_roots() when None), newest launcher first. Never raises.

    The walk runs on a private daemon thread so that one slow call (default_roots() on a redirected/offline folder,
    a OneDrive listing, a sleeping disk) cannot hold the caller past time_budget_s: when the budget (+0.25 s grace)
    is spent or stop_evt is set, the folders found so far are returned and the walker exits after its current step.
    Pass roots=None (not default_roots()) so that default_roots() runs on that thread too.
    """
    try:
        budget = max(0.0, float(time_budget_s))
    except (TypeError, ValueError):
        budget = 8.0
    deadline = time.monotonic() + budget
    results: dict[str, Found] = {}
    lock = threading.Lock()
    abort = threading.Event()

    def run() -> None:
        try:
            _walk(roots, deadline, max_depth, stop_evt, abort, results, lock)
        except Exception as exc:                    # belt and braces: never raise
            log.debug("strata_finder: walk failed: %s", exc)

    try:
        worker = threading.Thread(target=run, name="strata-finder", daemon=True)
        worker.start()
        hard_stop = deadline + _GRACE_S
        while worker.is_alive():
            if stop_evt is not None and stop_evt.is_set():
                break
            left = hard_stop - time.monotonic()
            if left <= 0:
                log.debug("strata_finder: time budget spent; returning partial results")
                break
            worker.join(min(0.05, left))
    except Exception as exc:
        log.debug("strata_finder: worker failed: %s", exc)
    finally:
        abort.set()
    with lock:
        found = list(results.values())
    return sorted(found, key=lambda f: (-f.newest_mtime, f.strata_dir.casefold()))


def _walk(roots, deadline: float, max_depth: int, stop_evt, abort: threading.Event, results: dict[str, Found],
          lock: threading.Lock) -> None:
    def out_of_time() -> bool:
        return (time.monotonic() >= deadline or abort.is_set()
                or (stop_evt is not None and stop_evt.is_set()))

    if roots is None:
        roots = default_roots()                    # on the walker thread: may block on a slow known folder
    visited: set[str] = set()                      # normcase paths already listed (roots overlap: home ⊂ C:\)
    # One BFS queue per root, (path, depth). All depth-0 entries go first; after that the next listing goes to the
    # root that has spent the least walk time so far (ties: root order), so no root can starve the others.
    queues: list[deque[tuple[str, int]]] = []
    limits: list[int] = []
    spent: list[float] = []
    seeded: set[str] = set()
    for root in roots:
        if out_of_time():
            break
        try:
            if not root or not _local_root(root) or not os.path.isdir(root):
                log.debug("strata_finder: root %r skipped", root)
                continue
            drive_root = _is_drive_root(root)
            start = os.path.abspath(root) if drive_root else os.path.normpath(os.path.abspath(root))
            key = os.path.normcase(os.path.normpath(start))
        except Exception:
            continue
        if key in seeded:
            continue                               # duplicate root
        seeded.add(key)
        queues.append(deque([(start, 0)]))
        limits.append(min(max_depth, DRIVE_ROOT_MAX_DEPTH) if drive_root else max_depth)
        spent.append(0.0)

    while True:
        if out_of_time():
            log.debug("strata_finder: budget/stop reached")
            return
        live = [i for i, q in enumerate(queues) if q]
        if not live:
            return
        i = min(live, key=lambda j: (queues[j][0][1] > 0, spent[j], j))
        t0 = time.perf_counter()
        try:
            path, depth = queues[i].popleft()
            for sub in _list_dir(path, depth, limits[i], visited, out_of_time, results, lock, abort):
                queues[i].append((sub, depth + 1))
        finally:
            spent[i] += time.perf_counter() - t0


def _list_dir(path: str, depth: int, limit: int, visited: set[str], out_of_time, results: dict[str, Found],
              lock: threading.Lock, abort: threading.Event) -> list[str]:
    """List one directory: record it when it is a Strata install, else return its walkable subdirectories."""
    key = os.path.normcase(os.path.normpath(path))
    if key in visited:
        return []
    visited.add(key)
    subdirs: list[str] = []
    bats: list[str] = []
    has_serve = False
    try:
        with os.scandir(path) as it:
            for n, entry in enumerate(it):
                if n & 255 == 255 and out_of_time():
                    return []
                name = entry.name
                low = name.casefold()
                try:
                    if low.startswith("run-") and low.endswith(".bat") and entry.is_file(follow_symlinks=False):
                        if not _cloud_only(entry):  # an online-only .bat would be downloaded when read
                            bats.append(name)
                        continue
                except OSError:
                    continue
                if low == "serve":
                    has_serve = True
                if depth < limit and not _skip_entry(entry):
                    subdirs.append(entry.path)
    except (PermissionError, OSError) as exc:
        log.debug("strata_finder: cannot list %s: %s", path, exc)
        return []
    except Exception as exc:
        log.debug("strata_finder: error listing %s: %s", path, exc)
        return []
    if has_serve and bats and not out_of_time() and os.path.isfile(os.path.join(path, "serve", "server.py")):
        usable = _usable_launchers(path, bats, out_of_time)
        if usable:
            k = _key(path)
            usable.sort(key=lambda t: os.path.basename(t[0]).casefold())
            with lock:
                if k not in results and not abort.is_set():
                    results[k] = Found(strata_dir=os.path.normpath(path), launchers=[p for p, _ in usable],
                                       newest_mtime=max(m for _, m in usable))
                    log.debug("strata_finder: found %s (%d launchers)", path, len(usable))
            return []                               # never descend into a qualifying folder
    subdirs.sort(key=str.casefold)
    return subdirs


def best_launcher(found: Found) -> str:
    """The launcher to preselect: the most recently modified .bat (the model the user set up/used last).

    Ties (and unreadable mtimes, treated as 0) are broken by name, case-insensitively A→Z. '' if none.
    """
    def mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    if not found.launchers:
        return ""
    return min(found.launchers, key=lambda p: (-mtime(p), os.path.basename(p).casefold()))
