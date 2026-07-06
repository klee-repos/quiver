"""Self-learning evaluation-lever registry (Python, wall-side).

The EVE agent can PROPOSE new evaluation levers (a data source, an analysis
angle, a sentiment weighting). This module records those proposals, tracks which
decisions exercised each lever, and scores each lever's realized alpha vs a
baseline — keeping the levers that improve skill (alpha, not beta) and retiring
the ones that don't.

INVARIANTS (enforced by an AST wall test in tests/test_units.py):
- This module NEVER imports lib.signals / lib.risk / lib.config. A lever cannot
  call sizing functions, the consistency gate, or read trading limits.
- A lever only affects WHICH evaluation inputs the agent uses. It can never
  touch sizing, caps, the consistency gate, or ref_id minting.
- EVE never touches SQLite; proposals arrive as a parsed dict from the analyze.py
  shim (which read them out of the EVE markdown), and Python owns the write.

Scoring reuses lib.memory's grading (directional_return / is_hit / alpha) so a
lever is scored on SKILL (excess-vs-benchmark), not beta — the same ground the
memory scorecard uses. The score is a running mean of per-decision alpha deltas
(lever_alpha - baseline_alpha) over the decisions that exercised the lever; a
lever is score-eligible after ``lever_min_decisions`` and auto-retires when its
running score drops to/below ``lever_retire_alpha``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


LEVERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_levers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,            -- data_source | analysis_angle | sentiment_weight | other
    config_json     TEXT NOT NULL DEFAULT '{}',
    rationale       TEXT,
    status          TEXT NOT NULL DEFAULT 'proposed',  -- proposed | active | kept | retired
    proposed_at     TEXT NOT NULL,
    first_used_at   TEXT,
    decisions_used  INTEGER NOT NULL DEFAULT 0,
    baseline_alpha  REAL NOT NULL DEFAULT 0.0,
    lever_alpha     REAL NOT NULL DEFAULT 0.0,
    score           REAL NOT NULL DEFAULT 0.0,   -- running mean of (lever_alpha - baseline_alpha)
    last_scored_at  TEXT,
    retired_reason  TEXT
);
CREATE TABLE IF NOT EXISTS eval_lever_uses (
    lever_id    INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    used_at     TEXT NOT NULL,
    PRIMARY KEY (lever_id, decision_id),
    FOREIGN KEY (lever_id) REFERENCES eval_levers(id)
);
"""


STATUS_PROPOSED = "proposed"
STATUS_ACTIVE = "active"
STATUS_KEPT = "kept"
STATUS_RETIRED = "retired"


@dataclass
class Lever:
    id: int
    name: str
    kind: str
    config: dict
    rationale: Optional[str]
    status: str
    decisions_used: int
    score: float
    lever_alpha: float
    baseline_alpha: float


def ensure_schema(c: sqlite3.Connection) -> None:
    """Create the eval_levers tables if absent. Idempotent."""
    c.executescript(LEVERS_SCHEMA)


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def propose(c: sqlite3.Connection, name: str, kind: str,
            config: Optional[dict] = None, rationale: Optional[str] = None) -> int:
    """Record a NEW lever proposal. Idempotent on name: a re-proposal of an
    existing name is a no-op (returns the existing id). Proposals start as
    STATUS_PROPOSED — activation is a separate, human/score-gated step.
    """
    existing = c.execute("SELECT id FROM eval_levers WHERE name = ?", (name,)).fetchone()
    if existing:
        return int(existing[0])
    c.execute(
        "INSERT INTO eval_levers (name, kind, config_json, rationale, status, proposed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, kind, json.dumps(config or {}), rationale, STATUS_PROPOSED, _now()),
    )
    return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def activate(c: sqlite3.Connection, lever_id: int) -> None:
    """Move a proposed lever to active (human --approve, or auto_apply_levers)."""
    c.execute(
        "UPDATE eval_levers SET status = ? WHERE id = ? AND status = ?",
        (STATUS_ACTIVE, lever_id, STATUS_PROPOSED),
    )


