"""Pure book helpers: the tradable universe of a book and the book validator.

No I/O; plain dicts and lists, unit-tested offline. ``tradable_universe`` lists the
names a snapshot must quote (``snapshot-prepare``). ``validate_book`` refuses a book
that does not sum to 100 or whose band is not inside its weight.
"""

from __future__ import annotations

from typing import List, Tuple

_WEIGHT_SUM_TOLERANCE = 0.5

# The sleeve label marking the cash residual (kept in sync with lib.strategy.CASH_SLEEVE
# without importing it, so this module stays a dependency-free leaf).
_CASH_SLEEVE = "Cash"


def _upper_set(xs) -> set:
    return {str(x).upper() for x in (xs or [])}


def tradable_universe(rows, cash_sleeve_ticker: str = "SGOV") -> List[str]:
    """The engine tickers eligible for analysis from a book's holding rows.

    This is the day's analysis universe — what used to be the hand-maintained
    ``watchlist``, now DERIVED from the active portfolio book so the two can never
    drift apart. ``rows`` is an iterable of mappings (ledger ``target_portfolio``
    rows OR strategy.yaml holdings normalized to the same shape), each with
    ``ticker`` and optional ``sleeve`` / ``quotable`` / ``status``.

    Excludes, in line with how ``construct`` builds the executable book:
      * the cash sleeve (``sleeve == "Cash"`` or ``ticker == cash_sleeve_ticker``)
        — the residual ballast is held as buying power, never analyzed/traded;
      * non-quotable names (``quotable`` False) — the equities MCP can't price
        them (spot crypto), so a signal could never become an order;
      * anything not ``active`` — ``exiting``/``removed`` holdings are wound down
        deterministically by the rebalance pass, not by an analyze signal.

    Returns uppercased tickers, deduped, original order preserved. An empty result
    (no book, or a book of only cash/unquotable names) is the fail-safe signal to
    the caller that there is nothing to analyze — no universe, no trades.
    """
    cash = str(cash_sleeve_ticker or "SGOV").strip().upper()
    out: List[str] = []
    seen = set()
    for r in (rows or []):
        ticker = str(r.get("ticker", "") or "").strip().upper()
        if not ticker:
            continue
        sleeve = str(r.get("sleeve", "") or "")
        if ticker == cash or sleeve == _CASH_SLEEVE:
            continue
        if not bool(r.get("quotable", True)):
            continue
        if str(r.get("status", "active")) != "active":
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(ticker)
    return out


def validate_book(rows: List[dict], tolerance: float = _WEIGHT_SUM_TOLERANCE) -> Tuple[bool, str]:
    """A book is valid when its non-removed weights sum to 100 within ``tolerance`` and
    every engine holding's band is strictly inside its weight. A confirmed change passes
    0.01; the seed book of strategy.yaml uses the default 0.5."""
    total = sum(float(r.get("target_weight", 0) or 0)
                for r in rows if r.get("status") != "removed")
    if abs(total - 100.0) > tolerance:
        return (False, f"weights sum to {total:.2f}, expected ~100")
    # No negative weights anywhere (incl. cash) — an over-funded ADD that drove the cash
    # sleeve below zero must be rejected, never committed.
    for r in rows:
        if r.get("status") == "removed":
            continue
        if float(r.get("target_weight", 0) or 0) < -1e-9:
            return (False, f"{r.get('ticker')} has negative weight "
                           f"{float(r.get('target_weight', 0) or 0):.2f}")
    for r in rows:
        if r.get("status") == "removed" or r.get("sleeve") == "Cash":
            continue
        w = float(r.get("target_weight", 0) or 0)
        b = float(r.get("band", 0) or 0)
        if w > 0 and b >= w:
            return (False, f"{r.get('ticker')} band {b} >= weight {w}")
    return (True, "ok")
