#!/usr/bin/env python3
"""Phase-2 tests: the signal ladder and the VOID guard.

THE REGRESSION THAT MATTERS. An adversarial verifier proved that against the REAL
`strategy.yaml` (`risk_policy.min_reward_risk: 3.0`) a source emitting a plausible-but-untuned
stop/target produces **0 orders, +0.00% return and ~300 `reward_risk:below_floor` skips**, while
the SAME source with no stop/target trades normally. A flat curve therefore has two
indistinguishable causes — no edge, or a gate that refused everything — and the second one
silently reads as the first. `bench.ladder.void_reason` exists to make that distinguishable, and
these tests pin it against the real config.

Deterministic: a closed-form price path, no network, no broker, scratch ledgers only.
Run: .venv/bin/python tests/test_bench_ladder.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import bench.baselines as B          # noqa: E402
import bench.ladder as L             # noqa: E402
import lib.wall_replay as wr         # noqa: E402
import tick as T                     # noqa: E402
from lib.config import load_config   # noqa: E402
from lib.ledger import Ledger        # noqa: E402

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


def book_tickers():
    """The REAL book universe, so the REAL strategy.yaml's risk_policy actually binds."""
    cp = L.temp_config(STRATEGY)
    cfg = load_config(cp)
    led = Ledger(Path(tempfile.mkdtemp(prefix="uni_")) / "u.db")
    return sorted(set(T._analysis_universe(cfg, led)))


def price_path(tickers, n=40):
    """A closed-form path with a real uptrend + oscillation (so an oracle has something to find)."""
    dates = []
    y, m, d = 2026, 3, 2
    while len(dates) < n:
        if d % 7 not in (0, 6):
            dates.append(f"{y}-{m:02d}-{d:02d}")
        d += 1
        if d > 27:
            d, m = 2, m + 1
    pm, bm = {}, {}
    for i, dt in enumerate(dates):
        pm[dt] = {}
        for k, tk in enumerate(tickers):
            base = 50.0 + 10.0 * k
            pm[dt][tk] = round(base * (1.0 + 0.004 * i) + 3.0 * ((i * (k + 3)) % 7) - 6.0, 4)
        bm[dt] = round(400.0 * (1.0 + 0.002 * i), 4)
    return dates, pm, bm


TICKERS = book_tickers()
DATES, PM, BM = price_path(TICKERS)
DS, PRICE_AT, BENCH_AT = wr.make_in_memory_price_fns(PM, BM)
CASH = 5000.0


def run(name, src, strategy=None, **kw):
    return L.run_arm(name, src, dates=DS, tickers=TICKERS, price_at=PRICE_AT,
                     benchmark_at=BENCH_AT, strategy_path=(strategy or STRATEGY),
                     starting_cash=CASH, **kw)


