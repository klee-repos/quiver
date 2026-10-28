"""test_desk_valuation: §8 metrics, reverse DCF and fair values on the real ``cf_anet`` facts.

Every input comes through the producers: ``xbrl.upsert_companyfacts`` ingests ``cf_anet``,
the fake yfinance price is $177.20 (the Robinhood market cap $223.49B of 2026-10-26 divided
by 1,261,224,648 shares), and ``compute_metrics`` builds the row. The findings that back
the scenarios are real ``10q_q2`` MD&A sentences, stored with ``store.store_document`` and
``findings.record_finding``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import Results, fx, temp_root  # noqa: E402

from lib.desk import findings, sections, store, valuation, xbrl  # noqa: E402
from lib.desk.valuation import ValuationError  # noqa: E402

r = Results("desk-valuation")
CF = fx("cf_anet")
CIK = xbrl.cik10(CF["cik"])
NOW = "2026-10-27T18:40:00Z"
PRICE = 177.20
S = {"valuation_discount_rate": 0.09, "valuation_terminal_growth": 0.03, "valuation_years": 10,
     "margin_of_safety_pct": 15}
MD = dict((k, (t, x)) for k, t, x in sections.split_sections(sections.html_to_text(fx("10q_q2")), "10-Q"))["10-Q:I-2"]
GROSS = ("Gross margin decreased to 62.9% and 62.4% for the three and six months ended June 30, 2026, "
         "compared to 65.2% and 64.5% for the same periods in 2025.")
CENTERS = ("Our “Centers of Data” strategy is a fundamental pivot from legacy, siloed networking to a "
           "unified, data-driven approach")


def pv(fcf0, g, r_, gt, n):
    """§8.2 written out: sum of FCF0 (1+g)^t / (1+r)^t plus TV / (1+r)^N."""
    total = sum(fcf0 * (1 + g) ** t / (1 + r_) ** t for t in range(1, n + 1))
    tv = fcf0 * (1 + g) ** n * (1 + gt) / (r_ - gt)
    return total + tv / (1 + r_) ** n


def dcf_loop(fcf0, g15, g610, r_, gt, n):
    """The two-stage DCF, one year at a time."""
    f, total = fcf0, 0.0
    for t in range(1, n + 1):
        f *= 1 + (g15 if t <= 5 else g610)
        total += f / (1 + r_) ** t
    return total + f * (1 + gt) / (r_ - gt) / (1 + r_) ** n


def setup(conn):
    """Ingest cf_anet, compute the metrics row, store the MD&A and two findings."""
    xbrl.upsert_companyfacts(conn, CF)
    m = valuation.compute_metrics(conn, "ANET", cik=CIK, settings=S, now=NOW, price_fn=lambda t: PRICE,
                                  technicals_fn=lambda t: {"regime": "UPTREND"},
                                  next_earnings_fn=lambda t: "2026-11-04")
    doc = store.store_document(conn, kind="filing_section", title=MD[0], text=MD[1], now=NOW, ticker="ANET",
                               accession="0001596532-26-000175", section_key="10-Q:I-2")
    f1 = findings.record_finding(conn, ticker="ANET", kind="margin", claim="Gross margin fell to 62.9%.",
                                 quote=GROSS, document_id=doc, created_by="desk", now=NOW)
    f2 = findings.record_finding(conn, ticker="ANET", kind="growth", claim="Centers of Data strategy.",
                                 quote=CENTERS, document_id=doc, created_by="desk", now=NOW)
    return m, f1, f2


def three(base: dict, f, bear=None, bull=None) -> dict:
    return {"bear": {**base, **(bear or {}), "finding_ids": [f]}, "base": {**base, "finding_ids": [f]},
            "bull": {**base, **(bull or {}), "finding_ids": [f]}}


with temp_root() as root:
    with root.led().connect() as c:
        m, F1, F2 = setup(c)

        # --- rdcf_pin ---------------------------------------------------------------
        r.eq("rdcf_pin: fcf_ttm", round(m["fcf_ttm"]), 5_155_300_000)
        r.ok("rdcf_pin: market_cap 223,489,007,625.60", abs(m["market_cap"] - 223_489_007_625.60) < 0.01, m["market_cap"])
        r.eq("rdcf_pin: cash, st_investments, debt", (round(m["cash"]), round(m["st_investments"]), m["debt"]),
             (2_290_200_000, 11_053_100_000, 0))
        r.ok("rdcf_pin: ev 210,145,707,625.60", abs(m["ev"] - 210_145_707_625.60) < 0.01, m["ev"])
        g = m["reverse_dcf_implied_growth"]
        r.ok("rdcf_pin: g = 0.143193 +- 2e-6", abs(g - 0.143193) <= 2e-6, g)
        r.eq("rdcf_pin: reverse_dcf gives the same g", valuation.reverse_dcf(m["ev"], m["fcf_ttm"], 0.09, 0.03, 10),
             (g, None))
        r.ok("rdcf_pin: the test's PV loop at g equals ev within $1M", abs(pv(m["fcf_ttm"], g, 0.09, 0.03, 10) - m["ev"]) < 1e6,
             pv(m["fcf_ttm"], g, 0.09, 0.03, 10) - m["ev"])

        # --- rdcf_rates -------------------------------------------------------------
        for rate, want in ((0.08, 0.116438), (0.10, 0.167472)):
            mm = valuation.compute_metrics(c, "ANET", cik=CIK, settings={**S, "valuation_discount_rate": rate}, now=NOW,
                                           price_fn=lambda t: PRICE, store=False)
            got = mm["reverse_dcf_implied_growth"]
            r.ok(f"rdcf_rates: valuation_discount_rate {rate} -> {want}", abs(got - want) <= 1e-6, got)

        # --- rdcf_null (G26) --------------------------------------------------------
        r.eq("rdcf_null: fcf 0 -> negative_fcf", valuation.reverse_dcf(m["ev"], 0, 0.09, 0.03, 10), (None, "negative_fcf"))
        r.eq("rdcf_null: fcf -1 -> negative_fcf", valuation.reverse_dcf(m["ev"], -1, 0.09, 0.03, 10), (None, "negative_fcf"))
        top = pv(m["fcf_ttm"], 1.0, 0.09, 0.03, 10)
        r.eq("rdcf_null: ev above PV(g=1.0) -> no_solution, not 1.0",
             valuation.reverse_dcf(top * 1.01, m["fcf_ttm"], 0.09, 0.03, 10), (None, "no_solution"))
        r.eq("rdcf_null: ev below PV(g=-0.5) -> no_solution",
             valuation.reverse_dcf(pv(m["fcf_ttm"], -0.5, 0.09, 0.03, 10) * 0.99, m["fcf_ttm"], 0.09, 0.03, 10),
             (None, "no_solution"))
        rich = valuation.compute_metrics(c, "ANET", cik=CIK, settings=S, now=NOW, price_fn=lambda t: 1_000_000.0,
                                         store=False)
        r.ok("rdcf_null: a price that puts ev above PV(1.0) stores null", rich["ev"] > top
             and rich["reverse_dcf_implied_growth"] is None, rich["reverse_dcf_implied_growth"])

        # --- metrics ----------------------------------------------------------------
        r.eq("metrics: exactly the §8.1 keys", set(m), set(valuation.METRIC_KEYS))
        r.eq("metrics: 24 keys", len(valuation.METRIC_KEYS), 24)
        pins = {"revenue_growth_yoy": (0.3257, 4), "revenue_growth_q_yoy": (0.3769, 4), "fcf_margin_ttm": (0.4891, 4),
                "ev_sales": (19.94, 2), "fcf_yield": (0.0231, 4), "rule_of_40": (81.5, 1), "dilution_yoy": (0.0038, 4)}
        for k, (want, nd) in pins.items():
            r.eq(f"metrics: {k} {want}", round(m[k], nd), want)
        r.ok("metrics: pe_ttm set", m["pe_ttm"] is not None and abs(m["pe_ttm"] - m["market_cap"] / 4_044_600_000) < 1e-6,
             m["pe_ttm"])
        r.eq("metrics: the seams fill technicals and next_earnings", (m["technicals"], m["next_earnings"]),
             ({"regime": "UPTREND"}, "2026-11-04"))
        row = c.execute("SELECT * FROM metrics WHERE ticker='ANET'").fetchall()
        r.eq("metrics: one row (store=False writes none)", len(row), 1)
        r.eq("metrics: the row", (row[0]["as_of"], row[0]["price"], row[0]["source_accession"]),
             (NOW, PRICE, "0001596532-26-000175"))
        r.eq("metrics: metrics_json holds the returned dict", json.loads(row[0]["metrics_json"]), m)
        bad = valuation.compute_metrics(c, "ANET", cik=CIK, settings=S, now=NOW, price_fn=lambda t: 1 / 0,
                                        technicals_fn=lambda t: 1 / 0, store=False)
        r.ok("metrics: a seam that raises leaves its value null",
             bad["market_cap"] is None and bad["ev"] is None and bad["technicals"] is None
             and bad["fcf_ttm"] is not None, bad)

        # --- dcf_fcf ----------------------------------------------------------------
        sc = three({"growth_1_5": 0.25, "growth_6_10": 0.12}, F1, bear={"growth_1_5": 0.10, "growth_6_10": 0.05},
                   bull={"growth_1_5": 0.35, "growth_6_10": 0.15})
        fv = valuation.fair_values(c, "ANET", {"method": "dcf_fcf", "scenarios": sc}, settings=S, cik=CIK)
        cash, st, debt, fcf = 2_290_200_000, 11_053_100_000, 0, m["fcf_ttm"]
        want_base = (dcf_loop(fcf, 0.25, 0.12, 0.09, 0.03, 10) - debt + cash + st) / 1_276_000_000
        r.ok("dcf_fcf: base = (DCF EV - debt + cash + st) / 1,276,000,000 (1e-9)",
             abs(fv["fair_value_base"] - want_base) < 1e-9, (fv["fair_value_base"], want_base))
        r.ok("dcf_fcf: bear and bull", abs(fv["fair_value_low"] - (dcf_loop(fcf, .10, .05, .09, .03, 10) + cash + st)
                                           / 1_276_000_000) < 1e-9
             and abs(fv["fair_value_high"] - (dcf_loop(fcf, .35, .15, .09, .03, 10) + cash + st) / 1_276_000_000) < 1e-9, fv)
        own = three({"growth_1_5": 0.25, "growth_6_10": 0.12, "discount_rate": 0.10, "terminal_growth": 0.02}, F1)
        fv2 = valuation.fair_values(c, "ANET", {"method": "dcf_fcf", "scenarios": own}, settings=S, cik=CIK)
        r.ok("dcf_fcf: a scenario's own discount_rate and terminal_growth win",
             abs(fv2["fair_value_base"] - (dcf_loop(fcf, .25, .12, .10, .02, 10) + cash + st) / 1_276_000_000) < 1e-9, fv2)

        # --- ev_sales ---------------------------------------------------------------
        sc = three({"revenue_cagr_5y": 0.20, "exit_ev_sales": 12}, F2)
        fv = valuation.fair_values(c, "ANET", {"method": "ev_sales", "scenarios": sc}, settings=S, cik=CIK)
        rev = 10_540_800_000
        want = (rev * 1.20 ** 5 * 12 / 1.09 ** 5 - debt + cash + st) / 1_276_000_000
        r.ok("ev_sales: base = the §8.3 formula", abs(fv["fair_value_base"] - want) < 1e-6, (fv["fair_value_base"], want))

        # --- multiple ---------------------------------------------------------------
        r.ok("multiple: the finding quote holds 62.9", "62.9" in findings.get_finding(c, F1)["quote"])
        sc = {"bear": {"metric_name": "gross_margin_pct", "metric_value": 62.9, "finding_id": F1, "multiple": 1.5},
              "base": {"metric_name": "gross_margin_pct", "metric_value": 62.9, "finding_id": F1, "multiple": 2.0},
              "bull": {"metric_name": "gross_margin_pct", "metric_value": 62.9, "finding_id": F1, "multiple": 2.5}}
        fv = valuation.fair_values(c, "ANET", {"method": "multiple", "scenarios": sc}, settings=S, cik=CIK)
        r.ok("multiple: metric_value x multiple", (abs(fv["fair_value_low"] - 62.9 * 1.5) < 1e-9
                                                   and abs(fv["fair_value_base"] - 62.9 * 2.0) < 1e-9
                                                   and abs(fv["fair_value_high"] - 62.9 * 2.5) < 1e-9), fv)
        no = {k: {**v, "metric_value": 3.45} for k, v in sc.items()}
        r.raises("multiple: 3.45 is not in the quote -> refused",
                 lambda: valuation.fair_values(c, "ANET", {"method": "multiple", "scenarios": no}, settings=S, cik=CIK),
                 ValuationError, "does not contain metric_value 3.45")
        r.raises("multiple: the finding must hold the value, not another finding",
                 lambda: valuation.fair_values(c, "ANET", {"method": "multiple", "scenarios": {
                     k: {**v, "finding_id": F2} for k, v in sc.items()}}, settings=S, cik=CIK),
                 ValuationError, "does not contain")

        # --- evidence ---------------------------------------------------------------
        sc = three({"growth_1_5": 0.25, "growth_6_10": 0.12}, F1)
        sc["bull"].pop("finding_ids")
        r.raises("evidence: a scenario with no finding id -> refused",
                 lambda: valuation.fair_values(c, "ANET", {"method": "dcf_fcf", "scenarios": sc}, settings=S, cik=CIK),
                 ValuationError, "scenario 'bull' cites no finding")
        sc["bull"]["finding_ids"] = [9999]
        r.raises("evidence: a finding id that does not exist -> refused",
                 lambda: valuation.fair_values(c, "ANET", {"method": "dcf_fcf", "scenarios": sc}, settings=S, cik=CIK),
                 ValuationError, "finding 9999")
        r.raises("evidence: a missing scenario -> refused",
                 lambda: valuation.fair_values(c, "ANET", {"method": "dcf_fcf", "scenarios": {"base": sc["base"]}},
                                               settings=S, cik=CIK), ValuationError, "scenario 'bear' is missing")
        r.raises("evidence: an unknown method -> refused",
                 lambda: valuation.fair_values(c, "ANET", {"method": "dcf_ebitda", "scenarios": sc}, settings=S, cik=CIK),
                 ValuationError, "unknown method")
        pl = valuation.fair_values(c, "BOT", {"method": "price_levels", "reason": "a cef has no fair value"},
                                   settings=S, cik=CIK)
        r.eq("evidence: price_levels gives no fair value", pl, {"fair_value_low": None, "fair_value_base": None,
                                                               "fair_value_high": None, "buy_below": None,
                                                               "trim_above": None})
        pl = valuation.fair_values(c, "BOT", {"method": "price_levels", "reason": "nav discount",
                                              "buy_below": 20.5, "trim_above": 31}, settings=S, cik=CIK)
        r.eq("evidence: price_levels keeps the given levels", (pl["buy_below"], pl["trim_above"], pl["fair_value_base"]),
             (20.5, 31.0, None))
        r.raises("evidence: price_levels needs a reason",
                 lambda: valuation.fair_values(c, "BOT", {"method": "price_levels"}, settings=S, cik=CIK),
                 ValuationError, "reason")

        # --- levels -----------------------------------------------------------------
        sc = three({"growth_1_5": 0.25, "growth_6_10": 0.12}, F1, bear={"growth_1_5": 0.10, "growth_6_10": 0.05},
                   bull={"growth_1_5": 0.35, "growth_6_10": 0.15})
        v = {"method": "dcf_fcf", "scenarios": sc}
        fv = valuation.fair_values(c, "ANET", v, settings=S, cik=CIK)
        r.ok("levels: buy_below = base x 0.85", abs(fv["buy_below"] - fv["fair_value_base"] * 0.85) < 1e-9, fv)
        r.eq("levels: trim_above = high", fv["trim_above"], fv["fair_value_high"])
        fv20 = valuation.fair_values(c, "ANET", v, settings={**S, "margin_of_safety_pct": 20}, cik=CIK)
        r.ok("levels: margin_of_safety_pct 20 -> base x 0.80", abs(fv20["buy_below"] - fv20["fair_value_base"] * 0.80) < 1e-9,
             fv20)
        given = valuation.fair_values(c, "ANET", {**v, "buy_below": 101.0, "trim_above": 250.0}, settings=S, cik=CIK)
        r.eq("levels: caller-given levels win", (given["buy_below"], given["trim_above"]), (101.0, 250.0))
        before = c.total_changes
        counts = [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("metrics", "theses", "findings")]
        rv = valuation.run_valuation(c, "ANET", "dcf_fcf", sc, settings=S, cik=CIK)
        after = [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("metrics", "theses", "findings")]
        r.eq("levels: run_valuation writes no row", (c.total_changes - before, after), (0, counts))
        r.eq("levels: run_valuation gives the same fair values",
             {k: rv[k] for k in fv}, fv)
        r.ok("levels: run_valuation reports the reverse DCF of the metrics row",
             rv["reverse_dcf"]["implied_growth"] == m["reverse_dcf_implied_growth"] and rv["reverse_dcf"]["reason"] is None,
             rv["reverse_dcf"])
        r.eq("levels: run_valuation shows the XBRL inputs",
             (rv["inputs"]["diluted_shares"], round(rv["inputs"]["fcf_ttm"])), (1_276_000_000, 5_155_300_000))
        r.raises("levels: run_valuation refuses a scenario with no finding",
                 lambda: valuation.run_valuation(c, "ANET", "ev_sales", {
                     "bear": {"revenue_cagr_5y": 0.1, "exit_ev_sales": 8}, "base": sc["base"], "bull": sc["bull"]},
                     settings=S, cik=CIK), ValuationError, "cites no finding")

raise SystemExit(r.done())
