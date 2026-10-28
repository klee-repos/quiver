"""yfinance news: headline rows with a URL for the desk news sweep (plan §7.8).

``fetch_news_items`` returns one dict per headline and raises on a yfinance error, so a
caller can tell "no news" from "the fetch failed". ``yf.Search(t).news`` is the first
source: on 2026-10-28 ``yf.Ticker(t).get_news()`` returned an empty list for every ticker.
"""

from typing import Optional

import yfinance as yf
from datetime import datetime, timezone

from .config import get_config
from .stockstats_utils import yf_retry


def _extract_article_data(article: dict) -> dict:
    """Extract article data from yfinance news format (handles nested 'content' structure)."""
    # Handle nested content structure
    if "content" in article:
        content = article["content"]
        title = content.get("title", "No title")
        summary = content.get("summary", "")
        provider = content.get("provider", {})
        publisher = provider.get("displayName", "Unknown")

        # Get URL from canonicalUrl or clickThroughUrl
        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        link = url_obj.get("url", "")

        # Get publish date
        pub_date_str = content.get("pubDate", "")
        pub_date = None
        if pub_date_str:
            try:
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return {
            "title": title,
            "summary": summary,
            "publisher": publisher,
            "link": link,
            "pub_date": pub_date,
        }
    else:
        # Flat structure — this is what ``yf.Search(...).news`` actually returns (keys:
        # title/publisher/link/providerPublishTime/...). Its timestamp is a Unix epoch int,
        # NOT the ISO ``pubDate`` the nested shape carries. This used to hardcode
        # ``pub_date: None``, which silently disabled every date filter downstream.
        pub_date = None
        ts = article.get("providerPublishTime")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            try:
                pub_date = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                pub_date = None
        return {
            "title": article.get("title", "No title"),
            "summary": article.get("summary", ""),
            "publisher": article.get("publisher", "Unknown"),
            "link": article.get("link", ""),
            "pub_date": pub_date,
        }


def _related_tickers(article: dict) -> list:
    """The tickers yfinance links to one article, for both shapes. Empty when unknown."""
    if "content" in article:
        fin = (article.get("content") or {}).get("finance") or {}
        out = []
        for item in fin.get("stockTickers") or []:
            sym = item.get("symbol") if isinstance(item, dict) else item
            if isinstance(sym, str) and sym:
                out.append(sym)
        return out
    rel = article.get("relatedTickers") or []
    return [t for t in rel if isinstance(t, str) and t]


def _default_search(ticker: str, count: int) -> list:
    return list(yf.Search(ticker, news_count=count, raise_errors=True).news or [])


def _default_ticker_news(ticker: str, count: int) -> list:
    return list(yf.Ticker(ticker).get_news(count=count) or [])


def fetch_news_items(ticker: str, limit: Optional[int] = None, *, search_fn=None,
                     ticker_news_fn=None) -> list:
    """Headlines for ``ticker``: ``[{title, publisher, link, pub_date, related_tickers}]``.

    ``pub_date`` is an aware UTC datetime or None. The function asks ``yf.Search`` first
    and ``yf.Ticker.get_news`` when the search fails or returns nothing. It skips a row
    with no link or no title, and it keeps the first row of each link. It raises the
    yfinance error when every source fails. An empty list means "no news", not an error.
    ``search_fn`` and ``ticker_news_fn`` are the test seams: ``(ticker, count) -> [article]``.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError(f"ticker must be a non-empty string, got {ticker!r}")
    ticker = ticker.strip().upper()
    count = int(limit or get_config()["news_article_limit"])
    search_fn = search_fn or _default_search
    ticker_news_fn = ticker_news_fn or _default_ticker_news

    error = None
    articles = []
    try:
        articles = yf_retry(lambda: search_fn(ticker, count)) or []
    except Exception as exc:  # noqa: BLE001 - yfinance raises many types
        error = exc
    if not articles:
        try:
            articles = yf_retry(lambda: ticker_news_fn(ticker, count)) or []
        except Exception as exc:  # noqa: BLE001
            error = error or exc
            articles = []
        if not articles and error is not None:
            raise error

    rows, seen = [], set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        data = _extract_article_data(article)
        link = str(data.get("link") or "").strip()
        title = str(data.get("title") or "").strip()
        if not link or not title or title == "No title" or link in seen:
            continue
        seen.add(link)
        pub = data.get("pub_date")
        if pub is not None and pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        rows.append({
            "title": title,
            "publisher": data.get("publisher") or None,
            "link": link,
            "pub_date": pub.astimezone(timezone.utc) if pub is not None else None,
            "related_tickers": _related_tickers(article),
        })
        if len(rows) >= count:
            break
    return rows
