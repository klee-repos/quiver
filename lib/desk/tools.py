"""The desk agent's and the analyst's tools, scoped by role (plan §5.4).

Each tool takes typed input and returns compact JSON. Roles: D = the desk agent (chat),
A = the analyst (worker). The tool server is built per process with closures over one
``ToolContext``, which holds the role, the per-turn ``TurnState`` or per-job ``JobState``,
the ledger, the outbox, and the network seams.

No tool approves, executes, places or cancels an order. No tool edits a file, runs a shell
command, or changes code. A tool may queue a broker ``snapshot`` job, which can place
nothing. The W3 test parses this file and enforces these rules.

Blocking work (SQLite, EDGAR, yfinance, a page fetch) runs in a worker thread, so a tool
never blocks the chat bridge's event loop: a button tap is answered while a turn runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

from lib import market, memory
from lib.desk import (agenda, documents, edgar, etf, events, findings, news, outbox, proposals, quotes,
                      reactions, settings as desk_settings, store, transcripts, valuation, xbrl)

SAY_LIMIT = 1200
REPORT_LIMIT = 3000
DEEP_DIVES_PER_DAY = 6
REFRESH_MAX_WAIT_S = 120

# --- the registry -------------------------------------------------------------------

READ_TOOLS = (
    "portfolio_view", "refresh_portfolio", "get_quotes", "get_book", "list_companies", "get_company",
    "search_research", "list_filings", "get_filing_sections", "get_document", "get_financials",
    "get_metrics", "run_valuation", "get_technicals", "get_transcript", "get_etf_lookthrough",
    "get_events", "get_proposals", "get_track_record", "get_attribution", "get_settings",
    "ledger_query", "screen_companies",
)
DESK_ONLY = ("react", "say", "set_research_status", "start_deep_dive", "bring_up_next", "close_item",
             "defer_item", "hold_agenda", "show_card")
ANALYST_ONLY = ("report", "record_verdict")
SHARED_WRITE = ("load_company", "fetch_document", "record_finding", "add_note", "log_news_event",
                "withdraw_proposal", "propose_trade", "propose_change")


def role_tool_names(role: str) -> list:
    if role == "desk":
        return list(READ_TOOLS) + list(DESK_ONLY) + list(SHARED_WRITE)
    if role == "analyst":
        return list(READ_TOOLS) + list(ANALYST_ONLY) + list(SHARED_WRITE)
    raise ValueError(role)


@dataclass
class ToolContext:
    role: str
    runtime: object
    secrets: object
    led: object
    cfg: object
    turn: object = None
    job: object = None
    box: object = None
    transport: Optional[Callable] = None
    seams: object = None
    render: Callable = None
    yf_price_fn: Callable = None
    yf_time_fn: Callable = None
    screen_fn: Callable = None
    efts_fn: Callable = None
    fetch_fn: Callable = None
    sleep: Callable = time.sleep
    touch: Callable = None
    attribution_fn: Callable = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def build(cls, *, role, runtime, secrets, turn=None, job=None, led=None, transport=None, box=None,
              render=None, **kw):
        from lib.ledger import Ledger
        led = led or Ledger(runtime.ledger_db)
        cfg = runtime.cfg
        if cfg is None:
            from lib.config import load_config
            cfg = load_config(Path(runtime.root) / "config.yaml")
        if transport is None:
            transport = outbox.telegram_transport(secrets.telegram_bot_token)
        owner = (secrets.owner_ids or [None])[0]
        box = box or outbox.Outbox(led, transport=transport, chat_id=owner)
        seams = kw.pop("seams", None) or events.Seams(
            client=edgar.EdgarClient(secrets.sec_user_agent) if secrets.sec_user_agent else None,
            av_key=secrets.alpha_vantage_api_key)
        return cls(role=role, runtime=runtime, secrets=secrets, led=led, cfg=cfg, turn=turn, job=job,
                   box=box, transport=transport, seams=seams, render=render or outbox.render_model_text, **kw)

    # helpers
    def conn(self):
        return self.led.connect()

    def settings(self) -> dict:
        with self.conn() as c:
            return desk_settings.effective(c, self.cfg)

    def now(self):
        return store.utc_now()

    def price(self, ticker):
        fn = self.yf_price_fn or quotes.yf_last_price
        return fn(ticker)

    def trigger(self, name: str) -> None:
        if self.touch:
            self.touch(name)
            return
        store.touch_trigger(self.runtime.root, name)


class ToolError(ValueError):
    """A refused tool call. The text goes back to the model."""


def _ok(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, default=str, separators=(",", ":"))}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": str(msg)}], "is_error": True}


def _limit(v, default, high):
    try:
        n = int(v if v is not None else default)
    except (TypeError, ValueError):
        raise ToolError("limit must be a number")
    if n < 1 or n > high:
        raise ToolError(f"limit must be 1 to {high}")
    return n


def _ticker(v) -> str:
    t = str(v or "").strip().upper()
    if not t or len(t) > 12 or not all(ch.isalnum() or ch in ".-" for ch in t):
        raise ToolError("give a valid ticker")
    return t


# --- read tools ----------------------------------------------------------------------

def t_portfolio_view(ctx: ToolContext, args: dict) -> dict:
    with ctx.conn() as c:
        snap = proposals.latest_snapshot(c)
        goal = proposals.active_goal(c)
        rows = {r["ticker"]: r for r in proposals.book_rows(c, goal["id"], ("active", "exiting"))} if goal else {}
        base_day = c.execute("SELECT baseline_equity FROM day_baseline WHERE trade_date=?",
                             (market.trading_day_et(),)).fetchone()
    if snap is None:
        return {"snapshot": None, "note": "no portfolio snapshot yet; call refresh_portfolio"}
    positions, total = [], 0.0
    for sym, p in sorted((snap.get("positions") or {}).items()):
        qty = float(p.get("quantity") or 0)
        try:
            px = float(ctx.price(sym))
        except Exception:  # noqa: BLE001
            px = (float(p["market_value"]) / qty) if p.get("market_value") and qty else None
        mv = qty * px if px else 0.0
        total += mv
        positions.append({"ticker": sym, "quantity": qty, "price": px, "value": round(mv, 2)})
    dep = total + float(snap["buying_power"])
    for p in positions:
        r = rows.get(p["ticker"])
        p["weight_pct"] = round(p["value"] / dep * 100, 2) if dep else None
        if r:
            p["target_pct"] = float(r["target_weight"] or 0)
            p["band_pct"] = float(r.get("band") or 0)
            p["status"] = r["status"]
        else:
            p["status"] = "unmanaged"
    for t, r in rows.items():
        if t not in {p["ticker"] for p in positions}:
            positions.append({"ticker": t, "quantity": 0, "value": 0.0, "weight_pct": 0.0,
                              "target_pct": float(r["target_weight"] or 0), "band_pct": float(r.get("band") or 0),
                              "status": r["status"]})
    eq = total + float(snap["buying_power"])
    age = store.age_seconds(snap["captured_at"])
    return {"equity_live": round(eq, 2), "snapshot_equity": snap["equity"], "cash": snap["cash"],
            "buying_power": snap["buying_power"], "positions": positions,
            "day_pnl": round(eq - float(base_day[0]), 2) if base_day else None,
            "snapshot_age_min": round(age / 60, 1) if age is not None else None,
            "approval_setting": snap.get("approval_setting")}


def t_refresh_portfolio(ctx: ToolContext, args: dict) -> dict:
    wait = int(args.get("wait_seconds") or 60)
    if wait < 0 or wait > REFRESH_MAX_WAIT_S:
        raise ToolError("wait_seconds must be 0 to 120")
    with ctx.conn() as c:
        before = c.execute("SELECT COALESCE(MAX(id), 0) FROM broker_snapshots").fetchone()[0]
        # Reuse a queued snapshot job: each job is one paid broker run, and a queued job
        # starts after this request anyway.
        if c.execute("SELECT 1 FROM broker_jobs WHERE kind='snapshot' AND status='queued' LIMIT 1").fetchone() is None:
            store.enqueue_snapshot_job(c, requested_by=ctx.role, now=store.now_iso())
    ctx.trigger("broker")
    waited = 0
    while waited < wait:
        with ctx.conn() as c:
            newest = c.execute("SELECT COALESCE(MAX(id), 0) FROM broker_snapshots").fetchone()[0]
        if newest > before:
            return {"refreshed": True, **t_portfolio_view(ctx, {})}
        ctx.sleep(3)
        waited += 3
    return {"refreshed": False, "note": "the broker has not answered yet; the snapshot job is queued"}


def t_get_quotes(ctx: ToolContext, args: dict) -> dict:
    tickers = [_ticker(t) for t in (args.get("tickers") or [])]
    if not tickers or len(tickers) > 20:
        raise ToolError("give 1 to 20 tickers")
    out = []
    for t in tickers:
        try:
            out.append({"ticker": t, "price": float(ctx.price(t)), "source": "yfinance",
                        "at": store.now_iso()})
        except Exception as e:  # noqa: BLE001
            out.append({"ticker": t, "price": None, "error": f"{type(e).__name__}"})
    return {"quotes": out}


def t_get_book(ctx: ToolContext, args: dict) -> dict:
    with ctx.conn() as c:
        prem = c.execute("SELECT version, text, goal_json, confirmed_at FROM premise ORDER BY version DESC LIMIT 1").fetchone()
        goal = proposals.active_goal(c)
        rows = proposals.book_rows(c, goal["id"], ("active", "exiting")) if goal else []
        sett = desk_settings.effective(c, ctx.cfg)
    sleeves = {}
    for r in rows:
        sleeves[r.get("sleeve") or "?"] = round(sleeves.get(r.get("sleeve") or "?", 0) + float(r["target_weight"] or 0), 4)
    return {"premise": {"version": prem[0], "text": prem[1], "goal": json.loads(prem[2]),
                        "confirmed_at": prem[3]} if prem else None,
            "targets": [{"ticker": r["ticker"], "sleeve": r.get("sleeve"), "target_weight": r["target_weight"],
                         "band": r.get("band"), "status": r["status"], "entry_price_max": r.get("entry_price_max")}
                        for r in rows],
            "sleeves": sleeves,
            "cash_floor_pct": desk_settings.cash_floor_pct(sett, rows, ctx.cfg.risk.cash_sleeve_ticker)}


def t_list_companies(ctx: ToolContext, args: dict) -> dict:
    with ctx.conn() as c:
        rows = store.list_companies(c, args.get("status"))
    return {"companies": [{k: r[k] for k in ("ticker", "name", "kind", "sleeve", "status", "status_reason")}
                          for r in rows]}


def t_get_company(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    with ctx.conn() as c:
        comp = store.get_company(c, t)
        if comp is None:
            raise ToolError(f"{t} is not loaded; call load_company")
        th = c.execute("SELECT * FROM theses WHERE ticker=? ORDER BY version DESC LIMIT 1", (t,)).fetchone()
        met = valuation.latest_metrics(c, t)
        fnd = findings.list_findings(c, t, limit=10)
        evs = [dict(r) for r in c.execute("SELECT id, type, status, verdict, detected_at FROM events WHERE ticker=? "
                                          "AND status IN ('new','queued','analyzed') ORDER BY id DESC LIMIT 10", (t,))]
        snap = proposals.latest_snapshot(c)
        lv = [dict(r) for r in c.execute("SELECT level, price, armed FROM level_state WHERE ticker=?", (t,))]
    thesis = None
    if th:
        thesis = {k: th[k] for k in ("version", "summary", "premise_link", "fair_value_low", "fair_value_base",
                                     "fair_value_high", "buy_below", "trim_above", "confirmed_at")}
        thesis["drivers"] = json.loads(th["drivers_json"])
        thesis["falsifiers"] = json.loads(th["falsifiers_json"])
    return {"company": {k: comp[k] for k in ("ticker", "name", "kind", "cik", "sleeve", "status", "status_reason")},
            "thesis": thesis, "metrics": met, "findings": fnd, "open_events": evs, "levels": lv,
            "position": (snap or {}).get("positions", {}).get(t),
            "next_earnings": ((met or {}).get("metrics") or met or {}).get("next_earnings") if met else None}


def t_search_research(ctx: ToolContext, args: dict) -> dict:
    limit = _limit(args.get("limit"), 10, 20)
    with ctx.conn() as c:
        hits = store.search(c, str(args.get("query") or ""), ticker=args.get("ticker"),
                            kinds=args.get("kinds"), since=args.get("since"), limit=limit)
    return {"hits": hits}


def t_list_filings(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    limit = _limit(args.get("limit"), 20, 40)
    forms = [str(f).upper() for f in (args.get("forms") or [])]
    sql = "SELECT accession, form, filed_date, report_date, items, tier, ingested_at, ingest_error FROM filings WHERE ticker=?"
    params = [t]
    if forms:
        sql += " AND form IN (%s)" % ",".join("?" for _ in forms)
        params += forms
    if args.get("since"):
        sql += " AND filed_date >= ?"
        params.append(str(args["since"]))
    sql += " ORDER BY filed_date DESC, accession DESC LIMIT ?"
    params.append(limit)
    with ctx.conn() as c:
        return {"filings": [dict(r) for r in c.execute(sql, params).fetchall()]}


def t_get_filing_sections(ctx: ToolContext, args: dict) -> dict:
    acc = str(args.get("accession") or "").strip()
    with ctx.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT id AS document_id, section_key, title, char_len FROM documents "
                                           "WHERE accession=? ORDER BY id", (acc,)).fetchall()]
    if not rows:
        raise ToolError(f"no stored sections for {acc}")
    return {"accession": acc, "sections": rows}


def t_get_document(ctx: ToolContext, args: dict) -> dict:
    did = int(args.get("document_id"))
    offset = int(args.get("offset") or 0)
    mx = int(args.get("max_chars") or 20000)
    if mx < 1 or mx > 20000 or offset < 0:
        raise ToolError("max_chars must be 1 to 20000 and offset >= 0")
    with ctx.conn() as c:
        d = store.get_document(c, did)
    if d is None:
        raise ToolError(f"document {did} does not exist")
    return {"document_id": did, "kind": d["kind"], "ticker": d["ticker"], "title": d["title"],
            "section_key": d["section_key"], "url": d["url"], "published_at": d["published_at"],
            "total_chars": d["char_len"], "offset": offset, "text": d["text"][offset:offset + mx]}


def _cik(ctx, t):
    with ctx.conn() as c:
        comp = store.get_company(c, t)
    if comp is None or not comp.get("cik"):
        raise ToolError(f"{t} has no SEC entity loaded; call load_company")
    return comp["cik"]


def t_get_financials(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    period = args.get("period") or "quarterly"
    if period not in ("quarterly", "annual"):
        raise ToolError("period must be quarterly or annual")
    limit = _limit(args.get("limit"), 8, 12)
    cik = _cik(ctx, t)
    with ctx.conn() as c:
        return {"ticker": t, "period": period, "rows": xbrl.financials(c, cik, period, limit)}


def t_get_metrics(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    with ctx.conn() as c:
        m = valuation.latest_metrics(c, t)
    return {"ticker": t, "metrics": m}


def t_run_valuation(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    cik = _cik(ctx, t)
    with ctx.conn() as c:
        try:
            return valuation.run_valuation(c, t, str(args.get("method") or ""), args.get("scenarios") or {},
                                           settings=ctx.settings(), cik=cik)
        except valuation.ValuationError as e:
            raise ToolError(str(e))


def t_get_technicals(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    fn = ctx.extra.get("technicals_fn") or valuation.trend_technicals
    return {"ticker": t, "technicals": fn(t)}


def t_get_transcript(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    q = args.get("quarter")
    with ctx.conn() as c:
        if q:
            row = c.execute("SELECT quarter, document_id FROM transcripts WHERE ticker=? AND quarter=?", (t, q)).fetchone()
        else:
            row = c.execute("SELECT quarter, document_id FROM transcripts WHERE ticker=? ORDER BY quarter DESC LIMIT 1",
                            (t,)).fetchone()
        if row:
            return {"ticker": t, "quarter": row[0], "document_id": row[1]}
        comp = store.get_company(c, t)
        if not q:
            er = c.execute("SELECT report_date, filed_date FROM filings WHERE ticker=? AND form='8-K' AND items LIKE '%2.02%' "
                           "ORDER BY filed_date DESC LIMIT 1", (t,)).fetchone()
            if er is None or comp is None:
                raise ToolError(f"no earnings release on file for {t}; give a quarter like 2026Q2")
            qs = transcripts.quarter_for(c, cik=comp["cik"], report_date=er[0] or er[1])
        else:
            qs = [q]
        last = None
        for quarter in qs:
            last = transcripts.fetch_transcript(c, t, quarter, api_key=ctx.secrets.alpha_vantage_api_key,
                                                now=store.now_iso(), opener=ctx.extra.get("av_opener"))
            if last.get("state") in ("stored", "no_budget", "no_key"):
                break
    return {"ticker": t, **{k: v for k, v in (last or {}).items() if k != "error"}}


def t_get_etf_lookthrough(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    with ctx.conn() as c:
        latest = etf.latest_holdings(c, t)
        lt = events.lookthrough_union(c, t, threshold_pct=0.0)
        mets = {}
        for r in lt:
            m = valuation.latest_metrics(c, r["ticker"])
            if m:
                mets[r["ticker"]] = m.get("metrics", m)
        er = etf.get_expense_ratio(c, t)
    holdings = [{"ticker": r["ticker"], "weight_pct": r["weight_pct"], "name": r["name"], "source": r["source"]} for r in lt]
    return {"fund": t, "as_of": (latest or {}).get("as_of"), "source": (latest or {}).get("source"),
            "holdings": holdings[:40],
            "weighted": etf.weighted_lookthrough(holdings, mets, expense_ratio=(er or {}).get("value") if isinstance(er, dict) else er),
            "expense_ratio": er}


def t_get_events(ctx: ToolContext, args: dict) -> dict:
    limit = _limit(args.get("limit"), 15, 30)
    sql, params = "SELECT id, ticker, type, created_by, status, verdict, detected_at, analysis, detail_json FROM events WHERE 1=1", []
    if args.get("ticker"):
        sql += " AND ticker=?"
        params.append(_ticker(args["ticker"]))
    if args.get("status"):
        sql += " AND status=?"
        params.append(str(args["status"]))
    if args.get("since"):
        sql += " AND detected_at >= ?"
        params.append(str(args["since"]))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with ctx.conn() as c:
        rows = [dict(r) for r in c.execute(sql, params).fetchall()]
    for r in rows:
        r["detail"] = store.loads(r.pop("detail_json"), {})
    return {"events": rows}


def t_get_proposals(ctx: ToolContext, args: dict) -> dict:
    limit = _limit(args.get("limit"), 10, 30)
    sql, params = "SELECT * FROM proposals WHERE 1=1", []
    if args.get("state"):
        sql += " AND state=?"
        params.append(str(args["state"]))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    out = []
    with ctx.conn() as c:
        for p in [dict(r) for r in c.execute(sql, params).fetchall()]:
            legs = [{k: l[k] for k in ("ticker", "side", "state", "dollar_amount", "quantity", "quote_at_proposal",
                                       "fill_avg_price", "fill_quantity", "skip_reason")}
                    for l in ctx.led.legs(p["id"], conn=c)]
            out.append({"id": p["id"], "kind": p["kind"], "origin": p["origin"], "state": p["state"],
                        "rationale": p["rationale"], "created_at": p["created_at"], "expires_at": p["expires_at"],
                        "payload": store.loads(p["payload_json"], {}), "legs": legs,
                        "result": store.loads(p.get("result_json"), None)})
    return {"proposals": out}


def t_get_track_record(ctx: ToolContext, args: dict) -> dict:
    if args.get("ticker"):
        t = _ticker(args["ticker"])
        rows = ctx.led.decisions_with_outcomes(t, limit=20)
        return {"ticker": t, "scorecard": memory.scorecard(ctx.led, t), "decisions": rows}
    with ctx.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT d.id, d.trade_date, d.ticker, d.signal, d.decision_price, o.directional_return, o.alpha, "
            "o.realized_pnl FROM decisions d LEFT JOIN outcomes o ON o.decision_id=d.id "
            "WHERE d.proof_json LIKE '%proposal_id%' ORDER BY d.id DESC LIMIT 40").fetchall()]
    scored = [r for r in rows if r["directional_return"] is not None]
    hits = sum(1 for r in scored if (r["directional_return"] or 0) * (1 if r["signal"] == "buy" else -1) > 0)
    return {"decisions": rows, "scored": len(scored), "hit_rate": round(hits / len(scored), 4) if scored else None,
            "realized_pnl": round(sum(r["realized_pnl"] or 0 for r in rows), 2)}


def t_get_attribution(ctx: ToolContext, args: dict) -> dict:
    fn = ctx.attribution_fn
    if fn is None:
        import argparse
        from lib import factor_report  # the same read-only code as `tick.py attribution`
        fn = lambda: factor_report.cmd_attribution(  # noqa: E731
            argparse.Namespace(date="", benchmark="SPY"), led=ctx.led,
            cfg_path=Path(ctx.runtime.root) / "config.yaml")
    return fn()


def t_get_settings(ctx: ToolContext, args: dict) -> dict:
    return {"settings": ctx.settings(), "kill_switch_active": proposals.kill_active(ctx.cfg),
            "halted_today": ctx.led.is_halted(market.trading_day_et()),
            "cash_sleeve": ctx.cfg.risk.cash_sleeve_ticker}


def t_ledger_query(ctx: ToolContext, args: dict) -> dict:
    from deploy.runner import chat_query
    return chat_query.query(str(args.get("sql") or ""), ctx.runtime.ledger_db, max_rows=200)


def t_screen_companies(ctx: ToolContext, args: dict) -> dict:
    limit = _limit(args.get("limit"), 15, 25)
    out, seen = [], set()
    q = str(args.get("query") or "").strip()
    if q and ctx.efts_fn is not None:
        for hit in (ctx.efts_fn(q) or [])[:limit]:
            if hit.get("ticker") and hit["ticker"] not in seen:
                seen.add(hit["ticker"])
                out.append({**hit, "source": "sec_fulltext"})
    screen = {k: args[k] for k in ("sector", "industry", "market_cap_min") if args.get(k)}
    if screen:
        fn = ctx.screen_fn
        if fn is None:
            from lib.screener_data import yfinance_candidate_provider as fn
        for row in (fn(screen) or [])[:limit]:
            t = str(row.get("ticker") or "").upper()
            if t and t not in seen:
                seen.add(t)
                out.append({**row, "source": "yfinance_screener"})
    with ctx.conn() as c:
        goal = proposals.active_goal(c)
        funds = [r["ticker"] for r in proposals.book_rows(c, goal["id"], ("active",))] if goal else []
        for f in funds:
            for r in events.lookthrough_union(c, f, threshold_pct=2.0):
                if r["ticker"] not in seen:
                    seen.add(r["ticker"])
                    out.append({"ticker": r["ticker"], "name": r["name"], "source": f"holding of {f} ({r['weight_pct']:.1f}%)"})
        for row in out:
            m = valuation.latest_metrics(c, row["ticker"])
            if m:
                mm = m.get("metrics", m)
                row["quick"] = {k: mm.get(k) for k in ("revenue_growth_yoy", "fcf_margin_ttm", "ev_sales",
                                                       "reverse_dcf_implied_growth")}
    return {"candidates": out[: limit * 2]}


# --- write tools ---------------------------------------------------------------------

def t_react(ctx: ToolContext, args: dict) -> dict:
    turn = ctx.turn
    mid = int(args.get("message_id"))
    if mid not in (turn.owner_message_ids or []):
        raise ToolError(f"message {mid} is not an owner message of this turn ({turn.owner_message_ids})")
    emoji = reactions.check(args.get("emoji"))
    if emoji is None:
        raise ToolError("Telegram allows only these reactions: " + " ".join(reactions.ALLOWED))
    res = ctx.transport("setMessageReaction", {"chat_id": turn.chat_id, "message_id": mid,
                                               "reaction": [{"type": "emoji", "emoji": emoji}]}) or {}
    turn.reacted_ids.add(mid)
    with ctx.conn() as c:
        c.execute("UPDATE chat_messages SET reaction=?, reacted_at=? WHERE chat_id=? AND tg_message_id=?",
                  (emoji, store.now_iso(), str(turn.chat_id), mid))
    return {"reacted": mid, "emoji": emoji, "telegram_ok": bool(res.get("ok"))}


def check_say(turn, text: str) -> None:
    """The ``say`` rules (§4.4 step 1, §4.2 step 3): at most 1,200 characters, and a
    reaction on every owner message of the turn first."""
    if not str(text or "").strip():
        raise ToolError("say needs text")
    if len(text) > SAY_LIMIT:
        raise ToolError(f"that message is {len(text)} characters; the limit is 1200, so split it")
    missing = turn.unreacted()
    if missing:
        raise ToolError(f"react to owner message(s) {missing} first")


def t_say(ctx: ToolContext, args: dict) -> dict:
    turn = ctx.turn
    text = str(args.get("text") or "")
    check_say(turn, text)
    ctx.transport("sendChatAction", {"chat_id": turn.chat_id, "action": "typing"})
    reply = turn.first_reply_to if not turn.said else None
    res = ctx.box.send(ctx.render(text), source="chat", reply_to=reply, session_id=turn.session_id, delivered=1)
    turn.said.append(text)
    return {"sent": bool(res.get("ok")), "message_id": res.get("message_id")}


def t_report(ctx: ToolContext, args: dict) -> dict:
    """The analyst's report goes to its job's agenda item, not to the owner. The desk brings
    it up when the owner is free. A second call replaces the first."""
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("report needs text")
    if len(text) > REPORT_LIMIT:
        raise ToolError(f"that report is {len(text)} characters; the limit is {REPORT_LIMIT}, so cut it")
    if ctx.job is None or ctx.job.job_id is None:
        raise ToolError("report works inside a job only")
    with ctx.conn() as c:
        if not agenda.set_report(c, ctx.job.job_id, text, now=store.now_iso()):
            raise ToolError("this job's report already went to the desk")
    ctx.job.messages += 1
    return {"saved": True, "chars": len(text),
            "note": "The desk brings this up with the owner when they are free, with your cards."}


def t_load_company(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    if ctx.seams is None or ctx.seams.client is None:
        raise ToolError("SEC access is not configured")
    try:
        return events.load_company(ctx.led, t, settings=ctx.settings(), seams=ctx.seams, status="candidate")
    except edgar.UnknownTicker:
        raise ToolError(f"{t} has no SEC entity (unknown ticker)")


def t_fetch_document(ctx: ToolContext, args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    t = _ticker(args["ticker"]) if args.get("ticker") else None
    with ctx.conn() as c:
        try:
            did = documents.store_fetched(c, url, kind="web", ticker=t, now=store.now_iso(),
                                          **({"connector": ctx.fetch_fn} if ctx.fetch_fn else {}))
        except documents.FetchError as e:
            raise ToolError(str(e))
        d = store.get_document(c, did)
    return {"document_id": did, "title": d["title"], "chars": d["char_len"]}


def t_record_finding(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    with ctx.conn() as c:
        try:
            fid = findings.record_finding(c, ticker=t, kind=str(args.get("kind") or ""), claim=str(args.get("claim") or ""),
                                          quote=str(args.get("quote") or ""), document_id=int(args.get("document_id")),
                                          created_by=ctx.role, now=store.now_iso(),
                                          event_id=getattr(ctx.job, "event_id", None) if ctx.job else None)
        except findings.FindingError as e:
            raise ToolError(str(e))
    return {"finding_id": fid}


def t_add_note(ctx: ToolContext, args: dict) -> dict:
    text = str(args.get("text") or "").strip()
    if not text or len(text) > 4000:
        raise ToolError("a note needs 1 to 4000 characters")
    t = _ticker(args["ticker"]) if args.get("ticker") else None
    tags = args.get("tags")
    tags = ",".join(str(x) for x in tags) if isinstance(tags, list) else (str(tags) if tags else None)
    with ctx.conn() as c:
        cur = c.execute("INSERT INTO notes (ticker, text, tags, created_by, created_at) VALUES (?,?,?,?,?)",
                        (t, text, tags, ctx.role, store.now_iso()))
    return {"note_id": cur.lastrowid}


def t_set_research_status(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    status = str(args.get("status") or "")
    if status not in ("candidate", "watch", "rejected"):
        raise ToolError("status must be candidate, watch or rejected (holding and dropped change only by a confirmed proposal)")
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ToolError("give a reason")
    with ctx.conn() as c:
        comp = store.get_company(c, t)
        if comp is None:
            raise ToolError(f"{t} is not loaded")
        if comp["status"] in ("holding", "dropped"):
            raise ToolError(f"{t} is {comp['status']}; only a confirmed proposal changes that")
        store.set_company_status(c, t, status, reason, store.now_iso())
    return {"ticker": t, "status": status}


def t_log_news_event(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    with ctx.conn() as c:
        try:
            eid = news.log_news_event(c, ticker=t, document_id=int(args.get("document_id")), now=store.now_iso())
        except news.NewsError as e:
            raise ToolError(str(e))
    return {"event_id": eid}


def t_start_deep_dive(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    focus = str(args.get("focus") or "").strip()[:300] or None
    with ctx.conn() as c:
        if store.get_company(c, t) is None:
            raise ToolError(f"{t} is not loaded; call load_company first")
        busy = c.execute("SELECT id FROM jobs WHERE kind='deep_dive' AND status IN ('queued','running') "
                         "AND payload_json LIKE ?", (f'%"ticker": "{t}"%',)).fetchone()
        if busy:
            raise ToolError(f"a deep-dive on {t} is already queued or running (job {busy[0]})")
        if not store.budget_take(c, "deep_dive", market.trading_day_et(), DEEP_DIVES_PER_DAY):
            raise ToolError("that is the 7th deep-dive today; the limit is 6 a day")
        jid = store.enqueue_job(c, kind="deep_dive", payload={"ticker": t, "focus": focus},
                                requested_by=ctx.role, now=store.now_iso())
    ctx.trigger("worker")
    return {"job_id": jid, "ticker": t}


def _owner_turn(ctx: ToolContext, name: str) -> None:
    """The agenda moves on only after the owner speaks: a turn with no owner message (the
    bridge brought an item up) cannot close, defer, hold or bring up an item."""
    if ctx.turn is None or not ctx.turn.owner_message_ids:
        raise ToolError(f"{name} needs an owner message in this turn; wait for the owner to answer")


def _agenda_item_out(conn, it: dict) -> dict:
    return {"item": it["id"], "priority": agenda.PRIORITY_TEXT.get(it["priority"]), "kind": it["kind"],
            "ticker": it["ticker"], "title": it["title"], "report": it["body"],
            "cards": agenda.card_facts(conn, it)}


def t_bring_up_next(ctx: ToolContext, args: dict) -> dict:
    _owner_turn(ctx, "bring_up_next")
    now = store.now_iso()
    with ctx.conn() as c:
        agenda.refresh(c, now)
        op = agenda.open_item(c)
        if op is not None:
            raise ToolError(f"item {op['id']} is still open; call close_item or defer_item first")
        agenda.hold(c, None)
        it = agenda.take_next(c, now)
        if it is None:
            return {"item": None, "note": "Nothing waits on the agenda."}
        out = _agenda_item_out(c, it)
    out["note"] = ("Tell the owner about it now, in your own words. The bridge sends its cards after your "
                   "last message; call show_card to place a card sooner.")
    return out


def t_close_item(ctx: ToolContext, args: dict) -> dict:
    _owner_turn(ctx, "close_item")
    iid = int(args.get("item_id"))
    reason = str(args.get("reason") or "").strip() or "the owner dealt with it"
    with ctx.conn() as c:
        it = agenda.get(c, iid)
        if it is None or it["status"] not in ("waiting", "open"):
            raise ToolError(f"item {iid} is not open or waiting")
        agenda.close(c, iid, reason=reason, now=store.now_iso())
        pending = [x["id"] for x in agenda.card_facts(c, it) if x["state"] == "pending"]
    out = {"item": iid, "closed": True}
    if pending:
        out["note"] = ("Its cards stay open until the owner taps them or they expire: "
                       + ", ".join(f"#{p}" for p in pending) + ".")
    return out


def t_defer_item(ctx: ToolContext, args: dict) -> dict:
    _owner_turn(ctx, "defer_item")
    iid = int(args.get("item_id"))
    try:
        hours = float(args.get("hours"))
    except (TypeError, ValueError):
        raise ToolError("hours must be a number from 1 to 72")
    if not 1 <= hours <= 72:
        raise ToolError("hours must be a number from 1 to 72")
    with ctx.conn() as c:
        until = agenda.defer(c, iid, hours=hours, now=store.now_iso())
    if until is None:
        raise ToolError(f"item {iid} is not open or waiting")
    return {"item": iid, "not_before": until}


def t_hold_agenda(ctx: ToolContext, args: dict) -> dict:
    _owner_turn(ctx, "hold_agenda")
    try:
        hours = float(args.get("hours"))
    except (TypeError, ValueError):
        raise ToolError("hours must be a number from 0 to 24 (0 lifts the hold)")
    if not 0 <= hours <= 24:
        raise ToolError("hours must be a number from 0 to 24 (0 lifts the hold)")
    until = store.iso(ctx.now() + timedelta(hours=hours)) if hours else None
    with ctx.conn() as c:
        agenda.hold(c, until)
        n = agenda.counts(c)
    return {"hold_until": until, "waiting": n["waiting"], "open": n["open"]}


def t_show_card(ctx: ToolContext, args: dict) -> dict:
    """Send a pending card again at the bottom of the chat (or for the first time)."""
    pid = int(args.get("proposal_id"))
    with ctx.conn() as c:
        p = proposals.get_proposal(c, pid)
        it = agenda.item_of_proposal(c, pid)
    if p is None:
        raise ToolError(f"proposal #{pid} does not exist")
    if p["state"] != "pending":
        raise ToolError(f"#{pid} is {p['state']}; only a pending card has buttons")
    card = (it or {}).get("cards", {}).get(str(pid))
    res = outbox.send_card(ctx.led, ctx.box, ctx.cfg, pid, card=card)
    return {"proposal_id": pid, "sent": bool(res.get("ok")),
            "note": "The card is now the newest message with buttons. The older copy says Moved down."}


def t_record_verdict(ctx: ToolContext, args: dict) -> dict:
    eid = int(args.get("event_id"))
    verdict = str(args.get("verdict") or "")
    if verdict not in ("intact", "strengthened", "weakened", "broken"):
        raise ToolError("verdict must be intact, strengthened, weakened or broken")
    summary = str(args.get("summary") or "").strip()
    if not summary:
        raise ToolError("give a summary of what changed")
    if ctx.job is not None and (ctx.job.kind != "analyze_event" or eid != ctx.job.event_id):
        # A job records the verdict of its own event only (an injected text must not close another).
        raise ToolError(f"this job analyzes event {ctx.job.event_id}, not event {eid}")
    with ctx.conn() as c:
        cur = c.execute("UPDATE events SET status='analyzed', verdict=?, analysis=?, analyzed_at=? "
                        "WHERE id=? AND status IN ('queued','analyzed')", (verdict, summary[:4000], store.now_iso(), eid))
        if cur.rowcount != 1:
            raise ToolError(f"event {eid} is not queued for analysis")
    if ctx.job is not None:
        ctx.job.verdict_recorded = True
    return {"event_id": eid, "verdict": verdict}


def t_withdraw_proposal(ctx: ToolContext, args: dict) -> dict:
    pid = int(args.get("proposal_id"))
    with ctx.led.transaction() as c:
        try:
            moved = proposals.withdraw(c, pid, created_by=ctx.role, now=store.now_iso())
        except proposals.ProposalError as e:
            raise ToolError(str(e))
        prop = proposals.get_proposal(c, pid)
    if moved and prop.get("tg_message_id"):
        ctx.box.set_label(prop["tg_message_id"], "Withdrawn")
    return {"proposal_id": pid, "withdrawn": moved}


def _file_with_job(ctx: ToolContext, proposal_id: int, superseded, *, card=None) -> bool:
    """An analyst job's card waits on the job's agenda item (the bridge sends it when the
    item comes up). The cards it replaced lose their buttons now. False outside a job: the
    caller sends the card at once."""
    if ctx.role != "analyst" or ctx.job is None or ctx.job.job_id is None:
        return False
    with ctx.conn() as c:
        it = agenda.job_draft(c, ctx.job.job_id, now=store.now_iso())
        agenda.attach(c, it["id"], proposal_id, card=card)
    outbox.label_superseded(ctx.led, ctx.box, superseded, proposal_id)
    return True


def t_propose_trade(ctx: ToolContext, args: dict) -> dict:
    origin = args.get("origin") or {}
    if not isinstance(origin, dict):
        raise ToolError('origin must be {"event_id": n} or {"owner_message_id": n}')
    if ctx.role == "analyst" and "event_id" not in origin:
        raise ToolError("the analyst proposes trades from an event only")
    if ctx.role == "analyst" and ctx.job is not None and (
            ctx.job.kind != "analyze_event" or int(origin.get("event_id") or 0) != int(ctx.job.event_id or 0)):
        raise ToolError(f"this job can propose a trade only from its own event ({ctx.job.event_id})")
    yf = proposals.prefetch_prices(ctx.led, [str(t) for t in args.get("tickers") or [] if t],
                                   yf_price_fn=ctx.yf_price_fn or quotes.yf_last_price)
    try:
        with ctx.led.transaction() as c:
            built = proposals.build_trade(
                ctx.led, c, cfg=ctx.cfg, tickers=args.get("tickers") or [], origin=origin,
                fraction=args.get("fraction"), rationale=args.get("rationale") or "",
                evidence=args.get("evidence") or [], created_by=ctx.role, role=ctx.role,
                turn_owner_ids=(ctx.turn.owner_message_ids if ctx.turn is not None else ()),
                yf_price_fn=yf)
    except proposals.ProposalError as e:
        raise ToolError(str(e))
    card = {"legs": built["legs"], "skipped": built["skipped"], "scaled": built["scaled"]}
    if _file_with_job(ctx, built["proposal_id"], built["superseded"], card=card):
        note = ("The card waits with your report. The desk brings both up with the owner when they are free. "
                "Only the owner's tap approves it.")
    else:
        outbox.send_trade_card(ctx.led, ctx.box, ctx.led.connect, built)
        note = "The card with Approve and Reject is sent. Only the owner's tap approves it."
    if ctx.job is not None:
        ctx.job.proposal_ids.append(built["proposal_id"])
    return {"proposal_id": built["proposal_id"], "fraction": built["fraction"],
            "legs": [{"ticker": l["ticker"], "side": l["side"], "dollars": l.get("dollars"),
                      "quantity": l.get("quantity"), "quote": l["quote"]} for l in built["legs"]],
            "skipped": built["skipped"], "superseded": built["superseded"], "expires_at": built["expires_at"],
            "note": note}


def t_propose_change(ctx: ToolContext, args: dict) -> dict:
    change = args.get("change")
    if not isinstance(change, dict):
        raise ToolError("change must be an object with a kind")
    try:
        with ctx.led.transaction() as c:
            built = proposals.build_change(c, ctx.cfg, change=change, rationale=args.get("rationale") or "",
                                           evidence=args.get("evidence") or [], created_by=ctx.role)
    except proposals.ProposalError as e:
        raise ToolError(str(e))
    if _file_with_job(ctx, built["proposal_id"], built["superseded"]):
        note = ("The card waits with your report. The desk brings both up with the owner when they are free. "
                "Only the owner's tap applies it.")
    else:
        outbox.send_change_card(ctx.led, ctx.box, ctx.cfg, built["proposal_id"], superseded=built["superseded"])
        note = "The card with Confirm and Reject is sent. Only the owner's tap applies it."
    if ctx.job is not None:
        ctx.job.proposal_ids.append(built["proposal_id"])
    if built["superseded"]:
        note += (" It replaces the older open card(s) " + ", ".join(f"#{s}" for s in built["superseded"]) +
                 " on the same subject; those buttons now say Replaced.")
    return {"proposal_id": built["proposal_id"], "change": built["change"],
            "superseded": built["superseded"], "note": note}


# --- schemas ---------------------------------------------------------------------------

def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
TICKERS = {"type": "array", "items": S, "maxItems": 20}
EVIDENCE = {"type": "array", "items": {"type": ["string", "object", "integer"]}, "maxItems": 30}

SPECS = {
    "portfolio_view": ("The latest broker snapshot with live prices: equity, cash, buying power, each position's "
                       "weight vs target vs band, day P&L, snapshot age.", _obj({})),
    "refresh_portfolio": ("Queue a broker snapshot and wait (up to 120 s) for it.",
                          _obj({"wait_seconds": I})),
    "get_quotes": ("yfinance last prices for up to 20 tickers.", _obj({"tickers": TICKERS}, ["tickers"])),
    "get_book": ("The premise, the target book (weights, bands, status, entry limits), sleeve totals, cash floor.", _obj({})),
    "list_companies": ("The research list. status: candidate, watch, holding, rejected or dropped.", _obj({"status": S})),
    "get_company": ("One company's dossier: profile, thesis, metrics, last findings, open events, levels, position.",
                    _obj({"ticker": S}, ["ticker"])),
    "search_research": ("Full-text search over documents, findings, notes and chat. kinds: document, finding, note, chat.",
                        _obj({"query": S, "ticker": S, "kinds": {"type": "array", "items": S}, "since": S, "limit": I},
                             ["query"])),
    "list_filings": ("A company's filings with tier and ingest state.",
                     _obj({"ticker": S, "forms": {"type": "array", "items": S}, "since": S, "limit": I}, ["ticker"])),
    "get_filing_sections": ("The stored sections of one filing, with document ids and sizes.",
                            _obj({"accession": S}, ["accession"])),
    "get_document": ("A slice of a stored text (max 20,000 chars).",
                     _obj({"document_id": I, "offset": I, "max_chars": I}, ["document_id"])),
    "get_financials": ("The XBRL table: revenue, gross profit, operating income, net income, OCF, capex, FCF, shares.",
                       _obj({"ticker": S, "period": S, "limit": I}, ["ticker"])),
    "get_metrics": ("The latest metrics row of a company.", _obj({"ticker": S}, ["ticker"])),
    "run_valuation": ("Compute a DCF, reverse DCF, EV/Sales or multiple valuation. Stores nothing. Each scenario cites "
                      "finding ids in an `evidence` list.",
                      _obj({"ticker": S, "method": S, "scenarios": {"type": "object"}}, ["ticker", "method", "scenarios"])),
    "get_technicals": ("Trend facts: regime, ADX, drawdown, Sharpe, momentum.", _obj({"ticker": S}, ["ticker"])),
    "get_transcript": ("The earnings-call transcript's document id; fetches it if the budget allows.",
                       _obj({"ticker": S, "quarter": S}, ["ticker"])),
    "get_etf_lookthrough": ("An ETF's holdings, weighted metrics and expense ratio.", _obj({"ticker": S}, ["ticker"])),
    "get_events": ("Events with verdicts.", _obj({"ticker": S, "status": S, "since": S, "limit": I})),
    "get_proposals": ("Proposals with legs and results.", _obj({"state": S, "limit": I})),
    "get_track_record": ("Executed decisions with outcomes.", _obj({"ticker": S})),
    "get_attribution": ("The read-only factor attribution of the book (beta).", _obj({})),
    "get_settings": ("The effective settings, the KILL state and the halt state.", _obj({})),
    "ledger_query": ("One read-only SELECT over the ledger, at most 200 rows.", _obj({"sql": S}, ["sql"])),
    "screen_companies": ("Find candidates: SEC full-text search, the yfinance screener, and the book ETFs' holdings.",
                         _obj({"query": S, "sector": S, "industry": S, "market_cap_min": N, "limit": I})),
    "react": ("Put one reaction emoji on an owner message. Pick it for what the message says.",
              _obj({"message_id": I, "emoji": S}, ["message_id", "emoji"])),
    "say": ("Send one short Telegram message (at most 1,200 characters).", _obj({"text": S}, ["text"])),
    "report": ("File your report for the desk (at most 3,000 characters). The desk brings it up with the owner "
               "when they are free, with your cards. A second call replaces the first.", _obj({"text": S}, ["text"])),
    "bring_up_next": ("Open the next item of the agenda and get its report and cards, to tell the owner about it "
                      "now. Only after the owner spoke in this turn, and only when no item is open.", _obj({})),
    "close_item": ("Close an agenda item the owner dealt with (tapped, decided, said next or skip). Its cards stay "
                   "open until tapped or expired.", _obj({"item_id": I, "reason": S}, ["item_id"])),
    "defer_item": ("Put an agenda item back in line; it does not come up for `hours` (1 to 72).",
                   _obj({"item_id": I, "hours": N}, ["item_id", "hours"])),
    "hold_agenda": ("No agenda item comes up by itself for `hours` (0 to 24; 0 lifts the hold).",
                    _obj({"hours": N}, ["hours"])),
    "show_card": ("Send a pending card again as the newest message, so the owner need not scroll. The old copy "
                  "loses its buttons.", _obj({"proposal_id": I}, ["proposal_id"])),
    "load_company": ("Load a company or ETF: filings, sections, XBRL, metrics, transcript.", _obj({"ticker": S}, ["ticker"])),
    "fetch_document": ("Fetch a web page and store its text so it can be quoted.", _obj({"url": S, "ticker": S}, ["url"])),
    "record_finding": ("Store a fact with an exact quote from a stored document. kind: market_size, growth, guidance, "
                       "margin, segment, customer, competition, risk, management, capital, other.",
                       _obj({"ticker": S, "kind": S, "claim": S, "quote": S, "document_id": I},
                            ["ticker", "kind", "claim", "quote", "document_id"])),
    "add_note": ("Store a note (for example an owner preference).",
                 _obj({"text": S, "ticker": S, "tags": {"type": "array", "items": S}}, ["text"])),
    "set_research_status": ("Set a research name to candidate, watch or rejected, with a reason.",
                            _obj({"ticker": S, "status": S, "reason": S}, ["ticker", "status", "reason"])),
    "log_news_event": ("Create a news event from a stored news document (feed date within 7 days).",
                       _obj({"ticker": S, "document_id": I}, ["ticker", "document_id"])),
    "start_deep_dive": ("Queue a deep-dive by the analyst; it reports back.", _obj({"ticker": S, "focus": S}, ["ticker"])),
    "record_verdict": ("Close an event's analysis: intact, strengthened, weakened or broken, and what changed.",
                       _obj({"event_id": I, "verdict": S, "summary": S}, ["event_id", "verdict", "summary"])),
    "withdraw_proposal": ("Withdraw a pending proposal the desk made.", _obj({"proposal_id": I}, ["proposal_id"])),
    "propose_trade": ("Propose a trade toward the target. Python sizes it; the owner taps Approve. origin is "
                      '{"event_id": n} or {"owner_message_id": n}; fraction (owner trades only) is 0.25, 0.5 or 1.0.',
                      _obj({"tickers": TICKERS, "origin": {"type": "object"}, "fraction": N, "rationale": S,
                            "evidence": EVIDENCE}, ["tickers", "origin", "rationale", "evidence"])),
    "propose_change": ("Propose a change. The owner taps Confirm. `change.kind` is one of: "
                       "add_company {ticker, sleeve, target_weight, band, entry {mode: now|at_or_below, price}, "
                       "funding [{ticker, target_weight}]}; drop_company {ticker, exit {mode: now|at_or_above, price}, "
                       "redistribute [{ticker, target_weight}]}; set_weights {changes [{ticker, target_weight, band}]}; "
                       "set_risk {key, value}; set_premise {text, goal {target_return_pct, horizon_months, benchmark}}; "
                       "set_thesis {ticker, premise_link, summary, drivers [], falsifiers [], "
                       "valuation {method, scenarios}, buy_below, trim_above}; resume_trading {}. "
                       "Use exactly these field names. Each accepted call sends a card to the owner.",
                       _obj({"change": {"type": "object"}, "rationale": S, "evidence": EVIDENCE},
                            ["change", "rationale", "evidence"])),
}

FUNCS = {name: globals()[f"t_{name}"] for name in SPECS}


TOOL_RESULT_MAX_CHARS = 100_000


def call(ctx: ToolContext, name: str, args: dict) -> dict:
    """Run one tool synchronously. Returns the SDK tool result dict. A result over
    ``TOOL_RESULT_MAX_CHARS`` is refused: the model narrows the call."""
    if name not in role_tool_names(ctx.role):
        return _err(f"{name} is not a {ctx.role} tool")
    try:
        res = _ok(FUNCS[name](ctx, args or {}))
        size = sum(len(str(b.get("text") or "")) for b in res.get("content") or [] if isinstance(b, dict))
        if size > TOOL_RESULT_MAX_CHARS:
            return _err(f"{name}: the result is {size:,} characters, over {TOOL_RESULT_MAX_CHARS:,}; narrow it")
        return res
    except ToolError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001 — a tool bug answers the model, it never kills the turn
        return _err(f"{name} failed: {type(e).__name__}: {e}")


def build_server(ctx: ToolContext):
    """The in-process MCP server ``desk`` with the role's tools."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def make(name):
        desc, schema = SPECS[name]

        async def handler(args):
            return await asyncio.to_thread(call, ctx, name, args)

        return tool(name, desc, schema)(handler)

    return create_sdk_mcp_server(name="desk", version="1.0.0",
                                 tools=[make(n) for n in role_tool_names(ctx.role)])
