#!/usr/bin/env python3
"""Deterministic e2e for the benchmark measurement (`benchmark-backfill`).

WHAT IT PINS. `outcomes.benchmark_return` feeds `alpha`, which `lib.memory.score_return`
prefers over the raw directional return, which `lib.calibrate` turns into a conviction
multiplier, which `lib.allocate` turns into a target weight, which becomes order
dollars. So this is a trading-loop input, not plumbing, and every case below asserts
REAL column values in a REAL sqlite file after driving the REAL CLI in a subprocess.

THE INCIDENT BEING PINNED. Seven rows carried 0.007521 -- SPY 2026-06-08 -> 2026-07-02 --
while the position leg of those same outcomes used 2026-07-06 prices. The true window
return is 0.016314572756584766, a 2.2x understatement. Forty-four more rows were NULL
because TICK.md sanctioned "or omit" and the orchestrator omitted.

OFFLINE. The sidecar is written as a fixture through QUIVER_BENCHMARK_PATH; nothing
here touches the network or the live state/ledger.db. `benchmark-fetch` is the only
networked path and is exercised by the operator/runner, not by this suite.

MUTATION MAP (every line below was RUN: revert the named behavior, confirm EXACTLY that
assertion fails while the feature still imports, restore, confirm green):
  * anchor walks back over a missing session    -> T2/T10b (writes 0.007521 instead of None)
  * `record_outcome` instead of the narrow UPDATE -> T5 (resolved_at becomes "clobbered")
  * dry-run writes                              -> T4  (all-table digest differs)
  * refusal preserves instead of clears         -> T10b (the wrong value survives)
  * cmd_reflect honors the supplied benchmark   -> T12 (alpha non-NULL through the real CLI)
  * strategy_change_log not written on --apply  -> T9
  * typo'd trust kwarg in lib/wall_replay.py    -> the replay assertion in test_e2e_wall_replay
  * producer calendar bounded by max(series)    -> BM1 (producer-contract assertions)
  * lib.benchmark coverage check removed        -> BM1 (coverage assertions)
  * BOTH of the previous two                    -> BM1 end-to-end: the truncated series is
      written instead of refused (11 assertions red) -- this is the defect that actually
      shipped and passed a green suite, because both fixtures hand-wrote a session calendar
      the producer could not emit. `_producer_sessions` now calls the REAL builder.
  * _benchmark_scorecard returns a constant     -> T11 (was vacuous: the old fixture had one
      decision per ticker, so n=1 < min_n=5 pinned every multiplier at 1.0 and the diff was
      always empty). The fixture now clears min_n AND flips the hit/miss sign.
  * alpha written where directional_return NULL -> T17 (was a constant 0: the old fixture had
      no such row at all, so the count could never become non-zero)
  * hyphenated `--trust-input-benchmark` flag   -> T19 (the source scan matched only the
      underscore spelling, so argparse's hyphen->underscore dest mapping slipped past it)
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "bin" / "python")
sys.path.insert(0, str(REPO))

from lib.ledger import Ledger  # noqa: E402

PASS = 0
FAIL = 0

# The real closes from the incident window.
SPY = {"2026-05-29": 754.5, "2026-06-01": 758.539978, "2026-06-08": 739.219971,
       "2026-07-02": 744.780029, "2026-07-06": 751.280029}
TRUE_WINDOW = 0.016314572756584766   # SPY 2026-06-08 -> 2026-07-06
WRONG_WINDOW = 0.007521              # what was stored: 2026-06-08 -> 2026-07-02


def _producer_sessions(series: dict) -> list:
    """Call the PRODUCER'S OWN calendar builder -- never a re-implementation of it.

    This indirection is the whole lesson of the BM1 regression. An earlier version of
    this file hand-wrote a session list that happened to extend beyond a truncated
    fixture, which the real producer could not emit; the tail guard was therefore never
    exercised, and a defect that silently reproduced the 0.007521 incident value shipped
    with a green suite. Re-implementing the rule here would rebuild the same blind spot:
    reverting the producer has to turn these assertions RED, which only happens if the
    test calls the real function.
    """
    import tick as _tick
    return _tick._benchmark_sessions(series, max(series))


def ok(name, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}{(': ' + str(extra)) if extra else ''}")


def _run(db: Path, sidecar: Path, *args, timeout=180):
    env = dict(os.environ)
    env["QUIVER_LEDGER_DB"] = str(db)
    env["QUIVER_BENCHMARK_PATH"] = str(sidecar)
    env["RH_ACCOUNT_NUMBER"] = "12345678"
    env.pop("NOTIFY_TO", None)
    proc = subprocess.run([PY, "tick.py", *args], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=timeout)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    return proc, (json.loads(lines[-1]) if lines else None)


def _write_sidecar(path: Path, series=None, sessions=None):
    series = SPY if series is None else series
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "trading_day": "2026-07-25", "benchmark": "SPY", "series": series,
        # Derived from the producer's own rule unless a case overrides it on purpose.
        "sessions": _producer_sessions(series) if sessions is None else sessions,
        "source": "fixture"}))
    return path


# Returns chosen so the re-measurement FLIPS the hit/miss sign, and enough rows per
# ticker to clear calibrate.DEFAULT_MIN_N=5. An earlier fixture used one decision per
# ticker, which pinned every multiplier at the neutral 1.0 and left calibration_diff
# empty -- so T11/T18 passed against a _benchmark_scorecard that returned a constant.
# A fixture that cannot move the number under test cannot test it.
WRG_DRET = 0.012    # bearish call, sits BETWEEN the wrong (0.007521) and true (0.016315)
                    # benchmark: MISS while the wrong value stands, HIT once corrected.
NUL_DRET = -0.005   # bearish call over a window where SPY FELL (06-01 -> 07-06):
                    # HIT on absolute return, MISS once the market leg is subtracted.


def _seed(db: Path):
    """Reproduce the incident: 7 rows carrying the WRONG value, 5 NULL, 1 unscorable."""
    led = Ledger(str(db))
    ids = {}
    for i in range(7):
        d = led.record_decision(trade_date="2026-06-08", ticker="WRG", run_id="r",
                                signal="Underweight", intent="sell",
                                decided_at=f"2026-06-08T10:0{i}:00-04:00", decision_price=100.0)
        led.record_outcome(d, resolved_at="2026-07-06T21:25:17-04:00", holding_days=28,
                           directional_return=WRG_DRET, benchmark_return=WRONG_WINDOW,
                           alpha=WRG_DRET - WRONG_WINDOW, scored_against="directional")
        ids[f"WRG{i}"] = d
    for i in range(5):
        d = led.record_decision(trade_date="2026-06-01", ticker="NUL", run_id="r",
                                signal="Underweight", intent="sell",
                                decided_at=f"2026-06-01T10:0{i}:00-04:00", decision_price=100.0)
        led.record_outcome(d, resolved_at="2026-07-06T21:25:17-04:00", holding_days=35,
                           directional_return=NUL_DRET, scored_against="both",
                           realized_pnl=4.0, adherence="within")
        ids[f"NUL{i}"] = d
    # A resolved row with NO position leg. alpha MUST stay NULL here however the window
    # resolves, or the `alpha non-NULL implies directional_return non-NULL` subset breaks
    # and the graded sample size could move across the min_n cliff. Without this row T17
    # counts zero candidates and can never fail.
    d = led.record_decision(trade_date="2026-06-08", ticker="NODR", run_id="r",
                            signal="Buy", intent="buy",
                            decided_at="2026-06-08T10:00:00-04:00", decision_price=100.0)
    led.record_outcome(d, resolved_at="2026-07-06T21:25:17-04:00", holding_days=28,
                       directional_return=None, realized_pnl=1.0, scored_against="both")
    ids["NODR"] = d
    return led, ids


def _digest(db: Path) -> str:
    """Canonical dump of EVERY table (enumerated from sqlite_master), not a file hash:
    opening a Ledger runs the schema + migrations, so file bytes are not stable."""
    c = sqlite3.connect(str(db))
    names = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    parts = []
    for n in names:
        for row in c.execute(f"SELECT * FROM {n}"):
            parts.append(f"{n}:{row!r}")
    c.close()
    return "\n".join(sorted(parts))


def _col(db: Path, decision_id: int, col: str):
    c = sqlite3.connect(str(db))
    v = c.execute(f"SELECT {col} FROM outcomes WHERE decision_id=?", (decision_id,)).fetchone()
    c.close()
    return v[0] if v else None


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        db, sidecar = td / "ledger.db", td / "benchmark.json"
        _, ids = _seed(db)
        _write_sidecar(sidecar)

        # --- T4: dry-run is inert across EVERY table -------------------------------
        before = _digest(db)
        proc, out = _run(db, sidecar, "benchmark-backfill")
        ok("T4 dry-run exits 0", proc.returncode == 0, proc.stderr[-400:])
        ok("T4 dry-run reports dry_run", out and out.get("dry_run") is True, out)
        ok("T4 dry-run says it did not apply", out and out.get("applied") is False, out)
        ok("T4 dry-run writes NOTHING (all-table digest unchanged)", _digest(db) == before)
        ok("T4 dry-run still counts the work", out and out.get("changes") == 13, out)
        ok("T4 dry-run classifies 7 corrections + 6 fills",
           out and out["counts"].get("correct") == 7 and out["counts"].get("fill") == 6, out)

        # --- T11: the printed diff cannot lie -------------------------------------
        # Recompute the same multipliers independently, through the real consumer.
        shadow = td / "shadow.db"
        shutil.copy(str(db), str(shadow))
        sled = Ledger(str(shadow))
        from lib import benchmark as _bmod
        _sess = _producer_sessions(SPY)
        for row in sled.outcomes_for_backfill():
            b, _ = _bmod.window_return(SPY, row["trade_date"],
                                       str(row["resolved_at"])[:10], _sess)
            dr = row["directional_return"]
            sled.update_outcome_benchmark(row["decision_id"], b,
                                          (dr - b) if (b is not None and dr is not None) else None)
        import lib.calibrate as calibrate
        from lib.config import load_config
        cfg = load_config(REPO / "config.yaml")
        names = sorted({r["ticker"] for r in Ledger(str(db)).outcomes_for_backfill()})
        indep_before = calibrate.build_calibration(
            Ledger(str(db)), names, min_n=int(cfg.strategy.learning.min_resolved_n))
        indep_after = calibrate.build_calibration(
            sled, names, min_n=int(cfg.strategy.learning.min_resolved_n))
        moved = [t for t in names if abs(indep_before[t] - indep_after[t]) > 1e-9]
        # Vacuity guard: if the fixture cannot move a multiplier, everything below passes
        # against a _benchmark_scorecard that just returns a constant.
        ok("T11 (non-vacuity) the fixture actually MOVES a real multiplier", len(moved) >= 2, moved)
        ok("T11 (non-vacuity) the command printed a non-empty diff",
           len((out or {}).get("calibration_diff", [])) >= 2, (out or {}).get("calibration_diff"))
        printed = {d["ticker"]: d for d in (out or {}).get("calibration_diff", [])}
        mismatch = [t for t in names
                    if (abs(indep_before[t] - indep_after[t]) > 1e-9) != (t in printed)]
        ok("T11 the printed diff names exactly the names whose multiplier really moves",
           not mismatch, mismatch)
        ok("T11 printed before/after match an independent lib.calibrate computation",
           all(abs(printed[t]["multiplier_before"] - round(indep_before[t], 4)) < 1e-9
               and abs(printed[t]["multiplier_after"] - round(indep_after[t], 4)) < 1e-9
               for t in printed), printed)
        ok("T11 the printed guidance tier is a real derive_guidance tier",
           all(("conviction" in (p["tier_before"] or "") or "DATA" in (p["tier_before"] or ""))
               for p in printed.values()), printed)

        # --- T5/T6/T7/T17: --apply ------------------------------------------------
        proc, out = _run(db, sidecar, "benchmark-backfill", "--apply")
        ok("T5 apply exits 0", proc.returncode == 0, proc.stderr[-400:])
        ok("T5 apply reports applied", out and out.get("applied") is True, out)
        ok("T6 the 7 WRONG rows are corrected to the true window return",
           all(abs(_col(db, ids[f"WRG{i}"], "benchmark_return") - TRUE_WINDOW) < 1e-6
               for i in range(7)), _col(db, ids["WRG0"], "benchmark_return"))
        ok("T6 the corrected value is NOT the old wrong one",
           abs(_col(db, ids["WRG0"], "benchmark_return") - WRONG_WINDOW) > 1e-6)
        ok("T6 alpha recomputed from the corrected benchmark",
           abs(_col(db, ids["WRG0"], "alpha") - (WRG_DRET - TRUE_WINDOW)) < 1e-6,
           _col(db, ids["WRG0"], "alpha"))
        ok("T7 the NULL rows are filled",
           all(_col(db, ids[f"NUL{i}"], "benchmark_return") is not None for i in range(5)))

        # T5 sentinels: a narrow UPDATE preserves these; INSERT OR REPLACE would blank them.
        ok("T5 resolved_at is byte-identical (proves UPDATE, not INSERT OR REPLACE)",
           _col(db, ids["WRG0"], "resolved_at") == "2026-07-06T21:25:17-04:00",
           _col(db, ids["WRG0"], "resolved_at"))
        ok("T5 holding_days preserved", _col(db, ids["WRG0"], "holding_days") == 28)
        ok("T5 scored_against preserved", _col(db, ids["NUL0"], "scored_against") == "both")
        ok("T5 adherence preserved", _col(db, ids["NUL0"], "adherence") == "within")
        ok("T5 realized_pnl preserved", _col(db, ids["NUL0"], "realized_pnl") == 4.0)
        ok("T5 directional_return preserved",
           _col(db, ids["WRG0"], "directional_return") == WRG_DRET)
        # The unscorable row: its window resolves, but with no position leg there is no
        # excess to compute, so alpha must stay NULL.
        ok("T17 the no-position-leg row still gets a benchmark",
           _col(db, ids["NODR"], "benchmark_return") is not None)
        ok("T17 but its alpha stays NULL (no directional_return to take an excess of)",
           _col(db, ids["NODR"], "alpha") is None, _col(db, ids["NODR"], "alpha"))

        # T17 — the subset invariant that keeps the graded sample size fixed.
        c = sqlite3.connect(str(db))
        viol = c.execute("SELECT COUNT(*) FROM outcomes WHERE alpha IS NOT NULL "
                         "AND directional_return IS NULL").fetchone()[0]
        c.close()
        ok("T17 alpha non-NULL implies directional_return non-NULL", viol == 0, viol)

        # T18 — the graded sample size must NOT move (no min_n cliff crossing).
        ok("T18 graded sample size per ticker is unchanged by the re-measurement",
           all(len(calibrate._graded_hits(Ledger(str(db)).decisions_with_outcomes(t, limit=20)))
               == len(calibrate._graded_hits(
                   Ledger(str(shadow)).decisions_with_outcomes(t, limit=20)))
               for t in names))

        # --- T8: idempotency -------------------------------------------------------
        settled = _digest(db)
        proc, out = _run(db, sidecar, "benchmark-backfill", "--apply")
        ok("T8 re-apply reports zero changes", out and out.get("changes") == 0, out)
        ok("T8 re-apply leaves the DB byte-identical", _digest(db) == settled)

        # --- T9: the audit row -----------------------------------------------------
        c = sqlite3.connect(str(db))
        rows = c.execute("SELECT change_type, trigger, proof_json FROM strategy_change_log").fetchall()
        c.close()
        ok("T9 no active goal -> NO change-log row (a NULL goal_id row is invisible)",
           rows == [], rows)

        with tempfile.TemporaryDirectory() as td2:
            td2 = Path(td2)
            gdb, gside = td2 / "g.db", td2 / "b.json"
            gled, gids = _seed(gdb)
            _write_sidecar(gside)
            gled.set_strategy_goal_with_holdings(
                goal={"created_at": "t", "target_return_pct": 15, "horizon_months": 12,
                      "benchmark": "SGOV", "benchmark_annual_pct": 3.6, "constraint_note": "",
                      "macro_thesis_version": "v", "macro_thesis_json": "{}",
                      "active_book": "core", "as_of": "x", "start_date": "2026-01-01",
                      "start_equity": 1000.0}, holdings=[])
            _run(gdb, gside, "benchmark-backfill", "--apply")
            c = sqlite3.connect(str(gdb))
            grows = c.execute("SELECT change_type, trigger, proof_json FROM strategy_change_log").fetchall()
            c.close()
            ok("T9 with an active goal -> exactly one change-log row", len(grows) == 1, grows)
            ok("T9 row is change_type=measurement / trigger=benchmark-backfill",
               grows and grows[0][0] == "measurement" and grows[0][1] == "benchmark-backfill", grows)
            ok("T9 proof carries the counts and the calibration diff",
               grows and "counts" in json.loads(grows[0][2])
               and "calibration_diff" in json.loads(grows[0][2]), grows)

        # --- T10a/T10b: refusal clears, never preserves a bad value ----------------
        with tempfile.TemporaryDirectory() as td3:
            td3 = Path(td3)
            rdb, rside = td3 / "r.db", td3 / "b.json"
            rled, rids = _seed(rdb)
            # A TRUNCATED series: stops at 07-02, exactly the incident's shape.
            _write_sidecar(rside, series={k: v for k, v in SPY.items() if k <= "2026-07-02"})
            proc, out = _run(rdb, rside, "benchmark-backfill", "--apply")
            ok("T10 truncated series still exits 0 (refusal is not an error)",
               proc.returncode == 0, proc.stderr[-300:])
            ok("T10b a WRONG value is CLEARED, not preserved, on refusal",
               _col(rdb, rids["WRG0"], "benchmark_return") is None,
               _col(rdb, rids["WRG0"], "benchmark_return"))
            ok("T10b its alpha is cleared too",
               _col(rdb, rids["WRG0"], "alpha") is None, _col(rdb, rids["WRG0"], "alpha"))
            ok("T10a an already-NULL row stays NULL",
               _col(rdb, rids["NUL0"], "benchmark_return") is None)
            ok("T10 the refusal reason is surfaced, not silent",
               out and out.get("refusals"), out)
            ok("T10 the refusal names the missing session",
               out and any("2026-07-06" in r for r in out["refusals"]), out.get("refusals"))

        # --- T12: cmd_reflect ignores an orchestrator-supplied benchmark ----------
        with tempfile.TemporaryDirectory() as td4:
            td4 = Path(td4)
            fdb, fside = td4 / "f.db", td4 / "b.json"
            fled = Ledger(str(fdb))
            fid = fled.record_decision(trade_date="2026-06-08", ticker="RFL", run_id="r",
                                       signal="Buy", intent="buy",
                                       decided_at="2026-06-08T10:00:00-04:00",
                                       decision_price=100.0)
            _write_sidecar(fside)
            rin = td4 / "reflect_input.json"
            rin.write_text(json.dumps({"resolutions": [
                {"decision_id": fid, "price_now": 110.0, "benchmark_return": 0.04}]}))
            proc, out = _run(fdb, fside, "reflect", "--input", str(rin))
            ok("T12 reflect exits 0", proc.returncode == 0, proc.stderr[-400:])
            ok("T12 reflect still resolves the outcome", out and out.get("resolved") == 1, out)
            ok("T12 the position leg is still computed",
               abs((_col(fdb, fid, "directional_return") or 0) - 0.10) < 1e-6)
            # The exact assertion: NULL, not "the sidecar value or NULL".
            ok("T12 orchestrator-supplied benchmark_return is IGNORED (column NULL)",
               _col(fdb, fid, "benchmark_return") is None, _col(fdb, fid, "benchmark_return"))
            ok("T12 alpha is therefore NULL (never 0.10-0.04=0.06)",
               _col(fdb, fid, "alpha") is None, _col(fdb, fid, "alpha"))
            ok("T12 the ignore is reported through the real CLI, not silent",
               out and out.get("ignored_benchmark_returns") == 1, out)

        # --- max-rows: the unattended path cannot land a backlog ------------------
        with tempfile.TemporaryDirectory() as td5:
            td5 = Path(td5)
            mdb, mside = td5 / "m.db", td5 / "b.json"
            _seed(mdb)
            _write_sidecar(mside)
            mbefore = _digest(mdb)
            proc, out = _run(mdb, mside, "benchmark-backfill", "--apply",
                             "--fill-only", "--max-rows", "2")
            ok("max-rows: a backlog over the cap is REFUSED", out and out.get("applied") is False, out)
            ok("max-rows: refusing writes nothing", _digest(mdb) == mbefore)
            ok("max-rows: the reason names the reviewed path",
               out and "reviewed" in (out.get("reason") or ""), out.get("reason"))
            proc, out = _run(mdb, mside, "benchmark-backfill", "--apply",
                             "--fill-only", "--max-rows", "50")
            ok("max-rows: under the cap it applies", out and out.get("applied") is True, out)
            ok("fill-only NEVER overwrites an existing value (the 7 wrong rows are untouched)",
               abs(_col(mdb, 1, "benchmark_return") - WRONG_WINDOW) < 1e-9,
               _col(mdb, 1, "benchmark_return"))

        # --- BM1: the PRODUCER/CONSUMER PAIR (the regression that got shipped) -----
        # The guard is "the last XNYS session on or before the target must be present in
        # the series". Its strength depends entirely on the calendar the PRODUCER emits.
        # When cmd_benchmark_fetch bounded that calendar by max(series), max(sessions)
        # could never exceed the series, so for any target past the series end the anchor
        # collapsed onto the series' own last close -- the refusal was structurally dead
        # at the tail, which is the ONLY place the original incident occurred. Verified:
        # it wrote 0.007521520275593348 (bit-for-bit the incident) with zero refusals.
        # Neither the module test nor the e2e caught it, because both hand-wrote a
        # calendar the producer could not emit. This pins the PAIR.
        with tempfile.TemporaryDirectory() as td6:
            td6 = Path(td6)
            bdb, bside = td6 / "b.db", td6 / "b.json"
            _, bids = _seed(bdb)
            trunc = {k: v for k, v in SPY.items() if k <= "2026-07-02"}
            psess = _producer_sessions(trunc)
            ok("BM1 the producer's calendar extends PAST the series (so the tail guard can fire)",
               max(psess) > max(trunc), (max(psess), max(trunc)))
            ok("BM1 it therefore contains the session the truncated series lacks",
               "2026-07-06" in psess)
            _write_sidecar(bside, series=trunc)          # sessions derived by the producer rule
            proc, out = _run(bdb, bside, "benchmark-backfill", "--apply",
                             "--fill-only", "--max-rows", "12")
            got = _col(bdb, bids["NUL0"], "benchmark_return")
            ok("BM1 a truncated series REFUSES instead of anchoring on its own last close",
               got is None, got)
            ok("BM1 it specifically does NOT write the incident value",
               got is None or abs(got - 0.007521520275593348) > 1e-12, got)
            ok("BM1 the refusal is surfaced", out and out.get("refusals"), out)

            # And the pure-module half: a calendar that stops short must refuse on
            # COVERAGE, so the rule cannot silently degrade into a walk-back.
            from lib import benchmark as _bmod
            short = [s for s in psess if s <= "2026-07-02"]
            v, why = _bmod.window_return(trunc, "2026-06-08", "2026-07-06", short)
            ok("BM1 a calendar ending before the target refuses on coverage", v is None, v)
            ok("BM1 the coverage refusal explains itself", "calendar ends" in (why or ""), why)

        # --- SAME-TICK: a row resolved during a still-open session must NOT be filled --
        # run_tick.py fetches and backfills in the SAME tick, minutes after reflect stamped
        # resolved_at=now. yfinance publishes no bar for an in-progress equity session, so
        # the series ends at the PRIOR session while the calendar extends past it -- the
        # row's own session is therefore missing and the fill is refused until tomorrow.
        # Without the calendar reaching past the series this would instead anchor on
        # yesterday's close and write a window that is a full session short.
        with tempfile.TemporaryDirectory() as td7:
            td7 = Path(td7)
            sdb, sside = td7 / "s.db", td7 / "b.json"
            sled = Ledger(str(sdb))
            sid = sled.record_decision(trade_date="2026-06-08", ticker="TDY", run_id="r",
                                       signal="Buy", intent="buy",
                                       decided_at="2026-06-08T10:00:00-04:00",
                                       decision_price=100.0)
            # resolved TODAY (2026-07-06 here), series stops at the prior session.
            sled.record_outcome(sid, resolved_at="2026-07-06T14:05:00-04:00", holding_days=28,
                                directional_return=0.02, scored_against="directional")
            _write_sidecar(sside, series={k: v for k, v in SPY.items() if k <= "2026-07-02"})
            proc, out = _run(sdb, sside, "benchmark-backfill", "--apply",
                             "--fill-only", "--max-rows", "12")
            got = _col(sdb, sid, "benchmark_return")
            ok("same-tick: an in-progress session is NOT filled from the prior close",
               got is None, got)
            ok("same-tick: alpha stays NULL until the session settles",
               _col(sdb, sid, "alpha") is None)
            ok("same-tick: the refusal is reported", out and out.get("refusals"), out)

    return 1 if FAIL else 0


if __name__ == "__main__":
    rc = main()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else rc)
