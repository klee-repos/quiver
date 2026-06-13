"""The single analysis-side renderer for the strategy/goal target block.

Turns the active book + goal state into a READ-ONLY advisory block injected into
analyze.py's past_context (the PM + Trader prompts), so each ticker's daily
reasoning serves the greater 15% goal. It is the ONLY place the analysis path
learns about the book.

The decision wall (decision D2): this block is strictly QUALITATIVE — it carries
no numbers and no '$'. The only goal signal that crosses is the coarse
AHEAD/ON-TRACK/BEHIND regime label (derived from equity, but never the equity
value itself). It reads StrategyConfig + the strategy/goal ledger tables only;
it is FORBIDDEN from reading config dollar caps, the broker's buying-power or
position market values, or any sizing limit. A wall test asserts the rendered
text has no digit and no '$', and that this source references none of those
forbidden symbols (so even this prose avoids their literal spellings).
"""

from __future__ import annotations

from typing import Optional

# Target weight (a strategy-config %, not equity) above which a holding reads as
# a CORE position rather than a SATELLITE. Used only to pick a qualitative word;
# the number itself never reaches the rendered block.
_CORE_WEIGHT_THRESHOLD = 6.0

_DISCLAIMER = "advisory only — does NOT change sizing; Python owns all orders"


def build_target_context(led, ticker: str, strategy_cfg) -> Optional[dict]:
    """Assemble the read-only target context for ONE ticker, or None.

    None when the strategy layer is inactive (strategy_cfg is None) or the ticker
    is not in any book. Never raises — callers wrap it too, but it degrades to
    None internally so a hiccup can never shrink the proven scorecard context.
    """
    if strategy_cfg is None:
        return None
    try:
        import lib.strategy as _strategy
        import lib.goal as _goal
        thesis = _strategy.sleeve_thesis(strategy_cfg, ticker)
        if thesis is None:
            return None  # ticker not in the book -> inject no target block
        tk = ticker.upper()
        target_weight = None
        status = "active"
        regime = None
        goal_row = led.get_active_goal()
        if goal_row:
            rows = led.active_target_portfolio(
                goal_row["id"], statuses=("active", "exiting", "removed"))
            match = next((r for r in rows if r["ticker"] == tk), None)
            if match:
                target_weight = match.get("target_weight")
                status = str(match.get("status", "active"))
            prog = _goal.compute_from_ledger(led, goal_row)
            if prog:
                regime = prog.get("regime")
        if thesis["sleeve"] == _strategy.CASH_SLEEVE:
            tier = "CASH BALLAST"
        elif target_weight is not None and float(target_weight) >= _CORE_WEIGHT_THRESHOLD:
            tier = "CORE"
        else:
            tier = "SATELLITE"
        return {
            "ticker": tk,
            "sleeve": thesis["sleeve"],
            "tier": tier,
            "status": status,
            "goal_regime": regime,
            "correlation_note": thesis["correlation_note"],
        }
    except Exception:  # noqa: BLE001 — never break the analysis path
        return None


def render_target_block(target_context: Optional[dict], *, compact: bool = False) -> str:
    """Render the read-only target block (full or compact). '' when there is none.

    Strictly digit-free and '$'-free (the wall test asserts this): the only goal
    signal is the coarse regime word; the target weight is rendered as CORE /
    SATELLITE, never a number.
    """
    if not target_context:
        return ""
    tc = target_context
    being_exited = " (being exited)" if tc.get("status") == "exiting" else ""
    regime = tc.get("goal_regime") or "ON-TRACK"
    if compact:
        return (f"Portfolio ({_DISCLAIMER}): {tc['tier']}/{tc['sleeve']}{being_exited}; "
                f"goal {regime}; correlated AI/rate/liquidity bet.")
    return (
        f"Portfolio context ({_DISCLAIMER}):\n"
        f"  This ticker is a {tc['tier']} holding in the {tc['sleeve']} sleeve of the active book{being_exited}.\n"
        f"  Book progress vs the return goal: {regime}.\n"
        f"  Correlation note: {tc['correlation_note']}"
    )
