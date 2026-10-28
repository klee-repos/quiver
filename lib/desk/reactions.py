"""The 73 emoji that Telegram Bot API 10.3 allows as a message reaction (plan §4.2).

Stored exactly as Telegram lists them. Note `✍` is U+270D with no U+FE0F. A model often
sends `❤️` or `✍️` with U+FE0F: ``normalize`` strips it before the lookup.
"""

from __future__ import annotations

ALLOWED = (
    '❤', '👍', '👎', '🔥', '🥰', '👏', '😁', '🤔', '🤯', '😱',
    '🤬', '😢', '🎉', '🤩', '🤮', '💩', '🙏', '👌', '🕊', '🤡',
    '🥱', '🥴', '😍', '🐳', '❤\u200d🔥', '🌚', '🌭', '💯', '🤣', '⚡',
    '🍌', '🏆', '💔', '🤨', '😐', '🍓', '🍾', '💋', '🖕', '😈',
    '😴', '😭', '🤓', '👻', '👨\u200d💻', '👀', '🎃', '🙈', '😇', '😨',
    '🤝', '✍', '🤗', '🫡', '🎅', '🎄', '☃', '💅', '🤪', '🗿',
    '🆒', '💘', '🙉', '🦄', '😘', '💊', '🙊', '😎', '👾', '🤷\u200d♂',
    '🤷', '🤷\u200d♀', '😡',
)
ALLOWED_SET = frozenset(ALLOWED)


def normalize(emoji: str) -> str:
    """Strip U+FE0F (the emoji presentation selector) and spaces."""
    return str(emoji or "").replace("\ufe0f", "").strip()


def check(emoji: str):
    """Return the allowed form of ``emoji``, or None when Telegram does not allow it."""
    e = normalize(emoji)
    return e if e in ALLOWED_SET else None
