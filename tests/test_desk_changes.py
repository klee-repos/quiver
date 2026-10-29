"""Change proposals (plan §10.3-§10.4; §18.4 ``test_desk_changes.py``; §18.9 G6) and §8.3.

State comes only from the real producers: ``seed_desk`` (the TEST_BOOK book: ANET 25 band 5,
CEG 15 band 5, SGOV 60 cash band 10), ``events.load_company`` (TSM and SPCX companies, SOXX
an ETF, all candidates), ``snapshot`` (``snapshot-commit`` on ``rh_snap`` with values
changed; a second snapshot with a 25% lower equity is the real daily-loss halt),
``findings.record_finding``, ``proposals.build_change`` and the tap
(``approvals.handle_tap``). The card's ``tg_chat_id``/``tg_message_id`` are set with one
UPDATE, as ``outbox.send_card`` does. The follow-up after a stale snapshot is built by
``run_broker.Broker.follow_ups`` (§11.1 step 9).

One seeded ledger is made once; each case starts from a copy of it in its own ``temp_root()``.
"""

from __future__ import annotations

import atexit
import contextlib
import html
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from desk_fakes import (REPO, TEST_BOOK, FakeClock, FakePrices, Results, seams, seed_desk, snapshot,
                        temp_root)

from lib import market
from lib.desk import approvals, events, findings, outbox, proposals, store, valuation
from lib.desk import settings as desk_settings
from lib.desk.proposals import ProposalError

sys.path.insert(0, str(REPO / "deploy" / "runner"))
import run_broker  # noqa: E402

r = Results("desk-changes")

T0 = "2026-08-05T14:00:00Z"          # 10:00 ET, a Wednesday session
OWNER = 515151
CHAT = "515151"
ACC_10Q = "0001596532-26-000175"


