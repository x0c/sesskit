"""U3 usage export: snapshot loading, CLI envelope, paging, selectors, schema."""

from __future__ import annotations

import json
import os
from unittest import mock

from sesskit import cli
from sesskit import usage as usage_mod
from sesskit.models import make_session_info
from sesskit.registry import ParserRegistry, RuntimeParser


def _write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as file:
        file.write("".join(json.dumps(row) + "\n" for row in rows))


def _claude_rows() -> list[dict]:
    return [
        {"type": "assistant", "message": {
            "id": "m1", "model": "m",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {
            "id": "m2", "model": "m",
            "usage": {"input_tokens": 50, "output_tokens": 10},
            "content": [{"type": "text", "text": "yo"}]}},
    ]


def _session(source="claude", sid="abc12345", path="/tmp/x.jsonl"):
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
        path=path,
        first_user_msg="hello",
        last_user_msg="hello",
        last_agent_msg="world",
    )


def _registry(sessions):
    def scan(limit=50, keep_ids=None, cwd_filter=None):
        return list(sessions)[:limit]

    def load(session):
        from sesskit.models import ConversationMessage
        return [ConversationMessage(role="user", text="hello", timestamp=1.0)]

    return ParserRegistry(
        [RuntimeParser(id="claude", display_name="Claude Code", _scan=scan, _load=load)]
    )


def test_schema_dual_copy_identical():
    root = os.path.join(os.path.dirname(__file__), "..", "schemas", "usage.v1.json")
    packaged = os.path.join(os.path.dirname(__file__), "..", "src", "sesskit", "schemas", "usage.v1.json")
    with open(root, encoding="utf-8") as file:
        first = file.read()
    with open(packaged, encoding="utf-8") as file:
        second = file.read()
    assert first == second
    schema = json.loads(first)
    assert schema["properties"]["schema"]["const"] == "sesskit.usage/v1"


def test_payload_shape_matches_schema_required_keys(tmp_path):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    snapshot = usage_mod.load_usage(_session(path=path))
    assert snapshot.state == "available"
    payload = usage_mod.snapshot_to_dict(snapshot)
    for key in ("schema", "runtime", "state", "summary"):
        assert key in payload
    summary = payload["summary"]
    for key in ("runtime", "history_view", "coverage", "metrics", "cost"):
        assert key in summary
    assert set(summary["metrics"]) == {
        "input_total", "cache_read", "cache_write",
        "output_total", "reasoning", "total_tokens",
    }


def test_load_usage_empty_reports_not_recorded(tmp_path):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, [{"type": "assistant", "message": {"id": "m1", "content": []}}])
    snapshot = usage_mod.load_usage(_session(path=path))
    assert snapshot.state == "empty"
    assert snapshot.summary is not None
    assert snapshot.summary.coverage == "not_recorded"


def test_load_usage_missing_file_is_unavailable(tmp_path):
    snapshot = usage_mod.load_usage(_session(path=str(tmp_path / "gone.jsonl")))
    assert snapshot.state == "unavailable"


def test_load_usage_unsupported_runtime():
    snapshot = usage_mod.load_usage(_session(source="cursor", sid="c1", path="/tmp/c.db"))
    assert snapshot.state == "unsupported"
    assert snapshot.summary is not None
    assert snapshot.summary.coverage == "unsupported"


def test_capabilities_table():
    pi_caps = usage_mod.usage_capabilities("pi")
    assert pi_caps.supported is True
    assert pi_caps.auxiliary_support is True
    assert pi_caps.incremental_support is False
    assert usage_mod.usage_capabilities("cursor").supported is False


def test_cursor_stale_and_malformed():
    token = usage_mod.encode_usage_cursor("gen-a", 4)
    assert usage_mod.decode_usage_cursor(token, "gen-a") == 4
    try:
        usage_mod.decode_usage_cursor(token, "gen-b")
    except ValueError as exc:
        assert str(exc) == "stale"
    else:
        raise AssertionError("expected stale cursor")
    try:
        usage_mod.decode_usage_cursor("!!!", "gen-a")
    except ValueError as exc:
        assert str(exc) == "malformed"
    else:
        raise AssertionError("expected malformed cursor")


def test_native_subdivisions_redacted_by_default(tmp_path):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    snapshot = usage_mod.load_usage(_session(path=path))
    plain = usage_mod.observation_to_dict(snapshot.observations[0])
    assert "native_subdivisions" not in plain
    rich = usage_mod.observation_to_dict(snapshot.observations[0], include_native=True)
    assert "native_subdivisions" in rich


