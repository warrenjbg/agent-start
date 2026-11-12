r"""Find llama.cpp on this PC: llama-server.exe builds and .gguf model files. Bounded, read-only, never raises.

What counts:
  * A build is a llama-server.exe (a real file, not a link) that sits next to llama.dll or ggml.dll, so a random file
    with that name is not offered. Its backend is read from the ggml backend DLL beside it (ggml-cuda.dll -> 'cuda',
    ggml-hip / ggml-sycl / ggml-vulkan / ggml-opencl likewise, none -> 'cpu'); its build number ('b12314') from the
    folder name, the parent folder name or a .zip name in the folder. The exe is never run.
  * A model is a *.gguf of at least MIN_MODEL_BYTES whose first 4 bytes are b'GGUF' - except vision projectors
    ('mmproj' in the name), split parts other than the first ('-00002-of-00005.gguf'; the first part IS the model and
    its size is the sum of the parts beside it) and 'ggml-vocab*' files. Reading those 4 bytes is the only time a file
    is opened.

Where it looks (in this order; the first place a build is seen sets Build.source):
  builds: the winget package folder(s) (%LOCALAPPDATA%\Microsoft\WinGet\Packages\ggml.llamacpp_*, also under
          %ProgramFiles%\WinGet\Packages), every folder on PATH (listed only, not walked), then the roots.
  models: extra_dirs (folders the user already used), llama.cpp's download cache (%LLAMA_CACHE% or
          %LOCALAPPDATA%\llama.cpp), the Hugging Face hub cache (%HF_HUB_CACHE%, %HF_HOME%\hub or
          %USERPROFILE%\.cache\huggingface\hub), LM Studio (%USERPROFILE%\.lmstudio\models and
          %USERPROFILE%\.cache\lm-studio\models), then the roots.
  roots:  strata_finder.default_roots() when None (Downloads, Desktop, Documents, home, fixed drive roots; local
          fixed drives only), walked breadth-first to max_depth (drive roots at most 3).

Safety / cost rules (as strata_finder):
  * Directory walking uses os.scandir() only. Never follows symlinks, junctions or other name-surrogate reparse
    points; skips hidden/system and cloud-only (online-only OneDrive) entries, and noisy trees: Windows, ProgramData,
    AppData, $Recycle.Bin, node_modules, .git, venvs, site-packages, caches, .ollama (its blobs have no .gguf
    names), .lmstudio (LM Studio's private runtimes; its models folder is an explicit root), Strata-data (Strata's
    own GGUF files), Steam libraries. 'Program Files' is entered only to reach direct children whose name contains
    'llama'.
  * Inside the Hugging Face cache, snapshots\<hash>\*.gguf are file symlinks into blobs\: there (only there) a file
    link is resolved; the real file must be on a local fixed drive and is reported once (deduplicated by real path;
    Model.path keeps the link's .gguf name).
  * Roots off a local fixed drive (UNC, mapped or network-redirected folders) are dropped by a string check +
    GetDriveTypeW, without being touched (strata_finder._local_root).
  * Bounded by depth, by a number of directories listed (max_dirs), by a time budget and by an optional
    threading.Event. All roots share the walk fairly (the next listing goes to the root that used the least time).
    The walk runs on a private daemon thread, so one blocking call cannot hold the caller past the budget (+0.25 s);
    what was found so far is returned.
  * Never raises; problems are logged at DEBUG on the 'agent_start' logger. No Tk: safe on a worker thread.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import launcher_bat
import strata_finder

log = logging.getLogger("agent_start")

SERVER_EXE = "llama-server.exe"
CORE_DLLS = frozenset({"llama.dll", "ggml.dll"})
# label preference when a folder holds several backend DLLs
BACKENDS = ("cuda", "hip", "sycl", "vulkan", "opencl")
_BACKEND_RANK_NVIDIA = ("cuda", "vulkan", "opencl", "sycl", "hip", "cpu")
_BACKEND_RANK_OTHER = ("vulkan", "hip", "sycl", "opencl", "cuda", "cpu")
_BACKEND_NAMES = {"cuda": "CUDA", "hip": "HIP", "sycl": "SYCL", "vulkan": "Vulkan", "opencl": "OpenCL", "cpu": "CPU"}
_SOURCE_RANK = {"folder": 0, "winget": 1, "path": 2}

MIN_MODEL_BYTES = 100 * 1024 * 1024
GGUF_MAGIC = b"GGUF"
DEFAULT_BUDGET_S = 8.0
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_DIRS = 20000
DRIVE_ROOT_MAX_DEPTH = strata_finder.DRIVE_ROOT_MAX_DEPTH
_EXPLICIT_DEPTH = 6          # winget / model caches: HF is hub\models--o--r\snapshots\<hash>\x.gguf
_EXTRA_DEPTH = 2             # extra_dirs: the folder, its subfolders and theirs
_GRACE_S = 0.25

# .ollama: blobs without .gguf names. .lmstudio: LM Studio's private llama.cpp runtimes (extensionsackends, versioned
# and replaced by LM Studio); its models folder is searched as an explicit root. Strata-data: Strata's own GGUF files
# (custom 'qwen4exp' architecture and MTP heads) that only the Strata engine runs.
SKIP_NAMES = strata_finder.SKIP_NAMES | frozenset(n.casefold() for n in (
    ".ollama", ".lmstudio", "Strata-data", "steamapps", "SteamLibrary", "Steam", ".svn", ".hg", ".tox", ".mypy_cache",
    ".ruff_cache"))
_PROGRAM_FILES = frozenset({"program files", "program files (x86)"})
_WINGET_PREFIX = "ggml.llamacpp_"

_BUILD_RE = re.compile(r"(?<![a-z0-9])b(\d{3,6})(?![0-9])", re.IGNORECASE)
_SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})$", re.IGNORECASE)

_MODEL_VALUE_FLAGS = frozenset({"-m", "--model"})
# options that name the model some other way; they are dropped (with their value) when a model file is set
_MODEL_SOURCE_FLAGS = frozenset({"-hf", "-hfr", "--hf-repo", "-hff", "--hf-file", "-mu", "--model-url", "-dr",
                                 "--docker-repo"})


@dataclass(frozen=True)
class Build:
    program: str                 # absolute path of llama-server.exe
    folder: str                  # its folder
    backend: str                 # 'cuda' | 'hip' | 'sycl' | 'vulkan' | 'opencl' | 'cpu' (the preferred one)
    backends: tuple[str, ...]    # every backend DLL found, preferred first; ('cpu',) when none
    build: str | None            # 'b12314' or None when unknown
    source: str                  # 'winget' | 'path' | 'folder'


@dataclass(frozen=True)
class Model:
    path: str                    # absolute path of the .gguf (the first part of a split model)
    name: str                    # file name without .gguf and without the split suffix
    size_bytes: int              # the file, or the sum of the split parts found beside it
    folder: str


@dataclass(frozen=True)
class Choice:
    build: Build | None
    model: Model | None
    clear: bool                  # True only when a build AND exactly one model were found
    why: str


# ------------------------------------------------------------------------------------------------ small helpers
def _real_key(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(path))


def _env_get(env: Mapping[str, str] | None, name: str) -> str:
    try:
        if env is None:
            return ""
        v = env.get(name)
        if v is None:              # Windows env names are case-insensitive; a plain dict is not
            low = name.casefold()
            v = next((val for k, val in env.items() if str(k).casefold() == low), None)
        return str(v).strip().strip('"') if v else ""
    except Exception:
        return ""


def _build_number(text: str) -> int | None:
    nums = [int(m.group(1)) for m in _BUILD_RE.finditer(text or "")]
    return max(nums) if nums else None


def _build_int(b: Build) -> int:
    try:
        return int(str(b.build)[1:]) if b.build else -1
    except ValueError:
        return -1


def has_nvidia(env: Mapping[str, str] = os.environ, exists: Callable[[str], bool] = os.path.isfile) -> bool:
    """True when %SystemRoot%\\System32\\nvml.dll exists (the NVIDIA driver installs it). Only its existence is
    checked; it is never loaded. Never raises."""
    try:
        root = _env_get(env, "SystemRoot") or _env_get(env, "windir")
        if not root:
            return False
        return bool(exists(os.path.join(root, "System32", "nvml.dll")))
    except Exception:
        return False


# ------------------------------------------------------------------------------------------------ roots
def winget_dirs(env: Mapping[str, str] = os.environ) -> list[str]:
    """The ggml.llamacpp_* package folders of winget (user and machine scope). Lists only the Packages folders."""
    out: list[str] = []
    bases = []
    lad = _env_get(env, "LOCALAPPDATA")
    if lad:
        bases.append(os.path.join(lad, "Microsoft", "WinGet", "Packages"))
    for var in ("ProgramFiles", "ProgramW6432"):
        pf = _env_get(env, var)
        if pf:
            bases.append(os.path.join(pf, "WinGet", "Packages"))
    seen: set[str] = set()
    for base in bases:
        try:
            if not strata_finder._local_root(base) or not os.path.isdir(base):
                continue
            with os.scandir(base) as it:
                for e in it:
                    if e.name.casefold().startswith(_WINGET_PREFIX) and not strata_finder._skip_entry(e):
                        k = os.path.normcase(e.path)
                        if k not in seen:
                            seen.add(k)
                            out.append(os.path.normpath(e.path))
        except Exception as exc:
            log.debug("llama_finder: winget folder %s: %s", base, exc)
    return sorted(out, key=str.casefold)


def path_dirs(env: Mapping[str, str] = os.environ) -> list[str]:
    """The folders on PATH (absolute, unquoted, deduplicated, in PATH order)."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        for p in _env_get(env, "PATH").split(os.pathsep):
            p = p.strip().strip('"')
            if not p or not os.path.isabs(p):
                continue
            k = os.path.normcase(os.path.normpath(p))
            if k not in seen:
                seen.add(k)
                out.append(os.path.normpath(p))
    except Exception:
        pass
    return out


