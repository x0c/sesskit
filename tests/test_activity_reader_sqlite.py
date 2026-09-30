"""Incremental SQLite activity readers: Cursor store.db and OpenCode sessions.

Covers the W2 protocol: cold open equals the snapshot, append polls return
only new events with stable seq, no-change polls never open the database,
concurrent-append (WAL) visibility, vacuum/rowid-reuse/replacement resets
with a new generation, backward paging that preserves call/result pairing,
and corrupt-cursor fallback. Counts and seqs only, never content or paths.
"""

from __future__ import annotations

import json
import sqlite3

from sesskit import load_activity, to_v1_dicts
from sesskit.activity_reader import (
    open_activity_reader,
    supports_incremental,
)
from sesskit.transcript import load_events

# --- Cursor fixtures --------------------------------------------------------


def _cursor_session(store_path) -> dict:
    return {"source": "cursor", "path": str(store_path), "id": "chat-1"}


def _cursor_write(store_path, objects, *, keep_open=False):
    store_path.parent.mkdir(parents=True, exist_ok=True)
    first = not store_path.exists()
    conn = sqlite3.connect(store_path)
    if first:
        conn.execute("CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, data BLOB)")
    base = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    for index, obj in enumerate(objects):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        conn.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            (f"blob-{base + index}", raw),
        )
    conn.commit()
    if keep_open:
        return conn
    conn.close()
    return None


def _cursor_user(text):
    return {"role": "user", "content": f"<user_query>{text}</user_query>"}


def _cursor_assistant(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _cursor_call(call_id, name="Read", args='{"path":"a"}'):
    return {"role": "assistant",
            "content": [{"type": "tool-call", "toolCallId": call_id,
                         "toolName": name, "args": args}]}


def _cursor_result(call_id, result):
    return {"role": "tool",
            "content": [{"type": "tool-result", "toolCallId": call_id,
                         "toolName": "Read", "result": result}]}


def _seqs(events):
    return [e.seq for e in events]


def _kinds(events):
    return [(e.seq, e.type) for e in events]


# --- OpenCode fixtures ------------------------------------------------------


def _opencode_session(db_path, session_id) -> dict:
    return {"source": "opencode", "path": str(db_path), "id": session_id}


def _v1_db(db_path, session_id, messages):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT OR IGNORE INTO session VALUES (?)", (session_id,))
    for index, (message_id, message, parts) in enumerate(messages):
        created = message.get("time", {}).get("created", (index + 1) * 1000)
        conn.execute(
            "INSERT OR REPLACE INTO message VALUES (?,?,?,?)",
            (message_id, session_id, created, json.dumps(message)),
        )
        for part_index, (part_id, part) in enumerate(parts):
            conn.execute(
                "INSERT OR REPLACE INTO part VALUES (?,?,?,?,?)",
                (part_id, message_id, session_id, created + part_index,
                 json.dumps(part)),
            )
    conn.commit()
    conn.close()


def _v1_append_message(db_path, session_id, message_id, message, parts, created):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO message VALUES (?,?,?,?)",
        (message_id, session_id, created, json.dumps(message)),
    )
    for part_index, (part_id, part) in enumerate(parts):
        conn.execute(
            "INSERT OR REPLACE INTO part VALUES (?,?,?,?,?)",
            (part_id, message_id, session_id, created + part_index,
             json.dumps(part)),
        )
    conn.commit()
    conn.close()


def _v2_db(db_path, session_id, rows):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_v2 (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT OR IGNORE INTO session_v2 VALUES (?)", (session_id,))
    for row_id, seq, kind, data in rows:
        conn.execute(
            "INSERT OR REPLACE INTO session_message VALUES (?,?,?,?,?,?)",
            (row_id, session_id, kind, seq, seq * 1000, json.dumps(data)),
        )
    conn.commit()
    conn.close()


def _v2_append(db_path, session_id, row_id, seq, kind, data, *, keep_open=False):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO session_message VALUES (?,?,?,?,?,?)",
        (row_id, session_id, kind, seq, seq * 1000, json.dumps(data)),
    )
    conn.commit()
    if keep_open:
        return conn
    conn.close()
    return None


