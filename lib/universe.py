"""Pure add/remove-asset transition logic + the RH allow-list / quotable gate.

No I/O — operates on plain dicts/lists so it is fully unit-tested offline. Enforces
the plan's safety rules: an ADD must be allow-listed AND equities-MCP quotable; a
REMOVE transitions a holding active -> exiting (never deleted while a position may
exist) so the rebalancer winds it to zero before it is gone; freed weight is
conserved into the cash sleeve and the book re-validated so a malformed book is
never committed.
"""

from __future__ import annotations

from typing import List, Tuple

_WEIGHT_SUM_TOLERANCE = 0.5


def _upper_set(xs) -> set:
    return {str(x).upper() for x in (xs or [])}


def validate_add(ticker: str, rh_tradable_confirmed, quotable_set) -> Tuple[bool, str]:
    """An ADD must be on the RH allow-list AND equities-MCP quotable.

    Spot crypto (SOL, etc.) is quotable=False -> blocked from auto-apply; it would
    need a manual route once a crypto MCP exposes quotes."""
    t = str(ticker).upper()
    if t not in _upper_set(rh_tradable_confirmed):
        return (False, f"{t} is not in rh_tradable_confirmed")
    if t not in _upper_set(quotable_set):
        return (False, f"{t} is not equities-MCP quotable (spot crypto blocked from auto-apply)")
    return (True, "ok")


def apply_remove(rows: List[dict], ticker: str) -> Tuple[List[dict], float]:
    """Transition a holding to 'exiting' with weight 0 (wound to zero by the
    rebalancer, never hard-deleted). Returns (new_rows, freed_weight)."""
    t = str(ticker).upper()
    out: List[dict] = []
    freed = 0.0
    for r in rows:
        if str(r.get("ticker")).upper() == t and r.get("status") != "removed":
            freed += float(r.get("target_weight", 0) or 0)
            nr = dict(r)
            nr["status"] = "exiting"
            nr["target_weight"] = 0.0
            out.append(nr)
        else:
            out.append(dict(r))
    return (out, freed)


def redistribute_to_cash(rows: List[dict], freed_weight: float, cash_ticker: str = "SGOV") -> List[dict]:
    """Conserve freed weight into the cash sleeve so the book keeps summing to ~100."""
    ct = str(cash_ticker).upper()
    out: List[dict] = []
    for r in rows:
        nr = dict(r)
        if str(nr.get("ticker")).upper() == ct:
            nr["target_weight"] = float(nr.get("target_weight", 0) or 0) + freed_weight
        out.append(nr)
    return out


def validate_book(rows: List[dict]) -> Tuple[bool, str]:
    """A book is valid when its non-removed weights sum to ~100 and every engine
    holding's band is strictly inside its weight. Refuses a malformed book."""
    total = sum(float(r.get("target_weight", 0) or 0)
                for r in rows if r.get("status") != "removed")
    if abs(total - 100.0) > _WEIGHT_SUM_TOLERANCE:
        return (False, f"weights sum to {total:.2f}, expected ~100")
    for r in rows:
        if r.get("status") == "removed" or r.get("sleeve") == "Cash":
            continue
        w = float(r.get("target_weight", 0) or 0)
        b = float(r.get("band", 0) or 0)
        if w > 0 and b >= w:
            return (False, f"{r.get('ticker')} band {b} >= weight {w}")
    return (True, "ok")
