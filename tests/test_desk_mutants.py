#!/usr/bin/env python3
"""Cases that kill the surviving mutants of the adversarial test round 1 (findings T2, T4-T7,
T10-T12; plan §10-§11, §18.4). Each case names the mutant (file:line rule) it kills.

The harness is the one of ``tests/test_desk_execution.py`` (``case``, ``Ctx``, the scripted
broker model) and of ``tests/test_desk_proposals.py`` (the seeded and swept ledger). Ledger
state comes from the producers: ``desk-seed``, ``snapshot-commit``, ``build_trade``,
``build_change``, ``approvals.handle_tap``, ``exec-prepare``/``exec-check``, the guard hooks
and ``proposals.requote_move``. One case changes one VALUE of a produced row (the kind of
a running execute job), as §18.1 rule 4 allows. Two review cases add a size field to the
model's review INPUT (not to a captured answer), as ``test_broker_guard`` place_body does for
the place input. No network, no LLM, no broker.
"""

from __future__ import annotations

import copy
import gzip
import json
import sys
import traceback
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO), str(REPO / "tests"), str(REPO / "deploy" / "runner")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_desk_execution as X  # noqa: E402  (its checks run only under __main__)
import test_desk_proposals as P  # noqa: E402
from desk_fakes import FIX, Results  # noqa: E402

import broker_guard  # noqa: E402
import chat_bridge  # noqa: E402
import run_broker  # noqa: E402
from lib import market  # noqa: E402
from lib.desk import approvals, execution, outbox, proposals, store  # noqa: E402

r = Results("desk-mutants")
X.r = r            # X.case records a crash of a case in this suite
PLACE = "mcp__robinhood-trading__place_equity_order"
DAY = "2026-08-05"
std = X.std


def _ok_reason(res, want_ok, want_reason):
    ok, why = res
    return ok is want_ok and why == want_reason


# =============================================================================================
# T2: Robinhood's order carries its own ref_id (rh_place), never the leg's
# =============================================================================================

def t_foreign_ref():
    rh_place = json.loads(gzip.decompress((FIX / "rh_place.json.gz").read_bytes()))["result"]
    # exec-commit on the production place result: kills a commit rule that needs an equal ref_id.
    with X.case("mut rh_place commit") as c:
        pid, env, fields = X.placing_leg(c)
        o = copy.deepcopy(rh_place)
        o.update(id="6ada272a-0000-4a58-856f-0c8cd6858f76", symbol=fields["symbol"], side=fields["side"])
        o["dollar_based_amount"]["amount"] = f"{float(fields['dollar_amount']):.6f}"
        r.ok("T2 commit: the rh_place order has a ref_id that is not the leg's", o["ref_id"] != fields["ref_id"],
             (o["ref_id"], fields["ref_id"]))
        code, out = c.tick("exec-commit", "--proposal", pid, "--input",
                           c.write("res.json", {"place_equity_order": [o]}), env=env)
        leg = c.leg(fields["ref_id"])
        r.eq("T2 commit: the rh_place result (state unconfirmed) -> placed, bound by symbol and side",
             (code, leg["state"], leg["broker_order_id"]), (0, "placed", o["id"]))
        r.eq("T2 commit: the leg keeps its own ref_id", [l["ref_id"] for l in c.legs(pid)], [fields["ref_id"]])

    # A lost placing leg and its live order with Robinhood's ref_id: recorded, never re-sent.
    # Kills x_match_foreign_ref (execution.py match_order: skip an order whose ref_id differs).
    with X.case("mut recovery foreign ref") as c:
        pid, fields, _b = X._lost(c)
        sub = c.leg(fields["ref_id"])["submitted_at"]
        o = X.order_from(fields, oid="RH-LIVE-1", state="queued", created_at=sub.replace("Z", ".200000Z"))
        r.ok("T2 recovery: the live order carries Robinhood's ref_id", o["ref_id"] != fields["ref_id"], o["ref_id"])
        runner, _ = X.run_job(c, pid, {"recover": std(), "orders": [o], "after": None})
        leg = c.leg(fields["ref_id"])
        places = [x for x in run_broker.tool_calls(runner.last_bot.s.text()) if x["name"].endswith("place_equity_order")]
        r.eq("T2 recovery: a field match with a foreign ref_id is recorded", (leg["broker_order_id"], leg["state"]),
             ("RH-LIVE-1", "placed"))
        r.eq("T2 recovery: the order is never sent again (no double order)", (places, c.rh.orders), ([], {}))
        r.eq("T2 recovery: one leg, one ref_id", [l["ref_id"] for l in c.legs(pid)], [fields["ref_id"]])

    # match_order itself: the equal-ref_id rule wins alone; a foreign ref_id proves nothing.
    with X.case("mut match_order rules") as c:
        pid, env, fields = X.placing_leg(c)
        leg = c.leg(fields["ref_id"])
        sub = store.parse_ts(leg["submitted_at"])
        same = X.order_from(fields, oid="EQ", created_at=store.iso(sub), ref_id=fields["ref_id"])
        same["dollar_based_amount"]["amount"] = "199.000000"
        r.eq("T2 match: an order with the leg's ref_id matches, whatever its amount",
             (execution.match_order(leg, [same], set()) or {}).get("id"), "EQ")
        other = X.order_from(fields, oid="FOREIGN", created_at=store.iso(sub))
        r.eq("T2 match: a foreign ref_id with the leg's fields matches by the fields",
             (execution.match_order(leg, [other], set()) or {}).get("id"), "FOREIGN")
        other_amt = X.order_from(fields, oid="FOREIGN2", created_at=store.iso(sub))
        other_amt["dollar_based_amount"]["amount"] = "199.000000"
        r.eq("T2 match: a foreign ref_id with another amount does not match",
             execution.match_order(leg, [other_amt], set()), None)


