"""Tests for Antigravity session scanning, transcript projection, and live parity."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from sesskit import registry, titles
from sesskit.adapters import get_adapter
from sesskit.parsers import antigravity as ag
from sesskit.parsers.common import HostExtension, classify_tool
from sesskit.transcript import load_events


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _init_summaries_db(
    root: Path,
    rows: list[tuple[str, str, str, str, int, int, str, int]],
    parents: list[tuple[str, str]] = (),
) -> Path:
    """Create a conversation_summaries.db fixture.

    Row tuple: (cid, title, preview, uris_json, step_count, killed, parent_cid, depth)
    """
    db_path = root / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                preview TEXT NOT NULL DEFAULT '',
                step_count INTEGER NOT NULL DEFAULT 0,
                last_modified_time TEXT NOT NULL DEFAULT '',
                workspace_uris TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                killed INTEGER NOT NULL DEFAULT 0,
                last_user_input_time TEXT NOT NULL DEFAULT '',
                app_data_dir TEXT NOT NULL DEFAULT '',
                parent_conversation_id TEXT NOT NULL DEFAULT '',
                nesting_depth INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE conversation_parents (
                conversation_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL
            );
            """
        )
        for cid, title, preview, uris, steps, killed, parent_cid, depth in rows:
            conn.execute(
                "INSERT INTO conversation_summaries ("
                "conversation_id, title, preview, step_count, last_modified_time,"
                " workspace_uris, status, killed, last_user_input_time,"
                " app_data_dir, parent_conversation_id, nesting_depth"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    title,
                    preview,
                    steps,
                    "2026-11-10T10:00:10Z",
                    uris,
                    "CASCADE_RUN_STATUS_IDLE",
                    killed,
                    "2026-11-10T10:00:00Z",
                    str(root),
                    parent_cid,
                    depth,
                ),
            )
        for cid, pid in parents:
            conn.execute(
                "INSERT INTO conversation_parents (conversation_id, parent_id) VALUES (?, ?)",
                (cid, pid),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _write_transcript(root: Path, cid: str, rows: list[dict], *, full: bool = True) -> Path:
    name = "transcript_full.jsonl" if full else "transcript.jsonl"
    path = root / "brain" / cid / ".system_generated" / "logs" / name
    return _write_jsonl(path, rows)


def _scan_root(root: Path, **kwargs) -> list[dict]:
    with mock.patch.object(ag, "_roots", return_value=[str(root)]):
        return ag.scan_sessions(**kwargs)


def test_subagents_excluded_via_summaries_and_parents_table(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    _init_summaries_db(
        root,
        [
            ("top-1", "Top session", "", uris, 2, 0, "", 0),
            ("sub-parent", "Sub by parent col", "", uris, 2, 0, "top-1", 0),
            ("sub-depth", "Sub by depth", "", uris, 2, 0, "", 1),
            ("sub-link", "Sub by link table", "", uris, 2, 0, "", 0),
        ],
        parents=[("sub-link", "top-1"), ("sub-only-link", "top-1")],
    )
    sample_rows = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-10T10:00:00Z",
            "content": "<USER_REQUEST>hello world</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-10T10:00:05Z",
            "content": "hi there",
        },
    ]
    for cid in ("top-1", "sub-parent", "sub-depth", "sub-link", "sub-only-link"):
        _write_transcript(root, cid, sample_rows)

    sessions = _scan_root(root, limit=20)
    assert [s["id"] for s in sessions] == ["top-1"]
    assert sessions[0]["status_tag"] == titles.STATUS_DONE


def test_pbtxt_annotation_title_fallback_and_summary_precedence(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    (root / "annotations").mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    _init_summaries_db(
        root,
        [
            ("cid-empty-title", "", "", uris, 2, 0, "", 0),
            ("cid-has-title", "Summary Title", "", uris, 2, 0, "", 0),
        ],
    )
    (root / "annotations" / "cid-empty-title.pbtxt").write_text(
        'title:"Fallback \\"PBTXT\\" Title"\n', encoding="utf-8"
    )
    (root / "annotations" / "cid-has-title.pbtxt").write_text(
        'title: "Should Not Override"\n', encoding="utf-8"
    )
    (root / "annotations" / "cid-no-summary.pbtxt").write_text(
        'title: "Orphan Annotation Title"\n', encoding="utf-8"
    )
    rows = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-10T10:00:00Z",
            "content": "<USER_REQUEST>fix bug</USER_REQUEST>",
        }
    ]
    for cid in ("cid-empty-title", "cid-has-title", "cid-no-summary"):
        _write_transcript(root, cid, rows)

    sessions = _scan_root(root, limit=20, include_missing_cwd=True)
    by_id = {s["id"]: s for s in sessions}
    assert by_id["cid-empty-title"]["native_title"] == 'Fallback "PBTXT" Title'
    assert by_id["cid-has-title"]["native_title"] == "Summary Title"
    assert by_id["cid-no-summary"]["native_title"] == "Orphan Annotation Title"


def test_synthetic_title_prompt_and_host_extension_filtering(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    eph = tmp_path / "oc-manager-run1"
    eph.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    _init_summaries_db(
        root,
        [
            ("real-1", "Real", "", json.dumps([f"file://{proj}"]), 1, 0, "", 0),
            ("noise-1", "", "", json.dumps([f"file://{proj}"]), 1, 0, "", 0),
            ("eph-1", "Auto", "", json.dumps([f"file://{eph}"]), 1, 0, "", 0),
        ],
    )
    _write_transcript(
        root,
        "real-1",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2026-11-10T10:00:00Z",
                "content": "<USER_REQUEST>WRAP: real prompt</USER_REQUEST>",
            }
        ],
    )
    _write_transcript(
        root,
        "noise-1",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2026-11-10T10:00:00Z",
                "content": f"<USER_REQUEST>{titles.PROMPT_MARKER} session excerpt</USER_REQUEST>",
            }
        ],
    )
    _write_transcript(
        root,
        "eph-1",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2026-11-10T10:00:00Z",
                "content": "<USER_REQUEST>automated work</USER_REQUEST>",
            }
        ],
    )

    neutral = _scan_root(root, limit=20)
    assert {s["id"] for s in neutral} == {"real-1", "noise-1", "eph-1"}

    host = HostExtension(
        name="corral",
        title_prompt_marker=titles.PROMPT_MARKER,
        ephemeral_prefixes=("oc-manager-",),
        excerpt_preprocess=lambda text: text.removeprefix("WRAP: "),
    )
    with mock.patch.object(ag, "_roots", return_value=[str(root)]):
        hosted_direct = ag.scan_sessions(limit=20, host=host)
        hosted_registry = (
            registry.default_registry().get("antigravity").scan_sessions(limit=20, host=host)
        )

    assert [s["id"] for s in hosted_direct] == ["real-1"]
    assert hosted_direct[0]["first_user_msg"] == "real prompt"
    assert [s["id"] for s in hosted_registry] == ["real-1"]


