"""Valuation and the metrics row: deterministic Python over XBRL facts (plan §8).

The model gives scenario inputs and cites findings for them. This module does every
calculation:

* ``compute_metrics`` writes one ``metrics`` row with the §8.1 keys.
* ``reverse_dcf`` finds the FCF growth that makes the DCF value equal today's EV (§8.2).
* ``fair_values`` turns a thesis valuation (bear, base, bull) into fair values and default
  levels (§8.3). It refuses a scenario that cites no stored finding.
* ``run_valuation`` gives the same results to the desk and stores nothing.

The caller passes ``settings`` as a plain dict with the keys ``valuation_discount_rate``,
``valuation_terminal_growth``, ``valuation_years`` and ``margin_of_safety_pct``. This module
is on the research side of the decision wall: it reads facts, never limits.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable, Optional

from lib.desk import store as desk_store
from lib.desk import xbrl

METRIC_KEYS = (
    "revenue_ttm", "revenue_growth_yoy", "revenue_growth_q_yoy", "gross_margin_ttm",
    "operating_margin_ttm", "net_margin_ttm", "ocf_ttm", "capex_ttm", "fcf_ttm", "fcf_margin_ttm",
    "sbc_ttm", "market_cap", "cash", "st_investments", "debt", "ev", "ev_sales", "fcf_yield",
    "pe_ttm", "rule_of_40", "dilution_yoy", "reverse_dcf_implied_growth", "next_earnings", "technicals",
)
METHODS = ("dcf_fcf", "ev_sales", "multiple", "price_levels")
SCENARIOS = ("bear", "base", "bull")
G_LOW, G_HIGH, G_TOL = -0.5, 1.0, 1e-6
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")


class ValuationError(ValueError):
    """A valuation the desk must fix: a missing input, a missing finding, or a bad method."""


# --- discounted cash flow ------------------------------------------------------------

def _pv(fcfs: list, r: float, gt: float) -> float:
    """PV of yearly cash flows plus a Gordon terminal value on the last one."""
    if r <= gt:
        raise ValuationError(f"discount rate {r:g} must be above terminal growth {gt:g}")
    n = len(fcfs)
    pv = sum(f / (1 + r) ** t for t, f in enumerate(fcfs, start=1))
    tv = fcfs[-1] * (1 + gt) / (r - gt)
    return pv + tv / (1 + r) ** n


def dcf_ev(fcf0: float, growth_1_5: float, growth_6_10: float, r: float, gt: float, n: int) -> float:
    """DCF enterprise value: FCF grows at ``growth_1_5`` in years 1-5 and at ``growth_6_10``
    in years 6-n, then a terminal value at ``gt``, all discounted at ``r``."""
    n = int(n)
    if n < 1:
        raise ValuationError("valuation_years must be at least 1")
    fcfs, f = [], float(fcf0)
    for t in range(1, n + 1):
        f *= 1 + (growth_1_5 if t <= 5 else growth_6_10)
        fcfs.append(f)
    return _pv(fcfs, r, gt)


def _pv_constant(fcf0: float, g: float, r: float, gt: float, n: int) -> float:
    return dcf_ev(fcf0, g, g, r, gt, n)


def reverse_dcf(ev, fcf0, r: float, gt: float, n: int):
    """The growth g in [-0.5, 1.0] at which the DCF value equals ``ev``, by bisection to
    1e-6. Returns (g, None), or (None, reason): ``negative_fcf`` when FCF0 <= 0,
    ``no_solution`` when no root lies in the interval (G26), ``missing_input`` without EV."""
    if fcf0 is None or float(fcf0) <= 0:
        return None, "negative_fcf"
    if ev is None:
        return None, "missing_input"
    ev, fcf0 = float(ev), float(fcf0)
    lo, hi = G_LOW, G_HIGH
    f_lo = _pv_constant(fcf0, lo, r, gt, n) - ev
    f_hi = _pv_constant(fcf0, hi, r, gt, n) - ev
    if f_lo > 0 or f_hi < 0:
        return None, "no_solution"
    while hi - lo > G_TOL:
        mid = (lo + hi) / 2
        if _pv_constant(fcf0, mid, r, gt, n) - ev < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, None


# --- the metrics row -----------------------------------------------------------------

def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _same(a: Optional[dict], b: Optional[dict]) -> bool:
    return bool(a and b and a["periods"] == b["periods"])


def _ratio_ttm(num: Optional[dict], den: Optional[dict]):
    """num / den only when both TTMs cover the same periods."""
    return _div(num["value"], den["value"]) if _same(num, den) else None


def _safe(fn: Optional[Callable], *args):
    if fn is None:
        return None
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - a feed outage leaves the value empty
        return None


def _rates(settings: dict) -> tuple:
    try:
        return (float(settings["valuation_discount_rate"]), float(settings["valuation_terminal_growth"]),
                int(settings["valuation_years"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ValuationError(f"settings need valuation_discount_rate, valuation_terminal_growth "
                             f"and valuation_years: {e}")


def _now(now) -> str:
    return desk_store.iso(now) if isinstance(now, datetime) else str(now)


def compute_metrics(conn, ticker: str, *, cik, settings: dict, now, price_fn: Callable,
                    market_cap_fn: Optional[Callable] = None, technicals_fn: Optional[Callable] = None,
                    next_earnings_fn: Optional[Callable] = None, store: bool = True) -> dict:
    """Compute the §8.1 metrics of one company and (with ``store``) write one ``metrics`` row.

    Market cap is ``price_fn(ticker)`` x dei shares outstanding; without a dei fact (SPCX)
    or a price it is ``market_cap_fn(ticker)``. ``technicals_fn`` and ``next_earnings_fn`` give the
    last two keys. A seam that raises leaves its value None. Returns the metrics dict.
    The row keeps no null reason; ``run_valuation`` reports the reverse DCF reason."""
    ticker = str(ticker).upper()
    if not cik:
        raise ValuationError(f"compute_metrics needs the CIK of {ticker}")
    r, gt, n = _rates(settings)
    t = {k: xbrl.ttm(conn, cik, k) for k in ("revenue", "gross_profit", "operating_income", "net_income",
                                             "ocf", "capex", "fcf", "sbc")}
    val = {k: (v["value"] if v else None) for k, v in t.items()}
    rev_prior = xbrl.ttm_year_ago(conn, cik, "revenue")
    rev_q = xbrl.quarter_year_ago(conn, cik, "revenue")
    dil_q = xbrl.quarter_year_ago(conn, cik, "diluted_shares")
    bal = xbrl.balance(conn, cik)
    price = _safe(price_fn, ticker)
    price = float(price) if price is not None else None
    shares = xbrl.shares_outstanding(conn, cik)
    if shares and price is not None:
        market_cap = price * shares["value"]
    else:
        mc = _safe(market_cap_fn, ticker)
        market_cap = float(mc) if mc is not None else None
    debt, cash, st = bal["debt"], bal["cash"], bal["st_investments"]
    ev = market_cap + (debt or 0) - (cash or 0) - (st or 0) if market_cap is not None else None
    growth = None
    if rev_prior and val["revenue"] is not None and rev_prior["value"] > 0:
        growth = val["revenue"] / rev_prior["value"] - 1
    fcf_margin = _ratio_ttm(t["fcf"], t["revenue"])
    g, _why = reverse_dcf(ev, val["fcf"], r, gt, n)
    m = {
        "revenue_ttm": val["revenue"],
        "revenue_growth_yoy": growth,
        "revenue_growth_q_yoy": (_div(rev_q[0]["value"], rev_q[1]["value"]) - 1
                                 if rev_q and rev_q[1]["value"] > 0 else None),
        "gross_margin_ttm": _ratio_ttm(t["gross_profit"], t["revenue"]),
        "operating_margin_ttm": _ratio_ttm(t["operating_income"], t["revenue"]),
        "net_margin_ttm": _ratio_ttm(t["net_income"], t["revenue"]),
        "ocf_ttm": val["ocf"],
        "capex_ttm": val["capex"],
        "fcf_ttm": val["fcf"],
        "fcf_margin_ttm": fcf_margin,
        "sbc_ttm": val["sbc"],
        "market_cap": market_cap,
        "cash": cash,
        "st_investments": st,
        "debt": debt,
        "ev": ev,
        "ev_sales": _div(ev, val["revenue"]) if (val["revenue"] or 0) > 0 else None,
        "fcf_yield": _div(val["fcf"], market_cap),
        "pe_ttm": _div(market_cap, val["net_income"]) if (val["net_income"] or 0) > 0 else None,
        "rule_of_40": (growth * 100 + fcf_margin * 100) if growth is not None and fcf_margin is not None
        else None,
        "dilution_yoy": (_div(dil_q[0]["value"], dil_q[1]["value"]) - 1
                         if dil_q and dil_q[1]["value"] > 0 else None),
        "reverse_dcf_implied_growth": g,
        "next_earnings": _safe(next_earnings_fn, ticker),
        "technicals": _safe(technicals_fn, ticker),
    }
    if store:
        conn.execute(
            "INSERT OR REPLACE INTO metrics (ticker, as_of, source_accession, price, metrics_json) "
            "VALUES (?,?,?,?,?)",
            (ticker, _now(now), xbrl.latest_accession(conn, cik), price, json.dumps(m, sort_keys=True)))
    return m


def latest_metrics(conn, ticker: str) -> Optional[dict]:
    """The latest ``metrics`` row: ``{ticker, as_of, source_accession, price, metrics}``."""
    row = conn.execute("SELECT * FROM metrics WHERE ticker=? ORDER BY as_of DESC LIMIT 1",
                       (str(ticker).upper(),)).fetchone()
    if not row:
        return None
    return {"ticker": row["ticker"], "as_of": row["as_of"], "source_accession": row["source_accession"],
            "price": row["price"], "metrics": desk_store.loads(row["metrics_json"], {})}


# --- fair values (§8.3) --------------------------------------------------------------

def _num(sc: dict, key: str, name: str, default=None) -> float:
    v = sc.get(key, default)
    if v is None or isinstance(v, bool):
        raise ValuationError(f"scenario '{name}' needs a number for '{key}'")
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValuationError(f"scenario '{name}': '{key}' must be a number, got {v!r}")


def _finding_ids(sc: dict) -> list:
    """The finding ids a scenario cites: ``finding_ids``, ``finding_id``, or an ``evidence``
    list of ids or ``finding:N`` strings (the form the analyst prompt names)."""
    ids = list(sc.get("finding_ids") or [])
    if sc.get("finding_id") is not None:
        ids.append(sc["finding_id"])
    for e in sc.get("evidence") or []:
        if isinstance(e, str) and e.startswith("finding:"):
            e = e.split(":", 1)[1]
        elif isinstance(e, dict):
            e = e.get("finding", e.get("id"))
        ids.append(e)
    out = []
    for i in ids:
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            raise ValuationError(f"finding id {i!r} is not a number")
    return out


def _check_evidence(conn, name: str, sc: dict) -> list:
    """The scenario's finding ids. Refuse a scenario with no stored finding."""
    ids = _finding_ids(sc)
    if not ids:
        raise ValuationError(f"scenario '{name}' cites no finding; add finding_ids from record_finding")
    for i in ids:
        if not conn.execute("SELECT 1 FROM findings WHERE id=?", (i,)).fetchone():
            raise ValuationError(f"scenario '{name}' cites finding {i}, which does not exist")
    return ids


