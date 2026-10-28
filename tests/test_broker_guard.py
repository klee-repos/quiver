#!/usr/bin/env python3
"""The broker hook ``deploy/runner/broker_guard.py`` (plan §11.3, §13.2, §18 test table).

A placeable leg comes from the real path: ``seed_desk`` + ``snapshot`` + ``proposals.build_trade``
+ ``approvals.handle_tap`` (Approve) + ``execution.exec_prepare`` + a running ``broker_jobs`` row
(``run_broker.Broker.mark``) + ``execution.exec_check`` on an ``rh_snap_file`` + ``record_review``
on the captured ``rh_review_buy`` / ``rh_review_sell`` with values changed to the leg.

Two leg shapes, sized by the producers on the test book (deployable $2,000):
  * buy: ANET 0.6 sh @ $150, target 25% -> a $410.00 dollar buy;
  * sell: CEG 2.603 sh @ $300, target 15% -> a 1.603 sh trim.
A case that needs a state the real path cannot reach on an executing proposal (``proposed``,
``placed``, ``filled``, ``skipped``, an old ``trade_date``) changes that one VALUE on a leg the
path produced. In-process cases set ``broker_guard.ROOT``; subprocess cases pass ``QUIVER_ROOT``.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import (REPO, FakeClock, FakePrices, Results, fx, rh_snap_file, seed_desk,  # noqa: E402
                        snapshot, temp_root)

sys.path.insert(0, str(REPO / "deploy" / "runner"))
import broker_guard  # noqa: E402
import run_broker  # noqa: E402
from lib.desk import approvals, execution, proposals, store  # noqa: E402

r = Results("broker-guard")
PY = str(REPO / ".venv" / "bin" / "python")
GUARD = str(REPO / "deploy" / "runner" / "broker_guard.py")
RH = "mcp__robinhood-trading__"
PLACE = RH + "place_equity_order"
OWNER = "987654321"
CARD_MSG = 9001
ACCT = "TEST1234"
READS = ["get_accounts", "get_portfolio", "get_equity_positions", "get_equity_quotes", "get_equity_orders",
         "get_trade_approval_setting", "get_trade_approvals", "review_equity_order"]

SHAPES = {
    "buy": {"tickers": ["ANET"], "positions": {"ANET": 0.6, "CEG": 1, "SGOV": 11}, "buying_power": 510.0,
            "equity": 2000.0},
    "sell": {"tickers": ["CEG"], "positions": {"ANET": 2, "CEG": 2.603, "SGOV": 9}, "buying_power": 19.1,
             "equity": 2000.0},
}
QUOTES = {"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0}


class FakeBox:
    """The broker runner's outbox seam: records each text."""

    def __init__(self):
        self.sent = []

    def send(self, text, **kw):
        self.sent.append(text)
        return {"ok": True, "message_id": None}