def test_abnormal_endings_killed_and_tail_error_vs_recovered_error(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    (root / "conversations").mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    _init_summaries_db(
        root,
        [
            ("killed-transcript", "Killed", "", uris, 2, 1, "", 0),
            ("error-tail", "Rate limited", "", uris, 2, 0, "", 0),
            ("error-recovered", "Recovered", "", uris, 3, 0, "", 0),
            ("killed-sqlite", "Killed SQLite", "partial preview", uris, 0, 1, "", 0),
        ],
    )
    _write_transcript(
        root,
        "killed-transcript",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:00Z",
                "content": "<USER_REQUEST>run long task</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "RUNNING",
                "created_at": "2026-11-10T10:00:02Z",
                "content": "working...",
            },
        ],
    )
    _write_transcript(
        root,
        "error-tail",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:00Z",
                "content": "<USER_REQUEST>try request</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "source": "SYSTEM",
                "type": "ERROR_MESSAGE",
                "status": "DONE",
                "error": "429 rate limit exceeded",
                "created_at": "2026-11-10T10:00:03Z",
            },
        ],
    )
    _write_transcript(
        root,
        "error-recovered",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:00Z",
                "content": "<USER_REQUEST>check</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "ERROR",
                "error": "file does not exist",
                "created_at": "2026-11-10T10:00:01Z",
                "content": "missing file",
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:04Z",
                "content": "recovered and finished",
            },
        ],
    )
    db_only = root / "conversations" / "killed-sqlite.db"
    sqlite3.connect(db_only).close()

    first_scan = {s["id"]: s for s in _scan_root(root, limit=20)}
    second_scan = {s["id"]: s for s in _scan_root(root, limit=20)}

    for aborted_id in ("killed-transcript", "error-tail", "killed-sqlite"):
        s = first_scan[aborted_id]
        assert s["status_tag"] == titles.STATUS_ABORTED
        assert s["completion_id"].startswith(f"{titles.STATUS_ABORTED}:")
        assert s["completion_id"] == second_scan[aborted_id]["completion_id"]

    assert first_scan["killed-transcript"]["terminal_error_kind"] == "user_interrupt"
    assert first_scan["killed-transcript"]["terminal_error_retryable"] is False

    assert first_scan["error-tail"]["terminal_error_kind"] == "rate_limited"
    assert first_scan["error-tail"]["terminal_error_retryable"] is True
    assert first_scan["error-tail"]["last_agent_msg"] == "429 rate limit exceeded"

    assert first_scan["killed-sqlite"]["terminal_error_kind"] == "user_interrupt"

    assert first_scan["error-recovered"]["status_tag"] == titles.STATUS_DONE
    assert first_scan["error-recovered"]["completion_id"].startswith(f"{titles.STATUS_DONE}:")


def test_transcript_emits_both_thinking_and_content_and_tool_errors(tmp_path: Path) -> None:
    tpath = _write_jsonl(
        tmp_path / "transcript_full.jsonl",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:00Z",
                "content": (
                    "<USER_REQUEST>inspect files</USER_REQUEST>"
                    "<USER_SETTINGS_CHANGE>The user changed setting `Model Selection` "
                    "from None to Gemini 2.5 Pro. No need to comment.</USER_SETTINGS_CHANGE>"
                ),
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:01Z",
                "thinking": "Let me inspect the repo first.",
                "content": "Checking files now.",
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": "ls"}},
                    {"name": "edit_file", "args": {"Path": "a.py"}},
                    {"name": "write_to_file", "args": {"TargetFile": "b.py"}},
                    {"name": "grep_search", "args": {"Query": "foo"}},
                    {"name": "search_web", "args": {"query": "bar"}},
                    {"name": "ask_question", "args": {"questions": []}},
                ],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "ERROR",
                "error": "permission denied",
                "created_at": "2026-11-10T10:00:02Z",
                "content": "",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "INVALID",
                "error": "invalid args",
                "created_at": "2026-11-10T10:00:03Z",
                "content": "Error invalid tool call",
            },
            {
                "step_index": 4,
                "source": "MODEL",
                "type": "CHECKPOINT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:04Z",
                "content": "# Resuming from a compaction",
            },
        ],
    )

    events = load_events({"source": "antigravity", "path": str(tpath), "id": "sess-1"})
    types = [e["type"] for e in events]
    assert types[:3] == ["user_message", "thinking", "assistant_message"]
    assert events[0]["text"] == "inspect files"
    assert events[1]["text"] == "Let me inspect the repo first."
    assert events[2]["text"] == "Checking files now."

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert [(tc["name"], tc["kind"]) for tc in tool_calls] == [
        ("run_command", "shell"),
        ("edit_file", "edit"),
        ("write_to_file", "write"),
        ("grep_search", "search"),
        ("search_web", "web"),
        ("ask_question", "question"),
    ]
    assert classify_tool("VIEW_FILE") == "read"
    assert classify_tool("read_url_content") == "web"
    assert classify_tool("list_dir") == "search"

    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 2
    assert tool_results[0]["status"] == "error"
    assert tool_results[0]["output"] == "permission denied"
    assert tool_results[1]["status"] == "error"
    assert tool_results[1]["output"] == "Error invalid tool call"

    assert ag.model_name_from_user_input(
        "<USER_SETTINGS_CHANGE>The user changed setting `Model Selection` "
        "from None to Gemini 2.5 Pro. No need to comment.</USER_SETTINGS_CHANGE>"
    ) == "Gemini 2.5 Pro"


