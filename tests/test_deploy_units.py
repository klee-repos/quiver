"""The deploy files against plan §15.1-§15.5: units, hooks, scripts, terraform, env examples.

Plain asserts through ``Results``. No network, no systemd, no gcloud, no terraform. The
shell scripts are parsed with ``bash -n``, and two small blocks of them run in bash with
stubs (the quiver.env writer of setup.sh and the token reader of claude-update.sh).

    .venv/bin/python tests/test_deploy_units.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.desk_fakes import Results  # noqa: E402

DEPLOY = REPO / "deploy"
GCP = DEPLOY / "gcp"
RUNNER = DEPLOY / "runner"

r = Results("deploy-units")


# --- helpers ------------------------------------------------------------------------

def parse_unit(path: Path) -> dict:
    """A systemd unit file as {section: [(key, value), ...]}. Repeated keys are kept."""
    sections: dict = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is None:
            raise ValueError(f"{path.name}: a key before any section: {line}")
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"{path.name}: no '=' in {line}")
        sections[current].append((key.strip(), value.strip()))
    return sections


def values(section: list, key: str) -> list:
    return [v for k, v in section if k == key]


def one(section: list, key: str):
    vs = values(section, key)
    return vs[0] if len(vs) == 1 else vs


def paths(section: list, key: str) -> list:
    return [p for v in values(section, key) for p in v.split()]


def env_of(section: list) -> dict:
    out = {}
    for v in values(section, "Environment"):
        k, _, val = v.partition("=")
        out[k] = val
    return out


def bash_n(path: Path):
    p = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    return p.returncode, p.stderr.strip()


def code_lines(text: str) -> list:
    """The lines of a shell script that are not comments."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def units_list(script: str) -> list:
    m = re.search(r'^UNITS="([^"]*)"', script, re.M)
    return m.group(1).split() if m else []


# --- §15.1 the four desk services ---------------------------------------------------

STATE = "/opt/quiver/state"
LOGS = "/opt/quiver/logs"
VARLOG = "/var/log/quiver"
SPEC = {
    "quiver-chat.service": dict(
        type="simple", exec="/opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/chat_bridge.py",
        rw={STATE, LOGS}, hidden={f"{STATE}/claude-config", "/etc/quiver"}, mem="2048M",
        config_dir=f"{STATE}/claude-desk", claude=True, yfinance=True),
    "quiver-worker.service": dict(
        type="simple", exec="/opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/run_worker.py",
        rw={STATE, LOGS}, hidden={f"{STATE}/claude-config", "/etc/quiver"}, mem="1536M",
        config_dir=f"{STATE}/claude-desk", claude=True, yfinance=True),
    "quiver-broker.service": dict(
        type="simple", exec="/opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/run_broker.py",
        rw={STATE, LOGS, VARLOG}, hidden={f"{STATE}/claude-desk", "/etc/quiver"}, mem="1024M",
        config_dir=f"{STATE}/claude-config", claude=True, yfinance=False),
    "quiver.service": dict(
        type="oneshot", exec="/opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/run_monitor.py",
        rw={STATE, LOGS, VARLOG},
        hidden={f"{STATE}/claude-config", f"{STATE}/claude-desk", "/etc/quiver"}, mem="512M",
        config_dir=None, claude=False, yfinance=True),
}
HARDENING = {"NoNewPrivileges": "true", "PrivateTmp": "true", "ProtectSystem": "strict",
             "ProtectHome": "read-only", "ProtectKernelTunables": "true",
             "ProtectControlGroups": "true", "RestrictSUIDSGID": "true"}

parsed = {}
for name in list(SPEC) + ["quiver-worker.path", "quiver-broker.path", "quiver.timer",
                          "quiver-claude-update.service", "quiver-claude-update.timer",
                          "quiver-backup.service", "quiver-backup.timer"]:
    path = DEPLOY / name
    r.ok(f"{name} exists", path.is_file())
    if path.is_file():
        try:
            parsed[name] = parse_unit(path)
            r.ok(f"{name} parses", True)
        except ValueError as e:
            r.ok(f"{name} parses", False, str(e))
        text = path.read_text(encoding="utf-8")
        r.ok(f"{name}: no line continuation", not any(ln.rstrip().endswith("\\")
                                                       for ln in text.splitlines()))
        r.ok(f"{name}: no '# deploy/' header line", "# deploy/" not in text)

for name, spec in SPEC.items():
    unit = parsed.get(name)
    if unit is None:
        continue
    svc = unit.get("Service", [])
    r.eq(f"{name}: Type", one(svc, "Type"), spec["type"])
    r.eq(f"{name}: User", one(svc, "User"), "quiver")
    r.eq(f"{name}: Group", one(svc, "Group"), "quiver")
    r.eq(f"{name}: WorkingDirectory", one(svc, "WorkingDirectory"), "/opt/quiver")
    r.eq(f"{name}: EnvironmentFile (no '-')", values(svc, "EnvironmentFile"), ["/etc/quiver/quiver.env"])
    r.eq(f"{name}: ExecStart", one(svc, "ExecStart"), spec["exec"])
    rw, hidden = paths(svc, "ReadWritePaths"), paths(svc, "InaccessiblePaths")
    r.eq(f"{name}: ReadWritePaths", set(rw), spec["rw"])
    r.eq(f"{name}: InaccessiblePaths", set(hidden), spec["hidden"])
    r.ok(f"{name}: no '-' or '+' prefixed path", not [p for p in rw + hidden if p[:1] in "-+"],
         rw + hidden)
    r.eq(f"{name}: MemoryMax", one(svc, "MemoryMax"), spec["mem"])
    for key, want in HARDENING.items():
        r.eq(f"{name}: {key}", one(svc, key), want)
    env = env_of(svc)
    r.eq(f"{name}: CLAUDE_CONFIG_DIR", env.get("CLAUDE_CONFIG_DIR"), spec["config_dir"])
    r.eq(f"{name}: DISABLE_AUTOUPDATER where Claude runs", env.get("DISABLE_AUTOUPDATER"),
         "1" if spec["claude"] else None)
    r.eq(f"{name}: XDG_CACHE_HOME where yfinance runs", env.get("XDG_CACHE_HOME"),
         f"{STATE}/.cache" if spec["yfinance"] else None)
    r.ok(f"{name}: no npm_config_cache, no chat log", "npm_config_cache" not in env
         and "QUIVER_CHAT_LOG" not in env, env)
    # Rule 2: only the monitor and the broker write /var/log/quiver (the tick.log the alerts read).
    r.eq(f"{name}: QUIVER_TICK_LOG", env.get("QUIVER_TICK_LOG"),
         f"{VARLOG}/tick.log" if VARLOG in spec["rw"] else None)
    # A hidden path under state/ nests inside a writable path (systemd applies the deeper one).
    for p in hidden:
        if p.startswith(STATE):
            r.ok(f"{name}: {p} nests in a ReadWritePaths entry",
                 any(p.startswith(w + "/") for w in rw))

