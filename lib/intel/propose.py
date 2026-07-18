"""Turn an aggregated key-player posture into strategy proposals — additive on tailwinds,
reductive on strong threats. PURE.

The service reads BOTH directions and can act on BOTH:
  - a TAILWIND on a tradable name we don't hold  -> PROPOSE_ADD (or a new-theme PROPOSE_SLEEVE)
  - a strong THREAT on a name we DO hold          -> PROPOSE_REMOVE (winds the position to cash)

Every proposal is tier ``intel`` — which matches neither auto-apply branch in the apply gate, so it
ALWAYS requires a human ``universe-apply --approve``. The human is the final gate on every add AND
every reduction; this module only surfaces the candidate and its evidence.

CONSERVATION is a HIGHER BAR, not a wall (the user's "don't overwrite the seeded focus UNLESS it's a
terrible place to invest"): a threat must clear ``threat_score`` to propose reducing an ordinary
holding, and the stronger ``protected_threat_score`` to propose reducing a PROTECTED seeded name — so
noise can't churn the core book, but a strong, confirmed legislative/regulatory threat can put an
exit in front of the operator. Funding for ADDs is cash-only and capped by ``intel_max_total_pct``.
Nothing here reads a trading limit or the broker; it consumes a book SNAPSHOT passed in by the glue.
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
    ticker_sleeve: Dict[str, str] = field(default_factory=dict)  # ticker -> its sleeve


@dataclass
class ProposeConfig:
    min_score: float = 0.5               # tailwind floor to ADD a non-held name
    add_weight: float = 4.0              # default target weight for a new ADD (%)
    intel_max_total_pct: float = 20.0    # aggregate cap on intel-originated ADDs
    new_sleeve_min_names: int = 2        # >= this many clustered tailwind names -> a new SLEEVE
    threat_score: float = 0.6            # threat magnitude to REDUCE an ordinary held name
    protected_threat_score: float = 1.5  # stronger threat needed to REDUCE a PROTECTED seeded name


def _is_protected(book: BookSnapshot, ticker: str) -> bool:
    """Fail SAFE toward protection: a held name whose sleeve is unknown/empty (a data glitch, or a
    ticker with no resolved sleeve) is treated as PROTECTED — it takes the HIGHER threat bar to
    propose reducing it. Only a name in a sleeve explicitly marked protected=False (e.g. cash) uses
    the ordinary bar. An empty-string sleeve must not slip past as unprotected."""
    sleeve = book.ticker_sleeve.get(ticker)
    if not sleeve or sleeve not in book.sleeves:
        return True
    return bool(book.sleeves[sleeve].get("protected"))


def propose(*, postures: Dict[str, dict], book: BookSnapshot,
            cfg: Optional[ProposeConfig] = None) -> List[dict]:
    """postures: {ticker: {"posture","score","evidence"}} aggregated across key players.
    Returns proposal dicts (ADD / SLEEVE / REMOVE), all tier ``intel`` (human-approve). Deterministic
    order: strongest tailwind ADDs first (funded until the cash budget runs out), then threat-driven
    REMOVEs (strongest threat first)."""
    cfg = cfg or ProposeConfig()
    out: List[dict] = []

    # ---- TAILWIND -> ADD (funded from cash, capped) ----------------------------------------
    budget = max(0.0, min(book.cash_pct, cfg.intel_max_total_pct))
    add_cands = [(t, p) for t, p in postures.items()
                 if p.get("posture") == "helps" and p.get("score", 0.0) >= cfg.min_score
                 and t in book.tradable and t not in book.held]
    add_cands.sort(key=lambda tp: -tp[1].get("score", 0.0))
    for ticker, p in add_cands:
        if budget <= 1e-9:
            break
        w = min(cfg.add_weight, budget)
        sleeve = book.ticker_sleeve.get(ticker)
        kind = "PROPOSE_ADD"
        if sleeve is None:
            cluster = [c for c, _ in add_cands if book.ticker_sleeve.get(c) is None]
            kind = "PROPOSE_SLEEVE" if len(cluster) >= cfg.new_sleeve_min_names else "PROPOSE_ADD"
        out.append({"kind": kind, "ticker": ticker, "sleeve": sleeve, "tier": TIER_INTEL,
                    "target_weight": round(w, 2), "direction": "helps", "score": p.get("score"),
                    "reason": f"intel tailwind {p.get('score'):+.2f} (key-player support)",
                    "evidence": p.get("evidence", [])})
        budget -= w

    # ---- THREAT -> REMOVE (reduce a HELD name to cash; higher bar for protected) ------------
    threat_cands = [(t, p) for t, p in postures.items()
                    if p.get("posture") == "hurts" and t in book.held]
    threat_cands.sort(key=lambda tp: tp[1].get("score", 0.0))  # most negative first
    for ticker, p in threat_cands:
        bar = cfg.protected_threat_score if _is_protected(book, ticker) else cfg.threat_score
        if abs(p.get("score", 0.0)) < bar:
            continue  # not a strong enough threat to put an exit in front of the operator
        out.append({"kind": "PROPOSE_REMOVE", "ticker": ticker,
                    "sleeve": book.ticker_sleeve.get(ticker), "tier": TIER_INTEL,
                    "target_weight": None, "direction": "hurts", "score": p.get("score"),
                    "reason": f"intel threat {p.get('score'):+.2f} (key-player threat; "
                              f"{'protected — ' if _is_protected(book, ticker) else ''}exit to cash)",
                    "evidence": p.get("evidence", [])})
    return out


def protected_reduction_below_bar(proposals: List[dict], book: BookSnapshot,
                                  cfg: Optional[ProposeConfig] = None) -> List[str]:
    """Safety assertion for the caller/test: the tickers of any PROTECTED-name REMOVE that did NOT
    clear ``protected_threat_score``. Must ALWAYS be empty — a protected reduction is only ever
    surfaced on a strong, confirmed threat. (Belt-and-suspenders on the propose() bar above.)"""
    cfg = cfg or ProposeConfig()
    bad = []
    for pr in proposals:
        if pr["kind"] == "PROPOSE_REMOVE" and _is_protected(book, pr["ticker"]):
            if abs(pr.get("score", 0.0)) < cfg.protected_threat_score:
                bad.append(pr["ticker"])
    return bad