# --- Cursor reader ----------------------------------------------------------


def test_reader_supported_for_sqlite_runtimes():
    assert supports_incremental({"source": "cursor"}) is True
    assert supports_incremental({"source": "opencode"}) is True
    assert supports_incremental({"source": "kimi"}) is False


def test_cursor_cold_open_matches_snapshot(tmp_path):
    store = tmp_path / "store.db"
    _cursor_write(store, [
        _cursor_user("first question"),
        _cursor_assistant("first answer"),
        _cursor_call("r1"),
        _cursor_result("r1", "file contents"),
    ])
    session = _cursor_session(store)
    reader = open_activity_reader(session)
    result = reader.poll()
    assert result.reset is True
    assert result.generation == "1"
    assert result.state == "available"
    snapshot = load_activity(session)
    assert _kinds(result.events) == _kinds(snapshot.events)
    assert [(e.type, e.text) for e in result.events] == [
        (e.type, e.text) for e in snapshot.events]
    assert result.outcome == snapshot.outcome
    assert to_v1_dicts(snapshot) == load_events(session)
    assert isinstance(result.cursor, str)
    assert json.loads(result.cursor)["runtime"] == "cursor"


def test_cursor_no_change_poll_opens_nothing(tmp_path):
    store = tmp_path / "store.db"
    _cursor_write(store, [_cursor_user("hello")])
    reader = open_activity_reader(_cursor_session(store))
    first = reader.poll()
    opens, parsed = reader.db_opens, reader.rows_parsed
    assert opens >= 1
    second = reader.poll()
    assert second.events == ()
    assert second.reset is False
    assert second.generation == first.generation
    assert second.cursor == first.cursor
    assert reader.db_opens == opens
    assert reader.rows_parsed == parsed


def test_cursor_append_is_incremental_with_stable_seq(tmp_path):
    store = tmp_path / "store.db"
    _cursor_write(store, [_cursor_user("hello"), _cursor_assistant("hi")])
    session = _cursor_session(store)
    reader = open_activity_reader(session)
    first = reader.poll()
    old_keys = [(e.seq, e.type) for e in first.events]
    opens = reader.db_opens
    _cursor_write(store, [_cursor_call("r9"), _cursor_result("r9", "done output")])
    result = reader.poll()
    assert result.reset is False
    assert result.generation == first.generation
    assert reader.db_opens == opens + 1
    assert _seqs(result.events) == [len(first.events) + 1, len(first.events) + 2]
    combined = list(first.events) + list(result.events)
    assert [(e.seq, e.type) for e in combined][: len(old_keys)] == old_keys
    assert reader.poll().events == ()
    snapshot = load_activity(session)
    assert _kinds(combined) == _kinds(snapshot.events)
    assert result.outcome == snapshot.outcome


def test_cursor_concurrent_append_is_visible(tmp_path):
    store = tmp_path / "store.db"
    conn = sqlite3.connect(store)
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO blobs VALUES (?, ?)",
                 ("b0", json.dumps(_cursor_user("started")).encode()))
    conn.commit()
    session = _cursor_session(store)
    reader = open_activity_reader(session)
    first = reader.poll()
    assert first.state == "available"
    # A second connection appends while the reader is open (writer stays
    # open with uncheckpointed WAL content, like a live agent process).
    writer = sqlite3.connect(store)
    writer.execute("INSERT INTO blobs VALUES (?, ?)",
                   ("b1", json.dumps(_cursor_assistant("live reply")).encode()))
    writer.commit()
    result = reader.poll()
    assert result.reset is False
    assert [e.text for e in result.events] == ["live reply"]
    writer.close()
    conn.close()


def test_cursor_replacement_resets_with_new_generation(tmp_path):
    store = tmp_path / "store.db"
    _cursor_write(store, [_cursor_user("original")])
    session = _cursor_session(store)
    reader = open_activity_reader(session)
    first = reader.poll()
    store.unlink()
    _cursor_write(store, [_cursor_user("replacement")])
    result = reader.poll()
    assert result.reset is True
    assert result.generation != first.generation
    assert [e.text for e in result.events] == ["replacement"]
    assert result.outcome == load_activity(session).outcome


