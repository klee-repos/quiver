"""The desk agent's and the analyst's tools, scoped by role (plan §5.4).

Each tool takes typed input and returns compact JSON. Roles: D = the desk agent (chat),
A = the analyst (worker). The tool server is built per process with closures over one
``ToolContext``, which holds the role, the per-turn ``TurnState`` or per-job ``JobState``,
the ledger, the outbox, and the network seams.

No tool approves, executes, places or cancels an order by itself. ``approve_card`` names a
card and an owner message of the turn; the bridge's ``TurnState.approver`` then reads the
owner's own words in Python (``approvals.handle_words``) and decides, as a tap does. No tool
edits a file, runs a shell command, or changes code. A tool may queue a broker ``snapshot``
job, which can place nothing. The W3 test parses this file and enforces these rules.

Blocking work (SQLite, EDGAR, yfinance, a page fetch) runs in a worker thread, so a tool
never blocks the chat bridge's event loop: a button tap is answered while a turn runs.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

from lib import market, memory, notify
from lib.desk import (agenda, documents, edgar, etf, events, findings, learning, news, outbox, proposals, quotes,
                      reactions, settings as desk_settings, store, transcripts, valuation, xbrl)

SAY_LIMIT = 500
REPORT_LIMIT = 3000
# A report-style label at the start of a line or a sentence (prompts/desk.md "How you talk").
# say and report refuse it.
LABEL_RE = re.compile(r"(?mi)(?:^|(?<=[.!?] ))[ \t*_]*(good|bad|worrying|value|verdict|card|next|risks?|answer|net"
                      r"|bottom line)[ \t*_]*:")
DEEP_DIVES_PER_DAY = 6
REFRESH_MAX_WAIT_S = 120

# --- the registry -------------------------------------------------------------------

READ_TOOLS = (
    "portfolio_view", "refresh_portfolio", "get_quotes", "get_book", "list_companies", "get_company",
    "search_research", "list_filings", "get_filing_sections", "get_document", "get_financials",
    "get_metrics", "run_valuation", "price_ladder", "get_technicals", "get_transcript", "get_etf_lookthrough",
    "get_events", "get_proposals", "get_track_record", "get_attribution", "get_settings",
    "ledger_query", "screen_companies", "get_learning",
)
DESK_ONLY = ("react", "say", "set_research_status", "start_deep_dive", "bring_up_next", "close_item",
             "defer_item", "hold_agenda", "show_card", "promise_read", "add_rule", "retire_rule",
             "retire_lesson", "plan_trade", "approve_card")
ANALYST_ONLY = ("report", "record_verdict", "record_forecast")
SHARED_WRITE = ("load_company", "fetch_document", "record_finding", "add_note", "log_news_event",
                "withdraw_proposal", "propose_trade", "propose_change")
# The reflect job (plan §6.5) reads the journal and the research, files one report and
# changes the lessons. It cannot propose, record a finding or load a company.
REFLECT_TOOLS = ("get_learning", "get_track_record", "get_company", "get_events", "get_proposals",
                 "search_research", "get_document", "report", "curate_lessons")


def role_tool_names(role: str) -> list:
    if role == "desk":
        return list(READ_TOOLS) + list(DESK_ONLY) + list(SHARED_WRITE)
    if role == "analyst":
        return list(READ_TOOLS) + list(ANALYST_ONLY) + list(SHARED_WRITE)
    if role == "reflect":
        return list(REFLECT_TOOLS)
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
    # 0 means "do not wait"; only a missing value takes the 60 s default.
    wait = int(args["wait_seconds"]) if args.get("wait_seconds") is not None else 60
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


def t_price_ladder(ctx: ToolContext, args: dict) -> dict:
    t = _ticker(args.get("ticker"))
    with ctx.conn() as c:
        comp = store.get_company(c, t)
    if comp is None:
        raise ToolError(f"{t} is not loaded; call load_company")
    try:
        price, source = ctx.price(t), "live (yfinance)"
    except Exception:  # noqa: BLE001 - a feed outage falls back to the metrics row price
        price, source = None, ""
    with ctx.conn() as c:
        try:
            return valuation.price_ladder(c, t, cik=comp.get("cik"), price=price, price_source=source,
                                          kind=comp.get("kind") or "company", prices=args.get("prices") or [],
                                          ev_sales_targets=args.get("ev_sales_targets") or [])
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


def _desk_decision(row: dict) -> bool:
    """A ``decisions`` row of a desk trade. The ledger also holds the old bot's calls (June to
    October 2026); they are not the desk's record."""
    return "proposal_id" in str(row.get("proof_json") or "")


def t_get_track_record(ctx: ToolContext, args: dict) -> dict:
    if args.get("ticker"):
        t = _ticker(args["ticker"])
        rows = [r for r in ctx.led.decisions_with_outcomes(t, limit=200) if _desk_decision(r)][:20]
        return {"ticker": t, "scorecard": memory.build_scorecard(t, rows), "decisions": rows,
                "forecasts": "see get_learning for the graded forecasts"}
    with ctx.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT d.id, d.trade_date, d.ticker, d.signal, d.decision_price, o.directional_return, o.alpha, "
            "o.realized_pnl FROM decisions d LEFT JOIN outcomes o ON o.decision_id=d.id "
            "WHERE d.proof_json LIKE '%proposal_id%' ORDER BY d.id DESC LIMIT 40").fetchall()]
    scored = [r for r in rows if r["directional_return"] is not None]
    hits = sum(1 for r in scored if (r["directional_return"] or 0) * (1 if r["signal"] == "buy" else -1) > 0)
    return {"decisions": rows, "scored": len(scored), "hit_rate": round(hits / len(scored), 4) if scored else None,
            "realized_pnl": round(sum(r["realized_pnl"] or 0 for r in rows), 2)}


