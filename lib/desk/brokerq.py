"""Robinhood reads and no-money actions from the chat (owner, 2026-11-09).

"Everything we have API or MCP access to, I want as a command I can call from chat." The
desk's ``broker_read`` tool names one Robinhood tool and its arguments. This module checks
the name against two lists and queues a ``query`` broker job. ``quiver-broker`` runs it with
``QUERY.md``: one call, the raw result back through ``tick.py query-commit``. The desk never
reaches Robinhood itself (wall 1).

  * ``READS``: tools that only read. They run on request.
  * ``ACTIONS``: watchlists, price alerts and scans. They move no money, so they run on the
    owner's request with no card, as a plan change does (plan §10.6).

An order, a cancel, an option, a crypto trade, a bank or card tool is in neither list:
orders go on a card (``propose_trade``), and the guard denies every other broker write.
A cancel goes on a card too (the change kind ``cancel_order``): only its Confirm (a tap or
the owner's typed "yes") queues the one call in ``CARD_ACTIONS``, through
``cancel_payload``. ``validate`` refuses those names, so ``broker_read`` cannot send one.
``broker_guard`` holds its own copy of these lists and checks each call again.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from lib.desk import store

READS = {
    # the account
    "get_portfolio": "the account value, cash and buying power",
    "get_equity_positions": "every stock and fund held, with cost and value",
    "get_equity_orders": "stock orders: open and past, with fills (args: state, symbol, created_at_gte, order_id)",
    "get_advanced_orders": "stop-loss and take-profit pairs (OCO)",
    "get_equity_tax_lots": "the tax lots of one holding (args: symbol)",
    "get_realized_pnl": "realized profit and loss",
    "get_pnl_trade_history": "profit and loss per trade",
    "get_trade_approval_setting": "whether Robinhood asks for its own approval",
    "get_option_positions": "option positions",
    "get_option_orders": "option orders",
    "get_crypto_positions": "crypto positions",
    "get_crypto_orders": "crypto orders",
    # prices and companies
    "get_equity_quotes": "live quotes (args: symbols)",
    "get_equity_historicals": "price history (args: symbols, interval, span)",
    "get_equity_fundamentals": "company basics: value, P/E, dividend, 52-week range (args: symbols)",
    "get_equity_analyst_ratings": "Wall Street ratings and price targets (args: symbol)",
    "get_equity_technical_indicators": "technical indicators (args: symbol, indicators)",
    "get_equity_tradability": "whether a stock can be traded, and fractional support (args: symbols)",
    "get_equity_price_book": "the order book (args: symbol)",
    "get_earnings_calendar": "upcoming earnings dates",
    "get_earnings_results": "past earnings results (args: symbol)",
    "get_financials": "income, balance sheet and cash flow (args: symbol)",
    "get_index_quotes": "index levels",
    "get_index_historicals": "index history",
    "get_indexes": "the index list",
    "get_option_chains": "option chains (args: symbol)",
    "get_option_instruments": "option contracts",
    "get_option_quotes": "option quotes",
    "get_option_historicals": "option price history",
    "get_crypto_quotes": "crypto quotes",
    "get_currency_pairs": "crypto pairs",
    "get_politician_trades": "trades disclosed by members of Congress",
    "get_sec_filing": "an SEC filing",
    "get_sec_filing_index": "the SEC filing list of a company",
    "get_sec_filing_facts": "XBRL facts of a filing",
    "get_sec_filing_facts_catalog": "the XBRL fact names",
    "search": "find a stock, fund or crypto by name",
    # lists, alerts and scans (read)
    "get_watchlists": "the owner's watchlists",
    "get_watchlist_items": "the names on a watchlist (args: watchlist id)",
    "get_popular_watchlists": "Robinhood's popular lists",
    "get_option_watchlist": "the option watchlist",
    "get_alerts": "price alerts",
    "get_alert_log": "alerts that fired",
    "get_scans": "saved scans",
    "get_scanner_filter_specs": "the filters a scan can use",
    "get_scanner_datapoints": "the data points a scan can use",
    "preview_scan": "run a scan definition once",
    "run_scan": "run a saved scan",
}

ACTIONS = {
    "add_to_watchlist": "add a name to a watchlist",
    "remove_from_watchlist": "remove a name from a watchlist",
    "create_watchlist": "make a watchlist",
    "update_watchlist": "rename a watchlist",
    "follow_watchlist": "follow a list",
    "unfollow_watchlist": "unfollow a list",
    "create_alert": "set a price alert",
    "update_alert": "change a price alert",
    "delete_alert": "delete a price alert",
    "mark_alerts_read": "mark alerts read",
    "create_scan": "save a scan",
    "update_scan_config": "change a saved scan",
    "update_scan_filters": "change a scan's filters",
}

# Run only from a confirmed ``cancel_order`` card (its Confirm queues the job).
CARD_ACTIONS = {
    "cancel_equity_order": "cancel an open stock order (args: order_id)",
    "cancel_advanced_order": "cancel an open stop-loss and take-profit pair (args: order_id)",
}
ORDER_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

ARGS_MAX_CHARS = 4000
RESULT_MAX_CHARS = 60_000
MAX_PAGES = 5


class QueryError(ValueError):
    """A tool name or arguments the desk may not send. The text goes back to the model."""


def catalog() -> dict:
    """What the desk can call, for the tool description and an error text."""
    return {"reads": READS, "actions": ACTIONS}


def validate(tool: str, args, pages=1) -> dict:
    """The job payload for one call, or ``QueryError``. ``account_number`` is never taken
    from the model: ``quiver-broker`` adds the configured account."""
    name = str(tool or "").strip()
    if name.startswith("mcp__"):
        name = name.split("__")[-1]
    if name not in READS and name not in ACTIONS:
        raise QueryError(f"{name or 'that'} is not a Robinhood read or a no-money action. Orders and cancels go "
                         "on a card with propose_trade; options, crypto trades, banking and cards are not "
                         "open to the desk. Reads: " + ", ".join(sorted(READS)) + ". Actions: " +
                         ", ".join(sorted(ACTIONS)))
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise QueryError("args is an object of the tool's own arguments")
    args = {k: v for k, v in args.items() if k != "account_number"}
    text = json.dumps(args, sort_keys=True, default=str)
    if len(text) > ARGS_MAX_CHARS:
        raise QueryError(f"args are {len(text)} characters; the limit is {ARGS_MAX_CHARS}")
    try:
        pages = int(pages or 1)
    except (TypeError, ValueError):
        raise QueryError("pages is a whole number")
    if not 1 <= pages <= MAX_PAGES:
        raise QueryError(f"pages is 1 to {MAX_PAGES}")
    return {"tool": name, "args": args, "pages": pages, "action": name in ACTIONS}


def cancel_payload(order_id: str, *, advanced: bool, proposal_id: int) -> dict:
    """The query job payload of one confirmed cancel card."""
    oid = str(order_id or "").strip()
    if not ORDER_ID_RE.match(oid):
        raise QueryError("order_id is the Robinhood order id")
    tool = "cancel_advanced_order" if advanced else "cancel_equity_order"
    return {"tool": tool, "args": {"order_id": oid}, "pages": 1, "action": True, "proposal_id": int(proposal_id)}


def validate_job(payload: dict, requested_by: str) -> dict:
    """The payload of a queued query job, checked again before it runs. A card action is
    valid only from a confirmed card (``requested_by='approval'`` with its proposal id)."""
    tool = str((payload or {}).get("tool") or "")
    if tool in CARD_ACTIONS:
        if requested_by != "approval" or not str((payload or {}).get("proposal_id") or "").isdigit():
            raise QueryError(f"{tool} runs only from a confirmed card")
        return cancel_payload((payload.get("args") or {}).get("order_id"), advanced=tool == "cancel_advanced_order",
                              proposal_id=int(payload["proposal_id"]))
    return validate(tool, (payload or {}).get("args"), (payload or {}).get("pages"))


def cancel_outcome(led, payload: dict, got: Optional[dict], why: Optional[str]) -> str:
    """The one line the owner gets after a confirmed cancel ran."""
    pid = payload.get("proposal_id")
    with led.connect() as conn:
        row = conn.execute("SELECT payload_json FROM proposals WHERE id=?", (int(pid),)).fetchone()
    ch = (store.loads(row[0], {}) or {}).get("change") or {} if row else {}
    name = f"the {ch.get('ticker')} order" if ch.get("ticker") else "the order"
    if got is None:
        return f"I could not cancel {name} (#{pid}): {why or 'Robinhood did not answer'}. Check the Robinhood app."
    data = (got.get("result") or {}).get("data")
    text = json.dumps(data, default=str).lower() if data is not None else ""
    if any(w in text for w in ("error", "not found", "cannot", "can't", "ineligible", "already", "rejected",
                               "failed", "denied")):
        return f"Robinhood did not cancel {name} (#{pid}). It may have filled or been cancelled already. Check the app."
    return f"Cancelled {name} (#{pid})."


def enqueue(conn, payload: dict, *, requested_by: str, now: str) -> int:
    cur = conn.execute("INSERT INTO broker_jobs (kind, status, requested_by, created_at, payload_json) "
                       "VALUES ('query', 'queued', ?, ?, ?)",
                       (requested_by, now, store.canonical_json(payload)))
    return int(cur.lastrowid)


def result_of(conn, job_id: int) -> Optional[dict]:
    """``{"status", "result", "error"}`` of a query job, or None when there is no such job."""
    row = conn.execute("SELECT status, result_json, error FROM broker_jobs WHERE id=? AND kind='query'",
                       (int(job_id),)).fetchone()
    if row is None:
        return None
    return {"status": row[0], "result": store.loads(row[1], None), "error": row[2]}


def commit(conn, job_id: int, file_obj: dict, *, secret: str = "", now: str) -> dict:
    """Store the raw result of a running query job (``tick.py query-commit``). The file must
    hold the job's own tool name, so a model cannot file another tool's answer. Every string
    that holds the account number is redacted, and a long result is cut."""
    row = conn.execute("SELECT status, payload_json FROM broker_jobs WHERE id=? AND kind='query'",
                       (int(job_id),)).fetchone()
    if row is None:
        raise QueryError(f"job {job_id} is not a query job")
    if row[0] != "running":
        raise QueryError(f"query job {job_id} is {row[0]}, not running")
    payload = store.loads(row[1], {}) or {}
    tool = payload.get("tool")
    if not isinstance(file_obj, dict) or tool not in file_obj:
        raise QueryError(f"the file must be {{\"{tool}\": <result>}}")
    raw = _redact(file_obj[tool], secret)
    text = json.dumps(raw, default=str)
    cut = len(text) > RESULT_MAX_CHARS
    result = {"tool": tool, "data": raw if not cut else text[:RESULT_MAX_CHARS], "cut": cut}
    conn.execute("UPDATE broker_jobs SET result_json=? WHERE id=?", (json.dumps(result, default=str), int(job_id)))
    return {"ok": True, "job": int(job_id), "tool": tool, "chars": len(text), "cut": cut, "at": now}


def _redact(obj, secret: str):
    if not secret:
        return obj
    if isinstance(obj, dict):
        return {k: _redact(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v, secret) for v in obj]
    if isinstance(obj, str) and secret in obj:
        return obj.replace(secret, "[account]")
    return obj
