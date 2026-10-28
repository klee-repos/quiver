#!/usr/bin/env python3
"""Spec for the desk execution path (plan §11; the test table of §18.4 `test_desk_execution.py`).

The suite drives the real code: ``tick.py`` (in process with a pinned clock, and as a real
subprocess for the cases that do not depend on the time), ``lib/desk/execution.py``,
``deploy/runner/run_broker.py`` (``Broker`` with an injected process runner) and
``deploy/runner/broker_guard.py``.

A scripted broker model (``Bot``) plays the ``claude -p`` child. It follows EXECUTE.md and
SNAPSHOT.md step by step. It sends EVERY tool call through ``broker_guard.evaluate`` first,
as the PreToolUse hook does, and it records each call as stream-json events copied from the
real CLI capture ``canary_guard.jsonl.gz`` (values changed only). The Robinhood answers are
the captured files ``rh_snap``, ``rh_review_buy``/``rh_review_sell`` and ``rh_orders_live``
with values changed (§18.1 rule 4). ``FILLED``/``PARTIAL`` come from ``tests/test_e2e_fills.py``.

No network, no LLM, no broker. Each case has its own temp root.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import gzip
import io
import json
import os
import subprocess
import sys
import time as _time
import traceback
import types
import uuid
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO), str(REPO / "tests"), str(REPO / "deploy" / "runner")):
    if p not in sys.path:
        sys.path.insert(0, p)

from desk_fakes import (FIX, FakeClock, Results, TEST_BOOK, fx, rh_snap_file,  # noqa: E402
                        seed_desk, snapshot, temp_root)

import tick  # noqa: E402
from lib import market, notify  # noqa: E402
from lib.desk import approvals, execution, outbox, proposals, quotes, store  # noqa: E402
from lib.desk import settings as desk_settings  # noqa: E402
import broker_guard  # noqa: E402
import run_broker  # noqa: E402
import runner_common as rc  # noqa: E402

PY = str(REPO / ".venv" / "bin" / "python")
OWNER = 111
CHAT = "111"
r = Results("desk-execution")

# --- fixtures ------------------------------------------------------------------------


def _fills_consts():
    """FILLED and PARTIAL from tests/test_e2e_fills.py. That module runs its checks and
    exits at import, so this runs only its statements up to the PARTIAL assignment."""
    path = REPO / "tests" / "test_e2e_fills.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = []
    for node in tree.body:
        body.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARTIAL" for t in node.targets):
            break
    ns = {"__file__": str(path), "__name__": "e2e_fills_consts"}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), ns)  # noqa: S102
    return ns["FILLED"], ns["PARTIAL"]


FILLED, PARTIAL = _fills_consts()
ORDERS_LIVE = fx("rh_orders_live")["get_equity_orders"]
BUY_ORDER = next(o for o in ORDERS_LIVE["data"]["orders"] if o["side"] == "buy")
SELL_ORDER = next(o for o in ORDERS_LIVE["data"]["orders"] if o["side"] == "sell")
REVIEW_BUY = fx("rh_review_buy")
REVIEW_SELL = fx("rh_review_sell")


def _stream_lines(name):
    return [json.loads(x) for x in gzip.decompress((FIX / name).read_bytes()).decode().splitlines() if x.strip()]


CANARY_GUARD = _stream_lines("canary_guard.jsonl.gz")
CANARY_FALSE = _stream_lines("canary_false.jsonl.gz")
_USE_EV = next(e for e in CANARY_GUARD if e.get("type") == "assistant"
               and any(c.get("type") == "tool_use" for c in e["message"]["content"]))
_RES_EV = next(e for e in CANARY_GUARD if e.get("type") == "user")


def _dump(events) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def ev_use(tid, name, inp):
    e = copy.deepcopy(_USE_EV)
    c = copy.deepcopy(next(x for x in e["message"]["content"] if x.get("type") == "tool_use"))
    c["id"], c["name"], c["input"] = tid, name, inp
    e["message"]["content"] = [c]
    return e


def ev_result(tid, text, is_error):
    e = copy.deepcopy(_RES_EV)
    c = e["message"]["content"][0]
    c["tool_use_id"], c["content"], c["is_error"] = tid, text, bool(is_error)
    return e


BOOK_B = TEST_BOOK.replace("rh_tradable_confirmed: [ANET, CEG, SGOV]",
                           "rh_tradable_confirmed: [ANET, CEG, GEV, SGOV]").replace(
    "      - {ticker: ANET, sleeve: AI Networking, weight: 25, band: 5}\n"
    "      - {ticker: CEG, sleeve: Power, weight: 15, band: 5}\n",
    "      - {ticker: ANET, sleeve: AI Networking, weight: 20, band: 5}\n"
    "      - {ticker: CEG, sleeve: Power, weight: 15, band: 5}\n"
    "      - {ticker: GEV, sleeve: Power, weight: 5, band: 2}\n")
BOOK_C = TEST_BOOK.replace("rh_tradable_confirmed: [ANET, CEG, SGOV]",
                           "rh_tradable_confirmed: [SMH, CEG, SGOV]").replace(
    "      - {ticker: ANET, sleeve: AI Networking, weight: 25, band: 5}\n"
    "      - {ticker: CEG, sleeve: Power, weight: 15, band: 5}\n",
    "      - {ticker: SMH, sleeve: Chips, weight: 20, band: 5}\n"
    "      - {ticker: CEG, sleeve: Power, weight: 20, band: 5}\n")
assert BOOK_B != TEST_BOOK and BOOK_C != TEST_BOOK

# --- seams -----------------------------------------------------------------------------

YF_CALLS = []


def _no_yfinance(ticker):
    """The yfinance seam for every build in this suite: no network, no quote."""
    YF_CALLS.append(ticker)
    return None


quotes.yf_last_price = _no_yfinance


class FakeTG:
    """The Telegram transport of ``Outbox``: the Bot API envelope with a message id."""

    def __init__(self):
        self.calls = []
        self.n = 500

    def __call__(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "sendMessage":
            self.n += 1
            return {"ok": True, "result": {"message_id": self.n}}
        return {"ok": True, "result": True}

    def texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


_REAL_ALERT = rc._maybe_alert


class Pages:
    """Records each page through the real ``_maybe_alert`` (its dedup runs) with a fake send."""

    def __init__(self):
        self.calls = []
        self.sent = []

    def send(self, *, token, chat_ids, text, disable_notification=False):
        self.sent.append(text)
        return {"ok": True}

    def __call__(self, led, **kw):
        self.calls.append(dict(kw))
        _REAL_ALERT(led, send=self.send, secrets={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_CHAT_IDS": CHAT}, **kw)

    def kinds(self):
        return [(c["kind"], c["stage"]) for c in self.calls]


class _TimeProxy(types.ModuleType):
    """``tick.time`` with ``sleep`` on the pinned clock."""

    def __init__(self, clk):
        super().__init__("time")
        self._clk = clk

    def __getattr__(self, name):
        return getattr(_time, name)

    def sleep(self, s):
        self._clk.sleep(s)


# --- the per-case context ------------------------------------------------------------------


class Ctx:
    def __init__(self, root, clk):
        self.root = root
        self.clk = clk
        self.led = root.led()
        self.cfg = root.cfg()
        self.tg = FakeTG()
        self.pages = Pages()
        self.box = outbox.Outbox(self.led, transport=self.tg, chat_id=CHAT, clock=clk.monotonic,
                                 sleep=lambda s: None)
        self.owner_msg = 9000
        self.rh = FakeRH(self)

    # time
    def at(self, iso):
        self.clk.set(iso)

    def now(self):
        return store.iso(self.clk.now)

    # ledger reads
    def q(self, sql, args=()):
        with self.led.connect() as c:
            return [dict(x) for x in c.execute(sql, args).fetchall()]

    def prop(self, pid):
        return self.q("SELECT * FROM proposals WHERE id=?", (pid,))[0]

    def legs(self, pid):
        return self.led.legs(pid)

    def leg(self, ref):
        return self.led.get_order(ref)

    def job_of(self, pid):
        rows = self.q("SELECT * FROM broker_jobs WHERE proposal_id=? ORDER BY id DESC", (pid,))
        return rows[0] if rows else None

    def setting(self, key, value):
        with self.led.connect() as c:
            desk_settings.write(c, key, value, self.now())

    # producers
    def snap(self, **kw):
        at = kw.pop("at", None) or self.now()
        return snapshot(self.root, at=at, **kw)

    def build(self, tickers, fraction=1.0):
        self.owner_msg += 1
        with self.led.transaction() as c:
            return proposals.build_trade(self.led, c, cfg=self.cfg, tickers=list(tickers),
                                         origin={"owner_message_id": self.owner_msg}, fraction=fraction,
                                         rationale="Test trade.", evidence=[], created_by="desk", role="desk",
                                         turn_owner_ids=[self.owner_msg], now_dt=self.clk.now,
                                         yf_price_fn=_no_yfinance)

    def approve(self, built):
        outbox.send_trade_card(self.led, self.box, self.led.connect, built, now_dt=self.clk.now)
        p = self.prop(built["proposal_id"])
        res = approvals.handle_tap(self.led, self.cfg, callback_data=f"p:{p['id']}:{p['nonce']}:a",
                                   from_id=OWNER, chat_id=CHAT, message_id=p["tg_message_id"],
                                   owner_ids=[OWNER], now_dt=self.clk.now)
        assert res.get("ok"), res
        return self.job_of(p["id"])["id"]

    def broker(self, runner=None):
        return run_broker.Broker(led=self.led, cfg=self.cfg, runner=runner or Runner(self), box=self.box,
                                 now_fn=lambda: self.clk.now)

    def start(self, pid, *, recovery=False):
        """What run_broker does before the spawn: mark the job running, run exec-prepare.
        Returns (prep, env of the execute job)."""
        job = self.q("SELECT * FROM broker_jobs WHERE proposal_id=? AND status='queued' ORDER BY id", (pid,))[0]
        self.broker().mark(job["id"], "running")
        args = ["exec-prepare", "--proposal", pid] + (["--recovery"] if recovery else [])
        code, out = self.tick(*args)
        return out, {"QUIVER_BROKER_JOB": str(job["id"]), "QUIVER_BROKER_PROPOSAL": str(pid),
                     "RH_ACCOUNT_NUMBER": "TEST1234"}

    def ready(self, tickers, fraction=1.0):
        built = self.build(tickers, fraction)
        self.clk.advance(30)
        self.approve(built)
        self.clk.advance(10)
        prep, env = self.start(built["proposal_id"])
        assert prep.get("ok"), prep
        return built["proposal_id"], env

    # the runbook files
    def snapfile(self, *, positions, quotes, bp=300.0, equity=2000.0, approval=None, at=None, mutate=None,
                 orders=None, drop=()):
        data = rh_snap_file(positions=positions, quotes=quotes, buying_power=bp, equity=equity,
                            at=at or self.now(), approval=approval)
        for k in drop:
            data.pop(k, None)
        if orders is not None:
            data["get_equity_orders"] = orders_answer(orders)
        if mutate:
            mutate(data)
        return data

    def write(self, name, obj, *, age=0.0):
        p = self.root.path / "state" / "tmp" / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        t = self.clk.now.timestamp() - age
        os.utime(p, (t, t))
        return f"state/tmp/{name}"

    def tick(self, *args, env=None):
        """``tick.main`` in process with this root, the pinned clock and ``env``."""
        keys = ("QUIVER_BROKER_JOB", "QUIVER_BROKER_PROPOSAL")
        saved = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        for k, v in (env or {}).items():
            if k in keys:
                os.environ[k] = str(v)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = tick.main([str(a) for a in args])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        lines = buf.getvalue().strip().splitlines()
        return code, (json.loads(lines[-1]) if lines else None)

    def check(self, pid, phase, rel, env):
        return self.tick("exec-check", "--proposal", pid, "--phase", phase, "--input", rel, env=env)[1]

    def cli(self, *args, env=None, timeout=120):
        """The real ``tick.py`` as a subprocess (it does not see the pinned clock)."""
        e = {k: v for k, v in os.environ.items() if not k.startswith("QUIVER_BROKER_")}
        e.update(self.root.env)
        e.update(env or {})
        p = subprocess.run([PY, "tick.py", *[str(a) for a in args]], cwd=str(REPO), env=e,
                           capture_output=True, text=True, timeout=timeout)
        lines = (p.stdout or "").strip().splitlines()
        try:
            out = json.loads(lines[-1]) if lines else None
        except ValueError:
            out = {"raw": p.stdout}
        return p.returncode, out, p.stderr


def orders_answer(orders):
    """A ``get_equity_orders`` raw answer: the captured envelope with its orders list
    replaced by value-changed copies of captured orders."""
    a = copy.deepcopy(ORDERS_LIVE)
    a["data"]["orders"] = [copy.deepcopy(o) for o in orders]
    return a


def foreign_ref(ref) -> str:
    """Robinhood's own ``ref_id`` for the order of leg ``ref``. Robinhood does not echo the
    client ref_id: client 81e748c5 came back as 01a16428-ff4c-743b-ada7-1f05229ba861
    (``rh_place``). The value is a UUID that always differs from ``ref``."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"robinhood-order:{ref}"))


def order_from(leg_fields, *, oid, state="queued", created_at=None, qty=None, avg=None, drop=(), ref_id=None):
    """One broker order object for a leg: a captured order with its values changed. Its
    ``ref_id`` is Robinhood's own value (``foreign_ref``), as in production. Pass
    ``ref_id`` to set another value (the one equal-ref_id case gives the leg's ref_id)."""
    side = leg_fields["side"]
    o = copy.deepcopy(BUY_ORDER if side == "buy" else SELL_ORDER)
    o.update(id=oid, ref_id=ref_id if ref_id is not None else foreign_ref(leg_fields.get("ref_id")),
             symbol=leg_fields["symbol"], side=side, state=state)
    if side == "buy":
        o["dollar_based_amount"]["amount"] = f"{float(leg_fields['dollar_amount']):.6f}"
        o["quantity"] = f"{float(qty or 0):.6f}"
    else:
        o["quantity"] = f"{float(leg_fields['quantity']):.6f}"
    o["cumulative_quantity"] = f"{float(qty or 0):.6f}"
    o["average_price"] = f"{float(avg):.6f}" if avg else None
    o["executions"] = o["executions"][:1] if qty else []
    if created_at:
        o["created_at"] = created_at
    for k in drop:
        o.pop(k, None)
    return o


def review_payload(fields, *, captured=None, cust=None, checks="keep", top_cust=None, form="dict",
                   drop_data_keys=(), drop_data=False):
    """A PostToolUse payload for one review: the captured answer of the leg's side with its
    values set to the leg (§18.3 rh_review; ``top_cust`` adds the one allowed key)."""
    base = copy.deepcopy(captured or (REVIEW_BUY if fields["side"] == "buy" else REVIEW_SELL))
    ti = base["tool_input"]
    for k in list(ti):
        if k in fields:
            ti[k] = fields[k]
    resp = base["tool_response"]
    d = resp["data"]
    if captured is None:
        d["symbol"], d["side"] = fields["symbol"], fields["side"]
        if fields["side"] == "buy":
            d["dollar_amount"] = fields["dollar_amount"]
        else:
            d["quantity"] = fields["quantity"]
    if cust is not None:
        d["customer_approval_required"] = cust
    if checks != "keep":
        d["order_checks"] = checks
    for k in drop_data_keys:
        d.pop(k, None)
    if top_cust is not None:
        resp["customer_approval_required"] = top_cust
    if drop_data:
        resp.pop("data")
    if form == "string":
        resp = json.dumps(resp)
    elif form == "blocks":
        resp = [{"type": "text", "text": json.dumps(resp)}]
    return {"tool_name": "mcp__robinhood-trading__review_equity_order", "tool_input": ti,
            "tool_response": resp}


