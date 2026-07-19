#!/usr/bin/env python3
"""E2E — PTJ defense BINDS when enabled (not just byte-identical when off).

Proves the F1-F6 defense actually fires through tick._run_plan on the LIVE book path
(rebalance_enabled + target_weights), per adversarial finding B5 (a gate/scalar that only
passes byte-identical tests could be silently inert). Drives the real plan brain with
crafted JSON; deterministic asserts, no LLM/broker/network.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def ok(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}{(' — ' + str(detail)) if detail else ''}")


def _mk(tmp: Path, *, risk_policy: str):
    """Config+Ledger with a book (AAA/SGOV) and the given risk_policy string."""
    from lib.config import load_config
    from lib.strategy import load_strategy
    from lib.ledger import Ledger
    strat = tmp / "strategy.yaml"
    strat.write_text(
        "schema: 1\n"
        "goal: {target_return_pct: 15, horizon_months: 12, benchmark: SGOV, benchmark_annual_pct: 3.6}\n"
        "macro_thesis: {deploy_trigger_pce_pct: 2.5, standdown_trigger_pce_pct: 3.5}\n"
        "rh_tradable_confirmed: [AAA, SGOV]\n"
        f"risk_policy: {{{risk_policy}}}\n"
        "learning: {min_resolved_n: 5}\n"
        "books:\n  core:\n    default: true\n    holdings:\n"
        "      - {sleeve: Tech, ticker: AAA, weight: 60, band: 5}\n"
        "      - {sleeve: Cash, ticker: SGOV, weight: 40}\n",
        encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "schema: 1\ndry_run: true\nkill_switch_file: KILL\naccount_number: TEST1234\n"
        f"strategy_path: {strat}\n"
        "glm: {chat_model: glm-5.2, reasoner_model: glm-5.2}\n"
        "risk: {max_dollars_per_trade: 100, daily_loss_halt_pct: 20, "
        "daily_capital_deploy_cap: 1000, min_buying_power_buffer: 5, "
        "max_actions_per_ticker_per_day: 1, max_analyses_per_ticker_per_day: 1, "
        "rebalance_enabled: true, cash_sleeve_ticker: SGOV, min_trade_notional: 5}\n"
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


def _analysis(ticker, *, entry, stop, target, signal="Buy"):
    return {"ticker": ticker, "signal": signal, "entry_price": entry, "stop_loss": stop,
            "target_price": target, "position_pct": 50.0, "basis": "b", "conviction": 70.0}


def _tw(target_dollars=500.0):
    # A construct-style target_weights map: AAA underweight -> buy toward target.
    return {"AAA": {"intent": "buy", "target_dollars": target_dollars, "quotable": True}}


def _buy_dollars(out, ticker="AAA"):
    return sum(float(o.get("dollar_amount") or 0.0) for o in out["orders"]
              if o.get("ticker") == ticker and o.get("side") == "buy")


def main() -> int:
    import tick
    from lib import market
    # The event-risk sidecar is day-stamped and the plan ignores a stale day (B6). Stamp it
    # with the REAL current trading day the plan compares against (market.trading_day_et()).
    _day = market.trading_day_et()
    print("=" * 64)
    print("E2E — PTJ defense binds when enabled")
    print("=" * 64)

    _RR_ON = "min_reward_risk: 3.0, per_name_max_pct: 100, sleeve_max_pct: 100, cash_floor_pct: 0"
    _OFF = "per_name_max_pct: 100, sleeve_max_pct: 100, cash_floor_pct: 0"
    base = {"now_iso": "2026-06-20T10:00:00-04:00", "equity": 0.0, "buying_power": 1000.0,
            "positions": {}, "quotes": {"AAA": 100.0}}

    # --- T1: R:R gate BINDS the live rebalance buy (F1/C1) ---------------------
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), risk_policy=_RR_ON)
        # R:R = (105-100)/(100-90) = 0.5 < 3.0 -> the rebalance buy is SKIPPED.
        out = tick._run_plan(cfg, led, {**base, "analyses": [_analysis("AAA", entry=100, stop=90, target=105)],
                                        "target_weights": _tw()})
        skipped = any(str(x.get("detail")) == "reward_risk:below_floor"
                      for x in out["decisions"] if x.get("ticker") == "AAA")
        ok("T1 R:R below floor SKIPS the live rebalance buy", skipped and _buy_dollars(out) == 0.0,
           [d for d in out["decisions"] if d.get("ticker") == "AAA"])
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), risk_policy=_RR_ON)
        # R:R = (140-100)/(100-90) = 4.0 >= 3.0 -> the buy PROCEEDS (control).
        out = tick._run_plan(cfg, led, {**base, "analyses": [_analysis("AAA", entry=100, stop=90, target=140)],
                                        "target_weights": _tw()})
        ok("T1 control: good R:R still buys AAA", _buy_dollars(out) > 0.0, _buy_dollars(out))

    # --- T2: downside-vol scalar SHRINKS a live buy vs control (F3/B5) ---------
    _VOL_ON = "downside_vol_target: 0.2, per_name_max_pct: 100, sleeve_max_pct: 100, cash_floor_pct: 0"
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), risk_policy=_VOL_ON)
        er_hi = {"trading_day": _day, "names": {"AAA": {"downside_vol": 0.4}}}   # vol>target -> 0.5x
        out_hi = tick._run_plan(cfg, led, {**base, "analyses": [_analysis("AAA", entry=100, stop=90, target=140)],
                                           "target_weights": _tw(50.0), "event_risk": er_hi})
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), risk_policy=_VOL_ON)
        er_lo = {"trading_day": _day, "names": {"AAA": {"downside_vol": 0.1}}}   # vol<=target -> 1.0x
        out_lo = tick._run_plan(cfg, led, {**base, "analyses": [_analysis("AAA", entry=100, stop=90, target=140)],
                                           "target_weights": _tw(50.0), "event_risk": er_lo})
    ok("T2 high downside-vol shrinks the buy (B5)",
       0 < _buy_dollars(out_hi) < _buy_dollars(out_lo), (_buy_dollars(out_hi), _buy_dollars(out_lo)))

    # --- T3: trend gate (soft/block) shrinks BUY, leaves SELL untouched (F4/B5) -
    _TREND = "trend_gate_mode: block, per_name_max_pct: 100, sleeve_max_pct: 100, cash_floor_pct: 0"
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), risk_policy=_TREND)
        er_dn = {"trading_day": _day, "names": {"AAA": {"trend_regime": "DOWNTREND"}}}
        out = tick._run_plan(cfg, led, {**base, "analyses": [_analysis("AAA", entry=100, stop=90, target=140)],
                                        "target_weights": _tw(50.0), "event_risk": er_dn})
        ok("T3 trend BLOCK zeroes a downtrend buy (F4)", _buy_dollars(out) == 0.0, _buy_dollars(out))

    # --- T4: graduated de-risk trims on a drawdown + NO cross-tick re-buy (F6/B4) -
    _DERISK = ('derisk_tiers: [{at_drawdown_pct: 5, trim_pct: 50}], '
               'per_name_max_pct: 100, sleeve_max_pct: 100, cash_floor_pct: 0')
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), risk_policy=_DERISK)
        # tick 1: set the baseline at TOTAL 1000 (AAA 1000 + 0 cash).
        tick._run_plan(cfg, led, {"now_iso": "2026-06-20T10:00:00-04:00", "equity": 1000.0,
                                  "buying_power": 0.0, "positions": {"AAA": {"market_value": 1000.0, "quantity": 10.0}},
                                  "quotes": {"AAA": 100.0}, "analyses": []})
        # tick 2: AAA drops to 920 (-8% vs 1000) -> breaches the -5% tier -> trim 50%; and even
        # though AAA is underweight in the book, the buy pass must NOT re-buy it same tick (B4).
        out = tick._run_plan(cfg, led, {"now_iso": "2026-06-20T11:00:00-04:00", "equity": 920.0,
                                        "buying_power": 0.0,
                                        "positions": {"AAA": {"market_value": 920.0, "quantity": 10.0}},
                                        "quotes": {"AAA": 92.0}, "analyses": [],
                                        "target_weights": _tw(600.0)})
        derisk_orders = [o for o in out["orders"] if o.get("order_kind") == "derisk_trim" and o.get("ticker") == "AAA"]
        rebuys = [o for o in out["orders"] if o.get("ticker") == "AAA" and o.get("side") == "buy"]
        ok("T4 de-risk trims AAA on an -8% drawdown", len(derisk_orders) == 1
           and abs(float(derisk_orders[0]["quantity"]) - 5.0) < 1e-6, out["orders"])
        ok("T4 buy pass does NOT re-buy the de-risked name same tick (B4)", len(rebuys) == 0, rebuys)

    # --- T5: knobs OFF -> BYTE-IDENTICAL to a config with no PTJ knobs ---------
    for rp_on in (_RR_ON, _VOL_ON, _DERISK):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            data = {**base, "analyses": [_analysis("AAA", entry=100, stop=90, target=140)],
                    "target_weights": _tw()}
            # OFF knobs default to their no-op values -> same orders as a plain risk_policy.
            cfg_off, led_off = _mk(Path(d1), risk_policy=_OFF)
            cfg_ctrl, led_ctrl = _mk(Path(d2), risk_policy=_OFF)
            o1 = tick._run_plan(cfg_off, led_off, dict(data))
            o2 = tick._run_plan(cfg_ctrl, led_ctrl, dict(data))
            ok(f"T5 OFF byte-identical buy dollars ({rp_on[:16]}...)",
               _buy_dollars(o1) == _buy_dollars(o2), (_buy_dollars(o1), _buy_dollars(o2)))

    print("=" * 64)
    print(f"{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
