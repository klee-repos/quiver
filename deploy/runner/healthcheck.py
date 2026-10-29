#!/usr/bin/env python3
"""The box self-test (plan §15.5). setup.sh and update.sh gate on it.

Offline mode (no flag) uses no network and no broker. It proves that the box loads the
config and the XNYS calendar, applies the full ledger schema, imports every module and
runner, runs the broker guard, and holds the env, the directories and the runbooks that
the units need.

Live mode (``--live``) runs the offline checks and then the live checks: the units and
their walls, the live ledger, the Claude logins, the desk tools, Robinhood, SEC EDGAR,
Telegram and Alpha Vantage. Run it by hand at the cutover and after a re-auth.

The script prints one JSON line ``{"ok": bool, "checks": {...}}``. Each value is ``ok``,
``FAIL: <reason>``, ``WARN: <reason>`` or ``SKIP: <reason>``. Only a FAIL makes ``ok``
false. A value never holds a secret. The exit code is 0 only when ``ok`` is true.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

BOX_ROOT = Path("/opt/quiver")

# §15.3: the five required keys. ALPHA_VANTAGE_API_KEY is optional (a warning when empty).
REQUIRED_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "RH_ACCOUNT_NUMBER", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_ALLOWED_CHAT_IDS", "SEC_USER_AGENT")
# Values that a check result must never show.
SECRET_ENV = REQUIRED_ENV + ("ALPHA_VANTAGE_API_KEY", "TELEGRAM_ALERT_CHAT_IDS", "ANTHROPIC_API_KEY")

SDK_VERSION = "0.2.174"
# The broker hook's onFailure: "block" needs this CLI version or later (§11.3).
MIN_CLAUDE = (2, 1, 295)

CORE_MODULES = ("lib.sizing", "lib.portfolio", "lib.goal", "lib.universe", "lib.runlock",
                "lib.strategy")
THIRD_PARTY = ("claude_agent_sdk", "bs4", "lxml")
RUNNERS = ("run_monitor", "run_worker", "run_broker", "broker_guard", "chat_bridge")
RUNBOOKS = ("prompts/desk.md", "prompts/analyst.md", "prompts/news_classifier.md",
            "prompts/broker.md", "EXECUTE.md", "SNAPSHOT.md")

# §6.1: the new columns on the existing tables.
ORDERS_COLUMNS = ("proposal_id", "leg_seq", "quote_at_proposal", "clamped_from", "check_quote",
                  "checked_at", "reconcile_attempts", "skip_reason", "review_ok", "review_json",
                  "reviewed_at")
TARGET_PORTFOLIO_COLUMNS = ("entry_price_max",)
BROKER_JOBS_COLUMNS = ("recovery",)

# §11.3: the two hooks of deploy/runner/settings.json, and the canary hook.
GUARD = "/opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/broker_guard.py"
EXPECTED_SETTINGS_HOOKS = {
    "PreToolUse": [{"matcher": ".*", "hooks": [
        {"type": "command", "command": GUARD, "timeout": 30, "onFailure": "block"}]}],
    "PostToolUse": [{"matcher": "mcp__robinhood-trading__review_equity_order", "hooks": [
        {"type": "command", "command": GUARD + " --record-review", "timeout": 30,
         "onFailure": "block"}]}],
}
EXPECTED_CANARY_HOOKS = {
    "PreToolUse": [{"matcher": ".*", "hooks": [
        {"type": "command", "command": "/bin/false", "timeout": 30, "onFailure": "block"}]}],
}

# Live: the units that must be active, and the wall of each service (§15.1).
LIVE_UNITS = ("quiver-chat.service", "quiver-worker.path", "quiver-broker.path", "quiver.timer",
              "quiver-backup.timer", "quiver-claude-update.timer")
HIDE_BROKER_OAUTH = ("quiver-chat.service", "quiver-worker.service", "quiver.service")
CLAUDE_DESK = "/opt/quiver/state/claude-desk"
CLAUDE_BROKER = "/opt/quiver/state/claude-config"
# §15.3: the code holds these defaults. quiver.env sets one only to override it.
MODEL_DEFAULTS = {
    "QUIVER_DESK_MODEL": "claude-sonnet-5-5",
    "QUIVER_ANALYST_MODEL": "claude-opus-5-5",
    "QUIVER_CLASSIFIER_MODEL": "claude-haiku-5-5",
    "QUIVER_BROKER_MODEL": "claude-sonnet-5-5",
}
DESK_BASE_TOOLS = ("Read", "Grep", "Glob", "WebSearch")
EDGAR_PROBE = "https://data.sec.gov/submissions/CIK0000320193.json"


class CheckFailed(Exception):
    """A check found a fault. The message is the reason."""


def is_box() -> bool:
    """True on the box: ``/opt/quiver`` exists."""
    return BOX_ROOT.is_dir()


def _scrub(text: str, env: dict) -> str:
    """Replace each secret value in ``text`` with ``***``."""
    for key in SECRET_ENV:
        value = env.get(key) or ""
        if len(value) >= 4:
            text = text.replace(value, "***")
    return text


def _version_tuple(text: str):
    """The first ``N.N.N`` in ``text`` as a tuple of ints, or None."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _claude_env(env: dict, config_dir: str) -> dict:
    """The minimal env for a ``claude`` probe: no secret but the Claude token."""
    out = {k: env[k] for k in ("PATH", "HOME", "LANG", "CLAUDE_CODE_OAUTH_TOKEN") if env.get(k)}
    out.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    out["DISABLE_AUTOUPDATER"] = "1"
    out["CLAUDE_CONFIG_DIR"] = config_dir
    return out


