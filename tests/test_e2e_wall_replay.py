#!/usr/bin/env python3
"""Deterministic e2e for the wall-replay backtest (①) — no network, no broker.

Covers: the injectable clock seam (unset-transparent + set/restore), SimBroker fill
math, a multi-day in-memory replay, the dry-run "reserve nothing" invariant, a
DELIBERATE daily-loss halt, conviction-driven sizing reachability, the commit-phase
consistency-gate fuel (differential proof of adversarial-review finding #3), reflect
outcome resolution, and run determinism.

Plain asserts, no pytest. Run: .venv/bin/python tests/test_e2e_wall_replay.py
"""
import math
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import tick as T  # noqa: E402
from lib import market  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
import lib.wall_replay as wr  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
PASS = 0
FAIL = 0


def ok(name, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}" + (f"  — {extra!r}" if extra is not None else ""))


def _cfg(rebalance=True, halt_pct=20.0, max_reversals=1, consistency=True):
    scratch_mem = Path(tempfile.mkdtemp(prefix="wr_mem_"))
    d = {
        "account_number": "12345678", "dry_run": True,
        "kill_switch_file": str(Path(tempfile.mkdtemp()) / "KILL"),
        "strategy_path": str(_REPO / "tests" / "fixtures" / "strategy_fixture.yaml"),
        "risk": {"max_dollars_per_trade": 100, "daily_loss_halt_pct": halt_pct,
                 "daily_capital_deploy_cap": 1000, "min_buying_power_buffer": 5,
                 "rebalance_enabled": rebalance,
                 "consistency": {"enabled": consistency,
                                 "max_discretionary_reversals": max_reversals, "flip_window": 6}},
        "memory": {"dir": str(scratch_mem)},
        "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
    }
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(d, p)
    p.close()
    return load_config(p.name), p.name


def _led():
    return Ledger(Path(tempfile.mkdtemp()) / "wr.db")


def _synth(tickers, n=40, dip=(25, 30)):
    """Deterministic synthetic prices: gentle uptrend with a ~6% dip window."""
    bases = {t: (50 + 7 * i) for i, t in enumerate(tickers)}
    days = []
    d0 = datetime(2024, 1, 2)
    from datetime import timedelta
    while len(days) < n:
        if d0.weekday() < 5:
            days.append(d0.strftime("%Y-%m-%d"))
        d0 += timedelta(days=1)
    pm, bench = {}, {}
    for i, day in enumerate(days):
        trend = 1 + 0.010 * i - (0.06 if dip[0] <= i < dip[1] else 0.0)
        pm[day] = {t: (round(bases[t] * trend * (1 + 0.002 * math.sin(i / 3.0)), 4)
                       if t != "SGOV" else 100.0) for t in tickers}
        bench[day] = round(500 * (1 + 0.008 * i - (0.05 if dip[0] <= i < dip[1] else 0)), 4)
    return days, pm, bench


BOOK = ["SMH", "SOXX", "URA", "GRID", "XLV", "RSP", "EEM", "IBIT", "ETHA", "IREN", "SGOV"]


# ---------------------------------------------------------------- seam (u1,u2)
def test_seam():
    print("\n[u1/u2] injectable clock seam")
    a = market.now_et(); b = datetime.now(ET)
    ok("unset now_et() is real-clock ET (byte-identical fall-through)",
       a.tzinfo == ET and abs((b - a).total_seconds()) < 1.0)
    ok("unset override is None", market._now_override is None)
    ok("unset trading_day_et matches wall clock",
       market.trading_day_et() == datetime.now(ET).strftime("%Y-%m-%d"))

    pin = datetime(2024, 3, 14, 10, 0, tzinfo=ET)
    with market.frozen_clock(pin):
        ok("SET: now_et()==pin", market.now_et() == pin)
        ok("SET: trading_day_et()==pin.date()", market.trading_day_et() == "2024-03-14")
        ok("SET: session-open derives the schedule from the pinned day",
           market.is_regular_session_open() is True)
    ok("restore: override None after block", market._now_override is None)

    with market.frozen_clock(datetime(2024, 7, 4, 12, 0, tzinfo=ET)):
        ok("SET holiday: XNYS closed under the pin", market.is_regular_session_open() is False)
    try:
        with market.frozen_clock(datetime(2024, 3, 14, 10, 0)):   # naive → ET
            ok("naive pin normalized to ET", market.now_et().tzinfo == ET)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    ok("restore-on-exception: override None", market._now_override is None)


