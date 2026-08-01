"""The signal ladder — the reference arms every benchmark result is reported against.

A benchmark number alone is meaningless. quiver's universe is a 2026 hand-pick of AI-supply-chain
winners, so an equal-weight buy-and-hold of it returned roughly +47% over 2022-23 against SPY's
~+2.6% — a ~44-point edge with ZERO strategy skill. Only oracle-relative, hold-relative and
noise-relative numbers are interpretable, which is what these arms provide:

    all-cash < hold-through-the-wall < random-signal ensemble < lagged oracle < oracle

THE R:R TRAP (measured, not theoretical). The real `strategy.yaml` sets `risk_policy.min_reward_risk:
3.0`, and `_rr_verdict` binds the REBALANCE buy-to-target pass — which is where 100% of a replay's
orders originate. A source emitting a plausible-but-untuned stop/target (R:R 1.88) produced
**0 orders, +0.00% return, and 300 `reward_risk:below_floor` skips**, while the SAME source against
a fixture yaml with no `risk_policy` traded normally. So "did this arm trade at all?" is decided by
the stop/target convention, not by skill.

Two consequences, both enforced here:
  1. `rr` is an EXPLICIT parameter of every arm (`RRSpec`), never an incidental field. All arms in
     a comparison must share one `RRSpec` or the comparison is invalid — a free `rr` per arm hands
     whichever arm got the looser one an illegitimate edge.
  2. `RRSpec.ungraded()` reproduces the historical measurement (emit NO stop/target, so the gate
     passes ungraded). It is the DEFAULT because that is the regime the +221pt oracle-vs-hold gap
     was measured in — but it is now a named, visible choice rather than an accident.

EXPOSURE MATCHING. `random_signal` takes `buy_p` so the noise arm can be matched to the arm it is
the null for. An unmatched null measures how INVESTED an arm was, not whether it had skill: a
long-only book in a rising market beats any less-invested null trivially.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

# The exact field set `tick._run_plan` / `_run_construct` bind on (verified against tick.py:
# ticker/signal/position_pct/conviction/uncertainty/decision_price/entry_price/stop_loss/
# target_price/basis/next_review_hours/rationale_summary).
SignalSource = Callable[[str, Sequence[str], dict], list]


@dataclass(frozen=True)
class RRSpec:
    """How an arm expresses its stop and target — i.e. whether the R:R gate binds at all.

    `stop_pct` / `target_pct` are fractions of the entry price (0.05 == 5%).
    `graded=False` emits NEITHER field, so `_rr_verdict` returns an ungraded pass.
    """
    graded: bool = False
    stop_pct: float = 0.05
    target_pct: float = 0.18

    @staticmethod
    def ungraded() -> "RRSpec":
        """No stop/target emitted -> the R:R gate cannot bind. The historical default."""
        return RRSpec(graded=False)

    @staticmethod
    def graded_at(ratio: float, *, stop_pct: float = 0.05) -> "RRSpec":
        """A stop/target pair with an EXACT reward:risk of `ratio`."""
        return RRSpec(graded=True, stop_pct=stop_pct, target_pct=stop_pct * ratio)

    @property
    def ratio(self) -> Optional[float]:
        if not self.graded or self.stop_pct <= 0:
            return None
        return self.target_pct / self.stop_pct

    def levels(self, price: float) -> dict:
        """The stop/target fields for a long entry at `price` (empty when ungraded)."""
        if not self.graded or not price or price <= 0:
            return {}
        return {"stop_loss": round(price * (1.0 - self.stop_pct), 6),
                "target_price": round(price * (1.0 + self.target_pct), 6)}


def _row(ticker: str, signal: str, price: float, *, rr: RRSpec, conviction: float,
         position_pct: float, basis: str, note: str = "") -> dict:
    row = {"ticker": ticker, "signal": signal, "conviction": conviction, "uncertainty": 10.0,
           "position_pct": position_pct, "basis": basis, "catalyst": basis,
           "decision_price": price, "entry_price": price,
           "rationale_summary": note or basis, "next_review_hours": 24.0}
    if signal in ("Buy", "Overweight"):
        row.update(rr.levels(price))
    return row


# ---------------------------------------------------------------- the arms

def hold_book() -> SignalSource:
    """The DO-NOTHING floor. Emits no analyses at all, so the only orders are the initial
    book deploy. Everything else must be measured against this, not against zero."""
    def src(date, tickers, snap):
        return []
    return src


def oracle(price_map: dict, *, horizon: int = 5, rr: RRSpec = RRSpec.ungraded(),
           mode: str = "underweight", invert: bool = False) -> SignalSource:
    """PERFECT FORESIGHT — the upper bound on what any signal could express through this wall.

    Not attainable and not meant to be: if `oracle - hold` is SMALL, no brain improvement can
    reach P&L and the problem is the wall, not the signal. Measured at +221pt (2025-01..2026-07)
    and +226pt (2023-01..2024-12), with controls confirming the gap tracks signal quality."""
    dates = sorted(price_map)
    idx = {d: i for i, d in enumerate(dates)}
    bear = "Sell" if mode == "sell" else "Underweight"

    def src(date, tickers, snap):
        i = idx.get(date)
        if i is None:
            return []
        j = min(i + horizon, len(dates) - 1)
        out = []
        for tk in tickers:
            tk = str(tk).upper()
            p0 = (price_map.get(dates[i]) or {}).get(tk)
            p1 = (price_map.get(dates[j]) or {}).get(tk)
            if not (p0 and p1 and p0 > 0):
                continue
            fwd = p1 / p0 - 1.0
            # `invert` flips the DECISION, not the horizon — the true ANTI-ORACLE control.
            # (Negating `horizon` instead would look backward and produce an inverse-momentum
            # rule, which is a different signal entirely and is NOT a control: measured at
            # +57.5% vs hold +22.9% on a trending path, i.e. it accidentally made money.)
            bullish = (fwd < 0) if invert else (fwd > 0)
            tag = "anti_oracle" if invert else "oracle"
            if bullish:
                out.append(_row(tk, "Buy", p0, rr=rr,
                                conviction=max(60.0, min(100.0, 50.0 + 1000.0 * abs(fwd))),
                                position_pct=20.0, basis=f"{tag}{horizon}:up",
                                note=f"fwd{horizon}={fwd:+.4f}"))
            else:
                out.append(_row(tk, bear, p0, rr=rr, conviction=0.0, position_pct=0.0,
                                basis=f"{tag}{horizon}:down", note=f"fwd{horizon}={fwd:+.4f}"))
        return out
    return src


def anti_oracle(price_map: dict, *, horizon: int = 5,
                rr: RRSpec = RRSpec.ungraded()) -> SignalSource:
    """PERFECT FORESIGHT, INVERTED — the negative control.

    This is the arm that makes the oracle gap mean something. If `oracle - hold` is large but
    `anti_oracle - hold` is ALSO large, the gap is measuring exposure or turnover, not skill.
    Measured on real prices: anti-oracle -46.61% against hold +49.53% (gap -96pts), versus
    oracle +268.76% (gap +219pts) — monotone in signal quality, which is the property that
    licenses reading the oracle gap as a ceiling on expressible edge."""
    return oracle(price_map, horizon=horizon, rr=rr, invert=True)


def lagged_oracle(price_map: dict, *, horizon: int = 5, lag: int = 5,
                  rr: RRSpec = RRSpec.ungraded()) -> SignalSource:
    """The oracle, `lag` sessions stale — a tunable SKILL DIAL between oracle and noise.

    Sweeping `lag` traces how much edge survives a given amount of staleness, which is the
    honest way to ask "how good would a signal have to be to matter here?\""""
    dates = sorted(price_map)
    idx = {d: i for i, d in enumerate(dates)}

    def src(date, tickers, snap):
        i = idx.get(date)
        if i is None:
            return []
        base = max(0, i - lag)
        j = min(base + horizon, len(dates) - 1)
        out = []
        for tk in tickers:
            tk = str(tk).upper()
            p_now = (price_map.get(dates[i]) or {}).get(tk)
            p0 = (price_map.get(dates[base]) or {}).get(tk)
            p1 = (price_map.get(dates[j]) or {}).get(tk)
            if not (p_now and p0 and p1 and p0 > 0):
                continue
            fwd = p1 / p0 - 1.0
            sig = "Buy" if fwd > 0 else "Underweight"
            out.append(_row(tk, sig, p_now, rr=rr,
                            conviction=(70.0 if fwd > 0 else 0.0),
                            position_pct=(20.0 if fwd > 0 else 0.0),
                            basis=f"lagged{lag}:{'up' if fwd > 0 else 'down'}"))
        return out
    return src


