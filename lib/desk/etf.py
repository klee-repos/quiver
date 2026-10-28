"""ETF holdings and look-through (plan §7, §7.7 step 4, §8.4, §9.1 step 4).

Two sources fill ``etf_holdings``. yfinance ``funds_data.top_holdings`` gives the current top
10 with tickers (source ``yfinance_top``). SEC NPORT-P gives every holding each quarter, 56 to
60 days late, with names and CUSIPs but no ticker (source ``nport``). The SEC map has no CUSIP,
so an N-PORT row maps to a ticker by its normalized name (§18.9 G23).

Each holding of 5% or more with a ticker and a CIK becomes a look-through company with
``status='watch'``. A ``cef`` or ``crypto_etf`` gets no look-through companies (§8.4).

``weighted_lookthrough`` is pure: it takes the holdings and the metrics rows as arguments.
This module reads no trading limit and no broker data.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from typing import Callable, Optional

from lib import market
from lib.desk import edgar, store

NPORT_FORM = "NPORT-P"
LOOKTHROUGH_PCT = 5.0
LOW_COVERAGE_PCT = 50.0
SOURCES = ("yfinance_top", "nport")
# A fund of these kinds holds no public companies to look through (§8.4).
NO_LOOKTHROUGH_KINDS = ("cef", "crypto_etf", "cash")
# Only common equity maps to a company ticker. A preferred stake, a derivative, or a cash
# vehicle (``STIV``) stays in etf_holdings with no ticker.
MAPPED_ASSET_CATS = ("EC",)

_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={series}"
         "&type={form}&dateb=&owner=include&count=40&output=atom")

_STOP_WORDS = frozenset(("inc", "corp", "corporation", "ltd", "co", "holdings", "plc", "sa",
                         "nv", "ag", "adr"))
# An SEC conformed name can end with a state-of-incorporation tag: "APPLIED MATERIALS INC /DE",
# "QUALCOMM INC/DE", "ARM HOLDINGS PLC /UK". The tag is not part of the name.
_STATE_TAG = re.compile(r"(\s*/[a-z0-9]{1,5}/?)+\s*$")
_DELETE = re.compile(r"[.'’]")
_SPLIT = re.compile(r"[^\w]+|_", re.UNICODE)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


# --- parsing -----------------------------------------------------------------------

def _xml_root(text) -> ET.Element:
    """Parse XML text or bytes. A str loses its encoding declaration first, because the
    text is already decoded."""
    if isinstance(text, bytes):
        return ET.fromstring(text)
    return ET.fromstring(_XML_DECL.sub("", str(text or ""), count=1))


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: Optional[ET.Element], name: str) -> Optional[str]:
    if el is None:
        return None
    c = _child(el, name)
    if c is None or c.text is None:
        return None
    return c.text.strip()


def _first(root: ET.Element, name: str) -> Optional[ET.Element]:
    for el in root.iter():
        if _local(el.tag) == name:
            return el
    return None


def _value(raw: Optional[str]) -> Optional[str]:
    """N-PORT writes ``N/A`` for a missing name, CUSIP or LEI. That is no value."""
    if raw is None:
        return None
    raw = raw.strip()
    return None if raw in ("", "N/A", "000000000") else raw


def parse_nport(xml_text) -> dict:
    """Parse one NPORT-P primary document. Returns ``{series_id, as_of, holdings: [{name,
    title, cusip, isin, weight_pct, asset_cat}], ...}``. ``weight_pct`` is ``pctVal`` (percent
    of net assets). A row with no name takes its title."""
    root = _xml_root(xml_text)
    gen = _first(root, "genInfo")
    series_id = _text(gen, "seriesId")
    if not series_id:
        sc = _first(root, "seriesClassInfo")
        series_id = _text(sc, "seriesId")
    fund_info = _first(root, "fundInfo")
    holdings = []
    for sec in root.iter():
        if _local(sec.tag) != "invstOrSec":
            continue
        title = _value(_text(sec, "title"))
        name = _value(_text(sec, "name")) or title or ""
        cat = _text(sec, "assetCat")
        if not cat:
            cond = _child(sec, "assetConditional")
            cat = cond.get("assetCat") if cond is not None else None
        isin = None
        ids = _child(sec, "identifiers")
        if ids is not None:
            isin_el = _child(ids, "isin")
            isin = _value(isin_el.get("value")) if isin_el is not None else None
        pct = _text(sec, "pctVal")
        holdings.append({
            "name": name, "title": title, "cusip": _value(_text(sec, "cusip")), "isin": isin,
            "weight_pct": float(pct) if pct not in (None, "") else 0.0, "asset_cat": cat,
        })
    sub = _first(root, "submissionType")
    net = _text(fund_info, "netAssets")
    return {
        "form": sub.text.strip() if sub is not None and sub.text else None,
        "series_id": series_id,
        "series_name": _text(gen, "seriesName"),
        "cik": _text(gen, "regCik"),
        "as_of": _text(gen, "repPdDate"),
        "net_assets": float(net) if net else None,
        "holdings": holdings,
    }


def parse_feed(atom_text) -> list:
    """Parse an EDGAR company or series Atom feed into ``[{form, accession, filed, href}]``,
    in feed order (newest first)."""
    root = _xml_root(atom_text)
    out = []
    for entry in root:
        if _local(entry.tag) != "entry":
            continue
        content = _child(entry, "content")
        form = _text(content, "filing-type")
        if not form:
            cat = _child(entry, "category")
            form = cat.get("term") if cat is not None else None
        href = _text(content, "filing-href")
        if not href:
            link = _child(entry, "link")
            href = link.get("href") if link is not None else None
        out.append({"form": (form or "").strip(), "accession": _text(content, "accession-number"),
                    "filed": _text(content, "filing-date"), "href": href})
    return out


def latest_nport(entries) -> Optional[dict]:
    """The newest entry with form exactly ``NPORT-P``. An ``NPORT-P/A`` amendment gives no
    holdings set, so it never counts."""
    rows = [e for e in entries or [] if e.get("form") == NPORT_FORM and e.get("accession")]
    if not rows:
        return None
    return max(rows, key=lambda e: (e.get("filed") or "", e.get("accession") or ""))


def feed_url(series_id: str, form: str = "") -> str:
    """The EDGAR Atom feed of one fund series. ``form=""`` is the unfiltered feed (§7.7 step 4)."""
    return _FEED.format(series=series_id, form=form)


def nport_doc_url(entry: dict, cik=None) -> str:
    """The raw XML of one N-PORT filing: ``primary_doc.xml`` in the filing folder. With no
    CIK, the folder comes from the entry's archive link."""
    if cik:
        return edgar.archive_url(cik, entry["accession"], "primary_doc.xml")
    href = str(entry.get("href") or "")
    if "/Archives/edgar/data/" not in href:
        raise ValueError(f"no CIK and no archive link for {entry.get('accession')}")
    return href.rsplit("/", 1)[0] + "/primary_doc.xml"


