#!/usr/bin/env python3
"""Deterministic Python CLI for the desk: execution, snapshots, scoring and seeding.

Every subcommand prints one JSON line and catches every exception into
``{"error": ...}`` with exit code 1. The broker runbooks (EXECUTE.md, SNAPSHOT.md) and
``deploy/runner/run_broker.py`` call the execution subcommands; the monitor calls the
scoring ones; the cutover calls ``desk-seed``. All guardrails live here in Python, never
in a model's judgment.

Subcommands:
  exec-prepare / exec-check / exec-commit   the execute job (plan §11)
  snapshot-prepare / snapshot-commit        the snapshot job (plan §11.4)
  fills / wait / auth-stop                  runbook helpers
  reflect / benchmark-fetch / benchmark-backfill / goal-track / prune   after the close
  flow-record / flow-suggest / attribution / factors-fetch / strategy-history /
  decision-proof / send-test                operator tools
  desk-seed                                 the one-time cutover seed (plan §16)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
from lib import market  # noqa: E402
from lib import notify  # noqa: E402
from lib import storage  # noqa: E402
from lib import memory  # noqa: E402
from lib import universe  # noqa: E402
from lib import attribution  # noqa: E402 — read-only diagnostic
from lib import fsutil  # noqa: E402
from lib.desk import store  # noqa: E402
from lib.factor_report import (FACTORS_SIDECAR, _capture_window_map, _capture_window_span,  # noqa: E402,F401
                               _daily_returns, _factors_path, _interpretation, _load_factors,
                               _session_set, _trading_day_spans, _xnys_sessions, cmd_attribution)


# REPO is the state root. QUIVER_ROOT points the SAME CLI at a temp root (a test) without
# touching the live state. QUIVER_LEDGER_DB and QUIVER_CONFIG override single paths.
# Production sets none of them.
REPO = Path(os.environ.get("QUIVER_ROOT") or Path(__file__).resolve().parent)
LEDGER_DB = Path(os.environ.get("QUIVER_LEDGER_DB") or (REPO / "state" / "ledger.db"))
CONFIG_PATH = Path(os.environ.get("QUIVER_CONFIG") or (REPO / "config.yaml"))


# The unique sentinel that `auth-stop` prints. The runbooks name the COMMAND and never this
# string, so it reaches run_broker.py's captured stream output only when the command ran on
# a real Robinhood 401.
AUTH_STOP_SENTINEL = "QUIVER_AUTH_STOP"


# Bulky, reconstructable artifacts the retention sweep ages out (state of record
# is the ledger, never these). Kept here so `prune` and any future caller agree.
PRUNE_TARGETS = [
    REPO / "state" / "cache",
    REPO / "logs" / "desk",
]


def _cfg_and_ledger():
    cfg = load_config(CONFIG_PATH)
    led = Ledger(LEDGER_DB)
    return cfg, led


def _to_float(v):
    """Best-effort float coercion (handles None / strings / blanks) -> float|None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fills_window(led) -> str:
    """The `created_at_gte` that covers EVERY order still missing its real fill.

    Derived from the OLDEST pending order, never guessed. A window narrower than
    the pending set makes the broker answer silent for orders it was never asked
    about, and a live run then retired 139 real orders as `not_found`.

    `submitted_at` carries an ET offset and the broker reads a naive value as UTC,
    so convert rather than pass the raw string. Returns a date (YYYY-MM-DD); one
    extra day of margin costs nothing and a short window loses fills.
    """
    pending = led.fills_pending()
    if not pending:
        return market.trading_day_et()
    oldest = min(str(r.get("submitted_at") or "") for r in pending if r.get("submitted_at"))
    try:
        dt = datetime.fromisoformat(oldest)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return market.trading_day_et()


