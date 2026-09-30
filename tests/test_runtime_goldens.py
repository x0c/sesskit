"""Independent semantic goldens through public runtime adapters and loaders.

The synthetic rows use small native-format shapes, not copies of private live
histories. Expected activity and conversation values below are hand-authored.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sesskit.adapters import get_adapter
from sesskit.registry import ConversationLoadError, load_session_conversation


def _codex_user(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _codex_assistant(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _codex_call(call_id: str, name: str, arguments: object) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call", "call_id": call_id, "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _codex_output(call_id: str, output: object) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output", "call_id": call_id, "output": output,
        },
    }


def _codex_task_complete(**payload: object) -> dict:
    return {"type": "event_msg", "payload": {"type": "task_complete", **payload}}


def _write_codex(tmp_path: Path, rows: list[dict]) -> dict:
    path = tmp_path / "codex-history.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return {"source": "codex", "path": str(path), "id": "synthetic-codex"}


def _write_opencode_v1(path: Path, rows: list[tuple[str, dict, list[tuple[str, dict]]]]) -> None:
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
    conn.execute("INSERT INTO session VALUES ('synthetic-opencode')")
    for message_index, (message_id, message, parts) in enumerate(rows, 1):
        created = message_index * 1000
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?)",
            (message_id, "synthetic-opencode", created, json.dumps(message)),
        )
        for part_index, (part_id, part) in enumerate(parts, 1):
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?)",
                (part_id, message_id, "synthetic-opencode", created + part_index,
                 json.dumps(part)),
            )
    conn.commit()
    conn.close()


def _write_opencode_v2(path: Path, rows: list[tuple[str, int, str, dict]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY);
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, data TEXT);
        """
    )
    conn.execute("INSERT INTO session_v2 VALUES ('synthetic-opencode')")
    for row_id, seq, row_type, data in rows:
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?)",
            (row_id, "synthetic-opencode", row_type, seq, seq * 1000,
             json.dumps(data)),
        )
    conn.commit()
    conn.close()


def _write_opencode(tmp_path: Path, layout: str, rows: list) -> dict:
    path = tmp_path / "opencode-history.db"
    if layout == "v1":
        _write_opencode_v1(path, rows)
    else:
        _write_opencode_v2(path, rows)
    return {"source": "opencode", "path": str(path), "id": "synthetic-opencode"}


def _snapshot(session: dict):
    return get_adapter(session["source"]).load_activity(session)


def _events(snapshot) -> list[tuple[str, str | None, str | None]]:
    return [(event.type, event.text, event.call_id) for event in snapshot.events]


def _conversation(session: dict, *, include_errors: bool = False) -> list[tuple[str, str]]:
    return [(message.role, message.text) for message in load_session_conversation(
        session, include_errors=include_errors,
    )]


def test_codex_clean_completion_tool_correlation_and_bookkeeping(tmp_path):
    session = _write_codex(tmp_path, [
        _codex_user("Run the checks."),
        _codex_call("call-ok", "shell", {"cmd": "true"}),
        _codex_output("call-ok", "all checks passed"),
        _codex_call("call-bad", "shell", {"cmd": "false"}),
        _codex_output("call-bad", "exit code: 1\ncommand failed"),
        _codex_assistant("Everything passed."),
        _codex_task_complete(),
        {"type": "token_usage_record", "payload": {
            "turn_id": "turn-1", "turn_token_usage": {"input_tokens": 8},
        }},
    ])

    snapshot = _snapshot(session)

    assert snapshot.state == "available"
    assert _events(snapshot) == [
        ("user_message", "Run the checks.", None),
        ("tool_call", None, "call-ok"),
        ("tool_result", None, "call-ok"),
        ("tool_call", None, "call-bad"),
        ("tool_result", None, "call-bad"),
        ("assistant_message", "Everything passed.", None),
        ("lifecycle", "task_complete", None),
    ]
    call, result, bad_call, bad_result = snapshot.events[1:5]
    assert (call.name, call.raw_input) == ("shell", {"cmd": "true"})
    assert result.raw_output == "all checks passed"
    assert result.result is not None
    assert (result.result.status, result.result.evidence.origin) == ("ok", "inferred")
    assert (bad_call.name, bad_call.raw_input) == ("shell", {"cmd": "false"})
    assert bad_result.raw_output == "exit code: 1\ncommand failed"
    assert bad_result.result is not None
    assert (bad_result.result.status, bad_result.result.evidence.origin) == (
        "error", "inferred",
    )
    assert snapshot.outcome.status == "done"
    assert (snapshot.outcome.evidence.origin, snapshot.outcome.evidence.field,
            snapshot.outcome.evidence.record) == ("native", "payload.type", "line:7")
    assert snapshot.events[-1].stop_reason == "task_complete"
    assert _conversation(session) == [
        ("user", "Run the checks."), ("assistant", "Everything passed."),
    ]


