"""Turns, per-turn outcomes, and tool-invocation pairing (stages C+G).

A turn starts at a user input and ends at the next user input or end of
history; where producers stamp native turn records the span keeps the
native id, otherwise a session-local ``turn-XXXX`` id is derived.
Everything here derives from an :class:`ActivitySnapshot` without changing
v1 output.
"""

from __future__ import annotations

from dataclasses import replace

from sesskit import errors as _errors
from sesskit.models import (
    ActivityEvent,
    ActivitySnapshot,
    ErrorEvent,
    Evidence,
    InteractionEvent,
    SessionOutcome,
    ToolInvocation,
    Turn,
    TurnOutcome,
    turn_id_for,
)

_TURN_STATUSES = (
    "completed", "awaiting_input", "interrupted", "failed", "rejected",
    "in_progress", "unknown",
)

#: TurnOutcome.status -> SessionOutcome.status mapping used by
#: :func:`derive_session_outcome`. ``rejected`` (a refusal, never an error)
#: has no session-level counterpart and stays ``unknown``.
_TURN_TO_SESSION = {
    "completed": "done",
    "awaiting_input": "pending",
    "in_progress": "pending",
    "failed": "aborted",
    "interrupted": "aborted",
    "rejected": "unknown",
    "unknown": "unknown",
}

_TIMEOUT_OUTPUT_MARKERS = ("timed out", "timeout", "deadline exceeded")


def _record(event: ActivityEvent) -> str | None:
    return event.evidence.record if event.evidence is not None else None


def _stop_reason(events: list[ActivityEvent]) -> str | None:
    """Last verbatim native stop reason in the span, if any producer kept one."""
    for event in reversed(events):
        if event.stop_reason:
            return event.stop_reason
    return None


def _turn_outcome(
    events: list[ActivityEvent], *, is_last: bool, runtime: str = "",
) -> TurnOutcome:
    """Outcome for one turn span; unknown stays unknown without evidence.

    A mid-turn error followed by clean agent content is a recovered error,
    not a turn failure: only a terminal error (no clean assistant-side
    content after the last error-carrying event) fails or interrupts the
    turn. A bare trailing user message is weak evidence: at most
    ``in_progress`` with inferred evidence, never ``awaiting_input``.
    Native lifecycle markers (abort records, bare completion markers) carry
    the same terminal weight as the tail-outcome rules give them.
    """
    error_seq = max(
        (event.seq for event in events if event.error is not None), default=-1,
    )
    clean_seq = max(
        (
            event.seq
            for event in events
            if event.error is None
            and event.type in {
                "assistant_message", "thinking", "tool_call", "tool_result",
            }
        ),
        default=-1,
    )
    stop_reason = _stop_reason(events)
    if error_seq >= 0 and error_seq > clean_seq:
        failed = next(
            event for event in reversed(events) if event.error is not None
        )
        assert failed.error is not None
        error_kind, _ = _errors.classify_agent_error(failed.error, runtime=runtime)
        status = "interrupted" if error_kind == "user_interrupt" else "failed"
        reason = failed.stop_reason or stop_reason
        return TurnOutcome(status, failed.error.evidence, failed.error, stop_reason=reason)  # type: ignore[arg-type]
    terminal_markers = [
        event for event in events
        if event.type == "lifecycle" and event.seq > max(error_seq, clean_seq)
    ]
    if terminal_markers:
        marker = terminal_markers[-1]
        text = (marker.text or "").strip().lower()
        reason = marker.stop_reason or stop_reason
        if marker.error is not None:
            error_kind, _ = _errors.classify_agent_error(marker.error, runtime=runtime)
            status = "interrupted" if error_kind == "user_interrupt" else "failed"
            return TurnOutcome(status, marker.error.evidence, marker.error, stop_reason=reason)  # type: ignore[arg-type]
        if "abort" in text or "cancel" in text:
            return TurnOutcome("interrupted", marker.evidence, stop_reason=reason)
        if "complete" in text or "finish" in text or "stop" in text:
            return TurnOutcome("completed", marker.evidence, stop_reason=reason)
        return TurnOutcome("unknown", Evidence("unknown"))
    if is_last and events and events[-1].type == "user_message":
        return TurnOutcome(
            "in_progress",
            Evidence("inferred", field="tail_role", record=_record(events[-1])),
            stop_reason=stop_reason,
        )
    assistant_side = any(
        event.type in {"assistant_message", "thinking", "tool_call", "tool_result", "lifecycle"}
        for event in events
    )
    if assistant_side:
        if is_last and _has_unresolved_calls(events):
            return TurnOutcome("unknown", Evidence("unknown"))
        last = events[-1]
        return TurnOutcome(
            "completed",
            Evidence("inferred", field="turn_tail", record=_record(last)),
            stop_reason=stop_reason,
        )
    if not is_last:
        # A user-only span followed by more input: no agent work evidenced.
        return TurnOutcome("unknown", Evidence("unknown"))
    return TurnOutcome(
        "in_progress",
        Evidence("inferred", field="tail_role", record=_record(events[-1])),
        stop_reason=stop_reason,
    )


