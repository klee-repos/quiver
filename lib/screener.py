"""Pure stock-screener: discover candidate tickers for a sleeve's thesis (Q3).

No broker, no trading limits, no LLM, no config caps. Given a sleeve's screen
criteria (sector / market-cap floor / valuation ceiling) + the Robinhood allow-list
+ the current holdings, it ranks candidate tickers NOT already held that match the
sleeve, by a momentum score. The market DATA comes from an INJECTED ``provider``
callable — the live one is yfinance-backed (``tradingagents.dataflows.screener_data``,
no new API key); unit tests inject synthetic rows — so this core is deterministic and
fully offline-testable.

Fail-safe by construction: a provider exception or empty data yields NO candidates
(the universe never grows on bad data). The screener only PROPOSES names; Python's
confirmation/cooldown/allow-list(validate_add)/human-approve gates decide whether to
apply, and the sizing layer decides how much. This module sits on the analysis side
of the decision wall: it imports stdlib only (the yfinance provider is injected), and
NEVER imports lib.signals / lib.risk / the broker (a wall test enforces this).
"""

from __future__ import annotations

from typing import Callable, Dict, List

# A candidate provider: screen-criteria dict -> list of candidate rows, each a mapping
# with at least ``ticker`` and optionally ``sector``/``market_cap``/``pe``/``momentum``/
# ``quotable``. Injected so the core stays pure.
CandidateProvider = Callable[[dict], List[dict]]


def _passes(c: dict, screen: dict) -> bool:
    """Does one candidate row clear the sleeve's screen filters?"""
    sector = screen.get("sector")
    if sector and str(c.get("sector", "")).strip().lower() != str(sector).strip().lower():
        return False
    cap_min = screen.get("market_cap_min")
    if cap_min is not None:
        mc = c.get("market_cap")
        if mc is None or float(mc) < float(cap_min):
            return False
    pe_max = screen.get("pe_max")
    if pe_max is not None:
        pe = c.get("pe")
        # A missing or non-positive P/E (loss-making / no data) fails a valuation ceiling.
        if pe is None or float(pe) <= 0 or float(pe) > float(pe_max):
            return False
    if not bool(c.get("quotable", True)):
        return False
    return True


def screen_sleeve(
    sleeve: str, screen: dict, allow_list, held, provider: CandidateProvider, *, top_n: int = 3,
) -> List[dict]:
    """Ranked candidates ``[{ticker, sleeve, score, reason}]`` for ONE sleeve.

    Keeps only tickers that are on the allow-list, NOT already held, and clear the
    screen; ranks by ``momentum`` (descending). ``provider(screen)`` supplies the rows.
    """
    allow = {str(a).strip().upper() for a in (allow_list or [])}
    held_set = {str(h).strip().upper() for h in (held or [])}
    if not screen:
        return []
    try:
        raw = provider(screen) or []
    except Exception:  # noqa: BLE001 — bad data must never grow the universe
        return []
    out: List[dict] = []
    for c in raw:
        t = str(c.get("ticker", "") or "").strip().upper()
        if not t or t in held_set or t not in allow:
            continue
        if not _passes(c, screen):
            continue
        score = float(c.get("momentum") or 0.0)
        out.append({
            "ticker": t, "sleeve": sleeve, "score": score,
            "reason": (f"screen[{sleeve}] match: sector={c.get('sector')} "
                       f"mcap={c.get('market_cap')} pe={c.get('pe')} momentum={score:+.3f}"),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:max(0, int(top_n))]


def screen_book(
    sleeves: Dict[str, dict], allow_list, held, provider: CandidateProvider, *,
    top_n_per_sleeve: int = 1,
) -> List[dict]:
    """Top candidate(s) across every sleeve that defines a ``screen``.

    ``sleeves`` is the strategy.yaml ``{sleeve_name: {"screen": {...}}}`` map. Sleeves
    without a ``screen`` are skipped (their seeded ticker stays — no auto-discovery).
    Returns a flat ``[{ticker, sleeve, score, reason}]`` list, de-duplicated by ticker
    (first/highest-scoring sleeve wins) so the same name isn't proposed for two sleeves.
    """
    results: List[dict] = []
    seen = set()
    for name, spec in (sleeves or {}).items():
        screen = ((spec or {}).get("screen")) or {}
        if not screen:
            continue
        for cand in screen_sleeve(name, screen, allow_list, held, provider, top_n=top_n_per_sleeve):
            if cand["ticker"] in seen:
                continue
            seen.add(cand["ticker"])
            results.append(cand)
    return results
