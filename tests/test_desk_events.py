"""The EDGAR sweep and the analysis queue (plan §9.1 steps 4 and 9, §18.9 G28).

Each case is a ``case_*`` function in ``CASES``. A case seeds a temp ledger with the real
``desk-seed`` over the SEC captures with some filings DROPPED (rule 4: a test may drop
entries or change values), then runs ``events.sweep`` with the full captures, so the
dropped filings arrive as new. Events and jobs come from the real producers.
Run: .venv/bin/python tests/test_desk_events.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import (REPO, TEST_BOOK, FakeClock, FakePrices, Results, SecFixtures, fx,  # noqa: E402
                        fx_bytes, seams, seed_desk, temp_root)

from lib.desk import edgar, etf, events, store  # noqa: E402

r = Results("desk-events")
NOW = "2026-10-28T14:00:00Z"            # a Wednesday session, after every captured filing
OWNER = "111"
SOXX_SERIES = "S000004354"
SOXX_CIK = "0001100663"
SOXX_ACC = "0002071691-26-019781"

SOXX_BOOK = TEST_BOOK.replace("rh_tradable_confirmed: [ANET, CEG, SGOV]", "rh_tradable_confirmed: [SOXX, CEG, SGOV]") \
    .replace("{ticker: ANET, sleeve: AI Networking, weight: 25, band: 5}",
             "{ticker: SOXX, sleeve: Semiconductors, weight: 25, band: 5}")


# --- fixture edits (drop entries, change values; never add a key) -----------------------

def recent_index(fid: str, accession: str) -> int:
    return fx(fid)["filings"]["recent"]["accessionNumber"].index(accession)


def edit_recent(drop=(), values=None):
    """A ``SecFixtures`` edit of a submissions file: drop the rows at the indexes ``drop``,
    and set ``values[(index, column)]`` on the rows kept."""
    drop, values = set(drop), dict(values or {})

    def fn(data):
        rec = data["filings"]["recent"]
        for (i, col), v in values.items():
            rec[col][i] = v
        for col, vals in rec.items():
            if isinstance(vals, list):
                rec[col] = [x for i, x in enumerate(vals) if i not in drop]
        return data
    return fn


def drop_facts(accession: str):
    """A ``SecFixtures`` edit of a companyfacts file: drop every fact of ``accession``."""
    def fn(data):
        for body in data["facts"].values():
            for concept in body.values():
                for unit, rows in concept["units"].items():
                    concept["units"][unit] = [x for x in rows if x.get("accn") != accession]
        return data
    return fn


def drop_feed_entries(feed_text: str, form: str) -> str:
    def keep(m):
        return "" if f"<filing-type>{form}</filing-type>" in m.group(0) else m.group(0)
    return re.sub(r"<entry>.*?</entry>\s*", keep, feed_text, flags=re.S)


class Routes:
    """The ``SecFixtures`` opener plus exact-URL answers for the SOXX series feed and its
    N-PORT document (``SecFixtures`` routes by the last path part only)."""

    def __init__(self, extra=None, drop=None):
        self.sec = SecFixtures(drop=drop)
        self.extra = dict(extra or {})
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if url in self.extra:
            return (200, {}, self.extra[url])
        return self.sec(url, headers, timeout)


def soxx_routes(*, feed=None, nport=None) -> dict:
    feed = fx("feed_soxx") if feed is None else feed
    doc = edgar.archive_url(SOXX_CIK, SOXX_ACC, "primary_doc.xml")
    out = {etf.feed_url(SOXX_SERIES, ""): feed.encode(), etf.feed_url(SOXX_SERIES, "NPORT-P"): feed.encode()}
    out[doc] = (fx("nport_soxx") if nport is None else nport).encode()
    return out


def nport_anet_for_mu() -> str:
    """The SOXX N-PORT with the Micron row's name and title VALUES set to Arista's, so ANET
    (whose captures exist) is the 8.54% look-through company."""
    x = fx("nport_soxx")
    old = "<name>Micron Technology, Inc.</name>\n        <lei>B3DXGBC8GAIYWI2Z0172</lei>\n        " \
          "<title>Micron Technology, Inc.</title>"
    assert x.count(old) == 1
    return x.replace(old, old.replace("Micron Technology, Inc.", "Arista Networks, Inc."))


ANET_10Q = "0001596532-26-000175"
ANET_8K = "0001596532-26-000174"
ANET_8K_OLD = "0001596532-26-000118"     # 8-K 5.07, 2026-06-02 (no capture of its body)
ANET_8K_Q1 = "0001596532-26-000074"      # 8-K 2.02, 2026-05-05 (no capture of its body)
CEG_10Q = "0001868275-26-000104"
CEG_8K = "0001868275-26-000097"


def anet_drop(extra=()):
    idx = [0, recent_index("sub_anet", ANET_10Q), recent_index("sub_anet", ANET_8K), *extra]
    return {"sub_anet": edit_recent(drop=idx), "cf_anet": drop_facts(ANET_10Q)}


def ceg_drop():
    idx = [recent_index("sub_ceg", CEG_10Q), recent_index("sub_ceg", CEG_8K)]
    return {"sub_ceg": edit_recent(drop=idx), "cf_ceg": drop_facts(CEG_10Q)}


def settings_of(root, led):
    from lib.desk import settings as desk_settings
    with led.connect() as conn:
        return desk_settings.effective(conn, root.cfg())


def run_sweep(root, led, *, drop=None, extra=None):
    op = Routes(extra=extra, drop=drop)
    out = events.sweep(led, settings=settings_of(root, led), seams=seams(opener=op))
    return out, op


def q(led, sql, *args):
    with led.connect() as conn:
        return [tuple(x) for x in conn.execute(sql, args).fetchall()]


def one(led, sql, *args):
    rows = q(led, sql, *args)
    return rows[0][0] if rows else None


def event_by_key(led, key):
    with led.connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE dedup_key=?", (key,)).fetchone()
        return dict(row) if row else None


def section_keys(led, accession):
    return sorted(x[0] for x in q(led, "SELECT section_key FROM documents WHERE accession=?", accession))


# --- cases ------------------------------------------------------------------------------

def case_dedup():
    clock = FakeClock(NOW).pin_market()
    try:
        with temp_root() as root:
            led = root.led()
            with led.connect() as conn:
                a = store.create_event(conn, dedup_key="filing:X-1", ticker="anet", type="filing",
                                       detail={"n": 1}, now=NOW)
                b = store.create_event(conn, dedup_key="filing:X-1", ticker="ANET", type="earnings",
                                       detail={"n": 2}, now=NOW)
            r.ok("dedup the first insert returns an id", isinstance(a, int), a)
            r.eq("dedup a repeated dedup_key returns None", b, None)
            r.eq("dedup one dedup_key -> one row", q(led, "SELECT COUNT(*), MIN(type), MIN(detail_json) FROM events"),
                 [(1, "filing", '{"n": 1}')])
    finally:
        clock.unpin()


def case_holding():
    clock = FakeClock(NOW).pin_market()
    try:
        with temp_root() as root:
            seed_desk(root, opener=Routes(drop=anet_drop()))
            led = root.led()
            r.eq("holding the seed lacks the 10-Q, the 8-K and the newest row",
                 one(led, "SELECT COUNT(*) FROM filings WHERE ticker='ANET'"), 997)
            r.eq("holding the seed has no fact of the 10-Q",
                 one(led, "SELECT COUNT(*) FROM xbrl_facts WHERE accession=?", ANET_10Q), 0)
            r.eq("holding the seed makes no event", one(led, "SELECT COUNT(*) FROM events"), 0)
            src0 = one(led, "SELECT source_accession FROM metrics WHERE ticker='ANET' ORDER BY as_of DESC LIMIT 1")
            clock.set("2026-10-28T14:10:00Z")
            out, op = run_sweep(root, led)
            r.eq("holding three new ANET filings", one(led, "SELECT COUNT(*) FROM filings WHERE ticker='ANET'"), 1000)
            tiers = dict(q(led, "SELECT accession, tier FROM filings WHERE accession IN (?,?,?)",
                           ANET_10Q, ANET_8K, fx("sub_anet")["filings"]["recent"]["accessionNumber"][0]))
            r.eq("holding tiers", (tiers.get(ANET_10Q), tiers.get(ANET_8K)), ("redecide", "redecide"))
            keys = section_keys(led, ANET_10Q)
            r.ok("holding the new 10-Q: sections", {"10-Q:I-1", "10-Q:I-2", "10-Q:II-1A"} <= set(keys), keys)
            r.ok("holding the new 10-Q: XBRL facts", one(led, "SELECT COUNT(*) FROM xbrl_facts WHERE accession=?",
                                                       ANET_10Q) > 0)
            r.eq("holding the new 10-Q: metrics recomputed from it",
                 (src0 != ANET_10Q, one(led, "SELECT source_accession FROM metrics WHERE ticker='ANET' "
                                             "ORDER BY as_of DESC LIMIT 1")), (True, ANET_10Q))
            e = event_by_key(led, f"filing:{ANET_10Q}")
            r.ok("holding then filing:<accession>", e is not None, out)
            if e:
                d = json.loads(e["detail_json"])
                r.eq("holding the 10-Q event", (e["type"], e["ticker"], e["status"], e["created_by"]),
                     ("filing", "ANET", "new", "monitor"))
                r.eq("holding the event names the filing", (d.get("accession"), d.get("form"), d.get("tier")),
                     (ANET_10Q, "10-Q", "redecide"))
                r.ok("holding the event lists the stored sections", set(keys) <= set(d.get("document_keys") or []),
                     d.get("document_keys"))
                ingested = one(led, "SELECT ingested_at FROM filings WHERE accession=?", ANET_10Q)
                r.ok("holding the sections were stored before the event", ingested and ingested <= e["detected_at"])
            k = event_by_key(led, f"filing:{ANET_8K}")
            r.eq("holding an 8-K 2.02 -> earnings", (k or {}).get("type"), "earnings")
            r.eq("holding the 8-K body + EX-99.1", section_keys(led, ANET_8K), ["8-K:EX-99.1", "8-K:body"])
            r.eq("holding two events", sorted(out["events"]), sorted(x[0] for x in q(led, "SELECT id FROM events")))
            r.eq("holding the event count", len(out["events"]), 2)
            r.eq("holding the newest Form 4 is the row only",
                 (tiers.get(fx("sub_anet")["filings"]["recent"]["accessionNumber"][0]),
                  section_keys(led, fx("sub_anet")["filings"]["recent"]["accessionNumber"][0])), ("ignore", []))
            # A second sweep: one dedup_key -> one row.
            out2, _ = run_sweep(root, led)
            r.eq("holding a second sweep adds no filing and no event", (out2["filings"], out2["events"]), (0, []))
            r.eq("holding one row per dedup_key", one(led, "SELECT COUNT(*) FROM events"), 2)
    finally:
        clock.unpin()


def case_lookthrough():
    clock = FakeClock(NOW).pin_market()
    try:
        with temp_root() as root:
            i118 = recent_index("sub_anet", ANET_8K_OLD)
            i074 = recent_index("sub_anet", ANET_8K_Q1)
            extra = soxx_routes(nport=nport_anet_for_mu())
            seed_desk(root, book=SOXX_BOOK, opener=Routes(extra=extra, drop=anet_drop(extra=[i118, i074])))
            led = root.led()
            with led.connect() as conn:
                comp = store.get_company(conn, "ANET")
            r.eq("lookthrough ANET is a look-through watch name", (comp or {}).get("status"), "watch")
            r.eq("lookthrough the reason", (comp or {}).get("status_reason"), "look-through: SOXX 8.5%")
            with led.connect() as conn:
                held = events.held_etf_weights(conn, "ANET")
            r.eq("lookthrough held ETF weights", held, [{"etf": "SOXX", "weight": 8.54}])
            # The sweep: the 10-Q is new; the 8-K -000174 says 5.02; the 8-K -000118 says 1.01.
            i8k = recent_index("sub_anet", ANET_8K)
            sweep_edit = {"sub_anet": edit_recent(values={(i8k, "items"): "5.02,9.01", (i118, "items"): "1.01"})}
            out, _ = run_sweep(root, led, drop=sweep_edit, extra=extra)
            e = event_by_key(led, f"lookthrough:{ANET_10Q}")
            r.ok("lookthrough a look-through 10-Q -> lookthrough:<accession>", e is not None, out)
            if e:
                d = json.loads(e["detail_json"])
                r.eq("lookthrough the event type", (e["type"], e["ticker"], e["status"]), ("lookthrough", "ANET", "new"))
                r.eq("lookthrough the detail lists each held ETF and its weight", d.get("etfs"),
                     [{"etf": "SOXX", "weight": 8.54}])
            r.ok("lookthrough the 10-Q was ingested", len(section_keys(led, ANET_10Q)) >= 8)
            r.eq("lookthrough no filing event for a look-through name",
                 event_by_key(led, f"filing:{ANET_10Q}"), None)
            r.eq("lookthrough its 8-K 5.02 -> none",
                 (event_by_key(led, f"lookthrough:{ANET_8K}"), event_by_key(led, f"filing:{ANET_8K}")), (None, None))
            r.eq("lookthrough the 8-K 5.02 is record", one(led, "SELECT tier FROM filings WHERE accession=?", ANET_8K),
                 "record")
            r.eq("lookthrough an 8-K 1.01 (redecide, not earnings) -> none",
                 (one(led, "SELECT tier FROM filings WHERE accession=?", ANET_8K_OLD),
                  event_by_key(led, f"lookthrough:{ANET_8K_OLD}")), ("redecide", None))
            e2 = event_by_key(led, f"lookthrough:{ANET_8K_Q1}")
            r.eq("lookthrough an earnings release -> lookthrough", ((e2 or {}).get("type"),
                 json.loads((e2 or {}).get("detail_json") or "{}").get("etfs")), ("lookthrough", [{"etf": "SOXX",
                                                                                                 "weight": 8.54}]))
            r.eq("lookthrough two events in all", sorted(x[0] for x in q(led, "SELECT dedup_key FROM events")),
                 sorted([f"lookthrough:{ANET_10Q}", f"lookthrough:{ANET_8K_Q1}"]))
    finally:
        clock.unpin()


def case_others():
    clock = FakeClock(NOW).pin_market()
    try:
        # 1. ANET is a watch name (not look-through); CEG is a holding.
        with temp_root() as root:
            seed_desk(root, opener=Routes(drop={**anet_drop(), **ceg_drop()}))
            led = root.led()
            with led.connect() as conn:
                store.set_company_status(conn, "ANET", "watch", "owner asked to watch it", NOW)
            i8k = recent_index("sub_ceg", CEG_8K)
            out, _ = run_sweep(root, led, drop={"sub_ceg": edit_recent(values={(i8k, "items"): "5.02,9.01"})})
            r.ok("others a watch redecide: ingest", len(section_keys(led, ANET_10Q)) >= 8
                 and section_keys(led, ANET_8K) == ["8-K:EX-99.1", "8-K:body"],
                 (section_keys(led, ANET_10Q), section_keys(led, ANET_8K)))
            r.eq("others a watch redecide: no event",
                 [x for x in q(led, "SELECT dedup_key FROM events WHERE ticker='ANET'")], [])
            r.ok("others a watch redecide: the close brief list names it",
                 {"ANET 10-Q", "ANET 8-K"} <= set(out["watch_filings"]), out["watch_filings"])
            r.eq("others the watch 10-Q still gets its XBRL", one(
                led, "SELECT COUNT(*) FROM xbrl_facts WHERE accession=?", ANET_10Q) > 0, True)
            first = fx("sub_anet")["filings"]["recent"]["accessionNumber"][0]
            r.eq("others ignore: the row only", (one(led, "SELECT tier FROM filings WHERE accession=?", first),
                                                 one(led, "SELECT ingested_at FROM filings WHERE accession=?", first),
                                                 section_keys(led, first)), ("ignore", None, []))
            r.eq("others record: the tier", one(led, "SELECT tier FROM filings WHERE accession=?", CEG_8K), "record")
            r.ok("others record: sections", {"8-K:body", "8-K:EX-99.1"} <= set(section_keys(led, CEG_8K)),
                 section_keys(led, CEG_8K))
            r.eq("others record: no event", (event_by_key(led, f"filing:{CEG_8K}"),), (None,))
            r.ok("others the holding CEG 10-Q makes its event", event_by_key(led, f"filing:{CEG_10Q}") is not None)
            r.eq("others one event in all", [x[0] for x in q(led, "SELECT dedup_key FROM events")],
                 [f"filing:{CEG_10Q}"])
        # 2. ANET is a candidate; CEG is dropped, then rejected: never swept.
        with temp_root() as root:
            seed_desk(root, opener=Routes(drop={**anet_drop(), **ceg_drop()}))
            led = root.led()
            with led.connect() as conn:
                store.set_company_status(conn, "ANET", "candidate", "owner asked", NOW)
                store.set_company_status(conn, "CEG", "dropped", "owner dropped it", NOW)
            n_ceg = one(led, "SELECT COUNT(*) FROM filings WHERE ticker='CEG'")
            out, op = run_sweep(root, led)
            r.ok("others a candidate redecide: ingest", len(section_keys(led, ANET_10Q)) >= 8)
            r.eq("others a candidate redecide: no event", one(led, "SELECT COUNT(*) FROM events"), 0)
            r.ok("others a candidate redecide: listed for the brief", "ANET 10-Q" in out["watch_filings"],
                 out["watch_filings"])
            ceg_sub = edgar.submissions_url("0001868275")
            r.eq("others dropped: not swept", (ceg_sub in op.calls, one(led, "SELECT COUNT(*) FROM filings WHERE "
                                                                         "ticker='CEG'")), (False, n_ceg))
            with led.connect() as conn:
                store.set_company_status(conn, "CEG", "rejected", "owner rejected it", NOW)
            out, op = run_sweep(root, led)
            r.eq("others rejected: not swept", (ceg_sub in op.calls, one(led, "SELECT COUNT(*) FROM filings WHERE "
                                                                          "ticker='CEG'")), (False, n_ceg))
            r.eq("others rejected: no event", one(led, "SELECT COUNT(*) FROM events"), 0)
        # 3. A new NPORT-P for a held ETF refreshes etf_holdings.
        with temp_root() as root:
            no_nport = drop_feed_entries(fx("feed_soxx"), "NPORT-P")
            seed_desk(root, book=SOXX_BOOK, opener=Routes(extra=soxx_routes(feed=no_nport)))
            led = root.led()
            r.eq("others the seed stored no N-PORT", one(led, "SELECT COUNT(*) FROM etf_holdings WHERE fund='SOXX' "
                                                          "AND source='nport'"), 0)
            r.eq("others SOXX is a held ETF", q(led, "SELECT kind, status, series_id FROM companies WHERE ticker='SOXX'"),
                 [("etf", "holding", SOXX_SERIES)])
            out, op = run_sweep(root, led, extra=soxx_routes())
            parsed = etf.parse_nport(fx("nport_soxx"))
            r.eq("others a new NPORT-P refreshes etf_holdings", out["nport"], ["SOXX"])
            r.eq("others the N-PORT rows", (one(led, "SELECT COUNT(*) FROM etf_holdings WHERE fund='SOXX' AND "
                                                "source='nport'"),
                                            one(led, "SELECT MAX(as_of) FROM etf_holdings WHERE fund='SOXX' AND "
                                                "source='nport'")), (len(parsed["holdings"]), parsed["as_of"]))
            out, op = run_sweep(root, led, extra=soxx_routes())
            r.eq("others the same NPORT-P again stores nothing", (out["nport"],
                 any(u.endswith("primary_doc.xml") for u in op.calls)), ([], False))
            r.eq("others an N-PORT makes no event", one(led, "SELECT COUNT(*) FROM events"), 0)
    finally:
        clock.unpin()


# --- the queue (§9.1 step 9) and the worker's transitions (G28) -------------------------

class FakeTransport:
    """The Telegram seam of the outbox: every send succeeds with a new message id."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        return {"ok": True, "result": {"message_id": 5000 + len(self.calls)}}


