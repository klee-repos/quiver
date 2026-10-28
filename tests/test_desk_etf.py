"""ETF holdings and look-through (plan §7.7 step 4, §8.4, §18.9 G23).

Fixtures: ``nport_soxx``, ``feed_soxx``, ``nport_bot``, ``tick_co``, ``yf_top``. No network.
The EDGAR client is a duck-typed fake with ``get_text``. ``sec_ticker_map`` is a copy of SEC
data, so ``etf.load_ticker_map_rows`` fills it from ``tick_co``. Every desk row comes from a
module function.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import REPO, Results, fx, temp_root  # noqa: E402

import pandas as pd  # noqa: E402

from lib.desk import etf, store  # noqa: E402

r = Results("desk-etf")
NOW = "2026-10-28T14:00:00Z"
SOXX_SERIES = "S000004354"
SOXX_TRUST = "0001100663"
SOXX_ACC = "0002071691-26-019781"
FIVE_PCT = [("MU", 8.54), ("AMD", 8.09), ("NVDA", 6.81), ("INTC", 6.33), ("AVGO", 6.08),
            ("AMAT", 5.77), ("KLAC", 5.64), ("MRVL", 5.22)]

# The §8.1 metrics row keys (compute_metrics). Each test row fills every key.
METRIC_KEYS = (
    "revenue_ttm", "revenue_growth_yoy", "revenue_growth_q_yoy", "gross_margin_ttm",
    "operating_margin_ttm", "net_margin_ttm", "ocf_ttm", "capex_ttm", "fcf_ttm",
    "fcf_margin_ttm", "sbc_ttm", "market_cap", "cash", "st_investments", "debt", "ev",
    "ev_sales", "fcf_yield", "pe_ttm", "rule_of_40", "dilution_yoy",
    "reverse_dcf_implied_growth", "next_earnings", "technicals")


class Client:
    """A duck-typed EDGAR client: exact URL -> body. Records each URL."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def get_text(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise RuntimeError(f"404 {url}")
        return self.routes[url]


def drop_entries(feed_text: str, form: str) -> str:
    """The feed with every entry of exactly ``form`` dropped (a test may drop entries)."""
    def keep(m):
        return "" if f"<filing-type>{form}</filing-type>" in m.group(0) else m.group(0)
    return re.sub(r"<entry>.*?</entry>\s*", keep, feed_text, flags=re.S)


def soxx_doc_url() -> str:
    return f"https://www.sec.gov/Archives/edgar/data/1100663/{SOXX_ACC.replace('-', '')}/primary_doc.xml"


def load_map(c) -> int:
    return etf.load_ticker_map_rows(c, fx("tick_co"), NOW)


def store_soxx_nport(c) -> dict:
    parsed = etf.parse_nport(fx("nport_soxx"))
    rows = etf.map_tickers(c, parsed["holdings"])
    etf.store_holdings(c, "SOXX", parsed["as_of"], "nport", rows)
    return parsed


def yf_funds_data():
    """``funds_data`` rebuilt from the ``yf_top`` capture."""
    raw = fx("yf_top")
    top = pd.DataFrame(**raw["top_holdings"])
    top.index.name = raw["top_holdings_index_name"]
    ops = pd.DataFrame(**raw["fund_operations"])
    ops.index.name = raw["fund_operations_index_name"]
    return SimpleNamespace(top_holdings=top, fund_operations=ops)


def metric_row(**values) -> dict:
    row = {k: None for k in METRIC_KEYS}
    row.update(values)
    return row


# --- nport -------------------------------------------------------------------------

