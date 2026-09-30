"""Cross-runtime session data models for SessKit."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict

EvidenceOrigin = Literal["native", "inferred", "unknown"]
OutcomeStatus = Literal["done", "pending", "aborted", "unknown"]
ToolResultStatus = Literal["ok", "error", "unknown"]


class _SessionInfoRequired(TypedDict):
    """Unified session metadata every runtime parser must return."""

    source: str
    id: str
    short_id: str
    cwd: str
    cwd_display: str
    mtime: float
    display_time: str
    time_source: str
    event_time: float | None
    file_mtime: float
    size_bytes: int
    size_kb: float
    native_title: str | None
    fallback_title: str
    status_tag: str
    live: bool
    pid: int | None
    first_user_msg: str
    last_user_msg: str
    last_agent_msg: str
    path: str


class SessionInfo(_SessionInfoRequired, total=False):
    """Optional fields beyond the required scan payload."""

    # 一轮结束的稳定标识：同一会话里每一轮结束唯一、重启可重算。
    # 终端态（已完成/已中断）才有值；进行中/未知为空串。供消费者完成通知去重。
    completion_id: str
    thread_source: str | None
    # Native id this session was continued into (Claude `continued-in`
    # forward pointer). Set only when the target history file exists on
    # disk; absent means no known continuation. Listings keep the record
    # with this flag; hiding or merging is host policy.
    superseded_by: str


@dataclass(frozen=True)
class Evidence:
    """Origin and optional native location for a normalized claim."""

    origin: EvidenceOrigin
    field: str | None = None
    record: str | None = None

    def __post_init__(self) -> None:
        if self.origin not in {"native", "inferred", "unknown"}:
            raise ValueError(f"unsupported evidence origin: {self.origin!r}")


ErrorScope = Literal["tool", "turn", "session"]


@dataclass(frozen=True)
class AgentError:
    """Structured failure evidence kept separate from assistant-authored text."""

    kind: str
    message: str
    evidence: Evidence
    code: str | None = None
    retryable: bool | None = None
    scope: ErrorScope | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("error kind must not be empty")
        if not self.message.strip():
            raise ValueError("error message must not be empty")
        if self.code is not None and not self.code.strip():
            raise ValueError("error code must be omitted when unavailable")
        if self.scope is not None and self.scope not in {"tool", "turn", "session"}:
            raise ValueError(f"unsupported error scope: {self.scope!r}")
        if self.http_status is not None and (
            isinstance(self.http_status, bool)
            or not 100 <= self.http_status <= 599
        ):
            raise ValueError("http_status carries only native HTTP codes")


@dataclass(frozen=True)
class ToolResultOutcome:
    """Normalized tool result status and whether that status is evidenced."""

    status: ToolResultStatus
    evidence: Evidence

    def __post_init__(self) -> None:
        if self.status not in {"ok", "error", "unknown"}:
            raise ValueError(f"unsupported tool result status: {self.status!r}")
        if self.status != "unknown" and self.evidence.origin == "unknown":
            raise ValueError("a known tool result status requires native or inferred evidence")


@dataclass(frozen=True)
class SessionOutcome:
    """Historical turn outcome; this does not describe current process liveness."""

    status: OutcomeStatus
    evidence: Evidence
    error: AgentError | None = None

    def __post_init__(self) -> None:
        if self.status not in {"done", "pending", "aborted", "unknown"}:
            raise ValueError(f"unsupported session outcome: {self.status!r}")
        if self.status != "unknown" and self.evidence.origin == "unknown":
            raise ValueError("a known session outcome requires native or inferred evidence")
        if self.status == "done" and self.error is not None:
            raise ValueError("a completed outcome cannot carry an agent error")


LoadState = Literal["available", "empty", "unavailable", "unsupported"]
ActivityEventType = Literal[
    "user_message", "assistant_message", "thinking", "tool_call", "tool_result",
    "compaction", "lifecycle",
]
MessageOrigin = Literal["human", "injected", "system", "unknown"]
RelationKind = Literal["subagent", "fork", "resume", "handoff", "continuation", "unknown"]
RequestPurpose = Literal[
    "question", "permission", "plan_approval", "elicitation",
    "elicitation_form", "elicitation_url", "unknown",
]
RequestResolution = Literal[
    "pending", "answered", "approved", "denied", "declined",
    "dismissed", "expired", "unknown",
]
DecidedBy = Literal["human", "policy", "unknown"]


@dataclass(frozen=True)
class QuestionOption:
    """One selectable option within a structured question."""

    label: str
    description: str | None = None


@dataclass(frozen=True)
class QuestionItem:
    """One question within a possibly grouped interaction request."""

    prompt: str = ""
    title: str | None = None
    options: tuple[QuestionOption, ...] = ()
    multi_select: bool = False
    free_text: bool = False


@dataclass(frozen=True)
class AnswerRecord:
    """A recorded answer linked to one question item."""

    question_index: int = 0
    text: str = ""
    selected: tuple[str, ...] = ()


@dataclass(frozen=True)
class InteractionRequest:
    """Structured agent-to-user request kept separate from plain conversation text."""

    purpose: RequestPurpose
    evidence: Evidence
    resolution: RequestResolution = "unknown"
    resolution_evidence: Evidence | None = None
    request_id: str | None = None
    tool_call_id: str | None = None
    questions: tuple[QuestionItem, ...] = ()
    answers: tuple[AnswerRecord, ...] = ()
    decided_by: DecidedBy = "unknown"
    is_secret: bool | None = None
    is_blocking: bool | None = None

    def __post_init__(self) -> None:
        if self.purpose not in {
            "question", "permission", "plan_approval", "elicitation",
            "elicitation_form", "elicitation_url", "unknown",
        }:
            raise ValueError(f"unsupported request purpose: {self.purpose!r}")
        if self.resolution not in {
            "pending", "answered", "approved", "denied", "declined",
            "dismissed", "expired", "unknown",
        }:
            raise ValueError(f"unsupported request resolution: {self.resolution!r}")
        if self.decided_by not in {"human", "policy", "unknown"}:
            raise ValueError(f"unsupported decided_by: {self.decided_by!r}")


@dataclass(frozen=True)
class SessionRelation:
    """One native link between this session and another history unit.

    ``kind`` names the relationship when native history states it
    (a fork record, an explicit resume chain, a continuation pointer,
    a handoff reference, a child-agent delegation); otherwise it stays
    ``unknown``. ``target`` is the other side's native session id when
    persisted, else None.
    """

    kind: RelationKind
    evidence: Evidence
    target: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"subagent", "fork", "resume", "handoff", "continuation", "unknown"}:
            raise ValueError(f"unsupported session relation: {self.kind!r}")
        if self.target is not None and not self.target.strip():
            raise ValueError("relation target must be omitted when unavailable")


@dataclass(frozen=True)
class Usage:
    """Model and token/cost usage exactly as native history records it.

    Every numeric field is populated only from a native record; nothing
    is estimated. Absent fields stay None.
    """

    evidence: Evidence
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None


@dataclass(frozen=True)
class CompactionInfo:
    """Marker for an event at a native context-compaction boundary.

    The carrying event keeps its projected kind (so v1 bytes do not
    change); standalone ``compaction`` events carry the summary as text.
    """

    evidence: Evidence
    summary: str | None = None


@dataclass(frozen=True)
class ActivityEvent:
    """One normalized activity record with evidence and optional typed details."""

    seq: int
    type: ActivityEventType
    evidence: Evidence
    ts: float | None = None
    message_id: str | None = None
    turn_id: str | None = None
    text: str | None = None
    name: str | None = None
    call_id: str | None = None
    raw_input: object = None
    raw_output: object = None
    result: ToolResultOutcome | None = None
    error: AgentError | None = None
    interaction: InteractionRequest | None = None
    origin: MessageOrigin = "unknown"
    usage: Usage | None = None
    compaction: CompactionInfo | None = None
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError("activity event seq starts at 1")
        if self.type not in {
            "user_message", "assistant_message", "thinking", "tool_call", "tool_result",
            "compaction", "lifecycle",
        }:
            raise ValueError(f"unsupported activity event type: {self.type!r}")
        if self.origin not in {"human", "injected", "system", "unknown"}:
            raise ValueError(f"unsupported message origin: {self.origin!r}")
        if self.stop_reason is not None and not self.stop_reason.strip():
            raise ValueError("stop_reason must be omitted when unavailable")


@dataclass(frozen=True)
class ActivitySnapshot:
    """Additive typed view of one session history; v1 list APIs stay unchanged."""

    state: LoadState
    events: tuple[ActivityEvent, ...]
    outcome: SessionOutcome
    cursor: str | None = None
    generation: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"available", "empty", "unavailable", "unsupported"}:
            raise ValueError(f"unsupported load state: {self.state!r}")
        if self.state == "available" and not self.events:
            raise ValueError("an available snapshot must carry at least one event")
        if self.state in {"empty", "unavailable", "unsupported"} and self.events:
            raise ValueError(f"a {self.state} snapshot must not carry events")

    @property
    def turns(self) -> tuple[Turn, ...]:
        """Per-turn view derived from event order; computed, not stored."""
        from sesskit.turns import derive_turns

        return derive_turns(self)

    @property
    def invocations(self) -> tuple[ToolInvocation, ...]:
        """Paired tool-call/result view; computed, not stored."""
        from sesskit.turns import pair_invocations

        return pair_invocations(self)


_STALE_MTIME_GAP_SECONDS = 3600


def effective_session_time(file_mtime: float, event_time: float | None) -> tuple[float, str]:
    """Prefer event time when file mtime is inflated by metadata-only writes."""
    if event_time is not None and file_mtime - event_time > _STALE_MTIME_GAP_SECONDS:
        return event_time, "event_time_stale_mtime"
    return file_mtime, "file_mtime"


def make_session_info(
    *,
    source: str,
    id: str,
    short_id: str,
    cwd: str,
    mtime: float,
    time_source: str,
    event_time: float | None,
    file_mtime: float,
    size_bytes: int,
    native_title: str | None,
    fallback_title: str,
    status_tag: str,
    path: str,
    first_user_msg: str | None = "",
    last_user_msg: str | None = "",
    last_agent_msg: str | None = "",
    **extra: object,
) -> SessionInfo:
    """Assemble a SessionInfo dict shared by all runtime parsers."""
    from sesskit.parsers.common import shorten_cwd
    from sesskit.titles import clip_user_excerpt

    session: SessionInfo = {
        "source": source,
        "id": id,
        "short_id": short_id,
        "cwd": cwd,
        "cwd_display": shorten_cwd(cwd),
        "mtime": mtime,
        "display_time": format_message_time(mtime),
        "time_source": time_source,
        "event_time": event_time,
        "file_mtime": file_mtime,
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024, 1),
        "native_title": native_title,
        "fallback_title": fallback_title,
        "status_tag": status_tag,
        "live": False,
        "pid": None,
        "first_user_msg": clip_user_excerpt(first_user_msg),
        "last_user_msg": clip_user_excerpt(last_user_msg),
        "last_agent_msg": clip_user_excerpt(last_agent_msg),
        "path": path,
    }
    session.update(extra)  # type: ignore[typeddict-item]
    return session


def completion_id_for(
    *,
    file_mtime: float,
    size_bytes: int,
    status_tag: str,
    tail_text: str | None = "",
) -> str:
    """一轮结束的稳定标识：同一轮重复扫描值不变，新一轮结束必变，重启可重算。

    只给终端态（已完成/已中断）用；进行中/未知返回空串，调用方不得拿它去重。
    组成：文件 mtime 纳秒 + 字节数 + 状态 + 尾事件短哈希——mtime/size 区分轮次，
    尾哈希防止同秒同大小的误判。全部来自已落盘历史，不依赖进程内存。
    """
    from sesskit import titles

    if status_tag not in (titles.STATUS_DONE, titles.STATUS_ABORTED):
        return ""
    try:
        mtime_ns = int(float(file_mtime) * 1_000_000_000)
    except (TypeError, ValueError):
        mtime_ns = 0
    try:
        size = int(size_bytes)
    except (TypeError, ValueError):
        size = 0
    tail = str(tail_text or "")[:500]
    digest = hashlib.sha256(tail.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{mtime_ns}:{size}:{status_tag}:{digest}"


def format_message_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")  # noqa: DTZ006 - Display uses local time.


def session_key(session: SessionInfo | dict) -> str:
    runtime_id = str(session.get("source") or "unknown")
    return f"{runtime_id}:{session['id']}"


def parse_session_key(key: str) -> tuple[str, str]:
    runtime_id, sep, session_id = str(key or "").partition(":")
    if not sep:
        return "unknown", runtime_id
    return runtime_id, session_id


@dataclass(frozen=True)
class ConversationMessage:
    """One user or assistant text turn extracted from native history."""

    role: Literal["user", "assistant"]
    text: str
    timestamp: float | None = None


# --- Stage C: closed error taxonomy, turns, typed event union (additive) ---

#: Closed failure classes for normalized agent errors. ``AgentError.kind``
#: stays a free string for compatibility (legacy producers emit values such
#: as ``"provider"``, ``"aborted"``, or native names like ``"APIError"``);
#: use :mod:`sesskit.errors` to map those to an ``ErrorKind``.
ErrorKind = Literal[
    "rate_limited",
    "quota_exhausted",
    "auth",
    "context_length",
    "provider_overloaded",
    "provider_error",
    "policy_blocked",
    "tool_error",
    "user_interrupt",
    "timeout",
    "runtime_crash",
    "unknown",
]

ERROR_KINDS: frozenset[str] = frozenset({
    "rate_limited",
    "quota_exhausted",
    "auth",
    "context_length",
    "provider_overloaded",
    "provider_error",
    "policy_blocked",
    "tool_error",
    "user_interrupt",
    "timeout",
    "runtime_crash",
    "unknown",
})

#: Per-turn completion states. ``failed`` carries a classified error;
#: ``interrupted`` is a user-interrupt specifically; ``rejected`` is a model
#: refusal (never an error); ``awaiting_input`` needs explicit native
#: evidence of waiting on input, while a bare trailing user message yields
#: at most ``in_progress``. The session outcome derives from the last turn
#: (completed->done, awaiting_input/in_progress->pending,
#: failed/interrupted->aborted, rejected/unknown->unknown).
#: ``"pending"`` is accepted as a deprecated alias of ``awaiting_input``.
TurnOutcomeStatus = Literal[
    "completed", "awaiting_input", "interrupted", "failed", "rejected",
    "in_progress", "unknown",
]

_TURN_STATUS_ALIASES = {"pending": "awaiting_input"}


@dataclass(frozen=True)
class TurnOutcome:
    """Outcome of one user-delimited turn with supporting evidence."""

    status: TurnOutcomeStatus
    evidence: Evidence
    error: AgentError | None = None
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        normalized = _TURN_STATUS_ALIASES.get(self.status, self.status)  # type: ignore[arg-type]
        if normalized != self.status:
            object.__setattr__(self, "status", normalized)
        if self.status not in {
            "completed", "awaiting_input", "interrupted", "failed",
            "rejected", "in_progress", "unknown",
        }:
            raise ValueError(f"unsupported turn outcome: {self.status!r}")
        if self.status != "unknown" and self.evidence.origin == "unknown":
            raise ValueError("a known turn outcome requires native or inferred evidence")
        if self.status == "completed" and self.error is not None:
            raise ValueError("a completed turn cannot carry an agent error")
        if self.status != "failed" and self.status != "interrupted" and self.error is not None:
            raise ValueError("only failed/interrupted turns carry an agent error")
        if self.stop_reason is not None and not self.stop_reason.strip():
            raise ValueError("stop_reason must be omitted when unavailable")


@dataclass(frozen=True)
class Turn:
    """One user-delimited turn: events from a user input to the next one."""

    turn_id: str
    index: int
    start_seq: int
    end_seq: int
    outcome: TurnOutcome
    user_seq: int | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("turn index starts at 0")
        if self.end_seq < self.start_seq:
            raise ValueError("turn end_seq must cover start_seq")


def turn_id_for(index: int) -> str:
    """Session-local stable turn identifier for a zero-based turn index."""
    return f"turn-{index:04d}"


@dataclass(frozen=True)
class _TypedEventBase:
    """Shared identity fields for every per-kind typed event view."""

    seq: int = 0
    ts: float | None = None
    message_id: str | None = None
    turn_id: str | None = None
    evidence: Evidence | None = None


@dataclass(frozen=True)
class UserMessage(_TypedEventBase):
    kind: Literal["user_message"] = "user_message"
    text: str = ""
    origin: MessageOrigin = "unknown"


@dataclass(frozen=True)
class AssistantMessage(_TypedEventBase):
    kind: Literal["assistant_message"] = "assistant_message"
    text: str = ""
    error: AgentError | None = None


@dataclass(frozen=True)
class Thinking(_TypedEventBase):
    kind: Literal["thinking"] = "thinking"
    text: str = ""


@dataclass(frozen=True)
class ToolCall(_TypedEventBase):
    kind: Literal["tool_call"] = "tool_call"
    name: str = "tool"
    call_id: str = ""
    raw_input: object = None
    interaction: InteractionRequest | None = None


@dataclass(frozen=True)
class ToolResult(_TypedEventBase):
    kind: Literal["tool_result"] = "tool_result"
    call_id: str = ""
    raw_output: object = None
    result: ToolResultOutcome | None = None


@dataclass(frozen=True)
class ErrorEvent(_TypedEventBase):
    """Normalized error view for an event carrying ``AgentError``.

    ``error_kind`` is the closed taxonomy classification of ``error``;
    ``source_seq`` points at the carrying event. Unclassifiable errors
    stay ``unknown``, never guessed.
    """

    kind: Literal["error"] = "error"
    error: AgentError | None = None
    error_kind: ErrorKind = "unknown"
    source_seq: int = 0


@dataclass(frozen=True)
class InteractionEvent(_TypedEventBase):
    """Structured-request view for an event carrying ``InteractionRequest``."""

    kind: Literal["interaction"] = "interaction"
    interaction: InteractionRequest | None = None
    source_seq: int = 0


@dataclass(frozen=True)
class CompactionEvent(_TypedEventBase):
    """Boundary view for an event marking native context compaction."""

    kind: Literal["compaction"] = "compaction"
    summary: str | None = None
    source_seq: int = 0


@dataclass(frozen=True)
class LifecycleEvent(_TypedEventBase):
    """Typed-only marker for native turn boundaries without chat content
    (abort records, bare completion markers, empty tail rows).

    Never projected to v1 or plain conversation; exists so turn derivation
    sees the same terminal evidence as the tail-outcome rules.
    """

    kind: Literal["lifecycle"] = "lifecycle"
    marker: str = ""
    stop_reason: str | None = None
    error: AgentError | None = None
    source_seq: int = 0


TypedEvent = UserMessage | AssistantMessage | Thinking | ToolCall | ToolResult | ErrorEvent | InteractionEvent | CompactionEvent | LifecycleEvent


def as_typed(event: ActivityEvent) -> UserMessage | AssistantMessage | Thinking | ToolCall | ToolResult | CompactionEvent | LifecycleEvent:
    """Convert one ``ActivityEvent`` to its per-kind typed view.

    ``ActivityEvent`` keeps working unchanged; this is a convenience
    projection so consumers get exhaustively checkable types.
    """
    shared: dict = {
        "seq": event.seq,
        "ts": event.ts,
        "message_id": event.message_id,
        "turn_id": event.turn_id,
        "evidence": event.evidence,
    }
    if event.type == "user_message":
        return UserMessage(text=event.text or "", origin=event.origin, **shared)
    if event.type == "assistant_message":
        return AssistantMessage(text=event.text or "", error=event.error, **shared)
    if event.type == "thinking":
        return Thinking(text=event.text or "", **shared)
    if event.type == "tool_call":
        return ToolCall(
            name=event.name or "tool",
            call_id=event.call_id or "",
            raw_input=event.raw_input,
            interaction=event.interaction,
            **shared,
        )
    if event.type == "tool_result":
        return ToolResult(
            call_id=event.call_id or "",
            raw_output=event.raw_output,
            result=event.result,
            **shared,
        )
    if event.type == "compaction":
        summary = event.text
        if summary is None and event.compaction is not None:
            summary = event.compaction.summary
        return CompactionEvent(
            summary=summary,
            source_seq=event.seq,
            **shared,
        )
    if event.type == "lifecycle":
        return LifecycleEvent(
            marker=str(event.text or ""),
            stop_reason=event.stop_reason,
            error=event.error,
            source_seq=event.seq,
            **shared,
        )
    raise ValueError(f"unsupported activity event type: {event.type!r}")


#: Result status of one tool invocation. ``proposed`` is an issued call with
#: no result yet; ``denied`` is a refusal by a person or policy (never
#: ``failed``); ``awaiting_approval`` waits on an explicit native approval;
#: missing evidence is ``unknown``, never success.
InvocationStatus = Literal[
    "proposed", "succeeded", "failed", "denied",
    "awaiting_approval", "timed_out", "unknown",
]

#: How a tool call/result pair was established: shared native correlation
#: id (``native``), positional adjacency fallback (``inferred``), or
#: unpaired/orphan (``unknown``).
InvocationPairing = Literal["native", "inferred", "unknown"]


@dataclass(frozen=True)
class ToolInvocation:
    """One tool call with its optional result, result status, and pairing."""

    call: ActivityEvent | None
    result: ActivityEvent | None
    status: InvocationStatus
    evidence: Evidence
    pairing: InvocationPairing = "unknown"

    def __post_init__(self) -> None:
        if self.status not in {
            "proposed", "succeeded", "failed", "denied",
            "awaiting_approval", "timed_out", "unknown",
        }:
            raise ValueError(f"unsupported invocation status: {self.status!r}")
        if self.pairing not in {"native", "inferred", "unknown"}:
            raise ValueError(f"unsupported invocation pairing: {self.pairing!r}")
        if self.call is not None and self.call.type != "tool_call":
            raise ValueError("invocation call must be a tool_call event")
        if self.result is not None and self.result.type != "tool_result":
            raise ValueError("invocation result must be a tool_result event")
        if self.call is None and self.result is None:
            raise ValueError("an invocation needs a call or a result")
