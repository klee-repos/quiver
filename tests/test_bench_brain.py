#!/usr/bin/env python3
"""Phase-8a tests: the frozen-fixture brain regression.

GATING IS THE LOAD-BEARING PART OF THIS FILE.

`QUIVER_REPLAY_REPORTS` stubs ONLY the gather turn — `deepTurn`/`quickTurn` still call the live
provider, so a full run bills real tokens. The obvious gate ("skip when there's no API key") is
BROKEN: `lib/config.py` `load_dotenv` injects the real key into `os.environ`, so a default-suite
run would find a key and quietly bill ~8 GLM turns x 35 fixtures. So the live path is gated on an
EXPLICIT `--live` / `QUIVER_LIVE_E2E=1`, mirroring `tests/test_llm_judge.py`.

The offline half is NOT a no-op: fixture discovery, the contract gate, row shaping and the
scorecard wiring are all exercised deterministically, with the model call replaced by recorded
text. That is a sanctioned seam (real parser, real scorecard, real fixtures) — not a stub that
returns success.

Run: .venv/bin/python tests/test_bench_brain.py        (offline)
     QUIVER_LIVE_E2E=1 .venv/bin/python tests/test_bench_brain.py   (bills tokens)
"""
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import bench.brain_tasks as BT   # noqa: E402
import bench.diagnostics as D    # noqa: E402

PASS = 0
FAIL = 0
LIVE = os.environ.get("QUIVER_LIVE_E2E") == "1" or "--live" in sys.argv


def ok(name, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}" + (f"  — {extra!r}" if extra is not None else ""))


def test_fixture_discovery():
    print("\n[fixture discovery]")
    fx = BT.discover_fixtures()
    ok("found the audit fixtures", len(fx) >= 30, len(fx))
    ok("every fixture has ticker+date", all(f["ticker"] and f["date"] for f in fx))
    ok("dates look like dates", all(len(f["date"]) == 10 for f in fx), fx[0] if fx else None)
    # HONESTY: the fixtures do NOT carry trend_report/macro_report, so a replay runs with two
    # channels dark. Pinned as a test so the limitation cannot be quietly forgotten.
    missing = {m for f in fx for m in f["missing_reports"]}
    ok("missing channels are reported, not hidden", len(missing) > 0, sorted(missing))
    print(f"     {len(fx)} fixtures; channels absent from every replay: {sorted(missing)}")
    ok("data_quality records the gap",
       "unavailable" in BT._row(fx[0], "Buy")["data_quality"], BT._row(fx[0], "Buy"))
    ok("unknown dir -> empty, no crash", BT.discover_fixtures("/nope/nothing") == [])


def test_contract_gate_fails_safe():
    """A truncated/garbage reply must become ERROR, never a silent bearish Hold."""
    print("\n[contract gate — unparseable must be ERROR, not Hold]")
    fx = {"path": "x", "ticker": "SMH", "date": "2026-06-07",
          "present_reports": [], "missing_reports": []}
    r_bad = BT.parse_decision(fx, "the model was cut off mid-sen")
    ok("garbage -> ERROR", r_bad["signal"] == "ERROR", r_bad)
    ok("ERROR is not Hold", r_bad["signal"] != "Hold")
    ok("says why", "contract" in r_bad["detail"], r_bad)
    ok("empty text -> ERROR", BT.parse_decision(fx, "")["signal"] == "ERROR")

    r_ok = BT.parse_decision(fx, "Analysis...\nRating: Overweight\nDone.")
    ok("valid rating parses", r_ok["signal"] == "Overweight", r_ok)
    ok("intent derived from signal", r_ok["intent"] == "buy", r_ok)
    ok("conviction NOT fabricated", r_ok["conviction"] is None, r_ok)
    r_sell = BT.parse_decision(fx, "Rating: Underweight")
    ok("bearish maps to sell intent", r_sell["intent"] == "sell", r_sell)


def test_rows_feed_the_scorecard():
    """The whole purpose: brain output must be scorable by the SAME detectors as the ledger."""
    print("\n[brain rows -> behavioral scorecard]")
    fx = {"path": "x", "ticker": "SMH", "date": "2026-06-07",
          "present_reports": [], "missing_reports": []}
    bearish = [dict(BT.parse_decision(fx, "Rating: Underweight"), id=i, trade_date=f"2026-06-{i%28+1:02d}")
               for i in range(22)]
    sc = D.scorecard(bearish, [], [])
    ok("a 0-bull brain run trips D1", "d1_signal_balance" in sc["red_flags"], sc["red_flags"])
    ok("D1 value is 0.0", sc["diagnostics"]["d1_signal_balance"]["value"] == 0.0)

    errs = [dict(BT.parse_decision(fx, "garbage"), id=i) for i in range(12)]
    sc2 = D.scorecard(errs, [], [])
    ok("an all-ERROR run trips D8", "d8_data_quality_degradation" in sc2["red_flags"], sc2["red_flags"])


def test_live_replay():
    print("\n[LIVE brain replay]")
    if not LIVE:
        # Sanctioned skip marker — tests/run_e2e.sh:71 greps for exactly four tail patterns and
        # prints a bare "OK" for anything else, which is indistinguishable from a real pass.
        print("  (skipped: needs QUIVER_LIVE_E2E=1 — this path BILLS TOKENS; "
              "deepTurn/quickTurn still call the provider under QUIVER_REPLAY_REPORTS)")
        return 2
    res = BT.run_task_set(rollouts=1, workers=2, limit=3)
    ok("live replay returned rows", len(res["rows"]) == 3, res["n_fixtures"])
    ok("every row has a signal", all(r["signal"] for r in res["rows"]))
    ok("scorecard computed", "diagnostics" in res["scorecard"])
    non_error = [r for r in res["rows"] if r["signal"] != "ERROR"]
    ok("at least one real signal", len(non_error) >= 1, [r["signal"] for r in res["rows"]])
    print(f"     signals: {[r['signal'] for r in res['rows']]}")
    return 0


def main():
    test_fixture_discovery()
    test_contract_gate_fails_safe()
    test_rows_feed_the_scorecard()
    skipped = test_live_replay()
    n_skip = 4 if skipped == 2 else 0
    print(f"\nbench-brain: {PASS} passed, {FAIL} failed, {n_skip} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