@pytest.mark.parametrize(
    ("rows", "status", "evidence_field", "event_types"),
    [
        ([_codex_user("Waiting for work.")], "pending", "tail_role",
         ["user_message"]),
        ([_codex_user("Run this."), _codex_call("open-1", "shell", {"cmd": "sleep"})],
         "unknown", None, ["user_message", "tool_call"]),
    ],
)
def test_codex_pending_and_incomplete_tails_are_not_success(
    tmp_path, rows, status, evidence_field, event_types,
):
    session = _write_codex(tmp_path, rows)
    snapshot = _snapshot(session)

    assert snapshot.outcome.status == status
    assert snapshot.outcome.error is None
    assert snapshot.outcome.evidence.field == evidence_field
    assert [event.type for event in snapshot.events] == event_types
    if len(rows) == 1:
        assert _conversation(session) == [("user", "Waiting for work.")]


def test_codex_empty_tool_output_has_unknown_result_status(tmp_path):
    session = _write_codex(tmp_path, [
        _codex_user("Run the task."),
        _codex_call("empty-result", "shell", {"cmd": "command"}),
        _codex_output("empty-result", ""),
    ])
    snapshot = _snapshot(session)
    result = next(event for event in snapshot.events if event.type == "tool_result")

    assert result.call_id == "empty-result"
    assert result.raw_output == ""
    assert result.result is not None
    assert (result.result.status, result.result.evidence.origin) == ("unknown", "unknown")
    assert snapshot.outcome.status == "pending"


@pytest.mark.parametrize(
    ("terminal", "status", "error_kind", "error_code", "error_text", "event_type", "event_text"),
    [
        (
            _codex_task_complete(error={
                "message": "Usage limit reached.",
                "codex_error_info": "usage_limit_exceeded",
            }),
            "aborted", "provider", "usage_limit_exceeded",
            "Usage limit reached.", "assistant_message", "Usage limit reached.",
        ),
        (
            {"type": "event_msg", "payload": {
                "type": "turn_aborted", "reason": "user cancelled",
            }},
            "aborted", "aborted", None, "turn_aborted", "lifecycle",
            "turn_aborted: user cancelled",
        ),
    ],
)
def test_codex_quota_error_only_and_explicit_abort(
    tmp_path, terminal, status, error_kind, error_code, error_text, event_type, event_text,
):
    session = _write_codex(tmp_path, [_codex_user("Do the task."), terminal])
    snapshot = _snapshot(session)

    assert snapshot.outcome.status == status
    assert snapshot.outcome.error is not None
    assert (snapshot.outcome.error.kind, snapshot.outcome.error.code,
            snapshot.outcome.error.message) == (error_kind, error_code, error_text)
    ending = snapshot.events[-1]
    if event_type == "assistant_message":
        # task_complete with text also records the typed-only turn-end
        # boundary after the text card (lifecycle-aware settlement needs it;
        # turn_aborted already ended with its own lifecycle).
        assert ending.type == "lifecycle"
        assert ending.text == "task_complete"
        ending = snapshot.events[-2]
    assert ending.type == event_type
    assert ending.text == event_text
    if event_type == "assistant_message":
        assert ending.error is not None
        assert ending.error.code == "usage_limit_exceeded"
        assert _conversation(session) == [("user", "Do the task.")]
        assert _conversation(session, include_errors=True) == [
            ("user", "Do the task."), ("assistant", "Usage limit reached."),
        ]
    assert not any(message[0] == "assistant" for message in _conversation(session))


