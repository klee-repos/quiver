#!/usr/bin/env python3
"""``deploy/runner/run_worker.py``: the job queue of the analyst and the news classifier
(plan §9.2, §18.4 table).

The ``job_runner`` seam replaces the Claude CLI. A scripted runner plays the model: it
calls the real tool functions through ``lib.desk.tools.call`` with a real ``ToolContext``
(role ``analyst``). The outbox records each message through a fake Telegram transport.
No test starts the Claude CLI and no test sends to the network.

Events come from the real producers: ``events.sweep`` finds the ANET Q2 10-Q (the seed read
the submissions with that entry dropped), and ``levels.check_levels`` fires price levels.
"""

from __future__ import annotations

import json
import os
import re
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
import run_worker as rw  # noqa: E402
import runner_common as rc  # noqa: E402
from lib import runlock  # noqa: E402
from lib.dataflows.yfinance_news import fetch_news_items  # noqa: E402
from lib.desk import agent, events, levels, news, outbox, proposals, store, tools  # noqa: E402
from lib.desk import settings as desk_settings  # noqa: E402

ET = ZoneInfo("America/New_York")
Q2_10Q = "0001596532-26-000175"     # ANET 10-Q for 2026-06-30 (sub_anet, index 55)
FEEDS = {"ANET": fx("yf_news")["news"], "CEG": fx("yf_news_ceg")["news"]}
FAIL_TEXT = "I could not analyze ANET's 10-Q (error: {}). I stopped trying."


def et(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ET).astimezone(timezone.utc)


def drop_acc(accs):
    def fn(d):
        rec = d["filings"]["recent"]
        keep = [i for i, a in enumerate(rec["accessionNumber"]) if a not in accs]
        for k, v in list(rec.items()):
            if isinstance(v, list):
                rec[k] = [v[i] for i in keep]
        return d
    return fn


def news_fetch(ticker, limit):
    return fetch_news_items(ticker, limit, search_fn=lambda q, c: FEEDS.get(q, []),
                            ticker_news_fn=lambda q, c: [])


class Transport:
    def __init__(self):
        self.calls = []
        self.n = 900

    def __call__(self, method, params):
        self.calls.append((method, params))
        self.n += 1
        return {"ok": True, "result": {"message_id": self.n}}

    def texts(self):
        return [p.get("text") for m, p in self.calls if m == "sendMessage"]


class Script:
    """The scripted model. ``fn(h, kind, prompt, js, ctx, attempt)`` runs the tool calls."""

    def __init__(self, h, fn):
        self.h = h
        self.fn = fn
        self.calls = []          # (kind, job_id, event_id)
        self.prompts = {}
        self.attempts = {}
        self.running_seen = []   # (job_id, running jobs at start)

    def __call__(self, kind, prompt, js):
        h = self.h
        self.calls.append((kind, js.job_id, js.event_id))
        self.prompts[js.job_id] = prompt
        self.attempts[js.job_id] = self.attempts.get(js.job_id, 0) + 1
        running = [r["id"] for r in h.q("SELECT id FROM jobs WHERE status='running'")]
        self.running_seen.append((js.job_id, running))
        ctx = tools.ToolContext.build(role="analyst", runtime=h.rt, secrets=h.sec, job=js, led=h.led,
                                      transport=h.tr, box=h.box, yf_price_fn=FakePrices())
        return self.fn(h, kind, prompt, js, ctx, self.attempts[js.job_id])


def ok_call(ctx, name, args):
    res = tools.call(ctx, name, args)
    if res.get("is_error"):
        raise AssertionError(f"{name} refused: {res['content'][0]['text']}")
    return json.loads(res["content"][0]["text"])


def verdict(ctx, js, v="intact"):
    return ok_call(ctx, "record_verdict", {"event_id": js.event_id, "verdict": v,
                                            "summary": "The new facts leave the thesis as it was."})