def _resolve_claude_bin(env: dict):
    """``$CLAUDE_BIN``. Off the box an unset value falls back to ``claude`` on PATH."""
    path = env.get("CLAUDE_BIN") or ""
    if not path and not is_box():
        path = shutil.which("claude") or ""
    return path


# --- offline checks ----------------------------------------------------------------

def check_config(env: dict):
    from lib.config import load_config
    load_config(_REPO / "config.yaml")


def check_market(env: dict):
    import lib.market as market
    market.trading_day_et()
    # trading_day_et() alone does not load pandas_market_calendars. This call does.
    market.is_regular_session_open()


def _schema_faults(conn) -> list:
    """The §6.1 tables, the 12 FTS5 triggers and the new columns that ``conn`` lacks."""
    from lib.desk import store
    rows = conn.execute("SELECT name, type FROM sqlite_master").fetchall()
    tables = {n for n, t in rows if t == "table"}
    triggers = {n for n, t in rows if t == "trigger"}
    faults = [f"table {t}" for t in store.DESK_TABLES if t not in tables]
    want = {f"{table}_{op}" for table, _cols in store.FTS_TABLES.values()
            for op in ("ai", "ad", "au")}
    if len(want) != 12:
        faults.append(f"FTS_TABLES gives {len(want)} triggers, not 12")
    faults += [f"trigger {t}" for t in sorted(want - triggers)]
    for table, cols in (("orders", ORDERS_COLUMNS), ("target_portfolio", TARGET_PORTFOLIO_COLUMNS),
                        ("broker_jobs", BROKER_JOBS_COLUMNS)):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        faults += [f"column {table}.{c}" for c in cols if c not in have]
    return faults


def check_ledger(env: dict):
    from lib.ledger import Ledger
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "hc.db"
        Ledger(path)  # applies the full schema
        conn = sqlite3.connect(path)
        try:
            faults = _schema_faults(conn)
        finally:
            conn.close()
    if faults:
        raise CheckFailed("missing " + ", ".join(faults))


def check_fts5(env: dict):
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")


def _desk_modules() -> list:
    """Every module under ``lib/desk/``, found on disk at run time."""
    desk = _REPO / "lib" / "desk"
    names = ["lib.desk"]
    for p in sorted(desk.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        parts = p.relative_to(desk).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(("lib", "desk") + parts))
    return names


def _import_runner(name: str) -> None:
    """Import ``deploy/runner/<name>.py`` by path. Its ``__main__`` block does not run."""
    path = _REPO / "deploy" / "runner" / f"{name}.py"
    if not path.is_file():
        raise ImportError(f"{path.relative_to(_REPO)} does not exist")
    mod_name = f"deploy.runner.{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise


def check_imports(env: dict):
    faults = []
    for name in CORE_MODULES + tuple(_desk_modules()) + THIRD_PARTY:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001
            faults.append(f"{name}: {type(e).__name__}: {e}")
    for name in RUNNERS:
        try:
            _import_runner(name)
        except Exception as e:  # noqa: BLE001
            faults.append(f"deploy/runner/{name}.py: {type(e).__name__}: {e}")
    if faults:
        raise CheckFailed("; ".join(faults))


