#!/usr/bin/env python3
"""Quiver chat bridge — a read-only Telegram front-end to the bot's own reasoning.

Long-polls Telegram (OUTBOUND https only, so it works behind the box's IAP-only firewall — no
public inbound endpoint, no phone-number registration), and for each message from an ALLOWLISTED
chat runs `claude -p` in the repo with a read-only tool profile and ZERO MCP servers, then sends
the reply back. It can inspect the ledger, strategy, logs, and per-decision proofs; it CANNOT
trade, move money, send email, or write to disk. The four walls that make "read-only" real:

  1. `--mcp-config chat_mcp.json --strict-mcp-config`  -> zero MCP servers -> no broker, no Resend
  2. `chat_guard.py` PreToolUse hook (chat_settings.json) -> denies writes/mutations/secret reads
  3. systemd ReadWritePaths (deploy/quiver-chat.service)  -> OS-level read-only on ledger + repo
  4. TELEGRAM_ALLOWED_CHAT_IDS allowlist                  -> only you can talk to it

Self-contained (stdlib urllib, mirrors lib/mailer.py — the ONE other network egress). Best-effort
and separate from the trading path: a crash just restarts (systemd Restart=on-failure); it never
imports or touches the trading brain. Reuses the box's Claude auth via CLAUDE_CONFIG_DIR.

Local smoke test (no Telegram, no token needed):
    .venv/bin/python deploy/runner/chat_bridge.py --ask "how did the last tick go?"
    .venv/bin/python deploy/runner/chat_bridge.py --print-cmd
"""

from __future__ import annotations

import html as _htmllib
import json
import os
import re
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The bridge runs as a script (systemd ExecStart / `python .../chat_bridge.py`), so its own dir is
# on sys.path[0]; insert it explicitly too so the sibling import also resolves when a test imports
# this module (mirrors tests/test_chat_guard.py's sys.path convention).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_history  # noqa: E402 — sibling module: per-thread conversation memory

_REPO = Path(__file__).resolve().parent.parent.parent

# --- config (env; sensible defaults) ----------------------------------------------------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = {
    s.strip() for s in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").replace(",", " ").split()
    if s.strip()
}
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
MODEL = (os.environ.get("QUIVER_CHAT_MODEL")
         or os.environ.get("QUIVER_MODEL") or "claude-sonnet-4-6")
EFFORT = os.environ.get("QUIVER_CHAT_EFFORT", "medium")
MAX_TURNS = int(os.environ.get("QUIVER_CHAT_MAX_TURNS", "40"))
# Per-question wall-clock. A ledger trace is a few read tool-calls; 240s is generous.
TIMEOUT_SEC = int(os.environ.get("QUIVER_CHAT_TIMEOUT_SEC", "240"))
LONG_POLL_SEC = int(os.environ.get("QUIVER_CHAT_POLL_SEC", "50"))
MAX_INPUT_CHARS = int(os.environ.get("QUIVER_CHAT_MAX_INPUT", "2000"))
LOG = os.environ.get("QUIVER_CHAT_LOG", str(_REPO / "logs" / "chat.log"))

# --- per-thread conversation memory (chat_history) --------------------------------------------
# Default ON: the whole point is that follow-ups carry context. Set QUIVER_CHAT_HISTORY=0 to get the
# classic stateless bridge (byte-identical single-question prompt). The store lives under the chat's
# OWN writable dir — derived from the log path so it lands in state/chat/ on the box (the one place
# the systemd unit's ReadWritePaths allows), logs/ locally; NEVER the read-only repo or ledger.
HISTORY_ENABLED = (os.environ.get("QUIVER_CHAT_HISTORY", "1").strip().lower()
                   not in ("0", "false", "no", "off", ""))
