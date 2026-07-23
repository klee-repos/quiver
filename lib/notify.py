"""Render a run's digest into an email payload — pure, deterministic, no I/O.

This module is a RENDERER, not a decider. It never reads config, the ledger,
or trading limits, and never touches the network (the orchestrator sends the
result via the Resend MCP). It takes a fully-assembled ``model`` dict (built by
``tick.py report`` from already-committed ledger state + the audit reports) and
returns ``{subject, html, text, content_hash, kind}``.

``content_hash`` is the dedup key. It hashes ONLY the semantic decision skeleton
(kind, dry-run, halt + halt_reason, event_detail — a constant "" for real digests —
and each ticker's signal/intent/status/amount/qty) and deliberately EXCLUDES
timestamps, equity, and rendered prose — so the same day's repeat (no-op) wakes hash
identically and never re-send, while a real change (e.g. a halt fires) yields a new
hash that sends exactly once more.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import math
import re

# Per-ticker reasoning is summarized to keep the email ~1 screen; the full
# reports live in state/analyze_logs/<date>_<ticker>.json.
_DECISION_CAP = 600
_DEBATE_CAP = 400

# --- Canonical alert stages (single source of truth) -------------------------
# Both senders (the in-tick orchestrator via tick.py, and the Python last-resort in
# run_tick.py) and TICK.md must agree on the stage string for a given event, because
# the alert dedup row is keyed on (trade_date, kind, stage). A drift here would
# double-page (two rows) or mis-suppress. '' is the digest grain.
AUTH_STAGE = "broker_auth"      # broker MCP token rejected (401)
HALT_STAGE = "daily_loss_halt"  # daily-loss kill-switch fired
# critical = hard stop (the tick did/should STOP); warning = best-effort hiccup.
_CRITICAL_STAGES = frozenset({
    AUTH_STAGE, HALT_STAGE, "preflight", "analyze", "plan", "commit", "orchestrator",
    "no_decisions",  # tick proceeded with pending tickers but recorded 0 decisions
})
_WARNING_STAGES = frozenset({"report", "prune", "reflect", "protect"})
STAGES = _CRITICAL_STAGES | _WARNING_STAGES


def stage_of(model: dict) -> str:
    """Resolve the dedup stage for a model (explicit, else derived from kind)."""
    st = str(model.get("stage") or "").strip()
    if st:
        return st
    kind = model.get("kind", "digest")
    if kind == "auth_error":
        return AUTH_STAGE
    if kind == "halt":
        return HALT_STAGE
    return ""


def severity_of(model: dict) -> str:
    """ok | dry_run (digest) or warning | critical (alerts)."""
    kind = model.get("kind", "digest")
    if kind in ("auth_error", "halt"):
        return "critical"
    if kind == "error":
        return "warning" if str(model.get("severity") or "").lower() == "warning" else "critical"
    return "dry_run" if model.get("dry_run") else "ok"


def action_steps(model: dict):
    """Concrete operator-recovery steps for an alert (the 'what to do' block).

    Host-aware when the runner passes ``host``; otherwise a generic '<the box>'
    placeholder. Returns [] for warning-severity hiccups (they self-heal — no CTA).
    """
    kind = model.get("kind", "digest")
    stage = stage_of(model)
    host = str(model.get("host") or "the production box")
    if severity_of(model) == "warning":
        return []
    if kind == "auth_error" or stage == AUTH_STAGE:
        return [
            "Connect to the box's desktop: https://remotedesktop.google.com/access "
            "(enter your PIN).",
            "Open a terminal there and run: claude  then  /mcp  -> robinhood-trading -> "
            "authenticate in the browser that opens.",
            "Robinhood's OAuth fully expires ~every 3.8 days with no headless refresh — this "
            "is the routine re-auth, done in the on-box browser (NOT over a remote shell: the "
            "localhost OAuth redirect needs the box's own browser).",
            "Trading resumes automatically on the next wake once the OAuth is refreshed.",
        ]
    if kind == "halt" or stage == HALT_STAGE:
        return [
            "Trading is HALTED and will NOT auto-resume.",
            "Review the loss: tail /var/log/quiver/tick.log and journalctl -u quiver.service.",
            "When ready to resume: sudo -u quiver rm /opt/quiver/KILL.",
        ]
    return [
        f"Open a shell on {host} (IAP SSH); check logs/orchestrator.log + /var/log/quiver/tick.log.",
        f"The tick stopped at the '{stage or 'unknown'}' stage — detail below.",
        "Trading is paused for this wake; the next wake retries from preflight.",
    ]


def summarize(text, cap: int = _DECISION_CAP) -> str:
    """Collapse whitespace and truncate to ``cap`` chars with an ellipsis."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) > cap:
        s = s[:cap].rstrip() + "…"
    return s


