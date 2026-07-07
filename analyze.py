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
import time
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

from lib.rating import parse_rating  # module-level: _validate_contract (F6) uses it too


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
        # Tolerate both `**Label**: value` (legacy render) and `**Label:** value`
        # (a common LLM variant where the colon sits inside the bold). The wall
        # reads the value either way; the label presence is what matters.
        m = re.search(rf"\*\*{re.escape(label)}(?::\*\*|\*\*:)\s*(.+)", plan_text)
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
    """Pull a numeric '**Label**: value' field out of the PM decision markdown.
    Tolerates both `**Label**: N` and `**Label:** N` (colon inside the bold)."""
    m = re.search(rf"\*\*{re.escape(label)}(?::\*\*|\*\*:)\s*(-?\d+(?:\.\d+)?)", decision_text)
    return float(m.group(1)) if m else None


# Sentinels a degraded fetcher leaves in a report. A report that merely CONTAINS one of
# these (or is near-empty) is NOT usable data — treating it as present is the fail-OPEN bug.
_DATA_ERROR_SENTINELS = ("error retrieving", "no data found", "no price data",
                         "data unavailable", "dataunavailableerror", "could not retrieve",
                         "no data for")


def _report_available(report) -> bool:
    """True only when a curated analyst report is real, usable data — not empty, not an
    error sentinel. Used to decide data-quality + the fail-SAFE core-data ERROR override."""
    t = str(report or "").strip().lower()
    if len(t) < 40:                       # empty / near-empty -> unusable
        return False
    return not any(s in t for s in _DATA_ERROR_SENTINELS)


def assess_data_quality(final_state: dict) -> dict:
    """Per-decision data-completeness, sentinel-aware. CORE = market_report (prices +
    indicators): without it you cannot even size, so a missing/errored market report
    fails the analysis SAFE (signal:ERROR -> the allocator holds the prior weight, never
    trades on garbage). Fundamentals/news/sentiment missing is recorded (the calibration
    can later down-weight thin-data calls) but is NOT on its own a trade-blocker — many
    book names are ETFs with intentionally sparse fundamentals."""
    market = _report_available(final_state.get("market_report"))
    sentiment = _report_available(final_state.get("sentiment_report"))
    news = _report_available(final_state.get("news_report"))
    fundamentals = _report_available(final_state.get("fundamentals_report"))
    avail = {"market": market, "sentiment": sentiment, "news": news, "fundamentals": fundamentals}
    return {
        **avail,
        "core_available": market,
        "sources_unavailable": sorted(k for k, ok in avail.items() if not ok),
    }


def extract_fields(final_state: dict, signal: str, ticker: str) -> dict:
    plan_text = str(final_state.get("trader_investment_plan") or "")
    plan = parse_trader_plan(plan_text)
    final_decision = str(final_state.get("final_trade_decision") or "")
    summary = re.sub(r"\s+", " ", final_decision).strip()[:600]
    dq = assess_data_quality(final_state)
    # FAIL SAFE: if the CORE market/price data was unavailable this run, refuse to emit a
    # tradable signal no matter what the model said — downgrade to ERROR so the execution
    # layer records a skip and the allocator holds the prior weight (never trades on garbage
    # data dressed up as a confident Buy/Sell). Records why, for the audit trail.
    out_signal, out_error = signal, None
    if not dq["core_available"]:
        out_signal = "ERROR"
        out_error = f"core data unavailable: missing {','.join(dq['sources_unavailable']) or 'market'}"
    fields = {
        "ticker": ticker,
        "signal": out_signal,  # deterministic: Buy/Overweight/Hold/Underweight/Sell (or ERROR on bad data)
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
        # Per-decision data-completeness flags (sentinel-aware: a report containing an
        # "Error retrieving…" sentinel or near-empty counts as UNAVAILABLE, not present).
        # Deterministic, derived from the curated report set — NOT a model field. Persisted
        # on the decision row so a thin-data call is auditable; the allocator may damp
        # conviction / hold prior when key reports are missing.
        "data_quality": json.dumps(dq),
        "error": out_error,   # populated only on the fail-SAFE core-data ERROR override
        "rationale_summary": summary,
        "schema": 1,
    }
    return fields


