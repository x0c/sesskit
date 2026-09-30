"""Incremental Pi activity reader: protocol, fast path, resets, paging."""

from __future__ import annotations

import json

import pytest

from sesskit import load_activity, to_v1_dicts
from sesskit.activity_reader import (
    IncrementalUnsupported,
    PiActivityReader,
    open_activity_reader,
    supports_incremental,
)
from sesskit.transcript import load_events


def _header(session_id="sess-1", cwd="/tmp/work"):
    return {"type": "session", "version": 3, "id": session_id,
            "timestamp": "2026-09-29T10:00:00.000Z", "cwd": cwd}


def _user(entry_id, parent_id, text, ts="2026-09-29T10:01:00.000Z"):
    return {"type": "message", "id": entry_id, "parentId": parent_id,
            "timestamp": ts,
            "message": {"role": "user", "content": text, "timestamp": 1000}}


def _assistant(entry_id, parent_id, text, ts="2026-09-29T10:02:00.000Z"):
    return {"type": "message", "id": entry_id, "parentId": parent_id,
            "timestamp": ts,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}],
                        "stopReason": "stop", "timestamp": 2000}}


def _assistant_call(entry_id, parent_id, call_id, name="bash", args=None):
    return {"type": "message", "id": entry_id, "parentId": parent_id,
            "timestamp": "2026-09-29T10:03:00.000Z",
            "message": {"role": "assistant",
                        "content": [{"type": "toolCall", "id": call_id,
                                     "name": name, "arguments": args or {"cmd": "ls"}}],
                        "stopReason": "stop", "timestamp": 3000}}


def _tool_result(entry_id, parent_id, call_id, output="ok-output", is_error=False):
    return {"type": "message", "id": entry_id, "parentId": parent_id,
            "timestamp": "2026-09-29T10:04:00.000Z",
            "message": {"role": "toolResult", "toolCallId": call_id,
                        "toolName": "bash", "content": output,
                        "isError": is_error, "timestamp": 4000}}


def _write(path, entries):
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)


def _append(path, entries):
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)


def _session(path, session_id="sess-1"):
    return {"source": "pi", "path": str(path), "id": session_id}


def _by_message_id(events):
    return {event.message_id: event.seq for event in events}


def test_cold_open_matches_snapshot(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello"),
                  _assistant("m2", "m1", "hi there"),
                  _assistant_call("m3", "m2", "call-1"),
                  _tool_result("m4", "m3", "call-1")])
    session = _session(path)
    reader = open_activity_reader(session)
    assert isinstance(reader, PiActivityReader)
    result = reader.poll()
    assert result.reset is True
    assert result.generation == "1"
    assert result.state == "available"
    snapshot = load_activity(session)
    assert [e.seq for e in result.events] == [e.seq for e in snapshot.events]
    assert [(e.type, e.text, e.message_id) for e in result.events] == [
        (e.type, e.text, e.message_id) for e in snapshot.events]
    assert to_v1_dicts(snapshot) == load_events(session)
    assert result.outcome == snapshot.outcome
    assert isinstance(result.cursor, str)
    json.dumps(result.cursor)  # JSON-safe for host persistence
    assert json.loads(result.cursor)["v"] == 1


