#!/usr/bin/env python3
"""Deterministic, blocking, parallel-capped analysis fan-out for ONE tick (STEP 3).

This runs the per-ticker `analyze.py` fan-out OFF the LLM. The headless orchestrator
(`claude -p`) CANNOT run this step: the analysis takes ~20-30 min, and a headless run
cannot hold a blocking Bash call that long — the claude harness auto-backgrounds it and
the -p turn then ends, REAPING the job (the 2026-06-17 failure: analyses backgrounded,
0 decisions, a silent false success; and again when the first fix made it ONE foreground
call — the harness still auto-backgrounded it).

So the supervisor (`deploy/runner/run_tick.py`, plain Python with no harness/turn) calls
``run(pending)`` directly, BLOCKS until every analysis finishes, and writes the result to
``state/tmp/analyses.json``; the orchestrator's STEP 3 just READS that file. analyze.py is
still the decision-maker — this only moves the INVOKER off the LLM and onto Python. The
``__main__`` CLI is retained for manual/debug use (e.g. ``run_analyses.py AAPL`` on the box).

Each ``analyze.py <TICKER>`` runs IN PARALLEL (capped to stay under the service MemoryMax).
Contract (mirrors analyze.py): each result is the one-line JSON `analyze.py` prints on
stdout; a crash / non-zero exit / timeout / unparseable line becomes
``{"ticker": T, "signal": "ERROR", "error": "...", "schema": 1}`` so `plan` records it as a
skip. Output order matches the input ticker order. The CLI exit code is ALWAYS 0 — a failed
ticker is data (an ERROR element), never a process failure.

Env:
  QUIVER_ANALYZE_CONCURRENCY  max parallel analyze.py procs (default 2; the
                              e2-medium box is 4 GiB and the service cgroup is capped at
                              MemoryMax=2500M, which must hold claude + EVERY analyze.py
                              child. 2 keeps a wide margin under that cap; bump it only
                              after profiling real analyze.py RSS shows headroom. The 4h
                              tick budget means even sequential (cap 1) finishes in time,
                              so memory safety wins over speed here).
  QUIVER_ANALYZE_TIMEOUT      per-ticker wall-clock seconds for the OUTER guard (this
                              file's SIGKILL). Precedence: this env var (if set)
                              OVERRIDES the caller-passed value; else the supervisor
                              passes config loop.analyze_timeout_sec; else a 3600s
                              default. Set very high so a normal high-reasoning GLM run
                              never hits it. This value is HONORED verbatim — never
                              silently raised — because it is the operator's only cap on
                              token burn (analyze.py has no knob for total child wall
                              time). See TIMEOUT-INVARIANT below for what we do instead.

TWO timeouts, and which one fires first matters:
  OUTER = this file's SIGKILL (above).  INNER = analyze.py:534's per-brain-run
  communicate(timeout=cfg.analyze_timeout_sec). The INNER is the better one to win: it
  yields an ERROR datum carrying error_mode via analyze._classify_failure, while the OUTER
  yields only the bare _error() string below, with no error_mode at all. We LOG the
  relationship once per fan-out (TIMEOUT-INVARIANT) rather than enforcing it, because every
  way of enforcing it is worse than the disease: clamping the outer UP removes the operator's
  burn cap and *lengthens* the window a leaked node keeps billing; a hard assert fails CLOSED
  (a no-trades tick is a P0 outage); and "inner always first" is unreachable anyway — the
  fallback's admission floor (analyze.py:611-626) is anchored to the SAME deadline the outer
  enforces, so raising the outer raises the floor identically and cancels out. Leaving it
  un-enforced is SAFE only because PGSWEEP (below) makes an outer SIGKILL non-leaking: losing
  the race now costs error_mode fidelity, not money.

PGSWEEP — each analyze.py child gets its OWN process group (start_new_session=True) and that
group is SIGKILLed on EVERY exit path. analyze.py:506 spawns `node decide.mjs` with no process
group of its own, so before this, SIGKILLing analyze.py orphaned node and it kept billing
OpenRouter to completion — silently: node holds no fd back to this process, so the outer call
returned promptly with a clean ERROR datum and nothing ever logged. Sweeping on success too
(not just on timeout) is what makes the guarantee hold by construction: analyze.py's own inner
timeout can orphan node and then exit 0-ish, in which case the outer SIGKILL never fires at all.
NOTE for on-box cleanup: children now live in their own process groups, so
`kill -TERM -<run_analyses pgid>` no longer reaches them — use `pkill -f analyze.py`.
  QUIVER_ANALYZE_SCRIPT       path to the analyzer (default <repo>/analyze.py);
                              overridable so the harness is testable offline.
  QUIVER_PYTHON               interpreter for analyze.py (default: this interpreter).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# Process groups with a live analyze.py tree in them, so an interrupt can reclaim ALL of them
# (see the Ctrl-C note in the header). Mutated from worker threads -> guarded.
_LIVE_PGIDS: set = set()
_LIVE_LOCK = threading.Lock()

ANALYZE_SCRIPT = os.environ.get("QUIVER_ANALYZE_SCRIPT", str(_REPO / "analyze.py"))
PYTHON = os.environ.get("QUIVER_PYTHON", sys.executable)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _error(ticker: str, msg: str) -> dict:
    return {"ticker": ticker, "signal": "ERROR", "error": str(msg)[:500], "schema": 1}


def _sweep_process_group(pgid, ticker: str, path: str) -> None:
    """PGSWEEP: SIGKILL anything still alive in one analyze.py child's process group.

    Runs on EVERY exit path of analyze_one — timeout, spawn failure, crash, AND success.
    A hit on path=ok is the most diagnostic event this file can produce: it means something
    outlived a *successful* analysis, i.e. a process still billing OpenRouter with nobody
    waiting on it.

    TOTAL, never raises: this sits on the live trading path and a sweep failure must never
    turn an analysis result into a process failure ("a failed ticker is data", see header).

    The guard is deliberately four conditions, not one equality. `pgid` is None on the
    spawn-failure path, and 0 is POSIX for "the CALLER's process group" — os.killpg(0,
    SIGKILL) would SIGKILL run_analyses AND the run_tick.py supervisor that calls run()
    IN-PROCESS (run_tick.py:328), un-catchably, with the run_lock held and not one ERROR
    datum written. `pgid <= 1` additionally rejects the init/launchd group.
    """
    if not hasattr(os, "killpg") or not hasattr(os, "getpgid"):
        return  # non-POSIX; both target platforms (darwin dev, linux box) are POSIX
    try:
        own = os.getpgid(0)
    except OSError:
        return
    if not pgid or pgid <= 1 or pgid == own:
        return
    try:
        os.killpg(pgid, 0)  # probe: is anything still alive in the group?
    except ProcessLookupError:
        return  # genuinely empty group — the normal, quiet case; say nothing
    except OSError:
        # EPERM and friends mean the group EXISTS but we may not signal it (e.g. it contains
        # only a process we no longer own). A bare `except OSError` here would score that as
        # "empty" and skip BOTH the log and the kill — silently, which is the exact property
        # that made the original leak invisible. Fall through: log it, then try anyway.
        pass
    except Exception:  # noqa: BLE001 — TOTAL: a monkeypatched/odd killpg must not escape
        return
    # Something OUTLIVED the analysis. GT-1's defining property was that this was SILENT,
    # so closing the leak without a signal would reproduce exactly that property.
    try:
        sys.stderr.write(f"[run_analyses] swept live process group {pgid} "
                         f"for {ticker} (path={path})\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — logging must never break the result path
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 — TOTAL, per this function's contract
        pass


def _sweep_all_live(reason: str) -> None:
    """Reclaim EVERY live analyze.py tree. The interrupt path: start_new_session moves the
    children out of the terminal's foreground process group, so a Ctrl-C on the documented
    manual CLI no longer reaches them the way it did before PGSWEEP. Without this, the
    operator's only recourse is `kill -9` on the fan-out — which is uncatchable, so the
    per-ticker `finally:` sweep never runs and PGSWEEP would have turned a fast teardown
    into a GUARANTEED leak. Sweeping here also unblocks shutdown(wait=True): the workers'
    communicate() returns as soon as their children die."""
    with _LIVE_LOCK:
        pgids = list(_LIVE_PGIDS)
    for p in pgids:
        _sweep_process_group(p, "*", reason)


def _discover_inner_timeout():
    """analyze.py's INNER per-brain-run timeout, or None when it cannot be known.

    Mirrors analyze.py:534's expression INCLUDING its 3600 fallback. That fallback is load
    bearing and must stay in sync: lib/config.py:791 defaults the same key to 900, so reading
    only the loader's default here would report an inner of 900 for a child that will actually
    use 3600 — an inverted invariant reported with a false sense of safety.

    Injectable seam: tests rebind this module attribute to force the unknown-inner branch.
    """
    try:
        # The documented manual CLI (`run_analyses.py AAPL`, see the header) puts scripts/ on
        # sys.path, NOT the repo root — so without this `lib.config` is unimportable and the
        # invariant line degrades to "unknown" on exactly the box where an operator would run
        # it by hand. In the supervisor process run_tick.py:25-26 has already done this.
        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        from lib.config import load_config
        # The SAME file analyze.py:716 loads, so we report the value the child will really use.
        return int(getattr(load_config(_REPO / "config.yaml"), "analyze_timeout_sec", 3600))
    except Exception:  # noqa: BLE001 — a broken config must not break the fan-out
        return None


def _timeout_invariant(outer, inner):
    """TIMEOUT-INVARIANT: classify OUTER vs INNER. PURE — no I/O, so it is unit-testable
    without spawning anything. Returns (verdict, note).

    verdict: "unknown" | "inner_first" | "race" | "outer_first".
    We report rather than enforce — see the module header for why every enforcement option
    is worse than the disease.
    """
    if not outer or not inner:
        # Name WHICH side is missing; "inner=unknown" when the outer was the falsy one sends
        # a reader to look at config.yaml for a problem that is not there.
        missing = "outer" if not outer else "inner"
        return "unknown", (f"outer={outer}s inner={inner}s — {missing} unknown, cannot "
                           f"determine which timeout fires first")
    if outer > inner:
        return "inner_first", (f"outer={outer}s > inner={inner}s — the INNER fires first "
                               f"(good: the ERROR datum carries error_mode)")
    if outer == inner:
        # What deploy/runner/run_tick.py:326 produces TODAY: it passes cfg.analyze_timeout_sec
        # as the OUTER while analyze.py:534 reads the SAME key as the INNER.
        return "race", (f"outer={outer}s == inner={inner}s — RACE: which timeout fires first "
                        f"is nondeterministic, so error_mode may or may not be recorded")
    return "outer_first", (f"outer={outer}s < inner={inner}s — the OUTER SIGKILL fires first; "
                           f"the ERROR datum will carry NO error_mode")


def analyze_one(ticker: str, *, timeout: int) -> dict:
    """Run analyze.py for one ticker; return its parsed JSON dict, or an ERROR dict.

    analyze.py prints framework chatter to stderr and exactly one JSON line to
    stdout (its LAST stdout line). We parse that; anything else is an ERROR datum.

    F2: pass a per-ticker --deadline (epoch s) = now + timeout, so analyze.py's
    fallback re-run can skip itself if < 600s remain (anchored to the SAME clock
    as this subprocess's SIGKILL, not the tick-wide deadline).

    PGSWEEP: this is subprocess.run() unrolled, because subprocess.run NEVER exposes the
    Popen — and without proc.pid there is no process group to reclaim. Everything else about
    the call is deliberately byte-identical to it (same argv, cwd, PIPEs, text, inherited
    stdin, same _error strings).
    """
    deadline = time.monotonic() + timeout
    proc = None
    pgid = None
    path = "ok"
    swept = [False]   # list so the finally can see the timeout branch's assignment
    try:
        try:
            proc = subprocess.Popen(
                [PYTHON, ANALYZE_SCRIPT, ticker, "--deadline", f"{deadline}"],
                cwd=str(_REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001 — a spawn failure is just an ERROR datum
            path = "spawn_failed"
            return _error(ticker, f"{type(e).__name__}: {e}")
        # setsid() makes the child the leader of a NEW session and group whose pgid == its
        # pid, so proc.pid IS the group. Captured HERE, at spawn — os.getpgid(proc.pid)
        # raises once the child is reaped, which would silently skip the sweep in exactly
        # the case where a grandchild survived. (While the group is NON-EMPTY the kernel
        # keeps that pid reserved as its pgid, so the leak case cannot hit a recycled pid;
        # once it is empty the probe below finds nothing and the sweep is a no-op.)
        pgid = proc.pid
        with _LIVE_LOCK:
            _LIVE_PGIDS.add(pgid)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            path = "timeout"
            # Kill the GROUP before draining, so the drain is not waiting on live writers.
            _sweep_process_group(pgid, ticker, path)
            # Suppress the finally-sweep's duplicate. SIGKILL is uncatchable, so nothing in
            # the group can still be RUNNING — but the grandchild spends ~6-30ms as a zombie
            # awaiting reparent-to-init reaping, and killpg(pgid, 0) succeeds for a zombie.
            # Without this the finally probe re-fires and logs a SECOND "swept" line for the
            # same pgid, so an operator counting leak events would see two per timeout.
            swept[0] = True
            # BOUNDED. An unbounded second communicate() waits for EOF on both pipes, which
            # any descendant that escaped the group can hold open forever -> the fan-out
            # never returns -> run_tick.py:328 hangs -> the run_lock TTL expires and the next
            # hourly fire steals it -> two concurrent ticks on one ledger. The recovery path
            # must never be less bounded than the path it replaced.
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001 — best-effort reap; the sweep already killed it
                pass
            return _error(ticker, f"analyze.py timed out after {timeout}s")
        except Exception as e:  # noqa: BLE001 — any other I/O failure is still just a datum
            path = "error"
            return _error(ticker, f"{type(e).__name__}: {e}")
        rc = proc.returncode
        # Label the sweep path honestly: a non-zero exit is not "ok", and mislabelling it
        # would make the one genuinely alarming log line (path=ok, i.e. something outlived a
        # SUCCESSFUL analysis) indistinguishable from a routine crash.
        path = "ok" if rc == 0 else f"exit{rc}"
        lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        if not lines:
            tail = (err or "").strip()[-300:]
            return _error(ticker, f"analyze.py produced no stdout (rc={rc}); "
                                  f"stderr tail: {tail}")
        return _parse_analysis_line(ticker, lines[-1])
    finally:
        # Sweep on EVERY exit path, success included — see the module header. Skipped only
        # when the timeout branch already swept this exact group moments ago.
        if not swept[0]:
            _sweep_process_group(pgid, ticker, path)
        if pgid:
            with _LIVE_LOCK:
                _LIVE_PGIDS.discard(pgid)


def _parse_analysis_line(ticker: str, line: str) -> dict:
    """The last stdout line -> the analysis dict, or an ERROR datum."""
    try:
        obj = json.loads(line)
    except ValueError:
        return _error(ticker, f"analyze.py last stdout line was not JSON: {line[:200]}")
    if not isinstance(obj, dict):
        return _error(ticker, f"analyze.py JSON was not an object: {line[:200]}")
    obj.setdefault("ticker", ticker)
    return obj


def run(tickers, *, concurrency: int | None = None, timeout: int | None = None) -> list:
    """Analyze every ticker in parallel (capped); return results in INPUT order."""
    tickers = [t for t in tickers]
    if not tickers:
        return []
    concurrency = concurrency or _int_env("QUIVER_ANALYZE_CONCURRENCY", 2)
    # Per-ticker timeout precedence: env override > caller (config) > 3600s default.
    if os.environ.get("QUIVER_ANALYZE_TIMEOUT"):
        timeout = _int_env("QUIVER_ANALYZE_TIMEOUT", 3600)
    elif timeout is None:
        timeout = 3600
    # TIMEOUT-INVARIANT: state the outer-vs-inner relationship ONCE per fan-out (not per
    # ticker — in production it is the same for all of them and a line that always prints is
    # a line nobody reads). Report only; the operator's value is honored verbatim.
    try:
        _verdict, _note = _timeout_invariant(timeout, _discover_inner_timeout())
        sys.stderr.write(f"[run_analyses] timeout invariant [{_verdict}]: {_note}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — observability must never block the fan-out
        pass
    results: list = [None] * len(tickers)
    workers = min(concurrency, len(tickers))
    # NOT `with ThreadPoolExecutor(...)`: __exit__ runs shutdown(wait=True) BEFORE any except
    # clause here, so a Ctrl-C would block until every in-flight analyze.py hit its full
    # timeout (3600s by default) while its children — now in their own sessions — kept
    # billing. Managing the executor by hand lets the interrupt reclaim the trees FIRST,
    # which then lets the join finish in milliseconds.
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        fut_to_i = {ex.submit(analyze_one, t, timeout=timeout): i
                    for i, t in enumerate(tickers)}
        done = 0
        for fut in as_completed(fut_to_i):
            i = fut_to_i[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # noqa: BLE001 — defensive; analyze_one already guards
                results[i] = _error(tickers[i], f"{type(e).__name__}: {e}")
            done += 1
            sys.stderr.write(f"[run_analyses] {done}/{len(tickers)} {tickers[i]} -> "
                             f"{results[i].get('signal')}\n")
            sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — KeyboardInterrupt/SystemExit included ON PURPOSE
        _sweep_all_live("interrupt")
        raise
    finally:
        ex.shutdown(wait=True)
    return results


def append_tee(path, rows, trade_date: str) -> int:
    """Append each analysis row to a durable JSONL archive, stamped with `trade_date`.

    WHY THIS EXISTS. `state/tmp/analyses.json` is OVERWRITTEN every tick
    (`deploy/runner/run_tick.py`), and `analyze.py` emits no `date` field, so quiver keeps NO
    durable record of what its brain actually said on any past day. Without an archive there is
    nothing to replay a real signal stream from, and `lib.wall_replay.jsonl_signal_source` — the
    seam built to consume exactly that — has never had a producer.

    Returns the number of rows appended. NEVER RAISES: this runs on the live trading path, and
    a full disk or a read-only mount must not be able to abort a tick. Failures return 0.

    Append-only, one JSON object per line, opened in "a" mode per call so a crash mid-write can
    at worst lose the tail rather than corrupt the archive.
    """
    try:
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        if not rows or not str(trade_date or "").strip():
            return 0
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with p.open("a", encoding="utf-8") as fh:
            for r in rows:
                # `date` FIRST and always present — its absence is what made every recorded
                # stream silently key under "None" and yield zero signals on replay.
                fh.write(json.dumps({"date": str(trade_date), **r}, default=str) + "\n")
                n += 1
        return n
    except Exception as e:  # noqa: BLE001 — deliberately total: observability must never trade
        sys.stderr.write(f"[run_analyses] tee-signals failed (ignored): {e}\n")
        return 0


def main(argv) -> int:
    argv = list(argv)
    tee_path = None
    tee_date = None
    if "--tee-signals" in argv:
        i = argv.index("--tee-signals")
        tee_path = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
    if "--date" in argv:
        i = argv.index("--date")
        tee_date = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]

    tickers = [a.strip().upper() for a in argv if a.strip()]
    if not tickers:
        sys.stderr.write("usage: run_analyses.py [--tee-signals PATH] [--date YYYY-MM-DD] "
                         "TICKER [TICKER ...]\n")
        print("[]")
        return 0
    sys.stderr.write(
        f"[run_analyses] analyzing {len(tickers)} tickers "
        f"(concurrency={_int_env('QUIVER_ANALYZE_CONCURRENCY', 2)}, "
        f"timeout={_int_env('QUIVER_ANALYZE_TIMEOUT', 3600)}s): {', '.join(tickers)}\n")
    results = run(tickers)
    if tee_path:
        from lib import market as _market
        n = append_tee(tee_path, results, tee_date or _market.trading_day_et())
        sys.stderr.write(f"[run_analyses] teed {n} signal rows -> {tee_path}\n")
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
