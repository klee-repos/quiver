#!/usr/bin/env python3
"""``deploy/runner/run_monitor.py``: one hourly monitor run (plan §9.1, §18.4 table).

Recording stubs replace every outward seam: the ``tick`` calls, ``systemctl``, the pager,
the Telegram transport, yfinance prices and news. The SEC answers come from the captured
files (``SecFixtures``). A new filing is made the producible way: the seed reads the ANET
submissions with the Q2 10-Q entry dropped, and the monitor then reads the full capture.

Times: 2026-10-28 is a regular session (EDT, 9:30-16:00 ET). 2026-11-27 is a half day
(EST, close 13:00 ET). Each case pins its own clock and unpins it in a ``finally``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from desk_fakes import (REPO, FakeClock, FakePrices, Results, SecFixtures, fx, seams,  # noqa: E402
                        seed_desk, snapshot, temp_root)

sys.path.insert(0, str(REPO / "deploy" / "runner"))
import run_monitor as rm  # noqa: E402
import runner_common as rc  # noqa: E402
from lib import runlock  # noqa: E402
from lib.dataflows.yfinance_news import fetch_news_items  # noqa: E402
from lib.desk import brief, edgar, events, levels, outbox, store  # noqa: E402

ET = ZoneInfo("America/New_York")
Q2_10Q = "0001596532-26-000175"     # ANET 10-Q for 2026-06-30 (sub_anet, index 55)
FEEDS = {"ANET": fx("yf_news")["news"], "CEG": fx("yf_news_ceg")["news"]}
AFTER_CLOSE_TICKS = [["benchmark-fetch"], ["benchmark-backfill", "--apply", "--fill-only", "--max-rows", "12"],
                     ["goal-track"], ["prune"]]
PLAN_ORDER = ["market", "snapshot", "edgar", "news", "levels", "drift", "expire", "queue", "path_unit",
              "brief", "after_close"]


def et(s: str) -> datetime:
    """'YYYY-MM-DD HH:MM' in ET -> an aware UTC datetime."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ET).astimezone(timezone.utc)


def drop_acc(accs):
    """A ``SecFixtures`` drop function: remove the entries of these accessions from the
    columnar ``filings.recent`` arrays (rule 4: drop entries, add no key)."""
    def fn(d):
        rec = d["filings"]["recent"]
        keep = [i for i, a in enumerate(rec["accessionNumber"]) if a not in accs]
        for k, v in list(rec.items()):
            if isinstance(v, list):
                rec[k] = [v[i] for i in keep]
        return d
    return fn


def news_fetch(ticker, limit):
    """The real ``fetch_news_items`` over the captured ``yf.Search`` answers."""
    return fetch_news_items(ticker, limit, search_fn=lambda q, c: FEEDS.get(q, []),
                            ticker_news_fn=lambda q, c: [])


def news_fail(ticker, limit):
    raise RuntimeError("yfinance down")


class Transport:
    """A recording Telegram transport: the minimal Bot API answer shape."""

    def __init__(self):
        self.calls = []
        self.n = 500

    def __call__(self, method, params):
        self.calls.append((method, params))
        self.n += 1
        return {"ok": True, "result": {"message_id": self.n}}

    def labels(self):
        out = []
        for method, p in self.calls:
            if method == "editMessageReplyMarkup":
                out.append((p["message_id"], p["reply_markup"]["inline_keyboard"][0][0]["text"]))
        return out


class RecPrices(FakePrices):
    def __init__(self, prices=None):
        super().__init__(prices)
        self.calls = []

    def __call__(self, ticker):
        self.calls.append(str(ticker).upper())
        return super().__call__(ticker)


class BoomClient:
    """An EDGAR client object (duck-typed) whose every call raises ``exc``."""

    def __init__(self, exc):
        self.exc = exc

    def get(self, url):
        raise self.exc

    def get_text(self, url):
        raise self.exc

    def get_json(self, url):
        raise self.exc


