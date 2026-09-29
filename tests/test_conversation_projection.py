"""Stage F: plain conversation as a projection of typed activity.

Per-runtime fixtures assert ``conversation_from_activity`` output, the
unified error policy (error-only assistant turns hidden by default, shown
with ``include_errors=True``), and adapter/registry wiring. Kimi stays on
its legacy parser by explicit scope decision.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sesskit.activity import load_activity
from sesskit.adapters import get_adapter
from sesskit.conversation import (
    conversation_from_activity,
    project_session_conversation,
)
from sesskit.parsers import kimi
from sesskit.registry import ConversationLoadError, load_session_conversation


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _roles(messages) -> list[tuple[str, str]]:
    return [(m.role, m.text) for m in messages]


# --- Pi ---------------------------------------------------------------------


def _pi_session(path: Path) -> dict:
    return {"source": "pi", "path": str(path), "id": "pi-projection"}


def _pi_history(path: Path) -> None:
    _write_jsonl(path, [
        {"type": "session", "id": "pi-projection",
         "timestamp": "2026-09-01T00:00:00Z", "cwd": "/tmp/pi-projection"},
        {"type": "message", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": "Run checks."}},
        {"type": "message", "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "On it."},
                                 {"type": "toolCall", "id": "c1", "name": "bash",
                                  "arguments": {}}]}},
        {"type": "message", "timestamp": "2026-09-01T00:00:03Z",
         "message": {"role": "assistant", "content": [],
                     "stopReason": "error", "errorMessage": "boom"}},
    ])


class PiProjectionTests(unittest.TestCase):
    def test_text_shown_tools_skipped_errors_hidden_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _pi_history(path)
            snapshot = load_activity(_pi_session(path))
            self.assertEqual(snapshot.state, "available")

            self.assertEqual(
                _roles(conversation_from_activity(snapshot)),
                [("user", "Run checks."), ("assistant", "On it.")],
            )
            self.assertEqual(
                _roles(conversation_from_activity(snapshot, include_errors=True)),
                [("user", "Run checks."), ("assistant", "On it."),
                 ("assistant", "boom")],
            )


# --- Claude -----------------------------------------------------------------


def _claude_session(path: Path) -> dict:
    return {"source": "claude", "path": str(path), "id": "claude-projection"}


def _claude_history(path: Path) -> None:
    _write_jsonl(path, [
        {"type": "user", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "Hello."}]}},
        {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "Hmm."},
             {"type": "text", "text": "Hi there."}]}},
        {"type": "system", "timestamp": "2026-09-01T00:00:03Z",
         "error": {"formatted": "429 rate limited.", "status": 429}},
    ])


class ClaudeProjectionTests(unittest.TestCase):
    def test_system_error_hidden_by_default_with_native_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _claude_history(path)
            snapshot = load_activity(_claude_session(path))

            error_events = [e for e in snapshot.events
                            if e.type == "assistant_message" and e.error is not None]
            self.assertEqual(len(error_events), 1)
            self.assertEqual(error_events[0].error.code, "429")

            self.assertEqual(
                _roles(conversation_from_activity(snapshot)),
                [("user", "Hello."), ("assistant", "Hi there.")],
            )
            self.assertEqual(
                _roles(conversation_from_activity(snapshot, include_errors=True)),
                [("user", "Hello."), ("assistant", "Hi there."),
                 ("assistant", "429 rate limited.")],
            )


# --- Codex ------------------------------------------------------------------


def _codex_session(path: Path) -> dict:
    return {"source": "codex", "path": str(path), "id": "codex-projection"}


def _codex_history(path: Path) -> None:
    _write_jsonl(path, [
        {"type": "event_msg", "timestamp": "2026-09-01T00:00:01Z",
         "payload": {"type": "user_message", "message": "Deploy."}},
        {"type": "event_msg", "timestamp": "2026-09-01T00:00:02Z",
         "payload": {"type": "agent_message", "message": "Deploying."}},
        {"type": "event_msg", "timestamp": "2026-09-01T00:00:03Z",
         "payload": {"type": "task_complete", "last_agent_message": None,
                     "error": {"message": "quota gone",
                               "codex_error_info": "usage_limit_exceeded"}}},
    ])


class CodexProjectionTests(unittest.TestCase):
    def test_task_complete_error_hidden_by_default_with_native_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _codex_history(path)
            snapshot = load_activity(_codex_session(path))

            error_events = [e for e in snapshot.events
                            if e.type == "assistant_message" and e.error is not None]
            self.assertEqual(len(error_events), 1)
            self.assertEqual(error_events[0].error.code, "usage_limit_exceeded")

            self.assertEqual(
                _roles(conversation_from_activity(snapshot)),
                [("user", "Deploy."), ("assistant", "Deploying.")],
            )
            self.assertEqual(
                _roles(conversation_from_activity(snapshot, include_errors=True)),
                [("user", "Deploy."), ("assistant", "Deploying."),
                 ("assistant", "quota gone")],
            )


# --- OpenCode ---------------------------------------------------------------


def _opencode_session(path: Path, session_id: str) -> dict:
    return {"source": "opencode", "path": str(path), "id": session_id}


def _opencode_v1_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT INTO session VALUES (?)", ("v1",))
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("m-user", "v1", 1000, json.dumps(
            {"role": "user", "time": {"created": 1000}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("p-user", "m-user", "v1", 1000, json.dumps(
            {"type": "text", "text": "Fix it."})),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("m-err", "v1", 2000, json.dumps(
            {"role": "assistant", "time": {"created": 2000},
             "error": {"name": "APIError",
                       "data": {"message": "bad gateway", "statusCode": 502}}})),
    )
    conn.commit()
    conn.close()


def _opencode_v2_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY);
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT INTO session_v2 VALUES (?)", ("v2",))
    rows = [
        ("r-user", 1, "user", {"text": "Ship it."}),
        ("r-ok", 2, "assistant",
         {"content": [{"type": "text", "text": "Shipping."}], "finish": "stop"}),
        ("r-err", 3, "assistant",
         {"content": [], "error": {"type": "provider", "message": "overloaded"}}),
    ]
    for row_id, seq, kind, data in rows:
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?)",
            (row_id, "v2", kind, seq, seq * 1000, json.dumps(data)),
        )
    conn.commit()
    conn.close()


class OpenCodeProjectionTests(unittest.TestCase):
    def test_v1_error_hidden_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "open.db"
            _opencode_v1_db(path)
            session = _opencode_session(path, "v1")
            snapshot = load_activity(session)
            self.assertEqual(snapshot.state, "available")

            self.assertEqual(
                _roles(conversation_from_activity(snapshot)),
                [("user", "Fix it.")],
            )
            shown = conversation_from_activity(snapshot, include_errors=True)
            self.assertEqual(
                _roles(shown),
                [("user", "Fix it."), ("assistant", "502: bad gateway")],
            )

    def test_v2_error_hidden_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "open.db"
            _opencode_v2_db(path)
            session = _opencode_session(path, "v2")

            self.assertEqual(
                _roles(conversation_from_activity(load_activity(session))),
                [("user", "Ship it."), ("assistant", "Shipping.")],
            )
            self.assertEqual(
                _roles(conversation_from_activity(
                    load_activity(session), include_errors=True)),
                [("user", "Ship it."), ("assistant", "Shipping."),
                 ("assistant", "overloaded")],
            )

    def test_v1_mixed_text_and_error_always_shown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "open.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE session (id TEXT PRIMARY KEY);
                CREATE TABLE message (
                    id TEXT PRIMARY KEY, session_id TEXT,
                    time_created INTEGER, data TEXT);
                CREATE TABLE part (
                    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                    time_created INTEGER, data TEXT);
                """
            )
            conn.execute("INSERT INTO session VALUES (?)", ("v1",))
            conn.execute(
                "INSERT INTO message VALUES (?,?,?,?)",
                ("m-mix", "v1", 1000, json.dumps(
                    {"role": "assistant", "time": {"created": 1000},
                     "error": {"name": "X", "data": {"message": "late failure"}}})),
            )
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                ("p-mix", "m-mix", "v1", 1000, json.dumps(
                    {"type": "text", "text": "Partial answer."})),
            )
            conn.commit()
            conn.close()

            messages = conversation_from_activity(
                load_activity(_opencode_session(path, "v1")))
            self.assertEqual(_roles(messages), [("assistant", "Partial answer.")])


