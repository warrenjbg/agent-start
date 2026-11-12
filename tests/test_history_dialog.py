"""history_dialog: the Usage history window (formatters, view model, and the window itself against a fake history).

Run:  .venv\\Scripts\\python.exe -u -X faulthandler tests\\test_history_dialog.py
Exit code 0 = all checks passed. Screenshots land in tests\\ui_history_<state>.png (git-ignored).
Opens only a bare CTk root and this dialog (no app window); never contacts a server, never touches processes.
"""
from __future__ import annotations

import datetime as dt
import gc
import logging
import os
import sys
import tempfile
import time
import tkinter
import traceback
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
# Anything the dialog's imports might write goes to a throwaway folder - never the project's files or %APPDATA%.
# Set before config_store is imported (sweep_dialog imports it), as test_ui_smoke does.
HOME = Path(tempfile.mkdtemp(prefix="as_history_home_"))
os.environ["AGENT_START_HOME"] = str(HOME)

import customtkinter as ctk  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import config_store  # noqa: E402
import history_dialog as hd  # noqa: E402
from history_dialog import HistoryDialog  # noqa: E402
from usage_history import DayRow, ModelUsage, Summary, Usage  # noqa: E402

assert config_store.DATA_DIR == HOME, "tests must not write to the real data dir"
logging.getLogger("agent_start").addHandler(logging.NullHandler())   # the broken-store tests log on purpose

PASSES, FAILS = 0, []
TODAY = dt.date(2026, 11, 13)          # a Friday
D = dt.timedelta


def check(cond, msg: str) -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(msg)
        print(f"FAIL: {msg}", flush=True)


def eq(got, want, msg: str) -> None:
    check(got == want, f"{msg}: got {got!r}, want {want!r}")


# ---------------------------------------------------------------------------------------------------- fake store
def mu(model: str, requests: int, prompt: int, gen: int, tps: float | None, served_s: float) -> ModelUsage:
    dec_s = gen / tps if tps and gen else 0.0
    return ModelUsage(model=model, requests=requests, prompt_tokens=prompt, generated_tokens=gen,
                      decode_s=dec_s, decode_tokens=gen if dec_s else 0, served_s=served_s)


def _sum(rows) -> Usage:
    rows = list(rows)
    return Usage(requests=sum(r.requests for r in rows), prompt_tokens=sum(r.prompt_tokens for r in rows),
                 generated_tokens=sum(r.generated_tokens for r in rows), decode_s=sum(r.decode_s for r in rows),
                 decode_tokens=sum(r.decode_tokens for r in rows), served_s=sum(r.served_s for r in rows))


class FakeHistory:
    """days() / totals() like usage_history.UsageHistory, over a fixed {date: [ModelUsage]}; counts reads."""

    def __init__(self, data: dict[dt.date, list[ModelUsage]], today: dt.date = TODAY) -> None:
        self.data = data
        self.today = today
        self.reads = 0

    def days(self, limit=None) -> list[DayRow]:
        self.reads += 1
        keys = sorted(self.data, reverse=True)[:limit]
        return [DayRow(date=d, total=_sum(self.data[d]),
                       models=tuple(sorted(self.data[d], key=lambda m: (-m.requests, -m.served_s, m.model))))
                for d in keys]

    def totals(self, days=None) -> Summary:
        cutoff = self.today - D(days=days - 1) if days is not None else None
        keys = sorted(d for d in self.data if cutoff is None or d >= cutoff)
        rows = [m for d in keys for m in self.data[d]]
        return Summary(total=_sum(rows), models=(), first=keys[0] if keys else None,
                       last=keys[-1] if keys else None, active_days=len(keys))


class BrokenHistory:
    def days(self, limit=None):
        raise OSError("disk gone")

    def totals(self, days=None):
        raise OSError("disk gone")


def populated() -> FakeHistory:
    return FakeHistory({
        TODAY: [mu("swift-1.5-iq3_s", 171, 2_900_000, 151_000, 101.7, 4 * 3600 + 2 * 60),
                mu("swift-1.5-iq3_xxs", 43, 520_000, 35_000, 84.1, 3600 + 10 * 60)],
        TODAY - D(days=2): [mu("swift-1.5-iq3_s", 96, 1_180_000, 74_000, 102.3, 2 * 3600 + 40 * 60)],
        TODAY - D(days=3): [mu("swift-1.5-iq3_s", 0, 0, 0, None, 18 * 60)],          # served, no requests
        TODAY - D(days=20): [mu("a-model-with-a-really-quite-long-name-q4_k_m", 12, 9_400, 1_902, 55.0, 600)],
        TODAY - D(days=40): [mu("swift-1.5-iq3_s", 5, 4_000, 800, 90.0, 300)],       # outside 30 days
    })


