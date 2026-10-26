"""Reader memory and streaming-rebuild contract.

Covers the transient-allocation change in the JSONL and Pi readers: a cold
or reset build streams fixed-size read batches through the shared
interpreters instead of materializing whole-file bytes, every split line and
all parsed rows together. These tests pin the exact split/commit semantics,
the bounded transient peak on synthetic large files, chunked appends, the
existing reset/page/tool-pairing behavior, and snapshot parity (including
batch-boundary linkage such as Codex token usage).

Synthetic fixtures only; no native history is read.
"""

from __future__ import annotations

import io
import json
import os
import tracemalloc

import pytest

from sesskit import load_activity
from sesskit.activity_reader import (
    _iter_committed_lines,
    _split_complete,
    open_activity_reader,
)

MiB = 1024 * 1024


# --- streaming splitter ---------------------------------------------------


def _stream(data: bytes, chunk_size: int) -> list[bytes]:
    return list(_iter_committed_lines(io.BytesIO(data), chunk_size))


@pytest.mark.parametrize("data", [
    b"",
    b"\n",
    b"a",
    b"a\n",
    b"a\r",
    b"a\rb",
    b"a\r\nb",
    b"a\r\n",
    b"\r",
    b"\r\n",
    b"\r\r\n\r",
    b"line one\nline two\r\nline three\rline four",
    b"trailing-partial\nstarted",
])
def test_streamed_lines_equal_full_file_split(data):
    expected, _remainder = _split_complete(data)
    for chunk_size in (1, 2, 3, 5, 64):
        assert _stream(data, chunk_size) == expected


def test_streamed_split_is_exact_for_arbitrary_byte_sequences():
    # Deterministic pseudo-random bytes over the terminators bytes.splitlines
    # recognizes plus ordinary content; every chunking must agree with the
    # full-file split used by the append path.
    import random

    rng = random.Random(20261025)
    alphabet = [b"a", b"b", b"{", b"}", b"\n", b"\r", b" ", b"\x00"]
    for _ in range(2000):
        data = b"".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 24)))
        expected, _remainder = _split_complete(data)
        for chunk_size in (1, 2, 4, 7):
            assert _stream(data, chunk_size) == expected


def test_line_larger_than_the_read_chunk_is_held_until_its_newline():
    long_line = b"x" * (3 * MiB)
    data = b'{"text": "' + long_line + b'"}\n' + b"tail"
    expected, remainder = _split_complete(data)
    assert remainder == b"tail"
    assert _stream(data, MiB) == expected


# --- synthetic large-file peak bounds ------------------------------------


def _write_lines(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(row) + "\n" for row in rows)


def _measure(path, runtime):
    session = {"source": runtime, "path": str(path), "id": path.stem}
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        reader = open_activity_reader(session)
        first = reader.poll()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return path.stat().st_size, first, retained, peak


def test_claude_streaming_build_does_not_retain_the_file(tmp_path):
    path = tmp_path / "big.jsonl"
    filler = "filler-" + "x" * 40
    _write_lines(path, [
        {"type": "system", "timestamp": "2026-09-01T00:00:00Z",
         "uuid": f"u-{index:06d}", "note": filler}
        for index in range(60000)
    ])
    size, first, retained, peak = _measure(path, "claude")
    assert first.state == "empty"
    assert retained < 2 * MiB
    # The old whole-file build held the file bytes plus every split line
    # (>= 2x size) before parsing; the streamed build stays far below one
    # copy of the source.
    assert peak < size


def test_codex_streaming_build_does_not_retain_the_file(tmp_path):
    path = tmp_path / "big.jsonl"
    _write_lines(path, [
        {"type": "event_msg", "timestamp": index,
         "payload": {"type": "token_count", "note": "filler-" + "x" * 40}}
        for index in range(60000)
    ])
    size, first, retained, peak = _measure(path, "codex")
    assert first.state == "empty"
    assert retained < 2 * MiB
    assert peak < size