def check_sdk(env: dict):
    import claude_agent_sdk
    version = getattr(claude_agent_sdk, "__version__", None)
    if version != SDK_VERSION:
        raise CheckFailed(f"claude_agent_sdk is {version}, not {SDK_VERSION}")


def check_claude_bin(env: dict):
    path = _resolve_claude_bin(env)
    if not path:
        if is_box():
            raise CheckFailed("CLAUDE_BIN is not set")
        return "SKIP: not the box (no CLAUDE_BIN, no claude on PATH)"
    if not os.path.isabs(path):
        raise CheckFailed(f"CLAUDE_BIN {path} is not an absolute path")
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise CheckFailed(f"CLAUDE_BIN {path} is not an executable file")
    p = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL, env=_claude_env(env, CLAUDE_DESK))
    version = _version_tuple(p.stdout)
    if p.returncode != 0 or version is None:
        raise CheckFailed(f"{path} --version: exit {p.returncode}, no version")
    if version < MIN_CLAUDE:
        want = ".".join(map(str, MIN_CLAUDE))
        raise CheckFailed(f"{path} is {'.'.join(map(str, version))}; the broker hook needs {want} or later")
    if not env.get("CLAUDE_BIN"):
        return f"WARN: CLAUDE_BIN unset off the box; {path} is {'.'.join(map(str, version))}"
    return None


def guard_payloads() -> dict:
    """The two synthetic PreToolUse inputs. The guard must deny both."""
    base = {"session_id": "healthcheck", "transcript_path": "", "cwd": str(_REPO),
            "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse"}
    return {
        "place_equity_order": dict(base, tool_name="mcp__robinhood-trading__place_equity_order",
                                   tool_input={"ref_id": str(uuid.uuid4()), "symbol": "AAPL",
                                               "side": "buy", "type": "market",
                                               "dollar_amount": "1"}),
        "Read": dict(base, tool_name="Read", tool_input={"file_path": str(_REPO / "config.yaml")}),
    }


def check_guard_selftest(env: dict):
    guard = _REPO / "deploy" / "runner" / "broker_guard.py"
    if not guard.is_file():
        raise CheckFailed("deploy/runner/broker_guard.py does not exist")
    # No QUIVER_BROKER_JOB or QUIVER_BROKER_PROPOSAL: no job runs.
    genv = {k: env[k] for k in ("PATH", "HOME", "LANG") if env.get(k)}
    faults = []
    for label, payload in guard_payloads().items():
        try:
            p = subprocess.run([sys.executable, str(guard)], input=json.dumps(payload),
                               capture_output=True, text=True, timeout=30, env=genv, cwd=str(_REPO))
        except subprocess.TimeoutExpired:
            faults.append(f"{label}: no answer in 30 s")
            continue
        if p.returncode != 2:
            faults.append(f"{label}: exit {p.returncode} (a deny is exit 2)")
    if faults:
        raise CheckFailed("; ".join(faults))