# --- names and tickers -------------------------------------------------------------

def normalize_name(name) -> str:
    """Lowercase; drop a trailing SEC state tag (``/DE``); drop punctuation; drop the words
    inc, corp, corporation, ltd, co, holdings, plc, sa, nv, ag, adr. A period or an apostrophe
    goes with no space ("N.V." is "nv"). Other punctuation splits words."""
    s = str(name or "").lower().strip()
    s = _STATE_TAG.sub("", s)
    s = _DELETE.sub("", s)
    words = [w for w in _SPLIT.split(s) if w and w not in _STOP_WORDS]
    return " ".join(words)


def _match_key(name) -> str:
    """The key two names match on: the normalized name with no spaces. "Amazon.com" and
    "AMAZON COM" give one key."""
    return normalize_name(name).replace(" ", "")


def load_ticker_map_rows(conn, company_tickers_json, now: str) -> int:
    """Copy SEC ``company_tickers.json`` into ``sec_ticker_map`` (source ``company_tickers``),
    in file order. Takes the parsed dict, or its JSON text or bytes. Returns the row count."""
    data = company_tickers_json
    if isinstance(data, (bytes, str)):
        data = json.loads(data)
    if isinstance(data, dict):
        keys = sorted(data, key=lambda k: int(k) if str(k).isdigit() else 0)
        items = [data[k] for k in keys]
    else:
        items = list(data or [])
    n = 0
    for it in items:
        ticker = str(it.get("ticker") or "").upper().strip()
        cik = it.get("cik_str")
        if not ticker or cik in (None, ""):
            continue
        conn.execute(
            "INSERT INTO sec_ticker_map (ticker, cik, series_id, title, source, fetched_at) "
            "VALUES (?, ?, NULL, ?, 'company_tickers', ?) ON CONFLICT(ticker) DO UPDATE SET "
            "cik=excluded.cik, title=excluded.title, source=excluded.source, "
            "fetched_at=excluded.fetched_at",
            (ticker, str(int(cik)).zfill(10), it.get("title"), now))
        n += 1
    return n


