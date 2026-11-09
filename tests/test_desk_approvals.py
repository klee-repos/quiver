"""The tap: ``lib/desk/approvals.handle_tap`` (plan §10.5, test table ``test_desk_approvals.py``).

Each case uses its own temp root. The ledger is a copy of one seeded template that the real
``desk-seed`` made. A ``build_trade`` card goes out through a recording fake transport, and
the tap uses a callback dict with ``id``, ``from.id``, ``message.chat.id``,
``message.message_id`` and ``data``. No network, no LLM, no broker.

Run: .venv/bin/python tests/test_desk_approvals.py
"""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import (REPO, FakeClock, FakePrices, Results, seed_desk, snapshot,  # noqa: E402
                        temp_root, TEST_BOOK)

from lib.desk import approvals, outbox, proposals, store  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

r = Results("desk-approvals")

OWNER = 777001
CHAT = 555001
SEED_AT = "2026-08-03T14:00:00Z"


# --- helpers ----------------------------------------------------------------------

class FakeTG:
    """A recording transport ``(method, params) -> dict``. The answers use only the Bot API's
    documented minimal shapes (the ``tg`` capture is pending)."""

    def __init__(self, start: int = 900):
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


_TEMPLATE = {}


def _template():
    """Seed once with the real ``desk-seed``; later cases copy the ledger file."""
    if not _TEMPLATE:
        with temp_root() as root:
            fc = FakeClock(SEED_AT).pin_market()
            try:
                seed_desk(root)
            finally:
                fc.unpin()
            _TEMPLATE["db"] = root.db.read_bytes()
    return _TEMPLATE["db"]


@contextmanager
def fresh_root():
    db = _template()
    with temp_root() as root:
        (root.path / "strategy.yaml").write_text(TEST_BOOK, encoding="utf-8")
        root.db.write_bytes(db)
        yield root


def snap_at(root, fc, at, **kw):
    fc.set(at)
    return snapshot(root, at=at, **kw)


def build(root, fc, at, *, tickers=("ANET",), led=None):
    """A drift ``build_trade`` at ``at`` (the monitor's origin), in one transaction."""
    fc.set(at)
    led = led or root.led()
    cfg = root.cfg()
    with led.transaction() as conn:
        return proposals.build_trade(led, conn, cfg=cfg, tickers=list(tickers), origin=None,
                                     rationale="ANET is below its band.", created_by="monitor",
                                     now_dt=fc.now, python_origin="drift", yf_price_fn=FakePrices())


def send_card(root, fc, built, tg):
    led = root.led()
    box = outbox.Outbox(led, transport=tg, chat_id=CHAT, clock=fc.monotonic, sleep=fc.sleep)
    outbox.send_trade_card(led, box, led.connect, built, now_dt=fc.now)
    return prop(root, built["proposal_id"])


def trade(root, fc, *, snap="2026-08-05T13:35:00Z", at="2026-08-05T14:00:00Z", tg=None):
    """``snap`` + a drift trade on ANET with its card sent. Returns (proposal row, tg)."""
    tg = tg or FakeTG()
    snap_at(root, fc, snap)
    b = build(root, fc, at)
    return send_card(root, fc, b, tg), tg


def prop(root, pid):
    with root.led().connect() as conn:
        return proposals.get_proposal(conn, pid)


def legs(root, pid):
    with root.led().connect() as conn:
        return [dict(x) for x in conn.execute(
            "SELECT ref_id, state, finalized, skip_reason FROM orders WHERE proposal_id=? ORDER BY leg_seq",
            (pid,)).fetchall()]


def jobs(root, pid=None):
    with root.led().connect() as conn:
        rows = conn.execute("SELECT id, kind, proposal_id, status FROM broker_jobs ORDER BY id").fetchall()
    return [dict(x) for x in rows if pid is None or x["proposal_id"] == pid]


def dump(root):
    with root.led().connect() as conn:
        return {
            "proposals": [dict(x) for x in conn.execute("SELECT * FROM proposals ORDER BY id")],
            "orders": [dict(x) for x in conn.execute("SELECT * FROM orders ORDER BY ref_id")],
            "jobs": [dict(x) for x in conn.execute("SELECT * FROM broker_jobs ORDER BY id")],
            "chat": [dict(x) for x in conn.execute("SELECT * FROM chat_messages ORDER BY id")],
            "settings": [dict(x) for x in conn.execute("SELECT * FROM desk_settings ORDER BY key")],
        }


def cb(p, action="a", *, data=None, frm=OWNER, chat=CHAT, mid=None):
    """A Telegram ``callback_query`` dict for proposal row ``p``."""
    return {"id": "cbq-1", "from": {"id": frm},
            "message": {"chat": {"id": chat}, "message_id": p["tg_message_id"] if mid is None else mid},
            "data": data if data is not None else f"p:{p['id']}:{p['nonce']}:{action}"}


def tap(led, cfg, q, now_dt, owners=(OWNER,)):
    return approvals.handle_tap(led, cfg, callback_data=q["data"], from_id=q["from"]["id"],
                                chat_id=q["message"]["chat"]["id"], message_id=q["message"]["message_id"],
                                owner_ids=list(owners), now_dt=now_dt)


def chat_texts(root):
    with root.led().connect() as conn:
        return [(x["text"], x["source"]) for x in conn.execute(
            "SELECT text, source FROM chat_messages ORDER BY id").fetchall()]


class _FaultConn:
    """A connection proxy that raises on the first statement that contains ``needle``."""

    def __init__(self, conn, needle):
        self._c = conn
        self._n = needle

    def execute(self, sql, *args):
        if self._n in sql:
            raise sqlite3.OperationalError(f"injected fault at {self._n!r}")
        return self._c.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._c, name)


class FaultLedger(Ledger):
    """A real Ledger whose ``transaction()`` connection fails on one statement."""

    def __init__(self, path, needle):
        super().__init__(path)
        self.needle = needle

    @contextmanager
    def transaction(self):
        with Ledger.transaction(self) as conn:
            yield _FaultConn(conn, self.needle)


class GateLedger(Ledger):
    """A real Ledger that waits on a barrier before its first ``transaction()``. Two taps
    then both read ``pending`` before either one claims, so the conditional UPDATE decides."""

    def __init__(self, path, barrier):
        super().__init__(path)
        self.barrier = barrier
        self.first = True

    @contextmanager
    def transaction(self):
        if self.first:
            self.first = False
            try:
                self.barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        with Ledger.transaction(self) as conn:
            yield conn


