"""Owned regressions for MS-02 (bounded admission/backpressure) and MS-03 (borrowed-fd lifetime).

Scope: sesskit.stream_source.NativeJSONSource / NativeEventSource, plus the original
user-path defect through open_message_stream with dense Pi cumulative events. No real
histories, providers, servers, or credentials. Synthetic fixtures live in tmp_path.
"""

from __future__ import annotations

import json
import os
from contextlib import ExitStack

import pytest

from sesskit import open_message_stream
from sesskit.stream_source import NativeEventSource, NativeJSONSource


def _ndjson_lines(count, *, final="FINAL"):
    lines = []
    for index in range(count - 1):
        lines.append(json.dumps({"type": "draft", "seq": index}, separators=(",", ":")))
    lines.append(json.dumps({"type": final, "seq": count - 1}, separators=(",", ":")))
    return "\n".join(lines) + "\n"


def _sse_frames(count, *, final="FINAL"):
    frames = []
    for index in range(count - 1):
        frames.append(f'data: {{"type":"draft","seq":{index}}}\n\n')
    frames.append(f'data: {{"type":"{final}","seq":{count - 1}}}\n\n')
    return "".join(frames)


def _pi_message(text, typ="message_update"):
    return {"type": typ, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _drain(source, *, limit=256, max_polls=16):
    collected = []
    batches = []
    for _ in range(max_polls):
        batch = source.poll(limit)
        batches.append(batch)
        collected.extend(batch.events)
        if not batch.connected:
            break
    return collected, batches


def _adopt(path):
    """Open path and hand sole ownership of the handle to a new source.

    The caller keeps no reference, so only the source's own retention keeps the
    descriptor alive. Release with _close_source.
    """
    fd = os.open(os.fspath(path), os.O_RDONLY)
    try:
        return NativeJSONSource(os.fdopen(fd, "rb", buffering=0))
    except BaseException:
        os.close(fd)
        raise


def _open_held(path):
    """Open path for a test that manages the owner handle explicitly."""
    fd = os.open(os.fspath(path), os.O_RDONLY)
    try:
        return os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _close_source(source):
    retained = getattr(source, "_handle", None)
    try:
        source.close()
    finally:
        if retained is not None and not retained.closed:
            retained.close()


@pytest.fixture
def pi_history(tmp_path):
    path = tmp_path / "session.jsonl"
    rows = [
        {"type": "session", "version": 3, "id": "session1", "cwd": str(tmp_path)},
        {"type": "message", "id": "u1", "parentId": None, "message": {"role": "user", "content": "hello"}},
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "saved"}],
                "stopReason": "stop",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path, {"source": "pi", "id": "session1", "path": str(path)}


def test_dense_ndjson_preserves_final_through_bounded_polls(tmp_path):
    path = tmp_path / "dense.ndjson"
    path.write_bytes(_ndjson_lines(300).encode())
    with path.open("rb") as handle:
        source = NativeJSONSource(handle)
        try:
            collected, batches = _drain(source)
        finally:
            source.close()
    assert len(collected) == 300
    assert collected[-1] == {"type": "FINAL", "seq": 299}
    assert batches[-1].reason == "source_eof"
    assert all(len(b.events) <= 256 for b in batches)


def test_dense_sse_preserves_final_through_bounded_polls(tmp_path):
    path = tmp_path / "dense.sse"
    path.write_bytes(_sse_frames(300).encode())
    with path.open("rb") as handle:
        source = NativeJSONSource(handle, format="sse")
        try:
            collected, batches = _drain(source)
        finally:
            source.close()
    assert len(collected) == 300
    assert collected[-1] == {"type": "FINAL", "seq": 299}
    assert batches[-1].reason == "source_eof"


def test_small_poll_limit_still_drains_dense_input(tmp_path):
    path = tmp_path / "dense.ndjson"
    path.write_bytes(_ndjson_lines(300).encode())
    with path.open("rb") as handle:
        source = NativeJSONSource(handle)
        try:
            collected, batches = _drain(source, limit=50, max_polls=16)
        finally:
            source.close()
    assert len(collected) == 300
    assert all(len(b.events) <= 50 for b in batches)
    assert collected[-1] == {"type": "FINAL", "seq": 299}


def test_split_lines_across_reads_reassemble(tmp_path):
    payload = {"type": "wide", "text": "x" * 500}
    body = "".join(json.dumps({**payload, "seq": i}, separators=(",", ":")) + "\n" for i in range(300))
    assert len(body.encode()) > 65536
    path = tmp_path / "wide.ndjson"
    path.write_bytes(body.encode())
    with path.open("rb") as handle:
        source = NativeJSONSource(handle)
        try:
            collected, batches = _drain(source, max_polls=16)
        finally:
            source.close()
    assert len(collected) == 300
    assert collected[-1]["seq"] == 299
    assert batches[-1].reason == "source_eof"


