"""Proposals: build, size, render data, expire and supersede trades; validate and apply
changes; close a proposal from its legs (plan §10, §11.1 step 9).

A proposal is one ``proposals`` row plus, for a trade, one reserved ``orders`` row per leg.
Python sizes every leg here. A model names tickers, an origin and its evidence. For an
event trade it never picks a dollar amount or the fraction (§10.2 step 7). For an owner
trade the owner's own request may give a size (``orders``), and Python moves the plan to
fit the trade (§10.6). The ``ref_id`` of each leg is fixed when the proposal is built, so a
retry can never mint a second order.

``apply_change`` runs only inside the tap code of ``lib.desk.approvals`` (a Confirm tap).
It is the only code that writes a risk setting or lifts a halt. No desk tool can call it
(the W3 test enforces this). ``apply_owner_change`` applies a plan change (PLAN_KINDS) from
the owner's own message in a desk turn, with no tap (§10.6). It cannot reach a risk
setting or the halt.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import uuid
from datetime import timedelta
from typing import Optional

from lib import market, portfolio
from lib import sizing
from lib.config import Config
from lib.desk import levels, quotes, settings as desk_settings, store

FRACTIONS = (0.25, 0.5, 1.0)
SNAPSHOT_MAX_AGE = timedelta(hours=26)
ANTI_FLIP_DAYS = 30
DESK_NEWS_MAX_AGE = timedelta(days=7)
IN_FLIGHT = ("approved", "checked", "placing", "placed", "partially_filled")
OPEN_AT_BROKER = ("placing", "placed", "partially_filled")
END_FILL_STATES = ("filled", "cancelled", "broker_rejected", "outcome_unknown")
EVENT_TRADE_TYPES = ("filing", "earnings", "price_level", "news", "lookthrough")
CHANGE_KINDS = ("add_company", "drop_company", "set_weights", "set_risk", "set_premise",
                "set_thesis", "resume_trading", "cancel_order")
# The plan kinds: a desk turn applies one at once from the owner's own message (§10.6).
PLAN_KINDS = ("add_company", "drop_company", "set_weights", "set_premise", "set_thesis")
# A risk setting, the halt and a cancel change only on a card: the owner's Confirm tap or
# typed "yes" (2026-11-09: a cancel of an open order is approved like an order).
TAP_ONLY_KINDS = ("set_risk", "resume_trading", "cancel_order")
# The keys of one sized owner order (§10.6). Exactly one per order.
ORDER_KEYS = ("buy_dollars", "sell_dollars", "sell_fraction", "target_weight", "sell_all", "buy_shares", "sell_shares")
# The order types the owner may ask for (owner, 2026-11-09: "I want to be able to put limit
# orders"). A type other than market trades whole shares only (Robinhood places fractional
# shares only as a market order in regular hours).
ORDER_TYPES = {"market": "market", "limit": "limit", "stop": "stop_market", "stop_market": "stop_market",
               "stop_loss": "stop_market", "stop_limit": "stop_limit"}
GOOD_TILL = {None: "gfd", "": "gfd", "day": "gfd", "gfd": "gfd", "today": "gfd",
             "cancelled": "gtc", "canceled": "gtc", "gtc": "gtc"}


def _order_style(o: dict, t: str) -> dict:
    """The type, prices and time in force of one owner order, checked. A market order keeps
    the defaults (gfd, no price)."""
    raw = str(o.get("type") or "market").lower().strip()
    typ = ORDER_TYPES.get(raw)
    if typ is None:
        raise ProposalError(f"{t}: type is market, limit, stop or stop_limit")
    tif = GOOD_TILL.get(str(o.get("good_till") or "").lower().strip() or None)
    if tif is None:
        raise ProposalError(f"{t}: good_till is day or cancelled")

    def price(key):
        v = _f(o.get(key))
        if v is None:
            return None
        if v <= 0:
            raise ProposalError(f"{t}: {key} is a price above 0")
        return round(v, 2) if v >= 1 else round(v, 4)

    lim, stp = price("limit_price"), price("stop_price")
    if typ == "market":
        if lim is not None or stp is not None:
            raise ProposalError(f"{t}: a market order takes no price; say type limit or stop")
        if tif != "gfd":
            raise ProposalError(f"{t}: a market order is good for the day only")
    elif typ == "limit" and (lim is None or stp is not None):
        raise ProposalError(f"{t}: a limit order needs limit_price (and no stop_price)")
    elif typ == "stop_market" and (stp is None or lim is not None):
        raise ProposalError(f"{t}: a stop order needs stop_price (and no limit_price)")
    elif typ == "stop_limit" and (stp is None or lim is None):
        raise ProposalError(f"{t}: a stop-limit order needs stop_price and limit_price")
    return {"type": typ, "limit_price": lim, "stop_price": stp, "time_in_force": tif}


def _reserve_price(style: dict, quote: float) -> float:
    """The most one share of a buy may cost: the limit, else the stop or the quote."""
    if style.get("limit_price") is not None:
        return float(style["limit_price"])
    return max(float(style.get("stop_price") or 0.0), float(quote))


class ProposalError(ValueError):
    """A refused proposal. The text goes back to the model or the owner as is."""


# --- small helpers -----------------------------------------------------------------

def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _now_dt():
    return store.utc_now()


def kill_active(cfg: Config) -> bool:
    return os.path.exists(cfg.kill_switch_file)


def today() -> str:
    return market.trading_day_et()


def active_goal(conn) -> Optional[dict]:
    row = conn.execute("SELECT * FROM strategy_goal WHERE status='active' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def book_rows(conn, goal_id: int, statuses=("active", "exiting")) -> list:
    marks = ",".join("?" for _ in statuses)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM target_portfolio WHERE goal_id=? AND status IN ({marks}) ORDER BY ticker",
        (goal_id, *statuses)).fetchall()]


def snapshot_view(row: dict) -> dict:
    """The parsed snapshot: positions ``{SYM: {quantity, market_value}}``, buying power,
    equity, and the capture time."""
    return {"id": row["id"], "captured_at": row["captured_at"],
            "equity": float(row["equity"]), "buying_power": float(row["buying_power"]),
            "cash": _f(row["cash"]), "positions": store.loads(row["positions_json"], {}) or {},
            "quotes": store.loads(row["quotes_json"], {}) or {},
            "approval_setting": row["approval_setting"]}


def latest_snapshot(conn) -> Optional[dict]:
    row = conn.execute("SELECT * FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return snapshot_view(dict(row)) if row else None


def evidence_items(evidence) -> list:
    """Normalize evidence to ``[(kind, value)]``: kind is finding, document or metric.
    Accepts ``"finding:212"``, ``{"finding": 212}``, ``{"kind": "finding", "id": 212}``."""
    out = []
    for e in evidence or []:
        if isinstance(e, dict):
            if "kind" in e:
                out.append((str(e["kind"]), e.get("id", e.get("key"))))
            else:
                for k, v in e.items():
                    out.append((str(k), v))
        else:
            kind, _, val = str(e).partition(":")
            out.append((kind.strip(), val.strip()))
    clean = []
    for kind, val in out:
        kind = {"findings": "finding", "documents": "document", "doc": "document",
                "metrics": "metric"}.get(kind, kind)
        if kind not in ("finding", "document", "metric"):
            raise ProposalError(f"evidence item {kind}:{val} is not a finding, document or metric")
        if kind in ("finding", "document"):
            try:
                val = int(val)
            except (TypeError, ValueError):
                raise ProposalError(f"evidence {kind} id {val!r} is not a number")
        clean.append((kind, val))
    return clean


# --- quotes and the deployable base ------------------------------------------------

def _leg_quote(conn, ticker, now_dt, yf_price_fn, snap_override: Optional[dict] = None):
    """The leg quote of §10.2 step 3: the leg price from the newest snapshot (< 60 min)
    that holds the ticker, else yfinance. A requote first reads the exec-check snapshot."""
    if snap_override is not None:
        q = (snap_override.get("quotes") or {}).get(ticker)
        if q is not None:
            price, _ = quotes.leg_price(q, store.parse_ts(snap_override["captured_at"]))
            if price is not None:
                return price, f"snapshot:{snap_override['id']}"
    return quotes.price_for(conn, ticker, now_dt, yf_price_fn)


def _last_known_price(conn, sym: str, pos: Optional[dict]) -> Optional[float]:
    """A value estimate for a held name with no fresh quote: the market value per share
    of the newest snapshot that priced it. It values a position; it never prices a leg."""
    mv, qty = _f((pos or {}).get("market_value")), _f((pos or {}).get("quantity"))
    if mv and qty:
        return mv / qty
    for row in conn.execute("SELECT positions_json FROM broker_snapshots ORDER BY id DESC LIMIT 50"):
        p = (store.loads(row[0], {}) or {}).get(sym) or {}
        mv, qty = _f(p.get("market_value")), _f(p.get("quantity"))
        if mv and qty:
            return mv / qty
    return None


def prefetch_prices(led, tickers=(), *, now_dt=None, yf_price_fn=None):
    """A ``yf_price_fn`` for a write transaction. It asks yfinance BEFORE the transaction
    for every book name, every snapshot position and ``tickers`` that has no fresh snapshot
    quote, and answers from that cache inside it. No write lock waits on a network call.
    A name the cache does not hold still asks yfinance (rare: a name outside the book)."""
    now_dt = now_dt or _now_dt()
    fn = yf_price_fn or quotes.yf_last_price
    with led.connect() as conn:
        goal = active_goal(conn)
        rows = book_rows(conn, goal["id"], ("active", "exiting")) if goal else []
        snap = latest_snapshot(conn) or {}
        names = {str(r["ticker"]).upper() for r in rows} | {str(t).upper() for t in (snap.get("positions") or {})}
        names |= {str(t).upper() for t in tickers or () if t}
        need = sorted(t for t in names if quotes.snapshot_price(conn, t, now_dt)[0] is None)
    cache = {}
    for t in need:
        try:
            cache[t] = fn(t)
        except Exception:  # noqa: BLE001 — a vendor error means no quote, never a guess
            cache[t] = None

    def lookup(ticker):
        t = str(ticker).upper()
        return cache[t] if t in cache else fn(t)
    return lookup


def change_tickers(change) -> list:
    """Every ``ticker`` value in a change payload (the change and its funding lists)."""
    out = []
    if isinstance(change, dict):
        for k, v in change.items():
            if k == "ticker" and isinstance(v, str):
                out.append(v.upper())
            else:
                out += change_tickers(v)
    elif isinstance(change, list):
        for v in change:
            out += change_tickers(v)
    return out


def valuation_base(conn, snap: dict, now_dt, yf_price_fn, snap_override=None) -> dict:
    """``deployable = Σ position values + buying_power`` (falls back to equity), with a
    quote for every held name. Returns ``{deployable, values, quantities, quotes}``."""
    values, qtys, qts = {}, {}, {}
    for sym, p in (snap.get("positions") or {}).items():
        sym = str(sym).upper()
        qty = _f((p or {}).get("quantity")) or 0.0
        if qty <= 0:
            continue
        price, _src = _leg_quote(conn, sym, now_dt, yf_price_fn, snap_override)
        if price is None:
            price = _last_known_price(conn, sym, p)
        qtys[sym] = qty
        qts[sym] = price
        values[sym] = qty * price if price else (_f((p or {}).get("market_value")) or 0.0)
    deployable = sum(values.values()) + float(snap.get("buying_power") or 0.0)
    if deployable <= 0:
        deployable = float(snap.get("equity") or 0.0)
    return {"deployable": deployable, "values": values, "quantities": qtys, "quotes": qts}


# --- the event-to-trade table (§10.2 step 7) ---------------------------------------

def allowed_event_trade(event: dict) -> dict:
    """``{side, fraction, only_above_target}`` for the event and its recorded verdict,
    or raise ProposalError with the reason. Python owns this table, not the model."""
    verdict = event.get("verdict")
    etype = event.get("type")
    detail = store.loads(event.get("detail_json"), {}) or {}
    if not verdict or verdict == "n/a":
        raise ProposalError(f"event #{event['id']} has no recorded verdict; record_verdict first")
    if verdict == "broken":
        raise ProposalError("the thesis is broken: propose drop_company, not a trade")
    if verdict == "weakened":
        return {"side": "sell", "fraction": 1.0, "only_above_target": True}
    if etype == "price_level":
        lvl = detail.get("level")
        if lvl in ("buy_below", "entry") and verdict in ("intact", "strengthened"):
            return {"side": "buy", "fraction": 1.0, "only_above_target": False}
        if lvl == "trim_above":
            return {"side": "sell", "fraction": 1.0, "only_above_target": True}
    if etype in ("filing", "earnings", "lookthrough", "news"):
        if verdict == "strengthened":
            return {"side": "buy", "fraction": 0.5, "only_above_target": False}
        if verdict == "intact":
            raise ProposalError("the verdict is intact: an unchanged thesis allows no trade")
    raise ProposalError(f"no trade is allowed for a {etype} event with verdict {verdict}")


# --- origin checks (§10.2 step 6) ---------------------------------------------------

def _check_event_origin(conn, event_id, tickers, ev_items, now_dt) -> dict:
    ev = store.get_event(conn, event_id)
    if ev is None:
        raise ProposalError(f"event #{event_id} does not exist")
    if ev["type"] not in EVENT_TRADE_TYPES:
        raise ProposalError(f"a {ev['type']} event cannot back a trade")
    detail = store.loads(ev["detail_json"], {}) or {}
    if ev["type"] == "filing":
        acc = detail.get("accession")
        row = conn.execute("SELECT tier FROM filings WHERE accession=?", (acc,)).fetchone()
        if not row or row[0] != "redecide":
            raise ProposalError(f"filing {acc} is not a redecide filing, so it backs no trade")
    if ev["status"] != "analyzed":
        raise ProposalError(f"event #{event_id} is {ev['status']}; it backs a trade only once analyzed")
    prior = conn.execute("SELECT id FROM proposals WHERE kind='trade' AND event_id=? LIMIT 1",
                         (event_id,)).fetchone()
    if prior:
        raise ProposalError(f"event #{event_id} already backs proposal #{prior[0]}")
    allowed = {ev["ticker"]}
    if ev["type"] == "lookthrough":
        allowed |= {str(x.get("etf", "")).upper() for x in detail.get("etfs", [])}
    bad = [t for t in tickers if t not in allowed]
    if bad:
        raise ProposalError(f"event #{event_id} is about {ev['ticker']}, not {', '.join(bad)}")
    if ev["created_by"] == "desk":
        doc_id = detail.get("document_id")
        drow = conn.execute("SELECT kind FROM documents WHERE id=?", (doc_id,)).fetchone()
        ni = conn.execute("SELECT published_at FROM news_items WHERE document_id=? "
                          "ORDER BY published_at DESC LIMIT 1", (doc_id,)).fetchone()
        pub = store.parse_ts(ni[0]) if ni else None
        if not drow or drow[0] != "news" or pub is None or now_dt - pub > DESK_NEWS_MAX_AGE:
            raise ProposalError("a desk news event backs a trade only while its feed date is within 7 days")
    # Fresh evidence: a finding or document at or after detected_at, or the event's own document.
    detected = store.parse_ts(ev["detected_at"])
    own_docs = set()
    if detail.get("accession"):
        own_docs |= {r[0] for r in conn.execute(
            "SELECT id FROM documents WHERE accession=?", (detail["accession"],)).fetchall()}
    if detail.get("document_id"):
        own_docs.add(int(detail["document_id"]))
    fresh = False
    for kind, val in ev_items:
        if kind == "document" and val in own_docs:
            fresh = True
        elif kind == "finding":
            r = conn.execute("SELECT created_at FROM findings WHERE id=?", (val,)).fetchone()
            if r and store.parse_ts(r[0]) and store.parse_ts(r[0]) >= detected:
                fresh = True
        elif kind == "document":
            if _document_counts(conn, val, now_dt, since=detected):
                fresh = True
    if not fresh:
        raise ProposalError("the evidence holds no finding or document from this event; record one first")
    return ev


def _document_counts(conn, doc_id, now_dt, since=None) -> bool:
    """True when a cited document counts as fresh evidence. A ``web`` document never
    counts toward the 7-day rule (a page controls its own date). A ``news`` document
    counts only while its feed ``published_at`` is within 7 days."""
    d = conn.execute("SELECT kind, fetched_at FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        return False
    if d[0] == "web":
        return False
    if d[0] == "news":
        ni = conn.execute("SELECT published_at FROM news_items WHERE document_id=? "
                          "ORDER BY published_at DESC LIMIT 1", (doc_id,)).fetchone()
        pub = store.parse_ts(ni[0]) if ni else None
        if pub is None or now_dt - pub > DESK_NEWS_MAX_AGE:
            return False
    fetched = store.parse_ts(d[1])
    return since is None or (fetched is not None and fetched >= since)


def _check_anti_flip(conn, ticker, side, ev_items, now_dt) -> None:
    """A trade against the direction of one approved in the last 30 days needs a finding
    created after that approval."""
    opposite = "buy" if side == "sell" else "sell"
    cutoff = store.iso(now_dt - timedelta(days=ANTI_FLIP_DAYS))
    row = conn.execute(
        "SELECT p.decided_at FROM proposals p JOIN orders o ON o.proposal_id = p.id "
        "WHERE p.kind='trade' AND o.ticker=? AND o.side=? AND p.decided_at IS NOT NULL "
        "AND p.decided_at >= ? AND p.state NOT IN ('rejected','expired','superseded','withdrawn','pending') "
        "ORDER BY p.decided_at DESC LIMIT 1", (ticker, opposite, cutoff)).fetchone()
    if not row:
        return
    approved = store.parse_ts(row[0])
    for kind, val in ev_items:
        if kind == "finding":
            r = conn.execute("SELECT created_at FROM findings WHERE id=?", (val,)).fetchone()
            if r and store.parse_ts(r[0]) and store.parse_ts(r[0]) > approved:
                return
    raise ProposalError(f"a {opposite} of {ticker} was approved on {row[0][:10]}; "
                        f"a {side} now needs a finding recorded after that")


def in_flight_ticker(conn, ticker) -> Optional[str]:
    marks = ",".join("?" for _ in IN_FLIGHT)
    row = conn.execute(
        f"SELECT o.ref_id FROM orders o JOIN proposals p ON p.id = o.proposal_id "
        f"WHERE p.kind='trade' AND o.ticker=? AND o.state IN ({marks}) LIMIT 1",
        (ticker, *IN_FLIGHT)).fetchone()
    return row[0] if row else None


def held_job_of_leg(conn, ref_id, now: str):
    """``(proposal_id, not_before)`` when the leg's proposal waits on a held execute job
    (an Approve tap while the market was closed, §10.5 check 4); else None."""
    row = conn.execute("SELECT j.proposal_id, j.not_before FROM orders o JOIN broker_jobs j ON "
                       "j.proposal_id = o.proposal_id WHERE o.ref_id=? AND j.kind='execute' AND "
                       "j.status='queued' AND j.not_before > ? ORDER BY j.id DESC LIMIT 1",
                       (ref_id, now)).fetchone()
    return (row[0], row[1]) if row else None


# --- trades (§10.2) ------------------------------------------------------------------

def _fund_names(fund_from, cash_ticker) -> list:
    """The holdings named to pay for a sized buy. "cash" and the cash sleeve mean cash."""
    out = []
    for f in fund_from or []:
        t = str(f or "").upper().strip()
        if t and t not in ("CASH", str(cash_ticker or "").upper()) and t not in out:
            out.append(t)
    return out


def _styled_leg(t, key, o, style, row, price, src, held, v, w_now, dep, cap) -> dict:
    """One limit or stop order of the owner: whole shares, the owner's prices, day or GTC.
    A buy reserves shares x its most a share may cost. A sale sells whole shares only."""
    base_leg = {"ticker": t, "quote": price, "quote_source": src, "weight_now": w_now,
                "target_weight": float(row["target_weight"] or 0.0), "band": row.get("band"), **style}
    reserve = _reserve_price(style, price)
    if key in ("buy_dollars", "buy_shares", "target_weight") and not (key == "target_weight" and
                                                                       (_f(o["target_weight"]) or 0) * dep / 100.0 <= v):
        if row["status"] != "active":
            raise ProposalError(f"{t} is on its way out of the plan. Add it back with propose_change add_company first")
        if key == "buy_shares":
            n = _f(o["buy_shares"])
        elif key == "buy_dollars":
            d = _f(o["buy_dollars"])
            if d is None or d <= 0:
                raise ProposalError(f"{t}: buy_dollars is a dollar amount above 0")
            n = math.floor(d / reserve + 1e-9)
        else:
            w = _f(o["target_weight"])
            if w is None or w < 0 or w > cap + 1e-9:
                raise ProposalError(f"{t}: target_weight is 0 to the {cap:g}% limit per name")
            n = math.floor((w / 100.0 * dep - v) / reserve + 1e-9)
        if n is None or n < 1 or abs(n - round(n)) > 1e-9:
            raise ProposalError(f"{t}: a {style['type'].replace('_', ' ')} order buys whole shares; at "
                                f"${reserve:,.2f} a share that size buys {math.floor(n or 0)}. Give buy_shares "
                                "of 1 or more, or a market order")
        n = int(round(n))
        if style.get("limit_price") is not None and style["type"] == "limit" and style["limit_price"] > price * 1.1:
            raise ProposalError(f"{t}: a buy limit of ${style['limit_price']:,.2f} is more than 10% over the "
                                f"price (${price:,.2f}); check the number with the owner")
        return {**base_leg, "side": "buy", "quantity": float(n), "dollars": round(n * reserve, 2),
                "want": round(n * reserve, 2), "gap": round(n * reserve, 2)}
    if held <= 0:
        raise ProposalError(f"{t}: nothing is held, so there is nothing to sell")
    whole = math.floor(held + 1e-9)
    if key == "sell_shares":
        n = _f(o["sell_shares"])
        if n is None or n <= 0 or abs(n - round(n)) > 1e-9:
            raise ProposalError(f"{t}: a {style['type'].replace('_', ' ')} order sells whole shares")
        n = int(round(n))
    elif key == "sell_all":
        n = whole
    elif key == "sell_fraction":
        frac = _f(o["sell_fraction"])
        if frac is None or not 0 < frac <= 1:
            raise ProposalError(f"{t}: sell_fraction is above 0 and at most 1")
        n = math.floor(held * frac + 1e-9)
    elif key == "sell_dollars":
        d = _f(o["sell_dollars"])
        if d is None or d <= 0:
            raise ProposalError(f"{t}: sell_dollars is a dollar amount above 0")
        n = math.floor(d / price + 1e-9)
    else:
        w = _f(o["target_weight"])
        n = math.floor((v - (w or 0.0) / 100.0 * dep) / price + 1e-9)
    if n > whole:
        raise ProposalError(f"{t}: {n} shares is more than the {whole} whole shares held ({held:g} in all)")
    if n < 1:
        raise ProposalError(f"{t}: a {style['type'].replace('_', ' ')} order sells whole shares, and {held:g} "
                            "held gives none for this size. Use a market order to sell part of a share")
    return {**base_leg, "side": "sell", "quantity": float(n), "exit": n >= held - 1e-9}


def _size_orders(rows: dict, base: dict, orders: list, fund_from, *, cash_ticker, price_of, min_notional,
                 money: float, money_text: str, cap: float) -> tuple:
    """The legs of a sized owner request (§10.6): each order buys or sells the dollars the
    owner asked for, sells ``sell_fraction`` of the holding, or moves the name to
    ``target_weight``. When the buys need more than
    the sales bring in and ``fund_from`` names holdings, Python sells the rest from them,
    in proportion to their values. Returns ``(sells, buys)``. Each refusal holds the numbers
    the desk needs to settle it with the owner."""
    dep = base["deployable"]
    legs = {}

    def held_of(t):
        return base["quantities"].get(t, 0.0)

    def value_of(t):
        return base["values"].get(t, 0.0)

    def quote(t):
        price, src = price_of(t)
        if price is None:
            raise ProposalError(f"no quote for {t}; refresh the portfolio first")
        return price, src

    def weight_now(t):
        return value_of(t) / dep * 100.0 if dep > 0 else 0.0

    def sell_leg(t, qty, price, src, exit_):
        r = rows[t]
        return {"ticker": t, "side": "sell", "quantity": qty, "quote": price, "quote_source": src,
                "weight_now": weight_now(t), "target_weight": float(r["target_weight"] or 0.0), "exit": exit_,
                "band": r.get("band")}

    for o in orders:
        o = o or {}
        t = str(o.get("ticker") or "").upper().strip()
        # A key counts when it is present with a value. 0 is a value (target_weight 0 sells
        # all), and sell_all counts only as the boolean true.
        given = [k for k in ORDER_KEYS if o.get(k) is not None and not (k == "sell_all" and o.get(k) is False)]
        if not t or len(given) != 1:
            raise ProposalError(f"{t or 'each order'}: give the ticker and exactly one of buy_dollars, sell_dollars, "
                                "buy_shares, sell_shares, sell_fraction, target_weight or sell_all")
        style = _order_style(o, t)
        if "sell_all" in given and o.get("sell_all") is not True:
            raise ProposalError(f"{t}: sell_all is true or left out")
        if t == cash_ticker:
            raise ProposalError(f"{t} is the cash sleeve. It is held as cash and gets no order; Python moves its "
                                "planned share for you")
        if t in legs:
            raise ProposalError("each ticker once, please")
        if t not in rows:
            raise ProposalError(f"{t} is not in the book; propose add_company (from the owner's message it applies "
                                "at once, and its buy card follows)")
        price, src = quote(t)
        held, v = held_of(t), value_of(t)
        key = given[0]
        if style["type"] != "market":
            legs[t] = _styled_leg(t, key, o, style, rows[t], price, src, held, v, weight_now(t), dep, cap)
            continue
        if key == "buy_shares":
            n = _f(o["buy_shares"])
            if n is None or n <= 0:
                raise ProposalError(f"{t}: buy_shares is a number of shares above 0")
            key, o = "buy_dollars", {**o, "buy_dollars": round(n * price, 2)}
        elif key == "sell_shares":
            n = _f(o["sell_shares"])
            if n is None or n <= 0:
                raise ProposalError(f"{t}: sell_shares is a number of shares above 0")
            if n > held + 1e-9:
                raise ProposalError(f"{t}: {n:g} shares is more than the {held:g} held")
            if n >= held - 1e-9:
                key = "sell_all"
            else:
                legs[t] = sell_leg(t, round(n, 6), price, src, False)
                continue
        if key == "sell_all":
            side, dollars = "sell", v
        elif key == "sell_fraction":
            frac = _f(o["sell_fraction"])
            if frac is None or not 0 < frac <= 1:
                raise ProposalError(f"{t}: sell_fraction is the share of the holding to sell, above 0 and at most 1")
            if held <= 0:
                raise ProposalError(f"{t}: nothing is held, so there is nothing to sell")
            if frac >= 1:
                key, side, dollars = "sell_all", "sell", v
            else:
                qty, why = sizing.resolve_sell_quantity_min_notional(held, round(held * frac, 6), price,
                                                                     min_notional=min_notional, bump_to_min=False)
                if qty <= 0:
                    raise ProposalError(f"{t}: {frac:g} of the holding is too small to send ({why})")
                legs[t] = sell_leg(t, qty, price, src, False)
                continue
        elif key == "target_weight":
            w = _f(o["target_weight"])
            if w is None or w < 0:
                raise ProposalError(f"{t}: target_weight is a percent of the account, 0 or more")
            if w > cap + 1e-9:
                raise ProposalError(f"{t}: {w:g}% of the account is over the {cap:g}% limit per name")
            dollars = w / 100.0 * dep - v
            side, dollars = ("buy" if dollars > 0 else "sell"), abs(dollars)
            if w == 0:
                key = "sell_all"
        else:
            d = _f(o[key])
            if d is None or d <= 0:
                raise ProposalError(f"{t}: {key} is a dollar amount above 0")
            side, dollars = ("buy" if key == "buy_dollars" else "sell"), d
        if side == "sell" and (key == "sell_all" or dollars >= v - 0.01):
            if held <= 0:
                raise ProposalError(f"{t}: nothing is held, so there is nothing to sell")
            legs[t] = sell_leg(t, round(held, 6), price, src, True)
            continue
        if side == "buy":
            if rows[t]["status"] != "active":
                raise ProposalError(f"{t} is on its way out of the plan. Add it back with propose_change "
                                    "add_company first; from the owner's message it applies at once")
            dollars = round(dollars, 2)
            if dollars < min_notional:
                raise ProposalError(f"{t}: a buy of ${dollars:,.2f} is under the ${min_notional:.2f} minimum order")
            legs[t] = {"ticker": t, "side": "buy", "want": dollars, "gap": dollars, "dollars": dollars,
                       "quote": price, "quote_source": src, "weight_now": weight_now(t),
                       "target_weight": float(rows[t]["target_weight"] or 0.0), "band": rows[t].get("band")}
            continue
        if held <= 0:
            raise ProposalError(f"{t}: nothing is held, so there is nothing to sell")
        raw = round(min(held, dollars / price), 6)
        qty, why = sizing.resolve_sell_quantity_min_notional(held, raw, price, min_notional=min_notional,
                                                             bump_to_min=False)
        if qty <= 0:
            raise ProposalError(f"{t}: a sale of ${dollars:,.2f} is too small to send ({why})")
        legs[t] = sell_leg(t, qty, price, src, False)

    def totals():
        # A limit or stop sale may never fill, so only a market sale pays for the buys.
        b = sum(l["dollars"] for l in legs.values() if l["side"] == "buy")
        s = sum(l["quantity"] * l["quote"] for l in legs.values()
                if l["side"] == "sell" and (l.get("type") or "market") == "market")
        return b, s

    buys_total, proceeds = totals()
    need = round(buys_total - proceeds, 2)
    names = _fund_names(fund_from, cash_ticker)
    if names and need > 0:
        for f in names:
            if f in legs:
                raise ProposalError(f"{f} already has an order here, so it cannot also pay for the buys")
            if f not in rows or held_of(f) <= 0:
                raise ProposalError(f"{f} is not a holding in the plan, so it cannot pay for the buys")
        pool = sum(value_of(f) for f in names)
        if pool + 0.01 < need:
            raise ProposalError(f"{' and '.join(names)} hold ${pool:,.2f}, less than the ${need:,.2f} the buys need")
        for f in names:
            price, src = quote(f)
            share = need * value_of(f) / pool
            held = held_of(f)
            if share >= value_of(f) - 0.01:
                legs[f] = sell_leg(f, round(held, 6), price, src, True)
                continue
            raw = min(held, round(share / price + 5e-7, 6))   # round up: the sale covers its share
            qty, why = sizing.resolve_sell_quantity_min_notional(held, raw, price, min_notional=min_notional,
                                                                 bump_to_min=True)
            if qty <= 0:
                raise ProposalError(f"{f}: a sale of ${share:,.2f} is too small to send ({why})")
            legs[f] = sell_leg(f, qty, price, src, False)
        buys_total, proceeds = totals()
    available = max(0.0, money) + proceeds
    if buys_total > available + 0.01:
        raise ProposalError(f"the buys need ${buys_total:,.2f}, but ${available:,.2f} can be spent ({money_text}). "
                            "Ask the owner which holding to sell to pay for it (fund_from), or buy less")
    return ([l for l in legs.values() if l["side"] == "sell"], [l for l in legs.values() if l["side"] == "buy"])


def _trade_plan(rows: dict, all_rows: list, legs: list, base: dict, deployable: float, *, cash_ticker: str,
                cap: float, hard_floor) -> Optional[dict]:
    """How the plan moves with a sized owner trade (§10.6). A name's planned share moves only
    as far as the trade takes it past the plan: a buy raises it to the weight after the buy,
    a sale lowers it to the weight after the sale, and a sale of all of it takes it out. The
    cash sleeve takes the difference, so the plan still sums to 100%. Only a raise is held to
    the per-name limit. None when the plan does not move. Each leg's ``target_weight``
    becomes the new planned share."""
    changes, delta = [], 0.0
    for leg in legs:
        t = leg["ticker"]
        frm = float(rows[t]["target_weight"] or 0.0)
        after = _weight_after(leg, base, deployable)
        if leg.get("exit"):
            to = 0.0
        elif leg["side"] == "buy":
            to = max(frm, round(after, 4))
        else:
            to = min(frm, round(after, 4))
        if to > frm + 1e-9 and to > cap + 1e-9:     # only a raise is held to the limit; a sale never is
            room = max(0.0, cap / 100.0 * deployable - base["values"].get(t, 0.0))
            raise ProposalError(f"{t} would be {after:.1f}% of the account, over the {cap:g}% limit per name. "
                                f"The most it can take now is about ${room:,.2f} more")
        leg["target_weight"] = to
        if abs(to - frm) > 1e-9 or leg.get("exit"):
            changes.append({"ticker": t, "from": frm, "target_weight": to, "exit": bool(leg.get("exit"))})
            delta += to - frm
    if not changes:
        return None
    cash_row = next((r for r in all_rows if r["ticker"] == cash_ticker and r["status"] == "active"), None)
    if cash_row is None:
        raise ProposalError(f"the plan has no cash sleeve ({cash_ticker}) to balance this trade")
    c_from = float(cash_row["target_weight"] or 0.0)
    c_to = round(c_from - delta, 4)
    if c_to < -1e-6:
        raise ProposalError(f"the plan keeps only {c_from:g}% of the account in cash, and this trade needs "
                            f"{delta:.1f} points of it. Sell something to pay for it (fund_from), or buy less")
    if hard_floor is not None and c_to + 1e-9 < float(hard_floor):
        raise ProposalError(f"the plan's cash would be {c_to:.1f}% of the account, under the "
                            f"{float(hard_floor):g}% cash floor")
    return {"changes": changes, "cash": {"ticker": cash_ticker, "from": c_from, "target_weight": max(0.0, c_to)}}


def _orders_from_legs(old_payload: dict, tickers, base: dict) -> list:
    """The sized orders of a requoted sized trade (§10.6): each requoted leg keeps the size the
    owner approved. A buy keeps its dollars; a sale keeps its share of the holding; a sale of
    all of it stays a sale of all of it."""
    exits = {c["ticker"] for c in ((old_payload.get("plan") or {}).get("changes") or []) if c.get("exit")}
    out = []
    for leg in old_payload.get("legs") or []:
        t = str(leg["ticker"]).upper()
        if t not in tickers:
            continue
        typ = leg.get("type") or "market"
        if typ != "market":
            # A limit or stop order keeps its shares and its prices.
            style = {"type": typ, "limit_price": leg.get("limit_price"), "stop_price": leg.get("stop_price"),
                     "good_till": "cancelled" if leg.get("time_in_force") == "gtc" else "day"}
            key = "buy_shares" if leg["side"] == "buy" else "sell_shares"
            out.append({"ticker": t, key: float(leg["quantity"]), **style})
            continue
        if leg["side"] == "buy":
            out.append({"ticker": t, "buy_dollars": float(leg["dollar_amount"])})
        elif t in exits:
            out.append({"ticker": t, "sell_all": True})
        else:
            held = base["quantities"].get(t, 0.0)
            frac = min(1.0, float(leg["quantity"]) / held) if held > 0 else 1.0
            out.append({"ticker": t, "sell_fraction": round(frac, 9)})
    return out


def _plan_subset(plan: Optional[dict], tickers) -> Optional[dict]:
    """The part of a card's plan move that belongs to ``tickers``; the cash sleeve takes the
    matching difference. None when no name of ``tickers`` moves."""
    if not plan:
        return None
    changes = [c for c in plan.get("changes") or [] if c["ticker"] in set(tickers)]
    if not changes:
        return None
    delta = sum(float(c["target_weight"]) - float(c["from"]) for c in changes)
    cash = dict(plan["cash"])
    cash["target_weight"] = round(float(cash["from"]) - delta, 4)
    return {"changes": changes, "cash": cash}


def build_trade(led, conn, *, cfg: Config, tickers, origin: dict, fraction=None, rationale: str,
                evidence=None, created_by: str, now_dt=None, role: Optional[str] = None,
                turn_owner_ids=None, python_origin: Optional[str] = None,
                parent_change_id: Optional[int] = None, requote_of: Optional[dict] = None,
                snap_override: Optional[dict] = None, yf_price_fn=None,
                only_side: Optional[str] = None, orders=None, fund_from=None,
                money_rule: Optional[str] = None) -> dict:
    """Build one pending trade proposal and reserve its legs. Runs inside the caller's
    transaction (``conn``). Raises ProposalError on any refusal; nothing is written then.

    ``origin`` is ``{"event_id": n}`` or ``{"owner_message_id": n}`` from a tool, or the
    ``python_origin`` (``change``, ``drift``) that only Python passes. ``requote_of`` is the
    replaced proposal row: its origin, rationale and evidence are copied, and the origin
    checks are skipped (§10.2 step 6).

    ``orders`` (an owner trade only) sizes the trade from the owner's request, and the
    payload's ``plan`` moves the planned shares to fit it when its orders fill (§10.6). A
    requote of such a trade keeps the sizes and the plan move (``sized`` in the payload).

    The money rule: an owner trade, and the follow-up of a change the owner made in chat
    (``money_rule="owner"``), may spend the cash that the plan keeps; the buffer and a set
    ``cash_floor_pct`` still bind. Every other trade keeps the plan's cash share."""
    now_dt = now_dt or _now_dt()
    now = store.iso(now_dt)
    orders = list(orders or [])
    if orders:
        if requote_of is not None or python_origin is not None:
            raise ProposalError("a sized order comes from the owner only")
        if not all(isinstance(o, dict) for o in orders):
            raise ProposalError("each order is {ticker, buy_dollars | sell_dollars | sell_fraction | target_weight | "
                                "sell_all}")
        names = [str(o.get("ticker") or "").upper().strip() for o in orders]
        tickers = names + [f for f in _fund_names(fund_from, cfg.risk.cash_sleeve_ticker) if f not in names]
    tickers = [str(t).upper().strip() for t in (tickers or []) if str(t).strip()]
    if not tickers:
        raise ProposalError("name at least one ticker")
    if len(set(tickers)) != len(tickers):
        raise ProposalError("each ticker once, please")
    rationale = str(rationale or "").strip()
    if len(rationale) > 280:
        raise ProposalError("rationale must be 280 characters or fewer")
    yf_price_fn = yf_price_fn or quotes.yf_last_price
    sett = desk_settings.effective(conn, cfg)

    # Origin resolution.
    event_id = owner_message_id = None
    side_rule = None
    old_payload = {}
    if requote_of is not None:
        kind_origin = requote_of["origin"]
        event_id = requote_of["event_id"]
        owner_message_id = requote_of["owner_message_id"]
        parent_change_id = requote_of["parent_change_id"]
        rationale = requote_of["rationale"]
        ev_items = evidence_items(store.loads(requote_of["evidence_json"], []))
        old_payload = store.loads(requote_of["payload_json"], {}) or {}
        fraction = float(old_payload.get("fraction", 1.0))
        side_rule = old_payload.get("side_rule")
    elif python_origin in ("change", "drift"):
        kind_origin = python_origin
        ev_items = evidence_items(evidence)
        fraction = 1.0
    else:
        if python_origin is not None:
            raise ProposalError(f"origin {python_origin} is not allowed here")
        origin = origin or {}
        ev_items = evidence_items(evidence)
        if "event_id" in origin and "owner_message_id" not in origin:
            kind_origin = "event"
            event_id = int(origin["event_id"])
        elif "owner_message_id" in origin and "event_id" not in origin:
            kind_origin = "owner"
            owner_message_id = int(origin["owner_message_id"])
        else:
            raise ProposalError('origin must be {"event_id": n} or {"owner_message_id": n}')
    if orders and kind_origin != "owner":
        raise ProposalError("only an owner trade takes a sized order")
    # The money rule (see the docstring). Only Python passes money_rule, for a follow-up.
    if requote_of is not None:
        money_rule = "owner" if old_payload.get("money_rule") == "owner" else "plan"
    elif kind_origin == "owner" or (kind_origin == "change" and money_rule == "owner"):
        money_rule = "owner"
    else:
        money_rule = "plan"

    if not rationale:
        raise ProposalError("give a rationale")

    # Step 1: the snapshot, KILL and the halt.
    snap = snap_override or latest_snapshot(conn)
    if snap is None:
        raise ProposalError("portfolio snapshot is stale; refresh it first")
    snap_age = now_dt - store.parse_ts(snap["captured_at"])
    if snap_age > SNAPSHOT_MAX_AGE:
        raise ProposalError("portfolio snapshot is stale; refresh it first")
    if kind_origin in ("event", "owner", "drift") and requote_of is None:
        if kill_active(cfg):
            raise ProposalError("trading is stopped on the box (KILL); no new trade proposal")
        if led.is_halted(today(), conn=conn):
            raise ProposalError("trading is halted for today after the daily-loss check")

    # Step 2: the book.
    goal = active_goal(conn)
    if goal is None:
        raise ProposalError("no active book; run desk-seed")
    rows = {r["ticker"]: r for r in book_rows(conn, goal["id"])}
    for t in tickers:
        if t not in rows:
            raise ProposalError(f"{t} is not in the book; propose add_company (from the owner's message it applies "
                                "at once, and its buy card follows)")
        hold = in_flight_ticker(conn, t)
        if hold:
            held = held_job_of_leg(conn, hold, store.iso(now_dt))
            if held:
                raise ProposalError(f"{t} has an approved order (#{held[0]}) that I send at "
                                    f"{market.fmt_when_et(store.parse_ts(held[1]), now_dt)}. To change it, the owner "
                                    "taps Cancel on that card first")
            raise ProposalError(f"{t} has an order in flight. Wait for the fill.")
    cash_ticker = cfg.risk.cash_sleeve_ticker

    # Origin checks and the fraction.
    if requote_of is None and kind_origin == "event":
        ev = _check_event_origin(conn, event_id, tickers, ev_items, now_dt)
        side_rule = allowed_event_trade(ev)
        fraction = side_rule["fraction"]
    elif requote_of is None and kind_origin == "owner":
        if role != "desk":
            raise ProposalError("only the desk can propose an owner trade")
        if owner_message_id not in set(turn_owner_ids or ()):
            raise ProposalError("that owner message is not part of this turn")
        if orders:
            fraction = 1.0          # the orders give the size
        elif fraction is None or float(fraction) not in FRACTIONS:
            raise ProposalError("fraction must be 0.25, 0.5 or 1.0 (or give orders with a dollar size)")
        fraction = float(fraction)
    fraction = float(fraction if fraction is not None else 1.0)
    if fraction not in FRACTIONS:
        raise ProposalError("fraction must be 0.25, 0.5 or 1.0")

    # Step 3: sizing.
    base = valuation_base(conn, snap, now_dt, yf_price_fn, snap_override)
    deployable = base["deployable"]
    bp = float(snap["buying_power"])
    min_notional = max(float(sett["min_trade_notional"]), sizing.MIN_SELL_NOTIONAL_USD)
    all_rows = book_rows(conn, goal["id"], statuses=("active", "exiting", "removed"))
    cash_value = base["values"].get(cash_ticker, 0.0)
    hard_floor = sett.get("cash_floor_pct")
    if money_rule == "owner":
        floor_pct = float(hard_floor) if hard_floor is not None else 0.0
    else:
        floor_pct = desk_settings.cash_floor_pct(sett, all_rows, cash_ticker)
    cash_floor_dollars = floor_pct / 100.0 * deployable
    buffer = float(sett["min_buying_power_buffer"])
    money = min(bp - buffer, bp + cash_value - cash_floor_dollars)
    if money_rule == "owner" and hard_floor is None:
        money_text = f"cash is ${bp:,.2f}, and ${buffer:,.2f} stays as a buffer"
    elif money_rule == "owner":
        money_text = (f"cash is ${bp:,.2f}, ${buffer:,.2f} stays as a buffer, and the cash floor keeps "
                      f"{floor_pct:g}% of the account (${cash_floor_dollars:,.2f})")
    else:
        money_text = (f"cash is ${bp:,.2f}, and the plan keeps {floor_pct:g}% of the account "
                      f"(${cash_floor_dollars:,.2f}) in cash")

    sells, buy_wants, skipped, buys = [], [], [], []
    plan = None
    # A requote of a sized trade keeps the owner's sizes and carries the card's plan move
    # (the plan moves only when an order fills, so nothing of it is applied yet).
    sized = bool(orders) or (requote_of is not None and bool(old_payload.get("sized")))
    if sized:
        price_of = (lambda t: _leg_quote(conn, t, now_dt, yf_price_fn, snap_override))
        if orders:
            sells, buys = _size_orders(rows, base, orders, fund_from, cash_ticker=cash_ticker, price_of=price_of,
                                       min_notional=min_notional, money=money, money_text=money_text,
                                       cap=float(sett["per_name_max_pct"]))
            plan = _trade_plan(rows, all_rows, sells + buys, base, deployable, cash_ticker=cash_ticker,
                               cap=float(sett["per_name_max_pct"]), hard_floor=hard_floor)
        else:
            sells, buys = _size_orders(rows, base, _orders_from_legs(old_payload, tickers, base), None,
                                       cash_ticker=cash_ticker, price_of=price_of, min_notional=min_notional,
                                       money=money, money_text=money_text, cap=float(sett["per_name_max_pct"]))
            plan = _plan_subset(old_payload.get("plan"), {l["ticker"] for l in sells + buys})
            for leg in sells + buys:
                for ch in (plan or {}).get("changes") or []:
                    if ch["ticker"] == leg["ticker"]:
                        leg["target_weight"] = float(ch["target_weight"])
        tickers = [t for t in tickers if t in {l["ticker"] for l in sells + buys}]
    for t in ([] if sized else tickers):
        r = rows[t]
        if t == cash_ticker:
            skipped.append({"ticker": t, "reason": "the cash sleeve is held as cash; it gets no leg"})
            continue
        price, qsrc = _leg_quote(conn, t, now_dt, yf_price_fn, snap_override)
        if price is None:
            raise ProposalError(f"no quote for {t}; refresh the portfolio first")
        held = base["quantities"].get(t, 0.0)
        v = base["values"].get(t, 0.0)
        T = float(r["target_weight"] or 0.0) / 100.0 * deployable
        weight_now = v / deployable * 100.0 if deployable > 0 else 0.0
        if r["status"] == "exiting":
            if held <= 0:
                skipped.append({"ticker": t, "reason": "nothing is held"})
                continue
            raw = held if fraction == 1.0 else sizing.resolve_sell_quantity(held, fraction)
            qty, why = sizing.resolve_sell_quantity_min_notional(
                held, raw, price, min_notional=sizing.MIN_SELL_NOTIONAL_USD, bump_to_min=True)
            if fraction == 1.0 and qty > 0:
                qty = round(held, 6)
            if qty <= 0:
                skipped.append({"ticker": t, "reason": f"the exit is too small to send ({why})"})
                continue
            sells.append({"ticker": t, "side": "sell", "quantity": qty, "quote": price,
                          "quote_source": qsrc, "weight_now": weight_now,
                          "target_weight": float(r["target_weight"] or 0.0), "exit": True,
                          "band": r.get("band")})
            continue
        gap = T - v
        if side_rule and side_rule.get("side") == "sell" and gap >= 0:
            raise ProposalError(f"{t} is not above its target, so this event allows no sell")
        if side_rule and side_rule.get("side") == "buy" and gap <= 0:
            raise ProposalError(f"{t} is at or above its target, so this event allows no buy")
        if only_side == "buy" and gap <= 0:
            skipped.append({"ticker": t, "reason": "it is at or above its target"})
            continue
        if abs(gap) < min_notional:
            skipped.append({"ticker": t, "reason": f"it is within ${min_notional:.2f} of its target"})
            continue
        if gap > 0:
            emax = _f(r.get("entry_price_max"))
            if emax is not None and price > emax:
                skipped.append({"ticker": t, "reason": f"its price ${price:,.2f} is above the ${emax:,.2f} entry limit"})
                continue
            want = round(gap * fraction, 2)
            if want < min_notional:
                skipped.append({"ticker": t, "reason": f"the buy is under ${min_notional:.2f}"})
                continue
            buy_wants.append({"ticker": t, "side": "buy", "want": want, "gap": gap, "quote": price,
                              "quote_source": qsrc, "weight_now": weight_now,
                              "target_weight": float(r["target_weight"] or 0.0), "band": r.get("band")})
        else:
            raw = sizing.resolve_target_sell_quantity(held, price, v, T)
            raw = round(raw * fraction, 6)
            qty, why = sizing.resolve_sell_quantity_min_notional(
                held, raw, price, min_notional=min_notional, bump_to_min=False)
            if qty <= 0:
                skipped.append({"ticker": t, "reason": f"the trim is too small to send ({why})"})
                continue
            sells.append({"ticker": t, "side": "sell", "quantity": qty, "quote": price,
                          "quote_source": qsrc, "weight_now": weight_now,
                          "target_weight": float(r["target_weight"] or 0.0), "exit": False,
                          "band": r.get("band")})

    # Step 4: buys may use the estimated sell proceeds; scale pro rata when short. The
    # sized orders of an owner trade are already checked against the money (no scaling).
    proceeds = sum(s["quantity"] * s["quote"] for s in sells)
    available = max(0.0, money) + proceeds
    total_want = sum(b["want"] for b in buy_wants)
    scaled = False
    if buy_wants:
        if total_want > available + 1e-9:
            scaled = True
            for b in buy_wants:
                b["want_scaled"] = round(b["want"] * available / total_want, 2)
        for b in buy_wants:
            amount = b.get("want_scaled", b["want"])
            dollars, _src = sizing.resolve_buy_dollars(amount, 1.0, available=amount,
                                                       min_notional=min_notional)
            if dollars <= 0:
                why = ("there is no cash to spend: " + money_text if available < min_notional
                       else f"the buy scaled to the cash left is under the ${min_notional:.2f} minimum ({money_text})")
                skipped.append({"ticker": b["ticker"], "reason": why})
                continue
            b["dollars"] = dollars
            buys.append(b)
    legs = sells + buys
    if not legs:
        reasons = "; ".join(f"{s['ticker']}: {s['reason']}" for s in skipped) or "nothing to do"
        hint = (". To own more or less than the plan, give orders with a dollar size or a new planned share: "
                "Python moves the plan to fit" if kind_origin == "owner" else "")
        raise ProposalError(f"no order to propose ({reasons}){hint}")
    # A trade against a recent one needs new evidence. The owner's own request is the reason
    # for an owner trade, so the rule binds the event trades only.
    if requote_of is None and kind_origin == "event":
        for leg in legs:
            _check_anti_flip(conn, leg["ticker"], leg["side"], ev_items, now_dt)
    buys_total = sum(float(b.get("dollars") or 0.0) for b in buys)
    cash_info = {"before": round(bp, 2), "after": round(bp - buys_total + proceeds, 2)}

    # Expiry and the leg trade date (the session date of expires_at, G7).
    session_date, close_dt = market.session_close_et(now_dt.astimezone(market.ET))
    expires_at = store.iso(close_dt)

    payload_legs = []
    for seq, leg in enumerate(legs, start=1):
        leg["seq"] = seq
        leg["ref_id"] = str(uuid.uuid4())
        payload_legs.append({"seq": seq, "ref_id": leg["ref_id"], "ticker": leg["ticker"],
                             "side": leg["side"], "dollar_amount": leg.get("dollars"),
                             "quantity": leg.get("quantity"), "quote": leg["quote"],
                             "type": leg.get("type") or "market", "limit_price": leg.get("limit_price"),
                             "stop_price": leg.get("stop_price"), "time_in_force": leg.get("time_in_force") or "gfd"})
    payload = {"kind": "trade", "fraction": fraction, "tickers": tickers, "snapshot_id": snap["id"],
               "deployable": round(deployable, 2), "legs": payload_legs, "scaled": scaled,
               "skipped": skipped, "side_rule": side_rule, "money_rule": money_rule, "cash": cash_info,
               "sized": sized}
    if plan is not None:
        payload["plan"] = plan
    payload_json = store.canonical_json(payload)
    created_by = created_by if created_by in ("desk", "analyst", "monitor", "system") else "system"
    cur = conn.execute(
        "INSERT INTO proposals (kind, origin, event_id, owner_message_id, parent_change_id, "
        "payload_json, payload_sha256, rationale, evidence_json, created_by, created_at, "
        "expires_at, state, nonce) VALUES ('trade', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (kind_origin, event_id, owner_message_id, parent_change_id, payload_json,
         store.sha256_hex(payload_json), rationale,
         json.dumps([f"{k}:{v}" for k, v in ev_items]), created_by, now, expires_at,
         secrets.token_hex(5)))
    pid = int(cur.lastrowid)
    for leg in legs:
        led.reserve_order(leg["ref_id"], session_date, leg["ticker"], side=leg["side"],
                          type=leg.get("type") or "market", dollar_amount=leg.get("dollars"),
                          quantity=leg.get("quantity"), limit_price=leg.get("limit_price"),
                          stop_price=leg.get("stop_price"), time_in_force=leg.get("time_in_force") or "gfd",
                          now_iso=now, state="proposed", proposal_id=pid, leg_seq=leg["seq"],
                          quote_at_proposal=leg["quote"], conn=conn)

    # Supersede: every other pending trade proposal on one of these tickers, and (for a
    # drift proposal) every pending drift proposal.
    superseded = []
    others = conn.execute("SELECT DISTINCT p.id FROM proposals p JOIN orders o ON o.proposal_id=p.id "
                          "WHERE p.kind='trade' AND p.state='pending' AND p.id != ? AND o.ticker IN (%s)"
                          % ",".join("?" for _ in tickers), (pid, *tickers)).fetchall()
    ids = {r[0] for r in others}
    if kind_origin == "drift":
        ids |= {r[0] for r in conn.execute("SELECT id FROM proposals WHERE kind='trade' AND "
                                           "origin='drift' AND state='pending' AND id != ?", (pid,))}
    for old in sorted(ids):
        if end_pending(conn, old, "superseded", now, superseded_by=pid):
            superseded.append(old)
    if requote_of is not None:
        conn.execute("UPDATE proposals SET superseded_by=? WHERE id=?", (pid, requote_of["id"]))

    for leg in legs:
        leg["weight_after"] = _weight_after(leg, base, deployable)
    return {"proposal_id": pid, "legs": legs, "skipped": skipped, "scaled": scaled,
            "fraction": fraction, "deployable": deployable, "expires_at": expires_at,
            "superseded": superseded, "origin": kind_origin, "snapshot_id": snap["id"],
            "plan": plan, "cash": cash_info, "money_rule": money_rule}


