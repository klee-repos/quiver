"""The EDGAR client, the SEC ticker map, and ``load_company`` (plan §7.1, §7.2, §7.7).

Each case is a ``case_*`` function in ``CASES``. Run: .venv/bin/python tests/test_desk_ingest.py
"""

from __future__ import annotations

import gzip
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import FakeClock, FakeOpener, Results, fx, fx_bytes, temp_root  # noqa: E402

from lib.desk import edgar  # noqa: E402

r = Results("desk-ingest")
UA = "Quiver test@example.com"
ANET = "0001596532"
# The value SEC sent for company_tickers.json (docs/desk_research/research_data_sources.md:42).
CO_LAST_MODIFIED = "Mon, 26 Oct 2026 21:11:03 GMT"


def new_client(op, fc=None, **kw):
    fc = fc or FakeClock()
    return edgar.EdgarClient(UA, opener=op, sleep=fc.sleep, clock=fc, **kw), fc


def map_opener(co=None, mf=None, co_status=200) -> FakeOpener:
    op = FakeOpener()
    if co_status == 304:
        op.add(edgar.COMPANY_TICKERS_URL, 304, b"", exact=True)
    else:
        op.add(edgar.COMPANY_TICKERS_URL, 200, co if co is not None else fx_bytes("tick_co"),
               {"Last-Modified": CO_LAST_MODIFIED}, exact=True, gzip_body=True)
    op.add(edgar.COMPANY_TICKERS_MF_URL, 200, mf if mf is not None else fx_bytes("tick_mf"), exact=True,
           gzip_body=True)
    return op


def case_ua():
    op = FakeOpener()
    r.raises("ua empty user agent", lambda: edgar.EdgarClient("", opener=op), edgar.EdgarConfigError)
    r.raises("ua blank user agent", lambda: edgar.EdgarClient("   ", opener=op), edgar.EdgarConfigError)
    prev = os.environ.get("SEC_USER_AGENT")
    os.environ["SEC_USER_AGENT"] = ""
    try:
        r.raises("ua empty SEC_USER_AGENT", lambda: edgar.client_from_env(opener=op), edgar.EdgarConfigError)
        os.environ["SEC_USER_AGENT"] = UA
        r.eq("ua SEC_USER_AGENT is read", edgar.client_from_env(opener=op).user_agent, UA)
    finally:
        if prev is None:
            os.environ.pop("SEC_USER_AGENT", None)
        else:
            os.environ["SEC_USER_AGENT"] = prev
    r.eq("ua no request before the error", op.calls, [])
    url = edgar.submissions_url(ANET)
    r.eq("ua submissions url", url, "https://data.sec.gov/submissions/CIK0001596532.json")
    op.add(url, 200, fx_bytes("sub_anet"), exact=True, gzip_body=True)
    cl, _fc = new_client(op)
    data = cl.get_json(url)
    r.eq("ua a gzip body decodes", data, fx("sub_anet"))
    r.eq("ua one request", len(op.calls), 1)
    hdrs = op.calls[0][1]
    r.eq("ua User-Agent", hdrs.get("User-Agent"), UA)
    r.eq("ua Accept-Encoding", hdrs.get("Accept-Encoding"), "gzip")
    raw = FakeOpener().add(url, 200, gzip.compress(fx_bytes("sub_anet")), exact=True)
    r.eq("ua gzip magic decodes without the header", new_client(raw)[0].get_json(url)["cik"], "0001596532")
    text = new_client(FakeOpener().add(url, 200, fx_bytes("sub_anet"), exact=True))[0].get_text(url)
    r.ok("ua a plain body reads as text", text.startswith("{"), text[:20])
    r.eq("ua cik10", (edgar.cik10(1596532), edgar.cik10("CIK0001596532"), edgar.cik10("1181412")),
         ("0001596532", "0001596532", "0001181412"))
    r.eq("ua archive url", edgar.archive_url(ANET, "0001596532-26-000174", "anet-20260804.htm"),
         "https://www.sec.gov/Archives/edgar/data/1596532/000159653226000174/anet-20260804.htm")
    r.eq("ua index url", edgar.index_url(ANET, "0001596532-26-000174"),
         "https://www.sec.gov/Archives/edgar/data/1596532/000159653226000174/0001596532-26-000174-index.htm")
    r.eq("ua companyfacts url", edgar.companyfacts_url(1596532),
         "https://data.sec.gov/api/xbrl/companyfacts/CIK0001596532.json")


