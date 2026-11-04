"""The learning loop (plan §6.5): the decision journal, its grades, the lessons and the
owner's rules.

The loop has four parts.

1. **The journal.** Each analyst verdict, each deep-dive and each trade or change card
   writes one ``forecasts`` row when the call is made: the chance (``p_beat``) that the
   name's price return beats the benchmark's over the horizon. Python sets the horizon
   (``horizon_for``). The window starts at the first close after the call, so the call
   cannot see any part of it.
2. **The grade.** ``grade`` takes the marks at 30, 90, 180 and 365 days from market
   closes, with the strict anchors of ``lib.benchmark`` (no stale close stands in). A mark
   waits until its date's close is final. At the horizon the forecast resolves: the
   outcome (1 when the name beat the benchmark), the excess return and the Brier score.
   A fetched series with no close ``MARK_VOID_DAYS`` after a mark's date makes the mark
   void; a void forecast has no outcome and counts in no score. A failed or empty fetch
   never voids a mark. No model writes a grade.
3. **The lessons.** The reflect job (an Opus model) reads the resolved forecasts and
   proposes changes to a short list of lessons. ``apply_ops`` checks each change and merges
   it: a new lesson cites at least 3 resolved forecasts, and a general lesson cites at
   least 2 names. A close copy of an active lesson, or of a lesson retired for a cause, is
   refused. Python counts each lesson's graded uses (``helpful`` / ``harmful``) and retires a
   lesson that is wrong 3 or more times more than right, or that stays untested.
   ``lesson_log`` keeps every change, append-only.
4. **The owner's rules.** A rule comes only from a desk turn with an owner message, and the
   owner gets a fixed notice with each new or retired rule (a planted rule is seen at once).
   Every desk turn and every analyst job sees the active rules. A rule outranks a lesson.

This module is on the research side of the decision wall (``tests/test_units.py`` W2). It
reads no limit, sizes nothing and approves nothing. Its output goes to the models as
context only.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from lib import benchmark as bench_mod
from lib import market
from lib.desk import store

BENCHMARK = "SPY"
MARKS = (30, 90, 180, 365)
DEFAULT_HORIZON = 90
# A news headline or a price level is a short-term call: it resolves in 30 days. A filing,
# results, a deep-dive or a card resolves in 90 days (about one quarter).
SHORT_HORIZON_EVENTS = ("news", "price_level")
SHORT_HORIZON = 30
# A mark with no close this many days after its date is void (the name stopped trading, or
# its symbol changed). A void forecast has no outcome and counts in no score.
MARK_VOID_DAYS = 30
P_LOW, P_HIGH = 0.01, 0.99
REASON_MAX = 400
SMALL_SAMPLE = 20
BINS = 10

LESSON_MIN, LESSON_MAX = 20, 280
LESSON_EVIDENCE_MIN = 3
LESSON_GENERAL_NAMES_MIN = 2
LESSONS_ACTIVE_MAX = 40
LESSON_ADDS_PER_JOB = 5
LESSON_RETIRES_PER_JOB = 5
LESSON_DUP_RATIO = 0.8
LESSON_NET_HARM_RETIRE = 3
LESSON_UNTESTED_DAYS = 180
LESSON_TESTED_MIN = 3
DESK_LESSONS = 5
ANALYST_LESSONS = 8
# A lesson is advice about research and judgment. It may not speak to the gates.
LESSON_FORBIDDEN = re.compile(r"mcp__|\bapprov|\btaps?\b|\bKILL\b|\bhalt|buying power|\binstructions?\b|"
                              r"system prompt", re.IGNORECASE)

RULE_MIN, RULE_MAX = 5, 300
RULES_ACTIVE_MAX = 20

REFLECT_MIN_NEW = 5
REFLECT_MIN_GAP_DAYS = 6
REFLECT_DETAIL_MAX = 40
REFLECT_GROUP_IDS_MAX = 60

# Proposal states, grouped by what the owner did with the card.
TAKEN = ("executed", "partially_executed", "applied")
REFUSED = ("rejected",)
LAPSED = ("expired", "superseded", "withdrawn", "requoted", "failed")


class ForecastError(ValueError):
    """A refused forecast. The text goes back to the model."""


class LessonError(ValueError):
    """A refused lesson or rule change. The text goes back to the model."""


# --- the journal ---------------------------------------------------------------------

def horizon_for(source: str, event_type: Optional[str] = None) -> int:
    """The horizon in calendar days. Python sets it, so every call of one kind is graded
    over the same window."""
    if source == "verdict" and event_type in SHORT_HORIZON_EVENTS:
        return SHORT_HORIZON
    return DEFAULT_HORIZON


def start_session_for(made_at: datetime) -> str:
    """The session whose close starts the window: the session open at ``made_at``, else the
    next one. A call made after the close starts at the next session's close."""
    return market.session_close_et(made_at)[0]