def test_adapter_refresh_session_and_live_parity(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity"
    root.mkdir(parents=True)
    _init_summaries_db(
        root,
        [("sess-r", "", "", json.dumps([f"file://{proj}"]), 1, 0, "", 0)],
    )
    tpath = _write_transcript(
        root,
        "sess-r",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:00Z",
                "content": "<USER_REQUEST>initial</USER_REQUEST>",
            }
        ],
    )
    adapter = get_adapter("antigravity")
    with mock.patch.object(ag, "_roots", return_value=[str(root)]):
        listed = adapter.scan(limit=10)
        assert len(listed) == 1
        assert listed[0]["status_tag"] == titles.STATUS_PENDING
        _write_transcript(
            root,
            "sess-r",
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-10T10:00:00Z",
                    "content": "<USER_REQUEST>initial</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-10T10:00:02Z",
                    "content": "completed",
                },
            ],
        )
        refreshed = adapter.refresh_session(listed[0])
        assert refreshed is not None
        assert refreshed["status_tag"] == titles.STATUS_DONE
        assert refreshed["path"] == str(tpath)


def test_live_antigravity_cli_parity() -> None:
    live_root = Path(os.path.expanduser("~/.gemini/antigravity-cli"))
    db_path = live_root / "conversation_summaries.db"
    if not db_path.is_file():
        pytest.skip("no local ~/.gemini/antigravity-cli history")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sub_cids = {
            row[0]
            for row in conn.execute(
                "SELECT conversation_id FROM conversation_summaries "
                "WHERE parent_conversation_id != '' OR nesting_depth > 0"
            ).fetchall()
        }
        try:
            sub_cids.update(
                row[0]
                for row in conn.execute(
                    "SELECT conversation_id FROM conversation_parents"
                ).fetchall()
            )
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    sessions = ag.scan_sessions(limit=50, include_missing_cwd=True)
    scanned_ids = {s["id"] for s in sessions}
    assert scanned_ids.isdisjoint(sub_cids)

    # Verify thinking + assistant_message co-emission on any live session with both.
    for s in sessions:
        path = str(s.get("path") or "")
        if not path.endswith(".jsonl"):
            continue
        rows = ag._iter_transcript_rows(path)
        has_both = any(
            r.get("type") == "PLANNER_RESPONSE"
            and isinstance(r.get("thinking"), str)
            and r["thinking"].strip()
            and isinstance(r.get("content"), str)
            and r["content"].strip()
            and r["content"].strip() != "None"
            for r in rows
        )
        if has_both:
            events = load_events(s)
            kinds = {e["type"] for e in events}
            assert "thinking" in kinds
            assert "assistant_message" in kinds
            break

    # If the local sesskit-stats debug/release binary was built, verify token parity.
    repo_root = Path(__file__).resolve().parent.parent
    stats_bin = next(
        (
            cand
            for cand in (
                repo_root / "crates/sesskit-stats/target/release/sesskit-stats",
                repo_root / "crates/sesskit-stats/target/debug/sesskit-stats",
            )
            if cand.is_file()
        ),
        None,
    )
    if stats_bin is None:
        return
    out = subprocess.check_output([str(stats_bin), "--full", "--sessions"], text=True)
    detail = json.loads(out)["detail"]
    rust_ag = {entry["id"]: entry for entry in detail if entry["runtime"] == "antigravity"}
    for sub_id in sub_cids:
        if sub_id in rust_ag:
            assert rust_ag[sub_id]["excluded"] == "subagent"
    for s in sessions:
        path = str(s.get("path") or "")
        if not path.endswith(".jsonl") or s["id"] not in rust_ag:
            continue
        expected_tokens = 0
        for row in ag._iter_transcript_rows(path):
            if row.get("type") in ("PLANNER_RESPONSE", "CHECKPOINT"):
                in_tok = row.get("input_tokens") or 0
                cache_tok = row.get("cache_read_tokens") or 0
                out_tok = row.get("output_tokens") or 0
                if in_tok or cache_tok or out_tok:
                    expected_tokens += int(in_tok) + int(cache_tok) + int(out_tok)
        assert rust_ag[s["id"]]["window_tokens"] == expected_tokens


