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

# How many decision rows the POSITION block reads back over. A query window, not a
# rule: an open run can start well before the scorecard's own short window.
POSITION_WINDOW = 60

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


def plan_adherence(row: dict, price_now: Optional[float]) -> str:
    """Coarse plan-adherence grade for a resolved decision (F7/P7): did price reach the
    recorded TARGET or breach the recorded STOP since the decision? PTJ's "grade with no
    curve" — feeds calibrate so the system learns which setups' plans actually held.

    Pure over the decision row + a later price. Returns:
      * 'target_hit'    -> price >= the recorded target_price (the plan's upside landed).
      * 'stop_violated' -> price <= the recorded stop_loss (the downside the stop guards fired).
      * 'within'        -> a stop/target was recorded but neither was reached.
      * 'na'            -> no stop/target recorded, or no usable price (not gradeable).
    """
    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    p = _f(price_now)
    if not p or p <= 0:
        return "na"
    stop = _f((row or {}).get("stop_loss"))
    target = _f((row or {}).get("target_price"))
    if target and p >= target:
        return "target_hit"
    if stop and p <= stop:
        return "stop_violated"
    if stop or target:
        return "within"
    return "na"


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x * 100:+.1f}%" if x is not None else "n/a"


def _is_skip(row: dict) -> bool:
    """A decision that put NO capital at risk (Underweight/Sell with no position, or
    any explicit skip). Grading these directionally poisons the hit-rate: an
    Underweight-skip into a +9% move is recorded as a 'miss' on a trade we never made.
    They stay in the recent-decisions list for context but never count toward skill.
    """
    return (row.get("intent") or "").strip().lower() == "skip"


def score_return(row: dict) -> Optional[float]:
    """The return we LEARN from: excess-vs-benchmark (alpha) when present — the bot's
    SKILL — else the absolute directional return (which is mostly market beta). In a
    bull tape every long 'hits' on absolute return, so alpha is what makes the loop
    measure the decision, not the market."""
    a = row.get("alpha")
    return a if a is not None else row.get("directional_return")


def build_scorecard(ticker: str, rows: List[dict], recent: int = 6) -> str:
    """Render a compact memory scorecard from decision+outcome rows (newest first).

    PURE: takes already-fetched dicts (as from Ledger.decisions_with_outcomes),
    returns a string (empty when there's no history). Each row may carry
    directional_return / alpha / intent / holding_days / realized_pnl / signal /
    basis / rationale.

    Two corrections vs. a naive hit-rate (both ground the learning loop in truth):
      * SKIP decisions (no capital at risk) are excluded from the graded set.
      * grading + avg use ``score_return`` (alpha-when-present), so a bull-tape long
        is not an automatic 'hit' — the loop measures skill, not beta.
    """
    if not rows:
        return ""

    positioned = [r for r in rows if not _is_skip(r)]
    resolved = [(r, sr) for r in positioned if (sr := score_return(r)) is not None]
    graded = [(r.get("signal"), sr, is_hit(r.get("signal"), sr)) for (r, sr) in resolved]
    gradeable = [g for g in graded if g[2] is not None]
    n_hit = sum(1 for g in gradeable if g[2])
    scores = [sr for (_r, sr) in resolved]
    avg_dir = (sum(scores) / len(scores)) if scores else None
    # Realized P&L is a NAME-level figure the orchestrator copies onto EVERY pending
    # decision for the ticker when the position closes. With always-on multi-tranche
    # buys a single close lands on several decision rows with the IDENTICAL value, so a
    # naive sum double/triple-counts it. Dedup distinct close values (set) — one close
    # collapses to one figure, while genuinely-distinct closes still sum.
    realized_vals = {r["realized_pnl"] for r in rows if r.get("realized_pnl") is not None}
    realized_sum = sum(realized_vals) if realized_vals else None
    metric_basis = "excess-vs-benchmark" if any(r.get("alpha") is not None for (r, _sr) in resolved) else "absolute"

    lines = [f"Your prior calls on {ticker}: {len(rows)} decision(s), "
             f"{len(resolved)} resolved on position-taking calls (skips excluded)."]
    if gradeable:
        pct = n_hit / len(gradeable) * 100.0
        tail = f", avg move {_fmt_pct(avg_dir)} ({metric_basis})." if avg_dir is not None else "."
        lines.append(f"Directional hit-rate: {n_hit}/{len(gradeable)} ({pct:.0f}%){tail}")
    elif resolved:
        lines.append(f"Avg move {_fmt_pct(avg_dir)} ({metric_basis}; no directional bets to grade).")
    if realized_sum is not None:
        lines.append(f"Realized P&L on closed positions: ${realized_sum:+.2f}.")

    lines.append("Recent decisions (newest first):")
    for r in rows[:recent]:
        sr = score_return(r)
        outcome = f"{_fmt_pct(sr)} / {r.get('holding_days') or '?'}d" if sr is not None else "pending"
        basis = (r.get("basis") or "").strip()
        basis_tok = f" [{basis}]" if basis else ""
        rationale = (r.get("rationale") or "").strip().replace("\n", " ")
        if len(rationale) > 140:
            rationale = rationale[:140] + "…"
        quote = f' | "{rationale}"' if rationale else ""
        lines.append(f"- {r.get('trade_date')} {r.get('signal')}{basis_tok} -> {outcome}{quote}")
    return "\n".join(lines)