class Harness:
    def __init__(self, root, clock):
        self.root, self.clock = root, clock
        self.led = root.led()
        self.cfg = root.cfg()
        self.rt = agent.Runtime.from_env()
        self.sec = agent.Secrets()
        self.tr = Transport()
        self.box = outbox.Outbox(self.led, transport=self.tr, chat_id="111", clock=clock.monotonic,
                                 sleep=lambda s: None)
        self.classified = []
        self.fetched = []

    def classify_fn(self, items):
        running = [dict(r) for r in self.q("SELECT id, kind FROM jobs WHERE status='running'")]
        self.classified.append({"items": [i["id"] for i in items], "running": running})
        return json.dumps([{"id": i["id"], "material": False, "category": "other",
                            "reason": "routine market chatter"} for i in items])

    def fetch_document(self, url, kind="news", ticker=None):
        self.fetched.append(url)
        raise RuntimeError("paywall 403")

    def worker(self, script):
        return rw.Worker(runtime=self.rt, secrets=self.sec, led=self.led, box=self.box, job_runner=script,
                         classify_fn=self.classify_fn, fetch_document=self.fetch_document)

    def q(self, sql, args=()):
        with self.led.connect() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.led.connect() as c:
            row = c.execute(sql, args).fetchone()
            return row[0] if row else None

    def job(self, jid):
        return self.q("SELECT * FROM jobs WHERE id=?", (jid,))[0]

    def at(self, s):
        self.clock.set(et(s))

    # producers
    def level_event(self, ticker, level, price, when):
        self.at(when)
        with self.led.connect() as c:
            levels.upsert_level(c, ticker, level, price, store.now_iso())
            res = levels.check_levels(c, now_dt=self.clock.now, yf_price_fn=FakePrices())
        return res["events"]

    def queue(self, when=None):
        if when:
            self.at(when)
        return events.queue_analysis(self.led, now=store.now_iso())

    def sweep_10q(self, when):
        self.at(when)
        with self.led.connect() as c:
            sett = desk_settings.effective(c, self.cfg)
        res = events.sweep(self.led, settings=sett, seams=seams())
        return res["events"]

    def sent(self):
        return [r["text"] for r in self.q("SELECT text FROM chat_messages WHERE role='desk' AND source='analyst' "
                                          "ORDER BY id")]


class Patched:
    def __init__(self, root):
        self.root = root

    def __enter__(self):
        self.prev = (rc.ROOT, rc.TICK_LOG, rw.Worker)
        rc.ROOT = self.root.path
        rc.TICK_LOG = str(self.root.path / "logs" / "tick.log")
        return self

    def __exit__(self, *exc):
        rc.ROOT, rc.TICK_LOG, rw.Worker = self.prev
        return False


def with_root(start, fn, *, new_10q=False):
    clock = FakeClock(et(start)).pin_market()
    try:
        with temp_root() as root, Patched(root):
            opener = SecFixtures(drop={"sub_anet": drop_acc({Q2_10Q})}) if new_10q else None
            seed_desk(root, opener=opener)
            return fn(Harness(root, clock))
    finally:
        clock.unpin()


def log_lines():
    p = Path(rc.TICK_LOG)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# --- order -----------------------------------------------------------------------------