def test_typed_activity_questions_media_compaction_and_errors(tmp_path: Path) -> None:
    from sesskit.activity import load_activity, to_v1_dicts
    from sesskit.activity_reader import open_activity_reader
    from sesskit.conformance import run_conformance
    from sesskit.registry import load_session_conversation

    root = tmp_path / ".gemini" / "antigravity-cli"
    tpath = _write_transcript(
        root,
        "sess-typed",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:00Z",
                "content": (
                    "<USER_REQUEST>\nInspect screenshots\n</USER_REQUEST>\n"
                    "<USER_SETTINGS_CHANGE>The user changed setting `Model Selection` "
                    "from None to Gemini 2.5 Pro.</USER_SETTINGS_CHANGE>"
                ),
                "media": [{"mime_type": "image/png", "uri": "/tmp/in_0.png"}],
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:01Z",
                "input_tokens": 120,
                "cache_read_tokens": 800,
                "output_tokens": 45,
                "thinking": "Need target confirmation and file read.",
                "content": "Checking options now.",
                "tool_calls": [
                    {
                        "name": "ask_question",
                        "args": {
                            "questions": [
                                {
                                    "question": "Which env?",
                                    "options": ["Staging", "Production"],
                                    "is_multi_select": False,
                                }
                            ]
                        },
                    },
                    {"name": "run_command", "args": {"CommandLine": "false"}},
                ],
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "INVALID",
                "error": "invalid args",
                "created_at": "2026-11-10T10:00:02Z",
                "content": "Error invalid tool call",
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:03Z",
                "content": "Staging",
                "media": [{"mime_type": "image/png", "uri": "file:///tmp/out_2.png"}],
            },
            {
                "step_index": 4,
                "source": "SYSTEM",
                "type": "CHECKPOINT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:04Z",
                "input_tokens": 50,
                "cache_read_tokens": 200,
                "output_tokens": 10,
                "content": "# Resuming from compaction",
            },
            {
                "step_index": 5,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-10T10:00:05Z",
                "content": "<USER_REQUEST></USER_REQUEST>",
                "media": [{"mime_type": "image/png", "uri": "/tmp/img_only.png"}],
            },
            {
                "step_index": 6,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "ERROR",
                "error": "503 Service Unavailable",
                "created_at": "2026-11-10T10:00:06Z",
                "content": "",
            },
        ],
    )
    session = {"source": "antigravity", "path": str(tpath), "id": "sess-typed"}
    adapter = get_adapter("antigravity")
    snapshot = adapter.load_activity(session)
    assert snapshot == load_activity(session)
    assert snapshot.state == "available"
    assert snapshot.outcome.status == "aborted"
    assert snapshot.outcome.error is not None
    assert snapshot.outcome.error.http_status == 503

    by_type = {}
    for ev in snapshot.events:
        by_type.setdefault(ev.type, []).append(ev)

    u_msgs = by_type["user_message"]
    assert u_msgs[0].text == "Inspect screenshots"
    assert u_msgs[0].origin == "human"
    assert u_msgs[0].raw_input == {"media": [{"mime_type": "image/png", "uri": "/tmp/in_0.png"}]}
    assert "[image/png]" in (u_msgs[1].text or "")

    assert by_type["thinking"][0].usage is not None
    assert by_type["thinking"][0].usage.model == "Gemini 2.5 Pro"
    assert by_type["thinking"][0].usage.input_tokens == 120
    assert by_type["thinking"][0].usage.cache_read_tokens == 800
    assert by_type["thinking"][0].usage.output_tokens == 45

    ask_call = by_type["tool_call"][0]
    assert ask_call.call_id == "1-0"
    assert ask_call.interaction is not None
    assert ask_call.interaction.purpose == "question"
    assert ask_call.interaction.resolution == "answered"
    assert ask_call.interaction.questions[0].prompt == "Which env?"
    assert [o.label for o in ask_call.interaction.questions[0].options] == ["Staging", "Production"]
    assert ask_call.interaction.answers[0].text == "Staging"

    tool_results = by_type["tool_result"]
    # Step 3 (invalid) arrived before step 2 (done), paired to 1-1 and 1-0 respectively.
    assert [(r.call_id, r.result.status) for r in tool_results] == [("1-1", "error"), ("1-0", "ok")]
    assert tool_results[1].raw_output == {
        "text": "Staging",
        "media": [{"mime_type": "image/png", "uri": "file:///tmp/out_2.png"}],
    }

    assert len(by_type["compaction"]) == 1
    assert by_type["compaction"][0].compaction is not None
    assert by_type["compaction"][0].compaction.summary == "# Resuming from compaction"

    conv_default = load_session_conversation(session)
    conv_with_errors = adapter.load_conversation(session, include_errors=True)
    assert len(conv_with_errors) == len(conv_default) + 1
    assert conv_with_errors[-1].text == "503 Service Unavailable"

    report = run_conformance(
        "antigravity",
        snapshot=snapshot,
        v1_events=to_v1_dicts(snapshot),
        conversation=conv_default,
    )
    assert report.passed, report.violations

    # SQLite-only sessions remain readable, empty snapshots cleanly in activity and reader.
    db_file = root / "conversations" / "sqlite-only.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(db_file).close()
    db_sess = {"source": "antigravity", "path": str(db_file), "id": "sqlite-only"}
    assert adapter.load_activity(db_sess).state == "empty"
    reader = open_activity_reader(db_sess)
    poll = reader.poll()
    assert poll.state == "empty"
    assert poll.events == ()


def test_antigravity_incremental_reader_append_partial_truncation_and_pages(tmp_path: Path) -> None:
    from sesskit.activity import load_activity
    from sesskit.activity_reader import open_activity_reader

    tpath = tmp_path / "brain" / "sess-inc" / ".system_generated" / "logs" / "transcript_full.jsonl"
    row0 = {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-11-10T10:00:00Z",
        "content": "<USER_REQUEST>Pick one</USER_REQUEST>",
    }
    row1 = {
        "step_index": 1,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-11-10T10:00:01Z",
        "tool_calls": [
            {
                "name": "ask_question",
                "args": {
                    "questions": [
                        {"question": "Proceed?", "options": ["Yes", "No"], "is_multi_select": False}
                    ]
                },
            }
        ],
    }
    _write_jsonl(tpath, [row0, row1])
    session = {"source": "antigravity", "path": str(tpath), "id": "sess-inc"}

    reader = open_activity_reader(session)
    first = reader.poll()
    assert first.reset is True
    assert first.state == "available"
    assert first.outcome.status == "unknown"  # unmatched ask_question tool call
    assert [e.seq for e in first.events] == [1, 2]

    # No-change poll stats only and reads nothing.
    reader.bytes_read = 0
    reader.lines_parsed = 0
    same = reader.poll()
    assert same.reset is False
    assert same.events == ()
    assert reader.bytes_read == 0
    assert reader.lines_parsed == 0

    # Partial trailing line without newline is held back.
    row2 = {
        "step_index": 2,
        "source": "MODEL",
        "type": "GENERIC",
        "status": "DONE",
        "created_at": "2026-11-10T10:00:02Z",
        "content": "Yes",
    }
    row3 = {
        "step_index": 3,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-11-10T10:00:03Z",
        "content": "Proceeding.",
    }
    partial = json.dumps(row2)
    with open(tpath, "ab") as fh:
        fh.write(partial.encode("utf-8"))
    held = reader.poll()
    assert held.events == ()

    with open(tpath, "ab") as fh:
        fh.write(b"\n" + json.dumps(row3).encode("utf-8") + b"\n")
    appended = reader.poll()
    assert appended.reset is False
    assert appended.outcome.status == "done"
    assert [e.type for e in appended.events] == ["tool_result", "assistant_message"]
    assert [e.seq for e in appended.events] == [3, 4]

    # Reassembling pages matches full snapshot including enriched interaction resolution.
    page = reader.page(limit=10)
    snap = load_activity(session)
    assert page.events == snap.events
    assert page.events[1].interaction is not None
    assert page.events[1].interaction.resolution == "answered"

    # Cursor resume across fresh reader instance skips full parse on unchanged poll.
    resumed = open_activity_reader(session, cursor=appended.cursor)
    resumed_poll = resumed.poll()
    assert resumed_poll.reset is False
    assert resumed_poll.events == ()
    assert resumed.full_parses == 0

    # Truncation resets generation cleanly.
    _write_jsonl(tpath, [row0])
    reset_poll = resumed.poll()
    assert reset_poll.reset is True
    assert reset_poll.outcome.status == "pending"
    assert len(reset_poll.events) == 1


