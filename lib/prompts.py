"""Prompt loader — every LLM prompt in this app lives in prompts/*.md, not in code.

The pattern, app-wide: a prompt is editable markdown you can tune without touching
Python. ``load_prompt('name')`` reads ``prompts/name.md`` (sub-paths like
'agents/trader' map to ``prompts/agents/trader.md``). Pass ``**kwargs`` ONLY when
the prompt is a Python ``.format`` template; agent prompts that carry langchain
``{placeholders}`` (filled downstream by ChatPromptTemplate) are loaded raw with no
kwargs so their braces pass through untouched.

Cached after first read (the per-tick process is short-lived, so the cache never
goes stale in practice).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def load_prompt(name: str, **kwargs) -> str:
    """Return prompts/<name>.md. With kwargs, ``.format(**kwargs)`` the template;
    without, return it raw (so langchain placeholders survive)."""
    text = _read(name)
    return text.format(**kwargs) if kwargs else text