def add_days(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=int(days))).isoformat()


def check_p(p, *, required: bool) -> Optional[float]:
    """The stated chance as a float in [0.01, 0.99], or None when it may be absent."""
    if p is None or p == "":
        if required:
            raise ForecastError("give p_beat_benchmark: the chance (0.01 to 0.99) that the name beats "
                                f"{BENCHMARK} over the horizon")
        return None
    try:
        v = float(p)
    except (TypeError, ValueError):
        raise ForecastError("p_beat_benchmark must be a number from 0.01 to 0.99")
    if v != v or not (P_LOW <= v <= P_HIGH):
        raise ForecastError("p_beat_benchmark must be from 0.01 to 0.99 (0.5 means no edge)")
    return round(v, 4)


def stance_of(p: Optional[float]) -> int:
    if p is None or p == 0.5:
        return 0
    return 1 if p > 0.5 else -1


def check_lessons_used(conn, ids) -> list:
    """The active lesson ids a call says it used. An unknown or retired id is refused."""
    if ids is not None and not isinstance(ids, (list, tuple)):
        raise ForecastError("lessons_used must be a list of lesson ids")
    out = []
    for raw in ids or []:
        try:
            lid = int(raw)
        except (TypeError, ValueError):
            raise ForecastError(f"lessons_used holds {raw!r}, not a lesson id")
        row = conn.execute("SELECT status FROM lessons WHERE id=?", (lid,)).fetchone()
        if row is None or row[0] != "active":
            raise ForecastError(f"lesson {lid} is not an active lesson")
        if lid not in out:
            out.append(lid)
    return out


def record_forecast(conn, *, source: str, ticker: str, p_beat, stance: int, made_at: datetime,
                    created_by: str, reason: str, horizon_days: int, benchmark: str = BENCHMARK,
                    verdict=None, event_id=None, job_id=None, proposal_id=None, lessons_used=(),
                    start_session: Optional[str] = None) -> int:
    """Write one forecast. ``made_at`` is a timezone-aware datetime. Returns the row id."""
    t = str(ticker or "").upper().strip()
    if not t:
        raise ForecastError("a forecast needs a ticker")
    if t == benchmark:
        raise ForecastError(f"{t} is the benchmark; a forecast against itself means nothing")
    if source not in ("verdict", "deep_dive", "proposal"):
        raise ForecastError(f"unknown forecast source {source!r}")
    if stance not in (-1, 0, 1):
        raise ForecastError("stance must be -1, 0 or 1")
    p = check_p(p_beat, required=False)
    text = " ".join(str(reason or "").split())
    if not text:
        raise ForecastError("give a reason for the forecast")
    lessons = check_lessons_used(conn, lessons_used)
    start = start_session or start_session_for(made_at)
    cur = conn.execute(
        "INSERT INTO forecasts (source, ticker, benchmark, p_beat, stance, horizon_days, made_at, start_session, "
        "due_date, verdict, event_id, job_id, proposal_id, created_by, reason, lesson_ids) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, t, benchmark, p, int(stance), int(horizon_days), store.iso(made_at), start,
         add_days(start, horizon_days), verdict, event_id, job_id, proposal_id, created_by,
         text[:REASON_MAX], json.dumps(lessons)))
    return int(cur.lastrowid)


# --- the grade -----------------------------------------------------------------------

def _row(r) -> dict:
    d = dict(r)
    d["marks"] = store.loads(d.get("marks_json"), {}) or {}
    d["lessons"] = store.loads(d.get("lesson_ids"), []) or []
    return d


def due_marks(fc: dict, final_through: str) -> list:
    """The mark days of one forecast whose date has a final close and that have no mark yet.
    The horizon always counts as a mark day."""
    days = sorted(set(MARKS) | {int(fc["horizon_days"])})
    return [d for d in days if str(d) not in fc["marks"] and add_days(fc["start_session"], d) <= final_through]


def open_forecasts(conn, final_through: str) -> list:
    """Every forecast with at least one due mark."""
    rows = [_row(r) for r in conn.execute(
        "SELECT * FROM forecasts WHERE start_session <= ? ORDER BY id", (final_through,)).fetchall()]
    return [fc for fc in rows if due_marks(fc, final_through)]


def tickers_due(conn, final_through: str) -> dict:
    """``{ticker: earliest start session}`` for every name and benchmark with a due mark."""
    out = {}
    for fc in open_forecasts(conn, final_through):
        for t in (fc["ticker"], fc["benchmark"]):
            if t not in out or fc["start_session"] < out[t]:
                out[t] = fc["start_session"]
    return out


def brier(p: float, outcome: int) -> float:
    return round((float(p) - int(outcome)) ** 2, 6)