class Patched:
    """Point the runner modules at the temp root (they read QUIVER_ROOT at import)."""

    def __init__(self, root):
        self.root = root

    def __enter__(self):
        self.prev = (rm.ROOT, rc.ROOT, rc.TICK_LOG, rm.Monitor)
        rm.ROOT = self.root.path
        rc.ROOT = self.root.path
        rc.TICK_LOG = str(self.root.path / "logs" / "tick.log")
        return self

    def __exit__(self, *exc):
        rm.ROOT, rc.ROOT, rc.TICK_LOG, rm.Monitor = self.prev
        return False


class Harness:
    """One temp root's monitor wiring. ``run(when)`` makes a fresh ``Monitor`` and runs it."""

    def __init__(self, root, clock, *, client=None, page_real=False):
        self.root, self.clock = root, clock
        self.led = root.led()
        self.cfg = root.cfg()
        self.tr = Transport()
        self.box = outbox.Outbox(self.led, transport=self.tr, chat_id="111", clock=clock.monotonic,
                                 sleep=lambda s: None)
        self.prices = RecPrices()
        if client is None:
            self.seams = seams()
        else:
            self.seams = events.Seams(client=client, price_fn=FakePrices(), market_cap_fn=lambda t: None,
                                      technicals_fn=lambda t: None, next_earnings_fn=lambda t: None,
                                      top_fn=lambda t: [], av_key="")
        self.ticks = []
        self.pages = []
        self.sysc = []
        self.sleeps = []
        self.snap_on_wait = True
        self.page_real = page_real
        self.news = news_fetch
        self.failed_paths = False
        self.broker_active = "active"

    # seams
    def tick(self, args, timeout=None):
        self.ticks.append(list(args))
        if args and args[0] == "benchmark-fetch":
            # The real cmd_benchmark_fetch answer shape (tick.py).
            return {"ok": True, "benchmark": "SPY", "trading_day": self.clock.now.astimezone(ET).strftime("%Y-%m-%d"),
                    "observations": 501, "last_observation": "2026-10-27", "path": "state/tmp/benchmark.json"}
        return {"ok": True}

    def page(self, *, stage, detail, day):
        self.pages.append({"stage": stage, "detail": detail, "day": day})

    def systemctl(self, args):
        self.sysc.append(list(args))
        if args[0] == "is-failed":
            return (0, "failed\nactive") if self.failed_paths else (1, "active\nactive")
        return (0 if self.broker_active == "active" else 3), self.broker_active

    def sleep(self, s):
        self.sleeps.append(s)
        self.clock.sleep(s)
        if self.snap_on_wait and not self._took:
            self._took = True
            snapshot(self.root, at=store.iso(self.clock.now))

    # the run
    def make(self):
        return rm.Monitor(led=self.led, cfg=self.cfg, now_fn=lambda: self.clock.now, sleep=self.sleep,
                          seams=self.seams, box=self.box, systemctl=self.systemctl, tick=self.tick,
                          page=None if self.page_real else self.page, news_fetch=self.news,
                          yf_price_fn=self.prices, earnings_fn=lambda t: None)

    def run(self, when, *, snap_on_wait=None):
        self.clock.set(when)
        self._took = False
        if snap_on_wait is not None:
            self.snap_on_wait = snap_on_wait
        self.sleeps = []
        self.ticks_before = len(self.ticks)
        self.log_before = len(self.log_lines())
        m = self.make()
        st = m.run()
        return st, m

    # reads
    def q(self, sql, args=()):
        with self.led.connect() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.led.connect() as c:
            return c.execute(sql, args).fetchone()[0]

    def log_lines(self):
        p = Path(rc.TICK_LOG)
        if not p.exists():
            return []
        out = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                out.append({"raw": ln})
        return out

    def new_log(self):
        return self.log_lines()[self.log_before:]

    def run_ticks(self):
        return self.ticks[self.ticks_before:]

    def trig(self, name):
        return self.root.path / "state" / "triggers" / name

    def clear_trig(self, *names):
        for n in names:
            try:
                self.trig(n).unlink()
            except FileNotFoundError:
                pass

    def drift_proposals(self):
        return self.q("SELECT * FROM proposals WHERE kind='trade' AND origin='drift' ORDER BY id")