def test_cursor_rowid_reuse_resets(tmp_path):
    store = tmp_path / "store.db"
    _cursor_write(store, [_cursor_user("alpha"), _cursor_assistant("beta")])
    session = _cursor_session(store)
    reader = open_activity_reader(session)
    first = reader.poll()
    conn = sqlite3.connect(store)
    conn.execute("DELETE FROM blobs")
    conn.execute("INSERT INTO blobs VALUES (?, ?)",
                 ("n0", json.dumps(_cursor_user("gamma")).encode()))
    conn.execute("INSERT INTO blobs VALUES (?, ?)",
                 ("n1", json.dumps(_cursor_assistant("delta")).encode()))
    conn.commit()
    conn.close()
    result = reader.poll()
    assert result.reset is True
    assert result.generation != first.generation
    assert [e.text for e in result.events] == ["gamma", "delta"]
    assert _kinds(result.events) == _kinds(load_activity(session).events)


def test_cursor_paging_preserves_pairing(tmp_path):
    store = tmp_path / "store.db"
    _cursor_write(store, [
        _cursor_user("run it"),
        _cursor_call("call-9"),
        _cursor_result("call-9", "some output"),
        _cursor_assistant("wrapped up"),
    ])
    reader = open_activity_reader(_cursor_session(store))
    full = reader.poll().events
    assert any(e.type == "tool_call" for e in full)
    newest = reader.page(limit=2)
    assert _seqs(newest.events) == [len(full) - 1, len(full)]
    assert newest.has_more is True
    assert isinstance(newest.before, str)
    older = reader.page(before=newest.before, limit=2)
    assert _seqs(older.events) == [1, 2]
    assert older.has_more is False
    kinds = [e.type for e in newest.events + older.events]
    assert "tool_call" in kinds and "tool_result" in kinds
    call_ids = {e.call_id for e in older.events + newest.events
                if e.type == "tool_call" and e.call_id}
    for event in older.events + newest.events:
        if event.type == "tool_result" and event.call_id:
            assert event.call_id in call_ids
    assert reader.poll().events == ()


def test_cursor_bad_cursor_falls_back(tmp_path):
    store = tmp_path / "store.db"
    _cursor_write(store, [_cursor_user("hello")])
    session = _cursor_session(store)
    full = load_activity(session)
    for bad in ("not-json{{{",
                json.dumps({"v": 0, "runtime": "cursor"}),
                json.dumps({"v": 1, "runtime": "cursor", "path": "elsewhere",
                            "session": "chat-1"})):
        result = open_activity_reader(session, cursor=bad).poll()
        assert result.reset is True
        assert _kinds(result.events) == _kinds(full.events)


def test_cursor_cursor_round_trip(tmp_path):
    store = tmp_path / "store.db"
    _cursor_write(store, [_cursor_user("hello")])
    session = _cursor_session(store)
    first = open_activity_reader(session).poll()
    restored = open_activity_reader(session, cursor=first.cursor).poll()
    assert restored.events == ()
    assert restored.reset is False
    assert restored.generation == first.generation


def test_cursor_unavailable_history(tmp_path):
    store = tmp_path / "chat" / "store.db"
    session = _cursor_session(store)
    reader = open_activity_reader(session)
    result = reader.poll()
    assert result.state == "unavailable"
    assert result.cursor is None
    _cursor_write(store, [_cursor_user("hello")])
    back = reader.poll()
    assert back.state == "available"
    assert back.reset is True


# --- OpenCode v1 reader -----------------------------------------------------