def _dump_full_state(final_state: dict, ticker: str, date: str) -> None:
    """Audit dump of the curated report set (default=str handles any objects)."""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    keys = [
        "company_of_interest", "trade_date", "market_report", "sentiment_report",
        "news_report", "fundamentals_report", "trend_report", "investment_plan",
        "trader_investment_plan", "final_trade_decision",
    ]
    safe = {k: final_state.get(k) for k in keys if k in final_state}
    path = LOGDIR / f"{date}_{ticker}.json"
    path.write_text(json.dumps(safe, indent=2, default=str), encoding="utf-8")


# --- EVE brain (the migration path) -----------------------------------------
# EVE (eve.dev) is a TypeScript/Node agent framework with no Python SDK. The
# brain is invoked as a subprocess (`eve run` in quiver_eve/), passed the
# read-only past_context via a temp file, and returns its final answer as
# markdown on stdout. The markdown carries the SAME `**Label**: value` lines the
# legacy framework rendered, so extract_fields/parse_trader_plan are reused
# unchanged. The wall is untouched: this process still DECIDES and never touches
# the broker / limits; EVE is the brain only.

# The separable `## section` headers EVE emits (F1 contract) -> final_state keys.
# F3: trend_report added (optional long-horizon guidepost block; missing -> the
# splitter just won't have it, never an ERROR — only core market data gates).
_EVE_SECTIONS = (
    "market_report", "sentiment_report", "news_report", "fundamentals_report",
    "trend_report", "trader_investment_plan", "final_trade_decision", "lever_proposals",
)

# F6: the 12 contract labels. The Python-side gate is BINDING (the TS validator
# is dead code in the live path — decide.mjs never imports it). Ported from
# quiver_eve/test/contract.test.mjs (agent.ts does NOT export REQUIRED_LABELS;
# validate.ts:8's import is broken, so this Python list is the source of truth).
_CONTRACT_LABELS = (
    # trader_investment_plan (8)
    "Action", "Entry Price", "Stop Loss", "Position Sizing", "Position Pct",
    "Strategy Basis", "Catalyst", "Target Price",
    # final_trade_decision (4)
    "Rating", "Next Review Hours", "Conviction", "Uncertainty",
)


def _validate_contract(pseudo: dict) -> None:
    """F6 binding fail-safe gate. Asserts the 12 contract labels are PRESENT in
    the assembled markdown AND ``**Strategy Basis**`` is non-empty — then a
    multi-turn brain (F4) cannot emit a half-formed PM decision that passes the
    section-only check at line 363 and yields a tradable signal. Uses
    parse_trader_plan's ``grab`` regex (the ``(?::\\*\\*|\\*\\*:)`` alternation)
    which accepts BOTH ``**Label**: v`` and ``**Label:** v`` — the common LLM
    variant. MUST NOT use validate.ts's regex (``:?\\*\\*:``) which REJECTS the
    colon-inside-bold form and would make every ticker ERROR (a unit test pins
    this). Raises RuntimeError -> _run_eve maps to signal:ERROR (skip)."""
    plan_md = str(pseudo.get("trader_investment_plan") or "")
    pm_md = str(pseudo.get("final_trade_decision") or "")
    # Strategy Basis non-empty AND not a sentinel (none/null/n/a) — mirroring
    # validate.ts's empty_basis check. parse_trader_plan.grab returns the raw
    # text; "none" is a non-empty string but is the model saying "no basis",
    # which the consistency gate must treat as ungrounded (fail-safe).
    basis = (parse_trader_plan(plan_md).get("basis") or "").strip()
    if not basis or re.match(r"^(none|null|n/a)$", basis, re.IGNORECASE):
        raise RuntimeError(
            "contract violation: **Strategy Basis** missing or empty")
    missing = []
    for label in _CONTRACT_LABELS:
        # grab's regex (analyze.py:127): \*\*LABEL(?::\*\*|\*\*:)\s*(.+)
        m = re.search(rf"\*\*{re.escape(label)}(?::\*\*|\*\*:)\s*(.+)", plan_md + "\n" + pm_md)
        if not m:
            missing.append(label)
    if missing:
        raise RuntimeError(
            f"contract violation: missing labels {missing}")
    rating = parse_rating(pm_md)
    if rating is None or rating == "ERROR":
        raise RuntimeError(f"contract violation: no valid **Rating** (got {rating!r})")


