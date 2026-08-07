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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
from lib import market  # noqa: E402
from lib import notify  # noqa: E402
from lib import signals  # noqa: E402
from lib import allocate  # noqa: E402 — binding-side glue reads _DEFAULTS for the PTJ knobs
from lib import storage  # noqa: E402
from lib import memory  # noqa: E402
from lib import universe  # noqa: E402
from lib import attribution  # noqa: E402 — read-only diagnostic; never read by plan/sizing/halt
from lib import fsutil  # noqa: E402

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
            # No fixed per-trade / daily-deploy dollar cap: both are CALCULATED live at plan time
            # (per_name_max_pct% of deployable equity / deployable equity) — nothing to echo here.
            "daily_loss_halt_pct": cfg.risk.daily_loss_halt_pct,
            "min_buying_power_buffer": cfg.risk.min_buying_power_buffer,
        },
        "order": {
            "buy_type": cfg.buy_type,
            "time_in_force": cfg.time_in_force,
            "market_hours": cfg.market_hours,
            # TICK.md STEP 5e gates the protective stop on `order.protective_stop.enabled`.
            # Omitting it here made that condition read a missing key -> permanently false, so
            # `tick.py protect` was NEVER invoked and every entry since 2026-07-19 filled naked
            # (verified: 161 broker orders, 0 with a stop_price). Echo the real config.
            "protective_stop": {
                "enabled": cfg.protective_stop_enabled,
                "stop_pct": cfg.protective_stop_pct,
                "time_in_force": cfg.protective_stop_tif,
            },
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

    # --- PTJ defense (F1-F6): risk_policy knobs, analysis lookup, event-risk sidecar ---
    # None-safe: cfg.strategy is None on a bare/garbled checkout, and every knob defaults OFF
    # via allocate._DEFAULTS, so a strategy.yaml predating these knobs is BYTE-IDENTICAL. The
    # glue reads the knobs and only passes a BINDING value (risk_cap not None / exposure_scalar
    # < 1.0 / an R:R skip) when the operator opted in — the pure fns are otherwise no-ops.
    _rp = (getattr(cfg.strategy, "risk_policy", None) or {}) if cfg.strategy is not None else {}

    def _pol(key):
        v = _rp.get(key)
        return v if v is not None else allocate._DEFAULTS.get(key)

    _min_rr = _to_float(_pol("min_reward_risk")) or 0.0
    _risk_pct = _to_float(_pol("per_trade_risk_pct")) or 0.0
    _vol_target = _to_float(_pol("downside_vol_target")) or 0.0
    _trend_mode = str(_pol("trend_gate_mode") or "off")
    _require_target = bool(_pol("require_target_for_buy"))
    _derisk_tiers = _pol("derisk_tiers") or []

    by_ticker = {str(a.get("ticker", "")).upper(): a
                 for a in analyses if a.get("ticker")}
    # Event-risk / non-confirmation sidecar (F8/F9), analysis-side + day-stamped. A stale-day
    # or absent/garbled map is treated as EMPTY -> byte-identical (best-effort observability).
    _er_raw = data.get("event_risk") or {}
    if isinstance(_er_raw, dict) and str(_er_raw.get("trading_day") or day) == day:
        _event_risk = {str(k).upper(): (v or {})
                       for k, v in (_er_raw.get("names") or {}).items()}
    else:
        _event_risk = {}
    # Names TRIMMED by the graduated de-risk pass THIS tick — the same-tick half of the B4
    # re-buy guard (the committed `actions` log that derisk_acted_today reads isn't written
    # until STEP 6 commit, so it can't see this run's DERISK order). Declared unconditionally
    # so the buy pass can reference it even when de-risk is OFF (empty set -> byte-identical).
    _derisked: set = set()

    def _rr_verdict(a):
        """(allowed, detail) for a BUY under the asymmetric R:R gate (F1). Gate OFF
        (min_rr<=0) -> allow. Uncomputable R:R (missing target/stop) is VISIBLE, not
        silent (C7): skip when require_target, else pass tagged 'ungraded_pass'."""
        if _min_rr <= 0:
            return (True, None)
        rr = signals.reward_risk_ratio(_to_float(a.get("entry_price")),
                                       _to_float(a.get("stop_loss")),
                                       _to_float(a.get("target_price")))
        if rr is None:
            return (False, "reward_risk:ungraded") if _require_target else (True, "reward_risk:ungraded_pass")
        if rr < _min_rr:
            return (False, "reward_risk:below_floor")
        return (True, None)

    def _ptj_buy_clamps(a, ticker):
        """(risk_cap, exposure_scalar) for a BUY — both no-op (None/None) when knobs OFF.
        risk_cap = stop-distance risk budget (F2); exposure_scalar = downside-vol (F3) x
        trend gate (F4) x event-risk shrink (F8), passed as None when it would be 1.0 so the
        buy is byte-identical."""
        risk_cap = signals.risk_budget_cap(baseline.baseline_equity, _risk_pct,
                                           _to_float(a.get("entry_price")),
                                           _to_float(a.get("stop_loss")))
        desc = _event_risk.get(str(ticker).upper()) or {}
        sc = (signals.downside_vol_scalar(_to_float(desc.get("downside_vol")), _vol_target)
              * signals.trend_gate_scalar(desc.get("trend_regime"),
                                          _to_float(desc.get("momentum")), _trend_mode))
        sev = _to_float(desc.get("severity"))
        if sev and sev > 0:
            sc *= max(0.25, 1.0 - sev)   # shrink a buy INTO a known binary (F8/P10)
        return (risk_cap, (None if sc >= 1.0 else sc))

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

    # There is NO per-trade dollar cap: per-name exposure is bounded by the % target weight
    # (capped at per_name_max_pct inside the allocator), not a redundant per-order ceiling. The
    # only calculated bound is the daily deploy budget = live DEPLOYABLE capital (held positions +
    # idle cash), mirroring _run_construct so it scales off the SAME base the book's target_dollars
    # use, and so a freshly-funded ALL-CASH account (positions {}) sizes off its cash instead of
    # collapsing to ~0 (the broker's positions-only `equity`). The 20% daily-loss halt + KILL stay
    # the circuit breaker; buying_power - buffer is the hard cash bound.
    deployable_equity = positions_mv_sum + buying_power
    if deployable_equity <= 0:
        deployable_equity = equity   # back-compat fallback (mirrors _run_construct)

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

    remaining_daily_cap = deployable_equity - led.day_buys_total(day)

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
            # PTJ asymmetric R:R gate (F1): a fresh buy below the reward:risk floor is a
            # RANDOM/low-quality entry -> skip (visible detail, never silent). OFF -> allow.
            _rr_ok, _rr_detail = _rr_verdict(a)
            if not _rr_ok:
                led.record_action(day, ticker, signal=signal, intent="buy", status="skipped",
                                  detail=_rr_detail, now_iso=now_iso)
                decision.update(status="skipped", intent="buy", detail=_rr_detail)
                result["decisions"].append(decision)
                continue
            _risk_cap, _exposure = _ptj_buy_clamps(a, ticker)
            dollars, src = signals.resolve_buy_dollars(
                a.get("position_sizing"), baseline.baseline_equity, frac,
                remaining_daily_cap=remaining_daily_cap,
                buying_power=buying_power,
                buffer=cfg.risk.min_buying_power_buffer,
                position_pct=_to_float(a.get("position_pct")),
                room_under_target=_target_room(target_weights, ticker, held_mv),
                risk_cap=_risk_cap, exposure_scalar=_exposure,
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
                # F2: floor classic market buys at the economic minimum too (RH rejects sub-$1
                # fractional buys; skip a sub-min entry rather than dribble). Reached only for a
                # non-book name / rebalance-off; in-book buys defer to the rebalance pass below.
                buy_min = max(signals.MIN_SELL_NOTIONAL_USD, cfg.risk.min_trade_notional)
                if dollars < buy_min:
                    led.record_action(day, ticker, signal=signal, intent="buy", status="skipped",
                                      detail="sized_below_min", now_iso=now_iso)
                    decision.update(status="skipped", intent="buy", detail="sized_below_min")
                    result["decisions"].append(decision)
                    continue
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
            # F2: a full Sell (frac>=1.0) is a wind-down EXIT -> keep RH's $1 floor and BUMP so it
            # always completes (a $1-$5 position must still be sellable to zero); an Underweight
            # TRIM (frac<1.0) uses the economic floor and SKIPS a sub-floor churn trim.
            is_full_close = frac >= 1.0 or signal == "Sell"
            sell_min = (signals.MIN_SELL_NOTIONAL_USD if is_full_close
                        else max(signals.MIN_SELL_NOTIONAL_USD, cfg.risk.min_trade_notional))
            qty, sreason = signals.resolve_sell_quantity_min_notional(
                held_qty, raw_qty, quote, min_notional=sell_min, bump_to_min=is_full_close)
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
            # Trim only down to the near band EDGE (target + band), not to the exact
            # target — a name settles AT the edge instead of overshooting and re-arming
            # opposite-side churn next tick. Full exits ignore the band (wind to zero).
            raw_qty = signals.resolve_target_sell_quantity(
                held_qty, quote, held_mv, _to_float(tw.get("target_dollars")) or 0.0,
                full_exit=full_exit,
                upper_band_dollars=(0.0 if full_exit else _to_float(tw.get("band_dollars")) or 0.0))
            # F2: a full EXIT / reconcile winds to zero at RH's $1 floor (BUMP so it completes);
            # a partial rebalance TRIM uses the economic floor and SKIPS a sub-floor churn trim.
            sell_min = (signals.MIN_SELL_NOTIONAL_USD if full_exit
                        else max(signals.MIN_SELL_NOTIONAL_USD, cfg.risk.min_trade_notional))
            qty, sreason = signals.resolve_sell_quantity_min_notional(
                held_qty, raw_qty, quote, min_notional=sell_min, bump_to_min=full_exit)
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

    # PTJ graduated de-risk pass (F6/P4): BELOW the hard 20% halt (which already returned),
    # a shallower drawdown that breaches a configured tier TRIMS held names — "cut before it's
    # catastrophic; the market falls faster than it rises." OFF by default (derisk_tiers empty
    # -> _trim_frac == 0.0) so this whole block is a no-op and plan is BYTE-IDENTICAL. Placed
    # AFTER the rebalance SELL pass and BEFORE the buy pass (C4): a de-risk trim lands in
    # result["orders"] + records a DERISK action, and the buy pass excludes it same-tick
    # (_derisked) AND cross-tick (derisk_acted_today), never re-buying what the defense just
    # protected. ONE de-risk action per name per day (has_trade_like_action dedup); the 20%
    # halt stays the ultimate backstop. Trims are long-only sells, Python-clamped, stops cancelled.
    _trim_frac = signals.derisk_trim_fraction(drop_pct, _derisk_tiers)
    if _trim_frac > 0 and not result["halt"]:
        _handled = {o.get("ticker") for o in result["orders"]}
        _suppressed = {str(d.get("ticker")).upper() for d in result["decisions"]
                       if str(d.get("detail") or "").startswith("consistency:")}
        for raw_ticker, pos in positions.items():
            ticker = str(raw_ticker).upper()
            if ticker in _handled or ticker in _suppressed or led.has_trade_like_action(day, ticker):
                continue
            held_qty = float((pos or {}).get("quantity", 0) or 0)
            if held_qty <= 0:
                continue
            quote = _to_float(quotes.get(ticker))
            raw_qty = signals.resolve_sell_quantity(held_qty, _trim_frac)
            # A de-risk TRIM (never a full exit) respects the economic floor + skips a
            # sub-floor churn trim (F2 TRIM semantics: bump_to_min=False).
            sell_min = max(signals.MIN_SELL_NOTIONAL_USD, cfg.risk.min_trade_notional)
            qty, sreason = signals.resolve_sell_quantity_min_notional(
                held_qty, raw_qty, quote, min_notional=sell_min, bump_to_min=False)
            if qty <= 0:
                led.record_action(day, ticker, signal="DERISK", intent="sell", status="skipped",
                                  detail=f"derisk_below_min:{sreason}", now_iso=now_iso)
                continue
            cancel_ref_ids = [s["ref_id"] for s in led.open_protective_stops(ticker)]
            ref_id = None
            if not cfg.dry_run:
                ref_id = led.new_ref_id()
                led.reserve_order(ref_id, day, ticker, side="sell", type="market",
                                  dollar_amount=None, quantity=qty, now_iso=now_iso,
                                  order_kind="derisk_trim")
            _derisked.add(ticker)   # same-tick re-buy guard (B4)
            led.record_action(day, ticker, signal="DERISK", intent="sell", status="order",
                              detail="daily_loss_derisk", now_iso=now_iso)
            result["orders"].append({
                "ticker": ticker, "signal": "DERISK", "intent": "sell",
                "ref_id": ref_id, "side": "sell", "type": "market",
                "dollar_amount": None, "quantity": qty,
                "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                "order_kind": "derisk_trim", "cancel_ref_ids": cancel_ref_ids,
                "plan_trigger": "daily_loss_derisk"})
            result["decisions"].append({
                "ticker": ticker, "signal": "DERISK", "status": "order",
                "intent": "sell", "quantity": qty, "detail": "daily_loss_derisk"})

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
        # F2: no rebalance buy tranche below the economic minimum (floored at RH's $1) — kills
        # sub-$5 residual/dribble buys. Threaded through the outer guard AND the tranche loop.
        buy_min = max(signals.MIN_SELL_NOTIONAL_USD, cfg.risk.min_trade_notional)
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
            # PTJ R:R gate on the LIVE book deploy (F1/C1): a rebalance buy below the
            # reward:risk floor is skipped exactly like a classic buy. OFF -> allow.
            a_rr = by_ticker.get(ticker)
            if a_rr is not None:
                _rr_ok, _rr_detail = _rr_verdict(a_rr)
                if not _rr_ok:
                    led.record_action(day, ticker, signal="REBALANCE", intent="buy",
                                      status="skipped", detail=_rr_detail, now_iso=now_iso)
                    result["decisions"].append({
                        "ticker": ticker, "signal": "REBALANCE", "status": "skipped",
                        "intent": "buy", "detail": _rr_detail})
                    continue
            # B4: never re-buy a name the graduated de-risk pass TRIMMED — same-tick (_derisked)
            # or a prior committed tick today (derisk_acted_today) — or the deploy-to-target
            # would undo the defense on the exact positions it just protected.
            if ticker in _derisked or led.derisk_acted_today(day, ticker):
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
            # PTJ defense on the LIVE book deploy (F2/F3/F4/F8 via C1): shrink the room by the
            # exposure scalar (downside-vol x trend x event) and add the stop-distance risk cap
            # to the min(). Both no-op when the knobs are OFF -> name_budget BYTE-IDENTICAL.
            _room = signals.room_under_target(td, held_mv)
            _risk_cap_r, _exposure_r = (_ptj_buy_clamps(a_rr, ticker) if a_rr is not None else (None, None))
            _clamps_r = [_room, remaining_daily_cap, avail_cash]
            if _exposure_r is not None:
                # Cap THIS ORDER, do not shrink the room. Multiplying `_room` itself made the
                # exposure scalar a permanent ceiling rather than a rate limiter: the name landed
                # short of target but INSIDE its drift band, so needs_rebalance() read "on target"
                # on the next tick and the deploy never completed. Capping the order instead
                # leaves the untouched residual visible, so a high-vol name walks to its target
                # over several ticks (still <= room x scalar per tick) instead of parking below it.
                _clamps_r.append(_room * _exposure_r)
            if _risk_cap_r is not None:
                _clamps_r.append(_risk_cap_r)
            name_budget = round(min(_clamps_r), 2)
            if name_budget < buy_min:
                # Below the economic minimum (F2; >= RH's $1 fractional floor). Skip pre-submit
                # rather than dribble a tiny order. At full deployment no book weight lands here;
                # this only guards a genuinely tiny / at-target name (room under target < buy_min).
                led.record_action(day, ticker, signal="REBALANCE", intent="buy",
                                  status="skipped", detail="rebalance_buy_below_min", now_iso=now_iso)
                result["decisions"].append({
                    "ticker": ticker, "signal": "REBALANCE", "status": "skipped",
                    "intent": "buy", "detail": "rebalance_buy_below_min"})
                continue
            # Deploy the name to its target room in ONE order — no per-trade ceiling, so no
            # tranching. name_budget is already min(room, remaining_daily_cap, avail_cash, risk_cap)
            # and >= buy_min here (it cleared the gate above), so a single market buy of exactly
            # name_budget lands the name on (or as close as cash allows to) its target weight.
            spend = name_budget
            remaining_daily_cap -= spend
            avail_cash -= spend
            ref_id = None
            if not cfg.dry_run:
                ref_id = led.new_ref_id()
                led.reserve_order(ref_id, day, ticker, side="buy", type="market",
                                  dollar_amount=spend, quantity=None, now_iso=now_iso,
                                  order_kind="rebalance_buy")
            result["orders"].append({
                "ticker": ticker, "signal": "REBALANCE", "intent": "buy",
                "ref_id": ref_id, "side": "buy", "type": "market",
                "dollar_amount": spend, "quantity": None,
                "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                "sizing_source": "rebalance_target", "order_kind": "rebalance_buy"})
            led.record_action(day, ticker, signal="REBALANCE", intent="buy", status="order",
                              detail="rebalance_buy", now_iso=now_iso)
            result["decisions"].append({
                "ticker": ticker, "signal": "REBALANCE", "status": "order", "intent": "buy",
                "dollar_amount": spend, "detail": "rebalance_buy"})

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
            # F3: material-change HYSTERESIS on the conviction target. Keep each name at its PRIOR
            # target weight unless the fresh reading moved it >= conviction_rebalance_min_delta_pct
            # weight-points — a noisy ~0-alpha signal no longer wanders the target (and churns the
            # book with $5-40 trims) day to day. Applies to names the allocator KEPT (>0); a grounded
            # reduction to 0 (a real Sell/exit) is NOT reverted. Then CONSERVE: engines never exceed
            # (100 - cash_floor); cash takes the residual so the book still sums to ~100%. min_delta
            # 0 -> no-op (byte-identical to re-clipping every tick — the pre-F3 behavior).
            prior_map = {c["ticker"]: c["prior_weight"] for c in candidates}
            eng_weights = allocate.apply_target_hysteresis(
                alloc.weights, prior_map, cfg.risk.conviction_rebalance_min_delta_pct)
            cash_floor = float(policy["cash_floor_pct"]) if (policy and policy.get("cash_floor_pct") is not None) else 5.0
            eng_ceiling = max(0.0, 100.0 - cash_floor)
            # F3: conviction-driven deployment (the reallocation teeth). regime_scalar sets
            # deployment from the MACRO read; this adds a per-tick BOOK-CONVICTION scalar so a
            # broadly-BEARISH book (weak mean fresh conviction) deploys LESS -> raises cash, while
            # still rotating what it holds toward the relatively-stronger names. Applied HERE,
            # AFTER apply_target_hysteresis, as a book-level cash CARVE-OUT on the FRESH-conviction
            # names ONLY — scaling allocate's internal budget instead is absorbed by EWMA + the 2pt
            # per-name dead-band (a no-op on a diversified book). D2 hold-prior + D3 hold_floor names
            # are NEVER scaled (a data outage / suppressed reversal still never sells). base anchor is
            # the FIXED eng_ceiling (100 - cash_floor), so a stable bearish book yields a STABLE cash
            # target (no runaway decay). The downstream rebalance dead-band remains the anti-churn rail.
            raw_convs = [ec for ec in (
                allocate.effective_conviction(analyses_map.get(str(c["ticker"]).upper()), policy)
                for c in candidates) if ec is not None]
            dep_factor = allocate.conviction_deploy_factor(raw_convs, policy)
            if dep_factor < 1.0 - 1e-9 and eng_weights:
                deploy_target = dep_factor * eng_ceiling
                protected = {t for t in eng_weights
                             if str((alloc.detail.get(t) or {}).get("reason") or "").startswith("hold_prior")
                             or hold_floors.get(t, 0.0) > 0.0}
                # sorted(): `protected` is a SET, so this summed floats in set-iteration order,
                # which for str keys varies with PYTHONHASHSEED — a cross-process determinism
                # hazard for the process-parallel benchmark sweep. HONEST CAVEAT: this is
                # hygiene, not a proven-live bug. On this interpreter (CPython 3.12, compensated
                # summation) the `round(..., 4)` below absorbs the residue — 0 of 200000
                # perturbation trials changed the output — so no test can currently make it go
                # red. It is applied because the ordering guarantee is free and the property is
                # relied upon, not because a failure was observed.
                protected_sum = sum(eng_weights[t] for t in sorted(protected))
                scalable = [t for t in eng_weights if t not in protected]
                scalable_sum = sum(eng_weights[t] for t in scalable)
                target_scalable = max(0.0, deploy_target - protected_sum)
                if scalable_sum > target_scalable + 1e-9 and scalable_sum > 0:
                    scale = target_scalable / scalable_sum
                    for t in scalable:
                        eng_weights[t] = round(eng_weights[t] * scale, 4)
            eng_sum = sum(eng_weights.values())
            if eng_sum > eng_ceiling + 1e-9 and eng_sum > 0:
                scale = eng_ceiling / eng_sum
                eng_weights = {t: round(w * scale, 4) for t, w in eng_weights.items()}
            cash_pct = round(max(cash_floor, 100.0 - sum(eng_weights.values())), 4)
            new_targets = []
            for t in targets:
                tk = str(t["ticker"]).upper()
                nt = dict(t)
                if tk == cash_tk or (t.get("sleeve") or "") == strategy.CASH_SLEEVE:
                    nt["target_weight"] = cash_pct                # cash takes the residual
                elif str(t.get("status", "active")) == "active":
                    w = eng_weights.get(tk, 0.0)                  # exiting rows keep their 0
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
            conviction_detail = {"regime_scalar": rscalar, "cash_pct": cash_pct,
                                 "weights": eng_weights, "calibration": calib,
                                 "hysteresis_min_delta_pct": cfg.risk.conviction_rebalance_min_delta_pct}

    rows = portfolio.construct_target_book(
        targets, positions_mv, deployable, cash_sleeve_ticker=cfg.risk.cash_sleeve_ticker,
        default_band_pct=cfg.risk.rebalance_drift_band_pct)
    target_weights = {r["ticker"]: {"intent": r["intent"], "target_dollars": r["target_dollars"],
                                    "target_weight": r["target_weight"],
                                    "delta_dollars": r.get("delta_dollars"),
                                    "band_dollars": r.get("band_dollars", 0.0),
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
        ckey = p.change_key()
        # E1 — anti-oscillation: if this change (kind,ticker, ANY tier) was applied/rejected
        # within the cooldown window, do NOT re-propose it (stops propose->reject->propose
        # churn, pooled across sources so one source's rejection cools the other too).
        last = led.last_decided_change_key(goal["id"], ckey)
        if last and (last.get("decided_at") or "") >= cooldown_cut:
            cooled.append(ckey)
            continue
        rid = led.record_universe_proposal(
            goal_id=goal["id"], proposed_at=now_iso, kind=p.kind, ticker=p.ticker,
            sleeve=p.sleeve, from_book=p.from_book, to_book=p.to_book,
            target_weight=p.target_weight, tier=p.tier, content_hash=chash,
            change_key=ckey, reason=p.reason, goal_gap_pct=p.goal_gap_pct)
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


def cmd_bills_review(args) -> dict:
    cfg, led = _cfg_and_ledger()
    return _run_bills_review(cfg, led, _load_input(args))


def _run_bills_review(cfg, led, data, *, poll=None, detail_fetch=None, text_fetch=None,
                      analyze=None, judge=None) -> dict:
    """Best-effort (never stops a tick): ingest changed US Congress bills, run an LLM impact
    analysis + adversarial judge, and MINT legislative universe proposals for the rare bill
    clearing BOTH a Python-owned passage gate AND the judge. All sizing/apply stays in the
    existing Python wall — this only records + proposes (always human `--approve` gated).

    Fully injectable (poll/detail_fetch/text_fetch/analyze/judge default to the real callables)
    so the offline e2e drives the whole chain with fakes and zero network/subprocess. Fails
    SAFE to zero proposals on any error; a per-bill failure is isolated so one bad bill can
    neither abort the review nor strand the poll cursor."""
    import time
    import lib.legislative as legis
    import lib.learn as learn
    import lib.strategy as strategy
    from lib.dataflows import congress
    from lib.dataflows.errors import DataUnavailableError

    leg = cfg.legislative
    if not getattr(leg, "enabled", False):
        return {"reviewed": False, "reason": "disabled"}
    goal = led.get_active_goal()
    if not goal or cfg.strategy is None:
        return {"reviewed": False, "reason": "no active goal / strategy layer inactive"}
    if not leg.api_key:
        return {"reviewed": False, "reason": "no CONGRESS_API_KEY"}

    poll = poll or congress.poll_changed_bills
    detail_fetch = detail_fetch or congress.fetch_bill_detail
    analyze = analyze or _default_bill_analyze
    judge = judge or _default_bill_judge
    if text_fetch is None:
        text_fetch = lambda detail: _default_bill_text(congress, detail, leg.api_key)  # noqa: E731

    now = market.now_et()
    now_iso = now.isoformat()
    today = market.trading_day_et()
    deadline = time.monotonic() + max(30, int(leg.review_deadline_sec))

    with led.legislative_conn() as c:
        cur = legis.get_cursor(c)

    # RECONCILE (GB2): mark applied any bill whose linked proposal is now applied, so a
    # multi-bill-per-ticker orphan can't stay actionable and re-mint/re-fund cash. Runs
    # BEFORE the daily-dedup return, so an applied bill is marked promptly even on a
    # skip day (cheap + idempotent).
    reconciled = 0
    try:
        with led.legislative_conn() as c:
            hashes = legis.linked_proposal_hashes(c)
        for h in hashes:
            last = led.last_decided_universe_change(goal["id"], h)
            if last and last.get("status") == "applied":
                with led.legislative_conn() as c:
                    for bid in legis.bills_for_proposal_hash(c, h):
                        legis.mark_bill_applied(c, bid)
                        reconciled += 1
    except Exception:  # noqa: BLE001 — reconcile is best-effort
        pass

    # daily dedup: one full ingest per trading day (so intraday mode can't re-poll hourly)
    if cur and str(cur.get("polled_at") or "")[:10] == today:
        return {"reviewed": False, "reason": "already ran today", "reconciled": reconciled}

    # POLL. Arm the cursor immediately after a successful poll (GB5) so any downstream raise
    # can't leave the daily latch un-set and cause a re-poll storm.
    since = (cur.get("last_update_date") if cur and cur.get("last_update_date")
             else (now - timedelta(days=int(leg.lookback_days))).strftime("%Y-%m-%dT%H:%M:%SZ"))
    until = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        changed = poll(since, until, leg.api_key)
    except DataUnavailableError as e:
        return {"reviewed": False, "reason": f"poll failed: {e}", "reconciled": reconciled}
    max_update = max([b.get("update_date") for b in changed if b.get("update_date")],
                     default=(cur.get("last_update_date") if cur else None))
    with led.legislative_conn() as c:
        legis.set_cursor(c, max_update or until, until, now_iso)

    # ANALYZE (heuristic-first: skip below-passage bills BEFORE any text-fetch/LLM; cap the
    # loop; per-bill try/except; honor the wall-clock deadline).
    analyzed = below_gate = skipped_enacted = 0
    for b in changed:
        if time.monotonic() > deadline or analyzed >= int(leg.max_bills_analyzed_per_review):
            break
        try:
            chash = legis.bill_content_hash(congress.derive_status(b.get("latest_action")),
                                            b.get("latest_action_date"),
                                            b.get("update_date_including_text"))
            with led.legislative_conn() as c:
                needs = legis.upsert_bill(
                    c, bill_id=b["bill_id"], congress=b["congress"], chamber=None,
                    bill_type=b["bill_type"], number=b["number"], title=b.get("title"),
                    status=congress.derive_status(b.get("latest_action")),
                    latest_action=b.get("latest_action"), latest_action_date=b.get("latest_action_date"),
                    policy_codes=[], content_hash=chash, first_seen=now_iso, now=now_iso)
            if not needs:
                continue
            detail = detail_fetch(b["congress"], b["bill_type"], b["number"], leg.api_key)
            prob = round(float(congress.heuristic_passage_prob(detail)), 4)
            with led.legislative_conn() as c:
                legis.set_bill_detail(c, b["bill_id"], detail.get("status"), detail.get("policy_codes"), now_iso)
                legis.record_analysis(c, b["bill_id"], passage_prob=prob,
                                      passage_source=leg.passage_source, thesis="", model="", now=now_iso)
            if detail.get("status") == "became_law":
                skipped_enacted += 1
                continue  # already enacted -> priced in; no front-run edge (prob is honestly 1.0)
            if prob < float(leg.passage_prob_min):
                below_gate += 1
                continue  # below the passage gate -> never spend an LLM call
            text = text_fetch(detail)
            meta = {"bill_id": b["bill_id"], "title": detail.get("title") or b.get("title"),
                    "status": detail.get("status"), "policy_codes": detail.get("policy_codes"),
                    "latest_action": detail.get("latest_action")}
            analysis = analyze(meta, text)
            with led.legislative_conn() as c:
                legis.record_analysis(c, b["bill_id"], passage_prob=prob, passage_source=leg.passage_source,
                                      thesis=analysis.get("thesis", ""), model=leg.model, now=now_iso)
                legis.replace_bill_impacts(c, b["bill_id"], analysis.get("impacted_tickers", []), now_iso)
            analyzed += 1
        except Exception:  # noqa: BLE001 — one bad bill never aborts the review
            continue

    # JUDGE analyzed-but-unjudged bills (adversarial N-of-M; a judge outage = non-PASS = safe).
    judged = 0
    with led.legislative_conn() as c:
        pending = legis.bills_needing_judgment(c)
    for pj in pending:
        if time.monotonic() > deadline:
            break
        try:
            with led.legislative_conn() as c:
                impacts = legis.get_bill_impacts(c, pj["bill_id"])
            if leg.judge_enabled:
                verdict = judge({"bill_id": pj["bill_id"], "title": pj.get("title"), "status": pj.get("status")},
                                {"impacted_tickers": impacts, "thesis": pj.get("thesis") or ""},
                                votes=int(leg.judge_votes), pass_min=int(leg.judge_pass_min))
            else:
                verdict = {"verdict": legis.VERDICT_PASS, "reason": "judge disabled", "votes_json": "[]"}
            with led.legislative_conn() as c:
                legis.mark_bill_judged(c, pj["bill_id"], verdict.get("verdict", legis.VERDICT_FAIL),
                                       verdict.get("reason", ""), verdict.get("votes_json", "[]"), now_iso)
            judged += 1
        except Exception:  # noqa: BLE001
            continue

    # MINT proposals from actionable (judged-PASS) impacts, via the pure builder + the
    # existing E1 cooldown + record_universe_proposal path. Link EVERY contributing bill.
    with led.legislative_conn() as c:
        actionable = legis.actionable_impacts(c, min_passage_prob=float(leg.passage_prob_min),
                                              min_magnitude=float(leg.impact_min))
    held = [h["ticker"] for h in led.active_target_portfolio(goal["id"], statuses=("active", "exiting"))]
    allow = cfg.strategy.rh_tradable_confirmed
    props = learn.build_catalyst_proposals(
        actionable, held, add_weight=float(leg.add_weight), tier=learn.TIER_LEGISLATIVE,
        allow=allow, policy_area_map=leg.ticker_policy_areas, remove_enabled=bool(leg.remove_enabled))
    cons = cfg.strategy.consistency or strategy.ConsistencyConfig()
    cooldown_cut = (now - timedelta(days=cons.proposal_cooldown_days)).isoformat()
    recorded = []
    for p in props[:int(leg.max_proposals_per_review)]:
        if p.ticker is None:
            continue
        chash = p.content_hash()
        ckey = p.change_key()
        last = led.last_decided_change_key(goal["id"], ckey)
        if last and (last.get("decided_at") or "") >= cooldown_cut:
            continue  # E1 anti-oscillation (pooled across tiers): recently decided -> hold
        rid = led.record_universe_proposal(
            goal_id=goal["id"], proposed_at=now_iso, kind=p.kind, ticker=p.ticker, sleeve=p.sleeve,
            from_book=None, to_book=None, target_weight=p.target_weight, tier=p.tier,
            content_hash=chash, change_key=ckey, reason=p.reason, goal_gap_pct=None)
        direction = "benefit" if p.kind == learn.ADD else "suffer"
        with led.legislative_conn() as c:
            for row in actionable:
                if (str(row.get("ticker", "")).upper() == p.ticker
                        and str(row.get("direction", "")).lower() == direction):
                    legis.link_impact_proposal(c, row["bill_id"], p.ticker, chash)
        if rid:
            recorded.append(rid)
    return {"reviewed": True, "changed": len(changed), "analyzed": analyzed, "below_gate": below_gate,
            "skipped_enacted": skipped_enacted,
            "judged": judged, "reconciled": reconciled, "minted": len(recorded), "recorded_ids": recorded}


def _default_bill_text(congress_mod, detail: dict, api_key: str) -> str:
    """Resolve a bill's text via the real Congress fetcher (text versions -> newest text)."""
    tvs = congress_mod.fetch_text_versions(detail.get("text_versions_url"), api_key) \
        if detail.get("text_versions_url") else []
    return congress_mod.fetch_bill_text(tvs) if tvs else ""


def _default_bill_analyze(meta, text):
    from lib import legislative_llm
    return legislative_llm.analyze_bill(meta, text)


def _default_bill_judge(meta, analysis, *, votes, pass_min):
    from lib import legislative_llm
    return legislative_llm.judge_bill(meta, analysis, votes=votes, pass_min=pass_min)


def cmd_catalyst_eval(args) -> dict:
    """Observability (never trades): measure the legislative-catalyst signal quality.
      passage  — calibrate congress.heuristic_passage_prob vs KNOWN outcomes (self-labeling
                 backtest over a completed Congress; Brier/AUC/reliability + per-status rates).
      sleeve   — the live report card: the Legislative Catalyst sleeve's realized alpha from
                 resolved decisions (reads the decision-memory scorecard only)."""
    cfg, led = _cfg_and_ledger()
    kind = getattr(args, "kind", "")
    if kind == "passage":
        return _run_passage_eval(cfg, led, args)
    if kind == "sleeve":
        return _run_sleeve_eval(cfg, led)
    if kind == "judge":
        return _run_judge_eval(cfg, led, args)
    return {"error": f"unknown catalyst-eval kind: {kind!r} (use passage|sleeve|judge)"}


def _run_passage_eval(cfg, led, args) -> dict:
    import lib.legislative_backtest as bt
    fixture = (getattr(args, "fixture", "") or "").strip() or "state/fixtures/congress_118.json"
    fixture = fixture if os.path.isabs(fixture) else str(REPO / fixture)
    built = None
    if getattr(args, "build", False):
        built = _build_passage_fixture(cfg, led, int(getattr(args, "congress", 118) or 118), fixture,
                                       n_pos=int(getattr(args, "n_pos", 160) or 160),
                                       n_neg=int(getattr(args, "n_neg", 280) or 280))
    if not os.path.exists(fixture):
        return {"error": f"no fixture at {fixture}; run with --build to fetch it (needs CONGRESS_API_KEY)"}
    bills = json.load(open(fixture, encoding="utf-8"))
    report = bt.calibrate(bt.records_from_fixture(bills))
    return {"eval": "passage", "fixture": fixture, "built": built, **report}


def _build_passage_fixture(cfg, led, congress_num: int, out_path: str, *, n_pos: int, n_neg: int) -> dict:
    """One-time real fetch of a completed Congress -> a re-runnable backtest fixture. Positives =
    enacted laws (/law/{c}); negatives = a sample of un-enacted bills. Every GET is memoized in the
    ledger's congress_cache (permanent — historical data is immutable), so a rebuild is served from
    the DB and never re-hits the quota. Also caches each bill's RAW action timeline so the
    calibration re-scores offline after a derive_status change."""
    import urllib.parse
    from concurrent.futures import ThreadPoolExecutor
    import lib.legislative as legis
    key = cfg.legislative.api_key
    if not key:
        raise RuntimeError("CONGRESS_API_KEY not set (needed to build the fixture)")
    now_iso = market.now_et().isoformat()
    stats: dict = {}
    opener = legis.caching_opener(led, now=now_iso, stats=stats)  # ttl=None: historical data is immutable

    def _get(path, **p):
        q = {"api_key": key, "format": "json", **p}
        url = f"https://api.congress.gov/v3/{path}?{urllib.parse.urlencode(q)}"
        return json.loads(opener(url, 30))

    def _actions(c, bt_type, num):
        return [{"text": a.get("text"), "actionDate": a.get("actionDate")}
                for a in _get(f"bill/{c}/{str(bt_type).lower()}/{num}/actions", limit=250).get("actions", [])]

    laws = []
    for off in (0, 250, 500):
        laws += _get(f"law/{congress_num}", limit=250, offset=off).get("bills", [])
        if len(laws) >= n_pos:
            break
    laws = laws[:n_pos]

    def _pos(b):
        return {"bill_id": f"{congress_num}-{str(b['type']).lower()}-{b['number']}", "enacted": 1,
                "title": b.get("title", ""), "cosponsors_n": 0, "actions": _actions(congress_num, b["type"], b["number"])}
    with ThreadPoolExecutor(max_workers=8) as ex:
        pos = list(ex.map(_pos, laws))
    law_ids = {p["bill_id"] for p in pos}

    cands = []
    for off in (0, 250, 500, 750, 1000, 1250):
        for b in _get(f"bill/{congress_num}", sort="updateDate+desc", limit=250, offset=off).get("bills", []):
            bid = f"{congress_num}-{str(b['type']).lower()}-{b['number']}"
            if bid not in law_ids:
                cands.append(b)
        if len(cands) >= n_neg:
            break
    cands = cands[:n_neg]

    def _neg(b):
        return {"bill_id": f"{congress_num}-{str(b['type']).lower()}-{b['number']}", "enacted": 0,
                "title": b.get("title", ""), "cosponsors_n": 0, "actions": _actions(congress_num, b["type"], b["number"])}
    with ThreadPoolExecutor(max_workers=8) as ex:
        neg = list(ex.map(_neg, cands))

    bills = pos + neg
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bills, f)
    return {"bills": len(bills), "cache": stats}


def _run_sleeve_eval(cfg, led) -> dict:
    """Live report card: realized alpha/hit-rate of the Legislative Catalyst sleeve from resolved
    decisions. Reads the decision-memory scorecard only (never limits/broker)."""
    import lib.memory as memory
    rows = led.all_return_series()
    goal = led.get_active_goal()
    if goal:
        sleeve_of = {r["ticker"]: r.get("sleeve") for r in led.active_target_portfolio(
            goal["id"], statuses=("active", "exiting", "removed"))}
        for r in rows:
            r["sleeve"] = sleeve_of.get(r.get("ticker"))
    n_cat = sum(1 for r in rows if r.get("sleeve") == "Legislative Catalyst")
    block = memory.build_sliced_scorecard(rows, "sleeve", "sleeve/sector")
    return {"eval": "sleeve", "catalyst_decisions_scored": n_cat,
            "note": ("no resolved catalyst decisions yet — this is the live report card that fills in "
                     "as catalyst-added names trade and resolve" if n_cat == 0 else "catalyst sleeve is scoring"),
            "scorecard": block or "(no cross-sectional slice yet)"}


def _labels_path(args) -> str:
    p = (getattr(args, "labels", "") or "").strip() or "state/fixtures/judge_labels.jsonl"
    return p if os.path.isabs(p) else str(REPO / p)


def _run_judge_eval(cfg, led, args, *, judge=None) -> dict:
    """Measure the impact-JUDGE against the operator's human labels: re-run judge_bill with N
    votes over each labeled (bill, analysis), then PURELY sweep pass_min to find the operating
    point that best matches the labels. ``judge`` is injectable (offline test uses a replay)."""
    import lib.legislative_judge_eval as je
    labels_path = _labels_path(args)
    if not os.path.exists(labels_path):
        return {"error": f"no labels at {labels_path}; run `tick.py catalyst-label` first"}
    rows = []
    with open(labels_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get("human_verdict", "")).strip().lower() in ("pass", "fail"):
                rows.append(r)
    if not rows:
        return {"error": f"no pass/fail-labeled rows in {labels_path}"}
    limit = int(getattr(args, "limit", 0) or 0)
    if limit > 0:
        rows = rows[:limit]
    judge = judge or _default_bill_judge
    votes_n = int(getattr(args, "judge_votes", 5) or 5)
    workers = max(1, int(getattr(args, "workers", 6) or 6))

    def _judge_row(r):
        meta = {"bill_id": r.get("bill_id"), "title": r.get("title"), "status": r.get("status")}
        analysis = {"impacted_tickers": r.get("impacted_tickers", []), "thesis": r.get("thesis", "")}
        try:
            v = judge(meta, analysis, votes=votes_n, pass_min=1)
            cast = json.loads(v.get("votes_json", "{}")).get("votes", []) or []
        except Exception:  # noqa: BLE001 — a judge hiccup on one row shouldn't sink the eval
            cast = []
        return (cast, str(r["human_verdict"]).strip().lower())

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        vote_rows = list(ex.map(_judge_row, rows))
    sweep = je.sweep_pass_min(vote_rows, max_votes=votes_n)
    current = next((s for s in sweep if s["pass_min"] == int(cfg.legislative.judge_pass_min)), None)
    return {"eval": "judge", "labels": labels_path, "n": len(rows), "votes_cast": votes_n,
            "current_config": {"judge_votes": cfg.legislative.judge_votes,
                               "judge_pass_min": cfg.legislative.judge_pass_min},
            "current_agreement": current, "sweep": sweep,
            "best_f1": je.best_operating_point(sweep, "f1"),
            "best_precision": je.best_operating_point(sweep, "precision")}


def _load_corpus(path: str) -> list:
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _save_corpus(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def cmd_catalyst_label(args) -> dict:
    """Interactive: grade the QUALITY of each impact-analysis in the corpus (is it coherent,
    grounded, directionally right — NOT a market prediction). Fills in the un-labeled rows a
    `catalyst-seed` produced (and pulls any live-analyzed bills from the ledger into the corpus
    too). The accumulated labels tune the judge via `catalyst-eval judge`. Runs locally."""
    import sys
    cfg, led = _cfg_and_ledger()
    import lib.legislative as legis
    corpus_path = _labels_path(args)
    rows = _load_corpus(corpus_path)
    known = {r.get("bill_id") for r in rows}
    # fold in any live-analyzed bills not already in the corpus, as un-labeled rows
    with led.legislative_conn() as c:
        for b in legis.analyzed_bills(c):
            if b["bill_id"] in known:
                continue
            rows.append({"bill_id": b["bill_id"], "title": b["title"], "status": b["status"],
                         "impacted_tickers": legis.get_bill_impacts(c, b["bill_id"]),
                         "thesis": b["thesis"], "human_verdict": None, "why": ""})
            known.add(b["bill_id"])
    unlabeled = [r for r in rows if str(r.get("human_verdict") or "").lower() not in ("pass", "fail")]
    labeled_total = len(rows) - len(unlabeled)
    if not unlabeled:
        _save_corpus(corpus_path, rows)
        return {"labeled": 0, "corpus": corpus_path, "total_in_corpus": len(rows),
                "already_labeled": labeled_total,
                "reason": "no un-labeled entries — run `catalyst-seed` to add bills to grade"}
    if not sys.stdin.isatty():
        return {"labeled": 0, "corpus": corpus_path, "unlabeled": len(unlabeled),
                "already_labeled": labeled_total,
                "note": "interactive — run `.venv/bin/python tick.py catalyst-label` in a terminal to grade"}
    limit = int(getattr(args, "limit", 20) or 20)
    labeled = 0
    for r in unlabeled[:limit]:
        print("\n" + "=" * 72, file=sys.stderr)
        print(f"{r['bill_id']}  [{r.get('status')}]  ({labeled_total + labeled + 1} of "
              f"{labeled_total + len(unlabeled)})", file=sys.stderr)
        print(f"  {r.get('title')}", file=sys.stderr)
        print(f"  thesis: {r.get('thesis')}", file=sys.stderr)
        for i in (r.get("impacted_tickers") or []):
            print(f"    - {i.get('ticker')}: {i.get('direction')} mag={i.get('magnitude')} "
                  f"({i.get('horizon')}) — {i.get('rationale')}", file=sys.stderr)
        ans = input("  is this impact analysis GOOD? [g]ood / [b]ad / [s]kip / [q]uit: ").strip().lower()
        if ans in ("q", "quit"):
            break
        if ans not in ("g", "good", "b", "bad"):
            continue
        r["human_verdict"] = "pass" if ans in ("g", "good") else "fail"
        r["why"] = input("  why (optional one-liner): ").strip()
        labeled += 1
    _save_corpus(corpus_path, rows)
    return {"labeled": labeled, "corpus": corpus_path, "total_in_corpus": len(rows),
            "labeled_total": labeled_total + labeled,
            "remaining_unlabeled": len(unlabeled) - labeled}


def cmd_catalyst_seed(args) -> dict:
    """Build a labeling corpus: fetch recent SUBSTANTIVE bills (reached committee+; cached), run
    the real impact analysis on each (parallel), and append the ones that name tickers to the
    corpus as un-labeled rows for `catalyst-label`. Decoupled from the live trading ledger — a
    seed bill is NEVER judged or proposed. Cost = one LLM analysis per candidate; bounded by
    --max-analyze + a deadline."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    import lib.legislative as legis
    from lib.dataflows import congress
    from lib.dataflows.errors import DataUnavailableError
    cfg, led = _cfg_and_ledger()
    leg = cfg.legislative
    if not leg.api_key:
        return {"error": "CONGRESS_API_KEY not set"}
    corpus_path = _labels_path(args)
    have = {r.get("bill_id") for r in _load_corpus(corpus_path)}
    target = int(getattr(args, "n", 15) or 15)
    max_analyze = int(getattr(args, "max_analyze", 40) or 40)
    lookback = int(getattr(args, "lookback_days", 240) or 240)
    deadline = time.monotonic() + int(getattr(args, "deadline_sec", 540) or 540)
    now = market.now_et()
    stats: dict = {}
    opener = legis.caching_opener(led, now=now.isoformat(), stats=stats)

    # candidate pool: recent bills that reached a SUBSTANTIVE stage (impact-likely), newest first.
    since = (now - timedelta(days=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        pool = congress.poll_changed_bills(since, until, leg.api_key, page_limit=250, max_pages=6, opener=opener)
    except DataUnavailableError as e:
        return {"error": f"poll failed: {e}", "cache": stats}
    substantive = {"reported", "passed_one", "passed_both", "to_president", "became_law"}
    cands = [b for b in pool
             if congress.derive_status(b.get("latest_action")) in substantive and b["bill_id"] not in have]
    cands = cands[:max_analyze]

    def _analyze_one(b):
        if time.monotonic() > deadline:
            return None
        try:
            detail = congress.fetch_bill_detail(b["congress"], b["bill_type"], b["number"], leg.api_key, opener=opener)
            tvs = congress.fetch_text_versions(detail.get("text_versions_url"), leg.api_key, opener=opener) \
                if detail.get("text_versions_url") else []
            text = congress.fetch_bill_text(tvs, opener=opener) if tvs else ""
            meta = {"bill_id": b["bill_id"], "title": detail.get("title") or b.get("title"),
                    "status": detail.get("status"), "policy_codes": detail.get("policy_codes"),
                    "latest_action": detail.get("latest_action")}
            analysis = _default_bill_analyze(meta, text)
            if not analysis.get("impacted_tickers"):
                return None
            return {"bill_id": b["bill_id"], "title": meta["title"], "status": meta["status"],
                    "impacted_tickers": analysis["impacted_tickers"], "thesis": analysis.get("thesis", ""),
                    "human_verdict": None, "why": ""}
        except Exception:  # noqa: BLE001 — one bad bill never sinks the seed
            return None

    seeded = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for row in ex.map(_analyze_one, cands):
            if row and row["bill_id"] not in have:
                seeded.append(row)
                have.add(row["bill_id"])
                if len(seeded) >= target:
                    break
    corpus = _load_corpus(corpus_path)
    corpus.extend(seeded)
    _save_corpus(corpus_path, corpus)
    return {"seeded": len(seeded), "analyzed_candidates": len(cands), "corpus": corpus_path,
            "total_in_corpus": len(corpus), "cache": stats,
            "next": "label them: .venv/bin/python tick.py catalyst-label"}


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
    recurrence = led.count_proposal_recurrence(goal["id"], change["change_key"]) if goal else 0
    if auto and change["kind"] in ("PROPOSE_REMOVE", "PROPOSE_DERISK", "PROPOSE_ADD"):
        if recurrence < cons.universe_confirm_days:
            return {"applied": False, "change": change,
                    "reason": f"not yet confirmed: seen on {recurrence}/{cons.universe_confirm_days} "
                              "distinct days (re-proposed until it persists)"}

    effect = "recorded"
    if change["kind"] == "PROPOSE_REMOVE" and change.get("ticker") and goal:
        import lib.risk as risk
        import lib.universe as universe
        # A legislative-catalyst REMOVE gets an allow-list gate AT APPLY TIME (the shared
        # REMOVE branch otherwise has none, and --approve skips the underperf re-check). Scoped
        # to tier=='legislative' so the shipped derisk/underperf REMOVE path is untouched.
        if change.get("tier") == "legislative":
            allow = cfg.strategy.rh_tradable_confirmed if cfg.strategy is not None else set()
            ok_al, why_al = universe.validate_add(change["ticker"], allow, allow)
            if not ok_al:
                return {"applied": False, "change": change,
                        "reason": f"refusing legislative remove: {why_al}"}
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


# --- FACTOR ATTRIBUTION (diagnostic; NOT part of the tick) -------------------
# Read-only observability, in the style of decision-proof / strategy-history. It
# answers "how much of this book's return is market exposure rather than
# selection" and NOTHING else: it is not wired into run_tick.py, nothing in the
# trading path consumes its output, and it never returns a verdict or threshold.
#
# Two estimates are always printed side by side, because they answer different
# questions and have wildly different sample sizes:
#   * bottom-up (PRIMARY) -- the book's weights times each name's own beta, at
#     n in the hundreds per name. Ex-ante designed exposure of the static book.
#   * top-down (SECONDARY) -- a regression of the account's own return series on
#     the benchmark. Honest but tiny-n; inference is withheld below the df floor.

FACTORS_SIDECAR = "factors.json"


def _factors_path():
    """Sidecar location. Overridable so tests never touch the live scratch file."""
    return Path(os.environ.get("QUIVER_FACTORS_PATH")
                or (REPO / "state" / "tmp" / FACTORS_SIDECAR))


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


# --- benchmark measurement (outcomes.benchmark_return / alpha) ------------------
# The market leg of the decision-memory scorecard. It USED to be computed by the LLM
# orchestrator per TICK.md STEP 6b, which is why it was NULL on 44 of 51 rows and,
# where present, priced off a series that stopped four days short of the window it
# was measuring. Python owns it now: a bounded producer writes the closes, and the
# backfill below is the ONLY thing that writes the two columns.
#
# Split into fetch/apply on purpose. Nothing here runs inside cmd_reflect: reflect
# executes inside the orchestrator's shared deadline, and a stalled yfinance socket
# cannot be caught by try/except -- only bounded by a separate process.

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


def _benchmark_scorecard(db_path, tickers, cfg) -> dict:
    """{ticker: {multiplier, tier}} through the REAL consumers, so the printed diff
    cannot drift from what the trading loop will actually compute."""
    from lib import calibrate, reflect_memory

    led = Ledger(str(db_path))
    mult = calibrate.build_calibration(led, tickers,
                                       min_n=int(cfg.strategy.learning.min_resolved_n))
    out = {}
    for t in tickers:
        tier = None
        try:
            tier = reflect_memory.build_metric_bundle(led, t, cfg.memory)["ticker"]["guidance"].tier
        except Exception:  # noqa: BLE001 — the tier is reporting only; never block the diff
            tier = "unavailable"
        out[t] = {"multiplier": mult.get(t, 1.0), "tier": tier}
    return out


def cmd_benchmark_backfill(args) -> dict:
    """Recompute outcomes.benchmark_return/alpha from the sidecar. DRY-RUN unless --apply.

    Prints the before/after conviction multiplier AND guidance tier for every affected
    name, because this is a learning input that reaches order dollars -- not plumbing.
    """
    import shutil
    import tempfile

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
    cfg = load_config(CONFIG_PATH)
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

    tickers = sorted({r["ticker"] for r in rows if r.get("ticker")}) or \
        sorted({r["ticker"] for r in led.outcomes_for_backfill() if r.get("ticker")})
    before = _benchmark_scorecard(LEDGER_DB, tickers, cfg)

    # The "after" side is measured on a COPY, so a dry-run writes nothing anywhere.
    tmpdir = tempfile.mkdtemp(prefix="quiver_benchmark_")
    try:
        shadow = Path(tmpdir) / "after.db"
        shutil.copy(str(LEDGER_DB), str(shadow))
        sled = Ledger(str(shadow))
        for r in changes:
            sled.update_outcome_benchmark(r["decision_id"], r["new_benchmark_return"], r["new_alpha"])
        after = _benchmark_scorecard(shadow, tickers, cfg)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    diff = []
    for t in tickers:
        b, a = before[t], after[t]
        if abs(b["multiplier"] - a["multiplier"]) > 1e-9 or b["tier"] != a["tier"]:
            diff.append({"ticker": t,
                         "multiplier_before": round(b["multiplier"], 4),
                         "multiplier_after": round(a["multiplier"], 4),
                         "tier_before": b["tier"], "tier_after": a["tier"]})
    out["calibration_diff"] = diff

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
                    proof_json=json.dumps({"counts": counts, "calibration_diff": diff,
                                           "benchmark": out["benchmark"],
                                           "fill_only": fill_only}))
            except Exception:  # noqa: BLE001 — the change-log is observability; never blocks
                pass
    out["ok"] = True
    return out


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


def cmd_attribution(args) -> dict:
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
    led = Ledger(LEDGER_DB)
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
            cfg = load_config(CONFIG_PATH)
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
    from lib import telegram  # local: keep `import tick` free of the egress (block-J import graph)
    plan = data.get("plan") or {}
    baseline = led.get_baseline(date)
    actions = led.day_actions(date)
    orders = led.day_orders(date)
    _tg_token, _tg_chats = telegram.resolve_env()

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
        # Digest footer hint: is the Telegram pager actually armed on this box? (a bot token
        # + at least one resolvable chat id). So every healthy digest passively confirms the
        # alert channel — a NOT-configured footer is itself a signal (the box needs the
        # TELEGRAM_* secrets in /etc/quiver/quiver.env; see F7 / docs/DEPLOY.md).
        "pager_armed": bool(_tg_token and _tg_chats) if kind == "digest" else None,
        # Telegram loudness policy input (is_silent): a routine digest pings LOUD unless
        # the operator opted into the quiet-daily-record mode via notify.loud_digest: false.
        "loud_digest": cfg.notify.loud_digest,
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
        "telegram": digest["telegram"],
        "silent": notify.is_silent(model),
        "content_hash": digest["content_hash"],
    }


def _run_report_send(cfg, led, data: dict) -> dict:
    """Build + SEND the digest/alert to Telegram + record the dedup row — ONE atomic,
    best-effort Python step. Replaces the orchestrator's old report -> Resend-MCP send ->
    report-commit dance (there is no Telegram MCP; Telegram is a plain HTTPS POST).

    NEVER lets a failure become a tick error: a send failure / missing token / malformed input
    returns ``{"sent": False, ...}`` (not an exception), so ``main`` prints it with exit 0 and a
    report/alert can never abort or alter a trading tick. The dedup row is written only after a
    confirmed delivery to the PRIMARY chat (at-least-once); a secondary-chat failure is surfaced
    as ``partial`` but never blocks the row (else a dead extra chat would re-spam the primary).
    """
    from lib import telegram  # local: ops-layer egress, NOT the trading brain (block-J graph)
    try:
        rep = _run_report(cfg, led, data)
        if not rep.get("should_send"):
            return {"sent": False, "reason": rep.get("reason"),
                    "kind": rep.get("kind"), "stage": rep.get("stage", "")}
        kind, stage, date = rep["kind"], rep.get("stage", ""), rep["date"]
        now_iso = data.get("now_iso") or market.now_et().isoformat()
        token, chat_ids = telegram.resolve_env()
        if not token or not chat_ids:
            return {"sent": False, "reason": "unconfigured",
                    "detail": "no_token" if not token else "no_chats",
                    "kind": kind, "stage": stage}
        res = telegram.send_message(token=token, chat_ids=chat_ids, text=rep["telegram"],
                                    disable_notification=bool(rep.get("silent")))
        if res.get("ok"):
            led.mark_notified(date, kind, rep["content_hash"], ",".join(chat_ids),
                              now_iso, stage=stage)
            return {"sent": True, "partial": bool(res.get("partial")), "kind": kind,
                    "stage": stage, "hash": rep["content_hash"], "chats": len(chat_ids)}
        return {"sent": False, "reason": "send_failed", "detail": res.get("error"),
                "kind": kind, "stage": stage}
    except Exception as e:  # noqa: BLE001 — report-send is observability; NEVER fail a tick
        return {"sent": False, "error": f"{type(e).__name__}: {e}"}


def cmd_report_send(args) -> dict:
    # Fully defensive: even a broken config / missing input file returns {sent:False} (exit 0),
    # so `report-send` can NEVER abort a tick — it is strictly observability (TICK.md STEP 7b).
    try:
        cfg, led = _cfg_and_ledger()
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — never fail a tick on a report input/config error
        return {"sent": False, "error": f"{type(e).__name__}: {e}"}
    return _run_report_send(cfg, led, data)


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
        "equity": 100.0, "host": os.environ.get("QUIVER_HOST_HINT") or None,
    }
    model = _build_report_model(cfg, led, data, date, now_iso, kind)
    built = notify.build_digest(model)
    token, chat_ids = telegram.resolve_env()
    if args.to:  # explicit override: comma/space-separated chat id(s) for the test
        chat_ids = telegram._recipients(args.to)
    res = telegram.send_message(token=token, chat_ids=chat_ids,
                                text="[SELF-TEST] " + built["telegram"],
                                disable_notification=False)
    return {"sent": bool(res.get("ok")), "result": res, "chats": chat_ids,
            "partial": bool(res.get("partial")), "kind": kind, "stage": notify.stage_of(model)}


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
#        "realized_pnl": 0.0}
#   ]}
#
# benchmark_return is NOT accepted here. It used to be, and the orchestrator was told
# to compute a SPY window return "or omit" — so it was omitted on 44 of 51 rows and,
# where supplied, priced off a series ending four days before the window it measured.
# Python owns that measurement now (`tick.py benchmark-backfill`), which also means it
# is computed only from FINAL closes: at reflect time the resolve date's session is
# still open, so the number simply does not exist yet. The row is written with a NULL
# market leg and the backfill fills it on a later run; a NULL alpha degrades exactly as
# before (the scorecard grades that row on its absolute return).
#
# trust_input_benchmark is the ONE exception, for IN-PROCESS Python callers that own a
# deterministic benchmark series of their own — lib/wall_replay.py replaying history,
# and the alpha-arithmetic e2e. The argparse CLI never sets it, so the orchestrator
# path cannot reach it.

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

def cmd_event_risk(_args) -> dict:
    """PRODUCER for the event-risk / non-confirmation sidecar (F8/F9). Runs as its OWN subprocess
    with an outer timeout (B3: a yfinance socket stall can't be caught by try/except, only bounded
    by the subprocess), invoked by run_tick.py BEFORE the orchestrator. Best-effort per ticker;
    ALWAYS writes state/tmp/event_risk.json (`{}` on total failure), day-stamped so tick.py plan
    ignores a stale sidecar (B6). Analysis-side: event_risk.build_descriptor is wall-clean; the
    yfinance/trend fetch is binding-side glue that only emits a descriptor the plan reads as an arg.
    """
    import socket
    from lib import event_risk as _er
    day = market.trading_day_et()
    now_iso = market.now_et().isoformat()
    names: dict = {}
    try:
        cfg = load_config(CONFIG_PATH)
        led = Ledger(LEDGER_DB)
        universe_tks = _analysis_universe(cfg, led)
        socket.setdefaulttimeout(15)      # bound each yfinance call (belt-and-suspenders under the outer timeout)
        import yfinance as yf             # noqa: F401 — best-effort optional dep
        from lib import trend as _trend
        for tk in universe_tks:
            try:
                hist = yf.Ticker(tk).history(period="1y", auto_adjust=True)
                closes = [float(x) for x in list(hist["Close"]) if x and x == x]
                if len(closes) < 30:
                    continue
                rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
                        for i in range(1, len(closes)) if closes[i - 1]]
                bundle = _trend.trend_metrics(closes)
                dvol = _trend.downside_deviation(rets)
                recent = ((closes[-1] - closes[-6]) / closes[-6]) if len(closes) >= 6 and closes[-6] else None
                # Earnings date = the nearest known binary event (F8). Best-effort; None -> no event.
                ev_iso = None
                try:
                    cal = yf.Ticker(tk).calendar
                    ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
                    if isinstance(ed, (list, tuple)) and ed:
                        ed = ed[0]
                    ev_iso = str(ed)[:10] if ed else None
                except Exception:  # noqa: BLE001 — earnings calendar is flaky; skip on any error
                    ev_iso = None
                desc = _er.build_descriptor(
                    now_iso=now_iso, event_iso=ev_iso, downside_vol=dvol,
                    trend_regime=bundle.trend_regime, momentum=bundle.mom_12_1,
                    recent_return=recent, news_polarity=None)
                if desc and (desc.get("severity") or desc.get("downside_vol") is not None
                             or desc.get("trend_regime")):
                    names[tk.upper()] = desc
            except Exception:  # noqa: BLE001 — one ticker's fetch failing must not sink the rest
                continue
    except Exception as e:  # noqa: BLE001 — a total failure still writes {} so plan degrades cleanly
        _write_event_risk({"trading_day": day, "names": {}})
        return {"error": f"{type(e).__name__}: {e}", "written": 0, "day": day}
    _write_event_risk({"trading_day": day, "names": names})
    return {"written": len(names), "day": day}


def _write_event_risk(payload: dict) -> None:
    """Always-write the sidecar (B6): create state/tmp and write event_risk.json; best-effort."""
    try:
        tmp = REPO / "state" / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "event_risk.json").write_text(json.dumps(payload))
    except OSError:
        pass


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


# ==================== STRATEGY INTELLIGENCE (lib/intel) glue ===============
# Binding-side orchestration for the section-level intelligence service. Reads config + the
# ledger and drives the PURE lib/intel modules. Best-effort + human-gated: intel proposals are
# tier 'intel' which matches no auto-apply branch, so they ALWAYS need universe-apply --approve.
def cmd_intel_ingest(args) -> dict:
    """Load an analyzed-document bundle (one doc + its chained sections) into the intel store.

    Input JSON: {"doc": {doc_id, kind, title, published, sponsor, congress[, agency]},
                 "results": [{"sec","chain":{...},"verify":{...}}, ...]}
    The `results` shape is exactly what the section chain emits, so live and backfill share this
    path. Idempotent per (doc_id, sec, ticker)."""
    cfg, led = _cfg_and_ledger()
    data = _load_input(args)
    from lib.intel import sections as _isec, prefilter as _ipf, store as _istore
    doc = data.get("doc") or {}
    now_iso = market.now_et().isoformat()
    ndoc = nsec = nimp = 0
    with led.intel_conn() as c:
        _istore.upsert_document(c, doc_id=doc["doc_id"], kind=doc.get("kind", "bill"),
                                title=doc.get("title", ""), published=doc["published"],
                                sponsor=doc.get("sponsor"), congress=doc.get("congress"),
                                agency=doc.get("agency"), fetched_at=now_iso)
        ndoc = 1
        for r in data.get("results", []):
            ch = r.get("chain") or {}
            vf = r.get("verify") or {}
            sec = str(ch.get("sec") or r.get("sec") or "")
            if not sec:
                continue
            _istore.upsert_section(c, doc_id=doc["doc_id"], sec=sec, header=ch.get("header", ""),
                                   kept=True)
            nsec += 1
            # span-verbatim: deterministic char check the caller may have precomputed in verify
            span = ch.get("step1_span", "")
            verdicts = {h.get("ticker"): h.get("verdict") for h in (vf.get("hits_upheld") or [])}
            for hit in (ch.get("step4_book_hits") or []):
                _istore.record_impact(
                    c, doc_id=doc["doc_id"], sec=sec, ticker=hit["ticker"],
                    direction=hit.get("direction", ""), confidence=hit.get("confidence", ""),
                    step1_what_changes=ch.get("step1_what_changes", ""), step1_span=span,
                    step2_mechanism=ch.get("step2_mechanism", ""), reasoning=hit.get("reasoning", ""),
                    verify_verdict=verdicts.get(hit["ticker"], ""),
                    span_verbatim=bool(vf.get("span_is_verbatim")),
                    model=doc.get("model", ""), prompt_version=doc.get("prompt_version", "v1"),
                    scored_at=now_iso)
                nimp += 1
    return {"ingested": True, "documents": ndoc, "sections": nsec, "impacts": nimp}


def cmd_intel_power_load(args) -> dict:
    """Load point-in-time power rows (chairs/leadership/agency heads). Input: {"power": [
    {bioguide, congress, role, committee, chamber, name, source}, ...]}."""
    cfg, led = _cfg_and_ledger()
    data = _load_input(args)
    from lib.intel import store as _istore
    n = 0
    with led.intel_conn() as c:
        for p in data.get("power", []):
            _istore.upsert_power(c, bioguide=p["bioguide"], congress=int(p["congress"]),
                                 role=p.get("role", ""), committee=p.get("committee", ""),
                                 chamber=p.get("chamber", ""), name=p.get("name", ""),
                                 source=p.get("source", ""))
            n += 1
    return {"loaded": True, "power_rows": n}


def cmd_intel_refresh(args) -> dict:
    """One live intelligence pass: poll recent bills (Congress.gov) + rules (Federal Register),
    split -> prefilter -> chain -> ingest. Best-effort; writes only intel tables. Fail-SAFE OFF
    unless intel.enabled. The allow-list + book description come from strategy.yaml."""
    cfg, led = _cfg_and_ledger()
    ic = getattr(cfg, "intel", None)
    if ic is None or not ic.enabled:
        return {"skipped": "intel disabled (config: intel.enabled)"}
    from lib.intel import refresh as _iref
    from lib.dataflows import congress as _cong
    data = _load_input(args)
    now_iso = market.now_et().isoformat()
    sc = cfg.strategy
    allow = sorted({t.upper() for t in (sc.rh_tradable_confirmed or [])}) if sc else []
    book_desc = "AI-infrastructure / semiconductors / nuclear power / data centers / grid / space"
    # Documents: from --input if provided (backfill/replay), else POLL live — bills (Congress) AND
    # rules (Federal Register). Both flow through the same split -> prefilter -> chain -> ingest.
    docs = data.get("documents", [])
    polled_bills = polled_rules = 0
    if not docs:
        maxd = int(data.get("max_docs", 25))
        bdocs, polled_bills = _intel_poll_bill_docs(cfg, max_docs=maxd)
        rdocs, polled_rules = _intel_poll_rule_docs(cfg, max_docs=maxd)
        # ensure the agencies behind any polled rules exist as key players (so their profiles render)
        if rdocs:
            _intel_seed_agency_power(led, now_iso)
        docs = bdocs + rdocs

    # Pass the connection FACTORY (not an open conn): run_refresh commits per document, so the slow
    # per-section fetch + claude -p I/O holds no ledger lock and a timeout keeps completed docs.
    mx = getattr(ic, "max_sections_per_run", 120)
    out = _iref.run_refresh(led.intel_conn, documents=docs, allow=allow, book_desc=book_desc,
                            fetch_text=lambda d: _cong._default_opener(
                                d.get("text_url") or d.get("body_url") or "", 30),
                            now=now_iso, max_chained_sections=int(mx))
    return {"refreshed": True, "polled_bills": polled_bills, "polled_rules": polled_rules, **out}


def _intel_poll_bill_docs(cfg, *, max_docs=25):
    """Discover recent bills to analyse: poll Congress.gov for changed bills, pull each sponsor
    bioguide + introduced date, and construct the govinfo bulk USLM URL (the section-XML the
    splitter reads). Best-effort + bounded — a bill that won't resolve is skipped. Returns
    (documents, n_polled). Bills only — the Federal Register arm (lib/dataflows/fedreg) is polled
    separately by `_intel_poll_rule_docs` and IS in the intel auto-loop (cmd_intel_refresh; commit
    9a39dac); this helper just covers the Congress-bill half."""
    from lib.dataflows import congress as _cong
    leg = cfg.legislative
    api_key = getattr(leg, "api_key", "") or ""
    if not api_key:
        return [], 0
    from datetime import timedelta
    until = market.now_et()
    since = (until - timedelta(days=int(getattr(leg, "lookback_days", 3) or 3)))
    try:
        bills = _cong.poll_changed_bills(since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         until.strftime("%Y-%m-%dT%H:%M:%SZ"), api_key,
                                         max_pages=2)
    except Exception:  # noqa: BLE001 — best-effort; a poll outage yields no docs, never raises
        return [], 0
    # Only law-making instruments (bills + joint resolutions) — simple/concurrent resolutions
    # can't become law and rarely touch a company, so they'd waste chain calls.
    LAWMAKING = {"hr", "s", "hjres", "sjres"}
    docs = []
    for b in bills:
        if len(docs) >= max_docs:
            break
        btype = b["bill_type"]
        if btype not in LAWMAKING:
            continue
        try:
            det = _cong.fetch_bill_detail(b["congress"], btype, b["number"], api_key)
        except Exception:  # noqa: BLE001
            continue
        sponsor = det.get("sponsor_bioguide", "")
        introduced = det.get("introduced_date", "")
        if not sponsor or not introduced:
            continue
        # session 1 = odd (congress-start) year, 2 = even; the INTRODUCED version exists for
        # essentially every freshly-introduced instrument — House-origin -> 'ih', Senate -> 'is'
        # (the validated govinfo bulk USLM path with real <section> elements).
        year = int(introduced[:4]) if introduced[:4].isdigit() else until.year
        session = 1 if year % 2 == 1 else 2
        ver = "ih" if btype.startswith("h") else "is"
        url = (f"https://www.govinfo.gov/bulkdata/BILLS/{b['congress']}/{session}/{btype}/"
               f"BILLS-{b['congress']}{btype}{b['number']}{ver}.xml")
        docs.append({"doc_id": b["bill_id"], "kind": "bill", "title": b.get("title", ""),
                     "published": introduced, "sponsor": sponsor, "congress": b["congress"],
                     "text_url": url})
    return docs, len(bills)


# Agency -> short code + display name. The regulatory key players (the CFR-by-agency crosswalk):
# a rule attributes to its issuing agency, whose "profile" is what industries its rules point at.
_INTEL_AGENCIES = [
    ("nuclear regulatory", "NRC", "Nuclear Regulatory Commission"),
    ("federal energy regulatory", "FERC", "Federal Energy Regulatory Commission"),
    ("energy department", "DOE", "Department of Energy"),
    ("federal communications", "FCC", "Federal Communications Commission"),
    ("industry and security", "BIS", "Bureau of Industry and Security"),
    ("federal aviation", "FAA", "Federal Aviation Administration"),
    ("securities and exchange", "SEC", "Securities and Exchange Commission"),
    ("commodity futures", "CFTC", "Commodity Futures Trading Commission"),
    ("environmental protection", "EPA", "Environmental Protection Agency"),
]
_INTEL_AGENCY_CONGRESS = 0  # sentinel "regulatory era" — rules aren't congress-scoped


def _intel_agency_code(agency_name: str):
    """Map an FR agency name to (code, display) if it's a book-relevant regulator, else None."""
    a = (agency_name or "").lower()
    for needle, code, disp in _INTEL_AGENCIES:
        if needle in a:
            return code, disp
    return None


def _intel_seed_agency_power(led, now_iso):
    """Seed the book-relevant regulators as key players (congress=0), so a rule attributes to its
    agency and an agency profile renders — mirroring the congressional power table."""
    from lib.intel import store as _istore
    with led.intel_conn() as c:
        for _needle, code, disp in _INTEL_AGENCIES:
            _istore.upsert_power(c, bioguide=code, congress=_INTEL_AGENCY_CONGRESS,
                                 role="agency", committee=code, chamber="executive",
                                 name=disp, source="intel-agency-map")


def _intel_poll_rule_docs(cfg, *, max_docs=25):
    """Discover recent Federal Register RULES from book-relevant agencies. Each rule attributes to
    its agency code (the key player); the full-text XML is the FR-schema section source. Best-effort
    + bounded. Returns (documents, n_polled)."""
    from lib.dataflows import fedreg as _fr
    from datetime import timedelta
    until = market.now_et()
    lookback = int(getattr(cfg.legislative, "lookback_days", 3) or 3)
    since = (until - timedelta(days=lookback))
    try:
        rules = _fr.poll_rules(since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d"),
                               doc_type="RULE", per_page=100, max_pages=2)
    except Exception:  # noqa: BLE001 — best-effort; an FR outage yields no docs, never raises
        return [], 0
    docs = []
    for r in rules:
        if len(docs) >= max_docs:
            break
        code_disp = _intel_agency_code(r.get("agency", ""))
        if not code_disp:  # only agencies that regulate a book sleeve
            continue
        code, _disp = code_disp
        dn = str(r["doc_id"]).replace("FR-", "")
        pub = r.get("published", "")
        # FR full-text XML: /documents/full_text/xml/YYYY/MM/DD/<docnum>.xml
        text_url = (f"https://www.federalregister.gov/documents/full_text/xml/"
                    f"{pub[:4]}/{pub[5:7]}/{pub[8:10]}/{dn}.xml") if len(pub) >= 10 else ""
        if not text_url:
            continue
        docs.append({"doc_id": r["doc_id"], "kind": "rule", "title": r.get("title", ""),
                     "published": pub, "sponsor": code, "congress": _INTEL_AGENCY_CONGRESS,
                     "agency": r.get("agency", ""), "text_url": text_url})
    return docs, len(rules)


def cmd_intel_profile(args) -> dict:
    """Render one markdown profile per key player (a sponsor who held power) into
    state/profiles/{bioguide}.md. Hash-gated: an unchanged profile is not rewritten."""
    cfg, led = _cfg_and_ledger()
    from lib.intel import aggregate as _iagg, profiles as _iprof, store as _istore
    from lib import fsutil
    as_of = market.trading_day_et()
    outdir = Path(LEDGER_DB).resolve().parent / "profiles"
    written, skipped = [], []
    with led.intel_conn() as c:
        players = _istore.all_key_player_sponsors(c)
        for pl in players:
            bio, cong = pl["bioguide"], int(pl["congress"])
            power = _istore.power_for(c, bio, cong)
            impacts = _istore.impacts_for_sponsor(c, bio)
            prof = _iagg.build_profile(bioguide=bio, congress=cong, power=power, impacts=impacts)
            rendered = _iprof.render(prof, as_of=as_of)
            path = outdir / f"{bio}.md"
            # hash-gate: skip rewrite when the semantic body is unchanged (stable diffs)
            if path.exists() and f"input_hash: {rendered['content_hash']}" in path.read_text(encoding="utf-8"):
                skipped.append(bio)
                continue
            fsutil.atomic_write_text(path, rendered["md"])
            written.append(bio)
    return {"profiles_written": written, "skipped_unchanged": skipped, "dir": str(outdir)}


def cmd_intel_propose(args) -> dict:
    """Aggregate the book-level key-player posture and emit ADDITIVE shadow proposals. With
    --record, also mint them as tier='intel' universe proposals (human-approve only). Never
    reduces a protected sleeve — the proposer can only add."""
    cfg, led = _cfg_and_ledger()
    from lib.intel import aggregate as _iagg, propose as _iprop, store as _istore
    record = bool(getattr(args, "record", False))
    now_iso = market.now_et().isoformat()
    # book snapshot from strategy.yaml (weights + protected + cash + allow-list)
    book = _intel_book_snapshot(cfg)
    # aggregate every key player's impacts into a book-level per-ticker posture
    agg: dict = {}
    with led.intel_conn() as c:
        for pl in _istore.all_key_player_sponsors(c):
            impacts = _istore.impacts_for_sponsor(c, pl["bioguide"])
            for tk in {i["ticker"] for i in impacts}:
                p = _iagg.posture_for_ticker([i for i in impacts if i["ticker"] == tk])
                if tk not in agg:
                    agg[tk] = p
                else:  # pool evidence across players
                    agg[tk]["score"] = round(agg[tk]["score"] + p["score"], 4)
                    agg[tk]["evidence"] += p["evidence"]
                    agg[tk]["posture"] = ("helps" if agg[tk]["score"] > 1e-9
                                          else "hurts" if agg[tk]["score"] < -1e-9 else "neutral")
        pcfg = _intel_propose_cfg(cfg)
        proposals = _iprop.propose(postures=agg, book=book, cfg=pcfg)
        # A protected-name REMOVE must have cleared the higher threat bar (belt-and-suspenders on
        # propose()'s own gate — conservation is a higher bar, not a wall).
        _below = _iprop.protected_reduction_below_bar(proposals, book, pcfg)
        assert not _below, f"intel protected reduction below the threat bar: {_below}"
        recorded = []
        with led.intel_conn() as ic:
            for pr in proposals:
                _istore.record_shadow_proposal(
                    ic, created_at=now_iso, kind=pr["kind"], ticker=pr["ticker"],
                    sleeve=pr.get("sleeve"), target_weight=pr.get("target_weight"),
                    direction=pr["direction"], evidence={"score": pr.get("score"),
                                                         "evidence": pr.get("evidence", [])})
        if record:
            goal = led.get_active_goal()
            if goal:
                import lib.learn as learn
                # ADD/SLEEVE mint as PROPOSE_ADD; a threat mints as PROPOSE_REMOVE (winds to cash
                # via the shared exiting path at apply time). Both tier 'intel' -> human --approve.
                _kindmap = {"PROPOSE_ADD": "PROPOSE_ADD", "PROPOSE_SLEEVE": "PROPOSE_ADD",
                            "PROPOSE_REMOVE": "PROPOSE_REMOVE"}
                for pr in proposals:
                    kind = _kindmap.get(pr["kind"])
                    if not kind:
                        continue
                    p = learn.Proposal(kind=kind, ticker=pr["ticker"], sleeve=pr.get("sleeve"),
                                       tier=_iprop.TIER_INTEL, reason=pr["reason"],
                                       target_weight=pr.get("target_weight"))
                    rid = led.record_universe_proposal(
                        goal_id=goal["id"], proposed_at=now_iso, kind=p.kind, ticker=p.ticker,
                        sleeve=p.sleeve, from_book=None, to_book=None, target_weight=p.target_weight,
                        tier=p.tier, content_hash=p.content_hash(), change_key=p.change_key(),
                        reason=p.reason, goal_gap_pct=None)
                    if rid:
                        recorded.append(rid)
    return {"proposed": len(proposals), "shadow_written": len(proposals),
            "recorded_ids": recorded, "postures": {t: p["posture"] for t, p in agg.items()}}


def _intel_book_snapshot(cfg):
    """Build a lib.intel.propose.BookSnapshot from strategy.yaml. PURE-ish glue (reads cfg only)."""
    from lib.intel.propose import BookSnapshot
    import lib.strategy as strategy
    held, tradable, sleeves, tsleeve = set(), set(), {}, {}
    cash_pct = 0.0
    sc = cfg.strategy
    if sc is not None:
        tradable = {t.upper() for t in (sc.rh_tradable_confirmed or [])}
        book = sc.book(sc.default_book) if hasattr(sc, "book") else None
        holdings = getattr(book, "holdings", []) if book else []
        cash_ticker = (getattr(cfg.risk, "cash_sleeve_ticker", None) or "SGOV").upper()
        for h in holdings:
            tk = str(getattr(h, "ticker", "")).upper()
            w = float(getattr(h, "weight", 0.0) or 0.0)
            sl = str(getattr(h, "sleeve", "") or "")
            held.add(tk)
            tsleeve[tk] = sl
            sleeves.setdefault(sl, {"weight": 0.0, "protected": True, "tickers": []})
            sleeves[sl]["weight"] += w
            sleeves[sl]["tickers"].append(tk)
            if tk == cash_ticker:
                cash_pct += w
    return BookSnapshot(held=held, tradable=tradable, sleeves=sleeves, cash_pct=cash_pct,
                        ticker_sleeve=tsleeve)


def _intel_propose_cfg(cfg):
    from lib.intel.propose import ProposeConfig
    ic = getattr(cfg, "intel", None)
    if ic is None:
        return ProposeConfig()
    return ProposeConfig(min_score=ic.min_score, add_weight=ic.add_weight,
                         intel_max_total_pct=ic.intel_max_total_pct,
                         new_sleeve_min_names=ic.new_sleeve_min_names,
                         threat_score=ic.threat_score,
                         protected_threat_score=ic.protected_threat_score)


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
    p_rsend = sub.add_parser("report-send")
    p_rsend.add_argument("--input", required=True)
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
    p_st.add_argument("--detail", default="",
                      help="override the alert body detail (preview a real failure message)")
    p_reflect = sub.add_parser("reflect")
    p_reflect.add_argument("--input", required=True)
    p_protect = sub.add_parser("protect")
    p_protect.add_argument("--input", required=True)
    p_ss = sub.add_parser("strategy-set")
    p_ss.add_argument("--input", required=False)
    p_con = sub.add_parser("construct")
    p_con.add_argument("--input", required=False)
    sub.add_parser("goal-track")
    p_fr = sub.add_parser("flow-record")
    p_fr.add_argument("--input", required=True)
    sub.add_parser("flow-suggest")
    p_lr = sub.add_parser("learn-review")
    p_lr.add_argument("--input", required=False)
    p_br = sub.add_parser("bills-review")
    p_br.add_argument("--input", required=False)
    p_ce = sub.add_parser("catalyst-eval")
    p_ce.add_argument("kind", choices=["passage", "sleeve", "judge"])
    p_ce.add_argument("--fixture", default="")
    p_ce.add_argument("--build", action="store_true")
    p_ce.add_argument("--congress", default="118")
    p_ce.add_argument("--n-pos", dest="n_pos", default="160")
    p_ce.add_argument("--n-neg", dest="n_neg", default="280")
    p_ce.add_argument("--labels", default="")
    p_ce.add_argument("--judge-votes", dest="judge_votes", default="5")
    p_ce.add_argument("--workers", default="6")
    p_ce.add_argument("--limit", default="0")
    p_cl = sub.add_parser("catalyst-label")
    p_cl.add_argument("--labels", default="")
    p_cl.add_argument("--limit", default="20")
    p_cs = sub.add_parser("catalyst-seed")
    p_cs.add_argument("--labels", default="")
    p_cs.add_argument("--n", default="15")
    p_cs.add_argument("--max-analyze", dest="max_analyze", default="40")
    p_cs.add_argument("--lookback-days", dest="lookback_days", default="240")
    p_cs.add_argument("--deadline-sec", dest="deadline_sec", default="540")
    p_ua = sub.add_parser("universe-apply")
    p_ua.add_argument("--id", required=True)
    p_ua.add_argument("--approve", action="store_true")
    sub.add_parser("event-risk")
    sub.add_parser("prune")
    sub.add_parser("auth-stop")
    p_ms = sub.add_parser("memory-show")
    p_ms.add_argument("--ticker", default="")
    sub.add_parser("memory-rebuild")
    p_dp = sub.add_parser("decision-proof")
    p_dp.add_argument("--id", required=True)
    p_at = sub.add_parser("attribution")
    p_at.add_argument("--date", default="")
    p_at.add_argument("--benchmark", default="SPY")
    p_ff = sub.add_parser("factors-fetch")
    p_ff.add_argument("--date", default="")
    p_ff.add_argument("--tickers", default="")
    p_ff.add_argument("--period", default="2y")
    p_bf = sub.add_parser("benchmark-fetch")
    p_bf.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    p_bf.add_argument("--date", default="")
    p_bf.add_argument("--period", default="2y")
    p_bb = sub.add_parser("benchmark-backfill")
    # DRY-RUN by default: this rewrites a learning input that reaches order dollars.
    p_bb.add_argument("--apply", action="store_true")
    p_bb.add_argument("--fill-only", dest="fill_only", action="store_true")
    p_bb.add_argument("--max-rows", dest="max_rows", default="0")
    p_sh = sub.add_parser("strategy-history")
    p_sh.add_argument("--limit", default="50")
    p_sh.add_argument("--type", default="")
    p_ir = sub.add_parser("intel-refresh")
    p_ir.add_argument("--input", default="")
    p_ii = sub.add_parser("intel-ingest")
    p_ii.add_argument("--input", required=True)
    p_ipl = sub.add_parser("intel-power-load")
    p_ipl.add_argument("--input", required=True)
    sub.add_parser("intel-profile")
    p_ipr = sub.add_parser("intel-propose")
    p_ipr.add_argument("--record", action="store_true")
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
        elif args.cmd == "report-send":
            out = cmd_report_send(args)
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
        elif args.cmd == "flow-record":
            out = cmd_flow_record(args)
        elif args.cmd == "flow-suggest":
            out = cmd_flow_suggest(args)
        elif args.cmd == "learn-review":
            out = cmd_learn_review(args)
        elif args.cmd == "bills-review":
            out = cmd_bills_review(args)
        elif args.cmd == "catalyst-eval":
            out = cmd_catalyst_eval(args)
        elif args.cmd == "catalyst-label":
            out = cmd_catalyst_label(args)
        elif args.cmd == "catalyst-seed":
            out = cmd_catalyst_seed(args)
        elif args.cmd == "universe-apply":
            out = cmd_universe_apply(args)
        elif args.cmd == "event-risk":
            out = cmd_event_risk(args)
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
        elif args.cmd == "attribution":
            out = cmd_attribution(args)
        elif args.cmd == "benchmark-fetch":
            out = cmd_benchmark_fetch(args)
        elif args.cmd == "benchmark-backfill":
            out = cmd_benchmark_backfill(args)
        elif args.cmd == "factors-fetch":
            out = cmd_factors_fetch(args)
        elif args.cmd == "strategy-history":
            out = cmd_strategy_history(args)
        elif args.cmd == "intel-refresh":
            out = cmd_intel_refresh(args)
        elif args.cmd == "intel-ingest":
            out = cmd_intel_ingest(args)
        elif args.cmd == "intel-power-load":
            out = cmd_intel_power_load(args)
        elif args.cmd == "intel-profile":
            out = cmd_intel_profile(args)
        elif args.cmd == "intel-propose":
            out = cmd_intel_propose(args)
        else:  # unreachable
            raise SystemExit(2)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e), "cmd": args.cmd}))
        return 1

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
