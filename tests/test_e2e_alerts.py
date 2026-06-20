#!/usr/bin/env python3
"""End-to-end coverage for the email ALERTING logic — WHEN emails are sent.

Drives the real deterministic pipeline offline (no network, no `claude`, no MCP):

  * tick.py `_run_report` — the should_send / dedup / per-event-toggle / recipient-
    routing decisions, across the run-complete + alert scenarios.
  * run_tick.py `_maybe_alert` — the headless last-resort sender: dedup against the
    SAME ledger row the in-tick path uses, injectable transport, never-raises, and the
    "pager unconfigured" path.
  * the ledger stage-keyed dedup + the (date, kind) -> (date, kind, stage) migration.
  * lib/mailer payload construction + best-effort no-raise (QUIVER_MAILER_DISABLE).
  * the import-graph invariant: the trading brain never imports lib.mailer.

Run: .venv/bin/python tests/test_e2e_alerts.py
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import yaml  # noqa: E402

import tick as T  # noqa: E402
from lib import mailer, notify  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

PASS = 0
FAIL = 0


def ok(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}")


def _cfg(notify_block):
    for _k in ("NOTIFY_TO", "NOTIFY_ALERTS_TO"):  # env-isolate config assertions
        os.environ.pop(_k, None)
    d = {
        "account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/alerts_kill",
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                 "daily_capital_deploy_cap": 75,
                 "min_buying_power_buffer": 5},
        "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
        "notify": notify_block,
    }
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(d, p)
    p.close()
    return load_config(p.name)


def _led():
    return Ledger(Path(tempfile.mkdtemp()) / "alerts_ledger.db")


def _run_tick_module():
    """Import deploy/runner/run_tick.py as a module (resolves _REPO via __file__)."""
    spec = importlib.util.spec_from_file_location(
        "run_tick_under_test", _REPO / "deploy" / "runner" / "run_tick.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DAY = "2026-06-14"
NOW = "2026-06-14T11:00:00-04:00"
ENABLED = {"enabled": True, "to": ["digest@x.com"], "from": "bot@quiver.dev",
           "on_complete": True, "on_error": True, "on_warning": False,
           "alerts_to": ["pager@x.com"]}


def _digest_data(tickers=True):
    plan = {"dry_run": True, "decisions": []}
    if tickers:
        plan["decisions"] = [{"ticker": "AAPL", "signal": "Hold", "intent": "hold",
                              "status": "skipped", "detail": "hold"}]
    return {"date": DAY, "now_iso": NOW, "kind": "digest", "equity": 100.0, "plan": plan}


def _alert_data(kind, stage=None, severity=None, detail="boom"):
    return {"date": DAY, "now_iso": NOW, "kind": kind, "stage": stage,
            "severity": severity, "event_detail": detail}


def main() -> int:
    print("=" * 70)
    print("E2E ALERTS — when (and to whom) emails are sent")
    print("=" * 70)
    # Snapshot live-delivery creds NOW — section G mutates/pops these env vars.
    live_to = os.environ.get("QUIVER_E2E_LIVE_TO", "").strip()
    live_key = os.environ.get("RESEND_API_KEY", "").strip()
    live_from = os.environ.get("RESEND_FROM", "").strip()

    # ---- A. run-complete digest: send once, then dedup ----
    print("\n[A] run-complete digest gating + dedup")
    cfg, led = _cfg(ENABLED), _led()
    r1 = T._run_report(cfg, led, _digest_data())
    ok("digest should_send True (new)", r1["should_send"] and r1["reason"] == "new")
    ok("digest routes to `to` (not alerts_to)", r1["recipients"] == ["digest@x.com"])
    led.mark_notified(r1["date"], r1["kind"], r1["content_hash"], "digest@x.com", NOW,
                      stage=r1.get("stage", ""))
    r2 = T._run_report(cfg, led, _digest_data())
    ok("identical wake dedups (already_sent)",
       not r2["should_send"] and r2["reason"] == "already_sent")

    # ---- B. toggles + nothing-to-report ----
    print("\n[B] per-event toggles + nothing_to_report guard")
    cfg_oc, led_b = _cfg({**ENABLED, "on_complete": False}), _led()
    ok("on_complete False -> complete_disabled",
       T._run_report(cfg_oc, led_b, _digest_data())["reason"] == "complete_disabled")
    cfg_off, led_b2 = _cfg({**ENABLED, "enabled": False}), _led()
    ok("notify disabled -> notify_disabled",
       T._run_report(cfg_off, led_b2, _digest_data())["reason"] == "notify_disabled")
    ok("empty digest -> nothing_to_report",
       T._run_report(cfg, _led(), _digest_data(tickers=False))["reason"] == "nothing_to_report")

    # ---- C. critical alerts: halt / auth_error ----
    print("\n[C] critical alerts (halt, auth_error) gate on on_error + route to alerts_to")
    cfg, led_c = _cfg(ENABLED), _led()
    rh = T._run_report(cfg, led_c, _alert_data("halt", detail="-6.1%"))
    ok("halt should_send True", rh["should_send"])
    ok("halt stage derived = daily_loss_halt", rh["stage"] == "daily_loss_halt")
    ok("alert routes to alerts_to", rh["recipients"] == ["pager@x.com"])
    led_c.mark_notified(rh["date"], rh["kind"], rh["content_hash"], "p", NOW, stage=rh["stage"])
    ok("halt repeat dedups", not T._run_report(cfg, led_c, _alert_data("halt", detail="-9%"))["should_send"])
    ra = T._run_report(cfg, _led(), _alert_data("auth_error"))
    ok("auth_error should_send True + stage broker_auth",
       ra["should_send"] and ra["stage"] == "broker_auth" and ra["severity"] == "critical")

    # ---- D. hard errors: per-stage independence ----
    print("\n[D] hard errors page per (kind, stage), independently")
    cfg, led_d = _cfg(ENABLED), _led()
    rp = T._run_report(cfg, led_d, _alert_data("error", stage="plan", severity="critical"))
    ok("error/plan should_send True", rp["should_send"] and rp["stage"] == "plan")
    led_d.mark_notified(rp["date"], rp["kind"], rp["content_hash"], "p", NOW, stage=rp["stage"])
    ok("error/plan repeat (different detail) dedups — anti-spam",
       not T._run_report(cfg, led_d, _alert_data("error", stage="plan", detail="other"))["should_send"])
    rc = T._run_report(cfg, led_d, _alert_data("error", stage="commit", severity="critical"))
    ok("error/commit pages independently (different stage)", rc["should_send"])

    # ---- E. warning hiccups: off by default, opt-in via on_warning ----
    print("\n[E] warning hiccups suppressed by default, fold into digest, opt-in to email")
    cfg, led_e = _cfg(ENABLED), _led()
    rw = T._run_report(cfg, led_e, _alert_data("error", stage="prune", severity="warning"))
    ok("warning suppressed by default -> warning_disabled",
       not rw["should_send"] and rw["reason"] == "warning_disabled")
    cfg_w = _cfg({**ENABLED, "on_warning": True})
    ok("on_warning True -> warning emails",
       T._run_report(cfg_w, _led(), _alert_data("error", stage="prune", severity="warning"))["should_send"])
    # folded delivery: a hiccup rides along in the digest FYI section
    d = _digest_data()
    d["warnings"] = [{"stage": "reflect", "detail": "yfinance 403 blip"}]
    dr = T._run_report(cfg, _led(), d)
    ok("digest carries the FYI hiccup (folded delivery)",
       "yfinance 403 blip" in dr["html"] and "yfinance 403 blip" in dr["text"])

    # ---- F. cross-sender dedup: orchestrator + last-resort agree on the hash ----
    print("\n[F] cross-sender dedup: in-tick + last-resort produce the SAME hash")
    cfg, led_f = _cfg(ENABLED), _led()
    # orchestrator side: report auth_error, commit it (as TICK.md report-commit would)
    orch = T._run_report(cfg, led_f, _alert_data("auth_error"))
    led_f.mark_notified(orch["date"], orch["kind"], orch["content_hash"], "p", NOW,
                        stage=orch["stage"])
    # last-resort side: reconstruct the SAME event's model (dry_run must match cfg.dry_run)
    lr_model = {"date": DAY, "now_iso": NOW, "kind": "auth_error", "stage": "broker_auth",
                "severity": "critical", "dry_run": cfg.dry_run, "event_detail": "AUTH_ERROR",
                "subject_prefix": "[Quiver]", "tickers": []}
    lr_hash = notify.build_digest(lr_model)["content_hash"]
    ok("orchestrator + last-resort hashes match (no double page)",
       lr_hash == orch["content_hash"])
    ok("last-resort sees the row already present -> would skip",
       led_f.last_notified_hash(DAY, "auth_error", "broker_auth") == lr_hash)

    # ---- G. run_tick last-resort: inject + dedup + never-raise + unconfigured ----
    print("\n[G] run_tick._maybe_alert (the headless safety net)")
    rt = _run_tick_module()
    led_g = _led()
    os.environ.update(NOTIFY_ALERTS_TO="me@x.com", RESEND_FROM="bot@q.dev", RESEND_API_KEY="k")
    sent = []
    rt._maybe_alert(led_g, kind="auth_error", stage="broker_auth", day=DAY, now_iso=NOW,
                    event_detail="401", send=lambda **k: sent.append(k) or {"ok": True, "id": "1"})
    ok("last-resort sends once", len(sent) == 1)
    ok("alert copy is actionable (/mcp + remote desktop in html & text)",
       "/mcp" in sent[0]["text"] and "remotedesktop.google.com" in sent[0]["html"])
    rt._maybe_alert(led_g, kind="auth_error", stage="broker_auth", day=DAY, now_iso=NOW,
                    event_detail="different", send=lambda **k: sent.append(k) or {"ok": True})
    ok("last-resort dedups against the shared row (no double page)", len(sent) == 1)
    rt._maybe_alert(led_g, kind="error", stage="plan", day=DAY, now_iso=NOW,
                    event_detail="x", send=lambda **k: sent.append(k) or {"ok": True})
    ok("different stage pages independently", len(sent) == 2)

    def _boom(**k):
        raise RuntimeError("network down")
    before = len(sent)
    rt._maybe_alert(led_g, kind="error", stage="commit", day=DAY, now_iso=NOW,
                    event_detail="y", send=_boom)  # must not raise
    ok("a raising transport is swallowed (never crashes the supervisor)", len(sent) == before)
    # Simulate an UNCONFIGURED box by blanking the key (not popping it): _maybe_alert
    # calls load_config -> load_dotenv, which would re-inject RESEND_API_KEY from a real
    # .env (dotenv only fills UNSET vars; override=False leaves a present-but-empty one).
    os.environ["RESEND_API_KEY"] = ""
    rt._maybe_alert(led_g, kind="error", stage="reflect", day=DAY, now_iso=NOW,
                    event_detail="z", send=lambda **k: sent.append(k) or {"ok": True})
    ok("unconfigured (no key) -> no send, no raise", len(sent) == before)
    os.environ.pop("RESEND_API_KEY", None)
    os.environ.pop("NOTIFY_ALERTS_TO", None)
    os.environ.pop("RESEND_FROM", None)
    # generic orchestrator-crash alert is suppressed when the in-tick path already paged
    # a hard-stop error (so it can't double-page the same failure under a different stage)
    led_gate = _led()
    ok("no in-tick error -> generic orchestrator alert allowed",
       rt._in_tick_error_paged(led_gate, DAY) is False)
    led_gate.mark_notified(DAY, "error", "h", "p", NOW, stage="plan")
    ok("in-tick plan error -> generic orchestrator alert suppressed",
       rt._in_tick_error_paged(led_gate, DAY) is True)

    # ---- H. ledger: stage isolation + (date,kind)->(date,kind,stage) migration ----
    print("\n[H] ledger stage-keyed dedup + schema migration preserves old rows")
    led_h = _led()
    led_h.mark_notified(DAY, "error", "h_plan", "p", NOW, stage="plan")
    led_h.mark_notified(DAY, "error", "h_prune", "p", NOW, stage="prune")
    ok("same kind, different stage -> independent rows (no clobber)",
       led_h.last_notified_hash(DAY, "error", "plan") == "h_plan"
       and led_h.last_notified_hash(DAY, "error", "prune") == "h_prune")
    ok("digest stage='' isolated", led_h.last_notified_hash(DAY, "digest") is None)
    # migration: build an OLD (date,kind)-keyed table, open with Ledger, assert rebuild
    olddir = Path(tempfile.mkdtemp())
    oldp = olddir / "old.db"
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
        cols = {r[1] for r in c.execute("PRAGMA table_info(notifications)").fetchall()}
    ok("migration adds the stage column", "stage" in cols)
    ok("migration preserves the old row at stage=''",
       migrated.last_notified_hash("2026-06-14", "digest", "") == "oldhash")

    # ---- I. lib/mailer: payload + best-effort no-raise + blank-from ----
    print("\n[I] lib/mailer payload construction + best-effort guards")
    pay = mailer.build_payload(from_addr="b@q.dev", to="x@a.com,y@a.com",
                               subject="s", html="<p>h</p>", text="t")
    ok("payload normalizes recipients + carries all fields",
       pay["to"] == ["x@a.com", "y@a.com"] and pay["from"] == "b@q.dev" and pay["subject"] == "s")
    ok("blank from short-circuits (HTTP api needs a sender)",
       mailer.send_email(api_key="k", from_addr="", to=["x@a.com"], subject="s",
                         html="h", text="t")["ok"] is False)
    ok("missing key short-circuits",
       mailer.send_email(api_key="", from_addr="b@q.dev", to=["x@a.com"], subject="s",
                         html="h", text="t")["ok"] is False)
    ok("non-iterable `to` -> {ok:False}, never raises (contract)",
       mailer.send_email(api_key="k", from_addr="b@q.dev", to=42, subject="s",
                         html="h", text="t").get("ok") is False)
    ok("junk recipients dropped (None/int filtered)",
       mailer.build_payload(from_addr="b@q.dev", to=["a@b.com", None, 123], subject="s",
                            html="h", text="t")["to"] == ["a@b.com"])
    os.environ["QUIVER_MAILER_DISABLE"] = "1"
    dis = mailer.send_email(api_key="k", from_addr="b@q.dev", to=["x@a.com"], subject="s",
                            html="h", text="t")
    ok("QUIVER_MAILER_DISABLE=1 builds payload without sending",
       dis["ok"] and dis.get("disabled") and dis["payload"]["to"] == ["x@a.com"])
    os.environ.pop("QUIVER_MAILER_DISABLE", None)

    # ---- J. import-graph invariant: the trading brain never imports lib.mailer ----
    print("\n[J] import-graph: lib.mailer is ops-layer only (not in the trading brain)")
    code = ("import sys; import tick, lib.signals, lib.memory; "
            "bad=[m for m in sys.modules if m=='lib.mailer']; "
            "print('LOADED' if bad else 'CLEAN')")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(_REPO),
                          capture_output=True, text=True)
    ok("importing tick/signals/memory does NOT load lib.mailer",
       "CLEAN" in proc.stdout and "LOADED" not in proc.stdout)

    # ---- K. LIVE delivery (opt-in) — actually send one of EACH kind ----
    # Strictly opt-in so the default suite stays offline (CLAUDE.md): set
    #   QUIVER_E2E_LIVE_TO=you@example.com  RESEND_API_KEY=...  RESEND_FROM=<verified sender>
    # and each kind is rendered + sent through the PRODUCTION lib.mailer HTTP path (the
    # same path the headless last-resort sender uses) — the real deploy acceptance gate.
    if live_to:
        print(f"\n[K] LIVE delivery -> {live_to} (production lib.mailer HTTP path)")
        ok("RESEND_API_KEY present for live send", bool(live_key))
        ok("RESEND_FROM present for live send", bool(live_from))
        for kind, m in _LIVE_SAMPLES().items():
            built = notify.build_digest(m)
            res = mailer.send_email(api_key=live_key, from_addr=live_from, to=[live_to],
                                    subject="[E2E] " + built["subject"],
                                    html=built["html"], text=built["text"])
            ok(f"live send '{kind}' -> {res.get('id') or res.get('error')}", bool(res.get("ok")))

    print("\n" + "=" * 70)
    print(f"E2E ALERTS: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


def _LIVE_SAMPLES():
    """Representative model dicts (with tickers / real detail) for live delivery — one
    per email kind, so the inbox shows what each looks like in production."""
    return {
        "digest": {
            "date": DAY, "now_iso": "2026-06-14T16:00:00-04:00", "kind": "digest",
            "dry_run": False, "subject_prefix": "[Quiver]", "equity": 1042.55,
            "baseline_equity": 1024.00, "drop_pct": 1.81, "mailer_armed": True,
            "account_risk": {"drawdown_pct": 3.2, "sharpe": 1.1, "sharpe_n": 12},
            "warnings": [{"stage": "reflect", "detail": "yfinance 403 transient blip"}],
            "tickers": [
                {"ticker": "AAPL", "signal": "Buy", "intent": "buy", "status": "placed",
                 "amount": 25.0, "side": "buy", "broker_order_id": "abc123",
                 "decision": "Strong Services growth; phased entry into strength.",
                 "debate": "Bull wins on margin expansion; bear flags valuation."},
                {"ticker": "MSFT", "signal": "Hold", "intent": "hold", "status": "skipped",
                 "detail": "hold", "decision": "Fairly valued; await a pullback.",
                 "debate": "Stalemate."}]},
        "auth_error": {
            "date": DAY, "now_iso": NOW, "kind": "auth_error", "dry_run": False,
            "subject_prefix": "[Quiver]", "host": "ubuntu@52.1.2.3",
            "event_detail": "401 Unauthorized: OAuth token expired at 2026-06-14T09:31Z",
            "tickers": []},
        "halt": {
            "date": DAY, "now_iso": NOW, "kind": "halt", "dry_run": False,
            "subject_prefix": "[Quiver]", "halted": True,
            "halt_reason": "daily_loss -6.1% vs baseline", "equity": 961.0,
            "baseline_equity": 1024.0, "drop_pct": -6.1, "tickers": []},
        "error_critical": {
            "date": DAY, "now_iso": NOW, "kind": "error", "severity": "critical",
            "stage": "plan", "dry_run": False, "subject_prefix": "[Quiver]",
            "host": "ubuntu@52.1.2.3",
            "event_detail": "KeyError: 'buying_power' in tick.py plan (line 212)",
            "tickers": []},
        "error_warning": {
            "date": DAY, "now_iso": NOW, "kind": "error", "severity": "warning",
            "stage": "prune", "dry_run": True, "subject_prefix": "[Quiver]",
            "event_detail": "PermissionError pruning state/cache", "tickers": []},
    }


if __name__ == "__main__":
    raise SystemExit(main())
