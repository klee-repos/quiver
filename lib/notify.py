"""Render every message the desk sends from Python: alert pages, proposal cards, execution
results, and the close brief. Pure and deterministic: no I/O, no LLM, no config read.

Each renderer takes an already assembled ``model`` dict and returns Telegram-HTML text.
The prose reads like a person texting: sentences, never a bullet or numbered list, never
" / ". ``lib/desk/outbox.py`` sends the text; ``lib/telegram.py`` is the network path.

``content_hash`` is the dedup key of an alert. It hashes only ``(kind, stage, severity)``,
so the senders compute the SAME hash for one event, and a recurring same-stage failure
pages once per day per stage, not every run.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import math
import re
from datetime import datetime, timezone

_DECISION_CAP = 600

# --- Canonical alert stages (single source of truth) -------------------------
# Every pager agrees on the stage string for an event, because the alert dedup row is
# keyed on (trade_date, kind, stage). Each §9.1 monitor step pages with its own name.
AUTH_STAGE = "broker_auth"      # Robinhood login rejected (401)
HALT_STAGE = "daily_loss_halt"  # the daily-loss check fired
# critical = something stopped; warning = a best-effort step failed and retries.
_CRITICAL_STAGES = frozenset({
    AUTH_STAGE, HALT_STAGE, "monitor", "worker", "broker", "desk", "edgar", "snapshot",
    "execute", "path_unit",
})
_WARNING_STAGES = frozenset({"levels", "drift", "expire", "queue", "reflect", "benchmark", "goal",
                             "prune", "news", "transcripts", "brief"})
STAGES = _CRITICAL_STAGES | _WARNING_STAGES


def stage_of(model: dict) -> str:
    """Resolve the dedup stage for a model (explicit, else derived from kind)."""
    st = str(model.get("stage") or "").strip()
    if st:
        return st
    kind = model.get("kind", "error")
    if kind == "auth_error":
        return AUTH_STAGE
    if kind == "halt":
        return HALT_STAGE
    return ""


def severity_of(model: dict) -> str:
    """warning | critical."""
    kind = model.get("kind", "error")
    if kind in ("auth_error", "halt"):
        return "critical"
    if str(model.get("severity") or "").lower() == "warning":
        return "warning"
    if stage_of(model) in _WARNING_STAGES and not model.get("severity"):
        return "warning"
    return "critical"


def action_steps(model: dict):
    """Concrete recovery steps for an alert. [] for a warning (it retries by itself)."""
    kind = model.get("kind", "error")
    stage = stage_of(model)
    host = str(model.get("host") or "the production box")
    if severity_of(model) == "warning":
        return []
    if kind == "auth_error" or stage == AUTH_STAGE:
        return [
            "On your laptop, in the quiver repo, run ./deploy/gcp/rh-reauth.sh.",
            "It prints a Robinhood link: open it, log in and approve, then paste the localhost "
            "address the browser lands on back into the terminal.",
            "Robinhood's login expires every few days and has no headless refresh, so this is "
            "the routine re-auth.",
            "Execution waits for it; research and chat keep working.",
        ]
    if kind == "halt" or stage == HALT_STAGE:
        return [
            "The halt blocks every Approve tap for the rest of today.",
            "Review the loss in the close brief and in /var/log/quiver/tick.log.",
            "To lift it today, ask the desk to propose resume_trading and confirm it. "
            "It ends by itself at the next day's baseline.",
        ]
    return [
        f"Open a shell on {host} (IAP SSH) and check logs/desk/ and /var/log/quiver/tick.log.",
        f"The {stage or 'unknown'} step stopped; the detail is below.",
        "The next run retries it.",
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


def _money_short(v) -> str:
    """$200 for a whole amount, $98.20 otherwise (the card form)."""
    f = float(v)
    return f"${f:,.0f}" if abs(f - round(f)) < 0.005 else f"${f:,.2f}"


def _qty(q) -> str:
    s = f"{float(q):.6f}".rstrip("0").rstrip(".")
    return s or "0"


def content_hash(model: dict) -> str:
    """Stable 16-hex alert dedup hash over (kind, stage, severity)."""
    payload = {"kind": model.get("kind", "error"), "stage": stage_of(model),
               "severity": severity_of(model)}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


_TG_DETAIL_CAP = 600


def _te(v) -> str:
    """Escape dynamic text for Telegram HTML (``quote=False``, as Telegram needs)."""
    return _html.escape(str(v if v is not None else ""), quote=False)


def _tg_join(names) -> str:
    """English list-join of ESCAPED names: "A", "A and B", "A, B and C"."""
    xs = [_te(n) for n in names if n is not None and str(n).strip()]
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    if len(xs) == 2:
        return f"{xs[0]} and {xs[1]}"
    return ", ".join(xs[:-1]) + f" and {xs[-1]}"


def _plain_join(items) -> str:
    xs = [str(x) for x in items if x]
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    return ", ".join(xs[:-1]) + f" and {xs[-1]}"


def _tg_steps_prose(model: dict) -> str:
    """The recovery steps (already full sentences) as one flowing paragraph."""
    return " ".join(_te(s) for s in action_steps(model))


def _render_telegram_alert(model: dict, kind: str) -> str:
    """A halt / auth_error / error page as a human text: a bold headline sentence, a plain
    explanation, the recovery as a paragraph, the raw detail in a code block, a footer."""
    sev = severity_of(model)
    stg = _te(stage_of(model) or "desk")
    lines = []
    if kind == "auth_error":
        lines.append("<b>⚠️ Quiver can't reach Robinhood: the login expired.</b>")
        lines.append("I placed nothing. Execution waits until you log in again.")
    elif kind == "halt":
        lines.append("<b>🛑 Quiver halted trading for today.</b>")
        lines.append("The daily-loss check fired: "
                     f"{_te(model.get('halt_reason') or 'the daily-loss limit was hit')}.")
    elif sev == "warning":
        lines.append(f"<b>⚠️ Quiver had a problem at the {stg} step.</b>")
        lines.append("It was a best-effort step. The next run tries it again.")
    else:
        lines.append(f"<b>✖️ Quiver stopped at the {stg} step.</b>")
        lines.append("The next run tries it again.")
    steps = _tg_steps_prose(model)
    if steps:
        lines.append(("To fix it: " if kind == "auth_error" else "What to do: ") + steps)
    detail = model.get("event_detail")
    if detail:
        lines.append(f"Detail: <pre>{_te(summarize(detail, _TG_DETAIL_CAP))}</pre>")
    lines.append(f"<i>generated {_te(model.get('now_iso', ''))}</i>")
    return "\n".join(lines)


def build_alert(model: dict) -> dict:
    """Render a halt, auth_error or error page: ``{telegram, content_hash, kind}``."""
    kind = model.get("kind", "error")
    if kind not in ("auth_error", "halt", "error"):
        kind = "error"
    return {"telegram": _render_telegram_alert(model, kind), "content_hash": content_hash(model),
            "kind": kind}


# --- times -----------------------------------------------------------------------

def _parse(ts):
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts or "")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _et(ts):
    from zoneinfo import ZoneInfo
    dt = _parse(ts)
    return dt.astimezone(ZoneInfo("America/New_York")) if dt else None


def _clock(ts) -> str:
    t = _et(ts)
    return t.strftime("%I:%M %p").lstrip("0") if t else ""


def _until(expires_at, now) -> str:
    """'Open until 4:00 PM ET.' with the weekday and date when it is not today (ET)."""
    e, n = _et(expires_at), _et(now)
    if e is None:
        return ""
    if n is not None and e.date() == n.date():
        return f"Open until {_clock(expires_at)} ET."
    return f"Open until {_clock(expires_at)} ET on {e.strftime('%A, %B')} {e.day}."


# --- proposal cards (§10.1 rule 5) -------------------------------------------------

def _leg_sentence(leg: dict, first: bool) -> str:
    t = _te(leg["ticker"])
    verb = ("Buy" if leg["side"] == "buy" else "Sell") if first else ("then buy" if leg["side"] == "buy" else "then sell")
    if leg["side"] == "buy":
        shares = float(leg["dollars"]) / float(leg["quote"]) if leg.get("quote") else 0.0
        s = (f"{verb} {_money_short(leg['dollars'])} of {t}, about {shares:,.1f} shares at "
             f"{_fmt_money(leg['quote'])}.")
    else:
        value = float(leg["quantity"]) * float(leg["quote"])
        s = (f"{verb} {_qty(leg['quantity'])} shares of {t}, about {_money_short(round(value, 2))} at "
             f"{_fmt_money(leg['quote'])}.")
    if not first:
        s = s[0].upper() + s[1:]
    now_w, after_w, target_w = leg.get("weight_now"), leg.get("weight_after"), leg.get("target_weight")
    if now_w is not None and after_w is not None and target_w is not None:
        if abs(after_w - target_w) < 0.05:
            s += f" That takes {t} from {now_w:.1f}% of the account to the {target_w:.1f}% we planned."
        elif leg.get("exit"):
            s += f" That takes {t} from {now_w:.1f}% of the account to {after_w:.1f}%, on its way out."
        else:
            s += (f" That takes {t} from {now_w:.1f}% of the account to {after_w:.1f}%, on the way to the "
                  f"{target_w:.1f}% we planned.")
    return s


def _sentence(text) -> str:
    """End a free-text clause with a full stop, so the next sentence reads cleanly."""
    t = str(text or "").strip()
    return t if not t or t[-1] in ".!?" else t + "."


def _evidence_sentence(items) -> str:
    parts = []
    for e in items or []:
        kind, _, val = str(e).partition(":")
        if kind == "finding":
            parts.append(f"finding #{val}")
        elif kind == "document":
            parts.append(f"document #{val}")
        elif kind == "metric":
            parts.append(f"the metric {val}")
    return f"Evidence: {_te(_plain_join(parts))}." if parts else ""


def render_trade_card(model: dict) -> list:
    """A trade card as a list of messages (one, or more for a long card). The buttons sit
    on the last one. Prose only."""
    pid = model["id"]
    parts = [f"<b>Proposal #{pid}.</b>"]
    legs = model.get("legs") or []
    for i, leg in enumerate(legs):
        parts.append(_leg_sentence(leg, i == 0))
    if model.get("drift"):
        for leg in legs:
            band = leg.get("band")
            band_txt = (f", more than the {float(band):.1f} points it may drift" if band is not None else "")
            side = "above" if (leg.get("weight_now") or 0) > (leg.get("target_weight") or 0) else "below"
            parts.append(f"{_te(leg['ticker'])} is {float(leg.get('weight_now') or 0):.1f}% of the account, "
                         f"{side} the {float(leg.get('target_weight') or 0):.1f}% we planned{band_txt}.")
    if model.get("scaled"):
        parts.append("Cash covers only part of the buys, so I scaled each buy down by the same share.")
    for s in model.get("skipped") or []:
        parts.append(f"I left out {_te(s['ticker'])}: {_te(s['reason'])}.")
    oq = model.get("owner_quote")
    if oq:
        parts.append(f"You asked: '{_te(oq['text'])}' ({_clock(oq.get('at'))}).")
    if model.get("rationale"):
        parts.append(f"Why: {_te(_sentence(model['rationale']))}")
    dn = model.get("desk_news")
    if dn:
        parts.append(f"This rests on news found by the desk: {_te(dn.get('url') or 'no link')}.")
    ev = _evidence_sentence(model.get("evidence"))
    if ev:
        parts.append(ev)
    parts.append(_until(model["expires_at"], model.get("now")))
    return _pack(" ".join(p for p in parts if p))


def _pack(text: str, limit: int = 3500, max_messages: int = 3) -> list:
    """Split a long card into at most three messages on sentence boundaries. A paragraph
    break stays a paragraph break."""
    if len(text) <= limit:
        return [text]
    pieces = re.split(r"(?<=[.!?])(\s+)", text)
    out, cur = [], ""
    for i in range(0, len(pieces), 2):
        sentence, gap = pieces[i], (pieces[i + 1] if i + 1 < len(pieces) else "")
        if cur.strip() and len(cur.rstrip()) + 1 + len(sentence) > limit and len(out) < max_messages - 1:
            out.append(cur.rstrip())
            cur = sentence + gap
        else:
            cur += sentence + gap
    if cur.strip():
        out.append(cur.rstrip())
    return out


_PARA = "\x00"  # a paragraph break inside a card; _join_parts turns it into a blank line


def _semi_join(items) -> str:
    """'a; b; and c': long clauses that may hold commas of their own."""
    items = [str(x).strip().rstrip(".;") for x in items or [] if str(x).strip()]
    if len(items) <= 2:
        return " and ".join(items)
    return "; ".join(items[:-1]) + "; and " + items[-1]


def _join_parts(parts) -> str:
    """Join card sentences with a space; a part that ends in ``_PARA`` ends a paragraph."""
    text = " ".join(p for p in parts if p)
    return re.sub(r"\s*\x00\s*", "\n\n", text).strip()


def render_change_card(model: dict) -> list:
    """A change card (§10.3) in prose, at most three messages. The buttons are Confirm
    and Reject on the last message."""
    pid = model["id"]
    ch = model["change"]
    kind = ch["kind"]
    parts = [f"<b>Proposal #{pid}.</b>"]
    if kind == "add_company":
        entry = ch["entry"]
        e = ("The first buy is now." if entry["mode"] == "now"
             else f"The first buy waits until the price is at or below {_fmt_money(entry['price'])}.")
        parts.append(f"Add {_te(ch['ticker'])} to the plan as {ch['target_weight']:.1f}% of the account. I suggest "
                     f"a trade when it drifts more than {ch['band']:.1f} points from that. {e}")
        if ch.get("funding"):
            parts.append("That share comes from " + _te(_plain_join(
                f"{f['ticker']} (to {f['target_weight']:.1f}%)" for f in ch["funding"])) + ".")
    elif kind == "drop_company":
        ex = ch["exit"]
        e = ("Sell now." if ex["mode"] == "now"
             else f"Sell when the price is at or above {_fmt_money(ex['price'])}.")
        what = (f"Sell all of {_te(ch['ticker'])}. It is not part of the plan." if ch.get("unmanaged")
                else f"Sell all of {_te(ch['ticker'])} and take it out of the plan.")
        parts.append(f"{what} {e}")
        if ch.get("redistribute"):
            parts.append("Its share goes to " + _te(_plain_join(
                f"{f['ticker']} (to {f['target_weight']:.1f}%)" for f in ch["redistribute"])) + ".")
    elif kind == "set_weights":
        parts.append("New planned shares of the account: " + _te(_plain_join(
            f"{c['ticker']} to {c['target_weight']:.1f}%" + (f", with {c['band']:.1f} points of drift allowed" if c.get("band") is not None else "")
            for c in ch["changes"])) + ".")
    elif kind == "set_risk":
        parts.append(f"Change the setting {_te(ch['key'])} from {_te(ch.get('old'))} to {_te(ch['value'])}.")
    elif kind == "set_premise":
        g = ch["goal"]
        parts.append(f"A new premise (the big idea the account follows), with a goal of {g['target_return_pct']:g}% "
                     f"in {g['horizon_months']} months.")
        parts.append(f"<i>{_te(summarize(ch['text'], 3000))}</i>")
    elif kind == "set_thesis":
        # Short paragraphs: the reason, what it depends on, what would make us rethink it,
        # and the prices. A long list joined into one sentence is hard to read on a phone.
        parts.append(f"Our reason to own {_te(ch['ticker'])} (the thesis): {_te(_sentence(ch['summary']))}{_PARA}")
        if ch.get("drivers"):
            parts.append("It depends on " + _te(_semi_join(ch["drivers"])) + "." + _PARA)
        if ch.get("falsifiers"):
            parts.append("We should rethink it if " + _te(_semi_join(ch["falsifiers"])) + "." + _PARA)
        fv = [ch.get("fair_value_low"), ch.get("fair_value_base"), ch.get("fair_value_high")]
        if all(v is not None for v in fv):
            parts.append(f"I think one share is worth about {_fmt_money(fv[1])}. It is {_fmt_money(fv[0])} if "
                         f"things go badly and {_fmt_money(fv[2])} if things go very well.")
        else:
            parts.append("I give it no single worth per share.")
        if ch.get("buy_below") is not None:
            parts.append(f"If the price falls below {_fmt_money(ch['buy_below'])}, I'll tell you: that is a good "
                         "price to buy more.")
        if ch.get("trim_above") is not None:
            parts.append(f"If the price rises above {_fmt_money(ch['trim_above'])}, I'll tell you: that is a good "
                         "price to sell some.")
        if ch.get("buy_below") is None and ch.get("trim_above") is None:
            parts.append("It sets no alert prices.")
        parts[-1] += _PARA
    elif kind == "resume_trading":
        why = ch.get("halt_reason")
        parts.append("Trading is stopped for today" + (f" (the reason: {_te(why)})" if why else "") +
                     ". This starts it again, so Approve taps work. The KILL file is not touched.")
    if model.get("preview"):
        parts.append(_te(model["preview"]))
    if model.get("rationale"):
        parts.append(f"Why: {_te(_sentence(model['rationale']))}")
    ev = _evidence_sentence(model.get("evidence"))
    if ev:
        parts.append(ev)
    parts.append("Tap Confirm to apply it. " + _until(model["expires_at"], model.get("now")))
    return _pack(_join_parts(parts))


# --- execution results (§11.1 step 9) ---------------------------------------------

_SKIP_TEXT = {
    "needs_app_approval": "Robinhood wants the {t} order approved in its app, so I did not send it.",
    "review_alert": "Robinhood's review flagged the {t} order, so I did not send it.",
}


def render_execution_result(model: dict) -> str:
    """'Done. Sold 1.603 shares of SMH at $301.10 ($482.66). Bought $410.00 of CEG: 1.595
    shares at $257.05.' plus a sentence for each leg that did not go out or is still open."""
    pid = model["id"]
    legs = model.get("legs") or []
    done, notes, open_legs = [], [], []
    for l in legs:
        t = _te(l["ticker"])
        st = l["state"]
        qty = l.get("fill_quantity")
        px = l.get("fill_avg_price")
        if st in ("filled", "partially_filled") and qty and px:
            if l["side"] == "sell":
                done.append(f"Sold {float(qty):,.3f} shares of {t} at {_fmt_money(px)} ({_fmt_money(float(qty) * float(px))}).")
            else:
                done.append(f"Bought {_fmt_money(l.get('dollar_amount') or float(qty) * float(px))} of {t}: "
                            f"{float(qty):,.3f} shares at {_fmt_money(px)}.")
            if st == "partially_filled":
                open_legs.append((t, "partially filled"))
        elif st in ("placing", "placed"):
            open_legs.append((t, l.get("fill_state") or l.get("broker_state") or ("sent" if st == "placing" else "queued")))
        elif st == "skipped":
            why = l.get("skip_reason") or "not_placed"
            if why in _SKIP_TEXT:
                notes.append(_SKIP_TEXT[why].format(t=t))
            elif why not in ("not_placed", "rejected", "superseded", "withdrawn", "expired"):
                notes.append(f"I did not send the {t} order ({_te(why.replace('_', ' '))}).")
        elif st == "broker_rejected":
            notes.append(f"Robinhood rejected the {t} order.")
        elif st == "cancelled" and qty and px and float(qty) > 0:
            if l["side"] == "sell":
                done.append(f"Sold {float(qty):,.3f} of {float(l.get('quantity') or qty):,.3f} shares of {t} at "
                            f"{_fmt_money(px)} ({_fmt_money(float(qty) * float(px))}); Robinhood cancelled the rest.")
            else:
                done.append(f"Bought {_fmt_money(float(qty) * float(px))} of {t}: {float(qty):,.3f} shares at "
                            f"{_fmt_money(px)}; Robinhood cancelled the rest.")
        elif st == "cancelled":
            notes.append(f"The {t} order was cancelled.")
        elif st == "outcome_unknown":
            notes.append(f"I can't confirm the {t} order. Check the Robinhood app.")
    text = []
    if done:
        text.append("Done. " + " ".join(done))
    for t, st in open_legs:
        where = f" ({_te(st)})" if st else ""
        text.append(f"The {t} order is open at Robinhood{where}. I'll tell you when it fills.")
    text.extend(notes)
    # "I placed nothing" only when no leg went out: a sent leg can be live at Robinhood.
    if not done and not open_legs and not any(l.get("submitted_at") for l in legs):
        reason = model.get("reason")
        text.insert(0, f"I placed nothing for #{pid}: {_te(reason)}." if reason else f"I placed nothing for #{pid}.")
    return " ".join(text)


