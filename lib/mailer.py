"""Last-resort HTTP email sender — the ONE network egress, ops-layer ONLY.

This module exists so the headless supervisor (``deploy/runner/run_tick.py``) can
email the operator when the Claude orchestrator never reaches its in-tick Resend-MCP
send — i.e. when the ``claude`` subprocess crashes, times out, or preflight itself
errors. Those are exactly the failures the operator most needs to hear about, and the
MCP path (driven from inside that dead subprocess) cannot cover them.

INVARIANT — read before importing this anywhere:
  * This is a DELIBERATE, narrow exception to the project rule "the trading path never
    sends; the orchestrator sends via the Resend MCP." It is scoped to the ops
    supervisor. ``lib/mailer`` MUST NEVER be imported by the trading brain
    (``analyze.py`` / ``lib.signals`` / ``lib.memory`` / the plan path). A unit test
    asserts that import graph.
  * ``lib/notify`` stays a pure renderer (no network). This module is the only place
    that touches the network, and it is best-effort: ``send_email`` NEVER raises — a
    transport/DNS/TLS/4xx/5xx failure returns ``{"ok": False, "error": ...}`` so a
    send failure can never change a tick's exit code.

No third-party deps (stdlib ``urllib``) so the systemd-sandboxed Python process needs
nothing extra. Set ``QUIVER_MAILER_DISABLE=1`` to short-circuit the actual POST (used
by the offline tests to exercise payload construction with zero network).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _recipients(to) -> list:
    """Normalize ``to`` into a clean list of address strings. TOTAL — never raises
    (a non-iterable scalar yields []), and drops non-str junk so one bad element
    can't poison the whole send. Keeps the ``send_email`` no-raise contract intact."""
    if isinstance(to, str):
        items = to.split(",")
    elif isinstance(to, (list, tuple, set)):
        items = list(to)
    else:
        items = []
    out = []
    for x in items:
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
    return out


def build_payload(*, from_addr: str, to, subject: str, html: str, text: str) -> dict:
    """The exact JSON body Resend's POST /emails expects. Pure — no I/O."""
    return {
        "from": from_addr,
        "to": _recipients(to),
        "subject": subject,
        "html": html,
        "text": text,
    }


def send_email(*, api_key: str, from_addr: str, to, subject: str, html: str,
               text: str, timeout: float = 10.0) -> dict:
    """POST one transactional email to Resend. BEST-EFFORT — never raises.

    Returns ``{"ok": True, "id": <message id>}`` on a 2xx, else
    ``{"ok": False, "error": <reason>}``. Missing key / blank ``from`` / empty
    recipients short-circuit to ``{"ok": False, ...}`` (the HTTP API, unlike the
    Resend MCP, has no implicit sender — a blank ``from`` would 422 at the worst
    possible moment, so we refuse loudly instead of firing a doomed request).
    """
    api_key = (api_key or "").strip()
    from_addr = (from_addr or "").strip()
    recips = _recipients(to)
    if not api_key:
        return {"ok": False, "error": "mailer_unconfigured: no RESEND_API_KEY"}
    if not from_addr:
        return {"ok": False, "error": "mailer_unconfigured: no verified from address"}
    if not recips:
        return {"ok": False, "error": "mailer_unconfigured: no recipients"}

    payload = build_payload(from_addr=from_addr, to=recips, subject=subject,
                            html=html, text=text)

    # Offline escape hatch for tests: build everything, send nothing. Gated on an
    # EXPLICIT truthy value so a stray QUIVER_MAILER_DISABLE=0/false can't silently
    # mute the last-resort pager (the worst failure for this module).
    if os.environ.get("QUIVER_MAILER_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return {"ok": True, "id": "disabled", "disabled": True, "payload": payload}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        RESEND_ENDPOINT, data=data, method="POST",
        # A real User-Agent is REQUIRED: the Resend API sits behind Cloudflare, which
        # 403s urllib's default "Python-urllib/x.y" UA (error 1010). Without this the
        # last-resort pager silently fails every send.
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "User-Agent": "quiver-mailer/1.0 (+https://github.com/klee-repos/quiver)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body) if body else {}
            except ValueError:
                parsed = {}
            return {"ok": True, "id": parsed.get("id", ""), "status": resp.status}
    except urllib.error.HTTPError as e:  # 4xx/5xx — capture the body for the operator
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 — best-effort detail only
            detail = ""
        return {"ok": False, "error": f"http {e.code}: {detail or e.reason}"}
    except Exception as e:  # noqa: BLE001 — DNS/TLS/timeout/socket: NEVER raise out of here
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