def case_order(r: Results):
    def body(h: Harness):
        deep = [j["id"] for j in h.q("SELECT id FROM jobs WHERE kind='deep_dive' ORDER BY id")]
        r.eq("order: the seed queued two deep-dives first", len(deep), 2)
        ev_a = h.level_event("ANET", "buy_below", 200.0, "2026-10-28 13:10")
        job_a = h.queue()
        ev_b = h.level_event("CEG", "trim_above", 250.0, "2026-10-28 13:20")
        job_b = h.queue()
        h.at("2026-10-28 13:30")
        with h.led.connect() as c:
            n = news.ingest(c, now=store.now_iso(), fetch_fn=news_fetch)
            job_c = news.queue_classify(c, store.now_iso())
        r.ok("order: news produced a classify_news job", n > 0 and job_c is not None, (n, job_c))
        r.eq("order: one level event each", (len(ev_a), len(ev_b), len(job_a), len(job_b)), (1, 1, 1, 1))

        def fn(h, kind, prompt, js, ctx, attempt):
            if kind == "analyze_event":
                verdict(ctx, js)
            else:
                ok_call(ctx, "get_company", {"ticker": "ANET"})

        script = Script(h, fn)
        order = []
        w = h.worker(script)
        real_run_one = w.run_one

        def spy(job):
            order.append((job["kind"], job["id"]))
            return real_run_one(job)

        w.run_one = spy
        h.at("2026-10-28 13:40")
        ran = w.drain()
        r.eq("order: classify_news, then analyze_event oldest first, then deep_dive", order,
             [("classify_news", job_c), ("analyze_event", job_a[0]), ("analyze_event", job_b[0]),
              ("deep_dive", deep[0]), ("deep_dive", deep[1])])
        r.eq("order: drain ran five jobs", ran, 5)
        r.eq("order: every job is done", sorted({j["status"] for j in h.q("SELECT status FROM jobs")}), ["done"])
        r.ok("order: one at a time (the runner saw only its own job running)",
             all(run == [jid] for jid, run in script.running_seen), script.running_seen)
        r.ok("order: one at a time (the classifier saw only the classify job running)",
             h.classified and all(c["running"] == [{"id": job_c, "kind": "classify_news"}] for c in h.classified),
             h.classified)
        r.eq("order: the analyzed level events end noted",
             [h.one("SELECT status FROM events WHERE id=?", (e,)) for e in ev_a + ev_b], ["noted", "noted"])
        r.eq("order: the classifier stored every headline",
             h.one("SELECT COUNT(*) FROM news_items WHERE material IS NULL"), 0)

    with_root("2026-10-28 13:00", body)


# --- retry_fail ------------------------------------------------------------------------

def _ten_q_job(h: Harness):
    evs = h.sweep_10q("2026-10-28 16:50")
    jobs = h.queue()
    return evs, jobs


def case_retry_fail(r: Results):
    def once(h: Harness):
        evs, jobs = _ten_q_job(h)
        r.eq("retry_fail: the sweep made one 10-Q event and job", (len(evs), len(jobs)), (1, 1))
        if not jobs:
            return

        def fn(h, kind, prompt, js, ctx, attempt):
            if kind != "analyze_event":
                return
            if attempt == 1:
                raise RuntimeError("boom")
            verdict(ctx, js)

        script = Script(h, fn)
        w = h.worker(script)
        w.run_one(w.next_job())
        j = h.job(jobs[0])
        r.eq("retry_fail: one error -> queued, attempts 1", (j["status"], j["attempts"]), ("queued", 1))
        r.ok("retry_fail: the error is stored", "boom" in (j["error"] or ""), j["error"])
        r.eq("retry_fail: one error -> no owner message", h.sent(), [])
        w.drain()
        j = h.job(jobs[0])
        r.eq("retry_fail: then done on the second attempt", (j["status"], j["attempts"]), ("done", 2))
        r.eq("retry_fail: the event ends noted", h.one("SELECT status FROM events WHERE id=?", (evs[0],)), "noted")

    with_root("2026-10-28 16:45", once, new_10q=True)

    def twice(h: Harness):
        evs, jobs = _ten_q_job(h)
        if not jobs:
            r.ok("retry_fail: setup", False, "no job")
            return

        def fn(h, kind, prompt, js, ctx, attempt):
            if kind == "analyze_event":
                raise RuntimeError("boom")

        w = h.worker(Script(h, fn))
        w.drain()
        j = h.job(jobs[0])
        r.eq("retry_fail: two errors -> failed, attempts 2", (j["status"], j["attempts"]), ("failed", 2))
        r.eq("retry_fail: two errors -> event failed", h.one("SELECT status FROM events WHERE id=?", (evs[0],)),
             "failed")
        r.eq("retry_fail: the owner message (plan text)", h.sent(), [FAIL_TEXT.format("boom")])
        r.eq("retry_fail: one Telegram message", len(h.tr.texts()), 1)

    with_root("2026-10-28 16:45", twice, new_10q=True)


# --- no_verdict ------------------------------------------------------------------------