def test_pi_streaming_build_adds_no_second_copy_of_the_file(tmp_path):
    path = tmp_path / "big.jsonl"
    rows = [{"type": "session", "version": 3, "id": "sess-big",
             "timestamp": "2026-09-29T10:00:00.000Z", "cwd": "/tmp/work"}]
    parent = None
    for index in range(12000):
        entry_id = f"m{index:05d}"
        rows.append({"type": "message", "id": entry_id, "parentId": parent,
                     "timestamp": "2026-09-29T10:01:00.000Z",
                     "message": {"role": "user", "content": "turn " + "y" * 40,
                                 "timestamp": index}})
        parent = entry_id
    _write_lines(path, rows)
    size, first, retained, peak = _measure(path, "pi")
    assert first.state == "available"
    # Pi keeps its native entry map and branch (retained); the transient
    # build cost must not add a second full copy of the file on top.
    assert peak - retained < size


# --- chunked append / partial tail ---------------------------------------


def test_partial_line_larger_than_a_chunk_is_held_back_then_committed(tmp_path):
    path = tmp_path / "s.jsonl"
    _write_lines(path, [{"type": "user", "timestamp": "2026-09-01T00:00:01Z",
                         "message": {"role": "user",
                                     "content": [{"type": "text", "text": "hi"}]}}])
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    first = reader.poll()
    assert [event.text for event in first.events] == ["hi"]
    cursor = reader.poll().cursor
    partial = b'{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "' \
        + b"z" * (2 * MiB)
    with open(path, "ab") as handle:
        handle.write(partial)
    held = reader.poll()
    assert held.events == ()
    assert held.cursor == cursor
    assert held.reset is False
    with open(path, "ab") as handle:
        handle.write(b'"}]}}\n')
    committed = reader.poll()
    assert committed.reset is False
    assert len(committed.events) == 1
    assert committed.events[0].type == "assistant_message"
    assert len(committed.events[0].text) == 2 * MiB


def test_cr_separated_history_keeps_the_hold_back_rule(tmp_path):
    path = tmp_path / "cr.jsonl"
    good = json.dumps({"type": "user", "timestamp": "2026-09-01T00:00:01Z",
                       "message": {"role": "user",
                                   "content": [{"type": "text", "text": "one"}]}})
    trailing = json.dumps({"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
                           "message": {"role": "assistant",
                                       "content": [{"type": "text", "text": "two"}]}})
    with open(path, "wb") as handle:
        handle.write(good.encode() + b"\r" + trailing.encode() + b"\r")
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    first = reader.poll()
    # The trailing lone \r record is not committed yet, matching the
    # previous full-file split; the earlier \r-separated record is.
    assert [event.text for event in first.events] == ["one"]
    assert json.loads(first.cursor)["size"] == len(good) + 1
    with open(path, "ab") as handle:
        handle.write(b"\n")
    second = reader.poll()
    assert second.reset is False
    assert [event.text for event in second.events] == ["two"]
    # The same bytes now project like the snapshot (universal newlines).
    assert [event.text for event in load_activity(session).events] == ["one", "two"]


def test_invalid_utf8_and_json_keep_snapshot_line_records(tmp_path):
    path = tmp_path / "broken.jsonl"
    rows = [
        {"type": "user", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "one"}]}},
        {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "two"}]}},
    ]
    with open(path, "wb") as handle:
        handle.write(json.dumps(rows[0]).encode() + b"\n")
        handle.write(b"\xff\xfe not utf-8\n")
        handle.write(b"{broken json line\n")
        handle.write(json.dumps(rows[1]).encode() + b"\n")
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    first = reader.poll()
    snapshot = load_activity(session)
    assert list(first.events) == list(snapshot.events)
    assert [event.evidence.record for event in first.events] == ["line:1", "line:4"]
    assert reader.lines_parsed == 4  # malformed/invalid lines still consume a line


