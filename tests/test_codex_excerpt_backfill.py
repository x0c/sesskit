"""Running Codex turns: list excerpt backfills the prompt, status stays on the 8 KB tail."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sesskit import titles
from sesskit.parsers import codex

UUID = "01a0f646-e8da-7151-8a19-3a62c48a9ad5"


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


class CodexExcerptBackfillTests(unittest.TestCase):
    def test_prompt_beyond_tail_backfills_without_status_change(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"rollout-2026-10-01T15-03-57-{UUID}.jsonl"
            lines = [
                _line({"type": "session_meta", "timestamp": "2026-10-01T07:03:57Z",
                       "payload": {"id": UUID, "cwd": td}}),
                _line({"type": "event_msg", "timestamp": "2026-10-01T07:04:00Z",
                       "payload": {"type": "user_message", "message": "design haptic feedback"}}),
            ]
            for i in range(20):  # ~80 KB of tool traffic, no assistant text yet
                lines.append(_line({"type": "response_item", "payload": {
                    "type": "function_call", "name": "shell", "call_id": f"c{i}",
                    "arguments": json.dumps({"cmd": "ls"})}}))
                lines.append(_line({"type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": f"c{i}", "output": "x" * 4000}}))
            path.write_text("".join(lines))
            info = codex._build_session_info(str(path), {})
        self.assertEqual(info["last_user_msg"], "design haptic feedback")
        self.assertEqual(info["last_agent_msg"], "")
        self.assertEqual(info["status_tag"], titles.STATUS_NONE)
        self.assertEqual(info["completion_id"], "")


if __name__ == "__main__":
    unittest.main()