def _load_input(args) -> dict:
    """Read a subcommand's --input JSON, or {} when none was given."""
    path = getattr(args, "input", None)
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_strategy_set(cfg, led, data) -> dict:
    """Write the active goal + target book from strategy.yaml into the ledger (desk-seed
    step 1b, only when the ledger has no active goal).

    STRICT: load_strategy validate-or-raise (a bad book fails loudly at setup).
    Snapshots start_equity (from the input, else the latest baseline) + start_date (today).
    Places NO orders."""
    import lib.strategy as strategy
    sc = strategy.load_strategy(cfg.strategy_path)
    day = market.trading_day_et()
    now_iso = data.get("now_iso") or market.now_et().isoformat()
    equity = _to_float(data.get("equity"))
    if equity is None or equity <= 0:
        # No equity in --input. Use the latest baseline (total account value, the base of the
        # goal return). A goal with no start_equity gives no goal return: goal-track wrote no
        # row from 2026-08-07 to 2026-10-13 for this reason.
        _series = led.baseline_equity_series() or []
        _known = [r["baseline_equity"] for r in _series
                  if r.get("baseline_equity") and str(r.get("trade_date")) <= day]
        equity = float(_known[-1]) if _known else None
    book = sc.book(sc.default_book)
    # Component D3: capture the PRIOR active book before we supersede it, so we can diff
    # and log each changed weight / added / removed holding to the strategy_change_log.
    prior_goal = led.get_active_goal()
    prior_targets = (led.active_target_portfolio(
        prior_goal["id"], statuses=("active", "exiting", "removed")) if prior_goal else [])
    # ATOMIC: the goal + ALL its holdings commit together (one transaction) or not
    # at all. A partial write (crash mid-setup) would leave a truncated book that
    # reconciliation reads as "everything else is unmanaged" -> mass liquidation.
    goal_fields = {
        "created_at": now_iso, "target_return_pct": sc.goal.target_return_pct,
        "horizon_months": sc.goal.horizon_months, "benchmark": sc.goal.benchmark,
        "benchmark_annual_pct": sc.goal.benchmark_annual_pct,
        "constraint_note": sc.goal.constraint, "macro_thesis_version": sc.macro_thesis.version,
        "macro_thesis_json": json.dumps({"summary": sc.macro_thesis.summary,
                                         "correlation_note": sc.macro_thesis.correlation_note}),
        "active_book": sc.default_book, "as_of": sc.macro_thesis.version,
        "start_date": day, "start_equity": equity,
    }
    holdings = [{"sleeve": h.sleeve, "ticker": h.ticker, "target_weight": h.weight,
                 "band": h.band, "status": "active", "book": sc.default_book,
                 "quotable": h.quotable, "proxy_ticker": h.proxy_ticker,
                 "updated_at": now_iso} for h in book.holdings]
    gid = led.set_strategy_goal_with_holdings(goal=goal_fields, holdings=holdings)
    # D3: diff the new book against the prior one + log the strategic changes (best-effort
    # — a change-log hiccup must never block setup). Also a min-interval note (D3) when the
    # book is re-set sooner than advised, surfacing churn without blocking it.
    changes = []
    try:
        prior_by_tk = {str(t["ticker"]).upper(): t for t in prior_targets}
        new_by_tk = {h["ticker"].upper(): h for h in holdings}
        for tk, h in new_by_tk.items():
            prior = prior_by_tk.get(tk)
            if prior is None:
                if prior_goal:  # only an audit-worthy ADD when there WAS a prior book
                    led.record_strategy_change(goal_id=gid, changed_at=now_iso, change_type="status",
                                               ticker=tk, from_value=None, to_value="added",
                                               trigger="strategy-set", reason="holding added to the book")
                    changes.append({"ticker": tk, "change": "added"})
            elif float(prior.get("target_weight", 0) or 0) != float(h["target_weight"]):
                led.record_strategy_change(goal_id=gid, changed_at=now_iso, change_type="weight",
                                           ticker=tk, from_value=prior.get("target_weight"),
                                           to_value=h["target_weight"], trigger="strategy-set",
                                           reason="target weight changed")
                changes.append({"ticker": tk, "change": "weight",
                                "from": prior.get("target_weight"), "to": h["target_weight"]})
        for tk in prior_by_tk:
            if tk not in new_by_tk:
                led.record_strategy_change(goal_id=gid, changed_at=now_iso, change_type="status",
                                           ticker=tk, from_value="active", to_value="removed",
                                           trigger="strategy-set", reason="holding removed from the book")
                changes.append({"ticker": tk, "change": "removed"})
    except Exception:  # noqa: BLE001 — change-logging is observability; never blocks setup
        pass
    return {"goal_id": gid, "active_book": sc.default_book, "holdings": len(book.holdings),
            "start_equity": equity, "start_date": day, "changes": changes}


def cmd_goal_track(args) -> dict:
    cfg, led = _cfg_and_ledger()
    return _run_goal_track(cfg, led)


def _run_goal_track(cfg, led) -> dict:
    """Best-effort goal-progress snapshot (never stops a tick). Records a
    goal_tracking row from the ledger equity curve + the active goal.

    Also runs deposit auto-capture (observability side): DETECTS suspected external cash
    flows from day-over-day baseline jumps and SURFACES them (`suspected_flows`), and —
    only when `cfg.goal_auto_capture_flows` — AUTO-WRITES the settled/persisted ones to
    `cash_flows`. This never touches sizing, order placement, or the daily-loss halt (none
    of them read `cash_flows`); its only downstream effect is the deposit-adjusted goal
    return + coarse regime. Fully wrapped: a hiccup here can never break goal-track."""
    import lib.goal as goal_mod
    goal = led.get_active_goal()
    if not goal:
        return {"recorded": False, "reason": "no active goal"}
    prog = goal_mod.compute_from_ledger(led, goal)
    if not prog:
        return {"recorded": False, "reason": "insufficient equity history"}
    led.record_goal_snapshot(
        goal_id=goal["id"], trade_date=market.trading_day_et(),
        captured_at=market.now_et().isoformat(), portfolio_value=prog["current_equity"],
        glidepath_target_value=prog["glidepath_target_value"],
        cumulative_return_pct=prog["cumulative_return_pct"],
        ahead_behind_pct=prog["ahead_behind_pct"],
        alpha_vs_benchmark_pct=prog["alpha_vs_benchmark_pct"],
        active_book=goal["active_book"], regime=prog["regime"])
    out = {"recorded": True, "goal_id": goal["id"], **prog}
    try:
        min_pct = float(getattr(cfg, "goal_flow_min_pct", 25.0))
        min_abs = float(getattr(cfg, "goal_flow_min_abs", 30.0))
        suspected = goal_mod.suggest_flows_from_ledger(led, goal, min_pct=min_pct, min_abs=min_abs)
        out["suspected_flows"] = suspected
        out["n_suspected_flows"] = len(suspected)
        if suspected and getattr(cfg, "goal_auto_capture_flows", False):
            confirm_days = int(getattr(cfg, "goal_flow_confirm_days", 2))
            points, _fl = goal_mod._goal_window(
                led.baseline_equity_series() or [], [], goal.get("start_date"))
            now_iso = market.now_et().isoformat()
            captured = []
            for f in goal_mod.confirmable_flows(points, suspected, confirm_days=confirm_days):
                if led.record_cash_flow_if_absent(
                        trade_date=f["trade_date"], amount=f["amount"],
                        note=f"auto:equity-jump settled>={confirm_days}d ({f['pct']}%)",
                        created_at=now_iso):
                    captured.append({"trade_date": f["trade_date"], "amount": f["amount"]})
            out["auto_captured"] = captured
    except Exception as e:  # noqa: BLE001 — deposit-capture is best-effort observability
        out["flow_capture_error"] = f"{type(e).__name__}: {e}"
    return out


