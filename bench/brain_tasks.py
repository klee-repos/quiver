"""Phase 8a — frozen-fixture brain regression.

WHAT THIS IS FOR. The "0 bulls in 22 sessions" collapse took 22 live sessions to notice. Replaying
the brain against FROZEN analyst reports turns that into a minutes-long offline check: the inputs
are fixed, so any shift in the signal distribution is attributable to the prompt or the model,
not to the market. This is the direct regression gate for a prompt change.

THREE HONEST LIMITS, STATED UP FRONT.

1. **It is NOT free and NOT fully offline.** `QUIVER_REPLAY_REPORTS` stubs only the GATHER turn.
   `deepTurn`/`quickTurn` still call the live provider, so a run bills real tokens. It is
   therefore gated on an EXPLICIT `--live` / `QUIVER_LIVE_E2E=1`, never on API-key presence —
   `lib/config.py` `load_dotenv` injects the real key into `os.environ`, so "no key" is never
   the state of a default run and key-gating would silently bill the default suite.

2. **The fixtures are partial.** The audit JSONs carry `market_report`, `fundamentals_report`,
   `news_report`, `sentiment_report` — but NOT `trend_report` or `macro_report`, which replay as
   `UNAVAILABLE: not in replay fixture`. So this measures the brain with two channels dark. That
   is a legitimate regression harness (the condition is CONSTANT across runs) but it is NOT the
   production input, and a signal distribution measured here is not the live one.

3. **`temperature: 0` is not determinism** against a hosted model. Run `rollouts > 1` and read
   the SPREAD; a single rollout is an anecdote.
"""
from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Sequence

_REPO = Path(__file__).resolve().parent.parent

# Reports `loadReplayReports` looks for, and the subset the audit fixtures actually carry.
REPLAY_REPORT_KEYS = ("market_report", "trend_report", "fundamentals_report",
                      "news_report", "sentiment_report", "macro_report")


def discover_fixtures(fixture_dir=None) -> list:
    """-> [{'path','ticker','date','present_reports','missing_reports'}] sorted by (date,ticker)."""
    d = Path(fixture_dir or (_REPO / "state" / "analyze_logs"))
    out = []
    for p in sorted(d.glob("*.json")):
        stem = p.stem
        if "_" not in stem:
            continue
        date, _, ticker = stem.partition("_")
        try:
            j = json.loads(p.read_text())
        except Exception:  # noqa: BLE001 — a corrupt fixture is data, not a crash
            continue
        present = [k for k in REPLAY_REPORT_KEYS if str(j.get(k) or "").strip()]
        out.append({"path": str(p), "ticker": ticker, "date": date,
                    "present_reports": present,
                    "missing_reports": [k for k in REPLAY_REPORT_KEYS if k not in present]})
    return out


def run_one(fixture: dict, *, timeout: int = 900, env: Optional[dict] = None) -> dict:
    """Replay ONE fixture through the real decide.mjs. Returns a ledger-shaped decision row."""
    e = dict(os.environ)
    e["QUIVER_REPLAY_REPORTS"] = fixture["path"]
    e.update(env or {})
    try:
        p = subprocess.run(["node", "quiver_eve/run/decide.mjs", fixture["ticker"], fixture["date"]],
                           cwd=str(_REPO), input="", capture_output=True, text=True,
                           timeout=timeout, env=e)
        text = p.stdout or ""
    except subprocess.TimeoutExpired:
        return _row(fixture, "ERROR", detail="timeout")
    except FileNotFoundError as ex:
        return _row(fixture, "ERROR", detail=f"node missing: {ex}")
    if p.returncode != 0 and not text.strip():
        return _row(fixture, "ERROR", detail=f"rc={p.returncode} {(p.stderr or '')[-200:]}")
    return parse_decision(fixture, text)


def parse_decision(fixture: dict, text: str) -> dict:
    """Parse through the SAME gate production uses.

    `lib.rating.parse_rating(text, default) -> Optional[str]` returns a RATING STRING (not a
    dict), and is called here with `default=None` deliberately: the docstring is explicit that
    a caller which must fail-safe passes None so a total miss surfaces as a contract violation
    (-> `signal: ERROR`) instead of a silent bearish `Hold`. A regression harness that scored an
    unparseable reply as Hold would report a bearish collapse as normal behaviour — the exact
    bug this harness exists to detect.

    `conviction` is left None: `parse_rating` does not extract one, and inventing a value would
    poison D5 (conviction calibration) with a constant.
    """
    try:
        from lib.rating import parse_rating
        signal = parse_rating(text, default=None)
    except Exception as e:  # noqa: BLE001 — a parser crash is an ERROR row, not a dead sweep
        return _row(fixture, "ERROR", detail=f"parse_rating raised: {type(e).__name__}")
    if not signal:
        return _row(fixture, "ERROR", detail="no parseable rating (contract violation)")
    return _row(fixture, str(signal).strip(), conviction=None)


def _row(fixture: dict, signal: str, *, conviction=None, detail: str = "") -> dict:
    return {"id": f"{fixture['date']}_{fixture['ticker']}", "trade_date": fixture["date"],
            "ticker": fixture["ticker"], "signal": signal,
            "intent": {"buy": "buy", "overweight": "buy", "sell": "sell",
                       "underweight": "sell"}.get(signal.lower(), "hold"),
            "conviction": conviction,
            "data_quality": ("unavailable: " + ",".join(fixture["missing_reports"])
                             if fixture["missing_reports"] else ""),
            "detail": detail}


def run_task_set(fixture_dir=None, *, rollouts: int = 1, workers: int = 4,
                 limit: Optional[int] = None, timeout: int = 900) -> dict:
    """Replay every fixture `rollouts` times and score the result with the behavioral scorecard."""
    from bench import diagnostics as D
    fixtures = discover_fixtures(fixture_dir)
    if limit:
        fixtures = fixtures[:limit]
    jobs = [(f, r) for f in fixtures for r in range(rollouts)]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        rows = list(ex.map(lambda fr: run_one(fr[0], timeout=timeout), jobs))
    for i, row in enumerate(rows):
        row["rollout"] = jobs[i][1]
    sc = D.scorecard(rows, [], [])
    spread = {}
    for row in rows:
        spread.setdefault(row["id"], set()).add(row["signal"])
    unstable = {k: sorted(v) for k, v in spread.items() if len(v) > 1}
    return {"rows": rows, "scorecard": sc, "n_fixtures": len(fixtures), "rollouts": rollouts,
            "unstable_across_rollouts": unstable,
            "note": ("temperature=0 is not determinism against a hosted model; "
                     f"{len(unstable)}/{len(spread)} fixtures gave >1 distinct signal")}
