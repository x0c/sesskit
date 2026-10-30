"""Native usage observations (U1): one preserved record per native reading.

Each :class:`UsageObservation` keeps what the source said: which native
record it came from, what entity/counter/operation it describes, and the
native numbers with their evidence. Observations are never additive on
their own: the same model call can appear in several records (split
messages, repeated snapshots), and cumulative counters are revised over
time. Reconciliation and summation belong to a later stage; this module
only collects, never deduplicates or totals.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Literal

from sesskit.models import Evidence, LoadState, Usage

ScopeKind = Literal["call", "message", "turn", "session", "unknown"]
CounterKind = Literal["absolute", "delta", "cumulative", "unknown"]
OperationKind = Literal[
    "model_call", "compaction", "branch_summary",
    "cache_warm", "tool_nested", "unknown",
]
CostBasis = Literal["provider_reported", "runtime_estimate", "unknown"]
ValueQuality = Literal["final", "provisional", "placeholder", "unknown"]


@dataclass(frozen=True)
class UsageObservation:
    """One native usage reading, preserved with its correlation identity."""

    observation_id: str
    runtime: str
    record: str
    scope_kind: ScopeKind
    counter_kind: CounterKind
    operation: OperationKind
    usage: Usage
    evidence: Evidence
    scope_id: str | None = None
    request_id: str | None = None
    response_id: str | None = None
    turn_id: str | None = None
    message_id: str | None = None
    provider: str | None = None
    counter_name: str | None = None
    operation_detail: str | None = None
    cost_basis: CostBasis = "unknown"
    quality: ValueQuality = "unknown"
    # Extra native numeric usage keys not mapped to typed fields, kept as
    # (name, value) pairs. Never summed; surviving namespacing until a
    # verified unit/inclusion mapping exists.
    native_subdivisions: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("observation_id must not be empty")
        if not self.runtime.strip():
            raise ValueError("runtime must not be empty")
        if self.scope_kind not in {"call", "message", "turn", "session", "unknown"}:
            raise ValueError(f"unsupported scope: {self.scope_kind!r}")
        if self.counter_kind not in {"absolute", "delta", "cumulative", "unknown"}:
            raise ValueError(f"unsupported counter: {self.counter_kind!r}")
        if self.operation not in {
            "model_call", "compaction", "branch_summary",
            "cache_warm", "tool_nested", "unknown",
        }:
            raise ValueError(f"unsupported operation: {self.operation!r}")


def _observation_id(runtime: str, record: str, index: int) -> str:
    return f"{runtime}:{record}:{index}"


def _native_evidence(record: str, field: str | None = None) -> Evidence:
    return Evidence("native", field=field, record=record)


# --- shared int helper ---

def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


# --- Claude ---

def collect_claude_observations(rows: list[tuple[int, dict]]) -> list[UsageObservation]:
    """Preserve one observation per assistant message carrying usage.

    A model response split into thinking/text/tool fragments shares one
    native record and message id; each fragment row currently yields its
    own observation with the same correlation identity so a later stage
    can collapse them to a single call. Placeholder/output-finality
    resolution is explicitly out of scope here: quality stays unknown
    unless the source marks it.
    """
    observations: list[UsageObservation] = []
    for lineno, entry in rows:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        model = message.get("model")
        if not isinstance(usage, dict) and not isinstance(model, str):
            continue
        record = f"line:{lineno}"
        native_id = message.get("id")
        fields = {
            "input_tokens": _int(usage.get("input_tokens")) if isinstance(usage, dict) else None,
            "output_tokens": _int(usage.get("output_tokens")) if isinstance(usage, dict) else None,
            "cache_read_tokens": _int(usage.get("cache_read_input_tokens")) if isinstance(usage, dict) else None,
            "cache_write_tokens": _int(usage.get("cache_creation_input_tokens")) if isinstance(usage, dict) else None,
        }
        if all(value is None for value in fields.values()) and not isinstance(model, str):
            continue
        observations.append(UsageObservation(
            observation_id=_observation_id("claude", record, len(observations)),
            runtime="claude",
            record=record,
            scope_kind="message",
            counter_kind="absolute",
            operation="model_call",
            usage=Usage(
                _native_evidence(record, "usage"),
                model=model.strip() if isinstance(model, str) and model.strip() else None,
                **fields,  # type: ignore[arg-type]
            ),
            evidence=_native_evidence(record, "usage"),
            scope_id=str(native_id) if isinstance(native_id, str) and native_id else None,
            response_id=str(native_id) if isinstance(native_id, str) and native_id else None,
            message_id=str(native_id) if isinstance(native_id, str) and native_id else None,
            quality="unknown",
        ))
    return observations


# --- Codex ---

def _codex_turn_id(payload: dict) -> str | None:
    for key in ("turn_id", "turnId", "turnID"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def collect_codex_observations(rows: list[tuple[int, dict]]) -> list[UsageObservation]:
    """Preserve every usage-shaped Codex record without collapsing revisions.

    Both request-level ``usage`` blocks (keyed by response id) and
    ``turn_token_usage`` cumulative snapshots are kept as separate
    observations in file order. A later reconciliation stage selects the
    latest valid snapshot per counter epoch; this collector must not
    keep only the first (``setdefault``) or sum the revisions.
    Legacy ``token_count`` info blocks are preserved with unknown request
    linkage rather than dropped.
    """
    observations: list[UsageObservation] = []

    def add(
        *, record: str, block: dict, payload: dict,
        scope_kind: ScopeKind, counter_kind: CounterKind,
        response_id: str | None, turn_id: str | None,
    ) -> None:
        fields = {
            "input_tokens": _int(block.get("input_tokens")),
            "output_tokens": _int(block.get("output_tokens")),
            "cache_read_tokens": _int(block.get("cached_input_tokens")),
            "cache_write_tokens": _int(block.get("cache_write_input_tokens")),
            "reasoning_tokens": _int(block.get("reasoning_output_tokens")),
            "total_tokens": _int(block.get("total_tokens")),
        }
        model = payload.get("model")
        if all(value is None for value in fields.values()) and not isinstance(model, str):
            return
        observations.append(UsageObservation(
            observation_id=_observation_id("codex", record, len(observations)),
            runtime="codex",
            record=record,
            scope_kind=scope_kind,
            counter_kind=counter_kind,
            operation="model_call",
            usage=Usage(
                _native_evidence(record, "payload"),
                model=model.strip() if isinstance(model, str) and model.strip() else None,
                **fields,  # type: ignore[arg-type]
            ),
            evidence=_native_evidence(record, "payload"),
            scope_id=response_id or turn_id,
            response_id=response_id,
            turn_id=turn_id,
            counter_name="turn_token_usage" if counter_kind == "cumulative" else "usage",
            quality="unknown",
        ))

    for lineno, entry in rows:
        if not isinstance(entry, dict):
            continue
        record = f"line:{lineno}"
        entry_type = str(entry.get("type") or "")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if entry_type == "token_usage_record":
            turn_id = _codex_turn_id(payload)
            response_id = payload.get("response_id")
            response_id = response_id.strip() if isinstance(response_id, str) and response_id.strip() else None
            turn_block = payload.get("turn_token_usage")
            if isinstance(turn_block, dict):
                add(record=record, block=turn_block, payload=payload,
                    scope_kind="turn", counter_kind="cumulative",
                    response_id=response_id, turn_id=turn_id)
            request_block = payload.get("usage")
            if isinstance(request_block, dict):
                add(record=record, block=request_block, payload=payload,
                    scope_kind="call", counter_kind="absolute",
                    response_id=response_id, turn_id=turn_id)
            continue
        if entry_type == "event_msg" and isinstance(payload.get("info"), dict):
            info = payload["info"]
            for key in ("last_token_usage", "last_usage", "total_token_usage", "total_usage"):
                block = info.get(key)
                if isinstance(block, dict):
                    add(record=record, block=block, payload={**payload, **info},
                        scope_kind="turn" if key.startswith("total") else "call",
                        counter_kind="cumulative" if key.startswith("total") else "absolute",
                        response_id=None, turn_id=_codex_turn_id(payload) or _codex_turn_id(info))
    return observations


# --- OpenCode ---

def _opencode_int(value: object) -> int | None:
    return _int(value)


def collect_opencode_observations(
    messages: list[dict],
    steps: list[dict] | None = None,
    session_rollup: dict | None = None,
) -> list[UsageObservation]:
    """Preserve per-message, per-step and session-aggregate OpenCode usage.

    Each level is a separate observation with its own scope; message/step
    values and session aggregates must never enter the same sum. The
    collector does not decide which level is complete for a session.
    """
    observations: list[UsageObservation] = []

    def tokens_of(data: dict) -> dict:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        return {
            "input_tokens": _opencode_int(tokens.get("input")),
            "output_tokens": _opencode_int(tokens.get("output")),
            "cache_read_tokens": _opencode_int(cache.get("read")),
            "cache_write_tokens": _opencode_int(cache.get("write")),
            "reasoning_tokens": _opencode_int(tokens.get("reasoning")),
            "total_tokens": None,
        }

    def model_of(data: dict) -> str | None:
        model = data.get("modelID")
        nested = data.get("model")
        if isinstance(nested, dict) and isinstance(nested.get("id"), str) and nested["id"].strip():
            model = nested["id"].strip()
        return model.strip() if isinstance(model, str) and model.strip() else None

    def cost_of(data: dict) -> tuple[float | None, CostBasis]:
        cost = data.get("cost")
        if isinstance(cost, bool):
            return None, "unknown"
        if isinstance(cost, (int, float)) and cost >= 0:
            return float(cost), "unknown"
        return None, "unknown"

    for message in messages:
        if not isinstance(message, dict):
            continue
        mid = message.get("id") or message.get("message_id") or message.get("messageId")
        fields = tokens_of(message)
        cost, basis = cost_of(message)
        model = model_of(message)
        if all(value is None for value in fields.values()) and cost is None and model is None:
            continue
        record = f"message:{mid}" if isinstance(mid, str) and mid else "message:unknown"
        observations.append(UsageObservation(
            observation_id=_observation_id("opencode", record, len(observations)),
            runtime="opencode",
            record=record,
            scope_kind="message",
            counter_kind="absolute",
            operation="model_call",
            usage=Usage(_native_evidence(record, "tokens"), model, cost=cost, **fields),  # type: ignore[arg-type]
            evidence=_native_evidence(record, "tokens"),
            scope_id=str(mid) if isinstance(mid, str) and mid else None,
            message_id=str(mid) if isinstance(mid, str) and mid else None,
            cost_basis=basis,
            quality="unknown",
        ))
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        sid = step.get("id") or step.get("step_id")
        fields = tokens_of(step)
        cost, basis = cost_of(step)
        model = model_of(step)
        if all(value is None for value in fields.values()) and cost is None and model is None:
            continue
        record = f"step:{sid}" if isinstance(sid, str) and sid else "step:unknown"
        observations.append(UsageObservation(
            observation_id=_observation_id("opencode", record, len(observations)),
            runtime="opencode",
            record=record,
            scope_kind="turn",
            counter_kind="absolute",
            operation="model_call",
            operation_detail="step",
            usage=Usage(_native_evidence(record, "tokens"), model, cost=cost, **fields),  # type: ignore[arg-type]
            evidence=_native_evidence(record, "tokens"),
            scope_id=str(sid) if isinstance(sid, str) and sid else None,
            cost_basis=basis,
            quality="unknown",
        ))
    if isinstance(session_rollup, dict):
        fields = tokens_of(session_rollup)
        cost, basis = cost_of(session_rollup)
        model = model_of(session_rollup)
        if not (all(value is None for value in fields.values()) and cost is None and model is None):
            observations.append(UsageObservation(
                observation_id=_observation_id("opencode", "session", len(observations)),
                runtime="opencode",
                record="session:rollup",
                scope_kind="session",
                counter_kind="cumulative",
                operation="model_call",
                operation_detail="session_rollup",
                usage=Usage(_native_evidence("session:rollup", "tokens"), model, cost=cost, **fields),  # type: ignore[arg-type]
                evidence=_native_evidence("session:rollup", "tokens"),
                scope_id="session",
                cost_basis=basis,
                quality="unknown",
            ))
    return observations


# --- Pi ---

def _pi_int(value: object) -> int | None:
    return _int(value)


def collect_pi_observations(entries: list[dict]) -> list[UsageObservation]:
    """Preserve every Pi usage carrier, including text-less assistant turns.

    Covers assistant message usage (even when the message carries only
    tool calls), nested tool-result usage, compaction usage, branch-summary
    usage and independent ``usage`` entries. Unknown entry kinds are kept
    with ``operation="unknown"`` rather than dropped so a later stage can
    decide their accounting scope.
    """
    observations: list[UsageObservation] = []

    def usage_from(data: dict, record: str) -> Usage | None:
        usage = data.get("usage")
        model = data.get("model")
        if not isinstance(usage, dict) and not isinstance(model, str):
            return None, ()
        fields = {
            "input_tokens": _pi_int(usage.get("input")) if isinstance(usage, dict) else None,
            "output_tokens": _pi_int(usage.get("output")) if isinstance(usage, dict) else None,
            "cache_read_tokens": _pi_int(usage.get("cacheRead")) if isinstance(usage, dict) else None,
            "cache_write_tokens": _pi_int(usage.get("cacheWrite")) if isinstance(usage, dict) else None,
            "reasoning_tokens": _pi_int(usage.get("reasoning")) if isinstance(usage, dict) else None,
            "total_tokens": _pi_int(usage.get("totalTokens")) if isinstance(usage, dict) else None,
        }
        subdivisions: list[tuple[str, int]] = []
        if isinstance(usage, dict):
            for key, value in usage.items():
                if key in {"input", "output", "cacheRead", "cacheWrite",
                           "reasoning", "totalTokens", "cost"}:
                    continue
                parsed = _pi_int(value)
                if parsed is not None:
                    subdivisions.append((str(key), parsed))
        cost: float | None = None
        if isinstance(usage, dict):
            details = usage.get("cost")
            if isinstance(details, dict):
                total = details.get("total")
                if isinstance(total, (int, float)) and not isinstance(total, bool) and total >= 0:
                    cost = float(total)
        if all(value is None for value in fields.values()) and cost is None and not isinstance(model, str):
            return None, ()
        return Usage(
            _native_evidence(record, "usage"),
            model.strip() if isinstance(model, str) and model.strip() else None,
            cost=cost,
            **fields,  # type: ignore[arg-type]
        ), tuple(subdivisions)

    for position, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            continue
        entry_type = str(item.get("type") or "")
        record = f"entry:{item.get('id')}" if isinstance(item.get("id"), str) else f"entry:#{position}"
        message = item.get("message")
        if entry_type == "message" and isinstance(message, dict):
            role = message.get("role")
            parsed, subdivisions = usage_from(message, record)
            if parsed is None:
                continue
            provider = message.get("provider")
            response_id = message.get("responseId") or message.get("response_id")
            if role == "assistant":
                operation: OperationKind = "model_call"
            elif role == "toolResult":
                operation = "tool_nested"
            else:
                operation = "unknown"
            observations.append(UsageObservation(
                observation_id=_observation_id("pi", record, len(observations)),
                runtime="pi",
                record=record,
                scope_kind="message" if role == "assistant" else "unknown",
                counter_kind="absolute",
                operation=operation,
                usage=parsed,
                evidence=_native_evidence(record, "usage"),
                scope_id=str(item.get("id")) if isinstance(item.get("id"), str) else None,
                response_id=str(response_id) if isinstance(response_id, str) and response_id else None,
                message_id=str(item.get("id")) if isinstance(item.get("id"), str) else None,
                provider=str(provider) if isinstance(provider, str) and provider else None,
                quality="unknown",
                native_subdivisions=subdivisions,
            ))
            continue
        # Compaction / branch-summary / independent usage carriers.
        carrier: dict | None = None
        operation: OperationKind = "unknown"
        if entry_type in {"compaction", "branch_summary", "usage"}:
            carrier = item
            operation = (
                "compaction" if entry_type == "compaction"
                else "branch_summary" if entry_type == "branch_summary"
                else "unknown"
            )
        elif isinstance(item.get("usage"), dict):
            carrier = item
        if carrier is not None:
            parsed, subdivisions = usage_from(carrier, record)
            if parsed is None:
                continue
            observations.append(UsageObservation(
                observation_id=_observation_id("pi", record, len(observations)),
                runtime="pi",
                record=record,
                scope_kind="unknown",
                counter_kind="unknown",
                operation=operation,
                operation_detail=None if operation == "unknown" else entry_type,
                usage=parsed,
                evidence=_native_evidence(record, "usage"),
                scope_id=str(item.get("id")) if isinstance(item.get("id"), str) else None,
                message_id=str(item.get("id")) if isinstance(item.get("id"), str) else None,
                quality="unknown",
                native_subdivisions=subdivisions,
            ))
    return observations


SUPPORTED_RUNTIMES = ("claude", "codex", "opencode", "pi")
UNSUPPORTED_RUNTIMES = ("cursor", "kimi")


def describe_support(runtime: str) -> Literal["supported", "unsupported"]:
    """Capability probe for usage observations: no history access needed."""
    if runtime in SUPPORTED_RUNTIMES:
        return "supported"
    return "unsupported"


# --- U2: normalization, reconciliation, summaries ---
#
# Normalization adapters are per-runtime mappings from native records to the
# shared metric names below. The reconciliation and summary cores that follow
# consume only those normalized metrics plus scope/counter/identity labels;
# they never branch on a runtime name or duplicate session/message identity.

RecordStanding = Literal["firm", "ambiguous", "conflicted"]
Coverage = Literal["complete", "partial", "unknown", "conflicted", "not_recorded", "unsupported"]


@dataclass(frozen=True)
class NormalizedUsage:
    """Inclusive metric names shared across runtimes; legacy ``Usage`` meanings unchanged.

    ``total`` is the calculated inclusive input-plus-output where the source
    variant supports that arithmetic; ``reported_total`` keeps the native
    total beside it. A disagreement between them is data, never a reason to
    overwrite one. ``gaps`` names expected addends that were absent while a
    metric was computed from a partial set; absent differs from zero.
    """

    input_total: int | None = None
    input_uncached: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output_total: int | None = None
    reasoning: int | None = None
    total: int | None = None
    reported_total: int | None = None
    cost: float | None = None
    currency: str | None = None
    cost_basis: CostBasis = "unknown"
    gaps: tuple[str, ...] = ()
    derivation: tuple[str, ...] = ()


@dataclass(frozen=True)
class UsageRecord:
    """One reconciled unit: proven duplicates collapsed, revisions folded.

    ``standing`` is ``firm`` for correlated records, ``ambiguous`` when no
    native linkage proves what the observation duplicates, and
    ``conflicted`` when the same identity carries differing absolute
    values. Ambiguous and conflicted records stay inspectable but never
    enter additive sums.
    """

    usage_id: str
    runtime: str
    observation_ids: tuple[str, ...]
    scope_kind: ScopeKind
    counter_kind: CounterKind
    operation: OperationKind
    normalized: NormalizedUsage
    revision: int = 0
    standing: RecordStanding = "firm"
    response_id: str | None = None
    turn_id: str | None = None
    message_id: str | None = None
    scope_id: str | None = None
    model: str | None = None
    provider: str | None = None
    counter_name: str | None = None
    native_subdivisions: tuple[tuple[str, int], ...] = ()
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.usage_id.strip():
            raise ValueError("usage_id must not be empty")
        if not self.observation_ids:
            raise ValueError("a record must cite at least one observation")
        if self.standing not in {"firm", "ambiguous", "conflicted"}:
            raise ValueError(f"unsupported standing: {self.standing!r}")


def _present_sum(values: list[int | None]) -> tuple[int | None, tuple[str, ...]]:
    """Sum present components; name the missing ones as gaps, never as zero."""
    present = [value for value in values if value is not None]
    if not present:
        return None, ()
    return sum(present), ()


def _normalize_claude(observation: UsageObservation) -> NormalizedUsage:
    usage = observation.usage
    parts = {
        "input.input_uncached": usage.input_tokens,
        "input.cache_read": usage.cache_read_tokens,
        "input.cache_write": usage.cache_write_tokens,
    }
    present = {name: value for name, value in parts.items() if value is not None}
    gaps = tuple(sorted(set(parts) - set(present))) if present else ()
    input_total = sum(present.values()) if present else None
    output_total = usage.output_tokens
    total = (
        input_total + output_total
        if input_total is not None and output_total is not None
        else None
    )
    return NormalizedUsage(
        input_total=input_total,
        input_uncached=usage.input_tokens,
        cache_read=usage.cache_read_tokens,
        cache_write=usage.cache_write_tokens,
        output_total=output_total,
        total=total,
        cost=usage.cost,
        cost_basis=observation.cost_basis,
        gaps=gaps,
        derivation=("anthropic_inclusive_input",),
    )


def _normalize_pi(observation: UsageObservation) -> NormalizedUsage:
    usage = observation.usage
    parts = {
        "input.input_uncached": usage.input_tokens,
        "input.cache_read": usage.cache_read_tokens,
        "input.cache_write": usage.cache_write_tokens,
    }
    present = {name: value for name, value in parts.items() if value is not None}
    gaps = tuple(sorted(set(parts) - set(present))) if present else ()
    input_total = sum(present.values()) if present else None
    output_total = usage.output_tokens
    diagnostics: list[str] = ["pi_inclusive_input", "pi_reasoning_subset_of_output"]
    if (
        usage.reasoning_tokens is not None
        and output_total is not None
        and usage.reasoning_tokens > output_total
    ):
        diagnostics.append("reasoning_exceeds_output")
    total = (
        input_total + output_total
        if input_total is not None and output_total is not None
        else None
    )
    return NormalizedUsage(
        input_total=input_total,
        input_uncached=usage.input_tokens,
        cache_read=usage.cache_read_tokens,
        cache_write=usage.cache_write_tokens,
        output_total=output_total,
        reasoning=usage.reasoning_tokens,
        total=total,
        reported_total=usage.total_tokens,
        cost=usage.cost,
        cost_basis=observation.cost_basis,
        gaps=gaps,
        derivation=tuple(diagnostics),
    )


def _normalize_opencode(observation: UsageObservation) -> NormalizedUsage:
    usage = observation.usage
    in_parts = {
        "input.input_uncached": usage.input_tokens,
        "input.cache_read": usage.cache_read_tokens,
        "input.cache_write": usage.cache_write_tokens,
    }
    in_present = {name: value for name, value in in_parts.items() if value is not None}
    out_parts = {
        "output.visible": usage.output_tokens,
        "output.reasoning": usage.reasoning_tokens,
    }
    out_present = {name: value for name, value in out_parts.items() if value is not None}
    gaps = tuple(sorted(
        (set(in_parts) - set(in_present) if in_present else set())
        | (set(out_parts) - set(out_present) if out_present else set())
    ))
    input_total = sum(in_present.values()) if in_present else None
    output_total = sum(out_present.values()) if out_present else None
    total = (
        input_total + output_total
        if input_total is not None and output_total is not None
        else None
    )
    return NormalizedUsage(
        input_total=input_total,
        input_uncached=usage.input_tokens,
        cache_read=usage.cache_read_tokens,
        cache_write=usage.cache_write_tokens,
        output_total=output_total,
        reasoning=usage.reasoning_tokens,
        total=total,
        cost=usage.cost,
        cost_basis=observation.cost_basis,
        gaps=gaps,
        derivation=("opencode_exclusive_buckets",),
    )


def _normalize_codex(observation: UsageObservation) -> NormalizedUsage:
    # Cached input is a subset of reported input and reasoning may already be
    # included in the reported output: preserve every bucket, add none.
    usage = observation.usage
    return NormalizedUsage(
        input_total=usage.input_tokens,
        input_uncached=None,
        cache_read=usage.cache_read_tokens,
        cache_write=usage.cache_write_tokens,
        output_total=usage.output_tokens,
        reasoning=usage.reasoning_tokens,
        total=None,
        reported_total=usage.total_tokens,
        cost=usage.cost,
        cost_basis=observation.cost_basis,
        gaps=(),
        derivation=("codex_no_additive_rollup", "total_native_reported_only"),
    )


_NORMALIZERS = {
    "claude": _normalize_claude,
    "pi": _normalize_pi,
    "opencode": _normalize_opencode,
    "codex": _normalize_codex,
}


def normalize_observation(observation: UsageObservation) -> NormalizedUsage:
    """Map one observation to shared metric names via its runtime adapter."""
    normalizer = _NORMALIZERS.get(observation.runtime)
    if normalizer is None:
        return NormalizedUsage(cost_basis=observation.cost_basis)
    return normalizer(observation)


def _record_key(observation: UsageObservation) -> tuple:
    # Scope joins the identity: a request detail and a turn snapshot that
    # share one response id are different records (the snapshot covers the
    # turn so far), never conflicting duplicates of each other. Genuine
    # duplicates share scope, counter kind and identity and still collapse.
    if observation.response_id:
        return ("response", observation.runtime, observation.response_id,
                observation.scope_kind, observation.counter_kind)
    if observation.counter_kind == "cumulative" and observation.turn_id:
        return ("counter", observation.runtime, observation.turn_id,
                observation.counter_name or "", observation.scope_kind)
    if observation.scope_id:
        return ("scope", observation.runtime, observation.scope_kind, observation.scope_id)
    return ("record", observation.runtime, observation.record, observation.observation_id)


def _metrics_equal(left: NormalizedUsage, right: NormalizedUsage) -> bool:
    fields = ("input_total", "input_uncached", "cache_read", "cache_write",
              "output_total", "reasoning", "total", "reported_total",
              "cost", "currency", "cost_basis")
    return all(getattr(left, name) == getattr(right, name) for name in fields)


def reconcile(observations: list[UsageObservation]) -> list[UsageRecord]:
    """Collapse proven duplicates; fold cumulative revisions; keep the rest.

    Grouping uses only native correlation identity. Equal-valued duplicates
    become one record; repeated cumulative snapshots of one counter become
    one record at the latest valid revision; same-identity absolute values
    that differ become one conflicted record each. Observations without any
    correlation identity stay ambiguous and never enter sums.
    """
    groups: dict[tuple, list[UsageObservation]] = {}
    for observation in observations:
        groups.setdefault(_record_key(observation), []).append(observation)

    records: list[UsageRecord] = []
    for key, members in groups.items():
        normals = [normalize_observation(member) for member in members]
        first = members[0]
        base = {
            "runtime": first.runtime,
            "scope_kind": first.scope_kind,
            "counter_kind": first.counter_kind,
            "operation": first.operation,
            "response_id": first.response_id,
            "turn_id": first.turn_id,
            "message_id": first.message_id,
            "scope_id": first.scope_id,
            "model": first.usage.model,
            "provider": first.provider,
            "counter_name": first.counter_name,
        }
        if len(members) == 1:
            records.append(UsageRecord(
                usage_id=f"{first.runtime}:rec:{len(records):04d}",
                observation_ids=(first.observation_id,),
                normalized=normals[0],
                revision=0,
                standing="firm" if key[0] != "record" else "ambiguous",
                native_subdivisions=first.native_subdivisions,
                diagnostics=(() if key[0] != "record"
                             else ("no_correlation_identity",)),
                **base,  # type: ignore[arg-type]
            ))
        elif all(_metrics_equal(normals[0], other) for other in normals[1:]):
            records.append(UsageRecord(
                usage_id=f"{first.runtime}:rec:{len(records):04d}",
                observation_ids=tuple(member.observation_id for member in members),
                normalized=normals[0],
                revision=0,
                standing="firm",
                native_subdivisions=tuple(
                    item for member in members for item in member.native_subdivisions
                ),
                diagnostics=("duplicate_observations_collapsed",) if len(members) > 1 else (),
                **base,  # type: ignore[arg-type]
            ))
        elif key[0] == "counter":
            latest = members[-1]
            records.append(UsageRecord(
                usage_id=f"{first.runtime}:rec:{len(records):04d}",
                observation_ids=tuple(member.observation_id for member in members),
                normalized=normals[-1],
                revision=len(members) - 1,
                standing="firm",
                native_subdivisions=latest.native_subdivisions,
                diagnostics=tuple(
                    ["cumulative_revision_latest_wins"]
                    + [f"superseded:{member.observation_id}" for member in members[:-1]]
                ),
                **{**base,
                   "response_id": latest.response_id,
                   "message_id": latest.message_id},
            ))
        else:
            for member, normal in zip(members, normals):
                records.append(UsageRecord(
                    usage_id=f"{first.runtime}:rec:{len(records):04d}",
                    observation_ids=(member.observation_id,),
                    normalized=normal,
                    revision=0,
                    standing="conflicted",
                    native_subdivisions=member.native_subdivisions,
                    diagnostics=tuple(
                        ["same_identity_differing_values"]
                        + [f"conflicts_with:{other.observation_id}"
                           for other in members if other is not member]
                    ),
                    **{**base,
                       "response_id": member.response_id,
                       "turn_id": member.turn_id,
                       "message_id": member.message_id,
                       "scope_id": member.scope_id,
                       "model": member.usage.model,
                       "provider": member.provider},
                ))
    return records


@dataclass(frozen=True)
class MetricSummary:
    """One metric's additive and authoritative answers side by side."""

    known_total: int | None = None
    detail_total: int | None = None
    interval_total: int | None = None
    complete_total: int | None = None
    reported_total: int | None = None
    unattributed: int | None = None
    coverage: Coverage = "unknown"
    contributing: tuple[str, ...] = ()