class _PreviewDone(Exception):
    """Ends a preview: the transaction rolls back and nothing stays written."""


def preview_trade(led, cfg: Config, **kw) -> dict:
    """The trade that ``build_trade`` would propose, with nothing written (§10.6). The build
    runs in a transaction that always rolls back, so the numbers are the card's numbers:
    the legs, the plan move and the cash after. The desk talks a size through with the
    owner from these, never from its own arithmetic. Raises ProposalError like the build."""
    out = {}
    try:
        with led.transaction() as conn:
            out.update(build_trade(led, conn, cfg=cfg, **kw))
            raise _PreviewDone()
    except _PreviewDone:
        pass
    out.pop("proposal_id", None)
    out.pop("superseded", None)
    out.pop("expires_at", None)
    return out


def _weight_after(leg: dict, base: dict, deployable: float) -> float:
    v = base["values"].get(leg["ticker"], 0.0)
    if leg["side"] == "buy":
        v += float(leg.get("dollars") or 0.0)
    else:
        v -= float(leg.get("quantity") or 0.0) * float(leg["quote"])
    return max(0.0, v) / deployable * 100.0 if deployable > 0 else 0.0


def end_pending(conn, proposal_id: int, new_state: str, now: str, *, superseded_by=None) -> bool:
    """Move a ``pending`` proposal to expired, superseded or withdrawn in ONE conditional
    statement, and finalize its open legs (§10.1 rule 6). A row that already left
    ``pending`` (an approval, say) is never touched. Returns True when it moved."""
    if new_state not in ("expired", "superseded", "withdrawn"):
        raise ValueError(new_state)
    cur = conn.execute("UPDATE proposals SET state=?, superseded_by=COALESCE(?, superseded_by) "
                       "WHERE id=? AND state='pending'", (new_state, superseded_by, proposal_id))
    if cur.rowcount != 1:
        return False
    leg_state = "expired" if new_state == "expired" else "skipped"
    conn.execute("UPDATE orders SET state=?, finalized=1, skip_reason=COALESCE(skip_reason, ?) "
                 "WHERE proposal_id=? AND state='proposed'", (leg_state, new_state, proposal_id))
    return True


