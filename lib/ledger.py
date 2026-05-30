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
    finalized       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notifications (
    trade_date   TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- digest|halt|auth_error
    content_hash TEXT NOT NULL,
    recipients   TEXT,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (trade_date, kind)
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

    # --- order idempotency ----------------------------------------------------

    def new_ref_id(self) -> str:
        return str(uuid.uuid4())

    def reserve_order(
        self, ref_id: str, trade_date: str, ticker: str, *, side: str, type: str,
        dollar_amount: Optional[float], quantity: Optional[float], now_iso: str,
    ) -> None:
        """Persist the order row BEFORE calling the broker (crash safety)."""
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO orders "
                "(ref_id, trade_date, ticker, side, type, dollar_amount, quantity, submitted_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ref_id, trade_date, ticker, side, type, dollar_amount, quantity, now_iso),
            )

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
