"""FolderFlow: the "In this folder" view of the "Download a model" window, without Tk.

The view lists the .gguf files of the models folder with their sizes and deletes, for good, the one file the user
chose and confirmed in the window. Every file operation is model_files': this module calls model_files.list_folder,
model_files.serving_paths and model_files.delete (through the `files` seam, so a suite passes a fake) and nothing
else from it. It never removes, renames or writes a file itself.

Rules:
  * One action at a time: reading the folder ('reading'), the question ('asking'), a delete ('deleting').
  * Every model_files call runs on a worker (run_worker); every state change happens on the UI thread (post); the
    view redraws from the public state on on_change(). The answer of an older request (another read, the window
    closed) is dropped: a sequence number for the reads, the closed flag for everything.
  * A file a running llama-server was started with is listed greyed with a note, and cannot be chosen.
  * The question names the file and its size. Delete for good passes model_files.delete the very ModelFile the
    question named, the folder it was listed from, and the serving set read on the worker after the button was
    pressed. There is never a second attempt and never another file. Afterwards the folder is read again.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

import model_files
from model_files import ModelFile, ModelFileError

log = logging.getLogger("agent_start")

PHASES = ("idle", "reading", "asking", "deleting")
MESSAGE_KINDS = ("info", "warn", "error", "ok")

GIB = 1024 ** 3
MIB = 1024 ** 2

IN_USE_NOTE = "· in use by llama.cpp"
QUESTION = "Delete {name} ({size}) for good? This cannot be undone."
DELETING = "Deleting {name}…"
DELETED = "Deleted {name} ({size})."
UNEXPECTED = "Deleting failed unexpectedly; see the log."
READ_FAILED = "Reading the folder failed unexpectedly; see the log."
HINT_EMPTY = "No .gguf files in this folder."
HINT_READING = "Reading the folder…"

# ModelFileError kind -> message colour (everything not named here is an error)
_KIND_TONE = {"serving": "warn", "in_use": "warn"}


def fmt_size(n: Any) -> str:
    """'12.2 GB' (GB = 1024^3 bytes, as Explorer and the Model list show it); '922 MB' below 1 GB. The download
    list and this view share it (download_dialog imports it from here)."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return "? GB"
    if v >= GIB:
        return f"{v / GIB:.1f} GB"
    return f"{max(1, round(v / MIB))} MB"


def question_for(file: ModelFile, name_limit: int | None = None) -> str:
    """The confirmation sentence for file: its name and its size. name_limit shortens a very long name (the
    window's room); the flow's own `question` always carries the full name."""
    name = str(file.name)
    if name_limit is not None and 1 < name_limit < len(name):
        name = name[:name_limit - 1] + "…"
    return QUESTION.format(name=name, size=fmt_size(file.size))


@dataclass(frozen=True)
class FolderRow:
    """One .gguf file of the folder, as shown."""
    file: ModelFile
    name: str             # the file name
    note: str             # '' or '· in use by llama.cpp'
    size_text: str        # '12.2 GB'
    selectable: bool      # False for a file a running llama-server serves