def with_root(start, fn, *, new_10q=False, **hkw):
    """Pin the clock at ``start``, seed a temp root, patch the runner globals, run ``fn(h)``."""
    clock = FakeClock(start).pin_market()
    try:
        with temp_root() as root, Patched(root):
            opener = SecFixtures(drop={"sub_anet": drop_acc({Q2_10Q})}) if new_10q else None
            seed_desk(root, opener=opener)
            h = Harness(root, clock, **hkw)
            return fn(h)
    finally:
        clock.unpin()


# --- order -----------------------------------------------------------------------------

def case_order(r: Results):
    def body(h: Harness):
        st, m = h.run(et("2026-10-28 16:40"))
        r.eq("order: the steps run in the plan order", m.order[:len(PLAN_ORDER)], PLAN_ORDER)
        r.eq("order: after-close runs reflect, benchmark, goal, prune last", m.order[len(PLAN_ORDER):],
             ["reflect", "benchmark", "goal", "prune"])
        r.eq("order: no step failed", m.failed, [])
        r.ok("order: the edgar step found the new 10-Q", st["filings"] == 1, st)
        r.ok("order: path_unit checks both .path units", ["is-failed", "quiver-worker.path", "quiver-broker.path"]
             in h.sysc and ["is-active", "quiver-broker.path"] in h.sysc, h.sysc)
        lines = h.new_log()
        r.ok("order: the status line is the last line of the run", bool(lines) and lines[-1].get("event") == "monitor_ok"
             and set(("names", "filings", "events", "proposals")) <= set(lines[-1]), lines[-3:])
        # The status line comes after the brief and the after-close calls.
        brief_rows = h.q("SELECT id FROM chat_messages WHERE source='brief'")
        r.ok("order: the brief was sent before the status line", len(brief_rows) >= 1, brief_rows)

        # The lock: main() takes run_lock("monitor") around the run and releases it after.
        seen = {}
        Real = Patched_prev_monitor[0]

        class Probe(Real):
            def run(self):
                with self.led.connect() as c:
                    row = c.execute("SELECT holder FROM run_locks WHERE name='monitor'").fetchone()
                seen["holder"] = row[0] if row else None
                seen["ran"] = True
                return super().run()

        def factory(led):
            m2 = Probe(led=led, cfg=h.cfg, now_fn=lambda: h.clock.now, sleep=h.sleep, seams=h.seams, box=h.box,
                       systemctl=h.systemctl, tick=h.tick, page=h.page, news_fetch=h.news,
                       yf_price_fn=h.prices, earnings_fn=lambda t: None)
            return m2

        rm.Monitor = factory
        h.clock.set(et("2026-10-28 17:40"))
        rc_ = rm.main()
        r.eq("lock: main exits 0", rc_, 0)
        r.eq("lock: the run ran while main held the monitor lock", seen.get("holder"), runlock.holder_id())
        r.eq("lock: main released the monitor lock", h.one("SELECT COUNT(*) FROM run_locks WHERE name='monitor'"), 0)
        # A second monitor exits on the lock.
        seen.clear()
        other = f"{runlock.boot_id()}:{os.getppid()}"
        assert h.led.try_acquire_named_lock("monitor", other, store.iso(h.clock.now), ttl_seconds=2400,
                                            holder_dead=runlock.holder_dead)
        before = len(h.log_lines())
        rm.main()
        r.ok("lock: a second monitor exits on the lock and does not run", "ran" not in seen, seen)
        skipped = [ln for ln in h.log_lines()[before:] if ln.get("skipped")]
        r.ok("lock: the skip is logged", len(skipped) == 1 and "monitor lock" in skipped[0].get("reason", ""),
             h.log_lines()[before:])
        h.led.release_named_lock("monitor", other)

    with_root(et("2026-10-28 13:00"), body, new_10q=True)


Patched_prev_monitor = [rm.Monitor]