# --- Cursor -----------------------------------------------------------------


def _cursor_session(path: Path) -> dict:
    return {"source": "cursor", "path": str(path), "id": "cursor-projection"}


def _cursor_store(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE IF EXISTS blobs")
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    objects = [
        {"role": "user", "content": "<user_query>Read it</user_query>"},
        {"role": "assistant", "content": [
            {"type": "reasoning", "text": "Inspecting."},
            {"type": "text", "text": "Reading now."},
            {"type": "tool-call", "toolCallId": "r1",
             "toolName": "Read", "args": "{}"},
        ]},
    ]
    for index, obj in enumerate(objects):
        connection.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            (f"blob-{index}", json.dumps(obj).encode()),
        )
    connection.commit()
    connection.close()


class CursorProjectionTests(unittest.TestCase):
    def test_reasoning_and_tools_skipped_no_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_store(path)
            messages = conversation_from_activity(
                load_activity(_cursor_session(path)))
            self.assertEqual(
                _roles(messages),
                [("user", "Read it"), ("assistant", "Reading now.")],
            )
            self.assertTrue(all(m.timestamp is None for m in messages))


# --- Wiring -----------------------------------------------------------------


def _pi_wiring_history(path: Path) -> None:
    _write_jsonl(path, [
        {"type": "session", "id": "w", "timestamp": "2026-09-01T00:00:00Z",
         "cwd": "/tmp/pi-wiring"},
        {"type": "message", "timestamp": "2026-09-01T00:00:01Z",
         "message": {"role": "user", "content": "Hi."}},
        {"type": "message", "timestamp": "2026-09-01T00:00:02Z",
         "message": {"role": "assistant", "content": "Hello."}},
    ])