def race(root, cfg, calls, now_dt, *, gate=True):
    """Run ``calls`` (a list of callback dicts) at the same time, one Ledger per thread."""
    barrier = threading.Barrier(len(calls))
    out = [None] * len(calls)
    errs = []

    def run(i, q):
        try:
            led = GateLedger(root.db, barrier) if gate else Ledger(root.db)
            if not gate:
                barrier.wait(timeout=10)
            out[i] = tap(led, cfg, q, now_dt)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=run, args=(i, q)) for i, q in enumerate(calls)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    return out, errs


def at(s):
    return store.parse_ts(s)


# --- identity -----------------------------------------------------------------------

def case_identity():
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T14:00:00Z").pin_market()
        try:
            p, _tg = trade(root, fc)
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T14:05:00Z")
            fc.set(now)
            before = dump(root)
            bad_nonce = ("0" if p["nonce"][0] != "0" else "1") + p["nonce"][1:]
            cases = {
                "data :x": cb(p, data=f"p:{p['id']}:{p['nonce']}:x"),
                "data q:": cb(p, data=f"q:{p['id']}:{p['nonce']}:a"),
                "data p:abc": cb(p, data=f"p:abc:{p['nonce']}:a"),
                "data 5 fields": cb(p, data=f"p:{p['id']}:{p['nonce']}:a:x"),
                "data empty": cb(p, data=""),
                "non-owner from.id": cb(p, frm=OWNER + 1),
                "another chat id": cb(p, chat=CHAT + 1),
                "another message id": cb(p, mid=int(p["tg_message_id"]) + 1),
                "wrong nonce approve": cb(p, data=f"p:{p['id']}:{bad_nonce}:a"),
                "wrong nonce reject": cb(p, data=f"p:{p['id']}:{bad_nonce}:r"),
                "another proposal id": cb(p, data=f"p:{p['id'] + 1}:{p['nonce']}:a"),
                "non-owner reject": cb(p, "r", frm=OWNER + 1),
            }
            for label, q in cases.items():
                res = tap(led, cfg, q, now)
                r.ok(f"identity: {label} refused", res.get("ok") is False and res.get("answer"), res)
                r.ok(f"identity: {label} no touch, no message", not res.get("touch") and not res.get("messages"), res)
                r.eq(f"identity: {label} no change", dump(root), before)
            # The owner list comes from the caller: an empty list refuses the owner too.
            res = tap(led, cfg, cb(p), now, owners=())
            r.ok("identity: empty owner list refuses", res.get("ok") is False, res)
            r.eq("identity: empty owner list no change", dump(root), before)
            # A proposal whose card was never sent has no tg_message_id: every tap is refused.
            b2 = build(root, fc, "2026-08-05T14:01:00Z", tickers=("ANET",))
            p2 = prop(root, b2["proposal_id"])
            fc.set(now)
            before2 = dump(root)
            res = tap(led, cfg, cb(p2, mid=int(p["tg_message_id"])), now)
            r.ok("identity: a card never sent (no tg_message_id) is refused", res.get("ok") is False, res)
            r.eq("identity: a card never sent: no change", dump(root), before2)
            p = prop(root, p["id"])
            r.eq("identity: the first card was superseded by the unsent one", p["state"], "superseded")
            p, before = p2, before2
            send_card(root, fc, b2, FakeTG(start=950))
            p = prop(root, p["id"])
            # The good tap still works after the refusals.
            res = tap(led, cfg, cb(p), now)
            r.eq("identity: the real tap approves after the refusals", (res.get("ok"), res.get("answer")),
                 (True, "Approved"))
        finally:
            fc.unpin()


# --- state --------------------------------------------------------------------------

def case_state():
    # superseded -> "Replaced by #N"; identity runs before state.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T14:00:00Z").pin_market()
        try:
            tg = FakeTG()
            p1, _ = trade(root, fc, tg=tg)
            b2 = build(root, fc, "2026-08-05T14:10:00Z")
            p2 = send_card(root, fc, b2, tg)
            r.eq("state: the new ANET proposal supersedes the old one", b2["superseded"], [p1["id"]])
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T14:15:00Z")
            fc.set(now)
            before = dump(root)
            res = tap(led, cfg, cb(p1), now)
            r.eq("state: superseded answers 'Replaced by #N'", res.get("answer"), f"Replaced by #{p2['id']}")
            r.ok("state: superseded is not ok", res.get("ok") is False, res)
            r.eq("state: superseded changes nothing", dump(root), before)
            res = tap(led, cfg, cb(p1, "r"), now)
            r.eq("state: a Reject on superseded answers its state", res.get("answer"), f"Replaced by #{p2['id']}")
            r.eq("state: superseded stays superseded", prop(root, p1["id"])["state"], "superseded")
            bad = cb(p1, data=f"p:{p1['id']}:{'f' * 10 if p1['nonce'] != 'f' * 10 else '0' * 10}:a")
            res = tap(led, cfg, bad, now)
            r.ok("state: identity is checked before state (wrong nonce is not 'Replaced')",
                 res.get("ok") is False and "Replaced" not in str(res.get("answer")), res)
            # approved -> "Already approved"; rejected -> "Rejected".
            ok = tap(led, cfg, cb(p2), now)
            r.eq("state: approve #2", ok.get("answer"), "Approved")
            again = tap(led, cfg, cb(p2), now)
            r.eq("state: a second tap on approved answers 'Already approved'", again.get("answer"), "Already approved")
            rej = tap(led, cfg, cb(p2, "r"), now)
            r.eq("state: a Reject on approved answers 'Already approved'", rej.get("answer"), "Already approved")
            r.eq("state: one execute job after three taps", len(jobs(root, p2["id"])), 1)
        finally:
            fc.unpin()
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T14:00:00Z").pin_market()
        try:
            p, _ = trade(root, fc)
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T14:05:00Z")
            res = tap(led, cfg, cb(p, "r"), now)
            r.eq("state: reject", res.get("answer"), "Rejected")
            res = tap(led, cfg, cb(p, "a"), now)
            r.eq("state: an Approve on rejected answers 'Rejected'", res.get("answer"), "Rejected")
            r.eq("state: rejected stays rejected", prop(root, p["id"])["state"], "rejected")
            r.eq("state: no job for a rejected proposal", jobs(root, p["id"]), [])
        finally:
            fc.unpin()
    # past expires_at -> expired, "Expired", checked before the market (KILL + closed market).
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T14:00:00Z").pin_market()
        try:
            p, _ = trade(root, fc)
            r.eq("state: expires_at is the 08-05 close", p["expires_at"], "2026-08-05T20:00:00Z")
            root.kill.write_text("stop", encoding="utf-8")
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T20:30:00Z")
            fc.set(now)
            res = tap(led, cfg, cb(p), now)
            r.eq("state: past expires_at answers 'Expired' (not the KILL or closed text)", res.get("answer"), "Expired")
            r.eq("state: past expires_at sets expired", prop(root, p["id"])["state"], "expired")
            r.eq("state: the open legs become expired, finalized=1",
                 [(l["state"], l["finalized"]) for l in legs(root, p["id"])], [("expired", 1)])
            r.eq("state: no job for an expired tap", jobs(root, p["id"]), [])
            res = tap(led, cfg, cb(p), now)
            r.eq("state: a tap on expired answers 'Expired'", res.get("answer"), "Expired")
        finally:
            fc.unpin()
    # At expires_at exactly: now < expires_at fails. A Reject past expiry also expires.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T14:00:00Z").pin_market()
        try:
            p, _ = trade(root, fc)
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T20:00:00Z")
            fc.set(now)
            res = tap(led, cfg, cb(p, "r"), now)
            r.eq("state: a Reject at expires_at answers 'Expired'", res.get("answer"), "Expired")
            r.eq("state: a Reject at expires_at sets expired, not rejected", prop(root, p["id"])["state"], "expired")
        finally:
            fc.unpin()


