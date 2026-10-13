#!/usr/bin/env python3
"""Ledger backup job (deploy/runner/backup_ledger.py) — offline. No network, no Cloud Storage.

The snapshot comes from a real `Ledger` and the real job code. Only the two network calls
(the metadata server and the upload) are replaced.

Run:  .venv/bin/python tests/test_backup.py
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy.runner import backup_ledger as B  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}{(' — ' + str(detail)) if detail else ''}")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _metadata(path, opener=None):
    return {"project/project-id": "test-project\n",
            "instance/service-accounts/default/token": json.dumps({"access_token": "tok-123"})}[path]


def main() -> int:
    print("=" * 64)
    print("BACKUP — ledger snapshot + create-only upload + verify")
    print("=" * 64)
    now = datetime(2026, 10, 13, 22, 15, 7, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state" / "ledger.db"
        db.parent.mkdir()
        led = Ledger(db)
        for i in range(5):
            led.get_or_create_baseline(f"2026-10-{i + 1:02d}", 2000.0 + i, "t")
        led.record_cash_flow(trade_date="2026-07-09", amount=800.0, note="deposit", created_at="t")
        sent = {}

        def opener(req, timeout=None):
            sent.update(url=req.full_url, method=req.get_method(), data=req.data,
                        auth=req.get_header("Authorization"))
            md5 = base64.b64encode(hashlib.md5(req.data).digest()).decode()
            return _Resp(json.dumps({"name": "x", "size": str(len(req.data)), "md5Hash": md5}).encode())

        rec = B.run(ledger=db, now=now, env={}, metadata=_metadata, opener=opener)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(sent["url"]).query)
        ok("the bucket comes from the metadata project id (no project id in the repo)",
           rec["bucket"] == "quiver-state-test-project" and "/b/quiver-state-test-project/o" in sent["url"], rec)
        ok("the object name is ledger/<UTC time>.db.gz",
           rec["object"] == "ledger/2026-10-13T221507Z.db.gz" and q["name"] == [rec["object"]], rec["object"])
        ok("the upload is create-only (never overwrites an older copy)",
           q["ifGenerationMatch"] == ["0"] and q["uploadType"] == ["media"] and sent["method"] == "POST", q)
        ok("the upload carries the service-account token", sent["auth"] == "Bearer tok-123", sent["auth"])
        restored = Path(d) / "restored.db"
        restored.write_bytes(gzip.decompress(sent["data"]))
        con = sqlite3.connect(str(restored))
        ok("the uploaded copy restores to a sound database",
           con.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        ok("the restored copy holds the rows of the source",
           con.execute("SELECT COUNT(*) FROM day_baseline").fetchone()[0] == 5
           and con.execute("SELECT amount FROM cash_flows").fetchall() == [(800.0,)])
        con.close()
        ok("the log record reports the row counts and both sizes",
           rec["ok"] is True and rec["rows"]["day_baseline"] == 5 and rec["integrity"] == "ok"
           and rec["gz_bytes"] == len(sent["data"]) and rec["db_bytes"] > rec["gz_bytes"], rec)
        ok("the snapshot file is removed from state/tmp", list((db.parent / "tmp").glob("backup_*")) == [])
        ok("the job did not write to the source ledger (still 5 baselines)",
           len(Ledger(db).baseline_equity_series()) == 5)

        rec2 = B.run(ledger=db, now=now, env={"QUIVER_BACKUP_BUCKET": "my-bucket"},
                     metadata=_metadata, opener=opener)
        ok("QUIVER_BACKUP_BUCKET overrides the default bucket", rec2["bucket"] == "my-bucket", rec2["bucket"])

        # A corrupt upload (Cloud Storage reports another MD5) is a failure, not a quiet pass.
        def bad_opener(req, timeout=None):
            return _Resp(json.dumps({"size": str(len(req.data)), "md5Hash": "AAAA"}).encode())
        try:
            B.run(ledger=db, now=now, env={}, metadata=_metadata, opener=bad_opener)
            ok("an MD5 mismatch raises", False)
        except RuntimeError as e:
            ok("an MD5 mismatch raises", "upload verify failed" in str(e), e)

        # main(): a failure logs, pages once, exits 1 and never raises.
        pages, logs = [], []
        _orig = (B.run, B._page, B._log)
        try:
            B._page = lambda detail: pages.append(detail)
            B._log = lambda obj: logs.append(obj)
            B.run = lambda **k: (_ for _ in ()).throw(FileNotFoundError("ledger not found: x"))
            rc = B.main()
            ok("main: a failure exits 1, logs ok:false and pages once",
               rc == 1 and logs[-1]["ok"] is False and "ledger not found" in logs[-1]["error"]
               and len(pages) == 1, (rc, logs, pages))
            B.run = lambda **k: {"event": "ledger_backup", "ok": True}
            ok("main: a success exits 0 with no page", B.main() == 0 and len(pages) == 1)
        finally:
            B.run, B._page, B._log = _orig
        try:
            B.snapshot(Path(d) / "missing.db", Path(d) / "out.db")
            ok("a missing ledger raises (never uploads an empty file)", False)
        except FileNotFoundError:
            ok("a missing ledger raises (never uploads an empty file)", True)

    print(f"\nBACKUP: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
