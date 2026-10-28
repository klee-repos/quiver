"""Earnings-call transcripts from Alpha Vantage (plan §7.6).

One call asks for one ticker and one quarter. The ledger counts each call in
``api_budget`` (``provider='alphavantage'``) and stops at 25 calls a day (ET date).
A stored transcript is one ``transcript`` document, one line per segment:
``Speaker (Title): content``. A ``transcripts`` row points to it.

The API key is a secret. It never appears in a return value, an exception text or a log
line: every text that leaves this module goes through ``_scrub``.

``quarter_for`` picks the quarter to ask for. ``try_pending`` retries each recent
``earnings`` event once per monitor run, for 7 days.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from lib.desk import store

log = logging.getLogger(__name__)

PROVIDER = "alphavantage"
DAILY_LIMIT = 25
RETRY_DAYS = 7
BASE_URL = "https://www.alphavantage.co/query"
TIMEOUT = 30.0
# An earnings release comes within about 90 days of its quarter end. The quarter before
# that ends at least about 105 days before the release. 100 days splits the two.
MATCH_WINDOW_DAYS = 100
_ET = ZoneInfo("America/New_York")
_FP_QUARTER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}


def _scrub(text, api_key: str) -> str:
    """Replace every copy of the key (plain and URL-quoted) with ``***``."""
    out = str(text)
    if api_key:
        for form in {api_key, urllib.parse.quote(api_key, safe=""), urllib.parse.quote_plus(api_key)}:
            if form:
                out = out.replace(form, "***")
    return out


def _as_dt(now) -> datetime:
    dt = store.parse_ts(now)
    if dt is None:
        raise ValueError(f"bad time {now!r}")
    return dt


def budget_day(now) -> str:
    """The ``api_budget`` day for ``now``: the ET calendar date."""
    return _as_dt(now).astimezone(_ET).date().isoformat()


def request_url(ticker: str, quarter: str, api_key: Optional[str]) -> str:
    """The Alpha Vantage URL for one transcript (§7.6 step 1). With ``api_key=None`` the
    URL has no ``apikey`` parameter: that is the form the document stores."""
    params = {"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": ticker.upper(), "quarter": quarter}
    if api_key is not None:
        params["apikey"] = api_key
    return f"{BASE_URL}?{urllib.parse.urlencode(params)}"


def _default_opener(url: str, headers: dict, timeout: float):
    """The production opener: ``(status, headers, body)``. It never raises for a status."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b""


def format_transcript(segments: list) -> str:
    """One line per segment: ``Speaker (Title): content``. A line break inside a segment
    becomes a space, so one segment stays one line."""
    lines = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        speaker = " ".join(str(seg.get("speaker") or "Unknown").split())
        title = " ".join(str(seg.get("title") or "").split())
        content = " ".join(str(seg.get("content") or "").split())
        head = f"{speaker} ({title})" if title else speaker
        lines.append(f"{head}: {content}")
    return "\n".join(lines)


def _valid_quarter(quarter: str) -> bool:
    q = str(quarter or "")
    return len(q) == 6 and q[:4].isdigit() and q[4] == "Q" and q[5] in "1234"


