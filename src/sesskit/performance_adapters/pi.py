"""Pi performance projection: per-assistant response-elapsed timing.

Native basis (pinned upstream commit
``6291b9bcc3ee9480c2caba8676fc2f6cd321eaa9``):
``AssistantMessageEventStream`` stamps ``message.durationMs`` (monotonic ms from
stream construction to the terminal done/error event); ``message.timestamp`` is
the documented request-start wall time; the entry ISO ``timestamp`` is append
time and is never used as a generation endpoint. ``usage.output`` is inclusive
(``reasoning`` is a subset). Only successful native outcomes (``stop``,
``toolUse``) authorize a rate numerator; failed/aborted counters are
placeholder/unknown. No first-token time or native rate is persisted.
"""

from __future__ import annotations

import math
from typing import Any

RUNTIME = "pi"
KIND_RESPONSE_ELAPSED = "response_elapsed"
OUTPUT_SEMANTICS_INCLUSIVE = "inclusive_output"
OUTPUT_SEMANTICS_UNKNOWN = "unknown"
CLOCK_MONOTONIC = "monotonic"
CLOCK_MIXED = "mixed"

_HISTORY_VIEWS = ("all_branches", "active_branch")

#: Native outcomes whose counters are final enough to authorize a rate.
_SUCCESSFUL_STOP_REASONS = frozenset({"stop", "toolUse"})

#: Natively recorded input fields cited as evidence, in stable order.
_EVIDENCE_FIELDS = ("durationMs", "timestamp", "usage")

try:
    from sesskit.performance import PerformanceCapabilities as _Capabilities

    CAPABILITIES = _Capabilities(
        runtime=RUNTIME,
        supported=True,
        timing_kinds=(KIND_RESPONSE_ELAPSED,),
        rate_kinds=(KIND_RESPONSE_ELAPSED,),
        history_views=_HISTORY_VIEWS,
        provider_decode_timing=False,
        incremental_support=False,
    )
except ImportError:
    CAPABILITIES = None


def _is_valid_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_duration_ms(value: Any) -> float | None:
    """Return a positive finite duration in ms, else None.

    Rejects booleans, NaN/Infinity, zero and negatives: an invalid duration
    authorizes no rate and keeps no zero-duration record.
    """
    if not _is_valid_number(value) or value <= 0:
        return None
    return float(value)


