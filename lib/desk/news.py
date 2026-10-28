"""The news sweep: headlines, classification, and news events (plan §7.8, §5.4).

1. ``ingest`` fetches yfinance headlines for every ``holding`` and ``watch`` name and
   inserts new rows into ``news_items``. The key is ``(url_hash, ticker)``, with
   ``url_hash`` = SHA-256 of the URL, so one article can concern several tickers.
2. ``queue_classify`` queues one ``classify_news`` job with at most 40 headlines, oldest
   first. ``classifier_input`` gives the model its rows. ``apply_classification`` stores
   the reply in the §5.3 shape. A headline with no valid answer gets one more attempt;
   after 3 attempts it is stored as not material with ``reason='unclassified'``.
3. ``handle_material`` fetches each material article that was classified today and
   creates a ``news`` event (``news:<ticker>:<url_hash>``). A fetch failure keeps the
   headline. At most 3 such events per ticker per day; the rest stay recorded with no event.
4. ``log_news_event`` is the desk tool path: one event from a stored ``news`` document.

Days are ET calendar days. Times in the ledger are UTC strings.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from lib.desk import store

log = logging.getLogger(__name__)

CATEGORIES = ("contract", "regulation", "m&a", "management", "guidance", "litigation",
              "product", "other")
BATCH_LIMIT = 40
MAX_ATTEMPTS = 3
REASON_MAX = 160
EVENTS_PER_DAY = 3          # monitor news events per ticker per day (§7.8 step 3)
DESK_EVENTS_PER_DAY = 1     # desk news events per ticker per day (§5.4 log_news_event)
DESK_MAX_AGE_DAYS = 7
NEWS_STATUSES = ("holding", "watch")
_ET = ZoneInfo("America/New_York")
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


class NewsError(RuntimeError):
    """A news action was refused. The text gives the reason."""


# --- helpers -------------------------------------------------------------------------

def _dt(now) -> datetime:
    dt = store.parse_ts(now)
    if dt is None:
        raise ValueError(f"bad time {now!r}")
    return dt


def _day_bounds(now) -> tuple:
    """The UTC strings that bound the ET calendar day of ``now``: ``[start, end)``."""
    et = _dt(now).astimezone(_ET)
    start = datetime(et.year, et.month, et.day, tzinfo=_ET)
    end = start + timedelta(days=1)
    return store.iso(start), store.iso(end)


def url_hash(url: str) -> str:
    """The ``news_items`` key of one URL: SHA-256 of the URL text."""
    return store.sha256_hex(str(url))


def _iso_or_none(value) -> Optional[str]:
    if value is None or value == "":
        return None
    dt = store.parse_ts(value)
    return store.iso(dt) if dt else None


def _items(payload) -> list:
    """The ``(url_hash, ticker)`` keys of a ``classify_news`` payload, in order."""
    if isinstance(payload, (str, bytes)):
        payload = store.loads(payload, {}) or {}
    out = []
    for item in (payload or {}).get("items") or []:
        if isinstance(item, dict) and item.get("url_hash") and item.get("ticker"):
            out.append((str(item["url_hash"]), str(item["ticker"]).upper()))
    return out


# --- 1. ingest -----------------------------------------------------------------------

def news_tickers(conn) -> list:
    """The names the sweep fetches: every ``holding`` and ``watch`` company that is not cash."""
    rows = conn.execute(
        "SELECT ticker FROM companies WHERE status IN ('holding','watch') AND kind != 'cash' "
        "ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


def ingest(conn, tickers=None, *, now, fetch_fn=None, limit: Optional[int] = None) -> int:
    """Fetch headlines and insert the new ones. Returns the number of new rows.

    ``tickers=None`` means ``news_tickers(conn)``. ``fetch_fn(ticker, limit)`` returns rows
    in the ``fetch_news_items`` shape; the default is that function. A row whose
    ``related_tickers`` list does not name the ticker is skipped (a fuzzy search hit).
    ``published_at`` comes from the feed. A fetch error for one ticker is logged and the
    sweep goes on; ``NewsError`` when every fetch fails.
    """
    if fetch_fn is None:
        from lib.dataflows.yfinance_news import fetch_news_items as fetch_fn
    now_s = store.iso(_dt(now))
    names = news_tickers(conn) if tickers is None else [str(t).strip().upper() for t in tickers if t]
    inserted, errors = 0, []
    # Fetch every ticker first, then insert: no write lock is held during a network call.
    if conn.in_transaction:
        conn.commit()
    fetched = []
    for ticker in names:
        try:
            fetched.append((ticker, fetch_fn(ticker, limit) or []))
        except Exception as exc:  # noqa: BLE001 - yfinance raises many types
            errors.append(f"{ticker}: {type(exc).__name__}: {exc}"[:300])
            log.warning("news fetch failed for %s: %s", ticker, exc)
    for ticker, rows in fetched:
        for row in rows:
            link = str(row.get("link") or "").strip()
            title = str(row.get("title") or "").strip()
            if not link or not title:
                continue
            related = {str(t).strip().upper() for t in (row.get("related_tickers") or []) if t}
            if related and ticker not in related:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO news_items (url_hash, ticker, title, publisher, url, "
                "published_at, seen_at) VALUES (?,?,?,?,?,?,?)",
                (url_hash(link), ticker, title[:500], row.get("publisher") or None, link,
                 _iso_or_none(row.get("pub_date")), now_s))
            inserted += cur.rowcount
    if names and len(errors) == len(names):
        raise NewsError("news fetch failed for every ticker: " + "; ".join(errors)[:900])
    return inserted


# --- 2. classify ---------------------------------------------------------------------

def queue_classify(conn, now) -> Optional[int]:
    """Queue one ``classify_news`` job for at most 40 unclassified headlines, oldest
    first. Returns the job id, or None when nothing waits or a job is queued or running."""
    busy = conn.execute("SELECT id FROM jobs WHERE kind='classify_news' AND status IN "
                        "('queued','running') LIMIT 1").fetchone()
    if busy:
        return None
    rows = conn.execute(
        "SELECT url_hash, ticker FROM news_items WHERE material IS NULL AND classify_attempts < ? "
        "ORDER BY COALESCE(published_at, seen_at), seen_at, ticker, url_hash LIMIT ?",
        (MAX_ATTEMPTS, BATCH_LIMIT)).fetchall()
    if not rows:
        return None
    payload = {"items": [{"url_hash": r[0], "ticker": r[1]} for r in rows]}
    return store.enqueue_job(conn, kind="classify_news", payload=payload, requested_by="monitor",
                             now=store.iso(_dt(now)))


def classifier_input(conn, payload) -> list:
    """The rows the classifier sees: ``[{id, ticker, title, publisher}]``. ``id`` is the
    1-based place of the key in the payload. A row that is gone or classified is left out."""
    out = []
    for idx, (h, ticker) in enumerate(_items(payload), start=1):
        row = conn.execute("SELECT title, publisher, material FROM news_items WHERE url_hash=? "
                           "AND ticker=?", (h, ticker)).fetchone()
        if row is None or row[2] is not None:
            continue
        out.append({"id": idx, "ticker": ticker, "title": row[0], "publisher": row[1]})
    return out


def parse_reply(reply_text) -> Optional[list]:
    """The JSON array of a classifier reply, or None when it does not parse. A fenced
    ```json block and text around the array are allowed."""
    text = str(reply_text or "").strip()
    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            return None
    if isinstance(data, dict) and "id" in data:
        data = [data]
    return data if isinstance(data, list) else None


def _answer(el, n_items: int):
    """One reply element as ``(id, material, category, reason)``; None when it is not valid."""
    if not isinstance(el, dict):
        return None
    raw_id = el.get("id")
    if isinstance(raw_id, bool):
        return None
    if isinstance(raw_id, str) and raw_id.strip().isdigit():
        raw_id = int(raw_id.strip())
    if not isinstance(raw_id, int) or not 1 <= raw_id <= n_items:
        return None
    material = el.get("material")
    category = str(el.get("category") or "").strip().lower()
    if not isinstance(material, bool) or category not in CATEGORIES:
        return ("bad", raw_id)
    reason = " ".join(str(el.get("reason") or "").split())[:REASON_MAX]
    return (raw_id, material, category, reason)


def apply_classification(conn, payload, reply_text, *, now) -> dict:
    """Store one classifier reply (§5.3 shape) for the headlines of ``payload``.

    A valid answer sets ``material``, ``category``, ``reason`` (cut to 160 chars) and
    ``classified_at``. An id that is not in the payload is ignored. A headline with no
    valid answer (a reply that does not parse, a missing id, an unknown category) gets one
    more ``classify_attempts``; at 3 it is stored ``material=0``, ``reason='unclassified'``.
    A failed job counts as one attempt when its runner calls this with ``reply_text=''``.
    """
    now_s = store.iso(_dt(now))
    keys = _items(payload)
    parsed = parse_reply(reply_text)
    answers, ignored, invalid = {}, [], []
    for el in parsed or []:
        ans = _answer(el, len(keys))
        if ans is None:
            ignored.append(el.get("id") if isinstance(el, dict) else None)
            continue
        if ans[0] == "bad":
            invalid.append(ans[1])
            continue
        answers.setdefault(ans[0], ans[1:])
    result = {"parsed": parsed is not None, "classified": 0, "material": [], "retry": 0,
              "gave_up": 0, "ignored_ids": ignored, "invalid_ids": invalid}
    for idx, (h, ticker) in enumerate(keys, start=1):
        row = conn.execute("SELECT material, classify_attempts FROM news_items WHERE url_hash=? "
                           "AND ticker=?", (h, ticker)).fetchone()
        if row is None or row[0] is not None:
            continue
        if idx in answers:
            material, category, reason = answers[idx]
            conn.execute("UPDATE news_items SET material=?, category=?, reason=?, classified_at=? "
                         "WHERE url_hash=? AND ticker=?",
                         (1 if material else 0, category, reason, now_s, h, ticker))
            result["classified"] += 1
            if material:
                result["material"].append({"url_hash": h, "ticker": ticker})
            continue
        attempts = int(row[1] or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            conn.execute("UPDATE news_items SET classify_attempts=?, material=0, category=NULL, "
                         "reason='unclassified', classified_at=? WHERE url_hash=? AND ticker=?",
                         (attempts, now_s, h, ticker))
            result["gave_up"] += 1
        else:
            conn.execute("UPDATE news_items SET classify_attempts=? WHERE url_hash=? AND ticker=?",
                         (attempts, h, ticker))
            result["retry"] += 1
    return result


# --- 3. material items -> events -----------------------------------------------------

def _news_events_today(conn, ticker: str, created_by: str, now) -> int:
    start, end = _day_bounds(now)
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type='news' AND ticker=? AND created_by=? "
                       "AND detected_at >= ? AND detected_at < ?",
                       (ticker, created_by, start, end)).fetchone()
    return int(row[0])


EVENT_MAX_AGE_S = 7 * 24 * 3600


def handle_material(conn, *, now, fetch_document) -> list:
    """Create a ``news`` event for each material headline classified today and published
    within the last 7 days. Returns the new event ids.

    ``fetch_document(url, kind='news', ticker=...) -> document_id`` fetches the article.
    A fetch failure (a paywall 403, a timeout) keeps the headline and puts the error in
    the event detail. A ticker gets at most 3 monitor news events per ET day; a headline
    over that cap is not fetched and gets no event, now or later. Call this in the same
    job as ``apply_classification``.
    """
    now_dt = _dt(now)
    now_s = store.iso(now_dt)
    start, end = _day_bounds(now_dt)
    rows = conn.execute(
        "SELECT url_hash, ticker, title, publisher, url, published_at, category, reason, "
        "document_id FROM news_items WHERE material=1 AND classified_at >= ? AND classified_at < ? "
        "ORDER BY COALESCE(published_at, seen_at), seen_at, ticker, url_hash", (start, end)).fetchall()
    created = []
    for row in rows:
        h, ticker, title, publisher, url, published_at, category, reason, doc_id = tuple(row)
        key = f"news:{ticker}:{h}"
        if conn.execute("SELECT 1 FROM events WHERE dedup_key=?", (key,)).fetchone():
            continue
        pub = store.parse_ts(published_at)
        if pub is not None and (now_dt - pub).total_seconds() > EVENT_MAX_AGE_S:
            # An old headline is not a change: after the seed, the first feed holds weeks of news.
            continue
        if _news_events_today(conn, ticker, "monitor", now_dt) >= EVENTS_PER_DAY:
            log.info("news %s: cap of %s events today; %s recorded with no event",
                     ticker, EVENTS_PER_DAY, h[:12])
            continue
        fetch_error = None
        if doc_id is None:
            # fetch_document stores the page on its own connection: release this one's
            # write lock first, else that store waits 30 s and fails "database is locked".
            if conn.in_transaction:
                conn.commit()
            try:
                doc_id = fetch_document(url, kind="news", ticker=ticker)
            except Exception as exc:  # noqa: BLE001 - any fetch failure keeps the headline
                fetch_error = f"{type(exc).__name__}: {exc}"[:300]
                doc_id = None
            if doc_id is not None:
                conn.execute("UPDATE news_items SET document_id=? WHERE url_hash=? AND ticker=? "
                             "AND document_id IS NULL", (int(doc_id), h, ticker))
        detail = {"url_hash": h, "url": url, "title": title, "publisher": publisher,
                  "published_at": published_at, "category": category, "reason": reason,
                  "document_id": int(doc_id) if doc_id is not None else None,
                  "fetch_error": fetch_error}
        eid = store.create_event(conn, dedup_key=key, ticker=ticker, type="news", detail=detail,
                                 now=now_s, created_by="monitor")
        if eid:
            created.append(eid)
    return created


# --- 4. the desk tool ----------------------------------------------------------------

def log_news_event(conn, *, ticker: str, document_id: int, now) -> int:
    """Create one desk ``news`` event from a stored ``news`` document (§5.4). Returns its id.

    The document must be a ``news`` document that a ``news_items`` row of ``ticker`` points
    to. ``published_at`` comes from that row (the feed, never the page) and must be within
    the last 7 days. Two feed URLs with the same text share one document: the newest
    ``published_at`` of those rows counts. A ``web`` document backs no event: a page controls its own date. At
    most one desk news event per ticker per ET day. Raises ``NewsError`` with the reason.
    """
    now_dt = _dt(now)
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        raise NewsError("a ticker is required")
    try:
        doc = store.get_document(conn, int(document_id))
    except (TypeError, ValueError):
        doc = None
    if doc is None:
        raise NewsError(f"no document {document_id}")
    if doc["kind"] != "news":
        raise NewsError(f"document {document_id} is a {doc['kind']} document; only a news document "
                        "from the feed backs a news event (a web page controls its own date)")
    row = conn.execute("SELECT url_hash, title, publisher, url, published_at, category, reason "
                       "FROM news_items WHERE document_id=? AND ticker=? "
                       "ORDER BY published_at IS NULL, published_at DESC LIMIT 1",
                       (int(document_id), ticker)).fetchone()
    if row is None:
        raise NewsError(f"no news_items row of {ticker} points to document {document_id}; fetch the "
                        "feed URL with fetch_document(kind='news') first")
    h, title, publisher, url, published_at, category, reason = tuple(row)
    pub = store.parse_ts(published_at)
    if pub is None:
        raise NewsError(f"the feed gives no published_at for document {document_id}")
    if now_dt - pub > timedelta(days=DESK_MAX_AGE_DAYS):
        raise NewsError(f"document {document_id} was published {published_at}, more than "
                        f"{DESK_MAX_AGE_DAYS} days ago")
    if pub - now_dt > timedelta(hours=1):
        raise NewsError(f"document {document_id} has a published_at in the future: {published_at}")
    key = f"news:{ticker}:{h}"
    old = conn.execute("SELECT id FROM events WHERE dedup_key=?", (key,)).fetchone()
    if old:
        raise NewsError(f"event {old[0]} already covers this news item")
    if _news_events_today(conn, ticker, "desk", now_dt) >= DESK_EVENTS_PER_DAY:
        raise NewsError(f"{ticker} already has a desk news event today")
    detail = {"url_hash": h, "url": url, "title": title, "publisher": publisher,
              "published_at": published_at, "category": category, "reason": reason,
              "document_id": int(document_id), "fetch_error": None}
    eid = store.create_event(conn, dedup_key=key, ticker=ticker, type="news", detail=detail,
                             now=store.iso(now_dt), created_by="desk")
    if not eid:
        raise NewsError("the event was not created")
    return eid
