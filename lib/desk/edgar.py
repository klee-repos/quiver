"""The SEC EDGAR client, the ticker-to-entity map, and filing rows (plan §7.1, §7.2).

``EdgarClient`` sends ``User-Agent`` and ``Accept-Encoding: gzip`` on every request. It
starts at most ``rps`` requests in any one second per process: each request takes one of
``rps`` tokens, and each token comes back one second after its use. It retries a 429 or a
5xx three times (sleeps 1, 4 and 16 s), then raises ``EdgarError``. A 403 raises
``EdgarBlocked`` at once. A 404 raises ``EdgarNotFound`` and is not retried.

The HTTP seam is ``opener(url, headers, timeout) -> (status, headers, body)``. The opener
never raises for an HTTP status. The default opener uses ``urllib``. A test passes a fake.

The ticker maps (``company_tickers.json``, ``company_tickers_mf.json``) refresh weekly
into ``sec_ticker_map`` with a conditional GET. ``resolve`` looks up ``sec_overrides``,
then ``company_tickers`` (kind ``company``), then ``company_tickers_mf`` (kind ``etf``).
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Optional

from lib.desk import store

log = logging.getLogger("quiver.desk.edgar")

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
COMPANY_TICKERS_URL = f"{SEC_WWW}/files/company_tickers.json"
COMPANY_TICKERS_MF_URL = f"{SEC_WWW}/files/company_tickers_mf.json"
TICKER_MAPS = (("company_tickers", COMPANY_TICKERS_URL), ("company_tickers_mf", COMPANY_TICKERS_MF_URL))
MAP_MAX_AGE_S = 7 * 24 * 3600
RETRY_SLEEPS = (1.0, 4.0, 16.0)
TIMEOUT_S = 30.0


class EdgarError(Exception):
    """A request to EDGAR failed after its retries, or the answer is not usable."""


class EdgarConfigError(EdgarError):
    """The client has no User-Agent. SEC refuses a request without a declared contact."""


class EdgarBlocked(EdgarError):
    """SEC answered 403. The caller logs it and pages once per day (stage ``edgar``)."""


class EdgarNotFound(EdgarError):
    """SEC answered 404."""


class UnknownTicker(LookupError):
    """No override and no SEC ticker map row names this ticker."""


# --- URL helpers -------------------------------------------------------------------

def cik10(cik) -> str:
    """A CIK as a 10-digit, zero-padded string. Accepts an int, ``"1596532"`` or ``"CIK0001596532"``."""
    s = str(cik).strip().upper()
    if s.startswith("CIK"):
        s = s[3:]
    if not s.isdigit():
        raise ValueError(f"bad CIK {cik!r}")
    return s.zfill(10)


def _acc_nodash(accession: str) -> str:
    return str(accession).replace("-", "")


def submissions_url(cik: str) -> str:
    return f"{SEC_DATA}/submissions/CIK{cik10(cik)}.json"


def companyfacts_url(cik: str) -> str:
    return f"{SEC_DATA}/api/xbrl/companyfacts/CIK{cik10(cik)}.json"


def archive_url(cik, accession: str, filename: str) -> str:
    """The URL of one file in a filing folder (``/Archives/edgar/data/<cik>/<acc>/<file>``)."""
    return f"{SEC_WWW}/Archives/edgar/data/{int(cik10(cik))}/{_acc_nodash(accession)}/{filename}"


def index_url(cik, accession: str) -> str:
    """The URL of a filing's ``-index.htm`` page. Only this page labels each exhibit type."""
    return archive_url(cik, accession, f"{accession}-index.htm")


# --- HTTP --------------------------------------------------------------------------

def default_opener(url: str, headers: dict, timeout: float):
    """``urllib`` GET. Returns ``(status, headers, body)``. An HTTP status never raises;
    a network failure raises ``OSError`` (the client retries it)."""
    req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https hosts)
            return int(resp.status), dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read() or b""
        except Exception:  # noqa: BLE001
            pass
        return int(e.code), dict((e.headers or {}).items()), body


def _header(headers: dict, name: str) -> Optional[str]:
    """A header value, case-insensitive."""
    lname = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == lname:
            return v
    return None


