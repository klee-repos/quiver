"""The whole desk flow on one day, with no LLM and no broker (plan §18.6).

One temp root on 2026-08-05 ET. The real producers make every row: ``desk-seed``,
``snapshot-commit``, the monitor, the worker, the tap, the broker runner, the guard and the
bridge. Two scripts stand in for the models:

  * the analyst script calls ``lib.desk.tools.call`` in a model's order (the job runner seam
    of ``run_worker.Worker``);
  * the broker script plays EXECUTE.md and SNAPSHOT.md (the process runner seam of
    ``run_broker.Broker``). It writes the ``state/tmp`` files from the captured
    Robinhood answers with values changed, runs the real ``tick.py`` CLI, and calls
    ``broker_guard.record_review`` and ``broker_guard.evaluate`` in process for the review
    hook and the place hook.

A ``tick.py`` subprocess does not see the pinned clock of this process. The broker script
puts a ``sitecustomize.py`` on the child's ``PYTHONPATH``. It reads ``QUIVER_E2E_NOW`` and
pins ``lib.market`` to the same instant, so the CLI runs on the same day and minute as
the rest of the flow. Each runbook file gets the pinned time as its modification time.

Run: ``.venv/bin/python tests/test_e2e_desk.py``. It prints ``N passed, M failed``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import (REPO, FakeClock, FakePrices, Results, SecFixtures, fx, rh_snap_file,  # noqa: E402
                        seams, seed_desk, snapshot, temp_root)

sys.path.insert(0, str(REPO / "deploy" / "runner"))

VENV_PY = str(REPO / ".venv" / "bin" / "python")
OWNER = "7100000001"
ACC = "0001596532-26-000175"
CLI_VERSION = "2.1.312 (Claude Code)"
PREV_CLOSE = {"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0}
S1_POS = {"ANET": 2, "CEG": 1, "SGOV": 11}
S2_POS = {"ANET": 2.642670, "CEG": 1, "SGOV": 11}
# tests/test_e2e_fills.py FILLED (a real get_equity_orders order), values set below.
FILLED = {"id": "6a79ec24", "symbol": "BOT", "state": "filled",
          "average_price": "27.949900", "cumulative_quantity": "0.962436",
          "quantity": "0.962436", "fees": "0.000000"}
BROKER_ORDER_ID = "6ada3a01-1c2d-4e5f-8a9b-0c1d2e3f4a5b"

STATES = {
    "proposals": {"pending", "approved", "rejected", "expired", "superseded", "withdrawn", "executing",
                  "executed", "partially_executed", "failed", "requoted", "applied"},
    "orders": {"proposed", "approved", "checked", "placing", "placed", "filled", "partially_filled",
               "cancelled", "broker_rejected", "expired", "requoted", "skipped", "outcome_unknown"},
    "events": {"new", "queued", "analyzed", "proposed", "noted", "failed"},
    "jobs": {"queued", "running", "done", "failed"},
    "broker_jobs": {"queued", "running", "done", "failed"},
}

SITECUSTOMIZE = '''"""Test shim (tests/test_e2e_desk.py): pin lib.market to QUIVER_E2E_NOW in a tick.py child."""
import os
import sys
_now = os.environ.get("QUIVER_E2E_NOW")
if _now:
    _repo = os.environ.get("QUIVER_E2E_REPO", "")
    if _repo and _repo not in sys.path:
        sys.path.append(_repo)
    from datetime import datetime as _dt
    from lib import market as _market
    _market.set_clock(_dt.fromisoformat(_now.replace("Z", "+00:00")))
'''


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- fixture edits (rule 4: drop entries, change values) -----------------------------

def drop_sub_000175(d):
    """``sub_anet`` without the -000175 row of ``filings.recent`` (every column)."""
    rec = d["filings"]["recent"]
    i = rec["accessionNumber"].index(ACC)
    for k in rec:
        del rec[k][i]
    return d


def drop_cf_000175(d):
    """``cf_anet`` without the facts of accession -000175."""
    for facts in d["facts"].values():
        for f in facts.values():
            for unit in f["units"]:
                f["units"][unit] = [v for v in f["units"][unit] if v.get("accn") != ACC]
    return d


def broker_file(*, positions, quotes, buying_power, equity, at):
    """A runbook file from ``rh_snap`` with the step's values. Each quote gets the
    previous close of the story (the S1 prices)."""
    data = rh_snap_file(positions=positions, quotes=quotes, buying_power=buying_power, equity=equity, at=at)
    for res in data["get_equity_quotes"]["data"]["results"]:
        q = res["quote"]
        prev = PREV_CLOSE.get(q["symbol"])
        if prev is not None:
            q["adjusted_previous_close"] = f"{prev:.6f}"
            q["previous_close"] = f"{prev:.6f}"
            q["previous_close_date"] = "2026-08-04"
            res["close"]["price"] = f"{prev:.2f}"
            res["close"]["date"] = "2026-08-04"
    return data


def review_answer(fields: dict, price: float, at: str) -> dict:
    """``rh_review_buy`` with the leg's values: the review call's tool_input and answer."""
    cap = copy.deepcopy(fx("rh_review_buy"))
    ti = {k: fields[k] for k in cap["tool_input"] if k in fields}
    resp = cap["tool_response"]
    d = resp["data"]
    d["symbol"], d["side"], d["type"] = fields["symbol"], fields["side"], fields["type"]
    d["dollar_amount"] = fields["dollar_amount"]
    q = d["quote_data"]
    q["symbol"] = fields["symbol"]
    for k in ("last_trade_price", "bid_price", "ask_price", "last_non_reg_trade_price"):
        q[k] = f"{price:.6f}"
    for k in ("venue_last_trade_time", "venue_bid_time", "venue_ask_time", "venue_last_non_reg_trade_time"):
        q[k] = at.replace("Z", ".000000000Z")
    q["adjusted_previous_close"] = q["previous_close"] = f"{PREV_CLOSE[fields['symbol']]:.6f}"
    q["previous_close_date"] = "2026-08-04"
    return {"tool_name": "mcp__robinhood-trading__review_equity_order", "tool_input": ti, "tool_response": resp}


def place_answer(fields: dict, price: float, at: str) -> dict:
    """The place result: an order object shaped like an ``rh_orders_live`` order, values set
    to the leg, state ``queued``. Its ``ref_id`` is Robinhood's own value, never the leg's:
    the ``rh_place`` capture holds 01a16428-ff4c-743b-ada7-1f05229ba861 for client 81e748c5."""
    o = copy.deepcopy(fx("rh_orders_live")["get_equity_orders"]["data"]["orders"][0])
    rh_ref = str(uuid.uuid5(uuid.NAMESPACE_URL, f"robinhood-order:{fields['ref_id']}"))
    assert rh_ref != fields["ref_id"]
    o.update(id=BROKER_ORDER_ID, ref_id=rh_ref, symbol=fields["symbol"], side=fields["side"],
             type="market", state="queued", quantity="0.000000", cumulative_quantity="0.000000",
             price=f"{price:.6f}", average_price=None, created_at=at.replace("Z", ".000000Z"),
             last_transaction_at=at.replace("Z", ".000Z"), executions=[])
    o["dollar_based_amount"]["amount"] = f"{float(fields['dollar_amount']):.6f}"
    return {"data": o}


# --- fakes -------------------------------------------------------------------------

class FakeTransport:
    """The Telegram seam. Every method answers ``{"ok": true, "result": {"message_id": n}}``
    (the ``tg`` capture is pending). Records every call."""

    def __init__(self):
        self.calls = []
        self.n = 900

    def __call__(self, method, params):
        self.calls.append((method, json.loads(json.dumps(params, default=str))))
        self.n += 1
        return {"ok": True, "result": {"message_id": self.n}}

    def sends(self, since=0):
        return [p for m, p in self.calls[since:] if m == "sendMessage"]


def job_values(argv) -> dict:
    """The "This job" JSON block of the broker prompt file the runner passes."""
    pf = argv[argv.index("--append-system-prompt-file") + 1]
    text = Path(pf).read_text(encoding="utf-8")
    m = re.search(r"## This job\s+```json\n(.*?)\n```", text, flags=re.S)
    return json.loads(m.group(1))


class BrokerScript:
    """The process runner of ``run_broker.Broker``: it plays the broker model. ``prices``
    and ``book`` are the broker's answers for the current step."""

    def __init__(self, root, clk, guard):
        self.root, self.clk, self.guard = root, clk, guard
        self.shim = root.path / "e2e_shim"
        self.shim.mkdir(exist_ok=True)
        (self.shim / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        self.prices = dict(PREV_CLOSE)
        self.positions = dict(S1_POS)
        self.buying_power = 300.0
        self.after = None          # (positions, buying_power) of the after-snapshot
        self.log = []              # (cmd, json) of each tick.py run
        self.guard_log = []        # (what, result)
        self.argv = []
        self.envs = []
        self.inspect = {}          # name -> callable(state) run at a runbook point

    # helpers
    def env(self, env):
        e = dict(env)
        e.update(self.root.env)
        e.update(PYTHONPATH=str(self.shim), QUIVER_E2E_NOW=iso(self.clk.now), QUIVER_E2E_REPO=str(REPO))
        return e

    def step(self, seconds=3):
        self.clk.advance(seconds)

    def write(self, name, obj) -> str:
        p = self.root.path / "state" / "tmp" / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        ts = self.clk.now.timestamp()
        os.utime(p, (ts, ts))
        return f"state/tmp/{name}"

    def tick(self, env, *args):
        p = subprocess.run([VENV_PY, "tick.py", *args], cwd=str(REPO), env=self.env(env),
                           capture_output=True, text=True, timeout=120)
        out = (p.stdout or "").strip().splitlines()
        try:
            res = json.loads(out[-1]) if out else {"error": (p.stderr or "")[-500:]}
        except ValueError:
            res = {"error": (p.stdout or "")[-500:] + (p.stderr or "")[-500:]}
        res["_exit"] = p.returncode
        self.log.append((args[0] + " " + " ".join(a for a in args[1:] if a.startswith("--phase") or
                                                     a in ("sells", "buys", "recover")), res))
        self.step()
        return res

    def equity(self, positions, buying_power):
        return round(sum(float(q) * self.prices[s] for s, q in positions.items()) + buying_power, 2)

    def file(self, symbols, *, positions=None, buying_power=None):
        pos = positions or self.positions
        bp = self.buying_power if buying_power is None else buying_power
        return broker_file(positions=pos, quotes={s: self.prices[s] for s in symbols}, buying_power=bp,
                           equity=self.equity(pos, bp), at=iso(self.clk.now))

    # the runner seam
    def __call__(self, argv, env, timeout, cwd):
        self.argv.append(list(argv))
        if argv[-1] == "--version":
            return 0, CLI_VERSION, ""
        self.envs.append(dict(env))
        job = job_values(argv)
        if "proposal_id" in job:
            return self.execute(job, env)
        return self.snapshot_job(job, env)

    def snapshot_job(self, job, env):
        """SNAPSHOT.md steps 2 to 4."""
        J = job["job_id"]
        data = self.file(job["tickers"], positions=(self.after or (None, None))[0],
                         buying_power=(self.after or (None, None))[1])
        if job.get("reconcile"):
            o = copy.deepcopy(fx("rh_orders_live")["get_equity_orders"])
            o["data"]["orders"] = []
            data["get_equity_orders"] = o
        path = self.write(f"snapshot_{J}.json", data)
        self.tick(env, "snapshot-commit", "--input", path)
        return 0, '{"type":"result","subtype":"success","is_error":false,"result":"Snapshot stored."}\n', ""

    def place(self, env, legs, stream, results):
        """EXECUTE.md step 3 for the returned legs: the review (PostToolUse hook in
        process), then the place call through the PreToolUse guard in process."""
        for fields in legs:
            now = iso(self.clk.now)
            price = self.prices[fields["symbol"]]
            rev = review_answer(fields, price, now)
            ok, why = self.guard.record_review(rev, env=env)
            self.guard_log.append(("review", (ok, why)))
            self.step()
            if "second_call" in self.inspect:
                # Not in the plan row: the same wrong amount while the leg is still checked,
                # so the amount rule itself is proven (after the allow the re-send rule denies first).
                bad = dict(fields, dollar_amount=self.inspect["second_call"])
                self.guard_log.append(("place_bad_amount_checked", self.guard.evaluate(
                    "mcp__robinhood-trading__place_equity_order", bad, env=env)))
                with self.root.led().connect() as c:
                    row = c.execute("SELECT state, submitted_at FROM orders WHERE ref_id=?",
                                    (fields["ref_id"],)).fetchone()
                self.guard_log.append(("leg_after_bad", (row[0], row[1])))
            allow, reason = self.guard.evaluate("mcp__robinhood-trading__place_equity_order", fields, env=env)
            self.guard_log.append(("place", (allow, reason)))
            n = len(stream) + 1
            stream.append({"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": f"toolu_{n}", "name": "mcp__robinhood-trading__place_equity_order",
                "input": fields}]}})
            if not allow:
                stream.append({"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": f"toolu_{n}", "is_error": True,
                    "content": f"broker_guard: denied: {reason}"}]}})
                continue
            if "after_allow" in self.inspect:
                self.inspect["after_allow"](fields)
            res = place_answer(fields, price, now)
            results.append(res)
            stream.append({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": f"toolu_{n}", "is_error": False,
                "content": [{"type": "text", "text": json.dumps(res)}]}]}})
            # The plan's second place call: the same leg with dollar_amount 98.21.
            if "second_call" in self.inspect:
                bad = dict(fields, dollar_amount=self.inspect["second_call"])
                self.step()
                deny = self.guard.evaluate("mcp__robinhood-trading__place_equity_order", bad, env=env)
                self.guard_log.append(("place_second", deny))
                n = len(stream) + 1
                stream.append({"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": f"toolu_{n}", "name": "mcp__robinhood-trading__place_equity_order",
                    "input": bad}]}})
                stream.append({"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": f"toolu_{n}", "is_error": not deny[0],
                    "content": f"broker_guard: denied: {deny[1]}"}]}})
            self.step()

    def execute(self, job, env):
        """EXECUTE.md steps 2 to 7."""
        N = job["proposal_id"]
        stream = []
        syms = sorted({l["symbol"] for l in job["legs"]})
        path = self.write(f"exec_{N}_sells.json", self.file(syms))
        chk = self.tick(env, "exec-check", "--proposal", str(N), "--phase", "sells", "--input", path)
        if chk.get("action") in ("requote", "refused") or chk.get("error"):
            return 0, "\n".join(json.dumps(s) for s in stream), ""
        if chk.get("action") == "place":
            results = []
            self.place(env, chk["legs"], stream, results)
            self.tick(env, "exec-commit", "--proposal", str(N), "--input",
                      self.write(f"exec_{N}_results.json", {"place_equity_order": results}))
        buys = sorted({l["symbol"] for l in job["legs"] if l["side"] == "buy"})
        if buys:
            path = self.write(f"exec_{N}_buys.json", self.file(buys))
            chk = self.tick(env, "exec-check", "--proposal", str(N), "--phase", "buys", "--input", path)
            if "after_buys_check" in self.inspect:
                self.inspect["after_buys_check"](chk)
            if chk.get("action") == "place":
                results = []
                self.place(env, chk["legs"], stream, results)
                self.tick(env, "exec-commit", "--proposal", str(N), "--input",
                          self.write(f"exec_{N}_results.json", {"place_equity_order": results}))
                if "after_commit" in self.inspect:
                    self.inspect["after_commit"]()
        # Step 6: fills.
        orders = copy.deepcopy(fx("rh_orders_live")["get_equity_orders"])
        f = dict(FILLED, id=BROKER_ORDER_ID, symbol="ANET", average_price="152.800000",
                 cumulative_quantity="0.642670", quantity="0.642670")
        orders["data"]["orders"] = [f]
        self.tick(env, "fills", "--input", self.write(f"exec_{N}_fills.json", {"get_equity_orders": orders}))
        if "after_fills" in self.inspect:
            self.inspect["after_fills"]()
        # Step 7: the after-snapshot.
        pos, bp = self.after
        data = self.file(job["book_tickers"], positions=pos, buying_power=bp)
        self.tick(env, "snapshot-commit", "--input", self.write(f"exec_{N}_after.json", data))
        stream.append({"type": "result", "subtype": "success", "is_error": False, "result": "Done."})
        return 0, "\n".join(json.dumps(s) for s in stream), ""