# ---------------------------------------------------------------- SimBroker (u3)
def test_simbroker():
    print("\n[u3] SimBroker fill math")
    b = wr.SimBroker(1000.0)
    b.apply_fills([{"ticker": "AAA", "side": "buy", "type": "market", "dollar_amount": 200.0}], {"AAA": 100.0})
    ok("market buy: 2 shares @100, cash 1000->800",
       abs(b.positions["AAA"] - 2.0) < 1e-9 and abs(b.cash - 800.0) < 1e-9, (b.positions, b.cash))
    snap = b.snapshot({"AAA": 110.0})
    ok("snapshot equity = cash + MV (800 + 2*110 = 1020)", abs(snap["equity"] - 1020.0) < 1e-6, snap["equity"])
    ok("snapshot carries prior mark when price missing (NaN)",
       abs(b.snapshot({"AAA": float("nan")})["equity"] - 1020.0) < 1e-6)

    f = b.apply_fills([{"ticker": "AAA", "side": "sell", "type": "market", "quantity": 1.0}], {"AAA": 150.0})
    ok("sell 1 @150: cash 800->950, realized (150-100)*1=50",
       abs(b.cash - 950.0) < 1e-9 and abs(f[0]["realized_pnl"] - 50.0) < 1e-6, (b.cash, f))

    b2 = wr.SimBroker(50.0)
    b2.apply_fills([{"ticker": "B", "side": "buy", "type": "market", "dollar_amount": 200.0}], {"B": 10.0})
    ok("overspend clamps to cash (buy 200 with only 50 -> 5 sh, cash 0)",
       abs(b2.positions["B"] - 5.0) < 1e-9 and abs(b2.cash) < 1e-9, (b2.positions, b2.cash))
    fo = b2.apply_fills([{"ticker": "B", "side": "sell", "type": "market", "quantity": 999.0}], {"B": 10.0})
    ok("oversell clamps to held (sell 999 of 5 -> sells 5)", abs(fo[0]["shares"] - 5.0) < 1e-9, fo)

    b3 = wr.SimBroker(100.0)
    fn = b3.apply_fills([{"ticker": "C", "side": "buy", "type": "market", "dollar_amount": 50.0}], {"C": float("nan")})
    ok("unpriceable (NaN) buy is skipped, cash untouched",
       fn[0]["filled"] is False and abs(b3.cash - 100.0) < 1e-9, (fn, b3.cash))
    b3.apply_fills([{"ticker": "C", "side": "buy", "type": "limit", "quantity": 3.0, "limit_price": 20.0}], {"C": 999.0})
    ok("limit buy fills at limit_price not the close (3 @20 = 60)",
       abs(b3.positions["C"] - 3.0) < 1e-9 and abs(b3.cash - 40.0) < 1e-9, (b3.positions, b3.cash))


# ---------------------------------------------------------------- signal src (u4)
def test_signal_determinism():
    print("\n[u4] synthetic signal source is deterministic + valid")
    _, pm, _ = _synth(["SMH", "URA"], n=40)
    src = wr.sma_cross_signal_source(pm, fast=5, slow=20)
    r1 = src("2024-02-01", ["SMH", "URA"], {})
    r2 = src("2024-02-01", ["SMH", "URA"], {})
    import lib.signals as signals
    ok("two calls identical (pure)", r1 == r2)
    ok("emits only VALID_SIGNALS", all(x["signal"] in signals.VALID_SIGNALS for x in r1), r1)


# ---------------------------------------------------------------- full replay
def test_replay_and_invariants():
    print("\n[a/b/f] multi-day replay + dry-run invariant + reflect outcomes")
    cfg, cfgp = _cfg(rebalance=True)
    led = _led()
    dates, pm, bench = _synth(BOOK, n=40)
    d, price_at, bench_at = wr.make_in_memory_price_fns(pm, bench)
    src = wr.sma_cross_signal_source(pm, fast=5, slow=20)
    res = wr.run_backtest(cfg, led, dates=d, tickers=BOOK, signal_source=src,
                          price_at=price_at, benchmark_at=bench_at, starting_cash=500.0, config_path=cfgp)
    s = res["summary"]
    ok("equity curve spans all ticks", s["n_ticks"] == len(d), s["n_ticks"])
    ok("every equity point finite & positive",
       all(isinstance(p["equity"], float) and p["equity"] > 0 and p["equity"] == p["equity"]
           for p in res["equity_curve"]))
    ok("day-1 deploy reduced cash below starting", res["equity_curve"][0]["cash"] < 500.0,
       res["equity_curve"][0]["cash"])
    ok("clock fully restored after replay", market._now_override is None)
    reserved = sum(len(led.day_orders(x)) for x in d)
    ok("DRY-RUN invariant: ZERO orders reserved in the ledger", reserved == 0, reserved)
    ok("reflect resolved >=1 decision with a directional_return",
       any(x.get("directional_return") is not None for x in res["decisions"]),
       len(res["decisions"]))
    # The replay is a TRUSTED in-process caller: it owns a real benchmark series for the
    # window it replays, so cmd_reflect must honor it (trust_input_benchmark=True). Read
    # the columns RAW -- nothing in Ledger SELECTs benchmark_return, and cmd_reflect's
    # output never exposes it, so without this the whole trust seam is untested and a
    # typo'd kwarg would silently degrade every replayed alpha to NULL.
    import sqlite3 as _sq3
    _c = _sq3.connect(led.db_path)
    _bm = _c.execute("SELECT COUNT(*) FROM outcomes WHERE benchmark_return IS NOT NULL").fetchone()[0]
    _al = _c.execute("SELECT COUNT(*) FROM outcomes WHERE alpha IS NOT NULL").fetchone()[0]
    _c.close()
    ok("replay's trusted benchmark_return reaches the ledger (trust seam intact)", _bm > 0, _bm)
    ok("replay's alpha is therefore computed, not NULL", _al > 0, _al)


