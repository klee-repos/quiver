"""Price levels: the buy-below / trim-above / entry state machine with re-arm (plan §9.1 step 6).

A confirmed thesis or change proposal arms a level. The monitor checks each armed level
while the session is open:
  * ``buy_below`` and ``entry`` fire when the quote is at or under the price;
  * ``trim_above`` fires when the quote is at or over the price.
A fire creates one ``price_level`` event (``level:<ticker>:<level>:<date>``) and disarms
the level. ``buy_below``/``entry`` re-arm when the quote rises to price × 1.02;
``trim_above`` re-arms when the quote falls to price × 0.98.
"""

from __future__ import annotations

from typing import Optional

from lib import market
from lib.desk import quotes, store

LEVELS = ("buy_below", "trim_above", "entry")
REARM_UP = 1.02
REARM_DOWN = 0.98


def upsert_level(conn, ticker: str, level: str, price: float, now: str, armed: int = 1) -> None:
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    conn.execute(
        "INSERT INTO level_state (ticker, level, price, armed, last_quote, changed_at) "
        "VALUES (?, ?, ?, ?, NULL, ?) ON CONFLICT(ticker, level) DO UPDATE SET "
        "price=excluded.price, armed=excluded.armed, changed_at=excluded.changed_at",
        (str(ticker).upper(), level, float(price), int(armed), now))


def delete_level(conn, ticker: str, level: str) -> None:
    conn.execute("DELETE FROM level_state WHERE ticker=? AND level=?", (str(ticker).upper(), level))


def get_levels(conn, ticker: Optional[str] = None) -> list:
    if ticker:
        rows = conn.execute("SELECT * FROM level_state WHERE ticker=? ORDER BY level",
                            (str(ticker).upper(),))
    else:
        rows = conn.execute("SELECT * FROM level_state ORDER BY ticker, level")
    return [dict(r) for r in rows.fetchall()]


def fires(level: str, price: float, quote: float) -> bool:
    if level in ("buy_below", "entry"):
        return round(quote, 6) <= round(price, 6)
    return round(quote, 6) >= round(price, 6)


def rearms(level: str, price: float, quote: float) -> bool:
    if level in ("buy_below", "entry"):
        return round(quote, 6) >= round(price * REARM_UP, 6)
    return round(quote, 6) <= round(price * REARM_DOWN, 6)


def check_levels(conn, *, now_dt, yf_price_fn) -> dict:
    """One pass over every ``level_state`` row. Returns the events made and the rows
    re-armed. The caller runs this only while the regular session is open."""
    now = store.iso(now_dt)
    day = now_dt.astimezone(market.ET).strftime("%Y-%m-%d")
    fired, rearmed, no_quote = [], [], []
    rows = get_levels(conn)
    # Price every ticker first (a yfinance call is slow), then write: no write lock is held
    # during a network call.
    priced = {}
    for t in sorted({r["ticker"] for r in rows}):
        priced[t] = quotes.price_for(conn, t, now_dt, yf_price_fn)
    for row in rows:
        ticker, level, price = row["ticker"], row["level"], float(row["price"])
        q, source = priced[ticker]
        if q is None:
            no_quote.append(ticker)
            continue
        if row["armed"]:
            if fires(level, price, q):
                eid = store.create_event(
                    conn, dedup_key=f"level:{ticker}:{level}:{day}", ticker=ticker,
                    type="price_level", now=now, created_by="monitor",
                    detail={"level": level, "price": price, "quote": q, "source": source})
                conn.execute("UPDATE level_state SET armed=0, last_quote=?, changed_at=? "
                             "WHERE ticker=? AND level=?", (q, now, ticker, level))
                if eid is not None:
                    fired.append(eid)
                continue
        elif rearms(level, price, q):
            conn.execute("UPDATE level_state SET armed=1, last_quote=?, changed_at=? "
                         "WHERE ticker=? AND level=?", (q, now, ticker, level))
            rearmed.append(f"{ticker}:{level}")
            continue
        conn.execute("UPDATE level_state SET last_quote=? WHERE ticker=? AND level=?",
                     (q, ticker, level))
    return {"events": fired, "rearmed": rearmed, "no_quote": no_quote}
