"""SQLite-backed state: per-day dedup, order idempotency, P&L baseline, halt.

Three guarantees the orchestrator relies on:
1. ``ticker_action`` (PK trade_date+ticker) -> at most ONE action per ticker
   per day, even across session restarts or duplicate ticks.
2. ``orders`` (PK ref_id UUID) reserved BEFORE the broker call -> a crash mid
   place is recoverable; retries reuse the same ref_id (Robinhood dedups by it).
3. ``day_baseline.halted`` + the KILL file -> once a halt fires, every later
   tick no-ops until a human clears it.

All writes are atomic sqlite transactions; the DB lives on disk so it survives
sleep/reboot.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_baseline (
    trade_date      TEXT PRIMARY KEY,
    baseline_equity REAL NOT NULL,
    captured_at     TEXT NOT NULL,
    halted          INTEGER NOT NULL DEFAULT 0,
    halt_reason     TEXT
);
CREATE TABLE IF NOT EXISTS ticker_action (
    trade_date TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    signal     TEXT,
    intent     TEXT,
    status     TEXT NOT NULL,          -- decided|dry_run|placed|skipped|error|blocked_guardrail
    detail     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, ticker)
);
CREATE TABLE IF NOT EXISTS orders (
    ref_id          TEXT PRIMARY KEY,
    trade_date      TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT,
    type            TEXT,
    dollar_amount   REAL,
    quantity        REAL,
    submitted_at    TEXT,
    broker_order_id TEXT,
    result_json     TEXT,
    finalized       INTEGER NOT NULL DEFAULT 0,
    -- Phase 5 order-lifecycle columns (also back-filled on old dbs via _migrate).
    order_kind      TEXT DEFAULT 'entry',      -- entry | protective_stop | exit
    limit_price     REAL,
    stop_price      REAL,
    parent_ref_id   TEXT,                        -- links a protective stop to its entry
    state           TEXT DEFAULT 'reserved'      -- reserved|filled|partial|unfilled|cancelled|stop_placed|triggered
);
CREATE TABLE IF NOT EXISTS notifications (
    trade_date   TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- digest|halt|auth_error
    content_hash TEXT NOT NULL,
    recipients   TEXT,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (trade_date, kind)
);
-- Decision memory (ground of record for the scorecard). One row per analysis
-- decision; `outcomes` links 1:1 once the call is old enough to score. Columns
-- run_id / position_pct / next_review_hours / decision_price are filled by later
-- phases (multi-run, structured sizing, RH quotes) and are NULL until then.
CREATE TABLE IF NOT EXISTS decisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date        TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    run_id            TEXT,
    decided_at        TEXT NOT NULL,
    signal            TEXT,
    intent            TEXT,
    position_pct      REAL,
    entry_price       REAL,
    stop_loss         REAL,
    next_review_hours REAL,
    decision_price    REAL,
    rationale         TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker, decided_at);
CREATE TABLE IF NOT EXISTS outcomes (
    decision_id        INTEGER PRIMARY KEY,    -- 1:1 with decisions.id
    resolved_at        TEXT NOT NULL,
    holding_days       INTEGER,
    directional_return REAL,                   -- (price_now - decision_price)/decision_price
    benchmark_return   REAL,
    alpha              REAL,
    realized_pnl       REAL,                    -- position-level (broker basis); NULL in dry-run
    unrealized_pnl     REAL,
    scored_against     TEXT                     -- 'directional' | 'both'
);
-- Append-only action event log (intraday multi-run). Feeds the cooldown,
-- per-ticker action count, and on-change gate. `ticker_action` stays the
-- per-(date,ticker) LATEST snapshot used by the digest; this is the history.
CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    run_id        TEXT,
    ts            TEXT NOT NULL,
    signal        TEXT,
    intent        TEXT,
    status        TEXT,
    detail        TEXT,
    dollar_amount REAL
);
CREATE INDEX IF NOT EXISTS idx_actions_ticker ON actions(trade_date, ticker, ts);
-- Per-ticker cadence state: when the ticker is next due for re-analysis and how
-- many analyses it has had today (the LLM-cost budget). Reset implicitly per day
-- (the date is part of the key).
CREATE TABLE IF NOT EXISTS ticker_schedule (
    trade_date     TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    next_due_ts    TEXT,
    analyses_today INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, ticker)
);
"""


