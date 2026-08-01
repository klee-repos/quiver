#!/usr/bin/env python3
"""Phase-3/4 tests: the cost model, and the reachability audit.

TWO CORRECTIONS FROM THE ADVERSARIAL REVIEW ARE PINNED HERE.

1. "SpreadCost leaves turnover_usd unchanged" is EMPIRICALLY FALSE. A verifier measured
   17 fills/$556.96 -> 13 fills/$376.96 under cost, because cost -> broker.cash ->
   `tick.py` deployable_equity -> target_dollars. Costs feed back into SIZING.

   Following that thread further while building these tests killed a SECOND assumption:
   "cost strictly reduces return" is ALSO false, for the same reason. Measured on the golden
   path, 0/10/50/200 bps -> +4.069% / +7.246% / +10.946% / +1.937% — a costlier run can return
   MORE because it trades smaller. So neither invariance nor monotonicity may be asserted. What
   is actually invariant: the friction is really charged (`total_costs_usd` > 0), the drag
   deepens monotonically, and the curve changes. `cost_elasticity` now DETECTS non-monotonicity
   and withholds `break_even_bps`, because a first-crossing scan over a non-monotone curve
   returns a number that reads like a break-even and is not one.

2. `drop_pct` is identically 0.0 in a one-tick-per-date replay (C4, measured), so the 20%
   daily-loss halt and every `derisk_tiers` entry are structurally UNFIRABLE. The reachability
   audit exists to say which knobs are live and which are dead, rather than letting a sweep
   "optimize" a knob that cannot move the curve.

Run: .venv/bin/python tests/test_bench_costs.py
"""
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import bench.baselines as B          # noqa: E402
import bench.costs as C              # noqa: E402
import bench.ladder as L             # noqa: E402
import bench.reachability as R       # noqa: E402
import capture_replay_golden as golden   # noqa: E402
import lib.wall_replay as wr         # noqa: E402

PASS = 0
FAIL = 0
STRATEGY = _REPO / "strategy.yaml"


def ok(name, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}" + (f"  — {extra!r}" if extra is not None else ""))


DATES, PM, BM = golden.build_price_path()
DS, PRICE_AT, BENCH_AT = wr.make_in_memory_price_fns(PM, BM)
TICKERS = golden.TICKERS
FIXTURE_STRATEGY = _REPO / "tests" / "fixtures" / "strategy_fixture.yaml"


def run(src, *, cost_model=None, cash=10000.0, **kw):
    return L.run_arm("x", src, dates=DS, tickers=TICKERS, price_at=PRICE_AT,
                     benchmark_at=BENCH_AT, strategy_path=FIXTURE_STRATEGY,
                     starting_cash=cash, cost_model=cost_model, **kw)


# ---------------------------------------------------------------- cost model unit

def test_cost_model_is_adverse_on_both_sides():
    print("\n[cost model — slippage must be ADVERSE, not merely applied]")
    z, s = C.ZeroCost(), C.SpreadCost(bps=50.0)
    ok("ZeroCost is a no-op on price", z.exec_price("buy", "X", 100.0) == 100.0)
    ok("ZeroCost charges no fee", z.fee("sell", 1000.0, 10.0) == 0.0)
    ok("ZeroCost.is_zero", z.is_zero is True)

    ok("buy fills ABOVE reference", s.exec_price("buy", "X", 100.0) > 100.0,
       s.exec_price("buy", "X", 100.0))
    ok("sell fills BELOW reference", s.exec_price("sell", "X", 100.0) < 100.0,
       s.exec_price("sell", "X", 100.0))
    # 50bps == 0.50%
    ok("buy slippage is exactly 50bps", abs(s.exec_price("buy", "X", 100.0) - 100.5) < 1e-9)
    ok("sell slippage is exactly 50bps", abs(s.exec_price("sell", "X", 100.0) - 99.5) < 1e-9)

    ok("fees are SELL-side only", s.fee("buy", 10000.0, 100.0) == 0.0)
    ok("sell fee is positive", s.fee("sell", 10000.0, 100.0) > 0)
    ok("TAF is capped", s.fee("sell", 1e9, 1e9) < 1e9 * s.sec_fee_rate + s.taf_cap + 1)
    ok("SpreadCost(0) is still not 'zero' if fees remain", C.SpreadCost(bps=0.0).is_zero is False)


