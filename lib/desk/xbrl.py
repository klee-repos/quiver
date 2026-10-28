"""Reported numbers from SEC companyfacts: ingest, concept synonyms, quarters, TTM (plan §7.5).

``upsert_companyfacts`` stores every ``us-gaap``, ``ifrs-full`` and ``dei`` fact in
``xbrl_facts``. The row key is the table's primary key, so a second upsert adds no row.
``ingest_after_filing`` uses a companyfacts file only when it holds facts of the new
accession, and gives up after 10 misses.

The read functions turn the stored facts into metrics:

* Synonyms: each metric has an ordered concept list. Per period, the first concept with a
  fact wins. Inside one concept, the fact with the latest ``filed`` date wins.
* Quarters: a duration fact of 80 to 100 days is a quarter. For a flow concept (revenue,
  gross profit, operating income, net income, OCF, capex, SBC), the code also derives a
  quarter as YTD(n) minus YTD(n-1) of the same fiscal year, and Q4 as FY minus Q3 YTD.
  It never subtracts EPS or share counts (G20).
* TTM: the sum of the last four consecutive quarters, else the last fiscal year.
* Balance sheet: values from the latest balance-sheet date. A debt component with no fact
  on that date counts as 0 (G21).
* Units: money metrics use ``USD`` facts only. A metric with facts in another currency only
  has no value and the reason ``non_usd`` (G22).

This module is on the research side of the decision wall: it reads facts, never limits.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from lib.desk import store

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
TAXONOMIES = ("us-gaap", "ifrs-full", "dei")
MAX_MISSES = 10
SHARES_CONCEPT = ("dei", "EntityCommonStockSharesOutstanding")


def _g(name: str) -> tuple:
    return ("us-gaap", name)


# metric -> ordered (taxonomy, concept) synonyms (§7.5 rule 3).
CONCEPTS = {
    "revenue": [_g("RevenueFromContractWithCustomerExcludingAssessedTax"), _g("Revenues"),
                _g("SalesRevenueNet"), _g("RevenueFromContractWithCustomerIncludingAssessedTax"),
                ("ifrs-full", "Revenue")],
    "gross_profit": [_g("GrossProfit")],
    "operating_income": [_g("OperatingIncomeLoss")],
    "net_income": [_g("NetIncomeLoss"), _g("ProfitLoss"), ("ifrs-full", "ProfitLoss")],
    "ocf": [_g("NetCashProvidedByUsedInOperatingActivities")],
    "capex": [_g("PaymentsToAcquirePropertyPlantAndEquipment"), _g("PaymentsToAcquireProductiveAssets"),
              _g("PaymentsForCapitalImprovements"), _g("PaymentsToDevelopRealEstateAssets")],
    "sbc": [_g("ShareBasedCompensation"), _g("AllocatedShareBasedCompensationExpense")],
    "cash": [_g("CashAndCashEquivalentsAtCarryingValue"),
             _g("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")],
    "st_investments": [_g("ShortTermInvestments"), _g("MarketableSecuritiesCurrent"),
                       _g("AvailableForSaleSecuritiesDebtSecuritiesCurrent")],
    "shares_outstanding": [SHARES_CONCEPT],
    "diluted_shares": [_g("WeightedAverageNumberOfDilutedSharesOutstanding")],
    "diluted_eps": [_g("EarningsPerShareDiluted")],
}
# The debt components (§7.5 rule 3). Each one is a balance-sheet value on one date.
DEBT_PRIMARY = [_g("LongTermDebt"), _g("ShortTermBorrowings"), _g("CommercialPaper")]
DEBT_FALLBACK = [_g("LongTermDebtNoncurrent"), _g("LongTermDebtCurrent"), _g("ShortTermBorrowings"),
                 _g("CommercialPaper")]
FLOW = frozenset({"revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "sbc"})
UNITS = {"shares_outstanding": "shares", "diluted_shares": "shares", "diluted_eps": "USD/shares"}
BALANCE = ("cash", "st_investments")
# fcf is OCF minus capex, per period. It is not a concept.
DERIVED = {"fcf": ("ocf", "capex")}
# Proxy forms carry the pay-versus-performance table. Its facts can be in millions under a
# "USD" unit (ANET DEF 14A 0001193125-26-158942 tags FY2025 net income as 3511), so the
# metric reads skip them. The upsert still stores them.
SKIP_FORM_PREFIXES = ("DEF", "PRE")
FINANCIAL_KEYS = ("revenue", "gross_profit", "operating_income", "net_income", "ocf", "capex", "fcf",
                  "diluted_shares")


class XbrlError(ValueError):
    """An unknown metric or a bad argument."""


def cik10(cik) -> str:
    """The 10-digit, zero-padded CIK string."""
    return f"{int(str(cik).strip()):010d}"


def companyfacts_url(cik) -> str:
    return COMPANYFACTS_URL.format(cik10=cik10(cik))


# --- ingest --------------------------------------------------------------------------

def _fact_rows(cf: dict) -> list:
    """Every us-gaap, ifrs-full and dei fact as a row tuple, one per primary key.

    Two facts with one key in ``dei:EntityCommonStockSharesOutstanding`` are two share
    classes on one date in one filing: the row holds their sum. For any other concept the
    first fact with a key wins."""
    cik = cik10(cf["cik"])
    rows: dict = {}
    for tax in TAXONOMIES:
        for concept, body in (cf.get("facts", {}).get(tax) or {}).items():
            for unit, facts in (body.get("units") or {}).items():
                for f in facts:
                    if f.get("val") is None or not f.get("end") or not f.get("accn"):
                        continue
                    key = (cik, tax, concept, unit, f.get("start") or "", f["end"], f["accn"])
                    if key in rows:
                        if (tax, concept) == SHARES_CONCEPT:
                            prev = rows[key]
                            rows[key] = prev[:6] + (prev[6] + float(f["val"]),) + prev[7:]
                        continue
                    rows[key] = (cik, tax, concept, unit, key[4], f["end"], float(f["val"]), f["accn"],
                                 f.get("fy"), f.get("fp"), f.get("form"), f.get("filed"), f.get("frame"))
    return list(rows.values())


def upsert_companyfacts(conn, cf: dict) -> int:
    """Store every us-gaap, ifrs-full and dei fact of one companyfacts file. Return the
    number of rows inserted. A fact already stored (same primary key) adds no row."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO xbrl_facts (cik, taxonomy, concept, unit, start_date, end_date, value, "
        "accession, fy, fp, form, filed, frame) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", _fact_rows(cf))
    return conn.total_changes - before