def case_nport():
    parsed = etf.parse_nport(fx("nport_soxx"))
    hs = parsed["holdings"]
    cats = {}
    for h in hs:
        cats[h["asset_cat"]] = cats.get(h["asset_cat"], 0) + 1
    r.eq("nport: 33 holdings", len(hs), 33)
    r.eq("nport: 30 EC, 2 STIV, 1 DE", cats, {"EC": 30, "STIV": 2, "DE": 1})
    r.eq("nport: as_of = repPdDate", parsed["as_of"], "2026-06-30")
    r.eq("nport: series id", parsed["series_id"], SOXX_SERIES)
    r.eq("nport: form", parsed["form"], "NPORT-P")
    by_name = {h["name"]: h for h in hs}
    mu = by_name.get("Micron Technology, Inc.") or {}
    r.eq("nport: Micron CUSIP", mu.get("cusip"), "595112103")
    r.eq("nport: Micron weight = pctVal", mu.get("weight_pct"), 8.544235842935)
    r.eq("nport: NVIDIA CUSIP", (by_name.get("NVIDIA Corp.") or {}).get("cusip"), "67066G104")
    r.eq("nport: Lam CUSIP", (by_name.get("Lam Research Corp.") or {}).get("cusip"), "512807306")
    r.eq("nport: CUSIP N/A is None", (by_name.get("Credo Technology Group Holding Ltd.") or {})
         .get("cusip", "missing"), None)
    r.ok("nport: no row has a ticker (G23)", all("holding_ticker" not in h for h in hs))
    de = [h for h in hs if h["asset_cat"] == "DE"]
    r.eq("nport: the DE row with name N/A takes its title", [h["name"] for h in de],
         ["E-Mini Technology Select Sector Index"])
    # pctVal is percent of net assets. The cash vehicles take the sum over 100.
    r.eq("nport: weights are percent", round(sum(h["weight_pct"] for h in hs), 2), 101.17)

    entries = etf.parse_feed(fx("feed_soxx"))
    forms = [e["form"] for e in entries]
    r.ok("feed: has NPORT-P and NPORT-P/A", "NPORT-P" in forms and "NPORT-P/A" in forms, forms)
    latest = etf.latest_nport(entries) or {}
    r.eq("feed: latest NPORT-P", latest.get("accession"), SOXX_ACC)
    r.eq("feed: latest filed", latest.get("filed"), "2026-08-25")
    amend_only = [e for e in entries if e["form"] == "NPORT-P/A"]
    r.ok("feed: the feed holds an amendment", len(amend_only) >= 1)
    r.eq("feed: NPORT-P/A gives no holdings set", etf.latest_nport(amend_only), None)
    r.eq("feed: dropped NPORT-P entries -> None",
         etf.latest_nport(etf.parse_feed(drop_entries(fx("feed_soxx"), "NPORT-P"))), None)

    with temp_root() as tr:
        led = tr.led()
        with led.connect() as c:
            load_map(c)
            store_soxx_nport(c)
            rows = c.execute("SELECT source, as_of, holding_name, cusip, holding_ticker "
                             "FROM etf_holdings WHERE fund='SOXX'").fetchall()
        r.eq("store: 33 rows", len(rows), 33)
        r.eq("store: source nport, as_of 2026-06-30", {(x[0], x[1]) for x in rows},
             {("nport", "2026-06-30")})
        names = {x[2] for x in rows}
        r.ok("store: both BlackRock Funds III rows kept",
             {"BlackRock Funds III (066922519)", "BlackRock Funds III (066922477)"} <= names, names)
        r.ok("store: names + CUSIPs", ("Micron Technology, Inc.", "595112103") in
             {(x[2], x[3]) for x in rows})
        unmatched = sorted(x[2] for x in rows if x[4] is None)
        r.eq("store: unmatched rows stay with no ticker", len(unmatched), 3)
        with led.connect() as c:
            again = etf.store_holdings(c, "SOXX", "2026-06-30", "nport",
                                       etf.map_tickers(c, etf.parse_nport(fx("nport_soxx"))["holdings"]))
            n = c.execute("SELECT COUNT(*) FROM etf_holdings").fetchone()[0]
        r.eq("store: a re-run replaces by key", (again, n), (33, 33))

    # refresh_nport: the unfiltered feed first, the form filter in Python.
    unfiltered = etf.feed_url(SOXX_SERIES)
    filtered = etf.feed_url(SOXX_SERIES, "NPORT-P")
    r.ok("refresh: the unfiltered feed sends type= empty",
         f"CIK={SOXX_SERIES}&type=&" in unfiltered and "output=atom" in unfiltered, unfiltered)
    with temp_root() as tr:
        led = tr.led()
        client = Client({unfiltered: fx("feed_soxx"), soxx_doc_url(): fx("nport_soxx")})
        with led.connect() as c:
            load_map(c)
            res = etf.refresh_nport(c, client, fund="SOXX", cik=SOXX_TRUST, series_id=SOXX_SERIES,
                                    now=NOW)
        r.eq("refresh: stored", (res["status"], res["accession"], res["as_of"], res["rows"]),
             ("stored", SOXX_ACC, "2026-06-30", 33))
        r.eq("refresh: one feed + the primary doc", client.calls, [unfiltered, soxx_doc_url()])
        with led.connect() as c:
            mapped = c.execute("SELECT COUNT(*) FROM etf_holdings WHERE holding_ticker IS NOT NULL"
                               ).fetchone()[0]
            res2 = etf.refresh_nport(c, client, fund="SOXX", cik=SOXX_TRUST,
                                     series_id=SOXX_SERIES, now=NOW)
        r.eq("refresh: rows are mapped by name", mapped, 30)
        r.eq("refresh: same accession -> unchanged, no doc fetch",
             (res2["status"], client.calls[2:]), ("unchanged", [unfiltered]))

    with temp_root() as tr:
        led = tr.led()
        client = Client({unfiltered: drop_entries(fx("feed_soxx"), "NPORT-P"),
                         filtered: fx("feed_soxx"), soxx_doc_url(): fx("nport_soxx")})
        with led.connect() as c:
            res = etf.refresh_nport(c, client, fund="SOXX", cik=None, series_id=SOXX_SERIES,
                                    now=NOW)
        r.eq("refresh: no NPORT-P unfiltered -> the form-filtered feed",
             (res["status"], client.calls), ("stored", [unfiltered, filtered, soxx_doc_url()]))

    with temp_root() as tr:
        led = tr.led()
        other = fx("nport_soxx").replace(f"<seriesId>{SOXX_SERIES}</seriesId>",
                                         "<seriesId>S000099999</seriesId>")
        client = Client({unfiltered: fx("feed_soxx"), soxx_doc_url(): other})
        with led.connect() as c:
            res = etf.refresh_nport(c, client, fund="SOXX", cik=SOXX_TRUST, series_id=SOXX_SERIES,
                                    now=NOW)
            n = c.execute("SELECT COUNT(*) FROM etf_holdings").fetchone()[0]
        r.eq("refresh: another series id -> nothing stored", (res["status"], n),
             ("series_mismatch", 0))


