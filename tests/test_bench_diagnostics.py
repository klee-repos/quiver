#!/usr/bin/env python3
"""Deterministic tests for the behavioral scorecard (bench/diagnostics.py).

No network, no broker, no ledger, no LLM — pure functions over ledger-shaped dict rows.

The load-bearing cases reproduce bugs quiver ACTUALLY PAID FOR, using their real dates:
  * D1 — the "0 bulls in 22 sessions" bearish-brain collapse (fixed 2026-07-24, `016feb2`)
  * D3 — the SOXX buy 2026-07-14 / sell 2026-07-15 rebalance churn
  * D4 — a gate suppressing everything (the R:R floor zeroing a whole run: 300 skips, 0 orders)
A detector that cannot go RED on the bug it was written for is worthless, so each of those has a
paired negative case proving it is not simply always-RED.

Plain asserts, no pytest. Run: .venv/bin/python tests/test_bench_diagnostics.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bench.diagnostics as D  # noqa: E402

PASS = 0
FAIL = 0


def ok(name, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}" + (f"  — {extra!r}" if extra is not None else ""))


def dec(i, date, ticker, signal, *, intent=None, conviction=None, data_quality=None):
    guess = {"buy": "buy", "overweight": "buy", "sell": "sell", "underweight": "sell"}
    return {"id": i, "trade_date": date, "ticker": ticker, "signal": signal,
            "intent": intent if intent is not None else guess.get(signal.lower(), "hold"),
            "conviction": conviction, "data_quality": data_quality}


def act(date, ticker, intent, *, signal="Buy", status="dry_run", dollars=None):
    return {"trade_date": date, "ticker": ticker, "intent": intent, "signal": signal,
            "status": status, "dollar_amount": dollars}


# ---------------------------------------------------------------- pure math first

def test_math_helpers():
    print("\n[math helpers]")
    # Spearman against a hand-computable case: perfectly monotone -> +1, reversed -> -1.
    ok("spearman monotone == +1", abs(D.spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) - 1.0) < 1e-12)
    ok("spearman reversed == -1", abs(D.spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) + 1.0) < 1e-12)
    # Ties must use AVERAGE ranks; with all-equal y there is no variance -> undefined, not 0.
    ok("spearman no-variance -> None", D.spearman([1, 2, 3, 4], [7, 7, 7, 7]) is None)
    ok("spearman n<3 -> None", D.spearman([1, 2], [3, 4]) is None)
    # Known Spearman: x=[1,2,3,4,5], y=[2,1,4,3,5] -> rho = 1 - 6*4/(5*24) = 0.8
    ok("spearman known value 0.8", abs(D.spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) - 0.8) < 1e-12,
       D.spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]))

    # Binomial: 0 of 22 at p=.5 is 2*0.5^22; 11 of 22 is 1.0.
    ok("binom 0/22 ~ 2*0.5^22", abs(D.binom_two_sided_p(0, 22) - 2 * 0.5 ** 22) < 1e-15,
       D.binom_two_sided_p(0, 22))
    ok("binom 11/22 == 1.0", abs(D.binom_two_sided_p(11, 22) - 1.0) < 1e-9)
    ok("binom n=0 -> None", D.binom_two_sided_p(0, 0) is None)

    ok("ols_slope positive", (D.ols_slope([0, 1, 2, 3], [0, 2, 4, 6]) or 0) > 1.99)
    # NOTE: compare against None explicitly — a legitimate slope of 0.0 is falsy, so
    # `slope or <default>` would silently substitute the default and pass a broken slope.
    _flat = D.ols_slope([0, 1, 2, 3], [5, 5, 5, 5])
    ok("ols_slope flat == 0", _flat is not None and abs(_flat) < 1e-12, _flat)
    ok("ols_slope no x-variance -> None", D.ols_slope([2, 2, 2, 2], [1, 2, 3, 4]) is None)


# ---------------------------------------------------------------- D1

def test_d1_zero_bulls_is_red():
    print("\n[D1 signal_balance — the 0-bulls-in-22-sessions bug]")
    # The REAL shape of the bug: 22 sessions, every call bearish.
    rows = [dec(i, f"2026-06-{(i % 28) + 1:02d}", "SMH", "Underweight") for i in range(22)]
    r = D.d1_signal_balance(rows)
    ok("0/22 bulls -> RED", r["flag"] == D.RED, r)
    ok("value == 0.0", r["value"] == 0.0, r)
    ok("n == 22", r["n"] == 22, r)
    ok("binom p reported in detail", "binom_p=" in r["detail"], r["detail"])

    # NEGATIVE CONTROL: the detector must not be always-RED.
    bal = ([dec(i, f"2026-06-{(i % 28) + 1:02d}", "SMH", "Overweight") for i in range(11)] +
           [dec(100 + i, f"2026-06-{(i % 28) + 1:02d}", "URA", "Underweight") for i in range(11)])
    rb = D.d1_signal_balance(bal)
    ok("11/22 balanced -> GREEN", rb["flag"] == D.GREEN, rb)

    # All-bull is the SAME pathology mirrored and must also fire.
    allbull = [dec(i, f"2026-06-{(i % 28) + 1:02d}", "SMH", "Buy") for i in range(22)]
    ok("22/22 bulls -> RED (mirror)", D.d1_signal_balance(allbull)["flag"] == D.RED)


def test_d1_hold_is_not_a_stance():
    print("\n[D1 — Hold is an abstention, not a bull]")
    # 20 Holds + 1 bull + 1 bear. If Hold were folded into either side the ratio would be a lie.
    rows = ([dec(i, "2026-06-01", "SMH", "Hold") for i in range(20)] +
            [dec(50, "2026-06-02", "SMH", "Buy"), dec(51, "2026-06-03", "URA", "Sell")])
    r = D.d1_signal_balance(rows)
    ok("Holds excluded from n", r["n"] == 2, r)
    ok("n=2 -> LOW_N not GREEN", r["flag"] == D.LOW_N, r)
    ok("holds counted in detail", "holds=20" in r["detail"], r["detail"])


def test_low_n_is_never_green():
    print("\n[LOW_N never silently passes]")
    rows = [dec(i, "2026-06-01", "SMH", "Underweight") for i in range(6)]
    r = D.d1_signal_balance(rows)
    ok("n=6 -> LOW_N", r["flag"] == D.LOW_N, r)
    ok("LOW_N is not GREEN", r["flag"] != D.GREEN)
    sc = D.scorecard(rows, [], [])
    ok("LOW_N never enters red_flags", "d1_signal_balance" not in sc["red_flags"], sc["red_flags"])
    ok("flag_counts records LOW_N", sc["summary"]["flag_counts"].get(D.LOW_N, 0) >= 1,
       sc["summary"])


# ---------------------------------------------------------------- D3

def test_d3_soxx_churn_is_red():
    print("\n[D3 round_trip_churn — the real SOXX 07-14/07-15 bug]")
    acts = [act("2026-07-14", "SOXX", "buy", signal="REBALANCE"),
            act("2026-07-15", "SOXX", "sell", signal="RECONCILE")]
    r = D.d3_round_trip_churn(acts)
    ok("SOXX 1-day round trip -> RED", r["flag"] == D.RED, r)
    ok("names SOXX in detail", "SOXX" in r["detail"], r["detail"])
    ok("reports gap=1d", "gap=1d" in r["detail"], r["detail"])
    ok("counts 1 pair", r["value"] == 1, r)


def test_d3_counts_rebalance_signals():
    """The whole point: lib/ledger.py:757 EXCLUDES REBALANCE/RECONCILE from the consistency
    gate, so the gate structurally could not have seen this bug. A detector that copied the
    gate's filter would be blind to it too."""
    print("\n[D3 — must count REBALANCE/RECONCILE, unlike the consistency gate]")
    acts = [act("2026-07-14", "SOXX", "buy", signal="REBALANCE"),
            act("2026-07-15", "SOXX", "sell", signal="RECONCILE")]
    ok("rebalance-only churn still RED", D.d3_round_trip_churn(acts)["flag"] == D.RED)
    # sanity: the same pair as discretionary signals also fires
    acts2 = [act("2026-07-14", "SOXX", "buy", signal="Buy"),
             act("2026-07-15", "SOXX", "sell", signal="Sell")]
    ok("discretionary churn also RED", D.d3_round_trip_churn(acts2)["flag"] == D.RED)


