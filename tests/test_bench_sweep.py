#!/usr/bin/env python3
"""Phase-5/9 tests: the sweep harness and the reporting statistics.

THE POINT OF THESE TESTS is to prove the harness REFUSES to produce a confident answer from
data that cannot support one. On one price path with ~9 correlated names the honest result is
almost always "inconclusive", and a benchmark that cannot say that is worse than no benchmark.

Run: .venv/bin/python tests/test_bench_sweep.py
"""
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import bench.stats as S      # noqa: E402
import bench.sweep as W      # noqa: E402

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


# A module-level run_fn so ProcessPoolExecutor can pickle it. Deterministic in (point, seed):
# no replay needed to test the GRID MACHINERY, and using a real replay here would test the
# wall instead of the sweep.
def _fake_run(overrides, seed, pin, window):
    base = 10.0
    risk = (overrides.get("risk") or {})
    bump = 2.0 if risk.get("consistency", {}).get("enabled") is False else 0.0
    noise = ((seed * 37) % 11) - 5
    # MUST vary with `pin`. The earlier version ignored it, which is precisely why the
    # walk_forward pin-axis defect passed 44/44: with both calibration regimes producing
    # identical numbers, comparing a live winner against a pinned default was invisible.
    # pinned scores LOWER, so the LIVE arm wins the argmax. That is the configuration in which
    # a point-only default lookup resolves to the WRONG (pinned) regime — with pinned winning,
    # the wrong lookup coincidentally lands on the right key and the defect stays invisible.
    pin_effect = -3.0 if pin else 0.0
    return {"total_return_pct": base + bump + pin_effect + noise * 0.1,
            "final_equity": 10000.0 * (1 + (base + bump + pin_effect) / 100),
            "pinned": pin, "window": window}


def test_gridspec_enumerates_the_full_product():
    print("\n[GridSpec]")
    spec = W.GridSpec(overrides={"risk.consistency.enabled": [True, False],
                                 "risk.min_buying_power_buffer": [5, 50, 500]},
                      seeds=[1, 2])
    pts = spec.points()
    ok("cartesian product", len(pts) == 6, len(pts))
    ok("size counts seeds too", spec.size() == 12, spec.size())
    ok("empty grid yields one default point", W.GridSpec().points() == [{}])
    nested = W._nest({"risk.consistency.enabled": False, "risk.min_buying_power_buffer": 5})
    ok("dotted keys nest correctly",
       nested == {"risk": {"consistency": {"enabled": False}, "min_buying_power_buffer": 5}},
       nested)


def test_grid_runs_in_PROCESSES_and_reports_both_pin_modes():
    print("\n[run_grid — processes, and pinned+live for every cell]")
    spec = W.GridSpec(overrides={"risk.consistency.enabled": [True, False]}, seeds=[1, 2, 3, 4])
    res = W.run_grid(spec, _fake_run, workers=2)
    ok("every cell ran twice (pinned + live)", len(res) == spec.size() * 2, len(res))
    ok("no cell errored", all(r["error"] is None for r in res),
       [r["error"] for r in res if r["error"]][:2])
    ok("both pin modes present", {r["pinned"] for r in res} == {True, False})

    # A raising cell must degrade to an error row, never kill the grid.
    def _boom(overrides, seed, pin, window):
        raise RuntimeError("cell exploded")
    bad = W.run_grid(W.GridSpec(seeds=[1]), _boom, workers=1)
    ok("a raising cell is captured, not fatal", bad[0]["error"] is not None, bad[0])
    ok("error row has no summary", bad[0]["summary"] is None)


def test_summarize_never_returns_a_bare_point_estimate():
    print("\n[summarize — distribution, not a scalar]")
    spec = W.GridSpec(overrides={"risk.consistency.enabled": [True, False]},
                      seeds=[1, 2, 3, 4, 5, 6])
    res = W.run_grid(spec, _fake_run, workers=1)
    summ = W.summarize(res)
    ok("one row per (point, pin)", len(summ) == 4, sorted(summ))
    for key, s in summ.items():
        ok(f"{key[0]} pin={key[1]} reports n/median/iqr/min/max",
           {"n", "median", "iqr", "min", "max", "values"} <= set(s), sorted(s))
        ok(f"{key[0]} pin={key[1]} keeps every value", len(s["values"]) == 6, s["n"])
    print(W.format_summary(summ))


