"""Codex CLI historical performance projection.

Turn wall timing only: the latest within-turn cumulative output counter paired
with the matched native ``task_complete`` duration, plus the native
turn-scoped first-output latency. No per-call decode rate is derivable from
rollout history (deltas and ``RawResponseCompleted`` are transient, item
windows bound local processing, ``TurnProfile`` is telemetry-only).

Pure collector helpers are fixture-testable without the shared performance
core; :func:`get_capabilities` and :func:`load` bind the core lazily and
raise a clear error until the core worker lands it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

RUNTIME = "codex"
TURN_ELAPSED = "turn_elapsed"
FIRST_OUTPUT_LATENCY = "first_output_latency"
TIMING_KINDS = (TURN_ELAPSED, FIRST_OUTPUT_LATENCY)
RATE_KINDS = (TURN_ELAPSED,)
HISTORY_VIEWS = ("all_branches",)
CORE_MISSING_DIAGNOSTIC = "performance_core_not_landed"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_ms(value: Any) -> float | None:
    """Finite nonnegative millisecond clock reading, else None."""
    if not _is_number(value):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _valid_epoch_seconds(value: Any) -> float | None:
    """Known UTC epoch-second wall endpoint, else None."""
    if not _is_number(value):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _strict_output_count(value: Any) -> int | None:
    """Trustworthy native output counter: integer >= 0, never bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _turn_id_of(payload: dict) -> str | None:
    for key in ("turn_id", "turnId", "turnID"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _record_locator(lineno: int) -> str:
    return f"line:{lineno}"


def collect_codex_turn_performance(
    rows: list[tuple[int, dict]],
) -> list[dict]:
    """Stage Codex turn performance observations from parsed rollout rows.

    Pure function over ``(lineno, entry)`` pairs; no core imports, no I/O.
    Each staged dict carries ``kind``, ``record`` locator, ``turn_id``,
    ``duration_ms``, ``output_tokens``, ``allow_rate`` and ``diagnostics``.
    The shared core builder (when landed) makes the final rate decision;
    this collector never subtracts across turns and never sums
    request/counter/legacy duplicates: the numerator is always the latest
    ``turn_token_usage`` output observed WITHIN the same turn.
    """
    turn_output: dict[str, int] = {}
    turn_responses: dict[str, list[str]] = {}
    turn_model: dict[str, str] = {}
    terminals: list[tuple[int, str, dict]] = []

    for lineno, entry in rows:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if entry_type == "token_usage_record":
            turn_id = _turn_id_of(payload)
            response_id = payload.get("response_id")
            if isinstance(response_id, str) and response_id.strip() and turn_id:
                turn_responses.setdefault(turn_id, []).append(response_id.strip())
            block = payload.get("turn_token_usage")
            if isinstance(block, dict) and turn_id:
                # Latest revision within the turn wins; a corrected rewrite
                # later in the file supersedes earlier cumulative snapshots.
                count = _strict_output_count(block.get("output_tokens"))
                if count is not None:
                    turn_output[turn_id] = count
            model = payload.get("model")
            if isinstance(model, str) and model.strip() and turn_id:
                turn_model[turn_id] = model.strip()
        elif entry_type == "event_msg":
            kind = payload.get("type")
            if kind in ("task_complete", "turn_aborted"):
                terminals.append((lineno, str(kind), payload))
            else:
                # Native model attribution only when a row carries both a
                # matching turn id and a model name (e.g. turn_context).
                # Session-level rows without a turn id never fill a turn.
                turn_id = _turn_id_of(payload)
                model = payload.get("model")
                if turn_id and isinstance(model, str) and model.strip():
                    turn_model.setdefault(turn_id, model.strip())
        elif entry_type == "turn_context":
            turn_id = _turn_id_of(payload)
            model = payload.get("model")
            if turn_id and isinstance(model, str) and model.strip():
                turn_model.setdefault(turn_id, model.strip())

    staged: list[dict] = []
    for lineno, kind, payload in terminals:
        record = _record_locator(lineno)
        turn_id = _turn_id_of(payload)
        aborted = kind == "turn_aborted"
        has_error = bool(payload.get("error"))
        duration = _valid_ms(payload.get("duration_ms"))
        started_at = _valid_epoch_seconds(payload.get("started_at"))
        ended_at = _valid_epoch_seconds(payload.get("completed_at"))
        model = turn_model.get(turn_id) if turn_id else None
        diagnostics: list[str] = []

        raw_duration = payload.get("duration_ms")
        if duration is None and raw_duration is not None:
            diagnostics.append(f"unusable_duration_ms:{record}")
        if turn_id is None:
            diagnostics.append(f"missing_turn_id:{record}")

        output: int | None = None
        if turn_id is not None and turn_id in turn_output:
            output = turn_output[turn_id]
        elif not aborted:
            diagnostics.append(f"missing_turn_output:{record}" if turn_id else f"unlinked_turn_output:{record}")

        # Turn wall interval. A rate is authorized only for a successful
        # native completion with a compatible positive duration and a
        # trustworthy same-turn counter; aborts, error-bearing completions,
        # and placeholder/partial usage never become measured speed.
        allow_rate = not aborted and not has_error and duration is not None and duration > 0 and output is not None
        if aborted:
            diagnostics.append(f"turn_aborted_no_rate:{record}")
        elif has_error:
            diagnostics.append(f"error_bearing_no_rate:{record}")
        elif duration is not None and duration <= 0:
            diagnostics.append(f"nonpositive_duration_no_rate:{record}")

        staged.append(
            {
                "kind": TURN_ELAPSED,
                "record": record,
                "turn_id": turn_id,
                "duration_ms": duration,
                "output_tokens": output,
                "allow_rate": allow_rate,
                "has_error": has_error,
                "aborted": aborted,
                "model": model,
                "started_at": started_at,
                "ended_at": ended_at,
                "response_ids": tuple(turn_responses.get(turn_id, ())) if turn_id else (),
                "diagnostics": tuple(diagnostics),
            }
        )

        # Native turn-scoped first-output latency (first model output of any
        # kind, tool-call items included). Preserved on error-bearing
        # completions; aborts carry no TTFT natively. Never a rate input and
        # never differenced against the duration.
        raw_ttft = payload.get("time_to_first_token_ms")
        ttft = _valid_ms(raw_ttft)
        if ttft is not None:
            staged.append(
                {
                    "kind": FIRST_OUTPUT_LATENCY,
                    "record": record,
                    "turn_id": turn_id,
                    "duration_ms": ttft,
                    "output_tokens": None,
                    "allow_rate": False,
                    "has_error": has_error,
                    "aborted": aborted,
                    "model": model,
                    "started_at": started_at,
                    "ended_at": None,
                    "response_ids": (),
                    "diagnostics": (),
                }
            )
        elif raw_ttft is not None:
            staged.append(
                {
                    "kind": FIRST_OUTPUT_LATENCY,
                    "record": record,
                    "turn_id": turn_id,
                    "duration_ms": None,
                    "output_tokens": None,
                    "allow_rate": False,
                    "has_error": has_error,
                    "aborted": aborted,
                    "model": model,
                    "started_at": started_at,
                    "ended_at": None,
                    "response_ids": (),
                    "diagnostics": (f"unusable_first_output_latency:{record}",),
                }
            )

    return staged


def _read_rows_fallback(
    path: str,
) -> tuple[list[tuple[int, dict]], int, str | None, tuple[str, ...]]:
    """Minimal read-only JSONL reader used only until the shared core lands."""
    diagnostics: list[str] = []
    size = os.path.getsize(path)
    mtime_ns = os.stat(path).st_mtime_ns
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    if text and not text.endswith("\n"):
        diagnostics.append("held_incomplete_trailing_record")
        text = text[: text.rfind("\n") + 1] if "\n" in text else ""
    rows: list[tuple[int, dict]] = []
    skipped = 0
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        rows.append((lineno, obj))
    if skipped:
        diagnostics.append(f"skipped_malformed_rows:{skipped}")
    fingerprint = hashlib.sha1(f"codex:{size}:{mtime_ns}".encode()).hexdigest()[:16]
    return rows, len(rows), f"codex:{fingerprint}", tuple(diagnostics)


def get_capabilities() -> Any:
    """Codex performance capabilities; requires the shared core."""
    from sesskit.performance import PerformanceCapabilities

    return PerformanceCapabilities(
        runtime=RUNTIME,
        supported=True,
        timing_kinds=TIMING_KINDS,
        rate_kinds=RATE_KINDS,
        history_views=HISTORY_VIEWS,
        provider_decode_timing=False,
        incremental_support=False,
    )


def load(session: dict, *, history_view: str = "all_branches") -> Any:
    """Load the Codex performance snapshot for a scanned session dict."""
    from sesskit.models import Evidence, LoadState
    from sesskit.performance import (
        PerformanceSnapshot,
        make_performance_record,
        validate_performance_view,
    )

    runtime = str(session.get("source") or RUNTIME)
    session_id = str(session.get("id") or "")
    session_ref = f"{runtime}:{session_id}" if session_id else None
    validate_performance_view(history_view, get_capabilities())
    path = str(session.get("path") or "")
    if not path:
        raise ValueError("session has no history path")

    try:
        try:
            from sesskit.performance_io import read_jsonl_snapshot

            feed = read_jsonl_snapshot(path)
            rows, entries_read = list(feed.rows), feed.entries_read
            watermark: str | None = feed.watermark
            read_diagnostics = tuple(feed.diagnostics)
        except ImportError:
            rows, entries_read, watermark, read_diagnostics = _read_rows_fallback(path)
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

    staged = collect_codex_turn_performance(rows)
    records = []
    for item in staged:
        evidence = (Evidence(origin="native", field="payload", record=item["record"]),)
        if item["kind"] == TURN_ELAPSED and item["allow_rate"]:
            evidence = evidence + (Evidence(origin="inferred", field="derived_rate", record=item["record"]),)
        records.append(
            make_performance_record(
                runtime=runtime,
                session_ref=session_ref,
                record=item["record"],
                kind=item["kind"],
                scope_kind="turn" if item["turn_id"] else "unknown",
                scope_id=item["turn_id"],
                duration_ms=item["duration_ms"],
                output_tokens=item["output_tokens"],
                output_semantics="provider_output" if item["output_tokens"] is not None else "unknown",
                evidence=evidence,
                allow_rate=item["allow_rate"],
                clock="monotonic",
                started_at=item["started_at"],
                ended_at=item["ended_at"],
                turn_id=item["turn_id"],
                model=item["model"],
                diagnostics=item["diagnostics"],
            )
        )
    if records:
        state: LoadState = "available"
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
        entries_read=entries_read,
        watermark=watermark,
        observed_operations=sum(1 for item in staged if item["kind"] == TURN_ELAPSED),
        coverage=coverage,
        diagnostics=tuple(read_diagnostics),
    )
