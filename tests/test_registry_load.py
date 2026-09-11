"""Integration tests that exercise the real registry wiring (no mocked loaders)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sesskit.parsers import claude, pi
from sesskit.registry import ConversationLoadError, default_registry, load_session_conversation
from sesskit.transcript import load_events


FIXTURES = Path(__file__).parent / "fixtures"


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_registry_loads_claude_fixture(tmp_path, monkeypatch):
    projects = tmp_path / "projects" / "-tmp-demo"
    path = _write(
        projects / "sess-claude-1.jsonl",
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "cwd": str(tmp_path / "demo"),
                        "timestamp": "2026-01-01T00:00:00.000Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": "hello human"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-01-01T00:00:01.000Z",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "hello back"}],
                            "stop_reason": "end_turn",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "isMeta": True,
                        "timestamp": "2026-01-01T00:00:02.000Z",
                        "message": {"role": "user", "content": "SYSTEM NOISE"},
                    }
                ),
            ]
        )
        + "\n",
    )
    (tmp_path / "demo").mkdir()
    monkeypatch.setattr(claude, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(claude, "SESSIONS_DIR", str(tmp_path / "sessions"))

    session = {
        "source": "claude",
        "id": "sess-claude-1",
        "path": path,
    }
    messages = load_session_conversation(session)
    assert [(m.role, m.text) for m in messages] == [
        ("user", "hello human"),
        ("assistant", "hello back"),
    ]
    # Meta/system rows must not leak into plain conversation.
    assert all("SYSTEM NOISE" not in m.text for m in messages)

    registry = default_registry()
    via_registry = registry.get("claude").load_conversation(session)
    assert [(m.role, m.text) for m in via_registry] == [(m.role, m.text) for m in messages]


def test_registry_rejects_missing_history():
    with pytest.raises(ConversationLoadError):
        load_session_conversation({"source": "claude", "id": "x", "path": "/no/such/file.jsonl"})


def test_export_refuses_overwrite_history(tmp_path):
    from sesskit.paths import assert_not_history_path

    history = tmp_path / "hist.jsonl"
    history.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        assert_not_history_path(str(history), [str(history)])


def test_pi_active_branch_follows_last_appended_not_clock():
    # Branch A ends with older wall-clock stamp; branch B is appended later with an
    # earlier timestamp (clock skew). Official restore uses last appended entry.
    entries = [
        {"type": "session", "id": "root-sess", "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/tmp"},
        {
            "type": "message",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "root"}]},
        },
        {
            "type": "message",
            "id": "m2a",
            "parentId": "m1",
            "timestamp": "2026-01-01T00:10:00.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "old-branch"}]},
        },
        {
            "type": "message",
            "id": "m2b",
            "parentId": "m1",
            "timestamp": "2026-01-01T00:00:02.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "new-branch"}]},
        },
    ]
    branch = pi.active_messages(entries)
    texts = [pi.message_text(item["message"].get("content")) for item in branch]
    assert texts == ["root", "new-branch"]


def test_cli_show_uses_real_loader(tmp_path, monkeypatch, capsys):
    from sesskit import cli

    projects = tmp_path / "projects" / "-tmp-demo"
    _write(
        projects / "abc12345-full.jsonl",
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "cwd": str(tmp_path / "demo"),
                        "timestamp": "2026-01-01T00:00:00.000Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": "ping"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-01-01T00:00:01.000Z",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "pong"}],
                            "stop_reason": "end_turn",
                        },
                    }
                ),
            ]
        )
        + "\n",
    )
    (tmp_path / "demo").mkdir()
    monkeypatch.setattr(claude, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(claude, "SESSIONS_DIR", str(tmp_path / "sessions"))
    (tmp_path / "sessions").mkdir(exist_ok=True)

    code = cli.dispatch(["show", "claude:abc12345-full", "--full", "--compact"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["data"]["message_count_total"] == 2
    assert out["data"]["messages"][0]["text"] == "ping"
    assert out["data"]["messages"][1]["text"] == "pong"


@pytest.mark.skipif(
    not any(
        os.path.isdir(os.path.expanduser(p))
        for p in ("~/.claude/projects", "~/.codex/sessions", "~/.cursor/chats", "~/.pi/agent/sessions")
    ),
    reason="no local agent history present",
)
def test_live_sample_plain_matches_events_and_filters_noise():
    """Sample real local history: user texts align; strong system markers stay out."""
    registry = default_registry()
    scanned = registry.scan_all(6)
    checked = 0
    strong = (
        "system reminder",
        "<system",
        "task-notification",
        "<ide_opened_file",
        "<agent_skills>",
        "<mcp_file_system>",
        "<function_calls>",
    )
    for runtime_id, sessions in scanned.items():
        for session in sessions[:2]:
            messages = registry.get(runtime_id).load_conversation(session)
            events = load_events(dict(session))
            user_plain = [m.text for m in messages if m.role == "user"]
            user_events = [e.get("text") for e in events if e.get("type") == "user_message"]
            for text in user_plain:
                assert text in user_events or any(
                    text in (ev or "") or (ev or "") in text for ev in user_events
                ), (runtime_id, session.get("id"), text[:80])
                low = text.lower()
                assert not any(marker in low for marker in strong), (runtime_id, text[:120])
            assert all(m.role in {"user", "assistant"} for m in messages)
            assert all((m.text or "").strip() for m in messages)
            checked += 1
    assert checked >= 1
