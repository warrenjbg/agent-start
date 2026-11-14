"""DownloadDialog: "Download a model" (opened from Settings > Model). Paste a Hugging Face repo (owner/name or a
huggingface.co link), see its .gguf files with their sizes, tick one or more and download them into the models
folder, one after another in list order, with progress, cancel and resume. All network and file work is
hf_download's; this module only drives it and shows it.

Two parts:
  * DownloadFlow - the whole behaviour, no Tk: a small state machine over hf_download. Every hf_download call runs
    on a worker (run_worker); every state change happens on the UI thread (post); the view redraws from the public
    state on on_change(). A late answer of an older request (another repo typed meanwhile, the window closed) is
    dropped by a sequence number. One action at a time; hf_download.download never runs twice at once: the files of
    a batch are fetched one by one, each on its own worker started when the one before has answered.
  * DownloadDialog - a thin CTkToplevel view over one DownloadFlow and one folder_flow.FolderFlow (the second
    view, "In this folder"), in the house style (history_dialog / settings_dialog). No Tk call is ever made off the
    main thread.

Layout (420 logical px wide, fixed height, as the v1.13 mockup): title + the view switch "Hugging Face | In this
folder" (the window opens on Hugging Face); repo box + List files; the
file list (scrolls when long; a click ticks / unticks a row; split / unsupported rows are greyed and cannot be
ticked); "To <folder> · N GB free · Change…"; Close + Download (Resume when the one ticked file has a kept partial
file; "Download N files" with several); a hairline; the file being fetched ("File 2 of 3: <name>" in a batch), its
progress bar, "4.6 of 12.2 GB · 38% · 41 MB/s · about 3 min left" and Cancel download (or Discard partial download,
one ticked file only); the message.

"In this folder" (FolderFlow; the owner-approved mockup): heading "Models in this folder"; no repo row, the list 38 px
taller in its place; the folder's .gguf files with their sizes, one chosen at a time (round marks), a file a running
llama-server serves greyed and not choosable; "In <folder> · N GB free · Change…"; Close + Delete…; under the
hairline nothing until Delete… asks "Delete <name> (<size>) for good? This cannot be undone." with Keep and Delete
for good. The file is deleted by model_files.delete only (FolderFlow calls it); this module deletes nothing.

Policy (on top of hf_download's own): gated and private repos only show hf_download's sentence (no login, no token).
Nothing downloaded is executed or opened here: the finished path only goes to on_done(path) as data. Repo text, file
names and paths are only ever passed as plain arguments to hf_download functions.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
import urllib.parse
from collections import deque
from dataclasses import dataclass
from tkinter import filedialog
from typing import Any, Callable

import customtkinter as ctk

import hf_download
import model_files
import theme as T
from folder_flow import FolderFlow, fmt_size, question_for
from hf_download import HfError, HfFile, RepoListing
from sweep_dialog import center_over, later, release_modal, style_dialog

log = logging.getLogger("agent_start")

PHASES = ("enter", "listing", "list", "downloading", "done", "error")
MESSAGE_KINDS = ("info", "warn", "error", "ok")

GIB = 1024 ** 3
MIB = 1024 ** 2

# ---- sentences (hf_download's HfError.message is shown as it is for every refusal / failure)
UNEXPECTED = "The download failed unexpectedly; see the log."
UNEXPECTED_LIST = "Listing the files failed unexpectedly; see the log."
DONE_MODEL = "Downloaded and checked (SHA-256 matches Hugging Face). It is now in the Model list."
DONE_VISION = ("Downloaded and checked (SHA-256 matches Hugging Face). It is now in the Vision list of the models in "
               "that folder.")
DONE_MANY = "Downloaded and checked {n} files (SHA-256 matches Hugging Face); they are now in the lists."
CANCELLING = "Cancelling the download…"
CLOSING = "Stopping the download first; the window closes when it has stopped. The partial file is kept."
STARTING = "Starting the download…"
DISCARDED = "The partial download was deleted."
NO_SUPPORTED = "This repo has no single-file .gguf model that Agent Start can download."
NO_SPACE = ("Not enough free space: this download needs {need} free (including a {margin} margin) and the drive has "
            "{free}. Free up {missing} more and try again.")
NO_SPACE_UNKNOWN = "Could not check the free space on the models drive."
HINT_ENTER = "Paste owner/name or a huggingface.co link, then press List files."
HINT_LISTING = "Listing the files…"
HINT_NONE = "No files listed."
# Shown in the list area under the no-.gguf reason: the Hub keeps its quants in repos whose name ends in -GGUF,
# and the bare reason leaves the user without a next step.
NO_GGUF_HINT = "On Hugging Face, .gguf models live in repos whose name ends in -GGUF. Look for one of those."

# HfError kind -> message colour (everything not named here is an error)
_KIND_TONE = {"cancelled": "info", "timeout": "warn", "rate_limited": "warn", "busy": "warn", "exists": "warn"}

RATE_WINDOW_S = 5.0            # smoothed rate: bytes over the last few seconds
RATE_MIN_SPAN_S = 1.0          # no rate (and no ETA) until the samples span at least this long

_SPLIT_N_RE = re.compile(r"-[0-9]{5}-of-([0-9]{5})\.gguf$", re.IGNORECASE)


# ================================================================================================ pure formatters
# fmt_size ('12.2 GB', '922 MB') lives in folder_flow, which shows the same sizes; it is imported above.

def fmt_free(n: int | None) -> str:
    """Free space for the folder line: '212 GB free', '8.4 GB free'; '' when unknown."""
    if n is None or n < 0:
        return ""
    if n >= 10 * GIB:
        return f"{n / GIB:.0f} GB free"
    return f"{fmt_size(n)} free"


def fmt_amount(done: int, total: int) -> str:
    """'4.6 of 12.2 GB' (MB for files under 1 GB)."""
    if total >= GIB:
        return f"{done / GIB:.1f} of {total / GIB:.1f} GB"
    return f"{round(done / MIB)} of {max(1, round(total / MIB))} MB"


def fmt_rate(bps: float | None) -> str:
    """'41 MB/s', '2.4 MB/s', '512 KB/s'; '' when unknown."""
    if bps is None or not math.isfinite(bps) or bps <= 0:
        return ""
    if bps < MIB:
        return f"{max(1, round(bps / 1024))} KB/s"
    if bps < 10 * MIB:
        return f"{bps / MIB:.1f} MB/s"
    return f"{bps / MIB:.0f} MB/s"


def fmt_eta(seconds: float | None) -> str:
    """'less than a minute left', 'about 3 min left', 'about 1 h 05 min left'; '' when unknown."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return ""
    if seconds < 45:
        return "less than a minute left"
    m = max(1, round(seconds / 60.0))
    if m < 60:
        return f"about {m} min left"
    h, mm = divmod(m, 60)
    return f"about {h} h {mm:02d} min left"


def fmt_progress(done: int, total: int, rate: float | None) -> str:
    """'4.6 of 12.2 GB · 38% · 41 MB/s · about 3 min left' (rate and ETA only once known)."""
    total = max(1, int(total))
    done = max(0, min(int(done), total))
    parts = [fmt_amount(done, total), f"{done * 100 // total}%"]
    r = fmt_rate(rate)
    if r:
        parts.append(r)
        eta = fmt_eta((total - done) / rate) if rate else ""
        if eta:
            parts.append(eta)
    return " · ".join(parts)


def short_folder(path: str) -> str:
    """The last two parts of a folder ('Downloads\\llama-models'), as the folder line shows it."""
    p = os.path.normpath(str(path or ""))
    head, tail = os.path.split(p)
    parent = os.path.basename(head)
    if not tail:
        return p
    return f"{parent}\\{tail}" if parent else tail


def linked_file(text: str) -> str | None:
    """The repo file a pasted /blob/<rev>/<path> or /resolve/<rev>/<path> link names (to pre-select it in the list;
    it is never downloaded blindly), else None. Pure string work; the repo itself comes from hf.parse_repo."""
    s = str(text or "").strip()
    if "/blob/" not in s and "/resolve/" not in s:
        return None
    if "://" not in s:
        s = "https://" + s
    try:
        segs = urllib.parse.urlsplit(s).path.split("/")[1:]
    except ValueError:
        return None
    if len(segs) < 5 or segs[2] not in ("blob", "resolve"):
        return None
    path = "/".join(urllib.parse.unquote(x) for x in segs[4:])
    return path or None


class RateMeter:
    """Smoothed transfer rate: bytes per second over the last RATE_WINDOW_S seconds of (time, bytes) samples."""

    def __init__(self, window_s: float = RATE_WINDOW_S, min_span_s: float = RATE_MIN_SPAN_S) -> None:
        self.window_s = window_s
        self.min_span_s = min_span_s
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def add(self, t: float, done: int) -> None:
        if self._samples and done < self._samples[-1][1]:
            self._samples.clear()                     # the count went back (a restart from zero): start over
        self._samples.append((t, done))
        cutoff = t - self.window_s
        while len(self._samples) >= 2 and self._samples[1][0] <= cutoff:
            self._samples.popleft()                   # keep one sample at or before the window's start

    def rate(self) -> float | None:
        if len(self._samples) < 2:
            return None
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        span = t1 - t0
        if span < self.min_span_s:
            return None
        return max(0.0, (d1 - d0) / span)