def fetch_transcript(conn, ticker: str, quarter: str, *, api_key: str, now, opener=None) -> dict:
    """Fetch and store one transcript. Returns ``{state, document_id?, quarter, error?}``.

    ``state`` is ``stored`` (a document exists), ``empty`` (the call found no transcript
    yet), ``error`` (the call failed), ``no_budget`` (25 calls today; no call sent), or
    ``no_key`` (no API key; no call sent). A stored quarter returns its document with no
    call. Each sent call counts one budget unit, also when it fails.
    """
    ticker = str(ticker or "").strip().upper()
    if not ticker or not _valid_quarter(quarter):
        raise ValueError(f"bad ticker or quarter: {ticker!r} {quarter!r}")
    now_dt = _as_dt(now)
    now_s = store.iso(now_dt)
    row = conn.execute("SELECT document_id FROM transcripts WHERE ticker=? AND quarter=?",
                       (ticker, quarter)).fetchone()
    if row:
        return {"state": "stored", "document_id": int(row[0]), "quarter": quarter, "cached": True}
    if not api_key:
        return {"state": "no_key", "quarter": quarter}
    if not store.budget_take(conn, PROVIDER, budget_day(now_dt), DAILY_LIMIT):
        return {"state": "no_budget", "quarter": quarter}
    # Commit the budget row before the network call: no write lock waits on Alpha Vantage.
    if conn.in_transaction:
        conn.commit()

    url = request_url(ticker, quarter, api_key)
    safe_url = request_url(ticker, quarter, None)
    try:
        status, _headers, body = (opener or _default_opener)(url, {"User-Agent": "Quiver"}, TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - the opener may raise any network error
        err = _scrub(f"{type(exc).__name__}: {exc}", api_key)[:300]
        log.warning("transcript %s %s: call failed: %s (%s)", ticker, quarter, err, safe_url)
        return {"state": "error", "quarter": quarter, "error": err}
    if status != 200:
        err = f"HTTP {status}"
        log.warning("transcript %s %s: %s (%s)", ticker, quarter, err, safe_url)
        return {"state": "error", "quarter": quarter, "error": err}
    try:
        data = json.loads(body.decode("utf-8", "replace") if isinstance(body, bytes) else body)
    except (TypeError, ValueError):
        log.warning("transcript %s %s: the body is not JSON (%s)", ticker, quarter, safe_url)
        return {"state": "error", "quarter": quarter, "error": "the body is not JSON"}
    if not isinstance(data, dict):
        return {"state": "error", "quarter": quarter, "error": "the body is not a JSON object"}
    segments = data.get("transcript")
    if not isinstance(segments, list):
        # Alpha Vantage sends a note ("Information", "Note", "Error Message") in place of
        # the transcript when it refuses a call.
        note = next((str(data[k]) for k in ("Error Message", "Information", "Note") if k in data), "")
        err = _scrub(note or "no transcript field in the reply", api_key)[:300]
        log.warning("transcript %s %s: %s (%s)", ticker, quarter, err, safe_url)
        return {"state": "error", "quarter": quarter, "error": err}
    if not segments:
        return {"state": "empty", "quarter": quarter}
    text = format_transcript(segments)
    if not text.strip():
        return {"state": "empty", "quarter": quarter}
    doc_id = store.store_document(conn, kind="transcript", ticker=ticker, now=now_s,
                                  url=request_url(ticker, quarter, None),
                                  title=f"{ticker} {quarter} earnings call transcript", text=text)
    conn.execute("INSERT INTO transcripts (ticker, quarter, document_id, fetched_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(ticker, quarter) DO UPDATE SET document_id=excluded.document_id, "
                 "fetched_at=excluded.fetched_at", (ticker, quarter, doc_id, now_s))
    log.info("transcript %s %s: stored document %s (%s lines)", ticker, quarter, doc_id,
             text.count("\n") + 1)
    return {"state": "stored", "document_id": doc_id, "quarter": quarter}


def _as_date(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def calendar_quarter_before(report_date) -> str:
    """The calendar quarter that ended before ``report_date`` (2026-08-04 -> ``2026Q2``)."""
    d = _as_date(report_date)
    if d is None:
        raise ValueError(f"bad report_date {report_date!r}")
    q = (d.month - 1) // 3 + 1
    year, q = (d.year, q - 1) if q > 1 else (d.year - 1, 4)
    return f"{year}Q{q}"


def fiscal_quarter(conn, *, cik: str, report_date) -> Optional[str]:
    """The fiscal quarter (``fy`` + ``fp``) of the XBRL period that the release reports.

    That period has the latest ``end_date`` before ``report_date`` and within 100 days of
    it. Its first filing (``filed`` ascending) gives ``fy`` and ``fp``, because a later
    filing repeats the period as a comparative with its own ``fy``. ``dei`` facts do not
    count: their cover date comes after the period end. None when no fact fits.
    """
    d = _as_date(report_date)
    if not cik or d is None:
        return None
    cik10 = str(cik).strip().zfill(10)
    low = (d - timedelta(days=MATCH_WINDOW_DAYS)).isoformat()
    row = conn.execute(
        "SELECT fy, fp FROM xbrl_facts WHERE cik=? AND taxonomy != 'dei' "
        "AND end_date < ? AND end_date >= ? "
        "AND fy IS NOT NULL AND fp IN ('Q1','Q2','Q3','Q4','FY') "
        "AND (form IS NULL OR form IN ('10-Q','10-K','10-Q/A','10-K/A','20-F','40-F')) "
        "ORDER BY end_date DESC, COALESCE(filed, '') ASC LIMIT 1",
        (cik10, d.isoformat(), low)).fetchone()
    if not row:
        return None
    return f"{int(row[0])}Q{_FP_QUARTER[str(row[1])]}"


def quarter_for(conn, *, cik: str, report_date) -> list:
    """The quarters to ask for, best first: the fiscal quarter from XBRL when it exists,
    then the calendar quarter that ended before ``report_date``. No repeats."""
    out = []
    fq = fiscal_quarter(conn, cik=cik, report_date=report_date)
    if fq:
        out.append(fq)
    cq = calendar_quarter_before(report_date)
    if cq not in out:
        out.append(cq)
    return out


def _event_filing(conn, event: dict) -> dict:
    """The accession, CIK and report date behind one ``earnings`` event."""
    detail = store.loads(event.get("detail_json"), {}) or {}
    accession = detail.get("accession")
    key = str(event.get("dedup_key") or "")
    if not accession and key.startswith("filing:"):
        accession = key.split(":", 1)[1]
    filing = None
    if accession:
        row = conn.execute("SELECT cik, report_date, filed_date FROM filings WHERE accession=?",
                           (accession,)).fetchone()
        filing = dict(row) if row else None
    company = store.get_company(conn, event["ticker"]) or {}
    cik = detail.get("cik") or (filing or {}).get("cik") or company.get("cik")
    report = (detail.get("report_date") or (filing or {}).get("report_date")
              or detail.get("filed_date") or (filing or {}).get("filed_date")
              or str(event.get("detected_at") or "")[:10])
    return {"accession": accession, "cik": cik, "report_date": report}


def try_pending(conn, *, api_key: str, now, opener=None) -> list:
    """Try each ``earnings`` event of the last 7 days that has no transcript yet, once.

    One call per (ticker, quarter) per run. Returns one dict per try: ``ticker``,
    ``event_id``, ``quarter`` and the ``fetch_transcript`` result. Without a key it sends
    nothing and returns the ``no_key`` state for each pending event.
    """
    now_dt = _as_dt(now)
    since = store.iso(now_dt - timedelta(days=RETRY_DAYS))
    rows = conn.execute(
        "SELECT * FROM events WHERE type='earnings' AND detected_at >= ? ORDER BY detected_at, id",
        (since,)).fetchall()
    out, seen = [], set()
    for row in rows:
        event = dict(row)
        ticker = str(event["ticker"]).upper()
        info = _event_filing(conn, event)
        try:
            quarters = quarter_for(conn, cik=info["cik"], report_date=info["report_date"])
        except ValueError as exc:
            out.append({"ticker": ticker, "event_id": event["id"], "state": "error",
                        "error": str(exc)})
            continue
        if any(conn.execute("SELECT 1 FROM transcripts WHERE ticker=? AND quarter=?",
                            (ticker, q)).fetchone() for q in quarters):
            continue
        quarter = quarters[0]
        if (ticker, quarter) in seen:
            continue
        seen.add((ticker, quarter))
        res = fetch_transcript(conn, ticker, quarter, api_key=api_key, now=now_dt, opener=opener)
        out.append({"ticker": ticker, "event_id": event["id"], **res})
    return out
