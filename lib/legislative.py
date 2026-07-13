"""Legislative-catalyst store + pure gate logic (Python, wall-side).

The legislative-catalyst feature ingests changed US Congress bills, runs a deep-LLM
impact analysis + an adversarial LLM judge, and — only for a bill that clears BOTH a
Python-owned passage-probability gate AND the judge — proposes a universe change that
rides the EXISTING confirm/cooldown/validate_add/human-``--approve`` machinery. This
module owns the SQLite tables and the PURE decision helpers; the network fetch lives in
``lib/dataflows/congress.py`` and the LLM calls in ``lib/legislative_llm.py``.

INVARIANTS (enforced by the AST + source-scan wall tests in tests/test_units.py):
- This module NEVER imports lib.risk / lib.signals / lib.config / lib.levers and never
  reads the broker or a trading limit. It sees bills + analyses only.
- The LLM never touches SQLite: analyses arrive as an already-parsed dict and Python
  owns every write (mirrors lib/levers.py).
- The passage probability is a PYTHON number (``congress.heuristic_passage_prob``) — the
  LLM analysis contract carries NO passage field, so injected bill text can never drive
  the passage gate (``parse_analysis`` drops any passage key by construction).
- Sizing/apply stay in the existing Python wall: this module only records + filters.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import List, Optional

# The impact fields the LLM is allowed to express. Anything else is dropped by
# parse_analysis so an injected blob can never smuggle a field into a Python gate.
DIRECTIONS = ("benefit", "suffer")
HORIZONS = ("near", "medium", "long")
_TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_HOLD = "hold"


LEGISLATIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    bill_id         TEXT PRIMARY KEY,          -- e.g. '119-hr-3633'
    congress        INTEGER,
    chamber         TEXT,
    bill_type       TEXT,
    number          INTEGER,
    title           TEXT,
    status          TEXT,                      -- introduced|committee|passed_house|...|became_law|failed
    latest_action   TEXT,
    latest_action_date TEXT,
    policy_codes    TEXT,                      -- JSON list of Congress policyArea/legislativeSubjects codes
    content_hash    TEXT,                      -- hash(status|latest_action_date|update-incl-text): re-analyze only on change
    passage_prob    REAL,                      -- FINAL Python-owned probability, 0..1 (NEVER from the LLM)
    passage_source  TEXT,                      -- 'heuristic' | 'kalshi_blend'
    impact_json     TEXT,                      -- LLM thesis blob (advisory)
    analysis_model  TEXT,
    analyzed_at     TEXT,
    judged          INTEGER NOT NULL DEFAULT 0,
    judge_verdict   TEXT,                      -- pass|fail|hold
    judge_reason    TEXT,
    judge_votes_json TEXT,
    judged_at       TEXT,
    applied         INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_bills_actionable ON bills(judged, applied, passage_prob);

CREATE TABLE IF NOT EXISTS bill_impacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id       TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    sector        TEXT,
    direction     TEXT,                        -- benefit|suffer
    magnitude     REAL,                        -- 0..1 (LLM ADVISORY; never sizes)
    horizon       TEXT,                        -- near|medium|long
    rationale     TEXT,
    proposal_hash TEXT,                        -- -> universe_change_log.content_hash (NULL until minted)
    created_at    TEXT,
    UNIQUE(bill_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_bill_impacts_ticker ON bill_impacts(ticker);
CREATE INDEX IF NOT EXISTS idx_bill_impacts_hash ON bill_impacts(proposal_hash);

CREATE TABLE IF NOT EXISTS bill_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_update_date TEXT,
    last_update_including_text TEXT,
    polled_at TEXT
);
"""


def ensure_schema(c: sqlite3.Connection) -> None:
    """Create the legislative tables if absent. Idempotent. Called from
    Ledger.ensure_schema beside levers.ensure_schema."""
    c.executescript(LEGISLATIVE_SCHEMA)


def bill_content_hash(status, latest_action_date, update_including_text) -> str:
    """Stable dedup key: re-analyze a bill only when its status / latest-action-date /
    text-inclusive update stamp advances."""
    key = f"{status}|{latest_action_date}|{update_including_text}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Pure gate helpers (no I/O; the offline unit tests are the spec)             #
# --------------------------------------------------------------------------- #

