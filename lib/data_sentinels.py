"""Canonical "is this report real data?" predicate, shared by the two layers that ask.

Two callers score reports for degradation, and they used to answer with two
hand-maintained sentinel tuples that had silently drifted apart:

  * ``quiver_eve/run/quill_data.py:_content_ok`` scores the RAW single-channel
    payload a fetcher returned.
  * ``analyze.py:_report_available`` scores the MODEL'S NARRATIVE about that
    payload — and its verdict on ``market_report`` is the only thing standing
    between a crashed price fetch and a real-money Buy.

The drift was a fail-OPEN hole. Every data tool reports a failure by OPENING its
report with ``UNAVAILABLE: <reason>`` (quill_data.py:40/169/172/232/237/271,
quill_data.mjs:52/59/62), and the gather prompt tells the model to copy that
marker verbatim (decide.mjs:204-205) — but only quill_data's tuple listed it.
A long failure message (mjs:52 appends up to 200 chars of stderr) therefore
cleared analyze.py's 40-char floor, matched none of its sentinels, and scored as
REAL DATA, so a Buy survived instead of being downgraded to ERROR.

``opens_with_unavailable`` is that marker, defined ONCE, and both layers reach it
through ``report_is_usable`` — so this rule cannot drift apart again.

The marker is ANCHORED to the report's first non-empty line, never searched for
as a substring. That is measured, not stylistic: a real recorded run
(``state/analyze_logs/2026-07-03_AAPL.json``) carries a full price series and
merely ANNOTATES inline that indicators were missing —
``- UNAVAILABLE: Short-term technical indicators (RSI, MACD, ...)``. A substring
rule ERRORs that healthy, tradable name, and because ``_latest_indicators``
returns ``{}`` on any exception and is deliberately NOT retried
(quill_data.py:137-138, :150-151), "priced but indicators degraded" is routine.

The per-caller SUBSTRING tuples are deliberately NOT merged. They score different
input domains, so the same words mean different things there: a healthy 12k-char
sentiment narrative legitimately QUOTES ``No news found for NVDA``, and a healthy
fundamentals report legitimately contains the English phrase ``no income cushion
during downturns``. Merging the two tuples was measured to flip 6 of 148 real
recorded report bodies from usable to unavailable. Each caller therefore passes
its OWN tuple, and ``tests/test_units.py`` pins that a sentinel one side gains is
either adopted by the other or recorded as domain-only — so they can never
diverge SILENTLY again.

THE RESIDUAL THIS RULE CANNOT CLOSE — and what now does. Everything above scores
LLM PROSE (decide.mjs runGather -> grabSub -> analyze.py:_split_eve_markdown), so a
FABRICATED or paraphrased market report is closed by no sentinel rule. Measured, not
theoretical: ``state/analyze_logs/2026-07-06_AAPL.json`` supplies
``Estimated current price ~$313 (Market Cap $4.592T / ~14.668B shares)`` while its
price window is 34 days stale and "The indicators block returned empty" — and it
scores as usable data. That run's own PM text even says "the ``core_available`` flag
returned false": the honest boolean reached the MODEL and the Python wall never saw
it. Only the model choosing to write Hold stopped a trade, which is prompt luck.

That boolean is no longer discarded. ``quiver_eve/run/contract.mjs`` accumulates the
per-channel ``core_available`` off every tool envelope and emits it as a
``## data_availability`` section — producer-written, never model-authored, always
LAST — and ``analyze.py:assess_data_quality`` ANDs the MARKET channel into
``core_available``. The two halves compose asymmetrically ON PURPOSE: only an
explicit ``false`` has teeth, and a deterministic ``true`` NEVER overrides this
module's verdict. The flag asserts "a price series arrived", not "the model's report
is faithful to it", so a channel that cannot see the narrative may add a veto but
must not grant a licence — and a false FALSE only costs an ERROR (the allocator holds
the prior weight) while a false TRUE is a real-money trade on garbage.

The NARROWED residual, stated rather than papered over:
  * only MARKET is deterministically gated; the other five channels remain
    prose-only (they gate nothing, by design).
  * a deterministic ``true`` still cannot detect prose that MISREPRESENTS data the
    fetcher really did return. What keeps that surface honest is ``sanitize`` in
    contract.mjs, which must stay BROADER than analyze.py's ``^##\\s+`` splitter in
    both its header class and its line-boundary class: JS ``\\s`` misses
    U+001C-001F/U+0085 and ``split("\\n")`` misses CR/VT/FF/NEL/LS/PS, and each gap
    was measured to let a model-authored body OVERWRITE the gated market_report via
    the splitter's last-duplicate-wins rule.
  * sticky-OR accumulation is bounded to the gather-retry window: attempt 1 true,
    attempt 1's text rejected, attempt 2 UNAVAILABLE but paraphrased confidently,
    still reads ``true``. AND-accumulation was rejected because it resurrects the
    transient-miss ERRORs quill_data.py:_retry exists to kill.

Analysis-side and stdlib-pure: no broker, no trading limits, no config.
"""
from __future__ import annotations

