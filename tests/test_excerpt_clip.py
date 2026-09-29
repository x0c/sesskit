from __future__ import annotations

import unittest

from sesskit.models import make_session_info
from sesskit.parsers.common import HostExtension, preprocess_excerpt
from sesskit.titles import clip_user_excerpt


def _session(**overrides) -> dict:
    kwargs = {
        "source": "codex",
        "id": "handoff1",
        "short_id": "handoff1",
        "cwd": "/tmp/proj",
        "mtime": 1.0,
        "time_source": "file_mtime",
        "event_time": 1.0,
        "file_mtime": 1.0,
        "size_bytes": 4000,
        "native_title": None,
        "fallback_title": "实现",
        "status_tag": "",
        "path": "/tmp/handoff1.jsonl",
    }
    kwargs.update(overrides)
    return make_session_info(**kwargs)


def _fake_peel(text: str) -> str:
    """Stand-in for a host's wrapper transform: drop ``WRAPPER:`` lines."""
    kept = [
        line for line in text.splitlines() if not line.startswith("WRAPPER:")
    ]
    return "\n".join(kept).strip()


class NeutralExcerptClipTests(unittest.TestCase):
    def test_plain_text_still_truncates_to_300(self) -> None:
        session = _session(first_user_msg="a" * 400)
        self.assertEqual(len(session["first_user_msg"]), 300)
        self.assertTrue(session["first_user_msg"].startswith("aaa"))

    def test_core_clip_is_host_neutral(self) -> None:
        wrapper = "WRAPPER: boilerplate intro\nTask: real request text"
        clipped = clip_user_excerpt(wrapper)
        # Core clipping only truncates; wrapper peeling belongs to the host.
        self.assertEqual(clipped, wrapper[:300])

    def test_host_preprocess_applies_before_clip(self) -> None:
        host = HostExtension(name="fake", excerpt_preprocess=_fake_peel)
        wrapper = "WRAPPER: boilerplate intro\nTask: real request text"
        self.assertEqual(
            preprocess_excerpt(wrapper, host), "Task: real request text"
        )
        self.assertEqual(preprocess_excerpt(wrapper, None), wrapper)

    def test_two_extensions_do_not_leak(self) -> None:
        host_a = HostExtension(name="a", excerpt_preprocess=_fake_peel)
        host_b = HostExtension(name="b")
        wrapper = "WRAPPER: boilerplate intro\nTask: real request text"
        self.assertEqual(
            preprocess_excerpt(wrapper, host_a), "Task: real request text"
        )
        self.assertEqual(preprocess_excerpt(wrapper, host_b), wrapper)
        self.assertEqual(preprocess_excerpt(wrapper, None), wrapper)


if __name__ == "__main__":
    unittest.main()
