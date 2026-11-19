"""Comprehensive cross-runtime skill invocation & user-message separation tests."""

from __future__ import annotations

import filecmp
import json
import sqlite3
from pathlib import Path

from test_runtime_goldens import _write_opencode

from sesskit import (
    SKILLS_SCHEMA_ID,
    cli,
    derive_skill_invocations,
    load_activity,
    load_events,
    load_session_conversation,
    load_skills,
    normalize_skill_name,
    raw_skill_name,
    run_conformance,
    skill_snapshot_to_dict,
)
from sesskit.models import as_typed
from sesskit.parsers import claude, codex, common, cursor, kimi, pi


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return str(path)


def test_classify_tool_and_name_normalization():
    assert common.classify_tool("Skill") == "skill"
    assert common.classify_tool("skill") == "skill"
    assert common.classify_tool("load_skill") == "skill"
    assert common.classify_tool("use_skill") == "skill"
    assert raw_skill_name(" /plugin:review ") == "plugin:review"
    assert raw_skill_name("$doc-update") == "doc-update"
    assert raw_skill_name("@grilling") == "grilling"
    assert normalize_skill_name("/plugin:review") == "review"


def test_claude_explicit_builtin_passive_and_is_meta_list_exclusion(tmp_path):
    path = Path(
        _write_jsonl(
            tmp_path / "claude-skill.jsonl",
            [
                {
                    "type": "attachment",
                    "timestamp": "2026-11-16T00:00:00Z",
                    "attachment": {
                        "type": "skill_listing",
                        "content": "- plugin:review: Review changes\n- doc-update: Update docs",
                    },
                },
                {
                    "type": "user",
                    "uuid": "u0",
                    "timestamp": "2026-11-16T00:00:01Z",
                    "message": {"role": "user", "content": "<command-name>/cost</command-name>\n<command-message>cost</command-message>\n<command-args></command-args>"},
                },
                {
                    "type": "user",
                    "uuid": "u1",
                    "timestamp": "2026-11-16T00:00:02Z",
                    "message": {
                        "role": "user",
                        "content": "<command-message>review</command-message>\n<command-name>/plugin:review</command-name>\n<command-args>fix auth</command-args>",
                    },
                },
                {
                    "type": "user",
                    "uuid": "u1-meta",
                    "isMeta": True,
                    "timestamp": "2026-11-16T00:00:02Z",
                    "message": {
                        "role": "user",
                        "content": "Base directory for this skill: /home/user/.claude/plugins/plugin/skills/review\nInternal instructions",
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "timestamp": "2026-11-16T00:00:03Z",
                    "message": {
                        "id": "msg_1",
                        "role": "assistant",
                        "content": [
                            # Companion Skill call for the same skill in the same logical round -> deduped!
                            {"type": "tool_use", "id": "tu-1", "name": "Skill", "input": {"skill": "plugin:review"}},
                            {"type": "text", "text": "Reviewing auth"},
                        ],
                        "stop_reason": "end_turn",
                    },
                },
                {
                    "type": "user",
                    "uuid": "u2",
                    "origin": {"kind": "human"},
                    "timestamp": "2026-11-16T00:00:04Z",
                    "message": {"role": "user", "content": "Also check <command-args>literal</command-args> and docs"},
                },
                {
                    "type": "assistant",
                    "uuid": "a2",
                    "timestamp": "2026-11-16T00:00:05Z",
                    "message": {
                        "id": "msg_2",
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "tu-2", "name": "Skill", "input": {"skill": "doc-update"}},
                            {"type": "tool_use", "id": "tu-3", "name": "Edit", "input": {"file_path": "/repo/.claude/skills/doc-init/SKILL.md"}},
                            {"type": "tool_use", "id": "tu-4", "name": "Read", "input": {"file_path": "/repo/.claude/skills/grilling/SKILL.md"}},
                            {"type": "text", "text": "Done"},
                        ],
                        "stop_reason": "end_turn",
                    },
                },
            ],
        )
    )
    # Stray <command-args> in prose must NOT be unwrapped.
    assert claude.extract_text("Also check <command-args>literal</command-args> and docs") == (
        "Also check <command-args>literal</command-args> and docs"
    )
    # Non-isMeta Base directory expansion must NOT look like human input.
    assert claude.extract_text("Base directory for this skill: /skills/x\nBody") is None

    session = {"source": "claude", "id": "claude-skill", "path": str(path)}
    snap = load_activity(session)
    assert snap.state == "available"
    # Built-in /cost has no skill_invocation; /plugin:review has explicit; companion Skill(plugin:review) is deduped!
    invs = snap.skill_invocations
    assert [(i.name, i.trigger, i.detection, i.evidence.origin) for i in invs] == [
        ("plugin:review", "explicit", "command", "native"),
        ("doc-update", "auto", "skill_tool", "native"),
        ("grilling", "auto", "file_read", "inferred"),
    ]
    assert invs[0].arguments == "fix auth"
    assert invs[0].path == "/home/user/.claude/plugins/plugin/skills/review"
    assert invs[0].bare_name == "review"
    # Edit on SKILL.md (doc-init) is NEVER counted as a skill invocation!
    assert all(i.name != "doc-init" for i in invs)
    # Typed user_message preserves skills on as_typed view.
    u1 = next(e for e in snap.events if e.type == "user_message" and e.text == "fix auth")
    typed_u1 = as_typed(u1)
    assert len(typed_u1.skills) == 1 and typed_u1.skills[0].name == "plugin:review"

    # List head/tail must skip isMeta even when isMeta is the trailing user row.
    path_tail_meta = Path(
        _write_jsonl(
            tmp_path / "claude-meta-tail.jsonl",
            [
                {"type": "user", "timestamp": "2026-11-16T00:00:01Z", "message": {"role": "user", "content": "Real prompt"}},
                {"type": "assistant", "timestamp": "2026-11-16T00:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}], "stop_reason": "end_turn"}},
                {"type": "user", "isMeta": True, "timestamp": "2026-11-16T00:00:03Z", "message": {"role": "user", "content": "Base directory for this skill: /x\nBody"}},
            ],
        )
    )
    info = claude.refresh_session({"source": "claude", "id": "claude-meta-tail", "path": str(path_tail_meta)})
    assert info is not None
    assert info["last_user_msg"] == "Real prompt"
    assert "Done" in info["last_agent_msg"]


def test_codex_markdown_links_multi_skill_system_paths_and_exclusions(tmp_path):
    rollout = Path(
        _write_jsonl(
            tmp_path / "rollout-2026-11-18T10-00-00-11111111-1111-4111-8111-111111111111.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "cx1", "cwd": str(tmp_path)}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "See ~/.codex/skills/dev-doc/SKILL.md"}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "world_state",
                        "state": {"host_skills": {"body": "/Users/x/.codex/skills/host-only/SKILL.md"}},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": "[$ui-prototyper](~/.config/agentsync/skills/ui-prototyper/SKILL.md) and $skill-creator fix $json",
                        }],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": "<skill>\n<name>ui-prototyper</name>\n<path>~/.config/agentsync/skills/ui-prototyper/SKILL.md</path>\nBody 1\n</skill>",
                        }],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": "<skill>\n<name>skill-creator</name>\n<path>/Users/x/.codex/skills/.system/skill-creator/SKILL.md</path>\nBody 2\n</skill>",
                        }],
                    },
                },
                # Companion cat of ui-prototyper SKILL.md in same round -> deduped!
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "c1",
                        "arguments": json.dumps({"cmd": "cat ~/.config/agentsync/skills/ui-prototyper/SKILL.md"}),
                    },
                },
                # Search & apply_patch on SKILL.md -> NEVER counted!
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "c2",
                        "input": 'exec_command({"cmd":"rg SKILL.md ~/.agents/skills/search-hit/SKILL.md"})',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": "c3",
                        "input": "*** Update File: /Users/x/.agents/skills/edited-skill/SKILL.md",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Second turn without explicit skill"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "c4",
                        "arguments": json.dumps({"cmd": "sed -n '1,80p' /Users/x/.codex/skills/.system/imagegen/SKILL.md"}),
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-11-18T10:00:05Z",
                    "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "Done"},
                },
            ],
        )
    )
    session = {"source": "codex", "id": "cx1", "path": str(rollout)}
    info = codex.refresh_session(session)
    assert info is not None
    # Markdown link collapsed cleanly so raw ~/.config/... path does not pollute title/excerpt.
    assert info["first_user_msg"] == "$ui-prototyper and $skill-creator fix $json"

    snap = load_activity(session)
    invs = snap.skill_invocations
    assert [(i.name, i.trigger, i.detection, i.evidence.origin) for i in invs] == [
        ("ui-prototyper", "explicit", "attached", "native"),
        ("skill-creator", "explicit", "attached", "native"),
        ("imagegen", "auto", "file_read", "inferred"),
    ]
    assert all(i.name not in {"json", "dev-doc", "host-only", "search-hit", "edited-skill"} for i in invs)


