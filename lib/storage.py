"""Bounded local retention for bulky run artifacts, with a pluggable archiver.

Housekeeping/observability only — like ``lib/notify``, everything here is
BEST-EFFORT and must NEVER raise into a trading tick. The decision/outcome state
of record lives in the SQLite ledger; the files swept here (LLM reasoning
transcripts, per-ticker analysis dumps, framework results/cache) are
reconstructable audit logs, safe to age out.

The ``Archiver`` interface is the seam for offloading to object storage (e.g.
S3) before deletion once the local tree grows. The S3 backend is intentionally
NOT implemented yet (interface only): ``storage.archive.enabled`` defaults to
``false``, and ``get_archiver`` degrades safely to local-only pruning (with a
logged warning) if archival is switched on before a backend exists.

The retention DECISION is a pure function (``select_for_prune``) so it is
unit-tested with no filesystem; ``prune_dir`` is the best-effort I/O wrapper.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


# --- pure selection (unit-tested; no filesystem, no clock) ------------------

def select_for_prune(
    entries: Sequence[Tuple[str, float]],
    keep_days: float,
    now_ts: float,
) -> List[str]:
    """Return paths whose mtime is strictly older than the retention window.

    ``entries`` are ``(path, mtime_epoch_seconds)`` pairs. A file whose mtime is
    exactly at the cutoff is KEPT (only strictly-older files drop), so the
    boundary is deterministic. ``keep_days <= 0`` disables pruning (returns []).
    """
    if keep_days is None or keep_days <= 0:
        return []
    cutoff = now_ts - keep_days * 86400.0
    return [p for (p, mtime) in entries if mtime < cutoff]


# --- archiver interface -----------------------------------------------------

class Archiver:
    """Move a file to long-term storage before it's pruned. Returns a URI/None."""

    def archive(self, path: Path) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError


class LocalArchiver(Archiver):
    """Default no-op archiver: nothing is offloaded; files just age out locally."""

    def archive(self, path: Path) -> Optional[str]:
        return str(path)


class S3Archiver(Archiver):
    """Deferred — interface placeholder only (see plan NOT-in-scope).

    Intentionally not implemented. Constructing it raises so a premature
    ``archive.enabled: true`` is caught by ``get_archiver`` and degraded to
    local-only retention rather than silently doing nothing.
    """

    def __init__(self, bucket: str, prefix: str = ""):
        raise NotImplementedError(
            "S3 archival is not implemented yet (interface only). Keep "
            "storage.archive.enabled=false until the S3 backend lands."
        )


def get_archiver(storage) -> Archiver:
    """Pick an archiver from a StorageConfig, degrading safely to local.

    Never raises: if archival is enabled but its backend isn't available, log to
    stderr and fall back to ``LocalArchiver`` so retention still runs.
    """
    try:
        if getattr(storage, "archive_enabled", False):
            backend = getattr(storage, "archive_backend", "s3")
            if backend == "s3":
                return S3Archiver(
                    getattr(storage, "archive_bucket", ""),
                    getattr(storage, "archive_prefix", ""),
                )
            print(f"[storage] unknown archive backend {backend!r}; using local",
                  file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — archival setup must never block a tick
        print(f"[storage] archiver unavailable ({e}); using local", file=sys.stderr)
    return LocalArchiver()


# --- retention sweep (best-effort; never raises) ----------------------------

def prune_dir(
    directory,
    keep_days: float,
    *,
    archiver: Optional[Archiver] = None,
    now_ts: Optional[float] = None,
    recursive: bool = True,
) -> dict:
    """Archive-then-delete files older than ``keep_days`` under ``directory``.

    Returns a summary ``{dir, scanned, pruned, archived, errors}``. Best-effort:
    a missing dir is a no-op, and any per-file failure is counted and skipped,
    never raised. With a ``LocalArchiver`` (default) nothing is offloaded —
    old files are simply deleted.
    """
    d = Path(directory)
    summary = {"dir": str(d), "scanned": 0, "pruned": 0, "archived": 0, "errors": 0}
    if not d.exists() or keep_days is None or keep_days <= 0:
        return summary
    now_ts = time.time() if now_ts is None else now_ts
    arch = archiver or LocalArchiver()
    offloads = not isinstance(arch, LocalArchiver)

    entries: List[Tuple[str, float]] = []
    walker = d.rglob("*") if recursive else d.glob("*")
    for f in walker:
        try:
            if not f.is_file():
                continue
            entries.append((str(f), f.stat().st_mtime))
            summary["scanned"] += 1
        except Exception:  # noqa: BLE001
            summary["errors"] += 1

    for p in select_for_prune(entries, keep_days, now_ts):
        try:
            if offloads and arch.archive(Path(p)):
                summary["archived"] += 1
            Path(p).unlink()
            summary["pruned"] += 1
        except Exception:  # noqa: BLE001
            summary["errors"] += 1
    return summary