def quote_has_value(quote: str, value: float) -> bool:
    """True when a number in ``quote`` equals ``value`` (commas ignored)."""
    for tok in _NUMBER.findall(str(quote or "")):
        try:
            if abs(float(tok.replace(",", "")) - float(value)) < 1e-9:
                return True
        except ValueError:
            continue
    return False


def _inputs(conn, cik) -> dict:
    """The XBRL inputs of the fair-value formulas."""
    bal = xbrl.balance(conn, cik)
    fcf = xbrl.ttm(conn, cik, "fcf")
    rev = xbrl.ttm(conn, cik, "revenue")
    dil = xbrl.diluted_shares(conn, cik)
    return {"fcf_ttm": fcf["value"] if fcf else None, "revenue_ttm": rev["value"] if rev else None,
            "cash": bal["cash"] or 0.0, "st_investments": bal["st_investments"] or 0.0,
            "debt": bal["debt"] or 0.0, "balance_as_of": bal["as_of"],
            "diluted_shares": dil["value"] if dil else None}


def _need(inp: dict, key: str, method: str) -> float:
    v = inp.get(key)
    if v is None:
        raise ValuationError(f"{method} needs {key}, and the XBRL facts have none")
    return float(v)


def _scenario_value(conn, method: str, name: str, sc: dict, inp: dict, settings: dict) -> float:
    r0, gt0, n = _rates(settings)
    if method == "dcf_fcf":
        fcf0 = _need(inp, "fcf_ttm", method)
        if fcf0 <= 0:
            raise ValuationError(f"dcf_fcf needs a positive fcf_ttm, got {fcf0:,.0f}; use ev_sales")
        shares = _need(inp, "diluted_shares", method)
        r = _num(sc, "discount_rate", name, r0)
        gt = _num(sc, "terminal_growth", name, gt0)
        ev = dcf_ev(fcf0, _num(sc, "growth_1_5", name), _num(sc, "growth_6_10", name), r, gt, n)
        return (ev - inp["debt"] + inp["cash"] + inp["st_investments"]) / shares
    if method == "ev_sales":
        rev = _need(inp, "revenue_ttm", method)
        shares = _need(inp, "diluted_shares", method)
        r = _num(sc, "discount_rate", name, r0)
        cagr, mult = _num(sc, "revenue_cagr_5y", name), _num(sc, "exit_ev_sales", name)
        ev = rev * (1 + cagr) ** 5 * mult / (1 + r) ** 5
        return (ev - inp["debt"] + inp["cash"] + inp["st_investments"]) / shares
    if method == "multiple":
        value, mult = _num(sc, "metric_value", name), _num(sc, "multiple", name)
        if not sc.get("metric_name"):
            raise ValuationError(f"scenario '{name}' needs metric_name")
        if sc.get("finding_id") is None:
            raise ValuationError(f"scenario '{name}' needs finding_id for metric_value")
        row = conn.execute("SELECT quote FROM findings WHERE id=?", (int(sc["finding_id"]),)).fetchone()
        if not row or not quote_has_value(row[0], value):
            raise ValuationError(f"scenario '{name}': the quote of finding {sc['finding_id']} "
                                 f"does not contain metric_value {value:g}")
        return value * mult
    raise ValuationError(f"unknown method '{method}'; use one of {', '.join(METHODS)}")


