#!/usr/bin/env python3
"""Deterministic e2e for the named run locks and the reflect scoring (no network, no broker, no LLM).

  * LOCK  ``lib/runlock.run_lock(led, name, holder, now_iso, ttl_seconds)`` keeps one row per
          runner name in ``run_locks`` (plan §14.3 ``runlock.py`` row, §3.5). The monitor, the
          worker and the broker never block each other. Two runs of the same name never
          overlap. A holder from another boot or with no live process loses the lock at once.
          The TTL is a second limit, for a live holder that is stuck.
  * T6    ``tick.py reflect`` writes ALPHA (the excess over the benchmark) and the realized P&L,
          and the scorecard then grades on skill. The untrusted CLI path ignores a supplied
          benchmark.

The daily-loss case (T7) moved to the ``exec-check`` halt tests of plan §18.
Plain asserts through ``Results``; exits non-zero on any failure.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
os.environ.setdefault("RH_ACCOUNT_NUMBER", "TEST1234")

from desk_fakes import Results  # noqa: E402
from lib import runlock  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

r = Results("e2e-safety")

T0 = "2026-08-05T14:00:00Z"


def _led(d) -> Ledger:
    return Ledger(Path(d) / f"lock_{os.urandom(4).hex()}.db")


def _dead_pid() -> int:
    """The PID of a process that has exited (a real one, not a made-up number)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


_HELD = []  # entered run_lock contexts: a dropped generator would run its `finally` and release


def _acquire(led, name, holder, now_iso, ttl):
    """True when ``run_lock`` takes the lock, False on RunLockError. The lock is left
    HELD on success (the context is entered and kept in ``_HELD``) so a later call sees it."""
    cm = runlock.run_lock(led, name, holder, now_iso, ttl)
    try:
        cm.__enter__()
    except runlock.RunLockError:
        return False
    _HELD.append(cm)
    return True


def _drop():
    """Exit every held context before its temp ledger is deleted."""
    while _HELD:
        _HELD.pop().__exit__(None, None, None)


