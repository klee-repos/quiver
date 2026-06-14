#!/usr/bin/env python3
"""LIVE end-to-end test of the memory-grounded consistency feature — REAL DeepSeek.

This is the only test that exercises the FULL real path the synthetic e2e can't:
  seed a decision-memory history  ->  REAL `analyze.py` (DeepSeek multi-agent run)
  ->  confirm it emits the consistency fields (basis/catalyst/target_price)
  ->  confirm the seeded memory (incl. the stance-consistency block) is what gets injected
  ->  feed the REAL model output through the REAL `tick.py plan` gate
  ->  confirm the gate's verdict is internally consistent + the proof is persisted.

It costs money + tokens, needs DEEPSEEK_API_KEY, takes a few minutes, and the model's
SIGNAL is non-deterministic — so it is GATED behind QUIVER_LIVE_E2E=1 and asserts on
STRUCTURE + internal consistency, never on a specific signal value. Without the flag it
is a no-op PASS (safe to leave in any runner / CI).

Run:  QUIVER_LIVE_E2E=1 .venv/bin/python tests/test_e2e_live.py [TICKER]
      (ticker also via QUIVER_LIVE_E2E_TICKER; defaults to NVDA)

Everything runs against an ISOLATED temp ledger (QUIVER_LEDGER_DB) — the live db is
never touched. analyze.py only READS the ledger for memory; it writes no rows.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
PY = str(REPO / ".venv" / "bin" / "python")

PASS = 0
FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    if os.environ.get("QUIVER_LIVE_E2E") != "1":
        print("LIVE E2E skipped (set QUIVER_LIVE_E2E=1 to run the real DeepSeek path). PASS (no-op).")
        return 0
    if not (REPO / ".env").exists():
        print("LIVE E2E: no .env (need DEEPSEEK_API_KEY). FAIL.")
        return 1

    ticker = (sys.argv[1] if len(sys.argv) > 1 else
              os.environ.get("QUIVER_LIVE_E2E_TICKER", "NVDA")).strip().upper()
    date = os.environ.get("QUIVER_LIVE_E2E_DATE", "2026-06-12")
    print("=" * 72)
    print(f"LIVE E2E — REAL DeepSeek consistency path  (ticker={ticker}, date={date})")
    print("=" * 72)

    from lib.ledger import Ledger
    from lib.config import load_config
    from lib import reflect_memory, signals

    db = str(Path(tempfile.mkdtemp(prefix="quiver_live_e2e_")) / "ledger.db")
    led = Ledger(db)

    # --- seed a real prior STANCE: two resolved BUYs on this ticker (so the memory the
    # model reads is non-trivial: a scorecard + the stance-consistency block) ---
    for d, price, ret in [("2026-06-05", 120.0, 0.06), ("2026-06-09", 127.0, 0.02)]:
        did = led.record_decision(trade_date=d, ticker=ticker, decided_at=f"{d}T10:00:00-04:00",
                                  signal="Buy", intent="buy", decision_price=price,
                                  stop_loss=round(price * 0.9, 2), basis="seeded_prior_thesis")
        led.record_outcome(did, resolved_at="t", holding_days=5, directional_return=ret,
                           benchmark_return=0.01, alpha=ret - 0.01, scored_against="directional")
        led.record_event(trade_date=d, ticker=ticker, ts=f"{d}T10:00:01-04:00", signal="Buy",
                         intent="buy", status="dry_run")
    prior = led.last_completed_trade(ticker)
    ok("seed: prior completed BUY stance recorded", prior and prior["intent"] == "buy")

    # --- (i) the memory that WILL be injected carries the stance-consistency block ---
    cfg = load_config(str(REPO / "config.yaml"))
    ctx = reflect_memory.safe_build_context(led, ticker, cfg)
    ok("memory: enriched context built", ctx.source == "enriched", f"source={ctx.source}")
    ok("memory: stance-consistency block is injected",
       f"recent stance on {ticker}" in ctx.full and "stance_reversal_rate" in ctx.full
       and "NAMED new catalyst" in ctx.full)
    ok("memory: scorecard injected (prior calls + hit-rate)",
       f"prior calls on {ticker}" in ctx.full and "hit-rate" in ctx.full)

    # --- (ii) REAL analyze.py: a live DeepSeek multi-agent run on the seeded memory ---
    print(f"\n[running REAL analyze.py {ticker} — a few minutes; reading the seeded memory]")
    env = {**os.environ, "QUIVER_LEDGER_DB": db}
    proc = subprocess.run([PY, "analyze.py", ticker, "--date", date], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=1500)
    line = next((ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")), None)
    ok("analyze: produced one JSON line", line is not None, proc.stdout[-300:] + proc.stderr[-300:])
    if not line:
        print(f"\nLIVE E2E: {PASS} passed, {FAIL} failed")
        return 1
    a = json.loads(line)
    ok("analyze: a real signal (not ERROR)", a.get("signal") in signals.VALID_SIGNALS,
       f"signal={a.get('signal')} error={a.get('error')}")

    # --- (iii) the model emitted the NEW consistency fields ---
    ok("analyze: emits the `basis` key", "basis" in a)
    ok("analyze: emits the `catalyst` key", "catalyst" in a)
    ok("analyze: emits the `target_price` key", "target_price" in a)
    ok("analyze: `basis` is populated (the declared strategy tag)",
       bool(a.get("basis")), f"basis={a.get('basis')!r}")
    print(f"    -> signal={a.get('signal')}  basis={a.get('basis')!r}  "
          f"catalyst={a.get('catalyst')!r}  target_price={a.get('target_price')}")

    # --- (iv) the REAL output through the REAL plan gate; verdict is internally consistent ---
    intent, _ = signals.plan_action(a["signal"], has_position=True)
    quote = a.get("entry_price") or a.get("target_price") or 100.0
    plan_in = {"run_id": "LIVE", "now_iso": f"{date}T11:00:00-04:00", "equity": 100.0,
               "buying_power": 100.0, "positions": {ticker: {"quantity": 0.1, "market_value": 12.0}},
               "quotes": {ticker: quote}, "analyses": [a]}
    pf = Path(tempfile.mktemp(suffix=".json"))
    pf.write_text(json.dumps(plan_in), encoding="utf-8")
    penv = {**os.environ, "QUIVER_LEDGER_DB": db, "QUIVER_CONFIG": str(REPO / "config.yaml")}
    pproc = subprocess.run([PY, "tick.py", "plan", "--input", str(pf)], cwd=str(REPO), env=penv,
                           capture_output=True, text=True, timeout=120)
    pline = next((ln for ln in pproc.stdout.splitlines() if ln.strip().startswith("{")), None)
    plan = json.loads(pline) if pline else {}
    ok("plan: consumed the real analyze output", bool(plan.get("decisions")), pproc.stdout[-300:])

    rows = led.decisions_with_outcomes(ticker, limit=1)
    proof = json.loads(rows[0]["proof_json"]) if (rows and rows[0].get("proof_json")) else {}
    ok("plan: persisted a proof bundle with the consistency verdict",
       bool(proof.get("verdict")), str(proof))
    ok("plan: recorded the REAL model basis on the decision",
       rows and rows[0].get("basis") == a.get("basis"))

    # The gate verdict MUST match the actual reversal relationship + grounding (robust to
    # whatever signal the model chose). prior stance = buy.
    is_rev = signals.is_reversal("buy", intent)
    verdict = proof.get("verdict", {})
    if not is_rev:
        ok("gate: a continuation/neutral is allowed", verdict.get("allowed") is True,
           str(verdict))
    else:
        # A reversal is allowed iff grounded (a plan trigger or a basis change within budget).
        grounded = bool(proof.get("plan_trigger")) or (
            proof.get("basis_changed") and
            proof.get("recent_discretionary_reversals", 0) < proof.get("max_discretionary_reversals", 1))
        ok("gate: reversal verdict matches the grounding rule",
           verdict.get("allowed") is bool(grounded), str(proof))

    print(f"\nLIVE E2E: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
