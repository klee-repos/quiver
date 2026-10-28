"""SQLite-backed state of record: orders, the P&L baseline and halt, the book, and the
desk tables (``lib/desk/store.py``).

The guarantees the desk relies on:
1. ``orders`` (PK ref_id UUID) is reserved when a proposal is built, BEFORE any broker
   call. A crash mid-place is recoverable, and a retry reuses the same ref_id
   (Robinhood dedups by it). Never mint a second ref_id for an order that might be sent.
2. ``day_baseline.halted`` stops every execution for the day once the daily-loss check
   fires. A confirmed ``resume_trading`` proposal lifts it (``clear_halt``).
3. ``Ledger.transaction()`` gives one connection with ``BEGIN IMMEDIATE``, so a tap claims
   a proposal, moves its legs and queues its job in one commit.

No table is ever dropped. Retired tables stay in the file and nothing reads them.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
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
    -- Desk leg states: proposed|approved|checked|placing|placed|filled|partially_filled|
    -- cancelled|broker_rejected|expired|requoted|skipped|outcome_unknown (older rows keep
    -- the retired values reserved|partial|unfilled|stop_placed|triggered).
    state           TEXT DEFAULT 'reserved',
    -- The REAL fill, read back from the broker by broker_order_id. `result_json` is
    -- assembled by the orchestrator at PLACE time, so it is inconsistent (15 distinct
    -- key shapes over 159 orders) and wrong as a price: BOT 6a79ec24 stored 27.59
    -- against an actual average_price of 27.9499. Python parses the broker answer here.
    -- `fill_quantity` is the ONLY place a dollar-based market buy's share count exists.
    fill_avg_price  REAL,
    fill_quantity   REAL,
    fill_fees       REAL,
    fill_state      TEXT,                        -- the BROKER's state, never our lifecycle `state`
    fill_captured_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    trade_date   TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- digest|halt|auth_error|error
    stage        TEXT NOT NULL DEFAULT '',  -- alert sub-type (broker_auth|plan|commit|...); '' for digest
    content_hash TEXT NOT NULL,
    recipients   TEXT,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (trade_date, kind, stage)
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
    rationale         TEXT,
    -- Consistency layer (back-filled on old dbs via _migrate_decisions):
    --   basis        = the declared strategy/thesis tag the call rests on
    --   plan_trigger = a Python-verified reason a reversal executes the recorded
    --                  plan (stop_hit|loss_catalyst|target_hit|review_due); NULL otherwise
    --   target_price = the model-seeded take-profit target (for plan-trigger checks)
    --   proof_json   = the auditable proof bundle assembled at plan time
    basis             TEXT,
    plan_trigger      TEXT,
    target_price      REAL,
    proof_json        TEXT,
    -- Conviction layer (back-filled on old dbs via _migrate_decisions):
    --   conviction   = the model's binding 0-100 conviction the allocator sizes on
    --   uncertainty  = the model's self-reported 0-100 uncertainty (damps sizing)
    --   data_quality = JSON of per-decision data-quality flags (bars_n, sources, ...)
    conviction        REAL,
    uncertainty       REAL,
    data_quality      TEXT
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
    scored_against     TEXT,                    -- 'directional' | 'both'
    adherence          TEXT                     -- F7/P7: 'honored'|'stop_violated'|'target_hit'|'na'
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
-- ===========================================================================
-- Strategy layer (the 15%/12mo macro book). strategy.yaml is the committed
-- SOURCE; `tick.py strategy-set` writes these tables; `construct`/`goal-track`
-- read them at runtime. These hold the book + goal + macro regime as state of
-- record — NO trading limits, NO broker. Brand-new tables auto-create via the
-- CREATE TABLE IF NOT EXISTS path (no _migrate_* helper needed — that's only
-- for new COLUMNS on existing tables).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS strategy_goal (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'active',   -- active|superseded
    target_return_pct    REAL,
    horizon_months       INTEGER,
    benchmark            TEXT,
    benchmark_annual_pct REAL,
    constraint_note      TEXT,
    macro_thesis_version TEXT,
    macro_thesis_json    TEXT,
    active_book          TEXT DEFAULT 'core_55_45',
    as_of                TEXT,
    start_date           TEXT,
    start_equity         REAL
);
CREATE TABLE IF NOT EXISTS target_portfolio (
    goal_id       INTEGER NOT NULL,
    sleeve        TEXT,
    ticker        TEXT NOT NULL,
    target_weight REAL NOT NULL,
    band          REAL DEFAULT 5.0,
    status        TEXT NOT NULL DEFAULT 'active',   -- active|exiting|removed
    book          TEXT DEFAULT 'core_55_45',
    quotable      INTEGER DEFAULT 1,
    proxy_ticker  TEXT,
    updated_at    TEXT,
    PRIMARY KEY (goal_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_target_portfolio_active ON target_portfolio(goal_id, status);
CREATE TABLE IF NOT EXISTS goal_tracking (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id                INTEGER NOT NULL,
    trade_date             TEXT,
    captured_at            TEXT,
    portfolio_value        REAL,
    glidepath_target_value REAL,
    cumulative_return_pct  REAL,
    ahead_behind_pct       REAL,
    alpha_vs_benchmark_pct REAL,
    active_book            TEXT,
    regime                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_goal_tracking_goal ON goal_tracking(goal_id, trade_date);
-- External cash flows: deposits (amount > 0) / withdrawals (amount < 0). Append-only.
-- Read ONLY by the goal/digest path (deposit-adjusted, time-weighted return) so external
-- capital is not miscounted as investment RETURN — NEVER by the trading path, sizing, or the
-- daily-loss halt. Populated out-of-band via `tick.py flow-record` (an ops/observability tool).
CREATE TABLE IF NOT EXISTS cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT NOT NULL,
    amount      REAL NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cash_flows_date ON cash_flows(trade_date);
CREATE TABLE IF NOT EXISTS thesis_state (
    goal_id         INTEGER PRIMARY KEY,
    as_of           TEXT,
    regime          TEXT NOT NULL DEFAULT 'neutral',     -- neutral|deploy|standdown (the EFFECTIVE, confirmed regime)
    active_book     TEXT DEFAULT 'core_55_45',           -- core_55_45|dial_up_63_37|derisked_cash
    last_trigger    TEXT,
    last_macro_json TEXT,
    updated_at      TEXT,
    -- Regime confirmation/hysteresis state (back-filled via _migrate_thesis_state):
    --   pending_regime = a different regime currently accumulating confirmations
    --   confirm_count  = consecutive readings seen for pending_regime
    --   pending_since  = when pending_regime first appeared
    --   regime_since   = when the EFFECTIVE regime last changed (for min-dwell)
    pending_regime  TEXT,
    pending_since   TEXT,
    confirm_count   INTEGER NOT NULL DEFAULT 0,
    regime_since    TEXT
);
CREATE TABLE IF NOT EXISTS universe_change_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id      INTEGER NOT NULL,
    proposed_at  TEXT,
    kind         TEXT,                                   -- add|remove|derisk|book_switch|flag
    ticker       TEXT,
    sleeve       TEXT,
    from_book    TEXT,
    to_book      TEXT,
    target_weight REAL,
    tier         TEXT,                                   -- soft|derisk|universe
    content_hash TEXT NOT NULL,                           -- (kind,ticker,tier): OPEN-row dedup
    change_key   TEXT,                                    -- (kind,ticker): cross-tier pooled reads
    reason       TEXT NOT NULL,
    goal_gap_pct REAL,
    status       TEXT NOT NULL DEFAULT 'proposed',       -- proposed|approved|applied|rejected|expired
    decided_at   TEXT,
    decided_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_universe_change_status ON universe_change_log(goal_id, status, proposed_at);
-- Dedup OPEN proposals: at most one 'proposed' row per (goal_id, content_hash),
-- so re-proposing the same change every tick is an INSERT OR IGNORE no-op.
CREATE UNIQUE INDEX IF NOT EXISTS idx_universe_open ON universe_change_log(goal_id, content_hash) WHERE status='proposed';
-- Append-only audit trail for EVERY strategic change (regime, book, weight, status,
-- basis). The thesis_state/target_portfolio tables are latest-snapshot UPSERTs; this
-- is the history that makes a flip-flop detectable after the fact and gives each
-- strategic change a proof. Written ONLY on a real diff (the tick.py glue compares
-- prior vs new). Observability — never gates a tick.
CREATE TABLE IF NOT EXISTS strategy_change_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     INTEGER,
    changed_at  TEXT NOT NULL,
    change_type TEXT NOT NULL,               -- regime|book|weight|status|basis|measurement
    ticker      TEXT,                        -- NULL for goal-level (regime|book|measurement)
    from_value  TEXT,
    to_value    TEXT,
    trigger     TEXT,                                     -- what caused it (macro_reading, strategy-set, ...)
    reason      TEXT,
    proof_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_strategy_change_goal ON strategy_change_log(goal_id, changed_at);
-- The broker's OWN average cost per held name, snapshotted by the tick glue from
-- the STEP 2 positions payload. The ledger cannot DERIVE this: a naive average of
-- our buy fills ignores sells and drifts (SPCX by 7.7%). Recording the broker
-- figure keeps lib/memory on LEDGER data while still telling the brain the truth.
CREATE TABLE IF NOT EXISTS position_basis (
    ticker           TEXT PRIMARY KEY,
    avg_buy_price    REAL NOT NULL,
    quantity         REAL,
    as_of            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),      -- single-row mutex
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL
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
        # Wait up to 30s for a busy DB instead of erroring immediately, so two overlapping
        # ticks (or the supervisor + a manual run) serialize cleanly rather than one crashing.
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def connect(self):
        """A connection that commits on a clean exit (the same as every method uses)."""
        return self._conn()

    @contextmanager
    def transaction(self):
        """One connection inside ``BEGIN IMMEDIATE``. Commit on a clean exit, roll back
        on an exception. The tap (plan §10.5) claims, moves the legs and queues the job
        in one of these, so the three writes commit together or not at all."""
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _use(self, conn):
        """Yield ``conn`` when the caller passes one (its transaction owns the commit),
        else a fresh connection that commits on exit."""
        if conn is not None:
            yield conn
            return
        with self._conn() as c:
            yield c

    def ensure_schema(self) -> None:
        from lib.desk import store
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate_orders(c)
            self._migrate_notifications(c)
            self._migrate_decisions(c)
            self._migrate_outcomes(c)
            self._migrate_target_portfolio(c)
            store.ensure_desk_schema(c)

    @staticmethod
    def _migrate_decisions(c) -> None:
        """Back-fill the consistency-layer columns on a decisions table created
        earlier. PRAGMA-diff + ADD COLUMN, same idempotent pattern as
        _migrate_orders (fresh dbs get them from _SCHEMA)."""
        existing = {row[1] for row in c.execute("PRAGMA table_info(decisions)").fetchall()}
        additions = {
            "basis": "TEXT",
            "plan_trigger": "TEXT",
            "target_price": "REAL",
            "proof_json": "TEXT",
            "conviction": "REAL",
            "uncertainty": "REAL",
            "data_quality": "TEXT",
        }
        for col, decl in additions.items():
            if col not in existing:
                c.execute(f"ALTER TABLE decisions ADD COLUMN {col} {decl}")

    @staticmethod
    def _migrate_outcomes(c) -> None:
        """Back-fill the plan-adherence column (F7/P7) on an outcomes table created earlier.
        PRAGMA-diff + ADD COLUMN, idempotent (same pattern as _migrate_decisions); old rows
        read NULL. Runs after _SCHEMA, so it covers fresh dbs too."""
        existing = {row[1] for row in c.execute("PRAGMA table_info(outcomes)").fetchall()}
        if not existing:
            return
        if "adherence" not in existing:
            c.execute("ALTER TABLE outcomes ADD COLUMN adherence TEXT")

    @staticmethod
    def _migrate_orders(c) -> None:
        """Back-fill the lifecycle and desk-leg columns on an orders table created earlier.

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
            "fill_avg_price": "REAL",
            "fill_quantity": "REAL",
            "fill_fees": "REAL",
            "fill_state": "TEXT",
            "fill_captured_at": "TEXT",
            # Desk legs (plan §6.1): the proposal that reserved the leg and the checks
            # that gate its place call.
            "proposal_id": "INTEGER",
            "leg_seq": "INTEGER",
            "quote_at_proposal": "REAL",
            "clamped_from": "REAL",
            "check_quote": "REAL",
            "checked_at": "TEXT",
            "reconcile_attempts": "INTEGER NOT NULL DEFAULT 0",
            "skip_reason": "TEXT",
            "review_ok": "INTEGER NOT NULL DEFAULT 0",
            "review_json": "TEXT",
            "reviewed_at": "TEXT",
        }
        for col, decl in additions.items():
            if col not in existing:
                c.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")

    @staticmethod
    def _migrate_target_portfolio(c) -> None:
        """Add ``entry_price_max`` (NULL means no entry condition, plan §6.1)."""
        existing = {row[1] for row in c.execute("PRAGMA table_info(target_portfolio)").fetchall()}
        if existing and "entry_price_max" not in existing:
            c.execute("ALTER TABLE target_portfolio ADD COLUMN entry_price_max REAL")

    @staticmethod
    def _migrate_notifications(c) -> None:
        """Widen the notifications PK from (trade_date, kind) to
        (trade_date, kind, stage) so distinct alert stages (e.g. a plan-error and a
        prune-hiccup, both kind='error') get independent dedup rows instead of
        clobbering one shared row.

        SQLite can't ALTER a primary key in place, so an old (stage-less) table is
        rebuilt: copy every existing row with stage='' (which is exactly the digest
        grain). Idempotent — a no-op once the `stage` column exists (fresh dbs get the
        new schema straight from _SCHEMA)."""
        cols = {row[1] for row in c.execute("PRAGMA table_info(notifications)").fetchall()}
        if not cols or "stage" in cols:
            return
        c.executescript(
            """
            ALTER TABLE notifications RENAME TO notifications_old;
            CREATE TABLE notifications (
                trade_date   TEXT NOT NULL,
                kind         TEXT NOT NULL,
                stage        TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                recipients   TEXT,
                sent_at      TEXT NOT NULL,
                PRIMARY KEY (trade_date, kind, stage)
            );
            INSERT INTO notifications
                (trade_date, kind, stage, content_hash, recipients, sent_at)
                SELECT trade_date, kind, '', content_hash, recipients, sent_at
                FROM notifications_old;
            DROP TABLE notifications_old;
            """
        )

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

    def mark_halted(self, trade_date: str, reason: str, conn=None) -> None:
        with self._use(conn) as c:
            c.execute(
                "UPDATE day_baseline SET halted=1, halt_reason=? WHERE trade_date=?",
                (reason, trade_date),
            )

    def clear_halt(self, trade_date: str, note: str, conn=None) -> int:
        """Lift today's daily-loss halt (a confirmed ``resume_trading`` proposal). Appends
        ``note`` to ``halt_reason``. Returns the rows changed (0 when the day is not halted).
        The KILL file is never touched here."""
        with self._use(conn) as c:
            cur = c.execute(
                "UPDATE day_baseline SET halted=0, halt_reason=COALESCE(halt_reason, '') || ? "
                "WHERE trade_date=? AND halted=1", ("; " + note, trade_date))
            return int(cur.rowcount or 0)

    def is_halted(self, trade_date: str, conn=None) -> bool:
        with self._use(conn) as c:
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

    # --- decision memory (ground of record for the scorecard) ----------------

    def record_decision(
        self, *, trade_date: str, ticker: str, decided_at: str,
        signal: Optional[str], intent: Optional[str],
        position_pct: Optional[float] = None, entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None, next_review_hours: Optional[float] = None,
        decision_price: Optional[float] = None, rationale: Optional[str] = None,
        run_id: Optional[str] = None, basis: Optional[str] = None,
        plan_trigger: Optional[str] = None, target_price: Optional[float] = None,
        proof_json: Optional[str] = None, conviction: Optional[float] = None,
        uncertainty: Optional[float] = None, data_quality: Optional[str] = None,
    ) -> int:
        """Persist one analysis decision; returns its id (the outcome FK).

        ``basis``/``plan_trigger``/``target_price``/``proof_json`` are the
        consistency-layer fields (the declared strategy tag, the Python-verified
        plan-execution trigger, the take-profit seed, and the auditable proof bundle).
        ``conviction``/``uncertainty`` are the model's binding 0-100 sizing inputs (the
        allocation engine reads them); ``data_quality`` is a JSON of per-decision data
        flags. All optional — older callers and back-filled rows leave them NULL.
        """
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO decisions (trade_date, ticker, run_id, decided_at, signal, "
                "intent, position_pct, entry_price, stop_loss, next_review_hours, "
                "decision_price, rationale, basis, plan_trigger, target_price, proof_json, "
                "conviction, uncertainty, data_quality) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_date, ticker, run_id, decided_at, signal, intent, position_pct,
                 entry_price, stop_loss, next_review_hours, decision_price, rationale,
                 basis, plan_trigger, target_price, proof_json, conviction, uncertainty,
                 data_quality),
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
        adherence: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO outcomes (decision_id, resolved_at, holding_days, "
                "directional_return, benchmark_return, alpha, realized_pnl, unrealized_pnl, "
                "scored_against, adherence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (decision_id, resolved_at, holding_days, directional_return, benchmark_return,
                 alpha, realized_pnl, unrealized_pnl, scored_against, adherence),
            )

    # --- benchmark measurement (the `benchmark-backfill` seam) --------------------
    # Deliberately NARROW, and deliberately NOT record_outcome: that one is INSERT OR
    # REPLACE across all ten columns, so re-measuring the market leg through it would
    # blank resolved_at/holding_days/scored_against/adherence/realized_pnl -- the very
    # fields the scorecard grades on. These two touch only the two columns the
    # measurement owns, and read back the two nothing else SELECTs.

    def update_outcome_benchmark(self, decision_id: int, benchmark_return: Optional[float],
                                 alpha: Optional[float]) -> int:
        """Set (or clear) ONLY the market leg + its excess. Returns rows updated."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE outcomes SET benchmark_return=?, alpha=? WHERE decision_id=?",
                (benchmark_return, alpha, decision_id),
            )
            return int(cur.rowcount or 0)

    def outcomes_for_backfill(self) -> list:
        """Every resolved decision with the fields the benchmark measurement needs.

        Carries ``benchmark_return`` -- which no other query selects -- so a backfill
        can tell a fill from a correction, and so a re-run can prove it is idempotent.
        """
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT d.id AS decision_id, d.ticker, d.trade_date, o.resolved_at, "
                "o.directional_return, o.benchmark_return, o.alpha "
                "FROM decisions d JOIN outcomes o ON o.decision_id = d.id "
                "ORDER BY d.id ASC"
            ).fetchall()]

    def decisions_with_outcomes(self, ticker: str, limit: int = 8) -> list:
        """Recent decisions for a ticker joined to their outcomes, newest first."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT d.*, o.directional_return, o.alpha, o.holding_days, o.realized_pnl, "
                "o.unrealized_pnl, o.scored_against, o.adherence FROM decisions d "
                "LEFT JOIN outcomes o ON o.decision_id = d.id "
                "WHERE d.ticker = ? ORDER BY d.decided_at DESC, d.id DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()]

    def baseline_capture_times(self) -> dict:
        """{trade_date: captured_at} for the equity curve. Read-only; diagnostic path.

        Separate from baseline_equity_series so that curve's shape is untouched.
        Callers need the timestamp because the curve is NOT uniformly captured near
        the open -- live rows include one stamped on a Saturday and one 5.3 hours
        after the close, which a return series must exclude rather than average in.
        """
        with self._conn() as c:
            return {r["trade_date"]: r["captured_at"] for r in c.execute(
                "SELECT trade_date, captured_at FROM day_baseline ORDER BY trade_date ASC"
            ).fetchall()}

    def baseline_equity_series(self) -> list:
        """Daily baseline equity curve, date order. Used ONLY by the digest path
        (account-equity drawdown) — never by the analysis/memory path."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT trade_date, baseline_equity FROM day_baseline ORDER BY trade_date ASC"
            ).fetchall()]

    # --- order idempotency ----------------------------------------------------

    def new_ref_id(self) -> str:
        return str(uuid.uuid4())

    def reserve_order(
        self, ref_id: str, trade_date: str, ticker: str, *, side: str, type: str,
        dollar_amount: Optional[float], quantity: Optional[float], now_iso: str,
        order_kind: str = "entry", limit_price: Optional[float] = None,
        stop_price: Optional[float] = None, parent_ref_id: Optional[str] = None,
        state: str = "proposed", proposal_id: Optional[int] = None,
        leg_seq: Optional[int] = None, quote_at_proposal: Optional[float] = None,
        conn=None,
    ) -> None:
        """Persist the order row BEFORE any broker call (crash safety).

        A desk leg is reserved when its proposal is built, in state ``proposed``. Its
        ``submitted_at`` stays NULL: ``broker_guard`` writes it at the first allowed place
        call. Any other state stores ``now_iso`` as ``submitted_at``.
        """
        submitted_at = None if state == "proposed" else now_iso
        with self._use(conn) as c:
            c.execute(
                "INSERT OR IGNORE INTO orders "
                "(ref_id, trade_date, ticker, side, type, dollar_amount, quantity, "
                "submitted_at, order_kind, limit_price, stop_price, parent_ref_id, state, "
                "proposal_id, leg_seq, quote_at_proposal) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref_id, trade_date, ticker, side, type, dollar_amount, quantity, submitted_at,
                 order_kind, limit_price, stop_price, parent_ref_id, state,
                 proposal_id, leg_seq, quote_at_proposal),
            )

    def get_order(self, ref_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE ref_id=?", (ref_id,)).fetchone()
            return dict(row) if row else None

    def set_order_state(self, ref_id: str, state: str, conn=None) -> None:
        with self._use(conn) as c:
            c.execute("UPDATE orders SET state=? WHERE ref_id=?", (state, ref_id))

    def legs(self, proposal_id: int, conn=None) -> list:
        """The order rows (legs) of one proposal, in ``leg_seq`` order."""
        with self._use(conn) as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE proposal_id=? ORDER BY leg_seq, ref_id",
                (int(proposal_id),)).fetchall()]

    def finalize_order(self, ref_id: str, broker_order_id: Optional[str], result_json: str,
                       conn=None) -> None:
        with self._use(conn) as c:
            c.execute(
                "UPDATE orders SET broker_order_id=?, result_json=?, finalized=1 WHERE ref_id=?",
                (broker_order_id, result_json, ref_id),
            )

    # --- real fills, read back from the broker -------------------------------
    # A fill is retired on the broker's STATE, never on a price. A partially_filled
    # order carries a REAL partial average_price, so a price key retires the row
    # mid-fill and freezes the share count. Live proof: BSOL order
    # 6a75fa00-9d7d-40d3-84af-8620e0943cc2 has two executions summing 1.697570; a
    # capture after the first stores 1.000000, which is 59% of the truth.
    TERMINAL_FILL_STATES = ("filled", "cancelled", "rejected", "failed",
                            "voided", "not_found")

    def fills_pending(self, limit: int = 500):
        """Orders whose real fill is not captured yet, oldest first."""
        marks = ",".join("?" for _ in self.TERMINAL_FILL_STATES)
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE broker_order_id IS NOT NULL "
                f"AND (fill_state IS NULL OR fill_state NOT IN ({marks})) "
                "ORDER BY submitted_at LIMIT ?",
                (*self.TERMINAL_FILL_STATES, limit)).fetchall()]

    def record_fill(self, ref_id: str, *, avg_price, quantity, fees,
                    fill_state: str, now_iso: str) -> None:
        """Write the broker's answer for one order.

        Always writes ``fill_state`` and ``fill_captured_at``, even when the broker
        returned no numbers. A row with no state would be re-fetched forever.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE orders SET fill_avg_price=?, fill_quantity=?, fill_fees=?, "
                "fill_state=?, fill_captured_at=? WHERE ref_id=?",
                (avg_price, quantity, fees, str(fill_state or ""), now_iso, ref_id))

    # --- notification dedup ---------------------------------------------------
    # A message is marked sent AFTER Telegram confirms delivery, so a crash in the
    # send/commit gap re-sends one duplicate instead of losing the message.
    # ``content_hash`` is the dedup key per (trade_date, kind, stage).

    def last_notified_hash(self, trade_date: str, kind: str,
                           stage: str = "") -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT content_hash FROM notifications "
                "WHERE trade_date=? AND kind=? AND stage=?",
                (trade_date, kind, stage),
            ).fetchone()
            return row["content_hash"] if row else None

    def mark_notified(self, trade_date: str, kind: str, content_hash: str,
                      recipients: str, now_iso: str, stage: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO notifications "
                "(trade_date, kind, stage, content_hash, recipients, sent_at) "
                "VALUES (?,?,?,?,?,?)",
                (trade_date, kind, stage, content_hash, recipients, now_iso),
            )

    # --- strategy layer: goal, target book, goal tracking ----------------------
    # The goal and the target book of record. `desk-seed` writes them once from
    # strategy.yaml; after that only a confirmed change proposal moves them.

    def set_strategy_goal_with_holdings(self, *, goal: dict, holdings: list) -> int:
        """ATOMIC strategy-set: supersede prior goals, insert the new active goal,
        AND upsert all its target holdings in ONE transaction (a single commit).

        This is the SAFE production path. The piecewise set_strategy_goal +
        upsert_target_holding each commit separately, so a crash/kill between them
        leaves an active goal with a PARTIAL or empty target book — and with
        reconciliation on, `construct` would then flag every held name not in that
        truncated book as an orphan and SELL the whole portfolio. Doing it in one
        transaction means a partial write commits nothing and is never observable.

        `goal` carries the set_strategy_goal kwargs; `holdings` is a list of dicts
        with keys sleeve/ticker/target_weight/band/status/book/quotable/proxy_ticker.
        Returns the new goal id.
        """
        with self._conn() as c:
            c.execute("UPDATE strategy_goal SET status='superseded' WHERE status='active'")
            cur = c.execute(
                "INSERT INTO strategy_goal (created_at, status, target_return_pct, "
                "horizon_months, benchmark, benchmark_annual_pct, constraint_note, "
                "macro_thesis_version, macro_thesis_json, active_book, as_of, "
                "start_date, start_equity) VALUES (?, 'active', ?,?,?,?,?,?,?,?,?,?,?)",
                (goal["created_at"], goal["target_return_pct"], goal["horizon_months"],
                 goal["benchmark"], goal["benchmark_annual_pct"], goal["constraint_note"],
                 goal["macro_thesis_version"], goal["macro_thesis_json"], goal["active_book"],
                 goal["as_of"], goal["start_date"], goal["start_equity"]),
            )
            rid = cur.lastrowid
            if rid is None:
                raise RuntimeError("strategy_goal INSERT did not return a row id")
            gid = int(rid)
            for h in holdings:
                c.execute(
                    "INSERT INTO target_portfolio (goal_id, sleeve, ticker, target_weight, "
                    "band, status, book, quotable, proxy_ticker, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(goal_id, ticker) DO UPDATE SET sleeve=excluded.sleeve, "
                    "target_weight=excluded.target_weight, band=excluded.band, "
                    "status=excluded.status, book=excluded.book, quotable=excluded.quotable, "
                    "proxy_ticker=excluded.proxy_ticker, updated_at=excluded.updated_at",
                    (gid, h["sleeve"], h["ticker"], h["target_weight"], h["band"],
                     h["status"], h["book"], 1 if h["quotable"] else 0,
                     h.get("proxy_ticker"), h["updated_at"]),
                )
            return gid

    def get_active_goal(self) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM strategy_goal WHERE status='active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def upsert_target_holding(self, *, goal_id: int, sleeve, ticker, target_weight, band,
                              status, book, quotable, proxy_ticker, updated_at,
                              entry_price_max=None, conn=None) -> None:
        """Latest-snapshot per (goal_id, ticker). Idempotent re-runs via ON CONFLICT
        DO UPDATE. ``entry_price_max`` NULL means the name has no entry condition."""
        with self._use(conn) as c:
            c.execute(
                "INSERT INTO target_portfolio (goal_id, sleeve, ticker, target_weight, "
                "band, status, book, quotable, proxy_ticker, updated_at, entry_price_max) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(goal_id, ticker) DO UPDATE SET sleeve=excluded.sleeve, "
                "target_weight=excluded.target_weight, band=excluded.band, "
                "status=excluded.status, book=excluded.book, quotable=excluded.quotable, "
                "proxy_ticker=excluded.proxy_ticker, updated_at=excluded.updated_at, "
                "entry_price_max=excluded.entry_price_max",
                (goal_id, sleeve, ticker, target_weight, band, status, book,
                 1 if quotable else 0, proxy_ticker, updated_at, entry_price_max),
            )

    def set_holding_status(self, goal_id: int, ticker: str, status: str, updated_at: str,
                           conn=None) -> None:
        with self._use(conn) as c:
            c.execute(
                "UPDATE target_portfolio SET status=?, updated_at=? WHERE goal_id=? AND ticker=?",
                (status, updated_at, goal_id, ticker),
            )

    def active_target_portfolio(self, goal_id: int, statuses=("active",), conn=None) -> list:
        placeholders = ",".join("?" for _ in statuses)
        with self._use(conn) as c:
            return [dict(r) for r in c.execute(
                f"SELECT * FROM target_portfolio WHERE goal_id=? AND status IN ({placeholders}) "
                "ORDER BY ticker ASC",
                (goal_id, *statuses),
            ).fetchall()]

    def record_goal_snapshot(self, *, goal_id: int, trade_date, captured_at, portfolio_value,
                             glidepath_target_value, cumulative_return_pct, ahead_behind_pct,
                             alpha_vs_benchmark_pct, active_book, regime) -> None:
        """Append-only goal-progress snapshot (like the actions event log)."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO goal_tracking (goal_id, trade_date, captured_at, portfolio_value, "
                "glidepath_target_value, cumulative_return_pct, ahead_behind_pct, "
                "alpha_vs_benchmark_pct, active_book, regime) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (goal_id, trade_date, captured_at, portfolio_value, glidepath_target_value,
                 cumulative_return_pct, ahead_behind_pct, alpha_vs_benchmark_pct, active_book, regime),
            )

    # --- external cash flows (deposits/withdrawals) — goal/digest path ONLY ------
    # These let the goal-progress math deposit-adjust the return so external capital is
    # not counted as investment gain. Append-only; read ONLY by lib.goal / the digest —
    # NEVER by sizing, order placement, or the daily-loss halt.

    def record_cash_flow(self, *, trade_date: str, amount: float, note: Optional[str],
                         created_at: str) -> None:
        """Append one external cash flow (amount > 0 deposit, < 0 withdrawal)."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO cash_flows (trade_date, amount, note, created_at) VALUES (?,?,?,?)",
                (trade_date, float(amount), note, created_at),
            )

    def record_cash_flow_if_absent(self, *, trade_date: str, amount: float,
                                   note: Optional[str], created_at: str) -> bool:
        """Atomically append an AUTO-captured cash flow iff no auto row exists for that date.

        The auto-capture path (``tick.py`` goal-track) can run concurrently — a scheduled tick
        holding ``run_lock`` and a manual ``tick.py goal-track`` do not serialize — so a plain
        read-then-INSERT is a TOCTOU that could double-insert the same jump. This does the guard
        in ONE statement (``INSERT ... SELECT ... WHERE NOT EXISTS``), so SQLite's writer lock
        enforces at-most-one ``auto:%`` row per ``trade_date`` (the auto-vs-auto race always lands
        on the same jump date). Manual ``record_cash_flow`` rows are untouched. Returns True iff a
        row was inserted.
        """
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO cash_flows (trade_date, amount, note, created_at) "
                "SELECT ?,?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM cash_flows WHERE trade_date=? AND note LIKE 'auto:%')",
                (trade_date, float(amount), note, created_at, trade_date),
            )
            return cur.rowcount > 0

    def cash_flows(self) -> list:
        """All recorded external cash flows, date-ASC. Read-only; goal/digest path only."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT trade_date, amount, note FROM cash_flows ORDER BY trade_date ASC, id ASC"
            ).fetchall()]

    # --- strategy change log (append-only audit trail for strategic changes) ------
    # Every applied change proposal writes rows here (trigger='owner_confirm').

    def record_strategy_change(self, *, goal_id, changed_at, change_type, ticker=None,
                               from_value=None, to_value=None, trigger=None, reason=None,
                               proof_json=None, conn=None) -> int:
        with self._use(conn) as c:
            cur = c.execute(
                "INSERT INTO strategy_change_log (goal_id, changed_at, change_type, ticker, "
                "from_value, to_value, trigger, reason, proof_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (goal_id, changed_at, change_type,
                 None if ticker is None else str(ticker).upper(),
                 None if from_value is None else str(from_value),
                 None if to_value is None else str(to_value),
                 trigger, reason, proof_json),
            )
            return int(cur.lastrowid or 0)

    def retarget_weights(self, goal_id: int, weights: dict, changed_at: str,
                         changes: list, *, trigger: str = "conviction", bands=None,
                         conn=None) -> None:
        """ATOMIC re-target: the new target weights (and bands) and their `weight`
        change-log rows commit together or not at all.

        weights: {ticker: target_weight} for the rows that change (the cash row included,
        so the book still sums to ~100). bands: an optional {ticker: band} map.
        changes: [{ticker, from_value, to_value, reason, proof_json}], one per name that
        moved. ``set_weights`` passes ``trigger='owner_confirm'``.
        """
        bands = bands or {}
        with self._use(conn) as c:
            for tk, w in weights.items():
                c.execute(
                    "UPDATE target_portfolio SET target_weight=?, updated_at=? "
                    "WHERE goal_id=? AND ticker=?",
                    (float(w), changed_at, goal_id, str(tk).upper()))
            for tk, b in bands.items():
                c.execute(
                    "UPDATE target_portfolio SET band=?, updated_at=? WHERE goal_id=? AND ticker=?",
                    (float(b), changed_at, goal_id, str(tk).upper()))
            for ch in changes:
                c.execute(
                    "INSERT INTO strategy_change_log (goal_id, changed_at, change_type, ticker, "
                    "from_value, to_value, trigger, reason, proof_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (goal_id, changed_at, "weight", str(ch["ticker"]).upper(),
                     str(ch.get("from_value")), str(ch.get("to_value")), trigger,
                     ch.get("reason"), ch.get("proof_json")))

    def strategy_change_history(self, goal_id: int, *, limit: int = 50,
                                change_type: Optional[str] = None) -> list:
        """Strategic changes for a goal, NEWEST first (optionally one change_type)."""
        with self._conn() as c:
            if change_type:
                rows = c.execute(
                    "SELECT * FROM strategy_change_log WHERE goal_id=? AND change_type=? "
                    "ORDER BY changed_at DESC, id DESC LIMIT ?",
                    (goal_id, change_type, limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM strategy_change_log WHERE goal_id=? "
                    "ORDER BY changed_at DESC, id DESC LIMIT ?",
                    (goal_id, limit)).fetchall()
            return [dict(r) for r in rows]

    # --- named run locks (lib/runlock) -----------------------------------------

    def try_acquire_named_lock(self, name: str, holder: str, now_iso: str, *,
                               ttl_seconds: int, holder_dead=None) -> bool:
        """Take the ``run_locks`` row ``name`` for ``holder``. Returns True on acquire.

        The lock is free when no row exists, when ``holder_dead(row.holder)`` is True
        (another boot, or no such process), or when the row is older than the TTL.
        ATOMIC: the read-check-write runs inside ``BEGIN IMMEDIATE``, so two runners
        can never both see a free lock."""
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT holder, acquired_at FROM run_locks WHERE name=?",
                               (name,)).fetchone()
            if row is not None:
                try:
                    age = (_aware(now_iso) - _aware(row[1])).total_seconds()
                except (TypeError, ValueError):
                    age = ttl_seconds + 1
                dead = bool(holder_dead(row[0])) if holder_dead is not None else False
                if age < ttl_seconds and not dead:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute("UPDATE run_locks SET holder=?, acquired_at=? WHERE name=?",
                             (holder, now_iso, name))
            else:
                conn.execute("INSERT INTO run_locks (name, holder, acquired_at) VALUES (?, ?, ?)",
                             (name, holder, now_iso))
            conn.execute("COMMIT")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    def release_named_lock(self, name: str, holder: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM run_locks WHERE name=? AND holder=?", (name, holder))

    def named_lock(self, name: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM run_locks WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None


def _aware(value: str) -> datetime:
    """Parse a stored time; a naive value counts as UTC, so ET and UTC stamps compare."""
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

