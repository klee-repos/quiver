"""Pure conviction -> target-weight allocation (the analysis-driven book).

No I/O, no broker, no LLM. Turns each candidate's analysis CONVICTION (plus the
learned calibration multiplier and the regime scalar) into a target WEIGHT vector:
smoothed against the prior weights (day-to-day stickiness), normalized to a
deployable budget, clamped by the operator's per-name / per-sleeve caps (which come
from strategy.yaml — never hardcoded here; the constants below are only fail-safe
fallbacks), with cash as the residual. It emits a target_weight per ticker;
``lib.portfolio`` turns weights into dollars/intents, so there is NO dollar math here
(DRY: the dollar clamp stack lives once, in lib.signals/lib.portfolio).

The decision wall holds: this is the SIZING side. It reads conviction (a model
output) plus prior weights + calibration (ledger-derived memory) — never trading
limits and never the broker. Every transform is deterministic and unit-tested.

Three guarantees the rest of the system relies on:
  * D2 (bad-data safety): a name whose analysis is ERROR / missing HOLDS its prior
    weight — a data outage NEVER sells the book to cash.
  * D3 (consistency): a name flagged ``hold_floor`` (an ungrounded reversal the
    tick.py consistency gate suppressed) cannot be cut below its prior weight here.
  * Cash floor is a HARD floor: the engine names never sum above (100 - cash_floor).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# Fail-safe fallbacks ONLY (operator overrides every one of these via strategy.yaml's
# risk_policy). Never treat these as policy — they exist so a missing field can't crash
# the allocator, not as hardcoded trading parameters.
_DEFAULTS = {
    "per_name_max_pct": 25.0,
    "sleeve_max_pct": 50.0,
    "cash_floor_pct": 5.0,
    "conviction_curve_k": 1.0,     # >1 sharpens (concentrate into best ideas); 1 = linear
    "smoothing_alpha": 0.4,        # EWMA weight on TODAY's target (lower = stickier); Moderate
    "uncertainty_damp": 0.5,       # how much self-reported uncertainty shrinks conviction
    "regime_min_factor": 0.5,      # most a STAND_DOWN regime can cut deployment
    "min_weight_floor_pct": 0.5,   # a smoothed weight below this rounds to 0 (avoid dust)
    # F3 (conviction-driven deployment): a broadly-BEARISH book deploys less -> raises cash.
    "conviction_full_deploy_at": 0.45,   # mean fresh conviction (0..1) that earns FULL deployment
    "conviction_deploy_min_factor": 0.70, # most a weak-conviction book can cut deployment (raise cash)
    "default_conviction": {        # used only when the model omits a numeric conviction
        "Buy": 70.0, "Overweight": 55.0, "Underweight": 35.0,
    },
}

def _policy(policy: Optional[dict], key: str):
    if policy and key in policy and policy[key] is not None:
        return policy[key]
    return _DEFAULTS[key]


_VALID_RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")


def effective_conviction(analysis: Optional[dict], policy: Optional[dict] = None) -> Optional[float]:
    """The 0..1 conviction strength a name's analysis implies, or ``None`` meaning
    "no fresh opinion — keep the prior weight" (Hold, ERROR, or missing analysis).

    * ERROR / missing / unparseable signal -> None (D2: the caller holds prior).
    * Hold -> None (sticky: keep the current allocation, neither add nor cut).
    * Sell -> 0.0 (a real exit signal; gated separately by the consistency layer).
    * Buy / Overweight / Underweight -> conviction/100 (model number, else the
      rating's default), damped by the model's self-reported uncertainty.
    Underweight is additionally halved (a lean-smaller, not a fresh full position).
    """
    if not analysis:
        return None
    signal = str(analysis.get("signal") or "").strip()
    s = signal.lower()
    if s in ("error", "") or signal not in _VALID_RATINGS:
        return None
    if s == "hold":
        return None
    if s == "sell":
        return 0.0
    conv = analysis.get("conviction")
    if conv is None:
        conv = _policy(policy, "default_conviction").get(signal)
    if conv is None:
        return None
    c = max(0.0, min(100.0, float(conv))) / 100.0
    unc = analysis.get("uncertainty")
    if unc is not None:
        damp = _policy(policy, "uncertainty_damp")
        c *= max(0.0, 1.0 - damp * max(0.0, min(100.0, float(unc))) / 100.0)
    if s == "underweight":
        c *= 0.5
    return c


def _deployed_budget(policy: Optional[dict], regime_scalar: float) -> float:
    """The % of the book to deploy into engine names this tick (rest is cash).

    Base deployable = 100 - cash_floor. The regime scalar DERISKS (a STAND_DOWN
    reading < 1 cuts deployment toward more cash, floored at regime_min_factor);
    leaning in (>= 1) deploys the full base — the cash floor stays a HARD floor, so
    lean-in's extra aggressiveness comes from conviction sharpening, never from
    spending the safety cash."""
    base = 100.0 - float(_policy(policy, "cash_floor_pct"))
    base = max(0.0, base)
    rs = float(regime_scalar) if regime_scalar is not None else 1.0
    factor = min(1.0, max(float(_policy(policy, "regime_min_factor")), rs))
    return base * factor


def conviction_deploy_factor(raw_convs, policy: Optional[dict] = None) -> float:
    """F3 — the BOOK-CONVICTION deployment scalar in ``[conviction_deploy_min_factor, 1.0]``.

    A sibling to the macro ``regime_scalar``: it scales TOTAL engine deployment by how bullish
    vs bearish the book's FRESH convictions are, so a broadly-BEARISH book deploys less (raises
    cash) instead of always water-filling to the full budget. The CALLER (tick.py) applies it as
    a book-level cash carve-out AFTER the per-name hysteresis — scaling allocate's internal
    budget instead is absorbed by EWMA + the 2pt dead-band (a no-op on a diversified book).

    ``raw_convs``: the RAW ``effective_conviction`` values (0..1, PRE curve_k, PRE calibration)
    for the names with a FRESH opinion — so curve_k (a within-budget concentration knob) and the
    per-name calibration (relative-share only) stay DEPLOYMENT-neutral. Empty (no fresh
    conviction) -> 1.0 (the all-Hold tick skips allocation upstream anyway). Bounded below by the
    floor so it de-risks WITHOUT dumping; ``min_factor == 1.0`` disables F3 (full deploy always).
    Pure, total, monotone in the mean conviction."""
    convs = [float(c) for c in (raw_convs or []) if c is not None]
    if not convs:
        return 1.0
    mean_c = sum(convs) / len(convs)                                   # 0..1, rating semantics
    ref = float(_policy(policy, "conviction_full_deploy_at"))
    floor = float(_policy(policy, "conviction_deploy_min_factor"))
    if ref <= 0:
        return 1.0
    return max(floor, min(1.0, mean_c / ref))


def _ewma(fresh: float, prior: float, alpha: float) -> float:
    """Sticky blend: alpha on today's fresh target, (1-alpha) on the prior weight."""
    a = max(0.0, min(1.0, alpha))
    return a * fresh + (1.0 - a) * prior


