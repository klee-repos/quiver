#!/usr/bin/env python3
"""The desk agent's and the analyst's SDK options and PreToolUse hook (plan §5.1, §5.2, §13.4,
§13.5; ``lib/desk/agent.py`` and ``lib/desk/guard.py``).

The hook under test is the one inside the built ``ClaudeAgentOptions`` (``options.hooks``), so a
case tests what the SDK gets. No case starts a CLI: the options are built, and the SDK's own
command builder (``SubprocessCLITransport._build_command``) shows the CLI flags. The scrub runs
in a SUBPROCESS, because it deletes keys from the process environment.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import REPO, FakeClock, FakePrices, Results, seed_desk, snapshot, temp_root  # noqa: E402

import claude_agent_sdk  # noqa: E402
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport  # noqa: E402
from lib.desk import agent, guard, outbox, store, tools  # noqa: E402

r = Results("desk-agent-hook")
PY = str(REPO / ".venv" / "bin" / "python")
OWNER = "987654321"
CLAUDE_BIN = "/opt/quiver/bin/claude-pinned"
ALLOWLIST = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "XDG_CACHE_HOME", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_BIN",
             "ANTHROPIC_API_KEY"}
BUILTINS = ["Read", "Grep", "Glob", "WebSearch"]

# §5.4, written out from the plan tables (the spec, not the code).
READ = ["portfolio_view", "refresh_portfolio", "get_quotes", "get_book", "list_companies", "get_company",
        "search_research", "list_filings", "get_filing_sections", "get_document", "get_financials", "get_metrics",
        "run_valuation", "get_technicals", "get_transcript", "get_etf_lookthrough", "get_events", "get_proposals",
        "get_track_record", "get_attribution", "get_settings", "ledger_query", "screen_companies"]
D_WRITE = ["react", "say", "load_company", "fetch_document", "record_finding", "add_note", "set_research_status",
           "log_news_event", "start_deep_dive", "withdraw_proposal", "propose_trade", "propose_change",
           "bring_up_next", "close_item", "defer_item", "hold_agenda", "show_card"]
A_WRITE = ["report", "load_company", "fetch_document", "record_finding", "add_note", "log_news_event",
           "record_verdict", "withdraw_proposal", "propose_trade", "propose_change"]
D_TOOLS = READ + D_WRITE
A_TOOLS = READ + A_WRITE
# The secret paths of today's tests/test_chat_guard.py (at 889a2ad), as (tool, input).
CHAT_GUARD_SECRETS = [
    ("Read", {"file_path": ".env"}), ("Read", {"file_path": "/etc/quiver/quiver.env"}),
    ("Read", {"file_path": "state/claude-config/credentials.json"}), ("Grep", {"pattern": "x", "path": "../.env"}),
    ("Read", {"file_path": "/etc/passwd"}), ("Read", {"file_path": "../../etc/shadow"}),
    ("Read", {"file_path": "CLAUDE.md"}), ("Read", {"file_path": "/proc/self/environ"}),
    ("Glob", {"pattern": "/proc/*/environ"}), ("Grep", {"pattern": "KEY", "path": "/proc/self/environ"}),
    ("Grep", {"pattern": "^[A-Z_]+=", "path": "/etc"}), ("Grep", {"pattern": "sk-", "path": "state"}),
    ("Glob", {"pattern": "**/*", "path": "state/.."}), ("Glob", {"pattern": "logs/*.log"}),
    ("Read", {"file_path": "logs/tick.log"}), ("Read", {"file_path": "strategy.yaml"}),
]


class FakeTransport:
    """The Telegram transport seam: records (method, params) and answers ok."""

    def __init__(self):
        self.calls = []
        self.n = 500

    def __call__(self, method, params):
        self.calls.append((method, params))
        self.n += 1
        return {"ok": True, "result": {"message_id": self.n}}


def make_secrets():
    return agent.Secrets(telegram_bot_token="111222:SECRET-TOKEN-zz", telegram_allowed_chat_ids=OWNER,
                         telegram_alert_chat_ids=OWNER, alpha_vantage_api_key="AVSECRETKEY99",
                         sec_user_agent="Quiver test@example.com")


def make_runtime(root, **env_extra):
    env = {"QUIVER_ROOT": str(root.path), "QUIVER_LEDGER_DB": str(root.db),
           "QUIVER_CONFIG": str(root.path / "config.yaml"), "CLAUDE_BIN": CLAUDE_BIN, **env_extra}
    return agent.Runtime.from_env(env)


def build_ctx(root, runtime, role, *, turn=None, job=None, transport=None):
    tp = transport or FakeTransport()
    led = root.led()
    box = outbox.Outbox(led, transport=tp, chat_id=OWNER, sleep=lambda s: None)
    return tools.ToolContext.build(role=role, runtime=runtime, secrets=make_secrets(), turn=turn, job=job, led=led,
                                   transport=tp, box=box, yf_price_fn=FakePrices())


def hook_of(options):
    m = options.hooks["PreToolUse"]
    return m[0].hooks[0]


def call_hook(hook, name, ti=None):
    out = asyncio.run(hook({"tool_name": name, "tool_input": ti or {}}, "toolu_1", None))
    hs = (out or {}).get("hookSpecificOutput") or {}
    return hs.get("permissionDecision") == "allow", hs


def cli_argv(options):
    return SubprocessCLITransport("x", options)._build_command()


def flag_value(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        return argv[i + 1] if i + 1 < len(argv) else None
    for a in argv:
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def setup_dirs(root):
    """The repo dirs the path rule names, plus the things it must hide, in a temp root."""
    for d in ("lib/desk", "deploy/runner", "docs", "prompts", "tests", "state/tmp"):
        (root.path / d).mkdir(parents=True, exist_ok=True)
    (root.path / "docs" / "DESK_PLAN.md").write_text("plan", encoding="utf-8")
    (root.path / "lib" / "desk" / "tools.py").write_text("x", encoding="utf-8")
    (root.path / ".env").write_text("TELEGRAM_BOT_TOKEN=x", encoding="utf-8")
    (root.path / "tick.py").write_text("x", encoding="utf-8")
    link = root.path / "lib" / "etc_link"
    if not link.exists():
        os.symlink("/etc", link)
    flink = root.path / "docs" / "passwd.md"
    if not flink.exists():
        os.symlink("/etc/passwd", flink)


def with_root(fn):
    clock = FakeClock("2026-08-05T14:00:00Z").pin_market()
    try:
        with temp_root() as root:
            fn(root, clock)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.ok(f"{fn.__name__}: ran without an exception", False, f"{type(e).__name__}: {e}")
    finally:
        clock.unpin()


# --- roles -------------------------------------------------------------------------

def case_roles(root, clock):
    rt = make_runtime(root)
    d_opts = agent.desk_options(rt, make_secrets(), agent.TurnState(), ctx=build_ctx(root, rt, "desk",
                                                                                   turn=agent.TurnState()))
    a_opts = agent.analyst_options(rt, make_secrets(), agent.JobState(), ctx=build_ctx(root, rt, "analyst",
                                                                                     job=agent.JobState()))
    dh, ah = hook_of(d_opts), hook_of(a_opts)
    d_pass = [t for t in D_TOOLS if call_hook(dh, f"mcp__desk__{t}")[0]]
    r.eq("roles: desk: every D tool passes", sorted(d_pass), sorted(D_TOOLS))
    for t in ("report", "record_verdict"):
        r.ok(f"roles: desk: {t} is denied", not call_hook(dh, f"mcp__desk__{t}")[0])
    a_pass = [t for t in A_TOOLS if call_hook(ah, f"mcp__desk__{t}")[0]]
    r.eq("roles: analyst: every A tool passes", sorted(a_pass), sorted(A_TOOLS))
    for t in ("react", "say", "start_deep_dive", "set_research_status", "bring_up_next", "close_item",
              "defer_item", "hold_agenda", "show_card"):
        r.ok(f"roles: analyst: {t} is denied", not call_hook(ah, f"mcp__desk__{t}")[0])
    r.eq("roles: desk allowed_tools = the D tools + the 4 built-ins", sorted(d_opts.allowed_tools),
         sorted([f"mcp__desk__{t}" for t in D_TOOLS] + BUILTINS))
    r.eq("roles: analyst allowed_tools = the A tools + the 4 built-ins", sorted(a_opts.allowed_tools),
         sorted([f"mcp__desk__{t}" for t in A_TOOLS] + BUILTINS))
    r.ok("roles: an unknown mcp__desk__ tool is denied", not call_hook(dh, "mcp__desk__approve_proposal")[0])


# --- banned ------------------------------------------------------------------------

def case_banned(root, clock):
    rt = make_runtime(root)
    for role, opts in (("desk", agent.desk_options(rt, make_secrets(), agent.TurnState(),
                                                   ctx=build_ctx(root, rt, "desk", turn=agent.TurnState()))),
                       ("analyst", agent.analyst_options(rt, make_secrets(), agent.JobState(),
                                                         ctx=build_ctx(root, rt, "analyst", job=agent.JobState())))):
        h = hook_of(opts)
        for t, ti in (("Bash", {"command": "ls"}), ("Write", {"file_path": "lib/x.py", "content": "x"}),
                      ("Edit", {"file_path": "lib/x.py"}), ("NotebookEdit", {}), ("WebFetch", {"url": "https://x.com"}),
                      ("Task", {"prompt": "x"}), ("mcp__other__x", {}), ("LS", {"path": "lib"}),
                      ("Frobnicate", {}), ("ToolSearch", {"query": "select:Bash"}), ("Agent", {}),
                      ("mcp__robinhood-trading__place_equity_order", {"ref_id": "x"})):
            r.ok(f"banned[{role}]: {t} -> deny", not call_hook(h, t, ti)[0])
        r.ok(f"banned[{role}]: WebSearch passes", call_hook(h, "WebSearch", {"query": "ANET earnings"})[0])


# --- paths and recursion ------------------------------------------------------------

def case_paths(root, clock):
    setup_dirs(root)
    rt = make_runtime(root)
    h = hook_of(agent.desk_options(rt, make_secrets(), agent.TurnState(),
                                   ctx=build_ctx(root, rt, "desk", turn=agent.TurnState())))
    ok = lambda t, ti: call_hook(h, t, ti)[0]  # noqa: E731
    r.ok("paths: Read docs/DESK_PLAN.md passes", ok("Read", {"file_path": "docs/DESK_PLAN.md"}))
    r.ok("paths: Read the absolute docs/DESK_PLAN.md passes",
         ok("Read", {"file_path": str(root.path / "docs" / "DESK_PLAN.md")}))
    for f in ("tick.py", "config.yaml", "EXECUTE.md", "SNAPSHOT.md", "README.md"):
        r.ok(f"paths: Read {f} at the root passes", ok("Read", {"file_path": f}))
    r.ok("paths: Grep lib/ passes", ok("Grep", {"pattern": "def ", "path": "lib/"}))
    r.ok("paths: Glob docs/ passes", ok("Glob", {"pattern": "*.md", "path": "docs/"}))
    r.ok("paths: Grep with no path is denied", not ok("Grep", {"pattern": "x"}))
    r.ok("paths: Glob with no path is denied", not ok("Glob", {"pattern": "**/*.py"}))
    r.ok("paths: Grep on the repo root is denied", not ok("Grep", {"pattern": "x", "path": str(root.path)}))
    r.ok("paths: Grep on . is denied", not ok("Grep", {"pattern": "x", "path": "."}))
    denied = {"/etc/passwd": ("Read", {"file_path": "/etc/passwd"}),
              "../../etc/shadow": ("Read", {"file_path": "../../etc/shadow"}),
              "a symlink to /etc": ("Read", {"file_path": "lib/etc_link/passwd"}),
              "Grep in a symlink to /etc": ("Grep", {"pattern": "root", "path": "lib/etc_link"}),
              "a docs/ file symlink to /etc/passwd": ("Read", {"file_path": "docs/passwd.md"}),
              "state/ledger.db": ("Read", {"file_path": "state/ledger.db"}),
              "state/tmp/x.json": ("Read", {"file_path": "state/tmp/x.json"}),
              "Grep state/": ("Grep", {"pattern": "x", "path": "state"}),
              ".env": ("Read", {"file_path": ".env"}),
              "/etc/quiver/quiver.env": ("Read", {"file_path": "/etc/quiver/quiver.env"}),
              "lib/../.env": ("Read", {"file_path": "lib/../.env"}),
              "~/.ssh/id_rsa": ("Read", {"file_path": "~/.ssh/id_rsa"}),
              "a secret name inside lib/": ("Read", {"file_path": "lib/secret_token.py"}),
              "Glob pattern with ..": ("Glob", {"pattern": "../*", "path": "lib"}),
              "Glob absolute pattern": ("Glob", {"pattern": "/etc/*", "path": "lib"})}
    for label, (t, ti) in denied.items():
        r.ok(f"paths: {label} is denied", not ok(t, ti))
    for t, ti in CHAT_GUARD_SECRETS:
        r.ok(f"paths: chat-guard secret path {t} {json.dumps(ti)} is denied", not ok(t, ti))


def case_recursion(root, clock):
    setup_dirs(root)
    rt = make_runtime(root)
    h = hook_of(agent.desk_options(rt, make_secrets(), agent.TurnState(),
                                   ctx=build_ctx(root, rt, "desk", turn=agent.TurnState())))
    ok = lambda t, ti: call_hook(h, t, ti)[0]  # noqa: E731
    for t in ("Grep", "Glob"):
        r.ok(f"recursion: {t} on the root (/opt/quiver in production) is denied",
             not ok(t, {"pattern": "*", "path": rt.root}))
        r.ok(f"recursion: {t} on the root with a trailing slash is denied",
             not ok(t, {"pattern": "*", "path": rt.root + "/"}))
        r.ok(f"recursion: {t} with no path is denied", not ok(t, {"pattern": "*"}))
        r.ok(f"recursion: {t} with an empty path is denied", not ok(t, {"pattern": "*", "path": " "}))
        r.ok(f"recursion: {t} on lib/ passes (G11)", ok(t, {"pattern": "*", "path": "lib/"}))
    # A hook exception: a NUL byte makes realpath raise inside the guard.
    allow, hs = call_hook(h, "Read", {"file_path": "lib/a\x00b.py"})
    r.ok("recursion: a hook exception -> deny", not allow and hs.get("permissionDecision") == "deny"
         and "hook error" in str(hs.get("permissionDecisionReason")), hs)


# --- options -----------------------------------------------------------------------

SCRUB_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
from lib.desk import agent
runtime = agent.Runtime.from_env()
secrets = agent.Secrets.from_env()
removed = agent.scrub_process_env()
out = {"keys": sorted(os.environ), "values": sorted(os.environ.values()), "removed": sorted(removed),
       "secrets": [secrets.telegram_bot_token, secrets.telegram_allowed_chat_ids, secrets.telegram_alert_chat_ids,
                   secrets.alpha_vantage_api_key, secrets.sec_user_agent],
       "acct": getattr(runtime.cfg, "account_number", None), "root": runtime.root,
       "desk_model": runtime.desk_model, "sdk_loaded_before": "claude_agent_sdk" in sys.modules}
opts = agent.small_job_options(runtime, system_prompt="x")
out["opts_env"] = opts.env
out["keys_after_options"] = sorted(os.environ)
print(json.dumps(out))
"""


