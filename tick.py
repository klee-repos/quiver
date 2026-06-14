#!/usr/bin/env python3
"""Deterministic orchestration brain for one trading tick.

The orchestrator (Claude Code, holding the Robinhood MCP) calls these
subcommands and executes EXACTLY what they return. All guardrails, clamps,
dedup, idempotency, and the daily-loss halt live here in Python — never in the
orchestrator's judgment.

Subcommands:
  preflight                  -> JSON: whether to proceed + config + pending tickers
  plan     --input FILE      -> JSON: concrete clamped orders to execute (+ records holds/skips)
  commit   --input FILE      -> record an order outcome (finalize + ticker_action)

`plan` input JSON shape (built by the orchestrator from MCP responses):
  {
    "equity": 10000.0,            # get_portfolio: total equity (today's value)
    "buying_power": 4000.0,       # get_portfolio: cash buying power
    "now_iso": "2026-05-30T09:36:00-04:00",
    "positions": {"AAPL": {"quantity": 3, "market_value": 600.0}},
    "analyses": [ <one analyze.py JSON per pending ticker> ]
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
from lib import market  # noqa: E402
from lib import notify  # noqa: E402
from lib import signals  # noqa: E402
from lib import storage  # noqa: E402
from lib import memory  # noqa: E402

LEDGER_DB = REPO / "state" / "ledger.db"
ANALYZE_LOGS = REPO / "state" / "analyze_logs"
# Bulky, reconstructable artifacts the retention sweep ages out (state of record
# is the ledger, never these). Kept here so `prune` and any future caller agree.
PRUNE_TARGETS = [
    REPO / "logs" / "reasoning",
    REPO / "state" / "analyze_logs",
    REPO / "state" / "results",
    REPO / "state" / "cache",
]


def _cfg_and_ledger():
    cfg = load_config(REPO / "config.yaml")
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


def cmd_preflight(_args) -> dict:
    cfg, led = _cfg_and_ledger()
    day = market.trading_day_et()
    now = market.now_et().isoformat()

    # Decisions old enough to score but not yet resolved. The orchestrator fetches
    # quotes/positions for these and feeds `tick.py reflect` (memory grounding).
    cutoff = (market.now_et().date() - timedelta(days=memory.HOLDING_DAYS)).isoformat()
    pending_outcomes = [
        {"decision_id": d["id"], "ticker": d["ticker"],
         "trade_date": d["trade_date"], "decision_price": d["decision_price"]}
        for d in led.pending_outcome_decisions(cutoff)
    ]

    out = {
        "proceed": False,
        "reason": None,
        "dry_run": cfg.dry_run,
        "intraday": cfg.intraday_enabled,
        "account_number": cfg.account_number,
        "trading_day": day,
        "now_iso": now,
        "pending": [],
        "pending_outcomes": pending_outcomes,
        "unfinalized": led.unfinalized_orders(day),
        "risk": {
            "max_dollars_per_trade": cfg.risk.max_dollars_per_trade,
            "daily_loss_halt_pct": cfg.risk.daily_loss_halt_pct,
            "daily_capital_deploy_cap": cfg.risk.daily_capital_deploy_cap,
            "max_open_position_per_ticker": cfg.risk.max_open_position_per_ticker,
            "min_buying_power_buffer": cfg.risk.min_buying_power_buffer,
        },
        "order": {
            "buy_type": cfg.buy_type,
            "time_in_force": cfg.time_in_force,
            "market_hours": cfg.market_hours,
        },
    }

    if Path(cfg.kill_switch_file).exists():
        out["reason"] = "kill_switch_present"
        return out
    if led.is_halted(day):
        out["reason"] = "daily_halt_flag_set"
        return out
    if not market.is_regular_session_open():
        out["reason"] = "market_closed"
        return out
    mso = market.minutes_since_open()
    if mso is None or mso < cfg.act_after_open_minutes:
        out["reason"] = f"too_early (minutes_since_open={mso})"
        return out

    if cfg.intraday_enabled:
        # Per-ticker eligibility: a ticker is due when its cadence timer has
        # elapsed AND it's under the daily analysis budget. Cheap-skip the rest
        # so a wake only re-analyzes what actually needs it.
        now_dt = market.now_et()
        max_an = cfg.risk.max_analyses_per_ticker_per_day

        def _due(t: str) -> bool:
            sched = led.get_schedule(day, t)
            if sched and int(sched["analyses_today"] or 0) >= max_an:
                return False
            nd = sched["next_due_ts"] if sched else None
            if not nd:
                return True
            try:
                return datetime.fromisoformat(nd) <= now_dt
            except (TypeError, ValueError):
                return True

        pending = [t for t in cfg.watchlist if _due(t)]
        no_pending = "no_tickers_due_yet (cooldown/cadence/analysis-budget)"
    else:
        # Classic once-a-day path: at most one action per ticker per day.
        pending = [t for t in cfg.watchlist if not led.already_acted(day, t)]
        no_pending = "all_watchlist_tickers_already_acted_today"

    out["pending"] = pending
    if not pending:
        out["reason"] = no_pending
        return out

    out["proceed"] = True
    out["reason"] = "ok"
    return out


def _load_input(args) -> dict:
    """Read a subcommand's --input JSON, or {} when none was given."""
    path = getattr(args, "input", None)
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _target_room(target_weights, ticker: str, held_mv: float):
    """room_under_target for a ticker, or None when there's no active target map.

    None -> resolve_buy_dollars is byte-identical to the classic path. A real value
    is one MORE min() clamp: a target can only ever REDUCE a buy, never bypass a cap.
    """
    if not target_weights:
        return None
    tw = target_weights.get(ticker)
    if not tw:
        return None
    td = _to_float(tw.get("target_dollars"))
    if td is None:
        return None
    return signals.room_under_target(td, held_mv)