def test_d3_negative_controls():
    print("\n[D3 negative controls]")
    ok("no actions -> GREEN", D.d3_round_trip_churn([])["flag"] == D.GREEN)
    # A hold of 30 days is not churn.
    held = [act("2026-06-01", "SMH", "buy"), act("2026-07-01", "SMH", "sell")]
    ok("30-day hold -> GREEN", D.d3_round_trip_churn(held)["flag"] == D.GREEN,
       D.d3_round_trip_churn(held))
    # Two buys with no sell is not a round trip.
    buys = [act("2026-06-01", "SMH", "buy"), act("2026-06-02", "SMH", "buy")]
    ok("buy,buy -> GREEN", D.d3_round_trip_churn(buys)["flag"] == D.GREEN)
    # A sell BEFORE the buy is not a round trip (ordering matters).
    rev = [act("2026-06-02", "SMH", "buy"), act("2026-06-01", "SMH", "sell")]
    ok("sell-then-buy -> GREEN", D.d3_round_trip_churn(rev)["flag"] == D.GREEN,
       D.d3_round_trip_churn(rev))
    # Unplaced (skipped) actions must not count.
    skipped = [act("2026-07-14", "SOXX", "buy", status="skipped"),
               act("2026-07-15", "SOXX", "sell", status="skipped")]
    ok("skipped actions ignored", D.d3_round_trip_churn(skipped)["flag"] == D.GREEN)
    # Gap of exactly max_gap_days fires; one past it does not.
    edge = [act("2026-06-01", "X", "buy"), act("2026-06-04", "X", "sell")]
    ok("gap==3d fires", D.d3_round_trip_churn(edge)["flag"] == D.RED)
    over = [act("2026-06-01", "X", "buy"), act("2026-06-05", "X", "sell")]
    ok("gap==4d does not", D.d3_round_trip_churn(over)["flag"] == D.GREEN)