def _apply_caps(weights: Dict[str, float], caps: Dict[str, float],
                floors: Dict[str, float], recipients: Optional[set] = None) -> Dict[str, float]:
    """Clamp each weight to its cap and water-fill the clipped excess onto eligible
    names (proportional to remaining room), honoring per-name floors. ``recipients``
    (when given) restricts who can absorb spillover — held / hold-prior names are
    excluded so a capped conviction name's excess never inflates a name the analysis
    had no fresh opinion on (it falls to cash instead). Iterates to a fixpoint."""
    w = dict(weights)
    allow = recipients if recipients is not None else set(w)
    for _ in range(100):  # fixpoint; bounded backstop
        over = {t: w[t] - caps[t] for t in w if w[t] > caps[t] + 1e-9}
        if not over:
            break
        excess = sum(over.values())
        for t in over:
            w[t] = caps[t]
        # recipients: eligible names strictly under their cap (excludes pinned/held)
        room = {t: caps[t] - w[t] for t in w
                if caps[t] - w[t] > 1e-9 and t not in over and t in allow}
        total_room = sum(room.values())
        if total_room <= 1e-9:
            break  # nowhere to put it -> it falls to cash (residual)
        for t, r in room.items():
            w[t] += excess * (r / total_room)
    # never below a floor (D2/D3 hold-prior)
    for t in w:
        if t in floors and w[t] < floors[t]:
            w[t] = floors[t]
    return w


@dataclass
class Allocation:
    weights: Dict[str, float]            # ticker -> target weight %
    cash_pct: float
    detail: Dict[str, dict]              # ticker -> {conviction, fresh, smoothed, capped, reason}


