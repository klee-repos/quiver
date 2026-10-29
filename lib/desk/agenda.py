"""The agenda: what the desk brings up with the owner, one item at a time.

No background process sends its own news to the owner. The analyst (a report and its
cards), the drift step (a trade card) and the close brief each put one item here. The chat
bridge brings up one item at a time in a desk turn, and sends the item's cards after the
desk's messages. The next item comes up only after the owner deals with the open one: a tap
that decides its last pending card, or a reply after which the desk calls ``close_item``.

Item states: ``draft`` (its job still runs), ``waiting``, ``open`` (brought up; a unique
index allows one), ``done`` and ``dropped``. A ``NOW`` (0) item comes up before a ``NEXT``
(1) item. An ``FYI`` (2) item never comes up by itself: the desk sees it in the
``<agenda>`` block, and the close brief covers it.

A reply to the owner's own action does not wait here: a tap answer, a fill, a requote and
the trade card after a Confirm go out at once.

This module reads ``proposals`` and ``orders`` with SQL only. It approves nothing and sends
nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from typing import Optional

from lib import market
from lib.desk import store

NOW, NEXT, FYI = 0, 1, 2
PRIORITY_TEXT = {NOW: "now", NEXT: "next", FYI: "fyi"}
HOLD_KEY = "agenda_hold_until"
PRESENTED_KEY = "agenda_presented"     # the id of the open item the owner has seen
BODY_MAX = 4000
TITLE_MAX = 300
# A proposal state that the owner set (or the broker set after an Approve).
_DECIDED = frozenset({"approved", "executing", "executed", "partially_executed", "failed", "requoted",
                      "applied", "rejected"})


def _item(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    d["proposal_ids"] = [int(x) for x in store.loads(d.get("proposal_ids"), []) or []]
    d["cards"] = store.loads(d.pop("cards_json", None), {}) or {}
    return d


def _items(conn, sql: str, args=()) -> list:
    return [_item(r) for r in conn.execute(sql, args).fetchall()]


def get(conn, item_id) -> Optional[dict]:
    return _item(conn.execute("SELECT * FROM agenda WHERE id=?", (int(item_id),)).fetchone())


def add(conn, *, kind: str, priority: int, title: str, now: str, body: str = "", ticker=None,
        event_id=None, proposal_ids=(), cards=None) -> int:
    """Put one ``waiting`` item on the agenda. Returns its id."""
    cur = conn.execute(
        "INSERT INTO agenda (kind, priority, status, ticker, title, body, event_id, proposal_ids, cards_json, "
        "created_at) VALUES (?, ?, 'waiting', ?, ?, ?, ?, ?, ?, ?)",
        (kind, int(priority), ticker, str(title)[:TITLE_MAX], str(body or "")[:BODY_MAX], event_id,
         json.dumps([int(p) for p in proposal_ids]), json.dumps(cards or {}, default=str), now))
    return int(cur.lastrowid)


# --- the draft of an analyst job -------------------------------------------------------

def job_draft(conn, job_id: int, *, now: str) -> dict:
    """The draft item of one analyst job, made on first use. A retry of the job reuses it."""
    conn.execute("INSERT INTO agenda (kind, priority, status, title, job_id, created_at) "
                 "VALUES ('report', ?, 'draft', '', ?, ?) ON CONFLICT DO NOTHING", (FYI, int(job_id), now))
    return _item(conn.execute("SELECT * FROM agenda WHERE job_id=?", (int(job_id),)).fetchone())


def set_report(conn, job_id: int, text: str, *, now: str) -> bool:
    """Store the analyst's report on the job's draft (a second call replaces the first).
    False when the draft already went out."""
    job_draft(conn, job_id, now=now)
    cur = conn.execute("UPDATE agenda SET body=? WHERE job_id=? AND status='draft'",
                       (str(text)[:BODY_MAX], int(job_id)))
    return cur.rowcount == 1


def attach(conn, item_id: int, proposal_id: int, *, card=None) -> None:
    """Add a proposal to an item. ``card`` holds a trade card's build-time inputs (its legs
    with the weights), so the card reads the same when it comes up later."""
    it = get(conn, item_id)
    if it is None:
        return
    ids = it["proposal_ids"]
    if int(proposal_id) not in ids:
        ids.append(int(proposal_id))
    cards = it["cards"]
    if card is not None:
        cards[str(int(proposal_id))] = card
    conn.execute("UPDATE agenda SET proposal_ids=?, cards_json=? WHERE id=?",
                 (json.dumps(ids), json.dumps(cards, default=str), it["id"]))


def finish_job(conn, job_id: int, *, kind: str, priority: int, title: str, now: str, ticker=None,
               event_id=None, body_if_empty: str = "") -> Optional[int]:
    """Move a job's draft to ``waiting`` with its title and priority. A draft with no
    report takes ``body_if_empty``. A draft with no text and no card ends ``done``: there
    is nothing to bring up. Returns the item id."""
    it = job_draft(conn, job_id, now=now)
    if it["status"] != "draft":
        return it["id"]
    body = it["body"] if str(it["body"] or "").strip() else str(body_if_empty or "")
    if not body.strip() and not it["proposal_ids"]:
        conn.execute("UPDATE agenda SET status='done', title=?, ticker=?, event_id=?, closed_at=?, "
                     "close_reason='nothing to report' WHERE id=?",
                     (str(title)[:TITLE_MAX], ticker, event_id, now, it["id"]))
        return it["id"]
    # created_at becomes the time the item is ready: the agenda brings items up in that order.
    conn.execute("UPDATE agenda SET kind=?, priority=?, status='waiting', title=?, ticker=?, event_id=?, body=?, "
                 "created_at=? WHERE id=? AND status='draft'",
                 (kind, int(priority), str(title)[:TITLE_MAX], ticker, event_id, body[:BODY_MAX], now, it["id"]))
    return it["id"]


# --- the priority rule ------------------------------------------------------------------

def _proposals(conn, ids) -> dict:
    ids = [int(i) for i in ids or []]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, kind, state, tg_message_id, expires_at, payload_json FROM proposals WHERE id IN ({marks})",
        ids).fetchall()}


def job_priority(conn, *, proposal_ids, verdict=None, ticker=None, deep_dive=False,
                 requested_by=None) -> int:
    """The one priority rule for an analyst job.

    NOW:  a pending trade card; a deep-dive the desk queued for the owner; a weaker or broken
          thesis on a name the owner holds.
    NEXT: a pending change card; any other deep-dive; a weaker or broken thesis on a name the
          owner does not hold.
    FYI:  everything else (a news check that changed nothing)."""
    pending = [p for p in _proposals(conn, proposal_ids).values() if p["state"] == "pending"]
    out = FYI
    if any(p["kind"] == "trade" for p in pending):
        out = NOW
    elif pending:
        out = NEXT
    if deep_dive:
        out = min(out, NOW if requested_by == "desk" else NEXT)
    if verdict in ("weakened", "broken"):
        held = conn.execute("SELECT 1 FROM companies WHERE ticker=? AND status='holding'",
                            (str(ticker or ""),)).fetchone()
        out = min(out, NOW if held else NEXT)
    return out


# --- bringing items up ----------------------------------------------------------------------

def open_item(conn) -> Optional[dict]:
    return _item(conn.execute("SELECT * FROM agenda WHERE status='open' LIMIT 1").fetchone())


def due(conn, now: str) -> list:
    """The waiting items that may come up now, in order: priority, then age."""
    return _items(conn, "SELECT * FROM agenda WHERE status='waiting' AND priority < ? AND "
                        "(not_before IS NULL OR not_before <= ?) ORDER BY priority, created_at, id", (FYI, now))


def take_next(conn, now: str) -> Optional[dict]:
    """Open the next due item and return it. None when an item is open or none is due."""
    refresh(conn, now)
    if open_item(conn) is not None:
        return None
    for it in due(conn, now):
        try:
            cur = conn.execute("UPDATE agenda SET status='open', opened_at=? WHERE id=? AND status='waiting'",
                               (now, it["id"]))
        except sqlite3.IntegrityError:  # another item opened first (the unique index)
            return None
        if cur.rowcount == 1:
            return get(conn, it["id"])
    return None


def close(conn, item_id: int, *, reason: str, now: str, status: str = "done") -> bool:
    cur = conn.execute("UPDATE agenda SET status=?, closed_at=?, close_reason=? WHERE id=? "
                       "AND status IN ('waiting','open')", (status, now, str(reason or "")[:300], int(item_id)))
    return cur.rowcount == 1


def defer(conn, item_id: int, *, hours: float, now: str) -> Optional[str]:
    """Put an item back in line; it may not come up for ``hours``. Returns the time."""
    until = store.iso(store.parse_ts(now) + timedelta(hours=float(hours)))
    cur = conn.execute("UPDATE agenda SET status='waiting', opened_at=NULL, not_before=? WHERE id=? "
                       "AND status IN ('waiting','open')", (until, int(item_id)))
    return until if cur.rowcount == 1 else None


def hold(conn, until: Optional[str]) -> None:
    """No item comes up by itself before ``until`` (None lifts the hold)."""
    store.set_state(conn, HOLD_KEY, until or "")


def hold_until(conn, now: str) -> Optional[str]:
    v = store.get_state(conn, HOLD_KEY) or ""
    return v if v and v > now else None


def item_of_proposal(conn, proposal_id: int) -> Optional[dict]:
    """The draft, waiting or open item that holds a proposal."""
    for it in _items(conn, "SELECT * FROM agenda WHERE status IN ('draft','waiting','open') ORDER BY id DESC"):
        if int(proposal_id) in it["proposal_ids"]:
            return it
    return None


def refresh(conn, now: str) -> list:
    """Settle each live item whose cards all ended. An open item, or one with a card the
    owner decided, ends ``done``. A waiting item whose cards expired, were replaced or were
    withdrawn before it came up keeps its report: the cards leave the item, a note says what
    happened to them, and a ``now`` item becomes ``next`` (its trade is gone). A waiting drift
    item has nothing left to say and ends ``dropped``. An item with no card stays as it is.
    Returns ``[(item id, status)]``."""
    out = []
    for it in _items(conn, "SELECT * FROM agenda WHERE status IN ('waiting','open') AND proposal_ids != '[]'"):
        props = _proposals(conn, it["proposal_ids"])
        states = [props[p]["state"] if p in props else "missing" for p in it["proposal_ids"]]
        if "pending" in states:
            continue
        ended = ", ".join(f"#{p} {s}" for p, s in zip(it["proposal_ids"], states))
        if it["status"] == "open" or any(s in _DECIDED for s in states):
            close(conn, it["id"], reason=f"its cards ended ({ended})", now=now)
            out.append((it["id"], "done"))
        elif it["kind"] == "drift" or not str(it["body"] or "").strip():
            close(conn, it["id"], reason=f"its cards ended before it came up ({ended})", now=now, status="dropped")
            out.append((it["id"], "dropped"))
        else:
            note = (f"(The card for this, {ended}, ended before it came up. "
                    "Ask the desk to propose it again if it still fits.)")
            conn.execute("UPDATE agenda SET proposal_ids='[]', cards_json='{}', priority=MAX(priority, ?), "
                         "body=? WHERE id=?", (NEXT, (str(it["body"]).rstrip() + "\n\n" + note)[:BODY_MAX], it["id"]))
            out.append((it["id"], "kept"))
    return out


def mark_presented(conn, item_id) -> None:
    """The owner has seen the open item (its turn ended, or the bridge sent it as written)."""
    store.set_state(conn, PRESENTED_KEY, str(int(item_id)))


def recover(conn) -> Optional[int]:
    """At bridge start: an open item the owner never saw (the bridge stopped during its
    turn) goes back in line, so it comes up again. Returns its id."""
    op = open_item(conn)
    if op is None or store.get_state(conn, PRESENTED_KEY) == str(op["id"]):
        return None
    conn.execute("UPDATE agenda SET status='waiting', opened_at=NULL WHERE id=? AND status='open'", (op["id"],))
    return op["id"]


def pending_cards(conn, item: Optional[dict]) -> list:
    """The pending proposals of an item."""
    if not item:
        return []
    props = _proposals(conn, item["proposal_ids"])
    return [p for p in item["proposal_ids"] if p in props and props[p]["state"] == "pending"]


def set_aside(conn, item_id: int, *, reason: str, now: str) -> str:
    """Take the open item off the table so another can come up. With a pending card it goes
    back in line (the card stays in the chat with its buttons); with none it ends ``done``.
    Returns the new status."""
    it = get(conn, item_id)
    if it is None or it["status"] != "open":
        return "none"
    if pending_cards(conn, it):
        conn.execute("UPDATE agenda SET status='waiting', opened_at=NULL WHERE id=? AND status='open'", (it["id"],))
        return "waiting"
    close(conn, it["id"], reason=reason, now=now)
    return "done"


def unsent_cards(conn, item: Optional[dict]) -> list:
    """The pending proposals of an item whose card is not in the chat yet."""
    if not item:
        return []
    props = _proposals(conn, item["proposal_ids"])
    return [p for p in item["proposal_ids"] if p in props and props[p]["state"] == "pending"
            and props[p]["tg_message_id"] is None]


def close_fyi(conn, *, now: str, reason: str) -> int:
    """Close every waiting fyi item (the close brief covers them)."""
    return conn.execute("UPDATE agenda SET status='done', closed_at=?, close_reason=? WHERE status='waiting' "
                        "AND priority=?", (now, reason, FYI)).rowcount


# --- what the desk sees -------------------------------------------------------------------

def _clock(ts) -> str:
    dt = store.parse_ts(ts)
    return market.fmt_et(dt) + " ET" if dt else ""


def card_facts(conn, item: Optional[dict]) -> list:
    """One fact line for each card of an item: id, kind, what it does, state, and whether
    it is in the chat. For the desk, not for the owner."""
    if not item:
        return []
    props = _proposals(conn, item["proposal_ids"])
    out = []
    for pid in item["proposal_ids"]:
        p = props.get(pid)
        if p is None:
            continue
        if p["kind"] == "trade":
            legs = conn.execute("SELECT ticker, side, dollar_amount, quantity FROM orders WHERE proposal_id=? "
                                "ORDER BY leg_seq", (pid,)).fetchall()
            what = "; ".join(f"{l[1]} {'$%.2f' % l[2] if l[2] is not None else '%g shares' % (l[3] or 0)} {l[0]}"
                             for l in legs) or "a trade"
        else:
            ch = (store.loads(p["payload_json"], {}) or {}).get("change") or {}
            what = " ".join(x for x in (str(ch.get("kind") or "a change"), str(ch.get("ticker") or "")) if x)
        out.append({"id": pid, "kind": p["kind"], "what": what, "state": p["state"],
                    "in_chat": p["tg_message_id"] is not None, "open_until": _clock(p["expires_at"])})
    return out


def view(conn, now: str, *, fyi_limit: int = 8) -> dict:
    """The agenda as the desk sees it: the open item, the items in line, the fyi items."""
    def short(it):
        return {"item": it["id"], "priority": PRIORITY_TEXT.get(it["priority"], "?"), "kind": it["kind"],
                "ticker": it["ticker"], "title": it["title"], "since": _clock(it["created_at"]),
                "cards": card_facts(conn, it),
                **({"not_before": _clock(it["not_before"])} if it.get("not_before") and it["not_before"] > now
                   else {})}
    op = open_item(conn)
    line = _items(conn, "SELECT * FROM agenda WHERE status='waiting' AND priority < ? "
                        "ORDER BY priority, created_at, id", (FYI,))
    fyi = _items(conn, "SELECT * FROM agenda WHERE status='waiting' AND priority=? ORDER BY id DESC", (FYI,))
    out = {"open": None, "waiting": [short(x) for x in line], "fyi": [short(x) for x in fyi[:fyi_limit]],
           "fyi_count": len(fyi), "hold_until": None}
    if op is not None:
        out["open"] = {**short(op), "brought_up": _clock(op["opened_at"])}
    h = hold_until(conn, now)
    if h:
        out["hold_until"] = _clock(h)
    return out


def _cards_text(cards) -> str:
    parts = []
    for c in cards:
        where = "in the chat" if c["in_chat"] else "not sent yet"
        parts.append(f"card #{c['id']} ({c['kind']}: {c['what']}; {c['state']}, {where}, open until {c['open_until']})")
    return "; ".join(parts)


def block_lines(v: dict) -> list:
    """The ``<agenda>`` lines of a turn input. Empty when the agenda holds nothing."""
    if not (v["open"] or v["waiting"] or v["fyi"] or v["hold_until"]):
        return []
    lines = []
    if v["open"]:
        o = v["open"]
        lines.append(f"open: item {o['item']} ({o['priority']}) {o['title']} · brought up {o['brought_up']}"
                     + (f" · {_cards_text(o['cards'])}" if o["cards"] else ""))
    else:
        lines.append("open: none")
    lines.append(f"waiting: {len(v['waiting'])}")
    for w in v["waiting"]:
        lines.append(f"- item {w['item']} ({w['priority']}) {w['title']}"
                     + (f" · not before {w['not_before']}" if w.get("not_before") else "")
                     + (f" · {_cards_text(w['cards'])}" if w["cards"] else ""))
    if v["fyi_count"]:
        lines.append(f"fyi, not brought up ({v['fyi_count']}; the close brief covers them):")
        for f in v["fyi"]:
            lines.append(f"- item {f['item']} {f['title']}")
    if v["hold_until"]:
        lines.append(f"on hold: nothing comes up by itself before {v['hold_until']}")
    return lines


def plain_messages(item: dict, limit: int = 1200) -> list:
    """The item as plain messages, for the bridge to send when a desk turn fails. Each
    message holds whole paragraphs, at most ``limit`` characters."""
    text = str(item.get("body") or "").strip() or str(item.get("title") or "").strip()
    out, cur = [], ""
    for para in [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]:
        while len(para) > limit:
            cut = para.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        if cur and len(cur) + 2 + len(para) > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def counts(conn) -> dict:
    """``{"open": n, "waiting": n, "fyi": n}`` for ``/status``."""
    out = {"open": 0, "waiting": 0, "fyi": 0}
    for status, prio, n in conn.execute("SELECT status, priority, COUNT(*) FROM agenda WHERE status IN "
                                        "('waiting','open') GROUP BY status, priority").fetchall():
        if status == "open":
            out["open"] += n
        elif prio == FYI:
            out["fyi"] += n
        else:
            out["waiting"] += n
    return out