def t_get_learning(ctx: ToolContext, args: dict) -> dict:
    """The graded track record of the journal, the active lessons and the owner's rules. With
    a ticker: also that name's resolved and open forecasts."""
    t = _ticker(args["ticker"]) if args.get("ticker") else None
    with ctx.conn() as c:
        cal = learning.calibration(c, ticker=t)
        out = {"summary": learning.summary_line(cal), "scores": cal,
               "lessons": learning.lesson_lines(learning.lessons_for(
                   c, scope="desk" if ctx.role == "desk" else "analyst", ticker=t, limit=learning.LESSONS_ACTIVE_MAX)),
               "rules": learning.rule_lines(learning.active_rules(c))}
        if t:
            out["resolved"] = learning.record_lines(c, t, limit=12)
            out["open"] = [dict(r) for r in c.execute(
                "SELECT id, source, verdict, p_beat, stance, made_at, due_date FROM forecasts WHERE ticker=? "
                "AND resolved_at IS NULL ORDER BY id DESC LIMIT 12", (t,)).fetchall()]
    return out


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


def check_labels(text: str) -> None:
    """Refuse a report-style label ("Good:", "Worrying:", "Net:") at the start of a line or a sentence."""
    m = LABEL_RE.search(str(text or ""))
    if m:
        raise ToolError(f"do not start a line with a label like '{m.group(0).strip()}'. Write it as a sentence "
                        "that says what the fact means for the owner, then send it again")


def check_say(turn, text: str) -> None:
    """The ``say`` rules (§4.4 step 1, §4.2 step 3): one message per turn, at most
    ``SAY_LIMIT`` characters, no report-style label, and a reaction on every owner message of
    the turn first. One short message, then the owner answers (owner, 2026-11-09: "present
    me one at a time ... do not send it in one wall")."""
    if not str(text or "").strip():
        raise ToolError("say needs text")
    if turn.said:
        raise ToolError("you already sent this turn's one message. Stop here and wait for the owner; "
                        "say the next thing in your next turn, after they answer")
    if len(text) > SAY_LIMIT:
        raise ToolError(f"that message is {len(text)} characters; the limit is {SAY_LIMIT}. Say only the "
                        "first point, in a few short sentences, and keep the rest for after the owner answers")
    check_labels(text)
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
    check_labels(text)
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


def t_promise_read(ctx: ToolContext, args: dict) -> dict:
    """Keep the promise "I'll send the read when it lands": the analyst's next report on the
    ticker comes up first (priority now), even when nothing changed."""
    t = _ticker(args.get("ticker"))
    with ctx.conn() as c:
        res = agenda.promise_read(c, t, now=store.now_iso())
    waiting = [{"item": it["id"], "title": it["title"], "report": it["body"]} for it in res["waiting"]]
    if not res["jobs"]:
        if waiting:
            raise ToolError(f"no analysis of {t} is queued or running, but its read is ready: tell the owner "
                            f"from the report below now, and do not promise a later one. Ready: "
                            + json.dumps(waiting)[:4000])
        raise ToolError(f"no analysis of {t} is queued or running, so nothing would come later. Do not promise "
                        f"a read; call start_deep_dive to research it")
    return {"ticker": t, "jobs": res["jobs"],
            "note": "Its report comes up first as soon as the analyst files it. Tell the owner you will bring it."}


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


def t_approve_card(ctx: ToolContext, args: dict) -> dict:
    """The owner said yes (or no, or cancel) to a card in words. The bridge's approver checks
    that the message is the owner's, in this turn, and says so plainly; then it decides the
    card as a tap does. The tool decides nothing itself."""
    turn = ctx.turn
    approver = getattr(turn, "approver", None) if turn is not None else None
    if approver is None:
        raise ToolError("approve_card works only in a chat turn")
    try:
        pid, mid = int(args.get("proposal_id")), int(args.get("owner_message_id"))
    except (TypeError, ValueError):
        raise ToolError("approve_card needs proposal_id and owner_message_id")
    res = approver(pid, mid)
    if not res.get("ok") and res.get("error"):
        raise ToolError(res["error"])
    return res


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
    job_id = getattr(ctx.job, "job_id", None) if ctx.job is not None else None
    with ctx.conn() as c:
        ev = c.execute("SELECT ticker, type FROM events WHERE id=?", (eid,)).fetchone()
        if ev is None:
            raise ToolError(f"event {eid} does not exist")
        against_itself = str(ev["ticker"]).upper() == learning.BENCHMARK
        try:
            p = learning.check_p(args.get("p_beat_benchmark"), required=not against_itself)
            used = learning.check_lessons_used(c, args.get("lessons_used"))
        except learning.ForecastError as e:
            raise ToolError(str(e))
        cur = c.execute("UPDATE events SET status='analyzed', verdict=?, analysis=?, analyzed_at=? "
                        "WHERE id=? AND status IN ('queued','analyzed')", (verdict, summary[:4000], store.now_iso(), eid))
        if cur.rowcount != 1:
            raise ToolError(f"event {eid} is not queued for analysis")
        fid = None
        if not against_itself:
            fid = _journal(c, ctx, source="verdict", ticker=ev["ticker"], p=p, stance=learning.stance_of(p),
                           reason=summary, horizon=learning.horizon_for("verdict", ev["type"]), verdict=verdict,
                           event_id=eid, job_id=job_id, used=used)
    if ctx.job is not None:
        ctx.job.verdict_recorded = True
        ctx.job.forecast_id = fid
    out = {"event_id": eid, "verdict": verdict}
    if fid:
        out["forecast"] = {"id": fid, "p_beat_benchmark": p, "horizon_days": learning.horizon_for("verdict", ev["type"]),
                           "benchmark": learning.BENCHMARK}
    return out