def has_accession(cf: dict, accession: str) -> bool:
    """True when the file holds at least one us-gaap, ifrs-full or dei fact of ``accession``."""
    for tax in TAXONOMIES:
        for body in (cf.get("facts", {}).get(tax) or {}).values():
            for facts in (body.get("units") or {}).values():
                if any(f.get("accn") == accession for f in facts):
                    return True
    return False


def ingest_after_filing(conn, client, *, cik, accession: str, now: str) -> str:
    """Fetch companyfacts after a new XBRL filing and store it when it is current.

    Returns ``ok`` (facts of ``accession`` found and stored), ``waiting`` (not there yet; one
    miss counted under ``bridge_state`` key ``xbrl_wait:<accession>``) or ``gave_up`` (the
    10th miss; ``filings.ingest_error`` is set). A fetch error propagates to the caller."""
    key = f"xbrl_wait:{accession}"
    misses = int(store.get_state(conn, key, "0") or 0)
    if misses >= MAX_MISSES:
        return "gave_up"
    cf = client.get_json(companyfacts_url(cik))
    if isinstance(cf, dict) and has_accession(cf, accession):
        upsert_companyfacts(conn, cf)
        conn.execute("DELETE FROM bridge_state WHERE key=?", (key,))
        return "ok"
    misses += 1
    store.set_state(conn, key, misses)
    if misses >= MAX_MISSES:
        conn.execute("UPDATE filings SET ingest_error=? WHERE accession=?",
                     (f"xbrl: companyfacts has no facts of {accession} after {MAX_MISSES} tries ({now})",
                      accession))
        return "gave_up"
    return "waiting"


# --- reading facts -------------------------------------------------------------------

def _d(s: str) -> date:
    return date.fromisoformat(s)


def _days(start: str, end: str) -> int:
    return (_d(end) - _d(start)).days


def _check(metric: str) -> None:
    if metric not in CONCEPTS and metric not in DERIVED:
        raise XbrlError(f"unknown metric '{metric}'; metrics are: {', '.join(list(CONCEPTS) + list(DERIVED))}")