def model_cache_dirs(env: Mapping[str, str] = os.environ) -> list[tuple[str, bool]]:
    """(folder, is_hf_cache) for the usual model download places named by `env`; existence is not checked."""
    out: list[tuple[str, bool]] = []
    lad = _env_get(env, "LOCALAPPDATA")
    home = _env_get(env, "USERPROFILE")
    llama_cache = _env_get(env, "LLAMA_CACHE")
    if llama_cache:
        out.append((llama_cache, False))
    if lad:
        out.append((os.path.join(lad, "llama.cpp"), False))
    hf = _env_get(env, "HF_HUB_CACHE")
    if hf:
        out.append((hf, True))
    hf_home = _env_get(env, "HF_HOME")
    if hf_home:
        out.append((os.path.join(hf_home, "hub"), True))
    if home:
        out.append((os.path.join(home, ".cache", "huggingface", "hub"), True))
        out.append((os.path.join(home, ".lmstudio", "models"), False))
        out.append((os.path.join(home, ".cache", "lm-studio", "models"), False))
    return out


# ------------------------------------------------------------------------------------------------ the walk
@dataclass
class _Root:
    path: str
    depth: int
    source: str          # Build.source for builds first seen under this root
    hf: bool = False     # resolve .gguf file links (Hugging Face cache only)