def active_levers(c: sqlite3.Connection) -> list:
    """Return the levers the agent should exercise this run (status active/kept)."""
    rows = c.execute(
        "SELECT id, name, kind, config_json, rationale, status, decisions_used, "
        "score, lever_alpha, baseline_alpha FROM eval_levers "
        "WHERE status IN (?, ?) ORDER BY id",
        (STATUS_ACTIVE, STATUS_KEPT),
    ).fetchall()
    return [
        Lever(id=int(r[0]), name=r[1], kind=r[2], config=json.loads(r[3] or "{}"),
              rationale=r[4], status=r[5], decisions_used=int(r[6] or 0),
              score=float(r[7] or 0.0), lever_alpha=float(r[8] or 0.0),
              baseline_alpha=float(r[9] or 0.0))
        for r in rows
    ]


def record_use(c: sqlite3.Connection, lever_id: int, decision_id: int) -> None:
    """Mark that a decision exercised this lever. Idempotent per (lever, decision).
    Sets first_used_at on first use; bumps decisions_used."""
    c.execute(
        "INSERT OR IGNORE INTO eval_lever_uses (lever_id, decision_id, used_at) "
        "VALUES (?, ?, ?)",
        (lever_id, decision_id, _now()),
    )
    used = c.execute(
        "SELECT COUNT(*) FROM eval_lever_uses WHERE lever_id = ?", (lever_id,)
    ).fetchone()[0]
    c.execute(
        "UPDATE eval_levers SET decisions_used = ?, first_used_at = COALESCE(first_used_at, ?) "
        "WHERE id = ?",
        (int(used), _now(), lever_id),
    )


def score_lever(c: sqlite3.Connection, lever_id: int,
                decision_id: int, lever_alpha: float, baseline_alpha: float,
                min_decisions: int, retire_alpha: float) -> str:
    """Score one decision's contribution to a lever. Computes a running mean of
    (lever_alpha - baseline_alpha) over the lever's used decisions, then either
    keeps the lever active, marks it KEPT (score-eligible and positive), or
    RETIRES it (score-eligible and <= retire_alpha).

    Returns the new status string. A lever is score-eligible after min_decisions.
    Never raises — a scoring hiccup must never block a tick.
    """
    row = c.execute(
        "SELECT decisions_used, score FROM eval_levers WHERE id = ?", (lever_id,)
    ).fetchone()
    if not row:
        return STATUS_PROPOSED
    n = int(row[0] or 0)
    prev_score = float(row[1] or 0.0)
    delta = float(lever_alpha) - float(baseline_alpha)
    # Running mean: blend the new delta into the prior running mean.
    new_score = (prev_score * max(n - 1, 0) + delta) / max(n, 1) if n > 0 else delta
    c.execute(
        "UPDATE eval_levers SET score = ?, lever_alpha = ?, baseline_alpha = ?, "
        "last_scored_at = ? WHERE id = ?",
        (new_score, float(lever_alpha), float(baseline_alpha), _now(), lever_id),
    )
    status = c.execute("SELECT status FROM eval_levers WHERE id = ?", (lever_id,)).fetchone()[0]
    if status not in (STATUS_ACTIVE, STATUS_KEPT):
        return status
    if n >= min_decisions and new_score <= retire_alpha:
        c.execute(
            "UPDATE eval_levers SET status = ?, retired_reason = ? WHERE id = ?",
            (STATUS_RETIRED, f"underperformance: score={new_score:.4f} <= {retire_alpha}", lever_id),
        )
        return STATUS_RETIRED
    if n >= min_decisions and new_score > 0:
        c.execute("UPDATE eval_levers SET status = ? WHERE id = ?", (STATUS_KEPT, lever_id))
        return STATUS_KEPT
    return status


def render_active_block(c: sqlite3.Connection) -> str:
    """A compact text block injected into the agent's past_context so it knows
    which discovered levers to exercise this run. Empty string when none active.
    """
    levers = active_levers(c)
    if not levers:
        return ""
    lines = ["**Active Evaluation Levers** (self-discovered; Python-gated):"]
    for lv in levers:
        cfg = json.dumps(lv.config) if lv.config else ""
        lines.append(f"- [{lv.kind}] {lv.name} (score={lv.score:+.3f}, n={lv.decisions_used}) {cfg}")
    return "\n".join(lines)