# --- five_pct ----------------------------------------------------------------------

def case_five_pct():
    r.eq("normalize: SEC state tag", etf.normalize_name("APPLIED MATERIALS INC /DE"),
         "applied materials")
    r.eq("normalize: N-PORT name", etf.normalize_name("Applied Materials, Inc."), "applied materials")
    r.eq("normalize: N.V. and NV", etf.normalize_name("NXP Semiconductors N.V."),
         etf.normalize_name("NXP Semiconductors NV"))
    r.eq("normalize: the stop words", etf.normalize_name("Taiwan Semiconductor Manufacturing Co. Ltd."),
         "taiwan semiconductor manufacturing")
    r.eq("normalize: QUALCOMM INC/DE", etf.normalize_name("QUALCOMM INC/DE"), "qualcomm")

    with temp_root() as tr:
        led = tr.led()
        with led.connect() as c:
            load_map(c)
            store_soxx_nport(c)
            store.upsert_company(c, ticker="NVDA", kind="company", status="holding", now=NOW,
                                 status_reason="owner holds it")
            store.upsert_company(c, ticker="AMD", kind="company", status="candidate", now=NOW)
            lt = etf.lookthrough_companies(c, "SOXX")
            got = [(x["ticker"], round(x["weight_pct"], 2)) for x in lt]
            lrcx = c.execute("SELECT holding_ticker, weight_pct FROM etf_holdings "
                             "WHERE holding_name='Lam Research Corp.'").fetchone()
            marked = etf.mark_lookthrough(c, "SOXX", lt, now=NOW)
            comps = {x["ticker"]: x for x in store.list_companies(c)}
            marked2 = etf.mark_lookthrough(c, "SOXX", lt, now=NOW)
        r.eq("five_pct: exactly the eight names", got, FIVE_PCT)
        r.ok("five_pct: each has a CIK", all(x["cik"] and len(x["cik"]) == 10 for x in lt), lt)
        r.eq("five_pct: MU CIK", lt[0]["cik"] if lt else None, "0000723125")
        r.eq("five_pct: LRCX maps but is 4.89", (lrcx[0], round(lrcx[1], 2)) if lrcx else None,
             ("LRCX", 4.89))
        r.ok("five_pct: LRCX is no look-through company", "LRCX" not in comps)
        r.eq("five_pct: MU watch + reason", (comps["MU"]["status"], comps["MU"]["status_reason"]),
             ("watch", "look-through: SOXX 8.5%"))
        r.eq("five_pct: MU CIK stored", comps["MU"]["cik"], "0000723125")
        r.eq("five_pct: a candidate becomes watch",
             (comps["AMD"]["status"], comps["AMD"]["status_reason"]),
             ("watch", "look-through: SOXX 8.1%"))
        r.eq("five_pct: a holding stays holding",
             (comps["NVDA"]["status"], comps["NVDA"]["status_reason"]), ("holding", "owner holds it"))
        want = {"MU": "8.5", "AMD": "8.1", "INTC": "6.3", "AVGO": "6.1", "AMAT": "5.8",
                "KLAC": "5.6", "MRVL": "5.2"}
        r.eq("five_pct: seven watch rows, one decimal",
             {t: comps[t]["status_reason"] for t in want},
             {t: f"look-through: SOXX {w}%" for t, w in want.items()})
        r.eq("five_pct: the return value", [(m["ticker"], m["status"]) for m in marked],
             [("MU", "watch"), ("AMD", "watch"), ("NVDA", "holding"), ("INTC", "watch"),
              ("AVGO", "watch"), ("AMAT", "watch"), ("KLAC", "watch"), ("MRVL", "watch")])
        r.ok("five_pct: a second run changes nothing", not any(m["changed"] for m in marked2), marked2)

    # Value changes on one captured row: the asset category gate and the spaceless match.
    with temp_root() as tr:
        led = tr.led()
        mu = next(h for h in etf.parse_nport(fx("nport_soxx"))["holdings"]
                  if h["name"] == "Micron Technology, Inc.")
        with led.connect() as c:
            load_map(c)
            as_ec = etf.map_tickers(c, [mu])[0]
            as_stiv = etf.map_tickers(c, [dict(mu, asset_cat="STIV")])[0]
            amzn = etf.map_tickers(c, [dict(mu, name="Amazon.com, Inc.", title="Amazon.com, Inc.")])[0]
        r.eq("map: an EC row maps", (as_ec["holding_ticker"], as_ec["cik"]), ("MU", "0000723125"))
        r.eq("map: a STIV row gets no ticker", (as_stiv["holding_ticker"], as_stiv["cik"]), (None, None))
        r.eq("map: Amazon.com matches AMAZON COM INC", amzn["holding_ticker"], "AMZN")