def grade(conn, *, series: dict, sessions: Iterable[str], final_through: str, now_iso: str) -> dict:
    """Take every due mark from ``series`` (``{ticker: {YYYY-MM-DD: close}}``) and resolve
    each forecast at its horizon. ``final_through`` is the last session whose close is
    final. A mark with a missing close waits for the next run: no other close stands in.
    A name or benchmark that is not in ``series``, or whose series is empty (its fetch
    failed), waits too, and never goes void.

    Returns the counts and the ids of the forecasts that resolved in this run."""
    sessions = sorted(set(sessions))
    marked = waiting = void = 0
    resolved = []
    for fc in open_forecasts(conn, final_through):
        st, sb = series.get(fc["ticker"]) or {}, series.get(fc["benchmark"]) or {}
        if not st or not sb:
            waiting += len(due_marks(fc, final_through))
            continue
        marks = dict(fc["marks"])
        for d in due_marks(fc, final_through):
            target = add_days(fc["start_session"], d)
            r, _why = bench_mod.window_return(st, fc["start_session"], target, sessions)
            b, _why_b = bench_mod.window_return(sb, fc["start_session"], target, sessions)
            if r is None or b is None:
                if add_days(target, MARK_VOID_DAYS) <= final_through:
                    marks[str(d)] = {"missing": True, "at": now_iso}
                    void += 1
                else:
                    waiting += 1
                continue
            anchor, _ = bench_mod.session_anchor(st, target, sessions)
            marks[str(d)] = {"session": anchor, "ret": round(r, 6), "bench": round(b, 6),
                             "excess": round(r - b, 6), "at": now_iso}
            marked += 1
        if marks == fc["marks"]:
            continue
        conn.execute("UPDATE forecasts SET marks_json=? WHERE id=?", (json.dumps(marks, sort_keys=True), fc["id"]))
        h = marks.get(str(fc["horizon_days"]))
        if h is not None and h.get("missing") and fc["resolved_at"] is None:
            conn.execute("UPDATE forecasts SET resolved_at=? WHERE id=? AND resolved_at IS NULL", (now_iso, fc["id"]))
        elif h is not None and fc["resolved_at"] is None:
            outcome = 1 if h["excess"] > 0 else 0
            score = brier(fc["p_beat"], outcome) if fc["p_beat"] is not None else None
            conn.execute("UPDATE forecasts SET resolved_at=?, outcome=?, excess=?, brier=? WHERE id=? "
                         "AND resolved_at IS NULL", (now_iso, outcome, h["excess"], score, fc["id"]))
            resolved.append(fc["id"])
            _tag_lessons(conn, {**fc, "outcome": outcome, "excess": h["excess"], "brier": score}, now_iso)
    retired = maintain_lessons(conn, now_iso)
    return {"marked": marked, "waiting": waiting, "void": void, "resolved": len(resolved), "resolved_ids": resolved,
            "lessons_retired": retired}


def call_was_right(fc: dict) -> Optional[bool]:
    """True when a resolved call was on the right side, False when on the wrong side, None
    when it took no side (a chance of 0.5, or a card with no side)."""
    if fc.get("outcome") is None:
        return None
    if fc.get("p_beat") is not None:
        if fc["p_beat"] == 0.5:
            return None
        return (fc["p_beat"] > 0.5) == bool(fc["outcome"])
    if not fc.get("stance") or not fc.get("excess"):
        return None
    return fc["stance"] * fc["excess"] > 0


def _tag_lessons(conn, fc: dict, now_iso: str) -> None:
    """ACE's counters, from the ground truth: each active lesson the resolved call cited
    counts one helpful (the call was right) or one harmful (it was wrong)."""
    right = call_was_right(fc)
    if right is None:
        return
    col, op = ("helpful", "helpful") if right else ("harmful", "harmful")
    for lid in fc.get("lessons") or []:
        cur = conn.execute(f"UPDATE lessons SET {col}={col}+1, updated_at=? WHERE id=? AND status='active'",
                           (now_iso, int(lid)))
        if cur.rowcount:
            _log(conn, int(lid), op, {"forecast_id": fc["id"]}, None, now_iso)


def maintain_lessons(conn, now_iso: str) -> list:
    """Retire a lesson that does worse than a coin flip (``LESSON_NET_HARM_RETIRE`` more
    wrong than right) or that has fewer than ``LESSON_TESTED_MIN`` graded uses after
    ``LESSON_UNTESTED_DAYS`` days. Returns the retired ids."""
    now_dt = store.parse_ts(now_iso)
    out = []
    for r in conn.execute("SELECT * FROM lessons WHERE status='active' ORDER BY id").fetchall():
        reason = None
        if r["harmful"] - r["helpful"] >= LESSON_NET_HARM_RETIRE:
            reason = f"the calls that used it were wrong more often than right ({r['helpful']} right, {r['harmful']} wrong)"
        else:
            made = store.parse_ts(r["created_at"])
            if (made and now_dt and (now_dt - made).days >= LESSON_UNTESTED_DAYS
                    and r["helpful"] + r["harmful"] < LESSON_TESTED_MIN):
                reason = f"fewer than {LESSON_TESTED_MIN} graded calls used it in {LESSON_UNTESTED_DAYS} days"
        if reason:
            _retire(conn, r["id"], reason, None, now_iso, by="python")
            out.append(int(r["id"]))
    return out