# --- isolation -------------------------------------------------------------------------

def case_isolation(r: Results):
    env_keys = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_ALERT_CHAT_IDS")
    prev_env = {k: os.environ.get(k) for k in env_keys}
    orig_alert = rc._maybe_alert
    sent = []
    calls = []

    def fake_send(*, token, chat_ids, text, disable_notification=False, **kw):
        sent.append(text)
        return {"ok": True}

    def wrapped(led, **kw):
        calls.append(kw.get("stage"))
        return orig_alert(led, send=fake_send, **kw)

    os.environ["TELEGRAM_BOT_TOKEN"] = "123:test"
    os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "111"
    os.environ.pop("TELEGRAM_ALERT_CHAT_IDS", None)
    rc._maybe_alert = wrapped
    try:
        def body(h: Harness):
            with h.led.connect() as c:
                levels.upsert_level(c, "ANET", "buy_below", 200.0, store.now_iso())
            st, m = h.run(et("2026-10-28 11:40"))
            r.eq("isolation: only the edgar step failed", m.failed, ["edgar"])
            r.eq("isolation: every later step still ran", m.order[:len(PLAN_ORDER)], PLAN_ORDER)
            r.ok("isolation: news still ingested headlines", h.one("SELECT COUNT(*) FROM news_items") > 0)
            lv = h.q("SELECT * FROM events WHERE type='price_level'")
            r.eq("isolation: levels still fired the armed ANET level", [e["ticker"] for e in lv], ["ANET"])
            jobs = h.q("SELECT * FROM jobs WHERE kind='analyze_event'")
            r.eq("isolation: queue still queued the level event", len(jobs), 1)
            r.ok("isolation: path_unit still ran", any(a[0] == "is-failed" for a in h.sysc), h.sysc)
            r.eq("isolation: the status line says stage_error", (st.get("event"), st.get("failed")),
                 ("stage_error", ["edgar"]))
            errs = [ln for ln in h.new_log() if ln.get("event") == "stage_error" and ln.get("stage") == "edgar"]
            r.eq("isolation: one stage_error line for edgar", len(errs), 1)
            r.eq("isolation: one page for the edgar stage", len(sent), 1)
            # A second run the same day: the edgar page is not sent again.
            h.news = news_fail
            st2, m2 = h.run(et("2026-10-28 12:40"))
            r.eq("isolation: second run fails edgar and news", sorted(m2.failed), ["edgar", "news"])
            r.eq("isolation: same day -> edgar does not page again; news pages once", len(sent), 2)
            r.ok("isolation: the second page names the news stage", "news" in sent[-1].lower(), sent[-1][:300])
            h.run(et("2026-10-28 13:40"))
            r.eq("isolation: third run the same day sends no page", len(sent), 2)
            # The next day pages again (one page per stage per day).
            h.news = news_fetch
            h.run(et("2026-10-29 11:40"))
            r.eq("isolation: the next day the edgar stage pages again", len(sent), 3)

        with_root(et("2026-10-28 09:00"), body, client=BoomClient(RuntimeError("edgar boom")), page_real=True)

        # EdgarBlocked: the sweep pages AND the step pages; the dedup sends one.
        sent.clear()
        calls.clear()

        def body2(h: Harness):
            st, m = h.run(et("2026-10-28 11:40"))
            r.eq("isolation: a 403 fails the edgar step", m.failed, ["edgar"])
            r.ok("isolation: the 403 path asks for an edgar page twice", calls.count("edgar") >= 2, calls)
            r.eq("isolation: a 403 sends one edgar page", len(sent), 1)

        with_root(et("2026-10-28 09:00"), body2, client=BoomClient(edgar.EdgarBlocked("SEC returned 403")),
                  page_real=True)
    finally:
        rc._maybe_alert = orig_alert
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- snapshot --------------------------------------------------------------------------

def _snap_jobs(h):
    return h.q("SELECT * FROM broker_jobs WHERE kind='snapshot' ORDER BY id")


