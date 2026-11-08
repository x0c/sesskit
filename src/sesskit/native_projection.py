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
    return module if getattr(module, "PROTOCOL_VERSION", None) == 1 else None


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