class Leg:
    """One leg made by the real path, and the facts a case needs."""

    def __init__(self, root, clock, shape: str, *, t0: str = "2026-08-05T14:00:00Z", stop: str = "reviewed",
                 review_mod=None):
        self.root, self.clock, self.shape = root, clock, shape
        sh = SHAPES[shape]
        clock.set(t0)
        seed_desk(root, load=False)
        snapshot(root, at=store.iso(clock.now - timedelta(minutes=1)), positions=sh["positions"],
                 buying_power=sh["buying_power"], equity=sh["equity"], quotes=QUOTES)
        self.led, self.cfg = root.led(), root.cfg()
        with self.led.transaction() as c:
            built = proposals.build_trade(self.led, c, cfg=self.cfg, tickers=sh["tickers"],
                                          origin={"owner_message_id": 101}, fraction=1.0, rationale="guard test",
                                          evidence=[], created_by="desk", role="desk", turn_owner_ids=[101],
                                          yf_price_fn=FakePrices(), now_dt=clock.now)
        self.built = built
        self.pid = built["proposal_id"]
        with self.led.connect() as c:
            c.execute("UPDATE proposals SET tg_chat_id=?, tg_message_id=? WHERE id=?", (OWNER, CARD_MSG, self.pid))
            nonce = c.execute("SELECT nonce FROM proposals WHERE id=?", (self.pid,)).fetchone()[0]
        clock.advance(10)
        self.tap = approvals.handle_tap(self.led, self.cfg, callback_data=f"p:{self.pid}:{nonce}:a", from_id=OWNER,
                                        chat_id=OWNER, message_id=CARD_MSG, owner_ids=[OWNER], now_dt=clock.now)
        clock.advance(10)
        self.prep = execution.exec_prepare(self.led, self.cfg, self.pid, now_dt=clock.now)
        with self.led.connect() as c:
            self.job = c.execute("SELECT id FROM broker_jobs WHERE proposal_id=? AND kind='execute'",
                                 (self.pid,)).fetchone()[0]
        self.broker = run_broker.Broker(led=self.led, cfg=self.cfg, box=FakeBox())
        self.broker.mark(self.job, "running")
        self.env = {"QUIVER_BROKER_PROPOSAL": str(self.pid), "QUIVER_BROKER_JOB": str(self.job),
                    "RH_ACCOUNT_NUMBER": ACCT}
        self.place_input = None
        if stop == "prepared":
            return
        clock.advance(10)
        self.check_out = self.check("sells")
        if shape == "buy":
            if stop == "sells":
                return
            self.check_out = self.check("buys")
        legs = self.check_out.get("legs") or []
        self.place_input = dict(legs[0]) if legs else None
        self.ref = self.place_input["ref_id"] if self.place_input else None
        if stop == "checked":
            return
        clock.advance(2)
        self.review_res = self.review(mod=review_mod)
        clock.advance(2)

    def snap_file(self, **kw):
        sh = SHAPES[self.shape]
        return rh_snap_file(positions=kw.get("positions", sh["positions"]), quotes=kw.get("quotes", QUOTES),
                            buying_power=sh["buying_power"], equity=sh["equity"], at=store.iso(self.clock.now))

    def check(self, phase, file_obj=None):
        prev = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            return execution.exec_check(self.led, self.cfg, self.pid, phase, file_obj or self.snap_file(),
                                        now_dt=self.clock.now)
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def review(self, *, mod=None, now=None):
        """``record_review`` on the captured answer of this side, with values set to the leg."""
        cap = copy.deepcopy(fx("rh_review_buy" if self.place_input["side"] == "buy" else "rh_review_sell"))
        ti = cap["tool_input"]
        data = cap["tool_response"]["data"]
        ti["account_number"] = self.place_input["account_number"]
        ti["symbol"] = data["symbol"] = self.place_input["symbol"]
        if "dollar_amount" in ti:
            ti["dollar_amount"] = data["dollar_amount"] = self.place_input["dollar_amount"]
        else:
            ti["quantity"] = data["quantity"] = self.place_input["quantity"]
        if mod:
            mod(cap)
        return broker_guard.record_review(cap, env=self.env, now=now or self.clock.now)

    def row(self):
        with self.led.connect() as c:
            return dict(c.execute("SELECT * FROM orders WHERE ref_id=?", (self.ref,)).fetchone())

    def set_leg(self, **vals):
        sets = ", ".join(f"{k}=?" for k in vals)
        with self.led.connect() as c:
            c.execute(f"UPDATE orders SET {sets} WHERE ref_id=?", (*vals.values(), self.ref))

    def evaluate(self, ti=None, *, env=None, now=None):
        return broker_guard.evaluate(PLACE, self.place_input if ti is None else ti,
                                     env=self.env if env is None else env, now=now or self.clock.now)

    def check_pure(self, ti):
        conn = broker_guard._connect()
        try:
            return broker_guard.check_place(conn, ti, env=self.env, now=self.clock.now,
                                            kill_path=Path(self.cfg.kill_switch_file))
        finally:
            conn.close()


def with_root(fn):
    """Run ``fn(root, clock)`` in a fresh temp root with a pinned clock."""
    clock = FakeClock("2026-08-05T14:00:00Z").pin_market()
    try:
        with temp_root() as root:
            broker_guard.ROOT = root.path
            fn(root, clock)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.ok(f"{fn.__name__}: ran without an exception", False, f"{type(e).__name__}: {e}")
    finally:
        clock.unpin()


def run_main_inproc(payload, *, env_extra=None, argv=None, stdout=None):
    """``broker_guard.main`` in this process (it reads the pinned clock). Returns
    (exit code, stdout text)."""
    prev_in, prev_out = sys.stdin, sys.stdout
    prev_env = {k: os.environ.get(k) for k in (env_extra or {})}
    out = stdout or io.StringIO()
    sys.stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    sys.stdout = out
    os.environ.update(env_extra or {})
    try:
        code = broker_guard.main(argv or [])
    finally:
        sys.stdin, sys.stdout = prev_in, prev_out
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, out.getvalue()


def run_sub(payload, *, root_path, cwd=None, argv=(), env_extra=None):
    """``broker_guard.py`` as the CLI starts it. Returns (exit code, stdout, stderr)."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "QUIVER_ROOT": str(root_path)}
    env.update(env_extra or {})
    p = subprocess.run([PY, GUARD, *argv], input=payload if isinstance(payload, str) else json.dumps(payload),
                       capture_output=True, text=True, timeout=60, env=env, cwd=str(cwd or root_path))
    return p.returncode, p.stdout, p.stderr


def is_deny_output(code, out, err=None) -> bool:
    """The deny contract: exit 2 and the PreToolUse deny JSON of today's order_guard.py."""
    try:
        o = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    hs = o.get("hookSpecificOutput") or {}
    ok = (code == 2 and hs.get("hookEventName") == "PreToolUse" and hs.get("permissionDecision") == "deny"
          and isinstance(hs.get("permissionDecisionReason"), str) and o.get("decision") == "block"
          and isinstance(o.get("reason"), str))
    if err is not None:
        ok = ok and "denied" in err
    return ok


def is_allow_output(code, out) -> bool:
    try:
        o = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    hs = o.get("hookSpecificOutput") or {}
    return code == 0 and hs.get("hookEventName") == "PreToolUse" and hs.get("permissionDecision") == "allow"


# --- reads -------------------------------------------------------------------------