def allocate_targets(
    candidates: List[dict],
    analyses: Dict[str, dict],
    *,
    calibration: Optional[Dict[str, float]] = None,
    regime_scalar: float = 1.0,
    policy: Optional[dict] = None,
    hold_floors: Optional[Dict[str, float]] = None,
) -> Allocation:
    """Turn per-candidate conviction into a clamped, smoothed target-weight vector.

    candidates: [{ticker, sleeve?, max_weight?, prior_weight?, quotable?}]
    analyses:   {ticker: {signal, conviction?, uncertainty?}}  (signal incl. 'ERROR')
    calibration: {ticker: multiplier} learned from realized outcomes (shrinks to 1.0
                 at low N upstream); applied to conviction before normalization.
    regime_scalar: deploys less in STAND_DOWN, full when leaning in (see _deployed_budget).
    policy: strategy.yaml risk_policy (per_name/sleeve caps, cash_floor, curve_k, alpha...).
    hold_floors: {ticker: floor%} — D3 reversal-suppressed names pinned at their prior weight.

    Returns an Allocation: engine weights (summing to <= 100 - cash_floor) + the cash
    residual + a per-ticker audit detail. Pure; deterministic; no I/O.
    """
    calibration = {str(k).upper(): v for k, v in (calibration or {}).items()}
    analyses = {str(k).upper(): v for k, v in (analyses or {}).items()}
    hold_floors = {str(k).upper(): v for k, v in (hold_floors or {}).items()}
    k = float(_policy(policy, "conviction_curve_k"))
    alpha = float(_policy(policy, "smoothing_alpha"))
    per_name_max = float(_policy(policy, "per_name_max_pct"))
    sleeve_max = float(_policy(policy, "sleeve_max_pct"))
    cash_floor = float(_policy(policy, "cash_floor_pct"))
    min_floor = float(_policy(policy, "min_weight_floor_pct"))
    budget = _deployed_budget(policy, regime_scalar)

    cand = [c for c in candidates if c.get("ticker")]
    prior = {str(c["ticker"]).upper(): max(0.0, float(c.get("prior_weight") or 0.0)) for c in cand}
    sleeve_of = {str(c["ticker"]).upper(): (c.get("sleeve") or "") for c in cand}
    per_cap = {str(c["ticker"]).upper(): min(per_name_max, float(c.get("max_weight") or per_name_max))
               for c in cand}

    detail: Dict[str, dict] = {}
    conv_score: Dict[str, float] = {}   # names with a fresh conviction opinion
    keep_prior: Dict[str, float] = {}   # Hold / ERROR / missing -> hold prior weight (D2)
    reduce_only: set = set()            # Underweight -> a REDUCE intent; target may never exceed prior
    for c in cand:
        t = str(c["ticker"]).upper()
        a = analyses.get(t)
        ec = effective_conviction(a, policy)
        if str((a or {}).get("signal") or "").strip().lower() == "underweight":
            reduce_only.add(t)
        if ec is None:
            keep_prior[t] = prior.get(t, 0.0)
            # ERROR / missing data is a HARD hold-prior floor (D2): never sell on bad data.
            sig = str((a or {}).get("signal") or "").strip().lower()
            reason = "hold_prior:no_analysis" if not a else (
                "hold_prior:error" if sig in ("error", "") else "hold_prior:hold_signal")
            if (not a) or sig in ("error", ""):
                hold_floors[t] = max(hold_floors.get(t, 0.0), prior.get(t, 0.0))
            detail[t] = {"conviction": None, "reason": reason, "prior": prior.get(t, 0.0)}
        else:
            c_adj = ec * float(calibration.get(t, 1.0))
            c_adj = max(0.0, c_adj) ** k if k != 1.0 else max(0.0, c_adj)
            conv_score[t] = c_adj
            detail[t] = {"conviction": ec, "calibrated": c_adj, "reason": "conviction",
                         "prior": prior.get(t, 0.0)}

    # Carve out the prior weight that Hold/ERROR names keep, then share the REST of the
    # deployable budget across the conviction names in proportion to their score.
    reserved = sum(min(keep_prior[t], per_cap.get(t, per_name_max)) for t in keep_prior)
    remaining = max(0.0, budget - reserved)
    score_sum = sum(conv_score.values())
    fresh: Dict[str, float] = {}
    for t in keep_prior:
        fresh[t] = keep_prior[t]
    for t, s in conv_score.items():
        fresh[t] = (s / score_sum * remaining) if score_sum > 1e-12 else 0.0

    # item 4: Underweight is REDUCE-ONLY — proportional renormalization must never push a
    # lean-smaller name's target ABOVE its prior weight. A lone Underweight water-filling UP
    # is the inverse of the model's intent and was the upward-concentration that fed the SOXX
    # churn (SOXX 7% prior -> an inflated ~11-18% target). Clamp BEFORE smoothing so the EWMA
    # blends a reduction, never an inflation. Never blocks a trim-down, a Sell, or a real add.
    for t in reduce_only:
        fresh[t] = min(fresh[t], prior.get(t, 0.0))

    # Day-to-day stickiness: EWMA-blend the fresh target with the prior weight.
    smoothed = {t: _ewma(fresh[t], prior.get(t, 0.0), alpha) for t in fresh}
    # D2/D3 hold-floors: a bad-data or suppressed-reversal name can't fall below prior.
    for t, fl in hold_floors.items():
        if t in smoothed:
            smoothed[t] = max(smoothed[t], fl)

    # Per-name caps + water-fill — only names with POSITIVE conviction absorb spillover. A
    # held / hold-prior name (no fresh opinion) and a Sell / zero-conviction name must NOT be
    # inflated by a capped peer's excess (that would re-grow a name the analysis wants OUT);
    # the unabsorbed excess falls to cash instead.
    capped = _apply_caps(smoothed, per_cap, hold_floors,
                         recipients={t for t, s in conv_score.items() if s > 0 and t not in reduce_only})

    # Per-sleeve cap: clip each sleeve's total, push excess to cash (do not water-fill
    # across sleeves — that would defeat the concentration limit).
    by_sleeve: Dict[str, List[str]] = {}
    for t in capped:
        by_sleeve.setdefault(sleeve_of.get(t, ""), []).append(t)
    for sl, ts in by_sleeve.items():
        if not sl:
            continue
        tot = sum(capped[t] for t in ts)
        if tot > sleeve_max + 1e-9 and tot > 0:
            scale = sleeve_max / tot
            for t in ts:
                capped[t] = max(hold_floors.get(t, 0.0), capped[t] * scale)

    # Drop dust, then enforce the HARD cash floor: if engine names sum above
    # (100 - cash_floor), scale the NON-floored names down until they fit.
    for t in list(capped):
        if capped[t] < min_floor and hold_floors.get(t, 0.0) <= 0:
            capped[t] = 0.0
    total = sum(capped.values())
    ceiling = 100.0 - cash_floor
    if total > ceiling + 1e-9:
        floored_sum = sum(hold_floors.get(t, 0.0) for t in capped)
        adj_total = total - floored_sum
        adj_ceiling = max(0.0, ceiling - floored_sum)
        scale = (adj_ceiling / adj_total) if adj_total > 1e-9 else 0.0
        for t in capped:
            fl = hold_floors.get(t, 0.0)
            capped[t] = fl + (capped[t] - fl) * scale

    weights = {t: round(w, 4) for t, w in capped.items() if w > 1e-9}
    cash_pct = round(max(0.0, 100.0 - sum(weights.values())), 4)
    for t in detail:
        detail[t]["smoothed"] = round(smoothed.get(t, 0.0), 4)
        detail[t]["target_weight"] = weights.get(t, 0.0)
    return Allocation(weights=weights, cash_pct=cash_pct, detail=detail)


