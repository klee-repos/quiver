#!/usr/bin/env python3
"""The broker hook: the mechanical gate on every tool call of the broker model (plan §11.3).

``deploy/runner/settings.json`` runs this file before EVERY tool call (PreToolUse, matcher
``.*``) and, with ``--record-review``, after each ``review_equity_order`` (PostToolUse).
Both hooks use ``onFailure: "block"``.

The rules:
  * It allows a ``place_equity_order`` only for a leg of the current execute job's proposal
    that ``exec-check`` passed less than 120 s ago, whose review passed, whose fields match
    the call exactly, with no KILL, no halt, and more than 120 s to the close. On allow it
    first commits the leg to ``placing`` (and its first ``submitted_at``), then prints allow.
  * It allows the Robinhood read tools (its own list plus ``lib.desk.brokerq.READS``), a
    ``ToolSearch`` ``select:`` of those names, a ``Write`` of ``state/tmp/*.json``, and the
    ``tick.py`` commands of one pattern.
  * It allows one no-money action (``brokerq.ACTIONS``: a watchlist, an alert, a scan) only
    in a ``query`` job (``QUIVER_BROKER_QUERY``) whose payload names that tool with exactly
    those arguments.
  * It allows a cancel (``brokerq.CARD_ACTIONS``) only in such a query job that a confirmed
    ``cancel_order`` card queued: ``requested_by='approval'``, its proposal ``applied``, and
    the same order id.
  * It denies everything else, including ``Read`` and every other broker write.

It denies ONLY with exit code 2 (the reason on stderr): Claude Code does not block a hook
that exits 0 with text that is not JSON. It exits 0 only on the allow path. Every
exception denies. Paths resolve against the repo root, never the hook input ``cwd``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = Path(os.environ.get("QUIVER_ROOT") or _HERE.parent.parent.parent)
if str(_HERE.parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent.parent.parent))

SERVER = "robinhood-trading"
READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_equity_positions", "get_equity_quotes",
    "get_equity_orders", "get_trade_approval_setting", "get_trade_approvals", "review_equity_order",
})
PLACE = "place_equity_order"
SEARCHABLE = READ_TOOLS | {PLACE}
BASH_RE = re.compile(
    r"\.venv/bin/python tick\.py (exec-check|exec-commit|fills|snapshot-commit|auth-stop|wait|query-commit)"
    r"( --(proposal|job|seconds) [0-9]+| --phase (sells|buys|recover)| --input state/tmp/[A-Za-z0-9_.-]+\.json)+")
CHECK_MAX_AGE_S = 120.0
CLOSE_MARGIN_S = 120.0
BUSY_MS = 10000


def _base(tool_name: str) -> tuple:
    """(server or None, base name) for ``mcp__server__tool`` or a built-in name."""
    name = str(tool_name or "")
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[1], "__".join(parts[2:])
        return "", name
    return None, name


def _now() -> datetime:
    from lib import market
    return market.now_et().astimezone(timezone.utc)


def _parse_ts(value):
    if not value:
        return None
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect():
    db = ROOT / "state" / "ledger.db"
    if not db.exists():
        raise RuntimeError("no ledger")
    conn = sqlite3.connect(str(db), timeout=BUSY_MS / 1000.0, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={BUSY_MS}")
    conn.row_factory = sqlite3.Row
    return conn


def _kill_path() -> Path:
    try:
        from lib.config import load_config
        return Path(load_config(ROOT / "config.yaml").kill_switch_file)
    except Exception:  # noqa: BLE001 — an unreadable config cannot widen the gate
        return ROOT / "KILL"


def _under(path: str, base: Path) -> bool:
    p = path if os.path.isabs(path) else str(ROOT / path)
    real = os.path.realpath(p)
    b = os.path.realpath(base)
    return real.startswith(b + os.sep)


# --- the pre-call decision -----------------------------------------------------------

def _desk_lists() -> tuple:
    """(reads, actions, card actions) from ``lib.desk.brokerq``. A failed import raises, so
    the guard denies."""
    from lib.desk import brokerq
    return frozenset(brokerq.READS), frozenset(brokerq.ACTIONS), frozenset(brokerq.CARD_ACTIONS)


def _canon(args: dict) -> str:
    return json.dumps({k: v for k, v in (args or {}).items() if k != "account_number"}, sort_keys=True,
                      separators=(",", ":"), default=str)


def check_action(conn, base: str, tool_input: dict, *, env: dict) -> tuple:
    """(allow, reason) for a no-money action: only in the running query job that names it,
    with exactly its arguments and the configured account."""
    job_env = str(env.get("QUIVER_BROKER_QUERY") or "")
    if not job_env.isdigit():
        return False, f"{base} runs only in a query job"
    job = conn.execute("SELECT kind, status, payload_json, requested_by FROM broker_jobs WHERE id=?",
                       (int(job_env),)).fetchone()
    if job is None or job["kind"] != "query" or job["status"] != "running":
        return False, "the query job is not running"
    try:
        payload = json.loads(job["payload_json"] or "{}")
    except ValueError:
        return False, "the query job has no payload"
    if payload.get("tool") != base:
        return False, f"the query job asks for {payload.get('tool')}, not {base}"
    if base in _desk_lists()[2]:
        # A cancel: only from a confirmed cancel_order card, for that card's order id.
        if job["requested_by"] != "approval" or not str(payload.get("proposal_id") or "").isdigit():
            return False, f"{base} runs only from a confirmed card"
        prop = conn.execute("SELECT kind, state, payload_json FROM proposals WHERE id=?",
                            (int(payload["proposal_id"]),)).fetchone()
        try:
            ch = (json.loads(prop["payload_json"] or "{}").get("change") or {}) if prop else {}
        except ValueError:
            ch = {}
        if prop is None or prop["kind"] != "change" or prop["state"] != "applied" or ch.get("kind") != "cancel_order":
            return False, "the cancel card is not confirmed"
        if str((tool_input or {}).get("order_id") or "") != str(ch.get("order_id") or "-"):
            return False, "order_id differs from the confirmed card"
    acct = str(env.get("RH_ACCOUNT_NUMBER") or "")
    if (tool_input or {}).get("account_number") not in (None, "") and str(tool_input["account_number"]) != acct:
        return False, "account_number does not match"
    if _canon(tool_input) != _canon(payload.get("args") or {}):
        return False, "the arguments differ from the query job's"
    return True, f"{base} matches the query job"


def _tool_search(tool_input: dict, env=None) -> tuple:
    q = str((tool_input or {}).get("query") or "")
    if not q.startswith("select:"):
        return False, "ToolSearch allows only a select: query of the broker read tools"
    names = [n.strip() for n in q[len("select:"):].split(",") if n.strip()]
    if not names:
        return False, "ToolSearch select: names no tool"
    reads, actions, cards = _desk_lists()
    # An action or a cancel loads only in a query job (its call has its own gate).
    in_query = str((env or {}).get("QUIVER_BROKER_QUERY") or "").isdigit()
    allowed = SEARCHABLE | reads | ((actions | cards) if in_query else frozenset())
    for n in names:
        server, base = _base(n)
        if server not in (None, SERVER) or base not in allowed:
            return False, f"ToolSearch may not load {n}"
    return True, "ToolSearch select: of allowed broker tools"


def _dec(v, places: int):
    try:
        return Decimal(str(v)).quantize(Decimal(1).scaleb(-places))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _styled(leg) -> bool:
    """A limit or stop leg (owner, 2026-11-09). A leg with no type is a market leg."""
    try:
        typ = leg["type"]
    except (KeyError, IndexError):
        typ = None
    return str(typ or "market") != "market"


def _prices_match(ti: dict, leg) -> bool:
    """The call's limit and stop prices equal the leg's (both absent, or equal to the cent)."""
    for f in ("limit_price", "stop_price"):
        want, got = leg[f], ti.get(f)
        if want is None:
            if got not in (None, ""):
                return False
        elif got in (None, "") or _dec(got, 4) != _dec(want, 4):
            return False
    return True


def check_place(conn, tool_input: dict, *, env: dict, now: datetime, kill_path: Path) -> tuple:
    """(allow, reason) for one place call. Pure on the connection: no write here."""
    from lib import market
    ti = tool_input or {}
    ref = str(ti.get("ref_id") or "")
    pid_env = str(env.get("QUIVER_BROKER_PROPOSAL") or "")
    job_env = str(env.get("QUIVER_BROKER_JOB") or "")
    if not pid_env.isdigit() or not job_env.isdigit():
        return False, "not an execute job: no order may be placed"
    if not ref:
        return False, "place call carries no ref_id"
    leg = conn.execute("SELECT * FROM orders WHERE ref_id=?", (ref,)).fetchone()
    if leg is None:
        return False, f"ref_id {ref} is not a reserved leg"
    if leg["proposal_id"] is None or int(leg["proposal_id"]) != int(pid_env):
        return False, "the leg is not part of this job's proposal"
    job = conn.execute("SELECT status, proposal_id, kind FROM broker_jobs WHERE id=?", (int(job_env),)).fetchone()
    if job is None or job["status"] != "running" or job["kind"] != "execute" or int(job["proposal_id"] or -1) != int(pid_env):
        return False, "the execute job is not running for this proposal"
    prop = conn.execute("SELECT state, expires_at FROM proposals WHERE id=?", (int(pid_env),)).fetchone()
    if prop is None or prop["state"] != "executing":
        return False, f"the proposal is {prop['state'] if prop else 'missing'}, not executing"
    exp = _parse_ts(prop["expires_at"])
    if exp is None or now >= exp:
        return False, "the proposal has expired"
    now_et = now.astimezone(market.ET)
    _day, open_t, close_t = market.session_open_et(now_et)
    if not (open_t <= now_et < close_t) or (close_t - now_et).total_seconds() <= CLOSE_MARGIN_S:
        return False, "the session is closed or within 120 s of the close"
    if kill_path.exists():
        return False, "the KILL file exists"
    day = market.trading_day_et()
    halted = conn.execute("SELECT halted FROM day_baseline WHERE trade_date=?", (day,)).fetchone()
    if halted is not None and int(halted[0]) == 1:
        return False, "trading is halted today"
    if leg["state"] not in ("checked", "placing"):
        return False, f"the leg is {leg['state']}, not checked"
    checked = _parse_ts(leg["checked_at"])
    if checked is None or (now - checked).total_seconds() >= CHECK_MAX_AGE_S:
        return False, "the leg check is older than 120 s"
    if leg["state"] == "placing":
        sub = _parse_ts(leg["submitted_at"])
        if sub is not None and not checked > sub:
            return False, "a re-send needs a new check after the first send"
    if int(leg["review_ok"] or 0) != 1:
        return False, "the leg has no passed review"
    reviewed = _parse_ts(leg["reviewed_at"])
    if reviewed is None or not reviewed > checked:
        return False, "the review is older than the check"
    if str(leg["trade_date"]) != day:
        return False, "the leg is not for today's session"
    acct = str(env.get("RH_ACCOUNT_NUMBER") or "")
    if not acct or str(ti.get("account_number") or "") != acct:
        return False, "account_number does not match"
    for field in ("symbol", "side", "type"):
        want = {"symbol": leg["ticker"], "side": leg["side"], "type": leg["type"]}[field]
        if str(ti.get(field) or "") != str(want or ""):
            return False, f"{field} does not match the leg"
    has_q, has_d = ti.get("quantity") not in (None, ""), ti.get("dollar_amount") not in (None, "")
    if has_q and has_d:
        return False, "give quantity or dollar_amount, not both"
    if _styled(leg):
        # A limit or stop leg: whole shares, the leg's prices, the leg's time in force.
        if not has_q or leg["quantity"] is None or _dec(ti["quantity"], 6) != _dec(leg["quantity"], 6):
            return False, "quantity does not match the leg"
        if not _prices_match(ti, leg):
            return False, "limit_price or stop_price does not match the leg"
        if ti.get("market_hours") != "regular_hours":
            return False, "market_hours must be regular_hours"
        if ti.get("time_in_force") != (leg["time_in_force"] or "gfd"):
            return False, "time_in_force does not match the leg"
        if ti.get("tax_lots") not in (None, [], ""):
            return False, "tax_lots is not allowed"
        return True, f"leg {ref} matches its checked and reviewed row"
    if leg["side"] == "sell":
        if not has_q or leg["quantity"] is None or _dec(ti["quantity"], 6) != _dec(leg["quantity"], 6):
            return False, "quantity does not match the leg"
    else:
        if not has_d or leg["dollar_amount"] is None or _dec(ti["dollar_amount"], 2) != _dec(leg["dollar_amount"], 2):
            return False, "dollar_amount does not match the leg"
    if ti.get("market_hours") != "regular_hours":
        return False, "market_hours must be regular_hours"
    if ti.get("time_in_force") != "gfd":
        return False, "time_in_force must be gfd"
    for field in ("limit_price", "stop_price"):
        if ti.get(field) not in (None, ""):
            return False, f"{field} is not allowed"
    if ti.get("tax_lots") not in (None, [], ""):
        return False, "tax_lots is not allowed"
    return True, f"leg {ref} matches its checked and reviewed row"


def evaluate(tool_name: str, tool_input: dict, *, env=None, now=None, conn=None, kill_path=None) -> tuple:
    """(allow, reason). For an allowed place call the leg is committed to ``placing``
    before this returns."""
    env = os.environ if env is None else env
    server, base = _base(tool_name)
    ti = tool_input or {}
    if server is None:
        if base == "ToolSearch":
            return _tool_search(ti, env)
        if base == "Write":
            path = str(ti.get("file_path") or "")
            if path.endswith(".json") and _under(path, ROOT / "state" / "tmp") and \
                    not os.path.isdir(os.path.realpath(path if os.path.isabs(path) else ROOT / path)):
                return True, "a runbook file in state/tmp"
            return False, "Write is allowed only for state/tmp/*.json"
        if base == "Bash":
            cmd = str(ti.get("command") or "")
            if not BASH_RE.fullmatch(cmd):
                return False, "Bash allows only the tick.py runbook commands"
            m = re.search(r"--input (\S+)", cmd)
            if m and not _under(m.group(1), ROOT / "state" / "tmp"):
                return False, "--input must be a file in state/tmp"
            return True, "a tick.py runbook command"
        return False, f"{base} is not allowed for the broker model"
    if server != SERVER:
        return False, f"tools of {server} are not allowed"
    reads, actions, cards = _desk_lists()
    if base in READ_TOOLS or base in reads:
        return True, f"{base} is a broker read"
    if base in actions or base in cards:
        if not str(env.get("QUIVER_BROKER_QUERY") or "").isdigit():
            return False, f"{base} runs only in a query job"
        own_q = conn is None
        cq = conn or _connect()
        try:
            return check_action(cq, base, ti, env=env)
        finally:
            if own_q:
                cq.close()
    if base != PLACE:
        return False, f"{base} is not allowed"
    now = now or _now()
    kill_path = kill_path or _kill_path()
    own = conn is None
    conn = conn or _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            allow, reason = check_place(conn, ti, env=env, now=now, kill_path=kill_path)
            if allow:
                cur = conn.execute(
                    "UPDATE orders SET state='placing', submitted_at=COALESCE(submitted_at, ?) "
                    "WHERE ref_id=? AND state IN ('checked','placing')", (_iso(now), ti["ref_id"]))
                if cur.rowcount != 1:
                    raise RuntimeError("the placing write changed no row")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return allow, reason
    finally:
        if own:
            conn.close()


# --- the post-review record ------------------------------------------------------------

def _unwrap(raw):
    if isinstance(raw, dict) and isinstance(raw.get("content"), list) and "data" not in raw:
        raw = raw["content"]
    if isinstance(raw, list) and raw and all(isinstance(b, dict) and b.get("type") == "text" for b in raw):
        raw = "".join(str(b.get("text") or "") for b in raw)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def _review_quote_move(leg, data: dict, now, tolerance_pct) -> "str | None":
    """The reason to refuse when Robinhood's own ``quote_data`` in the review shows a move
    over ``tolerance_pct`` from the leg's checked price, else None. The model writes the
    exec-check quote file; this quote comes from the broker through the hook, so the model
    cannot change it. A quote that is not usable here (another symbol, a venue time in the
    future, no price) gives None: exec-check already checked the model's file."""
    from lib.desk import quotes
    q = data.get("quote_data")
    if not isinstance(q, dict) or str(q.get("symbol") or "").upper() != str(leg["ticker"]).upper():
        return None
    if now is None or tolerance_pct is None:
        return None
    times = [quotes.parse_venue_time(q.get(k)) for k in ("venue_last_trade_time", "venue_bid_time", "venue_ask_time")]
    if any(t is not None and (t - now).total_seconds() > 5 for t in times):
        return None   # a venue time ahead of our clock: not a quote we can age
    if _styled(leg):
        return None   # the owner's limit or stop price protects a limit or stop leg
    price, _why = quotes.leg_price(q, now)
    ref = float(leg["check_quote"] or leg["quote_at_proposal"] or 0.0)
    if price is None or ref <= 0:
        return None
    moved = quotes.moved_pct(price, ref)
    if moved > float(tolerance_pct):
        return f"the broker's quote moved {moved:.2f}% from the checked price ({ref:.2f} to {price:.2f})"
    return None