def cmd_plan(args) -> dict:
    cfg, led = _cfg_and_ledger()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    return _run_plan(cfg, led, data)


def _run_plan(cfg, led, data) -> dict:
    """The deterministic plan brain (cfg/led/data injected for testability).

    BYTE-IDENTICAL to the classic path unless cfg.risk.rebalance_enabled AND a
    'target_weights' map is present in data. Then: (1) target room becomes one MORE
    min() clamp on buys (never bypasses a cap), and (2) overweight/removed holdings
    get a clamped trim/exit pass after the analyze loop. When the feature is off,
    target_weights is forced to None and neither path runs (regression-tested).
    """
    day = market.trading_day_et()
    data = data or {}
    now_iso = data.get("now_iso") or market.now_et().isoformat()
    # Strategy targets are consulted ONLY when rebalance is explicitly enabled.
    target_weights = data.get("target_weights") if cfg.risk.rebalance_enabled else None
    try:
        now_dt = datetime.fromisoformat(now_iso)
    except (TypeError, ValueError):
        now_dt = market.now_et()
    run_id = data.get("run_id") or led.new_ref_id()
    equity = float(data["equity"])
    buying_power = float(data.get("buying_power", 0.0))
    positions = data.get("positions", {}) or {}
    analyses = data.get("analyses", []) or []
    # Live Robinhood quotes {TICKER: last_price} the orchestrator fetched this wake.
    # Used as the one price source for sizing/decision_price (and Phase 5 limit/stop).
    quotes = data.get("quotes", {}) or {}

    baseline = led.get_or_create_baseline(day, equity, now_iso)
    drop_pct = (equity - baseline.baseline_equity) / baseline.baseline_equity * 100.0

    result = {
        "halt": False,
        "write_kill": False,
        "run_id": run_id,
        "intraday": cfg.intraday_enabled,
        "baseline_equity": baseline.baseline_equity,
        "equity": equity,
        "drop_pct": round(drop_pct, 3),
        "dry_run": cfg.dry_run,
        "orders": [],
        "decisions": [],
        "next_review_minutes": None,    # earliest clamped re-look (intraday); orchestrator sleeps this
        "next_wake_iso": None,
    }
    review_minutes: list = []           # per-analyzed-ticker clamped cadence (intraday)

    # Daily-loss kill-switch: breach -> halt the whole day, signal KILL write.
    if drop_pct <= -cfg.risk.daily_loss_halt_pct:
        led.mark_halted(day, f"daily_loss {drop_pct:.2f}% <= -{cfg.risk.daily_loss_halt_pct}%")
        result["halt"] = True
        result["write_kill"] = True
        return result

    remaining_daily_cap = cfg.risk.daily_capital_deploy_cap - led.day_buys_total(day)

    for a in analyses:
        ticker = str(a.get("ticker", "")).upper()
        signal = a.get("signal")
        decision = {"ticker": ticker, "signal": signal}

        if not ticker:
            continue
        if signal not in signals.VALID_SIGNALS:
            led.record_action(day, ticker, signal=str(signal), intent="skip",
                              status="error", detail=str(a.get("error", "bad signal")),
                              now_iso=now_iso)
            decision.update(status="error", detail=str(a.get("error", "bad signal")))
            result["decisions"].append(decision)
            continue

        pos = positions.get(ticker) or {}
        held_qty = float(pos.get("quantity", 0) or 0)
        held_mv = float(pos.get("market_value", 0) or 0)
        has_position = held_qty > 0
        quote = _to_float(quotes.get(ticker))
        # Prefer a live quote * held shares for the per-ticker room check if the
        # broker didn't report a market value (keeps the cap honest on fresh data).
        if held_mv == 0 and quote and held_qty:
            held_mv = quote * held_qty

        intent, frac = signals.plan_action(signal, has_position)

        # Persist the analysis decision to the ledger — the ground of record for
        # the memory scorecard. Recorded for EVERY valid signal (incl. hold/skip),
        # since directional grading applies to those too. decision_price comes
        # from a live RH quote when present (Phase 4), else the model's entry_price.
        led.record_decision(
            trade_date=day, ticker=ticker, decided_at=now_iso,
            signal=signal, intent=intent,
            position_pct=_to_float(a.get("position_pct")),
            entry_price=_to_float(a.get("entry_price")),
            stop_loss=_to_float(a.get("stop_loss")),
            next_review_hours=_to_float(a.get("next_review_hours")),
            decision_price=quote or _to_float(a.get("decision_price")) or _to_float(a.get("entry_price")),
            rationale=a.get("rationale_summary"),
            run_id=run_id,
        )

        # Cadence (intraday only): schedule this ticker's next look and count the
        # analysis against its daily budget. The model proposes next_review_hours;
        # Python clamps it (tighter ceiling while the market is open) and snaps the
        # wake out of any closed-market gap. Done for every analyzed ticker.
        if cfg.intraday_enabled:
            open_now = market.is_regular_session_open(now_dt)
            ceiling = cfg.review_ceiling_open_min if open_now else cfg.review_ceiling_min
            minutes = signals.clamp_review_minutes(
                a.get("next_review_hours"), cfg.review_floor_min, ceiling)
            next_due = market.next_market_time_et(
                now_dt + timedelta(minutes=minutes)).isoformat()
            led.bump_analysis(day, ticker, next_due_ts=next_due)
            review_minutes.append(minutes)

        if intent in ("hold", "skip"):
            detail = "hold" if intent == "hold" else "no-position (long-only, no short)"
            led.record_action(day, ticker, signal=signal, intent=intent,
                              status="skipped", detail=detail, now_iso=now_iso)
            decision.update(status="skipped", intent=intent, detail=detail)
            result["decisions"].append(decision)
            continue

        # Intraday repeat-trade backstop (buy/sell only): the daily $ caps already
        # bound exposure; these stop churn within a day — too many trades, too soon
        # after the last one, or an identical repeat of the last action.
        if cfg.intraday_enabled:
            last = led.last_trade_action(day, ticker)
            gate = None
            if not signals.within_action_cap(
                led.trade_actions_today(day, ticker), cfg.risk.max_actions_per_ticker_per_day):
                gate = "max_actions_reached"
            elif not signals.cooldown_ok(
                last["ts"] if last else None, now_iso, cfg.per_ticker_cooldown_min):
                gate = "cooldown"
            elif not signals.is_material_change(
                signal, intent,
                last["signal"] if last else None, last["intent"] if last else None):
                gate = "unchanged_since_last_action"
            if gate:
                led.record_action(day, ticker, signal=signal, intent=intent,
                                  status="skipped", detail=gate, now_iso=now_iso)
                decision.update(status="skipped", intent=intent, detail=gate)
                result["decisions"].append(decision)
                continue

        if intent == "buy":
            room = max(0.0, cfg.risk.max_open_position_per_ticker - held_mv)
            dollars, src = signals.resolve_buy_dollars(
                a.get("position_sizing"), baseline.baseline_equity, frac,
                ceiling=cfg.risk.max_dollars_per_trade,
                remaining_daily_cap=remaining_daily_cap,
                buying_power=buying_power,
                buffer=cfg.risk.min_buying_power_buffer,
                room_under_ticker_cap=room,
                position_pct=_to_float(a.get("position_pct")),
                room_under_target=_target_room(target_weights, ticker, held_mv),
            )
            if dollars <= 0:
                led.record_action(day, ticker, signal=signal, intent="buy",
                                  status="skipped", detail="sized to <=0 after clamps",
                                  now_iso=now_iso)
                decision.update(status="skipped", intent="buy", detail="sized_to_zero")
                result["decisions"].append(decision)
                continue
            if cfg.buy_type == "limit":
                # Marketable limit at the live quote + slippage; WHOLE shares only
                # (limit orders can't be fractional), so a sub-one-share budget skips.
                limit_price = signals.marketable_limit_price(quote, cfg.limit_slippage_pct)
                shares = signals.whole_shares_for_dollars(dollars, limit_price)
                if not limit_price or shares < 1:
                    detail = "limit_needs_live_quote" if not limit_price else "budget_below_one_share"
                    led.record_action(day, ticker, signal=signal, intent="buy",
                                      status="skipped", detail=detail, now_iso=now_iso)
                    decision.update(status="skipped", intent="buy", detail=detail)
                    result["decisions"].append(decision)
                    continue
                spend = round(shares * limit_price, 2)
                remaining_daily_cap -= spend
                ref_id = None
                if not cfg.dry_run:
                    ref_id = led.new_ref_id()
                    led.reserve_order(ref_id, day, ticker, side="buy", type="limit",
                                      dollar_amount=None, quantity=shares, now_iso=now_iso,
                                      order_kind="entry", limit_price=limit_price)
                result["orders"].append({
                    "ticker": ticker, "signal": signal, "intent": "buy",
                    "ref_id": ref_id, "side": "buy", "type": "limit",
                    "dollar_amount": None, "quantity": shares, "limit_price": limit_price,
                    "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                    "sizing_source": src, "order_kind": "entry",
                })
                decision.update(status="order", intent="buy", quantity=shares, limit_price=limit_price)
                result["decisions"].append(decision)
            else:
                remaining_daily_cap -= dollars
                ref_id = None
                if not cfg.dry_run:
                    ref_id = led.new_ref_id()
                    led.reserve_order(ref_id, day, ticker, side="buy", type="market",
                                      dollar_amount=dollars, quantity=None, now_iso=now_iso,
                                      order_kind="entry")
                result["orders"].append({
                    "ticker": ticker, "signal": signal, "intent": "buy",
                    "ref_id": ref_id, "side": "buy", "type": "market",
                    "dollar_amount": dollars, "quantity": None,
                    "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                    "sizing_source": src, "order_kind": "entry",
                })
                decision.update(status="order", intent="buy", dollar_amount=dollars)
                result["decisions"].append(decision)

        elif intent == "sell":
            qty = signals.resolve_sell_quantity(held_qty, frac)
            if qty <= 0:
                led.record_action(day, ticker, signal=signal, intent="sell",
                                  status="skipped", detail="nothing to sell", now_iso=now_iso)
                decision.update(status="skipped", intent="sell", detail="nothing_to_sell")
                result["decisions"].append(decision)
                continue
            # Any resting GTC protective stop MUST be cancelled before selling, or
            # the stop is left orphaned/oversized. The orchestrator cancels these
            # ref_ids first, then places the sell (and re-protects the remainder on
            # a trim via STEP 5e).
            cancel_ref_ids = [s["ref_id"] for s in led.open_protective_stops(ticker)]
            ref_id = None
            if not cfg.dry_run:
                ref_id = led.new_ref_id()
                led.reserve_order(ref_id, day, ticker, side="sell", type="market",
                                  dollar_amount=None, quantity=qty, now_iso=now_iso,
                                  order_kind="exit")
            result["orders"].append({
                "ticker": ticker, "signal": signal, "intent": "sell",
                "ref_id": ref_id, "side": "sell", "type": "market",
                "dollar_amount": None, "quantity": qty,
                "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                "order_kind": "exit", "cancel_ref_ids": cancel_ref_ids,
            })
            decision.update(status="order", intent="sell", quantity=qty)
            result["decisions"].append(decision)

    # Rebalance trim/exit pass (Stage 3): wind overweight/removed holdings toward
    # their target weight. Gated behind rebalance_enabled + a target_weights map
    # (so it never runs on the classic path). Respects the daily (date,ticker)
    # dedup and the halt, reserves a ref_id before the broker call like every
    # other order, and cancels any resting protective stop first. Sizing is
    # Python-clamped; the orchestrator only executes what lands in result["orders"].
    if target_weights and not result["halt"]:
        handled = {d.get("ticker") for d in result["decisions"]}
        for raw_ticker, tw in target_weights.items():
            ticker = str(raw_ticker).upper()
            if ticker in handled or led.already_acted(day, ticker):
                continue
            if tw.get("intent") not in ("trim", "exit"):
                continue
            pos = positions.get(ticker) or {}
            held_qty = float(pos.get("quantity", 0) or 0)
            if held_qty <= 0:
                continue  # nothing held to trim/exit
            quote = _to_float(quotes.get(ticker))
            held_mv = float(pos.get("market_value", 0) or 0)
            if held_mv == 0 and quote and held_qty:
                held_mv = quote * held_qty
            full_exit = tw.get("intent") == "exit"
            qty = signals.resolve_target_sell_quantity(
                held_qty, quote, held_mv, _to_float(tw.get("target_dollars")) or 0.0,
                full_exit=full_exit)
            if qty <= 0:
                continue
            cancel_ref_ids = [s["ref_id"] for s in led.open_protective_stops(ticker)]
            order_kind = "rebalance_exit" if full_exit else "rebalance_trim"
            ref_id = None
            if not cfg.dry_run:
                ref_id = led.new_ref_id()
                led.reserve_order(ref_id, day, ticker, side="sell", type="market",
                                  dollar_amount=None, quantity=qty, now_iso=now_iso,
                                  order_kind=order_kind)
            result["orders"].append({
                "ticker": ticker, "signal": "REBALANCE", "intent": "sell",
                "ref_id": ref_id, "side": "sell", "type": "market",
                "dollar_amount": None, "quantity": qty,
                "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                "order_kind": order_kind, "cancel_ref_ids": cancel_ref_ids,
            })
            result["decisions"].append({
                "ticker": ticker, "signal": "REBALANCE", "status": "order",
                "intent": "sell", "quantity": qty, "detail": order_kind})

    # Loop cadence: wake at the soonest re-look the model asked for (clamped),
    # snapped to market hours. The orchestrator schedules the next tick at this.
    if review_minutes:
        soonest = min(review_minutes)
        result["next_review_minutes"] = round(soonest, 1)
        result["next_wake_iso"] = market.next_market_time_et(
            now_dt + timedelta(minutes=soonest)).isoformat()

    return result


