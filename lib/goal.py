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


def time_weighted_return_pct(
    start_equity: Optional[float], start_date: str, points: list, flows: list,
) -> Optional[float]:
    """Chain-linked (time-weighted) return %, stripping external cash flows.

    ``points`` = ``[(trade_date, equity)]`` date-ASC (the baseline equity curve within the
    goal window). ``flows`` = ``[(trade_date, amount)]`` external deposits(+)/withdrawals(-).
    Chains per-interval returns anchored at ``start_equity`` (as of ``start_date``), removing
    each interval's net flow so deposited capital is NOT counted as investment gain::

        r_i = (E_i - flow_in_interval_i) / E_{i-1} ;  TWR = prod(1 + r_i) - 1

    (end-of-period flow convention; a flow lands in interval ``(prev_date, cur_date]``). With
    NO flows this short-circuits to the exact legacy simple return
    ``(E_last/start_equity - 1)*100`` — byte-identical, no float drift. Returns None on bad
    input (start_equity<=0, empty points, None equity, or a non-positive interval-start equity),
    matching the module's divide-by-zero-safe discipline.
    """
    if start_equity is None or start_equity <= 0 or not points:
        return None
    current = points[-1][1]
    if current is None:
        return None
    if not flows:  # nothing external to strip -> exact legacy return (guards against drift)
        return (current / start_equity - 1.0) * 100.0
    # Anchor the chain at start_equity, then walk each baseline; ISO-date strings compare
    # chronologically, so interval bucketing works even when a flow's date is not a baseline day.
    seq = [(start_date, float(start_equity))] + [(d, e) for d, e in points]
    factor = 1.0
    for i in range(1, len(seq)):
        prev_date, prev_eq = seq[i - 1]
        cur_date, cur_eq = seq[i]
        if prev_eq is None or cur_eq is None or prev_eq <= 0:
            return None
        flow = sum(a for d, a in flows if prev_date < d <= cur_date)
        factor *= (cur_eq - flow) / prev_eq
    return (factor - 1.0) * 100.0


def goal_progress(
    start_equity: float, current_equity: Optional[float], target_return_pct: float,
    horizon_months: float, start_date: str, as_of_date: str, *,
    benchmark_annual_pct: float = 0.0, cumulative_return_pct: Optional[float] = None,
) -> Optional[dict]:
    """Full progress snapshot vs the glidepath and the cash benchmark.

    Returns a dict (or None on bad inputs). Keys: elapsed_days, glidepath_target_value,
    current_equity, cumulative_return_pct, ahead_behind_pct (return-% gap to the
    glidepath), benchmark_value, benchmark_return_pct, alpha_vs_benchmark_pct, regime.

    ``cumulative_return_pct``: pass the deposit-adjusted (time-weighted) return when the
    caller has the equity curve + external flows (see ``compute_from_ledger``); leave None
    to use the legacy simple return ``(current_equity/start_equity - 1)*100`` (byte-identical
    for existing scalar callers). ``ahead_behind_pct`` and ``alpha`` inherit the adjustment.
    """
    if start_equity <= 0 or current_equity is None:
        return None
    elapsed = _parse_days(start_date, as_of_date)
    target_value = glidepath_target_value(start_equity, target_return_pct, horizon_months, elapsed)
    if elapsed is None or target_value is None:
        return None
    # Deposits/withdrawals are external capital, not gains: use the supplied deposit-adjusted
    # return when given, else the legacy simple return.
    if cumulative_return_pct is None:
        cumulative_return_pct = (current_equity / start_equity - 1.0) * 100.0
    # ahead/behind = the RETURN gap to the glidepath (return-space, so it inherits the deposit
    # adjustment). Algebraically identical to the old (current_equity - target_value)/start_equity
    # *100 when cumulative_return_pct is the simple return (verified byte-identical over a grid).
    glidepath_return_pct = (target_value / start_equity - 1.0) * 100.0
    ahead_behind_pct = cumulative_return_pct - glidepath_return_pct
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


def _goal_window(series: list, flows_raw: list, start_date: str):
    """Slice the baseline curve + recorded flows to the goal window.

    Returns ``(points, flows)``:
    - ``points`` = ``[(trade_date, baseline_equity)]`` for baselines ON OR AFTER
      ``start_date`` (``>=``) — the TWR chain is anchored at ``start_equity@start_date``,
      so the start-date baseline itself is a valid interval endpoint.
    - ``flows`` = ``[(trade_date, amount)]`` for external flows STRICTLY AFTER
      ``start_date`` (``>``) — a flow dated exactly on the anchor is part of the seed,
      not an in-window strip.
    Shared by ``compute_from_ledger`` and ``suggest_flows_from_ledger`` so the two
    boundary operators can never drift apart. Pure; order is preserved from the inputs.
    """
    sd = str(start_date)
    points = [(str(r.get("trade_date")), r.get("baseline_equity"))
              for r in (series or []) if str(r.get("trade_date")) >= sd]
    flows = [(str(f.get("trade_date")), float(f.get("amount") or 0.0))
             for f in (flows_raw or []) if str(f.get("trade_date")) > sd]
    return points, flows


