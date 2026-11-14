"""The .gguf files of the models folder: list them, and delete exactly one file the user picked and confirmed.

Pure functions: no Tk, no network, no child process. The models folder is a folder the user also uses, and the files
in it were not made by this app, so delete() is the one place that removes such a file. It removes for good (no
Recycle Bin) and only after every check below passed, immediately before its one call that removes it:

  1. the name ends in .gguf (any case)                                                        else "not_gguf"
  2. the file's parent folder IS the models folder (realpath + normcase; no subfolder, no '..',
     no other drive)                                                                          else "outside"
  3. it is still there                                                                        else "missing"
     and is a plain regular file reached by its own path: not a symlink, not a junction or any
     other reparse point, and its realpath is the path itself                                 else "link"
  4. its size and modification time are the ones the user was shown                          else "changed"
  5. no running llama-server.exe was started with it                                         else "serving"
  6. Windows lets it go: in use by a program -> "in_use", any other error -> "failed"

Every refusal raises ModelFileError (kind + one sentence for the user) and leaves every file as it was. Nothing in
this module renames, writes, recurses or expands a pattern; one call removes one file.
"""
from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import psutil

log = logging.getLogger("agent_start")

KINDS = frozenset({"not_gguf", "outside", "link", "missing", "changed", "serving", "in_use", "failed"})
SERVER_EXE = "llama-server.exe"
# llama-server options whose value is a model file (short and long names); any other argument that ends in .gguf
# (--model-draft, --lora, ...) is taken too.
_PATH_FLAGS = frozenset({"-m", "--model", "-mm", "--mmproj", "-md", "--model-draft"})
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_IN_USE_WINERRORS = frozenset({5, 32, 1224})  # access denied, sharing violation, mapped into a program's memory
_SEP_RE = re.compile(r"[\\/]+")
_BAD_SEGMENTS = frozenset({".", "..", "?"})   # '..' spellings and the \\?\ and \\.\ device prefixes

_MESSAGES = {
    "not_gguf": "{name} is not a .gguf model file from the models folder, so Agent Start did not delete it.",
    "outside": "{name} is not directly in the models folder, so Agent Start did not delete it.",
    "link": "{name} is a link to another place, not a plain file, so Agent Start did not delete it.",
    "missing": "{name} is no longer in the models folder.",
    "changed": "{name} has changed since the list was made, so it was not deleted; refresh the list and try again.",
    "serving": "{name} is the model an engine is running right now; turn the engine off first, then delete it.",
    "in_use": "{name} is in use by another program; close that program, then try again.",
    "failed": "{name} could not be deleted ({detail}).",
}

# Test seam only: lets a suite report a symlink or reparse point without the admin right to make one. The app
# never changes it.
_lstat = os.lstat


@dataclass(frozen=True)
class ModelFile:
    """One .gguf file as list_folder() saw it. The confirmation shows it; delete() takes this very object."""
    path: str        # absolute: the models folder's realpath joined with name
    name: str        # file name
    size: int        # bytes
    mtime_ns: int    # st_mtime_ns when listed


class ModelFileError(Exception):
    """A refusal. `kind` is one of KINDS; str(exc) / `message` is one plain sentence that names the file."""

    def __init__(self, kind: str, message: str) -> None:
        if kind not in KINDS:
            kind = "failed"
        super().__init__(message)
        self.kind = kind
        self.message = message


def _is_gguf(name: object) -> bool:
    return isinstance(name, str) and name.lower().endswith(".gguf")


def _plain_file(st: Any) -> bool:
    """A regular file that is not a symlink and carries no reparse point (junction, cloud placeholder, ...)."""
    return (stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode)
            and not getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _abs_folder(folder: object) -> str | None:
    """folder as an absolute str path, or None (empty, relative, not a path)."""
    try:
        f = os.fspath(folder)  # type: ignore[call-overload]
    except TypeError:
        return None
    if not isinstance(f, str) or not f or not os.path.isabs(f):
        return None
    return f


# ------------------------------------------------------------------------------------------------- listing

def _entries(folder: str) -> list[os.DirEntry[str]]:
    with os.scandir(folder) as it:
        return list(it)


def list_folder(folder: str) -> tuple[ModelFile, ...]:
    """The regular .gguf files directly in folder, sorted by name (casefold). Subfolders are not read; a symlink, a
    junction or any reparse point is not listed. Never raises: an unreadable or missing folder gives ()."""
    try:
        f = _abs_folder(folder)
        if f is None:
            return ()
        real = os.path.realpath(f)
        out: list[ModelFile] = []
        for e in _entries(real):
            if not _is_gguf(e.name):
                continue
            try:
                st = _lstat(e.path)
            except OSError:
                continue
            if _plain_file(st):
                out.append(ModelFile(os.path.join(real, e.name), e.name, st.st_size, st.st_mtime_ns))
        out.sort(key=lambda m: m.name.casefold())
        return tuple(out)
    except Exception:  # noqa: BLE001 - listing is display only; any surprise means "nothing to show"
        log.debug("model_files: could not list the models folder", exc_info=True)
        return ()


# ------------------------------------------------------------------------------------------------- serving

def _model_args(cmdline: list[str]) -> list[str]:
    """The arguments of one command line that name a model file: the value after -m / --model / --mmproj (and
    their short forms), the value of --flag=PATH, and any other argument ending in .gguf."""
    args = [str(a).strip().strip('"') for a in cmdline[1:]]
    found: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _PATH_FLAGS:
            if i + 1 < len(args):
                found.append(args[i + 1])
            i += 2
            continue
        flag, eq, value = a.partition("=")
        if eq and flag.startswith("-") and (flag in _PATH_FLAGS or _is_gguf(value)):
            found.append(value)
        elif _is_gguf(a):
            found.append(a)
        i += 1
    return [f for f in found if f]


