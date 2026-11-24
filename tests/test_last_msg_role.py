"""Tests for ``last_msg_role`` across runtimes, caches, CLI payload, and schema."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from sesskit import cli
from sesskit.models import SessionInfo, make_session_info
from sesskit.parsers import antigravity, claude, codex, cursor, kimi, opencode, pi


class _MemorySessionCache:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], dict] = {}

    def get_session(self, runtime: str, path: str, extra_version: str = "") -> dict | None:
        return self._items.get((runtime, path, extra_version))

    def put_session(self, runtime: str, path: str, payload: dict, extra_version: str = "") -> None:
        self._items[(runtime, path, extra_version)] = payload


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_make_session_info_normalizes_last_msg_role():
    assert make_session_info(
        source="pi", id="1", short_id="1", cwd="/tmp", mtime=1.0,
        time_source="file_mtime", event_time=1.0, file_mtime=1.0, size_bytes=1,
        native_title=None, fallback_title="t", status_tag="", path="/tmp/1.jsonl",
        last_user_msg="ask", last_agent_msg="reply", last_msg_role="user",
    )["last_msg_role"] == "user"
    assert make_session_info(
        source="pi", id="2", short_id="2", cwd="/tmp", mtime=1.0,
        time_source="file_mtime", event_time=1.0, file_mtime=1.0, size_bytes=1,
        native_title=None, fallback_title="t", status_tag="", path="/tmp/2.jsonl",
        last_user_msg="", last_agent_msg="reply", last_msg_role="user",
    )["last_msg_role"] == "assistant"
    assert make_session_info(
        source="pi", id="3", short_id="3", cwd="/tmp", mtime=1.0,
        time_source="file_mtime", event_time=1.0, file_mtime=1.0, size_bytes=1,
        native_title=None, fallback_title="t", status_tag="", path="/tmp/3.jsonl",
        last_user_msg="ask", last_agent_msg="", last_msg_role="assistant",
    )["last_msg_role"] == "user"
    minimal = make_session_info(
        source="pi", id="4", short_id="4", cwd="/tmp", mtime=1.0,
        time_source="file_mtime", event_time=1.0, file_mtime=1.0, size_bytes=1,
        native_title=None, fallback_title="t", status_tag="", path="/tmp/4.jsonl",
        last_user_msg="", last_agent_msg="", last_msg_role="user",
    )
    assert minimal["last_msg_role"] is None
    assert set(minimal) == SessionInfo.__required_keys__


def test_schemas_identical_and_include_last_msg_role():
    root = Path(__file__).resolve().parents[1]
    schema_a = (root / "schemas" / "session.v1.json").read_text(encoding="utf-8")
    schema_b = (root / "src" / "sesskit" / "schemas" / "session.v1.json").read_text(encoding="utf-8")
    assert schema_a == schema_b
    parsed = json.loads(schema_a)
    assert "last_msg_role" in parsed["properties"]
    assert parsed["properties"]["last_msg_role"]["enum"] == ["user", "assistant", None]


def test_cli_session_payload_includes_last_msg_role():
    session = make_session_info(
        source="antigravity", id="abc", short_id="abc", cwd="/tmp", mtime=1.0,
        time_source="file_mtime", event_time=1.0, file_mtime=1.0, size_bytes=1,
        native_title="T", fallback_title="T", status_tag="⏳待回复", path="/tmp/a.jsonl",
        last_user_msg="follow up", last_agent_msg="old reply", last_msg_role="user",
    )
    payload = cli.session_payload(session)
    assert payload["last_msg_role"] == "user"


def test_antigravity_last_msg_role_and_cache_refresh(tmp_path, monkeypatch):
    root = tmp_path / ".gemini" / "antigravity-cli"
    cid = "11111111-1111-1111-1111-111111111111"
    transcript = root / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
    _write_jsonl(transcript, [
        {"step_index": 0, "type": "USER_INPUT", "source": "USER",
         "created_at": "2026-11-24T10:00:00Z",
         "content": "<USER_REQUEST>\ninitial ask\n</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
         "created_at": "2026-11-24T10:01:00Z", "content": "older reply"},
        {"step_index": 2, "type": "USER_INPUT", "source": "USER",
         "created_at": "2026-11-24T10:02:00Z",
         "content": "<USER_REQUEST>\nnewer user prompt\n</USER_REQUEST>"},
        {"step_index": 3, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "RUNNING",
         "created_at": "2026-11-24T10:02:05Z", "thinking": "thinking only",
         "tool_calls": [{"name": "read_file", "args": "{}"}]},
    ])
    db_path = root / "conversations" / f"{cid}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
        conn.execute(
            "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
            (f"file://{tmp_path}".encode(),),
        )
    monkeypatch.setattr(antigravity, "_roots", lambda: [str(root)])
    antigravity._LIST_MEMO.clear()

    info = antigravity.refresh_session({"id": cid, "path": str(transcript)})
    assert info is not None
    assert info["last_user_msg"] == "newer user prompt"
    assert info["last_agent_msg"] == "older reply"
    assert info["last_msg_role"] == "user"

    # Append a progress assistant text -> flips to assistant
    _write_jsonl(transcript, [
        {"step_index": 0, "type": "USER_INPUT", "source": "USER",
         "created_at": "2026-11-24T10:00:00Z",
         "content": "<USER_REQUEST>\ninitial ask\n</USER_REQUEST>"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
         "created_at": "2026-11-24T10:01:00Z", "content": "older reply"},
        {"step_index": 2, "type": "USER_INPUT", "source": "USER",
         "created_at": "2026-11-24T10:02:00Z",
         "content": "<USER_REQUEST>\nnewer user prompt\n</USER_REQUEST>"},
        {"step_index": 3, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
         "created_at": "2026-11-24T10:03:00Z", "content": "newest progress reply"},
    ])
    os.utime(transcript, (2_000_000_000, 2_000_000_000))
    info2 = antigravity.refresh_session({"id": cid, "path": str(transcript)})
    assert info2 is not None
    assert info2["last_msg_role"] == "assistant"
    assert info2["last_agent_msg"] == "newest progress reply"


def test_claude_last_msg_role_with_tail_and_backfill(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    fpath = tmp_path / "claude.jsonl"
    # Old assistant reply > 64KB before newer user prompt + tool calls
    filler = "x" * 70_000
    _write_jsonl(fpath, [
        {"type": "user", "cwd": str(proj), "timestamp": "2026-11-24T10:00:00Z",
         "message": {"content": "first ask"}},
        {"type": "assistant", "timestamp": "2026-11-24T10:01:00Z",
         "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "older agent reply"}]}},
        {"type": "assistant", "timestamp": "2026-11-24T10:01:05Z",
         "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"blob": filler}}]}},
        {"type": "user", "cwd": str(proj), "timestamp": "2026-11-24T10:02:00Z",
         "message": {"content": "newer user ask"}},
    ])
    info = claude._build_session_info(str(fpath), str(proj))
    assert info is not None
    assert info["last_user_msg"] == "newer user ask"
    assert info["last_agent_msg"] == "older agent reply"
    assert info["last_msg_role"] == "user"

    # Stale cache entry without last_msg_role must be invalidated
    cache = _MemorySessionCache()
    stale = {k: v for k, v in info.items() if k != "last_msg_role"}
    cache.put_session("claude", str(fpath), stale, "tag")
    rebuilt = claude._cached_session_info(str(fpath), str(proj), None, cache, "tag")
    assert rebuilt is not None
    assert rebuilt["last_msg_role"] == "user"


def test_codex_last_msg_role_with_backfill_and_superseded_error(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    fpath = tmp_path / "rollout-2026-11-24T10-00-00-11111111-1111-1111-1111-111111111111.jsonl"
    filler = "y" * 10_000
    _write_jsonl(fpath, [
        {"type": "session_meta", "timestamp": "2026-11-24T10:00:00Z",
         "payload": {"id": "11111111-1111-1111-1111-111111111111", "cwd": str(proj)}},
        {"type": "event_msg", "timestamp": "2026-11-24T10:00:01Z",
         "payload": {"type": "user_message", "message": "first ask"}},
        {"type": "event_msg", "timestamp": "2026-11-24T10:01:00Z",
         "payload": {"type": "agent_message", "message": "older agent reply"}},
        {"type": "event_msg", "timestamp": "2026-11-24T10:01:30Z",
         "payload": {"type": "task_complete", "error": {"codex_error_info": "usage_limit_exceeded", "message": "limit hit"}}},
        {"type": "response_item", "timestamp": "2026-11-24T10:01:31Z",
         "payload": {"type": "function_call", "name": "shell", "arguments": filler}},
        {"type": "event_msg", "timestamp": "2026-11-24T10:02:00Z",
         "payload": {"type": "user_message", "message": "continue after limit"}},
    ])
    info = codex._build_session_info(str(fpath), {})
    assert info is not None
    assert info["last_user_msg"] == "continue after limit"
    assert info["last_agent_msg"] == "older agent reply"
    assert info["last_msg_role"] == "user"


def test_pi_last_msg_role_with_superseded_error_and_user_tail(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    fpath = tmp_path / "pi.jsonl"
    _write_jsonl(fpath, [
        {"type": "session", "id": "pi-sess-1", "cwd": str(proj), "timestamp": "2026-11-24T10:00:00Z"},
        {"type": "message", "id": "m1", "parentId": None, "timestamp": "2026-11-24T10:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "first ask"}]}},
        {"type": "message", "id": "m2", "parentId": "m1", "timestamp": "2026-11-24T10:00:02Z",
         "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "older reply"}]}},
        {"type": "message", "id": "m3", "parentId": "m2", "timestamp": "2026-11-24T10:00:03Z",
         "message": {"role": "assistant", "stopReason": "error", "errorMessage": "rate limit", "content": []}},
        {"type": "message", "id": "m4", "parentId": "m3", "timestamp": "2026-11-24T10:00:04Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "retry now"}]}},
    ])
    built = pi._build_session_info(str(fpath))
    assert built is not None
    info, _ = built
    assert info["last_user_msg"] == "retry now"
    assert info["last_agent_msg"] == "older reply"
    assert info["last_msg_role"] == "user"


def _create_opencode_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                time_created INTEGER, time_updated INTEGER,
                parent_id TEXT, time_archived INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                session_id TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE session_v2 (
                id TEXT PRIMARY KEY, directory TEXT NOT NULL, title TEXT,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                parent_id TEXT, time_archived INTEGER
            );
            CREATE TABLE session_message (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
                seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL, data TEXT NOT NULL
            );
            """
        )