writers = sorted(n for n, u in parsed.items() if n in SPEC
                 and VARLOG in paths(u.get("Service", []), "ReadWritePaths"))
r.eq("only quiver.service and quiver-broker.service write /var/log/quiver", writers,
     ["quiver-broker.service", "quiver.service"])

if "quiver-chat.service" in parsed:
    chat = parsed["quiver-chat.service"]
    r.eq("chat: Restart=on-failure", one(chat["Service"], "Restart"), "on-failure")
    r.eq("chat: TimeoutStopSec", one(chat["Service"], "TimeoutStopSec"), "30")
    r.eq("chat: WantedBy multi-user.target", values(chat.get("Install", []), "WantedBy"),
         ["multi-user.target"])
for name, limit in (("quiver-worker.service", "28800"), ("quiver-broker.service", "3600")):
    if name in parsed:
        svc = parsed[name]["Service"]
        r.eq(f"{name}: RuntimeMaxSec", one(svc, "RuntimeMaxSec"), limit)
        r.eq(f"{name}: Restart=no", one(svc, "Restart"), "no")
        r.ok(f"{name}: no [Install] (only its path unit starts it)", "Install" not in parsed[name])
if "quiver.service" in parsed:
    r.eq("monitor: TimeoutStartSec", one(parsed["quiver.service"]["Service"], "TimeoutStartSec"), "2400")
    r.ok("monitor: no [Install] (the timer starts it)", "Install" not in parsed["quiver.service"])

# --- §15.1 path units and the timer -------------------------------------------------

for name, trigger, service in (("quiver-worker.path", "worker", "quiver-worker.service"),
                               ("quiver-broker.path", "broker", "quiver-broker.service")):
    unit = parsed.get(name)
    if unit is None:
        continue
    r.eq(f"{name}: PathExists", values(unit.get("Path", []), "PathExists"),
         [f"{STATE}/triggers/{trigger}"])
    r.ok(f"{name}: no PathChanged (it loses a trigger)", not values(unit.get("Path", []), "PathChanged"))
    r.eq(f"{name}: Unit", one(unit.get("Path", []), "Unit"), service)
    r.eq(f"{name}: WantedBy paths.target", values(unit.get("Install", []), "WantedBy"), ["paths.target"])

if "quiver.timer" in parsed:
    timer = parsed["quiver.timer"]["Timer"]
    r.eq("quiver.timer: OnCalendar", values(timer, "OnCalendar"), ["Mon..Fri *-*-* 11..23:40:00 UTC"])
    r.eq("quiver.timer: Persistent", one(timer, "Persistent"), "false")
    r.eq("quiver.timer: AccuracySec", one(timer, "AccuracySec"), "1min")
    r.eq("quiver.timer: WantedBy", values(parsed["quiver.timer"].get("Install", []), "WantedBy"),
         ["timers.target"])

if "quiver-claude-update.service" in parsed:
    cu = parsed["quiver-claude-update.service"]
    after = {p for v in values(cu["Unit"], "After") for p in v.split()}
    r.ok("claude-update: After= the monitor, the worker and the broker",
         {"quiver.service", "quiver-worker.service", "quiver-broker.service"} <= after, after)
    r.ok("claude-update: no Conflicts= anywhere",
         not any(values(sec, "Conflicts") for sec in cu.values()))
    r.eq("claude-update: ExecStart", one(cu["Service"], "ExecStart"), "/usr/local/bin/quiver-claude-update")

r.ok("the intel units are gone", not (DEPLOY / "quiver-intel.service").exists()
     and not (DEPLOY / "quiver-intel.timer").exists())

# --- §11.3 settings.json and canary_fail.json ---------------------------------------

GUARD = "/opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/broker_guard.py"
WANT_SETTINGS = {
    "PreToolUse": [{"matcher": ".*", "hooks": [
        {"type": "command", "command": GUARD, "timeout": 30, "onFailure": "block"}]}],
    "PostToolUse": [{"matcher": "mcp__robinhood-trading__review_equity_order", "hooks": [
        {"type": "command", "command": GUARD + " --record-review", "timeout": 30,
         "onFailure": "block"}]}],
}
WANT_CANARY = {"PreToolUse": [{"matcher": ".*", "hooks": [
    {"type": "command", "command": "/bin/false", "timeout": 30, "onFailure": "block"}]}]}

