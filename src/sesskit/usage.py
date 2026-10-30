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

from dataclasses import dataclass
from typing import Literal

from sesskit.models import Evidence, Usage

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
            return None
        fields = {
            "input_tokens": _pi_int(usage.get("input")) if isinstance(usage, dict) else None,
            "output_tokens": _pi_int(usage.get("output")) if isinstance(usage, dict) else None,
            "cache_read_tokens": _pi_int(usage.get("cacheRead")) if isinstance(usage, dict) else None,
            "cache_write_tokens": _pi_int(usage.get("cacheWrite")) if isinstance(usage, dict) else None,
            "reasoning_tokens": _pi_int(usage.get("reasoning")) if isinstance(usage, dict) else None,
            "total_tokens": _pi_int(usage.get("totalTokens")) if isinstance(usage, dict) else None,
        }
        cost: float | None = None
        if isinstance(usage, dict):
            details = usage.get("cost")
            if isinstance(details, dict):
                total = details.get("total")
                if isinstance(total, (int, float)) and not isinstance(total, bool) and total >= 0:
                    cost = float(total)
        if all(value is None for value in fields.values()) and cost is None and not isinstance(model, str):
            return None
        return Usage(
            _native_evidence(record, "usage"),
            model.strip() if isinstance(model, str) and model.strip() else None,
            cost=cost,
            **fields,  # type: ignore[arg-type]
        )

    for position, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            continue
        entry_type = str(item.get("type") or "")
        record = f"entry:{item.get('id')}" if isinstance(item.get("id"), str) else f"entry:#{position}"
        message = item.get("message")
        if entry_type == "message" and isinstance(message, dict):
            role = message.get("role")
            parsed = usage_from(message, record)
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
            parsed = usage_from(carrier, record)
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
            ))
    return observations


SUPPORTED_RUNTIMES = ("claude", "codex", "opencode", "pi")
UNSUPPORTED_RUNTIMES = ("cursor", "kimi")


def describe_support(runtime: str) -> Literal["supported", "unsupported"]:
    """Capability probe for usage observations: no history access needed."""
    if runtime in SUPPORTED_RUNTIMES:
        return "supported"
    return "unsupported"


__all__ = [
    "SUPPORTED_RUNTIMES",
    "UNSUPPORTED_RUNTIMES",
    "CostBasis",
    "CounterKind",
    "OperationKind",
    "ScopeKind",
    "UsageObservation",
    "ValueQuality",
    "collect_claude_observations",
    "collect_codex_observations",
    "collect_opencode_observations",
    "collect_pi_observations",
    "describe_support",
]
