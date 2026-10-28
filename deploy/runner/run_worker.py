#!/usr/bin/env python3
"""The worker: the analyst jobs and the news classifier (plan §9.2).

``quiver-worker.path`` starts this unit while ``state/triggers/worker`` exists. The runner
deletes the trigger, puts jobs left in ``running`` back to ``queued``, takes the ``worker``
run lock, and drains ``jobs``: ``classify_news`` first, then ``analyze_event`` (oldest
first), then ``deep_dive``, one at a time. It checks the queue again before it exits, so a
job queued during the drain runs too.

A job that fails runs once more; the second failure marks it ``failed`` and tells the owner.
An ``analyze_event`` job must end with ``record_verdict``. The analyst has no broker access
(the unit hides the Robinhood config) and no shell: its tools are the A set of
``lib/desk/tools.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import runner_common as rc  # noqa: E402
from lib import market, runlock  # noqa: E402
from lib.desk import agent, documents, news, outbox, store, tools  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
from lib.prompts import load_prompt  # noqa: E402

LOCK_TTL_S = 28800
TIMEOUTS = {"analyze_event": 900, "deep_dive": 1800, "classify_news": 120}
ORDER = ("classify_news", "analyze_event", "deep_dive")
MAX_ATTEMPTS = 2


class JobFailed(RuntimeError):
    pass


def _texts(messages) -> str:
    out = []
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, list):
            for b in content:
                t = getattr(b, "text", None)
                if isinstance(t, str):
                    out.append(t)
        r = getattr(m, "result", None)
        if isinstance(r, str) and r and not out:
            out.append(r)
    return "\n".join(out)


async def _run_query(prompt: str, options, timeout: float) -> list:
    """One SDK query with a wall-clock limit. Raises JobFailed on an error result."""
    from claude_agent_sdk import ResultMessage, query
    import anyio
    msgs = []
    with anyio.fail_after(timeout):
        async for m in query(prompt=prompt, options=options):
            msgs.append(m)
            if isinstance(m, ResultMessage) and m.is_error:
                raise JobFailed(f"{m.subtype}: {(m.result or '')[:200]}")
    return msgs


class Worker:
    def __init__(self, *, runtime, secrets, led=None, box=None, job_runner=None, classify_fn=None,
                 fetch_document=None, ctx_factory=None):
        self.runtime = runtime
        self.secrets = secrets
        self.led = led or Ledger(runtime.ledger_db)
        if box is None:
            box = outbox.Outbox(self.led, transport=outbox.telegram_transport(secrets.telegram_bot_token),
                                chat_id=(secrets.owner_ids or [None])[0])
        self.box = box
        self.job_runner = job_runner or self.run_agent_job
        self.classify_fn = classify_fn or self.classify_with_model
        self.fetch_document = fetch_document or self._fetch_news_document
        self.ctx_factory = ctx_factory

    # --- the queue ------------------------------------------------------------------
    def requeue_running(self) -> int:
        with self.led.connect() as conn:
            return conn.execute("UPDATE jobs SET status='queued' WHERE status='running'").rowcount

    def next_job(self):
        with self.led.connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY CASE kind WHEN 'classify_news' THEN 0 "
                "WHEN 'analyze_event' THEN 1 ELSE 2 END, created_at, id LIMIT 1").fetchone()
            return dict(row) if row else None

    def start(self, job) -> int:
        with self.led.connect() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id=?",
                         (store.now_iso(), job["id"]))
            return int(conn.execute("SELECT attempts FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])

    def finish(self, job, status, error=None):
        with self.led.connect() as conn:
            conn.execute("UPDATE jobs SET status=?, finished_at=?, error=? WHERE id=?",
                         (status, store.now_iso(), error, job["id"]))

    # --- the jobs -------------------------------------------------------------------
    def classify_with_model(self, items: list) -> str:
        opts = agent.small_job_options(self.runtime, system_prompt=load_prompt("news_classifier"))
        msgs = asyncio.run(_run_query(json.dumps(items), opts, TIMEOUTS["classify_news"]))
        return _texts(msgs)

    def _fetch_news_document(self, url, kind="news", ticker=None):
        with self.led.connect() as conn:
            return documents.store_fetched(conn, url, kind=kind, ticker=ticker, now=store.now_iso())

    def run_classify(self, job):
        payload = store.loads(job["payload_json"], {}) or {}
        with self.led.connect() as conn:
            items = news.classifier_input(conn, payload)
        reply = ""
        error = None
        if items:
            try:
                reply = self.classify_fn(items)
            except Exception as e:  # noqa: BLE001 — a failed call counts one attempt per headline
                error = f"{type(e).__name__}: {e}"
        with self.led.connect() as conn:
            res = news.apply_classification(conn, payload, reply, now=store.now_iso())
            ev = news.handle_material(conn, now=store.now_iso(), fetch_document=self.fetch_document)
        if ev:
            rc._emit({"stage": "news", "events": ev})
        if error:
            raise JobFailed(error)
        return res

    def event_prompt(self, job_state, event: dict) -> str:
        """The analyze_event prompt (§9.2): the event, the dossier, the new documents, the
        last three verdicts for the name, and its open proposals."""
        ctx = self.make_ctx(job_state)
        t = event["ticker"]
        detail = store.loads(event["detail_json"], {}) or {}
        try:
            dossier = tools.t_get_company(ctx, {"ticker": t})
        except tools.ToolError as e:
            dossier = {"error": str(e)}
        with self.led.connect() as conn:
            docs = []
            if detail.get("accession"):
                docs = [dict(r) for r in conn.execute(
                    "SELECT id AS document_id, section_key, char_len FROM documents WHERE accession=? ORDER BY id",
                    (detail["accession"],)).fetchall()]
            if detail.get("document_id"):
                docs.append({"document_id": detail["document_id"], "section_key": "news", "char_len": None})
            verdicts = [dict(r) for r in conn.execute(
                "SELECT id, type, verdict, analysis, analyzed_at FROM events WHERE ticker=? AND verdict IS NOT NULL "
                "AND id != ? ORDER BY analyzed_at DESC LIMIT 3", (t, event["id"])).fetchall()]
            props = [dict(r) for r in conn.execute(
                "SELECT DISTINCT p.id, p.kind, p.state, p.rationale FROM proposals p LEFT JOIN orders o "
                "ON o.proposal_id=p.id WHERE p.state IN ('pending','approved','executing') AND "
                "(o.ticker=? OR p.payload_json LIKE ?)", (t, f'%"ticker":"{t}"%')).fetchall()]
        body = {"job": "analyze_event", "event": {"id": event["id"], "ticker": t, "type": event["type"],
                                                  "created_by": event["created_by"], "detail": detail,
                                                  "detected_at": event["detected_at"]},
                "new_documents": docs, "last_verdicts": verdicts, "open_proposals": props, "dossier": dossier}
        return ("Analyze this event. Follow the analyst instructions. End with record_verdict for event "
                f"{event['id']}.\n\n```json\n" + json.dumps(body, default=str)[:60000] + "\n```")

    def deep_dive_prompt(self, payload: dict) -> str:
        t = payload["ticker"]
        focus = payload.get("focus")
        return (f"Run a deep-dive on {t}." + (f" Focus: {focus}" if focus else "") +
                " Follow the analyst instructions for a deep-dive. Start with get_company and list_filings.")

    def make_ctx(self, job_state):
        if self.ctx_factory:
            return self.ctx_factory(job_state)
        return tools.ToolContext.build(role="analyst", runtime=self.runtime, secrets=self.secrets, job=job_state,
                                       led=self.led, box=self.box)

    def run_agent_job(self, kind: str, prompt: str, job_state) -> None:
        ctx = self.make_ctx(job_state)
        opts = agent.analyst_options(self.runtime, self.secrets, job_state, ctx=ctx)
        asyncio.run(_run_query(prompt, opts, TIMEOUTS[kind]))

    def run_event(self, job):
        payload = store.loads(job["payload_json"], {}) or {}
        with self.led.connect() as conn:
            ev = store.get_event(conn, int(payload["event_id"]))
        if ev is None:
            raise JobFailed(f"event {payload.get('event_id')} does not exist")
        js = agent.JobState(job_id=job["id"], event_id=ev["id"], role="analyst", kind="analyze_event")
        self.job_runner("analyze_event", self.event_prompt(js, ev), js)
        with self.led.connect() as conn:
            row = store.get_event(conn, ev["id"])
        # The job's own event decides: a verdict on another event never counts (the tool refuses it).
        if (row or {}).get("status") != "analyzed":
            raise JobFailed("no verdict")
        with self.led.connect() as conn:
            named = conn.execute("SELECT 1 FROM proposals WHERE event_id=? LIMIT 1", (ev["id"],)).fetchone()
            conn.execute("UPDATE events SET status=? WHERE id=? AND status='analyzed'",
                         ("proposed" if named or js.proposal_ids else "noted", ev["id"]))
        return {"event_id": ev["id"], "proposals": js.proposal_ids}

    def run_deep_dive(self, job):
        payload = store.loads(job["payload_json"], {}) or {}
        js = agent.JobState(job_id=job["id"], role="analyst", kind="deep_dive")
        self.job_runner("deep_dive", self.deep_dive_prompt(payload), js)
        return {"ticker": payload.get("ticker"), "proposals": js.proposal_ids, "messages": js.messages}

    def describe(self, job) -> str:
        payload = store.loads(job["payload_json"], {}) or {}
        if job["kind"] == "analyze_event":
            with self.led.connect() as conn:
                ev = store.get_event(conn, int(payload.get("event_id") or 0)) or {}
            d = store.loads(ev.get("detail_json"), {}) or {}
            form = d.get("form") or ev.get("type", "event")
            return f"analyze {ev.get('ticker', '?')}'s {form}"
        if job["kind"] == "deep_dive":
            return f"finish the deep-dive on {payload.get('ticker', '?')}"
        return "classify the news"

    def run_one(self, job):
        attempts = self.start(job)
        try:
            if job["kind"] == "classify_news":
                res = self.run_classify(job)
            elif job["kind"] == "analyze_event":
                res = self.run_event(job)
            else:
                res = self.run_deep_dive(job)
            self.finish(job, "done")
            rc._emit({"stage": "worker", "job": job["id"], "kind": job["kind"], "ok": True,
                      "result": res if isinstance(res, dict) else None})
        except Exception as e:  # noqa: BLE001
            err = str(e) or type(e).__name__
            if attempts < MAX_ATTEMPTS:
                self.finish(job, "queued", err)
            else:
                self.finish(job, "failed", err)
                if job["kind"] == "analyze_event":
                    payload = store.loads(job["payload_json"], {}) or {}
                    with self.led.connect() as conn:
                        conn.execute("UPDATE events SET status='failed' WHERE id=?", (int(payload.get("event_id") or 0),))
                if job["kind"] != "classify_news":
                    self.box.send(f"I could not {self.describe(job)} (error: {err[:200]}). I stopped trying.",
                                  source="analyst")
            rc._emit({"stage": "worker", "job": job["id"], "kind": job["kind"], "ok": False, "attempts": attempts,
                      "error": err[:300]})

    def drain(self) -> int:
        n = 0
        while True:
            job = self.next_job()
            if job is None:
                return n
            self.run_one(job)
            n += 1


def main() -> int:
    runtime = agent.Runtime.from_env()
    secrets = agent.Secrets.from_env()
    agent.scrub_process_env()
    trig = Path(runtime.root) / "state" / "triggers" / "worker"
    try:
        trig.unlink()
    except FileNotFoundError:
        pass
    w = Worker(runtime=runtime, secrets=secrets)
    holder = runlock.holder_id()
    try:
        with runlock.run_lock(w.led, "worker", holder, store.now_iso(), LOCK_TTL_S):
            # Inside the lock: a second worker that exits on the lock never requeues a live job.
            w.requeue_running()
            n = w.drain()
            rc._emit({"stage": "worker", "event": "worker_ok", "jobs": n})
    except runlock.RunLockError as e:
        rc._emit({"stage": "worker", "skipped": True, "reason": str(e)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
