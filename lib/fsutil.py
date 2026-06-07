"""Tiny filesystem helper: crash-safe atomic text write.

Shared by lib/reflect_memory (and anything else that needs a durable rewrite).
Write to a temp file in the same directory, then os.replace — on POSIX that swap
is atomic, so a crash mid-write never leaves a half-written file behind.

This is the one primitive the framework's own markdown log (tradingagents/agents/
utils/memory.py) reimplements; we keep our copy here so lib/ never imports the
vendored framework (the dependency only ever points the other way).
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (temp file + os.replace).

    Creates parent dirs as needed. The temp file lives in the same directory so
    the final ``os.replace`` is a same-filesystem rename (atomic on POSIX).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, p)
