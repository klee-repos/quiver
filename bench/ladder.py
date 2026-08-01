"""Run the signal ladder and report arms against each other — with the VOID guard.

THE GUARD IS THE POINT. A replay can return a flat curve for two completely different reasons:
the arm had no edge, or a gate refused every order and it never traded at all. Those are
indistinguishable in a summary, and the second one already happened — a source emitting an
untuned stop/target produced 0 orders, +0.00%, and 300 `reward_risk:below_floor` skips against
the real `strategy.yaml`. Reading that as "no edge" would be a false conclusion drawn from a
green-looking run.

So `run_arm` refuses to return a usable result when the skip mix says the run measured a GATE
rather than the wall, and `compare` propagates that refusal instead of ranking a void arm.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

import yaml

from lib.config import load_config
from lib.ledger import Ledger
import lib.wall_replay as wr

# A run whose would-be trades were dominated by ONE gate did not measure the wall.
VOID_SHARE = 0.60
VOID_MIN_SKIPS = 20

# NOT suppressions. These skip reasons mean "handled elsewhere" or "nothing to do", and counting
# them as gate activity is wrong in the direction that MATTERS: it dilutes a real gate's share
# below the threshold and lets a gate-zeroed run read as a clean measurement. Measured on a
# 40-tick run against the real strategy.yaml: reward_risk=250 suppressions sat alongside
# deferred_to_rebalance_buy=283 + deferred_to_rebalance_trim=185, which dragged the real gate's
# share from 95% down to 34% and silently disarmed the guard.
BENIGN_SKIPS = {
    "deferred_to_rebalance_buy",     # the name is handed to the rebalance pass, not refused
    "deferred_to_rebalance_trim",
    "hold",                          # the model declined to act; not a gate
    "no-position (long-only, no short)",   # long-only policy, not a risk gate
}


@dataclass
class ArmResult:
    name: str
    summary: dict
    equity_curve: list
    fills: list
    targets: list
    void: Optional[str] = None            # non-None => DO NOT interpret this arm's numbers
    skip_reasons: dict = field(default_factory=dict)

    @property
    def total_return_pct(self) -> Optional[float]:
        return None if self.void else self.summary.get("total_return_pct")

    def curve_hash(self) -> str:
        payload = [[p.get(k) for k in ("date", "equity", "cash", "n_orders", "n_filled")]
                   for p in self.equity_curve]
        return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


def void_reason(summary: dict) -> Optional[str]:
    """Return a reason string when this run measured a GATE rather than the wall.

    The denominator is WOULD-BE TRADES — orders actually placed plus the entries a gate
    suppressed — not all skips. Benign bookkeeping skips (see BENIGN_SKIPS) are excluded from
    both sides, because a name deferred to the rebalance pass was never refused.

    Fires in both observed shapes:
      * 0 orders, 300 `reward_risk` skips              -> 300/300  = 100%
      * 12 orders, 250 `reward_risk` skips             -> 250/262  =  95%
    and stays quiet when a gate is merely present rather than dominant.
    """
    skips = summary.get("skip_reasons") or {}
    suppressing = {k: v for k, v in skips.items() if k not in BENIGN_SKIPS}
    if not suppressing:
        return None
    top = max(suppressing, key=lambda k: suppressing[k])
    n_top = suppressing[top]
    would_be = summary.get("total_orders", 0) + sum(suppressing.values())
    if n_top < VOID_MIN_SKIPS or would_be <= 0:
        return None
    share = n_top / would_be
    if share < VOID_SHARE:
        return None
    placed = summary.get("total_orders", 0)
    return (f"VOID: {n_top}/{would_be} would-be trades suppressed by {top!r} ({share:.0%}), "
            f"{placed} order(s) placed — this run measured the {top!r} gate, not the wall")


def temp_config(strategy_path, *, rebalance: bool = True, dry_run: bool = True,
                overrides: Optional[dict] = None) -> str:
    """A scratch config pointing at a REAL strategy.yaml. `overrides` deep-merges into the dict
    so a sweep can vary a knob without editing any repo file."""
    scratch = Path(tempfile.mkdtemp(prefix="ladder_mem_"))
    d = {"account_number": "12345678", "dry_run": dry_run,
         "kill_switch_file": str(Path(tempfile.mkdtemp()) / "KILL"),
         "strategy_path": str(strategy_path),
         "risk": {"max_dollars_per_trade": 100, "daily_loss_halt_pct": 20.0,
                  "daily_capital_deploy_cap": 1000, "min_buying_power_buffer": 5,
                  "rebalance_enabled": rebalance,
                  "consistency": {"enabled": True, "max_discretionary_reversals": 1,
                                  "flip_window": 6}},
         "memory": {"dir": str(scratch)},
         "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}

    def merge(dst, src):
        for k, v in (src or {}).items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v
    merge(d, overrides or {})
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(d, p)
    p.close()
    return p.name


def run_arm(name: str, signal_source, *, dates, tickers, price_at, benchmark_at,
            strategy_path, starting_cash: float, rebalance: bool = True,
            overrides: Optional[dict] = None, **backtest_kwargs) -> ArmResult:
    """Run one arm through the REAL wall. Never writes state/ledger.db (scratch ledger)."""
    cp = temp_config(strategy_path, rebalance=rebalance, overrides=overrides)
    cfg = load_config(cp)
    if not cfg.dry_run:
        raise AssertionError("ladder refuses to run against a non-dry-run config")
    led = Ledger(Path(tempfile.mkdtemp(prefix="ladder_led_")) / "l.db")
    res = wr.run_backtest(cfg, led, dates=dates, tickers=tickers,
                          signal_source=signal_source, price_at=price_at,
                          benchmark_at=benchmark_at, starting_cash=starting_cash,
                          config_path=cp, **backtest_kwargs)
    s = res["summary"]
    return ArmResult(name=name, summary=s, equity_curve=res["equity_curve"],
                     fills=res.get("fills", []), targets=res.get("targets", []),
                     void=void_reason(s), skip_reasons=s.get("skip_reasons", {}))


def compare(arms: Sequence[ArmResult], *, baseline: str = "hold") -> dict:
    """Rank arms against `baseline`. A VOID arm is reported but NEVER ranked or differenced."""
    by = {a.name: a for a in arms}
    base = by.get(baseline)
    rows = []
    for a in arms:
        row = {"arm": a.name, "void": a.void,
               "total_return_pct": a.summary.get("total_return_pct"),
               "max_drawdown_pct": a.summary.get("max_drawdown_pct"),
               "total_orders": a.summary.get("total_orders"),
               "n_fills": a.summary.get("n_fills"),
               "turnover_usd": a.summary.get("turnover_usd"),
               "final_equity": a.summary.get("final_equity")}
        if a.void or base is None or base.void:
            row["gap_vs_baseline_pts"] = None
        else:
            row["gap_vs_baseline_pts"] = round(
                (a.summary.get("total_return_pct") or 0.0)
                - (base.summary.get("total_return_pct") or 0.0), 4)
        rows.append(row)
    usable = [r for r in rows if not r["void"] and r["gap_vs_baseline_pts"] is not None]
    usable.sort(key=lambda r: -(r["gap_vs_baseline_pts"] or 0.0))
    return {"baseline": baseline, "rows": rows, "ranked": usable,
            "any_void": any(r["void"] for r in rows)}


def format_comparison(cmp_: dict) -> str:
    lines = [f"  {'arm':<16s} {'return%':>10s} {'gap vs ' + cmp_['baseline']:>16s} "
             f"{'maxDD%':>8s} {'orders':>7s} {'fills':>6s} {'turnover$':>12s}"]
    for r in cmp_["rows"]:
        if r["void"]:
            lines.append(f"  {r['arm']:<16s} {'VOID':>10s}   {r['void'][:70]}")
            continue
        gap = "n/a" if r["gap_vs_baseline_pts"] is None else f"{r['gap_vs_baseline_pts']:+.2f}"
        lines.append(f"  {r['arm']:<16s} {r['total_return_pct']:>10.3f} {gap:>16s} "
                     f"{r['max_drawdown_pct']:>8.2f} {r['total_orders']:>7d} "
                     f"{r['n_fills']:>6d} {r['turnover_usd']:>12.2f}")
    if cmp_["any_void"]:
        lines.append("  !! at least one arm is VOID — it measured a gate, not the wall. "
                     "Do not interpret its number.")
    return "\n".join(lines)