# --- gate ---------------------------------------------------------------------------

def _gate_case(label, *, snap, build_at, tap_at, setup, want, want_ok=False):
    with fresh_root() as root:
        fc = FakeClock(snap).pin_market()
        try:
            p, _ = trade(root, fc, snap=snap, at=build_at)
            setup(root)
            led, cfg = root.led(), root.cfg()
            now = at(tap_at)
            fc.set(now)
            before = dump(root)
            res = tap(led, cfg, cb(p), now)
            if want_ok:
                r.eq(f"gate: {label} approves", (res.get("ok"), res.get("answer")), (True, "Approved"))
                return p, root
            r.eq(f"gate: {label} text", res.get("answer"), want)
            r.ok(f"gate: {label} is not ok", res.get("ok") is False, res)
            r.eq(f"gate: {label} keeps the row pending", prop(root, p["id"])["state"], "pending")
            r.eq(f"gate: {label} changes nothing", dump(root), before)
            r.ok(f"gate: {label} names no trigger", not res.get("touch"), res)
            return p, root
        finally:
            fc.unpin()


def _kill(root):
    root.kill.write_text("stop", encoding="utf-8")


def _halt(day):
    def f(root):
        root.led().mark_halted(day, "daily loss test")
    return f


def _both(*fs):
    def f(root):
        for g in fs:
            g(root)
    return f


def case_gate():
    kill_txt = "Trading is stopped on the box. This stays open until 4:00 PM ET."
    halt_txt = ("Trading is halted for today after the daily-loss check. "
                "Ask me to propose resume_trading to lift it.")
    _gate_case("KILL", snap="2026-08-05T13:35:00Z", build_at="2026-08-05T14:00:00Z",
               tap_at="2026-08-05T14:05:00Z", setup=_kill, want=kill_txt)
    _gate_case("halted", snap="2026-08-05T13:35:00Z", build_at="2026-08-05T14:00:00Z",
               tap_at="2026-08-05T14:05:00Z", setup=_halt("2026-08-05"), want=halt_txt)
    # The market hours never refuse a tap: they hold the order (case_held).
    # 6 minutes before the close goes at once; less holds (the broker needs a minute to start).
    _gate_case("exactly 6 min before the close", snap="2026-08-05T13:35:00Z",
               build_at="2026-08-05T14:00:00Z", tap_at="2026-08-05T19:54:00Z", setup=lambda root: None,
               want=None, want_ok=True)
    # The first failing reason wins.
    _gate_case("KILL + halted + closed -> KILL", snap="2026-08-05T11:55:00Z",
               build_at="2026-08-05T12:00:00Z", tap_at="2026-08-05T13:00:00Z",
               setup=_both(_kill, _halt("2026-08-05")), want=kill_txt)
    _gate_case("halted + closed -> halted", snap="2026-08-05T11:55:00Z",
               build_at="2026-08-05T12:00:00Z", tap_at="2026-08-05T13:00:00Z",
               setup=_halt("2026-08-05"), want=halt_txt)
    _gate_case("KILL + near the close -> KILL", snap="2026-08-05T13:35:00Z",
               build_at="2026-08-05T14:00:00Z", tap_at="2026-08-05T19:56:00Z", setup=_kill, want=kill_txt)
    # A refusal keeps it pending: the same tap approves once the reason is gone.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, _ = trade(root, fc)
            _kill(root)
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T14:05:00Z")
            fc.set(now)
            first = tap(led, cfg, cb(p), now)
            root.kill.unlink()
            second = tap(led, cfg, cb(p), now)
            r.eq("gate: KILL then no KILL: the second tap approves",
                 (first.get("answer"), second.get("answer")), (kill_txt, "Approved"))
            r.eq("gate: one execute job after the KILL refusal", len(jobs(root, p["id"])), 1)
        finally:
            fc.unpin()


# --- held (§10.5 check 4) -----------------------------------------------------------

def _held_case(label, *, snap, build_at, tap_at, send_at, expires_at, answer, button, why=None):
    """An Approve tap while the market cannot take the order: approved, one held job, no
    trigger, the held buttons. Returns (root context, root, proposal row, fc) for more taps."""
    root_cm = fresh_root()
    root = root_cm.__enter__()
    fc = FakeClock(snap).pin_market()
    p, _ = trade(root, fc, snap=snap, at=build_at)
    led, cfg = root.led(), root.cfg()
    now = at(tap_at)
    fc.set(now)
    res = tap(led, cfg, cb(p), now)
    r.eq(f"held: {label} approves", (res.get("ok"), res.get("answer"), res.get("label")), (True, answer, None))
    r.eq(f"held: {label} names no trigger", res.get("touch"), [])
    r.eq(f"held: {label} row approved", prop(root, p["id"])["state"], "approved")
    r.eq(f"held: {label} legs approved", [l["state"] for l in legs(root, p["id"])], ["approved"])
    with led.connect() as conn:
        js = [dict(x) for x in conn.execute("SELECT kind, status, recovery, not_before FROM broker_jobs")]
    r.eq(f"held: {label} one held execute job", js,
         [{"kind": "execute", "status": "queued", "recovery": 0, "not_before": send_at}])
    r.eq(f"held: {label} expires_at", prop(root, p["id"])["expires_at"], expires_at)
    r.eq(f"held: {label} the buttons: the label, then Cancel", res.get("buttons"),
         [[{"text": button, "callback_data": "noop"}],
          [{"text": "Cancel", "callback_data": f"p:{p['id']}:{p['nonce']}:r", "style": "danger"}]])
    when = answer.split("send it at ", 1)[1].rstrip(".")
    want = f"Approved #{p['id']}. It sends at {when}. Say cancel before then to stop it."
    r.eq(f"held: {label} the message names the reason, the send time and Cancel", res.get("messages"),
         [want] if why else res.get("messages"))
    return root_cm, root, p, fc


