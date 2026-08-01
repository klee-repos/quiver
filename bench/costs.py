"""Transaction-cost models for the replay.

WHY THIS MATTERS MORE THAN IT LOOKS. `lib/wall_replay.py` uses ONE `prices` dict to size, to
fill, and to mark, so buying at P and immediately marking at P means **trading is literally
free**. Every anti-churn knob (`conviction_rebalance_min_delta_pct`, `min_trade_notional`,
`rebalance_drift_band_pct`) therefore optimizes toward ZERO on the uninstrumented harness —
reproducing exactly the churn the SOXX RCA already cost real money for, and which was mitigated
by hand-raising that knob 2 -> 6 on the live box.

`ZeroCost` is the DEFAULT so every existing curve stays byte-identical (the golden fixture
enforces this). `SpreadCost` is opt-in, and the useful output is not one number but the
BREAK-EVEN: the bps at which an arm's edge disappears.

HONEST LIMIT: a spread/impact figure is a parameter you CHOOSE, not a measurement. There are no
partial fills, no ADV caps, no book, no gaps, no halts, no rejections. Report cost sensitivity as
a curve across bps, never as a single "after costs" number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class CostModel(Protocol):
    def exec_price(self, side: str, ticker: str, ref: float) -> float: ...
    def fee(self, side: str, dollars: float, shares: float) -> float: ...
    @property
    def is_zero(self) -> bool: ...


@dataclass(frozen=True)
class ZeroCost:
    """The DEFAULT. Frictionless — identical to the pre-cost-model behaviour, byte for byte."""

    def exec_price(self, side: str, ticker: str, ref: float) -> float:
        return ref

    def fee(self, side: str, dollars: float, shares: float) -> float:
        return 0.0

    @property
    def is_zero(self) -> bool:
        return True


@dataclass(frozen=True)
class SpreadCost:
    """Half-spread slippage in bps, plus the real US regulatory sell-side fees.

    A buy fills ABOVE the reference and a sell BELOW it — the cost is always adverse, which is
    the property a naive "apply bps" implementation gets wrong by signing it once.

    `sec_fee_rate` / `taf_per_share` default to the published sell-side rates; both are charged
    on SELLS ONLY, which is why a high-turnover long-only arm pays asymmetrically.
    """
    bps: float = 5.0
    sec_fee_rate: float = 0.0000278      # SEC Section 31, sells only, on notional
    taf_per_share: float = 0.000166      # FINRA TAF, sells only, per share
    taf_cap: float = 8.30

    def exec_price(self, side: str, ticker: str, ref: float) -> float:
        if not ref or ref <= 0:
            return ref
        half = self.bps / 10000.0
        return ref * (1.0 + half) if side == "buy" else ref * (1.0 - half)

    def fee(self, side: str, dollars: float, shares: float) -> float:
        if side != "sell":
            return 0.0
        return (abs(dollars) * self.sec_fee_rate
                + min(self.taf_cap, abs(shares) * self.taf_per_share))

    @property
    def is_zero(self) -> bool:
        return self.bps == 0.0 and self.sec_fee_rate == 0.0 and self.taf_per_share == 0.0


def cost_elasticity(run_fn, *, bps_grid=(0.0, 5.0, 10.0, 25.0, 50.0, 100.0)) -> dict:
    """Run the same arm across a bps grid and report cost sensitivity.

    **RETURN IS NOT MONOTONE IN BPS ON THIS HARNESS, AND THAT IS NOT A BUG.** Cost reduces
    `broker.cash`, which reduces the deployable equity `tick.py` sizes against, which reduces
    `target_dollars`. So a cost increase also makes the strategy trade SMALLER, and on a path
    where the strategy was over-trading, being poorer can help. Measured on the golden path:

        bps    0 ->  +4.069%
        bps   10 ->  +7.246%
        bps   50 -> +10.946%     <-- higher return WITH more cost
        bps  200 ->  +1.937%

    Consequences, enforced here rather than left as a footnote:
      * `break_even_bps` is returned ONLY when the return curve is actually monotone. Otherwise
        it is None and `monotone` is False — scanning a non-monotone curve for a first crossing
        produces a number that reads like a break-even and is not one.
      * `cost_drag_pct` (the dollars actually charged) IS monotone and is the honest measure of
        how much friction was applied. Use it, not the return delta, to reason about cost.
    """
    out = {}
    for bps in bps_grid:
        s = run_fn(ZeroCost() if bps == 0 else SpreadCost(bps=bps))
        out[bps] = {"total_return_pct": s.get("total_return_pct"),
                    "turnover_usd": s.get("turnover_usd"),
                    "cost_drag_pct": s.get("cost_drag_pct"),
                    "n_fills": s.get("n_fills")}
    ordered = sorted(out)
    rets = [out[b]["total_return_pct"] for b in ordered]
    monotone = all(a is not None and b is not None and a >= b - 1e-9
                   for a, b in zip(rets, rets[1:]))
    base = out.get(0.0, {}).get("total_return_pct")
    be = None
    if monotone and base is not None and base > 0:
        for bps in ordered:
            r = out[bps]["total_return_pct"]
            if r is not None and r <= 0.0:
                be = bps
                break
    return {"returns_by_bps": out, "break_even_bps": be, "zero_cost_return_pct": base,
            "monotone": monotone,
            "note": ("" if monotone else
                     "RETURN IS NON-MONOTONE IN COST — cost feeds back into sizing "
                     "(cash -> deployable equity -> target_dollars), so a higher-cost run can "
                     "return more by trading smaller. break_even_bps is withheld; read "
                     "cost_drag_pct instead.")}