class FakeClient:
    """The SDK client of the bridge (no CLI): records the prompt, answers nothing."""

    def __init__(self):
        self.prompts = []

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        if False:  # pragma: no cover - an empty async generator
            yield None

    async def interrupt(self):
        return None

    async def disconnect(self):
        return None


# --- the analyst script ------------------------------------------------------------

def tool_json(res):
    """The data of one tool result, or the error text."""
    text = res["content"][0]["text"]
    if res.get("is_error"):
        return {"_error": text}
    return json.loads(text)


def analyst_script(state):
    """The ``job_runner`` of ``run_worker.Worker``. ``state`` collects what the test checks."""
    from lib.desk import store, tools

    def runner(kind, prompt, js):
        ctx = state["ctx_factory"](js)
        calls = state.setdefault("calls", [])

        def call(name, args):
            res = tools.call(ctx, name, args)
            calls.append((kind, name, args, res))
            return tool_json(res)

        if kind != "analyze_event":
            state.setdefault("other_jobs", []).append(kind)
            return
        state["prompt"] = prompt
        eid = js.event_id
        call("get_company", {"ticker": "ANET"})
        secs = call("get_filing_sections", {"accession": ACC})
        state["sections"] = secs
        doc_id = state["doc_with_quote"]
        doc = call("get_document", {"document_id": doc_id})
        state["doc"] = doc
        # 1. an exact quote, copied from the get_document text.
        exact = state["exact_quote"]
        f1 = call("record_finding", {"ticker": "ANET", "kind": "growth", "claim": state["exact_claim"],
                                     "quote": exact, "document_id": doc_id})
        # 2. the "Centers of Data" sentence typed with straight quotes.
        f2 = call("record_finding", {"ticker": "ANET", "kind": "segment",
                                     "claim": "Arista frames its strategy around data centers of every size.",
                                     "quote": state["straight_quote"], "document_id": doc_id})
        # 3. an invented quote.
        f3 = call("record_finding", {"ticker": "ANET", "kind": "guidance",
                                     "claim": "Revenue will double next year.",
                                     "quote": "We expect revenue to double in fiscal 2027 on AI demand alone.",
                                     "document_id": doc_id})
        state["findings"] = (f1, f2, f3)
        state["metrics"] = call("get_metrics", {"ticker": "ANET"})
        fids = [x["finding_id"] for x in (f1, f2) if "finding_id" in x]
        sc = {"growth_1_5": 0.15, "growth_6_10": 0.08, "finding_ids": fids}
        state["valuation"] = call("run_valuation", {"ticker": "ANET", "method": "dcf_fcf", "scenarios": {
            "bear": dict(sc, growth_1_5=0.08), "base": sc, "bull": dict(sc, growth_1_5=0.22)}})
        state["verdict"] = call("record_verdict", {"event_id": eid, "verdict": "strengthened",
                                                   "summary": "The Q2 10-Q adds an AI risk factor and shows FCF "
                                                              "growth; the thesis is stronger."})
        with ctx.conn() as c:
            state["event_before_propose"] = store.get_event(c, eid)["status"]
        args = {"tickers": ["ANET"], "origin": {"event_id": eid}, "fraction": 1.0,
                "rationale": "The Q2 10-Q strengthens the AI networking thesis.",
                "evidence": [f"finding:{i}" for i in fids]}
        state["propose1"] = call("propose_trade", args)
        with ctx.conn() as c:
            state["event_after_propose"] = store.get_event(c, eid)["status"]
        state["propose2"] = call("propose_trade", args)
        state["report"] = call("report", {"text": "I read the ANET Q2 10-Q. Free cash flow grew, and a new risk "
                                                  "factor names AI demand, so I proposed a buy.\n\nAt $150 the market "
                                                  "prices ANET for <12% growth a year, below what the filing "
                                                  "supports."})

    return runner