for fname, want in (("settings.json", WANT_SETTINGS), ("canary_fail.json", WANT_CANARY)):
    path = RUNNER / fname
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        r.ok(f"{fname} is valid JSON", False, str(e))
        continue
    r.ok(f"{fname}: top-level keys are hooks (+ _comment)", set(data) - {"_comment"} == {"hooks"},
         sorted(data))
    r.eq(f"{fname}: exactly the plan hooks", data.get("hooks"), want)
    cmds = [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
    r.ok(f"{fname}: every command is an absolute path", all(c.startswith("/") for c in cmds), cmds)

# --- the healthcheck pure parts -------------------------------------------------------

import deploy.runner.healthcheck as hc  # noqa: E402

r.eq("healthcheck: settings hooks match the plan", hc.EXPECTED_SETTINGS_HOOKS, WANT_SETTINGS)
r.eq("healthcheck: canary hooks match the plan", hc.EXPECTED_CANARY_HOOKS, WANT_CANARY)
r.eq("healthcheck: committed settings.json passes", hc._hooks_fault(RUNNER / "settings.json",
                                                                    hc.EXPECTED_SETTINGS_HOOKS), None)
r.eq("healthcheck: committed canary_fail.json passes", hc._hooks_fault(RUNNER / "canary_fail.json",
                                                                       hc.EXPECTED_CANARY_HOOKS), None)
with tempfile.TemporaryDirectory() as d:
    extra_hook = json.loads((RUNNER / "settings.json").read_text())
    extra_hook["hooks"]["Stop"] = [{"matcher": ".*", "hooks": []}]
    (Path(d) / "a.json").write_text(json.dumps(extra_hook))
    r.ok("healthcheck: a third hook fails", hc._hooks_fault(Path(d) / "a.json",
                                                           hc.EXPECTED_SETTINGS_HOOKS) is not None)
    soft = json.loads((RUNNER / "settings.json").read_text())
    soft["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"] = "allow"
    (Path(d) / "b.json").write_text(json.dumps(soft))
    r.ok("healthcheck: onFailure other than block fails",
         hc._hooks_fault(Path(d) / "b.json", hc.EXPECTED_SETTINGS_HOOKS) is not None)
    top = json.loads((RUNNER / "canary_fail.json").read_text())
    top["permissions"] = {}
    (Path(d) / "c.json").write_text(json.dumps(top))
    r.ok("healthcheck: an extra top-level key fails",
         hc._hooks_fault(Path(d) / "c.json", hc.EXPECTED_CANARY_HOOKS) is not None)

r.eq("healthcheck: core modules", hc.CORE_MODULES,
     ("lib.sizing", "lib.portfolio", "lib.goal", "lib.universe", "lib.runlock", "lib.strategy"))
r.eq("healthcheck: runners", set(hc.RUNNERS),
     {"run_monitor", "run_worker", "run_broker", "broker_guard", "chat_bridge"})
r.eq("healthcheck: third-party modules", set(hc.THIRD_PARTY), {"claude_agent_sdk", "bs4", "lxml"})
on_disk = {"lib.desk." + p.stem for p in (REPO / "lib" / "desk").glob("*.py") if p.stem != "__init__"}
r.ok("healthcheck: every lib/desk module is imported", on_disk <= set(hc._desk_modules()),
     sorted(on_disk - set(hc._desk_modules())))
r.eq("healthcheck: the required env keys", set(hc.REQUIRED_ENV),
     {"CLAUDE_CODE_OAUTH_TOKEN", "RH_ACCOUNT_NUMBER", "TELEGRAM_BOT_TOKEN",
      "TELEGRAM_ALLOWED_CHAT_IDS", "SEC_USER_AGENT"})
r.eq("healthcheck: offline check names", [n for n, _ in hc.OFFLINE],
     ["config", "market", "ledger", "fts5", "imports", "sdk", "claude_bin", "guard_selftest",
      "fts_integrity", "env", "dirs", "runbooks"])
r.eq("healthcheck: live check names", list(hc.LIVE_ORDER),
     ["units", "walls", "ledger_live", "desk_auth", "desk_no_broker", "desk_tools", "robinhood",
      "edgar", "telegram", "alphavantage"])
r.eq("healthcheck: every live check has a function", set(hc.LIVE_ORDER), {n for n, _ in hc.LIVE})
r.eq("healthcheck: desk_tools runs last (it scrubs os.environ)", hc.LIVE[-1][0], "desk_tools")

# desk_tools ran a whole desk turn with live tools: the model's say("OK") reached the owner's
# chat (2026-10-29). Now the check denies every tool and allows one turn.
import asyncio as _aio  # noqa: E402
import claude_agent_sdk as _sdk  # noqa: E402
_opts = hc.deny_all_tools(_sdk.ClaudeAgentOptions(max_turns=40), str(REPO))
_hook = _opts.hooks["PreToolUse"][0].hooks[0]
_dec = _aio.run(_hook({"tool_name": "mcp__desk__say", "tool_input": {"text": "OK"}}, "t1", None))
r.eq("healthcheck: desk_tools denies the say tool", _dec["hookSpecificOutput"]["permissionDecision"], "deny")
_dec = _aio.run(_hook({"tool_name": "Read", "tool_input": {"file_path": str(REPO / "README.md")}}, "t2", None))
r.eq("healthcheck: desk_tools denies a built-in tool too", _dec["hookSpecificOutput"]["permissionDecision"], "deny")
r.eq("healthcheck: desk_tools runs one turn at most", _opts.max_turns, 1)
_seen = []


async def _fake_query(prompt, options):
    _seen.append("init")
    yield _sdk.SystemMessage(subtype="init", data={"tools": ["Read", "mcp__desk__say"]})
    _seen.append("past init")
    yield _sdk.SystemMessage(subtype="other", data={})

r.eq("healthcheck: desk_init_tools returns the init tool list",
     _aio.run(hc.desk_init_tools(_fake_query, _opts)), ["Read", "mcp__desk__say"])
r.eq("healthcheck: desk_init_tools drains the query to its end", _seen, ["init", "past init"])
r.eq("healthcheck: version parse", hc._version_tuple("2.1.312 (Claude Code)"), (2, 1, 312))
r.ok("healthcheck: 2.1.294 is below the minimum", hc._version_tuple("2.1.294") < hc.MIN_CLAUDE)

payloads = hc.guard_payloads()
r.eq("guard payloads: two cases", sorted(payloads), ["Read", "place_equity_order"])
place = payloads["place_equity_order"]
r.eq("guard payload: the place tool name", place["tool_name"],
     "mcp__robinhood-trading__place_equity_order")
r.ok("guard payload: a fresh ref_id", re.fullmatch(r"[0-9a-f-]{36}", place["tool_input"]["ref_id"]))
r.ok("guard payloads: a new ref_id each call",
     hc.guard_payloads()["place_equity_order"]["tool_input"]["ref_id"] != place["tool_input"]["ref_id"])

secret = "sk-ant-oat01-SECRETVALUE"


def _leaky(env):
    raise hc.CheckFailed(f"token {secret} refused")


got = hc._run_one(_leaky, {"CLAUDE_CODE_OAUTH_TOKEN": secret})
r.ok("healthcheck: a FAIL value never holds a secret", secret not in got and got.startswith("FAIL"), got)
r.eq("healthcheck: None is ok", hc._run_one(lambda env: None, {}), "ok")
r.eq("healthcheck: a WARN passes through", hc._run_one(lambda env: "WARN: x", {}), "WARN: x")


def _boom(env):
    raise RuntimeError("boom")


r.eq("healthcheck: an exception is a FAIL", hc._run_one(_boom, {}), "FAIL: RuntimeError: boom")

# env and dirs: SKIP off the box; strict on a box root.
saved_root = hc.BOX_ROOT
try:
    with tempfile.TemporaryDirectory() as d:
        hc.BOX_ROOT = Path(d) / "absent"
        r.eq("env: SKIP off the box", hc._run_one(hc.check_env, {}), "SKIP: not the box")
        r.eq("dirs: SKIP off the box", hc._run_one(hc.check_dirs, {}), "SKIP: not the box")
        hc.BOX_ROOT = Path(d)
        full = {k: "v" + k for k in hc.REQUIRED_ENV}
        r.ok("env: an empty required key fails",
             hc._run_one(hc.check_env, dict(full, SEC_USER_AGENT="")).startswith("FAIL: empty: SEC_USER_AGENT"))
        r.ok("env: CLAUDE_CONFIG_DIR set fails",
             hc._run_one(hc.check_env, dict(full, CLAUDE_CONFIG_DIR="/x")).startswith("FAIL"))
        r.ok("env: an empty ALPHA_VANTAGE_API_KEY warns",
             hc._run_one(hc.check_env, full).startswith("WARN"))
        r.eq("env: the full set passes", hc._run_one(hc.check_env, dict(full, ALPHA_VANTAGE_API_KEY="k")), "ok")
        for rel in ("state/triggers", "state/claude-desk", "state/claude-config"):
            (Path(d) / rel).mkdir(parents=True)
            os.chmod(Path(d) / rel, 0o755)
        got = hc._run_one(hc.check_dirs, {})
        r.ok("dirs: mode 0755 fails", got.startswith("FAIL") and "not 0700" in got, got)
        os.chmod(Path(d) / "state/claude-desk", 0o700)
        got = hc._run_one(hc.check_dirs, {})
        r.ok("dirs: a 0700 dir has no mode fault; a 0755 dir has one",
             "state/claude-desk mode" not in got and "state/triggers mode 0755" in got, got)
        r.ok("dirs: an owner other than quiver fails", "owner" in got and "not quiver" in got, got)
finally:
    hc.BOX_ROOT = saved_root

# guard_selftest: exit 2 on both inputs passes; any other exit fails.
saved_repo = hc._REPO
try:
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "deploy" / "runner").mkdir(parents=True)
        fake = Path(d) / "deploy" / "runner" / "broker_guard.py"
        hc._REPO = Path(d)
        fake.write_text("import sys, json\njson.load(sys.stdin)\nsys.exit(2)\n")
        r.eq("guard_selftest: deny on both passes", hc._run_one(hc.check_guard_selftest, dict(os.environ)), "ok")
        fake.write_text("import sys, json\nd = json.load(sys.stdin)\n"
                        "sys.exit(0 if d['tool_name'] == 'Read' else 2)\n")
        got = hc._run_one(hc.check_guard_selftest, dict(os.environ))
        r.ok("guard_selftest: an allowed Read fails", got.startswith("FAIL: Read: exit 0"), got)
        fake.write_text("raise SystemExit(1)\n")
        got = hc._run_one(hc.check_guard_selftest, dict(os.environ))
        r.ok("guard_selftest: a crash (exit 1) fails", got.startswith("FAIL") and "exit 1" in got, got)