def _fmt_money(v) -> str:
    try:
        f = float(v)
        return f"${f:,.2f}" if math.isfinite(f) else "—"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(v) -> str:
    try:
        f = float(v)
        return f"{f:+.2f}%" if math.isfinite(f) else "—"
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
    """Stable 16-hex dedup hash.

    Digest: the decision skeleton (no timestamps/equity) so repeat no-op wakes
    de-dup and a real change re-sends once. Alert kinds (halt/auth_error/error):
    keyed on (kind, stage, severity, dry_run) and DELIBERATELY excluding the variable
    event_detail/equity/timestamps — so (a) the two senders compute the SAME hash for
    the same event and dedup against each other via the shared notifications row, and
    (b) a recurring same-stage failure pages once per day per stage, not every wake.
    """
    kind = model.get("kind", "digest")
    if kind == "digest":
        payload = {
            "kind": kind,
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
    else:
        payload = {
            "kind": kind,
            "stage": stage_of(model),
            "severity": severity_of(model),
            "dry_run": bool(model.get("dry_run")),
        }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _subject(model: dict) -> str:
    """Front-load the signal: the action (alerts) or the P&L (digest) leads, before
    the date — mobile truncates ~35-40 chars, so the thing you must see comes first."""
    prefix = model.get("subject_prefix") or "[Quiver]"
    date = model.get("date", "")
    kind = model.get("kind", "digest")
    if kind == "auth_error":
        return f"{prefix} ⚠ AUTH ERROR — run /mcp (trading stopped) · {date}"
    if kind == "halt" or model.get("halted"):
        reason = model.get("halt_reason") or "daily loss"
        return f"{prefix} \U0001f6d1 HALT — {reason} · {date}"
    if kind == "error":
        stg = stage_of(model) or "tick"
        if severity_of(model) == "warning":
            return f"{prefix} ⚠ {stg} hiccup (tick continued) · {date}"
        return f"{prefix} ✖ TICK FAILED at {stg} · {date}"
    nbuy, nsell, nhold = _counts(model.get("tickers", []))
    tag = " [DRY RUN]" if model.get("dry_run") else ""
    money = (f"{_fmt_pct(model.get('drop_pct'))} {_fmt_money(model.get('equity'))} · "
             if model.get("equity") is not None else "")
    return (f"{prefix} {money}{nbuy} buy / {nsell} sell / {nhold} hold{tag} · {date}")


def _render_text(model: dict) -> str:
    kind = model.get("kind", "digest")
    lines = []
    mode = "DRY RUN (paper)" if model.get("dry_run") else "LIVE"
    lines.append(f"Quiver — {model.get('date', '')}  [{mode}]")
    lines.append("")

    if kind in ("auth_error", "halt", "error"):
        sev = severity_of(model)
        stg = stage_of(model) or "tick"
        if kind == "auth_error":
            lines.append("AUTH ERROR — the broker token was rejected; the tick stopped "
                         "without trading.")
        elif kind == "halt":
            lines.append("HALT — trading stopped and will NOT auto-resume: "
                         f"{model.get('halt_reason') or 'daily-loss kill-switch fired'}.")
        elif sev == "warning":
            lines.append(f"WARNING — a best-effort '{stg}' step hiccuped, but the tick "
                         f"continued normally (it self-heals on the next wake).")
        else:
            lines.append(f"TICK FAILED at the '{stg}' stage — the tick stopped.")
        steps = action_steps(model)
        if steps:
            lines.append("")
            lines.append("What to do:")
            for i, s in enumerate(steps, 1):
                lines.append(f"  {i}. {s}")
        if model.get("event_detail"):
            lines.append("")
            lines.append(f"Detail: {summarize(model['event_detail'], 500)}")
        lines.append("")
        lines.append(f"Generated {model.get('now_iso', '')}")
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
    warnings = model.get("warnings") or []
    if warnings:
        lines.append("FYI — best-effort steps that hiccuped (the tick was unaffected):")
        for w in warnings:
            lines.append(f"  - {w.get('stage', '?')}: {summarize(w.get('detail'), 200)}")
        lines.append("")
    lines.append(f"Generated {model.get('now_iso', '')}")
    return "\n".join(lines)


def _esc(v) -> str:
    return _html.escape(str(v if v is not None else ""))


# --- Design system (Mobbin-inspired, email-client-safe) ----------------------
# One shared visual vocabulary across every kind so the emails read as one product:
# a brand header + severity accent bar, status-icon banner (alerts), a hero metric
# (digest), cards with rows, and color-coded P&L pills that ALSO carry +/- + arrows
# (never color-only — colorblind-safe). Layout is table-based with a full document
# scaffold (doctype/head/MSO) so it renders in Outlook + scales on mobile, not bare
# divs (Outlook ignores div max-width and would full-bleed).
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
# severity -> (accent, tint background, ASCII glyph). The glyph is drawn inside a
# colored circle (a table cell), so severity survives even if a client drops emoji.
_PALETTE = {
    "ok":       ("#047857", "#ecfdf5", "✓"),
    "dry_run":  ("#b45309", "#fffbeb", "•"),
    "warning":  ("#d97706", "#fffbeb", "!"),
    "critical": ("#dc2626", "#fef2f2", "!"),
}


def _palette(sev: str):
    return _PALETTE.get(sev, _PALETTE["ok"])


def _preheader(model: dict) -> str:
    """The hidden inbox-preview snippet — front-loads the signal so a critical alert
    doesn't preview as 'Quiver — <date>'."""
    kind = model.get("kind", "digest")
    if kind == "auth_error":
        return ("Broker auth expired — no orders placed. Re-auth via the box's Chrome "
                "Remote Desktop (run /mcp).")
    if kind == "halt":
        return f"Daily-loss halt fired: {model.get('halt_reason') or 'daily loss'}. Trading stopped."
    if kind == "error":
        stg = stage_of(model) or "tick"
        return (f"A {stg} step hiccuped; the tick continued and self-heals."
                if severity_of(model) == "warning"
                else f"Tick failed at {stg} — trading paused for this wake.")
    nb, ns, nh = _counts(model.get("tickers", []))
    return (f"Equity {_fmt_money(model.get('equity'))} ({_fmt_pct(model.get('drop_pct'))}) · "
            f"{nb} buy / {ns} sell / {nh} hold.")


def _circle(glyph: str, color: str) -> str:
    """A filled status circle drawn with a table cell (no emoji dependency)."""
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td width="40" height="40" align="center" valign="middle" '
            f'style="width:40px;height:40px;background:{color};border-radius:20px;'
            f'color:#ffffff;font:700 20px/40px {_FONT};text-align:center">{_esc(glyph)}</td>'
            f'</tr></table>')


