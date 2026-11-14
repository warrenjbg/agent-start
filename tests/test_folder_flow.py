"""Self-check for folder_flow.FolderFlow (the "In this folder" view of the download window, no Tk).
Run: .venv\\Scripts\\python.exe tests\\test_folder_flow.py

Safe by construction:
  * A FAKE `files` object stands in for model_files in every check but one: it records list_folder / serving_paths /
    delete and only pretends. Nothing is listed, scanned or deleted for real there.
  * The one check with the real model_files works on two tiny .gguf files this suite writes into its own temp
    folder (removed at the end); its serving scan only reads the process table.
  * No window: FolderFlow has no Tk in it. Workers are a manual runner (each interleaving is deterministic).
  * No process is started, no port is opened, no network. The data dir is a throwaway temp folder.
"""
from __future__ import annotations

import ast
import logging
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="agentstart-folderflow-home-"))
os.environ["AGENT_START_HOME"] = str(HOME)

import folder_flow as ff  # noqa: E402
import model_files  # noqa: E402
from folder_flow import FolderFlow, FolderRow  # noqa: E402
from model_files import ModelFile, ModelFileError  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
GIB = 1024 ** 3
MIB = 1024 ** 2
DEST = "C:\\Models\\llama-models"
OTHER = "D:\\Other\\Models"

A = ("Ornith-1.5-9B-Q4_K_M.gguf", int(5.4 * GIB), 111)
B = ("Ornith-1.5-9B-Q8_0.gguf", int(9.1 * GIB), 222)
Q = ("Qwen3.8-27B-UD-IQ4_XS.gguf", int(13.4 * GIB), 333)
M = ("mmproj-Ornith-1.5-9B-BF16.gguf", 879 * MIB, 444)


