#!/usr/bin/env python3
"""End-to-end test of the consistency layer by ACTUALLY RUNNING the tick.py CLI.

Unlike test_units.py (pure functions) this drives the REAL `tick.py` subcommands as
SUBPROCESSES — the exact CLI the orchestrator invokes — against an ISOLATED temp
ledger + temp config (via the QUIVER_LEDGER_DB / QUIVER_CONFIG env overrides), so it
never touches live trading state. It proves the memory-grounded strategy-consistency
gate, the regime confirmation/hysteresis, the strategic change-log, and the decision
proof all work through the real process boundary, not just in-process.

Run: .venv/bin/python tests/test_e2e_consistency.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
FIXTURE = REPO / "tests" / "fixtures" / "strategy_fixture.yaml"

PASS = 0
FAIL = 0


def ok(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}")


class Box:
    """An isolated tick.py environment: temp DB + temp config + temp strategy file."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="quiver_e2e_"))
        self.db = self.dir / "ledger.db"
        self.strategy = self.dir / "strategy.yaml"
        self.strategy.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        self.config = self.dir / "config.yaml"
        self._write_config()

    def _write_config(self):
        # dry_run TRUE (reviews, reserves nothing); consistency gate ON; rebalance ON so
        # construct/learn-review run. account_number satisfies the loader.
        self.config.write_text(
            "schema: 1\n"
            "dry_run: true\n"
            "kill_switch_file: KILL_E2E\n"
            f"strategy_path: {self.strategy}\n"
            "risk:\n"
            "  max_dollars_per_trade: 25\n"
            "  daily_loss_halt_pct: 50.0\n"
            "  daily_capital_deploy_cap: 1000\n"
            "  min_buying_power_buffer: 5\n"
            "  rebalance_enabled: true\n"
            "  consistency:\n"
            "    enabled: true\n"
            "    max_discretionary_reversals: 1\n"
            "    flip_window: 6\n"
            "    loss_catalyst_pct: 8.0\n"
            "glm:\n"
            "  chat_model: glm-5.2\n"
            "  reasoner_model: glm-5.2\n"
            "notify:\n  enabled: false\n",
            encoding="utf-8",
        )

    def run(self, *args, stdin_json=None):
        """Run `tick.py <args>` as a subprocess; return the parsed last stdout JSON line."""
        env = dict(os.environ)
        env["QUIVER_LEDGER_DB"] = str(self.db)
        env["QUIVER_CONFIG"] = str(self.config)
        env["RH_ACCOUNT_NUMBER"] = "12345678"
        env.pop("NOTIFY_TO", None)
        inp = None
        if stdin_json is not None:
            f = self.dir / "in.json"
            f.write_text(json.dumps(stdin_json), encoding="utf-8")
            args = (*args, "--input", str(f))
        proc = subprocess.run([PY, "tick.py", *args], cwd=str(REPO), env=env,
                              input=inp, capture_output=True, text=True, timeout=120)
        line = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
        if not line:
            raise AssertionError(f"no JSON from `tick.py {' '.join(args)}`:\n{proc.stdout}\n{proc.stderr}")
        return json.loads(line[-1])

    def set_strategy_weight(self, ticker, new_weight):
        """Rewrite the temp strategy file changing one holding's weight (keeps sum via SGOV)."""
        import yaml
        data = yaml.safe_load(self.strategy.read_text(encoding="utf-8"))
        default = next(n for n, b in data["books"].items() if b.get("default"))
        holdings = data["books"][default]["holdings"]
        old = None
        for h in holdings:
            if h["ticker"].upper() == ticker.upper():
                old = h["weight"]
                h["weight"] = new_weight
        # rebalance the delta into the cash sleeve so the book still sums to ~100
        if old is not None:
            for h in holdings:
                if h.get("sleeve") == "Cash":
                    h["weight"] = round(h["weight"] + (old - new_weight), 4)
        self.strategy.write_text(yaml.safe_dump(data), encoding="utf-8")


def _commit_buy(box, ticker):
    """Record a COMPLETED dry-run buy via the real commit subcommand."""
    return box.run("commit", stdin_json={
        "ticker": ticker, "signal": "Buy", "intent": "buy", "status": "dry_run",
        "detail": "e2e seed buy", "run_id": "E2E-BUY"})


