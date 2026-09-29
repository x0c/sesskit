"""Application-neutral incremental activity reader with a Pi implementation.

``load_activity`` is a full snapshot. Hosts that poll (for example a phone UI
refreshing every few seconds) need incremental reads: return only the typed
``ActivityEvent`` records appended since an opaque cursor, without re-reading
and re-parsing whole histories on every poll.

Protocol (runtime-agnostic; other runtimes can implement it later without
changing this shape):

- ``open_activity_reader(session, cursor=None)`` opens a reader for one
  scanned session dict. ``cursor`` is an opaque value previously returned by
  ``poll()``; callers persist it across restarts without interpreting it.
- ``poll()`` returns a ``PollResult`` with the new typed events since the
  cursor, a ``reset`` flag, the current ``generation``, and the next cursor.
  When history can no longer be continued (branch switch, truncation,
  replacement, incompatible or corrupt cursor) the reader returns ``reset``
  true with a new generation: the returned events replace, not extend, the
  previous generation.
- ``page(before=None, limit=...)`` walks backwards through the current
  generation with an opaque ``before`` token and ``has_more``.
- Cursors and page tokens are JSON-safe strings carrying a version, so hosts
  can persist them and the reader can reject unknown versions.
- Runtimes without an incremental implementation raise
  ``IncrementalUnsupported``; consumers fall back to ``load_activity``.

The shapes are deliberately offset-friendly for later runtimes: Claude/Codex
JSONL readers can use byte offsets as their boundary, Cursor/OpenCode SQLite
readers can use row cursors, without changing ``PollResult``/``PageResult``.

Pi notes: entries form an ``id``/``parentId`` tree and the active branch is
rebuilt by walking parents from the last appended entry (never by timestamp,
so clock regression cannot select the wrong branch). The fast path advances
only when file identity is unchanged, the file did not shrink, the stored
boundary checksum matches, and appended complete lines extend the known leaf's
chain. A no-change poll performs metadata checks only. A trailing partial
line (concurrent writer) is held back until its newline arrives.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sesskit.activity import _build_pi_events, _pi_compactions, _pi_outcome
from sesskit.models import ActivityEvent, Evidence, LoadState, SessionOutcome
from sesskit.parsers import pi as scan_pi

_READER_CURSOR_VERSION = 1
_PAGE_TOKEN_VERSION = 1
_PAGE_DEFAULT_LIMIT = 50
_PAGE_MAX_LIMIT = 200


class IncrementalUnsupported(Exception):
    """Raised when a runtime has no incremental reader; use ``load_activity``."""


@dataclass(frozen=True)
class PollResult:
    """One incremental poll: new events plus continuation metadata."""

    events: tuple[ActivityEvent, ...]
    reset: bool
    generation: str
    cursor: str | None
    outcome: SessionOutcome
    state: LoadState


@dataclass(frozen=True)
class PageResult:
    """One backwards page of the current generation, newest last."""

    events: tuple[ActivityEvent, ...]
    before: str | None
    has_more: bool
    generation: str


class ActivityReader:
    """Base incremental reader; runtimes subclass this protocol."""

    def poll(self) -> PollResult:
        """Return new events since the last poll plus the next cursor."""
        raise NotImplementedError

    def page(self, before: str | None = None, limit: int = _PAGE_DEFAULT_LIMIT) -> PageResult:
        """Return an older page of the current generation, newest last."""
        raise NotImplementedError


def supports_incremental(session: dict) -> bool:
    """Return true when this session's runtime has an incremental reader."""
    return str(session.get("source") or "") == "pi"


def open_activity_reader(session: dict, cursor: str | Mapping[str, Any] | None = None) -> ActivityReader:
    """Open an incremental reader for one scanned session dict.

    Raises ``IncrementalUnsupported`` for runtimes without an implementation
    (including Kimi); callers fall back to ``load_activity`` snapshots.
    """
    if not supports_incremental(session):
        runtime_id = str(session.get("source") or "")
        raise IncrementalUnsupported(
            f"incremental reading is unsupported for runtime {runtime_id!r}; use load_activity instead",
        )
    return PiActivityReader(session, cursor)