finally:
    hc._REPO = saved_repo

# Live checks with a fake systemctl (no systemd here).
saved_sc = hc._systemctl
try:
    state = {"active": set(hc.LIVE_UNITS), "intel": "", "show": {}}

    def fake_systemctl(*args):
        if args[0] == "is-active":
            return "active" if args[1] in state["active"] else "inactive"
        if args[0] == "list-unit-files":
            return state["intel"]
        if args[0] == "show":
            return state["show"].get(args[-1], "")
        return ""

    hc._systemctl = fake_systemctl
    r.eq("units: all active, no intel unit", hc._run_one(hc.check_units, {}), "ok")
    state["active"] = set(hc.LIVE_UNITS) - {"quiver-broker.path"}
    r.ok("units: a stopped broker path fails",
         "quiver-broker.path is inactive" in hc._run_one(hc.check_units, {}))
    state["active"], state["intel"] = set(hc.LIVE_UNITS), "quiver-intel.timer disabled enabled"
    r.ok("units: an installed intel unit fails", hc._run_one(hc.check_units, {}).startswith("FAIL"))
    good = {u: "/opt/quiver/state/claude-config /etc/quiver" for u in hc.HIDE_BROKER_OAUTH}
    good["quiver-broker.service"] = "/opt/quiver/state/claude-desk /etc/quiver"
    state["show"] = good
    r.eq("walls: the §15.1 walls pass", hc._run_one(hc.check_walls, {}), "ok")
    state["show"] = dict(good, **{"quiver-broker.service": "/opt/quiver/state/claude-config /etc/quiver"})
    r.ok("walls: a broker that hides the OAuth fails",
         "quiver-broker.service hides" in hc._run_one(hc.check_walls, {}))
    state["show"] = dict(good, **{"quiver-chat.service": "/etc/quiver"})
    r.ok("walls: a chat that sees the OAuth fails",
         "quiver-chat.service does not hide state/claude-config" in hc._run_one(hc.check_walls, {}))
