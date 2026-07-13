"""Long-horizon trend/risk guideposts for the deep-research brain.

PURPOSE: feed the EVE brain a long-horizon, risk-adjusted view of a NAME so it
can follow big multi-month/multi-year trends instead of daily noise. This is
CONTEXT for the model's reasoning ONLY — it is pure data + deterministic math
and NEVER feeds ``lib.signals`` sizing/clamps (a boundary test in
tests/test_units.py enforces that). The wall: this module reads prices only;
never the broker, the ledger, or trading limits.

Everything here is PURE (side-effect-free) so it is unit-tested directly on
hand-built price series. ``fetch_trend`` (in quiver_eve/run/quill_data.py) is
the thin I/O wrapper that pulls a yfinance series and calls these helpers, then
renders the metrics as markdown text the model reads.

Metrics (each auditable, with N):
  - multi-horizon total return (1y, 2y, 3y) + 6m + 12-1 momentum
  - annualized Sharpe + Sortino + Calmar (CAGR / max DD) over the window
  - drawdown: current depth, max depth, max DURATION (days underwater)
  - trend strength: ADX(14) + DI+/DI- (Wilder, hand-rolled so it is unit-testable
    and not at the mercy of stockstats' column-name parser quirks)
  - MA structure: 50/200 SMA slope + golden/death-cross state, % above 200d,
    % off 52w high
  - trend_regime: ONE deterministic label (UPTREND/DOWNTREND/RANGE/BREAKOUT)
    derived from ADX + MA-slope + 200d position (named ``trend_regime`` to NOT
    shadow the macro ``regime_label`` in lib.strategy / lib.goal)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd


# --- pure price-series helpers ----------------------------------------------


def _returns(prices: Sequence[float]) -> List[float]:
    """Simple period-to-period returns from a price series (oldest->newest)."""
    p = [float(x) for x in prices if x and x == x]  # drop None/NaN
    return [(p[i] / p[i - 1]) - 1.0 for i in range(1, len(p)) if p[i - 1]]


def total_return(prices: Sequence[float], lookback_days: int) -> Optional[float]:
    """Return over the last `lookback_days` rows, or None when the series is too SHORT
    to cover the horizon. The old guard `min(lookback_days+1, 2)` was always 2, so a
    thin series never returned None — it clamped `i` to 0 and reported the
    SINCE-INCEPTION return mislabeled as (e.g.) a 3-year return, making 6m=1y=2y=3y
    identical on young names. Mirror momentum_12_1: need a real bar `lookback_days`
    rows back."""
    p = [float(x) for x in prices if x and x == x]
    if len(p) < lookback_days + 1:
        return None
    start = p[len(p) - 1 - lookback_days]
    if not start:
        return None
    return (p[-1] / start) - 1.0


def momentum_12_1(prices: Sequence[float]) -> Optional[float]:
    """Classic trend factor: 12m return minus the most-recent 1m return
    (12-1). Positive = sustained uptrend, not just a recent pop. None if the
    series is too short for a 12-month (>=252-row) lookback."""
    p = [float(x) for x in prices if x and x == x]
    if len(p) < 253:
        return None
    r12 = (p[-1] / p[-252]) - 1.0 if p[-252] else None
    r1 = (p[-1] / p[-22]) - 1.0 if p[-22] else None
    if r12 is None or r1 is None:
        return None
    return r12 - r1


def _mean_stdev(xs: Sequence[float]) -> tuple[Optional[float], Optional[float]]:
    n = len(xs)
    if n == 0:
        return (None, None)
    m = sum(xs) / n
    if n < 2:
        return (m, None)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return (m, math.sqrt(var))


def annualized_sharpe(returns: Sequence[float], *, periods_per_year: float = 252.0,
                      rf: float = 0.0) -> Optional[float]:
    """Annualized Sharpe of a return series (excess over rf). None when n<2 or
    zero variance. Returns are period returns (daily rows here)."""
    if len(returns) < 2:
        return None
    excess = [r - rf for r in returns]
    m, sd = _mean_stdev(excess)
    if m is None or sd is None or sd == 0:
        return None
    return (m / sd) * math.sqrt(periods_per_year)


def annualized_sortino(returns: Sequence[float], *, periods_per_year: float = 252.0,
                       rf: float = 0.0) -> Optional[float]:
    """Annualized Sortino = mean(excess)/downside_dev * sqrt(ppy).
    downside_dev = sqrt(mean(min(excess,0)^2)). None when no downside or n<1."""
    if not returns:
        return None
    excess = [r - rf for r in returns]
    m = sum(excess) / len(excess)
    downside = [e for e in excess if e < 0]
    if not downside:
        return None
    dd = math.sqrt(sum(e * e for e in downside) / len(downside))
    if dd == 0:
        return None
    return (m / dd) * math.sqrt(periods_per_year)


def cagr(prices: Sequence[float], *, periods_per_year: float = 252.0) -> Optional[float]:
    """Compound annual growth rate over the full series. None when <2 prices."""
    p = [float(x) for x in prices if x and x == x]
    if len(p) < 2 or not p[0]:
        return None
    years = (len(p) - 1) / periods_per_year
    if years <= 0:
        return None
    return ((p[-1] / p[0]) ** (1.0 / years)) - 1.0


# --- drawdown ---------------------------------------------------------------


@dataclass(frozen=True)
class DrawdownStats:
    current_depth: Optional[float]   # fraction off the rolling peak (>=0)
    max_depth: Optional[float]       # worst peak-to-trough (>=0)
    max_duration_days: Optional[int] # longest run underwater (rows)


def drawdown_stats(prices: Sequence[float]) -> DrawdownStats:
    """Peak-to-trough drawdown depth + DURATION over a price series.

    duration = the longest consecutive run of rows spent below the running
    peak (days underwater). Trend-followers live with drawdowns; duration +
    depth matter more than a snapshot. All None when <1 price.
    """
    p = [float(x) for x in prices if x and x == x]
    if not p:
        return DrawdownStats(None, None, None)
    peak = p[0]
    mdd = 0.0
    cur_dd = 0.0
    cur_run = 0
    max_run = 0
    for x in p:
        if x > peak:
            peak = x
        if peak > 0:
            dd = (peak - x) / peak
        else:
            dd = 0.0
        cur_dd = dd
        if dd > mdd:
            mdd = dd
        if dd > 0:
            cur_run += 1
            if cur_run > max_run:
                max_run = cur_run
        else:
            cur_run = 0
    return DrawdownStats(
        current_depth=cur_dd if cur_dd > 0 else 0.0,
        max_depth=mdd,
        max_duration_days=max_run,
    )


def calmar(prices: Sequence[float], *, periods_per_year: float = 252.0) -> Optional[float]:
    """Calmar = CAGR / max drawdown. None when no drawdown or CAGR undefined."""
    g = cagr(prices, periods_per_year=periods_per_year)
    dd = drawdown_stats(prices).max_depth
    if g is None or dd is None or dd == 0:
        return None
    return g / dd


# --- ADX / DI (Wilder, hand-rolled + unit-testable) --------------------------


@dataclass(frozen=True)
class ADXStats:
    adx: Optional[float]   # 14-period ADX (trend strength; >~25 = trending)
    plus_di: Optional[float]
    minus_di: Optional[float]


def adx_di(high: Sequence[float], low: Sequence[float], close: Sequence[float],
           *, period: int = 14) -> ADXStats:
    """Wilder's ADX + DI+/DI-. Hand-rolled (not stockstats) so the math is
    auditable and unit-testable on hand-built series. Returns the LAST values;
    None when the series is too short (< 2*period)."""
    h = [float(x) for x in high if x and x == x]
    l = [float(x) for x in low if x and x == x]
    c = [float(x) for x in close if x and x == x]
    n = min(len(h), len(l), len(c))
    if n < 2 * period:
        return ADXStats(None, None, None)
    h, l, c = h[:n], l[:n], c[:n]

    # True range
    tr = [0.0]
    for i in range(1, n):
        tr.append(max(h[i] - l[i],
                      abs(h[i] - c[i - 1]),
                      abs(l[i] - c[i - 1])))

    # Directional movement
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, n):
        up = h[i] - h[i - 1]
        down = l[i - 1] - l[i]
        if up > down and up > 0:
            plus_dm.append(up)
        else:
            plus_dm.append(0.0)
        if down > up and down > 0:
            minus_dm.append(down)
        else:
            minus_dm.append(0.0)

    # Wilder smoothing (running sum of `period` then recurse)
    def wilder_sm(values: List[float]) -> List[float]:
        out = [math.nan] * n
        s = sum(values[1:period + 1])
        out[period] = s
        for i in range(period + 1, n):
            s = s - (s / period) + values[i]
            out[i] = s
        return out

    atr_ = wilder_sm(tr)
    pdm_ = wilder_sm(plus_dm)
    mdm_ = wilder_sm(minus_dm)

    plus_di = [math.nan] * n
    minus_di = [math.nan] * n
    dx = [math.nan] * n
    for i in range(period, n):
        if atr_[i]:
            plus_di[i] = 100 * (pdm_[i] / atr_[i])
            minus_di[i] = 100 * (mdm_[i] / atr_[i])
            didiff = abs(plus_di[i] - minus_di[i])
            disum = plus_di[i] + minus_di[i]
            dx[i] = 100 * (didiff / disum) if disum else 0.0

    # ADX = Wilder smoothing of DX
    first_dx = [x for x in dx[period:2 * period] if not math.isnan(x)]
    if len(first_dx) < period:
        return ADXStats(None, plus_di[-1] if not math.isnan(plus_di[-1]) else None,
                        minus_di[-1] if not math.isnan(minus_di[-1]) else None)
    s = sum(first_dx)
    adx = [math.nan] * n
    adx[2 * period - 1] = s / period
    for i in range(2 * period, n):
        s = s - (s / period) + dx[i]
        adx[i] = s / period

    def _last(xs: List[float]) -> Optional[float]:
        for v in reversed(xs):
            if not math.isnan(v):
                return v
        return None

    return ADXStats(_last(adx), _last(plus_di), _last(minus_di))


# --- MA structure + regime --------------------------------------------------


@dataclass(frozen=True)
class MAStats:
    sma50: Optional[float]
    sma200: Optional[float]
    sma50_slope_20d: Optional[float]   # slope of 50d SMA over last 20 rows (rising/falling)
    sma200_slope_60d: Optional[float]
    above_200d_pct: Optional[float]    # current price vs 200d SMA (signed %)
    golden_cross: Optional[bool]      # sma50 > sma200 (True) else death (False); None if N/A
    pct_off_52w_high: Optional[float]  # how far below the 52w high (>=0; 0 = at the high)


def ma_stats(prices: Sequence[float]) -> MAStats:
    p = pd.Series([float(x) for x in prices if x and x == x], dtype="float64")
    if len(p) < 5:
        return MAStats(None, None, None, None, None, None, None)
    sma50 = p.rolling(50, min_periods=1).mean() if len(p) >= 50 else None
    sma200 = p.rolling(200, min_periods=1).mean() if len(p) >= 200 else None
    s50 = float(sma50.iloc[-1]) if sma50 is not None and not math.isnan(sma50.iloc[-1]) else None
    s200 = float(sma200.iloc[-1]) if sma200 is not None and not math.isnan(sma200.iloc[-1]) else None

    def _slope(series, window):
        if series is None or len(series) < window + 1:
            return None
        a = series.iloc[-window - 1]
        b = series.iloc[-1]
        if math.isnan(a) or math.isnan(b) or a == 0:
            return None
        return (b - a) / a  # fractional change over the window

    s50_slope = _slope(sma50, 20) if sma50 is not None else None
    s200_slope = _slope(sma200, 60) if sma200 is not None else None

    above_200 = None
    if s200 and s200 == s200:
        above_200 = (p.iloc[-1] - s200) / s200

    gc = None
    if s50 is not None and s200 is not None:
        gc = s50 > s200

    # 52w high: ~252 trading days
    lookback = min(252, len(p))
    window = p.iloc[-lookback:]
    hi = float(window.max())
    pct_off = None
    if hi > 0:
        pct_off = (hi - p.iloc[-1]) / hi

    return MAStats(s50, s200, s50_slope, s200_slope, above_200, gc, pct_off)


# Regime thresholds (explicit, deterministic, single source of truth here).
ADX_TRENDING = 25.0      # ADX >= this = a real trend, not chop
ADX_VERY_STRONG = 35.0
REGIME_RANGE_ADX = 20.0  # below this = range/no trend
MIN_ANNUALIZED_ROWS = 60  # min daily returns before annualized ratios are meaningful (~3mo)


def trend_regime(adx: Optional[float], ma: MAStats) -> str:
    """ONE deterministic label. ``trend_regime`` (named to avoid shadowing the
    macro regime_label). Rules (explicit, auditable, directional-trend-first):
      - ADX < 20 -> RANGE (no real trend strength)
      - directional trend (ADX >= 20, position + cross + slope agree) -> UPTREND / DOWNTREND
      - BREAKOUT: ADX >= 35 AND at a 52w extreme (pct_off < 3%) BUT the directional
        trend is NOT already established (slope and position disagree) — i.e. a FRESH
        break, not a mature trend that simply happens to be at its highs
      - otherwise -> RANGE
    A mature persistent uptrend that sits at its highs stays UPTREND (directional
    agreement wins over the at-high flag). Falls back to RANGE when inputs are
    insufficient (None). Never raises.
    """
    if adx is None:
        return "RANGE"
    if adx < REGIME_RANGE_ADX:
        return "RANGE"
    above = ma.above_200d_pct
    gc = ma.golden_cross
    s50_slope = ma.sma50_slope_20d
    # directional trend: position, cross, AND slope all agree
    up = (above is not None and above > 0) and (gc is not False) and \
        (s50_slope is None or s50_slope > 0)
    down = (above is not None and above < 0) and (gc is not True) and \
        (s50_slope is None or s50_slope < 0)
    if up:
        return "UPTREND"
    if down:
        return "DOWNTREND"
    # directional signals disagree but strength is very high + at an extreme -> fresh BREAKOUT
    at_high = ma.pct_off_52w_high is not None and ma.pct_off_52w_high < 0.03
    at_low = above is not None and above < -0.03
    if adx >= ADX_VERY_STRONG and (at_high or at_low):
        return "BREAKOUT"
    return "RANGE"


# --- the bundle (one compute, reused by the renderer) -----------------------


@dataclass
class TrendBundle:
    n_rows: int
    ret_6m: Optional[float]
    ret_1y: Optional[float]
    ret_2y: Optional[float]
    ret_3y: Optional[float]
    mom_12_1: Optional[float]
    sharpe: Optional[float]
    sortino: Optional[float]
    calmar: Optional[float]
    cagr_: Optional[float]
    dd_current: Optional[float]
    dd_max: Optional[float]
    dd_max_duration_days: Optional[int]
    adx: Optional[float]
    plus_di: Optional[float]
    minus_di: Optional[float]
    trend_regime: str
    ma: MAStats


def trend_metrics(prices: Sequence[float], *, high: Optional[Sequence[float]] = None,
                  low: Optional[Sequence[float]] = None,
                  periods_per_year: float = 252.0) -> TrendBundle:
    """Compute the full long-horizon bundle from a price (OHLC) series.

    `prices` is the CLOSE series (oldest->newest); `high`/`low` optional for
    ADX (fall back to close-derived highs/lows if absent, which makes ADX a
    degenerate ~0 — the regime then falls back to RANGE, fail-soft). Pure."""
    p = [float(x) for x in prices if x and x == x]
    n = len(p)
    rets = _returns(p)
    hi = list(high) if high is not None else [x * 1.001 for x in p]  # degenerate fallback
    lo = list(low) if low is not None else [x * 0.999 for x in p]
    adx = adx_di(hi, lo, p)
    ma = ma_stats(p)
    dd = drawdown_stats(p)
    # Annualized ratios (Sharpe/Sortino/Calmar/CAGR) on a tiny sample are noise: on
    # ~20-40 daily returns the sqrt(252) scaling produces confident-looking but
    # meaningless figures (a name down 15% can print a +Sharpe from vol-drag sign
    # artifacts). Gate them behind a minimum sample -> render "n/a" below it.
    have_ratio_n = len(rets) >= MIN_ANNUALIZED_ROWS
    return TrendBundle(
        n_rows=n,
        ret_6m=total_return(p, 126),
        ret_1y=total_return(p, 252),
        ret_2y=total_return(p, 504),
        ret_3y=total_return(p, 756),
        mom_12_1=momentum_12_1(p),
        sharpe=annualized_sharpe(rets, periods_per_year=periods_per_year) if have_ratio_n else None,
        sortino=annualized_sortino(rets, periods_per_year=periods_per_year) if have_ratio_n else None,
        calmar=calmar(p, periods_per_year=periods_per_year) if have_ratio_n else None,
        cagr_=cagr(p, periods_per_year=periods_per_year) if have_ratio_n else None,
        dd_current=dd.current_depth,
        dd_max=dd.max_depth,
        dd_max_duration_days=dd.max_duration_days,
        adx=adx.adx,
        plus_di=adx.plus_di,
        minus_di=adx.minus_di,
        trend_regime=trend_regime(adx.adx, ma),
        ma=ma,
    )


def _pct(x: Optional[float]) -> str:
    return f"{x * 100:+.1f}%" if x is not None else "n/a"


def _pctu(x: Optional[float]) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def _ratio(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "n/a"


def render_trend_report(b: TrendBundle) -> str:
    """Render the bundle as markdown. Uses ``###`` subheadings + bullets ONLY
    (NEVER ``##`` — analyze.py:_split_eve_markdown keys on ``^##\\s+<word>`` and
    a ``## Drawdown`` subheading would flip the section key and silently drop
    the whole trend_report body; enforced by a unit test)."""
    ma = b.ma
    # Coverage label from the ACTUAL row count, not a hard-coded "~3y" — the fetch
    # window is 3y but a newly-listed/spun-off name has far less history, and
    # claiming "~3y" on 5 weeks of data (SPCX N=19) is dishonest.
    _yrs = b.n_rows / 252.0
    if _yrs >= 1.0:
        _cov = f"~{_yrs:.1f}y"
    elif b.n_rows >= 21:
        _cov = f"~{b.n_rows / 21.0:.0f}mo"
    else:
        _cov = f"{b.n_rows} rows"
    lines = [
        f"Long-horizon trend guideposts (N={b.n_rows} rows; yfinance close, {_cov}).",
        f"**Trend regime**: {b.trend_regime}",
        "",
        "### Returns (multi-horizon)",
        f"- 6m: {_pct(b.ret_6m)} | 1y: {_pct(b.ret_1y)} | 2y: {_pct(b.ret_2y)} | 3y: {_pct(b.ret_3y)}",
        f"- 12-1 momentum (trend factor): {_pct(b.mom_12_1)}",
        "",
        "### Risk-adjusted return (annualized)",
        f"- Sharpe: {_ratio(b.sharpe)} | Sortino: {_ratio(b.sortino)} | Calmar: {_ratio(b.calmar)} | CAGR: {_pct(b.cagr_)}",
        "",
        "### Drawdown (depth + duration)",
        f"- current DD: {_pctu(b.dd_current)} | max DD: {_pctu(b.dd_max)} | max duration underwater: {b.dd_max_duration_days if b.dd_max_duration_days is not None else 'n/a'} days",
        "",
        "### Trend strength (ADX 14, Wilder)",
        f"- ADX: {_ratio(b.adx)} | DI+: {_ratio(b.plus_di)} | DI-: {_ratio(b.minus_di)} (ADX>~25 = trending)",
        "",
        "### MA structure",
        f"- 50d SMA: {_ratio(ma.sma50)} | 200d SMA: {_ratio(ma.sma200)}",
        f"- 50d SMA 20-row slope: {_pct(ma.sma50_slope_20d)} | 200d SMA 60-row slope: {_pct(ma.sma200_slope_60d)}",
        f"- vs 200d SMA: {_pct(ma.above_200d_pct)} | cross: {'golden' if ma.golden_cross else ('death' if ma.golden_cross is False else 'n/a')}",
        f"- % off 52w high: {_pctu(ma.pct_off_52w_high)}",
    ]
    return "\n".join(lines)