class FakeRH:
    """The broker side of the scripted model: orders it accepted and their state per poll."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.orders = {}       # ref_id -> order object
        self.script = {}       # symbol -> [state per poll] (the last repeats)
        self.fill = {}         # symbol -> (qty, avg)
        self.place_answer = {}  # symbol -> callable(fields) -> raw result, or "none" for no result
        self.polls = {}
        self.n = 0

    def place(self, fields):
        sym = fields["symbol"]
        ans = self.place_answer.get(sym)
        if ans == "none":
            return None
        if callable(ans):
            return ans(fields)
        self.n += 1
        o = order_from(fields, oid=f"ord-{self.n:04d}-{sym}", state="queued",
                       created_at=self.ctx.clk.now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z")
        self.orders[fields["ref_id"]] = o
        return o

    def poll(self):
        out = []
        for ref, o in self.orders.items():
            sym = o["symbol"]
            i = self.polls.get(ref, 0)
            self.polls[ref] = i + 1
            seq = self.script.get(sym, ["queued"])
            st = seq[min(i, len(seq) - 1)]
            o["state"] = st
            if st in ("filled", "partially_filled") and sym in self.fill:
                qty, avg = self.fill[sym]
                o["cumulative_quantity"] = f"{qty:.6f}"
                o["average_price"] = f"{avg:.6f}"
                if o["side"] == "sell":
                    o["quantity"] = f"{qty:.6f}" if st == "filled" else o["quantity"]
            out.append(copy.deepcopy(o))
        return out


# --- the scripted broker model -----------------------------------------------------------


class Stream:
    def __init__(self):
        self.events = []
        self.n = 0

    def call(self, name, inp, text, is_error):
        self.n += 1
        tid = f"toolu_t4_{self.n:04d}"
        self.events.append(ev_use(tid, name, inp))
        self.events.append(ev_result(tid, text, is_error))

    def text(self):
        return _dump(self.events)


class Bot:
    """Plays the broker model for one job. Every tool call goes through
    ``broker_guard.evaluate`` (the PreToolUse hook); a review also runs ``record_review``
    (the PostToolUse hook)."""

    def __init__(self, ctx, env, stream, job):
        self.ctx, self.env, self.s, self.job = ctx, env, stream, job
        self.denied = []

    def _hook(self, name, inp):
        return broker_guard.evaluate(name, inp, env=self.env, now=self.ctx.clk.now)

    def toolsearch(self):
        q = ("select:" + ",".join(f"mcp__robinhood-trading__{t}" for t in (
            "get_portfolio", "get_equity_positions", "get_equity_quotes", "get_trade_approval_setting",
            "get_equity_orders", "review_equity_order", "place_equity_order")))
        ok, why = self._hook("ToolSearch", {"query": q})
        assert ok, why
        self.s.call("ToolSearch", {"query": q}, "loaded", False)

    def rh(self, tool, inp, answer, *, is_error=False):
        name = f"mcp__robinhood-trading__{tool}"
        ok, why = self._hook(name, inp)
        assert ok, (tool, why)
        self.s.call(name, inp, answer if isinstance(answer, str) else json.dumps(answer), is_error)
        return answer

    def write(self, name, obj, *, age=0.0):
        path = str(self.ctx.root.path / "state" / "tmp" / name)
        ok, why = self._hook("Write", {"file_path": path, "content": "{}"})
        assert ok, why
        rel = self.ctx.write(name, obj, age=age)
        self.s.call("Write", {"file_path": path}, "written", False)
        return rel

    def bash(self, *args):
        cmd = ".venv/bin/python tick.py " + " ".join(str(a) for a in args)
        ok, why = self._hook("Bash", {"command": cmd})
        assert ok, (cmd, why)
        code, out = self.ctx.tick(*args, env=self.env)
        self.s.call("Bash", {"command": cmd}, json.dumps(out), code != 0)
        return out or {}

    def review(self, fields, payload=None):
        payload = payload or review_payload(fields)
        name = "mcp__robinhood-trading__review_equity_order"
        ok, why = self._hook(name, payload["tool_input"])
        assert ok, why
        broker_guard.record_review(payload, env=self.env, now=self.ctx.clk.now)
        resp = payload["tool_response"]
        self.s.call(name, payload["tool_input"], resp if isinstance(resp, str) else json.dumps(resp), False)

    def place(self, fields):
        name = "mcp__robinhood-trading__place_equity_order"
        ok, why = self._hook(name, fields)
        if not ok:
            self.denied.append((fields["ref_id"], why))
            self.s.call(name, fields, f"PreToolUse:{name} hook error: broker_guard: denied: {why}", True)
            return None
        res = self.ctx.rh.place(fields)
        self.s.call(name, fields, json.dumps(res) if res is not None else "", False)
        return res


def execute_runbook(bot, values, opts):
    """EXECUTE.md, normal job. ``opts`` holds the scripted broker data per step."""
    ctx, pid = bot.ctx, values["proposal_id"]
    bot.toolsearch()
    if opts.get("auth"):
        bot.rh("get_portfolio", {"account_number": "TEST1234"}, "Error: 401 Unauthorized", is_error=True)
        bot.bash("auth-stop", "--job", values["job_id"])
        return
    if opts.get("before_sells"):
        opts["before_sells"](ctx)
    sells = ctx.snapfile(**opts["sells"])
    rel = bot.write(f"exec_{pid}_sells.json", sells, age=opts.get("sells_age", 0.0))
    out = bot.bash("exec-check", "--proposal", pid, "--phase", "sells", "--input", rel)
    if opts.get("stop") == "after_sells" or out.get("action") in ("requote", "refused") or "error" in out:
        return
    ctx.clk.advance(2)
    sent = place_legs(bot, pid, out.get("legs") or [], opts)
    has_buys = any(l["side"] == "buy" for l in values["legs"])
    if has_buys:
        if any(f["side"] == "sell" for f in sent):
            poll(bot, pid, "sellpoll", stop_when_done=True)
        ctx.clk.advance(2)
        bspec = opts.get("buys", opts["sells"])
        buys = bspec(ctx) if callable(bspec) else ctx.snapfile(**bspec)
        rel = bot.write(f"exec_{pid}_buys.json", buys, age=opts.get("buys_age", 0.0))
        out = bot.bash("exec-check", "--proposal", pid, "--phase", "buys", "--input", rel)
        if "error" not in out and out.get("action") == "place":
            ctx.clk.advance(2)
            sent += place_legs(bot, pid, out.get("legs") or [], opts)
    if ctx.rh.orders and opts.get("final_polls", True):
        poll(bot, pid, "fills", stop_when_done=True)
    after = opts.get("after")
    if after is not None:
        data = ctx.snapfile(**after) if isinstance(after, dict) else after(ctx)
        rel = bot.write(f"exec_{pid}_after.json", data)
        bot.bash("snapshot-commit", "--input", rel)


def place_legs(bot, pid, legs, opts):
    results, sent = [], []
    for f in legs:
        if opts.get("review"):
            bot.review(f, opts["review"](f))
        else:
            bot.review(f)
        res = bot.place(f)
        if res is not None:
            results.append(res)
            sent.append(f)
        elif bot.ctx.leg(f["ref_id"])["state"] == "placing":
            sent.append(f)
    rel = bot.write(f"exec_{pid}_results.json", {"place_equity_order": results})
    bot.bash("exec-commit", "--proposal", pid, "--input", rel)
    return sent


def poll(bot, pid, tag, *, stop_when_done):
    """get_equity_orders up to 3 calls with ``tick.py wait --seconds 20`` between, then fills."""
    last = None
    for i in range(3):
        if i:
            bot.bash("wait", "--seconds", 20)
        last = orders_answer(bot.ctx.rh.poll())
        bot.rh("get_equity_orders", {"account_number": "TEST1234", "agent_initiated": True}, last)
        states = [o["state"] for o in last["data"]["orders"]]
        if stop_when_done and all(s in ("filled", "cancelled", "rejected", "failed") for s in states):
            break
    rel = bot.write(f"exec_{pid}_{tag}.json", {"get_equity_orders": last})
    return bot.bash("fills", "--input", rel)


def recovery_runbook(bot, values, opts):
    ctx, pid = bot.ctx, values["proposal_id"]
    bot.toolsearch()
    orders = opts["orders"](ctx) if callable(opts.get("orders")) else (opts.get("orders") or [])
    data = ctx.snapfile(**opts["recover"])
    data["get_equity_orders"] = orders_answer(orders)
    bot.rh("get_equity_orders", {"account_number": "TEST1234", "agent_initiated": True,
                                 "created_at_gte": values.get("created_at_gte")}, data["get_equity_orders"])
    rel = bot.write(f"exec_{pid}_recover.json", data)
    out = bot.bash("exec-check", "--proposal", pid, "--phase", "recover", "--input", rel)
    if out.get("action") == "place":
        ctx.clk.advance(2)
        place_legs(bot, pid, out.get("legs") or [], opts)
    if ctx.rh.orders:
        poll(bot, pid, "fills", stop_when_done=True)
    if opts.get("after") is not None:
        rel = bot.write(f"exec_{pid}_after.json", ctx.snapfile(**opts["after"]))
        bot.bash("snapshot-commit", "--input", rel)


def snapshot_runbook(bot, values, opts):
    ctx = bot.ctx
    bot.toolsearch()
    if opts.get("auth"):
        bot.rh("get_portfolio", {"account_number": "TEST1234"}, "Error: 401 Unauthorized", is_error=True)
        bot.bash("auth-stop", "--job", values["job_id"])
        return
    data = ctx.snapfile(**opts["snap"]) if isinstance(opts["snap"], dict) else opts["snap"](ctx)
    if values.get("reconcile") or opts.get("orders") is not None:
        orders = opts["orders"](ctx) if callable(opts.get("orders")) else ctx.rh.poll()
        data["get_equity_orders"] = orders_answer(orders)
    rel = bot.write(f"snapshot_{values['job_id']}.json", data)
    bot.bash("snapshot-commit", "--input", rel)


class Runner:
    """The injected process runner of ``Broker``: ``claude --version``, the two canary
    runs (the captured real streams), and the jobs (the scripted ``Bot``)."""

    def __init__(self, ctx, *, opts=None, code=0, err="", version="2.1.312 (Claude Code)",
                 canary=None, log=None):
        self.ctx = ctx
        self.opts = opts if opts is not None else {}
        self.code, self.err, self.version = code, err, version
        self.canary = canary or (lambda which, n: CANARY_GUARD if which == "guard" else CANARY_FALSE)
        self.calls = []
        self.canary_calls = 0
        self.jobs = []
        self.log = log if log is not None else []

    def __call__(self, argv, env, timeout, cwd):
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout, "cwd": cwd})
        if list(argv[1:]) == ["--version"]:
            return 0, self.version + "\n", ""
        if run_broker.CANARY_PROMPT in argv:
            self.canary_calls += 1
            self.log.append("canary")
            settings = argv[argv.index("--settings") + 1]
            which = "false" if settings.endswith("canary_fail.json") else "guard"
            return 0, _dump(self.canary(which, self.canary_calls)), ""
        pf = Path(argv[argv.index("--append-system-prompt-file") + 1])
        text = pf.read_text(encoding="utf-8")
        values = json.loads(text.rsplit("```json\n", 1)[1].split("\n```", 1)[0])
        self.jobs.append({"values": values, "argv": list(argv), "env": dict(env), "prompt": text})
        self.log.append(f"spawn:{values['job_id']}")
        stream = Stream()
        opts = self.opts(values) if callable(self.opts) else self.opts
        if opts.get("script") != "none":
            bot = Bot(self.ctx, env, stream, values["job_id"])
            if "proposal_id" in values:
                (recovery_runbook if values.get("recovery") else execute_runbook)(bot, values, opts)
            else:
                snapshot_runbook(bot, values, opts)
            self.last_bot = bot
        code = opts.get("code", self.code)
        return code, stream.text() + opts.get("extra_stdout", ""), opts.get("err", self.err)


@contextlib.contextmanager
def case(name, *, at="2026-08-05T13:35:00Z", book=TEST_BOOK, snap=True, extra_config="", **snapkw):
    """One case: a temp root, the seeded book, the first snapshot (the §18.4 ``snap`` values
    unless given), a pinned clock, and every module path pointed at the root."""
    saved = (broker_guard.ROOT, run_broker.ROOT, rc.ROOT, rc.TICK_LOG, rc.VENV_PY, rc._maybe_alert,
             tick.REPO, tick.LEDGER_DB, tick.CONFIG_PATH, tick.time)
    with temp_root(extra_config=extra_config) as root:
        clk = FakeClock(at).pin_market()
        try:
            broker_guard.ROOT = run_broker.ROOT = rc.ROOT = root.path
            rc.TICK_LOG = str(root.path / "logs" / "tick.log")
            tick.REPO, tick.LEDGER_DB, tick.CONFIG_PATH = root.path, root.db, root.path / "config.yaml"
            tick.time = _TimeProxy(clk)
            seed_desk(root, book=book, load=False)
            ctx = Ctx(root, clk)
            rc._maybe_alert = ctx.pages
            if snap:
                ctx.snap(at=at, **snapkw)
            yield ctx
        except Exception as e:  # noqa: BLE001 — a crash fails the case, never the suite
            r.ok(f"{name}: ran without an exception", False, "".join(traceback.format_exception(e))[-3000:])
        finally:
            clk.unpin()
            (broker_guard.ROOT, run_broker.ROOT, rc.ROOT, rc.TICK_LOG, rc.VENV_PY, rc._maybe_alert,
             tick.REPO, tick.LEDGER_DB, tick.CONFIG_PATH, tick.time) = saved


def tick_log(ctx):
    p = ctx.root.path / "logs" / "tick.log"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


STD_POS = {"ANET": 2, "CEG": 1, "SGOV": 11}
STD_Q = {"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0}


def std(**kw):
    out = {"positions": dict(STD_POS), "quotes": dict(STD_Q), "bp": 300.0, "equity": 2000.0}
    out.update(kw)
    return out


def set_avail(sym, avail):
    def m(data):
        for p in data["get_equity_positions"]["data"]["positions"]:
            if p["symbol"] == sym:
                p["shares_available_for_sells"] = f"{float(avail):.6f}"
    return m


def set_quote(sym, **vals):
    def m(data):
        for it in data["get_equity_quotes"]["data"]["results"]:
            if it["quote"]["symbol"] == sym:
                for k, v in vals.items():
                    if v is None:
                        it["quote"].pop(k, None)
                    else:
                        it["quote"][k] = v
    return m



# =============================================================================================
# prepare (§11.1 step 5; G7)
# =============================================================================================

def t_prepare():
    with case("prepare ok") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.clk.advance(60)
        c.approve(built)
        c.clk.advance(30)
        prep, env = c.start(pid)
        r.eq("prepare: all checks hold -> ok", prep.get("ok"), True)
        r.eq("prepare: proposal executing", c.prop(pid)["state"], "executing")
        r.eq("prepare: legs stay approved", [l["state"] for l in c.legs(pid)], ["approved"])
        r.eq("prepare: trade_date is today's session", [l["trade_date"] for l in c.legs(pid)], ["2026-08-05"])
        r.eq("prepare: returns account_number", prep.get("account_number"), "TEST1234")
        r.eq("prepare: returns the legs", [(l["symbol"], l["side"]) for l in prep.get("legs", [])], [("ANET", "buy")])

    # G7: a drift-style proposal made after the close executes on the next session date.
    with case("prepare G7", at="2026-08-05T13:35:00Z") as c:
        c.at("2026-08-05T20:35:00Z")
        c.snap()
        c.at("2026-08-05T20:40:00Z")
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        r.eq("G7: an after-close proposal expires at the next session close", built["expires_at"], "2026-08-06T20:00:00Z")
        r.eq("G7: its leg trade_date is the session date of expires_at", c.legs(pid)[0]["trade_date"], "2026-08-06")
        c.at("2026-08-06T13:45:00Z")
        c.snap()
        c.approve(built)
        c.clk.advance(20)
        prep, env = c.start(pid)
        r.eq("G7: the next-day tap executes", prep.get("ok"), True)
        r.eq("G7: trade_date = today's session date", c.legs(pid)[0]["trade_date"], "2026-08-06")

    # a failed check: failed, legs skipped, the reason sent by run_broker
    with case("prepare fail kill") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        c.clk.advance(5)
        c.root.kill.write_text("stop")
        runner = Runner(c)
        c.broker(runner).drain()
        r.eq("prepare fail: proposal failed", c.prop(pid)["state"], "failed")
        r.eq("prepare fail: legs skipped + finalized", [(l["state"], l["finalized"]) for l in c.legs(pid)], [("skipped", 1)])
        r.ok("prepare fail: run_broker sends the reason", any("Trading is stopped on the box" in t and f"#{pid}" in t
                                                           for t in c.tg.texts()), c.tg.texts())
        r.eq("prepare fail: no claude -p job was spawned", runner.jobs, [])
        r.eq("prepare fail: job failed", c.job_of(pid)["status"], "failed")

    # approval setting on in the latest snapshot
    with case("prepare approval on", approval=True) as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        c.clk.advance(5)
        c.broker(Runner(c)).drain()
        r.eq("prepare approval on: failed", c.prop(pid)["state"], "failed")
        r.ok("prepare approval on: the Robinhood-app text",
             any("Turn them off in the Robinhood app (Agent Settings → Safety Controls), then tap Approve again." in t
                 for t in c.tg.texts()), c.tg.texts())

    # an unknown approval setting (the tool result is missing) in the latest row
    with case("prepare approval unknown") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        c.clk.advance(5)
        data = c.snapfile(**std(), drop=("get_trade_approval_setting",))
        execution.snapshot_commit(c.led, c.cfg, data, now_dt=c.clk.now)
        r.eq("prepare: latest row approval_setting unknown", c.q("SELECT approval_setting FROM broker_snapshots "
                                                                  "ORDER BY id DESC LIMIT 1")[0]["approval_setting"], "unknown")
        prep, _ = c.start(pid)
        r.eq("prepare: unknown approval setting refused", (prep.get("ok"), prep.get("reason")), (False, "approval_setting"))

    # decided_at 10 min + 1 s old -> refused; 9 min 59 s -> ok
    for secs, want in ((601, False), (599, True)):
        with case(f"prepare tap age {secs}") as c:
            built = c.build(["ANET"])
            pid = built["proposal_id"]
            c.approve(built)
            c.clk.advance(secs)
            c.snap()
            prep, _ = c.start(pid)
            r.eq(f"prepare: tap {secs} s old -> ok={want}", prep.get("ok"), want)
            if not want:
                r.ok("prepare: the 'within 10 minutes of your tap' text",
                     f"I could not start #{pid} within 10 minutes of your tap, so I placed nothing. "
                     "Ask me for fresh numbers." == prep.get("text"), prep)

    # at least 5 minutes before the close
    for prep_at, want in (("2026-08-05T19:55:00Z", True), ("2026-08-05T19:55:01Z", False)):
        with case(f"prepare close {prep_at}", at="2026-08-05T19:45:00Z") as c:
            built = c.build(["ANET"])
            pid = built["proposal_id"]
            c.at("2026-08-05T19:54:00Z")
            c.approve(built)
            c.at(prep_at)
            prep, _ = c.start(pid)
            r.eq(f"prepare: {prep_at} (5 min rule) -> ok={want}", prep.get("ok"), want)

    # halted today
    with case("prepare halted") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        c.led.mark_halted("2026-08-05", "test halt")
        prep, _ = c.start(pid)
        r.eq("prepare: halted -> refused", (prep.get("ok"), prep.get("reason")), (False, "halted"))
        r.eq("prepare: halted -> legs skipped", [l["state"] for l in c.legs(pid)], ["skipped"])

    # the CLI: an expired proposal (the subprocess clock is the real one, past 2026-08-05)
    with case("prepare cli") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        code, out, err = c.cli("exec-prepare", "--proposal", pid)
        r.eq("prepare cli: exit 0 with a refusal", (code, out.get("ok"), out.get("reason")), (0, False, "expired"))
        r.eq("prepare cli: proposal failed", c.prop(pid)["state"], "failed")
        r.eq("prepare cli: legs skipped", [l["state"] for l in c.legs(pid)], ["skipped"])


# =============================================================================================
# check_gate (§11.2 steps 2 and 5)
# =============================================================================================

def t_check_gate():
    # a leg is placeable only after exec-check
    with case("gate placeable") as c:
        pid, env = c.ready(["ANET"])
        leg = c.legs(pid)[0]
        fields = execution.leg_fields(c.cfg, leg)
        ok, why = broker_guard.evaluate("mcp__robinhood-trading__place_equity_order", fields, env=env, now=c.clk.now)
        r.eq("gate: an approved leg is not placeable", (ok, "not checked" in why), (False, True))
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        r.eq("gate: buy-only sells phase returns no leg", out.get("action"), "none")
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std())), env)
        leg = c.legs(pid)[0]
        r.eq("gate: after exec-check the leg is checked", leg["state"], "checked")
        r.eq("gate: check_quote written", leg["check_quote"], 150.0)
        r.eq("gate: checked_at written", leg["checked_at"], c.now())
        r.eq("gate: review_ok reset", (leg["review_ok"], leg["review_json"]), (0, None))

    # file age: 61 s refused, 60 s accepted
    for age, refused in ((61, True), (60, False)):
        with case(f"gate file age {age}") as c:
            pid, env = c.ready(["ANET"])
            out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std()), age=age), env)
            if refused:
                r.ok("gate: a sells file 61 s old is refused", out.get("action") == "none" and "61 s old" in str(out.get("reason")), out)
                r.eq("gate: 61 s old -> proposal failed", c.prop(pid)["state"], "failed")
            else:
                r.eq("gate: a sells file 60 s old is accepted", (out.get("action"), c.prop(pid)["state"]), ("none", "executing"))

    # quote age: every venue time 121 s old refused; 120 s accepted
    for age, refused in ((121, True), (120, False)):
        with case(f"gate quote age {age}") as c:
            pid, env = c.ready(["ANET"])
            at = store.iso(c.clk.now - timedelta(seconds=age))
            out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(), at=at)), env)
            if refused:
                r.eq("gate: a quote 121 s old is refused", (out.get("action"), c.prop(pid)["state"]), ("none", "failed"))
                res = json.loads(c.prop(pid)["result_json"])
                r.eq("gate: the stale-quote text", res.get("reason_text"), "I could not get a fresh ANET price, so I placed nothing.")
            else:
                r.eq("gate: a quote 120 s old is accepted", (out.get("action"), c.prop(pid)["state"]), ("none", "executing"))

    # the daily-loss check on the fresh equity
    with case("gate halt") as c:
        pid, env = c.ready(["ANET"])
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(equity=1600.0))), env)
        r.eq("gate: equity down 20% -> no leg", out.get("action"), "none")
        r.eq("gate: day_baseline.halted", c.led.is_halted("2026-08-05"), True)
        r.eq("gate: halt -> proposal failed", c.prop(pid)["state"], "failed")
        r.eq("gate: no KILL written by exec-check", c.root.kill.exists(), False)

    with case("gate kill") as c:
        pid, env = c.ready(["ANET"])
        c.root.kill.write_text("x")
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        r.eq("gate: KILL -> no leg, failed", (out.get("action"), c.prop(pid)["state"]), ("none", "failed"))

    # a sell re-sized down: to the live target, and to the free quantity; never up
    with case("gate sell live target", positions={"ANET": 5, "CEG": 1, "SGOV": 11}) as c:
        pid, env = c.ready(["ANET"])
        row_q = c.legs(pid)[0]["quantity"]
        r.eq("gate: the built sell (ANET 5 sh, deployable 2450, T 612.50)", row_q, round((750 - 612.5) / 150, 6))
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(positions={"ANET": 5, "CEG": 1, "SGOV": 11},
                                                                        quotes={"ANET": 148.0, "CEG": 300.0, "SGOV": 100.0}))), env)
        d = 5 * 148.0 + 300 + 1100 + 300
        want = round(min(row_q, (5 * 148.0 - 0.25 * d) / 148.0, 5), 6)
        r.eq("gate: a sell re-sized down to the live target", [l.get("quantity") for l in out.get("legs", [])], [f"{want:.6f}"])
        r.eq("gate: the row quantity follows", c.legs(pid)[0]["quantity"], want)
    with case("gate sell free qty", positions={"ANET": 5, "CEG": 1, "SGOV": 11}) as c:
        pid, env = c.ready(["ANET"])
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(positions={"ANET": 5, "CEG": 1, "SGOV": 11}),
                                                                 mutate=set_avail("ANET", 0.5))), env)
        r.eq("gate: a sell sizes from the free quantity", [l.get("quantity") for l in out.get("legs", [])], ["0.500000"])
    with case("gate sell never up", positions={"ANET": 5, "CEG": 1, "SGOV": 11}) as c:
        pid, env = c.ready(["ANET"])
        row_q = c.legs(pid)[0]["quantity"]
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(positions={"ANET": 9, "CEG": 1, "SGOV": 11}))), env)
        r.eq("gate: a sell is never re-sized up", [l.get("quantity") for l in out.get("legs", [])], [f"{row_q:.6f}"])

    # a buy re-sized down to the room under the current target; never up
    with case("gate buy room") as c:
        pid, env = c.ready(["ANET"])
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        live = std(positions={"ANET": 2.5, "CEG": 1, "SGOV": 11})
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**live)), env)
        d = 2.5 * 150 + 300 + 1100 + 300
        room = 0.25 * d - 2.5 * 150
        money = min(300 - 5, 300 + 1100 - 0.60 * d)
        want = round(min(200.0, room, money), 2)
        r.eq("gate: a buy re-sized down to the room under the target", [l.get("dollar_amount") for l in out.get("legs", [])], [f"{want:.2f}"])
        r.eq("gate: clamped_from recorded", c.legs(pid)[0]["clamped_from"], 200.0)
    with case("gate buy never up") as c:
        pid, env = c.ready(["ANET"])
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std(bp=1_000_000.0))), env)
        r.eq("gate: a buy is never re-sized up", ([l.get("dollar_amount") for l in out.get("legs", [])], c.legs(pid)[0]["clamped_from"]),
             (["200.00"], None))

    # approval setting on, or missing, in the sells file
    for label, kw in (("on", {"approval": True}), ("missing", {"drop": ("get_trade_approval_setting",)})):
        with case(f"gate approval {label}") as c:
            pid, env = c.ready(["ANET"])
            out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(), **kw)), env)
            r.eq(f"gate: approval setting {label} in the sells file -> no leg, failed",
                 (out.get("action"), c.prop(pid)["state"]), ("none", "failed"))
            r.eq(f"gate: approval {label} stored", c.q("SELECT approval_setting FROM broker_snapshots ORDER BY id DESC LIMIT 1")[0]["approval_setting"],
                 "on" if label == "on" else "unknown")

    # refused quotes: state, has_traded, every venue time missing
    for label, mut in (("state halted", set_quote("ANET", state="halted")),
                       ("has_traded false", set_quote("ANET", has_traded=False)),
                       ("no venue times", set_quote("ANET", venue_last_trade_time=None, venue_bid_time=None, venue_ask_time=None))):
        with case(f"gate quote {label}") as c:
            pid, env = c.ready(["ANET"])
            out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(), mutate=mut)), env)
            r.eq(f"gate: a quote with {label} is refused", (out.get("action"), c.prop(pid)["state"]), ("none", "failed"))
    # no venue_last_trade_time, fresh bid/ask: §11.2 prices it at the midpoint
    with case("gate quote no last time") as c:
        pid, env = c.ready(["ANET"])
        mut = set_quote("ANET", venue_last_trade_time=None, bid_price="149.900000", ask_price="150.100000")
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std(), mutate=mut)), env)
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std(), mutate=mut)), env)
        r.eq("gate: no venue_last_trade_time + fresh bid/ask -> the midpoint (§11.2)",
             (out.get("action"), c.legs(pid)[0]["check_quote"]), ("place", 150.0))

    # from rh_snap: BOT with a 300 s old last trade and fresh bid/ask
    snap_q = {it["quote"]["symbol"]: it["quote"] for it in fx("rh_snap")["get_equity_quotes"]["data"]["results"]}
    bot_q = copy.deepcopy(snap_q["BOT"])
    at = quotes.parse_venue_time(bot_q["venue_bid_time"])
    bot_q["venue_last_trade_time"] = store.iso(at - timedelta(seconds=300))
    price, why = quotes.leg_price(bot_q, at)
    r.eq("gate: captured BOT bid/ask spread over 1% is refused", (price, why), (None, "wide_spread"))
    bot_q["ask_price"] = "24.200000"
    price, why = quotes.leg_price(bot_q, at)
    r.eq("gate: BOT 300 s since the last trade, spread within 1% -> the midpoint",
         (price, why), (round((24.09 + 24.20) / 2, 6), "mid"))
    with case("gate mid in exec-check") as c:
        pid, env = c.ready(["ANET"])
        old = store.iso(c.clk.now - timedelta(seconds=300)).replace("Z", ".000000000Z")
        mut = set_quote("ANET", venue_last_trade_time=old, bid_price="149.800000", ask_price="150.300000")
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std(), mutate=mut)), env)
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std(), mutate=mut)), env)
        r.eq("gate: exec-check prices a stale-last quote at the midpoint", c.legs(pid)[0]["check_quote"], 150.05)
    with case("gate wide spread in exec-check") as c:
        pid, env = c.ready(["ANET"])
        old = store.iso(c.clk.now - timedelta(seconds=300)).replace("Z", ".000000000Z")
        mut = set_quote("ANET", venue_last_trade_time=old, bid_price="149.000000", ask_price="151.000000")
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(), mutate=mut)), env)
        r.eq("gate: a spread over 1% is refused in exec-check", (out.get("action"), c.prop(pid)["state"]), ("none", "failed"))

    # G24: a raw result as a JSON string, or as MCP content blocks, reads the same
    with case("gate raw forms") as c:
        pid, env = c.ready(["ANET"])

        def forms(data):
            data["get_portfolio"] = json.dumps(data["get_portfolio"])
            data["get_equity_quotes"] = [{"type": "text", "text": json.dumps(data["get_equity_quotes"])}]
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std(), mutate=forms)), env)
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std(), mutate=forms)), env)
        r.eq("G24: string and content-block results parse", (out.get("action"), c.legs(pid)[0]["check_quote"]), ("place", 150.0))

    # --phase for a proposal that is not QUIVER_BROKER_PROPOSAL (the CLI)
    with case("gate other proposal") as c:
        pid, env = c.ready(["ANET"])
        rel = c.write("s.json", c.snapfile(**std()))
        code, out, err = c.cli("exec-check", "--proposal", pid, "--phase", "sells", "--input", rel,
                               env={**env, "QUIVER_BROKER_PROPOSAL": str(pid + 1)})
        r.ok("gate cli: --phase for another proposal refused", code == 1 and "only on the proposal" in str(out), (code, out))
        code, out, err = c.cli("exec-check", "--proposal", pid, "--phase", "sells", "--input", rel,
                               env={"QUIVER_BROKER_JOB": env["QUIVER_BROKER_JOB"]})
        r.ok("gate cli: no QUIVER_BROKER_PROPOSAL refused", code == 1 and "only on the proposal" in str(out), (code, out))
        r.eq("gate cli: nothing changed", [l["state"] for l in c.legs(pid)], ["approved"])

    # a second --phase sells, and --phase buys before the sells: refused, nothing changes (the CLI)
    with case("gate phase order") as c:
        pid, env = c.ready(["ANET"])
        rel = c.write("s.json", c.snapfile(**std()))
        code, out, _ = c.cli("exec-check", "--proposal", pid, "--phase", "buys", "--input", rel, env=env)
        r.eq("gate cli: --phase buys before the sells -> refused", (code, out.get("action")), (0, "refused"))
        r.eq("gate cli: nothing changed", ([l["state"] for l in c.legs(pid)], c.q("SELECT COUNT(*) n FROM broker_snapshots")[0]["n"]),
             (["approved"], 1))
        c.check(pid, "sells", c.write("s2.json", c.snapfile(**std())), env)
        before = c.q("SELECT COUNT(*) n FROM broker_snapshots")[0]["n"]
        code, out, _ = c.cli("exec-check", "--proposal", pid, "--phase", "sells", "--input", rel, env=env)
        r.eq("gate cli: a second --phase sells -> refused", (code, out.get("action")), (0, "refused"))
        r.eq("gate cli: the second sells stored nothing", c.q("SELECT COUNT(*) n FROM broker_snapshots")[0]["n"], before)
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std())), env)
        out2 = c.check(pid, "buys", c.write("b2.json", c.snapfile(**std())), env)
        r.eq("gate: a second --phase buys -> refused", out2.get("action"), "refused")
        r.eq("gate: --phase recover in a normal job -> refused", c.check(pid, "recover", c.write("r.json", c.snapfile(**std(), orders=[])), env).get("action"),
             "refused")


# =============================================================================================
# a full execute job through run_broker (helpers)
# =============================================================================================

def run_job(c, pid, opts, *, runner=None):
    runner = runner or Runner(c, opts=opts)
    b = c.broker(runner)
    b.drain()
    return runner, b


def two_leg(c):
    """TEST_BOOK with ANET 5 sh and no CEG: a sell of ANET and a buy of CEG."""
    return c.build(["ANET", "CEG"])


POS_SB = {"ANET": 5, "SGOV": 11}
Q_SB = {"ANET": 150.0, "CEG": 300.0, "SGOV": 100.0}


def sb(**kw):
    out = {"positions": dict(POS_SB), "quotes": dict(Q_SB), "bp": 300.0, "equity": 2150.0}
    out.update(kw)
    return out


# =============================================================================================
# check_gate, the job-level rows (run_broker): halt page, buys after a filled sell
# =============================================================================================

def t_check_gate_jobs():
    # a halt set by exec-check -> one halt page and one daily_halt line from run_broker
    with case("gate job halt", positions=POS_SB, quotes=Q_SB, equity=2150.0) as c:
        built = two_leg(c)
        pid = built["proposal_id"]
        c.approve(built)
        runner, b = run_job(c, pid, {"sells": sb(equity=1720.0)})
        r.eq("gate job: halt -> proposal failed", c.prop(pid)["state"], "failed")
        r.eq("gate job: one halt page", [k for k in c.pages.kinds() if k[0] == "halt"], [("halt", notify.HALT_STAGE)])
        r.eq("gate job: one page sent", len([t for t in c.pages.sent]), 1)
        r.eq("gate job: one daily_halt line in tick.log", len([x for x in tick_log(c) if x.get("event") == "daily_halt"]), 1)
        r.eq("gate job: nothing placed", c.rh.orders, {})

    # approval on / stale file / refused quote in the buys phase after a filled sell
    sell_qty = round((750 - 0.25 * 2150) / 150, 6)
    for label, extra, rule in (
            ("approval on", {"buys": lambda c: c.snapfile(**sb(positions={"ANET": 5 - sell_qty, "SGOV": 11},
                                                             bp=300 + sell_qty * 150), approval=True)},
             "approval_setting"),
            ("file 61 s old", {"buys_age": 61}, "stale_input"),
            ("quote refused", {"buys": lambda c: c.snapfile(**sb(positions={"ANET": 5 - sell_qty, "SGOV": 11},
                                                                bp=300 + sell_qty * 150),
                                                            mutate=set_quote("CEG", state="halted"))},
             "quote_state")):
        with case(f"gate buys {label}", positions=POS_SB, quotes=Q_SB, equity=2150.0) as c:
            built = two_leg(c)
            pid = built["proposal_id"]
            c.approve(built)
            c.rh.script = {"ANET": ["filled"]}
            c.rh.fill = {"ANET": (sell_qty, 150.0)}
            opts = {"sells": sb(), "buys": sb(positions={"ANET": 5 - sell_qty, "SGOV": 11}, bp=300 + sell_qty * 150),
                    "after": sb(positions={"ANET": 5 - sell_qty, "SGOV": 11}, bp=300 + sell_qty * 150)}
            opts.update(extra)
            run_job(c, pid, opts)
            legs = {l["ticker"]: l for l in c.legs(pid)}
            r.eq(f"gate buys {label}: the sell filled", legs["ANET"]["state"], "filled")
            r.eq(f"gate buys {label}: the buy skipped with the rule", (legs["CEG"]["state"], legs["CEG"]["skip_reason"]),
                 ("skipped", rule))
            r.eq(f"gate buys {label}: proposal partially_executed, never failed", c.prop(pid)["state"], "partially_executed")

    # a buys-only proposal: --phase sells, then --phase buys -> the buy placed
    with case("gate buys only") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        c.rh.script = {"ANET": ["filled"]}
        c.rh.fill = {"ANET": (1.333333, 150.0)}
        runner, b = run_job(c, pid, {"sells": std(), "after": std(positions={"ANET": 3.333333, "CEG": 1, "SGOV": 11}, bp=100.0)})
        leg = c.legs(pid)[0]
        r.eq("gate buys only: the buy placed and filled", (leg["state"], leg["broker_order_id"] is not None), ("filled", True))
        r.eq("gate buys only: executed", c.prop(pid)["state"], "executed")
        r.ok("gate buys only: the owner gets the result", any(t.startswith("Done. Bought $200.00 of ANET") for t in c.tg.texts()), c.tg.texts())


# =============================================================================================
# requote_buys (§11.2 step 5)
# =============================================================================================

POS_B = {"ANET": 5, "SGOV": 11}
Q_B = {"ANET": 150.0, "CEG": 300.0, "GEV": 900.0, "SGOV": 100.0}


def t_requote_buys():
    sell_qty = round((750 - 0.20 * 2150) / 150, 6)
    after_sell = {"positions": {"ANET": 5 - sell_qty, "SGOV": 11}, "bp": 300 + sell_qty * 150, "equity": 2150.0}

    def bspec(**q):
        return {**after_sell, "quotes": {**Q_B, **q}}

    with case("requote_buys", book=BOOK_B, positions=POS_B, quotes=Q_B, equity=2150.0) as c:
        built = c.build(["ANET", "CEG", "GEV"])
        pid = built["proposal_id"]
        legs0 = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("requote_buys: built sell ANET + buys CEG, GEV",
             (legs0["ANET"]["quantity"], legs0["CEG"]["dollar_amount"], legs0["GEV"]["dollar_amount"]),
             (sell_qty, 0.15 * 2150, 0.05 * 2150))
        c.approve(built)
        c.rh.script = {"ANET": ["filled"], "GEV": ["filled"]}
        c.rh.fill = {"ANET": (sell_qty, 150.0), "GEV": (107.5 / 900.0, 900.0)}
        run_job(c, pid, {"sells": {**sb(), "quotes": Q_B}, "buys": bspec(CEG=304.80),
                         "after": {**after_sell, "quotes": {**Q_B, "CEG": 304.80}}})
        legs = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("requote_buys: the moved buy requoted + finalized", (legs["CEG"]["state"], legs["CEG"]["finalized"]), ("requoted", 1))
        r.eq("requote_buys: the other buy placed and filled", legs["GEV"]["state"], "filled")
        r.eq("requote_buys: the sell filled", legs["ANET"]["state"], "filled")
        r.eq("requote_buys: proposal partially_executed", c.prop(pid)["state"], "partially_executed")
        new = c.q("SELECT * FROM proposals WHERE id != ? AND kind='trade'", (pid,))
        r.eq("requote_buys: one new pending proposal", [p["state"] for p in new], ["pending"])
        if new:
            nl = c.legs(new[0]["id"])
            r.eq("requote_buys: the new proposal is the moved buy only", [(l["ticker"], l["side"]) for l in nl], [("CEG", "buy")])
            r.eq("requote_buys: sized from the exec-check quote (G29)", nl[0]["quote_at_proposal"], 304.80)
            r.eq("requote_buys: superseded_by on the old one", c.prop(pid)["superseded_by"], new[0]["id"])
            r.eq("requote_buys: same origin", (new[0]["origin"], new[0]["owner_message_id"]),
                 (c.prop(pid)["origin"], c.prop(pid)["owner_message_id"]))
        msg = next((t for t in c.tg.texts() if "moved" in t), "")
        r.ok("requote_buys: the message names the sells first",
             msg.startswith("The sells went out:") and msg.index("ANET") < msg.index("CEG moved 1.6%"), msg)
        r.ok("requote_buys: the new card has buttons", any(m == "sendMessage" and "reply_markup" in p and f"p:{new[0]['id'] if new else 0}:" in json.dumps(p)
                                                           for m, p in c.tg.calls), None)
        with c.led.connect() as conn:
            r.eq("requote_buys: the requoted leg does not block CEG (§10.1 rule 4)", proposals.in_flight_ticker(conn, "CEG"), None)

    # a halt in the buys phase skips only the unplaced buys; an open sell keeps its state and is reconciled
    with case("requote_buys halt", book=BOOK_B, positions=POS_B, quotes=Q_B, equity=2150.0) as c:
        built = c.build(["ANET", "CEG", "GEV"])
        pid = built["proposal_id"]
        c.approve(built)
        c.rh.script = {"ANET": ["queued"]}
        run_job(c, pid, {"sells": {**sb(), "quotes": Q_B}, "buys": {**bspec(), "equity": 1700.0}, "after": None})
        legs = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("requote_buys halt: the open sell keeps its state", legs["ANET"]["state"], "placed")
        r.eq("requote_buys halt: the buys skipped (halted)", [(legs[t]["state"], legs[t]["skip_reason"]) for t in ("CEG", "GEV")],
             [("skipped", "halted"), ("skipped", "halted")])
        r.eq("requote_buys halt: proposal stays executing", c.prop(pid)["state"], "executing")
        r.eq("requote_buys halt: one halt page", [k for k in c.pages.kinds() if k[0] == "halt"], [("halt", notify.HALT_STAGE)])
        # the next snapshot job reconciles the open sell
        c.rh.script = {"ANET": ["filled"]}
        c.rh.fill = {"ANET": (sell_qty, 150.0)}
        c.clk.advance(300)
        with c.led.connect() as conn:
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
        run_job(c, pid, {"snap": {**after_sell, "quotes": Q_B, "equity": 1700.0}})
        legs = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("requote_buys halt: the open sell is reconciled (filled)", legs["ANET"]["state"], "filled")
        r.eq("requote_buys halt: then partially_executed", c.prop(pid)["state"], "partially_executed")


# =============================================================================================
# stopped (§11.1 steps 3 and 9)
# =============================================================================================

def t_stopped():
    def after_close(c, pid, label, want_text):
        legs = c.legs(pid)
        r.eq(f"stopped {label}: proposal failed", c.prop(pid)["state"], "failed")
        r.eq(f"stopped {label}: every leg skipped, finalized=1", [(l["state"], l["finalized"]) for l in legs],
             [("skipped", 1)] * len(legs))
        r.eq(f"stopped {label}: skip_reason not_placed", [l["skip_reason"] for l in legs], ["not_placed"] * len(legs))
        r.ok(f"stopped {label}: the owner text", any(want_text in t for t in c.tg.texts()), (want_text, c.tg.texts()))
        if c.root.kill.exists():
            c.root.kill.unlink()   # the owner lifts KILL; the ticker itself must not be blocked
        try:
            again = c.build(["ANET"])
            r.ok(f"stopped {label}: a new build_trade on the ticker passes", again["proposal_id"] > pid, again)
        except proposals.ProposalError as e:
            r.ok(f"stopped {label}: a new build_trade on the ticker passes", False, str(e))

    with case("stopped exit1") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        run_job(c, pid, {"script": "none", "code": 1, "err": "boom"})
        after_close(c, pid, "exit 1", f"I placed nothing for #{pid}: the broker run stopped (exit 1).")
        r.eq("stopped exit 1: job failed", c.job_of(pid)["status"], "failed")

    with case("stopped selftest") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        rc.VENV_PY = "/usr/bin/true"      # the guard "answers" exit 0: not a deny
        runner, _ = run_job(c, pid, {"sells": std()})
        r.eq("stopped self-test: no claude -p job spawned", runner.jobs, [])
        after_close(c, pid, "self-test", run_broker.GUARD_FAIL_TEXT)
        r.ok("stopped self-test: a broker page", ("error", "broker") in c.pages.kinds(), c.pages.kinds())

    with case("stopped auth") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        run_job(c, pid, {"auth": True})
        after_close(c, pid, "auth-stop", f"I placed nothing for #{pid}: the Robinhood login expired.")
        r.ok("stopped auth-stop: the auth_error page", ("auth_error", notify.AUTH_STAGE) in c.pages.kinds(), c.pages.kinds())

    with case("stopped kill") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        run_job(c, pid, {"sells": std(), "before_sells": lambda c: c.root.kill.write_text("x")})
        after_close(c, pid, "KILL at exec-check", f"placed nothing for #{pid}")
        r.eq("stopped KILL: nothing placed", c.rh.orders, {})

    with case("stopped runbook end") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        run_job(c, pid, {"sells": std(), "stop": "after_sells"})
        after_close(c, pid, "runbook ends after exec-check",
                    f"I placed nothing for #{pid}: the runbook ended before it placed the orders.")

    with case("stopped checked legs", positions=POS_SB, quotes=Q_SB, equity=2150.0) as c:
        built = two_leg(c)
        pid = built["proposal_id"]
        c.approve(built)
        run_job(c, pid, {"sells": sb(), "stop": "after_sells"})
        legs = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("stopped: a checked sell and an approved buy both end not_placed",
             [(legs[t]["state"], legs[t]["skip_reason"], legs[t]["finalized"]) for t in ("ANET", "CEG")],
             [("skipped", "not_placed", 1)] * 2)

    # a broker_jobs row in running at start -> failed, its legs closed; a placing leg untouched
    with case("stopped running at start") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        job = c.job_of(pid)["id"]
        c.broker().mark(job, "running")
        b = c.broker()
        b.close_stopped()
        r.eq("stopped: a running job at start -> failed 'runner stopped'", (c.job_of(pid)["status"], c.job_of(pid)["error"]),
             ("failed", "runner stopped"))
        r.eq("stopped: its legs closed", [(l["state"], l["finalized"]) for l in c.legs(pid)], [("skipped", 1)])
        r.eq("stopped: its proposal failed", c.prop(pid)["state"], "failed")
    with case("stopped running placing") as c:
        pid, env, fields = placing_leg(c)
        job = int(env["QUIVER_BROKER_JOB"])
        before = c.leg(fields["ref_id"])
        c.broker().close_stopped()
        after = c.leg(fields["ref_id"])
        r.eq("stopped: the job of a placing leg -> failed", c.q("SELECT status FROM broker_jobs WHERE id=?", (job,))[0]["status"], "failed")
        r.eq("stopped: a placing leg is not touched", (after["state"], after["submitted_at"], after["finalized"]),
             (before["state"], before["submitted_at"], before["finalized"]))
        r.eq("stopped: its proposal stays executing", c.prop(pid)["state"], "executing")


def placing_leg(c, tickers=("ANET",), *, spec=None):
    """A leg the guard moved to ``placing`` (reviewed and allowed), then a crash: no
    exec-commit. Returns (pid, env, fields)."""
    spec = spec or std()
    pid, env = c.ready(list(tickers))
    c.check(pid, "sells", c.write("s.json", c.snapfile(**spec)), env)
    out = c.check(pid, "buys", c.write("b.json", c.snapfile(**spec)), env)
    fields = out["legs"][0]
    c.clk.advance(1)
    broker_guard.record_review(review_payload(fields), env=env, now=c.clk.now)
    c.clk.advance(1)
    ok, why = broker_guard.evaluate("mcp__robinhood-trading__place_equity_order", fields, env=env, now=c.clk.now)
    assert ok, why
    return pid, env, fields


# =============================================================================================
# canary (§11.1 step 2)
# =============================================================================================

def _deny_dropped(events):
    out = copy.deepcopy(events)
    for e in out:
        for cpart in (e.get("message") or {}).get("content") or []:
            if isinstance(cpart, dict) and cpart.get("type") == "tool_result":
                cpart.pop("is_error", None)
    return out


def _second_call(events, *, allowed=False):
    """The captured Bash call twice: the second copy has new ids (values changed)."""
    twice = copy.deepcopy(events)
    for e in copy.deepcopy(events):
        for cpart in (e.get("message") or {}).get("content") or []:
            if isinstance(cpart, dict) and cpart.get("type") == "tool_use":
                cpart["id"] = cpart["id"] + "b"
            if isinstance(cpart, dict) and cpart.get("type") == "tool_result":
                cpart["tool_use_id"] = cpart["tool_use_id"] + "b"
                if allowed:
                    cpart["is_error"] = False
                    cpart["content"] = ""
        twice.append(e)
    return twice


def t_canary():
    g, f = _dump(CANARY_GUARD), _dump(CANARY_FALSE)
    bash = [x for x in run_broker.tool_calls(g) if x["name"] == "Bash"]
    r.eq("canary: the real run-1 stream holds one Bash tool_use", len(bash), 1)
    r.ok("canary: its tool_result is_error with the broker_guard reason",
         bash and bash[0]["is_error"] is True and "broker_guard" in bash[0]["text"], bash)
    r.eq("canary: run 1 (the real guard stream) passes", run_broker.canary_verdict(g, need_reason=True), (True, "ok"))
    r.eq("canary: run 2 (the real /bin/false stream) passes", run_broker.canary_verdict(f, need_reason=False), (True, "ok"))
    r.eq("canary: the /bin/false stream holds no guard reason", run_broker.canary_verdict(f, need_reason=True)[0], False)
    r.eq("canary: a tool_result without is_error fails",
         run_broker.canary_verdict(_dump(_deny_dropped(CANARY_GUARD)), need_reason=True)[0], False)
    r.eq("canary: run 2 without is_error fails", run_broker.canary_verdict(_dump(_deny_dropped(CANARY_FALSE)), need_reason=False)[0], False)
    r.eq("canary: two denied Bash calls pass", run_broker.canary_verdict(_dump(_second_call(CANARY_GUARD)), need_reason=True), (True, "ok"))
    r.eq("canary: one denied and one allowed call fail",
         run_broker.canary_verdict(_dump(_second_call(CANARY_GUARD, allowed=True)), need_reason=True)[0], False)
    no_bash = [e for e in CANARY_GUARD if e.get("type") not in ("assistant", "user")]
    r.eq("canary: no Bash tool_use fails", run_broker.canary_verdict(_dump(no_bash), need_reason=True)[0], False)

    # run_canary through the injected runner: the passed version is stored; a version change reruns it
    with case("canary store") as c:
        runner = Runner(c)
        ok, ver = run_broker.run_canary(c.led, "/x/claude", runner)
        r.eq("canary: run_canary passes on the real streams", (ok, ver), (True, "2.1.312 (Claude Code)"))
        with c.led.connect() as conn:
            r.eq("canary: the passed version is stored in bridge_state", store.get_state(conn, "canary_version"), "2.1.312 (Claude Code)")
        r.eq("canary: both runs use the canary prompt and --max-turns 3",
             [(x["argv"][-1], x["argv"][x["argv"].index("--max-turns") + 1]) for x in runner.calls if run_broker.CANARY_PROMPT in x["argv"]],
             [(run_broker.CANARY_PROMPT, "3")] * 2)
        r.eq("canary: run 1 settings.json, run 2 canary_fail.json",
             [Path(x["argv"][x["argv"].index("--settings") + 1]).name for x in runner.calls if run_broker.CANARY_PROMPT in x["argv"]],
             ["settings.json", "canary_fail.json"])
        r.ok("canary: the canary env has no job or proposal id",
             all("QUIVER_BROKER_PROPOSAL" not in x["env"] and "QUIVER_BROKER_JOB" not in x["env"] for x in runner.calls), None)
        r.eq("canary: not due on the same day and version", run_broker.canary_due(c.led, Runner(c)), False)
        r.eq("canary: due after a version change", run_broker.canary_due(c.led, Runner(c, version="2.1.313 (Claude Code)")), True)
        # the next execute job after a version change reruns it
        built = c.build(["ANET"])
        c.approve(built)
        runner2 = Runner(c, opts={"stop": "after_sells", "sells": std()}, version="2.1.313 (Claude Code)")
        c.broker(runner2).drain()
        r.eq("canary: a version change reruns the canary before the job", runner2.log[:3], ["canary", "canary", f"spawn:{c.job_of(built['proposal_id'])['id']}"])
        with c.led.connect() as conn:
            r.eq("canary: the new version is stored", store.get_state(conn, "canary_version"), "2.1.313 (Claude Code)")

    # a failed canary: every execute job fails with the guard text; a failed canary runs again
    with case("canary fail") as c:
        b1 = c.build(["ANET"])
        c.approve(b1)
        runner = Runner(c, opts={"sells": std(), "stop": "after_sells"},
                        canary=lambda which, n: _deny_dropped(CANARY_GUARD) if which == "guard" else CANARY_FALSE)
        c.broker(runner).drain()
        p1 = b1["proposal_id"]
        r.eq("canary fail: the execute job failed", (c.job_of(p1)["status"], c.prop(p1)["state"]), ("failed", "failed"))
        r.ok("canary fail: the owner text", run_broker.GUARD_FAIL_TEXT in c.tg.texts(), c.tg.texts())
        r.ok("canary fail: a broker page", ("error", "broker") in c.pages.kinds(), c.pages.kinds())
        r.eq("canary fail: no job spawned", runner.jobs, [])
        r.eq("canary fail: the failed version is not stored", c.q("SELECT value FROM bridge_state WHERE key='canary_version'"), [])

    with case("canary rerun") as c:
        p1 = c.build(["ANET"])
        c.approve(p1)
        # two execute jobs in one drain; the canary fails only on its first run
        c.clk.advance(1)
        runner = Runner(c, opts={"sells": std(), "stop": "after_sells"},
                        canary=lambda which, n: (_deny_dropped(CANARY_GUARD) if (which == "guard" and n == 1) else
                                                 (CANARY_GUARD if which == "guard" else CANARY_FALSE)))
        b = c.broker(runner)
        # queue a second execute job for a second proposal (a CEG trim needs CEG above target)
        c.snap(**std(positions={"ANET": 2, "CEG": 2, "SGOV": 11}))
        p2 = c.build(["CEG"])
        c.approve(p2)
        b.drain()
        r.eq("canary: the first job fails on the failed canary", c.prop(p1["proposal_id"])["state"], "failed")
        r.ok("canary: a failed canary runs again before the next execute job (§11.1 step 2)",
             runner.canary_calls >= 3 and any(x.startswith("spawn:") for x in runner.log), runner.log)

    # run_broker.py --canary --bin PATH: exit 0 on a pass, 1 on a fail (a real subprocess)
    for label, guard_fx, want in (("pass", "canary_guard.jsonl.gz", 0), ("fail", None, 1)):
        with case(f"canary cli {label}") as c:
            stream_dir = c.root.path / "canary"
            stream_dir.mkdir()
            gpath = stream_dir / "guard.jsonl"
            gpath.write_text(_dump(CANARY_GUARD if guard_fx else _deny_dropped(CANARY_GUARD)))
            (stream_dir / "false.jsonl").write_text(_dump(CANARY_FALSE))
            fake = stream_dir / "claude"
            fake.write_text("#!/bin/sh\n"
                            "case \"$*\" in\n"
                            "  --version) echo '2.1.312 (Claude Code)';;\n"
                            f"  *canary_fail.json*) cat '{stream_dir / 'false.jsonl'}';;\n"
                            f"  *) cat '{gpath}';;\n"
                            "esac\n")
            fake.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith("QUIVER_BROKER_")}
            env.update(c.root.env)
            p = subprocess.run([PY, str(REPO / "deploy" / "runner" / "run_broker.py"), "--canary", "--bin", str(fake)],
                               env=env, cwd=str(REPO), capture_output=True, text=True, timeout=120)
            r.eq(f"canary cli: --canary --bin exits {want} on a {label}", p.returncode, want)
            stored = c.q("SELECT value FROM bridge_state WHERE key='canary_version'")
            r.eq(f"canary cli: version stored only on a pass ({label})", bool(stored), want == 0)


# =============================================================================================
# audit (§11.1 step 9)
# =============================================================================================

def t_audit():
    with case("audit") as c:
        pid, env = c.ready(["ANET"])
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std())), env)
        fields = out["legs"][0]
        s = Stream()
        s.call("mcp__robinhood-trading__place_equity_order", fields, json.dumps(order_from(fields, oid="x1")), False)
        s.call("mcp__robinhood-trading__place_equity_order", {**fields, "ref_id": "denied-one"},
               "PreToolUse hook error: broker_guard: denied: x", True)
        b = c.broker()
        bad = b.audit(s.text())
        r.eq("audit: a place call with no guard allow row is found", bad, [fields["ref_id"]])
        r.eq("audit: day_baseline.halted", c.led.is_halted("2026-08-05"), True)
        r.eq("audit: one critical page (stage broker)", [(x["kind"], x["stage"], x.get("severity", "critical")) for x in c.pages.calls],
             [("error", "broker", "critical")])
        r.eq("audit: the page was sent once", len(c.pages.sent), 1)
    with case("audit allowed") as c:
        pid, env, fields = placing_leg(c)
        s = Stream()
        s.call("mcp__robinhood-trading__place_equity_order", fields, json.dumps(order_from(fields, oid="x1")), False)
        bad = c.broker().audit(s.text())
        r.eq("audit: a place call the guard allowed is clean", (bad, c.led.is_halted("2026-08-05"), c.pages.calls), ([], False, []))


# =============================================================================================
# requote_edge, requote, no_quote, fields
# =============================================================================================

def t_requote_edge():
    r.eq("requote_edge: (147.75/150-1)*100 is not exactly -1.5 in floats", (147.75 / 150 - 1) * 100 == -1.5, False)
    for px, want in ((152.25, "none"), (147.75, "none"), (152.26, "requote"), (147.74, "requote")):
        with case(f"requote_edge {px}") as c:
            pid, env = c.ready(["ANET"])
            out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(quotes={**STD_Q, "ANET": px}))), env)
            r.eq(f"requote_edge: 150.00 -> {px} -> {want}", out.get("action"), want)
            r.eq(f"requote_edge: {px} state", c.prop(pid)["state"], "executing" if want == "none" else "requoted")


def t_requote():
    with case("requote buy") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        old = c.prop(pid)
        run_job(c, pid, {"sells": std(quotes={**STD_Q, "ANET": 152.40})})
        r.eq("requote: proposal requoted", c.prop(pid)["state"], "requoted")
        r.eq("requote: legs finalized requoted", [(l["state"], l["finalized"]) for l in c.legs(pid)], [("requoted", 1)])
        r.eq("requote: nothing placed", c.rh.orders, {})
        new = c.q("SELECT * FROM proposals WHERE id != ?", (pid,))
        r.eq("requote: one new pending proposal", [p["state"] for p in new], ["pending"])
        if new:
            n = new[0]
            r.eq("requote: superseded_by", c.prop(pid)["superseded_by"], n["id"])
            r.eq("requote: same origin, owner message, rationale, evidence",
                 (n["origin"], n["owner_message_id"], n["rationale"], n["evidence_json"]),
                 (old["origin"], old["owner_message_id"], old["rationale"], old["evidence_json"]))
            nl = c.legs(n["id"])
            d = 2 * 152.40 + 300 + 1100 + 300
            want = round(min(0.25 * d - 2 * 152.40, min(295.0, 300 + 1100 - 0.60 * d)), 2)
            r.eq("requote (G29): the new leg is priced from the exec-check quote", nl[0]["quote_at_proposal"], 152.40)
            r.eq("requote (G29): and sized from that snapshot", nl[0]["dollar_amount"], want)
        r.ok("requote: the exact text (G29)",
             "ANET moved 1.6% since I proposed this ($150.00 to $152.40). Here are the new numbers." in c.tg.texts(), c.tg.texts())

    with case("requote flat sell", positions=POS_SB, quotes=Q_SB, equity=2150.0) as c:
        built = two_leg(c)
        pid = built["proposal_id"]
        c.approve(built)
        runner, _ = run_job(c, pid, {"sells": sb(quotes={**Q_SB, "CEG": 304.80})})
        r.eq("requote: a flat sell + a buy +1.6% -> the whole proposal requoted", c.prop(pid)["state"], "requoted")
        r.eq("requote: both legs finalized, nothing placed", ([(l["state"], l["finalized"]) for l in c.legs(pid)], c.rh.orders),
             ([("requoted", 1), ("requoted", 1)], {}))
        r.ok("requote: no place call in the stream", not any(x["name"].endswith("place_equity_order")
                                                              for x in run_broker.tool_calls(runner.last_bot.s.text())), None)

    with case("requote tolerance") as c:
        c.setting("requote_tolerance_pct", 0.25)
        pid, env = c.ready(["ANET"])
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(quotes={**STD_Q, "ANET": 150.45}))), env)
        r.eq("requote: requote_tolerance_pct 0.25 -> a 0.3% move requotes", (out.get("action"), c.prop(pid)["state"]),
             ("requote", "requoted"))


def t_no_quote():
    with case("no_quote") as c:
        pid, env = c.ready(["ANET"])
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**std(quotes={"CEG": 300.0, "SGOV": 100.0}))), env)
        r.eq("no_quote: a leg with no quote -> nothing placed, failed", (out.get("action"), out.get("legs"), c.prop(pid)["state"]),
             ("none", None, "failed"))
        r.eq("no_quote: text", json.loads(c.prop(pid)["result_json"]).get("reason_text"),
             "I could not get a fresh ANET price, so I placed nothing.")


def t_fields():
    with case("fields", positions=POS_SB, quotes=Q_SB, equity=2150.0) as c:
        pid, env = c.ready(["ANET", "CEG"])
        out = c.check(pid, "sells", c.write("s.json", c.snapfile(**sb())), env)
        legs = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("fields: a sell leg", out.get("legs"), [{
            "ref_id": legs["ANET"]["ref_id"], "account_number": "TEST1234", "symbol": "ANET", "side": "sell",
            "type": "market", "market_hours": "regular_hours", "time_in_force": "gfd",
            "quantity": f"{legs['ANET']['quantity']:.6f}"}])
        q = out["legs"][0]["quantity"]
        r.ok("fields: quantity has 6 decimals", len(q.split(".")[1]) == 6, q)
        c.check(pid, "buys", c.write("b.json", c.snapfile(**sb())), env)  # refused: the sell is not placed yet
    with case("fields buy") as c:
        pid, env = c.ready(["ANET"])
        c.check(pid, "sells", c.write("s.json", c.snapfile(**std())), env)
        out = c.check(pid, "buys", c.write("b.json", c.snapfile(**std())), env)
        r.eq("fields: a buy leg", out.get("legs"), [{
            "ref_id": c.legs(pid)[0]["ref_id"], "account_number": "TEST1234", "symbol": "ANET", "side": "buy",
            "type": "market", "market_hours": "regular_hours", "time_in_force": "gfd", "dollar_amount": "200.00"}])


# =============================================================================================
# buy_clamp (§11.2 step 5; G9)
# =============================================================================================

def t_buy_clamp():
    build_q = {**STD_Q, "ANET": 152.40}
    for label, bp, floor, want_kind in (("floor 0", 150.0, 0.0, "clamp"), ("floor null", 150.0, None, "clamp"),
                                        ("bp 500", 500.0, 0.0, "full"), ("bp 1e6", 1_000_000.0, 0.0, "full"),
                                        ("under min", 9.0, 0.0, "skip")):
        with case(f"buy_clamp {label}", quotes=build_q) as c:
            built = c.build(["ANET"])
            leg0 = c.legs(built["proposal_id"])[0]
            r.eq(f"buy_clamp {label}: the built leg is 196.40", leg0["dollar_amount"], 196.40)
            c.setting("cash_floor_pct", floor)
            c.clk.advance(10)
            c.approve(built)
            c.clk.advance(10)
            pid = built["proposal_id"]
            prep, env = c.start(pid)
            live = std(quotes={**STD_Q, "ANET": 152.80}, bp=bp)
            c.check(pid, "sells", c.write("s.json", c.snapfile(**live)), env)
            out = c.check(pid, "buys", c.write("b.json", c.snapfile(**live)), env)
            leg = c.legs(pid)[0]
            d = 2 * 152.80 + 300 + 1100 + bp
            room = 0.25 * d - 2 * 152.80
            fl = 0.0 if floor is not None else 60.0
            money = min(bp - 5, bp + 1100 - fl / 100.0 * d)
            want = round(min(196.40, room, money), 2)
            if want_kind == "clamp":
                exp = {"floor 0": 145.00, "floor null": 136.64}[label]
                r.eq(f"buy_clamp {label}: the plan number", want, exp)
                r.eq(f"buy_clamp {label}: clamped to {exp}", (leg["state"], leg["dollar_amount"], leg["clamped_from"]),
                     ("checked", exp, 196.40))
                r.eq(f"buy_clamp {label}: the returned field", [l["dollar_amount"] for l in out.get("legs", [])], [f"{exp:.2f}"])
            elif want_kind == "full":
                r.eq(f"buy_clamp {label}: 196.40, clamped_from NULL", (leg["state"], leg["dollar_amount"], leg["clamped_from"]),
                     ("checked", 196.40, None))
            else:
                r.eq(f"buy_clamp {label}: under min_trade_notional -> skipped", (leg["state"], out.get("action")), ("skipped", "none"))


# =============================================================================================
# input_path (G13)
# =============================================================================================

def t_input_path():
    with case("input_path") as c:
        pid, env = c.ready(["ANET"])
        link = c.root.path / "state" / "tmp" / "link.json"
        os.symlink("/etc/passwd", link)
        n0 = c.q("SELECT COUNT(*) n FROM broker_snapshots")[0]["n"]
        for args in (("exec-check", "--proposal", pid, "--phase", "sells"), ("exec-commit", "--proposal", pid),
                     ("fills",), ("snapshot-commit",)):
            for bad in ("/etc/passwd", "state/tmp/../../../../../etc/passwd", "state/tmp/link.json"):
                code, out, _ = c.cli(*args, "--input", bad, env=env)
                r.ok(f"input_path: {args[0]} --input {bad} -> exit 1", code == 1 and "state/tmp" in str((out or {}).get("error")), (code, out))
        r.eq("input_path: nothing read (no snapshot row, legs unchanged)",
             (c.q("SELECT COUNT(*) n FROM broker_snapshots")[0]["n"], [l["state"] for l in c.legs(pid)]), (n0, ["approved"]))
        ok, why = broker_guard.evaluate("Bash", {"command": ".venv/bin/python tick.py fills --input /etc/passwd.json"}, env=env)
        r.eq("input_path: the guard pattern denies --input outside state/tmp", ok, False)


# =============================================================================================
# commit (§11.2 step 3; G25)
# =============================================================================================

def t_commit():
    with case("commit placed") as c:
        pid, env, fields = placing_leg(c)
        place = order_from(fields, oid="6ada272c-0000-0000-0000-000000000001", state="queued")
        code, out = c.tick("exec-commit", "--proposal", pid, "--input", c.write("res.json", {"place_equity_order": [place]}), env=env)
        leg = c.leg(fields["ref_id"])
        r.eq("commit: rh_place -> placed + broker_order_id", (code, leg["state"], leg["broker_order_id"]),
             (0, "placed", "6ada272c-0000-0000-0000-000000000001"))
        rows = c.q("SELECT * FROM decisions WHERE run_id=?", (fields["ref_id"],))
        r.eq("commit: one decisions row per placed leg", len(rows), 1)
        if rows:
            proof = json.loads(rows[0]["proof_json"])
            r.eq("commit: decisions row fields (G25)", (rows[0]["ticker"], rows[0]["signal"], rows[0]["decision_price"],
                                                      rows[0]["rationale"], proof.get("proposal_id")),
                 ("ANET", "buy", leg["check_quote"], c.prop(pid)["rationale"], pid))
            r.ok("commit: proof_json has event_id and evidence", "event_id" in proof and "evidence" in proof, proof)
        code, out = c.tick("exec-commit", "--proposal", pid, "--input", c.write("res2.json", {"place_equity_order": [place]}), env=env)
        r.eq("commit: a second commit adds no decisions row", len(c.q("SELECT * FROM decisions WHERE run_id=?", (fields["ref_id"],))), 1)
    with case("commit envelope") as c:
        pid, env, fields = placing_leg(c)
        place = {"data": order_from(fields, oid="ord-env", state="confirmed"), "guide": ORDERS_LIVE["guide"]}
        c.tick("exec-commit", "--proposal", pid, "--input", c.write("res.json", {"place_equity_order": [place]}), env=env)
        r.eq("commit: an order in the MCP data envelope -> placed", (c.leg(fields["ref_id"])["state"], c.leg(fields["ref_id"])["broker_order_id"]),
             ("placed", "ord-env"))
    for label, make, want in (
            ("state rejected", lambda f: order_from(f, oid="ord-rej", state="rejected"), "broker_rejected"),
            ("state failed", lambda f: order_from(f, oid="ord-fail", state="failed"), "broker_rejected"),
            ("4xx naming a field", lambda f: [{"type": "text", "text": 'Error: 400 Bad Request: {"dollar_amount": '
                                                                       '["Ensure this value is at least 1.00."]}'}],
             "broker_rejected"),
            ("other error body", lambda f: [{"type": "text", "text": "Error: 503 Service Unavailable"}], "placing"),
            # gate B M3: a number in the text is never a status; only a leading 400/422 with a quoted field counts
            ("a transport error with a $4xx amount", lambda f: [{"type": "text", "text":
                "Error calling place_equity_order(symbol=ANET, dollar_amount=412.00): connection reset by peer"}],
             "placing"),
            ("an error object that echoes a $4xx amount", lambda f: {"error": "upstream timeout",
                                                                     "request": {"dollar_amount": "455.10"}}, "placing"),
            ("a 404 text that holds a field word", lambda f: [{"type": "text", "text":
                "Error: 404 page not found inside gateway"}], "placing"),
            ("a 408 that names a field", lambda f: [{"type": "text", "text":
                'Error: 408 Request Timeout: {"symbol": "ANET"}'}], "placing"),
            ("a 422 object that names a field", lambda f: {"status": 422, "detail": {"quantity": ["too small"]}},
             "broker_rejected"),
            ("an error object", lambda f: {"error": "the request timed out"}, "placing"),
            ("no result", None, "placing")):
        with case(f"commit {label}") as c:
            pid, env, fields = placing_leg(c)
            results = [make(fields)] if make else []
            code, out = c.tick("exec-commit", "--proposal", pid, "--input", c.write("res.json", {"place_equity_order": results}), env=env)
            leg = c.leg(fields["ref_id"])
            r.eq(f"commit: {label} -> {want}", (code, leg["state"]), (0, want))
            r.eq(f"commit: {label} -> no decisions row", len(c.q("SELECT * FROM decisions WHERE run_id=?", (fields["ref_id"],))), 0)
            if want == "placing":
                r.eq(f"commit: {label} keeps submitted_at and ref_id", (leg["submitted_at"] is not None, leg["ref_id"]),
                     (True, fields["ref_id"]))
    with case("commit env") as c:
        pid, env, fields = placing_leg(c)
        code, out, _ = c.cli("exec-commit", "--proposal", pid, "--input", c.write("res.json", {"place_equity_order": []}),
                             env={**env, "QUIVER_BROKER_PROPOSAL": str(pid + 7)})
        r.eq("commit cli: another proposal's commit -> exit 1", code, 1)


# =============================================================================================
# review_gate (§11.3 --record-review; G27)
# =============================================================================================

def _ceg_5(c):
    """A CEG buy leg checked at exactly $5.00 (the captured rh_review_buy tool_input)."""
    c.setting("cash_floor_pct", 0.0)
    pid, env = c.ready(["CEG"])
    spec = std(positions={"ANET": 2, "SGOV": 11}, bp=10.0)
    c.check(pid, "sells", c.write("s.json", c.snapfile(**spec)), env)
    out = c.check(pid, "buys", c.write("b.json", c.snapfile(**spec)), env)
    return pid, env, out["legs"][0]


def _anet_001(c):
    """An ANET sell leg checked at exactly 0.01 sh (the captured rh_review_sell tool_input)."""
    pid, env = c.ready(["ANET"])
    spec = std(positions={"ANET": 2, "CEG": 1, "SGOV": 11}, quotes={**STD_Q, "ANET": 500.0})
    out = c.check(pid, "sells", c.write("s.json", c.snapfile(**spec, mutate=set_avail("ANET", 0.01))), env)
    return pid, env, out["legs"][0]


def t_review_gate():
    BUY_SNAP = dict(positions={"ANET": 2, "SGOV": 11})
    with case("review buy", **BUY_SNAP) as c:
        pid, env, f = _ceg_5(c)
        r.eq("review: the CEG leg is checked at the captured 5.00", (f["symbol"], f["side"], f["dollar_amount"]), ("CEG", "buy", "5.00"))
        c.clk.advance(1)
        ok, why = broker_guard.record_review(REVIEW_BUY, env=env, now=c.clk.now)
        leg = c.leg(f["ref_id"])
        r.eq("review: the captured rh_review_buy -> review_ok=1 for its own leg", (ok, leg["review_ok"]), (1, 1))
        r.ok("review: reviewed_at written after checked_at", leg["reviewed_at"] > leg["checked_at"], leg)
        variants = [
            ("customer_approval_required absent", dict(drop_data_keys=("customer_approval_required",)), 1),
            ("customer_approval_required false", dict(cust=False), 1),
            ("order_checks []", dict(checks=[]), 1),
            ("the string form", dict(form="string"), 1),
            ("the content-block form", dict(form="blocks"), 1),
            ("customer_approval_required true in data", dict(cust=True), 0),
            ("customer_approval_required true at the top level", dict(top_cust=True), 0),
            ("no data", dict(drop_data=True), 0),
            ("no order_checks", dict(drop_data_keys=("order_checks",)), 0),
            ('order_checks {"x": "y"}', dict(checks={"x": "y"}), 0),
        ]
        for label, kw, want in variants:
            c.clk.advance(1)
            payload = review_payload(f, captured=REVIEW_BUY, **kw)
            broker_guard.record_review(payload, env=env, now=c.clk.now)
            r.eq(f"review: {label} -> review_ok={want}", c.leg(f["ref_id"])["review_ok"], want)
        # the place gate: review_ok=0 -> deny; a clean review -> allow
        ok, why = broker_guard.evaluate("mcp__robinhood-trading__place_equity_order", f, env=env, now=c.clk.now)
        r.eq("review: review_ok=0 -> the place is denied", ok, False)
        c.clk.advance(1)
        broker_guard.record_review(REVIEW_BUY, env=env, now=c.clk.now)
        ok, why = broker_guard.evaluate("mcp__robinhood-trading__place_equity_order", f, env=env, now=c.clk.now)
        r.eq("review: a clean review -> the place is allowed", (ok, c.leg(f["ref_id"])["state"]), (True, "placing"))
    with case("review sell", positions={"ANET": 2, "CEG": 1, "SGOV": 11}, quotes={**STD_Q, "ANET": 500.0}) as c:
        pid, env, f = _anet_001(c)
        r.eq("review: the ANET leg is checked at the captured 0.01", (f["symbol"], f["side"], f["quantity"]), ("ANET", "sell", "0.010000"))
        c.clk.advance(1)
        ok, why = broker_guard.record_review(REVIEW_BUY, env=env, now=c.clk.now)
        r.eq("review: the buy answer never matches a quantity leg", (ok, c.leg(f["ref_id"])["review_ok"]), (0, 0))
        dollar_sell = copy.deepcopy(REVIEW_BUY)
        dollar_sell["tool_input"].update(symbol="ANET", side="sell")
        ok, why = broker_guard.record_review(dollar_sell, env=env, now=c.clk.now)
        r.eq("review: a dollar_amount review never matches a sell leg", (ok, c.leg(f["ref_id"])["review_ok"]), (0, 0))
        ok, why = broker_guard.record_review(REVIEW_SELL, env=env, now=c.clk.now)
        r.eq("review: the captured rh_review_sell -> review_ok=1 for its own leg", (ok, c.leg(f["ref_id"])["review_ok"]), (1, 1))
        # a crash of the record hook -> review_ok=0 (a real hook process)
        c.clk.advance(5)
        cfg_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "QUIVER_ROOT": str(c.root.path),
                   "QUIVER_BROKER_PROPOSAL": str(pid), "QUIVER_BROKER_JOB": env["QUIVER_BROKER_JOB"]}
        p = subprocess.run([PY, str(REPO / "deploy" / "runner" / "broker_guard.py"), "--record-review"], input="{not json",
                           env=cfg_env, capture_output=True, text=True, timeout=60)
        r.eq("review: a crash of the record hook exits 2 (blocked)", p.returncode, 2)
    with case("review crash fresh", positions={"ANET": 2, "CEG": 1, "SGOV": 11}, quotes={**STD_Q, "ANET": 500.0}) as c:
        pid, env, f = _anet_001(c)
        cfg_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "QUIVER_ROOT": str(c.root.path / "nowhere"),
                   "QUIVER_BROKER_PROPOSAL": str(pid), "QUIVER_BROKER_JOB": env["QUIVER_BROKER_JOB"]}
        p = subprocess.run([PY, str(REPO / "deploy" / "runner" / "broker_guard.py"), "--record-review"],
                           input=json.dumps(REVIEW_SELL), env=cfg_env, capture_output=True, text=True, timeout=60)
        r.eq("review: a record hook that cannot reach the ledger crashes -> exit 2", p.returncode, 2)
        r.eq("review: after the crash review_ok stays 0", c.leg(f["ref_id"])["review_ok"], 0)
        ok, why = broker_guard.evaluate("mcp__robinhood-trading__place_equity_order", f, env=env, now=c.clk.now)
        r.eq("review: after the crash the place is denied", ok, False)
    # a review older than checked_at -> the place is denied
    with case("review old", positions={"ANET": 2, "CEG": 1, "SGOV": 11}, quotes={**STD_Q, "ANET": 500.0}) as c:
        pid, env, f = _anet_001(c)
        checked = store.parse_ts(c.leg(f["ref_id"])["checked_at"])
        broker_guard.record_review(REVIEW_SELL, env=env, now=checked - timedelta(seconds=1))
        c.clk.advance(2)
        ok, why = broker_guard.evaluate("mcp__robinhood-trading__place_equity_order", f, env=env, now=c.clk.now)
        r.eq("review: review_ok=1 but older than checked_at", c.leg(f["ref_id"])["review_ok"], 1)
        r.ok("review: a review older than checked_at -> the place is denied", ok is False and "older" in why, why)
    # true -> place denied; at job close skipped needs_app_approval with the text; order_checks -> review_alert
    for label, kw, reason, text in (
            ("app approval", dict(cust=True), "needs_app_approval",
             "Robinhood wants the CEG order approved in its app, so I did not send it."),
            ("order_checks alert", dict(checks={"x": "y"}), "review_alert", None)):
        with case(f"review close {label}", **BUY_SNAP) as c:
            c.setting("cash_floor_pct", 0.0)
            built = c.build(["CEG"])
            pid = built["proposal_id"]
            c.approve(built)
            spec = std(positions={"ANET": 2, "SGOV": 11}, bp=10.0)
            runner, _ = run_job(c, pid, {"sells": spec, "buys": spec,
                                         "review": lambda f, kw=kw: review_payload(f, captured=REVIEW_BUY, **kw)})
            leg = c.legs(pid)[0]
            r.eq(f"review close {label}: the place was denied", [d[0] for d in runner.last_bot.denied], [leg["ref_id"]])
            r.eq(f"review close {label}: at job close skipped {reason}", (leg["state"], leg["skip_reason"], leg["finalized"]),
                 ("skipped", reason, 1))
            r.eq(f"review close {label}: proposal failed", c.prop(pid)["state"], "failed")
            if text:
                r.ok(f"review close {label}: the owner text", any(text in t for t in c.tg.texts()), c.tg.texts())
            r.eq(f"review close {label}: the audit stays quiet", c.led.is_halted("2026-08-05"), False)


# =============================================================================================
# fills (§11.2 steps 4 and 6; §11.1 step 9)
# =============================================================================================

POS_C = {"SMH": 4, "SGOV": 11}
Q_C = {"SMH": 301.10, "CEG": 257.05, "SGOV": 100.0}


def t_fills():
    # the exact result text: Sold 1.603 SMH at 301.10, Bought $410.00 of CEG: 1.595 at 257.05
    with case("fills text", book=BOOK_C, positions=POS_C, quotes=Q_C, equity=2604.40) as c:
        c.setting("cash_floor_pct", 0.0)
        built = c.build(["SMH", "CEG"])
        pid = built["proposal_id"]
        c.approve(built)
        c.rh.script = {"SMH": ["filled"], "CEG": ["filled"]}
        c.rh.fill = {"SMH": (1.603, 301.10), "CEG": (1.595, 257.05)}
        base = {"quotes": Q_C, "equity": 2604.40}
        run_job(c, pid, {"sells": {**base, "positions": POS_C, "mutate": set_avail("SMH", 1.603)},
                         "buys": {**base, "positions": {"SMH": 4 - 1.603, "SGOV": 11}, "bp": 415.0},
                         "after": {**base, "positions": {"SMH": 4 - 1.603, "CEG": 1.595, "SGOV": 11}, "bp": 5.0}})
        legs = {l["ticker"]: l for l in c.legs(pid)}
        r.eq("fills: the sell sized from the free 1.603", legs["SMH"]["quantity"], 1.603)
        r.eq("fills: the buy clamped to 410.00", legs["CEG"]["dollar_amount"], 410.00)
        r.eq("fills: FILLED -> filled (both legs)", (legs["SMH"]["state"], legs["CEG"]["state"]), ("filled", "filled"))
        r.eq("fills: all filled -> executed", c.prop(pid)["state"], "executed")
        want = "Done. Sold 1.603 shares of SMH at $301.10 ($482.66). Bought $410.00 of CEG: 1.595 shares at $257.05."
        r.ok("fills: the result text", want in c.tg.texts(), c.tg.texts())
        r.eq("fills: the result goes through outbox (source execution)",
             c.q("SELECT COUNT(*) n FROM chat_messages WHERE source='execution' AND text=?", (want,))[0]["n"], 1)

    # FILLED / PARTIAL through tick.py fills (the CLI shape {"get_equity_orders": raw})
    with case("fills states") as c:
        pid, env, fields = placing_leg(c)
        c.tick("exec-commit", "--proposal", pid, "--input",
               c.write("res.json", {"place_equity_order": [order_from(fields, oid="P1")]}), env=env)
        part = dict(copy.deepcopy(PARTIAL), id="P1", symbol="ANET")
        code, out, _ = c.cli("fills", "--input", c.write("f.json", {"get_equity_orders": orders_answer([part])}))
        leg = c.leg(fields["ref_id"])
        r.eq("fills: PARTIAL -> partially_filled", (code, leg["state"], leg["fill_quantity"]), (0, "partially_filled", 1.0))
        with c.led.connect() as conn:
            r.eq("fills: a partial keeps the proposal open", proposals.close_from_legs(c.led, pid, c.clk.now) != "" and
                 c.prop(pid)["state"] == "executing", True)
        full = dict(copy.deepcopy(FILLED), id="P1", symbol="ANET")
        code, out, _ = c.cli("fills", "--input", c.write("f2.json", {"get_equity_orders": orders_answer([full])}))
        leg = c.leg(fields["ref_id"])
        r.eq("fills: FILLED -> filled", (code, leg["state"], leg["fill_avg_price"]), (0, "filled", 27.9499))
        r.eq("fills cli: the window comes from the rows", out.get("window"), "2026-08-04")
        proposals.close_from_legs(c.led, pid, c.clk.now)
        r.eq("fills: then executed", c.prop(pid)["state"], "executed")

    # mixed -> partially_executed; none -> failed
    for label, states, want in (("mixed", {"SMH": "filled", "CEG": "cancelled"}, "partially_executed"),
                                ("none", {"SMH": "cancelled", "CEG": "cancelled"}, "failed")):
        with case(f"fills {label}", book=BOOK_C, positions=POS_C, quotes=Q_C, equity=2604.40) as c:
            c.setting("cash_floor_pct", 0.0)
            built = c.build(["SMH", "CEG"])
            pid = built["proposal_id"]
            c.approve(built)
            c.rh.script = {k: [v] for k, v in states.items()}
            c.rh.fill = {"SMH": (1.603, 301.10), "CEG": (1.595, 257.05)}
            base = {"quotes": Q_C, "equity": 2604.40}
            run_job(c, pid, {"sells": {**base, "positions": POS_C, "mutate": set_avail("SMH", 1.603)},
                             "buys": {**base, "positions": {"SMH": 4 - 1.603, "SGOV": 11}, "bp": 415.0},
                             "after": {**base, "positions": POS_C, "bp": 300.0}})
            r.eq(f"fills: {label} -> {want}", c.prop(pid)["state"], want)

    # queued after the third call: executing, the ticker blocked, the open leg named; the fill at the next snapshot-commit
    with case("fills queued") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        c.rh.script = {"ANET": ["queued"]}
        runner, _ = run_job(c, pid, {"sells": std(), "after": std(bp=100.0)})
        gets = [x for x in run_broker.tool_calls(runner.last_bot.s.text()) if x["name"].endswith("get_equity_orders")]
        r.eq("fills queued: three get_equity_orders calls", len(gets), 3)
        r.eq("fills queued: two waits of 20 s", c.clk.sleeps, [20, 20])
        r.eq("fills queued: the leg stays placed", c.legs(pid)[0]["state"], "placed")
        r.eq("fills queued: the proposal stays executing", c.prop(pid)["state"], "executing")
        try:
            c.build(["ANET"])
            r.ok("fills queued: build_trade on that ticker is refused", False, "built")
        except proposals.ProposalError as e:
            r.ok("fills queued: build_trade on that ticker is refused", "in flight" in str(e), str(e))
        r.ok("fills queued: the message names the open leg (§11.1 step 9 text)",
             any("The ANET order is open at Robinhood (queued). I'll tell you when it fills." in t for t in c.tg.texts()),
             c.tg.texts())
        n_before = len(c.tg.texts())
        c.rh.script = {"ANET": ["filled"]}
        c.rh.fill = {"ANET": (1.333333, 150.0)}
        c.clk.advance(600)
        with c.led.connect() as conn:
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
        run_job(c, pid, {"snap": std(positions={"ANET": 3.333333, "CEG": 1, "SGOV": 11}, bp=100.0)})
        r.eq("fills queued: the fill at the next snapshot-commit -> executed", c.prop(pid)["state"], "executed")
        new = c.tg.texts()[n_before:]
        r.eq("fills queued: one result message", [t for t in new if t.startswith("Done.")],
             ["Done. Bought $200.00 of ANET: 1.333 shares at $150.00."])

    # the open-leg text without step 7 (so the close of run_broker reports it)
    with case("fills queued text") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        c.rh.script = {"ANET": ["queued"]}
        run_job(c, pid, {"sells": std()})
        opened = [t for t in c.tg.texts() if "open at Robinhood" in t]
        r.eq("fills queued text: run_broker names the open leg once", len(opened), 1)
        r.ok("fills queued text: the §11.1 step 9 text names the broker state",
             any("The ANET order is open at Robinhood (queued). I'll tell you when it fills." in t for t in opened), opened)

    # a sells-only proposal whose sell is FILLED at the second call -> executed in the same job
    with case("fills sells only", positions={"ANET": 5, "CEG": 1, "SGOV": 11}) as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        q = c.legs(pid)[0]["quantity"]
        c.approve(built)
        c.rh.script = {"ANET": ["queued", "filled"]}
        c.rh.fill = {"ANET": (q, 150.0)}
        runner, _ = run_job(c, pid, {"sells": std(positions={"ANET": 5, "CEG": 1, "SGOV": 11}),
                                     "after": std(positions={"ANET": 5 - q, "CEG": 1, "SGOV": 11}, bp=300 + q * 150)})
        gets = [x for x in run_broker.tool_calls(runner.last_bot.s.text()) if x["name"].endswith("get_equity_orders")]
        r.eq("fills sells only: filled at the second call", len(gets), 2)
        r.eq("fills sells only: executed in the same job", c.prop(pid)["state"], "executed")
        r.ok("fills sells only: one result message", sum(t.startswith("Done. Sold") for t in c.tg.texts()) == 1, c.tg.texts())


# =============================================================================================
# recovery (§11.1 step 4; G8)
# =============================================================================================

def _lost(c, **kw):
    """A leg left in ``placing`` by a crash, then a new runner start: close_stopped and
    recover_lost. Returns (pid, fields, broker)."""
    pid, env, fields = placing_leg(c, **kw)
    c.clk.advance(30)
    b = c.broker(Runner(c))
    b.close_stopped()
    b.recover_lost()
    return pid, fields, b


def t_recovery():
    with case("recovery notice") as c:
        pid, fields, b = _lost(c)
        sub = c.leg(fields["ref_id"])["submitted_at"]
        r.eq("recovery: the leg is placing with submitted_at", (c.leg(fields["ref_id"])["state"], sub is not None), ("placing", True))
        want = f"I lost track of the ANET order at {market.fmt_et(store.parse_ts(sub))}. I'm checking it with Robinhood."
        r.eq("recovery: the owner gets the lost-track message at once", [t for t in c.tg.texts() if t.startswith("I lost track")], [want])
        jobs = c.q("SELECT * FROM broker_jobs WHERE proposal_id=? AND recovery=1", (pid,))
        r.eq("recovery: one recovery execute job", [(j["kind"], j["status"]) for j in jobs], [("execute", "queued")])
        b.recover_lost()
        r.eq("recovery: the message goes once per leg", len([t for t in c.tg.texts() if t.startswith("I lost track")]), 1)
        r.eq("recovery: no second recovery job", len(c.q("SELECT * FROM broker_jobs WHERE proposal_id=? AND recovery=1", (pid,))), 1)
        r.eq("recovery: the stopped job did not fail the proposal", c.prop(pid)["state"], "executing")
        # --recovery skips every gate: KILL, halt, after the close, expired
        c.root.kill.write_text("x")
        c.led.mark_halted("2026-08-05", "test")
        c.at("2026-08-06T01:00:00Z")
        c.broker().mark(jobs[0]["id"], "running")
        code, prep = c.tick("exec-prepare", "--proposal", pid, "--recovery")
        r.eq("recovery: --recovery skips the expiry, session, KILL and halt checks", (prep.get("ok"), prep.get("recovery")), (True, True))
        r.eq("recovery: it returns the placing legs", [l["ref_id"] for l in prep.get("legs", [])], [fields["ref_id"]])
        sub_dt = store.parse_ts(sub)
        r.eq("recovery: created_at_gte = submitted_at - 2 min", prep.get("created_at_gte"), store.iso(sub_dt - timedelta(minutes=2)))

    def rec_run(c, pid, recover_spec, orders, **opts):
        return run_job(c, pid, {"recover": recover_spec, "orders": orders, "after": opts.pop("after", None), **opts})

    # a match by ref_id: the one case whose broker order carries the leg's own ref_id
    with case("recovery match ref") as c:
        pid, fields, b = _lost(c)
        refs0 = sorted(l["ref_id"] for l in c.legs(pid))
        o = order_from(fields, oid="M1", state="filled", qty=1.333333, avg=150.0, ref_id=fields["ref_id"],
                       created_at=c.leg(fields["ref_id"])["submitted_at"].replace("Z", ".100000Z"))
        rec_run(c, pid, std(), [o])
        leg = c.leg(fields["ref_id"])
        r.eq("recovery: a match by ref_id is recorded", (leg["broker_order_id"], leg["state"]), ("M1", "filled"))
        r.eq("recovery: then executed", c.prop(pid)["state"], "executed")
        r.eq("recovery: the ref_id set never changes", sorted(l["ref_id"] for l in c.legs(pid)), refs0)
    # a match by symbol, side, agent_initiated, amount and time (the order carries Robinhood's own ref_id)
    with case("recovery match fields") as c:
        pid, fields, b = _lost(c)
        sub = store.parse_ts(c.leg(fields["ref_id"])["submitted_at"])
        o = order_from(fields, oid="M2", state="queued", created_at=store.iso(sub - timedelta(seconds=60)))
        r.ok("recovery: the field-match order has a ref_id that is not the leg's", o["ref_id"] != fields["ref_id"], o["ref_id"])
        rec_run(c, pid, std(), [o])
        r.eq("recovery: a field match is recorded", c.leg(fields["ref_id"])["broker_order_id"], "M2")
        o_old = order_from(fields, oid="M3", state="queued", created_at=store.iso(sub - timedelta(seconds=121)))
        o_amt = dict(order_from(fields, oid="M4", state="queued", created_at=store.iso(sub)))
        o_amt["dollar_based_amount"]["amount"] = "199.000000"
        o_human = order_from(fields, oid="M5", state="queued", created_at=store.iso(sub))
        o_human["agent_initiated"] = False
        for lbl, cand in (("created 121 s before submitted_at", o_old), ("another amount", o_amt), ("not agent_initiated", o_human)):
            r.eq(f"recovery: no match for an order {lbl}", execution.match_order(c.leg(fields["ref_id"]) | {"state": "placing"},
                                                                                 [cand], set()), None)
    # a broker order id that another leg holds is skipped
    with case("recovery taken") as c:
        p0, env0, f0 = placing_leg(c)
        held = order_from(f0, oid="HELD", state="filled", qty=1.333333, avg=150.0)
        c.tick("exec-commit", "--proposal", p0, "--input", c.write("res0.json", {"place_equity_order": [held]}), env=env0)
        c.tick("fills", "--input", c.write("f0.json", {"get_equity_orders": orders_answer([held])}))
        proposals.close_from_legs(c.led, p0, c.clk.now)
        r.eq("recovery taken: the first leg holds HELD", (c.leg(f0["ref_id"])["broker_order_id"], c.prop(p0)["state"]),
             ("HELD", "executed"))
        c.clk.advance(60)
        c.snap()
        pid, fields, b = _lost(c)
        o = copy.deepcopy(held)
        o["created_at"] = c.leg(fields["ref_id"])["submitted_at"]
        rec_run(c, pid, std(), [o])
        leg = c.leg(fields["ref_id"])
        r.ok("recovery: a broker order id held by another leg is skipped", leg["broker_order_id"] != "HELD", leg)
        r.eq("recovery taken: HELD stays on the first leg only",
             c.q("SELECT COUNT(*) n FROM orders WHERE broker_order_id='HELD'")[0]["n"], 1)

    # no match + re-send allowed, quote within tolerance -> re-sent with the same ref_id
    with case("recovery resend") as c:
        pid, fields, b = _lost(c)
        sub = c.leg(fields["ref_id"])["submitted_at"]
        c.rh.script = {"ANET": ["queued"]}
        runner, _ = rec_run(c, pid, std(), [], after=std())
        leg = c.leg(fields["ref_id"])
        bot = runner.last_bot
        places = [x for x in run_broker.tool_calls(bot.s.text()) if x["name"].endswith("place_equity_order") and not x["is_error"]]
        r.eq("recovery resend: re-sent once with the same ref_id", [p["input"]["ref_id"] for p in places], [fields["ref_id"]])
        r.ok("recovery resend: a new checked_at after submitted_at", leg["checked_at"] > sub, (leg["checked_at"], sub))
        r.eq("recovery resend: submitted_at kept (the first allow only)", leg["submitted_at"], sub)
        # gate B M2: a matched order restarts the miss count, so the placed phase counts its own misses.
        r.eq("recovery resend: reconcile_attempts back to 0 once the re-sent order matches", leg["reconcile_attempts"], 0)
        r.eq("recovery resend: placed after the re-send", leg["state"], "placed")
        r.eq("recovery resend: one leg, one ref_id", [l["ref_id"] for l in c.legs(pid)], [fields["ref_id"]])
    # the recover phase keeps the leg placing with a new checked_at (the in-process check)
    with case("recovery recover phase") as c:
        pid, fields, b = _lost(c)
        job = c.q("SELECT id FROM broker_jobs WHERE proposal_id=? AND recovery=1", (pid,))[0]["id"]
        b.mark(job, "running")
        c.tick("exec-prepare", "--proposal", pid, "--recovery")
        env = {"QUIVER_BROKER_JOB": str(job), "QUIVER_BROKER_PROPOSAL": str(pid)}
        out = c.check(pid, "recover", c.write("rc.json", c.snapfile(**std(), orders=[])), env)
        leg = c.leg(fields["ref_id"])
        r.eq("recovery: --phase recover returns the leg with its ref_id", [l["ref_id"] for l in out.get("legs", [])], [fields["ref_id"]])
        r.eq("recovery: the leg stays placing, review_ok=0", (leg["state"], leg["review_ok"], leg["checked_at"]), ("placing", 0, c.now()))
        ok, why = broker_guard.evaluate("mcp__robinhood-trading__place_equity_order", out["legs"][0],
                                        env={**env, "RH_ACCOUNT_NUMBER": "TEST1234"}, now=c.clk.now)
        r.eq("recovery: the re-send needs a new review", ok, False)
        r.eq("recovery: --phase recover runs once", c.check(pid, "recover", c.write("rc2.json", c.snapfile(**std(), orders=[])), env).get("action"),
             "refused")
    # quote moved more than the tolerance -> outcome_unknown, not re-sent
    with case("recovery moved") as c:
        pid, fields, b = _lost(c)
        runner, _ = rec_run(c, pid, std(quotes={**STD_Q, "ANET": 153.0}), [])
        leg = c.leg(fields["ref_id"])
        r.eq("recovery moved: outcome_unknown, not re-sent", (leg["state"], c.rh.orders), ("outcome_unknown", {}))
        r.ok("recovery moved: the price-moved text", any("I can't confirm the ANET order, and the price moved, so I did not send it again. "
                                                         "Check the Robinhood app." in t for t in c.tg.texts()), c.tg.texts())
        r.eq("recovery moved: the proposal ends failed (no fill)", c.prop(pid)["state"], "failed")
    # no match after the close -> 1, then 2 -> outcome_unknown + the text; never failed while placing
    with case("recovery after close") as c:
        pid, fields, b = _lost(c)
        c.at("2026-08-05T20:01:00Z")
        rec_run(c, pid, std(), [])
        leg = c.leg(fields["ref_id"])
        r.eq("recovery after close: no re-send, reconcile_attempts 1", (leg["state"], leg["reconcile_attempts"], c.rh.orders),
             ("placing", 1, {}))
        r.eq("recovery after close: a proposal with a placing leg is never failed", c.prop(pid)["state"], "executing")
        c.clk.advance(900)
        with c.led.connect() as conn:
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
        run_job(c, pid, {"snap": std(), "orders": []})
        leg = c.leg(fields["ref_id"])
        r.eq("recovery after close: attempt 2 -> outcome_unknown", (leg["state"], leg["reconcile_attempts"]), ("outcome_unknown", 2))
        r.ok("recovery after close: the owner gets 'I can't confirm the ANET order. Check the Robinhood app.'",
             any("I can't confirm the ANET order. Check the Robinhood app." in t for t in c.tg.texts()), c.tg.texts())
    # no match with approvals on -> no re-send, placing, attempts +1, executing
    with case("recovery approvals on") as c:
        pid, fields, b = _lost(c)
        rec_run(c, pid, std(approval=True), [])
        leg = c.leg(fields["ref_id"])
        r.eq("recovery approvals on: no re-send, the leg stays placing, attempts 1",
             (leg["state"], leg["reconcile_attempts"], c.rh.orders), ("placing", 1, {}))
        r.eq("recovery approvals on: the proposal stays executing", c.prop(pid)["state"], "executing")
    # a re-send whose result is an error body -> never sent again; the next reconcile -> outcome_unknown
    with case("recovery resend error") as c:
        pid, fields, b = _lost(c)
        c.rh.place_answer = {"ANET": lambda f: [{"type": "text", "text": "Error: 503 Service Unavailable"}]}
        runner, _ = rec_run(c, pid, std(), [])
        leg = c.leg(fields["ref_id"])
        r.eq("recovery resend error: the leg stays placing after the error body", (leg["state"], leg["reconcile_attempts"]), ("placing", 1))
        c.clk.advance(120)
        b2 = c.broker(Runner(c))
        b2.recover_lost()
        r.eq("recovery resend error: no second lost-track message", len([t for t in c.tg.texts() if t.startswith("I lost track")]), 1)
        runner2 = Runner(c, opts={"recover": std(), "orders": []})
        c.broker(runner2).drain()
        leg = c.leg(fields["ref_id"])
        places = [x for x in run_broker.tool_calls(runner2.last_bot.s.text()) if x["name"].endswith("place_equity_order")]
        r.eq("recovery resend error: never sent again", places, [])
        r.eq("recovery resend error: the next reconcile with no match -> outcome_unknown", (leg["state"], leg["reconcile_attempts"]),
             ("outcome_unknown", 2))
    # a failed self-test in a recovery job never fails a proposal with a placing leg
    with case("recovery selftest") as c:
        pid, fields, b = _lost(c)
        rc.VENV_PY = "/usr/bin/true"
        c.broker(Runner(c)).drain()
        r.eq("recovery: a failed check never fails a proposal with a placing leg",
             (c.prop(pid)["state"], c.leg(fields["ref_id"])["state"]), ("executing", "placing"))


# =============================================================================================
# snapshot (§11.4)
# =============================================================================================

def t_snapshot():
    with case("snapshot baseline", snap=False) as c:
        c.at("2026-08-05T13:31:00Z")
        with c.led.connect() as conn:
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
        run_job(c, None, {"snap": std(equity=2000.0)})
        r.eq("snapshot: the day's first snapshot sets day_baseline", c.led.get_baseline("2026-08-05").baseline_equity, 2000.0)
        c.clk.advance(600)
        with c.led.connect() as conn:
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
        run_job(c, None, {"snap": std(equity=1700.0)})
        r.eq("snapshot: a later snapshot does not move the baseline", c.led.get_baseline("2026-08-05").baseline_equity, 2000.0)
        r.eq("snapshot: a 15% drop does not halt", c.led.is_halted("2026-08-05"), False)
    for eq, halts in ((1600.0, True), (1600.20, False)):
        with case(f"snapshot drop {eq}") as c:
            for i in range(2):
                c.clk.advance(600)
                with c.led.connect() as conn:
                    store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
                run_job(c, None, {"snap": std(equity=eq)})
            pct = round((2000 - eq) / 2000 * 100, 2)
            r.eq(f"snapshot: a drop of {pct}% halts={halts}", c.led.is_halted("2026-08-05"), halts)
            r.eq(f"snapshot: {pct}% pages {'once' if halts else 'never'}", len([k for k in c.pages.kinds() if k[0] == "halt"]),
                 1 if halts else 0)
            r.eq(f"snapshot: {pct}%: no KILL written", c.root.kill.exists(), False)
            if halts:
                r.eq("snapshot: one daily_halt line", len([x for x in tick_log(c) if x.get("event") == "daily_halt"]), 1)
                try:
                    c.build(["ANET"])
                    r.ok("snapshot: the halt blocks new trades today", False, "built")
                except proposals.ProposalError as e:
                    r.ok("snapshot: the halt blocks new trades today", "halted" in str(e), str(e))
                c.at("2026-08-06T13:35:00Z")
                c.snap(equity=1600.0)
                r.eq("snapshot: the next baseline ends the halt", (c.led.is_halted("2026-08-06"),
                                                                    c.led.get_baseline("2026-08-06").baseline_equity), (False, 1600.0))
    # the real CLI: snapshot-commit and snapshot-prepare shapes
    with case("snapshot cli", snap=False) as c:
        with c.led.connect() as conn:
            store.upsert_company(conn, ticker="DLR", kind="company", status="watch", now=c.now())
        data = c.snapfile(**std(positions={"ANET": 2, "CEG": 1, "SGOV": 11, "QQQ": 1},
                                quotes={**STD_Q, "QQQ": 700.0}))
        code, out, err = c.cli("snapshot-commit", "--input", c.write("snap.json", data))
        r.eq("snapshot cli: snapshot-commit exit 0 + a row", (code, out.get("ok"), out.get("approval_setting")), (0, True, "off"))
        code, out, err = c.cli("snapshot-prepare")
        r.eq("snapshot cli: snapshot-prepare exit 0", code, 0)
        r.ok("snapshot-prepare: tickers = the book, held names off the book, the watch names",
             {"ANET", "CEG", "SGOV", "QQQ", "DLR"} <= set(out.get("tickers") or []), out)
        r.eq("snapshot-prepare: no leg to reconcile", out.get("reconcile"), [])
        r.ok("snapshot-prepare: a fill window", bool(out.get("fills_created_at_gte")), out)
    with case("snapshot prepare legs") as c:
        pid, env, fields = placing_leg(c)
        code, out, err = c.cli("snapshot-prepare")
        r.eq("snapshot-prepare: the placing leg to reconcile", [l["ref_id"] for l in out.get("reconcile", [])], [fields["ref_id"]])


# =============================================================================================
# child (§11.1 step 7)
# =============================================================================================

def t_child():
    with case("child", positions=POS_SB, quotes=Q_SB, equity=2150.0) as c:
        keep = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "CLAUDE_CONFIG_DIR",
                                                "CLAUDE_CODE_OAUTH_TOKEN", "LANG")}
        os.environ.update(OPENROUTER_API_KEY="sk-or-x", TELEGRAM_BOT_TOKEN="123:abc",
                          CLAUDE_CONFIG_DIR="/opt/quiver/state/claude-config", CLAUDE_CODE_OAUTH_TOKEN="oat-x", LANG="C.UTF-8")
        try:
            with c.led.connect() as conn:
                snap_job = store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
            built = two_leg(c)
            pid = built["proposal_id"]
            c.approve(built)
            log = []
            real_selftest = run_broker.guard_selftest

            def selftest(runner=None):
                log.append("selftest")
                return real_selftest(runner)
            run_broker.guard_selftest = selftest
            try:
                runner = Runner(c, opts={"sells": sb(), "stop": "after_sells", "snap": sb()}, log=log)
                c.broker(runner).drain()
            finally:
                run_broker.guard_selftest = real_selftest
            jobs = runner.jobs
            r.eq("child: execute jobs run before snapshot jobs", [("proposal_id" in j["values"]) for j in jobs], [True, False])
            seven = {"PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "RH_ACCOUNT_NUMBER", "DISABLE_AUTOUPDATER"}
            if len(jobs) == 2:
                ex, sn = jobs
                r.eq("child: the execute env is the 7 keys + job and proposal", set(ex["env"]), seven | {"QUIVER_BROKER_JOB", "QUIVER_BROKER_PROPOSAL"})
                r.eq("child: the execute ids", (ex["env"]["QUIVER_BROKER_PROPOSAL"], ex["env"]["QUIVER_BROKER_JOB"]),
                     (str(pid), str(c.job_of(pid)["id"])))
                r.eq("child: the snapshot env is the 7 keys only", set(sn["env"]), seven)
                r.eq("child: DISABLE_AUTOUPDATER=1, no secrets", (ex["env"]["DISABLE_AUTOUPDATER"], "OPENROUTER_API_KEY" in ex["env"],
                                                                  "TELEGRAM_BOT_TOKEN" in ex["env"]), ("1", False, False))
                a = ex["argv"]
                r.eq("child: --model", a[a.index("--model") + 1], run_broker.BROKER_MODEL)
                pf = a[a.index("--append-system-prompt-file") + 1]
                r.ok("child: --append-system-prompt-file state/tmp/broker_prompt_<job>.md",
                     pf.endswith(f"state/tmp/broker_prompt_{c.job_of(pid)['id']}.md"), pf)
                r.ok("child: the prompt file holds the runbook", (REPO / "EXECUTE.md").read_text(encoding="utf-8") in ex["prompt"], None)
                r.ok("child: the prompt holds prompts/broker.md", "already confirmed each order" in ex["prompt"], None)
                r.eq("child: --settings is deploy/runner/settings.json of the repo (/opt/quiver on the box)",
                     a[a.index("--settings") + 1], str(REPO / "deploy" / "runner" / "settings.json"))
                r.eq("child: --max-turns 48 for a 2-leg proposal", a[a.index("--max-turns") + 1], "48")
                r.ok("child: --dangerously-skip-permissions, stream-json, verbose",
                     "--dangerously-skip-permissions" in a and a[a.index("--output-format") + 1] == "stream-json" and "--verbose" in a, a)
                r.ok("child: no --mcp-config", "--mcp-config" not in a and "--strict-mcp-config" not in a, a)
                r.eq("child: the prompt", a[-1], "Run the job in your instructions.")
                s = sn["argv"]
                r.eq("child: --max-turns 40 for a snapshot job", s[s.index("--max-turns") + 1], "40")
                r.ok("child: the snapshot prompt holds SNAPSHOT.md", (REPO / "SNAPSHOT.md").read_text(encoding="utf-8") in sn["prompt"], None)
                r.eq("child: the outer limit is 600 s", runner.calls[-1]["timeout"], 600)
            spawns = [i for i, x in enumerate(log) if x.startswith("spawn:")]
            r.ok("child: the guard self-test runs before each job",
                 len(spawns) == 2 and all(log[i - 1] in ("selftest", "canary") for i in spawns) and log.count("selftest") == 2, log)
            r.ok("child: the broker log holds the stream", (c.root.path / "logs" / "desk" / "broker.log").exists(), None)
        finally:
            for k, v in keep.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    # a non-deny self-test answer fails a snapshot job too
    with case("child selftest") as c:
        with c.led.connect() as conn:
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
        rc.VENV_PY = "/usr/bin/true"
        runner = Runner(c, opts={"snap": std()})
        c.broker(runner).drain()
        r.eq("child: a non-deny self-test fails the snapshot job", (runner.jobs, c.q("SELECT status FROM broker_jobs")[0]["status"]),
             ([], "failed"))
    # QUIVER_AUTH_STOP in the stream -> the auth_error page (a snapshot job)
    with case("child auth") as c:
        with c.led.connect() as conn:
            store.enqueue_snapshot_job(conn, requested_by="monitor", now=c.now())
        runner = Runner(c, opts={"auth": True})
        c.broker(runner).drain()
        r.ok("child: QUIVER_AUTH_STOP -> the auth_error page", ("auth_error", notify.AUTH_STAGE) in c.pages.kinds(), c.pages.kinds())
        r.ok("child: the page names rh-reauth.sh", any("rh-reauth.sh" in x["event_detail"] for x in c.pages.calls), c.pages.calls)
        r.eq("child: the snapshot job failed", c.q("SELECT status FROM broker_jobs")[0]["status"], "failed")
        code, out, _ = c.cli("auth-stop", "--job", 1)
        r.eq("child: auth-stop prints QUIVER_AUTH_STOP and exits 0", (code, out.get("event")), (0, "QUIVER_AUTH_STOP"))


# =============================================================================================
# wait
# =============================================================================================

def t_wait():
    with case("wait") as c:
        code, out = c.tick("wait", "--seconds", 20)
        r.eq("wait: 20 s sleeps 20 s (FakeClock)", (code, out.get("slept"), c.clk.sleeps), (0, 20, [20]))
        code, out = c.tick("wait", "--seconds", 31)
        r.eq("wait: 31 is refused", (code, "error" in out, c.clk.sleeps), (1, True, [20]))
        code, out, _ = c.cli("wait", "--seconds", 31)
        r.eq("wait cli: 31 is refused (exit 1)", code, 1)
        ok, _ = broker_guard.evaluate("Bash", {"command": ".venv/bin/python tick.py wait --seconds 20"}, env={})
        r.eq("wait: the guard allows the runbook form", ok, True)



# =============================================================================================
# run_broker.py main (§11.1 steps 1-3) as a real process with a fake claude binary
# =============================================================================================

def _fake_claude(dirpath: Path, guard_events) -> Path:
    (dirpath / "guard.jsonl").write_text(_dump(guard_events))
    (dirpath / "false.jsonl").write_text(_dump(CANARY_FALSE))
    fake = dirpath / "claude"
    fake.write_text("#!/bin/sh\n"
                    "case \"$*\" in\n"
                    "  --version) echo '2.1.312 (Claude Code)';;\n"
                    f"  *canary_fail.json*) cat '{dirpath / 'false.jsonl'}';;\n"
                    f"  *'Call the Bash tool once'*) cat '{dirpath / 'guard.jsonl'}';;\n"
                    "  *) echo '{\"type\": \"result\", \"result\": \"done\"}';;\n"
                    "esac\n")
    fake.chmod(0o755)
    return fake


def t_main():
    with case("main") as c:
        built = c.build(["ANET"])
        pid = built["proposal_id"]
        c.approve(built)
        # a second approved proposal whose job was left running by a dead runner
        c.snap(**std(positions={"ANET": 2, "CEG": 2, "SGOV": 11}))
        b2 = c.build(["CEG"])
        c.approve(b2)
        c.broker().mark(c.job_of(b2["proposal_id"])["id"], "running")
        trig = c.root.path / "state" / "triggers" / "broker"
        trig.write_text("x")
        d = c.root.path / "fakebin"
        d.mkdir()
        fake = _fake_claude(d, CANARY_GUARD)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("QUIVER_BROKER_", "TELEGRAM_"))}
        env.update(c.root.env, CLAUDE_BIN=str(fake))
        p = subprocess.run([PY, str(REPO / "deploy" / "runner" / "run_broker.py")], env=env, cwd=str(REPO),
                           capture_output=True, text=True, timeout=180)
        r.eq("main: exit 0", p.returncode, 0)
        r.eq("main: the trigger file is deleted", trig.exists(), False)
        r.eq("main: a running job at start -> failed 'runner stopped'",
             (c.job_of(b2["proposal_id"])["status"], c.job_of(b2["proposal_id"])["error"]), ("failed", "runner stopped"))
        r.eq("main: its proposal closed", c.prop(b2["proposal_id"])["state"], "failed")
        r.eq("main: the queued execute job ran (expired on the real clock)", (c.job_of(pid)["status"], c.prop(pid)["state"]),
             ("failed", "failed"))
        lines = [json.loads(x) for x in (c.root.path / "logs" / "tick.log").read_text().splitlines() if x.strip()]
        r.ok("main: a broker_ok line with the job count", any(x.get("event") == "broker_ok" and x.get("jobs") == 1 for x in lines), lines)
        with c.led.connect() as conn:
            r.eq("main: the canary passed with the fake binary and stored its version",
                 store.get_state(conn, "canary_version"), "2.1.312 (Claude Code)")
        r.eq("main: the broker lock is released", c.led.named_lock("broker"), None)



# =============================================================================================
# gate B review fixes (2026-10-28): M1 position gate, M2 miss count, M4 result binding, SEC-1
# =============================================================================================

DENY_TEXT = ("Error: PreToolUse:mcp__robinhood-trading__place_equity_order hook error: "
             "broker_guard: denied: the leg has no passed review")


POS_2S = {"ANET": 6, "CEG": 2, "SGOV": 5}   # ANET 45% and CEG 30% of $2,000: two sells


def _placing_sells(c, tickers, spec):
    """Every sell leg of one proposal moved to ``placing`` by the guard. Returns (pid, env, legs)."""
    pid, env = c.ready(list(tickers))
    out = c.check(pid, "sells", c.write("s.json", c.snapfile(**spec)), env)
    legs = out["legs"]
    for f in legs:
        c.clk.advance(1)
        broker_guard.record_review(review_payload(f), env=env, now=c.clk.now)
        c.clk.advance(1)
        ok, why = broker_guard.evaluate("mcp__robinhood-trading__place_equity_order", f, env=env, now=c.clk.now)
        assert ok, why
    return pid, env, legs


def t_gate_b():
    # M4: a guard deny binds no leg; an order binds its own leg even when an error comes first.
    with case("gate-b deny then order") as c:
        pid, env, fields = placing_leg(c)
        o = order_from(fields, oid="RH-A", state="confirmed")
        o["ref_id"] = "01a16428-ff4c-743b-ada7-1f05229ba861"   # Robinhood returns its own ref_id
        code, out = c.tick("exec-commit", "--proposal", pid, "--input",
                           c.write("res.json", {"place_equity_order": [DENY_TEXT, o]}), env=env)
        leg = c.leg(fields["ref_id"])
        r.eq("gate-b M4: the order binds its leg after a deny result", (leg["state"], leg["broker_order_id"]),
             ("placed", "RH-A"))
        r.ok("gate-b M4: the deny is reported, not bound", {"result": "guard_denied"} in (out or {}).get("legs", []), out)
    spec2 = std(positions=dict(POS_2S))
    with case("gate-b two legs, errors", positions=POS_2S, quotes=STD_Q, equity=2000.0) as c:
        pid, env, legs = _placing_sells(c, ["ANET", "CEG"], spec2)
        r.eq("gate-b M4: two sell legs are placing", [c.leg(l["ref_id"])["state"] for l in legs], ["placing", "placing"])
        if len(legs) == 2:
            err = [{"type": "text", "text": 'Error: 400 Bad Request: {"quantity": ["exceeds shares available"]}'}]
            later = order_from(legs[1], oid="RH-2", state="confirmed")
            later["ref_id"] = "01a16428-0000-743b-ada7-1f05229ba861"
            c.tick("exec-commit", "--proposal", pid, "--input",
                   c.write("res.json", {"place_equity_order": [err, later]}), env=env)
            a, b = c.leg(legs[0]["ref_id"]), c.leg(legs[1]["ref_id"])
            r.eq("gate-b M4: the order binds its own leg, the one error binds the one leg left",
                 (a["state"], b["state"], b["broker_order_id"]), ("broker_rejected", "placed", "RH-2"))
    with case("gate-b two errors", positions=POS_2S, quotes=STD_Q, equity=2000.0) as c2:
            pid2, env2, legs2 = _placing_sells(c2, ["ANET", "CEG"], spec2)
            if len(legs2) == 2:
                e1 = [{"type": "text", "text": "Error: 503 Service Unavailable"}]
                e2 = [{"type": "text", "text": 'Error: 400 Bad Request: {"quantity": ["exceeds shares available"]}'}]
                c2.tick("exec-commit", "--proposal", pid2, "--input",
                        c2.write("res.json", {"place_equity_order": [e1, e2]}), env=env2)
                r.eq("gate-b M4: two error bodies and two legs bind nothing (both stay placing)",
                     [c2.leg(l["ref_id"])["state"] for l in legs2], ["placing", "placing"])

    # M2: one snapshot read that misses a placed desk leg proves nothing; the second miss ends it.
    with case("gate-b miss count") as c:
        pid, env, fields = placing_leg(c)
        c.tick("exec-commit", "--proposal", pid, "--input",
               c.write("res.json", {"place_equity_order": [order_from(fields, oid="P9")]}), env=env)
        before = c.leg(fields["ref_id"])
        c.tick("fills", "--input", c.write("f.json", {"get_equity_orders": orders_answer([])}))
        leg = c.leg(fields["ref_id"])
        r.eq("gate-b M2: a fills read that misses the leg changes nothing",
             (leg["state"], leg["fill_state"], leg["reconcile_attempts"]),
             ("placed", before["fill_state"], before["reconcile_attempts"]))
        c.clk.advance(60)
        c.tick("snapshot-commit", "--input", c.write("s1.json", c.snapfile(**std(), orders=[])))
        leg = c.leg(fields["ref_id"])
        r.eq("gate-b M2: one snapshot miss -> still placed, 1 miss", (leg["state"], leg["reconcile_attempts"]),
             ("placed", 1))
        with c.led.connect() as conn:
            r.eq("gate-b M2: the ticker stays in flight", proposals.in_flight_ticker(conn, "ANET") is not None, True)
        c.clk.advance(60)
        filled = order_from(fields, oid="P9", state="filled", qty=1.333333, avg=150.0)
        c.tick("snapshot-commit", "--input", c.write("s2.json", c.snapfile(**std(), orders=[filled])))
        leg = c.leg(fields["ref_id"])
        r.eq("gate-b M2: a later read with the order fills the leg", (leg["state"], leg["fill_avg_price"]),
             ("filled", 150.0))
    with case("gate-b two misses") as c:
        pid, env, fields = placing_leg(c)
        c.tick("exec-commit", "--proposal", pid, "--input",
               c.write("res.json", {"place_equity_order": [order_from(fields, oid="P8")]}), env=env)
        for i in range(2):
            c.clk.advance(60)
            c.tick("snapshot-commit", "--input", c.write(f"s{i}.json", c.snapfile(**std(), orders=[])))
        leg = c.leg(fields["ref_id"])
        r.eq("gate-b M2: two snapshot misses -> outcome_unknown", (leg["state"], leg["reconcile_attempts"]),
             ("outcome_unknown", 2))
        queued = execution.pop_messages(c.led, pid)
        r.ok("gate-b M2: the owner hears 'can't confirm'", any("can't confirm the ANET order" in t for t in queued), queued)
        res = store.loads(c.prop(pid)["result_json"], {}) or {}
        closing = proposals.close_from_legs(c.led, pid, c.clk.now) or ""
        r.ok("gate-b M2/M8: the result text never says 'I placed nothing' for a sent leg",
             "I placed nothing" not in closing and "I placed nothing" not in json.dumps(res), (closing, res))

    # M1: no re-send when the live position shows the leg already executed.
    with case("gate-b position gate") as c:
        pid, fields, b = _lost(c)
        rose = std(positions={**STD_POS, "ANET": STD_POS["ANET"] + 1.333333})
        run_job(c, pid, {"recover": rose, "orders": [], "after": rose})
        leg = c.leg(fields["ref_id"])
        bot = b.runner.last_bot if hasattr(b, "runner") and getattr(b.runner, "last_bot", None) else None
        r.eq("gate-b M1: a risen position blocks the re-send (leg stays placing, 1 miss)",
             (leg["state"], leg["reconcile_attempts"]), ("placing", 1))
        with c.led.connect() as conn:
            r.eq("gate-b M1: _position_moved names the rise",
                 execution._position_moved(conn, c.leg(fields["ref_id"]),
                                           {"ANET": {"quantity": STD_POS["ANET"] + 1.333333}}), "position_rose")
            r.eq("gate-b M1: no live positions block a re-send",
                 execution._position_moved(conn, c.leg(fields["ref_id"]), None), "no_positions")
            r.eq("gate-b M1: an unchanged position allows it",
                 execution._position_moved(conn, c.leg(fields["ref_id"]), {"ANET": {"quantity": STD_POS["ANET"]}}),
                 None)

    # SEC-1: no ledger cell holds the account number after the snapshot paths run.
    with case("gate-b account scan") as c:
        pid, env, fields = placing_leg(c)
        c.tick("exec-commit", "--proposal", pid, "--input",
               c.write("res.json", {"place_equity_order": [order_from(fields, oid="P7")]}), env=env)
        c.tick("snapshot-commit", "--input", c.write("s.json", c.snapfile(**std(), orders=[])))
        acct = c.cfg.account_number
        hits = []
        with c.led.connect() as conn:
            for (tbl,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                       "AND name NOT LIKE '%fts%' AND name NOT LIKE 'sqlite_%'").fetchall():
                for row in conn.execute(f'SELECT * FROM "{tbl}"').fetchall():
                    if any(isinstance(v, str) and acct in v for v in tuple(row)):
                        hits.append(tbl)
        r.ok("gate-b SEC-1: the account number is set in the test config", bool(acct), acct)
        r.eq("gate-b SEC-1: no ledger cell holds the account number", sorted(set(hits)), [])

CASES = [t_prepare, t_check_gate, t_check_gate_jobs, t_requote_buys, t_stopped, t_canary, t_audit,
         t_requote_edge, t_requote, t_no_quote, t_fields, t_buy_clamp, t_input_path, t_commit, t_review_gate,
         t_fills, t_recovery, t_snapshot, t_child, t_wait, t_main, t_gate_b]

if __name__ == "__main__":
    only = set(sys.argv[1:])
    for fn in CASES:
        if only and fn.__name__ not in only:
            continue
        t0 = _time.time()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            r.ok(f"{fn.__name__}: no crash", False, "".join(traceback.format_exception(e))[-3000:])
        print(f"  {fn.__name__}: {_time.time() - t0:.1f}s", file=sys.stderr)
    r.ok("no yfinance call", YF_CALLS == [], YF_CALLS)
    raise SystemExit(r.done())
