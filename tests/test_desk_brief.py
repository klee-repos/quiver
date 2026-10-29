"""The daily close brief (plan §12; test table ``test_desk_brief.py``).

``brief.build_model`` makes the model from ledger rows and the injected seams;
``notify.render_brief`` turns it into two to four prose messages. The dedup case drives the
real monitor step (``run_monitor.Monitor.close_brief``) with a recording fake transport.
The ledger is a copy of one seeded template that the real ``desk-seed`` made. Each case uses
its own temp root. No network, no LLM, no broker.

Run: .venv/bin/python tests/test_desk_brief.py
"""

from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import (REPO, FakeClock, FakePrices, Results, rh_snap_file, seed_desk,  # noqa: E402
                        temp_root, TEST_BOOK)

from lib import goal as goal_mod  # noqa: E402
from lib import market, notify  # noqa: E402
from lib.desk import brief, execution, outbox, proposals, store  # noqa: E402

r = Results("desk-brief")

CHAT = 555001
SEED_AT = "2026-08-03T14:00:00Z"
DAY = "2026-08-05"

# The close snapshot of DAY: quantity, last price, previous close (VALUES set on rh_snap).
CLOSE_BOOK = {"ANET": (2, 150.0, 140.0), "CEG": (1, 300.0, 310.0), "SGOV": (11, 100.0, 99.99),
              "DLR": (1, 200.0, 170.0)}
EARNINGS = {"ANET": "2026-08-11", "CEG": "2026-08-13"}


class FakeTG:
    """A recording transport; the answers use the Bot API's documented minimal shape."""

    def __init__(self, start: int = 700):
        self.calls = []
        self.next_id = start

    def __call__(self, method, params):
        self.calls.append((method, json.loads(json.dumps(params))))
        self.next_id += 1
        return {"ok": True, "result": {"message_id": self.next_id}}


_TEMPLATE = {}


@contextmanager
def fresh_root():
    if not _TEMPLATE:
        with temp_root() as root:
            fc = FakeClock(SEED_AT).pin_market()
            try:
                seed_desk(root)
            finally:
                fc.unpin()
            _TEMPLATE["db"] = root.db.read_bytes()
    with temp_root() as root:
        (root.path / "strategy.yaml").write_text(TEST_BOOK, encoding="utf-8")
        root.db.write_bytes(_TEMPLATE["db"])
        yield root


def snap(root, fc, at, *, equity, book=None):
    """The real ``snapshot-commit`` on ``rh_snap`` with values set (prices, quantities, the
    previous close). The pinned clock gives the baseline day."""
    book = book or {"ANET": (2, 150.0, 150.0), "CEG": (1, 300.0, 300.0), "SGOV": (11, 100.0, 100.0)}
    fc.set(at)
    data = rh_snap_file(positions={t: q for t, (q, _p, _c) in book.items()},
                        quotes={t: p for t, (_q, p, _c) in book.items()}, buying_power=300.0,
                        equity=equity, at=at)
    for res in data["get_equity_quotes"]["data"]["results"]:
        sym = res["quote"]["symbol"]
        if sym in book:
            res["quote"]["adjusted_previous_close"] = f"{book[sym][2]:.6f}"
            res["quote"]["previous_close"] = f"{book[sym][2]:.6f}"
    return execution.snapshot_commit(root.led(), root.cfg(), data, now_dt=store.parse_ts(at))


