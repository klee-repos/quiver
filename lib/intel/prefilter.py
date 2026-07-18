"""The cost gate: does a section's text plausibly touch the book's universe? PURE.

The deep per-section LLM chain is ~10^5 tokens/section; running it on all ~2.5M historical
sections is out of reach and unnecessary. This keyword prefilter is a HIGH-RECALL first pass:
keep a section if its header OR operative text mentions the book's vocabulary, drop it otherwise.
The chain (a later phase) supplies PRECISION — it reads the operative text and rejects the
sections whose mechanism does not actually reach a holding.

Recall is the contract here, not precision: dropping a true positive is unrecoverable, keeping a
false one is corrected downstream. The vocabulary is therefore the whole judgment surface, and it
is validated (see tests): on 170 real sections it keeps 8.2% overall (0% of an immigration bill,
10% of an energy omnibus) while retaining both known true positives — H.R.1 §20308 (uranium,
catchable only via its HEADER — the operative text says "oil, oil shale, coal, or natural gas" and
names uranium by omission) and §20304 (covered sector / FAST-41).

VOCABULARY DISCIPLINE — the deepest limitation, stated plainly: a theme absent from this list is
invisible to the whole service, which is in direct tension with discovering NEW themes. Keep the
terms specific enough to filter (broad tokens like bare "mineral"/"nuclear" match every energy
section and defeat the gate) but broad enough to cover the book's real surface, and revisit the
list as the book evolves. ``dropped_count`` is logged so silent over-filtering is visible.
"""
from __future__ import annotations

import re
from typing import Iterable, List

# The book's universe vocabulary. SPECIFIC multi-word terms where a bare token would over-match:
# "critical mineral" not "mineral", "nuclear reactor" not "nuclear", "covered sector" not "sector".
# Grouped by sleeve for auditability; order is irrelevant (the regex is an alternation).
_VOCAB = [
    # Compute / semiconductors
    r"semiconduct", r"microelectronic", r"\bchips?\b", r"wafer", r"lithograph", r"foundry",
    # AI / data / networking
    r"artificial intelligence", r"machine learning", r"data cent(?:er|re)", r"supercomput",
    r"high-performance comput", r"quantum comput", r"\bspectrum\b", r"broadband",
    # Power / nuclear / grid / critical minerals
    r"nuclear reactor", r"small modular reactor", r"\bSMR\b", r"uranium", r"critical mineral",
    r"enrich(?:ed|ment) uranium", r"electric(?:ity)? grid", r"grid interconnect",
    r"transmission line", r"covered sector", r"\bFAST-?41", r"FAST Act",
    # Space / robotics / crypto
    r"satellite", r"space launch", r"launch vehicle", r"commercial space",
    r"digital asset", r"cryptocurrenc", r"blockchain", r"stablecoin", r"robotic",
]
_VOCAB_RE = re.compile("|".join(_VOCAB), re.I)


# REGULATION uses a signal bills lack: the CFR is organized BY AGENCY, so a rule from one of
# these regulators is presumptively relevant to a book sleeve regardless of the exact words in
# its operative text (a spent-fuel-cask rule says "cask", not "nuclear reactor", yet is nuclear-
# industry regulation). Substring-matched against the FR agency name -> the operative sections are
# kept and the chain supplies precision. The authoritative agency->industry crosswalk.
_BOOK_REGULATORS = (
    "nuclear regulatory",            # NRC -> nuclear (CEG, URA, SMR, NLR)
    "federal energy regulatory",     # FERC -> power / grid (CEG, GEV, VST)
    "energy department",             # DOE -> power / nuclear / grid
    "federal communications",        # FCC -> networking / spectrum (ANET)
    "industry and security",         # BIS (Commerce) -> semiconductor export controls (SMH, SOXX)
    "federal aviation",              # FAA -> space / launch (SPCX)
    "securities and exchange",       # SEC -> digital assets (BSOL, IBIT)
    "commodity futures",             # CFTC -> crypto derivatives
    "environmental protection",      # EPA -> power / energy (indirect)
)


def matches(text: str) -> bool:
    """True if the text (a section's header + operative body) mentions the book's vocabulary.
    Case-insensitive. The section text as extracted already includes the header echo, so a
    header-only signal (e.g. §20308's "uranium") is seen."""
    return bool(_VOCAB_RE.search(text or ""))


def agency_is_relevant(agency: str) -> bool:
    """True if the issuing agency regulates a book sleeve (the CFR-by-agency crosswalk). Used to
    keep a rule's operative sections even when its text uses domain jargon the vocab misses."""
    a = (agency or "").lower()
    return any(reg in a for reg in _BOOK_REGULATORS)


def matched_terms(text: str) -> List[str]:
    """The distinct vocabulary hits in the text — for the audit trail (why was this kept?)."""
    seen = set()
    out = []
    for m in _VOCAB_RE.finditer(text or ""):
        t = m.group(0).lower()
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def keep(sections: Iterable) -> tuple:
    """Partition Section-like objects (anything with a ``.text``) into (kept, dropped_count).

    Returns the kept sections plus a count of what was filtered, so a caller can LOG the drop
    rate — silent over-filtering (a too-narrow vocabulary quietly hiding the book's surface) is
    the failure mode this count exists to surface."""
    kept = []
    dropped = 0
    for s in sections:
        if matches(getattr(s, "text", "") or ""):
            kept.append(s)
        else:
            dropped += 1
    return kept, dropped