# =============================================================================================
# T4: two buys share one budget (execution.py _check_buys: money -= dollars)
# =============================================================================================

def t_two_buys_one_budget():
    pos = {"ANET": 2, "SGOV": 11}
    with X.case("mut two buys", positions=pos, buying_power=1000.0, equity=2400.0) as c:
        c.setting("cash_floor_pct", 0.0)
        pid, env = c.ready(["ANET", "CEG"])
        built = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("T4: the build has two buy legs", sorted((t, l["side"]) for t, l in built.items()),
             [("ANET", "buy"), ("CEG", "buy")])
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std(positions=pos, bp=1000.0, equity=2400.0))), env)
        bp = 400.0
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std(positions=pos, bp=bp, equity=2400.0))), env)
        # The plan rule (§11.2 step 5), computed from the producers' rows in leg order.
        d = 2 * 150.0 + 11 * 100.0 + bp
        values = {"ANET": 2 * 150.0, "CEG": 0.0}
        targets = {"ANET": 0.25 * d, "CEG": 0.15 * d}
        money = min(bp - 5.0, bp + 1100.0 - 0.0 * d)
        want = {}
        left = money
        for l in [l for l in c.legs(pid) if l["side"] == "buy"]:
            t = l["ticker"]
            cap = max(0.0, min(max(0.0, targets[t] - values[t]), left))
            dollars = round(min(float(built[t]["dollar_amount"]), cap), 2)
            want[t] = dollars
            left -= dollars
        got = {l["symbol"]: float(l["dollar_amount"]) for l in out.get("legs", [])}
        r.ok("T4: the first leg leaves less than the full money (the case binds)",
             min(want.values()) < min(money, max(targets[t] - values[t] for t in targets)), want)
        r.eq("T4: each buy is cut to what the earlier buys leave", got, want)
        r.ok("T4: the checked buys never sum above the money", sum(got.values()) <= money + 0.005,
             (sum(got.values()), money))
        rows = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("T4: the rows follow", {t: rows[t]["dollar_amount"] for t in want}, want)


# =============================================================================================
# T5: a drop_company exit sells the whole live position (execution.py _check_sells exit_leg)
# =============================================================================================

