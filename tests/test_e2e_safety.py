#!/usr/bin/env python3
"""Deterministic e2e for the SAFETY / robustness + learning-truth tail of the
analysis-driven-weights work (no network, no broker, no LLM):

  * T7  daily-loss halt measures TOTAL equity (positions + cash), so a cash<->positions
        reallocation at constant value does NOT trip it, but a real total drop does.
  * T7  the consistency gate FAILS CLOSED on a read error for a SELL/close (the ungrounded-
        reversal direction) and stays open for a BUY (never wedges deployment).
  * D3  a conviction-driven EXIT (Sell) that reverses a held name with no catalyst is pinned
        at its prior weight by construct (hold_floor), not cut to 0.
  * T9  multi-tranche buy-to-target: a name whose room exceeds the per-trade ceiling is filled
        with multiple child orders THIS tick, each <= the ceiling, sum <= available cash.
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


def _mk(tmp: Path, *, daily_loss=20, holdings=None, rh="[AAA, BBB, SGOV]"):
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
        "risk_policy: {per_name_max_pct: 80, sleeve_max_pct: 100, cash_floor_pct: 10, smoothing_alpha: 1.0}\n"
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

    # --- T9: multi-tranche buy-to-target ---------------------------------------
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        out = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-20T10:00:00-04:00", "equity": 0.0, "buying_power": 500.0,
            "positions": {}, "quotes": {"AAA": 50.0}, "analyses": [],
            "target_weights": {"AAA": {"intent": "buy", "target_dollars": 350.0, "quotable": True}}})
        aaa = [o for o in out["orders"] if o["ticker"] == "AAA"]
        ok("T9 multi-tranche emits multiple child orders (350 / 100 ceiling -> 4)", len(aaa) == 4, len(aaa))
        ok("T9 every tranche <= the per-trade ceiling", all(o["dollar_amount"] <= 100.0 for o in aaa), aaa)
        ok("T9 tranche sum == the name's room (350)", abs(sum(o["dollar_amount"] for o in aaa) - 350.0) < 0.01)
        ok("T9 each tranche is a rebalance_buy with its own tranche index",
           all(o["order_kind"] == "rebalance_buy" for o in aaa) and {o["tranche"] for o in aaa} == {1, 2, 3, 4})
        # cash-bound: tranches never exceed buying_power - buffer
        out2 = tick._run_plan(cfg, led, {
            "now_iso": "2026-06-20T10:00:00-04:00", "equity": 0.0, "buying_power": 120.0,
            "positions": {}, "quotes": {"AAA": 50.0}, "analyses": [],
            "target_weights": {"AAA": {"intent": "buy", "target_dollars": 350.0, "quotable": True}}})
        spent = sum(o["dollar_amount"] for o in out2["orders"] if o["ticker"] == "AAA")
        ok("T9 multi-tranche still bounded by buying_power - buffer (<=115)", spent <= 115.0 + 1e-6, spent)

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
        res = tick.cmd_reflect(types.SimpleNamespace(input=str(rin)))
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

    print(f"\nE2E safety: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
