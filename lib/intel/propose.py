"""Turn an aggregated key-player posture into ADDITIVE strategy proposals. PURE.

The service ADDS to the seeded book; it never overwrites it. That is a STRUCTURAL guarantee, not a
prompt: this module can only emit ``PROPOSE_ADD`` (and a new-sleeve ``PROPOSE_SLEEVE``), never a
REMOVE or a weight-down. So a protected sleeve's weight is untouched BY CONSTRUCTION — there is no
code path here that reduces an existing holding. Exit of a seeded name stays with the human-gated
DERISK path, which the intel service only ever contributes evidence to.

Funding is cash-only and capped: new positions draw from a budget of min(available cash,
``intel_max_total_pct``), so the seeded book can never be diluted below (100 - intel_max_total_pct)%
however strong the signal. Every proposal is tier ``intel`` — which matches neither auto-apply branch
in the apply gate, so it ALWAYS requires a human ``universe-apply --approve``.

Nothing here reads a trading limit or the broker; it consumes a book SNAPSHOT (weights + protected
flags + cash + the tradable allow-list) passed in by the glue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

TIER_INTEL = "intel"


@dataclass
class BookSnapshot:
    """The current book, as plain data (no ledger/broker access)."""
    held: Set[str]                       # tickers currently in the book
    tradable: Set[str]                   # rh_tradable_confirmed allow-list
    sleeves: Dict[str, dict]             # name -> {weight, protected, tickers}
    cash_pct: float                      # free cash weight available to deploy
    ticker_sleeve: Dict[str, str] = field(default_factory=dict)  # ticker -> its sleeve (for grouping)


@dataclass
class ProposeConfig:
    min_score: float = 0.5               # posture score floor to act on
    add_weight: float = 4.0              # default target weight for a new ADD (%)
    intel_max_total_pct: float = 20.0    # aggregate cap on intel-originated weight
    new_sleeve_min_names: int = 2        # a new SLEEVE needs >= this many tailwind names


def propose(*, postures: Dict[str, dict], book: BookSnapshot,
            cfg: Optional[ProposeConfig] = None) -> List[dict]:
    """postures: {ticker: {"posture","score","evidence"}} aggregated across key players.
    Returns a list of additive proposal dicts (kind/ticker/sleeve/tier/target_weight/reason/
    direction/evidence). Deterministic order: strongest tailwind first, funded until the budget
    (min cash, cap) is exhausted. Empty when no name clears the floor or no cash remains."""
    cfg = cfg or ProposeConfig()
    budget = max(0.0, min(book.cash_pct, cfg.intel_max_total_pct))
    out: List[dict] = []

    # candidate = a tailwind name we can trade and don't already hold
    cands = [(t, p) for t, p in postures.items()
             if p.get("posture") == "helps" and p.get("score", 0.0) >= cfg.min_score
             and t in book.tradable and t not in book.held]
    cands.sort(key=lambda tp: -tp[1].get("score", 0.0))

    # group by intended sleeve to decide ADD (sleeve exists) vs PROPOSE_SLEEVE (new theme)
    for ticker, p in cands:
        if budget <= 1e-9:
            break
        w = min(cfg.add_weight, budget)
        sleeve = book.ticker_sleeve.get(ticker)
        kind = "PROPOSE_ADD"
        # a tailwind name mapping to NO existing sleeve is the new-theme signal (the missing rung),
        # but only mint a SLEEVE when enough names cluster there; otherwise a plain ADD to cash.
        if sleeve is None:
            cluster = [c for c, _ in cands if book.ticker_sleeve.get(c) is None]
            kind = "PROPOSE_SLEEVE" if len(cluster) >= cfg.new_sleeve_min_names else "PROPOSE_ADD"
        out.append({
            "kind": kind,
            "ticker": ticker,
            "sleeve": sleeve,
            "tier": TIER_INTEL,
            "target_weight": round(w, 2),
            "direction": "helps",
            "score": p.get("score"),
            "reason": f"intel posture {p.get('score'):+.2f} (key-player tailwind)",
            "evidence": p.get("evidence", []),
        })
        budget -= w
    return out


def would_overwrite_protected(proposals: List[dict], book: BookSnapshot) -> bool:
    """Safety assertion for the caller/test: True if ANY proposal would reduce or remove a
    protected sleeve. Must ALWAYS be False — this module only adds. (A belt-and-suspenders check
    the conservation regression test asserts against.)"""
    protected = {name for name, s in book.sleeves.items() if s.get("protected")}
    for pr in proposals:
        if pr["kind"] in ("PROPOSE_REMOVE", "PROPOSE_DERISK"):
            return True
        # a WEIGHT change targeting a protected sleeve would be overwriting; we never emit one
        if pr.get("sleeve") in protected and pr["kind"] not in ("PROPOSE_ADD",):
            return True
    return False