def _drop_ceg_exit(c):
    """build_change drop_company CEG (exit now), the owner's confirm tap, the follow-up card,
    the owner's approve tap, exec-prepare. Returns (pid, env) of the exit trade."""
    with c.led.transaction() as conn:
        ch = proposals.build_change(conn, c.cfg, change={"kind": "drop_company", "ticker": "CEG",
                                                          "exit": {"mode": "now"},
                                                          "redistribute": [{"ticker": "SGOV", "target_weight": 75}]},
                                    rationale="The owner asked to drop CEG.", evidence=[], created_by="desk",
                                    now_dt=c.clk.now)
    cid = ch["proposal_id"]
    outbox.send_change_card(c.led, c.box, c.cfg, cid, now_dt=c.clk.now)
    p = c.prop(cid)
    res = approvals.handle_tap(c.led, c.cfg, callback_data=f"p:{cid}:{p['nonce']}:a", from_id=X.OWNER,
                               chat_id=X.CHAT, message_id=p["tg_message_id"], owner_ids=[X.OWNER],
                               now_dt=c.clk.now, yf_price_fn=X._no_yfinance)
    assert res.get("ok") and res.get("cards"), res
    fu = res["cards"][0]
    payload = json.loads(c.prop(fu)["payload_json"])
    built = {"proposal_id": fu, "legs": outbox.legs_for_card(c.led, fu),
             "skipped": payload.get("skipped", []), "scaled": payload.get("scaled"), "superseded": []}
    c.clk.advance(20)
    c.approve(built)
    c.clk.advance(10)
    prep, env = c.start(fu)
    assert prep.get("ok"), prep
    return fu, env


def t_exit_full():
    with X.case("mut exit full") as c:
        pid, env = _drop_ceg_exit(c)
        legs = c.legs(pid)
        r.eq("T5: the follow-up is one CEG sell of the whole 1 sh", [(l["ticker"], l["side"], l["quantity"]) for l in legs],
             [("CEG", "sell", 1.0)])
        with c.led.connect() as conn:
            goal = proposals.active_goal(conn)
            book = {x["ticker"]: x for x in proposals.book_rows(conn, goal["id"], ("active", "exiting"))}
        r.eq("T5: the confirmed drop made CEG exiting with target 0",
             (float(book["CEG"]["target_weight"]), book["CEG"]["status"]), (0.0, "exiting"))
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        r.eq("T5: the exit sells the live held quantity, not a part (kills ex_exit_full)",
             [(l["symbol"], l["quantity"]) for l in out.get("legs", [])], [("CEG", "1.000000")])
        r.eq("T5: the row is checked at the held quantity", (c.legs(pid)[0]["state"], c.legs(pid)[0]["quantity"]),
             ("checked", 1.0))
    with X.case("mut exit free qty") as c:
        pid, env = _drop_ceg_exit(c)
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(), mutate=X.set_avail("CEG", 0.6))), env)
        r.eq("T5: the exit is bounded by the free quantity", [l["quantity"] for l in out.get("legs", [])], ["0.600000"])
    with X.case("mut exit dust") as c:
        pid, env = _drop_ceg_exit(c)
        live = std(positions={"ANET": 2, "CEG": 0.003, "SGOV": 11})
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**live)), env)
        leg = c.legs(pid)[0]
        r.eq("T5: a full exit under $1 (0.003 sh at $300) is skipped 'dust'",
             (out.get("action"), leg["state"], leg["skip_reason"]), ("none", "skipped", "dust"))


# =============================================================================================
# T6: a buy-only event on a name at or above its target is refused (proposals.py side rule)
# =============================================================================================