finally:
    hc._systemctl = saved_sc

saved_repo = hc._REPO
try:
    with tempfile.TemporaryDirectory() as d:
        hc._REPO = Path(d)
        r.ok("ledger_live: no ledger fails", hc._run_one(hc.check_ledger_live, {}).startswith("FAIL"))
        (Path(d) / "state").mkdir()
        from lib.ledger import Ledger
        Ledger(Path(d) / "state" / "ledger.db")
        got = hc._run_one(hc.check_ledger_live, {})
        r.ok("ledger_live: an unseeded ledger fails (no premise v1, empty tables)",
             "no premise version 1" in got, got)
finally:
    hc._REPO = saved_repo
r.eq("alphavantage: key present passes", hc._run_one(hc.check_alphavantage, {"ALPHA_VANTAGE_API_KEY": "k"}), "ok")
r.ok("alphavantage: no key warns", hc._run_one(hc.check_alphavantage, {}).startswith("WARN"))
r.ok("telegram: no token fails without a call", hc._run_one(hc.check_telegram, {}).startswith("FAIL"))
r.ok("edgar: no User-Agent fails without a call", hc._run_one(hc.check_edgar, {}).startswith("FAIL"))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = hc.main(["--bogus"])
lines = buf.getvalue().splitlines()
r.eq("healthcheck main: an unknown flag exits 1", rc, 1)
r.ok("healthcheck main: one JSON line", len(lines) == 1 and json.loads(lines[0])["ok"] is False, lines)

# --- backup ---------------------------------------------------------------------------

import deploy.runner.backup_ledger as backup  # noqa: E402

r.ok("backup: counts proposals and documents", {"proposals", "documents"} <= set(backup._COUNT_TABLES),
     backup._COUNT_TABLES)
r.ok("backup: no ticker_action", "ticker_action" not in backup._COUNT_TABLES)

# --- §15.2 shell scripts --------------------------------------------------------------

SCRIPTS = (GCP / "setup.sh", GCP / "update.sh", GCP / "bootstrap-gcp.sh", DEPLOY / "claude-update.sh")
for path in SCRIPTS:
    rc, err = bash_n(path)
    r.eq(f"bash -n {path.relative_to(REPO)}", (rc, err), (0, ""))

ALL_UNITS = ["quiver.service", "quiver.timer", "quiver-chat.service", "quiver-worker.path",
             "quiver-worker.service", "quiver-broker.path", "quiver-broker.service",
             "quiver-claude-update.service", "quiver-claude-update.timer", "quiver-backup.service",
             "quiver-backup.timer"]

# setup.sh
setup = (GCP / "setup.sh").read_text(encoding="utf-8")
setup_code = "\n".join(code_lines(setup))
r.eq("setup.sh: UNITS is the eleven units", units_list(setup), ALL_UNITS)
r.ok("setup.sh: every unit in UNITS is a file", all((DEPLOY / u).is_file() for u in units_list(setup)))
m = re.search(r'^REQUIRED_KEYS="([^"]*)"', setup, re.M)
r.eq("setup.sh: required keys", m.group(1).split() if m else None,
     ["RH_ACCOUNT_NUMBER", "CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN",
      "TELEGRAM_ALLOWED_CHAT_IDS", "SEC_USER_AGENT"])
m = re.search(r'^OPTIONAL_KEYS="([^"]*)"', setup, re.M)
r.eq("setup.sh: optional keys", m.group(1).split() if m else None,
     ["ALPHA_VANTAGE_API_KEY", "TELEGRAM_ALERT_CHAT_IDS"])
for word in ("quiver_eve", "mcp.json", "state/chat", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
             "GLM_API_KEY", "DEEPSEEK_API_KEY", "RESEND", "NOTIFY_", "CONGRESS_API_KEY",
             "quiver-intel", "npm ci"):
    r.ok(f"setup.sh code: no {word}", word not in setup_code)
enable = re.search(r"systemctl enable --now ([^\n]*)", setup.replace("\\\n", " "))
enabled = enable.group(1).split() if enable else []
r.eq("setup.sh: enable --now the six units", set(enabled),
     {"quiver.timer", "quiver-worker.path", "quiver-broker.path", "quiver-claude-update.timer",
      "quiver-backup.timer", "quiver-chat.service"})
