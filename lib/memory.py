"""Distilled decision memory: a compact per-ticker scorecard fed back to the model.

Recall is deliberately cheap and prompt-efficient (no embeddings): we summarize
the ledger's past decisions + realized outcomes for one ticker into a few lines
the analysis agents (Portfolio Manager + Trader) read as ``past_context``.

Two outcome signals, per the design:
- **directional** — did price move the way the call implied (decision_price -> a
  later price). Always computable, even for Hold/skip/dry-run days.
- **actual** — realized/unrealized P&L at the POSITION level (from the broker's
  cost basis); present only once we've really traded. Surfaced as a P&L line.

``build_scorecard`` is a PURE function over already-fetched rows (unit-tested);
``scorecard`` is the thin ledger-reading wrapper analyze.py calls.
"""

from __future__ import annotations

from typing import List, Optional

# Minimum age (in trade-date days) before a decision's directional outcome is
# scored. Mirrors the framework's old 5-day holding window — short enough to
# build memory steadily, long enough to be a meaningful directional read.
HOLDING_DAYS = 5

_BULLISH = {"buy", "overweight"}
_BEARISH = {"sell", "underweight"}


def directional_return(decision_price: Optional[float], price_now: Optional[float]) -> Optional[float]:
    """Fractional return from the decision-time price to a later price.

    None when either price is missing or the decision price is non-positive
    (can't form a return) — callers leave the outcome's directional field NULL.
    """
    if not decision_price or decision_price <= 0 or price_now is None:
        return None
    return (price_now - decision_price) / decision_price


def is_hit(signal: Optional[str], ret: Optional[float]) -> Optional[bool]:
    """Was the directional call correct? None for ungradeable calls (Hold/etc.)."""
    if ret is None:
        return None
    s = (signal or "").strip().lower()
    if s in _BULLISH:
        return ret > 0
    if s in _BEARISH:
        return ret < 0
    return None  # Hold / Skip / unknown -> not a directional bet, excluded from hit-rate


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x * 100:+.1f}%" if x is not None else "n/a"


def build_scorecard(ticker: str, rows: List[dict], recent: int = 6) -> str:
    """Render a compact memory scorecard from decision+outcome rows (newest first).

    PURE: takes already-fetched dicts (as from Ledger.decisions_with_outcomes),
    returns a string (empty when there's no history). Each row may carry
    directional_return / holding_days / realized_pnl / signal / rationale.
    """
    if not rows:
        return ""

    resolved = [r for r in rows if r.get("directional_return") is not None]
    graded = [(r.get("signal"), r["directional_return"]) for r in resolved]
    graded = [(s, ret, is_hit(s, ret)) for (s, ret) in graded]
    gradeable = [g for g in graded if g[2] is not None]
    n_hit = sum(1 for g in gradeable if g[2])
    avg_dir = (sum(r["directional_return"] for r in resolved) / len(resolved)) if resolved else None
    realized_vals = [r["realized_pnl"] for r in rows if r.get("realized_pnl") is not None]
    realized_sum = sum(realized_vals) if realized_vals else None

    lines = [f"Your prior calls on {ticker}: {len(rows)} decision(s), {len(resolved)} resolved."]
    if gradeable:
        pct = n_hit / len(gradeable) * 100.0
        tail = f", avg move {_fmt_pct(avg_dir)}." if avg_dir is not None else "."
        lines.append(f"Directional hit-rate: {n_hit}/{len(gradeable)} ({pct:.0f}%){tail}")
    elif resolved:
        lines.append(f"Avg move {_fmt_pct(avg_dir)} (no directional bets to grade).")
    if realized_sum is not None:
        lines.append(f"Realized P&L on closed positions: ${realized_sum:+.2f}.")

    lines.append("Recent decisions (newest first):")
    for r in rows[:recent]:
        dr = r.get("directional_return")
        outcome = f"{_fmt_pct(dr)} / {r.get('holding_days') or '?'}d" if dr is not None else "pending"
        rationale = (r.get("rationale") or "").strip().replace("\n", " ")
        if len(rationale) > 140:
            rationale = rationale[:140] + "…"
        quote = f' | "{rationale}"' if rationale else ""
        lines.append(f"- {r.get('trade_date')} {r.get('signal')} -> {outcome}{quote}")
    return "\n".join(lines)


def scorecard(led, ticker: str, limit: int = 8) -> str:
    """Ledger-reading wrapper: fetch recent decisions+outcomes and render them.

    Read-only; never touches the broker or trading limits — this is the decision
    *memory* the analysis path is allowed to see.
    """
    rows = led.decisions_with_outcomes(ticker, limit=limit)
    return build_scorecard(ticker, rows)