def check_fts_integrity(env: dict):
    from lib.ledger import Ledger
    from lib.desk import store
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "fts.db"
        Ledger(path)
        conn = sqlite3.connect(path)
        try:
            now = store.now_iso()
            doc_id = store.store_document(conn, kind="news", title="healthcheck",
                                          text="healthcheck integrity row", now=now, ticker="AAPL")
            conn.execute(
                "INSERT INTO findings (ticker, kind, claim, quote, document_id, quote_start, "
                "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("AAPL", "other", "healthcheck claim", "healthcheck", doc_id, 0, "desk", now))
            conn.execute("INSERT INTO notes (ticker, text, tags, created_by, created_at) "
                         "VALUES (?,?,?,?,?)", ("AAPL", "healthcheck note", "hc", "desk", now))
            store.insert_owner_message(conn, chat_id="0", tg_message_id=1,
                                       text="healthcheck message", created_at=now)
            conn.commit()
            faults = []
            for fts, (table, _cols) in store.FTS_TABLES.items():
                if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != 1:
                    faults.append(f"{table}: no test row (add one to check_fts_integrity)")
                    continue
                try:
                    conn.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")
                except sqlite3.DatabaseError as e:
                    faults.append(f"{fts}: {e}")
        finally:
            conn.close()
    if faults:
        raise CheckFailed("; ".join(faults))


def check_env(env: dict):
    if not is_box():
        return "SKIP: not the box"
    empty = [k for k in REQUIRED_ENV if not (env.get(k) or "").strip()]
    if empty:
        raise CheckFailed("empty: " + ", ".join(empty))
    if env.get("CLAUDE_CONFIG_DIR") is not None:
        raise CheckFailed("CLAUDE_CONFIG_DIR is set; quiver.env must not set it (each unit sets it)")
    if not (env.get("ALPHA_VANTAGE_API_KEY") or "").strip():
        return "WARN: ALPHA_VANTAGE_API_KEY is empty (no transcripts)"
    return None


def check_dirs(env: dict):
    if not is_box():
        return "SKIP: not the box"
    import pwd
    faults = []
    for rel in ("state/triggers", "state/claude-desk", "state/claude-config"):
        path = BOX_ROOT / rel
        if not path.is_dir():
            faults.append(f"{rel} does not exist")
            continue
        st = path.stat()
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)
        mode = st.st_mode & 0o777
        if owner != "quiver":
            faults.append(f"{rel} owner {owner}, not quiver")
        if mode != 0o700:
            faults.append(f"{rel} mode {mode:04o}, not 0700")
    if faults:
        raise CheckFailed("; ".join(faults))


