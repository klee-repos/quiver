"""Proposals: build, size, render data, expire and supersede trades; validate and apply
changes; close a proposal from its legs (plan §10, §11.1 step 9).

A proposal is one ``proposals`` row plus, for a trade, one reserved ``orders`` row per leg.
Python sizes every leg here. A model only names tickers, an origin and its evidence; it
never picks a dollar amount, and for an event trade it never picks the fraction either
(§10.2 step 7). The ``ref_id`` of each leg is fixed when the proposal is built, so a retry
can never mint a second order.

``apply_change`` runs only inside the tap code of ``lib.desk.approvals`` (a Confirm tap).
No desk tool can call it (the W3 test enforces this).
"""

from __future__ import annotations

import json
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
                "set_thesis", "resume_trading")


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


# --- trades (§10.2) ------------------------------------------------------------------

def build_trade(led, conn, *, cfg: Config, tickers, origin: dict, fraction=None, rationale: str,
                evidence=None, created_by: str, now_dt=None, role: Optional[str] = None,
                turn_owner_ids=None, python_origin: Optional[str] = None,
                parent_change_id: Optional[int] = None, requote_of: Optional[dict] = None,
                snap_override: Optional[dict] = None, yf_price_fn=None,
                only_side: Optional[str] = None) -> dict:
    """Build one pending trade proposal and reserve its legs. Runs inside the caller's
    transaction (``conn``). Raises ProposalError on any refusal; nothing is written then.

    ``origin`` is ``{"event_id": n}`` or ``{"owner_message_id": n}`` from a tool, or the
    ``python_origin`` (``change``, ``drift``) that only Python passes. ``requote_of`` is the
    replaced proposal row: its origin, rationale and evidence are copied, and the origin
    checks are skipped (§10.2 step 6)."""
    now_dt = now_dt or _now_dt()
    now = store.iso(now_dt)
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
            raise ProposalError(f"{t} is not in the book; propose add_company")
        hold = in_flight_ticker(conn, t)
        if hold:
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
        if fraction is None or float(fraction) not in FRACTIONS:
            raise ProposalError("fraction must be 0.25, 0.5 or 1.0")
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
    floor_pct = desk_settings.cash_floor_pct(sett, all_rows, cash_ticker)
    cash_floor_dollars = floor_pct / 100.0 * deployable
    money = min(bp - float(sett["min_buying_power_buffer"]), bp + cash_value - cash_floor_dollars)

    sells, buy_wants, skipped = [], [], []
    for t in tickers:
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

    # Step 4: buys may use the estimated sell proceeds; scale pro rata when short.
    proceeds = sum(s["quantity"] * s["quote"] for s in sells)
    available = max(0.0, money) + proceeds
    total_want = sum(b["want"] for b in buy_wants)
    scaled = False
    buys = []
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
                skipped.append({"ticker": b["ticker"], "reason": "the scaled buy is under the minimum"})
                continue
            b["dollars"] = dollars
            buys.append(b)
    legs = sells + buys
    if not legs:
        reasons = "; ".join(f"{s['ticker']}: {s['reason']}" for s in skipped) or "nothing to do"
        raise ProposalError(f"no order to propose ({reasons})")
    if requote_of is None and kind_origin in ("event", "owner"):
        for leg in legs:
            _check_anti_flip(conn, leg["ticker"], leg["side"], ev_items, now_dt)

    # Expiry and the leg trade date (the session date of expires_at, G7).
    session_date, close_dt = market.session_close_et(now_dt.astimezone(market.ET))
    expires_at = store.iso(close_dt)

    payload_legs = []
    for seq, leg in enumerate(legs, start=1):
        leg["seq"] = seq
        leg["ref_id"] = str(uuid.uuid4())
        payload_legs.append({"seq": seq, "ref_id": leg["ref_id"], "ticker": leg["ticker"],
                             "side": leg["side"], "dollar_amount": leg.get("dollars"),
                             "quantity": leg.get("quantity"), "quote": leg["quote"]})
    payload = {"kind": "trade", "fraction": fraction, "tickers": tickers, "snapshot_id": snap["id"],
               "deployable": round(deployable, 2), "legs": payload_legs, "scaled": scaled,
               "skipped": skipped, "side_rule": side_rule}
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
        led.reserve_order(leg["ref_id"], session_date, leg["ticker"], side=leg["side"], type="market",
                          dollar_amount=leg.get("dollars"), quantity=leg.get("quantity"),
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
            "superseded": superseded, "origin": kind_origin, "snapshot_id": snap["id"]}


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
    model = {"id": prop["id"], "origin": prop["origin"], "rationale": prop["rationale"],
             "legs": built["legs"], "skipped": built["skipped"], "scaled": built["scaled"],
             "expires_at": prop["expires_at"], "now": store.iso(now_dt or _now_dt()),
             "evidence": store.loads(prop["evidence_json"], []), "drift": prop["origin"] == "drift"}
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
    if goal is None and kind not in ("set_premise",):
        raise ProposalError("no active book; run desk-seed")
    rows = book_rows(conn, goal["id"], statuses=("active", "exiting", "removed")) if goal else []
    by = {r["ticker"]: r for r in rows}
    out = {"kind": kind}
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
        _validate_book(new_rows, sett, cash_ticker)
        out.update(ticker=t, exit={"mode": mode, "price": price}, unmanaged=t not in by,
                   redistribute=[{"ticker": str(f["ticker"]).upper(),
                                  "target_weight": float(f["target_weight"])} for f in redistribute])
    elif kind == "set_weights":
        changes = list(change.get("changes") or [])
        if not changes:
            raise ProposalError("set_weights needs at least one change")
        new_rows = _apply_weights([dict(r) for r in rows], changes)
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
    return {"proposal_id": int(cur.lastrowid), "change": norm}


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
    ``strategy_change_log`` rows (``trigger='owner_confirm'``). Returns the applied change."""
    now_dt = now_dt or _now_dt()
    now = store.iso(now_dt)
    payload = store.loads(proposal["payload_json"], {}) or {}
    norm = validate_change(conn, cfg, payload.get("change") or {}, now_dt=now_dt)
    pid = int(proposal["id"])
    proof = json.dumps({"proposal_id": pid})
    goal = active_goal(conn)
    gid = goal["id"] if goal else None
    kind = norm["kind"]

    def log(change_type, ticker=None, frm=None, to=None, reason=None):
        led.record_strategy_change(goal_id=gid, changed_at=now, change_type=change_type,
                                   ticker=ticker, from_value=frm, to_value=to,
                                   trigger="owner_confirm", reason=reason or kind,
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
        led.retarget_weights(gid, weights, now, changes, trigger="owner_confirm", bands=bands, conn=conn)
        touched = list(weights)
    elif kind == "set_risk":
        desk_settings.write(conn, norm["key"], norm["value"], now, pid)
        log("risk", None, norm.get("old"), norm["value"], reason=f"set_risk {norm['key']}")
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
    elif kind == "resume_trading":
        n = led.clear_halt(today(), f"resumed by owner, proposal {pid}", conn=conn)
        if n != 1:
            raise ProposalError("trading is not halted today")
        log("status", None, "halted", "resumed")
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
    try:
        built = build_trade(led, conn, cfg=cfg, tickers=tickers, origin={}, python_origin="change",
                            parent_change_id=int(change_proposal["id"]),
                            rationale=f"Follows your confirmed change #{change_proposal['id']}.",
                            evidence=[], created_by="system", now_dt=now_dt, yf_price_fn=yf_price_fn,
                            only_side="buy" if norm["kind"] == "set_thesis" else None)
    except ProposalError as e:
        msg = str(e)
        if "snapshot is stale" in msg:
            return {"waiting": True}
        if msg.startswith("no order to propose"):
            return {"none": True, "why": msg}
        return {"refused": msg}
    return {"built": built}