@pytest.mark.parametrize("layout", ["v1", "v2"])
def test_opencode_clean_tool_results_and_finish_golden(tmp_path, layout):
    if layout == "v1":
        rows = [
            ("u1", {"role": "user"}, [("p-u", {"type": "text", "text": "Run the checks."})]),
            ("a1", {"role": "assistant", "finish": "tool-calls"}, [
                ("p-text", {"type": "text", "text": "Starting."}),
                ("p-ok", {"type": "tool", "callID": "call-ok", "tool": "bash",
                          "state": {"status": "completed", "input": {"cmd": "true"},
                                    "output": "ok"}}),
                ("p-bad", {"type": "tool", "callID": "call-bad", "tool": "edit",
                            "state": {"status": "error", "input": {"path": "note.txt"},
                                      "error": "write denied"}}),
            ]),
            ("a2", {"role": "assistant", "finish": "stop"}, [
                ("p-done", {"type": "text", "text": "Checks finished."}),
            ]),
        ]
    else:
        rows = [
            ("u1", 1, "user", {"text": "Run the checks."}),
            ("a1", 2, "assistant", {"finish": "tool-calls", "content": [
                {"type": "text", "text": "Starting."},
                {"type": "tool", "id": "call-ok", "name": "bash", "state": {
                    "status": "completed", "input": {"cmd": "true"},
                    "content": [{"type": "text", "text": "ok"}],
                }},
                {"type": "tool", "id": "call-bad", "name": "edit", "state": {
                    "status": "error", "input": {"path": "note.txt"},
                    "error": {"message": "write denied"},
                }},
            ]}),
            ("a2", 3, "assistant", {
                "finish": "stop", "content": [{"type": "text", "text": "Checks finished."}],
            }),
        ]
    session = _write_opencode(tmp_path, layout, rows)

    snapshot = _snapshot(session)

    assert snapshot.state == "available"
    assert _events(snapshot) == [
        ("user_message", "Run the checks.", None),
        ("assistant_message", "Starting.", None),
        ("tool_call", None, "call-ok"),
        ("tool_result", None, "call-ok"),
        ("tool_call", None, "call-bad"),
        ("tool_result", None, "call-bad"),
        ("assistant_message", "Checks finished.", None),
    ]
    by_call = {event.call_id: event for event in snapshot.events if event.type == "tool_result"}
    assert (by_call["call-ok"].result.status, by_call["call-ok"].result.evidence.origin) == (
        "ok", "native",
    )
    assert (by_call["call-bad"].result.status, by_call["call-bad"].result.evidence.origin) == (
        "error", "native",
    )
    assert (by_call["call-ok"].raw_output, by_call["call-bad"].raw_output) == (
        "ok" if layout == "v1" else [{"type": "text", "text": "ok"}],
        "write denied" if layout == "v1" else {"message": "write denied"},
    )
    assert snapshot.outcome.status == "done"
    assert snapshot.outcome.evidence.field == (
        "message.data.finish" if layout == "v1" else "data.finish"
    )
    assert _conversation(session) == [
        ("user", "Run the checks."), ("assistant", "Starting."),
        ("assistant", "Checks finished."),
    ]
    assert snapshot.events[0].message_id == "opencode:synthetic-opencode:message:u1"
    assert snapshot.events[2].message_id == snapshot.events[3].message_id