def _card(inner: str, accent: str = "#e5e7eb") -> str:
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:0 0 12px"><tr><td style="border:1px solid {accent};'
            f'border-radius:12px;padding:16px;background:#ffffff">{inner}</td></tr></table>')


def _pill(text: str, bg: str, fg: str) -> str:
    return (f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'border-radius:999px;padding:3px 10px;font:600 13px/1.2 {_FONT};'
            f'white-space:nowrap">{_esc(text)}</span>')


def _pnl_pill(drop_pct) -> str:
    """Color-coded P&L pill that ALSO carries a sign + arrow (never color-only)."""
    try:
        f = float(drop_pct)
    except (TypeError, ValueError):
        return _pill("—", "#f1f5f9", "#475569")
    if not math.isfinite(f):
        return _pill("—", "#f1f5f9", "#475569")
    if f > 0.05:
        return _pill(f"▲ +{f:.2f}%", "#dcfce7", "#166534")
    if f < -0.05:
        return _pill(f"▼ {f:.2f}%", "#fee2e2", "#991b1b")
    return _pill(f"{f:+.2f}%", "#f1f5f9", "#475569")


def _banner(sev: str, title: str, subtitle: str) -> str:
    """Status-icon banner (the Mobbin alert pattern): circle + bold headline + sub.
    The severity WORD is in the title so meaning isn't carried by color alone."""
    accent, tint, glyph = _palette(sev)
    word = {"critical": "Critical", "warning": "Warning"}.get(sev, "")
    tag = (f'<span style="display:inline-block;background:{accent};color:#fff;'
           f'border-radius:4px;padding:1px 6px;font:700 10px/1.4 {_FONT};'
           f'letter-spacing:.04em;margin-bottom:4px">{word.upper()}</span><br>') if word else ""
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:0 0 12px;background:{tint};border:1px solid {accent};'
            'border-radius:12px"><tr>'
            f'<td width="56" valign="top" style="padding:16px 0 16px 16px">{_circle(glyph, accent)}</td>'
            '<td valign="middle" style="padding:16px 16px 16px 12px">'
            f'{tag}<div style="font:700 18px/1.3 {_FONT};color:{accent}">{_esc(title)}</div>'
            f'<div style="font:400 14px/1.45 {_FONT};color:#374151;margin-top:3px">{_esc(subtitle)}</div>'
            '</td></tr></table>')