def _journal(c, ctx: ToolContext, *, source, ticker, p, stance, reason, horizon, verdict=None, event_id=None,
             job_id=None, proposal_id=None, used=()) -> int:
    """Write the call's forecast. A job keeps one forecast per source: a second call in the
    same job (or in its retry) replaces the first. The window moves to the new call's next
    close, so a restated chance never sees a price move inside its own window."""
    if job_id is not None and source in ("verdict", "deep_dive"):
        row = c.execute("SELECT id, horizon_days FROM forecasts WHERE job_id=? AND source=? AND resolved_at IS NULL "
                        "AND marks_json='{}'", (job_id, source)).fetchone()
        if row is not None:
            made = ctx.now()
            start = learning.start_session_for(made)
            c.execute("UPDATE forecasts SET p_beat=?, stance=?, verdict=?, reason=?, lesson_ids=?, made_at=?, "
                      "start_session=?, due_date=? WHERE id=?",
                      (p, stance, verdict, " ".join(str(reason).split())[:learning.REASON_MAX],
                       json.dumps(list(used)), store.iso(made), start, learning.add_days(start, row[1]), row[0]))
            return int(row[0])
    return learning.record_forecast(
        c, source=source, ticker=ticker, p_beat=p, stance=stance, made_at=ctx.now(),
        created_by="analyst" if ctx.role == "analyst" else "desk", reason=reason, horizon_days=horizon,
        verdict=verdict, event_id=event_id, job_id=job_id, proposal_id=proposal_id, lessons_used=used)


def t_record_forecast(ctx: ToolContext, args: dict) -> dict:
    """The deep-dive's forecast (an event job states its chance in record_verdict)."""
    if ctx.job is None or ctx.job.kind != "deep_dive" or ctx.job.job_id is None:
        raise ToolError("record_forecast works inside a deep-dive job; an event analysis states it in record_verdict")
    t = _ticker(args.get("ticker"))
    if ctx.job.ticker and t != ctx.job.ticker:
        raise ToolError(f"this deep-dive is on {ctx.job.ticker}, not {t}")
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ToolError("give a reason: the one or two facts behind the chance")
    with ctx.conn() as c:
        try:
            p = learning.check_p(args.get("p_beat_benchmark"), required=True)
            used = learning.check_lessons_used(c, args.get("lessons_used"))
            fid = _journal(c, ctx, source="deep_dive", ticker=t, p=p, stance=learning.stance_of(p), reason=reason,
                           horizon=learning.horizon_for("deep_dive"), job_id=ctx.job.job_id, used=used)
        except learning.ForecastError as e:
            raise ToolError(str(e))
    ctx.job.forecast_id = fid
    return {"forecast_id": fid, "ticker": t, "p_beat_benchmark": p, "horizon_days": learning.horizon_for("deep_dive"),
            "benchmark": learning.BENCHMARK}


def t_curate_lessons(ctx: ToolContext, args: dict) -> dict:
    if ctx.role != "reflect" or ctx.job is None or ctx.job.job_id is None:
        raise ToolError("curate_lessons works inside a reflect job only")
    with ctx.conn() as c:
        try:
            res = learning.apply_ops(c, args.get("ops"), job_id=ctx.job.job_id, now_iso=store.now_iso())
        except learning.LessonError as e:
            raise ToolError(str(e))
    return res


def _owner_message(ctx: ToolContext, name: str, raw) -> int:
    """The owner message id that a rule cites. It must be one of this turn's owner messages."""
    _owner_turn(ctx, name)
    try:
        mid = int(raw)
    except (TypeError, ValueError):
        raise ToolError("give owner_message_id: the id of the owner message that states the rule")
    if mid not in ctx.turn.owner_message_ids:
        raise ToolError(f"message {mid} is not an owner message of this turn")
    return mid


def t_add_rule(ctx: ToolContext, args: dict) -> dict:
    mid = _owner_message(ctx, "add_rule", args.get("owner_message_id"))
    t = _ticker(args["ticker"]) if args.get("ticker") else None
    with ctx.conn() as c:
        try:
            rid = learning.add_rule(c, text=str(args.get("text") or ""), owner_message_id=mid, ticker=t,
                                    now_iso=store.now_iso())
        except learning.LessonError as e:
            raise ToolError(str(e))
    _notice(ctx, f"I saved this as a standing rule (rule {rid}): \"{_norm(args.get('text'))}\" "
                 "Tell me to drop it any time.")
    return {"rule_id": rid, "note": "Every desk turn and every analyst job now sees this rule. Python sent the owner a "
                                    "short notice with its exact words, so do not repeat the rule."}


