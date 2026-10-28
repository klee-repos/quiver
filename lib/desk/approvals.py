"""The tap: the ONLY code path that moves a proposal to ``approved`` or ``applied`` (plan §10.5).

``handle_tap`` runs when the owner taps a button on a proposal card. It is pure Python
and takes well under a second. No model tool can reach it: only
``deploy/runner/chat_bridge.py`` imports this module (the W4 test enforces this).

Checks, in order; the first failure answers the tap and stops:
  1. ``callback_data`` is ``p:<id>:<nonce>:<a|r>``; ``from.id`` is an owner; the chat id and
     message id equal the proposal's card; the nonce matches.
  2. The proposal is ``pending``.
  3. ``now < expires_at`` (else it becomes ``expired``).
  4. Approve on a trade only: no KILL file, today not halted, the session open, and at
     least 5 minutes before the close. A failure keeps the proposal ``pending``.
  5. One conditional UPDATE claims it. A second tap changes zero rows.
  6. A trade: in the same transaction the legs become ``approved`` and one ``execute``
     broker job is queued. A change: it is applied in the same transaction.

The caller sends what the result names: the callback answer, the new button label, the
messages, the follow-up cards, and the broker trigger.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Optional

from lib import market
from lib.config import Config
from lib.desk import proposals, store

CLOSE_MARGIN = timedelta(minutes=5)
_STATE_TEXT = {
    "approved": "Already approved", "executing": "Already approved", "executed": "Already done",
    "partially_executed": "Already done", "failed": "This one failed", "rejected": "Rejected",
    "expired": "Expired", "withdrawn": "Withdrawn", "requoted": "Price moved",
    "applied": "Already confirmed",
}


def parse_callback(data: str):
    """``(proposal_id, nonce, action)`` or None for any other shape."""
    parts = str(data or "").split(":")
    if len(parts) != 4 or parts[0] != "p" or parts[3] not in ("a", "r"):
        return None
    if not parts[1].isdigit() or not parts[2]:
        return None
    return int(parts[1]), parts[2], parts[3]


def _result(answer: str, label: Optional[str] = None, **kw) -> dict:
    out = {"answer": answer, "label": label, "messages": [], "cards": [], "touch": [],
           "ok": False, "proposal_id": None}
    out.update(kw)
    return out


def _state_answer(prop: dict) -> dict:
    st = prop["state"]
    if st == "superseded":
        text = f"Replaced by #{prop['superseded_by']}" if prop.get("superseded_by") else "Replaced"
    else:
        text = _STATE_TEXT.get(st, st)
    return _result(text, label=text, proposal_id=prop["id"])


def trade_gate(led, cfg: Config, prop: dict, now_dt) -> Optional[str]:
    """The first check-4 reason that fails, as the owner's text; None when all pass."""
    close_txt = market.fmt_et(store.parse_ts(prop["expires_at"]))
    if os.path.exists(cfg.kill_switch_file):
        return f"Trading is stopped on the box. This stays open until {close_txt} ET."
    if led.is_halted(market.trading_day_et()):
        return ("Trading is halted for today after the daily-loss check. "
                "Ask me to propose resume_trading to lift it.")
    now_et = now_dt.astimezone(market.ET)
    _day, open_t, close_t = market.session_open_et(now_et)
    if not (open_t <= now_et < close_t):
        return (f"The market is closed. It opens at {market.fmt_et(open_t)} ET. "
                f"This stays open until {close_txt} ET. Tap again then.")
    if close_t - now_et < CLOSE_MARGIN:
        return f"It's too close to the close. This one expires at {close_txt} ET."
    return None


def handle_tap(led, cfg: Config, *, callback_data: str, from_id, chat_id, message_id,
               owner_ids, now_dt=None, yf_price_fn=None) -> dict:
    """Run the §10.5 checks and, when they pass, claim and act. Returns the result dict."""
    now_dt = now_dt or store.utc_now()
    now = store.iso(now_dt)
    parsed = parse_callback(callback_data)
    if parsed is None:
        return _result("This button is not valid.")
    pid, nonce, action = parsed
    owners = {str(x) for x in (owner_ids or ())}
    if str(from_id) not in owners:
        return _result("Not authorized.")
    with led.connect() as conn:
        prop = proposals.get_proposal(conn, pid)
    if prop is None:
        return _result("This proposal does not exist.")
    if (str(prop.get("tg_chat_id")) != str(chat_id) or prop.get("tg_message_id") is None
            or int(prop["tg_message_id"]) != int(message_id) or prop["nonce"] != nonce):
        return _result("This button is not valid.")
    if prop["state"] != "pending":
        return _state_answer(prop)
    if now_dt >= store.parse_ts(prop["expires_at"]):
        with led.transaction() as conn:
            proposals.end_pending(conn, pid, "expired", now)
        return _result("Expired", label="Expired", proposal_id=pid)
    if action == "r":
        return _reject(led, prop, from_id, now)
    if prop["kind"] == "trade":
        why = trade_gate(led, cfg, prop, now_dt)
        if why:
            return _result(why, label=None, proposal_id=pid)
        return _approve_trade(led, prop, from_id, now)
    return _confirm_change(led, cfg, prop, from_id, now_dt, yf_price_fn)