# --- the scores ----------------------------------------------------------------------

def brier_stats(rows: list) -> dict:
    """The Brier score of resolved forecasts that state a chance, with the Murphy split:
    ``brier ~= reliability - resolution + uncertainty``. ``reliability`` measures the gap
    between the stated chances and what happened (lower is better). ``resolution``
    measures how much the calls tell good names from bad (higher is better). ``skill``
    compares the Brier score with the climatology forecast (the base rate every time):
    above 0 beats it. ``coin`` is 0.25, the score of 0.5 every time."""
    pts = [(float(r["p_beat"]), int(r["outcome"])) for r in rows
           if r.get("p_beat") is not None and r.get("outcome") is not None]
    n = len(pts)
    if not n:
        return {"n": 0, "small_sample": True}
    scores = [(p - o) ** 2 for p, o in pts]
    bs = sum(scores) / n
    base = sum(o for _, o in pts) / n
    unc = base * (1 - base)
    bins = {}
    for p, o in pts:
        k = min(int(p * BINS), BINS - 1)
        bins.setdefault(k, []).append((p, o))
    rel = res = 0.0
    table = []
    for k in sorted(bins):
        grp = bins[k]
        mp = sum(p for p, _ in grp) / len(grp)
        fo = sum(o for _, o in grp) / len(grp)
        rel += len(grp) * (mp - fo) ** 2
        res += len(grp) * (fo - base) ** 2
        table.append({"from": round(k / BINS, 2), "to": round((k + 1) / BINS, 2), "n": len(grp),
                      "mean_p": round(mp, 3), "beat_rate": round(fo, 3)})
    sd = math.sqrt(sum((s - bs) ** 2 for s in scores) / (n - 1)) if n > 1 else None
    return {"n": n, "brier": round(bs, 4), "brier_se": round(sd / math.sqrt(n), 4) if sd is not None else None,
            "coin": 0.25, "base_rate": round(base, 3), "climatology": round(unc, 4),
            "skill": round(1 - bs / unc, 3) if unc > 0 else None,
            "reliability": round(rel / n, 4), "resolution": round(res / n, 4), "uncertainty": round(unc, 4),
            "sharpness": round(sum(abs(p - 0.5) for p, _ in pts) / n, 3), "bins": table,
            "small_sample": n < SMALL_SAMPLE}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _side_stats(rows: list) -> dict:
    """Right-side rate and mean excess at each mark for a group of calls."""
    judged = [call_was_right(r) for r in rows]
    judged = [j for j in judged if j is not None]
    out = {"n": len(rows), "resolved": sum(1 for r in rows if r.get("outcome") is not None),
           "right_rate": round(sum(judged) / len(judged), 3) if judged else None}
    for d in MARKS:
        out[f"excess_{d}d"] = _mean([(r["marks"].get(str(d)) or {}).get("excess") for r in rows])
    return out


def calibration(conn, *, ticker: Optional[str] = None, created_by: Optional[str] = None) -> dict:
    """The track record of the journal. Every number comes from graded rows only."""
    sql, args = "SELECT f.*, p.state AS card_state FROM forecasts f LEFT JOIN proposals p ON p.id=f.proposal_id", []
    where = []
    if ticker:
        where.append("f.ticker=?")
        args.append(str(ticker).upper())
    if created_by:
        where.append("f.created_by=?")
        args.append(created_by)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = [_row(r) for r in conn.execute(sql + " ORDER BY f.id", args).fetchall()]
    resolved = [r for r in rows if r["outcome"] is not None]
    pending = [r for r in rows if r["resolved_at"] is None]
    out = {"benchmark": BENCHMARK, "forecasts": len(rows), "resolved": len(resolved), "open": len(pending),
           "void": len(rows) - len(resolved) - len(pending),
           "next_due": min((r["due_date"] for r in pending), default=None),
           "overall": brier_stats(resolved),
           "by_source": {s: brier_stats([r for r in resolved if r["source"] == s])
                         for s in ("verdict", "deep_dive", "proposal") if any(r["source"] == s for r in resolved)},
           "by_verdict": {v: _side_stats([r for r in rows if r["verdict"] == v])
                          for v in ("strengthened", "intact", "weakened", "broken")
                          if any(r["verdict"] == v for r in rows)}}
    cards = [r for r in rows if r["source"] == "proposal"]
    for r in cards:
        r["card_state"] = final_card_state(conn, r.get("proposal_id"), r.get("card_state"))
    groups = {"taken": TAKEN, "refused": REFUSED, "lapsed": LAPSED}
    out["cards"] = {g: _side_stats([r for r in cards if r.get("card_state") in states])
                    for g, states in groups.items() if any(r.get("card_state") in states for r in cards)}
    out["small_sample"] = out["overall"].get("small_sample", True)
    return out


