"""The desk's research memory and work queues: schema + shared data access (plan §6).

Every desk table lives in the ledger database, so the nightly backup covers it and a
proposal can cite a finding by id. ``Ledger.ensure_schema`` calls ``ensure_desk_schema``.
Every statement is ``IF NOT EXISTS``, so a re-run is safe. No statement drops a table.

Times are UTC ISO-8601 strings (``2026-10-27T18:40:00Z``). CIKs are 10-digit, zero-padded
strings. Nothing in these tables is ever pruned (§6.4).

Helpers take an open ``sqlite3.Connection`` (``Ledger.connect()`` or
``Ledger.transaction()``), so a caller can group several writes in one transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

DESK_SCHEMA = """
-- The research list: every company or fund the desk knows about.
CREATE TABLE IF NOT EXISTS companies (
  ticker        TEXT PRIMARY KEY,
  name          TEXT,
  kind          TEXT NOT NULL CHECK (kind IN ('company','etf','cef','crypto_etf','cash')),
  cik           TEXT,
  series_id     TEXT,
  sleeve        TEXT,
  status        TEXT NOT NULL CHECK (status IN ('candidate','watch','holding','rejected','dropped')),
  status_reason TEXT,
  added_at      TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sec_ticker_map (
  ticker TEXT PRIMARY KEY, cik TEXT NOT NULL, series_id TEXT, title TEXT,
  source TEXT NOT NULL CHECK (source IN ('company_tickers','company_tickers_mf')),
  fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sec_overrides (
  ticker TEXT PRIMARY KEY, cik TEXT, kind TEXT NOT NULL, note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filings (
  accession   TEXT PRIMARY KEY,
  cik         TEXT NOT NULL,
  ticker      TEXT NOT NULL,
  form        TEXT NOT NULL,
  filed_date  TEXT NOT NULL,
  accepted_at TEXT,
  report_date TEXT,
  items       TEXT,
  primary_doc TEXT,
  tier        TEXT NOT NULL CHECK (tier IN ('redecide','record','ignore','baseline')),
  seen_at     TEXT NOT NULL,
  ingested_at TEXT,
  ingest_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_filings_ticker ON filings(ticker, filed_date);
-- Every stored text: filing sections, transcripts, news articles, web pages.
CREATE TABLE IF NOT EXISTS documents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL CHECK (kind IN ('filing_section','transcript','news','web')),
  ticker       TEXT,
  accession    TEXT,
  section_key  TEXT,
  title        TEXT NOT NULL,
  url          TEXT,
  published_at TEXT,
  fetched_at   TEXT NOT NULL,
  text         TEXT NOT NULL,
  sha256       TEXT NOT NULL,
  char_len     INTEGER NOT NULL,
  UNIQUE (kind, sha256)
);
CREATE INDEX IF NOT EXISTS idx_documents_ticker ON documents(ticker, kind, published_at);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  title, text, content='documents', content_rowid='id', tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS xbrl_facts (
  cik TEXT NOT NULL, taxonomy TEXT NOT NULL, concept TEXT NOT NULL, unit TEXT NOT NULL,
  start_date TEXT NOT NULL DEFAULT '', end_date TEXT NOT NULL, value REAL NOT NULL,
  accession TEXT NOT NULL, fy INTEGER, fp TEXT, form TEXT, filed TEXT, frame TEXT,
  PRIMARY KEY (cik, taxonomy, concept, unit, start_date, end_date, accession)
);
CREATE TABLE IF NOT EXISTS metrics (
  ticker TEXT NOT NULL, as_of TEXT NOT NULL, source_accession TEXT,
  price REAL, metrics_json TEXT NOT NULL,
  PRIMARY KEY (ticker, as_of)
);
CREATE TABLE IF NOT EXISTS findings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker      TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('market_size','growth','guidance','margin','segment',
                'customer','competition','risk','management','capital','other')),
  claim       TEXT NOT NULL,
  quote       TEXT NOT NULL,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  quote_start INTEGER NOT NULL,
  created_by  TEXT NOT NULL CHECK (created_by IN ('desk','analyst')),
  event_id    INTEGER,
  created_at  TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS findings_fts USING fts5(
  claim, quote, content='findings', content_rowid='id', tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, text TEXT NOT NULL, tags TEXT,
  created_by TEXT NOT NULL CHECK (created_by IN ('desk','analyst')), created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  text, tags, content='notes', content_rowid='id', tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS premise (
  version INTEGER PRIMARY KEY, text TEXT NOT NULL, goal_json TEXT NOT NULL,
  confirmed_at TEXT NOT NULL, proposal_id INTEGER          -- NULL only for the seeded v1
);
CREATE TABLE IF NOT EXISTS theses (
  ticker          TEXT NOT NULL,
  version         INTEGER NOT NULL,
  premise_link    TEXT NOT NULL,
  summary         TEXT NOT NULL,
  drivers_json    TEXT NOT NULL,
  falsifiers_json TEXT NOT NULL,
  valuation_json  TEXT NOT NULL,      -- method + scenario inputs (§8.3)
  fair_value_low  REAL, fair_value_base REAL, fair_value_high REAL,
  buy_below       REAL, trim_above REAL,
  evidence_json   TEXT NOT NULL,
  confirmed_at    TEXT NOT NULL,
  proposal_id     INTEGER NOT NULL,
  PRIMARY KEY (ticker, version)
);
CREATE TABLE IF NOT EXISTS level_state (
  ticker TEXT NOT NULL, level TEXT NOT NULL CHECK (level IN ('buy_below','trim_above','entry')),
  price REAL NOT NULL, armed INTEGER NOT NULL, last_quote REAL, changed_at TEXT NOT NULL,
  PRIMARY KEY (ticker, level)
);
CREATE TABLE IF NOT EXISTS news_items (
  url_hash TEXT NOT NULL, ticker TEXT NOT NULL, title TEXT NOT NULL, publisher TEXT,
  url TEXT NOT NULL, published_at TEXT, seen_at TEXT NOT NULL,
  material INTEGER, category TEXT, reason TEXT, classify_attempts INTEGER NOT NULL DEFAULT 0,
  classified_at TEXT, document_id INTEGER,
  PRIMARY KEY (url_hash, ticker)              -- one article can concern several tickers
);
CREATE TABLE IF NOT EXISTS transcripts (
  ticker TEXT NOT NULL, quarter TEXT NOT NULL, document_id INTEGER NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (ticker, quarter)
);
CREATE TABLE IF NOT EXISTS api_budget (
  provider TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL, PRIMARY KEY (provider, day)
);
CREATE TABLE IF NOT EXISTS etf_holdings (
  fund TEXT NOT NULL, as_of TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('yfinance_top','nport')),
  holding_name TEXT NOT NULL, holding_ticker TEXT, cusip TEXT, weight_pct REAL NOT NULL,
  PRIMARY KEY (fund, as_of, source, holding_name)
);
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  dedup_key    TEXT NOT NULL UNIQUE,
  ticker       TEXT NOT NULL,
  type         TEXT NOT NULL CHECK (type IN ('filing','earnings','price_level','drift','news','lookthrough')),
  created_by   TEXT NOT NULL CHECK (created_by IN ('monitor','desk')),
  detail_json  TEXT NOT NULL,
  detected_at  TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('new','queued','analyzed','proposed','noted','failed')),
  verdict      TEXT CHECK (verdict IN ('intact','strengthened','weakened','broken','n/a')),
  analysis     TEXT,
  analyzed_at  TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('analyze_event','deep_dive','classify_news')),
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed')),
  requested_by TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS broker_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('snapshot','execute')),
  proposal_id INTEGER, recovery INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed')),
  requested_by TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS broker_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
  equity REAL NOT NULL, cash REAL, buying_power REAL NOT NULL,
  positions_json TEXT NOT NULL, quotes_json TEXT NOT NULL,
  approval_setting TEXT, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT NOT NULL CHECK (kind IN ('trade','change')),
  origin         TEXT NOT NULL CHECK (origin IN ('event','owner','change','drift','seed')),
  event_id       INTEGER, owner_message_id INTEGER, parent_change_id INTEGER,
  payload_json   TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  rationale      TEXT NOT NULL,
  evidence_json  TEXT NOT NULL,
  created_by     TEXT NOT NULL CHECK (created_by IN ('desk','analyst','monitor','system')),
  created_at     TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  state          TEXT NOT NULL CHECK (state IN ('pending','approved','rejected','expired','superseded',
                   'withdrawn','executing','executed','partially_executed','failed','requoted','applied')),
  nonce          TEXT NOT NULL,
  tg_chat_id     TEXT, tg_message_id INTEGER,
  decided_at     TEXT, decided_by TEXT,
  superseded_by  INTEGER,
  result_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals(state, expires_at);