def case_no_verdict(r: Results):
    def body(h: Harness):
        evs, jobs = _ten_q_job(h)
        if not jobs:
            r.ok("no_verdict: setup", False, "no job")
            return

        def fn(h, kind, prompt, js, ctx, attempt):
            if kind == "analyze_event":
                ok_call(ctx, "get_company", {"ticker": "ANET"})

        script = Script(h, fn)
        w = h.worker(script)
        w.run_one(w.next_job())
        j = h.job(jobs[0])
        r.eq("no_verdict: the first try is retried", (j["status"], j["attempts"], j["error"]), ("queued", 1, "no verdict"))
        w.drain()
        j = h.job(jobs[0])
        r.eq("no_verdict: 'no verdict' after one retry", (j["status"], j["attempts"], j["error"]),
             ("failed", 2, "no verdict"))
        r.eq("no_verdict: the runner ran twice", script.attempts.get(jobs[0]), 2)
        r.eq("no_verdict: the event is failed", h.one("SELECT status FROM events WHERE id=?", (evs[0],)), "failed")
        r.eq("no_verdict: the owner is told", h.sent(), [FAIL_TEXT.format("no verdict")])

    with_root("2026-10-28 16:45", body, new_10q=True)


# --- drain -----------------------------------------------------------------------------

def case_drain(r: Results):
    def body(h: Harness):
        # Clear the seed's deep-dives through the worker itself, so only this case's jobs remain.
        quiet = h.worker(Script(h, lambda *a: None))
        quiet.drain()
        ev_a = h.level_event("ANET", "buy_below", 200.0, "2026-10-28 13:10")
        job_a = h.queue()
        added = {}

        def fn(h, kind, prompt, js, ctx, attempt):
            if js.job_id == job_a[0]:
                # Mid-run: a new event arrives and the monitor queues it.
                added["events"] = h.level_event("CEG", "trim_above", 250.0, "2026-10-28 13:15")
                added["jobs"] = h.queue()
            verdict(ctx, js)

        script = Script(h, fn)
        w = h.worker(script)
        n = w.drain()
        r.eq("drain: a job added mid-run is done before exit", n, 2)
        r.eq("drain: the runner ran both jobs in order", [c[1] for c in script.calls], job_a + added.get("jobs", []))
        r.eq("drain: both jobs are done", [h.job(j)["status"] for j in job_a + added.get("jobs", [])], ["done", "done"])
        r.eq("drain: the queue is empty at exit", h.one("SELECT COUNT(*) FROM jobs WHERE status='queued'"), 0)

        # A second worker exits on the lock.
        ev_x = h.level_event("ANET", "entry", 200.0, "2026-10-28 13:30")
        job_x = h.queue()
        ev_y = h.level_event("CEG", "buy_below", 400.0, "2026-10-28 13:31")
        job_y = h.queue()
        w.start(h.job(job_y[0]))          # job Y is live in another worker
        rc.touch_trigger(h.root.path, "worker", store.now_iso())
        other = f"{runlock.boot_id()}:{os.getppid()}"
        took = h.led.try_acquire_named_lock("worker", other, store.now_iso(), ttl_seconds=28800,
                                            holder_dead=runlock.holder_dead)
        r.ok("drain: another live worker holds the lock", took)
        guard = Script(h, fn=lambda h, kind, prompt, js, ctx, attempt: verdict(ctx, js))
        Real = Patched_prev[0]
        rw.Worker = lambda runtime, secrets: Real(runtime=runtime, secrets=secrets, led=h.led, box=h.box,
                                                  job_runner=guard, classify_fn=h.classify_fn,
                                                  fetch_document=h.fetch_document)
        env = dict(os.environ)
        before = len(log_lines())
        try:
            code = rw.main()
        finally:
            os.environ.clear()
            os.environ.update(env)
        r.eq("drain: the second worker exits 0", code, 0)
        r.eq("drain: the second worker runs no job", guard.calls, [])
        r.eq("drain: the queued job stays queued", h.job(job_x[0])["status"], "queued")
        r.eq("drain: the live job is not put back to queued", h.job(job_y[0])["status"], "running")
        r.ok("drain: the worker trigger is deleted", not (h.root.path / "state" / "triggers" / "worker").exists())
        skipped = [ln for ln in log_lines()[before:] if ln.get("skipped")]
        r.ok("drain: the skip is logged", len(skipped) == 1 and "worker lock" in skipped[0].get("reason", ""),
             log_lines()[before:])
        # The lock frees: the next worker puts the left 'running' job back and drains all.
        h.led.release_named_lock("worker", other)
        env = dict(os.environ)
        try:
            code = rw.main()
        finally:
            os.environ.clear()
            os.environ.update(env)
        r.eq("drain: the next worker requeues the left job and drains the queue",
             (h.job(job_x[0])["status"], h.job(job_y[0])["status"]), ("done", "done"))
        r.eq("drain: the next worker releases its lock", h.one("SELECT COUNT(*) FROM run_locks WHERE name='worker'"), 0)
        r.ok("drain: worker_ok is logged", any(ln.get("event") == "worker_ok" for ln in log_lines()), log_lines()[-3:])

    with_root("2026-10-28 13:00", body)


