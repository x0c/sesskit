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
        self._fd = handle.fileno()
        self._format = format
        self._pending = bytearray()
        self._data: list[bytes] = []
        self._data_size = 0
        self._ended = False

    def _line(self, raw: bytes) -> None:
        if self._format == "sse":
            if not raw:
                if not self._data:
                    return
                raw = b"\n".join(self._data)
                self._data, self._data_size = [], 0
            elif raw.startswith(b"data:"):
                value = raw[5:].removeprefix(b" ")
                self._data.append(value)
                self._data_size += len(value)
                if self._data_size > MAX_EVENT_BYTES:
                    raise ValueError("SSE frame exceeds byte limit")
                return
            else:
                return
        if raw.strip() and not self.feed(json.loads(raw.decode("utf-8"))):
            self._ended = True

    def poll(self, limit: int = 256) -> NativeBatch:
        if not self._ended:
            try:
                # One bounded read per poll; existing Python-buffered reads must
                # not be mixed with this descriptor reader by the caller.
                readable, _, _ = select.select([self._fd], [], [], 0)
                if readable:
                    chunk = os.read(self._fd, 65536)
                    if chunk:
                        self._pending.extend(chunk)
                        while b"\n" in self._pending:
                            raw, _, rest = self._pending.partition(b"\n")
                            self._pending = bytearray(rest)
                            if len(raw) > MAX_EVENT_BYTES:
                                raise ValueError("native line exceeds byte limit")
                            self._line(bytes(raw).removesuffix(b"\r"))
                            if self._ended:
                                self._pending.clear()
                                break
                        if len(self._pending) > MAX_EVENT_BYTES:
                            raise ValueError("native line exceeds byte limit")
                    else:
                        # EOF is not a completed agent turn, nor a complete SSE
                        # frame. A final non-newline NDJSON object is valid.
                        if self._pending and self._format == "ndjson":
                            self._line(bytes(self._pending))
                        self._pending.clear()
                        self._ended = True
                        self.disconnect("source_eof", gap=True)
            except (OSError, ValueError, TypeError, RecursionError):
                self._ended = True
                self.disconnect("invalid_event", gap=True)
        return super().poll(limit)

    def close(self) -> None:
        self._ended = True
        self._pending.clear()
        self._data.clear()
        super().close()