def test_codex_item_completed_user_skill_parts(tmp_path):
    rollout = Path(
        _write_jsonl(
            tmp_path / "rollout-2026-11-18T11-00-00-22222222-2222-4222-8222-222222222222.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "cx-item", "cwd": str(tmp_path)}},
                {
                    "type": "turn_context",
                    "payload": {"turn_id": "t-steer", "cwd": str(tmp_path)},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "阶段性总结一下 $doc-update"}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "turn_id": "t-steer",
                        "item": {
                            "type": "UserMessage",
                            "id": "um-steer",
                            "content": [
                                {"type": "text", "text": "阶段性总结一下 $doc-update"},
                                {
                                    "type": "skill",
                                    "name": "doc-update",
                                    "path": "/Users/test/.codex/skills/doc-update/SKILL.md",
                                },
                            ],
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-11-18T11:00:05Z",
                    "payload": {"type": "task_complete", "turn_id": "t-steer", "last_agent_message": "Done 1"},
                },
                {
                    "type": "turn_context",
                    "payload": {"turn_id": "t-both", "cwd": str(tmp_path)},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Run $swarm-manager please"}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "turn_id": "t-both",
                        "item": {
                            "type": "UserMessage",
                            "id": "um-both",
                            "content": [
                                {"type": "text", "text": "Run $swarm-manager please"},
                                {
                                    "type": "skill",
                                    "name": "swarm-manager",
                                    "path": "/Users/test/.codex/skills/swarm-manager/SKILL.md",
                                },
                            ],
                        },
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "<skill>\n<name>swarm-manager</name>\n<path>/Users/test/.codex/skills/swarm-manager/SKILL.md</path>\nBody\n</skill>",
                            }
                        ],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-11-18T11:00:10Z",
                    "payload": {"type": "task_complete", "turn_id": "t-both", "last_agent_message": "Done 2"},
                },
            ],
        )
    )
    session = {"source": "codex", "id": "cx-item", "path": str(rollout)}
    conv = load_session_conversation(session)
    assert [m.text for m in conv if m.role == "user"] == [
        "阶段性总结一下 $doc-update",
        "Run $swarm-manager please",
    ]
    events_v1 = load_events(session)
    assert [e["text"] for e in events_v1 if e["type"] == "user_message"] == [
        "阶段性总结一下 $doc-update",
        "Run $swarm-manager please",
    ]

    snap = load_skills(session)
    assert [(i.name, i.trigger, i.detection, i.evidence.origin, i.evidence.field) for i in snap.invocations] == [
        ("doc-update", "explicit", "attached", "native", "item_completed.UserMessage.skill"),
        ("swarm-manager", "explicit", "attached", "native", "item_completed.UserMessage.skill"),
    ]