def case_held():
    # Pre-open (08:00 ET build, 09:00 ET tap): held to 9:40 AM ET the same day.
    cm, root, p, fc = _held_case("pre-open", snap="2026-08-05T11:55:00Z", build_at="2026-08-05T12:00:00Z",
                                 tap_at="2026-08-05T13:00:00Z", send_at="2026-08-05T13:40:00Z",
                                 expires_at="2026-08-05T20:00:00Z", answer="Approved. I send it at 9:40 AM ET.",
                                 button="Approved, sends 9:40 AM ET", why="The market is closed now")
    try:
        led, cfg = root.led(), root.cfg()
        res = tap(led, cfg, cb(p), at("2026-08-05T13:01:00Z"))
        r.eq("held: a second Approve names the send time and keeps Cancel",
             (res.get("answer"), res.get("label"), res.get("buttons")),
             ("Already approved. I send it at 9:40 AM ET.", None,
              outbox.held_buttons(p["id"], p["nonce"], "Approved, sends 9:40 AM ET")))
        r.eq("held: still one job", len(jobs(root, p["id"])), 1)
        # A new trade on the same ticker names the held order and the way out.
        try:
            with led.transaction() as conn:
                proposals.build_trade(led, conn, cfg=cfg, tickers=["ANET"], origin=None, rationale="again",
                                      created_by="monitor", now_dt=at("2026-08-05T13:02:00Z"),
                                      python_origin="drift", yf_price_fn=FakePrices())
            err = None
        except proposals.ProposalError as e:
            err = str(e)
        r.eq("held: a new trade on the ticker names the held order",
             err, f"ANET has an approved order (#{p['id']}) that I send at 9:40 AM ET. To change it, the owner "
                  "taps Cancel on that card first")
        # Cancel before the send time: the job, the row and the legs end together.
        before = prop(root, p["id"])
        now = at("2026-08-05T13:30:00Z")
        fc.set(now)
        res = tap(led, cfg, cb(p, "r"), now)
        r.eq("held: Cancel before the send time", (res.get("ok"), res.get("answer"), res.get("label")),
             (True, "Cancelled", "Cancelled"))
        r.eq("held: Cancel's message", res.get("messages"), [f"You cancelled #{p['id']}. I sent nothing to Robinhood."])
        row = prop(root, p["id"])
        r.eq("held: Cancel ends the row rejected, decided now by the owner",
             (row["state"], row["decided_at"], row["decided_by"]), ("rejected", "2026-08-05T13:30:00Z", str(OWNER)))
        r.eq("held: Cancel keeps the approval time", store.loads(row["result_json"], {}).get("approved_at"),
             before["decided_at"])
        r.eq("held: Cancel skips the legs", [(l["state"], l["finalized"], l["skip_reason"]) for l in legs(root, p["id"])],
             [("skipped", 1, "cancelled")])
        with led.connect() as conn:
            j = dict(conn.execute("SELECT status, error FROM broker_jobs").fetchone())
        r.eq("held: Cancel fails the held job", j, {"status": "failed", "error": "cancelled by the owner"})
        res = tap(led, cfg, cb(p, "r"), at("2026-08-05T13:31:00Z"))
        r.eq("held: a second Cancel answers 'Rejected'", res.get("answer"), "Rejected")
        res = tap(led, cfg, cb(p), at("2026-08-05T13:32:00Z"))
        r.eq("held: an Approve after Cancel answers 'Rejected'", res.get("answer"), "Rejected")
        with led.connect() as conn:
            r.eq("held: no ANET leg is in flight after Cancel", proposals.in_flight_ticker(conn, "ANET"), None)
    finally:
        fc.unpin()
        cm.__exit__(None, None, None)

    # Cancel at the send time: too late, the approved row stays as it is.
    cm, root, p, fc = _held_case("late cancel", snap="2026-08-05T11:55:00Z", build_at="2026-08-05T12:00:00Z",
                                 tap_at="2026-08-05T13:00:00Z", send_at="2026-08-05T13:40:00Z",
                                 expires_at="2026-08-05T20:00:00Z", answer="Approved. I send it at 9:40 AM ET.",
                                 button="Approved, sends 9:40 AM ET")
    try:
        led, cfg = root.led(), root.cfg()
        before = dump(root)
        res = tap(led, cfg, cb(p, "r"), at("2026-08-05T13:40:00Z"))
        r.eq("held: Cancel at the send time says it is too late, in a popup",
             (res.get("answer"), res.get("label"), res.get("alert")),
             ("Too late to cancel: it is going to Robinhood now.", "Approved", True))
        r.eq("held: a late Cancel changes nothing", dump(root), before)
        res = tap(led, cfg, cb(p), at("2026-08-05T13:40:00Z"))
        r.eq("held: an Approve at the send time answers 'Already approved'", res.get("answer"), "Already approved")
    finally:
        fc.unpin()
        cm.__exit__(None, None, None)

    # After the close (the live case of 2026-11-04): held to 9:40 AM ET of the next session.
    cm, root, p, fc = _held_case("after the close", snap="2026-08-05T20:40:00Z", build_at="2026-08-05T20:44:00Z",
                                 tap_at="2026-08-05T20:46:00Z", send_at="2026-08-06T13:40:00Z",
                                 expires_at="2026-08-06T20:00:00Z",
                                 answer="Approved. I send it at 9:40 AM ET on Thursday, August 6.",
                                 button="Approved, sends Thu 9:40 AM ET", why="The market is closed now")
    fc.unpin()
    cm.__exit__(None, None, None)

    # 4 minutes before the close: held to the next session; expires_at moves to its close.
    soon, closed = "The market closes in a few minutes", "The market is closed now"
    for label, snap, build_at, tap_at, send_at, expires_at, answer, button, why in (
            ("4 min before the close", "2026-08-05T13:35:00Z", "2026-08-05T14:00:00Z", "2026-08-05T19:56:00Z",
             "2026-08-06T13:40:00Z", "2026-08-06T20:00:00Z",
             "Approved. I send it at 9:40 AM ET on Thursday, August 6.", "Approved, sends Thu 9:40 AM ET", soon),
            # 2026-11-27 is a half day (1:00 PM ET close); 11-28 and 11-29 are a weekend.
            ("4 min before the half-day close", "2026-11-27T14:35:00Z", "2026-11-27T15:00:00Z",
             "2026-11-27T17:56:00Z", "2026-11-30T14:40:00Z", "2026-11-30T21:00:00Z",
             "Approved. I send it at 9:40 AM ET on Monday, November 30.", "Approved, sends Mon 9:40 AM ET", soon),
            ("pre-open half day", "2026-11-27T12:55:00Z", "2026-11-27T13:00:00Z", "2026-11-27T13:30:00Z",
             "2026-11-27T14:40:00Z", "2026-11-27T18:00:00Z", "Approved. I send it at 9:40 AM ET.",
             "Approved, sends 9:40 AM ET", closed),
            ("exactly 5 min before the close", "2026-08-05T13:35:00Z", "2026-08-05T14:00:00Z",
             "2026-08-05T19:55:00Z", "2026-08-06T13:40:00Z", "2026-08-06T20:00:00Z",
             "Approved. I send it at 9:40 AM ET on Thursday, August 6.", "Approved, sends Thu 9:40 AM ET", soon)):
        cm, root, p, fc = _held_case(label, snap=snap, build_at=build_at, tap_at=tap_at, send_at=send_at,
                                     expires_at=expires_at, answer=answer, button=button, why=why)
        fc.unpin()
        cm.__exit__(None, None, None)

    # The halt binds the day the order goes: a halted day does not refuse a tap for tomorrow.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T20:40:00Z").pin_market()
        try:
            p, _ = trade(root, fc, snap="2026-08-05T20:40:00Z", at="2026-08-05T20:44:00Z")
            _halt("2026-08-05")(root)
            led, cfg = root.led(), root.cfg()
            res = tap(led, cfg, cb(p), at("2026-08-05T20:46:00Z"))
            r.eq("held: today's halt does not refuse a tap held to tomorrow",
                 (res.get("ok"), res.get("answer")), (True, "Approved. I send it at 9:40 AM ET on Thursday, August 6."))
        finally:
            fc.unpin()

    # The card names the send time when the market is closed, and not in the session.
    for label, snap, build_at, want in (
            ("after the close", "2026-08-05T20:40:00Z", "2026-08-05T20:44:00Z",
             "Sends Thu 9:40 AM ET. Reply yes or tap Approve."),
            ("in the session", "2026-08-05T13:35:00Z", "2026-08-05T14:00:00Z", None)):
        with fresh_root() as root:
            fc = FakeClock(snap).pin_market()
            try:
                tg = FakeTG()
                trade(root, fc, snap=snap, at=build_at, tg=tg)
                text = [c[1]["text"] for c in tg.calls if c[0] == "sendMessage"][-1]
                if want:
                    r.ok(f"held: the card {label} names the send time", want in text, text)
                else:
                    r.ok(f"held: the card {label} has no hold sentence", "Sends " not in text, text)
            finally:
                fc.unpin()