def requote_move(conn, proposal_id: int) -> bool:
    """``executing`` → ``requoted`` in one conditional statement; finalize the legs that
    were never sent (``approved``/``checked``). A leg in ``placing``, ``placed`` or
    ``partially_filled`` is never finalized by a requote."""
    cur = conn.execute("UPDATE proposals SET state='requoted' WHERE id=? AND state='executing'",
                       (proposal_id,))
    if cur.rowcount != 1:
        return False
    conn.execute("UPDATE orders SET state='requoted', finalized=1 WHERE proposal_id=? "
                 "AND state IN ('proposed','approved','checked')", (proposal_id,))
    return True


def expire_due(conn, now_dt) -> list:
    """Expire every pending proposal whose ``expires_at`` passed (§9.1 step 8). Returns
    the rows moved (id, kind, tg ids) so the caller edits their buttons to "Expired"."""
    now = store.iso(now_dt)
    rows = conn.execute("SELECT id, kind, tg_chat_id, tg_message_id FROM proposals "
                        "WHERE state='pending' AND expires_at <= ?", (now,)).fetchall()
    moved = []
    for r in rows:
        if end_pending(conn, r[0], "expired", now):
            moved.append({"id": r[0], "kind": r[1], "tg_chat_id": r[2], "tg_message_id": r[3]})
    return moved