# --- strategy layer subcommands (Stage 3) -----------------------------------

def cmd_strategy_set(args) -> dict:
    cfg, led = _cfg_and_ledger()
    return _run_strategy_set(cfg, led, _load_input(args))


def _run_strategy_set(cfg, led, data) -> dict:
    """Write the active goal + target book from strategy.yaml into the ledger.

    STRICT: load_strategy validate-or-raise (a bad book fails loudly at setup).
    Snapshots start_equity (from --input) + start_date (today). Supersedes any
    prior goal and re-upserts the targets idempotently. Places NO orders."""
    import lib.strategy as strategy
    sc = strategy.load_strategy(cfg.strategy_path)
    day = market.trading_day_et()
    now_iso = data.get("now_iso") or market.now_et().isoformat()
    equity = _to_float(data.get("equity"))
    book = sc.book(sc.default_book)
    gid = led.set_strategy_goal(
        created_at=now_iso, target_return_pct=sc.goal.target_return_pct,
        horizon_months=sc.goal.horizon_months, benchmark=sc.goal.benchmark,
        benchmark_annual_pct=sc.goal.benchmark_annual_pct,
        constraint_note=sc.goal.constraint, macro_thesis_version=sc.macro_thesis.version,
        macro_thesis_json=json.dumps({"summary": sc.macro_thesis.summary,
                                      "correlation_note": sc.macro_thesis.correlation_note}),
        active_book=sc.default_book, as_of=sc.macro_thesis.version,
        start_date=day, start_equity=equity)
    for h in book.holdings:
        led.upsert_target_holding(
            goal_id=gid, sleeve=h.sleeve, ticker=h.ticker, target_weight=h.weight,
            band=h.band, status="active", book=sc.default_book, quotable=h.quotable,
            proxy_ticker=h.proxy_ticker, updated_at=now_iso)
    return {"goal_id": gid, "active_book": sc.default_book, "holdings": len(book.holdings),
            "start_equity": equity, "start_date": day}


