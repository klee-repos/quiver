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
import time
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


def _retry(fn, *, tries: int = 3, base_sleep: float = 1.0):
    """F2: call ``fn()`` and retry up to ``tries`` on a RAISED exception (linear backoff),
    re-raising the last error after exhaustion. Kills the class-B "core data unavailable:
    missing market" ERRORs from a TRANSIENT yfinance miss (rate-limit/network blip) that a
    single-shot fetch turns into a wasted skip. Only wrap the RAISING core price fetch —
    helpers that already catch internally (e.g. ``_latest_indicators`` -> {}) never raise,
    so retrying them is a no-op. Fail-safe preserved: after ``tries`` it still raises, so
    ``_safe`` degrades to UNAVAILABLE and the analyze.py gate still refuses to trade blind."""
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — transient miss; retry, then fail-safe
            last = e
            if attempt < tries:
                time.sleep(base_sleep * attempt)
    assert last is not None
    raise last


def _recent_window(date: str | None, days: int = 60) -> tuple[str, str]:
    """A start/end yyyy-mm-dd window ending at `date` (or today)."""
    from datetime import datetime, timedelta
    end = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# Sentinels that mean "this fetch returned an error/empty placeholder, not real data".
# Used to derive an HONEST core_available per channel (mirrors analyze.py's gate), so a
# thin/garbage channel is reported unavailable instead of dressed up as present data.
_SENTINELS = (
    "error retrieving", "error fetching", "no data found", "no price data",
    "no fundamentals data", "no balance sheet data", "no news found",
    "no global news found", "no cash flow", "no income", "data unavailable", "could not retrieve",
    "unavailable:", "<stocktwits unavailable", "<no stocktwits messages",
)


def _content_ok(text) -> bool:
    """True only when `text` is real, usable data — non-trivial and free of any error
    sentinel. The single source of truth for a channel's core_available flag."""
    t = str(text or "").strip().lower()
    if len(t) < 30:               # empty / near-empty -> unusable
        return False
    return not any(s in t for s in _SENTINELS)


def _tail_csv(csv_text: str, max_rows: int = 45) -> str:
    """Keep the ``#`` header comment lines + the column header + the MOST RECENT
    ``max_rows`` data rows. get_YFin_data_online returns a chronological-ASCENDING
    CSV; the old ``json.dumps(sd)[:1200]`` char-slice dropped the NEWEST bars (the
    ones a decision actually needs) and cut mid-row. Tail-keeping preserves recency
    and whole rows."""
    lines = [l for l in str(csv_text).splitlines() if l != ""]
    head = [l for l in lines if l.startswith("#")]
    body = [l for l in lines if not l.startswith("#")]
    if not body:
        return str(csv_text)
    col_header, rows = body[0], body[1:]
    return "\n".join(head + [col_header] + rows[-max_rows:])


def _latest_indicators(ticker: str, curr_date: str | None) -> dict:
    """Latest available value of the key short-term indicators the trend tool does
    NOT cover (RSI/MACD/Bollinger/ATR/EMA). Computed on the LAST SETTLED bar
    (<= curr_date) so it works pre-market too — get_stockstats_indicator requires an
    exact same-day row, which doesn't exist before today's close (the old
    fetch_market ALSO passed the start-date into the indicator slot, so indicators
    were always empty). Returns {"as_of": date, <ind>: value|None, ...}, or {} on any
    failure (best-effort context; never fatal)."""
    try:
        from datetime import datetime
        import pandas as pd
        from stockstats import wrap
        from lib.dataflows.stockstats_utils import load_ohlcv
        d = curr_date or datetime.now().strftime("%Y-%m-%d")
        data = load_ohlcv(ticker, d)
        if data is None or data.empty:
            return {}
        df = wrap(data)
        names = ["rsi", "macd", "macds", "macdh", "boll", "boll_ub", "boll_lb",
                 "atr", "close_10_ema", "close_50_sma", "close_200_sma"]
        for n in names:
            df[n]  # trigger stockstats to compute the column
        last = df.iloc[-1]
        out = {"as_of": pd.to_datetime(last["Date"]).strftime("%Y-%m-%d")}
        for n in names:
            v = last[n]
            out[n] = round(float(v), 4) if pd.notna(v) else None
        return out
    except Exception:  # noqa: BLE001 — indicators are best-effort context, never fatal
        return {}


def fetch_market(ticker: str, date: str | None) -> tuple[str, bool]:
    from lib.dataflows.y_finance import get_YFin_data_online
    start, end = _recent_window(date)
    # get_YFin_data_online returns a CSV STRING (header + rows); it RAISES
    # DataUnavailableError when empty (caught by _safe -> UNAVAILABLE), so reaching
    # here means we have a real price series. Do NOT json.dumps it (the old code
    # double-encoded — main() re-encodes the whole envelope — which inflated the
    # escaped length so the char-slice cut even more real rows, mid-row).
    # F2: retry the CORE price fetch so a TRANSIENT yfinance miss doesn't skip the ticker
    # (this is the one fetch that GATES a trade). _latest_indicators below is NOT retried —
    # it catches internally and never raises (a retry there would be dead code).
    sd = _retry(lambda: get_YFin_data_online(ticker, start, end))
    price_block = _tail_csv(sd, max_rows=45)          # RECENT bars, whole rows
    inds = _latest_indicators(ticker, end)            # real RSI/MACD/BOLL/ATR/EMA snapshot
    report = (f"Price/Volume (most-recent bars, CSV):\n{price_block}\n"
              f"Latest indicators (as of {inds.get('as_of', 'n/a')}):\n{json.dumps(inds, default=str)}")
    core = bool(price_block) and ("\n" in price_block) and ("Total records: 0" not in str(sd))
    return report, core