Patched_prev = [rw.Worker]


# --- prompt ----------------------------------------------------------------------------

def _body(prompt):
    m = re.search(r"```json\n(.*)\n```", prompt, re.S)
    return json.loads(m.group(1)) if m else None


def case_prompt(r: Results):
    def body(h: Harness):
        # Four ANET verdicts and one CEG verdict, at rising times.
        vctx_runner = {}

        def record(ev_ids, job_ids, v):
            for eid, jid in zip(ev_ids, job_ids):
                js = agent.JobState(job_id=jid, event_id=eid, role="analyst", kind="analyze_event")
                ctx = tools.ToolContext.build(role="analyst", runtime=h.rt, secrets=h.sec, job=js, led=h.led,
                                              transport=h.tr, box=h.box)
                h.clock.advance(60)
                verdict(ctx, js, v)
                vctx_runner.setdefault("ids", []).append(eid)

        e1 = h.level_event("ANET", "buy_below", 200.0, "2026-10-26 11:00"); record(e1, h.queue(), "intact")
        e2 = h.level_event("ANET", "entry", 200.0, "2026-10-26 11:30"); record(e2, h.queue(), "strengthened")
        e3 = h.level_event("ANET", "trim_above", 100.0, "2026-10-26 12:00"); record(e3, h.queue(), "weakened")
        e4 = h.level_event("ANET", "buy_below", 200.0, "2026-10-27 11:00"); record(e4, h.queue(), "intact")
        e5 = h.level_event("CEG", "trim_above", 250.0, "2026-10-27 11:30"); record(e5, h.queue(), "intact")
        r.eq("prompt: setup made five verdict events", [len(x) for x in (e1, e2, e3, e4, e5)], [1, 1, 1, 1, 1])
        # An open drift proposal for ANET.
        snap_at = "2026-10-28T20:20:00Z"
        snapshot(h.root, at=snap_at)
        h.clock.set(snap_at)
        with h.led.transaction() as c:
            built = proposals.build_trade(h.led, c, cfg=h.cfg, tickers=["ANET"], origin={}, python_origin="drift",
                                          rationale="The book drifted outside the agreed bands.", evidence=[],
                                          created_by="monitor", now_dt=h.clock.now, yf_price_fn=FakePrices())
        # The new 10-Q event.
        evs = h.sweep_10q("2026-10-28 16:30")
        jobs = h.queue()
        r.eq("prompt: the sweep made the 10-Q event", len(evs), 1)
        if not evs:
            return
        seen = {}

        def fn(h, kind, prompt, js, ctx, attempt):
            seen["dossier"] = json.loads(json.dumps(tools.t_get_company(ctx, {"ticker": "ANET"}), default=str))
            seen["prompt"] = prompt
            verdict(ctx, js)

        w = h.worker(Script(h, fn))
        job = h.job(jobs[0])
        w.run_one(job)
        prompt = seen.get("prompt", "")
        b = _body(prompt)
        r.ok("prompt: the prompt holds a JSON body", b is not None, prompt[:400])
        if b is None:
            return
        r.ok("prompt: the prompt asks for record_verdict on the event", f"record_verdict for event {evs[0]}" in prompt,
             prompt[:300])
        ev = b.get("event", {})
        r.eq("prompt: the event", (ev.get("id"), ev.get("ticker"), ev.get("type"),
                                   (ev.get("detail") or {}).get("accession"), (ev.get("detail") or {}).get("form")),
             (evs[0], "ANET", "filing", Q2_10Q, "10-Q"))
        r.eq("prompt: the dossier is get_company's answer", b.get("dossier"), seen.get("dossier"))
        r.eq("prompt: the dossier names ANET", ((b.get("dossier") or {}).get("company") or {}).get("ticker"), "ANET")
        docs = h.q("SELECT id AS document_id, section_key, char_len FROM documents WHERE accession=? ORDER BY id",
                   (Q2_10Q,))
        r.ok("prompt: the 10-Q has stored sections", len(docs) > 0 and all(d["char_len"] > 0 and d["section_key"]
                                                                          for d in docs), docs)
        r.eq("prompt: new document ids with keys and sizes", b.get("new_documents"), docs)
        lv = b.get("last_verdicts") or []
        r.eq("prompt: the last 3 ANET verdicts, newest first", [v.get("id") for v in lv], [e4[0], e3[0], e2[0]])
        r.eq("prompt: each verdict carries its verdict word", [v.get("verdict") for v in lv],
             ["intact", "weakened", "strengthened"])
        ops = b.get("open_proposals") or []
        r.eq("prompt: the open proposals for the name", [p.get("id") for p in ops], [built["proposal_id"]])
        r.ok("prompt: the JSON body was not cut", prompt.rstrip().endswith("```"), len(prompt))

    with_root("2026-10-26 10:00", body, new_10q=True)