# ---------------------------------------------------------------- integration

def test_zero_cost_is_byte_identical_to_the_golden():
    """HARD CONSTRAINT: the default path must not move. The golden — captured from the
    UNMODIFIED code before any of this work — is the enforcement."""
    print("\n[ZeroCost default — golden byte-identity]")
    produced = golden.produce()
    import json
    recorded = json.loads(golden.FIXTURE.read_text())["scenarios"]
    for name in sorted(produced):
        ok(f"{name} unchanged", produced[name]["curve_sha256"] == recorded[name]["curve_sha256"],
           (name, produced[name]["curve_sha256"][:12], recorded[name]["curve_sha256"][:12]))


def test_cost_is_actually_charged_and_reduces_return():
    print("\n[cost is CHARGED — and feeds back into sizing]")
    src = golden._buy_src(PM)
    free = run(src)
    costly = run(src, cost_model=C.SpreadCost(bps=50.0))

    ok("free run has zero cost", free.summary["total_costs_usd"] == 0.0, free.summary)
    ok("free run has zero drag", free.summary["cost_drag_pct"] == 0.0, free.summary)
    ok("costly run charged something", costly.summary["total_costs_usd"] > 0,
       costly.summary["total_costs_usd"])
    ok("costly run has negative drag", costly.summary["cost_drag_pct"] < 0,
       costly.summary["cost_drag_pct"])
    # NOT asserted: "cost strictly reduces return". MEASURED FALSE (+10.946% at 50bps vs
    # +4.069% free) because cost -> cash -> deployable equity -> target_dollars, so a costlier
    # run also trades SMALLER. The honest invariant is that the friction was really charged and
    # that it changed the outcome — see test_cost_elasticity_detects_non_monotonicity.
    ok("cost changed the outcome", costly.curve_hash() != free.curve_hash())
    # The CORRECTED expectation: turnover is NOT invariant, because cost shrinks cash which
    # shrinks deployable equity which shrinks target_dollars.
    print(f"     free: ret={free.summary['total_return_pct']:+.3f}% "
          f"turnover=${free.summary['turnover_usd']:.2f} fills={free.summary['n_fills']}")
    print(f"     50bp: ret={costly.summary['total_return_pct']:+.3f}% "
          f"turnover=${costly.summary['turnover_usd']:.2f} fills={costly.summary['n_fills']} "
          f"cost=${costly.summary['total_costs_usd']:.2f}")
    ok("turnover responds to cost (NOT invariant)",
       costly.summary["turnover_usd"] != free.summary["turnover_usd"],
       (costly.summary["turnover_usd"], free.summary["turnover_usd"]))


def test_no_fills_means_no_cost_at_any_bps():
    """Zero fills must cost zero — with the zero-fill PRECONDITION asserted, not assumed.

    Without pinning `n_fills == 0` this test would silently become vacuous the moment the wall
    started filling on this path, and would then be asserting nothing at all."""
    print("\n[no fills -> no cost at any bps (precondition pinned)]")
    for bps in (0.0, 50.0, 500.0):
        r = run(B.hold_book(), cost_model=C.SpreadCost(bps=bps), cash=0.0)
        ok(f"{bps}bps: PRECONDITION n_fills == 0", r.summary["n_fills"] == 0, r.summary)
        ok(f"{bps}bps: zero cost charged", r.summary["total_costs_usd"] == 0.0, r.summary)
        ok(f"{bps}bps: zero drag", r.summary["cost_drag_pct"] == 0.0, r.summary)
    # ...and the NON-vacuous companion: with real capital the same cost model DOES charge,
    # proving the zero above came from "no fills", not from a cost model that never fires.
    live = run(golden._buy_src(PM), cost_model=C.SpreadCost(bps=500.0), cash=10000.0)
    ok("with capital, the SAME model charges", live.summary["total_costs_usd"] > 0, live.summary)
    ok("...and actually filled", live.summary["n_fills"] > 0, live.summary)


