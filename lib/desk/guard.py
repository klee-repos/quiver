"""The PreToolUse policy of the desk agent and the analyst: role allowlist + path confinement.

The desk and the analyst run with ``permission_mode="bypassPermissions"``, so this check is
the real gate (plan §5.2). It allows a call only when the tool is on the role's allowlist.
For ``Read``, ``Grep`` and ``Glob`` the realpath of the target must be inside one of
``lib/``, ``deploy/``, ``docs/``, ``prompts/``, ``tests/``, or be one of the files
``tick.py``, ``config.yaml``, ``EXECUTE.md``, ``SNAPSHOT.md``, ``README.md`` at the repo root.
A ``Grep`` or ``Glob`` with no path, or with the repo root as its path, is denied: it would
recurse into ``state/`` and ``.env``. The secret patterns stay as a second check. Every
other tool is denied, unknown ones included.

``evaluate`` is pure (no I/O beyond ``realpath``). ``lib/desk/agent.py`` wraps it in the SDK
hook, which denies on any exception.
"""

from __future__ import annotations

import os
import re

# Case-insensitive substrings that must never be read: env files, the box secrets, the
# Robinhood and Claude configs, tokens and keys, the process environment, and CLAUDE.md
# (it holds the account number).
SECRET_PATTERNS = re.compile(
    r"(\.env\b|quiver\.env|/etc/quiver|claude-config|claude-desk|\.npm|credentials|"
    r"id_rsa|\.ssh|oauth|api[_-]?key|secret|token|password|"
    r"rh_account|\.aws|\.gcloud|application_default|"
    r"/proc(/|$)|environ|cmdline|/sys/|claude\.md|ledger\.db)",
    re.IGNORECASE,
)

# Tools no role may use, whatever an allowlist says.
DENIED_TOOLS = {
    "bash", "write", "edit", "multiedit", "notebookedit", "applypatch", "webfetch", "task",
    "agent", "toolsearch", "skill", "killshell", "bashoutput",
}
BUILTINS = ("Read", "Grep", "Glob", "WebSearch")
ALLOWED_DIRS = ("lib", "deploy", "docs", "prompts", "tests")
ALLOWED_FILES = ("tick.py", "config.yaml", "EXECUTE.md", "SNAPSHOT.md", "README.md")


def _inside(root: str, path: str) -> bool:
    """True when ``path`` (relative to ``root`` or absolute) resolves inside an allowed
    directory or is an allowed root file. ``~`` and ``..`` are refused before realpath,
    and realpath resolves symlinks, so a link cannot smuggle a read out."""
    if not path or path.startswith("~"):
        return False
    if ".." in path.replace("\\", "/").split("/"):
        return False
    root_real = os.path.realpath(root)
    p = path if os.path.isabs(path) else os.path.join(root_real, path)
    try:
        real = os.path.realpath(p)
    except (OSError, ValueError):
        return False
    for d in ALLOWED_DIRS:
        base = os.path.join(root_real, d)
        if real == base or real.startswith(base + os.sep):
            return True
    return any(real == os.path.join(root_real, f) for f in ALLOWED_FILES)


def _path_ok(root: str, tool: str, ti: dict) -> tuple:
    if tool == "Read":
        path = ti.get("file_path")
        if not isinstance(path, str) or not path:
            return False, "Read needs a file_path"
    else:
        path = ti.get("path")
        if not isinstance(path, str) or not path.strip():
            return False, f"{tool} needs a path inside lib/, deploy/, docs/, prompts/ or tests/"
        pattern = str(ti.get("pattern") or ti.get("glob") or "")
        if tool == "Glob" and (pattern.startswith("/") or ".." in pattern.split("/")):
            return False, "Glob pattern must be relative and stay inside its path"
    if SECRET_PATTERNS.search(path):
        return False, f"{tool} targets a secret path"
    if os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path)) == os.path.realpath(root):
        return False, f"{tool} on the repo root would recurse into state/ and .env"
    if not _inside(root, path):
        return False, f"{tool} targets a path outside lib/, deploy/, docs/, prompts/, tests/ and the root files"
    return True, f"{tool} inside the repo"


def evaluate(tool_name: str, tool_input: dict, *, allowed: frozenset, root: str) -> tuple:
    """(allow, reason) for one tool call of a role whose allowlist is ``allowed`` (full
    tool names: ``mcp__desk__say``, ``Read``, ...)."""
    name = str(tool_name or "")
    ti = tool_input if isinstance(tool_input, dict) else {}
    if name.split("__")[-1].lower() in DENIED_TOOLS and not name.startswith("mcp__desk__"):
        return False, f"{name} is not allowed"
    if name not in allowed:
        return False, f"{name} is not on this role's tool list"
    if name in ("Read", "Grep", "Glob"):
        return _path_ok(root, name, ti)
    return True, f"{name} is on the tool list"