def _has_unresolved_calls(events: list[ActivityEvent]) -> bool:
    issued = {
        str(event.call_id)
        for event in events
        if event.type == "tool_call" and event.call_id
    }
    answered = {
        str(event.call_id)
        for event in events
        if event.type == "tool_result" and event.call_id
    }
    unkeyed_calls = sum(
        1 for event in events
        if event.type == "tool_call" and not event.call_id
    )
    unkeyed_results = sum(
        1 for event in events
        if event.type == "tool_result" and not event.call_id
    )
    return bool(issued - answered) or unkeyed_calls > unkeyed_results


def derive_turns(snapshot: ActivitySnapshot, *, runtime: str = "") -> tuple[Turn, ...]:
    """Group snapshot events into user-delimited turns with per-turn outcomes.

    Events before the first user message form a leading turn (index 0,
    ``user_seq`` None). Every ``user_message`` starts a new turn. When the
    producer stamped native turn records, a span whose events share one
    native id keeps it; otherwise a session-local ``turn-XXXX`` id is
    derived. Empty and non-available snapshots yield no turns.
    """
    events = list(snapshot.events)
    if not events:
        return ()
    spans: list[list[ActivityEvent]] = [[]]
    for event in events:
        if event.type == "user_message" and spans[-1]:
            spans.append([])
        spans[-1].append(event)
    # Drop a trailing empty span (cannot happen: spans only split on append).
    turns: list[Turn] = []
    for index, span in enumerate(spans):
        if not span:
            continue
        user_seq = next(
            (event.seq for event in span if event.type == "user_message"), None,
        )
        is_last = index == len(spans) - 1
        turns.append(Turn(
            turn_id=_span_turn_id(span, index),
            index=index,
            start_seq=span[0].seq,
            end_seq=span[-1].seq,
            outcome=_turn_outcome(span, is_last=is_last, runtime=runtime),
            user_seq=user_seq,
        ))
    for status in [turn.outcome.status for turn in turns]:
        assert status in _TURN_STATUSES, status
    return tuple(turns)


def _span_turn_id(span: list[ActivityEvent], index: int) -> str:
    """Native turn id when the span agrees on one, else a derived id."""
    from collections import Counter

    native = Counter(
        event.turn_id for event in span if event.turn_id
    )
    if len(native) == 1:
        only = next(iter(native))
        if only:
            return only
    return turn_id_for(index)


def derive_session_outcome(
    turns: tuple[Turn, ...], fallback: SessionOutcome,
) -> SessionOutcome:
    """Session outcome derived consistently from the last turn.

    ``fallback`` (the producer's own tail outcome) is returned unchanged
    when there are no turns. Otherwise the last turn decides: a known
    session status requires the turn's evidence; the error object rides
    along unchanged.
    """
    if not turns:
        return fallback
    last = turns[-1]
    status = _TURN_TO_SESSION[last.outcome.status]
    if status == "unknown":
        return SessionOutcome("unknown", Evidence("unknown"))
    if status == "done":
        return SessionOutcome("done", last.outcome.evidence)
    return SessionOutcome(status, last.outcome.evidence, last.outcome.error)  # type: ignore[arg-type]


def _invocation_status(
    call: ActivityEvent | None, result: ActivityEvent | None,
) -> tuple[str, Evidence]:
    """Result status for one paired call/result; missing evidence is unknown.

    ``denied`` comes only from an explicitly denied/declined linked
    interaction (never guessed from output text); ``timed_out`` needs an
    error result plus native timeout wording in the output.
    """
    if result is None:
        if call is not None and call.interaction is not None:
            if call.interaction.resolution == "pending":
                evidence = call.interaction.resolution_evidence or call.evidence
                return "awaiting_approval", evidence
            if call.interaction.resolution in {"denied", "declined"}:
                evidence = call.interaction.resolution_evidence or call.evidence
                return "denied", evidence
        record = _record(call) if call is not None else None
        evidence = call.evidence if call is not None and call.evidence is not None else Evidence("unknown", record=record)
        return "proposed", evidence
    outcome = result.result
    if outcome is not None and outcome.status == "ok":
        return "succeeded", outcome.evidence
    if outcome is not None and outcome.status == "error":
        output = result.raw_output
        text = output if isinstance(output, str) else str(output or "")
        if any(marker in text.lower() for marker in _TIMEOUT_OUTPUT_MARKERS):
            return "timed_out", outcome.evidence
        return "failed", outcome.evidence
    record = _record(result)
    return "unknown", Evidence("unknown", record=record)


