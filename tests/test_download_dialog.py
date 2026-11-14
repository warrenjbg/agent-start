"""download_dialog.DownloadDialog: the "Download a model" window against a fake hf object (no network, no disk).

Run:  .venv\\Scripts\\python.exe -u -X faulthandler tests\\test_download_dialog.py
Exit code 0 = all checks passed. Screenshots land in tests\\ui_download_<state>.png (git-ignored).
Opens only a bare CTk root and this dialog (no app window). It pops windows: ask before running it while the PC is
in use. Never contacts a server, never starts or stops a process, never writes a model file: the fake hf only
pretends. Clicks are invoke() / event_generate on the dialog's own widgets, never real mouse input.
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
from download_dialog import DownloadDialog  # noqa: E402
from hf_download import HfError, HfFile, RepoListing  # noqa: E402

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

    def parse_repo(self, text):
        m = re.fullmatch(r"(?:https?://)?(?:huggingface\.co/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/\S*)?",
                         text.strip())
        if not m:
            raise HfError("bad_repo")
        return f"{m.group(1)}/{m.group(2)}"

    def list_files(self, repo):
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


def open_dialog(root, hf: FakeHf, hidden: bool = True, threaded: bool = False) -> tuple[DownloadDialog, list[str]]:
    done: list[str] = []
    if threaded:      # the dialog's own fallback: daemon threads + its own answer queue (a bare root has no post())
        dlg = DownloadDialog(root, DEST, on_done=done.append, hf=hf)
    else:
        dlg = DownloadDialog(root, DEST, on_done=done.append, hf=hf, run_worker=sync_runner, post=direct_post)
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
    dlg = DownloadDialog(root, DEST, hf=hf, run_worker=sync_runner, post=direct_post)
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
    dlg = DownloadDialog(master, DEST, hf=hf)
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
                   test_batch):
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
