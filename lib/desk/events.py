"""Events: the filing tier rule (plan §7.3) and the monitor's event sweeps.

``filing_tier`` and ``event_type`` come first. They are pure: no I/O, no clock. The
sweep functions follow them.
"""

from __future__ import annotations

import re

REDECIDE_FORMS = frozenset(("10-K", "10-Q", "20-F", "40-F", "S-1", "F-1", "424B4"))
RECORD_FORMS = frozenset(("DEF 14A", "NPORT-P", "N-CSR", "N-CSRS"))
REDECIDE_8K_ITEMS = frozenset(("1.01", "1.03", "2.01", "2.02", "2.05", "2.06", "3.01", "4.02", "5.01"))
EX99_8K_ITEMS = frozenset(("7.01", "8.01"))       # redecide only with an EX-99 exhibit
EARNINGS_ITEM = "2.02"

_ITEM_CODE = re.compile(r"\d{1,2}\.\d{2}")


def _form(form) -> str:
    return " ".join(str(form or "").upper().split())


def _items(items) -> set:
    """``"2.02,9.01"`` or ``["2.02", "9.01"]`` -> ``{"2.02", "9.01"}``."""
    if items is None:
        return set()
    if isinstance(items, str):
        return set(_ITEM_CODE.findall(items))
    out = set()
    for it in items:
        out.update(_ITEM_CODE.findall(str(it)))
    return out


def _has_ex99(exhibits) -> bool:
    """True when an exhibit type starts with ``EX-99``. An exhibit is a type string or an
    index row dict with a ``type`` key (``sections.parse_index``)."""
    for ex in exhibits or ():
        typ = ex.get("type") if isinstance(ex, dict) else ex
        if str(typ or "").strip().upper().startswith("EX-99"):
            return True
    return False


def _base_tier(form: str, items, exhibits) -> str:
    if form in REDECIDE_FORMS:
        return "redecide"
    if form == "8-K":
        codes = _items(items)
        if codes & REDECIDE_8K_ITEMS:
            return "redecide"
        if codes & EX99_8K_ITEMS and _has_ex99(exhibits):
            return "redecide"
        return "record"
    if form == "6-K":
        return "redecide" if _has_ex99(exhibits) else "record"
    if form in RECORD_FORMS:
        return "record"
    return "ignore"


def filing_tier(form, items=None, exhibits=None) -> str:
    """``redecide``, ``record`` or ``ignore`` for one filing (§7.3).

    The ``/A`` of a redecide or record form is ``record``. The ``/A`` of an ignore form
    stays ``ignore`` (``SC 13G/A``, ``SCHEDULE 13G/A``, ``4/A``: §18.9 G18)."""
    f = _form(form)
    if f.endswith("/A"):
        return "ignore" if _base_tier(f[:-2].strip(), items, exhibits) == "ignore" else "record"
    return _base_tier(f, items, exhibits)


def event_type(form, items=None) -> str:
    """``earnings`` for an ``8-K`` with item 2.02, else ``filing``. A 6-K has no item codes,
    so a 6-K with an EX-99 exhibit makes a ``filing`` event (§18.9 G19)."""
    return "earnings" if _form(form) == "8-K" and EARNINGS_ITEM in _items(items) else "filing"


# --- load_company (§7.7) and the EDGAR sweep (§9.1 step 4) ----------------------------

import json  # noqa: E402
import logging  # noqa: E402

from lib.desk import edgar, etf, sections, store, transcripts, valuation, xbrl  # noqa: E402

log = logging.getLogger("quiver.desk.events")

ANNUAL_FORMS = ("10-K", "20-F", "40-F")
IPO_FORMS = ("424B4", "S-1", "F-1")
LOOKTHROUGH_EVENT_FORMS = ("10-K", "10-Q", "20-F", "40-F")
XBRL_PENDING_KEY = "xbrl_pending"
LOOKTHROUGH_PCT = 5.0
# companyfacts carries the facts of periodic reports only; an earnings 8-K (isXBRL=1 for its
# cover page) never appears there, so waiting for it would end in a false ingest_error.
XBRL_FORMS = ("10-K", "10-Q", "20-F", "40-F")


class Seams:
    """The network seams of ingestion (plan §18.2). Each default is the production call."""

    def __init__(self, *, client=None, price_fn=None, market_cap_fn=None, technicals_fn=None,
                 next_earnings_fn=None, top_fn=None, av_key=None, av_opener=None, news_fetch=None):
        self.client = client
        self.price_fn = price_fn or valuation.yf_price
        self.market_cap_fn = market_cap_fn or valuation.yf_market_cap
        self.technicals_fn = technicals_fn or valuation.trend_technicals
        self.next_earnings_fn = next_earnings_fn or valuation.yf_next_earnings
        self.top_fn = top_fn
        self.av_key = av_key
        self.av_opener = av_opener
        self.news_fetch = news_fetch


