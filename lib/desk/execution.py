"""Execution: exec-prepare, exec-check, exec-commit, fills, snapshot-prepare and
snapshot-commit (plan §11). ``tick.py`` exposes each as a subcommand; ``run_broker.py``
and the broker runbooks (EXECUTE.md, SNAPSHOT.md) call them.

The broker model never reshapes data. Each JSON file it writes is an object keyed by tool
name whose values are the raw tool results, unchanged (a list in call order when it called
a tool more than once). Python parses them here. A raw result may arrive as a parsed
object, a JSON string, or a list of MCP content blocks whose text is one JSON string.

The price check is structural: a leg is placeable only in state ``checked`` (or
``placing`` for a recovery re-send) with a fresh ``checked_at``, and only ``exec-check``
writes that state, after it priced the leg from a quote less than 120 s old.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Optional

from lib import market, sizing
from lib.config import Config
from lib.desk import proposals, quotes, settings as desk_settings, store

INPUT_MAX_AGE_S = 60.0
DECIDED_MAX_AGE = timedelta(minutes=10)
CLOSE_MARGIN = timedelta(minutes=5)
RESEND_CLOSE_MARGIN = timedelta(seconds=120)
RECONCILE_WINDOW = timedelta(minutes=2)
APPROVAL_OFF_TEXT = ("Robinhood trade approvals are on for this account. Turn them off in the "
                     "Robinhood app (Agent Settings → Safety Controls), then tap Approve again.")
_FIELD_NAMES = ("quantity", "dollar_amount", "symbol", "side", "type", "account_number",
                "ref_id", "market_hours", "time_in_force", "limit_price", "stop_price")


class ExecError(ValueError):
    """A refused execution call. The text is safe to show the owner."""


# --- raw tool results ----------------------------------------------------------------

def unwrap(raw):
    """A raw tool result as data: parse a JSON string, join MCP text blocks, drop an
    outer ``content`` wrapper. Anything else comes back as given."""
    if isinstance(raw, dict) and isinstance(raw.get("content"), list) and "data" not in raw:
        raw = raw["content"]
    if isinstance(raw, list) and raw and all(isinstance(b, dict) and b.get("type") == "text" for b in raw):
        raw = "".join(str(b.get("text") or "") for b in raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        s = raw.strip()
        try:
            return json.loads(s)
        except ValueError:
            return s
    return raw


def data_of(raw):
    obj = unwrap(raw)
    if isinstance(obj, dict) and isinstance(obj.get("data"), (dict, list)):
        return obj["data"]
    return obj


def _calls(file_obj: dict, tool: str) -> list:
    """Every raw result for ``tool`` in the file, in call order."""
    if not isinstance(file_obj, dict):
        return []
    v = None
    for key in (tool, f"mcp__robinhood-trading__{tool}"):
        if key in file_obj:
            v = file_obj[key]
            break
    if v is None:
        return []
    if isinstance(v, list) and not (v and all(isinstance(b, dict) and b.get("type") == "text" for b in v)):
        return v
    return [v]


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def parse_portfolio(file_obj: dict) -> Optional[dict]:
    calls = _calls(file_obj, "get_portfolio")
    if not calls:
        return None
    d = data_of(calls[-1])
    if not isinstance(d, dict):
        return None
    bp = d.get("buying_power")
    if isinstance(bp, dict):
        bp = bp.get("buying_power")
    equity = _num(d.get("total_value"))
    bp = _num(bp)
    if equity is None or bp is None:
        return None
    return {"equity": equity, "cash": _num(d.get("cash")), "buying_power": bp}


def parse_positions(file_obj: dict) -> Optional[dict]:
    calls = _calls(file_obj, "get_equity_positions")
    if not calls:
        return None
    out = {}
    for c in calls:
        d = data_of(c)
        if not isinstance(d, dict) or not isinstance(d.get("positions"), list):
            return None
        for p in d["positions"]:
            sym = str(p.get("symbol") or "").upper()
            qty = _num(p.get("quantity"))
            if not sym or qty is None:
                continue
            avail = _num(p.get("shares_available_for_sells"))
            held = _num(p.get("shares_held_for_sells")) or 0.0
            out[sym] = {"quantity": qty, "available": avail if avail is not None else max(0.0, qty - held),
                        "average_buy_price": _num(p.get("average_buy_price"))}
    return out


def parse_quotes(file_obj: dict) -> dict:
    out = {}
    for c in _calls(file_obj, "get_equity_quotes"):
        out.update(quotes.quote_objects(unwrap(c)))
    return out


def parse_approval(file_obj: dict) -> str:
    """``off``, ``on`` or ``unknown``. Only a known field with a known value counts; an
    unknown shape counts as on (``unknown`` fails every gate)."""
    calls = _calls(file_obj, "get_trade_approval_setting")
    if not calls:
        return "unknown"
    d = data_of(calls[-1])
    if isinstance(d, dict):
        s = d.get("setting")
        if isinstance(s, dict) and isinstance(s.get("human_must_approve_trades"), bool):
            return "on" if s["human_must_approve_trades"] else "off"
    return "unknown"


def parse_orders(file_obj: dict) -> Optional[list]:
    calls = _calls(file_obj, "get_equity_orders")
    if not calls:
        return None
    out = []
    for c in calls:
        d = data_of(c)
        if isinstance(d, dict) and isinstance(d.get("orders"), list):
            out.extend(o for o in d["orders"] if isinstance(o, dict))
        elif isinstance(d, list):
            out.extend(o for o in d if isinstance(o, dict))
        else:
            return None
    return out


# --- shared checks -------------------------------------------------------------------

def read_input(path: str, *, root: Path, now_dt, max_age_s: Optional[float] = INPUT_MAX_AGE_S) -> dict:
    """Read a runbook file. Its realpath must be inside ``state/tmp`` (G13), and with
    ``max_age_s`` the file must be at most that old by its modification time."""
    tmp = os.path.realpath(root / "state" / "tmp")
    real = os.path.realpath(path if os.path.isabs(path) else root / path)
    if not (real == tmp or real.startswith(tmp + os.sep)):
        raise ExecError("--input must be a file in state/tmp")
    if not os.path.isfile(real):
        raise ExecError(f"input file {path} does not exist")
    if max_age_s is not None:
        age = now_dt.timestamp() - os.path.getmtime(real)
        if age > max_age_s:
            raise ExecError(f"the input file is {int(age)} s old (limit {int(max_age_s)} s)")
    with open(real, encoding="utf-8") as f:
        return json.load(f)


def _proposal_env() -> Optional[int]:
    v = os.environ.get("QUIVER_BROKER_PROPOSAL", "").strip()
    return int(v) if v.isdigit() else None


def _job_env() -> Optional[int]:
    v = os.environ.get("QUIVER_BROKER_JOB", "").strip()
    return int(v) if v.isdigit() else None


def _set_reason(conn, pid: int, text: str, code: str) -> None:
    prop = proposals.get_proposal(conn, pid)
    res = store.loads(prop.get("result_json"), {}) or {}
    msgs = list(res.get("messages") or [])
    msgs.append(text)
    res.update(reason_code=code, reason_text=text, messages=msgs)
    conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), pid))


def _add_message(conn, pid: int, text: str) -> None:
    prop = proposals.get_proposal(conn, pid)
    res = store.loads(prop.get("result_json"), {}) or {}
    res["messages"] = list(res.get("messages") or []) + [text]
    conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), pid))


def _fail_proposal(conn, pid: int, code: str, text: str) -> None:
    """Set an approved/executing proposal ``failed`` and skip every never-sent leg. A
    proposal with a leg in ``placing`` is never failed (§11.1 step 4)."""
    placing = conn.execute("SELECT 1 FROM orders WHERE proposal_id=? AND state IN "
                           "('placing','placed','partially_filled')", (pid,)).fetchone()
    _set_reason(conn, pid, text, code)
    if placing:
        return
    conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason='not_placed' WHERE proposal_id=? "
                 "AND state IN ('proposed','approved','checked')", (pid,))
    conn.execute("UPDATE proposals SET state='failed' WHERE id=? AND state IN ('approved','executing')",
                 (pid,))


def _fmt_qty(q: float) -> str:
    return f"{float(q):.6f}"


def _fmt_dollars(d: float) -> str:
    return f"{float(d):.2f}"


def leg_fields(cfg: Config, leg: dict) -> dict:
    """The exact fields of the review and place calls for one leg."""
    out = {"ref_id": leg["ref_id"], "account_number": cfg.account_number, "symbol": leg["ticker"],
           "side": leg["side"], "type": "market", "market_hours": cfg.market_hours,
           "time_in_force": cfg.time_in_force}
    if leg["side"] == "sell":
        out["quantity"] = _fmt_qty(leg["quantity"])
    else:
        out["dollar_amount"] = _fmt_dollars(leg["dollar_amount"])
    return out


def _phase_key(job_id) -> str:
    return f"exec_phase:{job_id}"


def _redact(obj, secret: str):
    """``obj`` with every string that holds ``secret`` replaced. No tool returns a secret
    (§5.2), and ``ledger_query`` can read every column."""
    if not secret:
        return obj
    if isinstance(obj, dict):
        return {k: _redact(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v, secret) for v in obj]
    if isinstance(obj, str) and secret in obj:
        return obj.replace(secret, "[account]")
    return obj


def _store_snapshot(conn, *, now: str, portfolio_d: dict, positions: dict, qts: dict,
                    approval: str, raw: dict, account_number: str = "") -> int:
    raw = _redact(raw, str(account_number or ""))
    pos = {}
    for sym, p in positions.items():
        q = qts.get(sym)
        last = _num((q or {}).get("last_trade_price"))
        pos[sym] = {**p, "market_value": round(p["quantity"] * last, 6) if last else None}
    cur = conn.execute(
        "INSERT INTO broker_snapshots (captured_at, equity, cash, buying_power, positions_json, "
        "quotes_json, approval_setting, raw_json) VALUES (?,?,?,?,?,?,?,?)",
        (now, portfolio_d["equity"], portfolio_d.get("cash"), portfolio_d["buying_power"],
         json.dumps(pos, sort_keys=True), json.dumps(qts, sort_keys=True), approval,
         json.dumps(raw)))
    return int(cur.lastrowid)


def _daily_loss(led, conn, sett: dict, equity: float, now: str) -> tuple:
    """(halted_now, drop_pct). The first snapshot of the day sets the baseline."""
    day = market.trading_day_et()
    row = conn.execute("SELECT baseline_equity, halted FROM day_baseline WHERE trade_date=?", (day,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO day_baseline (trade_date, baseline_equity, captured_at) VALUES (?,?,?)",
                     (day, equity, now))
        return False, 0.0
    base, halted = float(row[0]), bool(row[1])
    drop = (base - equity) / base * 100.0 if base > 0 else 0.0
    if not halted and round(drop, 9) >= float(sett["daily_loss_halt_pct"]):
        conn.execute("UPDATE day_baseline SET halted=1, halt_reason=? WHERE trade_date=?",
                     (f"daily loss {drop:.2f}% >= {sett['daily_loss_halt_pct']}% (equity {equity:.2f} "
                      f"vs baseline {base:.2f})", day))
        return True, drop
    return False, drop


# --- exec-prepare (§11.1 step 5) -------------------------------------------------------

def exec_prepare(led, cfg: Config, proposal_id: int, *, recovery: bool = False, now_dt=None) -> dict:
    now_dt = now_dt or store.utc_now()
    now = store.iso(now_dt)
    with led.transaction() as conn:
        prop = proposals.get_proposal(conn, proposal_id)
        if prop is None or prop["kind"] != "trade":
            raise ExecError(f"proposal {proposal_id} is not a trade proposal")
        if recovery:
            # A reconcile is never gated: the guard's own checks gate any re-send.
            if prop["state"] not in ("approved", "executing"):
                raise ExecError(f"proposal #{proposal_id} is {prop['state']}")
            conn.execute("UPDATE proposals SET state='executing' WHERE id=? AND state='approved'", (proposal_id,))
            legs = [l for l in led.legs(proposal_id, conn=conn) if l["state"] == "placing"]
            sub = [store.parse_ts(l["submitted_at"]) for l in legs if l.get("submitted_at")]
            gte = store.iso(min(sub) - RECONCILE_WINDOW) if sub else store.iso(now_dt - timedelta(days=1))
            return {"ok": True, "proposal": proposal_id, "recovery": True,
                    "account_number": cfg.account_number, "created_at_gte": gte,
                    "legs": [{"ref_id": l["ref_id"], "symbol": l["ticker"], "side": l["side"]} for l in legs]}
        if prop["state"] != "approved":
            raise ExecError(f"proposal #{proposal_id} is {prop['state']}, not approved")
        sett = desk_settings.effective(conn, cfg)
        code = text = None
        expires = store.parse_ts(prop["expires_at"])
        decided = store.parse_ts(prop["decided_at"])
        now_et = now_dt.astimezone(market.ET)
        _d, open_t, close_t = market.session_open_et(now_et)
        snap = proposals.latest_snapshot(conn)
        if now_dt >= expires:
            code, text = "expired", f"Proposal #{proposal_id} expired before I could start it, so I placed nothing."
        elif decided is None or now_dt - decided >= DECIDED_MAX_AGE:
            code, text = "tap_too_old", (f"I could not start #{proposal_id} within 10 minutes of your tap, "
                                         "so I placed nothing. Ask me for fresh numbers.")
        elif not (open_t <= now_et < close_t) or close_t - now_et < CLOSE_MARGIN:
            code, text = "session", f"The market is closed or about to close, so I placed nothing for #{proposal_id}."
        elif proposals.kill_active(cfg):
            code, text = "kill", f"Trading is stopped on the box, so I placed nothing for #{proposal_id}."
        elif led.is_halted(market.trading_day_et(), conn=conn):
            code, text = "halted", f"Trading is halted for today, so I placed nothing for #{proposal_id}."
        elif snap is None or snap.get("approval_setting") != "off":
            code, text = "approval_setting", APPROVAL_OFF_TEXT
        if code:
            _fail_proposal(conn, proposal_id, code, text)
            return {"ok": False, "proposal": proposal_id, "reason": code, "text": text}
        conn.execute("UPDATE proposals SET state='executing' WHERE id=? AND state='approved'", (proposal_id,))
        session_day = now_et.strftime("%Y-%m-%d")
        conn.execute("UPDATE orders SET trade_date=? WHERE proposal_id=? AND state='approved'",
                     (session_day, proposal_id))
        legs = led.legs(proposal_id, conn=conn)
        return {"ok": True, "proposal": proposal_id, "recovery": False, "account_number": cfg.account_number,
                "legs": [{"ref_id": l["ref_id"], "symbol": l["ticker"], "side": l["side"],
                          "dollar_amount": l["dollar_amount"], "quantity": l["quantity"]} for l in legs],
                "requote_tolerance_pct": sett["requote_tolerance_pct"]}


# --- exec-check (§11.2 steps 2 and 5, recovery step 2) ----------------------------------

def exec_check(led, cfg: Config, proposal_id: int, phase: str, file_obj: dict, *, now_dt=None,
               file_error: Optional[str] = None) -> dict:
    """Check the legs of one phase against fresh broker data. Returns the legs to place
    (``action: place``), ``action: requote``, ``action: none`` or ``action: refused``.
    ``file_error`` is set when the input failed the age or path rule."""
    now_dt = now_dt or store.utc_now()
    now = store.iso(now_dt)
    env_pid = _proposal_env()
    if env_pid is None or env_pid != int(proposal_id):
        raise ExecError("exec-check acts only on the proposal of this execute job")
    job_id = _job_env()
    with led.transaction() as conn:
        prop = proposals.get_proposal(conn, proposal_id)
        if prop is None or prop["state"] != "executing":
            return {"action": "refused", "reason": f"proposal is {prop['state'] if prop else 'missing'}"}
        job = conn.execute("SELECT recovery FROM broker_jobs WHERE id=? AND proposal_id=? AND status='running'",
                           (job_id, proposal_id)).fetchone() if job_id is not None else None
        if job is None:
            return {"action": "refused", "reason": "no running execute job for this proposal"}
        done = set(json.loads(store.get_state(conn, _phase_key(job_id), "[]")))
        legs = led.legs(proposal_id, conn=conn)
        if phase == "recover":
            if not job[0] or "recover" in done:
                return {"action": "refused", "reason": "recover runs once, in a recovery job"}
            done.add("recover")
            store.set_state(conn, _phase_key(job_id), json.dumps(sorted(done)))
            return _check_recover(led, conn, cfg, prop, legs, file_obj, now_dt, file_error)
        if phase == "sells":
            if "sells" in done or any(l["state"] != "approved" for l in legs):
                return {"action": "refused", "reason": "sells runs once, when every leg is approved"}
            done.add("sells")
            store.set_state(conn, _phase_key(job_id), json.dumps(sorted(done)))
            return _check_sells(led, conn, cfg, prop, legs, file_obj, now_dt, file_error)
        if phase == "buys":
            if "sells" not in done or "buys" in done or any(
                    l["side"] == "sell" and l["state"] in ("approved", "checked", "placing") for l in legs):
                return {"action": "refused", "reason": "buys runs once, after the sells are placed"}
            done.add("buys")
            store.set_state(conn, _phase_key(job_id), json.dumps(sorted(done)))
            return _check_buys(led, conn, cfg, prop, legs, file_obj, now_dt, file_error)
    raise ExecError(f"unknown phase {phase}")


def _fresh_inputs(file_obj):
    return (parse_portfolio(file_obj), parse_positions(file_obj), parse_quotes(file_obj),
            parse_approval(file_obj))


def _check_sells(led, conn, cfg, prop, legs, file_obj, now_dt, file_error) -> dict:
    pid = prop["id"]
    now = store.iso(now_dt)
    sett = desk_settings.effective(conn, cfg)
    if file_error:
        _fail_proposal(conn, pid, "input", f"I could not check #{pid} ({file_error}), so I placed nothing.")
        return {"action": "none", "reason": file_error}
    pf, pos, qts, approval = _fresh_inputs(file_obj)
    if pf is None or pos is None:
        _fail_proposal(conn, pid, "input", f"I could not read the Robinhood portfolio for #{pid}, so I placed nothing.")
        return {"action": "none", "reason": "portfolio or positions missing"}
    snap_id = _store_snapshot(conn, now=now, portfolio_d=pf, positions=pos, qts=qts,
                              approval=approval, raw=file_obj,
                              account_number=cfg.account_number)
    prices = {}
    for l in legs:
        price, why = quotes.leg_price(qts.get(l["ticker"]), now_dt)
        if price is None:
            _fail_proposal(conn, pid, "quote", f"I could not get a fresh {l['ticker']} price, so I placed nothing.")
            return {"action": "none", "reason": f"{l['ticker']}: {why}"}
        prices[l["ref_id"]] = price
    if approval != "off":
        _fail_proposal(conn, pid, "approval_setting", APPROVAL_OFF_TEXT)
        return {"action": "none", "reason": f"approval setting {approval}"}
    halted_now, drop = _daily_loss(led, conn, sett, pf["equity"], now)
    if halted_now or led.is_halted(market.trading_day_et(), conn=conn):
        _fail_proposal(conn, pid, "halted", f"The daily-loss check stopped trading for today, so I placed nothing for #{pid}.")
        return {"action": "none", "reason": "halted", "halted_now": halted_now}
    if proposals.kill_active(cfg):
        _fail_proposal(conn, pid, "kill", f"Trading is stopped on the box, so I placed nothing for #{pid}.")
        return {"action": "none", "reason": "kill"}
    tol = float(sett["requote_tolerance_pct"])
    moves = []
    for l in legs:
        old = float(l["quote_at_proposal"] or 0.0)
        new = prices[l["ref_id"]]
        if old > 0 and quotes.moved_pct(new, old) > tol:
            moves.append({"ticker": l["ticker"], "side": l["side"], "old": old, "new": new,
                          "pct": round((new / old - 1.0) * 100.0, 4)})
    if moves:
        proposals.requote_move(conn, pid)
        res = store.loads(proposals.get_proposal(conn, pid).get("result_json"), {}) or {}
        res["requote"] = {"phase": "sells", "moves": moves, "snapshot_id": snap_id,
                          "tickers": [l["ticker"] for l in legs]}
        conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), pid))
        return {"action": "requote", "moves": moves}
    # Re-size each sell down, never up.
    goal = proposals.active_goal(conn)
    rows = {r["ticker"]: r for r in proposals.book_rows(conn, goal["id"], ("active", "exiting"))} if goal else {}
    snap = proposals.snapshot_view(dict(conn.execute("SELECT * FROM broker_snapshots WHERE id=?", (snap_id,)).fetchone()))
    base = proposals.valuation_base(conn, snap, now_dt, None, snap)
    deployable = base["deployable"]
    min_notional = max(float(sett["min_trade_notional"]), sizing.MIN_SELL_NOTIONAL_USD)
    out = []
    for l in legs:
        if l["side"] != "sell":
            continue
        price = prices[l["ref_id"]]
        p = pos.get(l["ticker"]) or {}
        held = float(p.get("quantity") or 0.0)
        free = float(p.get("available") if p.get("available") is not None else held)
        row = rows.get(l["ticker"])
        exit_leg = row is not None and row["status"] == "exiting"
        if exit_leg:
            target_qty = held
        else:
            T = float((row or {}).get("target_weight") or 0.0) / 100.0 * deployable
            target_qty = sizing.resolve_target_sell_quantity(held, price, held * price, T)
        qty = round(min(float(l["quantity"] or 0.0), target_qty, free), 6)
        full_exit = exit_leg and qty >= round(held, 6) - 1e-9
        if qty <= 0 or (not full_exit and qty * price < min_notional):
            conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason=? WHERE ref_id=? "
                         "AND state='approved'", ("below_min" if qty > 0 else "nothing_to_sell", l["ref_id"]))
            continue
        if exit_leg and qty * price < sizing.MIN_SELL_NOTIONAL_USD:
            conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason='dust' WHERE ref_id=? "
                         "AND state='approved'", (l["ref_id"],))
            continue
        conn.execute("UPDATE orders SET state='checked', quantity=?, check_quote=?, checked_at=?, "
                     "review_ok=0, review_json=NULL WHERE ref_id=? AND state='approved'",
                     (qty, price, now, l["ref_id"]))
        out.append(leg_fields(cfg, {**l, "quantity": qty}))
    return {"action": "place" if out else "none", "legs": out}


def _check_buys(led, conn, cfg, prop, legs, file_obj, now_dt, file_error) -> dict:
    pid = prop["id"]
    now = store.iso(now_dt)
    sett = desk_settings.effective(conn, cfg)
    buy_legs = [l for l in legs if l["side"] == "buy" and l["state"] == "approved"]
    if not buy_legs:
        return {"action": "none", "legs": []}

    def skip_all(rule: str) -> dict:
        for l in buy_legs:
            conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason=? WHERE ref_id=? "
                         "AND state='approved'", (rule, l["ref_id"]))
        _add_message(conn, pid, f"I did not send the buys of #{pid}: {rule.replace('_', ' ')}.")
        return {"action": "none", "reason": rule}

    if file_error:
        return skip_all("stale_input")
    pf, pos, qts, approval = _fresh_inputs(file_obj)
    if pf is None or pos is None:
        return skip_all("no_portfolio")
    snap_id = _store_snapshot(conn, now=now, portfolio_d=pf, positions=pos, qts=qts,
                              approval=approval, raw=file_obj,
                              account_number=cfg.account_number)
    if approval != "off":
        return skip_all("approval_setting")
    halted_now, _drop = _daily_loss(led, conn, sett, pf["equity"], now)
    if halted_now or led.is_halted(market.trading_day_et(), conn=conn):
        out = skip_all("halted")
        out["halted_now"] = halted_now
        return out
    if proposals.kill_active(cfg):
        return skip_all("kill")
    tol = float(sett["requote_tolerance_pct"])
    goal = proposals.active_goal(conn)
    rows = {r["ticker"]: r for r in proposals.book_rows(conn, goal["id"], ("active", "exiting", "removed"))}
    snap = proposals.snapshot_view(dict(conn.execute("SELECT * FROM broker_snapshots WHERE id=?", (snap_id,)).fetchone()))
    base = proposals.valuation_base(conn, snap, now_dt, None, snap)
    deployable = base["deployable"]
    bp = pf["buying_power"]
    cash_t = cfg.risk.cash_sleeve_ticker
    floor_pct = desk_settings.cash_floor_pct(sett, list(rows.values()), cash_t)
    money = min(bp - float(sett["min_buying_power_buffer"]),
                bp + base["values"].get(cash_t, 0.0) - floor_pct / 100.0 * deployable)
    min_notional = max(float(sett["min_trade_notional"]), sizing.MIN_SELL_NOTIONAL_USD)
    out, moves = [], []
    for l in buy_legs:
        price, why = quotes.leg_price(qts.get(l["ticker"]), now_dt)
        if price is None:
            conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason=? WHERE ref_id=? "
                         "AND state='approved'", (f"quote_{why}", l["ref_id"]))
            _add_message(conn, pid, f"I could not get a fresh {l['ticker']} price, so I did not buy it.")
            continue
        old = float(l["quote_at_proposal"] or 0.0)
        if old > 0 and quotes.moved_pct(price, old) > tol:
            conn.execute("UPDATE orders SET state='requoted', finalized=1 WHERE ref_id=? AND state='approved'",
                         (l["ref_id"],))
            moves.append({"ticker": l["ticker"], "side": "buy", "old": old, "new": price,
                          "pct": round((price / old - 1.0) * 100.0, 4)})
            continue
        row = rows.get(l["ticker"]) or {}
        T = float(row.get("target_weight") or 0.0) / 100.0 * deployable
        v = base["values"].get(l["ticker"], 0.0)
        room = max(0.0, T - v)
        want = float(l["dollar_amount"] or 0.0)
        cap = max(0.0, min(room, money))
        dollars, _src = sizing.resolve_buy_dollars(want, 1.0, available=cap, min_notional=min_notional)
        if dollars <= 0:
            conn.execute("UPDATE orders SET state='skipped', finalized=1, skip_reason='below_min' WHERE ref_id=? "
                         "AND state='approved'", (l["ref_id"],))
            continue
        clamped_from = want if dollars < round(want, 2) else None
        money -= dollars
        conn.execute("UPDATE orders SET state='checked', dollar_amount=?, clamped_from=?, check_quote=?, "
                     "checked_at=?, review_ok=0, review_json=NULL WHERE ref_id=? AND state='approved'",
                     (dollars, clamped_from, price, now, l["ref_id"]))
        out.append(leg_fields(cfg, {**l, "dollar_amount": dollars}))
    if moves:
        res = store.loads(proposals.get_proposal(conn, pid).get("result_json"), {}) or {}
        res["requote"] = {"phase": "buys", "moves": moves, "snapshot_id": snap_id,
                          "tickers": [m["ticker"] for m in moves]}
        conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), pid))
    return {"action": "place" if out else "none", "legs": out, "requoted": moves}


def _fill_leg_state(broker_state: str) -> str:
    s = str(broker_state or "").lower()
    if s == "filled":
        return "filled"
    if s == "partially_filled":
        return "partially_filled"
    if s in ("cancelled", "voided"):
        return "cancelled"
    if s in ("rejected", "failed"):
        return "broker_rejected"
    return "placed"


def _amount_matches(leg: dict, o: dict) -> bool:
    if leg["side"] == "buy":
        dba = o.get("dollar_based_amount")
        amt = _num((dba or {}).get("amount")) if isinstance(dba, dict) else None
        return amt is not None and abs(amt - float(leg["dollar_amount"] or 0.0)) < 0.005
    qty = _num(o.get("quantity"))
    return qty is not None and abs(qty - float(leg["quantity"] or 0.0)) < 5e-7


def match_order(leg: dict, orders: list, taken: set) -> Optional[dict]:
    """The broker order of a ``placing`` leg: symbol, side, ``agent_initiated``, the same
    amount, and ``created_at`` at or after the leg's ``submitted_at`` minus 2 minutes. An
    order id another leg holds is skipped. The order's own ``ref_id`` is Robinhood's, not
    ours (production 2026-10-22: leg 81e748c5 came back as 01a16428), so an equal ``ref_id``
    is a match when it occurs and a different one proves nothing."""
    for o in orders:
        if o.get("id") in taken:
            continue
        if o.get("ref_id") and str(o.get("ref_id")) == leg["ref_id"]:
            return o
    sub = store.parse_ts(leg.get("submitted_at"))
    for o in orders:
        if o.get("id") in taken:
            continue
        if str(o.get("symbol", "")).upper() != leg["ticker"] or o.get("side") != leg["side"]:
            continue
        if o.get("agent_initiated") is not True:
            continue
        created = quotes.parse_venue_time(o.get("created_at"))
        if sub is not None and (created is None or created < sub - RECONCILE_WINDOW):
            continue
        if _amount_matches(leg, o):
            return o
    return None


def _record_match(led, conn, leg: dict, o: dict, now: str) -> str:
    state = _fill_leg_state(o.get("state"))
    f = parse_fill(o)
    conn.execute("UPDATE orders SET broker_order_id=?, result_json=?, state=?, finalized=1, "
                 "fill_avg_price=?, fill_quantity=?, fill_fees=?, fill_state=?, fill_captured_at=?, "
                 "reconcile_attempts=0 WHERE ref_id=?", (o.get("id"), json.dumps(o), state, f["avg_price"], f["quantity"],
                                   f["fees"], f["fill_state"], now, leg["ref_id"]))
    return state


def parse_fill(o: dict) -> dict:
    avg = _num(o.get("average_price"))
    qty = _num(o.get("cumulative_quantity"))
    if qty is None:
        qty = _num(o.get("quantity"))
    fees = _num(o.get("fees"))
    return {"avg_price": avg if (avg and avg > 0) else None, "quantity": qty if (qty and qty > 0) else None,
            "fees": fees if fees is not None else 0.0, "fill_state": str(o.get("state") or "")}


def _resend_allowed(led, conn, cfg, prop, now_dt) -> Optional[str]:
    """None when a recovery re-send may go out, else the rule that blocks it."""
    if now_dt >= store.parse_ts(prop["expires_at"]):
        return "expired"
    now_et = now_dt.astimezone(market.ET)
    _d, open_t, close_t = market.session_open_et(now_et)
    if not (open_t <= now_et < close_t) or close_t - now_et <= RESEND_CLOSE_MARGIN:
        return "session"
    if proposals.kill_active(cfg):
        return "kill"
    if led.is_halted(market.trading_day_et(), conn=conn):
        return "halted"
    return None


def _position_moved(conn, leg: dict, pos: Optional[dict]) -> Optional[str]:
    """None when the live position shows the leg did NOT execute, else the reason to block a
    re-send. The base is the newest snapshot at or before the leg's ``submitted_at``
    (exec-check writes one just before the place). With no live positions or no base, the
    re-send is blocked: Robinhood's de-duplication of a re-sent ref_id is not proven."""
    if pos is None:
        return "no_positions"
    row = conn.execute("SELECT positions_json FROM broker_snapshots WHERE captured_at <= ? "
                       "ORDER BY captured_at DESC, id DESC LIMIT 1", (leg.get("submitted_at") or "",)).fetchone()
    if row is None:
        return "no_base_snapshot"
    base = store.loads(row[0], {}) or {}
    t = leg["ticker"]
    before = float((base.get(t) or {}).get("quantity") or 0.0)
    after = float((pos.get(t) or {}).get("quantity") or 0.0)
    if leg["side"] == "buy" and after - before > 1e-6:
        return "position_rose"
    if leg["side"] == "sell" and before - after >= float(leg.get("quantity") or 0.0) - 5e-7 and before - after > 1e-9:
        return "position_fell"
    return None


