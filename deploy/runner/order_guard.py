#!/usr/bin/env python3
"""PreToolUse order-authorization guard (decision D5) — the keystone safety hook.

Run by the headless `claude` CLI before every tool call. It mechanically DENIES any
place/cancel-order whose ref_id was not reserved by the Python plan this tick. The
ledger is Quiver's single source of truth and reserves every order's ref_id BEFORE
the broker call (reserve-before-place), so the guard and the brain cannot disagree:
the LLM literally cannot place an order Python did not authorize.

This is defense-in-depth on the invariant — even if the runbook prompt were ignored,
a hallucinated or duplicated ref_id is blocked here. Read tools (get_portfolio,
quotes, etc.) always pass.

The pure decision lives in `evaluate()` (unit-tested offline). `main()` adapts the
`claude` CLI PreToolUse hook stdin/stdout contract around it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Order-placing/cancelling tools (matched after stripping any mcp__server__ prefix).
ORDER_TOOLS = {
    "place_equity_order", "place_option_order",
    "cancel_equity_order", "cancel_option_order",
}


def _base_tool(tool_name: str) -> str:
    return str(tool_name or "").split("__")[-1]


def evaluate(tool_name: str, tool_input: dict, led) -> tuple:
    """(allow: bool, reason: str). Allows everything except an order/cancel whose
    ref_id is not a row the Python plan reserved in the ledger."""
    if _base_tool(tool_name) not in ORDER_TOOLS:
        return (True, "non-order tool — allowed")
    ref_id = (tool_input or {}).get("ref_id")
    if not ref_id:
        return (False, "order call carries no ref_id — Python never authorized it")
    if led.get_order(ref_id) is None:
        return (False, f"ref_id {ref_id} was NOT reserved by the Python plan — denied")
    return (True, f"ref_id {ref_id} matches a Python-reserved order — allowed")


def main() -> int:
    """Adapt the `claude` PreToolUse hook contract: read the tool call on stdin,
    print an allow/deny decision. Fail CLOSED on any error (deny order tools) so a
    guard bug can never wave an order through."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        payload = {}
    tool_name = payload.get("tool_name") or payload.get("tool") or ""
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    try:
        from lib.ledger import Ledger
        led = Ledger(_REPO / "state" / "ledger.db")
        allow, reason = evaluate(tool_name, tool_input, led)
    except Exception as e:  # noqa: BLE001 — fail CLOSED for order tools
        if _base_tool(tool_name) in ORDER_TOOLS:
            allow, reason = False, f"guard error, failing closed: {e}"
        else:
            allow, reason = True, f"guard error on non-order tool, allowed: {e}"
    decision = "approve" if allow else "block"
    # Claude Code PreToolUse hook JSON: permissionDecision + reason.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allow else "deny",
            "permissionDecisionReason": reason,
        },
        "decision": decision, "reason": reason,
    }))
    return 0 if allow else 2


if __name__ == "__main__":
    raise SystemExit(main())
