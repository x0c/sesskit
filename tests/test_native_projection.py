"""Differential coverage for compact native projections and Python fallbacks."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sesskit import native_projection as bridge
from sesskit.parsers import claude, codex, opencode


def native():
    backend = bridge._backend()
    if backend is None:
        pytest.skip("native wheel/development extension not installed")
    return backend


def view(profile, record):
    if profile == "claude_excerpt":
        return claude._last_texts([record])
    if profile == "codex_excerpt":
        return codex.user_message_text(record), codex.assistant_message_text(record), codex._activity_time(record)
    return (
        opencode._v2_user_text(record), opencode._v2_assistant_text(record),
        opencode.error_text_from_msg(record), opencode._error_code_from_msg(record),
        opencode._error_kind_from_msg(record), opencode._synthetic_goal_text(record),
        [opencode._status_tag_v2([(kind, record)]) for kind in ("user", "assistant", "idle", "synthetic")],
    )


CASES = [
    ("claude_excerpt", {"type": "user", "message": {"content": "hello"}}),
    ("claude_excerpt", {"type": "user", "message": {"content": "[Request interrupted by user]"}}),
    ("claude_excerpt", {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "input": {"large": "x" * 10_000}},
        {"type": "text", "text": "answer ☃ 😀\nnext"},
    ]}}),
    ("claude_excerpt", {"type": "attachment", "isMeta": False, "attachment": {
        "type": "queued_command", "prompt": "next", "origin": {"kind": "human"}}}),
    ("claude_excerpt", {"type": "attachment", "isMeta": True, "attachment": {
        "type": "queued_command", "prompt": "injected", "humanTurn": True}}),
    ("claude_excerpt", {"type": "attachment", "attachment": {
        "type": "queued_command", "prompt": "<task-notification>injected", "humanTurn": True}}),
    ("claude_excerpt", {"type": "attachment", "attachment": {
        "type": "queued_command", "prompt": "no origin"}}),
    ("codex_excerpt", {"type": "event_msg", "timestamp": "2026-11-01T10:00:00Z",
                       "payload": {"type": "user_message", "message": "hello"}}),
    ("codex_excerpt", {"type": "event_msg", "payload": {"type": "agent_message", "message": "answer"}}),
    ("codex_excerpt", {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "answer"}, {"type": "tool", "input": "x" * 10_000}]}}),
    ("codex_excerpt", {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "# AGENTS.md instructions\ninjected"}]}}),
    ("codex_excerpt", {"type": "event_msg", "timestamp": "2026-11-01T10:00:00Z",
                       "payload": {"type": "token_count", "timestamp": "2026-11-01T10:00:01Z"}}),
    ("opencode_list", {"text": '"a quoted user prompt"', "finish": "stop", "error": None}),
    ("opencode_list", {"content": [{"type": "text", "text": "reply"},
                                     {"type": "tool", "state": {"content": "x" * 10_000}}], "finish": "stop"}),
    ("opencode_list", {"finish": "stop", "error": {"type": "APIError", "message": "quota", "status": 429}}),
    ("opencode_list", {"error": {"name": "APIError", "data": {"message": "failure", "statusCode": 503}}}),
    ("opencode_list", {"outcome": "failed"}),
    ("opencode_list", {"metadata": {"source": "goal"}, "description": "Goal: implement search"}),
    ("opencode_list", {"metadata": {"source": "goal"},
                       "text": "A goal is now active <untrusted_objective>search</untrusted_objective>"}),
    ("opencode_list", {"text": False, "content": None, "finish": 0, "error": {}, "metadata": None}),
]


@pytest.mark.parametrize("profile,record", CASES)
def test_native_projection_preserves_existing_interpretation(profile, record):
    backend = native()
    record = {**record, "unused_tool_payload": "private" * 10_000}
    raw = json.dumps(record, ensure_ascii=False)
    projected = backend.project_json_many([raw], profile)[0]
    assert projected is not None
    assert len(projected) < len(raw) // 2
    assert view(profile, json.loads(projected)) == view(profile, record)


@pytest.mark.parametrize("raw", [
    '{"te\\u0078t":"first","text":"last","padding":"' + "x" * 5000 + '"}',
    '{"text":999999999999999999999999999999999999999999,"padding":"' + "x" * 5000 + '"}',
    '{"text":"\\ud800","padding":"' + "x" * 5000 + '"}',
    '{"text":NaN,"padding":"' + "x" * 5000 + '"}',
    '{"text":Infinity,"padding":"' + "x" * 5000 + '"}',
    '{"text":"ok","padding":"' + "x" * 5000 + '"} trailing',
    '{"text":"ok","padding":[' + "0," * 2500 + '}',
])
def test_edge_encodings_keep_original_decoder_semantics(raw):
    native()
    result = bridge.project_records([raw], "opencode_list")[0]
    try:
        original = json.loads(raw)
    except ValueError:
        with pytest.raises(ValueError):
            json.loads(result)
    else:
        assert opencode._v2_user_text(json.loads(result)) == opencode._v2_user_text(original)


def test_disable_switch_does_not_import_backend(monkeypatch):
    monkeypatch.setenv("SESSKIT_NATIVE", "0")
    monkeypatch.setattr(bridge.importlib, "import_module", lambda name: pytest.fail("native import while disabled"))
    records = [json.dumps({"text": "ok", "padding": "x" * 5000})]
    assert bridge.project_records(records, "opencode_list") is records
    assert not bridge.native_available()


def test_missing_backend_and_protocol_mismatch_fall_back(monkeypatch):
    monkeypatch.delenv("SESSKIT_NATIVE", raising=False)
    records = ['{"padding":"' + "x" * 5000 + '"}']

    def missing(name):
        raise ImportError("not installed")

    monkeypatch.setattr(bridge.importlib, "import_module", missing)
    assert bridge.project_records(records, "opencode_list") is records
    monkeypatch.setattr(bridge.importlib, "import_module", lambda name: SimpleNamespace(PROTOCOL_VERSION=99))
    assert bridge.project_records(records, "opencode_list") is records


def test_small_bytes_and_unusual_inputs_use_original_decoder(monkeypatch):
    monkeypatch.setattr(bridge, "_backend", lambda: pytest.fail("unnecessary backend call"))
    for records in ([], ["{}"], [b"{}"], [None], [{"text": "already decoded"}]):
        assert bridge.project_records(records, "opencode_list") is records


def test_lone_surrogate_at_ffi_boundary_falls_back():
    native()
    records = [json.dumps({"text": "\ud800", "padding": "x" * 5000}, ensure_ascii=False)]
    assert bridge.project_records(records, "opencode_list") is records


def test_bridge_really_uses_native_and_preserves_error_only_rows():
    native()
    raw = json.dumps({"finish": "stop", "error": {"message": "quota", "status": 429}, "tool": "x" * 5000})
    projected = bridge.project_records([raw], "opencode_list")[0]
    assert len(projected) < 100
    assert opencode._status_tag_v2([("assistant", json.loads(projected))]) == opencode.titles.STATUS_ABORTED


@pytest.mark.parametrize("parser,read_tail", [(claude, "_read_tail"), (codex, "_read_session_tail")])
def test_excerpt_projection_keeps_separate_status_tail(tmp_path, monkeypatch, parser, read_tail):
    native()
    path = tmp_path / "history.jsonl"
    rows = [
        {"type": "user", "message": {"content": "prompt"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "reply"}]}},
        {"type": "system", "discard": "x" * 6000},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    reader = getattr(parser, read_tail)
    assert reader(str(path), max_bytes=20_000) == rows
    projected = reader(str(path), max_bytes=20_000, excerpt=True)
    assert "discard" not in projected[-1]
    monkeypatch.setenv("SESSKIT_NATIVE", "0")
    assert reader(str(path), max_bytes=20_000, excerpt=True) == rows
