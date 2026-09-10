"""Minimal message lookup for parser write helpers (clone_session)."""

from __future__ import annotations

_MESSAGES = {
    "session.title.copy_suffix": "(copy)",
}


def t(key: str, **_kwargs: object) -> str:
    return _MESSAGES.get(key, key)