import re

# The marker every data tool opens a FAILED report with. Markdown decoration may
# precede it (bullet / bold / heading / block-quote) because the model re-renders
# the tool's text into its own report section rather than pasting it raw. The colon
# is required UNLESS the marker is the whole line (``**UNAVAILABLE**`` on its own):
# quiver_eve/agent/instructions.md:82,:93 describe a colon-less marker, but demanding
# a terminator is what keeps prose like "Unavailable indicators were excluded" from
# matching — there, ``[\s*_]*`` is followed by "i", not by ":" or end-of-line.
_UNAVAILABLE_MARKER_RX = re.compile(
    r"^[\s\-*#>`•_()\[\]|\d.]*unavailable\b[\s*_]*(?::|[-–—]|$)", re.I)

# How many leading non-empty lines may precede the marker. A model routinely titles
# its section ("**Market Data — AAPL**") before copying the tool's text, and a marker
# on line 2 is still a whole-channel failure. Measured over the 148 recorded report
# bodies in state/analyze_logs/: exactly two carry a marker-opening line — the genuine
# whole-report degrade at line #0, and the HEALTHY fully-priced AAPL report whose
# inline sub-channel caveat sits at line #29. A 3-line window therefore catches the
# titled failure shapes with 26 lines of margin before it could touch a healthy report.
_MARKER_SCAN_LINES = 3


def opens_with_unavailable(text) -> bool:
    """True when the UNAVAILABLE marker OPENS one of the report's first few lines.

    Windowed on purpose, in both directions. Scanning the whole body would reject a
    report that merely mentions ``UNAVAILABLE:`` about ONE sub-channel while carrying
    a full price series — measured, that is a routine state and rejecting it skips a
    tradable name. Scanning only line 1 would miss the equally routine case of a model
    that titles its section before copying the tool's failure text.
    """
    seen = 0
    for line in str(text or "").strip().splitlines():
        if not line.strip():
            continue
        if _UNAVAILABLE_MARKER_RX.match(line):
            return True
        seen += 1
        if seen >= _MARKER_SCAN_LINES:
            return False
    return False


def report_is_usable(text, *, min_chars: int, sentinels) -> bool:
    """True only when `text` is real, usable data — the ONE predicate both gates use.

    Three rejections, in order: too short to be a report at all, the anchored
    whole-channel UNAVAILABLE marker, then the caller's own error sentinels.
    ``min_chars`` and ``sentinels`` are per-caller because the two callers score
    different input domains (see the module docstring); the SHARED part — this
    skeleton and the marker — is what the fail-open bug came from.
    """
    t = str(text or "").strip().lower()
    if len(t) < min_chars:                    # empty / near-empty -> unusable
        return False
    if opens_with_unavailable(t):             # whole-channel failure placeholder
        return False
    return not any(s in t for s in sentinels)
