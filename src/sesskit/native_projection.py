"""Optional Rust compact projections; Python owns public interpretation.

Only listing paths use this bridge. Full conversations, transcripts, usage
records and native history bytes are never changed by it.
"""

from __future__ import annotations

import importlib
import json
import os
from types import ModuleType


def _backend() -> ModuleType | None:
    if os.environ.get("SESSKIT_NATIVE") == "0":
        return None
    try:
        module = importlib.import_module("sesskit._native")
    except (ImportError, OSError):
        return None
    return module if getattr(module, "PROTOCOL_VERSION", None) == 2 else None


def native_available() -> bool:
    """Whether a compatible native backend is enabled in this process."""
    return _backend() is not None


def project_records(records: list, profile: str) -> list:
    """Reduce large JSON records before Python decoding, retaining fallbacks.

    Small inputs are cheaper to decode directly. Bytes/BOM/UTF-16 and unusual
    scalar inputs keep the original decoder, as do malformed/unsupported rows.
    No cache, file I/O or runtime-specific outcome inference happens here.
    """
    if not records or not all(isinstance(raw, str) for raw in records):
        return records
    if sum(map(len, records)) < 4096:
        return records
    backend = _backend()
    if backend is None:
        return records
    try:
        projected = backend.project_json_many(records, profile)
    except UnicodeError:
        # Python JSON accepts lone surrogates that cannot cross UTF-8 FFI.
        return records
    return [raw if value is None else value for raw, value in zip(records, projected, strict=True)]


def project_opencode_sessions(db_path: str, session_ids: list[str]) -> dict | None:
    """Return compact v2 listing facts, or None to use the Python SQL path."""
    if not session_ids:
        return {}
    backend = _backend()
    if backend is None:
        return None
    try:
        result = json.loads(backend.project_opencode_sessions(db_path, session_ids))
    except (AttributeError, RuntimeError, UnicodeError, TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _python_count_loops(runtime: str, path: str) -> int | None:
    if runtime == "claude":
        from sesskit.parsers.claude import count_loops_from_path

        return count_loops_from_path(path)
    if runtime == "codex":
        from sesskit.parsers.codex import count_loops_from_path

        return count_loops_from_path(path)
    if runtime == "pi":
        from sesskit.parsers.pi import count_loops_from_path

        return count_loops_from_path(path)
    if runtime == "antigravity":
        from sesskit.parsers.antigravity import count_loops_from_path

        return count_loops_from_path(path)
    if runtime == "cursor":
        from sesskit.parsers.cursor import count_store_loops

        return count_store_loops(path)
    return None


def count_loops_many(items: list[tuple[str, str]]) -> list[int | None]:
    """Count assistant model-decision loops for ``(runtime, path)`` pairs.

    Uses the parallel Rust backend (GIL-detached) when enabled, falling back
    per-file on non-standard JSON or for all items when ``SESSKIT_NATIVE=0``.
    """
    if not items:
        return []
    backend = _backend()
    if backend is not None:
        try:
            raw_results = backend.count_loops_many(items)
            if isinstance(raw_results, list) and len(raw_results) == len(items):
                resolved: list[int | None] = []
                for (runtime, path), item in zip(items, raw_results, strict=True):
                    ok, count = item
                    if ok:
                        resolved.append(int(count) if count is not None else None)
                    else:
                        resolved.append(_python_count_loops(runtime, path))
                return resolved
        except (AttributeError, RuntimeError, UnicodeError, TypeError, ValueError):
            pass
    return [_python_count_loops(runtime, path) for runtime, path in items]


def count_loops(runtime: str, path: str) -> int | None:
    """Count assistant model-decision loops for one ``(runtime, path)``."""
    return count_loops_many([(runtime, path)])[0]
