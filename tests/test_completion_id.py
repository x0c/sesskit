"""completion_id: per-round stable identity for completion notifications."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sesskit import titles
from sesskit.models import completion_id_for
from sesskit.parsers import codex, pi


def _codex_session(path: Path, sid: str, last_line: dict) -> Path:
    lines = [
        {
            "timestamp": "2026-09-15T08:33:03.000Z",
            "type": "session_meta",
            "payload": {"cwd": "/tmp/demo", "thread_source": "user"},
        },
        {
            "timestamp": "2026-09-15T08:33:04.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "do the thing"},
        },
        last_line,
    ]
    path.write_text("\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8")
    return path


class CompletionIdHelperTests(unittest.TestCase):
    def test_non_terminal_has_empty_id(self) -> None:
        self.assertEqual(
            completion_id_for(
                file_mtime=100.0, size_bytes=10, status_tag=titles.STATUS_PENDING
            ),
            "",
        )
        self.assertEqual(
            completion_id_for(
                file_mtime=100.0, size_bytes=10, status_tag=titles.STATUS_NONE
            ),
            "",
        )

    def test_same_round_is_stable(self) -> None:
        first = completion_id_for(
            file_mtime=100.0,
            size_bytes=10,
            status_tag=titles.STATUS_DONE,
            tail_text="done:hello",
        )
        second = completion_id_for(
            file_mtime=100.0,
            size_bytes=10,
            status_tag=titles.STATUS_DONE,
            tail_text="done:hello",
        )
        self.assertTrue(first)
        self.assertEqual(first, second)

    def test_new_round_changes_id(self) -> None:
        first = completion_id_for(
            file_mtime=100.0,
            size_bytes=10,
            status_tag=titles.STATUS_DONE,
            tail_text="done:hello",
        )
        second = completion_id_for(
            file_mtime=101.0,
            size_bytes=20,
            status_tag=titles.STATUS_DONE,
            tail_text="done:world",
        )
        self.assertNotEqual(first, second)


class CodexCompletionIdTests(unittest.TestCase):
    def test_done_and_error_have_distinct_ids(self) -> None:
        sid = "01a0a432-a844-7300-951f-c6cc548cb2f4"
        with tempfile.TemporaryDirectory() as tmp:
            done_path = _codex_session(
                Path(tmp) / f"rollout-2026-09-15T16-32-58-{sid}.jsonl",
                sid,
                {
                    "timestamp": "2026-09-15T08:33:06.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "PONG",
                    },
                },
            )
            done = codex._build_session_info(str(done_path), {})
            assert done is not None
            self.assertEqual(done["status_tag"], titles.STATUS_DONE)
            self.assertTrue(done.get("completion_id"))

            err_path = _codex_session(
                Path(tmp) / f"rollout-2026-09-15T16-33-58-{sid}.jsonl",
                sid,
                {
                    "timestamp": "2026-09-15T08:33:06.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": None,
                        "error": {"message": "limit hit"},
                    },
                },
            )
            err = codex._build_session_info(str(err_path), {})
            assert err is not None
            self.assertEqual(err["status_tag"], titles.STATUS_ABORTED)
            self.assertTrue(err.get("completion_id"))
            self.assertNotEqual(done["completion_id"], err["completion_id"])

    def test_rescan_same_file_is_stable(self) -> None:
        sid = "01a0a432-a844-7300-951f-c6cc548cb2f4"
        with tempfile.TemporaryDirectory() as tmp:
            path = _codex_session(
                Path(tmp) / f"rollout-2026-09-15T16-32-58-{sid}.jsonl",
                sid,
                {
                    "timestamp": "2026-09-15T08:33:06.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": "PONG",
                    },
                },
            )
            first = codex._build_session_info(str(path), {})
            second = codex._build_session_info(str(path), {})
            assert first is not None and second is not None
            self.assertEqual(first["completion_id"], second["completion_id"])


class PiCompletionIdTests(unittest.TestCase):
    def _pi_session(self, path: Path, leaf_text: str, stop_reason: str = "stop") -> Path:
        # v1 风格：message 无 id/parentId，按文件顺序平铺为活动分支。
        entries = [
            {
                "type": "session",
                "id": "pi-session-1",
                "cwd": "/tmp/demo",
                "timestamp": "2026-09-15T08:33:03.000Z",
            },
            {
                "type": "message",
                "timestamp": "2026-09-15T08:33:04.000Z",
                "message": {"role": "user", "content": "do the thing"},
            },
            {
                "type": "message",
                "timestamp": "2026-09-15T08:33:05.000Z",
                "message": {
                    "role": "assistant",
                    "content": leaf_text,
                    "stopReason": stop_reason,
                },
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in entries) + "\n", encoding="utf-8")
        return path

    def test_done_has_id_and_new_round_changes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._pi_session(Path(tmp) / "s.jsonl", "hello")
            first, _ = pi._build_session_info(str(path))
            assert first is not None
            self.assertEqual(first["status_tag"], titles.STATUS_DONE)
            self.assertTrue(first.get("completion_id"))

            again, _ = pi._build_session_info(str(path))
            assert again is not None
            self.assertEqual(first["completion_id"], again["completion_id"])

    def test_error_round_has_distinct_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok_path = self._pi_session(Path(tmp) / "ok.jsonl", "hello", "stop")
            ok, _ = pi._build_session_info(str(ok_path))
            err_path = self._pi_session(Path(tmp) / "err.jsonl", "", "error")
            # 空正文 + error 仍独占一轮（errorMessage 为空时 stopReason 兜底）
            raw = json.loads(err_path.read_text(encoding="utf-8").splitlines()[-1])
            raw["message"]["errorMessage"] = "429 weekly limit"
            raw["message"]["content"] = ""
            lines = err_path.read_text(encoding="utf-8").splitlines()
            lines[-1] = json.dumps(raw)
            err_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            err, _ = pi._build_session_info(str(err_path))
            assert ok is not None and err is not None
            self.assertEqual(err["status_tag"], titles.STATUS_ABORTED)
            self.assertTrue(err.get("completion_id"))
            self.assertNotEqual(ok["completion_id"], err["completion_id"])


if __name__ == "__main__":
    unittest.main()
