"""Claude Agent SDK options for the desk agent, the analyst and the small jobs (plan §5).

Three roles:
  * desk agent (``quiver-chat``): Sonnet 5.5, a resident session, the D tools.
  * analyst (``quiver-worker``): Opus 5.5, one ``query()`` per job, the A tools.
  * small jobs (news classifier, quick reaction): Haiku 5.5, no tools, one turn.

The SDK starts the CLI with the parent's whole ``os.environ`` and lays ``options.env`` on
top. So each process that runs an agent loads ``Runtime`` and ``Secrets`` first, then calls
``scrub_process_env()`` before it creates any SDK client. No code reads ``os.environ``
after the scrub, and no tool returns a secret.

The SDK gives a tool handler only its arguments, and a ``ContextVar`` set before
``query()`` never reaches the handler task. So the bridge keeps one mutable ``TurnState``
per chat and the worker one ``JobState`` per job; the tool closures read them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lib.desk import guard

DEFAULT_MODELS = {
    "QUIVER_DESK_MODEL": "claude-sonnet-5-5",
    "QUIVER_ANALYST_MODEL": "claude-opus-5-5",
    "QUIVER_CLASSIFIER_MODEL": "claude-haiku-5-5",
    "QUIVER_BROKER_MODEL": "claude-sonnet-5-5",
}
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "XDG_CACHE_HOME", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CLAUDE_BIN", "ANTHROPIC_API_KEY")
DESK_CONFIG_DIR = "/opt/quiver/state/claude-desk"
DESK_MAX_TURNS = 60
ANALYST_MAX_TURNS = 80


@dataclass
class Runtime:
    """Non-secret runtime values, read once at start."""
    root: str
    ledger_db: str
    desk_model: str
    analyst_model: str
    classifier_model: str
    broker_model: str
    claude_bin: Optional[str]
    config_dir: str
    cfg: object = None

    @classmethod
    def from_env(cls, env=None, *, load_cfg: bool = True) -> "Runtime":
        e = os.environ if env is None else env
        root = e.get("QUIVER_ROOT") or str(Path(__file__).resolve().parent.parent.parent)
        cfg = None
        if load_cfg:
            from lib.config import load_config
            cfg = load_config(Path(e.get("QUIVER_CONFIG") or Path(root) / "config.yaml"))
        return cls(root=root, ledger_db=e.get("QUIVER_LEDGER_DB") or str(Path(root) / "state" / "ledger.db"),
                   desk_model=e.get("QUIVER_DESK_MODEL") or DEFAULT_MODELS["QUIVER_DESK_MODEL"],
                   analyst_model=e.get("QUIVER_ANALYST_MODEL") or DEFAULT_MODELS["QUIVER_ANALYST_MODEL"],
                   classifier_model=e.get("QUIVER_CLASSIFIER_MODEL") or DEFAULT_MODELS["QUIVER_CLASSIFIER_MODEL"],
                   broker_model=e.get("QUIVER_BROKER_MODEL") or DEFAULT_MODELS["QUIVER_BROKER_MODEL"],
                   claude_bin=e.get("CLAUDE_BIN") or None,
                   config_dir=e.get("CLAUDE_CONFIG_DIR") or DESK_CONFIG_DIR, cfg=cfg)


@dataclass
class Secrets:
    """Every secret a desk process needs, moved out of ``os.environ`` before the scrub."""
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""
    telegram_alert_chat_ids: str = ""
    alpha_vantage_api_key: str = ""
    sec_user_agent: str = ""

    @classmethod
    def from_env(cls, env=None) -> "Secrets":
        e = os.environ if env is None else env
        return cls(telegram_bot_token=(e.get("TELEGRAM_BOT_TOKEN") or "").strip(),
                   telegram_allowed_chat_ids=(e.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip(),
                   telegram_alert_chat_ids=(e.get("TELEGRAM_ALERT_CHAT_IDS") or "").strip(),
                   alpha_vantage_api_key=(e.get("ALPHA_VANTAGE_API_KEY") or "").strip(),
                   sec_user_agent=(e.get("SEC_USER_AGENT") or "").strip())

    @property
    def owner_ids(self) -> list:
        return [s for s in self.telegram_allowed_chat_ids.replace(",", " ").split() if s]

    def telegram_env(self) -> dict:
        return {"TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
                "TELEGRAM_ALLOWED_CHAT_IDS": self.telegram_allowed_chat_ids,
                "TELEGRAM_ALERT_CHAT_IDS": self.telegram_alert_chat_ids}

    def __repr__(self) -> str:  # never print a secret by accident
        return "Secrets(<redacted>)"


def scrub_process_env() -> list:
    """Delete every ``os.environ`` key not in the allowlist. Returns the removed keys."""
    removed = [k for k in list(os.environ) if k not in ENV_ALLOWLIST]
    for k in removed:
        del os.environ[k]
    return removed


@dataclass
class TurnState:
    """The per-turn facts the desk tools read (one object per chat, refilled per turn)."""
    chat_id: str = ""
    owner_message_ids: list = field(default_factory=list)
    reacted_ids: set = field(default_factory=set)
    turn_started_at: Optional[str] = None
    session_id: Optional[str] = None
    said: list = field(default_factory=list)
    first_reply_to: Optional[int] = None

    def start(self, *, chat_id, owner_message_ids, started_at, reacted=()):
        self.chat_id = str(chat_id)
        self.owner_message_ids = [int(x) for x in owner_message_ids]
        self.reacted_ids = {int(x) for x in reacted}
        self.turn_started_at = started_at
        self.said = []
        self.first_reply_to = max(self.owner_message_ids) if self.owner_message_ids else None

    def unreacted(self) -> list:
        return [m for m in self.owner_message_ids if m not in self.reacted_ids]


@dataclass
class JobState:
    """The per-job facts the analyst tools read."""
    job_id: Optional[int] = None
    event_id: Optional[int] = None
    role: str = "analyst"
    kind: str = ""
    verdict_recorded: bool = False
    proposal_ids: list = field(default_factory=list)
    messages: int = 0


def make_hook(allowed: frozenset, root: str):
    """The SDK PreToolUse hook for a role. It wraps its whole body in try/except and
    returns the deny result from the except branch: the SDK turns a hook exception into an
    error reply, not into a deny."""

    def deny(reason: str) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    async def hook(input_data, tool_use_id, context):
        try:
            name = input_data.get("tool_name") if isinstance(input_data, dict) else None
            ti = input_data.get("tool_input") if isinstance(input_data, dict) else None
            ok, reason = guard.evaluate(name, ti or {}, allowed=allowed, root=root)
            if not ok:
                return deny(reason)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                           "permissionDecisionReason": reason}}
        except Exception as e:  # noqa: BLE001 — any hook failure denies
            return deny(f"hook error, denied: {type(e).__name__}")

    return hook


def _base_env() -> dict:
    return {"CLAUDE_CONFIG_DIR": DESK_CONFIG_DIR, "DISABLE_AUTOUPDATER": "1", "ENABLE_TOOL_SEARCH": "false"}


def role_options(runtime: Runtime, *, role: str, server, tool_names: list, system_prompt: str,
                 model: str, max_turns: int, resume: Optional[str] = None):
    """``ClaudeAgentOptions`` for the desk agent or the analyst (§5.2)."""
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
    allowed = frozenset([f"mcp__desk__{n}" for n in tool_names] + list(guard.BUILTINS))
    env = _base_env()
    if runtime.config_dir and runtime.config_dir != DESK_CONFIG_DIR and not runtime.config_dir.endswith("claude-config"):
        env["CLAUDE_CONFIG_DIR"] = runtime.config_dir
    kw = dict(system_prompt=system_prompt, tools=list(guard.BUILTINS), mcp_servers={"desk": server},
              strict_mcp_config=True, allowed_tools=sorted(allowed), permission_mode="bypassPermissions",
              hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_hook(allowed, runtime.root)])]},
              setting_sources=[], cwd=runtime.root, env=env, model=model, max_turns=max_turns)
    if runtime.claude_bin:
        kw["cli_path"] = runtime.claude_bin
    if resume:
        kw["resume"] = resume
    return ClaudeAgentOptions(**kw)


def small_job_options(runtime: Runtime, *, system_prompt: str):
    """The news classifier and the quick reaction: no tools, no MCP server, one turn, no
    session persistence, and the same hook with an empty allowlist."""
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
    env = _base_env()
    if runtime.config_dir and runtime.config_dir != DESK_CONFIG_DIR and not runtime.config_dir.endswith("claude-config"):
        env["CLAUDE_CONFIG_DIR"] = runtime.config_dir
    kw = dict(system_prompt=system_prompt, tools=[], allowed_tools=[], strict_mcp_config=True,
              setting_sources=[], max_turns=1, env=env, model=runtime.classifier_model, cwd=runtime.root,
              permission_mode="bypassPermissions", extra_args={"no-session-persistence": None},
              hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_hook(frozenset(), runtime.root)])]})
    if runtime.claude_bin:
        kw["cli_path"] = runtime.claude_bin
    return ClaudeAgentOptions(**kw)


def desk_options(runtime: Runtime, secrets: Secrets, turn_state: TurnState, *, ctx=None,
                 resume: Optional[str] = None):
    """The desk agent's options with its tool server. ``ctx`` is a ``tools.ToolContext``;
    without one a context is built from ``runtime`` and ``secrets``."""
    from lib.desk import tools
    from lib.prompts import load_prompt
    ctx = ctx or tools.ToolContext.build(role="desk", runtime=runtime, secrets=secrets, turn=turn_state)
    server = tools.build_server(ctx)
    return role_options(runtime, role="desk", server=server, tool_names=tools.role_tool_names("desk"),
                        system_prompt=load_prompt("desk"), model=runtime.desk_model,
                        max_turns=DESK_MAX_TURNS, resume=resume)


def analyst_options(runtime: Runtime, secrets: Secrets, job_state: JobState, *, ctx=None):
    from lib.desk import tools
    from lib.prompts import load_prompt
    ctx = ctx or tools.ToolContext.build(role="analyst", runtime=runtime, secrets=secrets, job=job_state)
    server = tools.build_server(ctx)
    return role_options(runtime, role="analyst", server=server, tool_names=tools.role_tool_names("analyst"),
                        system_prompt=load_prompt("analyst"), model=runtime.analyst_model,
                        max_turns=ANALYST_MAX_TURNS)