HISTORY_DIR = os.environ.get("QUIVER_CHAT_HISTORY_DIR") or str(Path(LOG).parent / "history")
HISTORY_TURNS = int(os.environ.get("QUIVER_CHAT_HISTORY_TURNS", "30"))
HISTORY_CHARS = int(os.environ.get("QUIVER_CHAT_HISTORY_CHARS", "12000"))
HISTORY_TURN_CHARS = int(os.environ.get("QUIVER_CHAT_HISTORY_TURN_CHARS", "1500"))
HISTORY_STORE = int(os.environ.get("QUIVER_CHAT_HISTORY_STORE", "500"))

CHAT_MCP = str(_REPO / "deploy" / "runner" / "chat_mcp.json")
CHAT_SETTINGS = str(_REPO / "deploy" / "runner" / "chat_settings.json")

_TG = f"https://api.telegram.org/bot{TOKEN}/"
_MSG_LIMIT = 4000  # Telegram hard cap is 4096; leave headroom.


def _load_prompt() -> str:
    """The read-only analyst persona (prompts/chat.md), read directly (no lib import)."""
    try:
        return (_REPO / "prompts" / "chat.md").read_text(encoding="utf-8").strip()
    except OSError:
        return "You are Quiver's strictly read-only analyst. Never trade or write anything."


SYSTEM_PROMPT = _load_prompt()


def _log(obj) -> None:
    line = json.dumps(obj)
    print(line, flush=True)
    try:
        Path(LOG).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # logging is best-effort, never fails the bridge


# --- the read-only claude call ----------------------------------------------------------------
# Env vars the `claude` child legitimately needs. EVERYTHING ELSE is stripped so the trading
# secrets in the bridge's env (GLM/RESEND/RH_ACCOUNT/NOTIFY/CONGRESS/TELEGRAM_BOT_TOKEN, …) never
# reach the child — then no `/proc/self/environ`, `printenv`, or env read the assistant might do
# can leak them (audit: the secret-exfil cluster). The Robinhood broker OAuth lives in the shared
# CLAUDE_CONFIG_DIR and is excluded from the chat at load time by --strict-mcp-config.
_CHILD_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ",
    "CLAUDE_CONFIG_DIR", "npm_config_cache", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    # Claude auth (needed for the child to run). NOT a broker credential; only present on boxes
    # that auth via a token rather than an on-box `claude login` (config-dir OAuth).
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
)


def _child_env() -> dict:
    """A minimal, secret-free environment for the `claude` subprocess (allowlist, not denylist)."""
    return {k: os.environ[k] for k in _CHILD_ENV_KEYS if os.environ.get(k)}


def _claude_cmd(question: str) -> list:
    return [
        CLAUDE_BIN, "-p", "--output-format", "json",
        "--model", MODEL, "--effort", EFFORT,
        # Zero MCP servers: strict mode + an empty config overrides the box's user-scope
        # Robinhood OAuth server, so this assistant has NO broker and NO Resend.
        "--mcp-config", CHAT_MCP, "--strict-mcp-config",
        # The read-only guard hook is the mechanical gate (like the tick's order_guard).
        "--settings", CHAT_SETTINGS, "--add-dir", str(_REPO),
        "--max-turns", str(MAX_TURNS),
        "--append-system-prompt", SYSTEM_PROMPT,
        # Unattended: no interactive approval possible. The guard hook + strict-mcp are the gate.
        "--dangerously-skip-permissions",
        question,
    ]