def case_reads(root, clock):
    tools = fx("rh_tools")
    names = tools["tools"]
    r.eq("reads: rh_tools holds the 91 Robinhood tool names", len(names), 91)
    r.ok("reads: the 8 read tools are real Robinhood tools", set(READS) <= set(names), sorted(set(READS) - set(names)))
    allowed = [n for n in names if n != "place_equity_order" and broker_guard.evaluate(RH + n, {}, env={})[0]]
    r.eq("reads: exactly the 8 read tools pass", sorted(allowed), sorted(READS))
    r.ok("reads: place_equity_order is in rh_tools (it has its own gate)", "place_equity_order" in names)
    others = [n for n in names if n not in READS and n != "place_equity_order"]
    denied = [n for n in others if not broker_guard.evaluate(RH + n, {"ref_id": "x"}, env={})[0]]
    r.eq("reads: every other rh_tools name is denied", len(denied), len(others))
    for n in ("cancel_equity_order", "place_advanced_order", "place_crypto_order", "place_option_order",
              "exercise_option", "decline_trade_approval", "add_to_watchlist", "create_alert", "create_scan"):
        r.ok(f"reads: {n} is denied", not broker_guard.evaluate(RH + n, {}, env={})[0])
    code, out, err = run_sub({"tool_name": RH + "get_portfolio", "tool_input": {}}, root_path=root.path)
    r.ok("reads: the CLI allows get_portfolio with exit 0 and the allow JSON", is_allow_output(code, out), (code, out, err))
    code, out, err = run_sub({"tool_name": RH + "cancel_equity_order", "tool_input": {"order_id": "x"}},
                             root_path=root.path)
    r.ok("reads: the CLI denies cancel_equity_order with exit 2", is_deny_output(code, out, err), (code, out, err))
    # A server of another name is not Robinhood.
    r.ok("reads: get_portfolio of another server is denied",
         not broker_guard.evaluate("mcp__other__get_portfolio", {}, env={})[0])


# --- place_ok ----------------------------------------------------------------------

class SpyOut(io.StringIO):
    """A stdout that reads the leg row from a second connection when the allow is printed."""

    def __init__(self, db, ref):
        super().__init__()
        self.db, self.ref, self.at_print = db, ref, None

    def write(self, s):
        if '"allow"' in s and self.at_print is None:
            c = sqlite3.connect(str(self.db))
            try:
                row = c.execute("SELECT state, submitted_at FROM orders WHERE ref_id=?", (self.ref,)).fetchone()
            finally:
                c.close()
            self.at_print = tuple(row) if row else None
        return super().write(s)


def case_place_ok(root, clock):
    leg = Leg(root, clock, "buy")
    r.eq("place_ok: the producer sized the ANET buy at $410.00", leg.place_input.get("dollar_amount"), "410.00")
    r.eq("place_ok: the review passed", leg.review_res[0], 1)
    before = leg.row()
    r.ok("place_ok: the leg is checked with no submitted_at before the call",
         before["state"] == "checked" and before["submitted_at"] is None, before)
    spy = SpyOut(root.db, leg.ref)
    code, out = run_main_inproc({"tool_name": PLACE, "tool_input": leg.place_input}, env_extra=leg.env, stdout=spy)
    r.ok("place_ok: all rules hold -> allow (exit 0, allow JSON)", is_allow_output(code, out), (code, out))
    r.eq("place_ok: the row is placing with submitted_at committed before allow is printed",
         spy.at_print, ("placing", store.iso(clock.now)))
    first_sub = leg.row()["submitted_at"]

    # A retry with no new checked_at -> deny.
    clock.advance(5)
    ok, why = leg.evaluate()
    r.ok("place_ok: a retry with no new checked_at is denied", not ok and "new check" in why, why)
    r.eq("place_ok: the denied retry writes nothing", leg.row()["submitted_at"], first_sub)

    # A retry after exec-check --phase recover wrote a new checked_at and a new review -> allow.
    clock.advance(60)
    leg.broker.mark(leg.job, "done")
    leg.broker.recover_lost()
    r.ok("place_ok: recover_lost tells the owner it lost track of the order",
         any("I lost track of the ANET order" in t for t in leg.broker.box.sent), leg.broker.box.sent)
    with leg.led.connect() as c:
        rjob = c.execute("SELECT id FROM broker_jobs WHERE proposal_id=? AND recovery=1 AND status='queued'",
                         (leg.pid,)).fetchone()[0]
    leg.broker.mark(rjob, "running")
    leg.env["QUIVER_BROKER_JOB"] = str(rjob)
    execution.exec_prepare(leg.led, leg.cfg, leg.pid, recovery=True, now_dt=clock.now)
    rec_file = leg.snap_file()
    rec_file["get_equity_orders"] = fx("rh_orders_live")["get_equity_orders"]  # no order of this leg
    out_rec = leg.check("recover", rec_file)
    r.ok("place_ok: exec-check --phase recover returns the same ref_id to re-send",
         out_rec.get("action") == "place" and [l["ref_id"] for l in out_rec.get("legs", [])] == [leg.ref], out_rec)
    row = leg.row()
    r.ok("place_ok: recover wrote a new checked_at after the first submitted_at and reset review_ok",
         row["state"] == "placing" and row["checked_at"] > first_sub and row["review_ok"] == 0, row)
    ok, why = leg.evaluate()
    r.ok("place_ok: the re-send before its new review is denied", not ok, why)
    clock.advance(2)
    leg.review()
    clock.advance(2)
    ok, why = leg.evaluate()
    r.ok("place_ok: the re-send with the same ref_id after a new check and review is allowed", ok, why)
    row = leg.row()
    r.ok("place_ok: the re-send keeps the first submitted_at and the ref_id",
         row["state"] == "placing" and row["submitted_at"] == first_sub and row["ref_id"] == leg.ref, row)