def final_card_state(conn, proposal_id, state: Optional[str]) -> Optional[str]:
    """The state that counts for a card. A requoted card is the same call with new numbers:
    Python builds the new card (``superseded_by``), so the new card's state counts."""
    seen = set()
    pid = proposal_id
    while state == "requoted" and pid is not None and pid not in seen and len(seen) < 10:
        seen.add(pid)
        row = conn.execute("SELECT superseded_by FROM proposals WHERE id=?", (pid,)).fetchone()
        nxt = row[0] if row else None
        if nxt is None:
            break
        row = conn.execute("SELECT state FROM proposals WHERE id=?", (nxt,)).fetchone()
        if row is None:
            break
        pid, state = nxt, row[0]
    return state


def summary_line(cal: dict) -> str:
    """One plain line for a prompt: how good the graded calls are so far."""
    o = cal.get("overall") or {}
    if not o.get("n"):
        return (f"No forecast has resolved yet ({cal.get('open', 0)} open; the first is due "
                f"{cal.get('next_due') or 'when one is made'}). There is no track record to lean on.")
    line = (f"{o['n']} resolved forecasts: Brier {o['brier']} (0.25 is a coin flip; lower is better), "
            f"skill {o['skill']} against always guessing the base rate {o['base_rate']}, "
            f"reliability {o['reliability']}, resolution {o['resolution']}.")
    if o.get("small_sample"):
        line += f" Fewer than {SMALL_SAMPLE} graded calls: too few to judge skill."
    return line


def record_lines(conn, ticker: str, limit: int = 6) -> list:
    """The name's latest resolved forecasts, newest first, one plain line each."""
    out = []
    for r in conn.execute("SELECT * FROM forecasts WHERE ticker=? AND outcome IS NOT NULL ORDER BY resolved_at DESC, "
                          "id DESC LIMIT ?", (str(ticker).upper(), int(limit))).fetchall():
        fc = _row(r)
        what = fc["verdict"] or fc["source"]
        p = f"p {fc['p_beat']}" if fc["p_beat"] is not None else f"side {fc['stance']:+d}"
        right = call_was_right(fc)
        tail = "" if right is None else (", right side" if right else ", wrong side")
        out.append(f"[forecast {fc['id']}] {fc['made_at'][:10]} {what}, {p}: {fc['ticker']} "
                   f"{'beat' if fc['outcome'] else 'lagged'} {fc['benchmark']} by {abs(fc['excess']) * 100:.1f}% "
                   f"over {fc['horizon_days']} days{tail}"
                   + (f", Brier {fc['brier']}" if fc["brier"] is not None else ""))
    return out


# --- the lessons ---------------------------------------------------------------------

def _norm_text(text) -> str:
    return " ".join(str(text or "").split())


def _log(conn, lesson_id: int, op: str, detail: dict, job_id, now_iso: str) -> None:
    conn.execute("INSERT INTO lesson_log (lesson_id, op, detail_json, job_id, at) VALUES (?,?,?,?,?)",
                 (int(lesson_id), op, json.dumps(detail, sort_keys=True, default=str), job_id, now_iso))


def _retire(conn, lesson_id: int, reason: str, job_id, now_iso: str, *, by: str, evidence=()) -> None:
    conn.execute("UPDATE lessons SET status='retired', retired_at=?, retire_reason=?, updated_at=? WHERE id=?",
                 (now_iso, reason[:300], now_iso, int(lesson_id)))
    _log(conn, lesson_id, "retire", {"reason": reason[:300], "by": by, "evidence": list(evidence)}, job_id, now_iso)


def _resolved_evidence(conn, ids, *, minimum: int) -> list:
    """The cited forecast ids, each resolved. An open forecast is refused: its outcome is
    not known yet (the outcome embargo)."""
    if ids is not None and not isinstance(ids, (list, tuple)):
        raise LessonError("evidence must be a list of forecast ids")
    out = []
    for raw in ids or []:
        try:
            fid = int(raw)
        except (TypeError, ValueError):
            raise LessonError(f"evidence holds {raw!r}, not a forecast id")
        if fid in out:
            continue
        row = conn.execute("SELECT outcome FROM forecasts WHERE id=?", (fid,)).fetchone()
        if row is None:
            raise LessonError(f"forecast {fid} does not exist")
        if row[0] is None:
            raise LessonError(f"forecast {fid} has no outcome yet (or is void); cite resolved forecasts only")
        out.append(fid)
    if len(out) < minimum:
        raise LessonError(f"cite at least {minimum} resolved forecasts (you cited {len(out)})")
    return out