@dataclass(frozen=True)
class CostGroup:
    currency: str | None
    basis: CostBasis
    total: float
    records: tuple[str, ...]


@dataclass(frozen=True)
class CostSummary:
    known_total: float | None = None
    detail_total: float | None = None
    complete_total: float | None = None
    unattributed: float | None = None
    coverage: Coverage = "unknown"
    groups: tuple[CostGroup, ...] = ()
    contributing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelSlice:
    model: str | None
    provider: str | None
    input_total: int | None = None
    output_total: int | None = None
    total_tokens: int | None = None
    records: tuple[str, ...] = ()


@dataclass(frozen=True)
class UsageSummary:
    """Scoped arithmetic over explicitly selected native records.

    ``known_total`` sums only proven non-overlapping firm records;
    ``complete_total`` carries a native rollup only when a single firm
    rollup covers the declared scope; ``unattributed`` is their residual
    only when non-negative, otherwise the metric is conflicted. Missing
    stays missing; explicit zero stays zero.
    """

    session_ref: str | None
    runtime: str
    history_view: str
    coverage: Coverage
    input_total: MetricSummary
    cache_read: MetricSummary
    cache_write: MetricSummary
    output_total: MetricSummary
    reasoning: MetricSummary
    total_tokens: MetricSummary
    cost: CostSummary
    by_model: tuple[ModelSlice, ...]
    record_ids: tuple[str, ...]
    selected_ids: tuple[str, ...]
    ambiguous_ids: tuple[str, ...]
    conflicted_ids: tuple[str, ...]
    interval_ids: tuple[str, ...]
    observed_count: int
    record_count: int
    diagnostics: tuple[str, ...]