def _action_card(model: dict) -> str:
    steps = action_steps(model)
    if not steps:
        return ""
    items = "".join(f'<li style="margin:5px 0">{_esc(s)}</li>' for s in steps)
    return _card(f'<div style="font:700 14px/1.3 {_FONT};color:#111;margin-bottom:6px">'
                 f'What to do</div><ol style="margin:0;padding-left:20px;color:#374151;'
                 f'font:400 14px/1.55 {_FONT}">{items}</ol>')


def _detail_block(model: dict) -> str:
    d = model.get("event_detail")
    if not d:
        return ""
    return (f'<div style="font:400 12px/1.5 {_MONO};color:#6b7280;background:#f8fafc;'
            f'border:1px solid #e5e7eb;border-radius:8px;padding:12px;white-space:pre-wrap;'
            f'word-break:break-word">{_esc(summarize(d, 500))}</div>')


def _hero(model: dict) -> str:
    return _card(
        f'<div style="font:600 12px/1 {_FONT};color:#6b7280;text-transform:uppercase;'
        f'letter-spacing:.05em">Account equity</div>'
        f'<div style="font:700 32px/1.1 {_FONT};color:#111;margin:6px 0 6px">'
        f'{_esc(_fmt_money(model.get("equity")))}</div>'
        f'<div>{_pnl_pill(model.get("drop_pct"))}'
        f'<span style="font:400 13px/1 {_FONT};color:#6b7280">'
        f'&nbsp; vs baseline {_esc(_fmt_money(model.get("baseline_equity")))}</span></div>')


def _ticker_card(r: dict, dry: bool) -> str:
    inner = (f'<div style="font:700 15px/1.2 {_FONT};color:#111">{_esc(r.get("ticker", "?"))} '
             f'<span style="font:400 13px/1.2 {_FONT};color:#6b7280">{_esc(r.get("signal", "?"))}</span></div>'
             f'<div style="font:600 13px/1.4 {_FONT};color:#374151;margin:6px 0">'
             f'{_esc(_traded_line(r, dry))}</div>')
    decision = summarize(r.get("decision"), _DECISION_CAP)
    debate = summarize(r.get("debate"), _DEBATE_CAP)
    if decision:
        inner += (f'<div style="font:400 13px/1.5 {_FONT};color:#374151">'
                  f'<b>Decision.</b> {_esc(decision)}</div>')
    if debate:
        inner += (f'<div style="font:400 13px/1.5 {_FONT};color:#6b7280;margin-top:4px">'
                  f'<b>Debate.</b> {_esc(debate)}</div>')
    return _card(inner)


def _shell(*, accent: str, mode: str, date: str, preheader: str, inner: str,
           footer_extra: str = "") -> str:
    """Wrap body content in the full, Outlook-safe email document + brand chrome."""
    pre = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
           f'font-size:1px;line-height:1px;color:#f3f4f6">{_esc(preheader)}'
           + "&#8204;&nbsp;" * 30 + '</div>')
    header = (
        '<tr><td style="padding:20px 24px 0">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td align="left" style="font:800 18px/1 {_FONT};color:#111;letter-spacing:-.01em">Quiver</td>'
        f'<td align="right" style="font:400 13px/1 {_FONT};color:#9ca3af">{_esc(date)}</td>'
        '</tr></table>'
        f'<div style="display:inline-block;margin-top:8px;background:{accent};color:#ffffff;'
        f'border-radius:6px;padding:3px 9px;font:700 11px/1.3 {_FONT};letter-spacing:.03em">{_esc(mode)}</div>'
        '</td></tr>'
        f'<tr><td style="padding:14px 24px 0"><div style="height:3px;background:{accent};border-radius:3px;font-size:0;line-height:0">&nbsp;</div></td></tr>'
    )
    footer = (
        f'<tr><td style="padding:8px 24px 24px;font:400 12px/1.6 {_FONT};color:#9ca3af">'
        f'Quiver autonomous trading · generated {_esc(date)}{_esc(footer_extra)}'
        '</td></tr>'
    )
    return (
        '<!DOCTYPE html><html lang="en" xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        '<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch>'
        '</o:OfficeDocumentSettings></xml><![endif]-->'
        '<title>Quiver</title></head>'
        '<body style="margin:0;padding:0;background:#f3f4f6;-webkit-font-smoothing:antialiased">'
        f'{pre}'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#f3f4f6"><tr><td align="center" style="padding:24px 12px">'
        '<!--[if mso]><table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:640px;background:#ffffff;border-radius:16px;border:1px solid #e5e7eb">'
        f'{header}'
        f'<tr><td style="padding:16px 24px 4px">{inner}</td></tr>'
        f'{footer}'
        '</table>'
        '<!--[if mso]></td></tr></table><![endif]-->'
        '</td></tr></table></body></html>'
    )