def test_pi_non_greedy_multi_block_catalog_and_bash_filtering(tmp_path):
    skill_a = (
        '<skill name="alpha-skill" location="/tmp/.agents/skills/alpha-skill/SKILL.md">\n'
        "Example </skill> inside body\n</skill>"
    )
    skill_b = (
        '<skill name="beta-skill" location="/tmp/.agents/skills/beta-skill/SKILL.md">\n'
        "Beta body\n</skill>\n\nrun both"
    )
    path = Path(
        _write_jsonl(
            tmp_path / "pi-skills.jsonl",
            [
                {"type": "session", "id": "pi-s1", "cwd": str(tmp_path), "timestamp": "2026-11-18T00:00:00Z"},
                {
                    "type": "message",
                    "id": "sys1",
                    "parentId": None,
                    "message": {
                        "role": "system",
                        "content": "",
                        "sections": {
                            "skills": "<available_skills><skill><name>gamma-catalog-name</name>"
                            "<location>/custom/path/gamma-dir/SKILL.md</location></skill></available_skills>"
                        },
                    },
                },
                {"type": "message", "id": "u1", "parentId": "sys1", "message": {"role": "user", "content": f"{skill_a}\n\n{skill_b}"}},
                {
                    "type": "message",
                    "id": "a1",
                    "parentId": "u1",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "content": [
                            {"type": "toolCall", "id": "tc1", "name": "read", "arguments": {"path": "/custom/path/gamma-dir/SKILL.md"}},
                            {"type": "toolCall", "id": "tc2", "name": "edit", "arguments": {"path": "/tmp/.agents/skills/edited-one/SKILL.md"}},
                            {"type": "toolCall", "id": "tc3", "name": "bash", "arguments": {"command": "cat /tmp/.agents/skills/delta-skill/SKILL.md"}},
                            {"type": "toolCall", "id": "tc4", "name": "bash", "arguments": {"command": "rg foo /tmp/.agents/skills/rg-hit/SKILL.md"}},
                        ],
                    },
                },
            ],
        )
    )
    assert pi.user_message_text(f"{skill_a}\n\n{skill_b}") == "run both"

    snap = load_activity({"source": "pi", "id": "pi-s1", "path": str(path)})
    invs = snap.skill_invocations
    assert [(i.name, i.trigger, i.detection, i.evidence.origin) for i in invs] == [
        ("alpha-skill", "explicit", "command", "native"),
        ("beta-skill", "explicit", "command", "native"),
        ("gamma-catalog-name", "auto", "file_read", "inferred"),
        ("delta-skill", "auto", "file_read", "inferred"),
    ]