# ---------------------------------------------------------------- D4

def test_d4_gate_off_switch():
    print("\n[D4 gate_suppression_mix — the R:R floor zeroing a whole run]")
    # The measured artifact: 300 reward_risk:below_floor skips and 0 orders.
    rows = [{"status": "skipped", "detail": "reward_risk:below_floor"} for _ in range(30)]
    r = D.d4_gate_suppression_mix(rows)
    ok("single reason 100% -> RED", r["flag"] == D.RED, r)
    ok("names reward_risk", "reward_risk" in r["detail"], r["detail"])

    mixed = ([{"status": "skipped", "detail": "reward_risk:below_floor"} for _ in range(8)] +
             [{"status": "skipped", "detail": "consistency:basis_churn"} for _ in range(7)] +
             [{"status": "skipped", "detail": "hold"} for _ in range(8)])
    ok("mixed reasons -> GREEN", D.d4_gate_suppression_mix(mixed)["flag"] == D.GREEN,
       D.d4_gate_suppression_mix(mixed))
    ok("few skips -> LOW_N", D.d4_gate_suppression_mix(rows[:5])["flag"] == D.LOW_N)


# ---------------------------------------------------------------- D5 / D7 / D8

def test_d5_conviction_calibration():
    print("\n[D5 conviction_calibration]")
    # Conviction perfectly predicts correctness -> rho == 1 -> GREEN.
    decs, outs = [], []
    for i in range(40):
        high = i % 2 == 0
        decs.append(dec(i, "2026-06-01", "SMH", "Buy", intent="buy",
                        conviction=90.0 if high else 10.0))
        outs.append({"decision_id": i, "directional_return": 0.05 if high else -0.05})
    r = D.d5_conviction_calibration(decs, outs)
    ok("perfect calibration -> GREEN", r["flag"] == D.GREEN, r)
    ok("rho ~ 1.0", r["value"] is not None and r["value"] > 0.99, r)

    # INVERTED: high conviction is systematically wrong -> rho < 0 -> RED.
    outs_inv = [{"decision_id": i, "directional_return": -0.05 if i % 2 == 0 else 0.05}
                for i in range(40)]
    ri = D.d5_conviction_calibration(decs, outs_inv)
    ok("inverted calibration -> RED", ri["flag"] == D.RED, ri)
    ok("rho < 0", ri["value"] is not None and ri["value"] < 0, ri)

    ok("thin data -> LOW_N", D.d5_conviction_calibration(decs[:5], outs[:5])["flag"] == D.LOW_N)
    # A sell that goes down IS a hit — direction must be respected, not assumed long.
    sd = [dec(1, "2026-06-01", "X", "Sell", intent="sell", conviction=90.0)]
    so = [{"decision_id": 1, "directional_return": -0.10}]
    ok("_is_hit respects a short thesis", D._is_hit("sell", -0.10) is True)
    ok("_is_hit buy on a drop is a miss", D._is_hit("buy", -0.10) is False)
    ok("_is_hit unknown intent -> None", D._is_hit("hold", 0.1) is None)


