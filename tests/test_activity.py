"""Pi 先导加法活动 API：状态、证据、结局与 v1 投影对等。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sesskit.activity import load_activity, to_v1_dicts
from sesskit.transcript import load_events


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _session(path: Path, session_id: str = "pi-pilot") -> dict:
    return {"source": "pi", "path": str(path), "id": session_id}


def _entry(entry_id: str | None, parent_id: str | None, role: str,
           content: object, timestamp: str, **message_fields: object) -> dict:
    message = {"role": role, "content": content, **message_fields}
    item: dict = {"type": "message", "timestamp": timestamp, "message": message}
    if entry_id is not None:
        item["id"] = entry_id
    if parent_id is not None:
        item["parentId"] = parent_id
    return item


def _header(session_id: str = "pi-pilot") -> dict:
    return {"type": "session", "id": session_id,
            "timestamp": "2026-09-01T00:00:00Z", "cwd": "/tmp/pi-activity-fixture"}


def _full_flow(path: Path) -> None:
    _write_jsonl(path, [
        _header(),
        _entry("u1", None, "user", [{"type": "text", "text": "Run checks."}],
               "2026-09-01T00:00:01Z"),
        _entry("a1", "u1", "assistant", [
            {"type": "text", "text": "Running both checks."},
            {"type": "toolCall", "id": "check-ok", "name": "bash",
             "arguments": {"command": "check-ok"}},
            {"type": "toolCall", "id": "check-fail", "name": "bash",
             "arguments": {"command": "check-fail"}},
        ], "2026-09-01T00:00:02Z"),
        _entry("tr-ok", "a1", "toolResult", [{"type": "text", "text": "all passed"}],
               "2026-09-01T00:00:03Z", toolCallId="check-ok", toolName="bash",
               isError=False),
        _entry("tr-fail", "tr-ok", "toolResult", [{"type": "text", "text": "boom"}],
               "2026-09-01T00:00:04Z", toolCallId="check-fail", toolName="bash",
               isError=True),
    ])


class PiActivityTests(unittest.TestCase):
    def test_full_flow_states_outcomes_and_v1_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _full_flow(path)
            snapshot = load_activity(_session(path))

            self.assertEqual(snapshot.state, "available")
            self.assertEqual(snapshot.cursor, None)
            self.assertEqual(snapshot.generation, None)
            self.assertEqual([e.seq for e in snapshot.events], [1, 2, 3, 4, 5, 6])
            self.assertEqual(
                [e.type for e in snapshot.events],
                ["user_message", "assistant_message", "tool_call",
                 "tool_call", "tool_result", "tool_result"],
            )
            # 同一原生消息的事件共用 message_id。
            self.assertEqual(snapshot.events[1].message_id, snapshot.events[2].message_id)
            self.assertEqual(snapshot.events[1].message_id, snapshot.events[3].message_id)
            # 工具调用保留原生名与调用 id，结果按 call_id 配对。
            self.assertEqual(snapshot.events[2].name, "bash")
            self.assertEqual(snapshot.events[2].call_id, "check-ok")
            self.assertEqual(snapshot.events[4].call_id, "check-ok")
            self.assertEqual(snapshot.events[2].raw_input, {"command": "check-ok"})
            self.assertIsNone(snapshot.events[2].result)
            self.assertIsNone(snapshot.events[2].interaction)
            # 原生 isError 标记为 native 证据。
            ok_result = snapshot.events[4].result
            fail_result = snapshot.events[5].result
            assert ok_result is not None and fail_result is not None
            self.assertEqual((ok_result.status, ok_result.evidence.origin), ("ok", "native"))
            self.assertEqual((fail_result.status, fail_result.evidence.origin), ("error", "native"))
            # 尾部是工具结果、归属助手轮：done（尾部推断）。
            self.assertEqual(snapshot.outcome.status, "done")
            self.assertEqual(snapshot.outcome.evidence.origin, "inferred")
            self.assertIsNone(snapshot.outcome.error)
            # v1 投影与现有 load_events 完全一致。
            self.assertEqual(to_v1_dicts(snapshot), load_events(_session(path)))

    def test_error_only_tail_is_aborted_with_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "Continue."}],
                       "2026-09-01T00:00:01Z"),
                _entry("a-err", "u1", "assistant", [], "2026-09-01T00:00:02Z",
                       stopReason="error", errorMessage="Provider unavailable"),
            ])
            snapshot = load_activity(_session(path))

            self.assertEqual(snapshot.state, "available")
            self.assertEqual(snapshot.outcome.status, "aborted")
            self.assertEqual(snapshot.outcome.evidence.origin, "native")
            assert snapshot.outcome.error is not None
            self.assertEqual(snapshot.outcome.error.message, "Provider unavailable")
            self.assertIsNone(snapshot.outcome.error.code)
            error_event = snapshot.events[-1]
            self.assertEqual(error_event.type, "assistant_message")
            self.assertEqual(error_event.text, "Provider unavailable")
            assert error_event.error is not None
            self.assertEqual(error_event.error.message, "Provider unavailable")
            self.assertEqual(to_v1_dicts(snapshot), load_events(_session(path)))

    def test_user_tail_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "What next?"}],
                       "2026-09-01T00:00:01Z"),
            ])
            snapshot = load_activity(_session(path))

            self.assertEqual(snapshot.state, "available")
            self.assertEqual(snapshot.outcome.status, "pending")
            self.assertIsNone(snapshot.outcome.error)

    def test_empty_history_is_empty_not_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [_header()])
            snapshot = load_activity(_session(path))

            self.assertEqual(snapshot.state, "empty")
            self.assertEqual(snapshot.events, ())
            self.assertEqual(snapshot.outcome.status, "unknown")

    def test_missing_file_is_unavailable(self) -> None:
        snapshot = load_activity(
            {"source": "pi", "path": "/no/such/pi-session.jsonl", "id": "missing"})
        self.assertEqual(snapshot.state, "unavailable")
        self.assertEqual(snapshot.events, ())
        self.assertEqual(snapshot.outcome.status, "unknown")

    def test_other_runtimes_and_unknown_source_are_unsupported(self) -> None:
        for session in (
            {"source": "cursor", "path": "/tmp/x.jsonl", "id": "x"},
            {"source": "unknown", "path": "/tmp/x.jsonl", "id": "x"},
        ):
            snapshot = load_activity(session)
            self.assertEqual(snapshot.state, "unsupported")
            self.assertEqual(snapshot.events, ())
            self.assertEqual(snapshot.outcome.status, "unknown")

    def test_question_shaped_extension_has_no_synthetic_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "Choose."}],
                       "2026-09-01T00:00:01Z"),
                _entry("a1", "u1", "assistant", [
                    {"type": "toolCall", "id": "q-1", "name": "question",
                     "arguments": {"question": "Which?", "options": ["A", "B"]}},
                ], "2026-09-01T00:00:02Z"),
            ])
            snapshot = load_activity(_session(path))

            call = next(e for e in snapshot.events if e.type == "tool_call")
            self.assertEqual(call.raw_input, {"question": "Which?", "options": ["A", "B"]})
            self.assertIsNone(call.interaction)
            self.assertTrue(all(e.interaction is None for e in snapshot.events))
            self.assertEqual(to_v1_dicts(snapshot), load_events(_session(path)))

    def test_result_without_flag_or_output_is_unknown_but_projects_legacy_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "Go."}],
                       "2026-09-01T00:00:01Z"),
                _entry("a1", "u1", "assistant", [
                    {"type": "toolCall", "id": "t-1", "name": "bash",
                     "arguments": {"command": "true"}},
                ], "2026-09-01T00:00:02Z"),
                _entry("tr-1", "a1", "toolResult", [], "2026-09-01T00:00:03Z",
                       toolCallId="t-1", toolName="bash"),
            ])
            snapshot = load_activity(_session(path))

            result = next(e for e in snapshot.events if e.type == "tool_result").result
            assert result is not None
            self.assertEqual(result.status, "unknown")
            projected = next(e for e in to_v1_dicts(snapshot) if e["type"] == "tool_result")
            legacy = next(e for e in load_events(_session(path)) if e["type"] == "tool_result")
            self.assertEqual(projected["status"], "ok")
            self.assertEqual(projected, legacy)

    def test_abandoned_branch_is_not_projected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "Start."}],
                       "2026-09-01T00:00:01Z"),
                _entry("a-old", "u1", "assistant", [{"type": "text", "text": "Abandoned."}],
                       "2026-09-01T00:00:02Z"),
                _entry("u-new", "u1", "user", [{"type": "text", "text": "Continue."}],
                       "2026-09-01T00:00:03Z"),
                _entry("a-new", "u-new", "assistant", [{"type": "text", "text": "Active."}],
                       "2026-09-01T00:00:04Z"),
            ])
            snapshot = load_activity(_session(path))

            texts = [e.text for e in snapshot.events]
            self.assertNotIn("Abandoned.", texts)
            self.assertEqual(to_v1_dicts(snapshot), load_events(_session(path)))

    def test_tool_call_without_result_tail_is_unknown_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write_jsonl(path, [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "Run it."}],
                       "2026-09-01T00:00:01Z"),
                _entry("a1", "u1", "assistant", [
                    {"type": "toolCall", "id": "t-1", "name": "bash",
                     "arguments": {"command": "sleep 60"}},
                ], "2026-09-01T00:00:02Z"),
            ])
            snapshot = load_activity(_session(path))

            self.assertEqual(snapshot.state, "available")
            self.assertTrue(any(e.type == "tool_call" for e in snapshot.events))
            self.assertFalse(any(e.type == "tool_result" for e in snapshot.events))
            self.assertEqual(snapshot.outcome.status, "unknown")
            self.assertEqual(to_v1_dicts(snapshot), load_events(_session(path)))

    def test_real_pi_histories_keep_v1_parity(self) -> None:
        from sesskit.parsers import pi as scan_pi

        sessions = scan_pi.scan_sessions(limit=50)
        if not sessions:
            self.skipTest("没有本机 Pi 历史")
        checked = 0
        for session in sessions:
            payload = dict(session)
            snapshot = load_activity(payload)
            self.assertIn(snapshot.state, {"available", "empty"})
            if snapshot.state != "available":
                continue
            self.assertEqual(to_v1_dicts(snapshot), load_events(payload))
            checked += 1
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()


def _claude_session(path: Path) -> dict:
    return {"source": "claude", "path": str(path), "id": "claude-pilot"}


def _claude_user(text: str, timestamp: str) -> dict:
    return {"type": "user", "timestamp": timestamp, "origin": {"kind": "human"},
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _claude_question_call(call_id: str, questions: list[dict]) -> dict:
    return {"type": "tool_use", "id": call_id, "name": "AskUserQuestion",
            "input": {"questions": questions}}


def _claude_tool_result(call_id: str, output: object, is_error: bool = False) -> dict:
    part: dict = {"type": "tool_result", "tool_use_id": call_id, "content": output}
    if is_error:
        part["is_error"] = True
    return {"type": "user", "timestamp": "2026-09-01T00:00:03Z",
            "message": {"role": "user", "content": [part]}}


def _question(header: str, text: str, options: list[str], multi: bool = False) -> dict:
    return {"header": header, "question": text, "multiSelect": multi,
            "options": [{"label": label, "description": f"desc-{label}"} for label in options]}


class ClaudeActivityTests(unittest.TestCase):
    def test_multi_question_answered_without_per_question_linkage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(path, [
                _claude_user("Pick colors.", "2026-09-01T00:00:01Z"),
                {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "text", "text": "Two decisions needed."},
                     _claude_question_call("q-1", [
                         _question("Theme", "Which color?", ["Blue", "Green"]),
                         _question("Shape", "Which shape?", ["Circle"], multi=True),
                     ]),
                 ]}},
                _claude_tool_result("q-1", "Blue"),
                {"type": "assistant", "timestamp": "2026-09-01T00:00:04Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "text", "text": "Done."}]}},
            ])
            snapshot = load_activity(_claude_session(path))

            self.assertEqual(snapshot.state, "available")
            call = next(e for e in snapshot.events if e.type == "tool_call")
            assert call.interaction is not None
            request = call.interaction
            self.assertEqual(request.purpose, "question")
            self.assertEqual(request.tool_call_id, "q-1")
            self.assertEqual(request.resolution, "answered")
            self.assertEqual([q.prompt for q in request.questions],
                             ["Which color?", "Which shape?"])
            self.assertEqual([q.title for q in request.questions], ["Theme", "Shape"])
            self.assertEqual([q.options[0].label for q in request.questions], ["Blue", "Circle"])
            self.assertEqual([q.multi_select for q in request.questions], [False, True])
            self.assertEqual(request.answers, ())
            self.assertEqual(snapshot.outcome.status, "done")
            self.assertEqual(to_v1_dicts(snapshot), load_events(_claude_session(path)))

    def test_single_question_links_its_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(path, [
                _claude_user("Pick one.", "2026-09-01T00:00:01Z"),
                {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
                 "message": {"role": "assistant", "content": [
                     _claude_question_call("q-1", [_question("H", "Which?", ["A", "B"])]),
                 ]}},
                _claude_tool_result("q-1", "A"),
            ])
            snapshot = load_activity(_claude_session(path))

            call = next(e for e in snapshot.events if e.type == "tool_call")
            assert call.interaction is not None
            self.assertEqual(call.interaction.resolution, "answered")
            self.assertEqual([(a.question_index, a.text) for a in call.interaction.answers],
                             [(0, "A")])

    def test_unanswered_and_error_results_stay_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(path, [
                _claude_user("Pick.", "2026-09-01T00:00:01Z"),
                {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
                 "message": {"role": "assistant", "content": [
                     _claude_question_call("q-open", [_question("H", "Which?", ["A"])]),
                     _claude_question_call("q-bad", [_question("H2", "Which?", ["B"])]),
                 ]}},
                _claude_tool_result("q-bad", "denied", is_error=True),
            ])
            snapshot = load_activity(_claude_session(path))

            calls = {e.call_id: e for e in snapshot.events if e.type == "tool_call"}
            assert calls["q-open"].interaction is not None
            assert calls["q-bad"].interaction is not None
            self.assertEqual(calls["q-open"].interaction.resolution, "unknown")
            self.assertEqual(calls["q-open"].interaction.answers, ())
            self.assertEqual(calls["q-bad"].interaction.resolution, "unknown")
            self.assertEqual(calls["q-bad"].interaction.answers, ())
            failed = next(e for e in snapshot.events
                          if e.type == "tool_result" and e.call_id == "q-bad")
            assert failed.result is not None
            self.assertEqual(failed.result.status, "error")
            self.assertEqual(to_v1_dicts(snapshot), load_events(_claude_session(path)))

    def test_system_error_tail_is_aborted_with_native_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(path, [
                _claude_user("Do it.", "2026-09-01T00:00:01Z"),
                {"type": "system", "timestamp": "2026-09-01T00:00:02Z",
                 "error": {"formatted": "401 API key is invalid.", "status": 401}},
            ])
            snapshot = load_activity(_claude_session(path))

            self.assertEqual(snapshot.outcome.status, "aborted")
            assert snapshot.outcome.error is not None
            self.assertEqual(snapshot.outcome.error.kind, "provider")
            self.assertEqual(snapshot.outcome.error.message, "401 API key is invalid.")
            self.assertEqual(snapshot.outcome.error.code, "401")
            self.assertEqual(snapshot.events[-1].type, "assistant_message")
            self.assertEqual(to_v1_dicts(snapshot), load_events(_claude_session(path)))

    def test_interrupted_and_limit_tails_are_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            interrupted = Path(directory) / "interrupted.jsonl"
            _write_jsonl(interrupted, [
                _claude_user("Do it.", "2026-09-01T00:00:01Z"),
                {"type": "user", "timestamp": "2026-09-01T00:00:02Z",
                 "message": {"role": "user", "content": [
                     {"type": "text", "text": "[Request interrupted by user]"}]}},
            ])
            limited = Path(directory) / "limited.jsonl"
            _write_jsonl(limited, [
                _claude_user("Do it.", "2026-09-01T00:00:01Z"),
                {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "text", "text": "You've hit your session limit. Done."}]}},
            ])

            interrupted_snapshot = load_activity(_claude_session(interrupted))
            self.assertEqual(interrupted_snapshot.outcome.status, "aborted")
            assert interrupted_snapshot.outcome.error is not None
            self.assertEqual(interrupted_snapshot.outcome.error.kind, "aborted")

            limited_snapshot = load_activity(_claude_session(limited))
            self.assertEqual(limited_snapshot.outcome.status, "aborted")
            assert limited_snapshot.outcome.error is not None

    def test_user_tail_pending_and_missing_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(path, [_claude_user("Hello?", "2026-09-01T00:00:01Z")])
            snapshot = load_activity(_claude_session(path))
            self.assertEqual(snapshot.state, "available")
            self.assertEqual(snapshot.outcome.status, "pending")

            missing = load_activity(
                {"source": "claude", "path": "/no/such/claude.jsonl", "id": "missing"})
            self.assertEqual(missing.state, "unavailable")

    def test_missing_is_error_is_inferred_ok_not_native(self) -> None:
        # 真实历史里成功结果大多缺 is_error 键（仅失败写 True）。
        # 缺键不是原生成功证据：typed 层记 inferred，v1 投影仍为 "ok"。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(path, [
                _claude_user("Run it.", "2026-09-01T00:00:01Z"),
                {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "id": "t-ok", "name": "Bash",
                      "input": {"command": "true"}},
                     {"type": "tool_use", "id": "t-explicit", "name": "Bash",
                      "input": {"command": "true"}},
                     {"type": "tool_use", "id": "t-empty", "name": "Bash",
                      "input": {"command": "true"}},
                 ]}},
                {"type": "user", "timestamp": "2026-09-01T00:00:03Z",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "t-ok",
                      "content": "all passed"},
                     {"type": "tool_result", "tool_use_id": "t-explicit",
                      "content": "all passed", "is_error": False},
                     {"type": "tool_result", "tool_use_id": "t-empty",
                      "content": []},
                 ]}},
            ])
            snapshot = load_activity(_claude_session(path))
            by_call = {e.call_id: e for e in snapshot.events if e.type == "tool_result"}

            inferred = by_call["t-ok"].result
            assert inferred is not None
            self.assertEqual(inferred.status, "ok")
            self.assertEqual(inferred.evidence.origin, "inferred")

            native = by_call["t-explicit"].result
            assert native is not None
            self.assertEqual((native.status, native.evidence.origin), ("ok", "native"))

            empty = by_call["t-empty"].result
            assert empty is not None
            self.assertEqual(empty.status, "unknown")

            projected = {e["call_id"]: e for e in to_v1_dicts(snapshot)
                         if e["type"] == "tool_result"}
            self.assertEqual(
                [projected[c]["status"] for c in ("t-ok", "t-explicit", "t-empty")],
                ["ok", "ok", "ok"])
            self.assertEqual(to_v1_dicts(snapshot), load_events(_claude_session(path)))

    def test_unanswered_question_tail_is_unknown_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(path, [
                _claude_user("Pick.", "2026-09-01T00:00:01Z"),
                {"type": "assistant", "timestamp": "2026-09-01T00:00:02Z",
                 "message": {"role": "assistant", "content": [
                     _claude_question_call("q-open", [_question("H", "Which?", ["A"])]),
                 ]}},
            ])
            snapshot = load_activity(_claude_session(path))

            self.assertEqual(snapshot.state, "available")
            self.assertEqual(snapshot.outcome.status, "unknown")
            call = next(e for e in snapshot.events if e.type == "tool_call")
            assert call.interaction is not None
            self.assertEqual(call.interaction.resolution, "unknown")

    def test_real_claude_histories_keep_v1_parity(self) -> None:
        import glob
        import os

        files = sorted(
            glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")),
            key=os.path.getmtime,
            reverse=True,
        )[:20]
        if not files:
            self.skipTest("没有本机 Claude 历史")
        for filename in files:
            session = {"source": "claude", "path": filename, "id": "parity"}
            snapshot = load_activity(session)
            self.assertIn(snapshot.state, {"available", "empty"})
            if snapshot.state != "available":
                continue
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))


def _codex_session(path: Path) -> dict:
    return {"source": "codex", "path": str(path), "id": "codex-pilot"}


def _codex_function_call(call_id: str, name: str, arguments: object) -> dict:
    return {"type": "response_item", "timestamp": "2026-09-01T00:00:02Z",
            "payload": {"type": "function_call", "call_id": call_id, "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False)}}


def _codex_output(call_id: str, output: object) -> dict:
    return {"type": "response_item", "timestamp": "2026-09-01T00:00:03Z",
            "payload": {"type": "function_call_output", "call_id": call_id,
                        "output": output}}


class CodexActivityTests(unittest.TestCase):
    def test_sync_question_links_answers_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(path, [
                {"type": "response_item", "timestamp": "2026-09-01T00:00:01Z",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "Pick."}]}},
                _codex_function_call("q-1", "request_user_input", {"questions": [
                    {"id": "color", "header": "Color", "question": "Which color?",
                     "options": [{"label": "Red", "description": "warm"}]},
                    {"id": "size", "header": "Size", "question": "Which size?",
                     "options": [{"label": "Large", "description": "l"}]},
                ]}),
                _codex_output("q-1", json.dumps({"answers": {
                    "color": {"answers": ["Red"]},
                    "size": {"answers": ["Large", "user_note: extra wide"]},
                }})),
            ])
            snapshot = load_activity(_codex_session(path))

            self.assertEqual(snapshot.state, "available")
            call = next(e for e in snapshot.events if e.type == "tool_call")
            assert call.interaction is not None
            request = call.interaction
            self.assertEqual(request.purpose, "question")
            self.assertEqual(request.tool_call_id, "q-1")
            self.assertEqual(request.resolution, "answered")
            self.assertEqual([q.prompt for q in request.questions],
                             ["Which color?", "Which size?"])
            self.assertEqual([(a.question_index, a.selected) for a in request.answers],
                             [(0, ("Red",)), (1, ("Large", "user_note: extra wide"))])
            # 尾部仍是 user 轮：镜像 list 规则判 pending（与 status_tag 一致）。
            self.assertEqual(snapshot.outcome.status, "pending")

    def test_async_accepted_is_not_an_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(path, [
                {"type": "response_item", "timestamp": "2026-09-01T00:00:01Z",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "Go."}]}},
                _codex_function_call("q-a", "request_user_input_async", {"questions": [
                    {"title": "Which?", "options": ["A", "B"]},
                ]}),
                _codex_output("q-a", '{"accepted":true}'),
            ])
            snapshot = load_activity(_codex_session(path))

            call = next(e for e in snapshot.events if e.type == "tool_call")
            assert call.interaction is not None
            self.assertEqual(call.interaction.resolution, "unknown")
            self.assertEqual(call.interaction.answers, ())
            self.assertEqual(
                [q.options[0].label for q in call.interaction.questions], ["A"])
            self.assertEqual(snapshot.outcome.status, "pending")

    def test_tool_results_carry_inferred_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(path, [
                _codex_function_call("t-1", "shell", {"cmd": "true"}),
                _codex_output("t-1", "all passed"),
                _codex_function_call("t-2", "shell", {"cmd": "false"}),
                _codex_output("t-2", "exit code: 1\nboom"),
            ])
            snapshot = load_activity(_codex_session(path))
            by_call = {e.call_id: e for e in snapshot.events if e.type == "tool_result"}

            assert by_call["t-1"].result is not None
            self.assertEqual(by_call["t-1"].result.status, "ok")
            self.assertEqual(by_call["t-1"].result.evidence.origin, "inferred")
            assert by_call["t-2"].result is not None
            self.assertEqual(by_call["t-2"].result.status, "error")

    def test_task_complete_error_is_aborted_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.jsonl"
            _write_jsonl(bad, [
                {"type": "event_msg", "timestamp": "2026-09-01T00:00:01Z",
                 "payload": {"type": "task_complete", "last_agent_message": None,
                             "error": {"message": "quota gone",
                                       "codex_error_info": "usage_limit_exceeded"}}},
            ])
            bad_snapshot = load_activity(_codex_session(bad))
            self.assertEqual(bad_snapshot.outcome.status, "aborted")
            assert bad_snapshot.outcome.error is not None
            self.assertEqual(bad_snapshot.outcome.error.code, "usage_limit_exceeded")

            good = Path(directory) / "good.jsonl"
            _write_jsonl(good, [
                {"type": "response_item", "timestamp": "2026-09-01T00:00:01Z",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "Done."}]}},
            ])
            self.assertEqual(load_activity(_codex_session(good)).outcome.status, "done")

    def test_open_tool_call_tail_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(path, [
                {"type": "response_item", "timestamp": "2026-09-01T00:00:01Z",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "Running."}]}},
                _codex_function_call("t-open", "shell", {"cmd": "sleep 60"}),
            ])
            snapshot = load_activity(_codex_session(path))
            self.assertEqual(snapshot.outcome.status, "unknown")

    def test_real_codex_histories_keep_v1_parity(self) -> None:
        import glob
        import os

        files = sorted(
            glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True),
            key=os.path.getmtime,
            reverse=True,
        )[:20]
        if not files:
            self.skipTest("没有本机 Codex 历史")
        for filename in files:
            session = {"source": "codex", "path": filename, "id": "parity"}
            snapshot = load_activity(session)
            self.assertIn(snapshot.state, {"available", "empty"})
            if snapshot.state != "available":
                continue
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))