def test_no_change_poll_reads_nothing(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello"), _assistant("m2", "m1", "hi")])
    reader = open_activity_reader(_session(path))
    first = reader.poll()
    lines, raw = reader.lines_parsed, reader.bytes_read
    second = reader.poll()
    assert second.events == ()
    assert second.reset is False
    assert second.generation == first.generation
    assert second.cursor == first.cursor
    assert reader.lines_parsed == lines
    assert reader.bytes_read == raw


def test_append_poll_is_incremental_with_stable_seq(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello"), _assistant("m2", "m1", "hi")])
    session = _session(path)
    reader = open_activity_reader(session)
    first = reader.poll()
    old_map = _by_message_id(first.events)
    before_lines = reader.lines_parsed
    _append(path, [_user("m3", "m2", "again"), _assistant("m4", "m3", "done")])
    result = reader.poll()
    assert result.reset is False
    assert result.generation == first.generation
    assert [e.seq for e in result.events] == [len(first.events) + 1, len(first.events) + 2]
    assert reader.lines_parsed - before_lines == 2  # no full re-parse
    second = reader.poll()
    assert second.events == ()
    # Old messages keep their seq across polls.
    combined = {e.message_id: e.seq for e in first.events + result.events}
    for message_id, seq in old_map.items():
        assert combined[message_id] == seq
    assert result.outcome == load_activity(session).outcome


def test_partial_trailing_line_held_back(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello")])
    reader = open_activity_reader(_session(path))
    reader.poll()
    cursor_before = reader.poll().cursor
    with open(path, "ab") as handle:
        handle.write(b'{"type": "message", "id": "m2", "pare')
    result = reader.poll()
    assert result.events == ()
    assert result.reset is False
    assert result.cursor == cursor_before
    with open(path, "ab") as handle:
        handle.write(b'ntId": "m1"}\n')
    # Malformed remainder is skipped like the snapshot path; complete the line.
    result = reader.poll()
    assert result.reset is False  # chain continued, no reset


def test_partial_line_completed_on_next_poll(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello")])
    reader = open_activity_reader(_session(path))
    reader.poll()
    entry = _assistant("m2", "m1", "finished")
    raw = json.dumps(entry).encode()
    with open(path, "ab") as handle:
        handle.write(raw[:10])
    assert reader.poll().events == ()
    with open(path, "ab") as handle:
        handle.write(raw[10:] + b"\n")
    result = reader.poll()
    assert [e.text for e in result.events] == ["finished"]
    assert result.reset is False


def test_branch_switch_resets_with_new_generation(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "first"),
                  _assistant("m2", "m1", "abandoned reply")])
    session = _session(path)
    reader = open_activity_reader(session)
    first = reader.poll()
    assert any("abandoned" in (e.text or "") for e in first.events)
    _append(path, [
        {"type": "branch_summary", "id": "bs1", "parentId": "m1",
         "timestamp": "2026-09-29T10:05:00.000Z", "fromId": "m2",
         "summary": "tried approach A"},
        _user("m3", "bs1", "second attempt"),
    ])
    result = reader.poll()
    assert result.reset is True
    assert result.generation != first.generation
    texts = [e.text for e in result.events]
    assert not any("abandoned" in (t or "") for t in texts)
    assert any("second attempt" in (t or "") for t in texts)
    assert result.outcome == load_activity(session).outcome


def test_clock_regression_uses_tree_links(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(),
                  _user("m1", None, "hello", ts="2026-09-29T10:10:00.000Z"),
                  _assistant("m2", "m1", "late but linked",
                             ts="2026-09-29T09:00:00.000Z")])
    session = _session(path)
    result = open_activity_reader(session).poll()
    assert [e.text for e in result.events] == ["hello", "late but linked"]
    assert result.outcome == load_activity(session).outcome


def test_cursor_persistence_round_trip(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello"), _assistant("m2", "m1", "hi")])
    session = _session(path)
    first = open_activity_reader(session).poll()
    restored = open_activity_reader(session, cursor=first.cursor)
    result = restored.poll()
    assert result.events == ()
    assert result.reset is False
    assert result.generation == first.generation
    assert result.cursor == first.cursor


def test_corrupt_and_old_cursor_fall_back(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello")])
    session = _session(path)
    full = load_activity(session)
    for bad in ("not-json{{{", _encode_old_cursor(), _encode_foreign_cursor(str(path))):
        reader = open_activity_reader(session, cursor=bad)
        result = reader.poll()
        assert result.reset is True
        assert result.generation == "1"
        assert [e.seq for e in result.events] == [e.seq for e in full.events]


def _encode_old_cursor():
    return json.dumps({"v": 0, "runtime": "pi"})


def _encode_foreign_cursor(path):
    return json.dumps({"v": 1, "runtime": "pi", "path": path + "-other",
                       "session": "sess-1"})


def test_paging_across_tool_boundary(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "run it"),
                  _assistant_call("m2", "m1", "call-9"),
                  _tool_result("m3", "m2", "call-9", output="some output"),
                  _assistant("m4", "m3", "wrapped up")])
    reader = open_activity_reader(_session(path))
    full = reader.poll().events
    assert any(e.type == "tool_call" for e in full)
    assert any(e.type == "tool_result" for e in full)
    newest = reader.page(limit=2)
    assert [e.seq for e in newest.events] == [len(full) - 1, len(full)]
    assert newest.has_more is True
    assert isinstance(newest.before, str)
    older = reader.page(before=newest.before, limit=2)
    assert [e.seq for e in older.events] == [1, 2]
    assert older.has_more is False
    assert older.before is None
    kinds = [e.type for e in newest.events + older.events]
    assert "tool_call" in kinds and "tool_result" in kinds
    # Paging does not disturb the poll stream.
    assert reader.poll().events == ()


def _compaction(entry_id, parent_id, summary="older context summary"):
    return {"type": "compaction", "id": entry_id, "parentId": parent_id,
            "timestamp": "2026-09-29T10:01:30.000Z", "summary": summary,
            "usage": {"input": 100, "output": 20, "totalTokens": 120}}


def test_cold_open_emits_compaction_like_snapshot(tmp_path):
    # The snapshot loader emits a standalone compaction event for the entry
    # feeding into the active branch; the reader cold open must emit the
    # same event or every later seq desyncs (live gate: 4 sessions).
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello"),
                  _assistant("m2", "m1", "hi"),
                  _compaction("c1", "m2"),
                  _user("m3", "c1", "continue"),
                  _assistant("m4", "m3", "done")])
    session = _session(path)
    reader = open_activity_reader(session)
    result = reader.poll()
    assert result.reset is True
    snapshot = load_activity(session)
    assert [e.type for e in snapshot.events].count("compaction") == 1
    assert [(e.seq, e.type) for e in result.events] == [
        (e.seq, e.type) for e in snapshot.events]
    assert [(e.type, e.text, e.message_id) for e in result.events] == [
        (e.type, e.text, e.message_id) for e in snapshot.events]