def _render_html(model: dict) -> str:
    kind = model.get("kind", "digest")
    dry = bool(model.get("dry_run"))
    mode = "DRY RUN" if dry else "LIVE"
    sev = severity_of(model)
    accent = _palette(sev)[0]
    date = model.get("date", "")
    pre = _preheader(model)

    if kind in ("auth_error", "error"):
        stg = stage_of(model) or "tick"
        if kind == "auth_error":
            title = "Broker auth expired"
            sub = "The broker token was rejected — the tick stopped without trading."
        elif sev == "warning":
            title = f"{stg} hiccup — tick continued"
            sub = "A best-effort step hiccuped; the tick was unaffected and self-heals."
        else:
            title = f"Tick failed at {stg}"
            sub = "The tick stopped. Trading is paused for this wake."
        inner = _banner(sev, title, sub) + _action_card(model) + _detail_block(model)
        return _shell(accent=accent, mode=mode, date=date, preheader=pre, inner=inner)

    if kind == "halt":
        inner = (_banner("critical", "Trading halted",
                         model.get("halt_reason") or "daily-loss kill-switch fired")
                 + _hero(model) + _action_card(model) + _detail_block(model))
        return _shell(accent=accent, mode=mode, date=date, preheader=pre, inner=inner)

    # digest (run complete)
    inner = _hero(model)
    ar = model.get("account_risk") or {}
    if ar.get("drawdown_pct") is not None or ar.get("sharpe") is not None:
        inner += (f'<div style="font:400 13px/1.5 {_FONT};color:#6b7280;margin:-4px 0 12px">'
                  f'{_esc(_account_risk_line(ar))}</div>')
    if model.get("halted"):
        inner += _banner("critical", "Halt",
                         model.get("halt_reason") or "daily-loss kill-switch fired")
    tickers = model.get("tickers", [])
    if not tickers:
        inner += (f'<div style="font:400 14px/1.5 {_FONT};color:#6b7280">'
                  f'(no tickers analyzed this run)</div>')
    for r in tickers:
        inner += _ticker_card(r, dry)
    warnings = model.get("warnings") or []
    if warnings:
        items = "".join(f'<li style="margin:3px 0"><b>{_esc(w.get("stage", "?"))}:</b> '
                        f'{_esc(summarize(w.get("detail"), 200))}</li>' for w in warnings)
        inner += _card(
            f'<div style="font:700 13px/1.3 {_FONT};color:#92400e">FYI — best-effort hiccups '
            f'<span style="font-weight:400;color:#6b7280">(the tick was unaffected)</span></div>'
            f'<ul style="margin:6px 0 0;padding-left:18px;color:#6b7280;font:400 13px/1.5 {_FONT}">'
            f'{items}</ul>', accent="#fcd34d")
    footer_extra = ""
    pager = model.get("pager_armed", model.get("mailer_armed"))
    if pager is not None:
        footer_extra = " · last-resort alerting: " + ("armed" if pager else "NOT configured")
    footer_extra += " · reports in state/analyze_logs/"
    return _shell(accent=accent, mode=mode, date=date, preheader=pre, inner=inner,
                  footer_extra=footer_extra)


# --- Telegram render (the alert channel) -------------------------------------
# The Telegram messages read like a person texting you the day's summary — flowing
# SENTENCES, never a templated bullet list, a "N buy / N sell / N hold" header, a numbered
# step list, or a raw machine code / UUID wall. It is still deterministic Python prose (NO
# LLM, NO network): lib/notify stays a pure renderer, so content_hash (digest_hash, computed
# off the MODEL) is unaffected by any wording here. Telegram caps a message at 4096 chars and
# renders a small HTML subset (<b>/<i>/<code>/<pre>/<a>); the sender (lib/telegram) chunks any
# overflow and falls back to plain text on a parse error, so nothing is ever lost. Per-ticker
# decision/debate prose is dropped (recoverable via the read-only chat bridge). EVERY dynamic
# value is escaped via _te; only the fixed tags are literal.
_TG_DETAIL_CAP = 600


