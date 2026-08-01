"""Behavioral scorecard for the quiver brain + wall (Phase 0).

The idea is lifted from the long-horizon-planning benchmark: the actionable findings there were
not the P&L leaderboard, they were the BEHAVIORAL measures — how fast a model updated after a
latent regime shift, whether it trusted a signal before observing it, how its trade size
responded as drawdown deepened. quiver has already paid real money for exactly these
pathologies, so these detectors are written against the bugs that actually happened:

  D1  the "0 bulls in 22 sessions" bearish-brain collapse (fixed 2026-07-24, `016feb2`)
  D3  the SOXX buy-2026-07-14 / sell-2026-07-15 rebalance churn
  D4  a gate that suppresses everything, i.e. an accidental off switch
  D9  the paper's "GPT de-risks into drawdown, Gemini doubles down" measure
  D10 the allocator target oscillation the SOXX RCA blamed the churn on

DESIGN
- **Pure.** Every function takes plain `list[dict]` rows shaped exactly like the ledger's own
  tables, so ONE implementation serves the live ledger, a replay's scratch ledger, and a
  frozen-fixture brain run. No DB handle, no network, no config. (`bench/run_diagnostics.py`
  does the loading.)
- **Stdlib only.** No scipy — a new third-party import would need `requirements.lock.txt`, and
  the deploy box never resolves `pyproject` deps. Spearman is a dozen lines of rank correlation.
- **`LOW_N` is never `GREEN`.** A detector that silently passes on thin data is how a detector
  rots; an under-powered check reports `LOW_N` and is excluded from `red_flags`.

A diagnostic reports `{'value', 'n', 'flag', 'threshold', 'detail'}`. `flag` is one of
`GREEN` / `AMBER` / `RED` / `LOW_N`.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Iterable, Optional, Sequence

GREEN, AMBER, RED, LOW_N = "GREEN", "AMBER", "RED", "LOW_N"

# Signal vocabulary. `Hold` is deliberately NEITHER — it is an abstention, and folding it into
# either side is what makes a bearish book look balanced.
BULL = {"buy", "overweight"}
BEAR = {"sell", "underweight"}


@dataclass(frozen=True)
class Thresholds:
    """Every cutoff, explicit and overridable. No magic numbers inline."""
    # D1 signal balance
    d1_min_n: int = 20
    d1_red_low: float = 0.15
    d1_red_high: float = 0.85
    d1_amber_low: float = 0.25
    d1_amber_high: float = 0.75
    # D2 stance flips
    d2_min_n: int = 20
    d2_red: float = 0.50
    d2_amber: float = 0.35
    # D3 round-trip churn
    d3_max_gap_days: int = 3
    # D4 gate suppression concentration
    d4_min_n: int = 20
    d4_red: float = 0.60
    d4_amber: float = 0.45
    # D5 conviction calibration
    d5_min_n: int = 30
    d5_red: float = 0.0
    d5_amber: float = 0.15
    # D7 holding period (sessions)
    d7_min_n: int = 5
    d7_red_below: float = 2.0
    d7_amber_below: float = 4.0
    # D8 data-quality degradation
    d8_min_n: int = 10
    d8_red: float = 0.25
    d8_amber: float = 0.10
    # D9 trade size vs drawdown (slope of exposure on drawdown depth)
    d9_min_n: int = 10
    d9_red: float = 0.0          # positive slope = adding risk as losses deepen
    # D10 target oscillation
    d10_min_n: int = 10
    d10_red: float = 0.40
    d10_amber: float = 0.25


DEFAULTS = Thresholds()


# ---------------------------------------------------------------- small helpers

def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _direction(signal: Any) -> Optional[str]:
    """bull / bear / None (Hold, ERROR, blank). NEVER guess a direction for an abstention."""
    s = _norm(signal)
    if s in BULL:
        return "bull"
    if s in BEAR:
        return "bear"
    return None


def _f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        return v if v == v else None       # NaN != NaN
    except (TypeError, ValueError):
        return None


def _parse_day(s: Any) -> Optional[_date]:
    try:
        y, m, d = str(s)[:10].split("-")
        return _date(int(y), int(m), int(d))
    except Exception:  # noqa: BLE001 — a malformed date is data, not a crash
        return None


def _result(value, n, flag, threshold, detail="") -> dict:
    return {"value": value, "n": n, "flag": flag, "threshold": threshold, "detail": detail}


def _band(value: float, *, red, amber, low_is_bad: bool) -> str:
    """Two-sided banding helper for 'lower is worse' (low_is_bad) metrics."""
    if low_is_bad:
        if value <= red:
            return RED
        return AMBER if value <= amber else GREEN
    if value >= red:
        return RED
    return AMBER if value >= amber else GREEN


def binom_two_sided_p(k: int, n: int, p: float = 0.5) -> Optional[float]:
    """Exact two-sided binomial p for small n, normal approximation above n=1000.

    Reported alongside D1 so a 3-of-4 bearish stretch is not mistaken for the 0-of-22 collapse
    that actually cost money."""
    if n <= 0 or not (0.0 < p < 1.0):
        return None
    if n > 1000:
        mu, sd = n * p, math.sqrt(n * p * (1 - p))
        if sd == 0:
            return None
        z = abs(k - mu) / sd
        return max(0.0, min(1.0, math.erfc(z / math.sqrt(2))))
    obs = math.comb(n, k) * (p ** k) * ((1 - p) ** (n - k))
    tot = 0.0
    for i in range(n + 1):
        pi = math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
        if pi <= obs * (1 + 1e-12):
            tot += pi
    return max(0.0, min(1.0, tot))


def _ranks(xs: Sequence[float]) -> list[float]:
    """Average ranks, ties shared (required for a correct Spearman on conviction buckets)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j + 2) / 2.0          # 1-based average rank
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Spearman rank correlation. Stdlib only; returns None when undefined (n<3 or no variance)."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def ols_slope(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