def cmd_construct(args) -> dict:
    cfg, led = _cfg_and_ledger()
    return _run_construct(cfg, led, _load_input(args))


def _run_construct(cfg, led, data) -> dict:
    """Between preflight and plan: emit the deterministic target_weights the plan
    consumes. Reads the active goal + targets + the broker snapshot (--input:
    equity/positions/quotes + optional macro_reading). proceed:false when there is
    no active goal (the safe fallback -> plan runs the classic path). NO orders."""
    import lib.strategy as strategy
    import lib.portfolio as portfolio
    goal = led.get_active_goal()
    if not goal:
        return {"proceed": False, "reason": "no active strategy goal", "target_weights": {}}
    equity = _to_float(data.get("equity")) or 0.0
    positions_in = data.get("positions", {}) or {}
    positions_mv = {str(tk).upper(): (_to_float((pos or {}).get("market_value")) or 0.0)
                    for tk, pos in positions_in.items()}
    # The recommended book from the operator macro reading (advisory: the executable
    # targets still come from the ledger's current book; switching to dial-up is a
    # gated universe change, not an automatic construct-time swap).
    book_name, book_reason = goal["active_book"], "active goal book"
    if cfg.strategy is not None:
        book_name, book_reason = strategy.select_active_book(cfg.strategy, data.get("macro_reading"))
    targets = led.active_target_portfolio(goal["id"], statuses=("active", "exiting"))
    rows = portfolio.construct_target_book(
        targets, positions_mv, equity, cash_sleeve_ticker=cfg.risk.cash_sleeve_ticker)
    target_weights = {r["ticker"]: {"intent": r["intent"], "target_dollars": r["target_dollars"],
                                    "target_weight": r["target_weight"],
                                    "delta_dollars": r.get("delta_dollars"),
                                    "quotable": r["quotable"]} for r in rows}
    return {"proceed": True, "goal_id": goal["id"], "active_book": goal["active_book"],
            "recommended_book": book_name, "book_reason": book_reason,
            "target_weights": target_weights}


