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
  QUIVER_ANALYZE_TIMEOUT      per-ticker wall-clock seconds. Precedence: this env
                              var (if set) OVERRIDES the caller-passed value; else
                              the supervisor passes config loop.analyze_timeout_sec;
                              else a 3600s default. Set very high so a normal
                              high-reasoning GLM run never hits it.
  QUIVER_ANALYZE_SCRIPT       path to the analyzer (default <repo>/analyze.py);
                              overridable so the harness is testable offline.
  QUIVER_PYTHON               interpreter for analyze.py (default: this interpreter).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

ANALYZE_SCRIPT = os.environ.get("QUIVER_ANALYZE_SCRIPT", str(_REPO / "analyze.py"))
PYTHON = os.environ.get("QUIVER_PYTHON", sys.executable)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _error(ticker: str, msg: str) -> dict:
    return {"ticker": ticker, "signal": "ERROR", "error": str(msg)[:500], "schema": 1}


def analyze_one(ticker: str, *, timeout: int) -> dict:
    """Run analyze.py for one ticker; return its parsed JSON dict, or an ERROR dict.

    analyze.py prints framework chatter to stderr and exactly one JSON line to
    stdout (its LAST stdout line). We parse that; anything else is an ERROR datum.
    """
    try:
        p = subprocess.run(
            [PYTHON, ANALYZE_SCRIPT, ticker],
            cwd=str(_REPO), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _error(ticker, f"analyze.py timed out after {timeout}s")
    except Exception as e:  # noqa: BLE001 — a spawn failure is just an ERROR datum
        return _error(ticker, f"{type(e).__name__}: {e}")
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
    if not lines:
        tail = (p.stderr or "").strip()[-300:]
        return _error(ticker, f"analyze.py produced no stdout (rc={p.returncode}); "
                              f"stderr tail: {tail}")
    try:
        obj = json.loads(lines[-1])
    except ValueError:
        return _error(ticker, f"analyze.py last stdout line was not JSON: {lines[-1][:200]}")
    if not isinstance(obj, dict):
        return _error(ticker, f"analyze.py JSON was not an object: {lines[-1][:200]}")
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
    results: list = [None] * len(tickers)
    workers = min(concurrency, len(tickers))
    with ThreadPoolExecutor(max_workers=workers) as ex:
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
    return results


def main(argv) -> int:
    tickers = [a.strip().upper() for a in argv if a.strip()]
    if not tickers:
        sys.stderr.write("usage: run_analyses.py TICKER [TICKER ...]\n")
        print("[]")
        return 0
    sys.stderr.write(
        f"[run_analyses] analyzing {len(tickers)} tickers "
        f"(concurrency={_int_env('QUIVER_ANALYZE_CONCURRENCY', 2)}, "
        f"timeout={_int_env('QUIVER_ANALYZE_TIMEOUT', 3600)}s): {', '.join(tickers)}\n")
    results = run(tickers)
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