def test_append_after_streamed_open_parses_only_new_bytes(tmp_path):
    path = tmp_path / "s.jsonl"
    base = [{"type": "user", "timestamp": "2026-09-01T00:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}}]
    _write_lines(path, base)
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    reader.poll()
    assert reader.bytes_read == path.stat().st_size  # one pass, not two
    assert reader.full_parses == 1
    lines_before, bytes_before = reader.lines_parsed, reader.bytes_read
    more = [{"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
             "message": {"role": "assistant",
                         "content": [{"type": "tool_use", "id": "call-big",
                                      "name": "Bash",
                                      "input": {"cmd": "true", "pad": "p" * 40}}]}},
            {"type": "user", "timestamp": "2026-09-01T00:00:03Z",
             "message": {"role": "user",
                         "content": [{"type": "tool_result", "tool_use_id": "call-big",
                                      "content": "y" * (2 * MiB), "is_error": False}]}}]
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(row) + "\n" for row in more)
    appended_bytes = path.stat().st_size - bytes_before
    appended = reader.poll()
    assert appended.reset is False
    assert reader.full_parses == 1  # no full re-parse
    assert reader.lines_parsed - lines_before == 2
    # Only the appended bytes plus one bounded boundary re-check are read.
    read_delta = reader.bytes_read - bytes_before
    assert appended_bytes < read_delta <= appended_bytes + 4096
    assert [event.type for event in appended.events] == ["tool_call", "tool_result"]
    assert list(appended.events) == list(load_activity(session).events)[-2:]
    assert reader.poll().events == ()


# --- batch-boundary linkage parity ---------------------------------------


def test_claude_batched_interaction_link_matches_snapshot(tmp_path, monkeypatch):
    from sesskit import activity_reader_jsonl as jsonl_reader

    monkeypatch.setattr(jsonl_reader, "_ROW_BATCH_ROWS", 2)
    path = tmp_path / "c.jsonl"
    rows = [
        {"type": "user", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "ask"}]}},
        {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant", "content": [{
             "type": "tool_use", "id": "call-q", "name": "AskUserQuestion",
             "input": {"questions": [{"question": "Which?", "options":
                                      [{"label": "a"}, {"label": "b"}]}]}}]}},
        {"type": "user", "timestamp": "2026-09-01T00:00:03Z",
         "message": {"role": "user", "content": [{
             "type": "tool_result", "tool_use_id": "call-q", "content": "a",
             "is_error": False}]}},
        {"type": "assistant", "timestamp": "2026-09-01T00:00:04Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "done"}]}},
    ]
    _write_lines(path, rows)
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    reader.poll()
    snapshot = load_activity(session)
    assert list(reader._events) == list(snapshot.events)
    call = next(event for event in reader._events if event.type == "tool_call")
    assert call.interaction is not None
    assert call.interaction.resolution == "answered"
    assert call.interaction.questions[0].prompt == "Which?"


def test_codex_batched_usage_link_attaches_only_to_the_last_turn_message(tmp_path, monkeypatch):
    from sesskit import activity_reader_jsonl as jsonl_reader

    monkeypatch.setattr(jsonl_reader, "_ROW_BATCH_ROWS", 2)
    path = tmp_path / "r.jsonl"
    rows = [
        {"type": "response_item", "timestamp": 1,
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "first"}],
                     "turn_id": "turn-1"}},
        {"type": "token_usage_record", "timestamp": 2,
         "payload": {"type": "token_usage_record", "turn_id": "turn-1",
                     "usage": {"input_tokens": 100, "output_tokens": 5,
                               "total_tokens": 105}}},
        {"type": "response_item", "timestamp": 3,
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "second"}],
                     "turn_id": "turn-1"}},
        {"type": "event_msg", "timestamp": 4,
         "payload": {"type": "task_complete", "last_agent_message": "done",
                     "turn_id": "turn-1"}},
    ]
    _write_lines(path, rows)
    session = {"source": "codex", "path": str(path), "id": "r"}
    reader = open_activity_reader(session)
    reader.poll()
    snapshot = load_activity(session)
    assert list(reader._events) == list(snapshot.events)
    with_usage = [event for event in reader._events if event.usage is not None]
    assert len(with_usage) == 1
    assert with_usage[0].text == "done"
    assert with_usage[0].usage.total_tokens == 105


# --- reset / page / branch semantics -------------------------------------


def test_truncation_and_replacement_reset_after_streamed_open(tmp_path):
    path = tmp_path / "s.jsonl"
    rows = [
        {"type": "user", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "one"}]}},
        {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "two"}]}},
    ]
    _write_lines(path, rows)
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    first = reader.poll()
    _write_lines(path, rows[:1])
    truncated = reader.poll()
    assert truncated.reset is True
    assert truncated.generation != first.generation
    assert [event.text for event in truncated.events] == ["one"]
    assert list(truncated.events) == list(load_activity(session).events)
    # Same size, different body: head/boundary fingerprints still catch it.
    replacement = rows[0]
    replacement["message"]["content"][0]["text"] = "uno"
    _write_lines(path, [replacement])
    # Same-size writes need a visible stamp to enter the fingerprint recheck.
    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    replaced = reader.poll()
    assert replaced.reset is True
    assert [event.text for event in replaced.events] == ["uno"]


