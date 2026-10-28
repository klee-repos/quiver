"""Transcripts (plan §7.6): the Alpha Vantage request, storage, budget, and quarter choice.

Fixtures: ``av_anet`` (ANET 2026Q2), ``av_empty`` (2026Q4, an empty ``transcript``),
``av_err`` (``apikey=invalid``: Alpha Vantage still sends the full transcript), ``sub_anet``
and ``cf_anet`` for the quarter choice. No network: every call goes to ``FakeOpener``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from desk_fakes import FakeOpener, Results, fx, temp_root

from lib.desk import edgar, store, transcripts, xbrl

r = Results("desk-transcripts")
KEY = "AVKEY-TEST-7Q3Z"
AV = "https://www.alphavantage.co/query"
NOW = "2026-08-05T14:00:00Z"
ANET_CIK = "0001596532"
ACC_8K = "0001596532-26-000174"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


CAP = _Capture()
logging.getLogger().addHandler(CAP)
logging.getLogger().setLevel(logging.DEBUG)


def _opener(fid=None, status=200):
    op = FakeOpener()
    op.add(AV, status, fx(fid) if fid else b"")
    return op


def _no_key(label, obj):
    r.ok(label, KEY not in json.dumps(obj, default=str), obj)


# --- request ---------------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    op = _opener("av_anet")
    res = transcripts.fetch_transcript(conn, "ANET", "2026Q2", api_key=KEY, now=NOW, opener=op)
    url = op.calls[0][0]
    r.ok("request: the URL holds function, symbol and quarter",
         "function=EARNINGS_CALL_TRANSCRIPT&symbol=ANET&quarter=2026Q2" in url, url)
    r.ok("request: the URL starts with the Alpha Vantage query endpoint", url.startswith(AV + "?"), url)
    r.ok("request: the key goes in the URL", f"apikey={KEY}" in url, url)
    _no_key("request: the key is in no return value", res)
    doc = store.get_document(conn, res["document_id"])
    r.ok("request: the stored URL holds no key", KEY not in (doc["url"] or "") and "apikey" not in doc["url"],
         doc["url"])

    # An opener that raises with the full URL in its text: the error text holds no key.
    def _boom(u, h, t):
        raise OSError(f"connection reset while fetching {u}")
    res2 = transcripts.fetch_transcript(conn, "ANET", "2026Q3", api_key=KEY, now=NOW, opener=_boom)
    r.eq("request: a raised error gives state error", res2["state"], "error")
    _no_key("request: the error text holds no key", res2)
    r.ok("request: the error text keeps the cause", "connection reset" in res2.get("error", ""), res2)

    # No key: no call, no budget unit.
    op0 = _opener("av_anet")
    res3 = transcripts.fetch_transcript(conn, "ANET", "2026Q1", api_key="", now=NOW, opener=op0)
    r.eq("request: no key -> no_key", res3["state"], "no_key")
    r.eq("request: no key -> no call", len(op0.calls), 0)

# --- store -----------------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    op = _opener("av_anet")
    res = transcripts.fetch_transcript(conn, "anet", "2026Q2", api_key=KEY, now=NOW, opener=op)
    r.eq("store: state stored", res["state"], "stored")
    docs = conn.execute("SELECT * FROM documents WHERE kind='transcript'").fetchall()
    r.eq("store: one transcript document", len(docs), 1)
    doc = dict(docs[0])
    lines = doc["text"].split("\n")
    r.eq("store: 59 lines", len(lines), 59)
    r.ok("store: every line is 'Speaker (Title): content'",
         all(" (" in ln.split("): ", 1)[0] and "): " in ln for ln in lines), lines[:2])
    r.ok("store: line 0 is the operator", lines[0].startswith("Operator (Operator): Welcome to the Second"), lines[0][:60])
    r.ok("store: line 1 is Rudolph Araujo",
         lines[1].startswith("Rudolph Araujo (Head of Investor Advocacy): Thank you, Regina."), lines[1][:90])
    r.eq("store: char_len 43,940", doc["char_len"], 43940)
    r.eq("store: ticker ANET", doc["ticker"], "ANET")
    row = conn.execute("SELECT * FROM transcripts WHERE ticker='ANET' AND quarter='2026Q2'").fetchone()
    r.ok("store: a transcripts row points to the document", row and row["document_id"] == doc["id"], row)
    again = transcripts.fetch_transcript(conn, "ANET", "2026Q2", api_key=KEY, now=NOW, opener=op)
    r.eq("store: a stored quarter returns its document", again.get("document_id"), doc["id"])
    r.eq("store: a stored quarter sends no second call", len(op.calls), 1)
    r.eq("store: one budget unit used", store.budget_used(conn, "alphavantage", "2026-08-05"), 1)
    hits = store.search(conn, "Centers of Data", ticker="ANET", kinds=["document"])
    r.ok("store: the transcript is in the FTS index", any(h["id"] == doc["id"] for h in hits), hits[:2])

# --- empty_error -----------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    op = _opener("av_empty")
    res = transcripts.fetch_transcript(conn, "ANET", "2026Q4", api_key=KEY, now=NOW, opener=op)
    r.eq("empty_error: av_empty -> empty", res["state"], "empty")
    r.eq("empty_error: av_empty stores no document",
         conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
    r.eq("empty_error: av_empty stores no transcripts row",
         conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 0)
    r.eq("empty_error: av_empty counts one budget unit", store.budget_used(conn, "alphavantage", "2026-08-05"), 1)

    op500 = _opener(None, status=500)
    res = transcripts.fetch_transcript(conn, "ANET", "2026Q4", api_key=KEY, now=NOW, opener=op500)
    r.eq("empty_error: HTTP 500 -> error", res["state"], "error")
    r.eq("empty_error: HTTP 500 stores nothing", conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
    r.eq("empty_error: HTTP 500 counts one budget unit", store.budget_used(conn, "alphavantage", "2026-08-05"), 2)
    _no_key("empty_error: the error result holds no key", res)

    # Ground truth: av_err (apikey=invalid) is a full transcript, so it is stored.
    oper = _opener("av_err")
    res = transcripts.fetch_transcript(conn, "ANET", "2026Q2", api_key="invalid", now=NOW, opener=oper)
    r.eq("empty_error: av_err (key not checked) is stored", res["state"], "stored")
    r.eq("empty_error: av_err counts one budget unit", store.budget_used(conn, "alphavantage", "2026-08-05"), 3)

# The 7-day retry of an earnings event: av_empty each run, then nothing after day 7.
with temp_root() as root, root.led().connect() as conn:
    store.upsert_company(conn, ticker="ANET", kind="company", status="holding", now=NOW, cik=ANET_CIK)
    filing = next(f for f in edgar.recent_filings(fx("sub_anet")) if f["accession"] == ACC_8K)
    edgar.insert_filing(conn, filing=filing, cik=ANET_CIK, ticker="ANET", tier="redecide", now=NOW)
    t0 = datetime(2026, 8, 4, 21, 40, tzinfo=timezone.utc)
    eid = store.create_event(conn, dedup_key=f"filing:{ACC_8K}", ticker="ANET", type="earnings",
                             detail={"accession": ACC_8K, "form": "8-K"}, now=store.iso(t0))
    r.ok("empty_error: the earnings event exists", eid, eid)
    op = _opener("av_empty")
    per_run = []
    for day in range(0, 9):
        before = len(op.calls)
        out = transcripts.try_pending(conn, api_key=KEY, now=t0 + timedelta(days=day, minutes=60), opener=op)
        per_run.append(len(op.calls) - before)
        if day == 0:
            r.eq("empty_error: try_pending reports the empty state", [o["state"] for o in out], ["empty"])
            r.eq("empty_error: try_pending names the event", out[0]["event_id"], eid)
    r.eq("empty_error: one call per run for 7 days, then none", per_run, [1, 1, 1, 1, 1, 1, 1, 0, 0])
    r.eq("empty_error: nothing stored", conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0], 0)
    # Two runs in one day: each run tries once.
    op2 = _opener("av_empty")
    for _ in range(2):
        transcripts.try_pending(conn, api_key=KEY, now=t0 + timedelta(days=1, hours=5), opener=op2)
    r.eq("empty_error: each run tries once", len(op2.calls), 2)
    # When the transcript comes out, the next run stores it and the run after sends nothing.
    op3 = _opener("av_anet")
    out = transcripts.try_pending(conn, api_key=KEY, now=t0 + timedelta(days=2, hours=5), opener=op3)
    r.eq("empty_error: the run after release stores it", [o["state"] for o in out], ["stored"])
    out = transcripts.try_pending(conn, api_key=KEY, now=t0 + timedelta(days=2, hours=6), opener=op3)
    r.eq("empty_error: a stored transcript is not tried again", (out, len(op3.calls)), ([], 1))

# --- budget ----------------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    op = _opener("av_empty")
    states = []
    for i in range(26):
        res = transcripts.fetch_transcript(conn, "ANET", "2026Q4", api_key=KEY, now=NOW, opener=op)
        states.append(res["state"])
    r.eq("budget: calls 1-25 go out", len(op.calls), 25)
    r.eq("budget: calls 1-25 are sent", states[:25], ["empty"] * 25)
    r.eq("budget: call 26 is no_budget", states[25], "no_budget")
    r.eq("budget: 25 units used", store.budget_used(conn, "alphavantage", "2026-08-05"), 25)
    res = transcripts.fetch_transcript(conn, "ANET", "2026Q4", api_key=KEY, now="2026-08-06T14:00:00Z",
                                       opener=op)
    r.eq("budget: the next day resets", (res["state"], len(op.calls)), ("empty", 26))
    r.eq("budget: the next day counts from 1", store.budget_used(conn, "alphavantage", "2026-08-06"), 1)
    # The day is the ET date: 03:00 UTC on 08-07 is still 08-06 in New York.
    transcripts.fetch_transcript(conn, "ANET", "2026Q4", api_key=KEY, now="2026-08-07T03:00:00Z", opener=op)
    r.eq("budget: the day is the ET date", store.budget_used(conn, "alphavantage", "2026-08-06"), 2)

# --- quarter ---------------------------------------------------------------------
with temp_root() as root, root.led().connect() as conn:
    r.eq("quarter: calendar quarter before 2026-08-04", transcripts.calendar_quarter_before("2026-08-04"), "2026Q2")
    r.eq("quarter: calendar quarter before 2026-07-01", transcripts.calendar_quarter_before("2026-07-01"), "2026Q2")
    r.eq("quarter: calendar quarter before 2026-06-30", transcripts.calendar_quarter_before("2026-06-30"), "2026Q1")
    r.eq("quarter: calendar quarter before 2026-02-17", transcripts.calendar_quarter_before("2026-02-17"), "2025Q4")
    filing = next(f for f in edgar.recent_filings(fx("sub_anet")) if f["accession"] == ACC_8K)
    r.eq("quarter: 8-K -000174 report_date", filing["report_date"], "2026-08-04")
    r.eq("quarter: no XBRL facts -> the calendar quarter (G4)",
         transcripts.quarter_for(conn, cik=ANET_CIK, report_date=filing["report_date"]), ["2026Q2"])

    # The facts known on the 8-K date: drop the entries filed after it. The Q2 10-Q
    # (-000175, filed 2026-08-05) is not out yet, and the Q1 10-Q period is too old.
    cf_then = fx("cf_anet")
    for tax in cf_then["facts"].values():
        for body in tax.values():
            for unit, facts in body["units"].items():
                body["units"][unit] = [f for f in facts if f.get("filed", "") <= "2026-08-04"]
    xbrl.upsert_companyfacts(conn, cf_then)
    r.eq("quarter: facts of the 8-K date -> the calendar quarter",
         transcripts.quarter_for(conn, cik=ANET_CIK, report_date="2026-08-04"), ["2026Q2"])

    # A Q1 10-Q dei cover date (2026-04-30) is within 100 days of 08-04: it must not count.
    r.eq("quarter: a dei cover date does not count",
         transcripts.fiscal_quarter(conn, cik=ANET_CIK, report_date="2026-08-04"), None)

with temp_root() as root, root.led().connect() as conn:
    xbrl.upsert_companyfacts(conn, fx("cf_anet"))
    r.eq("quarter: the 10-Q fy/fp gives the fiscal quarter",
         transcripts.fiscal_quarter(conn, cik=ANET_CIK, report_date="2026-08-04"), "2026Q2")
    r.eq("quarter: fiscal and calendar agree for ANET",
         transcripts.quarter_for(conn, cik=ANET_CIK, report_date="2026-08-04"), ["2026Q2"])
    r.eq("quarter: the Q1 release maps to the Q1 10-Q",
         transcripts.quarter_for(conn, cik=ANET_CIK, report_date="2026-05-05"), ["2026Q1"])
    r.eq("quarter: a 10-K maps fp FY to Q4",
         transcripts.fiscal_quarter(conn, cik=ANET_CIK, report_date="2026-02-17"), "2025Q4")

with temp_root() as root, root.led().connect() as conn:
    # A fiscal year that is not the calendar year: change the fy VALUE of the Q2 10-Q facts.
    cf = fx("cf_anet")
    for tax in cf["facts"].values():
        for body in tax.values():
            for facts in body["units"].values():
                for f in facts:
                    if f.get("accn") == "0001596532-26-000175":
                        f["fy"] = 2027
    xbrl.upsert_companyfacts(conn, cf)
    r.eq("quarter: the fiscal quarter comes first",
         transcripts.quarter_for(conn, cik=ANET_CIK, report_date="2026-08-04"), ["2027Q2", "2026Q2"])
    store.upsert_company(conn, ticker="ANET", kind="company", status="holding", now=NOW, cik=ANET_CIK)
    filing = next(f for f in edgar.recent_filings(fx("sub_anet")) if f["accession"] == ACC_8K)
    edgar.insert_filing(conn, filing=filing, cik=ANET_CIK, ticker="ANET", tier="redecide", now=NOW)
    store.create_event(conn, dedup_key=f"filing:{ACC_8K}", ticker="ANET", type="earnings",
                       detail={"accession": ACC_8K}, now="2026-08-05T13:00:00Z")
    op = _opener("av_empty")
    transcripts.try_pending(conn, api_key=KEY, now=NOW, opener=op)
    r.ok("quarter: try_pending asks for the fiscal quarter first",
         len(op.calls) == 1 and "quarter=2027Q2" in op.calls[0][0], op.calls)

# --- the key in log lines ----------------------------------------------------------
logging.getLogger().removeHandler(CAP)
r.ok("request: the module logged lines", any("transcript" in ln for ln in CAP.lines), CAP.lines[:3])
r.ok("request: the key is in no log line", not [ln for ln in CAP.lines if KEY in ln],
     [ln for ln in CAP.lines if KEY in ln][:2])

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


# --- gate B D2: the Alpha Vantage call runs with the write lock free ----------------
with temp_root() as root, root.led().connect() as conn:
    base_op = _opener("av_anet")
    free = []

    def locked_opener(url, headers, timeout):
        free.append(_lock_free(root.db))
        return base_op(url, headers, timeout)
    transcripts.fetch_transcript(conn, "ANET", "2026Q2", api_key=KEY, now=NOW, opener=locked_opener)
    r.eq("lock: the transcript call runs after the budget row is committed", free, [True])

raise SystemExit(r.done())