def case_snapshot(r: Results):
    def body(h: Harness):
        h.clear_trig("broker")
        h.run(et("2026-10-28 11:40"), snap_on_wait=False)
        jobs = _snap_jobs(h)
        r.eq("snapshot: open -> one snapshot job", len(jobs), 1)
        r.eq("snapshot: the job is from the monitor", jobs[0]["requested_by"] if jobs else None, "monitor")
        r.ok("snapshot: open -> the broker trigger is touched", h.trig("broker").exists())
        r.eq("snapshot: with no snapshot the wait stops at 180 s", sum(h.sleeps), 180)
        # 49 minutes later the job is still fresh.
        h.clear_trig("broker")
        h.run(et("2026-10-28 12:29"), snap_on_wait=False)
        r.eq("snapshot: a job < 50 min old -> no new job", len(_snap_jobs(h)), 1)
        # 51 minutes after the first job: a new job.
        h.run(et("2026-10-28 12:31"), snap_on_wait=True)
        r.eq("snapshot: the job is 51 min old -> a new job", len(_snap_jobs(h)), 2)
        r.eq("snapshot: the wait stops when the snapshot arrives", sum(h.sleeps), 5)

    with_root(et("2026-10-28 09:00"), body)

    def at44(h: Harness):
        h.clear_trig("broker")
        h.run(et("2026-10-28 16:44"), snap_on_wait=False)
        r.eq("snapshot: 44 min after the close -> queued", len(_snap_jobs(h)), 1)
        r.ok("snapshot: 44 min after the close -> broker trigger touched", h.trig("broker").exists())

    with_root(et("2026-10-28 09:00"), at44)

    def at46(h: Harness):
        h.clear_trig("broker")
        h.run(et("2026-10-28 16:46"), snap_on_wait=False)
        r.eq("snapshot: 46 min after the close -> not queued", len(_snap_jobs(h)), 0)
        r.ok("snapshot: 46 min after the close -> broker trigger not touched", not h.trig("broker").exists())
        r.eq("snapshot: 46 min after the close -> no wait", h.sleeps, [])

    with_root(et("2026-10-28 09:00"), at46)

    def closed_levels(h: Harness):
        with h.led.connect() as c:
            levels.upsert_level(c, "ANET", "buy_below", 200.0, store.now_iso())
        h.clear_trig("broker")
        h.run(et("2026-10-28 18:00"), snap_on_wait=False)
        r.eq("snapshot: closed -> no level event", h.q("SELECT id FROM events WHERE type='price_level'"), [])
        r.eq("snapshot: closed -> the level stays armed",
             h.one("SELECT armed FROM level_state WHERE ticker='ANET' AND level='buy_below'"), 1)
        r.eq("snapshot: closed -> no level quote is read", h.prices.calls, [])
        r.ok("snapshot: closed -> broker trigger not touched", not h.trig("broker").exists())
        # The same level fires in the next session.
        h.run(et("2026-10-29 10:40"), snap_on_wait=False)
        ev = h.q("SELECT * FROM events WHERE type='price_level'")
        r.eq("snapshot: open -> the level fires", [(e["ticker"], e["dedup_key"]) for e in ev],
             [("ANET", "level:ANET:buy_below:2026-10-29")])

    with_root(et("2026-10-28 09:00"), closed_levels)


# --- once a day ------------------------------------------------------------------------

def _done(h, day):
    """(drift event exists, brief sent, after-close tick count) for ``day``."""
    drift = h.q("SELECT id FROM events WHERE dedup_key=?", (f"drift:{day}",))
    return (len(drift), brief.already_sent(h.led, day), len([t for t in h.ticks if t[0] == "reflect"]))