r.ok("setup.sh: the [1b/9] comment says 4 GiB", "4 GiB of RAM" in setup)
r.ok("setup.sh: the note names tick.py desk-seed after rh-reauth.sh",
     setup.find("rh-reauth.sh", setup.find("cat <<'NOTE'")) < setup.find("desk-seed", setup.find("cat <<'NOTE'")))
for d in ("state/claude-config", "state/claude-desk", "state/triggers", "state/tmp", "state/.cache",
          "state/.npm-cache", "logs/desk"):
    r.ok(f"setup.sh: creates {d} mode 700", re.search(rf"for _d in [^\n]*\b{re.escape(d)}\b", setup)
         and "install -d -m 700" in setup)

# The quiver.env writer, run in bash with stubs: KEY="value" lines, never CLAUDE_CONFIG_DIR.
start = setup.find("REQUIRED_KEYS=")
end_marker = "} > /etc/quiver/quiver.env"
end = setup.find(end_marker)
block = setup[start:end + len(end_marker)] if start >= 0 and end > start else ""
r.ok("setup.sh: one quiver.env writer block", block and setup.count(end_marker) == 1)
r.ok("setup.sh: the writer block never writes CLAUDE_CONFIG_DIR",
     "CLAUDE_CONFIG_DIR" not in block[block.find("{\n"):] if block else False)
writes_env = [ln for ln in code_lines(setup) if not ln.strip().startswith("echo ")
              and re.search(r">>?\s*\S*quiver\.env", ln)]
r.ok("setup.sh: no other line writes quiver.env", writes_env == [end_marker], writes_env)


def run_writer(values_by_key: dict):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "quiver.env"
        stub = ("set -euo pipefail\nQUIVER_HOME=/opt/quiver; QUIVER_USER=quiver; SECRET_PREFIX=quiver-\n"
                "install() { :; }\n"
                "fetch() { case \"$1\" in " + " ".join(
                    f"{k}) printf '%s' {json.dumps(v)} ;;" for k, v in values_by_key.items()) +
                " *) return 1 ;; esac; }\n"
                "fetch_opt() { fetch \"$1\" 2>/dev/null || echo \"\"; }\n")
        script = stub + block.replace("/etc/quiver/quiver.env", str(out))
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        text = out.read_text() if out.exists() else ""
        return p.returncode, p.stderr, text


vals = {"RH_ACCOUNT_NUMBER": "123", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x",
        "TELEGRAM_BOT_TOKEN": "1:abc", "TELEGRAM_ALLOWED_CHAT_IDS": "42",
        "SEC_USER_AGENT": "Quiver owner@example.com", "ALPHA_VANTAGE_API_KEY": "AV1"}
rc, err, text = run_writer(vals)
r.eq("setup.sh writer: exit 0", (rc, err), (0, ""))
env_lines = text.splitlines()
r.ok("setup.sh writer: every line is KEY=\"value\"",
     env_lines and all(re.fullmatch(r'[A-Z_]+="[^"]*"', ln) for ln in env_lines), env_lines)
keys = [ln.split("=", 1)[0] for ln in env_lines]
r.eq("setup.sh writer: the keys", keys,
     ["RH_ACCOUNT_NUMBER", "CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN",
      "TELEGRAM_ALLOWED_CHAT_IDS", "SEC_USER_AGENT", "ALPHA_VANTAGE_API_KEY",
      "TELEGRAM_ALERT_CHAT_IDS", "CLAUDE_BIN"])
r.ok("setup.sh writer: no CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR" not in text)
r.ok("setup.sh writer: SEC_USER_AGENT quoted", 'SEC_USER_AGENT="Quiver owner@example.com"' in env_lines)
r.ok("setup.sh writer: an absent optional key is empty", 'TELEGRAM_ALERT_CHAT_IDS=""' in env_lines)
r.ok("setup.sh writer: CLAUDE_BIN pinned", 'CLAUDE_BIN="/opt/quiver/npm-global/bin/claude"' in env_lines)
p = subprocess.run(["bash", "-c", "set -euo pipefail; set -a; . /dev/stdin; set +a; "
                    "printf '%s|%s' \"$SEC_USER_AGENT\" \"$CLAUDE_BIN\""],
                   input=text, capture_output=True, text=True)
r.eq("setup.sh writer: bash reads the file back", p.stdout,
     "Quiver owner@example.com|/opt/quiver/npm-global/bin/claude")
rc, err, _ = run_writer(dict(vals, SEC_USER_AGENT=""))
r.ok("setup.sh writer: an empty required key stops", rc != 0 and "FATAL" in err, err)
rc, err, _ = run_writer(dict(vals, TELEGRAM_BOT_TOKEN="1:a$b"))
r.ok("setup.sh writer: a $ in a value stops", rc != 0 and "FATAL" in err, err)
rc, err, _ = run_writer({k: v for k, v in vals.items() if k != "RH_ACCOUNT_NUMBER"})
r.ok("setup.sh writer: a missing required secret stops", rc != 0 and "FATAL" in err, err)

# update.sh
update = (GCP / "update.sh").read_text(encoding="utf-8")
ucode = "\n".join(code_lines(update))


def at(text: str, needle: str) -> int:
    i = text.find(needle)
    return i if i >= 0 else 10 ** 9


r.ok("update.sh: the deploy lock", "exec 9>/run/quiver-deploy.lock" in ucode and re.search(r"flock\b[^\n]* 9\b", ucode))
r.ok("update.sh: the lock comes before the fetch", at(ucode, "flock") < at(ucode, "git_q fetch"))
r.ok("update.sh: the broker pause comes after the up-to-date check",
     at(ucode, "Already up to date") < at(ucode, 'echo "[0/6] pause the broker"'))
