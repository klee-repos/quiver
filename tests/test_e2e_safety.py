#!/usr/bin/env python3
"""Deterministic e2e for the SAFETY / robustness + learning-truth tail of the
analysis-driven-weights work (no network, no broker, no LLM):

  * T7  daily-loss halt measures TOTAL equity (positions + cash), so a cash<->positions
        reallocation at constant value does NOT trip it, but a real total drop does.
  * T7  the consistency gate FAILS CLOSED on a read error for a SELL/close (the ungrounded-
        reversal direction) and stays open for a BUY (never wedges deployment).
  * D3  a conviction-driven EXIT (Sell) that reverses a held name with no catalyst is pinned
        at its prior weight by construct (hold_floor), not cut to 0.
  * T9  buy-to-target: a name deploys to its full target room in ONE order (no per-trade dollar
        cap, so no tranching), still bounded by available cash (buying_power - buffer).
  * T9  run_lock acquire is atomic + steals a stale lock.
  * T2  fail-SAFE data: a missing/errored core (market) report downgrades the signal to ERROR,
        and plan records that ERROR as a skip (no order).
  * T6  reflect populates ALPHA (excess vs benchmark) + realized P&L, and the scorecard then
        grades on skill, not market beta.

Plain asserts (no pytest); exits non-zero on any failure.
"""

import json
import os
import sys
import tempfile
import types
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