def withdraw(conn, proposal_id: int, *, created_by: str, now: str) -> bool:
    row = conn.execute("SELECT created_by, state FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise ProposalError(f"proposal #{proposal_id} does not exist")
    if row[0] not in ("desk", "analyst"):
        raise ProposalError(f"proposal #{proposal_id} was not made by the desk")
    if row[1] != "pending":
        raise ProposalError(f"proposal #{proposal_id} is {row[1]}, not pending")
    return end_pending(conn, proposal_id, "withdrawn", now)


def get_proposal(conn, proposal_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (int(proposal_id),)).fetchone()
    return dict(row) if row else None


# --- closing a proposal from its legs (§11.1 step 9) --------------------------------

def _leg_skip_reason(leg: dict) -> str:
    review = store.loads(leg.get("review_json"), {}) or {}
    data = review.get("data") if isinstance(review, dict) else None
    flags = [review, data if isinstance(data, dict) else {}]
    if any(isinstance(x, dict) and x.get("customer_approval_required") is True for x in flags):
        return "needs_app_approval"
    oc = (data or {}).get("order_checks") if isinstance(data, dict) else None
    if oc not in (None, {}, []):
        return "review_alert"
    return "not_placed"


def close_from_legs(led, proposal_id: int, now, *, reason: Optional[str] = None) -> str:
    """Close a trade proposal after its job (§11.1 step 9). Changes only ledger rows and
    returns the result text for ``outbox`` (empty when nothing changed to report).

    Never-sent legs (proposed, approved, checked) end ``skipped`` with a reason. A leg in
    ``placing`` waits for the reconcile. While any leg is open at Robinhood (placing,
    placed, partially_filled) the proposal stays ``executing``. When none is open, the
    proposal ends executed, partially_executed or failed."""
    from lib import notify
    now_s = now if isinstance(now, str) else store.iso(now)
    with led.transaction() as conn:
        prop = get_proposal(conn, proposal_id)
        if prop is None or prop["kind"] != "trade":
            return ""
        legs = led.legs(proposal_id, conn=conn)
        newly_skipped = []
        for leg in legs:
            if leg["state"] in ("proposed", "approved", "checked"):
                why = _leg_skip_reason(leg) if leg["state"] == "checked" else "not_placed"
                conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason=? "
                             "WHERE ref_id=? AND state=?", (why, leg["ref_id"], leg["state"]))
                newly_skipped.append({**leg, "skip_reason": why})
        legs = led.legs(proposal_id, conn=conn)
        open_legs = [l for l in legs if l["state"] in OPEN_AT_BROKER]
        result = {"legs": [{"ref_id": l["ref_id"], "ticker": l["ticker"], "state": l["state"]}
                           for l in legs]}
        if open_legs:
            if prop["state"] in ("approved", "executing"):
                conn.execute("UPDATE proposals SET state='executing' WHERE id=? AND state IN "
                             "('approved','executing')", (proposal_id,))
            text = notify.render_execution_result(_result_model(prop, legs, reason, open_now=True))
            already = (store.loads(prop.get("result_json"), {}) or {}).get("open_reported")
            conn.execute("UPDATE proposals SET result_json=? WHERE id=?",
                         (json.dumps({**(store.loads(prop.get("result_json"), {}) or {}),
                                      **result, "open_reported": True}), proposal_id))
            return "" if already and not newly_skipped else text
        if prop["state"] not in ("approved", "executing"):
            return ""
        filled = [l for l in legs if l["state"] == "filled"]
        # A cancelled order with a fill (Robinhood cancels the rest of a GFD order) is partial.
        partial = [l for l in legs if l["state"] == "partially_filled" or
                   (l["state"] == "cancelled" and float(l.get("fill_quantity") or 0.0) > 0)]
        if legs and len(filled) == len(legs):
            final = "executed"
        elif filled or partial:
            final = "partially_executed"
        else:
            final = "failed"
        merged = {**(store.loads(prop.get("result_json"), {}) or {}), **result, "reason": reason}
        cur = conn.execute("UPDATE proposals SET state=?, result_json=? WHERE id=? AND state IN "
                           "('approved','executing')", (final, json.dumps(merged), proposal_id))
        if cur.rowcount != 1:
            return ""
        if final != "failed":
            # A sized owner trade: the plan moves for the names whose orders filled (§10.6).
            done = {l["ticker"] for l in legs if l["state"] == "filled" or float(l.get("fill_quantity") or 0.0) > 0}
            apply_plan_moves(led, conn, {**prop, "result_json": json.dumps(merged)}, done, now_s)
        return notify.render_execution_result(_result_model({**prop, "state": final}, legs, reason))