def PX():
    return FakePrices({"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0, "TSM": 100.0, "SPCX": 50.0})


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- the template ledger (built once) -----------------------------------------------

_TEMPLATE = {}


def template_db() -> Path:
    if "db" in _TEMPLATE:
        return _TEMPLATE["db"]
    tmp = tempfile.mkdtemp(prefix="t2_changes_tpl_")
    atexit.register(shutil.rmtree, tmp, True)
    out = Path(tmp) / "ledger.db"
    with temp_root() as root:
        clk = FakeClock(T0).pin_market()
        try:
            seed_desk(root)
            led, cfg = root.led(), root.cfg()
            with led.connect() as c:
                sett = desk_settings.effective(c, cfg)
            for t in ("TSM", "SPCX", "SOXX"):
                events.load_company(led, t, settings=sett, seams=seams(), status="candidate")
            src, dst = sqlite3.connect(str(root.db)), sqlite3.connect(str(out))
            src.backup(dst)
            src.close()
            dst.close()
        finally:
            clk.unpin()
    _TEMPLATE["db"] = out
    return out


class FakeTG:
    """The Bot API transport seam of ``outbox.Outbox``: records each call."""

    def __init__(self):
        self.calls = []
        self.mid = 8000

    def __call__(self, method, params):
        self.calls.append((method, json.loads(json.dumps(params))))
        if method == "sendMessage":
            self.mid += 1
            return {"ok": True, "result": {"message_id": self.mid}}
        return {"ok": True, "result": True}


class Ctx:
    def __init__(self, root, clk):
        self.root = root
        self.clk = clk
        self.led = root.led()
        self.cfg = root.cfg()
        self.tg = FakeTG()
        self.box = outbox.Outbox(self.led, transport=self.tg, chat_id=CHAT, sleep=lambda s: None)

    def now(self) -> str:
        return store.iso(self.clk.now)

    def now_dt(self):
        return self.clk.now

    def at(self, when: str) -> None:
        self.clk.set(when)

    def gid(self) -> int:
        return self.one("SELECT id FROM strategy_goal WHERE status='active'")["id"]

    def q(self, sql, args=()):
        with self.led.connect() as c:
            return [dict(x) for x in c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def snap(self, at=None, **kw):
        return snapshot(self.root, at=at or store.iso(self.clk.now - timedelta(minutes=5)), **kw)

    def prop(self, pid):
        with self.led.connect() as c:
            return proposals.get_proposal(c, pid)

    def book(self) -> dict:
        return {x["ticker"]: (x["target_weight"], x["band"], x["status"], x["entry_price_max"])
                for x in self.q("SELECT * FROM target_portfolio WHERE goal_id=? ORDER BY ticker", (self.gid(),))}

    def setting(self, key, value):
        with self.led.connect() as c:
            desk_settings.write(c, key, value, self.now(), None)

    def finding(self, ticker="ANET", at=None) -> int:
        doc = self.one("SELECT id FROM documents WHERE ticker=? AND kind='filing_section' "
                       "AND length(text) > 2000 ORDER BY id LIMIT 1", (ticker,))["id"]
        with self.led.connect() as c:
            text = store.get_document(c, doc)["text"]
            return findings.record_finding(c, ticker=ticker, kind="guidance", claim="the base case input",
                                           quote=findings.normalize(text).strip()[:160], document_id=doc,
                                           created_by="analyst", now=at or self.now())

    def halt(self):
        """The real daily-loss halt: the day's first snapshot, then a 25% lower equity."""
        self.snap(at=store.iso(self.clk.now - timedelta(minutes=20)), equity=2000.0)
        out = self.snap(at=store.iso(self.clk.now - timedelta(minutes=10)), equity=1500.0)
        assert out["halted_now"], out
        return out


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


def propose(c: Ctx, change: dict, rationale="The owner asked for this change."):
    with c.led.transaction() as conn:
        return proposals.build_change(conn, c.cfg, change=change, rationale=rationale, evidence=[], created_by="desk")


def refusal(fn):
    try:
        fn()
    except ProposalError as e:
        return str(e)
    return None


def tap(c: Ctx, pid: int, action="a", at=None):
    p = c.prop(pid)
    if p["tg_message_id"] is None:
        with c.led.connect() as conn:   # what outbox.send_card writes
            conn.execute("UPDATE proposals SET tg_chat_id=?, tg_message_id=? WHERE id=?", (CHAT, 90000 + pid, pid))
        p = c.prop(pid)
    return approvals.handle_tap(c.led, c.cfg, callback_data=f"p:{pid}:{p['nonce']}:{action}", from_id=OWNER,
                                chat_id=CHAT, message_id=p["tg_message_id"], owner_ids=[OWNER],
                                now_dt=at or c.now_dt(), yf_price_fn=PX())


def confirm(c: Ctx, change: dict):
    built = propose(c, change)
    return built["proposal_id"], tap(c, built["proposal_id"])


def log_rows(c: Ctx, pid: int) -> list:
    rows = c.q("SELECT * FROM strategy_change_log WHERE trigger='owner_confirm'")
    return [x for x in rows if x["proof_json"] and json.loads(x["proof_json"]) == {"proposal_id": pid}]


def approved_trades(c: Ctx) -> int:
    return c.one("SELECT COUNT(*) n FROM proposals WHERE kind='trade' AND state='approved'")["n"] + \
        c.one("SELECT COUNT(*) n FROM orders WHERE state='approved'")["n"]


def exec_jobs(c: Ctx, pid=None) -> int:
    if pid is None:
        return c.one("SELECT COUNT(*) n FROM broker_jobs WHERE kind='execute'")["n"]
    return c.one("SELECT COUNT(*) n FROM broker_jobs WHERE kind='execute' AND proposal_id=?", (pid,))["n"]


def add(ticker, w, funding, entry=None, band=5.0, sleeve="Test"):
    return {"kind": "add_company", "ticker": ticker, "sleeve": sleeve, "target_weight": w, "band": band,
            "entry": entry or {"mode": "now"}, "funding": funding}


def weights(*pairs):
    return {"kind": "set_weights", "changes": [{"ticker": t, "target_weight": w} for t, w in pairs]}


def dcf(fid_bear, fid_base, fid_bull):
    def sc(g1, g2, fids):
        d = {"growth_1_5": g1, "growth_6_10": g2}
        if fids is not None:
            d["finding_ids"] = fids
        return d
    return {"method": "dcf_fcf", "scenarios": {"bear": sc(0.10, 0.05, fid_bear), "base": sc(0.18, 0.08, fid_base),
                                               "bull": sc(0.25, 0.10, fid_bull)}}


def thesis(ticker, val, **kw):
    out = {"kind": "set_thesis", "ticker": ticker, "premise_link": "AI networking needs fast switches",
           "summary": f"{ticker} sells what AI clusters need.", "drivers": ["cloud capex"],
           "falsifiers": ["cloud capex falls"], "valuation": val}
    out.update(kw)
    return out


# --- add --------------------------------------------------------------------------------

def case_add():
    with case() as c:
        c.snap()
        sg = lambda w: [{"ticker": "SGOV", "target_weight": w}]  # noqa: E731
        rows = [("not loaded", add("XYZQ", 10, sg(50)), "not loaded"),
                ("active", add("ANET", 10, sg(50)), "already in the book"),
                ("sum 99.98", add("TSM", 10, sg(49.98)), "sum"),
                ("over per_name_max_pct", add("TSM", 31, sg(29)), "per-name limit")]
        for name, ch, why in rows:
            e = refusal(lambda: propose(c, ch))
            r.ok(f"add: refused: {name}", e is not None and why in e, e)
        c.setting("cash_floor_pct", 50.0)
        e = refusal(lambda: propose(c, add("TSM", 15, sg(45))))
        r.ok("add: refused: cash under cash_floor_pct", e is not None and "cash floor" in e, e)
        c.setting("cash_floor_pct", None)
        r.ok("add: a book that sums to 100.009 passes", refusal(lambda: propose(c, add("TSM", 10, sg(50.009)))) is None)
        pid, res = confirm(c, add("TSM", 10, sg(50), entry={"mode": "at_or_below", "price": 140}))
        r.ok("add: the Confirm applies it", res["ok"] and c.prop(pid)["state"] == "applied", res)
        bk = c.book()
        r.eq("add: apply: TSM active at 10 with entry_price_max 140", bk.get("TSM"), (10.0, 5.0, "active", 140.0))
        lv = c.one("SELECT price, armed FROM level_state WHERE ticker='TSM' AND level='entry'")
        r.eq("add: apply: an armed entry level at 140", lv, {"price": 140.0, "armed": 1})
        r.eq("add: apply: the funding moves SGOV to 50", bk["SGOV"][0], 50.0)
        r.eq("add: apply: companies.status = holding",
             c.one("SELECT status FROM companies WHERE ticker='TSM'")["status"], "holding")


# --- drop ---------------------------------------------------------------------------------

def case_drop():
    with case() as c:
        c.snap()
        pid, res = confirm(c, {"kind": "drop_company", "ticker": "ANET", "exit": {"mode": "at_or_above", "price": 180},
                               "redistribute": [{"ticker": "SGOV", "target_weight": 85}]})
        r.ok("drop: the Confirm applies it", res["ok"] and c.prop(pid)["state"] == "applied", res)
        bk = c.book()
        r.eq("drop: target 0, exiting", bk["ANET"][0::2][:2], (0.0, "exiting"))
        r.eq("drop: companies.status = dropped", c.one("SELECT status FROM companies WHERE ticker='ANET'")["status"],
             "dropped")
        r.eq("drop: at_or_above 180 -> an armed trim_above",
             c.one("SELECT price, armed FROM level_state WHERE ticker='ANET' AND level='trim_above'"),
             {"price": 180.0, "armed": 1})
        r.eq("drop: at_or_above builds no trade", res.get("cards"), [])
    with case() as c:
        c.snap()
        pid, res = confirm(c, {"kind": "drop_company", "ticker": "CEG", "exit": {"mode": "now"},
                               "redistribute": [{"ticker": "SGOV", "target_weight": 75}]})
        bk = c.book()
        r.eq("drop: now: CEG target 0, exiting", (bk["CEG"][0], bk["CEG"][2]), (0.0, "exiting"))
        fu = res["cards"][0] if res.get("cards") else None
        legs = c.led.legs(fu) if fu else []
        r.eq("drop: now: the follow-up sells the exact position", [(l["ticker"], l["side"], l["quantity"]) for l in legs],
             [("CEG", "sell", 1.0)])
    with case() as c:
        c.snap(positions={"ANET": 2, "CEG": 1, "SGOV": 11, "SPCX": 2},
               quotes={"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0, "SPCX": 50.0})
        r.ok("drop: SPCX is held and not in the book", "SPCX" not in c.book())
        pid, res = confirm(c, {"kind": "drop_company", "ticker": "SPCX", "exit": {"mode": "now"}})
        r.ok("drop: an unmanaged position can be dropped", res["ok"], res)
        r.eq("drop: unmanaged: a new row with weight 0 and status exiting", (c.book().get("SPCX") or ())[0::2][:2],
             (0.0, "exiting"))
        r.eq("drop: unmanaged: companies.status = dropped",
             c.one("SELECT status FROM companies WHERE ticker='SPCX'")["status"], "dropped")
        fu = res["cards"][0] if res.get("cards") else None
        r.eq("drop: unmanaged: the follow-up sells all of it",
             [(l["ticker"], l["side"], l["quantity"]) for l in (c.led.legs(fu) if fu else [])], [("SPCX", "sell", 2.0)])


# --- weights_risk -----------------------------------------------------------------------

PLAN_RANGES = {   # §10.3 set_risk table: key -> (min, max, step just outside)
    "daily_loss_halt_pct": (5, 50, 0.01), "min_buying_power_buffer": (0, 1000, 0.01),
    "min_trade_notional": (1, 500, 0.01), "requote_tolerance_pct": (0.25, 5, 0.01),
    "cash_floor_pct": (0, 100, 0.01), "per_name_max_pct": (1, 100, 0.01), "margin_of_safety_pct": (0, 50, 0.01),
    "valuation_discount_rate": (0.05, 0.15, 0.001), "valuation_terminal_growth": (0, 0.04, 0.001),
    "valuation_years": (5, 15, 1),
}


def case_weights_risk():
    with case() as c:
        c.snap()
        for name, ch, why in (("sum", weights(("ANET", 26)), "sum"),
                              ("cap", weights(("ANET", 31), ("SGOV", 54)), "per-name limit")):
            e = refusal(lambda: propose(c, ch))
            r.ok(f"weights_risk: set_weights refused: {name}", e is not None and why in e, e)
        c.setting("cash_floor_pct", 58.0)
        e = refusal(lambda: propose(c, weights(("ANET", 30), ("SGOV", 55))))
        r.ok("weights_risk: set_weights refused: under the cash floor", e is not None and "cash floor" in e, e)
        c.setting("cash_floor_pct", None)
        r.ok("weights_risk: set_weights passes when the checks hold",
             refusal(lambda: propose(c, weights(("ANET", 30), ("SGOV", 55)))) is None)
        r.eq("weights_risk: the plan has 10 set_risk keys", len(PLAN_RANGES), 10)
        for key, (lo, hi, step) in PLAN_RANGES.items():
            for v, ok in ((lo, True), (hi, True), (round(lo - step, 6), False), (round(hi + step, 6), False)):
                e = refusal(lambda: propose(c, {"kind": "set_risk", "key": key, "value": v}))
                r.ok(f"weights_risk: set_risk {key}={v} {'passes' if ok else 'fails'}", (e is None) == ok, e)
        r.ok("weights_risk: set_risk cash_floor_pct=null passes",
             refusal(lambda: propose(c, {"kind": "set_risk", "key": "cash_floor_pct", "value": None})) is None)
        e = refusal(lambda: propose(c, {"kind": "set_risk", "key": "max_leverage", "value": 2}))
        r.ok("weights_risk: an unknown key fails", e is not None, e)
        pid, res = confirm(c, {"kind": "set_risk", "key": "margin_of_safety_pct", "value": 20})
        row = c.one("SELECT value_json, proposal_id FROM desk_settings WHERE key='margin_of_safety_pct'")
        r.eq("weights_risk: apply writes desk_settings with proposal_id",
             (json.loads(row["value_json"]), row["proposal_id"]), (20.0, pid))
        pid, res = confirm(c, {"kind": "set_weights", "changes": [{"ticker": "ANET", "target_weight": 30, "band": 7},
                                                                  {"ticker": "SGOV", "target_weight": 55}]})
        bk = c.book()
        r.eq("weights_risk: set_weights apply moves the weights and the band",
             (bk["ANET"][0], bk["ANET"][1], bk["SGOV"][0], bk["SGOV"][1]), (30.0, 7.0, 55.0, 10.0))


# --- premise --------------------------------------------------------------------------

def _text(n):
    base = "The desk owns the growth of AI and of the power it needs. "
    return (base * (n // len(base) + 1))[:n]


def case_premise():
    with case() as c:
        c.snap()
        goal = {"target_return_pct": 22, "horizon_months": 18, "benchmark": "SGOV ~3.6%"}
        for n, ok in ((199, False), (200, True), (6000, True), (6001, False)):
            e = refusal(lambda: propose(c, {"kind": "set_premise", "text": _text(n), "goal": goal}))
            r.ok(f"premise: {n} chars {'pass' if ok else 'fail'}", (e is None) == ok, e)
        before = c.one("SELECT * FROM strategy_goal WHERE status='active'")
        vmax = c.one("SELECT MAX(version) v FROM premise")["v"] or 0
        pid, res = confirm(c, {"kind": "set_premise", "text": _text(6000), "goal": goal})
        after = c.one("SELECT * FROM strategy_goal WHERE status='active'")
        row = c.one("SELECT version, text, proposal_id FROM premise ORDER BY version DESC LIMIT 1")
        r.eq("premise: apply adds a version", (row["version"], row["text"], row["proposal_id"]),
             (vmax + 1, _text(6000), pid))
        r.eq("premise: apply moves the goal target and horizon", (after["target_return_pct"], after["horizon_months"]),
             (22.0, 18))
        r.eq("premise: apply keeps start_equity", after["start_equity"], before["start_equity"])


# --- thesis ---------------------------------------------------------------------------------

def case_thesis():
    with case() as c:
        c.snap()
        f = c.finding()
        e = refusal(lambda: propose(c, thesis("ANET", dcf(None, [f], [f]))))
        r.ok("thesis: a scenario that cites no finding fails", e is not None and "bear" in e, e)
        e = refusal(lambda: propose(c, thesis("ANET", dcf([999999], [f], [f]))))
        r.ok("thesis: a scenario that cites a missing finding fails", e is not None and "999999" in e, e)
        val = dcf([f], [f], [f])
        with c.led.connect() as conn:
            sett = desk_settings.effective(conn, c.cfg)
            fv = valuation.fair_values(conn, "ANET", val, settings=sett, cik=store.get_company(conn, "ANET")["cik"])
        pid, res = confirm(c, thesis("ANET", val))
        th = c.one("SELECT * FROM theses WHERE ticker='ANET' ORDER BY version DESC LIMIT 1")
        r.ok("thesis: applied", res["ok"] and th is not None, res)
        r.eq("thesis: Python sets the fair values", (th["fair_value_low"], th["fair_value_base"], th["fair_value_high"]),
             (fv["fair_value_low"], fv["fair_value_base"], fv["fair_value_high"]))
        r.ok("thesis: buy_below = base x (1 - 15/100)", abs(th["buy_below"] - fv["fair_value_base"] * 0.85) < 1e-6, th)
        r.ok("thesis: trim_above = the bull value", abs(th["trim_above"] - fv["fair_value_high"]) < 1e-6, th)
        lv = {x["level"]: (round(x["price"], 6), x["armed"]) for x in
              c.q("SELECT level, price, armed FROM level_state WHERE ticker='ANET'")}
        r.eq("thesis: both levels upserted and armed", lv,
             {"buy_below": (round(th["buy_below"], 6), 1), "trim_above": (round(th["trim_above"], 6), 1)})
        pid2, res2 = confirm(c, thesis("ANET", val, buy_below=123.45))
        th2 = c.one("SELECT * FROM theses WHERE ticker='ANET' ORDER BY version DESC LIMIT 1")
        r.eq("thesis: an owner buy_below stays", (th2["version"], th2["buy_below"]), (th["version"] + 1, 123.45))
        r.eq("thesis: the owner level is armed",
             c.one("SELECT price, armed FROM level_state WHERE ticker='ANET' AND level='buy_below'"),
             {"price": 123.45, "armed": 1})
        r.eq("thesis: SOXX is an ETF", c.one("SELECT kind FROM companies WHERE ticker='SOXX'")["kind"], "etf")
        confirm(c, thesis("SOXX", {"method": "price_levels", "reason": "an ETF has no fair value"}, buy_below=250.0))
        r.eq("thesis: an ETF price_levels thesis with buy_below arms it",
             [(x["level"], x["price"]) for x in c.q("SELECT level, price FROM level_state WHERE ticker='SOXX'")],
             [("buy_below", 250.0)])
        pid3, res3 = confirm(c, thesis("SOXX", {"method": "price_levels", "reason": "an ETF has no fair value"}))
        th3 = c.one("SELECT * FROM theses WHERE ticker='SOXX' ORDER BY version DESC LIMIT 1")
        r.ok("thesis: an ETF price_levels thesis with both levels NULL applies",
             res3["ok"] and th3["buy_below"] is None and th3["trim_above"] is None and th3["fair_value_base"] is None,
             (res3, th3))
        r.eq("thesis: it leaves no level_state row", c.q("SELECT * FROM level_state WHERE ticker='SOXX'"), [])
    with case() as c:
        c.snap()
        f = c.finding()
        # prompts/analyst.md and the run_valuation tool text: "Each scenario cites finding ids in its
        # `evidence` list". A scenario written that way cites an existing finding.
        val = {"method": "dcf_fcf", "scenarios": {
            "bear": {"growth_1_5": 0.10, "growth_6_10": 0.05, "evidence": [f]},
            "base": {"growth_1_5": 0.18, "growth_6_10": 0.08, "evidence": [f]},
            "bull": {"growth_1_5": 0.25, "growth_6_10": 0.10, "evidence": [f]}}}
        e = refusal(lambda: propose(c, thesis("ANET", val)))
        r.ok("thesis: a scenario that cites an existing finding in its evidence list passes", e is None, e)
        confirm(c, {"kind": "set_risk", "key": "margin_of_safety_pct", "value": 20})
        val = dcf([f], [f], [f])
        pid, res = confirm(c, thesis("ANET", val))
        th = c.one("SELECT * FROM theses WHERE ticker='ANET' ORDER BY version DESC LIMIT 1")
        r.ok("thesis: buy_below follows margin_of_safety_pct 20 (base x 0.80)",
             th and abs(th["buy_below"] - th["fair_value_base"] * 0.80) < 1e-6, th)


# --- resume -------------------------------------------------------------------------------

def case_resume():
    with case() as c:
        c.snap()
        e = refusal(lambda: propose(c, {"kind": "resume_trading"}))
        r.ok("resume: not halted -> refused", e is not None and "not halted" in e, e)
        c.halt()
        c.root.kill.write_text("stop")
        reason0 = c.one("SELECT halt_reason FROM day_baseline WHERE trade_date=?", (market.trading_day_et(),))["halt_reason"]
        # The card names the real reason: an audit halt sets the same flag as a loss halt.
        card = propose(c, {"kind": "resume_trading"})
        r.eq("resume: the change carries the halt reason", card["change"]["halt_reason"], reason0)
        c.tg.calls.clear()
        outbox.send_change_card(c.led, c.box, c.cfg, card["proposal_id"])
        text = html.unescape(" ".join(p["text"] for m, p in c.tg.calls if m == "sendMessage"))
        r.ok("resume: the card shows the halt reason", f"Trading is stopped for today (the reason: {reason0})." in text,
             text)
        r.ok("resume: the card does not claim a loss of its own", "loss reached" not in text, text)
        pid, res = confirm(c, {"kind": "resume_trading"})
        row = c.one("SELECT halted, halt_reason FROM day_baseline WHERE trade_date=?", (market.trading_day_et(),))
        r.ok("resume: applied", res["ok"] and c.prop(pid)["state"] == "applied", res)
        r.eq("resume: halted = 0", row["halted"], 0)
        r.ok("resume: 'resumed by owner, proposal N' appended",
             row["halt_reason"].startswith(reason0) and row["halt_reason"].endswith(f"resumed by owner, proposal {pid}"),
             row["halt_reason"])
        r.ok("resume: KILL untouched", c.root.kill.exists() and c.root.kill.read_text() == "stop")


# --- revalidate -------------------------------------------------------------------------------

def case_revalidate():
    with case() as c:
        c.snap()
        sg = [{"ticker": "SGOV", "target_weight": 50}]
        a = propose(c, add("TSM", 10, sg, entry={"mode": "at_or_below", "price": 90}))
        b = propose(c, add("SPCX", 10, sg, entry={"mode": "at_or_below", "price": 40}))
        r.ok("revalidate: both add_company proposals pass at propose time", a["proposal_id"] and b["proposal_id"])
        tap(c, a["proposal_id"])
        mid_book = c.book()
        logs0 = len(c.q("SELECT id FROM strategy_change_log"))
        res = tap(c, b["proposal_id"])
        p = c.prop(b["proposal_id"])
        r.eq("revalidate: the second Confirm fails at apply -> failed", (res["ok"], p["state"]), (False, "failed"))
        r.ok("revalidate: the failure says why", "sum" in (res.get("answer") or "") and "error" in json.loads(p["result_json"]),
             (res, p["result_json"]))
        r.eq("revalidate: the book is unchanged", c.book(), mid_book)
        r.eq("revalidate: SPCX stays a candidate", c.one("SELECT status FROM companies WHERE ticker='SPCX'")["status"],
             "candidate")
        r.eq("revalidate: no change-log row", len(c.q("SELECT id FROM strategy_change_log")), logs0)


# --- log ------------------------------------------------------------------------------------

def case_log():
    with case() as c:
        c.snap()
        f = c.finding()
        kinds = [
            ("set_risk", {"kind": "set_risk", "key": "margin_of_safety_pct", "value": 20}),
            ("set_premise", {"kind": "set_premise", "text": _text(400),
                             "goal": {"target_return_pct": 20, "horizon_months": 12, "benchmark": "SGOV"}}),
            ("set_weights", weights(("ANET", 30), ("SGOV", 55))),
            ("add_company", add("TSM", 5, [{"ticker": "SGOV", "target_weight": 50}],
                                entry={"mode": "at_or_below", "price": 90})),
            ("drop_company", {"kind": "drop_company", "ticker": "CEG", "exit": {"mode": "at_or_above", "price": 400},
                              "redistribute": [{"ticker": "SGOV", "target_weight": 65}]}),
            ("set_thesis", thesis("ANET", dcf([f], [f], [f]))),
        ]
        for kind, ch in kinds:
            pid, res = confirm(c, ch)
            rows = log_rows(c, pid)
            r.ok(f"log: {kind} writes strategy_change_log (owner_confirm, proposal_id)", res["ok"] and rows, (res, rows))
        c.halt()
        pid, res = confirm(c, {"kind": "resume_trading"})
        r.ok("log: resume_trading writes strategy_change_log (owner_confirm, proposal_id)",
             res["ok"] and log_rows(c, pid), res)


# --- follow_up --------------------------------------------------------------------------------

def _fu_checks(c, name, ch_pid, res):
    fu = res["cards"][0] if res.get("cards") else None
    p = c.prop(fu) if fu else None
    r.ok(f"follow_up: {name}: one follow-up trade", p is not None, res)
    if p is None:
        return None
    r.eq(f"follow_up: {name}: origin change, parent_change_id, pending",
         (p["kind"], p["origin"], p["parent_change_id"], p["state"]), ("trade", "change", ch_pid, "pending"))
    r.eq(f"follow_up: {name}: no execute job until Approve", exec_jobs(c, fu), 0)
    r.eq(f"follow_up: {name}: a Confirm never sets any trade approved", approved_trades(c), 0)
    return fu


def case_follow_up():
    with case() as c:                                   # open session
        c.snap()
        ch, res = confirm(c, weights(("ANET", 30), ("SGOV", 55)))
        fu = _fu_checks(c, "open", ch, res)
        if fu:
            legs = c.led.legs(fu)
            # money = min(300 - 5, 300 + 1,100 - 55% x 2,000) = $295 < the $300 gap.
            r.eq("follow_up: open: the exact order", [(l["ticker"], l["side"], l["dollar_amount"]) for l in legs],
                 [("ANET", "buy", 295.0)])
            sent = outbox.send_trade_card(c.led, c.box, c.led.connect,
                                          {"proposal_id": fu, "skipped": [], "scaled": False, "superseded": [],
                                           "legs": [{"ticker": "ANET", "side": "buy", "dollars": 295.0, "quote": 150.0,
                                                     "weight_now": 15.0, "weight_after": 29.75,
                                                     "target_weight": 30.0}]})
            msgs = [p for m, p in c.tg.calls if m == "sendMessage"]
            kb = msgs[-1].get("reply_markup", {}).get("inline_keyboard") if msgs else None
            r.eq("follow_up: open: its own Approve/Reject card", kb, outbox.trade_buttons(fu, c.prop(fu)["nonce"]))
            r.ok("follow_up: open: the card states the exact order", "Buy $295 of ANET" in msgs[-1]["text"], msgs[-1:])
            r.ok("follow_up: open: card sent", sent["ok"])
            t = tap(c, fu)
            r.ok("follow_up: open: Approve queues the execute job", t["ok"] and exec_jobs(c, fu) == 1, t)
    with case(at="2026-08-05T21:00:00Z") as c:         # after the close
        c.snap()
        ch, res = confirm(c, weights(("ANET", 30), ("SGOV", 55)))
        fu = _fu_checks(c, "closed", ch, res)
        if fu:
            r.eq("follow_up: closed: it stays open to the next close", c.prop(fu)["expires_at"], "2026-08-06T20:00:00Z")
            t = tap(c, fu)
            r.ok("follow_up: closed: Approve is refused, the card stays pending",
                 not t["ok"] and "closed" in t["answer"] and c.prop(fu)["state"] == "pending" and exec_jobs(c) == 0, t)
    with case() as c:                                   # halted (G6)
        c.halt()
        ch, res = confirm(c, weights(("ANET", 30), ("SGOV", 55)))
        fu = _fu_checks(c, "halted", ch, res)
        if fu:
            t = tap(c, fu)
            r.ok("follow_up: halted: Approve is refused (G6)",
                 not t["ok"] and "halted" in t["answer"] and c.prop(fu)["state"] == "pending" and exec_jobs(c) == 0, t)
    with case() as c:                                   # KILL (G6)
        c.snap()
        c.root.kill.write_text("stop")
        ch, res = confirm(c, weights(("ANET", 30), ("SGOV", 55)))
        fu = _fu_checks(c, "KILL", ch, res)
        if fu:
            t = tap(c, fu)
            r.ok("follow_up: KILL: Approve is refused (G6)",
                 not t["ok"] and "stopped" in t["answer"] and c.prop(fu)["state"] == "pending" and exec_jobs(c) == 0, t)
    with case() as c:                                   # set_thesis at quote <= buy_below, under target
        c.snap()
        f = c.finding()
        ch, res = confirm(c, thesis("ANET", dcf([f], [f], [f]), buy_below=150.0))
        fu = _fu_checks(c, "set_thesis", ch, res)
        if fu:
            r.eq("follow_up: set_thesis: quote $150.00 = buy_below -> a pending buy card",
                 [(l["ticker"], l["side"]) for l in c.led.legs(fu)], [("ANET", "buy")])
    with case() as c:                                   # set_thesis at quote <= buy_below, above target
        c.snap(positions={"ANET": 5, "CEG": 1, "SGOV": 11})
        f = c.finding()
        ch, res = confirm(c, thesis("ANET", dcf([f], [f], [f]), buy_below=160.0))
        r.ok("follow_up: set_thesis above target -> no trade card", res["ok"] and res.get("cards") == [], res)
    with case() as c:                                   # any other refusal: one message with the reason
        c.snap()
        built = propose(c, add("TSM", 10, [{"ticker": "SGOV", "target_weight": 50}]))
        pid = built["proposal_id"]
        with c.led.connect() as conn:
            conn.execute("UPDATE proposals SET tg_chat_id=?, tg_message_id=? WHERE id=?", (CHAT, 90000 + pid, pid))
        no_tsm = FakePrices({"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0})
        res = approvals.handle_tap(c.led, c.cfg, callback_data=f"p:{pid}:{c.prop(pid)['nonce']}:a", from_id=OWNER,
                                   chat_id=CHAT, message_id=90000 + pid, owner_ids=[OWNER], now_dt=c.now_dt(),
                                   yf_price_fn=no_tsm)
        r.ok("follow_up: another refusal is sent in one message with the reason",
             res["ok"] and res.get("cards") == [] and len(res["messages"]) == 1 and "TSM" in res["messages"][0], res)
    with case() as c:                                   # a 27-hour-old snapshot
        c.snap(at="2026-08-04T11:00:00Z")
        ch, res = confirm(c, weights(("ANET", 30), ("SGOV", 55)))
        r.eq("follow_up: stale: no card", res.get("cards"), [])
        r.eq("follow_up: stale: no trade proposal", c.one("SELECT COUNT(*) n FROM proposals WHERE kind='trade'")["n"], 0)
        r.eq("follow_up: stale: one snapshot job",
             c.one("SELECT COUNT(*) n FROM broker_jobs WHERE kind='snapshot'")["n"], 1)
        r.eq("follow_up: stale: the follow_up: waiting mark", json.loads(c.prop(ch)["result_json"] or "{}"),
             {"follow_up": "waiting"})
        r.ok("follow_up: stale: the broker is touched", "broker" in res.get("touch", []), res)
        c.at("2026-08-05T14:06:00Z")
        c.snap(at="2026-08-05T14:05:00Z")
        n_sent = len([1 for m, _ in c.tg.calls if m == "sendMessage"])
        broker = run_broker.Broker(led=c.led, cfg=c.cfg, box=c.box, now_fn=c.now_dt)
        broker.follow_ups()
        trades = c.q("SELECT * FROM proposals WHERE kind='trade'")
        r.eq("follow_up: after snapshot-commit: one pending follow-up",
             [(x["origin"], x["parent_change_id"], x["state"]) for x in trades], [("change", ch, "pending")])
        sent = [p for m, p in c.tg.calls if m == "sendMessage"][n_sent:]
        r.ok("follow_up: after snapshot-commit: its card is sent with Approve/Reject",
             sent and trades and sent[-1].get("reply_markup", {}).get("inline_keyboard")
             == outbox.trade_buttons(trades[0]["id"], trades[0]["nonce"]), sent)
        r.ok("follow_up: after snapshot-commit: the mark is cleared",
             "follow_up" not in json.loads(c.prop(ch)["result_json"] or "{}"), c.prop(ch)["result_json"])
        broker.follow_ups()
        r.eq("follow_up: a second pass builds nothing more",
             c.one("SELECT COUNT(*) n FROM proposals WHERE kind='trade'")["n"], 1)


# --- supersede_on_change ------------------------------------------------------------------

def case_supersede_on_change():
    with case() as c:
        c.snap(positions={"ANET": 2, "SGOV": 11})           # CEG not held: under its target
        with c.led.transaction() as conn:
            mid = 4001
            store.insert_owner_message(conn, chat_id=CHAT, tg_message_id=mid, text="buy CEG", created_at=c.now())
            old = proposals.build_trade(c.led, conn, cfg=c.cfg, tickers=["CEG"], origin={"owner_message_id": mid},
                                        fraction=1.0, rationale="You asked.", evidence=[], created_by="desk",
                                        role="desk", turn_owner_ids=[mid], yf_price_fn=PX())
        outbox.send_trade_card(c.led, c.box, c.led.connect, old)
        ch, res = confirm(c, weights(("CEG", 20), ("SGOV", 55)))
        po = c.prop(old["proposal_id"])
        fu = res["cards"][0] if res.get("cards") else None
        r.ok("supersede_on_change: set_weights on CEG supersedes the pending CEG trade",
             po["state"] == "superseded" and old["proposal_id"] in res.get("superseded", []), (po, res))
        r.eq("supersede_on_change: its legs end skipped",
             [(l["state"], l["finalized"]) for l in c.led.legs(old["proposal_id"])], [("skipped", 1)])
        t = tap(c, old["proposal_id"])
        r.ok("supersede_on_change: the old card says Replaced by #N",
             t["answer"] in (f"Replaced by #{ch}", f"Replaced by #{fu}"), t)
        r.ok("supersede_on_change: a follow-up CEG trade is pending", fu and c.prop(fu)["state"] == "pending", res)
        if not fu:
            return
        tap(c, fu)                                      # Approve: the legs become approved
        ref = c.led.legs(fu)[0]["ref_id"]

        def new_ceg():
            with c.led.transaction() as conn:
                return proposals.build_trade(c.led, conn, cfg=c.cfg, tickers=["CEG"], origin={},
                                             python_origin="drift", rationale="drift", evidence=[],
                                             created_by="monitor", yf_price_fn=PX())
        for st in ("approved", "checked", "placing", "placed", "partially_filled"):
            c.led.set_order_state(ref, st)
            e = refusal(new_ceg)
            r.eq(f"supersede_on_change: a leg in {st} refuses a new build_trade", e,
                 "CEG has an order in flight. Wait for the fill.")
        for st in ("requoted", "skipped"):
            c.led.set_order_state(ref, st)
            e = refusal(new_ceg)
            r.ok(f"supersede_on_change: a {st} leg does not block", e is None, e)


# --- preview --------------------------------------------------------------------------------

def case_preview():
    with case() as c:
        c.snap()
        built = propose(c, add("TSM", 10, [{"ticker": "SGOV", "target_weight": 50}]))
        pid = built["proposal_id"]
        sent = outbox.send_change_card(c.led, c.box, c.cfg, pid)
        msgs = [p for m, p in c.tg.calls if m == "sendMessage"]
        text = html.unescape(re.sub(r"<[^>]+>", "", " ".join(p["text"] for p in msgs)))
        r.ok("preview: the card says the follow-up trade",
             "then i'll propose buying about $200 of tsm from cash" in text.lower(), text)
        kb = msgs[-1].get("reply_markup", {}).get("inline_keyboard")
        r.eq("preview: Confirm and Reject on the last message", kb, outbox.change_buttons(pid, c.prop(pid)["nonce"]))
        r.eq("preview: Confirm is success, Reject is danger", [(b["text"], b["style"]) for b in kb[0]],
             [("Confirm", "success"), ("Reject", "danger")])
        r.eq("preview: tg_message_id is the card's last message", c.prop(pid)["tg_message_id"], sent["message_id"])
        r.eq("preview: a change proposal is open until the close", c.prop(pid)["expires_at"], "2026-08-05T20:00:00Z")
        res = approvals.handle_tap(c.led, c.cfg, callback_data=f"p:{pid}:{c.prop(pid)['nonce']}:a", from_id=OWNER,
                                   chat_id=CHAT, message_id=sent["message_id"], owner_ids=[OWNER], now_dt=c.now_dt(),
                                   yf_price_fn=PX())
        fu = res["cards"][0] if res.get("cards") else None
        r.eq("preview: the follow-up buys about $200 of TSM, no SGOV leg",
             [(l["ticker"], l["side"], l["dollar_amount"]) for l in (c.led.legs(fu) if fu else [])],
             [("TSM", "buy", 200.0)])
        ch2, res2 = confirm(c, weights(("ANET", 30), ("SGOV", 45)))
        fu2 = res2["cards"][0] if res2.get("cards") else None
        r.ok("preview: a set_weights that moves SGOV builds no SGOV leg",
             fu2 and all(l["ticker"] != "SGOV" for l in c.led.legs(fu2)), res2)
        with c.led.transaction() as conn:
            mid = 4100
            store.insert_owner_message(conn, chat_id=CHAT, tg_message_id=mid, text="buy SGOV and ANET", created_at=c.now())
        e = None
        try:
            with c.led.transaction() as conn:
                b = proposals.build_trade(c.led, conn, cfg=c.cfg, tickers=["SGOV", "ANET"],
                                          origin={"owner_message_id": mid}, fraction=1.0, rationale="x", evidence=[],
                                          created_by="desk", role="desk", turn_owner_ids=[mid], yf_price_fn=PX())
            r.ok("preview: an SGOV ticker gets no leg", all(l["ticker"] != "SGOV" for l in b["legs"])
                 and any(s["ticker"] == "SGOV" for s in b["skipped"]), b)
        except ProposalError as err:
            e = str(err)
            r.ok("preview: an SGOV ticker gets no leg (refused, nothing else to do)", "SGOV" in e, e)
        r.eq("preview: no SGOV order row ever", c.one("SELECT COUNT(*) n FROM orders WHERE ticker='SGOV'")["n"], 0)
    with case() as c:
        # §10.4: set_thesis implies a trade only when the quote is at or under the new buy_below.
        c.snap()
        f = c.finding()
        built = propose(c, thesis("ANET", dcf([f], [f], [f])))
        norm = built["change"]
        r.ok("preview: the computed buy_below is under the $150 quote", norm["buy_below"] < 150.0, norm["buy_below"])
        with c.led.connect() as conn:
            pv = proposals.change_preview(conn, c.cfg, norm, now_dt=c.now_dt(), yf_price_fn=PX())
        res = tap(c, built["proposal_id"])
        r.eq("preview: a set_thesis with the quote above buy_below builds no trade", res.get("cards"), [])
        r.ok("preview: so its card promises no trade", "propose" not in pv.lower(), pv)
        built = propose(c, thesis("ANET", dcf([f], [f], [f]), buy_below=155.0))
        with c.led.connect() as conn:
            pv = proposals.change_preview(conn, c.cfg, built["change"], now_dt=c.now_dt(), yf_price_fn=PX())
        r.ok("preview: a set_thesis with the quote under buy_below says the buy",
             "then i'll propose buying about $200 of anet from cash" in pv.lower(), pv)


# --- one open card per subject -------------------------------------------------------------

def case_one_open_card():
    with case() as c:
        c.snap()
        val = {"method": "price_levels", "reason": "test levels"}
        a = propose(c, thesis("ANET", val, buy_below=120.0))
        other = propose(c, thesis("CEG", val, buy_below=250.0))
        outbox.send_change_card(c.led, c.box, c.cfg, a["proposal_id"])
        mid_a = c.prop(a["proposal_id"])["tg_message_id"]
        b = propose(c, thesis("ANET", val, buy_below=125.0))
        r.eq("one_open: a new ANET thesis supersedes the older pending one", b["superseded"], [a["proposal_id"]])
        pa = c.prop(a["proposal_id"])
        r.eq("one_open: the old card is superseded by the new one", (pa["state"], pa["superseded_by"]),
             ("superseded", b["proposal_id"]))
        r.eq("one_open: the CEG thesis stays pending", c.prop(other["proposal_id"])["state"], "pending")
        c.tg.calls.clear()
        outbox.send_change_card(c.led, c.box, c.cfg, b["proposal_id"], superseded=b["superseded"])
        labels = [p["reply_markup"]["inline_keyboard"][0][0]["text"] for m, p in c.tg.calls
                  if m == "editMessageReplyMarkup" and p.get("message_id") == mid_a]
        r.eq("one_open: the old card's buttons say Replaced by #N", labels, [f"Replaced by #{b['proposal_id']}"])
        t = tap(c, a["proposal_id"])
        r.eq("one_open: a tap on the old card applies nothing", (t["ok"], t["answer"]),
             (False, f"Replaced by #{b['proposal_id']}"))
        r.eq("one_open: no thesis from the old card", c.q("SELECT * FROM theses WHERE ticker='ANET'"), [])
        res = tap(c, b["proposal_id"])
        th = c.one("SELECT buy_below FROM theses WHERE ticker='ANET' ORDER BY version DESC LIMIT 1")
        r.ok("one_open: the new card applies", res["ok"] and th and th["buy_below"] == 125.0, (res, th))
        # A different kind on the same ticker is a separate card.
        d = propose(c, {"kind": "drop_company", "ticker": "CEG", "exit": {"mode": "now"},
                        "redistribute": [{"ticker": "SGOV", "target_weight": 75}]})
        r.eq("one_open: a drop_company leaves the CEG thesis open", (d["superseded"],
             c.prop(other["proposal_id"])["state"]), ([], "pending"))
        # set_risk: the same key supersedes; another key does not.
        k1 = propose(c, {"kind": "set_risk", "key": "min_trade_notional", "value": 10})
        k2 = propose(c, {"kind": "set_risk", "key": "margin_of_safety_pct", "value": 20})
        k3 = propose(c, {"kind": "set_risk", "key": "min_trade_notional", "value": 12})
        r.eq("one_open: set_risk on the same key supersedes", (k2["superseded"], k3["superseded"]),
             ([], [k1["proposal_id"]]))
        # set_weights: a shared ticker supersedes; no shared ticker does not.
        w1 = propose(c, weights(("ANET", 30), ("SGOV", 55)))
        w2 = propose(c, weights(("CEG", 10), ("SGOV", 65)))
        r.eq("one_open: set_weights that share only the cash fund (SGOV) stay apart", w2["superseded"], [])
        w3 = propose(c, weights(("ANET", 28), ("SGOV", 57)))
        r.eq("one_open: set_weights with a shared name (ANET) supersedes", w3["superseded"], [w1["proposal_id"]])
        r.eq("one_open: the CEG weights card stays open", c.prop(w2["proposal_id"])["state"], "pending")
        # set_premise has one subject.
        g = {"target_return_pct": 20, "horizon_months": 12, "benchmark": "SPY"}
        p1 = propose(c, {"kind": "set_premise", "text": "AI and energy need each other. " * 10, "goal": g})
        p2 = propose(c, {"kind": "set_premise", "text": "AI, energy and the grid need each other. " * 8, "goal": g})
        r.eq("one_open: a new premise supersedes the old one", p2["superseded"], [p1["proposal_id"]])
    with case() as c:
        # A ledger from before the rule: two pending ANET theses (the old producer, which
        # superseded nothing). The monitor's sweep keeps the newest.
        c.snap()
        val = {"method": "price_levels", "reason": "test levels"}
        keep = proposals.supersede_changes
        proposals.supersede_changes = lambda *a, **k: []
        try:
            a = propose(c, thesis("ANET", val, buy_below=120.0))
            b = propose(c, thesis("ANET", val, buy_below=125.0))
        finally:
            proposals.supersede_changes = keep
        r.eq("one_open: setup has two pending ANET cards",
             [c.prop(x["proposal_id"])["state"] for x in (a, b)], ["pending", "pending"])
        with c.led.connect() as conn:
            conn.execute("UPDATE proposals SET tg_message_id=? WHERE id=?", (777, a["proposal_id"]))
        with c.led.transaction() as conn:
            moved = proposals.supersede_duplicate_changes(conn, c.now())
        r.eq("one_open: the sweep supersedes the older card", moved,
             [{"id": a["proposal_id"], "tg_message_id": 777, "superseded_by": b["proposal_id"]}])
        r.eq("one_open: the newer card stays pending", c.prop(b["proposal_id"])["state"], "pending")
        with c.led.transaction() as conn:
            r.eq("one_open: a second sweep moves nothing", proposals.supersede_duplicate_changes(conn, c.now()), [])


# --- plain-English card text ------------------------------------------------------------------

def case_plain_card():
    with case() as c:
        c.snap()
        f = c.finding()
        built = propose(c, thesis("ANET", dcf([f], [f], [f])))
        outbox.send_change_card(c.led, c.box, c.cfg, built["proposal_id"])
        text = html.unescape(re.sub(r"<[^>]+>", "", " ".join(p["text"] for m, p in c.tg.calls
                                                            if m == "sendMessage")))
        for phrase in ("Our reason to own ANET (the thesis):", "I think one share is worth about",
                       "if things go badly", "if things go very well", "a good price to buy more",
                       "a good price to sell some", "We should rethink it if"):
            r.ok(f"plain_card: says '{phrase}'", phrase in text, text)
        for word in ("bear", "bull", "fair value", "the book", "trim above", "base case"):
            r.ok(f"plain_card: no '{word}'", word not in text.lower(), text)
        # A company valued by price levels is not called a fund, and the card does not contradict itself.
        c.tg.calls.clear()
        built = propose(c, thesis("CEG", {"method": "price_levels", "reason": "no clean cash flow"}))
        outbox.send_change_card(c.led, c.box, c.cfg, built["proposal_id"])
        text = " ".join(p["text"] for m, p in c.tg.calls if m == "sendMessage")
        r.ok("plain_card: a price_levels thesis says no single worth, then no alert prices",
             "I give it no single worth per share. It sets no alert prices." in text, text)
        r.ok("plain_card: a company is not called a fund", "fund" not in text.lower(), text)
        c.tg.calls.clear()
        built = propose(c, add("TSM", 10, [{"ticker": "SGOV", "target_weight": 50}]))
        outbox.send_change_card(c.led, c.box, c.cfg, built["proposal_id"])
        text = " ".join(p["text"] for m, p in c.tg.calls if m == "sendMessage")
        r.ok("plain_card: add_company says the planned share and the drift",
             "Add TSM to the plan as 10.0% of the account. I suggest a trade when it drifts more than 5.0 points"
             in text, text)


CASES = [case_add, case_drop, case_weights_risk, case_premise, case_thesis, case_resume, case_revalidate, case_log,
         case_follow_up, case_supersede_on_change, case_preview, case_one_open_card, case_plain_card]

if __name__ == "__main__":
    for fn in CASES:
        try:
            fn()
        except Exception:  # noqa: BLE001 — a crash is one failed case, the others still run
            r.ok(f"{fn.__name__}: runs without an exception", False, traceback.format_exc()[-2500:])
    raise SystemExit(r.done())