def test_cost_elasticity_detects_non_monotonicity():
    """Cost feeds back into SIZING, so return is not a monotone function of bps. The tool must
    DETECT that and withhold break_even_bps rather than emit a number that reads like one."""
    print("\n[cost elasticity — non-monotonicity must be detected, not hidden]")
    src = golden._buy_src(PM)
    curve = C.cost_elasticity(lambda cm: run(src, cost_model=cm).summary,
                              bps_grid=(0.0, 10.0, 50.0, 200.0))
    rets = [curve["returns_by_bps"][b]["total_return_pct"] for b in sorted(curve["returns_by_bps"])]
    print(f"     returns by bps: {rets}")
    ok("zero-cost baseline recorded", curve["zero_cost_return_pct"] == rets[0])

    # THE DOLLARS CHARGED are monotone even when the RETURN is not. This is the honest measure.
    drags = [curve["returns_by_bps"][b]["cost_drag_pct"] for b in sorted(curve["returns_by_bps"])]
    ok("drag deepens monotonically with bps",
       all(a >= b - 1e-9 for a, b in zip(drags, drags[1:])), drags)

    is_monotone = all(a >= b - 1e-9 for a, b in zip(rets, rets[1:]))
    ok("monotone flag matches the observed curve", curve["monotone"] == is_monotone,
       (curve["monotone"], is_monotone))
    if not is_monotone:
        ok("break_even_bps WITHHELD on a non-monotone curve",
           curve["break_even_bps"] is None, curve["break_even_bps"])
        ok("note explains the feedback", "sizing" in curve["note"], curve["note"])

    # A synthetic MONOTONE curve must still yield a break-even, so the guard is not just
    # "always withhold".
    seq = iter([{"total_return_pct": 5.0, "cost_drag_pct": 0.0},
                {"total_return_pct": 2.0, "cost_drag_pct": -1.0},
                {"total_return_pct": -1.0, "cost_drag_pct": -3.0}])
    mono = C.cost_elasticity(lambda cm: next(seq), bps_grid=(0.0, 10.0, 50.0))
    ok("monotone curve is flagged monotone", mono["monotone"] is True, mono)
    ok("monotone curve reports a break-even", mono["break_even_bps"] == 50.0, mono)


# ---------------------------------------------------------------- reachability

def test_reachability_marks_dead_knobs_dead():
    print("\n[reachability — a knob that cannot move the curve must be reported DEAD]")
    src = golden._buy_src(PM)

    def runner(overrides):
        return L.run_arm("probe", src, dates=DS, tickers=TICKERS, price_at=PRICE_AT,
                         benchmark_at=BENCH_AT, strategy_path=FIXTURE_STRATEGY,
                         starting_cash=10000.0, overrides=overrides).summary

    # daily_loss_halt_pct CANNOT bind: C4 proved drop_pct is identically 0.0 here.
    dead = R.probe_knob(runner, ("risk", "daily_loss_halt_pct"), baseline=20.0, probe=0.01)
    ok("daily_loss_halt_pct is DEAD in replay", dead["reachable"] is False, dead)
    ok("reports why", "identical" in (dead["detail"] or "").lower()
       or dead["delta_final_equity"] == 0.0, dead)

    # min_buying_power_buffer DOES bind — it directly reduces deployable cash.
    live = R.probe_knob(runner, ("risk", "min_buying_power_buffer"), baseline=5, probe=9000)
    ok("min_buying_power_buffer is REACHABLE", live["reachable"] is True, live)
    ok("reachable knob moved the curve", live["delta_final_equity"] != 0.0, live)

    table = R.audit(runner, R.DEFAULT_KNOBS)
    ok("audit returns a row per knob", len(table["rows"]) == len(R.DEFAULT_KNOBS))
    ok("audit partitions reachable/dead",
       set(table) >= {"rows", "reachable", "dead"}, sorted(table))
    ok("dead list is non-empty (the halt is dead here)", len(table["dead"]) >= 1, table["dead"])
    print(R.format_audit(table))


def main():
    test_cost_model_is_adverse_on_both_sides()
    test_zero_cost_is_byte_identical_to_the_golden()
    test_cost_is_actually_charged_and_reduces_return()
    test_no_fills_means_no_cost_at_any_bps()
    test_cost_elasticity_detects_non_monotonicity()
    test_reachability_marks_dead_knobs_dead()
    print(f"\nbench-costs: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
