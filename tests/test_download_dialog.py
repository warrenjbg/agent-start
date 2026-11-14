"""download_dialog.DownloadDialog: the "Download a model" window against a fake hf object (no network, no disk).

Run:  .venv\\Scripts\\python.exe -u -X faulthandler tests\\test_download_dialog.py
Exit code 0 = all checks passed. Screenshots land in tests\\ui_download_<state>.png (git-ignored).
Opens only a bare CTk root and this dialog (no app window). It pops windows: ask before running it while the PC is
in use. Never contacts a server, never starts or stops a process, never writes or deletes a model file: the fake hf
and the fake `files` (in place of model_files, passed to EVERY dialog this suite opens) only pretend. Clicks are
invoke() / event_generate on the dialog's own widgets, never real mouse input.
"""
from __future__ import annotations

import gc
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import tkinter
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
# Anything the dialog's imports might write goes to a throwaway folder - never the project's files or %APPDATA%.
HOME = Path(tempfile.mkdtemp(prefix="agentstart-dl-home-"))
os.environ["AGENT_START_HOME"] = str(HOME)

import customtkinter as ctk  # noqa: E402

import config_store  # noqa: E402
import download_dialog as dd  # noqa: E402
import hf_download  # noqa: E402
import folder_flow  # noqa: E402
from download_dialog import DownloadDialog  # noqa: E402
from hf_download import HfError, HfFile, RepoListing  # noqa: E402
from model_files import ModelFile, ModelFileError  # noqa: E402

assert config_store.DATA_DIR == HOME, "tests must not write to the real data dir"
logging.getLogger("agent_start").addHandler(logging.NullHandler())

PASSES = 0
FAILS: list[str] = []
GIB = 1024 ** 3
MIB = 1024 ** 2
DEST = "C:\\Models\\llama-models"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPO = "acme/Qwen3.8-27B-GGUF"


def check(cond, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}", flush=True)


def eq(got, want, msg: str) -> None:
    check(got == want, f"{msg}: got {got!r}, want {want!r}")


# ---------------------------------------------------------------------------------------------------- fake hf
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
LISTING = RepoListing(repo_id=REPO, commit=COMMIT, files=(MODEL_A, MODEL_B, MODEL_C, VISION, SPLIT))
MANY = RepoListing(repo_id="acme/Many-GGUF", commit=COMMIT,
                   files=tuple(F(f"many-model-{i:02d}-Q4_K_M.gguf", (i + 1) * GIB) for i in range(30)))
LONG = RepoListing(repo_id="acme/Long-GGUF", commit=COMMIT,
                   files=(F("a-model-with-a-really-quite-extraordinarily-long-file-name-that-will-not-fit-"
                            "UD-Q4_K_XL.gguf", 21 * GIB), VISION))


class FakeHf:
    SPACE_MARGIN_BYTES = GIB

    def __init__(self) -> None:
        self.listings: dict[str, object] = {REPO: LISTING, "acme/Many-GGUF": MANY, "acme/Long-GGUF": LONG}
        self.pending: dict[str, int] = {}
        self.free: int | None = 212 * GIB
        self.script: list = []
        self.scripts: dict[str, list] = {}       # per repo path; files not named here follow `script`
        self.download_calls = 0
        self.max_active = 0
        self.active = 0
        self.lock = threading.Lock()
        self.list_calls = 0

    def parse_repo(self, text):
        m = re.fullmatch(r"(?:https?://)?(?:huggingface\.co/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/\S*)?",
                         text.strip())
        if not m:
            raise HfError("bad_repo")
        return f"{m.group(1)}/{m.group(2)}"

    def list_files(self, repo):
        self.list_calls += 1
        r = self.listings.get(repo)
        if isinstance(r, BaseException):
            raise r
        if r is None:
            raise HfError("not_found")
        return r

    def pending_part(self, dest, file, listing):
        return self.pending.get(file.path, 0)

    def discard_part(self, dest, file):
        self.pending.pop(file.path, None)

    def free_space(self, dest):
        return self.free

    def space_ok(self, dest, size, margin_bytes=GIB):
        return self.free is not None and self.free >= size + margin_bytes

    def download(self, listing, file, dest, *, progress=None, stop_evt=None, **kw):
        with self.lock:
            self.download_calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            for step in list(self.scripts.get(file.path, self.script)):
                op = step[0]
                if op == "p":
                    if progress is not None:
                        progress(step[1], file.size)
                    if len(step) > 2:
                        time.sleep(step[2])
                elif op == "wait":                 # block (on the worker thread) until the test releases it
                    while not step[1].wait(0.05):
                        if stop_evt is not None and stop_evt.is_set():
                            break
                elif op == "hold":                 # block until the test releases it, whatever stop_evt says:
                    step[1].wait(10)               # the test looks at the 'cancelling' state without a race
                elif op == "stop":
                    if stop_evt is not None and stop_evt.is_set():
                        raise HfError("cancelled")
                elif op == "raise":
                    raise step[1]
            return os.path.join(dest, file.name)
        finally:
            with self.lock:
                self.active -= 1


OTHER = os.path.normpath("D:/Other/Models")
LA = ("Ornith-1.5-9B-Q4_K_M.gguf", int(5.4 * GIB), 111)
LB = ("Ornith-1.5-9B-Q8_0.gguf", int(9.1 * GIB), 222)
LQ = ("Qwen3.8-27B-UD-IQ4_XS.gguf", int(13.4 * GIB), 333)
LM = ("mmproj-Ornith-1.5-9B-BF16.gguf", 879 * MIB, 444)


class FakeFiles:
    """model_files' surface for the folder view: list_folder, serving_paths, delete. It only pretends: no folder is
    read, no process is scanned, nothing on disk is touched. LQ is 'served by llama.cpp'."""

    def __init__(self) -> None:
        self.folders: dict[str, list[tuple[str, int, int]]] = {DEST: [LA, LB, LQ, LM], OTHER: [LB]}
        self.serving: set[str] = {os.path.join(DEST, LQ[0])}
        self.deletes: list[tuple[ModelFile, str, object]] = []
        self.delete_error: BaseException | None = None
        self.lists: list[str] = []
        self.lock = threading.Lock()

    def list_folder(self, folder):
        with self.lock:
            self.lists.append(folder)
            return tuple(ModelFile(os.path.join(folder, n), n, s, t) for n, s, t in self.folders.get(folder, []))

    def serving_paths(self):
        return frozenset(os.path.normcase(os.path.realpath(p)) for p in self.serving)

    def delete(self, file, folder, serving=frozenset()):
        with self.lock:
            self.deletes.append((file, folder, serving))
            if self.delete_error is not None:
                raise self.delete_error
            listed = self.folders.get(folder, [])
            listed[:] = [x for x in listed if x[0] != file.name]
            return file.size


def sync_runner(fn) -> None:
    fn()


def direct_post(fn, *args) -> None:
    fn(*args)


