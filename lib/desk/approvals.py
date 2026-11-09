"""The tap: the ONLY code path that moves a proposal to ``approved`` or ``applied`` (plan §10.5).

``handle_tap`` runs when the owner taps a button on a proposal card. It is pure Python
and takes well under a second. No model tool can reach it: only
``deploy/runner/chat_bridge.py`` imports this module (the W4 test enforces this).

Checks, in order; the first failure answers the tap and stops:
  1. ``callback_data`` is ``p:<id>:<nonce>:<a|r>``; ``from.id`` is an owner; the chat id and
     message id equal the proposal's card; the nonce matches.
  2. The proposal is ``pending``. A held trade (below) answers with its send time and keeps
     its Cancel button; Reject on it before the send time cancels it.
  3. ``now < expires_at`` (else it becomes ``expired``).
  4. Approve on a trade only: no KILL file, and the day the order goes is not halted. A
     failure keeps the proposal ``pending``. The market hours never refuse a tap: when the
     session is closed or less than 6 minutes from its close, the tap approves and the job
     is held to ``market.held_send_et`` (10 minutes after the next open). The card then
     shows Cancel until that time.
  5. One conditional UPDATE claims it. A second tap changes zero rows.
  6. A trade: in the same transaction the plan move of a sized owner trade is checked
     (§10.6), the legs become ``approved``, and one ``execute`` broker job is queued. A change: it is
     applied in the same transaction.

The caller sends what the result names: the callback answer, the new button label, the
messages, the follow-up cards, and the broker trigger.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from lib import market
from lib.config import Config
from lib.desk import proposals, store

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


def trade_gate(led, cfg: Config, prop: dict, now_dt, held=None) -> Optional[str]:
    """The first check-4 reason that fails, as the owner's text; None when all pass. The
    market hours are not a reason: ``held_send_et`` holds the order instead. The halt
    binds the day the order goes: today, or the send day of a held order."""
    close_txt = market.fmt_et(store.parse_ts(prop["expires_at"]))
    if os.path.exists(cfg.kill_switch_file):
        return f"Trading is stopped on the box. This stays open until {close_txt} ET."
    day = held[0].astimezone(market.ET).strftime("%Y-%m-%d") if held else market.trading_day_et()
    if led.is_halted(day):
        return ("Trading is halted for today after the daily-loss check. "
                "Ask me to propose resume_trading to lift it.")
    return None


def held_send_et(now_dt):
    """``(send_dt, close_dt)`` when the order must wait for the session; None when it can
    go at once."""
    return market.held_send_et(now_dt)


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
    if prop["state"] == "approved" and prop["kind"] == "trade":
        held = _held_tap(led, prop, action, from_id, now_dt)
        if held is not None:
            return held
    if prop["state"] != "pending":
        return _state_answer(prop)
    if now_dt >= store.parse_ts(prop["expires_at"]):
        with led.transaction() as conn:
            proposals.end_pending(conn, pid, "expired", now)
        return _result("Expired", label="Expired", proposal_id=pid)
    if action == "r":
        return _reject(led, prop, from_id, now)
    if prop["kind"] == "trade":
        held = held_send_et(now_dt)
        why = trade_gate(led, cfg, prop, now_dt, held)
        if why:
            return _result(why, label=None, proposal_id=pid)
        return _approve_trade(led, cfg, prop, from_id, now, now_dt=now_dt, held=held)
    return _confirm_change(led, cfg, prop, from_id, now_dt, yf_price_fn)


def _claim(conn, pid: int, nonce: str, decided_by: str, now: str) -> bool:
    cur = conn.execute("UPDATE proposals SET state='approved', decided_at=?, decided_by=? "
                       "WHERE id=? AND state='pending' AND nonce=?", (now, decided_by, pid, nonce))
    return cur.rowcount == 1


def _current(led, pid: int) -> dict:
    with led.connect() as conn:
        return _state_answer(proposals.get_proposal(conn, pid))


def _approve_trade(led, cfg: Config, prop: dict, from_id, now: str, *, now_dt=None, held=None) -> dict:
    """Claim the trade, approve its legs and queue the execute job in one transaction. A
    sized owner trade's plan move is checked here (§10.6); the plan moves only for the
    orders that fill (``proposals.close_from_legs``), so a card that sends nothing leaves the
    plan as it was.

    ``held`` is ``(send_dt, close_dt)`` when the market cannot take the order now. The job
    then gets ``not_before = send_dt``, and ``expires_at`` moves to that session's close
    when it is earlier. The bridge wakes the broker at the send time. The exec-check of
    that run compares the live quote with the card, so a price move still makes a requote."""
    pid = prop["id"]
    error, claimed = None, False
    not_before = store.iso(held[0]) if held else None
    try:
        with led.transaction() as conn:
            if not _claim(conn, pid, prop["nonce"], str(from_id), now):
                claimed = False
            else:
                claimed = True
                proposals.check_trade_plan(conn, cfg, prop)
                conn.execute("UPDATE orders SET state='approved' WHERE proposal_id=? AND state='proposed'",
                             (pid,))
                conn.execute("INSERT INTO broker_jobs (kind, proposal_id, recovery, status, requested_by, "
                             "created_at, not_before) VALUES ('execute', ?, 0, 'queued', 'tap', ?, ?)",
                             (pid, now, not_before))
                if held and store.parse_ts(prop["expires_at"]) < held[1]:
                    conn.execute("UPDATE proposals SET expires_at=? WHERE id=?", (store.iso(held[1]), pid))
    except proposals.ProposalError as e:
        error = str(e)
    if error is not None:
        # The plan check failed and the claim rolled back with it. End the card: nothing was sent.
        with led.transaction() as conn:
            cur = conn.execute("UPDATE proposals SET state='failed', decided_at=?, decided_by=?, result_json=? "
                               "WHERE id=? AND state='pending'",
                               (now, str(from_id), store.canonical_json({"error": error}), pid))
            if cur.rowcount == 1:
                conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason='plan_changed' "
                             "WHERE proposal_id=? AND state='proposed'", (pid,))
        return _result(f"I did not send #{pid}: the plan changed after this card", label="Failed",
                       proposal_id=pid,
                       messages=[f"I did not send #{pid}, because the plan changed after I made the card "
                                 f"({error}). Nothing was bought or sold. Ask me and I'll make a fresh one."])
    if not claimed:
        return _current(led, pid)
    if held:
        from lib.desk import outbox
        now_dt = now_dt or store.parse_ts(now)
        when = market.fmt_when_et(held[0], now_dt)
        return _result(f"Approved. I send it at {when}.", label=None, ok=True, proposal_id=pid,
                       held=not_before, buttons=outbox.held_buttons(pid, prop["nonce"], held_label(held[0], now_dt)),
                       messages=[f"Approved #{pid}. It sends at {when}. Say cancel before then to stop it."])
    return _result("Approved", label="Approved", ok=True, proposal_id=pid, touch=["broker"],
                   messages=["On it. Checking the price now."])


def held_label(send_dt, now_dt) -> str:
    """The label button of a held card: 'Approved, sends 9:40 AM ET' (with the weekday on
    another day)."""
    t, n = send_dt.astimezone(market.ET), now_dt.astimezone(market.ET)
    day = "" if t.date() == n.date() else t.strftime("%a ")
    return f"Approved, sends {day}{market.fmt_et(t)} ET"


def _held_tap(led, prop: dict, action: str, from_id, now_dt) -> Optional[dict]:
    """A tap on a held trade. Before the send time: Approve answers with the send time and
    keeps the buttons (the label and Cancel); Reject cancels. At or after the send time:
    Reject answers that it is too late, in a popup. None when the trade was not held, or
    for an Approve once the send time came (the caller answers with the state)."""
    pid = prop["id"]
    with led.connect() as conn:
        row = conn.execute("SELECT status, not_before FROM broker_jobs WHERE kind='execute' AND proposal_id=? "
                           "AND recovery=0 AND not_before IS NOT NULL ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if row is None or row[0] not in ("queued", "running"):
        return None
    send = store.parse_ts(row[1])
    if row[0] == "queued" and now_dt < send:
        if action == "r":
            return _cancel_held(led, prop, from_id, store.iso(now_dt))
        from lib.desk import outbox
        return _result(f"Already approved. I send it at {market.fmt_when_et(send, now_dt)}.", label=None,
                       proposal_id=pid, buttons=outbox.held_buttons(pid, prop["nonce"], held_label(send, now_dt)))
    if action == "r":
        return _result("Too late to cancel: it is going to Robinhood now.", label="Approved", proposal_id=pid,
                       alert=True)
    return None


class _NotHeld(Exception):
    """No held job to cancel: the transaction rolls back."""


def _cancel_held(led, prop: dict, from_id, now: str) -> Optional[dict]:
    """Reject on a held trade before its send time: the job, the proposal and the legs end
    in one transaction. Returns None when no held job waits (an approval that goes at once
    is never cancelled: §10.5 check 5 keeps an approved row as it is)."""
    pid = prop["id"]
    try:
        with led.transaction() as conn:
            job = conn.execute("UPDATE broker_jobs SET status='failed', error='cancelled by the owner', "
                               "finished_at=? WHERE kind='execute' AND proposal_id=? AND recovery=0 AND "
                               "status='queued' AND not_before IS NOT NULL AND not_before > ?", (now, pid, now))
            if job.rowcount != 1:
                raise _NotHeld
            res = store.canonical_json({**(store.loads(prop.get("result_json"), {}) or {}),
                                        "cancelled_at": now, "approved_at": prop.get("decided_at")})
            cur = conn.execute("UPDATE proposals SET state='rejected', decided_at=?, decided_by=?, result_json=? "
                               "WHERE id=? AND state='approved' AND nonce=?",
                               (now, str(from_id), res, pid, prop["nonce"]))
            if cur.rowcount != 1:
                raise _NotHeld
            conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason='cancelled' "
                         "WHERE proposal_id=? AND state='approved'", (pid,))
    except _NotHeld:
        return None
    return _result("Cancelled", label="Cancelled", ok=True, proposal_id=pid,
                   messages=[f"You cancelled #{pid}. I sent nothing to Robinhood."])


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
    elif fu.get("why"):
        # The plan changed and no trade follows. Say why, so the owner never waits for a card.
        out["messages"].append(f"The plan is changed. No trade follows now: {fu['why']}.")
    return out


# --- a decision in the owner's own words (owner, 2026-11-09) ---------------------------
#
# "If I say 'yes, approve', it's approved." A typed reply decides a card like a tap does.
# Python reads the owner's stored message and decides the intent; a model never does. The
# bridge runs ``handle_words`` on each owner message before the desk turn, and the desk's
# ``approve_card`` tool reaches it only through ``Bridge.approve_by_words``, with the id of
# an owner message of the current turn. Either way the owner's own text must say yes (or
# no), with no "but", "wait" or question in it, and the card must be in the chat before
# that message. The checks after that are the tap's checks 2 to 6.

_YES = {"yes", "yeah", "yea", "yep", "yup", "y", "ok", "okay", "k", "kk", "sure", "go", "do", "send", "place",
        "lgtm", "approve", "approved", "confirm", "confirmed", "execute", "👍", "✅", "👌"}
_STRONG_YES = {"approve", "approved", "confirm", "confirmed", "execute"}
# "ok" or "sure" alone often means "I hear you". The bridge decides on it only when another
# yes word comes with it ("ok send it"); the desk's approve_card may still use it.
_SOFT_YES = {"ok", "okay", "k", "kk", "sure", "👌"}
_NO = {"no", "nope", "nah", "n", "reject", "rejected", "cancel", "cancelled", "canceled", "kill", "❌", "👎"}
_STRONG_NO = {"reject", "rejected", "cancel", "cancelled", "canceled", "kill"}
# A word that turns a yes into a condition or a change: the desk reads such a message.
_HEDGE = {"not", "dont", "don't", "never", "wait", "hold", "but", "except", "without", "drop", "instead", "change",
          "less", "more", "only", "if", "unless", "first", "after", "before", "later", "tomorrow", "maybe",
          "half", "smaller", "bigger", "skip", "remove", "keep"}
_FILLER = {"it", "that", "this", "one", "the", "card", "trade", "order", "orders", "please", "pls", "plz", "now",
           "ahead", "lets", "let's", "sounds", "good", "great", "perfect", "fine", "thanks", "thank", "you", "ty",
           "and", "all", "them", "both", "on", "for", "me", "i", "is", "a", "just", "go", "do", "send", "it's",
           "its", "fucking", "fuckin", "fuck", "rebalance", "buy", "sell", "sure", "hell", "yes", "already", "man",
           "dude", "bro", "sir", "so", "then", "right", "away", "asap", "everything", "whole", "thing", "lol",
           "cool", "nice", "alright", "aight", "got", "with", "to", "go-ahead"}
_TOKEN = re.compile(r"#?\d+|[a-z']+|[^\w\s#]", re.UNICODE)


def words_intent(text: str) -> dict:
    """Read a decision from the owner's own words. Returns ``{"intent": "approve"|"reject"|
    None, "strong": bool, "pure": bool, "numbers": [int]}``. ``pure`` is True when the message
    holds only decision words, filler and card numbers. A question, a hedge word, or a yes
    and a no together give no intent."""
    raw = str(text or "").strip().lower()
    toks = [t for t in _TOKEN.findall(raw) if t.strip() and t not in {".", ",", "!", ":", ";", "-", "—"}]
    nums = [int(t.lstrip("#")) for t in toks if t.lstrip("#").isdigit()]
    words = [t for t in toks if not t.lstrip("#").isdigit()]
    out = {"intent": None, "strong": False, "pure": False, "numbers": nums}
    if not words or "?" in raw or len(words) > 14:
        return out
    yes = {w for w in words if w in _YES}
    no = {w for w in words if w in _NO}
    if bool(yes) == bool(no) or any(w in _HEDGE for w in words):
        return out
    extra = [w for w in words if w not in _YES and w not in _NO and w not in _FILLER]
    out["pure"] = not extra
    if yes:
        out["intent"], out["strong"] = "approve", bool(yes & _STRONG_YES)
    else:
        out["intent"], out["strong"] = "reject", bool(no & _STRONG_NO)
    if not out["pure"] and not out["strong"]:
        out["intent"] = None
    return out


def _owner_message(conn, chat_id, message_id) -> Optional[dict]:
    row = conn.execute("SELECT * FROM chat_messages WHERE role='owner' AND chat_id=? AND tg_message_id=? "
                       "ORDER BY id DESC LIMIT 1", (str(chat_id), int(message_id))).fetchone()
    return dict(row) if row else None


def _held_job(conn, pid: int, now: str):
    return conn.execute("SELECT not_before FROM broker_jobs WHERE kind='execute' AND proposal_id=? AND recovery=0 "
                        "AND status='queued' AND not_before IS NOT NULL AND not_before > ? ORDER BY id DESC LIMIT 1",
                        (pid, now)).fetchone()


def _word_targets(conn, intent: str, owner_mid: int, now: str) -> list:
    """The cards a decision can mean: pending cards in the chat before the owner's message,
    and for a no, also approved trades still held for the open."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM proposals WHERE state='pending' AND tg_message_id IS NOT NULL AND tg_message_id < ? "
        "ORDER BY tg_message_id", (int(owner_mid),)).fetchall()]
    if intent == "reject":
        for r in conn.execute("SELECT * FROM proposals WHERE state='approved' AND kind='trade' AND "
                              "tg_message_id IS NOT NULL AND tg_message_id < ? ORDER BY tg_message_id",
                              (int(owner_mid),)).fetchall():
            if _held_job(conn, r["id"], now):
                rows.append(dict(r))
    return rows