# --- claim_once ---------------------------------------------------------------------

def case_claim_once():
    for trial in range(4):
        gate = trial % 2 == 0      # gated: both read pending, the UPDATE decides; free: a plain race
        with fresh_root() as root:
            fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
            try:
                p, _ = trade(root, fc)
                cfg = root.cfg()
                now = at("2026-08-05T14:05:00Z")
                fc.set(now)
                out, errs = race(root, cfg, [cb(p), cb(p)], now, gate=gate)
                tag = f"claim_once[{'gated' if gate else 'free'} {trial}]"
                r.eq(f"{tag}: no thread error", errs, [])
                answers = sorted(str((o or {}).get("answer")) for o in out)
                r.eq(f"{tag}: one approval, one 'Already approved'", answers, ["Already approved", "Approved"])
                r.eq(f"{tag}: exactly one ok", sum(1 for o in out if (o or {}).get("ok")), 1)
                r.eq(f"{tag}: one execute job", [(j["kind"], j["status"]) for j in jobs(root, p["id"])],
                     [("execute", "queued")])
                r.eq(f"{tag}: the row is approved", prop(root, p["id"])["state"], "approved")
                r.eq(f"{tag}: legs approved", [l["state"] for l in legs(root, p["id"])], ["approved"])
                r.eq(f"{tag}: one broker trigger named in all", sum(len((o or {}).get("touch") or []) for o in out), 1)
            finally:
                fc.unpin()


# --- txn ----------------------------------------------------------------------------

def case_txn():
    # The UPDATE, the legs and the job commit together.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, _ = trade(root, fc)
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T14:05:00Z")
            fc.set(now)
            trig = root.path / "state" / "triggers" / "broker"
            res = tap(led, cfg, cb(p), now)
            fresh = Ledger(root.db)              # a second connection sees the commit
            with fresh.connect() as conn:
                row = proposals.get_proposal(conn, p["id"])
                lg = [dict(x) for x in conn.execute("SELECT state, finalized FROM orders WHERE proposal_id=?",
                                                    (p["id"],))]
                jb = [dict(x) for x in conn.execute("SELECT kind, proposal_id, status, recovery FROM broker_jobs")]
            r.eq("txn: answer 'Approved'", (res.get("ok"), res.get("answer"), res.get("label")),
                 (True, "Approved", "Approved"))
            r.eq("txn: state approved, decided_at, decided_by", (row["state"], row["decided_at"], row["decided_by"]),
                 ("approved", "2026-08-05T14:05:00Z", str(OWNER)))
            r.eq("txn: legs approved", lg, [{"state": "approved", "finalized": 0}])
            r.eq("txn: one execute job", jb, [{"kind": "execute", "proposal_id": p["id"], "status": "queued",
                                               "recovery": 0}])
            r.eq("txn: 'On it. Checking the price now.'", res.get("messages"), ["On it. Checking the price now."])
            # The trigger: the tap names it (the bridge touches it after the call), or touched it itself
            # after the commit. Either way the job above is committed when the tap returns.
            r.ok("txn: the broker trigger follows the commit", "broker" in (res.get("touch") or []) or trig.exists(), res)
        finally:
            fc.unpin()
    # An error after the UPDATE rolls back all (the job INSERT, then the legs UPDATE).
    for needle in ("INSERT INTO broker_jobs", "UPDATE orders SET state='approved'"):
        with fresh_root() as root:
            fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
            try:
                p, _ = trade(root, fc)
                cfg = root.cfg()
                now = at("2026-08-05T14:05:00Z")
                fc.set(now)
                before = dump(root)
                res, err = None, None
                try:
                    res = tap(FaultLedger(root.db, needle), cfg, cb(p), now)
                except Exception as e:  # noqa: BLE001
                    err = e
                tag = f"txn: fault at {needle!r}"
                r.ok(f"{tag}: the tap does not report ok", err is not None or not (res or {}).get("ok"), res)
                r.eq(f"{tag}: rolls back all (row, legs, jobs)", dump(root), before)
                r.ok(f"{tag}: no trigger after a rollback",
                     not (root.path / "state" / "triggers" / "broker").exists()
                     and "broker" not in ((res or {}).get("touch") or []), res)
                ok = tap(root.led(), cfg, cb(p), now)
                r.eq(f"{tag}: a later tap approves", ok.get("answer"), "Approved")
            finally:
                fc.unpin()
    # A change: Confirm applies it and sets applied in the same transaction. Check 4 is for trades.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            snap_at(root, fc, "2026-08-05T11:55:00Z")
            p = _change(root, fc, "2026-08-05T12:00:00Z")
            _kill(root)
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T12:30:00Z")          # closed market, KILL: a Confirm still applies
            fc.set(now)
            res = tap(led, cfg, cb(p), now)
            row = prop(root, p["id"])
            r.eq("txn: change Confirm answer", (res.get("ok"), res.get("answer")), (True, "Confirmed"))
            r.eq("txn: change state applied, decided_at, decided_by", (row["state"], row["decided_at"], row["decided_by"]),
                 ("applied", "2026-08-05T12:30:00Z", str(OWNER)))
            r.eq("txn: change wrote desk_settings with proposal_id", _setting(root, "min_trade_notional"),
                 (10.0, p["id"]))
            r.eq("txn: a change queues no broker job", jobs(root), [])
            again = tap(led, cfg, cb(p), now)
            r.eq("txn: a second Confirm answers its state", again.get("answer"), "Already confirmed")
        finally:
            fc.unpin()
    for needle in ("UPDATE proposals SET state='applied'", "INSERT INTO strategy_change_log"):
        with fresh_root() as root:
            fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
            try:
                snap_at(root, fc, "2026-08-05T13:35:00Z")
                p = _change(root, fc, "2026-08-05T14:00:00Z")
                cfg = root.cfg()
                now = at("2026-08-05T14:05:00Z")
                fc.set(now)
                before = dump(root)
                log_before = _log_count(root)
                res, err = None, None
                try:
                    res = tap(FaultLedger(root.db, needle), cfg, cb(p), now)
                except Exception as e:  # noqa: BLE001
                    err = e
                tag = f"txn: change fault at {needle!r}"
                r.ok(f"{tag}: not ok", err is not None or not (res or {}).get("ok"), res)
                r.eq(f"{tag}: the apply rolls back with the claim", dump(root), before)
                r.eq(f"{tag}: no strategy_change_log row", _log_count(root), log_before)
            finally:
                fc.unpin()