def _result_model(prop: dict, legs: list, reason: Optional[str], open_now: bool = False) -> dict:
    return {"id": prop["id"], "state": prop["state"], "reason": reason, "open_now": open_now,
            "legs": [{"ticker": l["ticker"], "side": l["side"], "state": l["state"],
                      "broker_state": (store.loads(l.get("result_json"), {}) or {}).get("state")
                      if str(l.get("result_json") or "").startswith("{") else None,
                      "dollar_amount": l.get("dollar_amount"), "quantity": l.get("quantity"),
                      "fill_quantity": l.get("fill_quantity"), "fill_avg_price": l.get("fill_avg_price"),
                      "skip_reason": l.get("skip_reason"), "fill_state": l.get("fill_state") or None,
                      "submitted_at": l.get("submitted_at")}
                     for l in legs]}


# --- card data -------------------------------------------------------------------

def trade_card_model(conn, built: dict, *, now_dt=None) -> dict:
    """The data ``notify.render_trade_card`` needs for a freshly built trade."""
    prop = get_proposal(conn, built["proposal_id"])
    payload = store.loads(prop.get("payload_json"), {}) or {}
    model = {"id": prop["id"], "origin": prop["origin"], "rationale": prop["rationale"],
             "legs": built["legs"], "skipped": built["skipped"], "scaled": built["scaled"],
             "expires_at": prop["expires_at"], "now": store.iso(now_dt or _now_dt()),
             "evidence": store.loads(prop["evidence_json"], []), "drift": prop["origin"] == "drift",
             "plan": payload.get("plan"),
             "cash": payload.get("cash") if payload.get("money_rule") == "owner" else None}
    held = market.held_send_et(now_dt or _now_dt())
    if held is not None:
        # The market cannot take the order now: a tap approves it and holds it to this time.
        model["send_at"] = store.iso(held[0])
        model["closing"] = market.is_open_at(now_dt or _now_dt())
    if prop["owner_message_id"] is not None:
        row = conn.execute("SELECT text, created_at FROM chat_messages WHERE role='owner' "
                           "AND tg_message_id=? ORDER BY id DESC LIMIT 1",
                           (prop["owner_message_id"],)).fetchone()
        if row:
            model["owner_quote"] = {"text": row[0], "at": row[1]}
    if prop["event_id"] is not None:
        ev = store.get_event(conn, prop["event_id"])
        if ev and ev["created_by"] == "desk":
            detail = store.loads(ev["detail_json"], {}) or {}
            d = store.get_document(conn, detail.get("document_id") or 0)
            model["desk_news"] = {"url": (d or {}).get("url")}
        if ev:
            model["event"] = {"type": ev["type"], "verdict": ev["verdict"], "ticker": ev["ticker"]}
    return model


