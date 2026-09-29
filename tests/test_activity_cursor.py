"""Synthetic goldens for Cursor's typed activity adapter."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sesskit.activity import to_v1_dicts
from sesskit.activity_cursor import load_cursor_activity
from sesskit.transcript import load_events


def _session(path: Path, session_id: str = "cursor-golden") -> dict:
    return {"source": "cursor", "path": str(path), "id": session_id}


def _store(path: Path, objects: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE IF EXISTS blobs")
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    for index, obj in enumerate(objects):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        connection.execute("INSERT INTO blobs VALUES (?, ?)", (f"blob-{index}", raw))
    connection.commit()
    connection.close()


def _call(call_id: str, args: object, name: str = "AskQuestion") -> dict:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": call_id,
                "toolName": name,
                "args": args,
            }
        ],
    }


def _result(call_id: str, result: object) -> dict:
    return {
        "role": "tool",
        "content": [
            {
                "type": "tool-result",
                "toolCallId": call_id,
                "toolName": "AskQuestion",
                "result": result,
            }
        ],
    }


class CursorActivityTests(unittest.TestCase):
    def test_event_order_payloads_and_independent_v1_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _store(
                path,
                [
                    {"role": "user", "content": "<user_query>Read the file</user_query>"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "reasoning", "text": "Inspecting."},
                            {"type": "text", "text": "I will read it."},
                            {
                                "type": "tool-call",
                                "toolCallId": "r1",
                                "toolName": "Read",
                                "args": '{"path":"notes.txt"}',
                            },
                        ],
                    },
                    _result("r1", "file contents"),
                    {"role": "assistant", "content": "Done."},
                ],
            )
            session = _session(path)
            snapshot = load_cursor_activity(session)

            self.assertEqual(snapshot.state, "available")
            self.assertIsNone(snapshot.cursor)
            self.assertIsNone(snapshot.generation)
            self.assertEqual(snapshot.outcome.status, "unknown")
            self.assertEqual(snapshot.outcome.evidence.origin, "unknown")
            self.assertEqual(
                [event.type for event in snapshot.events],
                ["user_message", "thinking", "assistant_message", "tool_call", "tool_result", "assistant_message"],
            )
            self.assertEqual(snapshot.events[0].text, "Read the file")
            self.assertEqual(snapshot.events[3].raw_input, {"path": "notes.txt"})
            self.assertEqual(snapshot.events[3].call_id, "r1")
            result = snapshot.events[4]
            self.assertEqual(result.raw_output, "file contents")
            assert result.result is not None
            self.assertEqual((result.result.status, result.result.evidence.origin), ("unknown", "unknown"))
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_early_result_keeps_legacy_call_then_result_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _store(path, [_result("late", "raw result"), _call("late", {"x": 1}, "Read")])
            session = _session(path)
            snapshot = load_cursor_activity(session)

            self.assertEqual([event.type for event in snapshot.events], ["tool_call", "tool_result"])
            self.assertEqual(snapshot.events[0].seq, 1)
            self.assertEqual(snapshot.events[1].seq, 2)
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_question_normalization_keeps_unverified_string_answer_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            args = {
                "title": "Choose a color",
                "questions": [
                    {
                        "id": "color-id",
                        "prompt": "Which color?",
                        "options": [{"id": "blue-id", "label": "Blue"}],
                    }
                ],
            }
            _store(path, [_call("q1", args), _result("q1", "blue-id")])
            session = _session(path)
            snapshot = load_cursor_activity(session)
            call = snapshot.events[0]

            self.assertEqual(call.raw_input["questions"][0]["options"][0]["id"], "blue-id")
            request = call.interaction
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.purpose, "question")
            self.assertEqual(request.resolution, "unknown")
            self.assertEqual(request.questions[0].prompt, "Which color?")
            self.assertEqual(request.questions[0].options[0].label, "Blue")
            self.assertEqual(request.questions[0].multi_select, False)
            self.assertEqual(request.resolution_evidence.origin, "unknown")
            self.assertEqual(request.answers, ())
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_grouped_answers_remain_unlinked_and_absent_result_is_not_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            grouped = {
                "questions": [
                    {"id": "q1", "prompt": "First?", "options": []},
                    {"id": "q2", "prompt": "Second?", "options": []},
                ]
            }
            _store(path, [_call("grouped", grouped), _result("grouped", "one answer")])
            snapshot = load_cursor_activity(_session(path))
            request = snapshot.events[0].interaction
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.resolution, "unknown")
            self.assertEqual(request.answers, ())

            _store(path, [_call("open", grouped)])
            snapshot = load_cursor_activity(_session(path))
            request = snapshot.events[0].interaction
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.resolution, "unknown")
            self.assertEqual(request.resolution_evidence.origin, "unknown")
            self.assertEqual(request.answers, ())
            self.assertEqual(snapshot.outcome.status, "unknown")

    def test_incomplete_question_shape_stays_as_raw_tool_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _store(path, [_call("incomplete", {"questions": [{"prompt": "Choose?"}]})])
            snapshot = load_cursor_activity(_session(path))

            self.assertIsNone(snapshot.events[0].interaction)
            self.assertEqual(snapshot.events[0].raw_input, {"questions": [{"prompt": "Choose?"}]})

    def test_duplicate_results_do_not_claim_a_question_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            args = {"questions": [{"id": "q", "prompt": "Choose?", "options": []}]}
            _store(path, [_result("q-call", "first"), _result("q-call", "second"), _call("q-call", args)])
            session = _session(path)
            snapshot = load_cursor_activity(session)
            request = snapshot.events[0].interaction

            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.resolution, "unknown")
            self.assertEqual(request.answers, ())
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_failed_tool_text_is_inferred_not_a_structured_agent_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _store(path, [_call("fail", {"path": "a"}, "Read"), _result("fail", "Error: denied")])
            session = _session(path)
            snapshot = load_cursor_activity(session)
            result = snapshot.events[-1]

            self.assertEqual(result.raw_output, "Error: denied")
            assert result.result is not None
            self.assertEqual((result.result.status, result.result.evidence.origin), ("error", "inferred"))
            self.assertIsNone(result.error)
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

    def test_prompt_fallback_empty_and_unavailable_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            chat_dir = Path(directory)
            prompt_path = chat_dir / "prompt_history.json"
            prompt_path.write_text(json.dumps(["newer", "older"]), encoding="utf-8")
            session = _session(chat_dir)
            snapshot = load_cursor_activity(session)
            self.assertEqual(snapshot.state, "available")
            self.assertEqual([event.text for event in snapshot.events], ["older", "newer"])
            self.assertEqual(to_v1_dicts(snapshot), load_events(session))

            prompt_path.write_text("[]", encoding="utf-8")
            self.assertEqual(load_cursor_activity(session).state, "empty")
            prompt_path.unlink()
            self.assertEqual(load_cursor_activity(session).state, "unavailable")


if __name__ == "__main__":
    unittest.main()