# ---------------------------------------------------------------- the detectors

def d1_signal_balance(decisions: Iterable[dict], t: Thresholds = DEFAULTS) -> dict:
    """Bull share of directional calls. THE "0 bulls in 22 sessions" DETECTOR.

    `Hold` is excluded from the ratio (it is an abstention, not a stance) but reported, because a
    book that answers Hold to everything is a different pathology and must not read as balanced.
    """
    dirs = [_direction(d.get("signal")) for d in decisions]
    bulls = sum(1 for x in dirs if x == "bull")
    bears = sum(1 for x in dirs if x == "bear")
    holds = sum(1 for x in dirs if x is None)
    n = bulls + bears
    thr = f"RED outside [{t.d1_red_low}, {t.d1_red_high}] with n>={t.d1_min_n}"
    if n < t.d1_min_n:
        return _result(None if n == 0 else bulls / n, n, LOW_N, thr,
                       f"bulls={bulls} bears={bears} holds={holds} (need n>={t.d1_min_n})")
    share = bulls / n
    p = binom_two_sided_p(bulls, n)
    if share <= t.d1_red_low or share >= t.d1_red_high:
        flag = RED
    elif share <= t.d1_amber_low or share >= t.d1_amber_high:
        flag = AMBER
    else:
        flag = GREEN
    return _result(round(share, 6), n, flag, thr,
                   f"bulls={bulls} bears={bears} holds={holds} binom_p={None if p is None else round(p, 6)}")


