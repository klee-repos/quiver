#!/usr/bin/env python3
"""The desk tool set ``lib/desk/tools.py`` (plan §5.4, §13.3, §14.10 W3).

Static cases parse the source: the registry against the §5.4 tables, the W3 name list, the
``broker_jobs`` kinds a tool can reach, and the import closure and call graph that must not
reach ``approvals``, ``execution`` or ``proposals.apply_change``. The smoke case calls each
tool through ``tools.call`` on an empty ledger with fakes for every network seam.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_fakes import REPO, FakeClock, FakeConn, FakeOpener, FakePrices, Results, seams, temp_root  # noqa: E402

from lib.desk import agent, outbox, store, tools  # noqa: E402

r = Results("desk-tools-static")
TOOLS_PY = REPO / "lib" / "desk" / "tools.py"
OWNER = "987654321"

# §5.4, written out from the plan tables.
READ = ["portfolio_view", "refresh_portfolio", "get_quotes", "get_book", "list_companies", "get_company",
        "search_research", "list_filings", "get_filing_sections", "get_document", "get_financials", "get_metrics",
        "run_valuation", "get_technicals", "get_transcript", "get_etf_lookthrough", "get_events", "get_proposals",
        "get_track_record", "get_attribution", "get_settings", "ledger_query", "screen_companies"]
D_WRITE = ["react", "say", "load_company", "fetch_document", "record_finding", "add_note", "set_research_status",
           "log_news_event", "start_deep_dive", "withdraw_proposal", "propose_trade", "propose_change",
           "bring_up_next", "close_item", "defer_item", "hold_agenda", "show_card"]
A_WRITE = ["report", "load_company", "fetch_document", "record_finding", "add_note", "log_news_event",
           "record_verdict", "withdraw_proposal", "propose_trade", "propose_change"]
W3_NAMES = {"approvals", "execution", "handle_tap", "apply_change", "subprocess", "Popen", "shutil",
            "write_text", "write_bytes",
            # A dynamic import can load lib.desk.execution by a built string, so W3 refuses it.
            "importlib", "import_module", "__import__"}


# --- module map --------------------------------------------------------------------

def mod_path(name: str):
    """The repo file of a dotted module name, or None for a module outside the repo."""
    p = REPO.joinpath(*name.split("."))
    if p.with_suffix(".py").is_file():
        return p.with_suffix(".py")
    if (p / "__init__.py").is_file():
        return p / "__init__.py"
    return None


def mod_name(path: Path) -> str:
    rel = path.relative_to(REPO).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


_TREES = {}


def tree_of(name: str):
    if name not in _TREES:
        _TREES[name] = ast.parse(mod_path(name).read_text(encoding="utf-8"))
    return _TREES[name]


def imports_of(name: str) -> dict:
    """Every import anywhere in a repo module: {local alias: target}. A target is a module
    name, or ``module:attr`` for a name imported from a module."""
    out = {}
    for node in ast.walk(tree_of(name)):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                out.setdefault("__mods__", set()).add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                full = f"{node.module}.{a.name}"
                if mod_path(full) is not None:
                    out[a.asname or a.name] = full
                    out.setdefault("__mods__", set()).add(full)
                else:
                    out[a.asname or a.name] = f"{node.module}:{a.name}"
                    out.setdefault("__mods__", set()).add(node.module)
    return out


def repo_imports(name: str) -> set:
    return {m for m in imports_of(name).get("__mods__", set()) if mod_path(m) is not None}


# --- registry ----------------------------------------------------------------------

def case_registry():
    r.eq("registry: the D name set equals the §5.4 tables", set(tools.role_tool_names("desk")), set(READ + D_WRITE))
    r.eq("registry: the A name set equals the §5.4 tables", set(tools.role_tool_names("analyst")), set(READ + A_WRITE))
    r.eq("registry: no name twice in a role", (len(tools.role_tool_names("desk")), len(tools.role_tool_names("analyst"))),
         (len(set(READ + D_WRITE)), len(set(READ + A_WRITE))))
    r.eq("registry: the schemas name exactly the union", set(tools.SPECS), set(READ + D_WRITE + A_WRITE))
    r.eq("registry: one function t_<name> per tool", {n: f.__name__ for n, f in tools.FUNCS.items()},
         {n: f"t_{n}" for n in READ + D_WRITE + A_WRITE})
    bad = [n for n, (_d, s) in tools.SPECS.items() if s.get("type") != "object" or s.get("additionalProperties") is not False]
    r.eq("registry: every input schema is a closed object", bad, [])
    for name in ("approve", "approve_proposal", "execute", "place_order", "cancel_order", "run_shell", "write_file"):
        r.ok(f"registry: no tool named {name}", name not in tools.SPECS)
    r.raises("registry: an unknown role is refused", lambda: tools.role_tool_names("broker"), ValueError)


# --- no_refs (W3) ------------------------------------------------------------------

def _open_mode(call: ast.Call):
    """The mode of an ``open`` call: a string, None (no mode = read), or '?' (not a constant)."""
    mode = None
    if len(call.args) >= 2:
        mode = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return None
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return mode.value
    return "?"


def w3_hits(src: str) -> list:
    """The W3 items (§14.10) in a source text: an import, or an identifier in any function,
    from the W3 list; ``os.system``/``os.popen``; an ``open()`` whose mode writes."""
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if set(a.name.split(".")) & W3_NAMES or (a.asname and a.asname in W3_NAMES):
                    hits.append(f"import {a.name} (line {node.lineno})")
        elif isinstance(node, ast.ImportFrom):
            mods = set((node.module or "").split("."))
            names = {a.name for a in node.names} | {a.asname for a in node.names if a.asname}
            if mods & W3_NAMES or names & W3_NAMES or ({"system", "popen"} & names and "os" in mods):
                hits.append(f"from {node.module} import {sorted(names)} (line {node.lineno})")
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id in W3_NAMES:
                hits.append(f"{fn.name}: name {node.id} (line {node.lineno})")
            elif isinstance(node, ast.Attribute):
                if node.attr in W3_NAMES:
                    hits.append(f"{fn.name}: attribute .{node.attr} (line {node.lineno})")
                if node.attr in ("system", "popen") and isinstance(node.value, ast.Name) and node.value.id == "os":
                    hits.append(f"{fn.name}: os.{node.attr} (line {node.lineno})")
            elif isinstance(node, ast.Call):
                f = node.func
                is_open = (isinstance(f, ast.Name) and f.id == "open") or (isinstance(f, ast.Attribute) and f.attr == "open")
                if is_open:
                    m = _open_mode(node)
                    if m is not None and (m == "?" or set(m) & set("wax+")):
                        hits.append(f"{fn.name}: open(mode={m!r}) (line {node.lineno})")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in W3_NAMES:
                hits.append(f"{fn.name}: string {node.value!r} (line {node.lineno})")
    return sorted(set(hits))


def case_no_refs():
    r.eq("no_refs: tools.py has no W3 import, identifier or write-mode open()",
         w3_hits(TOOLS_PY.read_text(encoding="utf-8")), [])
    # The scan finds each W3 item when a source holds it.
    planted = {
        "approvals": "def f():\n    from lib.desk import approvals\n",
        "execution": "def f(c):\n    return execution.exec_check(c)\n",
        "handle_tap": "def f(a):\n    return a.handle_tap()\n",
        "apply_change": "def f(p):\n    return p.apply_change()\n",
        "subprocess": "import subprocess\n",
        "os.system": "import os\ndef f():\n    os.system('x')\n",
        "os.popen": "import os\ndef f():\n    os.popen('x')\n",
        "Popen": "def f(s):\n    return s.Popen(['x'])\n",
        "shutil": "def f():\n    import shutil\n",
        "write_text": "def f(p):\n    p.write_text('x')\n",
        "write_bytes": "def f(p):\n    p.write_bytes(b'x')\n",
        "open w": "def f():\n    open('x', 'w')\n",
        "open a+": "def f(p):\n    p.open(mode='a+')\n",
        "open x": "def f():\n    open('x', mode='x')\n",
        "import importlib": "import importlib\n",
        "from importlib import": "def f():\n    from importlib import import_module\n",
        "importlib.import_module": "def f(m):\n    return m.import_module('lib.desk.' + 'exec' + 'ution')\n",
        "__import__": "def f():\n    return __import__('lib.desk.' + 'exec' + 'ution')\n",
    }
    for label, src in planted.items():
        r.ok(f"no_refs: the W3 scan catches {label}", len(w3_hits(src)) >= 1, w3_hits(src))
    r.eq("no_refs: the W3 scan passes a read-only open()", w3_hits("def f():\n    open('x').read()\n"), [])


# --- call graph --------------------------------------------------------------------

class Graph:
    """A static call graph over repo modules: a node is ``module:function`` (or
    ``module:Class.method``). A call resolves through the module's imports."""

    def __init__(self):
        self.funcs = {}      # node -> ast function
        self.loaded = set()

    def load(self, mod: str):
        if mod in self.loaded or mod_path(mod) is None:
            return
        self.loaded.add(mod)
        for node in tree_of(mod).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[f"{mod}:{node.name}"] = node
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.funcs[f"{mod}:{node.name}.{sub.name}"] = sub

    def callees(self, node_id: str) -> set:
        mod, _fn = node_id.split(":", 1)
        imp = imports_of(mod)
        cls = _fn.split(".")[0] if "." in _fn else None
        out = set()
        for n in ast.walk(self.funcs[node_id]):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            targets = []
            if isinstance(f, ast.Name):
                if f"{mod}:{f.id}" in self.funcs or mod_path(mod) and any(
                        isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and x.name == f.id
                        for x in tree_of(mod).body):
                    targets.append(f"{mod}:{f.id}")
                elif f.id in imp and ":" in imp[f.id]:
                    m2, a2 = imp[f.id].split(":")
                    targets.append(f"{m2}:{a2}")
                elif f.id in imp and mod_path(imp[f.id]) is not None:
                    pass
            elif isinstance(f, ast.Attribute):
                base = f.value
                if isinstance(base, ast.Name) and base.id in imp and ":" not in imp[base.id] \
                        and mod_path(imp[base.id]) is not None:
                    targets.append(f"{imp[base.id]}:{f.attr}")
                elif isinstance(base, ast.Name) and base.id in ("cls", "self") and cls:
                    targets.append(f"{mod}:{cls}.{f.attr}")
            for t in targets:
                tm = t.split(":")[0]
                self.load(tm)
                if t in self.funcs:
                    out.add(t)
                elif f"{t}.__init__" in self.funcs:
                    out.add(f"{t}.__init__")
        # A function passed as an argument (``asyncio.to_thread(call, ...)``) or a value
        # in a table (``FUNCS``) also counts as reached.
        for n in ast.walk(self.funcs[node_id]):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and f"{mod}:{n.id}" in self.funcs:
                out.add(f"{mod}:{n.id}")
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in imp \
                    and ":" not in imp[n.value.id] and mod_path(imp[n.value.id]) is not None:
                t = f"{imp[n.value.id]}:{n.attr}"
                self.load(imp[n.value.id])
                if t in self.funcs:
                    out.add(t)
        return out

    def reach(self, starts) -> set:
        seen, todo = set(), list(starts)
        while todo:
            n = todo.pop()
            if n in seen or n not in self.funcs:
                continue
            seen.add(n)
            todo.extend(self.callees(n) - seen)
        return seen