# --- changes (§10.3) ---------------------------------------------------------------

def _book_sum(rows: list) -> float:
    return round(sum(float(r["target_weight"] or 0.0) for r in rows if r["status"] == "active"), 6)


def _validate_book(rows: list, sett: dict, cash_ticker: str, tolerance: float = 0.01) -> None:
    total = _book_sum(rows)
    if abs(total - 100.0) > tolerance:
        raise ProposalError(f"the book must sum to 100% (it sums to {total:g}%)")
    for r in rows:
        if r["status"] == "active" and float(r["target_weight"] or 0) < -1e-9:
            if r["ticker"] == cash_ticker:
                raise ProposalError(f"the plan keeps too little cash for this: the cash sleeve would be "
                                    f"{float(r['target_weight']):.2f}%. Lower another name to pay for it")
            raise ProposalError(f"{r['ticker']} at {float(r['target_weight']):g}% is under 0%")
    cap = float(sett["per_name_max_pct"])
    for r in rows:
        if r["status"] == "active" and r["ticker"] != cash_ticker and float(r["target_weight"] or 0) > cap + 1e-9:
            raise ProposalError(f"{r['ticker']} at {float(r['target_weight']):g}% is over the "
                                f"{cap:g}% per-name limit")
    floor = sett.get("cash_floor_pct")
    if floor is not None:
        cash = sum(float(r["target_weight"] or 0) for r in rows
                   if r["ticker"] == cash_ticker and r["status"] == "active")
        if cash + 1e-9 < float(floor):
            raise ProposalError(f"the cash sleeve at {cash:g}% is under the {float(floor):g}% cash floor")


def _apply_weights(rows: list, changes: list) -> list:
    by = {r["ticker"]: dict(r) for r in rows}
    for ch in changes:
        t = str(ch["ticker"]).upper()
        if t not in by:
            raise ProposalError(f"{t} is not in the book")
        by[t]["target_weight"] = float(ch["target_weight"])
        if ch.get("band") is not None:
            by[t]["band"] = float(ch["band"])
    return list(by.values())


def _balance_on_cash(rows: list, named, cash_ticker: str) -> Optional[dict]:
    """Close the plan on the cash sleeve. When a change does not name the cash sleeve and
    the plan no longer sums to 100%, the cash sleeve takes the difference, so the desk never
    does the sum. Returns the cash entry to add to the change, or None."""
    if cash_ticker in {str(t).upper() for t in named}:
        return None
    gap = round(100.0 - _book_sum(rows), 6)
    if abs(gap) <= 1e-6:
        return None
    for r in rows:
        if r["ticker"] == cash_ticker and r["status"] == "active":
            r["target_weight"] = round(float(r["target_weight"] or 0.0) + gap, 4)
            return {"ticker": cash_ticker, "target_weight": r["target_weight"]}
    return None


def validate_change(conn, cfg: Config, change: dict, *, now_dt=None) -> dict:
    """Validate a change against the current state. Returns a normalized change with the
    preview the card shows. Raises ProposalError. Runs at propose time and again at apply
    time (§10.3)."""
    from lib.desk import valuation  # research module; Python computes the fair values
    kind = (change or {}).get("kind")
    if kind not in CHANGE_KINDS:
        raise ProposalError(f"change kind must be one of {', '.join(CHANGE_KINDS)}")
    sett = desk_settings.effective(conn, cfg)
    cash_ticker = cfg.risk.cash_sleeve_ticker
    goal = active_goal(conn)
    if goal is None and kind not in ("set_premise", "cancel_order"):
        raise ProposalError("no active book; run desk-seed")
    rows = book_rows(conn, goal["id"], statuses=("active", "exiting", "removed")) if goal else []
    by = {r["ticker"]: r for r in rows}
    out = {"kind": kind}
    if kind == "cancel_order":
        from lib.desk import brokerq
        oid = str(change.get("order_id") or "").strip()
        if not brokerq.ORDER_ID_RE.match(oid):
            raise ProposalError("cancel_order needs order_id: the Robinhood id of the open order (read it with "
                                "broker_read)")
        t = str(change.get("ticker") or "").upper().strip()
        if not t:
            raise ProposalError("cancel_order needs the ticker of the order")
        about = str(change.get("about") or "").strip()
        if len(about) > 120:
            raise ProposalError("about is one short line (120 characters at most)")
        out.update(order_id=oid, ticker=t, about=about, advanced=bool(change.get("advanced")))
        return out
    if kind == "add_company":
        t = str(change.get("ticker", "")).upper()
        comp = store.get_company(conn, t)
        if comp is None:
            raise ProposalError(f"{t} is not loaded; run load_company first")
        if t in by and by[t]["status"] == "active":
            raise ProposalError(f"{t} is already in the book")
        w = _f(change.get("target_weight"))
        band = _f(change.get("band"))
        if w is None or w <= 0:
            raise ProposalError("target_weight must be a positive number")
        if band is None or band < 0:
            band = cfg.risk.rebalance_drift_band_pct
        entry = change.get("entry") or {"mode": "now"}
        mode = entry.get("mode")
        if mode not in ("now", "at_or_below"):
            raise ProposalError("entry.mode must be now or at_or_below")
        price = _f(entry.get("price"))
        if mode == "at_or_below" and (price is None or price <= 0):
            raise ProposalError("entry at_or_below needs a price")
        funding = list(change.get("funding") or [])
        new_rows = [dict(r) for r in rows if r["ticker"] != t]
        new_rows = _apply_weights(new_rows, funding)
        new_rows.append({"ticker": t, "target_weight": w, "band": band, "status": "active"})
        cash_entry = _balance_on_cash(new_rows, [t] + [f.get("ticker") for f in funding], cash_ticker)
        if cash_entry:
            funding.append(cash_entry)
        _validate_book(new_rows, sett, cash_ticker)
        out.update(ticker=t, sleeve=change.get("sleeve") or comp.get("sleeve"), target_weight=w,
                   band=band, entry={"mode": mode, "price": price},
                   funding=[{"ticker": str(f["ticker"]).upper(), "target_weight": float(f["target_weight"])}
                            for f in funding])
    elif kind == "drop_company":
        t = str(change.get("ticker", "")).upper()
        snap = latest_snapshot(conn)
        held = t in ((snap or {}).get("positions") or {})
        if t not in by and not held:
            raise ProposalError(f"{t} is neither in the book nor held")
        ex = change.get("exit") or {"mode": "now"}
        mode = ex.get("mode")
        if mode not in ("now", "at_or_above"):
            raise ProposalError("exit.mode must be now or at_or_above")
        price = _f(ex.get("price"))
        if mode == "at_or_above" and (price is None or price <= 0):
            raise ProposalError("exit at_or_above needs a price")
        redistribute = list(change.get("redistribute") or [])
        new_rows = [dict(r) for r in rows if r["ticker"] != t]
        new_rows = _apply_weights(new_rows, redistribute)
        cash_entry = _balance_on_cash(new_rows, [t] + [f.get("ticker") for f in redistribute], cash_ticker)
        if cash_entry:
            redistribute.append(cash_entry)
        _validate_book(new_rows, sett, cash_ticker)
        out.update(ticker=t, exit={"mode": mode, "price": price}, unmanaged=t not in by,
                   redistribute=[{"ticker": str(f["ticker"]).upper(),
                                  "target_weight": float(f["target_weight"])} for f in redistribute])
    elif kind == "set_weights":
        changes = list(change.get("changes") or [])
        if not changes:
            raise ProposalError("set_weights needs at least one change")
        new_rows = _apply_weights([dict(r) for r in rows], changes)
        cash_entry = _balance_on_cash(new_rows, [c.get("ticker") for c in changes], cash_ticker)
        if cash_entry:
            changes.append(cash_entry)
        _validate_book(new_rows, sett, cash_ticker)
        out["changes"] = [{"ticker": str(c["ticker"]).upper(), "target_weight": float(c["target_weight"]),
                           "band": _f(c.get("band"))} for c in changes]
    elif kind == "set_risk":
        key = change.get("key")
        try:
            value = desk_settings.validate(key, change.get("value"))
        except desk_settings.SettingError as e:
            raise ProposalError(str(e))
        out.update(key=key, value=value, old=sett.get(key))
    elif kind == "set_premise":
        text = str(change.get("text") or "")
        if len(text) < 200 or len(text) > 6000:
            raise ProposalError("the premise text must be 200 to 6,000 characters")
        g = change.get("goal") or {}
        tr, hm = _f(g.get("target_return_pct")), _f(g.get("horizon_months"))
        if tr is None or hm is None or hm <= 0:
            raise ProposalError("goal needs target_return_pct and horizon_months")
        out.update(text=text, goal={"target_return_pct": tr, "horizon_months": int(hm),
                                    "benchmark": g.get("benchmark") or (goal or {}).get("benchmark")})
    elif kind == "set_thesis":
        t = str(change.get("ticker", "")).upper()
        comp = store.get_company(conn, t)
        if comp is None:
            raise ProposalError(f"{t} is not loaded; run load_company first")
        for k in ("premise_link", "summary"):
            if not str(change.get(k) or "").strip():
                raise ProposalError(f"set_thesis needs {k}")
        val = change.get("valuation") or {}
        try:
            fv = valuation.fair_values(conn, t, val, settings=sett, cik=comp.get("cik"))
        except valuation.ValuationError as e:
            raise ProposalError(str(e))
        bb = _f(change.get("buy_below"))
        ta = _f(change.get("trim_above"))
        buy_below = bb if bb is not None else fv.get("buy_below")
        trim_above = ta if ta is not None else fv.get("trim_above")
        out.update(ticker=t, premise_link=str(change["premise_link"]), summary=str(change["summary"]),
                   drivers=list(change.get("drivers") or []), falsifiers=list(change.get("falsifiers") or []),
                   valuation=val, fair_value_low=fv.get("fair_value_low"),
                   fair_value_base=fv.get("fair_value_base"), fair_value_high=fv.get("fair_value_high"),
                   buy_below=buy_below, trim_above=trim_above,
                   evidence=list(change.get("evidence") or []))
    elif kind == "resume_trading":
        if not _is_halted_conn(conn, today()):
            raise ProposalError("trading is not halted today")
        # The card names the halt: a daily-loss halt and an audit halt set the same flag.
        row = conn.execute("SELECT halt_reason FROM day_baseline WHERE trade_date=?", (today(),)).fetchone()
        out["halt_reason"] = str(row[0] or "") if row else ""
    return out


def _is_halted_conn(conn, day: str) -> bool:
    row = conn.execute("SELECT halted FROM day_baseline WHERE trade_date=?", (day,)).fetchone()
    return bool(row[0]) if row else False


def follow_up_tickers(conn, cfg, change: dict) -> list:
    """The tickers whose trades follow a confirmed change (§10.4)."""
    kind = change["kind"]
    cash = cfg.risk.cash_sleeve_ticker
    if kind == "add_company" and change["entry"]["mode"] == "now":
        return [change["ticker"]] + [f["ticker"] for f in change.get("funding", []) if f["ticker"] != cash]
    if kind == "drop_company" and change["exit"]["mode"] == "now":
        return [change["ticker"]] + [f["ticker"] for f in change.get("redistribute", []) if f["ticker"] != cash]
    if kind == "set_weights":
        return [c["ticker"] for c in change["changes"] if c["ticker"] != cash]
    if kind == "set_thesis" and change.get("buy_below") is not None:
        return [change["ticker"]]
    return []