def _run_cli(argv, sessions, capsys):
    with mock.patch.object(cli, "default_registry", lambda: _registry(sessions)):
        code = cli.dispatch(argv)
    out = json.loads(capsys.readouterr().out)
    return code, out


def test_cli_summary_envelope(tmp_path, capsys):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    code, out = _run_cli(["usage", "abc12345", "--compact"], [_session(path=path)], capsys)
    assert code == 0
    assert out["ok"] is True
    assert out["data"]["schema"] == "sesskit.usage/v1"
    assert out["data"]["summary"]["metrics"]["input_total"]["known_total"] == 150


def test_cli_detail_paging_roundtrip(tmp_path, capsys):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    code, out = _run_cli(
        ["usage", "abc12345", "--detail", "records", "--limit", "1", "--compact"],
        [_session(path=path)], capsys)
    assert code == 0
    detail = out["data"]["detail"]
    assert detail["returned"] == 1 and detail["total"] == 2 and detail["has_more"] is True
    cursor = detail["cursor"]
    assert cursor
    code, out = _run_cli(
        ["usage", "abc12345", "--detail", "records", "--limit", "1",
         "--cursor", cursor, "--compact"],
        [_session(path=path)], capsys)
    assert code == 0
    second = out["data"]["detail"]
    assert second["returned"] == 1 and second["has_more"] is False
    assert second["items"][0]["usage_id"] != detail["items"][0]["usage_id"]


def test_cli_stale_cursor_is_error(tmp_path, capsys):
    first = str(tmp_path / "a.jsonl")
    second = str(tmp_path / "b.jsonl")
    _write_jsonl(first, _claude_rows())
    _write_jsonl(second, _claude_rows())
    _, out = _run_cli(
        ["usage", "abc12345", "--detail", "records", "--limit", "1", "--compact"],
        [_session(path=first)], capsys)
    cursor = out["data"]["detail"]["cursor"]
    with mock.patch.object(cli, "default_registry", lambda: _registry([_session(path=second)])):
        code = cli.dispatch(["usage", "abc12345", "--detail", "records",
                             "--cursor", cursor, "--compact"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ok"] is False
    assert out["error"]["code"] == "stale_cursor"


def test_cli_bad_cursor_is_usage_error(tmp_path, capsys):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    code, out = _run_cli(
        ["usage", "abc12345", "--detail", "records", "--cursor", "bogus", "--compact"],
        [_session(path=path)], capsys)
    assert code == 2
    assert out["error"]["code"] == "usage_error"


def test_cli_limit_in_summary_mode_is_usage_error(tmp_path, capsys):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    code, _ = _run_cli(["usage", "abc12345", "--limit", "5", "--compact"],
                      [_session(path=path)], capsys)
    assert code == 2


def test_cli_selectors_mutually_exclusive(tmp_path, capsys):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    result = _run_cli(
        ["usage", "abc12345", "--turn-id", "t", "--message-id", "m", "--compact"],
        [_session(path=path)], capsys)
    assert result[0] == 2


def test_cli_missing_ref_is_not_found(capsys):
    code, out = _run_cli(["usage", "nosuchref", "--compact"], [_session()], capsys)
    assert code == 3
    assert out["error"]["code"] == "not_found"


def test_cli_ambiguous_ref(tmp_path, capsys):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    sessions = [_session(sid="abc11111", path=path), _session(sid="abc22222", path=path)]
    code, out = _run_cli(["usage", "abc", "--compact"], sessions, capsys)
    assert code == 5
    assert out["error"]["code"] == "ambiguous"


def test_cli_unavailable_history_is_error(capsys):
    code, out = _run_cli(["usage", "abc12345", "--compact"],
                         [_session(path="/tmp/definitely-gone-sesskit.jsonl")], capsys)
    assert code == 1
    assert out["error"]["code"] == "load_failed"


def test_cli_selector_counts(tmp_path, capsys):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    code, out = _run_cli(["usage", "abc12345", "--message-id", "m1", "--compact"],
                         [_session(path=path)], capsys)
    assert code == 0
    assert out["data"]["matched_records"] == 1
    assert out["data"]["summary"]["metrics"]["input_total"]["known_total"] == 100