def main() -> int:
    print("=" * 70)
    print("E2E CONSISTENCY — driving the REAL tick.py CLI (isolated temp state)")
    print("=" * 70)
    box = Box()

    # --- A) strategy-set: activate the goal so construct/learn-review run ---
    print("\n[A] strategy-set (real subprocess)")
    ss = box.run("strategy-set", stdin_json={"equity": 100.0, "now_iso": "2026-06-13T10:00:00-04:00"})
    ok("strategy-set seeded a goal", ss.get("goal_id") is not None)

    # --- B) Day-1 BUY through plan + commit (records the prior stance) ---
    print("\n[B] BUY AAPL via plan, then commit it (completed trade)")
    plan_buy = box.run("plan", stdin_json={
        "run_id": "E2E1", "now_iso": "2026-06-13T11:00:00-04:00", "equity": 100.0,
        "buying_power": 100.0, "positions": {}, "quotes": {"AAPL": 100.0},
        "analyses": [{"ticker": "AAPL", "signal": "Buy", "position_pct": 10.0,
                      "entry_price": 100.0, "stop_loss": 90.0, "basis": "momentum_breakout"}]})
    ok("plan emitted a BUY order for AAPL",
       any(o["ticker"] == "AAPL" and o["intent"] == "buy" for o in plan_buy["orders"]))
    _commit_buy(box, "AAPL")

    # --- C) RANDOM reversal: SELL with no basis, quote ABOVE stop -> SUPPRESSED ---
    print("\n[C] random SELL (no catalyst, above stop) -> suppressed")
    plan_rev = box.run("plan", stdin_json={
        "run_id": "E2E2", "now_iso": "2026-06-13T12:00:00-04:00", "equity": 100.0,
        "buying_power": 100.0, "positions": {"AAPL": {"quantity": 1.0, "market_value": 105.0}},
        "quotes": {"AAPL": 105.0}, "analyses": [{"ticker": "AAPL", "signal": "Sell"}]})
    ok("NO sell order placed for a random reversal",
       not any(o["ticker"] == "AAPL" for o in plan_rev["orders"]))
    ok("decision recorded as consistency:ungrounded_reversal",
       any(d.get("detail") == "consistency:ungrounded_reversal" for d in plan_rev["decisions"]))

    # --- D) GROUNDED reversal via plan trigger: SELL with quote BELOW stop -> ALLOWED ---
    print("\n[D] stop-hit SELL (quote below recorded stop) -> allowed")
    plan_stop = box.run("plan", stdin_json={
        "run_id": "E2E3", "now_iso": "2026-06-13T12:30:00-04:00", "equity": 100.0,
        "buying_power": 100.0, "positions": {"AAPL": {"quantity": 1.0, "market_value": 85.0}},
        "quotes": {"AAPL": 85.0}, "analyses": [{"ticker": "AAPL", "signal": "Sell"}]})
    ok("stop-hit SELL IS allowed (executes the recorded plan)",
       any(o["ticker"] == "AAPL" and o["intent"] == "sell" for o in plan_stop["orders"]))

    # --- E) GROUNDED reversal via a named catalyst -> ALLOWED ---
    print("\n[E] basis-change SELL (named catalyst) -> allowed")
    plan_basis = box.run("plan", stdin_json={
        "run_id": "E2E4", "now_iso": "2026-06-13T13:00:00-04:00", "equity": 100.0,
        "buying_power": 100.0, "positions": {"AAPL": {"quantity": 1.0, "market_value": 105.0}},
        "quotes": {"AAPL": 105.0},
        "analyses": [{"ticker": "AAPL", "signal": "Sell", "basis": "thesis_broken_earnings_miss"}]})
    ok("basis-change SELL IS allowed (named catalyst)",
       any(o["ticker"] == "AAPL" for o in plan_basis["orders"]))

    # --- F) decision-proof: every decision carries an auditable proof bundle ---
    print("\n[F] decision-proof renders the consistency proof")
    dp = box.run("decision-proof", "--id", "1")
    ok("decision-proof returns a stored proof bundle", dp.get("ok") and dp.get("proof") is not None)
    ok("proof records the consistency verdict",
       isinstance(dp["proof"], dict) and "verdict" in dp["proof"])

    # --- G) regime hysteresis: a standdown reading must be CONFIRMED before it flips ---
    # construct is the in-runbook confirmer; one noisy print must NOT flip the posture.
    print("\n[G] regime confirmation via construct: one flip, not two, across two readings")
    sd = {"equity": 100.0, "positions": {}, "macro_reading": {"core_pce_pct": 9.9, "fed_hike": True}}
    box.run("construct", stdin_json={**sd, "now_iso": "2026-06-13T14:00:00-04:00"})
    h1 = box.run("strategy-history", "--type", "regime")
    ok("1st standdown reading does NOT flip the regime (no change logged)", h1.get("count") == 0)
    box.run("construct", stdin_json={**sd, "now_iso": "2026-06-14T14:00:00-04:00"})
    hist = box.run("strategy-history", "--type", "regime")
    ok("2nd consecutive reading CONFIRMS the flip (one change logged)", hist.get("count") == 1)
    ok("the regime change is HOLD->STAND_DOWN",
       bool(hist["changes"]) and hist["changes"][0]["from_value"] == "HOLD"
       and hist["changes"][0]["to_value"] == "STAND_DOWN")

    # --- H) strategy-set diff: a changed weight is captured in the change-log ---
    print("\n[H] strategy-set diff logs a changed target weight")
    box.set_strategy_weight("SMH", 12.0)  # was 9 in the fixture
    box.run("strategy-set", stdin_json={"equity": 100.0, "now_iso": "2026-06-14T10:00:00-04:00"})
    whist = box.run("strategy-history", "--type", "weight")
    ok("strategy-history captured the SMH weight change",
       any(c["ticker"] == "SMH" for c in whist.get("changes", [])))

    print("\n" + "=" * 70)
    print(f"E2E CONSISTENCY: {PASS} checks passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
