"""OpenCode v2 双表分支：session_v2 / session_message 解析回归。

约定（只读实测契约，库只读打开绝不写库）：
- v2 成功路径 live fixture：ses_f27328fccffel7bo1jxQWKHW4c
  （标题 Exact v2-ok reply request，用户原文 '"Reply with exactly: v2-ok"'，
  助手 stop 回复 v2-ok，尾部 idle/system 必须跳过）。
- v2 异常路径 live fixture：ses_f2732ba39ffe38JgwN0Hb2959a
  （空标题 + quota error + idle failed → ABORTED，error 文本占位 last_agent）。
- 工具形态辅 fixture：ses_f2744ffbfffe8anSXlodU7KHjI（双表并存的迁移行，
  必须继续走 v1 逻辑，行为零变化）。
- 合成 fixture（本文件自建临时库）覆盖 error / idle-failed / 空标题 /
  缺 finish / tool+reasoning 排除 / seq 跳号排序 / 删除链。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from sesskit import titles
from sesskit import transcript as transcript_mod
from sesskit.parsers import opencode as oc

LIVE_V2_OK = "ses_f27328fccffel7bo1jxQWKHW4c"
LIVE_V2_QUOTA = "ses_f2732ba39ffe38JgwN0Hb2959a"
LIVE_DUAL_V1 = "ses_f2744ffbfffe8anSXlodU7KHjI"


def _live_db() -> str | None:
    paths = oc._db_paths()
    return paths[0] if paths else None


def _scan_by_id(sessions: list, session_id: str) -> dict | None:
    return next((s for s in sessions if s.get("id") == session_id), None)


class LiveV2Tests(unittest.TestCase):
    """细粒度 live 检查：必须跑在真实会话 id 上，断言 status_tag、
    last_agent_msg、load_conversation（对话事件序列）。"""

    def setUp(self):
        db = _live_db()
        if db is None:
            self.skipTest("no live opencode.db on this machine")
        self.db = db

    def test_v2_success_status_last_agent_and_conversation(self):
        sessions = oc.scan_sessions(limit=500)
        info = _scan_by_id(sessions, LIVE_V2_OK)
        self.assertIsNotNone(info, "v2 success session must be listed")
        assert info is not None
        self.assertEqual(info["status_tag"], titles.STATUS_DONE)
        self.assertEqual(info["last_agent_msg"], "v2-ok")
        self.assertEqual(info["native_title"], "Exact v2-ok reply request")
        self.assertEqual(info["cwd"], "/Users/geraltgraham/Codes")
        self.assertGreater(info["size_bytes"], 0)
        self.assertTrue(info["completion_id"])

        conv = oc.load_conversation(self.db, LIVE_V2_OK)
        self.assertEqual(
            [(m.role, m.text) for m in conv],
            [("user", '"Reply with exactly: v2-ok"'), ("assistant", "v2-ok")],
        )

    def test_v2_quota_error_is_aborted_with_error_text(self):
        sessions = oc.scan_sessions(limit=500)
        info = _scan_by_id(sessions, LIVE_V2_QUOTA)
        self.assertIsNotNone(info, "v2 quota session must be listed")
        assert info is not None
        # 空标题是正常态：有用户正文就不得丢弃，走 fallback。
        self.assertIsNone(info["native_title"])
        self.assertIn("Reply with exactly", info["fallback_title"])
        self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
        self.assertIn("Go usage limit exceeded", info["last_agent_msg"])

        conv = oc.load_conversation(self.db, LIVE_V2_QUOTA)
        self.assertEqual(len(conv), 2)
        self.assertEqual(conv[0].role, "user")
        self.assertEqual(conv[1].role, "assistant")
        self.assertIn("Go usage limit exceeded", conv[1].text)

    def test_dual_table_session_still_served_by_v1(self):
        """双表并存的迁移行走 v1：v1 有 67 条 message，v2 只有 11 条；
        走 v1 时对话轮数远多于 v2 行数，且 scan 行为保持 v1 口径。"""
        sessions = oc.scan_sessions(limit=500)
        info = _scan_by_id(sessions, LIVE_DUAL_V1)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["native_title"], "swarm-manager 代码审查优化")

        conv = oc.load_conversation(self.db, LIVE_DUAL_V1)
        self.assertGreater(len(conv), 11, "v1 path must yield more turns than v2 rows")
        self.assertEqual(conv[0].role, "user")
        self.assertIn("swarm-manager", conv[0].text)
        self.assertTrue(any(m.role == "assistant" for m in conv))
        # v1 扫描口径：size 来自 part 表（几十万字节量级），不是 v2 的行长求和。
        self.assertGreater(info["size_bytes"], 100000)

        # v1 transcript 路径回归：load_events 仍从 message/part 读到事件，
        # 首事件是用户行（transcript.py 本次未动，此断言锁住既有行为）。
        events = transcript_mod.load_events(dict(info))
        self.assertGreater(len(events), 0)
        user_texts = [
            e.get("text", "") for e in events if e.get("type") == "user_message"
        ]
        self.assertTrue(any("swarm-manager" in t for t in user_texts))


# --- 合成 fixture：自建临时 opencode.db --------------------------------------
#
# v1 session 表留空以外的迁移行只放一行（dual），验证 NOT EXISTS 去重与
# 删除/加载分发；其余全部是 v2 专属会话。

_V2_FIXTURE_SESSIONS = [
    # (id, directory, title, created_ms, updated_ms)
    ("ses_ok", "/repo/ok", "ok title", 1000, 4000),
    ("ses_toolcalls", "/repo/tool", "tool title", 1000, 4000),
    ("ses_err", "/repo/err", "err title", 1000, 4000),
    ("ses_idle_fail", "/repo/idle", "idle title", 1000, 4000),
    ("ses_idle_ok", "/repo/idleok", "idle ok title", 1000, 4000),
    ("ses_nofinish", "/repo/nof", "nofinish title", 1000, 4000),
    ("ses_emptytitle", "/repo/empty", None, 1000, 4000),
    ("ses_notitle_nouser", "/repo/drop", None, 1000, 4000),
    ("ses_gap", "/repo/gap", "gap title", 1000, 4000),
]

_V2_FIXTURE_MESSAGES = [
    # 成功：user → assistant(stop, reasoning 空串 + text) → idle/system 尾巴
    ("ses_ok", "m1", "user", 4, 1001, {"time": {"created": 1001}, "text": "do the thing"}),
    (
        "ses_ok",
        "m2",
        "assistant",
        5,
        1002,
        {
            "time": {"created": 1002},
            "content": [
                {"type": "reasoning", "text": ""},
                {"type": "text", "text": "done it"},
            ],
            "finish": "stop",
        },
    ),
    ("ses_ok", "m3", "idle", 12, 1003, {"time": {"created": 1003}, "outcome": "succeeded"}),
    ("ses_ok", "m4", "system", 13, 1004, {"text": "tools changed", "time": {"created": 1004}}),
    # tool-calls：缺 finish→NONE；tool 内联结果不得混入正文
    ("ses_toolcalls", "m1", "user", 0, 1001, {"time": {"created": 1001}, "text": "run it"}),
    (
        "ses_toolcalls",
        "m2",
        "assistant",
        1,
        1002,
        {
            "time": {"created": 1002},
            "content": [
                {"type": "reasoning", "text": ""},
                {"type": "text", "text": "working"},
                {
                    "type": "tool",
                    "id": "call_1",
                    "name": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls"},
                        "content": [{"type": "text", "text": "SECRET-TOOL-OUTPUT"}],
                    },
                },
            ],
            "finish": "tool-calls",
        },
    ),
    # error：0 文本 + v2 error 形态 → ABORTED，error 文本占位
    ("ses_err", "m1", "user", 0, 1001, {"time": {"created": 1001}, "text": "hi"}),
    (
        "ses_err",
        "m2",
        "assistant",
        1,
        1002,
        {
            "time": {"created": 1002},
            "content": [],
            "error": {"type": "provider.quota", "message": "out of quota", "status": 429},
        },
    ),
    # user 尾 + idle failed → ABORTED（异常信号翻转 PENDING）
    ("ses_idle_fail", "m1", "user", 0, 1001, {"time": {"created": 1001}, "text": "wait"}),
    ("ses_idle_fail", "m2", "idle", 1, 1002, {"time": {"created": 1002}, "outcome": "failed"}),
    # stop + idle succeeded → DONE（bookkeeping 不推翻成功轮）
    ("ses_idle_ok", "m1", "user", 0, 1001, {"time": {"created": 1001}, "text": "q"}),
    (
        "ses_idle_ok",
        "m2",
        "assistant",
        1,
        1002,
        {"time": {"created": 1002}, "content": [{"type": "text", "text": "a"}], "finish": "stop"},
    ),
    ("ses_idle_ok", "m3", "idle", 2, 1003, {"time": {"created": 1003}, "outcome": "succeeded"}),
    # finish 缺失 → NONE 不硬判
    ("ses_nofinish", "m1", "user", 0, 1001, {"time": {"created": 1001}, "text": "q"}),
    (
        "ses_nofinish",
        "m2",
        "assistant",
        1,
        1002,
        {"time": {"created": 1002}, "content": [{"type": "text", "text": "maybe"}]},
    ),
    # 空标题 + 有用户正文 → 保留，走 fallback
    ("ses_emptytitle", "m1", "user", 0, 1001, {"time": {"created": 1001}, "text": "fallback me please"}),
    # 无标题 + 无用户正文（只有 compaction）→ 丢弃
    (
        "ses_notitle_nouser",
        "m1",
        "compaction",
        0,
        1001,
        {"status": "completed", "reason": "auto", "summary": "noise"},
    ),
    # seq 跳号排序：4,5,12
    ("ses_gap", "m1", "user", 4, 1001, {"time": {"created": 1001}, "text": "first"}),
    (
        "ses_gap",
        "m2",
        "assistant",
        5,
        1002,
        {"time": {"created": 1002}, "content": [{"type": "text", "text": "second"}], "finish": "stop"},
    ),
    ("ses_gap", "m3", "idle", 12, 1003, {"time": {"created": 1003}, "outcome": "succeeded"}),
]


def _build_fixture_db(path: str) -> None:
    conn = sqlite3.connect(path)
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
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, directory TEXT NOT NULL, title TEXT,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            parent_id TEXT, time_archived INTEGER);
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
            seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE UNIQUE INDEX session_message_session_seq_idx
            ON session_message (session_id, seq);
        CREATE TABLE instruction_entry (session_id TEXT, key TEXT);
        CREATE TABLE instruction_state (session_id TEXT, epoch_start INTEGER);
        CREATE TABLE session_pending (id TEXT, session_id TEXT);
        CREATE TABLE session_inbox (id TEXT, session_id TEXT);
        """
    )
    # 双表并存的迁移行：v1/v2 各一份，v2 分支必须让路（NOT EXISTS 去重）。
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
        (
            "dp1",
            "dm1",
            "ses_dual",
            1001,
            1001,
            json.dumps({"type": "text", "text": "v1 wins"}),
        ),
    )
    conn.execute(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
        ("ses_dual", "/repo/dual", "dual", 1000, 4000, None, None),
    )
    conn.execute(
        "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
        (
            "dm1v2",
            "ses_dual",
            "user",
            0,
            1001,
            1001,
            json.dumps({"time": {"created": 1001}, "text": "v2 shadow"}),
        ),
    )
    for sid, directory, title, created, updated in _V2_FIXTURE_SESSIONS:
        conn.execute(
            "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
            (sid, directory, title, created, updated, None, None),
        )
    for sid, mid, mtype, seq, ts, data in _V2_FIXTURE_MESSAGES:
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (f"{sid}#{mid}", sid, mtype, seq, ts, ts, json.dumps(data)),
        )
    conn.commit()
    conn.close()