def pair_invocations(snapshot: ActivitySnapshot) -> tuple[ToolInvocation, ...]:
    """Pair tool calls with results by native call id, adjacency otherwise.

    Exact non-empty ``call_id`` matches pair with ``native`` pairing; a
    result with an empty id attaches to the most recent unpaired call as
    ``inferred``; unpaired calls and orphan results surface with
    ``unknown`` pairing so no linkage is silently invented. The result
    ``status`` (proposed/succeeded/failed/denied/awaiting_approval/
    timed_out/unknown) is tracked separately from how the pair was made.
    """
    invocations: list[ToolInvocation] = []
    open_calls: dict[str, int] = {}
    pending_adjacent: list[int] = []
    for event in snapshot.events:
        if event.type == "tool_call":
            status, evidence = _invocation_status(event, None)
            invocations.append(ToolInvocation(
                call=event,
                result=None,
                status=status,  # type: ignore[arg-type]
                evidence=evidence,
                pairing="unknown",
            ))
            index = len(invocations) - 1
            call_id = str(event.call_id or "")
            if call_id:
                open_calls.setdefault(call_id, index)
            else:
                pending_adjacent.append(index)
            continue
        if event.type != "tool_result":
            continue
        call_id = str(event.call_id or "")
        if call_id and call_id in open_calls:
            index = open_calls.pop(call_id)
            previous = invocations[index]
            status, evidence = _invocation_status(previous.call, event)
            invocations[index] = ToolInvocation(
                call=previous.call,
                result=event,
                status=status,  # type: ignore[arg-type]
                evidence=evidence,
                pairing="native",
            )
        elif pending_adjacent:
            index = pending_adjacent.pop()
            previous = invocations[index]
            status, evidence = _invocation_status(previous.call, event)
            invocations[index] = ToolInvocation(
                call=previous.call,
                result=event,
                status=status,  # type: ignore[arg-type]
                evidence=evidence,
                pairing="inferred",
            )
        else:
            status, evidence = _invocation_status(None, event)
            invocations.append(ToolInvocation(
                call=None,
                result=event,
                status=status,  # type: ignore[arg-type]
                evidence=evidence,
                pairing="unknown",
            ))
    return tuple(invocations)


def with_turn_ids(snapshot: ActivitySnapshot, *, runtime: str = "") -> ActivitySnapshot:
    """Return a copy of ``snapshot`` with ``turn_id`` stamped on each event.

    Producers stamp native turn records at build time; this helper covers
    snapshots whose events lack them, deriving the same grouping the
    :attr:`turns` accessor computes on demand.
    """
    turns = derive_turns(snapshot, runtime=runtime)
    seq_to_turn = {}
    for turn in turns:
        for event in snapshot.events:
            if turn.start_seq <= event.seq <= turn.end_seq:
                seq_to_turn[event.seq] = turn.turn_id
    stamped = tuple(
        replace(event, turn_id=seq_to_turn.get(event.seq))
        for event in snapshot.events
    )
    return ActivitySnapshot(
        state=snapshot.state,
        events=stamped,
        outcome=snapshot.outcome,
        cursor=snapshot.cursor,
        generation=snapshot.generation,
    )


def error_views(
    snapshot: ActivitySnapshot, *, runtime: str = "",
) -> tuple[ErrorEvent, ...]:
    """Normalized :class:`ErrorEvent` view for every event carrying an error."""
    views: list[ErrorEvent] = []
    for event in snapshot.events:
        if event.error is None:
            continue
        error_kind, _ = _errors.classify_agent_error(event.error, runtime=runtime)
        views.append(ErrorEvent(
            seq=event.seq,
            ts=event.ts,
            message_id=event.message_id,
            turn_id=event.turn_id,
            evidence=event.error.evidence,
            error=event.error,
            error_kind=error_kind,
            source_seq=event.seq,
        ))
    return tuple(views)


def interaction_views(snapshot: ActivitySnapshot) -> tuple[InteractionEvent, ...]:
    """Normalized :class:`InteractionEvent` view for structured requests."""
    return tuple(
        InteractionEvent(
            seq=event.seq,
            ts=event.ts,
            message_id=event.message_id,
            turn_id=event.turn_id,
            evidence=event.interaction.evidence if event.interaction else event.evidence,
            interaction=event.interaction,
            source_seq=event.seq,
        )
        for event in snapshot.events
        if event.interaction is not None
    )
