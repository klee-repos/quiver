"""Continual-learning scorer (analysis side). Reads the LEDGER only.

Scores each holding's realized contribution against the 15% goal gap and emits
deterministic, proof-bearing proposals: KEEP / FLAG_UNDERPERFORM / PROPOSE_REMOVE /
PROPOSE_DERISK. The risky direction (remove / book-switch) is ALWAYS human-gated;
only de-risk-to-cash may auto-apply, and even that defaults OFF.

May import lib.risk (reflect_memory's side of the wall); it NEVER imports
lib.signals, the broker, or config dollar caps (a wall test asserts this). Degrades
to KEEP-everything when the strategy tables are empty — never raises into a tick.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Optional

import lib.risk as risk

KEEP = "KEEP"
FLAG = "FLAG_UNDERPERFORM"
REMOVE = "PROPOSE_REMOVE"
DERISK = "PROPOSE_DERISK"
ADD = "PROPOSE_ADD"

TIER_SOFT = "soft"
TIER_DERISK = "derisk"
TIER_UNIVERSE = "universe"


@dataclass
class Proposal:
    kind: str
    ticker: Optional[str]
    sleeve: Optional[str]
    tier: str
    reason: str
    goal_gap_pct: Optional[float] = None
    from_book: Optional[str] = None
    to_book: Optional[str] = None
    target_weight: Optional[float] = None

    def content_hash(self) -> str:
        """Dedup key for an OPEN proposal — (kind, ticker, tier). Re-proposing the
        same change every tick is then a ledger no-op (INSERT OR IGNORE)."""
        return hashlib.sha256(f"{self.kind}|{self.ticker}|{self.tier}".encode()).hexdigest()[:16]


def _mean(xs) -> Optional[float]:
    xs = list(xs)
    return (sum(xs) / len(xs)) if xs else None


def classify_holding(returns, target_weight: float, *, goal_behind: bool,
                     learning_cfg) -> Optional[Proposal]:
    """KEEP (None) / FLAG / REMOVE for one holding, with proof.

    INSUFFICIENT data -> KEEP (None). REMOVE only when sustained-underperformance
    AND the book is behind its glidepath; otherwise a soft FLAG. (build_proposals
    fills in ticker/sleeve/goal_gap.)
    """
    window = learning_cfg.underperf_window
    m = risk.sustained_underperformance(
        returns, window=window, min_n=learning_cfg.min_resolved_n,
        hit_floor=learning_cfg.underperf_hit_floor, mean_floor=learning_cfg.underperf_mean_floor)
    if m.value is None or m.value < 1.0:
        return None  # insufficient data OR not underperforming -> KEEP
    if goal_behind:
        contrib = risk.contribution_vs_thesis(_mean(list(returns)[-window:]), target_weight)
        return Proposal(REMOVE, None, None, TIER_UNIVERSE,
                        f"sustained underperformance AND book behind glidepath "
                        f"[{m.proof()}; {contrib.render()}]", target_weight=target_weight)
    return Proposal(FLAG, None, None, TIER_SOFT,
                    f"sustained underperformance, book not behind glidepath [{m.proof()}]")


def build_proposals(led, goal_id: int, learning_cfg, macro_regime: str, goal_progress) -> dict:
    """All proposals for the active goal, split into auto_apply vs needs_approval."""
    goal_behind = bool(goal_progress and goal_progress.get("regime") == "BEHIND")
    goal_gap = goal_progress.get("ahead_behind_pct") if goal_progress else None
    proposals: List[Proposal] = []
    for h in led.active_target_portfolio(goal_id, statuses=("active",)):
        if h.get("sleeve") == "Cash" or not h.get("quotable", 1):
            continue
        ticker = h["ticker"]
        # ticker_return_series rows carry directional_return among other columns;
        # the scorers want the bare float series (oldest first).
        rows = led.ticker_return_series(ticker)
        returns = [r["directional_return"] for r in rows if r.get("directional_return") is not None]
        p = classify_holding(returns, float(h.get("target_weight", 0) or 0),
                             goal_behind=goal_behind, learning_cfg=learning_cfg)
        if p is not None:
            p.ticker = ticker
            p.sleeve = h.get("sleeve")
            p.goal_gap_pct = goal_gap
            proposals.append(p)
    if learning_cfg.derisk_on_standdown and macro_regime == "STAND_DOWN":
        proposals.append(Proposal(DERISK, None, None, TIER_DERISK,
                                  "macro STAND_DOWN reading -> de-risk the engine toward cash",
                                  goal_gap_pct=goal_gap))
    auto: List[Proposal] = []
    needs: List[Proposal] = []
    for p in proposals:
        is_auto = ((p.tier == TIER_DERISK and learning_cfg.auto_apply_derisk) or
                   (p.tier == TIER_UNIVERSE and learning_cfg.auto_apply_universe_changes))
        (auto if is_auto else needs).append(p)
    return {"all": proposals, "auto_apply": auto, "needs_approval": needs}


def render_learning_block(proposal_set: dict, goal_progress) -> str:
    """Read-only digest section. Carries the 'requires approval' disclaimer."""
    ps = proposal_set.get("all", [])
    if not ps:
        return ""
    auto = proposal_set.get("auto_apply", [])
    lines = ["Learning review (advisory — universe changes REQUIRE approval):"]
    if goal_progress:
        lines.append(f"  goal: {goal_progress.get('regime')} "
                     f"({goal_progress.get('ahead_behind_pct')}% vs glidepath)")
    for p in ps:
        gate = "auto-eligible" if p in auto else "needs approval"
        lines.append(f"  [{p.kind}] {p.ticker or 'BOOK'} ({p.tier}, {gate}): {p.reason}")
    return "\n".join(lines)
