"""Pure portfolio-construction math: target weights -> per-ticker intents.

No I/O, no broker, no LLM. Turns target weights + current positions + equity
into per-ticker target dollars, drift, dead-band no-trade decisions, and
rebalance intents (buy / trim / exit / hold), LONG-ONLY by construction.

Two deliberate behaviors, both from the plan's decisions:
  * The cash sleeve (SGOV) is the RESIDUAL — it is held as uninvested buying
    power, never bought/trimmed like an engine name (decision D3).
  * A non-quotable ticker (spot crypto, no equities-MCP quote) is flagged and
    SKIPPED from the executable book; its weight stays in cash (decision D7).

This module is on the SIZING side of the decision wall: like lib.signals it must
never import lib.risk / lib.reflect_memory / the broker. The actual share
conversion for a trim happens once, in lib.signals.resolve_target_sell_quantity,
at plan time — construct here works in DOLLARS so there is no duplicated math.
"""

from __future__ import annotations

from typing import Dict, List


def target_dollars_for_weight(weight_pct: float, equity: float) -> float:
    """Dollar value a target weight implies at the given account equity."""
    if equity <= 0:
        return 0.0
    return weight_pct / 100.0 * equity


def weight_drift(current_mv: float, equity: float, target_weight_pct: float) -> float:
    """Signed drift in WEIGHT POINTS: (current weight %) - (target weight %).

    Negative => underweight (room to buy); positive => overweight (room to trim).
    Returns 0.0 when equity is unknown (<=0) so a degenerate snapshot never trades.
    """
    if equity <= 0:
        return 0.0
    current_weight = current_mv / equity * 100.0
    return current_weight - target_weight_pct


def needs_rebalance(drift_pct: float, band_pct: float) -> bool:
    """True only when |drift| exceeds the no-trade dead-band (strictly outside)."""
    return abs(drift_pct) > band_pct


def rebalance_intent(
    current_mv: float, equity: float, target_weight_pct: float, band_pct: float,
    status: str = "active",
) -> tuple:
    """(intent, target_dollars, delta_dollars) for one holding, LONG-ONLY.

    intent: "buy" | "trim" | "exit" | "hold". delta_dollars is signed: positive
    means buy that many more dollars, negative means trim/exit that many.

      * status exiting/removed -> always EXIT (wind the position to zero), even
        if it is within band, so a dropped name is fully unwound.
      * within the dead-band -> HOLD (no churn).
      * underweight beyond band -> BUY toward target.
      * overweight beyond band -> TRIM toward target.
    """
    if status in ("exiting", "removed"):
        return ("exit", 0.0, -current_mv)
    target_dollars = target_dollars_for_weight(target_weight_pct, equity)
    if equity <= 0:
        return ("hold", target_dollars, 0.0)
    drift = weight_drift(current_mv, equity, target_weight_pct)
    if not needs_rebalance(drift, band_pct):
        return ("hold", target_dollars, 0.0)
    delta = target_dollars - current_mv
    return ("buy" if drift < 0 else "trim", target_dollars, delta)


def construct_target_book(
    targets: List[dict], positions: Dict[str, float], equity: float, *,
    cash_sleeve_ticker: str = "SGOV",
) -> List[dict]:
    """The deterministic 'what the book should look like today'.

    targets: rows from the ledger target_portfolio (ticker, sleeve, target_weight,
    band, status, quotable). positions: {ticker -> market_value}. Returns one row
    per target with its intent + target/delta dollars + flags. The cash sleeve is
    the residual; non-quotable tickers are flagged and skipped (held in cash).
    """
    cash = (cash_sleeve_ticker or "SGOV").upper()
    rows: List[dict] = []
    for t in targets:
        ticker = str(t["ticker"]).upper()
        mv = float(positions.get(ticker, 0.0) or 0.0)
        weight = float(t.get("target_weight", 0.0) or 0.0)
        band = float(t.get("band", 0.0) or 0.0)
        status = str(t.get("status", "active"))
        quotable = bool(t.get("quotable", True))
        target_dollars = target_dollars_for_weight(weight, equity)
        row = {
            "ticker": ticker,
            "sleeve": t.get("sleeve"),
            "target_weight": weight,
            "band": band,
            "status": status,
            "quotable": quotable,
            "current_mv": round(mv, 2),
            "target_dollars": round(target_dollars, 2),
        }
        if ticker == cash:
            # Cash is the residual ballast — never bought/trimmed like an engine name.
            row.update(intent="cash_residual", delta_dollars=0.0,
                       drift_pct=0.0, note="cash held as residual buying power")
        elif not quotable:
            # No equities-MCP quote (spot crypto) -> not executable; weight sits in cash.
            row.update(intent="skip_unquotable", delta_dollars=0.0,
                       drift_pct=round(weight_drift(mv, equity, weight), 4),
                       note="no equities-MCP quote; held in cash")
        else:
            intent, target_dollars, delta = rebalance_intent(mv, equity, weight, band, status)
            row.update(intent=intent, delta_dollars=round(delta, 2),
                       target_dollars=round(target_dollars, 2),
                       drift_pct=round(weight_drift(mv, equity, weight), 4),
                       note="")
        rows.append(row)
    return rows
