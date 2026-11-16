"""Pi native skill expansion has one role-aware human-text interpretation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sesskit import load_events, load_session_conversation, open_activity_reader
from sesskit.activity import load_activity
from sesskit.parsers import pi


def _entry(entry_id: str, parent_id: str | None, role: str, text: str, second: int) -> dict:
    message: dict = {"role": role, "content": [{"type": "text", "text": text}]}
    if role == "assistant":
        message["stopReason"] = "stop"
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": f"2026-11-16T00:00:{second:02d}Z",
        "message": message,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _skill(name: str, arguments: str = "") -> str:
    return (
        f'<skill name="{name}" location="/tmp/{name}/SKILL.md">\n'
        f"References are relative to /tmp/{name}.\n\nInstruction prose\n</skill>"
        f"{arguments}"
    )


class PiSkillMessageTests(unittest.TestCase):
    def test_helper_unwraps_only_the_complete_native_user_shape(self) -> None:
        self.assertEqual(pi.user_message_text(_skill("swarm-manager", "\n\n修复标题")), "修复标题")
        self.assertEqual(pi.user_message_text(_skill("doc-compact")), "/skill:doc-compact")
        self.assertEqual(
            pi.user_message_text([{"type": "text", "text": _skill("doc-update", "\n继续")}]),
            "继续",
        )
        for text in (
            _skill("Uppercase", "\n任务"),
            _skill("double--hyphen", "\n任务"),
            '<skill name="doc-update">instructions</skill>',
            "<skill>not a native expansion</skill>",
        ):
            with self.subTest(text=text):
                self.assertEqual(pi.user_message_text(text), text)
        # The helper is role-aware: assistant prose that happens to have the
        # same complete shape stays assistant-authored content, not a command.
        assistant_example = _skill("doc-update", "\n示例")
        self.assertEqual(pi.message_text(assistant_example), assistant_example)

    def test_list_conversation_activity_and_v1_share_the_human_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "skill.jsonl"
            rows = [
                {"type": "session", "id": "skill-session", "timestamp": "2026-11-16T00:00:00Z", "cwd": directory},
                _entry("u1", None, "user", _skill("swarm-manager", "\n\n修复 skill 标题"), 1),
                _entry("a1", "u1", "assistant", "先处理", 2),
                _entry("u2", "a1", "user", _skill("doc-compact"), 3),
                _entry("a2", "u2", "assistant", "再处理", 4),
            ]
            _write(path, rows)
            built, _created = pi._build_session_info_from_entries(str(path), rows)
            self.assertEqual(built["first_user_msg"], "修复 skill 标题")
            self.assertEqual(built["fallback_title"], "修复 skill 标题")
            self.assertEqual(built["last_user_msg"], "/skill:doc-compact")

            session = {"source": "pi", "id": "skill-session", "path": str(path)}
            plain = load_session_conversation(session)
            self.assertEqual(
                [(message.role, message.text) for message in plain],
                [
                    ("user", "修复 skill 标题"),
                    ("assistant", "先处理"),
                    ("user", "/skill:doc-compact"),
                    ("assistant", "再处理"),
                ],
            )
            activity = load_activity(session)
            self.assertEqual(
                [event.text for event in activity.events if event.type == "user_message"],
                ["修复 skill 标题", "/skill:doc-compact"],
            )
            self.assertEqual(
                [event["text"] for event in load_events(session) if event["type"] == "user_message"],
                ["修复 skill 标题", "/skill:doc-compact"],
            )

    def test_incremental_reader_uses_the_same_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "skill-live.jsonl"
            _write(path, [
                {"type": "session", "id": "skill-live", "timestamp": "2026-11-16T00:00:00Z", "cwd": directory},
                _entry("u1", None, "user", _skill("swarm-manager", "\n\n第一条任务"), 1),
                _entry("a1", "u1", "assistant", "收到", 2),
            ])
            session = {"source": "pi", "id": "skill-live", "path": str(path)}
            reader = open_activity_reader(session)
            initial = reader.poll()
            self.assertEqual(
                [event.text for event in initial.events if event.type == "user_message"],
                ["第一条任务"],
            )
            with path.open("a", encoding="utf-8") as handle:
                for row in (
                    _entry("u2", "a1", "user", _skill("doc-update"), 3),
                    _entry("a2", "u2", "assistant", "继续", 4),
                ):
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            appended = reader.poll()
            self.assertEqual(
                [event.text for event in appended.events if event.type == "user_message"],
                ["/skill:doc-update"],
            )


if __name__ == "__main__":
    unittest.main()
