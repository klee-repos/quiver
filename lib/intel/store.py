"""Persistence for the strategy-intelligence service. Owns its own schema, same pattern as
lib/levers and lib/legislative — but lives in the SAME ledger.db (via Ledger.intel_conn(), a
pass-through to _conn()), because the repo has exactly one sqlite file and a second forfeits the
QUIVER_LEDGER_DB test-isolation override.

Analysis-side of the wall: never imports the sizing/limit/broker modules, never reads a limit.
Store fns take ``c: sqlite3.Connection`` first and inject ``now``/``published`` (no clock here).
The as-of anchor is ALWAYS ``published`` (the document's real date), never fetch time — so a
backtest can never see the future.
"""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

INTEL_SCHEMA = """
-- One row per source document (bill or Federal Register rule). published = the as-of anchor.
CREATE TABLE IF NOT EXISTS intel_documents (
    doc_id      TEXT PRIMARY KEY,          -- '118-hr-1' | 'FR-2026-12345'
    kind        TEXT NOT NULL,             -- bill | rule
    title       TEXT,
    published   TEXT NOT NULL,             -- ISO date. THE as-of anchor. NEVER fetch time.
    sponsor     TEXT,                      -- bioguide (bills only)
    congress    INTEGER,
    agency      TEXT,                      -- rules only
    url         TEXT,
    digest      TEXT,
    fetched_at  TEXT
);

-- One row per section of a document, with deterministic regex facts. kept = survived prefilter.
CREATE TABLE IF NOT EXISTS intel_sections (
    doc_id    TEXT NOT NULL,
    sec       TEXT NOT NULL,
    header    TEXT,
    usc_cites TEXT,                        -- JSON list
    deadline  TEXT,
    kept      INTEGER NOT NULL DEFAULT 0,  -- did it survive the prefilter cost-gate?
    PRIMARY KEY (doc_id, sec)
);

-- THE EVIDENCE TABLE: one row per (section, ticker) claim the chain derived. Append-only.
-- Every row carries the VERBATIM span it reasoned from + the adversarial verifier's verdict.
CREATE TABLE IF NOT EXISTS intel_section_impact (
    doc_id             TEXT NOT NULL,
    sec                TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    direction          TEXT,               -- helps | hurts | ambiguous
    confidence         TEXT,               -- high | medium | low
    step1_what_changes TEXT,
    step1_span         TEXT,               -- VERBATIM quote — the audit anchor
    step2_mechanism    TEXT,
    reasoning          TEXT,
    verify_verdict     TEXT,               -- UPHELD | REJECTED | DOWNGRADED | (none)
    span_verbatim      INTEGER,            -- deterministic char-for-char check
    model              TEXT,
    prompt_version     TEXT,
    scored_at          TEXT,
    PRIMARY KEY (doc_id, sec, ticker)
);
CREATE INDEX IF NOT EXISTS idx_intel_impact_ticker ON intel_section_impact(ticker);

-- POINT-IN-TIME power map: who held a gavel/leadership post, by congress. ~50 rows/congress.
-- source records provenance (stewart@103-115 | congress-legislators@current). NOT donors,
-- NOT ideology, NOT vote history — only the structural power to affect an outcome.
CREATE TABLE IF NOT EXISTS intel_power (
    bioguide  TEXT NOT NULL,
    congress  INTEGER NOT NULL,
    role      TEXT,                         -- chair | ranking | leadership | agency_head
    committee TEXT,
    chamber   TEXT,
    name      TEXT,
    source    TEXT,
    PRIMARY KEY (bioguide, congress, committee)
);

-- SHADOW: what the service WOULD have proposed. No gate reads this — forward evidence only.
CREATE TABLE IF NOT EXISTS intel_shadow_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT,
    kind          TEXT,                     -- ADD | SLEEVE | WEIGHT
    ticker        TEXT,
    sleeve        TEXT,
    target_weight REAL,
    direction     TEXT,
    evidence_json TEXT,                     -- the section_impact rows behind it
    applied       INTEGER NOT NULL DEFAULT 0
);
"""


def ensure_schema(c: sqlite3.Connection) -> None:
    """Create the intel tables if absent. Idempotent. Called from Ledger.ensure_schema
    beside legislative.ensure_schema."""
    c.executescript(INTEL_SCHEMA)


# ---- documents ------------------------------------------------------------------------------
def upsert_document(c: sqlite3.Connection, *, doc_id: str, kind: str, title: str, published: str,
                    sponsor: Optional[str] = None, congress: Optional[int] = None,
                    agency: Optional[str] = None, url: str = "", digest: str = "",
                    fetched_at: str = "") -> None:
    c.execute(
        "INSERT INTO intel_documents (doc_id, kind, title, published, sponsor, congress, agency, "
        "url, digest, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(doc_id) DO UPDATE SET title=excluded.title, published=excluded.published, "
        "sponsor=excluded.sponsor, congress=excluded.congress, agency=excluded.agency, "
        "url=excluded.url, digest=excluded.digest, fetched_at=excluded.fetched_at",
        (doc_id, kind, title, published, sponsor, congress, agency, url, digest, fetched_at))


