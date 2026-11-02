"""Kimi performance adapter: durable per-step timing and turn durations.

Native source: ``agents/main/wire.jsonl`` loop events
(``context.append_loop_event`` wrapping ``step.end``) and top-level
``turn.ended`` records (kimi-code 2.4.1 writer bundle).

All durable ``llm*Ms`` fields are client-observed wall-clock measurements
taken around streamed parts; names containing ``Server`` are writer labels
only and never provider-reported server time. Throughput is therefore
client-observed: ``usage.output / (llmStreamDurationMs / 1000)`` on
successful steps with trustworthy native counts. Error/aborted steps carry
minimal keys (no usage, no timing) and yield explicit no-rate records,
never zero speed. Standalone ``usage.record`` rows are never joined by
adjacency and usage totals are untouched here.
"""

from __future__ import annotations

import math
from typing import Any

from sesskit.models import Evidence
from sesskit.performance import (
    PerformanceCapabilities,
    PerformanceSnapshot,
    make_performance_record,
    validate_performance_view,
)

RUNTIME = "kimi"

_STREAM_DURATION_FIELD = "llmStreamDurationMs"
_FIRST_PART_FIELD = "llmFirstTokenLatencyMs"
_OUTPUT_FIELD = "usage.output"
_EXTRA_TIMING_FIELDS = (
    "llmRequestBuildMs",
    "llmServerFirstTokenMs",
    "llmServerDecodeMs",
    "llmClientConsumeMs",
    "llmClientBlockedMs",
)
_ERROR_FINISH_REASONS = frozenset({"error", "aborted", "failed", "cancelled"})

CAPABILITIES = PerformanceCapabilities(
    runtime=RUNTIME,
    supported=True,
    timing_kinds=("stream_elapsed", "first_part_latency", "turn_elapsed", "native_duration"),
    rate_kinds=("stream_elapsed",),
    history_views=("all_branches",),
    provider_decode_timing=False,
    incremental_support=False,
)


def _native_id(value: Any) -> str | None:
    """Native identifier text; bools and empties are never identifiers."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _valid_count(value: Any) -> int | None:
    """Non-negative integer token count; bools and negatives rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _classify_duration(
    raw: Any, *, missing_issue: str, untrusted_issue: str, zero_issue: str
) -> tuple[float | int | None, str | None]:
    """Normalize one native interval to positive-or-None with one issue."""
    if raw is None:
        return None, missing_issue
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return None, untrusted_issue
    if raw == 0:
        return None, zero_issue
    if raw < 0:
        return None, untrusted_issue
    return raw, None