def test_delete_session_cleans_artifacts_descendants_and_respects_flock(tmp_path: Path) -> None:
    import fcntl

    root = tmp_path / ".gemini" / "antigravity-cli"
    for rel in ("annotations", "conversations", "presence", "brain"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    db_path = _init_summaries_db(
        root,
        [
            ("parent-1", "Parent", "", json.dumps([f"file://{tmp_path}"]), 2, 0, "", 0),
            ("child-1", "Child", "", json.dumps([f"file://{tmp_path}"]), 1, 0, "parent-1", 1),
            ("grand-1", "Grandchild", "", json.dumps([f"file://{tmp_path}"]), 1, 0, "", 2),
            ("other-1", "Keep me", "", json.dumps([f"file://{tmp_path}"]), 1, 0, "", 0),
        ],
        parents=[("grand-1", "child-1")],
    )
    # Also verify FTS cleanup if conversations_fts exists.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, title)")
        for cid in ("parent-1", "child-1", "grand-1", "other-1"):
            conn.execute("INSERT INTO conversations_fts VALUES (?, ?)", (cid, cid))
        conn.commit()
    finally:
        conn.close()

    cids = ("parent-1", "child-1", "grand-1", "other-1")
    for cid in cids:
        _write_transcript(
            root,
            cid,
            [{"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "<USER_REQUEST>hi</USER_REQUEST>"}],
        )
        (root / "annotations" / f"{cid}.pbtxt").write_text(f'title:"{cid}"\n', encoding="utf-8")
        (root / "conversations" / f"{cid}.db").write_bytes(b"db")
        (root / "conversations" / f"{cid}.db-wal").write_bytes(b"wal")
        (root / "conversations" / f"{cid}.db-shm").write_bytes(b"shm")
        (root / "presence" / f"{cid}.lock").write_bytes(b"")

    held_lock = root / "presence" / "child-1.lock"
    fd = os.open(held_lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target_transcript = root / "brain" / "parent-1" / ".system_generated" / "logs" / "transcript_full.jsonl"
        ag.delete_session(str(target_transcript))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    for removed in ("parent-1", "child-1", "grand-1"):
        assert not (root / "brain" / removed).exists()
        assert not (root / "annotations" / f"{removed}.pbtxt").exists()
        assert not (root / "conversations" / f"{removed}.db").exists()
        assert not (root / "conversations" / f"{removed}.db-wal").exists()
        assert not (root / "conversations" / f"{removed}.db-shm").exists()

    assert not (root / "presence" / "parent-1.lock").exists()
    assert not (root / "presence" / "grand-1.lock").exists()
    # Active flock-held lock must be left untouched.
    assert held_lock.exists()

    # Unrelated session stays completely untouched.
    assert (root / "brain" / "other-1").is_dir()
    assert (root / "annotations" / "other-1.pbtxt").is_file()
    assert (root / "conversations" / "other-1.db").is_file()
    assert (root / "presence" / "other-1.lock").is_file()

    check_conn = sqlite3.connect(db_path)
    try:
        remaining_summaries = [
            r[0] for r in check_conn.execute("SELECT conversation_id FROM conversation_summaries").fetchall()
        ]
        remaining_parents = check_conn.execute("SELECT conversation_id FROM conversation_parents").fetchall()
        remaining_fts = [
            r[0] for r in check_conn.execute("SELECT conversation_id FROM conversations_fts").fetchall()
        ]
    finally:
        check_conn.close()
    assert remaining_summaries == ["other-1"]
    assert remaining_parents == []
    assert remaining_fts == ["other-1"]

    # Deleting by bare cid + explicit root works too; missing target is a clean no-op.
    ag.delete_session("other-1", root=str(root))
    assert not (root / "brain" / "other-1").exists()
    ag.delete_session("nonexistent-id", root=str(root))


def _init_summaries_db_with_idle(
    root: Path,
    rows: list[tuple[str, str, str, str, int, str, int, int]],
) -> Path:
    """Create conversation_summaries.db with explicit status and not_fully_idle."""
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                preview TEXT NOT NULL DEFAULT '',
                step_count INTEGER NOT NULL DEFAULT 0,
                last_modified_time TEXT NOT NULL DEFAULT '',
                workspace_uris TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                killed INTEGER NOT NULL DEFAULT 0,
                not_fully_idle INTEGER NOT NULL DEFAULT 0,
                last_user_input_time TEXT NOT NULL DEFAULT '',
                app_data_dir TEXT NOT NULL DEFAULT '',
                parent_conversation_id TEXT NOT NULL DEFAULT '',
                nesting_depth INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        for cid, title, preview, uris, steps, status, killed, nfi in rows:
            conn.execute(
                "INSERT INTO conversation_summaries ("
                "conversation_id, title, preview, step_count, last_modified_time,"
                " workspace_uris, status, killed, not_fully_idle, last_user_input_time,"
                " app_data_dir, parent_conversation_id, nesting_depth"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0)",
                (
                    cid,
                    title,
                    preview,
                    steps,
                    "2026-11-14T10:00:10Z",
                    uris,
                    status,
                    killed,
                    nfi,
                    "2026-11-14T10:00:00Z",
                    str(root),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_tool_calling_and_thinking_never_trigger_false_done(tmp_path: Path) -> None:
    from sesskit.activity import load_activity
    from sesskit.activity_reader import open_activity_reader

    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    _init_summaries_db(
        root,
        [
            ("thinking-tool", "Thinking+Tools", "", uris, 2, 0, "", 0),
            ("thinking-only", "Thinking Only", "", uris, 2, 0, "", 0),
            ("prose-tool", "Prose+Tools", "", uris, 2, 0, "", 0),
            ("stale-reply", "Stale Reply", "", uris, 4, 0, "", 0),
        ],
    )
    user_row = {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-11-14T10:00:00Z",
        "content": "<USER_REQUEST>check</USER_REQUEST>",
    }
    _write_transcript(
        root,
        "thinking-tool",
        [
            user_row,
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:01Z",
                "thinking": "Internal CoT should never notify or show up as assistant reply.",
                "content": "",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}],
            },
        ],
    )
    _write_transcript(
        root,
        "thinking-only",
        [
            user_row,
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:01Z",
                "thinking": "Still thinking without final answer.",
                "content": "",
            },
        ],
    )
    _write_transcript(
        root,
        "prose-tool",
        [
            user_row,
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:01Z",
                "content": "Let me inspect the repo first.",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}],
            },
        ],
    )
    # Earlier turn completed with reply A; later turn ends on an empty PLANNER_RESPONSE:
    # tail must NOT inherit turn 1's last_agent to fake STATUS_DONE.
    _write_transcript(
        root,
        "stale-reply",
        [
            user_row,
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:01Z",
                "content": "First answer.",
            },
            {
                "step_index": 2,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:02Z",
                "content": "<USER_REQUEST>follow up</USER_REQUEST>",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:03Z",
                "thinking": "CoT after second question",
                "content": "",
            },
        ],
    )

    scanned = {s["id"]: s for s in _scan_root(root, limit=20)}

    for cid, expected_tag in (
        ("thinking-tool", titles.STATUS_PENDING),
        ("thinking-only", titles.STATUS_NONE),
        ("prose-tool", titles.STATUS_PENDING),
        ("stale-reply", titles.STATUS_NONE),
    ):
        info = scanned[cid]
        assert info["status_tag"] == expected_tag, (cid, info["status_tag"])
        assert info["completion_id"] == ""
        snap = load_activity({"source": "antigravity", "path": info["path"], "id": cid})
        assert snap.outcome.status != "done"
        poll = open_activity_reader(
            {"source": "antigravity", "path": info["path"], "id": cid}
        ).poll()
        assert poll.outcome.status != "done"

    assert scanned["thinking-tool"]["last_agent_msg"] == ""
    assert scanned["thinking-only"]["last_agent_msg"] == ""
    assert ag.load_conversation(scanned["thinking-tool"]["path"]) == [
        m for m in ag.load_conversation(scanned["thinking-tool"]["path"]) if m.role == "user"
    ]
    assert [m.role for m in ag.load_conversation(scanned["thinking-only"]["path"])] == ["user"]