def build_change(conn, cfg: Config, *, change: dict, rationale: str, evidence=None,
                 created_by: str, now_dt=None) -> dict:
    """Validate and store one pending change proposal. Returns ``{proposal_id, change}``."""
    now_dt = now_dt or _now_dt()
    rationale = str(rationale or "").strip()
    if not rationale or len(rationale) > 280:
        raise ProposalError("give a rationale of 280 characters or fewer")
    ev_items = evidence_items(evidence)
    norm = validate_change(conn, cfg, change, now_dt=now_dt)
    _session, close_dt = market.session_close_et(now_dt.astimezone(market.ET))
    payload = {"kind": "change", "change": norm}
    payload_json = store.canonical_json(payload)
    cur = conn.execute(
        "INSERT INTO proposals (kind, origin, payload_json, payload_sha256, rationale, evidence_json, "
        "created_by, created_at, expires_at, state, nonce) VALUES ('change', 'change', ?, ?, ?, ?, ?, ?, ?, "
        "'pending', ?)",
        (payload_json, store.sha256_hex(payload_json), rationale,
         json.dumps([f"{k}:{v}" for k, v in ev_items]),
         created_by if created_by in ("desk", "analyst", "monitor", "system") else "system",
         store.iso(now_dt), store.iso(close_dt), secrets.token_hex(5)))
    pid = int(cur.lastrowid)
    superseded = supersede_changes(conn, pid, norm, store.iso(now_dt))
    return {"proposal_id": pid, "change": norm, "superseded": superseded}


def _change_subject(conn, norm: dict) -> set:
    """What a change acts on: its ticker, the risk key, or the tickers of a set_weights
    other than the cash fund (nearly every set_weights moves the cash fund, so it never
    joins two cards). A set_premise or resume_trading acts on one thing: its kind."""
    kind = norm.get("kind")
    if kind in ("set_thesis", "add_company", "drop_company"):
        return {norm.get("ticker")}
    if kind == "set_weights":
        names = {c.get("ticker") for c in norm.get("changes") or []}
        cash = {t for t in names if (store.get_company(conn, t) or {}).get("kind") == "cash"}
        return (names - cash) or names
    if kind == "set_risk":
        return {norm.get("key")}
    if kind == "cancel_order":
        return {norm.get("order_id")}
    return {kind}


def supersede_changes(conn, proposal_id: int, norm: dict, now: str) -> list:
    """End each older pending change of the same kind and subject as ``superseded`` by
    ``proposal_id``. Only the newest card stays open, so an old card cannot undo a newer
    one. Returns the ids moved."""
    subject = _change_subject(conn, norm)
    moved = []
    for r in conn.execute("SELECT id, payload_json FROM proposals WHERE kind='change' AND state='pending' "
                          "AND id < ? ORDER BY id", (int(proposal_id),)).fetchall():
        old = (store.loads(r[1], {}) or {}).get("change") or {}
        if old.get("kind") != norm.get("kind") or not (_change_subject(conn, old) & subject):
            continue
        if end_pending(conn, r[0], "superseded", now, superseded_by=int(proposal_id)):
            moved.append(int(r[0]))
    return moved


def supersede_duplicate_changes(conn, now: str) -> list:
    """Apply the one-open-card rule to the pending change cards: the newest card of each
    kind and subject stays, and it supersedes the older ones. Returns the rows moved
    (id, tg_message_id, superseded_by) so the caller relabels their buttons."""
    rows = conn.execute("SELECT id, payload_json FROM proposals WHERE kind='change' AND state='pending' "
                        "ORDER BY id DESC").fetchall()
    gone, out = set(), []
    for r in rows:
        if r[0] in gone:
            continue
        norm = (store.loads(r[1], {}) or {}).get("change") or {}
        for old in supersede_changes(conn, r[0], norm, now):
            gone.add(old)
            row = conn.execute("SELECT tg_message_id FROM proposals WHERE id=?", (old,)).fetchone()
            out.append({"id": old, "tg_message_id": row[0] if row else None, "superseded_by": int(r[0])})
    return out


def change_preview(conn, cfg: Config, norm: dict, *, now_dt=None, yf_price_fn=None) -> str:
    """The "then I'll propose ..." sentence of a change card (§10.4), or ""."""
    tickers = follow_up_tickers(conn, cfg, norm)
    if not tickers:
        return ""
    snap = latest_snapshot(conn)
    if snap is None:
        return "Then I'll propose the trades once I have a fresh portfolio snapshot."
    now_dt = now_dt or _now_dt()
    yf_price_fn = yf_price_fn or quotes.yf_last_price
    base = valuation_base(conn, snap, now_dt, yf_price_fn)
    dep = base["deployable"]
    parts = []
    weights = {}
    if norm["kind"] == "add_company":
        weights[norm["ticker"]] = norm["target_weight"]
        for f in norm.get("funding", []):
            weights[f["ticker"]] = f["target_weight"]
    elif norm["kind"] == "drop_company":
        weights[norm["ticker"]] = 0.0
        for f in norm.get("redistribute", []):
            weights[f["ticker"]] = f["target_weight"]
    elif norm["kind"] == "set_weights":
        for c in norm["changes"]:
            weights[c["ticker"]] = c["target_weight"]
    elif norm["kind"] == "set_thesis":
        goal = active_goal(conn)
        row = next((r for r in book_rows(conn, goal["id"]) if r["ticker"] == norm["ticker"]), None)
        bb = norm.get("buy_below")
        if row is None or bb is None:
            return ""
        price, _ = quotes.price_for(conn, norm["ticker"], now_dt, yf_price_fn)
        if price is None or price > float(bb):
            return ""
        weights[norm["ticker"]] = float(row["target_weight"] or 0)
    for t in tickers:
        if t == cfg.risk.cash_sleeve_ticker:
            continue
        gap = weights.get(t, 0.0) / 100.0 * dep - base["values"].get(t, 0.0)
        if abs(gap) < 1:
            continue
        if gap > 0:
            parts.append(f"buying about ${gap:,.0f} of {t}")
        else:
            parts.append(f"selling about ${-gap:,.0f} of {t}")
    if not parts:
        return ""
    src = " from cash" if norm["kind"] in ("add_company", "set_thesis") else ""
    if len(parts) == 1:
        return f"Then I'll propose {parts[0]}{src}."
    return "Then I'll propose " + ", ".join(parts[:-1]) + f" and {parts[-1]}{src}."


def apply_change(led, conn, cfg: Config, proposal: dict, *, now_dt=None) -> dict:
    """Apply a confirmed change inside the tap's transaction (§10.3 apply column). Re-
    validates first; a failure raises ProposalError and the caller rolls back. Writes the
    ``strategy_change_log`` rows (``trigger='owner_confirm'``). Returns the applied change.

    This is the only code that writes a risk setting (``set_risk``) or lifts a halt
    (``resume_trading``). Only the Confirm tap reaches it."""
    now_dt = now_dt or _now_dt()
    now = store.iso(now_dt)
    payload = store.loads(proposal["payload_json"], {}) or {}
    norm = validate_change(conn, cfg, payload.get("change") or {}, now_dt=now_dt)
    pid = int(proposal["id"])
    kind = norm["kind"]
    if kind not in TAP_ONLY_KINDS:
        return _apply_plan(led, conn, cfg, norm, pid, now, trigger="owner_confirm")
    goal = active_goal(conn)
    gid = goal["id"] if goal else None
    proof = json.dumps({"proposal_id": pid})

    def log(change_type, frm, to, reason):
        led.record_strategy_change(goal_id=gid, changed_at=now, change_type=change_type, ticker=None,
                                   from_value=frm, to_value=to, trigger="owner_confirm", reason=reason,
                                   proof_json=proof, conn=conn)

    if kind == "cancel_order":
        # One cancel call, queued for quiver-broker. The guard allows it only for this card's
        # order id (a query job with requested_by 'approval' and this proposal id).
        from lib.desk import brokerq
        try:
            payload = brokerq.cancel_payload(norm["order_id"], advanced=norm.get("advanced", False), proposal_id=pid)
        except brokerq.QueryError as e:
            raise ProposalError(str(e))
        brokerq.enqueue(conn, payload, requested_by="approval", now=now)
        return {"change": norm, "superseded": [], "touch": ["broker"]}
    if kind == "set_risk":
        desk_settings.write(conn, norm["key"], norm["value"], now, pid)
        log("risk", norm.get("old"), norm["value"], f"set_risk {norm['key']}")
    elif kind == "resume_trading":
        n = led.clear_halt(today(), f"resumed by owner, proposal {pid}", conn=conn)
        if n != 1:
            raise ProposalError("trading is not halted today")
        log("status", "halted", "resumed", kind)
    return {"change": norm, "superseded": []}


def _apply_plan(led, conn, cfg: Config, norm: dict, pid: int, now: str, *, trigger: str) -> dict:
    """Apply one validated plan change (PLAN_KINDS) in the caller's transaction and write
    its ``strategy_change_log`` rows with ``trigger``. The Confirm tap (``owner_confirm``)
    and an owner turn (``owner_chat``, §10.6) share it. It has no code for a risk setting
    or the halt, so the owner-turn path cannot reach them."""
    kind = norm["kind"]
    if kind not in PLAN_KINDS:
        raise ProposalError(f"{kind} is not a plan change")
    proof = json.dumps({"proposal_id": pid})
    goal = active_goal(conn)
    gid = goal["id"] if goal else None

    def log(change_type, ticker=None, frm=None, to=None, reason=None):
        led.record_strategy_change(goal_id=gid, changed_at=now, change_type=change_type,
                                   ticker=ticker, from_value=frm, to_value=to,
                                   trigger=trigger, reason=reason or kind,
                                   proof_json=proof, conn=conn)

    rows = {r["ticker"]: r for r in book_rows(conn, gid, statuses=("active", "exiting", "removed"))} if gid else {}
    touched = []
    if kind == "add_company":
        t = norm["ticker"]
        led.upsert_target_holding(goal_id=gid, sleeve=norm.get("sleeve"), ticker=t,
                                  target_weight=norm["target_weight"], band=norm["band"],
                                  status="active", book=goal.get("active_book"), quotable=True,
                                  proxy_ticker=None, updated_at=now,
                                  entry_price_max=norm["entry"]["price"] if norm["entry"]["mode"] == "at_or_below" else None,
                                  conn=conn)
        log("status", t, (rows.get(t) or {}).get("status"), "active")
        log("weight", t, (rows.get(t) or {}).get("target_weight"), norm["target_weight"])
        if norm["entry"]["mode"] == "at_or_below":
            levels.upsert_level(conn, t, "entry", norm["entry"]["price"], now, armed=1)
        for f in norm.get("funding", []):
            conn.execute("UPDATE target_portfolio SET target_weight=?, updated_at=? WHERE goal_id=? AND ticker=?",
                         (f["target_weight"], now, gid, f["ticker"]))
            log("weight", f["ticker"], (rows.get(f["ticker"]) or {}).get("target_weight"), f["target_weight"])
        comp = store.get_company(conn, t) or {}
        store.set_company_status(conn, t, "holding", f"added by proposal #{pid}", now)
        touched = [t] + [f["ticker"] for f in norm.get("funding", [])]
    elif kind == "drop_company":
        t = norm["ticker"]
        if t in rows:
            conn.execute("UPDATE target_portfolio SET target_weight=0, status='exiting', updated_at=? "
                         "WHERE goal_id=? AND ticker=?", (now, gid, t))
        else:
            led.upsert_target_holding(goal_id=gid, sleeve=None, ticker=t, target_weight=0.0, band=0.0,
                                      status="exiting", book=goal.get("active_book"), quotable=True,
                                      proxy_ticker=None, updated_at=now, conn=conn)
        log("status", t, (rows.get(t) or {}).get("status"), "exiting")
        log("weight", t, (rows.get(t) or {}).get("target_weight"), 0)
        for f in norm.get("redistribute", []):
            conn.execute("UPDATE target_portfolio SET target_weight=?, updated_at=? WHERE goal_id=? AND ticker=?",
                         (f["target_weight"], now, gid, f["ticker"]))
            log("weight", f["ticker"], (rows.get(f["ticker"]) or {}).get("target_weight"), f["target_weight"])
        if store.get_company(conn, t):
            store.set_company_status(conn, t, "dropped", f"dropped by proposal #{pid}", now)
        if norm["exit"]["mode"] == "at_or_above":
            levels.upsert_level(conn, t, "trim_above", norm["exit"]["price"], now, armed=1)
        touched = [t] + [f["ticker"] for f in norm.get("redistribute", [])]
    elif kind == "set_weights":
        weights = {c["ticker"]: c["target_weight"] for c in norm["changes"]}
        bands = {c["ticker"]: c["band"] for c in norm["changes"] if c.get("band") is not None}
        changes = [{"ticker": t, "from_value": (rows.get(t) or {}).get("target_weight"), "to_value": w,
                    "reason": "set_weights", "proof_json": proof} for t, w in weights.items()]
        led.retarget_weights(gid, weights, now, changes, trigger=trigger, bands=bands, conn=conn)
        touched = list(weights)
    elif kind == "set_premise":
        row = conn.execute("SELECT MAX(version) FROM premise").fetchone()
        version = int(row[0] or 0) + 1
        conn.execute("INSERT INTO premise (version, text, goal_json, confirmed_at, proposal_id) "
                     "VALUES (?, ?, ?, ?, ?)", (version, norm["text"], json.dumps(norm["goal"]), now, pid))
        if gid is not None:
            conn.execute("UPDATE strategy_goal SET target_return_pct=?, horizon_months=? WHERE id=?",
                         (norm["goal"]["target_return_pct"], norm["goal"]["horizon_months"], gid))
        log("premise", None, version - 1, version)
    elif kind == "set_thesis":
        t = norm["ticker"]
        row = conn.execute("SELECT MAX(version) FROM theses WHERE ticker=?", (t,)).fetchone()
        version = int(row[0] or 0) + 1
        conn.execute(
            "INSERT INTO theses (ticker, version, premise_link, summary, drivers_json, falsifiers_json, "
            "valuation_json, fair_value_low, fair_value_base, fair_value_high, buy_below, trim_above, "
            "evidence_json, confirmed_at, proposal_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t, version, norm["premise_link"], norm["summary"], json.dumps(norm["drivers"]),
             json.dumps(norm["falsifiers"]), json.dumps(norm["valuation"]), norm["fair_value_low"],
             norm["fair_value_base"], norm["fair_value_high"], norm["buy_below"], norm["trim_above"],
             json.dumps(norm["evidence"]), now, pid))
        for lvl in ("buy_below", "trim_above"):
            if norm.get(lvl) is not None:
                levels.upsert_level(conn, t, lvl, norm[lvl], now, armed=1)
            else:
                levels.delete_level(conn, t, lvl)
        log("thesis", t, version - 1, version)
    # A target change supersedes every pending trade proposal on the tickers it touched.
    superseded = []
    if touched:
        marks = ",".join("?" for _ in touched)
        for r in conn.execute(f"SELECT DISTINCT p.id FROM proposals p JOIN orders o ON o.proposal_id=p.id "
                              f"WHERE p.kind='trade' AND p.state='pending' AND o.ticker IN ({marks})",
                              touched).fetchall():
            if end_pending(conn, r[0], "superseded", now, superseded_by=pid):
                superseded.append(r[0])
    return {"change": norm, "superseded": superseded}