# --- the flow ----------------------------------------------------------------------

def main() -> int:
    r = Results("e2e-desk")
    clk = FakeClock("2026-08-05T13:35:00Z").pin_market()
    try:
        with temp_root() as root:
            flow(r, root, clk)
    finally:
        clk.unpin()
    return r.done()


def rows(led, sql, args=()):
    with led.connect() as c:
        return [dict(x) for x in c.execute(sql, args).fetchall()]


def one(led, sql, args=()):
    with led.connect() as c:
        x = c.execute(sql, args).fetchone()
        return x[0] if x else None


def flow(r: Results, root, clk: FakeClock) -> None:
    os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = OWNER
    import broker_guard
    import run_broker
    import run_monitor
    import run_worker
    import runner_common
    import chat_bridge
    from lib import runlock
    from lib.desk import agent, outbox, store, tools
    for mod in (run_broker, run_monitor, runner_common, broker_guard):
        mod.ROOT = root.path
    runner_common.TICK_LOG = str(root.path / "logs" / "tick.log")

    led = root.led()
    cfg = root.cfg()
    tp = FakeTransport()
    prices = FakePrices({"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0})
    box = lambda: outbox.Outbox(led, transport=tp, chat_id=OWNER, clock=lambda: 0.0, sleep=lambda s: None)  # noqa: E731
    pages = []
    trig = root.path / "state" / "triggers"

    # ------------------------------------------------------------------ step 1 (09:35)
    clk.set("2026-08-05T13:35:00Z")
    op1 = SecFixtures(drop={"sub_anet": drop_sub_000175, "cf_anet": drop_cf_000175})
    seed = seed_desk(root, opener=op1, prices=prices)
    snap1 = snapshot(root, at="2026-08-05T13:35:00Z")
    r.eq("1 seed ok", seed.get("ok"), True)
    hold = rows(led, "SELECT ticker FROM companies WHERE status='holding' ORDER BY ticker")
    r.eq("1 three holdings", [h["ticker"] for h in hold], ["ANET", "CEG", "SGOV"])
    r.eq("1 four overrides", one(led, "SELECT COUNT(*) FROM sec_overrides"), 4)
    r.eq("1 premise v1", [x["version"] for x in rows(led, "SELECT version FROM premise")], [1])
    tiers = {x["tier"]: x["n"] for x in rows(led, "SELECT tier, COUNT(*) AS n FROM filings GROUP BY tier")}
    r.eq("1 every filing is baseline", list(tiers), ["baseline"])
    r.eq("1 -000175 not loaded", one(led, "SELECT COUNT(*) FROM filings WHERE accession=?", (ACC,)), 0)
    r.eq("1 zero events", one(led, "SELECT COUNT(*) FROM events"), 0)
    r.eq("1 baseline $2,000.00", one(led, "SELECT baseline_equity FROM day_baseline WHERE trade_date='2026-08-05'"),
         2000.0)
    r.ok("1 snapshot stored", snap1.get("snapshot_id"), snap1)

    # The broker runner: its canary already passed today with this CLI version.
    with led.connect() as c:
        store.set_state(c, "canary_version", CLI_VERSION)
        store.set_state(c, "canary_day", "2026-08-05")
    script = BrokerScript(root, clk, broker_guard)
    broker = run_broker.Broker(led=led, cfg=cfg, runner=script, box=box())

    def broker_drain():
        """``run_broker.main`` without the process: trigger, lock, close, recover, drain."""
        try:
            (trig / "broker").unlink()
        except FileNotFoundError:
            pass
        with runlock.run_lock(led, "broker", runlock.holder_id(), store.now_iso(), run_broker.LOCK_TTL_S):
            broker.close_stopped()
            broker.recover_lost()
            return broker.drain()

    def msleep(seconds):
        """The monitor's wait for its snapshot job: systemd starts the broker unit."""
        if (trig / "broker").exists():
            broker_drain()

    def tick_fn(args, timeout=None):
        if args and args[0] == "benchmark-fetch":
            return {"ok": True, "observations": 0}
        env = {k: v for k, v in os.environ.items()}
        env.update(root.env, PYTHONPATH=str(script.shim), QUIVER_E2E_NOW=iso(clk.now), QUIVER_E2E_REPO=str(REPO))
        return runner_common._tick_json(args, timeout=timeout, env=env)

    def monitor(full=True):
        sx = SecFixtures()
        return run_monitor.Monitor(
            led=led, cfg=cfg, sleep=msleep, seams=seams(opener=sx, prices=prices), box=box(),
            systemctl=lambda a: (None, ""), tick=tick_fn, page=lambda **kw: pages.append(kw),
            news_fetch=lambda t, limit=None: [], yf_price_fn=prices), sx

    # ------------------------------------------------------------------ step 2 (10:40)
    clk.set("2026-08-05T14:40:00Z")
    msgs_before = len(tp.calls)
    jobs_before = one(led, "SELECT COUNT(*) FROM jobs")
    m2, sx2 = monitor()
    st2 = m2.run()
    r.eq("2 monitor status", st2.get("event"), "monitor_ok")
    new = rows(led, "SELECT * FROM filings WHERE tier != 'baseline'")
    r.eq("2 one new filing", [(f["accession"], f["tier"]) for f in new], [(ACC, "redecide")])
    r.eq("2 status counts the filing", st2.get("filings"), 1)
    r.ok("2 the 10-Q was fetched (10q_q2)", any(u.endswith("anet-20260630.htm") for u in sx2.calls), sx2.calls[-5:])
    sec = rows(led, "SELECT id, section_key, char_len FROM documents WHERE accession=? ORDER BY id", (ACC,))
    r.ok("2 10-Q:II-1A-added stored", any(s["section_key"] == "10-Q:II-1A-added" for s in sec),
         [s["section_key"] for s in sec])
    met = rows(led, "SELECT price, metrics_json, source_accession FROM metrics WHERE ticker='ANET' "
                    "ORDER BY as_of DESC LIMIT 1")[0]
    mj = json.loads(met["metrics_json"])
    r.eq("2 fcf_ttm 5,155,300,000", mj.get("fcf_ttm"), 5155300000.0)
    r.eq("2 implied growth 0.119774", round(mj.get("reverse_dcf_implied_growth") or 0, 6), 0.119774)
    r.eq("2 metrics at $150.00", met["price"], 150.0)
    r.eq("2 metrics from -000175", met["source_accession"], ACC)
    evs = rows(led, "SELECT * FROM events")
    r.eq("2 event filing:-000175 queued", [(e["dedup_key"], e["status"]) for e in evs], [(f"filing:{ACC}", "queued")])
    jobs2 = rows(led, "SELECT * FROM jobs WHERE kind='analyze_event'")
    r.eq("2 the monitor added one job", one(led, "SELECT COUNT(*) FROM jobs") - jobs_before, 1)
    r.eq("2 one analyze_event job", [(j["kind"], json.loads(j["payload_json"])) for j in jobs2],
         [("analyze_event", {"event_id": evs[0]["id"] if evs else None})])
    r.ok("2 worker trigger touched", (trig / "worker").exists())
    r.eq("2 the monitor's snapshot job ran", [(j["kind"], j["status"]) for j in
                                              rows(led, "SELECT kind, status FROM broker_jobs")], [("snapshot", "done")])
    mon_lines = rows(led, "SELECT * FROM chat_messages WHERE source='monitor'")
    r.ok("2 the monitor tells the desk the analysis is queued (§4.3)", len(mon_lines) >= 1,
         [p for p in tp.sends(msgs_before)])

    # ------------------------------------------------------------------ step 3
    clk.set("2026-08-05T14:41:00Z")
    nf = one(led, "SELECT COUNT(*) FROM filings")
    ne = one(led, "SELECT COUNT(*) FROM events")
    m3, _ = monitor()
    st3 = m3.run()
    r.eq("3 zero new filings", (st3.get("filings"), one(led, "SELECT COUNT(*) FROM filings") - nf), (0, 0))
    r.eq("3 zero new events", (st3.get("events"), one(led, "SELECT COUNT(*) FROM events") - ne), (0, 0))

    # ------------------------------------------------------------------ step 4 (10:45)
    clk.set("2026-08-05T14:45:00Z")
    rt = agent.Runtime.from_env(env={**root.env, "CLAUDE_CONFIG_DIR": str(root.path / "claude-desk")})
    sec_obj = agent.Secrets(telegram_allowed_chat_ids=OWNER, sec_user_agent="Quiver test@example.com")
    wbox = box()
    wseams = seams(opener=SecFixtures(), prices=prices)
    astate = {}

    def ctx_factory(js):
        return tools.ToolContext(role="analyst", runtime=rt, secrets=sec_obj, led=led, cfg=cfg, job=js, box=wbox,
                                 transport=tp, seams=wseams, render=lambda t: t, yf_price_fn=prices,
                                 touch=lambda name: store.touch_trigger(root.path, name))
    astate["ctx_factory"] = ctx_factory
    # The quote the "model" copies: find the "Centers of Data" sentence in the 10-Q sections.
    doc_id, sentence = None, None
    for s in sec:
        with led.connect() as c:
            text = store.get_document(c, s["id"])["text"]
        m = re.search(r"[^.]*Centers of Data[^.]*\.", text)
        if m:
            doc_id, sentence = s["id"], m.group(0).strip()
            break
    r.ok("4 the 10-Q holds the Centers of Data sentence", doc_id is not None)
    astate["doc_with_quote"] = doc_id
    straight = sentence.replace("“", '"').replace("”", '"').replace("’", "'") if sentence else ""
    r.ok("4 the sentence has curly quotes in the document", sentence is not None and sentence != straight, sentence)
    astate["straight_quote"] = "  " + straight.replace(" ", "  ", 2) + " "
    with led.connect() as c:
        dtext = store.get_document(c, doc_id)["text"] if doc_id else ""
    m = re.search(r"[A-Z][^.“”]{60,240}\.", dtext[:20000])
    astate["exact_quote"] = m.group(0) if m else ""
    astate["exact_claim"] = "A fact the 10-Q states."
    worker = run_worker.Worker(runtime=rt, secrets=sec_obj, led=led, box=wbox, job_runner=analyst_script(astate),
                               classify_fn=lambda items: "[]", ctx_factory=ctx_factory)
    try:
        (trig / "worker").unlink()
    except FileNotFoundError:
        pass
    with runlock.run_lock(led, "worker", runlock.holder_id(), store.now_iso(), run_worker.LOCK_TTL_S):
        worker.requeue_running()
        drained = worker.drain()
    r.ok("4 the worker drained its jobs", drained >= 1, drained)
    names = [c[1] for c in astate.get("calls", [])]
    r.eq("4 tool order", names, ["get_company", "get_filing_sections", "get_document", "record_finding",
                                 "record_finding", "record_finding", "get_metrics", "run_valuation",
                                 "record_verdict", "propose_trade", "propose_trade", "report"])
    f1, f2, f3 = astate.get("findings", ({}, {}, {}))
    r.ok("4 exact quote stored", "finding_id" in f1, f1)
    r.ok("4 straight-quote sentence stored", "finding_id" in f2, f2)
    r.ok("4 invented quote refused", "quote not found" in f3.get("_error", ""), f3)
    r.eq("4 two findings", one(led, "SELECT COUNT(*) FROM findings"), 2)
    r.ok("4 valuation ran", "_error" not in astate.get("valuation", {}), astate.get("valuation"))
    r.eq("4 the event is analyzed at the first propose_trade", astate.get("event_before_propose"), "analyzed")
    p1 = astate.get("propose1", {})
    r.eq("4 proposal #1", p1.get("proposal_id"), 1)
    r.eq("4 fraction 0.5 (filing + strengthened; the 1.0 passed is ignored)", p1.get("fraction"), 0.5)
    r.eq("4 one buy leg $100.00 at $150.00", [(l["ticker"], l["side"], l["dollars"], l["quote"]) for l in p1.get("legs", [])],
         [("ANET", "buy", 100.0, 150.0)])
    legs1 = led.legs(1)
    r.eq("4 one proposed leg", [(l["state"], l["dollar_amount"], l["quote_at_proposal"]) for l in legs1],
         [("proposed", 100.0, 150.0)])
    r.eq("4 #1 pending", one(led, "SELECT state FROM proposals WHERE id=1"), "pending")
    r.ok("4 the second proposal is refused", "already backs proposal #1" in astate.get("propose2", {}).get("_error", ""),
         astate.get("propose2"))
    r.eq("4 one trade proposal", one(led, "SELECT COUNT(*) FROM proposals"), 1)
    r.eq("4 event proposed at the job end", one(led, "SELECT status FROM events WHERE id=1"), "proposed")
    r.eq("4 the analyze_event job is done", one(led, "SELECT status FROM jobs WHERE kind='analyze_event'"), "done")
    # The worker sends nothing: the report and card #1 wait on the agenda as one item.
    items4 = rows(led, "SELECT * FROM agenda WHERE status != 'done'")
    r.eq("4 one agenda item: the report with card #1, priority now, waiting",
         [(i["kind"], i["priority"], i["status"], json.loads(i["proposal_ids"]), i["event_id"]) for i in items4],
         [("report", 0, "waiting", [1], 1)])
    r.eq("4 the worker sent no message and no card",
         (one(led, "SELECT COUNT(*) FROM chat_messages WHERE source IN ('analyst','proposal')"),
          [p for p in tp.sends() if p.get("reply_markup")]), (0, []))

    # The bridge (no CLI): the agenda turn, the tap path and, at step 13, the turn input.
    fclient = FakeClient()

    async def factory(options):
        return fclient

    async def qpick(text):
        return "\U0001F44D"
    bridge = chat_bridge.Bridge(runtime=rt, secrets=sec_obj, led=led, transport=tp, client_factory=factory,
                                quick_pick=qpick)

    def present():
        """One agenda turn (the owner has been quiet): the bridge opens the next item."""
        bridge.last_owner_at = bridge.last_tap_at = bridge.last_owner_turn_end = None

        async def go():
            await bridge.maybe_turn()
            if bridge.turn_task is not None:
                await bridge.turn_task
            for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                t.cancel()
        asyncio.run(go())

    # ------------------------------------------------------------------ step 5: the card
    before5 = len(tp.calls)
    present()
    r.ok("5 the agenda turn brings up item 1", fclient.prompts and '<bring_up item="1"' in fclient.prompts[-1],
         fclient.prompts[-1:])
    r.eq("5 item 1 is open", one(led, "SELECT status FROM agenda WHERE id=1"), "open")
    sent5 = tp.sends(before5)
    # The fake model said nothing, so the bridge sent the report as written, then the card.
    r.ok("5 the report as written, then the card", len(sent5) == 2 and "I read the ANET Q2 10-Q." in sent5[0]["text"]
         and not sent5[0].get("reply_markup") and sent5[1].get("reply_markup"), [p.get("text", "")[:60] for p in sent5])
    prop1 = rows(led, "SELECT * FROM proposals WHERE id=1")[0]
    card = [p for p in tp.sends() if p.get("reply_markup")]
    r.eq("5 one card with buttons", len(card), 1)
    c1 = card[0] if card else {"text": "", "reply_markup": {"inline_keyboard": [[{}]]}}
    r.ok("5 card text", "Buy $100 of ANET, about 0.7 shares at $150.00." in c1["text"], c1["text"])
    btn = c1["reply_markup"]["inline_keyboard"][0][0]
    r.eq("5 callback_data", btn.get("callback_data"), f"p:1:{prop1['nonce']}:a")
    r.ok("5 nonce 10 hex", re.fullmatch(r"[0-9a-f]{10}", prop1["nonce"] or ""), prop1["nonce"])
    card_ix = next((i for i, (m, p) in enumerate(tp.calls) if m == "sendMessage" and p.get("reply_markup")), None)
    r.eq("5 tg_message_id stored (the card's message id)", (prop1["tg_message_id"], str(prop1["tg_chat_id"])),
         (901 + card_ix if card_ix is not None else None, OWNER))

    def tap(prop, action="a", qid="q1"):
        cbq = {"id": qid, "from": {"id": int(OWNER), "is_bot": False, "first_name": "Kevin"},
               "message": {"message_id": prop["tg_message_id"], "chat": {"id": int(OWNER), "type": "private"}},
               "chat_instance": "1", "data": f"p:{prop['id']}:{prop['nonce']}:{action}"}
        before = len(tp.calls)

        async def go():
            res = await bridge.handle_tap(cbq, offline=False)
            for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                t.cancel()
            return res
        return asyncio.run(go()), tp.calls[before:]

    # ------------------------------------------------------------------ step 6 (10:50)
    clk.set("2026-08-05T14:50:00Z")
    res6, calls6 = tap(prop1)
    r.eq("6 approved", (res6.get("answer"), res6.get("ok")), ("Approved", True))
    r.eq("6 #1 approved", one(led, "SELECT state FROM proposals WHERE id=1"), "approved")
    r.eq("6 one execute job", [(j["kind"], j["proposal_id"], j["status"]) for j in
                               rows(led, "SELECT * FROM broker_jobs WHERE kind='execute'")], [("execute", 1, "queued")])
    r.ok("6 broker trigger touched", (trig / "broker").exists())
    sent6 = [p["text"] for m, p in calls6 if m == "sendMessage"]
    r.eq("6 the tap message", sent6, ["On it. Checking the price now."])
    r.ok("6 the callback answer", any(m == "answerCallbackQuery" and p.get("text") == "Approved" for m, p in calls6),
         calls6)
    r.eq("6 the tap on its last card closes item 1", one(led, "SELECT status FROM agenda WHERE id=1"), "done")

    # ------------------------------------------------------------------ step 7: requote
    clk.set("2026-08-05T14:50:20Z")
    script.prices = {"ANET": 152.40, "CEG": 300.0, "SGOV": 100.0}
    before7 = len(tp.calls)
    broker_drain()
    r.eq("7 exec-check sells: requote", [x[1].get("action") for x in script.log if x[0].startswith("exec-check")],
         ["requote"])
    r.eq("7 #1 requoted", one(led, "SELECT state FROM proposals WHERE id=1"), "requoted")
    r.eq("7 #1 superseded_by #2", one(led, "SELECT superseded_by FROM proposals WHERE id=1"), 2)
    prop2 = (rows(led, "SELECT * FROM proposals WHERE id=2") or [{}])[0]
    r.eq("7 #2 pending", prop2.get("state"), "pending")
    legs2 = led.legs(2)
    r.eq("7 #2 buy $98.20 at $152.40", [(l["side"], l["dollar_amount"], l["quote_at_proposal"], l["state"])
                                        for l in legs2], [("buy", 98.2, 152.4, "proposed")])
    pl2 = json.loads(prop2.get("payload_json") or "{}")
    snapx = rows(led, "SELECT * FROM broker_snapshots WHERE id=?", (pl2.get("snapshot_id"),))
    dep = 2 * 152.40 + 1 * 300.0 + 11 * 100.0 + 300.0
    r.eq("7 deployable $2,004.80", (pl2.get("deployable"), round(dep, 2)), (2004.8, 2004.8))
    r.eq("7 gap $196.40", round(0.25 * dep - 2 * 152.40, 2), 196.4)
    r.eq("7 fraction 0.5", pl2.get("fraction"), 0.5)
    r.ok("7 sized from the exec-check snapshot", snapx and snapx[0]["equity"] == 2004.8, snapx[:1])
    r.eq("7 #2 copies origin and event", (prop2.get("origin"), prop2.get("event_id"), prop2.get("rationale"),
                                          prop2.get("evidence_json")),
         (prop1["origin"], prop1["event_id"], prop1["rationale"], prop1["evidence_json"]))
    sent7 = [p["text"] for p in tp.sends(before7)]
    r.ok("7 the requote message", "ANET moved 1.6% since I proposed this ($150.00 to $152.40). Here are the new "
                                  "numbers." in sent7, sent7)
    r.ok("7 the new card", any("Buy $98.20 of ANET" in t for t in sent7), sent7)
    r.eq("7 #1 legs requoted", [l["state"] for l in led.legs(1)], ["requoted"])
    snap_env, exec_env = script.envs[0], script.envs[-1]
    r.eq("7 the execute child env names job and proposal", (exec_env.get("QUIVER_BROKER_PROPOSAL"),
                                                             bool(exec_env.get("QUIVER_BROKER_JOB"))), ("1", True))
    r.ok("7 the snapshot child env names no proposal", "QUIVER_BROKER_PROPOSAL" not in snap_env, sorted(snap_env))
    r.ok("7 no Telegram key in a broker child env", not any(k.startswith("TELEGRAM") for e in script.envs for k in e),
         sorted({k for e in script.envs for k in e}))
    prop2 = rows(led, "SELECT * FROM proposals WHERE id=2")[0]

    # ------------------------------------------------------------------ step 8
    clk.set("2026-08-05T14:52:00Z")
    snap_before = rows(led, "SELECT id, state, decided_at FROM proposals ORDER BY id")
    res8, _ = tap(prop1, qid="q2")
    r.eq("8 tap #1 again: Price moved", res8.get("answer"), "Price moved")
    r.eq("8 no change", rows(led, "SELECT id, state, decided_at FROM proposals ORDER BY id"), snap_before)
    r.eq("8 still one execute job", one(led, "SELECT COUNT(*) FROM broker_jobs WHERE kind='execute'"), 1)

    # ------------------------------------------------------------------ step 9 (10:53)
    clk.set("2026-08-05T14:53:00Z")
    res9, _ = tap(prop2, qid="q3")
    r.eq("9 #2 approved", (res9.get("answer"), one(led, "SELECT state FROM proposals WHERE id=2")),
         ("Approved", "approved"))
    clk.set("2026-08-05T14:53:20Z")
    script.prices = {"ANET": 152.80, "CEG": 300.0, "SGOV": 100.0}
    script.after = (S2_POS, 201.80)
    script.log.clear()
    seen = {}

    def after_buys_check(chk):
        seen["buys"] = chk
        seen["leg_checked"] = led.legs(2)
        seen["buys_snap"] = rows(led, "SELECT * FROM broker_snapshots ORDER BY id DESC LIMIT 1")[0]

    def after_allow(fields):
        seen["leg_after_allow"] = led.legs(2)

    def after_commit():
        seen["leg_after_commit"] = led.legs(2)

    def after_fills():
        seen["leg_after_fills"] = led.legs(2)
        seen["prop_after_fills"] = one(led, "SELECT state FROM proposals WHERE id=2")
    script.inspect = {"after_buys_check": after_buys_check, "after_allow": after_allow,
                      "second_call": "98.21", "after_commit": after_commit, "after_fills": after_fills}
    before9 = len(tp.calls)
    broker_drain()
    checks = [(x[0], x[1].get("action")) for x in script.log if x[0].startswith("exec-check")]
    r.eq("9 no requote, no sells, then the buy", checks, [("exec-check --phase sells", "none"),
                                                          ("exec-check --phase buys", "place")])
    bl = (seen.get("buys") or {}).get("legs") or [{}]
    r.eq("9 buy $98.20", [(l.get("symbol"), l.get("side"), l.get("dollar_amount")) for l in bl], [("ANET", "buy", "98.20")])
    lc = (seen.get("leg_checked") or [{}])[0]
    r.eq("9 leg checked, clamped_from NULL", (lc.get("state"), lc.get("dollar_amount"), lc.get("clamped_from")),
         ("checked", 98.2, None))
    bs = seen.get("buys_snap") or {}
    pos = json.loads(bs.get("positions_json") or "{}")
    live = {"ANET": 152.80, "CEG": 300.0, "SGOV": 100.0}
    dep9 = sum(p["quantity"] * live[s] for s, p in pos.items()) + (bs.get("buying_power") or 0)
    r.eq("9 room under target $195.80", round(0.25 * dep9 - 2 * 152.80, 2), 195.8)
    r.eq("9 buys snapshot buying power $300.00", bs.get("buying_power"), 300.0)

    # ------------------------------------------------------------------ step 10: review + guard
    glog = dict((k, v) for k, v in script.guard_log)
    r.eq("10 review_ok=1", glog.get("review", (None,))[0], 1)
    r.ok("10 the exact place call is allowed", glog.get("place", (False,))[0] is True, glog.get("place"))
    la = (seen.get("leg_after_allow") or [{}])[0]
    r.eq("10 leg placing after allow", (la.get("state"), la.get("review_ok"), bool(la.get("submitted_at"))),
         ("placing", 1, True))
    r.eq("10 dollar_amount 98.21 denied", (glog.get("place_second") or (None,))[0], False)
    r.eq("10 98.21 on the checked leg: the amount rule denies, no write",
         (glog.get("place_bad_amount_checked"), glog.get("leg_after_bad")),
         ((False, "dollar_amount does not match the leg"), ("checked", None)))

    # ------------------------------------------------------------------ step 11: commit + fills
    lcm = (seen.get("leg_after_commit") or [{}])[0]
    r.eq("11 exec-commit: placed with the broker order id", (lcm.get("state"), lcm.get("broker_order_id")),
         ("placed", BROKER_ORDER_ID))
    lf = (seen.get("leg_after_fills") or [{}])[0]
    r.eq("11 fills: filled", (lf.get("state"), lf.get("fill_avg_price"), lf.get("fill_quantity")),
         ("filled", 152.8, 0.64267))
    r.eq("11 #2 executed", one(led, "SELECT state FROM proposals WHERE id=2"), "executed")
    sent9 = [p["text"] for p in tp.sends(before9)]
    if os.environ.get("E2E_DESK_DEBUG"):
        for cmd, out in script.log:
            print("DEBUG tick", cmd, json.dumps(out, default=str)[:600])
        print("DEBUG guard", script.guard_log)
    r.ok("11 the result message", "Done. Bought $98.20 of ANET: 0.643 shares at $152.80." in sent9, sent9)
    r.eq("11 one decisions row", [(d["ticker"], d["signal"], d["decision_price"]) for d in
                                  rows(led, "SELECT * FROM decisions")], [("ANET", "buy", 152.8)])

    # ------------------------------------------------------------------ step 12: S2
    s2 = rows(led, "SELECT * FROM broker_snapshots ORDER BY id DESC LIMIT 1")[0]
    r.eq("12 equity $2,005.60", s2["equity"], 2005.6)
    r.eq("12 S2 ANET 2.642670 sh, buying power $201.80",
         (json.loads(s2["positions_json"])["ANET"]["quantity"], s2["buying_power"]), (2.64267, 201.8))
    r.eq("12 no halt", one(led, "SELECT halted FROM day_baseline WHERE trade_date='2026-08-05'"), 0)
    r.eq("12 the baseline stays $2,000.00", one(led, "SELECT baseline_equity FROM day_baseline WHERE "
                                                 "trade_date='2026-08-05'"), 2000.0)
    r.eq("12 broker jobs done", [x["status"] for x in rows(led, "SELECT status FROM broker_jobs ORDER BY id")],
         ["done", "done", "done"])
    r.ok("12 the audit matched the place call", not led.is_halted("2026-08-05"))

    # ------------------------------------------------------------------ step 13: the desk turn
    clk.set("2026-08-05T15:00:00Z")
    msg = {"message_id": 501, "date": int(clk.now.timestamp()), "text": "How did the ANET buy go?",
           "chat": {"id": int(OWNER), "type": "private"}, "from": {"id": int(OWNER), "is_bot": False}}
    asyncio.run(bridge.handle_message(msg))
    owners = bridge.pending_owner()
    updates = bridge.desk_updates()
    prompt = bridge.build_input(owners, updates)
    lines = prompt.splitlines()
    r.ok("13 desk_updates opens the input", lines and lines[0].startswith('<desk_updates since="'), lines[:1])
    body = lines[1:lines.index("</desk_updates>")] if "</desk_updates>" in lines else []
    r.eq("13 one line per update, in id order", len(body), len(updates))
    r.eq("13 update ids ascend", [u["id"] for u in updates], sorted(u["id"] for u in updates))
    srcs = []
    for u in updates:
        if u["source"] not in srcs:
            srcs.append(u["source"])
    # The step-5 agenda turn saw the monitor line; what went out after it is new here.
    for s in ("analyst", "proposal", "execution"):
        r.ok(f"13 a {s} line", s in srcs, srcs)
    r.ok("13 the agenda turn took the monitor line", "monitor" not in srcs and one(
        led, "SELECT MIN(delivered_to_session) FROM chat_messages WHERE source='monitor'") == 1, srcs)
    r.ok("13 lines carry the source", all(f"· {u['source']}]" in ln for u, ln in zip(updates, body)), body[:3])
    esc = [ln for ln in body if "&lt;12%" in ln]
    r.ok("13 '<' in an update is escaped", esc and "<12%" not in prompt, [ln for ln in body if "12%" in ln])
    r.ok("13 the owner tag", lines[-1].startswith('<owner message_id="501"'), lines[-1:])
    asyncio.run(bridge.run_turn())
    r.eq("13 the turn input is build_input", fclient.prompts[-1:], [prompt])
    r.eq("13 all updates delivered after", one(led, "SELECT COUNT(*) FROM chat_messages WHERE role='desk' AND "
                                                "source != 'chat' AND delivered_to_session=0"), 0)
    r.eq("13 the owner message delivered", one(led, "SELECT delivered_to_session FROM chat_messages WHERE "
                                                    "tg_message_id=501"), 1)

    # ------------------------------------------------------------------ step 14 (16:40)
    clk.set("2026-08-05T20:40:00Z")
    before14 = len(tp.calls)
    m14, _ = monitor()
    st14 = m14.run()
    s14 = rows(led, "SELECT * FROM broker_snapshots ORDER BY id DESC LIMIT 1")[0]
    r.eq("14 the snapshot job returned S2", (s14["captured_at"], s14["equity"]), ("2026-08-05T20:40:00Z", 2005.6))
    from lib.desk import proposals as _props
    with led.connect() as c:
        base14 = _props.valuation_base(c, _props.latest_snapshot(c), clk.now, prices)
    w = {s: round(v / base14["deployable"] * 100, 2) for s, v in base14["values"].items()}
    r.eq("14 deployable $2,005.60", round(base14["deployable"], 2), 2005.6)
    r.eq("14 weights ANET 20.13, CEG 14.96, SGOV 54.85", w, {"ANET": 20.13, "CEG": 14.96, "SGOV": 54.85})
    r.ok("14 ANET inside its band (20 to 30)", 20.0 <= w.get("ANET", 0) <= 30.0, w)
    r.eq("14 the drift step ran", one(led, "SELECT COUNT(*) FROM bridge_state WHERE key='drift_done:2026-08-05'"), 1)
    r.eq("14 no drift proposal", (one(led, "SELECT COUNT(*) FROM proposals WHERE origin='drift'"),
                                  one(led, "SELECT COUNT(*) FROM events WHERE type='drift'")), (0, 0))
    r.eq("14 the monitor sends no brief: it waits on the agenda", (tp.sends(before14), one(
        led, "SELECT status FROM agenda WHERE kind='brief'")), ([], "waiting"))
    present()
    r.ok("14 the agenda turn brings up the brief", '<bring_up item="' in fclient.prompts[-1] and 'kind="brief"'
         in fclient.prompts[-1], fclient.prompts[-1][:300])
    sent14 = [p["text"] for p in tp.sends(before14)]
    r.eq("14 the brief goes out as one message", len(sent14), 1)
    brief = (sent14[0] if sent14 else "").split("\n\n")
    if os.environ.get("E2E_DESK_DEBUG"):
        print("DEBUG prompt\n" + prompt)
        print("DEBUG brief", json.dumps(brief, indent=1))
        print("DEBUG monitor14", st14)
    r.eq("14 brief of 3 paragraphs", len(brief), 3)
    b = brief + ["", "", ""]
    r.ok("14 brief 1: $2,005.60, +$5.60, +0.28%", "$2,005.60" in b[0] and "up $5.60" in b[0] and "+0.28%" in b[0], b[0])
    r.ok("14 brief 2: ANET leads the movers", b[1].startswith("What moved the account: ANET"), b[1])
    r.ok("14 brief 3: the 10-Q and its verdict", "ANET filed its quarterly report (10-Q), and our reason to own it got stronger." in b[2], b[2])
    r.eq("14 one brief notifications row", one(led, "SELECT COUNT(*) FROM notifications WHERE kind='brief'"), 1)
    r.eq("14 the brief row is source brief", one(led, "SELECT COUNT(*) FROM chat_messages WHERE source='brief'"), 1)
    r.ok("14 the monitor run is clean", not st14.get("failed"), st14)

    # ------------------------------------------------------------------ step 15 (17:40)
    clk.set("2026-08-05T21:40:00Z")
    before15 = len(tp.calls)
    m15, _ = monitor()
    m15.run()
    r.eq("15 no second brief", (tp.sends(before15), one(led, "SELECT COUNT(*) FROM notifications WHERE kind='brief'")),
         ([], 1))
    r.eq("15 #1 legs requoted", [l["state"] for l in led.legs(1)], ["requoted"])
    r.eq("15 #2 legs filled", [l["state"] for l in led.legs(2)], ["filled"])
    refs = [x["ref_id"] for x in rows(led, "SELECT ref_id FROM orders")]
    r.ok("15 unique ref_ids", len(refs) == len(set(refs)) == 2, refs)
    for table, col in (("proposals", "state"), ("orders", "state"), ("events", "status"), ("jobs", "status"),
                       ("broker_jobs", "status")):
        got = {x["v"] for x in rows(led, f"SELECT DISTINCT {col} AS v FROM {table}")}
        r.ok(f"15 every {table}.{col} is a §6.1 value", got <= STATES[table], got - STATES[table])
    r.eq("15 final states", [(x["id"], x["state"]) for x in rows(led, "SELECT id, state FROM proposals ORDER BY id")],
         [(1, "requoted"), (2, "executed")])
    r.eq("15 no page", pages, [])
    tg_methods = sorted({m for m, _ in tp.calls})
    r.ok("15 Telegram methods used", set(tg_methods) <= {"sendMessage", "editMessageReplyMarkup", "answerCallbackQuery",
                                                       "sendChatAction", "setMessageReaction"}, tg_methods)


if __name__ == "__main__":
    raise SystemExit(main())