def _check_recover(led, conn, cfg, prop, legs, file_obj, now_dt, file_error) -> dict:
    pid = prop["id"]
    now = store.iso(now_dt)
    sett = desk_settings.effective(conn, cfg)
    placing = [l for l in legs if l["state"] == "placing"]
    orders = parse_orders(file_obj) if not file_error else None
    if orders is None:
        return {"action": "none", "reason": file_error or "no order list in the input"}
    taken = {r[0] for r in conn.execute("SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL")}
    qts = parse_quotes(file_obj)
    approval = parse_approval(file_obj)
    live_pos = parse_positions(file_obj)
    resend = []
    for l in placing:
        o = match_order(l, orders, taken)
        if o is not None:
            taken.add(o.get("id"))
            _record_match(led, conn, l, o, now)
            continue
        attempts = int(l.get("reconcile_attempts") or 0)
        block = _resend_allowed(led, conn, cfg, prop, now_dt) or _position_moved(conn, l, live_pos)
        if block is None and attempts == 0 and approval == "off":
            price, _why = quotes.leg_price(qts.get(l["ticker"]), now_dt)
            old = float(l["quote_at_proposal"] or 0.0)
            if price is None or (old > 0 and quotes.moved_pct(price, old) > float(sett["requote_tolerance_pct"])):
                conn.execute("UPDATE orders SET state='outcome_unknown', finalized=1, reconcile_attempts=? "
                             "WHERE ref_id=? AND state='placing'", (attempts + 1, l["ref_id"]))
                _add_message(conn, pid, f"I can't confirm the {l['ticker']} order, and the price moved, so I did "
                                        "not send it again. Check the Robinhood app.")
                continue
            conn.execute("UPDATE orders SET reconcile_attempts=?, check_quote=?, checked_at=?, review_ok=0, "
                         "review_json=NULL WHERE ref_id=? AND state='placing'",
                         (attempts + 1, price, now, l["ref_id"]))
            resend.append(leg_fields(cfg, l))
            continue
        attempts += 1
        if attempts >= 2:
            conn.execute("UPDATE orders SET state='outcome_unknown', finalized=1, reconcile_attempts=? "
                         "WHERE ref_id=? AND state='placing'", (attempts, l["ref_id"]))
            _add_message(conn, pid, f"I can't confirm the {l['ticker']} order. Check the Robinhood app.")
        else:
            conn.execute("UPDATE orders SET reconcile_attempts=? WHERE ref_id=? AND state='placing'",
                         (attempts, l["ref_id"]))
    return {"action": "place" if resend else "none", "legs": resend}