def _name_index(conn) -> dict:
    """match key -> (ticker, cik), from ``sec_ticker_map`` in insertion order. One CIK with
    several tickers keeps the first (the SEC file lists the main ticker first). A key that
    two CIKs share is ambiguous and maps to None."""
    index: dict = {}
    rows = conn.execute("SELECT ticker, cik, title FROM sec_ticker_map "
                        "WHERE title IS NOT NULL ORDER BY rowid").fetchall()
    for ticker, cik, title in rows:
        key = _match_key(title)
        if not key:
            continue
        if key not in index:
            index[key] = (ticker, cik)
        elif index[key] is not None and index[key][1] != cik:
            index[key] = None
    return index


def _cik_for(conn, ticker) -> Optional[str]:
    if not ticker:
        return None
    row = conn.execute("SELECT cik FROM sec_ticker_map WHERE ticker=?",
                       (str(ticker).upper(),)).fetchone()
    return row[0] if row else None


def map_tickers(conn, holdings) -> list:
    """Return copies of ``holdings`` with ``holding_ticker`` and ``cik``. A row that has a
    ticker (yfinance) gets its CIK from the SEC map. A common-equity N-PORT row maps by
    normalized name against ``sec_ticker_map.title``. An unmatched row keeps None."""
    index = None
    out = []
    for h in holdings or []:
        row = dict(h)
        ticker = row.get("holding_ticker") or row.get("ticker")
        if ticker:
            row["holding_ticker"] = str(ticker).upper()
            row["cik"] = _cik_for(conn, ticker)
            out.append(row)
            continue
        row["holding_ticker"], row["cik"] = None, None
        cat = row.get("asset_cat")
        if cat is None or cat in MAPPED_ASSET_CATS:
            if index is None:
                index = _name_index(conn)
            for candidate in (row.get("name") or row.get("holding_name"), row.get("title")):
                hit = index.get(_match_key(candidate)) if candidate else None
                if hit:
                    row["holding_ticker"], row["cik"] = hit
                    break
        out.append(row)
    return out


# --- storage -----------------------------------------------------------------------

def _holding_names(rows) -> list:
    """One ``holding_name`` per row. A name that occurs twice in one set (SOXX lists two
    "BlackRock Funds III" cash vehicles) takes its CUSIP or title, so the primary key keeps
    both rows."""
    names = [str(r.get("holding_name") or r.get("name") or r.get("title") or "").strip()
             for r in rows]
    counts: dict = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    out, seen = [], {}
    for n, r in zip(names, rows):
        if counts[n] > 1:
            tag = r.get("cusip") or r.get("title")
            seen[n] = seen.get(n, 0) + 1
            n = f"{n} ({tag})" if tag else f"{n} #{seen[n]}"
        out.append(n or "unnamed")
    return out