def detect_external_flows(points: list, flows: list, *, min_pct: float, min_abs: float) -> list:
    """Candidate external cash flows inferred from day-over-day baseline jumps.

    ``points`` = ``[(date, equity)]`` date-ASC; ``flows`` = ``[(date, amount)]`` already
    recorded. Buys/sells don't change total equity (cash<->positions is internal), so a
    day-over-day jump beyond a market-move band is very likely an external deposit/withdrawal.
    A consecutive pair ``(prev, cur)`` is flagged when BOTH ``abs(pct) >= min_pct`` and
    ``abs(delta) >= min_abs`` AND no recorded flow already falls in ``(prev_date, cur_date]``
    (the exact bucketing ``time_weighted_return_pct`` uses to strip flows, so a recorded flow
    suppresses its own candidate). Returns ``[{trade_date, amount, prev_equity, cur_equity,
    pct, direction}]`` date-ASC.

    Pure; divide-by-zero-safe (a pair with ``prev_eq`` None/``<=0`` or ``cur_eq`` None is
    skipped, never raised). This is the LIBERAL set surfaced for human review; auto-writes
    are additionally gated by ``confirmable_flows``.
    """
    out = []
    pts = points or []
    for i in range(1, len(pts)):
        prev_date, prev_eq = pts[i - 1]
        cur_date, cur_eq = pts[i]
        if prev_eq is None or cur_eq is None or prev_eq <= 0:
            continue
        delta = cur_eq - prev_eq
        pct = delta / prev_eq * 100.0
        if abs(pct) < min_pct or abs(delta) < min_abs:
            continue
        if any(prev_date < str(d) <= cur_date for d, _ in (flows or [])):
            continue  # a recorded flow already explains this interval
        out.append({
            "trade_date": cur_date,
            "amount": round(delta, 2),
            "prev_equity": round(float(prev_eq), 2),
            "cur_equity": round(float(cur_eq), 2),
            "pct": round(pct, 2),
            "direction": "deposit" if delta > 0 else "withdrawal",
        })
    return out


def confirmable_flows(points: list, candidates: list, *, confirm_days: int) -> list:
    """The subset of ``candidates`` safe to AUTO-write (vs merely surface for a human).

    A candidate (a jump ending on its ``trade_date``) is confirmable only if it has SETTLED:
    (1) at least ``confirm_days`` baselines exist strictly AFTER its ``trade_date`` — an age
    gate that NEVER auto-acts on the most-recent/same-session jump (so a mid-session regime
    flip from a not-yet-confirmed move can't happen), AND
    (2) the new level PERSISTED past the interval midpoint ``(prev_eq+cur_eq)/2``: for a
    deposit ``min(later equities) >= midpoint``; for a withdrawal ``max(...) <= midpoint``.
    A transient spike that reverts toward ``prev_eq`` fails (2) and is never auto-written.

    Pure; returns the confirmable candidates in input order. ``points`` is the same baseline
    window ``detect_external_flows`` ran on.
    """
    pts = points or []
    out = []
    for c in (candidates or []):
        cur_date = c.get("trade_date")
        prev_eq = c.get("prev_equity")
        cur_eq = c.get("cur_equity")
        if cur_date is None or prev_eq is None or cur_eq is None:
            continue
        later = [e for (d, e) in pts if str(d) > str(cur_date) and e is not None]
        if len(later) < confirm_days or not later:
            continue  # not yet settled (needs >= confirm_days later baselines, min 1)
        midpoint = (float(prev_eq) + float(cur_eq)) / 2.0
        if float(c.get("amount") or 0.0) > 0:  # deposit: elevated level must hold
            if min(later) >= midpoint:
                out.append(c)
        else:  # withdrawal: reduced level must hold
            if max(later) <= midpoint:
                out.append(c)
    return out


def suggest_flows_from_ledger(led, goal_row: Optional[dict], *,
                              min_pct: float, min_abs: float) -> list:
    """Read-only: suspected external flows from the ledger's baseline equity curve.

    Duck-typed on ``led`` (``baseline_equity_series()`` + ``cash_flows()``) exactly like
    ``compute_from_ledger``; reads NOTHING but the equity curve, recorded flows, and the
    goal — never trading limits or the broker. Returns the LIBERAL candidate set
    (``detect_external_flows``) over the goal window; ``[]`` on no goal / no series.
    """
    if not goal_row:
        return []
    series = led.baseline_equity_series() or []
    if not series:
        return []
    start_date = goal_row.get("start_date")
    if not start_date:
        return []
    flows_raw = led.cash_flows() if hasattr(led, "cash_flows") else []
    points, flows = _goal_window(series, flows_raw, start_date)
    return detect_external_flows(points, flows, min_pct=min_pct, min_abs=min_abs)


def compute_from_ledger(led, goal_row: Optional[dict]) -> Optional[dict]:
    """Thin read-only wrapper: baseline equity curve + external flows + the active goal.

    Duck-typed on `led` (anything with baseline_equity_series() and cash_flows()) so goal.py
    need not import lib.ledger. Read-only: it reads the equity curve + recorded external cash
    flows + the goal, never trading limits or the broker. Deposit-adjusts the return so
    external capital isn't miscounted as gain; with no flows recorded it is byte-identical to
    the legacy simple return. None when there's no goal or no equity history.
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
    start_date = str(start_date)
    # Deposit-adjust the return with recorded external flows (best-effort; cash_flows() is
    # duck-typed like baseline_equity_series). Windowed via _goal_window (shared with
    # suggest_flows_from_ledger so the >=/> boundary rule can never drift between them).
    flows_raw = led.cash_flows() if hasattr(led, "cash_flows") else []
    points, flows = _goal_window(series, flows_raw, start_date)
    twr = time_weighted_return_pct(float(start_equity), start_date, points, flows)
    return goal_progress(
        start_equity=float(start_equity),
        current_equity=latest.get("baseline_equity"),
        target_return_pct=float(target_return_pct),
        horizon_months=float(horizon_months),
        start_date=start_date,
        as_of_date=str(latest.get("trade_date")),
        benchmark_annual_pct=float(goal_row.get("benchmark_annual_pct") or 0.0),
        cumulative_return_pct=twr,
    )