def test_opencode_v1_and_v2_explicit_and_passive_skills(tmp_path):
    # v1: skill tool call + read SKILL.md
    s1 = _write_opencode(
        tmp_path,
        "v1",
        [
            ("u1", {"role": "user"}, [("p1", {"type": "text", "text": "help"})]),
            (
                "a1",
                {"role": "assistant", "finish": "stop"},
                [
                    (
                        "p2",
                        {
                            "type": "tool",
                            "tool": "skill",
                            "callID": "c1",
                            "state": {
                                "status": "completed",
                                "input": {"name": "gitnexus-impact-analysis"},
                                "metadata": {"name": "gitnexus-impact-analysis", "dir": "/skills/gitnexus"},
                                "output": "<skill_content>...</skill_content>",
                            },
                        },
                    ),
                    (
                        "p3",
                        {
                            "type": "tool",
                            "tool": "read",
                            "callID": "c2",
                            "state": {"status": "completed", "input": {"filePath": "/tmp/.agents/skills/grilling/SKILL.md"}, "output": "..."},
                        },
                    ),
                ],
            ),
        ],
    )
    invs1 = load_skills(s1).invocations
    assert [(i.name, i.trigger, i.detection) for i in invs1] == [
        ("gitnexus-impact-analysis", "auto", "skill_tool"),
        ("grilling", "auto", "file_read"),
    ]
    assert [e["kind"] for e in load_events(s1) if e["type"] == "tool_call"] == ["skill", "read"]

    # v2: explicit user.data.skills[] + type='skill' + location-switched tail preservation
    v2_dir = tmp_path / "v2"
    v2_dir.mkdir()
    s2 = _write_opencode(
        v2_dir,
        "v2",
        [
            ("u1", 1, "user", {"text": "use @doc-update", "skills": [{"id": "doc-update"}]}),
            ("sk1", 2, "skill", {"skill": "doc-init", "text": "[Skill activated: doc-init]"}),
            (
                "a1",
                3,
                "assistant",
                {
                    "finish": "stop",
                    "content": [
                        {
                            "type": "tool",
                            "name": "skill",
                            "id": "c1",
                            "state": {
                                "status": "completed",
                                "input": {"id": "doc-update"},  # Same skill as explicit -> deduped!
                                "metadata": {"name": "doc-update"},
                                "content": [{"type": "text", "text": "ok"}],
                            },
                        },
                        {
                            "type": "tool",
                            "name": "skill",
                            "id": "c2",
                            "state": {
                                "status": "completed",
                                "input": {},
                                "metadata": {"name": "macos-window-screenshot"},
                                "content": [{"type": "text", "text": "ok"}],
                            },
                        },
                    ],
                },
            ),
            ("loc1", 4, "location-switched", {"cwd": "/tmp"}),
        ],
    )
    snap2 = load_activity(s2)
    assert snap2.outcome.status == "done"
    assert [(i.name, i.trigger, i.detection) for i in snap2.skill_invocations] == [
        ("doc-update", "explicit", "attached"),
        ("doc-init", "explicit", "attached"),
        ("macos-window-screenshot", "auto", "skill_tool"),
    ]
    # type='skill' must not appear in plain conversation as a fake human turn.
    plain2 = load_session_conversation(s2)
    assert [m.text for m in plain2 if m.role == "user"] == ["use @doc-update"]