def _resolve(conn, cik, pairs, unit: str):
    """Per period, the winning fact of the synonym list ``pairs`` in ``unit``.

    Returns ({(start, end): fact}, reason). A fact is a dict with value, fy, fp, concept,
    accession, filed. The value comes from the latest filed fact; fy and fp come from the
    first filing of that period (the filing in which the period is the current one).
    reason is None, ``missing`` (no fact of any synonym) or ``non_usd`` (facts only in
    another currency). Facts of a proxy form (``SKIP_FORM_PREFIXES``) do not count."""
    names = sorted({c for _t, c in pairs})
    marks = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT taxonomy, concept, unit, start_date, end_date, value, accession, fy, fp, filed, form "
        f"FROM xbrl_facts WHERE cik=? AND concept IN ({marks})", (cik10(cik), *names)).fetchall()
    rank = {p: i for i, p in enumerate(pairs)}
    rows = [r for r in rows if (r[0], r[1]) in rank
            and not str(r[10] or "").upper().startswith(SKIP_FORM_PREFIXES)]
    if not rows:
        return {}, "missing"
    in_unit = [r for r in rows if r[2] == unit]
    if not in_unit:
        return {}, ("non_usd" if unit.startswith("USD") else "missing")
    best: dict = {}
    first: dict = {}
    for r in in_unit:
        key = (r[3], r[4])
        cand = (rank[(r[0], r[1])], r[9] or "", r[6])
        cur = best.get(key)
        if cur is None or cand[0] < cur[0][0] or (cand[0] == cur[0][0] and cand[1:] > cur[0][1:]):
            best[key] = (cand, r)
        lab = ((r[0], r[1]), r[9] or "", r[6])
        prev = first.get((key, lab[0]))
        if prev is None or lab[1:] < prev[0]:
            first[(key, lab[0])] = (lab[1:], r[7], r[8])
    out = {}
    for key, (_cand, r) in best.items():
        _f, fy, fp = first[(key, (r[0], r[1]))]
        out[key] = {"value": float(r[5]), "fy": fy, "fp": fp, "concept": f"{r[0]}:{r[1]}",
                    "accession": r[6], "filed": r[9]}
    return out, None


def _unit(metric: str) -> str:
    return UNITS.get(metric, "USD")


def _periods(conn, cik, metric: str):
    return _resolve(conn, cik, CONCEPTS[metric], _unit(metric))


def _q(start: str, end: str, value: float, fy, fp) -> dict:
    return {"start": start, "end": end, "value": value, "fy": fy, "fp": fp}


def _quarters_from(periods: dict, flow: bool) -> list:
    """Quarters from resolved duration periods. A direct 80-100 day fact wins over a
    derived value for the same quarter end."""
    direct = {}
    for (start, end), f in periods.items():
        if start and 80 <= _days(start, end) <= 100:
            prev = direct.get(end)
            if prev is None or start > prev["start"]:
                direct[end] = _q(start, end, f["value"], f["fy"], f["fp"])
    out = dict(direct)
    if flow:
        by_start: dict = {}
        for (start, end), f in periods.items():
            if start and _days(start, end) >= 80:
                by_start.setdefault(start, []).append((end, f))
        for start, items in by_start.items():
            items.sort(key=lambda x: x[0])
            for (e1, f1), (e2, f2) in zip(items, items[1:]):
                if e2 in out or not 80 <= _days(e1, e2) <= 100:
                    continue
                fp = "Q4" if f2["fp"] == "FY" else f2["fp"]
                qstart = (_d(e1) + timedelta(days=1)).isoformat()
                out[e2] = _q(qstart, e2, f2["value"] - f1["value"], f2["fy"], fp)
    return [out[k] for k in sorted(out)]


def _annual_from(periods: dict) -> list:
    """Fiscal-year values: duration facts of 350 to 380 days, oldest first."""
    out = {}
    for (start, end), f in periods.items():
        if start and 350 <= _days(start, end) <= 380:
            prev = out.get(end)
            if prev is None or start > prev["start"]:
                out[end] = _q(start, end, f["value"], f["fy"], f["fp"])
    return [out[k] for k in sorted(out)]