def d2_stance_flip_rate(decisions: Iterable[dict], t: Thresholds = DEFAULTS) -> dict:
    """Share of consecutive per-ticker transitions that REVERSE direction. High = random flipping."""
    by_tk: dict[str, list] = {}
    for d in decisions:
        dirn = _direction(d.get("signal"))
        if dirn is None:
            continue
        by_tk.setdefault(str(d.get("ticker") or "").upper(), []).append(
            (str(d.get("trade_date") or ""), _f(d.get("id")) or 0.0, dirn))
    flips = trans = 0
    worst: list[tuple[str, int, int]] = []
    for tk, rows in by_tk.items():
        rows.sort(key=lambda r: (r[0], r[1]))
        f = sum(1 for a, b in zip(rows, rows[1:]) if a[2] != b[2])
        tr = max(0, len(rows) - 1)
        flips += f
        trans += tr
        if tr:
            worst.append((tk, f, tr))
    thr = f"RED >{t.d2_red} with n>={t.d2_min_n} transitions"
    if trans < t.d2_min_n:
        return _result(None if trans == 0 else flips / trans, trans, LOW_N, thr,
                       f"transitions={trans} (need >={t.d2_min_n})")
    rate = flips / trans
    worst.sort(key=lambda r: (-(r[1] / r[2]) if r[2] else 0, r[0]))
    top = ", ".join(f"{tk}:{f}/{tr}" for tk, f, tr in worst[:4])
    return _result(round(rate, 6), trans, _band(rate, red=t.d2_red, amber=t.d2_amber, low_is_bad=False),
                   thr, f"flips={flips}/{trans}  worst: {top}")


def d3_round_trip_churn(actions: Iterable[dict], t: Thresholds = DEFAULTS) -> dict:
    """Buy on day d, sell the same name within `max_gap_days`. THE SOXX-CHURN DETECTOR.

    Counts REBALANCE and RECONCILE rows DELIBERATELY: the SOXX 2026-07-14 buy / 07-15 sell was
    allocator target oscillation on the rebalance path, and `lib/ledger.py:757` excludes exactly
    those signals from the consistency gate — so the gate could not have seen it and neither
    would a detector that copied the gate's filter.
    """
    rows = [a for a in actions
            if _norm(a.get("intent")) in ("buy", "sell")
            and _norm(a.get("status")) in ("placed", "dry_run", "filled")]
    by_tk: dict[str, list] = {}
    for a in rows:
        d = _parse_day(a.get("trade_date"))
        if d is None:
            continue
        by_tk.setdefault(str(a.get("ticker") or "").upper(), []).append(
            (d, _norm(a.get("intent")), str(a.get("signal") or "")))
    pairs: list[str] = []
    for tk, evs in by_tk.items():
        evs.sort(key=lambda r: r[0])
        for i, (d0, i0, s0) in enumerate(evs):
            if i0 != "buy":
                continue
            for d1, i1, s1 in evs[i + 1:]:
                gap = (d1 - d0).days
                if gap <= 0:
                    continue
                if gap > t.d3_max_gap_days:
                    break
                if i1 == "sell":
                    pairs.append(f"{tk} buy {d0.isoformat()}({s0 or '-'}) -> sell "
                                 f"{d1.isoformat()}({s1 or '-'}) gap={gap}d")
                    break
    thr = f"RED on any buy->sell round trip within {t.d3_max_gap_days}d"
    flag = RED if pairs else GREEN
    return _result(len(pairs), len(rows), flag, thr,
                   ("; ".join(pairs[:6]) + (f" (+{len(pairs)-6} more)" if len(pairs) > 6 else ""))
                   or "no round trips")


def d4_gate_suppression_mix(ticker_actions: Iterable[dict], t: Thresholds = DEFAULTS) -> dict:
    """Concentration of skip REASONS. A single reason dominating is an accidental off switch.

    This is the detector that would have caught the R:R gate zeroing a whole run
    (300 `reward_risk:below_floor` skips, 0 orders, +0.00%)."""
    skipped = [r for r in ticker_actions if _norm(r.get("status")) == "skipped"]
    counts: dict[str, int] = {}
    for r in skipped:
        raw = str(r.get("detail") or "").strip()
        # normalize "consistency:ungrounded_reversal" / "reward_risk:below_floor" to their family
        key = raw.split(":")[0].strip().lower()[:48] or "(blank)"
        counts[key] = counts.get(key, 0) + 1
    n = len(skipped)
    thr = f"RED when one reason >{t.d4_red} of skips, n>={t.d4_min_n}"
    if n < t.d4_min_n:
        return _result(None if n == 0 else max(counts.values()) / n, n, LOW_N, thr,
                       f"skips={n} (need >={t.d4_min_n})")
    top_key = max(counts, key=lambda k: counts[k])
    share = counts[top_key] / n
    mix = ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:5])
    return _result(round(share, 6), n,
                   _band(share, red=t.d4_red, amber=t.d4_amber, low_is_bad=False), thr,
                   f"top={top_key!r} {counts[top_key]}/{n}  mix: {mix}")


