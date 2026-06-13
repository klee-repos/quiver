"""Deterministic risk + return statistics over the ledger's decision returns.

PURE: every function is side-effect-free and unit-tested (see tests/test_units.py).
No I/O, no broker, no trading limits — this is analysis-side ("use case 2") math
that feeds the LLM agents' REASONING as context. It must NEVER feed the
deterministic sizing/clamps in ``lib/signals.py`` (a boundary test in
tests/test_units.py enforces that signals.py / tick.py plan never import this).

Every metric returns a :class:`Metric` carrying ``value`` + ``n`` + ``formula`` +
the ACTUAL inputs + a note, so each number written to memory can be hand-audited
("show me the proof"). Small samples are first-class: N<2 / zero-variance /
no-downside / no-loss return ``value=None`` with the reason in ``note`` — never a
divide-by-zero, never an exception. Callers pass returns in CHRONOLOGICAL order
(oldest -> newest); these functions never sort, so the ordering stays the caller's
responsibility (and is testable on hand-built lists).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from lib.memory import is_hit  # single source of directional grading (no duplication)


# --- the proof object -------------------------------------------------------
# Returned by every metric. Its render() prints one auditable line:
#   sharpe = -0.210  [n=3; formula: mean/stdev (ddof=1, rf=0); inputs: ...; LOW-CONFIDENCE]

@dataclass(frozen=True)
class Metric:
    name: str
    value: Optional[float]            # None when uncomputable (small N / zero variance)
    n: int                            # sample size actually used
    formula: str                      # human formula
    inputs_summary: str               # the ACTUAL numbers that went in
    unit: str = "ratio"              # "ratio" | "pct" (unsigned) | "signed_pct"
    note: str = ""                   # why None / annualization assumption / caveat
    low_confidence: bool = False     # True when n < low_confidence_min_n
    annualized: Optional[float] = None  # sharpe only; a labeled ESTIMATE, never the value

    def value_str(self) -> str:
        x = self.value
        if x is None:
            return "n/a"
        if self.unit == "pct":
            return f"{x * 100:.1f}%"
        if self.unit == "signed_pct":
            return f"{x * 100:+.1f}%"
        return f"{x:.3f}"  # dimensionless ratio (sharpe, sortino, profit_factor)

    def proof(self) -> str:
        """The auditable derivation: N + formula + the actual inputs (+ caveats)."""
        bits = [f"N={self.n}", f"formula: {self.formula}", f"inputs: {self.inputs_summary}"]
        if self.annualized is not None:
            bits.append(f"annualized≈{self.annualized:.3f} (ESTIMATE)")
        if self.note:
            bits.append(self.note)
        if self.low_confidence:
            bits.append("LOW-CONFIDENCE")
        return "; ".join(bits)

    def render(self) -> str:
        return f"{self.name} = {self.value_str()}  [{self.proof()}]"


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x * 100:+.1f}%" if x is not None else "n/a"


def _pct_list(xs: Sequence[float], cap: int = 8) -> str:
    shown = [_fmt_pct(x) for x in list(xs)[:cap]]
    extra = len(xs) - cap
    tail = f",…(+{extra})" if extra > 0 else ""
    return "[" + ",".join(shown) + tail + "]"


# --- core statistics --------------------------------------------------------

def mean_stdev(returns: Sequence[float]) -> Tuple[Optional[float], Optional[float], int]:
    """Sample mean and sample stdev (ddof=1). stdev is None when n<2 (undefined)."""
    n = len(returns)
    if n == 0:
        return (None, None, 0)
    mean = sum(returns) / n
    if n < 2:
        return (mean, None, n)
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)  # ddof=1
    sd = math.sqrt(var)
    # Snap floating-point dust to exact zero: identical inputs (e.g. [0.1,0.1,0.1])
    # produce var ~1e-17, not 0.0, which would let Sharpe explode to ~1e15. The
    # threshold sits far below any real return-stdev (~1e-4+), so it only catches
    # the degenerate "all returns effectively identical" case.
    if sd < 1e-12:
        sd = 0.0
    return (mean, sd, n)


def mean_return(returns: Sequence[float], *, low_n: int = 5) -> Metric:
    mean, _, n = mean_stdev(returns)
    return Metric(
        "mean_return", mean, n, "sum(r)/n", f"returns={_pct_list(returns)}",
        unit="signed_pct", note=("" if n else "n=0: no returns"),
        low_confidence=(n < low_n),
    )


def volatility(returns: Sequence[float], *, low_n: int = 5) -> Metric:
    mean, sd, n = mean_stdev(returns)
    inputs = f"returns={_pct_list(returns)}"
    if mean is not None:
        inputs += f"; mean={_fmt_pct(mean)}"
    return Metric(
        "volatility", sd, n, "stdev(returns, ddof=1)", inputs, unit="pct",
        note=("" if sd is not None else "n<2: sample stdev undefined"),
        low_confidence=(n < low_n),
    )


def sharpe(returns: Sequence[float], rf: float = 0.0, *,
           periods_per_year: Optional[float] = None, low_n: int = 5) -> Metric:
    """Per-trade Sharpe = mean(excess)/stdev(excess), excess = r - rf, ddof=1.

    Primary value is the dimensionless per-trade ratio. An ``annualized`` ESTIMATE
    (value * sqrt(periods_per_year)) is carried separately when periods_per_year is
    given — irregular ~5-day holds make annualizing an estimate, never the value.
    None (with reason) when n<2 or zero variance — never divides by zero.
    """
    excess = [r - rf for r in returns]
    mean, sd, n = mean_stdev(excess)
    value: Optional[float] = None
    annualized: Optional[float] = None
    if n < 2:
        note = "n<2: Sharpe undefined"
    elif sd == 0:
        note = "zero variance: Sharpe undefined"
    else:
        assert mean is not None and sd is not None  # n>=2 guarantees both
        value = mean / sd
        if periods_per_year:
            annualized = value * math.sqrt(periods_per_year)
            note = f"per-trade; annualized ×sqrt({periods_per_year:g})"
        else:
            note = f"per-trade (rf={rf:g})"
    return Metric(
        "sharpe", value, n, f"mean(excess)/stdev(excess), ddof=1, rf={rf:g}",
        f"excess={_pct_list(excess)}; mean={_fmt_pct(mean)}; stdev={_fmt_pct(sd)}",
        unit="ratio", note=note, low_confidence=(n < low_n), annualized=annualized,
    )


def sortino(returns: Sequence[float], rf: float = 0.0, *, low_n: int = 5) -> Metric:
    """Sortino = mean(excess)/downside_dev; downside_dev = sqrt(mean(e^2 for e<0)).

    None (with reason) when n<1, there are no downside observations (all >= rf), or
    the downside deviation is zero — never divides by zero.
    """
    excess = [r - rf for r in returns]
    n = len(excess)
    mean = (sum(excess) / n) if n else None
    downside = [e for e in excess if e < 0]
    value: Optional[float] = None
    if n < 1:
        note = "n<1: Sortino undefined"
    elif not downside:
        note = "no downside observations: Sortino undefined (all returns >= rf)"
    else:
        dd = math.sqrt(sum(e * e for e in downside) / len(downside))
        if dd == 0:
            note = "zero downside deviation: Sortino undefined"
        else:
            assert mean is not None  # n>=1 guarantees mean
            value = mean / dd
            note = f"downside n={len(downside)}, rf={rf:g}"
    return Metric(
        "sortino", value, n,
        "mean(excess)/downside_dev; downside_dev=sqrt(sum(min(e,0)^2)/count_downside)",
        f"excess={_pct_list(excess)}; downside={_pct_list(downside)}; mean={_fmt_pct(mean)}",
        unit="ratio", note=note, low_confidence=(n < low_n),
    )


def max_drawdown(curve: Sequence[float], *, kind: str) -> Metric:
    """Peak-to-trough max drawdown as a fraction (>=0).

    kind='equity': ``curve`` is equity levels, drawdown computed directly on them.
    kind='cumret': ``curve`` is a return series; the equity index prod(1+r) is built
    first (start 1.0), then the same peak-to-trough walk. Per-ticker AND portfolio
    use 'cumret' (decisions+outcomes only); 'equity' is for the digest path, which
    already sees account equity.
    """
    if kind == "cumret":
        levels: List[float] = []
        acc = 1.0
        for r in curve:
            acc *= (1.0 + r)
            levels.append(acc)
        formula = "peak-to-trough on prod(1+r) (start=1.0)"
        inputs = f"returns={_pct_list(curve)}"
    elif kind == "equity":
        levels = list(curve)
        formula = "peak-to-trough on equity levels"
        shown = [round(x, 2) for x in levels[:8]]
        inputs = f"equity={shown}" + (f"…(+{len(levels) - 8})" if len(levels) > 8 else "")
    else:
        raise ValueError(f"max_drawdown: unknown kind {kind!r}")

    n = len(levels)
    if n < 1:
        return Metric("max_drawdown", None, 0, formula, inputs, unit="pct",
                      note="n<1: drawdown undefined")
    peak: Optional[float] = None
    mdd = 0.0
    for x in levels:
        if peak is None or x > peak:
            peak = x
        if peak and peak > 0:
            dd = (peak - x) / peak
            if dd > mdd:
                mdd = dd
    return Metric("max_drawdown", mdd, n, formula, inputs, unit="pct")


def hit_rate(graded: Sequence[Tuple[Optional[str], float]], *, low_n: int = 5) -> Metric:
    """Fraction of directional bets that were correct. graded = [(signal, ret), ...].

    Uses lib.memory.is_hit to grade; Hold/ungradeable calls are dropped. None when
    there are no gradeable bets.
    """
    gradeable = [(s, r) for (s, r) in graded if is_hit(s, r) is not None]
    n = len(gradeable)
    hits = sum(1 for (s, r) in gradeable if is_hit(s, r))
    value = (hits / n) if n else None
    return Metric(
        "hit_rate", value, n, "hits/gradeable via is_hit(signal,ret)",
        f"hits={hits}/{n}", unit="pct",
        note=("" if n else "no directional bets to grade"), low_confidence=(n < low_n),
    )


def win_loss_stats(returns: Sequence[float], *, low_n: int = 5) -> Dict[str, Metric]:
    """avg_win, avg_loss, profit_factor, win_rate over RAW returns (sign-based).

    win_rate here is return-SIGN based (distinct from directional hit_rate, which
    respects bullish/bearish intent). profit_factor is None when there are no losses.
    """
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    nwl = len(wins) + len(losses)
    sum_w = sum(wins)
    sum_l = sum(losses)
    return {
        "win_rate": Metric(
            "win_rate", (len(wins) / nwl) if nwl else None, nwl, "wins/(wins+losses)",
            f"wins={len(wins)},losses={len(losses)}", unit="pct", low_confidence=(nwl < low_n),
        ),
        "avg_win": Metric(
            "avg_win", (sum_w / len(wins)) if wins else None, len(wins), "mean(r>0)",
            f"wins={_pct_list(wins)}", unit="signed_pct",
        ),
        "avg_loss": Metric(
            "avg_loss", (sum_l / len(losses)) if losses else None, len(losses), "mean(r<0)",
            f"losses={_pct_list(losses)}", unit="signed_pct",
        ),
        "profit_factor": Metric(
            "profit_factor", (sum_w / abs(sum_l)) if losses else None, nwl,
            "sum(wins)/abs(sum(losses))",
            f"sum_wins={_fmt_pct(sum_w)},sum_losses={_fmt_pct(sum_l) if losses else '0%'}",
            unit="ratio", note=("" if losses else "no losing trades: profit factor undefined"),
            low_confidence=(nwl < low_n),
        ),
    }


def rolling_window_trend(
    returns: Sequence[float],
    graded: Sequence[Tuple[Optional[str], float]],
    *, window: int = 10,
) -> Dict[str, object]:
    """Compare the last ``window`` resolved decisions vs the prior ``window``.

    Returns per-window hit_rate + per-trade Sharpe Metrics and their deltas, plus
    the effective window (shrunk symmetrically when fewer than 2*window samples
    exist). ``returns`` and ``graded`` must be the SAME chronological series.
    This is the "improvement over time" signal.
    """
    n = len(returns)
    w = window
    note = ""
    if n < 2 * window:
        w = max(1, n // 2)
        if w < window:
            note = f"window shrunk to {w} (have {n}, need {2 * window})"

    recent_r = list(returns[-w:]) if w else []
    prior_r = list(returns[-2 * w:-w]) if w else []
    recent_g = list(graded[-w:]) if w else []
    prior_g = list(graded[-2 * w:-w]) if w else []

    recent_hit = hit_rate(recent_g, low_n=1)
    prior_hit = hit_rate(prior_g, low_n=1)
    recent_sharpe = sharpe(recent_r, low_n=2)
    prior_sharpe = sharpe(prior_r, low_n=2)

    def _delta(a: Metric, b: Metric) -> Optional[float]:
        return (a.value - b.value) if (a.value is not None and b.value is not None) else None

    return {
        "window": w,
        "note": note,
        "n_recent": len(recent_r),
        "n_prior": len(prior_r),
        "recent_hit": recent_hit,
        "prior_hit": prior_hit,
        "hit_delta": _delta(recent_hit, prior_hit),
        "recent_sharpe": recent_sharpe,
        "prior_sharpe": prior_sharpe,
        "sharpe_delta": _delta(recent_sharpe, prior_sharpe),
    }


# --- deterministic guidance derivation --------------------------------------
# Maps the computed metrics to ONE calibration tier via EXPLICIT thresholds, and
# renders it WITH its proof (thresholds crossed + the actual numbers). This is
# GUIDANCE injected as agent context only — it never changes sizing in signals.py.

@dataclass(frozen=True)
class GuidanceThresholds:
    low_confidence_min_n: int = 5
    hit_rate_elevated: float = 0.60
    hit_rate_reduced: float = 0.40
    sharpe_elevated: float = 0.50
    sharpe_reduced: float = 0.0
    rolling_window: int = 10
    periods_per_year: float = 50.0


@dataclass(frozen=True)
class Guidance:
    scope: str
    tier: str
    text: str   # the full, auditable proof line

    def render(self) -> str:
        return self.text


_DISCLAIMER = "context for your reasoning ONLY — it does NOT change position sizing"


def derive_guidance(scope_label: str, metrics: Dict[str, Metric],
                    thresholds: GuidanceThresholds) -> Guidance:
    hit = metrics.get("hit_rate")
    shp = metrics.get("sharpe")
    hv = hit.value if hit else None
    sv = shp.value if shp else None
    hn = hit.n if hit else 0
    sn = shp.n if shp else 0
    t = thresholds

    if hv is None or sv is None or hn < t.low_confidence_min_n or sn < t.low_confidence_min_n:
        tier = "INSUFFICIENT DATA"
        reason = (f"hit_rate n={hn}, sharpe n={sn}; need n>={t.low_confidence_min_n} "
                  f"and both computable — treat history as non-informative")
    elif hv >= t.hit_rate_elevated and sv >= t.sharpe_elevated:
        tier = "ELEVATED conviction"
        reason = (f"hit_rate={hv * 100:.0f}%>={t.hit_rate_elevated * 100:.0f}% AND "
                  f"sharpe={sv:.3f}>={t.sharpe_elevated:.2f}")
    elif hv <= t.hit_rate_reduced or sv <= t.sharpe_reduced:
        tier = "REDUCED conviction"
        reason = (f"hit_rate={hv * 100:.0f}%<={t.hit_rate_reduced * 100:.0f}% OR "
                  f"sharpe={sv:.3f}<={t.sharpe_reduced:.2f}")
    else:
        tier = "NORMAL conviction"
        reason = (f"hit_rate={hv * 100:.0f}% in [{t.hit_rate_reduced * 100:.0f}%,"
                  f"{t.hit_rate_elevated * 100:.0f}%); sharpe={sv:.3f} in "
                  f"[{t.sharpe_reduced:.2f},{t.sharpe_elevated:.2f})")

    text = (f"GUIDANCE ({scope_label}): {tier}. {reason}. "
            f"Thresholds: elevated>={t.hit_rate_elevated * 100:.0f}%/{t.sharpe_elevated:.2f}, "
            f"reduced<={t.hit_rate_reduced * 100:.0f}%/{t.sharpe_reduced:.2f}. "
            f"NOTE: {_DISCLAIMER}.")
    return Guidance(scope=scope_label, tier=tier, text=text)


# --- continual-learning helpers (Stage 4) -----------------------------------
# Used by lib.learn to score holdings vs thesis against the goal gap. Same Metric
# discipline (value + N + formula + the actual inputs). Analysis-side; never
# imported by lib.signals.

def sustained_underperformance(returns: Sequence[float], *, window: int = 8, min_n: int = 5,
                               hit_floor: float = 0.40, mean_floor: float = 0.0) -> Metric:
    """Rolling-window underperformance flag — NOT a single bad call.

    Over the most recent ``window`` resolved decisions (a 'hit' = a positive
    directional return), value is 1.0 iff hit_rate < hit_floor AND mean < mean_floor,
    else 0.0. value is None (INSUFFICIENT -> caller KEEPs) when fewer than ``min_n``
    decisions exist, so one bad week can never evict a sleeve.
    """
    recent = list(returns)[-window:]
    n = len(recent)
    if n < min_n:
        return Metric("sustained_underperf", None, n,
                      "1 iff hit_rate<floor AND mean<floor over last W; None when N<min_n",
                      f"have N={n}, need >={min_n}", note="INSUFFICIENT (N<min_n)",
                      low_confidence=True)
    mean = sum(recent) / n
    hit_rate = sum(1 for r in recent if r > 0) / n
    underperforming = hit_rate < hit_floor and mean < mean_floor
    return Metric("sustained_underperf", 1.0 if underperforming else 0.0, n,
                  "1 iff hit_rate<floor AND mean<floor over last W (else 0)",
                  f"hit_rate={hit_rate * 100:.0f}%, mean={mean * 100:+.1f}%, "
                  f"floors=({hit_floor * 100:.0f}%,{mean_floor * 100:+.1f}%), W={window}")


def contribution_vs_thesis(mean_return: Optional[float], target_weight: float) -> Metric:
    """Signed contribution of a holding to the book = mean directional return *
    its target weight. Positive pulls the book toward the goal; negative drags it."""
    if mean_return is None:
        return Metric("contribution", None, 0, "mean_return * weight/100",
                      "mean_return=n/a", unit="signed_pct", note="no resolved outcomes")
    contrib = mean_return * (target_weight / 100.0)
    return Metric("contribution", contrib, 0, "mean_return * weight/100",
                  f"mean_return={mean_return * 100:+.1f}% * weight={target_weight:.0f}%",
                  unit="signed_pct")