def t_retire_rule(ctx: ToolContext, args: dict) -> dict:
    _owner_turn(ctx, "retire_rule")
    with ctx.conn() as c:
        try:
            learning.retire_rule(c, args.get("rule_id"), reason=str(args.get("reason") or ""), now_iso=store.now_iso())
        except learning.LessonError as e:
            raise ToolError(str(e))
        text = c.execute("SELECT text FROM owner_rules WHERE id=?", (int(args.get("rule_id")),)).fetchone()[0]
    _notice(ctx, f"I dropped rule {int(args.get('rule_id'))}: \"{text}\"")
    return {"rule_id": int(args.get("rule_id")), "retired": True}


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _notice(ctx: ToolContext, text: str) -> None:
    """A fixed Python message to the owner for each change to the rules and the lessons. A
    rule or a retirement that a planted document talked the desk into is seen at once."""
    ctx.box.send(text, source="system", html=False, delivered=1)


def t_retire_lesson(ctx: ToolContext, args: dict) -> dict:
    """The owner can drop a lesson. A lesson is the desk's inference; the owner outranks it."""
    _owner_turn(ctx, "retire_lesson")
    with ctx.conn() as c:
        try:
            learning.retire_lesson(c, args.get("lesson_id"),
                                   reason="the owner: " + (str(args.get("reason") or "").strip() or "asked to drop it"),
                                   by="owner", now_iso=store.now_iso())
        except learning.LessonError as e:
            raise ToolError(str(e))
        text = c.execute("SELECT text FROM lessons WHERE id=?", (int(args.get("lesson_id")),)).fetchone()[0]
    _notice(ctx, f"I dropped lesson {int(args.get('lesson_id'))}: \"{text}\"")
    return {"lesson_id": int(args.get("lesson_id")), "retired": True}


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


def _held(ctx: ToolContext, entry: tuple) -> bool:
    """Hold a card or a Python note for the end of the desk turn (the bridge sends ``after``
    once the desk's last message is out), so the card is the newest message. False outside
    a bridge turn: the caller sends at once."""
    if ctx.role != "desk" or ctx.turn is None or not getattr(ctx.turn, "hold_cards", False):
        return False
    ctx.turn.after.append(entry)
    return True


def _sizing_args(args: dict) -> tuple:
    """``(orders, fund_from, every ticker named)`` of a trade request (§10.6)."""
    orders = args.get("orders")
    if orders is not None and (not isinstance(orders, list) or not all(isinstance(o, dict) for o in orders)):
        raise ToolError("orders is a list of {ticker, buy_dollars | sell_dollars | sell_fraction | target_weight | "
                        "sell_all}")
    fund_from = args.get("fund_from")
    if fund_from is not None and not isinstance(fund_from, list):
        raise ToolError('fund_from is a list of tickers to sell to pay for the buys, or ["cash"]')
    names = [str(t) for t in args.get("tickers") or [] if t]
    names += [str(o.get("ticker")) for o in orders or [] if o.get("ticker")]
    names += [str(t) for t in fund_from or [] if t and str(t).lower() != "cash"]
    return orders or None, fund_from, names


def _trade_out(built: dict) -> dict:
    return {"legs": [{"ticker": l["ticker"], "side": l["side"], "dollars": l.get("dollars"),
                      "quantity": l.get("quantity"), "quote": l["quote"],
                      "weight_now": round(l["weight_now"], 2) if l.get("weight_now") is not None else None,
                      "weight_after": round(l["weight_after"], 2) if l.get("weight_after") is not None else None}
                     for l in built["legs"]],
            "skipped": built["skipped"], "plan": built.get("plan"), "cash": built.get("cash")}


def t_plan_trade(ctx: ToolContext, args: dict) -> dict:
    """Work out an owner trade with Python's numbers and send nothing (§10.6)."""
    origin = args.get("origin") or {}
    if not isinstance(origin, dict) or "owner_message_id" not in origin:
        raise ToolError('origin must be {"owner_message_id": n}: plan_trade works out a trade the owner asks for')
    orders, fund_from, names = _sizing_args(args)
    yf = proposals.prefetch_prices(ctx.led, names, yf_price_fn=ctx.yf_price_fn or quotes.yf_last_price)
    try:
        res = proposals.preview_trade(
            ctx.led, ctx.cfg, tickers=args.get("tickers") or [], origin=origin, fraction=args.get("fraction"),
            rationale="A preview for the owner.", evidence=[], created_by=ctx.role, role=ctx.role,
            turn_owner_ids=(ctx.turn.owner_message_ids if ctx.turn is not None else ()), yf_price_fn=yf,
            orders=orders, fund_from=fund_from)
    except proposals.ProposalError as e:
        raise ToolError(str(e))
    return {**_trade_out(res), "deployable": round(res["deployable"], 2), "scaled": res["scaled"],
            "note": ("Nothing was sent. Talk the size and the money through with the owner from these numbers. "
                     "propose_trade with the same inputs sends this card.")}


