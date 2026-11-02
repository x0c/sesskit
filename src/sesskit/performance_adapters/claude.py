"""Claude Code performance adapter: timing evidence over native JSONL rows.

Preserves the joined request-to-reply interval (``api-request.id`` equal to
``assistant.requestRef``), thinking-block durations, turn durations and the
cumulative ``cost-state`` durations as named native evidence. No rate is
derived: dispatch/flush/finality semantics are unverified, so every record
keeps ``tokens_per_second=None`` with ``rate_basis="unavailable"``.

Only schema/field names, numeric magnitudes and line locators are retained;
message content, payloads and native paths are never exported.
"""

from __future__ import annotations

from datetime import datetime, timezone
from math import isinf, isnan
from typing import Any

from sesskit.models import Evidence
from sesskit.performance import (
    PerformanceCapabilities,
    PerformanceSnapshot,
    make_performance_record,
)

RUNTIME = "claude"

CAPABILITIES = PerformanceCapabilities(
    runtime=RUNTIME,
    supported=True,
    timing_kinds=(
        "request_to_reply_elapsed",
        "thinking_elapsed",
        "turn_elapsed",
        "session_elapsed",
        "session_api_elapsed",
        "session_api_without_retries_elapsed",
        "session_tool_elapsed",
    ),
    rate_kinds=(),
    history_views=("all_branches",),
    provider_decode_timing=False,
    incremental_support=False,
)

_REQUEST_JOIN_DIAGNOSTIC = (
    "recorded_endpoints=request_dispatch_to_last_reply_flush;rate_not_derived_unverified_endpoints"
)


def _parse_epoch(value: Any) -> float | None:
    """Parse an ISO-8601 wall timestamp to UTC epoch seconds, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        epoch = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    if epoch is None or isnan(epoch):
        return None
    return epoch


def _valid_duration_ms(value: Any) -> float | None:
    """Return a finite non-negative duration in ms, else None."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    duration = float(value)
    if isnan(duration) or isinf(duration):
        return None
    if duration < 0:
        return None
    return duration


def _native_evidence(field: str, lineno: int) -> Evidence:
    return Evidence(origin="native", field=field, record=f"line:{lineno}")


