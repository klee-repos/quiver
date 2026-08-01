#!/usr/bin/env python3
"""E2E -- the untracked-reference guard, driven as a REAL subprocess against REAL git repos.

Every case builds a throwaway repo on disk, runs `tests/check_tracked_refs.py` in it as a
separate process, and asserts on the exit code AND on what the process actually printed. There
are no fakes: the thing under test is the same file `tests/run_e2e.sh` and `deploy/gcp/sync.sh`
invoke, and the git state it reads is real git state.

Two halves carry equal weight, and the second is the one that decides whether this guard
survives contact with a working tree:

  DETECTION -- it must fire on the shapes that actually shipped (a function-level
  `from lib import benchmark as …`, a suite path in a shell array, an ESM `./x.mjs`).

  PRECISION -- it must stay silent on the ordinary. A guard that reports 50 bogus hits the
  first time someone leaves a scratch copy of `analyze.py` in the tree gets deleted within a
  day, and then it protects nothing. Cases P1-P6 are that half.

HERMETIC BY CONSTRUCTION. `--exclude-standard` -- the guard's whole precision mechanism --
honors the developer's global gitignore, and `git commit` needs an identity. Both are supplied
per fixture via `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`, `HOME=<tmpdir>`
and explicit author/committer env, so a case can never be decided by ~/.config/git/ignore or by
whether the machine running it happens to have a git identity.

THIS FILE OPTS OUT OF BEING A REFERRER -- check-tracked-refs: ignore-file -- because every
fixture below WRITES files by name (`lib/runner.py`, `deploy/boot.sh`, `run/availability.mjs`).
Those strings are test data, not dependencies. Verified before the marker existed: planting an
untracked `lib/runner.py` in the real repo made the guard report FOUR violations against this
file and exit 1, which would have blocked a deploy over a scratch file. Case X1 proves the
marker is scoped -- a real reference from any other file is still caught.

ANTI-VACUOUS. A guard that resolved its repo root from `__file__` instead of the process CWD
would inspect *quiver* on every case, find it clean, exit 0, and make every precision case pass
while proving nothing. V1 pins that shut: it asserts the guard's output names the FIXTURE's own
file, so a run that silently inspected some other repo fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "tests" / "check_tracked_refs.py"
PY = sys.executable

PASS = 0
FAIL = 0
_TMPDIRS: list[Path] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f"\n         {detail}" if detail else ""))


class Fixture:
    """A real, hermetic git repo in a temp dir."""

    def __init__(self, init: bool = True) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="quiver-refs-"))
        _TMPDIRS.append(self.d)
        self.env = {
            **os.environ,
            "HOME": str(self.d),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            # XDG_CONFIG_HOME is a THIRD source of a global gitignore
            # (~/.config/git/ignore). Leaving it inherited lets the developer's own excludes
            # decide the precision cases, which is the same non-reproducibility the two
            # GIT_CONFIG_* pins exist to remove.
            "XDG_CONFIG_HOME": str(self.d / ".xdg"),
            "GIT_AUTHOR_NAME": "quiver-test", "GIT_AUTHOR_EMAIL": "quiver@test.invalid",
            "GIT_COMMITTER_NAME": "quiver-test", "GIT_COMMITTER_EMAIL": "quiver@test.invalid",
        }
        if init:
            self.git("init", "-q", "-b", "main", ".")

    def git(self, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *a], cwd=self.d, env=self.env,
                              capture_output=True, text=True)

    def write(self, rel: str, text: str) -> Path:
        p = self.d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def track(self, *rels: str, msg: str = "c") -> None:
        """Commit exactly these paths. Explicit, never `add -A`, so each case's tracked set
        and untracked set are both stated outright rather than implied."""
        self.git("add", "--", *rels)
        r = self.git("commit", "-qm", msg)
        assert r.returncode == 0, f"fixture commit failed: {r.stderr}"

    def guard(self) -> subprocess.CompletedProcess:
        """Run the REAL guard, as a subprocess, with the fixture as CWD."""
        return subprocess.run([PY, str(GUARD)], cwd=self.d, env=self.env,
                              capture_output=True, text=True)


# --------------------------------------------------------------------------- detection


def case_clean() -> None:
    f = Fixture()
    f.write("a.py", "x = 1\n")
    f.track("a.py")
    r = f.guard()
    ok("D1: a clean tree exits 0", r.returncode == 0, r.stdout + r.stderr)
    ok("D1: and says so with the suite's no-op marker", "PASS (no-op)" in r.stdout, r.stdout)


def case_python_from_import() -> None:
    """The exact shape that shipped: tick.py's function-level `from lib import benchmark as …`.

    No path string and no dotted `lib.benchmark` appears anywhere in the referring file, so
    this is the case every text-pattern design misses.
    """
    f = Fixture()
    f.write("lib/__init__.py", "")
    f.write("tick.py", "def rows():\n    from lib import benchmark as benchmark_lib\n"
                       "    return benchmark_lib\n")
    f.track("lib/__init__.py", "tick.py")
    f.write("lib/benchmark.py", "def window_return():\n    return None\n")
    r = f.guard()
    ok("D2: `from lib import benchmark as x` -> untracked lib/benchmark.py is caught",
       r.returncode == 1, r.stdout + r.stderr)
    ok("D2: names the referring file and line", "tick.py:2" in r.stdout, r.stdout)
    ok("D2: names the untracked target", "lib/benchmark.py" in r.stdout, r.stdout)
    ok("D2: prints the remedy", "git add lib/benchmark.py" in r.stdout, r.stdout)


def case_python_dotted_and_multiline() -> None:
    f = Fixture()
    f.write("lib/__init__.py", "")
    f.write("dotted.py", "import lib.bench\n")
    f.write("paren.py", "from lib import (\n    other,\n    bench,\n)\n")
    f.track("lib/__init__.py", "dotted.py", "paren.py")
    f.write("lib/bench.py", "x = 1\n")
    r = f.guard()
    ok("D3: `import lib.bench` is caught", "dotted.py:1" in r.stdout, r.stdout)
    ok("D3: a parenthesized multi-line `from lib import (…, bench, …)` is caught "
       "(AST, not regex)", "paren.py:1" in r.stdout, r.stdout)
    ok("D3: exits 1", r.returncode == 1, r.stdout)


def case_shell_suite_path() -> None:
    """The second shape that shipped: tests/run_e2e.sh naming a suite file by path."""
    f = Fixture()
    f.write("run_e2e.sh", 'SUITES=(\n  "e2e-benchmark|tests/test_e2e_benchmark.py"\n)\n')
    f.track("run_e2e.sh")
    f.write("tests/test_e2e_benchmark.py", "print(1)\n")
    r = f.guard()
    ok("D4: a suite path in a tracked shell array is caught", r.returncode == 1, r.stdout)
    ok("D4: names run_e2e.sh:2", "run_e2e.sh:2" in r.stdout, r.stdout)


def case_esm_relative() -> None:
    f = Fixture()
    f.write("run/decide.mjs", 'import { a } from "./availability.mjs";\n')
    f.track("run/decide.mjs")
    f.write("run/availability.mjs", "export const a = 1;\n")
    r = f.guard()
    ok("D5: ESM `./availability.mjs` -> untracked sibling is caught", r.returncode == 1, r.stdout)
    ok("D5: names run/decide.mjs:1", "run/decide.mjs:1" in r.stdout, r.stdout)


def case_merge_conflict() -> None:
    """A tree mid-merge still gets a verdict.

    This is why the guard reads `ls-files --others`, not HEAD: during a conflicted merge there
    is no single tree to diff against, and a HEAD-based check either errors or reports garbage
    -- precisely when a hand-resolved merge is most likely to drop a file.
    """
    f = Fixture()
    f.write("shared.txt", "base\n")
    f.write("lib/__init__.py", "")
    f.write("tick.py", "def rows():\n    from lib import benchmark as b\n    return b\n")
    f.track("shared.txt", "lib/__init__.py", "tick.py")
    f.git("checkout", "-q", "-b", "other")
    f.write("shared.txt", "other side\n")
    f.track("shared.txt", msg="other")
    f.git("checkout", "-q", "main")
    f.write("shared.txt", "main side\n")
    f.track("shared.txt", msg="main")
    m = f.git("merge", "other")
    f.write("lib/benchmark.py", "x = 1\n")
    r = f.guard()
    ok("D6: fixture really is mid-merge", (f.d / ".git" / "MERGE_HEAD").exists(),
       f"merge rc={m.returncode}")
    # returncode==1 alone is not enough: an uncaught traceback also exits 1, so that assertion
    # would pass on a guard that crashed instead of one that reported.
    ok("D6: still catches the violation mid-merge",
       r.returncode == 1 and "lib/benchmark.py" in r.stdout and "Traceback" not in r.stderr,
       f"rc={r.returncode}\n{r.stdout}\n{r.stderr}")


# --------------------------------------------------------------------------- precision


def case_basename_collision() -> None:
    """P1 -- the case that decides whether this guard survives.

    A scratch copy of a file whose basename is mentioned all over the repo must not produce a
    single hit. Naive `grep -F <basename>` reports 52 files on the real repo for `analyze.py`.
    """
    f = Fixture()
    f.write("README.md", "Run analyze.py to score a ticker. See analyze.py for details.\n")
    f.write("TICK.md", "`analyze.py` is the entry point; analyze.py writes reports.\n")
    f.write("runner.sh", "python3 analyze.py --ticker AAPL\n")
    f.track("README.md", "TICK.md", "runner.sh")
    f.write("scratch/analyze.py", "x = 1\n")
    r = f.guard()
    ok("P1: a scratch copy whose BASENAME is all over the repo is silent",
       r.returncode == 0, r.stdout)
    ok("P1: and it did look at it (reported as accounted-for, not skipped)",
       "scratch/analyze.py" in r.stdout and "0 failed" in r.stdout, r.stdout)


def case_substring_path() -> None:
    """P2 -- `lib/x.py` must not match inside `sublib/x.py`, nor `benchmark.py` inside
    `test_e2e_benchmark.py` (the real repo's run_e2e.sh line does exactly this)."""
    f = Fixture()
    f.write("ref.sh", 'A="sublib/x.py"\nB="tests/test_e2e_benchmark.py"\n')
    f.write("tests/test_e2e_benchmark.py", "print(1)\n")
    f.track("ref.sh", "tests/test_e2e_benchmark.py")
    f.write("lib/x.py", "x = 1\n")
    f.write("benchmark.py", "x = 1\n")
    r = f.guard()
    ok("P2: a path that is only a SUBSTRING of a tracked mention is silent",
       r.returncode == 0, r.stdout)


def case_non_code_extension() -> None:
    """P3 -- the box's own `.gcp-provisioned`: untracked, NOT gitignored, and legitimately
    named by tracked terraform. Flagging it would fail every deploy from the box."""
    f = Fixture()
    f.write("main.tf", 'if [ -f /opt/quiver/.gcp-provisioned ]; then :; fi\n')
    f.write("notes.md", "see scratch/design.md\n")
    f.track("main.tf", "notes.md")
    f.write(".gcp-provisioned", "")
    f.write("scratch/design.md", "# notes\n")
    r = f.guard()
    ok("P3: untracked non-source files referenced by tracked files are not this defect",
       r.returncode == 0, r.stdout)
    ok("P3: reported as a no-op (they never entered the suspect set)",
       "PASS (no-op)" in r.stdout, r.stdout)


def case_typescript_nodenext() -> None:
    """P4 -- `validate.ts` importing `./agent.js` is correct TypeScript NodeNext for a file
    named `agent.ts`. A resolver-shaped guard flags this; the real repo has it at
    quiver_eve/agent/validate.ts."""
    f = Fixture()
    f.write("agent/agent.ts", "export const x = 1;\n")
    f.write("agent/validate.ts", 'import { x } from "./agent.js";\n')
    f.track("agent/agent.ts", "agent/validate.ts")
    # An UNRELATED untracked .mjs, so the suspect set is non-empty and the relative-specifier
    # rule actually executes. Without it the guard short-circuits at "nothing untracked" and
    # this case would pass without ever exercising the rule it claims to test.
    f.write("agent/scratch.mjs", "export const y = 2;\n")
    r = f.guard()
    ok("P4: TS NodeNext .js->.ts specifiers are not flagged", r.returncode == 0, r.stdout)
    ok("P4: and the specifier rule really ran (suspect set was non-empty)",
       "agent/scratch.mjs" in r.stdout and "PASS (no-op)" not in r.stdout, r.stdout)


def case_specifier_is_directory_aware() -> None:
    """P10 -- a relative specifier is relative to the file it appears IN.

    Proven against the real repo before the fix: an untracked `vendor/sdk/agent.js` made
    `quiver_eve/agent/validate.ts` report a violation, because its `./agent.js` import merely
    shared a basename. That import resolves to `quiver_eve/agent/agent.ts` and has nothing to
    do with the stray file — so the specifier must be resolved, not name-matched.
    """
    f = Fixture()
    f.write("agent/agent.ts", "export const x = 1;\n")
    f.write("agent/validate.ts", 'import { x } from "./agent.js";\n')
    f.track("agent/agent.ts", "agent/validate.ts")
    f.write("vendor/sdk/agent.js", "export const x = 1;\n")     # same basename, different dir
    quiet = f.guard()
    ok("P10: a same-basename file in an UNRELATED directory is not flagged",
       quiet.returncode == 0, quiet.stdout)

    # ...and the same specifier DOES fire when it really does resolve to the untracked file.
    g = Fixture()
    g.write("agent/validate.ts", 'import { x } from "./agent.js";\n')
    g.track("agent/validate.ts")
    g.write("agent/agent.js", "export const x = 1;\n")          # exactly where it resolves
    hit = g.guard()
    ok("P10: but the file the specifier actually resolves to IS flagged",
       hit.returncode == 1 and "agent/validate.ts:1" in hit.stdout, hit.stdout)


def case_unreferenced_scratch() -> None:
    """P5 -- an untracked file nobody references is just a scratch file, not a defect."""
    f = Fixture()
    f.write("a.py", "x = 1\n")
    f.track("a.py")
    f.write("wip_notes.py", "y = 2\n")
    r = f.guard()
    ok("P5: an unreferenced untracked source file is not a violation",
       r.returncode == 0, r.stdout)


def case_stdlib_shadowing() -> None:
    """P11 -- a scratch file named after a stdlib/installed module is not a dependency.

    Proven on the real repo before the fix: a throwaway `json.py` at the root produced 52 bogus
    referrers and exit 1, because every tracked `import json` looked like a reference to it.
    `import json` resolved to the stdlib before that file existed and resolves to the stdlib on
    the box, which never receives it.
    """
    f = Fixture()
    f.write("app.py", "import json\nimport os\n")
    f.write("lib/__init__.py", "")
    f.write("user.py", "from lib import newmod\n")
    f.track("app.py", "lib/__init__.py", "user.py")
    f.write("json.py", "K = 1\n")                 # shadows the stdlib — NOT a dependency
    shadow_only = f.guard()
    ok("P11: a scratch file shadowing a stdlib module is not a violation",
       shadow_only.returncode == 0, shadow_only.stdout)

    # ...and the fix must not blunt real first-party detection in the same tree.
    f.write("lib/newmod.py", "x = 1\n")
    both = f.guard()
    ok("P11: a genuine first-party module in the SAME tree is still caught",
       both.returncode == 1 and "user.py:1" in both.stdout, both.stdout)
    ok("P11: and the stdlib shadow is still not blamed",
       "json.py" not in both.stdout.split("REFERENCES")[-1], both.stdout)


def case_rooted_token_only() -> None:
    """P12 -- `/`-prefixed matches count only when the whole token is ROOTED.

    `vendor/lib/x.py` is a different file from `lib/x.py`; `/opt/quiver/lib/x.py` is the same
    one. An earlier revision admitted any leading `/` and so confused the two.
    """
    f = Fixture()
    f.write("other.sh", 'A="vendor/lib/x.py"\n')
    f.track("other.sh")
    f.write("lib/x.py", "x = 1\n")
    quiet = f.guard()
    ok("P12: a longer, unrelated path is not blamed on the suspect",
       quiet.returncode == 0, quiet.stdout)

    g = Fixture()
    g.write("unit.service", "ExecStart=/usr/bin/python3 /opt/quiver/lib/x.py\n")
    g.track("unit.service")
    g.write("lib/x.py", "x = 1\n")
    hit = g.guard()
    ok("P12: but a rooted absolute reference IS caught",
       hit.returncode == 1 and "unit.service:1" in hit.stdout, hit.stdout)


def case_systemd_unit_suspect() -> None:
    """D10 -- an untracked systemd unit a tracked script installs.

    The unit never reaches the box, so the timer simply never exists — the failure is total
    silence, which is exactly the shape this guard exists to break.
    """
    f = Fixture()
    f.write("setup.sh", 'cp "$HOME/repo/deploy/backup.timer" /etc/systemd/system/\n')
    f.track("setup.sh")
    f.write("deploy/backup.timer", "[Timer]\nOnCalendar=daily\n")
    r = f.guard()
    ok("D10: an untracked .timer named by a tracked installer is caught",
       r.returncode == 1 and "setup.sh:1" in r.stdout, r.stdout)


def case_prose_mentions() -> None:
    """P7 -- prose is not a dependency.

    A doc that names a file, or a comment that does, cannot make anything fail to load. Counting
    them means a scratch file someone once wrote a sentence about blocks the next deploy. This
    also covers the guard's OWN docstring, which names `lib/benchmark.py` as its worked example.
    """
    f = Fixture()
    f.write("README.md", "Run scripts/tool.py to rebuild the cache.\n")
    f.write("setup.sh", "# scripts/tool.py used to live here; see README\n")
    f.write("mod.py", '"""Docs: scripts/tool.py is the old entry point."""\nX = 1\n')
    f.track("README.md", "setup.sh", "mod.py")
    f.write("scripts/tool.py", "x = 1\n")
    r = f.guard()
    ok("P7: a .md mention, a # comment and a docstring are all prose, not references",
       r.returncode == 0, r.stdout)


def case_real_string_literal_still_caught() -> None:
    """P8 -- the prose rule must not swallow REAL references.

    The docstring exclusion is line-ranged, so a genuine string literal naming the same path
    elsewhere in the same file must still fire. Without this, P7 could be 'passing' because the
    rule silently disabled the whole path-reference check.
    """
    f = Fixture()
    f.write("runner.py",
            '"""Prose naming scripts/tool.py, which must NOT count."""\n'
            'import subprocess\n'
            'subprocess.run(["python3", "scripts/tool.py"])\n')
    f.track("runner.py")
    f.write("scripts/tool.py", "x = 1\n")
    r = f.guard()
    ok("P8: a real string-literal path reference is still caught in a file whose "
       "docstring also mentions it", r.returncode == 1, r.stdout)
    ok("P8: and it points at the CODE line, not the docstring line",
       "runner.py:3" in r.stdout and "runner.py:1" not in r.stdout, r.stdout)


def case_gitignored() -> None:
    """P6 -- a gitignored file that tracked code imports on purpose (this repo does exactly
    this with strategy.yaml / local config). `--exclude-standard` must keep it out."""
    f = Fixture()
    f.write(".gitignore", "local_config.py\n")
    f.write("app.py", "import local_config\n")
    f.track(".gitignore", "app.py")
    f.write("local_config.py", "KEY = 1\n")
    r = f.guard()
    ok("P6: a deliberately gitignored dependency is excluded from the suspect set",
       r.returncode == 0, r.stdout)


# --------------------------------------------------------------------------- remedy / env


def case_remedy_clears() -> None:
    f = Fixture()
    f.write("lib/__init__.py", "")
    f.write("tick.py", "from lib import benchmark\n")
    f.track("lib/__init__.py", "tick.py")
    f.write("lib/benchmark.py", "x = 1\n")
    before = f.guard()
    f.git("add", "--", "lib/benchmark.py")          # staged, NOT committed
    after = f.guard()
    ok("R1: violation before `git add`", before.returncode == 1, before.stdout)
    ok("R1: STAGING alone clears it — no commit needed, which is the documented remedy",
       after.returncode == 0, after.stdout)


def case_not_a_repo() -> None:
    f = Fixture(init=False)
    f.write("tick.py", "from lib import benchmark\n")
    r = f.guard()
    ok("R2: a non-git directory is a no-op, not a crash", r.returncode == 0,
       r.stdout + r.stderr)
    ok("R2: says why", "not a git repository" in r.stdout, r.stdout)


def case_rooted_paths() -> None:
    """D7 -- references that carry a prefix.

    Every reference that matters on the box is rooted somewhere: a systemd unit's absolute
    `/opt/quiver/...`, a shell script's `"$REPO_ROOT/..."`, a plain `./`. A left boundary that
    excluded `/` reported a clean tree for all three.
    """
    f = Fixture()
    f.write("quiver.service", "ExecStart=/usr/bin/python3 /opt/quiver/lib/runner.py\n")
    f.write("wrap.sh", 'exec "$REPO_ROOT/deploy/boot.sh" --now\n')
    f.write("local.sh", "source ./lib/helper.sh\n")
    f.track("quiver.service", "wrap.sh", "local.sh")
    f.write("lib/runner.py", "x = 1\n")
    f.write("deploy/boot.sh", "echo hi\n")
    f.write("lib/helper.sh", "echo hi\n")
    r = f.guard()
    ok("D7: absolute /opt/quiver/... reference is caught",
       "quiver.service:1" in r.stdout, r.stdout)
    ok("D7: $VAR-rooted reference is caught", "wrap.sh:1" in r.stdout, r.stdout)
    ok("D7: ./-rooted reference is caught", "local.sh:1" in r.stdout, r.stdout)
    ok("D7: exits 1", r.returncode == 1, r.stdout)


def case_nested_relative_specifier() -> None:
    """D8 -- `../run/retry.mjs` is as ordinary an ESM specifier as `./retry.mjs`."""
    f = Fixture()
    f.write("agent/decide.mjs", 'import { r } from "../run/retry.mjs";\n')
    f.track("agent/decide.mjs")
    f.write("run/retry.mjs", "export const r = 1;\n")
    r = f.guard()
    ok("D8: a nested `../run/retry.mjs` specifier is caught",
       r.returncode == 1 and "agent/decide.mjs:1" in r.stdout, r.stdout)


def case_relative_python_import() -> None:
    """D9 -- `from .availability import x` resolved against the file's own package."""
    f = Fixture()
    f.write("pkg/__init__.py", "")
    f.write("pkg/decide.py", "from .availability import probe\n")
    f.track("pkg/__init__.py", "pkg/decide.py")
    f.write("pkg/availability.py", "def probe():\n    return 1\n")
    r = f.guard()
    ok("D9: a relative python import of an untracked sibling is caught",
       r.returncode == 1 and "pkg/decide.py:1" in r.stdout, r.stdout)


def case_shadowed_module() -> None:
    """P9 -- an untracked file whose dotted name a TRACKED file already provides.

    `import lib.benchmark` resolves to the tracked `lib/benchmark/__init__.py`; the untracked
    `lib/benchmark.py` sitting beside it is not the dependency being satisfied.
    """
    f = Fixture()
    f.write("lib/__init__.py", "")
    f.write("lib/benchmark/__init__.py", "def window_return():\n    return 1\n")
    f.write("user.py", "import lib.benchmark\n")
    f.track("lib/__init__.py", "lib/benchmark/__init__.py", "user.py")
    f.write("lib/benchmark.py", "x = 1\n")
    r = f.guard()
    ok("P9: an import satisfied by a TRACKED module is not a missing dependency",
       r.returncode == 0, r.stdout)


def case_fails_closed() -> None:
    """R3 -- when git cannot answer, the guard must NOT say 'clean'.

    This is the one that protects the deploy gate. An earlier revision swallowed every git
    error and printed `PASS (no-op)`, exit 0 — so a corrupt index turned into a green light
    from `sync.sh`, which is the only path that updates the live box.
    """
    f = Fixture()
    f.write("a.py", "x = 1\n")
    f.track("a.py")
    (f.d / ".git" / "index").write_text("GARBAGE", encoding="utf-8")
    r = f.guard()
    ok("R3: a broken git index does NOT report a clean tree", r.returncode != 0,
       f"rc={r.returncode}\n{r.stdout}")
    ok("R3: it exits 2 — 'could not check', distinct from 1 'found a violation'",
       r.returncode == 2, f"rc={r.returncode}\n{r.stdout}{r.stderr}")
    ok("R3: and says why, without a traceback",
       "CANNOT CHECK" in r.stderr and "Traceback" not in r.stderr, r.stderr)


def case_optout_marker() -> None:
    """X1 -- the opt-out marker exempts only the file that carries it.

    An escape hatch that leaked would turn the guard into decoration, so this pins both
    directions: the marked file stops being a referrer, and an UNMARKED file referencing the
    same path is still caught in the same run.
    """
    marker = "check-tracked-refs" + ": ignore-file"
    f = Fixture()
    f.write("fixtures.py", f'"""Test data. {marker}"""\nP = "lib/runner.py"\n')
    f.write("real.sh", 'exec python3 lib/runner.py\n')
    f.track("fixtures.py", "real.sh")
    f.write("lib/runner.py", "x = 1\n")
    r = f.guard()
    # Violations print as `<file>:<line>`. The marked file DOES still appear in the output --
    # in the "opted out" disclosure line asserted below -- so a bare substring test would be
    # testing the wrong thing. Pin the line the reference is actually on.
    ok("X1: the marked file is not reported as a referrer",
       "fixtures.py:2" not in r.stdout, r.stdout)
    ok("X1: an unmarked file referencing the same path IS still reported",
       "real.sh:1" in r.stdout and r.returncode == 1, r.stdout)
    ok("X1: the exemption is disclosed in the output, not silent",
       "opted out" in r.stdout, r.stdout)


def case_rev_mode() -> None:
    """X2 -- `--rev HEAD` judges the COMMIT, which is what the box actually pulls.

    Both halves matter and they pull in opposite directions, so both are pinned here:
      * ordinary WIP (an untracked module + an UNCOMMITTED line importing it) must NOT block a
        re-deploy of a clean HEAD -- otherwise the operator learns to bypass the gate;
      * the same edit, once COMMITTED with `commit -am` (which never stages the new file), must
        block, because that commit is exactly what would reach the box.
    """
    f = Fixture()
    f.write("lib/__init__.py", "")
    f.write("tick.py", 'print("stable")\n')
    f.track("lib/__init__.py", "tick.py")
    f.write("lib/newthing.py", "x = 1\n")
    f.write("tick.py", 'print("stable")\nfrom lib import newthing\n')

    def guard(*extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([PY, str(GUARD), ".", *extra], cwd=f.d, env=f.env,
                              capture_output=True, text=True)

    wip_rev = guard("--rev", "HEAD")
    wip_tree = guard()
    ok("X2: uncommitted WIP does NOT block the deploy gate (--rev HEAD)",
       wip_rev.returncode == 0, wip_rev.stdout)
    ok("X2: but the working-tree check still warns locally",
       wip_tree.returncode == 1, wip_tree.stdout)

    # `commit -am` stages tracked edits only — lib/newthing.py stays behind. This is the defect.
    f.git("commit", "-qam", "wire it in, forget to add it")
    shipped = guard("--rev", "HEAD")
    ok("X2: once committed, the deploy gate BLOCKS", shipped.returncode == 1, shipped.stdout)
    ok("X2: and names the committed referrer",
       "tick.py:2" in shipped.stdout and "lib/newthing.py" in shipped.stdout, shipped.stdout)


def case_anti_vacuous() -> None:
    """V1 -- prove the guard inspected THIS fixture, not the quiver repo it lives in.

    If `check_tracked_refs.py` resolved its root from `__file__`, every case above would run
    against quiver, find it clean, and pass for the wrong reason. Two fixtures with two
    DIFFERENT violations must produce two different outputs, each naming its own file.
    """
    f1 = Fixture()
    f1.write("one.sh", 'X="alpha/uniq_one.py"\n')
    f1.track("one.sh")
    f1.write("alpha/uniq_one.py", "x = 1\n")
    r1 = f1.guard()

    f2 = Fixture()
    f2.write("two.sh", 'X="beta/uniq_two.py"\n')
    f2.track("two.sh")
    f2.write("beta/uniq_two.py", "x = 1\n")
    r2 = f2.guard()

    ok("V1: fixture A's output names A's file and not B's",
       "alpha/uniq_one.py" in r1.stdout and "beta/uniq_two.py" not in r1.stdout, r1.stdout)
    ok("V1: fixture B's output names B's file and not A's",
       "beta/uniq_two.py" in r2.stdout and "alpha/uniq_one.py" not in r2.stdout, r2.stdout)


def case_real_repo_smoke() -> None:
    """S1 -- the guard RUNS cleanly on the real repo, in both modes.

    Deliberately NOT "the real repo is clean". That is a real claim, but it belongs to the
    `tracked-refs` entry in run_e2e.sh, which reports it directly. Asserting it here too would
    make THIS suite — the one that proves the guard is correct — go red whenever the developer
    merely has work in progress, which is a failure about the tree, not about the guard.
    So: any decided verdict is acceptable; what must not happen is a crash or a can't-check.
    """
    for extra, name in (([], "working tree"), (["--rev", "HEAD"], "--rev HEAD")):
        r = subprocess.run([PY, str(GUARD), *extra], cwd=str(REPO),
                           capture_output=True, text=True)
        ok(f"S1: real repo, {name} — reaches a verdict (0 or 1), never 'cannot check'",
           r.returncode in (0, 1), f"rc={r.returncode}\n{r.stdout[-600:]}{r.stderr[-400:]}")
        ok(f"S1: real repo, {name} — no traceback",
           "Traceback" not in r.stderr, r.stderr[-600:])


def main() -> None:
    print("=== detection ===")
    case_clean()
    case_python_from_import()
    case_python_dotted_and_multiline()
    case_shell_suite_path()
    case_esm_relative()
    case_merge_conflict()
    case_rooted_paths()
    case_nested_relative_specifier()
    case_relative_python_import()
    print("=== precision ===")
    case_basename_collision()
    case_substring_path()
    case_non_code_extension()
    case_typescript_nodenext()
    case_unreferenced_scratch()
    case_specifier_is_directory_aware()
    case_stdlib_shadowing()
    case_rooted_token_only()
    case_systemd_unit_suspect()
    case_prose_mentions()
    case_real_string_literal_still_caught()
    case_gitignored()
    case_shadowed_module()
    print("=== remedy / environment ===")
    case_remedy_clears()
    case_not_a_repo()
    case_fails_closed()
    case_optout_marker()
    case_rev_mode()
    case_anti_vacuous()
    case_real_repo_smoke()


if __name__ == "__main__":
    try:
        main()
    finally:
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
