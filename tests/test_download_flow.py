"""Self-check for download_dialog.DownloadFlow (the model-download dialog's behaviour, no Tk).
Run: .venv\\Scripts\\python.exe tests\\test_download_flow.py

Safe by construction:
  * No network at all: a FAKE hf object stands in for hf_download (only its HfError / HfFile / RepoListing classes
    and the KINDS / message table are used). Nothing is listed, downloaded, written or deleted for real.
  * No window: DownloadFlow has no Tk in it; customtkinter is only imported (no root is created).
  * Workers are a manual runner (each interleaving is deterministic); one test uses real daemon threads with a
    queue drained on the main thread, like the app's post(). No process is started; no port is opened.
  * The data dir is redirected to a throwaway temp folder (removed at the end) before anything app-side is imported.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="agentstart-dlflow-home-"))
os.environ["AGENT_START_HOME"] = str(HOME)

import hf_download  # noqa: E402  (classes and the message table only; no hf_download function is called)
from hf_download import HfError, HfFile, RepoListing  # noqa: E402

import download_dialog as dd  # noqa: E402
from download_dialog import DownloadFlow  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
GIB = 1024 ** 3
MIB = 1024 ** 2
DEST = "C:\\Models\\llama-models"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPO = "acme/Qwen3.8-27B-GGUF"


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
def F(name: str, size: int, kind: str = "model", supported: bool = True, note: str = "",
      path: str | None = None) -> HfFile:
    return HfFile(path=path or name, name=name, size=size, sha256="ab" * 32, kind=kind, supported=supported,
                  note=note)


MODEL_A = F("Qwen3.8-27B-UD-Q2_K_XL.gguf", int(9.8 * GIB))
MODEL_B = F("Qwen3.8-27B-UD-Q3_K_XL.gguf", int(12.2 * GIB))
MODEL_C = F("Qwen3.8-27B-UD-IQ4_XS.gguf", int(13.4 * GIB))
VISION = F("mmproj-BF16.gguf", int(0.9 * GIB), kind="vision")
SPLIT = F("Qwen3.8-27B-BF16-00001-of-00002.gguf", int(50.1 * GIB), kind="split", supported=False,
          note="Split into 2 parts (2 listed); split models are not supported.",
          path="BF16/Qwen3.8-27B-BF16-00001-of-00002.gguf")
ODD = F("odd-format.gguf", 300 * MIB, supported=False, note="This file uses a format Agent Start cannot run.")
LISTING = RepoListing(repo_id=REPO, commit=COMMIT, files=(MODEL_A, MODEL_B, MODEL_C, VISION, SPLIT, ODD))
TINY = F("tiny-Q4_K_M.gguf", 1 * GIB)
TINY_LISTING = RepoListing(repo_id="acme/Tiny-GGUF", commit=COMMIT, files=(TINY,))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class Runner:
    """Manual worker runner: jobs wait until the test runs them (in order, or picked)."""

    def __init__(self) -> None:
        self.jobs: list = []
        self.in_job = False

    def __call__(self, fn) -> None:
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


class FakeHf:
    """hf_download's surface: parse_repo, list_files, pending_part, discard_part, free_space, space_ok, download."""
    SPACE_MARGIN_BYTES = GIB

    def __init__(self, clock: Clock, runner: Runner | None = None) -> None:
        self.clock = clock
        self.runner = runner
        self.listings: dict[str, object] = {REPO: LISTING, "acme/Tiny-GGUF": TINY_LISTING}
        self.pending: dict[str, int] = {}
        self.free: int | None = 500 * GIB
        self.script: list = []
        self.calls: list[tuple] = []
        self.off_worker: list[str] = []
        self.threads: set[int] = set()
        self.active = 0
        self.max_active = 0
        self.download_calls = 0
        self.discarded: list[str] = []
        self.discard_error: BaseException | None = None
        self.last_progress = None
        self.last_stop: threading.Event | None = None
        self.download_kwargs: dict = {}

    def _rec(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        self.threads.add(threading.get_ident())
        if self.runner is not None and not self.runner.in_job:
            self.off_worker.append(name)

    def parse_repo(self, text):
        self._rec("parse_repo", text)
        s = text.strip()
        m = re.fullmatch(r"(?:https?://)?(?:huggingface\.co/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/\S*)?", s)
        if not m:
            raise HfError("bad_repo")
        return f"{m.group(1)}/{m.group(2)}"

    def list_files(self, repo):
        self._rec("list_files", repo)
        r = self.listings.get(repo)
        if isinstance(r, BaseException):
            raise r
        if r is None:
            raise HfError("not_found")
        return r

    def pending_part(self, dest, file, listing):
        self._rec("pending_part", dest, file.path)
        return self.pending.get(file.path, 0)

    def discard_part(self, dest, file):
        self._rec("discard_part", dest, file.path)
        if self.discard_error is not None:
            raise self.discard_error
        self.pending.pop(file.path, None)
        self.discarded.append(file.path)

    def free_space(self, dest):
        self._rec("free_space", dest)
        return self.free

    def space_ok(self, dest, size, margin_bytes=GIB):
        self._rec("space_ok", dest, size)
        return self.free is not None and self.free >= size + margin_bytes

    def download(self, listing, file, dest, *, progress=None, stop_evt=None, **kw):
        self._rec("download", file.path, dest)
        self.download_calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.last_progress, self.last_stop = progress, stop_evt
        self.download_kwargs = {"progress": progress, "stop_evt": stop_evt, **kw}
        try:
            for step in list(self.script):
                op = step[0]
                if op == "p":                       # ("p", done, seconds before it)
                    self.clock.advance(step[2] if len(step) > 2 else 0.0)
                    if progress is not None:
                        progress(step[1], file.size)
                elif op == "stop":
                    if stop_evt is not None and stop_evt.is_set():
                        raise HfError("cancelled")
                elif op == "call":
                    step[1]()
                elif op == "wait":
                    step[1].wait(10)
                elif op == "raise":
                    raise step[1]
            return os.path.join(dest, file.name)
        finally:
            self.active -= 1


class Env:
    def __init__(self, queued: bool = False, dest: str = DEST) -> None:
        self.clock = Clock()
        self.runner = Runner()
        self.poster = Poster(queued)
        self.hf = FakeHf(self.clock, self.runner)
        self.changes = 0
        self.done: list[str] = []
        self.flow = DownloadFlow(dest, hf=self.hf, run_worker=self.runner, post=self.poster,
                                 on_change=self._changed, on_done=self.done.append, clock=self.clock)
        self.runner.run_all()                       # the opening free-space read
        self.poster.drain()

    def _changed(self) -> None:
        self.changes += 1

    def listed(self, text: str = REPO) -> DownloadFlow:
        self.flow.set_repo_text(text)
        self.flow.list_now()
        self.runner.run_all()
        self.poster.drain()
        return self.flow

    def row(self, f: HfFile) -> dd.Row:
        return next(r for r in self.flow.rows if r.file == f)


# ----------------------------------------------------------------------------------------- tests
def test_formatters() -> None:
    section("formatters and the rate meter")
    for n, want in ((int(12.2 * GIB), "12.2 GB"), (int(0.9 * GIB), "922 MB"), (GIB, "1.0 GB"), (5 * MIB, "5 MB"),
                    (10, "1 MB"), ("x", "? GB")):
        eq(f"fmt_size({n!r})", dd.fmt_size(n), want)
    for b, want in ((None, ""), (0, ""), (-5, ""), (float("nan"), ""), (512 * 1024, "512 KB/s"),
                    (2.4 * MIB, "2.4 MB/s"), (41 * MIB, "41 MB/s"), (41.6 * MIB, "42 MB/s")):
        eq(f"fmt_rate({b!r})", dd.fmt_rate(b), want)
    for s, want in ((None, ""), (-1, ""), (float("inf"), ""), (10, "less than a minute left"),
                    (44, "less than a minute left"), (50, "about 1 min left"), (190, "about 3 min left"),
                    (3599, "about 1 h 00 min left"), (3900, "about 1 h 05 min left")):
        eq(f"fmt_eta({s!r})", dd.fmt_eta(s), want)
    eq("fmt_progress mockup line", dd.fmt_progress(int(4.6 * GIB), int(12.2 * GIB), 41 * MIB),
       "4.6 of 12.2 GB · 37% · 41 MB/s · about 3 min left")
    eq("fmt_progress without a rate", dd.fmt_progress(0, int(12.2 * GIB), None), "0.0 of 12.2 GB · 0%")
    eq("fmt_progress under 1 GB", dd.fmt_progress(300 * MIB, 900 * MIB, None), "300 of 900 MB · 33%")
    eq("fmt_free large", dd.fmt_free(212 * GIB), "212 GB free")
    eq("fmt_free small", dd.fmt_free(int(8.4 * GIB)), "8.4 GB free")
    eq("fmt_free unknown", dd.fmt_free(None), "")
    eq("short_folder", dd.short_folder("C:\\Users\\someone\\Downloads\\llama-models"), "Downloads\\llama-models")
    eq("short_folder drive child", dd.short_folder("D:\\Models"), "Models")
    eq("linked_file blob", dd.linked_file("https://huggingface.co/acme/X-GGUF/blob/main/sub/x-Q4.gguf"),
       "sub/x-Q4.gguf")
    eq("linked_file resolve without scheme", dd.linked_file("huggingface.co/acme/X-GGUF/resolve/main/x%20y.gguf"),
       "x y.gguf")
    eq("linked_file plain repo", dd.linked_file("acme/X-GGUF"), None)
    eq("linked_file tree", dd.linked_file("https://huggingface.co/acme/X-GGUF/tree/main"), None)

    m = dd.RateMeter(window_s=5.0, min_span_s=1.0)
    m.add(0.0, 0)
    eq("meter: one sample, no rate", m.rate(), None)
    m.add(0.5, 5 * MIB)
    eq("meter: under the minimum span, no rate", m.rate(), None)
    for i in range(2, 17):                       # steady 10 MiB/s, a sample every 0.5 s up to t=8
        m.add(i * 0.5, i * 5 * MIB)
    check("meter: steady 10 MiB/s", abs((m.rate() or 0) - 10 * MIB) < 1, repr(m.rate()))
    for i in range(1, 13):                       # stall: no new bytes for 6 s
        m.add(8.0 + i * 0.5, 80 * MIB)
    eq("meter: a stall longer than the window reads 0", m.rate(), 0.0)
    m.add(15.0, 90 * MIB)
    check("meter: smoothed over the window after the stall", 0 < (m.rate() or 0) < 10 * MIB, repr(m.rate()))
    m.add(15.5, 0)
    eq("meter: a count that goes back resets", m.rate(), None)
    m2 = dd.RateMeter()
    m2.add(0.0, 0)
    m2.add(1.0, 100 * MIB)                       # burst
    m2.add(2.0, 110 * MIB)
    m2.add(3.0, 120 * MIB)
    check("meter: a burst is smoothed (40 MiB/s over 3 s)", abs((m2.rate() or 0) - 40 * MIB) < 1, repr(m2.rate()))


def test_initial_state() -> None:
    section("opening state")
    e = Env()
    f = e.flow
    eq("phase enter", f.phase, "enter")
    eq("free space read on a worker", e.hf.off_worker, [])
    eq("free text", f.free_text, "500 GB free")
    eq("dest text", f.dest_text, "Models\\llama-models")
    check("nothing to list yet", not f.can_list and not f.list_now())
    check("nothing to download / cancel", not f.can_download and not f.can_cancel and not f.start()
          and not f.cancel())
    eq("download label", f.download_label, "Download")
    eq("no message", (f.message, f.message_kind), ("", "info"))


def test_bad_repo() -> None:
    section("bad repo text")
    e = Env()
    f = e.flow
    f.set_repo_text("  not a repo!  ")
    check("can_list with text", f.can_list)
    check("list_now starts", f.list_now())
    eq("phase listing", f.phase, "listing")
    check("one action at a time: no second listing", not f.can_list and not f.list_now())
    eq("no hf call before the worker runs", [c[0] for c in e.hf.calls if c[0] == "parse_repo"], [])
    e.runner.run_all()
    eq("parse_repo got the stripped text as data", [c for c in e.hf.calls if c[0] == "parse_repo"],
       [("parse_repo", "not a repo!")])
    eq("phase error", f.phase, "error")
    eq("bad_repo sentence, for the list area, in the error tone", (f.list_hint, f.list_hint_kind),
       (hf_download._DEFAULT_MSG["bad_repo"], "error"))
    eq("the message line stays empty", (f.message, f.message_kind), ("", "info"))
    check("usable again: can list", f.can_list)
    f.set_repo_text(REPO)
    eq("typing clears the error", (f.phase, f.message), ("enter", ""))
    e.listed()
    eq("then a good repo lists", f.phase, "list")
    eq("every hf call was on a worker", e.hf.off_worker, [])


def test_listing_ok() -> None:
    section("listing: models, a vision file, a split set, an unsupported row")
    e = Env()
    f = e.listed()
    eq("phase list", f.phase, "list")
    eq("listing kept", f.listing, LISTING)
    eq("no message", f.message, "")
    got = [(r.name, r.note, r.size_text, r.selectable) for r in f.rows]
    eq("rows", got, [
        ("Qwen3.8-27B-UD-Q2_K_XL.gguf", "", "9.8 GB", True),
        ("Qwen3.8-27B-UD-Q3_K_XL.gguf", "", "12.2 GB", True),
        ("Qwen3.8-27B-UD-IQ4_XS.gguf", "", "13.4 GB", True),
        ("mmproj-BF16.gguf", "· vision file", "922 MB", True),
        ("Qwen3.8-27B-BF16-00001-of-00002.gguf", "· 2 parts, not supported", "50.1 GB", False),
        ("odd-format.gguf", "· not supported", "300 MB", False)])
    eq("kinds", [r.kind for r in f.rows], ["model", "model", "model", "vision", "split", "model"])
    eq("pending all zero", [r.pending for r in f.rows], [0] * 6)
    check("nothing selected, nothing to download", f.selected is None and not f.can_download)
    check("pending read only for downloadable rows",
          {c[2] for c in e.hf.calls if c[0] == "pending_part"} == {MODEL_A.path, MODEL_B.path, MODEL_C.path,
                                                                   VISION.path})
    # two files with the same name in different folders show their repo path
    dup = RepoListing(repo_id="acme/Dup-GGUF", commit=COMMIT,
                      files=(F("x.gguf", GIB, path="a/x.gguf"), F("x.gguf", GIB, path="b/x.gguf")))
    e.hf.listings["acme/Dup-GGUF"] = dup
    e.listed("acme/Dup-GGUF")
    eq("same names show the path", [r.name for r in e.flow.rows], ["a/x.gguf", "b/x.gguf"])
    # a listing with no downloadable row says so
    e.hf.listings["acme/Split-GGUF"] = RepoListing(repo_id="acme/Split-GGUF", commit=COMMIT, files=(SPLIT,))
    e.listed("acme/Split-GGUF")
    eq("only split models: the warning", (e.flow.phase, e.flow.message, e.flow.message_kind),
       ("list", dd.NO_SUPPORTED, "warn"))
    # a pasted file link pre-selects that file (never downloads it)
    e2 = Env()
    e2.listed(f"https://huggingface.co/{REPO}/blob/main/{MODEL_B.name}")
    eq("file link pre-selects the file", e2.flow.selected, MODEL_B)
    eq("and downloads nothing", e2.hf.download_calls, 0)
    e3 = Env()
    e3.listed(f"https://huggingface.co/{REPO}/blob/main/{SPLIT.path}")
    eq("a link to a split file selects nothing", e3.flow.selected, None)


def test_select_rules() -> None:
    section("select rules")
    e = Env()
    f = e.flow
    f.set_repo_text(REPO)
    f.list_now()
    check("no select while listing", not f.select(MODEL_A))
    e.runner.run_all()
    check("split row cannot be picked", not f.select(SPLIT))
    eq("split: its reason is shown", (f.message, f.message_kind), (SPLIT.note, "warn"))
    check("unsupported row cannot be picked", not f.select(ODD))
    eq("unsupported: its reason", f.message, ODD.note)
    eq("still nothing selected", f.selected, None)
    check("a file of another listing cannot be picked", not f.select(TINY))
    check("vision file can be picked", f.select(VISION) and f.selected == VISION)
    eq("picking clears the warning", f.message, "")
    check("pick a model by its row", f.select(e.row(MODEL_B)) and f.selected == MODEL_B)
    check("can download", f.can_download)
    eq("label Download", f.download_label, "Download")
    check("select(None) clears", f.select(None) and f.selected is None and not f.can_download)


def test_resume_and_discard() -> None:
    section("Resume label, discard")
    e = Env()
    e.hf.pending[MODEL_B.path] = int(4.6 * GIB)
    f = e.listed()
    r = e.row(MODEL_B)
    eq("row pending", r.pending, int(4.6 * GIB))
    eq("row note", r.note, "· 37% downloaded")
    f.select(MODEL_B)
    eq("Resume label", f.download_label, "Resume")
    eq("progress line shows the kept part", f.progress_text,
       "4.6 of 12.2 GB already downloaded · Resume continues from there")
    check("bar shows the kept part", abs(f.progress_fraction - 4.6 / 12.2) < 0.01, repr(f.progress_fraction))
    f.select(MODEL_A)
    eq("another file: Download", f.download_label, "Download")
    check("no discard without a part", not f.can_discard and not f.discard_partial())
    f.select(MODEL_B)
    check("discard offered", f.can_discard)
    check("discard starts", f.discard_partial())
    check("one action at a time during discard", not f.can_download and not f.can_list and not f.start())
    e.runner.run_all()
    eq("discarded through hf on a worker", (e.hf.discarded, e.hf.off_worker), ([MODEL_B.path], []))
    eq("label back to Download", f.download_label, "Download")
    eq("discard message", (f.message, f.message_kind), (dd.DISCARDED, "info"))
    eq("row pending 0", e.row(MODEL_B).pending, 0)
    # discard refused by hf (another program's NAME.part): its sentence, the part stays
    e.hf.pending[MODEL_C.path] = GIB
    e.listed()
    f.select(MODEL_C)
    e.hf.discard_error = HfError("exists", "x.part is already in the models folder and was not made by Agent Start.")
    f.discard_partial()
    e.runner.run_all()
    eq("discard refusal shown", f.message, "x.part is already in the models folder and was not made by Agent Start.")
    eq("still Resume", f.download_label, "Resume")


def test_not_enough_space() -> None:
    section("not enough free space")
    e = Env()
    f = e.listed()
    e.hf.free = 5 * GIB
    f.select(MODEL_B)
    check("start", f.start())
    eq("phase downloading until the worker answers", f.phase, "downloading")
    e.runner.run_all()
    eq("no download attempted", e.hf.download_calls, 0)
    eq("space_ok asked with the bytes still to fetch", [c for c in e.hf.calls if c[0] == "space_ok"],
       [("space_ok", DEST, MODEL_B.size)])
    eq("back to list", f.phase, "list")
    need = MODEL_B.size + GIB
    eq("message says how much is missing", f.message, dd.NO_SPACE.format(
        need=dd.fmt_size(need), margin="1 GB", free="5.0 GB", missing=dd.fmt_size(need - 5 * GIB)))
    check("mentions 13.2 GB and 8.2 GB", "13.2 GB" in f.message and "Free up 8.2 GB more" in f.message, f.message)
    eq("tone error", f.message_kind, "error")
    check("usable again", f.can_download)
    # with a kept part only the rest counts
    e.hf.pending[MODEL_B.path] = int(4.6 * GIB)
    e.listed()
    f.select(MODEL_B)
    f.start()
    e.runner.run_all()
    check("resume: needs 8.6 GB, missing 3.6 GB", "8.6 GB free" in f.message and "3.6 GB more" in f.message,
          f.message)
    # unknown free space
    e.hf.free = None
    f.start()
    e.runner.run_all()
    eq("unknown free space", f.message, dd.NO_SPACE_UNKNOWN)
    eq("free text unknown", f.free_text, "")


def test_download_done() -> None:
    section("start -> progress -> done")
    e = Env()
    f = e.listed()
    f.select(MODEL_B)
    seen: dict = {}

    def mid() -> None:
        seen["phase"] = f.phase
        seen["rate"] = f.rate_text
        seen["eta"] = f.eta_text
        seen["text"] = f.progress_text
        seen["frac"] = f.progress_fraction
        seen["can"] = (f.can_download, f.can_list, f.can_cancel, f.can_edit, f.can_change_dest, f.can_close)
        seen["title"] = f.progress_title
        seen["edit"] = f.set_repo_text("other/repo")
        seen["start2"] = f.start()

    e.hf.script = [("p", 0, 0.0), ("p", 40 * MIB, 1.0), ("p", 80 * MIB, 1.0), ("call", mid),
                   ("p", MODEL_B.size, 1.0)]
    check("start", f.start())
    eq("message while starting", f.message, dd.STARTING)
    eq("nothing ran on the UI path", e.hf.download_calls, 0)
    e.runner.run_all()
    eq("mid: phase", seen["phase"], "downloading")
    eq("mid: rate", seen["rate"], "40 MB/s")
    eq("mid: ETA", seen["eta"], "about 5 min left")
    eq("mid: progress line", seen["text"], "0.1 of 12.2 GB · 0% · 40 MB/s · about 5 min left")
    eq("mid: title", seen["title"], MODEL_B.name)
    eq("mid: only cancel possible", seen["can"], (False, False, True, False, False, False))
    eq("mid: repo box locked", seen["edit"], False)
    eq("mid: no second start", seen["start2"], False)
    eq("hf.download got progress + stop_evt", (callable(e.hf.download_kwargs.get("progress")),
                                               isinstance(e.hf.download_kwargs.get("stop_evt"), threading.Event)),
       (True, True))
    eq("phase done", f.phase, "done")
    eq("done message", (f.message, f.message_kind), (dd.DONE_MODEL, "ok"))
    check("message says checked + Model list", "SHA-256 matches Hugging Face" in f.message
          and "now in the Model list" in f.message)
    path = os.path.join(DEST, MODEL_B.name)
    eq("on_done once with the path", e.done, [path])
    eq("result_path", f.result_path, path)
    r = e.row(MODEL_B)
    eq("row marked downloaded", (r.note, r.selectable, r.pending), ("· downloaded", False, 0))
    eq("bar full", f.progress_fraction, 1.0)
    eq("done line", f.progress_text, "12.2 of 12.2 GB · 100%")
    check("cannot download the same file again", not f.can_download)
    e.runner.run_all()
    e.poster.drain()
    eq("on_done still once", len(e.done), 1)
    check("another file can be picked", f.select(MODEL_A) and f.phase == "list" and f.message == "")
    # a vision file says where it shows up
    f.select(VISION)
    e.hf.script = [("p", VISION.size, 1.0)]
    f.start()
    e.runner.run_all()
    eq("vision done message", f.message, dd.DONE_VISION)
    eq("on_done twice in total (one per download)", len(e.done), 2)
    eq("download never ran twice at once", e.hf.max_active, 1)
    eq("every hf call was on a worker", e.hf.off_worker, [])


def test_errors_on_listing() -> None:
    section("every HfError kind on listing")
    for kind in sorted(hf_download.KINDS):
        e = Env()
        err = HfError(kind)
        e.hf.listings[REPO] = err
        f = e.listed()
        want = f"{err.message} {dd.NO_GGUF_HINT}" if kind == "no_gguf" else err.message
        ok = (f.phase == "error" and f.list_hint == want and f.message == "" and f.listing is None and f.rows == ()
              and f.can_list and f.list_hint_kind == dd._KIND_TONE.get(kind, "error"))
        check(f"listing {kind}: its sentence for the list area, usable", ok,
              f"{f.phase} {f.list_hint!r} {f.list_hint_kind} {f.message!r}")
        e.hf.listings[REPO] = LISTING
        e.listed()
        check(f"listing {kind}: listing again works, the reason is gone",
              f.phase == "list" and len(f.rows) == 6 and f.list_hint == "")
    check("only a repo without .gguf files gets the -GGUF advice",
          "-GGUF" in dd.NO_GGUF_HINT and not any("-GGUF" in m for m in hf_download._DEFAULT_MSG.values()))
    e = Env()
    e.hf.listings[REPO] = HfError("rate_limited", "Hugging Face is limiting requests from this connection. Try again "
                                                  "in 30 seconds.", retry_after_s=30)
    f = e.listed()
    eq("rate limit with seconds", (f.list_hint, f.list_hint_kind),
       ("Hugging Face is limiting requests from this connection. Try again in 30 seconds.", "warn"))
    e.hf.listings[REPO] = HfError("gated")
    e.listed()
    check("gated: login not supported, no token field", "login" in f.list_hint and "not support" in f.list_hint,
          f.list_hint)


def test_errors_on_download() -> None:
    section("every HfError kind on download")
    for kind in sorted(hf_download.KINDS):
        e = Env()
        f = e.listed()
        f.select(MODEL_B)
        err = HfError(kind)
        e.hf.script = [("p", 10 * MIB, 0.5), ("raise", err)]
        f.start()
        e.runner.run_all()
        ok = (f.phase == "list" and f.message == err.message and f.selected == MODEL_B and f.can_download
              and not f.can_cancel and e.done == [])
        check(f"download {kind}: its sentence, usable", ok, f"{f.phase} {f.message!r}")
    e = Env()
    f = e.listed()
    f.select(MODEL_B)
    e.hf.script = [("raise", HfError("cancelled"))]
    f.start()
    e.runner.run_all()
    eq("cancelled is not an error", f.message_kind, "info")


def test_cancel() -> None:
    section("cancel mid-download")
    e = Env()
    f = e.listed()
    f.select(MODEL_B)
    seen: dict = {}

    def press_cancel() -> None:
        seen["first"] = f.cancel()
        seen["second"] = f.cancel()
        seen["msg"] = f.message
        seen["can"] = (f.can_cancel, f.can_download, f.can_close, f.phase)
        e.hf.pending[MODEL_B.path] = int(1.1 * GIB)     # what hf_download keeps

    e.hf.script = [("p", GIB, 1.0), ("call", press_cancel), ("p", int(1.1 * GIB), 1.0), ("stop",),
                   ("p", 2 * GIB, 1.0)]
    f.start()
    e.runner.run_all()
    eq("cancel accepted once", (seen["first"], seen["second"]), (True, False))
    eq("cancelling message", seen["msg"], dd.CANCELLING)
    eq("while stopping: nothing else, and no close until it stopped", seen["can"], (False, False, False, "downloading"))
    check("stop event set", e.hf.last_stop is not None and e.hf.last_stop.is_set())
    eq("phase usable", f.phase, "list")
    eq("says the part is kept", f.message, hf_download._DEFAULT_MSG["cancelled"])
    check("message mentions kept", "kept" in f.message)
    eq("Resume now", f.download_label, "Resume")
    eq("row pending", e.row(MODEL_B).pending, int(1.1 * GIB))
    check("can resume", f.can_download and not f.can_cancel)
    eq("no on_done", e.done, [])
    # resume: the re-hash of the kept part shows as checking, then the bytes flow
    seen2: dict = {}

    def during_check() -> None:
        seen2["text"] = f.progress_text
        seen2["rate"] = f.rate_text
        seen2["frac"] = f.progress_fraction

    def after_check() -> None:
        seen2["text2"] = f.progress_text
        seen2["rate2"] = f.rate_text

    kept = int(1.1 * GIB)
    e.hf.script = [("p", int(0.5 * GIB), 0.1), ("call", during_check), ("p", kept, 0.1),
                   ("p", kept, 0.0), ("p", kept + 20 * MIB, 1.0), ("p", kept + 40 * MIB, 1.0), ("call", after_check),
                   ("p", MODEL_B.size, 1.0)]
    f.start()
    e.runner.run_all()
    eq("checking line", seen2["text"], "Checking the part already downloaded · 0.5 of 1.1 GB")
    eq("no rate while checking", (seen2["rate"], seen2["frac"]), ("", 0.0))
    check("after the check: a normal line with a 20 MB/s rate",
          seen2["text2"].startswith("1.1 of 12.2 GB · 9% · 20 MB/s · about ") and seen2["rate2"] == "20 MB/s",
          repr(seen2.get("text2")))
    eq("resumed download done", (f.phase, len(e.done)), ("done", 1))


def test_restart_from_zero() -> None:
    section("resume refused by the server: the count restarts")
    e = Env()
    e.hf.pending[MODEL_A.path] = 2 * GIB
    f = e.listed()
    f.select(MODEL_A)
    seen: dict = {}
    e.hf.script = [("p", GIB, 0.2), ("p", 0, 0.2), ("p", 10 * MIB, 1.0), ("p", 20 * MIB, 1.0),
                   ("call", lambda: seen.update(text=f.progress_text, rate=f.rate_text)), ("raise", HfError("network"))]
    f.start()
    e.runner.run_all()
    check("a restart leaves the checking state and measures afresh",
          seen["text"].startswith("0.0 of 9.8 GB · 0% · 10 MB/s") and seen["rate"] == "10 MB/s", repr(seen))


def test_close_during_download() -> None:
    section("close during a download cancels first")
    e = Env()
    f = e.listed()
    f.select(MODEL_B)
    seen: dict = {}

    def press_close() -> None:
        seen["r1"] = f.close()
        seen["r2"] = f.close()
        seen["state"] = (f.closing, f.closed, f.message, f.can_close)
        seen["stop"] = e.hf.last_stop.is_set() if e.hf.last_stop else None
        e.hf.pending[MODEL_B.path] = 3 * GIB

    e.hf.script = [("p", 3 * GIB, 1.0), ("call", press_close), ("stop",), ("p", 4 * GIB, 1.0)]
    f.start()
    e.runner.run_all()
    eq("close refused while it runs", (seen["r1"], seen["r2"]), (False, False))
    eq("closing state", seen["state"], (True, False, dd.CLOSING, False))
    eq("the download was told to stop", seen["stop"], True)
    check("stopped: close is possible now", f.can_close and f.phase == "list")
    eq("the part is kept (Resume next time)", (e.row(MODEL_B).pending, f.download_label), (3 * GIB, "Resume"))
    check("close() now True", f.close() and f.closed)
    changes = e.changes
    prog = e.hf.last_progress
    assert prog is not None
    prog(int(3.5 * GIB), MODEL_B.size)                  # a late progress callback after close
    e.poster.drain()
    eq("late progress after close changes nothing", (e.changes, f.done_bytes, f.phase), (changes, 3 * GIB, "list"))
    check("nothing works after close", not f.list_now() and not f.start() and not f.select(MODEL_A)
          and not f.set_repo_text("x/y") and not f.set_dest_dir("D:\\Other"))
    eq("no on_done", e.done, [])


def test_abandon() -> None:
    section("abandon (owner destroyed) during a download")
    e = Env(queued=True)
    f = e.listed()
    e.poster.drain()
    f.select(MODEL_B)
    f.start()
    f.abandon()
    check("abandon stops the download", f.closed and f._stop is not None and f._stop.is_set())
    changes = e.changes
    e.hf.script = [("p", GIB, 1.0), ("stop",)]
    e.runner.run_all()
    e.poster.drain()
    eq("late answers dropped, no on_change", e.changes, changes)
    eq("no on_done", e.done, [])
    eq("download saw the stop", e.hf.last_stop.is_set() if e.hf.last_stop else None, True)
    # a download that finishes despite the stop: still no callback into a gone window
    e2 = Env(queued=True)
    f2 = e2.listed()
    e2.poster.drain()
    f2.select(MODEL_A)
    f2.start()
    f2.abandon()
    e2.hf.script = [("p", MODEL_A.size, 1.0)]
    e2.runner.run_all()
    e2.poster.drain()
    eq("finished after abandon: no on_done, no change", (e2.done, f2.phase), ([], "downloading"))


def test_late_listing() -> None:
    section("late listing answers are dropped")
    e = Env()
    f = e.flow
    f.set_repo_text(REPO)
    f.list_now()
    f.set_repo_text("acme/Tiny-GGUF")                 # typed another repo while listing
    eq("typing abandons the listing", (f.phase, f.rows, f.can_list), ("enter", (), True))
    changes = e.changes
    e.runner.run_all()
    eq("the old answer is dropped", (f.phase, f.listing, e.changes), ("enter", None, changes))
    # two listings in flight, answered out of order
    f.set_repo_text(REPO)
    f.list_now()
    f.set_repo_text("acme/Tiny-GGUF")
    f.list_now()
    e.runner.run(1)                                  # Tiny answers first
    eq("the current listing shows", (f.phase, f.listing), ("list", TINY_LISTING))
    e.runner.run_all()                               # the older one answers late
    eq("the late one is dropped", f.listing, TINY_LISTING)
    # typing another repo while a list is shown drops it; typing it back needs a new listing
    f.set_repo_text(REPO)
    eq("another repo drops the shown list", (f.phase, f.rows), ("enter", ()))
    # close while listing
    e2 = Env()
    e2.flow.set_repo_text(REPO)
    e2.flow.list_now()
    check("close while listing is immediate", e2.flow.close())
    changes = e2.changes
    e2.runner.run_all()
    eq("listing answer after close dropped", (e2.changes, e2.flow.listing), (changes, None))


def test_unexpected() -> None:
    section("unexpected exceptions: one generic sentence, logged")
    e = Env()
    e.hf.listings[REPO] = ValueError("secret internal detail")
    LOG.records.clear()
    f = e.listed()
    eq("listing: generic sentence", (f.phase, f.list_hint, f.list_hint_kind, f.message),
       ("error", dd.UNEXPECTED_LIST, "error", ""))
    check("listing: no exception text shown", "secret" not in f.list_hint + f.message)
    check("listing: logged with the traceback", any(r.levelno == logging.ERROR and r.exc_info for r in LOG.records))
    e3 = Env()

    def no_worker(fn) -> None:
        raise RuntimeError("can't start new thread")

    e3.flow._run_worker = no_worker
    e3.flow.set_repo_text(REPO)
    e3.flow.list_now()
    eq("a listing that cannot start: the generic sentence in the list area, and usable again",
       (e3.flow.phase, e3.flow.list_hint, e3.flow.message, e3.flow.can_list), ("error", dd.UNEXPECTED_LIST, "", True))
    e.hf.listings[REPO] = LISTING
    e.listed()
    f.select(MODEL_B)
    e.hf.script = [("p", 5 * MIB, 1.0), ("raise", RuntimeError("boom at 0x1234"))]
    LOG.records.clear()
    f.start()
    e.runner.run_all()
    eq("download: the generic sentence", f.message, "The download failed unexpectedly; see the log.")
    check("download: no exception text", "boom" not in f.message and "RuntimeError" not in f.message)
    check("download: log.exception", any(r.levelno == logging.ERROR and r.exc_info for r in LOG.records))
    eq("download: usable", (f.phase, f.can_download), ("list", True))
    # on_done / on_change raising does not break the flow
    e2 = Env()
    f2 = e2.listed()
    f2._on_done = lambda p: (_ for _ in ()).throw(OSError("callback broke"))
    f2.select(MODEL_A)
    e2.hf.script = [("p", MODEL_A.size, 1.0)]
    f2.start()
    e2.runner.run_all()
    eq("a failing on_done is logged, the flow is done", f2.phase, "done")


def test_never_twice() -> None:
    section("hf.download never runs twice at once")
    e = Env()
    f = e.listed()
    f.select(MODEL_B)
    check("first start", f.start())
    check("second start refused", not f.start())
    f.cancel()
    check("start after cancel (not yet stopped) refused", not f.start())
    eq("one download job queued", sum(1 for _ in e.runner.jobs), 1)
    e.hf.script = [("stop",)]
    e.runner.run_all()
    eq("one call", (e.hf.download_calls, e.hf.max_active), (1, 1))

    # real threads + a queued post drained on this (the "UI") thread, like the app
    section("threads: workers on threads, state changes on the UI thread")
    clock = Clock()
    hf = FakeHf(clock, None)
    q: queue.SimpleQueue = queue.SimpleQueue()
    ui = threading.get_ident()
    change_threads: set[int] = set()
    done: list[str] = []

    def post(fn, *args) -> None:
        q.put((fn, args))

    def drain_until(cond, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                fn, args = q.get(timeout=0.02)
                fn(*args)
            except queue.Empty:
                pass
            if cond():
                return True
        return bool(cond())

    def run_worker(fn) -> None:
        threading.Thread(target=fn, daemon=True, name="test-hf-worker").start()

    f = DownloadFlow(DEST, hf=hf, run_worker=run_worker, post=post,
                     on_change=lambda: change_threads.add(threading.get_ident()), on_done=done.append, clock=clock)
    drain_until(lambda: f.free_bytes is not None)
    f.set_repo_text(REPO)
    f.list_now()
    check("threaded listing arrives", drain_until(lambda: f.phase == "list"))
    f.select(MODEL_C)
    gate = threading.Event()
    hf.script = [("p", 0, 0.0), ("wait", gate), ("p", MODEL_C.size, 1.0)]
    check("threaded start", f.start())
    refused = sum(1 for _ in range(50) if f.start())
    eq("50 more starts while it runs: all refused", refused, 0)
    drain_until(lambda: hf.active == 1, 2.0)
    eq("exactly one download running", (hf.active, hf.download_calls), (1, 1))
    gate.set()
    check("threaded download finishes", drain_until(lambda: f.phase == "done"))
    eq("on_done once", len(done), 1)
    eq("max one download at a time", hf.max_active, 1)
    check("no hf call on the UI thread", ui not in hf.threads, repr(hf.threads))
    eq("every on_change on the UI thread", change_threads, {ui})


def test_dest_dir() -> None:
    section("set_dest_dir")
    e = Env()
    f = e.listed()
    e.hf.free = 80 * GIB
    e.hf.pending[MODEL_C.path] = GIB
    check("change folder while idle", f.set_dest_dir("D:\\Other\\Models"))
    eq("free unknown until the worker answers", f.free_text, "")
    e.runner.run_all()
    eq("free re-read for the new folder", (f.dest_text, f.free_text), ("Other\\Models", "80 GB free"))
    eq("kept parts re-read for the new folder", e.row(MODEL_C).pending, GIB)
    check("re-read on a worker with the new folder", ("free_space", "D:\\Other\\Models") in e.hf.calls)
    f.select(MODEL_A)
    f.start()
    check("not while downloading", not f.can_change_dest and not f.set_dest_dir("E:\\X"))
    e.hf.script = [("p", MODEL_A.size, 1.0)]
    e.runner.run_all()
    eq("downloaded into the new folder", e.done, [os.path.join("D:\\Other\\Models", MODEL_A.name)])
    check("empty path refused", not f.set_dest_dir("  "))
    # a free-space answer for an older folder is dropped
    e2 = Env(queued=True)
    e2.flow.set_dest_dir("D:\\A")
    e2.flow.set_dest_dir("D:\\B")
    e2.hf.free = 7 * GIB
    e2.runner.run_all()
    e2.poster.drain()
    eq("only the newest folder's free space", (e2.flow.dest_text, e2.flow.free_text), ("B", "7.0 GB free"))


def test_source_tripwire() -> None:
    section("source tripwire")
    text = (ROOT / "download_dialog.py").read_text(encoding="utf-8")
    for bad in ("subprocess", "os.system", "os.startfile", "shell=True", "webbrowser", "Popen", "HF_TOKEN",
                "Authorization"):
        check(f"download_dialog.py has no {bad!r}", bad not in text)
    check("no str(exc) of an arbitrary exception in messages", "str(e)" not in text and "str(exc)" not in text)


# ----------------------------------------------------------------------------------------- main
def main() -> int:
    t0 = time.monotonic()
    try:
        for fn in (test_formatters, test_initial_state, test_bad_repo, test_listing_ok, test_select_rules,
                   test_resume_and_discard, test_not_enough_space, test_download_done, test_errors_on_listing,
                   test_errors_on_download, test_cancel, test_restart_from_zero, test_close_during_download,
                   test_abandon, test_late_listing, test_unexpected, test_never_twice, test_dest_dir,
                   test_source_tripwire):
            try:
                fn()
            except Exception:
                traceback.print_exc()
                check(f"{fn.__name__} raised", False, "exception")
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
        check("temp home removed", not HOME.exists(), str(HOME))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nRESULT: {passed} passed, {len(failed)} failed ({time.monotonic() - t0:.1f} s)")
    for n in failed:
        print(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
