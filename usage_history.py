"""Local usage history: per day and per model - finished requests, prompt / generated tokens, average generation
speed and time served - kept in one JSON file (<DATA_DIR>/usage.json). Nothing leaves this PC.

Fed by the app's metrics worker (`record()` per poll, `add_served()` periodically) and read by the History window
(`days()`, `totals()`). Engine-neutral: it only reads `engines.base.MetricsSample` / `RequestSample` fields.

File schema (version 1):

    {"version": 1,
     "days": {"YYYY-MM-DD": {"<model id>": {"requests": int, "prompt_tokens": int, "generated_tokens": int,
                                            "decode_s": float, "decode_tokens": int, "served_s": float}}},
     "recent": [["<request key>", <request time or null>], ...]}

The day is the LOCAL calendar day of the request's own time (the poll's wall clock when the engine gives none).
Average speed = decode_tokens / decode_s; a request adds to both only when it has a usable speed AND a token count,
so the average is token-weighted and requests without a speed do not distort it.

De-duplication: engines return their recent finished requests again on every poll (and all of them on a catch-up
poll), so each request is counted once by its identity (key, time). The newest identities are kept in memory and the
newest RECENT_PERSIST of them in the file, so a restart of the app while the same server keeps serving does not
count the re-read requests again. `begin_session()` (a new server run: keys may repeat) forgets them.

Threading: one state lock; record/add_served/flush/days/totals may be called from any thread. record/add_served only
mark the history dirty; the caller flushes from a worker thread. Never raises on a bad file or a failed write.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from engines.base import MetricsSample, RequestSample

log = logging.getLogger("agent_start")

VERSION = 1
FILE_NAME = "usage.json"
UNKNOWN_MODEL = "(unknown)"
DEFAULT_RETENTION_DAYS = 400
MAX_SEEN = 5000              # request identities kept in memory per serving session
RECENT_PERSIST = 1000        # ... of which the newest are saved (Strata keeps <= 500 requests: one catch-up fits)

INT_FIELDS = ("requests", "prompt_tokens", "generated_tokens", "decode_tokens")
FLOAT_FIELDS = ("decode_s", "served_s")
FIELDS = INT_FIELDS + FLOAT_FIELDS

Row = dict[str, Any]                 # one model's counters for one day (FIELDS)
Days = dict[str, dict[str, Row]]     # "YYYY-MM-DD" -> model id -> Row
Ident = tuple[str, float | None]     # a request's identity: (key, its own time)


# ------------------------------------------------------------------------------------------------ pure helpers
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _tokens(v: Any) -> int | None:
    """A token count: a finite number >= 0 as int, else None (unknown)."""
    f = _num(v)
    return int(f) if f is not None and f >= 0 else None


def day_key(t: float) -> str:
    """The LOCAL calendar day of epoch seconds `t` as 'YYYY-MM-DD'."""
    return time.strftime("%Y-%m-%d", time.localtime(t))


def model_key(model: Any) -> str:
    """The row key for a model id; None / empty -> '(unknown)'."""
    return model.strip() if isinstance(model, str) and model.strip() else UNKNOWN_MODEL


def empty_row() -> Row:
    return {"requests": 0, "prompt_tokens": 0, "generated_tokens": 0, "decode_s": 0.0, "decode_tokens": 0,
            "served_s": 0.0}


def request_ident(req: RequestSample) -> Ident | None:
    """(key, time) identifying one finished request across polls, or None when it has no usable key. The request's
    own time (not the poll's) is part of it, so a later server run that reuses a key is a different request."""
    key = getattr(req, "key", None)
    if not isinstance(key, str) or not key:
        return None
    return key, _num(getattr(req, "time", None))


def merge_request(days: Days, req: RequestSample, model: Any, fallback_time: float,
                  min_day: str | None = None) -> str | None:
    """Add one finished request to `days` (mutated). Returns the day key it went to (None: older than `min_day`,
    i.e. outside the retention window - not added).

    Day = local day of req.time (fallback_time when the request has none). Unknown token counts still count the
    request; speed counts only with decode_tps > 0 AND generated_tokens > 0 (token-weighted average)."""
    t = _num(getattr(req, "time", None))
    day = day_key(t if t is not None else fallback_time)
    if min_day is not None and day < min_day:
        return None
    row = days.setdefault(day, {}).setdefault(model_key(model), empty_row())
    row["requests"] += 1
    p = _tokens(getattr(req, "prompt_tokens", None))
    g = _tokens(getattr(req, "generated_tokens", None))
    if p:
        row["prompt_tokens"] += p
    if g:
        row["generated_tokens"] += g
    tps = _num(getattr(req, "decode_tps", None))
    if g and tps is not None and tps > 0:
        row["decode_tokens"] += g
        row["decode_s"] += g / tps
    return day


def add_seconds(days: Days, model: Any, seconds: float, day: str) -> None:
    """Add serving time to one day's row for `model` (mutated)."""
    days.setdefault(day, {}).setdefault(model_key(model), empty_row())["served_s"] += seconds


def retention_cutoff(today: _dt.date, retention_days: int) -> str:
    """The oldest day key kept: `retention_days` calendar days including today."""
    return (today - _dt.timedelta(days=max(1, int(retention_days)) - 1)).isoformat()


def prune(days: Days, today: _dt.date, retention_days: int) -> int:
    """Drop days older than `retention_days` calendar days (today counts as one). Returns how many were dropped."""
    cutoff = retention_cutoff(today, retention_days)
    old = [d for d in days if d < cutoff]     # ISO dates sort as text
    for d in old:
        del days[d]
    return len(old)


class BadFile(ValueError):
    """usage.json is not a valid version-1 history."""


def parse_doc(doc: Any) -> tuple[Days, list[Ident]]:
    """Validate a loaded usage.json document -> (days, recent). Raises BadFile on anything unexpected (any junk
    anywhere rejects the whole file: it is only ever written by this module, so junk means corruption).
    Missing counter fields read as 0; unknown extra keys are ignored."""
    if not isinstance(doc, dict):
        raise BadFile("not a JSON object")
    if doc.get("version") != VERSION or isinstance(doc.get("version"), bool):
        raise BadFile(f"unsupported version {doc.get('version')!r}")
    raw_days = doc.get("days", {})
    if not isinstance(raw_days, dict):
        raise BadFile("'days' is not an object")
    days: Days = {}
    for day, models in raw_days.items():
        try:
            if _dt.date.fromisoformat(day).isoformat() != day:
                raise ValueError(day)
        except (TypeError, ValueError):
            raise BadFile(f"bad day {day!r}") from None
        if not isinstance(models, dict):
            raise BadFile(f"day {day} is not an object")
        out: dict[str, Row] = {}
        for model, row in models.items():
            if not isinstance(model, str) or not model or not isinstance(row, dict):
                raise BadFile(f"bad model row {model!r} on {day}")
            clean = empty_row()
            for f in FIELDS:
                if f not in row:
                    continue
                v = _num(row[f])
                if v is None or v < 0 or (f in INT_FIELDS and v != int(v)):
                    raise BadFile(f"bad {f} for {model!r} on {day}: {row[f]!r}")
                clean[f] = int(v) if f in INT_FIELDS else v
            out[model] = clean
        days[day] = out
    raw_recent = doc.get("recent", [])
    if not isinstance(raw_recent, list):
        raise BadFile("'recent' is not a list")
    recent: list[Ident] = []
    for item in raw_recent:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and item[0]
                and (item[1] is None or _num(item[1]) is not None)):
            raise BadFile(f"bad recent entry {item!r}")
        recent.append((item[0], _num(item[1])))
    return days, recent