# --- desk-review ------------------------------------------------------------------------

def case_review(r: Results):
    from lib.desk import seed

    def body(h: Harness):
        snapshot(h.root, at=store.iso(h.clock.now))
        out = seed.queue_reviews(h.led, now=store.now_iso())
        r.eq("review: a holding with a queued seed deep-dive is skipped", (out["queued"], sorted(out["skipped"])),
             ([], ["ANET", "CEG", "SGOV"]))
        r.eq("review: SGOV is a cash fund", out["skipped"].get("SGOV"), "a cash fund")
        with h.led.connect() as c:
            c.execute("UPDATE jobs SET status='done' WHERE kind='deep_dive'")
        out = seed.queue_reviews(h.led, now=store.now_iso())
        held = json.loads(h.q("SELECT positions_json FROM broker_snapshots ORDER BY id DESC LIMIT 1")[0]["positions_json"])
        by_size = [t for t in sorted(held, key=lambda t: -held[t]["market_value"]) if t != "SGOV"]
        r.eq("review: one review per holding, largest first", out["queued"], by_size)
        jobs = h.q("SELECT payload_json, requested_by FROM jobs WHERE kind='deep_dive' AND status='queued' ORDER BY id")
        r.eq("review: each job carries the review focus",
             [(json.loads(j["payload_json"])["ticker"], json.loads(j["payload_json"])["focus"], j["requested_by"])
              for j in jobs], [(t, seed.REVIEW_FOCUS, "desk-review") for t in by_size])
        again = seed.queue_reviews(h.led, now=store.now_iso())
        r.eq("review: a second run queues nothing", again["queued"], [])
        only = seed.queue_reviews(h.led, now=store.now_iso(), tickers=["nvda"])
        r.eq("review: a ticker not held is reported", only, {"ok": True, "queued": [], "skipped":
                                                               {"NVDA": "not held in the newest snapshot"}})
        w = h.worker(Script(h, lambda *a: None))
        prompt = w.deep_dive_prompt(json.loads(jobs[0]["payload_json"]))
        r.ok("review: the deep-dive prompt carries the focus", seed.REVIEW_FOCUS in prompt, prompt)

    with_root("2026-10-28 16:45", body)


def main() -> int:
    r = Results("run-worker")
    for case in (case_order, case_retry_fail, case_no_verdict, case_drain, case_prompt, case_review):
        try:
            case(r)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__}: crashed", False, f"{type(e).__name__}: {e}")
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
