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


def _event_phrase(ev: dict) -> str:
    d = store.loads(ev.get("detail_json"), {}) or {}
    t = ev["ticker"]
    verdict = f" (verdict: {ev['verdict']})" if ev.get("verdict") and ev["verdict"] != "n/a" else ""
    if ev["type"] in ("filing", "earnings"):
        what = "reported results" if ev["type"] == "earnings" else f"filed a {d.get('form', 'filing')}"
        return f"{t} {what}{verdict}"
    if ev["type"] == "lookthrough":
        return f"{t}, a look-through holding, filed a {d.get('form', 'filing')}{verdict}"
    if ev["type"] == "price_level":
        lvl = str(d.get("level", "level")).replace("_", " ")
        return f"{t} crossed its {lvl} level at ${float(d.get('price') or 0):,.2f}{verdict}"
    if ev["type"] == "news":
        return f"{t} had material news{verdict}"
    if ev["type"] == "drift":
        return "the book drifted past its bands"
    return f"{t} had a {ev['type']} event"


def build_model(conn, *, day: str, now_dt, goal_progress: Optional[dict] = None,
                earnings_fn=None) -> dict:
    """The brief's data for ``day`` (YYYY-MM-DD, ET). ``earnings_fn(ticker) -> 'YYYY-MM-DD'|None``
    gives the next earnings date; the default reads the metrics rows."""
    snap = conn.execute("SELECT * FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    snap = dict(snap) if snap else {}
    base = conn.execute("SELECT baseline_equity FROM day_baseline WHERE trade_date=?", (day,)).fetchone()
    model = {"day": day, "equity": _f(snap.get("equity")), "baseline": _f(base[0]) if base else None,
             "goal": goal_progress, "movers": movers(snap) if snap else []}
    # The day's events: detected on the ET calendar date.
    evs = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY detected_at, id").fetchall()]
    model["events"] = [_event_phrase(e) for e in evs
                       if (store.parse_ts(e["detected_at"]).astimezone(market.ET).strftime("%Y-%m-%d") == day)]
    model["watch_filings"] = json.loads(store.get_state(conn, f"watch_filings:{day}", "[]"))
    # Waiting on the owner.
    pend = []
    for r in conn.execute("SELECT id, kind, payload_json, expires_at FROM proposals WHERE state='pending' "
                          "ORDER BY id").fetchall():
        payload = store.loads(r[2], {}) or {}
        if r[1] == "trade":
            what = "trade " + ", ".join(sorted({l["ticker"] for l in payload.get("legs", [])}))
        else:
            what = str((payload.get("change") or {}).get("kind", "change")).replace("_", " ")
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