def _encode_cursor(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_cursor(cursor: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(cursor, Mapping):
        data = dict(cursor)
    elif isinstance(cursor, str):
        try:
            data = json.loads(cursor)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
    else:
        return None
    if data.get("v") != _READER_CURSOR_VERSION or data.get("runtime") != "pi":
        return None
    return data


def _boundary_checksum(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def _split_complete(data: bytes) -> tuple[list[bytes], bytes]:
    """Split appended bytes into complete lines plus a held-back remainder."""
    if not data:
        return [], b""
    if data.endswith(b"\n"):
        return data.splitlines(keepends=True), b""
    *complete, remainder = data.splitlines(keepends=True)
    return complete, remainder


def _parse_lines(lines: list[bytes]) -> tuple[list[dict], int]:
    entries: list[dict] = []
    for raw in lines:
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries, len(lines)


class PiActivityReader(ActivityReader):
    """Incremental reader over a Pi JSONL session file.

    Holds the parsed entry map in memory so append polls only read and parse
    the newly appended bytes. Event ``seq`` values are assigned once per
    generation and stay stable for the same active-branch message across
    polls; native entry ids stay the ``message_id`` grouping key.
    """

    def __init__(self, session: dict, cursor: str | Mapping[str, Any] | None = None) -> None:
        self._path = str(session.get("path") or "")
        self._session_id = str(session.get("id") or self._path or "unknown")
        self._session = dict(session)
        saved = _decode_cursor(cursor)
        if saved is not None and (
            saved.get("path") != self._path or saved.get("session") != self._session_id
        ):
            saved = None
        self._pending_cursor: dict[str, Any] | None = saved
        self._gen = 0
        self._events: list[ActivityEvent] = []
        self._branch: list[dict] = []
        self._branch_keys: list[str] = []
        self._by_id: dict[str, dict] = {}
        self._all_entries: list[dict] = []
        self._leaf: str | None = None
        self._size = 0
        self._line_start = 0
        self._boundary = _boundary_checksum(b"")
        self._dev: int | None = None
        self._ino: int | None = None
        self._mtime_ns = 0
        self._outcome = SessionOutcome("unknown", Evidence("unknown"))
        self._state: LoadState = "unavailable"
        self._initialized = False
        # Test/observability counters: lines fed to the JSON parser and file
        # bytes consumed through reads (stat calls excluded).
        self.lines_parsed = 0
        self.bytes_read = 0
        self.full_parses = 0

    # -- public protocol -------------------------------------------------

    def poll(self) -> PollResult:
        self._sync()
        return PollResult(
            events=tuple(self._new_events),
            reset=self._last_reset,
            generation=str(self._gen),
            cursor=self._export_cursor(),
            outcome=self._outcome,
            state=self._state,
        )

    def page(self, before: str | None = None, limit: int = _PAGE_DEFAULT_LIMIT) -> PageResult:
        self._sync()
        total = len(self._events)
        end = total + 1
        token = self._decode_page_token(before)
        if token is not None and token.get("gen") == self._gen:
            try:
                end = min(max(int(token.get("end", total + 1)), 1), total + 1)
            except (TypeError, ValueError):
                end = total + 1
        try:
            count = int(limit)
        except (TypeError, ValueError):
            count = _PAGE_DEFAULT_LIMIT
        count = min(max(count, 1), _PAGE_MAX_LIMIT)
        start = max(end - count, 1)
        window = tuple(event for event in self._events if start <= event.seq < end)
        has_more = start > 1
        next_before = (
            _encode_cursor({"v": _PAGE_TOKEN_VERSION, "gen": self._gen, "end": start})
            if has_more
            else None
        )
        return PageResult(events=window, before=next_before, has_more=has_more, generation=str(self._gen))

    # -- sync machinery --------------------------------------------------

    def _sync(self) -> None:
        self._new_events: list[ActivityEvent] = []
        self._last_reset = False
        try:
            stat = os.stat(self._path)
        except OSError:
            self._mark_unavailable()
            return
        if not self._initialized:
            self._cold_open(stat)
            return
        if (
            self._dev is not None
            and (stat.st_dev != self._dev or stat.st_ino != self._ino)
        ):
            self._rebuild(stat, reason="replacement")
            return
        if stat.st_size < self._size:
            self._rebuild(stat, reason="truncation")
            return
        if stat.st_size == self._size and stat.st_mtime_ns == self._mtime_ns:
            return
        with open(self._path, "rb") as handle:
            if stat.st_size == self._size:
                # Same size but newer mtime: confirm the boundary line still
                # matches with one bounded read before reporting no change.
                if self._verify_boundary(handle):
                    self._mtime_ns = stat.st_mtime_ns
                else:
                    self._rebuild(stat, reason="boundary")
                return
            if not self._verify_boundary(handle):
                self._rebuild(stat, reason="boundary")
                return
            self._consume_appended(handle, stat)

    def _cold_open(self, stat: os.stat_result) -> None:
        with open(self._path, "rb") as handle:
            data = handle.read()
        self.bytes_read += len(data)
        lines, _remainder = _split_complete(data)
        size = len(data) - len(_remainder)
        entries, parsed = _parse_lines(lines)
        self.lines_parsed += parsed
        self.full_parses += 1
        saved = self._pending_cursor
        self._pending_cursor = None
        self._adopt_full(stat, entries, size, lines, fresh_gen=1)
        self._initialized = True
        resumed = saved is not None and self._cursor_matches(saved, stat, size)
        if resumed:
            try:
                self._gen = max(int(saved.get("gen", 1)), 1)
            except (TypeError, ValueError):
                self._gen = 1
            self._last_reset = False
            self._new_events = []
        else:
            if saved is not None:
                self._gen += 1
            self._last_reset = True
            self._new_events = list(self._events)

    def _rebuild(self, stat: os.stat_result, reason: str) -> None:
        with open(self._path, "rb") as handle:
            data = handle.read()
        self.bytes_read += len(data)
        lines, _remainder = _split_complete(data)
        size = len(data) - len(_remainder)
        entries, parsed = _parse_lines(lines)
        self.lines_parsed += parsed
        self.full_parses += 1
        self._adopt_full(stat, entries, size, lines, fresh_gen=self._gen + 1)
        self._initialized = True
        self._last_reset = True
        self._new_events = list(self._events)

    def _adopt_full(
        self,
        stat: os.stat_result,
        entries: list[dict],
        size: int,
        lines: list[bytes],
        fresh_gen: int,
    ) -> None:
        branch = scan_pi.active_messages(entries)
        # Same compaction linkage as the snapshot loader: the first branch
        # message naming a compaction id as its parent emits a standalone
        # ``compaction`` event first. Omitting this desyncs every later seq.
        events = _build_pi_events(branch, self._session_id,
                                  compactions=_pi_compactions(entries, branch))
        self._all_entries = entries
        self._by_id = {str(item["id"]): item for item in entries if isinstance(item.get("id"), str)}
        self._branch = branch
        self._branch_keys = [
            str(item["id"]) for item in branch if isinstance(item.get("id"), str)
        ]
        leaf: str | None = None
        for item in reversed(entries):
            if isinstance(item.get("id"), str):
                leaf = str(item["id"])
                break
        self._leaf = leaf
        self._events = events
        self._outcome = _pi_outcome(branch) if events else SessionOutcome("unknown", Evidence("unknown"))
        self._state = "available" if events else "empty"
        self._size = size
        if lines:
            last = lines[-1]
            self._line_start = size - len(last)
            self._boundary = _boundary_checksum(last)
        else:
            self._line_start = size
            self._boundary = _boundary_checksum(b"")
        self._dev = stat.st_dev
        self._ino = stat.st_ino
        self._mtime_ns = stat.st_mtime_ns
        self._gen = fresh_gen

    def _consume_appended(self, handle: Any, stat: os.stat_result) -> None:
        handle.seek(self._size)
        data = handle.read(stat.st_size - self._size)
        self.bytes_read += len(data)
        lines, _remainder = _split_complete(data)
        if not lines:
            self._mtime_ns = stat.st_mtime_ns
            return
        entries, parsed = _parse_lines(lines)
        self.lines_parsed += parsed
        advanced = self._size + sum(len(line) for line in lines)
        if self._leaf is None or not self._chain_continues(entries):
            self._rebuild(stat, reason="branch")
            return
        extended = dict(self._by_id)
        for item in entries:
            if isinstance(item.get("id"), str):
                extended[str(item["id"])] = item
        leaf: str | None = self._leaf
        for item in entries:
            if isinstance(item.get("id"), str):
                leaf = str(item["id"])
        path: list[dict] = []
        node = extended.get(leaf) if leaf is not None else None
        while isinstance(node, dict):
            path.append(node)
            parent = node.get("parentId")
            node = extended.get(parent) if isinstance(parent, str) else None
        path.reverse()
        branch = [
            item for item in path
            if item.get("type") == "message" and isinstance(item.get("message"), dict)
        ]
        keys = [str(item["id"]) for item in branch if isinstance(item.get("id"), str)]
        if len(keys) != len(branch) or keys[: len(self._branch_keys)] != self._branch_keys:
            self._rebuild(stat, reason="branch")
            return
        fresh = branch[len(self._branch):]
        if not self._branch_keys and not branch:
            self._rebuild(stat, reason="branch")
            return
        added = _build_pi_events(
            fresh, self._session_id, seq_start=len(self._events), position_start=len(self._branch),
            compactions=_pi_compactions(self._all_entries + entries, branch),
        )
        self._all_entries = self._all_entries + entries
        self._by_id = extended
        self._branch = branch
        self._branch_keys = keys
        self._leaf = leaf
        self._events.extend(added)
        self._new_events = list(added)
        self._outcome = _pi_outcome(branch) if self._events else SessionOutcome("unknown", Evidence("unknown"))
        self._state = "available" if self._events else "empty"
        self._size = advanced
        last = lines[-1]
        self._line_start = advanced - len(last)
        self._boundary = _boundary_checksum(last)
        self._mtime_ns = stat.st_mtime_ns
        self._last_reset = False

    def _chain_continues(self, entries: list[dict]) -> bool:
        if not entries:
            return True
        known = set(self._by_id)
        first = True
        for item in entries:
            item_id = item.get("id")
            parent = item.get("parentId")
            if first:
                if parent != self._leaf:
                    return False
                first = False
            elif isinstance(parent, str) and parent not in known:
                return False
            if isinstance(item_id, str):
                known.add(item_id)
        return True

    def _verify_boundary(self, handle: Any) -> bool:
        if self._size == 0:
            return self._boundary == _boundary_checksum(b"")
        handle.seek(self._line_start)
        data = handle.read(self._size - self._line_start)
        self.bytes_read += len(data)
        return _boundary_checksum(data) == self._boundary

    def _mark_unavailable(self) -> None:
        had_history = self._initialized and self._gen > 0 and self._state != "unavailable"
        self._state = "unavailable"
        self._outcome = SessionOutcome("unknown", Evidence("unknown"))
        self._new_events = []
        self._last_reset = had_history

    def _cursor_matches(self, saved: dict[str, Any], stat: os.stat_result, size: int) -> bool:
        try:
            if (
                int(saved.get("size", -1)) != size
                or int(saved.get("dev", -1)) != stat.st_dev
                or int(saved.get("ino", -1)) != stat.st_ino
            ):
                return False
            if saved.get("leaf") != self._leaf:
                return False
            if saved.get("boundary") != self._boundary:
                return False
            if list(saved.get("branch", [])) != self._branch_keys:
                return False
            if int(saved.get("events", -1)) != len(self._events):
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _export_cursor(self) -> str | None:
        if self._state == "unavailable":
            return None
        return _encode_cursor({
            "v": _READER_CURSOR_VERSION,
            "runtime": "pi",
            "path": self._path,
            "session": self._session_id,
            "dev": self._dev,
            "ino": self._ino,
            "size": self._size,
            "line_start": self._line_start,
            "boundary": self._boundary,
            "leaf": self._leaf,
            "branch": self._branch_keys,
            "events": len(self._events),
            "gen": self._gen,
        })

    @staticmethod
    def _decode_page_token(before: str | None) -> dict[str, Any] | None:
        if not before:
            return None
        try:
            data = json.loads(before)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("v") != _PAGE_TOKEN_VERSION:
            return None
        return data
