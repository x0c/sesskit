"""Historical performance projection: timed intervals plus derived rates.

One :class:`PerformanceRecord` preserves a single named timing interval on
an existing native operation (a response, step, turn or session aggregate)
with the native session/message/response/turn identifiers that established
it. Records are timing/rate observations, never additive billing rows: two
intervals on one operation never sum, and unlike intervals never average.

All native differences live in the per-runtime
``sesskit.performance_adapters`` feeds; this calculation and serialization
core never branches on a runtime name and never reads native history.

Rate arithmetic is pure ``output_tokens * 1000 / duration_ms`` and runs
only after the adapter explicitly authorizes the numerator/interval pairing
(``allow_rate=True``) on a successful native outcome. Missing timing,
invalid durations and untrusted counters stay missing: never zero speed.
No provider decode-speed claim is exposed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from sesskit.models import Evidence, LoadState

PERFORMANCE_SCHEMA_ID = "sesskit.performance/v1"

PerformanceKind = Literal[
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

OutputSemantics = Literal[
    "inclusive_output", "visible_output", "provider_output", "unknown",
]

PerformanceClock = Literal["monotonic", "wall", "mixed", "unknown"]

RateBasis = Literal["derived", "unavailable"]

PerformanceCoverage = Literal["partial", "unknown", "not_recorded", "unsupported"]

_VALID_KINDS = frozenset({
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
})

_VALID_SEMANTICS = frozenset({
    "inclusive_output", "visible_output", "provider_output", "unknown",
})

_VALID_CLOCKS = frozenset({"monotonic", "wall", "mixed", "unknown"})

_VALID_RATE_BASES = frozenset({"derived", "unavailable"})

_VALID_COVERAGE = frozenset({"partial", "unknown", "not_recorded", "unsupported"})

_VALID_VIEWS = ("all_branches", "active_branch")

_SUMMARY_KIND_LIMIT = 25


def _valid_count(value: object) -> int | None:
    """Native output-token counter, or None when unusable.

    Booleans, negatives, non-integral floats and non-numeric payloads are
    never counters. An explicit native zero stays zero (callers decide
    whether a zero counter is trustworthy); only the shape is checked here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_duration_ms(value: object) -> float | None:
    """Native duration in milliseconds, or None when unusable as a divisor.

    Only a positive finite number divides. Zero, negatives, NaN, infinity
    and booleans are never a measurement; they degrade to missing.
    """
    if isinstance(value, bool):
        return None
    if _finite_number(value) and value > 0:
        return float(value)
    return None


def _valid_timestamp(value: object) -> float | None:
    """UTC epoch seconds, or None unless actually known.

    Only a finite non-negative number is a timestamp. Source append time
    must never be passed here disguised as a generation endpoint.
    """
    if isinstance(value, bool):
        return None
    if _finite_number(value) and value >= 0:
        return float(value)
    return None


