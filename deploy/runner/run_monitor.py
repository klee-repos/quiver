#!/usr/bin/env python3
"""The monitor: one hourly run of the desk's change detection (plan §9.1).

``quiver.timer`` fires Monday to Friday at :40, 11:40 to 23:40 UTC. Pure Python: no LLM and
no broker. Each step catches its own errors, logs them, and pages once per day with the
stage of its own name. The steps, in order:

  snapshot (queue a broker snapshot in or just after the session), edgar (the EDGAR sweep,
  the day's first metrics, pending transcripts), news, levels (session only), drift and the
  close brief (once a day, after the close), expire, queue (analysis jobs), path_unit, and
  after the close: reflect, benchmark, goal, prune. The last line is one JSON status line.

Under KILL the monitor still detects events and queues analysis: research is harmless.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import runner_common as rc  # noqa: E402
from lib import goal as goal_mod  # noqa: E402
from lib import market, notify, portfolio, runlock  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.desk import brief, edgar, events, levels, news, outbox, proposals, quotes  # noqa: E402
from lib.desk import settings as desk_settings, store, transcripts  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

ROOT = Path(os.environ.get("QUIVER_ROOT") or _REPO)
LOCK_TTL_S = 2400
SNAPSHOT_FRESH = timedelta(minutes=50)
SNAPSHOT_AFTER_CLOSE = timedelta(minutes=45)
DAILY_AFTER_CLOSE = timedelta(minutes=15)
SNAPSHOT_WAIT_S = 180
NEWS_EVENT_MAX_AGE = timedelta(days=7)


def default_systemctl(args):
    try:
        p = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


class Monitor:
    def __init__(self, *, led=None, cfg=None, now_fn=None, sleep=time.sleep, seams=None, box=None,
                 systemctl=default_systemctl, tick=rc._tick_json, page=None, news_fetch=None,
                 yf_price_fn=None, fetch_document=None, earnings_fn=None):
        self.led = led or Ledger(ROOT / "state" / "ledger.db")
        self.cfg = cfg or load_config(ROOT / "config.yaml")
        self.now_fn = now_fn or store.utc_now
        self.sleep = sleep
        self.seams = seams
        self.systemctl = systemctl
        self.tick = tick
        self.news_fetch = news_fetch
        self.yf_price_fn = yf_price_fn or quotes.yf_last_price
        self.fetch_document = fetch_document
        self.earnings_fn = earnings_fn
        if box is None:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").replace(",", " ").split() or [None])[0]
            box = outbox.Outbox(self.led, transport=outbox.telegram_transport(token), chat_id=chat)
        self.box = box
        self._page = page
        self.status = {"event": "monitor_ok", "names": 0, "filings": 0, "events": 0, "proposals": 0}
        self.order = []
        self.failed = []

    # --- helpers --------------------------------------------------------------------
    def page(self, stage, detail, severity="critical"):
        day = market.trading_day_et()
        if self._page is not None:
            return self._page(stage=stage, detail=detail, day=day)
        rc._maybe_alert(self.led, kind="error", stage=stage, day=day, now_iso=store.iso(self.now_fn()),
                        event_detail=detail, severity=severity)

    def step(self, name, fn, *, severity="warning"):
        self.order.append(name)
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — one step never stops the others
            self.failed.append(name)
            rc._emit({"stage": name, "event": "stage_error", "error": f"{type(e).__name__}: {e}"})
            self.page(name, f"{type(e).__name__}: {e}",
                      severity="critical" if name in ("edgar", "snapshot", "path_unit") else severity)
            return None

    def get_seams(self):
        if self.seams is None:
            self.seams = events.Seams(client=edgar.client_from_env(),
                                      av_key=os.environ.get("ALPHA_VANTAGE_API_KEY") or "")
        return self.seams

    def settings(self):
        with self.led.connect() as conn:
            return desk_settings.effective(conn, self.cfg)

    def state(self, key, default=None):
        with self.led.connect() as conn:
            return store.get_state(conn, key, default)

    def set_state(self, key, value):
        with self.led.connect() as conn:
            store.set_state(conn, key, value)

    # --- the market ----------------------------------------------------------------
    def market_state(self):
        now = self.now_fn()
        now_et = now.astimezone(market.ET)
        self.now = now
        self.open = market.is_open_at(now_et)
        try:
            self.last_close = market.last_close_et(now_et)
        except RuntimeError:
            self.last_close = None
        self.day = now_et.strftime("%Y-%m-%d")
        self.after_close_today = bool(self.last_close and self.last_close.strftime("%Y-%m-%d") == self.day
                                      and now_et >= self.last_close + DAILY_AFTER_CLOSE)
        return {"open": self.open, "day": self.day}

    # --- §9.1 step 3 ---------------------------------------------------------------
    def snapshot(self):
        now_et = self.now.astimezone(market.ET)
        recent_close = self.last_close is not None and now_et - self.last_close < SNAPSHOT_AFTER_CLOSE \
            and now_et >= self.last_close
        if not (self.open or recent_close):
            return "not needed"
        cutoff = store.iso(self.now - SNAPSHOT_FRESH)
        with self.led.connect() as conn:
            fresh = conn.execute("SELECT 1 FROM broker_jobs WHERE kind='snapshot' AND created_at >= ? LIMIT 1",
                                 (cutoff,)).fetchone()
            if fresh:
                return "fresh job exists"
            before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM broker_snapshots").fetchone()[0]
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=store.iso(self.now))
        rc.touch_trigger(ROOT, "broker", store.iso(self.now))
        waited = 0
        while waited < SNAPSHOT_WAIT_S:
            with self.led.connect() as conn:
                newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM broker_snapshots").fetchone()[0]
            if newest > before:
                return "snapshot taken"
            self.sleep(5)
            waited += 5
        return "waited 180 s"

    # --- §9.1 step 4 ---------------------------------------------------------------
    def edgar_sweep(self):
        seams = self.get_seams()
        sett = self.settings()
        try:
            res = events.sweep(self.led, settings=sett, seams=seams)
        except edgar.EdgarBlocked as e:
            self.page("edgar", f"SEC returned 403: {e}")
            raise
        self.status["filings"] += res["filings"]
        self.status["events"] += len(res["events"])
        self.status["names"] = res["companies"]
        if res["watch_filings"]:
            key = f"watch_filings:{self.day}"
            prior = json.loads(self.state(key, "[]"))
            self.set_state(key, json.dumps(prior + res["watch_filings"]))
        # The day's first run: metrics at the new price.
        if self.state("metrics_day") != self.day:
            with self.led.connect() as conn:
                names = [dict(r) for r in conn.execute(
                    "SELECT ticker, cik FROM companies WHERE status IN ('holding','watch') AND kind='company' "
                    "AND cik IS NOT NULL").fetchall()]
            for n in names:
                events.compute_company_metrics(self.led, n["ticker"], cik=n["cik"], settings=sett, seams=seams)
            self.set_state("metrics_day", self.day)
        # Pending transcripts (§7.6 step 3).
        try:
            with self.led.connect() as conn:
                transcripts.try_pending(conn, api_key=seams.av_key or "", now=store.iso(self.now),
                                        opener=seams.av_opener)
        except Exception as e:  # noqa: BLE001
            self.page("transcripts", f"{type(e).__name__}: {e}", severity="warning")
        return res

    # --- §9.1 step 5 ---------------------------------------------------------------
    def news_sweep(self):
        with self.led.connect() as conn:
            n = news.ingest(conn, now=store.iso(self.now), fetch_fn=self.news_fetch)
            job = news.queue_classify(conn, store.iso(self.now))
        return {"new": n, "job": job}

    # --- §9.1 step 6 ---------------------------------------------------------------
    def check_levels(self):
        if not self.open:
            return "closed"
        with self.led.connect() as conn:
            res = levels.check_levels(conn, now_dt=self.now, yf_price_fn=self.yf_price_fn)
        self.status["events"] += len(res["events"])
        return res

    # --- §9.1 step 7 ---------------------------------------------------------------
    def post_close_snapshot(self):
        with self.led.connect() as conn:
            row = conn.execute("SELECT captured_at FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if not row or self.last_close is None:
            return False
        return store.parse_ts(row[0]) >= self.last_close

    def drift(self):
        if not self.after_close_today or self.state(f"drift_done:{self.day}") or not self.post_close_snapshot():
            return "not due"
        with self.led.connect() as conn:
            goal = proposals.active_goal(conn)
            snap = proposals.latest_snapshot(conn)
            rows = proposals.book_rows(conn, goal["id"], ("active",)) if goal else []
            base = proposals.valuation_base(conn, snap, self.now, self.yf_price_fn) if snap else None
        out = []
        cash = self.cfg.risk.cash_sleeve_ticker
        if base:
            dep = base["deployable"]
            for r in rows:
                t = r["ticker"]
                if t == cash:
                    continue
                w = float(r["target_weight"] or 0)
                band = portfolio.resolve_band_pct(float(r.get("band") or 0), w, self.cfg.risk.rebalance_drift_band_pct)
                d = portfolio.weight_drift(base["values"].get(t, 0.0), dep, w)
                if portfolio.needs_rebalance(d, band):
                    out.append(t)
        res = {"outside": out}
        if out:
            yf = proposals.prefetch_prices(self.led, out, now_dt=self.now, yf_price_fn=self.yf_price_fn)
            with self.led.transaction() as conn:
                if store.create_event(conn, dedup_key=f"drift:{self.day}", ticker=out[0], type="drift",
                                      detail={"tickers": out}, now=store.iso(self.now), status="noted"):
                    self.status["events"] += 1
                try:
                    built = proposals.build_trade(self.led, conn, cfg=self.cfg, tickers=out, origin={},
                                                  python_origin="drift", rationale="The book drifted outside "
                                                  "the agreed bands.", evidence=[], created_by="monitor",
                                                  now_dt=self.now, yf_price_fn=yf)
                except proposals.ProposalError as e:
                    built = None
                    res["refused"] = str(e)
            if built:
                outbox.send_trade_card(self.led, self.box, self.led.connect, built, now_dt=self.now)
                self.status["proposals"] += 1
                res["proposal_id"] = built["proposal_id"]
        self.set_state(f"drift_done:{self.day}", store.iso(self.now))
        return res

    # --- §9.1 step 8 ---------------------------------------------------------------
    def expire(self):
        with self.led.transaction() as conn:
            moved = proposals.expire_due(conn, self.now)
        for m in moved:
            if m.get("tg_message_id"):
                self.box.set_label(m["tg_message_id"], "Expired")
        with self.led.connect() as conn:
            stale = [r[0] for r in conn.execute(
                "SELECT p.id FROM proposals p WHERE p.kind='trade' AND p.state IN ('approved','executing') "
                "AND p.expires_at <= ? AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.proposal_id=p.id "
                "AND o.state IN ('placing','placed','partially_filled'))", (store.iso(self.now),)).fetchall()]
        for pid in stale:
            text = proposals.close_from_legs(self.led, pid, self.now, reason="it expired before it ran")
            if text:
                self.box.send(text, source="execution")
        return {"expired": len(moved), "closed": stale}

    # --- §9.1 step 9 ---------------------------------------------------------------
    def queue(self):
        jobs = events.queue_analysis(self.led, now=store.iso(self.now))
        rc.touch_trigger(ROOT, "worker", store.iso(self.now))
        return {"jobs": jobs}

    def path_units(self):
        code, _ = self.systemctl(["is-failed", "quiver-worker.path", "quiver-broker.path"])
        if code is None:
            return "no systemctl"
        if code == 0:
            self.page("path_unit", "a quiver .path unit is in the failed state")
        # The chat bridge ends in the failed state after a crash loop (StartLimitBurst).
        ccode, _ = self.systemctl(["is-failed", "quiver-chat.service"])
        if ccode == 0:
            self.page("path_unit", "quiver-chat.service is in the failed state (the chat bridge crash-looped)")
        acode, out = self.systemctl(["is-active", "quiver-broker.path"])
        inactive = out.strip() != "active"
        streak = int(self.state("broker_path_inactive", "0") or 0)
        streak = streak + 1 if (inactive and self.open) else 0
        self.set_state("broker_path_inactive", str(streak))
        if streak >= 2:
            self.page("path_unit", "quiver-broker.path is not active on two runs in a row in the session")
        return {"broker_path": out, "streak": streak}

    # --- §9.1 step 10 --------------------------------------------------------------
    def close_brief(self):
        if not self.after_close_today or not self.post_close_snapshot() or brief.already_sent(self.led, self.day):
            return "not due"
        goal = None
        try:
            g = self.led.get_active_goal()
            goal = goal_mod.compute_from_ledger(self.led, g) if g else None
            if goal is not None and g:
                goal = {**goal, "target_return_pct": g.get("target_return_pct")}
        except Exception:  # noqa: BLE001 — the goal line is optional
            goal = None
        with self.led.connect() as conn:
            model = brief.build_model(conn, day=self.day, now_dt=self.now, goal_progress=goal,
                                      earnings_fn=self.earnings_fn)
        msgs = notify.render_brief(model)
        sent = 0
        for m in msgs:
            if self.box.send(m, source="brief").get("ok"):
                sent += 1
        if sent:
            brief.mark_sent(self.led, self.day, self.box.chat_id, store.iso(self.now))
        return {"messages": len(msgs), "sent": sent}

    # --- §9.1 step 11 --------------------------------------------------------------
    def after_close(self):
        if not self.after_close_today or self.state(f"after_close_done:{self.day}") or not self.post_close_snapshot():
            return "not due"
        res = {}
        res["reflect"] = self.step("reflect", self.reflect)
        res["benchmark"] = self.step("benchmark", self.benchmark)
        res["goal"] = self.step("goal", lambda: self._check(self.tick(["goal-track"], timeout=120)))
        res["prune"] = self.step("prune", lambda: self._check(self.tick(["prune"], timeout=120)))
        self.set_state(f"after_close_done:{self.day}", store.iso(self.now))
        return res

    @staticmethod
    def _check(out):
        if isinstance(out, dict) and out.get("error"):
            raise RuntimeError(str(out["error"])[:300])
        return out

    def reflect(self):
        cutoff = (self.now.astimezone(market.ET).date() - timedelta(days=5)).isoformat()
        pending = self.led.pending_outcome_decisions(cutoff)
        res = []
        with self.led.connect() as conn:
            snap = proposals.latest_snapshot(conn)
            for d in pending:
                t = d["ticker"]
                price, _src = quotes.price_for(conn, t, self.now, self.yf_price_fn)
                pos = (snap or {}).get("positions", {}).get(t) or {}
                item = {"decision_id": d["id"], "price_now": price}
                if pos.get("quantity") and price:
                    item["position_market_value"] = float(pos["quantity"]) * float(price)
                    if pos.get("average_buy_price"):
                        item["position_cost_basis"] = float(pos["quantity"]) * float(pos["average_buy_price"])
                res.append(item)
        path = ROOT / "state" / "tmp" / "reflect_input.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"resolutions": res}), encoding="utf-8")
        return self._check(self.tick(["reflect", "--input", str(path)], timeout=120))

    def benchmark(self):
        bf = self._check(self.tick(["benchmark-fetch"], timeout=120))
        if not bf.get("observations"):
            return {"fetch": bf, "backfill": "skipped: no fresh benchmark sidecar"}
        bb = self._check(self.tick(["benchmark-backfill", "--apply", "--fill-only", "--max-rows", "12"], timeout=120))
        return {"fetch": {k: bf.get(k) for k in ("observations", "last_observation")}, "backfill": bb}

    # --- the run ---------------------------------------------------------------------
    def run(self):
        self.step("market", self.market_state, severity="critical")
        self.step("snapshot", self.snapshot)
        self.step("edgar", self.edgar_sweep)
        self.step("news", self.news_sweep)
        self.step("levels", self.check_levels)
        self.step("drift", self.drift)
        self.step("expire", self.expire)
        self.step("queue", self.queue)
        self.step("path_unit", self.path_units)
        self.step("brief", self.close_brief)
        self.order.append("after_close")
        self.after_close()
        if self.failed:
            self.status["event"] = "stage_error"
            self.status["failed"] = self.failed
        if self.led.is_halted(market.trading_day_et()):
            self.status["halted"] = True
        self.set_state("monitor_last_run", store.iso(self.now_fn()))
        rc._emit(self.status)
        return self.status


def main() -> int:
    led = Ledger(ROOT / "state" / "ledger.db")
    holder = runlock.holder_id()
    try:
        with runlock.run_lock(led, "monitor", holder, store.now_iso(), LOCK_TTL_S):
            Monitor(led=led).run()
    except runlock.RunLockError as e:
        rc._emit({"stage": "monitor", "skipped": True, "reason": str(e)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