def t_propose_trade(ctx: ToolContext, args: dict) -> dict:
    origin = args.get("origin") or {}
    if not isinstance(origin, dict):
        raise ToolError('origin must be {"event_id": n} or {"owner_message_id": n}')
    if ctx.role == "analyst" and "event_id" not in origin:
        raise ToolError("the analyst proposes trades from an event only")
    if ctx.role == "analyst" and ctx.job is not None and (
            ctx.job.kind != "analyze_event" or int(origin.get("event_id") or 0) != int(ctx.job.event_id or 0)):
        raise ToolError(f"this job can propose a trade only from its own event ({ctx.job.event_id})")
    p, used, fnote = _card_forecast_args(ctx, args)
    orders, fund_from, names = _sizing_args(args)
    evidence, enote = (_owner_evidence(args) if "owner_message_id" in origin
                       else (args.get("evidence") or [], None))
    yf = proposals.prefetch_prices(ctx.led, names, yf_price_fn=ctx.yf_price_fn or quotes.yf_last_price)
    try:
        with ctx.led.transaction() as c:
            built = proposals.build_trade(
                ctx.led, c, cfg=ctx.cfg, tickers=args.get("tickers") or [], origin=origin,
                fraction=args.get("fraction"), rationale=args.get("rationale") or "",
                evidence=evidence, created_by=ctx.role, role=ctx.role,
                turn_owner_ids=(ctx.turn.owner_message_ids if ctx.turn is not None else ()),
                yf_price_fn=yf, orders=orders, fund_from=fund_from)
            sides = {}
            for leg in built["legs"]:
                sides.setdefault(str(leg["ticker"]).upper(), 1 if leg["side"] == "buy" else -1)
            journal = _card_journal(c, ctx, built["proposal_id"], sides, p, used, args.get("rationale") or "",
                                    event_id=origin.get("event_id"))
    except proposals.ProposalError as e:
        raise ToolError(str(e))
    card = {"legs": built["legs"], "skipped": built["skipped"], "scaled": built["scaled"]}
    if _file_with_job(ctx, built["proposal_id"], built["superseded"], card=card):
        note = ("The card waits with your report. The desk brings both up with the owner when they are free. "
                "Only the owner's tap approves it.")
    elif _held(ctx, ("card", built["proposal_id"], card)):
        outbox.label_superseded(ctx.led, ctx.box, built["superseded"], built["proposal_id"])
        note = ("The card with Approve and Reject goes out right after your last message, so it is the newest "
                "message. Only the owner's tap approves it.")
    else:
        outbox.send_trade_card(ctx.led, ctx.box, ctx.led.connect, built)
        note = "The card with Approve and Reject is sent. Only the owner's tap approves it."
    if ctx.job is not None:
        ctx.job.proposal_ids.append(built["proposal_id"])
    if built.get("plan"):
        note += " When the order fills, the plan moves with it (the card says how), so nothing else is needed."
    note += _held_note()
    return {"proposal_id": built["proposal_id"], "fraction": built["fraction"], **_trade_out(built),
            "superseded": built["superseded"], "expires_at": built["expires_at"],
            "forecasts": journal, **({"forecast_note": fnote} if fnote else {}),
            **({"evidence_note": enote} if enote else {}), "note": note}


def _held_note() -> str:
    """The tool note when the market cannot take an order now (plan §10.5 check 4)."""
    held = market.held_send_et()
    if held is None:
        return ""
    return (f" The market cannot take the order now. The owner can approve at any time (a tap or a yes): Python "
            f"then holds the order and sends it at {market.fmt_when_et(held[0])}. The card says so; do not repeat it.")


def _owner_follow_up(ctx: ToolContext, pid: int) -> dict:
    """Build and send the trade that follows a change the owner made in chat (§10.6)."""
    with ctx.conn() as c:
        prop = proposals.get_proposal(c, pid)
    yf = proposals.prefetch_prices(ctx.led, proposals.change_tickers(store.loads(prop.get("payload_json"), {})),
                                   yf_price_fn=ctx.yf_price_fn or quotes.yf_last_price)
    with ctx.led.transaction() as c:
        fu = proposals.build_follow_up(ctx.led, c, ctx.cfg, prop, yf_price_fn=yf)
        if fu.get("waiting"):
            c.execute("UPDATE proposals SET result_json=? WHERE id=?",
                      (store.canonical_json({"follow_up": "waiting"}), pid))
            store.enqueue_snapshot_job(c, requested_by="follow_up", now=store.now_iso())
    if fu.get("built"):
        b = fu["built"]
        if _held(ctx, ("card", b["proposal_id"], {"legs": b["legs"], "skipped": b["skipped"], "scaled": b["scaled"]})):
            outbox.label_superseded(ctx.led, ctx.box, b["superseded"], b["proposal_id"])
        else:
            outbox.send_trade_card(ctx.led, ctx.box, ctx.led.connect, b)
        return {"follow_up": {"proposal_id": b["proposal_id"], **_trade_out(b)},
                "follow_note": "Its trade card with Approve and Reject goes to the owner as the newest message. Only "
                               "the owner's tap sends the order." + _held_note()}
    if fu.get("waiting"):
        ctx.trigger("broker")
        return {"follow_up": "waiting",
                "follow_note": "The portfolio snapshot is old, so the trade card comes after a fresh snapshot, in a "
                               "few minutes. Tell the owner."}
    if fu.get("refused"):
        return {"follow_up": {"refused": fu["refused"]},
                "follow_note": "No trade card: " + fu["refused"] + ". Tell the owner in plain words and settle it."}
    if fu.get("why"):
        return {"follow_up": {"none": fu["why"]},
                "follow_note": "No trade follows now: " + fu["why"] + ". Tell the owner why in plain words. To trade "
                               "anyway, use propose_trade with orders."}
    return {"follow_up": None, "follow_note": "No trade follows this change."}


