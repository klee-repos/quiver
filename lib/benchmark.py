"""Benchmark window returns for the decision-memory scorecard (PURE).

WHAT THIS IS FOR. ``outcomes.benchmark_return`` is the market leg that turns a raw
directional return into ``alpha`` -- the excess that ``lib.memory.score_return``
prefers, and therefore the number the conviction calibration actually learns from.
It used to be supplied by the LLM orchestrator, which is why it was NULL on most
rows and wrong on the rest. This module is the deterministic replacement: given a
benchmark's daily closes and the trading calendar, it returns the fractional move
over one decision's own holding window, or refuses with a reason.

THE RULE THAT MATTERS. The defect this replaces was a silently-substituted STALE
close: a series that stopped at 2026-07-02 was used to price a window ending
2026-07-06, understating the benchmark by 2.2x. So the anchor rule admits no
walk-back over a missing observation:

    The anchor for a target date is the LAST TRADING SESSION on or before it, and
    that session MUST be present in the series. Never any other date.

A lag tolerance cannot express this. The legitimate 2026-07-02 -> 2026-07-06 gap is
four calendar days (July 4th observed on Friday the 3rd), the same width as the
defect, so length alone cannot separate "the market was closed" from "the series was
truncated" -- only the calendar can. This also rejects an interior hole: a series of
{07-02, 07-09} asked for 07-06 refuses, because 07-06 was a real session and is
absent.

PURITY / THE DECISION WALL. Stdlib only, no network, no configuration, no ledger,
no clock. The session calendar is INJECTED by the caller (``tick.py`` glue reads it
from pandas_market_calendars) rather than imported here, which keeps this module
offline-testable and free of the calendar dependency and its clock seam. Returns are
FRACTIONS (0.01 == 1%), matching the rest of the repo.

Every entry point returns ``(value_or_None, reason)``. A refusal is never an
exception and never a fallback guess: the caller writes NULL, and NULL simply means
the scorecard grades that row on its absolute return, exactly as it did before.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple


def _price(raw) -> Optional[float]:
    """A usable close, or None. Rejects non-numerics, NaN, inf and non-positives."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")) or v <= 0:
        return None
    return v


def _day(raw) -> Optional[str]:
    """Normalize to a YYYY-MM-DD key, tolerating a full ISO timestamp."""
    if not raw:
        return None
    s = str(raw).strip()[:10]
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return None
    return s


def session_anchor(series: Dict[str, float], target_date: str,
                   sessions: Iterable[str]) -> Tuple[Optional[str], str]:
    """The date whose close prices ``target_date``, or (None, reason).

    The last session on or before ``target_date`` -- which must be present in
    ``series``. There is deliberately NO search past it: a missing session means the
    series is incomplete for this window, not that an earlier close may stand in.
    """
    day = _day(target_date)
    if day is None:
        return None, f"unusable target date {target_date!r}"
    if not series:
        return None, "empty benchmark series"

    known = sorted({s for s in (_day(x) for x in sessions) if s is not None})
    if not known:
        return None, "no session calendar supplied"
    # COVERAGE FIRST. If the calendar stops before the target, we cannot know whether
    # the target was a session, so "the last session on or before it" is unanswerable.
    # Without this the rule silently degrades into the walk-back it exists to forbid:
    # a calendar bounded by the series' own last observation makes the anchor collapse
    # onto that observation, which is trivially present, so the refusal can never fire
    # at the END of a series -- exactly where the original defect lived.
    if day > known[-1]:
        return None, (f"session calendar ends {known[-1]}, before target {day}; "
                      "cannot tell a market closure from a truncated series")
    prior = [s for s in known if s <= day]
    if not prior:
        return None, f"no trading session on or before {day}"
    anchor = max(prior)

    if anchor not in series:
        return None, (f"session {anchor} missing from the benchmark series "
                      f"(incomplete data for {day}; refusing to substitute an earlier close)")
    if _price(series[anchor]) is None:
        return None, f"unusable close for session {anchor}"
    return anchor, ""


def window_return(series: Dict[str, float], start_date: str, end_date: str,
                  sessions: Iterable[str]) -> Tuple[Optional[float], str]:
    """Fractional benchmark move across one decision's holding window.

    ``start_date`` is the decision's trade date and ``end_date`` the date its outcome
    was resolved; each is priced by ``session_anchor``. Close-to-close over the same
    span the position leg covers.
    """
    sessions = list(sessions)
    a, why = session_anchor(series, start_date, sessions)
    if a is None:
        return None, f"start: {why}"
    b, why = session_anchor(series, end_date, sessions)
    if b is None:
        return None, f"end: {why}"
    if b < a:
        return None, f"end session {b} precedes start session {a}"

    p0, p1 = _price(series[a]), _price(series[b])
    if p0 is None or p1 is None:
        return None, "unusable close on an anchor session"
    return (p1 - p0) / p0, ""