@dataclass
class _State:
    want_builds: bool
    want_models: bool
    min_bytes: int
    max_dirs: int
    lock: threading.Lock
    abort: threading.Event
    builds: dict[str, Build]
    models: dict[str, Model]
    dirs: int = 0


def _search(explicit: Callable[[], list[_Root]], roots: Iterable[str] | None, *, want_builds: bool,
            want_models: bool, time_budget_s: Any, stop_evt: threading.Event | None, max_depth: Any,
            max_dirs: Any, min_bytes: Any) -> tuple[list[Build], list[Model]]:
    try:
        budget = max(0.0, float(time_budget_s))
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET_S
    try:
        depth = max(0, int(max_depth))
    except (TypeError, ValueError):
        depth = DEFAULT_MAX_DEPTH
    try:
        dirs = max(1, int(max_dirs))
    except (TypeError, ValueError):
        dirs = DEFAULT_MAX_DIRS
    try:
        floor = max(0, int(min_bytes))
    except (TypeError, ValueError):
        floor = MIN_MODEL_BYTES
    deadline = time.monotonic() + budget
    st = _State(want_builds, want_models, floor, dirs, threading.Lock(), threading.Event(), {}, {})

    def out_of_time() -> bool:
        return (time.monotonic() >= deadline or st.abort.is_set()
                or (stop_evt is not None and _is_set(stop_evt)))

    def run() -> None:
        try:
            plan = explicit()
            if roots is None:
                user_roots = strata_finder.default_roots()      # on this thread: may block on a slow folder
            elif isinstance(roots, (str, bytes)):
                user_roots = [str(roots)]
            else:
                user_roots = [str(r) for r in roots]
            for r in user_roots:
                drive = strata_finder._is_drive_root(r) if r else False
                plan.append(_Root(r, min(depth, DRIVE_ROOT_MAX_DEPTH) if drive else depth, "folder"))
            _walk(plan, st, out_of_time)
        except Exception as exc:                               # belt and braces: never raise
            log.debug("llama_finder: walk failed: %s", exc)

    try:
        worker = threading.Thread(target=run, name="llama-finder", daemon=True)
        worker.start()
        hard_stop = deadline + _GRACE_S
        while worker.is_alive():
            if stop_evt is not None and _is_set(stop_evt):
                break
            left = hard_stop - time.monotonic()
            if left <= 0:
                log.debug("llama_finder: time budget spent; returning partial results")
                break
            worker.join(min(0.05, left))
    except Exception as exc:
        log.debug("llama_finder: worker failed: %s", exc)
    finally:
        st.abort.set()
    with st.lock:
        return list(st.builds.values()), list(st.models.values())


def _is_set(evt: Any) -> bool:
    try:
        return bool(evt.is_set())
    except Exception:
        return False