def make_worker(root, led, behavior: dict):
    sys.path.insert(0, str(REPO / "deploy" / "runner"))
    import run_worker  # noqa: E402
    run_worker.rc.TICK_LOG = str(root.path / "logs" / "tick.log")
    from lib.desk import outbox, tools
    transport = FakeTransport()
    box = outbox.Outbox(led, transport=transport, chat_id=OWNER, sleep=lambda s: None)
    cfg = root.cfg()
    seen = {}

    def ctx_factory(js):
        return tools.ToolContext(role="analyst", runtime=SimpleNamespace(root=str(root.path)), secrets=None,
                                 led=led, cfg=cfg, job=js, box=box, touch=lambda name: None,
                                 yf_price_fn=FakePrices())

    def job_runner(kind, prompt, js):
        act = behavior.get(js.event_id, "none")
        ctx = ctx_factory(js)
        if act == "fail":
            raise RuntimeError("the model call failed")
        if act in ("noted", "proposed"):
            tools.t_record_verdict(ctx, {"event_id": js.event_id, "verdict": "intact" if act == "noted" else "weakened",
                                         "summary": "The filing changes nothing." if act == "noted"
                                         else "Guidance fell; the buy level moves down."})
            with led.connect() as conn:
                seen[js.event_id] = conn.execute("SELECT status FROM events WHERE id=?", (js.event_id,)).fetchone()[0]
        if act == "proposed":
            tools.t_propose_change(ctx, {"rationale": "Move the ANET levels after the guidance cut.", "change": {
                "kind": "set_thesis", "ticker": "ANET", "premise_link": "AI networking is a bottleneck.",
                "summary": "Arista sells the back-end network of AI clusters.", "drivers": ["share"],
                "falsifiers": ["white box"], "evidence": [],
                "valuation": {"method": "price_levels", "reason": "guidance cut", "buy_below": 120.0,
                              "trim_above": 190.0}}})

    w = run_worker.Worker(runtime=SimpleNamespace(root=str(root.path), cfg=cfg, ledger_db=str(root.db)),
                          secrets=SimpleNamespace(telegram_bot_token="", owner_ids=[OWNER]), led=led, box=box,
                          job_runner=job_runner, classify_fn=lambda items: "", fetch_document=lambda *a, **k: None,
                          ctx_factory=ctx_factory)
    w.seen_status = seen
    return w, transport