def _is_hit(intent: Any, directional_return: Optional[float]) -> Optional[bool]:
    """Directional correctness: a buy wants a positive move, a sell/underweight a negative one."""
    if directional_return is None:
        return None
    i = _norm(intent)
    if i == "buy":
        return directional_return > 0
    if i == "sell":
        return directional_return < 0
    return None


def d5_conviction_calibration(decisions: Iterable[dict], outcomes: Iterable[dict],
                              t: Thresholds = DEFAULTS) -> dict:
    """Spearman rho(conviction, was-it-right). rho<=0 means conviction carries no information —
    and conviction feeds target weight, which feeds ORDER DOLLARS."""
    by_id = {o.get("decision_id"): o for o in outcomes}
    xs: list[float] = []
    ys: list[float] = []
    for d in decisions:
        conv = _f(d.get("conviction"))
        o = by_id.get(d.get("id"))
        if conv is None or not o:
            continue
        hit = _is_hit(d.get("intent"), _f(o.get("directional_return")))
        if hit is None:
            continue
        xs.append(conv)
        ys.append(1.0 if hit else 0.0)
    n = len(xs)
    thr = f"RED rho<={t.d5_red} with n>={t.d5_min_n}"
    if n < t.d5_min_n:
        return _result(None, n, LOW_N, thr, f"paired conviction+outcome rows={n} (need >={t.d5_min_n})")
    rho = spearman(xs, ys)
    if rho is None:
        return _result(None, n, LOW_N, thr, "no variance in conviction or in outcomes")
    return _result(round(rho, 6), n, _band(rho, red=t.d5_red, amber=t.d5_amber, low_is_bad=True),
                   thr, f"hit_rate={round(statistics.fmean(ys), 4)}")


def d7_holding_period(actions: Iterable[dict], t: Thresholds = DEFAULTS) -> dict:
    """Median calendar days from a buy to the covering sell. Short = the thesis isn't being held."""
    by_tk: dict[str, list] = {}
    for a in actions:
        if _norm(a.get("intent")) not in ("buy", "sell"):
            continue
        if _norm(a.get("status")) not in ("placed", "dry_run", "filled"):
            continue
        d = _parse_day(a.get("trade_date"))
        if d is None:
            continue
        by_tk.setdefault(str(a.get("ticker") or "").upper(), []).append((d, _norm(a.get("intent"))))
    spans: list[float] = []
    for tk, evs in by_tk.items():
        evs.sort(key=lambda r: r[0])
        open_buy = None
        for d, i in evs:
            if i == "buy" and open_buy is None:
                open_buy = d
            elif i == "sell" and open_buy is not None:
                spans.append(float((d - open_buy).days))
                open_buy = None
    n = len(spans)
    thr = f"RED median <{t.d7_red_below}d with n>={t.d7_min_n}"
    if n < t.d7_min_n:
        return _result(None if not spans else statistics.median(spans), n, LOW_N, thr,
                       f"completed round trips={n} (need >={t.d7_min_n})")
    med = statistics.median(spans)
    flag = RED if med < t.d7_red_below else (AMBER if med < t.d7_amber_below else GREEN)
    return _result(round(med, 4), n, flag, thr, f"min={min(spans)} max={max(spans)}")