def render_requote(moves: list, *, phase: str = "sells", sells=None) -> str:
    """'ANET moved 1.6% since I proposed this ($150.00 to $152.40). Here are the new
    numbers.' A buys-phase requote names the sells first."""
    parts = []
    if phase == "buys" and sells:
        parts.append("The sells went out: " + _te(_plain_join(
            f"{_qty(s['quantity'])} shares of {s['ticker']}" for s in sells)) + ".")
    for m in moves:
        parts.append(f"{_te(m['ticker'])} moved {abs(float(m['pct'])):.1f}% since I proposed this "
                     f"({_fmt_money(m['old'])} to {_fmt_money(m['new'])}).")
    parts.append("Here are the new numbers.")
    return " ".join(parts)


# --- the close brief (§12) ----------------------------------------------------------

def render_brief(model: dict) -> list:
    """Two to four prose messages. A message with nothing to say is left out."""
    out = []
    eq, base = model.get("equity"), model.get("baseline")
    if eq is not None and base:
        chg = float(eq) - float(base)
        pct = chg / float(base) * 100.0
        s = (f"The account closed at {_fmt_money(eq)}, {'up' if chg >= 0 else 'down'} "
             f"{_fmt_money(abs(chg))} ({_fmt_pct(pct)}) on the day.")
        g = model.get("goal")
        if g and g.get("cumulative_return_pct") is not None:
            s += (f" Since the start, it is {_fmt_pct(g['cumulative_return_pct'])}, not counting money you added. "
                  f"The goal is {float(g.get('target_return_pct') or 0):g}%.")
        out.append(s)
    movers = model.get("movers") or []
    if movers:
        bits = [f"{m['ticker']} {_fmt_pct(m['pct'])} ({'+' if m['dollars'] >= 0 else '-'}{_fmt_money(abs(m['dollars']))})"
                for m in movers]
        out.append("What moved the account: " + _te(_plain_join(bits)) + ".")
    events = model.get("events") or []
    filings = model.get("watch_filings") or []
    if events or filings:
        s = ""
        if events:
            # One sentence for each event: a phrase can hold a comma of its own.
            s = " ".join(_te(_sentence(("Today " + e) if i == 0 else (e[:1].upper() + e[1:])))
                         for i, e in enumerate(events))
        if filings:
            s += (" " if s else "") + "Names we watch filed: " + _te(_plain_join(filings)) + "."
        out.append(s)
    pending = model.get("pending") or []
    earnings = model.get("earnings") or []
    if pending or earnings:
        s = ""
        if pending:
            s = "Waiting on your tap: " + _te(_plain_join(
                f"card #{p['id']} ({p['what']}, open until {p['until']})" for p in pending)) + "."
        if earnings:
            s += (" " if s else "") + "Results coming in the next five trading days: " + _te(_plain_join(
                f"{e['ticker']} on {e['date']}" for e in earnings)) + "."
        out.append(s)
    return out