def test_cursor_summary_replay_bug_multi_attachment_and_passive_read(tmp_path):
    db_path = tmp_path / "store.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        rows = [
            (
                "u1",
                {
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "<agent_skills><available_skills>"
                            '<agent_skill fullPath="/Users/x/.cursor/skills/grilling/SKILL.md">desc</agent_skill>'
                            "</available_skills></agent_skills>\n"
                            "<manually_attached_skills>\n"
                            "Skill Name: doc-update\nPath: /Users/x/.cursor/skills/doc-update/SKILL.md\n"
                            "Skill Name: doc-init\nPath: /Users/x/.cursor/skills/doc-init/SKILL.md\n"
                            "</manually_attached_skills>\n"
                            "<user_query>\nUpdate docs for repo\n</user_query>"
                        ),
                    }],
                },
            ),
            (
                "a1",
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool-call", "toolCallId": "tc1", "toolName": "Read", "args": {"path": "/Users/x/.cursor/skills/grilling/SKILL.md"}},
                        {"type": "tool-call", "toolCallId": "tc2", "toolName": "StrReplace", "args": {"path": "/Users/x/.cursor/skills/edited/SKILL.md"}},
                    ],
                },
            ),
            # Compacted summary containing historical <user_query> -> MUST NOT become a new human message!
            (
                "u_sum",
                {
                    "role": "user",
                    "content": "[Previous conversation summary]:\nAll user messages:\n<user_query>Old request</user_query>\n<manually_attached_skills>Skill Name: doc-compact</manually_attached_skills>",
                },
            ),
            # Plain slash mention WITHOUT <manually_attached_skills> -> NOT a skill invocation!
            (
                "u2",
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "<user_query>/doc-init</user_query>"}],
                },
            ),
        ]
        for bid, obj in rows:
            db.execute("INSERT INTO blobs VALUES (?, ?)", (bid, json.dumps(obj).encode("utf-8")))

    assert cursor.user_text_from_blob({"role": "user", "content": "[Previous conversation summary]: <user_query>Old</user_query>"}) is None
    session = {"source": "cursor", "id": "cur1", "path": str(db_path)}
    plain = load_session_conversation(session)
    assert [m.text for m in plain if m.role == "user"] == ["Update docs for repo", "/doc-init"]
    invs = load_skills(session).invocations
    assert [(i.name, i.trigger, i.detection) for i in invs] == [
        ("doc-update", "explicit", "attached"),
        ("doc-init", "explicit", "attached"),
        ("grilling", "auto", "file_read"),
    ]


def test_antigravity_explicit_vs_bare_slash_passive_read(tmp_path):
    path = Path(
        _write_jsonl(
            tmp_path / "transcript_full.jsonl",
            [
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "status": "DONE",
                    "step_index": 0,
                    "created_at": "2026-11-18T00:00:00Z",
                    "content": (
                        "<USER_REQUEST>\n/doc-update fix links\n</USER_REQUEST>\n"
                        "<ADDITIONAL_METADATA>\n/doc-update is a [Slash Command]:\n"
                        "<SKILL>The user has explicitly invoked the (doc-update) skill. Body</SKILL>\n"
                        "</ADDITIONAL_METADATA>"
                    ),
                },
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "status": "DONE",
                    "step_index": 1,
                    "created_at": "2026-11-18T00:01:00Z",
                    "content": (
                        "<USER_REQUEST>\nLet us use /grilling now\n</USER_REQUEST>\n"
                        "<ADDITIONAL_METADATA>\nThe current local time is 2026-11-18\n</ADDITIONAL_METADATA>"
                    ),
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "status": "DONE",
                    "step_index": 2,
                    "created_at": "2026-11-18T00:01:05Z",
                    "content": "Reading grilling skill.",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": "/Users/x/.gemini/antigravity-cli/skills/grilling/SKILL.md"}},
                        {"name": "write_to_file", "args": {"TargetFile": "/Users/x/.gemini/antigravity-cli/skills/other/SKILL.md"}},
                    ],
                },
            ],
        )
    )
    session = {"source": "antigravity", "id": "agy1", "path": str(path)}
    snap = load_activity(session)
    assert [(e.text, len(e.skills)) for e in snap.events if e.type == "user_message"] == [
        ("/doc-update fix links", 1),
        ("Let us use /grilling now", 0),
    ]
    invs = snap.skill_invocations
    assert [(i.name, i.trigger, i.detection, i.evidence.origin) for i in invs] == [
        ("doc-update", "explicit", "command", "native"),
        ("grilling", "auto", "file_read", "inferred"),
    ]
    assert invs[0].arguments == "fix links"