def _extract_json(raw: str) -> dict:
    """Tolerantly pull the FIRST JSON object out of an LLM reply (fenced ```json
    block, or the first balanced {...}). Returns {} on anything unparseable — the
    fail-safe that turns a garbled analysis into zero impacts (never a fabricated one)."""
    if not raw or not isinstance(raw, str):
        return {}
    # 1) whole-string JSON
    s = raw.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        pass
    # 2) fenced ```json ... ``` block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            pass
    # 3) first balanced {...} span
    start = raw.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(raw)):
            ch = raw[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(raw[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except (ValueError, TypeError):
                        break
        start = raw.find("{", start + 1)
    return {}


def parse_analysis(raw: str) -> dict:
    """Parse an analyze_bill reply into a sanitized {impacted_tickers, thesis} dict.

    STRICT + fail-safe: drops any ticker that isn't a plausible symbol, any bad
    direction/horizon, clamps magnitude to [0,1], and — by construction — carries NO
    passage field (the passage probability is Python-owned; the LLM never touches it).
    A malformed/empty reply yields {"impacted_tickers": [], "thesis": ""} so no proposal
    is ever minted from junk."""
    obj = _extract_json(raw)
    impacted: List[dict] = []
    raw_list = obj.get("impacted_tickers") if isinstance(obj, dict) else None
    for it in (raw_list or []):
        if not isinstance(it, dict):
            continue
        ticker = str(it.get("ticker", "") or "").strip().upper()
        if not _TICKER_RE.match(ticker):
            continue
        direction = str(it.get("direction", "") or "").strip().lower()
        if direction not in DIRECTIONS:
            continue
        _mag_raw = it.get("magnitude")
        try:
            magnitude = float(_mag_raw) if _mag_raw is not None else 0.0
        except (TypeError, ValueError):
            magnitude = 0.0
        magnitude = max(0.0, min(1.0, magnitude))
        horizon = str(it.get("horizon", "") or "").strip().lower()
        if horizon not in HORIZONS:
            horizon = "medium"
        rationale = str(it.get("rationale", "") or "").strip()[:500]
        sector = str(it.get("sector", "") or "").strip()[:80]
        impacted.append({"ticker": ticker, "direction": direction, "magnitude": magnitude,
                         "horizon": horizon, "rationale": rationale, "sector": sector})
    thesis = str(obj.get("thesis", "") or "").strip()[:1000] if isinstance(obj, dict) else ""
    return {"impacted_tickers": impacted, "thesis": thesis}


def passes_thresholds(passage_prob, magnitude, *, prob_min: float, impact_min: float) -> bool:
    """The dual gate: high-passage (Python number) AND high-impact (advisory magnitude
    pre-filter). Both are inclusive ``>=``. None/garbage -> False (fail safe)."""
    try:
        p = float(passage_prob)
        m = float(magnitude)
    except (TypeError, ValueError):
        return False
    return p >= float(prob_min) and m >= float(impact_min)


def tally_judge(votes, pass_min: int) -> tuple:
    """Reduce N adversarial judge votes to a verdict. ``votes`` is an iterable of
    'pass'/'fail'/'hold'. Only ``>= pass_min`` PASS votes advance the bill; anything
    else is a non-actionable verdict (conservative — a tie never advances). Returns
    ``(verdict, n_pass)``."""
    vs = [str(v or "").strip().lower() for v in (votes or [])]
    n_pass = sum(1 for v in vs if v == VERDICT_PASS)
    n_hold = sum(1 for v in vs if v == VERDICT_HOLD)
    if n_pass >= int(pass_min):
        return (VERDICT_PASS, n_pass)
    # Distinguish "genuinely rejected" from "uncertain" for observability only —
    # neither advances (actionable_impacts filters verdict == 'pass').
    if n_hold and (n_pass + n_hold) >= int(pass_min):
        return (VERDICT_HOLD, n_pass)
    return (VERDICT_FAIL, n_pass)


# --------------------------------------------------------------------------- #
# Ledger methods (Python owns every write; take a cursor + explicit `now`)     #
# --------------------------------------------------------------------------- #

def get_cursor(c: sqlite3.Connection) -> Optional[dict]:
    row = c.execute("SELECT last_update_date, last_update_including_text, polled_at "
                    "FROM bill_cursor WHERE id = 1").fetchone()
    if not row:
        return None
    return {"last_update_date": row[0], "last_update_including_text": row[1], "polled_at": row[2]}


def set_cursor(c: sqlite3.Connection, last_update_date, last_update_including_text, now: str) -> None:
    """Upsert the single-row poll checkpoint. Armed right after a successful poll (in a
    ``finally``) so the daily-dedup latch holds even when downstream analysis raises."""
    c.execute(
        "INSERT INTO bill_cursor (id, last_update_date, last_update_including_text, polled_at) "
        "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "last_update_date=excluded.last_update_date, "
        "last_update_including_text=excluded.last_update_including_text, "
        "polled_at=excluded.polled_at",
        (last_update_date, last_update_including_text, now),
    )


def upsert_bill(c: sqlite3.Connection, *, bill_id, congress, chamber, bill_type, number,
                title, status, latest_action, latest_action_date, policy_codes,
                content_hash, first_seen, now) -> bool:
    """Insert a new bill or update a CHANGED one. Returns True when the bill is new OR its
    content_hash advanced (→ needs (re)analysis); False when unchanged (skip the LLM).

    On a content change we reset ``judged`` + clear ``analyzed_at`` so it is re-analyzed and
    re-judged, but we do NOT reset ``applied`` — an already-acted bill must never re-surface
    into actionable_impacts (which prevents the re-mint / cash re-debit path)."""
    pc = json.dumps(list(policy_codes or []))
    row = c.execute("SELECT content_hash, analyzed_at FROM bills WHERE bill_id = ?",
                    (bill_id,)).fetchone()
    if row is None:
        c.execute(
            "INSERT INTO bills (bill_id, congress, chamber, bill_type, number, title, status, "
            "latest_action, latest_action_date, policy_codes, content_hash, first_seen, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bill_id, congress, chamber, bill_type, number, title, status, latest_action,
             latest_action_date, pc, content_hash, first_seen, now),
        )
        return True
    if row[0] != content_hash:
        c.execute(
            "UPDATE bills SET congress=?, chamber=?, bill_type=?, number=?, title=?, status=?, "
            "latest_action=?, latest_action_date=?, policy_codes=?, content_hash=?, "
            "judged=0, judge_verdict=NULL, judge_reason=NULL, judge_votes_json=NULL, judged_at=NULL, "
            "analyzed_at=NULL, updated_at=? WHERE bill_id=?",
            (congress, chamber, bill_type, number, title, status, latest_action,
             latest_action_date, pc, content_hash, now, bill_id),
        )
        return True
    # unchanged content but never finished analysis (e.g. a crash between upsert and
    # record_analysis) -> retry so a half-ingested bill isn't stranded.
    return row[1] is None


def set_bill_detail(c: sqlite3.Connection, bill_id: str, status: str, policy_codes, now: str) -> None:
    """Store the structured detail (refined status + CRS policy codes) fetched after the
    cheap poll upsert, unconditionally (the poll row only had a coarse status + no codes)."""
    c.execute("UPDATE bills SET status=?, policy_codes=?, updated_at=? WHERE bill_id=?",
              (status, json.dumps(list(policy_codes or [])), now, bill_id))


def linked_proposal_hashes(c: sqlite3.Connection) -> List[str]:
    """Distinct proposal content_hashes linked to any not-yet-applied bill's impact — the
    set the apply-reconcile checks against universe_change_log to mark bills applied."""
    rows = c.execute(
        "SELECT DISTINCT i.proposal_hash FROM bill_impacts i JOIN bills b ON b.bill_id=i.bill_id "
        "WHERE i.proposal_hash IS NOT NULL AND b.applied=0").fetchall()
    return [r[0] for r in rows if r[0]]


def record_analysis(c: sqlite3.Connection, bill_id: str, *, passage_prob: float,
                    passage_source: str, thesis: str, model: str, now: str) -> None:
    """Persist the Python passage number + the advisory LLM thesis. Impacts are written
    separately via replace_bill_impacts."""
    c.execute(
        "UPDATE bills SET passage_prob=?, passage_source=?, impact_json=?, analysis_model=?, "
        "analyzed_at=?, updated_at=? WHERE bill_id=?",
        (float(passage_prob), passage_source, thesis, model, now, now, bill_id),
    )


def replace_bill_impacts(c: sqlite3.Connection, bill_id: str, impacts: List[dict], now: str) -> None:
    """Replace the impact rows for a bill (clears stale ones on re-analysis). Each impact is
    the sanitized dict from parse_analysis. proposal_hash starts NULL (set at mint time)."""
    c.execute("DELETE FROM bill_impacts WHERE bill_id=?", (bill_id,))
    for imp in (impacts or []):
        c.execute(
            "INSERT OR IGNORE INTO bill_impacts (bill_id, ticker, sector, direction, magnitude, "
            "horizon, rationale, proposal_hash, created_at) VALUES (?,?,?,?,?,?,?,NULL,?)",
            (bill_id, str(imp.get("ticker", "")).upper(), imp.get("sector"), imp.get("direction"),
             float(imp.get("magnitude", 0) or 0), imp.get("horizon"), imp.get("rationale"), now),
        )


def mark_bill_judged(c: sqlite3.Connection, bill_id: str, verdict: str, reason: str,
                     votes_json: str, now: str) -> None:
    c.execute(
        "UPDATE bills SET judged=1, judge_verdict=?, judge_reason=?, judge_votes_json=?, "
        "judged_at=?, updated_at=? WHERE bill_id=?",
        (verdict, (reason or "")[:500], votes_json, now, now, bill_id),
    )


def mark_bill_applied(c: sqlite3.Connection, bill_id: str) -> None:
    c.execute("UPDATE bills SET applied=1 WHERE bill_id=?", (bill_id,))


def link_impact_proposal(c: sqlite3.Connection, bill_id: str, ticker: str, proposal_hash: str) -> None:
    """Record that this bill's impact on ``ticker`` contributed to the minted proposal.
    Called for EVERY contributing bill (even when record_universe_proposal deduped the row)
    so the reconcile can later mark ALL of them applied — closing the multi-bill orphan path."""
    c.execute("UPDATE bill_impacts SET proposal_hash=? WHERE bill_id=? AND ticker=?",
              (proposal_hash, bill_id, str(ticker).upper()))


def bills_for_proposal_hash(c: sqlite3.Connection, proposal_hash: str) -> List[str]:
    """Every bill whose impact is linked to a given proposal content_hash (for the
    apply-reconcile that marks ALL contributing bills applied)."""
    rows = c.execute("SELECT DISTINCT bill_id FROM bill_impacts WHERE proposal_hash=?",
                     (proposal_hash,)).fetchall()
    return [r[0] for r in rows]


def bills_needing_judgment(c: sqlite3.Connection) -> List[dict]:
    """Analyzed-but-unjudged bills that have at least one impact (nothing to judge otherwise)."""
    rows = c.execute(
        "SELECT b.bill_id, b.title, b.status, b.passage_prob, b.impact_json FROM bills b "
        "WHERE b.analyzed_at IS NOT NULL AND b.judged=0 "
        "AND EXISTS (SELECT 1 FROM bill_impacts i WHERE i.bill_id=b.bill_id) "
        "ORDER BY b.passage_prob DESC").fetchall()
    return [{"bill_id": r[0], "title": r[1], "status": r[2], "passage_prob": r[3],
             "thesis": r[4]} for r in rows]


def get_bill_impacts(c: sqlite3.Connection, bill_id: str) -> List[dict]:
    rows = c.execute(
        "SELECT ticker, sector, direction, magnitude, horizon, rationale FROM bill_impacts "
        "WHERE bill_id=? ORDER BY magnitude DESC", (bill_id,)).fetchall()
    return [{"ticker": r[0], "sector": r[1], "direction": r[2], "magnitude": r[3],
             "horizon": r[4], "rationale": r[5]} for r in rows]


def actionable_impacts(c: sqlite3.Connection, *, min_passage_prob: float,
                       min_magnitude: float) -> List[dict]:
    """(bill, impact) rows eligible to become a proposal: the bill is judged PASS, not yet
    applied, its Python passage_prob >= min, and the impact magnitude >= min. Thresholds are
    PARAMS (never baked into SQL) — Python decides actionability, the ledger just stores."""
    rows = c.execute(
        "SELECT b.bill_id, b.passage_prob, b.policy_codes, b.title, "
        "i.ticker, i.direction, i.magnitude, i.horizon, i.rationale, i.sector "
        "FROM bills b JOIN bill_impacts i ON i.bill_id=b.bill_id "
        "WHERE b.judged=1 AND b.judge_verdict=? AND b.applied=0 "
        "AND b.passage_prob >= ? AND i.magnitude >= ? "
        "ORDER BY b.passage_prob DESC, i.magnitude DESC",
        (VERDICT_PASS, float(min_passage_prob), float(min_magnitude)),
    ).fetchall()
    out: List[dict] = []
    for r in rows:
        try:
            codes = json.loads(r[2] or "[]")
        except (ValueError, TypeError):
            codes = []
        out.append({"bill_id": r[0], "passage_prob": r[1], "policy_codes": codes, "title": r[3],
                    "ticker": r[4], "direction": r[5], "magnitude": r[6], "horizon": r[7],
                    "rationale": r[8], "sector": r[9]})
    return out