def job_row(led, job_id):
    with led.connect() as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def case_queue():
    clock = FakeClock(NOW).pin_market()
    try:
        with temp_root() as root:
            seed_desk(root)
            led = root.led()
            # 8 new events + 1 drift, inserted out of time order (the queue sorts by detected_at).
            spec = [("filing", "2026-10-28T13:07:00Z"), ("earnings", "2026-10-28T13:02:00Z"),
                    ("news", "2026-10-28T13:05:00Z"), ("price_level", "2026-10-28T13:01:00Z"),
                    ("lookthrough", "2026-10-28T13:08:00Z"), ("filing", "2026-10-28T13:03:00Z"),
                    ("news", "2026-10-28T13:06:00Z"), ("earnings", "2026-10-28T13:04:00Z")]
            ids = {}
            with led.connect() as conn:
                drift = store.create_event(conn, dedup_key="drift:2026-10-27", ticker="BOOK", type="drift",
                                           detail={"legs": []}, now="2026-10-27T20:30:00Z")
                for i, (typ, at) in enumerate(spec):
                    # The two news events name two tickers: one ticker's news is one job
                    # (case_queue_news_group).
                    ticker = "TSM" if i == 6 else ("ANET" if i % 2 == 0 else "CEG")
                    ids[at] = store.create_event(conn, dedup_key=f"{typ}:{i}", ticker=ticker, type=typ,
                                                 detail={"i": i}, now=at)
            oldest = [ids[at] for at in sorted(ids)]
            n_jobs0 = one(led, "SELECT COUNT(*) FROM jobs")
            jobs = events.queue_analysis(led, now=NOW)
            r.eq("queue 8 new -> 6 jobs", len(jobs), 6)
            queued = [x[0] for x in q(led, "SELECT id FROM events WHERE status='queued'")]
            r.eq("queue the 6 oldest are queued", sorted(queued), sorted(oldest[:6]))
            r.eq("queue the 2 newest stay new", sorted(x[0] for x in q(
                led, "SELECT id FROM events WHERE status='new' AND type != 'drift'")), sorted(oldest[6:]))
            payloads = [json.loads(job_row(led, j)["payload_json"])["event_id"] for j in jobs]
            r.eq("queue one analyze_event job per event, oldest first", payloads, oldest[:6])
            r.eq("queue the jobs", q(led, "SELECT DISTINCT kind, status, requested_by FROM jobs WHERE id IN (%s)" %
                                     ",".join(str(j) for j in jobs)), [("analyze_event", "queued", "monitor")])
            r.eq("queue drift is never queued", one(led, "SELECT status FROM events WHERE id=?", drift), "new")
            jobs2 = events.queue_analysis(led, now=NOW)
            r.eq("queue the next run takes the last 2", [json.loads(job_row(led, j)["payload_json"])["event_id"]
                                                         for j in jobs2], oldest[6:])
            r.eq("queue a third run queues nothing (drift stays)", events.queue_analysis(led, now=NOW), [])
            r.eq("queue drift still new", one(led, "SELECT status FROM events WHERE id=?", drift), "new")
            r.eq("queue 8 analyze_event jobs in all", one(led, "SELECT COUNT(*) FROM jobs") - n_jobs0, 8)
            # G28: queued -> analyzed -> noted or proposed; a twice-failed job -> failed.
            ev_noted, ev_prop, ev_fail = oldest[0], oldest[1], oldest[2]
            w, transport = make_worker(root, led, {ev_noted: "noted", ev_prop: "proposed", ev_fail: "fail"})
            job_of = {json.loads(job_row(led, j)["payload_json"])["event_id"]: j for j in jobs}
            w.run_one(job_row(led, job_of[ev_noted]))
            e = q(led, "SELECT status, verdict, analysis IS NOT NULL, analyzed_at IS NOT NULL FROM events WHERE id=?",
                  ev_noted)
            r.eq("queue record_verdict moves queued -> analyzed", w.seen_status.get(ev_noted), "analyzed")
            r.eq("queue a verdict with no proposal -> analyzed -> noted", e, [("noted", "intact", 1, 1)])
            r.eq("queue the noted job is done", job_row(led, job_of[ev_noted])["status"], "done")
            w.run_one(job_row(led, job_of[ev_prop]))
            r.eq("queue the proposing job passed through analyzed", w.seen_status.get(ev_prop), "analyzed")
            r.eq("queue a verdict with a proposal -> proposed",
                 q(led, "SELECT status, verdict FROM events WHERE id=?", ev_prop), [("proposed", "weakened")])
            r.eq("queue the proposal exists", one(led, "SELECT COUNT(*) FROM proposals WHERE kind='change' AND "
                                                       "state='pending'"), 1)
            w.run_one(job_row(led, job_of[ev_fail]))
            r.eq("queue a first failure requeues the job", (job_row(led, job_of[ev_fail])["status"],
                                                            job_row(led, job_of[ev_fail])["attempts"],
                                                            one(led, "SELECT status FROM events WHERE id=?", ev_fail)),
                 ("queued", 1, "queued"))
            sends = len(transport.calls)
            w.run_one(job_row(led, job_of[ev_fail]))
            r.eq("queue a second failure -> failed", (job_row(led, job_of[ev_fail])["status"],
                                                      one(led, "SELECT status FROM events WHERE id=?", ev_fail)),
                 ("failed", "failed"))
            r.eq("queue the worker sends nothing on a failure", len(transport.calls) - sends, 0)
            fail_items = q(led, "SELECT kind, priority, status, title FROM agenda WHERE event_id=?", ev_fail)
            r.eq("queue the failure is one agenda item that comes up (next)",
                 [(k, p, s) for k, p, s, _t in fail_items], [("failure", 1, "waiting")])
            r.ok("queue the failure item says what failed", fail_items and fail_items[0][3].startswith("I could not"),
                 fail_items)
            # A finished event takes no new verdict.
            from lib.desk import tools
            ctx = w.make_ctx(None)
            for eid, st in ((ev_noted, "noted"), (ev_prop, "proposed"), (ev_fail, "failed"), (oldest[7], "queued")):
                if st == "queued":
                    continue
                r.raises(f"queue a {st} event takes no new verdict",
                         lambda eid=eid: tools.t_record_verdict(ctx, {"event_id": eid, "verdict": "broken",
                                                                     "summary": "x"}), tools.ToolError, "not queued")
            r.raises("queue a new event takes no verdict",
                     lambda: tools.t_record_verdict(ctx, {"event_id": drift, "verdict": "broken", "summary": "x"}),
                     tools.ToolError, "not queued")
    finally:
        clock.unpin()