def case_limiter():
    url = edgar.submissions_url(ANET)
    fc = FakeClock()
    starts = []

    def opener(u, h, timeout):
        starts.append(fc.mono)
        return 200, {}, b"{}"

    cl, _ = new_client(opener, fc)
    for _ in range(20):
        cl.get(url)
    worst = max(sum(1 for t in starts if 0 <= t - s < 1.0) for s in starts)
    r.eq("limiter 20 requests sent", len(starts), 20)
    r.ok("limiter <= 5 start in any 1-s window", worst <= 5, worst)
    r.eq("limiter the first 5 start at once", starts[:5], [starts[0]] * 5)
    r.ok("limiter 20 requests take at least 3 s", starts[-1] - starts[0] >= 3.0, starts[-1] - starts[0])
    # Two clients on one clock share the limit (one process, one budget).
    fc2 = FakeClock()
    seen = []

    def op2(u, h, timeout):
        seen.append(fc2.mono)
        return 200, {}, b"{}"

    a, _ = new_client(op2, fc2)
    b, _ = new_client(op2, fc2)
    for i in range(12):
        (a if i % 2 else b).get(url)
    worst2 = max(sum(1 for t in seen if 0 <= t - s < 1.0) for s in seen)
    r.ok("limiter two clients on one clock share it", worst2 <= 5, worst2)


def case_retry():
    url = edgar.submissions_url(ANET)
    op = FakeOpener()
    for _ in range(4):
        op.add(url, 429, b"", exact=True)
    cl, fc = new_client(op)
    r.raises("retry 429 x4 raises", lambda: cl.get(url), edgar.EdgarError, "after 3 retries")
    r.eq("retry 429 sleeps 1, 4, 16", fc.sleeps, [1.0, 4.0, 16.0])
    r.eq("retry 429 four requests", len(op.calls), 4)
    op = FakeOpener().add(url, 503, b"", exact=True).add(url, 200, b'{"ok": 1}', exact=True)
    cl, fc = new_client(op)
    r.eq("retry 503 then 200 returns", cl.get_json(url), {"ok": 1})
    r.eq("retry 503 one sleep", fc.sleeps, [1.0])
    op = FakeOpener()                         # no route: the fake answers 404
    cl, fc = new_client(op)
    r.raises("retry 404 raises EdgarNotFound", lambda: cl.get(url), edgar.EdgarNotFound)
    r.ok("retry EdgarNotFound is an EdgarError", issubclass(edgar.EdgarNotFound, edgar.EdgarError))
    r.eq("retry 404 is not retried", (len(op.calls), fc.sleeps), (1, []))
    tries = []

    def flaky(u, h, timeout):
        tries.append(u)
        if len(tries) < 3:
            raise OSError("connection reset")
        return 200, {}, b"{}"

    cl, fc = new_client(flaky)
    r.eq("retry a network error is retried", (cl.get_json(url), len(tries), fc.sleeps), ({}, 3, [1.0, 4.0]))
    op = FakeOpener().add(url, 400, b"bad", exact=True)
    cl, fc = new_client(op)
    r.raises("retry another 4xx raises at once", lambda: cl.get(url), edgar.EdgarError, "HTTP 400")
    r.eq("retry 400 not retried", len(op.calls), 1)


def case_blocked():
    url = edgar.submissions_url(ANET)
    op = FakeOpener().add(url, 403, b"", exact=True)
    cl, fc = new_client(op)
    r.raises("blocked 403 raises EdgarBlocked", lambda: cl.get(url), edgar.EdgarBlocked)
    r.eq("blocked at once: one request, no sleep", (len(op.calls), fc.sleeps), (1, []))
    r.ok("blocked EdgarBlocked is an EdgarError", issubclass(edgar.EdgarBlocked, edgar.EdgarError))
    r.raises("blocked a conditional GET too", lambda: cl.get_conditional(url, {}), edgar.EdgarBlocked)