def case_once_a_day(r: Results):
    def seq_a(h: Harness):
        day = "2026-10-28"
        h.run(et("2026-10-28 15:40"), snap_on_wait=True)
        r.eq("once_a_day: 15:40 ET (open) -> nothing runs", _done(h, day), (0, False, 0))
        h.run(et("2026-10-28 15:50"), snap_on_wait=True)
        r.eq("once_a_day: not at 15:50 ET", _done(h, day), (0, False, 0))
        h.run(et("2026-10-28 16:14"), snap_on_wait=False)
        r.eq("once_a_day: 14 min after the close -> not yet", _done(h, day), (0, False, 0))
        h.run(et("2026-10-28 16:40"), snap_on_wait=False)
        r.eq("once_a_day: no post-close snapshot -> they wait", _done(h, day), (0, False, 0))
        r.eq("once_a_day: no after-close tick call while they wait", h.ticks, [])
        snapshot(h.root, at=store.iso(et("2026-10-28 16:45")))
        st, m = h.run(et("2026-10-28 17:40"), snap_on_wait=False)
        r.eq("once_a_day: first run >= 15 min with a post-close snapshot -> all three run", _done(h, day), (1, True, 1))
        r.eq("once_a_day: one drift proposal from the monitor",
             [(p["created_by"], p["state"]) for p in h.drift_proposals()], [("monitor", "pending")])
        r.eq("once_a_day: the drift event goes straight to noted",
             h.one("SELECT status FROM events WHERE dedup_key=?", (f"drift:{day}",)), "noted")
        n_ticks, n_brief = len(h.ticks), h.one("SELECT COUNT(*) FROM chat_messages WHERE source='brief'")
        h.run(et("2026-10-28 18:40"), snap_on_wait=False)
        r.eq("once_a_day: not twice (tick calls)", len(h.ticks), n_ticks)
        r.eq("once_a_day: not twice (brief messages)",
             h.one("SELECT COUNT(*) FROM chat_messages WHERE source='brief'"), n_brief)
        r.eq("once_a_day: not twice (drift proposals)", len(h.drift_proposals()), 1)
        r.eq("once_a_day: one brief notifications row",
             h.one("SELECT COUNT(*) FROM notifications WHERE trade_date=? AND kind='brief'", (day,)), 1)

    with_root(et("2026-10-28 09:00"), seq_a)

    def seq_b(h: Harness):
        day = "2026-10-28"
        snapshot(h.root, at=store.iso(et("2026-10-28 16:05")))
        h.run(et("2026-10-28 16:14"), snap_on_wait=False)
        r.eq("once_a_day: 16:14 ET with a post-close snapshot -> not yet", _done(h, day), (0, False, 0))
        h.run(et("2026-10-28 16:15"), snap_on_wait=False)
        r.eq("once_a_day: 16:15 ET (15 min) with a post-close snapshot -> all three run", _done(h, day), (1, True, 1))

    with_root(et("2026-10-28 09:00"), seq_b)

    def seq_c(h: Harness):
        day = "2026-11-27"
        h.run(et("2026-11-27 12:40"), snap_on_wait=True)
        r.eq("once_a_day: half day 12:40 ET (open) -> nothing runs", _done(h, day), (0, False, 0))
        h.run(et("2026-11-27 13:40"), snap_on_wait=True)
        r.eq("once_a_day: half day 13:40 ET (40 min after the 13:00 close) -> all three run",
             _done(h, day), (1, True, 1))

    with_root(et("2026-11-27 09:00"), seq_c)


# --- misc ------------------------------------------------------------------------------

def _counts(h):
    return {"filings": h.one("SELECT COUNT(*) FROM filings"), "events": h.one("SELECT COUNT(*) FROM events"),
            "proposals": h.one("SELECT COUNT(*) FROM proposals")}