def cmd_goal_track(args) -> dict:
    cfg, led = _cfg_and_ledger()
    return _run_goal_track(cfg, led)


def _run_goal_track(cfg, led) -> dict:
    """Best-effort goal-progress snapshot (never stops a tick). Records a
    goal_tracking row from the ledger equity curve + the active goal."""
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
    return {"recorded": True, "goal_id": goal["id"], **prog}


def cmd_learn_review(args) -> dict:
    cfg, led = _cfg_and_ledger()
    return _run_learn_review(cfg, led, _load_input(args))


def _run_learn_review(cfg, led, data) -> dict:
    """Best-effort (never stops a tick): score holdings vs thesis, record proof-
    bearing proposals (deduped), update thesis_state, expire stale proposals. The
    risky direction is human-gated; this records, it does NOT mutate the universe
    or place orders."""
    import lib.learn as learn
    import lib.goal as goal_mod
    import lib.strategy as strategy
    goal = led.get_active_goal()
    if not goal or cfg.strategy is None:
        return {"reviewed": False, "reason": "no active goal / strategy layer inactive"}
    learning = cfg.strategy.learning
    macro_regime = strategy.regime_label(cfg.strategy, data.get("macro_reading"))
    progress = goal_mod.compute_from_ledger(led, goal)
    now_iso = market.now_et().isoformat()
    day = market.trading_day_et()
    ps = learn.build_proposals(led, goal["id"], learning, macro_regime, progress)
    recorded = []
    new_proposals = []
    for p in ps["all"]:
        rid = led.record_universe_proposal(
            goal_id=goal["id"], proposed_at=now_iso, kind=p.kind, ticker=p.ticker,
            sleeve=p.sleeve, from_book=p.from_book, to_book=p.to_book,
            target_weight=p.target_weight, tier=p.tier, content_hash=p.content_hash(),
            reason=p.reason, goal_gap_pct=p.goal_gap_pct)
        if rid:
            recorded.append(rid)
            new_proposals.append(p)
    # The agent maintains STRATEGY.md: append each NEW proposal as a dated learning.
    # Best-effort — a doc-write hiccup must never stop the tick. Risky changes still
    # require approval; this only records what was learned, it never mutates weights.
    try:
        import lib.strategy_doc as strategy_doc
        for p in new_proposals:
            strategy_doc.append_learning(
                f"[{p.kind}] {p.ticker or 'BOOK'} ({p.tier}) — {p.reason}", date=day)
    except Exception:  # noqa: BLE001
        pass
    regime_word = {"STAND_DOWN": "standdown", "DEPLOY": "deploy"}.get(macro_regime, "neutral")
    led.upsert_thesis_state(goal_id=goal["id"], as_of=day, regime=regime_word,
                            active_book=goal["active_book"], last_trigger=macro_regime,
                            last_macro_json=json.dumps(data.get("macro_reading") or {}),
                            updated_at=now_iso)
    cutoff = (datetime.fromisoformat(now_iso) - timedelta(days=learning.proposal_expiry_days)).isoformat()
    expired = led.expire_old_proposals(goal["id"], cutoff)
    return {"reviewed": True, "macro_regime": macro_regime, "proposals": len(ps["all"]),
            "needs_approval": len(ps["needs_approval"]), "auto_apply_eligible": len(ps["auto_apply"]),
            "recorded_ids": recorded, "expired": expired}


def cmd_universe_apply(args) -> dict:
    cfg, led = _cfg_and_ledger()
    return _run_universe_apply(cfg, led, change_id=int(args.id),
                               approve=bool(getattr(args, "approve", False)))


def _run_universe_apply(cfg, led, *, change_id, approve) -> dict:
    """Apply one universe-change proposal (out-of-band, human/config gated). Places
    NO orders — a REMOVE sets the holding to 'exiting' so the next construct->plan
    winds it to zero via the clamped sell path. The risky direction needs --approve
    unless its auto flag is on; the safe de-risk direction needs the derisk flag."""
    change = led.get_universe_change(change_id)
    if not change or change["status"] != "proposed":
        return {"applied": False, "reason": "no such open proposal"}
    learning = cfg.strategy.learning if cfg.strategy is not None else None
    tier = change["tier"]
    auto_ok = bool(learning and (
        (tier == "derisk" and learning.auto_apply_derisk) or
        (tier == "universe" and learning.auto_apply_universe_changes)))
    if not approve and not auto_ok:
        flag = "auto_apply_derisk" if tier == "derisk" else "auto_apply_universe_changes"
        return {"applied": False, "reason": f"tier '{tier}' requires --approve ({flag} is off)",
                "change": change}
    now_iso = market.now_et().isoformat()
    goal = led.get_active_goal()
    effect = "recorded"
    if change["kind"] == "PROPOSE_REMOVE" and change.get("ticker") and goal:
        led.set_holding_status(goal["id"], change["ticker"], "exiting", now_iso)
        effect = "holding set to exiting (rebalancer winds to zero; freed dollars -> cash)"
    led.mark_universe_change(change_id, "applied", now_iso, "operator" if approve else "auto")
    return {"applied": True, "change_id": change_id, "kind": change["kind"],
            "ticker": change.get("ticker"), "effect": effect,
            "via": "operator" if approve else "auto"}