def _decode_body(headers: dict, body: bytes) -> bytes:
    """Gunzip a gzip body. The gzip magic bytes also count, because some proxies drop the header."""
    body = body or b""
    enc = (_header(headers, "Content-Encoding") or "").lower()
    if "gzip" in enc or body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except (OSError, EOFError) as e:
            raise EdgarError(f"bad gzip body: {e}") from e
    return body


class _Limiter:
    """At most ``rps`` request starts in any one-second window. Each start takes one of
    ``rps`` tokens; a token comes back one second after its start."""

    def __init__(self, rps: float):
        self.size = max(1, int(rps))
        self.starts: deque = deque()
        self.lock = threading.Lock()

    def wait(self, clock, sleep) -> None:
        with self.lock:
            while len(self.starts) >= self.size:
                gap = 1.0 - (clock() - self.starts[0])
                if gap <= 0:
                    self.starts.popleft()
                    continue
                sleep(gap)
            self.starts.append(clock())


# One limiter per (clock, rps): production clients share ``time.monotonic``, so the
# limit holds for the whole process. A test clock gets its own limiter.
_LIMITERS: dict = {}
_LIMITERS_LOCK = threading.Lock()


def _limiter_for(clock, rps: float) -> _Limiter:
    key = (clock, float(rps))
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(key)
        if lim is None:
            lim = _LIMITERS[key] = _Limiter(rps)
        return lim


class EdgarClient:
    """A rate-limited, retrying EDGAR GET client (§7.1)."""

    def __init__(self, user_agent: str, *, opener=None, sleep=time.sleep, clock=time.monotonic,
                 rps: float = 5.0, timeout: float = TIMEOUT_S):
        ua = str(user_agent or "").strip()
        if not ua:
            raise EdgarConfigError("SEC_USER_AGENT is empty: SEC refuses a request without a contact")
        self.user_agent = ua
        self.opener = opener or default_opener
        self.sleep = sleep
        self.clock = clock
        self.timeout = timeout
        self._limiter = _limiter_for(clock, rps)

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip"}
        h.update(extra or {})
        return h

    def _request(self, url: str, extra: Optional[dict] = None, ok=(200,)):
        """One GET with the limiter and the retry rule. Returns ``(status, headers, body)``
        for a status in ``ok``, else raises."""
        last = ""
        for attempt in range(len(RETRY_SLEEPS) + 1):
            if attempt:
                self.sleep(RETRY_SLEEPS[attempt - 1])
            self._limiter.wait(self.clock, self.sleep)
            try:
                status, headers, body = self.opener(url, self._headers(extra), self.timeout)
            except OSError as e:
                last = f"{type(e).__name__}: {e}"
                continue
            status = int(status)
            headers = dict(headers or {})
            if status in ok:
                return status, headers, (_decode_body(headers, body) if status == 200 else b"")
            if status == 403:
                log.warning("edgar 403 for %s", url)
                raise EdgarBlocked(f"SEC answered 403 for {url}")
            if status == 404:
                raise EdgarNotFound(f"SEC answered 404 for {url}")
            if status == 429 or 500 <= status <= 599:
                last = f"HTTP {status}"
                continue
            raise EdgarError(f"SEC answered HTTP {status} for {url}")
        raise EdgarError(f"{url} failed after {len(RETRY_SLEEPS)} retries ({last})")

    def get(self, url: str) -> bytes:
        """The body of a 200 answer, gunzipped."""
        return self._request(url)[2]

    def get_text(self, url: str) -> str:
        """The body as text: UTF-8, else Windows-1252 (older EDGAR documents)."""
        raw = self.get(url)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", "replace")

    def get_json(self, url: str):
        try:
            return json.loads(self.get(url))
        except ValueError as e:
            raise EdgarError(f"bad JSON from {url}: {e}") from e

    def get_conditional(self, url: str, headers: Optional[dict] = None):
        """A GET that also accepts 304. Returns ``(status, headers, body)``; a 304 body is empty."""
        return self._request(url, headers, ok=(200, 304))


def client_from_env(**kw) -> EdgarClient:
    """A client with ``User-Agent: $SEC_USER_AGENT``. An empty value raises ``EdgarConfigError``."""
    import os
    return EdgarClient(os.environ.get("SEC_USER_AGENT", ""), **kw)


# --- ticker maps -------------------------------------------------------------------