# --- cef ---------------------------------------------------------------------------

def case_cef():
    parsed = etf.parse_nport(fx("nport_bot"))
    with temp_root() as tr:
        led = tr.led()
        with led.connect() as c:
            load_map(c)
            rows = etf.map_tickers(c, parsed["holdings"])
            etf.store_holdings(c, "BOT", parsed["as_of"], "nport", rows)
            no_kind = etf.lookthrough_companies(c, "BOT")
            store.upsert_company(c, ticker="BOT", kind="cef", status="holding", now=NOW)
            lt = etf.lookthrough_companies(c, "BOT")
            big = c.execute("SELECT COUNT(*) FROM etf_holdings WHERE fund='BOT' AND "
                            "weight_pct >= 5").fetchone()[0]
            # The kind gate alone: SOXX's mapped rows under the BOT name still give none.
            soxx = etf.parse_nport(fx("nport_soxx"))
            etf.store_holdings(c, "BOT", "2026-09-30", "nport", etf.map_tickers(c, soxx["holdings"]))
            gate = etf.lookthrough_companies(c, "BOT")
            etf.store_holdings(c, "SOXX", "2026-09-30", "nport", etf.map_tickers(c, soxx["holdings"]))
            control = etf.lookthrough_companies(c, "SOXX")
            n_comp = len(store.list_companies(c))
        r.ok("cef: BOT has holdings of 5% or more", big >= 4, big)
        r.ok("cef: no BOT holding maps to a ticker", all(x["holding_ticker"] is None for x in rows))
        r.eq("cef: no ticker -> no look-through", no_kind, [])
        r.eq("cef: BOT gets no look-through companies", lt, [])
        r.eq("cef: kind cef blocks look-through", gate, [])
        r.eq("cef: the control fund gives eight", len(control), 8)
        r.eq("cef: only BOT is a company", n_comp, 1)


# --- weighted ----------------------------------------------------------------------

