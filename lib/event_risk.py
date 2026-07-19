"""Event-risk + non-confirmation descriptors (F8/P10 + F9/P13) — the analysis-side PRODUCER.

WALL-CLEAN by construction: this module takes only PRIMITIVE inputs (dates, price returns,
polarity) and returns a plain ``dict`` the Python risk layer consumes as an ARGUMENT (exactly like
lib.trend). It imports no lib.risk / lib.signals / lib.config / lib.levers and names no
limit/broker symbol, so the decision-wall test covers it with no grant.

PTJ defense: size DOWN into a KNOWN BINARY you cannot control — an earnings date or a bill/rule's
effective date — and read the market's REACTION to news (the tape, not the headline). The producer
(``tick.py event-risk``) fetches the raw inputs; this module is the pure math that turns them into a
per-ticker ``severity`` (and the raw trend/vol fields) the plan's buy gate + de-risk consume.
"""

from __future__ import annotations

from datetime import date
from typing import Optional


def days_until(now_iso: Optional[str], event_iso: Optional[str]) -> Optional[int]:
    """Whole days from ``now_iso`` to ``event_iso`` (both ISO date/datetime). None when either is
    missing/unparseable or the event is in the past. Pure (no clock read)."""
    if not now_iso or not event_iso:
        return None
    try:
        d0 = date.fromisoformat(str(now_iso)[:10])
        d1 = date.fromisoformat(str(event_iso)[:10])
    except (TypeError, ValueError):
        return None
    delta = (d1 - d0).days
    return delta if delta >= 0 else None


def event_severity(days_to_event: Optional[int], *, horizon_days: int = 10) -> float:
    """Binary-event severity in [0, 1]: 0 when there is no event (or it is > horizon_days away),
    rising linearly to ~1 as the event nears (0 days -> 1.0). Beyond the horizon it is 0, so a
    far-off event never shrinks a buy. Pure + total."""
    if days_to_event is None or days_to_event < 0 or horizon_days <= 0 or days_to_event > horizon_days:
        return 0.0
    return round(1.0 - days_to_event / float(horizon_days), 3)


def non_confirmation(recent_return: Optional[float], news_polarity: Optional[int]) -> float:
    """The tape's REACTION to news, in [-1, 1] (F9/P13). PTJ read the market's digestion of a
    headline, not the headline. ``news_polarity`` is -1 (bad), +1 (good), 0/None (none).

      * bad news (polarity<0) + price HOLDS/RISES (return>=0) -> constructive latent demand (>0).
      * good news (polarity>0) + price FAILS to rally (return<=0) -> distribution / a downside tell (<0).
      * otherwise (price confirmed the news, or no news) -> ~0.

    Magnitude scales with the size of the non-confirming move (capped). Pure + total."""
    if news_polarity is None or recent_return is None:
        return 0.0
    mag = min(1.0, abs(float(recent_return)) * 10.0)   # ~10% move -> full magnitude
    if news_polarity < 0 and recent_return >= 0:
        return round(mag, 3)          # shrugged off bad news -> constructive
    if news_polarity > 0 and recent_return <= 0:
        return round(-mag, 3)         # couldn't rally on good news -> downside tell
    return 0.0


def build_descriptor(*, now_iso: Optional[str] = None, event_iso: Optional[str] = None,
                     downside_vol: Optional[float] = None, trend_regime: Optional[str] = None,
                     momentum: Optional[float] = None, recent_return: Optional[float] = None,
                     news_polarity: Optional[int] = None, horizon_days: int = 10) -> dict:
    """Assemble the per-ticker descriptor the plan reads. Combines the event-date severity with a
    BEARISH non-confirmation bump (a downside tell raises effective severity -> shrinks the buy /
    feeds de-risk); a CONSTRUCTIVE reaction is recorded but does NOT inflate a buy (defense-only,
    the conservative PTJ reading). Trend/vol pass through as the F3/F4 sizing args. Every field is
    optional; a missing field is a no-op downstream (the sizing fns treat missing as no-shrink)."""
    dte = days_until(now_iso, event_iso)
    sev = event_severity(dte, horizon_days=horizon_days)
    reaction = non_confirmation(recent_return, news_polarity)
    if reaction < 0:
        sev = round(min(1.0, sev + abs(reaction)), 3)   # a downside non-confirmation is event risk too
    desc: dict = {"severity": sev}
    if dte is not None:
        desc["days_to_event"] = dte
    if downside_vol is not None:
        desc["downside_vol"] = downside_vol
    if trend_regime is not None:
        desc["trend_regime"] = trend_regime
    if momentum is not None:
        desc["momentum"] = momentum
    if reaction != 0.0:
        desc["reaction"] = reaction
    return desc
