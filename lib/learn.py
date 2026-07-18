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
        same change every tick is then a ledger no-op (INSERT OR IGNORE). Keeps ``tier``
        so the open-row unique index and the stored tier (which the apply gate reads)
        stay accurate — two sources proposing the same change keep distinct open rows,
        each carrying its true tier. Pooling across tiers is the job of ``change_key``."""
        return hashlib.sha256(f"{self.kind}|{self.ticker}|{self.tier}".encode()).hexdigest()[:16]

    def change_key(self) -> str:
        """Cross-tier identity of the underlying change — (kind, ticker), tier-FREE.
        The recurrence (confirm-over-N) and cooldown (anti-oscillation) reads key on THIS,
        so a screener ``ADD CEG`` and a legislative ``ADD CEG`` pool their confirm-days and
        share one cooldown instead of each source counting independently. Deliberately NOT
        the dedup key (that stays ``content_hash``, tier-aware) — only the pooled reads use it."""
        return hashlib.sha256(f"{self.kind}|{self.ticker}".encode()).hexdigest()[:16]


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


def build_add_proposals(led, goal_id: int, strategy_cfg, candidate_provider,
                        *, goal_gap=None) -> List[Proposal]:
    """Q3 — screener-discovered ADD proposals for sleeves that define a `screen`.

    Reuses lib.screener (pure; the candidate_provider is the only I/O) to rank tickers
    NOT already held that match each sleeve's criteria + the RH allow-list. Each becomes
    a PROPOSE_ADD (TIER_UNIVERSE), so it flows through the SAME confirm/cooldown/approve
    gates as a REMOVE. The initial weight is the sleeve's `screen.add_weight` (default 5%),
    funded from cash at apply time (universe.apply_add). Degrades to [] on any problem —
    the universe never grows on bad data."""
    if strategy_cfg is None or candidate_provider is None or not getattr(strategy_cfg, "sleeves", None):
        return []
    import lib.screener as screener
    held = [h["ticker"] for h in led.active_target_portfolio(goal_id, statuses=("active", "exiting"))]
    out: List[Proposal] = []
    for c in screener.screen_book(strategy_cfg.sleeves, strategy_cfg.rh_tradable_confirmed,
                                  held, candidate_provider, top_n_per_sleeve=1):
        screen = ((strategy_cfg.sleeves.get(c["sleeve"]) or {}).get("screen")) or {}
        add_w = float(screen.get("add_weight", 5.0) or 5.0)
        out.append(Proposal(ADD, c["ticker"], c["sleeve"], TIER_UNIVERSE,
                            f"screener candidate for '{c['sleeve']}': {c['reason']}",
                            goal_gap_pct=goal_gap, target_weight=add_w))
    return out


TIER_LEGISLATIVE = "legislative"


def build_catalyst_proposals(actionable, held_tickers, *, add_weight: float,
                             tier: str = TIER_LEGISLATIVE, allow=frozenset(),
                             policy_area_map=None, remove_enabled: bool = False) -> List[Proposal]:
    """PURE — turn judged-PASS bill impacts into universe proposals (legislative catalyst).

    ``actionable`` is a list of {bill_id, ticker, direction, magnitude, rationale,
    policy_codes, ...} rows (already prob/impact/judge-gated by lib.legislative). Gating,
    per the adversarial review:
      * benefit -> PROPOSE_ADD, but ONLY when the ticker is on the ``allow`` list
        (rh_tradable_confirmed) — an off-list name is undiscoverable/unaddable.
      * suffer  -> PROPOSE_REMOVE, ONLY when ``remove_enabled`` AND the ticker is
        (held AND allow-listed) AND bound to the bill by the operator's structured
        ``policy_area_map`` (ticker -> Congress policyArea/subject codes ∩ the bill's
        policy_codes is non-empty). No map / no code overlap -> NO remove. This never
        matches against attacker-controlled bill title/text.
    Returns unique Proposals (deduped by (kind, ticker)); the caller links every
    contributing bill to the minted proposal's content_hash. Degrades to [] on bad input."""
    allow_u = {str(t).upper() for t in (allow or [])}
    held_u = {str(t).upper() for t in (held_tickers or [])}
    pa_map = {str(k).upper(): {str(x) for x in (v or [])}
              for k, v in (policy_area_map or {}).items()}
    out: List[Proposal] = []
    seen = set()
    aw = float(add_weight)
    for row in (actionable or []):
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker", "") or "").strip().upper()
        direction = str(row.get("direction", "") or "").strip().lower()
        if not ticker:
            continue
        rationale = str(row.get("rationale", "") or "")[:200]
        bill_id = row.get("bill_id")
        if direction == "benefit":
            if ticker not in allow_u:
                continue  # cannot ADD a name outside the human-vetted allow-list
            key = ("PROPOSE_ADD", ticker)
            if key in seen:
                continue
            seen.add(key)
            out.append(Proposal(ADD, ticker, "Legislative Catalyst", tier,
                                f"legislative catalyst {bill_id}: {rationale}", target_weight=aw))
        elif direction == "suffer":
            if not remove_enabled or ticker not in held_u or ticker not in allow_u:
                continue  # REMOVE is off / not held / not allow-listed
            bill_codes = {str(c) for c in (row.get("policy_codes") or [])}
            if not (pa_map.get(ticker) and (pa_map[ticker] & bill_codes)):
                continue  # no structured policy-area binding -> never remove on free text
            key = ("PROPOSE_REMOVE", ticker)
            if key in seen:
                continue
            seen.add(key)
            out.append(Proposal(REMOVE, ticker, None, tier,
                                f"legislative catalyst {bill_id}: {rationale}"))
    return out


def build_proposals(led, goal_id: int, learning_cfg, macro_regime: str, goal_progress,
                    *, strategy_cfg=None, candidate_provider=None) -> dict:
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
    # Q3 — grow the universe: screener ADD proposals. Always run; build_add_proposals is a
    # no-op unless a candidate provider + sleeve `screen` criteria exist (so discovery is
    # enabled by DATA — define a sleeve `screen` — not a flag). APPLYING an ADD still goes
    # through the recurrence/cooldown/validate_add/human-approve gates downstream.
    proposals.extend(build_add_proposals(led, goal_id, strategy_cfg, candidate_provider,
                                         goal_gap=goal_gap))
    auto: List[Proposal] = []
    needs: List[Proposal] = []
    for p in proposals:
        is_auto = ((p.tier == TIER_DERISK and learning_cfg.auto_apply_derisk) or
                   (p.tier == TIER_UNIVERSE and learning_cfg.auto_apply_universe_changes))
        (auto if is_auto else needs).append(p)
    return {"all": proposals, "auto_apply": auto, "needs_approval": needs}