# --- exec-commit (§11.2 step 3) --------------------------------------------------------

# A validation error starts with its status: "Error: 400 Bad Request: {...}". Only 400 and
# 422 count. 408, 409 and 429 stay unknown: Robinhood can hold an order after a timeout or
# a conflict. A number inside the text (a $412.00 amount) never counts.
_HTTP_REJECT = re.compile(r"^\s*(?:error:\s*)?(?:http\s*)?(400|422)\s+[A-Za-z]", re.I)
GUARD_DENY_TEXT = "broker_guard: denied:"


def _classify_place_result(res) -> tuple:
    """(kind, order) for one place result: ``order`` (an order object), ``rejected``
    (an order object in state rejected/failed, or a 400/422 validation error that names a
    field as a quoted JSON key), or ``unknown`` (any other body: the leg stays ``placing``)."""
    obj = data_of(res)
    if isinstance(obj, dict) and isinstance(obj.get("order"), dict):
        obj = obj["order"]
    if isinstance(obj, dict) and obj.get("id") and obj.get("state"):
        if str(obj["state"]).lower() in ("rejected", "failed"):
            return "rejected", obj
        return "order", obj
    if isinstance(obj, str):
        if _HTTP_REJECT.match(obj) and any(f'"{f}"' in obj for f in _FIELD_NAMES):
            return "rejected", None
    elif isinstance(obj, dict) and not obj.get("id"):
        st = obj.get("status", obj.get("status_code"))
        text = json.dumps(obj, default=str)
        if isinstance(st, int) and not isinstance(st, bool) and st in (400, 422) and \
                any(f'"{f}"' in text for f in _FIELD_NAMES):
            return "rejected", None
    return "unknown", None


