"""Optional session-metadata cache. Default is a no-op (always miss)."""

from __future__ import annotations

import os
from typing import Any


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
    """Allow a host (e.g. Corral) to inject a real PerformanceCache."""
    global _CACHE
    _CACHE = cache
