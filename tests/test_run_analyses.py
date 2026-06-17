#!/usr/bin/env python3
"""Offline spec for scripts/run_analyses.py (the STEP 3 blocking fan-out) +
Ledger.count_decisions (the silent-noop guard's signal). No network / no DeepSeek:
analyze.py is stubbed via QUIVER_ANALYZE_SCRIPT, so this is fast and deterministic.

Plain asserts, prints "<n> checks passed, <m> failed", exits non-zero on any failure
(matches tests/run_e2e.sh's summary grep)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# A stub "analyzer" that mimics analyze.py's stdout contract (chatter on stderr, one
# JSON line on stdout) and branches on the ticker to exercise every failure path.
_STUB = '''\
import json, sys, time
t = sys.argv[1]
sys.stderr.write("framework chatter for %s\\n" % t)
if t == "NOOUT":      # non-zero exit, nothing on stdout
    sys.exit(1)
if t == "BADJSON":    # last stdout line is not JSON
    print("this is not json")
    sys.exit(0)
if t == "SLOW":       # sleeps longer than a tiny timeout
    time.sleep(5)
    print(json.dumps({"ticker": t, "signal": "Buy"}))
    sys.exit(0)
# normal: valid JSON, but deliberately NO "ticker" key (tests default injection)
print(json.dumps({"signal": "Buy", "position_pct": 0.1}))
'''

_PASS = 0
_FAIL = 0


def check(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {msg}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="quiver_runanalyses_"))
    stub = tmp / "stub_analyze.py"
    stub.write_text(_STUB)
    os.environ["QUIVER_ANALYZE_SCRIPT"] = str(stub)
    # PYTHON defaults to sys.executable; ANALYZE_SCRIPT read at import -> set env first.
    sys.path.insert(0, str(_REPO / "scripts"))
    import run_analyses as ra  # noqa: E402

    # --- empty input ---
    check(ra.run([]) == [], "empty ticker list -> empty result")

    # --- happy path + ordering + ERROR synthesis (parallel) ---
    res = ra.run(["AAPL", "MSFT", "BADJSON", "NOOUT"], concurrency=4, timeout=30)
    check(len(res) == 4, f"4 inputs -> 4 results (got {len(res)})")
    check(res[0].get("ticker") == "AAPL" and res[0].get("signal") == "Buy",
          f"order preserved + ticker injected for AAPL (got {res[0]})")
    check(res[1].get("ticker") == "MSFT" and res[1].get("signal") == "Buy",
          f"order preserved for MSFT (got {res[1]})")
    check(res[2].get("signal") == "ERROR" and "not JSON" in res[2].get("error", ""),
          f"non-JSON output -> ERROR (got {res[2]})")
    check(res[3].get("signal") == "ERROR" and "no stdout" in res[3].get("error", ""),
          f"no-stdout/non-zero -> ERROR (got {res[3]})")
    # Every element is a dict with a ticker (the contract plan consumes).
    check(all(isinstance(r, dict) and r.get("ticker") for r in res),
          "every result is a dict carrying its ticker")

    # --- result is JSON-serializable + round-trips (run_tick.py writes analyses.json) ---
    blob = json.dumps(res)
    back = json.loads(blob)
    check(isinstance(back, list) and len(back) == 4 and back[0]["ticker"] == "AAPL",
          "run() output round-trips through json (as written to state/tmp/analyses.json)")

    # --- per-ticker timeout becomes an ERROR datum, not a crash ---
    slow = ra.run(["SLOW"], concurrency=1, timeout=1)
    check(len(slow) == 1 and slow[0].get("signal") == "ERROR"
          and "timed out" in slow[0].get("error", ""),
          f"timeout -> ERROR datum (got {slow})")

    # --- analyze_one default-injects the ticker even when analyze.py omits it ---
    one = ra.analyze_one("NVDA", timeout=30)
    check(one.get("ticker") == "NVDA" and one.get("signal") == "Buy",
          f"analyze_one injects ticker (got {one})")

    # --- Ledger.count_decisions / count_ticker_actions (the silent-noop guard's signal) ---
    sys.path.insert(0, str(_REPO))
    from lib.ledger import Ledger  # noqa: E402
    led = Ledger(tmp / "ledger.db")
    check(led.count_decisions("2026-06-17") == 0, "fresh day -> 0 decisions")
    check(led.count_ticker_actions("2026-06-17") == 0, "fresh day -> 0 ticker_actions")
    led.record_decision(trade_date="2026-06-17", ticker="AAPL",
                        decided_at="2026-06-17T10:00:00-04:00", signal="Buy", intent="buy")
    led.record_decision(trade_date="2026-06-17", ticker="MSFT",
                        decided_at="2026-06-17T10:00:00-04:00", signal="Hold", intent="hold")
    check(led.count_decisions("2026-06-17") == 2, "two decisions recorded -> count 2")
    check(led.count_decisions("2026-06-16") == 0, "other day unaffected -> 0")

    # The DeepSeek-outage case the guard must NOT false-trip on: an ERROR analysis records
    # a ticker_action but NO decision (mirrors tick.py:418). On such a day the guard sees
    # 0 decisions but >0 actions -> sum > 0 -> no false page.
    led2 = Ledger(tmp / "ledger2.db")
    led2.record_action("2026-06-17", "NVDA", signal="ERROR", intent="skip",
                       status="error", detail="deepseek down", now_iso="2026-06-17T10:00:00-04:00")
    check(led2.count_decisions("2026-06-17") == 0, "all-ERROR day -> 0 decisions")
    check(led2.count_ticker_actions("2026-06-17") == 1, "all-ERROR day -> 1 error action")
    check(led2.count_decisions("2026-06-17") + led2.count_ticker_actions("2026-06-17") > 0,
          "all-ERROR day -> combined guard sum > 0 (no false page)")

    # --- run_tick._is_silent_noop (the run-scoped delta guard, pure) ---
    sys.path.insert(0, str(_REPO / "deploy" / "runner"))
    import run_tick as rt  # noqa: E402
    P = ["AAPL", "MSFT"]
    check(rt._is_silent_noop(P, 0, 0) is True,
          "proceeded, pending, 0 new rows -> silent noop (the 2026-06-17 bug)")
    check(rt._is_silent_noop(P, 5, 17) is False,
          "all-ERROR day: delta>0 (error actions added) -> NOT a noop")
    check(rt._is_silent_noop(P, 5, 8) is False,
          "rows added this run -> NOT a noop")
    # Intraday false-negative the review flagged: tick 2 does nothing, but tick 1 already
    # wrote rows today. Day-absolute counts would miss it; the DELTA catches it.
    check(rt._is_silent_noop(P, 5, 5) is True,
          "intraday: earlier tick wrote 5, this run added 0 -> silent noop (delta-scoped)")
    check(rt._is_silent_noop([], 0, 0) is False, "no pending -> never a noop")
    check(rt._is_silent_noop(P, None, 0) is False, "unknown before snapshot -> never false-trip")
    check(rt._is_silent_noop(P, 0, None) is False, "unknown after snapshot -> never false-trip")

    print(f"{_PASS} checks passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