r.ok("update.sh: the broker pause comes before [2/6] and the pull",
     at(ucode, "systemctl stop quiver-broker.path") < at(ucode, 'echo "[2/6]')
     < at(ucode, "merge --ff-only"))
r.ok("update.sh: wait while the broker runs",
     "while systemctl is-active --quiet quiver-broker.service; do sleep 5; done" in ucode)
r.ok("update.sh: the paused-broker EXIT trap", re.search(r"^trap '.*broker paused.*' EXIT$", ucode, re.M))
upto = ucode[at(ucode, "Already up to date"):at(ucode, 'echo "[0/6]')]
r.ok("update.sh: up to date + broker path stopped -> healthcheck, then start or exit 1",
     "healthcheck.py" in upto and "broker path started" in upto
     and "broker paused: healthcheck failed" in upto and "exit 1" in upto)
r.ok("update.sh: the broker path starts only after the [6/6] healthcheck",
     at(ucode, 'echo "[6/6]') < at(ucode, "\nsystemctl start quiver-broker.path"))
r.ok("update.sh: the chat restart comes after the healthcheck",
     at(ucode, 'echo "[6/6]') < at(ucode, "systemctl restart quiver-chat.service"))
r.ok("update.sh: the chat restart is a deny-list (docs/, tests/)", "grep -qvE '^(docs/|tests/)'" in ucode)
r.ok("update.sh: never restarts quiver-broker.service", "restart quiver-broker.service" not in ucode)
r.eq("update.sh: UNITS matches setup.sh", units_list(update), ALL_UNITS)
m = re.search(r"grep -qE '(\^deploy/\(quiver[^']*)' <<<\"\$CHANGED\"", update)
unit_re = re.compile(m.group(1)) if m else None
r.ok("update.sh: the unit regex matches every unit file and claude-update.sh",
     unit_re is not None and all(unit_re.match(f"deploy/{u}") for u in ALL_UNITS + ["claude-update.sh"]))
r.ok("update.sh: the unit regex skips other deploy files",
     unit_re is not None and not unit_re.match("deploy/runner/settings.json"))
for d in ("state/claude-config", "state/claude-desk", "state/triggers", "state/tmp", "state/.cache", "logs/desk"):
    r.ok(f"update.sh: creates {d}", re.search(rf"for d in [^\n]*\b{re.escape(d)}\b", update))
r.ok("update.sh: removes the intel units", "for u in quiver-intel.timer quiver-intel.service" in ucode)
r.ok("update.sh: removes an untracked quiver_eve/", 'rm -rf "$QUIVER_HOME/quiver_eve"' in ucode)
r.ok("update.sh: no npm ci", "npm ci" not in ucode)

# claude-update.sh
cu = (DEPLOY / "claude-update.sh").read_text(encoding="utf-8")
cucode = "\n".join(code_lines(cu))
traps = [ln for ln in code_lines(cu) if re.match(r"\s*trap\b", ln)]
r.eq("claude-update.sh: one trap statement", len(traps), 1)
r.ok("claude-update.sh: the trap keeps clean_caches and restarts the broker path",
     traps and traps[0].strip().endswith("EXIT") and "clean_caches" in traps[0]
     and "systemctl start quiver-broker.path" in traps[0], traps)
r.ok("claude-update.sh: the deploy lock (non-blocking)",
     "exec 9>/run/quiver-deploy.lock; flock -n 9" in cucode)
r.ok("claude-update.sh: lock, pause and busy checks come before the npm install",
     at(cucode, "flock -n 9") < at(cucode, "systemctl stop quiver-broker.path")
     < at(cucode, "for u in quiver-broker.service quiver-worker.service")
     < at(cucode, "npm install -g --prefix"))
ok_branch = cucode[at(cucode, 'log "verify OK'):at(cucode, "elif grep -qiE")]
r.ok("claude-update.sh: the canary runs in the verify-OK branch",
     "run_broker.py --canary --bin" in ok_branch and '[ "$AFTER" != "$BEFORE" ]' in ok_branch)
r.ok("claude-update.sh: --bin gets the binary path", '_ "$SVC_BIN"' in ok_branch)
r.ok("claude-update.sh: a canary failure rolls back, logs, pages and exits 1",
     "@anthropic-ai/claude-code@\"$BEFORE\"" in ok_branch and "canary failed: kept $BEFORE" in ok_branch
     and "page " in ok_branch and "exit 1" in ok_branch)
r.ok("claude-update.sh: the page names stage broker", "Stage: broker" in ok_branch)
r.ok("claude-update.sh: the chat bridge moves onto the new binary",
     "systemctl try-restart quiver-chat.service" in cucode)
r.ok("claude-update.sh: no Conflicts, no kill of a run", "systemctl stop quiver-broker.service" not in cucode)
tok_start = cu.find("TOKEN=$(sed")
tok_end = cu.find(")\n", tok_start)
tok_block = cu[tok_start:tok_end + 1] if tok_start >= 0 else ""
for label, line in (("quoted", 'CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-q"'),
                    ("bare", "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-q")):
    with tempfile.TemporaryDirectory() as d:
        envf = Path(d) / "quiver.env"
        envf.write_text(f'SEC_USER_AGENT="Quiver a@b.c"\n{line}\n')
        p = subprocess.run(["bash", "-c", f"ENVF={json.dumps(str(envf))}\n{tok_block}\nprintf '%s' \"$TOKEN\""],
                           capture_output=True, text=True)
        r.eq(f"claude-update.sh: the token reader strips quotes ({label})", p.stdout, "sk-ant-oat01-q")