def build_doc(days: Days, recent: Iterable[Ident]) -> dict:
    """The document flush() writes (floats rounded to keep the file small)."""
    out: dict[str, dict[str, Row]] = {}
    for day in sorted(days):
        out[day] = {m: {f: (round(r[f], 3) if f in FLOAT_FIELDS else int(r[f])) for f in FIELDS}
                    for m, r in sorted(days[day].items())}
    return {"version": VERSION, "days": out, "recent": [[k, t] for k, t in recent]}


# ------------------------------------------------------------------------------------------------ read model
@dataclass(frozen=True)
class Usage:
    """Counters over some span. decode_tps = token-weighted average generation speed (None: no usable speed)."""
    requests: int = 0
    prompt_tokens: int = 0
    generated_tokens: int = 0
    decode_s: float = 0.0
    decode_tokens: int = 0
    served_s: float = 0.0

    @property
    def decode_tps(self) -> float | None:
        return self.decode_tokens / self.decode_s if self.decode_tokens > 0 and self.decode_s > 0 else None


@dataclass(frozen=True)
class ModelUsage(Usage):
    """One model's counters (same fields as Usage, plus the model id; '(unknown)' when it was not reported)."""
    model: str = ""


@dataclass(frozen=True)
class DayRow:
    """One local calendar day: totals over all models + one row per model (most requests first)."""
    date: _dt.date
    total: Usage
    models: tuple[ModelUsage, ...]


