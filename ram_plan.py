"""Which apps to pre-tick so Strata gets its fast full-RAM mode.

Strata needs its whole expert arena in RAM (46.84 GiB for swift-iq3_s, 39.97 GiB for swift-iq3_xxs; learned from the
engine log by engine_log.learned_arena_gib). With less available it waits 20 s and falls back to reading experts from
the SSD (~20 tok/s instead of ~90-100). Verified: 52.0 GiB available at launch -> full RAM; 35.3 GiB -> fallback.

Target: available_at_launch >= arena + headroom (default 3 GiB: Strata's 2.5 GiB floor + pinned buffers).
Freed RAM per app is estimated from its working set (detector ram_mb), so every number shown is "~".

Rules (plan):
  1. Never tick a protected app (user whitelist / SYSTEM_PROTECTED; the detector already drops them, this is a
     second guard by exe name).
  2. Keep the detector's baseline pre-ticks (games).
  3. Still short and closing everything WOULD reach the target: add apps largest-first (fewest closures); apps of
     similar size (same 512 MB step) prefer game > gpu > background > window (a visible window is the most likely
     to hold unsaved work), then pid for determinism. Stop when the target is met, then drop any added app the
     target no longer needs (smallest first).
  4. Unreachable even with every app closed: add nothing beyond the baseline (closing them buys no speed) and say so.
Stdlib only, no Tk, no psutil.
"""
from __future__ import annotations

from dataclasses import dataclass

GIB = 1024.0
DEFAULT_HEADROOM_GIB = 3.0
_CATEGORY_RANK = {"game": 0, "gpu": 1, "background": 2, "window": 3}
_BUCKET_MB = 512.0


def format_gb(mb: float | None) -> str:
    """'6.8 GB' (one decimal), matching the app's _gb."""
    return f"{(mb or 0.0) / 1024.0:.1f} GB"


def _whole_gb(mb: float) -> str:
    return f"{max(0.0, mb) / 1024.0:.0f} GB"


@dataclass(frozen=True)
class RamPlan:
    need_mb: float = 0.0
    available_mb: float = 0.0
    short_mb: float = 0.0
    preticked_pids: tuple[int, ...] = ()
    freed_mb: float = 0.0          # estimate if the pre-ticked apps are closed
    reachable: bool = True         # closing every listed app would reach the target
    enough_now: bool = True
    summary: str = ""
    detail: str = ""

    def enough_with(self, freed_mb: float) -> bool:
        """Would closing apps worth freed_mb reach the target? (for live re-checks as the user ticks)."""
        return self.need_mb <= 0 or self.available_mb + freed_mb >= self.need_mb


def _ram(c: dict) -> float:
    try:
        return max(0.0, float(c.get("ram_mb") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _protected(c: dict, protected: set[str]) -> bool:
    return str(c.get("name") or "").lower() in protected


def plan(candidates: list[dict], available_mb: float, arena_gib: float | None,
         headroom_gib: float = DEFAULT_HEADROOM_GIB, baseline_preticked: set[int] | None = None,
         protected_names: set[str] | None = None) -> RamPlan:
    protected = {n.lower() for n in (protected_names or set())}
    pool = [c for c in candidates if not _protected(c, protected)]
    if baseline_preticked is None:
        baseline_preticked = {int(c["pid"]) for c in pool if c.get("preselect")}
    base = [c for c in pool if int(c["pid"]) in baseline_preticked]
    base_ids = {int(c["pid"]) for c in base}
    base_freed = sum(_ram(c) for c in base)

    def result(ids: list[int], freed: float, **kw) -> RamPlan:
        return RamPlan(preticked_pids=tuple(sorted(ids)), freed_mb=round(freed, 1), **kw)

    if not arena_gib:                       # requirement unknown: behave exactly like v1.0
        return result(list(base_ids), base_freed, available_mb=available_mb)

    need = (arena_gib + headroom_gib) * GIB
    short = max(0.0, need - available_mb)
    common = dict(need_mb=round(need, 1), available_mb=round(available_mb, 1), short_mb=round(short, 1))
    head = f"Full speed needs ~{_whole_gb(need)} free · you have {_whole_gb(available_mb)}"
    if short <= 0:
        return result(list(base_ids), base_freed, reachable=True, enough_now=True,
                      summary="Enough RAM for full speed", **common)

    total = sum(_ram(c) for c in pool)
    if available_mb + total < need:
        gap = need - available_mb - total
        return result(list(base_ids), base_freed, reachable=False, enough_now=False, summary=head,
                      detail=f"Closing every app here still leaves ~{format_gb(gap)} short", **common)

    rest = sorted((c for c in pool if int(c["pid"]) not in base_ids),
                  key=lambda c: (-(_ram(c) // _BUCKET_MB), _CATEGORY_RANK.get(c.get("category"), 9),
                                 -_ram(c), int(c["pid"])))
    added: list[dict] = []
    freed = base_freed
    for c in rest:
        if available_mb + freed >= need:
            break
        added.append(c)
        freed += _ram(c)
    for c in sorted(added, key=lambda c: (_ram(c), int(c["pid"]))):    # drop what the target no longer needs
        if available_mb + freed - _ram(c) >= need:
            added.remove(c)
            freed -= _ram(c)
    ids = sorted(base_ids | {int(c["pid"]) for c in added})
    n = len(ids)
    detail = f"Pre-ticked {n} app{'s' if n != 1 else ''} · frees ~{format_gb(freed)}"
    return result(ids, freed, reachable=True, enough_now=False, summary=head, detail=detail, **common)