def build_position_block(ticker: str, rows: List[dict]) -> str:
    """Render the CURRENT open run of decisions on ``ticker`` (newest first).

    The model judges the stock but never sees the position, so it cannot weigh a
    call it already made. This block gives it that, from the ledger only.

    What the ledger does NOT hold, and this never claims:
      * a fill price. ``orders`` records no fill, and a market buy stores
        ``quantity`` as NULL. ``decisions.entry_price`` is the model's own SEED,
        never a paid price.
      * a share count, so there is no cost basis and no average.
    So print each dated decision quote separately. Never average them, and never
    write "you paid" or "entry".

    The run stops at the last row that closed the position, so an older, already
    exited run never renders as if the bot still holds it.

    PURE over already-fetched rows. Returns '' when no open run exists.
    """
    if not rows:
        return ""
    run = []
    for r in rows:                       # newest first
        intent = str(r.get("intent") or "").strip().lower()
        if intent in ("sell", "exit"):   # the position closed here; stop
            break
        run.append(r)
    opens = [r for r in run if str(r.get("intent") or "").strip().lower() == "buy"]
    if not opens:
        return ""

    oldest = opens[-1]
    lines = [f"Your CURRENT position in {ticker} (ledger record of your own past "
             f"decisions — NOT limits, NOT a position snapshot):"]
    since = str(oldest.get("trade_date") or "").strip()
    if since:
        lines.append(f"- Held since your first recorded buy decision on {since}.")
    bases = [str(r.get("basis") or "").strip() for r in opens if (r.get("basis") or "").strip()]
    if bases:
        lines.append(f"- The thesis you bought on: {bases[-1]}.")
    for r in opens:
        dp = r.get("decision_price")
        px = f"{float(dp):.2f}" if dp is not None else "unrecorded"
        sr = score_return(r)
        move = f", moved {_fmt_pct(sr)} since" if sr is not None else ""
        lines.append(f"- {r.get('trade_date')}: quote at decision time {px}"
                     f" (the fill price is not recorded){move}.")
    pnl = {r["unrealized_pnl"] for r in run if r.get("unrealized_pnl") is not None}
    if pnl:
        lines.append(f"- Open P&L at the position level: ${sum(pnl):+.2f}.")
    lines.append("These are facts, not an instruction. A decision-time quote is a sunk "
                 "fact: it does not make the name cheap or expensive today. Judge whether "
                 "the thesis still holds. A position that has gone your way deserves the "
                 "same re-examination as one that has not.")
    return "\n".join(lines)


def build_sliced_scorecard(rows: List[dict], key: str, label: str, *, min_n: int = 3) -> str:
    """Cross-sectional 'what KIND of call wins' scorecard: group resolved, position-taking
    decisions by ``row[key]`` (e.g. 'basis' or 'sleeve') and show per-group hit-rate +
    avg skill-return (alpha when present) + N. Answers the user's goal — "what kind of call
    do I keep losing" — which per-ticker recall can't, because names rotate.

    PURE over already-fetched rows. Groups with fewer than ``min_n`` graded bets are omitted
    (too thin to learn from). Returns '' when nothing is gradeable yet.
    """
    groups: dict = {}
    for r in rows:
        if _is_skip(r):
            continue
        sr = score_return(r)
        if sr is None:
            continue
        g = (r.get(key) or "").strip() or "(unlabeled)"
        groups.setdefault(g, []).append((r.get("signal"), sr))
    lines = []
    for g in sorted(groups):
        graded = [(s, ret, is_hit(s, ret)) for (s, ret) in groups[g]]
        gradeable = [x for x in graded if x[2] is not None]
        if len(gradeable) < min_n:
            continue
        n_hit = sum(1 for x in gradeable if x[2])
        avg = sum(ret for (_s, ret, _h) in gradeable) / len(gradeable)
        lines.append(f"- {g}: {n_hit}/{len(gradeable)} ({n_hit / len(gradeable) * 100:.0f}%) "
                     f"hit, avg {_fmt_pct(avg)}")
    if not lines:
        return ""
    return f"How your calls perform by {label} (skill = excess-vs-benchmark when scored):\n" + "\n".join(lines)


def scorecard(led, ticker: str, limit: int = 8) -> str:
    """Ledger-reading wrapper: fetch recent decisions+outcomes and render them.

    Read-only; never touches the broker or trading limits — this is the decision
    *memory* the analysis path is allowed to see.
    """
    rows = led.decisions_with_outcomes(ticker, limit=limit)
    card = build_scorecard(ticker, rows)
    # The open run can be older than the scorecard window, so read a WIDER window
    # for it. This is a query window, never a rule: it bounds how far back the
    # position block looks, and it decides nothing.
    pos = build_position_block(ticker,
                               led.decisions_with_outcomes(ticker, limit=POSITION_WINDOW))
    return "\n\n".join([s for s in (pos, card) if s])
