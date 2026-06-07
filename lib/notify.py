"""Render a run's digest into an email payload — pure, deterministic, no I/O.

This module is a RENDERER, not a decider. It never reads config, the ledger,
or trading limits, and never touches the network (the orchestrator sends the
result via the Resend MCP). It takes a fully-assembled ``model`` dict (built by
``tick.py report`` from already-committed ledger state + the audit reports) and
returns ``{subject, html, text, content_hash, kind}``.

``content_hash`` is the dedup key. It hashes ONLY the semantic decision skeleton
(kind, dry-run, halt, and each ticker's signal/intent/status/amount) and
deliberately EXCLUDES timestamps, equity, and rendered prose — so the same day's
repeat (no-op) wakes hash identically and never re-send, while a real change
(e.g. a halt fires) yields a new hash that sends exactly once more.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re

# Per-ticker reasoning is summarized to keep the email ~1 screen; the full
# reports live in state/analyze_logs/<date>_<ticker>.json.
_DECISION_CAP = 600
_DEBATE_CAP = 400


def summarize(text, cap: int = _DECISION_CAP) -> str:
    """Collapse whitespace and truncate to ``cap`` chars with an ellipsis."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) > cap:
        s = s[:cap].rstrip() + "…"
    return s


def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(v) -> str:
    try:
        f = float(v)
        return f"{f:+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _account_risk_line(ar: dict) -> str:
    """One human line: account-equity max drawdown + daily Sharpe (T1).

    Operator-facing context computed from the equity curve (account state) — this
    is the digest only; the agents never see account-equity metrics.
    """
    dd = ar.get("drawdown_pct")
    sh = ar.get("sharpe")
    dd_s = f"{dd:.1f}%" if dd is not None else "—"
    sh_s = f"{sh:.2f} (N={ar.get('sharpe_n', 0)})" if sh is not None else "—"
    return f"Account risk: max drawdown {dd_s}, daily Sharpe {sh_s}"


def _counts(tickers):
    nbuy = nsell = nhold = 0
    for r in tickers:
        it = (r.get("intent") or "").lower()
        if it == "buy":
            nbuy += 1
        elif it == "sell":
            nsell += 1
        else:
            nhold += 1
    return nbuy, nsell, nhold


def _traded_line(r: dict, dry_run: bool) -> str:
    """One human line describing what actually happened for a ticker."""
    status = (r.get("status") or "").lower()
    amount, qty = r.get("amount"), r.get("qty")
    intent = (r.get("intent") or "").lower()
    if status in ("hold", "skipped") or intent in ("hold", "skip"):
        return f"no trade ({r.get('detail') or intent or 'hold'})"
    if status == "blocked_guardrail":
        return f"BLOCKED — {r.get('detail') or 'review alert'}"
    if status == "error":
        return f"ERROR — {r.get('detail') or 'see logs'}"
    sized = _fmt_money(amount) if amount is not None else (f"{qty} sh" if qty is not None else "—")
    side = (r.get("side") or intent or "").lower()
    if status == "dry_run" or dry_run:
        return f"{side or 'order'} {sized} (DRY RUN — no order placed)"
    if status == "placed":
        bid = r.get("broker_order_id")
        return f"{side or 'order'} {sized} PLACED" + (f" (#{bid})" if bid else "")
    return f"{side or 'order'} {sized} ({status or 'pending'})"