def _change(root, fc, when):
    fc.set(when)
    led, cfg = root.led(), root.cfg()
    with led.transaction() as conn:
        b = proposals.build_change(conn, cfg, change={"kind": "set_risk", "key": "min_trade_notional", "value": 10},
                                   rationale="Raise the minimum trade.", created_by="desk", now_dt=fc.now)
    box = outbox.Outbox(led, transport=FakeTG(), chat_id=CHAT, clock=fc.monotonic, sleep=fc.sleep)
    outbox.send_change_card(led, box, cfg, b["proposal_id"], now_dt=fc.now)
    return prop(root, b["proposal_id"])


def _setting(root, key):
    with root.led().connect() as conn:
        row = conn.execute("SELECT value_json, proposal_id FROM desk_settings WHERE key=?", (key,)).fetchone()
    return (float(json.loads(row[0])), row[1]) if row else None


def _log_count(root):
    with root.led().connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM strategy_change_log").fetchone()[0]


# --- reject -------------------------------------------------------------------------

def case_reject():
    # Passes with KILL, halt and a closed market (built before the open).
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T11:55:00Z").pin_market()
        try:
            p, _ = trade(root, fc, snap="2026-08-05T11:55:00Z", at="2026-08-05T12:00:00Z")
            _kill(root)
            _halt("2026-08-05")(root)
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T13:00:00Z")
            fc.set(now)
            res = tap(led, cfg, cb(p, "r"), now)
            row = prop(root, p["id"])
            r.eq("reject: answer and label 'Rejected'", (res.get("ok"), res.get("answer"), res.get("label")),
                 (True, "Rejected", "Rejected"))
            r.eq("reject: state rejected, decided_at, decided_by", (row["state"], row["decided_at"], row["decided_by"]),
                 ("rejected", "2026-08-05T13:00:00Z", str(OWNER)))
            r.eq("reject: legs skipped, finalized=1", [(l["state"], l["finalized"]) for l in legs(root, p["id"])],
                 [("skipped", 1)])
            r.ok(f"reject: mirror line 'You rejected #{p['id']}.'",
                 (f"You rejected #{p['id']}.", "proposal") in chat_texts(root), chat_texts(root))
            r.eq("reject: no broker job", jobs(root), [])
            r.ok("reject: no broker trigger", not res.get("touch"), res)
        finally:
            fc.unpin()
    # A change Reject: rejected, nothing applied; the mirror line is not yet delivered.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            snap_at(root, fc, "2026-08-05T13:35:00Z")
            p = _change(root, fc, "2026-08-05T14:00:00Z")
            led, cfg = root.led(), root.cfg()
            before = _setting(root, "min_trade_notional")
            now = at("2026-08-05T14:05:00Z")
            fc.set(now)
            res = tap(led, cfg, cb(p, "r"), now)
            r.eq("reject: a change Reject", (res.get("answer"), prop(root, p["id"])["state"]), ("Rejected", "rejected"))
            r.eq("reject: a rejected change applies nothing", _setting(root, "min_trade_notional"), before)
            with led.connect() as conn:
                row = conn.execute("SELECT delivered_to_session, role FROM chat_messages WHERE text=?",
                                   (f"You rejected #{p['id']}.",)).fetchone()
            r.eq("reject: the desk sees the mirror line next turn (desk row, delivered 0)",
                 tuple(row) if row else None, (0, "desk"))
        finally:
            fc.unpin()
    # Approve, then Reject: the approved row is never overwritten.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, _ = trade(root, fc)
            led, cfg = root.led(), root.cfg()
            now = at("2026-08-05T14:05:00Z")
            fc.set(now)
            tap(led, cfg, cb(p), now)
            before = prop(root, p["id"])
            res = tap(led, cfg, cb(p, "r"), at("2026-08-05T14:06:00Z"))
            after = prop(root, p["id"])
            r.eq("reject: after Approve, Reject answers 'Already approved'", res.get("answer"), "Already approved")
            r.eq("reject: the approved row is unchanged", after, before)
            r.eq("reject: legs stay approved", [l["state"] for l in legs(root, p["id"])], ["approved"])
        finally:
            fc.unpin()
    # Approve and Reject from two threads: one wins, the other answers the current state.
    seen = set()
    for trial in range(6):
        with fresh_root() as root:
            fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
            try:
                p, _ = trade(root, fc)
                cfg = root.cfg()
                now = at("2026-08-05T14:05:00Z")
                fc.set(now)
                calls = [cb(p, "a"), cb(p, "r")] if trial % 2 == 0 else [cb(p, "r"), cb(p, "a")]
                out, errs = race(root, cfg, calls, now, gate=True)
                tag = f"reject race[{trial}]"
                r.eq(f"{tag}: no thread error", errs, [])
                oks = [o for o in out if (o or {}).get("ok")]
                r.eq(f"{tag}: exactly one wins", len(oks), 1)
                if len(oks) != 1:
                    continue
                win = oks[0]["answer"]
                lose = next(o for o in out if not (o or {}).get("ok"))
                st = prop(root, p["id"])["state"]
                lg = [l["state"] for l in legs(root, p["id"])]
                seen.add(win)
                if win == "Approved":
                    r.eq(f"{tag}: Approve won: the Reject answers 'Already approved'", lose.get("answer"),
                         "Already approved")
                    r.eq(f"{tag}: Approve won: approved, legs approved, one job",
                         (st, lg, len(jobs(root, p["id"]))), ("approved", ["approved"], 1))
                else:
                    r.eq(f"{tag}: Reject won: the Approve answers 'Rejected'", lose.get("answer"), "Rejected")
                    r.eq(f"{tag}: Reject won: rejected, legs skipped, no job",
                         (st, lg, len(jobs(root, p["id"]))), ("rejected", ["skipped"], 0))
            finally:
                fc.unpin()
    r.ok("reject race: the winner is decided by the conditional UPDATE", seen <= {"Approved", "Rejected"}, seen)