def random_signal(*, seed: int, buy_p: float = 0.5,
                  rr: RRSpec = RRSpec.ungraded()) -> SignalSource:
    """THE NULL. Seeded so a run is byte-reproducible; the ENSEMBLE over seeds is the null
    distribution every claim must clear.

    `buy_p` exists for EXPOSURE MATCHING — set it to the bull rate of the arm under test, or the
    comparison measures investedness rather than skill. `beats_null` in bench/sweep.py refuses a
    verdict when the IQRs overlap, which is the other half of that guard."""
    def src(date, tickers, snap):
        out = []
        for tk in sorted(str(t).upper() for t in tickers):
            # Seeded per (seed, date, ticker): reproducible across processes AND independent of
            # iteration order, so a parallel sweep cannot perturb it.
            rng = random.Random(f"{seed}|{date}|{tk}")
            px = ((snap or {}).get("quotes") or {}).get(tk)
            if not px or px <= 0:
                continue
            if rng.random() < buy_p:
                out.append(_row(tk, "Buy", float(px), rr=rr, conviction=70.0,
                                position_pct=20.0, basis=f"random:{seed}"))
            else:
                out.append(_row(tk, "Underweight", float(px), rr=rr, conviction=0.0,
                                position_pct=0.0, basis=f"random:{seed}"))
        return out
    return src