def _pick(filings: list, forms, *, after: str = "") -> dict | None:
    rows = [f for f in filings if f["form"] in forms and f["filed_date"] > after]
    return max(rows, key=lambda f: (f["filed_date"], f["accession"])) if rows else None


def _latest_earnings(filings: list) -> dict | None:
    rows = [f for f in filings if f["form"] == "8-K" and EARNINGS_ITEM in _items(f.get("items"))]
    return max(rows, key=lambda f: (f["filed_date"], f["accession"])) if rows else None


def _ingest(led, client, *, ticker, cik, filing) -> dict:
    with led.connect() as conn:
        done = conn.execute("SELECT ingested_at FROM filings WHERE accession=?",
                            (filing["accession"],)).fetchone()
    if done and done[0]:
        return {"accession": filing["accession"], "ok": True, "cached": True}
    with led.connect() as conn:
        return sections.ingest_filing(conn, client, ticker=ticker, cik=cik, accession=filing["accession"],
                                      form=filing["form"], primary_doc=filing.get("primary_doc"),
                                      now=store.now_iso())


def compute_company_metrics(led, ticker: str, *, cik, settings: dict, seams: Seams) -> dict | None:
    """The §8.1 metrics row of a company (run after each XBRL ingest and at the day's first
    monitor run)."""
    with led.connect() as conn:
        try:
            return valuation.compute_metrics(conn, ticker, cik=cik, settings=settings, now=store.now_iso(),
                                             price_fn=seams.price_fn, market_cap_fn=seams.market_cap_fn,
                                             technicals_fn=seams.technicals_fn,
                                             next_earnings_fn=seams.next_earnings_fn)
        except valuation.ValuationError as e:
            log.warning("metrics %s: %s", ticker, e)
            return None


def lookthrough_union(conn, fund: str, threshold_pct: float = LOOKTHROUGH_PCT) -> list:
    """The look-through companies of ``fund`` from BOTH sources (the yfinance top 10 and
    the latest N-PORT), each at its larger weight. One source alone misses names: for SOXX
    the yfinance top 10 gives 5 of the 8 N-PORT names at or above 5%."""
    fund = str(fund).upper()
    comp = store.get_company(conn, fund)
    if comp and comp.get("kind") in ("cef", "crypto_etf", "cash"):
        return []
    best = {}
    for src, as_of in conn.execute("SELECT source, MAX(as_of) FROM etf_holdings WHERE fund=? GROUP BY source",
                                   (fund,)).fetchall():
        for name, tk, w in conn.execute("SELECT holding_name, holding_ticker, weight_pct FROM etf_holdings "
                                        "WHERE fund=? AND source=? AND as_of=?", (fund, src, as_of)).fetchall():
            if not tk or tk == fund or float(w) < threshold_pct:
                continue
            row = conn.execute("SELECT cik FROM sec_ticker_map WHERE ticker=?", (tk,)).fetchone()
            if not row:
                continue
            cur = best.get(tk)
            if cur is None or float(w) > cur["weight_pct"]:
                best[tk] = {"ticker": tk, "cik": row[0], "weight_pct": float(w), "name": name, "source": src}
    return sorted(best.values(), key=lambda r: (-r["weight_pct"], r["ticker"]))