def tool_graph():
    g = Graph()
    g.load("lib.desk.tools")
    starts = [k for k in g.funcs if k.startswith("lib.desk.tools:")]
    return g, g.reach(starts)


def strings_of(fn) -> list:
    return [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


_INS = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+broker_jobs\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", re.I | re.S)


def case_jobs_kind():
    g, reached = tool_graph()
    r.ok("jobs_kind: the graph reaches store.enqueue_snapshot_job (from refresh_portfolio)",
         "lib.desk.store:enqueue_snapshot_job" in reached, sorted(x for x in reached if "store" in x)[:20])
    r.ok("jobs_kind: the graph reaches proposals.build_trade (the scan is not empty)",
         "lib.desk.proposals:build_trade" in reached)
    found, bad = [], []
    for node in sorted(reached):
        for s in strings_of(g.funcs[node]):
            if re.search(r"INTO\s+broker_jobs", s, re.I):
                m = _INS.search(s)
                if not m:
                    bad.append((node, s[:120], "no column list"))
                    continue
                cols = [c.strip() for c in m.group(1).split(",")]
                vals = [v.strip() for v in m.group(2).split(",")]
                kind = vals[cols.index("kind")] if "kind" in cols and len(vals) == len(cols) else None
                found.append((node, kind))
                if kind != "'snapshot'":
                    bad.append((node, s[:120], kind))
    r.eq("jobs_kind: each broker_jobs INSERT a tool can reach uses kind 'snapshot'", bad, [])
    r.eq("jobs_kind: the reachable broker_jobs INSERTs", found, [("lib.desk.store:enqueue_snapshot_job", "'snapshot'")])
    # The textual rule of W3 on tools.py itself.
    src = TOOLS_PY.read_text(encoding="utf-8")
    r.ok("jobs_kind: tools.py itself holds no INSERT INTO broker_jobs with another kind",
         all("'snapshot'" in m.group(2) for m in _INS.finditer(src)), [m.group(0) for m in _INS.finditer(src)])


# --- state_writes (W3) --------------------------------------------------------------
# W3 refuses the approval and execution modules by name. This rule reads the SQL text of
# every function a tool can reach, so a new statement that approves a proposal, checks or
# reviews a leg, or changes a broker job fails even when it imports nothing.

GUARDED = {
    "proposals": {"state", "decided_by", "decided_at", "nonce", "kind", "payload_json", "payload_sha256",
                  "expires_at"},
    "orders": {"state", "review_ok", "review_json", "reviewed_at", "checked_at", "check_quote", "submitted_at",
               "broker_order_id", "dollar_amount", "quantity", "trade_date", "proposal_id", "ref_id", "side",
               "ticker", "type"},
    "broker_jobs": None,     # every column
}
# The INSERT value that each guarded table must get for its state column.
INSERT_STATE = {"proposals": ("state", "'pending'"), "orders": ("state", "'proposed'"),
                "broker_jobs": ("kind", "'snapshot'")}
# The state writes a tool may reach, item by item, checked both ways: a new one fails, and
# a grant that no reachable function holds fails too, so the list cannot rot.
STATE_WRITE_GRANTS = {
    ("lib.desk.proposals:end_pending",
     "UPDATE proposals SET state=?, superseded_by=COALESCE(?, superseded_by) WHERE id=? AND state='pending'"),
    ("lib.desk.proposals:end_pending",
     "UPDATE orders SET state=?, finalized=1, skip_reason=COALESCE(skip_reason, ?) WHERE proposal_id=? "
     "AND state='proposed'"),
}
END_PENDING_STATES = {"expired", "superseded", "withdrawn"}
# A function whose SQL names its tables from a module constant. The case checks that the
# constant names no guarded table.
DYNAMIC_TABLE_GRANTS = {"lib.desk.store:_fts_triggers"}   # the FTS triggers of store.FTS_TABLES

_STMT = re.compile(r"\b(UPDATE|INSERT|REPLACE|DELETE)\b", re.I)
_UPD = re.compile(r"\bUPDATE\s+(?:OR\s+\w+\s+)?(\S+)\s+SET\s+(.*?)(?:\s+WHERE\s+(.*?))?\s*(?:;|$)", re.I | re.S)
_INS_ANY = re.compile(r"\b(?:INSERT|REPLACE)\s+(OR\s+\w+\s+)?INTO\s+(\S+?)\s*(?:\(([^)]*)\)\s*VALUES\s*\((.*)\))?\s*(?:;|$)",
                      re.I | re.S)
_DEL = re.compile(r"\bDELETE\s+FROM\s+(\S+)", re.I)


def sql_texts(fn) -> list:
    """Each text in a function that can hold SQL: a str constant, and an f-string with
    ``{?}`` for each placeholder (then a dynamic table or column list fails the rule)."""
    out, skip = [], set()
    for n in ast.walk(fn):
        if isinstance(n, ast.JoinedStr):
            parts = []
            for v in n.values:
                skip.add(id(v))
                parts.append(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{?}")
            out.append("".join(parts))
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            out.append(n.value)
    return [t for t in out if _STMT.search(t)]


def _top_split(text: str) -> list:
    """Split on the commas outside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [x.strip() for x in parts if x.strip()]


def sql_writes(text: str) -> list:
    """(op, table, {column: value} or None, normalized statement) for each write in a text.
    ``None`` columns: the statement does not name them (a dynamic list)."""
    out = []
    norm = " ".join(text.split())
    for m in _UPD.finditer(norm):
        cols = {}
        for a in _top_split(m.group(2)):
            k, eq, v = a.partition("=")
            if not eq or not re.fullmatch(r"\w+", k.strip()):
                cols = None
                break
            cols[k.strip().lower()] = v.strip()
        out.append(("UPDATE", m.group(1).lower(), cols, norm))
    for m in _INS_ANY.finditer(norm):
        op = "REPLACE" if (m.group(1) and "replace" in m.group(1).lower()) or norm.lstrip().upper().startswith("REPLACE") \
            else "INSERT"
        cols = None
        if m.group(3) is not None:
            names, vals = [c.strip().lower() for c in m.group(3).split(",")], _top_split(m.group(4))
            cols = dict(zip(names, vals)) if len(names) == len(vals) else None
        out.append((op, m.group(2).lower(), cols, norm))
    for m in _DEL.finditer(norm):
        out.append(("DELETE", m.group(1).lower(), None, norm))
    return out


def write_verdict(node: str, w: tuple):
    """None when a write a tool can reach is allowed, else the reason."""
    op, table, cols, stmt = w
    if "{" in table:
        return None if node in DYNAMIC_TABLE_GRANTS else "a dynamic table name"
    if table not in GUARDED:
        return None
    guard = GUARDED[table]
    if op == "DELETE":
        return f"DELETE on {table}"
    if op == "REPLACE":
        return f"REPLACE on {table}"
    if op == "UPDATE":
        if guard is None:
            return f"UPDATE on {table}"
        if cols is None or set(cols) & guard:
            return None if (node, stmt) in STATE_WRITE_GRANTS else \
                f"UPDATE of {sorted(set(cols or {}) & guard) or 'unnamed columns'} on {table}"
        return None
    col, want = INSERT_STATE[table]
    if cols is None or cols.get(col) != want:
        return f"INSERT into {table} with {col} {cols.get(col) if cols else 'unnamed'}"
    if table == "broker_jobs" and cols.get("status") != "'queued'":
        return "INSERT into broker_jobs with a status other than 'queued'"
    return None


def case_state_writes():
    g, reached = tool_graph()
    bad, granted, dyn, n_writes = [], set(), set(), 0
    for node in sorted(reached):
        for text in sql_texts(g.funcs[node]):
            for w in sql_writes(text):
                n_writes += 1
                why = write_verdict(node, w)
                if why:
                    bad.append((node, why, w[3][:160]))
                elif (node, w[3]) in STATE_WRITE_GRANTS:
                    granted.add((node, w[3]))
                elif "{" in w[1]:
                    dyn.add(node)
    r.ok("state_writes (sanity): the scan sees the SQL writes a tool can reach", n_writes >= 5, n_writes)
    r.eq("state_writes: no reachable SQL approves, checks, reviews, places or re-kinds anything", bad, [])
    r.eq("state_writes: each grant is held by a reachable function (no stale grant)", granted, STATE_WRITE_GRANTS)
    r.eq("state_writes: each dynamic-table grant is used (no stale grant)", dyn, DYNAMIC_TABLE_GRANTS)
    fts_names = set(store.FTS_TABLES) | {t for t, _cols in store.FTS_TABLES.values()}
    r.eq("state_writes: the FTS trigger tables name no guarded table", sorted(fts_names & set(GUARDED)), [])
    # end_pending takes its state as a parameter: its own guard limits it to three states.
    fn = g.funcs.get("lib.desk.proposals:end_pending")
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.Compare) and len(n.ops) == 1
              and isinstance(n.ops[0], ast.NotIn) and isinstance(n.left, ast.Name) and n.left.id == "new_state"
              and isinstance(n.comparators[0], (ast.Tuple, ast.Set, ast.List))] if fn else []
    vals = [{e.value for e in gd.comparators[0].elts if isinstance(e, ast.Constant)} for gd in guards]
    r.eq("state_writes: end_pending refuses every state but expired, superseded and withdrawn", vals,
         [END_PENDING_STATES])
    # The rule binds on each form it must refuse (the probes of the adversarial round).
    planted = {
        "approve, state not first": "UPDATE proposals SET decided_by='desk', state='approved' WHERE id=1",
        "approve by a parameter": "UPDATE proposals SET state=? WHERE id=1",
        "a snapshot job made an execute job": "UPDATE broker_jobs SET kind='execute', status='queued' WHERE id=1",
        "a leg checked and reviewed": "UPDATE orders SET state='checked', review_ok=1, checked_at=datetime('now') WHERE id=1",
        "a leg made placeable by its amount": "UPDATE orders SET dollar_amount=? WHERE ref_id=?",
        "an execute job inserted": "INSERT INTO broker_jobs (kind, status, requested_by, created_at) "
                                   "VALUES ('execute', 'queued', ?, ?)",
        "a proposal inserted approved": "INSERT INTO proposals (kind, state, nonce) VALUES ('trade', 'approved', ?)",
        "a proposal replaced": "INSERT OR REPLACE INTO proposals (kind, state) VALUES ('trade', 'pending')",
        "a broker job deleted": "DELETE FROM broker_jobs WHERE id=1",
        "a dynamic table": "UPDATE {?} SET state='approved' WHERE id=1",
    }
    for label, text in planted.items():
        ws = sql_writes(text)
        r.ok(f"state_writes: the rule refuses {label}",
             bool(ws) and any(write_verdict("lib.desk.tools:_probe", w) for w in ws), ws)
    r.eq("state_writes: the rule passes the snapshot insert",
         [write_verdict("lib.desk.store:enqueue_snapshot_job", w) for w in sql_writes(
             "INSERT INTO broker_jobs (kind, status, requested_by, created_at) VALUES ('snapshot', 'queued', ?, ?)")],
         [None])
    fs = ast.parse('def f(t):\n    c.execute(f"UPDATE {t} SET state=? WHERE id=?")\n').body[0]
    r.ok("state_writes: an f-string table is read with a placeholder",
         any(write_verdict("x", w) == "a dynamic table name" for t in sql_texts(fs) for w in sql_writes(t)),
         sql_texts(fs))


# --- closure -----------------------------------------------------------------------

def case_closure():
    direct = repo_imports("lib.desk.tools")
    closure, todo = set(direct), [m for m in direct if m.startswith("lib.desk.")]
    while todo:
        m = todo.pop()
        for d in repo_imports(m):
            if d.startswith("lib.desk.") and d not in closure:
                closure.add(d)
                todo.append(d)
    forbidden = {"lib.desk.approvals", "lib.desk.execution"}
    r.ok("closure: tools.py does not import approvals or execution", not (direct & forbidden), sorted(direct & forbidden))
    offenders = {m: sorted(repo_imports(m) & forbidden) for m in sorted(closure) if repo_imports(m) & forbidden}
    r.eq("closure: no module tools.py imports (transitively in lib/desk) imports approvals or execution",
         offenders, {})
    r.ok("closure: approvals and execution are not in the import closure", not (closure & forbidden),
         sorted(closure & forbidden))
    g, reached = tool_graph()
    r.ok("closure: no call path from a tool reaches proposals.apply_change",
         "lib.desk.proposals:apply_change" not in reached)
    r.ok("closure: no call path from a tool reaches approvals.handle_tap",
         not any(x.startswith("lib.desk.approvals:") for x in reached), sorted(x for x in reached if "approvals" in x))
    r.ok("closure: no call path from a tool reaches an execution function",
         not any(x.startswith("lib.desk.execution:") for x in reached), sorted(x for x in reached if "execution" in x))
    # The graph finds a path when one exists (approvals reaches apply_change).
    g2 = Graph()
    g2.load("lib.desk.approvals")
    r.ok("closure: the call graph finds approvals.handle_tap -> proposals.apply_change",
         "lib.desk.proposals:apply_change" in g2.reach(["lib.desk.approvals:handle_tap"]))


# --- smoke -------------------------------------------------------------------------

SECRETS = agent.Secrets(telegram_bot_token="111222:SECRET-TOKEN-zz", telegram_allowed_chat_ids=OWNER,
                        telegram_alert_chat_ids="123123123", alpha_vantage_api_key="AVSECRETKEY99",
                        sec_user_agent="Quiver test@example.com")
SECRET_VALUES = ["111222:SECRET-TOKEN-zz", "SECRET-TOKEN", OWNER, "123123123", "AVSECRETKEY99",
                 "test@example.com", "TEST1234"]

ARGS = {
    "portfolio_view": {}, "refresh_portfolio": {"wait_seconds": 0}, "get_quotes": {"tickers": ["ANET"]},
    "get_book": {}, "list_companies": {}, "get_company": {"ticker": "ANET"},
    "search_research": {"query": "revenue"}, "list_filings": {"ticker": "ANET"},
    "get_filing_sections": {"accession": "0001596532-26-000174"}, "get_document": {"document_id": 1},
    "get_financials": {"ticker": "ANET"}, "get_metrics": {"ticker": "ANET"},
    "run_valuation": {"ticker": "ANET", "method": "dcf", "scenarios": {}}, "get_technicals": {"ticker": "ANET"},
    "get_transcript": {"ticker": "ANET"}, "get_etf_lookthrough": {"ticker": "SOXX"}, "get_events": {},
    "get_proposals": {}, "get_track_record": {}, "get_attribution": {}, "get_settings": {},
    "ledger_query": {"sql": "SELECT 1 AS one"}, "screen_companies": {},
    "react": {"message_id": 1, "emoji": "👍"}, "say": {"text": "Hello."},
    "report": {"text": "The ANET 10-Q is in."}, "load_company": {"ticker": "ZZZZQ"},
    "bring_up_next": {}, "close_item": {"item_id": 1}, "defer_item": {"item_id": 1, "hours": 2},
    "hold_agenda": {"hours": 1}, "show_card": {"proposal_id": 1},
    "fetch_document": {"url": "http://127.0.0.1/x"},
    "record_finding": {"ticker": "ANET", "kind": "growth", "claim": "x", "quote": "y", "document_id": 1},
    "add_note": {"text": "The owner prefers short messages."},
    "set_research_status": {"ticker": "ANET", "status": "watch", "reason": "test"},
    "log_news_event": {"ticker": "ANET", "document_id": 1}, "start_deep_dive": {"ticker": "ANET"},
    "record_verdict": {"event_id": 1, "verdict": "intact", "summary": "x"}, "withdraw_proposal": {"proposal_id": 1},
    "propose_trade": {"tickers": ["ANET"], "origin": {"owner_message_id": 1}, "fraction": 1.0, "rationale": "x",
                      "evidence": []},
    "propose_change": {"change": {"kind": "set_risk", "changes": {"min_trade_notional": 6}}, "rationale": "x",
                       "evidence": []},
}


class FakeTransport:
    def __init__(self):
        self.calls, self.n = [], 700

    def __call__(self, method, params):
        self.calls.append((method, params))
        self.n += 1
        return {"ok": True, "result": {"message_id": self.n}}


def case_smoke():
    r.eq("smoke: the smoke args cover every tool", set(ARGS), set(tools.SPECS))
    clock = FakeClock("2026-08-05T14:00:00Z").pin_market()
    crashes, non_json, leaks, kinds = [], [], [], []
    try:
        for name in sorted(tools.SPECS):
            role = "analyst" if name in ("report", "record_verdict") else "desk"
            with temp_root() as root:
                led = root.led()
                with led.connect() as c:
                    store.ensure_desk_schema(c)
                rt = agent.Runtime.from_env(root.env)
                tp = FakeTransport()
                box = outbox.Outbox(led, transport=tp, chat_id=OWNER, sleep=lambda s: None)
                turn = agent.TurnState() if role == "desk" else None
                if turn is not None:
                    turn.start(chat_id=OWNER, owner_message_ids=[], started_at=store.now_iso())
                ctx = tools.ToolContext.build(
                    role=role, runtime=rt, secrets=SECRETS, turn=turn, job=agent.JobState() if role == "analyst" else None,
                    led=led, transport=tp, box=box, seams=seams(), yf_price_fn=FakePrices(),
                    fetch_fn=FakeConn(), screen_fn=lambda q: [], efts_fn=lambda q: [], sleep=lambda s: None,
                    touch=lambda n: None,
                    extra={"technicals_fn": lambda t: {"regime": "up"}, "av_opener": FakeOpener()})
                try:
                    res = tools.call(ctx, name, ARGS[name])
                except Exception as e:  # noqa: BLE001
                    crashes.append((name, f"raised {type(e).__name__}: {e}"))
                    continue
                blob = json.dumps(res, default=str)
                text = ((res.get("content") or [{}])[0] or {}).get("text", "") if isinstance(res, dict) else ""
                if not isinstance(res, dict) or not isinstance(res.get("content"), list):
                    non_json.append((name, "not an SDK result dict"))
                elif res.get("is_error"):
                    if text.startswith(f"{name} failed:") or text == f"{name} is not a {role} tool":
                        crashes.append((name, text[:300]))
                else:
                    try:
                        body = json.loads(text)
                    except ValueError:
                        body = None
                    if not isinstance(body, dict):
                        non_json.append((name, text[:200]))
                for s in SECRET_VALUES:
                    if s in blob:
                        leaks.append((name, s))
                with led.connect() as c:
                    kinds += [(name, k[0]) for k in c.execute("SELECT kind FROM broker_jobs").fetchall()]
    finally:
        clock.unpin()
    r.eq("smoke: no tool crashes on an empty ledger", crashes, [])
    r.eq("smoke: every successful result is a JSON object", non_json, [])
    r.eq("smoke: no result holds a secret value", leaks, [])
    r.ok("smoke: a tool queues only snapshot broker jobs", all(k == "snapshot" for _n, k in kinds), kinds)
    r.ok("smoke: refresh_portfolio queued one snapshot job", ("refresh_portfolio", "snapshot") in kinds, kinds)


def main() -> int:
    for case in (case_registry, case_no_refs, case_jobs_kind, case_state_writes, case_closure, case_smoke):
        try:
            case()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r.ok(f"{case.__name__}: ran without an exception", False, f"{type(e).__name__}: {e}")
    return r.done()


if __name__ == "__main__":
    raise SystemExit(main())