def test_dryrun_assert():
    print("\n[safety] run_backtest refuses a non-dry_run config")
    cfg, cfgp = _cfg()
    object.__setattr__(cfg, "dry_run", False)   # force-live
    led = _led()
    try:
        wr.run_backtest(cfg, led, dates=["2024-01-02"], tickers=["SMH"],
                        signal_source=lambda *a: [], price_at=lambda d: {"SMH": 100.0},
                        benchmark_at=lambda d: 100.0, starting_cash=100.0, config_path=cfgp)
        ok("refused non-dry_run", False, "did not raise")
    except AssertionError:
        ok("refused non-dry_run", True)
    finally:
        market.set_clock(None)


def test_determinism():
    print("\n[det] two identical replays produce identical curves")
    dates, pm, bench = _synth(BOOK, n=25)
    d, price_at, bench_at = wr.make_in_memory_price_fns(pm, bench)
    outs = []
    for _ in range(2):
        cfg, cfgp = _cfg(rebalance=True)
        led = _led()
        src = wr.sma_cross_signal_source(pm, fast=5, slow=20)
        outs.append(wr.run_backtest(cfg, led, dates=d, tickers=BOOK, signal_source=src,
                                    price_at=price_at, benchmark_at=bench_at, starting_cash=500.0,
                                    config_path=cfgp))
    ok("equity curves byte-equal across runs",
       [p["equity"] for p in outs[0]["equity_curve"]] == [p["equity"] for p in outs[1]["equity_curve"]])
    ok("summaries equal across runs", outs[0]["summary"] == outs[1]["summary"], outs[0]["summary"])


# ---------------------------------------------------------------- deliberate halt (c)
def test_daily_loss_halt():
    print("\n[c] daily-loss halt fires on an engineered same-day crash")
    cfg, _ = _cfg(rebalance=False, halt_pct=20.0)
    led = _led()
    with market.frozen_clock(datetime(2024, 3, 14, 10, 0, tzinfo=ET)):
        iso = market.now_et().isoformat()
        # tick 1 locks the day's baseline at $1000
        T._run_plan(cfg, led, {"equity": 1000.0, "buying_power": 1000.0, "positions": {},
                               "quotes": {}, "analyses": [], "now_iso": iso, "run_id": "h1"})
        # tick 2 same day at crashed equity -> drop 30% >= 20% halt
        out = T._run_plan(cfg, led, {"equity": 700.0, "buying_power": 700.0, "positions": {},
                                     "quotes": {}, "analyses": [], "now_iso": iso, "run_id": "h2"})
    market.set_clock(None)
    ok("halt True on -30% vs the day baseline", out["halt"] is True, out.get("drop_pct"))
    ok("write_kill True", out["write_kill"] is True)


# ---------------------------------------------------------------- conviction (d)
def test_conviction_moves_target():
    print("\n[d] conviction sizing is reachable through construct")
    def _smh_target(analyses):
        cfg, _ = _cfg(rebalance=True)
        led = _led()
        with market.frozen_clock(datetime(2024, 3, 14, 10, 0, tzinfo=ET)):
            T._run_strategy_set(cfg, led, {"equity": 1000.0, "now_iso": market.now_et().isoformat()})
            snap = {"equity": 1000.0, "buying_power": 1000.0,
                    "positions": {}, "quotes": {t: 100.0 for t in BOOK}}
            con = T._run_construct(cfg, led, {**snap, "macro_reading": None, "analyses": analyses})
        market.set_clock(None)
        tw = con.get("target_weights") or {}
        return (tw.get("SMH") or {}).get("target_dollars")
    static = _smh_target([])                                   # all-Hold -> static book
    conv = _smh_target([{"ticker": "SMH", "signal": "Buy", "conviction": 0.95,
                         "position_pct": 15.0, "basis": "conviction"}])
    ok("all-Hold keeps a static SMH target", static is not None, static)
    ok("a high-conviction Buy MOVES SMH's target (allocate actually ran)",
       conv is not None and static is not None and abs(conv - static) > 1e-6, (static, conv))


