"""Pure order-sizing clamps for the desk.

No I/O, no broker, no LLM. Every function here is deterministic and unit tested
(``tests/test_units.py``). ``lib/desk/proposals.build_trade`` sizes each leg with these
functions, and ``lib/desk/execution`` re-sizes a leg at the fresh quote. Python sizes
every order; a model never does.

Policy: LONG-ONLY. A sell never exceeds the held quantity, so it never opens a short.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple


def resolve_buy_dollars(gap_dollars, fraction, *, available, min_notional=0.0) -> Tuple[float, str]:
    """min(gap*fraction, available). The caller computes available by §10.2 step 3.
    Below max(min_notional, MIN_SELL_NOTIONAL_USD) -> (0.0, "below_min"). Never negative."""
    try:
        gap = float(gap_dollars or 0.0)
        frac = float(fraction or 0.0)
        avail = float(available or 0.0)
        floor = max(float(min_notional or 0.0), MIN_SELL_NOTIONAL_USD)
    except (TypeError, ValueError):
        return (0.0, "below_min")
    want = gap * frac
    if want <= 0 or avail <= 0:
        return (0.0, "below_min")
    if want <= avail:
        dollars, source = want, "gap"
    else:
        dollars, source = avail, "available"
    dollars = round(dollars, 2)
    if dollars > avail:  # rounding up must never spend more than the caller allows
        dollars = math.floor(avail * 100) / 100.0
    if dollars < floor:
        return (0.0, "below_min")
    return (dollars, source)


def resolve_sell_quantity(held_qty: float, sell_fraction: float) -> float:
    """Shares to sell. Full close -> all held; trim -> a fraction of held.

    Never exceeds the held quantity (long-only; cannot oversell into a short).
    """
    qty = max(0.0, min(held_qty, held_qty * sell_fraction))
    return round(qty, 6)


# Robinhood rejects any fractional order whose notional (qty * price) is under
# $1. A model trim (e.g. the Underweight 0.5-half) on a thin position can land
# below that floor and bounce. This is the sell-side mirror of the buy path's
# `rebalance_buy_below_min` / `whole_shares_for_dollars < 1` guards.
MIN_SELL_NOTIONAL_USD = 1.0


# Headroom above the broker floor for a bumped-up sell. The quantity is rounded
# UP to 6 decimal places, so the order clears the floor by construction; this
# factor also absorbs a quote move between the plan and the fill.
MIN_NOTIONAL_HEADROOM = 1.02


def resolve_sell_quantity_min_notional(
    held_qty: float, raw_qty: float, quote: Optional[float],
    *, min_notional: float = MIN_SELL_NOTIONAL_USD, bump_to_min: bool = True,
) -> Tuple[float, str]:
    """Reconcile a sell's share count against a ``min_notional`` floor, clamped to
    the held quantity.

    ``bump_to_min`` selects the behavior for a below-floor sub-order:
      * ``True`` (default, EXIT/wind-down semantics): bump the qty UP to the
        cheapest that clears ``min_notional`` — the position must be reduced, so
        we complete it at the smallest legal size. This is what a full exit /
        reconcile wants (finish the wind-down), and it is the pre-F2 behavior.
      * ``False`` (TRIM semantics, F2): a below-floor rebalance/Underweight trim
        is CHURN — skip it (``skip_below_min``) rather than force a tiny trade.

    Returns (qty, reason). ``reason`` is one of:
      * "trim"           -> the raw qty already clears the floor; unchanged.
      * "bumped_to_min"  -> (bump_to_min=True) bumped up to clear ``min_notional``.
      * "skip_below_min" -> (bump_to_min=False) a sub-floor TRIM, deliberately not
                            sent (F2 churn guard); the position stays slightly off
                            target rather than churning a sub-``min_notional`` order.
      * "skip_dust"      -> the WHOLE position is under the floor (physically
                            unsellable as a fractional); nothing to sell.
      * "skip_no_quote"  -> no usable quote to value the trim (fail to skip,
                            never synthesize a price to force a sell).
      * "skip_nothing"   -> nothing held or nothing to trim.

    IMPORTANT: the ``skip_dust`` branch (whole position under the floor) fires
    BEFORE the bump/skip choice, so a full exit passed a raised ``min_notional``
    would strand a $1-$5 position. Callers therefore keep exits on the $1 RH floor
    (``MIN_SELL_NOTIONAL_USD``) and apply the raised economic floor to TRIMS only.

    Never exceeds ``held_qty`` (long-only: cannot oversell into a short). A 0
    return means SKIP — the caller records a clean ``sell_below_min`` skip, NOT a
    broker-rejected error. Pure + total (unit-tested).
    """
    if held_qty <= 0 or raw_qty <= 0:
        return (0.0, "skip_nothing")
    if not quote or quote <= 0:
        return (0.0, "skip_no_quote")
    raw_qty = min(held_qty, raw_qty)
    if raw_qty * quote >= min_notional:
        return (round(raw_qty, 6), "trim")
    if held_qty * quote < min_notional:
        # The entire position is under the floor — there is no fractional qty
        # that clears the floor without overselling. Honest outcome: leave the dust.
        return (0.0, "skip_dust")
    if not bump_to_min:
        # TRIM semantics (F2): a sub-floor trim is churn — skip it, don't bump it up.
        return (0.0, "skip_below_min")
    # Round the quantity UP to the 6 decimal places the broker accepts, and add
    # headroom above the floor. `round()` here truncated the last place downward
    # and put the order UNDER the floor: 7 of the 12 live names produced a
    # sub-$1 order, and Robinhood rejected each one with "Fractional orders must
    # be at least $1". The headroom also absorbs a quote move between the plan
    # and the fill. Never exceed the held quantity (long-only).
    need = (min_notional * MIN_NOTIONAL_HEADROOM) / quote
    bumped = min(held_qty, math.ceil(need * 1_000_000) / 1_000_000)
    if bumped <= 0:
        return (0.0, "skip_dust")
    return (bumped, "bumped_to_min")


def room_under_target(target_dollars: float, current_mv: float,
                      upper_band_dollars: float = 0.0) -> float:
    """Dollars of headroom before a holding reaches its target weight (+ band).

    The strategy layer's extra buy clamp (Stage 3): max(0, target + band - current),
    so a name already at/above target has 0 room and a target buy is suppressed.
    Always >= 0 — never widens a buy.
    """
    return max(0.0, target_dollars + upper_band_dollars - current_mv)


def resolve_target_sell_quantity(
    held_qty: float, quote: Optional[float], current_mv: float, target_dollars: float,
    *, full_exit: bool = False, upper_band_dollars: float = 0.0,
) -> float:
    """Shares to TRIM down to a target weight (+ band edge), or to FULLY EXIT.

    full_exit -> sell all held (a removed/exiting name); the band is ignored.
    Otherwise trim only the EXCESS over the UPPER BAND EDGE (target + band):
    shares = (current_mv - target_dollars - upper_band_dollars) / quote, clamped to
    held_qty. With upper_band_dollars == 0 this is the classic trim-to-exact-target;
    with a band it trims only down to the near edge of the no-trade band, so a name
    settles AT the edge instead of overshooting to target and re-arming opposite-side
    churn next tick (the SOXX buy-then-trim pattern). Returns 0 when already within
    band, the quote is missing, or nothing is held — never oversells (long-only).
    """
    if held_qty <= 0:
        return 0.0
    if full_exit:
        return round(held_qty, 6)
    if not quote or quote <= 0:
        return 0.0
    excess = current_mv - target_dollars - max(0.0, upper_band_dollars)
    if excess <= 0:
        return 0.0
    shares = excess / quote
    return round(max(0.0, min(held_qty, shares)), 6)