class FailingConn:
    """A connection whose placing write fails (a full disk, a lock)."""

    def __init__(self, real):
        self.real = real

    def execute(self, sql, *a):
        if sql.lstrip().startswith("UPDATE orders SET state='placing'"):
            raise sqlite3.OperationalError("disk I/O error")
        return self.real.execute(sql, *a)

    def close(self):
        self.real.close()


def case_place_failed_write(root, clock):
    leg = Leg(root, clock, "buy")
    real_connect = broker_guard._connect
    broker_guard._connect = lambda: FailingConn(real_connect())
    try:
        code, out = run_main_inproc({"tool_name": PLACE, "tool_input": leg.place_input}, env_extra=leg.env)
    finally:
        broker_guard._connect = real_connect
    r.ok("place_ok: a failed placing write -> deny (exit 2)", is_deny_output(code, out), (code, out))
    row = leg.row()
    r.ok("place_ok: after the failed write the leg is still checked with no submitted_at",
         row["state"] == "checked" and row["submitted_at"] is None, row)


# --- place_gate --------------------------------------------------------------------

def case_place_gate(root, clock):
    leg = Leg(root, clock, "buy")
    env = dict(leg.env)
    no_pid = {k: v for k, v in env.items() if k != "QUIVER_BROKER_PROPOSAL"}
    r.ok("place_gate: QUIVER_BROKER_PROPOSAL unset (a snapshot job) -> deny", not leg.evaluate(env=no_pid)[0])
    r.ok("place_gate: QUIVER_BROKER_PROPOSAL of another proposal -> deny",
         not leg.evaluate(env={**env, "QUIVER_BROKER_PROPOSAL": str(leg.pid + 1)})[0])
    # A running job for another proposal: a snapshot job (proposal_id NULL) in running.
    with leg.led.connect() as c:
        sj = store.enqueue_snapshot_job(c, requested_by="desk", now=store.now_iso())
    leg.broker.mark(sj, "running")
    r.ok("place_gate: QUIVER_BROKER_JOB of a job for another proposal -> deny",
         not leg.evaluate(env={**env, "QUIVER_BROKER_JOB": str(sj)})[0])
    r.ok("place_gate: QUIVER_BROKER_JOB of no job -> deny",
         not leg.evaluate(env={**env, "QUIVER_BROKER_JOB": "999999"})[0])
    checked = store.parse_ts(leg.row()["checked_at"])
    ok, why = leg.evaluate(now=checked + timedelta(seconds=121))
    r.ok("place_gate: checked_at 121 s old -> deny", not ok and "120" in why, why)
    leg.root.kill.write_text("stop", encoding="utf-8")
    ok, why = leg.evaluate()
    r.ok("place_gate: KILL -> deny", not ok and "KILL" in why, why)
    leg.root.kill.unlink()
    day = "2026-08-05"
    leg.led.mark_halted(day, "test halt")
    ok, why = leg.evaluate()
    r.ok("place_gate: halted today -> deny", not ok and "halted" in why, why)
    leg.led.clear_halt(day, "test")
    # Control: the same leg passes when every rule holds (checked 119 s ago).
    ok, why = leg.evaluate(now=checked + timedelta(seconds=119))
    r.ok("place_gate: control -> the leg passes with checked_at 119 s old", ok, why)
    # A placing leg whose checked_at is older than its submitted_at.
    ok, why = leg.evaluate(now=checked + timedelta(seconds=110))
    r.ok("place_gate: a placing leg whose checked_at is older than its submitted_at -> deny",
         not ok and "new check" in why, why)


def case_place_gate_approved(root, clock):
    leg = Leg(root, clock, "buy", stop="sells")
    with leg.led.connect() as c:
        row = dict(c.execute("SELECT * FROM orders WHERE proposal_id=?", (leg.pid,)).fetchone())
    ti = execution.leg_fields(leg.cfg, row)
    ok, why = leg.evaluate(ti)
    r.ok("place_gate: leg approved (not checked) -> deny", row["state"] == "approved" and not ok, (row["state"], why))


def case_place_gate_job_done(root, clock):
    leg = Leg(root, clock, "buy")
    leg.broker.mark(leg.job, "done")
    ok, why = leg.evaluate()
    r.ok("place_gate: QUIVER_BROKER_JOB not running -> deny", not ok and "job" in why, why)