def _clean_id(value: object) -> str | None:
    """Native identifier kept verbatim, or None when absent.

    Blank strings and non-string payloads collapse to None; identifiers
    are data-driven (native JSON), so they degrade rather than crash.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True)
class PerformanceRecord:
    """One named timing interval on an existing native operation.

    Identity (``performance_id``) derives only from the session plus the
    native record locator and kind: stable for one source generation,
    disjoint across sessions, with no content hash. ``duration_ms`` and
    ``output_tokens`` keep only validated native values; ``tokens_per_second``
    is set only with an explicitly derived basis, otherwise it stays None
    with ``rate_basis="unavailable"``. Multiple intervals on one operation
    are independent observations, never additive consumption.
    """

    performance_id: str
    runtime: str
    record: str
    scope_kind: str
    kind: str
    duration_ms: float | None
    evidence: tuple[Evidence, ...] = ()
    session_ref: str | None = None
    scope_id: str | None = None
    output_tokens: int | None = None
    output_semantics: str = "unknown"
    tokens_per_second: float | None = None
    rate_basis: str = "unavailable"
    clock: str = "unknown"
    started_at: float | None = None
    ended_at: float | None = None
    message_id: str | None = None
    turn_id: str | None = None
    response_id: str | None = None
    model: str | None = None
    provider: str | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.performance_id, str) or not self.performance_id.strip():
            raise ValueError("performance_id must not be empty")
        if not isinstance(self.runtime, str) or not self.runtime.strip():
            raise ValueError("runtime must not be empty")
        if not isinstance(self.record, str) or not self.record.strip():
            raise ValueError("record must not be empty")
        if not isinstance(self.scope_kind, str) or not self.scope_kind.strip():
            raise ValueError("scope_kind must not be empty")
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"unsupported performance kind: {self.kind!r}")
        if self.output_semantics not in _VALID_SEMANTICS:
            raise ValueError(f"unsupported output semantics: {self.output_semantics!r}")
        if self.rate_basis not in _VALID_RATE_BASES:
            raise ValueError(f"unsupported rate basis: {self.rate_basis!r}")
        if self.clock not in _VALID_CLOCKS:
            raise ValueError(f"unsupported clock: {self.clock!r}")
        duration = self.duration_ms
        if duration is not None:
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not _finite_number(duration) or duration <= 0):
                raise ValueError("duration_ms carries only a positive finite duration")
            if not isinstance(duration, float):
                object.__setattr__(self, "duration_ms", float(duration))
        count = self.output_tokens
        if (isinstance(count, float) and not isinstance(count, bool)
                and count.is_integer() and count >= 0):
            count = int(count)
            object.__setattr__(self, "output_tokens", count)
        if (count is not None and (isinstance(count, bool)
                                   or not isinstance(count, int) or count < 0)):
            raise ValueError("output_tokens carries only a native counter")
        rate = self.tokens_per_second
        if rate is not None:
            if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                    or not _finite_number(rate) or rate < 0):
                raise ValueError("tokens_per_second carries only a measured rate")
            if not isinstance(rate, float):
                object.__setattr__(self, "tokens_per_second", float(rate))
        if (self.rate_basis == "derived") != (self.tokens_per_second is not None):
            raise ValueError("a derived rate basis requires a rate and vice versa")
        for field in ("started_at", "ended_at"):
            stamp = getattr(self, field)
            if stamp is not None:
                if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                        or not _finite_number(stamp) or stamp < 0):
                    raise ValueError(f"{field} carries only UTC epoch seconds")
                if not isinstance(stamp, float):
                    object.__setattr__(self, field, float(stamp))
        evidence = self.evidence
        if isinstance(evidence, list):
            evidence = tuple(evidence)
            object.__setattr__(self, "evidence", evidence)
        if not isinstance(evidence, tuple) or any(
                not isinstance(item, Evidence) for item in evidence):
            raise ValueError("evidence carries only Evidence entries")
        diagnostics = self.diagnostics
        if isinstance(diagnostics, list):
            diagnostics = tuple(diagnostics)
            object.__setattr__(self, "diagnostics", diagnostics)
        if not isinstance(diagnostics, tuple) or any(
                not isinstance(item, str) for item in diagnostics):
            raise ValueError("diagnostics carries only stable strings")
        for field in ("session_ref", "scope_id", "message_id", "turn_id",
                      "response_id", "model", "provider"):
            value = getattr(self, field)
            cleaned = _clean_id(value)
            if cleaned != value:
                object.__setattr__(self, field, cleaned)


@dataclass(frozen=True)
class PerformanceSnapshot:
    """One session's preserved timing/rate observations.

    ``state`` follows the shared load states: a readable history without
    recorded timing is ``empty`` with ``not_recorded`` coverage, never a
    zero-speed session; read failure is ``unavailable`` (coverage
    ``unknown``: failure proves nothing about what was recorded), never
    empty success. Coverage is ``partial``/``unknown``/``not_recorded``/
    ``unsupported``, never falsely ``complete``. The snapshot is a
    projection over shared history identities, not a second hierarchy.
    """

    state: LoadState
    runtime: str
    session_ref: str | None
    history_view: str = "all_branches"
    records: tuple[PerformanceRecord, ...] = ()
    entries_read: int = 0
    watermark: str | None = None
    observed_operations: int = 0
    coverage: str = "not_recorded"
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in {"available", "empty", "unavailable", "unsupported"}:
            raise ValueError(f"unsupported load state: {self.state!r}")
        if not isinstance(self.runtime, str) or not self.runtime.strip():
            raise ValueError("runtime must not be empty")
        if not isinstance(self.history_view, str) or not self.history_view.strip():
            raise ValueError("history_view must not be empty")
        if self.coverage not in _VALID_COVERAGE:
            raise ValueError(f"unsupported performance coverage: {self.coverage!r}")
        if not isinstance(self.records, tuple) or any(
                not isinstance(item, PerformanceRecord) for item in self.records):
            raise ValueError("records carries only PerformanceRecord entries")
        if self.state == "available" and not self.records:
            raise ValueError("an available snapshot must carry at least one record")
        if self.state in {"empty", "unavailable", "unsupported"} and self.records:
            raise ValueError(f"a {self.state} snapshot must not carry records")
        for field in ("entries_read", "observed_operations"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} carries only a non-negative count")
        if self.watermark is not None and (
                not isinstance(self.watermark, str) or not self.watermark.strip()):
            raise ValueError("watermark is omitted when unavailable")
        if self.session_ref is not None and not isinstance(self.session_ref, str):
            raise ValueError("session_ref carries only a native string")
        if not isinstance(self.diagnostics, tuple) or any(
                not isinstance(item, str) for item in self.diagnostics):
            raise ValueError("diagnostics carries only stable strings")


@dataclass(frozen=True)
class PerformanceCapabilities:
    """Detailed performance support per runtime (extends the coarse flag)."""

    runtime: str
    supported: bool
    timing_kinds: tuple[str, ...] = ()
    rate_kinds: tuple[str, ...] = ()
    history_views: tuple[str, ...] = ("all_branches",)
    provider_decode_timing: bool = False
    incremental_support: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, str) or not self.runtime.strip():
            raise ValueError("runtime must not be empty")
        for field in ("timing_kinds", "rate_kinds"):
            kinds = getattr(self, field)
            if not isinstance(kinds, tuple) or any(
                    kind not in _VALID_KINDS for kind in kinds):
                raise ValueError(f"{field} carries only named performance kinds")
        if (not isinstance(self.history_views, tuple) or not self.history_views
                or any(view not in _VALID_VIEWS for view in self.history_views)):
            raise ValueError("history_views carries only supported branch views")


def make_performance_record(
    *,
    runtime: str,
    session_ref: str | None,
    record: str,
    kind: str,
    scope_kind: str = "unknown",
    scope_id: object = None,
    duration_ms: object = None,
    output_tokens: object = None,
    output_semantics: str = "unknown",
    evidence: tuple[Evidence, ...] = (),
    allow_rate: bool = False,
    clock: str = "unknown",
    started_at: object = None,
    ended_at: object = None,
    message_id: object = None,
    turn_id: object = None,
    response_id: object = None,
    model: object = None,
    provider: object = None,
    diagnostics: tuple[str, ...] = (),
) -> PerformanceRecord:
    """Build one validated record; invalid native data degrades, never crashes.

    Identity derives only from the session plus the native record locator
    and kind, so repeats of one source generation agree and sessions stay
    disjoint, with no content hash. ``duration_ms``/``output_tokens`` keep
    only validated native values: booleans-as-numbers, negatives,
    NaN/Infinity and non-positive divisors become missing with a stable
    diagnostic, never zero speed and never payload values. The rate derives
    only when ``allow_rate`` authorizes the pairing and both sides are
    valid; otherwise it stays missing with ``rate_basis="unavailable"``.
    Passed evidence (native input evidence) is preserved and the derived
    quotient gains explicit inferred evidence.
    """
    if not isinstance(runtime, str) or not runtime.strip():
        raise ValueError("runtime must not be empty")
    if not isinstance(record, str) or not record.strip():
        raise ValueError("record must not be empty")
    if kind not in _VALID_KINDS:
        raise ValueError(f"unsupported performance kind: {kind!r}")
    if output_semantics not in _VALID_SEMANTICS:
        raise ValueError(f"unsupported output semantics: {output_semantics!r}")
    if clock not in _VALID_CLOCKS:
        raise ValueError(f"unsupported clock: {clock!r}")
    if session_ref is not None and not isinstance(session_ref, str):
        raise ValueError("session_ref carries only a native string")
    native_evidence = tuple(evidence)
    if any(not isinstance(item, Evidence) for item in native_evidence):
        raise ValueError("evidence carries only Evidence entries")
    own_diagnostics: list[str] = []
    entries = tuple(diagnostics)
    if any(not isinstance(item, str) for item in entries):
        raise ValueError("diagnostics carries only stable strings")
    for item in entries:
        if item not in own_diagnostics:
            own_diagnostics.append(item)

    if not isinstance(scope_kind, str) or not scope_kind.strip():
        scope_kind = "unknown"
        own_diagnostics.append("scope_kind_unknown")

    duration = _valid_duration_ms(duration_ms)
    if duration_ms is not None and duration is None:
        own_diagnostics.append("duration_invalid")
    count = _valid_count(output_tokens)
    if output_tokens is not None and count is None:
        own_diagnostics.append("output_tokens_invalid")

    tokens_per_second: float | None = None
    rate_basis: str = "unavailable"
    record_evidence = list(native_evidence)
    if allow_rate and duration is not None and count is not None:
        try:
            candidate = count * 1000.0 / duration
        except OverflowError:
            candidate = math.inf
        if math.isfinite(candidate):
            tokens_per_second = candidate
            rate_basis = "derived"
            record_evidence.append(Evidence("inferred", field="tokens_per_second", record=record))
        else:
            own_diagnostics.append("rate_unavailable")
    elif allow_rate:
        own_diagnostics.append("rate_unavailable")

    start = _valid_timestamp(started_at)
    if started_at is not None and start is None:
        own_diagnostics.append("started_at_invalid")
    end = _valid_timestamp(ended_at)
    if ended_at is not None and end is None:
        own_diagnostics.append("ended_at_invalid")

    base = session_ref.strip() if isinstance(session_ref, str) and session_ref.strip() else runtime.strip()
    performance_id = f"{base}:{record}:{kind}"
    return PerformanceRecord(
        performance_id=performance_id,
        runtime=runtime.strip(),
        record=record,
        scope_kind=scope_kind.strip(),
        kind=kind,
        duration_ms=duration,
        evidence=tuple(record_evidence),
        session_ref=session_ref.strip() if isinstance(session_ref, str) and session_ref.strip() else None,
        scope_id=_clean_id(scope_id),
        output_tokens=count,
        output_semantics=output_semantics,
        tokens_per_second=tokens_per_second,
        rate_basis=rate_basis,
        clock=clock,
        started_at=start,
        ended_at=end,
        message_id=_clean_id(message_id),
        turn_id=_clean_id(turn_id),
        response_id=_clean_id(response_id),
        model=_clean_id(model),
        provider=_clean_id(provider),
        diagnostics=tuple(own_diagnostics),
    )


def performance_record_to_dict(record: PerformanceRecord) -> dict:
    """Serialize one record to safe typed fields plus evidence dictionaries.

    Identifier linkage (scope/message/turn/response/model/provider) stays
    flat beside the metrics, matching ``schemas/performance.v1.json``:
    never nested payloads, never message content or paths.
    """
    return {
        "performance_id": record.performance_id,
        "runtime": record.runtime,
        "record": record.record,
        "scope_kind": record.scope_kind,
        "scope_id": record.scope_id,
        "kind": record.kind,
        "duration_ms": record.duration_ms,
        "output_tokens": record.output_tokens,
        "output_semantics": record.output_semantics,
        "tokens_per_second": record.tokens_per_second,
        "rate_basis": record.rate_basis,
        "clock": record.clock,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "message_id": record.message_id,
        "turn_id": record.turn_id,
        "response_id": record.response_id,
        "model": record.model,
        "provider": record.provider,
        "evidence": [
            {"origin": item.origin, "field": item.field, "record": item.record}
            for item in record.evidence
        ],
        "diagnostics": list(record.diagnostics),
    }


def performance_summary(snapshot: PerformanceSnapshot) -> dict:
    """Bounded summary over a snapshot: no averages, no summed durations.

    Unlike intervals never average and overlapping durations never sum, so
    the summary counts records and rates, names the distinct kinds
    (bounded), and carries only the latest source-order record.
    """
    records = list(snapshot.records)
    kinds: list[str] = []
    for record in records:
        if record.kind not in kinds:
            kinds.append(record.kind)
    kinds = kinds[:_SUMMARY_KIND_LIMIT]
    return {
        "coverage": snapshot.coverage,
        "record_count": len(records),
        "rate_count": sum(1 for record in records
                          if record.tokens_per_second is not None),
        "observed_operations": snapshot.observed_operations,
        "kinds": kinds,
        "latest": performance_record_to_dict(records[-1]) if records else None,
    }


def performance_snapshot_to_dict(snapshot: PerformanceSnapshot) -> dict:
    """Serialize a snapshot; the watermark exports as a boolean only."""
    return {
        "schema": PERFORMANCE_SCHEMA_ID,
        "runtime": snapshot.runtime,
        "session_ref": snapshot.session_ref,
        "state": snapshot.state,
        "history_view": snapshot.history_view,
        "summary": performance_summary(snapshot),
        "entries_read": snapshot.entries_read,
        "watermark": snapshot.watermark is not None,
        "diagnostics": list(snapshot.diagnostics),
    }


def validate_performance_view(history_view: str,
                              capabilities: PerformanceCapabilities) -> None:
    """Reject branch views the runtime does not support; never fall back."""
    if history_view not in tuple(capabilities.history_views):
        raise ValueError(
            f"unsupported history view: {history_view!r}"
            f" for runtime {capabilities.runtime!r}"
        )


def unsupported_performance_snapshot(*, runtime: str, session_ref: str | None,
                                     history_view: str) -> PerformanceSnapshot:
    """Performance state for a runtime with no verified timing mapping."""
    return PerformanceSnapshot(
        "unsupported", runtime.strip() if isinstance(runtime, str) and runtime.strip() else "unknown",
        session_ref, history_view,
        (), 0, None, 0, "unsupported",
        ("runtime_performance_unsupported",),
    )


def performance_capabilities(runtime: str) -> PerformanceCapabilities:
    """Detailed performance support for one runtime; unknown runtimes report False."""
    from sesskit.adapters import get_adapter

    name = str(runtime or "unknown") or "unknown"
    try:
        adapter = get_adapter(str(runtime or ""))
    except KeyError:
        return PerformanceCapabilities(name, False)
    return adapter.performance_capabilities()


def load_performance(session: dict, *, history_view: str = "all_branches") -> PerformanceSnapshot:
    """Load preserved timing/rate observations for one session dict.

    Dispatch goes through the runtime adapter registry: each adapter owns
    its native feed, while calculation and serialization stay in this
    module's runtime-agnostic core. Unknown runtimes and runtimes without
    a verified timing mapping yield ``unsupported``; missing or unreadable
    history yields ``unavailable``. A readable history without recorded
    timing is ``empty`` with ``not_recorded`` coverage.
    """
    from sesskit.adapters import get_adapter

    runtime = str(session.get("source") or "")
    session_id = str(session.get("id") or "")
    session_ref = f"{runtime}:{session_id}" if runtime and session_id else None
    try:
        adapter = get_adapter(runtime)
    except KeyError:
        return unsupported_performance_snapshot(
            runtime=runtime or "unknown", session_ref=session_ref,
            history_view=history_view)
    validate_performance_view(history_view, adapter.performance_capabilities())
    return adapter.load_performance(session, history_view=history_view)


__all__ = [
    "PERFORMANCE_SCHEMA_ID",
    "PerformanceCapabilities",
    "PerformanceRecord",
    "PerformanceSnapshot",
    "load_performance",
    "make_performance_record",
    "performance_capabilities",
    "performance_record_to_dict",
    "performance_snapshot_to_dict",
    "performance_summary",
    "unsupported_performance_snapshot",
    "validate_performance_view",
]
