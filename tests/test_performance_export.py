"""Performance export: schema copies, synthetic payload shape, cursor codec.

Core-dependent tests skip until sesskit.performance lands (dependency_not_ready);
nothing here fakes core types.
"""

from __future__ import annotations

import json
import math
import os

import pytest

from sesskit import performance_cli as perf_cli

ROOT_SCHEMA = os.path.join(os.path.dirname(__file__), "..", "schemas", "performance.v1.json")
PACKAGED_SCHEMA = os.path.join(os.path.dirname(__file__), "..", "src", "sesskit", "schemas", "performance.v1.json")


def _load_schema() -> dict:
    with open(ROOT_SCHEMA, encoding="utf-8") as file:
        return json.load(file)


def test_schema_dual_copy_identical():
    with open(ROOT_SCHEMA, encoding="utf-8") as file:
        first = file.read()
    with open(PACKAGED_SCHEMA, encoding="utf-8") as file:
        second = file.read()
    assert first == second
    schema = json.loads(first)
    assert schema["properties"]["schema"]["const"] == "sesskit.performance/v1"


def test_schema_types_record_summary_capability_fields():
    schema = _load_schema()
    record = schema["$defs"]["performanceRecord"]
    assert record["properties"]["kind"]["enum"] == [
        "response_elapsed",
        "provider_phase_elapsed",
        "step_elapsed",
        "stream_elapsed",
        "turn_elapsed",
        "first_output_latency",
        "first_part_latency",
        "request_to_reply_elapsed",
        "thinking_elapsed",
        "session_elapsed",
        "session_api_elapsed",
        "session_api_without_retries_elapsed",
        "session_tool_elapsed",
        "native_duration",
    ]
    assert record["properties"]["output_semantics"]["enum"] == [
        "inclusive_output",
        "visible_output",
        "provider_output",
        "unknown",
    ]
    assert record["properties"]["rate_basis"]["enum"] == ["derived", "unavailable"]
    assert record["properties"]["clock"]["enum"] == ["monotonic", "wall", "mixed", "unknown"]
    summary = schema["$defs"]["performanceSummary"]
    assert summary["required"] == ["coverage", "record_count", "rate_count", "observed_operations", "kinds", "latest"]
    assert summary["properties"]["coverage"]["enum"] == ["partial", "unknown", "not_recorded", "unsupported"]
    caps = schema["$defs"]["performanceCapabilities"]
    assert caps["required"] == [
        "runtime",
        "supported",
        "timing_kinds",
        "rate_kinds",
        "history_views",
        "provider_decode_timing",
        "incremental_support",
    ]
    assert schema["properties"]["state"]["enum"] == ["available", "empty", "unavailable", "unsupported"]


def _synthetic_record() -> dict:
    return {
        "performance_id": "pi:abc12345:line:7:response_elapsed",
        "runtime": "pi",
        "record": "line:7",
        "scope_kind": "message",
        "scope_id": "m7",
        "kind": "response_elapsed",
        "duration_ms": 1250.0,
        "output_tokens": 50,
        "output_semantics": "provider_output",
        "tokens_per_second": 40.0,
        "rate_basis": "derived",
        "clock": "monotonic",
        "started_at": 1_700_000_000.0,
        "ended_at": 1_700_000_001.25,
        "message_id": "m7",
        "turn_id": None,
        "response_id": "r7",
        "model": "m",
        "provider": None,
        "evidence": [
            {"origin": "native", "field": "durationMs", "record": "line:7"},
            {"origin": "inferred", "field": "tokens_per_second", "record": "line:7"},
        ],
        "diagnostics": [],
    }


def test_synthetic_real_shaped_record_has_finite_nullable_numbers():
    schema = _load_schema()
    record_def = schema["$defs"]["performanceRecord"]
    item = _synthetic_record()
    for key in record_def["required"]:
        assert key in item, key
    assert item["kind"] in record_def["properties"]["kind"]["enum"]
    assert item["output_semantics"] in record_def["properties"]["output_semantics"]["enum"]
    assert item["rate_basis"] in record_def["properties"]["rate_basis"]["enum"]
    assert item["clock"] in record_def["properties"]["clock"]["enum"]
    for key in ("duration_ms", "tokens_per_second", "started_at", "ended_at"):
        assert item[key] is None or math.isfinite(item[key]), key
    assert item["output_tokens"] is None or item["output_tokens"] >= 0
    assert item["tokens_per_second"] == pytest.approx(50 * 1000 / 1250.0)
    for entry in item["evidence"]:
        assert entry["origin"] in ("native", "inferred", "unknown")
    assert "content" not in item and "path" not in item