def t_side_rule():
    def go(label, positions, rows):
        with P.case(at="2026-08-05T14:30:00Z") as c:
            c.snap(positions=positions)
            e10q, e8k = P._event_ids(c)
            bb = P._level_event(c, "ANET", "buy_below", 155.0)
            P.queue(c)
            f = c.finding(c.doc_id(P.ACC_10Q, "10-Q:I-2"), "2026-08-05T14:31:00Z")
            ev = ["finding:%d" % f]
            ids = {"filing": e10q, "earnings": e8k, "buy_below": bb}
            for ev_name, v, needle in rows:
                P.verdict(c, ids[ev_name], v)
                e = P.refusal(lambda: P.event_trade(c, ids[ev_name], evidence=ev))
                r.ok(f"T6 {label}: {ev_name} + {v} -> refused, '{needle}'", e is not None and needle in e, e)
            n = c.one("SELECT COUNT(*) n FROM proposals WHERE kind='trade'")["n"]
            r.eq(f"T6 {label}: no trade proposal was built", n, 0)
    try:
        # ANET 5 sh at $150: $750 of $2,450, above its 25% target ($612.50).
        go("above target", {"ANET": 5, "CEG": 1, "SGOV": 11},
           [("filing", "strengthened", "allows no buy"), ("buy_below", "intact", "allows no buy")])
        # ANET 3.777778 sh: $566.67 of $2,266.67, at its target.
        go("at target", {"ANET": 3.777778, "CEG": 1, "SGOV": 11}, [("earnings", "strengthened", "allows no buy")])
        # ANET 2 sh: $300 of $2,000, under its target ($500).
        go("under target", {"ANET": 2, "CEG": 1, "SGOV": 11}, [("filing", "weakened", "allows no sell")])
    except Exception:  # noqa: BLE001
        r.ok("T6: ran without an exception", False, traceback.format_exc()[-3000:])


# =============================================================================================
# T7: the guard binds a leg to its proposal and to its running execute job
# =============================================================================================

def _checked_buy(c, pid, env, pos=None, bp=None):
    spec = std(**({"positions": pos} if pos else {}), **({"bp": bp} if bp else {}))
    c.check(pid, "sells", c.write(f"s{pid}.json", c.snapfile(**spec)), env)
    out = c.check(pid, "buys", c.write(f"b{pid}.json", c.snapfile(**spec)), env)
    fields = out["legs"][0]
    c.clk.advance(1)
    rv = broker_guard.record_review(X.review_payload(fields), env=env, now=c.clk.now)
    assert rv[0] == 1, rv
    c.clk.advance(1)
    return fields


def t_guard_binding():
    pos = {"ANET": 2, "SGOV": 11}
    with X.case("mut guard two proposals", positions=pos, buying_power=1000.0, equity=2400.0) as c:
        c.setting("cash_floor_pct", 0.0)
        pa, env_a = c.ready(["ANET"])
        pb, env_b = c.ready(["CEG"])
        r.eq("T7: two proposals execute at once, each with its own running execute job",
             [(c.prop(p)["state"], c.job_of(p)["status"], c.job_of(p)["kind"]) for p in (pa, pb)],
             [("executing", "running", "execute")] * 2)
        fa = _checked_buy(c, pa, env_a, pos=pos, bp=1000.0)
        ev = lambda env: broker_guard.evaluate(PLACE, fa, env=env, now=c.clk.now)  # noqa: E731
        # Kills bg_leg_prop_match (broker_guard.py:148).
        r.ok("T7: A's leg under PROPOSAL=B / JOB=B -> 'the leg is not part of this job's proposal'",
             _ok_reason(ev(env_b), False, "the leg is not part of this job's proposal"), ev(env_b))
        # Kills bg_job_prop_match (broker_guard.py:151): the job runs, is an execute job, but for B.
        mixed = {**env_a, "QUIVER_BROKER_JOB": env_b["QUIVER_BROKER_JOB"]}
        r.ok("T7: PROPOSAL=A with JOB=B (running, execute) -> 'the execute job is not running for this proposal'",
             _ok_reason(ev(mixed), False, "the execute job is not running for this proposal"), ev(mixed))
        # Kills bg_job_kind (broker_guard.py:151): A's own running job with its kind VALUE changed.
        ja = env_a["QUIVER_BROKER_JOB"]
        with c.led.connect() as conn:
            conn.execute("UPDATE broker_jobs SET kind='snapshot' WHERE id=?", (int(ja),))
        r.ok("T7: A's running job with kind 'snapshot' -> 'the execute job is not running for this proposal'",
             _ok_reason(ev(env_a), False, "the execute job is not running for this proposal"), ev(env_a))
        with c.led.connect() as conn:
            conn.execute("UPDATE broker_jobs SET kind='execute' WHERE id=?", (int(ja),))
        r.eq("T7: no deny moved the leg", c.leg(fa["ref_id"])["state"], "checked")
        res = ev(env_a)
        r.ok("T7 control: the same call under its own proposal and job is allowed", res[0] is True, res)
        r.eq("T7 control: the allow moves A's leg to placing", c.leg(fa["ref_id"])["state"], "placing")
        r.eq("T7: B's leg is untouched", [l["state"] for l in c.legs(pb)], ["approved"])

    # Kills bg_prop_requoted (broker_guard.py:154): the proposal is requoted by the producer.
    with X.case("mut guard requoted") as c:
        pid, env = c.ready(["ANET"])
        f = _checked_buy(c, pid, env)
        with c.led.transaction() as conn:
            moved = proposals.requote_move(conn, pid)
        r.eq("T7 requoted: requote_move moved the proposal", (moved, c.prop(pid)["state"]), (True, "requoted"))
        res = broker_guard.evaluate(PLACE, f, env=env, now=c.clk.now)
        r.ok("T7 requoted: a place on a requoted proposal -> 'the proposal is requoted, not executing'",
             _ok_reason(res, False, "the proposal is requoted, not executing"), res)


