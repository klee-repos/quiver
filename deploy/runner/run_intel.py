#!/usr/bin/env python3
"""Ops supervisor for ONE strategy-intelligence refresh (deploy/quiver-intel.timer fires this).

Sequences the intel subcommands on the box: refresh (fetch -> split -> prefilter -> chain ->
ingest) -> profile (render one MD per key player) -> propose (shadow, additive). Each step is
best-effort and independent — a chain hiccup can't block profile rendering, and NONE of them
trades or mutates the trading book. Fail-safe: if intel is disabled in config, refresh no-ops.

Deliberately thin (mirrors run_tick.py's subprocess pattern): tick.py owns the logic; this only
sequences and logs, so the analysis stays in one place behind the wall.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
VENV_PY = os.environ.get("QUIVER_PYTHON", str(_REPO / ".venv" / "bin" / "python"))
LOG = os.environ.get("QUIVER_INTEL_LOG", str(_REPO / "logs" / "intel.log"))


def _run(args, timeout=None):
    try:
        p = subprocess.run([VENV_PY, str(_REPO / "tick.py"), *args],
                           capture_output=True, text=True, cwd=str(_REPO), timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"{' '.join(args)} exceeded {timeout}s"}
    out = (p.stdout or "").strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else {"error": (p.stderr or "")[:300]}
    except (ValueError, IndexError):
        return {"error": (p.stderr or p.stdout or "")[:300]}


def _log(obj):
    line = json.dumps(obj)
    print(line, flush=True)
    try:
        Path(LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # logging is best-effort, never fails the run


def main() -> int:
    # refresh is the heavy LLM phase; profile + propose are cheap Python over the ingested rows.
    for step, timeout in (("intel-refresh", 6000), ("intel-profile", 300), ("intel-propose", 300)):
        r = _run([step], timeout=timeout)
        _log({"stage": step, **{k: r[k] for k in r if k != "error"}} |
             ({"error": r["error"][:200]} if r.get("error") else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