def collect_claude_performance(
    rows: list[tuple[int, dict]],
    *,
    session_ref: str | None = None,
    session_id: str | None = None,
) -> tuple[list[Any], int, tuple[str, ...]]:
    """Collect timing records from ``(lineno, row)`` pairs in source order.

    Returns ``(records, observed_operations, diagnostics)``. ``records`` are
    core :class:`PerformanceRecord` objects; ``observed_operations`` counts
    distinct request ids observed on either side of the join; diagnostics
    are stable snapshot-level strings (counts only, no ids or content).
    """
    diagnostics: list[str] = []
    records: list[Any] = []

    request_start: dict[str, float] = {}
    request_line: dict[str, int] = {}
    repeated_request_ids = 0
    invalid_request_stamps = 0
    # request id -> {"ends": [epoch...], "last_lineno": int,
    #                "message_id": str|None, "response_id": str|None, "model": str|None}
    linked: dict[str, dict[str, Any]] = {}
    unlinked_replies = 0
    missing_request_rows = 0

    thinking_count = 0
    turn_count = 0
    cost_state_rows = 0
    latest_cost_state: tuple[int, dict] | None = None

    for lineno, entry in rows:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "api-request":
            request_id = entry.get("id")
            if not isinstance(request_id, str) or not request_id:
                continue
            if request_id in request_start:
                repeated_request_ids += 1
                continue
            epoch = _parse_epoch(entry.get("timestamp"))
            if epoch is None:
                invalid_request_stamps += 1
                continue
            request_start[request_id] = epoch
            request_line[request_id] = lineno
        elif kind == "assistant":
            request_ref = entry.get("requestRef")
            if not isinstance(request_ref, str) or not request_ref:
                unlinked_replies += 1
                continue
            reply = linked.setdefault(
                request_ref,
                {
                    "ends": [],
                    "last_lineno": lineno,
                    "message_id": None,
                    "response_id": None,
                    "model": None,
                },
            )
            reply["last_lineno"] = lineno
            epoch = _parse_epoch(entry.get("timestamp"))
            if epoch is not None:
                reply["ends"].append(epoch)
            message = entry.get("message")
            if isinstance(message, dict):
                message_id = message.get("id")
                if isinstance(message_id, str) and message_id:
                    reply["message_id"] = message_id
                model = message.get("model")
                if isinstance(model, str) and model:
                    reply["model"] = model
            response_id = entry.get("requestId")
            if isinstance(response_id, str) and response_id:
                reply["response_id"] = response_id
            thinking = entry.get("thinkingDurationMs")
            if thinking is not None:
                duration = _valid_duration_ms(thinking)
                if duration is None:
                    records.append(
                        make_performance_record(
                            runtime=RUNTIME,
                            session_ref=session_ref,
                            record=f"line:{lineno}",
                            kind="thinking_elapsed",
                            scope_kind="message",
                            scope_id=reply["message_id"],
                            duration_ms=None,
                            evidence=(_native_evidence("assistant.thinkingDurationMs", lineno),),
                            allow_rate=False,
                            clock="unknown",
                            message_id=reply["message_id"],
                            response_id=reply["response_id"],
                            model=reply["model"],
                            diagnostics=("invalid_thinking_duration",),
                        )
                    )
                else:
                    thinking_count += 1
                    records.append(
                        make_performance_record(
                            runtime=RUNTIME,
                            session_ref=session_ref,
                            record=f"line:{lineno}",
                            kind="thinking_elapsed",
                            scope_kind="message",
                            scope_id=reply["message_id"],
                            duration_ms=duration,
                            evidence=(_native_evidence("assistant.thinkingDurationMs", lineno),),
                            allow_rate=False,
                            clock="unknown",
                            message_id=reply["message_id"],
                            response_id=reply["response_id"],
                            model=reply["model"],
                            diagnostics=("thinking_span_unverified",),
                        )
                    )
        elif kind == "system" and entry.get("subtype") == "turn_duration":
            duration = _valid_duration_ms(entry.get("durationMs"))
            message_count = entry.get("messageCount")
            extra: tuple[str, ...] = ("turn_span_unverified_inclusions_unknown",)
            if isinstance(message_count, bool) or not isinstance(message_count, (int, float)):
                extra = extra + ("turn_message_count_absent",)
            turn_count += 1
            records.append(
                make_performance_record(
                    runtime=RUNTIME,
                    session_ref=session_ref,
                    record=f"line:{lineno}",
                    kind="turn_elapsed",
                    scope_kind="turn",
                    scope_id=None,
                    duration_ms=duration,
                    evidence=(_native_evidence("system.turn_duration.durationMs", lineno),),
                    allow_rate=False,
                    clock="unknown",
                    diagnostics=extra if duration is not None else extra + ("invalid_turn_duration",),
                )
            )
        elif kind == "cost-state":
            cost_state_rows += 1
            latest_cost_state = (lineno, entry)

    if latest_cost_state is not None:
        lineno, entry = latest_cost_state
        if cost_state_rows > 1:
            diagnostics.append(f"cost_state_revisions_superseded={cost_state_rows - 1}")
        started_at = None
        start_time = entry.get("startTime")
        if isinstance(start_time, bool):
            diagnostics.append("cost_state_start_time_invalid")
        elif isinstance(start_time, (int, float)):
            started_at = float(start_time) / 1000.0
            if isnan(started_at) or started_at < 0:
                started_at = None
                diagnostics.append("cost_state_start_time_invalid")
        else:
            diagnostics.append("cost_state_start_time_absent")
        fields = (
            ("totalDuration", "session_elapsed"),
            ("totalAPIDuration", "session_api_elapsed"),
            ("totalAPIDurationWithoutRetries", "session_api_without_retries_elapsed"),
            ("totalToolDuration", "session_tool_elapsed"),
        )
        for field_name, record_kind in fields:
            duration = _valid_duration_ms(entry.get(field_name))
            record_diagnostics = (
                "cost_state_scope_unknown_no_sdk_transfer",
                "session_start_endpoint_unverified",
            )
            if duration is None:
                record_diagnostics = record_diagnostics + (f"invalid_cost_state_duration:{field_name}",)
            records.append(
                make_performance_record(
                    runtime=RUNTIME,
                    session_ref=session_ref,
                    record=f"line:{lineno}",
                    kind=record_kind,
                    scope_kind="session",
                    scope_id=session_id,
                    duration_ms=duration,
                    evidence=(_native_evidence(f"cost-state.{field_name}", lineno),),
                    allow_rate=False,
                    clock="unknown",
                    started_at=started_at,
                    diagnostics=record_diagnostics,
                )
            )

    for request_id, reply in linked.items():
        start = request_start.get(request_id)
        if start is None:
            missing_request_rows += 1
            records.append(
                make_performance_record(
                    runtime=RUNTIME,
                    session_ref=session_ref,
                    record=f"line:{reply['last_lineno']}",
                    kind="request_to_reply_elapsed",
                    scope_kind="call",
                    scope_id=request_id,
                    duration_ms=None,
                    evidence=(_native_evidence("assistant.timestamp", reply["last_lineno"]),),
                    allow_rate=False,
                    clock="wall",
                    message_id=reply["message_id"],
                    response_id=reply["response_id"],
                    model=reply["model"],
                    diagnostics=("request_record_missing",),
                )
            )
            continue
        ends = [epoch for epoch in reply["ends"]]
        if not ends:
            records.append(
                make_performance_record(
                    runtime=RUNTIME,
                    session_ref=session_ref,
                    record=f"line:{request_line[request_id]}",
                    kind="request_to_reply_elapsed",
                    scope_kind="call",
                    scope_id=request_id,
                    duration_ms=None,
                    evidence=(_native_evidence("api-request.timestamp", request_line[request_id]),),
                    allow_rate=False,
                    clock="wall",
                    started_at=start,
                    message_id=reply["message_id"],
                    response_id=reply["response_id"],
                    model=reply["model"],
                    diagnostics=("reply_timestamps_unparseable",),
                )
            )
            continue
        end = max(ends)
        if end < start:
            records.append(
                make_performance_record(
                    runtime=RUNTIME,
                    session_ref=session_ref,
                    record=f"line:{reply['last_lineno']}",
                    kind="request_to_reply_elapsed",
                    scope_kind="call",
                    scope_id=request_id,
                    duration_ms=None,
                    evidence=(
                        _native_evidence("api-request.timestamp", request_line[request_id]),
                        _native_evidence("assistant.timestamp", reply["last_lineno"]),
                    ),
                    allow_rate=False,
                    clock="wall",
                    started_at=start,
                    ended_at=end,
                    message_id=reply["message_id"],
                    response_id=reply["response_id"],
                    model=reply["model"],
                    diagnostics=("clock_reversal_end_before_dispatch",),
                )
            )
            continue
        records.append(
            make_performance_record(
                runtime=RUNTIME,
                session_ref=session_ref,
                record=f"line:{reply['last_lineno']}",
                kind="request_to_reply_elapsed",
                scope_kind="call",
                scope_id=request_id,
                duration_ms=(end - start) * 1000.0,
                evidence=(
                    _native_evidence("api-request.timestamp", request_line[request_id]),
                    _native_evidence("assistant.timestamp", reply["last_lineno"]),
                ),
                allow_rate=False,
                clock="wall",
                started_at=start,
                ended_at=end,
                message_id=reply["message_id"],
                response_id=reply["response_id"],
                model=reply["model"],
                diagnostics=(_REQUEST_JOIN_DIAGNOSTIC,),
            )
        )

    if repeated_request_ids:
        diagnostics.append(f"repeated_api_request_ids_skipped={repeated_request_ids}")
    if invalid_request_stamps:
        diagnostics.append(f"invalid_api_request_timestamps={invalid_request_stamps}")
    if unlinked_replies:
        diagnostics.append(f"assistant_replies_without_request_link={unlinked_replies}")
    if missing_request_rows:
        diagnostics.append(f"linked_replies_without_request_record={missing_request_rows}")

    observed_operations = len(set(request_start) | set(linked))
    return records, observed_operations, tuple(diagnostics)


def load(session: dict, *, history_view: str = "all_branches") -> PerformanceSnapshot:
    """Load Claude timing evidence for a scanned session dict (read-only)."""
    from sesskit.native_jsonl import read_jsonl_snapshot

    if history_view not in CAPABILITIES.history_views:
        raise ValueError(f"unsupported history view for runtime {RUNTIME!r}: {history_view!r}")
    runtime = str(session.get("source") or RUNTIME)
    session_id = str(session.get("id") or "")
    session_ref = f"{runtime}:{session_id}" if session_id else None
    path = str(session.get("path") or "")
    try:
        feed = read_jsonl_snapshot(path)
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
    records, observed_operations, extra = collect_claude_performance(
        feed.rows, session_ref=session_ref, session_id=session_id or None
    )
    if records:
        state: Any = "available"
        coverage = "partial"
    else:
        state = "empty"
        coverage = "not_recorded"
    return PerformanceSnapshot(
        state=state,
        runtime=runtime,
        session_ref=session_ref,
        history_view=history_view,
        records=tuple(records),
        entries_read=feed.entries_read,
        watermark=feed.watermark,
        observed_operations=observed_operations,
        coverage=coverage,
        diagnostics=feed.diagnostics + tuple(extra),
    )