@dataclass(frozen=True)
class Summary:
    """Totals over a span of days. first/last: the oldest/newest day WITH data in it (None when empty);
    active_days: how many days had data."""
    total: Usage
    models: tuple[ModelUsage, ...]
    first: _dt.date | None
    last: _dt.date | None
    active_days: int


def _sum_rows(rows: Iterable[Row]) -> Row:
    acc = empty_row()
    for r in rows:
        for f in FIELDS:
            acc[f] += r[f]
    return acc


def _usage(r: Row) -> Usage:
    return Usage(**{f: r[f] for f in FIELDS})


def _model_rows(per_model: dict[str, Row]) -> tuple[ModelUsage, ...]:
    rows = [ModelUsage(model=m, **{f: r[f] for f in FIELDS}) for m, r in per_model.items()]
    rows.sort(key=lambda u: (-u.requests, -u.served_s, u.model))
    return tuple(rows)


# ------------------------------------------------------------------------------------------------ the store
def default_path() -> Path:
    import config_store            # lazy: tests pass their own path and never resolve the real data dir
    return Path(config_store.DATA_DIR) / FILE_NAME


class UsageHistory:
    def __init__(self, path: str | os.PathLike[str] | None = None, clock: Callable[[], float] = time.time,
                 retention_days: int = DEFAULT_RETENTION_DAYS, max_seen: int = MAX_SEEN,
                 recent_persist: int = RECENT_PERSIST) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._clock = clock
        self.retention_days = max(1, int(retention_days))
        self._max_seen = max(1, int(max_seen))
        self._recent_persist = max(0, min(int(recent_persist), self._max_seen))
        self._lock = threading.RLock()       # state: _days, _seen, _dirty
        self._io_lock = threading.Lock()     # one writer at a time (taken BEFORE _lock, never inside it)
        self._days: Days = {}
        self._seen: OrderedDict[Ident, None] = OrderedDict()
        self._dirty = False
        self._unread = False                 # the file exists but could not be read: never overwrite it blindly
        self._write_failing = False
        self._load()

    # ------------------------------------------------------------------ file
    def _today(self) -> _dt.date:
        return _dt.date.fromisoformat(day_key(self._clock()))

    def _read_file(self) -> tuple[Days, list[Ident]] | None:
        """(days, recent) from the file; None = no file. Raises BadFile (corrupt) or OSError (unreadable)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            doc = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BadFile(f"not valid JSON: {exc}") from None
        except RecursionError:
            raise BadFile("nested too deeply") from None
        return parse_doc(doc)

    def _set_aside(self, why: Exception) -> None:
        bad = self.path.with_name(self.path.name + ".bad")
        try:
            os.replace(self.path, bad)
            log.warning("usage history: %s is unusable (%s); moved to %s, starting empty", self.path, why, bad.name)
        except OSError as exc:
            log.warning("usage history: %s is unusable (%s) and could not be moved aside (%s); starting empty",
                        self.path, why, exc)

    def _load(self) -> None:
        try:
            got = self._read_file()
        except BadFile as exc:
            self._set_aside(exc)
            return
        except Exception as exc:  # noqa: BLE001 - locked by a scanner, permissions, ...: retried at flush
            self._unread = True
            log.warning("usage history: cannot read %s (%s); will retry before saving", self.path, exc)
            return
        if got is None:
            return
        days, recent = got
        self._days = days
        for ident in recent[-self._max_seen:]:
            self._seen[ident] = None
        if prune(self._days, self._today(), self.retention_days):
            self._dirty = True

    def _merge_loaded(self, days: Days, recent: list[Ident]) -> None:
        """A late successful read (the file was unreadable at start): add its history under ours."""
        for day, models in days.items():
            for model, row in models.items():
                mine = self._days.setdefault(day, {}).setdefault(model, empty_row())
                for f in FIELDS:
                    mine[f] += row[f]
        old = OrderedDict((i, None) for i in recent if i not in self._seen)
        old.update(self._seen)
        self._seen = old
        while len(self._seen) > self._max_seen:
            self._seen.popitem(last=False)

    def flush(self) -> bool:
        """Write the file if anything changed (atomic: temp file in the same folder + os.replace). Returns True if
        it wrote. Never raises: a failed write is logged and retried on the next flush."""
        with self._io_lock:
            try:
                return self._flush_locked()
            except Exception:  # noqa: BLE001 - belt and braces: a flush must never take down its caller
                log.warning("usage history: flush failed", exc_info=True)
                with self._lock:
                    self._dirty = True
                return False

    def _flush_locked(self) -> bool:
        with self._lock:
            if not self._dirty:
                return False
        if self._unread:                     # the existing file was unreadable at start: read + merge it first
            try:
                got = self._read_file()
            except BadFile as exc:
                self._set_aside(exc)
                got = None
            except OSError as exc:
                log.debug("usage history: %s still unreadable (%s); not saving yet", self.path, exc)
                return False
            with self._lock:
                if got is not None:
                    self._merge_loaded(*got)
                self._unread = False
        with self._lock:
            prune(self._days, self._today(), self.retention_days)
            recent = list(self._seen)[-self._recent_persist:] if self._recent_persist else []
            text = json.dumps(build_doc(self._days, recent), ensure_ascii=False, separators=(",", ":"))
            self._dirty = False
        try:
            self._write(text)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._dirty = True
            if not self._write_failing:
                log.warning("usage history: could not save %s (%s); will retry", self.path, exc)
            else:
                log.debug("usage history: save still failing: %s", exc)
            self._write_failing = True
            return False
        if self._write_failing:
            log.info("usage history: saved again")
        self._write_failing = False
        return True

    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".usage-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):         # a scanner / indexer holding the target open fails it briefly
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @property
    def dirty(self) -> bool:
        with self._lock:
            return self._dirty

    # ------------------------------------------------------------------ writes
    def begin_session(self, model: str | None = None) -> None:
        """A NEW server run starts (the app launched it): request keys may repeat, so forget the seen ones (also
        the saved list). Do NOT call it when adopting a server that may be the one already counted."""
        with self._lock:
            if self._seen:
                self._seen.clear()
                self._dirty = True
        log.debug("usage history: new session (%s)", model_key(model))

    def record(self, sample: MetricsSample | None, now: float | None = None) -> int:
        """Count the sample's finished requests not counted before. `now`: WALL-clock epoch seconds of the poll
        (time.time(), not monotonic) - the day of requests without a time of their own. Returns how many were new."""
        if sample is None:
            return 0
        reqs = getattr(sample, "requests", None) or ()
        if not reqs:
            return 0
        fallback = _num(now)
        if fallback is None:
            fallback = self._clock()
        model = getattr(sample, "model", None)
        min_day = retention_cutoff(self._today(), self.retention_days)
        added = 0
        with self._lock:
            for r in reqs:
                ident = request_ident(r)
                if ident is None:
                    continue
                if ident in self._seen:
                    self._seen.move_to_end(ident)      # still being reported: keep it among the newest
                    continue
                self._seen[ident] = None
                if merge_request(self._days, r, model, fallback, min_day) is not None:
                    added += 1
            while len(self._seen) > self._max_seen:
                self._seen.popitem(last=False)
            if added:
                self._dirty = True
        return added

    def add_served(self, model: str | None, seconds: float, now: float | None = None) -> None:
        """Add serving time to the day of `now` (default: the clock) for `model` (None -> '(unknown)'). Call it
        at short intervals (e.g. every minute and once at stop): each call goes to one day."""
        s = _num(seconds)
        if s is None or s <= 0:
            return
        t = _num(now)
        day = day_key(t if t is not None else self._clock())
        with self._lock:
            add_seconds(self._days, model, s, day)
            self._dirty = True

    # ------------------------------------------------------------------ reads
    def days(self, limit: int | None = None) -> list[DayRow]:
        """Days with data, newest first (at most `limit`)."""
        with self._lock:
            keys = sorted(self._days, reverse=True)
            if limit is not None:
                keys = keys[:max(0, int(limit))]
            snap = {d: {m: dict(r) for m, r in self._days[d].items()} for d in keys}
        return [DayRow(date=_dt.date.fromisoformat(d), total=_usage(_sum_rows(snap[d].values())),
                       models=_model_rows(snap[d])) for d in keys]

    def totals(self, days: int | None = None) -> Summary:
        """Totals over the last `days` calendar days ending today (None: everything kept)."""
        cutoff = None
        if days is not None:
            cutoff = (self._today() - _dt.timedelta(days=max(1, int(days)) - 1)).isoformat()
        with self._lock:
            keys = sorted(d for d in self._days if cutoff is None or d >= cutoff)
            per_model: dict[str, Row] = {}
            for d in keys:
                for m, r in self._days[d].items():
                    acc = per_model.setdefault(m, empty_row())
                    for f in FIELDS:
                        acc[f] += r[f]
        return Summary(total=_usage(_sum_rows(per_model.values())), models=_model_rows(per_model),
                       first=_dt.date.fromisoformat(keys[0]) if keys else None,
                       last=_dt.date.fromisoformat(keys[-1]) if keys else None, active_days=len(keys))