def test_cursor_roundtrip_and_refusals():
    token = perf_cli.encode_performance_cursor(
        watermark="gen-a",
        session_ref="pi:abc",
        history_view="all_branches",
        detail="records",
        turn_id=None,
        message_id=None,
        offset=4,
    )
    assert (
        perf_cli.decode_performance_cursor(
            token,
            watermark="gen-a",
            session_ref="pi:abc",
            history_view="all_branches",
            detail="records",
            turn_id=None,
            message_id=None,
        )
        == 4
    )
    # Stale generation.
    with pytest.raises(ValueError, match="stale"):
        perf_cli.decode_performance_cursor(
            token,
            watermark="gen-b",
            session_ref="pi:abc",
            history_view="all_branches",
            detail="records",
            turn_id=None,
            message_id=None,
        )
    # Cross-scope refusal: session, view, detail and filters all bind.
    for kwargs in (
        {
            "watermark": "gen-a",
            "session_ref": "pi:other",
            "history_view": "all_branches",
            "detail": "records",
            "turn_id": None,
            "message_id": None,
        },
        {
            "watermark": "gen-a",
            "session_ref": "pi:abc",
            "history_view": "active_branch",
            "detail": "records",
            "turn_id": None,
            "message_id": None,
        },
        {
            "watermark": "gen-a",
            "session_ref": "pi:abc",
            "history_view": "all_branches",
            "detail": "summary",
            "turn_id": None,
            "message_id": None,
        },
        {
            "watermark": "gen-a",
            "session_ref": "pi:abc",
            "history_view": "all_branches",
            "detail": "records",
            "turn_id": "t1",
            "message_id": None,
        },
    ):
        with pytest.raises(ValueError, match="stale"):
            perf_cli.decode_performance_cursor(token, **kwargs)
    # Malformed token.
    with pytest.raises(ValueError, match="malformed"):
        perf_cli.decode_performance_cursor(
            "!!!",
            watermark="gen-a",
            session_ref="pi:abc",
            history_view="all_branches",
            detail="records",
            turn_id=None,
            message_id=None,
        )


def test_cursor_carries_no_paths_or_content():
    import base64

    token = perf_cli.encode_performance_cursor(
        watermark="gen-a",
        session_ref="pi:abc",
        history_view="all_branches",
        detail="records",
        turn_id=None,
        message_id=None,
        offset=0,
    )
    raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    assert "/tmp" not in raw and "content" not in raw and "prompt" not in raw


@pytest.mark.parametrize("state", ["unsupported", "empty"])
def test_core_state_payload_shapes(state):
    perf = pytest.importorskip("sesskit.performance")
    if state == "unsupported":
        snapshot = perf.unsupported_performance_snapshot(
            runtime="cursor", session_ref="cursor:c1", history_view="all_branches"
        )
    else:
        snapshot = perf.PerformanceSnapshot(
            state="empty",
            runtime="pi",
            session_ref="pi:abc",
            history_view="all_branches",
            records=(),
            entries_read=3,
            watermark="gen-x",
            observed_operations=0,
            coverage="not_recorded",
        )
    assert snapshot.state == state
    payload = perf.performance_snapshot_to_dict(snapshot)
    assert payload["schema"] == "sesskit.performance/v1"
    assert payload["state"] == state
    assert isinstance(payload["watermark"], bool)
    summary = perf.performance_summary(snapshot)
    assert summary["record_count"] == 0 and summary["rate_count"] == 0
    assert summary["latest"] is None


def test_core_record_roundtrip_derives_rate():
    perf = pytest.importorskip("sesskit.performance")
    from sesskit.models import Evidence

    record = perf.make_performance_record(
        runtime="pi",
        session_ref="pi:abc",
        record="line:7",
        kind="response_elapsed",
        scope_kind="message",
        scope_id="m7",
        duration_ms=1250.0,
        output_tokens=50,
        output_semantics="provider_output",
        evidence=(Evidence(origin="native", field="durationMs", record="line:7"),),
        allow_rate=True,
        clock="monotonic",
        message_id="m7",
        response_id="r7",
    )
    assert record.tokens_per_second == pytest.approx(40.0)
    assert record.rate_basis == "derived"
    payload = perf.performance_record_to_dict(record)
    assert payload["performance_id"] and payload["tokens_per_second"] == pytest.approx(40.0)
    assert math.isfinite(payload["tokens_per_second"])