def _v1_seed():
    return [
        ("u1", {"role": "user", "time": {"created": 1000}}, [
            ("p-u", {"type": "text", "text": "Run the task."}),
        ]),
        ("a1", {"role": "assistant", "time": {"created": 2000}, "finish": "stop"}, [
            ("p-text", {"type": "text", "text": "Starting."}),
            ("p-tool", {"type": "tool", "callID": "call-1", "tool": "bash",
                        "state": {"status": "completed",
                                  "input": '{"cmd":"pwd"}', "output": "repo"}}),
        ]),
    ]


def test_opencode_v1_cold_open_matches_snapshot(tmp_path):
    db = tmp_path / "opencode.db"
    _v1_db(db, "v1", _v1_seed())
    session = _opencode_session(db, "v1")
    reader = open_activity_reader(session)
    result = reader.poll()
    assert result.reset is True
    assert result.state == "available"
    snapshot = load_activity(session)
    assert _kinds(result.events) == _kinds(snapshot.events)
    assert result.outcome == snapshot.outcome
    assert json.loads(result.cursor)["runtime"] == "opencode"


def test_opencode_v1_append_and_no_change(tmp_path):
    db = tmp_path / "opencode.db"
    _v1_db(db, "v1", _v1_seed())
    session = _opencode_session(db, "v1")
    reader = open_activity_reader(session)
    first = reader.poll()
    opens, parsed = reader.db_opens, reader.rows_parsed
    assert open_activity_reader(session, cursor=first.cursor).poll().events == ()
    assert reader.poll().events == ()
    assert reader.db_opens == opens
    assert reader.rows_parsed == parsed
    _v1_append_message(
        db, "v1", "a2",
        {"role": "assistant", "time": {"created": 3000}, "finish": "stop"},
        [("p-t2", {"type": "text", "text": "Finished."})], 3000)
    result = reader.poll()
    assert result.reset is False
    assert _seqs(result.events) == [len(first.events) + 1]
    assert [e.text for e in result.events] == ["Finished."]
    combined = list(first.events) + list(result.events)
    assert _kinds(combined) == _kinds(load_activity(session).events)


def test_opencode_v1_concurrent_append(tmp_path):
    db = tmp_path / "opencode.db"
    _v1_db(db, "v1", _v1_seed())
    session = _opencode_session(db, "v1")
    reader = open_activity_reader(session)
    reader.poll()
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO message VALUES (?,?,?,?)",
                   ("u9", "v1", 9000, json.dumps({"role": "user"})))
    writer.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                   ("p-u9", "u9", "v1", 9000,
                    json.dumps({"type": "text", "text": "Late question?"})))
    writer.commit()
    writer.close()
    result = reader.poll()
    assert result.reset is False
    assert [e.text for e in result.events] == ["Late question?"]
    assert result.outcome == load_activity(session).outcome


def test_opencode_v1_replacement_resets(tmp_path):
    db = tmp_path / "opencode.db"
    _v1_db(db, "v1", _v1_seed())
    session = _opencode_session(db, "v1")
    reader = open_activity_reader(session)
    first = reader.poll()
    db.unlink()
    _v1_db(db, "v1", [
        ("u1", {"role": "user", "time": {"created": 1000}}, [
            ("p-u", {"type": "text", "text": "Different start."}),
        ]),
    ])
    result = reader.poll()
    assert result.reset is True
    assert result.generation != first.generation
    assert [e.text for e in result.events] == ["Different start."]


def test_opencode_v1_paging_preserves_pairing(tmp_path):
    db = tmp_path / "opencode.db"
    _v1_db(db, "v1", _v1_seed())
    session = _opencode_session(db, "v1")
    reader = open_activity_reader(session)
    full = reader.poll().events
    assert any(e.type == "tool_call" for e in full)
    newest = reader.page(limit=2)
    assert _seqs(newest.events) == [len(full) - 1, len(full)]
    assert newest.has_more is True
    older = reader.page(before=newest.before, limit=50)
    assert _seqs(older.events) == [1, 2, 3][: len(older.events)]
    assert older.has_more is False
    assembled = list(older.events) + list(newest.events)
    assert _kinds(assembled) == _kinds(full)


# --- OpenCode v2 reader -----------------------------------------------------


