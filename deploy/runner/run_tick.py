#!/usr/bin/env python3
"""Headless tick supervisor — the BRAX pattern adapted to Quiver, in Python.

On each scheduled wake (systemd timer): acquire the single-run lock, run
`tick.py preflight`, and — ONLY if it says proceed — drive exactly one tick by
running the `claude` CLI in headless `-p` mode over TICK.md, with the Robinhood +
Resend MCPs attached and the PreToolUse order-guard denying any unauthorized order.

Execution-only: ZERO trading logic lives here. Mirrors BRAX's `spawnClaude` shape
(`claude -p --append-system-prompt <rules> --mcp-config mcp.json --settings <hooks>
"Follow ./TICK.md exactly"`), with a wall-clock timeout as the floor and an
AUTH_ERROR -> hard-stop posture (never trade blind). The fine-grained stream-idle /
poll-wedge watchdogs from BRAX are a future enhancement; the timeout bounds a wedge.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lib import market, runlock  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

VENV_PY = os.environ.get("QUIVER_PYTHON", str(_REPO / ".venv" / "bin" / "python"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
MCP_CONFIG = os.environ.get("QUIVER_MCP_CONFIG", str(_REPO / "deploy" / "runner" / "mcp.json"))
SETTINGS = os.environ.get("QUIVER_CLAUDE_SETTINGS", str(_REPO / "deploy" / "runner" / "settings.json"))
# Cheapest reliable tool-using model (decision D11) — the pass only shuttles JSON.
MODEL = os.environ.get("QUIVER_MODEL", "claude-haiku-4-5-20251001")
EFFORT = os.environ.get("QUIVER_EFFORT", "low")  # ultracode OFF; Python does the thinking
TICK_TIMEOUT_SEC = int(os.environ.get("QUIVER_TICK_TIMEOUT_SEC", "2400"))  # ~40 min ceiling

SYSTEM_PROMPT = (
    "You are Quiver's execution orchestrator running headless. ALL trading, sizing, "
    "risk and portfolio decisions come from the deterministic Python in this repo — you "
    "ONLY execute the MCP calls the runbook specifies and NEVER invent, resize, cancel, "
    "or place an order the Python plan/protect output did not authorize. Follow ./TICK.md "
    "EXACTLY, step by step. On ANY auth error from the Robinhood MCP, STOP immediately and "
    "place nothing (never trade on stale auth). Be terse."
)


def _tick_json(args):
    """Run a tick.py subcommand and return its one-line JSON (or an error dict)."""
    p = subprocess.run([VENV_PY, str(_REPO / "tick.py"), *args],
                       capture_output=True, text=True, cwd=str(_REPO))
    out = (p.stdout or "").strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else {"error": (p.stderr or "")[:300]}
    except (ValueError, IndexError):
        return {"error": (p.stderr or p.stdout or "")[:300]}


def _emit(d):
    print(json.dumps(d))


def main() -> int:
    now_iso = market.now_et().isoformat()
    led = Ledger(_REPO / "state" / "ledger.db")
    holder = f"run-{now_iso}"
    try:
        with runlock.run_lock(led, holder, now_iso):
            pre = _tick_json(["preflight"])
            if pre.get("error"):
                _emit({"stage": "preflight", "stopped": True, **pre})
                return 1
            if not pre.get("proceed"):
                _emit({"stage": "preflight", "proceed": False, "reason": pre.get("reason", "")})
                return 0  # cheap no-op wake — nothing to do
            # Drive ONE tick through the claude CLI over TICK.md (execution only).
            cmd = [
                CLAUDE_BIN, "-p", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose",
                "--model", MODEL, "--effort", EFFORT,
                "--mcp-config", MCP_CONFIG, "--strict-mcp-config",
                "--settings", SETTINGS, "--add-dir", str(_REPO),
                "--max-turns", "80", "--append-system-prompt", SYSTEM_PROMPT,
                "--dangerously-skip-permissions",  # unattended; the order guard is the real gate
                "Follow ./TICK.md exactly for today's tick.",
            ]
            try:
                proc = subprocess.run(cmd, cwd=str(_REPO), timeout=TICK_TIMEOUT_SEC,
                                      capture_output=True, text=True)
                ok = proc.returncode == 0
                _emit({"stage": "tick", "ok": ok, "returncode": proc.returncode,
                       "tail": (proc.stdout or proc.stderr or "")[-400:]})
                return 0 if ok else 1
            except subprocess.TimeoutExpired:
                _emit({"stage": "tick", "ok": False, "error": "tick exceeded the timeout"})
                return 1
    except runlock.RunLockError as e:
        _emit({"stage": "lock", "skipped": True, "reason": str(e)})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