def cmd_commit(args) -> dict:
    led = Ledger(LEDGER_DB)
    day = market.trading_day_et()
    now_iso = market.now_et().isoformat()
    d = json.loads(Path(args.input).read_text(encoding="utf-8"))

    ref_id = d.get("ref_id")
    if ref_id:
        led.finalize_order(ref_id, d.get("broker_order_id"),
                           json.dumps(d.get("result_json", {})))
        # Advance the order's lifecycle state. A protective stop that was placed (or
        # simulated in dry-run) becomes 'stop_placed' so open_protective_stops finds
        # it for later cancel-on-sell; entries/exits become 'filled'.
        order = led.get_order(ref_id)
        kind = (order or {}).get("order_kind", "entry")
        state_map = {"placed": "filled", "dry_run": "filled", "cancelled": "cancelled",
                     "blocked_guardrail": "unfilled", "error": "unfilled"}
        st = state_map.get(d["status"], d["status"])
        if kind == "protective_stop" and d["status"] in ("placed", "dry_run"):
            st = "stop_placed"
        led.set_order_state(ref_id, st)
    ticker = str(d["ticker"]).upper()
    led.record_action(
        day, ticker,
        signal=d.get("signal", ""), intent=d.get("intent", ""),
        status=d["status"], detail=str(d.get("detail", "")), now_iso=now_iso,
    )
    # Append to the action event log (cooldown / action-cap / on-change history).
    # The gates count only completed trades (intent buy|sell, status placed|dry_run);
    # blocked/error attempts are logged but don't start a cooldown or burn the cap.
    led.record_event(
        trade_date=day, ticker=ticker, run_id=d.get("run_id"), ts=now_iso,
        signal=d.get("signal", ""), intent=d.get("intent", ""),
        status=d["status"], detail=str(d.get("detail", "")),
        dollar_amount=_to_float(d.get("dollar_amount")),
    )
    return {"ok": True, "ticker": ticker, "status": d["status"]}


# --- email digest ------------------------------------------------------------
# Observability only: BUILD the email + DECIDE whether it's new here in Python;
# the orchestrator does the actual Resend MCP send and then calls report-commit.
# Email is best-effort — a report error must NEVER abort or alter a trading tick.

def _read_reasoning(date: str, ticker: str):
    """Pull the curated decision + debate text from the audit dump (best-effort)."""
    path = ANALYZE_LOGS / f"{date}_{ticker}.json"
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/garbled audit log is non-fatal
        return None, None
    return j.get("final_trade_decision"), j.get("investment_plan")


def _account_risk(led) -> dict:
    """Account-equity drawdown + daily Sharpe for the digest (T1). Equity is in
    scope HERE (the digest path); it is deliberately NEVER read on the analysis/
    memory path. Best-effort: returns empty fields if the series is too thin."""
    try:
        from lib import risk  # local: analysis-side math reused for an operator metric
        eq = [r["baseline_equity"] for r in led.baseline_equity_series()
              if r.get("baseline_equity") is not None]
        dd = risk.max_drawdown(eq, kind="equity") if eq else None
        rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1]]
        sh = risk.sharpe(rets) if rets else None
        return {
            "drawdown_pct": (dd.value * 100.0) if (dd and dd.value is not None) else None,
            "drawdown_proof": dd.proof() if dd else "",
            "sharpe": sh.value if (sh and sh.value is not None) else None,
            "sharpe_n": sh.n if sh else 0,
        }
    except Exception:  # noqa: BLE001 — observability only; never break the digest
        return {}


def _build_report_model(cfg, led, data: dict, date: str, now_iso: str, kind: str) -> dict:
    """Assemble the render model from committed ledger truth + the plan output.

    Per-ticker spine = the plan's decisions/orders (intent, signal, sized $);
    the ledger's committed ticker_action overlays the REAL outcome (placed /
    dry_run / blocked_guardrail / error) and broker ids. All reads are
    null-safe so the auth_error path (which stops before any ledger write)
    still renders a minimal alert.
    """
    plan = data.get("plan") or {}
    baseline = led.get_baseline(date)
    actions = led.day_actions(date)
    orders = led.day_orders(date)

    equity = data.get("equity")
    if equity is None:
        equity = plan.get("equity")
    baseline_equity = baseline.baseline_equity if baseline else plan.get("baseline_equity")
    drop_pct = plan.get("drop_pct")
    if drop_pct is None and equity is not None and baseline_equity:
        drop_pct = (float(equity) - float(baseline_equity)) / float(baseline_equity) * 100.0
    halted = baseline.halted if baseline else bool(plan.get("halt"))
    halt_reason = baseline.halt_reason if baseline else None

    def _blank(t):
        return {"ticker": t, "signal": None, "intent": None, "status": None,
                "detail": None, "amount": None, "qty": None, "side": None,
                "broker_order_id": None}

    rows: dict = {}
    for d in plan.get("decisions", []):
        t = str(d.get("ticker", "")).upper()
        if not t:
            continue
        r = rows.setdefault(t, _blank(t))
        r.update(signal=d.get("signal"), intent=d.get("intent"),
                 status=d.get("status"), detail=d.get("detail"),
                 amount=d.get("dollar_amount"), qty=d.get("quantity"))
    for o in plan.get("orders", []):
        t = str(o.get("ticker", "")).upper()
        r = rows.setdefault(t, _blank(t))
        r.update(signal=r.get("signal") or o.get("signal"),
                 intent=r.get("intent") or o.get("intent"),
                 amount=o.get("dollar_amount"), qty=o.get("quantity"), side=o.get("side"))
    # Committed ledger rows are the source of truth for what actually happened.
    for a in actions:
        t = str(a.get("ticker", "")).upper()
        r = rows.setdefault(t, _blank(t))
        r["status"] = a.get("status") or r.get("status")
        r["detail"] = a.get("detail") or r.get("detail")
        r["signal"] = r.get("signal") or a.get("signal")
        r["intent"] = r.get("intent") or a.get("intent")
    for o in orders:
        t = str(o.get("ticker", "")).upper()
        r = rows.get(t)
        if not r:
            continue
        if o.get("broker_order_id"):
            r["broker_order_id"] = o.get("broker_order_id")
        if r.get("amount") is None and o.get("dollar_amount") is not None:
            r["amount"] = o.get("dollar_amount")
        if r.get("qty") is None and o.get("quantity") is not None:
            r["qty"] = o.get("quantity")
        r["side"] = r.get("side") or o.get("side")

    for t, r in rows.items():
        decision, debate = _read_reasoning(date, t)
        r["decision"], r["debate"] = decision, debate

    return {
        "date": date,
        "now_iso": now_iso,
        "kind": kind,
        "dry_run": bool(plan.get("dry_run", cfg.dry_run)),
        "subject_prefix": cfg.notify.subject_prefix,
        "equity": equity,
        "baseline_equity": baseline_equity,
        "drop_pct": drop_pct,
        "halted": halted,
        "halt_reason": halt_reason,
        "event_detail": data.get("event_detail"),
        "account_risk": _account_risk(led),
        "tickers": [rows[t] for t in sorted(rows)],
    }


