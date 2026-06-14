"""The primary strategy doc (STRATEGY.md) — human-steered, agent-appended.

The narrative source of the strategy's intent. The continual-learning engine
appends dated, proof-bearing notes to the '## 7. Agent learnings' section as it
produces proposals; the human edits the doc freely (especially the operator-
directives section). The deterministic executable numbers live in strategy.yaml,
NOT here — appending a learning never changes a weight, a cap, or the universe on
its own (risky universe changes still require approval via `tick.py universe-apply`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

DOC = Path(__file__).resolve().parent.parent / "STRATEGY.md"
_LEARNINGS_HEADER = "## 7. Agent learnings"
_PLACEHOLDER = "- _(none yet)_"


def append_learning(entry: str, *, date: str, doc_path: Optional[Path] = None) -> bool:
    """Append a dated bullet under the Agent-learnings section. Returns False (no-op)
    if the doc or section is missing, so this can never break a tick."""
    path = Path(doc_path) if doc_path else DOC
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    idx = text.find(_LEARNINGS_HEADER)
    if idx < 0:
        return False
    bullet = f"- **{date}** — {entry.strip()}"
    head, tail = text[:idx], text[idx:]
    if _PLACEHOLDER in tail:
        tail = tail.replace(_PLACEHOLDER, bullet, 1)  # replace the first-run placeholder
    else:
        tail = tail.rstrip() + "\n" + bullet + "\n"
    path.write_text(head + tail, encoding="utf-8")
    return True
