"""Pure 15%-goal glidepath + progress-vs-benchmark math.

Proof-bearing and divide-by-zero-safe (lib.risk discipline): bad inputs
(start_equity<=0, horizon<=0, unparseable dates) return None rather than raising
or dividing by zero. The glidepath is LINEAR to +target% over the horizon.

NOTE on the decision wall: account equity is digest-side state. Only the COARSE
regime label (AHEAD / ON-TRACK / BEHIND) from coarse_regime() may cross into the
analysis path (decision D2) — never a raw equity/dollar value. The numeric
fields here feed the digest + goal_tracking ledger, not analyze.py.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

# Days per month for the linear glidepath (365.25 / 12).
_DAYS_PER_MONTH = 30.4375

# Dead-band (equity % points) around the glidepath inside which we call it
# ON-TRACK rather than AHEAD/BEHIND — avoids flip-flopping the coarse label on
# tiny daily noise.
_REGIME_DEADBAND_PCT = 1.0

REGIME_AHEAD = "AHEAD"
REGIME_ON_TRACK = "ON-TRACK"
REGIME_BEHIND = "BEHIND"


def _parse_days(start_date: str, as_of_date: str) -> Optional[float]:
    try:
        d0 = date.fromisoformat(str(start_date))
        d1 = date.fromisoformat(str(as_of_date))
    except (TypeError, ValueError):
        return None
    return float(max(0, (d1 - d0).days))


def glidepath_target_value(
    start_equity: Optional[float], target_return_pct: float,
    horizon_months: Optional[float], elapsed_days: Optional[float],
) -> Optional[float]:
    """The linear glidepath equity target at ``elapsed_days`` into the horizon.

    At elapsed 0 -> start_equity; at the horizon -> start_equity*(1+target). The
    fraction is capped at 1.0 so the target tops out at the goal (it does not
    extrapolate past +target% after the horizon). None on bad inputs.
    """
    if start_equity is None or start_equity <= 0 or horizon_months is None or horizon_months <= 0:
        return None
    if elapsed_days is None or elapsed_days < 0:
        return None
    horizon_days = horizon_months * _DAYS_PER_MONTH
    frac = min(1.0, elapsed_days / horizon_days) if horizon_days > 0 else 0.0
    return start_equity * (1.0 + (target_return_pct / 100.0) * frac)


def coarse_regime(ahead_behind_pct: Optional[float]) -> str:
    """The wall-safe coarse label for the analysis path (decision D2).

    Returns AHEAD / ON-TRACK / BEHIND from the signed ahead/behind equity-% gap,
    with a dead-band so small noise reads as ON-TRACK. None -> ON-TRACK (neutral).
    """
    if ahead_behind_pct is None:
        return REGIME_ON_TRACK
    if ahead_behind_pct > _REGIME_DEADBAND_PCT:
        return REGIME_AHEAD
    if ahead_behind_pct < -_REGIME_DEADBAND_PCT:
        return REGIME_BEHIND
    return REGIME_ON_TRACK


def goal_progress(
    start_equity: float, current_equity: Optional[float], target_return_pct: float,
    horizon_months: float, start_date: str, as_of_date: str, *,
    benchmark_annual_pct: float = 0.0,
) -> Optional[dict]:
    """Full progress snapshot vs the glidepath and the cash benchmark.

    Returns a dict (or None on bad inputs). Keys: elapsed_days, glidepath_target_value,
    current_equity, cumulative_return_pct, ahead_behind_pct (equity-% gap to the
    glidepath), benchmark_value, benchmark_return_pct, alpha_vs_benchmark_pct, regime.
    """
    if start_equity <= 0 or current_equity is None:
        return None
    elapsed = _parse_days(start_date, as_of_date)
    target_value = glidepath_target_value(start_equity, target_return_pct, horizon_months, elapsed)
    if elapsed is None or target_value is None:
        return None
    cumulative_return_pct = (current_equity / start_equity - 1.0) * 100.0
    ahead_behind_pct = (current_equity - target_value) / start_equity * 100.0
    # Linear daily-prorated cash benchmark (decision D12: a constant, no market read).
    bench_value = start_equity * (1.0 + (benchmark_annual_pct / 100.0) * (elapsed / 365.25))
    bench_return_pct = (bench_value / start_equity - 1.0) * 100.0
    return {
        "elapsed_days": elapsed,
        "glidepath_target_value": round(target_value, 2),
        "current_equity": round(current_equity, 2),
        "cumulative_return_pct": round(cumulative_return_pct, 4),
        "ahead_behind_pct": round(ahead_behind_pct, 4),
        "benchmark_value": round(bench_value, 2),
        "benchmark_return_pct": round(bench_return_pct, 4),
        "alpha_vs_benchmark_pct": round(cumulative_return_pct - bench_return_pct, 4),
        "regime": coarse_regime(ahead_behind_pct),
    }


def compute_from_ledger(led, goal_row: Optional[dict]) -> Optional[dict]:
    """Thin read-only wrapper: latest baseline equity + the active goal row.

    Duck-typed on `led` (anything with baseline_equity_series()) so goal.py need
    not import lib.ledger. Read-only: it reads the equity curve + the goal, never
    trading limits or the broker. None when there's no goal or no equity history.
    """
    if not goal_row:
        return None
    series = led.baseline_equity_series() or []
    if not series:
        return None
    latest = series[-1]  # date-ASC
    start_equity = goal_row.get("start_equity")
    target_return_pct = goal_row.get("target_return_pct")
    horizon_months = goal_row.get("horizon_months")
    start_date = goal_row.get("start_date")
    if start_equity is None or target_return_pct is None or horizon_months is None or not start_date:
        return None
    return goal_progress(
        start_equity=float(start_equity),
        current_equity=latest.get("baseline_equity"),
        target_return_pct=float(target_return_pct),
        horizon_months=float(horizon_months),
        start_date=str(start_date),
        as_of_date=str(latest.get("trade_date")),
        benchmark_annual_pct=float(goal_row.get("benchmark_annual_pct") or 0.0),
    )
