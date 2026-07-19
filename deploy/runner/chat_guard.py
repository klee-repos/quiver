#!/usr/bin/env python3
"""PreToolUse read-only guard for the Telegram chat bridge — the mechanical sandbox.

The chat assistant (`chat_bridge.py` -> `claude -p`) answers questions about the bot from the
ledger + repo. It runs headless with `--dangerously-skip-permissions`, so — exactly like the
trading tick's `order_guard.py` — this PreToolUse hook is the REAL security boundary, not the
prompt. It enforces STRICT read-only:

  * DENY every mutating built-in (Write/Edit/NotebookEdit) and any order/broker tool.
  * DENY WebFetch/WebSearch (no need; an exfiltration/prompt-injection vector).
  * DENY any Bash that isn't on a fail-closed read-only ALLOWLIST — no writes, no redirection,
    no command substitution, no network tools, no secret-file reads, no `python -c`.
  * Read/Grep/Glob/LS are allowed, but a secret-ish path (`.env`, quiver.env, claude-config,
    tokens/keys) is denied even for a read.

Design note (learned from two adversarial audits): do NOT try to *parse* arbitrary shell safely —
a hand-written parser inevitably diverges from bash (newlines as separators, backslash escapes,
`$(...)`/globs inside quotes, sqlite `.load`/`.system`), and every divergence is an RCE. Instead
this guard *eliminates* the dangerous constructs: it rejects control chars, backslashes, command/
parameter substitution (`$`, backtick — anywhere but inside single quotes), redirection, globbing,
subshells, and sqlite dot-commands outright. A banned character cannot create a parser divergence.
A legitimate read-only ledger query needs none of them.

Defense in depth: the bridge already loads ZERO MCP servers (`--strict-mcp-config` + an empty
mcp.json), so there is no broker to call in the first place; this guard is the second wall, and
the systemd unit's read-only `ReadWritePaths` is the third. The pure decision lives in
``evaluate()`` (unit-tested offline, no imports); ``main()`` adapts the `claude` CLI hook
stdin/stdout contract around it and FAILS CLOSED (deny) on any error.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# The repo root (this file is <repo>/deploy/runner/chat_guard.py). Resolved ONCE at import (not
# per call), so evaluate() stays effectively pure. Used to CONFINE file reads to the repo —
# containment, not a name denylist, closes the "Grep /etc" / "Glob state/.." recursion bypass.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
# Allowed READ roots: the repo, plus the box's shipped log dir (secret-free by design — the raw
# transcript is never written there). Reads are confined to these; everything else is denied.
# realpath'd so a symlinked repo root still matches its resolved target.
_READ_ROOTS = tuple(os.path.realpath(r) for r in (_REPO_DIR, "/var/log/quiver"))

# --- tool-name policy -------------------------------------------------------------------------
# Mutating / trading / network tools: always denied (there is no MCP loaded, but be explicit).
DENIED_TOOLS = {
    "write", "edit", "multiedit", "notebookedit", "applypatch",
    "webfetch", "websearch",
    "place_equity_order", "place_option_order",
    "cancel_equity_order", "cancel_option_order",
}
# The built-in RECURSIVE search tools are DENIED (they walk into secret subtrees regardless of the
# pattern text — the name-only check can't bound recursion). The assistant reads a specific file
# (Read) or `grep <pattern> <file>` on one file instead.
RECURSIVE_READ_TOOLS = {"grep", "glob"}
# Non-recursive read tools. A file-path arg is confined to the repo AND secret-checked below.
READ_TOOLS = {"read", "ls", "bashoutput", "todowrite", "todoread"}

# --- secret-path policy (applied to Bash commands AND file-path tool args) ---------------------
# Case-insensitive substrings that must never be read/printed. Blocks `.env` reads,
# `grep RESEND_API_KEY ...`-style exfiltration, AND the process-environment side channel
# (`/proc/self/environ`, `/proc/<pid>/environ`, `/proc/self/cmdline`) — the child inherits env,
# so those files can leak secrets around a filename denylist (audit finding). Intentionally
# broad: a chat that can't answer "did the token expire" in shell can still answer from the logs.
SECRET_PATTERNS = re.compile(
    r"(\.env\b|quiver\.env|/etc/quiver|claude-config|\.npm|credentials|"
    r"id_rsa|\.ssh|oauth|api[_-]?key|secret|token|password|"
    r"resend|glm_api|deepseek|anthropic|openrouter|congress_api|"
    r"rh_account|\.aws|\.gcloud|application_default|"
    r"/proc(/|$)|environ|cmdline|/sys/|claude\.md)",
    re.IGNORECASE,
)

# --- Bash allowlist ---------------------------------------------------------------------------
# Read-only shell utilities, matched on the command basename. CRITICAL: a basename allowlist is
# only sound for tools with NO write/exec flag. Tools that CAN write or exec via an argument are
# deliberately EXCLUDED (audit lesson): `find` (-exec/-delete/-fprintf), `sort` (-o), `yq` (-i),
# `rg` (--pre runs a program), `tree` (-o), plus `sed`/`awk`/`env`/`printenv`/`tee`. File search
# is served by the built-in Glob/Grep tools instead. `_DANGEROUS_ARGS` below is a second, global
# net so any future slip still can't reach an exec/write flag.
# NOTE: `jq` is intentionally EXCLUDED — its in-process `env`/`$ENV` builtin reads the process
# environment (an env-read path that bypasses the /proc block). The child env is already scrubbed,
# but dropping jq removes the read path entirely (audit defense-in-depth).
# EXCLUDED write vectors (a read-only chat needs none of them): `uniq [IN [OUT]]` (positional
# output write), `file -C` (compiles a magic .mgc file), plus sort -o / find -exec / sed -i / awk.
SAFE_BASH = {
    "cat", "head", "tail", "nl", "tac", "wc", "ls", "stat",
    "grep", "egrep", "fgrep", "cut", "column", "tr",
    "echo", "printf", "basename", "dirname", "pwd", "true", "date", "test",
}
# Argument tokens that grant code-exec or file-write on SOME utility. Denied on ANY segment,
# regardless of the command — a global backstop for the basename allowlist. Chosen so none
# collide with a benign flag of a SAFE_BASH tool (e.g. grep's -o/-i are NOT here; sort's -o is
# moot since sort is not allowlisted). `--follow`/`-f`/`-F` (tail -f) is handled per-tool below.
_DANGEROUS_ARGS = {
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprintf", "-fprint0", "-fls",
    "--pre", "--pre-glob", "--in-place", "--inplace", "--output-file", "--output",
}
# `git` is allowed only with a PURE-READ subcommand. `branch`/`tag`/`remote` are EXCLUDED — they
# are dual-use: read-only bare, but `git branch NAME` / `git tag NAME` / `git remote add …` mutate
# .git (and `git remote update` fetches over the network). The guard only vets the subcommand name.
SAFE_GIT_SUBS = {"log", "show", "diff", "status", "rev-parse", "blame",
                 "shortlog", "describe", "cat-file", "ls-files", "ls-tree"}
# `python` is allowed ONLY as a read-only tick.py subcommand, chat_query.py, or `-m lib.market`.
READONLY_TICK_SUBS = {"decision-proof", "strategy-history", "memory-show", "flow-suggest"}


def _base_tool(tool_name: str) -> str:
    """Strip any mcp__server__ prefix and lower-case for matching."""
    return str(tool_name or "").split("__")[-1].strip().lower()


def _deny(reason: str) -> tuple:
    return (False, reason)


def _inside_repo(p: str) -> bool:
    """True if a file path resolves inside an allowed read root (the repo or /var/log/quiver).
    Rejects `~` (would expand to $HOME) and `..` up front, then resolves SYMLINKS via realpath so
    an in-repo symlink pointing outside can't smuggle an out-of-repo read past the containment."""
    if not p:
        return True
    if p.startswith("~"):
        return False
    if ".." in p.replace("\\", "/").split("/"):
        return False
    try:
        real = os.path.realpath(p)  # relative resolves against cwd (= repo at runtime); resolves symlinks
    except (OSError, ValueError):
        return False
    return any(real == r or real.startswith(r + os.sep) for r in _READ_ROOTS)


