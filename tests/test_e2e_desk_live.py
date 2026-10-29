"""The live desk e2e (plan §18.7). Gated: the cases run only with ``QUIVER_LIVE_E2E=1``.

Without the gate it prints ``0 passed, 0 failed, 3 skipped``. With the gate it needs
``CLAUDE_CODE_OAUTH_TOKEN`` and ``CLAUDE_BIN``; a missing one FAILS the case, it does not skip.
Every case bills the subscription token. Run ``tests/run_e2e.sh --live`` before a cutover and
after a prompt change.

Each case uses a temp root made by the real ``desk-seed`` (ANET and CEG loaded from the
captured SEC files), the real tool server, a ``FakeTelegram`` that replays the captured Bot API
answers, and a temp ``CLAUDE_CONFIG_DIR``. No broker. ``WebSearch`` is not a desk tool.

  * desk_turn: one real desk turn (``QUIVER_DESK_MODEL``) through ``chat_bridge.Bridge``.
  * deep_dive: the seed's ANET ``deep_dive`` job (``QUIVER_ANALYST_MODEL``) through
    ``run_worker.Worker.run_one``.
  * research: the owner asks about NVDA, a name that is not loaded. The desk loads it from the
    real SEC EDGAR (it needs ``SEC_USER_AGENT``) and queues a deep-dive with the question as its
    focus; then the worker runs that deep-dive. Every message must be in plain English.

Run one case: ``QUIVER_LIVE_E2E=1 .venv/bin/python tests/test_e2e_desk_live.py desk_turn``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import REPO, Results, fx, seed_desk, temp_root  # noqa: E402

sys.path.insert(0, str(REPO / "deploy" / "runner"))

OWNER = 111
QUESTION = ("What did ANET say about AI networking in its last 10-Q? "
            "Two or three short messages.")


class FakeTelegram:
    """Replays the captured Bot API answers and records every call."""

    def __init__(self):
        self.tg = fx("tg")
        self.calls = []
        self.next_id = 5000

    def __call__(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "sendMessage":
            ans = copy.deepcopy(self.tg["sendMessage"])
            self.next_id += 1
            ans["result"].update(message_id=self.next_id, text=str(params.get("text", "")))
            ans["result"].pop("reply_markup", None)
            if params.get("reply_markup"):
                ans["result"]["reply_markup"] = params["reply_markup"]
            return ans
        if method in ("setMessageReaction", "sendChatAction", "editMessageReplyMarkup"):
            return copy.deepcopy(self.tg[method])
        return {"ok": True, "result": True}

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def reactions(self):
        return [p for m, p in self.calls if m == "setMessageReaction"]


def owner_update(update_id: int, message_id: int, text: str) -> dict:
    """An owner message update, made from the captured Message object (values changed)."""
    msg = copy.deepcopy(fx("tg")["sendMessage"]["result"])
    msg.pop("reply_markup", None)
    msg.pop("entities", None)
    msg.update(message_id=message_id, date=int(time.time()), text=text,
               **{"from": {"id": OWNER, "is_bot": False, "first_name": "Owner", "username": "owner_test"}})
    return {"update_id": update_id, "message": msg}


def need_env(R, label) -> bool:
    missing = [k for k in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_BIN") if not os.environ.get(k)]
    R.ok(f"{label}: CLAUDE_CODE_OAUTH_TOKEN and CLAUDE_BIN are set", not missing, missing)
    return not missing


def build_runtime(root):
    """Runtime and Secrets for a desk process in the temp root. The fake Telegram token
    never reaches the network: every call goes through ``FakeTelegram``."""
    from lib.desk import agent
    cfg_dir = root.path / "state" / "claude-desk"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
    runtime = agent.Runtime.from_env()
    secrets = agent.Secrets(telegram_bot_token="x", telegram_allowed_chat_ids=str(OWNER),
                            sec_user_agent=os.environ.get("SEC_USER_AGENT", ""))
    return runtime, secrets


class Recorder:
    """Wraps the real ``ClaudeSDKClient`` and records the name of every tool call."""

    def __init__(self, inner):
        self.inner = inner
        self.tools = []
        self.results = []

    async def query(self, prompt):
        await self.inner.query(prompt)

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
        async for m in self.inner.receive_response():
            if isinstance(m, AssistantMessage):
                self.tools += [b.name for b in m.content if isinstance(b, ToolUseBlock)]
            elif isinstance(m, ResultMessage):
                self.results.append({"is_error": m.is_error, "subtype": m.subtype,
                                     "usage": getattr(m, "usage", None)})
            yield m

    async def interrupt(self):
        await self.inner.interrupt()

    async def disconnect(self):
        await self.inner.disconnect()


def case_desk_turn(R):
    if not need_env(R, "desk_turn"):
        return
    import chat_bridge
    from lib.desk import agent, reactions
    with temp_root() as root:
        seed_desk(root)
        runtime, secrets = build_runtime(root)
        agent.scrub_process_env()
        tg = FakeTelegram()
        rec = {}

        async def factory(options):
            from claude_agent_sdk import ClaudeSDKClient
            client = ClaudeSDKClient(options=options)
            await client.connect()
            rec["c"] = Recorder(client)
            return rec["c"]

        bridge = chat_bridge.Bridge(runtime=runtime, secrets=secrets, led=root.led(), transport=tg,
                                    client_factory=factory)

        async def go():
            await bridge.handle_update(owner_update(1, 7001, QUESTION), offline=False)
            await bridge.maybe_turn()
            await bridge.turn_task
            await bridge.disconnect()

        t0 = time.monotonic()
        asyncio.run(go())
        secs = round(time.monotonic() - t0, 1)
        tools = rec["c"].tools if "c" in rec else []
        print(f"  desk_turn: {secs}s, tools={tools}, results={rec['c'].results if 'c' in rec else None}")
        R.ok("desk_turn: the first tool call is react", tools[:1] == ["mcp__desk__react"], tools)
        reacts = tg.reactions()
        emoji = (((reacts[0].get("reaction") or [{}])[0]).get("emoji") if reacts else None)
        R.ok("desk_turn: the reaction emoji is in the 73 allowed", reactions.check(emoji or "") is not None,
             emoji)
        R.ok("desk_turn: the reaction is on the owner's message",
             bool(reacts) and int(reacts[0].get("message_id", 0)) == 7001, reacts[:1])
        R.ok("desk_turn: one to three say calls (the voice rule)", 1 <= tools.count("mcp__desk__say") <= 3, tools)
        R.ok("desk_turn: one or more search_research calls", "mcp__desk__search_research" in tools, tools)
        sent = [p for p in tg.sent() if str(p.get("chat_id")) == str(OWNER)]
        R.ok("desk_turn: one or more messages reach the owner chat", len(sent) >= 1, len(sent))
        R.eq("desk_turn: no report labels or counters", labels(p.get("text") for p in sent), [])
        R.eq("desk_turn: one message per say (no recap after the last say)", len(sent),
             tools.count("mcp__desk__say"))
        R.ok("desk_turn: no error result", all(not r["is_error"] for r in rec["c"].results) if "c" in rec
             else False, rec["c"].results if "c" in rec else None)
        with root.led().connect() as c:
            row = c.execute("SELECT session_id, last_turn_at FROM chat_sessions WHERE chat_id=?",
                            (str(OWNER),)).fetchone()
            undelivered = c.execute("SELECT COUNT(*) FROM chat_messages WHERE role='owner' "
                                    "AND delivered_to_session=0").fetchone()[0]
        R.ok("desk_turn: the session id is stored", bool(row and row[0] and row[1]), row and tuple(row))
        R.eq("desk_turn: the owner message is delivered", undelivered, 0)
        for p in sent:
            print("    say>", str(p.get("text"))[:300].replace("\n", " "))


def case_deep_dive(R):
    if not need_env(R, "deep_dive"):
        return
    import run_worker
    from lib.desk import agent, findings as findings_mod, outbox, store
    with temp_root() as root:
        seed_desk(root)
        runtime, secrets = build_runtime(root)
        agent.scrub_process_env()
        tg = FakeTelegram()
        led = root.led()
        box = outbox.Outbox(led, transport=tg, chat_id=str(OWNER))
        worker = run_worker.Worker(runtime=runtime, secrets=secrets, led=led, box=box)
        captured = []
        orig_query = run_worker._run_query

        async def recording_query(prompt, options, timeout):
            msgs = await orig_query(prompt, options, timeout)
            captured.extend(msgs)
            return msgs
        run_worker._run_query = recording_query
        with led.connect() as c:
            job = c.execute("SELECT * FROM jobs WHERE kind='deep_dive' AND payload_json LIKE '%\"ANET\"%'"
                            ).fetchone()
        R.ok("deep_dive: desk-seed queued the ANET deep_dive", job is not None)
        if job is None:
            return
        job = dict(job)
        t0 = time.monotonic()
        try:
            worker.run_one(job)
        finally:
            run_worker._run_query = orig_query
        secs = round(time.monotonic() - t0, 1)
        from claude_agent_sdk import AssistantMessage, ToolUseBlock
        calls = [b for m in captured if isinstance(m, AssistantMessage) for b in m.content
                 if isinstance(b, ToolUseBlock)]
        print("  deep_dive tools:", [b.name.replace("mcp__desk__", "") for b in calls])
        for b in calls:
            if b.name.endswith("propose_change"):
                print("    propose_change input:", json.dumps(b.input)[:700])
        with led.connect() as c:
            j = dict(c.execute("SELECT status, error, attempts FROM jobs WHERE id=?", (job["id"],)).fetchone())
            finds = [dict(r) for r in c.execute(
                "SELECT f.*, d.text AS doc_text FROM findings f JOIN documents d ON d.id=f.document_id "
                "WHERE f.ticker='ANET' AND f.created_by='analyst'").fetchall()]
            props_all = [dict(r) for r in c.execute(
                "SELECT id, state, superseded_by, payload_json FROM proposals WHERE kind='change'").fetchall()]
            props = [p for p in props_all if p["state"] == "pending"]
            ids = {int(r[0]) for r in c.execute("SELECT id FROM findings").fetchall()}
        print(f"  deep_dive: {secs}s, job={j}, findings={len(finds)}, kinds={sorted({f['kind'] for f in finds})}")
        print("  change proposals:", [(p["id"], p["state"], p["superseded_by"]) for p in props_all])
        R.eq("deep_dive: the job is done", j["status"], "done")
        R.ok("deep_dive: done within 1,800 s", secs <= 1800, secs)
        R.ok("deep_dive: 8 or more analyst findings", len(finds) >= 8, len(finds))
        # findings.py stores the normalized quote and its offset in the normalized document.
        bad = [f["id"] for f in finds
               if findings_mod.normalize(f["doc_text"])[f["quote_start"]:f["quote_start"] + len(f["quote"])]
               != f["quote"]]
        R.eq("deep_dive: every finding quote is at its quote_start in its normalized document", bad, [])
        R.ok("deep_dive: 5 or more finding kinds", len({f["kind"] for f in finds}) >= 5,
             sorted({f["kind"] for f in finds}))
        def change_kind(p):
            return ((store.loads(p["payload_json"], {}) or {}).get("change") or {}).get("kind")
        thesis = [p for p in props if change_kind(p) == "set_thesis"]
        R.ok("deep_dive: a pending set_thesis proposal", len(thesis) >= 1, [p["id"] for p in props])
        if thesis:
            pay = (store.loads(thesis[0]["payload_json"], {}) or {}).get("change") or {}
            scen = ((pay.get("valuation") or {}).get("scenarios") or {})
            cited = set()
            for sc in scen.values() if isinstance(scen, dict) else []:
                if not isinstance(sc, dict):
                    continue
                for k in ("finding_ids", "evidence"):
                    for e in sc.get(k) or []:
                        v = e.get("finding_id") if isinstance(e, dict) else e
                        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
                            cited.add(int(v))
                if sc.get("finding_id") is not None:
                    cited.add(int(sc["finding_id"]))
            R.ok("deep_dive: the thesis scenarios cite findings", bool(cited), scen)
            R.eq("deep_dive: every cited finding exists", sorted(cited - ids), [])
        sent = [p for p in tg.sent() if str(p.get("chat_id")) == str(OWNER)]
        R.eq("deep_dive: the worker sends nothing to the owner (plan §4.8)", len(sent), 0)
        it = job_item(led, job["id"])
        print("  agenda item:", {k: it.get(k) for k in ("id", "kind", "priority", "status", "title", "proposal_ids")})
        print("    report>", str(it.get("body"))[:1500].replace("\n", " / "))
        R.eq("deep_dive: the job's item waits on the agenda, priority next", (it.get("status"), it.get("priority")),
             ("waiting", 1))
        R.ok("deep_dive: the item holds the report", 200 <= len(it.get("body") or "") <= 3000,
             len(it.get("body") or ""))
        R.ok("deep_dive: the item holds the pending set_thesis card",
             any(p["id"] in (it.get("proposal_ids") or []) for p in thesis), (it.get("proposal_ids"), thesis))
        R.eq("deep_dive: the report has no report labels or counters", labels([it.get("body")]), [])
        R.eq("deep_dive: the report uses no finance jargon", jargon([it.get("body")]), [])


RESEARCH_Q = "Can you research NVDA for me? I want to know if it is a good buy right now."
# Words the plain-English rules (prompts/desk.md, prompts/analyst.md) replace.
JARGON = ("bear case", "bull case", "base case", "fair value", "yoy", "adx", "the book", "bps")


def jargon(texts) -> list:
    return sorted({w for t in texts for w in JARGON if w in str(t).lower()})


# Report-style labels and counters the voice rules (prompts/desk.md, prompts/analyst.md) forbid.
LABELS = re.compile(r"(?m)(^|\n|\. )(Good|Bad|Value|Verdict|Card|Risks?|Answer)\s*:|\(\d/\d\)|\b\d of \d\b")


def labels(texts) -> list:
    return sorted({m.group(0).strip() for t in texts for m in LABELS.finditer(str(t))})


def job_item(led, job_id) -> dict:
    """The agenda item of one analyst job (plan §4.8)."""
    from lib.desk import agenda
    with led.connect() as c:
        row = c.execute("SELECT id FROM agenda WHERE job_id=?", (int(job_id),)).fetchone()
        return agenda.get(c, row[0]) if row else {}


def case_research(R):
    if not need_env(R, "research"):
        return
    R.ok("research: SEC_USER_AGENT is set", bool(os.environ.get("SEC_USER_AGENT")))
    import chat_bridge
    import run_worker
    from lib.desk import agent, findings as findings_mod, outbox, store
    with temp_root() as root:
        seed_desk(root)
        runtime, secrets = build_runtime(root)
        agent.scrub_process_env()
        tg = FakeTelegram()
        rec = {}

        async def factory(options):
            from claude_agent_sdk import ClaudeSDKClient
            client = ClaudeSDKClient(options=options)
            await client.connect()
            rec["c"] = Recorder(client)
            return rec["c"]

        led = root.led()
        bridge = chat_bridge.Bridge(runtime=runtime, secrets=secrets, led=led, transport=tg, client_factory=factory)

        async def go():
            await bridge.handle_update(owner_update(1, 7101, RESEARCH_Q), offline=False)
            await bridge.maybe_turn()
            await bridge.turn_task
            await bridge.disconnect()

        t0 = time.monotonic()
        asyncio.run(go())
        tools = rec["c"].tools if "c" in rec else []
        print(f"  research turn: {round(time.monotonic() - t0, 1)}s, tools={tools}")
        said = [p for p in tg.sent() if str(p.get("chat_id")) == str(OWNER)]
        for p in said:
            print("    say>", str(p.get("text"))[:300].replace("\n", " "))
        R.ok("research: the first tool call is react", tools[:1] == ["mcp__desk__react"], tools)
        R.ok("research: the desk loads NVDA", "mcp__desk__load_company" in tools, tools)
        R.ok("research: the desk starts a deep-dive", "mcp__desk__start_deep_dive" in tools, tools)
        R.ok("research: the desk answers in the chat", len(said) >= 1, len(said))
        R.eq("research: the desk's messages use no finance jargon", jargon(p.get("text") for p in said), [])
        with led.connect() as c:
            comp = store.get_company(c, "NVDA")
            job = c.execute("SELECT * FROM jobs WHERE kind='deep_dive' AND status='queued' "
                            "AND payload_json LIKE '%\"NVDA\"%'").fetchone()
        R.ok("research: NVDA is loaded as a candidate", bool(comp) and comp["status"] == "candidate", comp)
        R.ok("research: a queued NVDA deep-dive", job is not None)
        if job is None:
            return
        job = dict(job)
        focus = (store.loads(job["payload_json"], {}) or {}).get("focus")
        R.ok("research: the deep-dive carries the owner's question as its focus", bool(focus), focus)
        # The analyst runs it.
        tg2 = FakeTelegram()
        box = outbox.Outbox(led, transport=tg2, chat_id=str(OWNER))
        worker = run_worker.Worker(runtime=runtime, secrets=secrets, led=led, box=box)
        t1 = time.monotonic()
        worker.run_one(job)
        secs = round(time.monotonic() - t1, 1)
        with led.connect() as c:
            j = dict(c.execute("SELECT status, error, attempts FROM jobs WHERE id=?", (job["id"],)).fetchone())
            finds = [dict(r) for r in c.execute(
                "SELECT f.*, d.text AS doc_text FROM findings f JOIN documents d ON d.id=f.document_id "
                "WHERE f.ticker='NVDA' AND f.created_by='analyst'").fetchall()]
            cards = [dict(r) for r in c.execute("SELECT id, state, payload_json FROM proposals WHERE kind='change' "
                                                "AND state='pending' AND payload_json LIKE '%\"NVDA\"%'")]
        sent = [p for p in tg2.sent() if str(p.get("chat_id")) == str(OWNER)]
        it = job_item(led, job["id"])
        print(f"  research deep-dive: {secs}s, job={j}, findings={len(finds)}, cards={[c_['id'] for c_ in cards]}")
        print("    report>", str(it.get("body"))[:1500].replace("\n", " / "))
        R.eq("research: the NVDA deep-dive is done", j["status"], "done")
        R.ok("research: 8 or more NVDA findings", len(finds) >= 8, len(finds))
        bad = [f["id"] for f in finds
               if findings_mod.normalize(f["doc_text"])[f["quote_start"]:f["quote_start"] + len(f["quote"])]
               != f["quote"]]
        R.eq("research: every NVDA quote is in its document", bad, [])
        R.ok("research: a pending NVDA card", len(cards) >= 1, cards)
        R.eq("research: the worker sends nothing to the owner", len(sent), 0)
        R.eq("research: the owner's deep-dive waits with priority now", (it.get("status"), it.get("priority")),
             ("waiting", 0))
        R.eq("research: the report has no report labels or counters", labels([it.get("body")]), [])
        R.eq("research: the report uses no finance jargon", jargon([it.get("body")]), [])

        # The desk brings the item up in its own words; the card follows (plan §4.8).
        before = len(tg.sent())
        bridge.last_owner_at = bridge.last_tap_at = bridge.last_owner_turn_end = None

        async def present():
            await bridge.maybe_turn()
            if bridge.turn_task is not None:
                await bridge.turn_task
            await bridge.disconnect()
        t2 = time.monotonic()
        asyncio.run(present())
        up = [p for p in tg.sent()[before:] if str(p.get("chat_id")) == str(OWNER)]
        words = [str(p.get("text")) for p in up if not p.get("reply_markup")]
        print(f"  agenda turn: {round(time.monotonic() - t2, 1)}s")
        for p in up:
            print("    up>", str(p.get("text"))[:500].replace("\n", " / "))
        with led.connect() as c:
            status = c.execute("SELECT status FROM agenda WHERE id=?", (it.get("id"),)).fetchone()
        R.eq("research: the item is open", status and status[0], "open")
        R.ok("research: one to three messages from the desk, then the card",
             1 <= len(words) <= 3 and up and up[-1].get("reply_markup"), [str(p.get("text"))[:60] for p in up])
        R.eq("research: the desk's words use no report labels", labels(words), [])
        R.eq("research: the desk's words use no finance jargon", jargon(words), [])
        R.ok("research: the desk does not paste the report", not any(len(w) > 1200 for w in words),
             [len(w) for w in words])


CASES = {"desk_turn": case_desk_turn, "deep_dive": case_deep_dive, "research": case_research}


def main(argv) -> int:
    R = Results("e2e-desk-live")
    names = [a for a in argv[1:] if a in CASES] or list(CASES)
    if os.environ.get("QUIVER_LIVE_E2E") != "1":
        R.skip(len(names))
        return R.done()
    for n in names:
        try:
            CASES[n](R)
        except Exception as e:  # noqa: BLE001 — a crash is a failed case, never a skip
            import traceback
            traceback.print_exc()
            R.ok(f"{n}: ran without an exception", False, f"{type(e).__name__}: {e}")
    return R.done()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