def case_map():
    with temp_root() as root:
        with root.led().connect() as conn:
            op = map_opener()
            cl, fc = new_client(op)
            now = "2026-10-27T12:00:00Z"
            res = edgar.refresh_ticker_maps(conn, cl, now)
            r.eq("map first refresh statuses", (res["company_tickers"]["status"], res["company_tickers_mf"]["status"]),
                 (200, 200))
            r.ok("map first refresh sends no If-Modified-Since",
                 all("If-Modified-Since" not in h for _u, h in op.calls), op.calls)
            co_rows = conn.execute("SELECT COUNT(*) FROM sec_ticker_map WHERE source='company_tickers'").fetchone()[0]
            mf_rows = conn.execute("SELECT COUNT(*) FROM sec_ticker_map WHERE source='company_tickers_mf'").fetchone()[0]
            r.eq("map company rows", co_rows, len(fx("tick_co")))
            mf_named = [x for x in fx("tick_mf")["data"] if x[3]]       # one real row has no symbol
            r.eq("map mf rows (2 tickers are in both files; company wins)", mf_rows, len(mf_named) - 2)
            spcx = conn.execute("SELECT cik, source FROM sec_ticker_map WHERE ticker='SPCX'").fetchone()
            r.eq("map SPCX keeps the company row", tuple(spcx), ("0001181412", "company_tickers"))
            from lib.desk import store
            r.eq("map Last-Modified stored", store.get_state(conn, "sec_map:company_tickers:last_modified"),
                 CO_LAST_MODIFIED)
            r.eq("map checked_at stored", store.get_state(conn, "sec_map:company_tickers:checked_at"), now)
            n_calls = len(op.calls)
            res = edgar.refresh_ticker_maps(conn, cl, "2026-11-02T12:00:00Z")
            r.eq("map < 7 days sends nothing", len(op.calls), n_calls)
            r.eq("map < 7 days status", res["company_tickers"]["status"], "fresh")
            op304 = map_opener(co_status=304)
            cl2, _ = new_client(op304)
            later = "2026-11-04T12:00:00Z"
            res = edgar.refresh_ticker_maps(conn, cl2, later)
            co_call = [h for u, h in op304.calls if u == edgar.COMPANY_TICKERS_URL]
            r.eq("map the weekly refresh sends If-Modified-Since", co_call[0].get("If-Modified-Since"), CO_LAST_MODIFIED)
            r.eq("map 304 status", res["company_tickers"]["status"], 304)
            r.eq("map 304 keeps rows", conn.execute(
                "SELECT COUNT(*) FROM sec_ticker_map WHERE source='company_tickers'").fetchone()[0], co_rows)
            r.eq("map 304 moves checked_at", store.get_state(conn, "sec_map:company_tickers:checked_at"), later)
            r.ok("map mf had no Last-Modified, so its GET is plain",
                 all("If-Modified-Since" not in h for u, h in op304.calls if u == edgar.COMPANY_TICKERS_MF_URL))
            op_f = map_opener(co_status=304)
            cl3, _ = new_client(op_f)
            res = edgar.refresh_ticker_maps(conn, cl3, later, force=True)
            r.eq("map force sends both", len(op_f.calls), 2)
            every = op.calls + op304.calls + op_f.calls
            r.ok("map every request sends the User-Agent and gzip",
                 all(h.get("User-Agent") == UA and h.get("Accept-Encoding") == "gzip" for _u, h in every), every[:1])


def seeded_map(conn, co=None):
    op = map_opener(co=co)
    cl, _ = new_client(op)
    edgar.refresh_ticker_maps(conn, cl, "2026-10-27T12:00:00Z")


