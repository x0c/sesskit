"""CLI smoke tests with fixtures — no real home history required."""

from __future__ import annotations

import json
from unittest import mock

from sesskit import cli
from sesskit.models import ConversationMessage, make_session_info
from sesskit.registry import ParserRegistry, RuntimeParser


def _session(source="claude", sid="abc12345"):
    return make_session_info(
        source=source,
        id=sid,
        short_id=sid[:8],
        cwd="/tmp/demo",
        mtime=1_700_000_000.0,
        time_source="file_mtime",
        event_time=1_700_000_000.0,
        file_mtime=1_700_000_000.0,
        size_bytes=100,
        native_title="Demo",
        fallback_title="Demo",
        status_tag="",
        path=f"/tmp/{sid}.jsonl",
        first_user_msg="hello",
        last_user_msg="hello",
        last_agent_msg="world",
    )


def _registry(sessions=None):
    sessions = sessions or [_session()]

    def scan(limit=50, keep_ids=None, cwd_filter=None):
        return list(sessions)[:limit]

    def load(session):
        return [
            ConversationMessage(role="user", text="hello", timestamp=1.0),
            ConversationMessage(role="assistant", text="world", timestamp=2.0),
        ]

    return ParserRegistry(
        [
            RuntimeParser(
                id="claude",
                display_name="Claude Code",
                _scan=scan,
                _load=load,
            )
        ]
    )


def test_list_envelope():
    with mock.patch.object(cli, "default_registry", _registry):
        code = cli.dispatch(["list", "--compact"])
    assert code == 0


def test_show_messages(capsys):
    with mock.patch.object(cli, "default_registry", _registry):
        code = cli.dispatch(["show", "abc12345", "--full", "--compact"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["data"]["message_count_total"] == 2
    assert out["data"]["messages"][0]["role"] == "user"


def test_describe(capsys):
    with mock.patch.object(cli, "default_registry", _registry):
        code = cli.dispatch(["describe"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    names = {c["name"] for c in out["data"]["commands"]}
    assert {"list", "show", "share", "export", "search", "describe"} <= names