_METRIC_FIELDS = ("input_total", "cache_read", "cache_write",
                  "output_total", "reasoning")


def _metric_value(normalized: NormalizedUsage, field: str) -> int | None:
    value = getattr(normalized, field)
    return value if value is None or (isinstance(value, int) and not isinstance(value, bool)) else None


def _effective_total(normalized: NormalizedUsage) -> tuple[int | None, bool]:
    """Calculated total preferred; native reported total as fallback."""
    if normalized.total is not None:
        return normalized.total, False
    if normalized.reported_total is not None:
        return normalized.reported_total, True
    return None, False


def summarize(
    records: list[UsageRecord],
    *,
    session_ref: str | None = None,
    runtime: str = "unknown",
    history_view: str = "all_branches",
    observed_count: int | None = None,
) -> UsageSummary:
    """Build scope-aware totals over reconciled records.

    Turn groups prefer call/message details and set the overlapping turn
    rollup aside with a diagnostic; a turn known only through its
    cumulative rollup contributes as an interval amount. Session rollups
    never join the additive sums: a single firm rollup establishes
    ``complete_total`` for its scope, disagreeing rollups conflict.
    """
    observed = len(records) if observed_count is None else observed_count
    diagnostics: list[str] = []
    firm = [record for record in records if record.standing == "firm"]
    ambiguous = [record for record in records if record.standing == "ambiguous"]
    conflicted = [record for record in records if record.standing == "conflicted"]
    if ambiguous:
        diagnostics.append(f"ambiguous_excluded:{len(ambiguous)}")
    if conflicted:
        diagnostics.append(f"conflicted_excluded:{len(conflicted)}")

    details = [record for record in firm
               if record.scope_kind in {"call", "message"} and record.counter_kind == "absolute"]
    rollups = [record for record in firm if record.scope_kind == "session"]
    turn_cumulatives = [record for record in firm
                        if record not in details and record not in rollups
                        and record.counter_kind == "cumulative"]
    others = [record for record in firm
              if record not in details and record not in rollups
              and record not in turn_cumulatives]
    if others:
        diagnostics.append(f"unscoped_excluded:{len(others)}")

    groups: dict[str, list[UsageRecord]] = {}
    for record in details:
        groups.setdefault(record.turn_id or f"@record:{record.usage_id}", []).append(record)
    for record in turn_cumulatives:
        groups.setdefault(record.turn_id or f"@record:{record.usage_id}", []).append(record)

    @dataclass(frozen=True)
    class _GroupView:
        key: str
        details: tuple[UsageRecord, ...]
        interval: UsageRecord | None
        intervals_unresolved: bool = False

    group_views: list[_GroupView] = []
    for key, members in groups.items():
        member_details = tuple(record for record in members
                               if record.scope_kind in {"call", "message"})
        member_intervals = [record for record in members if record not in member_details]
        if not member_intervals:
            group_views.append(_GroupView(key, member_details, None))
            continue
        by_counter: dict[tuple, list[UsageRecord]] = {}
        for record in member_intervals:
            by_counter.setdefault(
                (record.turn_id, record.counter_name, record.scope_kind), []).append(record)
        if len(by_counter) > 1:
            diagnostics.append(
                "overlapping_counters_unresolved:"
                + ",".join(record.usage_id for record in member_intervals))
            group_views.append(_GroupView(key, member_details, None, True))
            continue
        ordered = next(iter(by_counter.values()))
        if len(ordered) > 1:
            diagnostics.append(
                "interval_revisions_folded:"
                + ",".join(record.usage_id for record in ordered))
        group_views.append(_GroupView(key, member_details, ordered[-1]))

    rollup: UsageRecord | None = None
    rollup_conflict = False
    if len(rollups) == 1:
        rollup = rollups[0]
    elif rollups:
        first_norm = rollups[0].normalized
        if all(_metrics_equal(first_norm, other.normalized) for other in rollups[1:]):
            rollup = rollups[0]
            diagnostics.append("duplicate_rollups_agree")
        else:
            rollup_conflict = True
            diagnostics.append("session_rollups_disagree")

    selected: list[UsageRecord] = []
    for view in group_views:
        selected.extend(view.details)
        if view.interval is not None:
            selected.append(view.interval)
    selected_ids = tuple(record.usage_id for record in selected)
    interval_ids = tuple(view.interval.usage_id for view in group_views
                         if view.interval is not None and not view.details)

    def build_metric(field: str) -> MetricSummary:
        # Per turn group the cumulative interval is authoritative for its
        # scope while details attribute it: the group contributes the
        # interval value once, details are attribution, and a positive
        # residual stays an unattributed interval amount. Details exceeding
        # their interval overlap something out of scope, so that turn is
        # excluded from this metric as conflicted.
        detail_total: int | None = None
        interval_total: int | None = None
        contributing: list[str] = []
        conflicted_here = False
        for view in group_views:
            if view.intervals_unresolved:
                continue
            detail_vals = [_metric_value(record.normalized, field)
                           for record in view.details]
            present = [value for value in detail_vals if value is not None]
            group_detail = sum(present) if present else None
            if view.interval is None:
                if group_detail is not None:
                    detail_total = (detail_total or 0) + group_detail
                    contributing.extend(record.usage_id for record in view.details
                                        if _metric_value(record.normalized, field) is not None)
                continue
            group_interval = _metric_value(view.interval.normalized, field)
            if group_interval is None:
                if group_detail is not None:
                    detail_total = (detail_total or 0) + group_detail
                    contributing.extend(record.usage_id for record in view.details
                                        if _metric_value(record.normalized, field) is not None)
                continue
            if group_detail is not None and group_detail > group_interval:
                conflicted_here = True
                diagnostics.append(f"turn_details_exceed_interval:{view.key}:{field}")
                continue
            residual = group_interval - (group_detail or 0)
            interval_total = (interval_total or 0) + residual
            contributing.append(view.interval.usage_id)
            if group_detail is not None:
                detail_total = (detail_total or 0) + group_detail
                contributing.extend(record.usage_id for record in view.details
                                    if _metric_value(record.normalized, field) is not None)
                if residual > 0:
                    diagnostics.append(
                        f"turn_residual_unattributed:{view.key}:{field}:{residual}")
        if detail_total is None and interval_total is None:
            known: int | None = None
        else:
            known = (detail_total or 0) + (interval_total or 0)
        contributing_ids = tuple(contributing)
        reported = _metric_value(rollup.normalized, field) if rollup is not None else None
        complete = reported if (rollup is not None and not rollup_conflict) else None
        unattributed: int | None = None
        coverage: Coverage
        if runtime in UNSUPPORTED_RUNTIMES:
            coverage = "unsupported"
        elif observed == 0:
            coverage = "unknown"
        elif conflicted_here:
            coverage = "conflicted"
        elif rollup_conflict:
            coverage = "conflicted"
            complete = None
        elif complete is not None and known is not None:
            residual = complete - known
            if residual < 0:
                coverage = "conflicted"
                complete = None
                diagnostics.append(f"details_exceed_rollup:{field}")
            else:
                unattributed = residual
                coverage = "partial"
                diagnostics.append(f"subset_compatibility_unproven:{field}")
        elif complete is not None or known is not None:
            coverage = "partial"
        else:
            coverage = "unknown"
        if (coverage == "partial" and any(
                record.normalized.gaps for record in selected
                if record.usage_id in contributing_ids)):
            diagnostics.append(f"partial_addends:{field}")
        return MetricSummary(
            known_total=known,
            detail_total=detail_total,
            interval_total=interval_total,
            complete_total=complete,
            reported_total=reported,
            unattributed=unattributed,
            coverage=coverage if observed or runtime in UNSUPPORTED_RUNTIMES else "unknown",
            contributing=contributing_ids,
        )

    def build_total() -> MetricSummary:
        detail_total: int | None = None
        interval_total: int | None = None
        contributing: list[str] = []
        conflicted_here = False
        fell_back = False
        for view in group_views:
            if view.intervals_unresolved:
                continue
            detail_vals: list[tuple[int, str]] = []
            for record in view.details:
                value, used_reported = _effective_total(record.normalized)
                if value is not None:
                    detail_vals.append((value, record.usage_id))
                    fell_back = fell_back or used_reported
            group_detail = sum(value for value, _ in detail_vals) if detail_vals else None
            if view.interval is None:
                if group_detail is not None:
                    detail_total = (detail_total or 0) + group_detail
                    contributing.extend(rid for _, rid in detail_vals)
                continue
            group_interval, interval_reported = _effective_total(view.interval.normalized)
            fell_back = fell_back or interval_reported
            if group_interval is None:
                if group_detail is not None:
                    detail_total = (detail_total or 0) + group_detail
                    contributing.extend(rid for _, rid in detail_vals)
                continue
            if group_detail is not None and group_detail > group_interval:
                conflicted_here = True
                diagnostics.append(f"turn_details_exceed_interval:{view.key}:total_tokens")
                continue
            residual = group_interval - (group_detail or 0)
            interval_total = (interval_total or 0) + residual
            contributing.append(view.interval.usage_id)
            if group_detail is not None:
                detail_total = (detail_total or 0) + group_detail
                contributing.extend(rid for _, rid in detail_vals)
                if residual > 0:
                    diagnostics.append(
                        f"turn_residual_unattributed:{view.key}:total_tokens:{residual}")
        known = ((detail_total or 0) + (interval_total or 0)
                 if detail_total is not None or interval_total is not None else None)
        contributing_ids = tuple(contributing)
        reported = rollup.normalized.reported_total if rollup is not None else None
        if reported is None and rollup is not None:
            reported = rollup.normalized.total
        complete = reported if (rollup is not None and not rollup_conflict) else None
        unattributed = None
        if runtime in UNSUPPORTED_RUNTIMES:
            coverage: Coverage = "unsupported"
        elif conflicted_here:
            coverage = "conflicted"
        elif rollup_conflict:
            coverage = "conflicted"
            complete = None
        elif complete is not None and known is not None:
            residual = complete - known
            if residual < 0:
                coverage = "conflicted"
                complete = None
                diagnostics.append("details_exceed_rollup:total_tokens")
            else:
                unattributed = residual
                coverage = "partial"
                diagnostics.append("subset_compatibility_unproven:total_tokens")
        elif complete is not None or known is not None:
            coverage = "partial"
        else:
            coverage = "unknown"
        if fell_back:
            diagnostics.append("total_native_reported_used")
        return MetricSummary(
            known_total=known,
            detail_total=detail_total,
            interval_total=interval_total,
            complete_total=complete,
            reported_total=reported,
            unattributed=unattributed,
            coverage=coverage,
            contributing=contributing_ids,
        )

    metrics = {field: build_metric(field) for field in _METRIC_FIELDS}
    total_summary = build_total()

    cost_groups: dict[tuple[str | None, CostBasis], list[UsageRecord]] = {}
    for record in selected:
        if record.normalized.cost is None:
            continue
        cost_groups.setdefault(
            (record.normalized.currency, record.normalized.cost_basis), []).append(record)
    group_list: list[CostGroup] = []
    for (currency, basis), members in sorted(cost_groups.items(), key=lambda item: str(item[0])):
        group_list.append(CostGroup(
            currency=currency,
            basis=basis,
            total=sum(member.normalized.cost or 0.0 for member in members),
            records=tuple(member.usage_id for member in members),
        ))
    rollup_cost = rollup.normalized.cost if rollup is not None else None
    if len(group_list) == 1 and not rollup_conflict:
        cost_known: float | None = group_list[0].total
        cost_complete = rollup_cost
        cost_unattributed: float | None = None
        if cost_complete is not None and cost_known is not None:
            residual_cost = cost_complete - cost_known
            if residual_cost < 0:
                cost_coverage: Coverage = "conflicted"
                cost_complete = None
                diagnostics.append("details_exceed_rollup:cost")
            else:
                cost_unattributed = residual_cost
                cost_coverage = "partial"
        elif cost_complete is not None or cost_known is not None:
            cost_coverage = "partial"
        else:
            cost_coverage = "unknown"
    else:
        if len(group_list) > 1:
            diagnostics.append("cost_basis_groups_kept_separate")
        cost_known = None
        cost_complete = rollup_cost if not rollup_conflict else None
        cost_unattributed = None
        if runtime in UNSUPPORTED_RUNTIMES:
            cost_coverage = "unsupported"
        elif rollup_conflict and not group_list:
            cost_coverage = "conflicted"
        elif cost_complete is not None:
            cost_coverage = "partial"
        else:
            cost_coverage = "unknown"
    if runtime in UNSUPPORTED_RUNTIMES:
        cost_coverage = "unsupported"
    cost_summary = CostSummary(
        known_total=cost_known,
        detail_total=cost_known,
        complete_total=cost_complete,
        unattributed=cost_unattributed,
        coverage=cost_coverage,
        groups=tuple(group_list),
        contributing=tuple(record.usage_id for record in selected
                            if record.normalized.cost is not None),
    )

    model_groups: dict[tuple[str | None, str | None], list[UsageRecord]] = {}
    for record in selected:
        model_groups.setdefault((record.model, record.provider), []).append(record)
    slices: list[ModelSlice] = []
    def _sum(members: list[UsageRecord], field: str) -> int | None:
        vals = [_metric_value(member.normalized, field) for member in members]
        present = [value for value in vals if value is not None]
        return sum(present) if present else None
    for (model, provider), members in sorted(model_groups.items(), key=lambda item: str(item[0])):
        total_vals: list[int] = []
        for member in members:
            value, _ = _effective_total(member.normalized)
            if value is not None:
                total_vals.append(value)
        slices.append(ModelSlice(
            model=model,
            provider=provider,
            input_total=_sum(members, "input_total"),
            output_total=_sum(members, "output_total"),
            total_tokens=sum(total_vals) if total_vals else None,
            records=tuple(member.usage_id for member in members),
        ))

    coverages = [metrics[field].coverage for field in _METRIC_FIELDS]
    coverages.append(total_summary.coverage)
    if runtime in UNSUPPORTED_RUNTIMES:
        overall: Coverage = "unsupported"
    elif observed == 0:
        overall = "not_recorded"
    elif conflicted or rollup_conflict or "conflicted" in coverages:
        overall = "conflicted"
    elif (rollup is not None and not ambiguous and not conflicted
          and not any(record.normalized.gaps for record in selected)
          and total_summary.unattributed == 0 and total_summary.complete_total is not None):
        overall = "complete"
    else:
        overall = "partial"
    if observed == 0 and runtime not in UNSUPPORTED_RUNTIMES:
        for field in _METRIC_FIELDS:
            metrics[field] = MetricSummary(coverage="not_recorded")
        total_summary = MetricSummary(coverage="not_recorded")

    return UsageSummary(
        session_ref=session_ref,
        runtime=runtime,
        history_view=history_view,
        coverage=overall,
        input_total=metrics["input_total"],
        cache_read=metrics["cache_read"],
        cache_write=metrics["cache_write"],
        output_total=metrics["output_total"],
        reasoning=metrics["reasoning"],
        total_tokens=total_summary,
        cost=cost_summary,
        by_model=tuple(slices),
        record_ids=tuple(record.usage_id for record in records),
        selected_ids=selected_ids,
        ambiguous_ids=tuple(record.usage_id for record in ambiguous),
        conflicted_ids=tuple(record.usage_id for record in conflicted),
        interval_ids=tuple(interval_ids),
        observed_count=observed,
        record_count=len(records),
        diagnostics=tuple(diagnostics),
    )