# =============================================================================================
# T10: exec-check stops on an existing halt or KILL in each phase
# =============================================================================================

def t_halt_kill_phases():
    # The halt comes from the exec-check of another executing proposal (B): its own
    # daily-loss check halts the day and fails only B. (A snapshot-commit halt also fails
    # every executing proposal, so it never reaches A's exec-check.)
    sell_pos = {"ANET": 5, "CEG": 1, "SGOV": 11}
    # Kills ex_sells_halted_existing (execution.py _check_sells: led.is_halted).
    with X.case("mut sells halted", positions=sell_pos) as c:
        c.setting("cash_floor_pct", 0.0)
        pa, env_a = c.ready(["ANET"])
        pb, env_b = c.ready(["CEG"])
        r.eq("T10 sells: A is a sell, B is a buy", (c.legs(pa)[0]["side"], c.legs(pb)[0]["side"]), ("sell", "buy"))
        out_b = c.check(pb, "sells", c.write("sb.json", c.snapfile(**std(positions=sell_pos, equity=1600.0))), env_b)
        r.eq("T10 sells: B's check halts the day and fails B", (out_b.get("halted_now"), c.led.is_halted(DAY),
                                                                 c.prop(pb)["state"], c.prop(pa)["state"]),
             (True, True, "failed", "executing"))
        c.clk.advance(5)
        out = c.check(pa, "sells", c.write("sa.json", c.snapfile(**std(positions=sell_pos))), env_a)
        r.eq("T10 sells: an existing halt -> no leg, reason halted, not halted_now",
             (out.get("action"), out.get("reason"), out.get("halted_now"), out.get("legs")), ("none", "halted", False, None))
        r.eq("T10 sells: A failed, its sell never checked",
             (c.prop(pa)["state"], [l["state"] for l in c.legs(pa)]), ("failed", ["skipped"]))
    # Kills ex_buys_halted_existing (execution.py _check_buys: led.is_halted).
    pos = {"ANET": 2, "SGOV": 11}
    with X.case("mut buys halted", positions=pos, buying_power=1000.0, equity=2400.0) as c:
        c.setting("cash_floor_pct", 0.0)
        pa, env_a = c.ready(["ANET"])
        pb, env_b = c.ready(["CEG"])
        spec = std(positions=pos, bp=1000.0, equity=2400.0)
        c.check(pa, "sells", c.write("sa.json", c.snapfile(**spec)), env_a)
        out_b = c.check(pb, "sells", c.write("sb.json", c.snapfile(**{**spec, "equity": 1900.0})), env_b)
        r.eq("T10 buys: B's check halts the day", (out_b.get("halted_now"), c.led.is_halted(DAY)), (True, True))
        c.clk.advance(5)
        out = c.check(pa, "buys", c.write("ba.json", c.snapfile(**spec)), env_a)
        leg = c.legs(pa)[0]
        r.eq("T10 buys: an existing halt -> the buy skipped 'halted'", (out.get("action"), out.get("reason"),
                                                                        leg["state"], leg["skip_reason"]),
             ("none", "halted", "skipped", "halted"))
    # Kills ex_buys_kill (execution.py _check_buys: kill_active).
    with X.case("mut buys kill") as c:
        pid, env = c.ready(["ANET"])
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        c.root.kill.write_text("stop", encoding="utf-8")
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std())), env)
        leg = c.legs(pid)[0]
        r.eq("T10 buys: KILL -> the buy skipped 'kill'", (out.get("action"), leg["state"], leg["skip_reason"]),
             ("none", "skipped", "kill"))
    # The job: KILL written after a filled sell -> the buy skipped 'kill', partially_executed.
    sell_qty = round((750 - 0.25 * 2150) / 150, 6)
    with X.case("mut job kill after sell", positions=X.POS_SB, quotes=X.Q_SB, equity=2150.0) as c:
        built = X.two_leg(c)
        pid = built["proposal_id"]
        c.approve(built)
        c.rh.script = {"ANET": ["filled"]}
        c.rh.fill = {"ANET": (sell_qty, 150.0)}
        after = X.sb(positions={"ANET": 5 - sell_qty, "SGOV": 11}, bp=300 + sell_qty * 150)

        def buys(ctx):
            ctx.root.kill.write_text("stop after the sell", encoding="utf-8")
            return ctx.snapfile(**after)
        X.run_job(c, pid, {"sells": X.sb(), "buys": buys, "after": after})
        legs = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("T10 job: the sell filled", legs["ANET"]["state"], "filled")
        r.eq("T10 job: KILL after the sell -> the buy skipped 'kill'", (legs["CEG"]["state"], legs["CEG"]["skip_reason"]),
             ("skipped", "kill"))
        r.eq("T10 job: the proposal is partially_executed", c.prop(pid)["state"], "partially_executed")
        r.eq("T10 job: no buy reached the broker", sorted(o["symbol"] for o in c.rh.orders.values()), ["ANET"])