def case_scrub(root, clock):
    secrets_env = {"TELEGRAM_BOT_TOKEN": "111222:SECRET-TOKEN-zz", "TELEGRAM_ALLOWED_CHAT_IDS": OWNER,
                   "TELEGRAM_ALERT_CHAT_IDS": "123123123", "ALPHA_VANTAGE_API_KEY": "AVSECRETKEY99",
                   "SEC_USER_AGENT": "Quiver test@example.com", "OPENROUTER_API_KEY": "sk-or-SECRET",
                   "RESEND_API_KEY": "re_SECRET", "CONGRESS_API_KEY": "CONGRESS-SECRET", "GLM_API_KEY": "GLM-SECRET",
                   "RH_ACCOUNT_NUMBER": "TEST1234", "AWS_SECRET_ACCESS_KEY": "AWS-SECRET"}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp", "LANG": "C.UTF-8", "TZ": "UTC",
           "XDG_CACHE_HOME": "/tmp/xdg", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-keep", "ANTHROPIC_API_KEY": "anth-keep",
           "CLAUDE_BIN": CLAUDE_BIN, "QUIVER_ROOT": str(root.path), "QUIVER_LEDGER_DB": str(root.db),
           "QUIVER_CONFIG": str(root.path / "config.yaml"), "QUIVER_DESK_MODEL": "claude-sonnet-5-5",
           "QUIVER_TELEGRAM_DISABLE": "1", **secrets_env}
    p = subprocess.run([PY, "-c", SCRUB_SCRIPT, str(REPO)], env=env, capture_output=True, text=True, timeout=120,
                       cwd=str(REPO))
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        r.ok("options: the scrub subprocess printed its result", False, (p.returncode, p.stdout[-500:], p.stderr[-1500:]))
        return
    r.ok("options: after scrub_process_env() the os.environ keys are a subset of the §5.2 allowlist",
         set(out["keys"]) <= ALLOWLIST, sorted(set(out["keys"]) - ALLOWLIST))
    r.ok("options: the scrub runs before the first SDK client (the SDK is not loaded yet)",
         out["sdk_loaded_before"] is False, out["sdk_loaded_before"])
    leaked = [v for v in secrets_env.values() if v in out["values"]]
    r.eq("options: no secret value stays in os.environ", leaked, [])
    r.eq("options: the Secrets object kept the 5 secrets", out["secrets"],
         ["111222:SECRET-TOKEN-zz", OWNER, "123123123", "AVSECRETKEY99", "Quiver test@example.com"])
    r.eq("options: Runtime loaded config.yaml before the scrub (account number)", out["acct"], "TEST1234")
    r.ok("options: the CLI auth keys stay (CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, CLAUDE_BIN)",
         {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_BIN"} <= set(out["keys"]), out["keys"])
    r.ok("options: building options does not put keys back in os.environ",
         set(out["keys_after_options"]) <= ALLOWLIST, out["keys_after_options"])
    r.eq("options: options.env holds no secret", out["opts_env"],
         {"CLAUDE_CONFIG_DIR": "/opt/quiver/state/claude-desk", "DISABLE_AUTOUPDATER": "1",
          "ENABLE_TOOL_SEARCH": "false"})


def case_options(root, clock):
    rt = make_runtime(root, QUIVER_DESK_MODEL="desk-model-x", QUIVER_ANALYST_MODEL="analyst-model-x",
                      QUIVER_CLASSIFIER_MODEL="classifier-model-x")
    ts = agent.TurnState()
    ctx = build_ctx(root, rt, "desk", turn=ts)
    o = agent.desk_options(rt, make_secrets(), ts, ctx=ctx)
    env_want = {"CLAUDE_CONFIG_DIR": "/opt/quiver/state/claude-desk", "DISABLE_AUTOUPDATER": "1",
                "ENABLE_TOOL_SEARCH": "false"}
    r.eq("options: setting_sources=[]", o.setting_sources, [])
    r.eq("options: strict_mcp_config=True", o.strict_mcp_config, True)
    r.eq("options: tools = Read, Grep, Glob, WebSearch", o.tools, BUILTINS)
    r.eq("options: options.env is the 3 §5.2 keys", o.env, env_want)
    r.eq("options: cli_path=$CLAUDE_BIN", str(o.cli_path), CLAUDE_BIN)
    r.eq("options: permission_mode=bypassPermissions", o.permission_mode, "bypassPermissions")
    r.eq("options: one MCP server, desk", sorted(o.mcp_servers), ["desk"])
    r.eq("options: the desk server is an in-process SDK server", (o.mcp_servers["desk"] or {}).get("type"), "sdk")
    r.eq("options: system_prompt is the full text of prompts/desk.md", o.system_prompt,
         (REPO / "prompts" / "desk.md").read_text(encoding="utf-8"))
    r.eq("options: cwd is the root", str(o.cwd), rt.root)
    r.eq("options: desk model from Runtime (QUIVER_DESK_MODEL)", o.model, "desk-model-x")
    r.eq("options: desk limit 60 tool turns (§5.1)", o.max_turns, 60)
    r.ok("options: one PreToolUse matcher for every tool", len(o.hooks.get("PreToolUse", [])) == 1
         and o.hooks["PreToolUse"][0].matcher is None and set(o.hooks) == {"PreToolUse"}, o.hooks)
    argv = cli_argv(o)
    r.eq("options: the CLI gets --tools Read,Grep,Glob,WebSearch", flag_value(argv, "--tools"), "Read,Grep,Glob,WebSearch")
    r.ok("options: the CLI gets --strict-mcp-config", "--strict-mcp-config" in argv, argv[:40])
    r.eq("options: the CLI gets --setting-sources= (empty)", flag_value(argv, "--setting-sources"), "")
    r.eq("options: the CLI gets --permission-mode bypassPermissions", flag_value(argv, "--permission-mode"),
         "bypassPermissions")
    r.eq("options: argv[0] is $CLAUDE_BIN", argv[0], CLAUDE_BIN)
    a = agent.analyst_options(rt, make_secrets(), agent.JobState(), ctx=build_ctx(root, rt, "analyst", job=agent.JobState()))
    r.eq("options: analyst model from Runtime (QUIVER_ANALYST_MODEL)", a.model, "analyst-model-x")
    r.eq("options: analyst limit 80 tool turns (§5.1)", a.max_turns, 80)
    r.eq("options: analyst system_prompt is prompts/analyst.md", a.system_prompt,
         (REPO / "prompts" / "analyst.md").read_text(encoding="utf-8"))
    r.ok("options: analyst setting_sources=[], strict MCP, the 4 built-ins, same env",
         a.setting_sources == [] and a.strict_mcp_config is True and a.tools == BUILTINS and a.env == env_want, a.env)
    # §5.1 defaults when no model variable is set.
    d = make_runtime(root)
    r.eq("options: §5.1 default models", (d.desk_model, d.analyst_model, d.classifier_model, d.broker_model),
         ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"))
    # Classifier and quick-reaction options.
    for name in ("news_classifier", "quick_reaction"):
        prompt = (REPO / "prompts" / f"{name}.md").read_text(encoding="utf-8")
        s = agent.small_job_options(rt, system_prompt=prompt)
        r.eq(f"options[{name}]: tools=[]", s.tools, [])
        r.eq(f"options[{name}]: allowed_tools=[]", s.allowed_tools, [])
        r.ok(f"options[{name}]: no MCP server", not s.mcp_servers, s.mcp_servers)
        r.eq(f"options[{name}]: setting_sources=[]", s.setting_sources, [])
        r.eq(f"options[{name}]: strict_mcp_config=True", s.strict_mcp_config, True)
        r.eq(f"options[{name}]: max_turns=1", s.max_turns, 1)
        r.eq(f"options[{name}]: the same env", s.env, env_want)
        r.eq(f"options[{name}]: classifier model from Runtime", s.model, "classifier-model-x")
        sargv = cli_argv(s)
        r.ok(f"options[{name}]: the CLI gets --no-session-persistence", "--no-session-persistence" in sargv, sargv[-20:])
        r.eq(f"options[{name}]: the CLI gets --tools '' (no built-in)", flag_value(sargv, "--tools"), "")
        sh = hook_of(s)
        for t, ti in (("Read", {"file_path": "lib/x.py"}), ("WebSearch", {"query": "x"}), ("mcp__desk__say", {"text": "x"}),
                      ("Grep", {"pattern": "x", "path": "lib"}), ("Bash", {"command": "true"})):
            r.ok(f"options[{name}]: the hook denies {t}", not call_hook(sh, t, ti)[0])


# --- hook_exc ----------------------------------------------------------------------

class Exploding(dict):
    def get(self, *a, **k):
        raise RuntimeError("boom")


def case_hook_exc(root, clock):
    for allowed in (frozenset(["Read", "mcp__desk__say"]), frozenset()):
        h = agent.make_hook(allowed, str(root.path))
        out = asyncio.run(h(Exploding(), "toolu_1", None))
        hs = (out or {}).get("hookSpecificOutput") or {}
        r.ok(f"hook_exc: an input that raises returns the deny dict (allowlist size {len(allowed)})",
             hs.get("permissionDecision") == "deny" and hs.get("hookEventName") == "PreToolUse", out)
    h = agent.make_hook(frozenset(["Read"]), str(root.path))
    out = asyncio.run(h({"tool_name": "Read", "tool_input": {"file_path": "lib/\x00x"}}, "t", None))
    r.eq("hook_exc: a guard exception (NUL byte path) returns deny",
         ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision"), "deny")
    out = asyncio.run(h(None, "t", None))
    r.eq("hook_exc: a None input returns deny", ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision"),
         "deny")


# --- turn_state --------------------------------------------------------------------

def capture_handlers(build):
    """Run ``build()`` with ``claude_agent_sdk.create_sdk_mcp_server`` wrapped, and return the
    tool objects (with their closures) that the desk server got."""
    real = claude_agent_sdk.create_sdk_mcp_server
    got = {}

    def spy(*a, **k):
        for t in k.get("tools") or []:
            got[t.name] = t
        return real(*a, **k)

    claude_agent_sdk.create_sdk_mcp_server = spy
    try:
        opts = build()
    finally:
        claude_agent_sdk.create_sdk_mcp_server = real
    return opts, got


def case_turn_state(root, clock):
    seed_desk(root, load=False)
    snapshot(root, at="2026-08-05T13:59:00Z")
    rt = make_runtime(root)
    ts = agent.TurnState()
    ctx = build_ctx(root, rt, "desk", turn=ts)
    opts, handlers = capture_handlers(lambda: agent.desk_options(rt, make_secrets(), ts, ctx=ctx))
    r.eq("turn_state: the desk server got exactly the D tools", sorted(handlers), sorted(D_TOOLS))
    pt = handlers["propose_trade"].handler
    args = {"tickers": ["ANET"], "origin": {"owner_message_id": 101}, "fraction": 1.0,
            "rationale": "owner asked", "evidence": []}

    res = asyncio.run(pt(args))
    r.ok("turn_state: before the turn is filled, owner message 101 is refused",
         res.get("is_error") and "not part of this turn" in res["content"][0]["text"], res)

    probe = contextvars.ContextVar("owner_ids", default=())

    async def scenario():
        gate = asyncio.Event()

        async def sdk_task():  # the SDK starts its tool task at connect(), before query()
            await gate.wait()
            return await pt(args)

        task = asyncio.create_task(sdk_task())
        await asyncio.sleep(0)
        probe.set((101,))           # a ContextVar set before query() never reaches that task
        ts.start(chat_id=OWNER, owner_message_ids=[101], started_at=store.now_iso())
        gate.set()
        return await task

    res = asyncio.run(scenario())
    body = {} if res.get("is_error") else json.loads(res["content"][0]["text"])
    r.ok("turn_state: owner_message_ids set on the TurnState before query() are what propose_trade checks",
         not res.get("is_error") and isinstance(body.get("proposal_id"), int), res)
    res = asyncio.run(pt({**args, "origin": {"owner_message_id": 102}}))
    r.ok("turn_state: an owner message that is not in this turn is refused",
         res.get("is_error") and "not part of this turn" in res["content"][0]["text"], res)
    for f in ("lib/desk/tools.py", "lib/desk/agent.py"):
        src = (REPO / f).read_text(encoding="utf-8")
        code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
        r.ok(f"turn_state: {f} does not use a ContextVar", "ContextVar" not in code.replace("``ContextVar``", "")
             and "contextvars" not in code, f)


def main() -> int:
    for case in (case_roles, case_banned, case_paths, case_recursion, case_scrub, case_options, case_hook_exc,
                 case_turn_state):
        with_root(case)
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
