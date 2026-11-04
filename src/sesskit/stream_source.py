"""Bounded, observer-only native event inputs. No agent execution or network ownership."""

from __future__ import annotations

import json
import os
import select
import threading
from collections import deque
from dataclasses import dataclass
from typing import BinaryIO

MAX_EVENT_BYTES = 1 << 20


def json_event(value: object, *, max_bytes: int | None = MAX_EVENT_BYTES) -> tuple[dict, int]:
    """Copy a strict JSON object, refusing lossy keys, non-finite values and cycles."""

    def check(item: object, depth: int = 0) -> None:
        if depth > 80:
            raise ValueError("native event nesting exceeds limit")
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("native event object keys must be strings")
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        else:
            raise ValueError("native event must contain plain JSON values")

    if type(value) is not dict:
        raise ValueError("native event must be an object")
    check(value)
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if max_bytes is not None and len(raw) > max_bytes:
        raise ValueError("native event exceeds byte limit")
    return json.loads(raw), len(raw)


@dataclass(frozen=True)
class NativeBatch:
    events: tuple[dict, ...]
    connected: bool
    epoch: int = 1
    reason: str | None = None
    gap: bool = False


class NativeEventSource:
    """Thread-safe callback bridge for an existing SDK/server subscription.

    ``feed`` never raises for a malformed event or an overflowing consumer. It
    refuses further input and signals degradation instead, so a display callback
    cannot abort its producer. Reconnect explicitly after repairing the input.
    Closing this bridge only removes its queued observations, never the agent.
    """

    def __init__(self, *, capacity: int = 256, max_bytes: int = 8 << 20):
        if type(capacity) is not int or capacity < 1 or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("source bounds must be positive integers")
        self._capacity, self._max_bytes = capacity, max_bytes
        self._queue: deque[tuple[dict, int]] = deque()
        self._bytes = 0
        self._connected = True
        self._epoch = 1
        self._reason: str | None = None
        self._gap = False
        self._lock = threading.Lock()

    def feed(self, event: dict) -> bool:
        try:
            value, size = json_event(event)
        except (ValueError, TypeError, RecursionError, OverflowError):
            self.disconnect("invalid_event", gap=True)
            return False
        with self._lock:
            if not self._connected:
                return False
            if len(self._queue) >= self._capacity or self._bytes + size > self._max_bytes:
                self._connected, self._reason, self._gap = False, "consumer_overflow", True
                return False
            self._queue.append((value, size))
            self._bytes += size
        return True

    def poll(self, limit: int = 256) -> NativeBatch:
        if type(limit) is not int or limit < 1:
            raise ValueError("poll limit must be a positive integer")
        with self._lock:
            events = []
            while self._queue and len(events) < limit:
                value, size = self._queue.popleft()
                self._bytes -= size
                events.append(value)
            # Drain accepted events before announcing the disconnection.
            connected = self._connected or bool(self._queue)
            return NativeBatch(tuple(events), connected, self._epoch, self._reason, self._gap)

    def disconnect(self, reason: str = "source_disconnected", *, gap: bool = True) -> None:
        # Reasons are machine codes; never retain arbitrary transport exception text.
        allowed = {"source_disconnected", "source_eof", "source_failed", "invalid_event", "consumer_overflow"}
        with self._lock:
            self._connected = False
            self._reason = reason if reason in allowed else "source_failed"
            self._gap = gap

    def reconnect(self) -> None:
        with self._lock:
            self._queue.clear()
            self._bytes = 0
            self._epoch += 1
            self._connected, self._reason, self._gap = True, None, True

    def close(self) -> None:
        self.disconnect()
        with self._lock:
            self._queue.clear()
            self._bytes = 0


