#!/usr/bin/env python3
"""Produce the wall-replay GOLDEN fixture — the enforcement of the byte-identical-default
invariant.

WHY THIS EXISTS. `tests/test_e2e_wall_replay.py::test_determinism` loops the same replay twice
in ONE process against ONE build. That proves REPRODUCIBILITY; it is structurally incapable of
detecting a change to the default path — it stays 2/2 green while a 50bps default cost silently
moves the curve (measured: final equity 565.464405 -> 562.952794). Nothing in the repo pinned a
replay value. This script pins one.

WHY IT IS A SCRIPT AND NOT A HAND-WRITTEN JSON. A fixture its producer cannot emit is how a
defect ships through a green suite. `tests/test_bench_replay.py` imports THIS module and re-runs
THESE scenarios through the real `lib.wall_replay.run_backtest`, so the fixture is always
regenerable by the code it describes.

    .venv/bin/python scripts/capture_replay_golden.py           # rewrite the fixture
    .venv/bin/python scripts/capture_replay_golden.py --check   # verify without writing

Prices are a closed-form deterministic path (no network), so the golden is reproducible forever.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lib.config import load_config          # noqa: E402
from lib.ledger import Ledger               # noqa: E402
import lib.wall_replay as wr                # noqa: E402

FIXTURE = _REPO / "tests" / "fixtures" / "replay_golden_curves.json"

# The hashed field set. KEEP ALL EIGHT. Narrowing this to the equity series would be a strict
# weakening (cash / n_filled / halt / drop_pct regressions would stop being covered) — and it is
# unnecessary, because later ADDITIVE fields are simply not read by this key list.
HASHED_FIELDS = ["date", "equity", "cash", "positions_mv",
                 "drop_pct", "n_orders", "n_filled", "halt"]

TICKERS = ["AAA", "BBB", "CCC"]


def build_price_path():
    """A closed-form, network-free daily path over 36 sessions."""
    dates = [f"2026-0{m}-{d:02d}" for m in (3, 4) for d in range(2, 26) if (d % 7) not in (0, 6)]
    price_map, bench_map = {}, {}
    for i, d in enumerate(dates):
        price_map[d] = {"AAA": round(100.0 + 3.0 * ((i * 7) % 11) - 0.5 * i, 4),
                        "BBB": round(50.0 + 2.0 * ((i * 5) % 13) + 0.3 * i, 4),
                        "CCC": round(75.0 + 1.5 * ((i * 3) % 17), 4)}
        bench_map[d] = round(400.0 + 0.8 * i, 4)
    return dates, price_map, bench_map


def _cfg_path(*, rebalance: bool) -> str:
    scratch = Path(tempfile.mkdtemp(prefix="golden_mem_"))
    d = {"account_number": "12345678", "dry_run": True,
         "kill_switch_file": str(Path(tempfile.mkdtemp()) / "KILL"),
         "strategy_path": str(_REPO / "tests" / "fixtures" / "strategy_fixture.yaml"),
         "risk": {"max_dollars_per_trade": 100, "daily_loss_halt_pct": 20.0,
                  "daily_capital_deploy_cap": 1000, "min_buying_power_buffer": 5,
                  "rebalance_enabled": rebalance,
                  "consistency": {"enabled": True, "max_discretionary_reversals": 1,
                                  "flip_window": 6}},
         "memory": {"dir": str(scratch)},
         "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(d, p)
    p.close()
    return p.name


def curve_hash(curve) -> str:
    payload = [[pt.get(k) for k in HASHED_FIELDS] for pt in curve]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _buy_src(price_map):
    def src(date, tickers, snap):
        return [{"ticker": t, "signal": "Buy", "position_pct": 15.0, "conviction": 75.0,
                 "uncertainty": 20.0, "basis": "golden",
                 "decision_price": price_map[date][t], "entry_price": price_map[date][t],
                 "rationale_summary": "golden"}
                for t in tickers if t in price_map.get(date, {})]
    return src


def _hold_src(_price_map):
    def src(date, tickers, snap):
        return []
    return src


def scenarios(price_map):
    """name -> (signal_source, rebalance_enabled, starting_cash). The single source of truth
    shared by this producer and tests/test_bench_replay.py."""
    return {
        "sma_rebalance_on":  (wr.sma_cross_signal_source(price_map, fast=3, slow=8, buy_pct=10.0), True, 10000.0),
        "sma_rebalance_off": (wr.sma_cross_signal_source(price_map, fast=3, slow=8, buy_pct=10.0), False, 10000.0),
        "buy_rebalance_on":  (_buy_src(price_map), True, 10000.0),
        "hold_rebalance_on": (_hold_src(price_map), True, 10000.0),
        "buy_small_account": (_buy_src(price_map), True, 500.0),
    }


def run_scenario(name: str, src, rebalance: bool, cash: float, ds, price_at, bench_at) -> dict:
    cp = _cfg_path(rebalance=rebalance)
    cfg = load_config(cp)
    led = Ledger(Path(tempfile.mkdtemp(prefix="golden_led_")) / "l.db")
    res = wr.run_backtest(cfg, led, dates=ds, tickers=TICKERS, signal_source=src,
                          price_at=price_at, benchmark_at=bench_at,
                          starting_cash=cash, config_path=cp)
    s = res["summary"]
    return {"curve_sha256": curve_hash(res["equity_curve"]),
            "n_ticks": s["n_ticks"], "final_equity": s["final_equity"],
            "total_orders": s["total_orders"], "total_return_pct": s["total_return_pct"],
            "max_drawdown_pct": s["max_drawdown_pct"]}


def produce() -> dict:
    """Run every scenario through the REAL run_backtest and return the fixture payload."""
    dates, price_map, bench_map = build_price_path()
    ds, price_at, bench_at = wr.make_in_memory_price_fns(price_map, bench_map)
    out = {}
    for name, (src, rebalance, cash) in scenarios(price_map).items():
        out[name] = run_scenario(name, src, rebalance, cash, ds, price_at, bench_at)
    return out


def _provenance() -> dict:
    """Honest provenance. The worktree that first produced this fixture was DIRTY — a clean
    checkout of the head sha will NOT reproduce these hashes, because the uncommitted diff
    includes `trust_input_benchmark=True` into `tick.cmd_reflect` (lib/wall_replay.py), which
    sits on the benchmark -> alpha -> calibration -> order-dollars path."""
    def _git(*args):
        try:
            return subprocess.run(["git", *args], cwd=_REPO, capture_output=True,
                                  text=True, timeout=15).stdout.strip()
        except Exception:  # noqa: BLE001 — provenance is best-effort, never fatal
            return ""
    head = _git("rev-parse", "--short", "HEAD")
    diff = _git("diff", "HEAD")
    dirty = bool(diff.strip())
    diff_sha = hashlib.sha256(diff.encode()).hexdigest()[:16] if dirty else ""
    return {"git_head": f"{head}{'+dirty' if dirty else ''}", "git_diff_sha256_16": diff_sha}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify the on-disk fixture reproduces; write nothing")
    a = ap.parse_args(argv)

    produced = produce()
    if a.check:
        if not FIXTURE.exists():
            print(f"MISSING fixture {FIXTURE}", file=sys.stderr)
            return 1
        recorded = json.loads(FIXTURE.read_text())["scenarios"]
        bad = [n for n in produced
               if recorded.get(n, {}).get("curve_sha256") != produced[n]["curve_sha256"]]
        for n in sorted(produced):
            mark = "MISMATCH" if n in bad else "ok"
            print(f"  {mark:8s} {n:22s} {produced[n]['curve_sha256'][:16]}…")
        return 1 if bad else 0

    payload = {
        "_note": "GOLDEN wall-replay curves. Regenerate with scripts/capture_replay_golden.py. "
                 "Guards the byte-identical-default invariant; test_determinism does NOT "
                 "(it loops in one process against one build).",
        "_hashed_fields": HASHED_FIELDS,
        "_provenance": _provenance(),
        "scenarios": produced,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, indent=2) + "\n")
    for n in sorted(produced):
        print(f"  {n:22s} sha={produced[n]['curve_sha256'][:16]}… "
              f"orders={produced[n]['total_orders']:4d} final={produced[n]['final_equity']}")
    print(f"\nwrote {FIXTURE}  provenance={payload['_provenance']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
