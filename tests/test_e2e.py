#!/usr/bin/env python3
"""End-to-end DRY-RUN simulation of one full tick — everything except the trade.

Drives the real deterministic pipeline (the same _run_* cores the CLI subcommands
call) against a temp config (dry_run=true) + a temp ledger + a SYNTHETIC broker
snapshot + SYNTHETIC analyze signals, in order:

  strategy-set -> construct -> plan(dry-run) -> commit-equivalent -> goal-track -> learn-review

It proves the stages hand off correctly AND the central safety invariant: in
dry-run, the plan produces reviewable orders but RESERVES NOTHING — zero rows in
the orders table, every ref_id None — so nothing would ever reach the broker.

Not covered here (by design): the live DeepSeek `analyze.py` signal call and the
live MCP broker execution. Run: .venv/bin/python tests/test_e2e.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import tick as T  # noqa: E402
from lib import market  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
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


def _cfg():
    d = {
        "account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/e2e_kill",
        "strategy_path": str(_REPO / "tests" / "fixtures" / "strategy_fixture.yaml"),
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 20.0,
                 "daily_capital_deploy_cap": 1000,
                 "min_buying_power_buffer": 5, "rebalance_enabled": True},
        "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"},
    }
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(d, p)
    p.close()
    return load_config(p.name)


def main() -> int:
    print("=" * 70)
    print("E2E DRY-RUN SIMULATION — one full tick, no trade execution")
    print("=" * 70)
    cfg = _cfg()
    led = Ledger(Path(tempfile.mkdtemp()) / "e2e_ledger.db")
    day = market.trading_day_et()
    now = market.now_et().isoformat()

    # --- STEP A: strategy-set (activate the 15% goal + book) ---
    print("\n[A] strategy-set — activate the goal + target book from strategy.yaml")
    ss = T._run_strategy_set(cfg, led, {"equity": 100.0, "now_iso": now})
    print(f"    goal_id={ss['goal_id']}  active_book={ss['active_book']}  holdings={ss['holdings']}")
    ok("one active goal written", led.get_active_goal() is not None)
    ok("book holdings persisted (>=10)", ss["holdings"] >= 10)

    # --- STEP B: broker snapshot (SYNTHETIC — read-only in real life) ---
    print("\n[B] broker snapshot (synthetic): equity=$100, holding $20 of XLV (overweight)")
    snapshot = {
        "equity": 100.0, "buying_power": 100.0,
        "positions": {"XLV": {"quantity": 2.0, "market_value": 20.0}},
        "quotes": {"SMH": 50.0, "IBIT": 36.0, "XLV": 10.0, "EEM": 68.0},
    }

    # --- STEP C: construct (deterministic target book today) ---
    print("\n[C] construct — what the book should look like today")
    con = T._run_construct(cfg, led, {"equity": snapshot["equity"],
                                      "positions": snapshot["positions"], "macro_reading": None})
    ok("construct proceeds with an active goal", con["proceed"] is True)
    tw = con["target_weights"]
    print(f"    active_book={con['active_book']}  recommended={con['recommended_book']}")
    for t in ("SMH", "IBIT", "SOL", "XLV", "SGOV"):
        if t in tw:
            print(f"      {t:5} -> intent={tw[t]['intent']:16} target=${tw[t]['target_dollars']}")
    ok("SMH underweight -> buy", tw["SMH"]["intent"] == "buy")
    ok("IBIT (BTC ETF) is executable -> buy", tw["IBIT"]["intent"] == "buy")
    ok("SOL (spot coin) skipped as unquotable -> held in cash", tw["SOL"]["intent"] == "skip_unquotable")
    ok("XLV overweight -> trim", tw["XLV"]["intent"] == "trim")
    ok("SGOV is the cash residual", tw["SGOV"]["intent"] == "cash_residual")

    # --- STEP D: analyze (SYNTHETIC signals — DeepSeek leg stubbed here) ---
    print("\n[D] analyze (synthetic): SMH=Buy, IBIT=Buy  (real run = analyze.py -> DeepSeek)")
    analyses = [
        {"ticker": "SMH", "signal": "Buy", "position_pct": 9.0, "rationale_summary": "synthetic"},
        {"ticker": "IBIT", "signal": "Buy", "position_pct": 4.0, "rationale_summary": "synthetic"},
    ]

    # --- STEP E: plan (DRY-RUN — reviews, reserves NOTHING) ---
    print("\n[E] plan (dry-run) — clamped orders to review; NOTHING reserved/placed")
    plan = T._run_plan(cfg, led, {**snapshot, "now_iso": now, "run_id": "E2E",
                                  "analyses": analyses, "target_weights": tw})
    ok("not halted", plan["halt"] is False)
    ok("dry_run flag is true", plan["dry_run"] is True)
    for o in plan["orders"]:
        amt = f"${o.get('dollar_amount')}" if o.get("dollar_amount") is not None else f"{o.get('quantity')} sh"
        print(f"      {o['intent']:5} {o['ticker']:5} {amt:10} kind={o.get('order_kind')}  ref_id={o['ref_id']}")
    smh = next((o for o in plan["orders"] if o["ticker"] == "SMH"), None)
    ok("SMH buy clamped to its $9 target (target binds under the $25 ceiling)",
       smh is not None and smh["dollar_amount"] == 9.0)
    ok("XLV got a clamped rebalance_trim from the overweight",
       any(o["ticker"] == "XLV" and o["order_kind"] == "rebalance_trim" for o in plan["orders"]))
    # THE central safety invariant:
    ok("DRY-RUN: every order ref_id is None (nothing authorized for the broker)",
       all(o["ref_id"] is None for o in plan["orders"]))
    ok("DRY-RUN: ZERO orders reserved in the ledger (nothing would be placed)",
       len(led.day_orders(day)) == 0)

    # --- STEP F: goal-track (best-effort progress snapshot) ---
    print("\n[F] goal-track — record progress vs the 15% glidepath")
    gt = T._run_goal_track(cfg, led)
    print(f"    recorded={gt.get('recorded')}  regime={gt.get('regime')}  "
          f"cumulative={gt.get('cumulative_return_pct')}%  alpha_vs_cash={gt.get('alpha_vs_benchmark_pct')}%")
    ok("goal snapshot recorded", gt.get("recorded") is True)

    # --- STEP G: learn-review (score holdings; propose, never auto-apply risky) ---
    print("\n[G] learn-review — score holdings vs thesis, record proposals")
    lr = T._run_learn_review(cfg, led, {"macro_reading": None})
    print(f"    reviewed={lr.get('reviewed')}  proposals={lr.get('proposals')}  "
          f"needs_approval={lr.get('needs_approval')}  auto_apply_eligible={lr.get('auto_apply_eligible')}")
    ok("learn-review ran", lr.get("reviewed") is True)
    ok("no auto-applied universe changes (human-gated by default)", lr.get("auto_apply_eligible", 0) == 0)

    print("\n" + "=" * 70)
    print(f"E2E: {PASS} checks passed, {FAIL} failed   "
          f"(execution leg = the only thing NOT exercised)")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