def _hooks_fault(path: Path, expected: dict):
    """A reason when ``path`` does not hold exactly ``expected`` as its hooks."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"{path.name}: {type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return f"{path.name}: not a JSON object"
    extra = set(data) - {"hooks", "_comment"}
    if extra:
        return f"{path.name}: unexpected keys {sorted(extra)}"
    if data.get("hooks") != expected:
        return f"{path.name}: hooks differ from plan §11.3"
    return None


def check_runbooks(env: dict):
    faults = [f"{rel} does not exist" for rel in RUNBOOKS if not (_REPO / rel).is_file()]
    runner = _REPO / "deploy" / "runner"
    for name, expected in (("settings.json", EXPECTED_SETTINGS_HOOKS),
                           ("canary_fail.json", EXPECTED_CANARY_HOOKS)):
        fault = _hooks_fault(runner / name, expected)
        if fault:
            faults.append(fault)
    if faults:
        raise CheckFailed("; ".join(faults))


OFFLINE = (
    ("config", check_config), ("market", check_market), ("ledger", check_ledger),
    ("fts5", check_fts5), ("imports", check_imports), ("sdk", check_sdk),
    ("claude_bin", check_claude_bin), ("guard_selftest", check_guard_selftest),
    ("fts_integrity", check_fts_integrity), ("env", check_env), ("dirs", check_dirs),
    ("runbooks", check_runbooks),
)


# --- live checks -------------------------------------------------------------------

def _systemctl(*args: str) -> str:
    p = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    return p.stdout.strip()


def check_units(env: dict):
    faults = []
    for unit in LIVE_UNITS:
        state = _systemctl("is-active", unit)
        if state != "active":
            faults.append(f"{unit} is {state or 'unknown'}")
    intel = _systemctl("list-unit-files", "quiver-intel*", "--no-legend", "--no-pager")
    if intel:
        faults.append("quiver-intel units still installed")
    if faults:
        raise CheckFailed("; ".join(faults))


def check_walls(env: dict):
    faults = []
    for unit in HIDE_BROKER_OAUTH + ("quiver-broker.service",):
        value = _systemctl("show", "-p", "InaccessiblePaths", "--value", unit)
        if "/etc/quiver" not in value:
            faults.append(f"{unit} does not hide /etc/quiver")
        hides_oauth = "claude-config" in value
        if unit in HIDE_BROKER_OAUTH and not hides_oauth:
            faults.append(f"{unit} does not hide state/claude-config")
        if unit == "quiver-broker.service" and hides_oauth:
            faults.append("quiver-broker.service hides state/claude-config")
    if faults:
        raise CheckFailed("; ".join(faults))


def check_ledger_live(env: dict):
    from lib.desk import store
    path = _REPO / "state" / "ledger.db"
    if not path.is_file():
        raise CheckFailed(f"{path} does not exist")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        faults = [f"table {t} missing" for t in store.DESK_TABLES if t not in tables]
        if not faults:
            if conn.execute("SELECT 1 FROM premise WHERE version=1").fetchone() is None:
                faults.append("no premise version 1 (run tick.py desk-seed)")
            for t in ("companies", "desk_settings", "sec_overrides"):
                if conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0:
                    faults.append(f"{t} is empty")
    finally:
        conn.close()
    if faults:
        raise CheckFailed("; ".join(faults))


def _claude_bin_or_fail(env: dict) -> str:
    path = _resolve_claude_bin(env)
    if not path:
        raise CheckFailed("CLAUDE_BIN is not set")
    return path


def check_desk_auth(env: dict):
    path = _claude_bin_or_fail(env)
    cenv = _claude_env(env, CLAUDE_DESK)
    faults, seen = [], {}
    for key, default in MODEL_DEFAULTS.items():
        model = env.get(key) or default
        if model not in seen:
            try:
                p = subprocess.run([path, "-p", "reply with exactly: AUTH_OK", "--model", model],
                                   capture_output=True, text=True, timeout=150,
                                   stdin=subprocess.DEVNULL, env=cenv)
                out = (p.stdout + p.stderr).strip()
                seen[model] = None if "AUTH_OK" in p.stdout else (out.splitlines() or [""])[-1][:120]
            except subprocess.TimeoutExpired:
                seen[model] = "no answer in 150 s"
        if seen[model] is not None:
            faults.append(f"{key}={model}: {seen[model]}")
    if faults:
        raise CheckFailed("; ".join(faults))


def check_desk_no_broker(env: dict):
    path = _claude_bin_or_fail(env)
    p = subprocess.run([path, "mcp", "list"], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL, env=_claude_env(env, CLAUDE_DESK))
    if p.returncode != 0:
        raise CheckFailed(f"claude mcp list under claude-desk: exit {p.returncode}")
    if "robinhood" in (p.stdout + p.stderr).lower():
        raise CheckFailed("state/claude-desk has a Robinhood MCP server")


def _make(agent, cls_name: str, loaders: tuple):
    """Build a ``lib.desk.agent`` object the way a runner does at start."""
    for name in loaders:
        fn = getattr(agent, name, None)
        if callable(fn):
            return fn()
    cls = getattr(agent, cls_name, None)
    fn = getattr(cls, "from_env", None)
    if callable(fn):
        return fn()
    raise CheckFailed(f"lib.desk.agent has no loader for {cls_name}")


def deny_all_tools(opts, root: str):
    """Make the desk options safe for a check: one turn, and a PreToolUse hook with an
    empty allowlist, so the model can call no tool (no ``say`` reaches the owner's chat)."""
    from claude_agent_sdk import HookMatcher
    from lib.desk import agent
    opts.hooks = {"PreToolUse": [HookMatcher(matcher=None, hooks=[agent.make_hook(frozenset(), root)])]}
    opts.max_turns = 1
    return opts


async def desk_init_tools(query_fn, opts):
    """The tool list of the system init message. The query runs to its end (an early stop
    leaves the CLI transport half closed); ``deny_all_tools`` keeps that one turn from
    calling any tool, and its text goes nowhere."""
    import claude_agent_sdk
    tools = None
    async for msg in query_fn(prompt="Do not call any tool. Reply with the word OK.", options=opts):
        if isinstance(msg, claude_agent_sdk.SystemMessage) and msg.subtype == "init":
            tools = list(msg.data.get("tools") or [])
    return tools


def check_desk_tools(env: dict):
    import asyncio
    import claude_agent_sdk
    from lib.desk import agent
    runtime = _make(agent, "Runtime", ("load_runtime",))
    secrets = _make(agent, "Secrets", ("load_secrets",))
    turn_state = agent.TurnState()
    opts = deny_all_tools(agent.desk_options(runtime, secrets, turn_state), runtime.root)
    # The SDK copies os.environ into the CLI. Scrub it, as each runner does at start.
    scrub = getattr(agent, "scrub_process_env", None)
    if callable(scrub):
        scrub()

    tools = asyncio.run(asyncio.wait_for(desk_init_tools(claude_agent_sdk.query, opts), timeout=180))
    if tools is None:
        raise CheckFailed("no system init message")
    have = set(tools)
    faults = [f"{t} missing" for t in DESK_BASE_TOOLS if t not in have]
    desk = {t for t in have if t.startswith("mcp__desk__")}
    if not desk:
        faults.append("no mcp__desk__ tool")
    want_desk = {t for t in (getattr(opts, "allowed_tools", None) or []) if t.startswith("mcp__desk__")}
    faults += [f"{t} missing" for t in sorted(want_desk - desk)]
    other = sorted(have - set(DESK_BASE_TOOLS) - desk)
    if other:
        faults.append("other tools: " + ", ".join(other))
    if faults:
        raise CheckFailed("; ".join(faults))


def check_robinhood(env: dict):
    path = _claude_bin_or_fail(env)
    p = subprocess.run([path, "mcp", "get", "robinhood-trading"], capture_output=True, text=True,
                       timeout=60, stdin=subprocess.DEVNULL, env=_claude_env(env, CLAUDE_BROKER))
    out = p.stdout + p.stderr
    if "Status:" not in out or "Connected" not in out.split("Status:", 1)[1]:
        raise CheckFailed("robinhood-trading is not connected: run ./deploy/gcp/rh-reauth.sh")


def check_edgar(env: dict):
    import gzip
    import urllib.request
    ua = env.get("SEC_USER_AGENT") or ""
    if not ua.strip():
        raise CheckFailed("SEC_USER_AGENT is empty")
    req = urllib.request.Request(EDGAR_PROBE, headers={"User-Agent": ua,
                                                       "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        status, body = resp.status, resp.read()
        encoding = resp.headers.get("Content-Encoding", "")
    if status != 200:
        raise CheckFailed(f"HTTP {status}")
    if encoding == "gzip" or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    data = json.loads(body)
    if not isinstance(data, dict) or "cik" not in data:
        raise CheckFailed("the reply has no cik")


def check_telegram(env: dict):
    import urllib.request
    token = env.get("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        raise CheckFailed("TELEGRAM_BOT_TOKEN is empty")
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=30) as resp:
        data = json.loads(resp.read())
    if data.get("ok") is not True:
        raise CheckFailed("getMe did not return ok: true")


def check_alphavantage(env: dict):
    # Key present only. A call counts against the 25 a day.
    if not (env.get("ALPHA_VANTAGE_API_KEY") or "").strip():
        return "WARN: ALPHA_VANTAGE_API_KEY is empty (no transcripts)"
    return None


# desk_tools runs last: it scrubs this process's environment for the SDK.
LIVE = (
    ("units", check_units), ("walls", check_walls), ("ledger_live", check_ledger_live),
    ("desk_auth", check_desk_auth), ("desk_no_broker", check_desk_no_broker),
    ("robinhood", check_robinhood), ("edgar", check_edgar), ("telegram", check_telegram),
    ("alphavantage", check_alphavantage), ("desk_tools", check_desk_tools),
)
LIVE_ORDER = ("units", "walls", "ledger_live", "desk_auth", "desk_no_broker", "desk_tools",
              "robinhood", "edgar", "telegram", "alphavantage")


def _run_one(fn, env: dict) -> str:
    try:
        result = fn(env)
    except CheckFailed as e:
        result = f"FAIL: {e}"
    except Exception as e:  # noqa: BLE001
        result = f"FAIL: {type(e).__name__}: {e}"
    if result is None:
        result = "ok"
    return _scrub(str(result), env)[:600]


def run_healthcheck(live: bool = False) -> dict:
    """Run the offline checks, and the live checks when ``live``. Return the result dict."""
    env = dict(os.environ)  # a module import or a scrub must not change what env sees
    checks: dict = {}
    # A module may print on import. stdout carries only the one JSON line.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            for name, fn in OFFLINE:
                checks[name] = _run_one(fn, env)
                if name == "imports":
                    os.environ.clear()
                    os.environ.update(env)
            if live:
                live_checks = {name: _run_one(fn, env) for name, fn in LIVE}
                checks.update((name, live_checks[name]) for name in LIVE_ORDER)
        finally:
            os.environ.clear()
            os.environ.update(env)
    ok = not any(v.startswith("FAIL") for v in checks.values())
    return {"ok": ok, "checks": checks}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    unknown = [a for a in argv if a != "--live"]
    if unknown:
        print(json.dumps({"ok": False, "checks": {"args": f"FAIL: unknown argument {unknown[0]}"}}))
        return 1
    result = run_healthcheck(live="--live" in argv)
    print(json.dumps(result), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
