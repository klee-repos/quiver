#!/usr/bin/env python3
"""Repo hygiene gate: no TRACKED file may reference a file git does not carry.

THE DEFECT THIS CATCHES. `git commit -am` stages only files git already tracks. Write a new
module, wire it into an existing one, `commit -am`, push -- and the reference ships while the
file stays on your laptop. `deploy/gcp/sync.sh` then pulls that commit onto the box, where the
import fails. It is silent in the worst way: `tick.py`'s benchmark import sits inside a function
and every tick wraps that call in `except Exception ... never blocks a tick`, so the box keeps
trading and simply stops measuring alpha. The offline healthcheck never sees it either -- it
probes a hand-maintained list of module names and never imports `tick`.

WHY IT ANCHORS ON THE UNTRACKED SET. The tempting design is the other direction: resolve every
import in the repo and check the target exists. Measured on this repo that yields ~80 false
alarms for one real hit -- `quiver_eve/agent/validate.ts` imports `./agent.js`, which is correct
TypeScript NodeNext for a file named `agent.ts`, and no resolver-shaped rule distinguishes that
from a genuine miss without a compiler. So this runs the search backwards. It starts from
`git ls-files --others --exclude-standard` -- files that exist, are NOT tracked, and are NOT
gitignored -- and only then asks whether anything tracked names them. That set is empty in a
clean tree, which makes the normal path one git call, and everything git deliberately ignores
(state/, logs/, .venv/, strategy.yaml, the secrets) is excluded for free rather than by a list
this file would have to maintain.

Two narrowings keep it quiet enough to survive. Only code-ish suffixes count: the failure being
prevented is "the box pulls code that will not load", so a stray note or a provisioning marker
like the box's own untracked `.gcp-provisioned` -- which tracked terraform legitimately
references -- is not this defect. And a bare filename only counts when it appears in path
position (after a quote or a slash), so prose that happens to mention a filename is not a hit.

WHAT IT DELIBERATELY DOES NOT CATCH, so the green is not read as wider than it is:

  * a reference to a TRACKED path that was deleted from the working tree (a different defect,
    and a loud one -- `git status` shows it as ` D` and the suite fails immediately);
  * `importlib` / dynamic / f-string-built paths;
  * references made FROM files git does not track;
  * a file that is required at runtime but deliberately GITIGNORED -- `strategy.yaml`,
    `deploy/runner/runner.env` and friends. Those never enter the suspect set by design; the
    box gets them from Secret Manager or `setup.sh`, not from a pull, so a missing one is a
    provisioning question rather than a commit that forgot a file;
  * non-source data the box loads by path (a `.json`/`.yaml`/`.service` that is untracked).
    `CODE_SUFFIXES` is intentionally narrow: widening it trades a rare miss for a common false
    positive, and a gate that blocks a legitimate deploy gets bypassed by reflex and then
    protects nothing. Widen it only with a precision case to match.

Exit 0 means "nothing untracked is referenced", never "every path in the repo resolves".
Exit 1 means a reference was found. Exit 2 means the check could NOT run -- deliberately
distinct, because "I could not look" must never read as "I looked and it is fine".
"""

from __future__ import annotations

import argparse
import ast
import importlib.machinery
import posixpath
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

# The defect is "the box pulls code that will not load", so only importable/executable sources
# count. This is what keeps the box's own untracked-but-not-ignored `.gcp-provisioned` (named by
# deploy/gcp/terraform/main.tf) from being reported as a missing file.
CODE_SUFFIXES = {".py", ".mjs", ".js", ".cjs", ".ts", ".tsx", ".sh", ".bash",
                 # systemd units: a tracked script can install a unit that git never
                 # carried, and the timer then simply never exists on the box.
                 ".service", ".timer"}

# Referrers that cannot break a tick. A README naming a file that is not in git is a stale doc,
# not a deploy hazard -- and treating it as one would block `sync.sh` over a prose sentence.
DOC_SUFFIXES = {".md", ".rst", ".adoc", ".txt"}

