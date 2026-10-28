"""The outbox: send to the owner and mirror each message (plan §4.4, §10.1 rule 3; test table
``test_desk_outbox.py``, case ``outbox.mirror``).

The transport is a recording fake ``transport(method, params) -> dict``. Its answers use only
the Bot API's documented minimal shapes: ``{"ok": true, "result": {"message_id": N}}`` and
``{"ok": false, "error_code": 400, "description": "Bad Request: can't parse entities: ..."}``.
The ``tg`` capture is pending. No network, no LLM, no broker.

Run: .venv/bin/python tests/test_desk_outbox.py
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import (FakeClock, FakePrices, Results, seed_desk, snapshot, temp_root,  # noqa: E402
                        TEST_BOOK)

from lib.desk import outbox, proposals, store  # noqa: E402

r = Results("desk-outbox")

CHAT = 555001
PARSE_ERR = {"ok": False, "error_code": 400,
             "description": "Bad Request: can't parse entities: Unsupported start tag \"foo\" at byte offset 0"}


class FakeTG:
    """A recording transport. ``script`` holds answers to return first, in order."""

    def __init__(self, start: int = 300):
        self.calls = []
        self.next_id = start
        self.script = []

    def __call__(self, method, params):
        self.calls.append((method, json.loads(json.dumps(params))))
        if self.script:
            return self.script.pop(0)
        if method == "sendMessage":
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": {"message_id": params.get("message_id")}}

    def sends(self):
        return [p for m, p in self.calls if m == "sendMessage"]


_TEMPLATE = {}


@contextmanager
def fresh_root(*, seeded=True):
    if seeded and not _TEMPLATE:
        with temp_root() as root:
            fc = FakeClock("2026-08-03T14:00:00Z").pin_market()
            try:
                seed_desk(root)
            finally:
                fc.unpin()
            _TEMPLATE["db"] = root.db.read_bytes()
    with temp_root() as root:
        if seeded:
            (root.path / "strategy.yaml").write_text(TEST_BOOK, encoding="utf-8")
            root.db.write_bytes(_TEMPLATE["db"])
        yield root


def desk_rows(root):
    with root.led().connect() as conn:
        return [dict(x) for x in conn.execute(
            "SELECT id, chat_id, tg_message_id, role, source, text, session_id, delivered_to_session "
            "FROM chat_messages ORDER BY id").fetchall()]


def box_for(root, tg, fc):
    return outbox.Outbox(root.led(), transport=tg, chat_id=CHAT, clock=fc.monotonic, sleep=fc.sleep)


def case_send():
    with fresh_root(seeded=False) as root:
        fc = FakeClock("2026-08-05T14:00:00Z").pin_market()
        try:
            tg = FakeTG()
            box = box_for(root, tg, fc)
            res = box.send("Hello <b>there</b> &amp; co. zebrafish", source="monitor")
            rows = desk_rows(root)
            p = tg.sends()[0]
            r.eq("mirror: one sendMessage", len(tg.sends()), 1)
            r.eq("mirror: HTML parse mode, owner chat, text as given",
                 (p.get("parse_mode"), p.get("chat_id"), p.get("text")),
                 ("HTML", str(CHAT), "Hello <b>there</b> &amp; co. zebrafish"))
            r.eq("mirror: the send answer", (res.get("ok"), res.get("message_id")), (True, 301))
            r.eq("mirror: one desk row", len(rows), 1)
            row = rows[0]
            r.eq("mirror: role, source, tg_message_id, delivered 0",
                 (row["role"], row["source"], row["tg_message_id"], row["delivered_to_session"], row["chat_id"]),
                 ("desk", "monitor", 301, 0, str(CHAT)))
            r.eq("mirror: the row holds the plain text", row["text"], "Hello there & co. zebrafish")
            # Every desk source makes its own row.
            for i, src in enumerate(("chat", "analyst", "brief", "proposal", "execution", "system")):
                box.send(f"source check {src}", source=src)
            rows = desk_rows(root)
            r.eq("mirror: each source gets a row with its source",
                 [x["source"] for x in rows], ["monitor", "chat", "analyst", "brief", "proposal", "execution", "system"])
            r.eq("mirror: each row has its own tg_message_id", [x["tg_message_id"] for x in rows],
                 list(range(301, 308)))
            r.eq("mirror: every row delivered 0", {x["delivered_to_session"] for x in rows}, {0})
            # kinds=[chat] finds the mirrored row.
            with root.led().connect() as conn:
                hits = store.search(conn, "zebrafish", kinds=["chat"])
            r.eq("mirror: kinds=[chat] finds it", [(h["kind"], h["id"]) for h in hits], [("chat", rows[0]["id"])])
            # A session id and the delivered flag pass through.
            box.send("turn reply", source="chat", session_id="sess-1", delivered=1)
            last = desk_rows(root)[-1]
            r.eq("mirror: session_id and delivered pass through", (last["session_id"], last["delivered_to_session"]),
                 ("sess-1", 1))
        finally:
            fc.unpin()


def case_parse_fallback():
    with fresh_root(seeded=False) as root:
        fc = FakeClock("2026-08-05T14:00:00Z").pin_market()
        try:
            tg = FakeTG()
            tg.script.append(PARSE_ERR)
            box = box_for(root, tg, fc)
            res = box.send("<foo>bad</foo> markup &amp; kiwifruit", source="chat")
            sends = tg.sends()
            r.eq("mirror: rejected HTML is sent again", len(sends), 2)
            r.eq("mirror: the first try is HTML", sends[0].get("parse_mode"), "HTML")
            r.ok("mirror: the resend is plain text (no parse_mode)", "parse_mode" not in sends[1], sends[1])
            r.eq("mirror: the resend strips the tags", sends[1].get("text"), "bad markup & kiwifruit")
            rows = desk_rows(root)
            r.eq("mirror: rejected HTML -> one row", len(rows), 1)
            r.eq("mirror: the row has the resent message id", rows[0]["tg_message_id"], res.get("message_id"))
            r.eq("mirror: the send is ok after the fallback", res.get("ok"), True)
            with root.led().connect() as conn:
                hits = store.search(conn, "kiwifruit", kinds=["chat"])
            r.eq("mirror: kinds=[chat] finds the fallback row", [h["id"] for h in hits], [rows[0]["id"]])
            # The fallback keeps the buttons and the reply.
            tg.script.append(PARSE_ERR)
            box.send("<foo>x</foo>", source="proposal", reply_to=42,
                     buttons=outbox.trade_buttons(7, "abcdef0123"))
            s2 = tg.sends()[-1]
            r.ok("mirror: the plain resend keeps reply_parameters and the keyboard",
                 s2.get("reply_parameters", {}).get("message_id") == 42 and "reply_markup" in s2, s2)
        finally:
            fc.unpin()


def case_pacing_reply_split():
    with fresh_root(seeded=False) as root:
        fc = FakeClock("2026-08-05T14:00:00Z").pin_market()
        try:
            tg = FakeTG()
            box = box_for(root, tg, fc)
            box.send("one", source="chat")
            box.send("two", source="chat")
            r.ok("§4.4 rule 2: at least 0.8 s between two messages to one chat",
                 sum(fc.sleeps) >= 0.8 - 1e-9 and len(fc.sleeps) == 1, fc.sleeps)
            fc.advance(5)
            n = len(fc.sleeps)
            box.send("three", source="chat")
            r.eq("§4.4 rule 2: no wait after a long gap", len(fc.sleeps), n)
            # A long text splits; the reply goes on the first part, the buttons on the last.
            long = "\n".join(f"line {i} " + "x" * 90 for i in range(90))
            tg.calls.clear()
            res = box.send(long, source="chat", reply_to=77, buttons=outbox.trade_buttons(9, "0123456789"))
            sends = tg.sends()
            r.ok("split: a long text takes more than one message", len(sends) >= 2, len(sends))
            r.ok("split: every part is within Telegram's 4,096 limit", all(len(s["text"]) <= 4096 for s in sends),
                 [len(s["text"]) for s in sends])
            r.eq("split: the parts join back to the text", "\n".join(s["text"] for s in sends), long)
            r.eq("§4.4 rule 3: reply_parameters on the first part only",
                 [("reply_parameters" in s) for s in sends], [True] + [False] * (len(sends) - 1))
            r.eq("split: the buttons on the last part only", [("reply_markup" in s) for s in sends],
                 [False] * (len(sends) - 1) + [True])
            r.eq("split: one row per part", len(desk_rows(root)), 3 + len(sends))
            r.eq("split: the answer names the last message", res.get("message_id"), tg.next_id)
            # No owner chat: nothing is sent.
            tg2 = FakeTG()
            box2 = outbox.Outbox(root.led(), transport=tg2, chat_id=None, clock=fc.monotonic, sleep=fc.sleep)
            res2 = box2.send("x", source="chat")
            r.eq("mirror: no owner chat id sends nothing", (res2.get("ok"), tg2.calls), (False, []))
        finally:
            fc.unpin()


def case_card():
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            snapshot(root, at="2026-08-05T13:35:00Z")
            fc.set("2026-08-05T14:00:00Z")
            led, cfg = root.led(), root.cfg()
            with led.transaction() as conn:
                b1 = proposals.build_trade(led, conn, cfg=cfg, tickers=["ANET"], origin=None,
                                           rationale="ANET is below its band.", created_by="monitor",
                                           now_dt=fc.now, python_origin="drift", yf_price_fn=FakePrices())
            tg = FakeTG()
            box = box_for(root, tg, fc)
            res = outbox.send_trade_card(led, box, led.connect, b1, now_dt=fc.now)
            with led.connect() as conn:
                p1 = proposals.get_proposal(conn, b1["proposal_id"])
            sends = tg.sends()
            r.eq("card: tg_chat_id and tg_message_id stored on the proposal",
                 (p1["tg_chat_id"], p1["tg_message_id"]), (str(CHAT), tg.next_id))
            r.eq("card: the send answer names the message", res.get("message_id"), p1["tg_message_id"])
            kb = sends[-1].get("reply_markup", {}).get("inline_keyboard")
            btn = kb[0] if kb else []
            r.eq("card: Approve success, Reject danger",
                 [(x["text"], x.get("style"), x["callback_data"]) for x in btn],
                 [("Approve", "success", f"p:{p1['id']}:{p1['nonce']}:a"),
                  ("Reject", "danger", f"p:{p1['id']}:{p1['nonce']}:r")])
            r.ok("card: callback_data under 64 bytes", all(len(x["callback_data"].encode()) < 64 for x in btn), btn)
            rows = desk_rows(root)
            r.eq("card: each card message is a proposal row", [x["source"] for x in rows], ["proposal"] * len(sends))
            # A new ANET proposal supersedes #1: its card goes out and the old card says "Replaced by #N".
            fc.set("2026-08-05T14:10:00Z")
            with led.transaction() as conn:
                b2 = proposals.build_trade(led, conn, cfg=cfg, tickers=["ANET"], origin=None,
                                           rationale="ANET is below its band.", created_by="monitor",
                                           now_dt=fc.now, python_origin="drift", yf_price_fn=FakePrices())
            tg.calls.clear()
            outbox.send_trade_card(led, box, led.connect, b2, now_dt=fc.now)
            edits = [p for m, p in tg.calls if m == "editMessageReplyMarkup"]
            r.eq("card: the superseded card's buttons become 'Replaced by #N'",
                 [(e["message_id"], e["reply_markup"]["inline_keyboard"][0][0]["text"]) for e in edits],
                 [(p1["tg_message_id"], f"Replaced by #{b2['proposal_id']}")])
            # A card of three messages: the buttons on the last one, and its id is stored.
            tg.calls.clear()
            out = box.send_card(b2["proposal_id"], ["part one", "part two", "part three"], kind="trade",
                                nonce="0a1b2c3d4e")
            sends = tg.sends()
            with led.connect() as conn:
                p2 = proposals.get_proposal(conn, b2["proposal_id"])
            r.eq("card: three messages", len(sends), 3)
            r.eq("card: buttons on the last message only", [("reply_markup" in s) for s in sends], [False, False, True])
            r.eq("card: the last message's id is tg_message_id", (p2["tg_message_id"], out.get("message_id")),
                 (tg.next_id, tg.next_id))
            # A change card has Confirm and Reject.
            cb = outbox.change_buttons(1234567890, "0a1b2c3d4e")[0]
            r.eq("card: change buttons Confirm success, Reject danger",
                 [(x["text"], x.get("style")) for x in cb], [("Confirm", "success"), ("Reject", "danger")])
            r.ok("card: callback_data under 64 bytes at a 10-digit id",
                 all(len(x["callback_data"].encode()) < 64 for x in cb), cb)
            # set_label replaces the keyboard with one label.
            tg.calls.clear()
            box.set_label(p2["tg_message_id"], "Approved")
            m, pr = tg.calls[-1]
            r.eq("card: set_label edits the reply markup to one label",
                 (m, pr["message_id"], [[b["text"] for b in row] for row in pr["reply_markup"]["inline_keyboard"]]),
                 ("editMessageReplyMarkup", p2["tg_message_id"], [["Approved"]]))
        finally:
            fc.unpin()


def main() -> int:
    for case in (case_send, case_parse_fallback, case_pacing_reply_split, case_card):
        try:
            case()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