def _is_guard_deny(res) -> bool:
    """A place call that the guard denied. The guard moved no leg to ``placing`` for it."""
    u = unwrap(res)
    text = u if isinstance(u, str) else json.dumps(u, default=str)
    return GUARD_DENY_TEXT in (text or "")


def exec_commit(led, cfg: Config, proposal_id: int, file_obj: dict, *, now_dt=None) -> dict:
    now_dt = now_dt or store.utc_now()
    now = store.iso(now_dt)
    if _proposal_env() != int(proposal_id):
        raise ExecError("exec-commit acts only on the proposal of this execute job")
    results = _calls(file_obj, "place_equity_order")
    out = []
    with led.transaction() as conn:
        prop = proposals.get_proposal(conn, proposal_id)
        if prop is None:
            raise ExecError("no such proposal")
        legs = [l for l in led.legs(proposal_id, conn=conn) if l["state"] == "placing"]
        used = set()
        bound, errors = [], []
        # Pass 1: an order object binds to its leg by ref_id, else to the one unused placing
        # leg with the same symbol and side. A guard deny moved no leg, so it binds nothing.
        for res in results:
            if _is_guard_deny(res):
                out.append({"result": "guard_denied"})
                continue
            kind, order = _classify_place_result(res)
            if order is None:
                errors.append((res, kind))
                continue
            leg = next((l for l in legs if l["ref_id"] not in used and order.get("ref_id") == l["ref_id"]), None)
            if leg is None:
                cands = [l for l in legs if l["ref_id"] not in used and l["ticker"] ==
                         str(order.get("symbol", "")).upper() and l["side"] == order.get("side")]
                leg = cands[0] if len(cands) == 1 else None
            if leg is None:
                out.append({"result": "unmatched"})
                continue
            used.add(leg["ref_id"])
            bound.append((res, kind, order, leg))
        # Pass 2: an error body names no order. It binds only when exactly one error body and
        # one placing leg are left. Else every leg stays placing and the reconcile decides.
        rest = [l for l in legs if l["ref_id"] not in used]
        if len(errors) == 1 and len(rest) == 1:
            bound.append((errors[0][0], errors[0][1], None, rest[0]))
        else:
            out.extend({"result": "unbound_error"} for _ in errors)
        for res, kind, order, leg in bound:
            if kind == "order":
                state = _record_match(led, conn, leg, order, now)
                _decision_row(conn, prop, leg, now)
                out.append({"ref_id": leg["ref_id"], "state": state})
            elif kind == "rejected":
                conn.execute("UPDATE orders SET state='broker_rejected', finalized=1, result_json=? WHERE ref_id=? "
                             "AND state='placing'", (json.dumps(unwrap(res)), leg["ref_id"]))
                out.append({"ref_id": leg["ref_id"], "state": "broker_rejected"})
            else:
                conn.execute("UPDATE orders SET result_json=? WHERE ref_id=? AND state='placing'",
                             (json.dumps(unwrap(res)) if not isinstance(unwrap(res), str) else json.dumps({"text": unwrap(res)}),
                              leg["ref_id"]))
                out.append({"ref_id": leg["ref_id"], "state": "placing"})
    return {"ok": True, "legs": out}


