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


def test_metric_total_to_report_prefers_complete_then_known():
    complete = usage_mod.metric_summary_to_dict(usage_mod.MetricSummary(
        known_total=15, complete_total=20, coverage="partial",
    ))
    known = usage_mod.metric_summary_to_dict(usage_mod.MetricSummary(
        known_total=15, complete_total=None, coverage="partial",
    ))
    unknown = usage_mod.metric_summary_to_dict(usage_mod.MetricSummary(coverage="unknown"))
    assert complete["total_to_report"] == 20
    assert complete["coverage"] == "partial"
    assert known["total_to_report"] == 15
    assert known["coverage"] == "partial"
    assert unknown["total_to_report"] is None
    assert unknown["coverage"] == "unknown"


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
    assert out["data"]["summary"]["metrics"]["input_total"]["total_to_report"] == 150


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


def _write_opencode_db(path: str, session_id: str = "ses_rollup1") -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, cost REAL,"
        " tokens_input INTEGER, tokens_output INTEGER,"
        " tokens_reasoning INTEGER, tokens_cache_read INTEGER,"
        " tokens_cache_write INTEGER, model TEXT)")
    conn.execute(
        "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT,"
        " type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER,"
        " data TEXT)")
    conn.execute(
        "INSERT INTO session_v2"
        " (id, cost, tokens_input, tokens_output, tokens_reasoning,"
        "  tokens_cache_read, tokens_cache_write, model)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, 0.5, 300, 200, 40, 10, 5, '{"id": "m1"}'))
    messages = [
        ("m1", "assistant", 1, {
            "modelID": "m1", "cost": 0.2,
            "tokens": {"input": 200, "output": 150, "reasoning": 30,
                       "cache": {"read": 10, "write": 5}}}),
        ("m2", "assistant", 2, {
            "modelID": "m1", "cost": 0.1,
            "tokens": {"input": 50, "output": 20, "reasoning": 5,
                       "cache": {"read": 0, "write": 0}}}),
    ]
    for mid, mtype, seq, data in messages:
        conn.execute(
            "INSERT INTO session_message"
            " (id, session_id, type, seq, time_created, time_updated, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, session_id, mtype, seq, seq, seq, json.dumps(data)))
    conn.commit()
    conn.close()


def test_load_usage_opencode_session_rollup_sets_complete_total(tmp_path):
    import sqlite3

    db_path = str(tmp_path / "opencode.db")
    _write_opencode_db(db_path)
    snapshot = usage_mod.load_usage(
        _session(source="opencode", sid="ses_rollup1", path=db_path))
    assert snapshot.state == "available"
    assert snapshot.summary is not None
    # Message details: input (200+10+5)+(50+0+0) = 265;
    # output (150+30)+(20+5) = 205; total 470.
    assert snapshot.summary.input_total.detail_total == 265
    # Session row: input 300+10+5 = 315; output 200+40 = 240; total 555.
    assert snapshot.summary.total_tokens.complete_total == 555
    assert snapshot.summary.total_tokens.detail_total == 470
    assert snapshot.summary.total_tokens.unattributed == 85
    assert snapshot.summary.cost.complete_total == 0.5
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT tokens_input FROM session_v2 WHERE id = 'ses_rollup1'").fetchone()
    conn.close()
    assert row is not None


def _write_kimi_wire(path: str) -> None:
    rows = [
        {"type": "metadata"},
        {"type": "llm.request", "agentId": "main", "kind": "loop",
         "provider": "openai", "model": "qwen3.5-9b"},
        {"type": "usage.record", "agentId": "main", "model": "localmodel",
         "usage": {"inputOther": 15, "output": 2,
                   "inputCacheRead": 41789, "inputCacheCreation": 0},
         "usageScope": "turn", "time": 1790772283338},
        {"type": "token_counting.turn_recorded", "agentId": "main",
         "turnId": 0, "length": 4, "tokens": 41806, "time": 1790772284052},
    ]
    _write_jsonl(path, rows)


def test_load_usage_kimi_turn_delta_available(tmp_path):
    path = str(tmp_path / "wire.jsonl")
    _write_kimi_wire(path)
    snapshot = usage_mod.load_usage(
        _session(source="kimi", sid="session_demo", path=path))
    assert snapshot.state == "available"
    assert snapshot.summary is not None
    assert snapshot.summary.input_total.known_total == 41804
    assert snapshot.summary.output_total.known_total == 2
    assert snapshot.summary.total_tokens.known_total == 41806
    assert snapshot.summary.total_tokens.complete_total is None
    caps = usage_mod.usage_capabilities("kimi")
    assert caps.supported is True
    assert caps.entity_granularities == ("turn",)


def test_load_usage_kimi_empty_wire_is_not_recorded(tmp_path):
    path = str(tmp_path / "wire.jsonl")
    _write_jsonl(path, [{"type": "metadata"}])
    snapshot = usage_mod.load_usage(
        _session(source="kimi", sid="session_demo", path=path))
    assert snapshot.state == "empty"
    assert snapshot.summary is not None
    assert snapshot.summary.coverage == "not_recorded"


def test_load_usage_claude_cost_state_combines_models(tmp_path):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, [
        {"type": "assistant", "message": {
            "id": "m1", "model": "claude-opus-5-5",
            "usage": {"input_tokens": 2, "output_tokens": 100,
                      "cache_read_input_tokens": 10,
                      "cache_creation_input_tokens": 20,
                      "output_tokens_details": {"thinking_tokens": 40}},
            "content": [{"type": "text", "text": "hi"}]}},
        {"type": "cost-state", "sessionId": "abc12345",
         "totalCostUSD": 1.5,
         "modelUsage": {
             "claude-opus-5-5": {"inputTokens": 2, "outputTokens": 100,
                                 "thinkingTokens": 40,
                                 "cacheReadInputTokens": 10,
                                 "cacheCreationInputTokens": 20,
                                 "costUSD": 1.4},
             "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 10,
                                  "thinkingTokens": 0,
                                  "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0,
                                  "costUSD": 0.1}}},
    ])
    snapshot = usage_mod.load_usage(_session(path=path))
    assert snapshot.state == "available"
    assert snapshot.summary is not None
    assert snapshot.summary.reasoning.known_total == 40
    assert snapshot.summary.total_tokens.complete_total == 147
    assert snapshot.summary.cost.complete_total == 1.5