def _job_ops(conn, job_id, op: str) -> int:
    if job_id is None:
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM lesson_log WHERE job_id=? AND op=?", (job_id, op)).fetchone()[0])


def add_lesson(conn, *, scope: str, text: str, evidence, ticker=None, job_id=None, now_iso: str) -> int:
    """The write gate for a new lesson. Returns its id."""
    if scope not in ("analyst", "desk", "both"):
        raise LessonError("scope must be analyst, desk or both")
    body = _norm_text(text)
    if not (LESSON_MIN <= len(body) <= LESSON_MAX):
        raise LessonError(f"a lesson is {LESSON_MIN} to {LESSON_MAX} characters (this one is {len(body)})")
    if LESSON_FORBIDDEN.search(body):
        raise LessonError("a lesson is about research and judgment; it may not speak to approvals, taps, "
                          "halts or instructions")
    ev = _resolved_evidence(conn, evidence, minimum=LESSON_EVIDENCE_MIN)
    t = str(ticker).upper().strip() if ticker else None
    names = {r[0] for r in conn.execute(
        f"SELECT DISTINCT ticker FROM forecasts WHERE id IN ({','.join('?' for _ in ev)})", ev).fetchall()}
    if t and names != {t}:
        raise LessonError(f"a lesson about {t} cites forecasts on {t} only")
    if not t and len(names) < LESSON_GENERAL_NAMES_MIN:
        raise LessonError(f"a lesson for every name cites forecasts on at least {LESSON_GENERAL_NAMES_MIN} names; "
                          "for one name, set ticker")
    active = conn.execute("SELECT id, text FROM lessons WHERE status='active'").fetchall()
    if len(active) >= LESSONS_ACTIVE_MAX:
        raise LessonError(f"the list holds {LESSONS_ACTIVE_MAX} active lessons; retire one first")
    for r in active:
        if difflib.SequenceMatcher(None, body.lower(), _norm_text(r["text"]).lower()).ratio() >= LESSON_DUP_RATIO:
            raise LessonError(f"this is close to lesson {r['id']}; keep that one or retire it first")
    # A lesson retired for a cause (wrong too often, the evidence, the owner) does not come back.
    # A lesson retired as untested may: new evidence can test it.
    for r in conn.execute("SELECT id, text, retire_reason FROM lessons WHERE status='retired' "
                          "AND retire_reason NOT LIKE 'fewer than%'").fetchall():
        if difflib.SequenceMatcher(None, body.lower(), _norm_text(r["text"]).lower()).ratio() >= LESSON_DUP_RATIO:
            raise LessonError(f"this is close to lesson {r['id']}, retired because {r['retire_reason']}")
    if _job_ops(conn, job_id, "add") >= LESSON_ADDS_PER_JOB:
        raise LessonError(f"one reflection adds at most {LESSON_ADDS_PER_JOB} lessons")
    cur = conn.execute("INSERT INTO lessons (scope, ticker, text, evidence_json, status, created_at, updated_at) "
                       "VALUES (?,?,?,?, 'active', ?, ?)", (scope, t, body, json.dumps(ev), now_iso, now_iso))
    lid = int(cur.lastrowid)
    _log(conn, lid, "add", {"scope": scope, "ticker": t, "text": body, "evidence": ev}, job_id, now_iso)
    return lid


def retire_lesson(conn, lesson_id, *, reason: str, by: str, evidence=(), job_id=None, now_iso: str) -> None:
    """Retire an active lesson. The reflect job cites at least one resolved forecast; the
    owner gives a reason only."""
    try:
        lid = int(lesson_id)
    except (TypeError, ValueError):
        raise LessonError("lesson_id must be a lesson id")
    row = conn.execute("SELECT status FROM lessons WHERE id=?", (lid,)).fetchone()
    if row is None or row[0] != "active":
        raise LessonError(f"lesson {lid} is not an active lesson")
    why = _norm_text(reason)
    if len(why) < 5:
        raise LessonError("give a reason")
    ev = []
    if by == "reflect":
        ev = _resolved_evidence(conn, evidence, minimum=1)
        if _job_ops(conn, job_id, "retire") >= LESSON_RETIRES_PER_JOB:
            raise LessonError(f"one reflection retires at most {LESSON_RETIRES_PER_JOB} lessons")
    _retire(conn, lid, why, job_id, now_iso, by=by, evidence=ev)


