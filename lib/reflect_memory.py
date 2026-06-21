"""Reflective memory: deterministic, ledger-grounded, proof-bearing markdown.

This is the WRITE/READ side of the risk layer. It reads ONLY the ledger
(decisions/outcomes) — never the broker or trading limits — and writes
human-readable markdown under ``state/memory/reflect/`` that the LLM agents read
on every run (use case 2 only). Every number it renders carries its formula +
actual inputs + N (via lib.risk.Metric), so the files are a hand-auditable proof.

Layout (gitignored under state/; prune-exempt):

    <dir>/portfolio.md
    <dir>/tickers/<TICKER>.md

Each ticker file is HEADER (regenerated from the ledger every write) + a
SNAPSHOTS region (an append/upsert log of one line per analyze.py run), split by
a stable ``<!-- SNAPSHOTS -->`` delimiter. Snapshot blocks are delimited by
``<!-- SNAP date=.. ticker=.. -->`` markers so re-runs upsert (never duplicate).

Robustness: this module is best-effort everywhere it's called (analyze.py write,
tick.py reflect update). The read path (safe_build_context) degrades
enriched -> plain scorecard -> "" so the agents never get LESS context than
today (D3). Bundles are computed ONCE per run and reused for both injection and
the written snapshot (D5), so the file provably matches what the agents saw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from lib import fsutil, memory, risk

DEFAULT_MEMORY_DIR = Path(__file__).resolve().parent.parent / "state" / "memory" / "reflect"

# Order metrics appear in the rendered table (stable, scannable).
_METRIC_ORDER = ["mean_return", "hit_rate", "volatility", "sharpe", "sortino",
                 "max_drawdown", "profit_factor"]

_INTRO = ("_All numbers below are Python-computed from the SQLite ledger; each shows its "
          "formula + actual inputs + sample size N, so it can be hand-audited. This is CONTEXT "
          "for reasoning ONLY — it never changes position sizing._")

_SNAP_SEP = "\n\n<!-- SNAPSHOTS -->\n\n"
_SNAP_HEADING = "## Decision snapshots (newest first; written every analyze.py run)\n\n"
_SNAP_RE = re.compile(
    r"<!-- SNAP date=(?P<date>\S+) ticker=(?P<tk>\S+) -->.*?<!-- /SNAP -->", re.DOTALL
)


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x * 100:+.1f}%" if x is not None else "n/a"


# --- metric bundle (the single compute, D5) ---------------------------------

def _metrics_for(returns: Sequence[float],
                 graded: Sequence[Tuple[Optional[str], float]],
                 cfg_memory) -> Dict[str, risk.Metric]:
    n = cfg_memory.low_confidence_min_n
    rf = cfg_memory.risk_free_rate
    ppy = cfg_memory.periods_per_year
    m = {
        "mean_return": risk.mean_return(returns, low_n=n),
        "hit_rate": risk.hit_rate(graded, low_n=n),
        "volatility": risk.volatility(returns, low_n=n),
        "sharpe": risk.sharpe(returns, rf=rf, periods_per_year=ppy, low_n=n),
        "sortino": risk.sortino(returns, rf=rf, low_n=n),
        "max_drawdown": risk.max_drawdown(returns, kind="cumret"),
    }
    m["profit_factor"] = risk.win_loss_stats(returns, low_n=n)["profit_factor"]
    return m


def _part(label: str, rows: List[dict], cfg_memory) -> dict:
    """Assemble one scope (ticker or portfolio): metrics + guidance + trend + rows.

    Metrics grade on ``memory.score_return`` (excess-vs-benchmark alpha when present,
    else absolute directional) so the risk/return numbers and the conviction
    calibration measure SKILL, not market beta. The ledger return-series already
    excludes SKIP decisions (no capital at risk), so the hit-rate is not poisoned.
    """
    scored = [(r["signal"], sr) for r in rows if (sr := memory.score_return(r)) is not None]
    returns = [sr for (_sig, sr) in scored]
    graded = scored
    th = cfg_memory.thresholds()
    metrics = _metrics_for(returns, graded, cfg_memory)
    return {
        "label": label,
        "metrics": metrics,
        "guidance": risk.derive_guidance(label, metrics, th),
        "trend": risk.rolling_window_trend(returns, graded, window=th.rolling_window),
        "rows": rows,
    }


def _ticker_part(led, ticker: str, cfg_memory) -> dict:
    return _part(ticker, led.ticker_return_series(ticker), cfg_memory)


def _portfolio_part(led, cfg_memory) -> dict:
    return _part("PORTFOLIO", led.all_return_series(), cfg_memory)


def build_metric_bundle(led, ticker: str, cfg_memory, strategy_cfg=None) -> dict:
    """Compute the per-ticker AND portfolio scopes ONCE (reused for inject + write).

    When strategy_cfg is supplied, also captures the read-only strategy/goal target
    context under bundle['target'] ONCE (decision D5), so the written decision
    snapshot reflects the same target the agents saw. Isolated: a target hiccup
    leaves bundle['target']=None and never breaks the proven metric bundle."""
    target = None
    if strategy_cfg is not None:
        try:
            import lib.strategy_context as _sctx
            target = _sctx.build_target_context(led, ticker, strategy_cfg)
        except Exception:  # noqa: BLE001
            target = None
    return {
        "ticker_label": ticker,
        "ticker": _ticker_part(led, ticker, cfg_memory),
        "portfolio": _portfolio_part(led, cfg_memory),
        "target": target,
    }


# --- rendering (one renderer, shared by READ injection and WRITE files) ------

def _render_trend(trend: dict) -> str:
    rh, ph = trend["recent_hit"], trend["prior_hit"]
    rs, ps = trend["recent_sharpe"], trend["prior_sharpe"]
    hd = trend.get("hit_delta")
    sd = trend.get("sharpe_delta")
    hd_s = f"{hd * 100:+.1f}pp" if hd is not None else "n/a"
    sd_s = f"{sd:+.3f}" if sd is not None else "n/a"
    note = f" ({trend['note']})" if trend.get("note") else ""
    return (f"Improvement trend (rolling window={trend['window']}{note}): "
            f"hit-rate recent {rh.value_str()} (N={rh.n}) vs prior {ph.value_str()} (N={ph.n}), Δ={hd_s}; "
            f"sharpe recent {rs.value_str()} (N={rs.n}) vs prior {ps.value_str()} (N={ps.n}), Δ={sd_s}.")


def render_metric_block(part: dict) -> str:
    """Markdown table of metrics (with proof) + the guidance line + the trend line.

    Identical output whether it lands in a prompt (READ) or a file (WRITE), so the
    file always matches what the agents saw.
    """
    metrics = part["metrics"]
    lines = [
        f"### {part['label']} — deterministic risk/return (from the ledger; each number shows its proof)",
        "",
        "| metric | value | N | proof (formula · inputs) |",
        "|---|---|---|---|",
    ]
    for key in _METRIC_ORDER:
        m = metrics.get(key)
        if m is None:
            continue
        proof = m.proof().replace("|", "/")
        lines.append(f"| {m.name} | {m.value_str()} | {m.n} | {proof} |")
    lines += ["", part["guidance"].text, "", _render_trend(part["trend"])]
    return "\n".join(lines)


def _resolved_outcomes_table(rows: List[dict]) -> str:
    if not rows:
        return "## Resolved outcomes\n\n_(none resolved yet)_"
    lines = [
        "## Resolved outcomes (newest first)",
        "",
        "| decision_id | trade_date | signal | directional_return | holding_days | proof |",
        "|---|---|---|---|---|---|",
    ]
    for r in reversed(rows):  # rows are oldest-first; show newest-first
        dp = r.get("decision_price")
        dr = r.get("directional_return")
        price_now = round(dp * (1 + dr), 4) if (dp and dr is not None) else "n/a"
        hd = r.get("holding_days")
        proof = (f"(price_now-decision_price)/decision_price; decision_price={dp}; "
                 f"price_now={price_now}")
        lines.append(
            f"| {r.get('id')} | {r.get('trade_date')} | {r.get('signal')} | "
            f"{_fmt_pct(dr)} | {hd if hd is not None else '?'} | {proof} |"
        )
    return "\n".join(lines)


def _header(part: dict, now_label: str) -> str:
    lines = [f"# Quiver reflective memory — {part['label']}", "", _INTRO, ""]
    if now_label:
        lines += [f"Last updated: {now_label}", ""]
    lines += [render_metric_block(part), "", _resolved_outcomes_table(part["rows"])]
    return "\n".join(lines)


# --- snapshot region (decision-time write, idempotent upsert) ----------------

def _ticker_path(base_dir, ticker: str) -> Path:
    return Path(base_dir) / "tickers" / f"{ticker}.md"


def _portfolio_path(base_dir) -> Path:
    return Path(base_dir) / "portfolio.md"


def _parse_snaps(text: str) -> List[Tuple[str, str]]:
    """Existing SNAP blocks as (date, full_block_text), document order (newest first)."""
    return [(m.group("date"), m.group(0)) for m in _SNAP_RE.finditer(text)]


def _snap_block(date: str, ticker: str, signal, decision_price, bundle: dict) -> str:
    tm = bundle["ticker"]["metrics"]
    hr, sh = tm["hit_rate"], tm["sharpe"]
    tier = bundle["ticker"]["guidance"].tier
    tgt = bundle.get("target") or {}
    goal_tok = f" | goal={tgt['goal_regime']}" if tgt.get("goal_regime") else ""
    line = (f"- {date} | signal={signal} | decision_price={decision_price} | "
            f"as-of metrics: hit_rate={hr.value_str()}(N={hr.n}), sharpe={sh.value_str()}(N={sh.n}) | "
            f"guidance={tier}{goal_tok}")
    return f"<!-- SNAP date={date} ticker={ticker} -->\n{line}\n<!-- /SNAP -->"


def _assemble(header: str, snaps: List[Tuple[str, str]]) -> str:
    body = header + _SNAP_SEP + _SNAP_HEADING
    body += "\n\n".join(blk for (_d, blk) in snaps)
    return body.rstrip() + "\n"


def write_decision_snapshot(ticker: str, date: str, *, signal, decision_price,
                            bundle: dict, base_dir, now_label: str = "") -> None:
    """Decision-time WRITE (every analyze.py run): regenerate the header from the
    bundle and idempotently upsert this (date,ticker) snapshot. Atomic.
    """
    path = _ticker_path(base_dir, ticker)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    snaps = _parse_snaps(existing)
    new_block = _snap_block(date, ticker, signal, decision_price, bundle)
    # upsert: drop any same-date block, prepend the new one (newest first)
    snaps = [(date, new_block)] + [(d, b) for (d, b) in snaps if d != date]
    fsutil.atomic_write_text(path, _assemble(_header(bundle["ticker"], now_label), snaps))


def _rewrite_ticker(path: Path, part: dict, now_label: str = "") -> None:
    """Regenerate the header, PRESERVING the existing snapshot region."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    snaps = _parse_snaps(existing)
    fsutil.atomic_write_text(path, _assemble(_header(part, now_label), snaps))