def case_resolve():
    with temp_root() as root:
        with root.led().connect() as conn:
            seeded_map(conn)
            a = edgar.resolve(conn, "ANET")
            r.eq("resolve ANET", (a["cik"], a["kind"], a["series_id"], a["title"]),
                 ("0001596532", "company", None, "Arista Networks, Inc."))
            s = edgar.resolve(conn, "soxx")
            r.eq("resolve SOXX", (s["ticker"], s["cik"], s["kind"], s["series_id"]),
                 ("SOXX", "0001100663", "etf", "S000004354"))
            r.eq("resolve SPCX is not the mf 1719812", edgar.resolve(conn, "SPCX")["cik"], "0001181412")
            r.raises("resolve ZZZZ", lambda: edgar.resolve(conn, "ZZZZ"), edgar.UnknownTicker)
            r.eq("resolve BOT before overrides is a company", edgar.resolve(conn, "BOT")["kind"], "company")
            r.eq("resolve desk-seed writes 4 overrides", edgar.seed_overrides(conn), 4)
            r.eq("resolve seed again writes none", edgar.seed_overrides(conn), 0)
            rows = {t: (c, k) for t, c, k in conn.execute("SELECT ticker, cik, kind FROM sec_overrides")}
            r.eq("resolve override rows", rows, {"SPCX": ("0001181412", "company"), "BOT": (None, "cef"),
                                                 "BSOL": (None, "crypto_etf"), "SGOV": (None, "cash")})
            notes = dict(conn.execute("SELECT ticker, note FROM sec_overrides"))
            r.ok("resolve override notes", notes["SPCX"].startswith("SpaceX (IPO June 2026).")
                 and "residual cash" in notes["SGOV"], notes)
            b = edgar.resolve(conn, "BOT")
            r.eq("resolve BOT override kind wins, map CIK", (b["cik"], b["kind"]), ("0002081119", "cef"))
            r.eq("resolve BSOL", (edgar.resolve(conn, "BSOL")["cik"], edgar.resolve(conn, "BSOL")["kind"]),
                 ("0002045872", "crypto_etf"))
            g = edgar.resolve(conn, "SGOV")
            r.eq("resolve SGOV cash, map CIK + series", (g["cik"], g["kind"], g["series_id"]),
                 ("0001100663", "cash", "S000068768"))
            sp = edgar.resolve(conn, "SPCX")
            r.eq("resolve SPCX override", (sp["cik"], sp["kind"]), ("0001181412", "company"))
    # The override wins over the map: drop SPCX from company_tickers, so the map holds only
    # the old SPAC ETF series.
    co = {k: v for k, v in fx("tick_co").items() if v["ticker"] != "SPCX"}
    with temp_root() as root:
        with root.led().connect() as conn:
            import json
            seeded_map(conn, co=json.dumps(co).encode())
            m = edgar.resolve(conn, "SPCX")
            r.eq("resolve without the override SPCX is the mf series", (m["cik"], m["kind"]), ("0001719812", "etf"))
            edgar.seed_overrides(conn)
            o = edgar.resolve(conn, "SPCX")
            r.eq("resolve the override wins", (o["cik"], o["kind"], o["series_id"]), ("0001181412", "company", None))



# --- load_company (§7.7): the real ``events.load_company`` over the SEC captures --------

LOAD_NOW = "2026-10-28T14:00:00Z"          # a Wednesday session, after every captured filing
AV_KEY = "TESTKEY"


class Routes:
    """The ``SecFixtures`` opener plus exact-URL answers for captures that its last-path-part
    routes cannot reach (a file name that several SPCX filings share, the BOT submissions)."""

    def __init__(self, extra=None, **kw):
        from desk_fakes import SecFixtures
        self.sec = SecFixtures(**kw)
        self.extra = dict(extra or {})
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if url in self.extra:
            return (200, {}, self.extra[url])
        return self.sec(url, headers, timeout)


def load_seams(opener, *, av=True):
    from desk_fakes import seams
    from lib.desk import transcripts
    s = seams(opener=opener, av_key=AV_KEY if av else None)
    s.av_opener = FakeOpener().add(transcripts.BASE_URL, 200, fx_bytes("av_anet")) if av else None
    return s


def loaded_root(root, opener):
    """A temp ledger with the SEC ticker map (from the captures) and the four overrides."""
    from desk_fakes import seams
    led = root.led()
    with led.connect() as conn:
        edgar.refresh_ticker_maps(conn, seams(opener=opener).client, LOAD_NOW)
        edgar.seed_overrides(conn)
    return led


def desk_settings_of(root, led):
    from lib.desk import settings as desk_settings
    with led.connect() as conn:
        return desk_settings.effective(conn, root.cfg())


