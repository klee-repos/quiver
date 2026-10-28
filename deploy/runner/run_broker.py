#!/usr/bin/env python3
"""The broker runner: the only process with Robinhood access (plan §11.1).

``quiver-broker.path`` starts this unit while ``state/triggers/broker`` exists. The runner
deletes the trigger, takes the ``broker`` run lock, and drains ``broker_jobs``: ``execute``
jobs first, then ``snapshot`` jobs. For each job it runs one ``claude -p`` with the Robinhood
MCP, a runbook (EXECUTE.md or SNAPSHOT.md) in its system prompt, and ``broker_guard`` as
the hook on every tool call. Python sizes and checks every order; the model only calls the
tools the runbook names and passes raw results to ``tick.py``.

Safety around each job:
  * the guard self-test (a synthetic place with an unknown ``ref_id`` must exit 2);
  * the CLI canary once a day and after a ``$CLAUDE_BIN --version`` change (proves the
    hooks load and that ``onFailure: "block"`` blocks in this binary);
  * an explicit child environment (no Telegram token, no other secret);
  * after the run: the AUTH-STOP sentinel, the place-call audit, the close of the proposal.

``run_broker.py --canary --bin PATH`` runs only the CLI canary; exit 0 on a pass, 1 on a fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import runner_common as rc  # noqa: E402
from lib import market, notify, runlock  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.desk import execution, outbox, proposals, store  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
from lib.prompts import load_prompt  # noqa: E402

ROOT = Path(os.environ.get("QUIVER_ROOT") or _REPO)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
BROKER_MODEL = os.environ.get("QUIVER_BROKER_MODEL", "claude-sonnet-5-5")
SETTINGS = str(_REPO / "deploy" / "runner" / "settings.json")
CANARY_FAIL = str(_REPO / "deploy" / "runner" / "canary_fail.json")
GUARD = str(_REPO / "deploy" / "runner" / "broker_guard.py")
JOB_TIMEOUT_S = 600
LOCK_TTL_S = 3600
CANARY_PROMPT = "Call the Bash tool once with the command `true`."
GUARD_FAIL_TEXT = "My order guard failed its check, so I placed nothing."
RECOVERY_GUARD_FAIL_TEXT = ("My order guard failed its check, so I sent no order again. The order I lost track "
                            "of may still be open: check the Robinhood app.")
_CHILD_KEYS = ("PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "RH_ACCOUNT_NUMBER")


def default_runner(argv, env, timeout, cwd):
    """The production process runner: ``(returncode, stdout, stderr)``."""
    try:
        p = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out, f"timeout after {timeout}s"


def child_env(*, job_id=None, proposal_id=None) -> dict:
    """The explicit environment of ``claude -p``: the 7 keys of §11.1 step 7, plus the job
    and proposal ids for an execute job only (a snapshot job can place nothing)."""
    env = {k: os.environ[k] for k in _CHILD_KEYS if os.environ.get(k)}
    env["DISABLE_AUTOUPDATER"] = "1"
    if proposal_id is not None:
        env["QUIVER_BROKER_JOB"] = str(job_id)
        env["QUIVER_BROKER_PROPOSAL"] = str(proposal_id)
    return env


def claude_argv(prompt_file: str, *, max_turns: int, settings: str = SETTINGS,
                prompt: str = "Run the job in your instructions.") -> list:
    return [CLAUDE_BIN, "-p", "--model", BROKER_MODEL, "--append-system-prompt-file", prompt_file,
            "--settings", settings, "--max-turns", str(max_turns), "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose", prompt]


# --- stream-json -----------------------------------------------------------------------

def stream_events(stdout: str) -> list:
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def tool_calls(stdout: str) -> list:
    """``[{id, name, input, is_error, text}]`` in order, joining each tool_use with its result."""
    uses, results = [], {}
    for ev in stream_events(stdout):
        msg = ev.get("message") or {}
        for c in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                uses.append({"id": c.get("id"), "name": c.get("name"), "input": c.get("input") or {}})
            elif c.get("type") == "tool_result":
                content = c.get("content")
                if isinstance(content, list):
                    content = " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
                results[c.get("tool_use_id")] = {"is_error": c.get("is_error") is True, "text": str(content or "")}
    for u in uses:
        r = results.get(u["id"]) or {}
        u["is_error"] = r.get("is_error")
        u["text"] = r.get("text", "")
    return uses


def canary_verdict(stdout: str, *, need_reason: bool) -> tuple:
    """(passed, why). Every Bash tool_result must be an error (and, for run 1, carry the
    broker_guard deny reason). At least one Bash call must exist."""
    bash = [c for c in tool_calls(stdout) if c["name"] == "Bash"]
    if not bash:
        return False, "no Bash tool_use in the stream"
    for c in bash:
        if c.get("is_error") is not True:
            return False, "a Bash tool_result was not an error: the hook did not block"
        if need_reason and "broker_guard" not in (c.get("text") or ""):
            return False, "a Bash tool_result did not carry the broker_guard reason"
    return True, "ok"


# --- checks ------------------------------------------------------------------------------

def guard_selftest(runner=default_runner) -> tuple:
    payload = json.dumps({"tool_name": "mcp__robinhood-trading__place_equity_order",
                          "tool_input": {"ref_id": "00000000-0000-0000-0000-000000000000",
                                         "symbol": "ZZZZ", "side": "buy", "type": "market"}})
    py = rc.VENV_PY
    try:
        p = subprocess.run([py, GUARD], input=payload, capture_output=True, text=True, timeout=30,
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "QUIVER_ROOT": str(ROOT)})
    except Exception as e:  # noqa: BLE001
        return False, f"guard self-test could not run: {e}"
    if p.returncode != 2:
        return False, f"guard self-test exit {p.returncode}, wanted 2"
    return True, "deny"


def cli_version(bin_path: str, runner=default_runner) -> str:
    code, out, err = runner([bin_path, "--version"], {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                      "HOME": os.environ.get("HOME", "/tmp")}, 30, str(_REPO))
    return (out or err).strip()[:60]


def run_canary(led, bin_path: str, runner=default_runner) -> tuple:
    """Run the two-run CLI canary with ``bin_path``. On a pass store the version."""
    global CLAUDE_BIN
    prev = CLAUDE_BIN
    CLAUDE_BIN = bin_path
    try:
        pf = ROOT / "state" / "tmp" / "broker_prompt_canary.md"
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(load_prompt("broker"), encoding="utf-8")
        env = child_env()
        code1, out1, _ = runner(claude_argv(str(pf), max_turns=3, settings=SETTINGS, prompt=CANARY_PROMPT),
                                env, 300, str(_REPO))
        ok1, why1 = canary_verdict(out1, need_reason=True)
        if not ok1:
            return False, f"run 1: {why1}"
        code2, out2, _ = runner(claude_argv(str(pf), max_turns=3, settings=CANARY_FAIL, prompt=CANARY_PROMPT),
                                env, 300, str(_REPO))
        ok2, why2 = canary_verdict(out2, need_reason=False)
        if not ok2:
            return False, f"run 2: {why2}"
        version = cli_version(bin_path, runner)
        with led.connect() as conn:
            store.set_state(conn, "canary_version", version)
            store.set_state(conn, "canary_day", market.trading_day_et())
        return True, version
    finally:
        CLAUDE_BIN = prev


def canary_due(led, runner=default_runner) -> bool:
    with led.connect() as conn:
        ver = store.get_state(conn, "canary_version")
        day = store.get_state(conn, "canary_day")
    if day != market.trading_day_et():
        return True
    return cli_version(CLAUDE_BIN, runner) != ver


# --- the runner ----------------------------------------------------------------------

class Broker:
    def __init__(self, *, led=None, cfg=None, runner=default_runner, box=None, now_fn=None):
        self.led = led or Ledger(ROOT / "state" / "ledger.db")
        self.cfg = cfg or load_config(ROOT / "config.yaml")
        self.runner = runner
        self.now_fn = now_fn or store.utc_now
        if box is None:
            token, chats = rc.telegram.resolve_env()
            allowed = [c for c in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").replace(",", " ").split() if c]
            box = outbox.Outbox(self.led, transport=outbox.telegram_transport(token),
                                chat_id=(allowed or chats or [None])[0])
        self.box = box
        self.canary_failed = None

    # paging
    def page(self, kind, stage, detail):
        rc._maybe_alert(self.led, kind=kind, stage=stage, day=market.trading_day_et(),
                        now_iso=store.iso(self.now_fn()), event_detail=detail)

    def say(self, text):
        if text:
            self.box.send(text, source="execution")

    # §11.1 step 3
    def close_stopped(self):
        with self.led.transaction() as conn:
            rows = conn.execute("SELECT id, proposal_id FROM broker_jobs WHERE status='running'").fetchall()
            for r in rows:
                conn.execute("UPDATE broker_jobs SET status='failed', error='runner stopped', finished_at=? "
                             "WHERE id=?", (store.now_iso(), r[0]))
        for r in rows:
            if r[1]:
                self.finish_proposal(r[1], reason="the broker run stopped before it finished")

    # §11.1 step 4
    def recover_lost(self):
        now = self.now_fn()
        with self.led.transaction() as conn:
            legs = [dict(x) for x in conn.execute(
                "SELECT ref_id, ticker, proposal_id, submitted_at FROM orders WHERE proposal_id IS NOT NULL "
                "AND state='placing'").fetchall()]
            todo = []
            for l in legs:
                key = f"lost_notified:{l['ref_id']}"
                if store.get_state(conn, key) is None:
                    store.set_state(conn, key, store.iso(now))
                    todo.append(l)
            for pid in sorted({l["proposal_id"] for l in legs}):
                q = conn.execute("SELECT 1 FROM broker_jobs WHERE kind='execute' AND proposal_id=? AND recovery=1 "
                                 "AND status='queued'", (pid,)).fetchone()
                if not q:
                    conn.execute("INSERT INTO broker_jobs (kind, proposal_id, recovery, status, requested_by, created_at) "
                                 "VALUES ('execute', ?, 1, 'queued', 'recovery', ?)", (pid, store.iso(now)))
        for l in todo:
            at = market.fmt_et(store.parse_ts(l["submitted_at"])) if l.get("submitted_at") else "an unknown time"
            self.say(f"I lost track of the {l['ticker']} order at {at}. I'm checking it with Robinhood.")

    def next_job(self):
        with self.led.connect() as conn:
            row = conn.execute("SELECT * FROM broker_jobs WHERE status='queued' ORDER BY "
                               "CASE kind WHEN 'execute' THEN 0 ELSE 1 END, id LIMIT 1").fetchone()
            return dict(row) if row else None

    def mark(self, job_id, status, error=None):
        with self.led.connect() as conn:
            if status == "running":
                conn.execute("UPDATE broker_jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                             (store.now_iso(), job_id))
            else:
                conn.execute("UPDATE broker_jobs SET status=?, finished_at=?, error=? WHERE id=?",
                             (status, store.now_iso(), error, job_id))

    def write_prompt(self, job, values: dict, runbook: str) -> str:
        text = load_prompt("broker") + "\n\n" + (_REPO / runbook).read_text(encoding="utf-8") + \
            "\n\n## This job\n\n```json\n" + json.dumps(values, indent=2, default=str) + "\n```\n"
        p = ROOT / "state" / "tmp" / f"broker_prompt_{job['id']}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return str(p)

    def spawn(self, job, prompt_file, *, max_turns, proposal_id=None):
        env = child_env(job_id=job["id"], proposal_id=proposal_id)
        code, out, err = self.runner(claude_argv(prompt_file, max_turns=max_turns), env, JOB_TIMEOUT_S, str(_REPO))
        try:
            log = ROOT / "logs" / "desk" / "broker.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"job": job["id"], "kind": job["kind"], "returncode": code,
                                    "at": store.now_iso()}) + "\n")
                f.write(out or "")
                if err:
                    f.write("\n" + err[-2000:] + "\n")
        except OSError:
            pass
        return code, out, err

    def auth_stopped(self, out: str) -> bool:
        if execution_sentinel() in (out or ""):
            rc._emit({"stage": "broker", "event": "AUTH_ERROR",
                      "detail": "Robinhood MCP auth failed; placed nothing"})
            self.page("auth_error", notify.AUTH_STAGE, "Robinhood login expired; run ./deploy/gcp/rh-reauth.sh")
            return True
        return False

    def audit(self, out: str):
        """Every place call that ran (no error result) must belong to a leg the guard
        moved to placing, and one job places a leg at most once (the guard allows one place
        per check). A bad call halts the day and pages. With no day_baseline row yet (a
        recovery job before the day's first snapshot) the audit inserts the row from the
        newest real snapshot equity, already halted. This unit cannot write KILL
        (ProtectSystem=strict)."""
        bad, seen = [], {}
        with self.led.connect() as conn:
            for c in tool_calls(out):
                if not str(c.get("name") or "").endswith("place_equity_order") or c.get("is_error"):
                    continue
                ref = str((c.get("input") or {}).get("ref_id") or "")
                seen[ref] = seen.get(ref, 0) + 1
                row = conn.execute("SELECT state, submitted_at FROM orders WHERE ref_id=?", (ref,)).fetchone()
                if row is None or row[1] is None or row[0] in ("proposed", "approved", "checked") or seen[ref] > 1:
                    bad.append(ref or "(no ref_id)")
        if bad:
            why = f"audit: place call with no guard allow ({', '.join(bad)})"
            day = market.trading_day_et()
            self.led.mark_halted(day, why)
            if not self.led.is_halted(day):
                with self.led.transaction() as conn:
                    row = conn.execute("SELECT equity, captured_at FROM broker_snapshots "
                                       "ORDER BY id DESC LIMIT 1").fetchone()
                    if row is not None:
                        conn.execute("INSERT OR IGNORE INTO day_baseline (trade_date, baseline_equity, captured_at, "
                                     "halted, halt_reason) VALUES (?,?,?,1,?)", (day, row[0], row[1], why))
                        conn.execute("UPDATE day_baseline SET halted=1, halt_reason=? WHERE trade_date=?", (why, day))
            self.page("error", "broker", f"place call with no guard allow row: {', '.join(bad)}; trading halted")
        return bad

    def finish_proposal(self, pid, reason=None):
        req = execution.build_requote(self.led, self.cfg, pid, now_dt=self.now_fn())
        text = proposals.close_from_legs(self.led, pid, self.now_fn(), reason=reason)
        for m in execution.pop_messages(self.led, pid):
            self.say(m)
        if req:
            self.say(req["text"])
            if req.get("new"):
                outbox.send_trade_card(self.led, self.box, self.led.connect, req["new"], now_dt=self.now_fn())
        if text:
            self.say(text)

    def follow_ups(self):
        """After a snapshot, build each waiting follow-up trade (§10.4)."""
        with self.led.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM proposals WHERE kind='change' AND state='applied' AND result_json LIKE '%waiting%'")]
        for prop in rows:
            res = store.loads(prop.get("result_json"), {}) or {}
            if res.get("follow_up") != "waiting":
                continue
            yf = proposals.prefetch_prices(self.led, proposals.change_tickers(store.loads(prop.get("payload_json"), {})),
                                           now_dt=self.now_fn())
            with self.led.transaction() as conn:
                fu = proposals.build_follow_up(self.led, conn, self.cfg, prop, now_dt=self.now_fn(), yf_price_fn=yf)
                if not fu.get("waiting"):
                    res.pop("follow_up", None)
                    conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(res), prop["id"]))
            if fu.get("built"):
                outbox.send_trade_card(self.led, self.box, self.led.connect, fu["built"], now_dt=self.now_fn())
            elif fu.get("refused"):
                self.say(f"I could not build the follow-up trade of #{prop['id']}: {fu['refused']}.")

    def sweep_closes(self):
        """Close every proposal whose legs ended, and send every queued message (the
        snapshot-commit inside the job queues its result text; it cannot send)."""
        with self.led.connect() as conn:
            pids = [r[0] for r in conn.execute("SELECT id FROM proposals WHERE kind='trade' AND state='executing'")]
        for pid in sorted(set(pids) | set(execution.proposals_with_messages(self.led))):
            self.finish_proposal(pid)

    def halt_check(self, was_halted: bool):
        if not was_halted and self.led.is_halted(market.trading_day_et()):
            rc._emit({"stage": "broker", "event": "daily_halt", "detail": "the daily-loss check fired"})
            self.page("halt", notify.HALT_STAGE, "The daily-loss check fired; Approve taps are blocked for today.")

    def run_execute(self, job):
        pid = int(job["proposal_id"])
        recovery = bool(job["recovery"])
        ok, why = guard_selftest(self.runner)
        if not ok:
            self.mark(job["id"], "failed", why)
            if not recovery:
                with self.led.transaction() as conn:
                    execution._fail_proposal(conn, pid, "guard", GUARD_FAIL_TEXT)
            self.say(RECOVERY_GUARD_FAIL_TEXT if recovery else GUARD_FAIL_TEXT)
            self.page("error", "broker", f"order guard check failed: {why}")
            self.finish_proposal(pid)
            return
        # A recovery job can re-send a leg, so it needs the canary too. On a fail it runs
        # nothing: the legs stay placing and the snapshot reconcile decides them.
        if canary_due(self.led, self.runner):
            cok, cwhy = run_canary(self.led, CLAUDE_BIN, self.runner)
            if not cok:
                self.canary_failed = cwhy
                self.mark(job["id"], "failed", f"canary: {cwhy}")
                if not recovery:
                    with self.led.transaction() as conn:
                        execution._fail_proposal(conn, pid, "guard", GUARD_FAIL_TEXT)
                self.say(RECOVERY_GUARD_FAIL_TEXT if recovery else GUARD_FAIL_TEXT)
                self.page("error", "broker", f"CLI canary failed: {cwhy}")
                self.finish_proposal(pid)
                return
        prep = execution.exec_prepare(self.led, self.cfg, pid, recovery=recovery, now_dt=self.now_fn())
        if not prep.get("ok"):
            self.mark(job["id"], "failed", prep.get("reason"))
            self.say(prep.get("text"))
            execution.pop_messages(self.led, pid)
            return
        legs = prep.get("legs") or []
        values = {"job_id": job["id"], "proposal_id": pid, "account_number": self.cfg.account_number,
                  "recovery": recovery, "legs": legs, "created_at_gte": prep.get("created_at_gte"),
                  "fills_created_at_gte": execution.fills_window(self.led),
                  "book_tickers": execution.snapshot_prepare(self.led, self.cfg)["tickers"]}
        prompt = self.write_prompt(job, values, "EXECUTE.md")
        was_halted = self.led.is_halted(market.trading_day_et())
        code, out, err = self.spawn(job, prompt, max_turns=40 + 4 * max(1, len(legs)), proposal_id=pid)
        reason = None
        if self.auth_stopped(out):
            reason = "the Robinhood login expired"
        elif code != 0:
            why = rc._orchestrator_reason(out) or (err or "")[-200:]
            if any(m in (why or "").lower() for m in rc._AUTH_FAIL_MARKERS):
                reason = "the Claude login failed"
            else:
                reason = f"the broker run stopped (exit {code})"
        self.audit(out)
        self.mark(job["id"], "done" if code == 0 else "failed", reason)
        self.finish_proposal(pid, reason=reason or "the runbook ended before it placed the orders")
        self.sweep_closes()
        self.halt_check(was_halted)
        self.follow_ups()

    def run_snapshot(self, job):
        ok, why = guard_selftest(self.runner)
        if not ok:
            self.mark(job["id"], "failed", why)
            self.page("error", "broker", f"order guard check failed: {why}")
            return
        prep = execution.snapshot_prepare(self.led, self.cfg)
        values = {"job_id": job["id"], "account_number": self.cfg.account_number,
                  "tickers": prep["tickers"], "reconcile": prep["reconcile"],
                  "fills_created_at_gte": prep["fills_created_at_gte"]}
        prompt = self.write_prompt(job, values, "SNAPSHOT.md")
        was_halted = self.led.is_halted(market.trading_day_et())
        code, out, err = self.spawn(job, prompt, max_turns=40)
        auth = self.auth_stopped(out)
        self.audit(out)
        self.mark(job["id"], "done" if code == 0 and not auth else "failed",
                  "auth" if auth else (None if code == 0 else f"exit {code}"))
        self.sweep_closes()
        self.halt_check(was_halted)
        self.follow_ups()

    def drain(self):
        n = 0
        while True:
            job = self.next_job()
            if job is None:
                return n
            self.mark(job["id"], "running")
            try:
                if job["kind"] == "execute":
                    self.run_execute(job)
                else:
                    self.run_snapshot(job)
            except Exception as e:  # noqa: BLE001 — one bad job must not stop the drain
                self.mark(job["id"], "failed", f"{type(e).__name__}: {e}")
                rc._emit({"stage": "broker", "event": "stage_error", "error": f"{type(e).__name__}: {e}"})
                self.page("error", "broker", f"job {job['id']}: {type(e).__name__}: {e}")
                if job.get("proposal_id"):
                    try:
                        self.finish_proposal(int(job["proposal_id"]), reason="the broker run hit an error")
                    except Exception:  # noqa: BLE001
                        pass
            n += 1


def execution_sentinel() -> str:
    return "QUIVER_AUTH_STOP"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--canary" in argv:
        i = argv.index("--bin") if "--bin" in argv else -1
        bin_path = argv[i + 1] if i >= 0 and i + 1 < len(argv) else CLAUDE_BIN
        led = Ledger(ROOT / "state" / "ledger.db")
        ok, why = run_canary(led, bin_path)
        rc._emit({"stage": "canary", "ok": ok, "detail": why, "bin": bin_path})
        return 0 if ok else 1
    trig = ROOT / "state" / "triggers" / "broker"
    try:
        trig.unlink()
    except FileNotFoundError:
        pass
    b = Broker()
    holder = runlock.holder_id()
    try:
        with runlock.run_lock(b.led, "broker", holder, store.now_iso(), LOCK_TTL_S):
            b.close_stopped()
            b.recover_lost()
            n = b.drain()
            rc._emit({"stage": "broker", "event": "broker_ok", "jobs": n})
    except runlock.RunLockError as e:
        rc._emit({"stage": "broker", "skipped": True, "reason": str(e)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