# bootstrap-gcp.sh
boot = (GCP / "bootstrap-gcp.sh").read_text(encoding="utf-8")
m = re.search(r"^for k in ([^;]+); do", boot, re.M)
r.eq("bootstrap-gcp.sh: the fixed key list", m.group(1).split() if m else None,
     ["RH_ACCOUNT_NUMBER", "CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS",
      "TELEGRAM_ALERT_CHAT_IDS", "SEC_USER_AGENT", "ALPHA_VANTAGE_API_KEY"])
r.ok("bootstrap-gcp.sh: still pushes STRATEGY_YAML", 'put STRATEGY_YAML "$(cat strategy.yaml)"' in boot)

# --- §15.3/§15.4 terraform ------------------------------------------------------------

tf = (GCP / "terraform" / "main.tf").read_text(encoding="utf-8")
m = re.search(r"log_patterns = \{([^}]*)\}", tf)
patterns = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', m.group(1))) if m else {}
r.eq("main.tf: log_patterns has exactly the three keys", patterns,
     {"auth_error": "AUTH_ERROR", "daily_halt": "daily_halt", "tick_error": "stage_error"})
m = re.search(r"secrets = \[(.*?)\n\s*\]", tf, re.S)
secrets = re.findall(r'"([A-Z_]+)"', m.group(1)) if m else []
r.eq("main.tf: locals.secrets", secrets,
     ["RH_ACCOUNT_NUMBER", "STRATEGY_YAML", "CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN",
      "TELEGRAM_ALLOWED_CHAT_IDS", "SEC_USER_AGENT", "ALPHA_VANTAGE_API_KEY", "OPENROUTER_API_KEY",
      "GLM_API_KEY", "DEEPSEEK_API_KEY", "RESEND_API_KEY", "RESEND_FROM", "NOTIFY_TO"])
r.ok("main.tf: prevent_destroy kept", "prevent_destroy = true" in tf)
r.ok("main.tf: allow_stopping_for_update kept", "allow_stopping_for_update = true" in tf)
r.ok("main.tf: ignore_changes kept", "boot_disk[0].initialize_params[0].image," in tf
     and "shielded_instance_config," in tf)
r.ok("main.tf: the startup script still starts the timer", "systemctl start quiver.timer 2>/dev/null || true" in tf)
r.ok("main.tf: machine_type from the variable", "machine_type = var.machine_type" in tf)
var = (GCP / "terraform" / "variables.tf").read_text(encoding="utf-8")
m = re.search(r'variable "machine_type" \{(.*?)\n\}', var, re.S)
r.ok("variables.tf: machine_type default e2-medium", m and 'default     = "e2-medium"' in m.group(1))
ex = (GCP / "terraform" / "terraform.tfvars.example").read_text(encoding="utf-8")
r.ok("terraform.tfvars.example: e2-medium", re.search(r'^machine_type\s*=\s*"e2-medium"', ex, re.M))

# --- §15.3 env examples and .gitignore ------------------------------------------------


def assigned(text: str) -> dict:
    out = {}
    for ln in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", ln)
        if m:
            out[m.group(1)] = m.group(2)
    return out


RETIRED = ("OPENROUTER_API_KEY", "GLM_API_KEY", "DEEPSEEK_API_KEY", "RESEND_API_KEY", "RESEND_FROM",
           "NOTIFY_TO", "NOTIFY_ALERTS_TO", "CONGRESS_API_KEY")
MODELS = ("QUIVER_DESK_MODEL", "QUIVER_ANALYST_MODEL", "QUIVER_CLASSIFIER_MODEL", "QUIVER_BROKER_MODEL")
for fname in ("deploy/runner/runner.env.example", ".env.example"):
    text = (REPO / fname).read_text(encoding="utf-8")
    keys = assigned(text)
    for k in RETIRED + ("QUIVER_MODEL", "QUIVER_EFFORT", "QUIVER_TICK_TIMEOUT_SEC"):
        r.ok(f"{fname}: no {k}", not re.search(rf"\b{k}\b", text))
    r.ok(f"{fname}: no CLAUDE_CONFIG_DIR assignment", "CLAUDE_CONFIG_DIR" not in keys)
    for k in MODELS + ("SEC_USER_AGENT", "ALPHA_VANTAGE_API_KEY"):
        r.ok(f"{fname}: names {k}", k in text)
    r.ok(f"{fname}: SEC_USER_AGENT is quoted", re.fullmatch(r'"[^"]+ [^"]+"', keys.get("SEC_USER_AGENT", "")),
         keys.get("SEC_USER_AGENT"))
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "RH_ACCOUNT_NUMBER", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS"):
        r.ok(f"{fname}: sets {k}", k in keys)
    p = subprocess.run(["bash", "-c", 'set -euo pipefail; set -a; . "$1"; set +a; printf "%s" "$SEC_USER_AGENT"',
                        "_", str(REPO / fname)], capture_output=True, text=True)
    r.ok(f"{fname}: bash sources it (bootstrap-gcp.sh does)", p.returncode == 0 and " " in p.stdout,
         (p.returncode, p.stderr))
r.eq("runner.env.example: the set keys",
     set(assigned((RUNNER / "runner.env.example").read_text(encoding="utf-8"))),
     {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_BIN", "RH_ACCOUNT_NUMBER", "TELEGRAM_BOT_TOKEN",
      "TELEGRAM_ALLOWED_CHAT_IDS", "SEC_USER_AGENT"})
gi = (REPO / ".gitignore").read_text(encoding="utf-8")
r.ok(".gitignore: no quiver_eve lines", "quiver_eve" not in gi)
r.ok(".gitignore: still ignores CLAUDE.md, strategy.yaml, .env",
     all(re.search(rf"^{re.escape(x)}$", gi, re.M) for x in ("CLAUDE.md", "strategy.yaml", ".env")))

raise SystemExit(r.done())
