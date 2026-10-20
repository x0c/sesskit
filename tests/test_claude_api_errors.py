"""Native assistant API failure evidence from Claude Code 2.1.304."""
from __future__ import annotations

import json

import pytest

from sesskit import derive_turns, load_activity, load_session_conversation, open_activity_reader, titles
from sesskit.activity import to_v1_dicts
from sesskit.parsers import claude
from sesskit.transcript import load_events


def rows(status=401, flagged=True):
    return [
        {"type": "user", "uuid": "u", "sessionId": "api-failure", "cwd": "/tmp/api-failure",
         "timestamp": "2026-10-20T00:00:00Z", "message": {"role": "user", "content": "Do the task."}},
        {"type": "assistant", "uuid": "a", "sessionId": "api-failure", "isApiErrorMessage": flagged,
         "error": "authentication_failed", "apiErrorStatus": status, "timestamp": "2026-10-20T00:00:01Z",
         "message": {"role": "assistant", "model": "<synthetic>" if flagged else "real-model",
                     "stop_reason": "stop_sequence" if flagged else "end_turn", "usage": {"output_tokens": 0},
                     "content": [{"type": "text", "text": f"API Error: {status} Request failed."}]}},
    ]


def write(path, entries):
    path.write_text("".join(json.dumps(row) + "\n" for row in entries), encoding="utf-8")
    return {"source": "claude", "path": str(path), "id": "api-failure"}


@pytest.mark.parametrize("status", [401, 429, 500])
def test_native_assistant_api_failure_is_aborted_in_every_projection(tmp_path, status):
    path = tmp_path / "api-failure.jsonl"
    session = write(path, rows(status))
    info = claude._build_session_info(str(path), {})
    assert info is not None
    assert info["status_tag"] == titles.STATUS_ABORTED
    assert str(status) in info["last_agent_msg"]
    assert info["completion_id"]
    snapshot = load_activity(session)
    assert snapshot.outcome.status == "aborted"
    assert snapshot.outcome.error.code == str(status)
    assert snapshot.outcome.evidence.origin == "native"
    failure = snapshot.events[-1]
    assert failure.error.http_status == status
    assert failure.error.evidence.field == "isApiErrorMessage"
    assert failure.usage is None
    assert derive_turns(snapshot, runtime="claude")[-1].outcome.status == "failed"
    assert to_v1_dicts(snapshot) == load_events(session)
    assert [m.role for m in load_session_conversation(session)] == ["user"]
    assert str(status) in load_session_conversation(session, include_errors=True)[-1].text
    reader = open_activity_reader(session)
    poll = reader.poll()
    assert list(poll.events) == list(snapshot.events)
    assert poll.outcome == snapshot.outcome


def test_error_prose_without_native_flag_remains_a_real_reply(tmp_path):
    session = write(tmp_path / "api-failure.jsonl", rows(flagged=False))
    snapshot = load_activity(session)
    assert snapshot.outcome.status == "done"
    assert snapshot.events[-1].error is None
    assert snapshot.events[-1].usage.model == "real-model"
    assert load_session_conversation(session)[-1].role == "assistant"


def test_incremental_error_then_recovery_matches_snapshot(tmp_path):
    path = tmp_path / "api-failure.jsonl"
    session = write(path, rows()[:1])
    reader = open_activity_reader(session)
    reader.poll()
    with path.open("a") as stream:
        stream.write(json.dumps(rows()[1]) + "\n")
    poll = reader.poll()
    assert poll.outcome.status == "aborted"
    assert poll.events[-1].error.http_status == 401
    recovery = [
        {"type": "user", "timestamp": "2026-10-20T00:00:02Z", "message": {"content": "Try again."}},
        {"type": "assistant", "timestamp": "2026-10-20T00:00:03Z",
         "message": {"role": "assistant", "model": "real-model", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "Recovered."}]}},
    ]
    with path.open("a") as stream:
        for row in recovery:
            stream.write(json.dumps(row) + "\n")
    poll = reader.poll()
    assert poll.outcome == load_activity(session).outcome
    assert poll.outcome.status == "done"
    assert poll.events[-1].text == "Recovered."
    assert poll.events[-1].error is None


def test_codex_question_reply_conformance_uses_the_readable_projection(tmp_path):
    from sesskit.conformance import verify_session

    path = tmp_path / "rollout.jsonl"
    envelope = ("<send_user_message_question_reply>" + json.dumps([
        {"questionItemId": "q", "question": "Which?", "answer": "Use the default"}
    ]) + "</send_user_message_question_reply>")
    write(path, [{"type": "response_item", "timestamp": "2026-10-20T00:00:00Z",
                  "payload": {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": envelope}]}}])
    session = {"source": "codex", "path": str(path), "id": "c"}
    assert load_session_conversation(session)[0].text == "Use the default"
    assert load_activity(session).events[0].text == envelope
    assert verify_session(session)["violations"] == []
