#!/usr/bin/env python3
"""Phase-1 tests: replay instrumentation, the golden byte-identity guard, and the two
determinism fixes that were empirically confirmed broken (C1, C3).

WHY A GOLDEN AND NOT `test_determinism`. `tests/test_e2e_wall_replay.py::test_determinism` runs
the same replay twice in ONE process against ONE build. That proves REPRODUCIBILITY. It is
structurally incapable of noticing that the default path CHANGED — measured: it stays 2/2 green
while a 10bps default fill haircut moves 4 of 5 golden curves. The golden fixture is the actual
enforcement of the byte-identical-default invariant, and it is produced by
`scripts/capture_replay_golden.py`, which calls the real `run_backtest` — so the fixture is
always regenerable by the code it describes.

No network, no broker, no live ledger.
Run: .venv/bin/python tests/test_bench_replay.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from datetime import datetime                          # noqa: E402
from zoneinfo import ZoneInfo                          # noqa: E402

from lib import market                                 # noqa: E402
from lib.config import load_config                     # noqa: E402
from lib.ledger import Ledger                          # noqa: E402
import lib.wall_replay as wr                           # noqa: E402
import capture_replay_golden as golden                 # noqa: E402

ET = ZoneInfo("America/New_York")
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


# ---------------------------------------------------------------- the golden guard

def test_golden_curves_reproduce():
    print("\n[golden — the byte-identical-default guard]")
    fx = golden.FIXTURE
    ok("fixture exists", fx.exists(), str(fx))
    if not fx.exists():
        return
    recorded = json.loads(fx.read_text())
    ok("fixture pins all 8 curve fields",
       recorded["_hashed_fields"] == ["date", "equity", "cash", "positions_mv",
                                      "drop_pct", "n_orders", "n_filled", "halt"],
       recorded["_hashed_fields"])
    # Provenance must be honest: this fixture was produced from a DIRTY worktree, and a clean
    # checkout of the head sha will NOT reproduce it.
    prov = recorded.get("_provenance", {})
    ok("provenance records dirty state", str(prov.get("git_head", "")).endswith("+dirty"), prov)
    ok("provenance records the diff sha", len(str(prov.get("git_diff_sha256_16") or "")) == 16, prov)

    produced = golden.produce()          # re-runs the REAL run_backtest, all 5 scenarios
    ok("all 5 scenarios present", set(produced) == set(recorded["scenarios"]),
       (sorted(produced), sorted(recorded["scenarios"])))
    for name in sorted(produced):
        exp, got = recorded["scenarios"][name], produced[name]
        ok(f"{name}: curve sha matches", exp["curve_sha256"] == got["curve_sha256"],
           (exp["curve_sha256"][:16], got["curve_sha256"][:16]))
        ok(f"{name}: final_equity matches", exp["final_equity"] == got["final_equity"],
           (exp["final_equity"], got["final_equity"]))
        ok(f"{name}: total_orders matches", exp["total_orders"] == got["total_orders"],
           (exp["total_orders"], got["total_orders"]))


# ---------------------------------------------------------------- C1: clock restore

def test_nested_frozen_clock_is_restored():
    """C1. `run_backtest`'s `finally` used to call `market.set_clock(None)`, blanking an
    ENCLOSING pin instead of restoring it. bench/sweep.py nests runs inside one pinned window,
    so this silently reverted the driver to the real wall clock after the first fold."""
    print("\n[C1 — nested frozen_clock survives run_backtest]")
    outer = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    dates, price_map, bench_map = golden.build_price_path()
    ds, price_at, bench_at = wr.make_in_memory_price_fns(price_map, bench_map)
    cp = golden._cfg_path(rebalance=True)
    cfg = load_config(cp)
    led = Ledger(Path(tempfile.mkdtemp(prefix="c1_")) / "l.db")

    with market.frozen_clock(outer):
        before = market.now_et()
        wr.run_backtest(cfg, led, dates=ds[:6], tickers=golden.TICKERS,
                        signal_source=lambda d, t, s: [], price_at=price_at,
                        benchmark_at=bench_at, starting_cash=10000.0, config_path=cp)
        after = market.now_et()
    ok("outer pin intact before", before == outer, before)
    ok("outer pin SURVIVES run_backtest", after == outer, (after, outer))
    ok("clock released outside the block", market._now_override is None, market._now_override)


# ---------------------------------------------------------------- C3: dateless rows

def test_jsonl_signal_source_rejects_dateless_rows():
    """C3. A row with no `date` used to key under the literal string "None", so a whole recorded
    stream silently yielded ZERO signals and the replay read as a clean all-Hold run.
    `analyze.py` emits no `date`, so this was the state of every recorded stream."""
    print("\n[C3 — recorded stream must not silently vanish]")
    p = Path(tempfile.mkdtemp(prefix="c3_")) / "s.jsonl"

    p.write_text(json.dumps({"ticker": "AAA", "signal": "Buy"}) + "\n")
    raised = None
    try:
        wr.jsonl_signal_source(str(p))
    except ValueError as e:
        raised = str(e)
    ok("dateless row raises ValueError", raised is not None)
    ok("error names the fix", raised is not None and "tee-signals" in raised, raised)

    # blank/whitespace date is just as broken and must also raise
    p.write_text(json.dumps({"date": "   ", "ticker": "AAA", "signal": "Buy"}) + "\n")
    blank_raised = False
    try:
        wr.jsonl_signal_source(str(p))
    except ValueError:
        blank_raised = True
    ok("blank date also raises", blank_raised)

    # POSITIVE: a well-formed stream round-trips and actually yields its rows on the right date.
    rows = [{"date": "2026-03-02", "ticker": "AAA", "signal": "Buy", "position_pct": 10.0},
            {"date": "2026-03-03", "ticker": "BBB", "signal": "Sell"}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    src = wr.jsonl_signal_source(str(p))
    got = src("2026-03-02", ["AAA", "BBB"], {})
    ok("valid stream yields that date's rows", len(got) == 1 and got[0]["ticker"] == "AAA", got)
    ok("other date yields nothing", src("2026-03-09", ["AAA"], {}) == [])


# ---------------------------------------------------------------- instrumentation

def _run_instrumented(cash=10000.0):
    dates, price_map, bench_map = golden.build_price_path()
    ds, price_at, bench_at = wr.make_in_memory_price_fns(price_map, bench_map)
    cp = golden._cfg_path(rebalance=True)
    cfg = load_config(cp)
    led = Ledger(Path(tempfile.mkdtemp(prefix="instr_")) / "l.db")
    src = golden._buy_src(price_map)
    res = wr.run_backtest(cfg, led, dates=ds, tickers=golden.TICKERS, signal_source=src,
                          price_at=price_at, benchmark_at=bench_at, starting_cash=cash,
                          config_path=cp)
    return res, cash


def test_blotter_reconciles_against_simulation_state():
    """The blotter must reconcile against the SIMULATED PORTFOLIO, not against itself.

    (Asserting `sum(fills) == sum(gross_notional)` would be a tautology — both are derived from
    the same list. This instead ties the blotter to cash the SimBroker actually moved.)"""
    print("\n[instrumentation — blotter reconciles to broker cash]")
    res, cash0 = _run_instrumented()
    ok("fills key present", "fills" in res)
    ok("targets key present", "targets" in res)
    filled = [f for f in res["fills"] if f.get("filled")]
    ok("some fills occurred", len(filled) > 0, res["summary"])

    spent = sum(float(f["dollars"]) for f in filled if f["side"] == "buy")
    got = sum(float(f["proceeds"]) for f in filled if f["side"] == "sell")
    final_cash = res["equity_curve"][-1]["cash"]
    # INDEPENDENT cross-check: cash out - cash in must equal the cash the broker actually moved.
    ok("blotter reconciles to broker cash",
       abs((cash0 - spent + got) - final_cash) < 1e-4,
       {"start": cash0, "spent": spent, "proceeds": got, "expected": cash0 - spent + got,
        "actual_final_cash": final_cash})

    ok("n_fills matches blotter", res["summary"]["n_fills"] == len(filled),
       (res["summary"]["n_fills"], len(filled)))
    ok("turnover_usd > 0", res["summary"]["turnover_usd"] > 0, res["summary"]["turnover_usd"])
    ok("turnover_ratio > 0", res["summary"]["turnover_ratio"] > 0)
    ok("every fill carries a date", all(f.get("date") for f in res["fills"]))
    ok("targets one row per tick", len(res["targets"]) == len(res["equity_curve"]),
       (len(res["targets"]), len(res["equity_curve"])))
    ok("gross_notional on every curve point",
       all("gross_notional" in p for p in res["equity_curve"]))


def test_instrumentation_feeds_the_scorecard():
    """The whole point of Phase 1: D9 and D10 are uncomputable without it."""
    print("\n[instrumentation -> diagnostics D9/D10 become computable]")
    import bench.diagnostics as D
    res, _ = _run_instrumented()
    d9 = D.d9_trade_size_vs_drawdown(res["equity_curve"])
    d10 = D.d10_target_oscillation(res["targets"])
    ok("D9 no longer LOW_N-for-missing-field",
       "needs gross_notional" not in (d9["detail"] or ""), d9)
    ok("D9 returns a real flag", d9["flag"] in (D.GREEN, D.RED, D.LOW_N), d9)
    ok("D10 returns a real flag", d10["flag"] in (D.GREEN, D.AMBER, D.RED, D.LOW_N), d10)


def test_c4_drop_pct_is_structurally_zero():
    """C4, pinned as a REGRESSION so the limitation cannot be silently forgotten: in a
    one-tick-per-date replay the day's baseline is created AT the current equity, so `drop_pct`
    is identically 0.0 and the 20% daily-loss halt (and every derisk tier) is UNFIRABLE.
    Any future change that makes drawdown reachable must update this test deliberately."""
    print("\n[C4 — drop_pct is identically 0.0 (documented limitation)]")
    res, _ = _run_instrumented()
    drops = {round(p["drop_pct"], 9) for p in res["equity_curve"] if p["drop_pct"] is not None}
    ok("drop_pct has exactly one distinct value", len(drops) == 1, drops)
    ok("that value is 0.0", drops == {0.0}, drops)
    ok("no tick halted", not any(p["halt"] for p in res["equity_curve"]))


def test_tee_signals_round_trips_and_never_raises():
    """`--tee-signals` is the producer the recorded-stream seam never had.

    Two obligations. (1) PRODUCIBILITY: the archive it writes must be consumable by the REAL
    `jsonl_signal_source` — a fixture whose producer cannot emit it is how a defect ships
    through a green suite. (2) SAFETY: this runs on the LIVE trading path, so no input, no
    filesystem state, and no unserializable payload may raise."""
    print("\n[--tee-signals — producible archive, and safe on the live path]")
    import run_analyses as ra
    d = Path(tempfile.mkdtemp(prefix="tee_"))

    n = ra.append_tee(d / "a.jsonl", [{"ticker": "SMH", "signal": "Buy"},
                                      {"ticker": "URA", "signal": "Sell"}], "2026-07-30")
    ok("appends both rows", n == 2, n)
    rows = [json.loads(x) for x in (d / "a.jsonl").read_text().splitlines()]
    ok("stamps the date analyze.py omits", rows[0]["date"] == "2026-07-30", rows[0])
    ok("preserves the payload", rows[0]["ticker"] == "SMH" and rows[0]["signal"] == "Buy", rows[0])

    ra.append_tee(d / "a.jsonl", [{"ticker": "CEG", "signal": "Hold"}], "2026-07-31")
    ok("append-only (does not truncate)",
       len((d / "a.jsonl").read_text().splitlines()) == 3)

    # THE ROUND TRIP: the real consumer must accept what the real producer emitted.
    src = wr.jsonl_signal_source(str(d / "a.jsonl"))
    got = src("2026-07-30", ["SMH", "URA"], {})
    ok("real consumer accepts real producer output", len(got) == 2, got)
    ok("consumer keys on the produced date",
       {r["ticker"] for r in got} == {"SMH", "URA"}, got)
    ok("later date isolated", len(src("2026-07-31", ["CEG"], {})) == 1)

    # `append_tee` is APPEND-ONLY by design, so re-running a day legitimately leaves two rows
    # for one (date, ticker). The consumer must fold to LAST-WINS, or _run_plan receives a
    # duplicate analysis for one name; and it must drop names outside the requested universe,
    # or the replay sizes a ticker it never priced.
    dup = Path(tempfile.mkdtemp(prefix="dup_")) / "s.jsonl"
    dup.write_text("\n".join(json.dumps(r) for r in [
        {"date": "2026-07-30", "ticker": "SMH", "signal": "Buy", "position_pct": 5.0},
        {"date": "2026-07-30", "ticker": "SMH", "signal": "Sell", "position_pct": 0.0},
        {"date": "2026-07-30", "ticker": "NOTINBOOK", "signal": "Buy"},
    ]) + "\n")
    dsrc = wr.jsonl_signal_source(str(dup))
    got2 = dsrc("2026-07-30", ["SMH"], {})
    ok("duplicate (date,ticker) folds to ONE row", len(got2) == 1, got2)
    ok("last write wins", got2[0]["signal"] == "Sell", got2)
    ok("ticker outside the universe is dropped",
       all(r["ticker"] == "SMH" for r in got2), got2)
    ok("empty universe passes everything through",
       len(dsrc("2026-07-30", [], {})) == 2, dsrc("2026-07-30", [], {}))

    # SAFETY: none of these may raise, and all must report 0.
    ok("unwritable path returns 0", ra.append_tee("/proc/nope/x.jsonl", [{"t": 1}], "2026-07-30") == 0)
    ok("empty rows returns 0", ra.append_tee(d / "c.jsonl", [], "2026-07-30") == 0)
    ok("blank date returns 0", ra.append_tee(d / "c.jsonl", [{"t": 1}], "") == 0)
    ok("None rows returns 0", ra.append_tee(d / "c.jsonl", None, "2026-07-30") == 0)

    class _Weird:
        def __repr__(self):
            return "<weird>"
    ok("unserializable payload does not raise",
       ra.append_tee(d / "b.jsonl", [{"ticker": "X", "o": _Weird()}], "2026-07-30") == 1)


def main():
    test_tee_signals_round_trips_and_never_raises()
    test_golden_curves_reproduce()
    test_nested_frozen_clock_is_restored()
    test_jsonl_signal_source_rejects_dateless_rows()
    test_blotter_reconciles_against_simulation_state()
    test_instrumentation_feeds_the_scorecard()
    test_c4_drop_pct_is_structurally_zero()
    print(f"\nbench-replay: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
