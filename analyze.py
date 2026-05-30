#!/usr/bin/env python3
"""Quiver decision wrapper.

Runs ONE TradingAgents analysis for a ticker and prints exactly one line of
JSON to stdout. This process DECIDES; it never touches the broker and never
reads trading limits. The orchestrator parses the single JSON line.

Usage:
    .venv/bin/python analyze.py AAPL [--date YYYY-MM-DD]

All framework chatter is redirected to stderr so stdout carries only the JSON.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent
STATE = REPO / "state"
LOGDIR = STATE / "analyze_logs"
REASONDIR = REPO / "logs" / "reasoning"


class _Tee:
    """Fan stdout writes to a primary stream (always) + a best-effort file.

    The primary is the real stderr (must always work); the secondary is the
    per-ticker reasoning log. The secondary NEVER raises out — if the log file
    is unwritable we silently keep streaming to stderr, so the wrapper can't be
    broken by a logging hiccup. stdout is untouched, so the single-JSON-line
    contract holds.
    """

    def __init__(self, primary, secondary):
        self._primary = primary
        self._secondary = secondary

    def write(self, data):
        n = self._primary.write(data)
        if self._secondary is not None:
            try:
                self._secondary.write(data)
            except Exception:  # noqa: BLE001 — logging must never break analysis
                pass
        return n

    def flush(self):
        for s in (self._primary, self._secondary):
            if s is None:
                continue
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass


def _open_reasoning_log(ticker: str, date: str):
    """Open logs/reasoning/<date>_<TICKER>.log for the live chatter, or None."""
    try:
        REASONDIR.mkdir(parents=True, exist_ok=True)
        return open(REASONDIR / f"{date}_{ticker}.log", "w", encoding="utf-8")
    except Exception:  # noqa: BLE001 — best-effort; fall back to stderr-only
        return None


def parse_trader_plan(plan_text: str) -> dict:
    """Pull fields out of the rendered TraderProposal markdown.

    The framework renders lines like '**Entry Price**: 195.1',
    '**Stop Loss**: 182.5', '**Position Sizing**: ...', '**Action**: Buy'.
    """
    def grab(label: str):
        m = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(.+)", plan_text)
        return m.group(1).strip() if m else None

    def grab_float(label: str):
        v = grab(label)
        if not v:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
        return float(m.group()) if m else None

    return {
        "action": (grab("Action") or None),
        "entry_price": grab_float("Entry Price"),
        "stop_loss": grab_float("Stop Loss"),
        "position_sizing": grab("Position Sizing"),
    }


def extract_fields(final_state: dict, signal: str, ticker: str) -> dict:
    plan_text = str(final_state.get("trader_investment_plan") or "")
    plan = parse_trader_plan(plan_text)
    final_decision = str(final_state.get("final_trade_decision") or "")
    summary = re.sub(r"\s+", " ", final_decision).strip()[:600]
    return {
        "ticker": ticker,
        "signal": signal,  # deterministic: Buy/Overweight/Hold/Underweight/Sell
        "action": plan["action"],
        "position_sizing": plan["position_sizing"],
        "entry_price": plan["entry_price"],
        "stop_loss": plan["stop_loss"],
        "rationale_summary": summary,
        "schema": 1,
    }


def _dump_full_state(final_state: dict, ticker: str, date: str) -> None:
    """Audit dump of the curated report set (default=str handles any objects)."""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    keys = [
        "company_of_interest", "trade_date", "market_report", "sentiment_report",
        "news_report", "fundamentals_report", "investment_plan",
        "trader_investment_plan", "final_trade_decision",
    ]
    safe = {k: final_state.get(k) for k in keys if k in final_state}
    path = LOGDIR / f"{date}_{ticker}.json"
    path.write_text(json.dumps(safe, indent=2, default=str), encoding="utf-8")


def run_analysis(ticker: str, date: str, cfg, past_context: str = "") -> dict:
    from lib.ds_config import build_deepseek_config
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    ta_cfg = build_deepseek_config(cfg.chat_model, cfg.reasoner_model, state_dir=str(STATE))
    # Send all framework stdout noise to stderr (so our stdout stays JSON-only)
    # AND tee it to logs/reasoning/<date>_<TICKER>.log so the live thinking is
    # watchable (`tail -f`) and durable. The tee is best-effort; stdout is never
    # touched, preserving the single-JSON-line contract.
    reasoning_log = _open_reasoning_log(ticker, date)
    try:
        sink = _Tee(sys.stderr, reasoning_log)
        with contextlib.redirect_stdout(sink):
            graph = TradingAgentsGraph(config=ta_cfg)
            final_state, signal = graph.propagate(ticker, date, past_context_override=past_context)
    finally:
        if reasoning_log is not None:
            try:
                reasoning_log.close()
            except Exception:  # noqa: BLE001
                pass
    _dump_full_state(final_state, ticker, date)
    return extract_fields(final_state, signal, ticker)


def main(argv) -> int:
    sys.path.insert(0, str(REPO))  # make `lib` importable when run as a script
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    from lib.config import load_config
    from lib.market import trading_day_et

    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--date", default=None)
    args = ap.parse_args(argv)
    ticker = args.ticker.strip().upper()
    date = args.date or trading_day_et()

    try:
        cfg = load_config(REPO / "config.yaml")
        # Best-effort decision memory: a ledger-derived scorecard of past calls +
        # their real outcomes, injected into the analysis as past_context. Read-only
        # and never fatal — a memory hiccup must not block a fresh analysis.
        past_context = ""
        try:
            from lib.ledger import Ledger
            from lib.memory import scorecard
            past_context = scorecard(Ledger(STATE / "ledger.db"), ticker)
        except Exception:  # noqa: BLE001 — memory is best-effort, never blocks analysis
            past_context = ""
        result = run_analysis(ticker, date, cfg, past_context)
    except Exception as e:  # noqa: BLE001 — wrapper must never crash silently
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ticker": ticker, "signal": "ERROR", "error": str(e), "schema": 1}))
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
