"""Pure factor-attribution math: Newey-West HAC OLS + a bottom-up book beta.

WHAT THIS IS FOR. Quiver has never estimated a beta. The per-decision "alpha" it
records is a raw arithmetic excess over a benchmark that implicitly assumes an
exposure of exactly 1.0, and the account-level one is an excess over a cash
constant. Neither separates *exposure* from *selection*. This module supplies the
estimator that can, and nothing else.

WHAT THIS IS NOT. A DIAGNOSTIC, NEVER A GATE. Nothing here returns a verdict, a
pass/fail, a p-value, or a critical value, and nothing here may be wired into
sizing, the daily-loss halt, order placement, the calibration/conviction loop, or
the analysis prompt. Every threshold-shaped number is either a function argument
or a module constant; this module reads no configuration of any kind.

THE SMALL-SAMPLE RULE. A point estimate is unbiased at any n, but its standard
error is not usable until there are enough residual degrees of freedom. Below
RESIDUAL_DF_MIN the Fit still carries every estimate and reports stderr/t/r2 as
None with a stated reason, so a reader never sees a number they could compare to
1.96. This is measured, not stylistic: at n=4 the HAC standard error is ~0.32x
the true sampling sd of the estimator (20k-draw Monte Carlo under a true null),
which turns a nominal 5% test into a ~49% false-positive rate; at n=8 with k=7,
pure Gaussian noise produces seven t-statistics between 4.3 and 11.8.

CONVENTIONS, PINNED.
  * Covariance is the plain sandwich V = (X'X)^-1 S (X'X)^-1 with a Bartlett
    kernel and NO small-sample correction -- i.e. statsmodels
    ``get_robustcov_results(cov_type="HAC", maxlags=L, use_correction=False)``.
    Stata's ``newey`` and statsmodels' standalone ``cov_hac`` instead multiply
    the covariance by n/(n-k), which at n=4, k=2 is a factor of exactly 2.0.
    Tests pin this in both directions against golden constants.
  * Returns are FRACTIONS (0.01 == 1%), never percent. Callers converting from a
    percent-quoted source normalize at the seam.

DEPENDENCIES. numpy only. statsmodels and scipy are deliberately NOT imported:
neither is declared in requirements.lock.txt, and the deploy box builds its venv
from that lock and then installs the package with --no-deps, so importing either
would pass every local test and then fail on the box. The estimator is therefore
implemented directly and verified against statsmodels in the test suite instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Anything numpy can coerce to a float array (list, tuple, ndarray, pandas Series).
ArrayLike = Any

# Minimum residual degrees of freedom (n - k) before any standard error, t-stat
# or r-squared is reported. See "THE SMALL-SAMPLE RULE" above.
RESIDUAL_DF_MIN = 10

# Rank tolerance for declaring the design matrix singular.
_RANK_TOL = 1e-10


@dataclass(frozen=True)
class Fit:
    """One regression result. ``stderr``/``t``/``r2`` are None below the df floor.

    ``estimates`` is always populated (a point estimate is unbiased at any n).
    ``inference_withheld_reason`` is non-empty exactly when inference is withheld.
    """

    estimates: List[float]
    stderr: Optional[List[float]]
    t: Optional[List[float]]
    r2: Optional[float]
    n: int
    k: int
    lag: int
    inference_withheld_reason: str = ""
    names: List[str] = field(default_factory=list)

    @property
    def df_resid(self) -> int:
        return self.n - self.k

    def as_dict(self) -> dict:
        """JSON-ready. Emits no verdict, no threshold, no p-value."""
        cols = self.names or [f"x{i}" for i in range(self.k)]
        coeffs = {}
        for i, nm in enumerate(cols):
            coeffs[nm] = {
                "estimate": self.estimates[i],
                "stderr": None if self.stderr is None else self.stderr[i],
                "t": None if self.t is None else self.t[i],
            }
        return {
            "coefficients": coeffs,
            "n": self.n,
            "k": self.k,
            "df_resid": self.df_resid,
            "lag": self.lag,
            "r2": self.r2,
            "hac_convention": "no_small_sample_correction",
            "inference_withheld_reason": self.inference_withheld_reason,
        }


def newey_west_lag(n: int, k: int = 0) -> int:
    """Automatic Bartlett bandwidth: floor(4 * (n/100)^(2/9)), clamped to [0, n-k-1].

    The rule matches statsmodels' own automatic selection. The clamp matters at
    the sample sizes this module actually sees: an unclamped rule asks for 2 lags
    at n=5, which exceeds the usable span.
    """
    if n <= 0:
        return 0
    raw = int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    ceiling = max(0, n - max(k, 0) - 1)
    return int(max(0, min(raw, ceiling)))


def ols_hac(
    y: ArrayLike,
    X: ArrayLike,
    lag: Optional[int] = None,
    names: Optional[Sequence[str]] = None,
) -> Optional[Fit]:
    """OLS with Newey-West HAC standard errors. Returns None rather than raising.

    ``X`` must already include an intercept column if one is wanted. Returns None
    on non-finite input, on n <= k, or on a rank-deficient design.

    None on a singular design is load-bearing, not defensive: ``np.linalg.pinv``
    on a rank-deficient matrix returns a *plausible-looking* finite answer with
    finite standard errors rather than failing, so an explicit rank check is the
    only thing standing between a degenerate input and a confident wrong number.
    """
    try:
        yv = np.asarray(y, dtype=float).ravel()
        Xv = np.asarray(X, dtype=float)
    except (TypeError, ValueError):
        return None
    if Xv.ndim == 1:
        Xv = Xv.reshape(-1, 1)
    if yv.ndim != 1 or Xv.ndim != 2 or yv.size == 0 or Xv.shape[0] != yv.size:
        return None
    if not (np.all(np.isfinite(yv)) and np.all(np.isfinite(Xv))):
        return None

    n, k = Xv.shape
    if n <= k or k == 0:
        return None

    XtX = Xv.T @ Xv
    # Rank check BEFORE pinv -- see the docstring.
    if np.linalg.matrix_rank(XtX, tol=_RANK_TOL) < k:
        return None

    XtX_inv = np.linalg.pinv(XtX)
    beta = XtX_inv @ Xv.T @ yv
    resid = yv - Xv @ beta
    if not np.all(np.isfinite(beta)):
        return None

    L = newey_west_lag(n, k) if lag is None else int(max(0, min(int(lag), n - 1)))

    # Bartlett-weighted long-run covariance of the score.
    u = Xv * resid[:, None]
    S = u.T @ u
    for j in range(1, L + 1):
        w = 1.0 - j / (L + 1.0)
        G = u[j:].T @ u[:-j]
        S = S + w * (G + G.T)

    cov = XtX_inv @ S @ XtX_inv
    var = np.diag(cov)

    col_names = list(names) if names is not None else [f"x{i}" for i in range(k)]
    if len(col_names) != k:
        col_names = [f"x{i}" for i in range(k)]

    df_resid = n - k
    if df_resid < RESIDUAL_DF_MIN:
        return Fit(
            estimates=[float(b) for b in beta],
            stderr=None, t=None, r2=None,
            n=n, k=k, lag=L, names=col_names,
            inference_withheld_reason=(
                f"residual_df={df_resid} < {RESIDUAL_DF_MIN}; standard errors at this "
                "sample size understate true sampling dispersion, so no standard "
                "error, t-statistic or r-squared is reported"
            ),
        )

    if not np.all(np.isfinite(var)) or np.any(var < 0):
        return Fit(
            estimates=[float(b) for b in beta],
            stderr=None, t=None, r2=None,
            n=n, k=k, lag=L, names=col_names,
            inference_withheld_reason="non-finite or negative HAC variance",
        )

    se = np.sqrt(var)
    # A degenerate (near-zero) standard error yields an unbounded t-statistic --
    # a perfect in-sample fit is not infinite significance. Withhold instead.
    scale = float(np.std(yv)) or 1.0
    if np.any(se <= _RANK_TOL * scale):
        return Fit(
            estimates=[float(b) for b in beta],
            stderr=None, t=None, r2=None,
            n=n, k=k, lag=L, names=col_names,
            inference_withheld_reason="degenerate (near-zero) standard error; fit is effectively exact",
        )

    tss = float(np.sum((yv - yv.mean()) ** 2))
    rss = float(np.sum(resid ** 2))
    r2 = (1.0 - rss / tss) if tss > 0 else None

    return Fit(
        estimates=[float(b) for b in beta],
        stderr=[float(s) for s in se],
        t=[float(b / s) for b, s in zip(beta, se)],
        r2=r2, n=n, k=k, lag=L, names=col_names,
    )


def align(
    lhs: Dict[str, float],
    rhs: Dict[str, Dict[str, float]],
    factors: Sequence[str],
) -> Tuple[List[float], List[List[float]], List[str]]:
    """Inner-join a return series against a factor table on date.

    Returns ``(y, X_without_intercept, dates)`` over dates present in ``lhs`` and
    in ``rhs`` with every requested factor finite. Pure; order is date-ascending.
    """
    y: List[float] = []
    X: List[List[float]] = []
    dates: List[str] = []
    for d in sorted(lhs):
        row = rhs.get(d)
        if not row:
            continue
        try:
            vals = [float(row[f]) for f in factors]
            yi = float(lhs[d])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(np.isfinite(v) for v in vals) or not np.isfinite(yi):
            continue
        y.append(yi)
        X.append(vals)
        dates.append(d)
    return y, X, dates


def with_intercept(X: Sequence[Sequence[float]]) -> List[List[float]]:
    """Prepend a constant column. Kept explicit so no caller assumes one."""
    return [[1.0] + list(row) for row in X]


def detect_weight_scale(weights: Dict[str, float]) -> float:
    """Divisor turning ``weights`` into fractions: 100.0 for percent, 1.0 if already.

    A book quoted in percent sums to ~100 and one quoted in fractions to ~1, so the
    two are unambiguous in practice. The chosen divisor is REPORTED by book_beta
    rather than applied silently: getting it wrong scales the book beta by 100x,
    and a beta of 149.8 is a wrong answer that still looks like a number.
    """
    total = 0.0
    for w in weights.values():
        try:
            total += abs(float(w))
        except (TypeError, ValueError):
            continue
    return 100.0 if total > 1.5 else 1.0


def book_beta(
    weights: Dict[str, float],
    returns_by_ticker: Dict[str, Sequence[float]],
    market: Sequence[float],
    weight_scale: Optional[float] = None,
    market_by_ticker: Optional[Dict[str, Sequence[float]]] = None,
) -> dict:
    """Bottom-up book beta: sum(w_i * beta_i) from each name's own history.

    This is the PRIMARY exposure estimate. Each per-name regression runs over that
    name's full return history against the same market series, so n is in the
    hundreds even when the account's own equity curve has a handful of usable
    points. It needs no equity curve, no cash-flow record, and no data cleaning.

    What it is: the ex-ante *designed* exposure of the book as currently weighted.
    What it is not: a measurement of trading skill, cash drag, or timing.

    Names with no usable series are reported in ``unpriced_weight_pct`` and are
    NEVER renormalized away -- silently rescaling the remaining weights to 100%
    would report the exposure of a book nobody holds.
    """
    mkt = np.asarray(list(market), dtype=float).ravel()
    scale = float(weight_scale) if weight_scale else detect_weight_scale(weights)
    if scale <= 0:
        scale = 1.0
    per_name: Dict[str, dict] = {}
    unpriced: Dict[str, float] = {}
    reasons: Dict[str, str] = {}
    total_w = 0.0
    weighted = 0.0

    for ticker, w in sorted(weights.items()):
        try:
            wf = float(w)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(wf) or wf == 0.0:
            continue
        total_w += wf

        series = returns_by_ticker.get(ticker)
        if series is None:
            unpriced[ticker] = wf
            reasons[ticker] = "no price series available"
            continue
        r = np.asarray(list(series), dtype=float).ravel()
        own = (market_by_ticker or {}).get(ticker)
        m = np.asarray(list(own), dtype=float).ravel() if own is not None else mkt
        if r.size != m.size:
            # REFUSE rather than reconcile by position. Trimming to a common length
            # pairs whichever observations happen to line up at the tail, which for
            # a name that stops early or has an interior gap silently regresses a
            # ticker against the WRONG days: a synthetic name with a true beta of
            # 2.0 trading only the first 30 of 100 market days reports 0.028.
            # Alignment is the caller's job (by DATE) via market_by_ticker.
            unpriced[ticker] = wf
            reasons[ticker] = (f"series length mismatch ({r.size} vs {m.size}); pass a "
                               "date-aligned market series via market_by_ticker")
            continue
        ok = np.isfinite(r) & np.isfinite(m)
        r, m = r[ok], m[ok]
        if r.size < 2:
            unpriced[ticker] = wf
            reasons[ticker] = "fewer than 2 usable observations"
            continue

        fit = ols_hac(r, with_intercept([[v] for v in m]), names=["intercept", "beta"])
        if fit is None:
            unpriced[ticker] = wf
            reasons[ticker] = "estimator refused (degenerate design)"
            continue

        b = fit.estimates[1]
        per_name[ticker] = {
            "weight": wf,
            "beta": b,
            "stderr": None if fit.stderr is None else fit.stderr[1],
            "n": fit.n,
            "inference_withheld_reason": fit.inference_withheld_reason,
        }
        weighted += (wf / scale) * b

    unpriced_w = sum(unpriced.values())
    return {
        "beta_book": (weighted if per_name else None),
        "weight_scale": scale,
        "per_name": per_name,
        "unpriced": unpriced,
        "unpriced_reasons": reasons,
        "unpriced_weight_pct": unpriced_w,
        "priced_weight_pct": total_w - unpriced_w,
        "total_weight_pct": total_w,
        "basis": "ex_ante_designed_exposure_of_static_book",
        "caveat": (
            "Holdings-weighted sum of per-name betas. Does not isolate stock "
            "selection, cash drag, or trade timing. Unpriced weight is reported, "
            "never renormalized away -- so beta_book is the exposure of the priced "
            "portion only and is understated by exactly the unpriced weight."
        ),
    }


# --- equity-series hygiene ---------------------------------------------------
# These run BEFORE any regression. They exist because the real series is dirty in
# four distinct ways, each of which silently corrupts a beta:
#   (1) a corrupt broker snapshot (a spike that immediately reverts),
#   (2) an unrecorded external deposit (a permanent LEVEL SHIFT, which a spike
#       filter cannot catch and which at small n single-handedly sets the slope),
#   (3) a multi-day gap masquerading as a one-day return,
#   (4) a reading captured outside the intended time-of-day window -- including,
#       in the real data, one stamped on a Saturday.

# An interval must span exactly this many trading days to count as a daily return.
MAX_INTERVAL_TRADING_DAYS = 1

# |log move| above which a single interval is treated as an unexplained external
# flow unless order activity accounts for it.
DEFAULT_JUMP_LOG_THRESHOLD = 0.25

# A spike must move at least this far and retrace at least this fraction of the
# move to be classed as a corrupt reading rather than a real drawdown.
_SPIKE_MIN_LOG_MOVE = 0.5
_SPIKE_RETRACE_FRAC = 0.8


def find_spike_reverts(points: Sequence[Tuple[str, float]]) -> List[dict]:
    """Interior points that spike hard and immediately retrace ~all of it.

    A corrupt snapshot looks like 241.90 -> 51.40 -> 242.93: a large move whose
    neighbours agree with each other and not with it. A REAL drawdown does not
    retrace ~fully by the very next observation, so it is preserved -- deleting a
    genuine crash would remove the single most important point in the history.
    """
    out: List[dict] = []
    for i in range(1, len(points) - 1):
        (_, prev), (d, cur), (_, nxt) = points[i - 1], points[i], points[i + 1]
        if not all(isinstance(v, (int, float)) and v and v > 0 for v in (prev, cur, nxt)):
            continue
        down = np.log(cur / prev)
        up = np.log(nxt / cur)
        if abs(down) < _SPIKE_MIN_LOG_MOVE:
            continue
        # Opposite directions, and the retrace undoes most of the move.
        if down * up < 0 and abs(up) >= _SPIKE_RETRACE_FRAC * abs(down):
            out.append({
                "date": d, "reason": "spike_revert", "magnitude": float(cur),
                "neighbours": [float(prev), float(nxt)],
            })
    return out


def clean_equity_points(
    points: Sequence[Tuple[str, float]],
    *,
    in_window: Optional[Dict[str, bool]] = None,
) -> Tuple[List[Tuple[str, float]], List[dict]]:
    """Drop non-positive, out-of-window and spike-revert points.

    ``in_window`` maps trade_date -> whether that row's capture timestamp fell in
    the intended window on a real session; a False entry is dropped. Callers that
    cannot supply it pass None and the check is skipped (and must say so).

    Returns ``(kept, dropped)``; every dropped point carries a date, a reason and
    a magnitude. Nothing is ever dropped silently.
    """
    dropped: List[dict] = []
    staged: List[Tuple[str, float]] = []
    for d, e in points:
        try:
            ev = float(e)
        except (TypeError, ValueError):
            dropped.append({"date": d, "reason": "non_numeric_equity", "magnitude": None})
            continue
        if not np.isfinite(ev) or ev <= 0:
            dropped.append({"date": d, "reason": "non_positive_equity", "magnitude": ev})
            continue
        if in_window is not None and not in_window.get(d, False):
            dropped.append({"date": d, "reason": "capture_outside_window", "magnitude": ev})
            continue
        staged.append((d, ev))

    spikes = {s["date"]: s for s in find_spike_reverts(staged)}
    kept = [(d, e) for d, e in staged if d not in spikes]
    dropped.extend(spikes.values())
    dropped.sort(key=lambda r: str(r.get("date")))
    return kept, dropped


def interval_returns(
    points: Sequence[Tuple[str, float]],
    flows: Optional[Dict[str, float]] = None,
    *,
    trading_days: Optional[Dict[Tuple[str, str], int]] = None,
    jump_log_threshold: float = DEFAULT_JUMP_LOG_THRESHOLD,
) -> Tuple[Dict[str, float], List[dict]]:
    """Per-interval simple returns over consecutive points, with refusals.

    Deliberately NOT built on the cumulative time-weighted return: that returns a
    single scalar and, with no recorded flows, collapses to last/first -- ignoring
    every intermediate point, so a series containing a corrupt reading produces
    exactly the same number as a clean one. A regression needs the series.

    An interval is REFUSED (dropped and reported, never adjusted) when it:
      * spans more than ``MAX_INTERVAL_TRADING_DAYS`` sessions,
      * contains a recorded external cash flow -- at these sample sizes one
        flow-adjusted point is not worth the convention risk, or
      * moves more than ``jump_log_threshold`` in log terms (an unrecorded
        external deposit or withdrawal).

    There is deliberately NO "but there were orders that day" escape hatch. An
    equity order converts cash into stock at equal value and so cannot move total
    equity at all; whitelisting on order activity let the real 2026-06-03 deposit
    (98.03 -> 245.98, +150.9%) through the one guard built to catch it, and that
    single point was the entire top-down beta.

    The compounding convention matches the cumulative time-weighted calculation
    elsewhere in the codebase; a test pins ``prod(1+r) - 1`` against it directly.
    """
    flows = flows or {}
    rets: Dict[str, float] = {}
    dropped: List[dict] = []

    for (d0, e0), (d1, e1) in zip(points, points[1:]):
        if e0 is None or e0 <= 0:
            dropped.append({"date": d1, "reason": "non_positive_prior_equity", "magnitude": e0})
            continue

        span = None if trading_days is None else trading_days.get((d0, d1))
        if span is not None and span > MAX_INTERVAL_TRADING_DAYS:
            dropped.append({"date": d1, "reason": "interval_gap", "magnitude": float(span)})
            continue

        if d1 in flows or d0 in flows:
            dropped.append({"date": d1, "reason": "recorded_cash_flow",
                            "magnitude": float(flows.get(d1, flows.get(d0, 0.0)))})
            continue

        move = float(np.log(e1 / e0))
        if abs(move) > jump_log_threshold:
            dropped.append({"date": d1, "reason": "suspected_unrecorded_flow",
                            "magnitude": float(e1 - e0)})
            continue

        rets[d1] = float(e1 / e0 - 1.0)

    return rets, dropped