def cmd_report(args) -> dict:
    cfg, led = _cfg_and_ledger()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    date = data.get("date") or market.trading_day_et()
    now_iso = data.get("now_iso") or market.now_et().isoformat()
    kind = data.get("kind", "digest")

    if not cfg.notify.enabled:
        return {"should_send": False, "reason": "notify_disabled", "kind": kind}

    model = _build_report_model(cfg, led, data, date, now_iso, kind)
    digest = notify.build_digest(model)
    already = led.last_notified_hash(date, kind)
    should_send = digest["content_hash"] != already
    return {
        "should_send": should_send,
        "reason": "new" if should_send else "already_sent",
        "kind": kind,
        "date": date,
        "from": cfg.notify.from_addr,
        "recipients": cfg.notify.to,
        "subject": digest["subject"],
        "html": digest["html"],
        "text": digest["text"],
        "content_hash": digest["content_hash"],
    }


def cmd_report_commit(args) -> dict:
    """Record a digest as sent — AFTER the orchestrator confirms delivery."""
    led = Ledger(LEDGER_DB)
    now_iso = market.now_et().isoformat()
    led.mark_notified(args.date, args.kind, args.content_hash,
                      args.recipients or "", now_iso)
    return {"ok": True, "date": args.date, "kind": args.kind, "hash": args.content_hash}


# --- protective stop (Phase 5) -----------------------------------------------
# After an entry FILLS, the orchestrator calls this with the fill price + qty; we
# return a Python-clamped gtc stop_market sell to place (the model's stop_loss only
# seeds the price). Also used after a TRIM to re-protect the remaining shares. The
# returned stop is reserved (ref_id) and tracked so it can be cancelled before any
# later sell. Returns {"stop": null} when stops are disabled or inputs are unusable.

def cmd_protect(args) -> dict:
    cfg, led = _cfg_and_ledger()
    day = market.trading_day_et()
    now_iso = market.now_et().isoformat()
    d = json.loads(Path(args.input).read_text(encoding="utf-8"))

    if not cfg.protective_stop_enabled:
        return {"ok": True, "stop": None, "reason": "protective_stop_disabled"}

    ticker = str(d.get("ticker", "")).upper()
    fill_price = _to_float(d.get("fill_price"))
    fill_qty = _to_float(d.get("fill_qty"))
    stop_price = signals.resolve_stop_price(
        fill_price, _to_float(d.get("model_stop_loss")), cfg.protective_stop_pct)
    if not ticker or not fill_qty or fill_qty <= 0 or stop_price is None:
        return {"ok": True, "stop": None, "reason": "no_stop (need ticker, fill qty, valid price)"}

    ref_id = None
    if not cfg.dry_run:
        ref_id = led.new_ref_id()
        led.reserve_order(ref_id, day, ticker, side="sell", type="stop_market",
                          dollar_amount=None, quantity=fill_qty, now_iso=now_iso,
                          order_kind="protective_stop", stop_price=stop_price,
                          parent_ref_id=d.get("ref_id"), state="reserved")
    return {"ok": True, "stop": {
        "ticker": ticker, "ref_id": ref_id, "side": "sell", "type": "stop_market",
        "quantity": fill_qty, "stop_price": stop_price,
        "time_in_force": cfg.protective_stop_tif, "market_hours": "regular_hours",
        "order_kind": "protective_stop", "parent_ref_id": d.get("ref_id"),
    }}


# --- decision-memory outcome resolution --------------------------------------
# Grounds the memory scorecard in REAL market data: the orchestrator passes a
# snapshot (current quote + position market value/cost basis + any realized P&L)
# for each decision preflight flagged as pending_outcomes; Python computes the
# directional return (decision_price -> price_now) and position-level P&L and
# writes the outcome row. tick.py stays offline — all market data is passed in.
#
# `reflect` input JSON:
#   {"resolutions": [
#       {"decision_id": 12, "price_now": 196.4,
#        "position_market_value": 48.0, "position_cost_basis": 50.0,   # optional
#        "realized_pnl": 0.0, "benchmark_return": 0.004}               # optional
#   ]}

