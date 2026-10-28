"""test_desk_findings: the §6.3 exact-quote check on real ``10q_q2`` sections.

The sections come from ``lib.desk.sections`` (``html_to_text`` + ``split_sections``) and go
into the ledger through ``store.store_document``, as the ingest does. Every quote is real
text from the fixture; a test only retypes it (quotes, dashes, spaces) or changes one word.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import Results, fx, temp_root  # noqa: E402

from lib.desk import findings, sections, store  # noqa: E402
from lib.desk.findings import FindingError, normalize, record_finding  # noqa: E402

r = Results("desk-findings")
NOW = "2026-08-05T20:30:00Z"
ACC = "0001596532-26-000175"
SECS = {k: (title, text) for k, title, text in sections.split_sections(sections.html_to_text(fx("10q_q2")), "10-Q")}

CENTERS = ("Our “Centers of Data” strategy is a fundamental pivot from legacy, siloed networking to a "
           "unified, data-driven approach in which the network is a service that interconnects four "
           "primary domains")
GROSS = ("Gross margin decreased to 62.9% and 62.4% for the three and six months ended June 30, 2026, "
         "compared to 65.2% and 64.5% for the same periods in 2025.")
DASH = "Preferred stock, $0.0001 par value—100 shares authorized and no shares issued and outstanding"
PAGE = "within the memory and silicon markets and reduce overall lead times"


def raw_text() -> str:
    """The fixture as plain BeautifulSoup text, page lines kept (the control for G3)."""
    import warnings
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        return BeautifulSoup(fx("10q_q2"), "lxml").get_text("\n")


def store_sections(conn, keys=("10-Q:I-1", "10-Q:I-2")) -> dict:
    out = {}
    for key in keys:
        title, text = SECS[key]
        out[key] = store.store_document(conn, kind="filing_section", title=title, text=text, now=NOW,
                                        ticker="ANET", accession=ACC, section_key=key)
    return out


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]


def add(conn, doc_id, quote, kind="margin", claim="A claim."):
    return record_finding(conn, ticker="ANET", kind=kind, claim=claim, quote=quote, document_id=doc_id,
                          created_by="desk", now=NOW)


r.ok("setup: the sections hold MD&A and the statements", {"10-Q:I-1", "10-Q:I-2"} <= set(SECS), list(SECS))

# --- exact -------------------------------------------------------------------------
with temp_root() as root:
    with root.led().connect() as c:
        docs = store_sections(c)
        md = docs["10-Q:I-2"]
        fid = add(c, md, GROSS, kind="margin", claim="Gross margin fell to 62.9% in Q2-2026.")
        row = findings.get_finding(c, fid)
        norm_doc = normalize(store.get_document(c, md)["text"])
        r.eq("exact: the quote_start is the offset in the normalized text", row["quote_start"],
             norm_doc.find(normalize(GROSS)))
        r.eq("exact: the stored quote sits at quote_start",
             norm_doc[row["quote_start"]:row["quote_start"] + len(row["quote"])], row["quote"])
        r.eq("exact: the row fields", (row["ticker"], row["kind"], row["document_id"], row["created_by"],
                                       row["created_at"], row["event_id"]),
             ("ANET", "margin", md, "desk", NOW, None))
        ev = add(c, md, CENTERS, kind="other")
        r.ok("exact: a second finding gets its own id", ev != fid and count(c) == 2, (ev, fid))
        fid3 = record_finding(c, ticker="anet", kind="growth", claim="x", quote=GROSS, document_id=md,
                              created_by="analyst", now=NOW, event_id=7)
        row3 = findings.get_finding(c, fid3)
        r.eq("exact: the ticker is upper case, the event id is kept", (row3["ticker"], row3["event_id"]), ("ANET", 7))

# --- normalize ---------------------------------------------------------------------
with temp_root() as root:
    with root.led().connect() as c:
        docs = store_sections(c)
        md, stmts = docs["10-Q:I-2"], docs["10-Q:I-1"]
        r.ok("normalize: the source has curly quotes", "“Centers of Data”" in SECS["10-Q:I-2"][1])
        typed = ('Our "Centers of Data"   strategy is a fundamental\npivot from legacy, siloed networking '
                 'to a  unified, data-driven approach in which the network is a service that interconnects '
                 'four primary domains')
        fid = add(c, md, typed, kind="other")
        r.ok("normalize: straight quotes, extra spaces and a line break are found", fid > 0)
        r.ok("normalize: the source has an em dash", "par value—100 shares" in SECS["10-Q:I-1"][1])
        fid = add(c, stmts, DASH.replace("—", "-"), kind="capital")
        r.ok("normalize: a hyphen for the dash is found", fid > 0)
        fid = add(c, stmts, DASH.replace("—", "–"), kind="capital")
        r.ok("normalize: an en dash for the dash is found", fid > 0)
        wide = GROSS.replace("62.9%", "６２.９%").replace("% and 62.4%", "% and 62.4%")
        r.ok("normalize: the typed quote holds a full-width digit and a no-break space",
             "６" in wide and " " in wide)
        fid = add(c, md, wide)
        r.ok("normalize: they match after NFKC", fid > 0)
        r.eq("normalize: the rules", normalize("“a”  ‘b’—c–d\n\te １"), '"a" \'b\'-c-d e 1')

# --- refuse ------------------------------------------------------------------------
with temp_root() as root:
    with root.led().connect() as c:
        docs = store_sections(c)
        md = docs["10-Q:I-2"]
        changed = GROSS.replace("decreased", "increased")
        e = r.raises("refuse: one changed word", lambda: add(c, md, changed), FindingError,
                     f"quote not found in document {md}; copy it exactly")
        r.eq("refuse: the exact message", str(e), f"quote not found in document {md}; copy it exactly")
        r.eq("refuse: no row is stored", count(c), 0)
        text = normalize(store.get_document(c, md)["text"])
        start = text.find(normalize(GROSS))
        while any(text[start + n - 1] == " " for n in (19, 20, 1200, 1201)):
            start += 1
        for n, good in ((19, False), (20, True), (1200, True), (1201, False)):
            q = text[start:start + n]
            ok = q == q.strip() and len(q) == n
            r.ok(f"refuse: the {n}-char quote is a real slice with no edge space", ok, repr(q[-10:]))
            if good:
                r.ok(f"refuse: {n} chars pass", add(c, md, q) > 0)
            else:
                r.raises(f"refuse: {n} chars refused", lambda: add(c, md, q), FindingError, "characters")
        r.eq("refuse: only the 20 and 1,200 char quotes are stored", count(c), 2)
        r.raises("refuse: an unknown kind", lambda: add(c, md, GROSS, kind="valuation"), FindingError, "kind")
        r.raises("refuse: an unknown author",
                 lambda: record_finding(c, ticker="ANET", kind="margin", claim="x", quote=GROSS, document_id=md,
                                        created_by="monitor", now=NOW), FindingError, "created_by")
        r.raises("refuse: a missing document", lambda: add(c, 999, GROSS), FindingError, "document 999")
        r.raises("refuse: an empty claim", lambda: add(c, md, GROSS, claim=" "), FindingError, "claim")
        r.eq("refuse: still only 2 rows", count(c), 2)

# --- page_break (G3) ---------------------------------------------------------------
with temp_root() as root:
    with root.led().connect() as c:
        docs = store_sections(c)
        md = docs["10-Q:I-2"]
        raw = normalize(raw_text())
        r.ok("page_break: in the source a page number and 'Table of Contents' split the phrase",
             "within the memory and silicon markets 19 Table of Contents and reduce overall lead times" in raw)
        quote = "to navigate the tightening supply " + PAGE + ", which will increase our working capital requirements"
        fid = add(c, md, quote, kind="risk", claim="ANET raises purchase commitments to cut lead times.")
        row = findings.get_finding(c, fid)
        r.ok("page_break: the quote across the page break is found in the MD&A", fid > 0 and row["quote_start"] > 0)

# --- fts ---------------------------------------------------------------------------
with temp_root() as root:
    with root.led().connect() as c:
        docs = store_sections(c)
        fid = add(c, docs["10-Q:I-2"], CENTERS, kind="other", claim="Arista sells a Centers of Data strategy.")
        add(c, docs["10-Q:I-2"], GROSS, kind="margin", claim="Gross margin fell.")
        hits = store.search(c, "Centers of Data", kinds=["finding"])
        r.eq("fts: the finding is found with kinds=[finding]", [h["id"] for h in hits][:1], [fid])
        r.ok("fts: only findings come back", all(h["kind"] == "finding" for h in hits), hits)
        r.eq("fts: the ticker filter keeps it", [h["id"] for h in store.search(c, "Centers", kinds=["finding"],
                                                                            ticker="ANET")], [fid])

raise SystemExit(r.done())