def cmd_flow_suggest(args) -> dict:
    """Read-only: list SUSPECTED external cash flows the operator should confirm.

    Detects day-over-day baseline-equity jumps too large to be market moves and prints
    ready-to-run `flow-record` commands for each — the ops hook that replaces hand-digging
    Robinhood transfer history. Records NOTHING; the operator confirms each (a jump could be
    a genuine large market move on a concentrated book). Use the SUGGESTED trade_date: it is
    the day the deposit entered the equity curve, which is the date the deposit-adjusted
    return must strip it on."""
    import lib.goal as goal_mod
    cfg, led = _cfg_and_ledger()
    goal = led.get_active_goal()
    min_pct = float(getattr(cfg, "goal_flow_min_pct", 25.0))
    min_abs = float(getattr(cfg, "goal_flow_min_abs", 30.0))
    suspected = (goal_mod.suggest_flows_from_ledger(led, goal, min_pct=min_pct, min_abs=min_abs)
                 if goal else [])
    commands = []
    for f in suspected:
        payload = json.dumps({
            "trade_date": f["trade_date"], "amount": f["amount"],
            "note": f"{f['direction']} (equity {f['prev_equity']}->{f['cur_equity']}, {f['pct']}%)"})
        commands.append(f"tick.py flow-record --input '{payload}'")
    return {"suspected_flows": suspected, "commands": commands, "count": len(suspected)}


def cmd_flow_record(args) -> dict:
    """Record one external cash flow so goal-progress can deposit-adjust the return.

    amount > 0 = deposit, < 0 = withdrawal; trade_date defaults to today (ET). This is an
    ops/observability utility ONLY — it feeds the goal/digest deposit-adjusted return and the
    coarse goal-regime; it touches sizing, order placement, and the daily-loss halt in NO way.
    """
    cfg, led = _cfg_and_ledger()
    data = _load_input(args)
    amount = data.get("amount")
    if amount is None:
        raise ValueError("flow-record requires 'amount' (deposit > 0 / withdrawal < 0)")
    trade_date = str(data.get("trade_date") or market.trading_day_et())
    note = data.get("note")
    led.record_cash_flow(trade_date=trade_date, amount=float(amount),
                         note=(str(note) if note is not None else None),
                         created_at=market.now_et().isoformat())
    return {"recorded": True, "trade_date": trade_date, "amount": float(amount)}


def _jsonable_series(hist) -> dict:
    """Coerce a price frame to {date: {open, close}} with plain Python floats.

    The coercion is the point: pandas Timestamps are not JSON keys, numpy scalars
    are not JSON values, and NaN serializes to invalid JSON. Doing it here means a
    vendor quirk yields a smaller sidecar rather than an unserializable payload
    that would raise outside any handler.
    """
    out = {}
    try:
        for idx, row in hist.iterrows():
            try:
                d = str(idx)[:10]
                o, c = float(row["Open"]), float(row["Close"])
            except Exception:  # noqa: BLE001
                continue
            if o > 0 and c > 0 and o == o and c == c and o != float("inf") and c != float("inf"):
                out[d] = {"open": o, "close": c}
    except Exception:  # noqa: BLE001
        return {}
    return out


def _book_tickers() -> list:
    """The active book's tickers (ledger goal first, then strategy.yaml)."""
    led = Ledger(LEDGER_DB)
    goal = led.get_active_goal()
    rows = led.active_target_portfolio(goal["id"], statuses=("active",)) if goal else []
    if rows:
        return sorted({str(r["ticker"]).upper() for r in rows
                       if r.get("ticker") and str(r["ticker"]).upper() not in ("CASH", "USD")})
    cfg = load_config(CONFIG_PATH)
    if cfg.strategy is None:
        return []
    book = cfg.strategy.book(cfg.strategy.default_book)
    return sorted({str(h.ticker).upper() for h in book.holdings
                   if str(h.ticker).upper() not in ("CASH", "USD")})


