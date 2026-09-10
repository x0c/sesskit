"""跨运行时 transcript：事件顺序、配对、过滤与真实格式字段不得错位。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sesskit.transcript import SCHEMA_ID, count_events, load_events


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _session(source: str, path: Path, session_id: str = "session-1") -> dict:
    return {"source": source, "path": str(path), "id": session_id}


def _types(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def _assert_seq(test: unittest.TestCase, events: list[dict]) -> None:
    test.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))


def _assert_paired(test: unittest.TestCase, events: list[dict]) -> None:
    seen: set[str] = set()
    for event in events:
        if event["type"] == "tool_call":
            call_id = event["id"]
            test.assertTrue(call_id, f"tool_call 缺 id: {event}")
            seen.add(call_id)
        elif event["type"] == "tool_result":
            call_id = event["call_id"]
            test.assertTrue(call_id, f"tool_result 缺 call_id: {event}")
            test.assertIn(call_id, seen, f"tool_result {call_id} 没有对应的前置 tool_call")


def _thinking_texts(events: list[dict]) -> list[str]:
    return [event["text"] for event in events if event["type"] == "thinking"]


def _assistant_texts(events: list[dict]) -> list[str]:
    return [event["text"] for event in events if event["type"] == "assistant_message"]


class TranscriptUtilityTests(unittest.TestCase):
    def test_unknown_runtime_and_missing_file_are_empty(self) -> None:
        self.assertEqual(load_events({"source": "unknown", "path": "/nope"}), [])
        self.assertEqual(load_events({"source": "claude", "path": "/no/such/file.jsonl"}), [])

    def test_count_events_covers_all_types(self) -> None:
        counts = count_events([
            {"type": "user_message"},
            {"type": "tool_call"},
            {"type": "tool_result"},
            {"type": "thinking"},
            {"type": "assistant_message"},
            {"type": "assistant_message"},
        ])
        self.assertEqual(counts["user_message"], 1)
        self.assertEqual(counts["assistant_message"], 2)
        self.assertEqual(counts["tool_call"], 1)
        self.assertEqual(SCHEMA_ID, "sesskit.transcript/v1")


class ClaudeTranscriptTests(unittest.TestCase):
    def test_thinking_text_tools_results_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(path, [
                {"type": "user", "origin": {"kind": "human"}, "timestamp": "2026-08-01T00:00:00Z",
                 "message": {"role": "user", "content": [{"type": "text", "text": "帮我改 bar.py"}]}},
                {"type": "user", "origin": {"kind": "task-notification"},
                 "message": {"role": "user", "content": [{"type": "text", "text": "系统注入"}]}},
                {"type": "assistant", "timestamp": "2026-08-01T00:00:01Z",
                 "message": {"stop_reason": "tool_use", "content": [
                     {"type": "thinking", "thinking": "先读文件"},
                     {"type": "text", "text": "我先看一下"},
                     {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/tmp/bar.py"}},
                 ]}},
                {"type": "user", "timestamp": "2026-08-01T00:00:02Z",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "t1", "content": "print(1)"},
                 ]}},
                {"type": "assistant", "isSidechain": True,
                 "message": {"content": [{"type": "text", "text": "子 agent 噪音"}]}},
                {"type": "assistant", "timestamp": "2026-08-01T00:00:03Z",
                 "message": {"stop_reason": "end_turn", "content": [
                     {"type": "text", "text": "改好了"},
                     {"type": "tool_use", "id": "t2", "name": "Bash",
                      "input": {"command": "python3 -m pytest"}},
                 ]}},
                {"type": "user", "timestamp": "2026-08-01T00:00:04Z",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                      "content": "exit code: 1\nfailed"},
                 ]}},
            ])
            events = load_events(_session("claude", path))

            self.assertEqual(_types(events), [
                "user_message", "thinking", "assistant_message", "tool_call",
                "tool_result", "assistant_message", "tool_call", "tool_result",
            ])
            _assert_seq(self, events)
            _assert_paired(self, events)
            self.assertEqual(events[0]["text"], "帮我改 bar.py")
            self.assertNotIn("系统注入", [e.get("text") for e in events])
            self.assertNotIn("子 agent 噪音", _assistant_texts(events))
            self.assertEqual(_thinking_texts(events), ["先读文件"])
            self.assertNotIn("先读文件", _assistant_texts(events))
            read_call = next(e for e in events if e.get("id") == "t1")
            self.assertEqual(read_call["name"], "Read")
            self.assertEqual(read_call["kind"], "read")
            self.assertEqual(read_call["input"]["file_path"], "/tmp/bar.py")
            bash_result = next(e for e in events if e.get("call_id") == "t2")
            self.assertEqual(bash_result["status"], "error")
            self.assertEqual(bash_result["output"], "exit code: 1\nfailed")


class CodexTranscriptTests(unittest.TestCase):
    def test_reasoning_tools_outputs_and_user_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(path, [
                {"type": "response_item", "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "帮我改一下"}],
                }},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "帮我改一下"}},
                {"type": "response_item", "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "准备打补丁"}],
                    "encrypted_content": "SKIP_ME",
                }},
                {"type": "response_item", "payload": {
                    "type": "function_call", "name": "apply_patch", "call_id": "edit-1",
                    "arguments": json.dumps({"path": "/proj/main.py"}),
                }},
                {"type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "edit-1", "output": "patched",
                }},
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call", "name": "shell", "call_id": "shell-1",
                    "input": 'tools.exec_command({"cmd":"npm test"})',
                }},
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call_output", "call_id": "shell-1",
                    "output": "exit code: 1\nfailed",
                }},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "已经改好"}},
            ])
            events = load_events(_session("codex", path))
            _assert_seq(self, events)
            _assert_paired(self, events)
            self.assertEqual(_types(events).count("user_message"), 1)
            self.assertEqual(events[0]["text"], "帮我改一下")
            self.assertEqual(_thinking_texts(events), ["准备打补丁"])
            self.assertNotIn("SKIP_ME", json.dumps(events))
            edit = next(e for e in events if e.get("id") == "edit-1")
            self.assertEqual(edit["kind"], "edit")
            self.assertEqual(edit["input"]["path"], "/proj/main.py")
            shell = next(e for e in events if e.get("id") == "shell-1")
            self.assertEqual(shell["input"]["cmd"], "npm test")
            self.assertEqual(next(e for e in events if e.get("call_id") == "shell-1")["status"], "error")
            self.assertEqual(_assistant_texts(events), ["已经改好"])


class KimiTranscriptTests(unittest.TestCase):
    def test_think_tool_args_result_note_and_origin_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            _write_jsonl(path, [
                {"type": "config.update", "systemPrompt": "You are Kimi " * 200, "time": 1000},
                {"type": "context.append_message", "time": 1_784_275_205_000,
                 "message": {"role": "user", "origin": {"kind": "user"},
                             "content": [{"type": "text", "text": "读一下指南"}]}},
                {"type": "context.append_message", "time": 1_784_275_205_100,
                 "message": {"role": "user", "origin": {"kind": "task-notification"},
                             "content": [{"type": "text", "text": "注入"}]}},
                {"type": "context.append_loop_event", "time": 1_784_275_206_000,
                 "event": {"type": "content.part", "part": {"type": "think", "think": "先 Read"}}},
                {"type": "context.append_loop_event", "time": 1_784_275_207_000,
                 "event": {"type": "tool.call", "toolCallId": "tool_1", "name": "Read",
                           "args": {"path": "/tmp/guide.md"}, "description": "Reading guide"}},
                {"type": "context.append_loop_event", "time": 1_784_275_208_000,
                 "event": {"type": "tool.result", "toolCallId": "tool_1",
                           "result": {"output": "# 指南\n正文", "note": "<system>1 line</system>"}}},
                {"type": "context.append_loop_event", "time": 1_784_275_209_000,
                 "event": {"type": "content.part", "part": {"type": "text", "text": "读完了"}}},
            ])
            events = load_events(_session("kimi", path))
            _assert_seq(self, events)
            _assert_paired(self, events)
            self.assertEqual(_types(events), [
                "user_message", "thinking", "tool_call", "tool_result", "assistant_message",
            ])
            self.assertNotIn("注入", [e.get("text") for e in events])
            call = next(e for e in events if e["type"] == "tool_call")
            self.assertEqual(call["input"]["path"], "/tmp/guide.md")
            self.assertEqual(call["kind"], "read")
            result = next(e for e in events if e["type"] == "tool_result")
            self.assertEqual(result["output"], "# 指南\n正文")
            self.assertEqual(result["note"], "<system>1 line</system>")
            self.assertEqual(events[0]["ts"], 1_784_275_205_000 / 1000)


class OpenCodeTranscriptTests(unittest.TestCase):
    def _db(self, path: Path, session_id: str = "ses_1") -> None:
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        conn.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            ("m_user", session_id, 100_000, 100_000, json.dumps({"role": "user", "time": {"created": 100_000}})),
        )
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            ("m_asst", session_id, 200_000, 200_000,
             json.dumps({"role": "assistant", "finish": "stop", "time": {"created": 200_000}})),
        )
        parts = [
            ("p_user", "m_user", session_id, 100_000, {"type": "text", "text": "跑一下测试"}),
            ("p_reason", "m_asst", session_id, 200_000, {"type": "reasoning", "text": "该用 bash"}),
            ("p_text", "m_asst", session_id, 200_001, {"type": "text", "text": "我来跑"}),
            ("p_syn", "m_asst", session_id, 200_002, {"type": "text", "text": "系统注入", "synthetic": True}),
            ("p_step", "m_asst", session_id, 200_003, {"type": "step-start"}),
            ("p_ok", "m_asst", session_id, 200_004, {
                "type": "tool", "tool": "bash", "callID": "c1",
                "state": {"status": "completed", "input": {"command": "pytest"}, "output": "passed"},
            }),
            ("p_err", "m_asst", session_id, 200_005, {
                "type": "tool", "tool": "bash", "callID": "c2",
                "state": {"status": "error", "input": {"command": "false"}, "error": "exit 1"},
            }),
        ]
        for part_id, message_id, sid, t, data in parts:
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?)",
                (part_id, message_id, sid, t, t, json.dumps(data)),
            )
        conn.commit()
        conn.close()

    def test_reasoning_tools_synthetic_and_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            self._db(path)
            events = load_events(_session("opencode", path, "ses_1"))
            _assert_seq(self, events)
            _assert_paired(self, events)
            self.assertEqual(_types(events), [
                "user_message", "thinking", "assistant_message",
                "tool_call", "tool_result", "tool_call", "tool_result",
            ])
            self.assertNotIn("系统注入", [e.get("text") for e in events])
            self.assertEqual(_thinking_texts(events), ["该用 bash"])
            ok = next(e for e in events if e.get("id") == "c1")
            self.assertEqual(ok["input"]["command"], "pytest")
            self.assertEqual(next(e for e in events if e.get("call_id") == "c1")["output"], "passed")
            self.assertEqual(next(e for e in events if e.get("call_id") == "c2")["status"], "error")
            self.assertEqual(next(e for e in events if e.get("call_id") == "c2")["output"], "exit 1")


class CursorTranscriptTests(unittest.TestCase):
    def test_reasoning_tool_call_result_and_user_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
            objects = [
                {"role": "user", "content": "<user_query>帮我改图标</user_query>"},
                {"role": "assistant", "content": [
                    {"type": "reasoning", "text": "要用 Write"},
                    {"type": "text", "text": "开始改"},
                    {"type": "tool-call", "toolCallId": "w1", "toolName": "Write",
                     "args": {"file_path": "/tmp/icon.svg"}},
                ]},
                {"role": "tool", "content": [
                    {"type": "tool-result", "toolCallId": "w1", "toolName": "Write", "result": "ok"},
                ]},
            ]
            for index, obj in enumerate(objects):
                conn.execute(
                    "INSERT INTO blobs VALUES (?, ?)",
                    (f"blob-{index:04d}", json.dumps(obj, ensure_ascii=False).encode()),
                )
            conn.commit()
            conn.close()
            events = load_events(_session("cursor", path))
            _assert_seq(self, events)
            _assert_paired(self, events)
            self.assertEqual(_types(events), [
                "user_message", "thinking", "assistant_message", "tool_call", "tool_result",
            ])
            self.assertEqual(events[0]["text"], "帮我改图标")
            self.assertEqual(_thinking_texts(events), ["要用 Write"])
            self.assertEqual(events[3]["kind"], "write")
            self.assertEqual(events[3]["input"]["file_path"], "/tmp/icon.svg")
            self.assertEqual(events[4]["output"], "ok")

    def test_result_before_call_in_rowid_still_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
            objects = [
                {"role": "tool", "content": [
                    {"type": "tool-result", "toolCallId": "late-1", "result": "ok"},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool-call", "toolCallId": "late-1", "toolName": "Read",
                     "args": {"path": "/tmp/a.py"}},
                ]},
            ]
            for index, obj in enumerate(objects):
                conn.execute(
                    "INSERT INTO blobs VALUES (?, ?)",
                    (f"blob-{index:04d}", json.dumps(obj).encode()),
                )
            conn.commit()
            conn.close()
            events = load_events(_session("cursor", path))
            self.assertEqual(_types(events), ["tool_call", "tool_result"])
            _assert_paired(self, events)
            self.assertEqual(events[0]["input"]["path"], "/tmp/a.py")
            self.assertEqual(events[1]["output"], "ok")


class PiTranscriptTests(unittest.TestCase):
    def test_active_branch_thinking_tools_and_old_branch_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            _write_jsonl(path, [
                {"type": "session", "id": "pi-demo", "timestamp": "2026-01-01T00:00:00Z"},
                {"type": "session_info", "id": "meta", "parentId": None, "timestamp": "2026-01-01T00:00:01Z"},
                {"type": "message", "id": "u1", "parentId": "meta", "timestamp": "2026-01-01T00:00:02Z",
                 "message": {"role": "user", "content": [{"type": "text", "text": "首个需求"}]}},
                {"type": "message", "id": "old", "parentId": "u1", "timestamp": "2026-01-01T00:00:03Z",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "旧分支"}]}},
                {"type": "message", "id": "new", "parentId": "u1", "timestamp": "2026-01-01T00:00:04Z",
                 "message": {"role": "assistant", "stopReason": "toolUse", "content": [
                     {"type": "thinking", "thinking": "该读文件"},
                     {"type": "text", "text": "当前分支"},
                     {"type": "toolCall", "id": "r1", "name": "read", "arguments": {"path": "/tmp/a.py"}},
                 ]}},
                {"type": "message", "id": "tr", "parentId": "new", "timestamp": "2026-01-01T00:00:05Z",
                 "message": {"role": "toolResult", "toolCallId": "r1", "content": "print(1)"}},
            ])
            events = load_events(_session("pi", path))
            _assert_seq(self, events)
            _assert_paired(self, events)
            self.assertEqual(_types(events), [
                "user_message", "thinking", "assistant_message", "tool_call", "tool_result",
            ])
            self.assertNotIn("旧分支", _assistant_texts(events))
            self.assertEqual(_thinking_texts(events), ["该读文件"])
            self.assertEqual(events[3]["input"]["path"], "/tmp/a.py")
            self.assertEqual(events[4]["output"], "print(1)")


class RealHistoryPairingTests(unittest.TestCase):
    """本机有真实历史时抽查：有 call_id 的 result 必须能对上前面的 call，thinking 不得混进正文。"""

    def _check(self, source: str, session: dict) -> None:
        events = load_events(session)
        if not events:
            self.skipTest(f"{source} 解析结果为空")
        _assert_seq(self, events)
        seen: set[str] = set()
        thinking = _thinking_texts(events)
        assistant = _assistant_texts(events)
        for event in events:
            self.assertIn(event["type"], {
                "user_message", "assistant_message", "thinking", "tool_call", "tool_result",
            })
            if event["type"] == "tool_call":
                if event["id"]:
                    seen.add(event["id"])
                self.assertIn("name", event)
                self.assertIn("kind", event)
                self.assertIn("input", event)
            elif event["type"] == "tool_result":
                if event["call_id"]:
                    self.assertIn(event["call_id"], seen)
                self.assertIn(event["status"], {"ok", "error", "running"})
        for text in thinking:
            self.assertNotIn(text, assistant)

    def test_real_claude(self) -> None:
        import glob
        files = sorted(
            glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")),
            key=os.path.getmtime,
            reverse=True,
        )
        if not files:
            self.skipTest("没有本机 Claude 历史")
        self._check("claude", _session("claude", Path(files[0])))

    def test_real_codex(self) -> None:
        import glob
        files = sorted(
            glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True),
            key=os.path.getmtime,
            reverse=True,
        )
        if not files:
            self.skipTest("没有本机 Codex 历史")
        self._check("codex", _session("codex", Path(files[0])))

    def test_real_kimi(self) -> None:
        import glob
        files = sorted(
            glob.glob(os.path.expanduser("~/.kimi-code/sessions/**/agents/main/wire.jsonl"), recursive=True),
            key=os.path.getmtime,
            reverse=True,
        )
        for path in files:
            events = load_events(_session("kimi", Path(path)))
            if events:
                self._check("kimi", _session("kimi", Path(path)))
                return
        self.skipTest("没有本机 Kimi 可解析历史")

    def test_real_opencode(self) -> None:
        db = os.path.expanduser("~/.local/share/opencode/opencode.db")
        if not os.path.isfile(db):
            self.skipTest("没有本机 OpenCode 历史")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT id FROM session WHERE parent_id IS NULL AND time_archived IS NULL "
                "ORDER BY time_updated DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            self.skipTest("OpenCode 库里没有顶层会话")
        self._check("opencode", {"source": "opencode", "path": db, "id": row[0]})

    def test_real_cursor(self) -> None:
        import glob
        files = sorted(
            glob.glob(os.path.expanduser("~/.cursor/chats/*/*/store.db")),
            key=os.path.getmtime,
            reverse=True,
        )
        if not files:
            self.skipTest("没有本机 Cursor CLI 历史")
        self._check("cursor", _session("cursor", Path(files[0])))

    def test_real_pi(self) -> None:
        import glob
        files = sorted(
            glob.glob(os.path.expanduser("~/.pi/agent/sessions/**/*.jsonl"), recursive=True),
            key=os.path.getmtime,
            reverse=True,
        )
        if not files:
            self.skipTest("没有本机 Pi 历史")
        self._check("pi", _session("pi", Path(files[0])))


if __name__ == "__main__":
    unittest.main()