# ---------------------------------------------------------------------------------------------------- Tk helpers
def pump(root, seconds: float = 0.1) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def alive(w) -> bool:
    try:
        return bool(w.winfo_exists())
    except tkinter.TclError:
        return False


def install_error_traps(root) -> list[str]:
    """Collect Tcl background errors ('invalid command name ...' from a stale after-script) and Python callback
    exceptions instead of letting them print."""
    errs: list[str] = []
    root.report_callback_exception = lambda et, ev, tb: errs.append("".join(traceback.format_exception(et, ev, tb)))
    root.tk.createcommand("hc_bgerror", lambda msg, *a: errs.append(f"bgerror: {msg}"))
    root.tk.eval("proc bgerror {msg} {hc_bgerror $msg}")
    return errs


@contextmanager
def captured_stderr(out: list[str]):
    """Redirect fd 2 (Python and C-level writes) to a temp file for the block; its text is appended to `out`."""
    sys.stderr.flush()
    tmp = tempfile.TemporaryFile()
    saved = os.dup(2)
    os.dup2(tmp.fileno(), 2)
    try:
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 2)
        os.close(saved)
        tmp.seek(0)
        out.append(tmp.read().decode("utf-8", "replace"))
        tmp.close()


def pending_afters(root) -> int:
    return len(str(root.tk.call("after", "info")).split())


def open_dialog(root, history, hidden: bool = True, **kw) -> tuple[HistoryDialog, list[int]]:
    closed: list[int] = []
    dlg = HistoryDialog(root, history, on_closed=lambda: closed.append(1), today=TODAY, **kw)
    if hidden:
        dlg.attributes("-alpha", 0.0)
    pump(root, 0.6)                                   # past ARM_S and the 200 ms modal grab
    return dlg, closed


def close_dialog(root, dlg) -> None:
    if alive(dlg):
        dlg.close()
    pump(root, 0.1)


def shot(win, name: str) -> Path:
    win.attributes("-alpha", 1.0)
    win.attributes("-topmost", True)
    win.lift()
    win.update_idletasks()
    win.update()
    time.sleep(0.35)
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    path = HERE / f"ui_history_{name}.png"
    img.save(path)
    print(f"screenshot {path.name} ({w}x{h})", flush=True)
    return path


def fits(dlg) -> tuple[bool, str]:
    scale = ctk.ScalingTracker.get_window_scaling(dlg)
    dlg.update_idletasks()
    rw, rh = dlg.winfo_reqwidth() / scale, dlg.winfo_reqheight() / scale
    ok = rw <= dlg.logical_width + 1 and rh <= dlg.logical_height + 1
    return ok, f"req {rw:.0f}x{rh:.0f} vs {dlg.logical_width}x{dlg.logical_height} (scale {scale})"


