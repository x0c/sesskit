"""Abnormal endings must surface as aborted status + retained error text."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sesskit import titles
from sesskit.parsers import codex, pi
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


if __name__ == "__main__":
    unittest.main()