def _map_rows(source: str, data) -> list:
    """``(ticker, cik10, series_id, title)`` rows from one SEC ticker file."""
    rows = []
    if source == "company_tickers":
        for v in (data or {}).values():
            t = str(v.get("ticker") or "").strip().upper()
            if t and v.get("cik_str") is not None:
                rows.append((t, cik10(v["cik_str"]), None, v.get("title")))
    else:
        fields = list((data or {}).get("fields") or [])
        ix = {name: i for i, name in enumerate(fields)}
        for r in (data or {}).get("data") or []:
            t = str(r[ix["symbol"]] or "").strip().upper()
            if t and r[ix["cik"]] is not None:
                rows.append((t, cik10(r[ix["cik"]]), r[ix["seriesId"]] or None, None))
    return rows


def _write_map(conn, source: str, rows: list, now: str) -> int:
    """Upsert one file's rows. A ``company_tickers`` row replaces any row; a
    ``company_tickers_mf`` row never replaces a ``company_tickers`` row (§7.2 order)."""
    if source == "company_tickers":
        sql = ("INSERT INTO sec_ticker_map (ticker, cik, series_id, title, source, fetched_at) "
               "VALUES (?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET cik=excluded.cik, "
               "series_id=excluded.series_id, title=excluded.title, source=excluded.source, "
               "fetched_at=excluded.fetched_at")
    else:
        sql = ("INSERT INTO sec_ticker_map (ticker, cik, series_id, title, source, fetched_at) "
               "VALUES (?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET cik=excluded.cik, "
               "series_id=excluded.series_id, title=excluded.title, source=excluded.source, "
               "fetched_at=excluded.fetched_at WHERE sec_ticker_map.source='company_tickers_mf'")
    conn.executemany(sql, [(t, c, s, title, source, now) for t, c, s, title in rows])
    return len(rows)


def refresh_ticker_maps(conn, client, now, force: bool = False) -> dict:
    """Refresh both SEC ticker files into ``sec_ticker_map`` (§7.1 rule 4).

    A file checked less than 7 days ago sends nothing (unless ``force``). Otherwise send a
    conditional GET with the stored ``Last-Modified`` value. A 304 keeps the rows. A 200
    upserts the rows. ``bridge_state`` keeps ``sec_map:<file>:last_modified`` and
    ``sec_map:<file>:checked_at``. Returns ``{file: {"status", "rows"}}``."""
    now_dt = store.parse_ts(now) or store.utc_now()
    now_s = store.iso(now_dt)
    out = {}
    # company_tickers first, so the mf file cannot claim a ticker that both files hold.
    for source, url in TICKER_MAPS:
        k_lm, k_at = f"sec_map:{source}:last_modified", f"sec_map:{source}:checked_at"
        have = conn.execute("SELECT COUNT(*) FROM sec_ticker_map WHERE source=?", (source,)).fetchone()[0]
        age = store.age_seconds(store.get_state(conn, k_at), now_dt)
        if not force and have and age is not None and age < MAP_MAX_AGE_S:
            out[source] = {"status": "fresh", "rows": int(have)}
            continue
        last_mod = store.get_state(conn, k_lm) if have else None
        extra = {"If-Modified-Since": last_mod} if last_mod else {}
        status, headers, body = client.get_conditional(url, extra)
        if status == 304:
            store.set_state(conn, k_at, now_s)
            out[source] = {"status": 304, "rows": int(have)}
            continue
        try:
            data = json.loads(body)
        except ValueError as e:
            raise EdgarError(f"bad JSON from {url}: {e}") from e
        n = _write_map(conn, source, _map_rows(source, data), now_s)
        lm = _header(headers, "Last-Modified")
        if lm:
            store.set_state(conn, k_lm, lm)
        store.set_state(conn, k_at, now_s)
        out[source] = {"status": 200, "rows": n}
    return out


# --- overrides and resolution (§7.2) -----------------------------------------------

OVERRIDES = (
    ("SPCX", "0001181412", "company",
     "SpaceX (IPO June 2026). The ticker also maps to an old SPAC ETF series that still files."),
    ("BOT", None, "cef",
     "Closed-end fund of private robotics stakes. No public holdings to look through; facts are "
     "NAV, premium or discount, and fees."),
    ("BSOL", None, "crypto_etf", "Spot Solana trust. Facts are price, flows, and fees."),
    ("SGOV", None, "cash",
     "The cash sleeve. As today, `construct_target_book` treats it as residual cash: the desk never "
     "buys, sells, or analyzes it. Its weight is the cash the book keeps."),
)