def test_engine_running_and_not_fully_idle_suppress_completion(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    db_path = _init_summaries_db_with_idle(
        root,
        [
            ("running-row", "Running", "", uris, 2, "CASCADE_RUN_STATUS_RUNNING", 0, 0),
            ("nfi-row", "Not Idle", "", uris, 2, "CASCADE_RUN_STATUS_IDLE", 0, 1),
            ("idle-row", "Idle", "", uris, 2, "CASCADE_RUN_STATUS_IDLE", 0, 0),
        ],
    )
    rows = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:00Z",
            "content": "<USER_REQUEST>do work</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:05Z",
            "content": "Working on background steps, please hold.",
        },
    ]
    for cid in ("running-row", "nfi-row", "idle-row"):
        _write_transcript(root, cid, rows)

    scanned = {s["id"]: s for s in _scan_root(root, limit=20)}
    assert scanned["running-row"]["status_tag"] == titles.STATUS_PENDING
    assert scanned["running-row"]["completion_id"] == ""
    assert scanned["nfi-row"]["status_tag"] == titles.STATUS_PENDING
    assert scanned["nfi-row"]["completion_id"] == ""
    assert scanned["idle-row"]["status_tag"] == titles.STATUS_DONE
    assert scanned["idle-row"]["completion_id"].startswith(f"{titles.STATUS_DONE}:")

    # Once nfi-row flips to idle in DB, refresh_session emits completion.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE conversation_summaries SET not_fully_idle = 0 WHERE conversation_id = 'nfi-row'"
        )
        conn.commit()
    finally:
        conn.close()
    with mock.patch.object(ag, "_roots", return_value=[str(root)]):
        refreshed = ag.refresh_session(scanned["nfi-row"])
    assert refreshed is not None
    assert refreshed["status_tag"] == titles.STATUS_DONE
    assert refreshed["completion_id"].startswith(f"{titles.STATUS_DONE}:")