def case_place_gate_close(root, clock):
    # Tap 10 minutes before the 16:00 ET close; the check runs 125 s before the close.
    leg = Leg(root, clock, "buy", t0="2026-08-05T19:49:30Z", stop="prepared")
    clock.set("2026-08-05T19:57:55Z")
    leg.check_out = leg.check("sells")
    leg.check_out = leg.check("buys")
    leg.place_input = dict(leg.check_out["legs"][0])
    leg.ref = leg.place_input["ref_id"]
    clock.set("2026-08-05T19:57:57Z")
    leg.review()
    close = store.parse_ts("2026-08-05T20:00:00Z")
    ok, why = leg.evaluate(now=close - timedelta(seconds=119))
    r.ok("place_gate: 119 s to the close -> deny", not ok and "close" in why, why)
    ok, why = leg.evaluate(now=close - timedelta(seconds=120))
    r.ok("place_gate: 120 s to the close -> deny (more than 120 s must remain)", not ok, why)
    ok, why = leg.evaluate(now=close - timedelta(seconds=121))
    r.ok("place_gate: 121 s to the close -> allow", ok, why)


def case_place_gate_review(root, clock):
    leg = Leg(root, clock, "buy", stop="checked")
    r.eq("place_gate: a checked leg starts with review_ok=0", leg.row()["review_ok"], 0)
    ok, why = leg.evaluate()
    r.ok("place_gate: review_ok=0 (no review) -> deny", not ok and "review" in why, why)

    def needs_app(cap):
        cap["tool_response"]["data"]["customer_approval_required"] = True
    clock.advance(2)
    res = leg.review(mod=needs_app)
    r.eq("place_gate: a review with customer_approval_required true gives review_ok=0", res[0], 0)
    ok, why = leg.evaluate()
    r.ok("place_gate: review_ok=0 (approval in the app) -> deny", not ok, why)
    # A review older than checked_at.
    checked = store.parse_ts(leg.row()["checked_at"])
    res = leg.review(now=checked - timedelta(seconds=1))
    r.eq("place_gate: the early review itself is clean", res[0], 1)
    ok, why = leg.evaluate()
    r.ok("place_gate: reviewed_at older than checked_at -> deny", not ok and "older" in why, why)
    # The same answer once more, now after the check -> allow (the row was the only fault).
    clock.advance(1)
    leg.review()
    clock.advance(1)
    r.ok("place_gate: control -> a fresh clean review then passes", leg.evaluate()[0])


def case_review_crash(root, clock):
    leg = Leg(root, clock, "buy", stop="checked")
    code, out = run_main_inproc({"tool_name": RH + "review_equity_order", "tool_input": "not an object",
                                 "tool_response": "x"}, env_extra=leg.env, argv=["--record-review"])
    r.ok("place_gate: a crash of the record hook exits non-zero", code != 0, (code, out))
    r.eq("place_gate: after the record-hook crash review_ok stays 0", leg.row()["review_ok"], 0)
    r.ok("place_gate: after the record-hook crash the place is denied", not leg.evaluate()[0])
    # The CLI forms of tool_response: a JSON string, and a list of MCP text blocks.
    cap = copy.deepcopy(fx("rh_review_buy"))
    ti = cap["tool_input"]
    ti["symbol"] = cap["tool_response"]["data"]["symbol"] = leg.place_input["symbol"]
    ti["dollar_amount"] = cap["tool_response"]["data"]["dollar_amount"] = leg.place_input["dollar_amount"]
    for label, resp in (("string", json.dumps(cap["tool_response"])),
                        ("content blocks", [{"type": "text", "text": json.dumps(cap["tool_response"])}])):
        leg.set_leg(review_ok=0, reviewed_at=None)
        clock.advance(1)
        code, out = run_main_inproc({"tool_name": RH + "review_equity_order", "tool_input": ti,
                                     "tool_response": resp}, env_extra=leg.env, argv=["--record-review"])
        r.ok(f"place_gate: --record-review reads the {label} form -> review_ok=1",
             code == 0 and leg.row()["review_ok"] == 1, (code, out, leg.row()["review_ok"]))


# --- tool_search -------------------------------------------------------------------

def case_tool_search(root, clock):
    ev = broker_guard.evaluate
    allowed_q = "select:" + ",".join(RH + n for n in READS + ["place_equity_order"])
    r.ok("tool_search: a select: of the allowed names passes", ev("ToolSearch", {"query": allowed_q}, env={})[0])
    r.ok("tool_search: a select: of one read passes", ev("ToolSearch", {"query": "select:" + RH + "get_portfolio"}, env={})[0])
    r.ok("tool_search: a keyword query is denied",
         not ev("ToolSearch", {"query": "robinhood place order"}, env={})[0])
    r.ok("tool_search: select:mcp__robinhood-trading__cancel_equity_order is denied",
         not ev("ToolSearch", {"query": "select:" + RH + "cancel_equity_order"}, env={})[0])
    r.ok("tool_search: an allowed name plus a denied name is denied",
         not ev("ToolSearch", {"query": "select:" + RH + "get_portfolio," + RH + "place_option_order"}, env={})[0])
    r.ok("tool_search: select: of a tool of another server is denied",
         not ev("ToolSearch", {"query": "select:mcp__desk__say"}, env={})[0])
    r.ok("tool_search: select: of Bash is denied", not ev("ToolSearch", {"query": "select:Bash"}, env={})[0])
    r.ok("tool_search: an empty select: is denied", not ev("ToolSearch", {"query": "select:"}, env={})[0])


# --- settings ----------------------------------------------------------------------