def handle_words(led, cfg: Config, *, chat_id, owner_message_id, from_id, owner_ids, proposal_id=None,
                 reply_to=None, from_desk: bool = False, now_dt=None, yf_price_fn=None) -> Optional[dict]:
    """Decide a card from the owner's typed reply. Returns None when the message is not a
    decision Python can read (the desk reads it as talk), else a result like ``handle_tap``
    with ``card_message_id`` (the card to relabel) and ``by_words`` set.

    From the bridge (``from_desk`` False) a weak word ("yes", "ok", "no") decides only when
    the card is the last thing the desk sent: a desk message after the card may have asked
    something else. From the desk's ``approve_card`` the desk names the card, so the weak
    word counts; Python still needs the yes in the owner's own text."""
    now_dt = now_dt or store.utc_now()
    now = store.iso(now_dt)
    if str(from_id) not in {str(x) for x in (owner_ids or ())}:
        return None
    with led.connect() as conn:
        msg = _owner_message(conn, chat_id, owner_message_id)
        if msg is None:
            return None
        intent = words_intent(msg["text"])
        if intent["intent"] is None:
            return None
        cands = _word_targets(conn, intent["intent"], owner_message_id, now)
        by_id = {c["id"]: c for c in cands}
        target = None
        named = [n for n in intent["numbers"] if n in by_id]
        if proposal_id is not None:
            target = by_id.get(int(proposal_id))
            if target is None:
                return _result(f"#{proposal_id} is not a card open in the chat before that message.",
                               proposal_id=int(proposal_id), by_words=True, refused=True)
        elif len(set(named)) == 1:
            target = by_id[named[0]]
        elif intent["numbers"]:
            return None
        elif reply_to is not None:
            target = next((c for c in cands if int(c["tg_message_id"]) == int(reply_to)), None)
        elif len(cands) == 1:
            target = cands[0]
        if target is None:
            return None
        if not from_desk and not intent["strong"] and not named:
            # A weak word: the card must be the desk's last word in the chat.
            later = conn.execute("SELECT 1 FROM chat_messages WHERE role='desk' AND source='chat' AND chat_id=? "
                                 "AND tg_message_id > ? AND tg_message_id < ? LIMIT 1",
                                 (str(chat_id), int(target["tg_message_id"]), int(owner_message_id))).fetchone()
            if later:
                return None
        if not from_desk and not intent["pure"] and not intent["strong"]:
            return None
        if not from_desk and intent["intent"] == "approve" and not intent["strong"]:
            words_ = set(_TOKEN.findall(str(msg["text"] or "").lower()))
            if not (words_ & (_YES - _SOFT_YES)):
                return None
    pid = target["id"]
    card_mid = target["tg_message_id"]
    if intent["intent"] == "reject":
        if target["state"] == "approved":
            res = _cancel_held(led, target, from_id, now)
            if res is None:
                res = _result("Too late to cancel: it is going to Robinhood now.", proposal_id=pid)
        elif now_dt >= store.parse_ts(target["expires_at"]):
            with led.transaction() as conn:
                proposals.end_pending(conn, pid, "expired", now)
            res = _result("Expired", label="Expired", proposal_id=pid,
                          messages=[f"#{pid} expired before your reply, so nothing changed."])
        else:
            res = _reject(led, target, from_id, now)
            if res.get("ok"):
                res["messages"] = [f"Rejected #{pid}. Nothing was sent."]
    else:
        if now_dt >= store.parse_ts(target["expires_at"]):
            with led.transaction() as conn:
                proposals.end_pending(conn, pid, "expired", now)
            res = _result("Expired", label="Expired", proposal_id=pid,
                          messages=[f"#{pid} expired before your reply. Ask me for fresh numbers."])
        elif target["kind"] == "trade":
            held = held_send_et(now_dt)
            why = trade_gate(led, cfg, target, now_dt, held)
            if why:
                res = _result(why, proposal_id=pid, messages=[why])
            else:
                res = _approve_trade(led, cfg, target, from_id, now, now_dt=now_dt, held=held)
                if res.get("ok") and not held:
                    res["messages"] = [f"Approved #{pid}. Sending it now."]
        else:
            res = _confirm_change(led, cfg, target, from_id, now_dt, yf_price_fn)
            if res.get("ok") and not res.get("messages"):
                res["messages"] = [f"Done, #{pid} is applied."]
        if not res.get("ok") and not res.get("messages"):
            # Already decided, replaced or failed: say so in words, since no popup shows.
            res["messages"] = [f"#{pid}: {res.get('answer')}."]
    res["card_message_id"] = card_mid
    res["by_words"] = True
    res["intent"] = intent["intent"]
    return res