# =============================================================================================
# T11: the review answer must be for the leg, and a review names one size field
# =============================================================================================

def t_review_rules():
    with X.case("mut review buy") as c:
        pid, env = c.ready(["ANET"])
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std())), env)
        f = out["legs"][0]
        c.clk.advance(1)

        def rec(payload):
            res = broker_guard.record_review(payload, env=env, now=c.clk.now)
            return res, c.leg(f["ref_id"])["review_ok"]

        p = X.review_payload(f)
        p["tool_response"]["data"]["symbol"] = "CEG"
        r.eq("T11: an answer for another symbol -> review_ok 0 (kills bg_rv_symbol)", rec(p),
             ((0, "the review is for another symbol"), 0))
        p = X.review_payload(f)
        p["tool_response"]["data"]["side"] = "sell"
        r.eq("T11: an answer for another side -> review_ok 0 (kills bg_rv_side)", rec(p),
             ((0, "the review is for another side"), 0))
        # The model's review input with both size fields (a model input, as in place_body).
        p = X.review_payload(f)
        p["tool_input"]["quantity"] = "1.333333"
        r.eq("T11: a buy review with dollar_amount and quantity matches no leg (kills bg_rr_buy_qty)", rec(p),
             ((0, "no checked leg matches this review"), 0))
        r.eq("T11 control: the clean answer -> review_ok 1", rec(X.review_payload(f)), ((1, "clean review"), 1))
    with X.case("mut review sell", positions={"ANET": 5, "CEG": 1, "SGOV": 11}) as c:
        pid, env = c.ready(["ANET"])
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(positions={"ANET": 5, "CEG": 1, "SGOV": 11}))), env)
        f = out["legs"][0]
        r.eq("T11: the leg is a checked sell", (f["side"], c.leg(f["ref_id"])["state"]), ("sell", "checked"))
        c.clk.advance(1)
        p = X.review_payload(f)
        p["tool_input"]["dollar_amount"] = "100.00"
        res = broker_guard.record_review(p, env=env, now=c.clk.now)
        r.eq("T11: a sell review with quantity and dollar_amount matches no leg (kills bg_rr_sell_dollar)",
             (res, c.leg(f["ref_id"])["review_ok"]), ((0, "no checked leg matches this review"), 0))
        res = broker_guard.record_review(X.review_payload(f), env=env, now=c.clk.now)
        r.eq("T11 control: the clean sell answer -> review_ok 1", (res, c.leg(f["ref_id"])["review_ok"]),
             ((1, "clean review"), 1))


# =============================================================================================
# T12: the snapshot job audit, and the recovery re-send close margin
# =============================================================================================