def _owner_evidence(args: dict) -> tuple:
    """The evidence of an owner request: the owner's message is the reason, so an item that
    is not a finding, a document or a metric is dropped, never a reason to refuse."""
    keep, dropped = [], []
    for e in args.get("evidence") or []:
        try:
            proposals.evidence_items([e])
            keep.append(e)
        except proposals.ProposalError:
            dropped.append(e)
    return keep, (f"evidence items that are not a finding, document or metric were dropped: {dropped}"
                  if dropped else None)


def _apply_from_owner(ctx: ToolContext, args: dict, change: dict, origin: dict) -> dict:
    """A plan change the owner asked for in this turn: it applies at once, with no card."""
    p, used, fnote = _card_forecast_args(ctx, args)
    evidence, enote = _owner_evidence(args)
    try:
        with ctx.led.transaction() as c:
            res = proposals.apply_owner_change(
                ctx.led, c, ctx.cfg, change=change, rationale=args.get("rationale") or "",
                evidence=evidence, owner_message_id=origin.get("owner_message_id"),
                turn_owner_ids=(ctx.turn.owner_message_ids if ctx.turn is not None else ()), role=ctx.role)
            journal = _card_journal(c, ctx, res["proposal_id"], _change_sides(res["change"], res["before"]), p, used,
                                    args.get("rationale") or "")
    except proposals.ProposalError as e:
        raise ToolError(str(e))
    outbox.label_superseded(ctx.led, ctx.box, res["superseded"], res["proposal_id"])
    # A fixed Python note with the exact numbers: a change a planted text talked the desk
    # into is seen at once.
    note_text = notify.render_change_applied({"change": res["change"], "before": res["before"]})
    if not _held(ctx, ("text", note_text, None)):
        ctx.box.send(note_text, source="system", delivered=1)
    out = {"proposal_id": res["proposal_id"], "applied": True, "change": res["change"],
           "superseded": res["superseded"], "forecasts": journal, **({"forecast_note": fnote} if fnote else {}),
           **({"evidence_note": enote} if enote else {})}
    out.update(_owner_follow_up(ctx, res["proposal_id"]))
    out["note"] = ("The change is in. Python sends the owner a short note with the exact numbers, so do not repeat "
                   "them. " + out.pop("follow_note"))
    return out


def t_propose_change(ctx: ToolContext, args: dict) -> dict:
    change = args.get("change")
    if not isinstance(change, dict):
        raise ToolError("change must be an object with a kind")
    origin = args.get("origin")
    if origin is not None:
        if not isinstance(origin, dict) or set(origin) != {"owner_message_id"}:
            raise ToolError('origin must be {"owner_message_id": n}: the owner message of this turn that asks for it')
        if ctx.role != "desk":
            raise ToolError("only the desk applies a change from the owner's message; file a card without origin")
        if change.get("kind") not in proposals.TAP_ONLY_KINDS:
            return _apply_from_owner(ctx, args, change, origin)
    p, used, fnote = _card_forecast_args(ctx, args)
    try:
        with ctx.led.transaction() as c:
            goal = proposals.active_goal(c)
            before = {str(r["ticker"]).upper(): float(r["target_weight"] or 0)
                      for r in (proposals.book_rows(c, goal["id"]) if goal else [])}
            built = proposals.build_change(c, ctx.cfg, change=change, rationale=args.get("rationale") or "",
                                           evidence=args.get("evidence") or [], created_by=ctx.role)
            journal = _card_journal(c, ctx, built["proposal_id"], _change_sides(built["change"], before), p, used,
                                    args.get("rationale") or "")
    except proposals.ProposalError as e:
        raise ToolError(str(e))
    if _file_with_job(ctx, built["proposal_id"], built["superseded"]):
        note = ("The card waits with your report. The desk brings both up with the owner when they are free. "
                "Only the owner's tap applies it.")
    elif _held(ctx, ("card", built["proposal_id"], None)):
        outbox.label_superseded(ctx.led, ctx.box, built["superseded"], built["proposal_id"])
        note = ("The card with Confirm and Reject goes out right after your last message. Only the owner's tap "
                "applies it.")
    else:
        outbox.send_change_card(ctx.led, ctx.box, ctx.cfg, built["proposal_id"], superseded=built["superseded"])
        note = "The card with Confirm and Reject is sent. Only the owner's tap applies it."
    if ctx.job is not None:
        ctx.job.proposal_ids.append(built["proposal_id"])
    if built["superseded"]:
        note += (" It replaces the older open card(s) " + ", ".join(f"#{s}" for s in built["superseded"]) +
                 " on the same subject; those buttons now say Replaced.")
    if origin is not None:
        note += " It changes a safety limit, so it waits for the owner's Confirm tap; say so in one sentence."
    return {"proposal_id": built["proposal_id"], "change": built["change"],
            "superseded": built["superseded"], "forecasts": journal, **({"forecast_note": fnote} if fnote else {}),
            "note": note}


def _card_forecast_args(ctx: ToolContext, args: dict) -> tuple:
    """The card's optional stated chance and the lessons it used. A bad value is dropped, never
    a reason to refuse the card: ``(chance, lessons, note)``."""
    note = None
    with ctx.conn() as c:
        try:
            p = learning.check_p(args.get("p_beat_benchmark"), required=False)
        except learning.ForecastError as e:
            p, note = None, f"the chance was dropped: {e}"
        try:
            used = learning.check_lessons_used(c, args.get("lessons_used"))
        except learning.ForecastError as e:
            used, note = [], (note + "; " if note else "") + f"lessons_used was dropped: {e}"
    return p, used, note


