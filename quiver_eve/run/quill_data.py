#!/usr/bin/env python3
"""Quiver EVE data helper — a thin read-only shell over the existing, tested
tradingagents/dataflows/ fetchers, invoked by the EVE tools.

WHY a Python helper (not a TS re-implementation): the data-fetch logic (yfinance
default + alpha_vantage fallback + StockTwits + stockstats indicators) already
lives in tradingagents/dataflows/ and is unit-tested. Re-implementing it in
TypeScript would create a SECOND source of truth for vendor routing and risk
silent drift. EVE needs the DATA; this helper gives it the same data the legacy
analysts consumed, as one JSON line per call.

Read-only: fetches market data only. Never reads the broker, trading caps, or
the ledger. Fails safe: a fetch error returns {"report": "UNAVAILABLE: ...",
"core_available": false} so the EVE brain marks the report UNAVAILABLE (not a
fabricated confident assessment) and Python's fail-SAFE ERROR gate can fire.

Usage: quill_data.py <kind> <ticker> [--date YYYY-MM-DD]
  kind in {market, sentiment, news, fundamentals}
Prints one JSON line to stdout: {"report": "...", "core_available": bool}
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent  # the quiver root
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _safe(fn) -> dict:
    try:
        report, core = fn()
        return {"report": report, "core_available": bool(core)}
    except Exception as e:  # noqa: BLE001 — a fetch error is UNAVAILABLE, never fatal
        return {"report": f"UNAVAILABLE: {type(e).__name__}: {e}", "core_available": False}


def _recent_window(date: str | None, days: int = 60) -> tuple[str, str]:
    """A start/end yyyy-mm-dd window ending at `date` (or today)."""
    from datetime import datetime, timedelta
    end = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def fetch_market(ticker: str, date: str | None) -> tuple[str, bool]:
    from lib.dataflows.y_finance import get_YFin_data_online, get_stockstats_indicator
    start, end = _recent_window(date)
    sd = get_YFin_data_online(ticker, start, end)
    ind = get_stockstats_indicator(ticker, start, end)
    report = f"Price/Volume:\n{json.dumps(sd, default=str)[:1200]}\nIndicators:\n{json.dumps(ind, default=str)[:1200]}"
    # core_available = we got a usable close price
    core = bool(sd and isinstance(sd, dict) and sd.get("close") is not None)
    return report, core


def fetch_sentiment(ticker: str) -> tuple[str, bool]:
    try:
        from lib.dataflows.stocktwits import fetch_stocktwits_messages
        msgs = fetch_stocktwits_messages(ticker)
    except Exception as e:  # noqa: BLE001 — Reddit 403 / StockTwits errors degrade gracefully
        return f"UNAVAILABLE: sentiment fetch failed: {e}", False
    sample = (msgs or [])[:15]
    return f"StockTwits ({len(msgs or [])} msgs):\n" + "\n".join(
        json.dumps(m, default=str)[:200] for m in sample), bool(sample)


def fetch_news(ticker: str) -> tuple[str, bool]:
    from lib.dataflows.yfinance_news import get_news_yfinance
    start, end = _recent_window(None, days=7)
    news = get_news_yfinance(ticker, start, end)
    return f"News:\n{json.dumps(news, default=str)[:2000]}", bool(news)


def fetch_fundamentals(ticker: str) -> tuple[str, bool]:
    from lib.dataflows.y_finance import get_fundamentals, get_balance_sheet
    f = get_fundamentals(ticker)
    bs = get_balance_sheet(ticker)
    return (f"Fundamentals: {json.dumps(f, default=str)[:1500]}\n"
            f"Balance Sheet: {json.dumps(bs, default=str)[:1500]}"), bool(f)


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["market", "sentiment", "news", "fundamentals"])
    ap.add_argument("ticker")
    ap.add_argument("--date", default=None)
    args = ap.parse_args(argv)
    t = args.ticker.strip().upper()
    if args.kind == "market":
        out = _safe(lambda: fetch_market(t, args.date))
    elif args.kind == "sentiment":
        out = _safe(lambda: fetch_sentiment(t))
    elif args.kind == "news":
        out = _safe(lambda: fetch_news(t))
    else:
        out = _safe(lambda: fetch_fundamentals(t))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"report": "UNAVAILABLE: internal error", "core_available": False}))
        sys.exit(1)