def case_weighted():
    with temp_root() as tr:
        led = tr.led()
        with led.connect() as c:
            load_map(c)
            store_soxx_nport(c)
            latest = etf.latest_holdings(c, "SOXX")
    holdings = latest["rows"]
    r.eq("weighted: the latest set is the N-PORT set", (latest["source"], latest["as_of"],
                                                        len(holdings)), ("nport", "2026-06-30", 33))
    w = {h["holding_ticker"]: h["weight_pct"] for h in holdings if h["holding_ticker"]}
    # Values chosen by the test. AVGO has no EV/Sales, INTC has negative FCF.
    metrics = {
        "MU": metric_row(revenue_growth_yoy=0.40, fcf_yield=0.030, ev_sales=6.0, revenue_ttm=3.0e10),
        "AMD": metric_row(revenue_growth_yoy=0.25, fcf_yield=0.015, ev_sales=10.0),
        "NVDA": metric_row(revenue_growth_yoy=0.60, fcf_yield=0.020, ev_sales=25.0),
        "INTC": metric_row(revenue_growth_yoy=-0.05, fcf_yield=-0.010, ev_sales=3.0),
        "AVGO": metric_row(revenue_growth_yoy=0.20, fcf_yield=0.025, ev_sales=None),
        "AMAT": metric_row(revenue_growth_yoy=0.10, fcf_yield=0.035, ev_sales=7.0),
        "KLAC": metric_row(revenue_growth_yoy=0.15, fcf_yield=0.030, ev_sales=9.0),
        "MRVL": metric_row(revenue_growth_yoy=0.30, fcf_yield=0.010, ev_sales=12.0),
    }
    r.ok("weighted: the rows have the §8.1 keys", all(set(m) == set(METRIC_KEYS) for m in metrics.values()))
    t = list(metrics)
    tw = sum(w[k] for k in t)
    want_growth = sum(w[k] * metrics[k]["revenue_growth_yoy"] for k in t) / tw
    want_fcf = sum(w[k] * metrics[k]["fcf_yield"] for k in t) / tw
    ev_t = [k for k in t if metrics[k]["ev_sales"]]
    want_ev = sum(w[k] for k in ev_t) / sum(w[k] / metrics[k]["ev_sales"] for k in ev_t)
    out = etf.weighted_lookthrough(holdings, metrics, expense_ratio=0.33)

    def near(a, b):
        return a is not None and b is not None and abs(a - b) < 1e-9

    r.ok("weighted: growth = Σw·g / Σw", near(out["revenue_growth_yoy"], want_growth),
         (out["revenue_growth_yoy"], want_growth))
    r.ok("weighted: FCF yield = Σw·y / Σw", near(out["fcf_yield"], want_fcf), (out["fcf_yield"], want_fcf))
    r.ok("weighted: EV/Sales = Σw / Σ(w/x)", near(out["ev_sales"], want_ev), (out["ev_sales"], want_ev))
    r.ok("weighted: harmonic < arithmetic", out["ev_sales"] <
         sum(w[k] * metrics[k]["ev_sales"] for k in ev_t) / sum(w[k] for k in ev_t))
    r.ok("weighted: coverage = Σ covered weight", near(out["coverage_pct"], tw), (out["coverage_pct"], tw))
    r.ok("weighted: coverage 52.5% is not low", 50 <= out["coverage_pct"] < 53 and not out["low_coverage"],
         out["coverage_pct"])
    r.eq("weighted: expense ratio", out["expense_ratio"], 0.33)
    r.eq("weighted: holdings carry source and as_of",
         {(h["source"], h["as_of"]) for h in out["holdings"]}, {("nport", "2026-06-30")})
    r.eq("weighted: top source and as_of", (out["source"], out["as_of"]), ("nport", "2026-06-30"))
    r.eq("weighted: has_metrics on 8 holdings", sum(1 for h in out["holdings"] if h["has_metrics"]), 8)

    few = {k: metrics[k] for k in ("MU", "AMD", "NVDA", "INTC", "AVGO")}
    tw2 = sum(w[k] for k in few)
    out2 = etf.weighted_lookthrough(holdings, few)
    r.ok("weighted: coverage under 50 -> low_coverage",
         near(out2["coverage_pct"], tw2) and tw2 < 50 and out2["low_coverage"] is True,
         (out2["coverage_pct"], tw2))
    r.eq("weighted: no expense ratio -> None", out2["expense_ratio"], None)
    out3 = etf.weighted_lookthrough(holdings, {})
    r.eq("weighted: no metrics -> no averages",
         (out3["revenue_growth_yoy"], out3["fcf_yield"], out3["ev_sales"], out3["coverage_pct"],
          out3["low_coverage"]), (None, None, None, 0.0, True))


