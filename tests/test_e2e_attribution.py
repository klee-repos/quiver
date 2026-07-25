#!/usr/bin/env python3
"""E2E — the factor-attribution diagnostic, driven through the REAL CLI.

Proves the four properties that make this a diagnostic rather than a gate:
  A. it RECOVERS a known beta end-to-end from a seeded ledger + a seeded sidecar;
  B. it REFUSES rather than guesses on a starved ledger, a stale sidecar, or a
     corrupt one -- always exit 0, always a stated reason, never a traceback;
  C. it emits NO verdict / threshold / p-value key, at any sample size;
  D. it is INVISIBLE to the trading path: importing the sizing/learning modules
     never pulls it in, `plan` output is byte-identical with and without the
     sidecar present, and running it mutates no ledger row.

Every case runs against a TEMP ledger. Nothing here may write to state/ledger.db:
`plan` calls get_or_create_baseline(), which would insert a fabricated row into
the very equity series this feature regresses on.

Offline: the factor sidecar is written directly as a fixture, so no case here
touches the network. (`factors-fetch` is the only networked path and is exercised
by the operator, not by this suite.)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
sys.path.insert(0, str(REPO))

PASS = 0
FAIL = 0


def ok(name, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}{(': ' + str(extra)) if extra else ''}")


def _seed_config(dst: Path) -> Path:
    cfg = dst / "config.yaml"
    shutil.copy(REPO / "config.yaml", cfg)
    return cfg


def _run(workdir: Path, db: Path, cfg: Path, *args, timeout=180):
    env = dict(os.environ)
    env["QUIVER_LEDGER_DB"] = str(db)
    env["QUIVER_CONFIG"] = str(cfg)
    env["RH_ACCOUNT_NUMBER"] = "12345678"
    env["QUIVER_FACTORS_PATH"] = str(SIDECAR)
    env.pop("NOTIFY_TO", None)
    proc = subprocess.run([PY, "tick.py", *args], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=timeout)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    parsed = json.loads(lines[-1]) if lines else None
    return proc, parsed


# The sidecar lives in a TEMP dir via QUIVER_FACTORS_PATH. Writing to the real
# state/tmp/factors.json would clobber the operator's live scratch file -- which it
# did once during development, silently starving a real run down to one holding.
SIDECAR = Path(tempfile.mkdtemp(prefix="quiver-attr-sidecar-")) / "factors.json"


def _write_sidecar(day: str, series: dict) -> Path:
    """Write the factor sidecar the diagnostic reads. Returns its path."""
    SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    SIDECAR.write_text(json.dumps({"trading_day": day, "series": series,
                                   "source": "test-fixture"}))
    return SIDECAR


def _synthetic_series(n_days: int, beta: float, seed: int = 5):
    """A benchmark and one holding whose true beta is `beta`, as price levels."""
    import numpy as np
    rng = np.random.default_rng(seed)
    mkt_r = rng.normal(scale=0.008, size=n_days)
    hold_r = beta * mkt_r + rng.normal(scale=0.0015, size=n_days)
    dates, spy, hold = [], {}, {}
    px_s, px_h = 100.0, 50.0
    import datetime as _dt
    d = _dt.date(2026, 1, 5)
    for i in range(n_days):
        while d.weekday() >= 5:
            d += _dt.timedelta(days=1)
        ds = d.isoformat()
        dates.append(ds)
        spy[ds] = {"open": px_s, "close": px_s}
        hold[ds] = {"open": px_h, "close": px_h}
        px_s *= (1.0 + float(mkt_r[i]))
        px_h *= (1.0 + float(hold_r[i]))
        d += _dt.timedelta(days=1)
    return dates, spy, hold


def _seed_book(db: Path, holdings: dict):
    """Seed an active goal + target book into a TEMP ledger.

    Without this, cfg.strategy is None in a tempdir (config.py resolves
    strategy.yaml relative to the CONFIG file's parent), so bottom_up returns a
    stub and every assertion about the PRIMARY estimate passes vacuously.
    """
    from lib.ledger import Ledger
    led = Ledger(str(db))
    led.set_strategy_goal_with_holdings(
        goal=dict(created_at="2026-01-05T09:30:00-05:00", target_return_pct=15.0,
                  horizon_months=12, benchmark="SPY", benchmark_annual_pct=8.0,
                  constraint_note="test", macro_thesis_version="t1",
                  macro_thesis_json="{}", active_book="test_book",
                  as_of="2026-01-05", start_date="2026-01-05", start_equity=10000.0),
        holdings=[dict(sleeve="test", ticker=t, target_weight=w, band=5.0,
                       status="active", book="test_book", quotable=1, proxy_ticker=None,
                       updated_at="2026-01-05T09:30:00-05:00")
                  for t, w in holdings.items()])


def _seed_curve(db: Path, dates, series, beta: float, seed: int = 9):
    """Insert a real day_baseline curve into a TEMP ledger.

    Case C is only meaningful if top_down actually produces a Fit -- with an empty
    ledger it short-circuits to {"ok": false} and as_dict() is never called, so a
    banned key added to the Fit payload would go unseen. (That exact vacuity was
    caught by mutation testing: injecting a "verdict" key still passed.)
    """
    import numpy as np
    from lib.ledger import Ledger
    rng = np.random.default_rng(seed)
    led = Ledger(str(db))
    eq = 10000.0
    prev = None
    for ds in dates:
        if prev is not None:
            mkt_r = series[ds]["close"] / series[prev]["close"] - 1.0
            eq *= (1.0 + beta * mkt_r + float(rng.normal(scale=0.001)))
        led.get_or_create_baseline(ds, eq, f"{ds}T09:45:00-04:00")
        prev = ds


def main():
    # No live-state backup needed: the sidecar is a temp file (see SIDECAR).
    _run_cases()


def _run_cases():
    from lib.ledger import Ledger
    from lib import market

    day = market.trading_day_et()

    # ---------- A. recovery of a KNOWN beta, end-to-end through the CLI -------
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db = d / "ledger.db"
        cfg = _seed_config(d)
        Ledger(str(db))  # create schema

        dates, spy, hold = _synthetic_series(260, beta=1.35)
        _write_sidecar(day, {"SPY": spy, "QQQ": spy, "TESTCO": hold})
        _seed_book(db, {"TESTCO": 100.0})

        proc, out = _run(d, db, cfg, "attribution")
        ok("A: exit 0 on a seeded run", proc.returncode == 0, proc.stderr[-300:])
        ok("A: emits one parseable JSON line", out is not None)
        ok("A: ok:true", bool(out and out.get("ok")), out)

        # The bottom-up estimator is exercised directly with the same fixture, so
        # the recovery assertion does not depend on a book being seeded.
        from lib import attribution
        mkt_r, hold_r = [], []
        for a, b in zip(dates, dates[1:]):
            mkt_r.append(spy[b]["close"] / spy[a]["close"] - 1.0)
            hold_r.append(hold[b]["close"] / hold[a]["close"] - 1.0)
        bb = attribution.book_beta({"TESTCO": 100.0}, {"TESTCO": hold_r}, mkt_r)
        ok("A: recovers the injected beta=1.35 within 0.05 (in-process)",
           abs(bb["beta_book"] - 1.35) < 0.05, bb["beta_book"])

        # THE SAME ASSERTION THROUGH THE REAL CLI. Without this the whole bottom-up
        # path -- book resolution, the per-ticker date intersection, mkt_for_ticker
        # -- is covered by zero assertions: sign-flipping it left the suite green.
        cli_bu = (out or {}).get("bottom_up") or {}
        ok("A: CLI resolved the book from the seeded ledger goal",
           (out or {}).get("book_source") == "ledger_goal", (out or {}).get("book_source"))
        ok("A: CLI bottom_up recovers beta=1.35 (PRIMARY estimate, end-to-end)",
           cli_bu.get("beta_book") is not None and abs(cli_bu["beta_book"] - 1.35) < 0.05,
           cli_bu.get("beta_book"))
        ok("A: CLI reports a real standard error at n>200",
           (cli_bu.get("per_name", {}).get("TESTCO", {}).get("stderr") is not None
            and cli_bu["per_name"]["TESTCO"]["n"] > 200), cli_bu.get("per_name"))
        ok("A: nothing in the book is left unpriced",
           cli_bu.get("unpriced_weight_pct") == 0, cli_bu.get("unpriced_reasons"))

    # ---------- B. refusal paths: starved / stale / corrupt -------------------
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db = d / "ledger.db"
        cfg = _seed_config(d)
        Ledger(str(db))
        _, spy, _ = _synthetic_series(30, beta=1.0)

        _write_sidecar("1999-01-01", {"SPY": spy})
        proc, out = _run(d, db, cfg, "attribution")
        ok("B: stale sidecar -> exit 0", proc.returncode == 0)
        ok("B: stale sidecar -> ok:false with a reason",
           bool(out and out.get("ok") is False and out.get("reason")), out)
        ok("B: the reason names staleness", "stale" in str(out.get("reason", "")).lower(), out)

        SIDECAR.write_text("{not json")
        proc, out = _run(d, db, cfg, "attribution")
        ok("B: corrupt sidecar -> exit 0", proc.returncode == 0)
        ok("B: corrupt sidecar -> ok:false, no traceback",
           bool(out and out.get("ok") is False) and "Traceback" not in proc.stderr, proc.stderr[-200:])

        SIDECAR.unlink()
        proc, out = _run(d, db, cfg, "attribution")
        ok("B: missing sidecar -> exit 0 + ok:false",
           proc.returncode == 0 and bool(out) and out.get("ok") is False, out)

        _write_sidecar(day, {"SPY": spy})
        proc, out = _run(d, db, cfg, "attribution")
        ok("B: starved ledger (0 equity points) still exits 0", proc.returncode == 0)
        td_blk = (out or {}).get("top_down", {})
        ok("B: starved ledger refuses the regression with a stated reason",
           all(v.get("ok") is False and v.get("reason")
               for k, v in td_blk.items() if k != "timing_disagreement"), td_blk)

        _write_sidecar(day, {"QQQ": spy})
        proc, out = _run(d, db, cfg, "attribution")
        ok("B: benchmark absent from the sidecar -> ok:false, exit 0",
           proc.returncode == 0 and (out or {}).get("ok") is False, out)

    # ---------- C. no verdict / threshold / p-value, at any n ----------------
    BANNED = {"verdict", "significant", "significance", "p", "p_value", "pvalue",
              "t_crit", "t_critical", "threshold", "pass", "fail", "reject", "alpha"}

    def scan(node, path=""):
        bad = []
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in BANNED:
                    bad.append(f"{path}.{k}")
                bad += scan(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                bad += scan(v, f"{path}[{i}]")
        return bad

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db = d / "ledger.db"
        cfg = _seed_config(d)
        Ledger(str(db))
        dates, spy, hold = _synthetic_series(260, beta=1.35)
        _write_sidecar(day, {"SPY": spy, "QQQ": spy, "TESTCO": hold})
        # Seed a real curve so top_down produces an actual Fit -- otherwise the
        # scan below has nothing to scan and passes vacuously.
        _seed_curve(db, dates, spy, beta=1.35)
        _seed_book(db, {"TESTCO": 100.0})
        _, out = _run(d, db, cfg, "attribution")
        ok("C: the banned-key scan covers the REAL bottom_up payload",
           bool(((out or {}).get("bottom_up") or {}).get("per_name")),
           (out or {}).get("bottom_up"))

        c2c = ((out or {}).get("top_down", {}) or {}).get("close_to_close", {})
        ok("C: the scan is NOT vacuous — top_down produced a real fit",
           c2c.get("ok") is True and "coefficients" in c2c, c2c)
        ok("C: with df above the floor, stderr and t ARE reported",
           (c2c.get("coefficients", {}).get("beta", {}).get("stderr") is not None
            and c2c.get("coefficients", {}).get("beta", {}).get("t") is not None), c2c)
        ok("C: a real fit recovers the injected beta=1.35",
           abs(c2c.get("coefficients", {}).get("beta", {}).get("estimate", 0) - 1.35) < 0.15,
           c2c.get("coefficients", {}).get("beta"))
        offenders = scan(out or {})
        ok("C: output contains no verdict/threshold/p-value/'alpha' key",
           not offenders, offenders)
        ok("C: the diagnostic-only basis is stated in the payload",
           "not a gate" in str((out or {}).get("basis", "")).lower(), out.get("basis") if out else None)

    # ---------- D. invisible to the trading path -----------------------------
    probe = (
        "import sys; "
        "import tick, lib.signals, lib.allocate, lib.calibrate, lib.memory, "
        "lib.levers, lib.risk, lib.reflect_memory; "
        "import analyze; "
        "bad=[m for m in ('lib.attribution',) if m in sys.modules]; "
        "print('LEAK' if bad else 'CLEAN')"
    )
    del probe  # superseded by probe2 below (tick.py legitimately owns the subcommand)
    # tick.py itself legitimately imports attribution (it owns the subcommand), so
    # the meaningful check is the ANALYSIS/SIZING side in its own interpreter.
    probe2 = (
        "import sys; "
        "import lib.signals, lib.allocate, lib.calibrate, lib.memory, lib.levers, "
        "lib.risk, lib.reflect_memory, lib.strategy_context; "
        "bad=[m for m in sys.modules if m=='lib.attribution']; "
        "print('LEAK' if bad else 'CLEAN')"
    )
    pr2 = subprocess.run([PY, "-c", probe2], cwd=str(REPO), capture_output=True, text=True, timeout=180)
    ok("D: sizing/learning/analysis modules never import attribution (transitive)",
       "CLEAN" in pr2.stdout, pr2.stdout + pr2.stderr[-300:])

    # plan output must be byte-identical with and without the sidecar present.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        db = d / "ledger.db"
        cfg = _seed_config(d)
        Ledger(str(db))
        plan_in = d / "plan.json"
        # The REAL plan input shape (tick.py module docstring). An input plan
        # rejects produces an identical {"error": ...} on both runs, which would
        # make the byte-identity assertion below trivially true -- so the shape
        # matters and `ok:true` is asserted before the comparison is trusted.
        plan_in.write_text(json.dumps({
            "equity": 1000.0,
            "buying_power": 500.0,
            "now_iso": "2026-06-15T10:00:00-04:00",
            "run_id": "fixed-run-id-for-determinism",
            "positions": {},
            "analyses": [],
        }))
        env = dict(os.environ)
        env["QUIVER_LEDGER_DB"] = str(db)
        env["QUIVER_CONFIG"] = str(cfg)
        env["RH_ACCOUNT_NUMBER"] = "12345678"

        def run_plan():
            p = subprocess.run([PY, "tick.py", "plan", "--input", str(plan_in)],
                               cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180)
            return p.stdout

        _, spy, _ = _synthetic_series(30, beta=1.0)
        _write_sidecar(day, {"SPY": spy})
        with_sidecar = run_plan()
        SIDECAR.unlink()
        without_sidecar = run_plan()
        # NON-VACUITY GATE FIRST. A failing plan emits the same error string on both
        # runs, so byte-identity would hold for the wrong reason. Assert plan
        # actually SUCCEEDED before the comparison means anything. (Caught by
        # mutation testing: with a malformed input the comparison passed even when
        # plan was deliberately made sidecar-dependent.)
        _pj = [ln for ln in with_sidecar.splitlines() if ln.strip().startswith("{")]
        _pd = json.loads(_pj[-1]) if _pj else {}
        ok("D: plan SUCCEEDED, so the byte-identity comparison is not vacuous",
           bool(_pd) and "error" not in _pd and _pd.get("ok") is not False, _pd)
        ok("D: plan stdout is byte-identical with and without the factor sidecar",
           with_sidecar == without_sidecar,
           f"len {len(with_sidecar)} vs {len(without_sidecar)}")

    # ---------- D2. the real ledger is untouched by a real run ---------------
    real = REPO / "state" / "ledger.db"
    if real.exists():
        import sqlite3

        def counts():
            with sqlite3.connect(f"file:{real}?mode=ro", uri=True) as c:
                return tuple(c.execute(
                    "SELECT (SELECT COUNT(*) FROM day_baseline),"
                    "       (SELECT COUNT(*) FROM orders),"
                    "       (SELECT COUNT(*) FROM decisions),"
                    "       (SELECT COUNT(*) FROM outcomes)").fetchone())

        before = counts()
        pr3 = subprocess.run([PY, "tick.py", "attribution"], cwd=str(REPO),
                             capture_output=True, text=True, timeout=180)
        after = counts()
        ok("D2: SMOKE — real `tick.py attribution` against the real ledger exits 0",
           pr3.returncode == 0, pr3.stderr[-300:])
        ok("D2: SMOKE — it mutates ZERO rows in the real ledger",
           before == after, f"{before} -> {after}")
        jl = [ln for ln in pr3.stdout.splitlines() if ln.strip().startswith("{")]
        ok("D2: SMOKE — real run emits parseable JSON carrying 'ok'",
           bool(jl) and "ok" in json.loads(jl[-1]))


if __name__ == "__main__":
    main()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