def answer(question: str, history_block: str = "") -> tuple[str, bool]:
    """Run one read-only claude pass and return (final_text, ok). `history_block` is a prior-thread
    context block (empty for a stateless call) prepended to the question via chat_history — with an
    empty block the prompt is byte-identical to the classic single-question prompt. The `ok` flag
    lets callers record only real answers (never an error/timeout note) into thread memory."""
    q = (question or "").strip()[:MAX_INPUT_CHARS]
    if not q:
        return ("Ask me something about the bot — today's run, the strategy, or a decision trace.",
                False)
    prompt = chat_history.compose_prompt(history_block, q)
    # Run in its OWN process group (start_new_session) so a timeout can kill the WHOLE tree —
    # otherwise a heavy grandchild (e.g. a runaway chat_query.py) would orphan and keep burning.
    try:
        proc = subprocess.Popen(_claude_cmd(prompt), cwd=str(_REPO), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=_child_env(),
                                start_new_session=True)
    except Exception as e:  # noqa: BLE001 — a spawn failure must not crash the bridge
        _log({"event": "answer", "ok": False, "reason": f"{type(e).__name__}: {e}", "q": q[:200]})
        return ("I couldn't reach my reasoning engine just now. Try again in a moment.", False)
    try:
        stdout, stderr = proc.communicate(timeout=TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        _log({"event": "answer", "ok": False, "reason": "timeout", "q": q[:200]})
        return ("That took too long to work out — try a narrower question.", False)

    out = (stdout or "").strip()
    try:
        data = json.loads(out.splitlines()[-1]) if out else {}
    except (ValueError, IndexError):
        data = {}
    text = (data.get("result") or "").strip()
    ok = proc.returncode == 0 and not data.get("is_error") and bool(text)
    _log({"event": "answer", "ok": ok, "returncode": proc.returncode,
          "subtype": data.get("subtype"), "cost_usd": data.get("total_cost_usd"),
          "q": q[:200], "stderr": (stderr or "")[-200:] if not ok else ""})
    if not ok:
        # A common real cause is expired box Claude auth — surface a hint, not a stack trace.
        return ("I hit an error answering that. If this keeps happening the box's Claude login "
                "may need a refresh (/mcp on the box).", False)
    return (text, True)


# --- Telegram transport (stdlib urllib) -------------------------------------------------------
def _tg(method: str, params: dict, timeout: float) -> dict | None:
    body = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(_TG + method, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _log({"event": "tg_http_error", "method": method, "code": e.code,
              "body": (e.read()[:200].decode("utf-8", "replace") if e.fp else "")})
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        # A long-poll read timeout is normal (no updates) — don't spam the log for getUpdates.
        if method != "getUpdates":
            _log({"event": "tg_error", "method": method, "err": f"{type(e).__name__}: {e}"})
        return None
    if not payload.get("ok"):
        _log({"event": "tg_not_ok", "method": method, "desc": payload.get("description")})
        return None
    return payload.get("result")


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _to_telegram_html(text: str) -> str:
    """Convert the model's GitHub-flavored markdown to the SMALL subset Telegram renders
    (<b>, <code>, <pre>). Everything else is HTML-escaped or flattened — so literal `**`/backticks
    stop showing, tables collapse to readable lines, and no markdown Telegram ignores leaks
    through. Markdown links become `text (url)` (visible URL, never a hidden href)."""
    stash: list = []

    def _stash(tag, content):
        stash.append((tag, content))
        return f"\x00{len(stash) - 1}\x00"

    # 1) pull code blocks/spans out BEFORE escaping so their contents aren't transformed.
    text = re.sub(r"```[^\n]*\n?(.*?)```", lambda m: _stash("pre", m.group(1).rstrip("\n")),
                  text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", lambda m: _stash("code", m.group(1)), text)
    # 2) links -> "text (url)" (URL stays visible; no deceptive hidden href)
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    # 3) escape the remaining prose (so any <, >, & in the answer is safe)
    text = _htmllib.escape(text, quote=False)
    # 4) bold: **x** / __x__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text)
    # 5) line-level: headers -> bold, bullets -> •, drop table separators, flatten table rows
    out = []
    for ln in text.split("\n"):
        s = ln.rstrip()
        stripped = s.strip()
        if stripped and set(stripped) <= set("|:- ") and "-" in stripped:
            continue  # markdown table separator (|---|:--:|) or horizontal rule
        h = re.match(r"#{1,6}\s+(.*)", stripped)
        if h:
            out.append(f"<b>{h.group(1)}</b>"); continue
        s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)  # bullet
        if s.count("|") >= 2:  # a table data row -> space-separated cells
            s = "  ".join(c.strip() for c in s.strip().strip("|").split("|"))
        out.append(s)
    text = "\n".join(out)
    # 6) restore code (escaped, wrapped in the tag)
    text = re.sub(r"\x00(\d+)\x00",
                  lambda m: (lambda tg, c: f"<{tg}>{_htmllib.escape(c, quote=False)}</{tg}>")(*stash[int(m.group(1))]),
                  text)
    return text


def _strip_md(text: str) -> str:
    """Plain-text fallback: drop markup so a Telegram HTML parse failure still delivers a clean
    message (no literal ** / backticks)."""
    text = re.sub(r"```[^\n]*\n?(.*?)```", r"\1", text, flags=re.DOTALL).replace("`", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.M)
    return _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)


def _chunks(text: str, limit: int):
    """Split at line boundaries so an HTML tag is never cut across two messages."""
    out, buf, cur = [], [], 0
    for ln in text.split("\n"):
        while len(ln) > limit:  # a single over-long line: hard-split
            out.append(ln[:limit]); ln = ln[limit:]
        if cur + len(ln) + 1 > limit and buf:
            out.append("\n".join(buf)); buf, cur = [], 0
        buf.append(ln); cur += len(ln) + 1
    if buf:
        out.append("\n".join(buf))
    return out or [text]


def send_message(chat_id, text: str) -> None:
    text = text or "(no answer)"
    # Chunk the ORIGINAL below the 4096 cap with headroom for the <b>/<code> tags we add.
    for chunk in _chunks(text, _MSG_LIMIT - 400):
        # Try Telegram HTML (renders bold/monospace); on ANY failure resend as clean plain text
        # so a message is never lost to a parse error.
        r = _tg("sendMessage", {"chat_id": chat_id, "text": _to_telegram_html(chunk),
                                "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30)
        if r is None:
            _tg("sendMessage", {"chat_id": chat_id, "text": _strip_md(chunk),
                                "disable_web_page_preview": True}, timeout=30)


def _typing(chat_id) -> None:
    _tg("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)


HELP = (
    "I'm Quiver — your bot's read-only analyst. Ask me things like:\n"
    "• how did today's run go?\n"
    "• what did it do today?\n"
    "• what's the current strategy / book?\n"
    "• why did it buy NVDA? (I'll trace the decision)\n"
    "• are we halted? any trades this week?\n\n"
    "I can look at the ledger, strategy, and logs — but I can't trade or change anything.\n"
    "I remember our conversation in this chat, so follow-ups work — send /clear to start fresh."
)


def _answer_with_memory(chat_id, question: str) -> str:
    """Answer one question WITH this chat's thread context, then record the exchange. Shared by the
    Telegram handler and the `--ask --chat-id` CLI so both drive the identical stateful path.

    The history READ is gated on HISTORY_ENABLED (not just the write): with history off — or an
    unusable chat id — `hp` is None, so nothing is loaded or written and the prompt is byte-identical
    to the classic stateless bridge (chat_history.compose_prompt("", q) == q). Only a real answer
    (`ok`) is recorded, so error/timeout notes never pollute the thread."""
    hp = (chat_history.path_for(HISTORY_DIR, chat_id)
          if (HISTORY_ENABLED and chat_id is not None) else None)
    block = ""
    if hp is not None:
        block = chat_history.render_context(chat_history.load_turns(hp),
                                            HISTORY_TURNS, HISTORY_CHARS, HISTORY_TURN_CHARS)
    reply, ok = answer(question, block)
    if ok and hp is not None:
        chat_history.append_turn(hp, "user", (question or "").strip()[:MAX_INPUT_CHARS], HISTORY_STORE)
        chat_history.append_turn(hp, "assistant", reply, HISTORY_STORE)
    return reply


def _handle_message(msg: dict, notified_unauth: set) -> None:
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return
    # Only answer PRIVATE chats — the allowlist holds personal user ids. A group/channel chat.id
    # (Telegram makes these negative / type != "private") would let anyone the operator added to
    # that group post to the bot, silently voiding "only you can talk to it".
    if chat.get("type") not in (None, "private") or (isinstance(chat_id, int) and chat_id < 0):
        _log({"event": "non_private_chat_ignored", "chat_id": chat_id, "type": chat.get("type")})
        return
    if str(chat_id) not in ALLOWED:
        # Pairing helper: tell an unknown chat its own id ONCE, so setup is copy-paste. Never
        # reveals anything but the chat's own id; capped so it can't be turned into a spammer.
        if str(chat_id) not in notified_unauth and len(notified_unauth) < 50:
            notified_unauth.add(str(chat_id))
            send_message(chat_id, f"Not authorized. Your chat id is {chat_id} — add it to "
                                  "TELEGRAM_ALLOWED_CHAT_IDS to enable this bot.")
        _log({"event": "unauthorized", "chat_id": chat_id})
        return
    low = text.lower()
    if low in ("/start", "/help", "start", "help"):
        send_message(chat_id, HELP)
        return
    if low in ("/clear", "/reset", "/new"):
        hp = chat_history.path_for(HISTORY_DIR, chat_id) if HISTORY_ENABLED else None
        if hp is not None:
            chat_history.clear(hp)
        _log({"event": "clear", "chat_id": chat_id})
        send_message(chat_id, "Cleared this thread's context — starting fresh.")
        return
    _log({"event": "question", "chat_id": chat_id, "text": text[:200]})
    _typing(chat_id)
    send_message(chat_id, _answer_with_memory(chat_id, text))


def _drain(offset_holder: list) -> None:
    """On startup, confirm any backlog WITHOUT answering it, so a restart never replays stale
    questions. Advances the offset past the newest pending update."""
    pending = _tg("getUpdates", {"timeout": 0, "allowed_updates": ["message"]}, timeout=15) or []
    if pending:
        offset_holder[0] = max(u["update_id"] for u in pending) + 1
        _log({"event": "drained", "count": len(pending), "next_offset": offset_holder[0]})


def run() -> int:
    if not TOKEN:
        _log({"event": "no_token", "note": "TELEGRAM_BOT_TOKEN unset — chat bridge idle"})
        return 0
    if not ALLOWED:
        _log({"event": "no_allowlist",
              "note": "TELEGRAM_ALLOWED_CHAT_IDS unset — refusing to run without an allowlist"})
        return 0
    _log({"event": "start", "model": MODEL, "allowed": sorted(ALLOWED)})
    offset_holder = [None]
    _drain(offset_holder)
    notified_unauth: set = set()
    while True:
        params = {"timeout": LONG_POLL_SEC, "allowed_updates": ["message"]}
        if offset_holder[0] is not None:
            params["offset"] = offset_holder[0]
        # Socket timeout must exceed the long-poll or urlopen would abort every empty poll.
        updates = _tg("getUpdates", params, timeout=LONG_POLL_SEC + 15)
        if not updates:
            continue
        for u in updates:
            offset_holder[0] = u["update_id"] + 1
            msg = u.get("message")
            if msg:
                try:
                    _handle_message(msg, notified_unauth)
                except Exception as e:  # noqa: BLE001 — one bad message must not kill the loop
                    _log({"event": "handle_error", "err": f"{type(e).__name__}: {e}"})


def main(argv: list) -> int:
    if "--print-cmd" in argv:
        print(" ".join(_claude_cmd("EXAMPLE QUESTION")))
        return 0
    if "--ask" in argv:
        i = argv.index("--ask")
        q = argv[i + 1] if i + 1 < len(argv) else ""
        # Optional `--chat-id N` drives the SAME stateful thread path the Telegram handler uses
        # (load context → answer → record), so a two-turn CLI run proves memory carries. Without
        # it, chat_id is None → stateless, byte-identical to the classic smoke test.
        cid = None
        if "--chat-id" in argv:
            j = argv.index("--chat-id")
            cid = argv[j + 1] if j + 1 < len(argv) else None
        print(_answer_with_memory(cid, q))
        return 0
    return run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