def _split_eve_markdown(md: str) -> dict:
    """Split EVE's stdout markdown into a pseudo-final_state dict the existing
    extract_fields() can consume. Each `## <section>` becomes a final_state key
    with the section body (the labels are parsed downstream by regex). Unknown
    sections are ignored; everything before the first `##` is the preamble.
    """
    out = {}
    cur_key = None
    cur_lines = []
    for line in md.splitlines():
        m = re.match(r"^##\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if m:
            if cur_key in _EVE_SECTIONS:
                out[cur_key] = "\n".join(cur_lines).strip()
            cur_key = m.group(1)
            cur_lines = []
        elif cur_key in _EVE_SECTIONS:
            cur_lines.append(line)
    if cur_key in _EVE_SECTIONS:
        out[cur_key] = "\n".join(cur_lines).strip()
    return out


def _run_eve(ticker: str, date: str, cfg, past_context: str,
             past_context_compact: str, sink) -> dict:
    """Drive the EVE brain (a standalone Node script) and return a pseudo-final_state.

    The brain is `quiver_eve/run/decide.mjs` — a plain Node script that uses the ai
    SDK + OpenRouter to run GLM-5.2 with the data tools in-process, prints the final
    contract markdown to stdout. We pass the read-only past_context (memory scorecard
    — never limits/broker) via stdin, ticker/date via argv, and collect stdout.

    Why a subprocess script (not the `eve dev` HTTP server): EVE's stream proxy stalls
    on long generations; the ai SDK + OpenRouter stream fine in-process, and a plain
    `node decide.mjs` per ticker is simpler for the orchestrator's subprocess seam.
    stderr is tee'd into `sink` so logs/reasoning/<date>_<TICKER>.log stays live.

    Raises on any failure (non-zero exit / no stdout / missing decision sections) so
    the caller maps to an ERROR signal (fail-safe).
    """
    import subprocess
    import threading

    eve_dir = REPO / (getattr(cfg, "eve_dir", "quiver_eve") or "quiver_eve")
    script = eve_dir / "run" / "decide.mjs"
    if not script.is_file():
        raise RuntimeError(f"EVE brain script not found: {script}")

    env = dict(os.environ)
    env.setdefault("QUIVER_REPO", str(REPO))
    env.setdefault("QUIVER_PYTHON", str(REPO / ".venv" / "bin" / "python"))
    env.setdefault("QUIVER_DATA_HELPER", str(eve_dir / "run" / "quill_data.py"))
    # F4: the multi-agent debate round count (bull/bear + risk-debate per round).
    env.setdefault("QUIVER_RESEARCH_ROUNDS", str(getattr(cfg, "research_rounds", 1)))
    # Pipe the past_context in via stdin (argv-length-safe).
    ctx_stdin = (past_context or "").encode("utf-8")
    try:
        proc = subprocess.Popen(
            ["node", str(script), ticker, date],
            cwd=str(eve_dir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"node not found (is Node 24+ installed?): {e}")

    # Stream stderr into the tee so the reasoning log is live (tail -f), and
    # capture the REASONING_TOKENS: marker decide.mjs emits (the provider-reported
    # thinking-ON count — survivor #4 gate).
    reasoning_tokens = [0]
    def _drain_stderr():
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace")
                sink.write(line)
                m = re.search(r"REASONING_TOKENS:\s*(\d+)", line)
                if m:
                    # F4 (Gate-B): SUM across deep turns (not last-write-wins) so a
                    # Step-6 throw after a Step-3 count still leaves a nonzero total
                    # (honest thinking-ON proof). decide.mjs emits per deep turn in a
                    # finally so a crash still surfaces its count.
                    reasoning_tokens[0] += int(m.group(1))
        except Exception:  # noqa: BLE001 — the log is best-effort
            pass
    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()
    md, _ = proc.communicate(input=ctx_stdin, timeout=getattr(cfg, "analyze_timeout_sec", 3600))
    t.join(timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(f"EVE brain (decide.mjs) exited {proc.returncode}")
    md = md.decode("utf-8", errors="replace")
    if not md.strip():
        raise RuntimeError("EVE brain returned no markdown on stdout")
    pseudo = _split_eve_markdown(md)
    if not pseudo.get("final_trade_decision") or not pseudo.get("trader_investment_plan"):
        raise RuntimeError("EVE output missing final_trade_decision/trader_investment_plan sections")
    _validate_contract(pseudo)  # F6: binding fail-safe gate (12 labels + non-empty basis)
    pseudo["_reasoning_tokens"] = reasoning_tokens[0]
    return pseudo


def run_analysis(ticker: str, date: str, cfg, past_context: str = "",
                 past_context_compact: str = "") -> dict:
    # The brain engine. F8 deleted the legacy tradingagents/ framework; EVE
    # (quiver_eve/run/decide.mjs, GLM-5.2 via OpenRouter) is the only brain.
    # A stale config saying brain.engine: tradingagents fails LOUD here (not
    # silent) so a half-deployed box never trades blind.
    engine = (getattr(cfg, "brain_engine", "eve") or "eve").strip().lower()
    if engine != "eve":
        raise RuntimeError(
            f"brain.engine={engine!r} but tradingagents/ was removed (F8). "
            "Set brain.engine: eve (the EVE brain) in config.yaml."
        )
    return _run_eve_analysis(ticker, date, cfg, past_context, past_context_compact)


def _run_eve_analysis(ticker: str, date: str, cfg, past_context: str,
                      past_context_compact: str) -> dict:
    """The EVE path: spawn the brain subprocess, build a pseudo-final_state from
    its markdown, derive the 5-tier signal via lib.rating.parse_rating (the
    **Rating**: label), and feed the SAME extract_fields the legacy path uses.
    The wall is untouched; EVE is the brain only.
    """
    from lib.rating import parse_rating

    reasoning_log = _open_reasoning_log(ticker, date)
    sink = _Tee(sys.stderr, reasoning_log)
    sink.write(f"[eve] brain=eve ticker={ticker} date={date}\n")
    try:
        final_state = _run_eve(ticker, date, cfg, past_context, past_context_compact, sink)
        # Derive the 5-tier signal from the PM decision's **Rating**: label, the
        # same deterministic heuristic the legacy framework used internally
        # (parse_rating, ported to lib/rating.py so it survives tradingagents/
        # deletion). WITHOUT this, extract_fields gets signal=None -> plan_action
        # skips every ticker -> the bot silently stops trading.
        signal = parse_rating(str(final_state.get("final_trade_decision") or ""))
    finally:
        if reasoning_log is not None:
            try:
                reasoning_log.close()
            except Exception:  # noqa: BLE001
                pass
    _dump_full_state(final_state, ticker, date)
    out = extract_fields(final_state, signal, ticker)
    # Thinking-mode regression guard (survivor #4): surface the provider-reported
    # reasoning TOKEN count so a silent thinking-OFF regression (an ai-SDK upgrade
    # dropping reasoning) is observable on the decision row + caught by the live
    # e2e. The @ai-sdk/openai .chat() provider doesn't map OpenRouter's `reasoning`
    # content into the V4 reasoning-text channel, but it DOES report the count in
    # usage.outputTokenDetails.reasoningTokens — decide.mjs emits that as
    # `REASONING_TOKENS: N` on stderr, captured by _run_eve. >0 = GLM reasoned.
    out["reasoning_content_len"] = int(final_state.get("_reasoning_tokens") or 0)
    return out


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