@pytest.mark.parametrize("layout", ["v1", "v2"])
def test_opencode_open_tool_call_and_absent_result_stay_unknown(tmp_path, layout):
    if layout == "v1":
        rows = [
            ("a1", {"role": "assistant", "finish": "stop"}, [
                ("p-open", {"type": "tool", "callID": "open-1", "tool": "bash",
                             "state": {"status": "running", "input": {"cmd": "sleep"}}}),
            ]),
        ]
    else:
        rows = [
            ("a1", 1, "assistant", {"finish": "stop", "content": [
                {"type": "tool", "id": "open-1", "name": "bash", "state": {
                    "status": "running", "input": {"cmd": "sleep"},
                }},
            ]}),
        ]
    snapshot = _snapshot(_write_opencode(tmp_path, layout, rows))

    assert snapshot.outcome.status == "unknown"
    assert _events(snapshot) == [("tool_call", None, "open-1")]
    assert not any(event.type == "tool_result" for event in snapshot.events)


@pytest.mark.parametrize("layout", ["v1", "v2"])
@pytest.mark.parametrize("tail", ["user", "assistant_without_finish"])
def test_opencode_user_tail_and_missing_finish_have_literal_outcomes(tmp_path, layout, tail):
    if layout == "v1":
        if tail == "user":
            rows = [
                ("u1", {"role": "user"}, [
                    ("p-u", {"type": "text", "text": "Waiting for your reply."}),
                ]),
            ]
        else:
            rows = [
                ("a1", {"role": "assistant"}, [
                    ("p-a", {"type": "text", "text": "A partial answer."}),
                ]),
            ]
    elif tail == "user":
        rows = [("u1", 1, "user", {"text": "Waiting for your reply."})]
    else:
        rows = [("a1", 1, "assistant", {
            "content": [{"type": "text", "text": "A partial answer."}],
        })]
    session = _write_opencode(tmp_path, layout, rows)
    snapshot = _snapshot(session)

    if tail == "user":
        assert snapshot.outcome.status == "pending"
        assert (snapshot.outcome.evidence.origin, snapshot.outcome.evidence.field) == (
            "inferred", "tail_role",
        )
        assert _events(snapshot) == [("user_message", "Waiting for your reply.", None)]
        assert _conversation(session) == [("user", "Waiting for your reply.")]
    else:
        assert snapshot.outcome.status == "unknown"
        assert snapshot.outcome.evidence.origin == "unknown"
        assert snapshot.outcome.evidence.field is None
        assert _events(snapshot) == [("assistant_message", "A partial answer.", None)]
        assert _conversation(session) == [("assistant", "A partial answer.")]


@pytest.mark.parametrize("layout", ["v1", "v2"])
def test_opencode_native_stop_beats_trailing_idle_bookkeeping(tmp_path, layout):
    if layout == "v1":
        rows = [
            ("a1", {"role": "assistant", "finish": "stop"}, [
                ("p-done", {"type": "text", "text": "Finished."}),
            ]),
        ]
    else:
        rows = [
            ("a1", 1, "assistant", {
                "finish": "stop", "content": [{"type": "text", "text": "Finished."}],
            }),
            ("idle1", 2, "idle", {"outcome": "failed"}),
        ]
    snapshot = _snapshot(_write_opencode(tmp_path, layout, rows))

    assert snapshot.outcome.status == "done"
    assert snapshot.outcome.evidence.field == (
        "message.data.finish" if layout == "v1" else "data.finish"
    )
    assert _events(snapshot) == [("assistant_message", "Finished.", None)]


