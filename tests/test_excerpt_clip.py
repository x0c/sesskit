from __future__ import annotations

import unittest

from sesskit.models import make_session_info
from sesskit.titles import clip_user_excerpt, split_handoff_text


def _session(**overrides) -> dict:
    kwargs = dict(
        source="codex",
        id="handoff1",
        short_id="handoff1",
        cwd="/tmp/proj",
        mtime=1.0,
        time_source="file_mtime",
        event_time=1.0,
        file_mtime=1.0,
        size_bytes=4000,
        native_title=None,
        fallback_title="实现",
        status_tag="",
        path="/tmp/handoff1.jsonl",
    )
    kwargs.update(overrides)
    return make_session_info(**kwargs)


class HandoffExcerptClipTests(unittest.TestCase):
    def test_plain_text_still_truncates_to_300(self) -> None:
        session = _session(first_user_msg="a" * 400)
        self.assertEqual(len(session["first_user_msg"]), 300)
        self.assertTrue(session["first_user_msg"].startswith("aaa"))

    def test_handoff_clip_keeps_digest_not_wrapper_prefix(self) -> None:
        wrapper = (
            "Task: 实现\n\n"
            "You are picking up a session from Cursor. Start a new session of "
            "your own and continue the work. " + ("padding " * 40) + "\n\n"
            "Original session history file: /tmp/history.jsonl\n"
            "Original working directory: /tmp/proj\n"
            "History format hint: Codex rollout JSONL\n\n"
            "Below is a conversation excerpt automatically extracted from the "
            "original session (truncated; for quickly locating the task):\n"
            "User: 修复 Corral 测试失败\n"
            "Assistant: 开始改相关用例"
        )
        self.assertGreater(len(wrapper), 300)
        self.assertNotIn("修复 Corral 测试失败", wrapper[:300])

        inherited, digest = split_handoff_text(wrapper)
        self.assertEqual(inherited, "实现")
        self.assertIn("修复 Corral 测试失败", digest)
        self.assertNotIn("You are picking up", digest)

        clipped = clip_user_excerpt(wrapper)
        self.assertLessEqual(len(clipped), 300)
        self.assertIn("修复 Corral 测试失败", clipped)
        self.assertNotIn("You are picking up", clipped)

        session = _session(first_user_msg=wrapper, last_user_msg=wrapper)
        self.assertIn("修复 Corral 测试失败", session["first_user_msg"])
        self.assertNotIn("You are picking up", session["first_user_msg"])

    def test_nested_flattened_handoff_keeps_inner_task(self) -> None:
        nested = (
            "Task: 实现\n\n"
            "You are picking up a session from Cursor. Start a new session of "
            "your own and continue the work; this is not a native resume of the "
            "original session.\n\n"
            "Original session history file: /tmp/history.jsonl\n"
            "Original working directory: /tmp/proj\n"
            "History format hint: Codex rollout JSONL\n\n"
            "Below is a conversation excerpt automatically extracted from the "
            "original session (truncated; for quickly locating the task):\n"
            "[Original request]Task: 排查并根治 Corral CI 错误 You are picking up a "
            "session from Codex. Start a new session of your own and continue "
            "the work; this is not a native resume of the original session. "
            "Original session history file: /tmp/inner.jsonl\n"
            "[Recent conversation]\n"
            "Assistant: 开始改相关用例"
        )
        inherited, digest = split_handoff_text(nested)
        self.assertEqual(inherited, "排查并根治 Corral CI 错误")
        self.assertNotIn("You are picking up", inherited or "")
        self.assertNotIn("You are picking up", digest)

        clipped = clip_user_excerpt(nested)
        self.assertLessEqual(len(clipped), 300)
        self.assertIn("排查并根治 Corral CI 错误", clipped)
        self.assertNotIn("You are picking up", clipped)
        self.assertNotEqual(clipped[:20], "Task: 实现")

        session = _session(first_user_msg=nested)
        self.assertIn("排查并根治 Corral CI 错误", session["first_user_msg"])
        self.assertNotIn("You are picking up", session["first_user_msg"])


if __name__ == "__main__":
    unittest.main()