def held_etf_weights(conn, ticker: str) -> list:
    """``[{"etf": SOXX, "weight": 8.54}]`` for each book ETF whose latest holdings (either
    source) hold ``ticker``."""
    ticker = str(ticker).upper()
    out = []
    goal = conn.execute("SELECT id FROM strategy_goal WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    if not goal:
        return out
    funds = [r[0] for r in conn.execute(
        "SELECT t.ticker FROM target_portfolio t JOIN companies c ON c.ticker=t.ticker "
        "WHERE t.goal_id=? AND t.status='active' AND c.kind='etf'", (goal[0],)).fetchall()]
    for fund in funds:
        for r in lookthrough_union(conn, fund, threshold_pct=0.0):
            if r["ticker"] == ticker:
                out.append({"etf": fund, "weight": round(r["weight_pct"], 2)})
    return out


def load_company(led, ticker: str, *, settings: dict, seams: Seams, status: str = "candidate",
                 status_reason: str | None = None, sleeve: str | None = None, depth: int = 0) -> dict:
    """Ingest a company or fund (§7.7). Every existing filing is stored as ``baseline``, so
    loading creates no event. Returns a summary; an unknown ticker raises
    ``edgar.UnknownTicker``."""
    ticker = str(ticker).upper().strip()
    client = seams.client
    now = store.now_iso()
    with led.connect() as conn:
        ent = edgar.resolve(conn, ticker)
        existing = store.get_company(conn, ticker)
        store.upsert_company(conn, ticker=ticker, kind=ent["kind"], status=status, now=now,
                             name=ent.get("title"), cik=ent.get("cik"), series_id=ent.get("series_id"),
                             sleeve=sleeve, status_reason=status_reason)
    summary = {"ticker": ticker, "kind": ent["kind"], "cik": ent.get("cik"), "status":
               (existing or {}).get("status", status), "filings": 0, "ingested": [], "documents": 0,
               "metrics": False, "transcript": None, "lookthrough": [], "lookthrough_errors": []}
    kind, cik = ent["kind"], ent.get("cik")
    if kind in ("company", "cef", "crypto_etf") and cik:
        subs = client.get_json(edgar.submissions_url(cik))
        recent = edgar.recent_filings(subs)
        with led.connect() as conn:
            for f in recent:
                if edgar.insert_filing(conn, filing=f, cik=cik, ticker=ticker, tier="baseline", now=now):
                    summary["filings"] += 1
        annual = _pick(recent, ANNUAL_FORMS) or _pick(recent, IPO_FORMS)
        chosen = [annual] if annual else []
        q = _pick(recent, ("10-Q",), after=annual["filed_date"] if annual else "")
        if q:
            chosen.append(q)
        er = _latest_earnings(recent)
        if er:
            chosen.append(er)
        for f in chosen:
            res = _ingest(led, client, ticker=ticker, cik=cik, filing=f)
            summary["ingested"].append({"accession": f["accession"], "form": f["form"], "ok": res.get("ok"),
                                        "error": res.get("error")})
        try:
            cf = client.get_json(edgar.companyfacts_url(cik))
            with led.connect() as conn:
                xbrl.upsert_companyfacts(conn, cf)
        except edgar.EdgarError as e:
            log.warning("companyfacts %s: %s", ticker, e)
        if kind == "company":
            summary["metrics"] = compute_company_metrics(led, ticker, cik=cik, settings=settings,
                                                         seams=seams) is not None
            if er and seams.av_key is not None:
                with led.connect() as conn:
                    for quarter in transcripts.quarter_for(conn, cik=cik, report_date=er.get("report_date")
                                                           or er["filed_date"]):
                        res = transcripts.fetch_transcript(conn, ticker, quarter, api_key=seams.av_key,
                                                           now=now, opener=seams.av_opener)
                        summary["transcript"] = {"quarter": quarter, **{k: res.get(k) for k in ("state", "document_id")}}
                        if res.get("state") in ("stored", "no_budget", "no_key"):
                            break
    elif kind == "etf":
        try:
            with led.connect() as conn:
                summary["top"] = etf.refresh_top(conn, ticker, now=now, top_fn=seams.top_fn).get("rows")
        except Exception as e:  # noqa: BLE001 — yfinance is best-effort; N-PORT still runs
            summary["top_error"] = f"{type(e).__name__}: {e}"
        if ent.get("series_id"):
            try:
                with led.connect() as conn:
                    summary["nport"] = etf.refresh_nport(conn, client, fund=ticker, cik=cik,
                                                         series_id=ent["series_id"], now=now).get("status")
            except edgar.EdgarError as e:
                summary["nport_error"] = str(e)
        with led.connect() as conn:
            lt = lookthrough_union(conn, ticker)
            etf.mark_lookthrough(conn, ticker, lt, now=now)
        summary["lookthrough"] = [r["ticker"] for r in lt]
        if depth == 0:
            for r in lt:
                try:
                    load_company(led, r["ticker"], settings=settings, seams=seams, status="watch",
                                 status_reason=etf.lookthrough_reason(ticker, r["weight_pct"]), depth=1)
                except Exception as e:  # noqa: BLE001 — one bad look-through name never stops the fund
                    log.warning("look-through %s/%s: %s", ticker, r["ticker"], e)
                    summary["lookthrough_errors"].append(f"{r['ticker']}: {type(e).__name__}: {e}")
    with led.connect() as conn:
        summary["documents"] = conn.execute("SELECT COUNT(*) FROM documents WHERE ticker=?", (ticker,)).fetchone()[0]
        summary["status"] = (store.get_company(conn, ticker) or {}).get("status")
    return summary


def _is_lookthrough(conn, company: dict) -> bool:
    if str(company.get("status_reason") or "").startswith("look-through:"):
        return True
    return bool(held_etf_weights(conn, company["ticker"])) and company.get("status") != "holding"


def sweep(led, *, settings: dict, seams: Seams, statuses=("holding", "watch", "candidate")) -> dict:
    """One EDGAR sweep (§9.1 step 4): insert each new accession with its tier, ingest what
    the tier asks, and create the filing, earnings and look-through events."""
    client = seams.client
    now = store.now_iso()
    out = {"companies": 0, "filings": 0, "events": [], "watch_filings": [], "errors": [], "nport": []}
    with led.connect() as conn:
        marks = ",".join("?" for _ in statuses)
        companies = [dict(r) for r in conn.execute(
            f"SELECT * FROM companies WHERE status IN ({marks}) AND cik IS NOT NULL ORDER BY ticker",
            statuses).fetchall()]
    for comp in companies:
        ticker, cik, kind = comp["ticker"], comp["cik"], comp["kind"]
        if kind == "etf":
            if comp["status"] == "holding" and comp.get("series_id"):
                try:
                    with led.connect() as conn:
                        res = etf.refresh_nport(conn, client, fund=ticker, cik=cik, series_id=comp["series_id"], now=now)
                    if res.get("status") == "stored":
                        out["nport"].append(ticker)
                except edgar.EdgarBlocked:
                    raise
                except edgar.EdgarError as e:
                    out["errors"].append(f"{ticker}: {e}")
            continue
        if kind == "cash":
            continue
        out["companies"] += 1
        try:
            subs = client.get_json(edgar.submissions_url(cik))
        except edgar.EdgarBlocked:
            raise
        except edgar.EdgarError as e:
            out["errors"].append(f"{ticker}: {e}")
            continue
        with led.connect() as conn:
            known = {r[0] for r in conn.execute("SELECT accession FROM filings WHERE ticker=?", (ticker,))}
            lookthrough = _is_lookthrough(conn, comp)
        if not known:
            # First sight: a company with no filing rows (its load failed, or it came in by
            # another path). Record its filings as seen and make no event, so years of old
            # filings never become events. load_company still ingests the documents later.
            with led.connect() as conn:
                for f in edgar.recent_filings(subs):
                    edgar.insert_filing(conn, filing=f, cik=cik, ticker=ticker, tier="baseline", now=now)
            out.setdefault("baselined", []).append(ticker)
            continue
        for f in reversed(edgar.recent_filings(subs)):  # oldest first
            if f["accession"] in known:
                continue
            exhibits = None
            base = f["form"].upper().replace("/A", "").strip()
            if base in ("8-K", "6-K") and (base == "6-K" or _items(f.get("items")) & EX99_8K_ITEMS):
                try:
                    exhibits = edgar.filing_exhibits(client, cik, f["accession"])
                except edgar.EdgarError as e:
                    out["errors"].append(f"{ticker} {f['accession']}: {e}")
                    exhibits = []
            tier = filing_tier(f["form"], f.get("items"), exhibits)
            with led.connect() as conn:
                edgar.insert_filing(conn, filing=f, cik=cik, ticker=ticker, tier=tier, now=now)
            out["filings"] += 1
            if tier == "ignore":
                continue
            res = _ingest(led, client, ticker=ticker, cik=cik, filing=f)
            if f.get("is_xbrl") and tier == "redecide" and base in XBRL_FORMS:
                _xbrl_after(led, client, cik=cik, accession=f["accession"], ticker=ticker,
                            settings=settings, seams=seams, kind=kind)
            if tier != "redecide":
                continue
            etype = event_type(f["form"], f.get("items"))
            detail = {"accession": f["accession"], "form": f["form"], "items": f.get("items"),
                      "filed_date": f["filed_date"], "report_date": f.get("report_date"), "cik": cik,
                      "document_keys": res.get("keys", []), "tier": tier}
            if comp["status"] == "holding":
                with led.connect() as conn:
                    eid = store.create_event(conn, dedup_key=f"filing:{f['accession']}", ticker=ticker,
                                             type=etype, detail=detail, now=now, created_by="monitor")
                if eid:
                    out["events"].append(eid)
            elif lookthrough:
                if base in LOOKTHROUGH_EVENT_FORMS or etype == "earnings":
                    with led.connect() as conn:
                        detail["etfs"] = held_etf_weights(conn, ticker)
                        eid = store.create_event(conn, dedup_key=f"lookthrough:{f['accession']}", ticker=ticker,
                                                 type="lookthrough", detail=detail, now=now, created_by="monitor")
                    if eid:
                        out["events"].append(eid)
            else:
                out["watch_filings"].append(f"{ticker} {f['form']}")
    _xbrl_retry(led, client, settings=settings, seams=seams)
    return out


def _xbrl_after(led, client, *, cik, accession, ticker, settings, seams, kind) -> str:
    with led.connect() as conn:
        state = xbrl.ingest_after_filing(conn, client, cik=cik, accession=accession, now=store.now_iso())
        pending = set(json.loads(store.get_state(conn, XBRL_PENDING_KEY, "[]")))
        if state == "waiting":
            pending.add(f"{ticker}|{cik}|{accession}")
        else:
            pending.discard(f"{ticker}|{cik}|{accession}")
        store.set_state(conn, XBRL_PENDING_KEY, json.dumps(sorted(pending)))
    if state == "ok" and kind == "company":
        compute_company_metrics(led, ticker, cik=cik, settings=settings, seams=seams)
    return state


def _xbrl_retry(led, client, *, settings, seams) -> None:
    with led.connect() as conn:
        pending = json.loads(store.get_state(conn, XBRL_PENDING_KEY, "[]"))
    for item in pending:
        ticker, cik, accession = item.split("|")
        with led.connect() as conn:
            comp = store.get_company(conn, ticker) or {}
        _xbrl_after(led, client, cik=cik, accession=accession, ticker=ticker, settings=settings, seams=seams,
                    kind=comp.get("kind", "company"))


def queue_analysis(led, *, now: str, limit: int = 6) -> list:
    """Move up to ``limit`` groups of ``new`` events (oldest first) to ``queued`` and insert
    one ``analyze_event`` job for each group (§9.1 step 9). The new ``news`` events of one
    ticker are one group: three outlets with the same story get one analysis. The job's
    ``event_id`` is the oldest event of the group and ``also`` lists the others. Every other
    event is a group of one. A ``drift`` event is never queued."""
    with led.transaction() as conn:
        rows = conn.execute("SELECT id, ticker, type FROM events WHERE status='new' AND type != 'drift' "
                            "ORDER BY detected_at, id").fetchall()
        groups, news_group = [], {}
        for eid, ticker, typ in rows:
            if typ == "news" and ticker in news_group:
                news_group[ticker].append(eid)
                continue
            groups.append([eid])
            if typ == "news":
                news_group[ticker] = groups[-1]
        jobs = []
        owner = conn.execute("SELECT chat_id FROM chat_messages WHERE role='owner' ORDER BY id DESC LIMIT 1").fetchone()
        for group in groups[:limit]:
            moved = [eid for eid in group if conn.execute(
                "UPDATE events SET status='queued' WHERE id=? AND status='new'", (eid,)).rowcount == 1]
            if not moved:
                continue
            payload = {"event_id": moved[0]}
            if len(moved) > 1:
                payload["also"] = moved[1:]
            jobs.append(store.enqueue_job(conn, kind="analyze_event", payload=payload,
                                          requested_by="monitor", now=now))
            text = describe_event(store.get_event(conn, moved[0]))
            if len(moved) > 1:
                n = len(moved) - 1
                text += f" {n} more headline{'s' if n > 1 else ''} on {store.get_event(conn, moved[0])['ticker']} go with it."
            # A desk line (not sent): the desk's next turn sees it in <desk_updates>.
            store.insert_desk_message(conn, chat_id=owner[0] if owner else "", source="monitor",
                                      text=text + " Analysis queued.", now=now)
        return jobs


def describe_event(ev: dict) -> str:
    """One plain sentence for an event: 'ANET filed an 8-K, item 2.02 (results).'"""
    d = store.loads(ev.get("detail_json"), {}) or {}
    t = ev["ticker"]
    if ev["type"] in ("filing", "earnings", "lookthrough"):
        form = d.get("form") or "filing"
        art = "an" if str(form)[:1].upper() in "AEFHILMNORSX8" else "a"
        items = f", item {d['items']}" if d.get("items") and str(form).startswith(("8-K", "6-K")) else ""
        tail = " (results)" if ev["type"] == "earnings" else ""
        who = f"{t}, a look-through name," if ev["type"] == "lookthrough" else t
        return f"{who} filed {art} {form}{items}{tail}."
    if ev["type"] == "price_level":
        return f"{t} crossed its {str(d.get('level', 'price')).replace('_', ' ')} level at ${float(d.get('price') or 0):,.2f}."
    if ev["type"] == "news":
        return f"{t} has material news: {d.get('title') or 'a headline'}."
    return f"{t}: a {ev['type']} event."