def summarize_observations(
    observations: list[UsageObservation],
    *,
    session_ref: str | None = None,
    runtime: str = "unknown",
    history_view: str = "all_branches",
) -> UsageSummary:
    """Reconcile observations and summarize them in one step (U2 convenience)."""
    return summarize(
        reconcile(observations),
        session_ref=session_ref,
        runtime=runtime,
        history_view=history_view,
        observed_count=len(observations),
    )


USAGE_SCHEMA_ID = "sesskit.usage/v1"
_MODEL_SLICE_LIMIT = 50


@dataclass(frozen=True)
class UsageCapabilities:
    """Detailed usage support per runtime (extends the coarse ``usage_data`` flag)."""

    runtime: str
    supported: bool
    source_variants: tuple[str, ...] = ()
    metric_families: tuple[str, ...] = ()
    entity_granularities: tuple[str, ...] = ()
    counter_kinds: tuple[str, ...] = ()
    branch_views: tuple[str, ...] = ()
    auxiliary_support: bool = False
    incremental_support: bool = False


_USAGE_CAPABILITIES: dict[str, UsageCapabilities] = {
    "claude": UsageCapabilities(
        "claude", True,
        source_variants=("message.usage",),
        metric_families=("input_text", "cache_read", "cache_write", "output"),
        entity_granularities=("message",),
        counter_kinds=("absolute",),
        branch_views=("all_branches",),
        auxiliary_support=False,
        incremental_support=False,
    ),
    "codex": UsageCapabilities(
        "codex", True,
        source_variants=("token_usage_record", "token_count.info"),
        metric_families=("input", "cached_input", "output", "reasoning", "total"),
        entity_granularities=("call", "turn"),
        counter_kinds=("absolute", "cumulative"),
        branch_views=("all_branches",),
        auxiliary_support=False,
        incremental_support=False,
    ),
    "opencode": UsageCapabilities(
        "opencode", True,
        source_variants=("message.tokens",),
        metric_families=("input", "cache_read", "cache_write", "output", "reasoning", "cost"),
        entity_granularities=("message",),
        counter_kinds=("absolute",),
        branch_views=("all_branches",),
        auxiliary_support=False,
        incremental_support=False,
    ),
    "pi": UsageCapabilities(
        "pi", True,
        source_variants=("message.usage", "compaction.usage", "branch_summary.usage", "toolResult.usage"),
        metric_families=("input", "cache_read", "cache_write", "output", "reasoning", "total", "cost"),
        entity_granularities=("message",),
        counter_kinds=("absolute", "unknown"),
        branch_views=("all_branches", "active_branch"),
        auxiliary_support=True,
        incremental_support=False,
    ),
}


