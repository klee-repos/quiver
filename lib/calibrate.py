"""Per-ticker conviction calibration learned from realized outcomes.

The allocator (``lib.allocate``) multiplies each name's model conviction by a
learned ``calibration`` factor before turning it into a target weight. This module
computes that factor from the ledger's own decision history: a ticker whose past
directional calls have been RIGHT earns a multiplier > 1 (its conviction is
trusted more), a ticker that's been WRONG earns < 1, and a ticker with too little
history stays at 1.0 (no opinion).

Three guards keep this from becoming a runaway feedback loop on real money:
  * **Low-N shrink** — below ``min_n`` graded bets, the multiplier is exactly 1.0
    (no calibration without evidence).
  * **Smooth shrinkage** — above ``min_n``, the skill signal is shrunk toward
    neutral by ``n/(n+smoothing_k)`` so a small sample can't swing the factor far.
  * **Hard clamp** — the result is clamped to ``[lo, hi]`` (default [0.5, 1.5]),
    so no single name can compound its own weight unbounded across days.

The decision/execution wall holds: this reads the ledger's decision MEMORY
(``decisions_with_outcomes`` — past calls + realized outcomes, the one thing the
analysis/sizing side may see) via ``lib.memory`` helpers. It NEVER imports
``lib.signals``/the broker/config trading limits. Pure + deterministic given rows.
"""

from __future__ import annotations

from typing import Dict, List

import lib.memory as memory

# Conservative defaults (the operator overrides via strategy.yaml risk_policy /
# learning where wired). Tuned so a perfect record nudges weight up ~50% at most,
# and a terrible one halves it — never enough to dominate the per-name/sleeve caps.
DEFAULT_MIN_N = 5          # graded directional bets required before any calibration
DEFAULT_SPAN = 1.0         # maps a 0..1 hit-rate to roughly [1-span/2, 1+span/2]
DEFAULT_LO = 0.5
DEFAULT_HI = 1.5
DEFAULT_SMOOTHING_K = 5.0  # evidence half-weight: at n==k the skill signal is halved
DEFAULT_LIMIT = 20         # recent decisions per ticker to learn from


def _graded_hits(rows: List[dict]) -> List[bool]:
    """The resolved, position-taking, directionally-gradeable calls (reusing the
    memory module's truth filters: skips excluded; alpha-when-present return)."""
    out: List[bool] = []
    for r in rows:
        if (r.get("intent") or "").strip().lower() == "skip":
            continue
        sr = memory.score_return(r)
        hit = memory.is_hit(r.get("signal"), sr)
        if hit is not None:
            out.append(bool(hit))
    return out


def calibration_multiplier(
    rows: List[dict], *, min_n: int = DEFAULT_MIN_N, span: float = DEFAULT_SPAN,
    lo: float = DEFAULT_LO, hi: float = DEFAULT_HI,
    smoothing_k: float = DEFAULT_SMOOTHING_K,
) -> float:
    """Conviction multiplier in ``[lo, hi]`` from one ticker's decision rows.

    1.0 (neutral) below ``min_n`` graded bets. Above it: take the directional
    hit-rate, shrink it toward 0.5 by ``n/(n+smoothing_k)`` (so small samples stay
    near neutral), map to a multiplier centered on 1.0 via ``span``, and clamp.
    A perfect record approaches ``hi``; a 50% record stays ~1.0; a 0% record
    approaches ``lo``.
    """
    graded = _graded_hits(rows)
    n = len(graded)
    if n < max(1, int(min_n)):
        return 1.0
    hit_rate = sum(1 for h in graded if h) / n
    eff = 0.5 + (hit_rate - 0.5) * (n / (n + max(0.0, smoothing_k)))
    mult = 1.0 + (eff - 0.5) * span
    return max(lo, min(hi, mult))


def build_calibration(
    led, tickers, *, limit: int = DEFAULT_LIMIT, min_n: int = DEFAULT_MIN_N,
    span: float = DEFAULT_SPAN, lo: float = DEFAULT_LO, hi: float = DEFAULT_HI,
    smoothing_k: float = DEFAULT_SMOOTHING_K,
) -> Dict[str, float]:
    """Ledger-reading wrapper: ``{TICKER: multiplier}`` for each candidate ticker.

    Read-only over the decision memory. Best-effort per ticker: a read hiccup on
    one name degrades it to the neutral 1.0 (omitted), never raising into a tick.
    """
    out: Dict[str, float] = {}
    for t in (tickers or []):
        tk = str(t).strip().upper()
        if not tk:
            continue
        try:
            rows = led.decisions_with_outcomes(tk, limit=limit)
            m = calibration_multiplier(rows, min_n=min_n, span=span, lo=lo, hi=hi,
                                       smoothing_k=smoothing_k)
        except Exception:  # noqa: BLE001 — calibration is advisory; never break a tick
            m = 1.0
        out[tk] = m
    return out
