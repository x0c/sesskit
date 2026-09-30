"""Incremental activity readers for Claude and Codex JSONL histories.

Both runtimes append one JSON object per line. These readers reuse the
snapshot row-sequence builders in :mod:`sesskit.activity` (``_ClaudeFeed`` /
``_CodexFeed`` plus the outcome accumulators): no second interpreter exists.
A reader feeds one appended byte batch per poll while the feed object
carries interpretation state across polls, so the incremental outcome
always equals the snapshot outcome function over the same bytes.

Cold open builds the full interpretation in one pass (a bounded tail
window of events is *returned*, but sequence numbers are snapshot-global:
the window always equals the matching suffix of the snapshot). Local
measurement motivates this: the largest histories on this machine
(91 MB Codex, 63 MB Claude) are dominated by a few huge tool-output
lines and parse in ~0.15 s into hundreds of events, so one full build
per open is cheap while steady-state polls parse only appended bytes.
Parsed rows are freed after each build; only events plus the compact
feed maps are retained.

Cursor shape (opaque, versioned, JSON-safe)::

    {"v": 1, "runtime": "claude" | "codex", "path": ..., "session": ...,
     "dev": ..., "ino": ..., "size": <committed offset of next unread byte>,
     "head": <sha256 of first 4 KiB>, "boundary": <sha256 of last line>,
     "events": <materialized event total>, "gen": <generation>,
     "state": "available" | "empty",
     "outcome": <status plus evidence plus optional error>,
     "aux": <pending error (claude) / open turn (codex), dedup tail hash,
             unresolved call ids (capped)>}

Fingerprints never rely on inode alone: replacement or in-place rewrite
with the same size is caught by the head/boundary checksums. A half-written
trailing line is never consumed. Truncation, replacement, fingerprint
mismatch, or a corrupt/foreign/old cursor rebuilds with ``reset=True`` and
a new generation, never continuing silently. A no-change poll stats the
file only and parses nothing.

``page()`` slices the materialized generation with opaque ``before``
tokens; pairing is global over the materialized rows, so a tool call and
its result on different pages still pair by ``call_id``, and backward
pages reassembled equal the snapshot. Event ``seq`` is dense from 1 and
stable within a generation.

Documented stream-vs-snapshot edges (see the incremental-reader contract):
a trailing upstream error is emitted optimistically at poll end, and
linkage that arrives late (result/usage/verified rows after the linked
event was delivered) enriches the materialized list only. Consumers join
call/result by ``call_id`` exactly like the v1 pairing rule.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

from sesskit.activity import (
    _ClaudeFeed,
    _ClaudeOutcomeAcc,
    _CodexFeed,
    _CodexOutcomeAcc,
)
from sesskit.activity_reader import (
    _PAGE_DEFAULT_LIMIT,
    _PAGE_MAX_LIMIT,
    _PAGE_TOKEN_VERSION,
    _READER_CURSOR_VERSION,
    ActivityReader,
    PageResult,
    PollResult,
    _boundary_checksum,
    _encode_cursor,
    _split_complete,
    decode_reader_cursor,
)
from sesskit.models import ActivityEvent, Evidence, LoadState, SessionOutcome

_TAIL_EVENTS_DEFAULT = 200
_HEAD_FP_BYTES = 4096
_AUX_CALL_CAP = 500


def _head_checksum(handle: Any, length: int) -> str:
    """Checksum of the first ``length`` bytes (frozen per generation).

    The length is frozen at build time so appends past it never change the
    fingerprint; appends within it are still caught because they shift the
    committed size and boundary. Never use ``min(4K, current_size)`` here:
    that would invalidate the fingerprint on every append to a small file.
    """
    handle.seek(0)
    return hashlib.sha256(handle.read(max(0, length))).hexdigest()


def _parse_json_rows(lines: list[bytes]) -> list[tuple[int, dict]]:
    """Parse complete lines, keeping 1-based file line numbers.

    Malformed lines are skipped exactly like the snapshot loader, but they
    still consume a line number so evidence ``line:N`` records match the
    snapshot on the same bytes.
    """
    rows: list[tuple[int, dict]] = []
    for lineno, raw in enumerate(lines, 1):
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append((lineno, item))
    return rows


def _dedup_tail_hash(event: ActivityEvent | None) -> list | None:
    if event is None:
        return None
    text = event.text or ""
    return [event.type, hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()]


def _encode_outcome(outcome: SessionOutcome) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": outcome.status,
        "ev": outcome.evidence.origin,
        "ev_field": outcome.evidence.field,
        "ev_record": outcome.evidence.record,
        "error": None,
    }
    if outcome.error is not None:
        payload["error"] = {
            "kind": outcome.error.kind,
            "message": outcome.error.message,
            "code": outcome.error.code,
            "ev": outcome.error.evidence.origin,
            "ev_field": outcome.error.evidence.field,
            "ev_record": outcome.error.evidence.record,
            "retryable": outcome.error.retryable,
            "scope": outcome.error.scope,
            "http_status": outcome.error.http_status,
        }
    return payload


def _decode_outcome(payload: Any) -> SessionOutcome:
    from sesskit.models import AgentError

    if not isinstance(payload, dict):
        return SessionOutcome("unknown", Evidence("unknown"))
    status = payload.get("status")
    if status not in {"done", "pending", "aborted", "unknown"}:
        return SessionOutcome("unknown", Evidence("unknown"))
    ev_origin = payload.get("ev")
    if ev_origin not in {"native", "inferred", "unknown"}:
        ev_origin = "unknown"
    evidence = Evidence(ev_origin, field=payload.get("ev_field"), record=payload.get("ev_record"))
    error: AgentError | None = None
    raw_error = payload.get("error")
    if isinstance(raw_error, dict) and status == "aborted":
        try:
            err_ev_origin = raw_error.get("ev")
            if err_ev_origin not in {"native", "inferred", "unknown"}:
                err_ev_origin = "unknown"
            error = AgentError(
                str(raw_error.get("kind") or "unknown"),
                str(raw_error.get("message") or "error"),
                Evidence(err_ev_origin, field=raw_error.get("ev_field"),
                         record=raw_error.get("ev_record")),
                raw_error.get("code"),
                raw_error.get("retryable"),
                raw_error.get("scope"),
                raw_error.get("http_status"),
            )
        except (ValueError, TypeError):
            error = None
    try:
        return SessionOutcome(status, evidence, error)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return SessionOutcome("unknown", Evidence("unknown"))


class _JsonlActivityReaderBase(ActivityReader):
    """Shared byte-offset machinery; subclasses bind the runtime interpreter."""

    RUNTIME = ""

    def __init__(
        self,
        session: dict,
        cursor: str | Mapping[str, Any] | None = None,
        *,
        tail_events: int = _TAIL_EVENTS_DEFAULT,
    ) -> None:
        self._path = str(session.get("path") or "")
        self._session_id = str(session.get("id") or self._path or "unknown")
        self._tail_events = max(1, int(tail_events))
        saved = decode_reader_cursor(cursor, self.RUNTIME)
        if saved is not None and (
            saved.get("path") != self._path or saved.get("session") != self._session_id
        ):
            saved = None
        self._pending_cursor: dict[str, Any] | None = saved
        self._pending_invalid = cursor is not None and saved is None
        self._gen = 0
        self._events: list[ActivityEvent] = []
        self._feed: Any = None
        self._acc: Any = None
        self._next_lineno = 1
        self._size = 0
        self._line_start = 0
        self._last_line = b""
        self._boundary = _boundary_checksum(b"")
        self._head = hashlib.sha256(b"").hexdigest()
        self._head_len = 0
        self._dev: int | None = None
        self._ino: int | None = None
        self._mtime_ns = 0
        self._outcome = SessionOutcome("unknown", Evidence("unknown"))
        self._state: LoadState = "unavailable"
        self._initialized = False
        self._materialized = False
        self._last_cursor: str | None = (
            cursor if isinstance(cursor, str) and saved is not None else None
        )
        # Test/observability counters: lines fed to the JSON parser and file
        # bytes consumed through reads (stat calls excluded).
        self.lines_parsed = 0
        self.bytes_read = 0
        self.full_parses = 0

    # -- interpreter binding (runtime subclasses) --------------------------

    def _new_feed(self) -> Any:
        raise NotImplementedError

    def _new_acc(self) -> Any:
        raise NotImplementedError

    def _aux_state(self) -> dict[str, Any]:
        raise NotImplementedError

    # -- public protocol ---------------------------------------------------

    @property
    def full_coverage(self) -> bool:
        """True once materialized (the window always covers full history)."""
        return self._materialized

    @property
    def event_total(self) -> int:
        """Total materialized events (the returned window may be a suffix)."""
        return len(self._events)

    def poll(self) -> PollResult:
        self._sync()
        if self._materialized:
            cursor = self._export_cursor()
            self._last_cursor = cursor
        elif self._state == "unavailable":
            cursor = None
            self._last_cursor = None
        else:
            cursor = self._last_cursor
        return PollResult(
            events=tuple(self._new_events),
            reset=self._last_reset,
            generation=str(self._gen),
            cursor=cursor,
            outcome=self._outcome,
            state=self._state,
        )

    def page(self, before: str | None = None, limit: int = _PAGE_DEFAULT_LIMIT) -> PageResult:
        self._sync()
        if not self._materialized:
            self._materialize_full()
        total = len(self._events)
        end = total + 1
        token = self._decode_page_token(before)
        if token is not None and token.get("gen") == self._gen:
            try:
                end = min(max(int(token.get("end", total + 1)), 1), total + 1)
            except (TypeError, ValueError):
                end = total + 1
        return self._slice(end=end, limit=limit)

    def _slice(self, end: int, limit: int) -> PageResult:
        try:
            count = int(limit)
        except (TypeError, ValueError):
            count = _PAGE_DEFAULT_LIMIT
        count = min(max(count, 1), _PAGE_MAX_LIMIT)
        total = len(self._events)
        end = min(max(end, 1), total + 1)
        start = max(end - count, 1)
        window = tuple(event for event in self._events if start <= event.seq < end)
        has_more = start > 1
        next_before = (
            _encode_cursor({"v": _PAGE_TOKEN_VERSION, "gen": self._gen, "end": start})
            if has_more
            else None
        )
        return PageResult(events=window, before=next_before, has_more=has_more,
                          generation=str(self._gen))

    # -- sync machinery ----------------------------------------------------

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
        if not self._materialized:
            self._resume_poll(stat)
            return
        if self._dev is not None and (stat.st_dev != self._dev or stat.st_ino != self._ino):
            self._rebuild(stat, fresh_gen=self._gen + 1)
            return
        if stat.st_size < self._size:
            self._rebuild(stat, fresh_gen=self._gen + 1)
            return
        if stat.st_size == self._size and stat.st_mtime_ns == self._mtime_ns:
            return
        with open(self._path, "rb") as handle:
            if not self._verify_fingerprint(handle, stat):
                self._rebuild(stat, fresh_gen=self._gen + 1)
                return
            if stat.st_size == self._size:
                self._mtime_ns = stat.st_mtime_ns
                return
            self._consume_appended(handle, stat)

    def _cold_open(self, stat: os.stat_result) -> None:
        saved = self._pending_cursor
        self._pending_cursor = None
        self._initialized = True
        if saved is not None:
            if self._try_cheap_resume(stat, saved):
                return
            self._rebuild(stat, fresh_gen=self._resume_gen(saved) + 1)
            return
        # No cursor: one full build, but only a tail window is returned, so
        # opening a huge history stays a bounded response with global seqs.
        self._rebuild(stat, fresh_gen=2 if self._pending_invalid else 1, tail=True)

    def _resume_gen(self, saved: dict[str, Any]) -> int:
        try:
            return max(int(saved.get("gen", 1)), 1)
        except (TypeError, ValueError):
            return 1

    def _try_cheap_resume(self, stat: os.stat_result, saved: dict[str, Any]) -> bool:
        """Resume without a full parse while the fingerprint still validates."""
        try:
            if (int(saved.get("size", -1)) != stat.st_size
                    or int(saved.get("dev", -1)) != stat.st_dev
                    or int(saved.get("ino", -1)) != stat.st_ino):
                return False
        except (TypeError, ValueError):
            return False
        with open(self._path, "rb") as handle:
            if not self._verify_saved(handle, stat, saved):
                return False
        self._gen = self._resume_gen(saved)
        state = str(saved.get("state") or "")
        self._state = state if state in {"available", "empty"} else "unavailable"
        self._outcome = _decode_outcome(saved.get("outcome"))
        self._size = stat.st_size
        self._boundary = str(saved.get("boundary") or "")
        self._head = str(saved.get("head") or "")
        try:
            self._head_len = max(int(saved.get("head_len", 0)), 0)
        except (TypeError, ValueError):
            self._head_len = 0
        self._dev = stat.st_dev
        self._ino = stat.st_ino
        self._mtime_ns = stat.st_mtime_ns
        try:
            self._next_lineno = max(int(saved.get("lines", 1)), 1)
        except (TypeError, ValueError):
            self._next_lineno = 1
        self._materialized = False
        self._events = []
        self._feed = None
        self._acc = None
        self._last_reset = False
        self._new_events = []
        return True

    def _resume_poll(self, stat: os.stat_result) -> None:
        """A lazily-resumed reader polls cheaply until the file changes."""
        if (stat.st_size == self._size and stat.st_dev == self._dev
                and stat.st_ino == self._ino):
            with open(self._path, "rb") as handle:
                if self._tail_boundary(handle, self._size) == self._boundary:
                    self.bytes_read += min(self._size, 65536)
                    self._mtime_ns = stat.st_mtime_ns
                    return
            self._rebuild(stat, fresh_gen=self._gen + 1)
            return
        self._rebuild(stat, fresh_gen=self._gen + 1)

    def _rebuild(self, stat: os.stat_result, fresh_gen: int, tail: bool = False) -> None:
        """Full build in one pass; parsed rows are freed afterwards.

        When ``tail`` is true (cursor-less cold open) only the tail window
        of events is delivered, keeping the response bounded while seqs
        stay snapshot-global.
        """
        with open(self._path, "rb") as handle:
            data = handle.read()
            head_len = min(_HEAD_FP_BYTES, len(data))
            head = _head_checksum(handle, head_len)
        self.bytes_read += len(data)
        lines, _remainder = _split_complete(data)
        committed = len(data) - len(_remainder)
        rows = _parse_json_rows(lines)
        self.lines_parsed += len(lines)
        self.full_parses += 1
        feed = self._new_feed()
        acc = self._new_acc()
        feed.feed(rows)
        feed.finish_snapshot()
        for lineno, entry in rows:
            acc.add(lineno, entry)
        self._feed = feed
        self._acc = acc
        self._events = feed.events
        self._outcome = acc.result()
        self._state = "available" if feed.events else "empty"
        self._size = committed
        self._next_lineno = len(lines) + 1
        last = lines[-1] if lines else b""
        self._last_line = last
        if last:
            self._line_start = committed - len(last)
            self._boundary = _boundary_checksum(last)
        else:
            self._line_start = committed
            self._boundary = _boundary_checksum(b"")
        self._head = head
        self._head_len = head_len
        self._dev = stat.st_dev
        self._ino = stat.st_ino
        self._mtime_ns = stat.st_mtime_ns
        self._gen = fresh_gen
        self._initialized = True
        self._materialized = True
        self._last_reset = True
        if tail and len(self._events) > self._tail_events:
            self._new_events = list(self._events[-self._tail_events:])
        else:
            self._new_events = list(self._events)

    def _materialize_full(self) -> None:
        try:
            stat = os.stat(self._path)
        except OSError:
            self._mark_unavailable()
            return
        self._rebuild(stat, fresh_gen=self._gen if self._gen > 0 else 1)
        self._last_reset = False
        self._new_events = []

    def _consume_appended(self, handle: Any, stat: os.stat_result) -> None:
        handle.seek(self._size)
        data = handle.read(stat.st_size - self._size)
        self.bytes_read += len(data)
        lines, _remainder = _split_complete(data)
        if not lines:
            self._mtime_ns = stat.st_mtime_ns
            return
        base = self._next_lineno - 1
        batch = [(base + lineno, entry)
                 for lineno, entry in _parse_json_rows(lines)]
        self.lines_parsed += len(lines)
        assert self._feed is not None and self._acc is not None
        fresh = self._feed.feed(batch)
        finished = self._feed.finish_snapshot()
        for lineno, entry in batch:
            self._acc.add(lineno, entry)
        self._next_lineno = base + len(lines) + 1
        self._new_events = list(fresh) + list(finished)
        self._outcome = self._acc.result()
        self._state = "available" if self._events else "empty"
        advanced = self._size + sum(len(line) for line in lines)
        self._size = advanced
        last = lines[-1]
        self._last_line = last
        self._line_start = advanced - len(last)
        self._boundary = _boundary_checksum(last)
        self._mtime_ns = stat.st_mtime_ns
        self._last_reset = False

    def _verify_fingerprint(self, handle: Any, stat: os.stat_result) -> bool:
        if _head_checksum(handle, self._head_len) != self._head:
            return False
        handle.seek(self._line_start)
        data = handle.read(self._size - self._line_start)
        self.bytes_read += len(data)
        return _boundary_checksum(data) == self._boundary

    def _verify_saved(self, handle: Any, stat: os.stat_result, saved: dict[str, Any]) -> bool:
        try:
            head_len = max(int(saved.get("head_len", 0)), 0)
        except (TypeError, ValueError):
            return False
        if _head_checksum(handle, head_len) != str(saved.get("head") or ""):
            return False
        return self._tail_boundary(handle, stat.st_size) == str(saved.get("boundary") or "")

    @staticmethod
    def _tail_boundary(handle: Any, size: int) -> str:
        handle.seek(max(0, size - 65536))
        tail = handle.read()
        lines, _remainder = _split_complete(tail)
        if not lines:
            return _boundary_checksum(b"")
        return _boundary_checksum(lines[-1])

    def _mark_unavailable(self) -> None:
        had_history = self._initialized and self._gen > 0 and self._state != "unavailable"
        self._state = "unavailable"
        self._outcome = SessionOutcome("unknown", Evidence("unknown"))
        self._new_events = []
        self._last_reset = had_history

    def _export_cursor(self) -> str | None:
        if self._state == "unavailable":
            return None
        return _encode_cursor({
            "v": _READER_CURSOR_VERSION,
            "runtime": self.RUNTIME,
            "path": self._path,
            "session": self._session_id,
            "dev": self._dev,
            "ino": self._ino,
            "size": self._size,
            "lines": self._next_lineno,
            "head": self._head,
            "head_len": self._head_len,
            "boundary": self._boundary,
            "events": len(self._events),
            "gen": self._gen,
            "state": self._state,
            "outcome": _encode_outcome(self._outcome),
            "aux": self._aux_state(),
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


class ClaudeActivityReader(_JsonlActivityReaderBase):
    """Incremental reader over a Claude Code JSONL session file."""

    RUNTIME = "claude"

    def _new_feed(self) -> _ClaudeFeed:
        return _ClaudeFeed()

    def _new_acc(self) -> _ClaudeOutcomeAcc:
        return _ClaudeOutcomeAcc()

    def _aux_state(self) -> dict[str, Any]:
        feed = self._feed
        last = self._events[-1] if self._events else None
        pending = None
        unresolved: list[str] = []
        if feed is not None:
            pending = feed.pending_error
            calls = feed.calls
            results = feed.results
            unresolved = sorted(call_id for call_id in calls if call_id and call_id not in results)
        return {
            "pending": list(pending) if pending is not None else None,
            "last_emit": _dedup_tail_hash(last),
            "unresolved": unresolved[:_AUX_CALL_CAP],
            "unresolved_truncated": len(unresolved) > _AUX_CALL_CAP,
        }


class CodexActivityReader(_JsonlActivityReaderBase):
    """Incremental reader over a Codex rollout JSONL session file."""

    RUNTIME = "codex"

    def _new_feed(self) -> _CodexFeed:
        return _CodexFeed()

    def _new_acc(self) -> _CodexOutcomeAcc:
        return _CodexOutcomeAcc()

    def _aux_state(self) -> dict[str, Any]:
        feed = self._feed
        last = self._events[-1] if self._events else None
        turn: str | None = None
        unresolved: list[str] = []
        if feed is not None:
            turn = feed.current_turn
            calls = feed.calls
            results = feed.results
            unresolved = sorted(call_id for call_id in calls if call_id and call_id not in results)
        return {
            "turn": turn,
            "last_emit": _dedup_tail_hash(last),
            "unresolved": unresolved[:_AUX_CALL_CAP],
            "unresolved_truncated": len(unresolved) > _AUX_CALL_CAP,
        }
