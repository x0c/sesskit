"""Read Antigravity turn errors omitted from the native JSONL export."""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

_ERROR_STEP = 17
_MAX_PAYLOAD = 1 << 20


@dataclass(frozen=True)
class ErrorStep:
    index: int
    message: str
    created_at: str | None
    http_status: int | None
    is_db_tail: bool = False

    def row(self) -> dict:
        return {
            "step_index": self.index,
            "type": "ERROR_MESSAGE",
            "status": "ERROR",
            "created_at": self.created_at,
            "error": self.message,
            "_db_http_status": self.http_status,
            "_db_error_record": f"steps:{self.index}",
        }


def _varint(data: memoryview, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset == len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128 and value < 1 << 64:
            return value, offset
    raise ValueError("invalid protobuf varint")


def _fields(data: memoryview) -> dict[int, int | memoryview]:
    fields: dict[int, int | memoryview] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = divmod(tag, 8)
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            fields[number], offset = _varint(data, offset)
            continue
        if wire == 2:
            length, offset = _varint(data, offset)
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
        else:
            raise ValueError("unsupported protobuf wire type")
        end = offset + length
        if end > len(data):
            raise ValueError("truncated protobuf field")
        if wire == 2:
            fields[number] = data[offset:end]
        offset = end
    return fields


def _message(fields: dict[int, int | memoryview], number: int) -> dict[int, int | memoryview]:
    value = fields.get(number)
    return _fields(value) if isinstance(value, memoryview) else {}


def _created_at(top: dict[int, int | memoryview]) -> str | None:
    stamp = _message(_message(top, 5), 1)
    seconds, nanos = stamp.get(1), stamp.get(2, 0)
    if not isinstance(seconds, int) or not isinstance(nanos, int) or nanos >= 10**9:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).replace(
            microsecond=nanos // 1000,
        ).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _decode_step(index: int, payload: bytes, is_db_tail: bool) -> ErrorStep | None:
    try:
        top = _fields(memoryview(payload))
        if top.get(1) != _ERROR_STEP:
            return None
        # CascadeStep.error_message.error_details (24.3). Only expose its
        # user-facing message, never headers, stack traces or raw RPC details.
        details = _message(_message(top, 24), 3)
        text = details.get(1)
        if not isinstance(text, memoryview):
            return None
        message = bytes(text).decode("utf-8").strip()
        if not message:
            return None
        code = details.get(7)
        http_status = code if isinstance(code, int) and 100 <= code <= 599 else None
        return ErrorStep(index, message, _created_at(top), http_status, is_db_tail)
    except ValueError:
        return None


def _db_path(history_path: str) -> Path | None:
    path = Path(history_path)
    if path.suffix == ".db":
        return path
    if path.name not in {"transcript.jsonl", "transcript_full.jsonl"}:
        return None
    if (path.parent.name, path.parent.parent.name) != ("logs", ".system_generated"):
        return None
    item = path.parent.parent.parent
    if item.parent.name == "brain":
        return item.parent.parent / "conversations" / f"{item.name}.db"
    return None


def _stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def companion_signature(history_path: str) -> tuple[tuple[int, ...], ...]:
    """Stat-only invalidation inputs for an Antigravity JSONL's companion store."""
    path = _db_path(history_path)
    if path is None or history_path.endswith(".db"):
        return ()
    main = _stamp(path)
    return (main, _stamp(Path(f"{path}-wal")) or ()) if main is not None else ()


@lru_cache(maxsize=128)
def _read_steps(path: Path, stamp: tuple, wal_stamp: tuple | None) -> tuple[ErrorStep, ...]:
    # Cache keys include the WAL; lock and I/O failures do not poison the cache.
    uri = f"{path.absolute().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=0.05)
    try:
        rows = conn.execute(
            "SELECT idx, step_payload, idx = (SELECT max(idx) FROM steps) "
            "FROM steps WHERE step_type = ? "
            "AND length(step_payload) BETWEEN 1 AND ? ORDER BY idx",
            (_ERROR_STEP, _MAX_PAYLOAD),
        )
        return tuple(step for idx, payload, is_tail in rows
                     if isinstance(idx, int) and isinstance(payload, bytes)
                     if (step := _decode_step(idx, payload, bool(is_tail))) is not None)
    except sqlite3.OperationalError as exc:
        if str(exc).startswith(("no such table:", "no such column:")):
            return ()  # Older or empty database schema.
        raise
    finally:
        conn.close()


def read_error_steps(history_path: str) -> tuple[ErrorStep, ...] | None:
    """Known errors for the exact conversation; None on a transient DB error."""
    path = _db_path(history_path)
    stamp = _stamp(path) if path is not None else None
    if path is None or stamp is None:
        return ()
    try:
        return _read_steps(path, stamp, _stamp(Path(f"{path}-wal")))
    except (OSError, sqlite3.Error):
        return None


class ErrorStepMerge:
    """Insert DB steps at native index boundaries, without reordering JSONL rows."""

    def __init__(self, steps: tuple[ErrorStep, ...]) -> None:
        self.remaining = deque(steps)
        self.indices = frozenset(step.index for step in steps)

    def rows(self, rows: Iterable[tuple[int, dict]], *, final: bool = False
             ) -> Iterator[tuple[int, dict]]:
        for line, row in rows:
            step = row.get("step_index")
            if type(step) is int:
                while self.remaining and self.remaining[0].index <= step:
                    yield 0, self.remaining.popleft().row()
                if step in self.indices and row.get("type") == "ERROR_MESSAGE":
                    continue
            yield line, row
        if final:
            while self.remaining:
                yield 0, self.remaining.popleft().row()