def _scan(cmd: str) -> tuple:
    """Split a command into pipeline/sequence segments, ELIMINATING everything that could make a
    hand-written parser disagree with bash. Three quote states:
      * single-quoted  '...'  -> fully literal until the closing ' (bash-exact; nothing expands).
      * double-quoted  "..."  -> literal, BUT $ and backtick still expand in bash, so they are
                                 DENIED here (backslash is already banned globally, so `\\"` cannot
                                 occur and `"` always closes -> no quote-state divergence).
      * unquoted              -> split on ; | && ||, and DENY $ ` (substitution), < > (redirection),
                                 * ? [ ] (globbing), ( ) { } (subshell/group), ~ (tilde), & (bg).
    Metacharacters that are legitimately literal (a SQL `;` or `WHERE pnl > 0`) live inside quotes
    and pass through untouched. Fail-closed: an unbalanced quote returns a danger.
    Returns (segments, danger)."""
    segments, cur = [], []
    quote = None  # None | "'" | '"'
    i, n = 0, len(cmd)

    def _flush():
        seg = "".join(cur).strip()
        if seg:
            segments.append(seg)
        cur.clear()

    while i < n:
        c = cmd[i]
        if quote == "'":                       # single quotes: opaque (bash-exact)
            cur.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if quote == '"':                       # double quotes: $/backtick still expand -> deny
            if c == '"':
                quote = None; cur.append(c); i += 1; continue
            if c in "$`":
                return [], "expansion/substitution inside double quotes is not allowed"
            cur.append(c); i += 1; continue
        # unquoted
        if c == "'":
            quote = "'"; cur.append(c)
        elif c == '"':
            quote = '"'; cur.append(c)
        elif c in "$`":
            return [], "command/parameter substitution is not allowed"
        elif c in "<>":
            return [], "redirection is not allowed"
        elif c in "*?[]":
            return [], "filename globbing is not allowed"
        elif c in "(){}":
            return [], "subshell/grouping is not allowed"
        elif c == "&":
            if i + 1 < n and cmd[i + 1] == "&":
                _flush(); i += 2; continue
            return [], "backgrounding '&' is not allowed"
        elif c == "|":
            if i + 1 < n and cmd[i + 1] == "|":
                _flush(); i += 2; continue
            _flush()
        elif c == ";":
            _flush()
        else:
            cur.append(c)
        i += 1
    if quote:
        return [], "unbalanced quote"
    _flush()
    return segments, None