def _decision_row(conn, prop: dict, leg: dict, now: str) -> None:
    """One ``decisions`` row per placed leg (G25), so ``reflect`` and the track record
    score the desk's real trades."""
    exists = conn.execute("SELECT 1 FROM decisions WHERE run_id=?", (leg["ref_id"],)).fetchone()
    if exists:
        return
    proof = {"proposal_id": prop["id"], "event_id": prop.get("event_id"),
             "evidence": store.loads(prop.get("evidence_json"), [])}
    conn.execute("INSERT INTO decisions (trade_date, ticker, run_id, decided_at, signal, intent, "
                 "decision_price, rationale, proof_json) VALUES (?,?,?,?,?,?,?,?,?)",
                 (leg["trade_date"], leg["ticker"], leg["ref_id"], now, leg["side"], leg["side"],
                  leg.get("check_quote"), prop.get("rationale"), json.dumps(proof)))


# --- fills (§11.2 steps 4 and 6) -------------------------------------------------------

def fills_window(led) -> str:
    """``created_at_gte`` from the leg rows and the old pending orders: the oldest
    ``submitted_at`` of an order still missing its fill, minus one day. Never from a model
    file (with no window every pending order would read ``not_found``)."""
    pending = led.fills_pending()
    stamps = [store.parse_ts(r.get("submitted_at")) for r in pending if r.get("submitted_at")]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return market.trading_day_et()
    return (min(stamps) - timedelta(days=1)).strftime("%Y-%m-%d")


