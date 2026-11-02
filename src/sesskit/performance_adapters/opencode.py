"""OpenCode historical performance projection (read-only SQLite timing).

Native timing endpoints (verified at writer pin
``69269e4ebe309a08919b3c9be7948b9fd699c3a4`` and in the local store):
v1 ``message.data.time.{created,completed}``, v2
``session_message.data.time.{created,streamed?,completed?}`` where
``created`` is request dispatch, ``streamed`` is provider-body end before
tool settlement, and ``completed`` is step end after tool join plus
snapshot capture. The v2 row discriminator is the dedicated ``type``
COLUMN (``data`` JSON carries no ``type``/``role`` keys); v1 roles live
inside ``data`` JSON as ``$.role``.

Rate policy: ``tokens_per_second`` derives only on proven same-attempt
terminal rows -- ``finish`` in (``stop``, ``tool-calls``), no ``error``,
no ``retry``, valid output count plus compatible positive timestamps.
``Step.Started`` resets timing but not tokens/cost and ``Step.Failed``
rewrites usage only when both tokens and cost exist, so failed, retried
or in-flight rows may pair earlier-attempt usage against newer timing and
MUST NOT yield rates; their available native timing is still preserved
with diagnostics. No first-output timestamp is persisted anywhere, so no
first-output latency is emitted and stream end is never labeled first
token. Numerator uses visible ``tokens.output`` only
(``output_semantics="visible_output"``); ``tokens.reasoning`` is a
separate additive bucket and is never added in.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from typing import Any

from sesskit.models import Evidence
from sesskit.parsers import opencode as _parser
from sesskit.performance import (
    PerformanceCapabilities,
    PerformanceSnapshot,
    make_performance_record,
    validate_performance_view,
)

RUNTIME = "opencode"

CAPABILITIES = PerformanceCapabilities(
    runtime=RUNTIME,
    supported=True,
    timing_kinds=("provider_phase_elapsed", "step_elapsed"),
    rate_kinds=("provider_phase_elapsed", "step_elapsed"),
    history_views=("all_branches",),
    provider_decode_timing=False,
    incremental_support=False,
)

_TERMINAL_FINISH = ("stop", "tool-calls")

_LAYOUT_V1 = "layout_v1_message"
_LAYOUT_V2 = "layout_v2_session_message"


def _safe_int(value: object) -> int | None:
    """Non-negative native integer (same rule as usage ``_opencode_int``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _safe_ms(value: object) -> float | None:
    """Native millisecond epoch: finite non-negative number, never bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _native_evidence(record: str, field: str) -> Evidence:
    return Evidence("native", field=field, record=record)


def _derived_evidence(record: str) -> Evidence:
    return Evidence("inferred", field="tokens_per_second", record=record)


def parse_assistant_timing(data: object) -> dict[str, Any] | None:
    """Project one assistant ``data`` dict to timing/token endpoints.

    Returns wall-clock millisecond endpoints, token buckets, terminal
    pairing flags, model identity and response linkage, or None when
    ``data`` is not a dict. Raw payload text is never retained.
    """
    if not isinstance(data, dict):
        return None
    time_obj = data.get("time")
    time_map = time_obj if isinstance(time_obj, dict) else {}
    tokens = data.get("tokens")
    token_map = tokens if isinstance(tokens, dict) else {}
    cache = token_map.get("cache")
    cache_map = cache if isinstance(cache, dict) else {}
    model: str | None = None
    model_raw = data.get("model")
    if isinstance(model_raw, dict):
        candidate = model_raw.get("id")
        if isinstance(candidate, str) and candidate.strip():
            model = candidate.strip()
    else:
        candidate = data.get("modelID")
        if isinstance(candidate, str) and candidate.strip():
            model = candidate.strip()
    response_id: str | None = None
    native = data.get("native")
    if isinstance(native, dict):
        candidate = native.get("responseId")
        if isinstance(candidate, str) and candidate.strip():
            response_id = candidate.strip()
    error = data.get("error")
    retry = data.get("retry")
    return {
        "created_ms": _safe_ms(time_map.get("created")),
        "streamed_ms": _safe_ms(time_map.get("streamed")),
        "completed_ms": _safe_ms(time_map.get("completed")),
        "output": _safe_int(token_map.get("output")),
        "reasoning": _safe_int(token_map.get("reasoning")),
        "cache_read": _safe_int(cache_map.get("read")),
        "cache_write": _safe_int(cache_map.get("write")),
        "input": _safe_int(token_map.get("input")),
        "finish": data.get("finish"),
        "has_error": bool(error),
        "has_retry": retry is not None,
        "model": model,
        "response_id": response_id,
    }


def _pair_diagnostic(parsed: dict[str, Any], *, end_ms: float | None) -> str | None:
    """Return the no-rate cause for a row, or None when the row is pairable."""
    if parsed["has_error"]:
        return "error_present_no_rate"
    if parsed["has_retry"]:
        return "retry_present_no_rate"
    finish = parsed["finish"]
    if finish not in _TERMINAL_FINISH:
        return "finish_not_terminal_no_rate"
    if parsed["output"] is None:
        return "missing_output_tokens_no_rate"
    if parsed["created_ms"] is None or end_ms is None:
        return "missing_timing_no_rate"
    created = parsed["created_ms"]
    assert isinstance(created, float)
    assert isinstance(end_ms, float)
    if not end_ms > created:
        return "nonpositive_duration_no_rate"
    return None


def intervals_for_message(
    *,
    session_ref: str | None,
    locator: str,
    message_id: str | None,
    parsed: dict[str, Any],
    version: str,
) -> list:
    """Build provider-phase/step records for one parsed assistant row.

    Endpoints present but unpaired (errors, retries, in-flight rows,
    non-terminal finish, bad durations) still yield timing records with
    ``allow_rate=False`` plus a stable diagnostic; only proven
    same-attempt terminal rows derive ``tokens_per_second``.
    """
    records = []
    created = parsed["created_ms"]
    output = parsed["output"]
    endpoints = (
        ("provider_phase_elapsed", parsed["streamed_ms"], "time.streamed"),
        ("step_elapsed", parsed["completed_ms"], "time.completed"),
    )
    for kind, end_ms, end_field in endpoints:
        if kind == "provider_phase_elapsed" and version == "v1":
            continue  # v1 never persisted ``streamed``; do not overclaim.
        if created is None and end_ms is None:
            continue  # no native endpoint at all: nothing to preserve.
        if created is None or end_ms is None:
            records.append(make_performance_record(
                runtime=RUNTIME,
                session_ref=session_ref,
                record=locator,
                kind=kind,
                scope_kind="message",
                scope_id=message_id,
                duration_ms=None,
                output_tokens=output,
                output_semantics="visible_output",
                evidence=(
                    _native_evidence(locator, "time.created"),
                    _native_evidence(locator, "tokens.output"),
                ),
                allow_rate=False,
                clock="wall",
                started_at=created / 1000.0 if created is not None else None,
                ended_at=end_ms / 1000.0 if end_ms is not None else None,
                message_id=message_id,
                response_id=parsed["response_id"],
                model=parsed["model"],
                diagnostics=("missing_timing_no_rate",),
            ))
            continue
        duration_ms = end_ms - created
        if not math.isfinite(duration_ms) or duration_ms < 0:
            records.append(make_performance_record(
                runtime=RUNTIME,
                session_ref=session_ref,
                record=locator,
                kind=kind,
                scope_kind="message",
                scope_id=message_id,
                duration_ms=None,
                output_tokens=output,
                output_semantics="visible_output",
                evidence=(
                    _native_evidence(locator, "time.created"),
                    _native_evidence(locator, end_field),
                    _native_evidence(locator, "tokens.output"),
                ),
                allow_rate=False,
                clock="wall",
                started_at=created / 1000.0,
                ended_at=end_ms / 1000.0,
                message_id=message_id,
                response_id=parsed["response_id"],
                model=parsed["model"],
                diagnostics=("invalid_duration_no_rate",),
            ))
            continue
        cause = _pair_diagnostic(parsed, end_ms=end_ms)
        allow_rate = cause is None
        evidence = [
            _native_evidence(locator, "time.created"),
            _native_evidence(locator, end_field),
            _native_evidence(locator, "tokens.output"),
        ]
        if allow_rate:
            evidence.append(_derived_evidence(locator))
        records.append(make_performance_record(
            runtime=RUNTIME,
            session_ref=session_ref,
            record=locator,
            kind=kind,
            scope_kind="message",
            scope_id=message_id,
            duration_ms=duration_ms,
            output_tokens=output,
            output_semantics="visible_output",
            evidence=tuple(evidence),
            allow_rate=allow_rate,
            clock="wall",
            started_at=created / 1000.0,
            ended_at=end_ms / 1000.0,
            message_id=message_id,
            response_id=parsed["response_id"],
            model=parsed["model"],
            diagnostics=() if allow_rate else (cause,),
        ))
    return records


def _read_session_messages(
    conn: sqlite3.Connection, session_id: str
) -> tuple[str, list[tuple[str, str | None, Any]], int, str | None]:
    """Read one session's message rows on a single open connection.

    Returns ``(version, rows, entries_read, error_diagnostic)`` where each
    row is ``(message_id, row_type, raw_data)``; ``row_type`` is the v2
    ``type`` COLUMN (never a JSON lookup) and None for v1. The version
    follows the parser/usage table precedence: v1 when the id sits in the
    ``session`` table, otherwise v2.
    """
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error:
        return "unknown", [], 0, "history_unreadable"
    if {"session", "message"} <= tables:
        try:
            hit = conn.execute(
                "SELECT 1 FROM session WHERE id = ? LIMIT 1", (session_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            if "no such column" in str(exc).lower():
                return "unknown", [], 0, "unsupported_layout_column_missing"
            return "unknown", [], 0, "history_unreadable"
        if hit is not None:
            try:
                rows = conn.execute(
                    "SELECT id, data FROM message WHERE session_id = ?"
                    " ORDER BY time_created ASC, id ASC",
                    (session_id,),
                ).fetchall()
            except sqlite3.Error as exc:
                if "no such column" in str(exc).lower():
                    return "unknown", [], 0, "unsupported_layout_column_missing"
                return "unknown", [], 0, "history_unreadable"
            return "v1", [(str(r[0]), None, r[1]) for r in rows], len(rows), None
    if "session_message" in tables:
        if "session_v2" in tables:
            try:
                hit = conn.execute(
                    "SELECT 1 FROM session_v2 WHERE id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                if "no such column" in str(exc).lower():
                    return "unknown", [], 0, "unsupported_layout_column_missing"
                return "unknown", [], 0, "history_unreadable"
            if hit is None:
                try:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM session_message WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                except sqlite3.Error:
                    return "unknown", [], 0, "history_unreadable"
                if not count or not count[0]:
                    return "unknown", [], 0, "session_not_found"
        try:
            rows = conn.execute(
                "SELECT id, type, data FROM session_message WHERE session_id = ?"
                " ORDER BY seq ASC, time_created ASC, id ASC",
                (session_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            if "no such column" in str(exc).lower():
                return "unknown", [], 0, "unsupported_layout_column_missing"
            return "unknown", [], 0, "history_unreadable"
        typed = [
            (str(r[0]), str(r[1]) if r[1] is not None else None, r[2]) for r in rows
        ]
        return "v2", typed, len(typed), None
    return "unknown", [], 0, "unsupported_layout_missing_tables"


def _unavailable(
    *, session_ref: str | None, history_view: str, diagnostic: str
) -> PerformanceSnapshot:
    return PerformanceSnapshot(
        state="unavailable",
        runtime=RUNTIME,
        session_ref=session_ref,
        history_view=history_view,
        records=(),
        entries_read=0,
        watermark=None,
        observed_operations=0,
        coverage="unknown",
        diagnostics=(diagnostic,),
    )


def load(session: dict, *, history_view: str = "all_branches") -> PerformanceSnapshot:
    """Project one scanned session dict to a read-only performance snapshot."""
    validate_performance_view(history_view, CAPABILITIES)
    session_id = str(session.get("id") or "")
    session_ref = f"{RUNTIME}:{session_id}" if session_id else None
    if not session_id:
        return _unavailable(
            session_ref=None, history_view=history_view,
            diagnostic="session_id_missing",
        )
    path = str(session.get("path") or "")
    if not path:
        return _unavailable(
            session_ref=session_ref, history_view=history_view,
            diagnostic="session_path_missing",
        )
    if not os.path.isfile(path):
        return _unavailable(
            session_ref=session_ref, history_view=history_view,
            diagnostic="history_unreadable",
        )
    conn = _parser.connect_ro(path)
    if conn is None:
        return _unavailable(
            session_ref=session_ref, history_view=history_view,
            diagnostic="history_unreadable",
        )
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        version, rows, entries_read, error = _read_session_messages(conn, session_id)
        if error is not None:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:
                pass
            return _unavailable(
                session_ref=session_ref, history_view=history_view,
                diagnostic=error,
            )
        try:
            stat = os.stat(path)
            # Bind paging to the transaction rows, including uncheckpointed WAL data.
            digest = hashlib.sha256(f"{stat.st_dev}:{stat.st_ino}:{session_id}:{version}".encode())
            for row in rows:
                digest.update(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode())
                digest.update(b"\n")
            watermark = digest.hexdigest()
        except OSError:
            watermark = None
        if watermark is None:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:
                pass
            return _unavailable(
                session_ref=session_ref, history_view=history_view,
                diagnostic="history_unreadable",
            )
        records: list = []
        diagnostics: list[str] = []
        diagnostics.append(
            _LAYOUT_V1 if version == "v1" else _LAYOUT_V2
        )
        assistant_rows = 0
        skipped_rows = 0
        malformed_rows = 0
        unpaired: dict[str, int] = {}
        missing_streamed = 0
        reasoning_seen = False
        for message_id, row_type, raw in rows:
            if version == "v2" and row_type != "assistant":
                skipped_rows += 1
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                malformed_rows += 1
                continue
            if not isinstance(data, dict):
                malformed_rows += 1
                continue
            if version == "v1" and data.get("role") != "assistant":
                skipped_rows += 1
                continue
            assistant_rows += 1
            parsed = parse_assistant_timing(data)
            if parsed is None:
                malformed_rows += 1
                assistant_rows -= 1
                continue
            if parsed["reasoning"] is not None:
                reasoning_seen = True
            if (
                version == "v2"
                and parsed["streamed_ms"] is None
                and parsed["created_ms"] is not None
                and parsed["completed_ms"] is not None
            ):
                missing_streamed += 1
            locator = (
                f"message:{message_id}" if message_id else "message:unknown"
            )
            built = intervals_for_message(
                session_ref=session_ref,
                locator=locator,
                message_id=message_id or None,
                parsed=parsed,
                version=version,
            )
            for record in built:
                for cause in record.diagnostics:
                    unpaired[cause] = unpaired.get(cause, 0) + 1
            records.extend(built)
        conn.execute("COMMIT")
    except sqlite3.Error:
        return _unavailable(session_ref=session_ref, history_view=history_view,
                            diagnostic="history_unreadable")
    finally:
        conn.close()
    diagnostics.append(f"assistant_rows:{assistant_rows}")
    if skipped_rows:
        diagnostics.append(f"non_assistant_rows_skipped:{skipped_rows}")
    if malformed_rows:
        diagnostics.append(f"malformed_rows_skipped:{malformed_rows}")
    if missing_streamed:
        diagnostics.append(f"rows_missing_streamed:{missing_streamed}")
    for cause in sorted(unpaired):
        diagnostics.append(f"{cause}:{unpaired[cause]}")
    if reasoning_seen:
        diagnostics.append("visible_output_excludes_reasoning")
    if not records:
        if assistant_rows:
            diagnostics.append("assistant_rows_without_timing")
        else:
            diagnostics.append("no_assistant_rows")
        return PerformanceSnapshot(
            state="empty",
            runtime=RUNTIME,
            session_ref=session_ref,
            history_view=history_view,
            records=(),
            entries_read=entries_read,
            watermark=watermark,
            observed_operations=assistant_rows,
            coverage="not_recorded",
            diagnostics=tuple(diagnostics),
        )
    return PerformanceSnapshot(
        state="available",
        runtime=RUNTIME,
        session_ref=session_ref,
        history_view=history_view,
        records=tuple(records),
        entries_read=entries_read,
        watermark=watermark,
        observed_operations=assistant_rows,
        coverage="partial",
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "CAPABILITIES",
    "RUNTIME",
    "intervals_for_message",
    "load",
    "parse_assistant_timing",
]