def _change_sides(norm: dict, before: dict) -> dict:
    """The side a change card takes on each name: an add or a larger planned share favors the
    name; a drop or a smaller planned share goes against it. Other kinds take no side."""
    kind = norm.get("kind")
    if kind == "add_company":
        return {str(norm["ticker"]).upper(): 1}
    if kind == "drop_company":
        return {str(norm["ticker"]).upper(): -1}
    if kind == "set_weights":
        out = {}
        for ch in norm.get("changes") or []:
            t = str(ch["ticker"]).upper()
            delta = float(ch["target_weight"]) - before.get(t, 0.0)
            if delta:
                out[t] = 1 if delta > 0 else -1
        return out
    return {}


def _card_journal(c, ctx: ToolContext, proposal_id: int, sides: dict, p, used, rationale: str,
                  event_id=None) -> list:
    """One forecast per name a card takes a side on. The cash fund takes no side: a card moves
    it only to fund or to park the other names. A stated chance applies to a card on one
    name only. A journal refusal (the benchmark itself) never stops the card."""
    cash = {str(ctx.cfg.risk.cash_sleeve_ticker or "").upper()}
    cash |= {str(r[0]).upper() for r in c.execute("SELECT ticker FROM companies WHERE kind='cash'").fetchall()}
    sides = {t: s for t, s in sides.items() if t not in cash}
    out = []
    for t, side in sides.items():
        chance = p if len(sides) == 1 else None
        if chance is not None and learning.stance_of(chance) not in (0, side):
            chance = None  # a chance that contradicts the card's side is dropped, not graded
        try:
            fid = _journal(c, ctx, source="proposal", ticker=t, p=chance, stance=side,
                           reason=rationale or f"card #{proposal_id}", horizon=learning.horizon_for("proposal"),
                           event_id=event_id, job_id=getattr(ctx.job, "job_id", None) if ctx.job else None,
                           proposal_id=proposal_id, used=used)
        except learning.ForecastError:
            continue
        out.append({"forecast_id": fid, "ticker": t, "side": side, "p_beat_benchmark": chance})
    return out


# --- schemas ---------------------------------------------------------------------------

def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
TICKERS = {"type": "array", "items": S, "maxItems": 20}
EVIDENCE = {"type": "array", "items": {"type": ["string", "object", "integer"]}, "maxItems": 30}
P_BEAT = {"type": "number", "minimum": 0.01, "maximum": 0.99}
LESSONS_USED = {"type": "array", "items": I, "maxItems": 12}
ORDERS = {"type": "array", "maxItems": 10, "items": {"type": "object", "properties": {
    "ticker": S, "buy_dollars": N, "sell_dollars": N, "sell_fraction": N, "target_weight": N,
    "sell_all": {"type": "boolean"}},
    "required": ["ticker"], "additionalProperties": False}}
