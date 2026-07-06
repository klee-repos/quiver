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
import os
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
from lib import universe  # noqa: E402

# Paths default to the repo's state/config but accept env overrides so an isolated
# end-to-end test (or an alternate deployment) can point the SAME CLI at a temp DB +
# temp config WITHOUT touching live trading state. Production sets neither var.
LEDGER_DB = Path(os.environ.get("QUIVER_LEDGER_DB") or (REPO / "state" / "ledger.db"))
CONFIG_PATH = Path(os.environ.get("QUIVER_CONFIG") or (REPO / "config.yaml"))
ANALYZE_LOGS = REPO / "state" / "analyze_logs"

# Unique machine sentinel emitted by `auth-stop` ONLY. It must never appear in
# TICK.md prose (the runbook names the COMMAND, never echoes this token), so it
# lands in the supervisor's captured stdout (run_tick.py `combined`) only when the
# command actually executed on a real broker 401 — making the AUTH_ERROR detector
# collision-free with the literal "AUTH_ERROR" that TICK.md legitimately contains.
AUTH_STOP_SENTINEL = "QUIVER_AUTH_STOP"
# Bulky, reconstructable artifacts the retention sweep ages out (state of record
# is the ledger, never these). Kept here so `prune` and any future caller agree.
PRUNE_TARGETS = [
    REPO / "logs" / "reasoning",
    REPO / "state" / "analyze_logs",
    REPO / "state" / "results",
    REPO / "state" / "cache",
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


def _analysis_universe(cfg, led) -> list:
    """The day's universe of tickers to analyze = the active portfolio book's
    engine names. Replaces the old hand-maintained `watchlist`: the universe is
    now DERIVED from the defined portfolio so the two can never drift apart.

    Source of truth, in order:
      1. the ledger's active goal target book (runtime state — reflects any
         continual-learning universe changes), when a goal has been seeded;
      2. else strategy.yaml's default book (the committed source), so the bot is
         live the moment a valid strategy.yaml exists, before `strategy-set` runs;
      3. else empty — no portfolio defined, so the tick safely no-ops (fail-safe:
         no universe, no trades).
    The cash residual and non-quotable names are filtered out (lib.universe.
    tradable_universe), matching how `construct` builds the executable book.
    """
    goal = led.get_active_goal()
    if goal:
        rows = led.active_target_portfolio(goal["id"], statuses=("active",))
    elif cfg.strategy is not None:
        book = cfg.strategy.book(cfg.strategy.default_book)
        rows = [{"ticker": h.ticker, "sleeve": h.sleeve, "quotable": h.quotable,
                 "status": "active"} for h in book.holdings]
    else:
        rows = []
    return universe.tradable_universe(rows, cfg.risk.cash_sleeve_ticker)


def _has_winddown_work(cfg, led, *, include_reconcile: bool) -> bool:
    """Pending SELL work that needs a tick even when there's nothing to analyze, so
    preflight keeps the tick alive. The exits live in `cmd_plan`'s sell pass, which
    only runs AFTER preflight proceeds. Dormant with no active goal (classic path
    unchanged). Two sources:

      * rebalance_enabled + an `exiting` in-book holding -> Stage 3 wind-down. A
        cheap LEDGER fact, so it gates BOTH preflight no-op checks.
      * reconcile_unmanaged + an active goal -> a held off-book position may need a
        reconcile-exit. Positions aren't known at preflight (no snapshot yet), so
        this can't be conditioned on an actual orphan; it is honored only at the
        EMPTY-universe gate (``include_reconcile=True``) — an all-cash / fully
        wound-down book has no analysis tick to carry the sell, so reconcile must
        keep it alive. It is NOT honored at the all-acted gate: a normal book
        already did its sell work on the day's first (pending) tick, and keeping
        every later wake alive just to re-check would defeat the cheap hourly no-op.
        (An orphan introduced AFTER all in-book names acted that day self-heals on
        the next session — the conservative, fails-to-sell direction.)
    """
    goal = led.get_active_goal()
    if not goal:
        return False
    if include_reconcile and cfg.risk.reconcile_unmanaged:
        return True
    if cfg.risk.rebalance_enabled:
        return bool(led.active_target_portfolio(goal["id"], statuses=("exiting",)))
    return False


def cmd_preflight(_args) -> dict:
    cfg, led = _cfg_and_ledger()
    return _run_preflight(cfg, led)


def _run_preflight(cfg, led) -> dict:
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

    # The universe is the active portfolio book (no hand-maintained watchlist). An
    # empty universe means no portfolio is defined yet -> safe no-op, not an error
    # UNLESS plan still has SELL wind-down work (rebalance `exiting` holdings, or a
    # reconcile-exit of an off-book holding) — those aren't in the analysis universe
    # but must still be sold; keep the tick alive so construct -> plan can place them.
    book_universe = _analysis_universe(cfg, led)
    if not book_universe:
        # No analyzable engine names (no goal/strategy, or an all-cash / fully
        # wound-down book). There is no analysis tick to carry a sell, so keep the
        # tick alive ONLY if there is wind-down SELL work — a reconcile-exit of an
        # off-book holding or a rebalance `exiting` holding (include_reconcile=True);
        # otherwise it's a safe no-op, not an error.
        if out["unfinalized"] or _has_winddown_work(cfg, led, include_reconcile=True):
            out["proceed"] = True
            out["pending"] = []
            out["reason"] = "winddown/reconcile_only (unfinalized orders / rebalance / reconcile sells; no analysis universe)"
            return out
        out["reason"] = "no_portfolio_universe (set strategy.yaml / run strategy-set)"
        return out

    # A name with an order RESERVED but never finalized (a crash between place and
    # commit) must NOT be re-analyzed/re-planned — that would mint a SECOND ref_id and
    # double-place. It is reconciled by STEP 6 against its EXISTING ref_id instead, so
    # exclude it from `pending` here (the `unfinalized` list still carries it to STEP 6).
    unfinalized_tickers = {str(o["ticker"]).upper() for o in out["unfinalized"]}

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

        pending = [t for t in book_universe if _due(t) and t not in unfinalized_tickers]
        no_pending = "no_tickers_due_yet (cooldown/cadence/analysis-budget)"
    else:
        # Classic once-a-day path: at most one action per ticker per day.
        pending = [t for t in book_universe
                   if not led.already_acted(day, t) and t not in unfinalized_tickers]
        no_pending = "all_portfolio_tickers_already_acted_today"

    out["pending"] = pending
    # All-acted gate (book HAS engines, but all acted today): a normal book already
    # did its sell work on the day's first (pending) tick, so reconcile does NOT keep
    # every later wake alive here (include_reconcile=False) — only a cheap-to-detect
    # rebalance `exiting` holding does. (An orphan introduced after all names acted
    # self-heals on the next session — the conservative, fails-to-sell direction.)
    if (not pending and not out["unfinalized"]
            and not _has_winddown_work(cfg, led, include_reconcile=False)):
        out["reason"] = no_pending
        return out

    out["proceed"] = True
    # An empty `pending` here means a reconcile-only or rebalance `exiting` wind-down
    # tick: nothing to analyze, but STEP 6 reconcile / construct -> plan's sell pass
    # still has work (unfinalized orders to reconcile or exits to place).
    out["reason"] = ("ok" if pending else
                     "winddown/reconcile_only (unfinalized orders / rebalance exiting sells; no new analysis)")
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


def _plan_trigger(prior, new_intent, quote, loss_catalyst_pct):
    """Python-VERIFIED reason a buy<->sell REVERSAL executes the recorded plan, or None.

    Only the substantive, price-event exits ground a reversal (Component A G1): a SELL
    after a recorded BUY that breaches the recorded stop, takes a real loss, or hits the
    recorded target. The model's word is never trusted here — every trigger is checked
    against the live quote + the prior decision's recorded numbers. Re-entries (buy after
    sell) and target-less profit-taking carry NO auto-trigger; they must be grounded by a
    declared basis change instead (so a short review horizon can't make the gate toothless).
    """
    if not prior:
        return None
    prior_intent = prior.get("intent")
    if not signals.is_reversal(prior_intent, new_intent):
        return None
    q = _to_float(quote)
    if new_intent == "sell" and signals.direction_of(prior_intent) == "open":
        stop = _to_float(prior.get("stop_loss"))
        if q is not None and stop and q <= stop:
            return "stop_hit"
        dp = _to_float(prior.get("decision_price"))
        if q is not None and dp and dp > 0 and (q - dp) / dp <= -(loss_catalyst_pct / 100.0):
            return "loss_catalyst"
        tgt = _to_float(prior.get("target_price"))
        if q is not None and tgt and q >= tgt:
            return "target_hit"
    return None


def _consistency_context(cfg, led, ticker, new_intent, quote, new_basis,
                         decision_price, position_pct):
    """Assemble the cross-day consistency verdict + its proof for one ticker.

    Reads the prior completed-trade stance + the recent completed-trade flip history,
    computes the Python-verified plan_trigger, and returns
    (allowed, reason, plan_trigger, proof_dict). Degrades to ALLOW on any read error
    (the gate goes off for this ticker this tick rather than blocking a legit trade —
    the daily $ caps + dedup still bound exposure); never raises into the tick.
    """
    proof: dict = {"decision_price": decision_price, "position_pct": position_pct}
    try:
        prior = led.last_completed_trade(ticker)
        prior_intent = (prior or {}).get("intent")
        prior_basis = (prior or {}).get("basis")
        reversal = signals.is_reversal(prior_intent, new_intent)
        plan_trigger = _plan_trigger(prior, new_intent, quote, cfg.risk.loss_catalyst_pct)
        basis_changed = bool(new_basis) and (new_basis != prior_basis)
        recent = led.recent_completed_trades(ticker, cfg.risk.consistency_flip_window)
        recent_flips = signals.count_discretionary_reversals(list(reversed(recent)))
        allowed, reason = signals.strategy_consistency_verdict(
            prior_intent=prior_intent, new_intent=new_intent, plan_trigger=plan_trigger,
            basis_changed=basis_changed, recent_discretionary_reversals=recent_flips,
            max_discretionary_reversals=cfg.risk.max_discretionary_reversals,
            enabled=cfg.risk.consistency_enabled)
        proof.update(
            prior_intent=prior_intent, prior_basis=prior_basis, new_basis=new_basis,
            basis_changed=basis_changed, reversal=reversal, plan_trigger=plan_trigger,
            recent_discretionary_reversals=recent_flips,
            max_discretionary_reversals=cfg.risk.max_discretionary_reversals,
            verdict={"allowed": allowed, "reason": reason})
        return allowed, reason, plan_trigger, proof
    except Exception as e:  # noqa: BLE001 — a gate read error must never crash the tick
        proof["gate_error"] = str(e)
        # Fail CLOSED on the dangerous direction: a SELL/close we can't verify might be the
        # ungrounded reversal the gate exists to suppress ("don't randomly sell day 2 what we
        # bought day 1"), so suppress it and retry next tick. Opens (buys) are allowed — a buy
        # is not the random-flip risk and blocking it would wedge deployment on a transient
        # read error; the daily $ caps + dedup still bound it.
        if signals.direction_of(new_intent) == "close":
            return False, "gate_error_suppressed_reversal", None, proof
        return True, "gate_error_degraded", None, proof


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

    # Daily-loss halt must measure TOTAL account value (positions + idle cash), NOT the
    # broker's positions-only `equity`. With positions-only, a big rebalance-buy day
    # (cash -> positions) inflates the base and can MASK a real loss, and a big sell can
    # trip the halt on a flat account. When itemized positions are present we recompute
    # total = sum(market_value) + buying_power (matching construct's `deployable`); with no
    # itemized positions we fall back to the passed `equity` (back-compat, classic path).
    positions_mv_sum = sum(_to_float((p or {}).get("market_value")) or 0.0
                           for p in positions.values())
    total_equity = (positions_mv_sum + buying_power) if positions else equity
    baseline = led.get_or_create_baseline(day, total_equity, now_iso)
    drop_pct = ((total_equity - baseline.baseline_equity) / baseline.baseline_equity * 100.0
                if baseline.baseline_equity else 0.0)

    result = {
        "halt": False,
        "write_kill": False,
        "run_id": run_id,
        "intraday": cfg.intraday_enabled,
        "baseline_equity": baseline.baseline_equity,
        "equity": total_equity,
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

        # Cross-day strategy-consistency gate (Component A): is today's buy<->sell a
        # consistent recorded strategy executing, or a RANDOM reversal? Computed BEFORE
        # the decision is recorded so the verdict + its proof are persisted with it.
        # The declared strategy basis the call rests on (model-emitted; None pre-Phase-4).
        new_basis = (str(a.get("basis") or "").strip() or None)
        _decision_price = (quote or _to_float(a.get("decision_price"))
                           or _to_float(a.get("entry_price")))
        c_allowed, c_reason, c_plan_trigger, c_proof = _consistency_context(
            cfg, led, ticker, intent, quote, new_basis,
            _decision_price, _to_float(a.get("position_pct")))

        # Persist the analysis decision to the ledger — the ground of record for
        # the memory scorecard. Recorded for EVERY valid signal (incl. hold/skip),
        # since directional grading applies to those too. decision_price comes
        # from a live RH quote when present (Phase 4), else the model's entry_price.
        # basis/plan_trigger/proof_json are the consistency-layer fields (Component A/B).
        led.record_decision(
            trade_date=day, ticker=ticker, decided_at=now_iso,
            signal=signal, intent=intent,
            position_pct=_to_float(a.get("position_pct")),
            entry_price=_to_float(a.get("entry_price")),
            stop_loss=_to_float(a.get("stop_loss")),
            next_review_hours=_to_float(a.get("next_review_hours")),
            decision_price=quote or _to_float(a.get("decision_price")) or _to_float(a.get("entry_price")),
            rationale=a.get("rationale_summary"),
            run_id=run_id, basis=new_basis, plan_trigger=c_plan_trigger,
            target_price=_to_float(a.get("target_price")),
            proof_json=json.dumps(c_proof),
            conviction=_to_float(a.get("conviction")),
            uncertainty=_to_float(a.get("uncertainty")),
            data_quality=a.get("data_quality"),
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

        # Cross-day strategy-consistency gate (Component A): suppress a RANDOM reversal
        # (an ungrounded buy<->sell flip, or serial basis churn). A reversal grounded in
        # the recorded plan (stop/loss/target firing) or a budget-respecting basis change
        # passes. Only ever fires on a reversal — a continuation/buy is never blocked.
        if not c_allowed:
            led.record_action(day, ticker, signal=signal, intent=intent,
                              status="skipped", detail=f"consistency:{c_reason}", now_iso=now_iso)
            decision.update(status="skipped", intent=intent, detail=f"consistency:{c_reason}")
            result["decisions"].append(decision)
            continue

        if intent == "buy":
            # Book-driven allocation: when the rebalance layer is active and this name is
            # in the book, the deterministic buy-to-target pass below OWNS its sizing — it
            # deploys to the full target weight in Python, not the model's per-tick
            # position_pct (which dribbles a fraction of equity). Suppress the model's buy
            # here so the name is deployed ONCE, to target, by the rebalance pass. Only fires
            # when rebalance is on AND a goal seeded target_weights (else target_weights is
            # None -> classic path byte-identical).
            if target_weights and ticker in target_weights:
                led.record_action(day, ticker, signal=signal, intent="buy", status="skipped",
                                  detail="deferred_to_rebalance_buy", now_iso=now_iso)
                decision.update(status="skipped", intent="buy", detail="deferred_to_rebalance_buy")
                result["decisions"].append(decision)
                continue
            dollars, src = signals.resolve_buy_dollars(
                a.get("position_sizing"), baseline.baseline_equity, frac,
                ceiling=cfg.risk.max_dollars_per_trade,
                remaining_daily_cap=remaining_daily_cap,
                buying_power=buying_power,
                buffer=cfg.risk.min_buying_power_buffer,
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
                    # Persist the limit-buy NOTIONAL (shares*limit_price) so the cross-tick
                    # daily-deploy-cap reseed (day_buys_total sums orders.dollar_amount) and
                    # the digest rollups see it — a limit row left at NULL would make a
                    # same-day limit buy invisible to a later tick's cap and over-deploy.
                    led.reserve_order(ref_id, day, ticker, side="buy", type="limit",
                                      dollar_amount=spend, quantity=shares, now_iso=now_iso,
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
            # Book-driven trim: when the rebalance layer owns sizing and this name
            # is in the book, the rebalance trim/exit pass below OWNS its sizing —
            # it trims toward the allocator's target weight (which already lowered
            # for this Underweight/Sell), not the model's blunt 0.5-half. Suppress
            # the model trim here so a name is trimmed to its (lower) target, not
            # to half, and isn't bought back next tick. Symmetric with
            # deferred_to_rebalance_buy above. Only fires when rebalance is on AND
            # a goal seeded target_weights (else target_weights is None -> classic
            # path byte-identical). The consistency gate already ran above, so its
            # verdict + proof are already persisted with this decision.
            if target_weights and ticker in target_weights:
                led.record_action(day, ticker, signal=signal, intent="sell", status="skipped",
                                  detail="deferred_to_rebalance_trim", now_iso=now_iso)
                decision.update(status="skipped", intent="sell", detail="deferred_to_rebalance_trim")
                result["decisions"].append(decision)
                continue
            raw_qty = signals.resolve_sell_quantity(held_qty, frac)
            qty, sreason = signals.resolve_sell_quantity_min_notional(held_qty, raw_qty, quote)
            if qty <= 0:
                # Sub-$1 trim (RH rejects fractional orders under $1) or nothing to
                # sell. Skip cleanly rather than send an order the broker bounces
                # — mirror of the buy path's rebalance_buy_below_min guard.
                detail = "nothing_to_sell" if sreason == "skip_nothing" else f"sell_below_min:{sreason}"
                led.record_action(day, ticker, signal=signal, intent="sell",
                                  status="skipped", detail=detail, now_iso=now_iso)
                decision.update(status="skipped", intent="sell", detail=detail)
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

    # Reconcile + rebalance SELL pass (Stage 3). Two independently-gated jobs over
    # construct's target_weights map:
    #   * orphan full-exit (`orphan: True`) -> a HELD name NOT in the book; gated on
    #     reconcile_unmanaged. This is the bot self-healing: anything outside the plan
    #     (a prior watchlist, a removed engine) is wound to zero. Long-only -> sell.
    #   * in-book trim/exit -> wind an overweight/removed book holding toward its
    #     target weight; gated on rebalance_enabled (Stage 3, byte-identical when off).
    # Both respect the daily (date,ticker) dedup + the halt, reserve a ref_id before
    # the broker call, and cancel any resting protective stop first. Sizing is
    # Python-clamped; the orchestrator only executes what lands in result["orders"].
    sell_map = data.get("target_weights") or {}
    if (sell_map and not result["halt"]
            and (cfg.risk.rebalance_enabled or cfg.risk.reconcile_unmanaged)):
        # Dedup on names that actually got an ORDER this tick (not every analyzed
        # decision). A deferred-to-rebalance LLM sell (detail deferred_to_rebalance_trim)
        # records a SKIPPED decision but places NO order, so it must NOT block the
        # rebalance trim pass below — the book owns that trim now. Keying on orders
        # (like the buy pass's `ordered` set) keeps the classic double-sell guard: an
        # LLM `exit` order already placed for a ticker stays excluded here. The
        # (date,ticker) `already_acted` dedup is the second, cross-tick guard.
        handled = {o.get("ticker") for o in result["orders"]}
        # Names the consistency gate SUPPRESSED this tick (an ungrounded reversal) are
        # hands-off for the SELL pass too: construct may have lowered this name's target
        # (Underweight -> halved conviction -> trim) and that target would otherwise
        # re-trim a name the gate just told to hold — silently overriding the gate (which
        # by invariant can only ever suppress a trade). Symmetric with the buy pass's
        # `suppressed` set. A plain Hold (detail "hold") is NOT in this set.
        suppressed = {str(d.get("ticker")).upper() for d in result["decisions"]
                      if str(d.get("detail") or "").startswith("consistency:")}
        for raw_ticker, tw in sell_map.items():
            ticker = str(raw_ticker).upper()
            if ticker in handled or ticker in suppressed or led.has_trade_like_action(day, ticker):
                continue
            is_orphan = bool(tw.get("orphan"))
            intent = tw.get("intent")
            if is_orphan:
                if not cfg.risk.reconcile_unmanaged or intent != "exit":
                    continue
            else:
                if not cfg.risk.rebalance_enabled or intent not in ("trim", "exit"):
                    continue
            pos = positions.get(ticker) or {}
            held_qty = float(pos.get("quantity", 0) or 0)
            if held_qty <= 0:
                continue  # nothing held to trim/exit
            quote = _to_float(quotes.get(ticker))
            held_mv = float(pos.get("market_value", 0) or 0)
            if held_mv == 0 and quote and held_qty:
                held_mv = quote * held_qty
            full_exit = is_orphan or intent == "exit"
            if is_orphan:
                order_kind, signal = "reconcile_exit", "RECONCILE"
            else:
                order_kind = "rebalance_exit" if full_exit else "rebalance_trim"
                signal = "REBALANCE"
            raw_qty = signals.resolve_target_sell_quantity(
                held_qty, quote, held_mv, _to_float(tw.get("target_dollars")) or 0.0,
                full_exit=full_exit)
            qty, sreason = signals.resolve_sell_quantity_min_notional(held_qty, raw_qty, quote)
            if qty <= 0:
                # Sub-$1 trim/exit (RH rejects fractional orders under $1) — most
                # often a whole position under the floor (unstoppable dust). Skip
                # cleanly with an auditable row (and dedup-correct: already_acted
                # becomes true, so we don't re-attempt the same dust every tick),
                # mirroring the buy path's rebalance_buy_below_min. Never an error.
                led.record_action(day, ticker, signal=signal, intent="sell", status="skipped",
                                  detail=f"sell_below_min:{sreason}", now_iso=now_iso)
                result["decisions"].append({
                    "ticker": ticker, "signal": signal, "status": "skipped",
                    "intent": "sell", "detail": f"sell_below_min:{sreason}"})
                continue
            cancel_ref_ids = [s["ref_id"] for s in led.open_protective_stops(ticker)]
            ref_id = None
            if not cfg.dry_run:
                ref_id = led.new_ref_id()
                led.reserve_order(ref_id, day, ticker, side="sell", type="market",
                                  dollar_amount=None, quantity=qty, now_iso=now_iso,
                                  order_kind=order_kind)
            result["orders"].append({
                "ticker": ticker, "signal": signal, "intent": "sell",
                "ref_id": ref_id, "side": "sell", "type": "market",
                "dollar_amount": None, "quantity": qty,
                "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                "order_kind": order_kind, "cancel_ref_ids": cancel_ref_ids,
            })
            result["decisions"].append({
                "ticker": ticker, "signal": signal, "status": "order",
                "intent": "sell", "quantity": qty, "detail": order_kind})

    # Rebalance BUY-TO-TARGET pass (Stage 3, the buy mirror of the SELL pass above).
    # The DETERMINISTIC deploy: for every book name construct flagged underweight
    # (intent=="buy"), buy toward its target weight in ONE pass — the book owns sizing,
    # not the model. Runs ONLY when rebalance is enabled AND construct emitted a target
    # map (target_weights is None when rebalance is off -> this block is skipped and plan
    # is BYTE-IDENTICAL to the classic path). Dedup is on names ACTUALLY ORDERED this
    # tick (LLM sells, reconcile/rebalance exits) so a name is never bought and sold at
    # once; a name the model merely HELD is NOT excluded (that is the whole point — it
    # still gets deployed to target). Idempotent across ticks: a name already at target
    # has room_under_target == 0 and is skipped. Sizing is clamped by the per-trade
    # ceiling, the remaining daily deploy cap, and a RUNNING available-cash figure, so the
    # SUM of every buy planned this tick can never exceed buying_power - buffer.
    if target_weights and not result["halt"] and cfg.risk.rebalance_enabled:
        cash_tk = (cfg.risk.cash_sleeve_ticker or "SGOV").upper()
        ordered = {o.get("ticker") for o in result["orders"]}
        # Names the consistency gate SUPPRESSED this tick (an ungrounded reversal) are
        # hands-off: the deterministic buy-to-target deploy must NOT re-buy a name whose
        # buy the gate just blocked, or it would silently override the gate (which by
        # invariant can only ever suppress a trade). A plain Hold (detail "hold") is NOT
        # in this set, so the deploy still funds held book names to target as intended.
        suppressed = {str(d.get("ticker")).upper() for d in result["decisions"]
                      if str(d.get("detail") or "").startswith("consistency:")}
        spent = sum(float(o.get("dollar_amount") or 0.0)
                    for o in result["orders"] if o.get("side") == "buy")
        avail_cash = buying_power - cfg.risk.min_buying_power_buffer - spent
        for raw_ticker, tw in target_weights.items():
            ticker = str(raw_ticker).upper()
            if ticker in ordered or ticker in suppressed or ticker == cash_tk or tw.get("orphan"):
                continue
            if tw.get("intent") != "buy" or not tw.get("quotable", True):
                continue
            pos = positions.get(ticker) or {}
            held_qty = float(pos.get("quantity", 0) or 0)
            held_mv = float(pos.get("market_value", 0) or 0)
            quote = _to_float(quotes.get(ticker))
            if held_mv == 0 and quote and held_qty:
                held_mv = quote * held_qty
            td = _to_float(tw.get("target_dollars")) or 0.0
            # Room before this name hits its target weight (>=0), bounded by the remaining
            # daily cap and the running cash pool. The per-trade ceiling is applied PER
            # TRANCHE below: a name whose room exceeds the ceiling is filled with multiple
            # child orders THIS tick (each its own ref_id, each <= the ceiling), so a funded
            # account reaches its target weight the same day instead of dribbling toward it
            # over many ticks. Every clamp (per-trade ceiling, daily cap, running cash) stays
            # intact across the loop — the SUM still can't exceed buying_power - buffer.
            name_budget = round(min(signals.room_under_target(td, held_mv),
                                    remaining_daily_cap, avail_cash), 2)
            if name_budget < 1.0:
                # <$1 is Robinhood's fractional minimum (also covers <=0). Skip pre-submit
                # rather than send an order the broker bounces. At full deployment no book
                # weight lands here; this only guards a genuinely tiny/at-target name.
                led.record_action(day, ticker, signal="REBALANCE", intent="buy",
                                  status="skipped", detail="rebalance_buy_below_min", now_iso=now_iso)
                result["decisions"].append({
                    "ticker": ticker, "signal": "REBALANCE", "status": "skipped",
                    "intent": "buy", "detail": "rebalance_buy_below_min"})
                continue
            ceiling = cfg.risk.max_dollars_per_trade
            tranches = 0
            spent_name = 0.0
            while (name_budget - spent_name) >= 1.0 and remaining_daily_cap >= 1.0 and avail_cash >= 1.0:
                chunk = round(min(ceiling, name_budget - spent_name,
                                  remaining_daily_cap, avail_cash), 2)
                if chunk < 1.0:
                    break
                remaining_daily_cap -= chunk
                avail_cash -= chunk
                spent_name = round(spent_name + chunk, 2)
                tranches += 1
                ref_id = None
                if not cfg.dry_run:
                    ref_id = led.new_ref_id()
                    led.reserve_order(ref_id, day, ticker, side="buy", type="market",
                                      dollar_amount=chunk, quantity=None, now_iso=now_iso,
                                      order_kind="rebalance_buy")
                result["orders"].append({
                    "ticker": ticker, "signal": "REBALANCE", "intent": "buy",
                    "ref_id": ref_id, "side": "buy", "type": "market",
                    "dollar_amount": chunk, "quantity": None,
                    "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                    "sizing_source": "rebalance_target", "order_kind": "rebalance_buy",
                    "tranche": tranches})
            # One latest-state ticker_action snapshot + one decision summarizing the fill.
            led.record_action(day, ticker, signal="REBALANCE", intent="buy", status="order",
                              detail=f"rebalance_buy x{tranches}", now_iso=now_iso)
            result["decisions"].append({
                "ticker": ticker, "signal": "REBALANCE", "status": "order", "intent": "buy",
                "dollar_amount": spent_name, "tranches": tranches, "detail": "rebalance_buy"})

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
    interval_note = None
    try:
        cons = sc.consistency or strategy.ConsistencyConfig()
        prior_by_tk = {str(t["ticker"]).upper(): t for t in prior_targets}
        new_by_tk = {h["ticker"].upper(): h for h in holdings}
        if prior_goal and cons.min_strategy_set_interval_days > 0 and prior_goal.get("created_at"):
            try:
                _days = (datetime.fromisoformat(now_iso)
                         - datetime.fromisoformat(prior_goal["created_at"])).days
                if _days < cons.min_strategy_set_interval_days:
                    interval_note = (f"strategy-set re-run after {_days}d "
                                     f"(< {cons.min_strategy_set_interval_days}d advised)")
            except (TypeError, ValueError):
                pass
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
            "start_equity": equity, "start_date": day, "changes": changes,
            "interval_note": interval_note}


def _confirm_and_persist_regime(cfg, led, goal, macro_reading, now_iso, day):
    """Advance + persist the CONFIRMED macro regime (Component C) and return the effective
    regime (a REGIME_* constant). Run from CONSTRUCT — the in-runbook step that carries
    macro_reading — so the hysteresis is actually LIVE (learn-review, which only READS the
    result, is not in the runbook). Best-effort; the SOLE confirmer so the counter advances
    exactly once per tick. A missing/None macro_reading is NO new evidence -> hold the
    current effective regime (an operator omission never accumulates a flip toward HOLD)."""
    import lib.strategy as strategy
    prior_ts = led.get_thesis_state(goal["id"])
    effective_now = strategy.normalize_regime((prior_ts or {}).get("regime"))
    if not macro_reading:
        return effective_now  # no reading == no new evidence; hold current, accumulate nothing
    cons = (cfg.strategy.consistency if cfg.strategy is not None else None) or strategy.ConsistencyConfig()
    raw_regime = strategy.regime_label_banded(cfg.strategy, macro_reading, cons.regime_deadband_pce)
    conf = strategy.regime_with_confirmation(
        prior_ts, raw_regime, confirm_n=cons.regime_confirm_n,
        min_dwell_days=cons.regime_min_dwell_days, today=day)
    effective = conf["effective_regime"]
    _word = {strategy.REGIME_STAND_DOWN: "standdown", strategy.REGIME_DEPLOY: "deploy"}
    pending_word = _word.get(conf["pending_regime"], "neutral") if conf["pending_regime"] else None
    led.upsert_thesis_state(
        goal_id=goal["id"], as_of=day, regime=_word.get(effective, "neutral"),
        active_book=goal["active_book"], last_trigger=raw_regime,
        last_macro_json=json.dumps(macro_reading or {}), updated_at=now_iso,
        pending_regime=pending_word, pending_since=conf["pending_since"],
        confirm_count=conf["confirm_count"], regime_since=conf["regime_since"])
    if conf["changed"]:
        try:
            led.record_strategy_change(
                goal_id=goal["id"], changed_at=now_iso, change_type="regime",
                from_value=effective_now, to_value=effective, trigger="macro_reading",
                reason=conf["reason"], proof_json=json.dumps(macro_reading or {}))
        except Exception:  # noqa: BLE001 — the change-log is observability; never blocks
            pass
    return effective


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
    positions_in = data.get("positions", {}) or {}
    quotes = {str(k).upper(): _to_float(v) for k, v in (data.get("quotes") or {}).items()}
    positions_mv = {str(tk).upper(): (_to_float((pos or {}).get("market_value")) or 0.0)
                    for tk, pos in positions_in.items()}
    # Size the book against TOTAL DEPLOYABLE capital, not just held equity. The broker's
    # `equity` is positions-only (sum of holdings' market value) and excludes idle cash, so
    # sizing targets against it makes every target_dollars a fraction of the true base and the
    # bot can never deploy its cash toward the book. Deployable = held positions + buying_power
    # (idle cash). Reached ONLY past the proceed:false early-return above, i.e. only with an
    # active goal — the classic path is unaffected. The cash sleeve (SGOV) is the residual:
    # it is never bought/trimmed (intent=cash_residual), so the cash backing its target weight
    # naturally stays as uninvested buying power.
    buying_power = _to_float(data.get("buying_power")) or 0.0
    deployable = sum(positions_mv.values()) + buying_power
    if deployable <= 0:
        # Backward-compatible fallback: an equity-only caller (no itemized positions/cash)
        # sizes against the passed `equity` as the base. Also avoids a div-by-zero in
        # weight_drift (current_mv/equity) when nothing is itemized as deployable.
        deployable = _to_float(data.get("equity")) or 0.0
    now_iso = data.get("now_iso") or market.now_et().isoformat()
    day = market.trading_day_et()
    # Component C: advance + persist the CONFIRMED regime HERE (the in-runbook step) so the
    # hysteresis is live. Best-effort — a hiccup falls back to the prior/raw regime; it never
    # blocks construct. The recommended book then follows the CONFIRMED effective regime, NOT
    # the raw per-tick macro_reading, so a single noisy PCE print can't swing the recommendation.
    effective_regime = None
    if cfg.strategy is not None:
        try:
            effective_regime = _confirm_and_persist_regime(
                cfg, led, goal, data.get("macro_reading"), now_iso, day)
        except Exception:  # noqa: BLE001 — regime bookkeeping is best-effort
            effective_regime = None
    book_name, book_reason = goal["active_book"], "active goal book"
    if cfg.strategy is not None:
        if effective_regime is None:
            _ts = led.get_thesis_state(goal["id"])
            effective_regime = (strategy.normalize_regime(_ts["regime"])
                                if (_ts and _ts.get("regime")) else None)
        if effective_regime is not None:
            book_name, book_reason = strategy.book_for_regime(cfg.strategy, effective_regime)
            book_reason = f"confirmed regime {effective_regime}: {book_reason}"
        else:
            book_name, book_reason = strategy.select_active_book(cfg.strategy, data.get("macro_reading"))
    targets = led.active_target_portfolio(goal["id"], statuses=("active", "exiting"))

    # Q1 — conviction-driven sizing (always on). The PIPELINE's conviction (not the static
    # book weight) sets each engine name's target weight: lib.allocate turns conviction +
    # the ledger-learned calibration + the confirmed-regime scalar into a clamped, smoothed
    # weight vector (per-name/sleeve caps + hard cash floor from the strategy.yaml
    # risk_policy), which we write onto the book rows; cash takes the residual.
    # construct_target_book / plan / the dollar caps then run UNCHANGED. Pure + read-only
    # here (allocate + calibrate touch no broker, no limits) so the decision wall holds.
    # Candidates = the FULL active engine book, so a name with no fresh analysis holds its
    # prior weight (allocate's D2 guarantee). SAFETY: on an all-Hold / no-analysis tick
    # NObody has a fresh conviction, so we SKIP the allocation entirely and keep the static
    # book verbatim — quiet days never re-clip the book; we only re-size when the pipeline
    # actually expresses conviction (Buy/Overweight/Underweight/Sell).
    conviction_detail = None
    if cfg.strategy is not None:
        import lib.allocate as allocate
        import lib.calibrate as calibrate
        cash_tk = (cfg.risk.cash_sleeve_ticker or "SGOV").upper()
        analyses_map = {str(a.get("ticker") or "").upper(): a
                        for a in (data.get("analyses") or []) if a.get("ticker")}
        candidates = [
            {"ticker": str(t["ticker"]).upper(), "sleeve": t.get("sleeve"),
             "prior_weight": float(t.get("target_weight", 0) or 0),
             "quotable": bool(t.get("quotable", True))}
            for t in targets
            if str(t.get("status", "active")) == "active"
            and str(t["ticker"]).upper() != cash_tk
            and (t.get("sleeve") or "") != strategy.CASH_SLEEVE
        ]
        policy = dict(cfg.strategy.risk_policy or {})
        # Only re-allocate when the pipeline expresses conviction this tick; otherwise every
        # name is D2 hold-prior and the allocation just reproduces the static book — so we
        # SKIP it and keep the book verbatim (quiet/all-Hold ticks never re-clip the book).
        if any(allocate.effective_conviction(analyses_map.get(c["ticker"]), policy) is not None
               for c in candidates):
            rscalar = strategy.regime_scalar(
                effective_regime, min_factor=float(policy.get("regime_min_factor", 0.5) or 0.5))
            calib = calibrate.build_calibration(
                led, [c["ticker"] for c in candidates],
                min_n=int(cfg.strategy.learning.min_resolved_n))
            # D3 (temporal consistency): a conviction-driven EXIT (Sell) on a name we already
            # hold is a potential cross-day REVERSAL ("bought day 1, sold day 2"). Route it
            # through the SAME memory-grounded consistency gate the discrete path uses; when it
            # is ungrounded (no verified stop/target firing and no budgeted new catalyst) the
            # name is pinned at its prior weight (hold_floor) instead of being cut to 0. Large
            # TRIMS are NOT gated here (per the Moderate-stickiness decision) — the allocator's
            # EWMA smoothing damps those. Best-effort: a gate hiccup just omits the floor.
            hold_floors: dict = {}
            if cfg.risk.consistency_enabled:
                for c in candidates:
                    tk = c["ticker"]
                    a = analyses_map.get(tk)
                    if not a or str(a.get("signal") or "") != "Sell" or c["prior_weight"] <= 0:
                        continue
                    try:
                        allowed, reason, _t, _p = _consistency_context(
                            cfg, led, tk, "sell", quotes.get(tk), a.get("basis"), None, None)
                        if not allowed:
                            hold_floors[tk] = c["prior_weight"]
                    except Exception:  # noqa: BLE001 — gate is best-effort; never blocks construct
                        pass
            alloc = allocate.allocate_targets(
                candidates, analyses_map, calibration=calib, regime_scalar=rscalar,
                policy=policy, hold_floors=hold_floors)
            new_targets = []
            for t in targets:
                tk = str(t["ticker"]).upper()
                nt = dict(t)
                if tk == cash_tk or (t.get("sleeve") or "") == strategy.CASH_SLEEVE:
                    nt["target_weight"] = alloc.cash_pct          # cash takes the residual
                elif str(t.get("status", "active")) == "active":
                    w = alloc.weights.get(tk, 0.0)                # exiting rows keep their 0
                    nt["target_weight"] = w
                    # Keep the rebalance dead-band strictly INSIDE the (possibly shrunk)
                    # conviction weight. Without this the band stays at the static
                    # strategy.yaml value sized for the ORIGINAL weight, so a name
                    # conviction-sized DOWN sits inside a now-too-wide band and never
                    # rebalances toward its new target (conviction sizing wouldn't bind).
                    # min(ob, w*0.5) preserves the b<w invariant validate_book enforces.
                    ob = float(t.get("band", 0.0) or 0.0)
                    if w > 0 and ob > 0:
                        nt["band"] = round(min(ob, w * 0.5), 4)
                new_targets.append(nt)
            targets = new_targets
            conviction_detail = {"regime_scalar": rscalar, "cash_pct": alloc.cash_pct,
                                 "weights": alloc.weights, "calibration": calib}

    rows = portfolio.construct_target_book(
        targets, positions_mv, deployable, cash_sleeve_ticker=cfg.risk.cash_sleeve_ticker)
    target_weights = {r["ticker"]: {"intent": r["intent"], "target_dollars": r["target_dollars"],
                                    "target_weight": r["target_weight"],
                                    "delta_dollars": r.get("delta_dollars"),
                                    "quotable": r["quotable"]} for r in rows}
    # Self-reconciliation: any HELD position NOT in the book (and not the cash sleeve)
    # is UNMANAGED -> emit a full-exit so plan winds it to zero. This is how the bot
    # heals its own drift after a book edit or a prior hand-maintained watchlist:
    # whatever is no longer in the plan gets sold. Long-only (only ever SELLS, to cash).
    # Plan executes these only when cfg.risk.reconcile_unmanaged is on; we surface
    # them in `decisions`/`orders` (RECONCILE) so the run is fully auditable.
    # FAIL-SAFE FLOOR: only reconcile against a NON-EMPTY book. An active goal with
    # zero target rows can only arise from corruption (a valid strategy.yaml always
    # sums to ~100, and strategy-set is atomic) — and "sell everything" is never a
    # safe deterministic default. With no targets we emit NO orphans. (A deliberate
    # all-cash book still HAS the SGOV row, so a real go-to-cash instruction is
    # unaffected; this only blocks the degenerate empty-book case.)
    book_tickers = {str(t["ticker"]).upper() for t in targets}
    cash = (cfg.risk.cash_sleeve_ticker or "SGOV").upper()
    unmanaged = []
    if book_tickers:
        for tk, mv in positions_mv.items():
            if mv <= 0 or tk in book_tickers or tk == cash:
                continue
            target_weights[tk] = {"intent": "exit", "target_dollars": 0.0, "target_weight": 0.0,
                                  "delta_dollars": -round(mv, 2), "quotable": True, "orphan": True}
            unmanaged.append(tk)
    return {"proceed": True, "goal_id": goal["id"], "active_book": goal["active_book"],
            "recommended_book": book_name, "book_reason": book_reason,
            "reconcile_unmanaged": cfg.risk.reconcile_unmanaged, "unmanaged": unmanaged,
            "conviction_weights": conviction_detail is not None,
            "conviction_detail": conviction_detail,
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
    cons = cfg.strategy.consistency or strategy.ConsistencyConfig()
    now_iso = market.now_et().isoformat()
    day = market.trading_day_et()
    # Component C: the regime that drives the DERISK proposal is the CONFIRMED effective
    # regime that CONSTRUCT advanced + persisted earlier this tick (construct is the SOLE
    # confirmer, so the counter advances exactly once per tick). Here we only READ it. If
    # construct didn't run (no thesis_state yet), fall back to a raw banded reading for the
    # proposal only — best-effort, dormant when the strategy layer is otherwise inactive.
    _ts = led.get_thesis_state(goal["id"])
    if _ts and _ts.get("regime"):
        macro_regime = strategy.normalize_regime(_ts["regime"])
    else:
        macro_regime = strategy.regime_label_banded(
            cfg.strategy, data.get("macro_reading"), cons.regime_deadband_pce)
    progress = goal_mod.compute_from_ledger(led, goal)
    # Q3 — the screener's live candidate provider (yfinance), imported lazily + best-effort.
    # build_proposals only invokes it for sleeves that define a `screen`, so a strategy with
    # no sleeve screens yields no ADDs: discovery is enabled by DATA (define a screen), not a
    # flag. A missing provider / yfinance error degrades to no ADDs.
    try:
        from lib.screener_data import yfinance_candidate_provider as add_provider
    except Exception:  # noqa: BLE001 — discovery is optional; never break the review
        add_provider = None
    ps = learn.build_proposals(led, goal["id"], learning, macro_regime, progress,
                               strategy_cfg=cfg.strategy, candidate_provider=add_provider)
    recorded = []
    new_proposals = []
    cooled = []  # E1: hashes suppressed because they were recently decided
    cooldown_cut = (datetime.fromisoformat(now_iso)
                    - timedelta(days=cons.proposal_cooldown_days)).isoformat()
    for p in ps["all"]:
        chash = p.content_hash()
        # E1 — anti-oscillation: if this exact change was applied/rejected within the
        # cooldown window, do NOT re-propose it (stops propose->reject->propose churn).
        last = led.last_decided_universe_change(goal["id"], chash)
        if last and (last.get("decided_at") or "") >= cooldown_cut:
            cooled.append(chash)
            continue
        rid = led.record_universe_proposal(
            goal_id=goal["id"], proposed_at=now_iso, kind=p.kind, ticker=p.ticker,
            sleeve=p.sleeve, from_book=p.from_book, to_book=p.to_book,
            target_weight=p.target_weight, tier=p.tier, content_hash=chash,
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
    # (Regime confirmation + thesis_state persistence + the regime change-log live in
    # construct now — the in-runbook step + the SOLE confirmer. Learn-review only READS
    # the effective regime above, so the counter never double-advances.)
    cutoff = (datetime.fromisoformat(now_iso) - timedelta(days=learning.proposal_expiry_days)).isoformat()
    expired = led.expire_old_proposals(goal["id"], cutoff)
    return {"reviewed": True, "macro_regime": macro_regime, "proposals": len(ps["all"]),
            "needs_approval": len(ps["needs_approval"]), "auto_apply_eligible": len(ps["auto_apply"]),
            "recorded_ids": recorded, "expired": expired, "suppressed_cooldown": len(cooled)}


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
    import lib.strategy as strategy
    cons = (cfg.strategy.consistency if cfg.strategy is not None else None) or strategy.ConsistencyConfig()
    now_iso = market.now_et().isoformat()
    goal = led.get_active_goal()

    # E2/E3 guards apply to the AUTOMATIC path (learning auto-apply flags). A human
    # `--approve` is itself a deliberate, out-of-band confirmation, so it does the
    # book-conserving transform but is not re-gated by the recurrence/freshness checks.
    auto = not approve
    revalidation_note = None

    # E2 — confirm-over-N: an AUTO REMOVE/DERISK is actionable only after it has recurred
    # on >= universe_confirm_days distinct days, so one bad crossing can't evict a sleeve.
    recurrence = led.count_proposal_recurrence(goal["id"], change["content_hash"]) if goal else 0
    if auto and change["kind"] in ("PROPOSE_REMOVE", "PROPOSE_DERISK", "PROPOSE_ADD"):
        if recurrence < cons.universe_confirm_days:
            return {"applied": False, "change": change,
                    "reason": f"not yet confirmed: seen on {recurrence}/{cons.universe_confirm_days} "
                              "distinct days (re-proposed until it persists)"}

    effect = "recorded"
    if change["kind"] == "PROPOSE_REMOVE" and change.get("ticker") and goal:
        import lib.risk as risk
        import lib.universe as universe
        learning = cfg.strategy.learning
        # E3a — re-validate the underperformance NOW for the AUTO path (the propose-time
        # proof may be stale; the name may have recovered). A human --approve trusts the
        # operator's judgment and skips the re-litigation.
        rows = led.ticker_return_series(change["ticker"])
        returns = [r["directional_return"] for r in rows if r.get("directional_return") is not None]
        m = risk.sustained_underperformance(
            returns, window=learning.underperf_window, min_n=learning.min_resolved_n,
            hit_floor=learning.underperf_hit_floor, mean_floor=learning.underperf_mean_floor)
        if auto and (m.value is None or m.value < 1.0):
            return {"applied": False, "change": change,
                    "reason": f"underperformance no longer holds at apply time [{m.proof()}]"}
        revalidation_note = m.proof()
        # E3b — apply the pure remove + conserve freed weight into cash (keeps the book
        # summed to ~100%). The AUTO path REFUSES a resulting malformed book; a human
        # --approve proceeds with the conserved transform (logged) since they own the call.
        current = led.active_target_portfolio(goal["id"], statuses=("active", "exiting", "removed"))
        new_rows, freed = universe.apply_remove(current, change["ticker"])
        new_rows = universe.redistribute_to_cash(new_rows, freed, cfg.risk.cash_sleeve_ticker)
        ok, why = universe.validate_book(new_rows)
        if not ok and auto:
            return {"applied": False, "change": change,
                    "reason": f"refusing remove: resulting book invalid ({why})"}
        if not ok:
            revalidation_note = f"{revalidation_note}; book-check WARN: {why}"
        for r in new_rows:
            led.upsert_target_holding(
                goal_id=goal["id"], sleeve=r.get("sleeve"), ticker=r["ticker"],
                target_weight=r.get("target_weight"), band=r.get("band", 0) or 0,
                status=r.get("status", "active"), book=r.get("book"),
                quotable=bool(r.get("quotable", 1)), proxy_ticker=r.get("proxy_ticker"),
                updated_at=now_iso)
        try:
            led.record_strategy_change(
                goal_id=goal["id"], changed_at=now_iso, change_type="status",
                ticker=change["ticker"], from_value="active", to_value="exiting",
                trigger="universe-apply", reason=change.get("reason"),
                proof_json=json.dumps({"recurrence": recurrence, "revalidation": m.proof(),
                                       "freed_weight": round(freed, 2)}))
        except Exception:  # noqa: BLE001
            pass
        effect = (f"holding set to exiting; {round(freed, 2)}%% freed weight -> cash; "
                  "book re-validated to ~100% (rebalancer winds the position to zero)")
    elif change["kind"] == "PROPOSE_ADD" and change.get("ticker") and goal:
        import lib.universe as universe
        # E3b(add) — allow-list + quotable gate AT APPLY TIME. The allow-list is the
        # human-confirmed RH-tradable set; the screener already filtered quotable at
        # propose time, so the allow-list doubles as the quotable set here.
        allow = cfg.strategy.rh_tradable_confirmed if cfg.strategy is not None else set()
        ok, why = universe.validate_add(change["ticker"], allow, allow)
        if not ok:
            return {"applied": False, "change": change, "reason": f"refusing add: {why}"}
        # Fund the new name FROM cash + conserve the book to ~100% (apply_add REFUSES if
        # cash is insufficient — never creates a negative cash weight). The AUTO path
        # refuses a resulting malformed book; a human --approve owns the call.
        w = float(change.get("target_weight") or 0.0)
        band = round(min(max(w * 0.2, 0.5), w * 0.5), 2) if w > 0 else 0.0
        current = led.active_target_portfolio(goal["id"], statuses=("active", "exiting", "removed"))
        new_rows, applied_w, add_reason = universe.apply_add(
            current, change["ticker"], change.get("sleeve"), w, band,
            cash_ticker=cfg.risk.cash_sleeve_ticker)
        if applied_w <= 0:
            return {"applied": False, "change": change, "reason": f"refusing add: {add_reason}"}
        ok2, why2 = universe.validate_book(new_rows)
        if not ok2 and auto:
            return {"applied": False, "change": change,
                    "reason": f"refusing add: resulting book invalid ({why2})"}
        for r in new_rows:
            led.upsert_target_holding(
                goal_id=goal["id"], sleeve=r.get("sleeve"), ticker=r["ticker"],
                target_weight=r.get("target_weight"), band=r.get("band", 0) or 0,
                status=r.get("status", "active"), book=r.get("book"),
                quotable=bool(r.get("quotable", 1)), proxy_ticker=r.get("proxy_ticker"),
                updated_at=now_iso)
        try:
            led.record_strategy_change(
                goal_id=goal["id"], changed_at=now_iso, change_type="status",
                ticker=change["ticker"], from_value=None, to_value="active",
                trigger="universe-apply", reason=change.get("reason"),
                proof_json=json.dumps({"recurrence": recurrence, "added_weight": round(applied_w, 2),
                                       "funded_from": "cash", "book_check": why2}))
        except Exception:  # noqa: BLE001
            pass
        effect = (f"{change['ticker']} added to '{change.get('sleeve')}' at {round(applied_w, 2)}%% "
                  "(funded from cash); book re-validated to ~100% (next tick analyzes it)")
    led.mark_universe_change(change_id, "applied", now_iso, "operator" if approve else "auto")
    return {"applied": True, "change_id": change_id, "kind": change["kind"],
            "ticker": change.get("ticker"), "effect": effect,
            "via": "operator" if approve else "auto"}


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
        "stage": data.get("stage"),
        "severity": data.get("severity"),
        "warnings": data.get("warnings") or [],
        "host": data.get("host"),
        # Digest footer hint: is the Python last-resort sender actually armed on this
        # box? (RESEND_API_KEY + a resolvable from). So every healthy digest passively
        # confirms the backup pager — a NOT-configured footer is itself a signal.
        "mailer_armed": bool(os.environ.get("RESEND_API_KEY")
                             and (os.environ.get("RESEND_FROM") or cfg.notify.from_addr))
        if kind == "digest" else None,
        "account_risk": _account_risk(led),
        "tickers": [rows[t] for t in sorted(rows)],
    }


def _run_report(cfg, led, data: dict) -> dict:
    """Core report logic: build the model, decide should_send + recipients.

    Pure of I/O beyond the ledger (no network). Gates by the per-event toggle
    (on_complete for the digest; on_error/on_warning for the alert family), routes
    alert recipients to ``alerts_to`` (falling back to ``to``), and dedups on
    (date, kind, stage) so distinct alert stages page independently. Mirrors the
    ``_run_*`` cores so the e2e harness can drive it directly.
    """
    date = data.get("date") or market.trading_day_et()
    now_iso = data.get("now_iso") or market.now_et().isoformat()
    kind = data.get("kind", "digest")

    if not cfg.notify.enabled:
        return {"should_send": False, "reason": "notify_disabled", "kind": kind}

    model = _build_report_model(cfg, led, data, date, now_iso, kind)
    stage = notify.stage_of(model)
    severity = notify.severity_of(model)

    if kind == "digest":
        if not cfg.notify.on_complete:
            return {"should_send": False, "reason": "complete_disabled", "kind": kind}
        # Belt-and-suspenders: never email a trivial wake with nothing to report.
        if not model.get("tickers") and not model.get("halted"):
            return {"should_send": False, "reason": "nothing_to_report", "kind": kind}
        recipients = cfg.notify.to
    else:
        if severity == "warning":
            if not cfg.notify.on_warning:
                return {"should_send": False, "reason": "warning_disabled",
                        "kind": kind, "stage": stage}
        elif not cfg.notify.on_error:
            return {"should_send": False, "reason": "error_disabled",
                    "kind": kind, "stage": stage}
        recipients = cfg.notify.alerts_to or cfg.notify.to

    digest = notify.build_digest(model)
    already = led.last_notified_hash(date, kind, stage)
    should_send = digest["content_hash"] != already
    return {
        "should_send": should_send,
        "reason": "new" if should_send else "already_sent",
        "kind": kind,
        "stage": stage,
        "severity": severity,
        "date": date,
        "from": cfg.notify.from_addr,
        "recipients": recipients,
        "subject": digest["subject"],
        "html": digest["html"],
        "text": digest["text"],
        "content_hash": digest["content_hash"],
    }


def cmd_report(args) -> dict:
    cfg, led = _cfg_and_ledger()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    return _run_report(cfg, led, data)


def cmd_report_commit(args) -> dict:
    """Record a digest/alert as sent — AFTER the orchestrator confirms delivery."""
    led = Ledger(LEDGER_DB)
    now_iso = market.now_et().isoformat()
    led.mark_notified(args.date, args.kind, args.content_hash,
                      args.recipients or "", now_iso, stage=args.stage or "")
    return {"ok": True, "date": args.date, "kind": args.kind,
            "stage": args.stage or "", "hash": args.content_hash}


def cmd_send_test(args) -> dict:
    """Send a REAL alert through lib.mailer using the production env resolution.

    The deploy acceptance gate: it exercises the exact RESEND_API_KEY / RESEND_FROM
    path the last-resort sender uses, so an unverified from-domain or a stale key is
    caught now, not during a real 2am incident. (QUIVER_MAILER_DISABLE=1 builds the
    payload without sending — used by the offline tests.)
    """
    from lib import mailer  # local: ops-layer network egress, not the trading brain
    cfg, led = _cfg_and_ledger()
    date = market.trading_day_et()
    now_iso = market.now_et().isoformat()
    kind = args.kind or "auth_error"
    data = {
        "date": date, "now_iso": now_iso, "kind": kind,
        "stage": args.stage or None, "severity": args.severity or "critical",
        "event_detail": "send-test: Quiver alerting self-test (no real failure).",
        "equity": 100.0, "host": os.environ.get("QUIVER_HOST_HINT") or None,
    }
    model = _build_report_model(cfg, led, data, date, now_iso, kind)
    built = notify.build_digest(model)
    to = [a.strip() for a in (args.to or "").split(",") if a.strip()] \
        or cfg.notify.alerts_to or cfg.notify.to
    from_addr = os.environ.get("RESEND_FROM", "").strip() or cfg.notify.from_addr
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    res = mailer.send_email(api_key=api_key, from_addr=from_addr, to=to,
                            subject="[SELF-TEST] " + built["subject"],
                            html=built["html"], text=built["text"])
    return {"sent": bool(res.get("ok")), "result": res, "to": to,
            "from": from_addr, "kind": kind, "stage": notify.stage_of(model)}


def cmd_auth_stop(_args) -> dict:
    """Emit the unique AUTH-STOP sentinel for the headless supervisor.

    The orchestrator (TICK.md STEP 2) runs this on a confirmed Robinhood 401 BEFORE
    firing the in-tick auth alert and STOPPING. It places/decides nothing and touches
    no broker — it only prints AUTH_STOP_SENTINEL into the supervisor's captured stdout
    so run_tick.py recognises the hard-stop INDEPENDENTLY of whether the (best-effort)
    auth email actually sent. The token is unique and absent from TICK.md prose, so it
    cannot be produced by the orchestrator merely reading the runbook — only by running
    this command on a real auth failure."""
    return {"event": AUTH_STOP_SENTINEL, "stage": notify.AUTH_STAGE}


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
            cfg = load_config(CONFIG_PATH)
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


# --- reflective memory: proof tool + rebuild ---------------------------------
# Read-only/observability. memory-show prints the LIVE-recomputed risk math next
# to the on-disk markdown so the user can confirm the file matches the formulas;
# memory-rebuild regenerates every file from the ledger (also backfills history).

def cmd_memory_show(args) -> dict:
    cfg = load_config(CONFIG_PATH)
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
    cfg = load_config(CONFIG_PATH)
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
    p_rc.add_argument("--stage", default="")
    p_rc.add_argument("--hash", required=True, dest="content_hash")
    p_rc.add_argument("--recipients", default="")
    p_st = sub.add_parser("send-test")
    p_st.add_argument("--kind", default="auth_error")
    p_st.add_argument("--stage", default="")
    p_st.add_argument("--severity", default="critical")
    p_st.add_argument("--to", default="")
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
    sub.add_parser("auth-stop")
    p_ms = sub.add_parser("memory-show")
    p_ms.add_argument("--ticker", default="")
    sub.add_parser("memory-rebuild")
    p_dp = sub.add_parser("decision-proof")
    p_dp.add_argument("--id", required=True)
    p_sh = sub.add_parser("strategy-history")
    p_sh.add_argument("--limit", default="50")
    p_sh.add_argument("--type", default="")
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
        elif args.cmd == "send-test":
            out = cmd_send_test(args)
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
        elif args.cmd == "auth-stop":
            out = cmd_auth_stop(args)
        elif args.cmd == "memory-show":
            out = cmd_memory_show(args)
        elif args.cmd == "memory-rebuild":
            out = cmd_memory_rebuild(args)
        elif args.cmd == "decision-proof":
            out = cmd_decision_proof(args)
        elif args.cmd == "strategy-history":
            out = cmd_strategy_history(args)
        else:  # unreachable
            raise SystemExit(2)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e), "cmd": args.cmd}))
        return 1

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
