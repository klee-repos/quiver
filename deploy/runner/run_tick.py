#!/usr/bin/env python3
"""Headless tick supervisor — the BRAX pattern adapted to Quiver, in Python.

On each scheduled wake (systemd timer): acquire the single-run lock, run
`tick.py preflight`, and — ONLY if it says proceed — drive exactly one tick by
running the `claude` CLI in headless `-p` mode over TICK.md, with the Robinhood +
Resend MCPs attached and the PreToolUse order-guard denying any unauthorized order.

Execution-only: ZERO trading logic lives here. Mirrors BRAX's `spawnClaude` shape
(`claude -p --append-system-prompt <rules> --mcp-config mcp.json --settings <hooks>
"Follow ./TICK.md exactly"`), with a wall-clock timeout as the floor and an
AUTH_ERROR -> hard-stop posture (never trade blind). The fine-grained stream-idle /
poll-wedge watchdogs from BRAX are a future enhancement; the timeout bounds a wedge.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lib import market, runlock  # noqa: E402
from lib import mailer, notify  # noqa: E402 — ops-layer last-resort alerting
from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
from lib.prompts import load_prompt  # noqa: E402

# The unique sentinel `tick.py auth-stop` prints on a real broker 401 (tick.AUTH_STOP_SENTINEL).
# It is NOT in TICK.md prose, so its presence in the orchestrator's captured stdout means an
# auth hard-stop ACTUALLY happened — unlike the literal "AUTH_ERROR" the runbook legitimately
# contains. Imported with a string fallback so the supervisor never fails to start against an
# older tick.py.
try:
    from tick import AUTH_STOP_SENTINEL  # noqa: E402
except Exception:  # noqa: BLE001 — keep the supervisor importable regardless
    AUTH_STOP_SENTINEL = "QUIVER_AUTH_STOP"

VENV_PY = os.environ.get("QUIVER_PYTHON", str(_REPO / ".venv" / "bin" / "python"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
MCP_CONFIG = os.environ.get("QUIVER_MCP_CONFIG", str(_REPO / "deploy" / "runner" / "mcp.json"))
SETTINGS = os.environ.get("QUIVER_CLAUDE_SETTINGS", str(_REPO / "deploy" / "runner" / "settings.json"))
# Orchestrator model. Python does ALL the thinking; this pass only shuttles JSON and
# invokes the broker MCP. It MUST reliably invoke deferred MCP tools (ToolSearch-load then
# native tool-call) on a full rebalance tick — haiku-4-5 could not (it tried to call the
# Robinhood MCP via Bash/python and gave up, blocking the tick), so we use Sonnet, which
# handles deferred-tool invocation + the multi-order rebalance tick reliably. Override via
# QUIVER_MODEL if a cheaper model proves reliable for the now-simpler classic path.
MODEL = os.environ.get("QUIVER_MODEL", "claude-sonnet-4-6")
EFFORT = os.environ.get("QUIVER_EFFORT", "low")  # ultracode OFF; Python does the thinking
TICK_TIMEOUT_SEC = int(os.environ.get("QUIVER_TICK_TIMEOUT_SEC", "2400"))  # ~40 min ceiling
# Clean per-tick status lines are appended here too (best-effort) so the log agent
# on the box can tail a stable file and the log-based metric alarms have data to
# fire on. Defaults to a repo-relative, gitignored path so LOCAL runs are unaffected;
# the box overrides it to /var/log/quiver/tick.log (see deploy/quiver.service).
TICK_LOG = os.environ.get("QUIVER_TICK_LOG", str(_REPO / "logs" / "tick.log"))

# The execution-orchestrator rules live in prompts/orchestrator.md (edit there).
SYSTEM_PROMPT = load_prompt("orchestrator").strip()


def _tick_json(args):
    """Run a tick.py subcommand and return its one-line JSON (or an error dict)."""
    p = subprocess.run([VENV_PY, str(_REPO / "tick.py"), *args],
                       capture_output=True, text=True, cwd=str(_REPO))
    out = (p.stdout or "").strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else {"error": (p.stderr or "")[:300]}
    except (ValueError, IndexError):
        return {"error": (p.stderr or p.stdout or "")[:300]}


# Keys too noisy for the shipped tick log (raw orchestrator chatter) — kept on stdout
# (journald) for debugging but stripped from the file copy so the metric filters stay
# precise (a benign "error" in the tail must not page the plan-error alarm). The
# last-resort alert's raw failure text (alert_detail) AND the alert `kind` are also
# stripped: both routinely carry the literal substring "error" (alert_detail in the
# message, kind as "error"/"auth_error") which would otherwise trip the plan-error
# metric filter on a benign alert line. The terse `alert_*` event names + the `stage`
# (broker_auth/daily_loss_halt/orchestrator/...) — none of which contain "error" — ARE
# shipped, so a separate "the pager is down" alarm can still key on alert_failed /
# alert_unconfigured without double-firing plan_error.
_FILE_SKIP_KEYS = ("tail", "alert_detail", "kind")


def _log_line(text):
    """Append one line to TICK_LOG. Best-effort: a logging hiccup never breaks a tick."""
    try:
        p = Path(TICK_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def _emit(d):
    try:
        print(json.dumps(d))  # -> stdout/journald (full detail, including any tail)
    except OSError:
        pass  # a closed/broken stdout (piped, parent exited) must never crash the run
    _log_line(json.dumps({k: v for k, v in d.items() if k not in _FILE_SKIP_KEYS}))


def _alert_target():
    """Resolve alert recipients / from / dry_run DEFENSIVELY (env first, then config).

    Must work even when config validation is the very thing that's broken (a missing
    NOTIFY_TO makes load_config raise) — that's a common reason the orchestrator died,
    so we never let a config error stop us from paging. Env wins; config fills gaps.
    Returns (recipients, from_addr, on_error, dry_run).
    """
    cfg = None
    try:
        cfg = load_config(_REPO / "config.yaml")
    except Exception:  # noqa: BLE001 — a broken config must not silence the pager
        cfg = None
    to = (os.environ.get("NOTIFY_ALERTS_TO", "").strip()
          or os.environ.get("NOTIFY_TO", "").strip())
    recips = [a.strip() for a in to.split(",") if a.strip()]
    if not recips and cfg is not None:
        recips = list(cfg.notify.alerts_to or cfg.notify.to)
    from_addr = os.environ.get("RESEND_FROM", "").strip()
    if not from_addr and cfg is not None:
        from_addr = cfg.notify.from_addr
    on_error = True if cfg is None else bool(cfg.notify.enabled and cfg.notify.on_error)
    dry_run = bool(cfg.dry_run) if cfg is not None else False
    return recips, from_addr, on_error, dry_run


def _in_tick_error_paged(led, day) -> bool:
    """True if the in-tick orchestrator already paged a hard-stop error this day
    (preflight/plan/commit). Used to suppress the generic 'orchestrator' last-resort
    alert so the same failure isn't double-paged under a different stage. Best-effort."""
    try:
        return any(led.last_notified_hash(day, "error", s)
                   for s in ("preflight", "plan", "commit"))
    except Exception:  # noqa: BLE001 — observability only; never block
        return False