class SyntheticV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "opencode.db")
        _build_fixture_db(self.db_path)
        self.old_env = os.environ.get("OPENCODE_DATA_DIR")
        os.environ["OPENCODE_DATA_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self.old_env is None:
            os.environ.pop("OPENCODE_DATA_DIR", None)
        else:
            os.environ["OPENCODE_DATA_DIR"] = self.old_env

    def _by_id(self, session_id: str) -> dict:
        sessions = oc.scan_sessions(limit=50)
        info = _scan_by_id(sessions, session_id)
        self.assertIsNotNone(info, f"{session_id} must be listed")
        assert info is not None
        return info

    def test_stop_with_trailing_idle_and_system_is_done(self):
        info = self._by_id("ses_ok")
        self.assertEqual(info["status_tag"], titles.STATUS_DONE)
        self.assertEqual(info["last_agent_msg"], "done it")
        self.assertEqual(info["first_user_msg"], "do the thing")
        self.assertTrue(info["completion_id"])
        conv = oc.load_conversation(self.db_path, "ses_ok")
        self.assertEqual(
            [(m.role, m.text) for m in conv],
            [("user", "do the thing"), ("assistant", "done it")],
        )

    def test_tool_calls_is_none_and_tool_output_excluded(self):
        info = self._by_id("ses_toolcalls")
        self.assertEqual(info["status_tag"], titles.STATUS_NONE)
        self.assertEqual(info["completion_id"], "")
        conv = oc.load_conversation(self.db_path, "ses_toolcalls")
        self.assertEqual(len(conv), 2)
        self.assertEqual(conv[1].text, "working")
        self.assertNotIn("SECRET-TOOL-OUTPUT", conv[1].text)

    def test_error_without_text_is_aborted(self):
        info = self._by_id("ses_err")
        self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)
        self.assertEqual(info["last_agent_msg"], "429: out of quota")
        conv = oc.load_conversation(self.db_path, "ses_err")
        self.assertEqual(
            [(m.role, m.text) for m in conv],
            [("user", "hi"), ("assistant", "429: out of quota")],
        )

    def test_idle_failed_flips_pending_to_aborted(self):
        info = self._by_id("ses_idle_fail")
        self.assertEqual(info["status_tag"], titles.STATUS_ABORTED)

    def test_idle_succeeded_keeps_done(self):
        info = self._by_id("ses_idle_ok")
        self.assertEqual(info["status_tag"], titles.STATUS_DONE)

    def test_missing_finish_is_none(self):
        info = self._by_id("ses_nofinish")
        self.assertEqual(info["status_tag"], titles.STATUS_NONE)
        self.assertEqual(info["completion_id"], "")

    def test_empty_title_with_user_text_is_kept(self):
        info = self._by_id("ses_emptytitle")
        self.assertIsNone(info["native_title"])
        self.assertEqual(info["fallback_title"], "fallback me please")

    def test_no_title_no_user_text_is_dropped(self):
        sessions = oc.scan_sessions(limit=50)
        self.assertIsNone(_scan_by_id(sessions, "ses_notitle_nouser"))

    def test_seq_gaps_order_by_seq(self):
        conv = oc.load_conversation(self.db_path, "ses_gap")
        self.assertEqual(
            [(m.role, m.text) for m in conv],
            [("user", "first"), ("assistant", "second")],
        )

    def test_dual_id_prefers_v1(self):
        """迁移行不出现在 v2 扫描里（NOT EXISTS 去重），加载走 v1 part。"""
        sessions = oc.scan_sessions(limit=50)
        matches = [s for s in sessions if s["id"] == "ses_dual"]
        self.assertEqual(len(matches), 1)
        conv = oc.load_conversation(self.db_path, "ses_dual")
        self.assertEqual([(m.role, m.text) for m in conv], [("user", "v1 wins")])

    def test_size_is_sum_of_message_lengths(self):
        conn = sqlite3.connect(self.db_path)
        try:
            expected = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM session_message"
                " WHERE session_id = ?",
                ("ses_ok",),
            ).fetchone()[0]
        finally:
            conn.close()
        info = self._by_id("ses_ok")
        self.assertEqual(info["size_bytes"], expected)
        self.assertGreater(expected, 0)

    def test_delete_v2_chain(self):
        oc.delete_session(self.db_path, "ses_err")
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM session_message WHERE session_id = ?",
                    ("ses_err",),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM session_v2 WHERE id = ?", ("ses_err",)
                ).fetchone()[0],
                0,
            )
            # v1 行与其他 v2 会话不受影响。
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM message").fetchone()[0], 1
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM session_v2 WHERE id = ?", ("ses_ok",)
                ).fetchone()
            )
        finally:
            conn.close()
        sessions = oc.scan_sessions(limit=50)
        self.assertIsNone(_scan_by_id(sessions, "ses_err"))

    def test_delete_dual_id_uses_v1_path(self):
        oc.delete_session(self.db_path, "ses_dual")
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM message").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM session WHERE id = ?", ("ses_dual",)
                ).fetchone()[0],
                0,
            )
            # v2 影子行保留（v1 路径只删 v1 三表，与原有行为一致）。
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM session_v2 WHERE id = ?", ("ses_dual",)
                ).fetchone()
            )
        finally:
            conn.close()


class ErrorShapeTests(unittest.TestCase):
    def test_v1_shape_unchanged(self):
        self.assertEqual(
            oc.error_text_from_msg(
                {"error": {"name": "APIError", "data": {"message": "boom", "statusCode": 404}}}
            ),
            "404: boom",
        )
        self.assertEqual(
            oc.error_text_from_msg(
                {"error": {"name": "MessageAbortedError", "data": {"message": "The operation was aborted."}}}
            ),
            "The operation was aborted.",
        )

    def test_v2_shape_with_status(self):
        self.assertEqual(
            oc.error_text_from_msg(
                {"error": {"type": "provider.quota", "message": "Go usage limit exceeded", "status": 429}}
            ),
            "429: Go usage limit exceeded",
        )
        self.assertEqual(
            oc.error_text_from_msg({"error": {"type": "aborted", "message": "Aborted"}}),
            "Aborted",
        )


if __name__ == "__main__":
    unittest.main()