def review_verdict(leg, tool_response, *, now=None, tolerance_pct=None) -> tuple:
    """(review_ok, reason, parsed). ``review_ok`` is 1 only for a known, clean answer."""
    obj = _unwrap(tool_response)
    if not isinstance(obj, dict):
        return 0, "the review answer has an unknown shape", obj
    data = obj.get("data")
    if not isinstance(data, dict):
        return 0, "the review answer has no data object", obj
    if data.get("symbol") is not None and str(data["symbol"]).upper() != str(leg["ticker"]).upper():
        return 0, "the review is for another symbol", obj
    if data.get("side") is not None and str(data["side"]) != str(leg["side"]):
        return 0, "the review is for another side", obj
    if "order_checks" not in data:
        return 0, "the review has no order_checks", obj
    if data["order_checks"] not in ({}, []):
        return 0, "the review has an order_checks alert", obj
    if obj.get("customer_approval_required") is True or data.get("customer_approval_required") is True:
        return 0, "Robinhood requires the customer's approval in its app", obj
    moved = _review_quote_move(leg, data, now, tolerance_pct)
    if moved:
        return 0, moved, obj
    return 1, "clean review", obj


def _tolerance(conn) -> float:
    """``requote_tolerance_pct`` in force (desk_settings over config.yaml)."""
    from lib.config import load_config
    from lib.desk import settings as desk_settings
    return float(desk_settings.effective(conn, load_config(ROOT / "config.yaml"))["requote_tolerance_pct"])