# ---------------------------------------------------------------------------------------------------- pure tests
def test_formatters() -> None:
    for n, want in ((0, "0"), (None, "0"), (7, "7"), (1902, "1,902"), (9999, "9,999"), (10_000, "10.0 K"),
                    (74_000, "74.0 K"), (99_960, "100 K"), (186_000, "186 K"), (999_499, "999 K"),
                    (999_999, "1.00 M"), (1_710_000, "1.71 M"), (3_420_000, "3.42 M"), (31_600_000, "31.6 M"),
                    (1_370_000_000, "1.37 B"), (float("nan"), "0")):
        eq(hd.fmt_count(n), want, f"fmt_count({n!r})")
    for v, want in ((None, "–"), (0, "–"), (98.43, "98.4"), (101.66, "101.7"), (1234.5, "1,234.5")):
        eq(hd.fmt_tps(v), want, f"fmt_tps({v!r})")
    for s, want in ((0, "–"), (None, "–"), (20, "< 1 m"), (18 * 60, "18 m"), (59 * 60 + 40, "1 h 00 m"),
                    (5 * 3600 + 12 * 60, "5 h 12 m"), (61 * 3600 + 5 * 60, "61 h 05 m")):
        eq(hd.fmt_served(s), want, f"fmt_served({s!r})")
    eq(hd.fmt_day(TODAY, TODAY), "Today", "fmt_day today")
    eq(hd.fmt_day(TODAY - D(days=1), TODAY), "Yesterday", "fmt_day yesterday")
    eq(hd.fmt_day(dt.date(2026, 11, 11), TODAY), "Wed 11 Nov", "fmt_day weekday form")
    eq(hd.fmt_date(dt.date(2026, 10, 14)), "14 Oct", "fmt_date")
    eq(hd.usage_cells(Usage(served_s=18 * 60)), ("0", "–", "–", "–", "18 m"), "0 requests: tokens are dashes")
    eq(hd.usage_cells(mu("m", 3, 0, 0, None, 0)), ("3", "0", "0", "–", "–"), "requests without tokens: zeros")
    eq(hd.card_values(Usage()), {"requests": "0", "generated tokens": "0", "avg tok/s": "–", "served": "–"},
       "empty cards")
    eq(hd.range_start(TODAY, 30, None), TODAY - D(days=29), "range_start 30 days")
    eq(hd.range_start(TODAY, None, TODAY - D(days=10)), TODAY - D(days=10), "range_start All")
    eq(hd.range_start(TODAY, None, TODAY - D(days=300)), TODAY - D(days=89), "range_start All capped at 90")
    eq(hd.range_start(TODAY, None, None), None, "range_start All without data")


def test_build_view() -> None:
    h = populated()
    v = hd.build_view(h, 30, TODAY)
    eq(len(v.bars), 30, "30-day view: 30 bars")
    eq(v.bars[-1], 186_000, "30-day view: today's bar")
    eq(v.bars[-3], 74_000, "30-day view: two days ago")
    eq(v.bars[-4], 0, "30-day view: served-only day is an empty slot")
    eq(v.axis, ("15 Oct", hd.AXIS_CAPTION, "Today"), "30-day axis")
    eq([r.label for r in v.rows if r.kind == "day"], ["Today", "Wed 11 Nov", "Tue 10 Nov", "Sat 24 Oct"],
       "30-day day rows newest first, the 40-day-old one left out")
    v7 = hd.build_view(h, 7, TODAY)
    eq(v7.axis[0], "7 Nov", "7-day axis starts 6 days back")
    va = hd.build_view(h, None, TODAY)
    eq(len(va.bars), 41, "All: first day with data .. today")
    eq(hd.build_view(BrokenHistory(), 30, TODAY).table_note, hd.EMPTY_TABLE, "a failing store reads as empty")
    v1 = hd.build_view(FakeHistory({TODAY: [mu("m", 1, 10, 10, 10.0, 60)]}), None, TODAY)
    eq((len(v1.bars), v1.axis[0]), (1, ""), "All with today only: one bar, no left date")