class FolderFlow:
    """The folder view's behaviour, without Tk. Every method and every on_change / on_deleted call runs on the UI
    thread; model_files calls run inside run_worker(fn); their answers come back through post(fn, *args)."""

    def __init__(self, dest_dir: str, *, files: Any = model_files,
                 run_worker: Callable[[Callable[[], None]], None],
                 post: Callable[..., None],
                 on_change: Callable[[], None] | None = None,
                 on_deleted: Callable[[str], None] | None = None) -> None:
        self._files = files
        self._run_worker = run_worker
        self._post = post
        self._on_change = on_change
        self._on_deleted = on_deleted

        self.dest_dir = str(dest_dir)
        self.phase = "idle"
        self.rows: tuple[FolderRow, ...] = ()
        self.chosen: ModelFile | None = None
        self.question = ""
        self.message = ""
        self.message_kind = "info"
        self.read_once = False             # the folder was read (an empty list then means: no .gguf files)
        self.closing = False               # close() was asked while a delete ran: the window closes after it
        self.closed = False

        self._seq = 0                      # bumped by every read, delete and folder change: older answers drop
        self._asked: ModelFile | None = None   # the file the question names (the one Delete for good deletes)

    # ------------------------------------------------------------------ derived state
    @property
    def busy(self) -> bool:
        return self.phase in ("reading", "deleting")

    @property
    def can_choose(self) -> bool:
        return not self.closed and self.phase == "idle"

    @property
    def can_delete(self) -> bool:
        """Delete… may be pressed: a choosable row is chosen and nothing else is going on."""
        return self.can_choose and self._chosen_row() is not None

    @property
    def can_change_dest(self) -> bool:
        return not self.closed and self.phase in ("idle", "reading")

    @property
    def can_close(self) -> bool:
        """close() would return True now."""
        return self.closed or self.phase != "deleting"

    @property
    def asked(self) -> ModelFile | None:
        """The file the question is about while it is up (or being answered), else None."""
        return self._asked if self.phase in ("asking", "deleting") else None

    @property
    def hint(self) -> str:
        """The line for the empty list ('' when there are rows)."""
        if self.rows:
            return ""
        return HINT_EMPTY if self.read_once else HINT_READING

    def _chosen_row(self) -> FolderRow | None:
        c = self.chosen
        if c is None:
            return None
        return next((r for r in self.rows if r.file is c and r.selectable), None)

    # ------------------------------------------------------------------ plumbing
    def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - a view problem must not break the flow
            log.exception("folder view redraw failed")

    def _say(self, text: str, kind: str = "info") -> None:
        self.message = text
        self.message_kind = kind if kind in MESSAGE_KINDS else "info"

    def _current(self, seq: int) -> bool:
        return not self.closed and seq == self._seq

    def _spawn(self, fn: Callable[[], None]) -> bool:
        try:
            self._run_worker(fn)
            return True
        except Exception:  # noqa: BLE001
            log.exception("folder view: worker could not be started")
            return False

    @staticmethod
    def _served(path: str, keys: frozenset[str]) -> bool:
        if os.path.normcase(path) in keys:
            return True
        try:
            return os.path.normcase(os.path.realpath(path)) in keys
        except (OSError, ValueError):
            return False

    def _read(self, dest: str) -> tuple[tuple[ModelFile, ...], frozenset[str]]:
        """Worker: the folder's .gguf files, and the paths of those a running llama-server was started with."""
        listed = tuple(self._files.list_folder(dest))
        serving: Iterable[str] = self._files.serving_paths()
        keys = frozenset(os.path.normcase(str(s)) for s in serving)
        return listed, frozenset(f.path for f in listed if self._served(f.path, keys))

    def _apply_listing(self, listed: tuple[ModelFile, ...], in_use: frozenset[str], keep_choice: bool) -> None:
        """Rows from a fresh listing. keep_choice: the chosen file stays chosen when it is still listed (by path; the
        row's new ModelFile becomes the chosen one) and may still be chosen; otherwise nothing is chosen."""
        self.rows = tuple(FolderRow(file=f, name=f.name, note=IN_USE_NOTE if f.path in in_use else "",
                                    size_text=fmt_size(f.size), selectable=f.path not in in_use) for f in listed)
        old = self.chosen
        self.chosen = None
        if keep_choice and old is not None:
            key = os.path.normcase(old.path)
            row = next((r for r in self.rows if os.path.normcase(r.file.path) == key and r.selectable), None)
            self.chosen = row.file if row is not None else None
        self.read_once = True

    # ------------------------------------------------------------------ reading
    def refresh(self) -> bool:
        """Read the folder again (list_folder + serving_paths, on a worker). Not while the question is up or a
        delete runs; a read already under way is replaced (its answer is dropped). The rows stay until it answers."""
        if self.closed or self.phase in ("asking", "deleting"):
            return False
        self._seq += 1
        seq, dest = self._seq, self.dest_dir
        self.phase = "reading"

        def work() -> None:
            try:
                listed, in_use = self._read(dest)
            except Exception:  # noqa: BLE001
                log.exception("folder view: reading the models folder failed unexpectedly")
                self._post(self._read_answer, seq, dest, None, frozenset())
                return
            self._post(self._read_answer, seq, dest, listed, in_use)

        if not self._spawn(work):
            self.phase = "idle"
            self._say(READ_FAILED, "error")
        self._changed()
        return True

    def _read_answer(self, seq: int, dest: str, listed: tuple[ModelFile, ...] | None,
                     in_use: frozenset[str]) -> None:
        if not self._current(seq) or dest != self.dest_dir:
            log.debug("folder view: late folder reading dropped")
            return
        self.phase = "idle"
        if listed is None:
            self.rows, self.chosen, self.read_once = (), None, True
            self._say(READ_FAILED, "error")
        else:
            self._apply_listing(listed, in_use, keep_choice=True)
        self._changed()

    # ------------------------------------------------------------------ choosing and the question
    def choose(self, file: ModelFile | FolderRow | None) -> bool:
        """A click on a row: choose it (one at a time; a click on the chosen row keeps it chosen). A row in use by
        llama.cpp cannot be chosen. None clears the choice. Only while nothing else is going on."""
        if not self.can_choose:
            return False
        if isinstance(file, FolderRow):
            file = file.file
        if file is None:
            self.chosen = None
            self._changed()
            return True
        row = next((r for r in self.rows if r.file is file), None)
        if row is None or not row.selectable:
            return False
        if self.chosen is not row.file:
            self.chosen = row.file
            self._say("")
        self._changed()
        return True

    def ask(self) -> bool:
        """Delete…: put up the question for the chosen file (its name and size)."""
        row = self._chosen_row()
        if not self.can_choose or row is None:
            return False
        self._asked = row.file
        self.question = question_for(row.file)
        self.phase = "asking"
        self._say("")
        self._changed()
        return True

    def keep(self) -> bool:
        """Keep: the question goes, nothing is deleted, the same row stays chosen."""
        if self.closed or self.phase != "asking":
            return False
        self.phase = "idle"
        self.question = ""
        self._asked = None
        self._changed()
        return True

    def confirm(self) -> bool:
        """Delete for good: on a worker, the serving set is read again and model_files.delete gets the very file
        the question named, with the folder. Then the folder is read again; the answer comes back through post."""
        if self.closed or self.phase != "asking" or self._asked is None:
            return False
        file, dest, files = self._asked, self.dest_dir, self._files
        self._seq += 1
        seq = self._seq
        self.phase = "deleting"
        self._say(DELETING.format(name=file.name))

        def work() -> None:
            err: ModelFileError | None = None
            unexpected = False
            try:
                serving = files.serving_paths()
                files.delete(file, dest, serving)
            except ModelFileError as e:
                err = e
            except Exception:  # noqa: BLE001
                log.exception("folder view: deleting failed unexpectedly")
                unexpected = True
            listed: tuple[tuple[ModelFile, ...], frozenset[str]] | None
            try:
                listed = self._read(dest)
            except Exception:  # noqa: BLE001
                log.exception("folder view: reading the models folder failed unexpectedly")
                listed = None
            self._post(self._delete_answer, seq, dest, file, err, unexpected, listed)

        if not self._spawn(work):
            self.phase = "idle"
            self.question = ""
            self._asked = None
            self._say(UNEXPECTED, "error")
        self._changed()
        return True

    def _delete_answer(self, seq: int, dest: str, file: ModelFile, err: ModelFileError | None, unexpected: bool,
                       listed: tuple[tuple[ModelFile, ...], frozenset[str]] | None) -> None:
        if not self._current(seq):
            log.debug("folder view: late delete answer dropped")
            return
        self.phase = "idle"
        self.question = ""
        self._asked = None
        ok = err is None and not unexpected
        if listed is not None and dest == self.dest_dir:
            self._apply_listing(listed[0], listed[1], keep_choice=not ok)
        elif ok:
            self.rows = tuple(r for r in self.rows if r.file is not file)
            self.chosen = None
        if ok:
            self._say(DELETED.format(name=file.name, size=fmt_size(file.size)), "info")
        elif err is not None:
            self._say(err.message, _KIND_TONE.get(err.kind, "error"))
        else:
            self._say(UNEXPECTED, "error")
        self._changed()
        if ok and self._on_deleted is not None:
            try:
                self._on_deleted(file.path)
            except Exception:  # noqa: BLE001
                log.exception("folder view: on_deleted callback failed")

    # ------------------------------------------------------------------ folder
    def set_dest_dir(self, path: str, read: bool = True) -> bool:
        """Another models folder: the rows and the choice go, the message is cleared; with read, the new folder is
        read. Not while the question is up or a delete runs."""
        if not self.can_change_dest or not isinstance(path, str) or not path.strip():
            return False
        self.dest_dir = path
        self._seq += 1                     # a read of the old folder is answered into the void
        self.phase = "idle"
        self.rows = ()
        self.chosen = None
        self.read_once = False
        self._say("")
        if read:
            self.refresh()
        else:
            self._changed()
        return True

    # ------------------------------------------------------------------ closing
    def close(self) -> bool:
        """The window wants to close. True: it may (every later answer is ignored from now on). While a delete runs
        it returns False and the window waits for its answer (on_change fires, then close() returns True)."""
        if self.closed:
            return True
        if self.phase == "deleting":
            self.closing = True
            return False
        self.closed = True
        self.phase = "idle"
        self.question = ""
        self._asked = None
        return True

    def abandon(self) -> None:
        """The window is going away now (its owner was destroyed): every later answer is ignored. No on_change and
        no on_deleted after this (a delete under way still finishes on its worker; only its answer is dropped)."""
        self.closed = True