def write_portfolio(led, cfg_memory, base_dir, *, now_label: str = "") -> None:
    part = _portfolio_part(led, cfg_memory)
    lines = [f"# Quiver reflective memory — {part['label']}", "", _INTRO, ""]
    if now_label:
        lines += [f"Last updated: {now_label}", ""]
    lines += [render_metric_block(part), "", _resolved_outcomes_table(part["rows"])]
    fsutil.atomic_write_text(_portfolio_path(base_dir), "\n".join(lines).rstrip() + "\n")


def update_after_outcome(led, affected_tickers, cfg_memory, base_dir, *, now_label: str = "") -> dict:
    """Outcome-time UPDATE (tick.py reflect): recompute + rewrite each affected
    ticker file (preserving snapshots) and portfolio.md. Per-ticker failures are
    isolated so one bad file never blocks the rest (or the tick)."""
    updated = []
    for tk in sorted({t for t in affected_tickers if t}):
        try:
            part = _ticker_part(led, tk, cfg_memory)
            _rewrite_ticker(_ticker_path(base_dir, tk), part, now_label)
            updated.append(tk)
        except Exception:  # noqa: BLE001 — best-effort; isolate per ticker
            continue
    try:
        write_portfolio(led, cfg_memory, base_dir, now_label=now_label)
        port = True
    except Exception:  # noqa: BLE001
        port = False
    return {"tickers_updated": updated, "portfolio_updated": port}