def _dq_is_degraded(raw) -> bool:
    """Is this decision's `data_quality` payload actually degraded?

    THIS MUST PARSE, NOT SUBSTRING-MATCH. `analyze.assess_data_quality` (analyze.py:205-209)
    ALWAYS emits the KEY `sources_unavailable` — empty list when everything is healthy — and
    `tick.py:575` stores the dict verbatim. A naive `"unavailable" in str(dq)` therefore matches
    the KEY NAME on every single healthy row: measured 20/20 "degraded" on 20 flawless decisions,
    i.e. the detector was permanently RED and had ZERO discriminative power on exactly the silent
    fetcher regression it exists to catch. A permanently-tripped alarm is worse than no alarm.
    """
    if raw is None or raw == "":
        return False
    parsed = None
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
    if isinstance(parsed, dict):
        if parsed.get("core_available") is False:
            return True
        return bool(parsed.get("sources_unavailable"))
    # LEGACY free-text fallback (e.g. bench/brain_tasks.py writes "unavailable: trend_report").
    s = _norm(raw)
    return bool(s) and ("unavailable" in s or "degraded" in s or s in ("bad", "poor", "low"))


def d8_data_quality_degradation(decisions: Iterable[dict], t: Thresholds = DEFAULTS) -> dict:
    """Share of decisions produced on degraded/absent data (ERROR signals, or a data_quality
    field marking channels unavailable). Catches a silent fetcher regression — quiver has had
    eleven of those in one audit."""
    rows = list(decisions)
    n = len(rows)
    bad = 0
    for d in rows:
        if _norm(d.get("signal")) == "error":
            bad += 1
            continue
        if _dq_is_degraded(d.get("data_quality")):
            bad += 1
    thr = f"RED >{t.d8_red} degraded, n>={t.d8_min_n}"
    if n < t.d8_min_n:
        return _result(None if n == 0 else bad / n, n, LOW_N, thr, f"decisions={n} (need >={t.d8_min_n})")
    share = bad / n
    return _result(round(share, 6), n,
                   _band(share, red=t.d8_red, amber=t.d8_amber, low_is_bad=False), thr,
                   f"degraded={bad}/{n}")


def d9_trade_size_vs_drawdown(equity_curve: Sequence[dict], t: Thresholds = DEFAULTS) -> dict:
    """Slope of exposure (gross_notional / equity) on drawdown DEPTH.

    The paper's sharpest behavioral finding: one model de-risked into losses, another doubled
    down. A POSITIVE slope means quiver trades bigger the deeper underwater it is."""
    pts = [p for p in equity_curve if _f(p.get("equity")) and _f(p.get("gross_notional")) is not None]
    if len(pts) < 3:
        return _result(None, len(pts), LOW_N, f"RED slope>{t.d9_red}", "needs gross_notional (Phase 1)")
    peak = 0.0
    xs: list[float] = []
    ys: list[float] = []
    for p in pts:
        eq = _f(p["equity"]) or 0.0
        peak = max(peak, eq)
        dd = 0.0 if peak <= 0 else max(0.0, (peak - eq) / peak)     # depth, 0 = at high-water
        xs.append(dd)
        ys.append((_f(p["gross_notional"]) or 0.0) / eq if eq else 0.0)
    n = len(xs)
    thr = f"RED slope>{t.d9_red} with n>={t.d9_min_n}"
    if n < t.d9_min_n:
        return _result(None, n, LOW_N, thr, f"ticks={n} (need >={t.d9_min_n})")
    slope = ols_slope(xs, ys)
    if slope is None:
        return _result(None, n, LOW_N, thr, "no drawdown variance (never underwater)")
    flag = RED if slope > t.d9_red else GREEN
    return _result(round(slope, 6), n, flag, thr,
                   f"max_dd={round(max(xs), 4)} mean_exposure={round(statistics.fmean(ys), 4)}; "
                   f"positive slope = adding risk into losses")