def _mk(tmp: Path, *, daily_loss=20, holdings=None, rh="[AAA, BBB, SGOV]", per_name_max_pct=80):
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
        f"risk_policy: {{per_name_max_pct: {per_name_max_pct}, sleeve_max_pct: 100, cash_floor_pct: 10, smoothing_alpha: 1.0}}\n"
        "learning: {min_resolved_n: 5}\n"
        "books:\n  core:\n    default: true\n    holdings:\n" + "\n".join(holdings) + "\n",
        encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "schema: 1\ndry_run: true\nkill_switch_file: KILL\naccount_number: TEST1234\n"
        f"strategy_path: {strat}\n"
        "glm: {chat_model: glm-5.2, reasoner_model: glm-5.2}\n"
        f"risk: {{max_dollars_per_trade: 100, daily_loss_halt_pct: {daily_loss}, "
        "daily_capital_deploy_cap: 1000, min_buying_power_buffer: 5, "
        "max_actions_per_ticker_per_day: 1, max_analyses_per_ticker_per_day: 1, "
        "rebalance_enabled: true, cash_sleeve_ticker: SGOV}\nnotify: {enabled: false}\n",
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
    from lib import memory
    print("=" * 64)
    print("E2E — safety / robustness + learning-truth")
    print("=" * 64)

    # --- T7: daily-loss base = TOTAL equity (positions + cash) ----------------
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), daily_loss=20)
        # tick 1 sets the baseline at TOTAL = 500 positions + 500 cash = 1000.
        d1 = {"now_iso": "2026-06-20T10:00:00-04:00", "equity": 500.0, "buying_power": 500.0,
              "positions": {"AAA": {"market_value": 500.0, "quantity": 5.0}}, "analyses": []}
        tick._run_plan(cfg, led, dict(d1))
        # tick 2: a pure cash<->positions reallocation at constant TOTAL (900 + 100 = 1000).
        d2 = {"now_iso": "2026-06-20T11:00:00-04:00", "equity": 900.0, "buying_power": 100.0,
              "positions": {"AAA": {"market_value": 900.0, "quantity": 9.0}}, "analyses": []}
        out2 = tick._run_plan(cfg, led, dict(d2))
        ok("T7 cash<->positions realloc at constant total -> ~0 drop, NO halt",
           out2["halt"] is False and abs(out2["drop_pct"]) < 0.1, out2.get("drop_pct"))
        # tick 3: a REAL total-value drop (positions 600 + cash 100 = 700, -30% vs 1000) -> halt.
        d3 = {"now_iso": "2026-06-20T12:00:00-04:00", "equity": 600.0, "buying_power": 100.0,
              "positions": {"AAA": {"market_value": 600.0, "quantity": 6.0}}, "analyses": []}
        out3 = tick._run_plan(cfg, led, dict(d3))
        ok("T7 a real total-equity drop (-30%) DOES halt", out3["halt"] is True, out3.get("drop_pct"))

    # --- T7: consistency gate fails CLOSED on a read error (sell), open (buy) --
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))

        def _boom(*a, **k):
            raise RuntimeError("simulated ledger read error")
        led.last_completed_trade = _boom  # type: ignore[assignment]
        allowed_sell, reason_sell, _t, _p = tick._consistency_context(
            cfg, led, "AAA", "sell", 100.0, "thesis", None, None)
        allowed_buy, _r2, _t2, _p2 = tick._consistency_context(
            cfg, led, "AAA", "buy", 100.0, "thesis", None, None)
        ok("T7 gate fails CLOSED on a SELL read-error (suppressed reversal)",
           allowed_sell is False and reason_sell == "gate_error_suppressed_reversal", reason_sell)
        ok("T7 gate stays OPEN on a BUY read-error (never wedges deployment)", allowed_buy is True)

    # --- D3: conviction-driven EXIT reversing a held name w/o catalyst -> hold prior ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        gid = led.get_active_goal()["id"]
        # seed a PRIOR completed BUY on AAA (a committed accumulation stance)
        led.record_decision(trade_date="2026-06-18", ticker="AAA",
                            decided_at="2026-06-18T10:00:00-04:00", signal="Buy", intent="buy",
                            decision_price=100.0, basis="ai_secular")
        led.record_event(trade_date="2026-06-18", ticker="AAA", ts="2026-06-18T10:00:01-04:00",
                         signal="Buy", intent="buy", status="dry_run")
        # day 2: an ungrounded Sell (no new catalyst, no stop/target on the prior decision)
        an = [{"ticker": "AAA", "signal": "Sell", "conviction": 80, "uncertainty": 10},
              {"ticker": "BBB", "signal": "Buy", "conviction": 70, "uncertainty": 10}]
        out = tick._run_construct(cfg, led, {
            "now_iso": "2026-06-19T11:00:00-04:00", "equity": 1000.0, "buying_power": 1000.0,
            "positions": {"AAA": {"market_value": 300.0, "quantity": 3.0}},
            "quotes": {"AAA": 100.0, "BBB": 50.0}, "analyses": an})
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("D3 ungrounded Sell reversal HOLDS prior weight (not cut to 0)", w.get("AAA", 0) >= 29.0, w)

        # control: the SAME Sell WITH a named new catalyst is allowed to reduce (grounded reversal)
        cfg2, led2 = _mk(Path(d) / "x" if False else Path(tempfile.mkdtemp()))
        led2.record_decision(trade_date="2026-06-18", ticker="AAA",
                             decided_at="2026-06-18T10:00:00-04:00", signal="Buy", intent="buy",
                             decision_price=100.0, basis="ai_secular")
        led2.record_event(trade_date="2026-06-18", ticker="AAA", ts="2026-06-18T10:00:01-04:00",
                          signal="Buy", intent="buy", status="dry_run")
        an2 = [{"ticker": "AAA", "signal": "Sell", "conviction": 80, "uncertainty": 10,
                "basis": "thesis_broken_earnings_miss"},
               {"ticker": "BBB", "signal": "Buy", "conviction": 70, "uncertainty": 10}]
        out_c = tick._run_construct(cfg2, led2, {
            "now_iso": "2026-06-19T11:00:00-04:00", "equity": 1000.0, "buying_power": 1000.0,
            "positions": {"AAA": {"market_value": 300.0, "quantity": 3.0}},
            "quotes": {"AAA": 100.0, "BBB": 50.0}, "analyses": an2})
        wc = {t: out_c["target_weights"][t]["target_weight"] for t in out_c["target_weights"]}
        ok("D3 a GROUNDED Sell (named catalyst) IS allowed to exit (~0)", wc.get("AAA", 0) < 5.0, wc)

    # --- FIX(HIGH): rebalance buy-to-target must NOT re-buy a consistency-SUPPRESSED name ---
    # The consistency gate suppresses an ungrounded cross-day buy reversal (records a
    # "consistency:" skip, NO order). The deterministic buy-to-target deploy must respect that
    # — re-buying the same name the same tick would silently override the gate (which by
    # invariant can only ever SUPPRESS a trade, never add one).
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        # a prior COMPLETED sell on AAA -> today's Buy is a cross-day reversal; a buy-after-sell
        # has NO Python plan_trigger, and with no NEW catalyst it is an ungrounded reversal.
        led.record_decision(trade_date="2026-06-18", ticker="AAA",
                            decided_at="2026-06-18T10:00:00-04:00", signal="Sell", intent="sell",
                            decision_price=100.0, basis="took_profit")
        led.record_event(trade_date="2026-06-18", ticker="AAA", ts="2026-06-18T10:00:01-04:00",
                         signal="Sell", intent="sell", status="dry_run")
        out = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-19T11:00:00-04:00", "equity": 1000.0, "buying_power": 1000.0,
            "positions": {}, "quotes": {"AAA": 50.0},
            "analyses": [{"ticker": "AAA", "signal": "Buy", "position_pct": 20.0}],
            "target_weights": {"AAA": {"intent": "buy", "target_dollars": 200.0, "quotable": True}}})
        _sup = [dd for dd in out["decisions"] if dd.get("ticker") == "AAA"
                and str(dd.get("detail") or "").startswith("consistency:")]
        ok("HIGH-fix: ungrounded buy reversal IS consistency-suppressed", len(_sup) == 1, out["decisions"])
        ok("HIGH-fix: rebalance buy-to-target does NOT re-buy the suppressed name",
           [o for o in out["orders"] if o["ticker"] == "AAA"] == [], out["orders"])
        # CONTROL: a non-suppressed underweight book name IS still deployed (fix is scoped).
        out2 = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-19T11:00:00-04:00", "equity": 1000.0, "buying_power": 1000.0,
            "positions": {}, "quotes": {"BBB": 50.0},
            "analyses": [{"ticker": "BBB", "signal": "Hold", "position_pct": 0.0}],
            "target_weights": {"BBB": {"intent": "buy", "target_dollars": 200.0, "quotable": True}}})
        ok("HIGH-fix: a non-suppressed held book name is STILL deployed (scoped to suppression)",
           any(o["ticker"] == "BBB" and o["side"] == "buy" for o in out2["orders"]), out2["orders"])

    # --- FIX(MED): conviction must tighten the rebalance band to bind ----------------
    # When conviction sizes a name DOWN, its rebalance dead-band must shrink to stay inside
    # the new weight; otherwise the name sits inside a now-too-wide static band and never
    # rebalances toward its conviction target (conviction sizing wouldn't bind).
    with tempfile.TemporaryDirectory() as d:
        # AAA carries a WIDE static band (12); conviction sizes it to ~13.3%, so the clamp
        # tightens its band to ~6.6% (= 13.3 * 0.5). Held at $250 the drift is ~6.7% — INSIDE
        # the static 12% band (would HOLD without the clamp) but OUTSIDE the tightened band,
        # so it must TRIM. Reverting the clamp flips this back to "hold" and fails the test.
        _wideband = ["      - {sleeve: Tech, ticker: AAA, weight: 30, band: 12}",
                     "      - {sleeve: Fin, ticker: BBB, weight: 30, band: 5}",
                     "      - {sleeve: Cash, ticker: SGOV, weight: 40}"]
        cfg, led = _mk(Path(d), holdings=_wideband)
        out = tick._run_construct(cfg, led, {
            "now_iso": "2026-06-19T11:00:00-04:00", "equity": 1000.0, "buying_power": 1000.0,
            "positions": {"AAA": {"market_value": 250.0, "quantity": 5.0}},
            "quotes": {"AAA": 50.0, "BBB": 50.0},
            "analyses": [{"ticker": "AAA", "signal": "Buy", "conviction": 20, "uncertainty": 40},
                         {"ticker": "BBB", "signal": "Buy", "conviction": 95, "uncertainty": 5}]})
        _aaa = out["target_weights"].get("AAA", {})
        ok("MED-fix: conviction-shrunk name trims (tightened band binds, not stale wide band)",
           _aaa.get("intent") == "trim" and _aaa.get("target_weight", 0) < 20.0, _aaa)

    # --- T9: buy-to-target deploys in ONE order (no per-trade ceiling, no tranching) -----------
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        out = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-20T10:00:00-04:00", "equity": 0.0, "buying_power": 500.0,
            "positions": {}, "quotes": {"AAA": 50.0}, "analyses": [],
            "target_weights": {"AAA": {"intent": "buy", "target_dollars": 350.0, "quotable": True}}})
        aaa = [o for o in out["orders"] if o["ticker"] == "AAA"]
        # No per-trade dollar cap: a name deploys to its full target room in a SINGLE order
        # (the deleted fixed $100 ceiling used to spray this into four $100 tranches).
        ok("T9 buy-to-target is ONE order (no tranche spam)", len(aaa) == 1, len(aaa))
        ok("T9 the order deploys the full room (350)",
           abs(sum(o["dollar_amount"] for o in aaa) - 350.0) < 0.01)
        ok("T9 order kind rebalance_buy", all(o["order_kind"] == "rebalance_buy" for o in aaa))
        # Cash still bounds it: room 350 but avail = buying_power(120) - buffer(5) = 115, so the
        # single order is exactly 115 (as close to target as cash allows), never over-spending cash.
        out2 = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-20T10:00:00-04:00", "equity": 0.0, "buying_power": 120.0,
            "positions": {}, "quotes": {"AAA": 50.0}, "analyses": [],
            "target_weights": {"AAA": {"intent": "buy", "target_dollars": 350.0, "quotable": True}}})
        aaa2 = [o for o in out2["orders"] if o["ticker"] == "AAA"]
        spent = sum(o["dollar_amount"] for o in aaa2)
        ok("T9 buy still bounded by buying_power - buffer (one order = 115)",
           len(aaa2) == 1 and abs(spent - 115.0) < 1e-6, aaa2)

    # --- CAP: no fixed per-trade cap; sizing scales with the live account ------------------------
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))  # per_name_max_pct=80
        # A funded ALL-CASH account: deployable = buying_power = $10,000. positions {} does NOT
        # collapse the base to ~0 (the freshly-funded fix — the daily budget is DEPLOYABLE, not the
        # broker's positions-only equity). A $2,000 target deploys in ONE order — 20x what the
        # deleted fixed $100 cap allowed — with no per-trade ceiling standing in the way.
        out = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-20T10:00:00-04:00", "equity": 0.0, "buying_power": 10000.0,
            "positions": {}, "quotes": {"AAA": 50.0}, "analyses": [],
            "target_weights": {"AAA": {"intent": "buy", "target_dollars": 2000.0, "quotable": True}}})
        aaa = [o for o in out["orders"] if o["ticker"] == "AAA"]
        ok("cap: funded all-cash account DEPLOYS (base is deployable, not $0)",
           sum(o["dollar_amount"] for o in aaa) > 0, aaa)
        ok("cap: $2k target on a $10k account -> ONE order (no per-trade ceiling)",
           len(aaa) == 1 and abs(aaa[0]["dollar_amount"] - 2000.0) < 0.01, aaa)
        # The one remaining size gate: a sub-economic-minimum room ($2 < the $5 min-trade) is a
        # clean SKIP, never an order — the honest below-min behavior, no phantom.
        out_min = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-20T10:00:00-04:00", "equity": 0.0, "buying_power": 10000.0,
            "positions": {}, "quotes": {"AAA": 50.0}, "analyses": [],
            "target_weights": {"AAA": {"intent": "buy", "target_dollars": 2.0, "quotable": True}}})
        aaa_min = [o for o in out_min["orders"] if o["ticker"] == "AAA"]
        decs_min = [x for x in out_min["decisions"] if x.get("ticker") == "AAA"]
        ok("cap: sub-$5 room -> clean SKIP, no order",
           aaa_min == [] and len(decs_min) >= 1 and all(x.get("status") == "skipped" for x in decs_min),
           (aaa_min, decs_min))

    # --- T9: run_lock atomic acquire + stale steal -----------------------------
    with tempfile.TemporaryDirectory() as d:
        from lib.ledger import Ledger
        led = Ledger(Path(d) / "lock.db")
        ok("T9 first acquire succeeds", led.try_acquire_run_lock("A", "2026-06-20T10:00:00") is True)
        ok("T9 second acquire (within ttl) is refused",
           led.try_acquire_run_lock("B", "2026-06-20T10:00:30") is False)
        ok("T9 a STALE lock (past ttl) is stolen",
           led.try_acquire_run_lock("C", "2026-06-20T12:00:00", ttl_seconds=3600) is True)

    # --- T2: fail-SAFE data -> ERROR -> plan records a skip (no order) ---------
    with tempfile.TemporaryDirectory() as d:
        import analyze
        bad = {"market_report": "Error retrieving data for AAA: rate limited",
               "final_trade_decision": "**Rating**: Buy", "trader_investment_plan": "**Action**: Buy"}
        ef = analyze.extract_fields(bad, "Buy", "AAA")
        ok("T2 errored core market report -> signal ERROR", ef["signal"] == "ERROR", ef.get("signal"))
        cfg, led = _mk(Path(d))
        out = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-20T10:00:00-04:00", "equity": 1000.0, "buying_power": 1000.0,
            "positions": {}, "quotes": {}, "analyses": [ef]})
        aaa_orders = [o for o in out["orders"] if o["ticker"] == "AAA"]
        ok("T2 an ERROR analysis places NO order", aaa_orders == [], aaa_orders)

    # --- T6: reflect populates ALPHA (skill) + realized P&L; scorecard uses it --
    with tempfile.TemporaryDirectory() as d:
        from lib.ledger import Ledger
        tdb = Path(d) / "reflect.db"
        led = Ledger(tdb)
        did = led.record_decision(trade_date="2026-06-13", ticker="SMH",
                                  decided_at="2026-06-13T10:00:00-04:00", signal="Buy", intent="buy",
                                  decision_price=100.0, basis="compute")
        # drive the REAL reflect subcommand against this isolated ledger
        tick.LEDGER_DB = tdb
        rin = Path(d) / "reflect_input.json"
        rin.write_text(json.dumps({"resolutions": [
            {"decision_id": did, "price_now": 110.0, "benchmark_return": 0.04, "realized_pnl": 12.5}]}))
        # trust_input_benchmark: this is an IN-PROCESS Python caller supplying its own
        # deterministic benchmark (like lib/wall_replay), which is what the flag exists
        # for. The alpha arithmetic below is still the real thing. The UNtrusted path —
        # the orchestrator's CLI, which cannot set this flag — is asserted right after.
        res = tick.cmd_reflect(types.SimpleNamespace(input=str(rin), trust_input_benchmark=True))
        ok("T6 reflect resolves the outcome", res.get("resolved") == 1, res)
        row = led.decisions_with_outcomes("SMH")[0]
        ok("T6 directional_return computed (decision_price->price_now = +10%)",
           abs((row.get("directional_return") or 0) - 0.10) < 1e-6, row.get("directional_return"))
        ok("T6 ALPHA = excess vs benchmark (0.10 - 0.04 = 0.06)",
           abs((row.get("alpha") or 0) - 0.06) < 1e-6, row.get("alpha"))
        ok("T6 realized P&L leg closed (12.5 written)", abs((row.get("realized_pnl") or 0) - 12.5) < 1e-6,
           row.get("realized_pnl"))
        sc = memory.build_scorecard("SMH", led.decisions_with_outcomes("SMH"))
        ok("T6 scorecard now grades on skill (excess-vs-benchmark)", "excess-vs-benchmark" in sc, sc)
        ok("T6 scorecard surfaces realized P&L", "Realized P&L" in sc and "$+12.50" in sc, sc)

        # T6b — the UNTRUSTED path (what the orchestrator actually runs) must IGNORE a
        # supplied benchmark_return rather than trusting it. Same input, no flag: the
        # market leg must come out NULL, and the ignore must be reported, not silent.
        res2 = tick.cmd_reflect(types.SimpleNamespace(input=str(rin)))
        ok("T6b untrusted reflect still resolves", res2.get("resolved") == 1, res2)
        ok("T6b orchestrator-supplied benchmark_return is IGNORED (alpha NULL)",
           led.decisions_with_outcomes("SMH")[0].get("alpha") is None,
           led.decisions_with_outcomes("SMH")[0].get("alpha"))
        ok("T6b the ignore is reported, not silent",
           res2.get("ignored_benchmark_returns") == 1, res2.get("ignored_benchmark_returns"))
        import sqlite3 as _sq3
        _c = _sq3.connect(str(tdb))
        _br = _c.execute("SELECT benchmark_return FROM outcomes WHERE decision_id=?",
                         (did,)).fetchone()[0]
        _c.close()
        ok("T6b benchmark_return column itself is NULL on the untrusted path", _br is None, _br)

    print(f"\nE2E safety: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