FUND_FROM = {"type": "array", "items": S, "maxItems": 5}

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
    "price_ladder": ("At each price of the thesis (the three cases, buy more, sell some), today's price and any "
                     "extra `prices`: what the whole company costs, its multiple of a year of sales and of profit, "
                     "and the analyst's bet for the case, all on one share count. `ev_sales_targets` (sales "
                     "multiples) gives the price at each multiple today and the sales growth today's price needs. "
                     "Use these numbers; never compute a multiple yourself.",
                     _obj({"ticker": S, "prices": {"type": "array", "items": N, "maxItems": 6},
                           "ev_sales_targets": {"type": "array", "items": N, "maxItems": 6}}, ["ticker"])),
    "get_technicals": ("Trend facts: regime, ADX, drawdown, Sharpe, momentum.", _obj({"ticker": S}, ["ticker"])),
    "get_transcript": ("The earnings-call transcript's document id; fetches it if the budget allows.",
                       _obj({"ticker": S, "quarter": S}, ["ticker"])),
    "get_etf_lookthrough": ("An ETF's holdings, weighted metrics and expense ratio.", _obj({"ticker": S}, ["ticker"])),
    "get_events": ("Events with verdicts.", _obj({"ticker": S, "status": S, "since": S, "limit": I})),
    "get_proposals": ("Proposals with legs and results.", _obj({"state": S, "limit": I})),
    "get_track_record": ("The desk's executed trades with outcomes (the old bot's calls are left out).",
                         _obj({"ticker": S})),
    "get_learning": ("The graded track record of every forecast (Brier score, skill, reliability, resolution, "
                     "by verdict, by card and what the owner did with it), the active lessons and the owner's "
                     "rules. With ticker: that name's resolved and open forecasts.", _obj({"ticker": S})),
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
    "approve_card": ("The owner said yes, do it, no, or cancel to a card in their message of this turn: decide "
                     "that card now, as their tap would. Name the card and the owner's message id. Python reads "
                     "the owner's own words and refuses a message that does not plainly say yes or no. Never use "
                     "it without the owner's words.", _obj({"proposal_id": I, "owner_message_id": I},
                                                            ["proposal_id", "owner_message_id"])),
    "promise_read": ("Before you tell the owner you will come back with the analyst's read on a ticker, call this: "
                  "its next report then comes up first, even when nothing changed. Refused when no analysis of "
                  "the ticker is queued or running.", _obj({"ticker": S}, ["ticker"])),
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
    "record_verdict": ("Close an event's analysis: intact, strengthened, weakened or broken, and what changed. "
                       "p_beat_benchmark is your chance (0.01 to 0.99) that the name's price beats SPY's over the "
                       "horizon in the job; 0.5 means no edge. Python grades it. lessons_used: the ids of the lessons "
                       "you applied.",
                       _obj({"event_id": I, "verdict": S, "summary": S, "p_beat_benchmark": P_BEAT,
                             "lessons_used": LESSONS_USED}, ["event_id", "verdict", "summary"])),
    "record_forecast": ("A deep-dive's forecast: your chance (0.01 to 0.99) that the name's price beats SPY's "
                        "over the next 90 days, and the one or two facts behind it. Python grades it. "
                        "lessons_used: the ids of the lessons you applied.",
                        _obj({"ticker": S, "p_beat_benchmark": P_BEAT, "reason": S, "lessons_used": LESSONS_USED},
                             ["ticker", "p_beat_benchmark", "reason"])),
    "curate_lessons": ("Change the lessons. ops: [{op: add, scope: analyst|desk|both, ticker?, text, evidence: "
                       "[resolved forecast ids, at least 3]}, {op: retire, lesson_id, reason, evidence: [resolved "
                       "forecast ids]}]. Python checks each op and answers per op.",
                       _obj({"ops": {"type": "array", "items": {"type": "object"}, "maxItems": 10}}, ["ops"])),
    "add_rule": ("Store a standing rule the owner states in this turn (\"always\", \"never\", \"keep X at\"). "
                 "Every desk turn and analyst job sees it. owner_message_id: the owner message that states it.",
                 _obj({"text": S, "owner_message_id": I, "ticker": S}, ["text", "owner_message_id"])),
    "retire_rule": ("Retire an owner rule when the owner drops or changes it.",
                    _obj({"rule_id": I, "reason": S}, ["rule_id", "reason"])),
    "retire_lesson": ("Retire a lesson when the owner disagrees with it.",
                      _obj({"lesson_id": I, "reason": S}, ["lesson_id"])),
    "withdraw_proposal": ("Withdraw a pending proposal the desk made.", _obj({"proposal_id": I}, ["proposal_id"])),
    "propose_trade": ("Propose a trade. Python sizes it and sends the card; the owner taps Approve. origin is "
                      '{"event_id": n} or {"owner_message_id": n}. An owner trade takes either tickers with '
                      "fraction (0.25, 0.5 or 1.0 of the way to the plan), or orders: [{ticker, buy_dollars | "
                      "sell_dollars | sell_fraction (0.5 = half the holding) | target_weight (percent of the "
                      'account) | sell_all: true}] with fund_from (holdings to sell to pay for the buys; default '
                      '["cash"]). With orders, Python moves the plan to fit the trade when the order fills, so the '
                      "plan never blocks a trade the owner asks for. "
                      "p_beat_benchmark (optional, one name only): your chance the name beats SPY over 90 days.",
                      _obj({"tickers": TICKERS, "origin": {"type": "object"}, "fraction": N, "orders": ORDERS,
                            "fund_from": FUND_FROM, "rationale": S, "evidence": EVIDENCE, "p_beat_benchmark": P_BEAT,
                            "lessons_used": LESSONS_USED},
                           ["origin", "rationale"])),
    "plan_trade": ("Work out a trade the owner asks for, and send nothing: the same inputs as propose_trade "
                   '(origin {"owner_message_id": n}, orders or tickers with fraction, fund_from). Returns the exact '
                   "orders, how the plan moves, and the cash before and after. Use it to settle the size and where "
                   "the money comes from with the owner.",
                   _obj({"tickers": TICKERS, "origin": {"type": "object"}, "fraction": N, "orders": ORDERS,
                         "fund_from": FUND_FROM}, ["origin"])),
    "propose_change": ("Change the plan. With origin {\"owner_message_id\": n} (the owner's message in this turn "
                       "that asks for it), a plan change applies at once with no card: Python sends the owner a "
                       "short note and the trade card that follows, if any. set_risk and resume_trading always "
                       "send a Confirm card. Without origin, a card with Confirm and Reject goes to the owner. "
                       "Leave the cash sleeve out and Python balances the plan on it. `change.kind` is one of: "
                       "add_company {ticker, sleeve, target_weight, band, entry {mode: now|at_or_below, price}, "
                       "funding [{ticker, target_weight}]}; drop_company {ticker, exit {mode: now|at_or_above, price}, "
                       "redistribute [{ticker, target_weight}]}; set_weights {changes [{ticker, target_weight, band}]}; "
                       "set_risk {key, value}; set_premise {text, goal {target_return_pct, horizon_months, benchmark}}; "
                       "set_thesis {ticker, premise_link, summary, drivers [], falsifiers [], "
                       "valuation {method, scenarios}, buy_below, trim_above}; resume_trading {}. "
                       "Use exactly these field names.",
                       _obj({"change": {"type": "object"}, "origin": {"type": "object"}, "rationale": S,
                             "evidence": EVIDENCE, "p_beat_benchmark": P_BEAT, "lessons_used": LESSONS_USED},
                            ["change", "rationale"])),
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