def test_kimi_skill_activation_cwd_and_passive_tools(tmp_path):
    ws_dir = tmp_path / "ws"
    sess_dir = ws_dir / "sess_1"
    agents_dir = sess_dir / "agents" / "main"
    agents_dir.mkdir(parents=True)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (sess_dir / "state.json").write_text(
        json.dumps({"cwd": str(proj_dir), "updatedAt": "2026-11-18T00:01:00Z"}),
        encoding="utf-8",
    )
    wire_path = agents_dir / "wire.jsonl"
    _write_jsonl(
        wire_path,
        [
            {
                "type": "llm.tools_snapshot",
                "tools": [{"name": "Skill", "description": "Use <skill-loaded>...</skill-loaded>"}],
            },
            {
                "type": "context.append_message",
                "time": 1790771937000,
                "message": {
                    "role": "user",
                    "origin": {
                        "kind": "skill_activation",
                        "skillName": "git-commits",
                        "trigger": "user-slash",
                        "skillArgs": "fix login",
                        "skillPath": "/skills/git-commits/SKILL.md",
                    },
                    "content": [{
                        "type": "text",
                        "text": 'User activated the skill "git-commits". Follow the loaded skill instructions.\n\n'
                        '<skill-loaded name="git-commits" trigger="user-slash" args="fix login">\nBody\n</skill-loaded>',
                    }],
                },
            },
            {
                "type": "context.append_loop_event",
                "time": 1790771938000,
                "event": {"type": "content.part", "part": {"type": "text", "text": "Checking"}},
            },
            {
                "type": "context.append_message",
                "time": 1790771939000,
                "message": {
                    "role": "user",
                    "origin": {
                        "kind": "user",
                        "skillActivations": [{"skillName": "doc-update", "skillPath": "/skills/doc-update/SKILL.md"}],
                    },
                    "content": [
                        {"type": "text", "text": '<skill-loaded name="doc-update" trigger="user-slash">\nBody\n</skill-loaded>'},
                        {"type": "text", "text": "Please update docs"},
                    ],
                },
            },
            {
                "type": "context.append_loop_event",
                "time": 1790771940000,
                "event": {"type": "tool.call", "toolCallId": "k1", "name": "Skill", "args": {"skill": "grilling"}},
            },
            # Companion model-tool reminder in user channel -> excluded from human conversation & deduped!
            {
                "type": "context.append_message",
                "time": 1790771941000,
                "message": {
                    "role": "user",
                    "origin": {"kind": "skill_activation", "skillName": "grilling", "trigger": "model-tool"},
                    "content": [{"type": "text", "text": "Skill tool loaded instructions...\n<skill-loaded name=\"grilling\" trigger=\"model-tool\">Body</skill-loaded>"}],
                },
            },
            {
                "type": "context.append_loop_event",
                "time": 1790771942000,
                "event": {"type": "tool.call", "toolCallId": "k2", "name": "ReadFile", "args": {"path": "/Users/x/.kimi-code/skills/pdf/SKILL.md"}},
            },
        ],
    )
    built = kimi._build_session_info(str(sess_dir), "sess_1")
    assert built is not None
    assert built["cwd"] == str(proj_dir)
    assert built["first_user_msg"] == "fix login"

    session = {"source": "kimi", "id": "sess_1", "path": str(wire_path)}
    assert load_activity(session).state == "unsupported"
    plain = load_session_conversation(session)
    assert [m.text for m in plain if m.role == "user"] == ["fix login", "Please update docs"]

    snap = load_skills(session)
    assert snap.state == "available"
    assert [(i.name, i.trigger, i.detection) for i in snap.invocations] == [
        ("git-commits", "explicit", "command"),
        ("doc-update", "explicit", "attached"),
        ("grilling", "auto", "skill_tool"),
        ("pdf", "auto", "file_read"),
    ]


