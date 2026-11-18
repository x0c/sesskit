"""Differential parity and edge-case tests for cross-runtime `loop_count`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sesskit import cli
from sesskit import native_projection as bridge
from sesskit.parsers import antigravity, claude, codex, cursor, opencode, pi


def _require_native():
    backend = bridge._backend()
    if backend is None:
        pytest.skip("native extension not installed")
    return backend


def _write_jsonl(path: Path, rows: list[object], *, trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
    body = "\n".join(lines) + ("\n" if trailing_newline and lines else "")
    path.write_text(body, encoding="utf-8")


def _scan_both_modes(monkeypatch, fn):
    monkeypatch.delenv("SESSKIT_NATIVE", raising=False)
    native_res = fn()
    monkeypatch.setenv("SESSKIT_NATIVE", "0")
    fallback_res = fn()
    return native_res, fallback_res


def test_session_schemas_are_identical_and_declare_loop_count():
    root = Path(__file__).resolve().parents[1]
    s1 = (root / "schemas" / "session.v1.json").read_bytes()
    s2 = (root / "src" / "sesskit" / "schemas" / "session.v1.json").read_bytes()
    assert s1 == s2
    schema = json.loads(s1)
    prop = schema["properties"]["loop_count"]
    assert prop["type"] == ["integer", "null"]
    assert prop["minimum"] == 0


def test_cli_session_payload_exposes_loop_count():
    payload = cli.session_payload({"source": "claude", "id": "s1", "short_id": "s1", "loop_count": 4})
    assert payload["loop_count"] == 4
    payload_none = cli.session_payload({"source": "cursor", "id": "s2", "short_id": "s2", "loop_count": None})
    assert payload_none["loop_count"] is None


def test_claude_loop_count_parity_and_edge_cases(tmp_path, monkeypatch):
    _require_native()
    work = tmp_path / "work"
    work.mkdir()
    proj = tmp_path / "projects" / "proj-a"
    monkeypatch.setattr(claude, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(claude, "SESSIONS_DIR", str(tmp_path / "sessions"))

    # Session 1: fragmented message.id, parallel tools, synthetic/meta/sidechain/error, malformed tail
    s1 = proj / "sess-1.jsonl"
    _write_jsonl(
        s1,
        [
            {"type": "user", "cwd": str(work), "timestamp": "2026-11-18T10:00:00Z", "message": {"role": "user", "content": "check"}},
            {"type": "assistant", "timestamp": "2026-11-18T10:00:01Z", "message": {"id": "m1", "model": "claude-sonnet", "content": [{"type": "thinking", "thinking": "t"}]}},
            {"type": "assistant", "timestamp": "2026-11-18T10:00:02Z", "message": {"id": "m1", "model": "claude-sonnet", "content": [{"type": "text", "text": "running"}]}},
            {"type": "assistant", "timestamp": "2026-11-18T10:00:03Z", "message": {"id": "m1", "model": "claude-sonnet", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}, {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]}},
            {"type": "assistant", "isMeta": True, "message": {"id": "meta-1", "model": "claude-sonnet", "content": [{"type": "text", "text": "meta"}]}},
            {"type": "assistant", "isSidechain": True, "message": {"id": "side-1", "model": "claude-sonnet", "content": [{"type": "text", "text": "side"}]}},
            {"type": "assistant", "isApiErrorMessage": True, "message": {"id": "err-1", "model": "<synthetic>", "content": [{"type": "text", "text": "401"}]}},
            {"type": "assistant", "message": {"id": "empty-1", "model": "claude-sonnet", "content": []}},
            {"type": "assistant", "timestamp": "2026-11-18T10:00:04Z", "message": {"model": "claude-sonnet", "content": [{"type": "text", "text": "legacy no id"}]}},
            {"type": "assistant", "timestamp": "2026-11-18T10:00:05Z", "message": {"id": "m2", "model": "claude-sonnet", "content": [{"type": "text", "text": "done"}]}},
            '{"type": "assistant", "message": {"id": "partial',
        ],
        trailing_newline=False,
    )

    # Session 2: user only (0 loops), no trailing newline
    s2 = proj / "sess-2.jsonl"
    _write_jsonl(
        s2,
        [{"type": "user", "cwd": str(work), "timestamp": "2026-11-18T09:00:00Z", "message": {"role": "user", "content": "hello"}}],
        trailing_newline=False,
    )

    # Session 3: valid final assistant line without trailing newline
    s3 = proj / "sess-3.jsonl"
    _write_jsonl(
        s3,
        [
            {"type": "user", "cwd": str(work), "timestamp": "2026-11-18T08:00:00Z", "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": "2026-11-18T08:00:01Z", "message": {"id": "x1", "model": "claude-sonnet", "content": [{"type": "text", "text": "ok"}]}},
        ],
        trailing_newline=False,
    )

    native_scan, fallback_scan = _scan_both_modes(monkeypatch, lambda: claude.scan_sessions(limit=10))
    assert {s["id"]: s["loop_count"] for s in native_scan} == {"sess-1": 3, "sess-2": 0, "sess-3": 1}
    assert {s["id"]: s["loop_count"] for s in fallback_scan} == {"sess-1": 3, "sess-2": 0, "sess-3": 1}

    for item in native_scan:
        ref_n, ref_f = _scan_both_modes(monkeypatch, lambda i=item: claude.refresh_session(i))
        assert ref_n is not None and ref_f is not None
        assert ref_n["loop_count"] == item["loop_count"] == ref_f["loop_count"]

    assert bridge.count_loops("claude", str(proj / "missing.jsonl")) is None


def test_codex_loop_count_parity_and_edge_cases(tmp_path, monkeypatch):
    _require_native()
    work = tmp_path / "work"
    work.mkdir()
    sessions_dir = tmp_path / "sessions" / "2026" / "11" / "18"
    monkeypatch.setattr(codex, "SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(codex, "SESSION_INDEX", str(tmp_path / "session_index.jsonl"))
    monkeypatch.setattr(codex, "live_pid_snapshot", lambda _name: [])

    # Modern rollout: parallel calls + interleaved item_completed + empty aborted token_usage_record
    u1 = "01a1b98f-2a90-74c3-be60-a751f53571b1"
    f1 = sessions_dir / f"rollout-2026-11-18T10-00-00-{u1}.jsonl"
    _write_jsonl(
        f1,
        [
            {"type": "session_meta", "timestamp": "2026-11-18T10:00:00Z", "payload": {"cwd": str(work)}},
            {"type": "event_msg", "timestamp": "2026-11-18T10:00:01Z", "payload": {"type": "task_started", "turn_id": "t1"}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:02Z", "payload": {"type": "message", "role": "developer", "content": []}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:03Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:04Z", "payload": {"type": "reasoning"}},
            {"type": "event_msg", "timestamp": "2026-11-18T10:00:05Z", "payload": {"type": "item_completed", "turn_id": "t1"}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:06Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "running"}]}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:07Z", "payload": {"type": "custom_tool_call", "call_id": "c1"}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:08Z", "payload": {"type": "custom_tool_call", "call_id": "c2"}},
            {"type": "token_usage_record", "timestamp": "2026-11-18T10:00:09Z", "payload": {"turn_id": "t1"}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:10Z", "payload": {"type": "custom_tool_call_output", "call_id": "c1"}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:11Z", "payload": {"type": "custom_tool_call_output", "call_id": "c2"}},
            {"type": "event_msg", "timestamp": "2026-11-18T10:00:12Z", "payload": {"type": "token_count"}},
            # Interrupted API call with zero response items:
            {"type": "token_usage_record", "timestamp": "2026-11-18T10:00:13Z", "payload": {"turn_id": "t1"}},
            {"type": "response_item", "timestamp": "2026-11-18T10:00:14Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
            {"type": "event_msg", "timestamp": "2026-11-18T10:00:15Z", "payload": {"type": "task_complete", "turn_id": "t1"}},
            '{"type": "response_item", "payload": ',
        ],
    )

    # Legacy event_msg-only rollout
    u2 = "01a1b98f-2a90-74c3-be60-a751f53571b2"
    f2 = sessions_dir / f"rollout-2026-11-18T09-00-00-{u2}.jsonl"
    _write_jsonl(
        f2,
        [
            {"type": "session_meta", "timestamp": "2026-11-18T09:00:00Z", "payload": {"cwd": str(work)}},
            {"type": "event_msg", "timestamp": "2026-11-18T09:00:01Z", "payload": {"type": "user_message", "message": "q1"}},
            {"type": "event_msg", "timestamp": "2026-11-18T09:00:02Z", "payload": {"type": "agent_message", "message": "step1"}},
            {"type": "event_msg", "timestamp": "2026-11-18T09:00:03Z", "payload": {"type": "task_complete", "last_agent_message": None}},
            {"type": "event_msg", "timestamp": "2026-11-18T09:00:04Z", "payload": {"type": "user_message", "message": "q2"}},
            {"type": "event_msg", "timestamp": "2026-11-18T09:00:05Z", "payload": {"type": "task_complete", "last_agent_message": "answer2"}},
        ],
    )

    native_scan, fallback_scan = _scan_both_modes(monkeypatch, lambda: codex.scan_sessions(limit=10))
    assert {s["id"]: s["loop_count"] for s in native_scan} == {u1: 2, u2: 2}
    assert {s["id"]: s["loop_count"] for s in fallback_scan} == {u1: 2, u2: 2}
    ref_n, ref_f = _scan_both_modes(monkeypatch, lambda: codex.refresh_session(native_scan[0]))
    assert ref_n["loop_count"] == ref_f["loop_count"] == 2


def test_pi_loop_count_branches_and_error_stubs(tmp_path, monkeypatch):
    _require_native()
    work = tmp_path / "work"
    work.mkdir()
    sdir = tmp_path / "pi-sessions" / "proj"
    monkeypatch.setattr(pi, "SESSIONS_DIR", str(tmp_path / "pi-sessions"))
    monkeypatch.setattr(pi, "live_processes", lambda _name: [])

    # Branched v2 tree: old branch abandoned, empty transport error stub skipped, thinking-only + toolCall counted
    p1 = sdir / "2026-11-18T10-00-00-000Z_pi-1.jsonl"
    _write_jsonl(
        p1,
        [
            {"type": "session", "id": "pi-1", "cwd": str(work), "timestamp": "2026-11-18T10:00:00Z"},
            {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-11-18T10:00:01Z", "message": {"role": "user", "content": "hi"}},
            {"type": "message", "id": "old-a", "parentId": "u1", "timestamp": "2026-11-18T10:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "abandoned"}]}},
            {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-11-18T10:00:03Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1"}, {"type": "toolCall", "id": "c2"}]}},
            {"type": "message", "id": "tr1", "parentId": "a1", "timestamp": "2026-11-18T10:00:04Z", "message": {"role": "toolResult", "toolCallId": "c1", "content": "ok"}},
            {"type": "message", "id": "err-stub", "parentId": "tr1", "timestamp": "2026-11-18T10:00:05Z", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "ECONNRESET", "content": []}},
            {"type": "message", "id": "a2", "parentId": "err-stub", "timestamp": "2026-11-18T10:00:06Z", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "done"}]}},
        ],
    )

    # Flat v1 Pi (no message id/parentId)
    p2 = sdir / "2026-11-18T09-00-00-000Z_pi-2.jsonl"
    _write_jsonl(
        p2,
        [
            {"type": "session", "id": "pi-2", "cwd": str(work), "timestamp": "2026-11-18T09:00:00Z"},
            {"type": "message", "timestamp": "2026-11-18T09:00:01Z", "message": {"role": "user", "content": "hi"}},
            {"type": "message", "timestamp": "2026-11-18T09:00:02Z", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}]}},
        ],
    )

    pi._LIST_MEMO.clear()
    pi._entry_cache_clear()
    scanned = pi.scan_sessions(limit=10)
    assert {s["id"]: s["loop_count"] for s in scanned} == {"pi-1": 2, "pi-2": 1}

    # Verify Rust count_loops_many matches Python exactly
    rust_counts = bridge.count_loops_many([("pi", str(p1)), ("pi", str(p2))])
    assert rust_counts == [2, 1]
    assert pi.refresh_session(scanned[0])["loop_count"] == scanned[0]["loop_count"]


def test_antigravity_loop_count_transcript_and_sqlite_only(tmp_path, monkeypatch):
    _require_native()
    work = tmp_path / "work"
    work.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    tpath = root / "brain" / "ag-1" / ".system_generated" / "logs" / "transcript_full.jsonl"
    _write_jsonl(
        tpath,
        [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": "2026-11-18T10:00:00Z", "content": "<USER_REQUEST>go</USER_REQUEST>"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-11-18T10:00:01Z", "content": "step", "tool_calls": [{"name": "a"}, {"name": "b"}]},
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-18T10:00:02Z", "content": "out"},
            {"step_index": 3, "source": "SYSTEM", "type": "CHECKPOINT", "status": "DONE", "created_at": "2026-11-18T10:00:03Z", "content": "compact"},
            {"step_index": 4, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "created_at": "2026-11-18T10:00:04Z", "error": "rate limit"},
            {"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "CANCELED", "created_at": "2026-11-18T10:00:05Z", "content": "None"},
            {"step_index": 6, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-11-18T10:00:06Z", "content": "final"},
        ],
    )

    conv_dir = root / "conversations"
    conv_dir.mkdir(parents=True)
    db2 = conv_dir / "ag-sqlite.db"
    c2 = sqlite3.connect(db2)
    c2.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
    c2.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (f"file://{work}".encode(),))
    c2.commit()
    c2.close()

    sdb = root / "conversation_summaries.db"
    cs = sqlite3.connect(sdb)
    cs.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT, step_count INTEGER, last_modified_time TEXT, workspace_uris TEXT, status TEXT, killed INTEGER, last_user_input_time TEXT, app_data_dir TEXT, parent_conversation_id TEXT, nesting_depth INTEGER, not_fully_idle INTEGER)"
    )
    uris = json.dumps([f"file://{work}"])
    cs.execute(
        "INSERT INTO conversation_summaries VALUES ('ag-1', 'T1', 'final', 7, '2026-11-18T10:00:06Z', ?, '', 0, '', '', '', 0, 0)",
        (uris,),
    )
    cs.execute(
        "INSERT INTO conversation_summaries VALUES ('ag-sqlite', 'T2', '', 12, '2026-11-18T09:00:00Z', ?, '', 0, '', '', '', 0, 0)",
        (uris,),
    )
    cs.commit()
    cs.close()

    monkeypatch.setattr(antigravity, "_roots", lambda: [str(root)])
    monkeypatch.setattr(antigravity, "live_pid_snapshot", lambda _name: [])

    scanned = antigravity.scan_sessions(limit=10)
    by_id = {s["id"]: s for s in scanned}
    assert by_id["ag-1"]["loop_count"] == 2
    assert by_id["ag-sqlite"]["loop_count"] is None
    assert bridge.count_loops_many([("antigravity", str(tpath)), ("antigravity", str(db2))]) == [2, None]
    assert antigravity.refresh_session(by_id["ag-1"])["loop_count"] == 2
    assert antigravity.refresh_session(by_id["ag-sqlite"])["loop_count"] is None


def test_opencode_v1_and_v2_loop_count_parity(tmp_path, monkeypatch):
    _require_native()
    work = tmp_path / "work"
    work.mkdir()
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT, time_archived INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, parent_id TEXT, time_archived INTEGER);
        CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT);
    """)
    # v1 session: 1 valid assistant (2 parts), 1 assistant with only synthetic part (skip), 1 empty assistant (skip)
    conn.execute("INSERT INTO session VALUES ('v1-s', ?, 'V1', 1000, 2000, NULL, NULL)", (str(work),))
    conn.execute("INSERT INTO message VALUES ('m-u', 'v1-s', 1001, 1001, ?)", (json.dumps({"role": "user"}),))
    conn.execute("INSERT INTO part VALUES ('p-u', 'm-u', 'v1-s', 1001, 1001, ?)", (json.dumps({"type": "text", "text": "hello"}),))
    conn.execute("INSERT INTO message VALUES ('m-a1', 'v1-s', 1002, 1002, ?)", (json.dumps({"role": "assistant", "finish": "stop"}),))
    conn.execute("INSERT INTO part VALUES ('p-a1a', 'm-a1', 'v1-s', 1002, 1002, ?)", (json.dumps({"type": "tool", "tool": "bash"}),))
    conn.execute("INSERT INTO part VALUES ('p-a1b', 'm-a1', 'v1-s', 1002, 1002, ?)", (json.dumps({"type": "text", "text": "done"}),))
    conn.execute("INSERT INTO message VALUES ('m-a2', 'v1-s', 1003, 1003, ?)", (json.dumps({"role": "assistant"}),))
    conn.execute("INSERT INTO part VALUES ('p-a2', 'm-a2', 'v1-s', 1003, 1003, ?)", (json.dumps({"type": "text", "text": "synth", "synthetic": 1}),))
    conn.execute("INSERT INTO message VALUES ('m-a3', 'v1-s', 1004, 1004, ?)", (json.dumps({"role": "assistant", "finish": "error", "error": {"message": "fail"}}),))

    # v2 session: 2 valid assistants (1 with tools, 1 finish=stop empty), 1 empty error assistant (skip), plus system/synthetic/compaction/idle
    conn.execute("INSERT INTO session_v2 VALUES ('v2-s', ?, 'V2', 3000, 4000, NULL, NULL)", (str(work),))
    v2_rows = [
        ("sm1", "v2-s", "user", 1, {"text": "hi"}),
        ("sm2", "v2-s", "system", 2, {"text": "sys"}),
        ("sm3", "v2-s", "assistant", 3, {"content": [{"type": "reasoning", "text": "r"}, {"type": "tool", "name": "a"}, {"type": "tool", "name": "b"}]}),
        ("sm4", "v2-s", "compaction", 4, {"summary": "c"}),
        ("sm5", "v2-s", "assistant", 5, {"content": [], "finish": "error", "error": {"message": "quota", "status": 429}}),
        ("sm6", "v2-s", "assistant", 6, {"content": [], "finish": "stop"}),
        ("sm7", "v2-s", "idle", 7, {"outcome": "ok"}),
    ]
    for mid, sid, rtype, seq, data in v2_rows:
        conn.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)", (mid, sid, rtype, seq, seq, seq, json.dumps(data)))
    conn.commit()
    conn.close()

    monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(opencode, "live_processes", lambda _name: [])

    def _run():
        opencode._V2_LIST_MEMO.clear()
        return {s["id"]: s for s in opencode.scan_sessions(limit=10)}

    native_map, fallback_map = _scan_both_modes(monkeypatch, _run)
    assert native_map["v1-s"]["loop_count"] == 1
    assert native_map["v2-s"]["loop_count"] == 2
    assert fallback_map["v1-s"]["loop_count"] == 1
    assert fallback_map["v2-s"]["loop_count"] == 2
    assert opencode.refresh_session(native_map["v2-s"])["loop_count"] == 2
    assert opencode.refresh_session(native_map["v1-s"])["loop_count"] == 1