# --- top10 -------------------------------------------------------------------------

def case_top10():
    calls = []

    def top_fn(fund):
        calls.append(fund)
        return yf_funds_data()

    with temp_root() as tr:
        led = tr.led()
        with led.connect() as c:
            load_map(c)
            res = etf.refresh_top(c, "soxx", now=NOW, top_fn=top_fn)
            rows = c.execute("SELECT holding_ticker, holding_name, weight_pct, source, as_of "
                             "FROM etf_holdings WHERE fund='SOXX' ORDER BY weight_pct DESC").fetchall()
            state = store.get_state(c, "expense_ratio:SOXX")
            got_er = etf.get_expense_ratio(c, "SOXX")
            store_soxx_nport(c)
            lt = etf.lookthrough_companies(c, "SOXX")
            etf.refresh_top(c, "SOXX", now=NOW, top_fn=top_fn)
            n = c.execute("SELECT COUNT(*) FROM etf_holdings WHERE source='yfinance_top'").fetchone()[0]
    r.eq("top10: called with the fund", calls[:1], ["SOXX"])
    r.eq("top10: 10 rows", (res["rows"], len(rows)), (10, 10))
    r.eq("top10: source yfinance_top, as_of the ET date", {(x[3], x[4]) for x in rows},
         {("yfinance_top", "2026-10-28")})
    r.eq("top10: as_of in the result", res["as_of"], "2026-10-28")
    r.eq("top10: INTC first, weight in percent", (rows[0][0], rows[0][1], rows[0][2]),
         ("INTC", "Intel Corp", 9.591231))
    r.eq("top10: LRCX weight", [round(x[2], 4) for x in rows if x[0] == "LRCX"], [3.8903])
    r.ok("top10: expense ratio 0.33%", abs(res["expense_ratio"] - 0.33) < 1e-6, res["expense_ratio"])
    st = json.loads(state) if state else {}
    r.ok("top10: expense ratio stored", abs(st.get("pct", 0) - 0.33) < 1e-6 and
         st.get("as_of") == "2026-10-28", state)
    r.eq("top10: get_expense_ratio reads it", got_er, st)
    r.eq("top10: the newer as_of wins (yfinance over N-PORT)",
         [(x["ticker"], round(x["weight_pct"], 2), x["source"]) for x in lt],
         [("INTC", 9.59, "yfinance_top"), ("AMD", 9.28, "yfinance_top"),
          ("MU", 7.93, "yfinance_top"), ("NVDA", 7.39, "yfinance_top"),
          ("AVGO", 6.77, "yfinance_top")])
    r.eq("top10: a second run the same day keeps 10 rows", n, 10)
    with temp_root() as tr:
        led = tr.led()
        with led.connect() as c:
            late = etf.refresh_top(c, "SOXX", now="2026-10-29T02:00:00Z", top_fn=top_fn)
    r.eq("top10: as_of is the ET date (22:00 ET on 10-28)", late["as_of"], "2026-10-28")


# --- decision wall (§14.10 W2) -----------------------------------------------------

def case_wall():
    text = (REPO / "lib" / "desk" / "etf.py").read_text(encoding="utf-8")
    words = ("get_portfolio get_equity_positions get_equity_quotes get_equity_orders "
             "review_equity_order place_equity_order cancel_equity_order get_trade_approval_setting "
             "get_trade_approvals get_accounts buying_power remaining_daily_cap RiskConfig "
             "handle_tap apply_change").split()
    r.eq("wall: no broker or limit word", [w for w in words if w in text], [])
    mods = ("lib.sizing", "lib.config", "lib.desk.approvals", "lib.desk.execution",
            "lib.desk.proposals", "lib.desk.tools", "lib.desk.agent", "lib.desk.outbox",
            "lib.telegram", "subprocess")
    imported = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    r.eq("wall: no walled import", sorted(imported & set(mods)), [])


for case in (case_nport, case_five_pct, case_cef, case_weighted, case_top10, case_wall):
    try:
        case()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.ok(f"{case.__name__}: raised", False, f"{type(e).__name__}: {e}")

raise SystemExit(r.done())
