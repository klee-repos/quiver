"""Helpers every runner shares: tick.py calls, the tick.log status line, and the pager.

The monitor, the worker, the broker runner and the chat bridge all page through
``_maybe_alert``. It dedups on the same ``(date, kind, stage)`` notifications row for
every sender, so one event pages once per day per stage. The Telegram keys come from the
``Secrets`` the caller passes (after ``scrub_process_env``) or, for a runner that keeps
its environment, from ``os.environ``.
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

from lib import notify, telegram  # noqa: E402
from lib.config import load_config  # noqa: E402

ROOT = Path(os.environ.get("QUIVER_ROOT") or _REPO)
VENV_PY = os.environ.get("QUIVER_PYTHON", str(_REPO / ".venv" / "bin" / "python"))
# The Ops Agent ships this file to the quiver_tick log. Local runs default to logs/tick.log.
TICK_LOG = os.environ.get("QUIVER_TICK_LOG", str(ROOT / "logs" / "tick.log"))


def _tick_json(args, timeout=None, env=None):
    """Run a tick.py subcommand and return its one-line JSON (or an error dict). An
    optional ``timeout`` (seconds) bounds a step that could stall on the network."""
    try:
        p = subprocess.run([VENV_PY, str(_REPO / "tick.py"), *args], capture_output=True, text=True,
                           cwd=str(_REPO), timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"error": f"{' '.join(args)} exceeded its {timeout}s outer timeout"}
    out = (p.stdout or "").strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else {"error": (p.stderr or "")[:300]}
    except (ValueError, IndexError):
        return {"error": (p.stderr or p.stdout or "")[:300]}


# Keys too noisy for the shipped tick log. The alert's raw text (alert_detail) and its
# `kind` can hold the word "error", which would trip the stage_error metric on a benign
# alert line. The terse event names and the stage ARE shipped.
_FILE_SKIP_KEYS = ("tail", "alert_detail", "kind")


def _log_line(text):
    """Append one line to TICK_LOG. Best-effort: a logging failure never stops a run."""
    try:
        p = Path(TICK_LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def _emit(d):
    try:
        print(json.dumps(d, default=str), flush=True)  # stdout/journald: full detail
    except OSError:
        pass
    _log_line(json.dumps({k: v for k, v in d.items() if k not in _FILE_SKIP_KEYS}, default=str))


def _alert_target(secrets=None):
    """``(token, chat_ids, on_error)``. Creds come from ``secrets`` (a dict or an object
    with the three Telegram attributes) or the environment, never from config. A broken
    config must not silence the pager: on_error defaults ON when config fails to load."""
    try:
        cfg = load_config(ROOT / "config.yaml")
    except Exception:  # noqa: BLE001
        cfg = None
    env = _secrets_env(secrets)
    token, chat_ids = telegram.resolve_env(env)
    on_error = True if cfg is None else bool(cfg.notify.enabled and cfg.notify.on_error)
    return token, chat_ids, on_error


def _secrets_env(secrets):
    if secrets is None:
        return None
    if isinstance(secrets, dict):
        return secrets
    return {k: getattr(secrets, a, "") or "" for k, a in (
        ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
        ("TELEGRAM_ALLOWED_CHAT_IDS", "telegram_allowed_chat_ids"),
        ("TELEGRAM_ALERT_CHAT_IDS", "telegram_alert_chat_ids"))}


def _maybe_alert(led, *, kind, stage, day, now_iso, event_detail, send=telegram.send_message,
                 secrets=None, severity="critical"):
    """Page the operator (Telegram). BEST-EFFORT: never raises. Dedups on the shared
    ``(date, kind, stage)`` row; the row is written only after the primary chat got it."""
    try:
        token, chat_ids, on_error = _alert_target(secrets)
        if not on_error:
            _emit({"event": "alert_skipped", "kind": kind, "stage": stage, "reason": "disabled"})
            return
        if not token or not chat_ids:
            _emit({"event": "alert_unconfigured", "kind": kind, "stage": stage,
                   "reason": ("no_token" if not token else "no_chats")})
            return
        model = {"date": day, "now_iso": now_iso, "kind": kind, "stage": stage, "severity": severity,
                 "event_detail": event_detail, "host": os.environ.get("QUIVER_HOST_HINT")}
        built = notify.build_alert(model)
        if led.last_notified_hash(day, kind, stage) == built["content_hash"]:
            _emit({"event": "alert_skipped", "kind": kind, "stage": stage, "reason": "already_sent"})
            return
        res = send(token=token, chat_ids=chat_ids, text=built["telegram"], disable_notification=False)
        if res.get("ok"):
            led.mark_notified(day, kind, built["content_hash"], ",".join(chat_ids), now_iso, stage=stage)
            _emit({"event": "alert_sent", "kind": kind, "stage": stage})
            if res.get("partial"):
                _emit({"event": "alert_partial", "kind": kind, "stage": stage})
        else:
            _emit({"event": "alert_failed", "kind": kind, "stage": stage, "alert_detail": res.get("error", "")})
    except Exception as e:  # noqa: BLE001 — the pager must never crash a runner
        _emit({"event": "alert_failed", "kind": kind, "stage": stage, "alert_detail": f"{type(e).__name__}: {e}"})


# Text that marks a DEFINITE Claude login failure. Mirrors deploy/claude-update.sh.
_AUTH_FAIL_MARKERS = (
    "oauth", "authenticat", "401", "expired",
    "spend limit", "usage limit", "invalid api key",
)


def _orchestrator_auth_ok(claude_bin, timeout=120, env=None):
    """Probe a Claude login with one cheap prompt. ``(ok, detail)``. Only a DEFINITE auth
    failure returns False; a timeout or crash returns True (inconclusive)."""
    try:
        proc = subprocess.run([claude_bin, "-p", "reply with exactly: AUTH_OK"],
                              capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as e:  # noqa: BLE001
        return True, f"probe inconclusive: {type(e).__name__}: {e}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if "AUTH_OK" in out:
        return True, "ok"
    low = out.lower()
    if any(m in low for m in _AUTH_FAIL_MARKERS):
        return False, " ".join(out.split())[:300]
    return True, "probe inconclusive: " + " ".join(out.split())[:200]


def _orchestrator_reason(stdout):
    """The closing message of a ``claude -p --output-format stream-json`` run: the final
    ``result`` text, else the last assistant text. A one-liner (<= 300 chars) or None."""
    if not stdout:
        return None
    result_text = None
    assistant_text = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result":
            r = obj.get("result")
            if isinstance(r, str) and r.strip():
                result_text = r.strip()
        elif obj.get("type") == "assistant":
            for c in ((obj.get("message") or {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str) \
                        and c["text"].strip():
                    assistant_text = c["text"].strip()
    reason = result_text or assistant_text
    if not reason:
        return None
    return " ".join(reason.split())[:300]


def touch_trigger(root: Path, name: str, now_iso: str) -> None:
    """Create ``state/triggers/<name>`` with the UTC time. systemd starts the runner while
    the file exists (``PathExists=``); the runner deletes it first."""
    d = Path(root) / "state" / "triggers"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.tmp"
    tmp.write_text(now_iso + "\n", encoding="utf-8")
    os.replace(tmp, d / name)
