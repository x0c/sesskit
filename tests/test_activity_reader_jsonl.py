"""Incremental JSONL activity readers: Claude and Codex.

Covers the W1 contract: opaque versioned JSON-safe cursors with content
fingerprints, committed byte offsets plus interpreter aux state, tail-window
cold opens with snapshot-global seqs, stable seq within a generation,
backward paging with pairing across page edges, cursor persistence across
processes, reset-on-divergence (never silent continuation), snapshot
parity on the same bytes, and append polls without full re-parse.
"""

from __future__ import annotations

import json

import pytest

from sesskit import load_activity, to_v1_dicts
from sesskit.activity_reader import open_activity_reader, supports_incremental
from sesskit.activity_reader_jsonl import (
    ClaudeActivityReader,
    CodexActivityReader,
)
from sesskit.models import ActivitySnapshot
from sesskit.transcript import load_events
from sesskit.turns import derive_turns

# --- fixture builders ----------------------------------------------------


def _write(path, entries):
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)


def _append(path, entries):
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)


def _claude_user(text, ts="2026-09-01T00:00:01Z"):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _claude_assistant(text, ts="2026-09-01T00:00:02Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]},
            "uuid": f"u-{text[:8]}"}


def _claude_tool_use(call_id, name="Bash", args=None, ts="2026-09-01T00:00:03Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": call_id,
                                     "name": name, "input": args or {"cmd": "ls"}}]}}


def _claude_tool_result(call_id, output="ok-output", is_error=False,
                        ts="2026-09-01T00:00:04Z"):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": call_id,
                                     "content": output, "is_error": is_error}]}}


def _claude_system_error(text="429 rate limited", status=429):
    return {"type": "system", "timestamp": "2026-09-01T00:00:05Z",
            "error": {"formatted": text, "status": status}}


def _codex_user(text, ts=1000.0):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]},
            "id": f"i-{ts}"}


def _codex_assistant(text, ts=2000.0, turn_id=None):
    payload: dict = {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": text}]}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return {"type": "response_item", "timestamp": ts, "payload": payload,
            "id": f"i-{ts}"}


def _codex_call(call_id, name="shell", ts=3000.0, turn_id=None):
    payload: dict = {"type": "function_call", "call_id": call_id, "name": name,
                     "arguments": json.dumps({"cmd": "true"})}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return {"type": "response_item", "timestamp": ts, "payload": payload,
            "id": f"i-{ts}-{call_id}"}


def _codex_output(call_id, output="ok", ts=4000.0):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "function_call_output", "call_id": call_id,
                        "output": output},
            "id": f"i-{ts}-{call_id}"}


def _codex_task_complete(text="all done", ts=5000.0):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "task_complete", "last_agent_message": text}}


def _reader_snapshot(reader) -> ActivitySnapshot:
    poll = reader.poll()
    return ActivitySnapshot(
        state=poll.state if poll.state in {"available", "empty"} else "empty",
        events=tuple(reader._events),
        outcome=poll.outcome,
    )


def _semantic(event):
    return (event.type, event.text, event.name, event.call_id, event.origin)


# --- dispatch ------------------------------------------------------------


def test_dispatch_returns_jsonl_readers(tmp_path):
    claude = tmp_path / "c.jsonl"
    codex = tmp_path / "r.jsonl"
    claude.write_text("", encoding="utf-8")
    codex.write_text("", encoding="utf-8")
    assert isinstance(
        open_activity_reader({"source": "claude", "path": str(claude), "id": "c"}),
        ClaudeActivityReader)
    assert isinstance(
        open_activity_reader({"source": "codex", "path": str(codex), "id": "r"}),
        CodexActivityReader)
    assert supports_incremental({"source": "claude"}) is True
    assert supports_incremental({"source": "codex"}) is True


# --- Claude: snapshot parity ---------------------------------------------


def _claude_basic_rows():
    return [
        _claude_user("Run checks."),
        _claude_assistant("Running."),
        _claude_tool_use("call-1"),
        _claude_tool_result("call-1", "all green"),
        _claude_assistant("Done."),
    ]