def d10_target_oscillation(targets: Sequence[dict], t: Thresholds = DEFAULTS) -> dict:
    """Share of per-name target-weight changes that REVERSE the previous change.

    The SOXX RCA blamed the churn on allocator target oscillation, not the consistency gate.
    This measures it directly."""
    series: dict[str, list[float]] = {}
    for row in targets:
        w = row.get("weights") or {}
        for tk, v in w.items():
            fv = _f(v)
            if fv is not None:
                series.setdefault(str(tk).upper(), []).append(fv)
    rev = tot = 0
    worst: list[tuple[str, int, int]] = []
    for tk, ws in series.items():
        deltas = [b - a for a, b in zip(ws, ws[1:]) if abs(b - a) > 1e-9]
        r = sum(1 for a, b in zip(deltas, deltas[1:]) if (a > 0) != (b > 0))
        tr = max(0, len(deltas) - 1)
        rev += r
        tot += tr
        if tr:
            worst.append((tk, r, tr))
    thr = f"RED >{t.d10_red} reversal rate with n>={t.d10_min_n}"
    if tot < t.d10_min_n:
        return _result(None if tot == 0 else rev / tot, tot, LOW_N, thr,
                       f"target changes={tot} (need >={t.d10_min_n})")
    rate = rev / tot
    worst.sort(key=lambda r: -(r[1] / r[2]))
    return _result(round(rate, 6), tot,
                   _band(rate, red=t.d10_red, amber=t.d10_amber, low_is_bad=False), thr,
                   "worst: " + ", ".join(f"{tk}:{r}/{tr}" for tk, r, tr in worst[:4]))


# ---------------------------------------------------------------- the scorecard

def scorecard(decisions: Sequence[dict], actions: Sequence[dict], outcomes: Sequence[dict],
              *, ticker_actions: Sequence[dict] = (), equity_curve: Sequence[dict] = (),
              targets: Sequence[dict] = (), thresholds: Thresholds = DEFAULTS) -> dict:
    """Run every detector. Returns `{'diagnostics': {...}, 'red_flags': [...], 'summary': {...}}`.

    `red_flags` lists ONLY detectors that reached `RED` with enough data. A `LOW_N` never counts
    as a pass and never counts as a flag — it is reported as its own state so an under-powered
    check cannot be mistaken for a clean one.
    """
    decisions = list(decisions or [])
    actions = list(actions or [])
    outcomes = list(outcomes or [])
    diags = {
        "d1_signal_balance": d1_signal_balance(decisions, thresholds),
        "d2_stance_flip_rate": d2_stance_flip_rate(decisions, thresholds),
        "d3_round_trip_churn": d3_round_trip_churn(actions, thresholds),
        "d4_gate_suppression_mix": d4_gate_suppression_mix(list(ticker_actions or []), thresholds),
        "d5_conviction_calibration": d5_conviction_calibration(decisions, outcomes, thresholds),
        "d7_holding_period": d7_holding_period(actions, thresholds),
        "d8_data_quality_degradation": d8_data_quality_degradation(decisions, thresholds),
        "d9_trade_size_vs_drawdown": d9_trade_size_vs_drawdown(list(equity_curve or []), thresholds),
        "d10_target_oscillation": d10_target_oscillation(list(targets or []), thresholds),
    }
    flags = [k for k, v in diags.items() if v["flag"] == RED]
    counts: dict[str, int] = {}
    for v in diags.values():
        counts[v["flag"]] = counts.get(v["flag"], 0) + 1
    return {"diagnostics": diags, "red_flags": sorted(flags),
            "summary": {"n_decisions": len(decisions), "n_actions": len(actions),
                        "n_outcomes": len(outcomes), "flag_counts": counts}}


def format_scorecard(sc: dict) -> str:
    """Human-readable rendering (used by the CLI and by failure messages)."""
    lines = []
    for name, d in sc["diagnostics"].items():
        v = d["value"]
        vs = "n/a" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))
        lines.append(f"  {d['flag']:6s} {name:28s} value={vs:>10s} n={d['n']:<5d} {d['detail']}")
    s = sc["summary"]
    lines.append(f"  -- decisions={s['n_decisions']} actions={s['n_actions']} "
                 f"outcomes={s['n_outcomes']} flags={s['flag_counts']}")
    if sc["red_flags"]:
        lines.append(f"  RED: {', '.join(sc['red_flags'])}")
    return "\n".join(lines)