def sma_cross(price_map: dict, *, fast: int = 20, slow: int = 50,
              rr: RRSpec = RRSpec.ungraded()) -> SignalSource:
    """A real (weak) technical rule — the 'is this better than a moving-average cross?' rung."""
    dates = sorted(price_map)

    def series(tk, upto):
        out = []
        for d in dates:
            if d > upto:
                break
            px = (price_map.get(d) or {}).get(tk)
            if px and px > 0:
                out.append(float(px))
        return out

    def src(date, tickers, snap):
        out = []
        for tk in tickers:
            tk = str(tk).upper()
            s = series(tk, date)
            if len(s) < slow:
                continue
            f, sl, px = sum(s[-fast:]) / fast, sum(s[-slow:]) / slow, s[-1]
            if f > sl:
                out.append(_row(tk, "Buy", px, rr=rr, conviction=70.0, position_pct=20.0,
                                basis=f"sma{fast}/{slow}:up"))
            elif f < sl:
                out.append(_row(tk, "Underweight", px, rr=rr, conviction=0.0, position_pct=0.0,
                                basis=f"sma{fast}/{slow}:down"))
        return out
    return src


def equal_weight_hold_curve(price_map: dict, dates: Sequence[str], tickers: Sequence[str],
                            starting_cash: float) -> list:
    """Equal-weight buy-and-hold, computed entirely OUTSIDE the wall.

    This is the survivorship yardstick. It answers "what would owning this basket have returned
    with no strategy at all?" — and on quiver's 2026-selected universe that number is large, which
    is precisely why absolute replay returns are uninterpretable.
    """
    dates = [d for d in dates if price_map.get(d)]
    if not dates:
        return []
    first = price_map[dates[0]]
    held = [t for t in (str(x).upper() for x in tickers)
            if (first.get(t) or 0) > 0]
    if not held:
        return []
    per = float(starting_cash) / len(held)
    shares = {t: per / float(first[t]) for t in held}
    last = {t: float(first[t]) for t in held}
    curve = []
    for d in dates:
        px = price_map.get(d) or {}
        for t in held:
            v = px.get(t)
            if v and v > 0:
                last[t] = float(v)
        eq = sum(shares[t] * last[t] for t in held)
        curve.append({"date": d, "equity": round(eq, 6)})
    return curve


ARMS = {"hold": hold_book, "oracle": oracle, "anti_oracle": anti_oracle, "lagged": lagged_oracle,
        "random": random_signal, "sma": sma_cross}
