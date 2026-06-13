"""Single-run mutex: a context manager over the ledger run_lock table.

Belt-and-suspenders with serial scheduling — guarantees one tick writes the SQLite
ledger at a time so two overlapping wakes can never race the dedup/ref_id machinery
and double-place. Steals a STALE lock (held past the TTL, which exceeds any real
tick wall-clock) so a crashed holder cannot wedge the bot forever.
"""

from __future__ import annotations

from contextlib import contextmanager


class RunLockError(RuntimeError):
    """Raised when another tick already holds the run lock."""


@contextmanager
def run_lock(led, holder: str, now_iso: str, ttl_seconds: int = 3600):
    """Acquire-or-raise; always releases on exit (only if WE still hold it)."""
    if not led.try_acquire_run_lock(holder, now_iso, ttl_seconds=ttl_seconds):
        raise RunLockError("another tick holds the run lock — skipping this wake")
    try:
        yield
    finally:
        led.release_run_lock(holder)
