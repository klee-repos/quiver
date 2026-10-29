#!/usr/bin/env python3
"""The desk chat bridge: Telegram long poll, the desk agent's turns, and button taps (plan §4).

``quiver-chat.service`` runs this always. It is the only process that polls the bot token.
It keeps one Claude Agent SDK session per chat connected, so a turn starts with no process
start. The walls (plan §13):
  1. The unit hides the Robinhood config and /etc/quiver at the kernel level.
  2. The desk agent has typed tools, read-only file tools confined to the repo, and web
     search. No shell, no file write, no broker tool. A PreToolUse hook enforces it.
  3. ``scrub_process_env`` removes every secret from ``os.environ`` before the first SDK
     client starts; the Telegram token lives in ``Secrets`` only.
  4. A message or tap counts only from an owner ``from.id``, in a private chat with that id.
  5. A tap runs ``lib/desk/approvals.handle_tap``, the only code that approves. No tool
     reaches it.

Intake is crash-safe: each owner message is stored in ``chat_messages`` (unique on
``chat_id, tg_message_id``) before the update offset moves, so a crash re-delivers the
update and the unique index drops the duplicate. The backlog at start is processed, not
discarded.

The agenda (plan §4.8, ``lib/desk/agenda.py``): background work waits on the agenda, and
the bridge brings up one item at a time. When no item is open, no turn runs, and the owner
is not in the middle of a talk, the bridge opens the next item and runs a desk turn with a
``<bring_up>`` block. The desk tells the owner about it; then the bridge sends the item's
cards. The next item comes up only after the owner deals with the open one.
"""

from __future__ import annotations

import asyncio
import functools
import html as _htmllib
import json
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.desk.outbox import render_model_text  # noqa: E402

LONG_POLL_SEC = 50
TURN_TIMEOUT_S = 600
IDLE_DISCONNECT_S = 30 * 60
TYPING_EVERY_S = 4
OFFLINE_TAP_S = 60
QUICK_PICK_S = 10
BROKER_WATCH_S = 180
AGENDA_POLL_S = 5           # how often the bridge looks for an agenda item to bring up
AGENDA_AFTER_TAP_S = 4      # a tap closed the open item: the next one waits this long
AGENDA_QUIET_S = 120        # after the owner's message and the desk's reply: no new item for this long
AGENDA_PREEMPT_S = 20 * 60  # a `now` item takes over an unanswered `next` item after this long
AGENDA_CARD_HOLD_S = 15 * 60  # a fresh card in the chat (a requote, a follow-up) holds the line this long
AGENDA_FOLLOW_UP_S = 30 * 60  # a Confirm whose trade waits on a snapshot holds the line this long
STOP_HOLD_H = 1             # /stop during an agenda turn holds the agenda this many hours
_MSG_LIMIT = 4000
# The chat_messages source of an agenda item that the bridge sends as plain text.
_ITEM_SOURCE = {"report": "analyst", "failure": "analyst", "drift": "monitor", "brief": "brief"}

# --- formatting (kept from the earlier bridge) ------------------------------------------

_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


# One renderer for every model text (desk and analyst): lib.desk.outbox.render_model_text.
_to_telegram_html = render_model_text


def _strip_md(text: str) -> str:
    """Plain-text fallback: drop the markup."""
    text = re.sub(r"```[^\n]*\n?(.*?)```", r"\1", text, flags=re.DOTALL).replace("`", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.M)
    return _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)


def _chunks(text: str, limit: int):
    """Split at line boundaries so an HTML tag is never cut across two messages."""
    out, buf, cur = [], [], 0
    for ln in text.split("\n"):
        while len(ln) > limit:
            out.append(ln[:limit])
            ln = ln[limit:]
        if cur + len(ln) + 1 > limit and buf:
            out.append("\n".join(buf))
            buf, cur = [], 0
        buf.append(ln)
        cur += len(ln) + 1
    if buf:
        out.append("\n".join(buf))
    return out or [text]