class AdapterWiringTests(unittest.TestCase):
    def test_adapters_project_and_keep_missing_history_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _pi_wiring_history(path)
            session = {"source": "pi", "path": str(path), "id": "w"}

            via_adapter = get_adapter("pi").load_conversation(session)
            via_public = load_session_conversation(session)
            self.assertEqual(_roles(via_adapter), [("user", "Hi."), ("assistant", "Hello.")])
            self.assertEqual(_roles(via_public), _roles(via_adapter))

            missing = {"source": "pi", "path": str(Path(directory) / "gone.jsonl")}
            with self.assertRaises(ConversationLoadError):
                load_session_conversation(missing)
            with self.assertRaises(ConversationLoadError):
                load_session_conversation({"source": "nope", "path": str(path)})

    def test_empty_history_projects_to_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [
                {"type": "session", "id": "w",
                 "timestamp": "2026-09-01T00:00:00Z", "cwd": "/tmp/pi-wiring"},
            ])
            session = {"source": "pi", "path": str(path), "id": "w"}
            self.assertEqual(load_session_conversation(session), [])

    def test_zero_event_history_projects_to_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("not json at all {{{{\n" * 3, encoding="utf-8")
            session = {"source": "claude", "path": str(path), "id": "broken"}
            # Readable file with zero normalized events -> empty list (not an error).
            self.assertEqual(load_session_conversation(session), [])

    def test_opencode_missing_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "open.db"
            _opencode_v1_db(path)
            with self.assertRaises(ConversationLoadError):
                load_session_conversation({"source": "opencode", "path": str(path)})

    def test_kimi_stays_on_legacy_parser(self) -> None:
        self.assertFalse(get_adapter("kimi").capabilities.conversation_include_errors)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kimi.jsonl"
            path.write_text("", encoding="utf-8")
            session = {"source": "kimi", "path": str(path), "id": "k"}
            self.assertEqual(
                _roles(load_session_conversation(session)),
                _roles(kimi.load_conversation(str(path))),
            )

    def test_project_helper_states(self) -> None:
        from sesskit.models import ActivitySnapshot, Evidence, SessionOutcome

        empty = ActivitySnapshot(
            "empty", (), SessionOutcome("unknown", Evidence("unknown")))
        self.assertEqual(project_session_conversation(empty), [])
        unavailable = ActivitySnapshot(
            "unavailable", (), SessionOutcome("unknown", Evidence("unknown")))
        with self.assertRaises(ConversationLoadError):
            project_session_conversation(unavailable, history_ref="/x")

    def test_injected_user_rows_skipped_like_v1(self) -> None:
        from sesskit.models import (
            ActivityEvent,
            ActivitySnapshot,
            Evidence,
            SessionOutcome,
        )

        snapshot = ActivitySnapshot(
            "available",
            (ActivityEvent(1, "user_message", Evidence("native"),
                           text="real question", origin="human"),
             ActivityEvent(2, "user_message", Evidence("native"),
                           text="<command>chrome</command>", origin="injected"),
             ActivityEvent(3, "assistant_message", Evidence("native"),
                           text="answer")),
            SessionOutcome("unknown", Evidence("unknown")),
        )
        self.assertEqual(
            [(m.role, m.text) for m in conversation_from_activity(snapshot)],
            [("user", "real question"), ("assistant", "answer")],
        )


if __name__ == "__main__":
    unittest.main()