def _walk(plan: list[_Root], st: _State, out_of_time: Callable[[], bool]) -> None:
    visited: dict[str, int] = {}          # normcase path -> remaining depth it was listed with
    queues: list[deque[tuple[str, int, bool]]] = []
    owners: list[_Root] = []
    spent: list[float] = []
    for root in plan:
        if out_of_time():
            return
        try:
            if not root.path or not strata_finder._local_root(root.path) or not os.path.isdir(root.path):
                continue
            drive = strata_finder._is_drive_root(root.path)
            start = os.path.abspath(root.path) if drive else os.path.normpath(os.path.abspath(root.path))
        except Exception:
            continue
        queues.append(deque([(start, 0, False)]))
        owners.append(_Root(start, root.depth, root.source, root.hf))
        spent.append(0.0)
    while True:
        if out_of_time():
            log.debug("llama_finder: budget/stop reached")
            return
        if st.dirs >= st.max_dirs:
            log.debug("llama_finder: directory limit (%d) reached", st.max_dirs)
            return
        live = [i for i, q in enumerate(queues) if q]
        if not live:
            return
        i = min(live, key=lambda j: (queues[j][0][1] > 0, spent[j], j))
        t0 = time.perf_counter()
        try:
            path, d, llama_only = queues[i].popleft()
            for sub, sub_llama_only in _list_dir(path, d, owners[i], llama_only, visited, st, out_of_time):
                queues[i].append((sub, d + 1, sub_llama_only))
        except Exception as exc:
            log.debug("llama_finder: listing failed: %s", exc)
        finally:
            spent[i] += time.perf_counter() - t0


def _list_dir(path: str, depth: int, root: _Root, llama_only: bool, visited: dict[str, int], st: _State,
              out_of_time: Callable[[], bool]) -> list[tuple[str, bool]]:
    """List one directory: record builds/models in it, return its walkable subdirectories."""
    key = os.path.normcase(os.path.normpath(path))
    remaining = root.depth - depth
    if visited.get(key, -1) >= remaining:
        return []
    first_visit = key not in visited
    visited[key] = remaining
    st.dirs += 1
    subdirs: list[tuple[str, bool]] = []
    files: dict[str, os.DirEntry] = {}
    links: dict[str, os.DirEntry] = {}
    try:
        with os.scandir(path) as it:
            for n, entry in enumerate(it):
                if n & 255 == 255 and out_of_time():
                    return []
                low = entry.name.casefold()
                try:
                    if entry.is_symlink():
                        if root.hf and low.endswith(".gguf") and not llama_only:
                            links[low] = entry
                        continue
                    if entry.is_file(follow_symlinks=False):
                        if not llama_only:
                            files[low] = entry
                        continue
                except OSError:
                    continue
                if depth >= root.depth:
                    continue
                if llama_only and "llama" not in low:
                    continue
                if low in _PROGRAM_FILES and not llama_only:
                    if _walkable(entry, ignore_names=True):
                        subdirs.append((entry.path, True))
                    continue
                if _walkable(entry):
                    subdirs.append((entry.path, False))
    except (PermissionError, OSError) as exc:
        log.debug("llama_finder: cannot list %s: %s", path, exc)
        return []
    except Exception as exc:
        log.debug("llama_finder: error listing %s: %s", path, exc)
        return []
    if first_visit:
        if st.want_builds and SERVER_EXE in files and CORE_DLLS & files.keys():
            _record_build(path, files, root, st)
        if st.want_models and (files or links):
            _record_models(files, links, st, out_of_time)
    subdirs.sort(key=lambda t: t[0].casefold())
    return subdirs


def _walkable(entry: os.DirEntry, ignore_names: bool = False) -> bool:
    if not ignore_names and entry.name.casefold() in SKIP_NAMES:
        return False
    if ignore_names:
        # 'Program Files' is in the skip list; check everything else strata_finder checks under a neutral name
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                return False
            if hasattr(entry, "is_junction") and entry.is_junction():
                return False
            s = entry.stat(follow_symlinks=False)
        except OSError:
            return False
        attrs = strata_finder._attrs(s, entry.path)
        if attrs & (strata_finder._FILE_ATTRIBUTE_SYSTEM | strata_finder._CLOUD_ONLY):
            return False
        if attrs & strata_finder._FILE_ATTRIBUTE_REPARSE_POINT:
            return False
        return True
    try:
        return not strata_finder._skip_entry(entry)
    except Exception:
        return False


