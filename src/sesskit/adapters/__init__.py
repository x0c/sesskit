"""Runtime adapter registry: one adapter per runtime, no name dispatch."""

from __future__ import annotations

from sesskit.adapters.base import Capabilities, RuntimeAdapter
from sesskit.adapters.claude import adapter as _claude
from sesskit.adapters.codex import adapter as _codex
from sesskit.adapters.cursor import adapter as _cursor
from sesskit.adapters.kimi import adapter as _kimi
from sesskit.adapters.opencode import adapter as _opencode
from sesskit.adapters.pi import adapter as _pi

_ADAPTERS: dict[str, RuntimeAdapter] = {
    adapter.id: adapter
    for adapter in (_claude, _codex, _opencode, _kimi, _cursor, _pi)
}


def get_adapter(runtime_id: str) -> RuntimeAdapter:
    """Return the adapter for ``runtime_id`` or raise ``KeyError``."""
    try:
        return _ADAPTERS[runtime_id]
    except KeyError as exc:
        raise KeyError(f"unregistered runtime: {runtime_id}") from exc


def list_adapters() -> list[RuntimeAdapter]:
    """Return all registered adapters in registry order."""
    return list(_ADAPTERS.values())


__all__ = [
    "Capabilities",
    "RuntimeAdapter",
    "get_adapter",
    "list_adapters",
]