def _v2_seed():
    return [
        ("u1", 1, "user", {"time": {"created": 1000}, "text": "Run."}),
        ("a1", 2, "assistant", {
            "time": {"created": 2000}, "finish": "tool-calls",
            "content": [
                {"type": "text", "text": "Starting."},
                {"type": "tool", "id": "ok", "name": "bash", "state": {
                    "status": "completed", "input": {"cmd": "pwd"},
                    "content": [{"type": "text", "text": "repo"}],
                }},
            ],
        }),
    ]


def test_opencode_v2_cold_open_matches_snapshot(tmp_path):
    db = tmp_path / "opencode.db"
    _v2_db(db, "v2", _v2_seed())
    session = _opencode_session(db, "v2")
    reader = open_activity_reader(session)
    result = reader.poll()
    assert result.reset is True
    assert result.state == "available"
    snapshot = load_activity(session)
    assert _kinds(result.events) == _kinds(snapshot.events)
    assert result.outcome == snapshot.outcome


def test_opencode_v2_append_and_no_change(tmp_path):
    db = tmp_path / "opencode.db"
    _v2_db(db, "v2", _v2_seed())
    session = _opencode_session(db, "v2")
    reader = open_activity_reader(session)
    first = reader.poll()
    opens = reader.db_opens
    assert reader.poll().events == ()
    assert reader.db_opens == opens
    _v2_append(db, "v2", "a2", 3, "assistant", {
        "time": {"created": 3000}, "finish": "stop",
        "content": [{"type": "text", "text": "All done."}],
    })
    result = reader.poll()
    assert result.reset is False
    assert _seqs(result.events) == [len(first.events) + 1]
    combined = list(first.events) + list(result.events)
    assert _kinds(combined) == _kinds(load_activity(session).events)
    assert result.outcome == load_activity(session).outcome


def test_opencode_v2_concurrent_wal_append(tmp_path):
    db = tmp_path / "opencode.db"
    holder = sqlite3.connect(db)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_v2 (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, data TEXT);
        """
    )
    holder.execute("INSERT OR IGNORE INTO session_v2 VALUES (?)", ("v2",))
    for row_id, seq, kind, data in _v2_seed():
        holder.execute(
            "INSERT OR REPLACE INTO session_message VALUES (?,?,?,?,?,?)",
            (row_id, "v2", kind, seq, seq * 1000, json.dumps(data)),
        )
    holder.commit()
    session = _opencode_session(db, "v2")
    reader = open_activity_reader(session)
    reader.poll()
    writer = sqlite3.connect(db)
    writer.execute(
        "INSERT INTO session_message VALUES (?,?,?,?,?,?)",
        ("u9", "v2", "user", 9, 9000, json.dumps({"text": "WAL question?"})),
    )
    writer.commit()
    result = reader.poll()
    assert result.reset is False
    assert [e.text for e in result.events] == ["WAL question?"]
    writer.close()
    holder.close()


def test_opencode_v2_seq_reorder_resets(tmp_path):
    db = tmp_path / "opencode.db"
    _v2_db(db, "v2", _v2_seed())
    session = _opencode_session(db, "v2")
    reader = open_activity_reader(session)
    first = reader.poll()
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM session_message WHERE id = 'a1'")
    conn.execute(
        "INSERT INTO session_message VALUES (?,?,?,?,?,?)",
        ("a1", "v2", "assistant", 2, 2000, json.dumps({
            "finish": "stop", "content": [{"type": "text", "text": "Rewritten."}],
        })),
    )
    conn.commit()
    conn.close()
    result = reader.poll()
    assert result.reset is True
    assert result.generation != first.generation
    assert _kinds(result.events) == _kinds(load_activity(session).events)


def test_opencode_bad_cursor_falls_back(tmp_path):
    db = tmp_path / "opencode.db"
    _v2_db(db, "v2", _v2_seed())
    session = _opencode_session(db, "v2")
    full = load_activity(session)
    result = open_activity_reader(session, cursor="not-json{{{").poll()
    assert result.reset is True
    assert _kinds(result.events) == _kinds(full.events)