def upsert_section(c: sqlite3.Connection, *, doc_id: str, sec: str, header: str,
                   usc_cites: Optional[List[str]] = None, deadline: str = "", kept: bool = False) -> None:
    c.execute(
        "INSERT INTO intel_sections (doc_id, sec, header, usc_cites, deadline, kept) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(doc_id, sec) DO UPDATE SET header=excluded.header, "
        "usc_cites=excluded.usc_cites, deadline=excluded.deadline, kept=excluded.kept",
        (doc_id, sec, header, json.dumps(usc_cites or []), deadline, 1 if kept else 0))


def record_impact(c: sqlite3.Connection, *, doc_id: str, sec: str, ticker: str, direction: str,
                  confidence: str, step1_what_changes: str = "", step1_span: str = "",
                  step2_mechanism: str = "", reasoning: str = "", verify_verdict: str = "",
                  span_verbatim: bool = False, model: str = "", prompt_version: str = "",
                  scored_at: str = "") -> None:
    """Append one (section, ticker) impact claim. Idempotent per (doc_id, sec, ticker)."""
    c.execute(
        "INSERT INTO intel_section_impact (doc_id, sec, ticker, direction, confidence, "
        "step1_what_changes, step1_span, step2_mechanism, reasoning, verify_verdict, "
        "span_verbatim, model, prompt_version, scored_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(doc_id, sec, ticker) DO UPDATE SET direction=excluded.direction, "
        "confidence=excluded.confidence, step1_what_changes=excluded.step1_what_changes, "
        "step1_span=excluded.step1_span, step2_mechanism=excluded.step2_mechanism, "
        "reasoning=excluded.reasoning, verify_verdict=excluded.verify_verdict, "
        "span_verbatim=excluded.span_verbatim, model=excluded.model, "
        "prompt_version=excluded.prompt_version, scored_at=excluded.scored_at",
        (doc_id, sec, ticker, direction, confidence, step1_what_changes, step1_span,
         step2_mechanism, reasoning, verify_verdict, 1 if span_verbatim else 0, model,
         prompt_version, scored_at))


def upsert_power(c: sqlite3.Connection, *, bioguide: str, congress: int, role: str,
                 committee: str, chamber: str = "", name: str = "", source: str = "") -> None:
    c.execute(
        "INSERT INTO intel_power (bioguide, congress, role, committee, chamber, name, source) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(bioguide, congress, committee) DO UPDATE SET "
        "role=excluded.role, chamber=excluded.chamber, name=excluded.name, source=excluded.source",
        (bioguide, congress, role, committee, chamber, name, source))


def record_shadow_proposal(c: sqlite3.Connection, *, created_at: str, kind: str,
                           ticker: Optional[str], sleeve: Optional[str],
                           target_weight: Optional[float], direction: str, evidence: dict) -> int:
    cur = c.execute(
        "INSERT INTO intel_shadow_proposals (created_at, kind, ticker, sleeve, target_weight, "
        "direction, evidence_json) VALUES (?,?,?,?,?,?,?)",
        (created_at, kind, ticker, sleeve, target_weight, direction, json.dumps(evidence)))
    return int(cur.lastrowid or 0)


# ---- reads ----------------------------------------------------------------------------------
def power_for(c: sqlite3.Connection, bioguide: str, congress: int) -> List[dict]:
    """Every power row for a member in a congress (empty if not a key player)."""
    rows = c.execute("SELECT * FROM intel_power WHERE bioguide=? AND congress=?",
                     (bioguide, congress)).fetchall()
    return [dict(r) for r in rows]


def impacts_for_sponsor(c: sqlite3.Connection, sponsor: str) -> List[dict]:
    """Every section-impact claim from documents this member SPONSORED — the raw material
    of their profile. Joined to the document for congress/title/published context."""
    rows = c.execute(
        "SELECT i.*, d.sponsor, d.congress, d.title, d.published, d.kind "
        "FROM intel_section_impact i JOIN intel_documents d ON d.doc_id = i.doc_id "
        "WHERE d.sponsor = ? ORDER BY d.published DESC, i.sec",
        (sponsor,)).fetchall()
    return [dict(r) for r in rows]


def all_key_player_sponsors(c: sqlite3.Connection) -> List[dict]:
    """Every member who (a) sponsored a document we ingested AND (b) held power in that
    document's congress — i.e. the key players whose profiles are worth rendering.

    DISTINCT on (sponsor, congress) ONLY — a chair who sits on several committees has multiple
    intel_power rows, and returning role/committee here would emit one row per committee, so a
    caller iterating the result would double-count that player's impacts. Callers that need the
    roles re-fetch them via ``power_for``."""
    rows = c.execute(
        "SELECT DISTINCT d.sponsor AS bioguide, d.congress "
        "FROM intel_documents d JOIN intel_power p "
        "  ON p.bioguide = d.sponsor AND p.congress = d.congress "
        "WHERE d.sponsor IS NOT NULL ORDER BY d.sponsor",
        ).fetchall()
    return [dict(r) for r in rows]