def case_settings(root, clock):
    s = json.loads((REPO / "deploy" / "runner" / "settings.json").read_text(encoding="utf-8"))
    r.eq("settings: settings.json has only the hooks key (and a comment)", set(s) - {"_comment"}, {"hooks"})
    h = s.get("hooks") or {}
    r.eq("settings: exactly PreToolUse and PostToolUse", set(h), {"PreToolUse", "PostToolUse"})
    want_pre = [{"matcher": ".*", "hooks": [{"type": "command",
                 "command": "/opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/broker_guard.py",
                 "timeout": 30, "onFailure": "block"}]}]
    want_post = [{"matcher": "mcp__robinhood-trading__review_equity_order", "hooks": [{"type": "command",
                  "command": "/opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/broker_guard.py --record-review",
                  "timeout": 30, "onFailure": "block"}]}]
    r.eq("settings: the PreToolUse hook of §11.3", h.get("PreToolUse"), want_pre)
    r.eq("settings: the PostToolUse hook of §11.3", h.get("PostToolUse"), want_post)
    c = json.loads((REPO / "deploy" / "runner" / "canary_fail.json").read_text(encoding="utf-8"))
    pre = (c.get("hooks") or {}).get("PreToolUse") or []
    cmds = [hk.get("command") for m in pre for hk in m.get("hooks", [])]
    r.eq("settings: canary_fail.json has one PreToolUse hook, /bin/false", cmds, ["/bin/false"])
    r.ok("settings: the canary hook matches every tool and blocks on failure",
         pre and pre[0].get("matcher") == ".*" and pre[0]["hooks"][0].get("onFailure") == "block", pre)


# --- place_row ---------------------------------------------------------------------

def case_place_row(root, clock):
    leg = Leg(root, clock, "buy")
    ti = dict(leg.place_input)
    r.ok("place_row: no ref_id -> deny", not leg.evaluate({k: v for k, v in ti.items() if k != "ref_id"})[0])
    r.ok("place_row: an unknown ref_id -> deny", not leg.evaluate({**ti, "ref_id": str(uuid.uuid4())})[0])
    for st in ("proposed", "approved", "placed", "filled", "skipped"):
        leg.set_leg(state=st)
        ok, why = leg.evaluate()
        r.ok(f"place_row: row {st} -> deny", not ok, why)
    leg.set_leg(state="checked")
    leg.set_leg(trade_date="2026-08-04")
    ok, why = leg.evaluate()
    r.ok("place_row: trade_date yesterday -> deny", not ok and "today" in why, why)
    leg.set_leg(trade_date="2026-08-05")
    with leg.led.connect() as c:
        exp = c.execute("SELECT expires_at FROM proposals WHERE id=?", (leg.pid,)).fetchone()[0]
        c.execute("UPDATE proposals SET state='approved' WHERE id=?", (leg.pid,))
    r.ok("place_row: proposal approved (not executing) -> deny", not leg.evaluate()[0])
    with leg.led.connect() as c:
        c.execute("UPDATE proposals SET state='executing', expires_at=? WHERE id=?",
                  (store.iso(clock.now - timedelta(seconds=1)), leg.pid))
    ok, why = leg.evaluate()
    r.ok("place_row: proposal expired -> deny", not ok and "expired" in why, why)
    with leg.led.connect() as c:
        c.execute("UPDATE proposals SET expires_at=? WHERE id=?", (exp, leg.pid))
    r.ok("place_row: control -> the leg passes the row rules (pure check)", leg.check_pure(ti)[0])
    with leg.led.transaction() as c:
        moved = proposals.requote_move(c, leg.pid)
    ok, why = leg.evaluate()
    r.ok("place_row: proposal requoted -> deny", moved and not ok, (moved, why))


# --- place_body --------------------------------------------------------------------