def apply_ops(conn, ops, *, job_id, now_iso: str) -> dict:
    """The curator merge. Each op is checked and applied on its own: a refused op does not
    stop the others, and its reason goes back to the model."""
    if not isinstance(ops, list) or not ops:
        raise LessonError("ops must be a non-empty list of {op: add|retire, ...}")
    results = []
    for i, op in enumerate(ops):
        try:
            if not isinstance(op, dict):
                raise LessonError("each op is an object")
            kind = op.get("op")
            if kind == "add":
                lid = add_lesson(conn, scope=str(op.get("scope") or ""), text=op.get("text") or "",
                                 evidence=op.get("evidence"), ticker=op.get("ticker"), job_id=job_id, now_iso=now_iso)
                results.append({"op": i, "added": lid})
            elif kind == "retire":
                retire_lesson(conn, op.get("lesson_id"), reason=op.get("reason") or "", by="reflect",
                              evidence=op.get("evidence"), job_id=job_id, now_iso=now_iso)
                results.append({"op": i, "retired": int(op.get("lesson_id"))})
            else:
                raise LessonError("op must be add or retire")
        except LessonError as e:
            results.append({"op": i, "refused": str(e)})
    return {"results": results}


def lessons_for(conn, *, scope: str, ticker: Optional[str] = None, limit: int) -> list:
    """The active lessons a role sees: the name's own lessons first, then the general ones,
    each group by net graded uses, then the newest."""
    scopes = ("analyst", "both") if scope == "analyst" else ("desk", "both")
    t = str(ticker).upper() if ticker else None
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM lessons WHERE status='active' AND scope IN (?, ?) AND (ticker IS NULL OR ticker=?)",
        (*scopes, t)).fetchall()]
    rows.sort(key=lambda r: (0 if r["ticker"] else 1, -(r["helpful"] - r["harmful"]), -r["id"]))
    return rows[:limit]


def lesson_lines(rows: list) -> list:
    return [f"[lesson {r['id']}{', ' + r['ticker'] if r['ticker'] else ''}; {r['helpful']} right, "
            f"{r['harmful']} wrong when used] {r['text']}" for r in rows]


# --- the owner's rules ---------------------------------------------------------------

def add_rule(conn, *, text: str, owner_message_id: int, ticker=None, now_iso: str) -> int:
    body = _norm_text(text)
    if not (RULE_MIN <= len(body) <= RULE_MAX):
        raise LessonError(f"a rule is {RULE_MIN} to {RULE_MAX} characters")
    if LESSON_FORBIDDEN.search(body):
        raise LessonError("a rule may not speak to approvals, taps, halts, buying power or instructions; "
                          "a limit is a risk change card (set_risk)")
    active = conn.execute("SELECT id, text FROM owner_rules WHERE retired_at IS NULL").fetchall()
    for r in active:
        if _norm_text(r["text"]).lower() == body.lower():
            raise LessonError(f"rule {r['id']} already says this")
    if len(active) >= RULES_ACTIVE_MAX:
        raise LessonError(f"there are {RULES_ACTIVE_MAX} active rules; retire one first")
    cur = conn.execute("INSERT INTO owner_rules (ticker, text, owner_message_id, created_at) VALUES (?,?,?,?)",
                       (str(ticker).upper() if ticker else None, body, int(owner_message_id), now_iso))
    return int(cur.lastrowid)


def retire_rule(conn, rule_id, *, reason: str, now_iso: str) -> None:
    try:
        rid = int(rule_id)
    except (TypeError, ValueError):
        raise LessonError("rule_id must be a rule id")
    why = _norm_text(reason)
    if len(why) < 3:
        raise LessonError("give a reason")
    cur = conn.execute("UPDATE owner_rules SET retired_at=?, retire_reason=? WHERE id=? AND retired_at IS NULL",
                       (now_iso, why[:300], rid))
    if cur.rowcount != 1:
        raise LessonError(f"rule {rid} is not an active rule")


