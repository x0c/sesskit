"""Stage G: session relations, model/usage, compaction, injected context.

Synthetic fixtures pin each new normalization; bounded real-history checks
assert evidence-backed coverage (counts only, never content).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from sesskit.activity import load_activity, to_v1_dicts
from sesskit.models import (
    ActivityEvent,
    CompactionEvent,
    Evidence,
    SessionRelation,
    Usage,
    as_typed,
)
from sesskit.relations import session_relations
from sesskit.transcript import load_events
from sesskit.turns import with_turn_ids


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(item) + "\n" for item in lines))


# --- models ---------------------------------------------------------------


def test_relation_validation():
    SessionRelation("subagent", Evidence("native"))
    SessionRelation("fork", Evidence("native"), target="ses_1")
    with pytest.raises(ValueError):
        SessionRelation("friend", Evidence("native"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SessionRelation("fork", Evidence("native"), target="  ")


def test_usage_defaults_to_unknown():
    usage = Usage(Evidence("native"), model="m")
    assert usage.input_tokens is None
    assert usage.cost is None


def test_event_origin_validation():
    ActivityEvent(1, "user_message", Evidence("native"), text="hi", origin="human")
    with pytest.raises(ValueError):
        ActivityEvent(1, "user_message", Evidence("native"), text="hi",
                      origin="robot")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ActivityEvent(1, "telegram", Evidence("native"))  # type: ignore[arg-type]


def test_as_typed_compaction_and_origin():
    event = ActivityEvent(3, "compaction", Evidence("native"), text="summary")
    typed = as_typed(event)
    assert isinstance(typed, CompactionEvent)
    assert typed.summary == "summary"
    assert typed.source_seq == 3
    user = ActivityEvent(1, "user_message", Evidence("native"),
                         text="hi", origin="injected")
    assert as_typed(user).origin == "injected"


def test_with_turn_ids_preserves_stage_g_fields():
    from sesskit.models import ActivitySnapshot, SessionOutcome

    usage = Usage(Evidence("native"), model="m", input_tokens=3)
    snapshot = ActivitySnapshot(
        "available",
        (ActivityEvent(1, "user_message", Evidence("native"),
                       text="hi", origin="human"),
         ActivityEvent(2, "assistant_message", Evidence("native"),
                       text="yo", usage=usage)),
        SessionOutcome("done", Evidence("inferred")),
    )
    stamped = with_turn_ids(snapshot)
    assert stamped.events[0].origin == "human"
    assert stamped.events[1].usage is usage


# --- claude (synthetic) ----------------------------------------------------


def _claude_session(tmp_path, rows):
    path = str(tmp_path / "s.jsonl")
    _write(path, rows)
    return {"source": "claude", "id": "s", "path": path}


def test_claude_compact_summary_is_system_with_marker(tmp_path):
    session = _claude_session(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "do it"},
         "timestamp": "2026-09-29T00:00:00Z"},
        {"type": "user", "isCompactSummary": True,
         "message": {"role": "user",
                     "content": "This session is being continued from a previous conversation."},
         "timestamp": "2026-09-29T00:01:00Z"},
    ])
    events = load_activity(session).events
    assert [e.type for e in events] == ["user_message", "user_message"]
    assert events[0].origin == "unknown"
    assert events[1].origin == "system"
    assert events[1].compaction is not None
    assert events[1].compaction.evidence.origin == "native"


def test_claude_queued_human_vs_wrapper(tmp_path):
    session = _claude_session(tmp_path, [
        {"type": "attachment",
         "attachment": {"type": "queued_command", "prompt": "keep going",
                        "origin": {"kind": "human"}}},
        {"type": "user",
         "message": {"role": "user", "content": "<command-name>/cost</command-name>"}},
    ])
    events = load_activity(session).events
    assert len(events) == 2
    assert events[0].origin == "human"
    assert events[1].origin == "injected"


def test_claude_assistant_usage(tmp_path):
    session = _claude_session(tmp_path, [
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-x",
                     "usage": {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_input_tokens": 100,
                               "cache_creation_input_tokens": 50},
                     "content": [{"type": "text", "text": "done"}]}},
    ])
    events = load_activity(session).events
    assert events[0].usage is not None
    assert events[0].usage.model == "claude-x"
    assert events[0].usage.input_tokens == 10
    assert events[0].usage.cache_read_tokens == 100
    assert events[0].usage.cache_write_tokens == 50


# --- codex (synthetic) ------------------------------------------------------


def _codex_session(tmp_path, rows):
    path = str(tmp_path / "rollout.jsonl")
    _write(path, rows)
    return {"source": "codex", "id": "t", "path": path}


def test_codex_compaction_usage_and_injected(tmp_path):
    session = _codex_session(tmp_path, [
        {"type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "# AGENTS.md instructions\nbe good"}]}},
        {"type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "ok"}],
                     "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"}}},
        {"type": "token_usage_record",
         "payload": {"turn_id": "turn-1",
                     "turn_token_usage": {"input_tokens": 7, "output_tokens": 3,
                                          "cached_input_tokens": 1,
                                          "total_tokens": 10}}},
        {"type": "compacted",
         "payload": {"message": "",
                     "replacement_history": [{"type": "message"}]}},
    ])
    snapshot = load_activity(session)
    assert snapshot.state == "available"
    kinds = [e.type for e in snapshot.events]
    assert kinds == ["user_message", "assistant_message", "compaction"]
    assert snapshot.events[0].origin == "injected"
    assert snapshot.events[1].origin == "human"
    assert snapshot.events[1].usage is not None
    assert snapshot.events[1].usage.input_tokens == 7
    assert snapshot.events[1].usage.cache_read_tokens == 1
    assert snapshot.events[2].compaction is not None
    # v1 projection skips the two typed-only rows and renumbers densely.
    assert to_v1_dicts(snapshot) == load_events(session)
    assert [item["seq"] for item in load_events(session)] == [1]


def test_codex_subagent_relation(tmp_path):
    session = _codex_session(tmp_path, [
        {"type": "session_meta",
         "payload": {"thread_source": "subagent", "session_id": "t"}},
    ])
    relations = session_relations(session)
    assert [(r.kind, r.target) for r in relations] == [("subagent", None)]


# --- pi (synthetic) ----------------------------------------------------------


def _pi_session(tmp_path, entries):
    path = str(tmp_path / "s.jsonl")
    _write(path, entries)
    return {"source": "pi", "id": "s", "path": path}


def test_pi_compaction_usage_and_subagent(tmp_path):
    session = _pi_session(tmp_path, [
        {"type": "message", "id": "m1",
         "message": {"role": "user", "content": "hi"}},
        {"type": "message", "id": "m2", "parentId": "m1",
         "message": {"role": "assistant", "model": "pi-model",
                     "usage": {"input": 4, "output": 2, "totalTokens": 6},
                     "content": [{"type": "text", "text": "yo"}],
                     "stopReason": "stop"}},
        {"type": "compaction", "id": "c1", "parentId": "m2",
         "timestamp": "2026-09-27T05:21:23.333Z",
         "summary": "goal recap", "tokensBefore": 99},
        {"type": "custom", "id": "x1", "parentId": "m2",
         "customType": "subagents:record",
         "data": {"id": "child-1", "status": "completed"}},
        {"type": "message", "id": "m3", "parentId": "c1",
         "message": {"role": "user", "content": "again"}},
    ])
    snapshot = load_activity(session)
    kinds = [e.type for e in snapshot.events]
    assert kinds == ["user_message", "assistant_message", "compaction", "user_message"]
    assert snapshot.events[0].origin == "human"
    assert snapshot.events[1].usage is not None
    assert snapshot.events[1].usage.model == "pi-model"
    assert snapshot.events[1].usage.total_tokens == 6
    assert snapshot.events[2].text == "goal recap"
    assert snapshot.events[2].compaction is not None
    assert to_v1_dicts(snapshot) == load_events(session)
    relations = session_relations(session)
    assert [(r.kind, r.target) for r in relations] == [("subagent", "child-1")]


# --- opencode (synthetic sqlite) ----------------------------------------------


def _opencode_db(tmp_path):
    path = str(tmp_path / "opencode.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE session_v2 (id TEXT, parent_id TEXT, fork_session_id TEXT)")
    conn.execute("CREATE TABLE session_message (type TEXT, seq INTEGER, time_created INTEGER, id TEXT, session_id TEXT, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, session_id TEXT, data TEXT)")
    conn.execute(
        "INSERT INTO session_v2 VALUES ('child', 'parent-1', 'fork-9')")
    conn.execute(
        "INSERT INTO session_message VALUES ('user', 0, 1000, 'm1', 'child', ?)",
        (json.dumps({"text": "hi", "time": {"created": 1000}}),))
    conn.execute(
        "INSERT INTO session_message VALUES ('assistant', 1, 2000, 'm2', 'child', ?)",
        (json.dumps({"agent": "build", "model": {"id": "oc-model"},
                     "tokens": {"input": 5, "output": 6,
                                "cache": {"read": 7, "write": 8}},
                     "cost": 0.01, "finish": "stop",
                     "content": [{"type": "text", "text": "yo"}],
                     "time": {"created": 2000}}),))
    conn.execute(
        "INSERT INTO session_message VALUES ('compaction', 2, 3000, 'm3', 'child', ?)",
        (json.dumps({"summary": "recap", "time": {"created": 3000}}),))
    conn.execute(
        "INSERT INTO part VALUES ('p1', 'child', ?)",
        (json.dumps({"type": "subtask", "agent": "build",
                     "description": "review"}),))
    conn.commit()
    conn.close()
    return path


def test_opencode_relations_usage_compaction(tmp_path):
    path = _opencode_db(tmp_path)
    relations = session_relations(
        {"source": "opencode", "id": "child", "path": path})
    by_kind = {r.kind: r.target for r in relations}
    assert by_kind["fork"] == "fork-9"
    assert by_kind["unknown"] == "parent-1"
    assert any(r.kind == "subagent" for r in relations)
    snapshot = load_activity({"source": "opencode", "id": "child", "path": path})
    assert snapshot.state == "available"
    assistant = next(e for e in snapshot.events if e.type == "assistant_message")
    assert assistant.usage is not None
    assert assistant.usage.model == "oc-model"
    assert assistant.usage.input_tokens == 5
    assert assistant.usage.cache_read_tokens == 7
    assert assistant.usage.cost == 0.01
    users = [e for e in snapshot.events if e.type == "user_message"]
    assert users and all(e.origin == "human" for e in users)
    thinking = [e for e in snapshot.events if e.type == "thinking"]
    assert len(thinking) == 1 and thinking[0].compaction is not None
    assert to_v1_dicts(snapshot) == load_events(
        {"source": "opencode", "id": "child", "path": path})


# --- cursor (synthetic store.db) ------------------------------------------------


def _cursor_store(tmp_path, blobs):
    chat_dir = tmp_path / "ws" / "chat"
    chat_dir.mkdir(parents=True)
    db_path = str(chat_dir / "store.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
    for index, blob in enumerate(blobs):
        conn.execute("INSERT INTO blobs VALUES (?, ?)",
                     (str(index), json.dumps(blob).encode()))
    conn.commit()
    conn.close()
    (chat_dir / "meta.json").write_text(json.dumps({"title": "t"}))
    return str(chat_dir / "store.db")


def test_cursor_injected_vs_human(tmp_path):
    store = _cursor_store(tmp_path, [
        {"role": "user", "content": [{"type": "text",
                                      "text": "<user_query>real question</user_query>"}]},
        {"role": "user", "content": [{"type": "text",
                                      "text": "<user_info>rules dump</user_info>"}]},
    ])
    snapshot = load_activity({"source": "cursor", "id": "c", "path": store})
    assert snapshot.state == "available"
    assert [e.origin for e in snapshot.events] == ["human", "injected"]
    assert to_v1_dicts(snapshot) == load_events(
        {"source": "cursor", "id": "c", "path": store})
    assert [item["type"] for item in load_events(
        {"source": "cursor", "id": "c", "path": store})] == ["user_message"]


def test_relations_missing_history_is_empty(tmp_path):
    assert session_relations(
        {"source": "pi", "id": "x", "path": str(tmp_path / "nope.jsonl")}) == []
    assert session_relations(
        {"source": "opencode", "id": "x", "path": str(tmp_path / "nope.db")}) == []
    assert session_relations({"source": "kimi", "id": "x", "path": "y"}) == []


# --- real history (counts only) --------------------------------------------------


def _scan(runtime, limit, **kwargs):
    from sesskit.parsers import claude, codex, cursor, opencode, pi

    modules = {"claude": claude, "codex": codex, "cursor": cursor,
               "opencode": opencode, "pi": pi}
    return modules[runtime].scan_sessions(limit=limit, **kwargs)


@pytest.mark.parametrize("runtime,kwargs,min_ratio", [
    ("claude", {"include_missing_cwd": True}, 0.9),
    ("codex", {"include_missing_cwd": True}, 0.0),
    ("pi", {}, 0.9),
])
def test_real_history_parity_and_usage(runtime, kwargs, min_ratio):
    sessions = _scan(runtime, 60, **kwargs)
    if not sessions:
        pytest.skip(f"no local {runtime} sessions")
    checked = 0
    with_usage = 0
    assistant_total = 0
    for item in sessions:
        session = dict(item)
        snapshot = load_activity(session)
        if snapshot.state != "available":
            continue
        assert to_v1_dicts(snapshot) == load_events(session)
        checked += 1
        for event in snapshot.events:
            if event.type == "assistant_message":
                assistant_total += 1
                if event.usage is not None:
                    with_usage += 1
                    assert event.usage.evidence.origin == "native"
    assert checked > 0
    # Codex usage is per-turn and only some turns carry a native record;
    # per-message runtimes must cover nearly every assistant message.
    assert with_usage / max(assistant_total, 1) > min_ratio
    if min_ratio == 0.0:
        assert with_usage >= 1


def test_real_history_compaction_and_relations():
    from sesskit.parsers import claude, codex, pi

    compact_events = 0
    for item in claude.scan_sessions(limit=60, include_missing_cwd=True):
        for event in load_activity(dict(item)).events:
            if event.compaction is not None:
                compact_events += 1
    assert compact_events >= 1
    compact_rows = 0
    usage_turns = 0
    for item in codex.scan_sessions(limit=120, include_missing_cwd=True):
        for event in load_activity(dict(item)).events:
            if event.type == "compaction":
                compact_rows += 1
            if event.usage is not None:
                usage_turns += 1
    assert compact_rows >= 1
    assert usage_turns >= 1
    subagents = 0
    for item in pi.scan_sessions(limit=188):
        subagents += sum(
            1 for relation in session_relations(dict(item))
            if relation.kind == "subagent")
    assert subagents >= 1
    origins = set()
    for item in codex.scan_sessions(limit=20, include_missing_cwd=True):
        for event in load_activity(dict(item)).events:
            if event.type == "user_message":
                origins.add(event.origin)
    assert "human" in origins and "injected" in origins


def test_real_history_opencode_relations_and_usage():
    from sesskit.parsers import opencode

    sessions = opencode.scan_sessions(limit=100)
    if not sessions:
        pytest.skip("no local opencode sessions")
    checked = 0
    with_usage = 0
    assistant_total = 0
    for item in sessions[:40]:
        session = dict(item)
        snapshot = load_activity(session)
        if snapshot.state != "available":
            continue
        assert to_v1_dicts(snapshot) == load_events(session)
        checked += 1
        for event in snapshot.events:
            if event.type == "assistant_message":
                assistant_total += 1
                if event.usage is not None:
                    with_usage += 1
    assert checked > 0
    assert with_usage / max(assistant_total, 1) > 0.5