def test_temporary_handle_stays_alive(tmp_path):
    path = tmp_path / "kept.ndjson"
    path.write_bytes(b'{"type":"kept"}\n')
    source = _adopt(path)
    try:
        batch = source.poll()
        assert batch.events == ({"type": "kept"},)
    finally:
        _close_source(source)


def test_explicit_owner_close_then_fd_reuse_cannot_ingest(tmp_path):
    owned = tmp_path / "owned.ndjson"
    owned.write_bytes(b'{"type":"owned"}\n')
    foreign = tmp_path / "foreign.ndjson"
    foreign.write_bytes(b'{"type":"FROM-FOREIGN-FD"}\n')
    with ExitStack() as stack:
        handle = stack.enter_context(_open_held(owned))
        source = NativeJSONSource(handle)
        recycled_fd = handle.fileno()
        handle.close()
        holders = [stack.enter_context(_open_held(foreign)) for _ in range(8)]
        try:
            assert holders[0].fileno() == recycled_fd
            batch = source.poll()
            assert batch.events == ()
            assert not batch.connected
            assert batch.reason == "source_failed"
        finally:
            source.close()


def test_source_close_leaves_open_owner_handle_open(tmp_path):
    path = tmp_path / "owned.ndjson"
    path.write_bytes(b'{"type":"owned"}\n')
    with path.open("rb") as handle:
        source = NativeJSONSource(handle)
        source.close()
        source.close()
        assert not handle.closed
        assert os.fstat(handle.fileno())
        batch = source.poll()
        assert batch.events == ()


def test_callback_overflow_still_truthful_with_accepted_prefix():
    source = NativeEventSource(capacity=1)
    assert source.feed({"type": "accepted"})
    assert not source.feed({"type": "lost"})
    batch = source.poll()
    assert batch.events == ({"type": "accepted"},)
    assert not batch.connected
    assert batch.reason == "consumer_overflow"
    assert batch.gap


def test_incomplete_ndjson_across_polls_then_completes():
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as handle:
        source = NativeJSONSource(handle)
        try:
            os.write(write_fd, b'{"type":"partial"')
            assert source.poll().events == ()
            os.write(write_fd, b"}\n")
            assert source.poll().events == ({"type": "partial"},)
        finally:
            os.close(write_fd)
            source.close()


def test_incomplete_sse_frame_dropped_at_eof():
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as handle:
        source = NativeJSONSource(handle, format="sse")
        try:
            os.write(write_fd, b'data: {"type":"partial"}\n')
            os.close(write_fd)
            write_fd = None
            assert source.poll().events == ()
            batch = source.poll()
            assert batch.events == ()
            assert not batch.connected
            assert batch.reason == "source_eof"
        finally:
            if write_fd is not None:
                os.close(write_fd)
            source.close()


def test_dense_pi_cumulative_final_before_eof_fallback(pi_history):
    history_path, session = pi_history
    feed_path = history_path.parent / "pi-live.ndjson"
    events = [{"type": "message_start", "message": {"role": "assistant", "content": []}}]
    events.extend(_pi_message(f"draft-{index}") for index in range(298))
    events.append(_pi_message("FINAL-ANSWER", "message_end"))
    feed_path.write_text("".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events))
    with ExitStack() as stack:
        handle = stack.enter_context(feed_path.open("rb"))
        source = NativeJSONSource(handle)
        try:
            with open_message_stream(session, live_source=source) as stream:
                live_seen = False
                final_at = None
                fallback_at = None
                reasons = set()
                for poll_index in range(12):
                    records = stream.poll()
                    for record in records:
                        reasons.add(record.reason)
                        if record.mode == "live":
                            live_seen = True
                        if (
                            final_at is None
                            and record.type == "upsert"
                            and record.complete is True
                            and record.event.get("type") == "assistant_message"
                            and record.event.get("text") == "FINAL-ANSWER"
                        ):
                            final_at = poll_index
                    if (
                        fallback_at is None
                        and live_seen
                        and any(
                            r.type == "upsert"
                            and r.mode == "history"
                            and r.event.get("type") == "assistant_message"
                            and r.event.get("text") == "saved"
                            for r in records
                        )
                    ):
                        fallback_at = poll_index
        finally:
            source.close()
    assert final_at is not None
    assert fallback_at is not None and fallback_at >= final_at
    assert "consumer_overflow" not in reasons