def test_appended_compaction_stays_in_sync(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "hello"),
                  _assistant("m2", "m1", "hi")])
    session = _session(path)
    reader = open_activity_reader(session)
    first = reader.poll()
    _append(path, [_compaction("c1", "m2"),
                   _user("m3", "c1", "continue"),
                   _assistant("m4", "m3", "done")])
    result = reader.poll()
    assert result.reset is False
    snapshot = load_activity(session)
    combined = list(first.events) + list(result.events)
    assert [(e.seq, e.type) for e in combined] == [
        (e.seq, e.type) for e in snapshot.events]
    assert any(e.type == "compaction" for e in result.events)


def test_unsupported_runtime_falls_back_to_snapshot():
    assert supports_incremental({"source": "pi"}) is True
    assert supports_incremental({"source": "claude"}) is True
    assert supports_incremental({"source": "codex"}) is True
    assert supports_incremental({"source": "cursor"}) is True
    assert supports_incremental({"source": "opencode"}) is True
    assert supports_incremental({"source": "kimi"}) is False
    with pytest.raises(IncrementalUnsupported):
        open_activity_reader({"source": "kimi", "path": "/none", "id": "x"})


def test_unavailable_history(tmp_path):
    path = tmp_path / "missing.jsonl"
    reader = open_activity_reader(_session(path))
    result = reader.poll()
    assert result.state == "unavailable"
    assert result.events == ()
    assert result.cursor is None
    _write(path, [_header(), _user("m1", None, "hello")])
    back = reader.poll()
    assert back.state == "available"
    assert back.reset is True
    assert [e.text for e in back.events] == ["hello"]


def test_empty_history(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    reader = open_activity_reader(_session(path))
    result = reader.poll()
    assert result.state == "empty"
    assert result.events == ()
    again = reader.poll()
    assert again.events == ()
    assert again.reset is False


def test_truncation_resets(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_header(), _user("m1", None, "one"),
                  _assistant("m2", "m1", "two"),
                  _assistant("m3", "m2", "three")])
    reader = open_activity_reader(_session(path))
    first = reader.poll()
    assert len(first.events) == 3
    _write(path, [_header(), _user("m1", None, "restarted")])
    result = reader.poll()
    assert result.reset is True
    assert result.generation != first.generation
    assert [e.text for e in result.events] == ["restarted"]
