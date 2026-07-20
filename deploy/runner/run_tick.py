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
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lib import market, runlock  # noqa: E402
from lib import notify, telegram  # noqa: E402 — ops-layer last-resort alerting (Telegram egress)
from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402
from lib.prompts import load_prompt  # noqa: E402

# The Python-driven STEP 3 analysis fan-out (scripts/ is not a package). Run HERE, not in
# the headless orchestrator: the claude harness auto-backgrounds a long Bash command and the
# -p session then ends its turn, reaping the job (the 2026-06-17 + first-fix failures). Plain
# Python has no such harness, so it can block on the full-book analysis reliably.
sys.path.insert(0, str(_REPO / "scripts"))
import run_analyses  # noqa: E402

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
# Wall-clock ceiling for one orchestrator tick. Set DELIBERATELY HIGH (4h) so it is
# essentially never hit by a legitimate tick: a full rebalance analyzes the whole book
# (~11 GLM analyze.py runs) and ran ~40 min live on the e2-medium; a slow day with
# retries could be 60-90 min. The timeout is ONLY a last-resort backstop for a genuinely
# wedged orchestrator (MCP hang / infinite loop) — overlapping hourly timer fires no-op
# via the run_lock, so a long-but-legit tick is harmless. Override with QUIVER_TICK_TIMEOUT_SEC.
TICK_TIMEOUT_SEC = int(os.environ.get("QUIVER_TICK_TIMEOUT_SEC", "14400"))  # 4h backstop
# Clean per-tick status lines are appended here too (best-effort) so the log agent
# on the box can tail a stable file and the log-based metric alarms have data to
# fire on. Defaults to a repo-relative, gitignored path so LOCAL runs are unaffected;
# the box overrides it to /var/log/quiver/tick.log (see deploy/quiver.service).
TICK_LOG = os.environ.get("QUIVER_TICK_LOG", str(_REPO / "logs" / "tick.log"))

# The execution-orchestrator rules live in prompts/orchestrator.md (edit there).
SYSTEM_PROMPT = load_prompt("orchestrator").strip()


def _tick_json(args, timeout=None):
    """Run a tick.py subcommand and return its one-line JSON (or an error dict). An optional
    ``timeout`` (seconds) hard-bounds a heavy best-effort phase (e.g. bills-review's network +
    LLM work) so a wedged child can never hang the runner past the systemd margin."""
    try:
        p = subprocess.run([VENV_PY, str(_REPO / "tick.py"), *args],
                           capture_output=True, text=True, cwd=str(_REPO), timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"{' '.join(args)} exceeded its {timeout}s outer timeout"}
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
    """Resolve Telegram alert creds DEFENSIVELY (env for creds, config for the on_error toggle).

    Must work even when config validation is the very thing that's broken (a load_config raise
    is a common reason the orchestrator died) — so we never let a config error stop us from
    paging: creds come from the env (never config), and on_error defaults ON when cfg is None.
    Chat ids: TELEGRAM_ALERT_CHAT_IDS → TELEGRAM_ALLOWED_CHAT_IDS (lib.telegram.resolve_env).
    Returns (token, chat_ids, on_error, dry_run).
    """
    cfg = None
    try:
        cfg = load_config(_REPO / "config.yaml")
    except Exception:  # noqa: BLE001 — a broken config must not silence the pager
        cfg = None
    token, chat_ids = telegram.resolve_env()
    on_error = True if cfg is None else bool(cfg.notify.enabled and cfg.notify.on_error)
    dry_run = bool(cfg.dry_run) if cfg is not None else False
    return token, chat_ids, on_error, dry_run


def _brain_engine(cfg) -> str:
    """The configured brain engine for an alert payload. 'unknown' if cfg is None
    (load_config failed — the alert itself is the signal)."""
    try:
        return str(getattr(cfg, "brain_engine", "tradingagents") or "tradingagents")
    except Exception:  # noqa: BLE001
        return "unknown"


def _is_silent_noop(pending, recorded_before, recorded_after) -> bool:
    """True iff a PROCEEDED tick recorded NOTHING NEW this run despite pending tickers.

    Pure (unit-tested offline). ``recorded_*`` are ``count_decisions + count_ticker_actions``
    snapshots taken either side of the orchestrator run. Run-scoped via the delta, so it is
    correct in intraday mode (earlier ticks already wrote rows) and never false-trips a
    GLM-outage day (error analyses still add ticker_action rows -> delta > 0). A None
    snapshot (a DB read hiccup) -> not a noop (never false-trip)."""
    if not pending:
        return False
    if recorded_before is None or recorded_after is None:
        return False
    return (recorded_after - recorded_before) == 0