def apply_fills(led, conn, orders: list, *, window: str, now: str, count_misses: bool = False) -> dict:
    """Record the broker state of every order that has a ``broker_order_id`` and no final
    fill. A desk leg moves with its order (§11.1 step 9).

    One read that misses a desk leg proves nothing: the model can filter or drop orders when
    it writes the file. Only ``snapshot-commit`` counts a miss (``count_misses``). At 2 misses
    the leg ends ``outcome_unknown``. A row with no proposal (an old bot order) keeps the
    one-miss ``not_found`` rule."""
    by_id = {str(o.get("id") or ""): o for o in orders if o.get("id")}
    marks = ",".join("?" for _ in led.TERMINAL_FILL_STATES)
    pending = [dict(r) for r in conn.execute(
        "SELECT * FROM orders WHERE broker_order_id IS NOT NULL "
        f"AND (fill_state IS NULL OR fill_state NOT IN ({marks})) ORDER BY submitted_at",
        led.TERMINAL_FILL_STATES).fetchall()]
    captured, still_open, missing, unknown, outside = [], [], [], [], 0
    for row in pending:
        o = by_id.get(str(row["broker_order_id"]))
        if o is None:
            sub = store.parse_ts(row.get("submitted_at"))
            if window and sub is not None and sub.strftime("%Y-%m-%d") < window[:10]:
                outside += 1
                continue
            missing.append(row["ticker"])
            if not row.get("proposal_id"):
                conn.execute("UPDATE orders SET fill_state='not_found', fill_captured_at=? WHERE ref_id=?",
                             (now, row["ref_id"]))
                continue
            if count_misses and row["state"] in ("placed", "partially_filled"):
                attempts = int(row.get("reconcile_attempts") or 0) + 1
                if attempts >= 2:
                    conn.execute("UPDATE orders SET state='outcome_unknown', finalized=1, fill_state='not_found', "
                                 "fill_captured_at=?, reconcile_attempts=? WHERE ref_id=?",
                                 (now, attempts, row["ref_id"]))
                    unknown.append({"proposal_id": row["proposal_id"], "ticker": row["ticker"]})
                else:
                    conn.execute("UPDATE orders SET reconcile_attempts=? WHERE ref_id=?", (attempts, row["ref_id"]))
            continue
        f = parse_fill(o)
        conn.execute("UPDATE orders SET fill_avg_price=?, fill_quantity=?, fill_fees=?, fill_state=?, "
                     "fill_captured_at=? WHERE ref_id=?",
                     (f["avg_price"], f["quantity"], f["fees"], f["fill_state"], now, row["ref_id"]))
        if row.get("proposal_id") and row["state"] in ("placed", "partially_filled", "placing"):
            conn.execute("UPDATE orders SET state=? WHERE ref_id=?", (_fill_leg_state(o.get("state")), row["ref_id"]))
        if f["fill_state"] in led.TERMINAL_FILL_STATES:
            captured.append({"ticker": row["ticker"], "state": f["fill_state"], "avg_price": f["avg_price"],
                             "quantity": f["quantity"]})
        else:
            still_open.append({"ticker": row["ticker"], "state": f["fill_state"]})
    return {"pending_in": len(pending), "captured": captured, "still_open": still_open,
            "not_found": missing, "outcome_unknown": unknown, "outside_window": outside}


