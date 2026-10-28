"""Price levels: fire, re-arm, and the quote source (plan §9.1 step 6, §10.3 set_thesis).

Each case is a ``case_*`` function in ``CASES``. The fire and re-arm cases arm a level with
``levels.upsert_level`` and run ``levels.check_levels``. The source case uses the real seed,
the real ``snapshot-commit`` and a confirmed ``set_thesis`` change (``build_change`` then a
Confirm tap through ``approvals.handle_tap``).
Run: .venv/bin/python tests/test_desk_levels.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import FakeClock, FakePrices, Results, seed_desk, snapshot, temp_root  # noqa: E402

from lib.desk import levels, store  # noqa: E402

r = Results("desk-levels")
NOW = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)      # a Wednesday session
DAY = "2026-08-05"
OWNER = "111"


def check(led, quote_by_ticker: dict, now=NOW) -> dict:
    with led.connect() as conn:
        return levels.check_levels(conn, now_dt=now, yf_price_fn=FakePrices(quote_by_ticker))


def level_row(led, ticker, level) -> dict:
    with led.connect() as conn:
        return next((x for x in levels.get_levels(conn, ticker) if x["level"] == level), None)


def events_of(led, ticker=None) -> list:
    with led.connect() as conn:
        sql = "SELECT * FROM events" + (" WHERE ticker=?" if ticker else "") + " ORDER BY id"
        return [dict(x) for x in conn.execute(sql, (ticker,) if ticker else ()).fetchall()]


def arm(led, ticker, level, price):
    with led.connect() as conn:
        levels.upsert_level(conn, ticker, level, price, store.iso(NOW), armed=1)


def case_fire():
    clock = FakeClock(NOW).pin_market()
    try:
        with temp_root() as root:
            led = root.led()
            for t, lvl, near in (("AAA", "buy_below", 100.01), ("BBB", "entry", 100.01), ("CCC", "trim_above", 99.99)):
                arm(led, t, lvl, 100.00)
                out = check(led, {t: near})
                r.eq(f"fire {lvl} not at {near}", (out["events"], level_row(led, t, lvl)["armed"]), ([], 1))
                r.eq(f"fire {lvl} the miss records the quote", level_row(led, t, lvl)["last_quote"], near)
                out = check(led, {t: 100.00})
                r.eq(f"fire {lvl} fires at quote = price", len(out["events"]), 1)
                ev = events_of(led, t)
                r.eq(f"fire {lvl} one event", len(ev), 1)
                if ev:
                    e = ev[0]
                    r.eq(f"fire {lvl} dedup key", e["dedup_key"], f"level:{t}:{lvl}:{DAY}")
                    r.eq(f"fire {lvl} event fields", (e["type"], e["created_by"], e["status"], e["ticker"]),
                         ("price_level", "monitor", "new", t))
                    d = store.loads(e["detail_json"], {})
                    r.eq(f"fire {lvl} event detail", (d.get("level"), d.get("price"), d.get("quote"), d.get("source")),
                         (lvl, 100.0, 100.0, "yfinance"))
                row = level_row(led, t, lvl)
                r.eq(f"fire {lvl} armed=0", (row["armed"], row["last_quote"]), (0, 100.0))
            # Past the price fires too (a gap through the level).
            arm(led, "DDD", "buy_below", 50.0)
            arm(led, "EEE", "trim_above", 50.0)
            out = check(led, {"DDD": 45.0, "EEE": 55.0, "AAA": 100.0, "BBB": 100.0, "CCC": 100.0})
            r.eq("fire a gap through the level fires", sorted(e["ticker"] for e in events_of(led)
                                                             if e["ticker"] in ("DDD", "EEE")), ["DDD", "EEE"])
            r.eq("fire a disarmed level does not fire again", len(out["events"]), 2)
            # A ticker with no quote is reported and keeps its state.
            arm(led, "FFF", "buy_below", 10.0)
            out = check(led, {})
            r.ok("fire no quote: listed, no event", "FFF" in out["no_quote"] and not out["events"], out)
            r.eq("fire no quote: still armed", level_row(led, "FFF", "buy_below")["armed"], 1)
            r.raises("fire an unknown level is refused",
                     lambda: arm(led, "GGG", "stop_loss", 1.0), ValueError, "unknown level")
    finally:
        clock.unpin()


def case_rearm():
    clock = FakeClock(NOW).pin_market()
    try:
        with temp_root() as root:
            led = root.led()
            for t, lvl, fire_q, off_q, on_q in (("AAA", "buy_below", 100.00, 101.99, 102.00),
                                               ("BBB", "entry", 100.00, 101.99, 102.00),
                                               ("CCC", "trim_above", 100.00, 98.01, 98.00)):
                arm(led, t, lvl, 100.00)
                check(led, {t: fire_q})
                r.eq(f"rearm {lvl} fired", level_row(led, t, lvl)["armed"], 0)
                out = check(led, {t: off_q})
                r.eq(f"rearm {lvl} {off_q} stays off", (level_row(led, t, lvl)["armed"], out["rearmed"]), (0, []))
                out = check(led, {t: on_q})
                r.eq(f"rearm {lvl} {on_q} arms", (level_row(led, t, lvl)["armed"], out["rearmed"]), (1, [f"{t}:{lvl}"]))
                r.eq(f"rearm {lvl} the re-arm makes no event", len(events_of(led, t)), 1)
                out = check(led, {t: fire_q})
                r.eq(f"rearm {lvl} a re-fire on the same date adds no event", (len(events_of(led, t)), out["events"]),
                     (1, []))
                r.eq(f"rearm {lvl} the re-fire disarms", level_row(led, t, lvl)["armed"], 0)
                check(led, {t: on_q})
                nxt = NOW + timedelta(days=1)
                out = check(led, {t: fire_q}, now=nxt)
                r.eq(f"rearm {lvl} a fire on the next date adds one event", len(events_of(led, t)), 2)
                r.eq(f"rearm {lvl} next-date key", events_of(led, t)[-1]["dedup_key"], f"level:{t}:{lvl}:2026-08-06")
    finally:
        clock.unpin()


def _set_thesis(root, led, *, buy_below, trim_above, at):
    """Build a ``set_thesis`` change, give its card Telegram ids, and tap Confirm."""
    from lib.desk import approvals, proposals
    cfg = root.cfg()
    change = {"kind": "set_thesis", "ticker": "ANET", "premise_link": "AI networking is a bottleneck.",
              "summary": "Arista sells the back-end network of AI clusters.",
              "drivers": ["AI back-end share"], "falsifiers": ["a hyperscaler moves to white box"],
              "valuation": {"method": "price_levels", "reason": "owner levels",
                            "buy_below": buy_below, "trim_above": trim_above},
              "evidence": []}
    with led.transaction() as conn:
        built = proposals.build_change(conn, cfg, change=change, rationale="Set the ANET levels.",
                                       created_by="desk", now_dt=at)
    pid = built["proposal_id"]
    with led.connect() as conn:
        conn.execute("UPDATE proposals SET tg_chat_id=?, tg_message_id=? WHERE id=?", (OWNER, 1000 + pid, pid))
        nonce = conn.execute("SELECT nonce FROM proposals WHERE id=?", (pid,)).fetchone()[0]
    res = approvals.handle_tap(led, cfg, callback_data=f"p:{pid}:{nonce}:a", from_id=OWNER, chat_id=OWNER,
                               message_id=1000 + pid, owner_ids=[OWNER], now_dt=at,
                               yf_price_fn=FakePrices())
    return pid, res


def case_source():
    t0 = datetime(2026, 8, 5, 13, 35, tzinfo=timezone.utc)
    clock = FakeClock(t0).pin_market()
    try:
        with temp_root() as root:
            seed_desk(root)
            led = root.led()
            snap = snapshot(root, at="2026-08-05T13:35:00Z")
            with led.connect() as conn:
                sid = conn.execute("SELECT MAX(id) FROM broker_snapshots").fetchone()[0]
            r.ok("source the snapshot committed", sid is not None, snap)
            # set_thesis upserts both levels armed.
            at = datetime(2026, 8, 5, 13, 40, tzinfo=timezone.utc)
            clock.set(at)
            pid, res = _set_thesis(root, led, buy_below=150.0, trim_above=200.0, at=at)
            r.eq("source the set_thesis tap confirms", (res["answer"], res["ok"]), ("Confirmed", True))
            with led.connect() as conn:
                st = conn.execute("SELECT state FROM proposals WHERE id=?", (pid,)).fetchone()[0]
                th = conn.execute("SELECT version, buy_below, trim_above FROM theses WHERE ticker='ANET'").fetchall()
            r.eq("source the change is applied", st, "applied")
            r.eq("source thesis v1 stored", [tuple(x) for x in th], [(1, 150.0, 200.0)])
            bb, ta = level_row(led, "ANET", "buy_below"), level_row(led, "ANET", "trim_above")
            r.eq("source set_thesis upserts both levels armed",
                 ((bb or {}).get("price"), (bb or {}).get("armed"), (ta or {}).get("price"), (ta or {}).get("armed")),
                 (150.0, 1, 200.0, 1))
            # CEG: the snapshot (300.00) does not fire buy_below 299; yfinance (290) would.
            with led.connect() as conn:
                levels.upsert_level(conn, "CEG", "buy_below", 299.0, store.iso(at), armed=1)
            yf = {"ANET": 149.0, "CEG": 290.0, "SGOV": 100.0}
            t59 = t0 + timedelta(minutes=59)
            clock.set(t59)
            with led.connect() as conn:
                out = levels.check_levels(conn, now_dt=t59, yf_price_fn=FakePrices(yf))
            ev = [e for e in events_of(led) if e["type"] == "price_level"]
            r.eq("source 59 min: ANET buy_below fires from the snapshot", [e["dedup_key"] for e in ev],
                 [f"level:ANET:buy_below:{DAY}"])
            d = store.loads(ev[0]["detail_json"], {}) if ev else {}
            r.eq("source 59 min: the snapshot gives the quote", (d.get("quote"), d.get("source")),
                 (150.0, f"snapshot:{sid}"))
            r.eq("source 59 min: CEG reads the snapshot 300", level_row(led, "CEG", "buy_below")["last_quote"], 300.0)
            r.eq("source 59 min: CEG does not fire", level_row(led, "CEG", "buy_below")["armed"], 1)
            t61 = t0 + timedelta(minutes=61)
            clock.set(t61)
            with led.connect() as conn:
                out = levels.check_levels(conn, now_dt=t61, yf_price_fn=FakePrices(yf))
            ev = [e for e in events_of(led) if e["type"] == "price_level" and e["ticker"] == "CEG"]
            d = store.loads(ev[0]["detail_json"], {}) if ev else {}
            r.eq("source 61 min: yfinance gives the quote", (d.get("quote"), d.get("source")), (290.0, "yfinance"))
            r.eq("source 61 min: ANET trim_above reads yfinance", level_row(led, "ANET", "trim_above")["last_quote"],
                 149.0)
            r.eq("source 61 min: one CEG event", len(out["events"]), 1)
            # Exactly 60 minutes old is not "younger than 60 minutes" (§9.1 step 6).
            t60 = t0 + timedelta(minutes=60)
            with led.connect() as conn:
                levels.upsert_level(conn, "SGOV", "trim_above", 1000.0, store.iso(at), armed=1)
                levels.check_levels(conn, now_dt=t60, yf_price_fn=FakePrices({**yf, "SGOV": 99.5}))
            r.eq("source exactly 60 min: yfinance gives the quote", level_row(led, "SGOV", "trim_above")["last_quote"],
                 99.5)
            # The newest young snapshot that HOLDS the ticker gives the quote.
            snapshot(root, at="2026-08-05T14:40:00Z", positions={"CEG": 1, "SGOV": 11},
                     quotes={"CEG": 301.0, "SGOV": 100.0})
            snapshot(root, at="2026-08-05T14:45:00Z", positions={"ANET": 2, "CEG": 1, "SGOV": 11},
                     quotes={"ANET": 151.0, "CEG": 302.0, "SGOV": 100.0})
            snapshot(root, at="2026-08-05T14:50:00Z", positions={"CEG": 1, "SGOV": 11},
                     quotes={"CEG": 303.0, "SGOV": 100.0})
            t55 = datetime(2026, 8, 5, 14, 55, tzinfo=timezone.utc)
            with led.connect() as conn:
                levels.check_levels(conn, now_dt=t55, yf_price_fn=FakePrices(yf))
            r.eq("source the newest young snapshot that holds ANET", level_row(led, "ANET", "trim_above")["last_quote"],
                 151.0)
            r.eq("source the newest young snapshot for SGOV", level_row(led, "SGOV", "trim_above")["last_quote"], 100.0)
            # A newer set_thesis re-arms a fired level at its new price.
            at2 = t61 + timedelta(minutes=1)
            clock.set(at2)
            pid2, res2 = _set_thesis(root, led, buy_below=140.0, trim_above=210.0, at=at2)
            r.eq("source a second set_thesis confirms", res2["answer"], "Confirmed")
            bb = level_row(led, "ANET", "buy_below")
            r.eq("source the second set_thesis re-arms buy_below at the new price", (bb["price"], bb["armed"]),
                 (140.0, 1))
            r.eq("source the second set_thesis moves trim_above",
                 (level_row(led, "ANET", "trim_above")["price"], level_row(led, "ANET", "trim_above")["armed"]),
                 (210.0, 1))
    finally:
        clock.unpin()


CASES = [case_fire, case_rearm, case_source]

if __name__ == "__main__":
    for case in CASES:
        try:
            case()
        except Exception as e:  # noqa: BLE001 — one broken case must not hide the others
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__} raised", False, f"{type(e).__name__}: {e}")
    raise SystemExit(r.done())