def rebuild_all(led, cfg_memory, base_dir, *, now_label: str = "") -> dict:
    """Regenerate every ticker file + portfolio.md from the ledger (T2). Backfills
    existing history; preserves any existing snapshot regions."""
    rebuilt = []
    for tk in led.all_tickers_with_decisions():
        try:
            part = _ticker_part(led, tk, cfg_memory)
            _rewrite_ticker(_ticker_path(base_dir, tk), part, now_label)
            rebuilt.append(tk)
        except Exception:  # noqa: BLE001
            continue
    try:
        write_portfolio(led, cfg_memory, base_dir, now_label=now_label)
        port = True
    except Exception:  # noqa: BLE001
        port = False
    return {"tickers_rebuilt": rebuilt, "portfolio_rebuilt": port}


# --- read path: enriched context with ordered fallback (D3) ------------------

def _stance_block(led, ticker: str, *, compact: bool = False) -> str:
    """Advisory stance-history block (Component F): the recent decision SEQUENCE +
    the deterministic reversal-rate (with proof) + the no-reverse-without-a-catalyst
    contract. Lets the model SEE its own churn so it stays self-consistent. Reads
    decisions only (the analysis carve-out); never gates — the binding gate is Python."""
    rows = led.recent_decisions(ticker, 4 if compact else 8)  # newest first
    directional = [r for r in rows if r.get("intent") in ("buy", "sell")]
    if len(directional) < 2:
        return ""  # nothing to be consistent WITH yet
    intents_oldest_first = [r["intent"] for r in reversed(directional)]
    metric = risk.signal_stability(intents_oldest_first)
    seq = " <- ".join(f"{r.get('trade_date')}:{r.get('signal')}" for r in directional[:5])
    contract = ("Consistency contract: a REVERSAL of your prior stance needs a NAMED new "
                "catalyst (state it in Catalyst); absent one, hold your prior stance — the "
                "execution layer suppresses ungrounded flips.")
    return (f"Your recent stance on {ticker} (newest first): {seq}.\n"
            f"{metric.render()}\n{contract}")


