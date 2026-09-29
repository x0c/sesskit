"""Long tool-heavy Claude turns: list excerpt backfills, status stays on the 64 KB tail."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sesskit import titles
from sesskit.parsers import claude


def _line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


class ExcerptBackfillTests(unittest.TestCase):
    def test_tool_only_tail_backfills_excerpt_without_false_done(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "0d633b5b-1ec5-40f5-908b-f7c95efbb789.jsonl"
            lines = [
                _line({"type": "user", "cwd": td, "timestamp": "2026-09-29T03:40:37Z",
                       "message": {"role": "user", "content": "make a web panel"}}),
                _line({"type": "assistant", "timestamp": "2026-09-29T03:40:40Z",
                       "message": {"content": [{"type": "text", "text": "Looking at the board first."}]}}),
            ]
            blob = "x" * 4000
            for i in range(60):  # ~240 KB of tool traffic, no text
                lines.append(_line({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "ls"}}]}}))
                lines.append(_line({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": blob}]}}))
            path.write_text("".join(lines))
            info = claude._build_session_info(str(path), "proj")
        self.assertEqual(info["last_agent_msg"], "Looking at the board first.")
        self.assertEqual(info["last_user_msg"], "make a web panel")
        self.assertEqual(info["status_tag"], titles.STATUS_NONE)
        self.assertEqual(info["completion_id"], "")


if __name__ == "__main__":
    unittest.main()