def test_load_usage_unknown_runtime_is_unsupported():
    snapshot = usage_mod.load_usage(_session(source="bogus", sid="x", path="/tmp/none"))
    assert snapshot.state == "unsupported"
    assert snapshot.summary is not None


def test_load_usage_routes_through_adapter_registry(tmp_path):
    from sesskit.adapters import get_adapter

    path = str(tmp_path / "wire.jsonl")
    _write_kimi_wire(path)
    session = _session(source="kimi", sid="session_demo", path=path)
    via_registry = usage_mod.load_usage(session)
    via_adapter = get_adapter("kimi").load_usage(session)
    assert via_registry.state == "available"
    assert ([o.observation_id for o in via_registry.observations]
            == [o.observation_id for o in via_adapter.observations])
    assert via_registry.summary is not None and via_adapter.summary is not None
    assert (via_registry.summary.total_tokens.known_total
            == via_adapter.summary.total_tokens.known_total == 41806)


def test_observation_ids_are_session_qualified_end_to_end(tmp_path):
    path = str(tmp_path / "s.jsonl")
    _write_jsonl(path, _claude_rows())
    snapshot = usage_mod.load_usage(_session(path=path))
    assert snapshot.state == "available"
    ids = [o.observation_id for o in snapshot.observations]
    assert ids == ["claude:abc12345:line:1:usage", "claude:abc12345:line:2:usage"]
    again = usage_mod.load_usage(_session(path=path))
    assert [o.observation_id for o in again.observations] == ids


def test_equal_lines_in_different_sessions_stay_distinct(tmp_path):
    first = str(tmp_path / "a.jsonl")
    second = str(tmp_path / "b.jsonl")
    _write_jsonl(first, _claude_rows())
    _write_jsonl(second, _claude_rows())
    snap_a = usage_mod.load_usage(_session(sid="abc11111", path=first))
    snap_b = usage_mod.load_usage(_session(sid="abc22222", path=second))
    ids_a = {o.observation_id for o in snap_a.observations}
    ids_b = {o.observation_id for o in snap_b.observations}
    assert ids_a.isdisjoint(ids_b)