def _level(valuation: dict, key: str):
    v = valuation.get(key)
    if v is None:
        v = (valuation.get("scenarios") or {}).get(key)
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValuationError(f"{key} must be a price")
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValuationError(f"{key} must be a price, got {v!r}")
    if v <= 0:
        raise ValuationError(f"{key} must be above 0")
    return v


def _evaluate(conn, ticker: str, valuation: dict, settings: dict, cik) -> dict:
    if not isinstance(valuation, dict):
        raise ValuationError("valuation must be an object with method and scenarios")
    method = valuation.get("method")
    if method not in METHODS:
        raise ValuationError(f"unknown method '{method}'; use one of {', '.join(METHODS)}")
    out = {"method": method, "scenarios": {}, "inputs": None}
    if method == "price_levels":
        reason = valuation.get("reason") or (valuation.get("scenarios") or {}).get("reason")
        if not reason:
            raise ValuationError("price_levels needs a reason")
        out.update({"fair_value_low": None, "fair_value_base": None, "fair_value_high": None,
                    "buy_below": _level(valuation, "buy_below"),
                    "trim_above": _level(valuation, "trim_above"), "reason": reason})
        return out
    scenarios = valuation.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValuationError("scenarios must be an object with bear, base and bull")
    for name in SCENARIOS:
        if not isinstance(scenarios.get(name), dict):
            raise ValuationError(f"scenario '{name}' is missing")
    inp = _inputs(conn, cik)
    out["inputs"] = inp
    for name in SCENARIOS:
        sc = scenarios[name]
        ids = _check_evidence(conn, name, sc)
        out["scenarios"][name] = {"fair_value": _scenario_value(conn, method, name, sc, inp, settings),
                                  "finding_ids": ids}
    low, base, high = (out["scenarios"][s]["fair_value"] for s in SCENARIOS)
    mos = float(settings.get("margin_of_safety_pct", 15.0))
    bb, ta = _level(valuation, "buy_below"), _level(valuation, "trim_above")
    out.update({"fair_value_low": low, "fair_value_base": base, "fair_value_high": high,
                "buy_below": bb if bb is not None else (base * (1 - mos / 100) if base > 0 else None),
                "trim_above": ta if ta is not None else (high if high > 0 else None)})
    return out


