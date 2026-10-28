"""Send to the owner and mirror every message into ``chat_messages`` (plan §4, §10.1 rule 3).

Every desk process sends through this module: the chat bridge, the worker, the broker
runner and the monitor. A send is one Telegram message; a long text is split. Each send
writes one ``chat_messages`` desk row with its source and its Telegram message id, so the
desk's next turn sees it in ``<desk_updates>`` and ``search_research`` can find it.

The transport is a callable ``transport(method, params) -> dict`` (the Bot API answer).
Production builds it from ``lib.telegram.api`` and the bot token in ``Secrets``; a test
passes a fake. HTML that Telegram rejects is sent again as plain text.
"""

from __future__ import annotations

import functools
import html as _html
import re
import time
from typing import Optional

from lib.desk import store

_TAG = re.compile(r"<[^>]+>")
_MIN_GAP_S = 0.8
_LIMIT = 4000


def telegram_transport(token: str):
    """The production transport over ``lib.telegram.api``."""
    from lib import telegram
    return functools.partial(telegram.api, token)


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def render_model_text(text: str) -> str:
    """Convert the model's markdown to the small subset Telegram renders (<b>, <code>,
    <pre>). Everything else is HTML-escaped or flattened. Links become `text (url)`, so a
    URL is never hidden behind text. The desk's `say` and the analyst's `notify_owner` both
    use it: raw HTML from a model (a planted `<a href>`) reaches the owner as plain text."""
    text = str(text or "").replace("\x00", "")  # NUL marks the stash below
    stash: list = []

    def _stash(tag, content):
        stash.append((tag, content))
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"```[^\n]*\n?(.*?)```", lambda m: _stash("pre", m.group(1).rstrip("\n")), text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", lambda m: _stash("code", m.group(1)), text)
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text)
    out = []
    for ln in text.split("\n"):
        s = ln.rstrip()
        stripped = s.strip()
        if stripped and set(stripped) <= set("|:- ") and "-" in stripped:
            continue
        h = re.match(r"#{1,6}\s+(.*)", stripped)
        if h:
            out.append(f"<b>{h.group(1)}</b>")
            continue
        s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)
        if s.count("|") >= 2:
            s = "  ".join(c.strip() for c in s.strip().strip("|").split("|"))
        out.append(s)
    text = "\n".join(out)
    text = re.sub(r"\x00(\d+)\x00",
                  lambda m: (lambda tg, c: f"<{tg}>{_html.escape(c, quote=False)}</{tg}>")(*stash[int(m.group(1))]),
                  text)
    return text


def strip_html(text: str) -> str:
    return _html.unescape(_TAG.sub("", str(text or "")))


def trade_buttons(proposal_id: int, nonce: str) -> list:
    """Approve (style success) and Reject (style danger). Each callback is under 64 bytes."""
    return [[{"text": "Approve", "callback_data": f"p:{proposal_id}:{nonce}:a", "style": "success"},
             {"text": "Reject", "callback_data": f"p:{proposal_id}:{nonce}:r", "style": "danger"}]]


def change_buttons(proposal_id: int, nonce: str) -> list:
    return [[{"text": "Confirm", "callback_data": f"p:{proposal_id}:{nonce}:a", "style": "success"},
             {"text": "Reject", "callback_data": f"p:{proposal_id}:{nonce}:r", "style": "danger"}]]


