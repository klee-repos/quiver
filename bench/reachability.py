"""Reachability audit — which knobs can actually move the curve on THIS harness.

WHY THIS RUNS BEFORE ANY SWEEP. A grid search over a knob that cannot affect the outcome does
not return "no effect" — it returns NOISE, and an argmax over noise looks exactly like a result.
Worse, several of quiver's knobs are provably inert in a replay:

  * `daily_loss_halt_pct` and every `derisk_tiers` entry — `drop_pct` is identically 0.0 in a
    one-tick-per-date replay (measured across 19 and 36 ticks), because the day's baseline is
    created AT the current equity. The halt cannot fire, so the knob cannot bind.
  * every intraday knob — gated off unless `loop.intraday_enabled`.
  * `limit_slippage_pct` — read only under `order.buy_type: limit`.

So: probe each knob at its shipped value and at an extreme, and report REACHABLE or DEAD. A DEAD
knob is excluded from the sweep grid BY CONSTRUCTION, and the exclusion is printed rather than
silent — a benchmark that quietly drops coverage reads as if it covered everything.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Sequence

# (path-into-config, shipped-value, probe-value, note)
DEFAULT_KNOBS: Sequence[tuple] = (
    (("risk", "daily_loss_halt_pct"), 20.0, 0.01,
     "expected DEAD: drop_pct is identically 0.0 in a one-tick-per-date replay"),
    (("risk", "min_buying_power_buffer"), 5, 9000,
     "expected REACHABLE: directly reduces deployable cash"),
    (("risk", "rebalance_enabled"), True, False,
     "expected REACHABLE: switches the entire buy-to-target pass"),
    (("risk", "consistency", "enabled"), True, False,
     "consistency gate — only ever suppresses a DISCRETIONARY reversal, never a rebalance order"),
    (("risk", "consistency", "max_discretionary_reversals"), 1, 0,
     "0 = stop/target-only reversals"),
)


def _curve_sha(summary: dict) -> str:
    keys = ("n_ticks", "final_equity", "total_orders", "total_return_pct",
            "max_drawdown_pct", "n_fills", "turnover_usd")
    return hashlib.sha256(json.dumps([summary.get(k) for k in keys],
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _nest(path: Sequence[str], value) -> dict:
    out: dict = {}
    cur = out
    for k in path[:-1]:
        cur[k] = {}
        cur = cur[k]
    cur[path[-1]] = value
    return out


def probe_knob(runner: Callable[[dict], dict], path: Sequence[str], *, baseline, probe,
               note: str = "") -> dict:
    """Run `runner(overrides)` at `baseline` and at `probe`; report whether anything moved."""
    a = runner(_nest(path, baseline))
    b = runner(_nest(path, probe))
    sha_a, sha_b = _curve_sha(a), _curve_sha(b)
    d_eq = round((b.get("final_equity") or 0.0) - (a.get("final_equity") or 0.0), 6)
    d_orders = (b.get("total_orders") or 0) - (a.get("total_orders") or 0)
    d_turn = round((b.get("turnover_usd") or 0.0) - (a.get("turnover_usd") or 0.0), 6)
    reachable = sha_a != sha_b
    detail = note or ""
    if not reachable:
        detail = (detail + "; " if detail else "") + \
            f"identical summary at {baseline!r} and {probe!r} — knob cannot bind on this harness"
    return {"knob": ".".join(map(str, path)), "baseline": baseline, "probe": probe,
            "reachable": reachable, "delta_final_equity": d_eq, "delta_orders": d_orders,
            "delta_turnover": d_turn, "detail": detail}


def audit(runner: Callable[[dict], dict], knobs: Sequence[tuple] = DEFAULT_KNOBS) -> dict:
    rows = [probe_knob(runner, path, baseline=base, probe=pr, note=note)
            for path, base, pr, note in knobs]
    return {"rows": rows,
            "reachable": [r["knob"] for r in rows if r["reachable"]],
            "dead": [r["knob"] for r in rows if not r["reachable"]]}


def format_audit(table: dict) -> str:
    lines = ["     knob                                    status      d_equity  d_orders"]
    for r in table["rows"]:
        lines.append(f"     {r['knob']:<38s} {'REACHABLE' if r['reachable'] else 'DEAD':<10s} "
                     f"{r['delta_final_equity']:>10.2f} {r['delta_orders']:>9d}")
    if table["dead"]:
        lines.append(f"     EXCLUDED FROM ANY SWEEP (dead): {', '.join(table['dead'])}")
    return "\n".join(lines)
