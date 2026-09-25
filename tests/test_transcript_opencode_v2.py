"""OpenCode transcript v2 事件分支：session_message → 统一事件流。

- v1 事件流（message/part）零变化：双表并存 id 永远走 v1，本文件锁一条回归。
- v2 专属会话：user→user_message；assistant 展开 content[]（text→正文，
  tool→tool_call(+completed/error 时 tool_result)，reasoning 跳过）；
  compaction summary→thinking；system/synthetic/idle 不出事件；
  无文本 error 轮以 error 文本占位。
- 活 fixture：ses_f27328fccffel7bo1jxQWKHW4c（v2-ok 成功路径）、
  ses_f2732ba39ffe38JgwN0Hb2959a（quota 中断路径，均 v2 专属）、
  ses_f2744ffbfffe8anSXlodU7KHjI（双表并存，走 v1 回归）。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sesskit.parsers import opencode as oc
from sesskit.transcript import load_events

LIVE_V2_OK = "ses_f27328fccffel7bo1jxQWKHW4c"
LIVE_V2_QUOTA = "ses_f2732ba39ffe38JgwN0Hb2959a"
LIVE_DUAL_V1 = "ses_f2744ffbfffe8anSXlodU7KHjI"


def _live_db() -> str | None:
    paths = oc._db_paths()
    return paths[0] if paths else None


def _session(session_id: str, path: str) -> dict:
    return {"source": "opencode", "path": path, "id": session_id}


def _types(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def _assert_seq(test: unittest.TestCase, events: list[dict]) -> None:
    test.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))


def _assert_paired(test: unittest.TestCase, events: list[dict]) -> None:
    seen: set[str] = set()
    for event in events:
        if event["type"] == "tool_call":
            test.assertTrue(event["id"], f"tool_call 缺 id: {event}")
            seen.add(event["id"])
        elif event["type"] == "tool_result":
            test.assertTrue(event["call_id"], f"tool_result 缺 call_id: {event}")
            test.assertIn(event["call_id"], seen)


class LiveV2EventsTests(unittest.TestCase):
    def setUp(self):
        db = _live_db()
        if db is None:
            self.skipTest("no live opencode.db on this machine")
        self.db = db

    def test_v2_ok_session_emits_user_then_assistant(self):
        events = load_events(_session(LIVE_V2_OK, self.db))
        self.assertEqual(_types(events), ["user_message", "assistant_message"])
        _assert_seq(self, events)
        self.assertEqual(events[0]["text"], '"Reply with exactly: v2-ok"')
        self.assertEqual(events[1]["text"], "v2-ok")
        self.assertEqual(events[0]["ts"], 1790343737429 / 1000)
        self.assertEqual(events[1]["ts"], 1790343737444 / 1000)
        # CONTRACT Verification：纯文本 user 必须能在 user_message 里找到。
        conv = oc.load_conversation(self.db, LIVE_V2_OK)
        user_texts = [e["text"] for e in events if e["type"] == "user_message"]
        for msg in conv:
            if msg.role == "user":
                self.assertIn(msg.text, user_texts)

    def test_v2_quota_session_keeps_error_turn(self):
        events = load_events(_session(LIVE_V2_QUOTA, self.db))
        self.assertEqual(_types(events), ["user_message", "assistant_message"])
        _assert_seq(self, events)
        assistants = [e["text"] for e in events if e["type"] == "assistant_message"]
        self.assertTrue(any("Go usage limit exceeded" in t for t in assistants))
        # 尾部 idle failed 不出对话事件，但 status_tag 仍是中断。
        info = next(s for s in oc.scan_sessions(limit=500) if s["id"] == LIVE_V2_QUOTA)
        self.assertEqual(info["status_tag"], "⚠️已中断")

    def test_dual_session_still_uses_v1_events(self):
        """双表并存 id 走 v1：v2 只有 11 行，v1 事件流必须远多于此且含工具事件。"""
        events = load_events(_session(LIVE_DUAL_V1, self.db))
        self.assertGreater(len(events), 11)
        _assert_seq(self, events)
        _assert_paired(self, events)
        self.assertEqual(events[0]["type"], "user_message")
        self.assertIn("swarm-manager", events[0]["text"])
        self.assertIn("tool_call", _types(events))
        self.assertIn("assistant_message", _types(events))


def _write_v2_db(path: Path, with_v1_shadow: bool = False) -> None:
    """纯 v2 库（无 session 表，探针回落可测）；with_v1_shadow 时加 v1 三表，
    其中 session 表含 ses_dual 且 message/part 另有一套正文，用于验证分发走 v1。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT NOT NULL,"
        " title TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,"
        " parent_id TEXT, time_archived INTEGER)"
    )
    conn.execute(
        "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
        " type TEXT NOT NULL, seq INTEGER NOT NULL, time_created INTEGER NOT NULL,"
        " time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
    )
    if with_v1_shadow:
        conn.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, directory TEXT, title TEXT,
                time_created INTEGER, time_updated INTEGER,
                parent_id TEXT, time_archived INTEGER);
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                data TEXT NOT NULL);
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
                data TEXT NOT NULL);
            """
        )
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
            ("ses_dual", "/repo/dual", "dual", 1000, 4000, None, None),
        )
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            ("dm1", "ses_dual", 1001, 1001, json.dumps({"role": "user"})),
        )
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            ("dp1", "dm1", "ses_dual", 1001, 1001,
             json.dumps({"type": "text", "text": "v1 wins"})),
        )
        conn.execute(
            "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
            ("ses_dual", "/repo/dual", "dual", 1000, 4000, None, None),
        )
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            ("dm1v2", "ses_dual", "user", 0, 1001, 1001,
             json.dumps({"time": {"created": 1001}, "text": "v2 shadow"})),
        )
        conn.commit()
        conn.close()
        return

    conn.execute(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
        ("ses_e2e", "/repo/e2e", "e2e", 1000, 4000, None, None),
    )
    rows = [
        ("u1", "ses_e2e", "user", 0, 1001,
         {"time": {"created": 1001}, "text": "run ls please"}),
        ("a1", "ses_e2e", "assistant", 1, 1002,
         {"time": {"created": 1002},
          "content": [
              {"type": "reasoning", "text": ""},
              {"type": "text", "text": "on it"},
              {"type": "tool", "id": "call_1", "name": "bash",
               "state": {"status": "completed", "input": {"command": "ls"},
                         "content": [{"type": "text", "text": "file.txt"}]}},
              {"type": "tool", "id": "call_2", "name": "edit",
               "state": {"status": "error", "input": {"path": "/x"},
                         "error": {"type": "tool.execution", "message": "boom"}}},
          ],
          "finish": "tool-calls"}),
        ("s1", "ses_e2e", "system", 2, 1003,
         {"text": "tools changed", "time": {"created": 1003}}),
        ("c1", "ses_e2e", "compaction", 3, 1004,
         {"status": "completed", "reason": "auto", "summary": "did stuff"}),
        ("i1", "ses_e2e", "idle", 4, 1005,
         {"time": {"created": 1005}, "outcome": "failed"}),
        ("a2", "ses_e2e", "assistant", 5, 1006,
         {"time": {"created": 1006}, "content": [],
          "error": {"type": "provider.quota", "message": "nope", "status": 429}}),
    ]
    for mid, sid, mtype, seq, ts, data in rows:
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (mid, sid, mtype, seq, ts, ts, json.dumps(data)),
        )
    conn.commit()
    conn.close()


class SyntheticV2EventsTests(unittest.TestCase):
    def test_tool_error_idle_compaction_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _write_v2_db(path)
            events = load_events(_session("ses_e2e", str(path)))
            _assert_seq(self, events)
            _assert_paired(self, events)
            self.assertEqual(_types(events), [
                "user_message", "assistant_message",
                "tool_call", "tool_result", "tool_call", "tool_result",
                "thinking", "assistant_message",
            ])
            # reasoning 空串绝不能出现；system/idle 不出事件。
            texts = [e.get("text") for e in events]
            self.assertNotIn("", texts)
            self.assertTrue(all("tools changed" not in str(t) for t in texts))
            # tool_call 映射：id/name/input。
            call1 = next(e for e in events if e.get("id") == "call_1")
            self.assertEqual(call1["name"], "bash")
            self.assertEqual(call1["input"], {"command": "ls"})
            self.assertEqual(call1["kind"], "shell")
            # completed 结果取 state.content[] 文本；error 结果取原始 state.error。
            res1 = next(e for e in events if e.get("call_id") == "call_1")
            self.assertEqual((res1["status"], res1["output"]), ("ok", "file.txt"))
            res2 = next(e for e in events if e.get("call_id") == "call_2")
            self.assertEqual(res2["status"], "error")
            self.assertEqual(res2["output"]["message"], "boom")
            # compaction summary 进 thinking；无文本 error 轮以 error 文本占位。
            self.assertEqual(
                [e["text"] for e in events if e["type"] == "thinking"], ["did stuff"]
            )
            self.assertEqual(events[-1]["text"], "429: nope")

    def test_dual_id_prefers_v1_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _write_v2_db(path, with_v1_shadow=True)
            events = load_events(_session("ses_dual", str(path)))
            self.assertEqual(
                [(e["type"], e.get("text")) for e in events],
                [("user_message", "v1 wins")],
            )


if __name__ == "__main__":
    unittest.main()
