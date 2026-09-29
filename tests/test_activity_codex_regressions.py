"""Synthetic Codex regressions for typed activity and its v1 projection."""

from __future__ import annotations

import json

from sesskit.activity import load_activity, to_v1_dicts
from sesskit.transcript import load_events


def _message(role: str, text: str) -> dict:
    text_type = "input_text" if role == "user" else "output_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": text_type, "text": text}],
        },
    }


def _call(call_id: str, name: str, arguments: object) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _output(call_id: str, output: object) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


def _complete() -> dict:
    return {"type": "event_msg", "payload": {"type": "task_complete"}}


def _load(tmp_path, rows: list[dict]):
    path = tmp_path / "codex.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    session = {"source": "codex", "path": str(path), "id": "synthetic"}
    return load_activity(session), session


def _question_call(call_id: str, name: str = "request_user_input") -> dict:
    return _call(
        call_id,
        name,
        {
            "questions": [
                {"id": "color", "question": "Which color?", "options": ["Red", "Blue"]},
            ]
        },
    )


def test_codex_adjacent_dedup_has_independent_v1_golden(tmp_path):
    snapshot, session = _load(
        tmp_path,
        [
            _message("assistant", "same"),
            _call("tool-1", "bash", {"cmd": "true"}),
            _message("assistant", "same"),
            _message("assistant", "same"),
        ],
    )
    expected = [
        {"type": "assistant_message", "seq": 1, "ts": None, "text": "same"},
        {
            "type": "tool_call",
            "seq": 2,
            "ts": None,
            "id": "tool-1",
            "name": "bash",
            "kind": "shell",
            "input": {"cmd": "true"},
        },
        {"type": "assistant_message", "seq": 3, "ts": None, "text": "same"},
    ]

    assert to_v1_dicts(snapshot) == expected
    # Check both APIs against an independent fixture expectation, not each other.
    assert load_events(session) == expected


def test_codex_structured_answers_require_nonempty_matching_question_id(tmp_path):
    for index, result in enumerate(
        (
            {"answers": {"wrong-id": {"answers": ["Red"]}}},
            {"answers": {"color": {"answers": ["", "  "]}}},
            {"answers": {}},
            {"answers": []},
        )
    ):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        snapshot, _ = _load(
            case_path,
            [
                _question_call("question-1"),
                _output("question-1", json.dumps(result)),
            ],
        )

        call = next(event for event in snapshot.events if event.type == "tool_call")
        assert call.interaction is not None
        assert call.interaction.resolution == "unknown"
        assert call.interaction.answers == ()

    valid_path = tmp_path / "valid"
    valid_path.mkdir()
    snapshot, _ = _load(
        valid_path,
        [
            _question_call("question-2"),
            _output("question-2", '{"answers":{"color":{"answers":["Blue"]}}}'),
        ],
    )
    call = next(event for event in snapshot.events if event.type == "tool_call")
    assert call.interaction is not None
    assert call.interaction.resolution == "answered"
    assert [(answer.question_index, answer.text) for answer in call.interaction.answers] == [
        (0, "Blue"),
    ]


def test_codex_async_acceptance_and_receipts_are_not_answers(tmp_path):
    for index, receipt in enumerate(
        (
            "Request accepted; continuing work.",
            '{"accepted":true,"task_id":"task-2"}',
        )
    ):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        snapshot, _ = _load(
            case_path,
            [
                _question_call("async-1", "request_user_input_async"),
                _output("async-1", receipt),
            ],
        )

        call = next(event for event in snapshot.events if event.type == "tool_call")
        assert call.interaction is not None
        assert call.interaction.resolution == "unknown"
        assert call.interaction.answers == ()

    linked_path = tmp_path / "linked"
    linked_path.mkdir()
    snapshot, _ = _load(
        linked_path,
        [
            _question_call("async-2", "request_user_input_async"),
            _output("async-2", '{"answers":{"color":{"answers":["Red"]}}}'),
        ],
    )
    call = next(event for event in snapshot.events if event.type == "tool_call")
    assert call.interaction is not None
    assert call.interaction.resolution == "answered"
    assert [answer.text for answer in call.interaction.answers] == ["Red"]


def test_codex_open_call_is_scoped_to_latest_turn(tmp_path):
    snapshot, _ = _load(
        tmp_path,
        [
            _message("user", "earlier request"),
            _call("stale-open-call", "bash", {"cmd": "old"}),
            _message("user", "latest request"),
            _message("assistant", "latest response"),
            _complete(),
        ],
    )

    assert snapshot.outcome.status == "done"
    assert snapshot.outcome.evidence.origin == "native"
    assert snapshot.outcome.evidence.field == "payload.type"


def test_codex_user_followed_by_open_call_is_unknown(tmp_path):
    snapshot, _ = _load(
        tmp_path,
        [
            _message("user", "run this"),
            _call("open-call", "bash", {"cmd": "long-running"}),
        ],
    )

    assert snapshot.outcome.status == "unknown"


def test_codex_task_complete_after_unmatched_call_is_native_completion(tmp_path):
    snapshot, _ = _load(
        tmp_path,
        [
            _message("user", "run this"),
            _call("completed-call", "bash", {"cmd": "finished"}),
            _complete(),
        ],
    )

    assert snapshot.outcome.status == "done"
    assert snapshot.outcome.evidence.origin == "native"
    assert snapshot.outcome.evidence.field == "payload.type"


def test_codex_completed_turn_does_not_leak_open_calls_into_later_tail(tmp_path):
    snapshot, _ = _load(
        tmp_path,
        [
            _message("user", "first turn"),
            _call("old-open-call", "bash", {"cmd": "finished by turn end"}),
            _complete(),
            _message("assistant", "later assistant-only turn"),
        ],
    )

    assert snapshot.outcome.status == "done"
    assert snapshot.outcome.evidence.origin == "inferred"