def cmd_factors_fetch(args) -> dict:
    """OPERATOR-RUN price fetcher for the attribution diagnostic.

    Deliberately NOT invoked by run_tick.py: it draws no wall-clock budget from a
    tick and no tick phase reads its output. Like the event-risk producer it runs
    as its own process with an outer timeout and ALWAYS writes a day-stamped
    sidecar (an empty series on total failure), because a stalled socket cannot be
    caught by try/except -- only by process death.
    """
    day = str(getattr(args, "date", "") or market.trading_day_et())
    payload = {"trading_day": day, "series": {}, "source": None}
    try:
        import socket
        socket.setdefaulttimeout(15)
        import yfinance as yf

        tickers = [t.strip().upper() for t in str(getattr(args, "tickers", "") or "").split(",") if t.strip()]
        if not tickers:
            # Default to the book's own names so the PRIMARY (bottom-up) estimate
            # has a series for every holding it is asked to weight.
            try:
                tickers = _book_tickers()
            except Exception:  # noqa: BLE001
                tickers = []
        benches = ["SPY", "QQQ"]
        want = list(dict.fromkeys(benches + tickers))
        period = str(getattr(args, "period", "") or "2y")

        series = {}
        for t in want:
            try:
                hist = yf.Ticker(t).history(period=period, auto_adjust=True)
            except Exception:  # noqa: BLE001
                continue
            rows = _jsonable_series(hist)
            if rows:
                series[t] = rows
        payload["series"] = series
        payload["source"] = "yfinance(auto_adjust=True)"
    except Exception as e:  # noqa: BLE001
        payload["error"] = str(e)[:200]
    _write_json_sidecar(_factors_path(), payload)
    return {"ok": bool(payload["series"]), "trading_day": day,
            "tickers": sorted(payload["series"]), "path": str(_factors_path())}