def _maybe_alert(led, *, kind, stage, day, now_iso, event_detail, send=mailer.send_email):
    """Last-resort operator alert for a failure the in-tick orchestrator couldn't email.

    BEST-EFFORT: never raises, never changes the caller's exit code. Dedups against the
    SAME (date, kind, stage) notifications row the in-tick sender uses, so if the
    orchestrator already paged this event there is no double email; if it died before
    paging, this fires. ``send`` is injectable so the path is testable with zero network.
    """
    try:
        recips, from_addr, on_error, dry_run = _alert_target()
        if not on_error:
            _emit({"event": "alert_skipped", "kind": kind, "stage": stage,
                   "reason": "disabled"})
            return
        api_key = os.environ.get("RESEND_API_KEY", "").strip()
        if not api_key or not from_addr or not recips:
            _emit({"event": "alert_unconfigured", "kind": kind, "stage": stage,
                   "reason": ("no_key" if not api_key
                              else "no_from" if not from_addr else "no_recipients")})
            return
        model = {
            "date": day, "now_iso": now_iso, "kind": kind, "stage": stage,
            "severity": "critical", "dry_run": dry_run, "event_detail": event_detail,
            "subject_prefix": os.environ.get("QUIVER_SUBJECT_PREFIX", "[Quiver]"),
            "host": os.environ.get("QUIVER_HOST_HINT"), "tickers": [],
        }
        built = notify.build_digest(model)
        if led.last_notified_hash(day, kind, stage) == built["content_hash"]:
            _emit({"event": "alert_skipped", "kind": kind, "stage": stage,
                   "reason": "already_sent"})
            return
        res = send(api_key=api_key, from_addr=from_addr, to=recips,
                   subject=built["subject"], html=built["html"], text=built["text"])
        if res.get("ok"):
            led.mark_notified(day, kind, built["content_hash"], ",".join(recips),
                              now_iso, stage=stage)
            _emit({"event": "alert_sent", "kind": kind, "stage": stage,
                   "id": res.get("id", "")})
        else:
            _emit({"event": "alert_failed", "kind": kind, "stage": stage,
                   "alert_detail": res.get("error", "")})
    except Exception as e:  # noqa: BLE001 — the pager must never crash the supervisor
        _emit({"event": "alert_failed", "kind": kind, "stage": stage,
               "alert_detail": f"{type(e).__name__}: {e}"})


