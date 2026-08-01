"""Parameter sweep with walk-forward validation and the calibration confound controlled.

THREE HAZARDS THIS MODULE EXISTS TO CONTAIN.

1. **PROCESSES, NEVER THREADS.** `lib.wall_replay.run_backtest` mutates three PROCESS-WIDE
   globals with no lock — `tick.LEDGER_DB`, `tick.CONFIG_PATH`, and `market._now_override`.
   Two threads running replays concurrently will silently read each other's ledger and clock.
   `run_grid` uses `ProcessPoolExecutor` for that reason; do not "optimise" it to threads.

2. **THE CALIBRATION CONFOUND.** `tick._run_construct` calls `calibrate.build_calibration`
   against the replay's OWN scratch ledger, so the sweep's outcomes feed back into the sizing
   the sweep is measuring. Every cell is therefore run TWICE — `pin_calibration=True` (learning
   disabled) and False (learning live) — and BOTH are reported. Pinning halves the confound; it
   does not remove it, because the dates are still path-dependent rather than i.i.d.

3. **ARGMAX OVER NOISE.** A grid always has a winner. Walk-forward evaluates the train-set
   argmax on UNSEEN folds with NO re-tuning, and reports `degradation_pct` — how much of the
   in-sample edge survived. A large degradation means the grid fit noise, which is the default
   expectation on one price path, not a surprise.
"""
from __future__ import annotations

import itertools
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from bench import stats as S


@dataclass
class GridSpec:
    """`overrides` maps a dotted config path to the list of values to try."""
    overrides: dict = field(default_factory=dict)
    seeds: Sequence[int] = (0,)

    def points(self) -> list:
        if not self.overrides:
            return [{}]
        keys = sorted(self.overrides)
        return [dict(zip(keys, combo))
                for combo in itertools.product(*(self.overrides[k] for k in keys))]

    def size(self) -> int:
        return len(self.points()) * len(self.seeds)


def _nest(flat: dict) -> dict:
    """{'risk.consistency.enabled': False} -> {'risk': {'consistency': {'enabled': False}}}"""
    out: dict = {}
    for dotted, v in flat.items():
        cur = out
        parts = str(dotted).split(".")
        for k in parts[:-1]:
            cur = cur.setdefault(k, {})
        cur[parts[-1]] = v
    return out


def _cell(args):
    """Top-level so it is picklable by ProcessPoolExecutor."""
    run_fn, point, seed, pin, window = args
    try:
        summary = run_fn(_nest(point), seed, pin, window)
        return {"point": point, "seed": seed, "pinned": pin, "window": window,
                "summary": summary, "error": None}
    except Exception as e:  # noqa: BLE001 — one bad cell must not kill the grid
        return {"point": point, "seed": seed, "pinned": pin, "window": window,
                "summary": None, "error": f"{type(e).__name__}: {e}"}


def run_grid(spec: GridSpec, run_fn: Callable, *, workers: int = 4,
             windows: Sequence = ((None, None),), pin_both: bool = True) -> list:
    """Run every (point, seed, window) cell — pinned AND live when `pin_both`.

    `run_fn(nested_overrides, seed, pin_calibration, window) -> summary dict` must be a
    module-level callable (picklable).
    """
    jobs = []
    for point in spec.points():
        for seed in spec.seeds:
            for window in windows:
                pins = (True, False) if pin_both else (False,)
                for pin in pins:
                    jobs.append((run_fn, point, seed, pin, window))
    if workers <= 1:
        return [_cell(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_cell, jobs))