def store_holdings(conn, fund: str, as_of: str, source: str, rows) -> int:
    """Write one holdings set into ``etf_holdings`` (INSERT OR REPLACE by the primary key).
    Returns the row count."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    rows = list(rows or [])
    fund = str(fund).upper()
    for name, r in zip(_holding_names(rows), rows):
        ticker = r.get("holding_ticker") or r.get("ticker")
        conn.execute(
            "INSERT OR REPLACE INTO etf_holdings (fund, as_of, source, holding_name, "
            "holding_ticker, cusip, weight_pct) VALUES (?,?,?,?,?,?,?)",
            (fund, as_of, source, name, str(ticker).upper() if ticker else None,
             r.get("cusip"), float(r.get("weight_pct") or 0.0)))
    return len(rows)


def latest_holdings(conn, fund: str) -> Optional[dict]:
    """The newest holdings set of a fund: the source with the newer ``as_of`` (N-PORT on a
    tie). Returns ``{fund, source, as_of, rows}`` or None."""
    fund = str(fund).upper()
    sets = conn.execute("SELECT source, MAX(as_of) FROM etf_holdings WHERE fund=? "
                        "GROUP BY source", (fund,)).fetchall()
    if not sets:
        return None
    source, as_of = max(((s, a) for s, a in sets), key=lambda sa: (sa[1], sa[0] == "nport"))
    rows = conn.execute(
        "SELECT holding_name, holding_ticker, cusip, weight_pct FROM etf_holdings "
        "WHERE fund=? AND as_of=? AND source=? ORDER BY weight_pct DESC, holding_name",
        (fund, as_of, source)).fetchall()
    return {"fund": fund, "source": source, "as_of": as_of,
            "rows": [{"holding_name": r[0], "holding_ticker": r[1], "cusip": r[2],
                      "weight_pct": r[3], "source": source, "as_of": as_of} for r in rows]}


# --- refresh -----------------------------------------------------------------------

def _state_key(fund: str) -> str:
    return f"nport:{str(fund).upper()}"


def refresh_nport(conn, client, *, fund: str, cik=None, series_id: str, now: str) -> dict:
    """Store the latest NPORT-P of one fund series. ``client`` has ``get_text(url)`` (the
    EDGAR client). It reads the unfiltered series feed and filters by form in Python. If that
    feed holds no NPORT-P, it reads the form-filtered feed. It does not fetch the document of
    a stored accession again. A document of another series stores nothing. Returns
    ``{fund, status, accession, filed, as_of, rows}``; status is ``stored``, ``unchanged``,
    ``no_nport`` or ``series_mismatch``."""
    fund = str(fund).upper()
    out = {"fund": fund, "series_id": series_id, "status": "no_nport", "accession": None,
           "filed": None, "as_of": None, "rows": 0, "feeds": []}
    entry = None
    for form in ("", NPORT_FORM):
        url = feed_url(series_id, form)
        out["feeds"].append(url)
        try:
            entry = latest_nport(parse_feed(client.get_text(url)))
        except Exception as e:  # noqa: BLE001 - the second feed is the fallback
            if form == NPORT_FORM:
                raise
            out["feed_error"] = f"{type(e).__name__}: {e}"
            entry = None
        if entry:
            break
    if not entry:
        return out
    out.update(accession=entry["accession"], filed=entry["filed"])
    prev = store.loads(store.get_state(conn, _state_key(fund)), {}) or {}
    if prev.get("accession") == entry["accession"] and prev.get("as_of"):
        n = conn.execute("SELECT COUNT(*) FROM etf_holdings WHERE fund=? AND as_of=? "
                         "AND source='nport'", (fund, prev["as_of"])).fetchone()[0]
        if n:
            out.update(status="unchanged", as_of=prev["as_of"], rows=int(n))
            return out
    doc_url = nport_doc_url(entry, cik)
    out["doc_url"] = doc_url
    parsed = parse_nport(client.get_text(doc_url))
    if parsed.get("series_id") != series_id:
        out.update(status="series_mismatch", as_of=parsed.get("as_of"))
        out["got_series_id"] = parsed.get("series_id")
        return out
    rows = map_tickers(conn, parsed["holdings"])
    n = store_holdings(conn, fund, parsed["as_of"], "nport", rows)
    store.set_state(conn, _state_key(fund), store.canonical_json(
        {"accession": entry["accession"], "filed": entry["filed"], "as_of": parsed["as_of"],
         "stored_at": store.iso(store.parse_ts(now) or store.utc_now())}))
    out.update(status="stored", as_of=parsed["as_of"], rows=n)
    return out


def _default_top(fund: str):
    import yfinance as yf
    return yf.Ticker(fund).funds_data


def _as_of_day(now) -> str:
    """The ET date of ``now`` (a stored time string or a datetime; naive counts as UTC)."""
    dt = store.parse_ts(now)
    if dt is None:
        raise ValueError(f"bad now: {now!r}")
    return dt.astimezone(market.ET).date().isoformat()


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def refresh_top(conn, fund: str, *, now, top_fn: Optional[Callable] = None) -> dict:
    """Store the yfinance top holdings of one fund (source ``yfinance_top``, ``as_of`` = the
    ET date of ``now``, ``weight_pct`` in percent). Store the expense ratio in
    ``bridge_state`` as ``expense_ratio:<FUND>``. ``top_fn(fund)`` returns an object with
    ``top_holdings`` and ``fund_operations`` frames (yfinance ``funds_data``). Returns
    ``{rows, as_of, expense_ratio}``; ``expense_ratio`` is in percent (0.33 means 0.33%)."""
    fund = str(fund).upper()
    data = (top_fn or _default_top)(fund)
    as_of = _as_of_day(now)
    top = getattr(data, "top_holdings", None)
    rows = []
    if top is not None and len(top):
        cols = list(top.columns)
        name_col = "Name" if "Name" in cols else cols[0]
        pct_col = "Holding Percent" if "Holding Percent" in cols else cols[-1]
        for symbol, rec in top.iterrows():
            frac = _num(rec[pct_col])
            if frac is None or not symbol:
                continue
            rows.append({"holding_name": str(rec[name_col] or symbol),
                         "holding_ticker": str(symbol).upper(),
                         "weight_pct": round(frac * 100.0, 6)})
    n = store_holdings(conn, fund, as_of, "yfinance_top", rows) if rows else 0
    expense = None
    ops = getattr(data, "fund_operations", None)
    if ops is not None and len(ops) and "Annual Report Expense Ratio" in ops.index:
        col = fund if fund in ops.columns else ops.columns[0]
        frac = _num(ops.loc["Annual Report Expense Ratio", col])
        if frac is not None:
            expense = round(frac * 100.0, 6)
            store.set_state(conn, f"expense_ratio:{fund}", store.canonical_json(
                {"pct": expense, "as_of": as_of, "source": "yfinance"}))
    return {"fund": fund, "rows": n, "as_of": as_of, "expense_ratio": expense}


def get_expense_ratio(conn, fund: str) -> Optional[dict]:
    """The stored expense ratio of a fund: ``{pct, as_of, source}`` or None."""
    return store.loads(store.get_state(conn, f"expense_ratio:{str(fund).upper()}"), None)


# --- look-through ------------------------------------------------------------------

def lookthrough_companies(conn, fund: str, *, threshold_pct: float = LOOKTHROUGH_PCT) -> list:
    """The look-through companies of a fund: each holding of the latest set at or above
    ``threshold_pct`` with a ticker and a CIK, largest first. A fund of kind ``cef`` or
    ``crypto_etf`` returns []. Returns ``[{ticker, cik, weight_pct, name}]``."""
    fund = str(fund).upper()
    company = store.get_company(conn, fund)
    if company and company.get("kind") in NO_LOOKTHROUGH_KINDS:
        return []
    latest = latest_holdings(conn, fund)
    if not latest:
        return []
    out, seen = [], set()
    for r in latest["rows"]:
        ticker = r["holding_ticker"]
        if not ticker or ticker == fund or ticker in seen or float(r["weight_pct"]) < threshold_pct:
            continue
        cik = _cik_for(conn, ticker)
        if not cik:
            continue
        seen.add(ticker)
        out.append({"ticker": ticker, "cik": cik, "weight_pct": float(r["weight_pct"]),
                    "name": r["holding_name"], "source": latest["source"],
                    "as_of": latest["as_of"]})
    out.sort(key=lambda x: (-x["weight_pct"], x["ticker"]))
    return out


def lookthrough_reason(fund: str, weight_pct: float) -> str:
    """``look-through: SOXX 8.5%`` (one decimal)."""
    return f"look-through: {str(fund).upper()} {float(weight_pct):.1f}%"


def mark_lookthrough(conn, fund: str, rows, *, now: str) -> list:
    """Make each look-through row a company with ``status='watch'`` and the look-through
    reason. A ``holding`` stays ``holding``. Returns ``[{ticker, cik, weight_pct, status,
    changed}]``."""
    out = []
    for r in rows or []:
        ticker = str(r["ticker"]).upper()
        reason = lookthrough_reason(fund, r["weight_pct"])
        existing = store.get_company(conn, ticker)
        if existing is None:
            store.upsert_company(conn, ticker=ticker, kind="company", status="watch",
                                 status_reason=reason, now=now, name=r.get("name"),
                                 cik=r.get("cik"))
            out.append({"ticker": ticker, "cik": r.get("cik"), "weight_pct": r["weight_pct"],
                        "status": "watch", "changed": True})
            continue
        store.upsert_company(conn, ticker=ticker, kind=existing["kind"], status=existing["status"],
                             now=now, cik=r.get("cik"))
        if existing["status"] == "holding":
            out.append({"ticker": ticker, "cik": r.get("cik"), "weight_pct": r["weight_pct"],
                        "status": "holding", "changed": False})
            continue
        changed = (existing["status"], existing["status_reason"]) != ("watch", reason)
        if changed:
            store.set_company_status(conn, ticker, "watch", reason, now)
        out.append({"ticker": ticker, "cik": r.get("cik"), "weight_pct": r["weight_pct"],
                    "status": "watch", "changed": changed})
    return out


def _metric(m: dict, key: str) -> Optional[float]:
    v = m.get(key) if isinstance(m, dict) else None
    if isinstance(v, bool):
        return None
    return _num(v)


def weighted_lookthrough(holdings, metrics_by_ticker, *, expense_ratio=None) -> dict:
    """The ETF look-through row of §8.4 (pure). Over the holdings that have a metrics row:
    the weight-averaged ``revenue_growth_yoy`` and ``fcf_yield``, and the weighted harmonic
    mean of ``ev_sales`` (positive values only). Each average uses the holdings that have
    that metric. ``coverage_pct`` is the fund weight of the holdings with metrics;
    ``low_coverage`` is true under 50%."""
    metrics_by_ticker = {str(k).upper(): v for k, v in (metrics_by_ticker or {}).items()}
    listed, covered = [], []
    sources, dates = set(), set()
    for h in holdings or []:
        ticker = h.get("holding_ticker") or h.get("ticker")
        ticker = str(ticker).upper() if ticker else None
        w = _num(h.get("weight_pct")) or 0.0
        m = metrics_by_ticker.get(ticker) if ticker else None
        if isinstance(m, str):
            m = store.loads(m, None)
        has = isinstance(m, dict) and bool(m)
        source, as_of = h.get("source"), h.get("as_of")
        sources.add(source)
        dates.add(as_of)
        listed.append({"name": h.get("holding_name") or h.get("name"), "ticker": ticker,
                       "weight_pct": w, "source": source, "as_of": as_of, "has_metrics": has})
        if has:
            covered.append((w, m))

    def wavg(key):
        pairs = [(w, _metric(m, key)) for w, m in covered]
        pairs = [(w, v) for w, v in pairs if v is not None and w > 0]
        tw = sum(w for w, _ in pairs)
        return (sum(w * v for w, v in pairs) / tw if tw else None), tw

    growth, growth_w = wavg("revenue_growth_yoy")
    fcf, fcf_w = wavg("fcf_yield")
    ev_pairs = [(w, _metric(m, "ev_sales")) for w, m in covered]
    ev_pairs = [(w, v) for w, v in ev_pairs if v is not None and v > 0 and w > 0]
    ev_w = sum(w for w, _ in ev_pairs)
    ev_sales = ev_w / sum(w / v for w, v in ev_pairs) if ev_pairs else None
    coverage = min(100.0, sum(w for w, _ in covered))
    src = sorted(s for s in sources if s)
    day = sorted(d for d in dates if d)
    return {
        "source": src[0] if len(src) == 1 else (src or None),
        "as_of": day[0] if len(day) == 1 else (day or None),
        "holdings": listed,
        "revenue_growth_yoy": growth,
        "fcf_yield": fcf,
        "ev_sales": ev_sales,
        "metric_weight_pct": {"revenue_growth_yoy": growth_w, "fcf_yield": fcf_w,
                              "ev_sales": ev_w},
        "coverage_pct": coverage,
        "low_coverage": coverage < LOW_COVERAGE_PCT,
        "expense_ratio": expense_ratio,
    }