# ---------------------------------------------------------------------------------------------------- GUI tests
def test_populated(root) -> Path | None:
    h = populated()
    dlg, closed = open_dialog(root, h, hidden=False)
    path = None
    try:
        eq(dlg.range_days, 30, "default range")
        eq(dlg.range_buttons(), {"7 days": False, "30 days": True, "All": False}, "range switch state")
        eq(dlg.card_texts(), {"requests": "322", "generated tokens": "262 K", "avg tok/s": "98.5",
                              "served": "8 h 20 m"}, "30-day cards")
        rows = dlg.table_rows()
        eq(rows[:4], [("day", "Today", "214", "3.42 M", "186 K", "97.8", "5 h 12 m"),
                      ("model", "swift-1.5-iq3_s", "171", "2.90 M", "151 K", "101.7", "4 h 02 m"),
                      ("model", "swift-1.5-iq3_xxs", "43", "520 K", "35.0 K", "84.1", "1 h 10 m"),
                      ("day", "Wed 11 Nov", "96", "1.18 M", "74.0 K", "102.3", "2 h 40 m")], "first table rows")
        eq(rows[5], ("day", "Tue 10 Nov", "0", "–", "–", "–", "18 m"), "served-only day row")
        eq(rows[6], ("model", "swift-1.5-iq3_s", "0", "–", "–", "–", "18 m"), "served-only model row")
        eq(len(rows), 9, "4 day rows + 5 model rows in 30 days (every day shows its models)")
        bars = dlg.bar_values()
        eq(len(bars), 30, "bar count 30")
        eq((bars[-1], bars[-3], bars[-4], bars[-21], sum(1 for b in bars if b)), (186_000, 74_000, 0, 1_902, 3),
           "bar values on the right days")
        eq(dlg.axis_texts(), ("15 Oct", "generated tokens per day", "Today"), "axis line")
        eq(dlg.chart_message(), "", "no empty message with data")
        eq(dlg.table_note(), "", "no table note with rows")
        eq(dlg.table_view(), (0.0, 1.0), "8 rows fit without scrolling")
        ok, why = fits(dlg)
        check(ok, f"populated window content fits its fixed size: {why}")
        # hover a bar: the centre of the axis line names the day and its tokens
        chart = dlg._chart
        w = chart.winfo_width()
        chart.event_generate("<Enter>", x=w - 2, y=5)          # Tk delivers <Motion> only after an <Enter>
        chart.event_generate("<Motion>", x=w - 2, y=5)
        pump(root, 0.05)
        eq(dlg.axis_texts()[1], "Today · 186 K tokens", "hovering the last bar")
        chart.event_generate("<Motion>", x=int(w * 26.5 / 30), y=5)
        pump(root, 0.05)
        eq(dlg.axis_texts()[1], "Tue 10 Nov · no tokens", "hovering an empty slot")
        chart.event_generate("<Leave>")
        pump(root, 0.05)
        eq(dlg.axis_texts()[1], "generated tokens per day", "leaving the chart restores the caption")
        path = shot(dlg, "populated")

        reads = h.reads
        dlg.set_range(7)
        pump(root, 0.1)
        eq(h.reads, reads + 1, "one read per range change")
        eq(dlg.range_days, 7, "7-day range set")
        eq(dlg.range_buttons(), {"7 days": True, "30 days": False, "All": False}, "range switch follows")
        eq(len(dlg.bar_values()), 7, "7 bars")
        eq(dlg.card_texts()["requests"], "310", "7-day requests card")
        eq([r[1] for r in dlg.table_rows() if r[0] == "day"], ["Today", "Wed 11 Nov", "Tue 10 Nov"], "7-day days")
        dlg.set_range(7)
        eq(h.reads, reads + 1, "same range again: no re-read")

        dlg._range_btns[None].invoke()            # the 'All' button itself
        pump(root, 0.1)
        eq(dlg.range_days, None, "All via its button")
        eq(len(dlg.bar_values()), 41, "All: 41 bars (first day with data .. today)")
        eq(dlg.card_texts()["requests"], "327", "All: requests card counts everything")
        eq(dlg.table_rows()[-2:], [("day", "Sun 4 Oct", "5", "4,000", "800", "90.0", "5 m"),
                                   ("model", "swift-1.5-iq3_s", "5", "4,000", "800", "90.0", "5 m")],
           "All: the oldest day is in the table")
        eq(dlg.axis_texts()[0], "4 Oct", "All: axis starts at the first day")
        try:
            dlg.set_range(14)
            check(False, "set_range(14) should raise")
        except ValueError:
            check(True, "set_range rejects other ranges")
        close_dialog(root, dlg)
        pump(root, 0.05)
        eq(closed, [1], "on_closed once after close()")
    finally:
        if alive(dlg):
            dlg.destroy()
    return path


def test_empty(root) -> Path | None:
    dlg, closed = open_dialog(root, FakeHistory({}), hidden=False)
    path = None
    try:
        eq(dlg.card_texts(), {"requests": "0", "generated tokens": "0", "avg tok/s": "–", "served": "–"},
           "empty cards")
        eq(dlg.chart_message(), hd.EMPTY_CHART, "empty chart message")
        eq(dlg.table_rows(), [], "no table rows")
        eq(dlg.table_note(), hd.EMPTY_TABLE, "empty table line")
        eq(dlg.bar_values(), [0] * 30, "30 empty slots")
        texts = [dlg._chart.itemcget(i, "text") for i in dlg._chart.find_all() if dlg._chart.type(i) == "text"]
        eq(texts, [hd.EMPTY_CHART], "chart canvas draws the message")
        body = [dlg._tbody.itemcget(i, "text") for i in dlg._tbody.find_all() if dlg._tbody.type(i) == "text"]
        eq(body, [hd.EMPTY_TABLE], "table canvas draws the empty line")
        ok, why = fits(dlg)
        check(ok, f"empty window content fits: {why}")
        path = shot(dlg, "empty")
        dlg.set_range(None)
        pump(root, 0.05)
        eq((dlg.bar_values(), dlg.table_note()), ([], hd.EMPTY_TABLE), "All with no data: no bars, same line")
        dlg.set_range(7)
        eq(dlg.bar_values(), [0] * 7, "7 days with no data: 7 empty slots")
    finally:
        close_dialog(root, dlg)
    eq(closed, [1], "empty: on_closed once")
    return path


