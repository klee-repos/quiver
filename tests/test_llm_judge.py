#!/usr/bin/env python3
"""LLM-as-judge validation of every critical pipeline stage (no trade execution).

Runs each critical stage of the real pipeline, captures its actual output, and asks
an independent LLM (the headless `claude` CLI) to judge that output against a strict
rubric. This catches semantic regressions a unit assert can miss ("does the plan
output actually look safe?") with a second, reasoning set of eyes.

Slow + non-deterministic (one LLM round-trip per stage), so it is SEPARATE from the
fast offline suite. Requires the `claude` CLI on PATH. Run on demand:
  .venv/bin/python tests/test_llm_judge.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import tick as T  # noqa: E402
import deploy.runner.order_guard as order_guard  # noqa: E402
import lib.strategy_context as strategy_context  # noqa: E402
from lib import market  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
PASS = 0
FAIL = 0


def _cfg():
    d = {
        "account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/judge_kill",
        "watchlist": ["SMH"], "strategy_path": str(_REPO / "strategy.yaml"),
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 20.0,
                 "daily_capital_deploy_cap": 1000, "max_open_position_per_ticker": 50,
                 "min_buying_power_buffer": 5, "rebalance_enabled": True},
        "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"},
    }
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(d, p)
    p.close()
    return load_config(p.name)


def _tmp_led():
    return Ledger(Path(tempfile.mkdtemp()) / "judge.db")


def judge(name: str, rubric: str, data, *, timeout: int = 150) -> bool:
    """Ask the headless `claude` CLI to PASS/FAIL the stage output against the rubric."""
    global PASS, FAIL
    prompt = (
        "You are a STRICT QA auditor validating one stage of an autonomous, real-money "
        "trading bot. Evaluate whether the STAGE OUTPUT satisfies EVERY item in the RUBRIC. "
        "Be rigorous: if any single item is violated, the verdict is FAIL. Do not be lenient.\n\n"
        f"RUBRIC:\n{rubric}\n\n"
        f"STAGE OUTPUT (JSON):\n{json.dumps(data, indent=2, default=str)}\n\n"
        "Respond with EXACTLY one final line, either:\n"
        "VERDICT: PASS\n"
        "or:\n"
        "VERDICT: FAIL - <one short reason>"
    )
    try:
        p = subprocess.run(["claude", "-p", "--output-format", "text",
                            "--dangerously-skip-permissions", prompt],
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        out = "VERDICT: FAIL - judge timed out"
    line = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    up = line.upper()
    verdict = ("PASS" in up) and ("FAIL" not in up)
    if verdict:
        PASS += 1
        print(f"  ✓ [{name}] {line.strip()}")
    else:
        FAIL += 1
        print(f"  ✗ [{name}] {line.strip() or '(no verdict)'}")
    return verdict


def main() -> int:
    print("=" * 70)
    print("LLM-JUDGE PIPELINE VALIDATION (every critical stage but trade execution)")
    print("=" * 70)
    cfg = _cfg()
    led = _tmp_led()
    day = market.trading_day_et()
    now = market.now_et().isoformat()
    T._run_strategy_set(cfg, led, {"equity": 100.0, "now_iso": now})
    goal = led.get_active_goal()

    # 1) Strategy book ---------------------------------------------------------
    book = {h["ticker"]: {"sleeve": h["sleeve"], "weight": h["target_weight"],
                          "quotable": bool(h["quotable"])} for h in led.active_target_portfolio(goal["id"])}
    judge("strategy-book", (
        "1. The target weights sum to ~100 (within 1).\n"
        "2. Crypto exposure is the asset or an ETF WRAPPER (IBIT=BTC, ETHA=ETH) or a spot coin marked quotable:false; "
        "there must be NO crypto-treasury or miner stocks (MSTR, COIN, MARA, RIOT, CLSK, IREN).\n"
        "3. SGOV (the Cash sleeve) is present as the ballast."), book)

    # 2) Construct -------------------------------------------------------------
    con = T._run_construct(cfg, led, {"equity": 100.0, "positions": {"XLV": {"market_value": 20.0}},
                                      "macro_reading": None})
    judge("construct-intents", (
        "1. proceed is true.\n"
        "2. In target_weights, an unheld/underweight name (e.g. SMH) has intent 'buy'.\n"
        "3. XLV is held at $20 vs a ~$9 target, so XLV intent must be 'trim'.\n"
        "4. SOL (spot crypto, unquotable) intent must be 'skip_unquotable'.\n"
        "5. SGOV intent must be 'cash_residual'."), con)

    # 3) Plan dry-run safety ---------------------------------------------------
    plan = T._run_plan(cfg, led, {
        "equity": 100.0, "buying_power": 100.0, "now_iso": now, "run_id": "J",
        "positions": {"XLV": {"quantity": 2.0, "market_value": 20.0}},
        "quotes": {"SMH": 50.0, "IBIT": 36.0, "XLV": 10.0},
        "analyses": [{"ticker": "SMH", "signal": "Buy", "position_pct": 9.0}],
        "target_weights": con["target_weights"]})
    judge("plan-dryrun-safety", (
        "1. dry_run is true and halt is false.\n"
        "2. EVERY order has ref_id == null (nothing reserved for the broker).\n"
        "3. No buy order's dollar_amount exceeds the $25 per-trade ceiling.\n"
        "4. There is a sell order for XLV with order_kind 'rebalance_trim'."), plan)

    # 4) Halt precedence -------------------------------------------------------
    led_h = _tmp_led()
    led_h.get_or_create_baseline(day, 100.0, now)  # baseline established high
    plan_h = T._run_plan(cfg, led_h, {
        "equity": 50.0, "buying_power": 50.0, "now_iso": now, "run_id": "H",
        "positions": {}, "quotes": {},
        "analyses": [{"ticker": "SMH", "signal": "Buy", "position_pct": 9.0}],
        "target_weights": con["target_weights"]})
    judge("halt-precedence", (
        "Equity ($50) dropped >=20% below the day's baseline ($100), so:\n"
        "1. halt must be true.\n"
        "2. The orders list MUST be empty (zero orders), EVEN THOUGH target_weights were supplied. "
        "The halt must override everything."), plan_h)

    # 5) Analysis-path wall (D2) ----------------------------------------------
    block = strategy_context.render_target_block(
        strategy_context.build_target_context(led, "URA", cfg.strategy), compact=False)
    judge("analysis-wall-D2", (
        "This text is injected into the per-ticker LLM reasoning. It MUST be purely qualitative:\n"
        "1. It contains NO digit characters (0-9) anywhere.\n"
        "2. It contains NO '$' character and no dollar amount, share count, buying power, or numeric limit.\n"
        "3. It carries a disclaimer that it does NOT change sizing.\n"
        "4. It names the sleeve and a goal regime word (AHEAD/ON-TRACK/BEHIND)."),
        {"rendered_block": block})

    # 6) Order guard (D5) ------------------------------------------------------
    led.reserve_order("REF-OK", day, "SMH", side="buy", type="market",
                      dollar_amount=9.0, quantity=None, now_iso=now)
    guard = {
        "reserved_ref_id_place": list(order_guard.evaluate("place_equity_order", {"ref_id": "REF-OK"}, led)),
        "hallucinated_ref_id_place": list(order_guard.evaluate("place_equity_order", {"ref_id": "FAKE"}, led)),
        "no_ref_id_place": list(order_guard.evaluate("place_equity_order", {}, led)),
        "read_only_get_portfolio": list(order_guard.evaluate("get_portfolio", {}, led)),
    }
    judge("order-guard-D5", (
        "Each value is [allowed_bool, reason]. The guard is the last line of defense against the LLM placing "
        "an order the Python plan did not authorize:\n"
        "1. reserved_ref_id_place MUST be allowed (true).\n"
        "2. hallucinated_ref_id_place MUST be denied (false).\n"
        "3. no_ref_id_place MUST be denied (false).\n"
        "4. read_only_get_portfolio MUST be allowed (true)."), guard)

    # 7) Learn-review gating ---------------------------------------------------
    lr = T._run_learn_review(cfg, led, {"macro_reading": None})
    judge("learn-review-gating", (
        "1. reviewed is true.\n"
        "2. auto_apply_eligible MUST be 0 — risky universe changes (add/remove a whole asset) are "
        "human-gated by default and must never auto-apply."), lr)

    print("\n" + "=" * 70)
    print(f"LLM-JUDGE: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
