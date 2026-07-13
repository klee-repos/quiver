"""Judge-vs-human-label agreement scorer (analysis-side, PURE).

Measures how well the adversarial LLM impact-judge (``legislative_llm.judge_bill``) agrees
with the operator's own verdicts on impact-analysis quality, and — because ``tally_judge`` is
a pure reducer over raw vote vectors — grids the ``(judge_votes, judge_pass_min)`` operating
point against the labels for FREE (no re-running the LLM). PASS is the positive class:
precision = "when the judge says PASS, how often is the analysis actually good", recall =
"of the actually-good analyses, how many the judge lets through."

WALL: pure + analysis-side; imports only stdlib + lib.legislative (wall-clean). A wall test
enforces it reads no broker/limits.
"""

from __future__ import annotations

from typing import List, Optional

from lib import legislative

_POS = legislative.VERDICT_PASS


def agreement_metrics(pairs: List[tuple]) -> dict:
    """pairs = [(judge_verdict, human_label)] with human_label in {pass, fail} (PASS positive).
    Returns precision/recall/F1/accuracy + the confusion counts. Non-'pass' predictions
    (fail/hold) all count as a negative prediction."""
    tp = fp = fn = tn = 0
    for pred, label in pairs:
        pred_pos = str(pred).strip().lower() == _POS
        label_pos = str(label).strip().lower() == _POS
        if pred_pos and label_pos:
            tp += 1
        elif pred_pos and not label_pos:
            fp += 1
        elif not pred_pos and label_pos:
            fn += 1
        else:
            tn += 1
    n = tp + fp + fn + tn
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
    return {
        "n": n,
        "precision": round(prec, 4) if prec is not None else None,
        "recall": round(rec, 4) if rec is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "accuracy": round((tp + tn) / n, 4) if n else None,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def sweep_pass_min(vote_rows: List[tuple], max_votes: Optional[int] = None) -> List[dict]:
    """vote_rows = [(votes:list[str], human_label)]. For each candidate ``pass_min`` in
    1..(votes cast), reduce every row's votes via ``legislative.tally_judge`` and score the
    resulting verdict against the labels. Returns one metrics dict per pass_min — the operating
    point that maximizes F1 (or precision, if a bad ADD is costlier than a missed one) is the
    tuned ``judge_pass_min``. Pure: no LLM re-run needed to pick the threshold."""
    if not vote_rows:
        return []
    cast = max_votes or max(len(v) for v, _ in vote_rows)
    out = []
    for pm in range(1, cast + 1):
        pairs = [(legislative.tally_judge(votes, pm)[0], label) for votes, label in vote_rows]
        out.append({"pass_min": pm, "votes": cast, **agreement_metrics(pairs)})
    return out


def best_operating_point(sweep: List[dict], metric: str = "f1") -> Optional[dict]:
    """The sweep row maximizing ``metric`` (default F1; use 'precision' when a bad add is
    costlier than a missed one). None-valued metrics rank last."""
    scored = [s for s in sweep if s.get(metric) is not None]
    if not scored:
        return None
    return max(scored, key=lambda s: s[metric])
