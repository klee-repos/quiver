"""yfinance-based news data fetching functions."""

from typing import Optional

import yfinance as yf
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta

from .config import get_config
from .stockstats_utils import yf_retry

# curr_date is a TRADING date, i.e. an exchange-calendar date in ET (analyze.py derives it
# from trading_day_et()). Never compare publish times against the OS timezone.
_ET = ZoneInfo("America/New_York")


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


def get_news_yfinance(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """
    Retrieve news for a specific stock ticker using yfinance.

    Args:
        ticker: Stock ticker symbol (e.g., "AAPL")
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        Formatted string containing news articles
    """
    article_limit = get_config()["news_article_limit"]
    try:
        stock = yf.Ticker(ticker)
        news = yf_retry(lambda: stock.get_news(count=article_limit))

        if not news:
            return f"No news found for {ticker}"

        # Parse date range for filtering
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        news_str = ""
        filtered_count = 0

        for article in news:
            data = _extract_article_data(article)

            # Filter by date if publish time is available
            if data["pub_date"]:
                pub_date_naive = data["pub_date"].replace(tzinfo=None)
                if not (start_dt <= pub_date_naive <= end_dt + relativedelta(days=1)):
                    continue

            news_str += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                news_str += f"{data['summary']}\n"
            if data["link"]:
                news_str += f"Link: {data['link']}\n"
            news_str += "\n"
            filtered_count += 1

        if filtered_count == 0:
            return f"No news found for {ticker} between {start_date} and {end_date}"

        return f"## {ticker} News, from {start_date} to {end_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching news for {ticker}: {str(e)}"


def get_global_news_yfinance(
    curr_date: str,
    look_back_days: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """
    Retrieve global/macro economic news using yfinance Search.

    Args:
        curr_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles to return. ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        Formatted string containing global news articles
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    search_queries = config["global_news_queries"]

    all_news = []
    seen_titles = set()
    # Take a few from EACH query (a per-query cap) rather than filling `limit` from the
    # first query and breaking — otherwise the first query (Fed) saturates the cap and
    # the later oil/energy + sector queries (which carry e.g. an Iran/Strait-of-Hormuz
    # oil shock) never get sampled. ceil(limit / n_queries) per topic.
    per_query = max(2, -(-limit // max(1, len(search_queries))))

    try:
        for query in search_queries:
            search = yf_retry(lambda q=query: yf.Search(
                query=q,
                news_count=limit,
                enable_fuzzy_query=True,
            ))

            taken = 0
            if search.news:
                for article in search.news:
                    # _extract_article_data handles both the nested and flat shapes. Treat
                    # its "No title" placeholder as untitled so a title-less article stays
                    # skipped, exactly as the old flat branch's "" default did.
                    title = _extract_article_data(article)["title"]
                    if title == "No title":
                        title = ""

                    # Deduplicate by title
                    if title and title not in seen_titles:
                        seen_titles.add(title)
                        all_news.append(article)
                        taken += 1
                        if taken >= per_query:
                            break   # move to the next TOPIC so every query is represented

        if not all_news:
            return f"No global news found for {curr_date}"

        # Calculate date range
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - relativedelta(days=look_back_days)
        start_date = start_dt.strftime("%Y-%m-%d")

        news_str = ""
        rendered = 0
        # curr_date is an ET calendar date, so the cutoff is the end of curr_date IN ET.
        # The original ``+1 day`` slack WAS the UTC->ET compensation for a naive compare;
        # keeping it on top of the explicit ET conversion below would apply the correction
        # twice and admit the ENTIRE next trading session (curr+1 open through close) as
        # today's macro context.
        cutoff = curr_dt.date()
        for article in all_news[:limit]:
            # One extraction path for both shapes. This guard used to sit inside an
            # ``if "content" in article:`` branch, but yf.Search returns the FLAT shape, so
            # it never ran and future-dated headlines were rendered as current macro.
            data = _extract_article_data(article)
            pub = data.get("pub_date")
            if pub is not None:
                # Both shapes' timestamps are UTC (the flat epoch is converted to naive UTC
                # in _extract_article_data; the nested ISO one carries its own offset), so a
                # naive value is assumed UTC. Compare CALENDAR DATES in ET: a datetime
                # compare against a naive local midnight puts the boundary at ~20:00 ET and
                # silently drops same-day evening headlines.
                aware = pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc)
                if aware.astimezone(_ET).date() > cutoff:
                    continue
            news_str += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                news_str += f"{data['summary']}\n"
            if data["link"]:
                news_str += f"Link: {data['link']}\n"
            news_str += "\n"
            rendered += 1

        if not rendered:
            # Every article was filtered out. Returning the bare header here would hand back
            # a >30-char string that quill_data._content_ok scores as REAL DATA, so an empty
            # macro feed would report itself as available. Return the recognized sentinel.
            return f"No global news found for {curr_date}"

        return f"## Global Market News, from {start_date} to {curr_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching global news: {str(e)}"
