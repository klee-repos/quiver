"""Deterministic-wall replay (①) — an OFFLINE, READ-ONLY backtest of the Quiver
Python wall.

Replays the shipping deterministic pipeline
    strategy-set -> construct -> plan -> (simulated fills) -> commit -> reflect
over a historical date range WITHOUT the LLM and WITHOUT the live broker / live
ledger, producing an equity curve + per-decision directional-return/alpha. Its job
is to validate the SIZING CLAMPS / rebalance / consistency-gate / cadence logic over
history — NOT to estimate tradeable alpha (the trading signal is an INPUT here:
synthetic or a recorded analyze.py stream).

WHY this can exist safely: the wall is a pure function of (config, ledger-state,
broker-snapshot). It never calls the broker — it consumes a snapshot dict. So the
harness feeds a SIMULATED snapshot derived from a historical price series and reads
back the orders the wall would place, applying them to a toy portfolio. The one
wall-clock dependency (`market.now_et()`, which also fixes the ledger day-key
`trading_day_et()` and every XNYS session check) is pinned per tick via
`market.frozen_clock`, so a replay is deterministic and date-correct.

SAFETY INVARIANTS (enforced): `cfg.dry_run` is asserted True before any tick; a
THROWAWAY scratch ledger is used (never state/ledger.db); this module imports NO
broker / MCP. In dry-run the wall reserves no orders (`orders` table stays empty) —
the harness simulates fills itself.

HONEST FIDELITY LIMITS (also printed in the CLI banner):
  * Optimistic fills: market orders fill at the same-day close = the quote the wall
    sized against — no slippage, liquidity caps, partial fills, or bid/ask.
  * Once-a-day: one tick per date. The intraday daily-loss HALT (drawdown vs the
    day's OPENING baseline) can't arise in a one-tick-per-date loop — it is covered
    by a dedicated test, not the natural loop. Intraday replay is a TODO.
  * Prices use yfinance auto_adjust (mild split/div back-adjustment bias).
  * The signal is an INPUT, so results say NOTHING about LLM edge.
"""
from __future__ import annotations

import json
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lib import market, memory
import lib.signals as signals
import tick

ET = ZoneInfo("America/New_York")


# --- helpers ----------------------------------------------------------------

def _valid_px(px) -> bool:
    """True only for a usable positive price (rejects None and NaN — note NaN != NaN)."""
    try:
        return px is not None and px == px and float(px) > 0.0
    except (TypeError, ValueError):
        return False


def _et_open(date: str) -> datetime:
    """A tz-aware ET instant inside the regular session for `date` (10:00 ET). The
    replay pins the clock here so is_regular_session_open() is True on a trading day."""
    d = datetime.strptime(date, "%Y-%m-%d")
    return d.replace(hour=10, minute=0, tzinfo=ET)


# --- simulated broker (the only NEW market model; deliberately minimal) ------