def test_claude_cold_open_matches_snapshot(tmp_path):
    path = tmp_path / "c.jsonl"
    _write(path, _claude_basic_rows())
    session = {"source": "claude", "path": str(path), "id": "c-1"}
    reader = open_activity_reader(session)
    result = reader.poll()
    assert result.reset is True
    assert result.generation == "1"
    assert result.state == "available"
    assert reader.full_coverage is True
    snapshot = load_activity(session)
    assert list(result.events) == list(snapshot.events)
    assert result.outcome == snapshot.outcome
    assert to_v1_dicts(_reader_snapshot(reader)) == load_events(session)
    cursor = json.loads(result.cursor)
    assert cursor["v"] == 1 and cursor["runtime"] == "claude"
    assert {"head", "head_len", "boundary", "size", "lines", "aux", "events"} <= set(cursor)


def test_claude_outcome_and_turns_equal_snapshot(tmp_path):
    path = tmp_path / "c.jsonl"
    _write(path, _claude_basic_rows())
    session = {"source": "claude", "path": str(path), "id": "c-1"}
    reader = open_activity_reader(session)
    poll = reader.poll()
    snapshot = load_activity(session)
    assert poll.outcome == snapshot.outcome
    reader_view = derive_turns(_reader_snapshot(reader))
    snapshot_view = derive_turns(snapshot)
    assert [t.outcome for t in reader_view] == [t.outcome for t in snapshot_view]


def test_claude_error_tail_matches_snapshot(tmp_path):
    path = tmp_path / "c.jsonl"
    _write(path, [_claude_user("hi"), _claude_system_error("401 bad key", 401)])
    session = {"source": "claude", "path": str(path), "id": "c-1"}
    reader = open_activity_reader(session)
    result = reader.poll()
    snapshot = load_activity(session)
    assert list(result.events) == list(snapshot.events)
    assert result.outcome == snapshot.outcome
    assert result.outcome.status == "aborted"


# --- Codex: snapshot parity ----------------------------------------------


def _codex_basic_rows():
    return [
        _codex_user("Run checks."),
        _codex_assistant("Running.", turn_id="turn-1"),
        _codex_call("call-1", turn_id="turn-1"),
        _codex_output("call-1", "ok"),
        _codex_task_complete("Done."),
    ]


def test_codex_cold_open_matches_snapshot(tmp_path):
    path = tmp_path / "r.jsonl"
    _write(path, _codex_basic_rows())
    session = {"source": "codex", "path": str(path), "id": "r-1"}
    reader = open_activity_reader(session)
    result = reader.poll()
    assert result.reset is True
    assert result.generation == "1"
    snapshot = load_activity(session)
    assert list(result.events) == list(snapshot.events)
    assert result.outcome == snapshot.outcome
    assert result.outcome.status == "done"
    assert to_v1_dicts(_reader_snapshot(reader)) == load_events(session)
    assert [e.turn_id for e in result.events if e.type == "assistant_message"] == \
        [e.turn_id for e in snapshot.events if e.type == "assistant_message"]


def test_codex_outcome_and_turns_equal_snapshot(tmp_path):
    path = tmp_path / "r.jsonl"
    _write(path, [_codex_user("hi"),
                  {"type": "event_msg", "timestamp": 9.0,
                   "payload": {"type": "turn_aborted", "reason": "interrupt"}}])
    session = {"source": "codex", "path": str(path), "id": "r-1"}
    reader = open_activity_reader(session)
    poll = reader.poll()
    snapshot = load_activity(session)
    assert poll.outcome == snapshot.outcome
    assert poll.outcome.status == "aborted"
    assert [t.outcome for t in derive_turns(_reader_snapshot(reader))] == \
        [t.outcome for t in derive_turns(snapshot)]


# --- incremental polls ----------------------------------------------------


@pytest.mark.parametrize("runtime,rows,more_rows", [
    ("claude", None, None),
    ("codex", None, None),
])
def test_append_poll_is_incremental_with_stable_seq(tmp_path, runtime, rows, more_rows):
    ext = "jsonl"
    path = tmp_path / f"s.{ext}"
    if runtime == "claude":
        _write(path, [_claude_user("one"), _claude_assistant("uno")])
        extra = [_claude_user("two"), _claude_assistant("dos")]
    else:
        _write(path, [_codex_user("one"), _codex_assistant("uno")])
        extra = [_codex_user("two"), _codex_assistant("dos")]
    session = {"source": runtime, "path": str(path), "id": "s-1"}
    reader = open_activity_reader(session)
    first = reader.poll()
    before_lines, before_bytes = reader.lines_parsed, reader.bytes_read
    _append(path, extra)
    result = reader.poll()
    assert result.reset is False
    assert result.generation == first.generation
    assert reader.lines_parsed - before_lines == len(extra)  # no full re-parse
    assert reader.bytes_read - before_bytes < 4096
    assert [e.seq for e in result.events] == [len(first.events) + 1, len(first.events) + 2]
    combined = list(first.events) + list(result.events)
    for event in first.events:
        assert combined[event.seq - 1] == event  # old seqs stable
    assert result.outcome == load_activity(session).outcome
    assert list(combined) == list(load_activity(session).events)
    assert reader.poll().events == ()