def test_skills_cli_schema_and_conformance(tmp_path, capsys):
    root = Path(__file__).resolve().parent.parent
    schema_main = root / "schemas" / "skills.v1.json"
    schema_pkg = root / "src" / "sesskit" / "schemas" / "skills.v1.json"
    assert filecmp.cmp(schema_main, schema_pkg, shallow=False)
    schema = json.loads(schema_main.read_text(encoding="utf-8"))

    pi_path = Path(
        _write_jsonl(
            tmp_path / "pi-cli.jsonl",
            [
                {"type": "session", "id": "pi-cli-1", "cwd": str(tmp_path), "timestamp": "2026-11-18T00:00:00Z"},
                {
                    "type": "message",
                    "id": "u1",
                    "parentId": None,
                    "message": {
                        "role": "user",
                        "content": '<skill name="doc-update" location="/tmp/doc-update/SKILL.md">\nBody\n</skill>\n\nrefresh',
                    },
                },
                {
                    "type": "message",
                    "id": "a1",
                    "parentId": "u1",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "ok"}],
                    },
                },
            ],
        )
    )
    session = {"source": "pi", "id": "pi-cli-1", "path": str(pi_path)}
    snap = load_activity(session)
    report = run_conformance(
        "pi",
        snapshot=snap,
        v1_events=load_events(session),
        conversation=load_session_conversation(session),
    )
    assert report.passed, report.violations
    assert derive_skill_invocations(snap)[0].name == "doc-update"

    def _assert_matches_schema(obj: dict) -> None:
        for req in schema["required"]:
            assert req in obj
        assert obj["schema"] == schema["properties"]["schema"]["const"]
        if "state" in obj:
            assert obj["state"] in schema["properties"]["state"]["enum"]
        inv_def = schema["$defs"]["invocation"]
        for inv in obj.get("invocations", ()):
            for req in inv_def["required"]:
                assert req in inv
            assert inv["trigger"] in inv_def["properties"]["trigger"]["enum"]
            assert inv["detection"] in inv_def["properties"]["detection"]["enum"]
            assert inv["evidence"]["origin"] in schema["$defs"]["evidence"]["properties"]["origin"]["enum"]
        sum_def = schema["$defs"]["summaryRow"]
        for row in obj.get("summary", ()):
            for req in sum_def["required"]:
                assert req in row

    payload = skill_snapshot_to_dict(load_skills(session))
    assert payload["schema"] == SKILLS_SCHEMA_ID
    _assert_matches_schema(payload)

    from unittest import mock

    from sesskit.models import make_session_info
    from sesskit.registry import ParserRegistry, RuntimeParser

    info = make_session_info(
        source="pi",
        id="pi-cli-1",
        short_id="pi-cli-1",
        cwd=str(tmp_path),
        mtime=1_700_000_000.0,
        time_source="file_mtime",
        event_time=1_700_000_000.0,
        file_mtime=1_700_000_000.0,
        size_bytes=100,
        native_title=None,
        fallback_title="refresh",
        status_tag="✅已完成",
        path=str(pi_path),
    )
    reg = ParserRegistry([
        RuntimeParser(id="pi", display_name="Pi", _scan=lambda limit=50, **_: [info], _load=load_session_conversation),
    ])
    with mock.patch.object(cli, "default_registry", return_value=reg):
        code = cli.dispatch(["skills", "pi:pi-cli-1", "--json"])
        assert code == 0
        single = json.loads(capsys.readouterr().out)
        assert single["ok"] is True
        _assert_matches_schema(single["data"])
        assert single["data"]["summary"][0]["explicit_count"] == 1

        code2 = cli.dispatch(["skills", "--detail", "invocations", "--json"])
        assert code2 == 0
        multi = json.loads(capsys.readouterr().out)
        assert multi["ok"] is True
        _assert_matches_schema(multi["data"])
        assert multi["data"]["total_invocations"] == 1

        code3 = cli.dispatch(["skills", "--runtime", "pi", "--trigger", "explicit", "--json"])
        assert code3 == 0
        filtered = json.loads(capsys.readouterr().out)
        assert filtered["ok"] is True
        assert filtered["data"]["total_invocations"] == 1

        code4 = cli.dispatch(["skills", "--runtime", "pi", "--trigger", "auto", "--json"])
        assert code4 == 0
        auto_only = json.loads(capsys.readouterr().out)
        assert auto_only["ok"] is True
        assert auto_only["data"]["total_invocations"] == 0

        code5 = cli.dispatch(["describe", "skills"])
        assert code5 == 0
        desc = json.loads(capsys.readouterr().out)
        assert desc["data"]["name"] == "skills"
