"""Codex performance adapter tests: staged collector plus core binding gate.

Collector tests run without the shared performance core. Core-binding tests
skip until ``sesskit.performance`` lands (dependency_not_ready, not a pass).
"""

from __future__ import annotations

import importlib.util

import pytest

from sesskit.performance_adapters.codex import (
    FIRST_OUTPUT_LATENCY,
    TURN_ELAPSED,
    collect_codex_turn_performance,
)

_HAS_CORE = importlib.util.find_spec("sesskit.performance") is not None
requires_core = pytest.mark.skipif(
    not _HAS_CORE,
    reason="performance core not landed: dependency_not_ready",
)


def _usage_row(lineno, turn_id, output, response_id="resp-1", model=None):
    payload = {
        "turn_id": turn_id,
        "response_id": response_id,
        "usage": {"output_tokens": 10},
        "turn_token_usage": {"output_tokens": output},
        "thread_token_usage": {"output_tokens": output},
    }
    if model is not None:
        payload["model"] = model
    return (lineno, {"type": "token_usage_record", "payload": payload})


def _complete_row(lineno, turn_id=None, duration_ms=53000, ttft_ms=1200, error=None):
    payload = {
        "type": "task_complete",
        "started_at": 1762000000,
        "completed_at": 1762000053,
        "duration_ms": duration_ms,
        "time_to_first_token_ms": ttft_ms,
    }
    if turn_id is not None:
        payload["turn_id"] = turn_id
    if error is not None:
        payload["error"] = error
    return (lineno, {"type": "event_msg", "payload": payload})


def _abort_row(lineno, turn_id, duration_ms=5000):
    return (
        lineno,
        {
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "turn_id": turn_id,
                "started_at": 1762000000,
                "completed_at": 1762000005,
                "duration_ms": duration_ms,
                "reason": "interrupted",
            },
        },
    )


def _elapsed(records):
    return [r for r in records if r["kind"] == TURN_ELAPSED]


def test_latest_within_turn_counter_wins_no_summing():
    rows = [
        _usage_row(1, "turn-A", 100, response_id="r1"),
        _usage_row(2, "turn-A", 250, response_id="r2"),
        _complete_row(3, "turn-A"),
    ]
    (record,) = _elapsed(collect_codex_turn_performance(rows))
    assert record["output_tokens"] == 250
    assert record["allow_rate"] is True
    assert record["turn_id"] == "turn-A"


def test_two_turns_counters_reset_no_cross_turn_subtraction():
    rows = [
        _usage_row(1, "turn-A", 300, response_id="r1"),
        _complete_row(2, "turn-A"),
        _usage_row(3, "turn-B", 50, response_id="r2"),
        _complete_row(4, "turn-B"),
    ]
    elapsed = _elapsed(collect_codex_turn_performance(rows))
    assert [r["output_tokens"] for r in elapsed] == [300, 50]
    assert [r["turn_id"] for r in elapsed] == ["turn-A", "turn-B"]
    assert all(r["allow_rate"] for r in elapsed)


def test_error_bearing_complete_preserves_timing_but_no_rate():
    rows = [
        _usage_row(1, "turn-A", 100),
        _complete_row(2, "turn-A", error={"message": "usage_limit_exceeded"}),
    ]
    staged = collect_codex_turn_performance(rows)
    (elapsed,) = _elapsed(staged)
    assert elapsed["duration_ms"] == 53000
    assert elapsed["output_tokens"] == 100
    assert elapsed["allow_rate"] is False
    latencies = [r for r in staged if r["kind"] == FIRST_OUTPUT_LATENCY]
    assert len(latencies) == 1
    assert latencies[0]["duration_ms"] == 1200


def test_abort_preserves_duration_no_rate_no_ttft():
    rows = [
        _usage_row(1, "turn-A", 100),
        _abort_row(2, "turn-A"),
    ]
    staged = collect_codex_turn_performance(rows)
    (elapsed,) = _elapsed(staged)
    assert elapsed["duration_ms"] == 5000
    assert elapsed["allow_rate"] is False
    assert [r for r in staged if r["kind"] == FIRST_OUTPUT_LATENCY] == []


def test_first_output_latency_preserved_turn_scoped():
    rows = [_complete_row(1, "turn-A", ttft_ms=3456)]
    staged = collect_codex_turn_performance(rows)
    (latency,) = [r for r in staged if r["kind"] == FIRST_OUTPUT_LATENCY]
    assert latency["duration_ms"] == 3456
    assert latency["turn_id"] == "turn-A"
    assert latency["allow_rate"] is False


def test_missing_turn_id_timing_only_no_rate():
    rows = [_complete_row(1, None)]
    staged = collect_codex_turn_performance(rows)
    (elapsed,) = _elapsed(staged)
    assert elapsed["turn_id"] is None
    assert elapsed["duration_ms"] == 53000
    assert elapsed["allow_rate"] is False
    assert any("missing_turn_id" in d for d in elapsed["diagnostics"])


def test_malformed_and_zero_durations_never_rate_or_crash():
    for bad in ("fast", -5, True, float("nan"), float("inf"), 0, None):
        rows = [
            _usage_row(1, "turn-A", 100),
            _complete_row(2, "turn-A", duration_ms=bad),
        ]
        (elapsed,) = _elapsed(collect_codex_turn_performance(rows))
        assert elapsed["allow_rate"] is False
        if bad is None:
            assert elapsed["duration_ms"] is None


def test_item_completed_rows_are_not_generation_windows():
    rows = [
        (
            1,
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "turn-A",
                    "started_at_ms": 1000,
                    "completed_at_ms": 1955,
                },
            },
        ),
        _usage_row(2, "turn-A", 100),
        _complete_row(3, "turn-A"),
    ]
    staged = collect_codex_turn_performance(rows)
    kinds = {r["kind"] for r in staged}
    assert kinds <= {TURN_ELAPSED, FIRST_OUTPUT_LATENCY}


def test_native_model_attribution_only_no_fill():
    rows = [
        _usage_row(1, "turn-A", 100),
        (2, {"type": "turn_context", "payload": {"turn_id": "turn-A", "model": "gpt-5.2"}}),
        _complete_row(3, "turn-A"),
    ]
    (elapsed,) = _elapsed(collect_codex_turn_performance(rows))
    assert elapsed["model"] == "gpt-5.2"

    rows2 = [
        _usage_row(1, "turn-B", 100),
        _complete_row(2, "turn-B"),
    ]
    (elapsed2,) = _elapsed(collect_codex_turn_performance(rows2))
    assert elapsed2["model"] is None


@requires_core
def test_core_binding_capabilities_and_rate_math():
    from sesskit.performance_adapters.codex import get_capabilities

    caps = get_capabilities()
    assert caps.runtime == "codex"
    assert caps.supported is True
    assert TURN_ELAPSED in caps.timing_kinds
    assert caps.provider_decode_timing is False

    from sesskit.performance import make_performance_record

    record = make_performance_record(
        runtime="codex",
        session_ref="codex:s1",
        record="line:3",
        kind=TURN_ELAPSED,
        scope_kind="turn",
        scope_id="turn-A",
        duration_ms=53000,
        output_tokens=250,
        output_semantics="provider_output",
        evidence=(),
        allow_rate=True,
        clock="monotonic",
    )
    assert record.tokens_per_second == pytest.approx(250 * 1000 / 53000)
