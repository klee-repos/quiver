#!/usr/bin/env python3
"""Deterministic e2e for the sub-$1 fractional-sell guard + the LLM-sell/rebalance
unification (no network, no broker, no LLM).

  * Fix 1 + F2 (min-notional): a TRIM below min_trade_notional ($5) is SKIPPED
        (churn guard), not bumped; a full EXIT/reconcile stays on RH's $1 floor and
        still winds to zero (exempt); dust (whole pos < $1) is a clean skip. Both
        sell paths (classic LLM exit + rebalance trim/exit) honor it; never an error.
  * Fix 2 (unify): with rebalance ON, an in-book LLM Underweight/Sell is DEFERRED
        to the rebalance trim/exit pass (deferred_to_rebalance_trim) — the book owns
        trim sizing (target weight), not the model's blunt 0.5-half. Symmetric with
        deferred_to_rebalance_buy. Byte-identical to classic when rebalance is OFF.
  * Gate still holds: an ungrounded Underweight reversal is suppressed by the
        consistency gate; the unify must NOT let the rebalance pass re-exit it.

Plain asserts (no pytest); exits non-zero on any failure.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("RH_ACCOUNT_NUMBER", "TEST1234")

PASS = 0
FAIL = 0


def ok(name, cond, detail: object = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}{(' — ' + str(detail)) if detail else ''}")


def _mk(tmp: Path, *, rebalance=True, holdings=None, per_name=80, rh="[AAA, BBB, SGOV]",
        min_delta=None):
    """Write a strategy + config and return (Config, freshly-seeded Ledger)."""
    from lib.config import load_config
    from lib.strategy import load_strategy
    from lib.ledger import Ledger
    holdings = holdings or [
        "      - {sleeve: Tech, ticker: AAA, weight: 30, band: 5}",
        "      - {sleeve: Fin, ticker: BBB, weight: 30, band: 5}",
        "      - {sleeve: Cash, ticker: SGOV, weight: 40}"]
    strat = tmp / "strategy.yaml"
    strat.write_text(
        "schema: 1\n"
        "goal: {target_return_pct: 15, horizon_months: 12, benchmark: SGOV, benchmark_annual_pct: 3.6}\n"
        "macro_thesis: {deploy_trigger_pce_pct: 2.5, standdown_trigger_pce_pct: 3.5}\n"
        f"rh_tradable_confirmed: {rh}\n"
        f"risk_policy: {{per_name_max_pct: {per_name}, sleeve_max_pct: 100, "
        "cash_floor_pct: 10, smoothing_alpha: 1.0}\n"
        "learning: {min_resolved_n: 5}\n"
        "books:\n  core:\n    default: true\n    holdings:\n" + "\n".join(holdings) + "\n",
        encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "schema: 1\ndry_run: true\nkill_switch_file: KILL\naccount_number: TEST1234\n"
        f"strategy_path: {strat}\n"
        "glm: {chat_model: glm-5.2, reasoner_model: glm-5.2}\n"
        f"risk: {{max_dollars_per_trade: 100, daily_loss_halt_pct: 20, "
        "daily_capital_deploy_cap: 1000, min_buying_power_buffer: 5, "
        "max_actions_per_ticker_per_day: 1, max_analyses_per_ticker_per_day: 1, "
        f"rebalance_enabled: {str(rebalance).lower()}, cash_sleeve_ticker: SGOV"
        + (f", conviction_rebalance_min_delta_pct: {min_delta}" if min_delta is not None else "")
        + "}\n"
        "notify: {enabled: false}\n",
        encoding="utf-8")
    cfg = load_config(str(cfg_path))
    sc = load_strategy(str(strat))
    led = Ledger(tmp / f"l_{os.urandom(4).hex()}.db")
    holds = [{"sleeve": h.sleeve, "ticker": h.ticker, "target_weight": h.weight, "band": h.band,
              "status": "active", "quotable": h.quotable, "book": "core", "updated_at": "t"}
             for h in sc.book("core").holdings]
    led.set_strategy_goal_with_holdings(
        goal={"created_at": "t", "target_return_pct": 15, "horizon_months": 12, "benchmark": "SGOV",
              "benchmark_annual_pct": 3.6, "constraint_note": "", "macro_thesis_version": "v",
              "macro_thesis_json": "{}", "active_book": "core", "as_of": "x",
              "start_date": "2026-01-01", "start_equity": 1000.0},
        holdings=holds)
    return cfg, led


def main() -> int:
    import tick
    print("=" * 64)
    print("E2E — sub-$1 sell guard + LLM-sell/rebalance unification")
    print("=" * 64)

    # --- Fix 2: in-book LLM Underweight is DEFERRED to the rebalance trim pass ---
    # The model says Underweight on AAA (held). With rebalance ON, the LLM's 0.5-half
    # trim must NOT place an `exit` order; instead a deferred_to_rebalance_trim decision
    # + a rebalance_trim order sized to the book's target_dollars. The model becomes a
    # SIGNAL to the allocator, not a direct seller (symmetric with the buy side).
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        # AAA held overweight vs its 30% target on a $100 account -> construct trims.
        # Held 2.0 sh @ $60 = $120; target 30% of $100 = $30 -> trim = (120-30)/60 = 1.5 sh.
        # (1.5 sh differs from the model's 0.5*held = 1.0 sh, so the book sizing is distinct.)
        snap = {"equity": 100.0, "buying_power": 40.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 2.0, "market_value": 120.0}},
                "quotes": {"AAA": 60.0, "BBB": 30.0, "SGOV": 1.0}}
        con = tick._run_construct(cfg, led, {k: snap[k] for k in
                                              ("equity", "buying_power", "positions", "quotes")})
        tw = con["target_weights"]
        ok("AAA in book + held overweight -> construct intent=trim",
           tw.get("AAA", {}).get("intent") == "trim", tw.get("AAA"))
        an = [{"ticker": "AAA", "signal": "Underweight", "conviction": 68, "uncertainty": 30}]
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": an, "target_weights": tw})
        aaa_exit = next((o for o in plan["orders"] if o["ticker"] == "AAA"
                         and o.get("order_kind") == "exit"), None)
        aaa_rtrim = next((o for o in plan["orders"] if o["ticker"] == "AAA"
                          and o.get("order_kind") == "rebalance_trim"), None)
        aaa_dec = next((dd for dd in plan["decisions"] if dd.get("ticker") == "AAA"), None)
        ok("unify: NO LLM `exit` order on the in-book Underweight", aaa_exit is None,
           [o for o in plan["orders"] if o["ticker"] == "AAA"])
        ok("unify: rebalance_trim order IS placed (book owns the trim)", aaa_rtrim is not None,
           [o for o in plan["orders"] if o["ticker"] == "AAA"])
        ok("unify: decision recorded as deferred_to_rebalance_trim",
           aaa_dec.get("detail") == "deferred_to_rebalance_trim", aaa_dec)
        # The rebalance trim is sized to the book's target (toward target_dollars), NOT
        # the model's blunt 0.5*held (1.0 sh). Compute the expected book trim from the
        # target construct actually emitted (total equity = positions_mv + buying_power).
        aaa_target = tw["AAA"]["target_dollars"]
        expected_trim = round(max(0.0, (120.0 - aaa_target) / 60.0), 6)
        ok("unify: trim sized toward target_dollars (NOT 0.5*held)",
           aaa_rtrim is not None and aaa_rtrim["quantity"] == expected_trim
           and expected_trim != 0.5 * 2.0, (aaa_rtrim, expected_trim))

    # --- F2: a rebalance trim >= min_trade_notional ($5) passes; a sub-$5 trim SKIPS (churn) --
    # held 0.05 sh @ $140 = $7.00; target $1.00 -> raw trim = ($7-$1)/$140 = 0.042857 sh = $6.00
    # (>= $5 -> places at the raw excess shares, NOT bumped).
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        snap = {"equity": 1000.0, "buying_power": 500.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.05, "market_value": 7.00}},
                "quotes": {"AAA": 140.0, "BBB": 30.0, "SGOV": 1.0}}
        tw = {"AAA": {"intent": "trim", "target_dollars": 1.00, "quotable": True}}
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": [], "target_weights": tw})
        aaa = next((o for o in plan["orders"] if o["ticker"] == "AAA"), None)
        ok("F2: rebalance trim >= $5 places (raw excess shares, not bumped)",
           aaa is not None and aaa.get("order_kind") == "rebalance_trim"
           and abs(aaa["quantity"] - round((7.00 - 1.00) / 140.0, 6)) < 1e-9, aaa)

    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        # held 0.05 sh @ $140 = $7.00; target $3.50 -> raw trim = ($7-$3.50)/$140 = 0.025 sh = $3.50
        # (< $5, whole pos NOT dust) -> SKIPPED (F2 churn guard): don't bump a sub-floor trim up.
        snap = {"equity": 1000.0, "buying_power": 500.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.05, "market_value": 7.00}},
                "quotes": {"AAA": 140.0, "BBB": 30.0, "SGOV": 1.0}}
        tw = {"AAA": {"intent": "trim", "target_dollars": 3.50, "quotable": True}}
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": [], "target_weights": tw})
        aaa = next((o for o in plan["orders"] if o["ticker"] == "AAA"), None)
        skip_dec = next((dd for dd in plan["decisions"] if dd.get("ticker") == "AAA"
                        and dd.get("detail", "").startswith("sell_below_min")), None)
        ok("F2: sub-$5 rebalance trim SKIPS (not bumped, no order)", aaa is None, aaa)
        ok("F2: sub-$5 trim records sell_below_min:skip_below_min",
           skip_dec is not None and "skip_below_min" in skip_dec.get("detail", ""), skip_dec)

    # --- F2 P1 (CRITICAL, review survivor): a full EXIT of a $1-$5 position STILL winds to zero.
    # The $5 economic floor must NOT reach the exit path, or resolve_sell_quantity_min_notional's
    # skip_dust branch (held*quote < min_notional, evaluated BEFORE the bump) would strand it.
    # Exits stay on RH's $1 floor. held 0.02 sh @ $140 = $2.80 -> full rebalance_exit sells all 0.02.
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        snap = {"equity": 1000.0, "buying_power": 500.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.02, "market_value": 2.80}},
                "quotes": {"AAA": 140.0, "BBB": 30.0, "SGOV": 1.0}}
        tw = {"AAA": {"intent": "exit", "target_dollars": 0.0, "quotable": True}}
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": [], "target_weights": tw})
        aaa = next((o for o in plan["orders"] if o["ticker"] == "AAA"), None)
        ok("F2 P1: $1-$5 full EXIT still winds to zero (exit exempt from the $5 floor)",
           aaa is not None and aaa.get("order_kind") == "rebalance_exit"
           and abs(aaa["quantity"] - 0.02) < 1e-9, aaa)

    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        # DUST: held 0.004 sh @ $140 = $0.56 — whole position under $1. Trim toward $0 -> raw
        # trim = full $0.56 < $1, and whole pos < $1 -> skip_dust (clean skip, no error).
        snap = {"equity": 1000.0, "buying_power": 500.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.004, "market_value": 0.56}},
                "quotes": {"AAA": 140.0, "BBB": 30.0, "SGOV": 1.0}}
        tw = {"AAA": {"intent": "exit", "target_dollars": 0.0, "quotable": True}}
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": [], "target_weights": tw})
        aaa_ord = next((o for o in plan["orders"] if o["ticker"] == "AAA"), None)
        skip_dec = next((dd for dd in plan["decisions"] if dd.get("ticker") == "AAA"
                        and dd.get("detail", "").startswith("sell_below_min")), None)
        ok("dust (< $1 whole pos) -> NO order placed", aaa_ord is None, aaa_ord)
        ok("dust -> clean sell_below_min:skip_dust skip (NOT an error)",
           skip_dec is not None and "skip_dust" in skip_dec.get("detail", ""), skip_dec)

    # --- F2 on the CLASSIC LLM sell path (rebalance OFF): sub-$5 Underweight trim SKIPS ---------
    # rebalance OFF -> target_weights is None -> the LLM Underweight 0.5-trim runs. A sub-$5 trim
    # is churn -> skipped; a trim >= $5 places; a full Sell (wind-down) always completes.
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), rebalance=False)
        # Held 0.02 sh @ $140 = $2.80; LLM Underweight -> 0.5-trim = 0.01 sh = $1.40 (< $5 -> SKIP).
        snap = {"equity": 1000.0, "buying_power": 500.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.02, "market_value": 2.80}},
                "quotes": {"AAA": 140.0, "BBB": 30.0, "SGOV": 1.0}}
        an = [{"ticker": "AAA", "signal": "Underweight", "conviction": 60, "uncertainty": 30}]
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": an})  # no target_weights -> classic
        aaa = next((o for o in plan["orders"] if o["ticker"] == "AAA"), None)
        skip_dec = next((dd for dd in plan["decisions"] if dd.get("ticker") == "AAA"
                        and dd.get("detail", "").startswith("sell_below_min")), None)
        ok("F2 classic: sub-$5 Underweight trim SKIPS (churn guard)", aaa is None, aaa)
        ok("F2 classic: sub-$5 trim records sell_below_min", skip_dec is not None, skip_dec)

    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), rebalance=False)
        # Held 0.20 sh @ $140 = $28.00; Underweight 0.5-trim = 0.10 sh = $14.00 (>= $5 -> places).
        snap = {"equity": 1000.0, "buying_power": 500.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.20, "market_value": 28.00}},
                "quotes": {"AAA": 140.0, "BBB": 30.0, "SGOV": 1.0}}
        an = [{"ticker": "AAA", "signal": "Underweight", "conviction": 60, "uncertainty": 30}]
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": an})
        aaa = next((o for o in plan["orders"] if o["ticker"] == "AAA"), None)
        ok("F2 classic: Underweight trim >= $5 still places an exit order",
           aaa is not None and aaa.get("order_kind") == "exit", aaa)

    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), rebalance=False)
        # Held 0.004 sh @ $140 = $0.56 (dust); 0.5-trim = 0.002 sh = $0.28 < $1, whole pos <$1 -> skip_dust.
        snap = {"equity": 1000.0, "buying_power": 500.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.004, "market_value": 0.56}},
                "quotes": {"AAA": 140.0, "BBB": 30.0, "SGOV": 1.0}}
        an = [{"ticker": "AAA", "signal": "Underweight", "conviction": 60, "uncertainty": 30}]
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": an})
        aaa_ord = next((o for o in plan["orders"] if o["ticker"] == "AAA"), None)
        skip_dec = next((dd for dd in plan["decisions"] if dd.get("ticker") == "AAA"
                         and dd.get("detail", "").startswith("sell_below_min")), None)
        ok("classic dust -> NO order", aaa_ord is None, aaa_ord)
        ok("classic dust -> clean sell_below_min:skip_dust skip",
           skip_dec is not None and "skip_dust" in skip_dec.get("detail", ""), skip_dec)

    # --- Byte-identical classic: rebalance OFF -> no deferral, exit order placed --
    # The deferral guard is `if target_weights and ticker in target_weights`; with
    # rebalance OFF target_weights is None, so a Sell/Underweight places the LLM exit
    # exactly as before (the only change to that path is the min-notional bump, which
    # is a no-op when the trim already clears $1).
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), rebalance=False)
        snap = {"equity": 1000.0, "buying_power": 500.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 10.0, "market_value": 1400.0}},
                "quotes": {"AAA": 140.0, "BBB": 30.0, "SGOV": 1.0}}
        an = [{"ticker": "AAA", "signal": "Sell", "conviction": 70, "uncertainty": 20}]
        plan = tick._run_plan(cfg, led, {**snap, "now_iso": snap["now_iso"], "run_id": "E2E",
                                         "analyses": an})
        aaa = next((o for o in plan["orders"] if o["ticker"] == "AAA"), None)
        ok("classic Sell (rebalance OFF) -> LLM exit order placed (no deferral)",
           aaa is not None and aaa.get("order_kind") == "exit", aaa)

    # --- Gate still holds: an ungrounded Underweight reversal is NOT re-exited ---
    # The consistency gate suppresses an ungrounded reversal (detail consistency:...).
    # The unify must NOT let the rebalance pass re-trim/exit it: construct may have
    # lowered the target (Underweight -> trim) and that target would otherwise re-trim
    # a name the gate just told to hold — silently overriding the gate. The rebalance
    # SELL pass excludes the `suppressed` set (names with a consistency:* decision).
    # Seed a prior completed BUY so today's Underweight is a real cross-day reversal
    # with no plan_trigger and no fresh basis -> ungrounded -> suppressed.
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        # Seed a prior-day completed buy (an actions event the gate reads as the stance).
        led.record_event(trade_date="2026-06-19", ticker="AAA", ts="2026-06-19T10:00:00-04:00",
                         signal="Buy", intent="buy", status="dry_run",
                         detail="seed buy", run_id="seed")
        # Also record the decision so the gate can join it for a plan_trigger check.
        led.record_decision(trade_date="2026-06-19", ticker="AAA",
                            decided_at="2026-06-19T10:00:00-04:00", signal="Buy", intent="buy",
                            decision_price=100.0, stop_loss=90.0, target_price=110.0,
                            rationale="seed", run_id="seed")
        # Today: AAA Underweight, quote ABOVE stop (no plan trigger), no catalyst basis.
        snap2 = {"equity": 1000.0, "buying_power": 900.0, "now_iso": "2026-06-20T10:00:00-04:00",
                 "positions": {"AAA": {"quantity": 0.5, "market_value": 50.0}},
                 "quotes": {"AAA": 100.0, "BBB": 30.0, "SGOV": 1.0}}
        an2 = [{"ticker": "AAA", "signal": "Underweight", "conviction": 60, "uncertainty": 30}]
        tw2 = {"AAA": {"intent": "trim", "target_dollars": 30.0, "quotable": True}}
        p2 = tick._run_plan(cfg, led, {**snap2, "analyses": an2, "now_iso": snap2["now_iso"],
                                       "run_id": "r2", "target_weights": tw2})
        aaa_ord = next((o for o in p2["orders"] if o["ticker"] == "AAA"), None)
        aaa_dec = next((dd for dd in p2["decisions"] if dd.get("ticker") == "AAA"), None)
        ok("gate: ungrounded Underweight reversal -> NO order (gate held)",
           aaa_ord is None, aaa_ord)
        ok("gate: decision recorded as consistency:... skip",
           str(aaa_dec.get("detail", "")).startswith("consistency:"), aaa_dec)
        ok("gate: the rebalance pass did NOT re-exit the suppressed name",
           not any(o["ticker"] == "AAA" and o.get("order_kind", "").startswith("rebalance")
                   for o in p2["orders"]),
           [o for o in p2["orders"] if o["ticker"] == "AAA"])

    # --- F3: conviction HYSTERESIS pins a noisy tick to the static book (no churn) + conserves ---
    # A big min_delta gate holds every name at its STATIC weight regardless of the day's conviction,
    # so a name held AT target does not get re-clipped and trimmed. Proves _run_construct actually
    # calls apply_target_hysteresis AND conserves the book to ~100%.
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), min_delta=50.0)   # huge gate -> pin all to static
        # AAA + BBB held EXACTLY at their static 30% targets ($30 each on a $100 deployable).
        snap = {"equity": 100.0, "buying_power": 40.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.5, "market_value": 30.0},
                              "BBB": {"quantity": 1.0, "market_value": 30.0}},
                "quotes": {"AAA": 60.0, "BBB": 30.0, "SGOV": 1.0}}
        an = [{"ticker": "AAA", "signal": "Underweight", "conviction": 68, "uncertainty": 30}]
        con = tick._run_construct(cfg, led, {**{k: snap[k] for k in
                                  ("equity", "buying_power", "positions", "quotes")}, "analyses": an})
        tw = con["target_weights"]
        ok("F3: hysteresis pins AAA target to its static 30% (noise gated, not re-clipped)",
           abs(tw["AAA"]["target_weight"] - 30.0) < 1e-6, tw.get("AAA"))
        ok("F3: book still conserves to ~100% after hysteresis + cash residual",
           abs(sum(v["target_weight"] for v in tw.values()) - 100.0) < 0.5,
           {k: v["target_weight"] for k, v in tw.items()})
        # held == target -> construct intent is hold -> the plan places NO trim (the churn we kill).
        plan = tick._run_plan(cfg, led, {**snap, "run_id": "E2E", "analyses": an, "target_weights": tw})
        aaa_reb = next((o for o in plan["orders"] if o["ticker"] == "AAA"
                        and str(o.get("order_kind", "")).startswith("rebalance")), None)
        ok("F3: no rebalance trim/buy when hysteresis holds the target (no churn)", aaa_reb is None, aaa_reb)

    # --- F3 knob OFF (min_delta 0): the target re-clips to conviction (byte-identical to pre-F3) ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), min_delta=0.0)
        snap = {"equity": 100.0, "buying_power": 40.0, "now_iso": "2026-06-20T10:00:00-04:00",
                "positions": {"AAA": {"quantity": 0.5, "market_value": 30.0},
                              "BBB": {"quantity": 1.0, "market_value": 30.0}},
                "quotes": {"AAA": 60.0, "BBB": 30.0, "SGOV": 1.0}}
        an = [{"ticker": "AAA", "signal": "Underweight", "conviction": 68, "uncertainty": 30}]
        con = tick._run_construct(cfg, led, {**{k: snap[k] for k in
                                  ("equity", "buying_power", "positions", "quotes")}, "analyses": an})
        ok("F3 OFF: min_delta 0 lets the target move off static (re-clip, pre-F3 behavior)",
           abs(con["target_weights"]["AAA"]["target_weight"] - 30.0) > 1e-6,
           con["target_weights"].get("AAA"))

    print("\n" + "=" * 64)
    print(f"E2E: {PASS} checks passed, {FAIL} failed")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