def test_no_change_poll_reads_nothing(tmp_path):
    path = tmp_path / "c.jsonl"
    _write(path, [_claude_user("hi"), _claude_assistant("yo")])
    reader = open_activity_reader({"source": "claude", "path": str(path), "id": "c"})
    first = reader.poll()
    lines, raw = reader.lines_parsed, reader.bytes_read
    second = reader.poll()
    assert second.events == ()
    assert second.reset is False
    assert second.generation == first.generation
    assert second.cursor == first.cursor
    assert reader.lines_parsed == lines
    assert reader.bytes_read == raw


def test_codex_dedup_state_carries_across_polls(tmp_path):
    path = tmp_path / "r.jsonl"
    _write(path, [_codex_user("hi"), _codex_assistant("same")])
    session = {"source": "codex", "path": str(path), "id": "r"}
    reader = open_activity_reader(session)
    reader.poll()
    _append(path, [_codex_assistant("same", ts=9000.0)])
    result = reader.poll()
    assert result.reset is False
    # Adjacent duplicate suppressed exactly like the snapshot path.
    assert result.events == ()
    assert list(load_activity(session).events) == list(reader._events)


def test_partial_trailing_line_held_back(tmp_path):
    path = tmp_path / "c.jsonl"
    _write(path, [_claude_user("hi")])
    reader = open_activity_reader({"source": "claude", "path": str(path), "id": "c"})
    reader.poll()
    cursor_before = reader.poll().cursor
    with open(path, "ab") as handle:
        handle.write(b'{"type": "assistant", "message": {"role": "ass')
    result = reader.poll()
    assert result.events == ()
    assert result.reset is False
    assert result.cursor == cursor_before
    with open(path, "ab") as handle:
        handle.write(b'istant", "content": [{"type": "text", "text": "done"}]}}\n')
    result = reader.poll()
    assert [e.text for e in result.events] == ["done"]
    assert result.reset is False


# --- divergence resets ----------------------------------------------------


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_truncation_and_replacement_reset(tmp_path, runtime):
    path = tmp_path / "s.jsonl"
    first_rows = ([_claude_user("a"), _claude_assistant("b"), _claude_assistant("c")]
                  if runtime == "claude"
                  else [_codex_user("a"), _codex_assistant("b"), _codex_assistant("c")])
    _write(path, first_rows)
    session = {"source": runtime, "path": str(path), "id": "s"}
    reader = open_activity_reader(session)
    first = reader.poll()
    assert len(first.events) == 3
    second_rows = ([_claude_user("restarted")] if runtime == "claude"
                   else [_codex_user("restarted")])
    _write(path, second_rows)
    result = reader.poll()
    assert result.reset is True
    assert result.generation != first.generation
    assert [e.text for e in result.events] == ["restarted"]
    assert list(result.events) == list(load_activity(session).events)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_inplace_rewrite_detected_by_fingerprint(tmp_path, runtime):
    path = tmp_path / "s.jsonl"
    if runtime == "claude":
        _write(path, [_claude_user("aaa"), _claude_assistant("bbb")])
    else:
        _write(path, [_codex_user("aaa"), _codex_assistant("bbb")])
    session = {"source": runtime, "path": str(path), "id": "s"}
    reader = open_activity_reader(session)
    first = reader.poll()
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        data = bytearray(handle.read())
        # Same size, different middle bytes: flip one body character.
        body = data.decode("utf-8")
        alt = body.replace("aaa", "aab")
        assert len(alt.encode()) == size
        handle.seek(0)
        handle.write(alt.encode())
    result = reader.poll()
    assert result.reset is True
    assert result.generation != first.generation


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_corrupt_and_old_cursor_rebuild(tmp_path, runtime):
    path = tmp_path / "s.jsonl"
    rows = ([_claude_user("hi")] if runtime == "claude" else [_codex_user("hi")])
    _write(path, rows)
    session = {"source": runtime, "path": str(path), "id": "s"}
    full = load_activity(session)
    bad = [
        "not-json{{{",
        json.dumps({"v": 0, "runtime": runtime}),
        json.dumps({"v": 1, "runtime": "pi"}),
        json.dumps({"v": 1, "runtime": runtime, "path": str(path) + "-other",
                    "session": "s"}),
    ]
    for cursor in bad:
        reader = open_activity_reader(session, cursor=cursor)
        result = reader.poll()
        assert result.reset is True
        assert result.generation == "2"
        assert list(result.events) == list(full.events)