def _te(v) -> str:
    """Escape dynamic text for Telegram HTML. ``quote=False`` matches the proven
    ``chat_bridge`` transport — Telegram's HTML parser does not require quotes escaped."""
    return _html.escape(str(v if v is not None else ""), quote=False)


def is_silent(model: dict) -> bool:
    """Telegram ``disable_notification`` policy. Every alert kind, and a halted digest,
    ALWAYS pings LOUD. A routine (non-halted) digest pings LOUD by default so the operator
    gets a daily heartbeat and never mistakes a quiet do-nothing day for a dead bot; set
    ``notify.loud_digest: false`` (carried into the model as ``loud_digest``) to restore the
    old silent-daily-record behavior."""
    if model.get("kind", "digest") != "digest":
        return False
    if model.get("halted"):
        return False
    return not model.get("loud_digest", True)


def _tg_join(names) -> str:
    """English list-join of ESCAPED names: ``[]`` -> "", ``[A]`` -> "A", ``[A, B]`` ->
    "A and B", ``[A, B, C]`` -> "A, B and C". Each name is escaped via ``_te``. Falsy names
    are dropped, so an empty/whitespace ticker never yields a dangling comma."""
    xs = [_te(n) for n in names if n is not None and str(n).strip()]
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    if len(xs) == 2:
        return f"{xs[0]} and {xs[1]}"
    return ", ".join(xs[:-1]) + f" and {xs[-1]}"


def _tg_join_clauses(items) -> str:
    """Like ``_tg_join`` but for ALREADY-composed/escaped clauses (e.g. "TSLA (guardrail)"),
    which are emitted verbatim rather than re-escaped."""
    xs = [c for c in items if c]
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    if len(xs) == 2:
        return f"{xs[0]} and {xs[1]}"
    return ", ".join(xs[:-1]) + f" and {xs[-1]}"


def _tg_humanize(detail) -> str:
    """Turn a machine detail code into readable words for a human sentence:
    ``deferred_to_rebalance_trim`` -> "deferred to rebalance trim",
    ``consistency:ungrounded_reversal`` -> "consistency ungrounded reversal". Returns ""
    for a falsy / plain-hold detail. NOT escaped here — the caller escapes at interpolation."""
    return re.sub(r"\s+", " ", re.sub(r"[_:]+", " ", str(detail or ""))).strip()


def _tg_sized(r: dict) -> str:
    """The human size of an order: "$8.93" (dollar), or "3 shares" / "1 share" (whole-share
    limit order, singular/plural agreed), or "" when neither is known. Never spells the count
    as a word (would read "one shares")."""
    amount, qty = r.get("amount"), r.get("qty")
    if amount is not None:
        return _fmt_money(amount)
    if qty is not None:
        try:
            q = float(qty)
            n = int(q) if q == int(q) else q
        except (TypeError, ValueError):
            n = qty
        return f"{n} share" + ("" if str(n) == "1" else "s")
    return ""


def _tg_order_phrase(verb: str, sized: str, ticker: str, suffix: str = "") -> str:
    """"bought $8.93 of BOT (order #6a62…)" — or "bought BOT" when the size is unknown (no
    dangling "of")."""
    if sized:
        return f"{verb} {sized} of {ticker}{suffix}"
    return f"{verb} {ticker}{suffix}"


def _tg_steps_prose(model: dict) -> str:
    """Fold the operator-recovery ``action_steps`` (already full sentences) into ONE flowing
    paragraph — a person texting you how to fix it, not a numbered list. Each step is escaped;
    bare URLs in them are auto-linked by Telegram. Returns "" when there are no steps (a
    warning-severity hiccup)."""
    return " ".join(_te(s) for s in action_steps(model))


