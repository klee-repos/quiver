"""Findings: a claim plus an exact quote from a stored document (plan §6.3).

``record_finding`` normalizes the quote and the document text in the same way: Unicode NFKC,
curly quotes to straight quotes, dashes to ``-``, every whitespace run to one space. The
normalized quote must be 20 to 1,200 characters long and must occur in the normalized
document. The row stores the normalized quote and its offset in the normalized document
(``quote_start``). A quote that is not found stores nothing.

This module is on the research side of the decision wall: it reads documents, never limits.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from lib.desk import store

KINDS = ("market_size", "growth", "guidance", "margin", "segment", "customer", "competition", "risk",
         "management", "capital", "other")
CREATED_BY = ("desk", "analyst")
QUOTE_MIN, QUOTE_MAX = 20, 1200

_QUOTES = {ord(c): "'" for c in "\u2018\u2019\u201a\u201b"}
_QUOTES.update({ord(c): '"' for c in "\u201c\u201d\u201e\u201f"})
_DASHES = {ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"}
_TABLE = {**_QUOTES, **_DASHES}
_SPACE = re.compile(r"\s+")


class FindingError(ValueError):
    """A finding the desk must fix. The message tells the desk what to change."""


def normalize(text: str) -> str:
    """NFKC, curly quotes to straight, dashes to ``-``, whitespace runs to one space."""
    s = unicodedata.normalize("NFKC", str(text or ""))
    return _SPACE.sub(" ", s.translate(_TABLE))


def record_finding(conn, *, ticker: str, kind: str, claim: str, quote: str, document_id: int,
                   created_by: str, now: str, event_id: Optional[int] = None) -> int:
    """Store one finding and return its id. Raise ``FindingError`` and store nothing when
    the kind, the claim or the quote length is wrong, or the quote is not in the document."""
    if kind not in KINDS:
        raise FindingError(f"kind must be one of {', '.join(KINDS)}; got {kind!r}")
    if created_by not in CREATED_BY:
        raise FindingError(f"created_by must be desk or analyst; got {created_by!r}")
    if not str(claim or "").strip():
        raise FindingError("a finding needs a claim")
    if not str(ticker or "").strip():
        raise FindingError("a finding needs a ticker")
    try:
        doc_id = int(document_id)
    except (TypeError, ValueError):
        raise FindingError(f"document_id must be a number; got {document_id!r}")
    q = normalize(quote).strip()
    if not QUOTE_MIN <= len(q) <= QUOTE_MAX:
        raise FindingError(f"quote must be {QUOTE_MIN} to {QUOTE_MAX:,} characters; got {len(q):,}")
    doc = store.get_document(conn, doc_id)
    if doc is None:
        raise FindingError(f"document {doc_id} does not exist")
    start = normalize(doc["text"]).find(q)
    if start < 0:
        raise FindingError(f"quote not found in document {doc_id}; copy it exactly")
    cur = conn.execute(
        "INSERT INTO findings (ticker, kind, claim, quote, document_id, quote_start, created_by, event_id, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(ticker).upper(), kind, str(claim).strip(), q, doc_id, start, created_by, event_id, now))
    return int(cur.lastrowid)


def get_finding(conn, finding_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM findings WHERE id=?", (int(finding_id),)).fetchone()
    return dict(row) if row else None


def list_findings(conn, ticker: str, limit: int = 10) -> list:
    """The latest findings of one ticker, newest first."""
    rows = conn.execute("SELECT * FROM findings WHERE ticker=? ORDER BY id DESC LIMIT ?",
                        (str(ticker).upper(), int(limit))).fetchall()
    return [dict(r) for r in rows]