class NativeJSONSource(NativeEventSource):
    """Read available NDJSON or SSE frames from a borrowed file descriptor.

    Intended for POSIX pipes, regular files and stdin. No reader thread, file
    flag mutation, child process, or socket ownership is introduced. The caller
    owns and closes the input handle; closing SessKit cannot terminate a producer.
    """

    def __init__(self, handle: BinaryIO, *, format: str = "ndjson"):
        super().__init__()
        if format not in {"ndjson", "sse"}:
            raise ValueError("input format must be ndjson or sse")
        # Retain the caller's handle without closing or duplicating it: keeping
        # a strong reference stops the descriptor number from being recycled
        # while this source still reads from it. The caller still owns and
        # closes the input; close() below never touches it.
        self._handle = handle
        self._fd = handle.fileno()
        self._format = format
        self._pending = bytearray()
        self._data: list[bytes] = []
        self._data_size = 0
        # An SSE frame completed while the admission queue was full. Its data
        # lines are already consumed from _pending, so the frame waits here
        # instead of being dropped or merged with the next frame.
        self._frame_ready = False
        self._admitted = 0
        self._poll_limit = 256
        self._ended = False
        try:
            identity = os.fstat(self._fd)
        except OSError:
            self._identity: tuple[int, int] | None = None
        else:
            self._identity = (identity.st_ino, identity.st_dev)

    def _handle_failed(self) -> bool:
        """Fail closed when the caller-owned handle is observably unusable.

        Returns True without reading when the retained handle is closed,
        detached, or no longer the same open file as at construction (explicit
        owner close followed by descriptor reuse). This check races a caller
        thread that closes concurrently with poll(); such concurrent close
        remains prohibited and is not claimed to be proven safe.
        """
        handle = self._handle
        try:
            if handle.closed:
                return True
            current = handle.fileno()
        except (AttributeError, OSError, ValueError):
            return True
        if current != self._fd:
            return True
        if self._identity is not None:
            try:
                fresh = os.fstat(self._fd)
            except OSError:
                return True
            if (fresh.st_ino, fresh.st_dev) != self._identity:
                return True
        return False

    def _admit_dict(self, event: dict) -> str:
        """Admit one decoded event: "admitted", "backpressure", or "ended".

        Backpressure (per-poll budget exhausted, or the event fits only after
        the consumer drains) preserves the caller-visible input for a later
        poll and leaves the bridge state untouched. Genuine overflow — an event
        that cannot fit even an empty queue — and invalid input end the reader
        with the same reasons feed() reports, preserving the accepted prefix.
        """
        if self._admitted >= self._poll_limit:
            return "backpressure"
        try:
            value, size = json_event(event)
        except (ValueError, TypeError, RecursionError, OverflowError):
            with self._lock:
                self._connected, self._reason, self._gap = False, "invalid_event", True
            self._ended = True
            return "ended"
        with self._lock:
            if not self._connected:
                self._ended = True
                return "ended"
            if len(self._queue) < self._capacity and self._bytes + size <= self._max_bytes:
                self._queue.append((value, size))
                self._bytes += size
                self._admitted += 1
                return "admitted"
        if size <= self._max_bytes:
            return "backpressure"
        with self._lock:
            self._connected, self._reason, self._gap = False, "consumer_overflow", True
        self._ended = True
        return "ended"

    def _admit_raw(self, raw: bytes) -> str:
        return self._admit_dict(json.loads(raw.decode("utf-8")))

    def _line(self, raw: bytes) -> str:
        """Consume one newline-terminated line: "consumed", "backpressure", or "ended".

        Backpressure leaves the input unit intact for a later poll: the raw
        line stays in _pending (restored by the caller) or, for a completed
        SSE frame, the data lines stay in _data with _frame_ready set.
        Malformed input raises for the poll handler to report.
        """
        if self._format == "sse":
            if not raw:
                if not self._data:
                    return "consumed"
                outcome = self._admit_raw(b"\n".join(self._data))
                if outcome == "backpressure":
                    self._frame_ready = True
                    return "backpressure"
                self._data, self._data_size = [], 0
                return "consumed" if outcome == "admitted" else "ended"
            elif raw.startswith(b"data:"):
                value = raw[5:].removeprefix(b" ")
                self._data.append(value)
                self._data_size += len(value)
                if self._data_size > MAX_EVENT_BYTES:
                    raise ValueError("SSE frame exceeds byte limit")
                return "consumed"
            else:
                return "consumed"
        if not raw.strip():
            return "consumed"
        outcome = self._admit_raw(raw)
        return "consumed" if outcome == "admitted" else outcome

    def _admit_ready(self) -> None:
        """Admit complete buffered units within the per-poll budget.

        A backpressured SSE frame goes first to preserve order; then complete
        newline-terminated lines. A backpressured NDJSON line is restored byte
        for byte to the front of _pending. A fatal unit clears _pending.
        """
        if self._frame_ready:
            outcome = self._admit_raw(b"\n".join(self._data))
            if outcome == "backpressure":
                return
            self._data, self._data_size = [], 0
            self._frame_ready = False
            if outcome == "ended":
                self._pending.clear()
                return
        while b"\n" in self._pending and not self._ended:
            raw, _, rest = self._pending.partition(b"\n")
            self._pending = bytearray(rest)
            if len(raw) > MAX_EVENT_BYTES:
                raise ValueError("native line exceeds byte limit")
            outcome = self._line(bytes(raw).removesuffix(b"\r"))
            if outcome == "backpressure":
                self._pending = bytearray(bytes(raw) + b"\n") + self._pending
                return
            if outcome == "ended":
                self._pending.clear()
                return

    def _finish_eof(self) -> None:
        """End at EOF only when no complete unit still awaits admission."""
        if self._format == "ndjson" and self._pending and b"\n" not in self._pending:
            tail = bytes(self._pending)
            self._pending.clear()
            if tail.strip() and self._admit_raw(tail) == "backpressure":
                self._pending = bytearray(tail)
                return
        elif self._frame_ready or (self._pending and b"\n" in self._pending):
            return
        if self._ended:
            return
        # EOF is not a completed agent turn, nor a complete SSE
        # frame. A final non-newline NDJSON object is valid.
        self._pending.clear()
        self._data.clear()
        self._data_size = 0
        self._ended = True
        self.disconnect("source_eof", gap=True)

    def poll(self, limit: int = 256) -> NativeBatch:
        if type(limit) is not int or limit < 1:
            raise ValueError("poll limit must be a positive integer")
        if not self._ended:
            try:
                if self._handle_failed():
                    self._ended = True
                    self.disconnect("source_failed", gap=True)
                else:
                    self._poll_limit = limit
                    self._admitted = 0
                    eof = False
                    if b"\n" not in self._pending and not self._frame_ready:
                        # One bounded read per poll; existing Python-buffered reads must
                        # not be mixed with this descriptor reader by the caller.
                        readable, _, _ = select.select([self._fd], [], [], 0)
                        if readable:
                            chunk = os.read(self._fd, 65536)
                            if chunk:
                                self._pending.extend(chunk)
                            else:
                                eof = True
                    if not self._ended:
                        self._admit_ready()
                    if eof and not self._ended:
                        self._finish_eof()
                    if not self._ended and b"\n" not in self._pending and len(self._pending) > MAX_EVENT_BYTES:
                        raise ValueError("native line exceeds byte limit")
            except (OSError, ValueError, TypeError, RecursionError, OverflowError):
                self._ended = True
                self.disconnect("invalid_event", gap=True)
        return super().poll(limit)

    def close(self) -> None:
        self._ended = True
        self._pending.clear()
        self._data.clear()
        self._data_size = 0
        self._frame_ready = False
        super().close()