def digest_hash(model: dict) -> str:
    """Stable 16-hex hash of the decision skeleton (no timestamps/equity)."""
    payload = {
        "kind": model.get("kind"),
        "dry_run": bool(model.get("dry_run")),
        "halted": bool(model.get("halted")),
        "halt_reason": model.get("halt_reason") or "",
        "event_detail": model.get("event_detail") or "",
        "tickers": [
            {
                "t": r.get("ticker"),
                "sig": r.get("signal"),
                "int": r.get("intent"),
                "st": r.get("status"),
                "amt": r.get("amount"),
                "qty": r.get("qty"),
            }
            for r in sorted(model.get("tickers", []), key=lambda x: x.get("ticker") or "")
        ],
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _subject(model: dict) -> str:
    prefix = model.get("subject_prefix") or "[Quiver]"
    date = model.get("date", "")
    kind = model.get("kind", "digest")
    if kind == "auth_error":
        return f"{prefix} {date} — ⚠ AUTH ERROR (trading stopped)"
    if kind == "halt" or model.get("halted"):
        reason = model.get("halt_reason") or "daily loss"
        return f"{prefix} {date} — \U0001f6d1 HALT ({reason})"
    nbuy, nsell, nhold = _counts(model.get("tickers", []))
    tag = " [DRY RUN]" if model.get("dry_run") else ""
    n = len(model.get("tickers", []))
    return (f"{prefix} {date} — {n} analyzed: "
            f"{nbuy} buy / {nsell} sell / {nhold} hold{tag}")


def _render_text(model: dict) -> str:
    kind = model.get("kind", "digest")
    lines = []
    mode = "DRY RUN (paper)" if model.get("dry_run") else "LIVE"
    lines.append(f"Quiver — {model.get('date', '')}  [{mode}]")
    lines.append("")

    if kind == "auth_error":
        lines.append("AUTH ERROR — the broker token was rejected; the tick stopped "
                     "without trading. Recovery: re-run /mcp to re-authenticate.")
        if model.get("event_detail"):
            lines.append("")
            lines.append(f"Detail: {summarize(model['event_detail'], 500)}")
        return "\n".join(lines)

    eq, base, drop = model.get("equity"), model.get("baseline_equity"), model.get("drop_pct")
    lines.append(f"Equity: {_fmt_money(eq)}  (baseline {_fmt_money(base)}, {_fmt_pct(drop)})")
    ar = model.get("account_risk") or {}
    if ar.get("drawdown_pct") is not None or ar.get("sharpe") is not None:
        lines.append(_account_risk_line(ar))
    if model.get("halted"):
        lines.append(f"HALT: {model.get('halt_reason') or 'daily-loss kill-switch fired'}")
    else:
        lines.append("Halt: none")
    lines.append("")

    tickers = model.get("tickers", [])
    if not tickers:
        lines.append("(no tickers analyzed this run)")
    for r in tickers:
        lines.append(f"== {r.get('ticker', '?')} — {r.get('signal', '?')} "
                     f"-> {_traded_line(r, bool(model.get('dry_run')))}")
        decision = summarize(r.get("decision"), _DECISION_CAP)
        debate = summarize(r.get("debate"), _DEBATE_CAP)
        if decision:
            lines.append(f"   Decision: {decision}")
        if debate:
            lines.append(f"   Debate:   {debate}")
        lines.append("")
    lines.append(f"Generated {model.get('now_iso', '')}")
    return "\n".join(lines)


def _esc(v) -> str:
    return _html.escape(str(v if v is not None else ""))


def _render_html(model: dict) -> str:
    kind = model.get("kind", "digest")
    dry = bool(model.get("dry_run"))
    mode = "DRY RUN (paper)" if dry else "LIVE"
    accent = "#b45309" if dry else "#047857"
    css_card = ("border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;"
                "margin:10px 0;background:#fff")
    head = (f'<div style="font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;'
            f'color:#111;max-width:680px;margin:0 auto">')
    head += (f'<h2 style="margin:0 0 2px">Quiver <span style="color:#6b7280;'
             f'font-weight:400">— {_esc(model.get("date"))}</span></h2>')
    head += (f'<div style="display:inline-block;font-weight:600;color:#fff;'
             f'background:{accent};border-radius:6px;padding:2px 8px;font-size:12px">'
             f'{_esc(mode)}</div>')

    if kind == "auth_error":
        body = ('<div style="' + css_card + ';border-color:#fca5a5;background:#fef2f2">'
                '<b>⚠ AUTH ERROR.</b> The broker token was rejected; the tick '
                'stopped without trading. Recovery: re-run <code>/mcp</code> to '
                're-authenticate.</div>')
        if model.get("event_detail"):
            body += f'<pre style="white-space:pre-wrap;color:#6b7280">{_esc(summarize(model["event_detail"], 500))}</pre>'
        return head + body + "</div>"

    eq, base, drop = model.get("equity"), model.get("baseline_equity"), model.get("drop_pct")
    body = (f'<p style="color:#374151;margin:12px 0">Equity <b>{_esc(_fmt_money(eq))}</b> '
            f'<span style="color:#6b7280">(baseline {_esc(_fmt_money(base))}, {_esc(_fmt_pct(drop))})</span></p>')
    ar = model.get("account_risk") or {}
    if ar.get("drawdown_pct") is not None or ar.get("sharpe") is not None:
        body += (f'<p style="color:#6b7280;font-size:13px;margin:-6px 0 10px">'
                 f'{_esc(_account_risk_line(ar))}</p>')
    if model.get("halted"):
        body += (f'<div style="{css_card};border-color:#fca5a5;background:#fef2f2">'
                 f'<b>\U0001f6d1 HALT:</b> {_esc(model.get("halt_reason") or "daily-loss kill-switch fired")}</div>')

    tickers = model.get("tickers", [])
    if not tickers:
        body += '<p style="color:#6b7280">(no tickers analyzed this run)</p>'
    for r in tickers:
        body += f'<div style="{css_card}">'
        body += (f'<div style="font-weight:600">{_esc(r.get("ticker", "?"))} '
                 f'<span style="color:#6b7280;font-weight:400">— {_esc(r.get("signal", "?"))}</span></div>')
        body += (f'<div style="color:{accent};font-size:13px;margin:2px 0 6px">'
                 f'{_esc(_traded_line(r, dry))}</div>')
        decision = summarize(r.get("decision"), _DECISION_CAP)
        debate = summarize(r.get("debate"), _DEBATE_CAP)
        if decision:
            body += f'<div style="color:#374151"><b>Decision.</b> {_esc(decision)}</div>'
        if debate:
            body += f'<div style="color:#6b7280;margin-top:4px"><b>Debate.</b> {_esc(debate)}</div>'
        body += "</div>"
    body += (f'<p style="color:#9ca3af;font-size:12px;margin-top:16px">'
             f'Generated {_esc(model.get("now_iso"))} · full reports in '
             f'state/analyze_logs/ · live thinking in logs/reasoning/</p>')
    return head + body + "</div>"


def build_digest(model: dict) -> dict:
    """Render ``model`` into ``{subject, html, text, content_hash, kind}``."""
    return {
        "subject": _subject(model),
        "html": _render_html(model),
        "text": _render_text(model),
        "content_hash": digest_hash(model),
        "kind": model.get("kind", "digest"),
    }
