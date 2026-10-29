"""The agenda: ``lib/desk/agenda.py`` (plan §4.8).

Background work waits on the agenda, and the bridge brings up one item at a time. This
suite holds the rules of the queue itself: the order, one open item, the job draft, the
priority rule, the refresh after a card ends, defer, hold, the fyi items, the desk's view,
and the plain text the bridge sends when a desk turn fails. The bridge side is in
``tests/test_chat_bridge.py`` (the agenda cases).

Run: .venv/bin/python tests/test_desk_agenda.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import Results, temp_root  # noqa: E402

from lib.desk import agenda, store  # noqa: E402

r = Results("desk-agenda")
T0 = "2026-08-05T14:00:00Z"
T1 = "2026-08-05T15:00:00Z"
T2 = "2026-08-05T18:00:00Z"


def prop(c, *, kind="change", state="pending", tg=None, ticker="ANET", expires="2026-08-05T20:00:00Z") -> int:
    """One proposals row with the fields agenda reads. (No card is built: agenda reads SQL only.)"""
    payload = {"kind": kind, "change": {"kind": "set_thesis", "ticker": ticker}} if kind == "change" else {"kind": "trade"}
    cur = c.execute("INSERT INTO proposals (kind, origin, payload_json, payload_sha256, rationale, evidence_json, "
                    "created_by, created_at, expires_at, state, nonce, tg_chat_id, tg_message_id) VALUES "
                    "(?, 'event', ?, 'x', 'r', '[]', 'analyst', ?, ?, ?, 'n0nce00000', '111', ?)",
                    (kind, store.canonical_json(payload), T0, expires, state, tg))
    pid = int(cur.lastrowid)
    if kind == "trade":
        c.execute("INSERT INTO orders (ref_id, trade_date, ticker, side, dollar_amount, state, proposal_id, leg_seq) "
                  "VALUES (?, '2026-08-05', ?, 'buy', 120.0, 'proposed', ?, 0)", (f"ref-{pid}", ticker, pid))
    return pid


def set_state(c, pid, state):
    c.execute("UPDATE proposals SET state=? WHERE id=?", (state, pid))


def statuses(c):
    return [(x[0], x[1]) for x in c.execute("SELECT id, status FROM agenda ORDER BY id").fetchall()]


def case_order_and_one_open():
    with temp_root() as root:
        with root.led().connect() as c:
            fyi = agenda.add(c, kind="report", priority=agenda.FYI, title="AAPL news, no change.", now=T0)
            nxt1 = agenda.add(c, kind="report", priority=agenda.NEXT, title="GEV review.", now=T0)
            nxt2 = agenda.add(c, kind="brief", priority=agenda.NEXT, title="The brief.", now=T1)
            now_ = agenda.add(c, kind="drift", priority=agenda.NOW, title="Drift.", now=T1)
            r.eq("order: due = now first, then next by age; fyi is never due",
                 [x["id"] for x in agenda.due(c, T2)], [now_, nxt1, nxt2])
            first = agenda.take_next(c, T2)
            r.eq("order: take_next opens the now item", (first["id"], first["status"], first["opened_at"]),
                 (now_, "open", T2))
            r.eq("one open: take_next is None while an item is open", agenda.take_next(c, T2), None)
            try:
                c.execute("UPDATE agenda SET status='open' WHERE id=?", (nxt1,))
                second = "allowed"
            except sqlite3.IntegrityError:
                second = "refused"
            r.eq("one open: the unique index refuses a second open item", second, "refused")
            r.ok("close: the open item closes", agenda.close(c, now_, reason="owner tapped", now=T2))
            r.eq("close: a closed item does not close twice", agenda.close(c, now_, reason="x", now=T2), False)
            r.eq("order: then the oldest next item", agenda.take_next(c, T2)["id"], nxt1)
            agenda.close(c, nxt1, reason="done", now=T2)
            r.eq("order: then the brief", agenda.take_next(c, T2)["id"], nxt2)
            agenda.close(c, nxt2, reason="done", now=T2)
            r.eq("fyi: nothing more comes up (the fyi item waits)", agenda.take_next(c, T2), None)
            r.eq("fyi: the fyi item still waits", agenda.get(c, fyi)["status"], "waiting")
            r.eq("close_fyi: closes the waiting fyi items", agenda.close_fyi(c, now=T2, reason="the brief"), 1)
            r.eq("close_fyi: done with the reason", (agenda.get(c, fyi)["status"], agenda.get(c, fyi)["close_reason"]),
                 ("done", "the brief"))


def case_job_draft():
    with temp_root() as root:
        with root.led().connect() as c:
            d = agenda.job_draft(c, 7, now=T0)
            r.eq("draft: made on first use", (d["status"], d["job_id"], d["priority"]), ("draft", 7, agenda.FYI))
            r.eq("draft: a second call reuses it", agenda.job_draft(c, 7, now=T1)["id"], d["id"])
            r.ok("draft: set_report stores the text", agenda.set_report(c, 7, "First try.", now=T0))
            r.ok("draft: a second report replaces the first", agenda.set_report(c, 7, "The report.", now=T0))
            pid = prop(c)
            agenda.attach(c, d["id"], pid, card={"legs": [{"ticker": "ANET", "weight_now": 5.1}]})
            agenda.attach(c, d["id"], pid)
            got = agenda.get(c, d["id"])
            r.eq("draft: attach adds a proposal once and keeps the card inputs",
                 (got["proposal_ids"], got["cards"]), ([pid], {str(pid): {"legs": [{"ticker": "ANET", "weight_now": 5.1}]}}))
            r.eq("draft: a draft never comes up", agenda.take_next(c, T2), None)
            iid = agenda.finish_job(c, 7, kind="report", priority=agenda.NEXT, title="ANET review.", ticker="ANET",
                                    now=T1)
            got = agenda.get(c, iid)
            r.eq("finish: waiting, with the title, priority and ready time",
                 (got["status"], got["priority"], got["title"], got["body"], got["created_at"]),
                 ("waiting", agenda.NEXT, "ANET review.", "The report.", T1))
            r.eq("finish: the report is final", agenda.set_report(c, 7, "Late.", now=T1), False)
            r.eq("finish: a second finish changes nothing", agenda.finish_job(
                c, 7, kind="failure", priority=agenda.NOW, title="x", now=T2), iid)
            r.eq("finish: still the first result", agenda.get(c, iid)["kind"], "report")
            # A draft with nothing in it ends done; a draft with no report takes body_if_empty.
            e = agenda.finish_job(c, 8, kind="report", priority=agenda.FYI, title="Empty.", now=T1)
            r.eq("finish: no text and no card -> done", (agenda.get(c, e)["status"], agenda.get(c, e)["close_reason"]),
                 ("done", "nothing to report"))
            f = agenda.finish_job(c, 9, kind="report", priority=agenda.FYI, title="AAPL.", now=T1,
                                  body_if_empty="The verdict summary.")
            r.eq("finish: body_if_empty fills a draft with no report",
                 (agenda.get(c, f)["status"], agenda.get(c, f)["body"]), ("waiting", "The verdict summary."))


def case_priority():
    with temp_root() as root:
        with root.led().connect() as c:
            c.execute("INSERT INTO companies (ticker, kind, status, added_at, updated_at) VALUES "
                      "('ANET', 'company', 'holding', ?, ?), ('AVGO', 'company', 'watch', ?, ?)", (T0, T0, T0, T0))
            trade, change, gone = prop(c, kind="trade"), prop(c), prop(c, state="expired")
            jp = agenda.job_priority
            r.eq("priority: a pending trade card -> now", jp(c, proposal_ids=[change, trade]), agenda.NOW)
            r.eq("priority: a pending change card -> next", jp(c, proposal_ids=[change]), agenda.NEXT)
            r.eq("priority: an ended card counts for nothing -> fyi", jp(c, proposal_ids=[gone]), agenda.FYI)
            r.eq("priority: no card, intact -> fyi", jp(c, proposal_ids=[], verdict="intact", ticker="ANET"), agenda.FYI)
            r.eq("priority: strengthened, no card -> fyi", jp(c, proposal_ids=[], verdict="strengthened",
                                                             ticker="ANET"), agenda.FYI)
            r.eq("priority: weakened on a holding -> now", jp(c, proposal_ids=[], verdict="weakened", ticker="ANET"),
                 agenda.NOW)
            r.eq("priority: broken on a name we do not hold -> next",
                 jp(c, proposal_ids=[], verdict="broken", ticker="AVGO"), agenda.NEXT)
            r.eq("priority: a deep-dive the owner asked for -> now",
                 jp(c, proposal_ids=[], deep_dive=True, requested_by="desk"), agenda.NOW)
            r.eq("priority: a review deep-dive -> next",
                 jp(c, proposal_ids=[change], deep_dive=True, requested_by="desk-review"), agenda.NEXT)
            r.eq("priority: a review with a trade card -> now",
                 jp(c, proposal_ids=[trade], deep_dive=True, requested_by="desk-review"), agenda.NOW)


def case_refresh():
    with temp_root() as root:
        with root.led().connect() as c:
            p_open, p_wait_exp, p_wait_rej, p_pending, p_two_a, p_two_b = (prop(c, tg=901), prop(c), prop(c),
                                                                           prop(c), prop(c, tg=902), prop(c))
            a = agenda.add(c, kind="report", priority=agenda.NEXT, title="A", now=T0, proposal_ids=[p_open])
            b = agenda.add(c, kind="report", priority=agenda.NEXT, title="B", now=T0, proposal_ids=[p_wait_exp])
            d = agenda.add(c, kind="report", priority=agenda.NEXT, title="D", now=T0, proposal_ids=[p_wait_rej])
            e = agenda.add(c, kind="report", priority=agenda.NEXT, title="E", now=T0, proposal_ids=[p_pending])
            f = agenda.add(c, kind="report", priority=agenda.NEXT, title="F", now=T0)
            g = agenda.add(c, kind="report", priority=agenda.NEXT, title="G", now=T0, proposal_ids=[p_two_a, p_two_b])
            c.execute("UPDATE agenda SET status='open', opened_at=? WHERE id=?", (T0, a))
            r.eq("refresh: nothing ends while every card is pending", agenda.refresh(c, T1), [])
            set_state(c, p_open, "applied")
            set_state(c, p_wait_exp, "expired")
            set_state(c, p_wait_rej, "rejected")
            set_state(c, p_two_a, "applied")
            out = agenda.refresh(c, T1)
            r.eq("refresh: open + applied -> done; waiting + expired -> dropped; waiting + rejected -> done",
                 sorted(out), sorted([(a, "done"), (b, "dropped"), (d, "done")]))
            r.eq("refresh: the reasons name the cards",
                 [agenda.get(c, x)["close_reason"] for x in (a, b)],
                 [f"its cards ended (#{p_open} applied)", f"its cards ended before it came up (#{p_wait_exp} expired)"])
            r.eq("refresh: a pending card keeps an item; an item with no card stays; one of two cards left keeps it",
                 [agenda.get(c, x)["status"] for x in (e, f, g)], ["waiting", "waiting", "waiting"])
            r.eq("unsent_cards: the pending card with no message", agenda.unsent_cards(c, agenda.get(c, g)), [p_two_b])
            r.eq("item_of_proposal: finds the live item", agenda.item_of_proposal(c, p_two_b)["id"], g)
            r.eq("item_of_proposal: none for a closed item", agenda.item_of_proposal(c, p_open), None)


def case_defer_and_hold():
    with temp_root() as root:
        with root.led().connect() as c:
            a = agenda.add(c, kind="report", priority=agenda.NEXT, title="A", now=T0)
            b = agenda.add(c, kind="report", priority=agenda.NEXT, title="B", now=T0)
            r.eq("defer: take_next opens A", agenda.take_next(c, T0)["id"], a)
            until = agenda.defer(c, a, hours=2, now=T0)
            r.eq("defer: A goes back in line until 2 h later", (until, agenda.get(c, a)["status"],
                                                                agenda.get(c, a)["opened_at"]),
                 ("2026-08-05T16:00:00Z", "waiting", None))
            r.eq("defer: B comes up first", agenda.take_next(c, T1)["id"], b)
            agenda.close(c, b, reason="done", now=T1)
            r.eq("defer: A is not due before its time", agenda.take_next(c, T1), None)
            r.eq("defer: A is due after its time", agenda.take_next(c, T2)["id"], a)
            r.eq("defer: a closed item cannot be deferred", agenda.defer(c, 999, hours=1, now=T2), None)
            agenda.hold(c, T1)
            r.eq("hold: in force before its time", agenda.hold_until(c, T0), T1)
            r.eq("hold: over after its time", agenda.hold_until(c, T2), None)
            agenda.hold(c, None)
            r.eq("hold: None lifts it", agenda.hold_until(c, T0), None)


def case_view_and_text():
    with temp_root() as root:
        with root.led().connect() as c:
            r.eq("view: an empty agenda has no block", agenda.block_lines(agenda.view(c, T0)), [])
            card = prop(c, kind="trade", ticker="GEV")
            o = agenda.add(c, kind="drift", priority=agenda.NOW, title="The account drifted: GEV.", now=T0,
                           proposal_ids=[card])
            agenda.take_next(c, T0)
            agenda.add(c, kind="report", priority=agenda.NEXT, title="VRT review.", now=T0)
            agenda.add(c, kind="report", priority=agenda.FYI, title="AAPL news, no change.", now=T0)
            agenda.hold(c, T1)
            v = agenda.view(c, T0)
            r.eq("view: the open item and its card", (v["open"]["item"], v["open"]["cards"][0]["id"],
                                                     v["open"]["cards"][0]["what"], v["open"]["cards"][0]["in_chat"]),
                 (o, card, "buy $120.00 GEV", False))
            r.eq("view: one waiting, one fyi", (len(v["waiting"]), v["fyi_count"]), (1, 1))
            lines = agenda.block_lines(v)
            r.ok("block: names the open item, the waiting item, the fyi item and the hold",
                 lines[0].startswith(f"open: item {o} (now) The account drifted: GEV.")
                 and "waiting: 1" in lines and any("VRT review." in x for x in lines)
                 and any("AAPL news" in x for x in lines) and lines[-1].startswith("on hold"), lines)
            r.eq("counts: for /status", agenda.counts(c), {"open": 1, "waiting": 1, "fyi": 1})
    long_para = "word " * 300
    msgs = agenda.plain_messages({"body": "One.\n\nTwo.\n\n" + long_para, "title": "T"}, limit=400)
    r.ok("plain: whole short paragraphs share a message", msgs[0] == "One.\n\nTwo.", msgs[:1])
    r.ok("plain: no message is over the limit", all(len(m) <= 400 for m in msgs), [len(m) for m in msgs])
    r.eq("plain: no word is lost", " ".join(msgs).split(), ("One. Two. " + long_para).split())
    r.eq("plain: an empty body falls back to the title", agenda.plain_messages({"body": "", "title": "The title."}),
         ["The title."])


def case_review_fixes():
    """The gate fixes of 2026-10-29: no silent drop, recovery of an unseen open item, set aside."""
    with temp_root() as root:
        with root.led().connect() as c:
            p_rep, p_drift = prop(c, kind="trade"), prop(c, kind="trade")
            rep = agenda.add(c, kind="report", priority=agenda.NOW, title="ANET news.", body="Arista crossed.",
                             now=T0, proposal_ids=[p_rep])
            dr = agenda.add(c, kind="drift", priority=agenda.NOW, title="Drift.", body="Moved.", now=T0,
                            proposal_ids=[p_drift])
            set_state(c, p_rep, "expired")
            set_state(c, p_drift, "expired")
            r.eq("keep: a report whose card expired before it came up stays; a drift item drops",
                 sorted(agenda.refresh(c, T1)), sorted([(rep, "kept"), (dr, "dropped")]))
            it = agenda.get(c, rep)
            r.eq("keep: the report waits as next, with no card, and a note", (it["status"], it["priority"],
                 it["proposal_ids"], it["body"].startswith("Arista crossed.")), ("waiting", agenda.NEXT, [], True))
            r.ok("keep: the note names the card and what happened", f"#{p_rep} expired" in it["body"], it["body"])
            r.eq("keep: a second refresh changes nothing", agenda.refresh(c, T1), [])
            # recover: an open item the owner never saw goes back in line; a seen one stays.
            agenda.take_next(c, T1)
            r.eq("recover: an unseen open item goes back in line", (agenda.recover(c), agenda.get(c, rep)["status"]),
                 (rep, "waiting"))
            agenda.take_next(c, T1)
            agenda.mark_presented(c, rep)
            r.eq("recover: a seen open item stays open", (agenda.recover(c), agenda.get(c, rep)["status"]),
                 (None, "open"))
            # set_aside: with a pending card back in line; with none, done.
            r.eq("set_aside: no pending card -> done", agenda.set_aside(c, rep, reason="x", now=T2), "done")
            p_live = prop(c, tg=950)
            live = agenda.add(c, kind="report", priority=agenda.NEXT, title="CEG.", body="b", now=T2,
                              proposal_ids=[p_live])
            agenda.take_next(c, T2)
            r.eq("set_aside: a pending card -> back in line", (agenda.set_aside(c, live, reason="x", now=T2),
                                                               agenda.get(c, live)["status"]), ("waiting", "waiting"))
            r.eq("pending_cards: the pending ones", agenda.pending_cards(c, agenda.get(c, live)), [p_live])


def main() -> int:
    for case in (case_order_and_one_open, case_job_draft, case_priority, case_refresh, case_defer_and_hold,
                 case_view_and_text, case_review_fixes):
        try:
            case()
        except Exception as e:  # noqa: BLE001 — one broken case must not hide the others
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