# --- only_caller ----------------------------------------------------------------------

def _imports_approvals(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return "lib.desk.approvals" in path.read_text(encoding="utf-8", errors="replace")
    in_desk = path.parent == REPO / "lib" / "desk"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "lib.desk.approvals" or a.name.startswith("lib.desk.approvals.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and (mod == "lib.desk.approvals" or mod.startswith("lib.desk.approvals.")):
                return True
            if node.level == 0 and mod == "lib.desk" and any(a.name == "approvals" for a in node.names):
                return True
            if node.level > 0 and in_desk and (mod == "approvals" or
                                               (not mod and any(a.name == "approvals" for a in node.names))):
                return True
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
            if name in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) \
                    and str(node.args[0].value).startswith("lib.desk.approvals"):
                return True
    return False


def case_only_caller():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
                         cwd=REPO, capture_output=True, text=True, timeout=30)
    r.eq("only_caller: git ls-files runs", out.returncode, 0)
    files = [f for f in out.stdout.split("\n") if f.strip()]
    r.ok("only_caller: the scan sees the repo files", len(files) > 20 and "lib/desk/approvals.py" in files,
         len(files))
    importers = sorted(f for f in files if f != "lib/desk/approvals.py" and (REPO / f).exists()
                       and _imports_approvals(REPO / f))
    bad = [f for f in importers if f != "deploy/runner/chat_bridge.py" and not f.startswith("tests/")]
    r.eq("only_caller: only chat_bridge.py and tests import lib.desk.approvals", bad, [])
    r.ok("only_caller: chat_bridge.py imports it", "deploy/runner/chat_bridge.py" in importers, importers)
    # The scan itself finds each import form.
    import tempfile
    probe = Path(tempfile.mkdtemp(prefix="quiver_t3_probe_"))
    forms = ["from lib.desk import approvals\n", "import lib.desk.approvals\n",
             "from lib.desk.approvals import handle_tap\n", "from lib.desk import outbox, approvals as a\n",
             "import importlib\nimportlib.import_module('lib.desk.approvals')\n"]
    found = []
    for i, src in enumerate(forms):
        f = probe / f"_t3_probe_{i}.py"
        f.write_text(src, encoding="utf-8")
        try:
            found.append(_imports_approvals(f))
        finally:
            f.unlink()
    shutil.rmtree(probe, ignore_errors=True)
    r.eq("only_caller: the scan finds every import form", found, [True] * len(forms))


# --- words: a decision typed in the chat (owner, 2026-11-09) ---------------------------

def _owner_says(root, text, mid, when="2026-08-05T14:01:00Z"):
    with root.led().connect() as conn:
        store.insert_owner_message(conn, chat_id=CHAT, tg_message_id=mid, text=text, created_at=when)


def _desk_says(root, text, mid, when="2026-08-05T14:00:30Z"):
    with root.led().connect() as conn:
        store.insert_desk_message(conn, chat_id=CHAT, text=text, source="chat", now=when, tg_message_id=mid)


def words(root, mid, now_dt, **kw):
    kw.setdefault("from_id", OWNER)
    return approvals.handle_words(root.led(), root.cfg(), chat_id=CHAT, owner_message_id=mid,
                                  owner_ids=[OWNER], now_dt=now_dt, **kw)