def main() -> int:
    now_iso = market.now_et().isoformat()
    day = market.trading_day_et()
    led = Ledger(_REPO / "state" / "ledger.db")
    holder = f"run-{now_iso}"
    try:
        with runlock.run_lock(led, holder, now_iso):
            pre = _tick_json(["preflight"])
            if pre.get("error"):
                _emit({"stage": "preflight", "stopped": True, **pre})
                # preflight itself errored → the orchestrator never ran → last-resort page.
                _maybe_alert(led, kind="error", stage="preflight", day=day,
                             now_iso=now_iso, event_detail=str(pre.get("error"))[:500])
                return 1
            if not pre.get("proceed"):
                _emit({"stage": "preflight", "proceed": False, "reason": pre.get("reason", "")})
                return 0  # cheap no-op wake — nothing to do (NOT a failure; never page)
            # Drive ONE tick through the claude CLI over TICK.md (execution only).
            cmd = [
                CLAUDE_BIN, "-p", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose",
                "--model", MODEL, "--effort", EFFORT,
                # NO --strict-mcp-config: the Robinhood MCP is registered at USER scope and
                # carries its OAuth (done once in the on-box browser); strict mode would
                # ignore user-scope servers + their stored OAuth. --mcp-config still layers
                # in resend (static key). The box's quiver user has no other MCP servers, so
                # dropping strict mode adds no stray servers.
                "--mcp-config", MCP_CONFIG,
                "--settings", SETTINGS, "--add-dir", str(_REPO),
                "--max-turns", "80", "--append-system-prompt", SYSTEM_PROMPT,
                "--dangerously-skip-permissions",  # unattended; the order guard is the real gate
                "Follow ./TICK.md exactly for today's tick.",
            ]
            try:
                proc = subprocess.run(cmd, cwd=str(_REPO), timeout=TICK_TIMEOUT_SEC,
                                      capture_output=True, text=True)
                ok = proc.returncode == 0
                combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
                _emit({"stage": "tick", "ok": ok, "returncode": proc.returncode,
                       "tail": combined[-400:]})
                # Surface the deterministic alarm signals into the shipped tick log as
                # clean lines the log-based metric filters key on. AUTH_ERROR is logged
                # by the orchestrator (TICK.md) in its transcript; a daily-loss halt is
                # recorded by Python in the ledger (tick.py plan -> mark_halted). The raw
                # transcript itself is never shipped — that keeps secrets/positions out
                # of the shipped logs and the plan-error ("error") filter precise.
                # Collision-free auth hard-stop detection. The literal "AUTH_ERROR" appears
                # in TICK.md prose (the orchestrator Reads the runbook into stdout) and would
                # false-positive on EVERY healthy tick. Match instead on the UNIQUE sentinel
                # the orchestrator emits ONLY by running `tick.py auth-stop` on a real 401. OR
                # with the ledger marker so a 401 whose (best-effort) in-tick email FAILED to
                # send — leaving no notifications row — is still caught (no silent miss). The
                # ledger read is wrapped: a DB hiccup must never crash the supervisor.
                try:
                    auth_marked = bool(
                        led.last_notified_hash(day, "auth_error", notify.AUTH_STAGE))
                except Exception:  # noqa: BLE001 — observability only; never block
                    auth_marked = False
                auth_error = (AUTH_STOP_SENTINEL in combined) or auth_marked
                try:
                    halted = bool(led.is_halted(day))
                except Exception:  # noqa: BLE001 — observability only, never block a tick
                    halted = False
                if auth_error:
                    _emit({"stage": "tick", "event": "AUTH_ERROR",
                           "detail": "Robinhood MCP auth failed — placed nothing, hard-stop"})
                    # Dedups against the orchestrator's own in-tick auth_error email
                    # (TICK.md STEP 2.1) via the shared (date, kind, stage) row.
                    _maybe_alert(led, kind="auth_error", stage=notify.AUTH_STAGE, day=day,
                                 now_iso=now_iso,
                                 event_detail="Robinhood MCP auth failed — token expired; "
                                              "placed nothing, hard-stop.")
                if halted:
                    _emit({"stage": "tick", "event": "daily_halt", "write_kill": True,
                           "detail": "daily-loss halt fired this tick"})
                    _maybe_alert(led, kind="halt", stage=notify.HALT_STAGE, day=day,
                                 now_iso=now_iso,
                                 event_detail="Daily-loss halt fired this tick; KILL written.")
                # A non-zero/failed orchestrator that is NEITHER auth nor halt is a crash
                # the orchestrator couldn't email about (max-turns, MCP wedge, exception).
                # But if the in-tick path ALREADY paged a hard-stop error this day
                # (preflight/plan/commit), suppress this generic page — its stage differs
                # ("orchestrator") so it would NOT dedup against that row and would
                # double-page the same logical failure.
                if not ok and not auth_error and not halted and not _in_tick_error_paged(led, day):
                    _maybe_alert(led, kind="error", stage="orchestrator", day=day,
                                 now_iso=now_iso,
                                 event_detail=f"orchestrator exited {proc.returncode}; "
                                              f"tail: {combined[-300:]}")
                # AUTH_ERROR is a hard-stop posture: exit non-zero so systemd + the
                # documented drill see a failed run even if the orchestrator exited 0.
                return 0 if (ok and not auth_error) else 1
            except subprocess.TimeoutExpired:
                _emit({"stage": "tick", "ok": False, "error": "tick exceeded the timeout"})
                # The subprocess wedged past the wall-clock — it certainly never reached
                # its own email step; last-resort page.
                _maybe_alert(led, kind="error", stage="orchestrator", day=day,
                             now_iso=now_iso,
                             event_detail=f"tick exceeded the {TICK_TIMEOUT_SEC}s timeout "
                                          "(orchestrator wedged).")
                return 1
    except runlock.RunLockError as e:
        _emit({"stage": "lock", "skipped": True, "reason": str(e)})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