def _combine(a: list, b: list) -> list:
    """Per period end, a minus b (fcf = ocf - capex). Only ends present in both."""
    bmap = {x["end"]: x for x in b}
    return [_q(x["start"], x["end"], x["value"] - bmap[x["end"]]["value"], x["fy"], x["fp"])
            for x in a if x["end"] in bmap and bmap[x["end"]]["start"] == x["start"]]


def _series(conn, cik, metric: str):
    """(quarters, annual, reason) for one metric, derived metrics included."""
    _check(metric)
    if metric in DERIVED:
        a, b = DERIVED[metric]
        qa, ya, ra = _series(conn, cik, a)
        qb, yb, rb = _series(conn, cik, b)
        return _combine(qa, qb), _combine(ya, yb), ra or rb
    periods, reason = _periods(conn, cik, metric)
    return _quarters_from(periods, metric in FLOW), _annual_from(periods), reason


def quarters(conn, cik, metric: str) -> list:
    """Quarter values of one metric, oldest first: ``[{start, end, value, fy, fp}]``."""
    return _series(conn, cik, metric)[0]


def annual(conn, cik, metric: str) -> list:
    """Fiscal-year values of one metric, oldest first: ``[{start, end, value, fy, fp}]``."""
    return _series(conn, cik, metric)[1]


def reason(conn, cik, metric: str) -> Optional[str]:
    """Why a metric has no value: ``missing``, ``non_usd``, or None when it has facts."""
    return _series(conn, cik, metric)[2]


def _consecutive(qs: list) -> bool:
    return all(1 <= (_d(b["start"]) - _d(a["end"])).days <= 7 for a, b in zip(qs, qs[1:]))


def _ttm_at(qs: list, ys: list, end: Optional[str] = None) -> Optional[dict]:
    """TTM ending at quarter ``end`` (the latest when None)."""
    if end is not None:
        qs = [q for q in qs if q["end"] <= end]
        ys = [y for y in ys if y["end"] <= end]
    last_fy = ys[-1] if ys else None
    if len(qs) >= 4 and _consecutive(qs[-4:]) and (end is None or qs[-1]["end"] == end) \
            and (last_fy is None or last_fy["end"] <= qs[-1]["end"]):
        four = qs[-4:]
        return {"value": sum(q["value"] for q in four), "basis": "quarters",
                "periods": [{"start": q["start"], "end": q["end"]} for q in four]}
    if last_fy is not None and (end is None or last_fy["end"] == end):
        return {"value": last_fy["value"], "basis": "fy",
                "periods": [{"start": last_fy["start"], "end": last_fy["end"]}]}
    return None


def ttm(conn, cik, metric: str) -> Optional[dict]:
    """Trailing twelve months: ``{value, periods, basis}``, or None without facts.
    ``basis`` is ``quarters`` (four consecutive quarters) or ``fy`` (the last fiscal year)."""
    qs, ys, _r = _series(conn, cik, metric)
    return _ttm_at(qs, ys)


def _year_before(end: str, ends: list) -> Optional[str]:
    """The period end in ``ends`` that lies 355 to 375 days before ``end``."""
    for e in reversed(ends):
        if 355 <= (_d(end) - _d(e)).days <= 375:
            return e
    return None


def ttm_year_ago(conn, cik, metric: str) -> Optional[dict]:
    """The TTM that ends one year before the current TTM, on the same basis: four
    quarters for a quarter TTM, the previous fiscal year for a fiscal-year TTM."""
    qs, ys, _r = _series(conn, cik, metric)
    cur = _ttm_at(qs, ys)
    if cur is None:
        return None
    end = cur["periods"][-1]["end"]
    if cur["basis"] == "fy":
        prior = _year_before(end, [y["end"] for y in ys])
        fy = next((y for y in ys if y["end"] == prior), None)
        return ({"value": fy["value"], "basis": "fy", "periods": [{"start": fy["start"], "end": fy["end"]}]}
                if fy else None)
    prior = _year_before(end, [q["end"] for q in qs])
    if prior is None:
        return None
    got = _ttm_at(qs, ys, prior)
    return got if got and got["basis"] == "quarters" else None


def quarter_year_ago(conn, cik, metric: str) -> Optional[tuple]:
    """(latest quarter, the same quarter one year earlier), or None."""
    qs = quarters(conn, cik, metric)
    if not qs:
        return None
    last = qs[-1]
    prior = _year_before(last["end"], [q["end"] for q in qs])
    if prior is None:
        return None
    return last, next(q for q in qs if q["end"] == prior)