# --- cursor persistence ---------------------------------------------------


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_cursor_persistence_across_processes(tmp_path, runtime):
    path = tmp_path / "s.jsonl"
    rows = ([_claude_user("hi"), _claude_assistant("yo")]
            if runtime == "claude" else [_codex_user("hi"), _codex_assistant("yo")])
    _write(path, rows)
    session = {"source": runtime, "path": str(path), "id": "s"}
    first = open_activity_reader(session).poll()
    assert first.reset is True
    restored = open_activity_reader(session, cursor=first.cursor)
    result = restored.poll()
    assert result.events == ()
    assert result.reset is False
    assert result.generation == first.generation
    assert result.cursor == first.cursor
    assert result.outcome == first.outcome


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_restart_after_append_resets_with_snapshot_parity(tmp_path, runtime):
    path = tmp_path / "s.jsonl"
    rows = ([_claude_user("hi")] if runtime == "claude" else [_codex_user("hi")])
    _write(path, rows)
    session = {"source": runtime, "path": str(path), "id": "s"}
    first = open_activity_reader(session).poll()
    more = ([_claude_assistant("yo")] if runtime == "claude"
            else [_codex_assistant("yo")])
    _append(path, more)
    resumed = open_activity_reader(session, cursor=first.cursor)
    result = resumed.poll()
    assert result.reset is True
    assert result.generation != first.generation
    assert list(result.events) == list(load_activity(session).events)


def test_unavailable_history(tmp_path):
    path = tmp_path / "missing.jsonl"
    reader = open_activity_reader({"source": "claude", "path": str(path), "id": "c"})
    result = reader.poll()
    assert result.state == "unavailable"
    assert result.events == ()
    assert result.cursor is None