def counts(led, ticker):
    with led.connect() as conn:
        return {"documents": conn.execute("SELECT COUNT(*) FROM documents WHERE ticker=?", (ticker,)).fetchone()[0],
                "filings": conn.execute("SELECT COUNT(*) FROM filings WHERE ticker=?", (ticker,)).fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "xbrl": conn.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0],
                "metrics": conn.execute("SELECT COUNT(*) FROM metrics WHERE ticker=?", (ticker,)).fetchone()[0],
                "transcripts": conn.execute("SELECT COUNT(*) FROM transcripts WHERE ticker=?", (ticker,)).fetchone()[0]}


def expected_anet():
    """The three filings §7.7 step 3 picks, computed from the submissions capture."""
    rec = edgar.recent_filings(fx("sub_anet"))
    annual = max((f for f in rec if f["form"] in ("10-K", "20-F", "40-F")), key=lambda f: f["filed_date"])
    tenq = max((f for f in rec if f["form"] == "10-Q" and f["filed_date"] > annual["filed_date"]),
               key=lambda f: f["filed_date"])
    er = max((f for f in rec if f["form"] == "8-K" and "2.02" in (f["items"] or "")), key=lambda f: f["filed_date"])
    return rec, annual, tenq, er


_LOAD_STATE = {}