def _strategy_with_rr_gate(floor: float = 3.0):
    """A strategy file that ARMS the R:R gate, for the VOID guard only.

    The VOID guard proves that a gate which zeroes a run does not read as "no edge".
    It needs an armed gate to do that. It used to borrow the operator's live
    strategy.yaml, so removing `min_reward_risk` from that file silently DISARMED
    the guard while every assertion still ran. The fixture now supplies the gate,
    so the guard tests the machinery and never the operator's data.
    """
    import yaml as _yaml
    d = _yaml.safe_load(STRATEGY.read_text(encoding="utf-8")) or {}
    d.setdefault("risk_policy", {})["min_reward_risk"] = floor
    out = Path(tempfile.mkdtemp(prefix="ladder_rr_")) / "strategy.yaml"
    out.write_text(_yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    return out


# ---------------------------------------------------------------- the VOID guard

def test_rr_gate_below_floor_is_VOID_not_zero_edge():
    print("\n[VOID guard — a gate-zeroed run must not read as 'no edge']")
    ok("real strategy.yaml exists", STRATEGY.exists(), str(STRATEGY))
    ok("book universe is non-empty", len(TICKERS) >= 3, TICKERS)

    # R:R 1.88 — plausible, and BELOW the 3.0 floor this fixture arms.
    _rr_strategy = _strategy_with_rr_gate(3.0)
    below = B.oracle(PM, horizon=5, rr=B.RRSpec.graded_at(1.875))
    r_below = run("oracle_rr1.88", below, strategy=_rr_strategy)
    # NOTE: the initial book deploy can still place a handful of rebalance orders; what makes
    # the run void is that the gate refused the overwhelming majority of WOULD-BE entries.
    ok("below-floor arm placed few orders", r_below.summary["total_orders"] <= 20, r_below.summary)
    ok("below-floor arm suppressed nearly every entry",
       r_below.skip_reasons.get("reward_risk", 0) >= 100, r_below.skip_reasons)
    ok("skip_reasons captured reward_risk",
       any("reward_risk" in k for k in r_below.skip_reasons), r_below.skip_reasons)
    ok("VOID guard fires", r_below.void is not None, r_below.void)
    ok("VOID names the gate", r_below.void and "reward_risk" in r_below.void, r_below.void)
    ok("total_return_pct suppressed on a VOID arm", r_below.total_return_pct is None)

    # Same signal, ungraded -> the gate cannot bind -> it trades.
    r_ungraded = run("oracle_ungraded", B.oracle(PM, horizon=5, rr=B.RRSpec.ungraded()))
    ok("ungraded arm trades", r_ungraded.summary["total_orders"] > 0, r_ungraded.summary)
    ok("ungraded arm is NOT void", r_ungraded.void is None, r_ungraded.void)

    # Above the floor -> also trades. Proves the gate, not the grading, was the cause.
    r_above = run("oracle_rr4", B.oracle(PM, horizon=5, rr=B.RRSpec.graded_at(4.0)))
    ok("above-floor graded arm trades", r_above.summary["total_orders"] > 0, r_above.summary)
    ok("above-floor arm is NOT void", r_above.void is None, r_above.void)


def test_compare_refuses_to_rank_a_void_arm():
    print("\n[compare() must not rank a VOID arm]")
    hold = run("hold", B.hold_book())
    # Same armed-gate fixture as the VOID guard: compare() can only refuse to rank a
    # void arm if an arm is actually void, and only an armed gate makes one void.
    below = run("void_arm", B.oracle(PM, horizon=5, rr=B.RRSpec.graded_at(1.875)),
                strategy=_strategy_with_rr_gate(3.0))
    cmp_ = L.compare([hold, below], baseline="hold")
    ok("comparison flags any_void", cmp_["any_void"] is True, cmp_)
    void_row = [r for r in cmp_["rows"] if r["arm"] == "void_arm"][0]
    ok("void arm has no gap number", void_row["gap_vs_baseline_pts"] is None, void_row)
    ok("void arm excluded from ranking",
       "void_arm" not in [r["arm"] for r in cmp_["ranked"]], cmp_["ranked"])
    ok("format warns loudly", "VOID" in L.format_comparison(cmp_))


# ---------------------------------------------------------------- the ladder itself

def test_oracle_beats_hold_and_controls_track_signal_quality():
    print("\n[ladder — the gap must track SIGNAL QUALITY, not turnover]")
    hold = run("hold", B.hold_book())
    oracle = run("oracle5", B.oracle(PM, horizon=5))
    anti = run("anti_oracle", B.anti_oracle(PM, horizon=5))   # inverted DECISION = true control
    ok("hold is not void", hold.void is None, hold.void)
    ok("oracle is not void", oracle.void is None, oracle.void)

    g_oracle = oracle.summary["total_return_pct"] - hold.summary["total_return_pct"]
    g_anti = anti.summary["total_return_pct"] - hold.summary["total_return_pct"]
    print(f"     hold={hold.summary['total_return_pct']:+.3f}%  "
          f"oracle={oracle.summary['total_return_pct']:+.3f}% (gap {g_oracle:+.2f}pts)  "
          f"anti={anti.summary['total_return_pct']:+.3f}% (gap {g_anti:+.2f}pts)")
    ok("oracle beats hold", g_oracle > 0, g_oracle)
    # THE CONTROL: an inverted signal must be WORSE than hold. Without this, a positive oracle
    # gap could just be an artifact of being more invested.
    ok("anti-oracle is worse than hold", g_anti < 0, g_anti)
    ok("oracle gap exceeds anti gap", g_oracle > g_anti)


def test_random_arm_is_reproducible_across_processes():
    """The null must be byte-reproducible or the ensemble is not a distribution."""
    print("\n[random arm — cross-process reproducibility]")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import tests.test_bench_ladder as t\n"
        "import bench.baselines as B\n"
        "r = t.run('rnd', B.random_signal(seed=7))\n"
        "print(r.curve_hash())\n" % str(_REPO)
    )
    hashes = []
    for seed_env in ("0", "9999"):
        p = subprocess.run([sys.executable, "-c", code], cwd=str(_REPO),
                           capture_output=True, text=True,
                           env={**__import__("os").environ, "PYTHONHASHSEED": seed_env})
        out = (p.stdout or "").strip().splitlines()
        hashes.append(out[-1] if out else f"ERR:{p.stderr[-200:]}")
    ok("same curve hash under PYTHONHASHSEED 0 and 9999", hashes[0] == hashes[1], hashes)
    ok("hash is a real sha256", len(hashes[0]) == 64, hashes[0])


