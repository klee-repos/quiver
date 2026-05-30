"""Pure signal -> order-intent mapping and position sizing clamps.

No I/O, no broker, no LLM — every function here is deterministic and unit
tested (see tests/test_units.py). The orchestrator calls these to turn a
TradingAgents signal + the model's prose sizing into concrete, clamped order
parameters.

Policy: LONG-ONLY. We only ever buy, add to, trim, or close a long position.
A Sell/Underweight with no existing position is a no-op (never opens a short).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

VALID_SIGNALS = {"Buy", "Overweight", "Hold", "Underweight", "Sell"}


def plan_action(signal: str, has_position: bool) -> Tuple[str, float]:
    """Map a signal (+ whether a long position exists) to (intent, fraction).

    intent: "buy" | "sell" | "hold" | "skip"
    fraction: for buy -> sizing multiplier (1.0 full, 0.5 tilt);
              for sell -> fraction of held shares to sell (1.0 close, 0.5 trim).
    """
    if signal == "Buy":
        return ("buy", 1.0)
    if signal == "Overweight":
        return ("buy", 0.5)
    if signal == "Hold":
        return ("hold", 0.0)
    if signal == "Sell":
        return ("sell", 1.0) if has_position else ("skip", 0.0)
    if signal == "Underweight":
        return ("sell", 0.5) if has_position else ("skip", 0.0)
    # ERROR / unknown / unparseable -> never trade
    return ("skip", 0.0)


def parse_sizing_to_dollars(position_sizing: Optional[str], baseline_equity: float) -> Optional[float]:
    """Best-effort parse of the model's prose sizing into a dollar amount.

    Handles "~5% of capital" (percent of equity) and "$500"/"500 dollars"
    (absolute). Returns None when nothing parseable is found, so the caller
    can fall back to a small conservative default (never fails open to a
    large size).
    """
    if not position_sizing:
        return None
    txt = str(position_sizing).replace(",", "")
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", txt)
    if pct:
        return float(pct.group(1)) / 100.0 * baseline_equity
    dollars = re.search(r"\$\s*(\d+(?:\.\d+)?)", txt)
    if dollars:
        return float(dollars.group(1))
    bare = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:dollars|usd)\b", txt, re.IGNORECASE)
    if bare:
        return float(bare.group(1))
    return None


def resolve_buy_dollars(
    position_sizing: Optional[str],
    baseline_equity: float,
    buy_fraction: float,
    *,
    ceiling: float,
    remaining_daily_cap: float,
    buying_power: float,
    buffer: float,
    room_under_ticker_cap: float,
    fallback: float = 100.0,
    position_pct: Optional[float] = None,
) -> Tuple[float, str]:
    """Resolve a clamped USD buy amount. Returns (dollars, sizing_source).

    Sizing source preference: the model's STRUCTURED ``position_pct`` (% of equity)
    wins when present; otherwise the prose ``position_sizing`` is parsed; otherwise
    a conservative fallback. Final amount = min(that size, per-trade ceiling,
    remaining daily deploy cap, buying_power - buffer, room under per-ticker cap).
    Always >= 0; a non-positive result means "skip". Never fails open to a large size.
    """
    if position_pct is not None and position_pct > 0:
        base = position_pct / 100.0 * baseline_equity
        source = "structured"
    else:
        base = parse_sizing_to_dollars(position_sizing, baseline_equity)
        source = "parsed"
    if base is None or base <= 0:
        base = min(ceiling, fallback)
        source = "fallback"
    base *= buy_fraction

    capped = min(
        base,
        ceiling,
        remaining_daily_cap,
        buying_power - buffer,
        room_under_ticker_cap,
    )
    if capped <= 0:
        return (0.0, source)
    return (round(capped, 2), source)


def resolve_sell_quantity(held_qty: float, sell_fraction: float) -> float:
    """Shares to sell. Full close -> all held; trim -> a fraction of held.

    Never exceeds the held quantity (long-only; cannot oversell into a short).
    """
    qty = max(0.0, min(held_qty, held_qty * sell_fraction))
    return round(qty, 6)


# --- multi-run gates + cadence clamp (pure; intraday mode only) --------------
# These gate REPEAT trades on the same ticker within a day and bound how soon we
# re-look. They are deterministic and unit-tested; tick.py feeds them ledger
# facts (last action, today's counts) and config thresholds.

def cooldown_ok(last_action_iso: Optional[str], now_iso: str, cooldown_min: float) -> bool:
    """True if enough time has elapsed since the last action (or there was none).

    Unparseable timestamps fail OPEN (return True) rather than wedging the bot on
    a bad clock value — the daily $ caps and action cap still bound exposure.
    """
    if not last_action_iso:
        return True
    try:
        elapsed_min = (datetime.fromisoformat(now_iso)
                       - datetime.fromisoformat(last_action_iso)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return True
    return elapsed_min >= cooldown_min


def within_action_cap(actions_today: int, max_actions: int) -> bool:
    """True if another action on this ticker is allowed today."""
    return actions_today < max_actions


def is_material_change(cur_signal, cur_intent, last_signal, last_intent) -> bool:
    """True if this decision differs from the last one (so it's worth acting again).

    No prior decision -> material. Otherwise a repeat of the same (signal, intent)
    is NOT material — the on-change gate suppresses churn from identical calls.
    """
    if last_signal is None and last_intent is None:
        return True
    return (cur_signal, cur_intent) != (last_signal, last_intent)


def clamp_review_minutes(next_review_hours, floor_min: float, ceiling_min: float) -> float:
    """Clamp a model-proposed re-check delay (hours) into [floor_min, ceiling_min] minutes.

    None / non-positive / unparseable -> the ceiling (re-check at the far, least
    aggressive end of the safe window). Python always owns this bound, never the model.
    """
    try:
        minutes = float(next_review_hours) * 60.0 if next_review_hours is not None else None
    except (TypeError, ValueError):
        minutes = None
    if minutes is None or minutes <= 0:
        return float(ceiling_min)
    return float(max(floor_min, min(minutes, ceiling_min)))