def test_range_without_data(root) -> None:
    h = FakeHistory({TODAY - D(days=50): [mu("m", 4, 100, 50, 20.0, 120)]})
    dlg, _closed = open_dialog(root, h)
    try:
        eq(dlg.chart_message(), "", "data exists: no 'no requests yet' message")
        eq(dlg.table_note(), "No requests in the last 30 days", "range without data says so")
        eq(dlg.card_texts()["requests"], "0", "range without data: 0 requests")
    finally:
        close_dialog(root, dlg)


def test_long_history_scrolls(root) -> Path | None:
    data = {TODAY - D(days=i): [mu("swift-1.5-iq3_s", 50 + i, 600_000 + i * 1000, 40_000 + i * 300, 100.0, 7200),
                                mu("swift-1.5-iq3_xxs", 10, 90_000, 6_000, 80.0, 1800)]
            for i in range(0, 200, 2)}
    dlg, _closed = open_dialog(root, FakeHistory(data), hidden=False)
    path = None
    try:
        geo = dlg.geometry().split("+")[0]
        h_before = dlg.winfo_height()
        dlg.set_range(None)
        pump(root, 0.15)
        eq(len(dlg.table_rows()), 300, "All: 100 days x (1 + 2 models)")
        eq(len(dlg.bar_values()), 90, "All capped at the newest 90 days")
        top, bottom = dlg.table_view()
        check(top == 0.0 and bottom < 0.2, f"long table starts at the top and scrolls: view {(top, bottom)}")
        dlg.scroll_table(1.0)
        pump(root, 0.05)
        check(dlg.table_view()[1] == 1.0 and dlg.table_view()[0] > 0.8, f"scrolled to the end: {dlg.table_view()}")
        dlg.scroll_table(0.5)
        pump(root, 0.05)
        path = shot(dlg, "all_scrolled")
        eq(dlg.winfo_height(), h_before, "window height unchanged by a long table")
        eq(dlg.geometry().split("+")[0], geo, "geometry unchanged")
        dlg.event_generate("<MouseWheel>", delta=120)
        pump(root, 0.05)
        check(dlg.table_view()[0] < 0.5, f"mouse wheel scrolls up: {dlg.table_view()}")
        dlg.set_range(7)
        pump(root, 0.1)
        eq(dlg.table_view()[0], 0.0, "a range change scrolls back to the top")
        ok, why = fits(dlg)
        check(ok, f"long-history window still fits: {why}")
    finally:
        close_dialog(root, dlg)
    return path


def test_close_paths(root) -> None:
    # Escape
    dlg, closed = open_dialog(root, populated())
    dlg.event_generate("<Escape>")
    pump(root, 0.1)
    check(not alive(dlg), "Escape closes")
    eq(closed, [1], "Escape: on_closed once")
    # Close button
    dlg, closed = open_dialog(root, populated())
    dlg._close_btn.invoke()
    pump(root, 0.1)
    check(not alive(dlg), "Close button closes")
    dlg.close()
    pump(root, 0.05)
    eq(closed, [1], "Close button + a late close(): on_closed still once")
    # X (WM_DELETE_WINDOW)
    dlg, closed = open_dialog(root, populated())
    dlg.tk.call(dlg.protocol("WM_DELETE_WINDOW"))     # what the title bar's X runs
    pump(root, 0.1)
    check(not alive(dlg), "the X (WM_DELETE_WINDOW) closes")
    eq(closed, [1], "X: on_closed once")
    # close()
    dlg, closed = open_dialog(root, populated())
    dlg.close()
    pump(root, 0.1)
    check(not alive(dlg), "close() closes")
    eq(closed, [1], "close(): on_closed once")
    # a close in the first moment waits for the arm time instead of crashing Tk or being dropped
    closed2: list[int] = []
    dlg = HistoryDialog(root, populated(), on_closed=lambda: closed2.append(1), today=TODAY)
    dlg.attributes("-alpha", 0.0)
    pump(root, 0.05)
    dlg.close()
    dlg.close()
    pump(root, 0.1)
    check(alive(dlg), "early close is deferred (still open at 0.15 s)")
    pump(root, 0.6)
    check(not alive(dlg), "early close happens once armed")
    eq(closed2, [1], "early close: on_closed once")
    # destroy() straight away by an owner (Settings closing) is allowed and reports once
    dlg, closed = open_dialog(root, populated())
    dlg.destroy()
    pump(root, 0.05)
    eq(closed, [1], "destroy(): on_closed once")
    # a store that raises: the window still opens, empty
    dlg, closed = open_dialog(root, BrokenHistory())
    eq(dlg.table_note(), hd.EMPTY_TABLE, "broken store: empty state")
    close_dialog(root, dlg)