def test_d7_holding_period():
    print("\n[D7 holding_period]")
    quick = []
    for i in range(6):
        quick += [act(f"2026-06-{2*i+1:02d}", f"T{i}", "buy"),
                  act(f"2026-06-{2*i+2:02d}", f"T{i}", "sell")]
    r = D.d7_holding_period(quick)
    ok("1-day holds -> RED", r["flag"] == D.RED, r)
    ok("median == 1", r["value"] == 1.0, r)
    slow = []
    for i in range(6):
        slow += [act(f"2026-0{(i % 3) + 1}-01", f"T{i}", "buy"),
                 act(f"2026-0{(i % 3) + 4}-01", f"T{i}", "sell")]
    ok("long holds -> GREEN", D.d7_holding_period(slow)["flag"] == D.GREEN,
       D.d7_holding_period(slow))


def test_d8_data_quality():
    """THE FIXTURE IS DERIVED BY CALLING THE REAL PRODUCER.

    The first version of this test passed `data_quality=None`, which a real decision NEVER is —
    and that non-producible fixture hid a total defect: `analyze.assess_data_quality` ALWAYS
    emits the KEY `sources_unavailable`, so a substring scan for "unavailable" matched every
    healthy row and D8 was permanently RED (measured 20/20 degraded on 20 flawless decisions).
    A detector that always fires is worse than none. So the fixtures below come from the actual
    producer, not from a hand-written guess at its shape."""
    print("\n[D8 data_quality_degradation — fixtures from the REAL producer]")
    import json as _json
    import analyze

    healthy_dq = analyze.assess_data_quality({
        "market_report": "m" * 200, "sentiment_report": "s" * 200, "news_report": "n" * 200,
        "fundamentals_report": "f" * 200, "macro_report": "x" * 200})
    degraded_dq = analyze.assess_data_quality({
        "market_report": "m" * 200, "sentiment_report": "", "news_report": "Error retrieving data",
        "fundamentals_report": "f" * 200, "macro_report": "x" * 200})
    ok("producer marks a healthy state clean", healthy_dq["sources_unavailable"] == [], healthy_dq)
    ok("producer marks the degraded state", set(degraded_dq["sources_unavailable"]) ==
       {"news", "sentiment"}, degraded_dq)

    healthy = [dec(i, "2026-06-01", "URA", "Buy", data_quality=_json.dumps(healthy_dq))
               for i in range(20)]
    rh = D.d8_data_quality_degradation(healthy)
    ok("REAL healthy payload -> GREEN (was permanently RED)", rh["flag"] == D.GREEN, rh)
    ok("healthy counts zero degraded", "degraded=0/20" in rh["detail"], rh["detail"])

    degraded = [dec(i, "2026-06-01", "URA", "Buy", data_quality=_json.dumps(degraded_dq))
                for i in range(20)]
    rd = D.d8_data_quality_degradation(degraded)
    ok("REAL degraded payload -> RED", rd["flag"] == D.RED, rd)

    # core_available False is degraded even with an empty sources_unavailable list.
    core = [dec(i, "2026-06-01", "URA", "Buy",
                data_quality=_json.dumps({"core_available": False, "sources_unavailable": []}))
            for i in range(12)]
    ok("core_available False -> RED", D.d8_data_quality_degradation(core)["flag"] == D.RED)
    # a dict (not a JSON string) must work too — the replay path passes dicts
    dict_rows = [dec(i, "2026-06-01", "URA", "Buy", data_quality=healthy_dq) for i in range(20)]
    ok("dict payload also GREEN", D.d8_data_quality_degradation(dict_rows)["flag"] == D.GREEN)

    rows = ([dec(i, "2026-06-01", "SMH", "ERROR") for i in range(6)] +
            [dec(100 + i, "2026-06-01", "URA", "Buy", data_quality=_json.dumps(healthy_dq))
             for i in range(6)])
    ok("50% ERROR -> RED", D.d8_data_quality_degradation(rows)["flag"] == D.RED)
    # LEGACY free-text shape (bench/brain_tasks.py writes this) must still be understood.
    legacy = [dec(i, "2026-06-01", "URA", "Buy", data_quality="unavailable: trend_report")
              for i in range(12)]
    ok("legacy free-text still detected", D.d8_data_quality_degradation(legacy)["flag"] == D.RED)