def _record_build(path: str, files: dict[str, os.DirEntry], root: _Root, st: _State) -> None:
    try:
        exe = files[SERVER_EXE]
        if strata_finder._cloud_only(exe):
            return
        found = [b for b in BACKENDS if f"ggml-{b}.dll" in files]
        backends = tuple(found) or ("cpu",)
        folder = os.path.normpath(path)
        num = _build_number(os.path.basename(folder))
        if num is None:
            num = _build_number(os.path.basename(os.path.dirname(folder)))
        if num is None:
            zips = [_build_number(n) for n in files if n.endswith(".zip")]
            nums = [z for z in zips if z is not None]
            num = max(nums) if nums else None
        program = os.path.normpath(exe.path)
        b = Build(program=program, folder=folder, backend=backends[0], backends=backends,
                  build=f"b{num}" if num is not None else None, source=root.source)
        k = _real_key(program)
        with st.lock:
            if k not in st.builds and not st.abort.is_set():
                st.builds[k] = b
                log.debug("llama_finder: build %s (%s)", program, ",".join(backends))
    except Exception as exc:
        log.debug("llama_finder: build check in %s failed: %s", path, exc)


def _model_name(file_name: str) -> tuple[str, int | None]:
    """('name', split part number or None) for 'name-00001-of-00003.gguf'."""
    stem = file_name[:-5] if file_name.casefold().endswith(".gguf") else file_name
    m = _SPLIT_RE.search(stem)
    if m:
        return stem[:m.start()], int(m.group(1))
    return stem, None


def _excluded_name(low: str) -> bool:
    return "mmproj" in low or low.startswith("ggml-vocab")


def _record_models(files: dict[str, os.DirEntry], links: dict[str, os.DirEntry], st: _State,
                   out_of_time: Callable[[], bool]) -> None:
    # candidates: low name -> (path shown, real file to stat/open)
    cands: dict[str, tuple[str, str, os.DirEntry | None]] = {}
    for low, e in files.items():
        if low.endswith(".gguf") and not _excluded_name(low):
            cands[low] = (os.path.normpath(e.path), os.path.normpath(e.path), e)
    for low, e in links.items():
        if low in cands or _excluded_name(low):
            continue
        try:
            target = os.path.realpath(e.path)
            if not launcher_bat.is_local_fixed_path(target) or os.path.islink(target):
                continue
            cands[low] = (os.path.normpath(e.path), target, None)
        except (OSError, ValueError):
            continue
    if not cands:
        return
    sizes: dict[str, int] = {}
    for low, (_shown, real, ent) in cands.items():
        try:
            if ent is not None:
                if strata_finder._cloud_only(ent):
                    continue
                sizes[low] = int(ent.stat(follow_symlinks=False).st_size)
            else:
                s = os.stat(real)
                if strata_finder._attrs(s, real) & strata_finder._CLOUD_ONLY or not os.path.isfile(real):
                    continue
                sizes[low] = int(s.st_size)
        except (OSError, ValueError):
            continue
    # split models: the first part stands for the whole model (size = parts found here)
    totals: dict[tuple[str, int], int] = {}
    for low, size in sizes.items():
        m = _SPLIT_RE.search(low[:-5])
        if m:
            totals[(low[:m.start()], int(m.group(2)))] = totals.get((low[:m.start()], int(m.group(2))), 0) + size
    for low, (shown, real, _e) in cands.items():
        if out_of_time():
            return
        if low not in sizes:
            continue
        name, part = _model_name(os.path.basename(shown))
        if part is not None and part != 1:
            continue
        size = sizes[low]
        if part is not None:
            m = _SPLIT_RE.search(low[:-5])
            if m:
                size = totals.get((low[:m.start()], int(m.group(2))), size)
        if size < st.min_bytes:
            continue
        k = _real_key(real)
        with st.lock:
            if k in st.models:
                continue
        if not _has_magic(real):
            log.debug("llama_finder: %s is not a GGUF file", shown)
            continue
        model = Model(path=shown, name=name, size_bytes=size, folder=os.path.dirname(shown))
        with st.lock:
            if k not in st.models and not st.abort.is_set():
                st.models[k] = model
                log.debug("llama_finder: model %s", shown)


