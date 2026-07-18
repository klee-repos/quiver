"""Split a bill's USLM XML into sections + deterministic regex facts. PURE.

No network, no LLM, no clock. Given the bytes of a govinfo BILLS USLM document, return one
``Section`` per ``<section>`` element carrying its enum, header, full operative text, and the
structured facts a keyword/gate layer reads WITHOUT a model: US Code citations, an amended-Act
name, a statutory deadline, and dollar amounts. The LLM chain (a later phase) reads ``text``;
this module only extracts, it never judges impact.

Section splitting is deterministic — govinfo publishes real ``<section>`` elements — so the unit
of analysis (the section, not the whole bill) is established here with zero model involvement.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List

from lib.dataflows.errors import DataUnavailableError

# Deterministic fact patterns. These feed observability + the (future) impact reasoning; they
# are NOT the keyword gate (that's lib/intel/prefilter). Kept conservative — a miss drops a fact,
# never a section.
_USC_RE = re.compile(r"\d+\s+U\.S\.C\.\s+\d+[A-Za-z0-9-]*")
_CFR_RE = re.compile(r"\d+\s+CFR\s+(?:Part\s+)?\d+[A-Za-z0-9.\-]*")
_DEADLINE_RE = re.compile(r"[Nn]ot later than [^,.;]{3,60}")
_DOLLAR_RE = re.compile(r"\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|trillion))?")
# An amended Act: a Capitalized phrase ending in "Act" optionally with a year. Real Act short
# titles ("Energy Act of 2020", "Natural Gas Act") never contain a boilerplate pronoun, so a
# candidate carrying one ("...cited as the Test Act", "Nothing in this Act") is rejected.
_ACT_RE = re.compile(r"\b([A-Z][A-Za-z’'&\- ]{4,70}? Act(?: of \d{4})?)\b")
_ACT_PRONOUN_RE = re.compile(r"\b(this|such|said|that|these|those)\b", re.I)

# Section headers that are structural boilerplate — never a substantive change to analyse.
_BOILERPLATE = ("short title", "table of contents", "definitions", "severability",
                "effective date", "authorization of appropriations")


@dataclass
class Section:
    enum: str
    header: str
    text: str
    usc_cites: List[str] = field(default_factory=list)
    cfr_cites: List[str] = field(default_factory=list)
    acts_amended: List[str] = field(default_factory=list)
    deadline: str = ""
    dollar_amounts: List[str] = field(default_factory=list)

    @property
    def is_boilerplate(self) -> bool:
        h = self.header.strip().lower()
        return any(h.startswith(b) for b in _BOILERPLATE)


def _clean(node: ET.Element) -> str:
    """Flatten an element's text to a single normalized string (drops XML structure). Joins
    child text with spaces so adjacent elements (header/body) don't fuse into false tokens."""
    return " ".join(t for t in (s.strip() for s in node.itertext()) if t)


def _acts(text: str) -> List[str]:
    out = []
    for m in _ACT_RE.finditer(text):
        name = m.group(1).strip()
        if not _ACT_PRONOUN_RE.search(name):
            out.append(name)
    # dedupe preserving order
    seen = set()
    return [a for a in out if not (a in seen or seen.add(a))]


def parse_sections(xml_bytes: bytes, *, min_chars: int = 1) -> List[Section]:
    """Parse USLM bytes -> list[Section]. Raises DataUnavailableError on unparseable input
    (fail-safe: the caller skips the doc rather than fabricating sections). A ``<section>`` with
    no enum or no header is skipped (it is a container, not a substantive unit). ``min_chars``
    drops stub sections below a length that could carry a real change."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise DataUnavailableError(f"bill XML unparseable: {e}") from e

    out: List[Section] = []
    for node in root.findall(".//section"):
        enum = (node.findtext("enum", "") or "").strip().rstrip(".")
        header = " ".join((node.findtext("header", "") or "").split())
        if not enum or not header:
            continue
        text = _clean(node)
        if len(text) < min_chars:
            continue
        deadline_m = _DEADLINE_RE.search(text)
        out.append(Section(
            enum=enum,
            header=header,
            text=text,
            usc_cites=_USC_RE.findall(text)[:12],
            acts_amended=_acts(text)[:6],
            deadline=deadline_m.group(0) if deadline_m else "",
            dollar_amounts=_DOLLAR_RE.findall(text)[:8],
        ))
    return out


def parse_fr_sections(xml_bytes: bytes, *, min_chars: int = 1) -> List[Section]:
    """Split a Federal Register rule's full-text XML into Sections — the REGULATION analog of
    parse_sections. FR rules aren't USLM: the operative regulatory changes live in ``<SECTION>``
    elements (each with a ``<SECTNO>`` § number + a ``<SUBJECT>`` title) inside ``<REGTEXT>``, and
    the agency's own ``SUMMARY`` heading block concisely states what the rule does. We emit one
    Section per ``<SECTION>`` (the CFR amendment) plus a synthetic ``SUMMARY`` Section, so the
    same prefilter + chain run over rules unchanged. Fail-SAFE: unparseable input raises."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise DataUnavailableError(f"FR rule XML unparseable: {e}") from e

    out: List[Section] = []

    # 1) The SUMMARY block: the <HD>SUMMARY:</HD> heading followed by its <P> siblings (FR XML is
    #    flat, so collect paragraphs until the next <HD>). High-signal: the agency's own statement.
    parent_map = {c: p for p in root.iter() for c in p}
    for hd in root.iter("HD"):
        if "summary" not in "".join(hd.itertext()).strip().lower()[:12]:
            continue
        parent = parent_map.get(hd)
        if parent is None:
            continue
        kids = list(parent)
        collecting = False
        buf = []
        for el in kids:
            if el is hd:
                collecting = True
                continue
            if collecting:
                if el.tag == "HD":
                    break
                buf.append(_clean(el))
        summary = " ".join(t for t in buf if t)
        if len(summary) >= min_chars:
            out.append(Section(enum="SUMMARY", header="Summary",
                               text=summary, cfr_cites=_CFR_RE.findall(summary)[:12]))
        break

    # 2) Each operative <SECTION> (the amended CFR section): SECTNO -> enum, SUBJECT -> header.
    for node in root.iter("SECTION"):
        sectno = " ".join((node.findtext("SECTNO", "") or "").split())
        subject = " ".join((node.findtext("SUBJECT", "") or "").split()).rstrip(".")
        text = _clean(node)
        if not sectno or len(text) < min_chars:
            continue
        deadline_m = _DEADLINE_RE.search(text)
        out.append(Section(
            enum=sectno.replace("§", "").strip() or sectno,
            header=subject or sectno,
            text=text,
            usc_cites=_USC_RE.findall(text)[:12],
            cfr_cites=_CFR_RE.findall(text)[:12],
            acts_amended=_acts(text)[:6],
            deadline=deadline_m.group(0) if deadline_m else "",
            dollar_amounts=_DOLLAR_RE.findall(text)[:8],
        ))
    return out