@dataclass
class Baseline:
    trade_date: str
    baseline_equity: float
    halted: bool
    halt_reason: Optional[str]


class Ledger:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate_orders(c)

    @staticmethod
    def _migrate_orders(c) -> None:
        """Back-fill Phase 5 lifecycle columns on an orders table created earlier.

        SQLite has no 'ADD COLUMN IF NOT EXISTS', so we diff PRAGMA table_info and
        add only what's missing. Idempotent and safe on a fresh db (CREATE already
        made them) or an old one (this adds them)."""
        existing = {row[1] for row in c.execute("PRAGMA table_info(orders)").fetchall()}
        additions = {
            "order_kind": "TEXT DEFAULT 'entry'",
            "limit_price": "REAL",
            "stop_price": "REAL",
            "parent_ref_id": "TEXT",
            "state": "TEXT DEFAULT 'reserved'",
        }
        for col, decl in additions.items():
            if col not in existing:
                c.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")

    # --- daily baseline / halt ------------------------------------------------

    def get_or_create_baseline(self, trade_date: str, equity_now: float, now_iso: str) -> Baseline:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM day_baseline WHERE trade_date=?", (trade_date,)
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO day_baseline (trade_date, baseline_equity, captured_at) "
                    "VALUES (?,?,?)",
                    (trade_date, equity_now, now_iso),
                )
                return Baseline(trade_date, equity_now, False, None)
            return Baseline(
                row["trade_date"], row["baseline_equity"],
                bool(row["halted"]), row["halt_reason"],
            )

    def mark_halted(self, trade_date: str, reason: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE day_baseline SET halted=1, halt_reason=? WHERE trade_date=?",
                (reason, trade_date),
            )

    def is_halted(self, trade_date: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT halted FROM day_baseline WHERE trade_date=?", (trade_date,)
            ).fetchone()
            return bool(row["halted"]) if row else False

    def get_baseline(self, trade_date: str) -> Optional[Baseline]:
        """Read-only baseline lookup (None if the day was never opened).

        Unlike ``get_or_create_baseline`` this never writes — safe for the
        read-only digest path, which must not mutate trading state.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM day_baseline WHERE trade_date=?", (trade_date,)
            ).fetchone()
            if row is None:
                return None
            return Baseline(
                row["trade_date"], row["baseline_equity"],
                bool(row["halted"]), row["halt_reason"],
            )

    # --- per-ticker dedup -----------------------------------------------------

    def already_acted(self, trade_date: str, ticker: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM ticker_action WHERE trade_date=? AND ticker=?",
                (trade_date, ticker),
            ).fetchone()
            return row is not None

    def record_action(
        self, trade_date: str, ticker: str, *, signal: str, intent: str,
        status: str, detail: str, now_iso: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO ticker_action "
                "(trade_date, ticker, signal, intent, status, detail, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (trade_date, ticker, signal, intent, status, detail, now_iso),
            )

    def clear_day(self, trade_date: str) -> int:
        """Delete a day's ticker_action rows (used to switch dry_run->live on
        a day that already has dry-run rows). Returns rows deleted."""
        with self._conn() as c:
            cur = c.execute("DELETE FROM ticker_action WHERE trade_date=?", (trade_date,))
            return cur.rowcount

    def day_buys_total(self, trade_date: str) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(dollar_amount),0) AS t FROM orders "
                "WHERE trade_date=? AND side='buy'",
                (trade_date,),
            ).fetchone()
            return float(row["t"] or 0.0)

    # --- read-only day rollups (for the digest; never mutate state) -----------

    def day_actions(self, trade_date: str) -> list:
        """All committed ticker_action rows for the day, ordered by ticker."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM ticker_action WHERE trade_date=? ORDER BY ticker",
                (trade_date,),
            ).fetchall()]

    def day_orders(self, trade_date: str) -> list:
        """All order rows for the day (finalized or not), ordered by submit time."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE trade_date=? ORDER BY submitted_at",
                (trade_date,),
            ).fetchall()]

    # --- decision memory (ground of record for the scorecard) ----------------

    def record_decision(
        self, *, trade_date: str, ticker: str, decided_at: str,
        signal: Optional[str], intent: Optional[str],
        position_pct: Optional[float] = None, entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None, next_review_hours: Optional[float] = None,
        decision_price: Optional[float] = None, rationale: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> int:
        """Persist one analysis decision; returns its id (the outcome FK)."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO decisions (trade_date, ticker, run_id, decided_at, signal, "
                "intent, position_pct, entry_price, stop_loss, next_review_hours, "
                "decision_price, rationale) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_date, ticker, run_id, decided_at, signal, intent, position_pct,
                 entry_price, stop_loss, next_review_hours, decision_price, rationale),
            )
            rid = cur.lastrowid
            if rid is None:  # never happens after a successful INSERT, but be explicit
                raise RuntimeError("decisions INSERT did not return a row id")
            return int(rid)

    def get_decision(self, decision_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
            return dict(row) if row else None

    def pending_outcome_decisions(self, resolve_on_or_before_date: str) -> list:
        """Decisions with no outcome yet whose trade_date is old enough to score.

        Date (not timestamp) comparison so it's robust across DST. The orchestrator
        fetches quotes/positions for these and feeds them to ``reflect``.
        """
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT d.* FROM decisions d LEFT JOIN outcomes o ON o.decision_id = d.id "
                "WHERE o.decision_id IS NULL AND d.trade_date <= ? ORDER BY d.decided_at",
                (resolve_on_or_before_date,),
            ).fetchall()]

    def record_outcome(
        self, decision_id: int, *, resolved_at: str, holding_days: Optional[int] = None,
        directional_return: Optional[float] = None, benchmark_return: Optional[float] = None,
        alpha: Optional[float] = None, realized_pnl: Optional[float] = None,
        unrealized_pnl: Optional[float] = None, scored_against: str = "directional",
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO outcomes (decision_id, resolved_at, holding_days, "
                "directional_return, benchmark_return, alpha, realized_pnl, unrealized_pnl, "
                "scored_against) VALUES (?,?,?,?,?,?,?,?,?)",
                (decision_id, resolved_at, holding_days, directional_return, benchmark_return,
                 alpha, realized_pnl, unrealized_pnl, scored_against),
            )

    def decisions_with_outcomes(self, ticker: str, limit: int = 8) -> list:
        """Recent decisions for a ticker joined to their outcomes, newest first."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT d.*, o.directional_return, o.alpha, o.holding_days, o.realized_pnl, "
                "o.unrealized_pnl, o.scored_against FROM decisions d "
                "LEFT JOIN outcomes o ON o.decision_id = d.id "
                "WHERE d.ticker = ? ORDER BY d.decided_at DESC, d.id DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()]

    # --- intraday multi-run: action events + per-ticker cadence schedule ------
    # A "completed trade" (what cooldown / action-cap / on-change gate on) is an
    # event with intent buy|sell that actually went through or was simulated:
    # status placed|dry_run. Blocked/error attempts are logged but don't count.

    _TRADE_FILTER = "intent IN ('buy','sell') AND status IN ('placed','dry_run')"

    def record_event(
        self, *, trade_date: str, ticker: str, ts: str, signal: Optional[str],
        intent: Optional[str], status: Optional[str], detail: Optional[str] = None,
        dollar_amount: Optional[float] = None, run_id: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO actions (trade_date, ticker, run_id, ts, signal, intent, "
                "status, detail, dollar_amount) VALUES (?,?,?,?,?,?,?,?,?)",
                (trade_date, ticker, run_id, ts, signal, intent, status, detail, dollar_amount),
            )

    def last_trade_action(self, trade_date: str, ticker: str) -> Optional[dict]:
        """Most recent COMPLETED trade event for the ticker today (cooldown/on-change)."""
        with self._conn() as c:
            row = c.execute(
                f"SELECT * FROM actions WHERE trade_date=? AND ticker=? AND {self._TRADE_FILTER} "
                "ORDER BY ts DESC, id DESC LIMIT 1",
                (trade_date, ticker),
            ).fetchone()
            return dict(row) if row else None

    def trade_actions_today(self, trade_date: str, ticker: str) -> int:
        """Count of COMPLETED trades for the ticker today (the action cap)."""
        with self._conn() as c:
            row = c.execute(
                f"SELECT COUNT(*) AS n FROM actions WHERE trade_date=? AND ticker=? "
                f"AND {self._TRADE_FILTER}",
                (trade_date, ticker),
            ).fetchone()
            return int(row["n"] or 0)

    def get_schedule(self, trade_date: str, ticker: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ticker_schedule WHERE trade_date=? AND ticker=?",
                (trade_date, ticker),
            ).fetchone()
            return dict(row) if row else None

    def bump_analysis(self, trade_date: str, ticker: str, *, next_due_ts: Optional[str]) -> int:
        """Record that the ticker was analyzed: increment analyses_today, set next due.

        Returns the new analyses_today. Upsert keyed by (trade_date, ticker).
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO ticker_schedule (trade_date, ticker, next_due_ts, analyses_today) "
                "VALUES (?,?,?,1) ON CONFLICT(trade_date, ticker) DO UPDATE SET "
                "next_due_ts=excluded.next_due_ts, analyses_today=analyses_today+1",
                (trade_date, ticker, next_due_ts),
            )
            row = c.execute(
                "SELECT analyses_today FROM ticker_schedule WHERE trade_date=? AND ticker=?",
                (trade_date, ticker),
            ).fetchone()
            return int(row["analyses_today"]) if row else 0

    # --- order idempotency ----------------------------------------------------

    def new_ref_id(self) -> str:
        return str(uuid.uuid4())

    def reserve_order(
        self, ref_id: str, trade_date: str, ticker: str, *, side: str, type: str,
        dollar_amount: Optional[float], quantity: Optional[float], now_iso: str,
        order_kind: str = "entry", limit_price: Optional[float] = None,
        stop_price: Optional[float] = None, parent_ref_id: Optional[str] = None,
        state: str = "reserved",
    ) -> None:
        """Persist the order row BEFORE calling the broker (crash safety).

        Phase 5 fields (order_kind/limit_price/stop_price/parent_ref_id/state)
        default to a plain reserved market entry, so existing callers are unchanged.
        """
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO orders "
                "(ref_id, trade_date, ticker, side, type, dollar_amount, quantity, "
                "submitted_at, order_kind, limit_price, stop_price, parent_ref_id, state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref_id, trade_date, ticker, side, type, dollar_amount, quantity, now_iso,
                 order_kind, limit_price, stop_price, parent_ref_id, state),
            )

    def get_order(self, ref_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE ref_id=?", (ref_id,)).fetchone()
            return dict(row) if row else None

    def set_order_state(self, ref_id: str, state: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE orders SET state=? WHERE ref_id=?", (state, ref_id))

    def open_protective_stops(self, ticker: str) -> list:
        """Resting (GTC) protective stops for a ticker — must be cancelled before a
        sell to avoid an orphaned/oversized stop. Not date-scoped (stops are GTC)."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE ticker=? AND order_kind='protective_stop' "
                "AND state='stop_placed'",
                (ticker,),
            ).fetchall()]

    def finalize_order(self, ref_id: str, broker_order_id: Optional[str], result_json: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE orders SET broker_order_id=?, result_json=?, finalized=1 WHERE ref_id=?",
                (broker_order_id, result_json, ref_id),
            )

    def unfinalized_orders(self, trade_date: str):
        """Orders reserved but never finalized — reconcile these on restart."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE trade_date=? AND finalized=0", (trade_date,)
            ).fetchall()]

    # --- email-digest dedup ---------------------------------------------------
    # Email is best-effort: we mark a digest as sent AFTER the orchestrator
    # confirms delivery, so a crash in the send/commit gap re-sends (at most one
    # duplicate) rather than silently losing the digest. ``content_hash`` is the
    # dedup key: the same day's repeat (no-op) wakes produce the same hash and
    # never re-send; a genuine change (e.g. a halt fires) produces a new hash,
    # which REPLACEs the stored one so it sends exactly once more.

    def last_notified_hash(self, trade_date: str, kind: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT content_hash FROM notifications WHERE trade_date=? AND kind=?",
                (trade_date, kind),
            ).fetchone()
            return row["content_hash"] if row else None

    def mark_notified(self, trade_date: str, kind: str, content_hash: str,
                      recipients: str, now_iso: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO notifications "
                "(trade_date, kind, content_hash, recipients, sent_at) "
                "VALUES (?,?,?,?,?)",
                (trade_date, kind, content_hash, recipients, now_iso),
            )