def _bash_ok(command: str) -> tuple:
    """(allow, reason) for a single Bash tool call. Fail-closed allowlist."""
    cmd = (command or "").strip()
    if not cmd:
        return _deny("empty command")
    # Eliminate the two parser-divergence classes at the source (a read-only query needs neither):
    #  - control chars: bash treats a raw newline as a command separator, but shlex collapses it to
    #    whitespace, so `echo hi\ncurl evil` would smuggle a second command past a per-segment check.
    #  - backslash: escapes mis-toggle any hand-written quote tracker (`echo \" ; cmd \"`).
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in cmd):
        return _deny("control characters (newlines/tabs) are not allowed")
    if "\\" in cmd:
        return _deny("backslash escaping is not allowed")
    if SECRET_PATTERNS.search(cmd):
        return _deny("command references a secret/credential path")
    segments, danger = _scan(cmd)
    if danger:
        return _deny(danger)
    if not segments:
        return _deny("no runnable command")
    for seg in segments:
        ok, reason = _segment_ok(seg)
        if not ok:
            return _deny(reason)
    return (True, "read-only command allowed")


def _segment_ok(seg: str) -> tuple:
    """(allow, reason) for one pipeline segment (already split on ; and |)."""
    try:
        toks = shlex.split(seg)
    except ValueError:
        return _deny("unparseable command")
    if not toks:
        return _deny("empty segment")
    argv0 = toks[0]
    # No inline env assignment as the command (e.g. `PATH=/x cmd`, `KEY=v ...`).
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv0):
        return _deny("inline environment assignment is not allowed")
    # Tilde EXPANSION happens only on a word that STARTS with `~` (bash rule), so deny those but
    # allow `~` mid-word (git's `HEAD~1` revision syntax is not expanded).
    for t in toks:
        if t.startswith("~"):
            return _deny("tilde expansion is not allowed")
    base = argv0.rsplit("/", 1)[-1]

    # Global backstop: an exec/write flag anywhere in the segment is denied regardless of the
    # command (defends the basename allowlist against find -exec, sort -o, rg --pre, etc.).
    for t in toks[1:]:
        flag = t.split("=", 1)[0]
        if flag in _DANGEROUS_ARGS:
            return _deny(f"argument '{t}' can write/exec — denied")

    if base in SAFE_BASH:
        # `sed`/`awk`/`find`/`sort`/`jq` are intentionally NOT in SAFE_BASH (write/exec/env-read).
        # Semantic flag checks below (not an exact-token list) so bundled/`=value` forms can't slip.
        opt_toks = []
        for t in toks[1:]:
            if t == "--":          # POSIX end-of-options; the rest are file operands
                break
            opt_toks.append(t)

        def _short_bundle_has(flags: str) -> bool:
            return any(len(t) > 1 and t[0] == "-" and t[1] != "-" and any(c in t for c in flags)
                       for t in opt_toks)

        if base in ("tail", "head"):
            # follow (-f/-F/--follow[=x]/--retry) would block until the timeout.
            if any(t == "--follow" or t.startswith("--follow=") or t == "--retry" for t in opt_toks) \
                    or _short_bundle_has("fF"):
                return _deny(f"{base} --follow would hang — denied")
        if base in ("grep", "egrep", "fgrep"):
            # recursion walks into secret subtrees (state/claude-config, .env, ...).
            if any(t in ("-r", "-R", "--recursive", "-d", "--directories") or
                   t.startswith("--directories=") for t in opt_toks) or _short_bundle_has("rR"):
                return _deny(f"{base} recursive/directory search is not allowed")
        if base == "ls":
            if any(t in ("-R", "--recursive") for t in opt_toks) or _short_bundle_has("R"):
                return _deny("ls -R (recursive) is not allowed")
        # CONFINE file operands to the repo — same guarantee as the built-in Read/LS branch. A
        # coreutil must not read an out-of-repo path the name denylist doesn't enumerate
        # (~/.netrc, ~/.config/gh/hosts.yml, /etc/passwd, ../../etc/...). Operands are literal
        # tokens here (globbing/backslash/substitution are already eliminated), so any token that
        # is absolute (`/`, `~`) or contains `..` is a path we must contain; benign patterns/args
        # are relative and pass. This turns SECRET_PATTERNS back into defense-in-depth.
        for t in toks[1:]:
            if t.startswith("-"):
                continue  # a flag, not a path operand
            if not _inside_repo(t):
                return _deny("bash targets a path outside the repository")
        return (True, f"{base} is a read-only utility")

    if base == "git":
        sub = toks[1] if len(toks) > 1 else ""
        if sub in SAFE_GIT_SUBS:
            return (True, f"git {sub} is read-only")
        return _deny(f"git subcommand '{sub or '(none)'}' is not read-only")

    if base == "sqlite3":
        # The sqlite3 CLI ships writefile()/readfile()/edit() (filesystem write, NOT blocked by
        # -readonly) and load_extension() (native code = RCE) as SQL functions a keyword denylist
        # can't reliably catch. Route ledger reads through the sqlite3-MODULE wrapper instead.
        return _deny("the sqlite3 CLI is not allowed; use "
                     '`.venv/bin/python deploy/runner/chat_query.py "<SELECT ...>"` instead')

    if base in {"python", "python3"} or base.endswith("/python") or base.endswith("/python3"):
        return _python_ok(toks)

    return _deny(f"'{base}' is not on the read-only allowlist")