def summarize(results: Sequence[dict], metric: str = "total_return_pct") -> dict:
    """Per grid-point distribution across seeds. NEVER a bare point estimate."""
    by: dict = {}
    for r in results:
        if r.get("error") or not r.get("summary"):
            continue
        key = (tuple(sorted(r["point"].items())), r["pinned"])
        by.setdefault(key, []).append(r["summary"].get(metric))
    out = {}
    for (point, pinned), vals in by.items():
        vs = [v for v in vals if v is not None]
        if not vs:
            continue
        q = S.iqr(vs)
        out[(point, pinned)] = {
            "n": len(vs), "median": (q[1] if q else (sorted(vs)[len(vs) // 2])),
            "iqr": q, "min": min(vs), "max": max(vs), "values": sorted(vs)}
    return out


def walk_forward(results_train: Sequence[dict], results_test: Sequence[dict],
                 *, metric: str = "total_return_pct",
                 default_point: Optional[dict] = None) -> dict:
    """Pick the argmax on TRAIN, evaluate it on TEST, and report what survived.

    `degradation_pct` is the honest headline: the fraction of the in-sample edge lost out of
    sample. `beat_default_oos` compares the tuned point to the shipped default ON TEST ONLY.
    """
    tr = summarize(results_train, metric)
    te = summarize(results_test, metric)
    if not tr or not te:
        return {"verdict": None, "reason": "empty train or test grid"}

    best_key = max(tr, key=lambda k: tr[k]["median"])
    in_sample = tr[best_key]["median"]
    oos = te.get(best_key, {}).get("median")
    if oos is None:
        return {"verdict": None, "reason": "argmax point absent from the test grid"}

    # The default must be read in the SAME calibration regime as the winner. Matching on the
    # point alone (`k[0] == want`) silently resolved to whichever pin mode `run_grid` emitted
    # first — always pinned=True — so a live-calibration winner was compared against a
    # pinned-calibration default. Reproduced: the shipped default declared to BEAT ITSELF,
    # "verdict: beats, reason: IQRs disjoint", purely from the regime mismatch. Comparing across
    # pin modes destroys the entire reason both modes are run.
    default_key = None
    default_missing = None
    if default_point is not None:
        want = tuple(sorted(default_point.items()))
        cand = (want, best_key[1])
        if cand in te:
            default_key = cand
        else:
            default_missing = (f"default point absent from the test grid at "
                               f"pinned={best_key[1]} — refusing to compare across "
                               f"calibration regimes")
    default_oos = te[default_key]["median"] if default_key else None

    # `degradation_pct` is a RATIO, so it is undefined when there was no in-sample edge to lose
    # (in_sample <= 0). Reporting "-250% degradation" off a negative denominator reads like a
    # catastrophe and means nothing. `edge_delta` is always defined; prefer it.
    edge_delta = round(oos - in_sample, 6) if (oos is not None and in_sample is not None) else None
    degradation = None
    degradation_reason = None
    if in_sample is None or oos is None:
        degradation_reason = "missing fold median"
    elif in_sample <= 0:
        degradation_reason = ("in-sample median <= 0 — a retained-edge RATIO is undefined "
                              "(there was no in-sample edge to lose); read edge_delta instead")
    else:
        degradation = round((1.0 - (oos / in_sample)) * 100.0, 3)

    verdict = None
    if default_key is not None:
        cmp_ = S.beats_null(te[best_key]["values"], te[default_key]["values"])
        verdict = cmp_["verdict"]
        reason = cmp_["reason"]
    elif default_missing:
        reason = default_missing
    else:
        reason = "no default point supplied — cannot say the tuned config is better than shipped"

    return {"best_point": dict(best_key[0]), "pinned": best_key[1],
            "default_pinned": best_key[1] if default_key else None,
            "in_sample_median": in_sample, "out_of_sample_median": oos,
            "default_oos_median": default_oos, "degradation_pct": degradation,
            "degradation_reason": degradation_reason, "edge_delta": edge_delta,
            "verdict": verdict, "reason": reason}


def format_summary(summary: dict, *, metric: str = "total_return_pct") -> str:
    lines = [f"     {'point':<48s} {'pin':>5s} {'n':>3s} {'median':>9s} {'IQR':>22s}"]
    for (point, pinned), s in sorted(summary.items(), key=lambda kv: -kv[1]["median"]):
        p = ", ".join(f"{k}={v}" for k, v in point) or "(default)"
        rng = f"[{s['iqr'][0]:.3f}, {s['iqr'][2]:.3f}]" if s["iqr"] else "n/a"
        lines.append(f"     {p[:48]:<48s} {str(pinned):>5s} {s['n']:>3d} "
                     f"{s['median']:>9.3f} {rng:>22s}")
    return "\n".join(lines)