def _claim(conn, pid: int, nonce: str, decided_by: str, now: str) -> bool:
    cur = conn.execute("UPDATE proposals SET state='approved', decided_at=?, decided_by=? "
                       "WHERE id=? AND state='pending' AND nonce=?", (now, decided_by, pid, nonce))
    return cur.rowcount == 1


def _current(led, pid: int) -> dict:
    with led.connect() as conn:
        return _state_answer(proposals.get_proposal(conn, pid))


def _approve_trade(led, prop: dict, from_id, now: str) -> dict:
    pid = prop["id"]
    with led.transaction() as conn:
        if not _claim(conn, pid, prop["nonce"], str(from_id), now):
            claimed = False
        else:
            claimed = True
            conn.execute("UPDATE orders SET state='approved' WHERE proposal_id=? AND state='proposed'",
                         (pid,))
            conn.execute("INSERT INTO broker_jobs (kind, proposal_id, recovery, status, requested_by, "
                         "created_at) VALUES ('execute', ?, 0, 'queued', 'tap', ?)", (pid, now))
    if not claimed:
        return _current(led, pid)
    return _result("Approved", label="Approved", ok=True, proposal_id=pid, touch=["broker"],
                   messages=["On it. Checking the price now."])


def _reject(led, prop: dict, from_id, now: str) -> dict:
    pid = prop["id"]
    with led.transaction() as conn:
        cur = conn.execute("UPDATE proposals SET state='rejected', decided_at=?, decided_by=? "
                           "WHERE id=? AND state='pending' AND nonce=?",
                           (now, str(from_id), pid, prop["nonce"]))
        if cur.rowcount != 1:
            moved = False
        else:
            moved = True
            conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason='rejected' "
                         "WHERE proposal_id=? AND state='proposed'", (pid,))
            chat = prop.get("tg_chat_id") or ""
            store.insert_desk_message(conn, chat_id=chat, text=f"You rejected #{pid}.",
                                      source="proposal", now=now)
    if not moved:
        return _current(led, pid)
    return _result("Rejected", label="Rejected", ok=True, proposal_id=pid)


def _confirm_change(led, cfg: Config, prop: dict, from_id, now_dt, yf_price_fn) -> dict:
    pid = prop["id"]
    now = store.iso(now_dt)
    error = None
    applied = None
    try:
        with led.transaction() as conn:
            if not _claim(conn, pid, prop["nonce"], str(from_id), now):
                applied = False
            else:
                res = proposals.apply_change(led, conn, cfg, {**prop}, now_dt=now_dt)
                conn.execute("UPDATE proposals SET state='applied' WHERE id=? AND state='approved'", (pid,))
                applied = res
    except proposals.ProposalError as e:
        error = str(e)
    if error is not None:
        # The apply rolled back with the claim. Record the failure on the still-pending row.
        with led.transaction() as conn:
            conn.execute("UPDATE proposals SET state='failed', decided_at=?, decided_by=?, result_json=? "
                         "WHERE id=? AND state='pending'",
                         (now, str(from_id), store.canonical_json({"error": error}), pid))
        return _result(f"I could not apply #{pid}: {error}", label="Failed", proposal_id=pid,
                       messages=[f"I could not apply #{pid}: {error}. Nothing changed."])
    if applied is False:
        return _current(led, pid)
    out = _result("Confirmed", label="Confirmed", ok=True, proposal_id=pid)
    out["superseded"] = applied.get("superseded", [])
    # The follow-up trade (§10.4): built pending with its own buttons. Never approved here.
    with led.connect() as conn:
        fresh = proposals.get_proposal(conn, pid)
    yf = proposals.prefetch_prices(led, proposals.change_tickers(store.loads(fresh.get("payload_json"), {})),
                                   now_dt=now_dt, yf_price_fn=yf_price_fn)
    with led.transaction() as conn:
        fu = proposals.build_follow_up(led, conn, cfg, fresh, now_dt=now_dt, yf_price_fn=yf)
        if fu.get("waiting"):
            conn.execute("UPDATE proposals SET result_json=? WHERE id=?",
                         (store.canonical_json({"follow_up": "waiting"}), pid))
            store.enqueue_snapshot_job(conn, requested_by="follow_up", now=now)
    if fu.get("built"):
        out["cards"].append(fu["built"]["proposal_id"])
        out["superseded"] = out["superseded"] + fu["built"].get("superseded", [])
    elif fu.get("waiting"):
        out["touch"].append("broker")
        out["messages"].append("I need a fresh portfolio snapshot before I can size the trades. "
                               "I'll send them as soon as it lands.")
    elif fu.get("refused"):
        out["messages"].append(f"I could not build the follow-up trade: {fu['refused']}.")
    return out