def t_snapshot_audit():
    # Kills rb_audit_snapshot (run_broker.py run_snapshot: self.audit(out)).
    with X.case("mut snapshot audit") as c:
        built = c.build(["ANET"])
        leg = c.legs(built["proposal_id"])[0]
        fields = execution.leg_fields(c.cfg, leg)
        s = X.Stream()
        s.call(PLACE, fields, json.dumps(X.order_from(fields, oid="snap-x1")), False)
        with c.led.connect() as conn:
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
        runner = X.Runner(c, opts={"snap": std(), "extra_stdout": s.text()})
        c.broker(runner).drain()
        r.eq("T12 audit: the snapshot job ran", [j["values"].get("proposal_id") for j in runner.jobs], [None])
        r.eq("T12 audit: a place call in a snapshot stream halts the day", c.led.is_halted(DAY), True)
        r.ok("T12 audit: one critical broker page", [k for k in c.pages.kinds() if k == ("error", "broker")] ==
             [("error", "broker")], c.pages.kinds())


def t_resend_close_margin():
    # Kills x_resend_session (execution.py _resend_allowed: RESEND_CLOSE_MARGIN).
    with X.case("mut resend close margin") as c:
        pid, fields, b = X._lost(c)
        before = c.leg(fields["ref_id"])
        job = c.q("SELECT id FROM broker_jobs WHERE proposal_id=? AND recovery=1", (pid,))[0]["id"]
        c.at("2026-08-05T19:59:00Z")                     # 60 s before the 16:00 ET close
        r.ok("T12 resend: the proposal has not expired yet", store.parse_ts(c.prop(pid)["expires_at"]) > c.clk.now,
             c.prop(pid)["expires_at"])
        b.mark(job, "running")
        code, prep = c.tick("exec-prepare", "--proposal", pid, "--recovery")
        env = {"QUIVER_BROKER_JOB": str(job), "QUIVER_BROKER_PROPOSAL": str(pid)}
        out = c.check(pid, "recover", c.write("rc.json", c.snapfile(**std(), orders=[])), env)
        leg = c.leg(fields["ref_id"])
        r.eq("T12 resend: 60 s before the close -> no re-send", (out.get("action"), out.get("legs")), ("none", []))
        r.eq("T12 resend: the leg stays placing, one attempt, no new check",
             (leg["state"], leg["reconcile_attempts"], leg["checked_at"]), ("placing", 1, before["checked_at"]))


# =============================================================================================
# T1: the bridge takes an owner message only from a PRIVATE chat (chat_bridge.py accepted())
# =============================================================================================

def t_chat_private_only():
    # Kills cb_private (chat_bridge.py accepted(): chat.type == "private"). The other three
    # rules of T1 (owner id, chat.id == from.id, the offline tap) die in test_chat_bridge.py.
    import asyncio
    import test_chat_bridge as CB
    try:
        with CB.world() as w:
            async def go():
                CB.msg_update(w, 5101, "hello desk")
                for i, kind in enumerate(("group", "supergroup", "channel")):
                    # The owner's own id as the chat id, but the chat type is not private.
                    CB.msg_update(w, 5102 + i, f"sent in a {kind}", chat_type=kind)
                await w.bridge.poll_once()
            asyncio.run(go())
            got = [(o["tg_message_id"], o["text"]) for o in CB.owner_rows(w)]
            r.eq("T1: a group, supergroup or channel message is ignored, even with chat.id == the owner id",
                 got, [(5101, "hello desk")])
    except Exception:  # noqa: BLE001
        r.ok("T1: ran without an exception", False, traceback.format_exc()[-3000:])


CASES = [t_chat_private_only, t_foreign_ref, t_two_buys_one_budget, t_exit_full, t_side_rule, t_guard_binding, t_halt_kill_phases,
         t_review_rules, t_snapshot_audit, t_resend_close_margin]

if __name__ == "__main__":
    for fn in CASES:
        try:
            fn()
        except Exception:  # noqa: BLE001 — a crash is one failed case, the others still run
            r.ok(f"{fn.__name__}: runs without an exception", False, traceback.format_exc()[-3000:])
    raise SystemExit(r.done())