def _native_event(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Unwrap one durable envelope into its native event dict (or None)."""
    if entry.get("type") == "context.append_loop_event":
        inner = entry.get("event")
        return inner if isinstance(inner, dict) else None
    return entry


def _events_of_type(rows: list[tuple[int, dict[str, Any]]], event_type: str) -> Any:
    """Yield ``(line_number, event)`` pairs for one durable event type."""
    for lineno, entry in rows:
        event = _native_event(entry)
        if event is not None and event.get("type") == event_type:
            yield lineno, event


def collect_step_specs(rows: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Fixture-testable extraction of one spec per durable ``step.end``."""
    specs: list[dict[str, Any]] = []
    for lineno, event in _events_of_type(rows, "step.end"):
        usage = event.get("usage")
        usage_map = usage if isinstance(usage, dict) else {}
        raw_output = usage_map.get("output") if usage_map else None
        output = _valid_count(raw_output)
        stream_ms, stream_issue = _classify_duration(
            event.get(_STREAM_DURATION_FIELD),
            missing_issue="missing_stream_duration_ms",
            untrusted_issue="untrusted_stream_duration_ms",
            zero_issue="nonpositive_stream_duration_no_rate",
        )
        first_ms, first_issue = _classify_duration(
            event.get(_FIRST_PART_FIELD),
            missing_issue="",
            untrusted_issue="untrusted_first_part_latency_ms",
            zero_issue="nonpositive_first_part_latency_ms",
        )
        extras: list[tuple[str, float | int]] = []
        issues: list[str] = []
        finish = event.get("finishReason")
        finish_reason = finish if isinstance(finish, str) else None
        if finish_reason in _ERROR_FINISH_REASONS:
            issues.append("error_finish_no_rate")
        if not isinstance(usage, dict) or "output" not in usage_map:
            issues.append("missing_usage_output")
        elif output is None:
            issues.append("untrusted_usage_output")
        if stream_issue:
            issues.append(stream_issue)
        if first_issue:
            issues.append(first_issue)
        for field in _EXTRA_TIMING_FIELDS:
            raw_value = event.get(field)
            if raw_value is None:
                continue
            value, issue = _classify_duration(
                raw_value,
                missing_issue="",
                untrusted_issue=f"untrusted_{field}",
                zero_issue=f"nonpositive_{field}_no_record",
            )
            if value is None:
                issues.append(issue or f"untrusted_{field}")
                continue
            extras.append((field, value))
        specs.append(
            {
                "locator": f"line:{lineno}",
                "turn_id": _native_id(event.get("turnId")),
                "step": (
                    event.get("step")
                    if isinstance(event.get("step"), int) and not isinstance(event.get("step"), bool)
                    else None
                ),
                "uuid": (event.get("uuid") if isinstance(event.get("uuid"), str) else None),
                "message_id": (event.get("messageId") if isinstance(event.get("messageId"), str) else None),
                "finish_reason": finish_reason,
                "output_tokens": output,
                "stream_ms": stream_ms,
                "first_ms": first_ms,
                "extra": tuple(extras),
                "issues": tuple(issues),
            }
        )
    return specs


def collect_turn_specs(rows: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Fixture-testable extraction of one spec per ``turn.ended``."""
    specs: list[dict[str, Any]] = []
    for lineno, event in _events_of_type(rows, "turn.ended"):
        duration_ms, duration_issue = _classify_duration(
            event.get("durationMs"),
            missing_issue="missing_turn_duration_ms",
            untrusted_issue="untrusted_turn_duration_ms",
            zero_issue="nonpositive_turn_duration_ms",
        )
        issues: list[str] = []
        if duration_issue:
            issues.append(duration_issue)
        reason = event.get("reason")
        specs.append(
            {
                "locator": f"line:{lineno}",
                "turn_id": _native_id(event.get("turnId")),
                "duration_ms": duration_ms,
                "reason": reason if isinstance(reason, str) else None,
                "issues": tuple(issues),
            }
        )
    return specs


def _native_evidence(field: str, locator: str) -> Evidence:
    return Evidence(origin="native", field=field, record=locator)


def _records_for_step(spec: dict[str, Any], *, session_ref: str | None) -> list[Any]:
    """Build performance records for one step spec; never invents a rate."""
    locator = str(spec["locator"])
    turn_id = spec["turn_id"]
    scope_kind = "turn" if turn_id else "unknown"
    issues = list(spec["issues"])
    records: list[Any] = []
    stream_ms = spec["stream_ms"]
    output = spec["output_tokens"]
    rated = (
        output is not None
        and isinstance(stream_ms, (int, float))
        and stream_ms > 0
        and spec["finish_reason"] not in _ERROR_FINISH_REASONS
    )
    if isinstance(stream_ms, (int, float)):
        evidence = [_native_evidence(_STREAM_DURATION_FIELD, locator)]
        if output is not None:
            evidence.append(_native_evidence(_OUTPUT_FIELD, locator))
        diagnostics = list(issues)
        if rated:
            diagnostics.append("client_observed_throughput_not_provider_decode")
        records.append(
            make_performance_record(
                runtime=RUNTIME,
                session_ref=session_ref,
                record=locator,
                kind="stream_elapsed",
                scope_kind=scope_kind,
                scope_id=turn_id,
                duration_ms=stream_ms,
                output_tokens=output,
                output_semantics="provider_output",
                evidence=tuple(evidence),
                allow_rate=rated,
                clock="wall",
                message_id=spec["message_id"],
                turn_id=turn_id,
                diagnostics=tuple(diagnostics),
            )
        )
        issues = []
    first_ms = spec["first_ms"]
    if isinstance(first_ms, (int, float)):
        records.append(
            make_performance_record(
                runtime=RUNTIME,
                session_ref=session_ref,
                record=locator,
                kind="first_part_latency",
                scope_kind=scope_kind,
                scope_id=turn_id,
                duration_ms=first_ms,
                allow_rate=False,
                clock="wall",
                evidence=(_native_evidence(_FIRST_PART_FIELD, locator),),
                message_id=spec["message_id"],
                turn_id=turn_id,
                diagnostics=tuple(issues + ["client_first_part_latency_not_provider_ttft"]),
            )
        )
        issues = []
    for field, value in spec["extra"]:
        diagnostics = list(issues)
        if "Server" in field:
            diagnostics.append("client_observed_not_provider_decode")
        field_locator = f"{locator}:{field}"
        records.append(
            make_performance_record(
                runtime=RUNTIME,
                session_ref=session_ref,
                record=field_locator,
                kind="native_duration",
                scope_kind=scope_kind,
                scope_id=turn_id,
                duration_ms=value,
                allow_rate=False,
                clock="wall",
                evidence=(_native_evidence(field, field_locator),),
                message_id=spec["message_id"],
                turn_id=turn_id,
                diagnostics=tuple(diagnostics),
            )
        )
        issues = []
    if not records:
        records.append(
            make_performance_record(
                runtime=RUNTIME,
                session_ref=session_ref,
                record=locator,
                kind="step_elapsed",
                scope_kind=scope_kind,
                scope_id=turn_id,
                duration_ms=None,
                output_tokens=output,
                output_semantics="provider_output" if output is not None else "unknown",
                allow_rate=False,
                clock="wall",
                message_id=spec["message_id"],
                turn_id=turn_id,
                diagnostics=tuple(issues or ["missing_timing_no_rate"]),
            )
        )
    return records


def _record_for_turn(spec: dict[str, Any], *, session_ref: str | None) -> Any:
    """Build the wall-clock turn record; never a divisor for a rate."""
    locator = str(spec["locator"])
    turn_id = spec["turn_id"]
    return make_performance_record(
        runtime=RUNTIME,
        session_ref=session_ref,
        record=locator,
        kind="turn_elapsed",
        scope_kind="turn" if turn_id else "unknown",
        scope_id=turn_id,
        duration_ms=spec["duration_ms"],
        allow_rate=False,
        clock="wall",
        evidence=(_native_evidence("turn.ended.durationMs", locator),),
        turn_id=turn_id,
        diagnostics=tuple(
            list(spec["issues"])
            + [
                "wall_duration_covers_queue_steps_tools_retries",
                "turn_duration_not_generation_speed",
            ]
        ),
    )


def build_records(
    step_specs: list[dict[str, Any]], turn_specs: list[dict[str, Any]], *, session_ref: str | None
) -> list[Any]:
    """Build source-order performance records from extracted specs."""
    records: list[Any] = []
    for spec in step_specs:
        records.extend(_records_for_step(spec, session_ref=session_ref))
    for spec in turn_specs:
        records.append(_record_for_turn(spec, session_ref=session_ref))
    records.sort(key=lambda record: int(record.record.split(":", 2)[1]))
    return records


def load(session: dict[str, Any], *, history_view: str = "all_branches") -> PerformanceSnapshot:
    """Build the Kimi performance snapshot for one session wire log."""
    validate_performance_view(history_view, CAPABILITIES)
    runtime = str(session.get("source") or RUNTIME)
    session_id = str(session.get("id") or "")
    session_ref = f"{runtime}:{session_id}" if session_id else None
    path = str(session.get("path") or "")
    from sesskit.native_jsonl import read_jsonl_snapshot

    try:
        feed = read_jsonl_snapshot(path)
        rows = feed.rows
        watermark = feed.watermark
    except OSError:
        return PerformanceSnapshot(
            state="unavailable",
            runtime=runtime,
            session_ref=session_ref,
            history_view=history_view,
            records=(),
            entries_read=0,
            watermark=None,
            observed_operations=0,
            coverage="unknown",
            diagnostics=("history_unreadable",),
        )
    step_specs = collect_step_specs(rows)
    turn_specs = collect_turn_specs(rows)
    records = build_records(step_specs, turn_specs, session_ref=session_ref)
    diagnostics: list[str] = list(feed.diagnostics)
    if not records:
        diagnostics.append("no_native_timing_records")
        return PerformanceSnapshot(
            state="empty",
            runtime=runtime,
            session_ref=session_ref,
            history_view=history_view,
            records=(),
            entries_read=feed.entries_read,
            watermark=watermark,
            observed_operations=len(step_specs) + len(turn_specs),
            coverage="not_recorded",
            diagnostics=tuple(diagnostics),
        )
    return PerformanceSnapshot(
        state="available",
        runtime=runtime,
        session_ref=session_ref,
        history_view=history_view,
        records=tuple(records),
        entries_read=feed.entries_read,
        watermark=watermark,
        observed_operations=len(step_specs) + len(turn_specs),
        coverage="partial",
        diagnostics=tuple(diagnostics),
    )