def case_misc(r: Results):
    def expire_seq(h: Harness):
        # Run 1: 16:40 ET after the close -> drift proposal P (expires at the next close).
        h.clear_trig("worker", "broker")
        before = _counts(h)
        st, m = h.run(et("2026-10-28 16:40"), snap_on_wait=True)
        after = _counts(h)
        r.ok("misc: the worker trigger is touched (run 1)", h.trig("worker").exists())
        r.ok("misc: the broker trigger is touched 40 min after the close", h.trig("broker").exists())
        props = h.drift_proposals()
        r.eq("misc: run 1 made one pending drift proposal", [p["state"] for p in props], ["pending"])
        pid, mid = props[0]["id"], props[0]["tg_message_id"]
        r.eq("misc: P expires at the next session close", props[0]["expires_at"], store.iso(et("2026-10-29 16:00")))
        # After-close ticks: once, after the brief, with the plan's arguments.
        ticks = h.run_ticks()
        reflect_path = h.root.path / "state" / "tmp" / "reflect_input.json"
        r.eq("misc: after-close tick calls in order", ticks, [["reflect", "--input", str(reflect_path)]] + AFTER_CLOSE_TICKS)
        r.ok("misc: reflect runs after the brief", m.order.index("brief") < m.order.index("reflect"), m.order)
        r.ok("misc: reflect_input.json holds the resolutions list",
             reflect_path.exists() and isinstance(json.loads(reflect_path.read_text())["resolutions"], list))
        # Status: one JSON line with monitor_ok and the counts.
        lines = [ln for ln in h.new_log() if ln.get("event") == "monitor_ok"]
        r.eq("misc: one monitor_ok status line", len(lines), 1)
        r.eq("misc: run() returns the status line", lines[0] if lines else None, st)
        r.eq("misc: status filings = new filings rows", st.get("filings"), after["filings"] - before["filings"])
        r.eq("misc: status proposals = new proposals", st.get("proposals"), after["proposals"] - before["proposals"])
        r.eq("misc: status events = events the monitor made (incl. the drift event)", st.get("events"),
             after["events"] - before["events"])
        r.eq("misc: status names = the companies swept", st.get("names"), 2)

        # Run 2: in the next session -> P stays pending, no after-close call.
        h.clear_trig("worker", "broker")
        h.run(et("2026-10-29 15:40"), snap_on_wait=True)
        r.ok("misc: the worker trigger is touched (run 2)", h.trig("worker").exists())
        r.ok("misc: the broker trigger is touched in the session", h.trig("broker").exists())
        r.eq("misc: P is still pending before its expires_at",
             h.one("SELECT state FROM proposals WHERE id=?", (pid,)), "pending")
        r.eq("misc: no after-close call in the session", h.run_ticks(), [])

        # Run 3: 16:05 ET, after P's expires_at.
        h.clear_trig("worker")
        h.run(et("2026-10-29 16:05"), snap_on_wait=True)
        r.ok("misc: the worker trigger is touched (run 3)", h.trig("worker").exists())
        r.eq("misc: passed expires_at -> expired", h.one("SELECT state FROM proposals WHERE id=?", (pid,)), "expired")
        r.ok("misc: passed expires_at -> the card label is Expired", (mid, "Expired") in h.tr.labels(), h.tr.labels())
        legs = h.q("SELECT state, finalized FROM orders WHERE proposal_id=?", (pid,))
        r.ok("misc: the expired legs are finalized", legs and all(l["state"] == "expired" and l["finalized"] == 1
                                                                   for l in legs), legs)

        # Run 4: closed, 2 h after the close -> no broker trigger, still the worker trigger.
        h.clear_trig("worker", "broker")
        h.run(et("2026-10-29 18:00"), snap_on_wait=False)
        r.ok("misc: the worker trigger is touched (run 4, closed)", h.trig("worker").exists())
        r.ok("misc: the broker trigger is not touched 2 h after the close", not h.trig("broker").exists())

    with_root(et("2026-10-28 09:00"), expire_seq)

    def kill_seq(h: Harness):
        h.root.kill.write_text("stop\n", encoding="utf-8")
        h.clear_trig("worker")
        st, m = h.run(et("2026-10-28 16:40"), snap_on_wait=True)
        ev = h.q("SELECT * FROM events WHERE type='filing'")
        r.eq("misc: KILL -> the new 10-Q is still detected",
             [(e["ticker"], e["dedup_key"]) for e in ev], [("ANET", f"filing:{Q2_10Q}")])
        r.eq("misc: KILL -> the event is queued", [e["status"] for e in ev], ["queued"])
        jobs = h.q("SELECT payload_json FROM jobs WHERE kind='analyze_event'")
        r.eq("misc: KILL -> an analyze_event job is queued",
             [json.loads(j["payload_json"]).get("event_id") for j in jobs], [ev[0]["id"]] if ev else [])
        r.ok("misc: KILL -> the worker trigger is touched", h.trig("worker").exists())
        r.eq("misc: KILL -> no trade proposal (§9.3)", h.q("SELECT id FROM proposals WHERE kind='trade'"), [])

    with_root(et("2026-10-28 09:00"), kill_seq, new_10q=True)

    def metrics_seq(h: Harness):
        def n_metrics():
            return h.one("SELECT COUNT(*) FROM metrics WHERE ticker IN ('ANET','CEG')")
        n0 = n_metrics()
        h.run(et("2026-10-28 10:40"), snap_on_wait=True)
        n1 = n_metrics()
        r.eq("misc: the day's first run recomputes ANET and CEG metrics", n1 - n0, 2)
        h.run(et("2026-10-28 11:40"), snap_on_wait=True)
        r.eq("misc: the day's second run does not recompute metrics", n_metrics(), n1)
        h.run(et("2026-10-29 10:40"), snap_on_wait=True)
        r.eq("misc: the next day's first run recomputes metrics", n_metrics() - n1, 2)

    with_root(et("2026-10-28 09:00"), metrics_seq)

    def duplicate_seq(h: Harness):
        # Two pending min_trade_notional cards from the old producer (it superseded no change).
        from lib.desk import proposals
        h.clock.set(et("2026-10-28 15:00"))
        keep = proposals.supersede_changes
        proposals.supersede_changes = lambda *a, **k: []
        try:
            ids = []
            for v in (10, 12):
                with h.led.transaction() as c:
                    ids.append(proposals.build_change(c, h.cfg, change={"kind": "set_risk", "key": "min_trade_notional",
                                                                         "value": v},
                                                      rationale="A test card.", created_by="desk",
                                                      now_dt=h.clock.now)["proposal_id"])
        finally:
            proposals.supersede_changes = keep
        with h.led.connect() as c:
            for i, pid in enumerate(ids):
                c.execute("UPDATE proposals SET tg_message_id=? WHERE id=?", (6100 + i, pid))
        st, m = h.run(et("2026-10-28 15:40"), snap_on_wait=True)
        r.eq("misc: the expire step supersedes the older duplicate card",
             [h.one("SELECT state FROM proposals WHERE id=?", (pid,)) for pid in ids], ["superseded", "pending"])
        r.ok("misc: the older card is relabeled Replaced by #N", (6100, f"Replaced by #{ids[1]}") in h.tr.labels(),
             h.tr.labels())

    with_root(et("2026-10-28 09:00"), duplicate_seq)


# --- path units (§9.1 step 9b) ---------------------------------------------------------

def case_path_unit(r: Results):
    def body(h: Harness):
        h.failed_paths = True
        h.run(et("2026-10-28 10:40"), snap_on_wait=True)
        r.ok("path_unit: a failed .path unit pages (stage path_unit)",
             any(p["stage"] == "path_unit" for p in h.pages), h.pages)
        h.failed_paths = False
        h.pages.clear()
        h.broker_active = "inactive"
        h.run(et("2026-10-28 11:40"), snap_on_wait=True)
        r.eq("path_unit: broker.path inactive on one run -> no page", h.pages, [])
        h.run(et("2026-10-28 12:40"), snap_on_wait=True)
        r.ok("path_unit: broker.path inactive on two runs in the session -> page",
             [p["stage"] for p in h.pages] == ["path_unit"], h.pages)

    with_root(et("2026-10-28 09:00"), body)


def main() -> int:
    r = Results("run-monitor")
    for case in (case_order, case_isolation, case_snapshot, case_once_a_day, case_misc, case_path_unit):
        try:
            case(r)
        except Exception as e:  # noqa: BLE001 — a crashed case is one failure, the rest still run
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__}: crashed", False, f"{type(e).__name__}: {e}")
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