@pytest.mark.parametrize("layout", ["v1", "v2"])
@pytest.mark.parametrize(
    ("error_shape", "status", "error_kind", "error_code", "error_text"),
    [
        ("quota", "aborted", "quota_exhausted", "429", "429: Quota reached"),
        ("abort", "aborted", "user_interrupt", None, "The operation was aborted."),
    ],
)
def test_opencode_error_only_turns_retain_native_error(
    tmp_path, layout, error_shape, status, error_kind, error_code, error_text,
):
    if layout == "v1":
        error = (
            {"name": "APIError", "data": {"message": "Quota reached", "statusCode": 429}}
            if error_shape == "quota" else
            {"name": "MessageAbortedError", "data": {"message": "The operation was aborted."}}
        )
        rows = [
            ("u1", {"role": "user"}, [("p-u", {"type": "text", "text": "Do the task."})]),
            ("a1", {"role": "assistant", "finish": "stop", "error": error}, []),
        ]
    else:
        error = (
            {"type": "provider.quota", "message": "Quota reached", "status": 429}
            if error_shape == "quota" else
            {"type": "MessageAbortedError", "message": "The operation was aborted."}
        )
        rows = [
            ("u1", 1, "user", {"text": "Do the task."}),
            ("a1", 2, "assistant", {"finish": "stop", "content": [], "error": error}),
        ]
    session = _write_opencode(tmp_path, layout, rows)
    snapshot = _snapshot(session)

    assert snapshot.outcome.status == status
    assert snapshot.outcome.error is not None
    assert (snapshot.outcome.error.kind, snapshot.outcome.error.code,
            snapshot.outcome.error.message) == (error_kind, error_code, error_text)
    assert _events(snapshot) == [
        ("user_message", "Do the task.", None),
        ("assistant_message", error_text, None),
    ]
    assert snapshot.events[-1].error is not None
    assert _conversation(session) == [("user", "Do the task.")]
    assert _conversation(session, include_errors=True) == [
        ("user", "Do the task."), ("assistant", error_text),
    ]


@pytest.mark.parametrize("layout", ["v1", "v2"])
def test_opencode_prior_error_does_not_poison_later_clean_turn(tmp_path, layout):
    if layout == "v1":
        rows = [
            ("a-error", {"role": "assistant", "error": {
                "name": "APIError", "data": {"message": "Temporary outage", "statusCode": 503},
            }}, []),
            ("u2", {"role": "user"}, [("p-u2", {"type": "text", "text": "Try again."})]),
            ("a2", {"role": "assistant", "finish": "stop"}, [
                ("p-a2", {"type": "text", "text": "It worked."}),
            ]),
        ]
    else:
        rows = [
            ("a-error", 1, "assistant", {"content": [], "error": {
                "type": "provider.error", "message": "Temporary outage", "status": 503,
            }}),
            ("u2", 2, "user", {"text": "Try again."}),
            ("a2", 3, "assistant", {
                "finish": "stop", "content": [{"type": "text", "text": "It worked."}],
            }),
        ]
    session = _write_opencode(tmp_path, layout, rows)
    snapshot = _snapshot(session)

    assert snapshot.outcome.status == "done"
    assert snapshot.outcome.error is None
    assert _events(snapshot) == [
        ("assistant_message", "503: Temporary outage", None),
        ("user_message", "Try again.", None),
        ("assistant_message", "It worked.", None),
    ]
    assert _conversation(session) == [
        ("user", "Try again."), ("assistant", "It worked."),
    ]


def test_codex_prior_error_does_not_poison_later_clean_turn(tmp_path):
    session = _write_codex(tmp_path, [
        _codex_user("First attempt."),
        _codex_task_complete(error={
            "message": "Temporary outage.", "codex_error_info": "provider_error",
        }),
        _codex_user("Try again."),
        _codex_assistant("It worked."),
        _codex_task_complete(),
    ])
    snapshot = _snapshot(session)

    assert snapshot.outcome.status == "done"
    assert snapshot.outcome.error is None
    assert _events(snapshot) == [
        ("user_message", "First attempt.", None),
        ("assistant_message", "Temporary outage.", None),
        ("lifecycle", "task_complete", None),
        ("user_message", "Try again.", None),
        ("assistant_message", "It worked.", None),
        ("lifecycle", "task_complete", None),
    ]
    first_error = snapshot.events[1].error
    assert first_error is not None
    assert (first_error.code, first_error.message) == ("provider_error", "Temporary outage.")
    assert _conversation(session) == [
        ("user", "First attempt."), ("user", "Try again."),
        ("assistant", "It worked."),
    ]


