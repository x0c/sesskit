"""Third-party message streaming with automatic persisted-history degradation.

Presentation and native transport/execution stay with the caller. Feed an
existing SDK/server subscription into NativeEventSource, or omit it to follow
history immediately. No process is launched, resumed, interrupted or cancelled.
"""

from __future__ import annotations

import copy
import json
import math
import os
import sqlite3
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from typing import Any

from sesskit.activity_reader import open_activity_reader, supports_incremental
from sesskit.models import ActivityEvent, Evidence, SessionOutcome, session_key
from sesskit.registry import ConversationLoadError, load_session_conversation
from sesskit.stream_native import NativeEventNormalizer
from sesskit.stream_source import NativeBatch, NativeEventSource, json_event

MESSAGE_STREAM_SCHEMA_ID = "sesskit.message-stream/v1"


@dataclass(frozen=True)
class MessageStreamRecord:
    """One ordered state/reset/upsert/remove/checkpoint/end observation.

    Upsert replaces the entire value at ``key``; never append ``event.text``.
    A reset replaces the displayed window, including across source switches.
    ``complete`` is per event, not process completion. ``outcome`` describes
    only the selected source's last evidenced historical turn.
    """

    schema: str
    session: str
    seq: int
    generation: str
    mode: str
    type: str
    state: str
    history_state: str
    key: str | None = None
    event: dict[str, Any] | None = None
    complete: bool | None = None
    reason: str | None = None
    gap: bool = False
    cursor: str | None = None
    outcome: dict[str, Any] | None = None
    truncated: bool = False
    error: dict[str, Any] | None = None


def message_stream_record_to_dict(record: MessageStreamRecord) -> dict:
    return asdict(record)