def cmd_fills(led, file_obj: dict, *, now_dt=None) -> dict:
    now = store.iso(now_dt or store.utc_now())
    orders = parse_orders(file_obj)
    if orders is None:
        raise ExecError('fills needs {"get_equity_orders": <raw result>}')
    window = fills_window(led)
    with led.transaction() as conn:
        res = apply_fills(led, conn, orders, window=window, now=now)
    return {"ok": True, "window": window, **res}


# --- snapshot-prepare / snapshot-commit (§11.1 step 6, §11.4) -------------------------

def snapshot_prepare(led, cfg: Config) -> dict:
    from lib import universe
    with led.connect() as conn:
        goal = proposals.active_goal(conn)
        rows = proposals.book_rows(conn, goal["id"], ("active", "exiting")) if goal else []
        tickers = set(universe.tradable_universe(
            [r for r in rows if r["status"] == "active"], cash_sleeve_ticker=None) if rows else [])
        tickers |= {r["ticker"] for r in rows}
        snap = proposals.latest_snapshot(conn)
        tickers |= set((snap or {}).get("positions", {}).keys())
        tickers |= {r[0] for r in conn.execute("SELECT ticker FROM companies WHERE status='watch'")}
        legs = [dict(r) for r in conn.execute(
            "SELECT ref_id, ticker, side, state, proposal_id, submitted_at FROM orders WHERE proposal_id IS NOT NULL "
            "AND state IN ('placing','placed','partially_filled') ORDER BY submitted_at").fetchall()]
    return {"ok": True, "tickers": sorted(t for t in tickers if t),
            "reconcile": legs, "fills_created_at_gte": fills_window(led),
            "account_number": cfg.account_number}


