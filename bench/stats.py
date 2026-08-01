"""Statistics for the benchmark — built around what this data CANNOT support.

quiver's replay is ONE price path over ~9 tightly-correlated AI-supply-chain names. The effective
number of independent bets is single digits. This repo has already reached that conclusion once
by a different route: a political-signal study found a minimum detectable effect of 7.1%/yr
against a maximum expressible alpha of 3.83%/yr, i.e. the experiment could not have detected the
thing it was measuring.

So the job of this module is to make under-powered results LOOK under-powered:
  * daily returns from one path are autocorrelated -> a naive i.i.d. bootstrap understates the
    CI, so `block_bootstrap` resamples CONTIGUOUS BLOCKS.
  * `beats_null` requires DISJOINT IQRs, not a point estimate on the right side.
  * `mde_note` states the minimum detectable effect BEFORE a result is read.

`lib.risk` already implements sharpe/sortino/max_drawdown and returns `Metric` OBJECTS (not
floats) over PER-TRADE returns. `sharpe_from_curve` here is a different quantity — a portfolio
Sharpe over DAILY equity returns — and is named distinctly so the two are never conflated.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Optional, Sequence


def daily_returns(equity_curve: Sequence[dict]) -> list:
    out = []
    prev = None
    for p in equity_curve:
        eq = p.get("equity")
        if eq is None or eq <= 0:
            continue
        if prev is not None and prev > 0:
            out.append(eq / prev - 1.0)
        prev = eq
    return out


def sharpe_from_curve(equity_curve: Sequence[dict], *, periods_per_year: int = 252,
                      min_n: int = 20) -> Optional[float]:
    """Annualised Sharpe over DAILY equity returns.

    NOT the same quantity as `lib.risk.sharpe`, which is a per-trade ratio over irregular ~5-day
    holds. Returns None below `min_n` rather than a confident number from nothing."""
    rs = daily_returns(equity_curve)
    if len(rs) < min_n:
        return None
    sd = statistics.pstdev(rs)
    if sd == 0:
        return None
    return (statistics.fmean(rs) / sd) * math.sqrt(periods_per_year)


def max_drawdown_pct(equity_curve: Sequence[dict]) -> float:
    peak = None
    mdd = 0.0
    for p in equity_curve:
        eq = p.get("equity")
        if eq is None:
            continue
        peak = eq if peak is None else max(peak, eq)
        if peak > 0:
            mdd = min(mdd, (eq - peak) / peak)
    return round(mdd * 100.0, 4)


def block_bootstrap(returns: Sequence[float], *, block: int = 20, n_boot: int = 2000,
                    seed: int = 0, statistic=None) -> Optional[tuple]:
    """Stationary block bootstrap -> (lo, mid, hi) at 5/50/95.

    Contiguous blocks, because daily returns on one path are autocorrelated and an i.i.d.
    resample would report a CI several times too tight. Returns None when there is not enough
    data for the requested block size — an honest refusal beats a fabricated interval.
    """
    rs = [float(r) for r in returns]
    if len(rs) < max(block * 2, 10):
        return None
    stat = statistic or statistics.fmean
    rng = random.Random(seed)
    n = len(rs)
    n_blocks = max(1, n // block)
    samples = []
    for _ in range(n_boot):
        draw = []
        for _ in range(n_blocks):
            start = rng.randrange(n)
            draw.extend(rs[(start + k) % n] for k in range(block))
        samples.append(stat(draw[:n]))
    samples.sort()
    def q(p):
        i = min(len(samples) - 1, max(0, int(round(p * (len(samples) - 1)))))
        return samples[i]
    return (q(0.05), q(0.50), q(0.95))


def iqr(values: Sequence[float]) -> Optional[tuple]:
    """(Q1, median, Q3) by LINEAR INTERPOLATION.

    The nearest-rank version this replaces was wrong in a way that inverted verdicts, not merely
    imprecise: `iqr([1,2,3,4])` returned `(2.0, 3.0, 3.0)` — a median of 3.0 against a true 2.5,
    and a degenerate Q2 == Q3. Feeding that into `beats_null([2,3,4,5], [0,1,2,6])` produced
    "beats / IQRs disjoint" when the correct quartiles (observed Q1 2.75 vs null Q3 3.00) OVERLAP,
    i.e. it violated this module's own documented bar. n=4 is not exotic — it is this function's
    own minimum and the seed count the sweep tests use. The same estimator supplies the `median`
    that `bench.sweep.summarize` ranks on, so the argmax could invert too.
    """
    vs = sorted(float(v) for v in values if v is not None)
    if len(vs) < 4:
        return None
    q1, _, q3 = statistics.quantiles(vs, n=4, method="inclusive")
    return (q1, statistics.median(vs), q3)


def beats_null(observed: Sequence[float], null: Sequence[float]) -> dict:
    """A verdict ONLY when the IQRs are DISJOINT.

    The reference paper's headline was that its apparent leader had the widest spread, so a
    different 10 rollouts would have reordered the leaderboard. A point estimate on the right
    side of a null mean is not evidence; non-overlapping middle halves is a weak but honest bar.
    """
    a, b = iqr(observed), iqr(null)
    if a is None or b is None:
        return {"verdict": None, "reason": "insufficient samples for an IQR (need >=4 each)",
                "observed_iqr": a, "null_iqr": b}
    disjoint_above = a[0] > b[2]
    disjoint_below = a[2] < b[0]
    if not (disjoint_above or disjoint_below):
        return {"verdict": None,
                "reason": "IQRs OVERLAP — no verdict. This is the expected outcome on one "
                          "price path with ~9 correlated names; report it as inconclusive, "
                          "not as a narrow win.",
                "observed_iqr": a, "null_iqr": b}
    return {"verdict": "beats" if disjoint_above else "loses",
            "reason": "IQRs disjoint", "observed_iqr": a, "null_iqr": b}


def null_percentile(observed: float, null_samples: Sequence[float]) -> Optional[float]:
    vs = [float(v) for v in null_samples if v is not None]
    if not vs:
        return None
    return sum(1 for v in vs if v <= observed) / len(vs)


def mde_note(n_effective_bets: int, daily_vol: float, *, periods_per_year: int = 252) -> str:
    """State the MINIMUM DETECTABLE EFFECT before any result is read.

    quiver has been burned by the opposite order once already — a signal study concluded a
    real sensor was unusable only after the fact, because MDE 7.1%/yr exceeded the maximum
    expressible alpha of 3.83%/yr. Printing this first makes an under-powered result unreadable
    as a strong one."""
    if n_effective_bets <= 1 or daily_vol <= 0:
        return "MDE: undefined (need n>1 independent bets and positive vol)"
    ann_vol = daily_vol * math.sqrt(periods_per_year)
    mde = 2.8 * ann_vol / math.sqrt(n_effective_bets)   # ~80% power, 5% two-sided
    return (f"MDE ~= {mde * 100:.2f}%/yr at n_effective={n_effective_bets}, "
            f"ann_vol={ann_vol * 100:.1f}%. Any measured edge SMALLER than this is not "
            f"distinguishable from noise by this experiment.")
