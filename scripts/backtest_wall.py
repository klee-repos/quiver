#!/usr/bin/env python3
"""backtest_wall — CLI for the deterministic-wall replay (①).

Replays the Quiver Python wall (construct → plan → simulated fills → commit →
reflect) over a historical date range using yfinance prices and either a synthetic
SMA-cross signal or a recorded analyze.py JSONL stream, and prints an equity curve +
per-decision directional-return/alpha. It NEVER touches the broker or the live
ledger: it runs dry_run against a throwaway scratch ledger.

  .venv/bin/python scripts/backtest_wall.py --start 2024-01-02 --end 2024-06-28 \
      --starting-cash 500 --out /tmp/bt.json
  .venv/bin/python scripts/backtest_wall.py --start 2024-01-02 --end 2024-06-28 \
      --tickers SMH,URA,XLV --signals recorded.jsonl --no-rebalance

FIDELITY: optimistic same-day-close fills (no slippage/liquidity/partials); once a
day; auto_adjust prices; the SIGNAL IS AN INPUT so results say nothing about LLM edge.
This validates the SIZING/rebalance/consistency/cadence machinery, not tradeable alpha.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
import lib.wall_replay as wr  # noqa: E402
import tick  # noqa: E402


def _temp_config(strategy_path: str, rebalance: bool, per_trade: float, daily_cap: float,
                 halt_pct: float, buffer: float) -> str:
    scratch_mem = Path(tempfile.mkdtemp(prefix="wr_mem_"))
    d = {
        "account_number": "12345678", "dry_run": True,
        "kill_switch_file": str(Path(tempfile.mkdtemp(prefix="wr_kill_")) / "KILL"),
        "strategy_path": strategy_path,
        "risk": {"max_dollars_per_trade": per_trade, "daily_loss_halt_pct": halt_pct,
                 "daily_capital_deploy_cap": daily_cap, "min_buying_power_buffer": buffer,
                 "rebalance_enabled": rebalance},
        "memory": {"dir": str(scratch_mem)},
        "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
    }
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(d, p)
    p.close()
    return p.name


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic-wall replay backtest (offline, dry-run).")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (exclusive, yfinance convention)")
    ap.add_argument("--tickers", default=None, help="comma list; default = strategy book quotable names")
    ap.add_argument("--signals", default="synthetic", help="'synthetic' or a path to an analyze.py JSONL")
    ap.add_argument("--starting-cash", type=float, default=500.0)
    ap.add_argument("--strategy", default=str(REPO / "strategy.yaml"),
                    help="strategy yaml (default ./strategy.yaml; falls back to the test fixture)")
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--no-rebalance", action="store_true", help="disable the rebalance/construct pass")
    ap.add_argument("--per-trade", type=float, default=100.0)
    ap.add_argument("--daily-cap", type=float, default=1000.0)
    ap.add_argument("--halt-pct", type=float, default=20.0)
    ap.add_argument("--buffer", type=float, default=5.0)
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--out", default=None, help="write full result JSON here")
    args = ap.parse_args(argv)

    strategy_path = args.strategy
    if not Path(strategy_path).exists():
        strategy_path = str(REPO / "tests" / "fixtures" / "strategy_fixture.yaml")
        print(f"[note] {args.strategy} not found; using fixture {strategy_path}", file=sys.stderr)

    cfg_path = _temp_config(strategy_path, not args.no_rebalance, args.per_trade,
                            args.daily_cap, args.halt_pct, args.buffer)
    cfg = load_config(cfg_path)
    if not cfg.dry_run:                       # defence-in-depth: never reach the broker
        print("FATAL: config is not dry_run; refusing to run.", file=sys.stderr)
        return 2
    led = Ledger(Path(tempfile.mkdtemp(prefix="wr_ledger_")) / "ledger.db")

    # Canonical universe derivation (excludes cash sleeve + non-quotable), same helper
    # the live tick uses. led is empty here, so it falls back to strategy.yaml's book.
    book_uni = tick._analysis_universe(cfg, led)
    user = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] if args.tickers else []
    if not args.no_rebalance:
        # Rebalance targets the WHOLE book, so the book must be priced or its unpriced
        # names emit repeated unfillable orders. Always price the full book (+ any extras).
        tickers = sorted(set(book_uni) | set(user))
        if user and set(user) - set(book_uni):
            print(f"[note] rebalance on: pricing full book; extra --tickers included: "
                  f"{sorted(set(user) - set(book_uni))}", file=sys.stderr)
    else:
        tickers = user or book_uni
    if not tickers:
        print("FATAL: no tickers to backtest (empty book and no --tickers).", file=sys.stderr)
        return 2

    print(f"Fetching {len(tickers)} tickers + {args.benchmark} via yfinance "
          f"{args.start}..{args.end} ...", file=sys.stderr)
    price_map, bench_map = wr.fetch_price_history(tickers, args.start, args.end, args.benchmark)
    if not price_map:
        print("FATAL: yfinance returned no prices for the requested range.", file=sys.stderr)
        return 2
    dates, price_at, benchmark_at = wr.make_in_memory_price_fns(price_map, bench_map)

    if args.signals == "synthetic":
        signal_source = wr.sma_cross_signal_source(price_map, fast=args.fast, slow=args.slow)
        sig_label = f"synthetic sma{args.fast}/{args.slow}"
    else:
        signal_source = wr.jsonl_signal_source(args.signals)
        sig_label = f"recorded:{args.signals}"

    res = wr.run_backtest(cfg, led, dates=dates, tickers=tickers, signal_source=signal_source,
                          price_at=price_at, benchmark_at=benchmark_at,
                          starting_cash=args.starting_cash, run_construct=not args.no_rebalance,
                          config_path=cfg_path)

    s = res["summary"]
    print("=" * 72)
    print("WALL-REPLAY BACKTEST  (offline · dry-run · wall-validator, NOT a market sim)")
    print("=" * 72)
    print(f"  range        : {dates[0]} .. {dates[-1]}  ({s['n_ticks']} ticks)")
    print(f"  universe     : {', '.join(tickers)}")
    print(f"  signal       : {sig_label}   rebalance={'off' if args.no_rebalance else 'on'}")
    print(f"  start equity : ${s['start_equity']:.2f}")
    print(f"  final equity : ${s['final_equity']:.2f}")
    print(f"  total return : {s['total_return_pct']:+.2f}%   max drawdown: {s['max_drawdown_pct']:.2f}%")
    print(f"  orders       : {s['total_orders']}   decisions resolved: {s['decisions_resolved']}"
          f"   halted: {s['halted']}")
    dec = [d for d in res["decisions"] if d.get("directional_return") is not None]
    if dec:
        avg = sum(d["directional_return"] for d in dec) / len(dec)
        print(f"  avg per-decision directional return (~{5}d horizon): {avg:+.4f}  (n={len(dec)})")
    print("  NOTE: optimistic same-day-close fills; signal is an input → not an alpha estimate.")
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"  full JSON → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