def seed_overrides(conn) -> int:
    """Write the four §7.2 override rows. An existing row stays. Returns the rows inserted."""
    n = 0
    for ticker, cik, kind, note in OVERRIDES:
        cur = conn.execute("INSERT OR IGNORE INTO sec_overrides (ticker, cik, kind, note) VALUES (?,?,?,?)",
                           (ticker, cik, kind, note))
        n += cur.rowcount
    return n


def _map_row(conn, ticker: str):
    for t in dict.fromkeys((ticker, ticker.replace(".", "-"))):
        row = conn.execute("SELECT * FROM sec_ticker_map WHERE ticker=?", (t,)).fetchone()
        if row:
            return row
    return None


def resolve(conn, ticker: str) -> dict:
    """Ticker -> ``{ticker, cik, kind, series_id, title}`` (§7.2 order). An override with a
    NULL CIK takes the CIK and series id from the map and keeps its own kind. Raises
    ``UnknownTicker`` when nothing names the ticker."""
    t = str(ticker or "").strip().upper()
    if not t:
        raise UnknownTicker("empty ticker")
    ov = conn.execute("SELECT * FROM sec_overrides WHERE ticker=?", (t,)).fetchone()
    row = _map_row(conn, t)
    if ov is not None:
        if ov["cik"]:
            same = row is not None and row["cik"] == cik10(ov["cik"])
            return {"ticker": t, "cik": cik10(ov["cik"]), "kind": ov["kind"],
                    "series_id": row["series_id"] if same else None,
                    "title": row["title"] if same else None}
        return {"ticker": t, "cik": row["cik"] if row else None, "kind": ov["kind"],
                "series_id": row["series_id"] if row else None, "title": row["title"] if row else None}
    if row is None:
        raise UnknownTicker(f"{t} is not in sec_overrides or the SEC ticker maps")
    kind = "company" if row["source"] == "company_tickers" else "etf"
    return {"ticker": t, "cik": row["cik"], "kind": kind, "series_id": row["series_id"],
            "title": row["title"]}


# --- submissions and filing rows ---------------------------------------------------

def _utc_or_none(value) -> Optional[str]:
    dt = store.parse_ts(value)
    return store.iso(dt) if dt else None


def recent_filings(submissions: dict) -> list:
    """The ``filings.recent`` columns of a submissions JSON as one dict per filing:
    accession, form, filed_date, accepted_at (UTC), report_date, items, primary_doc, is_xbrl."""
    rec = ((submissions or {}).get("filings") or {}).get("recent") or {}
    acc = rec.get("accessionNumber") or []

    def col(name, i):
        vals = rec.get(name) or []
        return vals[i] if i < len(vals) else None

    out = []
    for i, a in enumerate(acc):
        out.append({
            "accession": a,
            "form": col("form", i) or "",
            "filed_date": col("filingDate", i) or "",
            "accepted_at": _utc_or_none(col("acceptanceDateTime", i)),
            "report_date": col("reportDate", i) or None,
            "items": col("items", i) or None,
            "primary_doc": col("primaryDocument", i) or None,
            "is_xbrl": bool(col("isXBRL", i)),
        })
    return out


def insert_filing(conn, *, filing: dict, cik, ticker: str, tier: str, now: str) -> bool:
    """Insert one ``filings`` row. A known accession inserts nothing and returns False."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO filings (accession, cik, ticker, form, filed_date, accepted_at, "
        "report_date, items, primary_doc, tier, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (filing["accession"], cik10(cik), str(ticker).upper(), filing["form"], filing["filed_date"],
         filing.get("accepted_at"), filing.get("report_date"), filing.get("items"),
         filing.get("primary_doc"), tier, now))
    return cur.rowcount > 0


def filing_exhibits(client, cik, accession: str) -> list:
    """The rows of a filing's ``-index.htm`` page: ``{seq, description, document, type, url}``."""
    from lib.desk import sections
    url = index_url(cik, accession)
    return sections.parse_index(client.get_text(url), url)