def _python_ok(toks: list) -> tuple:
    """Allow ONLY `python chat_query.py <sql>`, `python tick.py <readonly-sub> ...`, or
    `python -m lib.market`. Everything else (notably `-c`) is arbitrary code execution."""
    args = toks[1:]
    if not args:
        return _deny("bare python interpreter is not allowed")
    # `python -m lib.market` — the market-status helper (read-only).
    if args[0] == "-m":
        if len(args) >= 2 and args[1] in {"lib.market"}:
            return (True, "python -m lib.market allowed")
        return _deny(f"python -m {args[1] if len(args) > 1 else '(none)'} is not allowed")
    # Any other flag form (-c, -, -i, ...) is arbitrary code execution — deny.
    if args[0].startswith("-"):
        return _deny("only chat_query.py / tick.py / `-m lib.market` may be run via python")
    # PIN the script to the repo's real file by its exact relative path — a BASENAME match would
    # allow `python /tmp/evil/chat_query.py` (attacker-planted). Reject absolute paths and any
    # parent-traversal; the box always runs these repo-relative (cwd = repo).
    spath = args[0]
    if spath.startswith("/") or ".." in spath.split("/"):
        return _deny("script path must be repo-relative (no absolute path, no '..')")
    norm = spath[2:] if spath.startswith("./") else spath
    # The read-only ledger-query wrapper (SELECT-only, sqlite3 MODULE — no writefile/load_extension).
    if norm == "deploy/runner/chat_query.py":
        return (True, "chat_query.py (read-only ledger query) allowed")
    if norm != "tick.py":
        return _deny("only deploy/runner/chat_query.py or tick.py may be run via python")
    sub = args[1] if len(args) > 1 else ""
    if sub in READONLY_TICK_SUBS:
        return (True, f"tick.py {sub} is read-only")
    return _deny(f"tick.py subcommand '{sub or '(none)'}' is not read-only")


