"""Abnormal endings must surface as aborted status + retained error text."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sesskit import titles
from sesskit.parsers import claude, codex, opencode, pi
from sesskit.transcript import load_events


class CodexAbnormalEndingTests(unittest.TestCase):
    def test_task_complete_with_usage_limit_is_aborted(self) -> None:
        sid = "01a0a432-a844-7300-951f-c6cc548cb2f4"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"rollout-2026-09-15T16-32-58-{sid}.jsonl"
            err = {
                "message": "You've hit your usage limit. Try again later.",
                "codex_error_info": "usage_limit_exceeded",
            }
            lines = [
                {
                    "timestamp": "2026-09-15T08:33:03.000Z",
                    "type": "session_meta",
                    "payload": {"cwd": "/tmp/demo", "thread_source": "user"},
                },
                {
                    "timestamp": "2026-09-15T08:33:04.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "Reply with exactly the word PONG",
                    },
                },
                {
                    "timestamp": "2026-09-15T08:33:06.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": None,
                        "error": err,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
            info = codex._build_session_info(str(path), {})
            assert info is not None
            self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
            self.assertIn("usage limit", info["last_agent_msg"])
            conversation = codex.load_conversation(str(path))
            self.assertEqual([m.role for m in conversation], ["user", "assistant"])
            self.assertIn("usage limit", conversation[-1].text)
            events = load_events({"source": "codex", "path": str(path)})
            self.assertEqual([e["type"] for e in events], ["user_message", "assistant_message"])
            self.assertIn("usage limit", events[-1]["text"])

    def test_task_complete_without_error_stays_done(self) -> None:
        sid = "01a0a432-bbbb-7300-951f-c6cc548cb2f4"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"rollout-2026-09-15T16-40-00-{sid}.jsonl"
            lines = [
                {
                    "timestamp": "2026-09-15T08:40:00.000Z",
                    "type": "session_meta",
                    "payload": {"cwd": "/tmp/demo"},
                },
                {
                    "timestamp": "2026-09-15T08:40:01.000Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "hi"},
                },
                {
                    "timestamp": "2026-09-15T08:40:02.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "PONG",
                        "error": None,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
            info = codex._build_session_info(str(path), {})
            assert info is not None
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["last_agent_msg"], "PONG")


class PiAbnormalEndingTests(unittest.TestCase):
    def test_stop_reason_error_keeps_error_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-09-15T08-31-33-272Z_01a0a431-5b97-762d-ad90-050c6ff4e436.jsonl"
            err = (
                '429: {"message":"You\'ve reached your weekly usage limit for your plan.",'
                '"type":"rate_limit_error","code":"RATE_LIMITED"}'
            )
            lines = [
                {
                    "type": "session",
                    "version": 3,
                    "id": "01a0a431-5b97-762d-ad90-050c6ff4e436",
                    "timestamp": "2026-09-15T08:31:33.272Z",
                    "cwd": "/tmp/demo",
                },
                {
                    "type": "message",
                    "id": "m1",
                    "parentId": None,
                    "timestamp": "2026-09-15T08:31:34.000Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Reply with exactly the word PONG"}],
                    },
                },
                {
                    "type": "message",
                    "id": "m2",
                    "parentId": "m1",
                    "timestamp": "2026-09-15T08:31:35.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": err,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
            built = pi._build_session_info(str(path))
            assert built is not None
            info, _created = built
            self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
            self.assertIn("weekly usage limit", info["last_agent_msg"])
            conversation = pi.load_conversation(str(path))
            self.assertEqual([m.role for m in conversation], ["user", "assistant"])
            self.assertIn("RATE_LIMITED", conversation[-1].text)
            events = load_events({"source": "pi", "path": str(path), "id": info["id"]})
            self.assertEqual([e["type"] for e in events], ["user_message", "assistant_message"])
            self.assertIn("weekly usage limit", events[-1]["text"])

    def test_successful_stop_still_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-09-15T08-35-29-851Z_01a0a434-f7bb-73dc-b002-6fa2dfac6efc.jsonl"
            lines = [
                {
                    "type": "session",
                    "version": 3,
                    "id": "01a0a434-f7bb-73dc-b002-6fa2dfac6efc",
                    "timestamp": "2026-09-15T08:35:29.851Z",
                    "cwd": "/tmp/demo",
                },
                {
                    "type": "message",
                    "id": "m1",
                    "parentId": None,
                    "timestamp": "2026-09-15T08:35:30.000Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Reply with exactly the word PONG"}],
                    },
                },
                {
                    "type": "message",
                    "id": "m2",
                    "parentId": "m1",
                    "timestamp": "2026-09-15T08:35:31.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "PONG"}],
                        "stopReason": "stop",
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
            built = pi._build_session_info(str(path))
            assert built is not None
            info, _created = built
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["last_agent_msg"], "PONG")


def _claude_file_with_system_error(err: dict, follow_up_user: str | None = None) -> str:
    """Claude 2.1+ 风格 JSONL：用户提问 + system 报错，可选后续追问。"""
    import os

    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    lines = [
        {"type": "user", "cwd": "/tmp/demo", "timestamp": "2026-09-23T00:00:01.000Z",
         "message": {"role": "user", "content": "Reply with exactly the word PONG"}},
        {"type": "system", "timestamp": "2026-09-23T00:00:02.000Z", "isSidechain": False,
         "error": err},
    ]
    if follow_up_user:
        lines.append(
            {"type": "user", "timestamp": "2026-09-23T00:00:03.000Z",
             "message": {"role": "user", "content": follow_up_user}}
        )
    Path(path).write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
    return path


class ClaudeAbnormalEndingTests(unittest.TestCase):
    def test_401_system_error_is_aborted(self) -> None:
        import os

        err = {"message": '401 {"type":"error"}', "status": 401,
               "formatted": "401 API key is invalid.", "connection": None}
        path = _claude_file_with_system_error(err)
        try:
            info = claude._build_session_info(path, "proj")
            assert info is not None
            self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
            self.assertIn("401", info["last_agent_msg"])
            conversation = claude.load_conversation(path)
            self.assertEqual([m.role for m in conversation], ["user", "assistant"])
            self.assertIn("API key is invalid", conversation[-1].text)
            events = load_events({"source": "claude", "path": path})
            self.assertEqual([e["type"] for e in events], ["user_message", "assistant_message"])
            self.assertIn("API key is invalid", events[-1]["text"])
        finally:
            os.unlink(path)

    def test_connection_error_collapses_retries_to_one(self) -> None:
        import os

        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        rows = [
            {"type": "user", "cwd": "/tmp/demo", "timestamp": "2026-09-23T00:00:01.000Z",
             "message": {"role": "user", "content": "hi"}},
        ]
        for i in range(4):
            rows.append({"type": "system", "timestamp": f"2026-09-23T00:00:0{i + 2}.000Z",
                           "isSidechain": False,
                           "error": {"message": "Connection error.",
                                     "formatted": "Connection dropped (ECONNRESET)"}})
        Path(path).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        try:
            conversation = claude.load_conversation(path)
            assistants = [m for m in conversation if m.role == "assistant"]
            self.assertEqual(len(assistants), 1)
            self.assertIn("ECONNRESET", assistants[0].text)
        finally:
            os.unlink(path)

    def test_old_error_before_new_prompt_is_not_aborted(self) -> None:
        import os

        err = {"message": "Connection error.", "formatted": "Connection dropped (ECONNRESET)"}
        path = _claude_file_with_system_error(err, follow_up_user="are you back?")
        try:
            info = claude._build_session_info(path, "proj")
            assert info is not None
            # 新提问在后：上一轮的报错不算，当前是待回复。
            self.assertEqual(info["status_tag"], titles.STATUS_PENDING)
        finally:
            os.unlink(path)


def _opencode_db_with_error(err: dict) -> str:
    """Temp opencode.db with one user turn + one error-only assistant turn."""
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,
            time_created INTEGER, time_updated INTEGER, parent_id TEXT, time_archived INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        ("ses_err1", "/tmp/demo", "t", 1000, 2000, None, None),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m1", "ses_err1", 1001, 1001, json.dumps({"role": "user", "time": {"created": 1001000}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("p1", "m1", "ses_err1", 1001, 1001, json.dumps({"type": "text", "text": "do thing"})),
    )
    conn.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        (
            "m2",
            "ses_err1",
            1002,
            1002,
            json.dumps({"role": "assistant", "time": {"created": 1002000}, "error": err}),
        ),
    )
    conn.commit()
    conn.close()
    return path


class OpencodeAbnormalEndingTests(unittest.TestCase):
    def test_api_error_surfaces_status_and_text(self) -> None:
        import os

        err = {
            "name": "APIError",
            "data": {
                "message": "models/antigravity-gemini-3-pro-high is not found",
                "statusCode": 404,
                "isRetryable": False,
            },
        }
        path = _opencode_db_with_error(err)
        try:
            conn = opencode.connect_ro(path)
            assert conn is not None
            try:
                row = list(conn.execute(opencode._SCAN_SQL, (10,)).fetchall())[0]
            finally:
                conn.close()
            info = opencode._build_session_info(row, path)
            assert info is not None
            self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
            self.assertIn("not found", info["last_agent_msg"])
            self.assertIn("404", info["last_agent_msg"])
            conversation = opencode.load_conversation(path, "ses_err1")
            self.assertEqual([m.role for m in conversation], ["user", "assistant"])
            self.assertIn("not found", conversation[-1].text)
            events = load_events({"source": "opencode", "path": path, "id": "ses_err1"})
            self.assertEqual([e["type"] for e in events], ["user_message", "assistant_message"])
            self.assertIn("not found", events[-1]["text"])
        finally:
            os.unlink(path)

    def test_aborted_operation_surfaces_message(self) -> None:
        import os

        err = {"name": "MessageAbortedError", "data": {"message": "The operation was aborted."}}
        path = _opencode_db_with_error(err)
        try:
            conn = opencode.connect_ro(path)
            assert conn is not None
            try:
                row = list(conn.execute(opencode._SCAN_SQL, (10,)).fetchall())[0]
            finally:
                conn.close()
            info = opencode._build_session_info(row, path)
            assert info is not None
            self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
            self.assertIn("aborted", info["last_agent_msg"])
            conversation = opencode.load_conversation(path, "ses_err1")
            self.assertEqual(conversation[-1].role, "assistant")
            events = load_events({"source": "opencode", "path": path, "id": "ses_err1"})
            self.assertEqual(events[-1]["type"], "assistant_message")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