def _tg_actions(tickers, dry_run: bool) -> list:
    """Describe what happened to the book as prose sentences, grouped by OBSERVED OUTCOME
    (honest — not the intent counts): each real/paper order is its own capitalized sentence
    (commas inside money like "$1,024.00" make a comma-joined clause list ambiguous), while
    holds and same-reason no-trades collapse into one grouped sentence each."""
    real, paper, blocked, errored, held = [], [], [], [], []
    stood: dict = {}  # humanized reason -> [ticker, ...]
    for r in tickers:
        t = _te(r.get("ticker") or "?")
        raw_t = r.get("ticker") or "?"
        status = (r.get("status") or "").lower()
        intent = (r.get("intent") or "").lower()
        side = (r.get("side") or intent or "").lower()
        verb = {"buy": "bought", "sell": "sold"}.get(side, "traded")
        sized = _tg_sized(r)
        if status == "error":
            errored.append(f"{t} ({_te(_tg_humanize(r.get('detail')) or 'see logs')})")
        elif status == "blocked_guardrail":
            blocked.append(f"{t} ({_te(_tg_humanize(r.get('detail')) or 'guardrail')})")
        elif status in ("hold", "skipped") or intent in ("hold", "skip"):
            reason = _tg_humanize(r.get("detail"))
            if reason and reason.lower() != "hold":
                stood.setdefault(reason, []).append(raw_t)
            else:
                held.append(raw_t)
        elif status == "dry_run" or dry_run:  # a simulated (paper) order — dry-run wins
            paper.append(_tg_order_phrase("would have " + verb, sized, t))
        elif status == "placed":
            bid = r.get("broker_order_id")
            suffix = f" (order #{_te(str(bid)[:8])})" if bid else ""  # short greppable ref
            real.append(_tg_order_phrase(verb, sized, t, suffix))
        elif sized:  # a real order in flight (pending/other) that carries a size
            real.append(_tg_order_phrase(verb, sized, t, f" ({_te(status or 'pending')})"))
        else:  # nothing sized and not a recognized no-trade — treat as held, never a bare clause
            held.append(raw_t)

    sentences = []
    for clause in real + paper:  # each order its own sentence (capitalized) — no comma-join
        sentences.append(clause[:1].upper() + clause[1:] + ".")
    if blocked:
        sentences.append("Held off on " + _tg_join_clauses(blocked) + ".")
    if errored:
        sentences.append("Hit a snag on " + _tg_join_clauses(errored) + ".")
    if held:
        sentences.append("Held " + _tg_join(sorted(held)) + ".")
    for reason in sorted(stood):
        sentences.append("Left " + _tg_join(sorted(stood[reason])) + f" alone ({_te(reason)}).")
    if not (real or paper) and not blocked and not errored and (held or stood):
        sentences.insert(0, "Quiet session — no orders placed.")
    return sentences


def _tg_risk_sentence(ar: dict) -> str:
    """One prose risk line, each clause composed independently so a fresh box (drawdown present
    but Sharpe still None) reads cleanly, and day(s) agrees on N."""
    dd, sh = ar.get("drawdown_pct"), ar.get("sharpe")
    bits = []
    if dd is not None:
        bits.append(f"{dd:.1f}% max drawdown")
    if sh is not None:
        n = ar.get("sharpe_n", 0)
        bits.append(f"a {sh:.2f} daily Sharpe over {n} day" + ("" if n == 1 else "s"))
    return "Risk check: " + " and ".join(bits) + "." if bits else ""


def _render_telegram_alert(model: dict, kind: str) -> str:
    """A halt / auth_error / error page as a human text: a bold headline sentence, a plain
    explanation, the recovery folded into a flowing paragraph, the raw detail in a code block,
    and a generated-at footer. NO numbered list."""
    sev = severity_of(model)
    stg = _te(stage_of(model) or "tick")
    lines = []
    if kind == "auth_error":
        lines.append("<b>⚠️ Quiver stopped — broker login expired.</b>")
        lines.append("The broker token was rejected, so the tick stopped without placing any "
                     "trades. It'll pick back up on its own once you re-auth.")
    elif kind == "halt":
        lines.append("<b>🛑 Quiver halted trading — it won't restart on its own.</b>")
        lines.append("The daily-loss kill-switch fired: "
                     f"{_te(model.get('halt_reason') or 'daily-loss limit hit')}. "
                     "Nothing will trade until you step in.")
    elif sev == "warning":
        lines.append(f"<b>⚠️ Quiver had a hiccup at the {stg} step.</b>")
        lines.append("It was a best-effort step, so the tick was unaffected and it self-heals "
                     "on the next wake.")
    else:
        lines.append(f"<b>✖️ Quiver's tick failed at the {stg} stage.</b>")
        lines.append("It stopped, so nothing traded this wake — it'll retry from the top on "
                     "the next wake.")
    steps = _tg_steps_prose(model)
    if steps:
        lines.append(("To fix it: " if kind == "auth_error" else "What to do: ") + steps)
    detail = model.get("event_detail")
    if detail:
        lines.append(f"Detail: <pre>{_te(summarize(detail, _TG_DETAIL_CAP))}</pre>")
    lines.append(f"<i>generated {_te(model.get('now_iso', ''))}</i>")
    return "\n".join(lines)