def record_review(payload: dict, *, env=None, now=None, conn=None) -> tuple:
    """Write the review onto the matching ``checked``/``placing`` leg (matched on the
    review call's own ``tool_input``). Returns (review_ok, reason)."""
    env = os.environ if env is None else env
    pid = str(env.get("QUIVER_BROKER_PROPOSAL") or "")
    if not pid.isdigit():
        return 0, "not an execute job"
    ti = payload.get("tool_input") or {}
    now = now or _now()
    own = conn is None
    conn = conn or _connect()
    try:
        legs = conn.execute("SELECT * FROM orders WHERE proposal_id=? AND state IN ('checked','placing')",
                            (int(pid),)).fetchall()
        match = None
        for leg in legs:
            if str(ti.get("symbol") or "") != leg["ticker"] or str(ti.get("side") or "") != leg["side"] \
                    or str(ti.get("type") or "") != str(leg["type"] or ""):
                continue
            if _styled(leg):
                if ti.get("quantity") in (None, "") or ti.get("dollar_amount") not in (None, ""):
                    continue
                if _dec(ti.get("quantity"), 6) != _dec(leg["quantity"], 6) or not _prices_match(ti, leg):
                    continue
            elif leg["side"] == "sell":
                if ti.get("quantity") in (None, "") or ti.get("dollar_amount") not in (None, ""):
                    continue
                if _dec(ti.get("quantity"), 6) != _dec(leg["quantity"], 6):
                    continue
            else:
                if ti.get("dollar_amount") in (None, "") or ti.get("quantity") not in (None, ""):
                    continue
                if _dec(ti.get("dollar_amount"), 2) != _dec(leg["dollar_amount"], 2):
                    continue
            match = leg
            break
        if match is None:
            return 0, "no checked leg matches this review"
        try:
            tol = _tolerance(conn)
        except Exception:  # noqa: BLE001 — no tolerance means no clean review (fail closed)
            tol = None
        if tol is None:
            ok, reason, parsed = 0, "the requote tolerance could not be read", _unwrap(payload.get("tool_response"))
        else:
            ok, reason, parsed = review_verdict(match, payload.get("tool_response"), now=now, tolerance_pct=tol)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE orders SET review_json=?, reviewed_at=?, review_ok=? WHERE ref_id=?",
                     (json.dumps({"reason": reason, **(parsed if isinstance(parsed, dict) else {"raw": parsed})}),
                      _iso(now), ok, match["ref_id"]))
        conn.execute("COMMIT")
        return ok, reason
    finally:
        if own:
            conn.close()


def _deny(reason: str) -> int:
    msg = f"broker_guard: denied: {reason}"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": msg},
                      "decision": "block", "reason": msg}))
    print(msg, file=sys.stderr)
    return 2


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook input is not an object")
        if "--record-review" in argv:
            ok, reason = record_review(payload)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                     "additionalContext": f"broker_guard review: {reason}"}}))
            return 0
        tool_name = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        allow, reason = evaluate(tool_name, tool_input)
    except Exception as e:  # noqa: BLE001 — any guard failure denies
        return _deny(f"guard error, failing closed: {type(e).__name__}: {e}")
    if not allow:
        return _deny(reason)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                             "permissionDecisionReason": f"broker_guard: {reason}"}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
