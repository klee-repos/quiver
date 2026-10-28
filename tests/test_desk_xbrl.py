"""test_desk_xbrl: companyfacts ingest and the §7.5 metric rules on the real fixtures.

Fixtures: ``cf_anet`` (ANET companyfacts), ``sub_anet`` (for the filings row), ``cf_tsm``
(IFRS, TWD and USD), ``cf_spcx`` (no dei facts). A test drops entries of a fixture; it
never adds one.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import Results, fx, temp_root  # noqa: E402

from lib.desk import edgar, xbrl  # noqa: E402

r = Results("desk-xbrl")
CF = fx("cf_anet")
CIK = xbrl.cik10(CF["cik"])
ACC = "0001596532-26-000175"
NOW = "2026-08-05T20:10:00Z"
M = 1e6


def near(a, b, tol=1.0):
    return a is not None and b is not None and abs(a - b) <= tol


def drop(cf, keep):
    """A copy of a companyfacts file without the facts for which ``keep(fact)`` is False."""
    out = copy.deepcopy(cf)
    for body in out["facts"].values():
        for concept in body.values():
            for unit in list(concept["units"]):
                concept["units"][unit] = [f for f in concept["units"][unit] if keep(f)]
    return out


def json_facts(cf, concept, unit="USD", tax="us-gaap"):
    return cf["facts"][tax][concept]["units"][unit]


def loaded(cf=CF):
    """A temp root with ``cf`` ingested. Returns (root, ledger)."""
    root = temp_root().__enter__()
    led = root.led()
    with led.connect() as c:
        xbrl.upsert_companyfacts(c, cf)
    return root, led


class FakeClient:
    """The duck-typed EDGAR client: ``get_json`` returns one scripted body per call."""

    def __init__(self, body):
        self.body = body
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        return self.body


# --- upsert ------------------------------------------------------------------------
with temp_root() as root:
    keys = set()
    for tax in ("us-gaap", "ifrs-full", "dei"):
        for concept, body in CF["facts"].get(tax, {}).items():
            for unit, facts in body["units"].items():
                for f in facts:
                    keys.add((tax, concept, unit, f.get("start", ""), f["end"], f["accn"]))
    led = root.led()
    with led.connect() as c:
        n1 = xbrl.upsert_companyfacts(c, CF)
        n2 = xbrl.upsert_companyfacts(c, CF)
        stored = c.execute("SELECT COUNT(*) FROM xbrl_facts WHERE cik=?", (CIK,)).fetchone()[0]
        taxes = {row[0] for row in c.execute("SELECT DISTINCT taxonomy FROM xbrl_facts")}
    r.eq("upsert: rows = distinct primary keys of us-gaap + dei", n1, len(keys))
    r.eq("upsert: the table holds the same count", stored, len(keys))
    r.eq("upsert: a second upsert adds 0", n2, 0)
    r.eq("upsert: ecd and srt facts are not stored", taxes, {"us-gaap", "dei"})

# --- gate --------------------------------------------------------------------------
with temp_root() as root:
    led = root.led()
    sub_row = next(f for f in edgar.recent_filings(fx("sub_anet")) if f["accession"] == ACC)
    stale = drop(CF, lambda f: f["accn"] != ACC)
    r.ok("gate: the fixture holds the new accession", xbrl.has_accession(CF, ACC))
    r.ok("gate: the stale copy does not", not xbrl.has_accession(stale, ACC))
    with led.connect() as c:
        edgar.insert_filing(c, filing=sub_row, cik=CIK, ticker="ANET", tier="redecide", now=NOW)
    client = FakeClient(stale)
    got = []
    for i in range(9):
        with led.connect() as c:
            got.append(xbrl.ingest_after_filing(c, client, cik=CIK, accession=ACC, now=NOW))
    with led.connect() as c:
        err9 = c.execute("SELECT ingest_error FROM filings WHERE accession=?", (ACC,)).fetchone()[0]
        count9 = c.execute("SELECT value FROM bridge_state WHERE key=?", (f"xbrl_wait:{ACC}",)).fetchone()[0]
        rows9 = c.execute("SELECT COUNT(*) FROM xbrl_facts").fetchone()[0]
        tenth = xbrl.ingest_after_filing(c, client, cik=CIK, accession=ACC, now=NOW)
        err10 = c.execute("SELECT ingest_error FROM filings WHERE accession=?", (ACC,)).fetchone()[0]
        eleventh = xbrl.ingest_after_filing(c, client, cik=CIK, accession=ACC, now=NOW)
    r.eq("gate: 9 misses wait", got, ["waiting"] * 9)
    r.eq("gate: the stale file is not used", rows9, 0)
    r.eq("gate: each run counts one miss", count9, "9")
    r.eq("gate: no ingest_error before the 10th miss", err9, None)
    r.eq("gate: the 10th miss gives up", tenth, "gave_up")
    r.ok("gate: the 10th miss sets ingest_error", bool(err10) and ACC in err10, err10)
    r.eq("gate: after giving up, no more fetches", (eleventh, len(client.calls)), ("gave_up", 10))
    r.eq("gate: the companyfacts URL", client.calls[0],
         "https://data.sec.gov/api/xbrl/companyfacts/CIK0001596532.json")

with temp_root() as root:
    led = root.led()
    client = FakeClient(drop(CF, lambda f: f["accn"] != ACC))
    with led.connect() as c:
        first = xbrl.ingest_after_filing(c, client, cik=CIK, accession=ACC, now=NOW)
    client.body = CF
    with led.connect() as c:
        second = xbrl.ingest_after_filing(c, client, cik=CIK, accession=ACC, now=NOW)
        left = c.execute("SELECT COUNT(*) FROM bridge_state WHERE key=?", (f"xbrl_wait:{ACC}",)).fetchone()[0]
        new = c.execute("SELECT COUNT(*) FROM xbrl_facts WHERE accession=?", (ACC,)).fetchone()[0]
    r.eq("gate: a later run with the new facts is ok", (first, second), ("waiting", "ok"))
    r.ok("gate: the new accession's facts are stored", new > 0, new)
    r.eq("gate: the miss count is cleared", left, 0)

# --- quarters ----------------------------------------------------------------------
root, led = loaded()
with led.connect() as c:
    rev = [q for q in xbrl.quarters(c, CIK, "revenue") if "2024-09-30" <= q["end"] <= "2026-06-30"]
    r.eq("quarters: revenue 2024-09-30..2026-06-30 ($M)", [round(q["value"] / M, 1) for q in rev],
         [1810.9, 1930.4, 2004.8, 2204.8, 2308.3, 2487.8, 2709.0, 3035.7])
    r.eq("quarters: the quarter ends", [q["end"] for q in rev],
         ["2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
          "2026-03-31", "2026-06-30"])
    r.eq("quarters: a derived Q4 has fp Q4", (rev[1]["fp"], rev[1]["fy"], rev[1]["start"]),
         ("Q4", 2024, "2024-10-01"))
    ocf = {q["end"]: q["value"] for q in xbrl.quarters(c, CIK, "ocf")}
    capex = {q["end"]: q for q in xbrl.quarters(c, CIK, "capex")}
    r.ok("quarters: Q2-2026 OCF 1,083.0M", near(ocf.get("2026-06-30"), 1083.0 * M), ocf.get("2026-06-30"))
    h1 = next(f["val"] for f in json_facts(CF, "PaymentsToAcquirePropertyPlantAndEquipment")
              if f.get("start") == "2026-01-01" and f["end"] == "2026-06-30")
    q1 = next(f["val"] for f in json_facts(CF, "PaymentsToAcquirePropertyPlantAndEquipment")
              if f.get("start") == "2026-01-01" and f["end"] == "2026-03-31")
    r.eq("quarters: the fixture holds capex H1 84.2M and Q1 54.5M", (h1, q1), (84_200_000, 54_500_000))
    r.ok("quarters: Q2-2026 capex 84.2 - 54.5 = 29.7M", near(capex["2026-06-30"]["value"], 29.7 * M),
         capex["2026-06-30"])

    # q4_synonym
    fy = [f for f in json_facts(CF, "PaymentsToAcquireProductiveAssets")
          if f.get("start") == "2025-01-01" and f["end"] == "2025-12-31"]
    ytd = [f for f in json_facts(CF, "PaymentsToAcquirePropertyPlantAndEquipment")
           if f.get("start") == "2025-01-01" and f["end"] == "2025-09-30"]
    no_ppe_fy = not any(f.get("start") == "2025-01-01" and f["end"] == "2025-12-31"
                        for f in json_facts(CF, "PaymentsToAcquirePropertyPlantAndEquipment"))
    r.ok("q4_synonym: FY2025 capex is only in PaymentsToAcquireProductiveAssets",
         no_ppe_fy and [f["val"] for f in fy] == [119_500_000], fy)
    r.eq("q4_synonym: Q3 YTD PaymentsToAcquirePropertyPlantAndEquipment", [f["val"] for f in ytd], [82_500_000])
    r.ok("q4_synonym: Q4-2025 capex = 119.5 - 82.5 = 37.0M", near(capex["2025-12-31"]["value"], 37.0 * M),
         capex["2025-12-31"])

    # non_additive (G20)
    eps_ends = {q["end"] for q in xbrl.quarters(c, CIK, "diluted_eps")}
    dil_ends = {q["end"] for q in xbrl.quarters(c, CIK, "diluted_shares")}
    r.ok("non_additive: Q4-2025 diluted_eps stays null", "2025-12-31" not in eps_ends)
    r.ok("non_additive: Q4-2025 diluted_shares stays null", "2025-12-31" not in dil_ends)
    r.ok("non_additive: the direct quarters are there", {"2025-09-30", "2026-06-30"} <= eps_ends & dil_ends)
    fin = {row["end"]: row for row in xbrl.financials(c, CIK, "quarterly", 12)}
    r.eq("non_additive: the financials row for Q4-2025 has no shares", fin["2025-12-31"]["diluted_shares"], None)

    # ttm
    t = {k: xbrl.ttm(c, CIK, k) for k in ("revenue", "ocf", "capex", "fcf")}
    r.ok("ttm: revenue 10,540.8M", near(t["revenue"]["value"], 10540.8 * M), t["revenue"])
    r.ok("ttm: OCF 5,306.6M", near(t["ocf"]["value"], 5306.6 * M), t["ocf"])
    r.ok("ttm: capex 151.3M", near(t["capex"]["value"], 151.3 * M), t["capex"])
    r.ok("ttm: FCF 5,155.3M", near(t["fcf"]["value"], 5155.3 * M), t["fcf"])
    r.eq("ttm: four consecutive quarters", (t["fcf"]["basis"], [p["end"] for p in t["fcf"]["periods"]]),
         ("quarters", ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]))

    # latest: H1-2024 and 9M-2024 capex were restated in a later filing
    old = sorted((f["filed"], f["val"]) for f in json_facts(CF, "PaymentsToAcquirePropertyPlantAndEquipment")
                 if f.get("start") == "2024-01-01" and f["end"] in ("2024-06-30", "2024-09-30"))
    r.eq("latest: the fixture has two filed values per period", [v for _f, v in old],
         [12_555_000, 19_580_000, 12_600_000, 19_600_000])
    r.ok("latest: Q3-2024 capex uses the later filed facts (19.6 - 12.6 = 7.0M, not 7.025M)",
         near(capex["2024-09-30"]["value"], 7.0 * M), capex["2024-09-30"])

    # balance (G21)
    bal = xbrl.balance(c, CIK)
    so = xbrl.shares_outstanding(c, CIK)
    ltd = json_facts(CF, "LongTermDebtNoncurrent")
    r.ok("balance: cash 2,290.2M", near(bal["cash"], 2290.2 * M), bal)
    r.ok("balance: st_investments 11,053.1M", near(bal["st_investments"], 11053.1 * M), bal)
    r.eq("balance: the balance-sheet date", bal["as_of"], "2026-06-30")
    r.eq("balance: shares 1,261,224,648 on 2026-07-30", (so["value"], so["date"]), (1_261_224_648, "2026-07-30"))
    r.ok("balance: LongTermDebtNoncurrent exists only for 2013-2014",
         ltd and all(f["end"] <= "2014-12-31" for f in ltd), [f["end"] for f in ltd])
    r.eq("balance: debt 0", bal["debt"], 0)
    dil = xbrl.diluted_shares(c, CIK)
    r.eq("balance: diluted shares of Q2-2026", (dil["value"], dil["end"]), (1_276_000_000, "2026-06-30"))

    # the desk table
    q4 = xbrl.financials(c, CIK, "quarterly", 4)
    r.eq("financials: the latest four quarters, newest first", [row["end"] for row in q4],
         ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])
    r.ok("financials: FCF = OCF - capex per row",
         all(near(row["fcf"], row["ocf"] - row["capex"]) for row in q4), q4)
    r.eq("financials: the row keys", set(q4[0]), {"start", "end", "fy", "fp", "revenue", "gross_profit",
                                                  "operating_income", "net_income", "ocf", "capex", "fcf",
                                                  "diluted_shares"})
    ann = xbrl.financials(c, CIK, "annual", 2)
    r.ok("financials: FY2025 revenue 9,005.7M", near(ann[0]["revenue"], 9005.7 * M) and ann[0]["end"] == "2025-12-31",
         ann[0])
    r.ok("financials: FY2025 net income is the 10-K value, not the DEF 14A 3511",
         near(ann[0]["net_income"], 3511.4 * M), ann[0]["net_income"])
    r.raises("financials: limit 13 refused", lambda: xbrl.financials(c, CIK, "quarterly", 13), xbrl.XbrlError)
    r.raises("financials: an unknown metric refused", lambda: xbrl.ttm(c, CIK, "ebitda"), xbrl.XbrlError)
root.__exit__(None, None, None)

# ttm without four consecutive quarters: drop every fact that ends 2025-06-30
root, led = loaded(drop(CF, lambda f: f["end"] != "2025-06-30"))
with led.connect() as c:
    fy_ocf = next(f["val"] for f in json_facts(CF, "NetCashProvidedByUsedInOperatingActivities")
                  if f.get("start") == "2025-01-01" and f["end"] == "2025-12-31")
    t_ocf = xbrl.ttm(c, CIK, "ocf")
    t_capex = xbrl.ttm(c, CIK, "capex")
    t_rev = xbrl.ttm(c, CIK, "revenue")
    r.eq("ttm: without Q3-2025 OCF, TTM = FY2025", (t_ocf["basis"], t_ocf["value"]), ("fy", fy_ocf))
    r.eq("ttm: the FY period", t_ocf["periods"], [{"start": "2025-01-01", "end": "2025-12-31"}])
    r.ok("ttm: capex TTM = FY2025 119.5M", t_capex["basis"] == "fy" and near(t_capex["value"], 119.5 * M), t_capex)
    r.ok("ttm: revenue keeps four quarters (Q3-2025 is a direct fact)",
         t_rev["basis"] == "quarters" and near(t_rev["value"], 10540.8 * M), t_rev)
root.__exit__(None, None, None)

# --- others (G22, SPCX) ------------------------------------------------------------
TSM = fx("cf_tsm")
root, led = loaded(TSM)
with led.connect() as c:
    tcik = TSM["cik"]
    usd = {f["end"]: f["val"] for f in json_facts(TSM, "Revenue", "USD", "ifrs-full") if f.get("start")}
    twd = {f["end"]: f["val"] for f in json_facts(TSM, "Revenue", "TWD", "ifrs-full") if f.get("start")}
    t = xbrl.ttm(c, tcik, "revenue")
    last = t["periods"][-1]["end"]
    r.eq("others: TSM revenue TTM is the last USD fiscal year", (t["basis"], t["value"]), ("fy", usd[last]))
    r.ok("others: the TWD value differs", twd.get(last) not in (None, t["value"]), (twd.get(last), t["value"]))
    r.eq("others: TSM revenue comes from ifrs-full:Revenue", xbrl.reason(c, tcik, "revenue"), None)
    prior = xbrl.ttm_year_ago(c, tcik, "revenue")
    r.eq("others: the year-ago TSM TTM is the previous USD fiscal year",
         (prior["basis"], prior["value"]), ("fy", usd[max(e for e in usd if e < last)]))
    r.eq("others: TSM has shares outstanding from dei", xbrl.shares_outstanding(c, tcik)["date"], "2025-12-31")
root.__exit__(None, None, None)

root, led = loaded(drop(TSM, lambda f: False))
with led.connect() as c:
    r.eq("others: no facts at all -> missing", xbrl.reason(c, TSM["cik"], "revenue"), "missing")
root.__exit__(None, None, None)
twd_only = copy.deepcopy(TSM)
for concept in twd_only["facts"]["ifrs-full"].values():
    if "USD" in concept["units"]:
        concept["units"]["USD"] = []
root, led = loaded(twd_only)
with led.connect() as c:
    r.eq("others: TWD-only revenue is null", xbrl.ttm(c, TSM["cik"], "revenue"), None)
    r.eq("others: with reason non_usd", xbrl.reason(c, TSM["cik"], "revenue"), "non_usd")
root.__exit__(None, None, None)

SPCX = fx("cf_spcx")
root, led = loaded(SPCX)
with led.connect() as c:
    from lib.desk import valuation  # noqa: E402
    S = {"valuation_discount_rate": 0.09, "valuation_terminal_growth": 0.03, "valuation_years": 10,
         "margin_of_safety_pct": 15}
    r.ok("others: SPCX companyfacts has no dei", "dei" not in SPCX["facts"])
    r.eq("others: SPCX has no shares outstanding", xbrl.shares_outstanding(c, SPCX["cik"]), None)
    calls = []
    m = valuation.compute_metrics(c, "SPCX", cik=SPCX["cik"], settings=S, now=NOW, price_fn=lambda t: 210.0,
                                  market_cap_fn=lambda t: calls.append(t) or 2.1e12)
    r.eq("others: SPCX market cap comes from yfinance", (m["market_cap"], calls), (2.1e12, ["SPCX"]))
    bal = xbrl.balance(c, SPCX["cik"])
    r.ok("others: SPCX EV = market cap + debt - cash - st",
         near(m["ev"], 2.1e12 + bal["debt"] - bal["cash"] - bal["st_investments"]), (m["ev"], bal))
root.__exit__(None, None, None)

raise SystemExit(r.done())
