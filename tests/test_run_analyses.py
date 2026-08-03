#!/usr/bin/env python3
"""Offline spec for scripts/run_analyses.py (the STEP 3 blocking fan-out) +
Ledger.count_decisions (the silent-noop guard's signal). No network / no GLM:
analyze.py is stubbed via QUIVER_ANALYZE_SCRIPT, so this is fast and deterministic.

Plain asserts, prints "<n> checks passed, <m> failed", exits non-zero on any failure
(matches tests/run_e2e.sh's summary grep)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# A stub "analyzer" that mimics analyze.py's stdout contract (chatter on stderr, one
# JSON line on stdout) and branches on the ticker to exercise every failure path.
_STUB = '''\
import json, sys, time
t = sys.argv[1]
sys.stderr.write("framework chatter for %s\\n" % t)
if t == "NOOUT":      # non-zero exit, nothing on stdout
    sys.exit(1)
if t == "BADJSON":    # last stdout line is not JSON
    print("this is not json")
    sys.exit(0)
if t == "SLOW":       # sleeps longer than a tiny timeout
    time.sleep(5)
    print(json.dumps({"ticker": t, "signal": "Buy"}))
    sys.exit(0)
# normal: valid JSON, but deliberately NO "ticker" key (tests default injection)
print(json.dumps({"signal": "Buy", "position_pct": 0.1}))
'''

# A second stub for the PGSWEEP arms: it spawns a real GRANDCHILD (mirroring analyze.py:506,
# which spawns `node decide.mjs`) and then either hangs (so the OUTER timeout fires) or exits 0
# leaving the grandchild behind (the case a kill-only-on-timeout fix would miss entirely).
_LEAK_STUB = '''\
import json, os, subprocess, sys, time
t = sys.argv[1]
# The grandchild must PROVABLY NEVER WRITE. A writing grandchild dies of EPIPE/SIGPIPE by
# itself when this stub is killed, and both arms below would then pass with the fix removed.
# Explicit DEVNULL on all three fds also stops it holding the outer capture pipe open, which
# would silently convert the SUCCESS arm into a timeout arm.
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(45)"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
open(os.environ["QUIVER_TEST_PIDFILE"], "w").write(str(g.pid))
if t == "LEAKOK":
    # SUCCESS path: valid analysis, exit 0 — deliberately orphaning the grandchild.
    print(json.dumps({"signal": "Buy", "pgid": os.getpgid(0), "argv": sys.argv}))
    sys.exit(0)
time.sleep(45)   # LEAKHANG: block so the OUTER timeout fires
'''

_PASS = 0
_FAIL = 0
_SPAWNED = []          # every pid this file created, killed unconditionally at exit


def _alive(pid):
    """Does `pid` exist? NOTE: also True for a ZOMBIE (dead but not yet reaped) — which is
    exactly why callers POLL rather than sleeping a fixed interval. PermissionError is NOT
    'gone': it means the pid was recycled onto another uid, so the verdict is unusable and
    must go red loudly rather than pass quietly."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_gone(pid, timeout=5.0):
    """Poll until `pid` leaves the process table. Measured sweep latency is ~6-30ms while a
    LEAKED grandchild sleeps 45s, so the two populations are three orders of magnitude apart
    and this is deterministic — unlike a fixed sleep, which is a flake generator against an
    unbounded quantity (SIGKILL delivery + reap + reparent-to-init reap) on a loaded box."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.01)
    return False


def _arm(name, fn):
    """Run one process-level arm. An exception becomes a FAILED check instead of an uncaught
    raise, which would kill the '<n> checks passed' summary line that tests/run_e2e.sh greps
    and silently skip every check after it."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — that is the entire point of this wrapper
        check(False, f"{name}: raised {type(e).__name__}: {e}")