def case_load_anet():
    from desk_fakes import FakeClock
    from lib.desk import events, store, transcripts
    clock = FakeClock(LOAD_NOW).pin_market()
    try:
        root = temp_root()
        root.__enter__()
        _LOAD_STATE["root"] = root
        op = Routes()
        led = loaded_root(root, op)
        s = load_seams(op)
        _LOAD_STATE["seams"] = s
        sett = desk_settings_of(root, led)
        _LOAD_STATE["settings"] = sett
        out = events.load_company(led, "ANET", settings=sett, seams=s)
        rec, annual, tenq, er = expected_anet()
        r.eq("load_anet the three picks are the captured files",
             (annual["accession"], tenq["accession"], er["accession"]),
             ("0001596532-26-000013", "0001596532-26-000175", "0001596532-26-000174"))
        with led.connect() as conn:
            comp = store.get_company(conn, "ANET")
            r.eq("load_anet a new name is candidate", (comp["status"], comp["kind"], comp["cik"]),
                 ("candidate", "company", "0001596532"))
            r.eq("load_anet summary status", out["status"], "candidate")
            n = conn.execute("SELECT COUNT(*) FROM filings WHERE ticker='ANET'").fetchone()[0]
            r.eq("load_anet 1,000 filings", (n, out["filings"], len(rec)), (1000, 1000, 1000))
            tiers = [tuple(x) for x in conn.execute("SELECT tier, COUNT(*) FROM filings GROUP BY tier")]
            r.eq("load_anet all baseline", tiers, [("baseline", 1000)])
            r.eq("load_anet 0 events", conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            keys = {}
            for acc, key in conn.execute("SELECT accession, section_key FROM documents WHERE kind='filing_section'"):
                keys.setdefault(acc, set()).add(key)
            k10 = keys.get(annual["accession"], set())
            r.ok("load_anet sections of the 10-K", {"10-K:1", "10-K:1A", "10-K:7", "10-K:8"} <= k10, sorted(k10))
            kq = keys.get(tenq["accession"], set())
            r.ok("load_anet sections of the 10-Q", {"10-Q:I-1", "10-Q:I-2", "10-Q:II-1A"} <= kq, sorted(kq))
            r.eq("load_anet 8-K -000174 body + EX-99.1", keys.get(er["accession"]), {"8-K:body", "8-K:EX-99.1"})
            r.eq("load_anet only the three picks are ingested", set(keys), {annual["accession"], tenq["accession"],
                                                                         er["accession"]})
            ing = [tuple(x) for x in conn.execute(
                "SELECT accession FROM filings WHERE ingested_at IS NOT NULL ORDER BY accession")]
            r.eq("load_anet the three filings are marked ingested", sorted(a for (a,) in ing),
                 sorted([annual["accession"], tenq["accession"], er["accession"]]))
            r.eq("load_anet no ingest error", conn.execute(
                "SELECT COUNT(*) FROM filings WHERE ingest_error IS NOT NULL").fetchone()[0], 0)
            xrows = conn.execute("SELECT COUNT(*) FROM xbrl_facts WHERE cik='0001596532'").fetchone()[0]
            r.ok("load_anet XBRL rows", xrows > 1000, xrows)
            r.ok("load_anet XBRL holds the 10-Q facts", conn.execute(
                "SELECT COUNT(*) FROM xbrl_facts WHERE accession=?", (tenq["accession"],)).fetchone()[0] > 0)
            m = [tuple(x) for x in conn.execute("SELECT ticker, source_accession, price FROM metrics")]
            r.eq("load_anet a metrics row", m, [("ANET", tenq["accession"], 150.0)])
            r.eq("load_anet summary metrics", out["metrics"], True)
            t = [tuple(x) for x in conn.execute("SELECT ticker, quarter, document_id FROM transcripts")]
            r.eq("load_anet the transcript row", [x[:2] for x in t], [("ANET", fx("av_anet")["quarter"])])
            doc = store.get_document(conn, t[0][2]) if t else None
            r.ok("load_anet the transcript document", doc and doc["kind"] == "transcript" and "apikey" not in (doc["url"] or ""),
                 doc and {k: doc[k] for k in ("kind", "url", "char_len")})
            r.eq("load_anet summary transcript", (out["transcript"] or {}).get("state"), "stored")
            day = transcripts.budget_day(LOAD_NOW)
            r.eq("load_anet budget 1", store.budget_used(conn, transcripts.PROVIDER, day), 1)
        av_calls = [u for u, _h in s.av_opener.calls]
        r.eq("load_anet one Alpha Vantage call", len(av_calls), 1)
        r.ok("load_anet the call asks for the 8-K's quarter",
             av_calls and "function=EARNINGS_CALL_TRANSCRIPT" in av_calls[0] and "symbol=ANET" in av_calls[0]
             and f"quarter={fx('av_anet')['quarter']}" in av_calls[0], av_calls)
        r.eq("load_anet summary documents", out["documents"], counts(led, "ANET")["documents"])
        _LOAD_STATE["op"] = op
        _LOAD_STATE["counts"] = counts(led, "ANET")
    finally:
        clock.unpin()


def case_load_again():
    from desk_fakes import FakeClock
    from lib.desk import events, store, transcripts
    root = _LOAD_STATE.get("root")
    if root is None:
        r.ok("load_again needs load_anet", False)
        return
    clock = FakeClock("2026-10-28T15:00:00Z").pin_market()
    try:
        led = root.led()
        op, s = _LOAD_STATE["op"], _LOAD_STATE["seams"]
        n_sec = len(op.calls)
        n_av = len(s.av_opener.calls)
        out = events.load_company(led, "ANET", settings=_LOAD_STATE["settings"], seams=s)
        after = counts(led, "ANET")
        before = _LOAD_STATE["counts"]
        r.eq("load_again adds no document", after["documents"], before["documents"])
        r.eq("load_again adds no filing", (after["filings"], out["filings"]), (before["filings"], 0))
        r.eq("load_again adds no event", after["events"], 0)
        r.eq("load_again adds no transcript", after["transcripts"], before["transcripts"])
        r.eq("load_again sends no Alpha Vantage call", len(s.av_opener.calls), n_av)
        with led.connect() as conn:
            r.eq("load_again budget stays 1", store.budget_used(conn, transcripts.PROVIDER,
                                                                transcripts.budget_day(LOAD_NOW)), 1)
        fetched = [u for u in op.calls[n_sec:] if "/Archives/" in u]
        r.eq("load_again fetches no ingested filing again", fetched, [])
        with led.connect() as conn:
            store.set_company_status(conn, "ANET", "watch", "owner asked", store.now_iso())
        events.load_company(led, "ANET", settings=_LOAD_STATE["settings"], seams=s)
        with led.connect() as conn:
            r.eq("load_again keeps an existing status", store.get_company(conn, "ANET")["status"], "watch")
    finally:
        clock.unpin()
        root.__exit__(None, None, None)
        _LOAD_STATE.clear()


def case_load_other():
    from desk_fakes import FIX, FakeClock
    from lib.desk import events, store
    spcx_424 = next(f for f in edgar.recent_filings(fx("sub_spcx")) if f["form"] == "424B4")
    url_424 = edgar.archive_url("0001181412", spcx_424["accession"], spcx_424["primary_doc"])
    bot_sub = gzip.decompress((FIX / "sub_0002081119.json.gz").read_bytes())
    extra = {url_424: fx_bytes("s1"), edgar.submissions_url("0002081119"): bot_sub}
    clock = FakeClock(LOAD_NOW).pin_market()
    try:
        with temp_root() as root:
            op = Routes(extra)
            led = loaded_root(root, op)
            s = load_seams(op, av=False)
            sett = desk_settings_of(root, led)
            with led.connect() as conn:
                r.eq("load_other no annual report in the SPCX capture",
                     [f["form"] for f in edgar.recent_filings(fx("sub_spcx")) if f["form"] in ("10-K", "20-F", "40-F")],
                     [])
            out = events.load_company(led, "SPCX", settings=sett, seams=s)
            with led.connect() as conn:
                keys = sorted(x[0] for x in conn.execute(
                    "SELECT section_key FROM documents WHERE accession=?", (spcx_424["accession"],)))
                r.ok("load_other SPCX stores the 424B4 sections", len(keys) >= 3 and all(k.startswith("424B4:")
                                                                                        for k in keys), keys)
                r.eq("load_other SPCX is a company", (out["kind"], out["cik"]), ("company", "0001181412"))
                r.ok("load_other SPCX filings stored as baseline", conn.execute(
                    "SELECT COUNT(*) FROM filings WHERE ticker='SPCX' AND tier='baseline'").fetchone()[0] == len(
                    edgar.recent_filings(fx("sub_spcx"))))
                r.ok("load_other SPCX the 424B4 is in the summary",
                     any(i["accession"] == spcx_424["accession"] and i["ok"] for i in out["ingested"]), out["ingested"])
            n0 = len(op.calls)
            out = events.load_company(led, "SGOV", settings=sett, seams=s)
            with led.connect() as conn:
                r.eq("load_other SGOV is cash", out["kind"], "cash")
                r.eq("load_other SGOV fetches no filings", (op.calls[n0:], conn.execute(
                    "SELECT COUNT(*) FROM filings WHERE ticker='SGOV'").fetchone()[0]), ([], 0))
                r.eq("load_other SGOV stores no holdings", conn.execute(
                    "SELECT COUNT(*) FROM etf_holdings WHERE fund='SGOV'").fetchone()[0], 0)
            out = events.load_company(led, "BOT", settings=sett, seams=s)
            with led.connect() as conn:
                nb = conn.execute("SELECT COUNT(*) FROM filings WHERE ticker='BOT'").fetchone()[0]
                r.eq("load_other BOT is a cef", out["kind"], "cef")
                r.eq("load_other BOT stores filings", (nb, out["filings"]),
                     (len(edgar.recent_filings(__import__("json").loads(bot_sub))),) * 2)
                r.eq("load_other BOT stores no look-through rows",
                     (conn.execute("SELECT COUNT(*) FROM etf_holdings WHERE fund='BOT'").fetchone()[0],
                      conn.execute("SELECT COUNT(*) FROM companies WHERE status_reason LIKE 'look-through:%'").fetchone()[0],
                      out["lookthrough"]), (0, 0, []))
                r.eq("load_other no events", conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
                r.raises("load_other an unknown ticker returns an error",
                         lambda: events.load_company(led, "ZZZZ", settings=sett, seams=s), edgar.UnknownTicker)
                r.eq("load_other new names are candidate",
                     sorted(tuple(x) for x in conn.execute("SELECT ticker, status FROM companies")),
                     [("BOT", "candidate"), ("SGOV", "candidate"), ("SPCX", "candidate")])
    finally:
        clock.unpin()


CASES = [case_ua, case_limiter, case_retry, case_blocked, case_map, case_resolve,
         case_load_anet, case_load_again, case_load_other]

if __name__ == "__main__":
    for case in CASES:
        case()
    raise SystemExit(r.done())
