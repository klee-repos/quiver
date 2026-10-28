"""News (plan §7.8, §5.4 ``log_news_event``): ingest, the classify batch, the reply parser,
retries, news events, and the desk tool.

Fixtures: ``yf_news`` (ANET) and ``yf_news_ceg`` (CEG), both captured from
``yf.Search(t, news_count=20).news``. Rows go through the real producers:
``fetch_news_items`` -> ``news.ingest``. Classifier replies are strings in the §5.3 shape.
Article pages go through ``documents.store_fetched`` with ``FakeResolver``/``FakeConn``;
no Yahoo article page was captured, so real EDGAR HTML captures stand in as bodies.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

from desk_fakes import FakeConn, FakeResolver, Results, fx, fx_bytes, temp_root

from lib.desk import documents, news, store
from lib.desk.news import NewsError
from lib.dataflows.yfinance_news import fetch_news_items

r = Results("desk-news")
NOW = "2026-10-28T15:00:00Z"
NOW_DT = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)
YAHOO = "finance.yahoo.com"
YAHOO_IP = "69.147.88.8"
HTML_CT = {"Content-Type": "text/html; charset=utf-8"}
FEED_ANET = fx("yf_news")["news"]
FEED_CEG = fx("yf_news_ceg")["news"]
FEED_VST = [a for a in FEED_CEG if "VST" in a["relatedTickers"]]   # DROP entries of the CEG capture


def _fetcher(feeds: dict):
    calls = []

    def fetch(ticker, limit):
        calls.append(ticker)
        return fetch_news_items(ticker, limit, search_fn=lambda q, c: feeds.get(q, []),
                                ticker_news_fn=lambda q, c: [])
    fetch.calls = calls
    return fetch


def _count(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def _payload(conn, job_id):
    return json.loads(conn.execute("SELECT payload_json FROM jobs WHERE id=?", (job_id,)).fetchone()[0])


def _reply(inp, special=None, extra=None):
    """A classifier reply in the §5.3 shape: every id not material unless ``special`` says so."""
    out = []
    for row in inp:
        el = {"id": row["id"], "material": False, "category": "other", "reason": "routine coverage"}
        el.update((special or {}).get(row["id"], {}))
        out.append(el)
    return json.dumps(out + list(extra or []))


def _worker_done(conn, job_id):
    # run_worker.py owns this step (§9.2 step 2): it marks a finished job done.
    conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id=?", (NOW, job_id))


def _path(url):
    return url.split(YAHOO, 1)[1]


# --- dedupe ----------------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    store.upsert_company(conn, ticker="ANET", kind="company", status="holding", now=NOW)
    store.upsert_company(conn, ticker="CEG", kind="company", status="watch", now=NOW)
    store.upsert_company(conn, ticker="VST", kind="company", status="candidate", now=NOW)
    store.upsert_company(conn, ticker="SGOV", kind="cash", status="holding", now=NOW)
    fetch = _fetcher({"ANET": FEED_ANET, "CEG": FEED_CEG, "VST": FEED_VST})
    n = news.ingest(conn, None, now=NOW, fetch_fn=fetch)
    r.eq("dedupe: holding and watch fetched, candidate and cash not", fetch.calls, ["ANET", "CEG"])
    r.eq("dedupe: 40 new rows", n, 40)
    r.eq("dedupe: 20 ANET rows", _count(conn, "SELECT COUNT(*) FROM news_items WHERE ticker='ANET'"), 20)
    first = conn.execute("SELECT * FROM news_items WHERE ticker='ANET' AND url=?",
                         (FEED_ANET[0]["link"],)).fetchone()
    r.eq("dedupe: the key is SHA-256 of the URL", first["url_hash"], store.sha256_hex(FEED_ANET[0]["link"]))
    r.eq("dedupe: published_at from the feed", first["published_at"],
         store.iso(datetime.fromtimestamp(FEED_ANET[0]["providerPublishTime"], timezone.utc)))
    r.eq("dedupe: title and publisher", (first["title"], first["publisher"]),
         (FEED_ANET[0]["title"], FEED_ANET[0]["publisher"]))
    r.eq("dedupe: seen_at is now", first["seen_at"], NOW)
    r.eq("dedupe: a new row is unclassified", (first["material"], first["classify_attempts"]), (None, 0))
    n2 = news.ingest(conn, None, now="2026-10-28T16:00:00Z", fetch_fn=fetch)
    r.eq("dedupe: one URL twice -> one row", (n2, _count(conn, "SELECT COUNT(*) FROM news_items")), (0, 40))
    twice = _fetcher({"ANET": [FEED_ANET[0], copy.deepcopy(FEED_ANET[0])]})
    with temp_root() as root2, root2.led().connect() as c2:
        r.eq("dedupe: one URL twice in one feed -> one row", news.ingest(c2, ["ANET"], now=NOW, fetch_fn=twice), 1)
    # G5: one article for CEG and VST -> a row each.
    store.set_company_status(conn, "VST", "watch", None, NOW)
    n3 = news.ingest(conn, None, now=NOW, fetch_fn=fetch)
    r.eq("dedupe: VST (now watch) adds its 3 rows", n3, len(FEED_VST))
    shared = FEED_VST[0]["link"]
    rows = conn.execute("SELECT ticker, url_hash FROM news_items WHERE url=? ORDER BY ticker", (shared,)).fetchall()
    r.eq("dedupe: one article for CEG and VST -> a row each (G5)", [x[0] for x in rows], ["CEG", "VST"])
    r.eq("dedupe: both rows share the url_hash", len({x[1] for x in rows}), 1)
    # A fuzzy search hit that does not name the ticker is skipped.
    fuzzy = _fetcher({"ANET": FEED_CEG[:1]})
    r.eq("dedupe: a row whose related_tickers lack the ticker is skipped",
         news.ingest(conn, ["ANET"], now=NOW, fetch_fn=fuzzy), 0)

    def _fail(ticker, limit):
        raise RuntimeError("yfinance down")
    r.raises("dedupe: every fetch failing raises NewsError",
             lambda: news.ingest(conn, ["ANET", "CEG"], now=NOW, fetch_fn=_fail), NewsError, "yfinance down")

    def _half(ticker, limit):
        if ticker == "ANET":
            raise RuntimeError("yfinance down")
        return fetch(ticker, limit)
    r.eq("dedupe: one failed ticker does not stop the sweep",
         news.ingest(conn, ["ANET", "CEG"], now=NOW, fetch_fn=_half), 0)

    # --- batch -------------------------------------------------------------------
    total = _count(conn, "SELECT COUNT(*) FROM news_items")
    r.eq("batch: 43 unclassified rows", total, 43)
    job = news.queue_classify(conn, NOW)
    r.ok("batch: a job id", isinstance(job, int), job)
    jobs = conn.execute("SELECT * FROM jobs").fetchall()
    r.eq("batch: one classify_news job", [(j["kind"], j["status"], j["requested_by"]) for j in jobs],
         [("classify_news", "queued", "monitor")])
    payload = _payload(conn, job)
    items = payload["items"]
    r.eq("batch: at most 40 headlines", len(items), 40)
    r.eq("batch: the payload holds only (url_hash, ticker) keys", {tuple(sorted(i)) for i in items},
         {("ticker", "url_hash")})
    pubs = [conn.execute("SELECT published_at FROM news_items WHERE url_hash=? AND ticker=?",
                         (i["url_hash"], i["ticker"])).fetchone()[0] for i in items]
    r.eq("batch: oldest first", pubs, sorted(pubs))
    left = conn.execute("SELECT MIN(published_at) FROM news_items WHERE NOT EXISTS (SELECT 1 FROM json_each(?) j "
                        "WHERE json_extract(j.value,'$.url_hash')=news_items.url_hash AND "
                        "json_extract(j.value,'$.ticker')=news_items.ticker)", (json.dumps(items),)).fetchone()[0]
    r.ok("batch: the 3 rows left out are the newest", left >= pubs[-1], (left, pubs[-1]))
    r.eq("batch: no second job while one is queued", news.queue_classify(conn, NOW), None)

    # --- parse -------------------------------------------------------------------
    inp = news.classifier_input(conn, payload)
    r.eq("parse: classifier_input has 40 rows", len(inp), 40)
    r.eq("parse: classifier_input keys", set(inp[0]), {"id", "ticker", "title", "publisher"})
    r.eq("parse: ids are 1..40", [x["id"] for x in inp], list(range(1, 41)))
    long_reason = "Arista wins a multi-year 800G contract with a hyperscaler; " * 5
    reply = _reply(inp, special={
        1: {"material": True, "category": "contract", "reason": long_reason},
        2: {"material": True, "category": "M&A", "reason": "acquires a switch maker"},
        3: {"material": True, "category": "earnings", "reason": "unknown category"},
        4: {"material": "yes", "category": "product"},
    }, extra=[{"id": 99, "material": True, "category": "contract", "reason": "foreign"}])
    res = news.apply_classification(conn, payload, reply, now=NOW)
    r.eq("parse: a valid array parses", res["parsed"], True)
    r.eq("parse: 38 classified", res["classified"], 38)
    r.eq("parse: a foreign id is ignored", res["ignored_ids"], [99])
    row1 = conn.execute("SELECT * FROM news_items WHERE url_hash=? AND ticker=?",
                        (items[0]["url_hash"], items[0]["ticker"])).fetchone()
    r.eq("parse: the 4 fields are set", (row1["material"], row1["category"], row1["classified_at"]),
         (1, "contract", NOW))
    r.eq("parse: a reason over 160 chars is cut", len(row1["reason"]), 160)
    r.ok("parse: the cut reason keeps its start", row1["reason"].startswith("Arista wins a multi-year"), row1["reason"])
    row2 = conn.execute("SELECT material, category FROM news_items WHERE url_hash=? AND ticker=?",
                        (items[1]["url_hash"], items[1]["ticker"])).fetchone()
    r.eq("parse: the category is stored in lowercase", tuple(row2), (1, "m&a"))
    for idx, label in ((2, "an unknown category"), (3, "a material that is not a boolean")):
        rw = conn.execute("SELECT material, classify_attempts FROM news_items WHERE url_hash=? AND ticker=?",
                          (items[idx]["url_hash"], items[idx]["ticker"])).fetchone()
        r.eq(f"parse: {label} counts as unclassified", tuple(rw), (None, 1))
    r.eq("parse: the material list", res["material"], [items[0], items[1]])
    r.eq("parse: a foreign id changed no row",
         _count(conn, "SELECT COUNT(*) FROM news_items WHERE reason='foreign'"), 0)
    again = news.apply_classification(conn, payload, reply.replace('"other"', '"contract"'), now=NOW)
    r.eq("parse: a classified row is not changed again", again["classified"], 0)

with temp_root() as root, root.led().connect() as conn:
    store.upsert_company(conn, ticker="ANET", kind="company", status="holding", now=NOW)
    news.ingest(conn, None, now=NOW, fetch_fn=_fetcher({"ANET": FEED_ANET}))
    job = news.queue_classify(conn, NOW)
    payload = _payload(conn, job)
    inp = news.classifier_input(conn, payload)
    fenced = ("Here is the classification.\n```json\n"
              + _reply(inp, special={5: {"material": True, "category": "guidance", "reason": "raises FY guide"}})
              + "\n```\n")
    res = news.apply_classification(conn, payload, fenced, now=NOW)
    r.eq("parse: a fenced ```json reply parses", (res["parsed"], res["classified"]), (True, 20))
    r.eq("parse: the fenced reply sets the material row", len(res["material"]), 1)
    r.eq("parse: parse_reply of a bare object", news.parse_reply('{"id": 1, "material": false, '
                                                               '"category": "other", "reason": "x"}')[0]["id"], 1)

# --- retry -----------------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    store.upsert_company(conn, ticker="ANET", kind="company", status="holding", now=NOW)
    news.ingest(conn, None, now=NOW, fetch_fn=_fetcher({"ANET": FEED_ANET}))
    attempts = []
    for run in range(3):
        job = news.queue_classify(conn, NOW)
        r.ok(f"retry: run {run + 1} queues a job", job, job)
        payload = _payload(conn, job)
        r.eq(f"retry: run {run + 1} retries all 20", len(payload["items"]), 20)
        res = news.apply_classification(conn, payload, "Sorry, I cannot help with that [", now=NOW)
        r.eq(f"retry: run {run + 1} reply does not parse", res["parsed"], False)
        attempts.append(sorted({x[0] for x in conn.execute("SELECT classify_attempts FROM news_items")}))
        _worker_done(conn, job)
    r.eq("retry: bad JSON adds 1 to classify_attempts each run", attempts, [[1], [2], [3]])
    rows = conn.execute("SELECT material, reason, category FROM news_items").fetchall()
    r.eq("retry: after 3 -> material=0, reason='unclassified'", {tuple(x) for x in rows},
         {(0, "unclassified", None)})
    r.eq("retry: a given-up row is not queued again", news.queue_classify(conn, NOW), None)
    r.eq("retry: the last run reports 20 given up", res["gave_up"], 20)

# --- event -----------------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    store.upsert_company(conn, ticker="ANET", kind="company", status="holding", now=NOW)
    store.upsert_company(conn, ticker="CEG", kind="company", status="holding", now=NOW)
    news.ingest(conn, None, now=NOW, fetch_fn=_fetcher({"ANET": FEED_ANET, "CEG": FEED_CEG}))
    job = news.queue_classify(conn, NOW)
    payload = _payload(conn, job)
    inp = news.classifier_input(conn, payload)
    anet_ids = [x["id"] for x in inp if x["ticker"] == "ANET"][:5]
    ceg_ids = [x["id"] for x in inp if x["ticker"] == "CEG"][:1]
    special = {i: {"material": True, "category": "contract", "reason": "new hyperscaler order"}
               for i in anet_ids + ceg_ids}
    news.apply_classification(conn, payload, _reply(inp, special=special), now=NOW)
    mats = conn.execute("SELECT url_hash, ticker, url FROM news_items WHERE material=1 AND ticker='ANET' "
                        "ORDER BY published_at, seen_at, ticker, url_hash").fetchall()
    r.eq("event: 5 material ANET rows", len(mats), 5)
    res_ = FakeResolver({YAHOO: [YAHOO_IP]})
    cn = FakeConn()
    cn.add(YAHOO_IP, _path(mats[0]["url"]), 403, b"", {})          # a paywall: status only
    bodies = ["ex991", "body_anet", "ex991_ceg", "body_ceg"]
    for m, fid in zip(mats[1:], bodies):
        cn.add(YAHOO_IP, _path(m["url"]), 200, fx_bytes(fid), HTML_CT)
    ceg_row = conn.execute("SELECT url FROM news_items WHERE material=1 AND ticker='CEG'").fetchone()
    cn.add(YAHOO_IP, _path(ceg_row["url"]), 200, fx_bytes("spcx_ex991"), HTML_CT)
    fetched = []

    def fetch_document(url, kind="news", ticker=None):
        fetched.append((url, kind, ticker))
        return documents.store_fetched(conn, url, kind=kind, ticker=ticker, now=NOW, resolver=res_, connector=cn)
    ids = news.handle_material(conn, now=NOW, fetch_document=fetch_document)
    evs = [store.get_event(conn, i) for i in ids]
    anet_evs = [e for e in evs if e["ticker"] == "ANET"]
    r.eq("event: 3 ANET events (the cap)", len(anet_evs), 3)
    r.eq("event: 1 CEG event (the cap is per ticker)", len([e for e in evs if e["ticker"] == "CEG"]), 1)
    r.eq("event: material -> fetch_document with kind news",
         [(u, k, t) for u, k, t in fetched if t == "ANET"], [(m["url"], "news", "ANET") for m in mats[:3]])
    r.eq("event: dedup keys news:<ticker>:<url_hash>", [e["dedup_key"] for e in anet_evs],
         [f"news:ANET:{m['url_hash']}" for m in mats[:3]])
    d0 = json.loads(anet_evs[0]["detail_json"])
    r.ok("event: a paywall 403 still makes the event", d0["fetch_error"] and "HTTP 403" in d0["fetch_error"], d0)
    r.eq("event: the 403 event keeps the headline", (d0["title"], d0["document_id"]),
         (conn.execute("SELECT title FROM news_items WHERE url_hash=?", (mats[0]["url_hash"],)).fetchone()[0], None))
    d1 = json.loads(anet_evs[1]["detail_json"])
    linked = conn.execute("SELECT document_id FROM news_items WHERE url_hash=? AND ticker='ANET'",
                          (mats[1]["url_hash"],)).fetchone()[0]
    r.ok("event: a fetched article is linked", d1["document_id"] and d1["document_id"] == linked, (d1, linked))
    r.eq("event: created_by monitor, type news, status new",
         {(e["created_by"], e["type"], e["status"]) for e in evs}, {("monitor", "news", "new")})
    r.eq("event: the 4th one for a ticker in a day makes no event",
         _count(conn, "SELECT COUNT(*) FROM events WHERE dedup_key=?", (f"news:ANET:{mats[3]['url_hash']}",)), 0)
    r.ok("event: the 4th and 5th are not fetched", all(u != mats[3]["url"] and u != mats[4]["url"]
                                                        for u, _k, _t in fetched), fetched)
    r.eq("event: the 4th stays recorded as material",
         _count(conn, "SELECT material FROM news_items WHERE url_hash=? AND ticker='ANET'", (mats[3]["url_hash"],)), 1)
    n_fetch = len(fetched)
    r.eq("event: a second run the same day makes nothing", news.handle_material(
        conn, now="2026-10-28T18:00:00Z", fetch_document=fetch_document), [])
    r.eq("event: a second run fetches nothing", len(fetched), n_fetch)
    r.eq("event: the next day makes nothing for yesterday's rows", news.handle_material(
        conn, now="2026-10-29T15:00:00Z", fetch_document=fetch_document), [])

# --- log_news --------------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    store.upsert_company(conn, ticker="ANET", kind="company", status="holding", now=NOW)
    feed = copy.deepcopy(FEED_ANET[:4])
    ages = [timedelta(days=7), timedelta(days=8), timedelta(days=1), timedelta(hours=3)]
    for art, age in zip(feed, ages):
        art["providerPublishTime"] = int((NOW_DT - age).timestamp())   # change a VALUE
    news.ingest(conn, ["ANET"], now=NOW, fetch_fn=_fetcher({"ANET": feed}))
    res_ = FakeResolver({YAHOO: [YAHOO_IP]})
    cn = FakeConn()
    for art, fid in zip(feed, ["ex991", "ex991_ceg", "spcx_ex991", "body_anet"]):
        cn.add(YAHOO_IP, _path(art["link"]), 200, fx_bytes(fid), HTML_CT)
    docs = [documents.store_fetched(conn, a["link"], kind="news", ticker="ANET", now=NOW, resolver=res_,
                                    connector=cn) for a in feed]
    r.eq("log_news: 4 distinct news documents", len(set(docs)), 4)
    eid = news.log_news_event(conn, ticker="anet", document_id=docs[0], now=NOW)
    ev = store.get_event(conn, eid)
    r.eq("log_news: a 7-day-old document -> event", (ev["type"], ev["created_by"], ev["ticker"]),
         ("news", "desk", "ANET"))
    r.eq("log_news: the dedup key", ev["dedup_key"], f"news:ANET:{store.sha256_hex(feed[0]['link'])}")
    r.eq("log_news: published_at in the detail is the feed's",
         json.loads(ev["detail_json"])["published_at"], store.iso(NOW_DT - ages[0]))
    r.raises("log_news: 8 days -> refused", lambda: news.log_news_event(
        conn, ticker="ANET", document_id=docs[1], now=NOW), NewsError, "more than 7 days")
    r.raises("log_news: one desk news event per ticker per day", lambda: news.log_news_event(
        conn, ticker="ANET", document_id=docs[2], now=NOW), NewsError, "already has a desk news event today")
    eid2 = news.log_news_event(conn, ticker="ANET", document_id=docs[2], now="2026-10-29T15:00:00Z")
    r.ok("log_news: the next day allows one more", eid2 and eid2 != eid, eid2)
    r.ok("log_news: a 2-day-old document on 10-30 -> event",
         news.log_news_event(conn, ticker="ANET", document_id=docs[3], now="2026-10-30T15:00:00Z"))
    r.raises("log_news: the same item twice is refused", lambda: news.log_news_event(
        conn, ticker="ANET", document_id=docs[3], now="2026-10-31T15:00:00Z"), NewsError, "already covers")
    sec_res = FakeResolver({"www.sec.gov": ["23.53.40.98"]})
    sec_cn = FakeConn().add("23.53.40.98", "/x.htm", 200, fx_bytes("body_ceg"), HTML_CT)
    web = documents.store_fetched(conn, "https://www.sec.gov/x.htm", kind="web", ticker="ANET", now=NOW,
                                  resolver=sec_res, connector=sec_cn)
    r.raises("log_news: a web document backs no event", lambda: news.log_news_event(
        conn, ticker="ANET", document_id=web, now="2026-10-31T15:00:00Z"), NewsError, "web document")
    r.raises("log_news: a news document of another ticker is refused", lambda: news.log_news_event(
        conn, ticker="CEG", document_id=docs[3], now="2026-10-31T15:00:00Z"), NewsError, "no news_items row of CEG")
    r.raises("log_news: an unknown document is refused", lambda: news.log_news_event(
        conn, ticker="ANET", document_id=99999, now=NOW), NewsError, "no document")

def _lock_free(db) -> bool:
    """Gate B D2: True when another process could take the write lock now."""
    import sqlite3 as _sq
    c = _sq.connect(str(db), timeout=0.05, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("ROLLBACK")
        return True
    except _sq.OperationalError:
        return False
    finally:
        c.close()


# --- gate B D2: no write lock is held during a network call ------------------------
with temp_root() as root, root.led().connect() as conn:
    store.upsert_company(conn, ticker="ANET", kind="company", status="holding", now=NOW)
    store.upsert_company(conn, ticker="CEG", kind="company", status="watch", now=NOW)
    base = _fetcher({"ANET": FEED_ANET, "CEG": FEED_CEG})
    free = []

    def fetch_locked(ticker, limit=None):
        free.append(_lock_free(root.db))
        return base(ticker, limit)
    news.ingest(conn, None, now=NOW, fetch_fn=fetch_locked)
    r.eq("lock: every news fetch runs with the write lock free", free, [True, True])
    job = news.queue_classify(conn, NOW)
    payload = _payload(conn, job)
    inp = news.classifier_input(conn, payload)
    first = next(x for x in inp if x["ticker"] == "ANET")
    special = {first["id"]: {"material": True, "category": "contract", "reason": "new order"}}
    news.apply_classification(conn, payload, _reply(inp, special=special), now=NOW)
    seen = []

    def fetch_document(url, kind="news", ticker=None):
        # The worker stores the page on a second connection (run_worker._fetch_news_document).
        seen.append(_lock_free(root.db))
        return None
    news.handle_material(conn, now=NOW, fetch_document=fetch_document)
    r.ok("lock: handle_material fetched a material page", len(seen) >= 1, seen)
    r.ok("lock: every material page fetch runs with the write lock free", seen and all(seen), seen)

raise SystemExit(r.done())