def test_beats_null_refuses_a_verdict_on_overlapping_iqrs():
    print("\n[beats_null — the guard against a narrow 'win']")
    # Clearly separated -> a verdict is allowed.
    hi = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5]
    lo = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    ok("disjoint IQRs -> 'beats'", S.beats_null(hi, lo)["verdict"] == "beats", S.beats_null(hi, lo))
    ok("reversed -> 'loses'", S.beats_null(lo, hi)["verdict"] == "loses")

    # Overlapping -> NO verdict, even though the observed mean is higher.
    a = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    b = [4.0, 5.5, 6.5, 7.5, 8.5, 9.5]
    r = S.beats_null(a, b)
    ok("overlapping IQRs -> no verdict", r["verdict"] is None, r)
    ok("says why", "OVERLAP" in r["reason"], r["reason"])
    ok("mean was higher yet still no verdict",
       sum(a) / len(a) > sum(b) / len(b) and r["verdict"] is None)
    ok("too few samples -> no verdict", S.beats_null([1.0, 2.0], [3.0, 4.0])["verdict"] is None)

    # REGRESSION (adversarial finding): the old nearest-rank iqr returned (2.0, 3.0, 3.0) for
    # [1,2,3,4] — median 3.0 against a true 2.5, and a degenerate Q2 == Q3. That made the pair
    # below report "beats / IQRs disjoint" when the correct quartiles genuinely OVERLAP
    # (observed Q1 2.75 vs null Q3 3.00), violating this module's own documented bar.
    q = S.iqr([1.0, 2.0, 3.0, 4.0])
    ok("iqr uses interpolated quartiles", q == (1.75, 2.5, 3.25), q)
    ok("iqr median is the TRUE median", q[1] == 2.5, q)
    false_beat = S.beats_null([2.0, 3.0, 4.0, 5.0], [0.0, 1.0, 2.0, 6.0])
    ok("overlapping quartiles no longer report 'beats'", false_beat["verdict"] is None, false_beat)


def test_block_bootstrap_is_blocked_and_honest_about_thin_data():
    print("\n[block_bootstrap]")
    rs = [0.001 * ((i * 7) % 13 - 6) for i in range(300)]
    ci = S.block_bootstrap(rs, block=20, n_boot=200, seed=1)
    ok("returns a 3-tuple", ci is not None and len(ci) == 3, ci)
    ok("ordered lo<=mid<=hi", ci[0] <= ci[1] <= ci[2], ci)
    ok("deterministic for a seed", S.block_bootstrap(rs, block=20, n_boot=200, seed=1) == ci)
    ok("different seed -> different draw",
       S.block_bootstrap(rs, block=20, n_boot=200, seed=2) != ci)
    ok("refuses thin data rather than fabricating a CI",
       S.block_bootstrap([0.01, 0.02, 0.03], block=20) is None)


def test_sharpe_from_curve_is_distinct_from_lib_risk_sharpe():
    print("\n[sharpe_from_curve — daily, not per-trade]")
    curve = [{"equity": 10000.0 * (1.001 ** i)} for i in range(120)]
    sh = S.sharpe_from_curve(curve)
    ok("steady growth -> high Sharpe", sh is not None and sh > 5, sh)
    ok("flat curve -> None (no variance)",
       S.sharpe_from_curve([{"equity": 100.0} for _ in range(60)]) is None)
    ok("thin curve -> None", S.sharpe_from_curve([{"equity": 100.0}, {"equity": 101.0}]) is None)
    ok("max_drawdown_pct is negative on a fall",
       S.max_drawdown_pct([{"equity": 100.0}, {"equity": 50.0}]) == -50.0)
    # The two Sharpes are different quantities; lib.risk returns a Metric object, not a float.
    import lib.risk as risk
    m = risk.sharpe([0.01, -0.02, 0.03, 0.01, -0.01, 0.02])
    ok("lib.risk.sharpe returns a Metric, not a float", not isinstance(m, float), type(m).__name__)