def test_transcript_pending_async_lifecycle_timers_subagents_and_daemon(tmp_path: Path) -> None:
    from sesskit.activity import load_activity
    from sesskit.activity_reader import open_activity_reader

    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    _init_summaries_db_with_idle(
        root,
        [
            ("bg-session", "BG Task", "", uris, 10, "CASCADE_RUN_STATUS_IDLE", 0, 0),
            ("daemon-session", "Daemon Cron", "", uris, 4, "CASCADE_RUN_STATUS_IDLE", 0, 0),
            ("workflow-session", "Workflow Async", "", uris, 4, "CASCADE_RUN_STATUS_IDLE", 0, 0),
            ("quoted-doc", "Quoted Doc", "", uris, 4, "CASCADE_RUN_STATUS_IDLE", 0, 0),
        ],
    )

    bg_rows = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:00Z",
            "content": "<USER_REQUEST>run tests</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:01Z",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest"}}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-11-14T10:00:02Z",
            "content": (
                "Created At: 2026-11-14T10:00:02Z\n"
                "Tool is running as a background task with task id: bg-session/task-2\n"
                "Task Description: pytest"
            ),
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:03Z",
            "tool_calls": [
                {
                    "name": "schedule",
                    "args": {"DurationSeconds": 120, "TimerCondition": "bg-session/task-2"},
                }
            ],
        },
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-11-14T10:00:04Z",
            "content": (
                "Created At: 2026-11-14T10:00:04Z\n"
                "Tool is running as a background task with task id: bg-session/task-4"
            ),
        },
        {
            "step_index": 5,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:05Z",
            "content": "Tests running in background, waiting for wakeup.",
        },
    ]
    tpath = _write_transcript(root, "bg-session", bg_rows)

    # Mid-wait: DB may already say IDLE, yet transcript pending_async keeps BOTH pending.
    scanned = {s["id"]: s for s in _scan_root(root, limit=20)}
    assert scanned["bg-session"]["status_tag"] == titles.STATUS_PENDING
    assert scanned["bg-session"]["completion_id"] == ""
    assert load_activity({"source": "antigravity", "path": str(tpath), "id": "bg-session"}).outcome.status == "pending"

    # Task notification arrives -> still SYSTEM_MESSAGE -> still pending (never done).
    # Note: dependent TimerCondition timer task-4 also auto-cancels!
    sys_done = {
        "step_index": 6,
        "source": "SYSTEM",
        "type": "SYSTEM_MESSAGE",
        "status": "DONE",
        "created_at": "2026-11-14T10:00:20Z",
        "content": (
            "<SYSTEM_MESSAGE>\n"
            "[Message] timestamp=2026-11-14T02:00:20Z sender=bg-session/task-2 "
            'priority=MESSAGE_PRIORITY_HIGH content=Task id "bg-session/task-2" finished with result:'
        ),
    }
    _write_transcript(root, "bg-session", bg_rows + [sys_done])
    with mock.patch.object(ag, "_roots", return_value=[str(root)]):
        mid = ag.refresh_session(scanned["bg-session"])
    assert mid is not None and mid["status_tag"] == titles.STATUS_PENDING
    assert load_activity({"source": "antigravity", "path": str(tpath), "id": "bg-session"}).outcome.status == "pending"

    # Final no-tool PLANNER_RESPONSE after pending_async cleared -> DONE.
    final_done = {
        "step_index": 7,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": "2026-11-14T10:00:22Z",
        "content": "All tests passed.",
    }
    _write_transcript(root, "bg-session", bg_rows + [sys_done, final_done])
    with mock.patch.object(ag, "_roots", return_value=[str(root)]):
        done = ag.refresh_session(scanned["bg-session"])
    assert done is not None and done["status_tag"] == titles.STATUS_DONE
    assert done["completion_id"].startswith(f"{titles.STATUS_DONE}:")
    assert load_activity({"source": "antigravity", "path": str(tpath), "id": "bg-session"}).outcome.status == "done"

    # Daemon schedule (IsDaemon=True) never holds pending_async across final turn reply.
    _write_transcript(
        root,
        "daemon-session",
        [
            bg_rows[0],
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:01Z",
                "tool_calls": [
                    {"name": "schedule", "args": {"CronExpression": "*/5 * * * *", "IsDaemon": True}}
                ],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "RUNNING",
                "created_at": "2026-11-14T10:00:02Z",
                "content": "Created At: 2026-11-14T10:00:02Z\nTool is running as a background task with task id: daemon-session/task-2",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:03Z",
                "content": "Daemon scheduled, session complete.",
            },
        ],
    )
    # Workflow/subagent async launch stays pending across ordinary follow-up turns until resolved or aborted.
    wf_path = _write_transcript(
        root,
        "workflow-session",
        [
            bg_rows[0],
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:01Z",
                "tool_calls": [{"name": "run_workflow", "args": {"Description": "fanout"}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:02Z",
                "content": json.dumps({"workflow_id": "wf-9", "status": "running"}),
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:03Z",
                "content": "Waiting on workflow wf-9.",
            },
        ],
    )
    # Quoting a background task log inside a DONE read_file does NOT create phantom pending_async.
    _write_transcript(
        root,
        "quoted-doc",
        [
            bg_rows[0],
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:01Z",
                "tool_calls": [{"name": "read_file", "args": {"Path": "doc.md"}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:02Z",
                "content": "Tool is running as a background task with task id: fake/task-99\nNo background tasks are currently running.",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:03Z",
                "content": "Read the docs cleanly.",
            },
        ],
    )

    scanned2 = {s["id"]: s for s in _scan_root(root, limit=20)}
    assert scanned2["daemon-session"]["status_tag"] == titles.STATUS_DONE
    assert scanned2["workflow-session"]["status_tag"] == titles.STATUS_PENDING
    assert scanned2["workflow-session"]["completion_id"] == ""
    assert load_activity({"source": "antigravity", "path": str(wf_path), "id": "workflow-session"}).outcome.status == "pending"
    assert scanned2["quoted-doc"]["status_tag"] == titles.STATUS_DONE

    # Ordinary USER_EXPLICIT follow-up preserves pending_async from active wf-9 until resolved.
    reader = open_activity_reader({"source": "antigravity", "path": str(wf_path), "id": "workflow-session"})
    assert reader.poll().outcome.status == "pending"
    with open(wf_path, "a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "step_index": 4,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-14T10:01:00Z",
                    "content": "<USER_REQUEST>fresh question</USER_REQUEST>",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "step_index": 5,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-14T10:01:05Z",
                    "content": "Side answer while wf-9 runs.",
                }
            )
            + "\n"
        )
    assert reader.poll().outcome.status == "pending"
    with open(wf_path, "a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "step_index": 6,
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": "2026-11-14T10:01:10Z",
                    "content": (
                        "<SYSTEM_MESSAGE>\n"
                        "[Message] timestamp=2026-11-14T10:01:10Z sender=wf-9 "
                        "priority=MESSAGE_PRIORITY_HIGH content=Workflow wf-9 finished\n"
                        "</SYSTEM_MESSAGE>"
                    ),
                }
            )
            + "\n"
            + json.dumps(
                {
                    "step_index": 7,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-14T10:01:12Z",
                    "content": "Fresh answer.",
                }
            )
            + "\n"
        )
    assert reader.poll().outcome.status == "done"


def test_tool_result_errors_stay_pending_while_error_message_aborts(tmp_path: Path) -> None:
    from sesskit.activity import load_activity, to_v1_dicts
    from sesskit.conformance import run_conformance
    from sesskit.registry import load_session_conversation

    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    _init_summaries_db_with_idle(
        root,
        [
            ("midturn-tool-err", "Tool Err", "", uris, 3, "CASCADE_RUN_STATUS_IDLE", 0, 0),
            ("turn-api-err", "API Err", "", uris, 2, "CASCADE_RUN_STATUS_IDLE", 0, 0),
            ("turn-api-recovered", "API Recovered", "", uris, 3, "CASCADE_RUN_STATUS_IDLE", 0, 0),
        ],
    )
    user_row = {
        "step_index": 0,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": "2026-11-14T10:00:00Z",
        "content": "<USER_REQUEST>do work</USER_REQUEST>",
    }
    tool_err_rows = [
        user_row,
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:01Z",
            "tool_calls": [{"name": "read_file", "args": {"Path": "missing.py"}}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "ERROR",
            "error": "file does not exist",
            "created_at": "2026-11-14T10:00:02Z",
            "content": "file does not exist",
        },
    ]
    api_err_rows = [
        user_row,
        {
            "step_index": 1,
            "source": "SYSTEM",
            "type": "ERROR_MESSAGE",
            "status": "DONE",
            "error": 'API error (attempt 1): request failed: operation timed out',
            "created_at": "2026-11-14T10:00:02Z",
        },
    ]
    recovered_rows = api_err_rows + [
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:06Z",
            "content": "Succeeded after retry.",
        }
    ]
    p_tool = _write_transcript(root, "midturn-tool-err", tool_err_rows)
    p_api = _write_transcript(root, "turn-api-err", api_err_rows)
    p_rec = _write_transcript(root, "turn-api-recovered", recovered_rows)

    scanned = {s["id"]: s for s in _scan_root(root, limit=20)}
    assert scanned["midturn-tool-err"]["status_tag"] == titles.STATUS_PENDING
    assert scanned["midturn-tool-err"]["completion_id"] == ""
    assert scanned["midturn-tool-err"].get("terminal_error_kind", "") == ""

    assert scanned["turn-api-err"]["status_tag"] == titles.STATUS_ABORTED
    assert scanned["turn-api-err"]["completion_id"].startswith(f"{titles.STATUS_ABORTED}:")
    assert "operation timed out" in scanned["turn-api-err"]["last_agent_msg"]

    assert scanned["turn-api-recovered"]["status_tag"] == titles.STATUS_DONE
    assert scanned["turn-api-recovered"]["completion_id"].startswith(f"{titles.STATUS_DONE}:")

    for cid, p, expected_outcome in (
        ("midturn-tool-err", p_tool, "pending"),
        ("turn-api-err", p_api, "aborted"),
        ("turn-api-recovered", p_rec, "done"),
    ):
        sess = {"source": "antigravity", "path": str(p), "id": cid}
        snap = load_activity(sess)
        assert snap.outcome.status == expected_outcome
        rep = run_conformance(
            "antigravity",
            snapshot=snap,
            v1_events=to_v1_dicts(snap),
            conversation=load_session_conversation(sess),
        )
        assert rep.passed, rep.violations