class SimBroker:
    """A toy long-only portfolio: cash + fractional share positions with a weighted
    cost basis. Values positions at each tick's close (carrying the last mark when a
    tick has no price, never producing NaN). NOT a market simulator — see module docs."""

    def __init__(self, cash: float):
        self.cash = float(cash)
        self.positions: dict[str, float] = {}
        self.cost_basis: dict[str, float] = {}   # total $ cost per held name
        self._last_mark: dict[str, float] = {}

    def snapshot(self, prices: dict) -> dict:
        """Broker-shaped snapshot. NOTE equity is TOTAL account value (cash + MV), not
        the broker's positions-only figure: `_run_plan` uses `data["equity"]` only as
        the all-cash fallback (when `positions` is empty), so passing total avoids a
        zero denominator on cold-start/all-cash days (adversarial review #3/#4)."""
        quotes = {str(t).upper(): float(p) for t, p in prices.items() if _valid_px(p)}
        pos: dict[str, dict] = {}
        mv_total = 0.0
        for tk, sh in self.positions.items():
            if sh <= 1e-12:
                continue
            px = prices.get(tk)
            if _valid_px(px):
                mark = float(px)
                self._last_mark[tk] = mark
            else:
                mark = self._last_mark.get(tk)   # carry prior mark; never NaN
            if mark is None:
                continue                          # never priced -> can't value; omit from MV
            mv = sh * mark
            mv_total += mv
            pos[tk] = {"quantity": sh, "market_value": round(mv, 6)}
        return {
            "equity": round(self.cash + mv_total, 6),
            "buying_power": round(self.cash, 6),
            "positions": pos,
            "quotes": quotes,
        }

    def apply_fills(self, orders: list, prices: dict) -> list:
        """Simulate execution of the wall's returned orders at this tick's prices.
        Guards every fill against an unpriceable/NaN close (skips it, keeps cash/shares
        intact) and clamps to available cash/shares (no negative cash, no oversell)."""
        fills = []
        for o in orders:
            tk = str(o.get("ticker", "")).upper()
            side = o.get("side")
            # A limit order fills at its Python-computed limit price; else the close.
            if o.get("type") == "limit" and _valid_px(o.get("limit_price")):
                exec_px = float(o["limit_price"])
            else:
                exec_px = prices.get(tk)
            if not _valid_px(exec_px):
                fills.append({"ticker": tk, "side": side, "filled": False,
                              "note": f"unpriceable:{tk}"})
                continue
            exec_px = float(exec_px)

            if side == "buy":
                if o.get("dollar_amount") is not None:
                    dollars = min(float(o["dollar_amount"]), self.cash)
                    shares = dollars / exec_px if exec_px else 0.0
                elif o.get("quantity") is not None:
                    shares = float(o["quantity"])
                    if shares * exec_px > self.cash:      # clamp a limit buy to cash
                        shares = self.cash / exec_px if exec_px else 0.0
                    dollars = shares * exec_px
                else:
                    fills.append({"ticker": tk, "side": side, "filled": False, "note": "buy_no_size"})
                    continue
                if shares <= 1e-12:
                    fills.append({"ticker": tk, "side": side, "filled": False, "note": "insufficient_cash"})
                    continue
                self.cash -= dollars
                self.positions[tk] = self.positions.get(tk, 0.0) + shares
                self.cost_basis[tk] = self.cost_basis.get(tk, 0.0) + dollars
                self._last_mark[tk] = exec_px
                fills.append({"ticker": tk, "side": "buy", "filled": True, "shares": shares,
                              "price": exec_px, "dollars": round(dollars, 6), "realized_pnl": 0.0})

            elif side == "sell":
                held = self.positions.get(tk, 0.0)
                qty = min(float(o.get("quantity") or 0.0), held)
                if qty <= 1e-12:
                    fills.append({"ticker": tk, "side": side, "filled": False, "note": "no_shares"})
                    continue
                proceeds = qty * exec_px
                avg = (self.cost_basis.get(tk, 0.0) / held) if held > 0 else 0.0
                realized = (exec_px - avg) * qty
                self.cash += proceeds
                self.cost_basis[tk] = max(0.0, self.cost_basis.get(tk, 0.0) - avg * qty)
                self.positions[tk] = held - qty
                if self.positions[tk] <= 1e-9:
                    self.positions[tk] = 0.0
                    self.cost_basis[tk] = 0.0
                fills.append({"ticker": tk, "side": "sell", "filled": True, "shares": qty,
                              "price": exec_px, "proceeds": round(proceeds, 6),
                              "realized_pnl": round(realized, 6)})
            else:
                fills.append({"ticker": tk, "side": side, "filled": False, "note": "unknown_side"})
        return fills


# --- price + signal sources (all injectable; yfinance isolated to one fn) -----

def make_in_memory_price_fns(price_map: dict, benchmark_map: dict | None = None):
    """price_map: {date: {ticker: close}}; benchmark_map: {date: close}. Returns
    (dates_sorted, price_at, benchmark_at) — the no-network path used by tests."""
    dates = sorted(price_map)
    bench = benchmark_map or {}

    def price_at(date: str) -> dict:
        return {str(t).upper(): p for t, p in (price_map.get(date) or {}).items()}

    def benchmark_at(date: str):
        return bench.get(date)

    return dates, price_at, benchmark_at


def fetch_price_history(tickers, start: str, end: str, benchmark: str = "SPY"):
    """Batched yfinance daily-close pull (auto_adjust=True). The ONE network call —
    isolated here so the rest of the module (and the whole test suite) needs no
    network. Returns (price_map, benchmark_map) shaped for make_in_memory_price_fns."""
    import yfinance as yf   # local import: never needed on the deterministic test path

    syms = sorted({str(t).upper() for t in tickers} | {benchmark.upper()})
    df = yf.download(syms, start=start, end=end, auto_adjust=True, progress=False)
    close = df["Close"] if "Close" in df else df
    price_map: dict[str, dict] = {}
    bench_map: dict[str, float] = {}
    for ts, row in close.iterrows():
        date = ts.strftime("%Y-%m-%d")
        day = {}
        for sym in syms:
            try:
                v = float(row[sym])
            except (KeyError, TypeError, ValueError):
                v = None
            if _valid_px(v):
                if sym == benchmark.upper():
                    bench_map[date] = v
                day[sym] = v
        if day:
            price_map[date] = day
    return price_map, bench_map