def usage_capabilities(runtime: str) -> UsageCapabilities:
    """Detailed usage support for one runtime; unsupported runtimes report False."""
    capabilities = _USAGE_CAPABILITIES.get(runtime)
    if capabilities is None:
        return UsageCapabilities(runtime, False)
    return capabilities


@dataclass(frozen=True)
class UsageSelection:
    """Typed record selector: native identities only, never a predicate callback.

    ``turn_ids`` and ``message_ids`` are mutually exclusive. Message ids
    match message- or response-linked records. SessKit never invents a
    task, benchmark or attempt model on top of these identities.
    """

    turn_ids: tuple[str, ...] = ()
    message_ids: tuple[str, ...] = ()
    include_auxiliary: bool = True

    def __post_init__(self) -> None:
        if self.turn_ids and self.message_ids:
            raise ValueError("turn_ids and message_ids are mutually exclusive")


@dataclass(frozen=True)
class UsageSnapshot:
    """One session's preserved usage readings plus their reconciled summary.

    ``state`` follows the shared load states: a readable history without
    recorded usage is ``empty`` with ``not_recorded`` coverage, never a
    zero-cost session; failure is never empty success. The snapshot is a
    projection container over shared history identities, not a second
    session hierarchy.
    """

    state: LoadState
    runtime: str
    session_ref: str | None
    history_view: str
    watermark: str | None
    observations: tuple[UsageObservation, ...]
    records: tuple[UsageRecord, ...]
    summary: UsageSummary | None
    entries_read: int
    observed_count: int
    diagnostics: tuple[str, ...] = ()