def _write_json_sidecar(path, payload: dict) -> None:
    """Atomic, best-effort sidecar write. Serialization happens INSIDE the guard."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write_text(path, json.dumps(payload))
    except (OSError, TypeError, ValueError):
        pass


BENCHMARK_SIDECAR = "benchmark.json"


DEFAULT_BENCHMARK = "SPY"


def _benchmark_path():
    """Sidecar location. Overridable so tests never touch the live scratch file."""
    return Path(os.environ.get("QUIVER_BENCHMARK_PATH")
                or (REPO / "state" / "tmp" / BENCHMARK_SIDECAR))


def _load_benchmark():
    """Read the benchmark sidecar. NOT day-stamp gated: a backfill is a historical
    operation, and per-window completeness is enforced by the calendar rule in
    lib.benchmark rather than by how fresh the file is."""
    try:
        raw = json.loads(_benchmark_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None, "no usable benchmark sidecar; run: tick.py benchmark-fetch"
    if not isinstance(raw, dict):
        return None, "benchmark sidecar is not an object"
    if not isinstance(raw.get("series"), dict) or not raw["series"]:
        return None, "benchmark sidecar carries no price series; run: tick.py benchmark-fetch"
    if not isinstance(raw.get("sessions"), list) or not raw["sessions"]:
        return None, "benchmark sidecar carries no session calendar; run: tick.py benchmark-fetch"
    return raw, ""


def _benchmark_sessions(series, day: str) -> list:
    """The XNYS session calendar to ship alongside a benchmark series.

    Extracted so a test can drive the REAL rule instead of re-implementing it. That is
    not cosmetic: this function shipped once bounded by ``max(series)``, and because both
    test fixtures hand-wrote a calendar the producer could never emit, nothing caught it.

    It MUST extend PAST the series. lib.benchmark anchors a target to the last session on
    or before it and refuses when that session is absent from the series; a calendar that
    stops where the series stops can never contain such a session, so the refusal goes
    structurally dead exactly at the tail -- and the tail is where the original defect
    lived (a series ending 2026-07-02 pricing a window ending 2026-07-06, 2.2x low).
    """
    import pandas_market_calendars as mcal

    end = (datetime.strptime(max(max(series), day or ""), "%Y-%m-%d")
           + timedelta(days=45)).strftime("%Y-%m-%d")
    sched = mcal.get_calendar("XNYS").schedule(start_date=min(series), end_date=end)
    return sorted({d.strftime("%Y-%m-%d") for d in sched.index})


def cmd_benchmark_fetch(args) -> dict:
    """OPERATOR/RUNNER-RUN price fetcher for the benchmark measurement.

    Own process with an outer timeout, and ALWAYS writes a sidecar (an empty series
    on total failure), because a stalled socket can only be bounded by process death.

    Uses auto_adjust=False and the raw Close ON PURPOSE, unlike factors-fetch: the
    position leg of every outcome is a PRICE return between two broker quotes with no
    dividend credit, so the market leg must be a price return too. A total-return
    series would overstate the benchmark by its dividend yield and bias every alpha
    negative. (Close is already split-adjusted, so this is not a split hazard.)
    """
    bench = str(getattr(args, "benchmark", "") or DEFAULT_BENCHMARK).upper()
    day = str(getattr(args, "date", "") or market.trading_day_et())
    payload = {"trading_day": day, "benchmark": bench, "series": {}, "sessions": [], "source": None}
    try:
        import socket
        socket.setdefaulttimeout(15)
        import yfinance as yf
        import pandas_market_calendars as mcal

        period = str(getattr(args, "period", "") or "2y")
        hist = yf.Ticker(bench).history(period=period, auto_adjust=False)
        series = {}
        for idx, row in hist.iterrows():
            try:
                d, c = str(idx)[:10], float(row["Close"])
            except Exception:  # noqa: BLE001
                continue
            if c > 0 and c == c and c != float("inf"):
                series[d] = c
        payload["series"] = series
        if series:
            payload["sessions"] = _benchmark_sessions(series, day)
        payload["source"] = "yfinance(auto_adjust=False, Close)"
    except Exception as e:  # noqa: BLE001 — a total failure still writes an empty sidecar
        payload["error"] = str(e)[:200]
    _write_json_sidecar(_benchmark_path(), payload)
    return {"ok": bool(payload["series"]), "benchmark": bench, "trading_day": day,
            "observations": len(payload["series"]),
            "last_observation": max(payload["series"]) if payload["series"] else None,
            "path": str(_benchmark_path())}


def _benchmark_plan_rows(led, sidecar, fill_only: bool) -> list:
    """Recompute the market leg for every resolved decision. Pure classification --
    decides nothing and writes nothing."""
    from lib import benchmark as benchmark_lib

    series, sessions = sidecar["series"], sidecar["sessions"]
    out = []
    for row in led.outcomes_for_backfill():
        cur_b, cur_a = row.get("benchmark_return"), row.get("alpha")
        if fill_only and cur_b is not None:
            continue
        new_b, why = benchmark_lib.window_return(
            series=series, start_date=row.get("trade_date"),
            end_date=str(row.get("resolved_at") or "")[:10], sessions=sessions)
        dret = row.get("directional_return")
        # alpha ONLY where the position leg exists, so `alpha non-NULL` stays a subset
        # of `directional_return non-NULL`. That subset relation is what keeps the
        # graded sample size fixed, which is what keeps any name from crossing the
        # calibration min_n cliff on a re-measurement.
        new_a = (dret - new_b) if (new_b is not None and dret is not None) else None
        same = ((cur_b is None and new_b is None)
                or (cur_b is not None and new_b is not None and abs(cur_b - new_b) <= 1e-12))
        if same:
            kind = "unchanged"
        elif new_b is None:
            # A refusal on an already-populated row CLEARS it. Leaving a value that
            # cannot be reproduced would let a bad measurement survive its own fix.
            kind = "cleared"
        elif cur_b is None:
            kind = "fill"
        else:
            kind = "correct"
        out.append({**row, "new_benchmark_return": new_b, "new_alpha": new_a,
                    "kind": kind, "reason": why})
    return out


def cmd_benchmark_backfill(args) -> dict:
    """Recompute outcomes.benchmark_return/alpha from the sidecar. DRY-RUN unless --apply.

    The track record (``get_track_record``, the close brief) reads alpha; nothing in the
    order path reads it. ``--max-rows`` still bounds the unattended path: a large backlog
    is left for a reviewed run.
    """
    apply = bool(getattr(args, "apply", False))
    fill_only = bool(getattr(args, "fill_only", False))
    try:
        max_rows = int(getattr(args, "max_rows", 0) or 0)
    except (TypeError, ValueError):
        max_rows = 0

    out = {"ok": False, "applied": False, "dry_run": not apply,
           "fill_only": fill_only, "db": str(LEDGER_DB)}
    sidecar, why = _load_benchmark()
    if sidecar is None:
        out["reason"] = why
        return out
    out["benchmark"] = sidecar.get("benchmark")
    out["last_observation"] = max(sidecar["series"]) if sidecar["series"] else None

    led = Ledger(LEDGER_DB)
    rows = _benchmark_plan_rows(led, sidecar, fill_only)
    changes = [r for r in rows if r["kind"] != "unchanged"]
    counts = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    out["counts"] = counts
    out["examined"] = len(rows)
    out["changes"] = len(changes)
    # Refusal reasons are the audit trail for anything left NULL -- surface them
    # rather than reporting a silent "unchanged".
    out["refusals"] = sorted({r["reason"] for r in rows
                              if r["reason"] and r["kind"] in ("unchanged", "cleared")})[:5]

    # Bound the UNATTENDED path by construction. Filling a NULL re-bases the scorecard
    # from absolute return onto alpha, so a large backlog re-prices the book just as
    # surely as correcting a wrong value does; only the row count separates routine
    # measurement from a reviewable change.
    if max_rows > 0 and len(changes) > max_rows:
        out["reason"] = (f"backlog of {len(changes)} rows exceeds --max-rows {max_rows}; "
                         "run a reviewed `tick.py benchmark-backfill` (dry-run), then --apply")
        out["ok"] = True
        return out

    if apply and changes:
        for r in changes:
            led.update_outcome_benchmark(r["decision_id"], r["new_benchmark_return"], r["new_alpha"])
        out["applied"] = True
        goal = led.get_active_goal()
        if goal:  # a NULL goal_id row is invisible to strategy_change_history
            try:
                led.record_strategy_change(
                    goal_id=goal["id"], changed_at=market.now_et().isoformat(),
                    change_type="measurement", trigger="benchmark-backfill",
                    from_value=f"{counts.get('fill', 0)} unmeasured/"
                               f"{counts.get('correct', 0)} mismeasured",
                    to_value=f"{len(changes)} rows re-measured vs {out['benchmark']}",
                    reason="benchmark_return/alpha recomputed from final closes over each "
                           "decision's own holding window (Python-owned measurement)",
                    proof_json=json.dumps({"counts": counts,
                                           "benchmark": out["benchmark"],
                                           "fill_only": fill_only}))
            except Exception:  # noqa: BLE001 — the change-log is observability; never blocks
                pass
    out["ok"] = True
    return out


def cmd_decision_proof(args) -> dict:
    """Print the stored proof bundle for one decision id (Component B observability).

    Shows the consistency verdict + its inputs (prior stance, plan_trigger, basis
    change, flip count) so a human can audit WHY a decision was allowed or suppressed."""
    led = Ledger(LEDGER_DB)
    d = led.get_decision(int(args.id))
    if not d:
        return {"ok": False, "reason": "no such decision id", "id": int(args.id)}
    proof = None
    if d.get("proof_json"):
        try:
            proof = json.loads(d["proof_json"])
        except Exception:  # noqa: BLE001
            proof = {"raw": d["proof_json"]}
    return {"ok": True, "id": d["id"], "ticker": d["ticker"], "trade_date": d["trade_date"],
            "signal": d.get("signal"), "intent": d.get("intent"), "basis": d.get("basis"),
            "plan_trigger": d.get("plan_trigger"), "proof": proof}


def cmd_strategy_history(args) -> dict:
    """Print the recent strategic-change audit trail (Component D observability).

    The append-only record of every regime/book/weight/status change with from->to +
    trigger + reason + proof — so a posture/weight flip-flop is detectable after the fact."""
    led = Ledger(LEDGER_DB)
    goal = led.get_active_goal()
    if not goal:
        return {"ok": True, "reason": "no active goal", "changes": []}
    rows = led.strategy_change_history(goal["id"], limit=int(getattr(args, "limit", 50) or 50),
                                       change_type=getattr(args, "type", None) or None)
    return {"ok": True, "goal_id": goal["id"], "count": len(rows), "changes": rows}


def cmd_send_test(args) -> dict:
    """Send a REAL alert through lib.telegram using the production env resolution.

    The deploy acceptance gate: it exercises the exact TELEGRAM_BOT_TOKEN / chat-id path the
    live alert senders use, so a missing token or a wrong chat id is caught now, not during a
    real 2am incident (the [6/6] healthcheck can't see it — it only load_config's). Pass
    ``--to`` to target specific chat id(s) for the test. (QUIVER_TELEGRAM_DISABLE=1 builds the
    message without sending — used by the offline tests.)
    """
    from lib import telegram  # local: ops-layer network egress, not the trading brain
    cfg, led = _cfg_and_ledger()
    date = market.trading_day_et()
    now_iso = market.now_et().isoformat()
    kind = args.kind or "auth_error"
    data = {
        "date": date, "now_iso": now_iso, "kind": kind,
        "stage": args.stage or None, "severity": args.severity or "critical",
        "event_detail": (getattr(args, "detail", "") or
                         "send-test: Quiver alerting self-test (no real failure)."),
        "host": os.environ.get("QUIVER_HOST_HINT") or None,
    }
    model = {"date": date, "now_iso": now_iso, "kind": kind, "stage": data["stage"],
             "severity": data["severity"], "event_detail": data["event_detail"],
             "host": data["host"]}
    built = notify.build_alert(model)
    token, chat_ids = telegram.resolve_env()
    if args.to:  # explicit override: comma/space-separated chat id(s) for the test
        chat_ids = telegram._recipients(args.to)
    res = telegram.send_message(token=token, chat_ids=chat_ids,
                                text="[SELF-TEST] " + built["telegram"],
                                disable_notification=False)
    return {"sent": bool(res.get("ok")), "result": res, "chats": chat_ids,
            "partial": bool(res.get("partial")), "kind": kind, "stage": notify.stage_of(model)}


def cmd_auth_stop(args) -> dict:
    """Print the AUTH-STOP sentinel for ``run_broker.py``. The broker runbook runs this on
    a Robinhood 401 and then stops. It places and decides nothing; ``run_broker.py`` finds
    the string in the stream output and sends the ``auth_error`` page."""
    return {"event": AUTH_STOP_SENTINEL, "stage": notify.AUTH_STAGE, "job": int(args.job)}


def cmd_reflect(args) -> dict:
    led = Ledger(LEDGER_DB)
    now_iso = market.now_et().isoformat()
    today = market.now_et().date()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    resolutions = data.get("resolutions", []) or []
    trust_input_benchmark = bool(getattr(args, "trust_input_benchmark", False))
    ignored_benchmarks = 0

    results = []
    affected: set = set()
    for r in resolutions:
        did = r.get("decision_id")
        dec = led.get_decision(did) if did is not None else None
        if not dec:
            results.append({"decision_id": did, "status": "unknown_decision"})
            continue

        price_now = _to_float(r.get("price_now"))
        dret = memory.directional_return(_to_float(dec.get("decision_price")), price_now)
        mv = _to_float(r.get("position_market_value"))
        basis = _to_float(r.get("position_cost_basis"))
        unrealized = (mv - basis) if (mv is not None and basis is not None) else None
        realized = _to_float(r.get("realized_pnl"))

        if dret is None and unrealized is None and realized is None:
            # Nothing to score yet (no usable price, no position) — leave pending.
            results.append({"decision_id": did, "status": "nothing_to_score"})
            continue

        try:
            td = datetime.strptime(dec["trade_date"], "%Y-%m-%d").date()
            holding_days = (today - td).days
        except Exception:  # noqa: BLE001
            holding_days = None
        supplied_bench = _to_float(r.get("benchmark_return"))
        if trust_input_benchmark:
            bench = supplied_bench
        else:
            bench = None
            if supplied_bench is not None:
                ignored_benchmarks += 1
        alpha = (dret - bench) if (dret is not None and bench is not None) else None
        scored = "both" if (unrealized is not None or realized is not None) else "directional"
        # F7/P7: grade whether the recorded stop/target plan held (PTJ "no curve" accountability).
        adherence = memory.plan_adherence(dec, price_now)

        led.record_outcome(
            did, resolved_at=now_iso, holding_days=holding_days,
            directional_return=dret, benchmark_return=bench, alpha=alpha,
            realized_pnl=realized, unrealized_pnl=unrealized, scored_against=scored,
            adherence=adherence,
        )
        affected.add(dec.get("ticker"))
        results.append({"decision_id": did, "status": "resolved",
                        "directional_return": dret, "scored_against": scored})

    out = {
        "ok": True,
        "resolved": sum(1 for x in results if x["status"] == "resolved"),
        "results": results,
    }
    if ignored_benchmarks:
        # Say so out loud. A silently-dropped input is how the old measurement rotted.
        out["ignored_benchmark_returns"] = ignored_benchmarks
        out["benchmark_note"] = ("benchmark_return is computed by Python, not accepted here; "
                                 "run: tick.py benchmark-fetch && tick.py benchmark-backfill")
    return out


def _parse_broker_fill(o: dict) -> dict:
    """Pull the REAL fill out of one broker order object. Pure.

    A zero price is not a fill, so it becomes None rather than 0.0 — the same
    convention lib/memory.py uses. Fees keep 0.0, which is a real value.
    """
    avg = _to_float(o.get("average_price"))
    qty = _to_float(o.get("cumulative_quantity"))
    if qty is None:
        qty = _to_float(o.get("quantity"))
    fees = _to_float(o.get("fees"))
    return {
        "avg_price": avg if (avg and avg > 0) else None,
        "quantity": qty if (qty and qty > 0) else None,
        "fees": fees if fees is not None else 0.0,
        "fill_state": str(o.get("state") or ""),
    }


def _execution():
    """``lib.desk.execution``, imported on first use. The chat and worker processes import
    tick for the read-only attribution; they never load the execution module."""
    from lib.desk import execution
    return execution


def cmd_fills(args) -> dict:
    """Capture the REAL fill of every pending order from a ``get_equity_orders`` answer
    (EXECUTE.md steps 4 and 6). The input is ``{"get_equity_orders": <raw result>}``.

    Python takes the window (``created_at_gte``) from the order rows, never from the
    model's file: with no window every pending order would read ``not_found``. An order
    older than the window stays pending for a later, wider fetch. A desk leg moves with
    its order: filled, partially_filled, cancelled, broker_rejected.
    """
    cfg, led = _cfg_and_ledger()
    execution = _execution()
    d = execution.read_input(args.input, root=REPO, now_dt=store.utc_now(), max_age_s=None)
    return execution.cmd_fills(led, d)


def cmd_wait(args) -> dict:
    """Sleep N seconds, at most 30 (EXECUTE.md steps 4 and 6). The broker model has no
    other way to wait between two order polls."""
    n = int(args.seconds)
    if n < 0 or n > 30:
        raise ValueError("wait takes 0 to 30 seconds")
    time.sleep(n)
    return {"ok": True, "slept": n}


def _exec_input(args, max_age_s=60.0):
    """Read a runbook file; on an age or path failure return ({}, reason)."""
    execution = _execution()
    try:
        return execution.read_input(args.input, root=REPO, now_dt=store.utc_now(), max_age_s=max_age_s), None
    except execution.ExecError as e:
        if "state/tmp" in str(e):
            raise
        return {}, str(e)


def cmd_exec_prepare(args) -> dict:
    execution = _execution()
    cfg, led = _cfg_and_ledger()
    return execution.exec_prepare(led, cfg, int(args.proposal), recovery=bool(args.recovery))


def cmd_exec_check(args) -> dict:
    execution = _execution()
    cfg, led = _cfg_and_ledger()
    d, err = _exec_input(args)
    return execution.exec_check(led, cfg, int(args.proposal), args.phase, d, file_error=err)


def cmd_exec_commit(args) -> dict:
    execution = _execution()
    cfg, led = _cfg_and_ledger()
    d = execution.read_input(args.input, root=REPO, now_dt=store.utc_now(), max_age_s=None)
    return execution.exec_commit(led, cfg, int(args.proposal), d)


def cmd_snapshot_prepare(_args) -> dict:
    execution = _execution()
    cfg, led = _cfg_and_ledger()
    return execution.snapshot_prepare(led, cfg)


def cmd_snapshot_commit(args) -> dict:
    execution = _execution()
    cfg, led = _cfg_and_ledger()
    d = execution.read_input(args.input, root=REPO, now_dt=store.utc_now(), max_age_s=None)
    return execution.snapshot_commit(led, cfg, d)


def cmd_desk_seed(args) -> dict:
    """The one-time cutover seed (plan §16.1). Idempotent: a second run changes nothing."""
    from lib.desk import seed
    cfg, led = _cfg_and_ledger()
    return seed.desk_seed(led, cfg, root=REPO, run_strategy_set=_run_strategy_set,
                          skip_load=bool(getattr(args, "skip_load", False)),
                          skip_intro=bool(getattr(args, "skip_intro", False)))


def cmd_prune(_args) -> dict:
    cfg = load_config(CONFIG_PATH)
    arch = storage.get_archiver(cfg.storage)
    summaries = [
        storage.prune_dir(t, cfg.storage.retention_days, archiver=arch)
        for t in PRUNE_TARGETS
    ]
    return {
        "ok": True,
        "retention_days": cfg.storage.retention_days,
        "archive_enabled": cfg.storage.archive_enabled,
        "pruned_total": sum(s["pruned"] for s in summaries),
        "archived_total": sum(s["archived"] for s in summaries),
        "dirs": summaries,
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_fills = sub.add_parser("fills")
    p_fills.add_argument("--input", required=True)
    p_wait = sub.add_parser("wait")
    p_wait.add_argument("--seconds", required=True)
    p_reflect = sub.add_parser("reflect")
    p_reflect.add_argument("--input", required=True)
    p_bf = sub.add_parser("benchmark-fetch")
    p_bf.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    p_bf.add_argument("--date", default="")
    p_bf.add_argument("--period", default="2y")
    p_bb = sub.add_parser("benchmark-backfill")
    p_bb.add_argument("--apply", action="store_true")
    p_bb.add_argument("--fill-only", dest="fill_only", action="store_true")
    p_bb.add_argument("--max-rows", dest="max_rows", default="0")
    sub.add_parser("goal-track")
    p_fr = sub.add_parser("flow-record")
    p_fr.add_argument("--input", required=True)
    sub.add_parser("flow-suggest")
    p_at = sub.add_parser("attribution")
    p_at.add_argument("--date", default="")
    p_at.add_argument("--benchmark", default="SPY")
    p_ff = sub.add_parser("factors-fetch")
    p_ff.add_argument("--date", default="")
    p_ff.add_argument("--tickers", default="")
    p_ff.add_argument("--period", default="2y")
    sub.add_parser("prune")
    p_st = sub.add_parser("send-test")
    p_st.add_argument("--kind", default="auth_error")
    p_st.add_argument("--stage", default="")
    p_st.add_argument("--severity", default="critical")
    p_st.add_argument("--to", default="")
    p_st.add_argument("--detail", default="")
    p_sh = sub.add_parser("strategy-history")
    p_sh.add_argument("--limit", default="50")
    p_sh.add_argument("--type", default="")
    p_dp = sub.add_parser("decision-proof")
    p_dp.add_argument("--id", required=True)
    p_as = sub.add_parser("auth-stop")
    p_as.add_argument("--job", required=True, type=int)
    p_ep = sub.add_parser("exec-prepare")
    p_ep.add_argument("--proposal", required=True, type=int)
    p_ep.add_argument("--recovery", action="store_true")
    p_ec = sub.add_parser("exec-check")
    p_ec.add_argument("--proposal", required=True, type=int)
    p_ec.add_argument("--phase", required=True, choices=["sells", "buys", "recover"])
    p_ec.add_argument("--input", required=True)
    p_ecm = sub.add_parser("exec-commit")
    p_ecm.add_argument("--proposal", required=True, type=int)
    p_ecm.add_argument("--input", required=True)
    sub.add_parser("snapshot-prepare")
    p_sc = sub.add_parser("snapshot-commit")
    p_sc.add_argument("--input", required=True)
    p_ds = sub.add_parser("desk-seed")
    p_ds.add_argument("--skip-load", dest="skip_load", action="store_true")
    p_ds.add_argument("--skip-intro", dest="skip_intro", action="store_true")
    args = ap.parse_args(argv)

    handlers = {
        "fills": cmd_fills, "wait": cmd_wait, "reflect": cmd_reflect,
        "benchmark-fetch": cmd_benchmark_fetch, "benchmark-backfill": cmd_benchmark_backfill,
        "goal-track": cmd_goal_track, "flow-record": cmd_flow_record, "flow-suggest": cmd_flow_suggest,
        "attribution": cmd_attribution, "factors-fetch": cmd_factors_fetch, "prune": cmd_prune,
        "send-test": cmd_send_test, "strategy-history": cmd_strategy_history,
        "decision-proof": cmd_decision_proof, "auth-stop": cmd_auth_stop,
        "exec-prepare": cmd_exec_prepare, "exec-check": cmd_exec_check, "exec-commit": cmd_exec_commit,
        "snapshot-prepare": cmd_snapshot_prepare, "snapshot-commit": cmd_snapshot_commit,
        "desk-seed": cmd_desk_seed,
    }
    try:
        out = handlers[args.cmd](args)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e), "cmd": args.cmd}))
        return 1
    print(json.dumps(out, default=str))
    # desk-seed is an operator step: a failed load must stop the cutover (§15.6 S10).
    if args.cmd == "desk-seed" and isinstance(out, dict) and out.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