def jsonl_signal_source(path: str):
    """A recorded analyze.py stream (one JSON object per line, each with at least a
    `date` + `ticker`). Returns a src(date, tickers, snap) that yields that date's rows."""
    idx: dict[str, list] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        idx.setdefault(str(row.get("date")), []).append(row)

    def src(date, tickers, snap):
        return list(idx.get(date, []))

    return src


def sma_cross_signal_source(price_map: dict, *, fast: int = 5, slow: int = 20,
                            buy_pct: float = 8.0):
    """A DETERMINISTIC synthetic signal: SMA(fast) vs SMA(slow) on closes up to `date`.
    fast>slow -> Buy; fast<slow -> Underweight; else Hold. Emits the fields _run_plan
    reads (ticker/signal/position_pct/basis/decision_price/entry_price). Pure — same
    inputs give the same signals (for the determinism test)."""
    dates_sorted = sorted(price_map)
    buy = "Buy" if "Buy" in signals.VALID_SIGNALS else sorted(signals.VALID_SIGNALS)[0]
    under = ("Underweight" if "Underweight" in signals.VALID_SIGNALS
             else ("Sell" if "Sell" in signals.VALID_SIGNALS else "Hold"))

    def _series(ticker, upto):
        out = []
        for d in dates_sorted:
            if d > upto:
                break
            px = (price_map.get(d) or {}).get(ticker)
            if _valid_px(px):
                out.append(float(px))
        return out

    def src(date, tickers, snap):
        rows = []
        for tk in tickers:
            tk = str(tk).upper()
            s = _series(tk, date)
            if len(s) < slow:
                continue
            fast_ma = sum(s[-fast:]) / fast
            slow_ma = sum(s[-slow:]) / slow
            close = s[-1]
            if fast_ma > slow_ma:
                sig, pct = buy, buy_pct
            elif fast_ma < slow_ma:
                sig, pct = under, 0.0
            else:
                sig, pct = "Hold", 0.0
            rows.append({"ticker": tk, "signal": sig, "position_pct": pct,
                         "basis": "sma_cross", "decision_price": close, "entry_price": close,
                         "rationale_summary": f"sma{fast}/{slow}"})
        return rows

    return src


# --- the replay driver -------------------------------------------------------

def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _commit_order(order: dict, fill: dict, run_id, tmp: Path) -> None:
    """Drive the REAL cmd_commit (dry-run path: ref_id omitted) so the `actions` event
    log is written — this is what makes the cross-day consistency gate exercisable in a
    replay (adversarial review #3). Must be called under a pinned clock."""
    status = "dry_run" if fill.get("filled") else "error"
    _write_json(tmp, {
        "ticker": order.get("ticker"), "signal": order.get("signal"),
        "intent": order.get("intent"), "status": status,
        "detail": str(fill.get("note", "")), "dollar_amount": order.get("dollar_amount"),
        "run_id": run_id,
    })
    tick.cmd_commit(types.SimpleNamespace(input=str(tmp)))


def _resolve_due_outcomes(led, date: str, price_at, benchmark_at, tmp: Path) -> list:
    """Resolve every decision old enough to score (trade_date <= date - HOLDING_DAYS)
    via the REAL cmd_reflect: price_now = today's close, benchmark_return = the
    benchmark's move since the decision's trade_date. Returns the per-decision rows."""
    cutoff = (datetime.strptime(date, "%Y-%m-%d")
              - timedelta(days=memory.HOLDING_DAYS)).strftime("%Y-%m-%d")
    pending = led.pending_outcome_decisions(cutoff)
    if not pending:
        return []
    prices = price_at(date)
    bench_now = benchmark_at(date)
    resolutions = []
    meta = {}
    for dec in pending:
        tk = str(dec.get("ticker", "")).upper()
        px = prices.get(tk)
        if not _valid_px(px):
            continue
        bench_dec = benchmark_at(dec.get("trade_date"))
        bret = ((bench_now / bench_dec) - 1.0) if (_valid_px(bench_now) and _valid_px(bench_dec)) else None
        resolutions.append({"decision_id": dec["id"], "price_now": float(px),
                            "benchmark_return": bret})
        meta[dec["id"]] = {"ticker": tk, "trade_date": dec.get("trade_date")}
    if not resolutions:
        return []
    _write_json(tmp, {"resolutions": resolutions})
    # trust_input_benchmark: the replay owns a real benchmark series for the historical
    # window it is replaying (benchmark_at above), so it is a Python authority in the
    # same sense the live backfill is. The orchestrator CLI never sets this flag.
    out = tick.cmd_reflect(types.SimpleNamespace(input=str(tmp), trust_input_benchmark=True))
    rows = []
    for r in out.get("results", []):
        if r.get("status") != "resolved":
            continue
        m = meta.get(r.get("decision_id"), {})
        rows.append({"decision_id": r.get("decision_id"), "ticker": m.get("ticker"),
                     "trade_date": m.get("trade_date"), "resolved_on": date,
                     "directional_return": r.get("directional_return")})
    return rows


