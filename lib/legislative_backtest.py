"""Passage-probability calibration backtest (analysis-side, PURE core).

The one OBJECTIVE, self-labeling eval for the legislative catalyst: score
``congress.heuristic_passage_prob`` against KNOWN legislative outcomes from a COMPLETED
Congress (Congress.gov gives every bill a terminal became_law / died status for free).
Answers the only question that matters for the passage gate — is the heuristic calibrated?
— and directly tunes the ``_STATUS_BASE`` table + the must-pass/cosponsor bumps.

LEAKAGE GUARD (the real subtlety): an enacted bill's *terminal* status is ``became_law``
(heuristic 1.0) — scoring that is a tautology. So we score each bill at its **peak
NON-terminal status** (the furthest milestone it reached BEFORE enactment/death), replayed
from its action timeline, and ask "did it EVENTUALLY become law?". That measures real
predictive skill: given a bill got as far as X, does the heuristic's X-probability match the
observed eventual-enactment rate?

WALL: pure + analysis-side. Imports only stdlib + lib.dataflows.congress (itself wall-clean);
never the broker or a trading limit. A wall test enforces this.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from lib.dataflows.congress import derive_status, heuristic_passage_prob

# Non-terminal milestone ladder (rank = how far a bill advanced). Terminal states
# (became_law / failed) are excluded — we score the peak status BEFORE the outcome.
_STATUS_RANK = {
    "introduced": 0, "committee": 1, "reported": 2,
    "passed_one": 3, "passed_both": 4, "to_president": 5,
}


# --------------------------------------------------------------------------- #
# Timeline replay                                                             #
# --------------------------------------------------------------------------- #

def statuses_from_actions(actions: List[dict]) -> List[str]:
    """Map each action's text to a coarse status via the shared derive_status. ``actions``
    is the Congress.gov action list (each ``{text, actionDate}``)."""
    out: List[str] = []
    for a in (actions or []):
        text = a.get("text") if isinstance(a, dict) else None
        out.append(derive_status(str(text or "")))
    return out


def peak_nonterminal_status(actions: List[dict]) -> str:
    """The furthest-advanced NON-terminal status a bill reached across its whole timeline.
    Defaults to 'introduced' (every bill is at least introduced). This is the leak-free
    status we score: it excludes the terminal became_law/failed action."""
    best = "introduced"
    best_rank = 0
    for st in statuses_from_actions(actions):
        r = _STATUS_RANK.get(st)
        if r is not None and r > best_rank:
            best_rank, best = r, st
    return best


# --------------------------------------------------------------------------- #
# Pure scorers over pairs = [(prob in [0,1], label in {0,1})]                  #
# --------------------------------------------------------------------------- #

def brier_score(pairs: List[tuple]) -> Optional[float]:
    """Mean squared error of the probability vs the 0/1 outcome (lower = better; 0.25 is the
    always-0.5 baseline, and the base-rate-constant predictor scores p(1-p))."""
    ps = [(float(p), float(y)) for p, y in pairs]
    if not ps:
        return None
    return round(sum((p - y) ** 2 for p, y in ps) / len(ps), 5)


def auc(pairs: List[tuple]) -> Optional[float]:
    """Rank-based ROC-AUC (Mann-Whitney), tie-aware. Imbalance-robust: the probability a
    random enacted bill is ranked above a random non-enacted one. None if a class is absent."""
    pos = [float(p) for p, y in pairs if float(y) == 1.0]
    neg = [float(p) for p, y in pairs if float(y) == 0.0]
    if not pos or not neg:
        return None
    # sum of ranks of positives (average ranks for ties), 1-indexed
    allp = sorted(((p, 1) for p in pos), key=lambda x: x[0]) + sorted(((p, 0) for p in neg), key=lambda x: x[0])
    allp.sort(key=lambda x: x[0])
    ranks = [0.0] * len(allp)
    i = 0
    while i < len(allp):
        j = i
        while j + 1 < len(allp) and allp[j + 1][0] == allp[i][0]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0  # average of 1-indexed ranks in the tie block
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    sum_pos_ranks = sum(r for r, (_, lbl) in zip(ranks, allp) if lbl == 1)
    n_pos, n_neg = len(pos), len(neg)
    return round((sum_pos_ranks - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg), 4)


def reliability_curve(pairs: List[tuple], n_bins: int = 10) -> List[dict]:
    """Bin predictions into [0,0.1),...,[0.9,1.0] and report per-bin predicted-vs-observed.
    A calibrated model has mean_pred ≈ mean_obs in every populated bin."""
    bins = [{"lo": round(b / n_bins, 3), "hi": round((b + 1) / n_bins, 3),
             "n": 0, "sum_pred": 0.0, "sum_obs": 0.0} for b in range(n_bins)]
    for p, y in pairs:
        p = min(max(float(p), 0.0), 1.0)
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx]["n"] += 1
        bins[idx]["sum_pred"] += p
        bins[idx]["sum_obs"] += float(y)
    out = []
    for b in bins:
        if b["n"]:
            out.append({"range": f"[{b['lo']:.1f},{b['hi']:.1f})", "n": b["n"],
                        "mean_pred": round(b["sum_pred"] / b["n"], 3),
                        "obs_rate": round(b["sum_obs"] / b["n"], 3)})
    return out


# --------------------------------------------------------------------------- #
# Calibration over a labeled bill set                                         #
# --------------------------------------------------------------------------- #

def records_from_fixture(bills: List[dict]) -> List[dict]:
    """Turn cached fixture bills (each with a RAW ``actions`` timeline) into scored records by
    (re-)deriving the peak non-terminal status with the CURRENT derive_status. Caching raw
    actions — not the pre-derived status — is what makes the backtest re-runnable offline after
    a derive_status change (the calibration re-scores without a re-fetch)."""
    out = []
    for b in (bills or []):
        out.append({
            "bill_id": b.get("bill_id"),
            "peak_status": peak_nonterminal_status(b.get("actions") or []),
            "enacted": int(bool(b.get("enacted"))),
            "title": b.get("title", ""),
            "cosponsors_n": int(b.get("cosponsors_n", 0) or 0),
        })
    return out


def status_enactment_rates(records: List[dict]) -> dict:
    """Empirical P(eventually enacted | peak non-terminal status) per status, with N. This
    is the ground truth the heuristic's _STATUS_BASE table should match. ``records`` are
    {peak_status, enacted 0/1}."""
    agg: dict = {}
    for r in records:
        st = r.get("peak_status", "introduced")
        a = agg.setdefault(st, {"n": 0, "enacted": 0})
        a["n"] += 1
        a["enacted"] += int(bool(r.get("enacted")))
    return {st: {"n": v["n"], "enact_rate": round(v["enacted"] / v["n"], 4),
                 "enacted": v["enacted"]} for st, v in agg.items()}


def build_pairs(records: List[dict],
                heuristic_fn: Callable[[dict], float] = heuristic_passage_prob) -> List[tuple]:
    """Turn labeled bill records into (heuristic_prob, enacted) pairs — the heuristic scored
    at each bill's PEAK NON-TERMINAL status (leak-free). Each record carries peak_status +
    title + cosponsors_n so the must-pass / cosponsor bumps apply as they do live."""
    pairs = []
    for r in records:
        detail = {"status": r.get("peak_status", "introduced"), "title": r.get("title", ""),
                  "cosponsors_n": int(r.get("cosponsors_n", 0) or 0),
                  "latest_action": r.get("latest_action", "")}
        pairs.append((float(heuristic_fn(detail)), float(int(bool(r.get("enacted"))))))
    return pairs


def calibrate(records: List[dict],
              heuristic_fn: Callable[[dict], float] = heuristic_passage_prob) -> dict:
    """Full calibration report over a labeled bill set: Brier, AUC, reliability curve, the
    per-status empirical enactment rates vs the heuristic's base, and the base rate."""
    pairs = build_pairs(records, heuristic_fn)
    n = len(records)
    base_rate = round(sum(1 for r in records if r.get("enacted")) / n, 4) if n else None
    emp = status_enactment_rates(records)
    # heuristic's own prob at each status (title-agnostic, 0 cosponsors) for a clean compare
    heur_at = {st: round(heuristic_fn({"status": st, "title": "", "cosponsors_n": 0}), 4)
               for st in _STATUS_RANK}
    return {
        "n": n, "base_rate": base_rate,
        "brier": brier_score(pairs),
        "brier_baseline_constant": round(base_rate * (1 - base_rate), 5) if base_rate is not None else None,
        "auc": auc(pairs),
        "reliability": reliability_curve(pairs),
        "status_calibration": {
            st: {**emp[st], "heuristic_base": heur_at.get(st)}
            for st in sorted(emp, key=lambda s: _STATUS_RANK.get(s, -1))
        },
    }
