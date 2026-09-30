"""Golden and bounded parity tests for the standalone OpenCode activity adapter."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sesskit.activity import to_v1_dicts
from sesskit.activity_opencode import load_opencode_activity
from sesskit.parsers import opencode as scan_opencode
from sesskit.transcript import load_events


def _session(path: Path, session_id: str) -> dict:
    return {"source": "opencode", "path": str(path), "id": session_id}


def _v1_db(path: Path, messages: list[tuple[str, dict, list[tuple[str, dict]]]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT INTO session VALUES (?)", ("v1",))
    for index, (message_id, message, parts) in enumerate(messages):
        created = message.get("time", {}).get("created", (index + 1) * 1000)
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?)",
            (message_id, "v1", created, json.dumps(message)),
        )
        for part_index, (part_id, part) in enumerate(parts):
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                (part_id, message_id, "v1", created + part_index, json.dumps(part)),
            )
    conn.commit()
    conn.close()


def _v2_db(path: Path, rows: list[tuple[str, int, str, dict]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY);
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT INTO session_v2 VALUES (?)", ("v2",))
    for row_id, seq, kind, data in rows:
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?)",
            (row_id, "v2", kind, seq, seq * 1000, json.dumps(data)),
        )
    conn.commit()
    conn.close()


def _legacy_projection(snapshot) -> list[dict]:
    # The production v1 projection is the contract; a private copy here fell
    # behind once typed-only lifecycle markers were added (2026-09-30).
    return to_v1_dicts(snapshot)


def _same_legacy_events(snapshot, legacy: list[dict]) -> bool:
    return _legacy_projection(snapshot) == legacy


class OpenCodeV1ActivityTests(unittest.TestCase):
    def test_v1_order_raw_tool_data_error_and_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _v1_db(path, [
                ("u1", {"role": "user", "time": {"created": 1000}}, [
                    ("p-u", {"type": "text", "text": "Run the task."}),
                ]),
                ("a1", {"role": "assistant", "time": {"created": 2000},
                        "finish": "tool-calls"}, [
                    ("p-text", {"type": "text", "text": "Starting."}),
                    ("p-think", {"type": "reasoning", "text": "Check plan."}),
                    ("p-tool", {"type": "tool", "callID": "call-1", "tool": "bash",
                                "state": {"status": "completed", "input": '{"cmd":"pwd"}',
                                          "output": "repo"}}),
                ]),
                ("a2", {"role": "assistant", "time": {"created": 3000},
                        "finish": "stop", "error": {"name": "APIError", "data": {
                            "message": "Provider quota reached", "statusCode": 429,
                        }}}, []),
            ])
            session = _session(path, "v1")
            snapshot = load_opencode_activity(session)
            legacy = load_events(session)

            self.assertEqual(snapshot.state, "available")
            self.assertIsNone(snapshot.cursor)
            self.assertIsNone(snapshot.generation)
            self.assertTrue(_same_legacy_events(snapshot, legacy))
            self.assertEqual(
                [event.type for event in snapshot.events],
                ["user_message", "assistant_message", "thinking", "tool_call",
                 "tool_result", "assistant_message"],
            )
            call = next(event for event in snapshot.events if event.type == "tool_call")
            self.assertEqual(call.raw_input, {"cmd": "pwd"})
            self.assertEqual(call.call_id, "call-1")
            result = next(event for event in snapshot.events if event.type == "tool_result")
            self.assertEqual(result.raw_output, "repo")
            assert result.result is not None
            self.assertEqual((result.result.status, result.result.evidence.origin),
                             ("ok", "native"))
            self.assertEqual(snapshot.outcome.status, "aborted")
            assert snapshot.outcome.error is not None
            self.assertEqual(snapshot.outcome.error.code, "429")
            self.assertEqual(snapshot.events[-1].text, "429: Provider quota reached")
            self.assertIsNotNone(snapshot.events[-1].error)

    def test_running_tool_and_nonterminal_finish_stay_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _v1_db(path, [
                ("a1", {"role": "assistant", "finish": "tool-calls"}, [
                    ("p1", {"type": "tool", "callID": "call-open", "tool": "bash",
                            "state": {"status": "running", "input": {"cmd": "sleep"}}}),
                ]),
            ])
            snapshot = load_opencode_activity(_session(path, "v1"))
            self.assertEqual(snapshot.outcome.status, "unknown")
            self.assertEqual([event.type for event in snapshot.events], ["tool_call"])

    def test_readable_empty_missing_and_unsupported_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            empty_path = Path(directory) / "empty.db"
            _v1_db(empty_path, [])
            empty = load_opencode_activity(_session(empty_path, "v1"))
            self.assertEqual(empty.state, "empty")

            unknown_path = Path(directory) / "unknown.db"
            sqlite3.connect(unknown_path).close()
            unsupported = load_opencode_activity(_session(unknown_path, "v1"))
            self.assertEqual(unsupported.state, "unsupported")

        missing = load_opencode_activity({
            "source": "opencode", "path": "/no/such/opencode.db", "id": "missing",
        })
        wrong_runtime = load_opencode_activity({
            "source": "cursor", "path": "/unused", "id": "wrong",
        })
        self.assertEqual(missing.state, "unavailable")
        self.assertEqual(wrong_runtime.state, "unsupported")


class OpenCodeV2ActivityTests(unittest.TestCase):
    def test_v2_native_status_raw_payload_and_error_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _v2_db(path, [
                ("u1", 1, "user", {"time": {"created": 1000}, "text": "Run."}),
                ("a1", 2, "assistant", {
                    "time": {"created": 2000}, "finish": "tool-calls",
                    "content": [
                        {"type": "reasoning", "text": "private thought"},
                        {"type": "text", "text": "Starting."},
                        {"type": "tool", "id": "ok", "name": "bash", "state": {
                            "status": "completed", "input": {"cmd": "pwd"},
                            "content": [{"type": "text", "text": "repo"}],
                        }},
                        {"type": "tool", "id": "bad", "name": "edit", "state": {
                            "status": "error", "input": {"path": "/x"},
                            "error": {"type": "tool.execution", "message": "write failed"},
                        }},
                    ],
                }),
                ("c1", 3, "compaction", {"summary": "Compacted."}),
                ("a2", 4, "assistant", {
                    "time": {"created": 4000}, "content": [],
                    "error": {"type": "provider.quota", "message": "Quota reached",
                              "status": 429},
                }),
            ])
            session = _session(path, "v2")
            snapshot = load_opencode_activity(session)

            self.assertEqual(snapshot.state, "available")
            self.assertTrue(_same_legacy_events(snapshot, load_events(session)))
            self.assertEqual(
                [event.type for event in snapshot.events],
                ["user_message", "assistant_message", "tool_call", "tool_result",
                 "tool_call", "tool_result", "thinking", "assistant_message"],
            )
            self.assertNotIn("private thought", [event.text for event in snapshot.events])
            results = {event.call_id: event for event in snapshot.events
                       if event.type == "tool_result"}
            assert results["ok"].result is not None
            assert results["bad"].result is not None
            self.assertEqual(results["ok"].result.status, "ok")
            self.assertEqual(results["ok"].result.evidence.field, "state.status")
            self.assertEqual(results["ok"].raw_output,
                             [{"type": "text", "text": "repo"}])
            self.assertEqual(results["bad"].result.status, "error")
            self.assertEqual(results["bad"].raw_output["message"], "write failed")
            self.assertEqual(snapshot.outcome.status, "aborted")
            assert snapshot.outcome.error is not None
            self.assertEqual(snapshot.outcome.error.code, "429")

    def test_grouped_question_links_only_proven_answers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _v2_db(path, [
                ("u1", 1, "user", {"text": "Choose."}),
                ("a1", 2, "assistant", {
                    "content": [
                        {"type": "tool", "id": "q1", "name": "question", "state": {
                            "status": "completed",
                            "input": {"questions": [
                                {"header": "Color", "question": "Which color?",
                                 "multiple": True, "options": [
                                     {"label": "Blue", "description": "Cool"}, "Green",
                                 ]},
                                {"title": "Shape", "options": ["Circle"]},
                            ]},
                            "content": [{"type": "text", "text": "Blue"}],
                        }},
                        {"type": "tool", "id": "q2", "name": "question", "state": {
                            "status": "error", "input": {"questions": [
                                {"question": "Continue?", "options": ["Yes", "No"]},
                            ]},
                            "error": {"message": "dismissed"},
                        }},
                    ],
                }),
            ])
            snapshot = load_opencode_activity(_session(path, "v2"))
            calls = {event.call_id: event for event in snapshot.events
                     if event.type == "tool_call"}
            answered = calls["q1"].interaction
            failed = calls["q2"].interaction
            assert answered is not None and failed is not None
            self.assertEqual(answered.purpose, "question")
            self.assertEqual(answered.resolution, "answered")
            self.assertEqual([item.prompt for item in answered.questions],
                             ["Which color?", "Shape"])
            self.assertTrue(answered.questions[0].multi_select)
            self.assertEqual([option.label for option in answered.questions[0].options],
                             ["Blue", "Green"])
            self.assertEqual(answered.answers, ())  # Plain text cannot identify either item.
            self.assertEqual(failed.resolution, "unknown")
            self.assertEqual(failed.answers, ())

    def test_single_question_answer_requires_a_matching_nonerror_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _v2_db(path, [
                ("a1", 1, "assistant", {"content": [
                    {"type": "tool", "id": "good", "name": "question", "state": {
                        "status": "completed", "input": {"questions": [
                            {"question": "Continue?", "options": ["Yes", "No"]},
                        ]}, "content": [{"type": "text", "text": "Yes"}],
                    }},
                    {"type": "tool", "id": "bad", "name": "question", "state": {
                        "status": "error", "input": {"questions": [
                            {"question": "Deploy?", "options": ["Now"]},
                        ]}, "error": "cancelled",
                    }},
                ]}),
            ])
            snapshot = load_opencode_activity(_session(path, "v2"))
            calls = {event.call_id: event for event in snapshot.events
                     if event.type == "tool_call"}
            good = calls["good"].interaction
            bad = calls["bad"].interaction
            assert good is not None and bad is not None
            self.assertEqual([(answer.question_index, answer.text) for answer in good.answers],
                             [(0, "Yes")])
            self.assertEqual(bad.resolution, "unknown")
            self.assertEqual(bad.answers, ())

    def test_idle_failure_only_affects_a_trailing_skipped_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _v2_db(path, [
                ("idle-old", 1, "idle", {"outcome": "failed"}),
                ("u1", 2, "user", {"text": "Still waiting?"}),
            ])
            pending = load_opencode_activity(_session(path, "v2"))
            self.assertEqual(pending.outcome.status, "pending")

            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?,?)",
                ("idle-tail", "v2", "idle", 3, 3000, json.dumps({"outcome": "failed"})),
            )
            conn.commit()
            conn.close()
            aborted = load_opencode_activity(_session(path, "v2"))
            self.assertEqual(aborted.outcome.status, "aborted")
            self.assertEqual(aborted.outcome.evidence.field, "data.outcome")

    def test_unidentified_running_tool_prevents_false_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _v2_db(path, [
                ("a1", 1, "assistant", {
                    "finish": "stop",
                    "content": [{"type": "tool", "name": "bash", "state": {
                        "status": "running", "input": {"cmd": "long-running"},
                    }}],
                }),
            ])
            snapshot = load_opencode_activity(_session(path, "v2"))
            self.assertEqual(snapshot.outcome.status, "unknown")
            self.assertEqual([event.type for event in snapshot.events], ["tool_call"])

    def test_real_history_parity_is_bounded_and_never_prints_payloads(self) -> None:
        paths = scan_opencode._db_paths()
        if not paths:
            self.skipTest("no local OpenCode history")
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "opencode-snapshot.db"
            source = scan_opencode.connect_ro(paths[0])
            if source is None:
                self.fail("could not open local history read-only for snapshot")
            destination = sqlite3.connect(snapshot_path)
            try:
                source.backup(destination)
            except sqlite3.Error:
                self.fail("could not create a stable local history snapshot")
            finally:
                destination.close()
                source.close()

            # Select IDs only after backup, so all exact IDs and both views
            # refer to the same consistent database image.
            conn = sqlite3.connect(snapshot_path)
            try:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                sample: list[str] = []
                if {"session", "message", "part"} <= tables:
                    sample.extend(row[0] for row in conn.execute(
                        "SELECT s.id FROM session s JOIN message m ON m.session_id=s.id "
                        "WHERE s.parent_id IS NULL GROUP BY s.id "
                        "ORDER BY MAX(m.time_created) DESC LIMIT 2"
                    ))
                if "session_message" in tables:
                    sample.extend(row[0] for row in conn.execute(
                        "SELECT session_id FROM session_message "
                        "WHERE type IN ('user','assistant') "
                        "GROUP BY session_id ORDER BY MAX(time_updated) DESC LIMIT 3"
                    ))
            finally:
                conn.close()

            sample = list(dict.fromkeys(str(session_id) for session_id in sample))[:5]
            if not sample:
                self.skipTest("stable local history snapshot has no bounded sample")
            checked = 0
            for session_id in sample:
                # Keep each source-native session ID unchanged in the copied DB.
                session = _session(snapshot_path, session_id)
                snapshot = load_opencode_activity(session)
                legacy = load_events(session)
                self.assertIn(snapshot.state, {"available", "empty"})
                if snapshot.state == "available":
                    typed = _legacy_projection(snapshot)
                    if typed != legacy:
                        field_counts: dict[str, int] = {}
                        first_difference = None
                        for left, right in zip(typed, legacy):
                            if left != right and first_difference is None:
                                first_difference = left["seq"]
                            for field in set(left) | set(right):
                                if left.get(field) != right.get(field):
                                    field_counts[field] = field_counts.get(field, 0) + 1
                        typed_types: dict[str, int] = {}
                        legacy_types: dict[str, int] = {}
                        for event in typed:
                            typed_types[event["type"]] = typed_types.get(event["type"], 0) + 1
                        for event in legacy:
                            legacy_types[event["type"]] = legacy_types.get(event["type"], 0) + 1
                        self.fail(
                            "stable snapshot typed-v1 mismatch: "
                            f"event_counts=({len(typed)}, {len(legacy)}), "
                            f"first_differing_seq={first_difference}, "
                            f"typed_type_counts={typed_types}, "
                            f"legacy_type_counts={legacy_types}, "
                            f"typed_tail={[(event['type'], event['seq']) for event in typed[-3:]]}, "
                            f"legacy_tail={[(event['type'], event['seq']) for event in legacy[-3:]]}, "
                            f"differing_fields={field_counts}"
                        )
                checked += 1
            self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
