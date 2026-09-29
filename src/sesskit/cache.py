"""Optional session-metadata cache. Default is a no-op (always miss)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def cache_dir() -> Path:
    """Default ``~/.cache/sesskit``; honor ``SESSKIT_CACHE_DIR`` then XDG.

    Host-neutral: no host-specific override lives here. A host that needs
    its own cache location resolves it on its side and passes cache
    implementations per scan via the host extension.
    """
    override = os.environ.get("SESSKIT_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return root / "sesskit"


def file_signature(path: str) -> tuple[int, int, int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class NullSessionCache:
    def get_session(self, runtime: str, path: str, extra_version: str = "") -> dict | None:
        return None

    def put_session(self, runtime: str, path: str, payload: dict, extra_version: str = "") -> None:
        return None

    def get_conversation(self, runtime: str, key: str, path: str) -> list | None:
        return None

    def put_conversation(self, runtime: str, key: str, path: str, messages: list) -> None:
        return None


_CACHE = NullSessionCache()


def get_cache() -> NullSessionCache:
    return _CACHE


def set_cache(cache: Any) -> None:
    """Inject a real PerformanceCache (process-global, deprecated).

    Prefer passing a cache per scan via the host extension: process-global
    state cannot isolate two hosts in one process. Kept for older callers.
    """
    global _CACHE
    _CACHE = cache
