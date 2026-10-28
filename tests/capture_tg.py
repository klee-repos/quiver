#!/usr/bin/env python3
"""Capture the raw Telegram Bot API answers that ``tests/test_chat_bridge.py`` replays.

Run it ONCE, by hand, with the chat bridge stopped (only one process may poll the bot
token): ``sudo systemctl stop quiver-chat.service`` on the box, or no bridge running.

    set -a; . ./.env; set +a; .venv/bin/python tests/capture_tg.py

It sends one card with an Approve button to the owner chat, then waits up to 3 minutes for
ONE owner message and ONE tap on the button. It also records a reaction, a typing action,
a button edit, a callback answer, and a 400 "can't parse entities" answer. The answers go
to ``tests/fixtures/desk/tg.json.gz`` with the owner's and the bot's identity replaced
(id 111 and test names): the repo is public. Nothing else changes.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib import telegram  # noqa: E402

OUT = REPO / "tests" / "fixtures" / "desk" / "tg.json.gz"
OWNER_TEST_ID = 111
BOT_TEST_ID = 999


def scrub(obj, owner_id: int, bot_id: int):
    """Replace the owner's and the bot's identity with test values (values only)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "id" and isinstance(v, int) and v in (owner_id, bot_id):
                out[k] = OWNER_TEST_ID if v == owner_id else BOT_TEST_ID
            elif k in ("first_name", "last_name", "username") and isinstance(v, str):
                out[k] = {"first_name": "Owner", "last_name": "Test", "username": "owner_test"}[k]
            elif k == "chat_instance":
                out[k] = "1000000000000000000"
            else:
                out[k] = scrub(v, owner_id, bot_id)
        return out
    if isinstance(obj, list):
        return [scrub(x, owner_id, bot_id) for x in obj]
    if isinstance(obj, str):
        return obj.replace(str(owner_id), str(OWNER_TEST_ID)).replace(str(bot_id), str(BOT_TEST_ID))
    return obj


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    owner = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").replace(",", " ").split() or [""])[0]
    if not token or not owner:
        print("set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS", file=sys.stderr)
        return 2
    api = lambda m, p: telegram.api(token, m, p, timeout=70)  # noqa: E731
    me = api("getMe", {})
    bot_id = int((me.get("result") or {}).get("id") or 0)
    cap = {"getMe": me}
    drained = api("getUpdates", {"timeout": 0, "allowed_updates": ["message", "callback_query"]})
    offset = max([u["update_id"] for u in drained.get("result") or []] or [-1]) + 1
    card = api("sendMessage", {
        "chat_id": owner, "parse_mode": "HTML",
        "text": "<b>Capture test.</b> Send me any short message, then tap Approve. Nothing trades.",
        "reply_markup": {"inline_keyboard": [[
            {"text": "Approve", "callback_data": "p:1:abcdef0123:a", "style": "success"},
            {"text": "Reject", "callback_data": "p:1:abcdef0123:r", "style": "danger"}]]}})
    cap["sendMessage"] = card
    cap["sendMessage_bad_html"] = api("sendMessage", {"chat_id": owner, "parse_mode": "HTML",
                                                     "text": "<b>broken <i>html</b>"})
    print("Card sent. Send one message and tap Approve within 3 minutes.", flush=True)
    got_msg = got_tap = None
    batches = []
    deadline = time.time() + 180
    while time.time() < deadline and not (got_msg and got_tap):
        res = api("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message", "callback_query"]})
        batches.append(res)
        for u in res.get("result") or []:
            offset = u["update_id"] + 1
            if "message" in u and not got_msg:
                got_msg = u["message"]
            if "callback_query" in u and not got_tap:
                got_tap = u["callback_query"]
    cap["getUpdates"] = [b for b in batches if b.get("result")]
    if got_msg:
        cap["setMessageReaction"] = api("setMessageReaction", {"chat_id": owner, "message_id": got_msg["message_id"],
                                                               "reaction": [{"type": "emoji", "emoji": "👀"}]})
        cap["setMessageReaction_bad"] = api("setMessageReaction", {"chat_id": owner, "message_id": got_msg["message_id"],
                                                                   "reaction": [{"type": "emoji", "emoji": "🙂"}]})
    cap["sendChatAction"] = api("sendChatAction", {"chat_id": owner, "action": "typing"})
    if got_tap:
        cap["answerCallbackQuery"] = api("answerCallbackQuery", {"callback_query_id": got_tap["id"], "text": "Captured."})
    mid = (card.get("result") or {}).get("message_id")
    if mid:
        cap["editMessageReplyMarkup"] = api("editMessageReplyMarkup", {
            "chat_id": owner, "message_id": mid,
            "reply_markup": {"inline_keyboard": [[{"text": "Captured", "callback_data": "noop"}]]}})
    api("getUpdates", {"offset": offset, "timeout": 0})  # confirm what we read
    cap = scrub(cap, int(owner), bot_id)
    OUT.write_bytes(gzip.compress(json.dumps(cap, ensure_ascii=False).encode()))
    print(json.dumps({"wrote": str(OUT), "message": bool(got_msg), "tap": bool(got_tap),
                      "keys": sorted(cap)}))
    return 0 if (got_msg and got_tap) else 1


if __name__ == "__main__":
    raise SystemExit(main())