def apply_owner_change(led, conn, cfg: Config, *, change: dict, rationale: str, evidence=None,
                       owner_message_id, turn_owner_ids, role: str, now_dt=None) -> dict:
    """Apply a plan change at once from the owner's own message in a desk turn (§10.6). No
    Confirm card: the owner already said it. Runs in the caller's transaction and stores
    the change as an ``applied`` proposal row (origin ``owner``) for the audit trail and the
    follow-up trade. Refused for the analyst, for a message outside this turn, and for a
    risk setting or the halt (TAP_ONLY_KINDS keep their Confirm tap). Returns
    ``{proposal_id, change, before, superseded}``: ``before`` holds the planned shares
    before the change, ``superseded`` the cards it replaced."""
    now_dt = now_dt or _now_dt()
    now = store.iso(now_dt)
    if role != "desk":
        raise ProposalError("only the desk applies a change from the owner's message")
    try:
        mid = int(owner_message_id)
    except (TypeError, ValueError):
        raise ProposalError("give owner_message_id: the id of the owner message that asks for the change")
    if mid not in {int(x) for x in (turn_owner_ids or ())}:
        raise ProposalError("that owner message is not part of this turn")
    kind = (change or {}).get("kind")
    if kind in TAP_ONLY_KINDS:
        raise ProposalError(f"{kind} changes a safety limit, so it needs the owner's Confirm tap: "
                            "propose it without an owner origin")
    if kind not in PLAN_KINDS:
        raise ProposalError(f"change kind must be one of {', '.join(CHANGE_KINDS)}")
    rationale = str(rationale or "").strip()
    if not rationale or len(rationale) > 280:
        raise ProposalError("give a rationale of 280 characters or fewer")
    ev_items = evidence_items(evidence)
    goal = active_goal(conn)
    before = {str(r["ticker"]).upper(): float(r["target_weight"] or 0.0)
              for r in (book_rows(conn, goal["id"]) if goal else [])}
    norm = validate_change(conn, cfg, change, now_dt=now_dt)
    payload_json = store.canonical_json({"kind": "change", "change": norm})
    cur = conn.execute(
        "INSERT INTO proposals (kind, origin, owner_message_id, payload_json, payload_sha256, rationale, "
        "evidence_json, created_by, created_at, expires_at, state, nonce, decided_at, decided_by) "
        "VALUES ('change', 'owner', ?, ?, ?, ?, ?, 'desk', ?, ?, 'applied', ?, ?, 'chat')",
        (mid, payload_json, store.sha256_hex(payload_json), rationale,
         json.dumps([f"{k}:{v}" for k, v in ev_items]), now, now, secrets.token_hex(5), now))
    pid = int(cur.lastrowid)
    cards = supersede_changes(conn, pid, norm, now)
    res = _apply_plan(led, conn, cfg, norm, pid, now, trigger="owner_chat")
    return {"proposal_id": pid, "change": norm, "before": before, "superseded": cards + res["superseded"]}


def _plan_moves(plan: dict, tickers=None) -> list:
    """The moves of a card's plan for ``tickers`` (all when None), with the cash sleeve's
    matching difference last: ``[{ticker, delta, exit}]``."""
    changes = [c for c in plan.get("changes") or [] if tickers is None or c["ticker"] in tickers]
    if not changes:
        return []
    moves = [{"ticker": str(c["ticker"]).upper(), "delta": float(c["target_weight"]) - float(c["from"]),
              "exit": bool(c.get("exit"))} for c in changes]
    moves.append({"ticker": str(plan["cash"]["ticker"]).upper(), "delta": -sum(m["delta"] for m in moves),
                  "exit": False})
    return moves


def check_trade_plan(conn, cfg: Config, proposal: dict) -> None:
    """At the Approve tap: can the card's plan move still apply (§10.6)? Each name moves by
    the difference the card showed. Raises ProposalError when a name left the plan, a raised
    name would pass the per-name limit, or the cash sleeve would fall under 0% or under a
    set ``cash_floor_pct``. Writes nothing: the plan moves only when an order fills."""
    plan = (store.loads(proposal.get("payload_json"), {}) or {}).get("plan")
    if not plan:
        return
    goal = active_goal(conn)
    if goal is None:
        raise ProposalError("no active book")
    sett = desk_settings.effective(conn, cfg)
    by = {r["ticker"]: r for r in book_rows(conn, goal["id"], statuses=("active", "exiting"))}
    cap = float(sett["per_name_max_pct"])
    cash_t = str(plan["cash"]["ticker"]).upper()
    for m in _plan_moves(plan):
        r = by.get(m["ticker"])
        if r is None:
            raise ProposalError(f"{m['ticker']} left the plan after this card")
        new = 0.0 if m["exit"] else float(r["target_weight"] or 0.0) + m["delta"]
        if m["ticker"] == cash_t:
            hard = sett.get("cash_floor_pct")
            if new < -1e-6:
                raise ProposalError(f"the plan's cash would fall under 0% ({new:.1f}%)")
            if hard is not None and new + 1e-9 < float(hard):
                raise ProposalError(f"the plan's cash would be {new:.1f}% of the account, under the "
                                    f"{float(hard):g}% cash floor")
        elif m["delta"] > 0 and new > cap + 1e-9:
            raise ProposalError(f"{m['ticker']} would be {new:.1f}% of the plan, over the {cap:g}% limit per name")


def apply_plan_moves(led, conn, proposal: dict, tickers, now: str) -> list:
    """Move the plan for the names of a sized owner trade whose orders filled (§10.6), in the
    caller's transaction. Each name moves by the difference the card showed, so a change
    made after the card still holds; a name sold in full leaves the plan (``exiting``). The
    cash sleeve takes the matching difference, so the plan still sums to 100%. A name moves
    once: ``result_json.plan_applied`` lists the names done. Returns the names moved now."""
    payload = store.loads(proposal.get("payload_json"), {}) or {}
    plan = payload.get("plan")
    if not plan:
        return []
    done = set((store.loads(proposal.get("result_json"), {}) or {}).get("plan_applied") or [])
    todo = {str(t).upper() for t in tickers or ()} - done
    moves = _plan_moves(plan, todo)
    goal = active_goal(conn)
    if not moves or goal is None:
        return []
    gid, pid = goal["id"], int(proposal["id"])
    by = {r["ticker"]: r for r in book_rows(conn, gid, statuses=("active", "exiting"))}
    proof = json.dumps({"proposal_id": pid})
    weights, log, exits = {}, [], []
    for m in moves:
        r = by.get(m["ticker"])
        if r is None:
            continue
        cur_w = float(r["target_weight"] or 0.0)
        new_w = 0.0 if m["exit"] else round(max(0.0, cur_w + m["delta"]), 4)
        weights[m["ticker"]] = new_w
        log.append({"ticker": m["ticker"], "from_value": cur_w, "to_value": new_w, "reason": "owner_trade",
                    "proof_json": proof})
        if m["exit"]:
            exits.append(m["ticker"])
    led.retarget_weights(gid, weights, now, log, trigger="owner_trade", conn=conn)
    for t in exits:
        conn.execute("UPDATE target_portfolio SET status='exiting', updated_at=? WHERE goal_id=? AND ticker=?",
                     (now, gid, t))
        led.record_strategy_change(goal_id=gid, changed_at=now, change_type="status", ticker=t,
                                   from_value="active", to_value="exiting", trigger="owner_trade",
                                   reason="sold in full", proof_json=proof, conn=conn)
        if store.get_company(conn, t):
            store.set_company_status(conn, t, "dropped", f"sold in full by proposal #{pid}", now)
    moved = sorted(m["ticker"] for m in moves[:-1])
    res = store.loads(proposal.get("result_json"), {}) or {}
    res["plan_applied"] = sorted(done | set(moved))
    conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), pid))
    return moved


def build_follow_up(led, conn, cfg: Config, change_proposal: dict, *, now_dt=None, yf_price_fn=None) -> dict:
    """Build the follow-up trade of a confirmed change (§10.4). Returns
    ``{"built": result}``, ``{"waiting": True}`` when the snapshot is stale (the caller
    queues a snapshot job), ``{"none": True}`` when no trade follows, or
    ``{"refused": text}``."""
    now_dt = now_dt or _now_dt()
    payload = store.loads(change_proposal["payload_json"], {}) or {}
    norm = payload.get("change") or {}
    tickers = follow_up_tickers(conn, cfg, norm)
    if not tickers:
        return {"none": True}
    if norm["kind"] == "set_thesis":
        snap = latest_snapshot(conn)
        bb = norm.get("buy_below")
        if snap is None or bb is None:
            return {"none": True}
        price, _ = quotes.price_for(conn, norm["ticker"], now_dt, yf_price_fn or quotes.yf_last_price)
        if price is None or price > float(bb):
            return {"none": True}
    owner_made = change_proposal.get("origin") == "owner"
    why = (f"Follows the change you asked for (#{change_proposal['id']})." if owner_made
           else f"Follows your confirmed change #{change_proposal['id']}.")
    try:
        built = build_trade(led, conn, cfg=cfg, tickers=tickers, origin={}, python_origin="change",
                            parent_change_id=int(change_proposal["id"]), rationale=why,
                            evidence=[], created_by="system", now_dt=now_dt, yf_price_fn=yf_price_fn,
                            only_side="buy" if norm["kind"] == "set_thesis" else None,
                            money_rule="owner" if owner_made else None)
    except ProposalError as e:
        msg = str(e)
        if "snapshot is stale" in msg:
            return {"waiting": True}
        if msg.startswith("no order to propose"):
            return {"none": True, "why": no_order_reasons(msg)}
        return {"refused": msg}
    return {"built": built}


def no_order_reasons(msg: str) -> str:
    """The reasons inside a "no order to propose (...)" refusal, for the owner's message."""
    m = str(msg or "")
    if m.startswith("no order to propose (") and ")" in m:
        return m[len("no order to propose ("):m.rindex(")")]
    return m
