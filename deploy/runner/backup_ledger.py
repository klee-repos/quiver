#!/usr/bin/env python3
"""Ops job: copy the ledger to Cloud Storage (deploy/quiver-backup.timer fires this).

The ledger (`state/ledger.db`) is the only copy of the decisions, the outcomes, the orders
and the targets. It is on one zonal disk. This job makes one dated copy per run:

  1. Take a consistent snapshot with the SQLite online backup API. Do NOT `cp` a live
     SQLite file: a write during the copy gives a corrupt file. The source opens read-only.
  2. Run `PRAGMA integrity_check` on the snapshot. A bad snapshot is not uploaded.
  3. Gzip it and upload it as `ledger/<UTC time>.db.gz`. The upload is create-only
     (`ifGenerationMatch=0`), so a run never overwrites an older copy.
  4. Compare the MD5 that Cloud Storage reports with the local MD5.

The bucket is `QUIVER_BACKUP_BUCKET`, else `quiver-state-<project id>` (the project id comes
from the metadata server, so no project id is in this repo). The token is the box service
account token from the metadata server. Stdlib only: no gcloud, no new dependency.

This job never trades and never writes to the ledger. A failure logs one line, sends one
Telegram page (best-effort) and exits 1. It does not stop a tick.

Restore:
  gcloud storage cp gs://<bucket>/ledger/<name>.db.gz - | gunzip > ledger.db
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
LEDGER = Path(os.environ.get("QUIVER_LEDGER", str(_REPO / "state" / "ledger.db")))
LOG = os.environ.get("QUIVER_BACKUP_LOG", str(_REPO / "logs" / "backup.log"))
_METADATA = "http://metadata.google.internal/computeMetadata/v1/"
_UPLOAD = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{query}"
# The tables a restore must hold. The snapshot reports their row counts in the log line.
_COUNT_TABLES = ("decisions", "outcomes", "orders", "proposals", "documents", "target_portfolio",
                 "day_baseline", "strategy_change_log")


def _log(obj: dict) -> None:
    line = json.dumps(obj)
    print(line, flush=True)
    try:
        Path(LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # the log is best-effort; it never fails the run


def snapshot(src: Path, dst: Path) -> dict:
    """Write a consistent copy of `src` to `dst`. Return its row counts.

    Raises when the source is missing or the copy fails `integrity_check`."""
    if not src.is_file():
        raise FileNotFoundError(f"ledger not found: {src}")
    ro = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dst))
        try:
            ro.backup(out)
            verdict = out.execute("PRAGMA integrity_check").fetchone()[0]
            if verdict != "ok":
                raise RuntimeError(f"integrity_check: {verdict}")
            tables = {r[0] for r in out.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts = {t: out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in _COUNT_TABLES if t in tables}
        finally:
            out.close()
    finally:
        ro.close()
    return {"integrity": "ok", "rows": counts}


def _metadata(path: str, opener=urllib.request.urlopen) -> str:
    req = urllib.request.Request(_METADATA + path, headers={"Metadata-Flavor": "Google"})
    with opener(req, timeout=10) as r:
        return r.read().decode("utf-8")


def bucket_name(env=None, metadata=_metadata) -> str:
    env = os.environ if env is None else env
    b = str(env.get("QUIVER_BACKUP_BUCKET") or "").strip()
    return b or f"quiver-state-{metadata('project/project-id').strip()}"


def upload(bucket: str, name: str, data: bytes, token: str,
           opener=urllib.request.urlopen) -> dict:
    """Create the object `name`. Return the object resource (it holds `md5Hash`, `size`)."""
    query = urllib.parse.urlencode({"uploadType": "media", "name": name, "ifGenerationMatch": "0"})
    req = urllib.request.Request(
        _UPLOAD.format(bucket=urllib.parse.quote(bucket, safe=""), query=query),
        data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/gzip"})
    with opener(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def run(*, ledger: Path = LEDGER, now: datetime | None = None, env=None,
        metadata=_metadata, opener=urllib.request.urlopen) -> dict:
    """One backup. Returns the log record. Raises on any failure (main() catches it)."""
    now = now or datetime.now(timezone.utc)
    name = f"ledger/{now.strftime('%Y-%m-%dT%H%M%SZ')}.db.gz"
    tmp = ledger.parent / "tmp" / f"backup_{os.getpid()}.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        snap = snapshot(ledger, tmp)
        raw = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    data = gzip.compress(raw, compresslevel=6, mtime=0)
    bucket = bucket_name(env, metadata)
    token = json.loads(metadata("instance/service-accounts/default/token"))["access_token"]
    obj = upload(bucket, name, data, token, opener)
    want = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
    if obj.get("md5Hash") != want or int(obj.get("size") or -1) != len(data):
        raise RuntimeError(f"upload verify failed: md5 {obj.get('md5Hash')} != {want} "
                           f"or size {obj.get('size')} != {len(data)}")
    return {"event": "ledger_backup", "ok": True, "object": name, "bucket": bucket,
            "db_bytes": len(raw), "gz_bytes": len(data), **snap}


def _page(detail: str) -> None:
    """One Telegram page for a failed backup. Best-effort: it never raises."""
    try:
        sys.path.insert(0, str(_REPO))
        from lib import telegram
        token, chats = telegram.resolve_env()
        if not token or not chats:
            return
        telegram.send_message(
            token=token, chat_ids=chats, disable_notification=True,
            text=("<b>⚠️ Quiver ledger backup failed.</b> Tonight's copy of the ledger did not "
                  "reach Cloud Storage, so the newest backup is more than a day old. Trading is "
                  f"not affected. Detail: <code>{html.escape(detail[:300])}</code>"))
    except Exception:  # noqa: BLE001 — the page is best-effort
        pass


def main() -> int:
    try:
        _log(run())
        return 0
    except urllib.error.HTTPError as e:
        detail = f"HTTPError {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:  # noqa: BLE001 — an ops job reports, it does not crash
        detail = f"{type(e).__name__}: {e}"
    _log({"event": "ledger_backup", "ok": False, "error": detail[:400]})
    _page(detail)
    return 1


if __name__ == "__main__":
    sys.exit(main())
