"""Roll a key player's section-level impacts into a per-ticker / per-sleeve posture. PURE.

The section chain answers "does THIS section reach a holding, how, and is it verified." This module
answers the profile question: given every section a member SPONSORED, where does their legislative
energy point — which tickers do they help, which do they threaten, how strongly?

Weighting is deterministic and Python-owned (never an LLM number):
  - direction sign: helps=+1, hurts=-1, ambiguous=0
  - confidence weight: high=1.0, medium=0.6, low=0.3
  - verifier weight:  UPHELD=1.0, DOWNGRADED=0.5, (unverified)=0.4, REJECTED=0.0
A REJECTED impact contributes nothing (score 0) — the adversarial verifier's veto is absolute.
The per-ticker score is the sum of (sign x confidence x verifier) over the member's sections; a
posture is 'helps' if the net score is positive, 'hurts' if negative, 'mixed' if it nets to ~0 with
evidence on both sides. Nothing here reads a trading limit or the broker.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

_SIGN = {"helps": 1.0, "hurts": -1.0, "ambiguous": 0.0}
_CONF = {"high": 1.0, "medium": 0.6, "low": 0.3}
_VERIFY = {"UPHELD": 1.0, "DOWNGRADED": 0.5, "REJECTED": 0.0}
_VERIFY_DEFAULT = 0.4  # unverified (verifier didn't run / no verdict): discounted, not zero

_EPS = 1e-9


def impact_weight(direction: str, confidence: str, verdict: str) -> float:
    """The deterministic contribution of one section-impact to a ticker's score."""
    sign = _SIGN.get((direction or "").lower(), 0.0)
    conf = _CONF.get((confidence or "").lower(), 0.3)
    ver = _VERIFY.get((verdict or "").upper(), _VERIFY_DEFAULT)
    return sign * conf * ver


def posture_for_ticker(impacts: List[dict]) -> dict:
    """Aggregate a list of section-impact rows for ONE ticker into a posture dict.
    ``impacts`` rows carry direction/confidence/verify_verdict (+ doc_id/sec/span for the trail)."""
    score = 0.0
    helps = hurts = 0
    evidence = []
    for im in impacts:
        w = impact_weight(im.get("direction", ""), im.get("confidence", ""), im.get("verify_verdict", ""))
        score += w
        if w > _EPS:
            helps += 1
        elif w < -_EPS:
            hurts += 1
        if abs(w) > _EPS:
            evidence.append({
                "doc_id": im.get("doc_id"), "sec": im.get("sec"),
                "direction": im.get("direction"), "confidence": im.get("confidence"),
                "verdict": im.get("verify_verdict"), "span": im.get("step1_span"),
                "title": im.get("title"),
            })
    if score > _EPS:
        posture = "helps"
    elif score < -_EPS:
        posture = "hurts"
    elif helps and hurts:
        posture = "mixed"
    else:
        posture = "neutral"
    return {"score": round(score, 4), "posture": posture,
            "n_helps": helps, "n_hurts": hurts, "evidence": evidence}


def build_profile(*, bioguide: str, congress: int, power: List[dict], impacts: List[dict]) -> dict:
    """Assemble the full structured profile for one key player — the dict a renderer turns into
    markdown and a proposer turns into weights. ``power`` = their intel_power rows; ``impacts`` =
    every section-impact from documents they sponsored. Groups impacts by ticker, scores each,
    and orders by absolute conviction."""
    by_ticker: Dict[str, List[dict]] = defaultdict(list)
    docs = {}
    for im in impacts:
        by_ticker[im["ticker"]].append(im)
        docs[im.get("doc_id")] = {"title": im.get("title"), "published": im.get("published"),
                                  "kind": im.get("kind")}
    tickers = {t: posture_for_ticker(ims) for t, ims in by_ticker.items()}
    # drop tickers whose evidence all netted to zero (e.g. every impact REJECTED)
    tickers = {t: p for t, p in tickers.items() if p["evidence"]}
    ordered = dict(sorted(tickers.items(), key=lambda kv: -abs(kv[1]["score"])))
    roles = [{"role": p.get("role"), "committee": p.get("committee"), "chamber": p.get("chamber")}
             for p in power]
    name = next((p.get("name") for p in power if p.get("name")), "")
    return {
        "bioguide": bioguide,
        "name": name,
        "congress": congress,
        "roles": roles,
        "is_key_player": bool(power),
        "n_documents": len(docs),
        "documents": docs,
        "tickers": ordered,
    }