def apply_target_hysteresis(
    new_weights: Dict[str, float], prior_weights: Dict[str, float],
    min_delta_pct: float,
) -> Dict[str, float]:
    """Material-change gate on the TARGET weight (F3, anti-churn).

    The allocator re-runs every tick that carries any fresh conviction, so a noisy,
    ~0-alpha signal nudges each name's smoothed target a little every day and the
    rebalancer chases that MOVING target with tiny value-destroying trims. This gate
    keeps a name at its PRIOR target weight unless the fresh conviction moved it by at
    least ``min_delta_pct`` weight-points — a real conviction shift relocates the
    target; day-to-day noise does not.

    REVERT (not blend) to the prior on a sub-threshold move, so a persistent tiny lean
    can NOT slow-accumulate the target away point-by-point: the target only ever moves
    in a single ``>= min_delta_pct`` step. A name with no prior (a fresh add) always
    takes its new weight. ``min_delta_pct <= 0`` disables the gate (byte-identical to
    re-clipping every tick). Pure + total; the CALLER re-derives the cash residual /
    book conservation after (this only decides engine weights).
    """
    if min_delta_pct is None or min_delta_pct <= 0:
        return dict(new_weights)
    prior_u = {str(k).upper(): v for k, v in (prior_weights or {}).items()}
    out: Dict[str, float] = {}
    for t, w_new in new_weights.items():
        w_prior = prior_u.get(str(t).upper())
        if w_prior is not None and abs(float(w_new) - float(w_prior)) < min_delta_pct:
            out[t] = round(float(w_prior), 4)
        else:
            out[t] = w_new
    return out