def full_day(root, fc, *, deposit=True, events=True, pending=True):
    """DAY-1 baseline $2,000; a $500 deposit on DAY; DAY baseline $2,500; the close at $2,550."""
    led = root.led()
    snap(root, fc, "2026-08-04T13:35:00Z", equity=2000.0)
    if deposit:
        led.record_cash_flow(trade_date=DAY, amount=500.0, note="deposit", created_at="2026-08-05T12:00:00Z")
    snap(root, fc, "2026-08-05T13:35:00Z", equity=2500.0)
    snap(root, fc, "2026-08-05T20:30:00Z", equity=2550.0, book=CLOSE_BOOK)
    ids = {}
    if events:
        with led.connect() as conn:
            ids["filing"] = store.create_event(conn, dedup_key="filing:0001596532-26-000175", ticker="ANET",
                                               type="filing", detail={"form": "10-Q"}, now="2026-08-05T15:00:00Z")
            ids["level"] = store.create_event(conn, dedup_key="level:CEG:buy_below:2026-08-05", ticker="CEG",
                                              type="price_level", detail={"level": "buy_below", "price": 290.5},
                                              now="2026-08-05T18:00:00Z")
            # The ET date decides: 03:30Z on 08-05 is 23:30 ET on 08-04 (out); 01:00Z on 08-06 is 21:00 ET (in).
            store.create_event(conn, dedup_key="news:CEG:old", ticker="CEG", type="news", detail={},
                               now="2026-08-05T03:30:00Z")
            ids["late"] = store.create_event(conn, dedup_key="news:ANET:late", ticker="ANET", type="news",
                                             detail={}, now="2026-08-06T01:00:00Z")
            # The monitor writes the watched-name filings under this key (run_monitor.edgar_sweep).
            store.set_state(conn, f"watch_filings:{DAY}", json.dumps(["TSM 6-K"]))
    if pending:
        fc.set("2026-08-05T20:35:00Z")
        cfg = root.cfg()
        with led.transaction() as conn:
            ch = proposals.build_change(conn, cfg, change={"kind": "set_risk", "key": "min_trade_notional",
                                                           "value": 10},
                                        rationale="Raise the minimum trade.", created_by="desk", now_dt=fc.now)
            ids["change"] = ch["proposal_id"]
            # A withdrawn proposal is not waiting on the owner.
            # Another key: a second min_trade_notional card would supersede the first.
            gone = proposals.build_change(conn, cfg, change={"kind": "set_risk", "key": "margin_of_safety_pct",
                                                             "value": 20},
                                          rationale="Another try.", created_by="desk", now_dt=fc.now)
            proposals.withdraw(conn, gone["proposal_id"], created_by="desk", now=store.iso(fc.now))
    return ids


def goal_progress(root):
    led = root.led()
    g = led.get_active_goal()
    gp = goal_mod.compute_from_ledger(led, g)
    # The production caller (run_monitor.close_brief) adds the goal's target.
    return ({**gp, "target_return_pct": g.get("target_return_pct")} if gp else None), g


def model_for(root, fc, *, day=DAY, now="2026-08-05T20:40:00Z", earnings=EARNINGS, gp="auto"):
    fc.set(now)
    if gp == "auto":
        gp, _g = goal_progress(root)
    with root.led().connect() as conn:
        return brief.build_model(conn, day=day, now_dt=store.parse_ts(now), goal_progress=gp,
                                 earnings_fn=(lambda t: earnings.get(t)) if earnings is not None else None)


def fmt_pct(v):
    return f"{v:+.2f}%"


def proposals_expiry(root, pid):
    with root.led().connect() as conn:
        return proposals.get_proposal(conn, pid)["expires_at"]


# --- brief.full ---------------------------------------------------------------------