class LogTrap(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


LOG = LogTrap()
_logger = logging.getLogger("agent_start")
_logger.addHandler(LOG)
_logger.setLevel(logging.DEBUG)
_logger.propagate = False


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return bool(cond)


def eq(name: str, got, want) -> bool:
    return check(name, got == want, f"got {got!r}, want {want!r}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ----------------------------------------------------------------------------------------- fixtures
class Runner:
    """Manual worker runner: jobs wait until the test runs them (in order, or picked)."""

    def __init__(self) -> None:
        self.jobs: list = []
        self.in_job = False
        self.fail = False

    def __call__(self, fn) -> None:
        if self.fail:
            raise RuntimeError("no thread for you")
        self.jobs.append(fn)

    def run(self, index: int = 0) -> None:
        fn = self.jobs.pop(index)
        self.in_job = True
        try:
            fn()
        finally:
            self.in_job = False

    def run_all(self) -> None:
        while self.jobs:
            self.run(0)


class Poster:
    """post(fn, *args): direct (the default) or queued until drain()."""

    def __init__(self, queued: bool = False) -> None:
        self.queued = queued
        self.q: list = []

    def __call__(self, fn, *args) -> None:
        if self.queued:
            self.q.append((fn, args))
        else:
            fn(*args)

    def drain(self) -> None:
        while self.q:
            fn, args = self.q.pop(0)
            fn(*args)


def key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


class FakeFiles:
    """model_files' surface: list_folder, serving_paths, delete. Each listing makes NEW ModelFile objects (as the
    real one does), each serving scan a NEW set carrying a stamp (`mark` and its call number), so a test can tell
    which object reached delete(). delete() refuses like the real one would for a missing, changed or served file."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.folders: dict[str, list[tuple[str, int, int]]] = {DEST: [A, B, Q, M], OTHER: [B]}
        self.serving: set[str] = {os.path.join(DEST, Q[0])}
        self.mark = "start"
        self.calls: list[tuple] = []
        self.off_worker: list[str] = []
        self.scans: list[frozenset[str]] = []
        self.deletes: list[tuple[ModelFile, str, object]] = []
        self.delete_error: BaseException | None = None
        self.list_error: BaseException | None = None

    def _rec(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if not self.runner.in_job:
            self.off_worker.append(name)

    def names(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def list_folder(self, folder):
        self._rec("list_folder", folder)
        if self.list_error is not None:
            raise self.list_error
        return tuple(ModelFile(os.path.join(folder, n), n, s, t) for n, s, t in self.folders.get(folder, []))

    def serving_paths(self):
        self._rec("serving_paths")
        out = frozenset({key(p) for p in self.serving} | {f"<scan {self.mark} {len(self.scans) + 1}>"})
        self.scans.append(out)
        return out

    def delete(self, file, folder, serving=frozenset()):
        self._rec("delete", file, folder, serving)
        self.deletes.append((file, folder, serving))
        if self.delete_error is not None:
            raise self.delete_error
        listed = self.folders.get(os.path.dirname(file.path), [])
        now = next((x for x in listed if x[0] == file.name), None)
        if now is None:
            raise ModelFileError("missing", model_files._MESSAGES["missing"].format(name=file.name))
        if (now[1], now[2]) != (file.size, file.mtime_ns):
            raise ModelFileError("changed", model_files._MESSAGES["changed"].format(name=file.name))
        if key(file.path) in serving:
            raise ModelFileError("serving", model_files._MESSAGES["serving"].format(name=file.name))
        listed.remove(now)
        return file.size


class Env:
    def __init__(self, queued: bool = False, dest: str = DEST, read: bool = True) -> None:
        self.runner = Runner()
        self.poster = Poster(queued)
        self.files = FakeFiles(self.runner)
        self.changes = 0
        self.deleted: list[str] = []
        self.flow = FolderFlow(dest, files=self.files, run_worker=self.runner, post=self.poster,
                               on_change=self._changed, on_deleted=self.deleted.append)
        if read:
            self.read()

    def _changed(self) -> None:
        self.changes += 1

    def read(self) -> FolderFlow:
        self.flow.refresh()
        self.runner.run_all()
        self.poster.drain()
        return self.flow

    def row(self, name: str) -> FolderRow:
        return next(r for r in self.flow.rows if r.name == name)

    def asked(self, name: str = B[0]) -> ModelFile:
        """Choose name and press Delete…; returns the file the question names."""
        f = self.row(name).file
        self.flow.choose(f)
        self.flow.ask()
        return f

    def answer(self) -> None:
        self.runner.run_all()
        self.poster.drain()


def texts(flow: FolderFlow) -> list[tuple[str, str, str, bool]]:
    return [(r.name, r.note, r.size_text, r.selectable) for r in flow.rows]


# ----------------------------------------------------------------------------------------- tests
def test_reading() -> None:
    section("reading the folder")
    e = Env(read=False)
    f = e.flow
    eq("nothing is read when the flow is made (the window opens on Hugging Face)", e.files.calls, [])
    eq("before the first read: idle, no rows, the reading hint", (f.phase, f.rows, f.hint),
       ("idle", (), ff.HINT_READING))
    check("the default `files` is the model_files module",
          FolderFlow(DEST, run_worker=e.runner, post=e.poster)._files is model_files)
    f.refresh()
    eq("refresh: reading, until the worker answers", (f.phase, f.busy, len(e.runner.jobs)), ("reading", True, 1))
    check("nothing can be chosen while reading", not f.can_choose and not f.choose(None))
    e.answer()
    eq("list_folder of the folder, then one serving scan, both on the worker",
       ([c[:2] for c in e.files.calls], e.files.off_worker), ([("list_folder", DEST), ("serving_paths",)], []))
    eq("rows: name, note, size (fmt_size), choosable", texts(f), [
        (A[0], "", "5.4 GB", True), (B[0], "", "9.1 GB", True),
        (Q[0], ff.IN_USE_NOTE, "13.4 GB", False), (M[0], "", "879 MB", True)])
    eq("the in-use note reads as the mockup", ff.IN_USE_NOTE, "· in use by llama.cpp")
    eq("idle, nothing chosen, no hint with rows", (f.phase, f.chosen, f.hint, f.read_once), ("idle", None, "", True))
    check("on_change fired", e.changes >= 2, f"{e.changes}")
    e2 = Env(read=False)
    e2.files.folders[DEST] = []
    e2.read()
    eq("an empty folder: the placeholder sentence", (e2.flow.rows, e2.flow.hint),
       ((), "No .gguf files in this folder."))
    e3 = Env(read=False)
    e3.files.list_error = RuntimeError("disk on fire")
    e3.read()
    eq("a reading that raises: no rows, the generic sentence in the error colour",
       (e3.flow.rows, e3.flow.message, e3.flow.message_kind, e3.flow.phase), ((), ff.READ_FAILED, "error", "idle"))
    check("...and the traceback is logged",
          any(r.exc_info and "reading the models folder" in r.getMessage() for r in LOG.records))
    e4 = Env(read=False)
    e4.runner.fail = True
    eq("no worker: refresh says so, nothing hangs", (e4.flow.refresh(), e4.flow.phase, e4.flow.message),
       (True, "idle", ff.READ_FAILED))


def test_choosing() -> None:
    section("choosing a row")
    e = Env()
    f = e.flow
    a, b, q = e.row(A[0]).file, e.row(B[0]).file, e.row(Q[0]).file
    check("Delete… is off with nothing chosen", not f.can_delete)
    check("a row is chosen by its file", f.choose(a) and f.chosen is a and f.can_delete)
    check("a click on the chosen row keeps it chosen", f.choose(a) and f.chosen is a)
    check("another row replaces it (one at a time)", f.choose(e.row(B[0])) and f.chosen is b)
    check("the file in use by llama.cpp cannot be chosen", not f.choose(q) and f.chosen is b)
    stranger = ModelFile(os.path.join(DEST, "x.gguf"), "x.gguf", 1, 1)
    check("a file that is not a row cannot be chosen", not f.choose(stranger) and f.chosen is b)
    same_values = ModelFile(b.path, b.name, b.size, b.mtime_ns)
    check("only the listed object itself counts, not an equal copy", not f.choose(same_values) and f.chosen is b)
    check("None clears the choice", f.choose(None) and f.chosen is None and not f.can_delete)
    f.choose(b)
    e.read()
    check("a read keeps the choice on the same file (the new listing's object)",
          f.chosen is not None and f.chosen is e.row(B[0]).file and f.chosen is not b)
    e.files.serving.add(os.path.join(DEST, B[0]))
    e.read()
    eq("the chosen file is now served: the choice goes, Delete… is off", (f.chosen, f.can_delete), (None, False))
    eq("...and its row is greyed with the note", texts(f)[1], (B[0], ff.IN_USE_NOTE, "9.1 GB", False))


def test_question_and_keep() -> None:
    section("the question, Keep")
    e = Env()
    f = e.flow
    check("ask with nothing chosen does nothing", not f.ask() and f.phase == "idle" and f.question == "")
    b = e.asked(B[0])
    eq("Delete…: the question names the file and its size",
       (f.phase, f.question), ("asking", "Delete Ornith-1.5-9B-Q8_0.gguf (9.1 GB) for good? This cannot be undone."))
    eq("...the same size text as the row", ff.question_for(b),
       ff.QUESTION.format(name=b.name, size=e.row(B[0]).size_text))
    check("the question is about the chosen file", f.asked is b)
    check("while it is up: no choosing, no second question, no Delete…",
          not f.choose(e.row(A[0]).file) and f.chosen is b and not f.ask() and not f.can_delete)
    check("while it is up: no refresh, no folder change", not f.refresh() and not f.set_dest_dir(OTHER)
          and f.dest_dir == DEST)
    eq("nothing was deleted by asking", e.files.names("delete"), [])
    check("Keep: back to the list with the same row chosen",
          f.keep() and f.phase == "idle" and f.question == "" and f.chosen is b and f.asked is None)
    eq("Keep deletes nothing and starts nothing", (e.files.names("delete"), e.runner.jobs), ([], []))
    check("confirm after Keep does nothing", not f.confirm() and not e.runner.jobs)
    check("Keep without a question does nothing", not f.keep())
    long = ModelFile(os.path.join(DEST, "x" * 150 + ".gguf"), "x" * 150 + ".gguf", GIB, 1)
    short = ff.question_for(long, 40)
    check("the window may shorten a very long name, the size stays",
          "x" * 38 + "…" in short and "(1.0 GB)" in short and "x" * 40 not in short, short)
    check("the flow's own question keeps the full name", long.name in ff.question_for(long))
    eq("fmt_size: one formatter for both lists", (ff.fmt_size(int(12.2 * GIB)), ff.fmt_size(922 * MIB),
                                                  ff.fmt_size(None)), ("12.2 GB", "922 MB", "? GB"))


def test_confirm_deletes_once() -> None:
    section("Delete for good")
    e = Env()
    f = e.flow
    b = e.asked(B[0])
    listed_scan = e.files.scans[-1]
    e.files.mark = "after-confirm"
    check("Delete for good with the question up starts one worker",
          f.confirm() and len(e.runner.jobs) == 1)
    eq("deleting: nothing removed yet (it runs on the worker)", (f.phase, e.files.names("delete")), ("deleting", []))
    check("deleting: busy, the question still names the file, a message says so",
          f.busy and f.asked is b and f.message == ff.DELETING.format(name=b.name))
    check("a second confirm while deleting is refused (no second delete)", not f.confirm() and len(e.runner.jobs) == 1)
    check("while deleting: no Keep, no question, no choosing, no refresh, no folder change",
          not f.keep() and not f.ask() and not f.choose(None) and not f.refresh() and not f.set_dest_dir(OTHER))
    lists_before = len(e.files.names("list_folder"))
    e.answer()
    eq("delete ran once, on the worker", (len(e.files.deletes), e.files.off_worker), (1, []))
    got_file, got_folder, got_serving = e.files.deletes[0]
    check("delete got the very ModelFile the question named", got_file is b)
    eq("...and the folder", got_folder, DEST)
    scans_after = [s for s in e.files.scans if any("after-confirm" in x for x in s)]
    check("...and a serving set read after Delete for good was pressed (not the listing's)",
          got_serving is not listed_scan and bool(scans_after) and got_serving is scans_after[0], f"{got_serving}")
    names = [c[0] for c in e.files.calls]
    i = names.index("delete")
    eq("order on the worker: serving scan, delete, then the folder is read again",
       (names[i - 1], names[i + 1:i + 3]), ("serving_paths", ["list_folder", "serving_paths"]))
    eq("the list was read again (once)", len(e.files.names("list_folder")) - lists_before, 1)
    eq("the row is gone", [r.name for r in f.rows], [A[0], Q[0], M[0]])
    eq("no row is chosen, back to idle", (f.chosen, f.phase, f.question, f.asked), (None, "idle", "", None))
    eq("the message names what went and its size, in the info colour", (f.message, f.message_kind),
       ("Deleted Ornith-1.5-9B-Q8_0.gguf (9.1 GB).", "info"))
    eq("on_deleted once, with the path", e.deleted, [b.path])
    check("nothing else was deleted", [d[0].name for d in e.files.deletes] == [B[0]])
    check("confirm afterwards does nothing", not f.confirm() and not e.runner.jobs)
    # another program puts a file of the same name there at once: it is listed, but it is not the confirmed one
    e = Env()
    f = e.flow
    e.asked(B[0])
    real_delete = e.files.delete

    def delete_and_recreate(file, folder, serving=frozenset()):
        out = real_delete(file, folder, serving)
        e.files.folders[DEST].insert(1, (B[0], 7, 999))
        return out

    e.files.delete = delete_and_recreate      # type: ignore[method-assign]
    f.confirm()
    e.answer()
    eq("a new file of the same name is listed after the delete, and it is NOT chosen",
       ([r.name for r in f.rows], f.chosen, f.can_delete), ([A[0], B[0], Q[0], M[0]], None, False))


def test_refusals() -> None:
    section("refusals and failures")
    for kind in sorted(model_files.KINDS):
        e = Env()
        f = e.flow
        b = e.asked(B[0])
        msg = model_files._MESSAGES[kind].format(name=b.name, detail="x")
        e.files.delete_error = ModelFileError(kind, msg)
        lists_before = len(e.files.names("list_folder"))
        f.confirm()
        e.answer()
        tone = "warn" if kind in ("serving", "in_use") else "error"
        eq(f"{kind}: its sentence, {tone}", (f.message, f.message_kind), (msg, tone))
        eq(f"{kind}: delete called once, never again", len(e.files.deletes), 1)
        eq(f"{kind}: the list was read again", len(e.files.names("list_folder")) - lists_before, 1)
        check(f"{kind}: no on_deleted, the choice is kept, back to idle",
              e.deleted == [] and f.chosen is not None and f.chosen.path == b.path and f.phase == "idle"
              and f.question == "")
        check(f"{kind}: no worker left behind", not e.runner.jobs)
    # a refusal of a file that turned out to be served: the re-read greys it; it cannot stay chosen
    e = Env()
    f = e.flow
    b = e.asked(B[0])
    e.files.serving.add(b.path)                    # llama-server started with it after the listing
    e.files.mark = "after-confirm"
    f.confirm()
    e.answer()
    eq("served since the listing: the scan after confirm catches it", (f.message, f.message_kind),
       (model_files._MESSAGES["serving"].format(name=b.name), "warn"))
    later_scan = e.files.deletes[0][2]
    check("...the delete got that later scan",
          isinstance(later_scan, frozenset) and any("after-confirm" in x for x in later_scan))
    eq("...its row is greyed now and nothing is chosen", (texts(f)[1][1:], f.chosen),
       ((ff.IN_USE_NOTE, "9.1 GB", False), None))
    # something unexpected
    LOG.records.clear()
    e = Env()
    f = e.flow
    e.asked(B[0])
    e.files.delete_error = RuntimeError("boom C:\\secret\\detail")
    f.confirm()
    e.answer()
    eq("an unexpected exception: the generic sentence, error colour", (f.message, f.message_kind),
       ("Deleting failed unexpectedly; see the log.", "error"))
    check("...never the exception's own text", "boom" not in f.message)
    check("...the traceback is logged",
          any(r.exc_info and "deleting failed unexpectedly" in r.getMessage() for r in LOG.records))
    check("...once, no on_deleted, list read again, the choice kept",
          len(e.files.deletes) == 1 and e.deleted == [] and f.chosen is not None and len(f.rows) == 4)
    # the worker cannot start
    e = Env()
    f = e.flow
    e.asked(B[0])
    e.runner.fail = True
    check("no worker for the delete: the generic sentence, nothing deleted, back to idle",
          f.confirm() and f.phase == "idle" and f.message == ff.UNEXPECTED and not e.files.deletes)
    # on_deleted that raises
    e = Env()
    e.flow._on_deleted = lambda p: (_ for _ in ()).throw(RuntimeError("app"))
    e.asked(B[0])
    e.flow.confirm()
    e.answer()
    check("an on_deleted that raises is logged; the flow is fine",
          e.flow.phase == "idle" and e.flow.message.startswith("Deleted")
          and any("on_deleted" in r.getMessage() for r in LOG.records))


def test_vanished_and_changed() -> None:
    section("a file that vanished or changed between listing and delete")
    e = Env()
    f = e.flow
    e.asked(B[0])
    e.files.folders[DEST] = [x for x in e.files.folders[DEST] if x[0] != B[0]]   # gone meanwhile
    f.confirm()
    e.answer()
    eq("vanished: 'no longer in the models folder', error colour",
       (f.message, f.message_kind), (f"{B[0]} is no longer in the models folder.", "error"))
    eq("...the list read again without it, nothing chosen", ([r.name for r in f.rows], f.chosen),
       ([A[0], Q[0], M[0]], None))
    check("...one delete call, no on_deleted", len(e.files.deletes) == 1 and e.deleted == [])
    e = Env()
    f = e.flow
    b = e.asked(B[0])
    e.files.folders[DEST][1] = (B[0], B[1] + 5, B[2] + 1)        # rewritten meanwhile
    f.confirm()
    e.answer()
    check("changed: delete still got the listed object (old size and time), so it refused",
          e.files.deletes[0][0] is b and f.message == model_files._MESSAGES["changed"].format(name=B[0]))
    check("...the new listing is shown and the same name stays chosen (its new object)",
          f.chosen is not None and f.chosen.name == B[0] and f.chosen.size == B[1] + 5 and e.deleted == [])
    eq("...a new question shows the new size", (f.ask(), f.question.split("(")[1][:6]), (True, "9.1 GB"))


def test_late_answers() -> None:
    section("late answers, close, abandon")
    e = Env(queued=True)
    f = e.flow
    e.runner.run_all()
    e.poster.drain()
    f.refresh()
    first = len(e.runner.jobs)
    e.files.folders[DEST] = [A]
    f.refresh()
    eq("two reads queued", (first, len(e.runner.jobs)), (1, 2))
    e.runner.run(0)                     # the first one answers with the folder as it was then... (A only now)
    e.poster.drain()
    eq("the answer of the older read is dropped", (len(f.rows), f.phase), (4, "reading"))
    e.runner.run_all()
    e.poster.drain()
    eq("the newer one is applied", ([r.name for r in f.rows], f.phase), ([A[0]], "idle"))
    # a read that answers after close
    e = Env()
    f = e.flow
    f.refresh()
    e.files.folders[DEST] = []
    check("close while reading: allowed", f.close() and f.closed)
    changes = e.changes
    e.answer()
    eq("the read answers after close: nothing applied, no redraw", (len(f.rows), e.changes), (4, changes))
    check("closed: nothing can be read, chosen, asked or confirmed",
          not f.refresh() and not f.choose(f.rows[0].file) and not f.ask() and not f.confirm()
          and not f.set_dest_dir(OTHER) and not e.runner.jobs)
    # the question up, then the window closes: no delete
    e = Env()
    f = e.flow
    e.asked(B[0])
    check("close with the question up: closed, the question gone", f.close() and f.closed and f.question == "")
    check("confirm after close does nothing", not f.confirm() and not e.runner.jobs and not e.files.deletes)
    # a close while deleting waits for the answer
    e = Env()
    f = e.flow
    e.asked(B[0])
    f.confirm()
    check("close while deleting: refused (the window waits), closing marked", not f.close() and f.closing
          and not f.can_close)
    e.answer()
    check("...the delete answers: on_deleted once, then close is allowed", e.deleted == [f"{DEST}\\{B[0]}"]
          and f.can_close and f.close() and f.closed)
    # abandon while deleting: the worker still finishes (one delete), its answer is dropped
    e = Env()
    f = e.flow
    e.asked(B[0])
    f.confirm()
    f.abandon()
    changes = e.changes
    e.answer()
    eq("abandoned while deleting: one delete on the worker, no on_deleted, no redraw",
       (len(e.files.deletes), e.deleted, e.changes, f.phase), (1, [], changes, "deleting"))
    e = Env()
    e.flow.close()
    check("close twice is fine", e.flow.close())


def test_folder_change() -> None:
    section("another folder")
    e = Env()
    f = e.flow
    f.choose(e.row(B[0]).file)
    lists = len(e.files.names("list_folder"))
    check("set_dest_dir: taken", f.set_dest_dir(OTHER) and f.dest_dir == OTHER)
    eq("the choice and the rows go at once, the message is cleared, reading",
       (f.chosen, f.rows, f.message, f.phase, f.hint, f.can_delete), (None, (), "", "reading", ff.HINT_READING, False))
    e.answer()
    eq("the new folder is read", (e.files.names("list_folder")[lists:], [r.name for r in f.rows]),
       ([("list_folder", OTHER)], [B[0]]))
    other_b = e.asked(B[0])
    f.confirm()
    e.answer()
    eq("a delete there gets the new folder", e.files.deletes[0][1], OTHER)
    check("...and the new folder's file", e.files.deletes[0][0] is other_b and other_b.path.startswith(OTHER))
    eq("the file of the first folder is untouched", [x[0] for x in e.files.folders[DEST]], [A[0], B[0], Q[0], M[0]])
    # a read of the old folder under way is dropped
    e = Env(queued=True)
    e.answer()
    f = e.flow
    f.refresh()
    f.set_dest_dir(OTHER)
    e.runner.run(0)                     # the old folder's read
    e.poster.drain()
    eq("the old folder's late answer is dropped", (f.rows, f.phase), ((), "reading"))
    e.answer()
    eq("the new folder's answer is shown", [r.name for r in f.rows], [B[0]])
    e = Env()
    f = e.flow
    jobs_before = len(e.runner.jobs)
    check("set_dest_dir(read=False) takes the folder without reading it",
          f.set_dest_dir(OTHER, read=False) and f.dest_dir == OTHER and len(e.runner.jobs) == jobs_before
          and f.phase == "idle" and f.rows == ())
    odd: object = None
    check("an empty or odd path is refused", not f.set_dest_dir("  ") and not f.set_dest_dir(odd)  # type: ignore[arg-type]
          and f.dest_dir == OTHER)


def test_real_model_files_temp() -> None:
    """The real model_files, on two tiny files in this suite's own temp folder (and nowhere else)."""
    section("the real model_files on a temp folder")
    tmp = Path(tempfile.mkdtemp(prefix="agentstart-folderflow-models-"))
    try:
        keep_f, drop_f = tmp / "keep-Q4_K_M.gguf", tmp / "drop-Q8_0.gguf"
        keep_f.write_bytes(b"GGUF" + b"\0" * 60)
        drop_f.write_bytes(b"GGUF" + b"\0" * 40)
        (tmp / "notes.txt").write_text("not a model", encoding="utf-8")
        runner, poster = Runner(), Poster()
        gone: list[str] = []
        f = FolderFlow(str(tmp), run_worker=runner, post=poster, on_deleted=gone.append)
        f.refresh()
        runner.run_all()
        eq("lists the two .gguf files (sorted), not the .txt", [r.name for r in f.rows], [drop_f.name, keep_f.name])
        f.choose(f.rows[0].file)
        f.ask()
        f.confirm()
        runner.run_all()
        eq("Delete for good removed exactly the chosen file", (drop_f.exists(), keep_f.exists()), (False, True))
        eq("the list shows what is left; on_deleted got the path", ([r.name for r in f.rows], len(gone)),
           ([keep_f.name], 1))
        check("the message names it", f.message.startswith(f"Deleted {drop_f.name} ("))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sources() -> None:
    section("source checks")
    for name in ("folder_flow.py", "download_dialog.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        bad = [w for w in ("os.remove", "os.unlink", "unlink(", "rmtree", "send2trash", "shutil") if w in src]
        eq(f"{name} contains nothing that deletes a file itself", bad, [])
    src = (ROOT / "folder_flow.py").read_text(encoding="utf-8")
    used: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Attribute):
            v = node.value
            if (isinstance(v, ast.Name) and v.id == "files") or (isinstance(v, ast.Attribute) and v.attr == "_files"):
                used.add(node.attr)
            if isinstance(v, ast.Name) and v.id == "model_files":
                direct.add(node.attr)
    eq("folder_flow calls only list_folder, serving_paths and delete on `files` (the model_files seam)", used,
       {"list_folder", "serving_paths", "delete"})
    eq("...and uses nothing of model_files directly (only the seam's default and the two classes)", direct, set())
    check("no rename / write calls in folder_flow",
          not re.search(r"\b(os\.rename|os\.replace|\.write_|open\()", src))
    import download_dialog as dd
    check("the download list and the folder view share one fmt_size", dd.fmt_size is ff.fmt_size)


# ----------------------------------------------------------------------------------------- runner
def main() -> int:
    tests = [test_reading, test_choosing, test_question_and_keep, test_confirm_deletes_once, test_refusals,
             test_vanished_and_changed, test_late_answers, test_folder_change, test_real_model_files_temp,
             test_sources]
    try:
        for t in tests:
            try:
                t()
            except Exception:  # noqa: BLE001
                check(f"{t.__name__} raised", False, traceback.format_exc())
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
