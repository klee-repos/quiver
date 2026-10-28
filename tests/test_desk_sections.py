"""lib/desk/sections.py on the real documents (plan §7.4, §18.9 G2 G3).

Run: .venv/bin/python tests/test_desk_sections.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import FakeClock, FakeOpener, Results, fx, fx_bytes, temp_root  # noqa: E402

from lib.desk import edgar, sections  # noqa: E402

r = Results("desk-sections")
UA = "Quiver test@example.com"
ANET = "0001596532"
ACC_8K, ACC_Q2, ACC_Q1 = "0001596532-26-000174", "0001596532-26-000175", "0001596532-26-000078"
ACC_10K = "0001596532-26-000013"
NOW = "2026-08-05T14:00:00Z"

_TEXT: dict = {}


def text_of(fid: str) -> str:
    if fid not in _TEXT:
        _TEXT[fid] = sections.html_to_text(fx(fid))
    return _TEXT[fid]


def secs(fid: str, form: str) -> list:
    return sections.split_sections(text_of(fid), form)


def anet_filing(accession: str) -> dict:
    return [f for f in edgar.recent_filings(fx("sub_anet")) if f["accession"] == accession][0]


def client(op: FakeOpener) -> edgar.EdgarClient:
    fc = FakeClock()
    return edgar.EdgarClient(UA, opener=op, sleep=fc.sleep, clock=fc)


def serve(op: FakeOpener, accession: str, fid: str) -> dict:
    """Route the primary document of a real ANET filing to its captured body (gzip, as SEC sends)."""
    f = anet_filing(accession)
    op.add(edgar.archive_url(ANET, accession, f["primary_doc"]), 200, fx_bytes(fid), exact=True, gzip_body=True)
    return f


def ingest(conn, cl, f: dict, ticker: str = "ANET") -> dict:
    edgar.insert_filing(conn, filing=f, cik=ANET, ticker=ticker, tier="baseline", now=NOW)
    return sections.ingest_filing(conn, cl, ticker=ticker, cik=ANET, accession=f["accession"],
                                  form=f["form"], primary_doc=f["primary_doc"], now=NOW)


def doc_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]


def case_to_text():
    raw = fx("10q_q2")
    text = text_of("10q_q2")
    r.ok("to_text the raw file has a script tag", "<script" in raw)
    r.ok("to_text no script source", "ynyAVAgEmeE" not in text)
    r.ok("to_text the raw ix:header holds iso4217:USD", "<ix:header" in raw and "iso4217:USD" in raw)
    r.ok("to_text no ix:header text", "iso4217:USD" not in text and "xbrli" not in text)
    r.ok("to_text no style text", "font-family" not in text)
    lines = text.split("\n")
    r.ok("to_text a balance-sheet row is tab-separated",
         "Cash and cash equivalents\t$\t2,290.2\t$\t1,963.9" in lines)
    r.ok("to_text a revenue row is tab-separated",
         "Total revenue\t$\t3,035.7\t$\t2,204.8\t$\t5,744.7\t$\t4,209.6" in lines)
    r.ok("to_text no empty line", all(line.strip() for line in lines))
    r.ok("to_text a plain-text body keeps its lines",
         sections.html_to_text("Item 1. Business\nWe sell switches.\n") == "Item 1. Business\nWe sell switches.")


def case_page_lines():
    pn = re.compile(r"(?:[A-Z]{1,2}-)?\d{1,3}|[ivxlc]{1,6}")
    for fid in ("10k", "10q_q2"):
        raw = fx(fid)
        r.ok(f"page_lines {fid} raw has Table of Contents links", raw.count("Table of Contents") > 10)
        lines = text_of(fid).split("\n")
        r.eq(f"page_lines {fid} no page-number line (G3)", [x for x in lines if pn.fullmatch(x)], [])
        r.eq(f"page_lines {fid} no Table of Contents line (G3)",
             [x for x in lines if x.strip().lower() == "table of contents"], [])
    r.ok("page_lines the 10-K body ends with its signatures, not page 103",
         text_of("10k").rstrip().endswith("Yvonne Wassenaar"))


def case_keys_10k():
    out = secs("10k", "10-K")
    keys = [k for k, _t, _x in out]
    want = ["10-K:" + k for k in ("1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8", "9", "9A",
                                  "9B", "9C", "10", "11", "12", "13", "14", "15", "16")]
    r.eq("keys_10k every key once, in order", keys, want)
    d = {k: x for k, _t, x in out}
    r.ok("keys_10k 1A starts with its heading", d["10-K:1A"].startswith("Item 1A. Risk Factors"), d["10-K:1A"][:40])
    r.ok("keys_10k 1A > 150,000 chars", len(d["10-K:1A"]) > 150_000, len(d["10-K:1A"]))
    r.ok("keys_10k 7 > 35,000 chars", len(d["10-K:7"]) > 35_000, len(d["10-K:7"]))
    text = text_of("10k")
    toc16 = text.find("Item 16.\tForm 10-K Summary\t102")
    part1 = [m.start() for m in re.finditer(r"(?m)^PART I$", text)]
    r.eq("keys_10k two PART I lines (TOC + body)", len(part1), 2)
    r.ok("keys_10k the TOC Item 16 is before the body start", 0 <= toc16 < part1[1], (toc16, part1))
    r.ok("keys_10k about 8,000 chars follow the TOC Item 16", 7_000 < part1[1] - toc16 < 9_000, part1[1] - toc16)
    r.ok("keys_10k 10-K:16 is the body heading (G2)", d["10-K:16"].startswith("Item 16. Form 10-K Summary\nNone."),
         d["10-K:16"][:60])


def case_keys_10q():
    out = secs("10q_q2", "10-Q")
    keys = [k for k, _t, _x in out]
    want = ["10-Q:I-1", "10-Q:I-2", "10-Q:I-3", "10-Q:I-4", "10-Q:II-1", "10-Q:II-1A", "10-Q:II-2",
            "10-Q:II-3", "10-Q:II-4", "10-Q:II-5", "10-Q:II-6"]
    r.eq("keys_10q every key once, in order", keys, want)
    d = {k: x for k, _t, x in out}
    text = text_of("10q_q2")
    toc1 = text.find("Item 1.\tFinancial Statements (Unaudited)\t1")
    toc2 = text.find("Item 2.\tManagement")
    r.ok("keys_10q the TOC Item 1 row exists", 0 <= toc1 < toc2, (toc1, toc2))
    r.ok("keys_10q a short gap follows the TOC Item 1", 500 < toc2 - toc1 < 800, toc2 - toc1)
    r.ok("keys_10q I-1 is the body (no extra I-1)",
         d["10-Q:I-1"].startswith("Item 1. Financial Statements (Unaudited)\nARISTA NETWORKS, INC."), d["10-Q:I-1"][:80])
    ceg = [k for k, _t, _x in secs("10q_ceg", "10-Q")]
    r.ok("keys_10q CEG prints Item 4 Controls after Part II: it stays I-4", "10-Q:I-4" in ceg and
         ceg.count("10-Q:II-4") == 1, ceg)


def case_short():
    k = {key: x for key, _t, x in secs("10k", "10-K")}
    q = {key: x for key, _t, x in secs("10q_q2", "10-Q")}
    r.eq("short 10-K:1B stays a section", k["10-K:1B"], "Item 1B. Unresolved Staff Comments\nNot applicable.")
    r.eq("short 10-K:6 [Reserved] stays a section", k["10-K:6"], "Item 6. [Reserved]")
    r.eq("short 10-Q:II-3 stays a section", q["10-Q:II-3"], "Item 3. Defaults Upon Senior Securities\nNot applicable.")
    r.ok("short sizes", (len(k["10-K:1B"]), len(q["10-Q:II-3"])) == (50, 55), (len(k["10-K:1B"]), len(q["10-Q:II-3"])))


def case_exhibits():
    op = FakeOpener()
    f = serve(op, ACC_8K, "body_anet")
    op.add(edgar.index_url(ANET, ACC_8K), 200, fx_bytes("idx_anet"), exact=True)
    op.add("https://www.sec.gov/Archives/edgar/data/1596532/000159653226000174/ex991q226-earningsrelease.htm",
           200, fx_bytes("ex991"), exact=True, gzip_body=True)
    with temp_root() as root:
        with root.led().connect() as conn:
            res = ingest(conn, client(op), f)
            r.ok("exhibits ingest ok", res["ok"], res)
            r.eq("exhibits keys", res["keys"], ["8-K:body", "8-K:EX-99.1"])
            body = conn.execute("SELECT * FROM documents WHERE section_key='8-K:body'").fetchone()
            ex = conn.execute("SELECT * FROM documents WHERE section_key='8-K:EX-99.1'").fetchone()
            r.ok("exhibits body text", "ARISTA NETWORKS, INC." in body["text"] and "Item 2.02" in body["text"],
                 body["text"][:200])
            r.ok("exhibits EX-99.1 is the release",
                 "Arista Networks, Inc. Reports Second Quarter 2026 Financial Results" in ex["text"], ex["text"][:200])
            r.ok("exhibits EX-99.1 drops the SGML wrapper", not ex["text"].startswith("EX-99.1"), ex["text"][:40])
            r.eq("exhibits rows carry ticker + accession", (ex["ticker"], ex["accession"], ex["kind"]),
                 ("ANET", ACC_8K, "filing_section"))
            r.ok("exhibits url is the exhibit", ex["url"].endswith("ex991q226-earningsrelease.htm"), ex["url"])
            row = conn.execute("SELECT ingested_at, ingest_error FROM filings WHERE accession=?", (ACC_8K,)).fetchone()
            r.eq("exhibits ingested_at set", (row["ingested_at"], row["ingest_error"]), (NOW, None))
            urls = [u for u, _h in op.calls]
            r.ok("exhibits the index page was read", edgar.index_url(ANET, ACC_8K) in urls, urls)
    # SPCX: the exhibit file has the same name as the 424B4 file, so only the index labels it.
    acc = "0001628280-26-066482"
    op = FakeOpener()
    op.add(edgar.index_url("0001181412", acc), 200, fx_bytes("idx_spcx"), exact=True)
    op.add("https://www.sec.gov/Archives/edgar/data/1181412/000162828026066482/spaceexplorationtechnologi.htm",
           200, fx_bytes("spcx_ex991"), exact=True)
    r.ok("exhibits SPCX file names collide", "<FILENAME>spaceexplorationtechnologi.htm" in fx("s1")[:300]
         and "<FILENAME>spaceexplorationtechnologi.htm" in fx("spcx_ex991")[:300])
    parts = sections.exhibit_sections(client(op), "0001181412", acc, "8-K")
    r.eq("exhibits SPCX keys", [p[0] for p in parts], ["8-K:EX-99.1"])
    r.ok("exhibits SPCX release text", "SpaceX Reports Third Quarter 2026 Results" in parts[0][2], parts[0][2][:200])
    r.ok("exhibits SPCX fetched no GRAPHIC", all(not u.endswith((".jpg", ".gif")) for u, _h in op.calls), op.calls)


def case_s1():
    out = secs("s1", "424B4")
    keys = [k for k, _t, _x in out]
    for h in ("RISK FACTORS", "USE OF PROCEEDS", "MANAGEMENT'S DISCUSSION AND ANALYSIS", "BUSINESS", "UNDERWRITING"):
        r.ok(f"s1 key {h}", f"424B4:{h}" in keys, keys)
    r.eq("s1 each key once", len(keys), len(set(keys)))
    hits = [k for k, _t, x in out if "largest actionable total addressable market" in x]
    r.ok("s1 one section holds the TAM sentence", len(hits) >= 1, hits)
    r.eq("s1 S-1 form uses the same headings", [k.split(":", 1)[1] for k, _t, _x in
                                                 sections.split_sections(text_of("s1"), "S-1")],
         [k.split(":", 1)[1] for k in keys])


def case_fallback():
    av = fx("av_anet")
    one = "\n".join(f"{s['speaker']} ({s['title']}): {s['content']}" for s in av["transcript"])  # §7.6 rule 4 form
    text = one + one
    r.eq("fallback input is 87,880 chars", len(text), 87_880)
    out = sections.split_sections(text, "10-K")
    r.eq("fallback keys", [k for k, _t, _x in out], ["10-K:part-1", "10-K:part-2"])
    r.ok("fallback each part <= 60,000", all(len(x) <= 60_000 for _k, _t, x in out), [len(x) for _k, _t, x in out])
    r.ok("fallback parts join to the input", "".join(x for _k, _t, x in out) == text)


def case_rf_diff():
    op = FakeOpener()
    f1 = serve(op, ACC_Q1, "10q_q1")
    f2 = serve(op, ACC_Q2, "10q_q2")
    with temp_root() as root:
        with root.led().connect() as conn:
            res1 = ingest(conn, client(op), f1)
            r.ok("rf_diff q1 ok", res1["ok"], res1)
            r.eq("rf_diff the first 10-Q stores no diff", res1["diffs"], {})
            res2 = ingest(conn, client(op), f2)
            r.ok("rf_diff q2 ok", res2["ok"], res2)
            r.eq("rf_diff q2 diff keys", sorted(res2["diffs"]), ["10-Q:II-1A-added", "10-Q:II-1A-removed"])
            get = lambda key: conn.execute("SELECT text FROM documents WHERE accession=? AND section_key=?",  # noqa: E731
                                           (ACC_Q2, key)).fetchone()["text"]
            added, removed, ii1a = get("10-Q:II-1A-added"), get("10-Q:II-1A-removed"), get("10-Q:II-1A")
            sent = ("We continue to increase our purchase commitments in response to the tightening of supply "
                    "conditions in the memory and silicon markets.")
            r.ok("rf_diff added holds the new sentence", sent in added.split("\n"))
            r.ok("rf_diff removed holds the old sentence",
                 sent.replace("We continue to increase", "We may also need to increase") in removed.split("\n"))
            share = len(added) / len(ii1a)
            r.ok("rf_diff added <= 8% of II-1A", share <= 0.08, round(share, 4))
            print(f"  rf_diff added share {share:.2%} ({len(added)} of {len(ii1a)} chars)")
            n = doc_count(conn)
            again = ingest(conn, client(op), f2)
            r.eq("rf_diff a re-ingest of q2 adds no row", doc_count(conn), n)
            r.eq("rf_diff a re-ingest of q2 returns the same diff ids", again["diffs"], res2["diffs"])


def case_rf_none():
    k = {key: x for key, _t, x in secs("10k", "10-K")}
    r.eq("rf_none the 10-K against itself", sections.risk_diff(k["10-K:1A"], k["10-K:1A"]), ([], []))
    op = FakeOpener()
    f = serve(op, ACC_10K, "10k")
    with temp_root() as root:
        with root.led().connect() as conn:
            res = ingest(conn, client(op), f)
            r.ok("rf_none 10-K ok", res["ok"], res)
            r.eq("rf_none a family's first filing has no diff", res["diffs"], {})
            r.ok("rf_none 10-K:1A stored", "10-K:1A" in res["documents"], res["keys"])
            n = doc_count(conn)
            fts = conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
            again = ingest(conn, client(op), f)
            r.eq("rf_none a re-ingest adds no row", doc_count(conn), n)
            r.eq("rf_none a re-ingest adds no FTS row", conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0], fts)
            r.eq("rf_none a re-ingest returns the same ids", again["documents"], res["documents"])
            r.eq("rf_none a re-ingest makes no diff", again["diffs"], {})


def case_ingest_error():
    op = FakeOpener()                                   # no route: the fake answers 404
    f = anet_filing(ACC_8K)
    with temp_root() as root:
        with root.led().connect() as conn:
            res = ingest(conn, client(op), f)
            r.ok("ingest_error not ok", not res["ok"] and "EdgarNotFound" in (res["error"] or ""), res)
            row = conn.execute("SELECT ingested_at, ingest_error FROM filings WHERE accession=?", (ACC_8K,)).fetchone()
            r.ok("ingest_error recorded", row["ingested_at"] is None and "404" in (row["ingest_error"] or ""), dict(row))
            r.eq("ingest_error no document", doc_count(conn), 0)
            op2 = FakeOpener().add(edgar.archive_url(ANET, ACC_8K, f["primary_doc"]), 403, b"", exact=True)
            r.raises("ingest_error a 403 raises EdgarBlocked", lambda: ingest(conn, client(op2), f), edgar.EdgarBlocked)
            row = conn.execute("SELECT ingest_error FROM filings WHERE accession=?", (ACC_8K,)).fetchone()
            r.ok("ingest_error a 403 is recorded too", "EdgarBlocked" in (row["ingest_error"] or ""), dict(row))


CASES = [case_to_text, case_page_lines, case_keys_10k, case_keys_10q, case_short, case_exhibits, case_s1,
         case_fallback, case_rf_diff, case_rf_none, case_ingest_error]

if __name__ == "__main__":
    for case in CASES:
        case()
    raise SystemExit(r.done())