# ---------------------------------------------------------------------------------------------------- Tk helpers
def pump(root, seconds: float = 0.1) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def pump_until(root, cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        root.update()
        time.sleep(0.01)
    return bool(cond())


def alive(w) -> bool:
    try:
        return bool(w.winfo_exists())
    except tkinter.TclError:
        return False


def install_error_traps(root) -> list[str]:
    errs: list[str] = []
    root.report_callback_exception = lambda et, ev, tb: errs.append("".join(traceback.format_exception(et, ev, tb)))
    root.tk.createcommand("hc_bgerror", lambda msg, *a: errs.append(f"bgerror: {msg}"))
    root.tk.eval("proc bgerror {msg} {hc_bgerror $msg}")
    return errs


def pending_afters(root) -> int:
    return len(str(root.tk.call("after", "info")).split())


def open_dialog(root, hf: FakeHf, hidden: bool = True, threaded: bool = False, files: FakeFiles | None = None,
                deleted: list[str] | None = None) -> tuple[DownloadDialog, list[str]]:
    done: list[str] = []
    files = files if files is not None else FakeFiles()          # never the real model_files
    on_deleted = deleted.append if deleted is not None else None
    if threaded:      # the dialog's own fallback: daemon threads + its own answer queue (a bare root has no post())
        dlg = DownloadDialog(root, DEST, on_done=done.append, on_deleted=on_deleted, hf=hf, files=files)
    else:
        dlg = DownloadDialog(root, DEST, on_done=done.append, on_deleted=on_deleted, hf=hf, files=files,
                             run_worker=sync_runner, post=direct_post)
    if hidden:
        dlg.attributes("-alpha", 0.0)
    pump(root, 0.6)                                   # past ARM_S and the 200 ms raise
    return dlg, done


def close_dialog(root, dlg) -> None:
    if alive(dlg):
        dlg.close()
    pump(root, 0.1)
    if alive(dlg):
        dlg.destroy()
        pump(root, 0.05)


def shot(win, name: str) -> Path:
    import shots                                  # on screen, or drawn by Windows on a hidden desktop
    path, w, h = shots.grab(win, HERE / f"ui_download_{name}.png", opaque=True)
    if w:
        print(f"screenshot {path.name} ({w}x{h})", flush=True)
    return path


def placeholder_look(dlg) -> tuple[str, int, str]:
    """How the empty list's line is drawn: colour, font size, alignment of wrapped lines."""
    ph = dlg._placeholder
    return str(ph.cget("text_color")), int(ph.cget("font").cget("size")), str(ph._label.cget("justify"))


def side_margins(dlg) -> dict[str, tuple[int, int]]:
    """Left and right margin, in logical px, of every section of the window that is on screen."""
    scale = ctk.ScalingTracker.get_window_scaling(dlg)
    dlg.update_idletasks()
    width = dlg.winfo_width() / scale
    out = {}
    for w in dlg.grid_slaves(column=0):
        if w.winfo_manager() and w.winfo_ismapped():
            left = w.winfo_x() / scale
            out[f"row {w.grid_info()['row']}"] = (round(left), round(width - left - w.winfo_width() / scale))
    return out


def fits(dlg) -> tuple[bool, str]:
    scale = ctk.ScalingTracker.get_window_scaling(dlg)
    dlg.update_idletasks()
    rw, rh = dlg.winfo_reqwidth() / scale, dlg.winfo_reqheight() / scale
    ok = rw <= dlg.logical_width + 1 and rh <= dlg.logical_height + 1
    return ok, f"req {rw:.0f}x{rh:.0f} vs {dlg.logical_width}x{dlg.logical_height} (scale {scale})"


def listed(root, dlg, text: str = REPO) -> None:
    dlg.set_repo(text)
    dlg.list_files()
    pump(root, 0.1)


# ---------------------------------------------------------------------------------------------------- tests
def test_enter_and_bad_repo(root) -> Path | None:
    hf = FakeHf()
    dlg, _done = open_dialog(root, hf, hidden=False)
    path = None
    try:
        eq(dlg.title(), "Download a model", "window title")
        eq(dlg.logical_width, 420, "420 px wide, as the mockup")
        eq(dlg.row_texts(), [], "no rows yet")
        eq(dlg.list_placeholder(), dd.HINT_ENTER, "the empty list says what to do")
        eq(dlg.button_states(), {"List files": False, "Close": True, "Download": False, "Change…": True},
           "buttons before any text")
        eq(dlg.dest_line(), "To Models\\llama-models · 212 GB free · Change…", "the folder line")
        ok, why = fits(dlg)
        check(ok, f"empty window content fits its fixed size: {why}")
        dlg.set_repo("not a repo!")
        eq(dlg.button_states()["List files"], True, "List files enabled with text")
        check(bool(dlg._entry._entry.bind("<Return>")), "Enter in the repo box is bound (it lists, like the button)")
        dlg.list_files()                                # the same call; no key event: no test depends on the focus
        pump(root, 0.1)
        eq(dlg.list_placeholder(), hf_download._DEFAULT_MSG["bad_repo"], "bad repo: its sentence, in the list area")
        eq(dlg.message_text(), "", "...and not a second time in the message line")
        eq(placeholder_look(dlg), (dd._TONE_COLOR["error"], 12, "left"),
           "the reason is drawn as a message: error colour, body size, left-aligned")
        path = shot(dlg, "bad_repo")
        dlg.set_repo(REPO)
        eq((dlg.list_placeholder(), placeholder_look(dlg)), (dd.HINT_ENTER, (dd.T.TEXT_3, 11, "left")),
           "typing clears the reason: the hint is back, drawn as a hint")
    finally:
        close_dialog(root, dlg)
    return path


def test_list_and_select(root) -> Path | None:
    hf = FakeHf()
    dlg, _done = open_dialog(root, hf, hidden=False)
    path = None
    try:
        listed(root, dlg)
        rows = dlg.row_texts()
        eq(rows[:4], [
            ("Qwen3.8-27B-UD-Q2_K_XL.gguf", "", "9.8 GB"),
            ("Qwen3.8-27B-UD-Q3_K_XL.gguf", "", "12.2 GB"),
            ("Qwen3.8-27B-UD-IQ4_XS.gguf", "", "13.4 GB"),
            ("mmproj-BF16.gguf", "· vision file", "922 MB")], "rows as the mockup")
        split = rows[4] if len(rows) == 5 else ("", "", "")
        full = "Qwen3.8-27B-BF16-00001-of-00002.gguf"
        check(split[1:] == ("· 2 parts, not supported", "50.1 GB")
              and (split[0] == full or (split[0].endswith("…") and full.startswith(split[0][:-1]) and len(split[0]) > 12)),
              f"the split set: one greyed row with its reason; a long name is shortened to make room for it ({split})")
        eq(dlg.list_placeholder(), "", "no placeholder with rows")
        eq([s for s, _sel in dlg.row_states()], [True, True, True, True, False], "split row cannot be picked")
        dlg.choose_row(4)
        pump(root, 0.05)
        eq([sel for _s, sel in dlg.row_states()], [False] * 5, "clicking the split row picks nothing")
        eq((dlg.message_text(), dlg.message_kind()), (SPLIT.note, "warn"), "the split row says why")
        # a click on a row (its file-name label) picks it, as the user would
        name_label = dlg._row_widgets[1][3][0]
        name_label._label.event_generate("<Button-1>")
        pump(root, 0.05)
        eq([sel for _s, sel in dlg.row_states()], [False, True, False, False, False], "row 2 picked by a click")
        eq(dlg.button_states()["Download"], True, "Download enabled")
        eq(dlg.progress_title(), MODEL_B.name, "the chosen file is named above the bar")
        eq(dlg.message_text(), "", "picking clears the warning")
        ok, why = fits(dlg)
        check(ok, f"list window content fits: {why}")
        path = shot(dlg, "list")
    finally:
        close_dialog(root, dlg)
    return path


def test_resume_and_discard(root) -> Path | None:
    hf = FakeHf()
    hf.pending[MODEL_B.path] = int(4.6 * GIB)
    dlg, _done = open_dialog(root, hf, hidden=False)
    path = None
    try:
        listed(root, dlg)
        eq(dlg.row_texts()[1][1], "· 37% downloaded", "the kept part shows in the row")
        dlg.choose_row(1)
        pump(root, 0.05)
        states = dlg.button_states()
        eq((states.get("Resume"), states.get("Download")), (True, None), "Download reads Resume")
        eq(states.get("Discard partial download"), True, "Discard offered")
        eq(dlg.progress_text(), "4.6 of 12.2 GB already downloaded · Resume continues from there",
           "the kept part under the bar")
        check(abs(dlg.progress_value() - 4.6 / 12.2) < 0.02, f"bar at the kept part: {dlg.progress_value()}")
        path = shot(dlg, "resume")
        dlg._discard_btn.invoke()
        pump(root, 0.1)
        eq(dlg.message_text(), dd.DISCARDED, "discarded")
        states = dlg.button_states()
        eq((states.get("Download"), "Discard partial download" in states), (True, False), "back to Download")
    finally:
        close_dialog(root, dlg)
    return path


def test_progress_and_cancel(root) -> Path | None:
    """Real worker threads + the dialog's own answer queue: progress shows while the fake download is blocked."""
    hf = FakeHf()
    gate = threading.Event()
    # samples over ~1.2 s, so the smoothed rate (RATE_MIN_SPAN_S = 1 s) and the ETA are known while it waits
    hf.script = [("p", 0, 0.6), ("p", int(1.5 * GIB), 0.6), ("p", int(4.6 * GIB), 0.1), ("hold", gate), ("stop",)]
    dlg, done = open_dialog(root, hf, hidden=False, threaded=True)
    path = None
    try:
        dlg.set_repo(REPO)
        dlg.list_files()
        check(pump_until(root, lambda: len(dlg.row_texts()) == 5), "threaded listing arrives")
        dlg.choose_row(1)
        pump(root, 0.05)
        dlg._download_btn.invoke()
        check(pump_until(root, lambda: dlg.progress_text().startswith("4.6 of 12.2 GB · 37%")),
              f"progress line: {dlg.progress_text()!r}")
        check(re.search(r"· \d+(\.\d)? (MB|KB)/s · (about|less)", dlg.progress_text()) is not None,
              f"rate and ETA shown: {dlg.progress_text()!r}")
        states = dlg.button_states()
        eq((states.get("Cancel download"), states.get("Download"), states.get("List files"), states.get("Change…")),
           (True, False, False, False), "only Cancel (and Close) while downloading")
        eq(dlg.entry_enabled(), False, "the repo box is locked while downloading")
        cancel = dlg._cancel_btn
        eq((str(cancel.cget("text_color")), str(cancel.cget("font").cget("weight")), int(cancel.cget("font").cget("size"))),
           (dd.T.RED_TEXT, "bold", 12), "Cancel download: bold, in the error text's colour")
        eq(str(dlg._download_btn.cget("font").cget("weight")), "normal", "...and it is the only bold button")
        margins = side_margins(dlg)
        eq(dd.PAD, 20, "the side margin is 20 px")
        check(len(margins) >= 9 and all(lr == (dd.PAD, dd.PAD) for name, lr in margins.items() if name != "row 10")
              and margins["row 10"][0] == dd.PAD and margins["row 10"][1] >= dd.PAD,
              f"every section starts and ends on the side margin while downloading: {margins}")
        cw = cancel.winfo_width() / ctk.ScalingTracker.get_window_scaling(dlg)
        check(cw <= dd.WIDTH - 2 * dd.PAD and dlg._measure(12, "Cancel download") * 1.12 + 16 <= cw,
              f"the bold label has room in its button ({cw:.0f} px)")
        check(abs(dlg.progress_value() - 4.6 / 12.2) < 0.02, f"bar follows: {dlg.progress_value()}")
        path = shot(dlg, "progress")
        hf.pending[MODEL_B.path] = int(4.6 * GIB)
        dlg._cancel_btn.invoke()
        pump(root, 0.05)
        eq(dlg.message_text(), dd.CANCELLING, "cancelling")
        eq(dlg.button_states().get("Cancel download"), False, "Cancel greyed while stopping")
        gate.set()                                   # now the (fake) download notices the stop
        check(pump_until(root, lambda: dlg.flow.phase == "list"), "the download stopped")
        eq(dlg.message_text(), hf_download._DEFAULT_MSG["cancelled"], "cancel says the part is kept")
        eq(dlg.button_states().get("Resume"), True, "Resume offered")
        eq(done, [], "no on_done")
        eq(hf.max_active, 1, "one download at a time")
    finally:
        gate.set()
        close_dialog(root, dlg)
    return path


def test_done(root) -> Path | None:
    hf = FakeHf()
    hf.script = [("p", 0), ("p", MODEL_B.size // 2), ("p", MODEL_B.size)]
    dlg, done = open_dialog(root, hf, hidden=False)
    path = None
    try:
        listed(root, dlg)
        dlg.choose_row(1)
        dlg._download_btn.invoke()
        pump(root, 0.1)
        eq(dlg.message_text(), dd.DONE_MODEL, "done message")
        eq(dlg.message_kind(), "ok", "done in green")
        eq(done, [os.path.join(DEST, MODEL_B.name)], "on_done once with the path")
        eq(dlg.progress_value(), 1.0, "bar full")
        eq(dlg.progress_text(), "12.2 of 12.2 GB · 100%", "done line")
        eq(dlg.row_texts()[1][1], "· downloaded", "the row says downloaded")
        eq(dlg.button_states().get("Download"), False, "the same file cannot be downloaded again")
        ok, why = fits(dlg)
        check(ok, f"done window content fits: {why}")
        path = shot(dlg, "done")
    finally:
        close_dialog(root, dlg)
    return path


def test_errors(root) -> Path | None:
    hf = FakeHf()
    dlg, _done = open_dialog(root, hf, hidden=False)
    path = None
    try:
        hf.listings[REPO] = HfError("gated")
        listed(root, dlg)
        eq((dlg.list_placeholder(), dlg.message_text()), (hf_download._DEFAULT_MSG["gated"], ""),
           "gated repo: its sentence in the list area, the message line empty")
        hf.listings[REPO] = HfError("rate_limited")
        dlg.list_files()
        pump(root, 0.05)
        eq(placeholder_look(dlg)[0], dd._TONE_COLOR["warn"], "a 'try again later' reason keeps the warning colour")
        hf.listings[REPO] = HfError("no_gguf")
        dlg.list_files()
        pump(root, 0.05)
        ph = dlg.list_placeholder()
        check(ph.startswith(hf_download._DEFAULT_MSG["no_gguf"]) and dd.NO_GGUF_HINT in ph,
              f"the empty repo list names the reason and the -GGUF repos: {ph!r}")
        scale = ctk.ScalingTracker.get_window_scaling(dlg)
        ph_h = dlg._placeholder.winfo_height() / scale
        ok, why = fits(dlg)
        check(2 * 14 <= ph_h <= dd.LIST_H and ok,
              f"the longest reason wraps onto several lines and fits the list area ({ph_h:.0f} px of {dd.LIST_H}; {why})")
        shot(dlg, "no_gguf")
        hf.listings[REPO] = LISTING
        dlg.list_files()
        pump(root, 0.05)
        hf.free = 5 * GIB
        dlg.choose_row(1)
        dlg._download_btn.invoke()
        pump(root, 0.1)
        check("Free up 8.2 GB more" in dlg.message_text(), f"not enough space: {dlg.message_text()!r}")
        eq(dlg.message_kind(), "error", "space shown as an error")
        path = shot(dlg, "no_space")
        hf.free = 212 * GIB
        hf.script = [("raise", RuntimeError("boom"))]
        dlg._download_btn.invoke()
        pump(root, 0.1)
        eq(dlg.message_text(), dd.UNEXPECTED, "an unexpected failure: the generic sentence")
        hf.script = [("raise", HfError("mismatch"))]
        dlg._download_btn.invoke()
        pump(root, 0.1)
        eq(dlg.message_text(), hf_download._DEFAULT_MSG["mismatch"], "mismatch sentence")
        eq(dlg.button_states().get("Download"), True, "usable after an error")
    finally:
        close_dialog(root, dlg)
    return path


def test_long_list_scrolls(root) -> Path | None:
    hf = FakeHf()
    dlg, _done = open_dialog(root, hf, hidden=False)
    path = None
    try:
        geo = dlg.geometry().split("+")[0]
        listed(root, dlg, "acme/Many-GGUF")
        pump(root, 0.2)
        eq(len(dlg.row_texts()), 30, "30 rows")
        canvas = dlg._list._parent_canvas                # CTkScrollableFrame's canvas (CTk internal)
        top, bottom = canvas.yview()
        check(top == 0.0 and bottom < 0.5, f"the list scrolls: view {(top, bottom)}")
        eq(dlg.geometry().split("+")[0], geo, "window size unchanged by a long list")
        ok, why = fits(dlg)
        check(ok, f"long list window fits: {why}")
        path = shot(dlg, "long_list")
        listed(root, dlg, "acme/Long-GGUF")
        name = dlg.row_texts()[0][0]
        check(name.endswith("…") and len(name) < len(LONG.files[0].name), f"a long name is shortened: {name!r}")
        eq(dlg.row_texts()[0][2], "21.0 GB", "its size still shows")
        shot(dlg, "long_name")
    finally:
        close_dialog(root, dlg)
    return path


def test_change_folder(root) -> None:
    hf = FakeHf()
    dlg, _done = open_dialog(root, hf)
    saved = dd.filedialog.askdirectory
    try:
        dd.filedialog.askdirectory = lambda **kw: "D:/Other/Models"      # no native picker in a test
        hf.free = 80 * GIB
        dlg._change_link._label.event_generate("<Button-1>")
        pump(root, 0.1)
        eq(dlg.flow.dest_dir, os.path.normpath("D:/Other/Models"), "Change… sets the folder")
        eq(dlg.dest_line(), "To Other\\Models · 80 GB free · Change…", "the folder line follows")
        dd.filedialog.askdirectory = lambda **kw: ""                     # picker cancelled
        dlg.change_folder()
        eq(dlg.flow.dest_dir, os.path.normpath("D:/Other/Models"), "a cancelled picker changes nothing")
    finally:
        dd.filedialog.askdirectory = saved
        close_dialog(root, dlg)


def test_close_paths(root) -> None:
    hf = FakeHf()
    for how in ("button", "x", "escape", "close"):
        dlg, _done = open_dialog(root, hf)
        if how == "button":
            dlg._close_btn.invoke()
        elif how == "x":
            dlg.tk.call(dlg.protocol("WM_DELETE_WINDOW"))
        elif how == "escape":
            dlg.event_generate("<Escape>")
        else:
            dlg.close()
        pump(root, 0.1)
        check(not alive(dlg), f"{how} closes the idle window")
    # an early close waits for the arm time
    dlg = DownloadDialog(root, DEST, hf=hf, files=FakeFiles(), run_worker=sync_runner, post=direct_post)
    dlg.attributes("-alpha", 0.0)
    pump(root, 0.05)
    dlg.close()
    pump(root, 0.1)
    check(alive(dlg), "early close is deferred")
    pump(root, 0.6)
    check(not alive(dlg), "early close happens once armed")
    # closing during a download: cancelled first, the window goes once it has stopped
    gate = threading.Event()
    hf.script = [("p", GIB, 0.1), ("wait", gate), ("stop",)]
    dlg, done = open_dialog(root, hf, threaded=True)
    try:
        dlg.set_repo(REPO)
        dlg.list_files()
        pump_until(root, lambda: len(dlg.row_texts()) == 5)
        dlg.choose_row(0)
        dlg._download_btn.invoke()
        pump_until(root, lambda: dlg.flow.done_bytes >= GIB)
        dlg.tk.call(dlg.protocol("WM_DELETE_WINDOW"))
        pump(root, 0.05)
        check(alive(dlg), "X during a download: still open while it stops")
        eq(dlg.message_text(), dd.CLOSING, "says it stops first")
        check(dlg.flow.closing, "the flow knows it is closing")
        check(pump_until(root, lambda: not alive(dlg), 5.0), "the window closes once the download stopped")
        eq(done, [], "no on_done after a cancel")
    finally:
        gate.set()
        if alive(dlg):
            dlg.destroy()
    # the master goes while a download runs: no Tk call from the worker, nothing raises
    master = ctk.CTkToplevel(root)
    master.attributes("-alpha", 0.0)
    master.geometry("340x300")
    pump(root, 0.5)
    gate2 = threading.Event()
    hf.script = [("p", GIB, 0.1), ("wait", gate2), ("p", int(1.5 * GIB)), ("stop",)]
    dlg = DownloadDialog(master, DEST, hf=hf, files=FakeFiles())
    dlg.attributes("-alpha", 0.0)
    pump(root, 0.6)
    dlg.set_repo(REPO)
    dlg.list_files()
    pump_until(root, lambda: len(dlg.row_texts()) == 5)
    dlg.choose_row(0)
    dlg._download_btn.invoke()
    pump_until(root, lambda: dlg.flow.done_bytes >= GIB)
    raised = None
    try:
        master.destroy()
    except Exception as exc:  # noqa: BLE001
        raised = exc
    gate2.set()
    pump(root, 0.5)
    eq(raised, None, "destroying the master during a download raises nothing")
    check(not alive(dlg), "the window went with its master")
    check(dlg.flow.closed, "its flow was abandoned (late answers ignored)")


def glyphs(dlg) -> list[tuple[str, str, str]]:
    """Per row as drawn: (mark glyph, its colour, the row's background)."""
    return [(str(g.cget("text")), str(g.cget("text_color")), str(fr.cget("fg_color")))
            for _row, fr, g, _l in dlg._row_widgets]


def x_span(dlg, w) -> tuple[float, float]:
    """Left and right edge of widget w inside the window, in logical px."""
    scale = ctk.ScalingTracker.get_window_scaling(dlg)
    dlg.update_idletasks()
    left = (w.winfo_rootx() - dlg.winfo_rootx()) / scale
    return left, left + w.winfo_width() / scale


def test_batch(root) -> Path | None:
    """Several ticked files: checkbox marks, 'Download N files', no Discard, 'File 2 of 3: ...' while it runs."""
    hf = FakeHf()
    hf.pending[MODEL_B.path] = int(4.6 * GIB)
    gate = threading.Event()
    hf.scripts = {MODEL_B.path: [("p", int(4.6 * GIB), 0.6), ("p", int(5.5 * GIB), 0.6), ("p", int(6.1 * GIB), 0.1),
                                 ("hold", gate), ("p", MODEL_B.size)]}
    hf.script = [("p", 0), ("p", GIB // 2)]
    dlg, done = open_dialog(root, hf, hidden=False, threaded=True)
    path = None
    try:
        dlg.set_repo(REPO)
        dlg.list_files()
        check(pump_until(root, lambda: len(dlg.row_texts()) == 5), "batch: listing arrives")
        T_ = dd.T
        off_sel, off_grey = (dd.CHECK_OFF, T_.TEXT_2, T_.SURFACE), (dd.CHECK_OFF, T_.HAIRLINE, T_.SURFACE)
        on = (dd.CHECK_ON, T_.TEXT, T_.RAISED)
        eq(glyphs(dlg), [off_sel] * 4 + [off_grey], "empty checkboxes; the split row's greyed")
        eq(dd.CHECK_OFF, "\uE739", "the empty mark is Segoe Fluent Icons Checkbox")
        eq(dd.CHECK_ON, "\uE73D", "the ticked mark is CheckboxCompositeReversed")
        g0 = dlg._row_widgets[0][2]
        eq((str(g0.cget("font").cget("family")), int(g0.cget("font").cget("size")), int(g0.cget("width")),
            g0.grid_info()["column"]), (T_.icon_family(), 12, 16, 0), "the mark: same font, size, width, column")
        name1 = dlg._row_widgets[1][3][0]
        name1._label.event_generate("<Button-1>")      # a click on row 2's name ticks it
        pump(root, 0.05)
        eq(dlg.ticked_rows(), [1], "a click ticks the row")
        eq(glyphs(dlg)[1], on, "a ticked row: the ticked box, today's selected look")
        states = dlg.button_states()
        eq((states.get("Resume"), states.get("Discard partial download")), (True, True),
           "one ticked with a kept part: Resume and Discard, as today")
        dlg.choose_row(0)
        pump(root, 0.05)
        eq((dlg.ticked_rows(), [s for _a, s in dlg.row_states()]), ([0, 1], [True, True, False, False, False]),
           "two ticked")
        states = dlg.button_states()
        eq((states.get("Download 2 files"), "Discard partial download" in states, "Resume" in states),
           (True, False, False), "two ticked: 'Download 2 files', no Discard")
        eq((dlg.progress_title(), dlg.progress_text()), ("", ""), "nothing named under the bar while several wait")
        dlg.choose_row(3)
        pump(root, 0.05)
        eq(dlg.button_states().get("Download 3 files"), True, "three ticked: 'Download 3 files'")
        eq(glyphs(dlg), [on, on, off_sel, on, off_grey], "three ticked boxes, the others empty")
        dlg.choose_row(4)
        pump(root, 0.05)
        eq(dlg.ticked_rows(), [0, 1, 3], "the split row cannot be ticked")
        dlg.choose_row(3)
        pump(root, 0.05)
        eq((dlg.ticked_rows(), glyphs(dlg)[3]), ([0, 1], off_sel), "a second click unticks")
        dlg.choose_row(0)
        pump(root, 0.05)
        eq(dlg.button_states().get("Resume"), True, "back to one ticked: Resume")
        dlg.choose_row(0)
        dlg.choose_row(3)
        pump(root, 0.05)
        # the wider button: right-aligned, Close to its left with the same gap
        b, c = dlg._download_btn, dlg._close_btn
        bl, br = x_span(dlg, b)
        cl, cr = x_span(dlg, c)
        check(abs(br - (dd.WIDTH - dd.PAD)) <= 1 and abs(bl - cr - 8) <= 1,
              f"Download stays right-aligned, Close 8 px to its left (close {cl:.0f}-{cr:.0f}, "
              f"download {bl:.0f}-{br:.0f})")
        text_w = dlg._measure(12, "Download 12 files")
        check(text_w + 2 * 8 <= br - bl, f"'Download 12 files' has room in the button ({br - bl:.0f} px, text {text_w})")
        ok, why = fits(dlg)
        check(ok, f"batch: three ticked, the window fits: {why}")
        dlg._download_btn.invoke()
        want_title = f"File 2 of 3: {MODEL_B.name}"
        check(pump_until(root, lambda: dlg.progress_title() == want_title and dlg.progress_text().startswith("6.1 of")),
              f"while file 2 of 3 runs: {dlg.progress_title()!r} / {dlg.progress_text()!r}")
        eq(done, [os.path.join(DEST, MODEL_A.name)], "file 1 already reported")
        eq(dlg.row_texts()[0][1], "· downloaded", "file 1's row: downloaded")
        eq((dlg.ticked_rows(), glyphs(dlg)[0][0]), ([1, 3], dd.CHECK_OFF), "file 1 no longer ticked")
        states = dlg.button_states()
        eq((states.get("Cancel download"), states.get("Download 2 files"), "Discard partial download" in states),
           (True, False, False), "while it runs: Cancel; Download greyed")
        check(abs(dlg.progress_value() - 6.1 / 12.2) < 0.02, f"the bar is file 2's: {dlg.progress_value()}")
        ok, why = fits(dlg)
        check(ok, f"batch: the window fits while file 2 of 3 runs: {why}")
        path = shot(dlg, "batch")
        gate.set()
        check(pump_until(root, lambda: dlg.flow.phase == "done"), "the batch finishes")
        eq((dlg.message_text(), dlg.message_kind()), (dd.DONE_MANY.format(n=3), "ok"), "the several-files sentence")
        eq(done, [os.path.join(DEST, x.name) for x in (MODEL_A, MODEL_B, VISION)], "each reported once, in list order")
        eq((dlg.ticked_rows(), hf.max_active), ([], 1), "nothing ticked; never two at once")
    finally:
        gate.set()
        close_dialog(root, dlg)
    return path


def boxes(dlg) -> dict[int, tuple[int, int]]:
    """Top and height, in logical px, of every section of the window that is on screen, by grid row."""
    scale = ctk.ScalingTracker.get_window_scaling(dlg)
    dlg.update_idletasks()
    out = {}
    for w in dlg.grid_slaves(column=0):
        if w.winfo_manager() and w.winfo_ismapped():
            out[int(w.grid_info()["row"])] = (round(w.winfo_y() / scale), round(w.winfo_height() / scale))
    return out


def label_look(w) -> tuple[str, int, str]:
    return str(w.cget("text_color")), int(w.cget("font").cget("size")), str(w.cget("font").cget("weight"))


def row_colours(dlg) -> list[tuple[str, str, str, str]]:
    """Per row as drawn: (mark, mark colour, name colour, size colour)."""
    return [(str(g.cget("text")), str(g.cget("text_color")), str(lab[0].cget("text_color")),
             str(lab[2].cget("text_color"))) for _r, _f, g, lab in dlg._row_widgets]


def test_folder_view(root) -> Path | None:
    """The 'In this folder' view: the switch, the rows, Delete…, the question, Keep, Delete for good, a refusal."""
    hf = FakeHf()
    files = FakeFiles()
    deleted: list[str] = []
    dlg, _done = open_dialog(root, hf, hidden=False, files=files, deleted=deleted)
    path = None
    T_ = dd.T
    try:
        # ---- the Hugging Face view: the window as it was, with the switch in the heading
        eq((dlg.view(), dlg.heading_text()), ("hf", "Download a model"), "the window opens on Hugging Face")
        eq(dlg.switch_states(), {"Hugging Face": (True, True), "In this folder": (False, True)},
           "the switch: Hugging Face chosen, both parts usable")
        eq(files.lists, [], "the folder is not read while its view is not shown")
        tabs = dlg._tabs
        eq((str(tabs["hf"].cget("fg_color")), str(tabs["folder"].cget("fg_color")),
            str(dlg._switch.cget("fg_color"))), (T_.RAISED, T_.SURFACE, T_.SURFACE),
           "switch: the track SURFACE, the chosen part RAISED")
        eq([label_look(t)[:2] for t in tabs.values()], [(T_.TEXT, 11), (T_.TEXT, 11)], "switch text: TEXT, 11 px")
        hf_boxes = boxes(dlg)
        scale = ctk.ScalingTracker.get_window_scaling(dlg)
        sw_h = dlg._switch.winfo_height() / scale
        check(abs(sw_h - 20) <= 1 and abs(hf_boxes[0][1] - 20) <= 1,
              f"the switch is about 20 px high; the heading row keeps its height ({sw_h:.0f}, {hf_boxes[0]})")
        # ---- switch to the folder view with a click
        tabs["folder"]._label.event_generate("<Button-1>")
        pump(root, 0.1)
        eq((dlg.view(), dlg.heading_text()), ("folder", "Models in this folder"), "a click on 'In this folder'")
        eq(dlg.switch_states(), {"Hugging Face": (False, True), "In this folder": (True, True)},
           "the switch follows")
        eq((str(tabs["hf"].cget("fg_color")), str(tabs["folder"].cget("fg_color"))), (T_.SURFACE, T_.RAISED),
           "the chosen part is raised")
        eq(files.lists, [DEST], "the folder is read when its view is chosen")
        check(not dlg._repo_box.winfo_manager(), "no repo box and no List files in the folder view")
        fb = boxes(dlg)
        check(1 not in fb and abs((fb[2][1] - hf_boxes[2][1]) - dd.LIST_GROW) <= 1 and dd.LIST_GROW == 38,
              f"the list is 38 px taller ({hf_boxes[2]} -> {fb[2]})")
        same = {r: (hf_boxes[r][0], fb.get(r, (None,))[0]) for r in (3, 4, 5, 6, 7, 8, 9, 10)}
        check(all(a is not None and b is not None and abs(a - b) <= 1 for a, b in same.values())
              and abs(sum(fb[2]) - sum(hf_boxes[2])) <= 1,
              f"everything below the list stays where it was (row: hf y, folder y) {same}")
        ok, why = fits(dlg)
        check(ok, f"folder view content fits: {why}")
        eq(dlg.folder_rows(), [(LA[0], "", "5.4 GB"), (LB[0], "", "9.1 GB"),
                               (LQ[0], "· in use by llama.cpp", "13.4 GB"), (LM[0], "", "879 MB")],
           "the folder's .gguf files with their sizes; the served one says so")
        eq([s for s, _c in dlg.row_states()], [True, True, False, True], "the served file cannot be chosen")
        off = (dd.RADIO_OFF, T_.TEXT_2, T_.TEXT, T_.TEXT_2)
        grey = (dd.RADIO_OFF, T_.HAIRLINE, T_.TEXT_3, T_.TEXT_3)
        eq(row_colours(dlg), [off, off, grey, off], "round marks; the served row greyed as an unsupported row")
        eq((dd.RADIO_OFF, dd.RADIO_ON), ("\uECCA", "\uECCB"), "the round marks: Segoe Fluent Icons RadioBtnOff / On")
        eq(dlg.dest_line(), "In Models\\llama-models · 212 GB free · Change…", "the folder line says In")
        eq(dlg.button_states(), {"Close": True, "Delete…": False, "Change…": True},
           "Close and a greyed Delete… (nothing chosen)")
        db = dlg._delete_btn
        eq((str(db.cget("fg_color")), str(db.cget("text_color")), str(db.cget("text_color_disabled")),
            str(db.cget("font").cget("weight")), int(db.cget("width"))),
           (T_.RAISED, T_.RED_TEXT, T_.TEXT_3, "bold", dd.DOWNLOAD_W),
           "Delete…: RAISED fill, bold, red text; TEXT_3 greyed")
        dl, dr = x_span(dlg, db)
        cl, cr = x_span(dlg, dlg._close_btn)
        check(abs(dr - (dd.WIDTH - dd.PAD)) <= 1 and abs(dl - cr - 8) <= 1,
              f"Delete… where Download was: right-aligned, Close 8 px to its left "
              f"({cl:.0f}-{cr:.0f}, {dl:.0f}-{dr:.0f})")
        eq((dlg.progress_title(), dlg.progress_text(), dlg.question_text(), str(dlg._bar.cget("progress_color")),
            str(dlg._bar.cget("fg_color"))), ("", "", "", T_.BG, T_.BG), "under the hairline: nothing shown")
        # ---- choosing
        dlg._row_widgets[1][3][0]._label.event_generate("<Button-1>")      # a click on row 2's name
        pump(root, 0.05)
        eq(dlg.ticked_rows(), [1], "a click chooses the row")
        eq(row_colours(dlg)[1][:2], (dd.RADIO_ON, T_.TEXT), "the chosen row: the filled circle")
        eq(str(dlg._row_widgets[1][1].cget("fg_color")), T_.RAISED, "...on the selected background")
        eq(dlg.button_states()["Delete…"], True, "Delete… is on with a row chosen")
        dlg.choose_folder_row(2)
        pump(root, 0.05)
        eq(dlg.ticked_rows(), [1], "the served row cannot be chosen")
        dlg.choose_folder_row(0)
        dlg.choose_folder_row(0)
        pump(root, 0.05)
        eq(dlg.ticked_rows(), [0], "one row at a time; a click on the chosen row keeps it")
        dlg.choose_folder_row(1)
        pump(root, 0.05)
        margins = side_margins(dlg)
        check(all(lr == (dd.PAD, dd.PAD) for name, lr in margins.items() if name != "row 10")
              and margins["row 10"][0] == dd.PAD and margins["row 10"][1] >= dd.PAD,
              f"folder view: every section on the side margin: {margins}")
        path = shot(dlg, "folder")
        # ---- Delete…: the question
        asked = dlg.folder.rows[1].file
        dlg.press_delete()
        pump(root, 0.05)
        eq(dlg.question_text(), "Delete Ornith-1.5-9B-Q8_0.gguf (9.1 GB) for good? This cannot be undone.",
           "the question names the file and its size")
        eq(dlg.button_states(), {"Close": True, "Delete…": False, "Keep": True, "Delete for good": True,
                                 "Change…": False}, "the question up: Delete… and Change… off, Keep, Delete for good")
        eq(dlg.switch_states(), {"Hugging Face": (False, False), "In this folder": (True, False)},
           "the switch is off while the question is up")
        eq([label_look(t)[0] for t in tabs.values()], [T_.TEXT_3, T_.TEXT_3], "...and greyed")
        dlg.choose_folder_row(0)
        pump(root, 0.05)
        eq(dlg.ticked_rows(), [1], "rows cannot be chosen while the question is up")
        check(not dlg.set_view("hf") and dlg.view() == "folder", "the view cannot be switched")
        qt = dlg._ask_text
        eq((label_look(qt)[:2], str(qt._label.cget("justify")), str(qt.cget("anchor"))), ((T_.TEXT, 12), "left", "nw"),
           "the question: 12 px, TEXT, left-aligned")
        kb, yb = dlg._keep_btn, dlg._yes_btn
        eq((str(kb.cget("fg_color")), int(kb.cget("border_width")), str(kb.cget("border_color")),
            str(kb.cget("text_color")), str(kb.cget("font").cget("weight"))),
           ("transparent", 1, T_.RAISED_HOVER, T_.TEXT, "normal"), "Keep: an outline button, as Close")
        eq((str(yb.cget("fg_color")), str(yb.cget("hover_color")), str(yb.cget("text_color")),
            str(yb.cget("font").cget("weight"))), (T_.DANGER, T_.DANGER_HOVER, T_.TEXT, "bold"),
           "Delete for good: the danger fill, bold")
        yl, yr = x_span(dlg, yb)
        kl, kr = x_span(dlg, kb)
        check(abs(yr - (dd.WIDTH - dd.PAD)) <= 1 and abs(yl - kr - 8) <= 1,
              f"Keep and Delete for good right-aligned, 8 px apart ({kl:.0f}-{kr:.0f}, {yl:.0f}-{yr:.0f})")
        cb = boxes(dlg)
        check(abs(cb[10][0] - hf_boxes[10][0]) <= 1 and 6 in cb and 7 not in cb,
              f"the question takes the name / bar rows; the message line stays put ({cb})")
        ok, why = fits(dlg)
        check(ok, f"the question fits: {why}")
        margins = side_margins(dlg)
        check(all(lr == (dd.PAD, dd.PAD) for name, lr in margins.items() if name != "row 10"),
              f"question: every section on the side margin: {margins}")
        path = shot(dlg, "folder_confirm") or path
        # ---- Keep
        dlg.press_keep()
        pump(root, 0.05)
        eq((dlg.question_text(), dlg.ticked_rows(), files.deletes), ("", [1], []),
           "Keep: the question goes, the same row stays chosen, nothing deleted")
        eq(dlg.switch_states()["In this folder"], (True, True), "...and the switch is back")
        # ---- Delete for good
        hf.free = 221 * GIB
        dlg.press_delete()
        dlg.press_delete_for_good()
        pump(root, 0.1)
        eq(len(files.deletes), 1, "Delete for good: one delete")
        check(files.deletes[0][0] is asked and files.deletes[0][1] == DEST,
              "...of the very file the question named, in the folder")
        eq([r[0] for r in dlg.folder_rows()], [LA[0], LQ[0], LM[0]], "the row goes (the folder is read again)")
        eq(dlg.ticked_rows(), [], "no row is chosen")
        eq((dlg.message_text(), dlg.message_kind()), ("Deleted Ornith-1.5-9B-Q8_0.gguf (9.1 GB).", "info"),
           "the message says what went")
        eq(dlg.dest_line(), "In Models\\llama-models · 221 GB free · Change…", "the free space is read again")
        eq(deleted, [asked.path], "on_deleted once, with the path")
        eq(dlg.question_text(), "", "the question is gone")
        # ---- a refusal
        msg = "Ornith-1.5-9B-Q4_K_M.gguf is in use by another program; close that program, then try again."
        files.delete_error = ModelFileError("in_use", msg)
        dlg.choose_folder_row(0)
        dlg.press_delete()
        dlg.press_delete_for_good()
        pump(root, 0.1)
        eq((dlg.message_text(), dlg.message_kind()), (msg, "warn"), "a refusal: its sentence, in the warning colour")
        eq((dlg.ticked_rows(), len(files.deletes), deleted), ([0], 2, [asked.path]),
           "...the choice is kept, one delete call, no on_deleted")
        files.delete_error = ModelFileError("changed", "x changed.")
        dlg.press_delete()
        dlg.press_delete_for_good()
        pump(root, 0.1)
        eq(dlg.message_kind(), "error", "another refusal: the error colour")
        # ---- back to Hugging Face
        dlg.set_view("hf")
        pump(root, 0.05)
        eq((dlg.view(), dlg.heading_text(), bool(dlg._repo_box.winfo_manager())), ("hf", "Download a model", True),
           "back to Hugging Face: heading and repo row")
        eq(dlg.button_states(), {"List files": False, "Close": True, "Download": False, "Change…": True},
           "the download side's buttons, as before")
        eq((dlg.dest_line().split(" ")[0], dlg.list_placeholder(), dlg.message_text()), ("To", dd.HINT_ENTER, ""),
           "'To', the hint and the download side's (empty) message")
        eq(boxes(dlg), {**hf_boxes}, "the layout is the Hugging Face one again")
        eq((str(dlg._bar.cget("fg_color")), str(dlg._bar.cget("progress_color"))), (T_.RAISED, T_.ACCENT),
           "the bar is back")
    finally:
        close_dialog(root, dlg)
    return path


def test_folder_switch_rules(root) -> None:
    """The download side is left as it was; listed again only after a delete; the switch is off while downloading;
    Close / Escape answer Keep first; Change… in the folder view; an empty folder."""
    hf = FakeHf()
    files = FakeFiles()
    dlg, _done = open_dialog(root, hf, files=files)
    saved = dd.filedialog.askdirectory
    try:
        listed(root, dlg)
        dlg.choose_row(1)
        dlg.choose_row(3)
        dlg.choose_row(4)                                   # the split row: its warning in the message line
        pump(root, 0.05)
        before = (dlg.row_texts(), dlg.ticked_rows(), dlg.message_text(), hf.list_calls)
        dlg.set_view("folder")
        pump(root, 0.05)
        dlg.set_view("hf")
        pump(root, 0.05)
        eq((dlg.row_texts(), dlg.ticked_rows(), dlg.message_text(), hf.list_calls), before,
           "back to Hugging Face: the listing, the ticks and the message as they were, nothing fetched")
        dlg.set_view("folder")
        dlg.choose_folder_row(0)
        dlg.press_delete()
        dlg.press_delete_for_good()
        pump(root, 0.05)
        dlg.set_view("hf")
        pump(root, 0.05)
        eq(hf.list_calls, before[3] + 1, "a file was deleted meanwhile: the shown listing is listed again")
        eq(len(dlg.row_texts()), 5, "...and shown")
        dlg.set_view("folder")
        dlg.set_view("hf")
        pump(root, 0.05)
        eq(hf.list_calls, before[3] + 1, "nothing deleted this time: nothing fetched")
        # Escape and Close with the question up
        dlg.set_view("folder")
        dlg.choose_folder_row(0)
        dlg.press_delete()
        pump(root, 0.05)
        dlg.event_generate("<Escape>")
        pump(root, 0.1)
        check(alive(dlg) and dlg.question_text() == "" and dlg.ticked_rows() == [0],
              "Escape with the question up answers Keep (the window stays)")
        dlg.press_delete()
        dlg._close_btn.invoke()
        pump(root, 0.1)
        check(alive(dlg) and dlg.question_text() == "", "Close with the question up answers Keep first")
        eq(len(files.deletes), 1, "...and deletes nothing")
        # Change… in the folder view
        dd.filedialog.askdirectory = lambda **kw: "D:/Other/Models"      # no native picker in a test
        dlg._change_link._label.event_generate("<Button-1>")
        pump(root, 0.1)
        eq((dlg.flow.dest_dir, dlg.folder.dest_dir), (OTHER, OTHER), "Change… sets the one folder of both views")
        eq((dlg.folder_rows(), dlg.ticked_rows()), ([(LB[0], "", "9.1 GB")], []), "...and reads the new folder")
        eq(dlg.dest_line().split(" ")[:2], ["In", "Other\\Models"], "the folder line follows")
        files.folders[OTHER] = []
        dlg.set_view("hf")
        dlg.set_view("folder")
        pump(root, 0.05)
        eq((dlg.list_placeholder(), placeholder_look(dlg)), (folder_flow.HINT_EMPTY, (dd.T.TEXT_3, 11, "left")),
           "an empty folder: the placeholder, drawn as a hint")
        eq(dlg.button_states()["Delete…"], False, "...and Delete… off")
        dlg._close_btn.invoke()
        pump(root, 0.1)
        check(not alive(dlg), "Close with no question closes the window")
    finally:
        dd.filedialog.askdirectory = saved
        close_dialog(root, dlg)
    # the switch is off while downloading
    gate = threading.Event()
    hf = FakeHf()
    hf.script = [("p", GIB, 0.1), ("hold", gate), ("stop",)]
    files = FakeFiles()
    dlg, _done = open_dialog(root, hf, threaded=True, files=files)
    try:
        dlg.set_repo(REPO)
        dlg.list_files()
        eq(dlg.switch_states()["In this folder"], (False, False), "listing: the switch is off")
        check(pump_until(root, lambda: len(dlg.row_texts()) == 5), "listing arrives")
        dlg.choose_row(0)
        dlg._download_btn.invoke()
        check(pump_until(root, lambda: dlg.flow.done_bytes >= GIB), "the download runs")
        eq(dlg.switch_states(), {"Hugging Face": (True, False), "In this folder": (False, False)},
           "downloading: the switch is greyed")
        dlg._tabs["folder"]._label.event_generate("<Button-1>")
        pump(root, 0.05)
        eq((dlg.view(), files.lists), ("hf", []), "a click on it does nothing")
        dlg.cancel()
        gate.set()
        check(pump_until(root, lambda: dlg.flow.phase == "list"), "the download stopped")
        eq(dlg.switch_states()["In this folder"], (False, True), "the switch is back")
    finally:
        gate.set()
        close_dialog(root, dlg)


def test_no_after_leaks(root, errs: list[str]) -> None:
    pump(root, 0.3)
    before = pending_afters(root)
    for i in range(4):
        hf = FakeHf()
        hf.script = [("p", MODEL_A.size)]
        dlg, _done = open_dialog(root, hf, threaded=bool(i % 2))
        dlg.set_repo(REPO)
        dlg.list_files()
        pump_until(root, lambda d=dlg: len(d.row_texts()) == 5)
        dlg.choose_row(0)
        dlg._download_btn.invoke()
        pump_until(root, lambda d=dlg: d.flow.phase == "done")
        dlg.close()
        pump(root, 0.3)
    pump(root, 0.6)
    after = pending_afters(root)
    check(after <= before, f"no after-scripts left behind ({before} before, {after} after)")
    check(not errs, f"no Tcl background errors / callback exceptions: {errs[-3:]}")


# ---------------------------------------------------------------------------------------------------- runner
def run() -> int:
    root = ctk.CTk()
    root.geometry("340x420+200+150")
    root.attributes("-alpha", 0.0)
    errs = install_error_traps(root)
    pump(root, 0.4)
    try:
        for fn in (test_enter_and_bad_repo, test_list_and_select, test_resume_and_discard, test_progress_and_cancel,
                   test_done, test_errors, test_long_list_scrolls, test_change_folder, test_close_paths,
                   test_batch, test_folder_view, test_folder_switch_rules):
            gc.collect()
            print(f"-- {fn.__name__}", flush=True)
            try:
                fn(root)
            except Exception:  # noqa: BLE001
                FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
                print(f"FAIL: {FAILS[-1]}", flush=True)
        print("-- test_no_after_leaks", flush=True)
        errs.clear()
        test_no_after_leaks(root, errs)
    except Exception:  # noqa: BLE001
        FAILS.append(f"runner raised:\n{traceback.format_exc()}")
        print(f"FAIL: {FAILS[-1]}", flush=True)
    finally:
        if alive(root):
            root.destroy()
        shutil.rmtree(HOME, ignore_errors=True)
    gc.collect()
    print(f"\nRESULT: {PASSES} passed, {len(FAILS)} failed", flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