# ---------------------------------------------------------------- consistency (e)
def _plan_then_commit(cfg, led, day, analyses, positions, do_commit):
    """Drive one plan on `day` and (optionally) the commit phase, exactly like the
    harness — under a pinned clock so trading_day_et()==day."""
    tmp = Path(tempfile.mkdtemp()) / "c.json"
    prev = T.LEDGER_DB
    T.LEDGER_DB = Path(led.db_path)
    try:
        with market.frozen_clock(datetime.strptime(day, "%Y-%m-%d").replace(hour=10, tzinfo=ET)):
            iso = market.now_et().isoformat()
            plan = T._run_plan(cfg, led, {"equity": 1000.0, "buying_power": 1000.0,
                                          "positions": positions, "quotes": {"AAA": 100.0},
                                          "analyses": analyses, "now_iso": iso, "run_id": f"BT-{day}"})
            if do_commit:
                for o in plan["orders"]:
                    wr._commit_order(o, {"filled": True}, plan["run_id"], tmp)
        return plan
    finally:
        market.set_clock(None)
        T.LEDGER_DB = prev


def test_consistency_gate_needs_commit():
    print("\n[e] commit phase writes the actions log -> consistency gate becomes live (review #3)")
    # max_discretionary_reversals=0 -> any ungrounded cross-day reversal must be suppressed,
    # BUT only if the prior completed trade is in the `actions` log (written by commit).
    # WITH commit: the buy is logged -> the later sell is a detected reversal -> suppressed.
    cfg, _ = _cfg(rebalance=False, max_reversals=0)
    led = _led()
    _plan_then_commit(cfg, led, "2024-03-14", [{"ticker": "AAA", "signal": "Buy", "basis": "thesis-1"}],
                      {}, do_commit=True)
    import sqlite3
    c = sqlite3.connect(led.db_path)
    nact = c.execute("SELECT COUNT(*) FROM actions WHERE ticker='AAA' AND intent='buy'").fetchone()[0]
    c.close()
    ok("commit phase wrote a buy event to the actions log", nact >= 1, nact)
    sell = _plan_then_commit(cfg, led, "2024-03-21",
                             [{"ticker": "AAA", "signal": "Sell", "basis": "flip"}],
                             {"AAA": {"quantity": 1.0, "market_value": 100.0}}, do_commit=False)
    dec = next((d for d in sell["decisions"] if d.get("ticker") == "AAA"), {})
    ok("WITH commit: ungrounded cross-day reversal is SUPPRESSED",
       dec.get("status") == "skipped" and str(dec.get("detail", "")).startswith("consistency:"), dec)

    # WITHOUT commit: no actions row -> the gate sees no prior trade -> the sell is NOT suppressed.
    cfg2, _ = _cfg(rebalance=False, max_reversals=0)
    led2 = _led()
    _plan_then_commit(cfg2, led2, "2024-03-14", [{"ticker": "AAA", "signal": "Buy", "basis": "thesis-1"}],
                      {}, do_commit=False)      # <-- NO commit: actions log stays empty
    sell2 = _plan_then_commit(cfg2, led2, "2024-03-21",
                              [{"ticker": "AAA", "signal": "Sell", "basis": "flip"}],
                              {"AAA": {"quantity": 1.0, "market_value": 100.0}}, do_commit=False)
    dec2 = next((d for d in sell2["decisions"] if d.get("ticker") == "AAA"), {})
    ok("WITHOUT commit: gate is inert (no actions log) -> reversal NOT consistency-suppressed",
       not str(dec2.get("detail", "")).startswith("consistency:"), dec2)


def main() -> int:
    print("=" * 72)
    print("WALL-REPLAY e2e (offline, deterministic)")
    print("=" * 72)
    test_seam()
    test_simbroker()
    test_signal_determinism()
    test_replay_and_invariants()
    test_dryrun_assert()
    test_determinism()
    test_daily_loss_halt()
    test_conviction_moves_target()
    test_consistency_gate_needs_commit()
    print("\n" + "=" * 72)
    print(f"wall-replay e2e: {PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