def cmd_reflect(args) -> dict:
    led = Ledger(LEDGER_DB)
    now_iso = market.now_et().isoformat()
    today = market.now_et().date()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    resolutions = data.get("resolutions", []) or []

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
        bench = _to_float(r.get("benchmark_return"))
        alpha = (dret - bench) if (dret is not None and bench is not None) else None
        scored = "both" if (unrealized is not None or realized is not None) else "directional"

        led.record_outcome(
            did, resolved_at=now_iso, holding_days=holding_days,
            directional_return=dret, benchmark_return=bench, alpha=alpha,
            realized_pnl=realized, unrealized_pnl=unrealized, scored_against=scored,
        )
        affected.add(dec.get("ticker"))
        results.append({"decision_id": did, "status": "resolved",
                        "directional_return": dret, "scored_against": scored})

    out = {
        "ok": True,
        "resolved": sum(1 for x in results if x["status"] == "resolved"),
        "results": results,
    }
    # Best-effort: refresh the reflective-memory metric blocks for the resolved
    # tickers + portfolio.md now that new outcomes landed. A memory error must NEVER
    # fail the tick (reflect is best-effort) — surface it in the output instead.
    if affected:
        try:
            cfg = load_config(REPO / "config.yaml")
            from lib import reflect_memory
            out["memory_update"] = reflect_memory.update_after_outcome(
                led, affected, cfg.memory, cfg.memory.dir, now_label=now_iso)
        except Exception as e:  # noqa: BLE001 — observability only, never blocks the tick
            out["memory_update_error"] = str(e)
    return out


# --- storage retention -------------------------------------------------------
# Best-effort housekeeping: age out bulky reconstructable artifacts past the
# retention window, optionally offloading first (S3 backend deferred). Like the
# digest, this is observability only and must NEVER abort or alter a trading
# tick — prune_dir/get_archiver never raise.

def cmd_prune(_args) -> dict:
    cfg = load_config(REPO / "config.yaml")
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


# --- reflective memory: proof tool + rebuild ---------------------------------
# Read-only/observability. memory-show prints the LIVE-recomputed risk math next
# to the on-disk markdown so the user can confirm the file matches the formulas;
# memory-rebuild regenerates every file from the ledger (also backfills history).

def cmd_memory_show(args) -> dict:
    cfg = load_config(REPO / "config.yaml")
    led = Ledger(LEDGER_DB)
    from lib import reflect_memory
    ticker = (getattr(args, "ticker", "") or "").strip().upper()
    out = {"ok": True, "enabled": cfg.memory.enabled, "dir": cfg.memory.dir, "ticker": ticker or None}
    bundle = reflect_memory.build_metric_bundle(led, ticker or "PORTFOLIO", cfg.memory)
    if ticker:
        out["ticker_block"] = reflect_memory.render_metric_block(bundle["ticker"])
        tpath = Path(cfg.memory.dir) / "tickers" / f"{ticker}.md"
        out["ticker_file"] = tpath.read_text(encoding="utf-8") if tpath.exists() else None
    out["portfolio_block"] = reflect_memory.render_metric_block(bundle["portfolio"])
    ppath = Path(cfg.memory.dir) / "portfolio.md"
    out["portfolio_file"] = ppath.read_text(encoding="utf-8") if ppath.exists() else None
    return out


def cmd_memory_rebuild(_args) -> dict:
    cfg = load_config(REPO / "config.yaml")
    led = Ledger(LEDGER_DB)
    from lib import reflect_memory
    now_iso = market.now_et().isoformat()
    summary = reflect_memory.rebuild_all(led, cfg.memory, cfg.memory.dir, now_label=now_iso)
    return {"ok": True, "dir": cfg.memory.dir, **summary}


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--input", required=True)
    p_commit = sub.add_parser("commit")
    p_commit.add_argument("--input", required=True)
    p_report = sub.add_parser("report")
    p_report.add_argument("--input", required=True)
    p_rc = sub.add_parser("report-commit")
    p_rc.add_argument("--date", required=True)
    p_rc.add_argument("--kind", default="digest")
    p_rc.add_argument("--hash", required=True, dest="content_hash")
    p_rc.add_argument("--recipients", default="")
    p_reflect = sub.add_parser("reflect")
    p_reflect.add_argument("--input", required=True)
    p_protect = sub.add_parser("protect")
    p_protect.add_argument("--input", required=True)
    p_ss = sub.add_parser("strategy-set")
    p_ss.add_argument("--input", required=False)
    p_con = sub.add_parser("construct")
    p_con.add_argument("--input", required=False)
    sub.add_parser("goal-track")
    p_lr = sub.add_parser("learn-review")
    p_lr.add_argument("--input", required=False)
    p_ua = sub.add_parser("universe-apply")
    p_ua.add_argument("--id", required=True)
    p_ua.add_argument("--approve", action="store_true")
    sub.add_parser("prune")
    p_ms = sub.add_parser("memory-show")
    p_ms.add_argument("--ticker", default="")
    sub.add_parser("memory-rebuild")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "preflight":
            out = cmd_preflight(args)
        elif args.cmd == "plan":
            out = cmd_plan(args)
        elif args.cmd == "commit":
            out = cmd_commit(args)
        elif args.cmd == "report":
            out = cmd_report(args)
        elif args.cmd == "report-commit":
            out = cmd_report_commit(args)
        elif args.cmd == "reflect":
            out = cmd_reflect(args)
        elif args.cmd == "protect":
            out = cmd_protect(args)
        elif args.cmd == "strategy-set":
            out = cmd_strategy_set(args)
        elif args.cmd == "construct":
            out = cmd_construct(args)
        elif args.cmd == "goal-track":
            out = cmd_goal_track(args)
        elif args.cmd == "learn-review":
            out = cmd_learn_review(args)
        elif args.cmd == "universe-apply":
            out = cmd_universe_apply(args)
        elif args.cmd == "prune":
            out = cmd_prune(args)
        elif args.cmd == "memory-show":
            out = cmd_memory_show(args)
        elif args.cmd == "memory-rebuild":
            out = cmd_memory_rebuild(args)
        else:  # unreachable
            raise SystemExit(2)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e), "cmd": args.cmd}))
        return 1

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