def _render_telegram_digest(model: dict) -> str:
    """The run-complete digest as a human text: a bold P&L lead, the day's actions grouped by
    outcome into flowing sentences, a short risk/warnings paragraph, and an italic footer."""
    dry = bool(model.get("dry_run"))
    date = _te(model.get("date", ""))
    eq, base, drop = model.get("equity"), model.get("baseline_equity"), model.get("drop_pct")
    paras = []  # each entry is one flowing paragraph; joined with a blank line

    if model.get("halted"):
        paras.append("🛑 <b>Quiver halted trading</b> — the daily-loss kill-switch fired "
                     f"({_te(model.get('halt_reason') or 'daily loss')}).")

    # Lead P&L sentence + the action sentences flow together as ONE paragraph (a human text).
    paper = " (paper run)" if dry else ""
    if eq is not None:
        try:
            d = float(drop) if drop is not None else None
        except (TypeError, ValueError):
            d = None
        # The mood WORD carries direction, so the percent is a sign-stripped magnitude (never
        # _fmt_pct, which would force "down -6.10%").
        if d is None:
            pnl = f" — sitting at {_fmt_money(eq)}"
        elif d > 0.05:
            pnl = f" — up {abs(d):.2f}% today, now at {_fmt_money(eq)}"
        elif d < -0.05:
            pnl = f" — down {abs(d):.2f}% today, at {_fmt_money(eq)}"
        else:
            pnl = f" — flat on the day at {_fmt_money(eq)}"
        base_s = f", started at {_fmt_money(base)}" if base is not None else ""
        lead = f"<b>Quiver</b>{paper}{pnl}{base_s}."
    else:
        lead = f"<b>Quiver</b>{paper} — daily run for {date}."

    tickers = model.get("tickers", [])
    if not tickers:
        actions = ["No tickers were analyzed this run."]
    else:
        actions = []
        # A qualitative directional lean (from intent counts) — the at-a-glance stance, honest
        # and number-free so it never contradicts the outcome grouping below.
        nb, ns, _ = _counts(tickers)
        if nb != ns and (nb + ns) > 0:
            actions.append(f"Signals leaned {'bullish' if nb > ns else 'cautious'} today.")
        actions += _tg_actions(tickers, dry)
    paras.append(" ".join([lead] + actions))

    # A second short paragraph: account risk + any best-effort warnings.
    ctx = []
    risk = _tg_risk_sentence(model.get("account_risk") or {})
    if risk:
        ctx.append(risk)
    warnings = model.get("warnings") or []
    if warnings:
        ws = _tg_join_clauses([f"{_te(_tg_humanize(w.get('stage')) or '?')} "
                               f"({_te(summarize(w.get('detail'), 120))})" for w in warnings])
        ctx.append(f"Heads up — {ws}, but the tick was unaffected.")
    if ctx:
        paras.append(" ".join(ctx))

    foot = "Full reasoning's in state/analyze_logs/ — ask the chat bot for details."
    pager = model.get("pager_armed")
    if pager is not None:
        foot += " Pager armed." if pager else " Pager NOT configured."
    paras.append(f"<i>{_te(foot)}</i>")
    return "\n\n".join(paras)


def _render_telegram(model: dict) -> str:
    """Render ``model`` into a human-text Telegram-HTML message (prose, not a templated list).

    All dynamic text is escaped via ``_te``; only fixed tags are literal. Bare URLs in the
    recovery prose are auto-linked by Telegram. The sender's chunker keeps it under 4096.
    """
    kind = model.get("kind", "digest")
    if kind in ("auth_error", "halt", "error"):
        return _render_telegram_alert(model, kind)
    return _render_telegram_digest(model)


def build_digest(model: dict) -> dict:
    """Render ``model`` into ``{subject, html, text, telegram, content_hash, kind}``.

    ``telegram`` is the compact Telegram-HTML alert body (the live channel); ``html``/``text``
    remain the email renders (kept for rollback + offline tests). ``content_hash`` is
    unchanged by the Telegram addition, so the dedup identity the two senders share holds.
    """
    return {
        "subject": _subject(model),
        "html": _render_html(model),
        "text": _render_text(model),
        "telegram": _render_telegram(model),
        "content_hash": digest_hash(model),
        "kind": model.get("kind", "digest"),
    }
