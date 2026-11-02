"""Bounded, read-only complete-line snapshots shared by metric projections."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class NativeJsonlRows:
    """Physical source positions, a generation watermark and safe diagnostics."""

    rows: tuple[tuple[int, dict], ...]
    entries_read: int
    watermark: str
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rows, tuple) or any(
                not isinstance(item, tuple) or len(item) != 2
                or isinstance(item[0], bool) or not isinstance(item[0], int)
                or item[0] < 1 or not isinstance(item[1], dict)
                for item in self.rows):
            raise ValueError("rows carries only (1-based line, object) pairs")
        if (isinstance(self.entries_read, bool)
                or not isinstance(self.entries_read, int) or self.entries_read < 0):
            raise ValueError("entries_read carries only a non-negative count")
        if not isinstance(self.watermark, str) or not self.watermark.strip():
            raise ValueError("watermark must not be empty")
        if not isinstance(self.diagnostics, tuple) or any(
                not isinstance(item, str) for item in self.diagnostics):
            raise ValueError("diagnostics carries only stable strings")


def _stamp(stat: os.stat_result) -> tuple[int, ...]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def read_jsonl_snapshot(path: str) -> NativeJsonlRows:
    """Read only the opening size; never consume a writer\'s incomplete record.

    One descriptor supplies both data and identity. Detectable writes or path
    replacement during the read fail explicitly. This is not an OS snapshot or
    a guarantee against an adversarial writer restoring all observed metadata.
    """
    rows: list[tuple[int, dict]] = []
    diagnostics: list[str] = []
    malformed = non_object = entries_read = 0
    with open(path, "rb") as handle:
        initial = os.fstat(handle.fileno())
        remaining = initial.st_size
        while remaining:
            line = handle.readline(remaining)
            if not line:
                raise OSError("history_changed_during_read")
            remaining -= len(line)
            if not line.endswith(b"\n"):
                diagnostics.append("trailing_partial_held")
                break
            entries_read += 1
            try:
                entry = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                malformed += 1
                continue
            if isinstance(entry, dict):
                rows.append((entries_read, entry))
            else:
                non_object += 1
        if _stamp(initial) != _stamp(os.fstat(handle.fileno())) or _stamp(initial) != _stamp(os.stat(path)):
            raise OSError("history_changed_during_read")
    if malformed:
        diagnostics.append(f"skipped_malformed:{malformed}")
    if non_object:
        diagnostics.append(f"skipped_non_object:{non_object}")
    watermark = hashlib.sha256(":".join(map(str, _stamp(initial))).encode()).hexdigest()
    return NativeJsonlRows(tuple(rows), entries_read, watermark, tuple(diagnostics))
