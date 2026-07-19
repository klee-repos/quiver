#!/usr/bin/env python3
"""Read-only ledger query for the chat bridge — SAFE BY CONSTRUCTION.

The `sqlite3` CLI is BANNED for the chat assistant because its shell ships dangerous SQL
functions — `writefile()`/`readfile()`/`edit()` (filesystem write/read, NOT stopped by
`-readonly`) and `load_extension()` (native code = RCE) — that a keyword denylist can't reliably
catch (audit finding). This wrapper uses the Python `sqlite3` MODULE instead, where those
functions simply DO NOT EXIST and extension loading is disabled by default. It also:

  * opens the DB read-only (`file:...?mode=ro`) with `PRAGMA query_only=ON`,
  * accepts ONLY a single SELECT/WITH statement (no second statement, no DDL/DML),
  * caps the result set,

so there is no filesystem write, no code-exec, and no DB mutation possible — regardless of the
SQL text. The chat guard allows `python deploy/runner/chat_query.py "<sql>"` and denies `sqlite3`.

    chat_query.py "SELECT ... FROM decisions ORDER BY id DESC LIMIT 20"
    chat_query.py --schema         # table names + CREATE statements (from sqlite_master)
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent.parent / "state" / "ledger.db"
MAX_ROWS = 2000
QUERY_DEADLINE_SEC = 20.0  # a runaway SELECT (e.g. a recursive CTE) self-interrupts, no CPU burn
SCHEMA_SQL = "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"


def _run(sql: str) -> int:
    body = sql.strip().rstrip(";").strip()
    if not body:
        print("empty query", file=sys.stderr)
        return 2
    low = body.lstrip("(").lower()
    if not (low.startswith("select") or low.startswith("with")):
        print("only SELECT / WITH queries are allowed", file=sys.stderr)
        return 2
    if ";" in body:
        print("only a single statement is allowed", file=sys.stderr)
        return 2
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error as e:
        print(f"cannot open ledger read-only: {e}", file=sys.stderr)
        return 1
    # Wall-clock watchdog: the sqlite3 progress handler fires every N VM steps; once past the
    # deadline, returning non-zero aborts the query (raises OperationalError). Bounds a runaway
    # SELECT/recursive-CTE independently of the bridge's subprocess timeout.
    t0 = time.monotonic()
    con.set_progress_handler(lambda: 1 if (time.monotonic() - t0) > QUERY_DEADLINE_SEC else 0, 100_000)
    try:
        con.execute("PRAGMA query_only=ON")  # belt-and-suspenders on top of mode=ro
        cur = con.execute(body)              # .execute runs exactly ONE statement
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(MAX_ROWS)
    except sqlite3.Error as e:
        print(f"query error: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()
    if cols:
        print(" | ".join(cols))
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))
    if len(rows) == MAX_ROWS:
        print(f"... (truncated at {MAX_ROWS} rows)")
    return 0


def main(argv: list) -> int:
    if len(argv) != 1:
        print('usage: chat_query.py "<SELECT ...>"  |  chat_query.py --schema', file=sys.stderr)
        return 2
    return _run(SCHEMA_SQL if argv[0] == "--schema" else argv[0])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