def case_words():
    # The parser: Python, not a model, reads the owner's words.
    for text, intent, pure in (("yes, approve", "approve", True), ("Yes", "approve", True),
                               ("approve #51", "approve", True), ("do it", "approve", True),
                               ("ok send it", "approve", True), ("yes, approve, fucking do it", "approve", True),
                               ("approve it and what about NVDA", "approve", False), ("no", "reject", True),
                               ("cancel 51", "reject", True), ("yes but drop CEG", None, False),
                               ("yes?", None, False), ("no wait", None, False), ("don't approve", None, False),
                               ("Show it to me", None, False), ("Do you need me to review something", None, False),
                               ("yes and no", None, False), ("sure, tell me more about it", None, False)):
        got = approvals.words_intent(text)
        r.eq(f"words: {text!r} -> {intent}", (got["intent"], got["pure"] if intent else pure), (intent, pure))

    # The live ask: "If I say 'yes, approve,' it's approved." The same claim as a tap.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, _tg = trade(root, fc)
            mid = p["tg_message_id"] + 1
            _owner_says(root, "yes, approve", mid)
            res = words(root, mid, at("2026-08-05T14:01:00Z"))
            r.eq("words: 'yes, approve' approves the card as a tap does",
                 (res["ok"], prop(root, p["id"])["state"], [l["state"] for l in legs(root, p["id"])],
                  [j["kind"] for j in jobs(root, p["id"])], res["touch"]),
                 (True, "approved", ["approved"], ["execute"], ["broker"]))
            r.eq("words: the result names the card to relabel and says so in words",
                 (res["card_message_id"], res["label"], res["messages"]),
                 (p["tg_message_id"], "Approved", [f"Approved #{p['id']}. Sending it now."]))
            _owner_says(root, "approve", mid + 1)
            r.eq("words: a second yes finds no pending card and changes nothing",
                 (words(root, mid + 1, at("2026-08-05T14:01:30Z")), len(jobs(root, p["id"]))), (None, 1))
        finally:
            fc.unpin()

    # A weak word after the desk asked something else is not a decision for Python; the desk
    # reads it, and its approve_card (from_desk) may decide the named card.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, _tg = trade(root, fc)
            _desk_says(root, "Want me to look into NVDA too?", p["tg_message_id"] + 1)
            mid = p["tg_message_id"] + 2
            _owner_says(root, "yes", mid)
            r.eq("words: a weak yes after a desk question -> no decision", words(root, mid, at("2026-08-05T14:01:00Z")),
                 None)
            r.eq("words: ... the card stays pending", prop(root, p["id"])["state"], "pending")
            _owner_says(root, "yes approve", mid + 1)
            res = words(root, mid + 1, at("2026-08-05T14:01:10Z"))
            r.eq("words: a strong word decides even after a desk question", (res["ok"], prop(root, p["id"])["state"]),
                 (True, "approved"))
        finally:
            fc.unpin()
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, _tg = trade(root, fc)
            _desk_says(root, "It trims CEG. Want to go ahead?", p["tg_message_id"] + 1)
            mid = p["tg_message_id"] + 2
            _owner_says(root, "ok do it", mid)
            res = words(root, mid, at("2026-08-05T14:01:00Z"), proposal_id=p["id"], from_desk=True)
            r.eq("words: the desk's approve_card decides the named card from the owner's 'ok do it'",
                 (res["ok"], prop(root, p["id"])["state"]), (True, "approved"))
        finally:
            fc.unpin()

    # Refusals: a hedge, a question, a stranger, a card sent after the message, a card the desk
    # names that is not in the chat, and the owner's words that say nothing about a card.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, _tg = trade(root, fc)
            base = p["tg_message_id"]
            for i, text in enumerate(("yes but drop CEG", "approve?", "show me the card")):
                _owner_says(root, text, base + 1 + i)
                r.eq(f"words: {text!r} decides nothing", words(root, base + 1 + i, at("2026-08-05T14:01:00Z")), None)
            _owner_says(root, "yes approve", base + 10)
            r.eq("words: a stranger's id decides nothing",
                 words(root, base + 10, at("2026-08-05T14:01:00Z"), from_id=123), None)
            _owner_says(root, "yes approve", base - 1, when="2026-08-05T13:59:00Z")
            r.eq("words: a yes from before the card decides nothing", words(root, base - 1, at("2026-08-05T14:01:00Z")),
                 None)
            res = words(root, base + 10, at("2026-08-05T14:01:00Z"), proposal_id=p["id"] + 7, from_desk=True)
            r.eq("words: the desk names a card that is not in the chat -> refused",
                 (res.get("ok"), res.get("refused")), (False, True))
            _owner_says(root, "what do you think of NVDA", base + 11)
            r.eq("words: the desk's approve_card on talk -> no decision",
                 words(root, base + 11, at("2026-08-05T14:01:00Z"), proposal_id=p["id"], from_desk=True), None)
            r.eq("words: after every refusal the card is still pending", prop(root, p["id"])["state"], "pending")
        finally:
            fc.unpin()

    # Two cards in the chat: a bare yes is not Python's to pick; the card number is.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, tg = trade(root, fc)
            ch = _change(root, fc, "2026-08-05T14:00:10Z")
            mid = max(p["tg_message_id"], ch["tg_message_id"]) + 1
            _owner_says(root, "yes approve", mid)
            r.eq("words: two cards + a bare yes -> no decision", words(root, mid, at("2026-08-05T14:01:00Z")), None)
            _owner_says(root, f"approve {p['id']}", mid + 1)
            res = words(root, mid + 1, at("2026-08-05T14:01:05Z"))
            r.eq("words: 'approve <n>' picks that card", (res["ok"], prop(root, p["id"])["state"],
                                                          prop(root, ch["id"])["state"]), (True, "approved", "pending"))
            _owner_says(root, "yes", mid + 2)
            res = words(root, mid + 2, at("2026-08-05T14:01:10Z"))
            r.eq("words: a yes on the one change card left confirms it",
                 (res["ok"], prop(root, ch["id"])["state"], _setting(root, "min_trade_notional")[0]),
                 (True, "applied", 10.0))
        finally:
            fc.unpin()

    # "no" rejects; KILL refuses a yes as it refuses a tap.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T13:35:00Z").pin_market()
        try:
            p, _tg = trade(root, fc)
            _kill(root)
            _owner_says(root, "yes, approve", p["tg_message_id"] + 1)
            res = words(root, p["tg_message_id"] + 1, at("2026-08-05T14:01:00Z"))
            r.eq("words: KILL refuses a yes and keeps the card pending",
                 (res["ok"], prop(root, p["id"])["state"], jobs(root, p["id"])), (False, "pending", []))
            r.ok("words: ... and says why in words", res["messages"] and "stopped" in res["messages"][0], res)
            _owner_says(root, "no", p["tg_message_id"] + 2)
            res = words(root, p["tg_message_id"] + 2, at("2026-08-05T14:01:10Z"))
            r.eq("words: 'no' rejects the card", (res["ok"], prop(root, p["id"])["state"], res["messages"]),
                 (True, "rejected", [f"Rejected #{p['id']}. Nothing was sent."]))
        finally:
            fc.unpin()

    # "cancel" stops a held order before its send time, as Cancel does.
    with fresh_root() as root:
        fc = FakeClock("2026-08-05T20:40:00Z").pin_market()
        try:
            p, _tg = trade(root, fc, snap="2026-08-05T20:40:00Z", at="2026-08-05T20:44:00Z")
            res = tap(root.led(), root.cfg(), cb(p), at("2026-08-05T20:46:00Z"))
            r.eq("words: the tap holds the order", (res["ok"], res.get("held")), (True, "2026-08-06T13:40:00Z"))
            _owner_says(root, "cancel it", p["tg_message_id"] + 2, when="2026-08-05T21:00:00Z")
            res = words(root, p["tg_message_id"] + 2, at("2026-08-05T21:00:00Z"))
            r.eq("words: 'cancel it' cancels the held order",
                 (res["ok"], prop(root, p["id"])["state"], [j["status"] for j in jobs(root, p["id"])]),
                 (True, "rejected", ["failed"]))
        finally:
            fc.unpin()


def main() -> int:
    for case in (case_identity, case_state, case_gate, case_held, case_claim_once, case_txn, case_reject,
                 case_only_caller, case_words):
        try:
            case()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