def test_scan_signature_tracks_real_files_not_path_characters(tmp_path: Path) -> None:
    empty_root = tmp_path / "empty-ag"
    empty_root.mkdir()
    with mock.patch.object(ag, "_roots", return_value=[str(empty_root)]):
        assert ag.scan_signature() is None

    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    db_path = _init_summaries_db_with_idle(
        root,
        [("sig-1", "Sig Session", "", uris, 2, "CASCADE_RUN_STATUS_RUNNING", 0, 1)],
    )
    tpath = _write_transcript(
        root,
        "sig-1",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-14T10:00:00Z",
                "content": "<USER_REQUEST>hi</USER_REQUEST>",
            }
        ],
    )
    with (
        mock.patch.object(ag, "_roots", return_value=[str(root)]),
        mock.patch.object(ag, "live_pid_snapshot", return_value=(1234,)),
    ):
        sig1 = ag.scan_signature()
        assert isinstance(sig1, tuple) and len(sig1) == 2
        file_stamps, pids = sig1
        assert pids == (1234,)
        statted_paths = {entry[0] for entry in file_stamps}
        assert "." not in statted_paths and "/" not in statted_paths
        assert str(db_path) in statted_paths
        assert str(tpath) in statted_paths

        with open(tpath, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-14T10:00:02Z",
                        "content": "Finished.",
                    }
                )
                + "\n"
            )
        sig2 = ag.scan_signature()
        assert sig2 != sig1

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE conversation_summaries "
                "SET status = 'CASCADE_RUN_STATUS_IDLE', not_fully_idle = 0 "
                "WHERE conversation_id = 'sig-1'"
            )
            conn.commit()
        finally:
            conn.close()
        sig3 = ag.scan_signature()
        assert sig3 != sig2


def test_side_user_input_during_running_background_task_preserves_pending(tmp_path: Path) -> None:
    from sesskit.activity import load_activity
    from sesskit.activity_reader import open_activity_reader

    proj = tmp_path / "proj"
    proj.mkdir()
    root = tmp_path / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    uris = json.dumps([f"file://{proj}"])
    _init_summaries_db_with_idle(
        root,
        [
            ("side-q", "Side Question", "", uris, 7, "CASCADE_RUN_STATUS_IDLE", 0, 0),
            ("cancel-q", "Canceled Abort", "", uris, 6, "CASCADE_RUN_STATUS_IDLE", 0, 0),
        ],
    )
    prefix_rows = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:00Z",
            "content": "<USER_REQUEST>build crate</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:01Z",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "cargo test"}}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-11-14T10:00:02Z",
            "content": (
                "Created At: 2026-11-14T10:00:02Z\n"
                "Tool is running as a background task with task id: side-q/task-1\n"
                "Task Description: cargo test"
            ),
        },
        {
            "step_index": 3,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:05Z",
            "content": "<USER_REQUEST>why was quota hidden?</USER_REQUEST>",
        },
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:10Z",
            "content": "Here is why quota was hidden while the build continues.",
        },
    ]
    tpath = _write_transcript(root, "side-q", prefix_rows)
    scanned = {s["id"]: s for s in _scan_root(root, limit=20)}
    assert scanned["side-q"]["status_tag"] == titles.STATUS_PENDING
    assert scanned["side-q"]["completion_id"] == ""
    assert load_activity({"source": "antigravity", "path": str(tpath), "id": "side-q"}).outcome.status == "pending"
    reader = open_activity_reader({"source": "antigravity", "path": str(tpath), "id": "side-q"})
    assert reader.poll().outcome.status == "pending"

    done_rows = prefix_rows + [
        {
            "step_index": 5,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:20Z",
            "content": (
                "<SYSTEM_MESSAGE>\n"
                "[Message] timestamp=2026-11-14T10:00:20Z sender=side-q/task-1 "
                'priority=MESSAGE_PRIORITY_HIGH content=Task id "side-q/task-1" finished with result:'
            ),
        },
        {
            "step_index": 6,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:25Z",
            "content": "Cargo build finished cleanly.",
        },
    ]
    _write_transcript(root, "side-q", done_rows)
    with mock.patch.object(ag, "_roots", return_value=[str(root)]):
        final_info = ag.refresh_session(scanned["side-q"])
    assert final_info is not None and final_info["status_tag"] == titles.STATUS_DONE
    assert final_info["completion_id"].startswith(f"{titles.STATUS_DONE}:")
    assert load_activity({"source": "antigravity", "path": str(tpath), "id": "side-q"}).outcome.status == "done"
    assert reader.poll().outcome.status == "done"

    cancel_rows = [
        prefix_rows[0],
        prefix_rows[1],
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "created_at": "2026-11-14T10:00:02Z",
            "content": "Tool is running as a background task with task id: cancel-q/task-1",
        },
        {
            "step_index": 3,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "CANCELED",
            "created_at": "2026-11-14T10:00:05Z",
            "content": "<USER_REQUEST>stop and reply cleanly</USER_REQUEST>",
        },
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-14T10:00:08Z",
            "content": "Canceled previous work and finished.",
        },
    ]
    cpath = _write_transcript(root, "cancel-q", cancel_rows)
    scanned_cancel = {s["id"]: s for s in _scan_root(root, limit=20)}
    assert scanned_cancel["cancel-q"]["status_tag"] == titles.STATUS_DONE
    assert scanned_cancel["cancel-q"]["completion_id"].startswith(f"{titles.STATUS_DONE}:")
    assert load_activity({"source": "antigravity", "path": str(cpath), "id": "cancel-q"}).outcome.status == "done"