def test_empty_history(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    reader = open_activity_reader({"source": "codex", "path": str(path), "id": "r"})
    result = reader.poll()
    assert result.state == "empty"
    assert result.events == ()


# --- paging ---------------------------------------------------------------


def test_claude_page_parity_and_pairing(tmp_path):
    path = tmp_path / "c.jsonl"
    _write(path, [_claude_user("run it"),
                  _claude_tool_use("call-9"),
                  _claude_tool_result("call-9", "some output"),
                  _claude_assistant("wrapped up")])
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    full = reader.poll().events
    assert any(e.type == "tool_call" for e in full)
    newest = reader.page(limit=2)
    assert [e.seq for e in newest.events] == [len(full) - 1, len(full)]
    assert newest.has_more is True
    older = reader.page(before=newest.before, limit=2)
    assert [e.seq for e in older.events] == [1, 2]
    assert older.has_more is False
    assert list(older.events) + list(newest.events) == list(full)
    assert list(older.events) + list(newest.events) == list(load_activity(session).events)
    # Call and result sit on different pages but still pair by call_id.
    calls = [e for e in older.events + newest.events if e.type == "tool_call"]
    results = [e for e in older.events + newest.events if e.type == "tool_result"]
    assert calls and results and calls[0].call_id == results[0].call_id == "call-9"


def test_codex_page_parity(tmp_path):
    path = tmp_path / "r.jsonl"
    _write(path, _codex_basic_rows())
    session = {"source": "codex", "path": str(path), "id": "r"}
    reader = open_activity_reader(session)
    full = reader.poll().events
    newest = reader.page(limit=2)
    assert newest.has_more is True
    older = reader.page(before=newest.before, limit=50)
    assert list(older.events) + list(newest.events) == list(full)
    assert reader.poll().events == ()


def test_poll_straddle_call_result_pairs_by_call_id(tmp_path):
    # A call delivered in one poll and its result in the next: the stream
    # keeps the unresolved call (documented), the result carries the call id,
    # and the v1 projection stays byte-compatible.
    path = tmp_path / "c.jsonl"
    _write(path, [_claude_user("run it"), _claude_tool_use("call-7")])
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    first = reader.poll()
    calls = [e for e in first.events if e.type == "tool_call"]
    assert len(calls) == 1 and calls[0].interaction is None
    _append(path, [_claude_tool_result("call-7", "did it"),
                   _claude_assistant("wrapped")])
    second = reader.poll()
    assert second.reset is False
    results = [e for e in second.events if e.type == "tool_result"]
    assert results and results[0].call_id == "call-7"
    combined = list(first.events) + list(second.events)
    projected = to_v1_dicts(ActivitySnapshot("available", tuple(combined), second.outcome))
    assert projected == load_events(session)


# --- tail-window cold open -------------------------------------------------


def _large_claude_rows(count):
    rows = []
    for index in range(count):
        rows.append(_claude_user(f"prompt number {index} filler text " + "x" * 40))
        rows.append(_claude_assistant(f"reply number {index} filler text " + "y" * 40))
    return rows


def test_cold_open_returns_bounded_tail_with_global_seqs(tmp_path):
    from sesskit.activity_reader_jsonl import _TAIL_EVENTS_DEFAULT

    path = tmp_path / "big.jsonl"
    _write(path, _large_claude_rows(3000))  # 6000 events, past the window
    session = {"source": "claude", "path": str(path), "id": "big"}
    reader = open_activity_reader(session, cursor=None)
    assert reader.lines_parsed == 0  # opening alone reads nothing
    result = reader.poll()
    assert result.reset is True
    assert result.generation == "1"
    tail = list(result.events)
    # Bounded response with snapshot-global seqs: exact suffix equality.
    assert len(tail) == _TAIL_EVENTS_DEFAULT < reader.event_total
    snapshot = load_activity(session)
    assert tail == list(snapshot.events[-len(tail):])
    assert result.outcome == snapshot.outcome
    # Backward pages reassemble the full snapshot in one generation.
    assembled: list = []
    before = None
    guard = 0
    while guard < 100:
        guard += 1
        page = reader.page(before=before, limit=200)
        assert page.generation == result.generation
        assembled = list(page.events) + assembled
        if not page.has_more:
            break
        before = page.before
    assert assembled == list(snapshot.events)
    # Appends still parse only new bytes; deltas equal the snapshot suffix.
    parsed_before = reader.lines_parsed
    _append(path, [_claude_user("late question"), _claude_assistant("late answer")])
    delta = reader.poll()
    assert delta.reset is False
    assert reader.lines_parsed - parsed_before == 2  # no full re-parse
    after = load_activity(session)
    assert list(delta.events) == list(after.events[-len(delta.events):])
    assert delta.outcome == after.outcome
    assert [e.seq for e in delta.events] == [len(snapshot.events) + 1,
                                             len(snapshot.events) + 2]


def test_small_file_open_is_exact_and_full_coverage(tmp_path):
    path = tmp_path / "c.jsonl"
    _write(path, _claude_basic_rows())
    session = {"source": "claude", "path": str(path), "id": "c"}
    reader = open_activity_reader(session)
    result = reader.poll()
    assert reader.full_coverage is True
    assert [e.seq for e in result.events] == list(range(1, len(result.events) + 1))


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_malformed_lines_keep_snapshot_records(tmp_path, runtime):
    # Malformed lines are skipped but still consume a line number, so
    # evidence records match the snapshot on the same bytes.
    path = tmp_path / "s.jsonl"
    if runtime == "claude":
        rows = [_claude_user("hi"), _claude_assistant("yo")]
    else:
        rows = [_codex_user("hi"), _codex_assistant("yo")]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(rows[0], ensure_ascii=False) + "\n")
        handle.write("{broken json line\n")
        handle.write(json.dumps(rows[1], ensure_ascii=False) + "\n")
    session = {"source": runtime, "path": str(path), "id": "s"}
    reader = open_activity_reader(session)
    result = reader.poll()
    snapshot = load_activity(session)
    assert list(result.events) == list(snapshot.events)
    assert result.outcome == snapshot.outcome
