"""The desk schema, the FTS indexes, the migrations, the named locks and retrieval (plan §6.1, §6.2, §18.9 G1).

Each case is a ``case_*`` function in ``CASES``. The table list, the column lists and the
CHECK values below are copied from the §6.1 SQL of the plan, not from ``lib/desk/store.py``,
so a drift between the code and the plan fails a case.
Run: .venv/bin/python tests/test_desk_store.py
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import FakeClock, Results, fx, fx_bytes, temp_root  # noqa: E402

from lib import runlock  # noqa: E402
from lib.desk import findings, sections, store  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

r = Results("desk-store")
T0 = "2026-08-05T14:00:00Z"

# §6.1: every table and its columns, in order (the 4 FTS tables list their indexed columns).
PLAN_TABLES = {
    "companies": ('ticker', 'name', 'kind', 'cik', 'series_id', 'sleeve', 'status', 'status_reason',
                  'added_at', 'updated_at'),
    "sec_ticker_map": ('ticker', 'cik', 'series_id', 'title', 'source', 'fetched_at'),
    "sec_overrides": ('ticker', 'cik', 'kind', 'note'),
    "filings": ('accession', 'cik', 'ticker', 'form', 'filed_date', 'accepted_at', 'report_date', 'items',
                'primary_doc', 'tier', 'seen_at', 'ingested_at', 'ingest_error'),
    "documents": ('id', 'kind', 'ticker', 'accession', 'section_key', 'title', 'url', 'published_at',
                  'fetched_at', 'text', 'sha256', 'char_len'),
    "documents_fts": ('title', 'text'),
    "xbrl_facts": ('cik', 'taxonomy', 'concept', 'unit', 'start_date', 'end_date', 'value', 'accession', 'fy',
                   'fp', 'form', 'filed', 'frame'),
    "metrics": ('ticker', 'as_of', 'source_accession', 'price', 'metrics_json'),
    "findings": ('id', 'ticker', 'kind', 'claim', 'quote', 'document_id', 'quote_start', 'created_by',
                 'event_id', 'created_at'),
    "findings_fts": ('claim', 'quote'),
    "notes": ('id', 'ticker', 'text', 'tags', 'created_by', 'created_at'),
    "notes_fts": ('text', 'tags'),
    "premise": ('version', 'text', 'goal_json', 'confirmed_at', 'proposal_id'),
    "theses": ('ticker', 'version', 'premise_link', 'summary', 'drivers_json', 'falsifiers_json',
               'valuation_json', 'fair_value_low', 'fair_value_base', 'fair_value_high', 'buy_below',
               'trim_above', 'evidence_json', 'confirmed_at', 'proposal_id'),
    "level_state": ('ticker', 'level', 'price', 'armed', 'last_quote', 'changed_at'),
    "news_items": ('url_hash', 'ticker', 'title', 'publisher', 'url', 'published_at', 'seen_at', 'material',
                   'category', 'reason', 'classify_attempts', 'classified_at', 'document_id'),
    "transcripts": ('ticker', 'quarter', 'document_id', 'fetched_at'),
    "api_budget": ('provider', 'day', 'used'),
    "etf_holdings": ('fund', 'as_of', 'source', 'holding_name', 'holding_ticker', 'cusip', 'weight_pct'),
    "events": ('id', 'dedup_key', 'ticker', 'type', 'created_by', 'detail_json', 'detected_at', 'status',
               'verdict', 'analysis', 'analyzed_at'),
    "jobs": ('id', 'kind', 'payload_json', 'status', 'requested_by', 'created_at', 'started_at', 'finished_at',
             'attempts', 'error'),
    "broker_jobs": ('id', 'kind', 'proposal_id', 'recovery', 'status', 'requested_by', 'created_at',
                    'started_at', 'finished_at', 'error'),
    "broker_snapshots": ('id', 'captured_at', 'equity', 'cash', 'buying_power', 'positions_json',
                         'quotes_json', 'approval_setting', 'raw_json'),
    "proposals": ('id', 'kind', 'origin', 'event_id', 'owner_message_id', 'parent_change_id', 'payload_json',
                  'payload_sha256', 'rationale', 'evidence_json', 'created_by', 'created_at', 'expires_at',
                  'state', 'nonce', 'tg_chat_id', 'tg_message_id', 'decided_at', 'decided_by',
                  'superseded_by', 'result_json'),
    "desk_settings": ('key', 'value_json', 'updated_at', 'proposal_id'),
    "chat_messages": ('id', 'chat_id', 'tg_message_id', 'role', 'source', 'text', 'reaction', 'reacted_at',
                      'created_at', 'session_id', 'delivered_to_session'),
    "chat_messages_fts": ('text',),
    "chat_sessions": ('chat_id', 'session_id', 'started_at', 'last_turn_at'),
    "bridge_state": ('key', 'value'),
    "run_locks": ('name', 'holder', 'acquired_at'),
}
PLAN_FTS = {"documents_fts": "documents", "findings_fts": "findings", "notes_fts": "notes",
            "chat_messages_fts": "chat_messages"}
PLAN_INDEXES = {"idx_filings_ticker": "filings", "idx_documents_ticker": "documents",
                "idx_proposals_state": "proposals", "idx_chat_tg": "chat_messages"}

# §6.1: every CHECK enum and its allowed values.
PLAN_ENUMS = {
    ("companies", "kind"): ('company', 'etf', 'cef', 'crypto_etf', 'cash'),
    ("companies", "status"): ('candidate', 'watch', 'holding', 'rejected', 'dropped'),
    ("sec_ticker_map", "source"): ('company_tickers', 'company_tickers_mf'),
    ("filings", "tier"): ('redecide', 'record', 'ignore', 'baseline'),
    ("documents", "kind"): ('filing_section', 'transcript', 'news', 'web'),
    ("findings", "kind"): ('market_size', 'growth', 'guidance', 'margin', 'segment', 'customer', 'competition',
                           'risk', 'management', 'capital', 'other'),
    ("findings", "created_by"): ('desk', 'analyst'),
    ("notes", "created_by"): ('desk', 'analyst'),
    ("level_state", "level"): ('buy_below', 'trim_above', 'entry'),
    ("etf_holdings", "source"): ('yfinance_top', 'nport'),
    ("events", "type"): ('filing', 'earnings', 'price_level', 'drift', 'news', 'lookthrough'),
    ("events", "created_by"): ('monitor', 'desk'),
    ("events", "status"): ('new', 'queued', 'analyzed', 'proposed', 'noted', 'failed'),
    ("events", "verdict"): ('intact', 'strengthened', 'weakened', 'broken', 'n/a'),
    ("jobs", "kind"): ('analyze_event', 'deep_dive', 'classify_news'),
    ("jobs", "status"): ('queued', 'running', 'done', 'failed'),
    ("broker_jobs", "kind"): ('snapshot', 'execute'),
    ("broker_jobs", "status"): ('queued', 'running', 'done', 'failed'),
    ("proposals", "kind"): ('trade', 'change'),
    ("proposals", "origin"): ('event', 'owner', 'change', 'drift', 'seed'),
    ("proposals", "created_by"): ('desk', 'analyst', 'monitor', 'system'),
    ("proposals", "state"): ('pending', 'approved', 'rejected', 'expired', 'superseded', 'withdrawn', 'executing',
                             'executed', 'partially_executed', 'failed', 'requoted', 'applied'),
    ("chat_messages", "role"): ('owner', 'desk'),
    ("chat_messages", "source"): ('chat', 'analyst', 'monitor', 'brief', 'proposal', 'execution', 'system'),
}

# One row of each CHECK table with every NOT NULL column filled. The check case inserts it
# inside a savepoint and rolls it back, so the row never stays in the ledger.
BASE_ROWS = {
    "companies": {"ticker": "TST", "kind": "company", "status": "candidate", "added_at": T0, "updated_at": T0},
    "sec_ticker_map": {"ticker": "TST", "cik": "0000000001", "source": "company_tickers", "fetched_at": T0},
    "filings": {"accession": "0000000001-26-000001", "cik": "0000000001", "ticker": "TST", "form": "10-Q",
                "filed_date": "2026-08-05", "tier": "record", "seen_at": T0},
    "documents": {"kind": "web", "title": "t", "fetched_at": T0, "text": "x", "sha256": "h", "char_len": 1},
    "findings": {"ticker": "TST", "kind": "other", "claim": "c", "quote": "q", "document_id": 1, "quote_start": 0,
                 "created_by": "desk", "created_at": T0},
    "notes": {"text": "n", "created_by": "desk", "created_at": T0},
    "level_state": {"ticker": "TST", "level": "entry", "price": 1.0, "armed": 1, "changed_at": T0},
    "etf_holdings": {"fund": "TSTF", "as_of": "2026-08-05", "source": "nport", "holding_name": "h", "weight_pct": 1.0},
    "events": {"dedup_key": "k", "ticker": "TST", "type": "filing", "created_by": "monitor", "detail_json": "{}",
               "detected_at": T0, "status": "new"},
    "jobs": {"kind": "deep_dive", "payload_json": "{}", "status": "queued", "requested_by": "t", "created_at": T0},
    "broker_jobs": {"kind": "snapshot", "status": "queued", "requested_by": "t", "created_at": T0},
    "proposals": {"kind": "change", "origin": "change", "payload_json": "{}", "payload_sha256": "h",
                  "rationale": "r", "evidence_json": "[]", "created_by": "desk", "created_at": T0,
                  "expires_at": T0, "state": "pending", "nonce": "n"},
    "chat_messages": {"chat_id": "1", "role": "owner", "source": "chat", "text": "t", "created_at": T0},
}

# §6.1 migrations: the 11 new orders columns with their declared types and defaults.
ORDERS_NEW = {"proposal_id": ("INTEGER", 0, None), "leg_seq": ("INTEGER", 0, None),
              "quote_at_proposal": ("REAL", 0, None), "clamped_from": ("REAL", 0, None),
              "check_quote": ("REAL", 0, None), "checked_at": ("TEXT", 0, None),
              "reconcile_attempts": ("INTEGER", 1, "0"), "skip_reason": ("TEXT", 0, None),
              "review_ok": ("INTEGER", 1, "0"), "review_json": ("TEXT", 0, None), "reviewed_at": ("TEXT", 0, None)}
ORDER_STATES = ("proposed", "approved", "checked", "placing", "placed", "filled", "partially_filled", "cancelled",
                "broker_rejected", "expired", "requoted", "skipped", "outcome_unknown")


def raw(path) -> sqlite3.Connection:
    """A plain connection in autocommit mode, so a case controls each savepoint."""
    c = sqlite3.connect(str(path), isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def master_sql(conn) -> list:
    return [tuple(x) for x in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()]


def table_names(conn) -> set:
    return {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def cols(conn, table) -> list:
    return [x[1] for x in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def try_insert(conn, table: str, row: dict):
    """Insert ``row`` in a savepoint and roll it back. Returns None or the error text."""
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute("SAVEPOINT chk")
    try:
        conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(row.values()))
        return None
    except sqlite3.IntegrityError as e:
        return str(e)
    finally:
        conn.execute("ROLLBACK TO chk")
        conn.execute("RELEASE chk")


def case_schema_all():
    with temp_root() as root:
        root.led()
        c = raw(root.db)
        names = table_names(c)
        r.eq("schema_all store.DESK_TABLES has 30 names", len(store.DESK_TABLES), 30)
        r.eq("schema_all store.DESK_TABLES is the plan list", set(store.DESK_TABLES), set(PLAN_TABLES))
        r.eq("schema_all the plan lists 30 tables", len(PLAN_TABLES), 30)
        r.eq("schema_all every plan table exists", sorted(set(PLAN_TABLES) - names), [])
        for t, want in PLAN_TABLES.items():
            r.eq(f"schema_all columns of {t}", tuple(cols(c, t)), want)
        for fts, content in PLAN_FTS.items():
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name=?", (fts,)).fetchone()[0]
            r.ok(f"schema_all {fts} is an external-content fts5 table of {content}",
                 "USING fts5" in sql and f"content='{content}'" in sql and "content_rowid='id'" in sql
                 and "porter unicode61" in sql, sql)
        idx = {x[0]: x[1] for x in c.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'")}
        for name, table in PLAN_INDEXES.items():
            r.eq(f"schema_all index {name}", idx.get(name), table)
        r.eq("schema_all idx_filings_ticker columns", [x[2] for x in c.execute("PRAGMA index_info(idx_filings_ticker)")],
             ["ticker", "filed_date"])
        r.eq("schema_all idx_documents_ticker columns",
             [x[2] for x in c.execute("PRAGMA index_info(idx_documents_ticker)")], ["ticker", "kind", "published_at"])
        r.eq("schema_all idx_proposals_state columns",
             [x[2] for x in c.execute("PRAGMA index_info(idx_proposals_state)")], ["state", "expires_at"])
        tg = c.execute("SELECT sql FROM sqlite_master WHERE name='idx_chat_tg'").fetchone()[0]
        r.ok("schema_all idx_chat_tg is a partial unique index",
             "UNIQUE" in tg.upper() and "WHERE tg_message_id IS NOT NULL" in tg, tg)
        r.ok("schema_all documents UNIQUE (kind, sha256)",
             any(set(x[2] for x in c.execute(f"PRAGMA index_info({i[1]})")) == {"kind", "sha256"} and i[2]
                 for i in c.execute("PRAGMA index_list(documents)").fetchall()))
        r.ok("schema_all run_locks: name is the key (one row per runner)",
             [x[1] for x in c.execute("PRAGMA table_info(run_locks)") if x[5]] == ["name"])
        r.ok("schema_all xbrl_facts start_date defaults to ''",
             [x[4] for x in c.execute("PRAGMA table_info(xbrl_facts)") if x[1] == "start_date"] == ["''"])
        c.close()


def case_schema_rerun():
    with temp_root() as root:
        root.led()
        c = raw(root.db)
        before = master_sql(c)
        try:
            store.ensure_desk_schema(c)
            store.ensure_desk_schema(c)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = repr(e)
        r.eq("schema_rerun ensure_desk_schema twice raises nothing", ok, True)
        c.close()
        try:
            Ledger(root.db)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = repr(e)
        r.eq("schema_rerun a second Ledger() raises nothing", ok, True)
        c = raw(root.db)
        r.eq("schema_rerun sqlite_master SQL is byte-identical", master_sql(c), before)
        c.close()


def case_fts_triggers():
    with temp_root() as root:
        root.led()
        c = raw(root.db)
        trig = [tuple(x) for x in c.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
            "('documents','findings','notes','chat_messages') ORDER BY name")]
        r.eq("fts_triggers 12 triggers on the 4 content tables", len(trig), 12)
        for table in PLAN_FTS.values():
            kinds = sorted(next(w for w in ("INSERT", "DELETE", "UPDATE") if f"AFTER {w}" in s.upper())
                           for _n, t, s in trig if t == table)
            r.eq(f"fts_triggers {table}: insert, delete, update", kinds, ["DELETE", "INSERT", "UPDATE"])
        c.close()


def _add_note(root, args: dict) -> int:
    """One note through the ``add_note`` desk tool (the notes producer)."""
    from types import SimpleNamespace
    from lib.desk import tools
    ctx = tools.ToolContext(role="desk", runtime=SimpleNamespace(root=str(root.path)), secrets=None,
                            led=root.led(), cfg=None)
    return int(tools.t_add_note(ctx, args)["note_id"])


def _fts_rows(conn, root):
    """One row in each content table through its producer. Returns {fts: (table, id, indexed columns)}."""
    now = T0
    doc = store.store_document(conn, kind="web", title="zebracorn page", text="the zebracorn walks alone",
                               now=now, ticker="ANET")
    fid = findings.record_finding(conn, ticker="ANET", kind="other", claim="zebracorn claim here",
                                  quote="the zebracorn walks alone", document_id=doc, created_by="desk", now=now)
    store.insert_owner_message(conn, chat_id="42", tg_message_id=7, text="zebracorn chat", created_at=now)
    mid = conn.execute("SELECT id FROM chat_messages WHERE chat_id='42' AND tg_message_id=7").fetchone()[0]
    conn.commit()
    nid = _add_note(root, {"ticker": "ANET", "text": "zebracorn note", "tags": ["zebracorn"]})
    return {"documents_fts": ("documents", doc, ("title", "text")),
            "findings_fts": ("findings", fid, ("claim", "quote")),
            "notes_fts": ("notes", nid, ("text", "tags")), "chat_messages_fts": ("chat_messages", mid, ("text",))}


def _hits(conn, fts, term) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {fts} WHERE {fts} MATCH ?", (f'"{term}"',)).fetchone()[0]


def _integrity(conn, fts):
    try:
        conn.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")
        return None
    except sqlite3.DatabaseError as e:
        return str(e)


def case_fts_sync():
    clock = FakeClock(T0).pin_market()
    try:
        with temp_root() as root:
            led = root.led()
            with led.connect() as conn:
                made = _fts_rows(conn, root)
            c = raw(root.db)
            for fts, (table, rid, fcols) in made.items():
                r.eq(f"fts_sync {fts} a new term hits", _hits(c, fts, "zebracorn"), 1)
                sets = ", ".join(f"{k}=?" for k in fcols)
                c.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*[f"quokkafish {k}" for k in fcols], rid))
                r.eq(f"fts_sync {fts} after UPDATE the old term 0", _hits(c, fts, "zebracorn"), 0)
                r.eq(f"fts_sync {fts} after UPDATE the new term 1", _hits(c, fts, "quokkafish"), 1)
                r.eq(f"fts_sync {fts} integrity-check passes", _integrity(c, fts), None)
            for fts, (table, rid, _cols) in made.items():
                if table == "documents":
                    continue                    # findings.document_id points at it; delete it last
                c.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
                r.eq(f"fts_sync {fts} after DELETE 0", _hits(c, fts, "quokkafish"), 0)
                r.eq(f"fts_sync {fts} integrity-check passes after DELETE", _integrity(c, fts), None)
            c.execute("DELETE FROM documents WHERE id=?", (made["documents_fts"][1],))
            r.eq("fts_sync documents_fts after DELETE 0", _hits(c, "documents_fts", "quokkafish"), 0)
            r.eq("fts_sync documents_fts integrity-check passes after DELETE", _integrity(c, "documents_fts"), None)
            c.close()
        # A row deleted behind the triggers' back makes the check fail (the check has teeth).
        with temp_root() as root:
            led = root.led()
            with led.connect() as conn:
                made = _fts_rows(conn, root)
            c = raw(root.db)
            for fts, (table, rid, _col) in made.items():
                c.execute(f"DROP TRIGGER {table}_ad")
                c.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
                err = _integrity(c, fts)
                r.ok(f"fts_sync {fts} integrity-check fails after a silent delete", err is not None, err)
            c.close()
    finally:
        clock.unpin()


def case_checks():
    with temp_root() as root:
        root.led()
        c = raw(root.db)
        for (table, col), allowed in PLAN_ENUMS.items():
            base = BASE_ROWS[table]
            for v in allowed:
                err = try_insert(c, table, {**base, col: v})
                r.eq(f"checks {table}.{col} accepts {v!r}", err, None)
            err = try_insert(c, table, {**base, col: "bogus"})
            r.ok(f"checks {table}.{col} refuses a bad value", err is not None and "CHECK" in err, err)
        r.eq("checks no row stays behind", sum(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                               for t in BASE_ROWS), 0)
        c.close()


def case_dedup():
    with temp_root() as root:
        led = root.led()
        with led.connect() as conn:
            a = store.store_document(conn, kind="filing_section", title="A", text="same text", now=T0, ticker="ANET")
            b = store.store_document(conn, kind="filing_section", title="B", text="same text", now=T0, ticker="CEG")
            other = store.store_document(conn, kind="news", title="C", text="same text", now=T0)
            r.eq("dedup one (kind, sha256) gives one document id", a, b)
            r.ok("dedup another kind is another row", other != a, (a, other))
            r.eq("dedup rows", conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
            r.eq("dedup first insert of (chat_id, tg_message_id)",
                 store.insert_owner_message(conn, chat_id="9", tg_message_id=5, text="hi", created_at=T0), True)
            r.eq("dedup a duplicate (chat_id, tg_message_id) is refused",
                 store.insert_owner_message(conn, chat_id="9", tg_message_id=5, text="again", created_at=T0), False)
            store.insert_desk_message(conn, chat_id="9", text="x", source="chat", now=T0, tg_message_id=5)
            r.eq("dedup a desk row with that tg id is refused",
                 conn.execute("SELECT COUNT(*) FROM chat_messages WHERE chat_id='9' AND tg_message_id=5").fetchone()[0], 1)
            r.eq("dedup the same tg id in another chat is allowed",
                 store.insert_owner_message(conn, chat_id="10", tg_message_id=5, text="hi", created_at=T0), True)
            n1 = store.insert_desk_message(conn, chat_id="9", text="one", source="brief", now=T0)
            n2 = store.insert_desk_message(conn, chat_id="9", text="two", source="brief", now=T0)
            r.ok("dedup two NULL tg ids are allowed", n1 > 0 and n2 > 0 and n1 != n2, (n1, n2))
            r.eq("dedup chat rows", conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0], 4)
        c = raw(root.db)
        err = None
        try:
            c.execute("INSERT INTO chat_messages (chat_id, tg_message_id, role, source, text, created_at) "
                      "VALUES ('9', 5, 'owner', 'chat', 'raw', ?)", (T0,))
        except sqlite3.IntegrityError as e:
            err = str(e)
        r.ok("dedup the unique index refuses a plain duplicate insert", err and "UNIQUE" in err, err)
        c.close()


def _pre_db(root) -> Path:
    p = root.path / "state" / "pre.db"
    p.write_bytes(fx_bytes("pre_db"))
    return p


def case_migrate():
    with temp_root() as root:
        path = _pre_db(root)
        c = raw(path)
        pre_tables = table_names(c)
        old_cols = cols(c, "orders")
        old_orders = [tuple(x) for x in c.execute("SELECT * FROM orders ORDER BY ref_id")]
        old_tp_cols = cols(c, "target_portfolio")
        old_tp = [tuple(x) for x in c.execute("SELECT * FROM target_portfolio ORDER BY goal_id, ticker")]
        r.ok("migrate pre_db has no desk table", not (set(PLAN_TABLES) & pre_tables), set(PLAN_TABLES) & pre_tables)
        r.ok("migrate pre_db orders lacks the desk columns", not (set(ORDERS_NEW) & set(old_cols)))
        c.close()
        led = Ledger(path)
        c = raw(path)
        info = {x[1]: (x[2].upper(), x[3], x[4]) for x in c.execute("PRAGMA table_info(orders)")}
        for col, want in ORDERS_NEW.items():
            r.eq(f"migrate orders gains {col}", info.get(col), want)
        r.eq("migrate orders gains exactly 11 columns", len(cols(c, "orders")) - len(old_cols), 11)
        r.eq("migrate orders keeps the old values",
             [tuple(x)[:len(old_cols)] for x in c.execute(f"SELECT {', '.join(old_cols)} FROM orders ORDER BY ref_id")],
             old_orders)
        r.eq("migrate the old row reads 0 in the NOT NULL DEFAULT 0 columns",
             [tuple(x) for x in c.execute("SELECT reconcile_attempts, review_ok FROM orders")], [(0, 0)] * len(old_orders))
        tp = {x[1]: x[2].upper() for x in c.execute("PRAGMA table_info(target_portfolio)")}
        r.eq("migrate target_portfolio gains entry_price_max REAL", tp.get("entry_price_max"), "REAL")
        r.eq("migrate target_portfolio keeps the old values",
             [tuple(x) for x in c.execute(f"SELECT {', '.join(old_tp_cols)} FROM target_portfolio ORDER BY goal_id, ticker")],
             old_tp)
        r.eq("migrate entry_price_max is NULL on old rows",
             c.execute("SELECT COUNT(*) FROM target_portfolio WHERE entry_price_max IS NOT NULL").fetchone()[0], 0)
        bj = {x[1]: (x[2].upper(), x[3], x[4]) for x in c.execute("PRAGMA table_info(broker_jobs)")}
        r.eq("migrate broker_jobs has recovery INTEGER NOT NULL DEFAULT 0", bj.get("recovery"), ("INTEGER", 1, "0"))
        after1 = table_names(c)
        r.eq("migrate no pre table dropped", sorted(pre_tables - after1), [])
        r.eq("migrate every desk table added", sorted(set(PLAN_TABLES) - after1), [])
        m1 = master_sql(c)
        c.close()
        try:
            led = Ledger(path)
            again = True
        except Exception as e:  # noqa: BLE001
            again = repr(e)
        r.eq("migrate run twice: no error", again, True)
        c = raw(path)
        r.eq("migrate run twice: no table dropped or added", table_names(c), after1)
        r.eq("migrate run twice: sqlite_master unchanged", master_sql(c), m1)
        c.close()
        ref = old_orders[0][0]
        stored = []
        for st in ORDER_STATES:
            led.set_order_state(ref, st)
            stored.append(led.get_order(ref)["state"])
        r.eq("migrate each of the 13 orders.state values stores", stored, list(ORDER_STATES))
        r.eq("migrate the plan lists 13 states", len(ORDER_STATES), 13)
        # reserve_order (§6.1): a proposed leg leaves submitted_at NULL; another state stores the time.
        led.reserve_order("leg-1", "2026-08-05", "ANET", side="buy", type="market", dollar_amount=10.0,
                          quantity=None, now_iso=T0, state="proposed", proposal_id=1, leg_seq=1,
                          quote_at_proposal=150.0)
        led.reserve_order("leg-2", "2026-08-05", "ANET", side="buy", type="market", dollar_amount=10.0,
                          quantity=None, now_iso=T0, state="approved")
        o1, o2 = led.get_order("leg-1"), led.get_order("leg-2")
        r.eq("migrate a proposed leg has submitted_at NULL", o1["submitted_at"], None)
        r.eq("migrate a proposed leg keeps its proposal fields",
             (o1["state"], o1["proposal_id"], o1["leg_seq"], o1["quote_at_proposal"]), ("proposed", 1, 1, 150.0))
        r.eq("migrate another state stores submitted_at", o2["submitted_at"], T0)
    with temp_root() as root:
        led = root.led()
        c = raw(root.db)
        fresh = {x[1]: (x[2].upper(), x[3], x[4]) for x in c.execute("PRAGMA table_info(orders)")}
        r.eq("migrate a fresh ledger has the same desk columns", {k: fresh.get(k) for k in ORDERS_NEW}, ORDERS_NEW)
        r.ok("migrate a fresh target_portfolio has entry_price_max", "entry_price_max" in cols(c, "target_portfolio"))
        c.close()


def _iso(dt: datetime) -> str:
    return store.iso(dt)


def case_named_locks():
    t0 = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)
    now = _iso(t0)
    ttl = 600
    with temp_root() as root:
        led = root.led()
        me = runlock.holder_id()
        held = []
        try:
            with runlock.run_lock(led, "monitor", me, now, ttl):
                with runlock.run_lock(led, "worker", me, now, ttl):
                    with runlock.run_lock(led, "broker", me, now, ttl):
                        held = sorted(n for n in ("monitor", "worker", "broker") if led.named_lock(n))
                        r.raises("named_locks a second worker is refused",
                                 lambda: runlock.run_lock(led, "worker", runlock.holder_id(os.getppid()),
                                                          now, ttl).__enter__(), runlock.RunLockError, "worker")
                        r.eq("named_locks the refused run left the holder", led.named_lock("worker")["holder"], me)
        except runlock.RunLockError as e:
            r.ok("named_locks three runners hold at once", False, str(e))
        r.eq("named_locks monitor, worker and broker hold at once", held, ["broker", "monitor", "worker"])
        r.eq("named_locks each lock is released on exit",
             [led.named_lock(n) for n in ("monitor", "worker", "broker")], [None, None, None])

        boot = "boot-a"

        def dead_fn(alive_pids):
            return lambda h: runlock.holder_dead(h, this_boot=boot, alive=lambda pid: pid in alive_pids)

        # A live holder inside its TTL keeps the lock.
        r.eq("named_locks take w1", led.try_acquire_named_lock("worker", f"{boot}:111", now, ttl_seconds=ttl,
                                                                 holder_dead=dead_fn({111, 222})), True)
        later = _iso(t0 + timedelta(seconds=ttl - 1))
        r.eq("named_locks a live holder inside its TTL keeps it",
             led.try_acquire_named_lock("worker", f"{boot}:222", later, ttl_seconds=ttl,
                                        holder_dead=dead_fn({111, 222})), False)
        r.eq("named_locks holder unchanged", led.named_lock("worker")["holder"], f"{boot}:111")
        # A stale lock (live holder, past the TTL) is stolen (G1).
        stale = _iso(t0 + timedelta(seconds=ttl + 1))
        r.eq("named_locks a stale lock is stolen",
             led.try_acquire_named_lock("worker", f"{boot}:222", stale, ttl_seconds=ttl,
                                        holder_dead=dead_fn({111, 222})), True)
        r.eq("named_locks the thief holds it", (led.named_lock("worker")["holder"], led.named_lock("worker")["acquired_at"]),
             (f"{boot}:222", stale))
        # A dead holder PID is taken at once.
        r.eq("named_locks take m1", led.try_acquire_named_lock("monitor", f"{boot}:333", now, ttl_seconds=ttl,
                                                                 holder_dead=dead_fn({333})), True)
        r.eq("named_locks a dead holder PID is taken at once",
             led.try_acquire_named_lock("monitor", f"{boot}:444", now, ttl_seconds=ttl,
                                        holder_dead=dead_fn({444})), True)
        r.eq("named_locks monitor holder after the dead-PID take", led.named_lock("monitor")["holder"], f"{boot}:444")
        # A lock from another boot id is taken at once, even when that PID number is alive now.
        r.eq("named_locks take b1", led.try_acquire_named_lock("broker", "boot-old:555", now, ttl_seconds=ttl,
                                                                 holder_dead=dead_fn({555})), True)
        r.eq("named_locks a lock from another boot id is taken at once",
             led.try_acquire_named_lock("broker", f"{boot}:666", now, ttl_seconds=ttl,
                                        holder_dead=dead_fn({555, 666})), True)
        # The other names were not touched by these takes.
        r.eq("named_locks one row per name", sorted(x[0] for x in raw(root.db).execute("SELECT name FROM run_locks")),
             ["broker", "monitor", "worker"])
        # Release by another holder deletes nothing; by the holder deletes the row.
        led.release_named_lock("broker", f"{boot}:999")
        r.ok("named_locks release by another holder keeps the row", led.named_lock("broker") is not None)
        led.release_named_lock("broker", f"{boot}:666")
        r.eq("named_locks release by the holder frees it", led.named_lock("broker"), None)

    # holder_dead itself, with injected values and with the real PID check.
    r.eq("named_locks holder_dead another boot", runlock.holder_dead("b1:10", this_boot="b2", alive=lambda p: True), True)
    r.eq("named_locks holder_dead same boot, dead pid", runlock.holder_dead("b1:10", this_boot="b1", alive=lambda p: False), True)
    r.eq("named_locks holder_dead same boot, live pid", runlock.holder_dead("b1:10", this_boot="b1", alive=lambda p: True), False)
    r.eq("named_locks holder_dead an unknown format is never dead",
         [runlock.holder_dead(h, this_boot="b1", alive=lambda p: False) for h in ("", "garbage", "b1:", ":12", "b1:x")],
         [False] * 5)
    r.eq("named_locks holder_id is <boot_id>:<pid>", runlock.holder_id(), f"{runlock.boot_id()}:{os.getpid()}")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    r.eq("named_locks pid_alive: this process", runlock.pid_alive(os.getpid()), True)
    r.eq("named_locks pid_alive: an exited child", runlock.pid_alive(proc.pid), False)
    r.eq("named_locks a real dead holder is taken at once (default check)",
         runlock.holder_dead(runlock.holder_id(proc.pid)), True)


def _seed_search(root):
    """The 10-Q sections of ``10q_q2`` (ANET) + one finding, note and chat message that quote it."""
    class Client:
        def get_text(self, url):
            return fx("10q_q2")

    led = root.led()
    with led.connect() as conn:
        res = sections.ingest_filing(conn, Client(), ticker="ANET", cik="0001596532",
                                     accession="0001596532-26-000175", form="10-Q",
                                     primary_doc="anet-20260630.htm", now=store.now_iso())
        quote = "we had non-cancellable purchase commitments not recorded on our balance sheet of $9.7 billion"
        doc = next(i for i in res["documents"].values() if quote in store.get_document(conn, i)["text"])
        findings.record_finding(conn, ticker="ANET", kind="other", claim="Large purchase commitments",
                                quote=quote, document_id=doc, created_by="desk", now=store.now_iso())
        store.store_document(conn, kind="web", title="CEG page", text="CEG purchase commitments for fuel",
                             now=store.now_iso(), ticker="CEG")
        store.insert_owner_message(conn, chat_id="42", tg_message_id=1,
                                   text="what are the purchase commitments now?", created_at=store.now_iso())
    _add_note(root, {"ticker": "ANET", "text": "Watch the purchase commitments each quarter."})
    return res


def case_search():
    clock = FakeClock(T0).pin_market()
    try:
        with temp_root() as root:
            res = _seed_search(root)
            r.ok("search the 10-Q stored sections", res["ok"] and len(res["documents"]) >= 8, res.get("error"))
            led = root.led()
            with led.connect() as conn:
                for q in ('"gross margin"', "AI back-end", '"', "Item 1A.", "(", ")", "NEAR(", "-", "*", "a:b",
                          'margin"', "AND", "OR margin", "margin NOT", "^gross", "{text}: margin"):
                    try:
                        store.search(conn, q)
                        ok = True
                    except Exception as e:  # noqa: BLE001
                        ok = repr(e)
                    r.eq(f"search {q!r} raises nothing", ok, True)
                r.ok("search '\"gross margin\"' hits", len(store.search(conn, '"gross margin"', kinds=["document"])) > 0)

                def n(q, kinds=("document",), **kw):
                    return len(store.search(conn, q, kinds=list(kinds), limit=20, **kw))

                r.eq("search a missing word hits nothing", n("zzqxv"), 0)
                r.eq("search two words are AND by default", n("zzqxv margin"), 0)
                r.ok("search OR in capitals is an operator", n("zzqxv OR margin") > 0)
                r.eq("search or in lower case is a word", n("zzqxv or margin"), 0)
                r.ok("search NOT in capitals is an operator", n("margin NOT zzqxv") > 0)
                r.ok("search a lone leading OR is dropped", n("OR margin") > 0)
                r.ok("search NEAR(...) as written is an operator", n("NEAR(purchase commitments, 2)") > 0)
                r.eq("search NEAR(...) with a missing word", n("NEAR(purchase zzqxv)"), 0)
                r.eq("search near(...) in lower case is words", n("near(purchase commitments)"), 0)
                # The porter tokenizer stores "commitments" as "commit", so the prefix is "commi".
                r.ok("search a word* is a prefix query", n("commi*") > 0)
                r.eq("search a word with no * is not a prefix", n("commi"), 0)
                r.eq("search a * inside a word is no prefix", n("co*mmi"), 0)
                r.eq("search fts_query of a NEAR", store.fts_query("NEAR(gross margin, 5)"), 'NEAR("gross" "margin", 5)')
                r.eq("search fts_query quotes words and keeps the operator",
                     store.fts_query("Item 1A. OR back-end*"), '"Item" "1A" OR "back end"*')
                hits = store.search(conn, "purchase commitments", limit=20)
                kinds = {h["kind"] for h in hits}
                r.eq("search one query hits all 4 tables", kinds, {"document", "finding", "note", "chat"})
                scores = [h["score"] for h in hits]
                r.eq("search the merged list is in bm25 order", scores, sorted(scores))
                r.ok("search every hit has a snippet with the match marked",
                     all("[" in h["snippet"] and "]" in h["snippet"] for h in hits), [h["snippet"] for h in hits])
                doc_hit = next(h for h in hits if h["kind"] == "document")
                r.ok("search a document hit cites its source",
                     doc_hit["source"]["doc_kind"] in ("filing_section", "web") and "section_key" in doc_hit["source"])
                th = store.search(conn, "purchase commitments", ticker="anet", limit=20)
                r.ok("search ticker filter keeps only ANET", th and all(h["ticker"] == "ANET" for h in th),
                     [(h["kind"], h["ticker"]) for h in th])
                r.ok("search ticker filter drops chat", all(h["kind"] != "chat" for h in th))
                r.ok("search without the filter CEG is there", any(h["ticker"] == "CEG" for h in hits))
                r.eq("search since after every row hits nothing",
                     store.search(conn, "purchase commitments", since="2026-08-06T00:00:00Z"), [])
                r.eq("search since before the rows keeps them",
                     len(store.search(conn, "purchase commitments", since="2026-08-05T00:00:00Z", limit=20)), len(hits))
                r.raises("search limit 21 refused", lambda: store.search(conn, "margin", limit=21), ValueError)
                r.raises("search limit 0 refused", lambda: store.search(conn, "margin", limit=0), ValueError)
                r.eq("search limit 20 allowed", len(store.search(conn, "margin", limit=20)) <= 20, True)
                r.eq("search limit caps the merged list", len(store.search(conn, "purchase commitments", limit=2)), 2)
                try:
                    store.search(conn, "x'; DROP TABLE documents; --")
                    ok = True
                except Exception as e:  # noqa: BLE001
                    ok = repr(e)
                r.eq("search the query is a parameter (SQL text in it is safe)", ok, True)
                r.ok("search the documents table is still there",
                     conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 0)
    finally:
        clock.unpin()


CASES = [case_schema_all, case_schema_rerun, case_fts_triggers, case_fts_sync, case_checks, case_dedup,
         case_migrate, case_named_locks, case_search]

if __name__ == "__main__":
    for case in CASES:
        try:
            case()
        except Exception as e:  # noqa: BLE001 — one broken case must not hide the others
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    raise SystemExit(r.done())
