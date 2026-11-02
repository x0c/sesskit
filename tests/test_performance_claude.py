"""Claude performance adapter tests: join, durations, guards, views.

Skipped until the core ``sesskit.performance`` module lands; the collector
and loader below are written against the locked performance API.
"""

from __future__ import annotations

import json

import pytest

performance = pytest.importorskip("sesskit.performance", reason="core performance module has not landed")
perf_claude = pytest.importorskip(
    "sesskit.performance_adapters.claude",
    reason="core performance module has not landed",
)


def _write_fixture(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return str(path)


def _session(path, session_id="s1"):
    return {"source": "claude", "id": session_id, "path": path}


def _request_row(request_id, timestamp, lineno_marker="r"):
    assert lineno_marker  # placeholder for readability of ordered fixtures
    return {
        "type": "api-request",
        "id": request_id,
        "timestamp": timestamp,
        "sessionId": "s1",
        "version": "2.1.316",
        "querySource": "sdk",
        "shapeHash": "h",
        "params": {"model": "m"},
        "keep": 0,
        "tail": [],
        "cacheControl": [],
        "requestDigest": "d",
    }


def _reply_row(request_ref, timestamp, message_id, model="m", request_id="req-api", thinking=None):
    row = {
        "type": "assistant",
        "timestamp": timestamp,
        "requestRef": request_ref,
        "requestId": request_id,
        "message": {
            "id": message_id,
            "model": model,
            "role": "assistant",
            "type": "message",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
        "uuid": "u-" + message_id,
    }
    if thinking is not None:
        row["thinkingDurationMs"] = thinking
    return row


def test_request_to_reply_join_uses_last_flush(tmp_path):
    path = _write_fixture(
        tmp_path,
        "s.jsonl",
        [
            _request_row("req-1", "2026-11-01T08:22:16.181Z"),
            _reply_row("req-1", "2026-11-01T08:22:22.352Z", "msg-1"),
            _reply_row("req-1", "2026-11-01T08:22:23.592Z", "msg-1"),
        ],
    )
    snapshot = perf_claude.load(_session(path))
    assert snapshot.state == "available"
    assert snapshot.coverage == "partial"
    joined = [r for r in snapshot.records if r.kind == "request_to_reply_elapsed"]
    assert len(joined) == 1
    record = joined[0]
    assert record.clock == "wall"
    assert record.duration_ms == pytest.approx((23.592 - 16.181) * 1000.0)
    assert record.started_at == pytest.approx(1793521336.181)
    assert record.ended_at == pytest.approx(1793521343.592)
    assert record.message_id == "msg-1"
    assert record.scope_kind == "call"
    assert record.scope_id == "req-1"
    assert record.tokens_per_second is None
    assert record.rate_basis == "unavailable"
    assert record.output_tokens is None


def test_no_rate_derived_anywhere(tmp_path):
    path = _write_fixture(
        tmp_path,
        "s.jsonl",
        [
            _request_row("req-1", "2026-11-01T08:22:16.181Z"),
            _reply_row("req-1", "2026-11-01T08:22:22.352Z", "msg-1", thinking=1106),
            {
                "type": "system",
                "subtype": "turn_duration",
                "durationMs": 640907,
                "messageCount": 110,
                "timestamp": "2026-11-01T10:44:22.613Z",
            },
            {
                "type": "cost-state",
                "totalDuration": 10881,
                "totalAPIDuration": 10149,
                "totalAPIDurationWithoutRetries": 10144,
                "totalToolDuration": 18,
                "startTime": 1793532628527,
                "sessionId": "s1",
                "modelUsage": {},
            },
        ],
    )
    snapshot = perf_claude.load(_session(path))
    assert snapshot.records
    assert all(r.tokens_per_second is None for r in snapshot.records)
    assert all(r.rate_basis == "unavailable" for r in snapshot.records)
    kinds = {r.kind for r in snapshot.records}
    assert {
        "request_to_reply_elapsed",
        "thinking_elapsed",
        "turn_elapsed",
        "session_elapsed",
        "session_api_elapsed",
        "session_api_without_retries_elapsed",
        "session_tool_elapsed",
    } <= kinds


def test_cost_state_latest_revision_only(tmp_path):
    path = _write_fixture(
        tmp_path,
        "s.jsonl",
        [
            {
                "type": "cost-state",
                "totalDuration": 1000,
                "totalAPIDuration": 900,
                "totalAPIDurationWithoutRetries": 890,
                "totalToolDuration": 10,
                "startTime": 1000000000000,
                "sessionId": "s1",
                "modelUsage": {},
            },
            {
                "type": "cost-state",
                "totalDuration": 2000,
                "totalAPIDuration": 1800,
                "totalAPIDurationWithoutRetries": 1790,
                "totalToolDuration": 20,
                "startTime": 1000000000000,
                "sessionId": "s1",
                "modelUsage": {},
            },
        ],
    )
    snapshot = perf_claude.load(_session(path))
    by_kind = {r.kind: r for r in snapshot.records}
    assert by_kind["session_elapsed"].duration_ms == 2000.0
    assert by_kind["session_tool_elapsed"].duration_ms == 20.0
    assert any("cost_state_revisions_superseded=1" in d for d in snapshot.diagnostics)


def test_guards_missing_request_reversal_repeats(tmp_path):
    path = _write_fixture(
        tmp_path,
        "s.jsonl",
        [
            _request_row("req-1", "2026-11-01T08:22:16.181Z"),
            _request_row("req-1", "2026-11-01T08:22:17.000Z"),
            _request_row("req-bad", "not-a-timestamp"),
            _reply_row("req-1", "2026-11-01T08:22:10.000Z", "msg-early"),
            _reply_row("req-orphan", "2026-11-01T08:22:30.000Z", "msg-orphan"),
            _reply_row(None, "2026-11-01T08:22:31.000Z", "msg-nolink"),
        ],
    )
    snapshot = perf_claude.load(_session(path))
    assert snapshot.state == "available"
    by_scope = {r.scope_id: r for r in snapshot.records if r.kind == "request_to_reply_elapsed"}
    assert by_scope["req-1"].duration_ms is None
    assert "clock_reversal_end_before_dispatch" in by_scope["req-1"].diagnostics
    assert by_scope["req-orphan"].duration_ms is None
    assert "request_record_missing" in by_scope["req-orphan"].diagnostics
    joined_text = " ".join(snapshot.diagnostics)
    assert "repeated_api_request_ids_skipped=1" in joined_text
    assert "invalid_api_request_timestamps=1" in joined_text
    assert "assistant_replies_without_request_link=1" in joined_text


def test_invalid_native_values_do_not_crash(tmp_path):
    path = _write_fixture(
        tmp_path,
        "s.jsonl",
        [
            _reply_row("req-1", "2026-11-01T08:22:22.352Z", "msg-1", thinking=True),
            {
                "type": "system",
                "subtype": "turn_duration",
                "durationMs": -5,
                "messageCount": 3,
                "timestamp": "2026-11-01T10:44:22.613Z",
            },
            {
                "type": "cost-state",
                "totalDuration": float("nan"),
                "totalAPIDuration": "lots",
                "totalAPIDurationWithoutRetries": None,
                "totalToolDuration": 18,
                "startTime": True,
                "sessionId": "s1",
                "modelUsage": {},
            },
        ],
    )
    snapshot = perf_claude.load(_session(path))
    assert snapshot.state == "available"
    assert all(r.duration_ms is None or r.duration_ms >= 0 for r in snapshot.records)
    assert snapshot.records  # locators retained, nothing fabricated


def test_empty_and_unreadable_histories(tmp_path):
    empty = _write_fixture(tmp_path, "empty.jsonl", [])
    snapshot = perf_claude.load(_session(empty))
    assert snapshot.state == "empty"
    assert snapshot.coverage == "not_recorded"
    missing = perf_claude.load(_session(str(tmp_path / "gone.jsonl")))
    assert missing.state == "unavailable"
    assert missing.coverage == "unknown"
    assert not missing.records


def test_history_view_validation():
    with pytest.raises(ValueError):
        perf_claude.load({"source": "claude", "id": "s", "path": "nowhere.jsonl"}, history_view="active-branch")


def test_capabilities_declare_no_rates():
    capabilities = perf_claude.CAPABILITIES
    assert capabilities.supported is True
    assert capabilities.rate_kinds == ()
    assert capabilities.provider_decode_timing is False
    assert "request_to_reply_elapsed" in capabilities.timing_kinds


def test_adapter_lazy_methods_delegate():
    from sesskit.adapters.claude import adapter

    assert hasattr(adapter, "load_performance")
    assert hasattr(adapter, "performance_capabilities")
    assert adapter.performance_capabilities() is perf_claude.CAPABILITIES
