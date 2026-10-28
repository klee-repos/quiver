#!/usr/bin/env python3
"""End-to-end coverage of the PAGER: when, and to which chat, an alert goes to Telegram.

Drives the real code offline (no network, no ``claude``, no MCP):

  * ``deploy/runner/runner_common._maybe_alert``: the one pager that the monitor, the worker,
    the broker runner and the chat bridge share (plan §14.3 ``runner_common`` row). It dedups
    on the ledger ``(date, kind, stage)`` row, takes an injectable transport, never raises,
    and handles an unconfigured pager (no token, no chat).
  * the critical pages (halt, auth_error) and the per-stage error pages.
  * two senders in process (``run_broker.Broker.page`` and ``run_monitor.Monitor.page``, and a
    later run of either) agree on the hash, so one event pages once per day per stage.
  * the ledger stage-keyed dedup + the (date, kind) -> (date, kind, stage) migration.
  * the import graph: no research module of plan §14.10 ``_RESEARCH`` loads ``lib.telegram``.

Every sender here runs with ``QUIVER_TELEGRAM_DISABLE=1`` or an injected ``send``, so nothing
reaches Telegram. Run: .venv/bin/python tests/test_e2e_alerts.py
"""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))
sys.path.insert(0, str(_REPO / "deploy" / "runner"))
# A default sender must never post from a test.
os.environ["QUIVER_TELEGRAM_DISABLE"] = "1"

from desk_fakes import FakeClock, Results, temp_root  # noqa: E402
from lib import notify  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
import runner_common as rc  # noqa: E402

r = Results("e2e-alerts")

DAY = "2026-08-05"
NOW = "2026-08-05T15:00:00Z"
SECRETS = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALERT_CHAT_IDS": "900000111"}
_TG_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALERT_CHAT_IDS", "TELEGRAM_ALLOWED_CHAT_IDS")


class _Recorder:
    """A transport that records each call and answers with ``result``."""

    def __init__(self, result=None):
        self.calls = []
        self.result = {"ok": True} if result is None else result

    def __call__(self, **kw):
        self.calls.append(kw)
        return dict(self.result)


