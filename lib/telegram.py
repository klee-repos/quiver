"""Last-resort Telegram sender — the bot's ONE alerting egress, ops-layer ONLY.

This is the Telegram twin of ``lib/mailer.py``: the single network path that ships the
bot's autonomous alerts (the daily digest + the halt / auth_error / error pages) to the
operator's Telegram chat, replacing email. Both senders use it — the in-tick path
(``tick.py report-send``) and the headless last-resort supervisor
(``deploy/runner/run_tick.py`` ``_maybe_alert``).

INVARIANT — read before importing this anywhere:
  * This is a DELIBERATE, narrow exception to the project rule "the trading path never
    sends." It is scoped to the ops/observability layer. ``lib/telegram`` MUST NEVER be
    imported by the trading brain (``analyze.py`` / ``lib.signals`` / ``lib.memory`` / the
    plan path); ``tick.py`` and ``run_tick.py`` import it LOCALLY inside their send funcs so
    ``import tick`` stays clean. An e2e test (``tests/test_e2e_alerts.py`` block J) asserts
    that import graph for BOTH ``lib.mailer`` and ``lib.telegram``.
  * ``lib/notify`` stays a pure renderer (it produces the Telegram-HTML string). This module
    is the only place that touches the Telegram network, and it is best-effort:
    ``send_message`` NEVER raises — a transport/DNS/TLS/4xx/5xx/parse failure is captured into
    a result dict so a send failure can never change a tick's exit code.

Telegram specifics this module owns (the "restructure for Telegram" contract):
  * the 4096-char hard cap — ``send_message`` splits ``text`` into ≤``_CHUNK`` pieces on line
    boundaries (an over-long single line is hard-split), so nothing is silently dropped;
  * HTML with a per-chunk PLAINTEXT fallback — a chunk is first POSTed with ``parse_mode=HTML``;
    if Telegram rejects the entities (a 400) it is re-POSTed as plain text, so a stray
    ``<``/``&`` never loses the whole message;
  * partial-delivery dedup semantics — ``ok`` (the flag the callers use to write the dedup
    row) tracks ONLY the PRIMARY (first) chat id receiving all its chunks. A blocked/kicked
    SECONDARY chat sets ``partial`` but does NOT flip ``ok`` — otherwise one dead extra chat
    would keep the ``(date,kind,stage)`` notifications row unwritten and re-spam the healthy
    primary on every hourly tick. (The email path returns one batch ``ok`` for the whole
    recipient list, so it never regresses this way; per-chat ``ok=all-chats`` would.)

No third-party deps (stdlib ``urllib``) so the systemd-sandboxed Python process needs nothing
extra. Set ``QUIVER_TELEGRAM_DISABLE=1`` to short-circuit the actual POST (used by the offline
tests to exercise chunking/formatting/dedup with zero network).
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import urllib.error
import urllib.request

# Telegram's hard cap on a text message is 4096 chars; chunk well under it to leave headroom
# for the <b>/<code>/<pre>/<a> tags and any entity expansion we add.
_HARD_CAP = 4096
_CHUNK = _HARD_CAP - 596  # 3500; headroom for tags/entities under the hard cap

_TAG_RE = re.compile(r"<[^>]+>")


def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _recipients(chat_ids) -> list:
    """Normalize ``chat_ids`` into a clean list of stringified ids. TOTAL — never raises
    (a non-iterable scalar becomes a single id; junk elements are dropped), so the
    ``send_message`` no-raise contract holds. Order is PRESERVED: the first id is the
    dedup-gating primary (the operator's own chat, by convention)."""
    if isinstance(chat_ids, str):
        items = re.split(r"[,\s]+", chat_ids)
    elif isinstance(chat_ids, (list, tuple)):
        items = list(chat_ids)
    elif isinstance(chat_ids, int):
        items = [chat_ids]
    else:
        items = []
    out = []
    for x in items:
        s = str(x).strip() if x is not None else ""
        if s:
            out.append(s)
    return out


def resolve_env(env=None) -> tuple:
    """The ops callers' shared cred lookup: resolve ``(token, [chat_id,...])`` from the process
    env. Chat ids come from ``TELEGRAM_ALERT_CHAT_IDS`` (optional alert-only override) else
    ``TELEGRAM_ALLOWED_CHAT_IDS`` — the var the chat bridge + box provisioning already set, so no
    NEW secret is needed to turn alerts on. Pure/total (never raises)."""
    e = env if env is not None else os.environ
    token = (e.get("TELEGRAM_BOT_TOKEN") or "").strip()
    raw = ((e.get("TELEGRAM_ALERT_CHAT_IDS") or "").strip()
           or (e.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip())
    return token, _recipients(raw)


def _chunks(text: str, limit: int = _CHUNK) -> list:
    """Split ``text`` at line boundaries so an HTML tag is never cut across two messages
    on the common path; an over-long single line is hard-split as a last resort. Ported
    from the proven ``deploy/runner/chat_bridge.py`` transport."""
    out, buf, cur = [], [], 0
    for ln in str(text or "").split("\n"):
        while len(ln) > limit:  # a single over-long line: hard-split
            out.append(ln[:limit])
            ln = ln[limit:]
        if cur + len(ln) + 1 > limit and buf:
            out.append("\n".join(buf))
            buf, cur = [], 0
        buf.append(ln)
        cur += len(ln) + 1
    if buf:
        out.append("\n".join(buf))
    return out or [text or ""]


def _strip_html(text: str) -> str:
    """Plaintext fallback: drop tags + unescape entities so a Telegram HTML parse failure
    still delivers a clean, readable message (no literal ``<b>`` / ``&amp;``)."""
    return _html.unescape(_TAG_RE.sub("", str(text or "")))


def _disabled() -> bool:
    """The offline escape hatch, gated on an EXPLICIT truthy value so a stray
    ``QUIVER_TELEGRAM_DISABLE=0/false`` can't silently mute the pager."""
    return os.environ.get("QUIVER_TELEGRAM_DISABLE", "").strip().lower() in (
        "1", "true", "yes", "on")


def post(*, token: str, chat_id: str, text: str, parse_mode: str = "HTML",
         disable_notification: bool = False, timeout: float = 10.0) -> dict:
    """POST ONE ``sendMessage`` to Telegram. BEST-EFFORT — never raises.

    ``parse_mode`` "" (falsy) omits the field → Telegram treats ``text`` as plain text.
    Returns ``{"ok": True, "id": <message id>}`` on Telegram ``ok:true``, else
    ``{"ok": False, "error": <reason>}``.
    """
    payload = {"chat_id": chat_id, "text": text,
               "disable_web_page_preview": True,
               "disable_notification": bool(disable_notification)}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _api_url(token, "sendMessage"), data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body) if body else {}
            except ValueError:
                parsed = {}
            if parsed.get("ok"):
                return {"ok": True, "id": (parsed.get("result") or {}).get("message_id", "")}
            return {"ok": False, "error": f"telegram not ok: {parsed.get('description') or body[:200]}"}
    except urllib.error.HTTPError as e:  # 4xx/5xx — capture the body for the operator
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 — best-effort detail only
            detail = ""
        return {"ok": False, "error": f"http {e.code}: {detail or e.reason}"}
    except Exception as e:  # noqa: BLE001 — DNS/TLS/timeout/socket: NEVER raise out of here
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _deliver_chunk(*, token, chat_id, chunk, parse_mode, disable_notification, timeout) -> dict:
    """Deliver one chunk to one chat: try HTML, then fall back to plain text on ANY failure
    so a stray entity never loses the message. Returns the winning ``post`` result + how."""
    res = post(token=token, chat_id=chat_id, text=chunk, parse_mode=parse_mode,
               disable_notification=disable_notification, timeout=timeout)
    if res.get("ok") or not parse_mode:
        return {**res, "via": parse_mode or "plain"}
    fb = post(token=token, chat_id=chat_id, text=_strip_html(chunk), parse_mode="",
              disable_notification=disable_notification, timeout=timeout)
    return {**fb, "via": "plain_fallback", "html_error": res.get("error")}


def send_message(*, token: str, chat_ids, text: str, parse_mode: str = "HTML",
                 disable_notification: bool = False, timeout: float = 10.0) -> dict:
    """Send an alert to Telegram. BEST-EFFORT — never raises.

    Chunks ``text`` to ≤``_CHUNK`` and POSTs every chunk to every chat id (HTML, plaintext
    fallback per chunk). Missing token / no chat ids short-circuit to ``{"ok": False, ...}``.

    ``ok`` is the DEDUP-GATING flag: True iff the PRIMARY (first) chat id received ALL its
    chunks. A failure on a SECONDARY chat sets ``partial`` but does NOT flip ``ok`` — so one
    blocked extra chat can't keep the notifications row unwritten and re-spam the primary.
    Returns ``{ok, partial, sent, chunks, results:[...], error?}``.
    """
    token = (token or "").strip()
    recips = _recipients(chat_ids)
    if not token:
        return {"ok": False, "partial": False, "sent": 0,
                "error": "telegram_unconfigured: no TELEGRAM_BOT_TOKEN"}
    if not recips:
        return {"ok": False, "partial": False, "sent": 0,
                "error": "telegram_unconfigured: no chat ids"}

    chunks = _chunks(text, _CHUNK)

    # Offline escape hatch for tests: build everything, send nothing. ok=True so the dedup
    # row still gets written on the offline path (mirrors lib/mailer's disabled return).
    if _disabled():
        return {"ok": True, "partial": False, "disabled": True, "sent": 0,
                "chunks": len(chunks), "chat_ids": recips}

    results = []
    delivered = 0
    for idx, chat_id in enumerate(recips):
        chat_ok = True
        chunk_results = []
        for chunk in chunks:
            r = _deliver_chunk(token=token, chat_id=chat_id, chunk=chunk, parse_mode=parse_mode,
                               disable_notification=disable_notification, timeout=timeout)
            chunk_results.append(r)
            if r.get("ok"):
                delivered += 1
            else:
                chat_ok = False
        results.append({"chat_id": chat_id, "ok": chat_ok, "primary": idx == 0,
                        "chunks": chunk_results})

    primary_ok = bool(results and results[0]["ok"])
    partial = any(not r["ok"] for r in results[1:])  # a SECONDARY chat failed
    out = {"ok": primary_ok, "partial": partial, "sent": delivered,
           "chunks": len(chunks), "results": results}
    if not primary_ok:
        # Surface the primary's first error so the caller can log something actionable.
        first_bad = next((c for c in results[0]["chunks"] if not c.get("ok")), {}) if results else {}
        out["error"] = first_bad.get("error") or "primary chat delivery failed"
    return out