def snapshot_commit(led, cfg: Config, file_obj: dict, *, now_dt=None) -> dict:
    """Store the snapshot; capture the day's baseline at its first snapshot; run the
    daily-loss check (a halt blocks taps for the day; KILL is never written); record
    fills; reconcile ``placing`` legs; close every proposal whose last open leg ended."""
    now_dt = now_dt or store.utc_now()
    now = store.iso(now_dt)
    pf, pos, qts, approval = _fresh_inputs(file_obj)
    if pf is None or pos is None:
        raise ExecError("snapshot-commit needs get_portfolio and get_equity_positions results")
    orders = parse_orders(file_obj)
    out = {"ok": True}
    touched = set()
    with led.transaction() as conn:
        sett = desk_settings.effective(conn, cfg)
        was_halted = led.is_halted(market.trading_day_et(), conn=conn)
        out["snapshot_id"] = _store_snapshot(conn, now=now, portfolio_d=pf, positions=pos, qts=qts,
                                             approval=approval, raw=file_obj,
                              account_number=cfg.account_number)
        halted_now, drop = _daily_loss(led, conn, sett, pf["equity"], now)
        out.update(halted_now=bool(halted_now and not was_halted), drop_pct=round(drop, 4),
                   approval_setting=approval)
        if orders is not None:
            window = fills_window(led)
            out["fills"] = apply_fills(led, conn, orders, window=window, now=now, count_misses=True)
            for u in out["fills"]["outcome_unknown"]:
                _add_message(conn, u["proposal_id"], f"I can't confirm the {u['ticker']} order. "
                                                     "Check the Robinhood app.")
                touched.add(u["proposal_id"])
            legs = [dict(r) for r in conn.execute(
                "SELECT * FROM orders WHERE proposal_id IS NOT NULL AND state='placing'").fetchall()]
            taken = {r[0] for r in conn.execute("SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL")}
            for l in legs:
                o = match_order(l, orders, taken)
                if o is not None:
                    taken.add(o.get("id"))
                    _record_match(led, conn, l, o, now)
                    touched.add(l["proposal_id"])
                    continue
                attempts = int(l.get("reconcile_attempts") or 0) + 1
                if attempts >= 2:
                    conn.execute("UPDATE orders SET state='outcome_unknown', finalized=1, reconcile_attempts=? "
                                 "WHERE ref_id=?", (attempts, l["ref_id"]))
                    _add_message(conn, l["proposal_id"], f"I can't confirm the {l['ticker']} order. "
                                                         "Check the Robinhood app.")
                    touched.add(l["proposal_id"])
                else:
                    conn.execute("UPDATE orders SET reconcile_attempts=? WHERE ref_id=?", (attempts, l["ref_id"]))
        executing = [r[0] for r in conn.execute("SELECT id FROM proposals WHERE kind='trade' AND state='executing'")]
    # Close each proposal whose last open leg ended. This runs inside the broker model's
    # process, which holds no Telegram token: the result text is queued on the proposal and
    # run_broker.py sends it after the job (``pop_messages``).
    closed = []
    for pid in sorted(set(executing) | touched):
        text = proposals.close_from_legs(led, pid, now_dt)
        if text:
            with led.transaction() as conn:
                _add_message(conn, pid, text)
            closed.append(pid)
    out["closed"] = closed
    return out


def proposals_with_messages(led) -> list:
    """The trade proposals that hold queued owner messages."""
    with led.connect() as conn:
        return [r[0] for r in conn.execute("SELECT id FROM proposals WHERE kind='trade' AND "
                                           "result_json LIKE '%\"messages\": [\"%' ORDER BY id")]


def pop_messages(led, proposal_id: int) -> list:
    """Take the owner messages that exec-check and the reconcile queued on a proposal."""
    with led.transaction() as conn:
        prop = proposals.get_proposal(conn, proposal_id)
        if prop is None:
            return []
        res = store.loads(prop.get("result_json"), {}) or {}
        msgs = list(res.pop("messages", []) or [])
        if msgs:
            conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), proposal_id))
        return msgs


# --- requote proposals (§11.1 step 10) -------------------------------------------------

def build_requote(led, cfg: Config, proposal_id: int, *, now_dt=None, yf_price_fn=None) -> Optional[dict]:
    """After a job, build the new pending proposal for a requoted proposal (the whole
    proposal in the sells phase, the moved buys only in the buys phase). Returns
    ``{"new": built, "text": message}`` or None when there is nothing to requote."""
    from lib import notify
    now_dt = now_dt or store.utc_now()
    with led.connect() as conn:
        prop0 = proposals.get_proposal(conn, proposal_id)
    rq0 = (store.loads((prop0 or {}).get("result_json"), {}) or {}).get("requote")
    if not prop0 or not rq0 or rq0.get("built"):
        return None
    yf = proposals.prefetch_prices(led, rq0.get("tickers") or [], now_dt=now_dt,
                                   yf_price_fn=yf_price_fn or quotes.yf_last_price)
    with led.transaction() as conn:
        prop = proposals.get_proposal(conn, proposal_id)
        res = store.loads((prop or {}).get("result_json"), {}) or {}
        rq = res.get("requote")
        if not prop or not rq or rq.get("built"):
            return None
        snap_row = conn.execute("SELECT * FROM broker_snapshots WHERE id=?", (rq["snapshot_id"],)).fetchone()
        snap = proposals.snapshot_view(dict(snap_row)) if snap_row else None
        try:
            built = proposals.build_trade(
                led, conn, cfg=cfg, tickers=rq["tickers"], origin={}, requote_of=prop,
                rationale=prop["rationale"], created_by="system", now_dt=now_dt,
                snap_override=snap, yf_price_fn=yf)
        except proposals.ProposalError as e:
            res["requote"]["built"] = {"error": str(e)}
            conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), proposal_id))
            return {"new": None, "text": f"The price moved on #{proposal_id}, and I could not build new numbers: {e}."}
        res["requote"]["built"] = {"proposal_id": built["proposal_id"]}
        conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), proposal_id))
        sells_done = [dict(r) for r in conn.execute(
            "SELECT ticker, quantity, fill_avg_price, state FROM orders WHERE proposal_id=? AND side='sell' "
            "AND state IN ('placed','filled','partially_filled')", (proposal_id,)).fetchall()]
    text = notify.render_requote(rq["moves"], phase=rq.get("phase", "sells"), sells=sells_done)
    return {"new": built, "text": text}