def test_codex_question_answers_match_ids_and_async_acceptance_is_not_answer(tmp_path):
    session = _write_codex(tmp_path, [
        _codex_user("Choose and continue."),
        _codex_call("sync-q", "request_user_input", {"questions": [
            {"id": "color", "question": "Which color?", "options": ["Red", "Blue"]},
        ]}),
        _codex_output("sync-q", '{"answers":{"color":{"answers":["Blue"]}}}'),
        _codex_call("async-q", "request_user_input_async", {"questions": [
            {"title": "Which size?", "options": ["Small", "Large"]},
        ]}),
        _codex_output("async-q", '{"accepted":true,"task_id":"task-1"}'),
    ])
    snapshot = _snapshot(session)
    questions = {
        event.call_id: event.interaction
        for event in snapshot.events if event.type == "tool_call"
    }

    sync = questions["sync-q"]
    async_request = questions["async-q"]
    assert sync is not None and async_request is not None
    assert (sync.purpose, sync.tool_call_id, sync.resolution) == (
        "question", "sync-q", "answered",
    )
    assert [(answer.question_index, answer.text, answer.selected)
            for answer in sync.answers] == [(0, "Blue", ("Blue",))]
    assert (async_request.purpose, async_request.tool_call_id,
            async_request.resolution, async_request.answers) == (
        "question", "async-q", "unknown", (),
    )
    assert snapshot.outcome.status == "pending"


@pytest.mark.parametrize("layout", ["v1", "v2"])
def test_opencode_question_answer_matches_its_tool_call(tmp_path, layout):
    question = {"question": "Continue?", "options": ["Yes", "No"]}
    if layout == "v1":
        rows = [
            ("u1", {"role": "user"}, [("p-u", {"type": "text", "text": "Choose."})]),
            ("a1", {"role": "assistant", "finish": "tool-calls"}, [
                ("p-q", {"type": "tool", "callID": "question-1", "tool": "question",
                         "state": {"status": "completed", "input": {"questions": [question]},
                                   "output": "Yes"}}),
            ]),
        ]
    else:
        rows = [
            ("u1", 1, "user", {"text": "Choose."}),
            ("a1", 2, "assistant", {"content": [
                {"type": "tool", "id": "question-1", "name": "question", "state": {
                    "status": "completed", "input": {"questions": [question]},
                    "content": [{"type": "text", "text": "Yes"}],
                }},
            ]}),
        ]
    snapshot = _snapshot(_write_opencode(tmp_path, layout, rows))
    call = next(event for event in snapshot.events if event.type == "tool_call")

    assert call.call_id == "question-1"
    assert call.interaction is not None
    assert (call.interaction.purpose, call.interaction.tool_call_id,
            call.interaction.resolution) == ("question", "question-1", "answered")
    assert [(answer.question_index, answer.text)
            for answer in call.interaction.answers] == [(0, "Yes")]
    assert call.interaction.questions[0].prompt == "Continue?"


@pytest.mark.parametrize(
    ("runtime", "layout"),
    [("codex", "jsonl"), ("opencode", "v1"), ("opencode", "v2")],
)
def test_readable_empty_and_unavailable_histories_are_distinct(tmp_path, runtime, layout):
    if runtime == "codex":
        empty_path = tmp_path / "empty-codex.jsonl"
        empty_path.write_text("", encoding="utf-8")
        empty_session = {"source": runtime, "path": str(empty_path), "id": "empty"}
        missing_session = {
            "source": runtime, "path": str(tmp_path / "missing-codex.jsonl"), "id": "missing",
        }
    else:
        empty_session = _write_opencode(tmp_path, layout, [])
        empty_session["id"] = "synthetic-opencode"
        missing_session = {
            "source": runtime, "path": str(tmp_path / "missing-opencode.db"),
            "id": "synthetic-opencode",
        }

    empty = _snapshot(empty_session)
    unavailable = _snapshot(missing_session)
    assert (empty.state, empty.events, empty.outcome.status) == ("empty", (), "unknown")
    assert (unavailable.state, unavailable.events, unavailable.outcome.status) == (
        "unavailable", (), "unknown",
    )
    with pytest.raises(ConversationLoadError, match="history"):
        load_session_conversation(missing_session)