def case_queue_news_group():
    """One ticker's new news events are one analyze_event job (three outlets, one story)."""
    clock = FakeClock(NOW).pin_market()
    try:
        with temp_root() as root:
            seed_desk(root)
            led = root.led()
            with led.connect() as conn:
                ceg = [store.create_event(conn, dedup_key=f"news:CEG:{i}", ticker="CEG", type="news",
                                          detail={"title": f"DOE loan story {i}", "document_id": None},
                                          now=f"2026-10-28T13:0{i}:00Z") for i in (1, 3, 5)]
                anet = store.create_event(conn, dedup_key="news:ANET:1", ticker="ANET", type="news",
                                          detail={"title": "ANET story"}, now="2026-10-28T13:02:00Z")
                filing = store.create_event(conn, dedup_key="filing:CEG:1", ticker="CEG", type="filing",
                                            detail={"form": "8-K"}, now="2026-10-28T13:04:00Z")
            jobs = events.queue_analysis(led, now=NOW)
            pays = [json.loads(job_row(led, j)["payload_json"]) for j in jobs]
            r.eq("group: 5 new events -> 3 jobs, oldest first", pays,
                 [{"event_id": ceg[0], "also": ceg[1:]}, {"event_id": anet}, {"event_id": filing}])
            r.eq("group: every event of the group is queued",
                 sorted(x[0] for x in q(led, "SELECT id FROM events WHERE status='queued'")),
                 sorted(ceg + [anet, filing]))
            lines = [x[0] for x in q(led, "SELECT text FROM chat_messages WHERE source='monitor' ORDER BY id")]
            r.ok("group: one desk line names the extra headlines",
                 any("2 more headlines on CEG go with it." in t for t in lines), lines)
            r.eq("group: a second run queues nothing", events.queue_analysis(led, now=NOW), [])
            # The worker: one verdict for the group; the others share it.
            w, transport = make_worker(root, led, {ceg[0]: "noted"})
            prompts = []
            inner = w.job_runner

            def runner(kind, prompt, js):
                prompts.append(prompt)
                return inner(kind, prompt, js)
            w.job_runner = runner
            w.run_one(job_row(led, jobs[0]))
            body = prompts[0] if prompts else ""
            r.ok("group: the prompt asks for ONE record_verdict", f"ONE record_verdict for event {ceg[0]}" in body,
                 body[:300])
            r.ok("group: the prompt lists the also_events", all(f'"id": {e}' in body for e in ceg[1:]), body[-600:])
            r.eq("group: all three events share the verdict",
                 q(led, "SELECT status, verdict, analysis IS NOT NULL FROM events WHERE id IN (%s) ORDER BY id"
                   % ",".join(map(str, ceg))), [("noted", "intact", 1)] * 3)
            r.eq("group: the ANET event is untouched", one(led, "SELECT status FROM events WHERE id=?", anet), "queued")
            # The next CEG event sees the group's one verdict once, not three copies.
            with led.connect() as conn:
                nxt = store.create_event(conn, dedup_key="news:CEG:next", ticker="CEG", type="news",
                                         detail={"title": "next"}, now="2026-10-28T15:00:00Z")
                ev = store.get_event(conn, nxt)
            body = json.loads(w.event_prompt(None, ev).split("```json\n", 1)[1].rsplit("\n```", 1)[0])
            r.eq("group: last_verdicts counts the group once", [v["id"] for v in body["last_verdicts"]], [ceg[0]])
            # The brief names the story once.
            from lib.desk import brief
            with led.connect() as conn:
                phrases = brief.build_model(conn, day="2026-10-28", now_dt=clock.now, earnings_fn=lambda t: None)["events"]
            r.eq("group: the brief names the CEG story once",
                 phrases.count("CEG had important news, and our reason to own it did not change"), 1)
            # A group whose job fails twice fails every event of the group.
            with led.connect() as conn:
                more = [store.create_event(conn, dedup_key=f"news:TSM:{i}", ticker="TSM", type="news",
                                           detail={"title": "TSM"}, now=f"2026-10-28T14:0{i}:00Z") for i in (1, 2)]
            jobs2 = events.queue_analysis(led, now=NOW)
            w2, _t = make_worker(root, led, {more[0]: "fail"})
            for _ in range(2):
                w2.run_one(job_row(led, jobs2[0]))
            r.eq("group: a failed group fails every event",
                 [x[0] for x in q(led, "SELECT status FROM events WHERE id IN (?, ?) ORDER BY id", *more)],
                 ["failed", "failed"])
    finally:
        clock.unpin()


CASES = [case_dedup, case_holding, case_lookthrough, case_others, case_queue, case_queue_news_group]

if __name__ == "__main__":
    for case in CASES:
        try:
            case()
        except Exception as e:  # noqa: BLE001 — one broken case must not hide the others
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    raise SystemExit(r.done())
