"""Independent native skill shapes across all six actively integrated runtimes."""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_runtime_goldens import _write_opencode

from sesskit import load_activity, load_events, load_session_conversation, open_activity_reader
from sesskit.parsers import claude, codex

TASK = "修复用户消息和会话标题"
INSTRUCTIONS = "SKILL_INSTRUCTION_SENTINEL: internal workflow, not the human task."
CODEX_CONTEXT = (
    "<skill>\n<name>review</name>\n<path>/skills/review/SKILL.md</path>\n"
    + INSTRUCTIONS + "\n</skill>"
)


def write_rows(tmp_path, runtime, rows):
    filename = (
        "rollout-2026-11-16T10-00-00-11111111-1111-4111-8111-111111111111.jsonl"
        if runtime == "codex" else f"{runtime}.jsonl"
    )
    path = tmp_path / filename
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return {"source": runtime, "path": str(path), "id": "native-skill"}


def codex_message(text):
    return {"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}],
    }}


def claude_command(args):
    return (
        "<command-message>review</command-message>\n"
        "<command-name>/review</command-name>\n"
        f"<command-args>{args}</command-args>"
    )


@pytest.mark.parametrize("runtime", ["claude", "codex", "cursor", "pi", "antigravity", "opencode-v1", "opencode-v2"])
@pytest.mark.parametrize("arguments", [TASK, ""])
def test_native_skill_views_keep_one_human_invocation(tmp_path, runtime, arguments):
    if runtime == "claude":
        expected = arguments or "/review"
        session = write_rows(tmp_path, runtime, [
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": claude_command(arguments)}},
            {"type": "user", "uuid": "ctx", "isMeta": True,
             "message": {"role": "user", "content": "Base directory for this skill: /skills/review\n" + INSTRUCTIONS}},
        ])
    elif runtime == "codex":
        expected = "$review" + (" " + arguments if arguments else "")
        session = write_rows(tmp_path, runtime, [codex_message(expected), codex_message(CODEX_CONTEXT)])
    elif runtime == "pi":
        expected = arguments or "/skill:review"
        expanded = '<skill name="review" location="/skills/review/SKILL.md">\n' + INSTRUCTIONS + "\n</skill>"
        if arguments:
            expanded += "\n\n" + arguments
        session = write_rows(tmp_path, runtime, [
            {"type": "session", "id": "native-skill", "cwd": str(tmp_path)},
            {"type": "message", "id": "u1", "message": {"role": "user", "content": expanded}},
        ])
    elif runtime == "antigravity":
        expected = "/review" + (" " + arguments if arguments else "")
        session = write_rows(tmp_path, runtime, [{
            "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "step_index": 0,
            "content": f"<USER_REQUEST>\n{expected}\n</USER_REQUEST>\n"
                       f"<ADDITIONAL_METADATA><SKILL>{INSTRUCTIONS}</SKILL></ADDITIONAL_METADATA>",
        }])
    elif runtime == "cursor":
        expected = "/review" + (" " + arguments if arguments else "")
        path = tmp_path / "store.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
            db.execute("INSERT INTO blobs VALUES (?, ?)", ("u1", json.dumps({
                "role": "user", "content": [{"type": "text", "text":
                    f"<manually_attached_skills>{INSTRUCTIONS}</manually_attached_skills>\n"
                    f"<user_query>{expected}</user_query>"}],
            }).encode()))
        session = {"source": runtime, "path": str(path), "id": "native-skill"}
    else:
        expected = "@review" + (" " + arguments if arguments else "")
        if runtime.endswith("v1"):
            rows = [
                ("u1", {"role": "user"}, [("p1", {"type": "text", "text": expected})]),
                ("a1", {"role": "assistant", "finish": "tool-calls"}, [("p2", {
                    "type": "tool", "tool": "skill", "callID": "skill-1",
                    "state": {"status": "completed", "input": {"name": "review"}, "output": INSTRUCTIONS},
                })]),
            ]
            session = _write_opencode(tmp_path, "v1", rows)
        else:
            session = _write_opencode(tmp_path, "v2", [
                ("u1", 1, "user", {"text": expected}),
                ("a1", 2, "assistant", {"finish": "tool-calls", "content": [{
                    "type": "tool", "name": "skill", "id": "skill-1", "state": {
                        "status": "completed", "input": {"id": "review"},
                        "content": [{"type": "text", "text": INSTRUCTIONS}],
                    },
                }]}),
            ])

    plain = load_session_conversation(session)
    assert [m.text for m in plain if m.role == "user"] == [expected]
    assert [e["text"] for e in load_events(session) if e["type"] == "user_message"] == [expected]
    snapshot = load_activity(session)
    human = [e.text for e in snapshot.events if e.type == "user_message" and e.origin != "injected"]
    assert human == [expected]
    assert all(INSTRUCTIONS not in (m.text or "") for m in plain)
    if runtime == "codex":
        assert any(e.origin == "injected" and e.text == CODEX_CONTEXT for e in snapshot.events)
    reader = open_activity_reader(session)
    observed = reader.poll()
    assert observed.events == snapshot.events
    assert reader.poll().events == ()


@pytest.mark.parametrize("content", ["string", "parts"])
@pytest.mark.parametrize("args", [TASK, ""])
def test_claude_command_metadata_has_identical_text_and_part_semantics(content, args):
    raw = claude_command(args)
    value = raw if content == "string" else [{"type": "text", "text": raw}]
    assert claude.extract_text(value) == (args or "/review")
    assert claude.extract_text("<command-name>/cost</command-name>") is None


@pytest.mark.parametrize("text", [
    "Please explain <skill> and </skill>",
    "<skill>not native metadata</skill>",
    "<skill><name>review</name>body without path</skill>",
    CODEX_CONTEXT + "\nThis is a real user continuation",
    "```xml\n" + CODEX_CONTEXT + "\n```",
])
def test_codex_malformed_quoted_and_mixed_content_stays_human(text):
    assert codex.user_message_text(codex_message(text)) == text


def test_codex_context_does_not_own_list_excerpt_or_reset_a_finished_turn(tmp_path):
    from sesskit.parsers import codex as parser

    session = write_rows(tmp_path, "codex", [
        {"type": "session_meta", "payload": {"id": "native-skill", "cwd": str(tmp_path)}},
        codex_message(TASK),
        {"type": "event_msg", "timestamp": "2026-11-16T10:00:00Z",
         "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "Done"}},
        codex_message(CODEX_CONTEXT),
    ])
    info = parser.refresh_session(session)
    assert info is not None
    assert info["first_user_msg"] == TASK
    assert info["last_user_msg"] == TASK
    assert INSTRUCTIONS not in info["fallback_title"]
    assert load_activity(session).outcome.status == "done"