# Opt-out markers, for a tracked file that legitimately contains path-shaped strings it does not
# depend on -- a test that WRITES fixture files by name is the motivating case, and this file's
# own test is one: it names `lib/runner.py`, `deploy/boot.sh` and friends purely as fixture data,
# so any scratch file that happened to share those names would block a deploy.
# Assembled from fragments so this module's own source does not contain the literal markers and
# therefore cannot silently exempt itself.
_IGNORE_FILE = "check-tracked-refs" + ": ignore-file"   # whole file is never a referrer
_IGNORE_LINE = "check-tracked-refs" + ": ignore"        # this one line is never a referrer

# Where a line's comment starts, per language. A path inside a comment is a mention, not a
# dependency: nothing imports it, so nothing breaks when git does not carry it.
_COMMENT_MARKERS = {
    ".py": ("#",), ".sh": ("#",), ".bash": ("#",), ".yaml": ("#",), ".yml": ("#",),
    ".toml": ("#",), ".cfg": ("#",), ".ini": ("#",), ".tf": ("#", "//"),
    ".js": ("//",), ".mjs": ("//",), ".cjs": ("//",), ".ts": ("//",), ".tsx": ("//",),
}


class GitUnavailable(RuntimeError):
    """git could not answer a question this check depends on."""


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True)


def _git_ok(root: str, *args: str) -> str:
    """Run git and REQUIRE it to succeed.

    Fail CLOSED. An earlier revision returned an empty list whenever git errored, which turned a
    corrupt index -- or any other git failure -- into `PASS (no-op)`, exit 0, and a green light
    from the `sync.sh` deploy gate. A gate in front of a live trading box that answers "all
    clear" when it could not look is worse than no gate: it converts an unknown into a
    reassurance. Anything that is not a clean git answer must reach the operator.
    """
    r = _git(root, *args)
    if r.returncode != 0:
        raise GitUnavailable(f"`git {' '.join(args)}` failed ({r.returncode}): "
                             f"{r.stderr.strip() or 'no stderr'}")
    return r.stdout