def test_destroy_master(root) -> None:
    """The window's master (a stand-in for Settings / the app) is destroyed while the window is open."""
    master = ctk.CTkToplevel(root)
    master.attributes("-alpha", 0.0)
    master.geometry("340x300")
    pump(root, 0.5)
    closed: list[int] = []
    dlg = HistoryDialog(master, populated(), on_closed=lambda: closed.append(1), today=TODAY)
    dlg.attributes("-alpha", 0.0)
    pump(root, 0.6)
    raised = None
    try:
        master.destroy()
    except Exception as exc:  # noqa: BLE001
        raised = exc
    pump(root, 0.4)
    eq(raised, None, "destroying the master raises nothing")
    check(not alive(dlg), "the window went with its master")
    eq(closed, [1], "master destroyed: on_closed once")


def test_no_after_leaks(root, errs: list[str]) -> None:
    pump(root, 0.3)
    before = pending_afters(root)
    out: list[str] = []
    counts: list[int] = []
    with captured_stderr(out):
        for i in range(5):
            dlg, closed = open_dialog(root, populated())
            dlg.set_range(None if i % 2 else 7)
            dlg.close()
            pump(root, 0.3)
            counts.append(len(closed))
        pump(root, 0.6)                                # past every timer a leaked after-script could have
    after = pending_afters(root)
    eq(counts, [1] * 5, "5 open/close cycles: on_closed once each")
    check(after <= before, f"no after-scripts left behind ({before} before, {after} after)")
    check("invalid command name" not in out[0], f"no 'invalid command name' on stderr: {out[0][-400:]!r}")
    check(not errs, f"no Tcl background errors / callback exceptions: {errs[-3:]}")


def test_root_destroyed_last(root) -> None:
    """Last: the app's root itself goes while the window is open (app quit with History up)."""
    dlg, closed = open_dialog(root, populated())
    out: list[str] = []
    raised = None
    with captured_stderr(out):
        try:
            root.destroy()
        except Exception as exc:  # noqa: BLE001
            raised = exc
    eq(raised, None, "destroying the root raises nothing")
    eq(closed, [1], "root destroyed: on_closed once")
    check("invalid command name" not in out[0] and "Traceback" not in out[0], f"quiet root destroy: {out[0]!r}")


# ---------------------------------------------------------------------------------------------------- runner
def run() -> int:
    for fn in (test_formatters, test_build_view):
        print(f"-- {fn.__name__}", flush=True)
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
            print(f"FAIL: {FAILS[-1]}", flush=True)

    root = ctk.CTk()
    root.geometry("340x420+200+150")
    root.attributes("-alpha", 0.0)
    errs = install_error_traps(root)
    pump(root, 0.4)
    shots: list[Path] = []
    try:
        for fn in (test_populated, test_empty, test_range_without_data, test_long_history_scrolls,
                   test_close_paths, test_destroy_master):
            gc.collect()
            print(f"-- {fn.__name__}", flush=True)
            try:
                p = fn(root)
                if isinstance(p, Path):
                    shots.append(p)
            except Exception:  # noqa: BLE001
                FAILS.append(f"{fn.__name__} raised:\n{traceback.format_exc()}")
                print(f"FAIL: {FAILS[-1]}", flush=True)
        print("-- test_no_after_leaks", flush=True)
        errs.clear()
        test_no_after_leaks(root, errs)
        print("-- test_root_destroyed_last", flush=True)
        test_root_destroyed_last(root)
    except Exception:  # noqa: BLE001
        FAILS.append(f"runner raised:\n{traceback.format_exc()}")
        print(f"FAIL: {FAILS[-1]}", flush=True)
    finally:
        if alive(root):
            root.destroy()
    gc.collect()
    print(f"\nRESULT: {PASSES} passed, {len(FAILS)} failed", flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(run())