def _has_magic(path: str) -> bool:
    """The file starts with b'GGUF' (reads 4 bytes)."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == GGUF_MAGIC
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------------------------------------ public search
def _build_plan(env: Mapping[str, str]) -> list[_Root]:
    plan = [_Root(d, _EXPLICIT_DEPTH, "winget") for d in winget_dirs(env)]
    plan += [_Root(d, 0, "path") for d in path_dirs(env)]
    return plan


def _model_plan(env: Mapping[str, str], extra_dirs: Any) -> list[_Root]:
    plan: list[_Root] = []
    try:
        extras = [extra_dirs] if isinstance(extra_dirs, str) else list(extra_dirs or ())
    except TypeError:
        extras = []
    for d in extras:
        if isinstance(d, (str, os.PathLike)):
            plan.append(_Root(os.fspath(d), _EXTRA_DEPTH, "folder"))
    plan += [_Root(d, _EXPLICIT_DEPTH, "folder", hf) for d, hf in model_cache_dirs(env)]
    return plan


def find_builds(roots: Iterable[str] | None = None, *, time_budget_s: float = DEFAULT_BUDGET_S,
                stop_evt: threading.Event | None = None, env: Mapping[str, str] = os.environ,
                max_depth: int = DEFAULT_MAX_DEPTH, max_dirs: int = DEFAULT_MAX_DIRS) -> list[Build]:
    """llama-server.exe builds (next to llama.dll / ggml.dll): winget folder(s), PATH folders, then `roots`
    (default_roots() when None). Deduplicated by real path, best first (rank_builds with has_nvidia(env))."""
    try:
        builds, _ = _search(lambda: _build_plan(env), roots, want_builds=True, want_models=False,
                            time_budget_s=time_budget_s, stop_evt=stop_evt, max_depth=max_depth, max_dirs=max_dirs,
                            min_bytes=MIN_MODEL_BYTES)
        return rank_builds(sorted(builds, key=lambda b: b.program.casefold()), has_nvidia(env))
    except Exception as exc:
        log.debug("llama_finder: find_builds failed: %s", exc)
        return []


def find_models(roots: Iterable[str] | None = None, *, time_budget_s: float = DEFAULT_BUDGET_S,
                stop_evt: threading.Event | None = None, extra_dirs: Iterable[str] = (),
                env: Mapping[str, str] = os.environ, max_depth: int = DEFAULT_MAX_DEPTH,
                max_dirs: int = DEFAULT_MAX_DIRS, min_bytes: int = MIN_MODEL_BYTES) -> list[Model]:
    """.gguf models: extra_dirs first, the model caches named by `env`, then `roots` (default_roots() when None).
    Deduplicated by real path, sorted by name (case-insensitive) then size."""
    try:
        _, models = _search(lambda: _model_plan(env, extra_dirs), roots, want_builds=False, want_models=True,
                            time_budget_s=time_budget_s, stop_evt=stop_evt, max_depth=max_depth, max_dirs=max_dirs,
                            min_bytes=min_bytes)
        return sort_models(models)
    except Exception as exc:
        log.debug("llama_finder: find_models failed: %s", exc)
        return []


def find_all(roots: Iterable[str] | None = None, *, time_budget_s: float = DEFAULT_BUDGET_S,
             stop_evt: threading.Event | None = None, extra_dirs: Iterable[str] = (),
             env: Mapping[str, str] = os.environ, max_depth: int = DEFAULT_MAX_DEPTH,
             max_dirs: int = DEFAULT_MAX_DIRS, min_bytes: int = MIN_MODEL_BYTES) -> tuple[list[Build], list[Model]]:
    """find_builds + find_models in ONE walk of the shared roots (one budget instead of two)."""
    try:
        builds, models = _search(lambda: _model_plan(env, extra_dirs) + _build_plan(env), roots, want_builds=True,
                                 want_models=True, time_budget_s=time_budget_s, stop_evt=stop_evt,
                                 max_depth=max_depth, max_dirs=max_dirs, min_bytes=min_bytes)
        # a build first seen from a model root is attributed by what it is, not by that root
        wk = {_real_key(d) for d in winget_dirs(env)}
        pk = {os.path.normcase(os.path.normpath(d)) for d in path_dirs(env)}
        fixed = []
        for b in builds:
            src = ("winget" if _real_key(b.folder) in wk else
                   "path" if os.path.normcase(b.folder) in pk else b.source)
            fixed.append(b if src == b.source else Build(b.program, b.folder, b.backend, b.backends, b.build, src))
        return rank_builds(sorted(fixed, key=lambda b: b.program.casefold()), has_nvidia(env)), sort_models(models)
    except Exception as exc:
        log.debug("llama_finder: find_all failed: %s", exc)
        return [], []


def sort_models(models: Iterable[Model]) -> list[Model]:
    try:
        return sorted(models, key=lambda m: (m.name.casefold(), m.size_bytes, m.path.casefold()))
    except Exception:
        return []


# ------------------------------------------------------------------------------------------------ choosing
def rank_builds(builds: Iterable[Build], nvidia: bool) -> list[Build]:
    """Best first: backend (CUDA first on an NVIDIA card, last among GPU backends otherwise; CPU last), then the
    higher build number, then folder > winget > path. Stable and pure; never raises."""
    order = _BACKEND_RANK_NVIDIA if nvidia else _BACKEND_RANK_OTHER

    def key(b: Build) -> tuple[int, int, int]:
        try:
            bks = tuple(b.backends) or (b.backend,)
            best = min((order.index(x) if x in order else len(order)) for x in bks)
        except Exception:
            best = len(order)
        return best, -_build_int(b), _SOURCE_RANK.get(getattr(b, "source", ""), 3)

    try:
        items = [b for b in builds if isinstance(b, Build)]
        return sorted(items, key=key)
    except Exception:
        return []


def choose(builds: Iterable[Build], models: Iterable[Model], nvidia: bool) -> Choice:
    """The build to preselect (best ranked) and the model when there is exactly one. Never raises."""
    try:
        ranked = rank_builds(builds, nvidia)
        ms = [m for m in (models or ()) if isinstance(m, Model)]
    except Exception:
        ranked, ms = [], []
    build = ranked[0] if ranked else None
    model = ms[0] if len(ms) == 1 else None
    if build is None and not ms:
        why = "No llama-server.exe and no .gguf model were found."
    elif build is None:
        why = (f"No llama-server.exe next to llama.dll or ggml.dll was found; {len(ms)} model"
               f"{'' if len(ms) == 1 else 's'} found.")
    else:
        what = build_label(build) + (f" (best of {len(ranked)} builds)" if len(ranked) > 1 else "")
        if model is not None:
            why = f"Chose {what} and the only model found, {model.name}."
        elif not ms:
            why = f"Chose {what}; no .gguf model was found, so a model file must be picked."
        else:
            why = f"Chose {what}; {len(ms)} models were found, so the model is left to the user."
    return Choice(build=build, model=model, clear=build is not None and model is not None, why=why)


# ------------------------------------------------------------------------------------------------ arguments
def default_args(model_path: str, port: int) -> tuple[str, ...]:
    return ("-m", str(model_path), "--port", str(port))


def _flag(tok: str) -> str:
    """An argv token as llama.cpp's parser sees it: in a `--` option, '_' means '-'."""
    return tok.replace("_", "-") if tok.startswith("--") else tok