def fair_values(conn, ticker: str, valuation: dict, *, settings: dict, cik) -> dict:
    """Fair values and levels of a thesis valuation (§8.3):
    ``{fair_value_low, fair_value_base, fair_value_high, buy_below, trim_above}``.

    The bear, base and bull scenarios give low, base and high. The default levels are
    ``buy_below = base x (1 - margin_of_safety_pct/100)`` and ``trim_above = high``; a
    ``buy_below`` or ``trim_above`` in ``valuation`` wins. Raises ``ValuationError``."""
    res = _evaluate(conn, ticker, valuation, settings, cik)
    return {k: res[k] for k in ("fair_value_low", "fair_value_base", "fair_value_high",
                                "buy_below", "trim_above")}


def run_valuation(conn, ticker: str, method: str, scenarios: dict, *, settings: dict, cik) -> dict:
    """The ``run_valuation`` desk tool: the fair values of ``scenarios`` by ``method``,
    each scenario's value, the XBRL inputs, and the reverse DCF at the latest metrics row.
    It computes and stores nothing. Raises ``ValuationError``."""
    scenarios = dict(scenarios or {})
    valuation = {"method": method, "scenarios": scenarios}
    for k in ("buy_below", "trim_above", "reason"):
        if k in scenarios and method != "price_levels":
            valuation[k] = scenarios.pop(k)
    res = _evaluate(conn, ticker, valuation, settings, cik)
    r, gt, n = _rates(settings)
    row = latest_metrics(conn, ticker)
    rd = {"implied_growth": None, "reason": "no_metrics", "ev": None, "fcf_ttm": None, "as_of": None}
    if row:
        mm = row["metrics"]
        g, why = reverse_dcf(mm.get("ev"), mm.get("fcf_ttm"), r, gt, n)
        rd = {"implied_growth": g, "reason": why, "ev": mm.get("ev"), "fcf_ttm": mm.get("fcf_ttm"),
              "as_of": row["as_of"], "price": row["price"]}
    res["ticker"] = str(ticker).upper()
    res["reverse_dcf"] = rd
    res["settings"] = {"discount_rate": r, "terminal_growth": gt, "years": n,
                       "margin_of_safety_pct": float(settings.get("margin_of_safety_pct", 15.0))}
    return res


