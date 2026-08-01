#!/usr/bin/env python3
"""Freeze a price panel so a benchmark is reproducible across calendar time.

WHY. `yf.download(auto_adjust=True)` RE-ADJUSTS history after every split and dividend, so the
same replay run a month apart silently returns different numbers and any A/B against a recorded
result is meaningless. A frozen panel pins the prices once, with a sha256 over the payload.

ADJUSTMENT POLICY (both stored, so the choice is auditable rather than buried):
  * positions  -> auto_adjust=True   (total return; what the wall sizes and marks against)
  * benchmark  -> auto_adjust=False  (PRICE return, matching the live convention in tick.py's
                  benchmark path — a total-return benchmark biases every alpha negative)

    .venv/bin/python scripts/freeze_prices.py --start 2022-01-01 --end 2026-08-01 \
        --out state/panels/book_2022_2026.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _book_universe() -> list:
    from lib.config import load_config
    from lib.ledger import Ledger
    import tick
    cfg = load_config(str(_REPO / "config.yaml"))
    led = Ledger(Path(tempfile.mkdtemp(prefix="panel_uni_")) / "u.db")
    return sorted(set(tick._analysis_universe(cfg, led)))


def fetch(tickers, start, end, benchmark):
    """The ONE network call. Isolated here so nothing else in bench/ needs a network."""
    import yfinance as yf
    syms = sorted({str(t).upper() for t in tickers} | {benchmark.upper()})
    out = {}
    for adjusted in (True, False):
        df = yf.download(syms, start=start, end=end, auto_adjust=adjusted, progress=False)
        rec: dict = {}
        for field in ("Open", "Close"):
            if field not in df:
                continue
            for ts, row in df[field].iterrows():
                d = ts.strftime("%Y-%m-%d")
                for sym in syms:
                    try:
                        v = float(row[sym])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if v == v and v > 0:
                        rec.setdefault(d, {}).setdefault(sym, {})[field.lower()] = v
        out["adjusted" if adjusted else "unadjusted"] = rec
    return out


def build_payload(raw, tickers, benchmark, start, end) -> dict:
    bench = benchmark.upper()
    adj, unadj = raw["adjusted"], raw["unadjusted"]
    prices, bench_series = {}, {}
    for d, syms in sorted(adj.items()):
        day = {t: v for t, v in syms.items() if t != bench and v.get("close")}
        if day:
            prices[d] = day
        # benchmark from the UNADJUSTED pull (price return) — see module docstring
        b = (unadj.get(d) or {}).get(bench)
        if b and b.get("close"):
            bench_series[d] = b
    body = {"prices": prices, "benchmark": bench_series}
    sha = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    coverage = {t: sum(1 for day in prices.values() if t in day)
                for t in sorted({s for day in prices.values() for s in day})}
    return {"meta": {"tickers": sorted(tickers), "benchmark": bench, "start": start, "end": end,
                     "n_dates": len(prices), "sha256": sha, "coverage": coverage,
                     "positions_auto_adjust": True, "benchmark_auto_adjust": False},
            **body}


def load_price_panel(path):
    """-> (dates, price_at, benchmark_at, open_at). Drop-in for wall_replay's price fns."""
    payload = json.loads(Path(path).read_text())
    prices, bench = payload["prices"], payload["benchmark"]
    dates = sorted(prices)

    def price_at(date):
        return {t: v["close"] for t, v in (prices.get(date) or {}).items() if v.get("close")}

    def open_at(date):
        return {t: v["open"] for t, v in (prices.get(date) or {}).items() if v.get("open")}

    def benchmark_at(date):
        return (bench.get(date) or {}).get("close")

    return dates, price_at, benchmark_at, open_at


def verify(path) -> bool:
    payload = json.loads(Path(path).read_text())
    body = {"prices": payload["prices"], "benchmark": payload["benchmark"]}
    sha = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return sha == payload["meta"]["sha256"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--tickers", default="", help="comma list; default = the live book universe")
    ap.add_argument("--out", required=True)
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args(argv)

    if a.verify_only:
        good = verify(a.out)
        print(f"{'OK' if good else 'SHA MISMATCH'}  {a.out}")
        return 0 if good else 1

    tickers = ([t.strip().upper() for t in a.tickers.split(",") if t.strip()]
               if a.tickers else _book_universe())
    print(f"freezing {len(tickers)} tickers + {a.benchmark}  {a.start}..{a.end}")
    payload = build_payload(fetch(tickers, a.start, a.end, a.benchmark),
                            tickers, a.benchmark, a.start, a.end)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload))
    m = payload["meta"]
    print(f"  dates={m['n_dates']}  sha256={m['sha256'][:16]}…  -> {out}")
    thin = {t: n for t, n in m["coverage"].items() if n < m["n_dates"] * 0.9}
    if thin:
        # Loudly, because this IS the survivorship problem: names that did not exist for the
        # whole window silently drop out and understate drawdown.
        print(f"  !! PARTIAL COVERAGE (survivorship hazard): {thin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
