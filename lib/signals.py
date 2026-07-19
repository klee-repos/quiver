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
    fallback: float = 100.0,
    position_pct: Optional[float] = None,
    room_under_target: Optional[float] = None,
    risk_cap: Optional[float] = None,
    exposure_scalar: Optional[float] = None,
) -> Tuple[float, str]:
    """Resolve a clamped USD buy amount. Returns (dollars, sizing_source).

    Sizing source preference: the model's STRUCTURED ``position_pct`` (% of equity)
    wins when present; otherwise the prose ``position_sizing`` is parsed; otherwise
    a conservative fallback. Final amount = min(that size, per-trade ceiling,
    remaining daily deploy cap, buying_power - buffer).

    ``room_under_target`` is the strategy layer's extra clamp (Stage 3): the room
    left before this name hits its target weight. It is folded into the SAME min()
    when supplied, so a target can only ever REDUCE a buy, never bypass a cap. When
    it is None (the default, and the classic non-rebalance path) the output is
    BYTE-IDENTICAL to before — the min() args are unchanged. Always >= 0; a
    non-positive result means "skip". Never fails open to a large size.

    PTJ-defense clamps (both default None -> BYTE-IDENTICAL, same convention as
    room_under_target): ``risk_cap`` is the stop-distance risk budget (F2/P3 —
    ``risk_budget_cap``), appended to the SAME min() so it can only REDUCE a buy;
    ``exposure_scalar`` (F3/F4/P5/P6 — downside-vol x trend x event severity, in
    [floor,1.0]) multiplies ``base`` BEFORE the min(). The scalar is applied with an
    explicit ``is not None`` guard (NOT ``or 1.0``) so a legitimate 0.0 block-scalar
    zeroes the buy instead of being silently treated as 1.0.
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
    # Downside-vol / trend / event exposure scalar (F3/F4/F8). Guarded is-not-None
    # multiply so a 0.0 block-scalar zeroes the buy; None -> untouched (byte-identical).
    if exposure_scalar is not None:
        base *= exposure_scalar

    # The classic clamp stack. room_under_target + risk_cap are appended ONLY when
    # supplied, so with both None the min() args are identical to before (byte-identical).
    clamps = [base, ceiling, remaining_daily_cap, buying_power - buffer]
    if room_under_target is not None:
        clamps.append(room_under_target)
    if risk_cap is not None:
        clamps.append(risk_cap)
    capped = min(clamps)
    if capped <= 0:
        return (0.0, source)
    return (round(capped, 2), source)


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
    bumped = min(held_qty, min_notional / quote)
    bumped = round(bumped, 6)
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


# --- memory-grounded strategy-consistency gate (Component A; cross-DAY) -------
# Unlike the intraday gates above (cooldown/action-cap/on-change), these span days
# and answer: "is today's buy<->sell REVERSAL part of a consistent recorded strategy,
# or is it random?" Pure + total + unit-tested; tick.py feeds them ledger facts (the
# prior completed-trade stance + its declared plan) and a Python-VERIFIED plan_trigger.
# NOT a calendar dwell: a reversal is fine the very next day IF it executes the
# recorded plan (a stop/target/review firing) or a genuine, budget-respecting basis
# change; it is suppressed only when it is ungrounded (silently contradicts the
# recorded stance) or serial churn. This module never imports lib.risk (the wall).

_OPENING = {"buy"}
_CLOSING = {"sell"}


def direction_of(intent: Optional[str]) -> str:
    """Map an order intent to a position DIRECTION: 'open' (buy/add), 'close'
    (sell/trim), or 'neutral' (hold/skip/unknown). The unit the reversal check works in."""
    i = (intent or "").strip().lower()
    if i in _OPENING:
        return "open"
    if i in _CLOSING:
        return "close"
    return "neutral"


def is_reversal(prior_intent: Optional[str], new_intent: Optional[str]) -> bool:
    """True iff the new intent REVERSES the prior committed direction (open<->close).

    A continuation (open->open / close->close) or anything touching 'neutral' (no
    prior stance, or a hold) is NOT a reversal — only a real direction flip is gated.
    """
    a, b = direction_of(prior_intent), direction_of(new_intent)
    if a == "neutral" or b == "neutral":
        return False
    return a != b


def reversal_rate(intents) -> Tuple[int, int]:
    """(reversals, transitions) over an ordered intent sequence (oldest->newest).

    Pure churn counter for the ADVISORY signal_stability metric (lib.risk reuses it).
    'neutral' intents (hold/skip) are dropped so only real stance changes count.
    """
    dirs = [d for d in (direction_of(i) for i in intents) if d in ("open", "close")]
    transitions = max(0, len(dirs) - 1)
    reversals = sum(1 for a, b in zip(dirs, dirs[1:]) if a != b)
    return (reversals, transitions)


def count_discretionary_reversals(records) -> int:
    """Recent DISCRETIONARY reversals — the 'changed my mind' flips that count against
    the consistency budget. ``records`` is oldest->newest dicts with 'intent' and an
    optional 'plan_trigger'. A reversal driven by a plan_trigger (a recorded stop/
    target/review firing) is the strategy EXECUTING, not churn, so it does NOT count;
    only reversals with no plan_trigger do.
    """
    seq = []
    for r in records:
        d = direction_of((r or {}).get("intent"))
        if d in ("open", "close"):
            seq.append((d, (r or {}).get("plan_trigger")))
    count = 0
    for (a, _), (b, trig_b) in zip(seq, seq[1:]):
        if a != b and not trig_b:
            count += 1
    return count


def strategy_consistency_verdict(
    *, prior_intent: Optional[str], new_intent: Optional[str],
    plan_trigger: Optional[str], basis_changed: bool,
    recent_discretionary_reversals: int, max_discretionary_reversals: int,
    enabled: bool = True,
) -> Tuple[bool, str]:
    """Decide whether a discretionary buy/sell may proceed given the recorded stance.

    Returns (allowed, reason). Pure + total. The order of grounding (see plan):
      * gate disabled -> always allow (byte-identical to the classic path).
      * not a reversal (continuation / neutral) -> allow.
      * a Python-VERIFIED plan_trigger (stop/target/loss/review) -> allow: the recorded
        strategy is executing (this is the "buy one day, sell the next, consistently"
        case the user wants permitted).
      * a genuine basis change, WITHIN the churn budget -> allow.
      * a basis change OVER the budget -> suppress ('basis_churn'): serial unexplained
        changes of mind are the operational definition of random.
      * neither a trigger nor a declared basis change -> suppress ('ungrounded_reversal').

    Fail-safe: the suppressing branches only ever fire on a REVERSAL, never on a
    continuation/open — so a gate error (caller passes conservative defaults) can only
    block a flip, never a normal buy/add.
    """
    if not enabled:
        return (True, "gate_disabled")
    if not is_reversal(prior_intent, new_intent):
        return (True, "continuation_or_neutral")
    if plan_trigger:
        return (True, f"executing_recorded_plan:{plan_trigger}")
    if basis_changed:
        if recent_discretionary_reversals >= max_discretionary_reversals:
            return (False, "basis_churn")
        return (True, "recorded_basis_change")
    return (False, "ungrounded_reversal")


# Absolute upper bound on any re-check delay, enforced in code regardless of
# config. The model may propose an absurd cadence (e.g. 168h = 7 days) and an
# operator may mis-set review_ceiling_min in config.yaml — this backstop
# guarantees the bot never goes longer than 48h without re-looking at a ticker.
HARD_REVIEW_CEILING_MIN = 48 * 60  # 2880 minutes (48 hours)


def clamp_review_minutes(next_review_hours, floor_min: float, ceiling_min: float) -> float:
    """Clamp a model-proposed re-check delay (hours) into [floor_min, ceiling_min] minutes.

    None / non-positive / unparseable -> the ceiling (re-check at the far, least
    aggressive end of the safe window). Python always owns this bound, never the model.
    The effective ceiling is additionally capped at HARD_REVIEW_CEILING_MIN (48h), a
    code-level backstop that no config value or model proposal can exceed.
    """
    ceiling_min = min(float(ceiling_min), float(HARD_REVIEW_CEILING_MIN))
    try:
        minutes = float(next_review_hours) * 60.0 if next_review_hours is not None else None
    except (TypeError, ValueError):
        minutes = None
    if minutes is None or minutes <= 0:
        return float(ceiling_min)
    return float(max(floor_min, min(minutes, ceiling_min)))


# --- Phase 5: limit-entry + protective-stop pricing (pure) ------------------
# Prices are decided HERE in Python. The model's entry_price/stop_loss are only
# seeds/hints; these functions own the actual numbers sent to the broker.

def marketable_limit_price(quote: Optional[float], slippage_pct: float) -> Optional[float]:
    """A buy limit set slightly ABOVE the live quote: fills fast, but caps slippage.

    None when the quote is missing/non-positive (caller then skips the limit order).
    """
    if not quote or quote <= 0:
        return None
    return round(quote * (1.0 + slippage_pct / 100.0), 2)


def whole_shares_for_dollars(dollars: float, limit_price: Optional[float]) -> int:
    """Whole shares affordable for ``dollars`` at ``limit_price`` (FLOORED).

    Limit orders can't be fractional, so a sub-one-share budget yields 0 (skip) —
    the key constraint that makes limit entries impractical for tiny accounts.
    """
    if not limit_price or limit_price <= 0 or dollars <= 0:
        return 0
    return int(dollars // limit_price)


def resolve_stop_price(fill_price, model_stop_loss, stop_pct) -> Optional[float]:
    """Protective stop price below ``fill_price``.

    The model's ``stop_loss`` only SEEDS the choice; Python clamps the distance to
    a safe band around the configured ``stop_pct`` (between 0.25x and 2x that
    distance below fill). Returns a price strictly below fill, or None if unusable.
    """
    if not fill_price or fill_price <= 0 or stop_pct <= 0:
        return None
    default_stop = fill_price * (1.0 - stop_pct / 100.0)
    far = fill_price * (1.0 - min(stop_pct * 2.0, 90.0) / 100.0)    # lowest allowed price
    near = fill_price * (1.0 - max(stop_pct * 0.25, 0.1) / 100.0)   # highest allowed price
    if model_stop_loss and 0 < model_stop_loss < fill_price:
        stop = min(near, max(far, model_stop_loss))
    else:
        stop = default_stop
    stop = round(stop, 2)
    return stop if 0 < stop < fill_price else None


# --- PTJ defense: asymmetric R:R gate, risk-based sizing, vol/trend scalars, de-risk ---
# Pure + total + unit-tested. Every one is a NO-OP at its "off" value (None / <=0 / 1.0 /
# empty tiers) so a book whose strategy.yaml predates these knobs is BYTE-IDENTICAL — the
# tick.py glue reads the knobs (defaulting off via allocate._DEFAULTS) and only passes a
# binding value when the operator opted in. These never read a limit/broker (the wall).

def reward_risk_ratio(entry: Optional[float], stop: Optional[float],
                      target: Optional[float]) -> Optional[float]:
    """The reward:risk of a long entry = (target - entry) / (entry - stop).

    PTJ's asymmetry test ("risk a little to make a lot"). Returns None when any leg is
    missing/implausible (entry<=0, stop not strictly below entry, target not strictly
    above entry) — i.e. UNCOMPUTABLE, which the caller treats as ungraded (a visible
    skip/pass decision, never a silent one). A valid ratio is > 0.
    """
    if not entry or entry <= 0 or not stop or not target:
        return None
    if not (0 < stop < entry) or target <= entry:
        return None
    return (target - entry) / (entry - stop)


def risk_budget_cap(equity: float, per_trade_risk_pct: Optional[float],
                    entry: Optional[float], stop: Optional[float]) -> Optional[float]:
    """Max buy dollars such that a stop-out loses <= per_trade_risk_pct% of equity (F2/P3).

    Size by CAPITAL-AT-RISK, not notional: with loss-per-dollar = (entry-stop)/entry,
    dollars = (per_trade_risk_pct/100 * equity) / ((entry-stop)/entry). A TIGHT stop
    (small distance) yields a LARGER cap (each dollar risks less); a WIDE stop a smaller cap.

    Returns None (the cap is OFF, never appended to the sizing min()) when
    per_trade_risk_pct <= 0 (B1: the no-op default), the stop is invalid, or equity <= 0 —
    so a disabled cap can never zero a buy. Always > 0 when returned.
    """
    if not per_trade_risk_pct or per_trade_risk_pct <= 0:
        return None
    if not equity or equity <= 0 or not entry or entry <= 0 or not stop or not (0 < stop < entry):
        return None
    loss_frac = (entry - stop) / entry
    if loss_frac <= 0:
        return None
    return (per_trade_risk_pct / 100.0 * equity) / loss_frac


def downside_vol_scalar(downside_vol: Optional[float], target: Optional[float],
                        *, floor: float = 0.25) -> float:
    """Exposure scalar in [floor, 1.0] that SHRINKS a buy as downside vol exceeds target (F5/P5).

    Fat-tail-aware sizing: 1.0 when downside vol is at/under the target, shrinking toward
    ``floor`` as it rises (min(1, target/vol)). B2: returns 1.0 (a TRUE no-op) when
    ``target`` is None or <= 0 (the off default) OR the vol is unknown/<=0 — BEFORE any
    division — so a disabled floor never shrinks a buy.
    """
    if target is None or target <= 0:
        return 1.0
    if downside_vol is None or downside_vol <= 0:
        return 1.0
    return max(floor, min(1.0, target / downside_vol))


def trend_gate_scalar(trend_regime: Optional[str], momentum: Optional[float],
                      mode: str = "off", *, soft_factor: float = 0.5) -> float:
    """Buy-side trend-alignment scalar ("don't fight the train", F6/P6). BUY-ONLY — the
    caller never applies it to a sell/de-risk (PTJ's asymmetry: cut freely, add with the trend).

    mode "off" (default) -> always 1.0 (byte-identical). "soft" -> ``soft_factor`` (0.5) when
    the deterministic regime is DOWNTREND or 12-1 momentum is negative, else 1.0. "block" ->
    0.0 in those cases (a full veto), else 1.0. Unknown regime + None momentum -> 1.0 (fail-open;
    a missing trend read never blocks a standing buy).
    """
    if mode not in ("soft", "block"):
        return 1.0
    regime = (trend_regime or "").strip().upper()
    against = regime == "DOWNTREND" or (momentum is not None and momentum < 0)
    if not against:
        return 1.0
    return 0.0 if mode == "block" else soft_factor


def derisk_trim_fraction(drop_pct: Optional[float], tiers) -> float:
    """Fraction of a held position to TRIM given the account drawdown vs today's baseline (F4/P4).

    ``drop_pct`` is signed (a drawdown is negative, e.g. -6.0). ``tiers`` is a list of
    ``{"at_drawdown_pct": <positive %>, "trim_pct": <0..100>}``; the DEEPEST breached tier's
    trim fraction is returned (a tier is breached when the drawdown magnitude >= its
    at_drawdown_pct). Empty/absent tiers or a non-drawdown (drop_pct >= 0) -> 0.0 (no de-risk,
    byte-identical). Robust to unordered tiers and missing/garbled keys. Result in [0.0, 1.0].
    """
    if not tiers or drop_pct is None or drop_pct >= 0:
        return 0.0
    mag = -float(drop_pct)  # drawdown magnitude, positive
    best = 0.0
    for t in tiers:
        if not isinstance(t, dict):
            continue
        at_raw = t.get("at_drawdown_pct")
        trim_raw = t.get("trim_pct")
        if at_raw is None or trim_raw is None:
            continue
        try:
            at = float(at_raw)
            trim = float(trim_raw)
        except (TypeError, ValueError):
            continue
        if at <= 0:
            continue
        if mag >= at:
            best = max(best, trim)
    return max(0.0, min(1.0, best / 100.0))