# --- production seams (network; tests pass fakes) ------------------------------------

def yf_price(ticker: str) -> Optional[float]:
    """The yfinance last price (§7: live price)."""
    import yfinance as yf
    p = yf.Ticker(ticker).fast_info.get("lastPrice")
    return float(p) if p else None


def yf_market_cap(ticker: str) -> Optional[float]:
    """The yfinance market cap: the fallback when a company has no dei shares fact."""
    import yfinance as yf
    mc = yf.Ticker(ticker).fast_info.get("marketCap")
    return float(mc) if mc else None


def yf_next_earnings(ticker: str) -> Optional[str]:
    """The next earnings date from yfinance ``Ticker.calendar`` as ``YYYY-MM-DD``."""
    import yfinance as yf
    cal = yf.Ticker(ticker).calendar or {}
    dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not dates:
        return None
    first = dates[0] if isinstance(dates, (list, tuple)) else dates
    return first.isoformat() if hasattr(first, "isoformat") else str(first)


def trend_technicals(ticker: str, *, curr_date: Optional[str] = None, load=None) -> Optional[dict]:
    """The ``technicals`` key (§8.1): regime, ADX, drawdown and one-year Sharpe from
    ``lib.dataflows.stockstats_utils.get_technicals`` over the 5-year frame (§8.5)."""
    from lib import trend
    from lib.dataflows import stockstats_utils as su
    frames: list = []

    def keep(sym, day):
        frame = (load or su.load_ohlcv)(sym, day)
        frames.append(frame)
        return frame

    b = su.get_technicals(ticker, curr_date=curr_date, load=keep)
    closes = [float(x) for x in frames[0]["Close"].tolist()][-253:]
    rets = [y / x - 1 for x, y in zip(closes, closes[1:]) if x]
    return {"regime": b.get("trend_regime"), "adx": b.get("adx"), "drawdown": b.get("dd_current"),
            "max_drawdown": b.get("dd_max"), "sharpe_1y": trend.annualized_sharpe(rets) if len(rets) >= 200 else None,
            "ret_1y": b.get("ret_1y"), "as_of": b.get("as_of")}