# ================================================================================================ the behaviour
@dataclass(frozen=True)
class Row:
    """One file in the list, as shown."""
    file: HfFile
    name: str             # what the row shows (the file name; the repo path when two files share a name)
    size_text: str        # '12.2 GB'
    kind: str             # 'model' | 'vision' | 'split'
    supported: bool
    note: str             # '· vision file', '· 2 parts, not supported', '· downloaded', ...
    pending: int          # bytes of a kept, resumable partial file (0 = none)
    selectable: bool


def _row_note(f: HfFile, pending: int, downloaded: bool) -> str:
    if downloaded:
        return "· downloaded"
    if f.kind == "split":
        m = _SPLIT_N_RE.search(f.name)
        return f"· {int(m.group(1))} parts, not supported" if m else "· split, not supported"
    if not f.supported:
        return "· not supported"
    base = "· vision file" if f.kind == "vision" else ""
    if pending > 0:
        pct = min(99, pending * 100 // max(1, f.size))
        return (base + " " if base else "") + f"· {pct}% downloaded"
    return base


class DownloadFlow:
    """The download dialog's behaviour, without Tk. Every method and every on_change / on_done call runs on the UI
    thread; hf calls run inside run_worker(fn); their answers come back through post(fn, *args)."""

    def __init__(self, dest_dir: str, *, hf: Any = hf_download,
                 run_worker: Callable[[Callable[[], None]], None],
                 post: Callable[..., None],
                 on_change: Callable[[], None] | None = None,
                 on_done: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._hf = hf
        self._run_worker = run_worker
        self._post = post
        self._on_change = on_change
        self._on_done = on_done
        self._clock = clock
        self._margin = int(getattr(hf, "SPACE_MARGIN_BYTES", hf_download.SPACE_MARGIN_BYTES))

        self.dest_dir = str(dest_dir)
        self.free_bytes: int | None = None
        self.phase = "enter"
        self.repo_text = ""
        self.listing: RepoListing | None = None
        self.rows: tuple[Row, ...] = ()
        self.done_bytes = 0
        self.total_bytes = 0
        self.rate_bps: float | None = None
        self.message = ""
        self.message_kind = "info"
        self.list_hint = ""               # shown in the empty list area when a listing failed ('' = no reason)
        self.list_hint_kind = "error"     # its tone, one of MESSAGE_KINDS
        self.result_path: str | None = None
        self.closing = False               # close() was asked while a download ran: the window closes after it
        self.closed = False

        self._seq = 0                      # bumped by every new request, abandon and close: older answers drop
        self._info_seq = 0                 # the free-space refresh
        self._job: str | None = None       # 'list' | 'download' | 'discard' while one runs (one at a time)
        self._listed_text = ""             # the repo text the shown list belongs to
        self._pending: dict[str, int] = {}   # repo path -> kept partial bytes
        self._downloaded: set[str] = set()   # repo paths downloaded in this window (for this listing)
        self._ticks: list[str] = []          # repo paths of the ticked rows (the work order is the list order)
        self._shown: HfFile | None = None    # the file a finished download leaves in the progress area
        self._batch: tuple[HfFile, ...] = ()  # the files of the running (or last) Download, in list order
        self._batch_pos = 0                  # index in _batch of the file being fetched
        self._stop: threading.Event | None = None
        self._cancel_requested = False
        self._dl_file: HfFile | None = None
        self._resume_from = 0
        self._checking = False             # re-hashing a kept partial file before the rest is fetched
        self._last_done = 0
        self._meter = RateMeter()
        self._refresh_info()

    # ------------------------------------------------------------------ derived state
    @property
    def busy(self) -> bool:
        return self._job is not None

    @property
    def can_list(self) -> bool:
        return not self.closed and self._job is None and bool(self.repo_text.strip())

    @property
    def can_edit(self) -> bool:
        return not self.closed and self._job != "download"

    @property
    def ticked(self) -> tuple[HfFile, ...]:
        """The ticked files in list order: the order Download fetches them in, whatever order they were ticked."""
        return tuple(r.file for r in self.rows if r.file.path in self._ticks)

    @property
    def selected(self) -> HfFile | None:
        """The file the progress area is about: the one ticked file; with none ticked after a finished download,
        the file that finished last; otherwise (nothing, or several ticked) None."""
        t = self.ticked
        if len(t) == 1:
            return t[0]
        if not t and self.phase == "done":
            return self._shown
        return None

    def _selected_row(self) -> Row | None:
        sel = self.selected
        if sel is None:
            return None
        return next((r for r in self.rows if r.file == sel), None)

    @property
    def can_download(self) -> bool:
        return (not self.closed and self._job is None and self.phase in ("list", "done")
                and any(r.selectable for r in self.rows if r.file.path in self._ticks))

    @property
    def download_label(self) -> str:
        n = len(self.ticked)
        if n > 1:
            return f"Download {n} files"
        row = self._selected_row()
        return "Resume" if n == 1 and row is not None and row.pending > 0 and row.selectable else "Download"

    @property
    def can_cancel(self) -> bool:
        return self._job == "download" and not self._cancel_requested

    @property
    def can_discard(self) -> bool:
        row = self._selected_row()
        return (not self.closed and self._job is None and self.phase in ("list", "done") and row is not None
                and row.pending > 0)

    @property
    def can_change_dest(self) -> bool:
        return not self.closed and self._job is None

    @property
    def can_close(self) -> bool:
        """close() would return True now."""
        return self.closed or self._job != "download"

    @property
    def free_text(self) -> str:
        return fmt_free(self.free_bytes)

    @property
    def dest_text(self) -> str:
        return short_folder(self.dest_dir)

    @property
    def rate_text(self) -> str:
        return fmt_rate(self.rate_bps) if self.phase == "downloading" and not self._checking else ""

    @property
    def eta_text(self) -> str:
        if self.phase != "downloading" or self._checking or not self.rate_bps:
            return ""
        return fmt_eta(max(0, self.total_bytes - self.done_bytes) / self.rate_bps)

    @property
    def batch_size(self) -> int:
        """How many files the running Download fetches (0 when none runs)."""
        return len(self._batch) if self._job == "download" else 0

    @property
    def batch_index(self) -> int:
        """1-based number of the file being fetched in the running Download (0 when none runs)."""
        return self._batch_pos + 1 if self._job == "download" else 0

    @property
    def progress_title(self) -> str:
        if self.phase == "downloading":
            f = self._dl_file
            if f is None:
                return ""
            n = len(self._batch)
            return f"File {self._batch_pos + 1} of {n}: {f.name}" if n > 1 else f.name
        f = self.selected
        return f.name if f is not None else ""

    @property
    def progress_text(self) -> str:
        if self.phase == "downloading":
            if self._job == "download" and self.total_bytes and self._checking:
                return f"Checking the part already downloaded · {fmt_amount(self.done_bytes, self._resume_from)}"
            if self.total_bytes and (self.done_bytes or self.rate_bps):
                return fmt_progress(self.done_bytes, self.total_bytes, None if self._checking else self.rate_bps)
            return ""
        row = self._selected_row()
        if row is None:
            return ""
        if self.phase == "done" and self.result_path and row.file.path in self._downloaded:
            return fmt_progress(row.file.size, row.file.size, None)
        if row.pending > 0:
            return f"{fmt_amount(row.pending, row.file.size)} already downloaded · Resume continues from there"
        return ""

    @property
    def progress_fraction(self) -> float:
        if self.phase == "downloading":
            if self._checking or not self.total_bytes:
                return 0.0
            return max(0.0, min(1.0, self.done_bytes / self.total_bytes))
        row = self._selected_row()
        if row is None:
            return 0.0
        if row.file.path in self._downloaded:
            return 1.0
        return max(0.0, min(1.0, row.pending / max(1, row.file.size)))

    # ------------------------------------------------------------------ plumbing
    def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - a view problem must not break the flow
            log.exception("download dialog redraw failed")

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
            log.exception("download dialog: worker could not be started")
            return False

    def _build_rows(self) -> None:
        listing = self.listing
        if listing is None:
            self.rows = ()
            return
        names: dict[str, int] = {}
        for f in listing.files:
            names[f.name.casefold()] = names.get(f.name.casefold(), 0) + 1
        rows = []
        for f in listing.files:
            done = f.path in self._downloaded
            pending = 0 if done or not f.supported else max(0, int(self._pending.get(f.path, 0)))
            rows.append(Row(file=f, name=f.path if names[f.name.casefold()] > 1 else f.name, size_text=fmt_size(f.size),
                            kind=f.kind, supported=f.supported, note=_row_note(f, pending, done), pending=pending,
                            selectable=f.supported and f.kind != "split" and not done))
        self.rows = tuple(rows)
        self._ticks = [p for p in self._ticks if any(r.file.path == p and r.selectable for r in self.rows)]
        if self._shown is not None and all(r.file != self._shown for r in self.rows):
            self._shown = None

    def _pending_of(self, listing: RepoListing, dest: str) -> dict[str, int]:
        """Worker: kept partial bytes per supported file (hf.pending_part never raises)."""
        out: dict[str, int] = {}
        for f in listing.files:
            if f.supported and f.kind != "split":
                try:
                    n = int(self._hf.pending_part(dest, f, listing) or 0)
                except Exception:  # noqa: BLE001 - a fake / future hf that raises: treat as none
                    log.debug("pending_part failed", exc_info=True)
                    n = 0
                if n > 0:
                    out[f.path] = n
        return out

    def _free_of(self, dest: str) -> int | None:
        try:
            v = self._hf.free_space(dest)
            return int(v) if v is not None else None
        except Exception:  # noqa: BLE001
            log.debug("free_space failed", exc_info=True)
            return None

    # ------------------------------------------------------------------ free space / pending refresh
    def _refresh_info(self) -> None:
        """Free space (and kept partial files of the shown list) for dest_dir, on a worker; does not block actions."""
        self._info_seq += 1
        seq, dest, listing = self._info_seq, self.dest_dir, self.listing

        def work() -> None:
            free = self._free_of(dest)
            pend = self._pending_of(listing, dest) if listing is not None else None
            self._post(self._info_answer, seq, dest, listing, free, pend)

        self._spawn(work)

    def refresh_free(self) -> None:
        """Read the free space (and the kept parts of the shown list) of dest_dir again, on a worker: after a model
        file was deleted in the folder view."""
        if not self.closed:
            self._refresh_info()

    def _info_answer(self, seq: int, dest: str, listing: RepoListing | None, free: int | None,
                     pend: dict[str, int] | None) -> None:
        if self.closed or seq != self._info_seq or dest != self.dest_dir:
            return
        self.free_bytes = free
        if pend is not None and listing is self.listing and self._job is None:
            self._pending = pend
            self._build_rows()
        self._changed()

    # ------------------------------------------------------------------ repo text / listing
    def set_repo_text(self, text: str) -> bool:
        """The repo box changed. While a download runs it cannot change (False). Another repo than the listed one
        drops the shown list (and any listing still under way: its answer will be ignored)."""
        if self.closed or self._job == "download":
            return False
        text = str(text or "")
        if text == self.repo_text:
            return True
        self.repo_text = text
        stripped = text.strip()
        if self.phase == "listing" or (self.phase in ("list", "done") and stripped != self._listed_text):
            self._seq += 1                         # a listing under way is answered into the void
            if self._job == "list":
                self._job = None
            self.listing, self.rows, self._ticks, self._shown = None, (), [], None
            self._pending, self._downloaded = {}, set()
            self.result_path = None
            self.phase = "enter"
            self._say("")
        elif self.phase == "error":
            self.phase = "enter"
            self._say("")
        self._changed()
        return True

    def list_now(self) -> bool:
        """List files: parse the repo text and list its .gguf files (both on a worker)."""
        if not self.can_list:
            return False
        self._seq += 1
        seq, text, dest = self._seq, self.repo_text.strip(), self.dest_dir
        self._job = "list"
        self.phase = "listing"
        self.listing, self.rows, self._ticks, self._shown = None, (), [], None
        self._pending, self._downloaded = {}, set()
        self.result_path = None
        self._say("")
        hf = self._hf

        def work() -> None:
            try:
                repo = hf.parse_repo(text)
                listing = hf.list_files(repo)
                pend = self._pending_of(listing, dest)
                free = self._free_of(dest)
                self._post(self._list_answer, seq, text, dest, listing, pend, free, None, False)
            except HfError as e:
                self._post(self._list_answer, seq, text, dest, None, {}, None, e, False)
            except Exception:  # noqa: BLE001
                log.exception("download dialog: listing failed unexpectedly")
                self._post(self._list_answer, seq, text, dest, None, {}, None, None, True)

        if not self._spawn(work):
            self._job = None
            self._list_failed(UNEXPECTED_LIST, "error")
        self._changed()
        return True

    def _list_failed(self, reason: str, kind: str) -> None:
        """A listing that gave nothing. The reason goes to the empty list area, where the files would have been,
        and the message line stays empty: the same sentence twice in one small window reads as two problems."""
        self.phase = "error"
        self._say("")
        self.list_hint = reason
        self.list_hint_kind = kind if kind in MESSAGE_KINDS else "error"

    def _list_answer(self, seq: int, text: str, dest: str, listing: RepoListing | None, pend: dict[str, int],
                     free: int | None, err: HfError | None, unexpected: bool) -> None:
        if not self._current(seq):
            log.debug("download dialog: late listing answer dropped")
            return
        self._job = None
        if listing is None:
            if unexpected or err is None:
                self._list_failed(UNEXPECTED_LIST, "error")
            else:
                reason = f"{err.message} {NO_GGUF_HINT}" if err.kind == "no_gguf" else err.message
                self._list_failed(reason, _KIND_TONE.get(err.kind, "error"))
            self._changed()
            return
        self.listing = listing
        self.list_hint = ""
        self._listed_text = text
        self._pending = dict(pend)
        if dest == self.dest_dir:
            self.free_bytes = free
        self.phase = "list"
        self._build_rows()
        want = linked_file(text)
        if want is not None:
            row = next((r for r in self.rows if r.file.path == want and r.selectable), None)
            if row is not None:
                self._ticks = [row.file.path]
        if not any(r.selectable for r in self.rows):
            self._say(NO_SUPPORTED, "warn")
        else:
            self._say("")
        self._changed()

    # ------------------------------------------------------------------ selection
    def _pickable_row(self, file: HfFile | Row | None) -> Row | None:
        """The row of file when it may be ticked now; else None (a split / unsupported row says why)."""
        if self.closed or self._job is not None or self.phase not in ("list", "done") or file is None:
            return None
        if isinstance(file, Row):
            file = file.file
        row = next((r for r in self.rows if r.file == file), None)
        if row is None:
            return None
        if not row.selectable:
            if not row.supported or row.kind == "split":
                self._say(row.file.note or "This file cannot be downloaded by Agent Start.", "warn")
                self._changed()
            return None
        return row

    def select(self, file: HfFile | Row | None) -> bool:
        """Tick only this file (None clears every tick). Split and unsupported rows cannot be ticked (their reason
        is shown instead). Not while something runs."""
        if file is None:
            if self.closed or self._job is not None or self.phase not in ("list", "done"):
                return False
            self._ticks, self._shown = [], None
            self._changed()
            return True
        row = self._pickable_row(file)
        if row is None:
            return False
        if self._ticks != [row.file.path] or self.phase == "done":
            self._ticks = [row.file.path]
            if self.phase == "done":
                self.phase = "list"
            self._say("")
        self._changed()
        return True

    def toggle(self, file: HfFile | Row) -> bool:
        """A click on a row: tick it, or untick it when it is ticked. Any number of rows can be ticked; the same
        rows as select() refuse. Not while something runs."""
        row = self._pickable_row(file)
        if row is None:
            return False
        if row.file.path in self._ticks:
            self._ticks.remove(row.file.path)
        else:
            self._ticks.append(row.file.path)
        if self.phase == "done":
            self.phase = "list"
        self._say("")
        self._changed()
        return True

    # ------------------------------------------------------------------ download
    def start(self) -> bool:
        """Download (or Resume) the ticked files, one after another in list order. Before anything is fetched: one
        free-space check for the whole batch (hf.space_ok with the bytes still to fetch, summed); then each file
        through hf.download on its own worker, the next one started only when the one before has answered.
        Progress, the results and errors come back through post."""
        if not self.can_download:
            return False
        batch = self.ticked
        if self.listing is None or not batch:
            return False
        rows = {r.file.path: r for r in self.rows}
        need = sum(max(0, f.size - rows[f.path].pending) for f in batch)
        self._stop = threading.Event()
        self._job = "download"
        self._cancel_requested = False
        self.closing = False
        self._batch = batch
        self.result_path = None
        self._shown = None
        self._fetch(0, need)
        return True

    def _fetch(self, pos: int, need: int | None) -> None:
        """Start file pos of the batch on a worker. need: the batch's bytes for the one free-space check (the
        first file only; None for the rest)."""
        listing, stop = self.listing, self._stop
        f = self._batch[pos]
        pending = max(0, int(self._pending.get(f.path, 0)))
        self._seq += 1
        seq, dest = self._seq, self.dest_dir
        self._batch_pos = pos
        self._dl_file = f
        self._resume_from = pending
        self._checking = pending > 0
        self._last_done = 0
        self._meter.reset()
        self.phase = "downloading"
        self.done_bytes = 0
        self.total_bytes = f.size
        self.rate_bps = None
        self._say(STARTING)
        hf, margin = self._hf, self._margin

        def progress(done: int, total: int) -> None:      # worker thread: data only, through post
            self._post(self._progress_answer, seq, done, total)

        def work() -> None:
            path: str | None = None
            err: HfError | None = None
            unexpected = False
            no_space: tuple[int | None, int] | None = None
            try:
                if need is not None and not hf.space_ok(dest, need):
                    no_space = (self._free_of(dest), need + margin)
                else:
                    path = hf.download(listing, f, dest, progress=progress, stop_evt=stop)
            except HfError as e:
                err = e
            except Exception:  # noqa: BLE001
                log.exception("download dialog: download failed unexpectedly")
                unexpected = True
            pend = 0
            if path is None:
                try:
                    pend = int(hf.pending_part(dest, f, listing) or 0)
                except Exception:  # noqa: BLE001
                    log.debug("pending_part failed", exc_info=True)
            free = self._free_of(dest)
            self._post(self._download_answer, seq, f, path, err, unexpected, no_space, pend, free)

        if not self._spawn(work):
            self._job = None
            self._stop = None
            self.phase = "list"
            self._say(UNEXPECTED, "error")
        self._changed()

    def _progress_answer(self, seq: int, done: int, total: int) -> None:
        if not self._current(seq) or self._job != "download":
            return
        try:
            done, total = int(done), int(total)
        except (TypeError, ValueError):
            return
        now = self._clock()
        if self._checking and (done < self._last_done or done > self._resume_from):
            self._checking = False                 # the kept part is checked (or a restart from zero): bytes flow
            self._meter.reset()
        self._last_done = done
        self.done_bytes = max(0, done)
        if total > 0:
            self.total_bytes = total
        if not self._checking:
            self._meter.add(now, self.done_bytes)
            self.rate_bps = self._meter.rate()
        if self.message == STARTING:
            self._say("")
        self._changed()

    def _download_answer(self, seq: int, f: HfFile, path: str | None, err: HfError | None, unexpected: bool,
                         no_space: tuple[int | None, int] | None, pend: int, free: int | None) -> None:
        if not self._current(seq):
            log.debug("download dialog: late download answer dropped")
            return
        self._checking = False
        self.rate_bps = None
        self.free_bytes = free                     # a fresh reading of the same folder (None: unknown now)
        if path is not None:
            self._file_done(f, path)
            return
        self._job = None
        self._stop = None
        if pend > 0:
            self._pending[f.path] = pend
        else:
            self._pending.pop(f.path, None)
        self.phase = "list"
        self._build_rows()
        if no_space is not None:
            have, need = no_space
            if have is None:
                self._say(NO_SPACE_UNKNOWN, "error")
            else:
                self._say(NO_SPACE.format(need=fmt_size(need), margin=f"{self._margin / GIB:g} GB",
                                          free=fmt_size(have), missing=fmt_size(max(0, need - have))), "error")
        elif unexpected or err is None:
            self._say(UNEXPECTED, "error")
        else:
            self._say(err.message, _KIND_TONE.get(err.kind, "error"))
        self._changed()

    def _file_done(self, f: HfFile, path: str) -> None:
        """File f of the batch arrived: it is reported (on_done) at once and its row becomes a downloaded row
        (no tick). Then the next file starts, unless a cancel or a close was asked or it was the last one."""
        self._pending.pop(f.path, None)
        self._downloaded.add(f.path)
        self._shown = f
        self.result_path = path
        self.done_bytes = self.total_bytes = f.size
        self._build_rows()
        nxt = self._batch_pos + 1
        rest = nxt < len(self._batch)
        go_on = rest and not self._cancel_requested     # (a close asks the cancel too)
        if not go_on:
            self._job = None
            self._stop = None
            if rest:                               # a cancel came too late for this file: the rest waits
                self.phase = "list"
                self._say(HfError("cancelled").message, _KIND_TONE["cancelled"])
            else:
                self.phase = "done"
                if len(self._batch) > 1:
                    self._say(DONE_MANY.format(n=len(self._batch)), "ok")
                else:
                    self._say(DONE_VISION if f.kind == "vision" else DONE_MODEL, "ok")
        self._changed()
        if self._on_done is not None:
            try:
                self._on_done(path)
            except Exception:  # noqa: BLE001
                log.exception("download dialog: on_done callback failed")
        if go_on and not self.closed and self._job == "download":
            self._fetch(nxt, None)

    def cancel(self) -> bool:
        """Cancel download: hf_download stops between chunks and keeps the partial file for a resume."""
        if not self.can_cancel or self._stop is None:
            return False
        self._cancel_requested = True
        self._stop.set()
        self._say(CANCELLING)
        self._changed()
        return True

    def discard_partial(self) -> bool:
        """Delete the kept partial file of the selected row (hf.discard_part, on a worker)."""
        if not self.can_discard:
            return False
        row = self._selected_row()
        listing = self.listing
        if row is None or listing is None:
            return False
        self._seq += 1
        seq, dest, f, hf = self._seq, self.dest_dir, row.file, self._hf
        self._job = "discard"

        def work() -> None:
            err: HfError | None = None
            unexpected = False
            try:
                hf.discard_part(dest, f)
            except HfError as e:
                err = e
            except Exception:  # noqa: BLE001
                log.exception("download dialog: discarding the partial download failed")
                unexpected = True
            try:
                pend = int(hf.pending_part(dest, f, listing) or 0)
            except Exception:  # noqa: BLE001
                pend = 0
            self._post(self._discard_answer, seq, f, err, unexpected, pend, self._free_of(dest))

        if not self._spawn(work):
            self._job = None
            self._say(UNEXPECTED, "error")
        self._changed()
        return True

    def _discard_answer(self, seq: int, f: HfFile, err: HfError | None, unexpected: bool, pend: int,
                        free: int | None) -> None:
        if not self._current(seq):
            return
        self._job = None
        self.free_bytes = free
        if pend > 0:
            self._pending[f.path] = pend
        else:
            self._pending.pop(f.path, None)
        if self.phase == "done":
            self.phase = "list"
        self._build_rows()
        if unexpected:
            self._say("The partial download could not be deleted; see the log.", "error")
        elif err is not None:
            self._say(err.message, _KIND_TONE.get(err.kind, "error"))
        else:
            self._say(DISCARDED)
        self._changed()

    # ------------------------------------------------------------------ folder
    def set_dest_dir(self, path: str) -> bool:
        """Download into another folder. Only while nothing runs; free space and kept parts are read again."""
        if not self.can_change_dest or not isinstance(path, str) or not path.strip():
            return False
        self.dest_dir = path
        self.free_bytes = None
        self._pending = {}
        self._downloaded = set()
        self.result_path = None
        if self.phase == "done":
            self.phase = "list"
        self._build_rows()
        self._say("")
        self._refresh_info()
        self._changed()
        return True

    # ------------------------------------------------------------------ closing
    def close(self) -> bool:
        """The window wants to close. True: it may (every later answer is ignored from now on). While a download
        runs: the download is cancelled first (its partial file is kept for a resume) and False is returned; once
        hf_download has stopped, on_change fires and close() returns True."""
        if self.closed:
            return True
        if self._job == "download":
            self.closing = True
            if not self._cancel_requested and self._stop is not None:
                self._cancel_requested = True
                self._stop.set()
            self._say(CLOSING)
            self._changed()
            return False
        self.closed = True
        self._seq += 1
        self._info_seq += 1
        self._job = None
        return True

    def abandon(self) -> None:
        """The window is going away now (its owner was destroyed): stop a running download (its partial file is
        kept) and ignore every later answer. No on_change, no on_done after this."""
        if self._stop is not None:
            self._stop.set()
        self.closed = True
        self._seq += 1
        self._info_seq += 1
        self._job = None


# ================================================================================================ the window
WIDTH = 420
HEIGHT = 528            # the content's own height (the message line under the progress bar was cut off at 480)
PAD = 20                # the one side margin: every section starts and ends on it
LIST_H = 150             # about five rows; longer lists scroll
ROW_H = 28
ARM_S = 0.4              # a close in the first moment waits until the window is armed (see history_dialog)
PUMP_MS = 40             # own answer queue (only when the master has no post())
CHECK_OFF = "\uE739"     # Segoe Fluent Icons Checkbox
CHECK_ON = "\uE73D"      # CheckboxCompositeReversed
RADIO_OFF = "\uECCA"     # Segoe Fluent Icons RadioBtnOff: the folder view chooses one file at a time
RADIO_ON = "\uECCB"      # RadioBtnOn
DOWNLOAD_W = 130         # room for "Download 12 files"; Delete… and Delete for good use the same width
VIEW_HF = "hf"           # the two views of the window
VIEW_FOLDER = "folder"
TAB_HF = "Hugging Face"
TAB_FOLDER = "In this folder"
TITLE_HF = "Download a model"
TITLE_FOLDER = "Models in this folder"
DELETE_LABEL = "Delete…"
KEEP_LABEL = "Keep"
DELETE_YES_LABEL = "Delete for good"
LIST_GROW = 38           # the folder view has no repo row: the list takes its 28 px and its 10 px gap
ASK_H = 88               # the question area: the file name, bar, figures and action row's 100 px minus its gap
Q_NAME_MAX = 110         # a longer file name is shortened in the question so it stays within three lines
_TONE_COLOR = {"info": T.TEXT_2, "warn": T.AMBER, "error": T.RED_TEXT, "ok": T.ACCENT}


def _thread_runner(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="hc-hf-download", daemon=True).start()


def _named_runner(run: Callable[..., None]) -> Callable[[Callable[[], None]], None]:
    """The app's _run_worker(name, fn) as a run_worker(fn)."""
    def runner(fn: Callable[[], None]) -> None:
        run("hc-hf-download", fn)
    return runner


class DownloadDialog(ctk.CTkToplevel):
    """The "Download a model" window over one DownloadFlow and one FolderFlow. dest_dir: the models folder (it must
    exist). on_done(path) runs once per finished download, on_deleted(path) once per file deleted in the folder view,
    both on the Tk thread. hf / files: the hf_download and model_files stand-ins of a test."""

    def __init__(self, master, dest_dir: str, *, on_done: Callable[[str], None] | None = None,
                 on_deleted: Callable[[str], None] | None = None,
                 run_worker: Callable[[Callable[[], None]], None] | None = None,
                 post: Callable[..., None] | None = None, hf: Any = None, files: Any = None) -> None:
        super().__init__(master, fg_color=T.BG)
        style_dialog(self)
        self._master = master
        self._armed_at = time.monotonic() + ARM_S
        self._close_pending = False
        self._close_wanted = False
        self._closing = False
        self._destroyed = False
        self._q: queue.SimpleQueue[tuple[Callable[..., None], tuple[Any, ...]]] | None = None
        self._pump_id: str | None = None
        self.logical_width = WIDTH
        self.logical_height = HEIGHT
        self._configured: dict[int, dict[str, Any]] = {}
        self._view = VIEW_HF
        self._drawn_view = VIEW_HF
        self._on_deleted = on_deleted
        self._deleted_meanwhile = False       # a file was deleted in the folder view since it was switched to

        if post is None:
            mpost = getattr(master, "post", None)
            if callable(mpost):
                post = mpost
            else:                                   # a thread-safe queue drained on the Tk thread
                self._q = queue.SimpleQueue()
                post = self._queue_post
        if run_worker is None:
            mrun = getattr(master, "_run_worker", None)
            run_worker = _named_runner(mrun) if callable(mrun) else _thread_runner

        self.title("Download a model")
        self.resizable(False, False)
        center_over(self, master, WIDTH, HEIGHT)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.grid_columnconfigure(0, weight=1)

        ui = T.ui_family()
        self._small = ctk.CTkFont(family=ui, size=11)
        self._body = ctk.CTkFont(family=ui, size=12)
        self._body_bold = ctk.CTkFont(family=ui, size=12, weight="bold")
        self._mono = ctk.CTkFont(family=T.mono_family(), size=11)
        self._glyph = ctk.CTkFont(family=T.icon_family(), size=12)
        self._link_font = ctk.CTkFont(family=ui, size=11, underline=True)

        # ---- 1. title
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(12, 0))
        head.grid_columnconfigure(0, weight=1)
        self._title = ctk.CTkLabel(head, text=TITLE_HF, anchor="w", height=20,
                                   font=ctk.CTkFont(family=ui, size=14, weight="bold"))
        self._title.grid(row=0, column=0, sticky="w")
        # the view switch "Hugging Face | In this folder": a 20 px track, the chosen part raised
        self._switch = ctk.CTkFrame(head, fg_color=T.SURFACE, corner_radius=8)
        self._switch.grid(row=0, column=1, sticky="e")
        self._tabs: dict[str, ctk.CTkLabel] = {}
        for i, (key, text) in enumerate(((VIEW_HF, TAB_HF), (VIEW_FOLDER, TAB_FOLDER))):
            tab = ctk.CTkLabel(self._switch, text=text, height=16, width=self._measure(11, text) + 16,
                               corner_radius=6, font=self._small, fg_color=T.SURFACE, text_color=T.TEXT)
            tab.grid(row=0, column=i, padx=(2, 2 if i else 0), pady=2)
            tab.bind("<Button-1>", lambda _e, k=key: self.set_view(k))
            self._tabs[key] = tab
        self._switch_enabled = True

        # ---- 2. repo box + List files
        box = ctk.CTkFrame(self, fg_color="transparent")
        box.grid(row=1, column=0, sticky="ew", padx=PAD, pady=(10, 0))
        self._repo_box = box
        box.grid_columnconfigure(0, weight=1)
        self._repo_var = ctk.StringVar(value="")
        self._entry = ctk.CTkEntry(box, textvariable=self._repo_var, height=28, corner_radius=8, font=self._mono,
                                   fg_color=T.SURFACE, border_color=T.HAIRLINE, text_color=T.TEXT)
        self._entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._entry.bind("<Return>", lambda _e: self.list_files())
        self._list_btn = ctk.CTkButton(box, text="List files", width=84, height=28, corner_radius=8, font=self._body,
                                       fg_color=T.RAISED, hover_color=T.RAISED_HOVER, text_color=T.TEXT,
                                       text_color_disabled=T.TEXT_3, command=self.list_files)
        self._list_btn.grid(row=0, column=1)

        # ---- 3. the file list (one row per file; scrolls when long)
        self._list = ctk.CTkScrollableFrame(self, height=LIST_H, fg_color=T.SURFACE, corner_radius=8,
                                            scrollbar_button_color=T.RAISED,
                                            scrollbar_button_hover_color=T.RAISED_HOVER)
        self._list.grid(row=2, column=0, sticky="ew", padx=PAD, pady=(8, 0))
        self._list.grid_columnconfigure(0, weight=1)
        # no fixed height: a failed listing shows its reason here, and that wraps onto several lines
        self._placeholder = ctk.CTkLabel(self._list, text=HINT_ENTER, anchor="w", justify="left", font=self._small,
                                         text_color=T.TEXT_3, wraplength=WIDTH - 2 * PAD - 48)
        self._row_widgets: list[tuple[Any, ctk.CTkFrame, ctk.CTkLabel, list[Any]]] = []   # Row or FolderRow
        self._rows_drawn: tuple[Any, ...] | None = None
        self._hover_row: int | None = None

        # ---- 4. To <folder> · N GB free · Change…
        dest = ctk.CTkFrame(self, fg_color="transparent")
        dest.grid(row=3, column=0, sticky="ew", padx=PAD, pady=(8, 0))
        self._dest_to = ctk.CTkLabel(dest, text="To", anchor="w", height=16, font=self._small, text_color=T.TEXT_3)
        self._dest_to.grid(row=0, column=0, sticky="w")
        self._dest_folder = ctk.CTkLabel(dest, text="", anchor="w", height=16, font=self._small, text_color=T.TEXT_2)
        self._dest_folder.grid(row=0, column=1, sticky="w", padx=(4, 0))
        self._dest_free = ctk.CTkLabel(dest, text="", anchor="w", height=16, font=self._small, text_color=T.TEXT_3)
        self._dest_free.grid(row=0, column=2, sticky="w", padx=(4, 0))
        self._change_link = ctk.CTkLabel(dest, text="Change…", anchor="w", height=16, font=self._link_font,
                                         text_color=T.TEXT_2, cursor="hand2")
        self._change_link.grid(row=0, column=3, sticky="w", padx=(4, 0))
        self._change_link.bind("<Button-1>", lambda _e: self.change_folder())
        self._change_enabled = True

        # ---- 5. Close + Download
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=4, column=0, sticky="ew", padx=PAD, pady=(12, 0))
        foot.grid_columnconfigure(0, weight=1)
        self._close_btn = ctk.CTkButton(foot, text="Close", width=90, height=28, corner_radius=8, font=self._body,
                                        fg_color="transparent", border_width=1, border_color=T.RAISED_HOVER,
                                        hover_color=T.RAISED, text_color=T.TEXT, command=self.close)
        self._close_btn.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self._download_btn = ctk.CTkButton(foot, text="Download", width=DOWNLOAD_W, height=28, corner_radius=8,
                                           font=self._body, fg_color=T.RAISED, hover_color=T.PRIMARY_HOVER,
                                           text_color=T.ON_PRIMARY, text_color_disabled=T.TEXT_3,
                                           command=self.download, state="disabled")
        self._download_btn.grid(row=0, column=2, sticky="e")
        # the folder view's button in the same place: bold, in the colour of the error text, as Cancel download
        self._delete_btn = ctk.CTkButton(foot, text=DELETE_LABEL, width=DOWNLOAD_W, height=28, corner_radius=8,
                                         font=self._body_bold, fg_color=T.RAISED, hover_color=T.RAISED_HOVER,
                                         text_color=T.RED_TEXT, text_color_disabled=T.TEXT_3,
                                         command=self.ask_delete, state="disabled")
        self._delete_btn.grid(row=0, column=2, sticky="e")
        self._delete_btn.grid_remove()

        # ---- 6. progress: hairline, file name, bar, figures, Cancel download / Discard partial download
        ctk.CTkFrame(self, fg_color=T.HAIRLINE, bg_color=T.HAIRLINE, height=1, corner_radius=0).grid(
            row=5, column=0, sticky="ew", padx=PAD, pady=(12, 0))
        self._prog_title = ctk.CTkLabel(self, text="", anchor="w", height=16, font=self._body, text_color=T.TEXT)
        self._prog_title.grid(row=6, column=0, sticky="ew", padx=PAD, pady=(12, 0))
        self._bar = ctk.CTkProgressBar(self, height=6, corner_radius=3, fg_color=T.RAISED, progress_color=T.ACCENT)
        self._bar.grid(row=7, column=0, sticky="ew", padx=PAD, pady=(8, 0))
        self._bar.set(0.0)
        self._prog_text = ctk.CTkLabel(self, text="", anchor="w", height=16, font=self._small, text_color=T.TEXT_3)
        self._prog_text.grid(row=8, column=0, sticky="ew", padx=PAD, pady=(6, 0))
        act = ctk.CTkFrame(self, fg_color="transparent", height=28)
        act.grid(row=9, column=0, sticky="ew", padx=PAD, pady=(8, 0))
        act.grid_columnconfigure(0, weight=1)
        act.grid_propagate(False)
        self._act = act
        # the folder view's question, in place of the four rows above (same height: nothing below moves)
        self._ask = ctk.CTkFrame(self, fg_color="transparent", height=ASK_H)
        self._ask.grid(row=6, column=0, rowspan=4, sticky="ew", padx=PAD, pady=(12, 0))
        self._ask.grid_columnconfigure(0, weight=1)
        self._ask.grid_propagate(False)
        self._ask_text = ctk.CTkLabel(self._ask, text="", anchor="nw", justify="left", height=32,
                                      wraplength=WIDTH - 2 * PAD, font=self._body, text_color=T.TEXT)
        self._ask_text.grid(row=0, column=0, columnspan=3, sticky="ew")
        self._keep_btn = ctk.CTkButton(self._ask, text=KEEP_LABEL, width=90, height=28, corner_radius=8,
                                       font=self._body, fg_color="transparent", border_width=1,
                                       border_color=T.RAISED_HOVER, hover_color=T.RAISED, text_color=T.TEXT,
                                       text_color_disabled=T.TEXT_3, command=self.keep)
        self._keep_btn.grid(row=1, column=1, sticky="e", padx=(0, 8), pady=(8, 0))
        self._yes_btn = ctk.CTkButton(self._ask, text=DELETE_YES_LABEL, width=DOWNLOAD_W, height=28, corner_radius=8,
                                      font=self._body_bold, fg_color=T.DANGER, hover_color=T.DANGER_HOVER,
                                      text_color=T.TEXT, text_color_disabled=T.TEXT_3, command=self.delete_for_good)
        self._yes_btn.grid(row=1, column=2, sticky="e", pady=(8, 0))
        self._ask.grid_remove()
        # the one action here that stops something: bold, in the colour of the error text
        self._cancel_btn = ctk.CTkButton(act, text="Cancel download", width=130, height=28, corner_radius=8,
                                         font=self._body_bold, fg_color=T.RAISED, hover_color=T.RAISED_HOVER,
                                         text_color=T.RED_TEXT, text_color_disabled=T.TEXT_3, command=self.cancel)
        self._discard_btn = ctk.CTkButton(act, text="Discard partial download", width=170, height=28, corner_radius=8,
                                          font=self._body, fg_color="transparent", border_width=1,
                                          border_color=T.RAISED_HOVER, hover_color=T.RAISED, text_color=T.TEXT_2,
                                          text_color_disabled=T.TEXT_3, command=self.discard)

        # ---- 7. the message (one plain sentence; colour by kind)
        self.grid_rowconfigure(10, weight=1)
        self._msg = ctk.CTkLabel(self, text="", anchor="nw", justify="left", height=32, wraplength=WIDTH - 2 * PAD,
                                 font=self._small, text_color=T.TEXT_2)
        self._msg.grid(row=10, column=0, sticky="new", padx=PAD, pady=(10, 12))
        self._msg_kind = "info"

        self.folder = FolderFlow(dest_dir, files=files if files is not None else model_files, run_worker=run_worker,
                                 post=post, on_change=self._render, on_deleted=self._folder_deleted)
        self.flow = DownloadFlow(dest_dir, hf=hf if hf is not None else hf_download, run_worker=run_worker,
                                 post=post, on_change=self._render, on_done=on_done)
        self._repo_var.trace_add("write", lambda *_a: self._repo_changed())
        if self._q is not None:
            self._pump_id = self.after(PUMP_MS, self._pump)
        self._render()
        # NOT modal: a download can take a long time, and the app (Turn on / off, Settings) stays usable
        # meanwhile. The window stays above the main window and is brought to the front once.
        self.transient(master)
        later(self, 200, self._raise_once)

    def _raise_once(self) -> None:
        try:
            if self.winfo_exists():
                self.lift()
                self.focus_force()
        except Exception:  # noqa: BLE001
            log.debug("download dialog: could not be raised", exc_info=True)

    # ------------------------------------------------------------------ own answer queue (bare masters only)
    def _queue_post(self, fn: Callable[..., None], *args: Any) -> None:
        """Thread-safe: queue fn(*args) for the Tk thread (no Tk call here)."""
        if self._q is not None:
            self._q.put((fn, args))

    def _pump(self) -> None:
        self._pump_id = None
        if self._destroyed or self._q is None:
            return
        while True:
            try:
                fn, args = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                log.exception("download dialog: posted callback failed")
            if self._destroyed:
                return
        try:
            self._pump_id = self.after(PUMP_MS, self._pump)
        except tk.TclError:
            self._pump_id = None

    # ------------------------------------------------------------------ actions (buttons; also the tests' way in)
    def set_repo(self, text: str) -> None:
        """Type into the repo box (as the user would)."""
        self._repo_var.set(text)
        self._repo_changed()

    def _repo_changed(self) -> None:
        if self._destroyed or getattr(self, "flow", None) is None:
            return
        text = self._repo_var.get()
        if text != self.flow.repo_text and not self.flow.set_repo_text(text):
            self._repo_var.set(self.flow.repo_text)       # cannot change while downloading

    def list_files(self) -> None:
        if not self._destroyed:
            self._repo_changed()
            self.flow.list_now()

    def choose_row(self, index: int) -> None:
        """A click on row index: ticks it, or unticks it when it is ticked."""
        if self._destroyed or not 0 <= index < len(self.flow.rows):
            return
        self.flow.toggle(self.flow.rows[index])

    def choose_folder_row(self, index: int) -> None:
        """A click on row index of the folder view: chooses it (one at a time)."""
        if self._destroyed or not 0 <= index < len(self.folder.rows):
            return
        self.folder.choose(self.folder.rows[index])

    def _click_row(self, index: int) -> None:
        if self._view == VIEW_FOLDER:
            self.choose_folder_row(index)
        else:
            self.choose_row(index)

    def ask_delete(self) -> None:
        """Delete…: the question under the hairline."""
        if not self._destroyed:
            self.folder.ask()

    def keep(self) -> None:
        if not self._destroyed:
            self.folder.keep()

    def delete_for_good(self) -> None:
        if not self._destroyed:
            self.folder.confirm()

    def _can_switch(self) -> bool:
        """The view switch is off while the download side lists or downloads and while the question or a delete is
        up."""
        return (not self._destroyed and not self.flow.closed and not self.flow.busy and not self.folder.closed
                and self.folder.phase not in ("asking", "deleting"))

    def set_view(self, name: str) -> bool:
        """Show the Hugging Face view or the folder view (the switch). The folder view reads the folder again each
        time it is chosen; the download side is shown as it was left, listed again only when a file was deleted
        meanwhile and a listing is on screen (its rows could name a deleted file as downloaded)."""
        if self._destroyed or name not in (VIEW_HF, VIEW_FOLDER):
            return False
        if name == self._view:
            return True
        if not self._can_switch():
            return False
        self._view = name
        folder = name == VIEW_FOLDER
        self._shown(self._repo_box, not folder)
        self._list.configure(height=self._list_drawn_h() + LIST_GROW if folder else LIST_H)
        if folder:
            self._deleted_meanwhile = False
            self.folder.refresh()
        else:
            f = self.flow
            if self._deleted_meanwhile and f.rows and f.phase in ("list", "done") and f.can_list:
                f.list_now()
            self._deleted_meanwhile = False
        self._render()
        return True

    def _list_drawn_h(self) -> int:
        """The list's inner height as drawn in the Hugging Face view. CTkScrollableFrame is as tall as the taller of
        its canvas (LIST_H) and its scrollbar (200 by default), so the folder view grows from that, not from LIST_H."""
        try:
            bar = int(self._list._scrollbar.cget("height"))      # CTk internal, read only
        except Exception:  # noqa: BLE001
            bar = 200
        return max(LIST_H, bar)

    def _folder_deleted(self, path: str) -> None:
        """FolderFlow deleted path: the free space is read again, the download side is told, then the app."""
        self._deleted_meanwhile = True
        self.flow.refresh_free()
        if self._on_deleted is not None:
            try:
                self._on_deleted(path)
            except Exception:  # noqa: BLE001
                log.exception("download dialog: on_deleted callback failed")

    def download(self) -> None:
        if not self._destroyed:
            self.flow.start()

    def cancel(self) -> None:
        if not self._destroyed:
            self.flow.cancel()

    def discard(self) -> None:
        if not self._destroyed:
            self.flow.discard_partial()

    def change_folder(self) -> None:
        """Change…: pick another models folder (a folder picker; nothing is written until a download starts). Both
        views use the one folder; in the folder view the new folder is read at once."""
        folder = self._view == VIEW_FOLDER
        if self._destroyed or not self.flow.can_change_dest or (folder and not self.folder.can_change_dest):
            return
        try:
            path = filedialog.askdirectory(parent=self, initialdir=self.flow.dest_dir, mustexist=True,
                                           title="Models folder" if folder else "Download models to")
        except Exception:  # noqa: BLE001
            log.exception("folder picker failed")
            return
        if path and not self._destroyed:
            p = os.path.normpath(str(path))
            if self.flow.set_dest_dir(p):
                self.folder.set_dest_dir(p, read=folder)

    # ------------------------------------------------------------------ public read-only helpers (tests)
    def row_texts(self) -> list[tuple[str, str, str]]:
        """The rows as shown: (file name, note, size)."""
        out = []
        for _row, _frame, _glyph, labels in self._row_widgets:
            out.append((str(labels[0].cget("text")), str(labels[1].cget("text")), str(labels[2].cget("text"))))
        return out

    def row_states(self) -> list[tuple[bool, bool]]:
        """(can be ticked / chosen, ticked / chosen) per row of the view shown."""
        ticked = self._view_chosen()
        return [(row.selectable, row.file in ticked) for row, _f, _g, _l in self._row_widgets]

    def ticked_rows(self) -> list[int]:
        """The indexes of the ticked (chosen) rows, as drawn (the glyph shows the ticked box / the filled circle)."""
        on = self._view_marks()[1]
        return [i for i, (_row, _f, glyph, _l) in enumerate(self._row_widgets) if glyph.cget("text") == on]

    def view(self) -> str:
        """VIEW_HF or VIEW_FOLDER."""
        return self._view

    def heading_text(self) -> str:
        return str(self._title.cget("text"))

    def switch_states(self) -> dict[str, tuple[bool, bool]]:
        """Each part of the view switch: label -> (chosen, can be clicked)."""
        return {str(t.cget("text")): (k == self._view, self._switch_enabled) for k, t in self._tabs.items()}

    def folder_rows(self) -> list[tuple[str, str, str]]:
        """The folder view's rows as shown: (file name, note, size); [] in the other view."""
        return self.row_texts() if self._view == VIEW_FOLDER else []

    def question_text(self) -> str:
        """The question under the hairline ('' when it is not up)."""
        return str(self._ask_text.cget("text")) if self._ask.winfo_manager() else ""

    def press_delete(self) -> None:
        self._delete_btn.invoke()

    def press_keep(self) -> None:
        self._keep_btn.invoke()

    def press_delete_for_good(self) -> None:
        self._yes_btn.invoke()

    def list_placeholder(self) -> str:
        """The muted line inside the empty list ('' when rows are shown)."""
        return str(self._placeholder.cget("text")) if self._placeholder.winfo_manager() else ""

    def button_states(self) -> dict[str, bool]:
        """Every visible button (and the Change… link): label -> enabled."""
        out: dict[str, bool] = {}
        repo, ask = bool(self._repo_box.winfo_manager()), bool(self._ask.winfo_manager())
        for b, shown in ((self._list_btn, repo), (self._close_btn, True), (self._download_btn, True),
                         (self._delete_btn, True), (self._cancel_btn, True), (self._discard_btn, True),
                         (self._keep_btn, ask), (self._yes_btn, ask)):
            if shown and b.winfo_manager():
                out[str(b.cget("text"))] = str(b.cget("state")) != "disabled"
        out["Change…"] = self._change_enabled
        return out

    def message_text(self) -> str:
        return str(self._msg.cget("text"))

    def message_kind(self) -> str:
        return self._msg_kind

    def progress_title(self) -> str:
        return str(self._prog_title.cget("text"))

    def progress_text(self) -> str:
        return str(self._prog_text.cget("text"))

    def progress_value(self) -> float:
        return float(self._bar.get())

    def dest_line(self) -> str:
        parts = [str(w.cget("text")) for w in (self._dest_to, self._dest_folder, self._dest_free)]
        return " ".join(p for p in parts if p) + " Change…"

    def entry_enabled(self) -> bool:
        return str(self._entry.cget("state")) != "disabled"

    # ------------------------------------------------------------------ drawing
    def _scale(self) -> float:
        try:
            return float(ctk.ScalingTracker.get_widget_scaling(self))
        except Exception:  # noqa: BLE001
            return 1.0

    def _measure(self, px: int, text: str) -> int:
        s = self._scale()
        try:
            return int(self.tk.call("font", "measure", (T.ui_family(), -max(7, round(px * s))), text) / s)
        except tk.TclError:
            return len(text) * px // 2

    def _fit(self, text: str, width: int, px: int = 12) -> str:
        if width <= 0 or self._measure(px, text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._measure(px, text[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

    # the view shown: its rows, chosen files, marks, whether rows can be clicked now, the empty list's line
    def _view_rows(self) -> tuple[Any, ...]:
        return self.folder.rows if self._view == VIEW_FOLDER else self.flow.rows

    def _view_chosen(self) -> set[Any]:
        if self._view == VIEW_FOLDER:
            return {self.folder.chosen} if self.folder.chosen is not None else set()
        return set(self.flow.ticked)

    def _view_marks(self) -> tuple[str, str]:
        return (RADIO_OFF, RADIO_ON) if self._view == VIEW_FOLDER else (CHECK_OFF, CHECK_ON)

    def _view_locked(self) -> bool:
        return not self.folder.can_choose if self._view == VIEW_FOLDER else self.flow.busy

    def _view_hint(self) -> str:
        return self.folder.hint if self._view == VIEW_FOLDER else self._hint_for(self.flow)

    @staticmethod
    def _shown(w: Any, on: bool) -> None:
        """Grid w again (with its own options) or take it out; nothing when it already is so."""
        if on and not w.winfo_manager():
            w.grid()
        elif not on and w.winfo_manager():
            w.grid_remove()

    def _rebuild_rows(self) -> None:
        for _row, frame, glyph, _l in self._row_widgets:
            self._configured.pop(id(frame), None)
            self._configured.pop(id(glyph), None)
            frame.destroy()
        self._row_widgets = []
        self._hover_row = None
        f = self.flow
        rows = self._view_rows()
        self._rows_drawn = rows
        self._drawn_view = self._view
        if not rows:
            # something to act on (a failed listing), not a hint; the folder view only ever shows a hint here
            reason = self._view == VIEW_HF and f.phase == "error" and bool(f.list_hint)
            self._placeholder.configure(
                text=self._view_hint(), font=self._body if reason else self._small,
                text_color=_TONE_COLOR.get(f.list_hint_kind, T.RED_TEXT) if reason else T.TEXT_3)
            self._placeholder.grid(row=0, column=0, sticky="ew", padx=10)
            return
        self._placeholder.grid_remove()
        inner = WIDTH - 2 * PAD - 8 - 20                    # list padding + scrollbar
        mark_off = self._view_marks()[0]
        for i, row in enumerate(rows):
            muted = not row.selectable and row.note != "· downloaded"
            frame = ctk.CTkFrame(self._list, fg_color=T.SURFACE, corner_radius=6, height=ROW_H)
            frame.grid(row=i, column=0, sticky="ew", padx=2, pady=0)
            frame.grid_columnconfigure(2, weight=1)
            glyph = ctk.CTkLabel(frame, text=mark_off, width=16, height=ROW_H, font=self._glyph,
                                 text_color=T.TEXT_3)
            glyph.grid(row=0, column=0, padx=(6, 6))
            size_w = self._measure(12, row.size_text) + 8
            note_w = self._measure(11, row.note) + 6 if row.note else 0
            name = self._fit(row.name, inner - 28 - size_w - note_w - 12)
            name_l = ctk.CTkLabel(frame, text=name, anchor="w", height=ROW_H, font=self._body,
                                  text_color=T.TEXT_3 if muted else T.TEXT)
            name_l.grid(row=0, column=1, sticky="w")
            note_l = ctk.CTkLabel(frame, text=row.note, anchor="w", height=ROW_H, font=self._small,
                                  text_color=T.TEXT_3)
            note_l.grid(row=0, column=2, sticky="w", padx=(4, 0))
            size_l = ctk.CTkLabel(frame, text=row.size_text, anchor="e", height=ROW_H, font=self._body,
                                  text_color=T.TEXT_3 if muted else T.TEXT_2)
            size_l.grid(row=0, column=3, sticky="e", padx=(4, 8))
            for w in (frame, glyph, name_l, note_l, size_l):
                w.bind("<Button-1>", lambda _e, k=i: self._click_row(k))
                w.bind("<Enter>", lambda _e, k=i: self._set_hover(k))
                w.bind("<Leave>", lambda _e: self._set_hover(None))
                if row.selectable:
                    w.configure(cursor="hand2")
            self._row_widgets.append((row, frame, glyph, [name_l, note_l, size_l]))

    def _set_hover(self, index: int | None) -> None:
        if self._destroyed or index == self._hover_row:
            return
        self._hover_row = index
        self._paint_rows()

    def _cfg(self, w: Any, **kw: Any) -> None:
        """configure() only what changed (a redraw runs up to ten times a second while downloading)."""
        last = self._configured.setdefault(id(w), {})
        new = {k: v for k, v in kw.items() if last.get(k, self) != v}
        if new:
            w.configure(**new)
            last.update(new)

    def _paint_rows(self) -> None:
        ticked = self._view_chosen()
        mark_off, mark_on = self._view_marks()
        locked = self._view_locked()
        for i, (row, frame, glyph, _labels) in enumerate(self._row_widgets):
            on = row.file in ticked
            hover = self._hover_row == i and row.selectable and not locked
            self._cfg(frame, fg_color=T.RAISED if on or hover else T.SURFACE)
            self._cfg(glyph, text=mark_on if on else mark_off,
                      text_color=T.TEXT if on else (T.TEXT_2 if row.selectable else T.HAIRLINE))

    def _render(self) -> None:
        """Redraw from the flow's public state (on_change; always on the Tk thread)."""
        f = getattr(self, "flow", None)
        ff = getattr(self, "folder", None)
        if self._destroyed or f is None or ff is None:   # (a flow's first answer can come while it is built)
            return
        folder = self._view == VIEW_FOLDER
        rows = self._view_rows()
        if (rows is not self._rows_drawn or self._drawn_view != self._view
                or (not rows and self._placeholder.cget("text") != self._view_hint())):
            self._rebuild_rows()
        self._paint_rows()
        # heading + the view switch
        self._cfg(self._title, text=TITLE_FOLDER if folder else TITLE_HF)
        self._switch_enabled = self._can_switch()
        for key, tab in self._tabs.items():
            chosen = key == self._view
            self._cfg(tab, fg_color=T.RAISED if chosen else T.SURFACE,
                      text_color=T.TEXT if self._switch_enabled else T.TEXT_3,
                      cursor="hand2" if self._switch_enabled and not chosen else "")
        # entry + buttons
        self._cfg(self._entry, state="normal" if f.can_edit else "disabled",
                  text_color=T.TEXT if f.can_edit else T.TEXT_3)
        if self._repo_var.get() != f.repo_text:
            self._repo_var.set(f.repo_text)
        self._cfg(self._list_btn, state="normal" if f.can_list else "disabled")
        can = f.can_download
        self._cfg(self._download_btn, text=f.download_label, state="normal" if can else "disabled",
                  fg_color=T.TEXT if can else T.SURFACE)
        if folder:
            self._shown(self._download_btn, False)
            self._shown(self._delete_btn, True)
            self._cfg(self._delete_btn, state="normal" if ff.can_delete else "disabled")
            self._cancel_btn.grid_remove()
            self._discard_btn.grid_remove()
        else:
            self._shown(self._delete_btn, False)
            self._shown(self._download_btn, True)
            if f.phase == "downloading":
                self._discard_btn.grid_remove()
                self._cancel_btn.grid(row=0, column=1, sticky="e")
                self._cfg(self._cancel_btn, state="normal" if f.can_cancel else "disabled")
            else:
                self._cancel_btn.grid_remove()
                if f.can_discard:
                    self._discard_btn.grid(row=0, column=1, sticky="e")
                    self._cfg(self._discard_btn, state="normal")
                else:
                    self._discard_btn.grid_remove()
        # folder line ("In" the folder in the folder view, "To" it in the other)
        self._cfg(self._dest_to, text="In" if folder else "To")
        self._cfg(self._dest_folder, text=f.dest_text)
        self._cfg(self._dest_free, text=f"· {f.free_text} ·" if f.free_text else "·")
        self._change_enabled = f.can_change_dest and (not folder or ff.can_change_dest)
        self._cfg(self._change_link, text_color=T.TEXT_2 if self._change_enabled else T.TEXT_3,
                  cursor="hand2" if self._change_enabled else "")
        if folder:
            # under the hairline: nothing (no name, no bar) until Delete… puts the question there
            asked = ff.asked
            for w in (self._prog_title, self._bar, self._prog_text, self._act):
                self._shown(w, asked is None)
            self._shown(self._ask, asked is not None)
            self._cfg(self._prog_title, text="")
            self._cfg(self._prog_text, text="")
            self._cfg(self._bar, fg_color=T.BG, progress_color=T.BG)
            if asked is not None:
                answer = "normal" if ff.phase == "asking" else "disabled"
                self._cfg(self._ask_text, text=question_for(asked, Q_NAME_MAX))
                self._cfg(self._keep_btn, state=answer)
                self._cfg(self._yes_btn, state=answer)
            message, kind = ff.message, ff.message_kind
        else:
            self._shown(self._ask, False)
            for w in (self._prog_title, self._bar, self._prog_text, self._act):
                self._shown(w, True)
            self._cfg(self._bar, fg_color=T.RAISED, progress_color=T.ACCENT)
            # progress
            self._cfg(self._prog_title, text=self._fit(f.progress_title, WIDTH - 2 * PAD))
            self._cfg(self._prog_text, text=f.progress_text)
            frac = f.progress_fraction
            if abs(float(self._bar.get()) - frac) > 1e-4:
                self._bar.set(frac)
            message, kind = f.message, f.message_kind
        # message
        self._msg_kind = kind if message else "info"
        self._cfg(self._msg, text=message, text_color=_TONE_COLOR.get(self._msg_kind, T.TEXT_2))
        # a close that waited for the download (or a delete) to stop
        if self._close_wanted and f.can_close and ff.can_close and not self._closing:
            later(self, 1, self.close)

    @staticmethod
    def _hint_for(f: DownloadFlow) -> str:
        """The line shown in the empty list: the phase's hint; for a failed listing the reason itself (the
        no-.gguf reason carries the -GGUF-repo advice)."""
        if f.phase == "listing":
            return HINT_LISTING
        if f.phase == "error":
            return f.list_hint or HINT_NONE
        return HINT_ENTER

    # ------------------------------------------------------------------ closing
    def close(self) -> None:
        """Close / X / Escape. While the folder view's question is up, the first close answers Keep. While a
        download runs it is cancelled first (the partial file is kept) and the window closes once hf_download has
        stopped; while a delete runs the window closes when it has answered. In the first ARM_S seconds the close
        waits for the arm time."""
        if self._destroyed or self._closing:
            return
        if self.folder.phase == "asking":
            self.folder.keep()
            return
        wait = self._armed_at - time.monotonic()
        if wait > 0:
            if not self._close_pending:
                self._close_pending = True
                later(self, int(wait * 1000) + 20, self._close_armed)
            return
        if not self.folder.close() or not self.flow.close():
            self._close_wanted = True
            return
        self._closing = True
        release_modal(self)
        try:
            self.destroy()
        except Exception:  # noqa: BLE001
            log.exception("DownloadDialog destroy failed")

    def _close_armed(self) -> None:
        self._close_pending = False
        self.close()

    def destroy(self) -> None:
        """Also runs when the master is destroyed under the window: a running download is stopped (partial file
        kept) and every later answer is ignored."""
        if not self._destroyed:
            self._destroyed = True
            self.flow_abandon()
            if self._pump_id is not None:
                try:
                    self.after_cancel(self._pump_id)
                except Exception:  # noqa: BLE001
                    log.debug("after_cancel failed", exc_info=True)
                self._pump_id = None
            release_modal(self)
        super().destroy()

    def flow_abandon(self) -> None:
        flow = getattr(self, "flow", None)
        if flow is not None:
            flow.abandon()
        folder = getattr(self, "folder", None)
        if folder is not None:
            folder.abandon()
