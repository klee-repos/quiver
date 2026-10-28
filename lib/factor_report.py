"""The read-only factor attribution report of ``tick.py attribution`` and the desk's
``get_attribution`` tool.

It answers how much of the book's return is market exposure and how much is selection.
DIAGNOSTIC ONLY: it writes nothing and gates nothing; no order path reads it. The math
lives in ``lib/attribution.py``; this module reads the ledger and the price sidecar that
``tick.py factors-fetch`` writes. The desk tools import this module, not ``tick``, so the
chat and worker processes never load the execution code.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from lib import attribution, market
from lib.config import load_config
from lib.ledger import Ledger

_REPO = Path(__file__).resolve().parent.parent


def _root() -> Path:
    return Path(os.environ.get("QUIVER_ROOT") or _REPO)


def _ledger_db() -> Path:
    return Path(os.environ.get("QUIVER_LEDGER_DB") or (_root() / "state" / "ledger.db"))


def _config_path() -> Path:
    return Path(os.environ.get("QUIVER_CONFIG") or (_root() / "config.yaml"))


FACTORS_SIDECAR = "factors.json"


def _factors_path():
    """Sidecar location. Overridable so tests never touch the live scratch file."""
    return Path(os.environ.get("QUIVER_FACTORS_PATH")
                or (_root() / "state" / "tmp" / FACTORS_SIDECAR))


def _load_factors(day: str):
    """Read the sidecar, ignoring it when absent, unparseable or from another day."""
    try:
        raw = json.loads(_factors_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None, "no usable factor sidecar; run: tick.py factors-fetch"
    if not isinstance(raw, dict):
        return None, "factor sidecar is not an object"
    if raw.get("trading_day") != day:
        return None, f"factor sidecar is stale (stamped {raw.get('trading_day')}, today {day})"
    series = raw.get("series")
    if not isinstance(series, dict) or not series:
        return None, "factor sidecar carries no price series"
    return raw, ""


def _daily_returns(rows: dict, field: str) -> dict:
    """{date: simple return} from {date: {open, close}} over consecutive dates."""
    ds = sorted(rows)
    out = {}
    for a, b in zip(ds, ds[1:]):
        try:
            pa, pb = float(rows[a][field]), float(rows[b][field])
        except (KeyError, TypeError, ValueError):
            continue
        if pa > 0:
            out[b] = pb / pa - 1.0
        continue
    return out


def cmd_attribution(args, *, led=None, cfg_path=None) -> dict:
    """Estimate the book's market exposure. READ-ONLY; writes nothing, gates nothing.

    Never raises for missing data -- a thin ledger or an absent sidecar returns
    ok:false with a reason, exactly as decision-proof does for a missing id.
    """
    day = str(getattr(args, "date", "") or market.trading_day_et())
    bench = str(getattr(args, "benchmark", "") or "SPY").upper()
    out = {
        "ok": False, "trading_day": day, "benchmark": bench,
        "basis": "DIAGNOSTIC ONLY -- not a gate; nothing in the trading path reads this",
    }

    raw, why = _load_factors(day)
    if raw is None:
        out["reason"] = why
        return out
    series = raw["series"]
    out["source"] = raw.get("source")

    if bench not in series:
        out["reason"] = f"benchmark {bench} missing from the factor sidecar"
        return out

    # ---- PRIMARY: bottom-up book beta (n in the hundreds per name) ----------
    led = led or Ledger(_ledger_db())
    weights = {}
    # Same source-of-truth order as _analysis_universe: the ledger's seeded goal
    # book first (it reflects any continual-learning universe change), else the
    # committed strategy.yaml book, else nothing.
    book_source = None
    try:
        goal = led.get_active_goal()
        rows = led.active_target_portfolio(goal["id"], statuses=("active",)) if goal else []
        if rows:
            book_source = "ledger_goal"
            for row in rows:
                t = str(row.get("ticker", "")).upper()
                w = row.get("target_weight")
                if t and w is not None and t not in ("CASH", "USD"):
                    weights[t] = float(w)
        else:
            cfg = load_config(cfg_path or _config_path())
            if cfg.strategy is not None:
                book_source = "strategy_yaml"
                book = cfg.strategy.book(cfg.strategy.default_book)
                for h in book.holdings:
                    t = str(h.ticker).upper()
                    if t and t not in ("CASH", "USD"):
                        weights[t] = float(h.weight)
    except Exception:  # noqa: BLE001
        weights = {}
    out["book_source"] = book_source

    for source_field in ("close",):
        mkt_r = _daily_returns(series[bench], source_field)
        dates = sorted(mkt_r)
        # Align each holding to the benchmark BY DATE, and hand book_beta that
        # holding's own market series. Pairing by position would regress a name
        # that lists late or has an interior gap against the wrong days.
        per_ticker = {}
        mkt_for_ticker = {}
        for t in weights:
            rows = series.get(t)
            if not rows:
                continue
            tr = _daily_returns(rows, source_field)
            common = [d for d in dates if d in tr]
            if len(common) >= 2:
                per_ticker[t] = [tr[d] for d in common]
                mkt_for_ticker[t] = [mkt_r[d] for d in common]
        aligned_mkt = [mkt_r[d] for d in dates]
        if weights:
            bb = attribution.book_beta(weights, per_ticker, aligned_mkt,
                                       market_by_ticker=mkt_for_ticker)
            out["bottom_up"] = bb
        else:
            out["bottom_up"] = {"beta_book": None,
                                "reason": "no active target portfolio in the ledger"}

    # ---- SECONDARY: top-down regression on the account's own curve ----------
    pts_raw = [(r["trade_date"], r["baseline_equity"]) for r in led.baseline_equity_series()]
    in_window = _capture_window_map(led)
    kept, dropped = attribution.clean_equity_points(pts_raw, in_window=in_window)
    spans = _trading_day_spans(kept)
    flows = {}
    try:
        flows = {f["trade_date"]: f["amount"] for f in (led.cash_flows() or [])}
    except Exception:  # noqa: BLE001
        flows = {}
    port_r, dropped_iv = attribution.interval_returns(kept, flows, trading_days=spans)
    dropped = dropped + dropped_iv

    td = {}
    for field_name, label in (("close", "close_to_close"), ("open", "open_to_open")):
        br = _daily_returns(series[bench], field_name)
        y, X, used = attribution.align(port_r, {d: {"mkt": v} for d, v in br.items()}, ["mkt"])
        if len(y) < 2:
            td[label] = {"ok": False, "reason": f"only {len(y)} usable overlapping intervals"}
            continue
        fit = attribution.ols_hac(y, attribution.with_intercept(X),
                                  names=["residual_vs_" + bench.lower(), "beta"])
        td[label] = ({"ok": False, "reason": "estimator refused (degenerate design)"}
                     if fit is None else dict(fit.as_dict(), ok=True, window=[used[0], used[-1]]))

    # The two conventions measure the same thing; when they disagree materially the
    # estimate is dominated by the open/close timing mismatch, not by exposure.
    _bc = (td.get("close_to_close") or {}).get("coefficients", {}).get("beta", {}).get("estimate")
    _bo = (td.get("open_to_open") or {}).get("coefficients", {}).get("beta", {}).get("estimate")
    if _bc is not None and _bo is not None:
        _denom = max(abs(_bc), abs(_bo))
        _rel = (abs(_bc - _bo) / _denom) if _denom else 0.0
        td["timing_disagreement"] = {
            "relative": _rel,
            "timing_dominated": bool(_rel > 0.30),
            "note": ("close-to-close and open-to-open betas differ by more than 30%; the "
                     "estimate reflects the capture-time mismatch between the equity curve "
                     "and the benchmark, not the book's exposure"),
        }

    out["top_down"] = td
    out["dropped_points"] = dropped
    out["capture_window"] = _capture_window_span(led)
    out["ok"] = True
    out["interpretation"] = _interpretation(out)
    return out


def _interpretation(out: dict) -> str:
    """Describe THIS payload, not a generic one.

    Built from the realized numbers because a hardcoded string drifts: the first
    version claimed the bottom-up estimate ran "at several hundred observations per
    name" (SPCX has 28) and that "no standard error or t-statistic is reported"
    (both are, the moment residual df clears the floor).
    """
    parts = [
        "Bottom-up is the PRIMARY estimate: the book's weights times each name's own "
        "beta. It is the ex-ante designed exposure of the static book and does not "
        "isolate stock selection, cash drag or trade timing."
    ]
    bu = out.get("bottom_up") or {}
    per = bu.get("per_name") or {}
    if per:
        ns = sorted(int(v.get("n") or 0) for v in per.values())
        thin = [t for t, v in per.items() if (v.get("n") or 0) < 120]
        parts.append(f"Per-name samples run {ns[0]} to {ns[-1]} observations "
                     f"(median {ns[len(ns) // 2]}).")
        if thin:
            parts.append("Thin history on " + ", ".join(sorted(thin))
                         + " — those betas are far less precise than the rest; "
                           "read each name's own stderr rather than the book total.")
    unp = bu.get("unpriced_weight_pct") or 0
    if unp:
        parts.append(f"{unp:g}% of book weight is unpriced and excluded, so beta_book "
                     "is understated by that much.")

    td = out.get("top_down") or {}
    fits = [v for k, v in td.items() if k != "timing_disagreement" and isinstance(v, dict)]
    withheld = [f for f in fits if f.get("inference_withheld_reason")]
    if withheld:
        parts.append("Top-down inference is WITHHELD at this sample size: the point "
                     "estimates shown are descriptive only, with no standard error, "
                     "t-statistic or significance claim.")
    elif fits:
        parts.append("Top-down reports HAC standard errors; no significance claim is "
                     "made and no threshold is applied.")
    if (td.get("timing_disagreement") or {}).get("timing_dominated"):
        parts.append("The two benchmark conventions disagree by more than 30%, so the "
                     "top-down estimate is dominated by the capture-time mismatch "
                     "between the equity curve and the benchmark.")
    dropped = out.get("dropped_points") or []
    if dropped:
        parts.append(f"{len(dropped)} equity point(s) were excluded; see dropped_points "
                     "for each date and reason.")
    parts.append("The top-down intercept is NOT 'alpha' in the academic sense — with no "
                 "style controls it conflates stock selection with the book's sector "
                 "tilt and cannot separate them.")
    return " ".join(parts)


def _capture_window_map(led) -> dict:
    """{trade_date: captured within 09:30-10:30 ET on a real session}.

    Two of the live rows fail this: one is stamped on a Saturday and one 5.3 hours
    after the close, so the series is otherwise a mixture of measurement equations.
    """
    ok = {}
    try:
        rows = led.baseline_capture_times()
    except Exception:  # noqa: BLE001
        return {}
    sessions = _session_set(rows.keys())
    for d, ts in rows.items():
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:  # noqa: BLE001
            ok[d] = False
            continue
        session = d in sessions
        mins = dt.hour * 60 + dt.minute
        ok[d] = bool(session and 9 * 60 + 30 <= mins <= 10 * 60 + 30)
    return ok


def _capture_window_span(led) -> dict:
    try:
        rows = led.baseline_capture_times()
        times = sorted(str(v)[11:19] for v in rows.values() if v)
        return {"earliest": times[0], "latest": times[-1]} if times else {}
    except Exception:  # noqa: BLE001
        return {}


def _xnys_sessions(start: str, end: str) -> list:
    """XNYS session dates in [start, end], date-ASC. Empty on any calendar failure."""
    try:
        import pandas_market_calendars as mcal
        sched = mcal.get_calendar("XNYS").schedule(start_date=start, end_date=end)
        return [str(d)[:10] for d in sched.index]
    except Exception:  # noqa: BLE001
        return []


def _session_set(dates) -> set:
    """Sessions spanning ``dates``, built from ONE calendar construction.

    Building the calendar per date pair is quadratic-ish in wall time -- a 260-point
    curve meant 259 separate schedule() builds and blew a 180s test timeout. The
    whole range is fetched once and every lookup is a set/index hit.
    """
    ds = sorted(d for d in dates if d)
    if not ds:
        return set()
    return set(_xnys_sessions(ds[0], ds[-1]))


def _trading_day_spans(points) -> dict:
    """{(d0, d1): sessions between} so a multi-day gap is not read as a daily return."""
    dates = [d for d, _ in points]
    sessions = sorted(_session_set(dates))
    index = {d: i for i, d in enumerate(sessions)}
    spans = {}
    for (d0, _), (d1, _) in zip(points, points[1:]):
        i0, i1 = index.get(d0), index.get(d1)
        # Unknown dates (calendar unavailable, or a stamp on a non-session) fall
        # back to 1 so a missing calendar cannot silently delete every interval.
        spans[(d0, d1)] = (i1 - i0) if (i0 is not None and i1 is not None and i1 > i0) else 1
    return spans