def _max_drawdown_pct(curve: list) -> float:
    peak = None
    mdd = 0.0
    for pt in curve:
        eq = pt["equity"]
        peak = eq if peak is None else max(peak, eq)
        if peak > 0:
            mdd = min(mdd, (eq - peak) / peak)
    return round(mdd * 100.0, 3)


def run_backtest(cfg, led, *, dates, tickers, signal_source, price_at, benchmark_at,
                 starting_cash: float, run_construct: bool = True, macro_reading=None,
                 config_path: str | None = None) -> dict:
    """Chronological forward-only replay. One tick per date (v1). Returns
    {equity_curve, decisions, summary}. See module docstring for fidelity limits.

    Redirects tick.LEDGER_DB (so cmd_commit/cmd_reflect hit the SAME scratch db as
    `led`) and tick.CONFIG_PATH (so reflect's best-effort memory write lands in the
    temp config's scratch dir, not state/memory/); both restored on exit."""
    if not cfg.dry_run:
        raise AssertionError("wall_replay refuses to run unless cfg.dry_run is True")

    tmpdir = Path(tempfile.mkdtemp(prefix="wall_replay_"))
    commit_tmp = tmpdir / "commit.json"
    reflect_tmp = tmpdir / "reflect.json"

    prev_ldb, prev_cfg = tick.LEDGER_DB, tick.CONFIG_PATH
    tick.LEDGER_DB = Path(led.db_path)
    if config_path:
        tick.CONFIG_PATH = Path(config_path)

    broker = SimBroker(starting_cash)
    equity_curve: list = []
    per_decision: list = []
    halted = False

    try:
        # Activate the goal/book once at t0 (construct needs an active goal), like the
        # live strategy-set. Under the first date's pinned clock so start_date == d0.
        if dates and cfg.strategy is not None:
            with market.frozen_clock(_et_open(dates[0])):
                tick._run_strategy_set(cfg, led, {"equity": float(starting_cash),
                                                  "now_iso": market.now_et().isoformat()})

        for date in dates:
            with market.frozen_clock(_et_open(date)):
                prices = price_at(date)
                if not prices:
                    continue                      # non-trading day (calendar-driven skip)
                snap = broker.snapshot(prices)
                iso = market.now_et().isoformat()

                # analyses BEFORE construct (live order; construct's conviction sizer
                # reads THIS tick's analyses) — adversarial review #6.
                analyses = signal_source(date, tickers, snap) or []

                target_weights = None
                if run_construct and cfg.risk.rebalance_enabled:
                    con = tick._run_construct(cfg, led, {**snap, "macro_reading": macro_reading,
                                                         "analyses": analyses})
                    target_weights = con.get("target_weights") or None

                plan = tick._run_plan(cfg, led, {**snap, "now_iso": iso, "run_id": f"BT-{date}",
                                                 "analyses": analyses,
                                                 "target_weights": target_weights})
                halted = bool(plan.get("halt"))

                orders = plan.get("orders", []) if not halted else []
                fills = broker.apply_fills(orders, prices)
                # commit phase -> writes the actions event log (consistency gate fuel)
                for o, f in zip(orders, fills):
                    _commit_order(o, f, plan.get("run_id"), commit_tmp)

                after = broker.snapshot(prices)
                equity_curve.append({
                    "date": date, "equity": after["equity"], "cash": round(broker.cash, 6),
                    "positions_mv": round(after["equity"] - broker.cash, 6),
                    "drop_pct": plan.get("drop_pct"), "n_orders": len(orders),
                    "n_filled": sum(1 for f in fills if f.get("filled")), "halt": halted,
                })

                per_decision += _resolve_due_outcomes(led, date, price_at, benchmark_at, reflect_tmp)

            if halted:
                break
    finally:
        market.set_clock(None)
        tick.LEDGER_DB, tick.CONFIG_PATH = prev_ldb, prev_cfg

    start_eq = equity_curve[0]["equity"] if equity_curve else float(starting_cash)
    final_eq = equity_curve[-1]["equity"] if equity_curve else float(starting_cash)
    summary = {
        "n_ticks": len(equity_curve),
        "start_equity": start_eq,
        "final_equity": final_eq,
        "total_return_pct": round((final_eq / start_eq - 1.0) * 100.0, 3) if start_eq else 0.0,
        "max_drawdown_pct": _max_drawdown_pct(equity_curve),
        "total_orders": sum(pt["n_orders"] for pt in equity_curve),
        "decisions_resolved": len(per_decision),
        "halted": halted,
    }
    return {"equity_curve": equity_curve, "decisions": per_decision, "summary": summary}