def evaluate(tool_name: str, tool_input: dict) -> tuple:
    """(allow: bool, reason: str). The pure policy — no imports, no I/O, unit-tested offline."""
    base = _base_tool(tool_name)
    ti = tool_input or {}

    if base in DENIED_TOOLS:
        return _deny(f"{base} is a mutating/trading/network tool — read-only chat forbids it")

    if base == "bash":
        return _bash_ok(ti.get("command", ""))

    if base in RECURSIVE_READ_TOOLS:
        # Built-in Grep/Glob recurse into secret subtrees regardless of the pattern text — deny.
        return _deny(f"{base} (recursive search) is disabled; Read a specific file, or "
                     "`grep <pattern> <file>` on one file")

    if base in READ_TOOLS:
        # A read is fine, but never a secret path AND never outside the repo (containment —
        # a name-only check misses `Read /etc/quiver/..` reached via a parent dir).
        for key in ("file_path", "path", "notebook_path"):
            val = ti.get(key)
            if isinstance(val, str):
                if SECRET_PATTERNS.search(val):
                    return _deny(f"{base} targets a secret/credential path")
                if not _inside_repo(val):
                    return _deny(f"{base} targets a path outside the repository")
        return (True, f"{base} is a read-only tool")

    # Unknown tool: no MCP is loaded, so this should not occur. Fail closed.
    return _deny(f"unrecognized tool '{base}' — denied by the read-only allowlist")


def main() -> int:
    """Adapt the `claude` PreToolUse hook contract: tool call on stdin, allow/deny on stdout.
    Fail CLOSED (deny) on any error so a guard bug can never widen the sandbox."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        payload = {}
    if not isinstance(payload, dict):   # valid JSON but not an object -> fail CLOSED, don't crash
        payload = {}
    tool_name = payload.get("tool_name") or payload.get("tool") or ""
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    try:
        allow, reason = evaluate(tool_name, tool_input)
    except Exception as e:  # noqa: BLE001 — any guard failure denies
        allow, reason = False, f"guard error, failing closed: {type(e).__name__}: {e}"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allow else "deny",
            "permissionDecisionReason": reason,
        },
        "decision": "approve" if allow else "block", "reason": reason,
    }))
    return 0 if allow else 2


if __name__ == "__main__":
    raise SystemExit(main())