def test_equal_weight_hold_is_computed_outside_the_wall():
    print("\n[equal-weight hold — the survivorship yardstick]")
    curve = B.equal_weight_hold_curve(PM, DS, TICKERS, CASH)
    ok("one point per priced date", len(curve) == len([d for d in DS if PM.get(d)]), len(curve))
    ok("starts at starting cash", abs(curve[0]["equity"] - CASH) < 1e-6, curve[0])
    ok("no orders concept at all", all(set(p) == {"date", "equity"} for p in curve))
    ok("empty universe -> empty curve", B.equal_weight_hold_curve(PM, DS, [], CASH) == [])


def test_rrspec_semantics():
    print("\n[RRSpec — the gate is an explicit parameter, not an accident]")
    ok("ungraded emits no levels", B.RRSpec.ungraded().levels(100.0) == {})
    ok("ungraded ratio is None", B.RRSpec.ungraded().ratio is None)
    g = B.RRSpec.graded_at(3.0, stop_pct=0.05)
    ok("graded ratio is exact", abs((g.ratio or 0) - 3.0) < 1e-12, g.ratio)
    lv = g.levels(100.0)
    ok("stop below entry", lv["stop_loss"] < 100.0, lv)
    ok("target above entry", lv["target_price"] > 100.0, lv)
    reward = lv["target_price"] - 100.0
    risk = 100.0 - lv["stop_loss"]
    ok("emitted levels realize the ratio", abs(reward / risk - 3.0) < 1e-9, (reward, risk))
    # A bearish row must never carry long stop/target levels.
    rows = B.oracle(PM, horizon=5, rr=g)(DS[0], TICKERS, {})
    bears = [r for r in rows if r["signal"] in ("Underweight", "Sell")]
    ok("bear rows carry no long levels",
       all("stop_loss" not in r for r in bears), bears[:1])


def main():
    test_rrspec_semantics()
    test_rr_gate_below_floor_is_VOID_not_zero_edge()
    test_compare_refuses_to_rank_a_void_arm()
    test_oracle_beats_hold_and_controls_track_signal_quality()
    test_equal_weight_hold_is_computed_outside_the_wall()
    test_random_arm_is_reproducible_across_processes()
    print(f"\nbench-ladder: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