CREATE TABLE IF NOT EXISTS desk_settings (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL, proposal_id INTEGER
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL, tg_message_id INTEGER,
  role TEXT NOT NULL CHECK (role IN ('owner','desk')),
  source TEXT NOT NULL CHECK (source IN ('chat','analyst','monitor','brief','proposal','execution','system')),
  text TEXT NOT NULL, reaction TEXT, reacted_at TEXT, created_at TEXT NOT NULL, session_id TEXT,
  delivered_to_session INTEGER NOT NULL DEFAULT 0   -- owner row: a turn took it; desk row: a turn saw it
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_tg ON chat_messages(chat_id, tg_message_id)
  WHERE tg_message_id IS NOT NULL;
-- Insert owner rows with `ON CONFLICT DO NOTHING` (no conflict target). A target without
-- the index's WHERE clause fails to prepare against a partial index (SQLite 3.45 and 3.54).
CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
  text, content='chat_messages', content_rowid='id', tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS chat_sessions (
  chat_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, started_at TEXT NOT NULL, last_turn_at TEXT
);
CREATE TABLE IF NOT EXISTS bridge_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- One lock per runner name. lib/runlock.run_lock(led, name, holder, now_iso, ttl) uses it.
CREATE TABLE IF NOT EXISTS run_locks (
  name TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at TEXT NOT NULL
);
"""

# FTS5 table -> (content table, indexed columns). One external-content index per table.
FTS_TABLES = {
    "documents_fts": ("documents", ("title", "text")),
    "findings_fts": ("findings", ("claim", "quote")),
    "notes_fts": ("notes", ("text", "tags")),
    "chat_messages_fts": ("chat_messages", ("text",)),
}

# The tables of §6.1 (the 30 a fresh schema creates; FTS shadow tables not counted).
DESK_TABLES = (
    "companies", "sec_ticker_map", "sec_overrides", "filings", "documents", "documents_fts",
    "xbrl_facts", "metrics", "findings", "findings_fts", "notes", "notes_fts", "premise",
    "theses", "level_state", "news_items", "transcripts", "api_budget", "etf_holdings",
    "events", "jobs", "broker_jobs", "broker_snapshots", "proposals", "desk_settings",
    "chat_messages", "chat_messages_fts", "chat_sessions", "bridge_state", "run_locks",
)


def _fts_triggers() -> str:
    """The three standard external-content triggers per FTS table (12 in all)."""
    out = []
    for fts, (table, cols) in FTS_TABLES.items():
        names = ", ".join(cols)
        new_vals = ", ".join(f"new.{c}" for c in cols)
        old_vals = ", ".join(f"old.{c}" for c in cols)
        out.append(
            f"CREATE TRIGGER IF NOT EXISTS {table}_ai AFTER INSERT ON {table} BEGIN\n"
            f"  INSERT INTO {fts}(rowid, {names}) VALUES (new.id, {new_vals});\nEND;\n"
            f"CREATE TRIGGER IF NOT EXISTS {table}_ad AFTER DELETE ON {table} BEGIN\n"
            f"  INSERT INTO {fts}({fts}, rowid, {names}) VALUES ('delete', old.id, {old_vals});\nEND;\n"
            f"CREATE TRIGGER IF NOT EXISTS {table}_au AFTER UPDATE ON {table} BEGIN\n"
            f"  INSERT INTO {fts}({fts}, rowid, {names}) VALUES ('delete', old.id, {old_vals});\n"
            f"  INSERT INTO {fts}(rowid, {names}) VALUES (new.id, {new_vals});\nEND;\n")
    return "".join(out)


def ensure_desk_schema(conn) -> None:
    """Create every §6.1 table, index and FTS trigger. Safe to run again."""
    conn.executescript(DESK_SCHEMA)
    conn.executescript(_fts_triggers())


# --- time and hashing --------------------------------------------------------------

def utc_now() -> datetime:
    """Now in UTC. The desk reads the clock through ``lib.market.now_et`` so a test can pin it."""
    from lib import market
    return market.now_et().astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """A UTC ISO-8601 string with a ``Z`` suffix, second precision."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(utc_now())


def parse_ts(value) -> Optional[datetime]:
    """Parse a stored time (``Z`` or ``+00:00`` or an ET offset) into an aware datetime.
    A naive value counts as UTC. Returns None for an empty or bad value."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_seconds(value, now: Optional[datetime] = None) -> Optional[float]:
    dt = parse_ts(value)
    if dt is None:
        return None
    return ((now or utc_now()) - dt).total_seconds()


def canonical_json(obj) -> str:
    """Sorted keys, no spaces: the form ``payload_sha256`` hashes (§10.1 rule 1)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def loads(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# --- key-value state ---------------------------------------------------------------

def get_state(conn, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM bridge_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn, key: str, value) -> None:
    conn.execute("INSERT INTO bridge_state (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def touch_trigger(root, name: str) -> None:
    """Create ``state/triggers/<name>`` (``worker`` or ``broker``) with the UTC time.
    systemd starts the runner while the file exists; the runner deletes it first."""
    import os
    from pathlib import Path
    if name not in ("worker", "broker"):
        raise ValueError(f"unknown trigger {name!r}")
    d = Path(root) / "state" / "triggers"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.tmp"
    tmp.write_text(now_iso() + "\n", encoding="utf-8")
    os.replace(tmp, d / name)


# --- companies and SEC entity map --------------------------------------------------

def get_company(conn, ticker: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM companies WHERE ticker=?", (str(ticker).upper(),)).fetchone()
    return dict(row) if row else None


def upsert_company(conn, *, ticker, kind, status, now, name=None, cik=None, series_id=None,
                   sleeve=None, status_reason=None) -> None:
    """Insert a company, or update its facts. An existing row keeps its status: only the
    callers that own a status change (``set_company_status``) move it."""
    conn.execute(
        "INSERT INTO companies (ticker, name, kind, cik, series_id, sleeve, status, status_reason, "
        "added_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
        "name=COALESCE(excluded.name, companies.name), kind=excluded.kind, "
        "cik=COALESCE(excluded.cik, companies.cik), "
        "series_id=COALESCE(excluded.series_id, companies.series_id), "
        "sleeve=COALESCE(excluded.sleeve, companies.sleeve), updated_at=excluded.updated_at",
        (str(ticker).upper(), name, kind, cik, series_id, sleeve, status, status_reason, now, now))


def set_company_status(conn, ticker: str, status: str, reason: Optional[str], now: str) -> None:
    conn.execute("UPDATE companies SET status=?, status_reason=?, updated_at=? WHERE ticker=?",
                 (status, reason, now, str(ticker).upper()))


def list_companies(conn, status: Optional[str] = None) -> list:
    if status:
        rows = conn.execute("SELECT * FROM companies WHERE status=? ORDER BY ticker", (status,))
    else:
        rows = conn.execute("SELECT * FROM companies ORDER BY ticker")
    return [dict(r) for r in rows.fetchall()]


# --- documents ---------------------------------------------------------------------

def store_document(conn, *, kind: str, title: str, text: str, now: str, ticker=None,
                   accession=None, section_key=None, url=None, published_at=None) -> int:
    """Store one text and return its id. One ``(kind, sha256)`` is one row: the same text
    stored twice returns the first id."""
    text = str(text or "")
    digest = sha256_hex(text)
    row = conn.execute("SELECT id FROM documents WHERE kind=? AND sha256=?", (kind, digest)).fetchone()
    if row:
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO documents (kind, ticker, accession, section_key, title, url, published_at, "
        "fetched_at, text, sha256, char_len) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (kind, ticker.upper() if ticker else None, accession, section_key, str(title or "")[:500],
         url, published_at, now, text, digest, len(text)))
    return int(cur.lastrowid)


def get_document(conn, document_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM documents WHERE id=?", (int(document_id),)).fetchone()
    return dict(row) if row else None


# --- API budgets (Alpha Vantage, deep-dives) ---------------------------------------

def budget_used(conn, provider: str, day: str) -> int:
    row = conn.execute("SELECT used FROM api_budget WHERE provider=? AND day=?", (provider, day)).fetchone()
    return int(row[0]) if row else 0


def budget_take(conn, provider: str, day: str, limit: int) -> bool:
    """Count one call against ``limit`` for ``day``. False (and no count) at the limit."""
    if budget_used(conn, provider, day) >= limit:
        return False
    conn.execute("INSERT INTO api_budget (provider, day, used) VALUES (?, ?, 1) "
                 "ON CONFLICT(provider, day) DO UPDATE SET used=used+1", (provider, day))
    return True


# --- events and jobs ---------------------------------------------------------------

def create_event(conn, *, dedup_key: str, ticker: str, type: str, detail: dict, now: str,
                 created_by: str = "monitor", status: str = "new") -> Optional[int]:
    """Insert one event. A repeated ``dedup_key`` inserts nothing and returns None."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (dedup_key, ticker, type, created_by, detail_json, "
        "detected_at, status) VALUES (?,?,?,?,?,?,?)",
        (dedup_key, str(ticker).upper(), type, created_by, json.dumps(detail, sort_keys=True),
         now, status))
    return int(cur.lastrowid) if cur.rowcount else None


def get_event(conn, event_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM events WHERE id=?", (int(event_id),)).fetchone()
    return dict(row) if row else None


def enqueue_job(conn, *, kind: str, payload: dict, requested_by: str, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (kind, payload_json, status, requested_by, created_at) "
        "VALUES (?, ?, 'queued', ?, ?)", (kind, json.dumps(payload, sort_keys=True), requested_by, now))
    return int(cur.lastrowid)


def enqueue_snapshot_job(conn, *, requested_by: str, now: str) -> int:
    """Queue one broker ``snapshot`` job. A snapshot job places nothing (§11.3)."""
    cur = conn.execute(
        "INSERT INTO broker_jobs (kind, status, requested_by, created_at) "
        "VALUES ('snapshot', 'queued', ?, ?)", (requested_by, now))
    return int(cur.lastrowid)


def latest_snapshot(conn) -> Optional[dict]:
    row = conn.execute("SELECT * FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# --- chat mirror -------------------------------------------------------------------

def insert_owner_message(conn, *, chat_id, tg_message_id, text: str, created_at: str) -> bool:
    """Insert one owner message. A repeated ``(chat_id, tg_message_id)`` inserts nothing."""
    cur = conn.execute(
        "INSERT INTO chat_messages (chat_id, tg_message_id, role, source, text, created_at) "
        "VALUES (?, ?, 'owner', 'chat', ?, ?) ON CONFLICT DO NOTHING",
        (str(chat_id), int(tg_message_id), str(text or ""), created_at))
    return cur.rowcount > 0


def insert_desk_message(conn, *, chat_id, text: str, source: str, now: str,
                        tg_message_id=None, session_id=None, delivered: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO chat_messages (chat_id, tg_message_id, role, source, text, created_at, "
        "session_id, delivered_to_session) VALUES (?, ?, 'desk', ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (str(chat_id), tg_message_id, source, str(text or ""), now, session_id, int(delivered)))
    return int(cur.lastrowid or 0)


# --- retrieval (§6.2) --------------------------------------------------------------

_OPERATORS = {"AND", "OR", "NOT"}
_TOKEN_RE = re.compile(r'NEAR\([^)]*\)|"[^"]*"|\S+')
_WORD_CHARS = re.compile(r"[^\w*]+", re.UNICODE)


def _clean_word(raw: str) -> Optional[str]:
    """One plain word as a quoted FTS5 string. Punctuation splits the word into a phrase.
    A ``*`` stays only at the end of the word (a prefix query)."""
    star = raw.endswith("*")
    parts = [p for p in _WORD_CHARS.split(raw.replace("*", " ")) if p]
    if not parts:
        return None
    phrase = '"' + " ".join(parts) + '"'
    return phrase + "*" if star else phrase


def fts_query(query: str) -> str:
    """Turn the desk's search text into an FTS5 MATCH string that never raises a syntax
    error. Plain words are quoted. ``AND``/``OR``/``NOT`` (in capitals, between two terms)
    and ``NEAR(...)`` stay operators. Every other punctuation mark goes."""
    tokens = _TOKEN_RE.findall(str(query or ""))
    items: list = []  # ("term", text) or ("op", text)
    for tok in tokens:
        if tok.startswith("NEAR(") and tok.endswith(")"):
            inner = tok[5:-1]
            body, _, dist = inner.partition(",")
            terms = [t for t in (_clean_word(w) for w in body.split()) if t]
            if not terms:
                continue
            d = dist.strip()
            near = "NEAR(" + " ".join(terms) + (f", {int(d)}" if d.isdigit() else "") + ")"
            items.append(("term", near))
        elif tok in _OPERATORS:
            items.append(("op", tok))
        elif tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            words = [p for p in _WORD_CHARS.split(tok[1:-1].replace("*", " ")) if p]
            if words:
                items.append(("term", '"' + " ".join(words) + '"'))
        else:
            word = _clean_word(tok)
            if word:
                items.append(("term", word))
    out: list = []
    for i, (kind, text) in enumerate(items):
        if kind == "op":
            prev_term = bool(out) and out[-1][0] == "term"
            next_term = i + 1 < len(items) and items[i + 1][0] == "term"
            if prev_term and next_term:
                out.append((kind, text))
            continue
        out.append((kind, text))
    return " ".join(text for _k, text in out)


_SEARCH = {
    "document": ("documents_fts", "documents", "d.title", "COALESCE(d.published_at, d.fetched_at)", 1),
    "finding": ("findings_fts", "findings", "d.claim", "d.created_at", 1),
    "note": ("notes_fts", "notes", "substr(d.text, 1, 80)", "d.created_at", 0),
    "chat": ("chat_messages_fts", "chat_messages", "d.role || ' ' || d.source", "d.created_at", 0),
}
SEARCH_KINDS = tuple(_SEARCH)


def search(conn, query: str, *, ticker: Optional[str] = None, kinds: Optional[Iterable[str]] = None,
           since: Optional[str] = None, limit: int = 10) -> list:
    """BM25 search over the four FTS indexes, merged by score (§6.2). A chat message has
    no ticker, so a ticker filter leaves chat out. Raises ValueError on a bad argument."""
    limit = int(limit)
    if limit < 1 or limit > 20:
        raise ValueError("limit must be 1 to 20")
    kinds = list(kinds) if kinds else list(SEARCH_KINDS)
    bad = [k for k in kinds if k not in _SEARCH]
    if bad:
        raise ValueError(f"unknown kinds {bad}; use {list(SEARCH_KINDS)}")
    match = fts_query(query)
    if not match:
        return []
    hits = []
    for kind in kinds:
        fts, table, title_sql, date_sql, snip_col = _SEARCH[kind]
        if ticker and kind == "chat":
            continue
        where, args = [f"{fts} MATCH ?"], [match]
        if ticker:
            where.append("d.ticker = ?")
            args.append(str(ticker).upper())
        if since:
            where.append(f"{date_sql} >= ?")
            args.append(since)
        sql = (f"SELECT d.id AS id, {title_sql} AS title, {date_sql} AS date, "
               f"{'d.ticker' if kind != 'chat' else 'NULL'} AS ticker, "
               f"snippet({fts}, {snip_col}, '[', ']', '…', 16) AS snippet, bm25({fts}) AS score "
               f"FROM {fts} JOIN {table} d ON d.id = {fts}.rowid WHERE {' AND '.join(where)} "
               f"ORDER BY score LIMIT ?")
        for r in conn.execute(sql, (*args, limit)).fetchall():
            hit = {"kind": kind, "id": r["id"], "ticker": r["ticker"], "title": r["title"],
                   "date": r["date"], "snippet": r["snippet"], "score": round(float(r["score"]), 4)}
            if kind in ("finding", "note"):
                # The author shows on the hit: an analyst note is model text, not an owner rule.
                hit["created_by"] = conn.execute(f"SELECT created_by FROM {table} WHERE id=?",
                                                 (r["id"],)).fetchone()[0]
            if kind == "document":
                d = conn.execute("SELECT kind, section_key, accession, url FROM documents WHERE id=?",
                                 (r["id"],)).fetchone()
                hit["source"] = {"doc_kind": d["kind"], "section_key": d["section_key"],
                                 "accession": d["accession"], "url": d["url"]}
            hits.append(hit)
    hits.sort(key=lambda h: h["score"])
    return hits[:limit]