def case_full():
    with fresh_root() as root:
        fc = FakeClock(SEED_AT).pin_market()
        try:
            ids = full_day(root, fc)
            gp, g = goal_progress(root)
            model = model_for(root, fc)
            msgs = notify.render_brief(model)
            r.eq("full: 4 messages", len(msgs), 4)
            # 1. The day: equity and the change vs the day's baseline, from the ledger rows.
            with root.led().connect() as conn:
                eq = float(conn.execute("SELECT equity FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()[0])
                base = float(conn.execute("SELECT baseline_equity FROM day_baseline WHERE trade_date=?",
                                          (DAY,)).fetchone()[0])
            chg = eq - base
            r.eq("full: the model's equity and baseline are the ledger's", (model["equity"], model["baseline"]),
                 (eq, base))
            want1 = (f"The account closed at ${eq:,.2f}, up ${abs(chg):,.2f} ({fmt_pct(chg / base * 100)}) on the day.")
            r.ok("full: message 1 names equity and the change vs the baseline", msgs and msgs[0].startswith(want1),
                 msgs[:1])
            r.eq("full: the change is $50.00, +2.00%", (round(chg, 2), round(chg / base * 100, 2)), (50.0, 2.0))
            # The deposit-adjusted goal return: the $500 deposit is not P&L.
            twr = goal_mod.time_weighted_return_pct(2000.0, g["start_date"],
                                                    [("2026-08-04", 2000.0), (DAY, 2500.0)], [(DAY, 500.0)])
            naive = (2500.0 / 2000.0 - 1) * 100
            r.eq("full: compute_from_ledger strips the deposit (TWR from the producers)",
                 round(gp["cumulative_return_pct"], 4), round(twr, 4))
            r.ok("full: the deposit-adjusted return is 0.00%, not the naive +25.00%",
                 abs(gp["cumulative_return_pct"]) < 1e-9 and abs(naive - 25.0) < 1e-9, gp)
            r.ok("full: message 1 carries the deposit-adjusted return toward the goal",
                 msgs and f"Since the start, it is {fmt_pct(gp['cumulative_return_pct'])}, not counting money you "
                 "added." in msgs[0] and "The goal is 15%." in msgs[0], msgs[:1])
            r.ok("full: message 1 never shows the deposit as a gain", msgs and "+25.00%" not in msgs[0], msgs[:1])
            # 2. What moved: the three largest dollar movers, with their % moves.
            exp = sorted(((t, q * (p - c), (p / c - 1) * 100) for t, (q, p, c) in CLOSE_BOOK.items()),
                         key=lambda x: (-abs(x[1]), x[0]))[:3]
            r.eq("full: the 3 largest dollar movers (DLR, ANET, CEG; SGOV left out)",
                 [(m["ticker"], round(m["dollars"], 2), round(m["pct"], 2)) for m in model["movers"]],
                 [(t, round(d, 2), round(p, 2)) for t, d, p in exp])
            want2 = "What moved the account: " + ", ".join(
                f"{t} {fmt_pct(p)} ({'+' if d >= 0 else '-'}${abs(d):,.2f})" for t, d, p in exp[:2]) + \
                f" and {exp[2][0]} {fmt_pct(exp[2][2])} ({'+' if exp[2][1] >= 0 else '-'}${abs(exp[2][1]):,.2f})."
            r.eq("full: message 2 text", msgs[1] if len(msgs) > 1 else None, want2)
            # 3. What happened: the day's events (ET date) with names, and the watched-name filings.
            m3 = msgs[2] if len(msgs) > 2 else ""
            r.ok("full: message 3 names the day's events", "Today ANET filed its quarterly report (10-Q)." in m3 and
                 "CEG fell below $290.50, a good price to buy more." in m3 and "ANET had important news." in m3, m3)
            r.ok("full: message 3 leaves out an event of another ET day", "CEG had important news" not in m3, m3)
            r.ok("full: message 3 is one sentence per event, then the watched filings",
                 m3.startswith("Today ") and m3.endswith("Names we watch filed: TSM 6-K."), m3)
            # 4. What waits on you: pending proposals with their expiry; earnings in 5 trading days.
            m4 = msgs[3] if len(msgs) > 3 else ""
            r.ok("full: message 4 names the pending change and its expiry day",
                 f"card #{ids['change']} (a setting, open until 4:00 PM ET on Thursday, August 6)" in m4, m4)
            r.eq("full: only the pending proposal waits", [p["id"] for p in model["pending"]], [ids["change"]])
            # The brief runs after the close, so a pending proposal expires on a later day
            # (2026-08-06 16:00 ET here). "Its expiry" must name that day, as the card does
            # ("Open until 4:00 PM ET on Thursday, August 6.").
            exp_dt = store.parse_ts(proposals_expiry(root, ids["change"])).astimezone(market.ET)
            r.eq("full: the pending change expires at the next session's close",
                 exp_dt.strftime("%Y-%m-%d %H:%M"), "2026-08-06 16:00")
            r.ok("full: message 4 names the expiry day of a proposal that expires after the brief's day",
                 any(s in m4 for s in ("August 6", "Aug 6", "2026-08-06", "Thursday")), m4)
            r.ok("full: message 4 names earnings in 5 trading days (ANET 08-11 in, CEG 08-13 out)",
                 "ANET on 2026-08-11" in m4 and "CEG" not in m4.split("Earnings", 1)[-1], m4)
        finally:
            fc.unpin()


# --- brief.prose --------------------------------------------------------------------

_LIST_START = re.compile(r"^\s*(?:[-•*]|\d+\.)")


def case_prose():
    with fresh_root() as root:
        fc = FakeClock(SEED_AT).pin_market()
        try:
            full_day(root, fc)
            m1 = model_for(root, fc)
            a = notify.render_brief(m1)
            m2 = model_for(root, fc)
            b = notify.render_brief(m2)
            lines = [ln for m in a for ln in m.split("\n")]
            r.ok("prose: no line starts with -, •, * or <digit>.",
                 not [ln for ln in lines if _LIST_START.match(ln)], lines)
            r.ok("prose: no ' / '", all(" / " not in m for m in a), a)
            r.eq("prose: same inputs -> same model", m1, m2)
            r.eq("prose: same inputs -> same bytes", "\x00".join(a).encode(), "\x00".join(b).encode())
            r.ok("prose: two to four messages", 2 <= len(a) <= 4, len(a))
            # A name list with digits stays prose: earnings dates do not start a line.
            r.ok("prose: each message is one paragraph (no line break)", all("\n" not in m for m in a), a)
        finally:
            fc.unpin()


# --- brief.omit ---------------------------------------------------------------------

def case_omit():
    with fresh_root() as root:
        fc = FakeClock(SEED_AT).pin_market()
        try:
            full_day(root, fc, events=False, pending=False)
            model = model_for(root, fc, earnings={})
            msgs = notify.render_brief(model)
            r.eq("omit: no events -> no message 3; nothing pending, no earnings -> no message 4", len(msgs), 2)
            r.ok("omit: the two left are the day and the movers",
                 len(msgs) == 2 and msgs[0].startswith("The account closed") and msgs[1].startswith("What moved"), msgs)
            r.ok("omit: no 'Today' and no 'Waiting on your tap'",
                 all("Today " not in m and "Waiting on your tap" not in m and "Results coming" not in m for m in msgs),
                 msgs)
            # Earnings only: message 4 is there, with no pending part.
            model = model_for(root, fc, earnings={"ANET": "2026-08-07"})
            msgs = notify.render_brief(model)
            r.ok("omit: earnings only -> message 4 with no pending part",
                 len(msgs) == 3 and msgs[2].startswith("Results coming in the next five trading days: ANET on 2026-08-07"),
                 msgs)
            # Watched filings only: message 3 is there, with no events part.
            with root.led().connect() as conn:
                store.set_state(conn, f"watch_filings:{DAY}", json.dumps(["TSM 6-K", "AVGO 10-Q"]))
            msgs = notify.render_brief(model_for(root, fc, earnings={}))
            r.ok("omit: watched filings only -> message 3 with no events part",
                 len(msgs) == 3 and msgs[2] == "Names we watch filed: TSM 6-K and AVGO 10-Q.", msgs)
        finally:
            fc.unpin()


# --- brief.window -------------------------------------------------------------------

def case_window():
    days = market.trading_days_after("2026-11-20", 5)
    r.eq("window: 5 trading days after Fri 2026-11-20 skip Thanksgiving", days,
         ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27", "2026-11-30"])
    with fresh_root() as root:
        fc = FakeClock(SEED_AT).pin_market()
        try:
            dates = {"ANET": "2026-11-30", "CEG": "2026-12-01"}
            model = model_for(root, fc, day="2026-11-20", now="2026-11-20T21:30:00Z", earnings=dates, gp=None)
            r.eq("window: 2026-11-30 in, 2026-12-01 out", model["earnings"], [{"ticker": "ANET", "date": "2026-11-30"}])
            dates = {"ANET": "2026-11-26", "CEG": "2026-11-27"}
            model = model_for(root, fc, day="2026-11-20", now="2026-11-20T21:30:00Z", earnings=dates, gp=None)
            r.eq("window: the half day 11-27 is in; the holiday 11-26 is no session",
                 model["earnings"], [{"ticker": "CEG", "date": "2026-11-27"}])
            dates = {"ANET": "2026-11-20", "CEG": "2026-11-23"}
            model = model_for(root, fc, day="2026-11-20", now="2026-11-20T21:30:00Z", earnings=dates, gp=None)
            r.eq("window: the brief's own day is not 'the next five'", model["earnings"],
                 [{"ticker": "CEG", "date": "2026-11-23"}])
        finally:
            fc.unpin()


# --- brief.dedup --------------------------------------------------------------------

def case_dedup():
    with fresh_root() as root:
        fc = FakeClock(SEED_AT).pin_market()
        try:
            full_day(root, fc)
            sys.path.insert(0, str(REPO / "deploy" / "runner"))
            import run_monitor  # noqa: E402
            tg = FakeTG()
            led, cfg = root.led(), root.cfg()
            box = outbox.Outbox(led, transport=tg, chat_id=CHAT, clock=fc.monotonic, sleep=fc.sleep)

            def monitor(now):
                fc.set(now)
                m = run_monitor.Monitor(led=led, cfg=cfg, now_fn=lambda: store.parse_ts(now), box=box,
                                        earnings_fn=lambda t: EARNINGS.get(t), page=lambda **k: None,
                                        yf_price_fn=FakePrices(), sleep=fc.sleep)
                m.market_state()
                return m.close_brief()

            early = monitor("2026-08-05T20:10:00Z")        # 10 min after the close
            r.eq("dedup: a run under 15 min after the close sends nothing", (early, tg.calls), ("not due", []))
            first = monitor("2026-08-05T20:40:00Z")
            sends = [p for mth, p in tg.calls if mth == "sendMessage"]
            r.eq("dedup: the first run sends the brief", (first.get("messages") if isinstance(first, dict) else first,
                                                          len(sends)), (4, 4))
            r.ok("dedup: the monitor's message 1 names the 15% goal", sends and "The goal is 15%." in sends[0]["text"],
                 sends[:1])
            n = len(tg.calls)
            second = monitor("2026-08-05T21:40:00Z")
            r.eq("dedup: a second call for one trade_date sends nothing", (second, len(tg.calls)), ("not due", n))
            with led.connect() as conn:
                rows = [tuple(x) for x in conn.execute(
                    "SELECT trade_date, kind, stage FROM notifications WHERE kind='brief'").fetchall()]
                mirrored = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE source='brief'").fetchone()[0]
            r.eq("dedup: one notifications row (trade_date, brief, brief)", rows, [(DAY, "brief", "brief")])
            r.eq("dedup: each brief message is mirrored once", mirrored, 4)
            r.ok("dedup: already_sent is true for the day", brief.already_sent(led, DAY), DAY)
        finally:
            fc.unpin()


def main() -> int:
    for case in (case_full, case_prose, case_omit, case_window, case_dedup):
        try:
            case()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