def case_place_body(root, clock):
    for shape in ("sell", "buy"):
        with temp_root() as sub:
            broker_guard.ROOT = sub.path
            leg = Leg(sub, clock, shape)
            ti = dict(leg.place_input)
            r.ok(f"place_body[{shape}]: control -> the produced fields pass", leg.check_pure(ti)[0], leg.check_pure(ti))
            other = "CEG" if ti["symbol"] == "ANET" else "ANET"
            for k, v in (("symbol", other), ("side", "buy" if ti["side"] == "sell" else "sell"), ("type", "limit")):
                ok, why = leg.check_pure({**ti, k: v})
                r.ok(f"place_body[{shape}]: another {k} -> deny", not ok, why)
            if shape == "sell":
                r.eq("place_body[sell]: the producer sized the CEG trim at 1.603000 sh", ti["quantity"], "1.603000")
                r.ok("place_body[sell]: quantity 1.603001 -> deny", not leg.check_pure({**ti, "quantity": "1.603001"})[0])
                r.ok("place_body[sell]: quantity 1.603000 -> pass", leg.check_pure({**ti, "quantity": "1.603000"})[0])
                r.ok("place_body[sell]: both quantity and dollar_amount -> deny",
                     not leg.check_pure({**ti, "dollar_amount": "481.00"})[0])
            else:
                r.eq("place_body[buy]: the producer sized the ANET buy at $410.00", ti["dollar_amount"], "410.00")
                r.ok("place_body[buy]: dollar_amount 410.01 -> deny", not leg.check_pure({**ti, "dollar_amount": "410.01"})[0])
                for v in ("410", "410.0", "410.00"):
                    r.ok(f"place_body[buy]: dollar_amount {v} -> pass", leg.check_pure({**ti, "dollar_amount": v})[0])
                r.ok("place_body[buy]: both dollar_amount and quantity -> deny",
                     not leg.check_pure({**ti, "quantity": "2.733333"})[0])
            r.ok(f"place_body[{shape}]: market_hours other -> deny",
                 not leg.check_pure({**ti, "market_hours": "extended_hours"})[0])
            r.ok(f"place_body[{shape}]: market_hours absent -> deny",
                 not leg.check_pure({k: v for k, v in ti.items() if k != "market_hours"})[0])
            r.ok(f"place_body[{shape}]: time_in_force gtc -> deny", not leg.check_pure({**ti, "time_in_force": "gtc"})[0])
            r.ok(f"place_body[{shape}]: time_in_force absent -> deny",
                 not leg.check_pure({k: v for k, v in ti.items() if k != "time_in_force"})[0])
            r.ok(f"place_body[{shape}]: non-empty tax_lots -> deny",
                 not leg.check_pure({**ti, "tax_lots": [{"id": "lot1", "quantity": "1"}]})[0])
            r.ok(f"place_body[{shape}]: tax_lots null -> pass", leg.check_pure({**ti, "tax_lots": None})[0])
            r.ok(f"place_body[{shape}]: limit_price -> deny", not leg.check_pure({**ti, "limit_price": "150.00"})[0])
            r.ok(f"place_body[{shape}]: stop_price -> deny", not leg.check_pure({**ti, "stop_price": "140.00"})[0])
            r.ok(f"place_body[{shape}]: another account_number (G12) -> deny",
                 not leg.check_pure({**ti, "account_number": "OTHER999"})[0])
            r.ok(f"place_body[{shape}]: account_number absent -> deny",
                 not leg.check_pure({k: v for k, v in ti.items() if k != "account_number"})[0])
            ok, why = leg.evaluate(ti)
            r.ok(f"place_body[{shape}]: the produced fields place end to end", ok, why)
    broker_guard.ROOT = root.path


# --- write -------------------------------------------------------------------------

def case_write(root, clock):
    ev = lambda p, extra=None: broker_guard.evaluate("Write", {"file_path": p, "content": "{}", **(extra or {})}, env={})[0]  # noqa: E731
    r.ok("write: state/tmp/x.json passes", ev("state/tmp/x.json"))
    r.ok("write: the absolute state/tmp/x.json passes", ev(str(root.path / "state" / "tmp" / "x.json")))
    r.ok("write: state/tmp/x.txt is denied", not ev("state/tmp/x.txt"))
    r.ok("write: x.txt is denied", not ev("x.txt"))
    r.ok("write: state/tmp/../ledger.db is denied", not ev("state/tmp/../ledger.db"))
    r.ok("write: state/tmp/../x.json is denied", not ev("state/tmp/../x.json"))
    r.ok("write: state/x.json is denied", not ev("state/x.json"))
    link = root.path / "state" / "tmp" / "evil.json"
    os.symlink("/etc/passwd", link)
    r.ok("write: a .json symlink to /etc/passwd is denied", not ev("state/tmp/evil.json"))
    r.ok("write: x.json with the hook cwd at state/tmp resolves against the root -> denied",
         not ev("x.json", {"cwd": str(root.path / "state" / "tmp")}))
    code, out, err = run_sub({"tool_name": "Write", "tool_input": {"file_path": "state/tmp/x.json", "content": "{}"},
                              "cwd": "/"}, root_path=root.path, cwd="/")
    r.ok("write: the CLI allows state/tmp/x.json (paths resolve against QUIVER_ROOT)", is_allow_output(code, out),
         (code, out, err))
    code, out, err = run_sub({"tool_name": "Write", "tool_input": {"file_path": "state/tmp/evil.json", "content": "x"}},
                             root_path=root.path)
    r.ok("write: the CLI denies the symlink with exit 2", is_deny_output(code, out, err), (code, out, err))
    for t in ("Edit", "MultiEdit", "NotebookEdit"):
        r.ok(f"write: {t} is denied", not broker_guard.evaluate(t, {"file_path": "state/tmp/x.json"}, env={})[0])


# --- bash --------------------------------------------------------------------------