def active_rules(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM owner_rules WHERE retired_at IS NULL ORDER BY id").fetchall()]


def rule_lines(rows: list) -> list:
    return [f"[rule {r['id']}{', ' + r['ticker'] if r['ticker'] else ''}] {r['text']}" for r in rows]


# --- the reflect job -----------------------------------------------------------------

def _last_reflect_through(conn) -> Optional[str]:
    """The newest ``through`` of a reflect job that is done, queued or running. A failed
    job does not count, so its forecasts come up again."""
    best = None
    for r in conn.execute("SELECT payload_json FROM jobs WHERE kind='reflect' AND status IN ('done','queued','running')"):
        th = (store.loads(r[0], {}) or {}).get("through")
        if th and (best is None or th > best):
            best = th
    return best


def reflect_due(conn, now_iso: str) -> Optional[dict]:
    """The payload of the next reflect job, or None. A job is due when at least
    ``REFLECT_MIN_NEW`` forecasts resolved since the last one, none is queued or running,
    and the last one is ``REFLECT_MIN_GAP_DAYS`` days old or more."""
    if conn.execute("SELECT 1 FROM jobs WHERE kind='reflect' AND status IN ('queued','running')").fetchone():
        return None
    last = conn.execute("SELECT MAX(created_at) FROM jobs WHERE kind='reflect' AND status='done'").fetchone()[0]
    now_dt, last_dt = store.parse_ts(now_iso), store.parse_ts(last)
    if last_dt and now_dt and (now_dt - last_dt).days < REFLECT_MIN_GAP_DAYS:
        return None
    since = _last_reflect_through(conn)
    row = conn.execute("SELECT COUNT(*), MAX(resolved_at) FROM forecasts WHERE outcome IS NOT NULL "
                       "AND (? IS NULL OR resolved_at > ?)", (since, since)).fetchone()
    if int(row[0]) < REFLECT_MIN_NEW:
        return None
    return {"since": since, "through": row[1], "new": int(row[0])}


def reflect_input(conn, payload: dict) -> dict:
    """What the reflect job reads: the newly resolved forecasts with the analysis behind
    each, the scores, the active lessons and the owner's rules."""
    since, through = payload.get("since"), payload.get("through")
    rows = [_row(r) for r in conn.execute(
        "SELECT f.*, e.type AS event_type, e.analysis AS analysis, p.state AS card_state FROM forecasts f "
        "LEFT JOIN events e ON e.id=f.event_id LEFT JOIN proposals p ON p.id=f.proposal_id "
        "WHERE f.outcome IS NOT NULL AND (? IS NULL OR f.resolved_at > ?) AND f.resolved_at <= ? "
        "ORDER BY f.resolved_at, f.id", (since, since, through)).fetchall()]
    for r in rows:
        r["card_state"] = final_card_state(conn, r.get("proposal_id"), r.get("card_state"))
    groups = _groups(rows)
    graded = []
    for r in _detail_pick(rows):
        graded.append({"forecast_id": r["id"], "ticker": r["ticker"], "source": r["source"],
                       "event_type": r.get("event_type"), "verdict": r["verdict"], "p_beat": r["p_beat"],
                       "stance": r["stance"], "made_at": r["made_at"], "horizon_days": r["horizon_days"],
                       "reason": r["reason"], "analysis": (r.get("analysis") or "")[:600] or None,
                       "card_state": r.get("card_state"), "lessons_used": r["lessons"],
                       "excess": r["excess"], "outcome": r["outcome"], "brier": r["brier"],
                       "right_side": call_was_right(r), "marks": r["marks"]})
    lessons = [{**{k: r[k] for k in ("id", "scope", "ticker", "text", "helpful", "harmful", "created_at")},
                "evidence": store.loads(r["evidence_json"], [])}
               for r in conn.execute("SELECT * FROM lessons WHERE status='active' ORDER BY id").fetchall()]
    return {"new_graded": len(rows), "groups": groups, "graded": graded, "scores": calibration(conn),
            "lessons": lessons, "rules": rule_lines(active_rules(conn))}


def _detail_pick(rows: list) -> list:
    """The calls shown in full: the worst misses and the best calls by Brier score first,
    then the cards, then the rest, at most ``REFLECT_DETAIL_MAX``. The groups cover every call."""
    scored = sorted((r for r in rows if r["brier"] is not None), key=lambda r: (-r["brier"], r["id"]))
    half = REFLECT_DETAIL_MAX // 2
    pick = scored[:half] + list(reversed(scored))[:REFLECT_DETAIL_MAX // 4]
    pick += [r for r in rows if r["brier"] is None]
    pick += rows
    out, seen = [], set()
    for r in pick:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
        if len(out) >= REFLECT_DETAIL_MAX:
            break
    return out


def _groups(rows: list) -> list:
    """Every newly graded call, grouped by kind of call and by name: the count, the
    right-side rate, the mean chance, the mean excess, the Brier score and the ids to cite."""
    keyed = {}
    for r in rows:
        for key in (("call", r["source"], r.get("event_type") or "-", r["verdict"] or "-"), ("name", r["ticker"])):
            keyed.setdefault(key, []).append(r)
    out = []
    for key, grp in sorted(keyed.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        judged = [j for j in (call_was_right(r) for r in grp) if j is not None]
        ps = [r["p_beat"] for r in grp if r["p_beat"] is not None]
        out.append({"by": key[0], "group": "/".join(key[1:]), "n": len(grp),
                    "right_rate": round(sum(judged) / len(judged), 3) if judged else None,
                    "mean_p": round(sum(ps) / len(ps), 3) if ps else None,
                    "mean_excess": _mean([r["excess"] for r in grp]),
                    "brier": _mean([r["brier"] for r in grp]),
                    "ids": [r["id"] for r in grp][:REFLECT_GROUP_IDS_MAX]})
    return out


def job_changes(conn, job_id: int) -> dict:
    """The lesson changes one reflect job made."""
    out = {"added": [], "retired": []}
    for r in conn.execute("SELECT lesson_id, op FROM lesson_log WHERE job_id=? ORDER BY id", (int(job_id),)):
        if r[1] == "add":
            out["added"].append(int(r[0]))
        elif r[1] == "retire":
            out["retired"].append(int(r[0]))
    return out
