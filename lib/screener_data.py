"""yfinance-backed candidate provider for lib.screener (Q3 universe discovery).

The live data seam the pure screener (``lib.screener``) calls to enumerate candidate
tickers for a sleeve's screen criteria. Uses yfinance's equity screener — NO new API
key. DATA ONLY: it never touches the broker, trading limits, or the ledger, so the
decision wall holds. Best-effort: any failure raises (the screener wraps the call and
degrades to "no candidates", so a yfinance outage never grows the universe).

EXTRACTED from tradingagents/dataflows/screener_data.py as part of the EVE migration
(F8 prep) so the universe-discovery path survives the tradingagents/ deletion. This is
NOT brain code — it's a data provider the learn-review path calls.

Maps a sleeve ``screen`` dict -> yfinance EquityQuery:
  sector          -> EquityQuery('eq', ['sector', <name>])   (also stamped on results)
  market_cap_min  -> EquityQuery('gt', ['intradaymarketcap', <usd>])
  + region 'us'   so candidates are US-listed (the RH allow-list is the hard gate anyway).
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
    cap_min = screen.get("market_cap_min")
    if cap_min is not None:
        filters.append(yf.EquityQuery("gt", ["intradaymarketcap", float(cap_min)]))
    # US-listed only — Robinhood-tradable; the allow-list (validate_add) is the hard gate.
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