def check(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {msg}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="quiver_runanalyses_"))
    stub = tmp / "stub_analyze.py"
    stub.write_text(_STUB)
    os.environ["QUIVER_ANALYZE_SCRIPT"] = str(stub)
    # PYTHON defaults to sys.executable; ANALYZE_SCRIPT read at import -> set env first.
    sys.path.insert(0, str(_REPO / "scripts"))
    import run_analyses as ra  # noqa: E402

    # --- empty input ---
    check(ra.run([]) == [], "empty ticker list -> empty result")

    # --- happy path + ordering + ERROR synthesis (parallel) ---
    res = ra.run(["AAPL", "MSFT", "BADJSON", "NOOUT"], concurrency=4, timeout=30)
    check(len(res) == 4, f"4 inputs -> 4 results (got {len(res)})")
    check(res[0].get("ticker") == "AAPL" and res[0].get("signal") == "Buy",
          f"order preserved + ticker injected for AAPL (got {res[0]})")
    check(res[1].get("ticker") == "MSFT" and res[1].get("signal") == "Buy",
          f"order preserved for MSFT (got {res[1]})")
    check(res[2].get("signal") == "ERROR" and "not JSON" in res[2].get("error", ""),
          f"non-JSON output -> ERROR (got {res[2]})")
    check(res[3].get("signal") == "ERROR" and "no stdout" in res[3].get("error", ""),
          f"no-stdout/non-zero -> ERROR (got {res[3]})")
    # Every element is a dict with a ticker (the contract plan consumes).
    check(all(isinstance(r, dict) and r.get("ticker") for r in res),
          "every result is a dict carrying its ticker")

    # --- result is JSON-serializable + round-trips (run_tick.py writes analyses.json) ---
    blob = json.dumps(res)
    back = json.loads(blob)
    check(isinstance(back, list) and len(back) == 4 and back[0]["ticker"] == "AAPL",
          "run() output round-trips through json (as written to state/tmp/analyses.json)")

    # --- per-ticker timeout becomes an ERROR datum, not a crash ---
    slow = ra.run(["SLOW"], concurrency=1, timeout=1)
    check(len(slow) == 1 and slow[0].get("signal") == "ERROR"
          and "timed out" in slow[0].get("error", ""),
          f"timeout -> ERROR datum (got {slow})")

    # --- analyze_one default-injects the ticker even when analyze.py omits it ---
    one = ra.analyze_one("NVDA", timeout=30)
    check(one.get("ticker") == "NVDA" and one.get("signal") == "Buy",
          f"analyze_one injects ticker (got {one})")

    # --- Ledger.count_decisions / count_ticker_actions (the silent-noop guard's signal) ---
    sys.path.insert(0, str(_REPO))
    from lib.ledger import Ledger  # noqa: E402
    led = Ledger(tmp / "ledger.db")
    check(led.count_decisions("2026-06-17") == 0, "fresh day -> 0 decisions")
    check(led.count_ticker_actions("2026-06-17") == 0, "fresh day -> 0 ticker_actions")
    led.record_decision(trade_date="2026-06-17", ticker="AAPL",
                        decided_at="2026-06-17T10:00:00-04:00", signal="Buy", intent="buy")
    led.record_decision(trade_date="2026-06-17", ticker="MSFT",
                        decided_at="2026-06-17T10:00:00-04:00", signal="Hold", intent="hold")
    check(led.count_decisions("2026-06-17") == 2, "two decisions recorded -> count 2")
    check(led.count_decisions("2026-06-16") == 0, "other day unaffected -> 0")

    # The GLM-outage case the guard must NOT false-trip on: an ERROR analysis records
    # a ticker_action but NO decision (mirrors tick.py:418). On such a day the guard sees
    # 0 decisions but >0 actions -> sum > 0 -> no false page.
    led2 = Ledger(tmp / "ledger2.db")
    led2.record_action("2026-06-17", "NVDA", signal="ERROR", intent="skip",
                       status="error", detail="glm down", now_iso="2026-06-17T10:00:00-04:00")
    check(led2.count_decisions("2026-06-17") == 0, "all-ERROR day -> 0 decisions")
    check(led2.count_ticker_actions("2026-06-17") == 1, "all-ERROR day -> 1 error action")
    check(led2.count_decisions("2026-06-17") + led2.count_ticker_actions("2026-06-17") > 0,
          "all-ERROR day -> combined guard sum > 0 (no false page)")

    # --- run_tick._is_silent_noop (the run-scoped delta guard, pure) ---
    sys.path.insert(0, str(_REPO / "deploy" / "runner"))
    import run_tick as rt  # noqa: E402
    P = ["AAPL", "MSFT"]
    check(rt._is_silent_noop(P, 0, 0) is True,
          "proceeded, pending, 0 new rows -> silent noop (the 2026-06-17 bug)")
    check(rt._is_silent_noop(P, 5, 17) is False,
          "all-ERROR day: delta>0 (error actions added) -> NOT a noop")
    check(rt._is_silent_noop(P, 5, 8) is False,
          "rows added this run -> NOT a noop")
    # Intraday false-negative the review flagged: tick 2 does nothing, but tick 1 already
    # wrote rows today. Day-absolute counts would miss it; the DELTA catches it.
    check(rt._is_silent_noop(P, 5, 5) is True,
          "intraday: earlier tick wrote 5, this run added 0 -> silent noop (delta-scoped)")
    check(rt._is_silent_noop([], 0, 0) is False, "no pending -> never a noop")
    check(rt._is_silent_noop(P, None, 0) is False, "unknown before snapshot -> never false-trip")
    check(rt._is_silent_noop(P, 0, None) is False, "unknown after snapshot -> never false-trip")

    # --- run_tick._orchestrator_reason (pull the real failure message out of stream-json) ---
    # A spend-limit failure: the token-count tail is opaque, but the result event carries the text.
    spend = (
        '{"type":"system","subtype":"init"}\n'
        '{"type":"assistant","message":{"content":[{"type":"text",'
        '"text":"You\'ve hit your monthly spend limit \\u00b7 raise it at claude.ai/settings/usage"}]}}\n'
        '{"type":"result","subtype":"error","is_error":true,'
        '"result":"You\'ve hit your monthly spend limit \\u00b7 raise it at claude.ai/settings/usage",'
        '"usage":{"output_tokens":0},"modelUsage":{}}\n')
    check("monthly spend limit" in (rt._orchestrator_reason(spend) or ""),
          "spend-limit failure -> reason names the spend limit (not the token-count tail)")
    check("claude.ai/settings/usage" in (rt._orchestrator_reason(spend) or ""),
          "spend-limit failure -> reason keeps the actionable URL")
    # result event preferred over an earlier assistant block; whitespace collapsed to one line.
    both = ('{"type":"assistant","message":{"content":[{"type":"text","text":"working..."}]}}\n'
            '{"type":"result","result":"final\\n  reason\\ttext"}\n')
    check(rt._orchestrator_reason(both) == "final reason text",
          "result text preferred + whitespace collapsed to a compact line")
    # Assistant-text fallback when there is no usable result field.
    only_asst = ('{"type":"assistant","message":{"content":[{"type":"text","text":"just this"}]}}\n'
                 '{"type":"result","result":""}\n')
    check(rt._orchestrator_reason(only_asst) == "just this",
          "empty result -> falls back to the last assistant text")
    # Unparseable / empty -> None so the caller falls back to the raw tail.
    check(rt._orchestrator_reason("") is None, "empty stdout -> None")
    check(rt._orchestrator_reason("not json\n{oops\n") is None, "garbage stdout -> None (never raises)")

    # ================= PGSWEEP: the orphaned-grandchild token leak =================
    # Placed AFTER the 30 checks above so that even a catastrophic failure here cannot skip
    # them. Every arm goes through _arm() and asserts against REAL kernel process state.
    check(hasattr(os, "killpg") and hasattr(os, "getpgid"),
          "POSIX process-group API present (both target platforms are POSIX)")

    leak_stub = tmp / "leak_stub.py"
    leak_stub.write_text(_LEAK_STUB)
    # Swap the analyzer by REBINDING THE MODULE ATTRIBUTE. Re-setting QUIVER_ANALYZE_SCRIPT
    # here would be a silent no-op: run_analyses.py:52 binds ANALYZE_SCRIPT at IMPORT time,
    # so the arm would quietly re-run the original _STUB, spawn no grandchild, and pass
    # having tested nothing. analyze_one resolves the global at call time, so this works.
    _orig_script = ra.ANALYZE_SCRIPT

    def _arm_timeout_path():
        pidfile = tmp / "leak_hang.pid"
        os.environ["QUIVER_TEST_PIDFILE"] = str(pidfile)
        ra.ANALYZE_SCRIPT = str(leak_stub)
        # CONTROL: an identical grandchild that never goes near run_analyses. If the control
        # dies too, something in the environment is killing these processes for unrelated
        # reasons and the main verdict below would be a false green — so the control failing
        # must itself be a FAIL. This is the durable stand-in for a one-shot red/green ritual.
        ctrl = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(45)"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        _SPAWNED.append(ctrl.pid)
        res = ra.run(["LEAKHANG"], concurrency=1, timeout=2)
        check(pidfile.exists(), "timeout arm: the stub really did spawn a grandchild")
        gpid = int(pidfile.read_text())
        _SPAWNED.append(gpid)
        check(res[0].get("signal") == "ERROR" and "timed out" in res[0].get("error", ""),
              f"timeout arm: still yields the ERROR datum (got {res[0]})")
        check(_wait_gone(gpid),
              f"OUTER TIMEOUT: grandchild {gpid} swept from the process table (the leak)")
        check(_alive(ctrl.pid),
              "control grandchild still alive — the environment is not killing these for us")

    def _arm_success_path():
        # THE ARM WITH TEETH. A fix that only kills on timeout passes the arm above and fails
        # this one: analyze.py's own INNER timeout can orphan node and then exit 0-ish, in
        # which case the outer SIGKILL never fires at all.
        pidfile = tmp / "leak_ok.pid"
        os.environ["QUIVER_TEST_PIDFILE"] = str(pidfile)
        ra.ANALYZE_SCRIPT = str(leak_stub)
        t0 = time.monotonic()
        res = ra.run(["LEAKOK"], concurrency=1, timeout=20)
        elapsed = time.monotonic() - t0
        check(res[0].get("signal") == "Buy", f"success arm: took the SUCCESS path (got {res[0]})")
        # Pins that this really was the success path, independent of the stub's internals:
        # had the grandchild held the capture pipe open, this would have taken the full 20s.
        check(elapsed < 10, f"success arm: returned promptly, not via the timeout branch ({elapsed:.1f}s)")
        gpid = int(pidfile.read_text())
        _SPAWNED.append(gpid)
        check(_wait_gone(gpid),
              f"SUCCESS PATH: orphan {gpid} swept too (a kill-on-timeout-only fix leaks here)")
        # Same arm proves the child got its OWN process group, and that --deadline survived
        # the subprocess.run -> Popen rewrite (nothing else in this file inspects argv).
        check(res[0].get("pgid") not in (None, os.getpgid(0)),
              f"child ran in its OWN process group, not ours (got {res[0].get('pgid')})")
        argv = res[0].get("argv") or []
        check("--deadline" in argv, f"--deadline argv preserved through the rewrite (got {argv})")

    def _arm_self_kill_guard():
        # NEVER execute a real killpg against our own group to test this: if the guard is
        # broken — the ONLY case this check exists for — it would SIGKILL run_e2e.sh itself
        # (foreground, shared pgid), so the gate would vanish rather than fail. A recorder is
        # the right seam here precisely because the two arms above already supply the
        # un-mocked kernel verdict; this check is only about the branch decision.
        calls = []
        real = os.killpg
        os.killpg = lambda pgid, sig: calls.append((pgid, sig))
        try:
            for bad, label in ((os.getpgid(0), "own pgid"), (0, "0 = caller's group"),
                               (None, "None (spawn failure)"), (1, "init/launchd")):
                ra._sweep_process_group(bad, "GUARD", "unit")
                check(calls == [], f"self-kill guard held for {label} (got {calls})")
        finally:
            os.killpg = real

    def _arm_spawn_failure():
        # An unguarded `finally:` sweep would raise NameError on this path (proc/pgid unbound)
        # and convert an ERROR datum into a process failure — violating "a failed ticker is
        # data, never a process failure" (run_analyses.py header).
        old = ra.PYTHON
        ra.PYTHON = "/nonexistent/python/binary"
        try:
            res = ra.run(["SPAWNFAIL"], concurrency=1, timeout=5)
        finally:
            ra.PYTHON = old
        check(res[0].get("signal") == "ERROR",
              f"spawn failure -> ERROR datum, not a crash (got {res[0]})")
        check(len(res) == 1, "spawn failure still returns one result per input ticker")
        # signal=="ERROR" alone CANNOT tell a handled spawn failure from a crash inside the
        # finally-sweep: run()'s own except clause rewrites both to an ERROR datum. Pin the
        # message so an unbound-name crash in the sweep goes red instead of masquerading.
        err = res[0].get("error", "")
        check("FileNotFoundError" in err or "No such file" in err,
              f"the ERROR names the SPAWN failure (got {err!r})")
        check("NameError" not in err and "UnboundLocalError" not in err,
              f"the finally-sweep did not crash on unbound proc/pgid (got {err!r})")

    def _arm_sweep_failure_swallowed():
        ra.ANALYZE_SCRIPT = _orig_script
        real = os.killpg

        sigs = []

        def boom(_pgid, sig):  # noqa: ARG001 — signature must match os.killpg
            sigs.append(sig)
            # Raise ONLY for the real SIGKILL, not for the sig=0 liveness probe. Raising on
            # the probe too would make this arm die at the probe and never reach the guard it
            # exists to test — it would pass while the SIGKILL path stayed unprotected.
            if sig != 0:
                raise PermissionError("simulated: not permitted to signal that group")
        os.killpg = boom
        try:
            res = ra.run(["AAPL"], concurrency=1, timeout=30)
        finally:
            os.killpg = real
        check(res[0].get("signal") == "Buy",
              f"a raising killpg is swallowed; the analysis result survives (got {res[0]})")
        check(0 in sigs and any(s != 0 for s in sigs),
              f"the arm reached the real SIGKILL, not just the probe (sigs={sigs})")

    def _arm_non_posix():
        ra.ANALYZE_SCRIPT = _orig_script
        real = os.killpg
        del os.killpg
        try:
            res = ra.run(["AAPL"], concurrency=1, timeout=30)
        finally:
            os.killpg = real
        check(res[0].get("signal") == "Buy",
              f"no os.killpg (non-POSIX) -> degrades to a no-op, result intact (got {res[0]})")

    def _arm_observability():
        # F2's ONLY shipped behavior is the invariant line, and PGSWEEP's only visible signal
        # is the sweep line. Both were uncovered. A silent regression in either would leave
        # the original bug's defining property — total invisibility — fully intact.
        pidfile = tmp / "leak_log.pid"
        os.environ["QUIVER_TEST_PIDFILE"] = str(pidfile)
        ra.ANALYZE_SCRIPT = str(leak_stub)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            res = ra.run(["LEAKOK"], concurrency=1, timeout=20)
        seen = buf.getvalue()
        _SPAWNED.append(int(pidfile.read_text()))
        check(res[0].get("signal") == "Buy", "observability arm took the success path")
        check("timeout invariant" in seen,
              f"TIMEOUT-INVARIANT line emitted (stderr={seen[:200]!r})")
        check(any(v in seen for v in ("outer_first", "inner_first", "race", "unknown")),
              "the invariant line carries a real verdict, not just a label")
        check("swept live process group" in seen and "path=ok" in seen,
              f"PGSWEEP logs the success-path sweep — its most diagnostic event "
              f"(stderr={seen[:300]!r})")
        check(seen.count("swept live process group") == 1,
              f"exactly ONE sweep line per reclaimed group, not one per sweep call "
              f"(got {seen.count('swept live process group')})")

    def _arm_no_duplicate_sweep_log():
        # The timeout path sweeps explicitly and then again in `finally`. killpg(pgid,0)
        # succeeds for a ZOMBIE, so without suppression the second probe re-fires and an
        # operator counting leak events sees two per timeout. Pin it at one.
        pidfile = tmp / "leak_dup.pid"
        os.environ["QUIVER_TEST_PIDFILE"] = str(pidfile)
        ra.ANALYZE_SCRIPT = str(leak_stub)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            res = ra.run(["LEAKHANG"], concurrency=1, timeout=2)
        seen = buf.getvalue()
        _SPAWNED.append(int(pidfile.read_text()))
        check(res[0].get("signal") == "ERROR", "duplicate-log arm took the timeout path")
        check(seen.count("swept live process group") == 1,
              f"timeout path logs the sweep exactly ONCE (got "
              f"{seen.count('swept live process group')}): {seen[:300]!r}")

    def _arm_interrupt_sweep():
        # The Ctrl-C path. start_new_session moves children OUT of the terminal's foreground
        # process group, so an interrupt no longer reaches them the way it did before PGSWEEP.
        # If this regressed, the operator's only out is `kill -9` on the fan-out — uncatchable,
        # so the per-ticker finally-sweep never runs and PGSWEEP would have turned a fast
        # teardown into a GUARANTEED leak. Assert on the kill SIGNAL, not just liveness:
        # os.kill(pid,0) still succeeds for an unreaped zombie.
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(45)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        _SPAWNED.append(p.pid)
        with ra._LIVE_LOCK:
            ra._LIVE_PGIDS.add(p.pid)
        try:
            ra._sweep_all_live("test-interrupt")
            check(p.wait(timeout=5) == -signal.SIGKILL,
                  f"interrupt sweep SIGKILLed the registered tree (rc={p.returncode})")
        finally:
            with ra._LIVE_LOCK:
                ra._LIVE_PGIDS.discard(p.pid)

    try:
        _arm("interrupt-sweep", _arm_interrupt_sweep)
        _arm("timeout-path", _arm_timeout_path)
        _arm("success-path", _arm_success_path)
        _arm("observability", _arm_observability)
        _arm("no-duplicate-sweep-log", _arm_no_duplicate_sweep_log)
        _arm("self-kill-guard", _arm_self_kill_guard)
        _arm("spawn-failure", _arm_spawn_failure)
        _arm("sweep-failure-swallowed", _arm_sweep_failure_swallowed)
        _arm("non-posix", _arm_non_posix)
    finally:
        ra.ANALYZE_SCRIPT = _orig_script
        os.environ.pop("QUIVER_TEST_PIDFILE", None)

    # ============ TIMEOUT-INVARIANT: pure, so no process is spawned ============
    def _arm_timeout_invariant():
        check(ra._timeout_invariant(3600, 3600)[0] == "race",
              "outer == inner -> race (what run_tick.py:326 produces TODAY)")
        check(ra._timeout_invariant(3660, 3600)[0] == "inner_first",
              "outer > inner -> inner fires first (the good case)")
        check(ra._timeout_invariant(300, 3600)[0] == "outer_first",
              "outer < inner -> the OUTER SIGKILL wins, no error_mode")
        check(ra._timeout_invariant(3600, None)[0] == "unknown",
              "inner unknown -> 'unknown', never a bogus verdict")
        check(ra._timeout_invariant(0, 3600)[0] == "unknown", "outer falsy -> unknown")
        check("3600" in ra._timeout_invariant(3600, 3600)[1]
              and "race" in ra._timeout_invariant(3600, 3600)[1].lower(),
              "the note carries the real numbers and names the consequence")
        # The inner really is discoverable here, and matches config.yaml's analyze_timeout_sec.
        check(ra._discover_inner_timeout() == 3600,
              f"inner discovered from config.yaml (got {ra._discover_inner_timeout()})")

    def _arm_operator_value_honored():
        # The operator's value must be HONORED verbatim — never silently raised. Clamping it
        # up would remove the only cap on token burn AND lengthen the very leak PGSWEEP
        # closes, while still not delivering "inner first" (the fallback's admission floor is
        # anchored to the same deadline the outer enforces, so the two cancel).
        ra.ANALYZE_SCRIPT = _orig_script
        seen = ra.run(["AAPL"], concurrency=1, timeout=30)
        check(seen[0].get("signal") == "Buy",
              "a caller timeout far below the config inner is honored, not clamped away")

    _arm("timeout-invariant", _arm_timeout_invariant)
    _arm("operator-value-honored", _arm_operator_value_honored)

    # The registry must not leak entries across ticks, or a later interrupt would sweep a pgid
    # that has since been recycled onto an unrelated process group.
    _arm("live-pgid-registry", lambda: check(
        len(ra._LIVE_PGIDS) == 0,
        f"live-pgid registry drained after every fan-out (left {ra._LIVE_PGIDS})"))

    # A vanished arm is invisible to run_e2e.sh (it greps only for '0 failed'), so pin a floor.
    check(_PASS + _FAIL >= 64, f"arm count did not shrink (ran {_PASS + _FAIL})")

    print(f"{_PASS} checks passed, {_FAIL} failed")
    return 1 if _FAIL else 0


def _cleanup_spawned():
    """Kill every process this file created. A CORRECTLY-RED run leaks a 45s sleeper, and
    tests/run_e2e.sh runs 18 more suites after this one inside a cgroup capped at
    MemoryMax=2500M — so a red must not also poison the suites behind it."""
    for pid in _SPAWNED:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        _cleanup_spawned()
    raise SystemExit(rc)