def with_model(args: Sequence[str], model_path: str) -> tuple[str, ...]:
    """`args` with the model file set to model_path: '-m X' / '--model X' / '--model=X' are replaced (at the first
    one's place; later ones dropped); -hf / --hf-repo / --hf-file / -mu / -dr ... and their values are removed;
    with no model argument at all, '-m model_path' is put first. Pure; never raises."""
    mp = str(model_path)
    try:
        if isinstance(args, (str, bytes)) or args is None:
            return ("-m", mp)
        toks = [str(a) for a in args]
    except Exception:
        return ("-m", mp)
    out: list[str] = []
    placed = False
    i = 0
    while i < len(toks):
        tok = toks[i]
        name, eq, _val = _flag(tok).partition("=")
        if name in _MODEL_VALUE_FLAGS or name in _MODEL_SOURCE_FLAGS:
            if name in _MODEL_VALUE_FLAGS and not placed:
                out += ["-m", mp]
                placed = True
            i += 1 if eq else 2          # '--model=X' carries its value; '-m X' takes the next token
            continue
        out.append(tok)
        i += 1
    if not placed:
        out = ["-m", mp] + out
    return tuple(out)


# ------------------------------------------------------------------------------------------------ labels
# ---- friendly controls: one value option in an argument list (pure; never raise) --------------------------------
_CTX_FLAGS = ("-c", "--ctx-size")
_VISION_FLAGS = ("--mmproj", "-mm")
_VISION_OFF_FLAGS = ("--no-mmproj",)
_VISION_URL_FLAGS = ("--mmproj-url", "-mmu")
_PORT_FLAGS = ("--port",)
CONTEXT_CHOICES: tuple[tuple[str, int | None], ...] = (
    ("Auto (llama.cpp decides)", None), ("8K tokens", 8192), ("16K tokens", 16384), ("32K tokens", 32768),
    ("64K tokens", 65536), ("128K tokens", 131072))


def option_value(args: Sequence[str], flags: Sequence[str]) -> str | None:
    """The value of the LAST `flag value` / `flag=value` among `flags` (llama.cpp lets the last one win)."""
    try:
        out = None
        toks = [str(a) for a in args]
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in flags and i + 1 < len(toks):
                out = toks[i + 1]
                i += 2
                continue
            for f in flags:
                if f.startswith("--") and t.startswith(f + "="):
                    out = t[len(f) + 1:]
            i += 1
        return out
    except Exception:
        return None


def with_option(args: Sequence[str], flags: Sequence[str], value: str | None) -> tuple[str, ...]:
    """The same arguments with that option set to `value` (in the place of its first occurrence, else appended),
    or removed when value is None. Every other argument stays where it is."""
    try:
        toks = [str(a) for a in args]
        out: list[str] = []
        placed = False
        i = 0
        while i < len(toks):
            t = toks[i]
            hit = t in flags and i + 1 < len(toks)
            eq = any(f.startswith("--") and t.startswith(f + "=") for f in flags)
            if hit or eq:
                if value is not None and not placed:
                    out += [flags[0], str(value)]
                    placed = True
                i += 2 if hit else 1
                continue
            out.append(t)
            i += 1
        if value is not None and not placed:
            out += [flags[0], str(value)]
        return tuple(out)
    except Exception:
        try:
            return tuple(str(a) for a in args)
        except Exception:
            return ()