# --- balance sheet and shares --------------------------------------------------------

def _instants(conn, cik, pairs) -> dict:
    periods, _r = _resolve(conn, cik, pairs, "USD")
    return {end: f for (start, end), f in periods.items() if not start}


def balance(conn, cik) -> dict:
    """Cash, short-term investments and debt on the latest balance-sheet date.

    ``as_of`` is the latest date with a USD instant fact of a cash, short-term investment
    or debt concept. A value with no fact on that date is None; a debt component with no
    fact on that date counts as 0, so ``debt`` is 0 when no component has one."""
    groups = {k: _instants(conn, cik, CONCEPTS[k]) for k in BALANCE}
    debt_facts = {p: _instants(conn, cik, [p]) for p in DEBT_PRIMARY + DEBT_FALLBACK}
    dates = set()
    for g in list(groups.values()) + list(debt_facts.values()):
        dates.update(g)
    if not dates:
        return {"cash": None, "st_investments": None, "debt": None, "as_of": None}
    as_of = max(dates)
    out = {k: (groups[k][as_of]["value"] if as_of in groups[k] else None) for k in BALANCE}
    comps = DEBT_PRIMARY if as_of in debt_facts[DEBT_PRIMARY[0]] else DEBT_FALLBACK
    out["debt"] = sum(debt_facts[p][as_of]["value"] for p in comps if as_of in debt_facts[p])
    out["as_of"] = as_of
    return out


def shares_outstanding(conn, cik) -> Optional[dict]:
    """dei shares outstanding on the latest cover-page date: ``{value, date}``. The share
    classes of one date are summed at ingest. None when the company has no dei fact."""
    periods, _r = _periods(conn, cik, "shares_outstanding")
    inst = {end: f for (start, end), f in periods.items() if not start}
    if not inst:
        return None
    end = max(inst)
    return {"value": inst[end]["value"], "date": end}


def diluted_shares(conn, cik) -> Optional[dict]:
    """Weighted diluted shares of the latest quarter, else of the latest fiscal year:
    ``{value, start, end}``. Never derived by subtraction."""
    qs, ys, _r = _series(conn, cik, "diluted_shares")
    cands = ([qs[-1]] if qs else []) + ([ys[-1]] if ys else [])
    if not cands:
        return None
    best = max(cands, key=lambda p: (p["end"], -_days(p["start"], p["end"])))
    return {"value": best["value"], "start": best["start"], "end": best["end"]}


def latest_accession(conn, cik) -> Optional[str]:
    """The accession of the latest filed report of a company (proxy forms skipped)."""
    rows = conn.execute("SELECT accession, form FROM xbrl_facts WHERE cik=? GROUP BY accession "
                        "ORDER BY MAX(filed) DESC, accession DESC", (cik10(cik),)).fetchall()
    for acc, form in rows:
        if not str(form or "").upper().startswith(SKIP_FORM_PREFIXES):
            return acc
    return None


# --- the desk table ------------------------------------------------------------------

def financials(conn, cik, period: str = "quarterly", limit: int = 8) -> list:
    """The ``get_financials`` table: one row per quarter or fiscal year, latest first, with
    revenue, gross profit, operating income, net income, OCF, capex, FCF and diluted shares.
    A value with no fact is None."""
    if period not in ("quarterly", "annual"):
        raise XbrlError("period must be quarterly or annual")
    limit = int(limit)
    if limit < 1 or limit > 12:
        raise XbrlError("limit must be 1 to 12")
    pick = 0 if period == "quarterly" else 1
    cols = {k: {p["end"]: p for p in _series(conn, cik, k)[pick]} for k in FINANCIAL_KEYS}
    ends = sorted({e for c in cols.values() for e in c}, reverse=True)[:limit]
    rows = []
    for end in ends:
        anchor = next(c[end] for c in cols.values() if end in c)
        row = {"start": anchor["start"], "end": end, "fy": anchor["fy"], "fp": anchor["fp"]}
        for k in FINANCIAL_KEYS:
            row[k] = cols[k][end]["value"] if end in cols[k] else None
        rows.append(row)
    return rows