def test_page_reassembly_and_tool_pairing_without_reparse(tmp_path):
    path = tmp_path / "c.jsonl"
    _write_lines(path, [
        {"type": "user", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "run"}]}},
        {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "call-9", "name": "Bash",
              "input": {"cmd": "ls"}}]}},
        {"type": "user", "timestamp": "2026-09-01T00:00:03Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "call-9", "content": "ok",
              "is_error": False}]}},
        {"type": "assistant", "timestamp": "2026-09-01T00:00:04Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "done"}]}},
    ])
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    full = reader.poll().events
    parses = reader.full_parses
    newest = reader.page(limit=2)
    older = reader.page(before=newest.before, limit=50)
    assert newest.has_more is True and older.has_more is False
    assert list(older.events) + list(newest.events) == list(full)
    assert reader.full_parses == parses  # paging slices, never re-parses
    call = next(event for event in older.events + newest.events
                if event.type == "tool_call")
    result = next(event for event in older.events + newest.events
                  if event.type == "tool_result")
    assert call.call_id == result.call_id == "call-9"


def test_pi_branch_switch_and_entries_survive_streaming(tmp_path):
    path = tmp_path / "s.jsonl"
    rows = [
        {"type": "session", "version": 3, "id": "sess-1",
         "timestamp": "2026-09-29T10:00:00.000Z", "cwd": "/tmp/work"},
        {"type": "message", "id": "m1", "parentId": None,
         "timestamp": "2026-09-29T10:01:00.000Z",
         "message": {"role": "user", "content": "first", "timestamp": 1}},
        {"type": "message", "id": "m2", "parentId": "m1",
         "timestamp": "2026-09-29T10:02:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "abandoned"}],
                     "stopReason": "stop", "timestamp": 2}},
    ]
    _write_lines(path, rows)
    session = {"source": "pi", "path": str(path), "id": "sess-1"}
    reader = open_activity_reader(session)
    first = reader.poll()
    assert any("abandoned" in (event.text or "") for event in first.events)
    assert [entry["id"] for entry in reader._all_entries] == ["sess-1", "m1", "m2"]
    assert reader._leaf == "m2"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "message", "id": "m3", "parentId": "m1",
                                 "timestamp": "2026-09-29T10:03:00.000Z",
                                 "message": {"role": "user", "content": "second",
                                             "timestamp": 3}}) + "\n")
    switched = reader.poll()
    assert switched.reset is True
    assert switched.generation != first.generation
    texts = [event.text for event in switched.events]
    assert not any("abandoned" in (text or "") for text in texts)
    assert "second" in texts
    assert list(switched.events) == list(load_activity(session).events)


def test_cursor_head_len_stays_frozen_for_small_and_large_files(tmp_path):
    small = tmp_path / "small.jsonl"
    small.write_text('{"type":"system"}\n', encoding="utf-8")
    reader = open_activity_reader({"source": "claude", "path": str(small), "id": "s"})
    cursor = json.loads(reader.poll().cursor)
    assert cursor["head_len"] == small.stat().st_size

    large = tmp_path / "large.jsonl"
    _write_lines(large, [
        {"type": "system", "timestamp": "2026-09-01T00:00:00Z",
         "uuid": f"u-{index:05d}", "note": "x" * 80} for index in range(200)
    ])
    assert large.stat().st_size > 4096
    reader = open_activity_reader({"source": "claude", "path": str(large), "id": "l"})
    cursor = json.loads(reader.poll().cursor)
    assert cursor["head_len"] == 4096

    resumed = open_activity_reader({"source": "claude", "path": str(large), "id": "l"},
                                   cursor=json.dumps(cursor))
    poll = resumed.poll()
    assert poll.events == ()
    assert poll.reset is False
    assert resumed.lines_parsed == 0  # unchanged fingerprint needs no parse