@contextlib.contextmanager
def _pager(extra_config: str = ""):
    """A temp root whose config.yaml the pager reads (``rc.ROOT``), and a temp tick log."""
    saved_root, saved_log = rc.ROOT, rc.TICK_LOG
    saved_env = {k: os.environ.get(k) for k in _TG_KEYS}
    with temp_root(extra_config=extra_config) as root:
        rc.ROOT = root.path
        rc.TICK_LOG = str(root.path / "logs" / "tick.log")
        for k in _TG_KEYS:
            os.environ.pop(k, None)
        try:
            yield root
        finally:
            rc.ROOT, rc.TICK_LOG = saved_root, saved_log
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _page(led, kind, stage, detail="boom", *, send, secrets=SECRETS, severity="critical"):
    """One page through the shared pager. Returns the JSON events ``_emit`` printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc._maybe_alert(led, kind=kind, stage=stage, day=DAY, now_iso=NOW, event_detail=detail,
                        send=send, secrets=secrets, severity=severity)
    return [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.startswith("{")]


def _events(evs):
    return [e.get("event") for e in evs]


# ---- C. critical pages: halt and auth_error, gated on notify.on_error ----------------
with _pager() as root:
    led = root.led()
    tx = _Recorder()
    ev = _page(led, "halt", notify.HALT_STAGE, "drop 21.4% vs the baseline", send=tx)
    r.ok("[C] halt pages once", len(tx.calls) == 1, ev)
    r.eq("[C] the stage of a halt model with no stage is daily_loss_halt",
         notify.stage_of({"kind": "halt"}), "daily_loss_halt")
    r.eq("[C] a halt is critical", notify.severity_of({"kind": "halt"}), "critical")
    halt_text = tx.calls[0]["text"]
    r.ok("[C] the halt page says trading halted for today", "halted trading for today" in halt_text, halt_text)
    r.ok("[C] the halt page names the resume_trading proposal, not rm KILL",
         "resume_trading" in halt_text and "rm KILL" not in halt_text, halt_text)
    r.ok("[C] a critical page is LOUD (disable_notification False)",
         tx.calls[0].get("disable_notification") is False)
    _page(led, "halt", notify.HALT_STAGE, "drop 25.0% vs the baseline", send=tx)
    r.ok("[C] a repeat halt with a different detail dedups", len(tx.calls) == 1)
    _page(led, "auth_error", notify.AUTH_STAGE, "401 Unauthorized", send=tx)
    r.ok("[C] auth_error pages independently of the halt", len(tx.calls) == 2)
    r.eq("[C] auth_error stage is broker_auth", notify.stage_of({"kind": "auth_error"}), "broker_auth")
    r.eq("[C] auth_error is critical", notify.severity_of({"kind": "auth_error", "severity": "warning"}),
         "critical")
    r.ok("[C] auth_error and halt have different dedup hashes",
         notify.content_hash({"kind": "auth_error"}) != notify.content_hash({"kind": "halt"}))

with _pager(extra_config="\nnotify:\n  enabled: true\n  on_error: false\n") as root:
    tx = _Recorder()
    ev = _page(root.led(), "halt", notify.HALT_STAGE, send=tx)
    r.ok("[C] notify.on_error false -> no page", tx.calls == [], tx.calls)
    r.ok("[C] ... and the skip is logged with reason disabled",
         any(e.get("event") == "alert_skipped" and e.get("reason") == "disabled" for e in ev), ev)

with _pager(extra_config="\nnotify:\n  enabled: false\n") as root:
    tx = _Recorder()
    _page(root.led(), "auth_error", notify.AUTH_STAGE, send=tx)
    r.ok("[C] notify.enabled false -> no page (fails SAFE)", tx.calls == [], tx.calls)

with _pager() as root:
    (root.path / "config.yaml").write_text("schema: [this is: not, a valid config\n", encoding="utf-8")
    tx = _Recorder()
    _page(root.led(), "auth_error", notify.AUTH_STAGE, send=tx)
    r.ok("[C] a config that does not load does NOT silence the pager (on_error defaults on)",
         len(tx.calls) == 1, tx.calls)

# ---- D. hard errors page per (kind, stage), independently -------------------------------
with _pager() as root:
    led = root.led()
    tx = _Recorder()
    _page(led, "error", "monitor", "KeyError: 'positions'", send=tx)
    r.ok("[D] error/monitor pages", len(tx.calls) == 1)
    _page(led, "error", "monitor", "ValueError: other text", send=tx)
    r.ok("[D] error/monitor repeat with a different detail dedups (anti-spam)", len(tx.calls) == 1)
    _page(led, "error", "edgar", "SEC returned 403", send=tx)
    r.ok("[D] error/edgar pages independently (a different stage)", len(tx.calls) == 2)
    _page(led, "error", "news", "yfinance 429", send=tx, severity="warning")
    r.ok("[D] a warning stage pages once too", len(tx.calls) == 3)
    warn_text = tx.calls[-1]["text"]
    r.ok("[D] a warning page says the step is best-effort and retries",
         "best-effort" in warn_text and "What to do" not in warn_text, warn_text)
    r.ok("[D] a critical error page names the stage and the recovery",
         "stopped at the edgar step" in tx.calls[1]["text"] and "What to do" in tx.calls[1]["text"],
         tx.calls[1]["text"])
    r.ok("[D] the raw detail rides in the page", "SEC returned 403" in tx.calls[1]["text"])
    for st in ("monitor", "worker", "broker", "desk", "edgar", "snapshot", "execute", "path_unit"):
        r.eq(f"[D] stage {st} is critical", notify.severity_of({"kind": "error", "stage": st}), "critical")
    for st in ("levels", "drift", "expire", "queue", "reflect", "benchmark", "goal", "prune",
               "news", "transcripts", "brief"):
        r.eq(f"[D] stage {st} is a warning", notify.severity_of({"kind": "error", "stage": st}), "warning")

# ---- F. two senders in process agree on the hash: one event pages once ------------------
# The broker runner pages auth_error at a Robinhood 401. A later broker run (a new process)
# meets the same 401 with other text. Both go through runner_common with the PRODUCTION
# default transport (lib.telegram.send_message, offline under QUIVER_TELEGRAM_DISABLE=1).
import run_broker  # noqa: E402
import run_monitor  # noqa: E402


class _Box:
    chat_id = None

    def send(self, *a, **k):
        return {"ok": True}


clk = FakeClock("2026-08-05T15:00:00Z").pin_market()
try:
    with _pager() as root:
        os.environ.update(TELEGRAM_BOT_TOKEN="tok", TELEGRAM_ALLOWED_CHAT_IDS="900000111")
        led, cfg = root.led(), root.cfg()
        broker1 = run_broker.Broker(led=led, cfg=cfg, box=_Box(), now_fn=lambda: clk.now)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            broker1.page("auth_error", notify.AUTH_STAGE, "401 at get_portfolio")
        e1 = [json.loads(x) for x in buf.getvalue().splitlines() if x.startswith("{")]
        row_hash = led.last_notified_hash(DAY, "auth_error", notify.AUTH_STAGE)
        r.ok("[F] the first sender pages and writes the shared row",
             "alert_sent" in _events(e1) and row_hash is not None, e1)
        r.eq("[F] the row holds the hash of (kind, stage, severity) only",
             row_hash, notify.build_alert({"kind": "auth_error", "stage": notify.AUTH_STAGE,
                                           "severity": "critical"})["content_hash"])
        r.ok("[F] the hash ignores the detail and the host",
             notify.content_hash({"kind": "auth_error", "event_detail": "a", "host": "x"})
             == notify.content_hash({"kind": "auth_error", "event_detail": "b", "host": "y"}))
        broker2 = run_broker.Broker(led=root.led(), cfg=cfg, box=_Box(), now_fn=lambda: clk.now)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            broker2.page("auth_error", notify.AUTH_STAGE, "401 Unauthorized: token expired at 14:59Z")
        e2 = [json.loads(x) for x in buf.getvalue().splitlines() if x.startswith("{")]
        r.ok("[F] the second sender sees the row and skips (no double page)",
             any(e.get("event") == "alert_skipped" and e.get("reason") == "already_sent" for e in e2), e2)
        # The monitor is the other in-process sender. Its error page has its own stage.
        mon = run_monitor.Monitor(led=root.led(), cfg=cfg, box=_Box(), now_fn=lambda: clk.now)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mon.page("edgar", "SEC returned 403")
            mon.page("edgar", "SEC returned 403 again")
        e3 = [json.loads(x) for x in buf.getvalue().splitlines() if x.startswith("{")]
        r.eq("[F] the monitor pages its stage once, then dedups", _events(e3), ["alert_sent", "alert_skipped"])
        r.ok("[F] the monitor's row is its own (error, edgar)",
             led.last_notified_hash(DAY, "error", "edgar") == notify.content_hash({"kind": "error", "stage": "edgar"}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            broker1.page("error", "broker", "order guard check failed")
        e4 = [json.loads(x) for x in buf.getvalue().splitlines() if x.startswith("{")]
        r.ok("[F] the broker's error/broker page is independent of the monitor's error/edgar",
             "alert_sent" in _events(e4), e4)
finally:
    clk.unpin()

# ---- G. runner_common._maybe_alert (the shared pager) -------------------------------------
with _pager() as root:
    led = root.led()
    tx = _Recorder()
    _page(led, "auth_error", "broker_auth", "401", send=tx)
    r.ok("[G] the pager sends once", len(tx.calls) == 1)
    text = tx.calls[0]["text"]
    r.ok("[G] the auth copy is actionable (the re-auth command, no desktop)",
         "deploy/gcp/rh-reauth.sh" in text and "remotedesktop" not in text, text)
    r.ok("[G] it targets Telegram with the token and chat ids from Secrets",
         tx.calls[0].get("token") == "tok" and tx.calls[0].get("chat_ids") == ["900000111"], tx.calls[0])
    r.ok("[G] the row records the chat ids it reached",
         led.last_notified_hash(DAY, "auth_error", "broker_auth") is not None)
    _page(led, "auth_error", "broker_auth", "different", send=tx)
    r.ok("[G] it dedups against the shared row", len(tx.calls) == 1)
    _page(led, "error", "execute", "x", send=tx)
    r.ok("[G] a different stage pages independently", len(tx.calls) == 2)

    def _boom(**k):
        raise RuntimeError("network down")
    ev = _page(led, "error", "snapshot", "y", send=_boom)
    r.ok("[G] a raising transport is swallowed (never crashes a runner)",
         any(e.get("event") == "alert_failed" and "network down" in str(e.get("alert_detail")) for e in ev), ev)
    r.ok("[G] ... and no row is written, so the next run tries again",
         led.last_notified_hash(DAY, "error", "snapshot") is None)
    fail = _Recorder({"ok": False, "error": "chat not found"})
    _page(led, "error", "worker", "z", send=fail)
    r.ok("[G] a refused send writes no row", led.last_notified_hash(DAY, "error", "worker") is None)
    _page(led, "error", "worker", "z", send=tx)
    r.ok("[G] ... so the same event pages on the next run (at-least-once)", len(tx.calls) == 3)
    part = _Recorder({"ok": True, "partial": True})
    ev = _page(led, "error", "desk", "w", send=part)
    r.ok("[G] a partial send (primary ok) writes the row and logs alert_partial",
         led.last_notified_hash(DAY, "error", "desk") is not None and "alert_partial" in _events(ev), ev)

    # Unconfigured: no token, or no chat.
    nt = _Recorder()
    ev = _page(led, "error", "levels", "a", send=nt, secrets={"TELEGRAM_BOT_TOKEN": "",
                                                              "TELEGRAM_ALERT_CHAT_IDS": "1"})
    r.ok("[G] no token -> no send, no raise", nt.calls == [], nt.calls)
    r.ok("[G] ... logged as alert_unconfigured no_token",
         any(e.get("event") == "alert_unconfigured" and e.get("reason") == "no_token" for e in ev), ev)
    ev = _page(led, "error", "levels", "a", send=nt, secrets={"TELEGRAM_BOT_TOKEN": "tok"})
    r.ok("[G] no chat id -> no send, logged as no_chats",
         nt.calls == [] and any(e.get("reason") == "no_chats" for e in ev), ev)

    # Secrets win over os.environ (the runner scrubbed it); an object works like a dict.
    os.environ.update(TELEGRAM_BOT_TOKEN="envtok", TELEGRAM_ALLOWED_CHAT_IDS="999")
    obj = types.SimpleNamespace(telegram_bot_token="sectok", telegram_allowed_chat_ids="111",
                                telegram_alert_chat_ids="")
    so = _Recorder()
    _page(led, "error", "drift", "b", send=so, secrets=obj)
    r.ok("[G] a Secrets object supplies the token and chats, not os.environ",
         so.calls and so.calls[0]["token"] == "sectok" and so.calls[0]["chat_ids"] == ["111"], so.calls)
    obj2 = types.SimpleNamespace(telegram_bot_token="sectok", telegram_allowed_chat_ids="111",
                                 telegram_alert_chat_ids="222 333")
    _page(led, "error", "expire", "c", send=so, secrets=obj2)
    r.ok("[G] TELEGRAM_ALERT_CHAT_IDS wins over TELEGRAM_ALLOWED_CHAT_IDS",
         so.calls[-1]["chat_ids"] == ["222", "333"], so.calls[-1])
    env_tx = _Recorder()
    _page(led, "error", "queue", "d", send=env_tx, secrets=None)
    r.ok("[G] with no Secrets the pager reads os.environ",
         env_tx.calls and env_tx.calls[0]["token"] == "envtok" and env_tx.calls[0]["chat_ids"] == ["999"],
         env_tx.calls)

    # The shipped tick log drops the noisy keys (an alert's raw text and its kind).
    lines = [json.loads(x) for x in Path(rc.TICK_LOG).read_text(encoding="utf-8").splitlines() if x.strip()]
    r.ok("[G] the tick log carries the alert events", any(x.get("event") == "alert_sent" for x in lines), lines[:3])
    r.ok("[G] the tick log drops alert_detail and kind",
         all("alert_detail" not in x and "kind" not in x for x in lines), lines)

# ---- H. ledger: stage isolation + (date, kind) -> (date, kind, stage) migration ---------
with tempfile.TemporaryDirectory() as td:
    led_h = Ledger(Path(td) / "h.db")
    led_h.mark_notified(DAY, "error", "h_mon", "p", NOW, stage="monitor")
    led_h.mark_notified(DAY, "error", "h_prune", "p", NOW, stage="prune")
    r.ok("[H] same kind, different stage -> independent rows (no clobber)",
         led_h.last_notified_hash(DAY, "error", "monitor") == "h_mon"
         and led_h.last_notified_hash(DAY, "error", "prune") == "h_prune")
    r.ok("[H] the stage '' row is isolated", led_h.last_notified_hash(DAY, "brief") is None)
    led_h.mark_notified(DAY, "error", "h_mon2", "p", NOW, stage="monitor")
    r.eq("[H] a second mark on the same key replaces the hash",
         led_h.last_notified_hash(DAY, "error", "monitor"), "h_mon2")
    # The close brief keeps its own row (kind 'brief', stage 'brief', plan §14.9). An error
    # page of the brief step is kind 'error', stage 'brief': a separate row, so neither one
    # blocks the other.
    from lib.desk import brief as _brief
    _brief.mark_sent(led_h, DAY, "900000111", NOW)
    r.ok("[H] the brief row is kind 'brief', stage 'brief'",
         led_h.last_notified_hash(DAY, "brief", "brief") == _brief.content_hash(DAY))
    with _pager() as _root:
        btx = _Recorder()
        _page(led_h, "error", "brief", "render failed", send=btx, severity="warning")
        r.ok("[H] a sent brief does not block an error page of the brief step", len(btx.calls) == 1, btx.calls)
    r.ok("[H] ... and the error page does not mark the brief sent",
         led_h.last_notified_hash(DAY, "brief", "brief") == _brief.content_hash(DAY)
         and led_h.last_notified_hash(DAY, "error", "brief") == notify.content_hash(
             {"kind": "error", "stage": "brief", "severity": "warning"}))
    oldp = Path(td) / "old.db"
    con = sqlite3.connect(oldp)
    con.executescript(
        "CREATE TABLE notifications (trade_date TEXT NOT NULL, kind TEXT NOT NULL, "
        "content_hash TEXT NOT NULL, recipients TEXT, sent_at TEXT NOT NULL, "
        "PRIMARY KEY (trade_date, kind));"
        "INSERT INTO notifications VALUES ('2026-06-14','digest','oldhash','a@b.com','t');")
    con.commit()
    con.close()
    migrated = Ledger(oldp)  # __init__ -> ensure_schema -> _migrate_notifications
    with migrated._conn() as c:
        cols = {row[1] for row in c.execute("PRAGMA table_info(notifications)").fetchall()}
    r.ok("[H] the migration adds the stage column", "stage" in cols, cols)
    r.ok("[H] the migration keeps the old row at stage=''",
         migrated.last_notified_hash("2026-06-14", "digest", "") == "oldhash")

# ---- J. import graph: no research module loads lib.telegram ------------------------------
_RESEARCH = ("edgar", "sections", "xbrl", "valuation", "transcripts", "news", "etf", "documents",
             "findings", "events", "levels")
for m in _RESEARCH:
    code = (f"import sys; import lib.desk.{m}; "
            "print('LOADED' if 'lib.telegram' in sys.modules else 'CLEAN')")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(_REPO), capture_output=True, text=True,
                          timeout=120)
    r.ok(f"[J] importing lib.desk.{m} does not load lib.telegram",
         proc.stdout.strip() == "CLEAN", proc.stdout + proc.stderr[-300:])
for m in ("tick", "lib.notify"):
    proc = subprocess.run([sys.executable, "-c", f"import sys; import {m}; "
                           "print('LOADED' if 'lib.telegram' in sys.modules else 'CLEAN')"],
                          cwd=str(_REPO), capture_output=True, text=True, timeout=120)
    r.ok(f"[J] importing {m} does not load lib.telegram (tick imports it inside send-test only)",
         proc.stdout.strip() == "CLEAN", proc.stdout + proc.stderr[-300:])

raise SystemExit(r.done())