def _esc(text) -> str:
    """Escape ``<`` and ``>`` in embedded text, so quoted text cannot forge a tag."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def classify_error(text: str) -> str:
    """``auth``, ``limit`` or ``other`` for an SDK error text (§4.7)."""
    low = str(text or "").lower()
    if any(m in low for m in ("spend limit", "usage limit", "rate limit", "limit reached", "credit balance")):
        return "limit"
    if any(m in low for m in ("authenticat", "oauth", "401", "invalid api key", "invalid x-api-key", "expired")):
        return "auth"
    return "other"


class Bridge:
    """The bridge's state and loops. Every network call goes through ``transport``
    (``transport(method, params) -> dict``, sync) and the SDK ``client_factory``."""

    def __init__(self, *, runtime, secrets, led=None, transport=None, client_factory=None, quick_pick=None,
                 clock=time.monotonic, wall=None, sleep=asyncio.sleep, page=None, log_path=None):
        from lib.desk import agent, outbox, tools
        from lib.ledger import Ledger
        self.runtime = runtime
        self.secrets = secrets
        self.led = led or Ledger(runtime.ledger_db)
        self.owner_ids = set(secrets.owner_ids)
        self.transport = transport or functools.partial(_tg_api, secrets.telegram_bot_token)
        self.clock = clock
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.page = page
        self.log_path = Path(log_path or Path(runtime.root) / "logs" / "desk" / "bridge.log")
        self.notified_unauth = set()
        self.turn = agent.TurnState()
        self.chat_id = (secrets.owner_ids or [""])[0]
        self.box = outbox.Outbox(self.led, transport=self.transport, chat_id=self.chat_id)
        self.ctx = tools.ToolContext.build(role="desk", runtime=runtime, secrets=secrets, turn=self.turn,
                                           led=self.led, transport=self.transport, box=self.box,
                                           render=_to_telegram_html)
        self.client_factory = client_factory or self._default_client
        self.quick_pick = quick_pick or self._default_quick_pick
        self.client = None
        self.client_session = None
        self.turn_task = None
        self.quick_busy = False
        self.last_owner_at = None
        self.last_tap_at = None
        self.last_owner_wall = None        # the UTC time of the owner's last message or tap
        self.last_owner_turn_end = None    # when the last turn with owner messages ended
        self.taps_busy = 0
        self.cards_sending = set()         # proposal ids whose card is being sent now
        self.turn_item = None              # the agenda item of the running turn
        self.turn_stopped = False
        self.agenda_task = None
        self.stopping = False

    # --- plumbing -----------------------------------------------------------------
    async def tg(self, method, params):
        return await asyncio.to_thread(self.transport, method, params)

    def log(self, obj):
        line = json.dumps({"at": datetime.now(timezone.utc).isoformat(), **obj}, default=str)
        print(line, flush=True)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def state(self, key, default=None):
        from lib.desk import store
        with self.led.connect() as c:
            return store.get_state(c, key, default)

    def set_state(self, key, value):
        from lib.desk import store
        with self.led.connect() as c:
            store.set_state(c, key, value)

    async def send_text(self, text, *, source="chat", reply_to=None):
        return await asyncio.to_thread(self.box.send, text, source=source, reply_to=reply_to)

    # --- intake (§4.1) ------------------------------------------------------------
    def accepted(self, msg) -> bool:
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        return (chat.get("type") == "private" and str(frm.get("id")) in self.owner_ids
                and str(chat.get("id")) == str(frm.get("id")))

    async def handle_update(self, u, *, offline: bool):
        from lib.desk import store
        if "callback_query" in u:
            # Count the tap at once: no turn and no agenda item may start before it runs.
            self.taps_busy += 1
            self.last_tap_at = self.clock()
            asyncio.create_task(self._tap_task(u["callback_query"], offline))
            self.set_state("tg_offset", int(u["update_id"]) + 1)
            return
        msg = u.get("message")
        if msg:
            await self.handle_message(msg)
        self.set_state("tg_offset", int(u["update_id"]) + 1)

    async def handle_message(self, msg):
        from lib.desk import store
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if not self.accepted(msg):
            cid = chat.get("id")
            if chat.get("type") == "private" and cid is not None and str(cid) not in self.notified_unauth \
                    and len(self.notified_unauth) < 50:
                self.notified_unauth.add(str(cid))
                await self.tg("sendMessage", {"chat_id": cid, "text": f"Not authorized. Your chat id is {cid}."})
            self.log({"event": "ignored", "chat_type": chat.get("type"), "chat_id": cid})
            return
        if not text:
            return
        created = datetime.fromtimestamp(int(msg.get("date") or time.time()), tz=timezone.utc)
        with self.led.connect() as c:
            fresh = store.insert_owner_message(c, chat_id=chat["id"], tg_message_id=int(msg["message_id"]),
                                               text=text, created_at=store.iso(created))
        if not fresh:
            return  # a re-delivered update after a crash
        self.last_owner_at = self.clock()
        self.last_owner_wall = store.now_iso()
        low = text.lower()
        if low in ("/clear", "/reset", "/new", "/stop", "/status", "/start", "/help"):
            with self.led.connect() as c:
                c.execute("UPDATE chat_messages SET delivered_to_session=1 WHERE chat_id=? AND tg_message_id=?",
                          (str(chat["id"]), int(msg["message_id"])))
            await self.command(low, chat["id"])
            return
        if self.turn_task is not None and not self.turn_task.done():
            asyncio.create_task(self.quick_react(int(msg["message_id"]), text))

    # --- commands (§4.3 step 3) ----------------------------------------------------
    async def command(self, cmd, chat_id):
        from lib.desk import store
        if cmd in ("/clear", "/reset", "/new"):
            await self.disconnect()
            with self.led.connect() as c:
                c.execute("INSERT INTO chat_sessions (chat_id, session_id, started_at, last_turn_at) "
                          "VALUES (?, ?, ?, NULL) ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id, "
                          "started_at=excluded.started_at, last_turn_at=NULL",
                          (str(chat_id), str(uuid.uuid4()), store.now_iso()))
            await self.send_text("Started a fresh conversation. The research memory and our chat log stay.",
                                 source="system")
        elif cmd == "/stop":
            if self.client is not None and self.turn_task is not None and not self.turn_task.done():
                self.turn_stopped = True
                try:
                    await self.client.interrupt()
                except Exception:  # noqa: BLE001
                    pass
                if self.turn_item is not None:
                    await self.send_text(f"Stopped. I'll hold the rest of the list for {STOP_HOLD_H} hour.",
                                         source="system")
                else:
                    await self.send_text("Stopped.", source="system")
            else:
                await self.send_text("Nothing is running.", source="system")
        elif cmd == "/status":
            await self.send_text(self.status_text(), source="system")
        else:
            await self.send_text("I'm your desk. Ask me about the book, a company, a filing, or a trade. "
                                 "Nothing trades unless you tap Approve. /status, /clear, /stop.",
                                 source="system")

    def status_text(self) -> str:
        from lib import market
        from lib.desk import agenda, store
        with self.led.connect() as c:
            snap = c.execute("SELECT captured_at FROM broker_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            pending = c.execute("SELECT COUNT(*) FROM proposals WHERE state='pending'").fetchone()[0]
            last_run = store.get_state(c, "monitor_last_run")
            job = c.execute("SELECT status, error, finished_at FROM broker_jobs WHERE kind='snapshot' AND status IN "
                            "('done','failed') ORDER BY id DESC LIMIT 1").fetchone()
            ag = agenda.counts(c)
        halted = self.led.is_halted(market.trading_day_et())
        age = store.age_seconds(snap[0]) if snap else None
        parts = [f"Snapshot: {round(age / 60)} min old." if age is not None else "Snapshot: none yet.",
                 f"Pending proposals: {pending}.",
                 f"Agenda: {ag['open']} open, {ag['waiting']} waiting, {ag['fyi']} fyi.",
                 f"Last monitor run: {last_run}." if last_run else "Last monitor run: none yet.",
                 "Trading is halted today." if halted else "Trading is not halted.",
                 ("Robinhood login: expired, run ./deploy/gcp/rh-reauth.sh." if job and job[0] == "failed"
                  and "auth" in str(job[1] or "") else "Robinhood login: OK as of the last snapshot." if job
                  else "Robinhood login: no snapshot job yet.")]
        return " ".join(parts)

    # --- reactions (§4.2) ----------------------------------------------------------
    async def quick_react(self, message_id: int, text: str):
        """The quick pick for a message that arrives during a turn (at most one at a time)."""
        from lib.desk import reactions, store
        if self.quick_busy:
            return False
        self.quick_busy = True
        try:
            try:
                emoji = await asyncio.wait_for(self.quick_pick(text), QUICK_PICK_S)
            except Exception as e:  # noqa: BLE001
                self.log({"event": "quick_pick_failed", "error": f"{type(e).__name__}: {e}"})
                return False
            emoji = reactions.check(emoji)
            if not emoji:
                return False
            await self.tg("setMessageReaction", {"chat_id": self.chat_id, "message_id": message_id,
                                                 "reaction": [{"type": "emoji", "emoji": emoji}]})
            with self.led.connect() as c:
                c.execute("UPDATE chat_messages SET reaction=?, reacted_at=? WHERE chat_id=? AND tg_message_id=?",
                          (emoji, store.now_iso(), str(self.chat_id), int(message_id)))
            self.turn.reacted_ids.add(int(message_id))
            return True
        finally:
            self.quick_busy = False

    async def _default_quick_pick(self, text: str):
        from claude_agent_sdk import AssistantMessage, query
        from lib.desk import agent
        from lib.prompts import load_prompt
        opts = agent.small_job_options(self.runtime, system_prompt=load_prompt("quick_reaction"))
        out = ""
        async for m in query(prompt=text[:2000], options=opts):
            if isinstance(m, AssistantMessage):
                out += "".join(getattr(b, "text", "") or "" for b in m.content)
        return out.strip()

    # --- turns (§4.3) --------------------------------------------------------------
    def pending_owner(self):
        with self.led.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM chat_messages WHERE role='owner' AND chat_id=? AND delivered_to_session=0 "
                "AND tg_message_id IS NOT NULL ORDER BY id", (str(self.chat_id),)).fetchall()]

    def desk_updates(self):
        with self.led.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM chat_messages WHERE role='desk' AND source != 'chat' AND delivered_to_session=0 "
                "ORDER BY id").fetchall()]

    def build_input(self, owners, updates, item=None) -> str:
        from lib import market
        from lib.desk import agenda, store

        def clock(ts):
            dt = store.parse_ts(ts)
            return market.fmt_et(dt) + " ET" if dt else ""
        parts = []
        if updates:
            since = updates[0]["created_at"]
            parts.append(f'<desk_updates since="{since}">')
            for u in updates:
                # One row is one line: a newline in a model text cannot forge a second row.
                one_line = " / ".join(x.strip() for x in str(u["text"] or "").splitlines() if x.strip())
                parts.append(f"[{clock(u['created_at'])} · {u['source']}] {_esc(one_line)}")
            parts.append("</desk_updates>")
        now = store.now_iso()
        with self.led.connect() as c:
            agenda.refresh(c, now)
            lines = agenda.block_lines(agenda.view(c, now))
            cards = agenda.card_facts(c, item) if item else []
        if lines:
            parts.append("<agenda>")
            parts.extend(_esc(x) for x in lines)
            parts.append("</agenda>")
        for o in owners:
            parts.append(f'<owner message_id="{o["tg_message_id"]}" sent="{clock(o["created_at"])}">'
                         f'{_esc(o["text"])}</owner>')
        if item:
            parts.append(f'<bring_up item="{item["id"]}" priority="{agenda.PRIORITY_TEXT.get(item["priority"])}" '
                         f'kind="{item["kind"]}" ticker="{_esc(item.get("ticker") or "")}" '
                         f'ready="{clock(item["created_at"])}">')
            parts.append(f"title: {_esc(item['title'])}")
            parts.append("report:")
            parts.append(_esc(item.get("body") or "(none)"))
            for x in cards:
                parts.append(f"card #{x['id']}: {x['kind']}, {_esc(x['what'])}, {x['state']}, "
                             f"{'in the chat' if x['in_chat'] else 'the bridge sends it after your messages'}, "
                             f"open until {x['open_until']}")
            parts.append("</bring_up>")
        return "\n".join(parts)

    async def _default_client(self, options):
        from claude_agent_sdk import ClaudeSDKClient
        client = ClaudeSDKClient(options=options)
        await client.connect()
        return client

    async def ensure_client(self):
        from lib.desk import agent, store
        if self.client is not None:
            return self.client
        with self.led.connect() as c:
            row = c.execute("SELECT session_id, last_turn_at FROM chat_sessions WHERE chat_id=?",
                            (str(self.chat_id),)).fetchone()
        opts = agent.desk_options(self.runtime, self.secrets, self.turn, ctx=self.ctx,
                                  resume=row[0] if row and row[1] else None)
        if row and not row[1]:
            opts.session_id = row[0]
        self.client = await self.client_factory(opts)
        self.client_session = row[0] if row else None
        return self.client

    async def disconnect(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.client = None

    async def typing_loop(self):
        while True:
            await self.tg("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})
            await self.sleep(TYPING_EVERY_S)

    async def run_turn(self, item=None):
        """One desk turn: the owner's new messages, or (``item``) one agenda item to bring up.
        An agenda turn takes no owner message: one that arrives during it starts the next turn."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage
        from lib.desk import store, tools
        owners = [] if item is not None else self.pending_owner()
        if not owners and item is None:
            return
        updates = self.desk_updates()
        reacted = [o["tg_message_id"] for o in owners if o.get("reaction")]
        self.turn.start(chat_id=self.chat_id, owner_message_ids=[o["tg_message_id"] for o in owners],
                        started_at=store.now_iso(), reacted=reacted)
        self.turn_item, self.turn_stopped = item, False
        prompt = self.build_input(owners, updates, item=item)
        typing = asyncio.create_task(self.typing_loop())
        last_text, session_id, error = "", None, None
        try:
            client = await self.ensure_client()
            await client.query(prompt)

            async def consume():
                nonlocal last_text, session_id
                async for m in client.receive_response():
                    if isinstance(m, AssistantMessage):
                        t = "".join(getattr(b, "text", "") or "" for b in m.content if hasattr(b, "text"))
                        if t.strip():
                            last_text = t.strip()
                    elif isinstance(m, SystemMessage) and m.subtype == "init":
                        session_id = (m.data or {}).get("session_id") or session_id
                    elif isinstance(m, ResultMessage):
                        session_id = m.session_id or session_id
                        if m.is_error:
                            raise RuntimeError(m.result or m.subtype or "error result")
            await asyncio.wait_for(consume(), TURN_TIMEOUT_S)
        except asyncio.TimeoutError:
            try:
                await self.client.interrupt()
            except Exception:  # noqa: BLE001
                pass
            error = "timeout"
            if item is None:
                await self.send_text("That took too long, so I stopped. Ask again, or ask me for a deep-dive.",
                                     source="system")
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            await self.report_error(str(e), quiet=item is not None)
        finally:
            typing.cancel()
        self.turn.session_id = session_id or self.turn.session_id
        # The trailing text (§4.4 step 4): a turn with no `say` sends its last text as the answer.
        # A turn that sent with `say` drops it. Two live runs on 2026-10-28 showed it is a recap.
        if not error and last_text and self.turn.said:
            self.log({"event": "trailing_dropped", "chars": len(last_text)})
        if not error and last_text and not self.turn.said:
            for mid in self.turn.unreacted():
                text = next((o["text"] for o in owners if o["tg_message_id"] == mid), "")
                await self.quick_react(mid, text)
            if not self.turn.unreacted():
                for part in _chunks(last_text, tools.SAY_LIMIT):
                    # §4.4 step 2: the typing action goes before each message.
                    await self.tg("sendChatAction", {"chat_id": self.chat_id, "action": "typing"})
                    await self.send_text(_to_telegram_html(part), source="chat",
                                         reply_to=self.turn.first_reply_to if not self.turn.said else None)
                    self.turn.said.append(part)
            else:
                # The quick pick failed, so `say` checks forbid the send. Log it: the owner got no reply.
                self.log({"event": "trailing_unsent", "reason": "unreacted", "chars": len(last_text)})
        stopped = item is not None and self.turn_stopped
        if stopped:
            # /stop in an agenda turn: the item goes back in line, and the agenda holds.
            from lib.desk import agenda
            with self.led.connect() as c:
                agenda.defer(c, item["id"], hours=0, now=store.now_iso())
                agenda.hold(c, store.iso(store.utc_now() + timedelta(hours=STOP_HOLD_H)))
        elif item is not None and not self.turn.said:
            # The desk said nothing (an error, a timeout, an empty turn): send the item as written.
            await self.send_item_plain(item)
        if not stopped:
            await self.send_open_cards()
        self.after_turn(owners, item, error)
        with self.led.connect() as c:
            ids = [o["id"] for o in owners] + [u["id"] for u in updates]
            c.executemany("UPDATE chat_messages SET delivered_to_session=1 WHERE id=?", [(i,) for i in ids])
            if session_id:
                c.execute("INSERT INTO chat_sessions (chat_id, session_id, started_at, last_turn_at) VALUES (?,?,?,?) "
                          "ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id, "
                          "last_turn_at=excluded.last_turn_at", (str(self.chat_id), session_id, store.now_iso(),
                                                                 store.now_iso()))
        self.turn_item = None
        self.log({"event": "turn", "owners": len(owners), "updates": len(updates), "said": len(self.turn.said),
                  "error": error, **({"item": item["id"]} if item is not None else {})})

    def after_turn(self, owners, item, error):
        """The agenda after a turn (§4.8). The open item counts as seen. An open item with no
        pending card closes when the owner wrote after it came up: it needed no decision. An
        owner turn starts the quiet window again."""
        from lib.desk import agenda, store
        now = store.now_iso()
        with self.led.connect() as c:
            op = agenda.open_item(c)
            if op is not None:
                agenda.mark_presented(c, op["id"])
            if owners and not error and op is not None and op.get("opened_at") and \
                    max(str(o["created_at"]) for o in owners) > op["opened_at"] and not agenda.pending_cards(c, op):
                agenda.close(c, op["id"], reason="the owner wrote after it came up; it needed no decision", now=now)
                self.log({"event": "agenda_closed", "items": [[op["id"], "answered"]]})
        if owners:
            self.last_owner_turn_end = self.clock()

    async def send_item_plain(self, item):
        """Send an agenda item as written (the analyst's report, the brief), in whole paragraphs."""
        from lib.desk import agenda, tools
        source = _ITEM_SOURCE.get(item.get("kind"), "system")
        for m in agenda.plain_messages(item, tools.SAY_LIMIT):
            await self.send_text(_to_telegram_html(m), source=source)
        self.log({"event": "agenda_plain", "item": item["id"]})

    async def send_open_cards(self):
        """Send each pending card of the open item that is not in the chat yet. The desk may
        place a card sooner with ``show_card``; this makes sure no card is left out."""
        from lib.desk import agenda
        with self.led.connect() as c:
            it = agenda.open_item(c)
            pids = agenda.unsent_cards(c, it)
        for pid in pids:
            await self.send_card_once(pid, (it.get("cards") or {}).get(str(pid)))

    async def send_card_once(self, pid, card=None):
        """Send a card that is not in the chat yet, once: a second caller at the same time,
        or a card already sent, sends nothing."""
        from lib.desk import proposals
        if pid in self.cards_sending:
            return None
        self.cards_sending.add(pid)
        try:
            with self.led.connect() as c:
                p = proposals.get_proposal(c, pid)
            if p is None or p.get("tg_message_id") is not None:
                return None
            return await asyncio.to_thread(self._send_card, pid, card)
        finally:
            self.cards_sending.discard(pid)

    def agenda_next(self):
        """Open the next agenda item when the owner is free, else None (§4.8). The owner is
        free when no tap is in progress, no hold is set, no trade of a tap is still at the
        broker, and the owner's last act is old enough: ``AGENDA_AFTER_TAP_S`` after a tap,
        ``AGENDA_QUIET_S`` after a message and the desk's reply. With no item open, a fresh
        card in the chat (``AGENDA_CARD_HOLD_S``) or a Confirm whose trade waits on a snapshot
        (``AGENDA_FOLLOW_UP_S``) also holds the line. With an item open, only a ``now`` item
        may come up, after ``AGENDA_PREEMPT_S`` with no owner act since the open one came up."""
        from lib.desk import agenda, store
        if self.stopping or self.taps_busy:
            return None
        t = self.clock()
        msg_ref = max([x for x in (self.last_owner_at, self.last_owner_turn_end) if x is not None], default=None)
        if self.last_tap_at is not None and (msg_ref is None or self.last_tap_at >= msg_ref):
            if t - self.last_tap_at < AGENDA_AFTER_TAP_S:
                return None
        elif msg_ref is not None and t - msg_ref < AGENDA_QUIET_S:
            return None
        now_dt = store.utc_now()
        now = store.iso(now_dt)
        with self.led.connect() as c:
            if agenda.hold_until(c, now):
                return None
            if c.execute("SELECT 1 FROM broker_jobs WHERE kind='execute' AND status IN ('queued','running') "
                         "LIMIT 1").fetchone():
                return None
            agenda.refresh(c, now)
            op = agenda.open_item(c)
            if op is not None:
                if not self._preempt_due(c, op, now_dt):
                    return None
                status = agenda.set_aside(c, op["id"], reason="not answered; a more urgent item came up", now=now)
                self.log({"event": "agenda_preempt", "item": op["id"], "status": status})
            else:
                hold = store.iso(now_dt - timedelta(seconds=AGENDA_CARD_HOLD_S))
                if c.execute("SELECT 1 FROM proposals WHERE state='pending' AND tg_message_id IS NOT NULL "
                             "AND created_at > ? LIMIT 1", (hold,)).fetchone():
                    return None
                fu = store.iso(now_dt - timedelta(seconds=AGENDA_FOLLOW_UP_S))
                if c.execute("SELECT 1 FROM proposals WHERE kind='change' AND state='applied' AND "
                             "result_json LIKE '%waiting%' AND decided_at > ? LIMIT 1", (fu,)).fetchone():
                    return None
            return agenda.take_next(c, now)

    def _preempt_due(self, c, op, now_dt) -> bool:
        """A `now` item may take over an open `next` item that the owner left unanswered."""
        from lib.desk import agenda, store
        opened = store.parse_ts(op.get("opened_at"))
        if op["priority"] <= agenda.NOW or opened is None:
            return False
        if (now_dt - opened).total_seconds() < AGENDA_PREEMPT_S:
            return False
        if self.last_owner_wall is not None and self.last_owner_wall >= op["opened_at"]:
            return False
        return any(x["priority"] == agenda.NOW for x in agenda.due(c, store.iso(now_dt)))

    async def agenda_loop(self):
        while not self.stopping:
            await self.sleep(AGENDA_POLL_S)
            try:
                await self.maybe_turn()
            except Exception as e:  # noqa: BLE001 — the loop never stops
                self.log({"event": "agenda_error", "error": f"{type(e).__name__}: {e}"})

    async def report_error(self, text: str, *, quiet: bool = False):
        """Log, page and tell the owner. ``quiet`` (an agenda turn) skips the owner text: the
        bridge sends the item as written instead."""
        from lib import market
        kind = classify_error(text)
        self.log({"event": "turn_error", "kind": kind, "error": str(text)[:2000]})
        if kind == "auth":
            if not quiet:
                await self.send_text("My Claude login stopped working. Rotate CLAUDE_CODE_OAUTH_TOKEN "
                                     "(see docs/DEPLOY.md).", source="system")
            if self.page:
                self.page("auth_error", "desk", str(text)[:300])
            else:
                import runner_common as rc
                rc._maybe_alert(self.led, kind="auth_error", stage="desk", day=market.trading_day_et(),
                                now_iso=datetime.now(timezone.utc).isoformat(), event_detail=str(text)[:300],
                                secrets=self.secrets)
        elif quiet:
            return
        elif kind == "limit":
            await self.send_text(f"I hit a usage limit: {str(text).strip()[:300]}", source="system")
        else:
            first = (str(text).strip().splitlines() or ["unknown error"])[0][:300]
            await self.send_text(f"I hit an error: {first}", source="system")

    async def maybe_turn(self):
        """Start the next turn: the owner's messages first, else one agenda item. No await
        runs between the check and the start, so two turns never run at once. No turn starts
        while a tap runs: a Confirm's follow-up card must join its item first."""
        if self.turn_task is not None and not self.turn_task.done():
            return
        if self.taps_busy:
            return
        item = None
        if not self.pending_owner():
            item = self.agenda_next()
            if item is None:
                return
        self.turn_task = asyncio.create_task(self._turn(item))
        # Messages that arrived during this turn start the next one at once.
        self.turn_task.add_done_callback(lambda _t: asyncio.ensure_future(self.maybe_turn()))

    async def _turn(self, item):
        """``run_turn``, with a last net for an agenda turn: if the turn fails before the desk
        said anything, the bridge sends the item as written and its cards."""
        try:
            await self.run_turn(item=item)
        except Exception as e:  # noqa: BLE001
            self.log({"event": "turn_crash", "error": f"{type(e).__name__}: {e}",
                      **({"item": item["id"]} if item is not None else {})})
            self.turn_item = None
            if item is not None and not self.turn.said:
                await self.send_item_plain(item)
                await self.send_open_cards()
                self.after_turn([], item, "crash")

    # --- taps (§4.6) ---------------------------------------------------------------
    async def _tap_task(self, cbq, offline):
        """A tap from the poll loop. ``handle_update`` counted it in ``taps_busy`` at once."""
        try:
            await self.handle_tap(cbq, offline=offline)
        finally:
            self.taps_busy -= 1
            asyncio.ensure_future(self.maybe_turn())

    async def handle_tap(self, cbq, *, offline: bool):
        if offline:
            await self.tg("answerCallbackQuery", {"callback_query_id": cbq.get("id"),
                                                  "text": "I was offline when you tapped. Tap again."})
            return {"offline": True}
        # No agenda item comes up while a tap runs: its follow-up card must join the open item first.
        self.taps_busy += 1
        self.last_tap_at = self.clock()
        try:
            return await self._handle_tap(cbq)
        finally:
            self.taps_busy -= 1
            self.last_tap_at = self.clock()

    async def _handle_tap(self, cbq):
        from lib.desk import agenda, approvals, store
        cid = cbq.get("id")
        frm = (cbq.get("from") or {}).get("id")
        msg = cbq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        self.last_owner_wall = store.now_iso()
        res = await asyncio.to_thread(
            approvals.handle_tap, self.led, self.runtime.cfg, callback_data=cbq.get("data"), from_id=frm,
            chat_id=chat_id, message_id=msg.get("message_id"), owner_ids=self.owner_ids)
        await self.tg("answerCallbackQuery", {"callback_query_id": cid, "text": str(res["answer"])[:200]})
        if res.get("label") and msg.get("message_id"):
            await asyncio.to_thread(self.box.set_label, msg["message_id"], res["label"])
        for t in res.get("touch", []):
            from lib.desk import store
            store.touch_trigger(self.runtime.root, t)
        for m in res.get("messages", []):
            await self.send_text(m, source="proposal")
        for old in res.get("superseded", []) or []:
            await self._relabel(old)
        if res.get("cards") and res.get("proposal_id"):
            # The trade card after a Confirm joins the item of the change, so the item stays
            # open until the owner taps the trade too.
            with self.led.connect() as c:
                it = agenda.item_of_proposal(c, res["proposal_id"])
                for pid in res["cards"]:
                    if it is not None:
                        agenda.attach(c, it["id"], pid)
        for pid in res.get("cards", []):
            await self.send_card_once(pid)
        with self.led.connect() as c:
            closed = agenda.refresh(c, store.now_iso())
        if closed:
            self.log({"event": "agenda_closed", "items": closed})
        if res.get("ok") and "broker" in res.get("touch", []) and res.get("proposal_id"):
            asyncio.create_task(self.broker_watch(res["proposal_id"]))
        self.log({"event": "tap", "result": {k: res.get(k) for k in ("answer", "label", "ok", "proposal_id")}})
        return res

    async def _relabel(self, pid):
        from lib.desk import proposals
        with self.led.connect() as c:
            p = proposals.get_proposal(c, pid)
        if p and p.get("tg_message_id"):
            label = f"Replaced by #{p['superseded_by']}" if p.get("superseded_by") else "Replaced"
            await asyncio.to_thread(self.box.set_label, p["tg_message_id"], label)

    def _send_card(self, pid, card=None):
        from lib.desk import outbox
        return outbox.send_card(self.led, self.box, self.runtime.cfg, pid, card=card)

    async def broker_watch(self, pid):
        await self.sleep(BROKER_WATCH_S)
        with self.led.connect() as c:
            row = c.execute("SELECT 1 FROM broker_jobs WHERE kind='execute' AND proposal_id=? AND status != 'queued'",
                            (pid,)).fetchone()
        if not row:
            await self.send_text(f"The broker has not started #{pid} yet.", source="system")

    # --- the loops -----------------------------------------------------------------
    async def poll_once(self):
        from lib.desk import store
        off = self.state("tg_offset")
        params = {"timeout": LONG_POLL_SEC, "allowed_updates": ["message", "callback_query"]}
        if off is not None:
            params["offset"] = int(off)
        res = await self.tg("getUpdates", params)
        if not res or not res.get("ok"):
            await self.sleep(5)
            return []
        now = self.wall()
        prev = store.parse_ts(self.state("last_poll_at"))
        offline = prev is None or (now - prev).total_seconds() > OFFLINE_TAP_S
        self.set_state("last_poll_at", store.iso(now))
        updates = res.get("result") or []
        for u in updates:
            try:
                await self.handle_update(u, offline=offline)
            except Exception as e:  # noqa: BLE001 — one bad update never stops the loop
                self.log({"event": "update_error", "error": f"{type(e).__name__}: {e}"})
        return updates

    async def idle_check(self):
        if self.client is not None and self.last_owner_at is not None and \
                self.clock() - self.last_owner_at > IDLE_DISCONNECT_S and \
                (self.turn_task is None or self.turn_task.done()):
            await self.disconnect()

    def agenda_recover(self):
        """At start: an open item the owner never saw goes back in line (§4.8)."""
        from lib.desk import agenda
        with self.led.connect() as c:
            back = agenda.recover(c)
        if back is not None:
            self.log({"event": "agenda_recovered", "item": back})
        return back

    async def run(self):
        self.log({"event": "start", "desk_model": self.runtime.desk_model})
        self.agenda_recover()
        self.agenda_task = asyncio.create_task(self.agenda_loop())
        while not self.stopping:
            await self.poll_once()
            await self.maybe_turn()
            await self.idle_check()


def _tg_api(token, method, params):
    from lib import telegram
    return telegram.api(token, method, params, timeout=LONG_POLL_SEC + 15 if method == "getUpdates" else 20)


def main(argv) -> int:
    from lib.desk import agent
    runtime = agent.Runtime.from_env()
    secrets = agent.Secrets.from_env()
    agent.scrub_process_env()
    if not secrets.telegram_bot_token or not secrets.owner_ids:
        print(json.dumps({"event": "idle", "note": "TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_CHAT_IDS unset"}))
        return 0
    bridge = Bridge(runtime=runtime, secrets=secrets)
    asyncio.run(bridge.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