class Outbox:
    """One sender for one process. ``chat_id`` is the owner's private chat."""

    def __init__(self, led, *, transport, chat_id, clock=time.monotonic, sleep=time.sleep):
        self.led = led
        self.transport = transport
        self.chat_id = str(chat_id) if chat_id is not None else ""
        self.clock = clock
        self.sleep = sleep
        self._last = None

    def _pace(self) -> None:
        if self._last is not None:
            gap = self.clock() - self._last
            if gap < _MIN_GAP_S:
                self.sleep(_MIN_GAP_S - gap)
        self._last = self.clock()

    def _send_one(self, text: str, *, reply_to=None, buttons=None, html: bool = True) -> dict:
        self._pace()
        params = {"chat_id": self.chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if html:
            params["parse_mode"] = "HTML"
        if reply_to:
            params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        res = self.transport("sendMessage", params) or {}
        if not res.get("ok") and html and "parse" in str(res.get("description", "")).lower():
            params.pop("parse_mode", None)
            params["text"] = strip_html(text)
            res = self.transport("sendMessage", params) or {}
        return res

    def send(self, text: str, *, source: str, reply_to=None, buttons=None, html: bool = True,
             session_id: Optional[str] = None, delivered: int = 0) -> dict:
        """Send ``text`` (split when long) and mirror it. Returns ``{ok, message_id,
        message_ids}``. The buttons go on the last part."""
        if not self.chat_id:
            return {"ok": False, "error": "no owner chat id"}
        parts = _split(text)
        ids, ok = [], True
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            res = self._send_one(part, reply_to=reply_to if i == 0 else None,
                                 buttons=buttons if last else None, html=html)
            mid = ((res.get("result") or {}).get("message_id")) if res.get("ok") else None
            ok = ok and bool(res.get("ok"))
            ids.append(mid)
            with self.led.connect() as conn:
                store.insert_desk_message(conn, chat_id=self.chat_id, text=strip_html(part) if html else part,
                                          source=source, now=store.now_iso(), tg_message_id=mid,
                                          session_id=session_id, delivered=delivered)
        return {"ok": ok, "message_id": ids[-1] if ids else None, "message_ids": ids}

    def send_card(self, proposal_id: int, messages: list, *, kind: str, nonce: str) -> dict:
        """Send a proposal card (up to three messages); store the last message's id as the
        proposal's ``tg_message_id``, the one the tap must come from."""
        buttons = trade_buttons(proposal_id, nonce) if kind == "trade" else change_buttons(proposal_id, nonce)
        last = None
        for i, m in enumerate(messages):
            res = self.send(m, source="proposal", buttons=buttons if i == len(messages) - 1 else None)
            last = res
        mid = (last or {}).get("message_id")
        with self.led.connect() as conn:
            conn.execute("UPDATE proposals SET tg_chat_id=?, tg_message_id=? WHERE id=?",
                         (self.chat_id, mid, int(proposal_id)))
        return {"ok": bool((last or {}).get("ok")), "message_id": mid}

    def set_label(self, message_id, label: str) -> dict:
        """Replace a card's buttons with one label button ("Approved", "Expired", ...)."""
        if not message_id:
            return {"ok": False}
        return self.transport("editMessageReplyMarkup", {
            "chat_id": self.chat_id, "message_id": int(message_id),
            "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}}) or {}


def _split(text: str, limit: int = _LIMIT) -> list:
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def send_trade_card(led, box: Outbox, conn_factory, built: dict, *, now_dt=None) -> dict:
    """Render and send a freshly built trade proposal's card, then edit the cards it
    superseded to "Replaced by #N"."""
    from lib import notify
    from lib.desk import proposals
    with conn_factory() as conn:
        model = proposals.trade_card_model(conn, built, now_dt=now_dt)
        prop = proposals.get_proposal(conn, built["proposal_id"])
        old = [proposals.get_proposal(conn, s) for s in built.get("superseded", [])]
    res = box.send_card(prop["id"], notify.render_trade_card(model), kind="trade", nonce=prop["nonce"])
    for o in old:
        if o and o.get("tg_message_id"):
            box.set_label(o["tg_message_id"], f"Replaced by #{prop['id']}")
    return res


def send_change_card(led, box: Outbox, cfg, proposal_id: int, *, now_dt=None) -> dict:
    from lib import notify
    from lib.desk import proposals
    with led.connect() as conn:
        prop = proposals.get_proposal(conn, proposal_id)
        norm = (store.loads(prop["payload_json"], {}) or {}).get("change") or {}
        preview = proposals.change_preview(conn, cfg, norm, now_dt=now_dt)
    model = {"id": prop["id"], "change": norm, "rationale": prop["rationale"],
             "evidence": store.loads(prop["evidence_json"], []), "expires_at": prop["expires_at"],
             "now": store.iso(now_dt or store.utc_now()), "preview": preview}
    return box.send_card(prop["id"], notify.render_change_card(model), kind="change", nonce=prop["nonce"])