class MessageStream(AbstractContextManager["MessageStream"]):
    """Nonblocking observer with bounded display state and opaque checkpoints.

    ``poll`` performs one bounded source drain and one history poll. ``follow``
    adds interruptible waiting. Kimi/legacy adapters reuse public conversation
    snapshots instead of introducing another native parser. Readers may retain
    their own native indexes; ``max_events`` bounds this projection, not those
    existing history-reader internals. Instances are single-consumer; the input
    NativeEventSource supports concurrent producer callbacks.
    """

    def __init__(self, session: dict, *, live_source: NativeEventSource | None = None,
                 cursor: str | None = None, max_events: int = 200):
        runtime, sid = session.get("source"), session.get("id")
        if (not isinstance(runtime, str) or not 1 <= len(runtime) <= 64
                or not isinstance(sid, str) or not 1 <= len(sid) <= 1024):
            raise ValueError("streaming requires source (1..64 chars) and id (1..1024 chars)")
        if type(max_events) is not int or not 1 <= max_events <= 200:
            raise ValueError("max_events must be an integer between 1 and 200")
        self.session = dict(session)
        self.session_ref = session_key(session)
        self.max_events = max_events
        self.live_source = live_source
        self._cursor_data = self._decode_cursor(cursor)
        self._seq = self._cursor_data.get("seq", 0)
        self._generation = self._cursor_data.get("generation") or uuid.uuid4().hex
        self._mode: str | None = None
        self._state = "empty"
        self._history_state = "empty"
        self._history_cursor: str | None = None
        self._history_generation: str | None = None
        self._history_reset = False
        self._history: dict[str, dict] = {}
        self._display: OrderedDict[str, tuple[dict, bool | None]] = OrderedDict()
        self._history_outcome = SessionOutcome("unknown", Evidence("unknown"))
        self._native: NativeEventNormalizer | None = None
        self._live_active = False
        self._epoch: int | None = None
        self._last_status: tuple | None = None
        self._last_checkpoint: tuple | None = None
        self._legacy_version: tuple | None = None
        self._closed = False
        self._truncated = False
        self._gap = bool(cursor and not self._cursor_data)
        self._first_history_poll = True
        self._pending_reset = False
        self._reader = None
        if supports_incremental(session):
            native_cursor = self._cursor_data.get("history_cursor") if self._cursor_data.get("mode") == "history" else None
            self._reader = open_activity_reader(self.session, cursor=native_cursor)

    def _decode_cursor(self, cursor: str | None) -> dict:
        if cursor is None:
            return {}
        if not isinstance(cursor, str) or len(cursor) > 1 << 20:
            return {}
        try:
            data = json.loads(cursor)
        except (ValueError, TypeError):
            return {}
        if (not isinstance(data, dict) or data.get("schema") != MESSAGE_STREAM_SCHEMA_ID
                or data.get("session") != self.session_ref or data.get("max_events") != self.max_events
                or type(data.get("seq")) is not int or data["seq"] < 0
                or not isinstance(data.get("generation"), str)
                or data.get("mode") not in {"history", "live"}
                or data.get("history_cursor") is not None and not isinstance(data["history_cursor"], str)):
            return {}
        return data

    def _record(self, typ: str, **kwargs) -> MessageStreamRecord:
        self._seq += 1
        return MessageStreamRecord(
            schema=MESSAGE_STREAM_SCHEMA_ID, session=self.session_ref, seq=self._seq,
            generation=self._generation, mode=self._mode or "history", type=typ,
            state=self._state, history_state=self._history_state, gap=self._gap,
            truncated=self._truncated, **kwargs,
        )

    def _read_history(self) -> None:
        self._history_reset = False
        try:
            if self._reader is not None:
                result = self._reader.poll()
                self._history_state = result.state
                self._history_outcome = result.outcome
                self._history_cursor = result.cursor
                self._history_reset = result.reset
                self._history_generation = result.generation
                if result.state in {"available", "empty"}:
                    pending = {} if result.reset else dict(self._history)
                    events = result.events
                    if self._first_history_poll and not result.reset:
                        # A resumed reader returns only new records. Hydrate its
                        # current tail for an explicit idempotent display reset.
                        events = self._reader.page(limit=self.max_events).events
                        self._history_reset = True
                    self._first_history_poll = False
                    for event in events:
                        pending[f"history:{event.seq}"] = json_event(asdict(event), max_bytes=None)[0]
                    self._history = pending
                    # Reader cold loads may be a bounded tail, not all history.
                    self._truncated = bool(self._history and min(e["seq"] for e in self._history.values()) > 1)
                    keys = sorted(self._history, key=lambda key: self._history[key]["seq"])
                    if len(keys) > self.max_events:
                        self._truncated = True
                        for key in keys[:-self.max_events]:
                            self._history.pop(key)
            else:
                self._read_legacy_history()
        except ValueError:
            # Refuse non-JSON native values without committing a skipped cursor.
            self._history_state = "unavailable"
            self._history_cursor = None
            if self._reader is not None:
                self._reader = open_activity_reader(self.session)
                self._first_history_poll = True
        except (OSError, RuntimeError, sqlite3.Error, ConversationLoadError):
            # Unreadable/busy history is not an empty successful snapshot.
            self._history_state = "unavailable"

    def _read_legacy_history(self) -> None:
        path = self.session.get("path")
        if not isinstance(path, str) or not path:
            self._history_state = "unavailable"
            return
        stat = os.stat(path)
        version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if version == self._legacy_version:
            if self._history_state != "unavailable":
                return
            # Prior poll left history unavailable at this exact fingerprint.
            # A bare stat match is not availability evidence; reload below.
            prior_unavailable = True
            identity_changed = False
        else:
            prior_unavailable = self._history_state == "unavailable"
            identity_changed = (
                self._legacy_version is not None
                and (version[0], version[1]) != (self._legacy_version[0], self._legacy_version[1])
            )
        messages = load_session_conversation(self.session)
        # A racing writer invalidates this version again on the next poll.
        values = {}
        offset = max(0, len(messages) - self.max_events)
        for index, message in enumerate(messages[offset:], offset + 1):
            event = ActivityEvent(
                index, "user_message" if message.role == "user" else "assistant_message",
                Evidence("inferred", field="legacy_conversation_position"),
                text=message.text, ts=message.timestamp,
            )
            values[f"history:{index}"] = asdict(event)
        self._history_reset = (
            values != self._history
            or self._legacy_version is None
            or prior_unavailable
            or identity_changed
        )
        self._history, self._legacy_version = values, version
        self._history_state = "available" if values else "empty"
        self._truncated = offset > 0
        if self._history_reset:
            self._history_generation = uuid.uuid4().hex

    def _upsert(self, key: str, event: dict, complete: bool | None) -> list[MessageStreamRecord]:
        value = (event, complete)
        if self._display.get(key) == value:
            return []
        self._display[key] = copy.deepcopy(value)
        records = [self._record("upsert", key=key, event=copy.deepcopy(event), complete=complete)]
        while len(self._display) > self.max_events:
            old_key, _ = self._display.popitem(last=False)
            self._truncated = True
            records.append(self._record("remove", key=old_key, reason="window_limit"))
        return records

    def poll(self) -> tuple[MessageStreamRecord, ...]:
        if self._closed:
            raise RuntimeError("message stream is closed")
        self._read_history()
        records = []
        batch = NativeBatch((), False, reason="live_source_not_configured", gap=False)
        if self.live_source is not None:
            try:
                batch = self.live_source.poll(limit=64)
            except (OSError, ValueError, RuntimeError):
                batch = NativeBatch((), False, reason="source_failed", gap=True)
        epoch_changed = (batch.connected or bool(batch.events)) and batch.epoch != self._epoch
        updates = []
        try:
            if epoch_changed:
                self._native = NativeEventNormalizer(self.session["source"], self.session["id"], limit=self.max_events)
                self._epoch = batch.epoch
                self._live_active = False
                self._gap = self._gap or batch.gap or bool(self._cursor_data.get("mode") == "live")
            for raw in batch.events:
                updates.extend(self._native.normalize(raw))
            if self._native is not None:
                self._gap = self._gap or self._native.gap
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            if self.live_source is not None:
                self.live_source.disconnect("invalid_event", gap=True)
            batch = NativeBatch((), False, batch.epoch, "invalid_event", True)
        self._live_active = self._live_active or bool(updates)
        mode = "live" if updates or (batch.connected and self._live_active) else "history"
        reset = (mode != self._mode or (mode == "live" and epoch_changed) or self._pending_reset
                 or (mode == "history" and self._history_reset))
        self._state = "available" if mode == "live" else self._history_state
        reason = (batch.reason or "awaiting_native_events") if mode == "history" else None
        self._gap = self._gap or (not batch.connected and batch.gap)
        self._mode = mode
        status = (mode, self._state, self._history_state, reason, self._gap)
        if status != self._last_status:
            records.append(self._record("state", reason=reason))
            self._last_status = status
        # Do not erase the last visible data while the degraded store is unreadable.
        self._pending_reset = reset
        if reset and (mode == "live" or self._state in {"available", "empty"}):
            self._generation = uuid.uuid4().hex
            self._display.clear()
            self._pending_reset = False
            records.append(self._record("reset", reason="source_or_history_changed"))
        if mode == "history":
            if self._state in {"available", "empty"}:
                if not reset:
                    for key in list(self._display):
                        if key not in self._history:
                            self._display.pop(key)
                            records.append(self._record("remove", key=key, reason="window_limit"))
                for key, event in sorted(self._history.items(), key=lambda item: item[1]["seq"]):
                    records.extend(self._upsert(key, event, None))
        else:
            # A display needs the latest value per key, not every transport
            # fragment buffered during a poll. Coalescing bounds wire traffic
            # without inventing token timing or dropping the final state.
            latest = {update.key: update for update in updates}
            for update in latest.values():
                if update.operation == "remove":
                    if self._display.pop(update.key, None) is not None:
                        records.append(self._record("remove", key=update.key))
                elif update.event is not None:
                    records.extend(self._upsert(update.key, asdict(update.event), update.complete))
            # Accepted queued fragments are delivered before a subsequent poll
            # switches atomically to the authoritative history view.
            self._gap = self._gap or (not batch.connected and batch.gap)
        outcome = self._native.outcome if mode == "live" and self._native else self._history_outcome
        signature = (self._history_cursor, self._generation, self._mode, asdict(outcome), self._state)
        if records or signature != self._last_checkpoint:
            next_seq = self._seq + 1
            cursor = json.dumps({
                "schema": MESSAGE_STREAM_SCHEMA_ID, "session": self.session_ref,
                "max_events": self.max_events, "generation": self._generation,
                "mode": self._mode, "seq": next_seq, "history_cursor": self._history_cursor,
            }, separators=(",", ":"), sort_keys=True)
            records.append(self._record("checkpoint", cursor=cursor, outcome=asdict(outcome)))
            self._last_checkpoint = signature
        return tuple(records)

    @property
    def state(self) -> str:
        """Availability of the currently selected observation source."""
        return self._state

    def end(self, reason: str = "observer_stopped", *, truncated: bool = False,
            error: dict | None = None) -> MessageStreamRecord:
        """End this observation, never the agent or its owning transport."""
        if self._closed:
            raise RuntimeError("message stream is closed")
        self._truncated = self._truncated or truncated
        record = self._record("end", reason=reason, error=error)
        self.close()
        return record

    def follow(self, *, interval: float = 0.25, polls: int | None = None, stop_event=None) -> Iterator[MessageStreamRecord]:
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 0:
            raise ValueError("interval must be finite and nonnegative")
        if polls is not None and (type(polls) is not int or polls < 1):
            raise ValueError("polls must be a positive integer")
        if polls is None and interval < 0.01:
            raise ValueError("unbounded following requires an interval of at least 0.01")
        try:
            index = 0
            while polls is None or index < polls:
                if stop_event is not None and stop_event.is_set():
                    break
                if index:
                    if stop_event is not None:
                        if stop_event.wait(interval):
                            break
                    else:
                        time.sleep(interval)
                yield from self.poll()
                index += 1
            yield self.end("poll_limit" if polls is not None and index == polls else "observer_stopped")
        finally:
            self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self.live_source is not None:
                self.live_source.close()
            self._reader = None
            self._native = None
            self._history.clear()
            self._display.clear()

    def __exit__(self, *_args) -> None:
        self.close()


def open_message_stream(session: dict, *, live_source: NativeEventSource | None = None,
                        cursor: str | None = None, max_events: int = 200) -> MessageStream:
    """Observe an existing session, optionally normalizing a caller-owned live feed."""
    return MessageStream(session, live_source=live_source, cursor=cursor, max_events=max_events)