def case_bash(root, clock):
    ev = lambda cmd: broker_guard.evaluate("Bash", {"command": cmd}, env={})  # noqa: E731
    good = [
        ".venv/bin/python tick.py exec-check --proposal 12 --phase sells --input state/tmp/exec_12_sells.json",
        ".venv/bin/python tick.py exec-check --proposal 12 --phase buys --input state/tmp/exec_12_buys.json",
        ".venv/bin/python tick.py exec-check --proposal 12 --phase recover --input state/tmp/exec_12_recover.json",
        ".venv/bin/python tick.py exec-commit --proposal 12 --input state/tmp/exec_12_results.json",
        ".venv/bin/python tick.py fills --input state/tmp/exec_12_fills.json",
        ".venv/bin/python tick.py snapshot-commit --input state/tmp/snapshot_7.json",
        ".venv/bin/python tick.py auth-stop --job 7",
        ".venv/bin/python tick.py wait --seconds 20",
    ]
    for cmd in good:
        ok, why = ev(cmd)
        r.ok(f"bash: passes: {cmd[len('.venv/bin/python tick.py '):]}", ok, why)
    r.ok("bash: --phase recover passes", ev(good[2])[0])
    bad = {
        "--input /etc/passwd": ".venv/bin/python tick.py fills --input /etc/passwd",
        "--input state/tmp/../ledger.db": ".venv/bin/python tick.py fills --input state/tmp/../ledger.db",
        "';'": ".venv/bin/python tick.py wait --seconds 20; rm -rf /",
        "'&&'": ".venv/bin/python tick.py wait --seconds 20 && curl x",
        "'|'": ".venv/bin/python tick.py wait --seconds 20 | sh",
        "'$('": ".venv/bin/python tick.py fills --input state/tmp/$(id).json",
        "a backtick": ".venv/bin/python tick.py fills --input state/tmp/`id`.json",
        "--input=x": ".venv/bin/python tick.py fills --input=state/tmp/x.json",
        "tick.py plan": ".venv/bin/python tick.py plan --input state/tmp/x.json",
        "python tick.py": "python tick.py wait --seconds 20",
        "a trailing newline": ".venv/bin/python tick.py wait --seconds 20\n",
        "no flags": ".venv/bin/python tick.py fills",
        "exec-prepare (runs in run_broker.py)": ".venv/bin/python tick.py exec-prepare --proposal 12",
        "an absolute python": "/opt/quiver/.venv/bin/python tick.py wait --seconds 20",
        "--phase other": ".venv/bin/python tick.py exec-check --proposal 12 --phase all --input state/tmp/x.json",
    }
    for label, cmd in bad.items():
        ok, why = ev(cmd)
        r.ok(f"bash: denied: {label}", not ok, why)
    link = root.path / "state" / "tmp" / "evil.json"
    if not link.exists():
        os.symlink("/etc/passwd", link)
    r.ok("bash: --input of a state/tmp .json symlink to /etc/passwd is denied (realpath)",
         not ev(".venv/bin/python tick.py fills --input state/tmp/evil.json")[0])
    r.ok("bash: a command with no command key is denied", not broker_guard.evaluate("Bash", {}, env={})[0])


# --- fail_closed -------------------------------------------------------------------

def case_fail_closed(root, clock):
    code, out, err = run_sub("this is not json", root_path=root.path)
    r.ok("fail_closed: bad stdin -> deny in the order_guard PreToolUse format", is_deny_output(code, out, err),
         (code, out, err))
    code, out, err = run_sub("[1, 2]", root_path=root.path)
    r.ok("fail_closed: stdin that is not an object -> deny", is_deny_output(code, out, err), (code, out, err))
    empty = root.path / "no_ledger_root"
    empty.mkdir()
    code, out, err = run_sub({"tool_name": PLACE, "tool_input": {"ref_id": str(uuid.uuid4()), "symbol": "ANET"}},
                             root_path=empty, env_extra={"QUIVER_BROKER_PROPOSAL": "1", "QUIVER_BROKER_JOB": "1"})
    r.ok("fail_closed: no ledger -> deny", is_deny_output(code, out, err) and "ledger" in out, (code, out, err))
    code, out, err = run_sub({"tool_name": "Write", "tool_input": ["state/tmp/x.json"]}, root_path=root.path)
    r.ok("fail_closed: an exception in the guard -> deny", is_deny_output(code, out, err) and "error" in out,
         (code, out, err))
    code, out, err = run_sub({"tool_name": "Frobnicate", "tool_input": {}}, root_path=root.path)
    r.ok("fail_closed: an unknown tool -> deny", is_deny_output(code, out, err), (code, out, err))
    code, out, err = run_sub({"tool_name": "Read", "tool_input": {"file_path": "config.yaml"}}, root_path=root.path)
    r.ok("fail_closed: Read -> deny", is_deny_output(code, out, err), (code, out, err))
    code, out, err = run_sub({}, root_path=root.path)
    r.ok("fail_closed: an empty payload -> deny", is_deny_output(code, out, err), (code, out, err))
    # The 10-second SQLite busy timeout (§11.3), so the guard answers before the 30 s hook timeout.
    root.led()  # a fresh ledger file in this root
    conn = broker_guard._connect()
    try:
        r.eq("fail_closed: the guard sets a 10-second SQLite busy timeout",
             conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
    finally:
        conn.close()
    # The guard self-test of run_broker.py: a synthetic place with an unknown ref_id exits 2.
    code, out, err = run_sub({"tool_name": PLACE, "tool_input": {"ref_id": "00000000-0000-0000-0000-000000000000",
                                                               "symbol": "ZZZZ", "side": "buy", "type": "market"}},
                             root_path=root.path)
    r.ok("fail_closed: the run_broker self-test payload -> exit 2", code == 2, (code, out, err))


def main() -> int:
    for case in (case_reads, case_place_ok, case_place_failed_write, case_place_gate, case_place_gate_approved,
                 case_place_gate_job_done, case_place_gate_close, case_place_gate_review, case_review_crash,
                 case_tool_search, case_settings, case_place_row, case_place_body, case_write, case_bash,
                 case_fail_closed):
        with_root(case)
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