# ---------------------------------------------------------------- D9 / D10

def test_d9_doubling_down_into_drawdown():
    print("\n[D9 trade_size_vs_drawdown — the paper's 'Gemini goes all in']")
    # Equity falls monotonically; exposure RISES as drawdown deepens -> positive slope -> RED.
    curve = []
    for i in range(12):
        eq = 10000.0 - 500.0 * i
        curve.append({"equity": eq, "gross_notional": 100.0 + 200.0 * i})
    r = D.d9_trade_size_vs_drawdown(curve)
    ok("adds risk into losses -> RED", r["flag"] == D.RED, r)
    ok("slope > 0", (r["value"] or 0) > 0, r)

    # De-risking into drawdown -> negative slope -> GREEN.
    curve2 = []
    for i in range(12):
        eq = 10000.0 - 500.0 * i
        curve2.append({"equity": eq, "gross_notional": max(10.0, 3000.0 - 250.0 * i)})
    r2 = D.d9_trade_size_vs_drawdown(curve2)
    ok("de-risks into losses -> GREEN", r2["flag"] == D.GREEN, r2)
    ok("missing gross_notional -> LOW_N",
       D.d9_trade_size_vs_drawdown([{"equity": 1.0}] * 12)["flag"] == D.LOW_N)


def test_d10_target_oscillation():
    print("\n[D10 target_oscillation — the SOXX allocator oscillation]")
    osc = [{"date": f"2026-07-{i+1:02d}", "weights": {"SOXX": 10.0 + (5.0 if i % 2 else -5.0)}}
           for i in range(14)]
    r = D.d10_target_oscillation(osc)
    ok("alternating target -> RED", r["flag"] == D.RED, r)
    ok("names SOXX", "SOXX" in r["detail"], r["detail"])
    mono = [{"date": f"2026-07-{i+1:02d}", "weights": {"SOXX": 10.0 + 0.5 * i}} for i in range(14)]
    ok("monotone target -> GREEN", D.d10_target_oscillation(mono)["flag"] == D.GREEN,
       D.d10_target_oscillation(mono))
    ok("no targets -> LOW_N", D.d10_target_oscillation([])["flag"] == D.LOW_N)


# ---------------------------------------------------------------- scorecard wiring

def test_scorecard_shape_and_flags():
    print("\n[scorecard]")
    decs = [dec(i, f"2026-06-{(i % 28) + 1:02d}", "SMH", "Underweight") for i in range(22)]
    acts = [act("2026-07-14", "SOXX", "buy", signal="REBALANCE"),
            act("2026-07-15", "SOXX", "sell", signal="RECONCILE")]
    sc = D.scorecard(decs, acts, [])
    ok("has all 9 detectors", len(sc["diagnostics"]) == 9, sorted(sc["diagnostics"]))
    ok("D1 red flagged", "d1_signal_balance" in sc["red_flags"], sc["red_flags"])
    ok("D3 red flagged", "d3_round_trip_churn" in sc["red_flags"], sc["red_flags"])
    ok("summary counts decisions", sc["summary"]["n_decisions"] == 22)
    for name, d in sc["diagnostics"].items():
        ok(f"{name} has full shape",
           set(d) == {"value", "n", "flag", "threshold", "detail"}, (name, sorted(d)))
        ok(f"{name} flag is valid", d["flag"] in (D.GREEN, D.AMBER, D.RED, D.LOW_N), (name, d["flag"]))
    txt = D.format_scorecard(sc)
    ok("format mentions RED", "RED" in txt)
    ok("empty input does not crash", isinstance(D.scorecard([], [], []), dict))


def main():
    test_math_helpers()
    test_d1_zero_bulls_is_red()
    test_d1_hold_is_not_a_stance()
    test_low_n_is_never_green()
    test_d3_soxx_churn_is_red()
    test_d3_counts_rebalance_signals()
    test_d3_negative_controls()
    test_d4_gate_off_switch()
    test_d5_conviction_calibration()
    test_d7_holding_period()
    test_d8_data_quality()
    test_d9_doubling_down_into_drawdown()
    test_d10_target_oscillation()
    test_scorecard_shape_and_flags()
    print(f"\nbench-diagnostics: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
