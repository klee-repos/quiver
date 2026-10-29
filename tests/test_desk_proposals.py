"""Trade proposals (plan §10.1-§10.2; §18.4 ``test_desk_proposals.py``; §18.9 G7, G9, G10).

The test values are the §18.4 header: the TEST_BOOK book (ANET 25 band 5, CEG 15 band 5,
SGOV 60 cash band 10), buffer $5, ``min_trade_notional`` $5, and the snapshot ANET 2 sh at
$150.00, CEG 1 at $300.00, SGOV 11 at $100.00, buying power $300.00 (deployable $2,000).

The ledger state comes only from the real producers:
  * ``seed_desk`` (the real ``desk-seed``) on the SEC captures. The seed sees ``sub_anet``
    with its newest entries dropped (rule 4), so the real EDGAR sweep then finds the 10-Q
    0001596532-26-000175 (a ``filing`` event), the 8-K 0001596532-26-000174 (an
    ``earnings`` event) and the 8-K 0001596532-26-000118 (a ``record`` tier filing).
  * ``events.load_company`` for TSM and SPCX (candidates, not in the book).
  * ``snapshot`` (``snapshot-commit`` on ``rh_snap`` with values changed).
  * ``store.insert_owner_message`` (the bridge), ``events.queue_analysis`` (the monitor),
    ``tools.t_record_verdict`` and ``findings.record_finding`` (the analyst),
    ``levels.check_levels`` (the monitor), ``approvals.handle_tap`` (the tap),
    ``execution.exec_prepare`` / ``exec_check`` / ``build_requote`` (the execute job).

One seeded and swept ledger is made once; each case starts from a copy of it in its own
``temp_root()``.
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
import traceback
import urllib.parse
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from desk_fakes import (TEST_BOOK, FakeClock, FakeConn, FakePrices, FakeResolver, Results,
                        SecFixtures, fx, fx_bytes, rh_snap_file, seams, seed_desk, snapshot, temp_root)

from lib import market, notify, sizing
from lib.desk import (agent, approvals, documents, events, execution, findings, levels, news,
                      outbox, proposals, store, tools)
from lib.desk import settings as desk_settings
from lib.desk.proposals import ProposalError
from lib.dataflows.yfinance_news import fetch_news_items

r = Results("desk-proposals")

T0 = "2026-08-05T14:00:00Z"          # 10:00 ET, a Wednesday session
OWNER = 424242
CHAT = "424242"
ACC_10Q = "0001596532-26-000175"
ACC_8K = "0001596532-26-000174"
ACC_RECORD = "0001596532-26-000118"
HTML_CT = {"Content-Type": "text/html; charset=utf-8"}


def PX():
    return FakePrices({"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0, "TSM": 100.0, "SPCX": 50.0})


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- the template ledger (built once) -----------------------------------------------

_TEMPLATE = {}


def _drop_new_filings(data):
    """Rule 4: DROP the 57 newest entries (and the 8-K 5.07 at index 96) of ``sub_anet``."""
    rec = data["filings"]["recent"]
    for k in rec:
        rec[k] = rec[k][57:96] + rec[k][97:]
    return data


def template_db() -> Path:
    if "db" in _TEMPLATE:
        return _TEMPLATE["db"]
    tmp = tempfile.mkdtemp(prefix="t2_props_tpl_")
    atexit.register(shutil.rmtree, tmp, True)
    out = Path(tmp) / "ledger.db"
    with temp_root() as root:
        clk = FakeClock(T0).pin_market()
        try:
            seed_desk(root, opener=SecFixtures(drop={"sub_anet": _drop_new_filings}))
            led, cfg = root.led(), root.cfg()
            with led.connect() as c:
                sett = desk_settings.effective(c, cfg)
            sw = events.sweep(led, settings=sett, seams=seams(), statuses=("holding",))
            assert len(sw["events"]) == 2, sw
            for t in ("TSM", "SPCX"):
                events.load_company(led, t, settings=sett, seams=seams(), status="candidate")
            src, dst = sqlite3.connect(str(root.db)), sqlite3.connect(str(out))
            src.backup(dst)
            src.close()
            dst.close()
        finally:
            clk.unpin()
    _TEMPLATE["db"] = out
    return out


class Ctx:
    def __init__(self, root, clk):
        self.root = root
        self.clk = clk
        self.led = root.led()
        self.cfg = root.cfg()
        self._mid = 900
        self.tg = FakeTG()
        self.box = outbox.Outbox(self.led, transport=self.tg, chat_id=CHAT, sleep=lambda s: None)

    def now(self) -> str:
        return store.iso(self.clk.now)

    def now_dt(self):
        return self.clk.now

    def at(self, when: str) -> None:
        self.clk.set(when)

    def gid(self) -> int:
        with self.led.connect() as c:
            return c.execute("SELECT id FROM strategy_goal WHERE status='active'").fetchone()[0]

    def q(self, sql, args=()):
        with self.led.connect() as c:
            return [dict(x) for x in c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def owner_msg(self, text="please rebalance this", at=None) -> int:
        self._mid += 1
        with self.led.connect() as c:
            store.insert_owner_message(c, chat_id=CHAT, tg_message_id=self._mid, text=text,
                                       created_at=at or self.now())
        return self._mid

    def doc_id(self, accession, key) -> int:
        return self.one("SELECT id FROM documents WHERE accession=? AND section_key=?", (accession, key))["id"]

    def finding(self, doc_id: int, at: str, ticker="ANET") -> int:
        with self.led.connect() as c:
            d = store.get_document(c, doc_id)
            quote = findings.normalize(d["text"]).strip()[:150]
            return findings.record_finding(c, ticker=ticker, kind="other", claim="the analyst read this",
                                           quote=quote, document_id=doc_id, created_by="analyst", now=at)

    def snap(self, at=None, **kw):
        return snapshot(self.root, at=at or store.iso(self.clk.now - timedelta(minutes=5)), **kw)

    def prop(self, pid):
        with self.led.connect() as c:
            return proposals.get_proposal(c, pid)

    def legs(self, pid):
        return self.led.legs(pid)


class FakeTG:
    """The Bot API transport seam of ``outbox.Outbox``: records each call."""

    def __init__(self):
        self.calls = []
        self.mid = 5000

    def __call__(self, method, params):
        self.calls.append((method, json.loads(json.dumps(params))))
        if method == "sendMessage":
            self.mid += 1
            return {"ok": True, "result": {"message_id": self.mid}}
        return {"ok": True, "result": True}


@contextlib.contextmanager
def case(at=T0):
    tpl = template_db()
    with temp_root() as root:
        (root.path / "strategy.yaml").write_text(TEST_BOOK, encoding="utf-8")
        src, dst = sqlite3.connect(str(tpl)), sqlite3.connect(str(root.db))
        src.backup(dst)
        src.close()
        dst.close()
        clk = FakeClock(at).pin_market()
        try:
            yield Ctx(root, clk)
        finally:
            clk.unpin()


def build(c: Ctx, tickers, **kw):
    kw.setdefault("yf_price_fn", PX())
    with c.led.transaction() as conn:
        return proposals.build_trade(c.led, conn, cfg=c.cfg, tickers=list(tickers), **kw)


def owner_trade(c: Ctx, tickers, fraction=1.0, *, text="please rebalance this", evidence=(), mid=None, **kw):
    if mid is None:
        mid = c.owner_msg(text)
    return build(c, tickers, origin={"owner_message_id": mid}, fraction=fraction, rationale="You asked for it.",
                 evidence=list(evidence), created_by="desk", role="desk", turn_owner_ids=[mid], **kw)


def drift_trade(c: Ctx, tickers, **kw):
    return build(c, tickers, origin={}, python_origin="drift", rationale="The book drifted outside the agreed bands.",
                 evidence=[], created_by="monitor", **kw)


def event_trade(c: Ctx, eid, tickers=("ANET",), evidence=(), fraction=None, **kw):
    return build(c, tickers, origin={"event_id": eid}, fraction=fraction, rationale="The analyst read the new facts.",
                 evidence=list(evidence), created_by="analyst", role="analyst", **kw)


def refusal(fn):
    """The ProposalError text, or None when ``fn`` did not refuse."""
    try:
        fn()
    except ProposalError as e:
        return str(e)
    return None


class _VerdictCtx:
    """The two members ``tools.t_record_verdict`` reads: ``conn()`` and ``job``."""

    def __init__(self, led):
        self.led = led
        self.job = None

    def conn(self):
        return self.led.connect()


def verdict(c: Ctx, eid: int, v: str):
    return tools.t_record_verdict(_VerdictCtx(c.led), {"event_id": eid, "verdict": v,
                                                        "summary": f"the analyst finds the thesis {v}"})


def queue(c: Ctx):
    return events.queue_analysis(c.led, now=c.now())


def tap(c: Ctx, pid: int, action="a", at=None):
    p = c.prop(pid)
    if p["tg_message_id"] is None:
        with c.led.connect() as conn:   # what outbox.send_card writes
            conn.execute("UPDATE proposals SET tg_chat_id=?, tg_message_id=? WHERE id=?", (CHAT, 70000 + pid, pid))
        p = c.prop(pid)
    return approvals.handle_tap(c.led, c.cfg, callback_data=f"p:{pid}:{p['nonce']}:{action}", from_id=OWNER,
                                chat_id=CHAT, message_id=p["tg_message_id"], owner_ids=[OWNER],
                                now_dt=at or c.now_dt(), yf_price_fn=PX())


def card_text(c: Ctx, built) -> str:
    with c.led.connect() as conn:
        return " ".join(notify.render_trade_card(proposals.trade_card_model(conn, built)))


def leg(built, ticker):
    return next((l for l in built["legs"] if l["ticker"] == ticker), None)


def near(a, b, tol=1e-6):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# --- refuse ---------------------------------------------------------------------------

def case_refuse():
    with case() as c:
        c.snap(at="2026-08-05T13:35:00Z")
        r.ok("refuse: control drift proposal builds", drift_trade(c, ["ANET"])["legs"])
        c.root.kill.write_text("stop")
        for name, fn in (("drift", lambda: drift_trade(c, ["ANET"])), ("owner", lambda: owner_trade(c, ["ANET"]))):
            e = refusal(fn)
            r.ok(f"refuse: KILL refuses a {name} trade", e is not None and "KILL" in e, e)
        c.root.kill.unlink()
        c.led.mark_halted(market.trading_day_et(), "daily loss 21% (test)")
        for name, fn in (("drift", lambda: drift_trade(c, ["ANET"])), ("owner", lambda: owner_trade(c, ["ANET"]))):
            e = refusal(fn)
            r.ok(f"refuse: halted=1 refuses a {name} trade", e is not None and "halted" in e, e)
    with case() as c:
        c.snap(at="2026-08-05T13:35:00Z")
        c.at("2026-08-06T15:35:01Z")          # 26 h + 1 s
        e = refusal(lambda: drift_trade(c, ["ANET"]))
        r.eq("refuse: a snapshot 26 h + 1 s old", e, "portfolio snapshot is stale; refresh it first")
        e = refusal(lambda: owner_trade(c, ["ANET"]))
        r.eq("refuse: a snapshot 26 h + 1 s old (owner)", e, "portfolio snapshot is stale; refresh it first")
        c.at("2026-08-06T15:34:00Z")          # 25 h 59 min
        r.ok("refuse: a 25 h 59 min snapshot passes", refusal(lambda: drift_trade(c, ["ANET"])) is None)
    with case() as c:
        c.snap()
        e = refusal(lambda: owner_trade(c, ["TSM"]))
        r.ok("refuse: a name not in the book", e is not None and "not in the book; propose add_company" in e, e)
        c.led.set_holding_status(c.gid(), "CEG", "removed", c.now())
        e = refusal(lambda: owner_trade(c, ["CEG"]))
        r.ok("refuse: a removed name", e is not None and "not in the book; propose add_company" in e, e)


# --- buy ------------------------------------------------------------------------------

def case_buy():
    with case() as c:
        c.snap()
        got = {}
        for f in (0.25, 0.5, 1.0):
            b = owner_trade(c, ["ANET"], f)
            got[f] = leg(b, "ANET")
            r.eq(f"buy: deployable is $2,000 (fraction {f})", round(b["deployable"], 2), 2000.0)
        r.eq("buy: T - v = $200 x 0.25 -> $50.00", (got[0.25]["side"], got[0.25]["dollars"]), ("buy", 50.0))
        r.eq("buy: x 0.5 -> $100.00", got[0.5]["dollars"], 100.0)
        r.eq("buy: x 1.0 -> $200.00", got[1.0]["dollars"], 200.0)
        e = refusal(lambda: owner_trade(c, ["ANET"], 0.3))
        r.ok("buy: fraction 0.3 refused", e is not None and "0.25, 0.5 or 1.0" in e, e)
    with case() as c:
        c.snap(buying_power=100.0)
        b = owner_trade(c, ["ANET"], 1.0)
        # money = min(bp - buffer, bp + SGOV value - floor) = min(95, 100 + 1100 - 60% x 1800)
        r.eq("buy: buying power $100 -> $95.00", leg(b, "ANET")["dollars"], 95.0)
    with case() as c:
        # ANET 2 at $150, CEG 1 at $300, SGOV 6 at $100, buying power $19.96: deployable
        # $1,219.96, T = $304.99, so T - v = $4.99.
        c.snap(positions={"ANET": 2, "CEG": 1, "SGOV": 6}, buying_power=19.96, equity=1219.96)
        with c.led.connect() as conn:
            base = proposals.valuation_base(conn, proposals.latest_snapshot(conn), c.now_dt(), PX())
        gap = 0.25 * base["deployable"] - base["values"]["ANET"]
        r.ok("buy: the producer gives T - v = $4.99", near(gap, 4.99, 1e-6), gap)
        e = refusal(lambda: owner_trade(c, ["ANET"], 1.0))
        r.ok("buy: T - v $4.99 -> no leg", e is not None and "ANET" in e and "within $5.00" in e, e)


# --- sell -----------------------------------------------------------------------------

def case_sell():
    with case() as c:
        c.snap(positions={"ANET": 5, "CEG": 1, "SGOV": 11}, equity=2450.0)
        want_full = sizing.resolve_target_sell_quantity(5.0, 150.0, 750.0, 612.5)
        r.eq("sell: the producer gives 0.916667 sh to the exact target", want_full, 0.916667)
        for f in (0.25, 0.5, 1.0):
            b = owner_trade(c, ["ANET"], f)
            lg = leg(b, "ANET")
            r.eq(f"sell: deployable $2,450 (fraction {f})", round(b["deployable"], 2), 2450.0)
            r.ok(f"sell: 0.916667 x {f}", lg and lg["side"] == "sell" and near(lg["quantity"], 0.916667 * f, 1.01e-6),
                 lg)
            r.ok(f"sell: never above held (fraction {f})", lg and lg["quantity"] <= 5.0, lg)
            if f == 1.0:
                # G10: the exact target (25%), not the band edge (30%: 0.1 sh).
                r.ok("sell: to the exact target, not the band edge (G10)",
                     near(lg["weight_after"], 25.0, 1e-3) and not near(lg["quantity"], 0.1, 1e-3), lg)
    with case() as c:
        c.snap(positions={"ANET": 1.28534, "CEG": 1, "SGOV": 11})
        c.led.set_holding_status(c.gid(), "ANET", "exiting", c.now())
        b = owner_trade(c, ["ANET"], 0.5)
        r.eq("sell: exiting 1.285340 x 0.5 -> 0.642670", leg(b, "ANET")["quantity"], 0.64267)
        b = owner_trade(c, ["ANET"], 1.0)
        r.eq("sell: exiting x 1.0 -> 1.285340 exactly", leg(b, "ANET")["quantity"], 1.28534)
        r.eq("sell: the reserved row holds the exact quantity",
             c.one("SELECT quantity FROM orders WHERE proposal_id=?", (b["proposal_id"],))["quantity"], 1.28534)
    with case() as c:
        # A full exit is exempt from min_trade_notional: ANET 0.02 sh at $150 is $3.00.
        c.snap(positions={"ANET": 0.02, "CEG": 1, "SGOV": 11})
        c.led.set_holding_status(c.gid(), "ANET", "exiting", c.now())
        b = owner_trade(c, ["ANET"], 1.0)
        r.eq("sell: a $3.00 full exit is exempt from the $5 minimum", (leg(b, "ANET")["side"], leg(b, "ANET")["quantity"]),
             ("sell", 0.02))


# --- entry_max --------------------------------------------------------------------------

def case_entry_max():
    with case() as c:
        c.led.upsert_target_holding(goal_id=c.gid(), sleeve="AI Networking", ticker="ANET", target_weight=25.0,
                                    band=5.0, status="active", book="core_test", quotable=True, proxy_ticker=None,
                                    updated_at=c.now(), entry_price_max=150.0)
        c.snap(at="2026-08-05T13:50:00Z", positions={"ANET": 2, "SGOV": 11},
               quotes={"ANET": 150.01, "CEG": 300.0, "SGOV": 100.0})
        b = owner_trade(c, ["ANET", "CEG"], 1.0)
        r.ok("entry_max: quote 150.01 skips the ANET leg", leg(b, "ANET") is None and leg(b, "CEG") is not None, b["legs"])
        sk = [s for s in b["skipped"] if s["ticker"] == "ANET"]
        r.ok("entry_max: the skip names the price and the limit", sk and "150.01" in sk[0]["reason"]
             and "150.00" in sk[0]["reason"], b["skipped"])
        txt = card_text(c, b)
        r.ok("entry_max: the card names the skipped leg", "I left out ANET" in txt and "$150.01" in txt, txt)
        r.eq("entry_max: no ANET order row", c.one("SELECT COUNT(*) n FROM orders WHERE proposal_id=? AND ticker='ANET'",
                                                  (b["proposal_id"],))["n"], 0)
        c.at("2026-08-05T14:10:00Z")
        c.snap(at="2026-08-05T14:05:00Z", positions={"ANET": 2, "SGOV": 11},
               quotes={"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0})
        b = owner_trade(c, ["ANET", "CEG"], 1.0)
        r.ok("entry_max: quote 150.00 keeps the ANET leg", leg(b, "ANET") is not None
             and leg(b, "ANET")["quote"] == 150.0, b["legs"])


# --- order_scale ----------------------------------------------------------------------

def case_order_scale():
    with case() as c:
        # Deployable $2,000: ANET 2.8 sh at $150 (T - v = $80), CEG 1 at $260 (T - v = $40),
        # SGOV 12.2 at $100, buying power $100: money = min(95, 100 + 1220 - 1200) = $95.
        c.snap(positions={"ANET": 2.8, "CEG": 1, "SGOV": 12.2}, quotes={"ANET": 150.0, "CEG": 260.0, "SGOV": 100.0},
               buying_power=100.0)
        b = owner_trade(c, ["ANET", "CEG"], 1.0)
        r.eq("order_scale: $80 + $40 with $95 free -> $63.33 + $31.67",
             (leg(b, "ANET")["dollars"], leg(b, "CEG")["dollars"]),
             (round(80 * 95 / 120, 2), round(40 * 95 / 120, 2)))
        r.eq("order_scale: the plan numbers", (leg(b, "ANET")["dollars"], leg(b, "CEG")["dollars"]), (63.33, 31.67))
        r.ok("order_scale: scaled is true", b["scaled"] is True, b)
        txt = card_text(c, b)
        r.ok("order_scale: the card says the buys were scaled", "scaled" in txt.lower(), txt)
    with case() as c:
        # cash_floor_pct 50: the cash measure leaves room, so bp - buffer ($95) binds.
        with c.led.connect() as conn:
            desk_settings.write(conn, "cash_floor_pct", 50.0, c.now(), None)
        c.snap(positions={"ANET": 2, "CEG": 1, "SGOV": 11.7}, quotes={"ANET": 190.0, "CEG": 350.0, "SGOV": 100.0},
               buying_power=100.0)
        b = owner_trade(c, ["ANET", "CEG"], 1.0)
        seqs = [(l["seq"], l["ticker"], l["side"]) for l in b["legs"]]
        r.eq("order_scale: sells first by leg_seq", seqs, [(1, "CEG", "sell"), (2, "ANET", "buy")])
        rows = c.q("SELECT leg_seq, ticker, side FROM orders WHERE proposal_id=? ORDER BY leg_seq", (b["proposal_id"],))
        r.eq("order_scale: the reserved rows keep the order", [(x["leg_seq"], x["ticker"], x["side"]) for x in rows],
             [(1, "CEG", "sell"), (2, "ANET", "buy")])
        proceeds = leg(b, "CEG")["quantity"] * leg(b, "CEG")["quote"]
        r.ok("order_scale: about $50 of sell proceeds", near(proceeds, 50.0, 0.01), proceeds)
        r.ok("order_scale: $50 of sell proceeds -> no scaling", b["scaled"] is False and leg(b, "ANET")["dollars"] == 120.0,
             b["legs"])
        b2 = owner_trade(c, ["ANET"], 1.0)
        r.ok("order_scale: without the sell the same buy is scaled to $95", b2["scaled"] is True
             and leg(b2, "ANET")["dollars"] == 95.0, b2["legs"])
    with case() as c:
        # G9: cash_floor_pct 10 (floor $200 at deployable $2,000). Buying power $100, SGOV $1,300.
        with c.led.connect() as conn:
            desk_settings.write(conn, "cash_floor_pct", 10.0, c.now(), None)
        c.snap(positions={"ANET": 2, "CEG": 1, "SGOV": 13}, buying_power=100.0)
        b = owner_trade(c, ["ANET"], 1.0)
        dollars = leg(b, "ANET")["dollars"]
        r.eq("order_scale: G9 the cash measure is buying power + SGOV value (buy $95)", dollars, 95.0)
        r.ok("order_scale: G9 the cash measure stays >= $200", 100.0 + 1300.0 - dollars >= 200.0, dollars)
    with case() as c:
        with c.led.connect() as conn:
            desk_settings.write(conn, "cash_floor_pct", 70.0, c.now(), None)
        c.snap()                    # cash measure $300 + $1,100 = $1,400 = 70% of $2,000
        e = refusal(lambda: owner_trade(c, ["ANET"], 1.0))
        r.ok("order_scale: a floor at the cash measure allows no buy", e is not None and "no order" in e, e)


# --- reserve ----------------------------------------------------------------------------

def case_reserve():
    with case() as c:
        c.snap(positions={"ANET": 2, "CEG": 2, "SGOV": 8})
        b = owner_trade(c, ["ANET", "CEG"], 1.0)
        rows = c.q("SELECT * FROM orders WHERE proposal_id=? ORDER BY leg_seq", (b["proposal_id"],))
        r.eq("reserve: one row per leg", len(rows), len(b["legs"]))
        r.eq("reserve: two legs (a CEG sell, an ANET buy)", [(x["ticker"], x["side"]) for x in rows],
             [("CEG", "sell"), ("ANET", "buy")])
        for x, lg in zip(rows, b["legs"]):
            t = x["ticker"]
            try:
                ok_uuid = uuid.UUID(x["ref_id"]).version == 4
            except ValueError:
                ok_uuid = False
            r.ok(f"reserve: {t} ref_id is a UUID", ok_uuid, x["ref_id"])
            r.eq(f"reserve: {t} state, type, proposal_id, leg_seq",
                 (x["state"], x["type"], x["proposal_id"], x["leg_seq"]), ("proposed", "market", b["proposal_id"], lg["seq"]))
            r.eq(f"reserve: {t} quote_at_proposal", x["quote_at_proposal"], {"ANET": 150.0, "CEG": 300.0}[t])
            r.eq(f"reserve: {t} trade_date = the session date of expires_at (G7)", x["trade_date"], "2026-08-05")
            r.eq(f"reserve: {t} not finalized, not submitted", (x["finalized"], x["submitted_at"]), (0, None))
        sell, buy = rows
        r.ok("reserve: the sell has a quantity and no dollar_amount", sell["quantity"] == 1.0 and sell["dollar_amount"] is None,
             sell)
        r.ok("reserve: the buy has a dollar_amount and no quantity", buy["dollar_amount"] == leg(b, "ANET")["dollars"]
             and buy["dollar_amount"] > 0 and buy["quantity"] is None, buy)
        r.eq("reserve: the ref_ids are distinct", len({x["ref_id"] for x in rows}), 2)
    with case(at="2026-08-05T20:40:00Z") as c:   # 16:40 ET, after the close
        c.snap()
        b = drift_trade(c, ["ANET"])
        p = c.prop(b["proposal_id"])
        r.eq("reserve: a 16:40 proposal expires at the next close", p["expires_at"], "2026-08-06T20:00:00Z")
        r.eq("reserve: its legs carry the session date of expires_at (G7)",
             c.one("SELECT trade_date FROM orders WHERE proposal_id=?", (b["proposal_id"],))["trade_date"], "2026-08-06")


# --- origin_event -----------------------------------------------------------------------

def _event_ids(c):
    rows = {x["dedup_key"]: x["id"] for x in c.q("SELECT id, dedup_key FROM events")}
    return rows[f"filing:{ACC_10Q}"], rows[f"filing:{ACC_8K}"]


def _price_event(conn, c, status):
    """A price_level event row as ``levels.check_levels`` writes it, in a given status."""
    return store.create_event(conn, dedup_key=f"level:ANET:buy_below:{status}", ticker="ANET", type="price_level",
                              now=c.now(), status=status,
                              detail={"level": "buy_below", "price": 155.0, "quote": 150.0, "source": "snapshot:1"})


def case_origin_event():
    with case(at="2026-08-05T14:30:00Z") as c:
        c.snap()
        e10q, e8k = _event_ids(c)
        fresh = c.finding(c.doc_id(ACC_10Q, "10-Q:I-2"), "2026-08-05T14:20:00Z")
        old = c.finding(c.doc_id("0001596532-26-000013", "10-K:7"), "2026-08-05T13:50:00Z")
        with c.led.connect() as conn:
            drift_eid = store.create_event(conn, dedup_key="drift:2026-08-05", ticker="ANET", type="drift",
                                           detail={"tickers": ["ANET"]}, now=c.now(), status="noted")
            rec_eid = store.create_event(conn, dedup_key=f"filing:{ACC_RECORD}", ticker="ANET", type="filing",
                                         detail={"accession": ACC_RECORD, "form": "8-K", "items": "5.07"},
                                         now=c.now(), status="new")
            st_ids = {}
            for st in ("queued", "noted", "failed"):
                st_ids[st] = _price_event(conn, c, st)
        r.eq("origin_event: the sweep made a filing and an earnings event",
             [c.one("SELECT type FROM events WHERE id=?", (i,))["type"] for i in (e10q, e8k)], ["filing", "earnings"])
        r.eq("origin_event: the record filing is tier record",
             c.one("SELECT tier FROM filings WHERE accession=?", (ACC_RECORD,))["tier"], "record")
        queue(c)        # new -> queued: the two sweep events and the record one
        for eid in (e10q, e8k, rec_eid):
            verdict(c, eid, "strengthened")
        with c.led.connect() as conn:
            st_ids["new"] = _price_event(conn, c, "new")
        ev = ["finding:%d" % fresh]
        cases = [
            ("a missing event", lambda: event_trade(c, 999999, evidence=ev), "does not exist"),
            ("type drift", lambda: event_trade(c, drift_eid, evidence=ev), "cannot back a trade"),
            ("a record filing", lambda: event_trade(c, rec_eid, evidence=ev), "not a redecide filing"),
            ("status new", lambda: event_trade(c, st_ids["new"], evidence=ev), "is new"),
            ("status queued", lambda: event_trade(c, st_ids["queued"], evidence=ev), "is queued"),
            ("status noted", lambda: event_trade(c, st_ids["noted"], evidence=ev), "is noted"),
            ("status failed", lambda: event_trade(c, st_ids["failed"], evidence=ev), "is failed"),
            ("a foreign ticker", lambda: event_trade(c, e10q, tickers=("CEG",), evidence=ev), "not CEG"),
            ("evidence older than detected_at", lambda: event_trade(c, e10q, evidence=["finding:%d" % old]),
             "no finding or document from this event"),
        ]
        for name, fn, why in cases:
            e = refusal(fn)
            r.ok(f"origin_event: refused: {name}", e is not None and why in e, e)
        b = event_trade(c, e10q, evidence=ev)
        r.ok("origin_event: an analyzed redecide filing with a fresh finding passes",
             b["origin"] == "event" and c.prop(b["proposal_id"])["event_id"] == e10q, b)
        e = refusal(lambda: event_trade(c, e10q, evidence=ev))
        r.ok("origin_event: refused: a 2nd proposal from the event", e is not None and "already backs" in e, e)
        p = c.prop(b["proposal_id"])
        with c.led.connect() as conn:
            snapv = proposals.latest_snapshot(conn)
        rq = build(c, ["ANET"], origin={}, requote_of=p, rationale=p["rationale"], created_by="system",
                   snap_override=snapv)
        r.ok("origin_event: a Python requote of it passes", c.prop(rq["proposal_id"])["event_id"] == e10q, rq)
        b8 = event_trade(c, e8k, evidence=["document:%d" % c.doc_id(ACC_8K, "8-K:EX-99.1")])
        r.ok("origin_event: the event's own document counts as evidence", b8["legs"], b8)


# --- anti_flip ----------------------------------------------------------------------------

def case_anti_flip():
    with case() as c:
        c.snap()
        old = c.finding(c.doc_id(ACC_10Q, "10-Q:I-2"), "2026-08-05T13:00:00Z")
        buy = owner_trade(c, ["ANET"], 1.0)
        c.at("2026-08-05T14:01:00Z")
        res = tap(c, buy["proposal_id"])
        r.ok("anti_flip: the buy is approved by the tap", res["ok"] and c.prop(buy["proposal_id"])["state"] == "approved", res)
        for lgr in c.legs(buy["proposal_id"]):       # the fill lands (the ledger producer)
            c.led.set_order_state(lgr["ref_id"], "filled")
        proposals.close_from_legs(c.led, buy["proposal_id"], c.now())
        r.eq("anti_flip: the buy ends executed", c.prop(buy["proposal_id"])["state"], "executed")
        new = c.finding(c.doc_id(ACC_10Q, "10-Q:I-1"), "2026-08-10T14:00:00Z")
        c.at("2026-08-15T14:00:00Z")                 # 10 days after the approval
        c.snap(positions={"ANET": 5, "CEG": 1, "SGOV": 11})
        e = refusal(lambda: owner_trade(c, ["ANET"], 1.0, evidence=["finding:%d" % old]))
        r.ok("anti_flip: 10 days: a sell with only older evidence is refused", e is not None, e)
        e = refusal(lambda: owner_trade(c, ["ANET"], 1.0, evidence=["finding:%d" % new]))
        r.ok("anti_flip: 10 days: a finding newer than the approval passes", e is None, e)
        c.at("2026-09-05T14:02:00Z")                 # 31 days after the approval
        c.snap(positions={"ANET": 5, "CEG": 1, "SGOV": 11})
        e = refusal(lambda: owner_trade(c, ["ANET"], 1.0, evidence=["finding:%d" % old]))
        r.ok("anti_flip: 31 days: no rule", e is None, e)


# --- origin_owner -------------------------------------------------------------------------

def _tool_ctx(c, role, turn_ids):
    turn = agent.TurnState()
    turn.start(chat_id=CHAT, owner_message_ids=turn_ids, started_at=c.now())
    return tools.ToolContext(role=role, runtime=None, secrets=None, led=c.led, cfg=c.cfg, turn=turn, box=c.box,
                             yf_price_fn=PX())


def case_origin_owner():
    with case() as c:
        c.snap()
        mid = c.owner_msg("buy more ANET")
        other = c.owner_msg("an older message")
        b = build(c, ["ANET"], origin={"owner_message_id": mid}, fraction=1.0, rationale="You asked.", evidence=[],
                  created_by="desk", role="desk", turn_owner_ids=[mid])
        r.ok("origin_owner: the desk role with an id of this turn passes",
             c.prop(b["proposal_id"])["owner_message_id"] == mid and b["origin"] == "owner", b)
        e = refusal(lambda: build(c, ["ANET"], origin={"owner_message_id": other}, fraction=1.0, rationale="x",
                                  evidence=[], created_by="desk", role="desk", turn_owner_ids=[mid]))
        r.ok("origin_owner: an id from another turn is refused", e is not None, e)
        e = refusal(lambda: build(c, ["ANET"], origin={"owner_message_id": mid}, fraction=1.0, rationale="x",
                                  evidence=[], created_by="analyst", role="analyst", turn_owner_ids=[mid]))
        r.ok("origin_owner: the analyst role is refused", e is not None, e)
        # The tools: the model passes only args. change/drift origins are Python's.
        desk = _tool_ctx(c, "desk", [mid])
        out = tools.t_propose_trade(desk, {"tickers": ["ANET"], "origin": {"owner_message_id": mid}, "fraction": 0.5,
                                           "rationale": "You asked.", "evidence": []})
        r.ok("origin_owner: the desk tool with an id of this turn passes", out["proposal_id"] and out["fraction"] == 0.5, out)
        for org in ({"python_origin": "change"}, {"python_origin": "drift"}, {"origin": "change"}, {"origin": "drift"},
                    {"change": 1}, {"drift": True}, {"parent_change_id": 1}):
            try:
                tools.t_propose_trade(desk, {"tickers": ["ANET"], "origin": org, "rationale": "x", "evidence": []})
                ok, msg = False, "no error"
            except tools.ToolError as e:
                ok, msg = True, str(e)
            r.ok(f"origin_owner: the tool refuses origin {org}", ok, msg)
        res = tools.call(desk, "propose_trade", {"tickers": ["ANET"], "origin": "drift", "rationale": "x", "evidence": []})
        r.ok("origin_owner: a string origin 'drift' is refused", res.get("is_error") is True, res)
        before = c.one("SELECT COUNT(*) n FROM proposals WHERE origin IN ('change','drift')")["n"]
        r.eq("origin_owner: no change or drift proposal came from a tool", before, 0)
        ana = _tool_ctx(c, "analyst", [mid])
        try:
            tools.t_propose_trade(ana, {"tickers": ["ANET"], "origin": {"owner_message_id": mid}, "fraction": 1.0,
                                        "rationale": "x", "evidence": []})
            ok = False
        except tools.ToolError:
            ok = True
        r.ok("origin_owner: the analyst tool with an owner id is refused", ok)
        wrong_turn = _tool_ctx(c, "desk", [other])
        try:
            tools.t_propose_trade(wrong_turn, {"tickers": ["ANET"], "origin": {"owner_message_id": mid}, "fraction": 1.0,
                                               "rationale": "x", "evidence": []})
            ok = False
        except tools.ToolError:
            ok = True
        r.ok("origin_owner: the tool refuses an id from another turn", ok)


# --- supersede ------------------------------------------------------------------------------

def case_supersede():
    with case() as c:
        c.snap(positions={"ANET": 2, "SGOV": 11})          # CEG not held: both names are under target
        old = owner_trade(c, ["ANET", "CEG"], 1.0)
        outbox.send_trade_card(c.led, c.box, c.led.connect, old)
        old_mid = c.prop(old["proposal_id"])["tg_message_id"]
        new = owner_trade(c, ["ANET"], 1.0)
        res = outbox.send_trade_card(c.led, c.box, c.led.connect, new)
        po = c.prop(old["proposal_id"])
        r.eq("supersede: the old one is superseded by the new one", (po["state"], po["superseded_by"]),
             ("superseded", new["proposal_id"]))
        r.eq("supersede: build_trade names it", new["superseded"], [old["proposal_id"]])
        legs = c.legs(old["proposal_id"])
        r.eq("supersede: the old legs (ANET and CEG) end skipped and finalized",
             sorted((l["ticker"], l["state"], l["finalized"]) for l in legs),
             [("ANET", "skipped", 1), ("CEG", "skipped", 1)])
        edits = [p for m, p in c.tg.calls if m == "editMessageReplyMarkup" and p.get("message_id") == old_mid]
        label = edits[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] if edits else None
        r.eq("supersede: the old card's buttons say Replaced by #N", label, f"Replaced by #{new['proposal_id']}")
        t = tap(c, old["proposal_id"])
        r.eq("supersede: a tap on the old card answers Replaced by #N", t["answer"], f"Replaced by #{new['proposal_id']}")
        r.ok("supersede: the new card is sent", res["ok"], res)


# --- expiry ---------------------------------------------------------------------------------

def case_expiry():
    rows = [("2026-08-05T14:00:00Z", "2026-08-05T20:00:00Z", "2026-08-05 10:00 -> 16:00 ET"),
            ("2026-11-27T15:00:00Z", "2026-11-27T18:00:00Z", "2026-11-27 10:00 -> 13:00 ET (half day)"),
            ("2026-11-26T15:00:00Z", "2026-11-27T18:00:00Z", "2026-11-26 (holiday) -> 2026-11-27 13:00 ET"),
            ("2026-10-30T21:00:00Z", "2026-11-02T21:00:00Z", "Fri 2026-10-30 17:00 -> Mon 2026-11-02 16:00 ET")]
    with case() as c:
        for at, want, name in sorted(rows):
            c.at(at)
            c.snap()
            b = drift_trade(c, ["ANET"])
            r.eq(f"expiry: {name}", (b["expires_at"], c.prop(b["proposal_id"])["expires_at"]), (want, want))
            r.eq(f"expiry: {name}: legs carry the session date (G7)",
                 c.one("SELECT trade_date FROM orders WHERE proposal_id=?", (b["proposal_id"],))["trade_date"], want[:10])


# --- card -----------------------------------------------------------------------------------

def case_card():
    with case() as c:
        c.snap()
        b = owner_trade(c, ["ANET"], 1.0)
        p = c.prop(b["proposal_id"])
        with c.led.connect() as conn:
            msgs = notify.render_trade_card(proposals.trade_card_model(conn, b))
        txt = " ".join(msgs)
        plain = html.unescape(re.sub(r"<[^>]+>", "", txt))
        r.ok("card: one message of prose", len(msgs) == 1 and "\n" not in txt, msgs)
        r.ok("card: no bullet", not re.search(r"(^|\s)[•·\-\*]\s", plain) and not re.search(r"^\s*\d+[.)]\s", plain), plain)
        r.ok("card: no ' / '", " / " not in plain, plain)
        for s in ("Buy $200 of ANET, about 1.3 shares at $150.00.", "from 15.0% of the account to the 25.0% we planned",
                  "Open until 4:00 PM ET."):
            r.ok(f"card: says {s!r}", s in plain, plain)
        btn = outbox.trade_buttons(p["id"], p["nonce"])[0]
        r.eq("card: Approve is style success", (btn[0]["text"], btn[0]["style"]), ("Approve", "success"))
        r.eq("card: Reject is style danger", (btn[1]["text"], btn[1]["style"]), ("Reject", "danger"))
        r.eq("card: callback_data p:<id>:<nonce>:a|r", (btn[0]["callback_data"], btn[1]["callback_data"]),
             (f"p:{p['id']}:{p['nonce']}:a", f"p:{p['id']}:{p['nonce']}:r"))
        big = outbox.trade_buttons(9_999_999_999, p["nonce"])[0]
        r.ok("card: callback_data < 64 bytes at a 10-digit id",
             all(len(x["callback_data"].encode()) < 64 for x in big), big)
        r.ok("card: nonce is 10 hex", re.fullmatch(r"[0-9a-f]{10}", p["nonce"] or "") is not None, p["nonce"])
        payload = json.loads(p["payload_json"])
        want = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        r.eq("card: payload_sha256 is SHA-256 of the canonical payload", p["payload_sha256"], want)
        res = outbox.send_trade_card(c.led, c.box, c.led.connect, b)
        sent = [prm for m, prm in c.tg.calls if m == "sendMessage"]
        r.ok("card: the sent card carries the two buttons on its last message",
             sent and sent[-1].get("reply_markup", {}).get("inline_keyboard") == outbox.trade_buttons(p["id"], p["nonce"]),
             sent[-1:] if sent else None)
        r.eq("card: tg_message_id is the last message", c.prop(p["id"])["tg_message_id"], res["message_id"])


# --- drift ----------------------------------------------------------------------------------

def case_drift():
    with case(at="2026-08-05T20:40:00Z") as c:
        c.snap(positions={"ANET": 2, "SGOV": 11})          # deployable $1,700; ANET 17.6%, CEG 0%
        ceg_owner = owner_trade(c, ["CEG"], 1.0)
        first = drift_trade(c, ["CEG"])
        r.eq("drift: the owner CEG one is superseded by the CEG drift one (same ticker)",
             c.prop(ceg_owner["proposal_id"])["state"], "superseded")
        p1 = c.prop(first["proposal_id"])
        r.eq("drift: origin drift, created_by monitor", (p1["origin"], p1["created_by"]), ("drift", "monitor"))
        second = drift_trade(c, ["ANET"])
        r.eq("drift: a new drift proposal supersedes the pending drift one",
             (c.prop(first["proposal_id"])["state"], c.prop(first["proposal_id"])["superseded_by"]),
             ("superseded", second["proposal_id"]))
        txt = html.unescape(re.sub(r"<[^>]+>", "", card_text(c, second)))
        r.ok("drift: the card names weight, target and band per leg",
             "ANET is 17.6% of the account, below the 25.0% we planned, more than the 5.0 points it may drift." in txt, txt)


# --- quote_source ---------------------------------------------------------------------------

def case_quote_source():
    with case() as c:
        full = rh_snap_file(positions={"ANET": 2, "CEG": 0.5, "SGOV": 11},
                            quotes={"ANET": 150.0, "CEG": 290.0, "SGOV": 100.0}, buying_power=300.0,
                            equity=1845.0, at="2026-08-05T13:35:00Z")
        for res in full["get_equity_quotes"]["data"]["results"]:
            if res["quote"]["symbol"] == "CEG":      # values only: a bid and ask around the last trade
                res["quote"]["bid_price"], res["quote"]["ask_price"] = "288.000000", "291.000000"
        execution.snapshot_commit(c.led, c.cfg, full, now_dt=_dt("2026-08-05T13:35:00Z"))
        part = rh_snap_file(positions={"ANET": 2, "CEG": 0.5, "SGOV": 11}, quotes={"ANET": 150.0},
                            buying_power=300.0, equity=1845.0, at="2026-08-05T13:50:00Z")
        execution.snapshot_commit(c.led, c.cfg, part, now_dt=_dt("2026-08-05T13:50:00Z"))
        ids = [x["id"] for x in c.q("SELECT id FROM broker_snapshots ORDER BY id")]
        newest = json.loads(c.one("SELECT quotes_json FROM broker_snapshots WHERE id=?", (ids[-1],))["quotes_json"])
        r.eq("quote_source: the newest row holds ANET only", sorted(newest), ["ANET"])
        px = PX()
        px.prices["CEG"] = 288.5
        b = owner_trade(c, ["CEG"], 1.0, yf_price_fn=px)
        lg = leg(b, "CEG")
        r.eq("quote_source: CEG comes from the older full row (< 60 min)", (lg["quote"], lg["quote_source"]),
             (290.0, f"snapshot:{ids[0]}"))
        r.eq("quote_source: quote_at_proposal is last_trade_price (not the 289.50 mid)",
             c.one("SELECT quote_at_proposal FROM orders WHERE proposal_id=?", (b["proposal_id"],))["quote_at_proposal"],
             290.0)
        c.at("2026-08-05T14:35:00Z")        # the full row is exactly 60 min old: not "less than 60"
        b = owner_trade(c, ["CEG"], 1.0, yf_price_fn=px)
        lg = leg(b, "CEG")
        r.eq("quote_source: at exactly 60 min the row is not < 60 min old: yfinance", (lg["quote"], lg["quote_source"]),
             (288.5, "yfinance"))
        c.at("2026-08-05T14:36:00Z")        # the full row is 61 min old
        b = owner_trade(c, ["CEG"], 1.0, yf_price_fn=px)
        lg = leg(b, "CEG")
        r.eq("quote_source: at 61 min CEG comes from yfinance", (lg["quote"], lg["quote_source"]), (288.5, "yfinance"))
        r.eq("quote_source: quote_at_proposal is the yfinance price",
             c.one("SELECT quote_at_proposal FROM orders WHERE proposal_id=?", (b["proposal_id"],))["quote_at_proposal"],
             288.5)


# --- verdict_table ----------------------------------------------------------------------------

def _level_event(c, ticker, level, price):
    with c.led.connect() as conn:
        levels.upsert_level(conn, ticker, level, price, c.now(), armed=1)
        res = levels.check_levels(conn, now_dt=c.now_dt(), yf_price_fn=PX())
    got = [e for e in res["events"] if c.one("SELECT detail_json FROM events WHERE id=?", (e,))
           and json.loads(c.one("SELECT detail_json FROM events WHERE id=?", (e,))["detail_json"])["level"] == level]
    return got[0] if got else None


def case_verdict_table():
    with case(at="2026-08-05T14:30:00Z") as c:
        c.snap()                                      # ANET 15% vs 25% target
        e10q, e8k = _event_ids(c)
        bb = _level_event(c, "ANET", "buy_below", 155.0)
        en = _level_event(c, "ANET", "entry", 155.0)
        r.ok("verdict_table: check_levels made the buy_below and entry events", bb and en, (bb, en))
        queue(c)
        f = c.finding(c.doc_id(ACC_10Q, "10-Q:I-2"), "2026-08-05T14:31:00Z")
        ev = ["finding:%d" % f]
        e = refusal(lambda: event_trade(c, e10q, evidence=ev))
        r.ok("verdict_table: no recorded verdict -> refused", e is not None and "queued" in e, e)
        verdict(c, bb, "intact")
        b = event_trade(c, bb, evidence=ev, fraction=0.25)
        r.eq("verdict_table: price_level buy_below + intact -> buy, fraction 1.0 (a passed 0.25 is ignored)",
             (b["fraction"], leg(b, "ANET")["side"], leg(b, "ANET")["dollars"]), (1.0, "buy", 200.0))
        verdict(c, e10q, "strengthened")
        b = event_trade(c, e10q, evidence=ev, fraction=1.0)
        r.eq("verdict_table: filing + strengthened -> buy, fraction 0.5 (a passed 1.0 is ignored)",
             (b["fraction"], leg(b, "ANET")["side"], leg(b, "ANET")["dollars"]), (0.5, "buy", 100.0))
        verdict(c, e8k, "broken")
        e = refusal(lambda: event_trade(c, e8k, evidence=ev))
        r.ok("verdict_table: broken -> refused with 'propose drop_company'", e is not None and "propose drop_company" in e, e)
        verdict(c, en, "weakened")
        e = refusal(lambda: event_trade(c, en, evidence=ev))
        r.ok("verdict_table: weakened below target -> refused", e is not None and "not above its target" in e, e)
    with case(at="2026-08-05T14:30:00Z") as c:
        c.snap()
        e10q, e8k = _event_ids(c)
        queue(c)
        f = c.finding(c.doc_id(ACC_10Q, "10-Q:I-2"), "2026-08-05T14:31:00Z")
        verdict(c, e10q, "intact")
        e = refusal(lambda: event_trade(c, e10q, evidence=["finding:%d" % f]))
        r.ok("verdict_table: filing + intact -> refused", e is not None and "intact" in e, e)
        verdict(c, e8k, "strengthened")
        b = event_trade(c, e8k, evidence=["finding:%d" % f])
        r.eq("verdict_table: earnings + strengthened -> buy, fraction 0.5", (b["fraction"], leg(b, "ANET")["side"]),
             (0.5, "buy"))
    with case(at="2026-08-05T14:30:00Z") as c:
        # ANET 3.777778 sh at $150: $566.67 of $2,266.67, at its 25% target.
        c.snap(positions={"ANET": 3.777778, "CEG": 1, "SGOV": 11})
        e10q, _ = _event_ids(c)
        queue(c)
        f = c.finding(c.doc_id(ACC_10Q, "10-Q:I-2"), "2026-08-05T14:31:00Z")
        verdict(c, e10q, "weakened")
        e = refusal(lambda: event_trade(c, e10q, evidence=["finding:%d" % f]))
        r.ok("verdict_table: weakened at target -> refused",
             e is not None and ("not above its target" in e or "within $5.00" in e), e)
    with case(at="2026-08-05T14:30:00Z") as c:
        c.snap(positions={"ANET": 5, "CEG": 1, "SGOV": 11})   # above target: T $612.50 of $2,450
        e10q, _ = _event_ids(c)
        ta = _level_event(c, "ANET", "trim_above", 150.0)
        queue(c)
        f = c.finding(c.doc_id(ACC_10Q, "10-Q:I-2"), "2026-08-05T14:31:00Z")
        verdict(c, e10q, "weakened")
        b = event_trade(c, e10q, evidence=["finding:%d" % f], fraction=0.25)
        lg = leg(b, "ANET")
        r.ok("verdict_table: weakened above target -> sell toward target, fraction 1.0",
             b["fraction"] == 1.0 and lg["side"] == "sell" and near(lg["quantity"], 0.916667), (b["fraction"], lg))
        verdict(c, ta, "strengthened")
        b = event_trade(c, ta, evidence=["finding:%d" % f])
        lg = leg(b, "ANET")
        r.ok("verdict_table: price_level trim_above + any verdict above target -> sell, fraction 1.0",
             b["fraction"] == 1.0 and lg["side"] == "sell" and near(lg["quantity"], 0.916667), (b["fraction"], lg))


# --- desk_news --------------------------------------------------------------------------------

def _desk_news_event(c, pub_age: timedelta):
    """A desk news event by the real chain: feed -> ingest -> store_fetched -> log_news_event."""
    now_dt = c.now_dt()
    feed = copy.deepcopy(fx("yf_news")["news"][:1])
    feed[0]["providerPublishTime"] = int((now_dt - pub_age).timestamp())      # a VALUE
    with c.led.connect() as conn:
        news.ingest(conn, ["ANET"], now=c.now(), fetch_fn=lambda t, lim: fetch_news_items(
            t, lim, search_fn=lambda q, n: feed if q == "ANET" else [], ticker_news_fn=lambda q, n: []))
        link = feed[0]["link"]
        parts = urllib.parse.urlsplit(link)
        path = parts.path + (("?" + parts.query) if parts.query else "")
        conn_fake = FakeConn().add("69.147.88.8", path, 200, fx_bytes("ex991"), HTML_CT)
        doc = documents.store_fetched(conn, link, kind="news", ticker="ANET", now=c.now(),
                                      resolver=FakeResolver({parts.hostname: ["69.147.88.8"]}), connector=conn_fake)
        eid = news.log_news_event(conn, ticker="ANET", document_id=doc, now=c.now())
    queue(c)
    verdict(c, eid, "strengthened")
    return eid, doc, c.one("SELECT url FROM documents WHERE id=?", (doc,))["url"]


def case_desk_news():
    with case(at="2026-08-05T15:00:00Z") as c:
        c.snap()
        eid, doc, url = _desk_news_event(c, timedelta(days=1))
        r.eq("desk_news: log_news_event made a desk event", c.one("SELECT created_by, type FROM events WHERE id=?", (eid,)),
             {"created_by": "desk", "type": "news"})
        b = event_trade(c, eid, evidence=["document:%d" % doc])
        r.ok("desk_news: a 1-day-old feed date backs a trade", b["legs"], b)
        txt = html.unescape(re.sub(r"<[^>]+>", "", card_text(c, b)))
        r.ok("desk_news: the card says 'found by the desk' with the URL", "found by the desk" in txt and url in txt, txt)
    with case(at="2026-08-05T15:00:00Z") as c:
        c.snap()
        eid, doc, _ = _desk_news_event(c, timedelta(days=6, hours=23))
        c.at("2026-08-06T16:00:00Z")      # the feed date is now 8 days old
        c.snap()
        e = refusal(lambda: event_trade(c, eid, evidence=["document:%d" % doc]))
        r.ok("desk_news: 8 days -> refused", e is not None and "7 days" in e, e)
    with case(at="2026-08-05T15:00:00Z") as c:
        c.snap()
        eid, doc, _ = _desk_news_event(c, timedelta(days=1))
        with c.led.connect() as conn:     # a VALUE: the feed gave no date
            conn.execute("UPDATE news_items SET published_at=NULL WHERE document_id=?", (doc,))
        e = refusal(lambda: event_trade(c, eid, evidence=["document:%d" % doc]))
        r.ok("desk_news: a NULL published_at -> refused", e is not None and "7 days" in e, e)


# --- owner_quote ------------------------------------------------------------------------------

def case_owner_quote():
    with case(at="2026-08-05T19:15:00Z") as c:
        c.snap()
        text = "add to ANET a bit, it's under my buy-below & cheap"
        mid = c.owner_msg(text, at="2026-08-05T19:10:00Z")
        b = owner_trade(c, ["ANET"], 0.5, mid=mid)
        txt = html.unescape(re.sub(r"<[^>]+>", "", card_text(c, b)))
        r.ok("owner_quote: the card quotes the owner message word for word with its time",
             f"You asked: '{text}' (3:10 PM)" in txt, txt)


# --- leg_end ----------------------------------------------------------------------------------

def case_leg_end():
    with case() as c:
        c.snap()
        pe = drift_trade(c, ["ANET"])
        with c.led.transaction() as conn:
            moved = proposals.expire_due(conn, _dt("2026-08-05T20:00:00Z"))
        r.ok("leg_end: expire_due moves the pending one", [m["id"] for m in moved] == [pe["proposal_id"]], moved)
        r.eq("leg_end: expire -> legs expired, finalized",
             [(l["state"], l["finalized"]) for l in c.legs(pe["proposal_id"])], [("expired", 1)])
        s1 = owner_trade(c, ["ANET"], 1.0)
        owner_trade(c, ["ANET"], 0.5)
        r.eq("leg_end: supersede -> legs skipped, finalized",
             (c.prop(s1["proposal_id"])["state"], [(l["state"], l["finalized"]) for l in c.legs(s1["proposal_id"])]),
             ("superseded", [("skipped", 1)]))
        w = owner_trade(c, ["ANET"], 1.0)
        with c.led.transaction() as conn:
            ok = proposals.withdraw(conn, w["proposal_id"], created_by="desk", now=c.now())
        r.eq("leg_end: withdraw -> legs skipped, finalized",
             (ok, c.prop(w["proposal_id"])["state"], [(l["state"], l["finalized"]) for l in c.legs(w["proposal_id"])]),
             (True, "withdrawn", [("skipped", 1)]))
    with case() as c:
        c.snap()
        rq = owner_trade(c, ["ANET"], 1.0)
        tap(c, rq["proposal_id"])
        prep = execution.exec_prepare(c.led, c.cfg, rq["proposal_id"], now_dt=c.now_dt())
        r.ok("leg_end: exec-prepare moves it to executing", prep["ok"] and c.prop(rq["proposal_id"])["state"] == "executing",
             prep)
        with c.led.transaction() as conn:
            ok = proposals.requote_move(conn, rq["proposal_id"])
        r.eq("leg_end: requote -> legs requoted, finalized",
             (ok, c.prop(rq["proposal_id"])["state"], [(l["state"], l["finalized"]) for l in c.legs(rq["proposal_id"])]),
             (True, "requoted", [("requoted", 1)]))
        with c.led.transaction() as conn:
            again = proposals.requote_move(conn, rq["proposal_id"])
        r.eq("leg_end: a requote changes no row that left executing", again, False)
        pa = owner_trade(c, ["ANET"], 1.0)
        with c.led.transaction() as conn:
            pend = proposals.requote_move(conn, pa["proposal_id"])
        r.eq("leg_end: a requote of a pending proposal changes nothing",
             (pend, c.prop(pa["proposal_id"])["state"], [l["state"] for l in c.legs(pa["proposal_id"])]),
             (False, "pending", ["proposed"]))
        c.at("2026-08-05T14:02:00Z")
        tap(c, pa["proposal_id"])
        with c.led.transaction() as conn:
            moved = proposals.expire_due(conn, _dt("2026-08-05T20:00:01Z"))
            sup = proposals.end_pending(conn, pa["proposal_id"], "superseded", c.now(), superseded_by=999)
        p = c.prop(pa["proposal_id"])
        r.eq("leg_end: an expire or a supersede changes no row that left pending",
             (pa["proposal_id"] in [m["id"] for m in moved], sup, p["state"], p["superseded_by"],
              [(l["state"], l["finalized"]) for l in c.legs(pa["proposal_id"])]),
             (False, False, "approved", None, [("approved", 0)]))


# --- requote_copy ---------------------------------------------------------------------------

def _requote(c, pid, moved_quotes):
    """Tap Approve, run exec-prepare and the sells exec-check on moved quotes, then build the requote."""
    tap(c, pid)
    prep = execution.exec_prepare(c.led, c.cfg, pid, now_dt=c.now_dt())
    assert prep["ok"], prep
    job = c.one("SELECT id FROM broker_jobs WHERE proposal_id=? AND kind='execute'", (pid,))["id"]
    with c.led.connect() as conn:     # run_broker.py claims the job
        conn.execute("UPDATE broker_jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                     (c.now(), job))
    prev = {k: os.environ.get(k) for k in ("QUIVER_BROKER_PROPOSAL", "QUIVER_BROKER_JOB")}
    os.environ["QUIVER_BROKER_PROPOSAL"], os.environ["QUIVER_BROKER_JOB"] = str(pid), str(job)
    try:
        f = rh_snap_file(positions={"ANET": 2, "CEG": 1, "SGOV": 11}, quotes=moved_quotes, buying_power=300.0,
                         equity=2000.0, at=c.now())
        chk = execution.exec_check(c.led, c.cfg, pid, "sells", f, now_dt=c.now_dt())
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    snap_id = c.one("SELECT id FROM broker_snapshots ORDER BY id DESC LIMIT 1")["id"]
    res = execution.build_requote(c.led, c.cfg, pid, now_dt=c.now_dt(), yf_price_fn=PX())
    return chk, res, snap_id


FIELDS = ("origin", "event_id", "owner_message_id", "parent_change_id", "rationale", "evidence_json")


def case_requote_copy():
    moved = {"ANET": 155.0, "CEG": 300.0, "SGOV": 100.0}
    with case(at="2026-08-05T14:30:00Z") as c:
        c.snap()
        e10q, _ = _event_ids(c)
        queue(c)
        f = c.finding(c.doc_id(ACC_10Q, "10-Q:I-2"), "2026-08-05T14:31:00Z")
        verdict(c, e10q, "strengthened")
        b = event_trade(c, e10q, evidence=["finding:%d" % f])
        old = c.prop(b["proposal_id"])
        c.at("2026-08-05T14:32:00Z")
        chk, res, snap_id = _requote(c, b["proposal_id"], moved)
        r.eq("requote_copy: exec-check requotes a 3.3% move", chk.get("action"), "requote")
        new = c.prop(res["new"]["proposal_id"]) if res and res.get("new") else None
        r.ok("requote_copy: event origin: a new proposal is built", new is not None, res)
        if new:
            r.eq("requote_copy: event origin: copies origin, event_id, owner_message_id, parent_change_id, "
                 "rationale and evidence", {k: new[k] for k in FIELDS}, {k: old[k] for k in FIELDS})
            lg = res["new"]["legs"][0]
            # The exec-check snapshot: ANET 2 at $155 -> deployable $2,010, T $502.50, gap $192.50 x 0.5.
            r.eq("requote_copy: sizes from the exec-check snapshot",
                 (lg["quote"], lg["quote_source"], lg["dollars"]), (155.0, f"snapshot:{snap_id}", 96.25))
            r.eq("requote_copy: skips the origin checks (the event already backs a proposal)", new["event_id"], e10q)
    with case(at="2026-08-05T14:30:00Z") as c:
        c.snap()
        b = owner_trade(c, ["ANET"], 1.0, text="top up ANET")
        old = c.prop(b["proposal_id"])
        c.at("2026-08-05T14:32:00Z")
        chk, res, snap_id = _requote(c, b["proposal_id"], moved)
        new = c.prop(res["new"]["proposal_id"]) if res and res.get("new") else None
        r.ok("requote_copy: owner origin: a new proposal is built", new is not None, res)
        if new:
            r.eq("requote_copy: owner origin: copies the six fields", {k: new[k] for k in FIELDS},
                 {k: old[k] for k in FIELDS})
            r.eq("requote_copy: owner origin: sized from the exec-check snapshot",
                 (res["new"]["legs"][0]["quote"], res["new"]["legs"][0]["dollars"]), (155.0, 192.5))
    with case(at="2026-08-05T14:30:00Z") as c:
        c.snap()
        with c.led.transaction() as conn:
            ch = proposals.build_change(conn, c.cfg, change={"kind": "set_weights", "changes": [
                {"ticker": "ANET", "target_weight": 30}, {"ticker": "SGOV", "target_weight": 55}]},
                rationale="More networking.", evidence=[], created_by="desk")
        conf = tap(c, ch["proposal_id"])
        fu = conf["cards"][0] if conf.get("cards") else None
        r.ok("requote_copy: change origin: the Confirm built a follow-up", fu is not None, conf)
        if fu:
            old = c.prop(fu)
            c.at("2026-08-05T14:32:00Z")
            chk, res, snap_id = _requote(c, fu, moved)
            new = c.prop(res["new"]["proposal_id"]) if res and res.get("new") else None
            r.ok("requote_copy: change origin: copies parent_change_id and the rest",
                 new is not None and {k: new[k] for k in FIELDS} == {k: old[k] for k in FIELDS}
                 and new["parent_change_id"] == ch["proposal_id"], (new, old))


CASES = [case_refuse, case_buy, case_sell, case_entry_max, case_order_scale, case_reserve, case_origin_event,
         case_anti_flip, case_origin_owner, case_supersede, case_expiry, case_card, case_drift, case_quote_source,
         case_verdict_table, case_desk_news, case_owner_quote, case_leg_end, case_requote_copy]

if __name__ == "__main__":
    for fn in CASES:
        try:
            fn()
        except Exception:  # noqa: BLE001 — a crash is one failed case, the others still run
            r.ok(f"{fn.__name__}: runs without an exception", False, traceback.format_exc()[-2500:])
    raise SystemExit(r.done())