def _in_tick_error_paged(led, day) -> bool:
    """True if the in-tick orchestrator already paged a hard-stop error this day
    (preflight/plan/commit). Used to suppress the generic 'orchestrator' last-resort
    alert so the same failure isn't double-paged under a different stage. Best-effort."""
    try:
        return any(led.last_notified_hash(day, "error", s)
                   for s in ("preflight", "plan", "commit"))
    except Exception:  # noqa: BLE001 — observability only; never block
        return False


def _maybe_alert(led, *, kind, stage, day, now_iso, event_detail, send=telegram.send_message):
    """Last-resort operator alert (Telegram) for a failure the in-tick orchestrator couldn't send.

    BEST-EFFORT: never raises, never changes the caller's exit code. Dedups against the SAME
    (date, kind, stage) notifications row the in-tick sender uses, so if the orchestrator already
    paged this event there is no double message; if it died before paging, this fires. These are
    all CRITICAL alerts → sent LOUD (disable_notification False). ``send`` is injectable so the
    path is testable with zero network. The dedup row is written only after a confirmed delivery
    to the PRIMARY chat; a secondary-chat failure is surfaced as ``alert_partial`` (non-gating).
    """
    try:
        token, chat_ids, on_error, dry_run = _alert_target()
        if not on_error:
            _emit({"event": "alert_skipped", "kind": kind, "stage": stage,
                   "reason": "disabled"})
            return
        if not token or not chat_ids:
            _emit({"event": "alert_unconfigured", "kind": kind, "stage": stage,
                   "reason": ("no_token" if not token else "no_chats")})
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
        res = send(token=token, chat_ids=chat_ids, text=built["telegram"],
                   disable_notification=False)
        if res.get("ok"):
            led.mark_notified(day, kind, built["content_hash"], ",".join(chat_ids),
                              now_iso, stage=stage)
            _emit({"event": "alert_sent", "kind": kind, "stage": stage})
            if res.get("partial"):
                _emit({"event": "alert_partial", "kind": kind, "stage": stage})
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
    # Load config ONCE, DEFENSIVELY (mirror _alert_target): a broken/garbled config must never
    # crash a tick — it degrades to None and any config-derived label falls back safely. This is
    # the `cfg` the brain-outage page below reads (it previously referenced an undefined name).
    try:
        cfg = load_config(_REPO / "config.yaml")
    except Exception:  # noqa: BLE001 — a broken config must not crash the supervisor
        cfg = None
    holder = f"run-{now_iso}"
    try:
        # TTL must EXCEED the max tick wall-clock so a legit long tick (STEP 3 now blocks
        # on the full-book analysis; a slow retry-heavy day can run toward TICK_TIMEOUT_SEC)
        # is never seen as "stale" and stolen by the next hourly fire — that would start a
        # second overlapping tick and race the dedup/ref_id machinery. +600s grace past the
        # subprocess timeout (which itself releases the lock via the context manager on exit).
        with runlock.run_lock(led, holder, now_iso, ttl_seconds=TICK_TIMEOUT_SEC + 600):
            # ONE wall-clock budget for the whole locked section (fan-out + orchestrator).
            # The slow analysis fan-out runs BEFORE the orchestrator subprocess, so a fixed
            # per-each timeout lets fan-out + orchestrator SUM past systemd's TimeoutStartSec
            # and get SIGTERM'd mid-tick. Anchoring both to a single deadline keeps the total
            # within TICK_TIMEOUT_SEC (under the +600s systemd margin): whatever the fan-out
            # consumes is subtracted from the orchestrator's share below.
            _deadline = time.monotonic() + TICK_TIMEOUT_SEC
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

            # --- STEP 3: analysis fan-out (Python-driven; NOT the orchestrator) -------
            # Run the slow (~20-30 min) per-ticker analyze.py fan-out HERE, blocking, and
            # write the results to state/tmp/analyses.json. The orchestrator's STEP 3 then
            # just READS that file. This keeps the long job off the LLM, which cannot hold a
            # blocking call in headless -p mode (the harness auto-backgrounds it and the turn
            # ends -> reaped). analyze.py is still the decision-maker; only the INVOKER moves
            # from the LLM to Python. Always write the file (even []), so the orchestrator
            # always has a definite input. Best-effort on the fan-out itself: a crash yields
            # an empty list -> plan makes no buys (sells/reconcile still run) and the
            # silent-noop guard pages; never crash the supervisor here.
            pending = pre.get("pending") or []
            analyses = []
            if pending:
                # Per-ticker analyze timeout comes from config.yaml
                # (loop.analyze_timeout_sec — set very high so a normal high-reasoning
                # GLM run never hits it). QUIVER_ANALYZE_TIMEOUT still overrides inside
                # run_analyses. Reuses the single defensive `cfg` loaded at the top of
                # main(): a broken config (cfg is None) falls back to run_analyses' own
                # default rather than blocking the fan-out.
                _analyze_timeout = cfg.analyze_timeout_sec if cfg is not None else None
                try:
                    analyses = run_analyses.run(pending, timeout=_analyze_timeout)
                except Exception as e:  # noqa: BLE001 — never crash the supervisor on analysis
                    _emit({"stage": "analyze", "error": f"{type(e).__name__}: {e}"})
                    analyses = []
                n_err = sum(1 for a in analyses if (a or {}).get("signal") == "ERROR")
                _emit({"stage": "analyze", "count": len(analyses), "errors": n_err})
                # Brain-outage paging: if EVERY analysis this tick ERRORED (and there
                # was at least one), the brain is down (EVE subprocess crash / missing
                # node_modules / provider failure). The all-ERROR case records skip rows
                # with delta>0, so _is_silent_noop does NOT catch it — without this
                # page, a total brain outage on a live box is silent partial-blind
                # trading. Page it (best-effort, deduped by content_hash per stage/day).
                if analyses and n_err == len(analyses):
                    _maybe_alert(led, kind="error", stage="analyze", day=day, now_iso=now_iso,
                                 event_detail=f"brain outage: all {n_err} analyses ERROR (brain_engine={_brain_engine(cfg)})")
            try:
                _tmp = _REPO / "state" / "tmp"
                _tmp.mkdir(parents=True, exist_ok=True)
                (_tmp / "analyses.json").write_text(json.dumps(analyses))
            except Exception as e:  # noqa: BLE001 — OSError (fs) OR a json.dumps error: either
                # way the orchestrator's STEP 3 read fails and it STOPs — page it here too.
                _emit({"stage": "analyze", "error": f"could not write analyses.json: {e}"})
                _maybe_alert(led, kind="error", stage="analyze", day=day, now_iso=now_iso,
                             event_detail=f"could not write state/tmp/analyses.json: {e}")
                return 1

            # --- PTJ event-risk producer (F8/F9): write state/tmp/event_risk.json BEFORE the
            # orchestrator so plan can consume it. Runs as its OWN subprocess with an outer timeout
            # — a yfinance socket stall does NOT raise, so a try/except cannot bound it; only the
            # subprocess timeout can (B3). Best-effort: it always writes the sidecar ({} on failure),
            # and plan treats an absent/stale sidecar as empty -> byte-identical. Never blocks a tick.
            try:
                _er_budget = int(_deadline - time.monotonic())
                if _er_budget >= 30:
                    _er = _tick_json(["event-risk"], timeout=min(180, _er_budget))
                    _emit({"stage": "event-risk", **{k: _er[k] for k in ("written", "day", "error")
                                                     if k in _er}})
                else:
                    _emit({"stage": "event-risk", "skipped": "insufficient wall-clock budget"})
            except Exception as e:  # noqa: BLE001 — best-effort; a producer hiccup never blocks a tick
                _emit({"stage": "event-risk", "error": f"{type(e).__name__}: {e}"})

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
            # Snapshot how much the ledger has recorded for today BEFORE the orchestrator
            # runs, so the silent-noop guard below can measure a RUN-SCOPED delta (correct
            # even in intraday mode, where earlier ticks already wrote rows). Best-effort:
            # a read hiccup -> None -> the guard simply won't fire (never false-trip).
            try:
                recorded_before = led.count_decisions(day) + led.count_ticker_actions(day)
            except Exception:  # noqa: BLE001 — observability only; never block a tick
                recorded_before = None
            # The orchestrator gets whatever is LEFT of the shared budget after the fan-out
            # (floored so a budget-exhausted tick fails fast rather than running unbounded).
            _orch_timeout = max(60.0, _deadline - time.monotonic())
            try:
                proc = subprocess.run(cmd, cwd=str(_REPO), timeout=_orch_timeout,
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
                # Silent-noop guard: a tick that preflight let PROCEED with pending tickers
                # but recorded NOTHING NEW this run did no analysis — the 2026-06-17 failure
                # (analyses backgrounded then reaped when the orchestrator ended its turn).
                # The orchestrator exits 0, so without this it would page nothing. `plan`
                # writes a ticker_action for EVERY analysis it processes (real/skip/error)
                # and a decision for every valid (non-ERROR) signal, so a zero before->after
                # DELTA with a non-empty `pending` is an unambiguous "plan never ran". Using
                # the delta (not an absolute count) makes it RUN-SCOPED — correct in intraday
                # mode where earlier ticks already wrote rows — and a GLM-outage day still
                # adds error ticker_actions (delta > 0), so it won't false-trip. Only checked
                # on an otherwise-clean tick (auth/halt/crash already paged above).
                no_decisions = False
                try:
                    recorded_after = led.count_decisions(day) + led.count_ticker_actions(day)
                except Exception:  # noqa: BLE001 — observability only; never false-trip
                    recorded_after = None
                if ok and not auth_error and not halted and _is_silent_noop(
                        pre.get("pending"), recorded_before, recorded_after):
                    no_decisions = True
                    n = len(pre.get("pending") or [])
                    _emit({"stage": "tick", "event": "NO_DECISIONS",
                           "detail": f"proceeded with {n} pending tickers but recorded 0 new "
                                     "decisions/actions — analyses never ran (silent no-op)"})
                    _maybe_alert(led, kind="error", stage="no_decisions", day=day,
                                 now_iso=now_iso,
                                 event_detail=(f"Tick proceeded with {n} pending tickers but "
                                               "recorded nothing new (0 decisions, 0 actions) "
                                               "— the analysis step did not run (likely "
                                               "backgrounded + reaped). No trades placed."))
                # --- Learning loop (Python best-effort; NOT a skippable LLM step) ----------
                # After the orchestrator placed trades + recorded decisions, run the
                # goal-progress snapshot and the learning review HERE in Python so they ALWAYS
                # run — a TICK.md line the LLM can silently skip is not robust enough for the
                # "learns smarter trades daily" mandate. Both are best-effort + human-gated for
                # the risky direction (the screener PROPOSES adds; applying still needs
                # universe-apply --approve); neither places an order or mutates the universe
                # here. A hiccup is logged and never changes the tick's exit code.
                if ok and not auth_error and not halted:
                    for _step in ("goal-track", "learn-review"):
                        try:
                            _r = _tick_json([_step])
                            if _r.get("error"):
                                _emit({"stage": _step, "error": str(_r["error"])[:200]})
                            else:
                                _emit({"stage": _step, **{k: _r[k] for k in (
                                    "reviewed", "recorded", "new_proposals", "regime",
                                    "ahead_behind_pct", "cumulative_return_pct",
                                    "n_suspected_flows", "auto_captured") if k in _r}})
                        except Exception as e:  # noqa: BLE001 — learning is best-effort
                            _emit({"stage": _step, "error": f"{type(e).__name__}: {e}"})
                    # bills-review: heavier (Congress API + LLM analysis/judge), so it runs LAST
                    # with an EXPLICIT outer timeout floored by the remaining wall-clock budget,
                    # so a wedged fetch/LLM can never push the unit past the systemd margin. It
                    # only ingests + PROPOSES universe changes (human `universe-apply --approve`
                    # gated); OFF unless config.yaml legislative.enabled. Best-effort like the rest.
                    try:
                        _budget = int(_deadline - time.monotonic())
                        if _budget >= 60:
                            _r = _tick_json(["bills-review"], timeout=min(360, _budget))
                            if _r.get("error"):
                                _emit({"stage": "bills-review", "error": str(_r["error"])[:200]})
                            else:
                                _emit({"stage": "bills-review", **{k: _r[k] for k in (
                                    "reviewed", "changed", "analyzed", "judged", "minted") if k in _r}})
                        else:
                            _emit({"stage": "bills-review", "skipped": "insufficient wall-clock budget"})
                    except Exception as e:  # noqa: BLE001 — best-effort
                        _emit({"stage": "bills-review", "error": f"{type(e).__name__}: {e}"})

                # AUTH_ERROR is a hard-stop posture: exit non-zero so systemd + the
                # documented drill see a failed run even if the orchestrator exited 0.
                return 0 if (ok and not auth_error and not no_decisions) else 1
            except subprocess.TimeoutExpired:
                _emit({"stage": "tick", "ok": False, "error": "tick exceeded the timeout"})
                # The subprocess wedged past the wall-clock — it certainly never reached
                # its own email step; last-resort page.
                _maybe_alert(led, kind="error", stage="orchestrator", day=day,
                             now_iso=now_iso,
                             event_detail=f"tick exceeded its {int(_orch_timeout)}s orchestrator "
                                          f"budget (of the {TICK_TIMEOUT_SEC}s tick deadline; "
                                          "orchestrator wedged or fan-out consumed the budget).")
                return 1
            except Exception as e:  # noqa: BLE001 — a spawn failure (e.g. claude binary
                # missing/not executable) can't reach the orchestrator's own pager; page here.
                # The run_lock is still released by the context manager as this propagates out.
                _emit({"stage": "tick", "ok": False, "error": f"{type(e).__name__}: {e}"})
                _maybe_alert(led, kind="error", stage="orchestrator", day=day, now_iso=now_iso,
                             event_detail=f"orchestrator subprocess failed to launch: "
                                          f"{type(e).__name__}: {e}")
                return 1
    except runlock.RunLockError as e:
        _emit({"stage": "lock", "skipped": True, "reason": str(e)})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