def _file_watermark(path: str) -> str | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def _read_jsonl_rows(path: str) -> list[tuple[int, dict]]:
    rows: list[tuple[int, dict]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for lineno, line in enumerate(file, 1):
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    rows.append((lineno, entry))
    except OSError:
        pass
    return rows


def _load_jsonl_usage(path: str, runtime: str) -> tuple[list[UsageObservation], int, str | None]:
    rows = _read_jsonl_rows(path)
    if runtime == "claude":
        return collect_claude_observations(rows), len(rows), _file_watermark(path)
    return collect_codex_observations(rows), len(rows), _file_watermark(path)


def _load_pi_usage(path: str, history_view: str) -> tuple[list[UsageObservation], int, str | None]:
    from sesskit.activity import _pi_compactions
    from sesskit.parsers import pi as scan_pi

    entries = scan_pi.read_entries(path)
    watermark = _file_watermark(path)
    if history_view == "active_branch":
        branch = scan_pi.active_messages(entries)
        # _pi_compactions keeps only compaction entries feeding this branch.
        wanted = {id(item) for item in branch}
        wanted.update(id(entry) for entry in _pi_compactions(entries, branch).values())
        positions = {id(item): index for index, item in enumerate(entries)}
        scoped = sorted(
            (item for item in entries if id(item) in wanted),
            key=lambda item: positions[id(item)],
        )
        return collect_pi_observations(scoped), len(entries), watermark
    return collect_pi_observations(entries), len(entries), watermark


def _load_opencode_usage(path: str, session_id: str) -> tuple[list[UsageObservation], list[dict] | None, int, str | None]:
    """Read v1/v2 message usage; stored session aggregates are not present in this schema."""
    import sqlite3

    from sesskit.parsers import opencode as scan_opencode

    conn = scan_opencode.connect_ro(path)
    if conn is None:
        return [], None, 0, None
    try:
        tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        messages: list[dict] = []
        count = 0
        if {"session", "message", "part"} <= tables:
            try:
                has_session = conn.execute(
                    "SELECT 1 FROM session WHERE id = ? LIMIT 1", (session_id,)).fetchone()
            except sqlite3.Error:
                has_session = None
            if has_session is not None:
                try:
                    rows = conn.execute(
                        "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created ASC, id ASC",
                        (session_id,)).fetchall()
                except sqlite3.Error:
                    return [], None, 0, _file_watermark(path)
                for row in rows:
                    count += 1
                    try:
                        data = json.loads(row[1])
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
                    if isinstance(data, dict):
                        data = dict(data)
                        data.setdefault("id", row[0])
                        messages.append(data)
                return collect_opencode_observations(messages), None, count, _file_watermark(path)
        if "session_message" in tables:
            try:
                rows = conn.execute(
                    "SELECT id, data FROM session_message WHERE session_id = ? ORDER BY seq ASC, id ASC",
                    (session_id,)).fetchall()
            except sqlite3.Error:
                return [], None, 0, _file_watermark(path)
            for row in rows:
                count += 1
                try:
                    data = json.loads(row[1])
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
                if isinstance(data, dict):
                    data = dict(data)
                    data.setdefault("id", row[0])
                    messages.append(data)
            return collect_opencode_observations(messages), None, count, _file_watermark(path)
        return [], None, 0, _file_watermark(path)
    finally:
        conn.close()


def load_usage(session: dict, *, history_view: str = "all_branches") -> UsageSnapshot:
    """Load preserved usage observations, records and summary for one session dict.

    ``history_view`` is ``all_branches`` (default, the session's own
    operations) or ``active_branch`` (Pi only; other runtimes fall back to
    the full history with a diagnostic). Unknown runtimes and runtimes
    without a verified usage mapping yield ``unsupported``; missing or
    unreadable history yields ``unavailable``. A readable history without
    recorded usage is ``empty`` with ``not_recorded`` coverage.
    """
    runtime = str(session.get("source") or "")
    session_id = str(session.get("id") or "")
    session_ref = f"{runtime}:{session_id}" if runtime and session_id else None
    if history_view not in {"all_branches", "active_branch"}:
        raise ValueError(f"unsupported history view: {history_view!r}")
    if runtime in UNSUPPORTED_RUNTIMES or runtime not in SUPPORTED_RUNTIMES:
        summary = summarize([], session_ref=session_ref, runtime=runtime or "unknown",
                            history_view=history_view, observed_count=0)
        return UsageSnapshot("unsupported", runtime or "unknown", session_ref,
                             history_view, None, (), (), summary, 0, 0,
                             ("runtime_usage_unsupported",))
    path = str(session.get("path") or "")
    if not path or not os.path.isfile(path):
        summary = summarize([], session_ref=session_ref, runtime=runtime,
                            history_view=history_view, observed_count=0)
        return UsageSnapshot("unavailable", runtime, session_ref, history_view,
                             None, (), (), summary, 0, 0, ("history_unreadable",))
    diagnostics: list[str] = []
    observations: list[UsageObservation] = []
    entries_read = 0
    watermark: str | None = None
    if runtime in {"claude", "codex"}:
        try:
            observations, entries_read, watermark = _load_jsonl_usage(path, runtime)
        except OSError:
            observations, entries_read, watermark = [], 0, None
        if history_view == "active_branch":
            diagnostics.append("branch_views_unsupported_fallback_all_branches")
    elif runtime == "pi":
        try:
            observations, entries_read, watermark = _load_pi_usage(path, history_view)
        except OSError:
            observations, entries_read, watermark = [], 0, None
    elif runtime == "opencode":
        if history_view == "active_branch":
            diagnostics.append("branch_views_unsupported_fallback_all_branches")
        try:
            observations, _rollup, entries_read, watermark = _load_opencode_usage(path, session_id)
        except OSError:
            observations, entries_read, watermark = [], 0, None
    else:
        summary = summarize([], session_ref=session_ref, runtime=runtime,
                            history_view=history_view, observed_count=0)
        return UsageSnapshot("unsupported", runtime, session_ref, history_view,
                             None, (), (), summary, 0, 0, ("runtime_usage_unsupported",))
    if watermark is None:
        summary = summarize([], session_ref=session_ref, runtime=runtime,
                            history_view=history_view, observed_count=0)
        return UsageSnapshot("unavailable", runtime, session_ref, history_view,
                             None, (), (), summary, entries_read, 0, ("history_unreadable",))
    records = reconcile(observations)
    summary = summarize(records, session_ref=session_ref, runtime=runtime,
                        history_view=history_view, observed_count=len(observations))
    state: LoadState = "available" if observations else "empty"
    return UsageSnapshot(state, runtime, session_ref, history_view, watermark,
                         tuple(observations), tuple(records), summary,
                         entries_read, len(observations), tuple(diagnostics))


def summarize_usage(snapshot: UsageSnapshot, *,
                    selection: UsageSelection | None = None) -> UsageSummary:
    """Summarize a snapshot, optionally narrowed to native turn/message ids."""
    if selection is None:
        assert snapshot.summary is not None
        return snapshot.summary
    records = list(snapshot.records)
    if selection.turn_ids:
        wanted = set(selection.turn_ids)
        records = [record for record in records if record.turn_id in wanted]
    else:
        wanted_messages = set(selection.message_ids)
        records = [record for record in records
                   if record.message_id in wanted_messages
                   or (record.response_id is not None and record.response_id in wanted_messages)]
    return summarize(records, session_ref=snapshot.session_ref, runtime=snapshot.runtime,
                     history_view=snapshot.history_view, observed_count=snapshot.observed_count)


def _usage_numbers(usage: Usage) -> dict:
    return {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_tokens,
        "reasoning": usage.reasoning_tokens,
        "total": usage.total_tokens,
        "cost": usage.cost,
    }


def observation_to_dict(observation: UsageObservation, *, include_native: bool = False) -> dict:
    payload: dict = {
        "observation_id": observation.observation_id,
        "runtime": observation.runtime,
        "record": observation.record,
        "scope": {"kind": observation.scope_kind, "id": observation.scope_id},
        "counter": {"kind": observation.counter_kind, "name": observation.counter_name},
        "operation": observation.operation,
        "operation_detail": observation.operation_detail,
        "correlation": {
            "request_id": observation.request_id,
            "response_id": observation.response_id,
            "turn_id": observation.turn_id,
            "message_id": observation.message_id,
        },
        "model": observation.usage.model,
        "provider": observation.provider,
        "usage": _usage_numbers(observation.usage),
        "cost_basis": observation.cost_basis,
        "quality": observation.quality,
        "evidence": {
            "origin": observation.evidence.origin,
            "field": observation.evidence.field,
            "record": observation.evidence.record,
        },
    }
    if include_native:
        payload["native_subdivisions"] = [
            {"name": name, "value": value}
            for name, value in observation.native_subdivisions
        ]
    return payload


def record_to_dict(record: UsageRecord, *, include_native: bool = False) -> dict:
    normalized = record.normalized
    payload: dict = {
        "usage_id": record.usage_id,
        "runtime": record.runtime,
        "observation_ids": list(record.observation_ids),
        "scope": {"kind": record.scope_kind, "id": record.scope_id},
        "counter": {"kind": record.counter_kind, "name": record.counter_name},
        "operation": record.operation,
        "correlation": {
            "response_id": record.response_id,
            "turn_id": record.turn_id,
            "message_id": record.message_id,
        },
        "model": record.model,
        "provider": record.provider,
        "standing": record.standing,
        "revision": record.revision,
        "normalized": {
            "input_total": normalized.input_total,
            "input_uncached": normalized.input_uncached,
            "cache_read": normalized.cache_read,
            "cache_write": normalized.cache_write,
            "output_total": normalized.output_total,
            "reasoning": normalized.reasoning,
            "total": normalized.total,
            "reported_total": normalized.reported_total,
            "cost": normalized.cost,
            "currency": normalized.currency,
            "cost_basis": normalized.cost_basis,
            "gaps": list(normalized.gaps),
            "derivation": list(normalized.derivation),
        },
        "diagnostics": list(record.diagnostics),
    }
    if include_native:
        payload["native_subdivisions"] = [
            {"name": name, "value": value}
            for name, value in record.native_subdivisions
        ]
    return payload


def metric_summary_to_dict(summary: MetricSummary) -> dict:
    return {
        "known_total": summary.known_total,
        "detail_total": summary.detail_total,
        "interval_total": summary.interval_total,
        "complete_total": summary.complete_total,
        "reported_total": summary.reported_total,
        "unattributed": summary.unattributed,
        "coverage": summary.coverage,
        "contributing": list(summary.contributing),
    }


def cost_summary_to_dict(summary: CostSummary) -> dict:
    return {
        "known_total": summary.known_total,
        "detail_total": summary.detail_total,
        "complete_total": summary.complete_total,
        "unattributed": summary.unattributed,
        "coverage": summary.coverage,
        "groups": [
            {"currency": group.currency, "basis": group.basis,
             "total": group.total, "records": list(group.records)}
            for group in summary.groups
        ],
        "contributing": list(summary.contributing),
    }


def summary_to_dict(summary: UsageSummary) -> dict:
    slices = summary.by_model[:_MODEL_SLICE_LIMIT]
    return {
        "session_ref": summary.session_ref,
        "runtime": summary.runtime,
        "history_view": summary.history_view,
        "coverage": summary.coverage,
        "metrics": {
            "input_total": metric_summary_to_dict(summary.input_total),
            "cache_read": metric_summary_to_dict(summary.cache_read),
            "cache_write": metric_summary_to_dict(summary.cache_write),
            "output_total": metric_summary_to_dict(summary.output_total),
            "reasoning": metric_summary_to_dict(summary.reasoning),
            "total_tokens": metric_summary_to_dict(summary.total_tokens),
        },
        "cost": cost_summary_to_dict(summary.cost),
        "by_model": [
            {"model": item.model, "provider": item.provider,
             "input_total": item.input_total, "output_total": item.output_total,
             "total_tokens": item.total_tokens, "records": list(item.records)}
            for item in slices
        ],
        "models_omitted": max(0, len(summary.by_model) - len(slices)),
        "record_ids": list(summary.record_ids),
        "selected_ids": list(summary.selected_ids),
        "ambiguous_ids": list(summary.ambiguous_ids),
        "conflicted_ids": list(summary.conflicted_ids),
        "interval_ids": list(summary.interval_ids),
        "observed_count": summary.observed_count,
        "record_count": summary.record_count,
        "diagnostics": list(summary.diagnostics),
    }


def snapshot_to_dict(snapshot: UsageSnapshot, *, include_native: bool = False) -> dict:
    return {
        "schema": USAGE_SCHEMA_ID,
        "session_ref": snapshot.session_ref,
        "runtime": snapshot.runtime,
        "state": snapshot.state,
        "history_view": snapshot.history_view,
        "watermark": snapshot.watermark is not None,
        "entries_read": snapshot.entries_read,
        "summary": summary_to_dict(snapshot.summary) if snapshot.summary is not None else None,
        "diagnostics": list(snapshot.diagnostics),
        "native_subdivisions_omitted": (
            0 if include_native else sum(
                1 for observation in snapshot.observations if observation.native_subdivisions)
        ),
    }


def encode_usage_cursor(watermark: str, offset: int) -> str:
    raw = json.dumps({"v": 1, "gen": watermark, "off": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_usage_cursor(token: str, watermark: str | None) -> int:
    """Return the page offset, or raise ValueError("stale"|"malformed")."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("malformed") from None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("malformed")
    if not isinstance(payload.get("off"), int) or payload["off"] < 0:
        raise ValueError("malformed")
    if watermark is None or payload.get("gen") != watermark:
        raise ValueError("stale")
    return int(payload["off"])


__all__ = [
    "SUPPORTED_RUNTIMES",
    "UNSUPPORTED_RUNTIMES",
    "USAGE_SCHEMA_ID",
    "CostBasis",
    "CostGroup",
    "CostSummary",
    "CounterKind",
    "Coverage",
    "MetricSummary",
    "ModelSlice",
    "NormalizedUsage",
    "OperationKind",
    "RecordStanding",
    "ScopeKind",
    "UsageCapabilities",
    "UsageObservation",
    "UsageRecord",
    "UsageSelection",
    "UsageSnapshot",
    "UsageSummary",
    "ValueQuality",
    "collect_claude_observations",
    "collect_codex_observations",
    "collect_opencode_observations",
    "collect_pi_observations",
    "decode_usage_cursor",
    "describe_support",
    "encode_usage_cursor",
    "load_usage",
    "metric_summary_to_dict",
    "normalize_observation",
    "observation_to_dict",
    "reconcile",
    "record_to_dict",
    "snapshot_to_dict",
    "summarize",
    "summarize_observations",
    "summarize_usage",
    "summary_to_dict",
    "usage_capabilities",
]