def build_past_context(bundle: dict, led, ticker: str, *, compact: bool = False) -> str:
    """Compose the injectable context: existing scorecard + stance-consistency block +
    per-ticker risk block + portfolio risk block (each with proof). compact=True
    (upstream agents) trims the lists. Each block is independently isolated."""
    recent = 3 if compact else 6
    limit = 3 if compact else 8
    parts: List[str] = []
    try:
        rows = led.decisions_with_outcomes(ticker, limit=limit)
        sc = memory.build_scorecard(ticker, rows, recent=recent)
        if sc:
            parts.append(sc)
    except Exception:  # noqa: BLE001
        pass
    # Stance-consistency block — its own try/except so a hiccup never shrinks the
    # proven scorecard/risk context below.
    try:
        block = _stance_block(led, ticker, compact=compact)
        if block:
            parts.append(block)
    except Exception:  # noqa: BLE001
        pass
    for key in ("ticker", "portfolio"):
        try:
            parts.append(render_metric_block(bundle[key]))
        except Exception:  # noqa: BLE001
            pass
    # Cross-sectional 'what KIND of call wins' (T8): slice ALL resolved, position-taking
    # decisions by strategy basis (and by sleeve/sector) so the model learns which thesis
    # categories actually pay — per-ticker recall can't show this because names rotate. Read-
    # only, portfolio-wide; its own try/except so a hiccup never shrinks the proven blocks
    # above. Skipped in compact mode (upstream agents) to keep that prompt lean.
    if not compact:
        try:
            rows = led.all_return_series()
            basis_block = memory.build_sliced_scorecard(rows, "basis", "strategy basis")
            if basis_block:
                parts.append(basis_block)
            try:
                goal = led.get_active_goal()
                if goal:
                    sleeve_of = {r["ticker"]: r.get("sleeve") for r in led.active_target_portfolio(
                        goal["id"], statuses=("active", "exiting", "removed"))}
                    for r in rows:
                        r["sleeve"] = sleeve_of.get(r.get("ticker"))
                    sleeve_block = memory.build_sliced_scorecard(rows, "sleeve", "sleeve/sector")
                    if sleeve_block:
                        parts.append(sleeve_block)
            except Exception:  # noqa: BLE001 — sleeve annotation is a courtesy
                pass
        except Exception:  # noqa: BLE001 — cross-sectional slice is best-effort
            pass
    # 4th block: the read-only strategy/goal target context (decision D2). Its own
    # try/except, so a strategy hiccup can never shrink the proven scorecard/risk
    # context above. '' when there is no target (ticker not in book / layer inactive).
    try:
        import lib.strategy_context as _sctx
        block = _sctx.render_target_block(bundle.get("target"), compact=compact)
        if block:
            parts.append(block)
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(p for p in parts if p)


@dataclass
class ContextResult:
    """What analyze.py needs from one run: the two prompt variants + the bundle
    (None when we fell back) so the snapshot is written from the SAME numbers."""
    full: str
    compact: str
    bundle: Optional[dict]
    source: str   # "enriched" | "scorecard" | "empty"


def _safe_scorecard(led, ticker: str) -> str:
    try:
        return memory.scorecard(led, ticker)
    except Exception:  # noqa: BLE001
        return ""


def safe_build_context(led, ticker: str, cfg) -> ContextResult:
    """Ordered fallback (D3): enriched -> plain scorecard -> "". Never raises, never
    regresses below today's context. Computes the bundle once (D5)."""
    if not cfg.memory.enabled:
        sc = _safe_scorecard(led, ticker)
        return ContextResult(sc, sc, None, "scorecard" if sc else "empty")
    try:
        bundle = build_metric_bundle(led, ticker, cfg.memory, getattr(cfg, "strategy", None))
        full = build_past_context(bundle, led, ticker, compact=False)
        compact = build_past_context(bundle, led, ticker, compact=True)
        return ContextResult(full, compact, bundle, "enriched")
    except Exception:  # noqa: BLE001 — fall back, don't crash analysis
        sc = _safe_scorecard(led, ticker)
        return ContextResult(sc, sc, None, "scorecard" if sc else "empty")