def test_opencode_v1_and_v2_last_msg_role_parity(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    db_path = tmp_path / "opencode.db"
    _create_opencode_db(db_path)
    with sqlite3.connect(db_path) as conn:
        # v1 session: assistant reply followed by newer user prompt
        conn.execute(
            "INSERT INTO session VALUES ('s_v1', ?, 'V1', 1000, 3000, NULL, NULL)",
            (str(proj),),
        )
        conn.execute("INSERT INTO message VALUES ('m1', 's_v1', 1000, 1000, '{\"role\":\"user\"}')")
        conn.execute("INSERT INTO part VALUES ('p1', 'm1', 's_v1', '{\"type\":\"text\",\"text\":\"ask 1\"}')")
        conn.execute("INSERT INTO message VALUES ('m2', 's_v1', 2000, 2000, '{\"role\":\"assistant\",\"finish\":\"stop\"}')")
        conn.execute("INSERT INTO part VALUES ('p2', 'm2', 's_v1', '{\"type\":\"text\",\"text\":\"reply 1\"}')")
        conn.execute("INSERT INTO message VALUES ('m3', 's_v1', 3000, 3000, '{\"role\":\"user\"}')")
        conn.execute("INSERT INTO part VALUES ('p3', 'm3', 's_v1', '{\"type\":\"text\",\"text\":\"ask 2\"}')")

        # v2 session: assistant reply followed by newer user prompt + tool-only assistant shell
        conn.execute(
            "INSERT INTO session_v2 VALUES ('s_v2', ?, 'V2', 1000, 4000, NULL, NULL)",
            (str(proj),),
        )
        conn.execute("INSERT INTO session_message VALUES ('vm1', 's_v2', 'user', 1, 1000, 1000, '{\"text\":\"v2 ask 1\"}')")
        conn.execute(
            "INSERT INTO session_message VALUES ('vm2', 's_v2', 'assistant', 2, 2000, 2000, ?)",
            (json.dumps({"content": [{"type": "text", "text": "v2 reply 1"}], "finish": "stop"}),),
        )
        conn.execute("INSERT INTO session_message VALUES ('vm3', 's_v2', 'user', 3, 3000, 3000, '{\"text\":\"v2 ask 2\"}')")
        conn.execute(
            "INSERT INTO session_message VALUES ('vm4', 's_v2', 'assistant', 4, 4000, 4000, ?)",
            (json.dumps({"content": [{"type": "tool", "name": "bash"}], "finish": "tool-calls"}),),
        )

    monkeypatch.setattr(opencode, "_db_paths", lambda: [str(db_path)])
    opencode._V2_LIST_MEMO.clear()
    rows_native = {s["id"]: s for s in opencode.scan_sessions(limit=10)}
    assert rows_native["s_v1"]["last_msg_role"] == "user"
    assert rows_native["s_v1"]["last_user_msg"] == "ask 2"
    assert rows_native["s_v2"]["last_msg_role"] == "user"
    assert rows_native["s_v2"]["last_user_msg"] == "v2 ask 2"

    # Parity with SESSKIT_NATIVE=0
    monkeypatch.setenv("SESSKIT_NATIVE", "0")
    opencode._V2_LIST_MEMO.clear()
    rows_py = {s["id"]: s for s in opencode.scan_sessions(limit=10)}
    assert rows_py["s_v2"]["last_msg_role"] == "user"


def test_cursor_and_kimi_last_msg_role(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    chat_dir = tmp_path / "cursor_chat"
    chat_dir.mkdir()
    (chat_dir / "meta.json").write_text(
        json.dumps({"cwd": str(proj), "title": "Cursor", "updatedAtMs": 1_700_000_000_000}),
        encoding="utf-8",
    )
    (chat_dir / "prompt_history.json").write_text(json.dumps(["cursor ask"]), encoding="utf-8")
    c_info = cursor._build_session_info(str(chat_dir), "c1")
    assert c_info is not None
    assert c_info["last_msg_role"] == "user"

    k_dir = tmp_path / "kimi_sess"
    (k_dir / "agents" / "main").mkdir(parents=True)
    (k_dir / "state.json").write_text(json.dumps({"cwd": str(proj), "title": "Kimi"}), encoding="utf-8")
    _write_jsonl(k_dir / "agents" / "main" / "wire.jsonl", [
        {"type": "context.append_message", "message": {"role": "user", "origin": {"kind": "user"}, "content": [{"type": "text", "text": "k1"}]}},
        {"type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "a1"}}},
        {"type": "context.append_message", "message": {"role": "user", "origin": {"kind": "user"}, "content": [{"type": "text", "text": "k2"}]}},
    ])
    k_info = kimi._build_session_info(str(k_dir), "k1")
    assert k_info is not None
    assert k_info["last_msg_role"] == "user"
