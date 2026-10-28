"""yfinance-backed candidate provider for the desk's ``screen_companies`` tool.

Uses yfinance's equity screener (no API key). DATA ONLY: it never touches the broker,
trading limits, or the ledger. Any failure raises; the tool reports it.

Maps a ``screen`` dict -> yfinance EquityQuery:
  sector          -> EquityQuery('eq', ['sector', <name>])   (also stamped on results)
  industry        -> EquityQuery('eq', ['industry', <name>])
  market_cap_min  -> EquityQuery('gt', ['intradaymarketcap', <usd>])
  + region 'us'   so candidates are US-listed.
"""

from __future__ import annotations

from typing import List

# How many screen rows to pull before the screener filters/ranks them.
_DEFAULT_SIZE = 50


def yfinance_candidate_provider(screen: dict) -> List[dict]:
    """Return candidate rows ``[{ticker, sector, market_cap, pe, momentum, quotable}]``
    matching ``screen``. Raises on any yfinance error (caller degrades to no-candidates)."""
    import yfinance as yf

    sector = screen.get("sector")
    filters = []
    if sector:
        filters.append(yf.EquityQuery("eq", ["sector", str(sector)]))
    industry = screen.get("industry")
    if industry:
        filters.append(yf.EquityQuery("eq", ["industry", str(industry)]))
    cap_min = screen.get("market_cap_min")
    if cap_min is not None:
        filters.append(yf.EquityQuery("gt", ["intradaymarketcap", float(cap_min)]))
    # US-listed only (Robinhood-tradable). The owner's add_company confirm is the gate.
    filters.append(yf.EquityQuery("eq", ["region", "us"]))
    query = filters[0] if len(filters) == 1 else yf.EquityQuery("and", filters)

    size = int(screen.get("size", _DEFAULT_SIZE))
    res = yf.screen(query, size=size, sortField="intradaymarketcap", sortAsc=False)
    quotes = res.get("quotes", []) if isinstance(res, dict) else (res or [])

    out: List[dict] = []
    for q in quotes:
        sym = (q or {}).get("symbol")
        if not sym:
            continue
        # 12-month momentum, falling back to the 50-day average change.
        momentum = q.get("fiftyTwoWeekChangePercent")
        if momentum is None:
            momentum = q.get("fiftyDayAverageChangePercent")
        out.append({
            "ticker": sym,
            "sector": sector,                 # query-enforced; not returned per-row
            "market_cap": q.get("marketCap"),
            "pe": q.get("trailingPE"),
            "momentum": float(momentum) if momentum is not None else 0.0,
            "quotable": True,
        })
    return out
