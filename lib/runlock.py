"""Named run locks: one lock per runner name over the ledger ``run_locks`` table.

The monitor, the worker and the broker each take their own lock (``monitor``,
``worker``, ``broker``), so they never block each other. Two runs of the SAME runner
never overlap. The holder is ``<boot_id>:<pid>``. A caller takes a lock at once when
its holder ran in an earlier boot or its process no longer exists: a SIGKILL skips
``finally``, so a dead holder must not wedge a runner until the TTL. The TTL is a second
limit, for a live holder that is stuck.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


class RunLockError(RuntimeError):
    """Raised when another live run holds the named lock."""


def boot_id() -> str:
    """This boot's id from the Linux kernel. Other systems (the laptop tests) get a
    fixed value, so the PID check alone decides there."""
    try:
        with open(_BOOT_ID_PATH, encoding="utf-8") as f:
            return f.read().strip() or "no-boot-id"
    except OSError:
        return "no-boot-id"


def holder_id(pid: int | None = None) -> str:
    return f"{boot_id()}:{os.getpid() if pid is None else int(pid)}"


def pid_alive(pid: int) -> bool:
    """True when a process with this PID exists (a PermissionError still means it exists)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def holder_dead(holder: str, *, this_boot: str | None = None, alive=pid_alive) -> bool:
    """True when ``holder`` cannot still hold a lock: another boot, or no such process.
    A holder string in an unknown format is never dead (the TTL then decides)."""
    boot, _, pid = str(holder or "").rpartition(":")
    if not boot or not pid.isdigit():
        return False
    if boot != (this_boot if this_boot is not None else boot_id()):
        return True
    return not alive(int(pid))


@contextmanager
def run_lock(led, name: str, holder: str, now_iso: str, ttl_seconds: int):
    """Acquire the lock ``name`` or raise ``RunLockError``. Always releases on exit
    (only while ``holder`` still holds it)."""
    if not led.try_acquire_named_lock(name, holder, now_iso, ttl_seconds=ttl_seconds,
                                      holder_dead=holder_dead):
        raise RunLockError(f"another run holds the {name} lock")
    try:
        yield
    finally:
        led.release_named_lock(name, holder)