def serving_paths(process_iter: Callable[[], Iterable[Any]] | None = None) -> frozenset[str]:
    """normcase(realpath()) of every model file a running llama-server.exe was started with, whoever started it.
    Read-only process scan; a relative path is resolved against that process's working folder when it can be read
    (otherwise it is skipped). Never raises: a process that cannot be read or has gone is skipped.
    process_iter is the test seam (default psutil.process_iter)."""
    out: set[str] = set()
    try:
        procs = (process_iter or psutil.process_iter)()
        for p in procs:
            try:
                if str(p.name()).lower() != SERVER_EXE:
                    continue
                cwd: str | None = None
                cwd_read = False
                for raw in _model_args(list(p.cmdline())):
                    if not os.path.isabs(raw):
                        if not cwd_read:
                            cwd_read = True
                            try:
                                cwd = str(p.cwd()) or None
                            except Exception:  # noqa: BLE001 - psutil.Error, OSError: no folder to resolve against
                                cwd = None
                        if cwd is None:
                            continue
                        raw = os.path.join(cwd, raw)
                    out.add(os.path.normcase(os.path.realpath(raw)))
            except Exception:  # noqa: BLE001 - psutil.Error (AccessDenied, gone), odd values: skip this process
                log.debug("model_files: skipped a process in the serving scan", exc_info=True)
    except Exception:  # noqa: BLE001
        log.debug("model_files: serving scan failed", exc_info=True)
    return frozenset(out)


# ------------------------------------------------------------------------------------------------- delete

def _label(file: object) -> str:
    name = getattr(file, "name", None)
    if not isinstance(name, str) or not name:
        path = getattr(file, "path", None)
        name = os.path.basename(path) if isinstance(path, str) else ""
    name = "".join(c for c in name if c.isprintable())[:200]
    return name or "This file"


def _refuse(kind: str, name: str, detail: str = "") -> ModelFileError:
    log.warning("model_files: did not delete %s (%s)", name, kind)
    return ModelFileError(kind, _MESSAGES[kind].format(name=name, detail=detail))


def _serving_keys(serving: Iterable[str] | str | None) -> set[str]:
    if serving is None:
        return set()
    if isinstance(serving, (str, bytes, os.PathLike)):
        serving = (serving,)
    keys: set[str] = set()
    for s in serving:
        p = os.fsdecode(os.fspath(s))
        try:
            keys.add(os.path.normcase(os.path.realpath(p)))
        except (OSError, ValueError):
            keys.add(os.path.normcase(p))
    return keys


def delete(file: ModelFile, folder: str, serving: Iterable[str] = frozenset(), *,
           remove: Callable[[str], object] = os.remove) -> int:
    """Delete file.path for good and return the bytes freed. `file` is the very ModelFile the confirmation showed;
    `serving` is serving_paths() taken just before. Raises ModelFileError, leaving everything as it was, unless
    every check in the module docstring passes; then exactly this one file is removed."""
    name = _label(file)
    path = getattr(file, "path", None)

    # 1. a .gguf name, and the name the user saw is the file that would go
    if not isinstance(file, ModelFile) or not isinstance(path, str) or not isinstance(file.name, str) \
            or ":" in file.name:
        raise _refuse("not_gguf", name)
    if not _is_gguf(file.name):
        raise _refuse("not_gguf", name)
    if os.path.basename(path) != file.name:
        raise _refuse("not_gguf", name)

    # 2. directly in the models folder
    ok = False
    try:
        f = _abs_folder(folder)
        if f is not None and os.path.isabs(path) and not _BAD_SEGMENTS & set(_SEP_RE.split(path)):
            parent = os.path.normcase(os.path.realpath(os.path.dirname(path)))
            want = os.path.normcase(os.path.realpath(f))
            ok = parent == want
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise _refuse("outside", name)

    # 3. still there, a plain regular file, reached by its own path
    try:
        st = _lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        raise _refuse("missing", name) from None
    except OSError as e:
        raise _refuse("failed", name, e.strerror or type(e).__name__) from None
    if not _plain_file(st):
        raise _refuse("link", name)
    try:
        same = os.path.normcase(os.path.realpath(path)) == os.path.normcase(path)
    except (OSError, ValueError):
        same = False
    if not same:
        raise _refuse("link", name)

    # 4. the file the user confirmed, not one put there since
    if st.st_size != file.size or st.st_mtime_ns != file.mtime_ns:
        raise _refuse("changed", name)

    # 5. no engine serves it
    try:
        keys = _serving_keys(serving)
    except Exception:  # noqa: BLE001 - an unreadable list cannot prove the file is free
        raise _refuse("serving", name) from None
    if os.path.normcase(os.path.realpath(path)) in keys:
        raise _refuse("serving", name)

    # 6. the one removal
    try:
        remove(path)
    except OSError as e:
        if isinstance(e, PermissionError) or getattr(e, "winerror", None) in _IN_USE_WINERRORS:
            raise _refuse("in_use", name) from None
        raise _refuse("failed", name, e.strerror or type(e).__name__) from None
    log.info("model_files: deleted %s (%d bytes)", name, st.st_size)
    return int(st.st_size)
