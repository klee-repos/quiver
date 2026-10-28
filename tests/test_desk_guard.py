#!/usr/bin/env python3
"""Offline spec for the path confinement of the desk guard (``lib/desk/guard.py``).

Plain asserts through ``Results``. No network, no LLM. The desk agent and the analyst run with
``bypassPermissions``, so ``guard.evaluate`` is the real gate (plan §5.2). This file proves two
rules. A ``Read``, ``Grep`` or ``Glob`` resolves inside ``lib/``, ``deploy/``, ``docs/``,
``prompts/``, ``tests/`` or one of the five root files, else the guard denies it. A secret path
is denied even inside an allowed directory. The role and tool rules are in
``tests/test_desk_agent_hook.py``.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))

from desk_fakes import Results  # noqa: E402
from lib.desk import guard as g  # noqa: E402

ROOT = str(_REPO)
# The four built-ins of §5.2 plus one desk tool. A path check runs only for a tool on the list.
ALLOWED = frozenset({"Read", "Grep", "Glob", "WebSearch", "mcp__desk__say"})

r = Results("desk-guard")


def allow(label, tool, ti=None, *, root=ROOT, allowed=ALLOWED):
    ok, reason = g.evaluate(tool, ti or {}, allowed=allowed, root=root)
    r.ok(f"ALLOW {label}", ok, reason)


def deny(label, tool, ti=None, *, root=ROOT, allowed=ALLOWED):
    ok, reason = g.evaluate(tool, ti or {}, allowed=allowed, root=root)
    r.ok(f"DENY {label}", not ok, reason)


# --- the allowed directories and root files pass -------------------------------------
allow("Read config.yaml", "Read", {"file_path": "config.yaml"})
allow("Read tick.py", "Read", {"file_path": "tick.py"})
allow("Read EXECUTE.md", "Read", {"file_path": "EXECUTE.md"})
allow("Read SNAPSHOT.md", "Read", {"file_path": "SNAPSHOT.md"})
allow("Read README.md", "Read", {"file_path": "README.md"})
allow("Read docs/DESK_PLAN.md", "Read", {"file_path": "docs/DESK_PLAN.md"})
allow("Read lib/notify.py", "Read", {"file_path": "lib/notify.py"})
allow("Read an absolute path inside lib/", "Read", {"file_path": str(_REPO / "lib" / "notify.py")})
allow("Read prompts/ file", "Read", {"file_path": "prompts/desk.md"})
allow("Read deploy/ file", "Read", {"file_path": "deploy/runner/runner_common.py"})
allow("Read tests/ file", "Read", {"file_path": "tests/desk_fakes.py"})
allow("Grep on lib/", "Grep", {"pattern": "def ", "path": "lib"})
allow("Grep on docs/", "Grep", {"pattern": "halt", "path": "docs"})
allow("Glob on lib/ with a relative pattern", "Glob", {"pattern": "**/*.py", "path": "lib"})
allow("WebSearch on the list", "WebSearch", {"query": "Arista 10-K"})

# --- a path outside the allowed set is denied ----------------------------------------
# These were allowed by the old chat guard (any repo path). The plan confines reads to the
# five directories and the five root files (§5.2).
deny("Read strategy.yaml (a root file not on the list)", "Read", {"file_path": "strategy.yaml"})
deny("Read logs/tick.log", "Read", {"file_path": "logs/tick.log"})
deny("Read logs/reasoning/x.log", "Read", {"file_path": "logs/reasoning/x.log"})
deny("Read /var/log/quiver/tick.log", "Read", {"file_path": "/var/log/quiver/tick.log"})
deny("Read state/tmp file", "Read", {"file_path": "state/tmp/exec_check.json"})
deny("Read state/ledger.db", "Read", {"file_path": "state/ledger.db"})
deny("Grep on state/", "Grep", {"pattern": "sk-", "path": "state"})

# --- containment: outside the repo, traversal, tilde ---------------------------------
deny("Read /etc/passwd", "Read", {"file_path": "/etc/passwd"})
deny("Read ../../etc/shadow", "Read", {"file_path": "../../etc/shadow"})
deny("Read lib/../.env (traversal)", "Read", {"file_path": "lib/../.env"})
deny("Read lib/../strategy.yaml (traversal out of an allowed dir)", "Read",
     {"file_path": "lib/../strategy.yaml"})
deny("Read ~/x (tilde)", "Read", {"file_path": "~/x"})
deny("Read with no file_path", "Read", {})
deny("Read with a non-string file_path", "Read", {"file_path": 42})
deny("Grep on /etc", "Grep", {"pattern": "^[A-Z_]+=", "path": "/etc"})
deny("Glob on state/.. (traversal)", "Glob", {"pattern": "**/*", "path": "state/.."})

# --- recursion: no path, or the repo root as the path --------------------------------
deny("Glob with no path", "Glob", {"pattern": "logs/*.log"})
deny("Grep with no path", "Grep", {"pattern": "TOKEN"})
deny("Grep with a blank path", "Grep", {"pattern": "x", "path": "   "})
deny("Grep on the repo root (relative .)", "Grep", {"pattern": "x", "path": "."})
deny("Grep on the repo root (absolute)", "Grep", {"pattern": "x", "path": ROOT})
deny("Glob on the repo root", "Glob", {"pattern": "**/*", "path": ROOT})
deny("Glob with an absolute pattern", "Glob", {"pattern": "/etc/*", "path": "lib"})
deny("Glob with a .. pattern", "Glob", {"pattern": "../*.env", "path": "lib"})

# --- a symlink cannot smuggle a read out ---------------------------------------------
_tmp = Path(tempfile.mkdtemp(prefix="quiver_guard_"))
try:
    (_tmp / "lib").mkdir()
    (_tmp / "lib" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    os.symlink("/etc", _tmp / "lib" / "etc_link")
    allow("Read a real file under a temp root's lib/", "Read", {"file_path": "lib/ok.py"}, root=str(_tmp))
    deny("Read through a symlink in lib/ to /etc", "Read", {"file_path": "lib/etc_link/passwd"},
         root=str(_tmp))
    deny("Grep on a symlink in lib/ to /etc", "Grep", {"pattern": "root", "path": "lib/etc_link"},
         root=str(_tmp))
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# --- secret paths: denied everywhere, also inside an allowed directory ----------------
deny("Read .env", "Read", {"file_path": ".env"})
deny("Read /etc/quiver/quiver.env", "Read", {"file_path": "/etc/quiver/quiver.env"})
deny("Read state/claude-config/credentials.json", "Read",
     {"file_path": "state/claude-config/credentials.json"})
deny("Read state/claude-config/.credentials.json", "Read",
     {"file_path": "state/claude-config/.credentials.json"})
deny("Read state/claude-desk/ config", "Read", {"file_path": "state/claude-desk/settings.json"})
deny("Grep on ../.env", "Grep", {"pattern": "x", "path": "../.env"})
deny("Read CLAUDE.md (it holds the account number)", "Read", {"file_path": "CLAUDE.md"})
deny("Read ~/.ssh/id_rsa", "Read", {"file_path": "~/.ssh/id_rsa"})
deny("Read /proc/self/environ", "Read", {"file_path": "/proc/self/environ"})
deny("Read /proc/1234/environ", "Read", {"file_path": "/proc/1234/environ"})
deny("Read /proc/self/cmdline", "Read", {"file_path": "/proc/self/cmdline"})
deny("Glob /proc/*/environ", "Glob", {"pattern": "/proc/*/environ"})
deny("Grep on /proc/self/environ", "Grep", {"pattern": "KEY", "path": "/proc/self/environ"})
# The secret patterns are a second check: they deny inside an allowed directory too.
deny("Read lib/.env (secret name inside lib/)", "Read", {"file_path": "lib/.env"})
deny("Read docs/.ENV (case-insensitive)", "Read", {"file_path": "docs/.ENV"})
deny("Read docs/api_key.txt", "Read", {"file_path": "docs/api_key.txt"})
deny("Read tests/fixtures/token.json", "Read", {"file_path": "tests/fixtures/token.json"})
deny("Read deploy/secret.yaml", "Read", {"file_path": "deploy/secret.yaml"})
deny("Grep on docs/ for a password file", "Grep", {"pattern": "x", "path": "docs/password"})
for pat in (".env", "quiver.env", "/etc/quiver", "claude-config", "claude-desk", ".npm",
            "credentials", "id_rsa", ".ssh", "oauth", "api_key", "api-key", "apikey", "secret",
            "token", "password", "rh_account", ".aws", ".gcloud", "application_default",
            "/proc/", "environ", "cmdline", "/sys/", "claude.md", "ledger.db"):
    r.ok(f"SECRET_PATTERNS matches {pat!r}", bool(g.SECRET_PATTERNS.search(f"lib/x{pat}/y")), pat)
r.ok("SECRET_PATTERNS does not match a plain module path",
     g.SECRET_PATTERNS.search("lib/desk/proposals.py") is None)

# --- tools that write or fetch are denied, also when a list names them ----------------
_ALL = ALLOWED | {"Write", "Edit", "NotebookEdit", "WebFetch", "Bash", "Task"}
for tool in ("Write", "Edit", "NotebookEdit", "WebFetch", "Bash", "Task"):
    deny(f"{tool} even when on the list", tool, {"file_path": "lib/x.py", "content": "y"}, allowed=_ALL)
deny("mcp__robinhood-trading__place_equity_order (not on the list)",
     "mcp__robinhood-trading__place_equity_order", {"ref_id": "abc"})
deny("place_equity_order (not on the list)", "place_equity_order", {"ref_id": "abc"})
deny("cancel_equity_order (not on the list)", "cancel_equity_order", {"ref_id": "abc"})
deny("an mcp-prefixed read name (not on the list)", "mcp__whatever__read", {"file_path": "config.yaml"})

# --- fail closed ----------------------------------------------------------------------
deny("an unknown tool", "SomeUnknownTool", {})
deny("an empty tool name", "", {})
deny("LS (not a §5.2 built-in)", "LS", {"path": "lib"})
deny("Read with a non-dict input", "Read", None)
ok_nd, _ = g.evaluate("Read", "lib/notify.py", allowed=ALLOWED, root=ROOT)
r.ok("DENY Read with a string input (no file_path)", not ok_nd)

raise SystemExit(r.done())
