"""The daily close brief: the model that ``lib/notify.render_brief`` turns into prose (plan §12).

The monitor builds it once per trading day, at the first run that starts 15 minutes or
more after the close, after the drift step. It is deduplicated on the ``notifications``
row (``trade_date``, ``kind='brief'``, ``stage='brief'``). Python reads only ledger rows
and the injected seams; no LLM writes any of it.
"""

from __future__ import annotations

import json
from typing import Optional

from lib import market
from lib.desk import store

KIND = "brief"
STAGE = "brief"


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def movers(snapshot: dict, n: int = 3) -> list:
    """The ``n`` positions with the largest dollar effect on the book today, from the
    snapshot's quotes (``last_trade_price`` against ``adjusted_previous_close``)."""
    pos = store.loads(snapshot.get("positions_json"), {}) or {}
    qts = store.loads(snapshot.get("quotes_json"), {}) or {}
    out = []
    for sym, p in pos.items():
        q = qts.get(sym) or {}
        last, prev = _f(q.get("last_trade_price")), _f(q.get("adjusted_previous_close") or q.get("previous_close"))
        qty = _f((p or {}).get("quantity"))
        if not last or not prev or not qty:
            continue
        out.append({"ticker": sym, "pct": (last / prev - 1.0) * 100.0, "dollars": qty * (last - prev)})
    out.sort(key=lambda m: (-abs(m["dollars"]), m["ticker"]))
    return out[:n]


# The analyst's verdict on the thesis, in plain words.
_VERDICT_TEXT = {"intact": "our reason to own it did not change",
                 "strengthened": "our reason to own it got stronger",
                 "weakened": "our reason to own it got weaker",
                 "broken": "our reason to own it no longer holds"}
_FORM_TEXT = {"10-K": "its annual report (10-K)", "10-Q": "its quarterly report (10-Q)",
              "8-K": "a news filing (8-K)"}


_CHANGE_TEXT = {"set_thesis": "our reason to own it", "add_company": "a new name", "drop_company": "sell all",
                "set_weights": "new planned shares", "set_risk": "a setting", "set_premise": "a new premise",
                "resume_trading": "restart trading"}


def _form_text(form) -> str:
    form = str(form or "filing")
    return _FORM_TEXT.get(form, f"a {form}")


def _event_phrase(ev: dict) -> str:
    d = store.loads(ev.get("detail_json"), {}) or {}
    t = ev["ticker"]
    v = _VERDICT_TEXT.get(ev.get("verdict") or "")
    verdict = f", and {v}" if v else ""
    if ev["type"] in ("filing", "earnings"):
        what = "reported results" if ev["type"] == "earnings" else f"filed {_form_text(d.get('form'))}"
        return f"{t} {what}{verdict}"
    if ev["type"] == "lookthrough":
        return f"{t}, a company inside one of our funds, filed {_form_text(d.get('form'))}{verdict}"
    if ev["type"] == "price_level":
        price = f"${float(d.get('price') or 0):,.2f}"
        lvl = d.get("level")
        if lvl == "buy_below":
            return f"{t} fell below {price}, a good price to buy more{verdict}"
        if lvl == "trim_above":
            return f"{t} rose above {price}, a good price to sell some{verdict}"
        if lvl == "entry":
            return f"{t} reached its first-buy price of {price}{verdict}"
        return f"{t} crossed {price}{verdict}"
    if ev["type"] == "news":
        return f"{t} had important news{verdict}"
    if ev["type"] == "drift":
        return "some holdings moved too far from their planned share of the account"
    return f"{t} had a {ev['type']} event"


def _event_line(ev: dict) -> str:
    """The event phrase; a failed analysis says so."""
    text = _event_phrase(ev)
    if ev.get("status") == "failed":
        text += ", but I could not finish reading it"
    return text


def build_model(conn, *, day: str, now_dt, goal_progress: Optional[dict] = None,
                earnings_fn=None) -> dict:
    """The brief's data for ``day`` (YYYY-MM-DD, ET). ``earnings_fn(ticker) -> 'YYYY-MM-DD'|None``
    gives the next earnings date; the default reads the metrics rows."""
    snap = conn.execute("SELECT * FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    snap = dict(snap) if snap else {}
    base = conn.execute("SELECT baseline_equity FROM day_baseline WHERE trade_date=?", (day,)).fetchone()
    model = {"day": day, "equity": _f(snap.get("equity")), "baseline": _f(base[0]) if base else None,
             "goal": goal_progress, "movers": movers(snap) if snap else []}
    # The events since the last brief (its agenda item), or the day's events (the ET calendar
    # date) when no brief came before. The agenda's fyi items close with this brief, so an
    # event from the evening after the last brief is said here.
    last = conn.execute("SELECT MAX(created_at) FROM agenda WHERE kind='brief'").fetchone()
    last = last[0] if last else None
    evs = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY detected_at, id").fetchall()]
    # One phrase once: a news group's events share one verdict, so they read the same.
    model["events"] = list(dict.fromkeys(
        _event_line(e) for e in evs
        if (e["detected_at"] > last if last else
            store.parse_ts(e["detected_at"]).astimezone(market.ET).strftime("%Y-%m-%d") == day)))
    model["watch_filings"] = json.loads(store.get_state(conn, f"watch_filings:{day}", "[]"))
    # Waiting on the owner.
    pend = []
    for r in conn.execute("SELECT id, kind, payload_json, expires_at FROM proposals WHERE state='pending' "
                          "ORDER BY id").fetchall():
        payload = store.loads(r[2], {}) or {}
        if r[1] == "trade":
            what = "trade " + ", ".join(sorted({l["ticker"] for l in payload.get("legs", [])}))
        else:
            ch = payload.get("change") or {}
            what = _CHANGE_TEXT.get(ch.get("kind"), "a change")
            if ch.get("ticker"):
                what += f" for {ch['ticker']}"
        exp = store.parse_ts(r[3]).astimezone(market.ET)
        until = market.fmt_et(exp) + " ET"
        if exp.strftime("%Y-%m-%d") != day:
            until += f" on {exp.strftime('%A, %B')} {exp.day}"
        pend.append({"id": r[0], "what": what, "until": until})
    model["pending"] = pend
    # Earnings in the next five trading days.
    window = set(market.trading_days_after(day, 5))
    earn = []
    names = [r[0] for r in conn.execute("SELECT ticker FROM companies WHERE status IN ('holding','watch') "
                                        "AND kind='company' ORDER BY ticker").fetchall()]
    for t in names:
        d = None
        if earnings_fn is not None:
            try:
                d = earnings_fn(t)
            except Exception:  # noqa: BLE001
                d = None
        if d is None:
            row = conn.execute("SELECT metrics_json FROM metrics WHERE ticker=? ORDER BY as_of DESC LIMIT 1",
                               (t,)).fetchone()
            d = (store.loads(row[0], {}) or {}).get("next_earnings") if row else None
        if d and str(d)[:10] in window:
            earn.append({"ticker": t, "date": str(d)[:10]})
    model["earnings"] = sorted(earn, key=lambda e: (e["date"], e["ticker"]))
    return model


def content_hash(day: str) -> str:
    """One brief per trading day: the dedup hash is the day itself."""
    return store.sha256_hex(f"brief:{day}")[:16]


def already_sent(led, day: str) -> bool:
    return led.last_notified_hash(day, KIND, STAGE) is not None


def mark_sent(led, day: str, recipients: str, now_iso: str) -> None:
    led.mark_notified(day, KIND, content_hash(day), recipients, now_iso, stage=STAGE)