def _ttl_constant(runner: str):
    """``LOCK_TTL_S`` of a runner, read from its source with ``ast`` (the runners import
    the SDK, which a laptop venv does not need to hold for this check)."""
    tree = ast.parse((REPO / "deploy" / "runner" / runner).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "LOCK_TTL_S" for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def _lock_name(runner: str):
    """The first string argument of the ``runlock.run_lock(...)`` call in a runner."""
    tree = ast.parse((REPO / "deploy" / "runner" / runner).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run_lock":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                return node.args[1].value
    return None


ME = runlock.holder_id()                 # this process: same boot, alive
PARENT = runlock.holder_id(os.getppid())  # the parent shell: same boot, alive

# --- LOCK 1: acquire, refuse a second live holder, release on exit ---------------------
with tempfile.TemporaryDirectory() as d:
    led = _led(d)
    with runlock.run_lock(led, "monitor", ME, T0, 2400):
        row = led.named_lock("monitor")
        r.ok("LOCK the first acquire writes one row for the name", row is not None and row["holder"] == ME, row)
        r.ok("LOCK a second LIVE holder of the same name is refused within the TTL",
             _acquire(led, "monitor", PARENT, "2026-08-05T14:05:00Z", 2400) is False)
        r.ok("LOCK the refused call leaves the first holder in place",
             led.named_lock("monitor")["holder"] == ME, led.named_lock("monitor"))
    r.ok("LOCK the lock is released when the run ends", led.named_lock("monitor") is None,
         led.named_lock("monitor"))
    r.ok("LOCK after the release the next run takes it",
         _acquire(led, "monitor", PARENT, "2026-08-05T14:06:00Z", 2400) is True)
    _drop()

# --- LOCK 2: one lock per name: monitor, worker and broker never block each other ------
with tempfile.TemporaryDirectory() as d:
    led = _led(d)
    r.ok("LOCK monitor taken", _acquire(led, "monitor", ME, T0, 2400) is True)
    r.ok("LOCK broker taken while the monitor holds its lock (G1)",
         _acquire(led, "broker", PARENT, T0, 3600) is True)
    r.ok("LOCK worker taken while monitor and broker hold theirs",
         _acquire(led, "worker", PARENT, T0, 28800) is True)
    r.ok("LOCK three rows, one per name",
         [led.named_lock(n)["holder"] for n in ("monitor", "broker", "worker")] == [ME, PARENT, PARENT])
    r.ok("LOCK a second broker run is still refused", _acquire(led, "broker", ME, T0, 3600) is False)
    _drop()

# --- LOCK 3: the TTL steals a lock from a live but stuck holder ------------------------
with tempfile.TemporaryDirectory() as d:
    led = _led(d)
    _acquire(led, "monitor", PARENT, "2026-08-05T10:00:00-04:00", 2400)
    # 14:39:59Z is 2,399 s after 14:00Z (the ET stamp): still inside the TTL. The ET and
    # UTC stamps compare as instants, not as strings.
    r.ok("LOCK a live holder 1 s inside the TTL keeps the lock (ET vs UTC stamps compare)",
         _acquire(led, "monitor", ME, "2026-08-05T14:39:59Z", 2400) is False)
    r.ok("LOCK a live holder past the TTL loses the lock",
         _acquire(led, "monitor", ME, "2026-08-05T14:40:00Z", 2400) is True)
    r.ok("LOCK the stealer is the holder now", led.named_lock("monitor")["holder"] == ME)
    # The stuck holder ends later and releases: it must not delete the stealer's row.
    led.release_named_lock("monitor", PARENT)
    r.ok("LOCK an old holder's release does not delete the new holder's row",
         (led.named_lock("monitor") or {}).get("holder") == ME, led.named_lock("monitor"))
    _drop()

# --- LOCK 4: a dead holder loses the lock at once (a SIGKILL skips `finally`) ----------
with tempfile.TemporaryDirectory() as d:
    led = _led(d)
    dead = runlock.holder_id(_dead_pid())
    r.ok("LOCK holder_dead: a PID with no process is dead", runlock.holder_dead(dead) is True, dead)
    r.ok("LOCK holder_dead: this process is alive", runlock.holder_dead(ME) is False)
    _acquire(led, "worker", dead, T0, 28800)
    r.ok("LOCK a holder with no live process loses the lock within the TTL",
         _acquire(led, "worker", ME, "2026-08-05T14:00:05Z", 28800) is True)
    other_boot = "another-boot-id:" + str(os.getpid())
    r.ok("LOCK holder_dead: a holder from another boot is dead", runlock.holder_dead(other_boot) is True)
    _acquire(led, "broker", other_boot, T0, 3600)
    r.ok("LOCK a holder from another boot loses the lock at once",
         _acquire(led, "broker", ME, "2026-08-05T14:00:05Z", 3600) is True)
    # An unknown holder format is never 'dead': only the TTL can free it.
    r.ok("LOCK holder_dead: an unknown holder format is not dead", runlock.holder_dead("A") is False)
    _acquire(led, "monitor", "A", T0, 2400)
    r.ok("LOCK an unknown-format holder keeps the lock within the TTL",
         _acquire(led, "monitor", ME, "2026-08-05T14:10:00Z", 2400) is False)
    _drop()

# --- LOCK 5: the lock is released when the run raises ----------------------------------
with tempfile.TemporaryDirectory() as d:
    led = _led(d)
    try:
        with runlock.run_lock(led, "broker", ME, T0, 3600):
            raise ValueError("a step failed")
    except ValueError:
        pass
    r.ok("LOCK an exception inside the run still releases the lock", led.named_lock("broker") is None)
    r.raises("LOCK RunLockError names the lock", lambda: (
        _acquire(led, "broker", ME, T0, 3600),
        runlock.run_lock(led, "broker", PARENT, T0, 3600).__enter__()), runlock.RunLockError, "broker")
    _drop()

# --- LOCK 6: the acquire is atomic (two runners never both see a free lock) ------------
with tempfile.TemporaryDirectory() as d:
    led = _led(d)
    races = []
    for i in range(10):
        name = f"race{i}"
        wins = []
        gate = threading.Barrier(2)

        def _go(holder, _name=name, _wins=wins, _gate=gate):
            _gate.wait()
            _wins.append(led.try_acquire_named_lock(_name, holder, T0, ttl_seconds=2400,
                                                    holder_dead=runlock.holder_dead))
        ts = [threading.Thread(target=_go, args=(h,)) for h in (ME, PARENT)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        races.append(sorted(wins))
    r.ok("LOCK a race of two live runners: exactly one wins, every round",
         all(w == [False, True] for w in races), races)
    _drop()

# --- LOCK 7: each runner takes its own name with its unit's time limit (§14.3) ---------
for runner, name, ttl in (("run_monitor.py", "monitor", 2400), ("run_worker.py", "worker", 28800),
                          ("run_broker.py", "broker", 3600)):
    r.eq(f"LOCK {runner} locks the name {name!r}", _lock_name(runner), name)
    r.eq(f"LOCK {runner} LOCK_TTL_S is {ttl}", _ttl_constant(runner), ttl)
r.ok("LOCK the holder id is <boot_id>:<pid>", ME == f"{runlock.boot_id()}:{os.getpid()}", ME)

# --- T6: reflect writes ALPHA (skill) + realized P&L; the scorecard uses them -----------
import tick  # noqa: E402
from lib import memory  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    tdb = Path(d) / "reflect.db"
    led = Ledger(tdb)
    did = led.record_decision(trade_date="2026-06-13", ticker="SMH",
                              decided_at="2026-06-13T10:00:00-04:00", signal="Buy", intent="buy",
                              decision_price=100.0, basis="compute")
    _saved_db = tick.LEDGER_DB
    tick.LEDGER_DB = tdb
    try:
        rin = Path(d) / "reflect_input.json"
        rin.write_text(json.dumps({"resolutions": [
            {"decision_id": did, "price_now": 110.0, "benchmark_return": 0.04, "realized_pnl": 12.5}]}))
        # trust_input_benchmark: an IN-PROCESS Python caller that supplies its own benchmark.
        # The CLI has no such flag, so the untrusted path is the one the runners use.
        res = tick.cmd_reflect(types.SimpleNamespace(input=str(rin), trust_input_benchmark=True))
        r.ok("T6 reflect resolves the outcome", res.get("resolved") == 1, res)
        row = led.decisions_with_outcomes("SMH")[0]
        r.ok("T6 directional_return computed (decision_price->price_now = +10%)",
             abs((row.get("directional_return") or 0) - 0.10) < 1e-6, row.get("directional_return"))
        r.ok("T6 ALPHA = excess vs benchmark (0.10 - 0.04 = 0.06)",
             abs((row.get("alpha") or 0) - 0.06) < 1e-6, row.get("alpha"))
        r.ok("T6 realized P&L leg closed (12.5 written)",
             abs((row.get("realized_pnl") or 0) - 12.5) < 1e-6, row.get("realized_pnl"))
        sc = memory.build_scorecard("SMH", led.decisions_with_outcomes("SMH"))
        r.ok("T6 scorecard grades on skill (excess-vs-benchmark)", "excess-vs-benchmark" in sc, sc)
        r.ok("T6 scorecard shows realized P&L", "Realized P&L" in sc and "$+12.50" in sc, sc)

        # T6b: the UNTRUSTED path ignores a supplied benchmark_return and says so.
        res2 = tick.cmd_reflect(types.SimpleNamespace(input=str(rin)))
        r.ok("T6b untrusted reflect still resolves", res2.get("resolved") == 1, res2)
        r.ok("T6b a supplied benchmark_return is IGNORED (alpha NULL)",
             led.decisions_with_outcomes("SMH")[0].get("alpha") is None,
             led.decisions_with_outcomes("SMH")[0].get("alpha"))
        r.ok("T6b the ignore is reported, not silent",
             res2.get("ignored_benchmark_returns") == 1, res2.get("ignored_benchmark_returns"))
        import sqlite3 as _sq3
        _c = _sq3.connect(str(tdb))
        _br = _c.execute("SELECT benchmark_return FROM outcomes WHERE decision_id=?", (did,)).fetchone()[0]
        _c.close()
        r.ok("T6b benchmark_return column is NULL on the untrusted path", _br is None, _br)
    finally:
        tick.LEDGER_DB = _saved_db

raise SystemExit(r.done())
