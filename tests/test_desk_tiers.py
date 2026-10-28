"""events.filing_tier and events.event_type (plan §7.3, §18.9 G18 G19), and the index parser.

Run: .venv/bin/python tests/test_desk_tiers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import Results, fx  # noqa: E402

from lib.desk import edgar, events, sections  # noqa: E402

r = Results("desk-tiers")
tier = events.filing_tier

REDECIDE_ITEMS = ("1.01", "1.03", "2.01", "2.02", "2.05", "2.06", "3.01", "4.02", "5.01")
ANET_8K = "0001596532-26-000174"


def anet_index_rows() -> list:
    url = edgar.index_url("0001596532", ANET_8K)
    return sections.parse_index(fx("idx_anet"), url)


def case_redecide():
    for form in ("10-K", "10-Q", "20-F", "40-F", "S-1", "F-1", "424B4"):
        r.eq(f"redecide {form}", tier(form, None, None), "redecide")
    for item in REDECIDE_ITEMS:
        r.eq(f"redecide 8-K {item}", tier("8-K", item, []), "redecide")
        r.eq(f"redecide 8-K {item},9.01", tier("8-K", f"{item},9.01", []), "redecide")
        r.eq(f"redecide 8-K list [{item}, 9.01]", tier("8-K", [item, "9.01"], []), "redecide")


def case_ex99():
    rows = anet_index_rows()
    without = [x for x in rows if not x["type"].upper().startswith("EX-99")]
    r.ok("ex99 the real index has an EX-99.1 row", any(x["type"] == "EX-99.1" for x in rows), rows)
    for items in ("7.01", "8.01", "7.01,9.01", "8.01,9.01"):
        r.eq(f"ex99 8-K {items} with EX-99", tier("8-K", items, rows), "redecide")
        r.eq(f"ex99 8-K {items} without EX-99", tier("8-K", items, without), "record")
        r.eq(f"ex99 8-K {items} with type strings", tier("8-K", items, ["EX-99.1"]), "redecide")
        r.eq(f"ex99 8-K {items} no exhibit list", tier("8-K", items, None), "record")
    r.eq("ex99 6-K with EX-99", tier("6-K", None, rows), "redecide")
    r.eq("ex99 6-K with EX-99.2 string", tier("6-K", "", ["EX-99.2"]), "redecide")
    r.eq("ex99 6-K without EX-99", tier("6-K", None, without), "record")
    r.eq("ex99 6-K no exhibits", tier("6-K", None, None), "record")


def case_record():
    for items in ("5.02", "5.03", "5.07", "5.02,9.01", "5.03,9.01", "9.01", ""):
        r.eq(f"record 8-K {items!r}", tier("8-K", items, []), "record")
    r.eq("record 10-K/A", tier("10-K/A", None, None), "record")
    r.eq("record 10-Q/A", tier("10-Q/A", None, None), "record")
    r.eq("record 8-K/A with 2.02", tier("8-K/A", "2.02,9.01", []), "record")
    r.eq("record 8-K/A with 5.07", tier("8-K/A", "5.07", []), "record")
    r.eq("record 6-K/A", tier("6-K/A", None, ["EX-99.1"]), "record")
    for form in ("DEF 14A", "NPORT-P", "N-CSR", "N-CSRS", "NPORT-P/A", "S-1/A", "424B4/A"):
        r.eq(f"record {form}", tier(form, None, None), "record")


def case_ignore():
    for form in ("3", "4", "5", "144", "SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
                 "13F-HR", "S-8", "11-K", "DEFA14A", "SD", "ARS", "UPLOAD", "CORRESP", "4/A", "144/A",
                 "SD/A", "3/A", "S-8 POS", ""):
        r.eq(f"ignore {form!r}", tier(form, None, None), "ignore")
    r.eq("ignore lower-case input normalizes", tier("sc 13g/a", None, None), "ignore")
    r.eq("ignore form 4 even with item text", tier("4", "2.02", ["EX-99.1"]), "ignore")


def case_event_type():
    r.eq("event_type 8-K 2.02", events.event_type("8-K", "2.02,9.01"), "earnings")
    r.eq("event_type 8-K 2.02 list", events.event_type("8-K", ["2.02"]), "earnings")
    r.eq("event_type 8-K 1.01", events.event_type("8-K", "1.01,9.01"), "filing")
    r.eq("event_type 6-K with EX-99 (G19)", events.event_type("6-K", None), "filing")
    r.eq("event_type 6-K tier with EX-99 is redecide", tier("6-K", None, ["EX-99.1"]), "redecide")
    r.eq("event_type 10-Q", events.event_type("10-Q", None), "filing")
    r.eq("event_type 8-K/A 2.02 is a filing", events.event_type("8-K/A", "2.02"), "filing")


def case_real_anet():
    filings = edgar.recent_filings(fx("sub_anet"))
    r.eq("real_anet 1,000 recent filings", len(filings), 1000)
    small = [f for f in filings if f["form"] in ("4", "144")]
    r.eq("real_anet 892 forms 4/144", len(small), 892)
    r.ok("real_anet every 4/144 ignore", all(tier(f["form"], f["items"], None) == "ignore" for f in small))
    earn = [f for f in filings if f["form"] == "8-K" and "2.02" in (f["items"] or "")]
    r.eq("real_anet 18 8-Ks with 2.02", len(earn), 18)
    r.ok("real_anet 2.02 8-Ks redecide", all(tier("8-K", f["items"], None) == "redecide" for f in earn))
    r.ok("real_anet 2.02 8-Ks earnings", all(events.event_type("8-K", f["items"]) == "earnings" for f in earn))
    only_507 = [f for f in filings if f["form"] == "8-K" and f["items"] == "5.07"]
    r.ok("real_anet 5.07-only 8-Ks exist", len(only_507) >= 1, len(only_507))
    r.ok("real_anet 5.07-only 8-Ks record", all(tier("8-K", f["items"], None) == "record" for f in only_507))
    periodic = [f for f in filings if f["form"] in ("10-K", "10-Q")]
    r.eq("real_anet 4 10-K + 14 10-Q", len(periodic), 18)
    r.ok("real_anet every 10-K/10-Q redecide", all(tier(f["form"], f["items"], None) == "redecide" for f in periodic))
    r.ok("real_anet SC 13G/A and SCHEDULE 13G/A ignore",
         all(tier(f["form"]) == "ignore" for f in filings if "13G" in f["form"]))


def case_index():
    url = edgar.index_url("0001181412", "0001628280-26-066482")
    rows = sections.parse_index(fx("idx_spcx"), url)
    types = [x["type"] for x in rows]
    r.eq("index types in order", list(dict.fromkeys(types)), ["8-K", "EX-99.1", "GRAPHIC"])
    r.ok("index no row without a type", all(types))
    primary = rows[0]
    r.eq("index primary seq", primary["seq"], 1)
    r.eq("index primary drops the /ix?doc= viewer prefix", primary["url"],
         "https://www.sec.gov/Archives/edgar/data/1181412/000162828026066482/spcx-20261020.htm")
    ex = [x for x in rows if x["type"] == "EX-99.1"][0]
    r.eq("index EX-99.1 document", ex["document"], "spaceexplorationtechnologi.htm")
    r.eq("index EX-99.1 url", ex["url"],
         "https://www.sec.gov/Archives/edgar/data/1181412/000162828026066482/spaceexplorationtechnologi.htm")
    ceg = sections.parse_index(fx("idx_ceg"), edgar.index_url("0001868275", "0001868275-26-000097"))
    r.ok("index CEG keeps the co-registrant CIK in the link",
         [x["url"] for x in ceg if x["type"] == "EX-99.1"][0].startswith(
             "https://www.sec.gov/Archives/edgar/data/1168165/"), ceg[:2])
    r.eq("index CEG EX-99 rows", [x["type"] for x in ceg if x["type"].startswith("EX-99")], ["EX-99.1", "EX-99.2"])


CASES = [case_redecide, case_ex99, case_record, case_ignore, case_event_type, case_real_anet, case_index]

if __name__ == "__main__":
    for case in CASES:
        case()
    raise SystemExit(r.done())