def fetch_sentiment(ticker: str) -> tuple[str, bool]:
    try:
        from lib.dataflows.stocktwits import fetch_stocktwits_messages
        # NB: this returns a pre-formatted STRING (bull/bear summary + recent message
        # lines), NOT a list of dicts. The old code sliced it as a list -> the model
        # got the first 15 CHARACTERS, one per line. Pass the string through verbatim.
        text = fetch_stocktwits_messages(ticker)
    except Exception as e:  # noqa: BLE001 — StockTwits errors degrade gracefully
        return f"UNAVAILABLE: sentiment fetch failed: {e}", False
    text = (text or "").strip()
    if not _content_ok(text):   # placeholder / outage string -> unavailable, not sentiment
        return f"UNAVAILABLE: {text or 'no sentiment'}", False
    return f"StockTwits sentiment:\n{text}", True


def fetch_news(ticker: str, date: str | None) -> tuple[str, bool]:
    from lib.dataflows.yfinance_news import get_news_yfinance
    # Window ends at the TRADE DATE (was hard-coded None -> today, i.e. the only
    # fetcher that ignored --date: a look-ahead landmine on any dated/backtest run).
    start, end = _recent_window(date, days=7)
    news = get_news_yfinance(ticker, start, end)     # returns a STRING; single-encode
    core = _content_ok(news)
    return f"News ({start}..{end}):\n{news[:3000]}", core


def fetch_fundamentals(ticker: str, date: str | None) -> tuple[str, bool]:
    from lib.dataflows.y_finance import get_fundamentals, get_balance_sheet
    # Thread curr_date so filter_financials_by_date engages (the balance-sheet
    # look-ahead guard was silently bypassed when curr_date was None).
    f = get_fundamentals(ticker, date)
    bs = get_balance_sheet(ticker, curr_date=date)
    core = _content_ok(f)
    # Single-encode; balance-sheet budget raised 1500 -> 3000 (the old cap dropped
    # Total Assets/Liabilities/Cash rows on the bigger balance sheets).
    return (f"Fundamentals:\n{str(f)[:2000]}\n"
            f"Balance Sheet:\n{str(bs)[:3000]}"), core


def fetch_macro(date: str | None) -> tuple[str, bool]:
    """MARKET-WIDE macro/geopolitical news — the ONE non-ticker-scoped feed, so a
    macro shock (Fed move, oil spike, war/tariffs) reaches the brain even when no held
    name mentions it in its own feed. yfinance Search over the curated
    `global_news_queries` (keyless; headline-only — Search returns titles+publishers,
    no summaries). Window ends at the TRADE DATE (look-ahead safe live). Fails safe:
    an error -> UNAVAILABLE via _content_ok. Not ticker-specific, so it ignores ticker."""
    from datetime import datetime
    from lib.dataflows.yfinance_news import get_global_news_yfinance
    d = date or datetime.now().strftime("%Y-%m-%d")
    news = get_global_news_yfinance(d)     # returns a STRING; single-encode
    core = _content_ok(news)
    return f"Macro / market-wide news (as of {d}):\n{news[:3000]}", core


def fetch_trend(ticker: str, date: str | None) -> tuple[str, bool]:
    """F1: long-horizon trend/risk guideposts (yfinance, no key). ~3y daily.
    Pure metrics computed by lib.trend (unit-tested). Rendered as markdown with
    ``###`` subheadings ONLY (NEVER ``##`` — analyze.py:_split_eve_markdown
    keys on ``^##\\s+<word>`` and would drop the whole trend_report body). Fails
    safe: any error -> UNAVAILABLE (never raises). ``core_available`` reflects
    the price series (a usable close), NOT the trend metrics — a missing trend
    report never trips the core-data ERROR gate (only market_report does)."""
    import yfinance as yf
    from datetime import datetime, timedelta
    from lib import trend

    end = datetime.strptime(date, "%Y-%m-%d") if date else datetime.now()
    start = end - timedelta(days=365 * 3 + 30)
    tk = yf.Ticker(ticker)
    hist = tk.history(start=start.strftime("%Y-%m-%d"),
                     end=(end + timedelta(days=1)).strftime("%Y-%m-%d"), auto_adjust=True)
    if hist is None or hist.empty:
        return "UNAVAILABLE: no price history for trend window", False
    close = [float(x) for x in hist["Close"].tolist()]
    high = [float(x) for x in hist["High"].tolist()]
    low = [float(x) for x in hist["Low"].tolist()]
    if not close or close[-1] is None:
        return "UNAVAILABLE: no usable close", False
    bundle = trend.trend_metrics(close, high=high, low=low)
    report = trend.render_trend_report(bundle)
    return report, True


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["market", "sentiment", "news", "fundamentals", "trend", "macro"])
    ap.add_argument("ticker")
    ap.add_argument("--date", default=None)
    args = ap.parse_args(argv)
    t = args.ticker.strip().upper()
    if args.kind == "market":
        out = _safe(lambda: fetch_market(t, args.date))
    elif args.kind == "sentiment":
        out = _safe(lambda: fetch_sentiment(t))
    elif args.kind == "news":
        out = _safe(lambda: fetch_news(t, args.date))
    elif args.kind == "trend":
        out = _safe(lambda: fetch_trend(t, args.date))
    elif args.kind == "macro":
        out = _safe(lambda: fetch_macro(args.date))   # market-wide; ticker ignored
    else:
        out = _safe(lambda: fetch_fundamentals(t, args.date))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"report": "UNAVAILABLE: internal error", "core_available": False}))
        sys.exit(1)