def valid_output_tokens(value: Any) -> int | None:
    """Return a non-negative int counter, else None (bools never count)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value >= 0:
        return int(value)
    return None


def valid_start_epoch(value: Any) -> float | None:
    """Map the native ms-epoch request-start proxy to epoch seconds."""
    if not _is_valid_number(value) or value < 0:
        return None
    return float(value) / 1000.0


def _record_locator(entry_id: Any, position: int) -> str:
    if isinstance(entry_id, str) and entry_id:
        return f"entry:{entry_id}"
    return f"entry:#{position}"


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def collect_pi_performance_inputs(
    rows: list[tuple[int, dict]], *, session_id: str | None = None
) -> tuple[list[dict], list[dict]]:
    """Map ``(physical position, entry)`` rows to core record inputs.

    Positions travel with their entries through branch filtering, so records
    keep original locators and are never renumbered. Returns
    ``(inputs, skipped)``: eligible timing inputs plus one entry per skipped
    assistant message (missing/invalid duration) carrying a stable reason.
    ``toolResult`` auxiliary timing is ignored: it has no compatible output
    numerator. The entry ISO ``timestamp`` (append time) is never read here.
    """
    del session_id  # identity is session-qualified by the core builder.
    inputs: list[dict] = []
    skipped: list[dict] = []
    for position, entry in rows:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        entry_id = entry.get("id")
        record = _record_locator(entry_id, position)
        duration_ms = valid_duration_ms(message.get("durationMs"))
        if duration_ms is None:
            skipped.append({"record": record, "reason": "duration-missing-or-invalid"})
            continue
        raw_stop = message.get("stopReason")
        stop_token = raw_stop.strip() if isinstance(raw_stop, str) and raw_stop.strip() else ""
        usage = message.get("usage")
        output = valid_output_tokens(usage.get("output")) if isinstance(usage, dict) else None
        trusted = stop_token in _SUCCESSFUL_STOP_REASONS and output is not None
        started_at = valid_start_epoch(message.get("timestamp"))
        diagnostics: list[str] = []
        if not trusted:
            if stop_token not in _SUCCESSFUL_STOP_REASONS:
                diagnostics.append(f"stop-not-successful:{stop_token or 'missing'}:output-untrusted:no-rate")
            else:
                diagnostics.append("output-absent-or-invalid:no-rate")
        response = message.get("responseId") or message.get("response_id")
        entry_id_text = entry_id if isinstance(entry_id, str) and entry_id else None
        inputs.append(
            {
                "record": record,
                "kind": KIND_RESPONSE_ELAPSED,
                "scope_kind": "message",
                "scope_id": entry_id_text,
                "duration_ms": duration_ms,
                "output_tokens": output if trusted else None,
                "output_semantics": (OUTPUT_SEMANTICS_INCLUSIVE if trusted else OUTPUT_SEMANTICS_UNKNOWN),
                "allow_rate": trusted,
                "clock": (CLOCK_MIXED if started_at is not None else CLOCK_MONOTONIC),
                "started_at": started_at,
                "ended_at": None,
                "message_id": entry_id_text,
                "response_id": _optional_text(response),
                "model": _optional_text(message.get("model")),
                "provider": _optional_text(message.get("provider")),
                "evidence_fields": _EVIDENCE_FIELDS,
                "diagnostics": tuple(diagnostics),
            }
        )
    return inputs, skipped


def select_rows(entries: list[dict], positions: list[int], *, history_view: str) -> list[tuple[int, dict]]:
    """Filter full-order entries to a branch view, keeping positions."""
    if history_view not in _HISTORY_VIEWS:
        raise ValueError(f"unsupported history view: {history_view!r}")
    if history_view == "active_branch":
        from sesskit.parsers import pi as scan_pi

        wanted = {id(item) for item in scan_pi.active_messages(entries)}
        return [(position, item) for position, item in zip(positions, entries) if id(item) in wanted]
    return list(zip(positions, entries))


def get_capabilities() -> Any:
    """Return native capabilities, or fail loudly when core is absent."""
    if CAPABILITIES is None:
        raise RuntimeError("sesskit.performance core is not installed (dependency_not_ready)")
    return CAPABILITIES


def load(session: dict, *, history_view: str = "all_branches") -> Any:
    """Load one Pi session's performance snapshot (lazy core binding)."""
    if history_view not in _HISTORY_VIEWS:
        raise ValueError(f"unsupported history view: {history_view!r}")
    try:
        from sesskit import performance as _performance
    except ImportError as exc:
        raise RuntimeError("sesskit.performance core is not installed (dependency_not_ready)") from exc
    from sesskit.models import Evidence as _Evidence

    runtime = str(session.get("source") or RUNTIME)
    raw_id = session.get("id")
    session_id = str(raw_id) if isinstance(raw_id, str) and raw_id else None
    session_ref = f"{runtime}:{session_id}" if session_id else None
    path = str(session.get("path") or "")

    try:
        rows, entries_read, watermark, native_diagnostics = _read_rows(path)
    except OSError:
        return _performance.PerformanceSnapshot(
            state="unavailable",
            runtime=runtime,
            session_ref=session_ref,
            history_view=history_view,
            records=(),
            entries_read=0,
            watermark=None,
            observed_operations=0,
            coverage="unknown",
            diagnostics=("history-unreadable",),
        )
    entries = [entry for _, entry in rows]
    positions = [position for position, _ in rows]
    selected = select_rows(entries, positions, history_view=history_view)
    examined = sum(
        1
        for _, entry in selected
        if isinstance(entry, dict)
        and entry.get("type") == "message"
        and isinstance(entry.get("message"), dict)
        and entry.get("message", {}).get("role") == "assistant"
    )
    inputs, skipped = collect_pi_performance_inputs(selected)
    records = []
    for item in inputs:
        evidence = tuple(_Evidence("native", field=field, record=item["record"]) for field in item["evidence_fields"])
        if item["allow_rate"]:
            evidence += (
                _Evidence(
                    "inferred",
                    field="tokens_per_second",
                    record=item["record"],
                ),
            )
        records.append(
            _performance.make_performance_record(
                runtime=runtime,
                session_ref=session_ref,
                record=item["record"],
                kind=item["kind"],
                scope_kind=item["scope_kind"],
                scope_id=item["scope_id"],
                duration_ms=item["duration_ms"],
                output_tokens=item["output_tokens"],
                output_semantics=item["output_semantics"],
                evidence=evidence,
                allow_rate=item["allow_rate"],
                clock=item["clock"],
                started_at=item["started_at"],
                ended_at=item["ended_at"],
                message_id=item["message_id"],
                response_id=item["response_id"],
                model=item["model"],
                provider=item["provider"],
                diagnostics=item["diagnostics"],
            )
        )
    reasons: dict[str, int] = {}
    for item in skipped:
        reason = str(item.get("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    snap_diagnostics = list(native_diagnostics)
    snap_diagnostics.append(f"assistants-examined:{examined}")
    snap_diagnostics.append(f"records:{len(records)}")
    for reason in sorted(reasons):
        snap_diagnostics.append(f"skipped-{reason}:{reasons[reason]}")
    if records:
        state = "available"
        coverage = "partial" if skipped else "unknown"
    else:
        state = "empty"
        coverage = "not_recorded"
    return _performance.PerformanceSnapshot(
        state=state,
        runtime=runtime,
        session_ref=session_ref,
        history_view=history_view,
        records=tuple(records),
        entries_read=entries_read,
        watermark=watermark,
        observed_operations=examined,
        coverage=coverage,
        diagnostics=tuple(snap_diagnostics),
    )


def _read_rows(
    path: str,
) -> tuple[list[tuple[int, dict]], int, str | None, tuple[str, ...]]:
    """Read frozen ``(position, entry)`` rows, preferring the shared reader."""
    try:
        from sesskit import performance_io as _performance_io
    except ImportError:
        from sesskit.parsers import pi as scan_pi

        entries = scan_pi.read_entries(path)
        rows = [(position, entry) for position, entry in enumerate(entries, 1)]
        return rows, len(entries), None, ()
    native = _performance_io.read_jsonl_snapshot(path)
    return (
        list(native.rows),
        native.entries_read,
        native.watermark,
        tuple(native.diagnostics),
    )