def context_of(args: Sequence[str]) -> int | None:
    v = option_value(args, _CTX_FLAGS)
    return int(v) if v is not None and v.isdigit() and int(v) > 0 else None


def with_context(args: Sequence[str], tokens: int | None) -> tuple[str, ...]:
    return with_option(args, _CTX_FLAGS, str(int(tokens)) if tokens else None)


def port_of(args: Sequence[str]) -> int | None:
    v = option_value(args, _PORT_FLAGS)
    return int(v) if v is not None and v.isdigit() else None


def with_port(args: Sequence[str], port: int) -> tuple[str, ...]:
    return with_option(args, _PORT_FLAGS, str(int(port)))


def vision_of(args: Sequence[str]) -> str | None:
    """The vision projector file the arguments name, or None (also None for --no-mmproj / a projector URL)."""
    toks = [str(a) for a in args]
    if any(t in _VISION_OFF_FLAGS for t in toks):
        return None
    v = option_value(toks, _VISION_FLAGS)
    return v.strip('"') if v else None


def with_vision(args: Sequence[str], projector: str | None) -> tuple[str, ...]:
    """Vision on with that projector file, or off (None): every projector option is removed first."""
    toks = [t for t in (str(a) for a in args) if t not in _VISION_OFF_FLAGS]
    return with_option(with_option(toks, _VISION_URL_FLAGS, None), _VISION_FLAGS, projector)


def vision_files(model_path: str, listdir: Callable[[str], list[str]] = os.listdir) -> list[tuple[str, str]]:
    """(label, path) of the vision projector files (mmproj*.gguf) in the model file's own folder - where
    downloads put them. One directory listing; nothing is opened. Never raises."""
    try:
        folder = os.path.dirname(os.path.abspath(str(model_path)))
        names = sorted(n for n in listdir(folder) if n.casefold().endswith(".gguf") and "mmproj" in n.casefold())
        return [(os.path.splitext(n)[0], os.path.join(folder, n)) for n in names]
    except Exception:
        return []


def _size_text(n: int) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "? GB"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{max(1, round(n / 1024 ** 2))} MB"


def build_label(b: Build) -> str:
    """'CUDA · b12314 · llama.cpp-cuda-b12314', 'Vulkan · winget'. Pure; never raises."""
    try:
        parts = [_BACKEND_NAMES.get(b.backend, str(b.backend))]
        if b.build:
            parts.append(str(b.build))
        if b.source == "winget":
            parts.append("winget")
        else:
            parts.append(os.path.basename(os.path.normpath(b.folder)) or b.folder)
        return " · ".join(parts)
    except Exception:
        return str(getattr(b, "program", b))


def model_label(m: Model) -> str:
    """'Qwen3.8-27B-UD-Q3_K_XL · 12.2 GB' (GB = 1024^3 bytes, as Explorer shows it). Pure; never raises."""
    try:
        return f"{m.name} · {_size_text(m.size_bytes)}"
    except Exception:
        return str(getattr(m, "path", m))


T = TypeVar("T")


def unique_labels(items: Sequence[T], label_fn: Callable[[T], str]) -> list[str]:
    """label_fn(item) for each item, made unique: equal labels get ' · <parent folder name>', then the full folder,
    then ' (2)', ' (3)'... Same order and length as items. Never raises."""
    try:
        seq = list(items)
    except Exception:
        return []

    def base(it: T) -> str:
        try:
            return str(label_fn(it))
        except Exception:
            return str(it)

    def folder(it: T) -> str:
        f = getattr(it, "folder", None)
        return os.path.normpath(str(f)) if f else ""

    bases = [base(it) for it in seq]
    labels = list(bases)
    for step in (1, 2):
        counts: dict[str, int] = {}
        for lab in labels:
            counts[lab] = counts.get(lab, 0) + 1
        if all(c == 1 for c in counts.values()):
            return labels
        new = []
        for it, b0, lab in zip(seq, bases, labels, strict=True):
            f = folder(it)
            if counts[lab] > 1 and f:
                extra = (os.path.basename(f) or f) if step == 1 else f
                new.append(f"{b0} · {extra}")
            else:
                new.append(lab)
        labels = new
    seen: dict[str, int] = {}
    out = []
    for lab in labels:
        n = seen.get(lab, 0) + 1
        seen[lab] = n
        out.append(lab if n == 1 else f"{lab} ({n})")
    # a numbered label could collide with a real one; number again until unique (bounded by len(out))
    for _ in range(len(out)):
        if len(set(out)) == len(out):
            break
        fixed, used = [], set()
        for lab in out:
            cand, n = lab, 1
            while cand in used:
                n += 1
                cand = f"{lab} ({n})"
            used.add(cand)
            fixed.append(cand)
        out = fixed
    return out