def test_walk_forward_evaluates_only_on_unseen_data():
    print("\n[walk_forward — no re-tuning on test]")
    spec = W.GridSpec(overrides={"risk.consistency.enabled": [True, False]},
                      seeds=[1, 2, 3, 4, 5, 6])
    train = W.run_grid(spec, _fake_run, workers=1)
    test = W.run_grid(spec, _fake_run, workers=1)
    wf = W.walk_forward(train, test, default_point={"risk.consistency.enabled": True})
    ok("picked a best point", wf.get("best_point") is not None, wf)
    ok("reports in-sample AND out-of-sample", wf["in_sample_median"] is not None
       and wf["out_of_sample_median"] is not None, wf)
    ok("reports degradation", wf["degradation_pct"] is not None, wf)
    ok("compares against the shipped default", wf["default_oos_median"] is not None, wf)
    # _fake_run is deterministic, so train == test and degradation must be exactly 0.
    ok("identical folds -> 0% degradation", abs(wf["degradation_pct"]) < 1e-9, wf)
    ok("empty grids -> no verdict", W.walk_forward([], [])["verdict"] is None)

    # REGRESSION (adversarial finding): default_key matched the POINT only, so it silently
    # resolved to pinned=True regardless of the winner's regime — letting the shipped default be
    # compared against ITSELF read under a different calibration regime, and declared beaten.
    ok("default is read in the winner's pin regime",
       wf.get("default_pinned") == wf.get("pinned"), wf)
    same = W.walk_forward(train, test, default_point=dict(wf["best_point"]))
    ok("default == winner cannot 'beat' itself", same["verdict"] is None, same)
    ok("...and says why (IQRs identical, therefore overlapping)",
       "OVERLAP" in (same["reason"] or ""), same["reason"])

    # A ratio off a non-positive in-sample median is undefined; edge_delta always is.
    neg = W.walk_forward(
        [{"point": {}, "seed": s_, "pinned": False, "window": None, "error": None,
          "summary": {"total_return_pct": -4.0}} for s_ in range(6)],
        [{"point": {}, "seed": s_, "pinned": False, "window": None, "error": None,
          "summary": {"total_return_pct": -2.0}} for s_ in range(6)])
    ok("negative in-sample -> degradation withheld", neg["degradation_pct"] is None, neg)
    ok("...with a stated reason", "undefined" in (neg["degradation_reason"] or ""), neg)
    ok("edge_delta is still defined", neg["edge_delta"] == 2.0, neg)


def test_mde_is_stated_before_a_result_is_read():
    print("\n[mde_note — under-powered results must LOOK under-powered]")
    note = S.mde_note(6, 0.02)
    ok("names an MDE", "MDE" in note and "%/yr" in note, note)
    ok("warns about indistinguishability", "noise" in note, note)
    ok("undefined at n<=1", "undefined" in S.mde_note(1, 0.02))
    print(f"     {note}")


def test_grid_is_reproducible_across_processes():
    print("\n[cross-process reproducibility of the grid]")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import tests.test_bench_sweep as t, bench.sweep as W, json\n"
        "res = W.run_grid(W.GridSpec(overrides={'risk.consistency.enabled':[True,False]},"
        " seeds=[1,2,3]), t._fake_run, workers=2)\n"
        "print(json.dumps(sorted((json.dumps(r['point'],sort_keys=True), r['seed'], r['pinned'],"
        " r['summary']['total_return_pct']) for r in res)))\n" % str(_REPO)
    )
    outs = []
    for hs in ("0", "9999"):
        p = subprocess.run([sys.executable, "-c", code], cwd=str(_REPO), capture_output=True,
                           text=True, env={**os.environ, "PYTHONHASHSEED": hs})
        lines = (p.stdout or "").strip().splitlines()
        outs.append(lines[-1] if lines else f"ERR:{p.stderr[-300:]}")
    ok("identical grid output under PYTHONHASHSEED 0 and 9999", outs[0] == outs[1],
       [o[:120] for o in outs])


def main():
    test_gridspec_enumerates_the_full_product()
    test_grid_runs_in_PROCESSES_and_reports_both_pin_modes()
    test_summarize_never_returns_a_bare_point_estimate()
    test_beats_null_refuses_a_verdict_on_overlapping_iqrs()
    test_block_bootstrap_is_blocked_and_honest_about_thin_data()
    test_sharpe_from_curve_is_distinct_from_lib_risk_sharpe()
    test_walk_forward_evaluates_only_on_unseen_data()
    test_mde_is_stated_before_a_result_is_read()
    test_grid_is_reproducible_across_processes()
    print(f"\nbench-sweep: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
