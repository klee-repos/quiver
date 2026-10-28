"""The leg-price rule for a Robinhood quote object, and the quote source for sizing.

One rule prices a leg everywhere (plan §11.2 step 2): ``build_trade`` takes
``quote_at_proposal`` by it, ``exec-check`` re-checks a leg by it, and the monitor's level
step reads it. A quote object is the ``quote`` dict of one ``data.results[]`` entry of a real
quote answer (fields ``last_trade_price``, ``venue_last_trade_time``, ``bid_price``,
``venue_bid_time``, ``ask_price``, ``venue_ask_time``, ``state``, ``has_traded``).

The rule, with each age measured at ``at``:
  * refuse when ``state`` is not ``active`` or ``has_traded`` is not ``true``;
  * refuse when the newest of the three venue times is absent or more than 120 s old;
  * the price is ``last_trade_price`` when the last trade is 120 s old or less;
  * else the price is the bid/ask midpoint, refused when the spread is more than 1% of it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

MAX_QUOTE_AGE_S = 120.0
MAX_SPREAD_FRACTION = 0.01
SNAPSHOT_QUOTE_MAX_AGE = timedelta(minutes=60)

_FRAC = re.compile(r"(\.\d{6})\d+")


def parse_venue_time(value) -> Optional[datetime]:
    """Parse a venue time like ``2026-10-27T19:59:59.901475032Z`` (nanoseconds are cut
    to microseconds). Returns an aware UTC datetime, or None."""
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = _FRAC.sub(r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def leg_price(quote: Optional[dict], at: datetime) -> tuple:
    """``(price, reason)``. ``reason`` is ``last`` or ``mid`` with a price, else one of
    ``no_quote``, ``state``, ``has_traded``, ``stale``, ``no_bid_ask``, ``wide_spread``."""
    if not isinstance(quote, dict):
        return None, "no_quote"
    if quote.get("state") != "active":
        return None, "state"
    if quote.get("has_traded") is not True:
        return None, "has_traded"
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    times = [parse_venue_time(quote.get(k)) for k in
             ("venue_last_trade_time", "venue_bid_time", "venue_ask_time")]
    valid = [t for t in times if t is not None]
    if not valid or (at - max(valid)).total_seconds() > MAX_QUOTE_AGE_S:
        return None, "stale"
    last_t = times[0]
    last = _num(quote.get("last_trade_price"))
    if last_t is not None and (at - last_t).total_seconds() <= MAX_QUOTE_AGE_S and last and last > 0:
        return last, "last"
    bid, ask = _num(quote.get("bid_price")), _num(quote.get("ask_price"))
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None, "no_bid_ask"
    mid = (bid + ask) / 2.0
    if (ask - bid) / mid > MAX_SPREAD_FRACTION:
        return None, "wide_spread"
    return round(mid, 6), "mid"


def quote_objects(quotes_answer) -> dict:
    """``{SYMBOL: quote}`` from a quote answer (the ``data`` object or the whole answer)."""
    data = quotes_answer
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    out = {}
    for item in (data or {}).get("results", []) if isinstance(data, dict) else []:
        q = item.get("quote") if isinstance(item, dict) else None
        if isinstance(q, dict) and q.get("symbol"):
            out[str(q["symbol"]).upper()] = q
    return out


def moved_pct(new: float, old: float) -> float:
    """The absolute move in percent, rounded to 9 places so an exact 1.5% stays 1.5."""
    return round(abs(float(new) / float(old) - 1.0) * 100.0, 9)


def snapshot_price(conn, ticker: str, now: datetime) -> tuple:
    """``(price, source)`` from the newest ``broker_snapshots`` row younger than 60
    minutes that holds a usable quote for ``ticker``, priced at that row's
    ``captured_at``. ``(None, reason)`` when no row qualifies."""
    import json
    from lib.desk import store
    t = str(ticker).upper()
    cutoff = store.iso(now - SNAPSHOT_QUOTE_MAX_AGE)
    rows = conn.execute("SELECT id, captured_at, quotes_json FROM broker_snapshots "
                        "WHERE captured_at > ? ORDER BY id DESC", (cutoff,)).fetchall()
    for row in rows:
        try:
            quotes = json.loads(row[2] or "{}")
        except ValueError:
            continue
        q = quotes.get(t)
        if q is None:
            continue
        at = store.parse_ts(row[1])
        price, reason = leg_price(q, at)
        if price is not None:
            return price, f"snapshot:{row[0]}"
    return None, "no_snapshot_quote"


def price_for(conn, ticker: str, now: datetime, yf_price_fn) -> tuple:
    """The sizing quote of §10.2 step 3 and the level quote of §9.1 step 6: a fresh
    snapshot price, else the yfinance last price. ``(price, source)``."""
    price, source = snapshot_price(conn, ticker, now)
    if price is not None:
        return price, source
    try:
        p = yf_price_fn(str(ticker).upper()) if yf_price_fn else None
    except Exception:  # noqa: BLE001 — a vendor error means no quote, never a guess
        p = None
    p = _num(p)
    if p and p > 0:
        return p, "yfinance"
    return None, "no_quote"


def yf_last_price(ticker: str) -> Optional[float]:
    """The production yfinance seam: ``fast_info['last_price']``."""
    import yfinance as yf
    return float(yf.Ticker(ticker).fast_info["last_price"])
