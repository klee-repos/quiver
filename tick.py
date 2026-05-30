#!/usr/bin/env python3
"""Deterministic orchestration brain for one trading tick.

The orchestrator (Claude Code, holding the Robinhood MCP) calls these
subcommands and executes EXACTLY what they return. All guardrails, clamps,
dedup, idempotency, and the daily-loss halt live here in Python — never in the
orchestrator's judgment.

Subcommands:
  preflight                  -> JSON: whether to proceed + config + pending tickers
  plan     --input FILE      -> JSON: concrete clamped orders to execute (+ records holds/skips)
  commit   --input FILE      -> record an order outcome (finalize + ticker_action)

`plan` input JSON shape (built by the orchestrator from MCP responses):
  {
    "equity": 10000.0,            # get_portfolio: total equity (today's value)
    "buying_power": 4000.0,       # get_portfolio: cash buying power
    "now_iso": "2026-05-30T09:36:00-04:00",
    "positions": {"AAPL": {"quantity": 3, "market_value": 600.0}},
    "analyses": [ <one analyze.py JSON per pending ticker> ]
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
from lib import market  # noqa: E402
from lib import notify  # noqa: E402
from lib import signals  # noqa: E402
from lib import storage  # noqa: E402

LEDGER_DB = REPO / "state" / "ledger.db"
ANALYZE_LOGS = REPO / "state" / "analyze_logs"
# Bulky, reconstructable artifacts the retention sweep ages out (state of record
# is the ledger, never these). Kept here so `prune` and any future caller agree.
PRUNE_TARGETS = [
    REPO / "logs" / "reasoning",
    REPO / "state" / "analyze_logs",
    REPO / "state" / "results",
    REPO / "state" / "cache",
]


def _cfg_and_ledger():
    cfg = load_config(REPO / "config.yaml")
    led = Ledger(LEDGER_DB)
    return cfg, led


def cmd_preflight(_args) -> dict:
    cfg, led = _cfg_and_ledger()
    day = market.trading_day_et()
    now = market.now_et().isoformat()

    out = {
        "proceed": False,
        "reason": None,
        "dry_run": cfg.dry_run,
        "account_number": cfg.account_number,
        "trading_day": day,
        "now_iso": now,
        "pending": [],
        "unfinalized": led.unfinalized_orders(day),
        "risk": {
            "max_dollars_per_trade": cfg.risk.max_dollars_per_trade,
            "daily_loss_halt_pct": cfg.risk.daily_loss_halt_pct,
            "daily_capital_deploy_cap": cfg.risk.daily_capital_deploy_cap,
            "max_open_position_per_ticker": cfg.risk.max_open_position_per_ticker,
            "min_buying_power_buffer": cfg.risk.min_buying_power_buffer,
        },
        "order": {
            "buy_type": cfg.buy_type,
            "time_in_force": cfg.time_in_force,
            "market_hours": cfg.market_hours,
        },
    }

    if Path(cfg.kill_switch_file).exists():
        out["reason"] = "kill_switch_present"
        return out
    if led.is_halted(day):
        out["reason"] = "daily_halt_flag_set"
        return out
    if not market.is_regular_session_open():
        out["reason"] = "market_closed"
        return out
    mso = market.minutes_since_open()
    if mso is None or mso < cfg.act_after_open_minutes:
        out["reason"] = f"too_early (minutes_since_open={mso})"
        return out

    pending = [t for t in cfg.watchlist if not led.already_acted(day, t)]
    out["pending"] = pending
    if not pending:
        out["reason"] = "all_watchlist_tickers_already_acted_today"
        return out

    out["proceed"] = True
    out["reason"] = "ok"
    return out


def cmd_plan(args) -> dict:
    cfg, led = _cfg_and_ledger()
    day = market.trading_day_et()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    now_iso = data.get("now_iso") or market.now_et().isoformat()
    equity = float(data["equity"])
    buying_power = float(data.get("buying_power", 0.0))
    positions = data.get("positions", {}) or {}
    analyses = data.get("analyses", []) or []

    baseline = led.get_or_create_baseline(day, equity, now_iso)
    drop_pct = (equity - baseline.baseline_equity) / baseline.baseline_equity * 100.0

    result = {
        "halt": False,
        "write_kill": False,
        "baseline_equity": baseline.baseline_equity,
        "equity": equity,
        "drop_pct": round(drop_pct, 3),
        "dry_run": cfg.dry_run,
        "orders": [],
        "decisions": [],
    }

    # Daily-loss kill-switch: breach -> halt the whole day, signal KILL write.
    if drop_pct <= -cfg.risk.daily_loss_halt_pct:
        led.mark_halted(day, f"daily_loss {drop_pct:.2f}% <= -{cfg.risk.daily_loss_halt_pct}%")
        result["halt"] = True
        result["write_kill"] = True
        return result

    remaining_daily_cap = cfg.risk.daily_capital_deploy_cap - led.day_buys_total(day)

    for a in analyses:
        ticker = str(a.get("ticker", "")).upper()
        signal = a.get("signal")
        decision = {"ticker": ticker, "signal": signal}

        if not ticker:
            continue
        if signal not in signals.VALID_SIGNALS:
            led.record_action(day, ticker, signal=str(signal), intent="skip",
                              status="error", detail=str(a.get("error", "bad signal")),
                              now_iso=now_iso)
            decision.update(status="error", detail=str(a.get("error", "bad signal")))
            result["decisions"].append(decision)
            continue

        pos = positions.get(ticker) or {}
        held_qty = float(pos.get("quantity", 0) or 0)
        held_mv = float(pos.get("market_value", 0) or 0)
        has_position = held_qty > 0

        intent, frac = signals.plan_action(signal, has_position)

        if intent in ("hold", "skip"):
            detail = "hold" if intent == "hold" else "no-position (long-only, no short)"
            led.record_action(day, ticker, signal=signal, intent=intent,
                              status="skipped", detail=detail, now_iso=now_iso)
            decision.update(status="skipped", intent=intent, detail=detail)
            result["decisions"].append(decision)
            continue

        if intent == "buy":
            room = max(0.0, cfg.risk.max_open_position_per_ticker - held_mv)
            dollars, src = signals.resolve_buy_dollars(
                a.get("position_sizing"), baseline.baseline_equity, frac,
                ceiling=cfg.risk.max_dollars_per_trade,
                remaining_daily_cap=remaining_daily_cap,
                buying_power=buying_power,
                buffer=cfg.risk.min_buying_power_buffer,
                room_under_ticker_cap=room,
            )
            if dollars <= 0:
                led.record_action(day, ticker, signal=signal, intent="buy",
                                  status="skipped", detail="sized to <=0 after clamps",
                                  now_iso=now_iso)
                decision.update(status="skipped", intent="buy", detail="sized_to_zero")
                result["decisions"].append(decision)
                continue
            remaining_daily_cap -= dollars
            ref_id = None
            if not cfg.dry_run:
                ref_id = led.new_ref_id()
                led.reserve_order(ref_id, day, ticker, side="buy", type=cfg.buy_type,
                                  dollar_amount=dollars, quantity=None, now_iso=now_iso)
            result["orders"].append({
                "ticker": ticker, "signal": signal, "intent": "buy",
                "ref_id": ref_id, "side": "buy", "type": cfg.buy_type,
                "dollar_amount": dollars, "quantity": None,
                "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
                "sizing_source": src,
            })
            decision.update(status="order", intent="buy", dollar_amount=dollars)
            result["decisions"].append(decision)

        elif intent == "sell":
            qty = signals.resolve_sell_quantity(held_qty, frac)
            if qty <= 0:
                led.record_action(day, ticker, signal=signal, intent="sell",
                                  status="skipped", detail="nothing to sell", now_iso=now_iso)
                decision.update(status="skipped", intent="sell", detail="nothing_to_sell")
                result["decisions"].append(decision)
                continue
            ref_id = None
            if not cfg.dry_run:
                ref_id = led.new_ref_id()
                led.reserve_order(ref_id, day, ticker, side="sell", type="market",
                                  dollar_amount=None, quantity=qty, now_iso=now_iso)
            result["orders"].append({
                "ticker": ticker, "signal": signal, "intent": "sell",
                "ref_id": ref_id, "side": "sell", "type": "market",
                "dollar_amount": None, "quantity": qty,
                "time_in_force": cfg.time_in_force, "market_hours": cfg.market_hours,
            })
            decision.update(status="order", intent="sell", quantity=qty)
            result["decisions"].append(decision)

    return result


def cmd_commit(args) -> dict:
    led = Ledger(LEDGER_DB)
    day = market.trading_day_et()
    now_iso = market.now_et().isoformat()
    d = json.loads(Path(args.input).read_text(encoding="utf-8"))

    ref_id = d.get("ref_id")
    if ref_id:
        led.finalize_order(ref_id, d.get("broker_order_id"),
                           json.dumps(d.get("result_json", {})))
    led.record_action(
        day, str(d["ticker"]).upper(),
        signal=d.get("signal", ""), intent=d.get("intent", ""),
        status=d["status"], detail=str(d.get("detail", "")), now_iso=now_iso,
    )
    return {"ok": True, "ticker": d["ticker"], "status": d["status"]}


# --- email digest ------------------------------------------------------------
# Observability only: BUILD the email + DECIDE whether it's new here in Python;
# the orchestrator does the actual Resend MCP send and then calls report-commit.
# Email is best-effort — a report error must NEVER abort or alter a trading tick.

def _read_reasoning(date: str, ticker: str):
    """Pull the curated decision + debate text from the audit dump (best-effort)."""
    path = ANALYZE_LOGS / f"{date}_{ticker}.json"
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/garbled audit log is non-fatal
        return None, None
    return j.get("final_trade_decision"), j.get("investment_plan")


def _build_report_model(cfg, led, data: dict, date: str, now_iso: str, kind: str) -> dict:
    """Assemble the render model from committed ledger truth + the plan output.

    Per-ticker spine = the plan's decisions/orders (intent, signal, sized $);
    the ledger's committed ticker_action overlays the REAL outcome (placed /
    dry_run / blocked_guardrail / error) and broker ids. All reads are
    null-safe so the auth_error path (which stops before any ledger write)
    still renders a minimal alert.
    """
    plan = data.get("plan") or {}
    baseline = led.get_baseline(date)
    actions = led.day_actions(date)
    orders = led.day_orders(date)

    equity = data.get("equity")
    if equity is None:
        equity = plan.get("equity")
    baseline_equity = baseline.baseline_equity if baseline else plan.get("baseline_equity")
    drop_pct = plan.get("drop_pct")
    if drop_pct is None and equity is not None and baseline_equity:
        drop_pct = (float(equity) - float(baseline_equity)) / float(baseline_equity) * 100.0
    halted = baseline.halted if baseline else bool(plan.get("halt"))
    halt_reason = baseline.halt_reason if baseline else None

    def _blank(t):
        return {"ticker": t, "signal": None, "intent": None, "status": None,
                "detail": None, "amount": None, "qty": None, "side": None,
                "broker_order_id": None}

    rows: dict = {}
    for d in plan.get("decisions", []):
        t = str(d.get("ticker", "")).upper()
        if not t:
            continue
        r = rows.setdefault(t, _blank(t))
        r.update(signal=d.get("signal"), intent=d.get("intent"),
                 status=d.get("status"), detail=d.get("detail"),
                 amount=d.get("dollar_amount"), qty=d.get("quantity"))
    for o in plan.get("orders", []):
        t = str(o.get("ticker", "")).upper()
        r = rows.setdefault(t, _blank(t))
        r.update(signal=r.get("signal") or o.get("signal"),
                 intent=r.get("intent") or o.get("intent"),
                 amount=o.get("dollar_amount"), qty=o.get("quantity"), side=o.get("side"))
    # Committed ledger rows are the source of truth for what actually happened.
    for a in actions:
        t = str(a.get("ticker", "")).upper()
        r = rows.setdefault(t, _blank(t))
        r["status"] = a.get("status") or r.get("status")
        r["detail"] = a.get("detail") or r.get("detail")
        r["signal"] = r.get("signal") or a.get("signal")
        r["intent"] = r.get("intent") or a.get("intent")
    for o in orders:
        t = str(o.get("ticker", "")).upper()
        r = rows.get(t)
        if not r:
            continue
        if o.get("broker_order_id"):
            r["broker_order_id"] = o.get("broker_order_id")
        if r.get("amount") is None and o.get("dollar_amount") is not None:
            r["amount"] = o.get("dollar_amount")
        if r.get("qty") is None and o.get("quantity") is not None:
            r["qty"] = o.get("quantity")
        r["side"] = r.get("side") or o.get("side")

    for t, r in rows.items():
        decision, debate = _read_reasoning(date, t)
        r["decision"], r["debate"] = decision, debate

    return {
        "date": date,
        "now_iso": now_iso,
        "kind": kind,
        "dry_run": bool(plan.get("dry_run", cfg.dry_run)),
        "subject_prefix": cfg.notify.subject_prefix,
        "equity": equity,
        "baseline_equity": baseline_equity,
        "drop_pct": drop_pct,
        "halted": halted,
        "halt_reason": halt_reason,
        "event_detail": data.get("event_detail"),
        "tickers": [rows[t] for t in sorted(rows)],
    }


def cmd_report(args) -> dict:
    cfg, led = _cfg_and_ledger()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    date = data.get("date") or market.trading_day_et()
    now_iso = data.get("now_iso") or market.now_et().isoformat()
    kind = data.get("kind", "digest")

    if not cfg.notify.enabled:
        return {"should_send": False, "reason": "notify_disabled", "kind": kind}

    model = _build_report_model(cfg, led, data, date, now_iso, kind)
    digest = notify.build_digest(model)
    already = led.last_notified_hash(date, kind)
    should_send = digest["content_hash"] != already
    return {
        "should_send": should_send,
        "reason": "new" if should_send else "already_sent",
        "kind": kind,
        "date": date,
        "from": cfg.notify.from_addr,
        "recipients": cfg.notify.to,
        "subject": digest["subject"],
        "html": digest["html"],
        "text": digest["text"],
        "content_hash": digest["content_hash"],
    }


def cmd_report_commit(args) -> dict:
    """Record a digest as sent — AFTER the orchestrator confirms delivery."""
    led = Ledger(LEDGER_DB)
    now_iso = market.now_et().isoformat()
    led.mark_notified(args.date, args.kind, args.content_hash,
                      args.recipients or "", now_iso)
    return {"ok": True, "date": args.date, "kind": args.kind, "hash": args.content_hash}


# --- storage retention -------------------------------------------------------
# Best-effort housekeeping: age out bulky reconstructable artifacts past the
# retention window, optionally offloading first (S3 backend deferred). Like the
# digest, this is observability only and must NEVER abort or alter a trading
# tick — prune_dir/get_archiver never raise.

def cmd_prune(_args) -> dict:
    cfg = load_config(REPO / "config.yaml")
    arch = storage.get_archiver(cfg.storage)
    summaries = [
        storage.prune_dir(t, cfg.storage.retention_days, archiver=arch)
        for t in PRUNE_TARGETS
    ]
    return {
        "ok": True,
        "retention_days": cfg.storage.retention_days,
        "archive_enabled": cfg.storage.archive_enabled,
        "pruned_total": sum(s["pruned"] for s in summaries),
        "archived_total": sum(s["archived"] for s in summaries),
        "dirs": summaries,
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--input", required=True)
    p_commit = sub.add_parser("commit")
    p_commit.add_argument("--input", required=True)
    p_report = sub.add_parser("report")
    p_report.add_argument("--input", required=True)
    p_rc = sub.add_parser("report-commit")
    p_rc.add_argument("--date", required=True)
    p_rc.add_argument("--kind", default="digest")
    p_rc.add_argument("--hash", required=True, dest="content_hash")
    p_rc.add_argument("--recipients", default="")
    sub.add_parser("prune")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "preflight":
            out = cmd_preflight(args)
        elif args.cmd == "plan":
            out = cmd_plan(args)
        elif args.cmd == "commit":
            out = cmd_commit(args)
        elif args.cmd == "report":
            out = cmd_report(args)
        elif args.cmd == "report-commit":
            out = cmd_report_commit(args)
        elif args.cmd == "prune":
            out = cmd_prune(args)
        else:  # unreachable
            raise SystemExit(2)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e), "cmd": args.cmd}))
        return 1

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