def test_cursor_loop_count_store_wal_empty_and_prompt_only(tmp_path, monkeypatch):
    _require_native()
    work = tmp_path / "work"
    work.mkdir()
    chats = tmp_path / "chats" / "ws1"
    monkeypatch.setattr(cursor, "CHATS_DIR", str(tmp_path / "chats"))
    monkeypatch.setattr(cursor, "live_processes", lambda _name: [])

    def _make_chat(cid: str, *, with_prompts: bool = True, title: str | None = "Title"):
        cdir = chats / cid
        cdir.mkdir(parents=True)
        (cdir / "meta.json").write_text(json.dumps({"cwd": str(work), "title": title, "updatedAtMs": 1_700_000_000_000}), encoding="utf-8")
        if with_prompts:
            (cdir / "prompt_history.json").write_text(json.dumps(["hello"]), encoding="utf-8")
        return cdir

    # Chat 1: store.db + uncheckpointed WAL row
    c1 = _make_chat("11111111-1111-1111-1111-111111111111")
    db1 = c1 / "store.db"
    conn = sqlite3.connect(db1)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("INSERT INTO blobs VALUES ('b0', ?)", (b"\x01\x02dag",))
    conn.execute("INSERT INTO blobs VALUES ('b1', ?)", (json.dumps({"role": "user", "content": "<user_query>hi</user_query>"}).encode(),))
    conn.execute("INSERT INTO blobs VALUES ('b2', ?)", (json.dumps({"role": "assistant", "content": [{"type": "reasoning", "text": "r"}, {"type": "tool-call", "toolCallId": "1"}, {"type": "tool-call", "toolCallId": "2"}]}).encode(),))
    conn.execute("INSERT INTO blobs VALUES ('b3', ?)", (json.dumps({"role": "tool", "content": [{"type": "tool-result", "toolCallId": "1"}]}).encode(),))
    conn.execute("INSERT INTO blobs VALUES ('b4', ?)", (json.dumps({"role": "assistant", "content": []}).encode(),))
    conn.commit()
    # Second connection leaves an uncheckpointed WAL row
    conn2 = sqlite3.connect(db1)
    conn2.execute("INSERT INTO blobs VALUES ('b5', ?)", (json.dumps({"role": "assistant", "content": "final"}).encode(),))
    conn2.commit()

    # Chat 2: store.db with user-only JSON blob -> 0
    c2 = _make_chat("22222222-2222-2222-2222-222222222222")
    conn_u = sqlite3.connect(c2 / "store.db")
    conn_u.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn_u.execute("INSERT INTO blobs VALUES ('b1', ?)", (json.dumps({"role": "user", "content": "hi"}).encode(),))
    conn_u.commit()
    conn_u.close()

    # Chat 3: store.db with only binary DAG blobs -> None
    c3 = _make_chat("33333333-3333-3333-3333-333333333333")
    conn_b = sqlite3.connect(c3 / "store.db")
    conn_b.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn_b.execute("INSERT INTO blobs VALUES ('b0', ?)", (b"\x00\x01binary",))
    conn_b.commit()
    conn_b.close()

    # Chat 4: prompt_history.json only (no store.db) -> None
    _make_chat("44444444-4444-4444-4444-444444444444")

    try:
        native_scan, fallback_scan = _scan_both_modes(monkeypatch, lambda: cursor.scan_sessions(limit=10))
        expected = {
            "11111111-1111-1111-1111-111111111111": 2,
            "22222222-2222-2222-2222-222222222222": 0,
            "33333333-3333-3333-3333-333333333333": None,
            "44444444-4444-4444-4444-444444444444": None,
        }
        assert {s["id"]: s["loop_count"] for s in native_scan} == expected
        assert {s["id"]: s["loop_count"] for s in fallback_scan} == expected
        for item in native_scan:
            assert cursor.refresh_session(item)["loop_count"] == item["loop_count"]
    finally:
        conn2.close()
        conn.close()


def test_nonstandard_json_falls_back_cleanly_for_loop_count(tmp_path, monkeypatch):
    _require_native()
    p = tmp_path / "weird.jsonl"
    p.write_text(
        '{"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "\\ud800"}], "extra": NaN}}\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("SESSKIT_NATIVE", raising=False)
    native_count = bridge.count_loops("claude", str(p))
    monkeypatch.setenv("SESSKIT_NATIVE", "0")
    fallback_count = bridge.count_loops("claude", str(p))
    assert native_count == fallback_count == 1
