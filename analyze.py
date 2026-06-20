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
import logging
import os
import re
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent
STATE = REPO / "state"
LOGDIR = STATE / "analyze_logs"
REASONDIR = REPO / "logs" / "reasoning"
# The decision-memory ledger the past_context is read from. Defaults to the live db;
# QUIVER_LEDGER_DB points it at an isolated db (e.g. an e2e test with seeded history)
# WITHOUT touching live state. analyze.py only READS the ledger here (it writes no rows).
LEDGER_DB = Path(os.environ.get("QUIVER_LEDGER_DB") or (STATE / "ledger.db"))


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


# HTTP/SDK libraries log a line per request; at INFO they drown out the agent
# reasoning we actually want to see. Pin them to WARNING for the run.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "openai", "langsmith", "langchain")


def _install_run_logging(stream) -> logging.Handler | None:
    """Mirror all framework log records (INFO+) into ``stream`` (the reasoning tee).

    Returns the attached handler (pass it to :func:`_remove_run_logging`) or None
    if anything went wrong — logging must never break the analysis.
    """
    try:
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        return handler
    except Exception:  # noqa: BLE001 — logging must never break analysis
        return None


def _remove_run_logging(handler: logging.Handler | None) -> None:
    """Detach the run handler installed by :func:`_install_run_logging`."""
    if handler is None:
        return
    try:
        logging.getLogger().removeHandler(handler)
        handler.close()
    except Exception:  # noqa: BLE001
        pass


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
        "position_pct": grab_float("Position Pct"),
        # Consistency layer (Component A): the declared strategy/thesis tag the call
        # rests on, an optional take-profit target, and (on a reversal) the named new
        # catalyst. Python's consistency gate reads these to tell a strategy-grounded
        # reversal from a random one — it never trusts them blindly (the binding part
        # is always Python-verified against the quote or rate-limited by memory).
        "basis": grab("Strategy Basis"),
        "catalyst": grab("Catalyst"),
        "target_price": grab_float("Target Price"),
    }


def parse_pm_field_float(decision_text: str, label: str):
    """Pull a numeric '**Label**: value' field out of the PM decision markdown."""
    m = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(-?\d+(?:\.\d+)?)", decision_text)
    return float(m.group(1)) if m else None


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
        "position_pct": plan["position_pct"],   # structured sizing (% of equity); preferred over prose
        "entry_price": plan["entry_price"],
        "stop_loss": plan["stop_loss"],
        # Consistency layer (Component A): the declared strategy basis + any take-profit
        # target + named catalyst. The plan's gate reads these to distinguish a
        # strategy-grounded reversal from a random one. Optional (None when the model
        # didn't render the field) -> a basis-less reversal with no Python trigger is
        # treated as ungrounded and suppressed (fail-safe).
        "basis": plan["basis"],
        "target_price": plan["target_price"],
        "catalyst": plan["catalyst"],
        # Model-proposed re-check cadence (hours); Python clamps it. Optional.
        "next_review_hours": parse_pm_field_float(final_decision, "Next Review Hours"),
        # Conviction layer: the binding numeric conviction (0-100) the allocation
        # engine uses to size the position, plus the model's self-reported uncertainty
        # (0-100) which damps sizing. Optional -> None when the model didn't render
        # them (the allocator then falls back to a rating-implied default).
        "conviction": parse_pm_field_float(final_decision, "Conviction"),
        "uncertainty": parse_pm_field_float(final_decision, "Uncertainty"),
        # Per-decision data-completeness flags (which analyst reports were produced this
        # run). Deterministic, derived from the curated report set — NOT a model field.
        # Persisted on the decision row so a thin-data call is auditable; the allocator
        # may later damp conviction when key reports are missing.
        "data_quality": json.dumps({
            "market": bool(final_state.get("market_report")),
            "sentiment": bool(final_state.get("sentiment_report")),
            "news": bool(final_state.get("news_report")),
            "fundamentals": bool(final_state.get("fundamentals_report")),
        }),
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


def run_analysis(ticker: str, date: str, cfg, past_context: str = "",
                 past_context_compact: str = "") -> dict:
    from lib.ds_config import build_glm_config
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    ta_cfg = build_glm_config(cfg.chat_model, cfg.reasoner_model, state_dir=str(STATE))
    # Send all framework stdout noise to stderr (so our stdout stays JSON-only)
    # AND tee it to logs/reasoning/<date>_<TICKER>.log so the live thinking is
    # watchable (`tail -f`) and durable. The tee is best-effort; stdout is never
    # touched, preserving the single-JSON-line contract.
    reasoning_log = _open_reasoning_log(ticker, date)
    sink = _Tee(sys.stderr, reasoning_log)
    # The framework emits most of its progress (analyst tool calls, debate turns,
    # node transitions) via the `logging` module, which bypasses redirect_stdout.
    # Attach a handler that mirrors EVERY log record into the same tee, so the
    # per-ticker reasoning log is a complete record of the run — not just the
    # stdout chatter. Noisy HTTP libs are pinned to WARNING so the signal isn't
    # buried. Handler + level changes are reverted in `finally` (this process is
    # short-lived, but keep it clean for the test/import paths).
    log_handler = _install_run_logging(sink)
    try:
        with contextlib.redirect_stdout(sink):
            graph = TradingAgentsGraph(config=ta_cfg)
            final_state, signal = graph.propagate(
                ticker, date, past_context_override=past_context,
                past_context_compact_override=past_context_compact,
            )
    finally:
        _remove_run_logging(log_handler)
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
        # Best-effort decision memory: the ledger-derived scorecard ENRICHED with
        # deterministic risk/return metrics + guidance (lib/reflect_memory), injected
        # as past_context. safe_build_context never raises and degrades enriched ->
        # plain scorecard -> "" (D3), so a memory hiccup never blocks a fresh analysis
        # and the agents never get LESS context than the proven scorecard. The bundle
        # is computed ONCE here and reused for the written snapshot (D5).
        from lib import reflect_memory
        try:
            from lib.ledger import Ledger
            ctx = reflect_memory.safe_build_context(Ledger(LEDGER_DB), ticker, cfg)
        except Exception:  # noqa: BLE001 — even ledger-open failure must not block analysis
            ctx = reflect_memory.ContextResult("", "", None, "empty")
        result = run_analysis(ticker, date, cfg, ctx.full, ctx.compact)
        # Decision-time WRITE: append this run's snapshot + refresh the metric blocks
        # from the SAME bundle the agents saw. Best-effort; never touches stdout.
        if ctx.bundle is not None:
            try:
                reflect_memory.write_decision_snapshot(
                    ticker, date, signal=result.get("signal"),
                    decision_price=result.get("entry_price"),
                    bundle=ctx.bundle, base_dir=cfg.memory.dir, now_label=date,
                )
            except Exception:  # noqa: BLE001 — snapshot is a courtesy, never fatal
                pass
    except Exception as e:  # noqa: BLE001 — wrapper must never crash silently
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ticker": ticker, "signal": "ERROR", "error": str(e), "schema": 1}))
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