def repo_root(start: str) -> str | None:
    r = _git(start, "rev-parse", "--show-toplevel")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def untracked_code(root: str) -> list[str]:
    """Files that exist, are not tracked, are not ignored, and are source."""
    out = _git_ok(root, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(p for p in out.split("\0")
                  if p and PurePosixPath(p).suffix in CODE_SUFFIXES)


class Source:
    """The set of tracked files to search -- the working tree, or a specific commit.

    WHY BOTH EXIST. The two call sites ask genuinely different questions.

    `tests/run_e2e.sh` asks about the WORKING TREE: "is what I am editing right now
    self-consistent?" That is the earliest useful warning, while the fix is still one
    `git add` away.

    `deploy/gcp/sync.sh` must ask about the COMMIT, because a commit is what the box pulls --
    `sync.sh` pushes and `update.sh` fast-forwards; neither ever sees your working tree. Asking
    the working-tree question there is not merely imprecise, it is wrong in the common case:
    an ordinary in-progress edit (a new untracked module plus the uncommitted line that imports
    it) would block a re-deploy of a HEAD that is perfectly consistent. Measured on a throwaway
    repo: working-tree check exit 1, HEAD check exit 0, same instant. A gate that blocks
    legitimate deploys teaches the operator to set the bypass variable by reflex, and then it
    is protecting nothing at all.

    The SUSPECT set stays the same either way -- files that exist on disk, untracked and
    unignored. That is what keeps the commit-side question precise: "does the commit I am about
    to ship name a file that is sitting on my laptop instead of in git?"
    """

    def __init__(self, root: str, rev: str | None = None) -> None:
        self.root = root
        self.rev = rev

    def label(self) -> str:
        return f"commit {self.rev}" if self.rev else "the working tree"

    def files(self) -> list[str]:
        if self.rev:
            out = _git_ok(self.root, "ls-tree", "-r", "--name-only", "-z", self.rev)
        else:
            out = _git_ok(self.root, "ls-files", "-z")
        return [p for p in out.split("\0") if p]

    def read(self, rel: str) -> str | None:
        if self.rev:
            r = _git(self.root, "show", f"{self.rev}:{rel}")
            return r.stdout if r.returncode == 0 else None
        try:
            return (Path(self.root) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def grep(self, needle: str) -> list[tuple[str, int, str]]:
        """Fixed-string search over the tracked files of this source.

        Fixed-string only, deliberately. git's ERE is the platform's, and BSD/macOS does not
        implement `\\b` -- a word-boundary pattern there matches NOTHING and git still exits 1,
        so the guard would report a clean tree while silently testing nothing (observed). Any
        boundary rule therefore belongs in Python, where the dialect is known.
        """
        args = ["grep", "-n", "-I", "--no-color", "-F", "-e", needle]
        if self.rev:
            args.append(self.rev)
        r = _git(self.root, *args)
        if r.returncode not in (0, 1):        # 1 = no match; anything else is a real error
            raise GitUnavailable(f"`git grep` failed ({r.returncode}) searching for {needle!r}: "
                                 f"{r.stderr.strip() or 'no stderr'}")
        out = []
        for line in r.stdout.splitlines():
            rest = line
            if self.rev:                       # `git grep <rev>` prefixes each hit with `<rev>:`
                if not rest.startswith(self.rev + ":"):
                    continue
                rest = rest[len(self.rev) + 1:]
            f, _, tail = rest.partition(":")
            n, _, text = tail.partition(":")
            if n.isdigit():
                out.append((f, int(n), text.strip()))
        return out

    def grep_files(self, needle: str) -> set[str]:
        return {f for f, _, _ in self.grep(needle)}


def module_of(rel: str) -> str | None:
    """The dotted module a repo-relative .py path would be imported as."""
    p = PurePosixPath(rel)
    if p.suffix != ".py":
        return None
    parts = p.parent.parts if p.name == "__init__.py" else (*p.parent.parts, p.stem)
    return ".".join(parts) if parts else None


def satisfied_outside_repo(root: str, dotted: str) -> bool:
    """True when `dotted`'s top-level name is already provided by the stdlib or a real package.

    A scratch `json.py` or `numpy.py` left at the repo root has the dotted name `json`/`numpy`,
    and every tracked `import json` then looks like a reference to it. Measured on this repo:
    one throwaway file produced 52 bogus referrers and blocked the deploy gate. It is not a
    dependency at all -- `import json` resolved to the stdlib before the file existed and will
    resolve to the stdlib on the box, which never receives it.

    The probe deliberately excludes the repo itself from the search path; otherwise the shadow
    file answers the question about itself and the false positive survives the fix.
    """
    top = dotted.split(".")[0]
    if top in getattr(sys, "stdlib_module_names", frozenset()):
        return True
    try:
        rootp = Path(root).resolve()
        elsewhere = [p for p in sys.path
                     if p and Path(p).resolve() != rootp]
        spec = importlib.machinery.PathFinder.find_spec(top, elsewhere)
    except (ImportError, ValueError, OSError, AttributeError):
        return False
    return spec is not None


def _package_of(rel: str) -> tuple[str, ...]:
    """The dotted package a repo-relative .py file lives in."""
    p = PurePosixPath(rel)
    return p.parent.parts if p.name != "__init__.py" else p.parent.parts


def _imported_modules(rel: str, src: str) -> dict[str, int]:
    """Every dotted module `rel` imports -> the first line that imports it.

    Parsed, not grepped. `tick.py` spells its dependency `from lib import benchmark as
    benchmark_lib` -- a form containing neither the path `lib/benchmark.py` nor the dotted name
    `lib.benchmark`, so every text pattern misses it. The AST also covers the parenthesized
    multi-line form and aliases without needing a regex per spelling.

    Three things are folded in so the caller can do a single exact lookup:
      * `from PKG import NAME` also yields `PKG.NAME`, which is the tick.py shape;
      * every dotted PREFIX is yielded too, because `import lib.a.b` genuinely depends on
        `lib/__init__.py` and `lib/a/__init__.py`;
      * relative imports are resolved against the file's own package, so `from .availability
        import x` inside `quiver_eve/run/` is recorded as `quiver_eve.run.availability`
        rather than skipped -- skipping them made an entire import style invisible.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    pkg = _package_of(rel)
    out: dict[str, int] = {}

    def note(dotted: str, line: int) -> None:
        parts = [p for p in dotted.split(".") if p]
        for i in range(1, len(parts) + 1):
            out.setdefault(".".join(parts[:i]), line)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                note(a.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from .x import y` / `from ..x import y` -- resolve against this file's package.
                base = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                if len(pkg) < node.level - 1:
                    continue                     # escapes the repo root; not our business
                mod = ".".join((*base, *(node.module.split(".") if node.module else ())))
            else:
                mod = node.module or ""
            if not mod:
                continue
            note(mod, node.lineno)
            for a in node.names:
                if a.name != "*":
                    note(f"{mod}.{a.name}", node.lineno)
    return out


def _in_comment(text: str, needle: str, suffix: str) -> bool:
    """True when `needle` sits after this language's comment marker on the line."""
    i = text.find(needle)
    if i < 0:
        return False
    for marker in _COMMENT_MARKERS.get(suffix, ("#", "//")):
        j = text.find(marker)
        if 0 <= j < i:
            return True
    return False


def _docstring_lines(src: "Source", rel: str) -> set[int]:
    """Line numbers covered by docstrings in a tracked .py file.

    A module that EXPLAINS a path -- this file's own docstring names `lib/benchmark.py` as the
    worked example -- is not depending on it. Without this, the guard reports itself as a
    referrer, and a file mentioned ONLY in prose would block a deploy outright.
    """
    try:
        text = src.read(rel)
        if text is None:
            return set()
        tree = ast.parse(text)
    except (OSError, SyntaxError):
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _whole_path(text: str, path: str) -> bool:
    """True when `path` appears in `text` as a whole path, not inside a longer one.

    `git grep -F` matches substrings, so a suspect `lib/x.py` would otherwise be "found" inside
    a tracked mention of `sublib/x.py`, and `benchmark.py` inside `test_e2e_benchmark.py`.
    Both ends must fall on a path boundary.

    A leading `/` is allowed only when the WHOLE path token it belongs to is rooted -- it starts
    with `/`, `~`, `$VAR`, or `./`. That keeps the references that matter on this repo (a
    systemd unit's `/opt/quiver/tick.py`, a shell script's `"$REPO_ROOT/deploy/gcp/x.sh"`, a
    plain `./lib/x.py`), which an earlier revision dropped silently while still reporting a
    clean tree. And it rejects the residual that admitting any `/` let in: a tracked mention of
    a genuinely different `vendor/lib/x.py` no longer blames an untracked `lib/x.py`, because
    that token's head is a bare directory name rather than a root.
    """
    for m in re.finditer(rf"(?<![\w.\-]){re.escape(path)}(?![\w.\-/])", text):
        i = m.start()
        if i == 0 or text[i - 1] != "/":
            return True                       # the token starts at the match itself
        j = i - 1                             # walk back over the rest of the path token
        while j > 0 and text[j - 1] in "/.-_~${}" or (j > 0 and text[j - 1].isalnum()):
            j -= 1
        if text[j] in "/~$." :                # rooted: /abs, ~/home, $VAR/, ./rel
            return True
    return False


_SPECIFIER = re.compile(r"(?:\.{1,2}/)(?:[\w.\-]+/)*[\w.\-]+")


def _relative_targets(text: str, referrer: str) -> set[str]:
    """Every `./x` / `../y/z` specifier in `text`, RESOLVED against the referrer's directory.

    Requiring an explicit `./` or `../` is what makes a bare-filename rule safe at all: a plain
    "contains the basename" test is a substring match on an ordinary word, and on this repo
    `analyze.py` appears in 52 tracked files, so that rule would emit ~50 bogus hits for one
    stray scratch copy and get itself deleted.

    But the prefix alone is not enough, because a specifier is relative to the file it appears
    IN. Matching `./agent.js` against any suspect merely NAMED agent.js is directory-blind, and
    that misfires on real repo content: an untracked `vendor/sdk/agent.js` was proven to make
    `quiver_eve/agent/validate.ts` report a violation, even though that import resolves to
    `quiver_eve/agent/agent.ts` and has nothing to do with the stray file. So resolve the
    specifier to a repo-relative path and demand an exact match. This is also what lets the
    rule stay useful: `../run/retry.mjs` from `quiver_eve/agent/` resolves correctly instead of
    being either missed or matched by coincidence.
    """
    parent = PurePosixPath(referrer).parent
    out = set()
    for m in _SPECIFIER.finditer(text):
        out.add(posixpath.normpath(str(parent / m.group(0))).lstrip("./") or m.group(0))
    return out


def exempt_referrers(src: "Source") -> set[str]:
    """Tracked files that opted out of being treated as referrers (see _IGNORE_FILE).

    Reported in the summary rather than applied silently: an exemption is a hole in the check,
    and a hole nobody can see is how a guard rots into decoration.
    """
    return src.grep_files(_IGNORE_FILE)


def violations(src: "Source", suspects: list[str]) -> list[dict]:
    tracked_py = [f for f in src.files() if f.endswith(".py")]
    found: list[dict] = []
    seen: set[tuple[str, int, str]] = set()

    def add(referrer: str, line: int, target: str, how: str, text: str) -> None:
        key = (referrer, line, target)
        if key not in seen:
            seen.add(key)
            found.append({"referrer": referrer, "line": line, "target": target,
                          "how": how, "text": text[:160]})

    _doc_cache: dict[str, set[int]] = {}
    exempt_files = exempt_referrers(src)

    def is_prose(referrer: str, line: int, text: str, needle: str) -> bool:
        """A mention that cannot make anything fail to load. See DOC_SUFFIXES/_in_comment."""
        if referrer in exempt_files or _IGNORE_LINE in text:
            return True
        suffix = PurePosixPath(referrer).suffix
        if suffix in DOC_SUFFIXES or _in_comment(text, needle, suffix):
            return True
        if suffix == ".py":
            if referrer not in _doc_cache:
                _doc_cache[referrer] = _docstring_lines(src, referrer)
            return line in _doc_cache[referrer]
        return False

    # Every tracked .py is parsed ONCE, not once per suspect. The per-suspect loop it replaces
    # was O(suspects x tracked files): measured at ~10s on a 400-file tree with a handful of
    # suspects, which is slow enough that an operator starts skipping the gate.
    import_index: dict[str, list[tuple[str, int, str]]] = {}
    module_owner: set[str] = set()               # dotted modules a TRACKED file already provides
    if any(module_of(u) for u in suspects):
        for f in tracked_py:
            owner = module_of(f)
            if owner:
                module_owner.add(owner)
            text = src.read(f)
            if text is None:
                continue
            lines = text.splitlines()
            for mod, line in _imported_modules(f, text).items():
                text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
                import_index.setdefault(mod, []).append((f, line, text))

    for u in suspects:
        # 1. The repo-relative path, verbatim. Catches tests/run_e2e.sh's SUITES entries and any
        #    shell/config that names a file by its path.
        for f, n, t in src.grep(u):
            if _whole_path(t, u) and not is_prose(f, n, t, u):
                add(f, n, u, "path reference", t)

        # 2. Python imports, resolved by AST rather than by pattern (see _imported_modules).
        mod = module_of(u)
        if mod:
            # A dotted name a TRACKED file already provides resolves to that file, not to this
            # untracked one -- e.g. an untracked `lib/benchmark.py` shadowed by a tracked
            # `lib/benchmark/__init__.py`. Importing it is then not a missing dependency.
            if mod not in module_owner and not satisfied_outside_repo(src.root, mod):
                for f, n, text in import_index.get(mod, []):
                    if not is_prose(f, n, text, mod):
                        add(f, n, u, f"imports {mod}", text)
        else:
            # 3. Non-Python: an explicit RELATIVE specifier. This is what catches ESM's
            #    `import x from "./availability.mjs"`, whose text contains neither the
            #    repo-relative path nor a dotted module. git greps the cheap fixed string;
            #    Python applies the `./`-prefix rule that keeps ordinary filenames out.
            # git greps the cheap fixed basename; Python then resolves each specifier against
            # the referrer's own directory and demands it point AT this suspect.
            base = PurePosixPath(u).name
            for f, n, t in src.grep(base):
                if u in _relative_targets(t, f) and not is_prose(f, n, t, base):
                    add(f, n, u, "relative import specifier", t)
    return found


def main(argv: list[str]) -> int:
    """Exit 0 = clean, 1 = a tracked file references an untracked source file, 2 = COULD NOT
    CHECK. The third code is deliberate: `sync.sh` must be able to tell "I looked and it is
    broken" from "I could not look", because the second is not a reason to deploy."""
    try:
        return _run(argv)
    except GitUnavailable as e:
        print(f"CANNOT CHECK — {e}", file=sys.stderr)
        print("Refusing to report a clean tree from an incomplete answer.", file=sys.stderr)
        print("0 checks passed, 1 failed")
        return 2


def _run(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Fail if a tracked file references a source file git is not carrying.")
    ap.add_argument("path", nargs="?", default=".", help="anywhere inside the repo")
    ap.add_argument("--rev", metavar="REF",
                    help="search the files in REF (e.g. HEAD) instead of the working tree. "
                         "Use this on the DEPLOY path: a commit is what the box pulls, so an "
                         "in-progress edit must not block a re-deploy of a clean HEAD.")
    args = ap.parse_args(argv[1:])

    root = repo_root(args.path)
    if root is None:
        print(f"{args.path}: not a git repository — nothing to check")
        print("PASS (no-op)")
        return 0

    # The suspect set is the working tree either way: the question is always "does the code
    # being shipped name a file that is sitting on this disk instead of in git?"
    suspects = untracked_code(root)
    if not suspects:
        print("no untracked-and-unignored source files — nothing can be referenced")
        print("PASS (no-op)")
        return 0

    src = Source(root, args.rev)
    print(f"searching {src.label()} for references to "
          f"{len(suspects)} untracked, unignored source file(s):")
    for u in suspects:
        print(f"  ? {u}")
    for x in sorted(exempt_referrers(src)):
        print(f"  (not searched as a referrer, opted out: {x})")
    bad = violations(src, suspects)
    checked = len(suspects)
    if not bad:
        print(f"\nnone of them is referenced by {src.label()}")
        print(f"{checked} checks passed, 0 failed")
        return 0

    culprits = sorted({v["target"] for v in bad})
    where = f"{src.label().upper()} REFERENCES" if src.rev else "TRACKED CODE REFERENCES"
    print(f"\n{where} UNTRACKED FILES — what git carries would not include them:")
    for v in bad:
        print(f"  {v['referrer']}:{v['line']}  ->  {v['target']}   [{v['how']}]")
        print(f"      {v['text']}")
    print("\nFix each one, then re-run:")
    for c in culprits:
        print(f"  rm {c}   /   git add {c}")
    print("  Delete the file (or the reference) if it is a stray; `git add` it if it ships.")
    print(f"\n{checked - len(culprits)} checks passed, {len(culprits)} failed")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
