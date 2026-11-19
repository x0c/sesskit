"""Additive typed activity view over native session history.

``load_events`` in :mod:`sesskit.transcript` stays the v1 compatibility
projection; for migrated runtimes it is generated from the same typed
snapshot built here, so raw-history interpretation has a single source.
This module adds ``load_activity``, which returns an
:class:`~sesskit.models.ActivitySnapshot` with per-event evidence,
typed tool-result outcomes, and structured errors kept separate from
assistant-authored text.

The ``pi``, ``claude``, ``codex``, ``cursor``, and ``opencode`` runtimes are
implemented. Kimi intentionally remains on its legacy transcript path until a
separate typed-adaptation slice is approved. No v1 serialization changes here.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from typing import Any

from sesskit.adapters import get_adapter
from sesskit.errors import classify_error, native_http_status
from sesskit.models import (
    ActivityEvent,
    ActivitySnapshot,
    AgentError,
    AnswerRecord,
    CompactionInfo,
    Evidence,
    InteractionRequest,
    QuestionItem,
    QuestionOption,
    SessionOutcome,
    SkillInvocation,
    ToolResultOutcome,
    Usage,
)
from sesskit.parsers import claude as scan_claude
from sesskit.parsers import codex as scan_codex
from sesskit.parsers import pi as scan_pi
from sesskit.parsers.common import classify_tool, parse_timestamp
from sesskit.skills import normalize_skill_name, raw_skill_name, skill_from_tool_call
from sesskit.transcript import _codex_custom_input as _codex_coerce_custom
from sesskit.transcript import _codex_reasoning_text as _codex_reasoning
from sesskit.transcript import _failed as _heuristic_status
from sesskit.transcript import _json_args as _coerce_input
from sesskit.visibility import visible_in_v1

_NATIVE = "native"
_INFERRED = "inferred"
_UNKNOWN = "unknown"

_PILOT_RUNTIMES = ("pi", "claude", "codex", "cursor", "opencode")


def load_activity(session: dict) -> ActivitySnapshot:
    """Return a typed activity snapshot for one scanned session dict.

    State mapping for migrated runtimes: missing/unreadable history ->
    ``unavailable``; readable history with zero normalized events ->
    ``empty``; otherwise ``available``. Unknown runtimes and not-yet-migrated
    runtimes -> ``unsupported`` (use ``load_events`` for those). Dispatch
    goes through the runtime adapter registry; each adapter owns its
    per-runtime branch.
    """
    runtime_id = str(session.get("source") or "")
    try:
        return get_adapter(runtime_id).load_activity(session)
    except KeyError:
        return ActivitySnapshot(
            state="unsupported",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )


def _load_opencode_legacy_schema(session: dict) -> ActivitySnapshot:
    """Bridge old v1 fixtures lacking the newer ``session`` table.

    The standalone adapter owns interpretation; this only supplies its v1
    rows for databases written before the session table was introduced.
    """
    path = str(session.get("path") or "")
    session_id = str(session.get("id") or "")
    if not path or not session_id or not os.path.isfile(path):
        return ActivitySnapshot("unsupported", (), SessionOutcome("unknown", Evidence(_UNKNOWN)))
    try:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
                      p.id AS part_id, p.time_created AS part_time, p.data AS part_data
               FROM message m LEFT JOIN part p ON p.message_id = m.id
               WHERE m.session_id = ?
               ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC""",
            (session_id,),
        ).fetchall()
        connection.close()
    except (OSError, sqlite3.Error):
        return ActivitySnapshot("unsupported", (), SessionOutcome("unknown", Evidence(_UNKNOWN)))
    from sesskit.activity_opencode import _build_v1, _v1_outcome

    events, tail = _build_v1(rows, session_id)
    outcome = _v1_outcome(tail)
    return ActivitySnapshot("available" if events else "empty", tuple(events), outcome)


def _readable(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb"):
            pass
    except OSError:
        return False
    return True


def _load_pi(session: dict) -> ActivitySnapshot:
    path = str(session.get("path") or "")
    session_id = str(session.get("id") or path or "unknown")
    if not _readable(path):
        return ActivitySnapshot(
            state="unavailable",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    entries = scan_pi.read_entries(path)
    branch = scan_pi.active_messages(entries)
    events = _build_pi_events(branch, session_id, compactions=_pi_compactions(entries, branch),
                               transitions=_pi_transitions(entries, branch))
    if not events:
        return ActivitySnapshot(
            state="empty",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    return ActivitySnapshot(
        state="available",
        events=tuple(events),
        outcome=_pi_outcome(branch),
        # Pi has no safe incremental boundary: every poll rebuilds the active
        # branch, so no cursor is advertised. Never infer one from mtime.
        cursor=None,
        generation=None,
    )


def to_v1_dicts(snapshot: ActivitySnapshot) -> list[dict]:
    """Project a snapshot back to ``load_events``-shaped v1 dicts.

    Field set and legacy defaults mirror ``transcript._Sink.add`` plus the Pi
    parser so the Pi pilot keeps byte-level parity: a typed ``unknown`` tool
    result still projects to the legacy ``"ok"`` default. ``compaction``
    events, typed-only ``lifecycle`` markers, and injected-context user
    messages never appeared in v1, so the projection skips them (see
    :mod:`sesskit.visibility`, shared with the plain-conversation
    projection). Projected ``seq`` values are renumbered densely
    from 1 in projection order, which keeps v1 bytes identical to the
    pre-typed build (the skipped set is exactly the newly surfaced set).
    """
    projected: list[dict] = []
    for event in snapshot.events:
        if not visible_in_v1(event):
            continue
        item: dict[str, Any] = {"type": event.type, "seq": len(projected) + 1, "ts": event.ts}
        if event.type in {"user_message", "assistant_message", "thinking"}:
            if event.text is not None:
                item["text"] = event.text
        elif event.type == "tool_call":
            item["id"] = event.call_id or ""
            item["name"] = event.name or "tool"
            item["kind"] = classify_tool(item["name"])
            item["input"] = event.raw_input if event.raw_input is not None else {}
        elif event.type == "tool_result":
            item["call_id"] = event.call_id or ""
            if event.result is not None and event.result.status != "unknown":
                item["status"] = event.result.status
            else:
                item["status"] = "ok"
            if event.raw_output is not None:
                item["output"] = event.raw_output
        is_opencode = isinstance(event.message_id, str) and event.message_id.startswith("opencode:")
        is_pi = isinstance(event.message_id, str) and event.message_id.startswith("pi:")
        # Claude native message ids (msg_*) are typed-only: projecting them
        # would change v1 bytes versus the pre-typed build, so only the Pi
        # grouping ids (pi:...) project. opencode ids were already excluded.
        # Parity 2026-11-04: transcript/opencode_v2/activity 51 passed;
        # 20 live Pi + 10 live Claude sessions byte-equal
        # (to_v1_dicts(load_activity(s)) == load_events(s); Claude typed
        # carries msg_* e.g. 154 events in the largest session while v1
        # projects 0, Pi v1 keeps its pi:... grouping ids).
        if is_pi:
            item["message_id"] = event.message_id
        if event.type == "tool_result" and is_opencode and isinstance(item.get("output"), list):
            item["output"] = "\n\n".join(
                part["text"].strip()
                for part in item["output"]
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            )
        projected.append(item)
    return projected


# Identity and join contract (mirrors activity_opencode.py style):
# activity ``message_id`` values are namespaced as
# ``pi:{session_id}:entry:{native_id}`` (fallback
# ``pi:{session_id}:message:{position}`` when the entry has no native id)
# while event evidence ``record`` values stay native (``entry:{id}`` /
# ``message:{position}``). The usage collector
# (``usage.collect_pi_observations``) stores the bare native id in its
# ``message_id``/``scope_id`` fields, so usage-side joins must strip the
# ``pi:{session_id}:entry:`` prefix (or match on the evidence-record
# suffix) instead of comparing full ids.
def _message_id(session_id: str, entry_id: object, position: int) -> str:
    if isinstance(entry_id, str) and entry_id:
        return f"pi:{session_id}:entry:{entry_id}"
    return f"pi:{session_id}:message:{position}"


def _record_ref(item: dict, position: int) -> str:
    entry_id = item.get("id")
    if isinstance(entry_id, str) and entry_id:
        return f"entry:{entry_id}"
    return f"message:{position}"


def _pi_compactions(entries: list[dict], branch: list[dict]) -> dict[str, dict]:
    """Compaction entries feeding directly into the active branch.

    A compaction entry links into the chain through ``parentId``: the
    first branch message after compaction names the compaction id as its
    parent. Only those entries are emitted, in branch order.
    """
    followed = {
        str(item.get("parentId"))
        for item in branch
        if isinstance(item.get("parentId"), str)
    }
    found: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "compaction":
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id and entry_id in followed:
            found.setdefault(entry_id, entry)
    return found


def _pi_transitions(entries: list[dict], branch: list[dict]) -> dict[str, dict]:
    """Model/thinking-level changes feeding into the active branch.

    Like compactions, these link through ``parentId``: only entries whose
    id is named as a parent by the active branch (or that head the chain)
    are emitted, as typed-only ``lifecycle`` markers (v1 skips lifecycle,
    so projection bytes stay identical).
    """
    on_path: set[str] = set()
    for item in branch:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            on_path.add(item_id)
        parent_id = item.get("parentId")
        # Walk up through non-message ancestors is done at emit time;
        # here just collect direct parents so head-of-chain transitions
        # (whose child is the first branch message) are included.
        if isinstance(parent_id, str) and parent_id:
            on_path.add(parent_id)
    # Include transitive non-message ancestors (model_change <->
    # thinking_level_change chains at session start).
    by_id = {str(e.get("id")): e for e in entries
             if isinstance(e, dict) and isinstance(e.get("id"), str)}
    expanded = set(on_path)
    for pid in list(on_path):
        node = by_id.get(pid)
        while isinstance(node, dict) and node.get("type") in {"model_change", "thinking_level_change"}:
            expanded.add(str(node.get("id")))
            parent = node.get("parentId")
            if not isinstance(parent, str) or not parent or parent in expanded:
                if isinstance(parent, str) and parent:
                    expanded.add(parent)
                break
            expanded.add(parent)
            node = by_id.get(parent)
    found: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") not in {"model_change", "thinking_level_change"}:
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id and entry_id in expanded:
            found.setdefault(entry_id, entry)
    return found


def _pi_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _pi_usage(message: dict, record: str) -> Usage | None:
    """Native per-message usage; None when the shape is absent or unparseable.

    ``model`` stays the reported/effective model. Attribution extras ride
    alongside: ``provider``/``api`` from the message, ``reasoning_level``
    from ``thinkingLevel`` (a configuration-side strength selector, never
    the ``reasoning`` output-token count). Absent stays None; nothing is
    estimated.
    """
    usage = message.get("usage")
    model = message.get("model")
    if not isinstance(usage, dict) and not isinstance(model, str):
        return None
    fields: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    cost: float | None = None
    if isinstance(usage, dict):
        fields["input_tokens"] = _pi_int(usage.get("input"))
        fields["output_tokens"] = _pi_int(usage.get("output"))
        fields["cache_read_tokens"] = _pi_int(usage.get("cacheRead"))
        fields["cache_write_tokens"] = _pi_int(usage.get("cacheWrite"))
        fields["reasoning_tokens"] = _pi_int(usage.get("reasoning"))
        fields["total_tokens"] = _pi_int(usage.get("totalTokens"))
        details = usage.get("cost")
        if isinstance(details, dict):
            total = details.get("total")
            if isinstance(total, (int, float)) and not isinstance(total, bool) and total >= 0:
                cost = float(total)
    if all(value is None for value in fields.values()) and cost is None and not isinstance(model, str):
        return None
    provider = message.get("provider")
    api = message.get("api")
    thinking_level = message.get("thinkingLevel")
    return Usage(
        Evidence(_NATIVE, field="usage", record=record),
        model=model.strip() if isinstance(model, str) and model.strip() else None,
        cost=cost,
        provider=provider.strip() if isinstance(provider, str) and provider.strip() else None,
        api=api.strip() if isinstance(api, str) and api.strip() else None,
        reasoning_level=thinking_level.strip() if isinstance(thinking_level, str) and thinking_level.strip() else None,
        **fields,  # type: ignore[arg-type]
    )


def _build_pi_events(
    branch: list[dict], session_id: str, *, seq_start: int = 0, position_start: int = 0,
    compactions: dict[str, dict] | None = None,
    transitions: dict[str, dict] | None = None,
) -> list[ActivityEvent]:
    """Mirror ``transcript._parse_pi`` emission order, with typed evidence.

    Assistant ``Usage`` lives on ``ActivityEvent.usage``. Note for typed
    consumers: ``as_typed`` exposes usage on ``Thinking`` but
    ``AssistantMessage`` carries no usage field, so assistant usage must be
    read back from ``ActivityEvent.usage`` (usage reconciliation collapses
    text/thinking slices sharing one native record; nothing is merged here).
    """
    events: list[ActivityEvent] = []
    pending_compactions = dict(compactions or {})
    pending_transitions = dict(transitions or {})
    pi_catalog: dict[str, str] = {}

    def _collect_pi_catalog(msg: dict) -> None:
        sections = msg.get("sections")
        if not isinstance(sections, dict):
            return
        skills_xml = sections.get("skills")
        if not isinstance(skills_xml, str) or "<available_skills>" not in skills_xml:
            return
        import re as _re

        for m in _re.finditer(
            r"<skill>\s*<name>([^<>\r\n]+)</name>[\s\S]*?<location>([^<>\r\n]+)</location>[\s\S]*?</skill>",
            skills_xml,
        ):
            sname = raw_skill_name(m.group(1))
            sloc = m.group(2).strip()
            if sname and sloc:
                pi_catalog[sloc] = sname

    def add(event_type: str, ts: float | None, message_id: str,
            record: str, **fields: Any) -> None:
        text = fields.get("text")
        if event_type in {"user_message", "assistant_message", "thinking"} and (
            not isinstance(text, str) or not text.strip()
        ):
            return
        events.append(ActivityEvent(
            seq=seq_start + len(events) + 1,
            type=event_type,  # type: ignore[arg-type]
            evidence=Evidence(_NATIVE, record=record),
            ts=ts,
            message_id=message_id,
            **fields,
        ))

    for position, item in enumerate(branch, position_start + 1):
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        parent_id = item.get("parentId")
        # Emit head-of-chain / mid-session model transitions before the
        # first message that follows them (oldest ancestor first). These
        # are typed-only lifecycle markers; v1 skips lifecycle so
        # projection bytes stay identical.
        if isinstance(parent_id, str):
            chain: list[str] = []
            seen: set[str] = set()
            cursor: str | None = parent_id
            while isinstance(cursor, str) and cursor in pending_transitions and cursor not in seen:
                seen.add(cursor)
                chain.append(cursor)
                parent = pending_transitions[cursor].get("parentId")
                cursor = parent if isinstance(parent, str) and parent else None
            for tid in reversed(chain):
                entry = pending_transitions.pop(tid)
                kind = str(entry.get("type") or "")
                trecord = f"entry:{tid}"
                tts = parse_timestamp(entry.get("timestamp"))
                tmid = _message_id(session_id, tid, position)
                if kind == "model_change":
                    model_id = entry.get("modelId")
                    provider = entry.get("provider")
                    label = str(model_id).strip() if isinstance(model_id, str) and model_id.strip() else kind
                    if isinstance(provider, str) and provider.strip():
                        label = f"{kind}: {provider.strip()}/{label}" if label != kind else f"{kind}: {provider.strip()}"
                    else:
                        label = f"{kind}: {label}" if label != kind else kind
                    events.append(ActivityEvent(
                        seq=seq_start + len(events) + 1,
                        type="lifecycle",
                        evidence=Evidence(_NATIVE, record=trecord),
                        ts=tts,
                        message_id=tmid,
                        text=label,
                    ))
                elif kind == "thinking_level_change":
                    level = entry.get("thinkingLevel")
                    label = f"{kind}: {level}".strip() if isinstance(level, str) and level.strip() else kind
                    events.append(ActivityEvent(
                        seq=seq_start + len(events) + 1,
                        type="lifecycle",
                        evidence=Evidence(_NATIVE, record=trecord),
                        ts=tts,
                        message_id=tmid,
                        text=label,
                    ))
        if isinstance(parent_id, str) and parent_id in pending_compactions:
            entry = pending_compactions.pop(parent_id)
            summary = entry.get("summary") if isinstance(entry.get("summary"), str) else ""
            record = f"entry:{parent_id}"
            events.append(ActivityEvent(
                seq=seq_start + len(events) + 1,
                type="compaction",
                evidence=Evidence(_NATIVE, record=record),
                ts=parse_timestamp(entry.get("timestamp")),
                message_id=_message_id(session_id, parent_id, position),
                text=summary.strip() or None,
                usage=_pi_usage(entry, record),
                compaction=CompactionInfo(
                    Evidence(_NATIVE, field="summary", record=record),
                    summary=summary.strip() or None,
                ),
            ))
        message_id = _message_id(session_id, item.get("id"), position)
        record = _record_ref(item, position)
        ts = parse_timestamp(item.get("timestamp")) or parse_timestamp(message.get("timestamp"))
        role = message.get("role")
        usage = _pi_usage(message, record)
        if role == "system":
            _collect_pi_catalog(message)
            continue
        if role == "user":
            content_obj = message.get("content")
            parsed_skill = scan_pi.parse_pi_skill_blocks(content_obj)
            user_skills: tuple[SkillInvocation, ...] = ()
            if parsed_skill is not None:
                blocks, skill_args = parsed_skill
                user_skills = tuple(
                    SkillInvocation(
                        name=bname,
                        trigger="explicit",
                        detection="command",
                        evidence=Evidence(_NATIVE, field="skill_block", record=record),
                        arguments=skill_args or None,
                        path=bloc or None,
                    )
                    for bname, bloc in blocks
                )
            add("user_message", ts, message_id, record,
                text=scan_pi.user_message_text(content_obj),
                origin="human",
                skills=user_skills)
            continue
        if role == "toolResult":
            output = message.get("content")
            explicit = message.get("isError")
            if explicit is None and message.get("error"):
                explicit = True
            call_id = str(message.get("toolCallId") or "")
            if isinstance(explicit, bool):
                outcome = ToolResultOutcome(
                    "error" if explicit else "ok",
                    Evidence(_NATIVE, field="isError", record=record),
                )
            elif _is_empty_output(output):
                outcome = ToolResultOutcome("unknown", Evidence(_UNKNOWN))
            else:
                outcome = ToolResultOutcome(
                    _heuristic_status(output),  # type: ignore[arg-type]
                    Evidence(_INFERRED, field="message.content", record=record),
                )
            add("tool_result", ts, message_id, record,
                call_id=call_id, raw_output=output, result=outcome)
            continue
        if role != "assistant":
            continue
        content = message.get("content")
        message_stop = str(message.get("stopReason") or "").strip() or None
        message_error_text = str(message.get("errorMessage") or "").strip()
        message_error: AgentError | None = None
        if message_error_text and message_stop in {"error", "aborted"}:
            # Error evidence rides alongside reply text (never replaces it):
            # the same record carries both content and failure.
            error_kind, retryable = classify_error(
                runtime="pi", kind=message_stop, message=message_error_text,
            )
            message_error = AgentError(
                error_kind, message_error_text,
                Evidence(_NATIVE, field="errorMessage", record=record),
                None, retryable, "turn",
                native_http_status(message_error_text),
            )
        emitted = False
        surfaced_error = False
        has_text_event = False
        has_tool_call = False
        if isinstance(content, str):
            text = content.strip()
            if text:
                add("assistant_message", ts, message_id, record, text=text, usage=usage,
                    stop_reason=message_stop, error=message_error)
                emitted = True
                has_text_event = True
                surfaced_error = message_error is not None
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "thinking":
                    thinking = str(part.get("thinking") or part.get("text") or "").strip()
                    if thinking:
                        add("thinking", ts, message_id, record, text=thinking,
                            stop_reason=message_stop, usage=usage)
                        emitted = True
                        has_text_event = True
                elif part_type == "text":
                    text = str(part.get("text") or "").strip()
                    if text:
                        add("assistant_message", ts, message_id, record, text=text, usage=usage,
                            stop_reason=message_stop, error=message_error)
                        emitted = True
                        has_text_event = True
                        surfaced_error = message_error is not None
                elif part_type == "toolCall":
                    raw = part.get("arguments") if part.get("arguments") is not None else part.get("input")
                    tname = str(part.get("name") or "tool")
                    cid = str(part.get("id") or "")
                    coerced = _coerce_input(raw)
                    tskills = skill_from_tool_call(
                        tname, coerced, record=record, call_id=cid, catalog=pi_catalog or None,
                    )
                    add("tool_call", ts, message_id, record,
                        name=tname,
                        call_id=cid,
                        raw_input=coerced,
                        skills=tskills)
                    emitted = True
                    has_tool_call = True
        if message_error is not None and emitted and not surfaced_error:
            # Thinking-only (or tool-call-only) error turn: the thinking
            # event carries the stop reason but no error text, so the
            # failure would vanish from typed activity. Emit one typed-only
            # lifecycle error event (v1 skips lifecycle, so projection
            # bytes stay identical).
            add("lifecycle", ts, message_id, record, text=message_error_text,
                stop_reason=message_stop, error=message_error)
        if usage is not None and has_tool_call and not has_text_event and message_error is None:
            # Tool-only assistant turn (toolCall parts, no text/thinking):
            # usage would otherwise vanish from typed activity since it
            # only hangs off text/thinking events. Emit one typed-only
            # lifecycle carrier for the usage (v1 skips lifecycle, so
            # projection bytes stay identical). Dedup across slices stays
            # in usage reconciliation, never merged here.
            add("lifecycle", ts, message_id, record, text="tool_calls_only",
                stop_reason=message_stop, usage=usage)
        if not emitted:
            stop_reason = str(message.get("stopReason") or "").strip()
            error_text = str(message.get("errorMessage") or "").strip()
            if error_text and (stop_reason in {"error", "aborted"} or error_text):
                legacy_kind = stop_reason or "error"
                field = "errorMessage" if str(message.get("errorMessage") or "").strip() else "stopReason"
                error_kind, retryable = classify_error(
                    runtime="pi", kind=legacy_kind, message=error_text,
                )
                add("assistant_message", ts, message_id, record, text=error_text, usage=usage,
                    stop_reason=stop_reason or None,
                    error=AgentError(error_kind, error_text,
                                     Evidence(_NATIVE, field=field, record=record),
                                     None, retryable, "turn",
                                     native_http_status(error_text)))
    return events


def _is_empty_output(output: object) -> bool:
    if output is None:
        return True
    if isinstance(output, str):
        return not output.strip()
    if isinstance(output, (list, dict)):
        return not output
    return False


def _pi_outcome(branch: list[dict]) -> SessionOutcome:
    """Tail outcome mirroring the Pi ``status_tag`` rules with typed evidence.

    An assistant turn that only issued tool calls without any matching result
    is not completion: the run may still be working or the history truncated.
    Such a tail stays ``unknown`` instead of ``done``.
    """
    last_role: str | None = None
    last_stop_reason: str | None = None
    last_error_text: str | None = None
    tail_record = "message:0"
    issued_calls: set[str] = set()
    answered_calls: set[str] = set()
    for position, item in enumerate(branch, 1):
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "toolResult":
            call_id = message.get("toolCallId")
            if isinstance(call_id, str) and call_id:
                answered_calls.add(call_id)
        if role == "user":
            if scan_pi.message_text(message.get("content")):
                last_role = "user"
                last_stop_reason = None
                last_error_text = None
                tail_record = _record_ref(item, position)
        elif role == "assistant":
            stop_reason = str(message.get("stopReason") or "").strip() or None
            error_text = str(message.get("errorMessage") or "").strip()
            content = message.get("content")
            text = content.strip() if isinstance(content, str) else scan_pi.message_text(content)
            issued_here = False
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "toolCall":
                        call_id = part.get("id")
                        if isinstance(call_id, str) and call_id:
                            issued_calls.add(call_id)
                            issued_here = True
            if text:
                last_role = "assistant"
                last_stop_reason = stop_reason
                last_error_text = error_text or None
                tail_record = _record_ref(item, position)
            elif stop_reason in {"error", "aborted"} or error_text:
                last_role = "assistant"
                last_stop_reason = stop_reason or "error"
                last_error_text = error_text or None
                tail_record = _record_ref(item, position)
            elif issued_here:
                # 纯工具调用轮（无正文无错误）：助手侧回合，结果未到之前
                # 不能判 done；done 路径再查调用是否都有结果。
                last_role = "assistant"
                last_stop_reason = stop_reason
                last_error_text = error_text or None
                tail_record = _record_ref(item, position)
    if last_stop_reason in {"error", "aborted"}:
        kind = last_stop_reason
        detail = (last_error_text or kind).strip()
        field = "errorMessage" if last_error_text else "stopReason"
        return SessionOutcome(
            "aborted",
            Evidence(_NATIVE, field="stopReason", record=tail_record),
            error=AgentError(kind, detail, Evidence(_NATIVE, field=field, record=tail_record)),
        )
    if last_role == "user":
        return SessionOutcome("pending", Evidence(_INFERRED, field="tail_role", record=tail_record))
    if last_role == "assistant":
        if issued_calls - answered_calls:
            return SessionOutcome("unknown", Evidence(_UNKNOWN))
        return SessionOutcome("done", Evidence(_INFERRED, field="tail_role", record=tail_record))
    return SessionOutcome("unknown", Evidence(_UNKNOWN))


# --- Claude -----------------------------------------------------------------


def _load_claude(session: dict) -> ActivitySnapshot:
    path = str(session.get("path") or "")
    if not _readable(path):
        return ActivitySnapshot(
            state="unavailable",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    rows = _read_jsonl_rows(path)
    events = _build_claude_events(rows)
    if not events:
        return ActivitySnapshot(
            state="empty",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    return ActivitySnapshot(
        state="available",
        events=tuple(events),
        outcome=_claude_outcome(rows),
        # Claude history is a full JSONL read with no incremental boundary.
        cursor=None,
        generation=None,
    )


def _read_jsonl_rows(path: str) -> list[tuple[int, dict]]:
    """Parse JSONL rows with 1-based line numbers; malformed lines are skipped."""
    rows: list[tuple[int, dict]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    rows.append((lineno, entry))
    except OSError:
        pass
    return rows


def _claude_record(lineno: int) -> str:
    return f"line:{lineno}"


def _claude_entry_error_code(entry: dict) -> str | None:
    return scan_claude.native_error_code(entry)


def _claude_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _claude_clean_label(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _claude_usage(message: dict, record: str, entry: dict | None = None) -> Usage | None:
    """Native per-message model/usage; None when absent or unparseable.

    ``model`` stays the reported/effective ``message.model``. Attribution
    extras ride alongside from the entry/usage record: ``requested_model``
    from ``entry.requestedModel``, ``reasoning_level`` from
    ``perTurnEffort`` (else ``effort``), ``service_tier`` from
    ``usage.service_tier``. Absent stays None; nothing is estimated.
    """
    usage = message.get("usage")
    model = message.get("model")
    entry = entry if isinstance(entry, dict) else {}
    requested_model = _claude_clean_label(entry.get("requestedModel"))
    reasoning_level = _claude_clean_label(entry.get("perTurnEffort")) or _claude_clean_label(entry.get("effort"))
    service_tier = _claude_clean_label(usage.get("service_tier")) if isinstance(usage, dict) else None
    if (not isinstance(usage, dict) and not isinstance(model, str)
            and requested_model is None and reasoning_level is None and service_tier is None):
        return None
    fields: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    if isinstance(usage, dict):
        fields["input_tokens"] = _claude_int(usage.get("input_tokens"))
        fields["output_tokens"] = _claude_int(usage.get("output_tokens"))
        fields["cache_read_tokens"] = _claude_int(usage.get("cache_read_input_tokens"))
        fields["cache_write_tokens"] = _claude_int(usage.get("cache_creation_input_tokens"))
        details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            fields["reasoning_tokens"] = _claude_int(details.get("thinking_tokens"))
    if (all(value is None for value in fields.values()) and not isinstance(model, str)
            and requested_model is None and reasoning_level is None and service_tier is None):
        return None
    return Usage(
        Evidence(_NATIVE, field="usage", record=record),
        model=model.strip() if isinstance(model, str) and model.strip() else None,
        requested_model=requested_model,
        reasoning_level=reasoning_level,
        service_tier=service_tier,
        **fields,  # type: ignore[arg-type]
    )


def _claude_upstream_error(err_text: str, err_record: str, err_code: str | None,
                           field: str = "error.formatted") -> AgentError:
    """Classified upstream system error (401/5xx/retry bursts) with scope."""
    error_kind, retryable = classify_error(
        runtime="claude", kind="provider", code=err_code, message=err_text,
    )
    return AgentError(
        error_kind, err_text,
        Evidence(_NATIVE, field=field, record=err_record),
        err_code, retryable, "turn", native_http_status(err_code, err_text),
    )


class _ClaudeFeed:
    """Stateful row-sequence interpreter shared by snapshots and readers.

    ``_build_claude_events`` feeds every row at once; incremental readers
    feed one appended batch per poll while this object carries the pending
    upstream error, the tool call/result maps, and emission state across
    polls, so both paths run the same interpretation row for row. Linking
    (question answers) re-runs idempotently after each batch: a call whose
    result has not materialized yet keeps ``unknown`` resolution in the
    delivered stream and is enriched in the materialized list only.
    """

    def __init__(self, seq_start: int = 0) -> None:
        self._seq_start = seq_start
        self.events: list[ActivityEvent] = []
        self.pending_error: tuple[str, float | None, str, str | None, str] | None = None
        # call_id -> (event index, tool name, raw input, use row)
        self.calls: dict[str, tuple[int, str, object, int]] = {}
        # call_id -> (output, typed outcome, result row)
        self.results: dict[str, tuple[Any, ToolResultOutcome, int]] = {}
        self.skill_catalog: set[str] = set()
        self.pending_command: tuple[int | None, str, str | None, str] | None = None

    def _add(self, event_type: str, ts: float | None, record: str, **fields: Any) -> int | None:
        text = fields.get("text")
        if event_type in {"user_message", "assistant_message", "thinking"} and (
            not isinstance(text, str) or not text.strip()
        ):
            return None
        # Claude has no native turn id: turn_id stays None (the ActivityEvent
        # default). Callers pass message_id=message.id for assistant/thinking
        # slices; usage dedup across those slices stays in usage
        # reconciliation, never merged here.
        self.events.append(ActivityEvent(
            seq=self._seq_start + len(self.events) + 1,
            type=event_type,  # type: ignore[arg-type]
            evidence=Evidence(_NATIVE, record=record),
            ts=ts,
            **fields,
        ))
        return len(self.events) - 1

    def feed(self, rows: list[tuple[int, dict]]) -> list[ActivityEvent]:
        """Interpret one batch of ``(lineno, entry)`` rows, appending events."""
        before = len(self.events)
        for lineno, entry in rows:
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            record = _claude_record(lineno)
            if entry.get("isMeta"):
                if entry.get("type") == "user" and self.pending_command is not None:
                    meta_msg = entry.get("message")
                    meta_content = meta_msg.get("content") if isinstance(meta_msg, dict) else None
                    base_dir = scan_claude.is_skill_expansion_text(meta_content)
                    if base_dir is not None:
                        ev_idx, cmd_name, cmd_args, cmd_record = self.pending_command
                        if ev_idx is not None and 0 <= ev_idx < len(self.events):
                            confirmed = SkillInvocation(
                                name=cmd_name,
                                trigger="explicit",
                                detection="command",
                                evidence=Evidence(_NATIVE, field="command-name+isMeta", record=cmd_record),
                                arguments=cmd_args,
                                path=base_dir or None,
                            )
                            self.events[ev_idx] = replace(self.events[ev_idx], skills=(confirmed,))
                        self.pending_command = None
                continue
            err_text = scan_claude.system_error_text(entry)
            if err_text:
                field = "isApiErrorMessage" if entry.get("isApiErrorMessage") is True else "error.formatted"
                self.pending_error = (err_text, scan_claude.entry_time(entry), record,
                                      _claude_entry_error_code(entry), field)
                continue
            if entry.get("type") == "system":
                continue
            if entry.get("type") == "attachment":
                for listed in scan_claude.parse_skill_listing(entry):
                    self.skill_catalog.add(listed.lower())
                text = scan_claude.queued_command_text(entry)
                if text:
                    if self.pending_error is not None:
                        err_text, err_ts, err_record, err_code, err_field = self.pending_error
                        self._add("assistant_message", err_ts, err_record, text=err_text,
                                  error=_claude_upstream_error(err_text, err_record, err_code, err_field))
                        self.pending_error = None
                    # queued_command_text admits only explicit human evidence.
                    self._add("user_message", scan_claude.entry_time(entry), record,
                              text=text, origin="human")
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            ts = scan_claude.entry_time(entry)
            entry_type = entry.get("type")
            content = message.get("content")
            if entry_type == "user":
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "tool_result":
                            continue
                        call_id = str(part.get("tool_use_id") or "")
                        output = part.get("content")
                        is_error = part.get("is_error")
                        # Claude 只在失败时写 is_error=True；成功（含已回答的提问）
                        # 大多缺该键。缺键不是原生成功证据：空输出记 unknown，
                        # 非空按文本启发式记 inferred，投影到 v1 时仍为 "ok"。
                        if isinstance(is_error, bool):
                            outcome = ToolResultOutcome(
                                "error" if is_error else "ok",
                                Evidence(_NATIVE, field="message.content.is_error", record=record),
                            )
                        elif _is_empty_output(output):
                            outcome = ToolResultOutcome("unknown", Evidence(_UNKNOWN))
                        else:
                            outcome = ToolResultOutcome(
                                _heuristic_status(output),  # type: ignore[arg-type]
                                Evidence(_INFERRED, field="message.content", record=record),
                            )
                        self._add("tool_result", ts, record,
                                  call_id=call_id,
                                  raw_output=output,
                                  result=outcome)
                        self.results[call_id] = (output, outcome, lineno)
                origin = entry.get("origin")
                origin_kind = origin.get("kind") if isinstance(origin, dict) else None
                if origin_kind not in (None, "human"):
                    continue
                compact = entry.get("isCompactSummary") is True
                parsed_cmd = None if compact else scan_claude.parse_user_skill_command(content or "")
                user_skills: tuple[SkillInvocation, ...] = ()
                if parsed_cmd is not None:
                    cmd_name, cmd_args = parsed_cmd
                    if not scan_claude.is_builtin_command_name(cmd_name, catalog=self.skill_catalog):
                        user_skills = (
                            SkillInvocation(
                                name=cmd_name,
                                trigger="explicit",
                                detection="command",
                                evidence=Evidence(_NATIVE, field="command-name", record=record),
                                arguments=cmd_args,
                            ),
                        )
                text = scan_claude.extract_text(content or "")
                if text and not scan_claude.is_interrupted_marker(text):
                    if self.pending_error is not None:
                        err_text, err_ts, err_record, err_code, err_field = self.pending_error
                        self._add("assistant_message", err_ts, err_record, text=err_text,
                                  error=_claude_upstream_error(err_text, err_record, err_code, err_field))
                        self.pending_error = None
                    if compact:
                        # System-generated continuation summary in the user
                        # channel: kept projected (as today) but marked so
                        # consumers do not mistake it for typed human input.
                        self._add("user_message", ts, record, text=text, origin="system",
                                  compaction=CompactionInfo(
                                      Evidence(_NATIVE, field="isCompactSummary", record=record)))
                        self.pending_command = None
                    else:
                        add_kwargs: dict[str, Any] = {"text": text}
                        if origin_kind == "human":
                            add_kwargs["origin"] = "human"
                        if user_skills:
                            add_kwargs["skills"] = user_skills
                        ev_idx = self._add("user_message", ts, record, **add_kwargs)
                        if parsed_cmd is not None:
                            self.pending_command = (ev_idx, parsed_cmd[0], parsed_cmd[1], record)
                        else:
                            self.pending_command = None
                elif (compact or (isinstance(content, str) and content.strip())) and not scan_claude.is_interrupted_marker(text):
                    # Command/caveat wrappers strip to nothing: native injected
                    # chrome with no human text. Surfaced typed-only (v1 skips
                    # injected user messages, so projection bytes do not change).
                    raw = content.strip() if isinstance(content, str) else ""
                    if raw:
                        self._add("user_message", ts, record, text=raw, origin="injected")
                    self.pending_command = None
                continue
            if entry_type != "assistant" or not isinstance(content, list):
                continue
            self.pending_command = None
            usage = _claude_usage(message, record, entry)
            native_msg_id = message.get("id")
            native_msg_id = native_msg_id.strip() if isinstance(native_msg_id, str) and native_msg_id.strip() else None
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "thinking":
                    self._add("thinking", ts, record,
                              text=str(part.get("thinking") or part.get("text") or "").strip(),
                              usage=usage, message_id=native_msg_id)
                elif part_type == "text":
                    text = str(part.get("text") or "").strip()
                    if text:
                        self.pending_error = None
                    self._add("assistant_message", ts, record, text=text, usage=usage,
                              message_id=native_msg_id)
                elif part_type == "tool_use":
                    call_id = str(part.get("id") or "")
                    name = str(part.get("name") or "tool")
                    raw = _coerce_input(part.get("input"))
                    tskills = skill_from_tool_call(name, raw, record=record, call_id=call_id)
                    index = self._add(
                        "tool_call", ts, record, name=name, call_id=call_id, raw_input=raw,
                        skills=tskills,
                    )
                    if index is not None and call_id:
                        self.calls[call_id] = (index, name, raw, lineno)
        self._link_interactions()
        return self.events[before:]

    def flush_trailing_error(self) -> list[ActivityEvent]:
        """Emit a still-pending upstream error (snapshot end, or poll end).

        Readers call this at each poll end (optimistic emit): a live tail
        ending on an upstream failure surfaces immediately instead of
        waiting for rows that may never arrive. If the agent later recovers
        with reply text, the delivered stream keeps both while a later
        snapshot over the same bytes shows only the text; that edge is
        documented in the incremental-reader contract.
        """
        before = len(self.events)
        if self.pending_error is not None:
            err_text, err_ts, err_record, err_code, err_field = self.pending_error
            last = self.events[-1] if self.events else None
            if last is None or last.type != "assistant_message" or last.text != err_text:
                self._add("assistant_message", err_ts, err_record, text=err_text,
                          error=_claude_upstream_error(err_text, err_record, err_code, err_field))
            self.pending_error = None
        return self.events[before:]

    def _link_interactions(self) -> None:
        _attach_claude_interactions(self.events, self.calls, self.results)

    def finish_snapshot(self) -> list[ActivityEvent]:
        """End-of-history step for full loads: flush plus a final link pass."""
        flushed = self.flush_trailing_error()
        self._link_interactions()
        return flushed


def _build_claude_events(rows: list[tuple[int, dict]]) -> list[ActivityEvent]:
    """Mirror ``transcript._parse_claude`` emission order, with typed evidence.

    Error collapsing matches v1 exactly: consecutive upstream errors keep only
    the last one, a real assistant text discards the pending error, and a
    pending error flushes on real user text and at the end.
    """
    feed = _ClaudeFeed()
    feed.feed(rows)
    feed.finish_snapshot()
    return feed.events


def _claude_question_items(raw_input: object) -> tuple[QuestionItem, ...] | None:
    """Normalize an AskUserQuestion-shaped input; None when the shape is absent."""
    if not isinstance(raw_input, dict):
        return None
    questions = raw_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    items: list[QuestionItem] = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        options: list[QuestionOption] = []
        raw_options = item.get("options")
        if isinstance(raw_options, list):
            for option in raw_options:
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "").strip()
                if not label:
                    continue
                description = option.get("description")
                options.append(QuestionOption(
                    label,
                    str(description) if isinstance(description, str) else None,
                ))
        prompt = item.get("question")
        header = item.get("header")
        items.append(QuestionItem(
            prompt=str(prompt) if isinstance(prompt, str) else "",
            title=str(header) if isinstance(header, str) else None,
            options=tuple(options),
            multi_select=bool(item.get("multiSelect")),
            # The Other/notes free-text path is documented behavior, not
            # native-history evidence; the raw input is preserved regardless.
            free_text=True,
        ))
    return tuple(items) if items else None


def _attach_claude_interactions(
    events: list[ActivityEvent],
    calls: dict[str, tuple[int, str, object, int]],
    results: dict[str, tuple[Any, ToolResultOutcome, int]],
) -> None:
    """Attach ``InteractionRequest`` to question-shaped Claude tool calls.

    Purpose needs both the question classification and the native
    ``questions`` array shape, never the tool name alone. Resolution is
    ``answered`` only when a non-error result exists for the same call id
    (explicit ``is_error=False`` or a non-error inferred outcome); a missing
    or failed tool result leaves resolution ``unknown``, never ``pending``.
    Per-question answer linkage is recorded only for single-question calls
    with a non-empty string result; multi-question answers stay unlinked.
    """
    for call_id, (index, name, raw, lineno) in calls.items():
        if classify_tool(name) != "question":
            continue
        items = _claude_question_items(raw)
        if items is None:
            continue
        record = _claude_record(lineno)
        outcome = results.get(call_id)
        answers: tuple[AnswerRecord, ...] = ()
        if outcome is not None and outcome[1].status == "ok":
            output, result_outcome, _result_lineno = outcome
            resolution: str = "answered"
            resolution_evidence = result_outcome.evidence
            if len(items) == 1 and isinstance(output, str) and output.strip():
                answers = (AnswerRecord(0, output),)
        else:
            resolution = "unknown"
            resolution_evidence = Evidence(_UNKNOWN)
        event = events[index]
        events[index] = replace(
            event,
            interaction=InteractionRequest(
                "question",
                Evidence(_NATIVE, field="message.content", record=record),
                resolution=resolution,  # type: ignore[arg-type]
                resolution_evidence=resolution_evidence,
                tool_call_id=call_id or None,
                questions=items,
                answers=answers,
            ),
        )


class _ClaudeOutcomeAcc:
    """Stateful tail-outcome accumulator; ``_claude_outcome`` runs it over rows.

    Readers feed one appended batch per poll and read ``result()`` after
    each batch, so the incremental outcome always equals the snapshot
    outcome function over the same materialized rows.
    """

    def __init__(self) -> None:
        self.last_was_user: bool | str | None = None
        self.last_agent_msg: str | None = None
        self.last_content_lineno = -1
        self.last_error_text = ""
        self.last_error_field = "error.formatted"
        self.last_error_lineno = -1
        self.last_error_code: str | None = None
        self.issued_calls: set[str] = set()
        self.answered_calls: set[str] = set()

    def add(self, lineno: int, entry: dict) -> None:
        if not isinstance(entry, dict) or entry.get("isMeta") or entry.get("isSidechain"):
            return
        kind = entry.get("type")
        err_text = scan_claude.system_error_text(entry)
        if err_text:
            self.last_error_text = err_text
            self.last_error_lineno = lineno
            self.last_error_code = _claude_entry_error_code(entry)
            self.last_error_field = "isApiErrorMessage" if entry.get("isApiErrorMessage") is True else "error.formatted"
            return
        if kind == "user":
            content = (entry.get("message") or {}).get("content", "")
            if isinstance(content, list):
                for part in content:
                    if (isinstance(part, dict) and part.get("type") == "tool_result"
                            and part.get("tool_use_id")):
                        self.answered_calls.add(str(part.get("tool_use_id")))
            text = scan_claude.extract_text(content)
            if scan_claude.is_interrupted_marker(text):
                self.last_was_user = "aborted"
                self.last_content_lineno = lineno
            elif text:
                self.last_was_user = True
                self.last_content_lineno = lineno
        elif kind == "attachment":
            if scan_claude.queued_command_text(entry):
                self.last_was_user = True
                self.last_content_lineno = lineno
        elif kind == "assistant":
            content = (entry.get("message") or {}).get("content", [])
            if isinstance(content, list):
                issued_here = False
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_use" and part.get("id"):
                        self.issued_calls.add(str(part.get("id")))
                        issued_here = True
                    if part.get("type") == "text" and (part.get("text") or "").strip():
                        self.last_agent_msg = part["text"]
                        self.last_was_user = False
                        self.last_content_lineno = lineno
                        break
                else:
                    # 纯工具调用轮（无正文）：仍是助手侧回合，不能回落到
                    # 更早的用户文本去判 pending/done；done 路径再查是否有结果。
                    if issued_here:
                        self.last_was_user = False
                        self.last_content_lineno = lineno
        elif kind == "system":
            err_text = scan_claude.system_error_text(entry)
            if err_text:
                self.last_error_text = err_text
                self.last_error_lineno = lineno
                self.last_error_code = _claude_entry_error_code(entry)

    def result(self) -> SessionOutcome:
        if self.last_error_text and self.last_error_lineno > self.last_content_lineno:
            code = self.last_error_code
            return SessionOutcome(
                "aborted",
                Evidence(_NATIVE, field=self.last_error_field, record=_claude_record(self.last_error_lineno)),
                error=AgentError("provider", self.last_error_text,
                                 Evidence(_NATIVE, field=self.last_error_field,
                                          record=_claude_record(self.last_error_lineno)),
                                 code=code),
            )
        if self.last_was_user == "aborted":
            return SessionOutcome(
                "aborted",
                Evidence(_NATIVE, field="message.content", record=_claude_record(self.last_content_lineno)),
                error=AgentError("aborted", scan_claude.INTERRUPTED_MARKER,
                                 Evidence(_NATIVE, field="message.content",
                                          record=_claude_record(self.last_content_lineno))),
            )
        if self.last_was_user is True:
            return SessionOutcome(
                "pending",
                Evidence(_INFERRED, field="tail_role", record=_claude_record(self.last_content_lineno)))
        if self.last_was_user is False:
            # Same private prefix the list scan checks; kept as a module-attribute
            # reference so the two stay in sync.
            if (self.last_agent_msg or "").startswith(scan_claude._SESSION_LIMIT_PREFIX):
                return SessionOutcome(
                    "aborted",
                    Evidence(_NATIVE, field="message.content",
                             record=_claude_record(self.last_content_lineno)),
                    error=AgentError("provider", self.last_agent_msg or "",
                                     Evidence(_NATIVE, field="message.content",
                                              record=_claude_record(self.last_content_lineno))),
                )
            if self.issued_calls - self.answered_calls:
                return SessionOutcome("unknown", Evidence(_UNKNOWN))
            return SessionOutcome(
                "done",
                Evidence(_INFERRED, field="tail_role", record=_claude_record(self.last_content_lineno)))
        return SessionOutcome("unknown", Evidence(_UNKNOWN))


def _claude_outcome(rows: list[tuple[int, dict]]) -> SessionOutcome:
    """Tail outcome mirroring the Claude ``status_tag`` rules with typed evidence.

    The list scan reads a bounded tail window for speed; the activity loader
    already holds the full row sequence, so the same rules run over the full
    tail here. Both agree on the tail-most markers; the full read can only add
    older evidence, and the error/content ordering rule handles that.

    Tool calls without a matching result are not completion: an assistant turn
    that only issued calls (e.g. an unanswered ``AskUserQuestion``) stays
    ``unknown`` instead of ``done``.
    """
    acc = _ClaudeOutcomeAcc()
    for lineno, entry in rows:
        acc.add(lineno, entry)
    return acc.result()


def _claude_system_error_code(rows: list[tuple[int, dict]], lineno: int) -> str | None:
    for row_lineno, entry in rows:
        if row_lineno == lineno:
            return _claude_entry_error_code(entry)
    return None


# --- Codex ------------------------------------------------------------------


def _load_codex(session: dict) -> ActivitySnapshot:
    path = str(session.get("path") or "")
    if not _readable(path):
        return ActivitySnapshot(
            state="unavailable",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    rows = _read_jsonl_rows(path)
    events = _build_codex_events(rows)
    if not events:
        return ActivitySnapshot(
            state="empty",
            events=(),
            outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        )
    return ActivitySnapshot(
        state="available",
        events=tuple(events),
        outcome=_codex_outcome(rows),
        # Codex rollout 文件线性追加但 tool call/result 可能乱序到达；
        # 增量边界另行设计，这里不宣称 cursor。
        cursor=None,
        generation=None,
    )


def _codex_record(lineno: int) -> str:
    return f"line:{lineno}"


def _codex_turn_id(payload: dict) -> str | None:
    """Native turn linkage when the payload carries it; else None."""
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(meta, dict):
        turn_id = meta.get("turn_id")
        if isinstance(turn_id, str) and turn_id.strip():
            return turn_id.strip()
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str) and turn_id.strip():
        return turn_id.strip()
    return None


def _codex_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _codex_clean_label(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _codex_turn_model(payload: dict) -> str | None:
    """Model label from a ``turn_context`` payload; None when absent."""
    return _codex_clean_label(payload.get("model"))


def _codex_turn_effort(payload: dict) -> str | None:
    """Reasoning-level label from a ``turn_context`` payload; else None.

    Prefers the top-level ``effort`` selector, falling back to
    ``collaboration_mode.settings.reasoning_effort``. Both name a
    configuration-side strength selector, never a token count.
    """
    effort = _codex_clean_label(payload.get("effort"))
    if effort is not None:
        return effort
    collab = payload.get("collaboration_mode")
    if isinstance(collab, dict):
        settings = collab.get("settings")
        if isinstance(settings, dict):
            return _codex_clean_label(settings.get("reasoning_effort"))
    return None


def _codex_usage_from_record(
    payload: dict, record: str,
    turn_models: dict[str, str] | None = None,
    turn_efforts: dict[str, str] | None = None,
) -> Usage | None:
    """Native per-turn usage from a ``token_usage_record`` payload.

    Block priority is ``turn_token_usage`` (turn-cumulative, the per-turn
    attach scope), then ``usage`` (request-level absolute), then
    ``thread_token_usage`` (session-cumulative, last resort only): the
    Evidence ``field`` names the block actually hit, never a hardcoded
    name. All three blocks share the same counter-key layout.

    ``model`` prefers the record's own ``payload.model`` and falls back to
    the ``turn_context`` model for the same ``turn_id``; ``reasoning_level``
    comes from the ``turn_context`` effort selector for that turn (same
    rule as ``usage.collect_codex_observations``: native payload wins,
    turn map fills the gap). When this file carries no mapping, the turn
    id is still propagated and ``model`` stays None (honest): never guess.
    Per the ``Usage`` contract ``model`` stays the reported/effective
    model, never a request-time guess.
    """
    block_name: str | None = None
    block: dict | None = None
    for name in ("turn_token_usage", "usage", "thread_token_usage"):
        candidate = payload.get(name)
        if isinstance(candidate, dict):
            block = candidate
            block_name = name
            break
    if block is None or block_name is None:
        return None
    fields = {
        "input_tokens": _codex_int(block.get("input_tokens")),
        "output_tokens": _codex_int(block.get("output_tokens")),
        "cache_read_tokens": _codex_int(block.get("cached_input_tokens")),
        "cache_write_tokens": _codex_int(block.get("cache_write_input_tokens")),
        "reasoning_tokens": _codex_int(block.get("reasoning_output_tokens")),
        "total_tokens": _codex_int(block.get("total_tokens")),
    }
    turn_id = _codex_turn_id(payload)
    model = _codex_clean_label(payload.get("model"))
    if model is None and turn_id is not None and turn_models:
        model = turn_models.get(turn_id)
    reasoning_level: str | None = None
    if turn_id is not None and turn_efforts:
        reasoning_level = turn_efforts.get(turn_id)
    if all(value is None for value in fields.values()) and model is None:
        return None
    return Usage(
        Evidence(_NATIVE, field=block_name, record=record),
        model=model,
        reasoning_level=reasoning_level,
        **fields,  # type: ignore[arg-type]
    )


def _codex_injected_user_text(entry: dict) -> str | None:
    """Framework-injected user-channel text (AGENTS instructions, env context).

    The conversation parser drops these rows; they surface here as typed-only
    ``injected`` events so consumers need no prefix heuristics.
    """
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        str(part.get("text") or "").strip()
        for part in content
        if isinstance(part, dict) and part.get("type") == "input_text"
    ]
    text = "\n".join(part for part in parts if part).strip()
    if (
        text.startswith(("# AGENTS.md instructions", "<environment_context>"))
        or scan_codex.is_skill_context_text(text)
    ):
        return text or None
    return None


def _merge_codex_user_skills(
    existing: tuple[SkillInvocation, ...],
    incoming: tuple[SkillInvocation, ...],
) -> tuple[SkillInvocation, ...]:
    """Merge Codex user-message skills by normalized name, preferring native evidence."""
    merged = list(existing)
    by_norm: dict[str, int] = {normalize_skill_name(inv.name): idx for idx, inv in enumerate(merged)}
    for inv in incoming:
        key = normalize_skill_name(inv.name)
        idx = by_norm.get(key)
        if idx is None:
            by_norm[key] = len(merged)
            merged.append(inv)
            continue
        prev = merged[idx]
        if prev.evidence.origin == "inferred" and inv.evidence.origin == "native":
            merged[idx] = replace(inv, arguments=inv.arguments or prev.arguments)
        elif prev.arguments is None and inv.arguments is not None:
            merged[idx] = replace(prev, arguments=inv.arguments)
    return tuple(merged)


class _CodexFeed:
    """Stateful row-sequence interpreter shared by snapshots and readers.

    ``_build_codex_events`` feeds every row at once; incremental readers
    feed one appended batch per poll while this object carries the open
    native turn, the usage/verified maps, the tool call/result maps, and
    emission state across polls, so both paths run the same interpretation
    row for row. Usage and interaction linking re-run idempotently after
    each batch: linkage whose row has not materialized yet enriches the
    materialized list only, never re-emitted polls.
    """

    def __init__(self, seq_start: int = 0) -> None:
        self._seq_start = seq_start
        self.events: list[ActivityEvent] = []
        self.calls: dict[str, tuple[int, str, object, int]] = {}
        self.results: dict[str, tuple[Any, ToolResultOutcome, int]] = {}
        self.usages: dict[str, Usage] = {}
        self.turn_event_index: dict[str, int] = {}
        self.verified: dict[str, tuple[list[dict], int]] = {}
        # Native turn linkage: item_completed/task_complete/turn_aborted and
        # usage records carry turn_id; rows are chronological, so the last id
        # seen stamps every following event until the next native turn starts.
        self.current_turn: str | None = None
        # turn_id -> model / reasoning-level labels from native
        # turn_context rows (source: turn_context payload.model/effort
        # plus collaboration_mode.settings fallbacks, same rule as
        # usage.collect_codex_observations: native payload wins, turn map
        # fills the gap). Per the Usage contract model stays the
        # reported/effective model, never a request-time guess; absent
        # stays None, never guessed.
        self.turn_models: dict[str, str] = {}
        self.turn_efforts: dict[str, str] = {}

    def _add(self, event_type: str, ts: float | None, record: str, **fields: Any) -> int | None:
        text = fields.get("text")
        if event_type in {"user_message", "assistant_message", "thinking"} and (
            not isinstance(text, str) or not text.strip()
        ):
            return None
        if "turn_id" not in fields:
            fields["turn_id"] = self.current_turn
        self.events.append(ActivityEvent(
            seq=self._seq_start + len(self.events) + 1,
            type=event_type,  # type: ignore[arg-type]
            evidence=Evidence(_NATIVE, record=record),
            ts=ts,
            **fields,
        ))
        return len(self.events) - 1

    def _add_dedup(self, event_type: str, ts: float | None, record: str, text: str,
                   error: AgentError | None = None, origin: str = "unknown",
                   turn_id: str | None = None,
                   skills: tuple[SkillInvocation, ...] = ()) -> None:
        # Match the legacy adjacent-event rule. Non-text events break adjacency.
        if self.events and self.events[-1].type == event_type and self.events[-1].text == text:
            if skills and not self.events[-1].skills:
                self.events[-1] = replace(self.events[-1], skills=skills)
            return
        # Codex has no per-message native id: per-message granularity stops
        # at the turn. Keep message_id None explicitly; turn linkage carries
        # the identity (explicit turn first, else the open native turn).
        effective_turn = turn_id if turn_id is not None else self.current_turn
        if error is None:
            index = self._add(event_type, ts, record, text=text, origin=origin,  # type: ignore[arg-type]
                              message_id=None, turn_id=effective_turn, skills=skills)
        else:
            index = self._add(event_type, ts, record, text=text, error=error,  # type: ignore[arg-type]
                              origin=origin, message_id=None, turn_id=effective_turn, skills=skills)
        if index is not None and turn_id is not None and event_type == "assistant_message":
            self.turn_event_index[turn_id] = index

    def feed(self, rows: list[tuple[int, dict]]) -> list[ActivityEvent]:
        """Interpret one batch of ``(lineno, entry)`` rows, appending events."""
        before = len(self.events)
        # First pass: turn_context labels (model/effort per turn, source:
        # native turn_context rows) so usage records in this same batch
        # resolve identically to a full-file feed regardless of row order
        # within the batch.
        for _lineno, entry in rows:
            if str(entry.get("type")) != "turn_context":
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                # Some writers put turn_context fields at entry level.
                payload = entry
            turn_id = _codex_turn_id(payload) or _codex_clean_label(entry.get("turn_id"))
            if turn_id is None:
                continue
            model = _codex_turn_model(payload)
            if model is not None:
                self.turn_models.setdefault(turn_id, model)
            effort = _codex_turn_effort(payload)
            if effort is not None:
                self.turn_efforts.setdefault(turn_id, effort)
        for lineno, entry in rows:
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(entry.get("type")) == "token_usage_record":
                turn_id = _codex_turn_id(payload)
                usage = _codex_usage_from_record(payload, _codex_record(lineno),
                                                 self.turn_models, self.turn_efforts)
                if turn_id is not None and usage is not None:
                    self.usages.setdefault(turn_id, usage)
        for lineno, entry in rows:
            if str(entry.get("type")) == "compacted":
                payload = entry.get("payload")
                record = _codex_record(lineno)
                summary = ""
                if isinstance(payload, dict):
                    raw_message = payload.get("message")
                    summary = raw_message.strip() if isinstance(raw_message, str) else ""
                self.events.append(ActivityEvent(
                    seq=self._seq_start + len(self.events) + 1,
                    type="compaction",
                    evidence=Evidence(_NATIVE, record=record),
                    ts=scan_codex.entry_time(entry),
                    text=summary or None,
                    compaction=CompactionInfo(
                        Evidence(_NATIVE, field="payload.replacement_history", record=record)),
                ))
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            ts = scan_codex.entry_time(entry)
            record = _codex_record(lineno)
            kind = payload.get("type")
            native_turn = _codex_turn_id(payload)
            if native_turn is not None:
                self.current_turn = native_turn
            if kind == "turn_aborted":
                reason = str(payload.get("reason") or "").strip() or "turn_aborted"
                self._add("lifecycle", ts, record, text=f"turn_aborted: {reason}",
                          stop_reason=reason,
                          error=AgentError("user_interrupt", reason,
                                           Evidence(_NATIVE, field="payload.reason", record=record),
                                           None, False, "turn"))
                continue
            if entry.get("type") == "event_msg" and kind == "item_completed":
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") == "UserMessage":
                    content = item.get("content")
                    if isinstance(content, list):
                        item_skills = tuple(
                            SkillInvocation(
                                name=sname,
                                trigger="explicit",
                                detection="attached",
                                evidence=Evidence(_NATIVE, field="item_completed.UserMessage.skill", record=record),
                                path=str(part.get("path") or "").strip() or None,
                            )
                            for part in content
                            if isinstance(part, dict)
                            and part.get("type") == "skill"
                            and (sname := raw_skill_name(str(part.get("name") or "")))
                        )
                        if item_skills:
                            for idx in range(len(self.events) - 1, max(-1, len(self.events) - 9), -1):
                                ev = self.events[idx]
                                if ev.type != "user_message" or ev.origin != "human":
                                    continue
                                if ev.turn_id is not None and native_turn is not None and ev.turn_id != native_turn:
                                    continue
                                self.events[idx] = replace(
                                    ev,
                                    skills=_merge_codex_user_skills(ev.skills, item_skills),
                                )
                                break
                continue
            user_text = scan_codex.user_message_text(entry)
            if user_text:
                raw_content_text = ""
                if entry.get("type") == "event_msg":
                    raw_content_text = str(payload.get("message") or "").strip()
                elif entry.get("type") == "response_item":
                    raw_parts = payload.get("content")
                    if isinstance(raw_parts, list):
                        raw_content_text = "\n".join(
                            str(p.get("text") or "").strip()
                            for p in raw_parts
                            if isinstance(p, dict) and p.get("type") in ("input_text", "output_text")
                        )
                md_skills = tuple(
                    SkillInvocation(
                        name=raw_skill_name(m_name),
                        trigger="explicit",
                        detection="attached",
                        evidence=Evidence(_INFERRED, field="markdown_skill_link", record=record),
                        path=m_path,
                    )
                    for m_name, m_path, m_field in scan_codex.extract_codex_user_skill_mentions(raw_content_text or user_text)
                    if m_field == "markdown_link" and m_path
                )
                self._add_dedup("user_message", ts, record, user_text, origin="human", skills=md_skills)
                continue
            injected_text = _codex_injected_user_text(entry)
            if injected_text:
                # Dropped from the conversation view; typed-only here.
                ctx_skills = tuple(
                    SkillInvocation(
                        name=raw_skill_name(sname),
                        trigger="explicit",
                        detection="attached",
                        evidence=Evidence(_NATIVE, field="payload.content", record=record),
                        path=spath,
                    )
                    for sname, spath in scan_codex.parse_codex_skill_context(injected_text)
                )
                self._add("user_message", ts, record, text=injected_text, origin="injected", skills=ctx_skills)
                continue
            assistant_text = scan_codex.assistant_message_text(entry)
            if assistant_text:
                self._add_dedup("assistant_message", ts, record, assistant_text,
                                origin="human", turn_id=native_turn)
                continue
            if kind == "verified_answer":
                # Native answer record for a request_user_input call: linked by
                # call id in _attach_codex_interactions. No chat content, so no
                # event (v1 parity); only interaction metadata.
                call_id = str(payload.get("call_id") or "")
                questions = payload.get("questions")
                if call_id and isinstance(questions, list):
                    self.verified[call_id] = (
                        [q for q in questions if isinstance(q, dict)], lineno,
                    )
            if kind == "reasoning":
                self._add("thinking", ts, record, text=_codex_reasoning(payload))
            elif kind == "function_call":
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                name = str(payload.get("name") or "tool")
                raw = _coerce_input(payload.get("arguments"))
                tskills = skill_from_tool_call(name, raw, record=record, call_id=call_id)
                index = self._add("tool_call", ts, record, name=name, call_id=call_id, raw_input=raw, skills=tskills)
                if index is not None and call_id:
                    self.calls[call_id] = (index, name, raw, lineno)
            elif kind == "custom_tool_call":
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                name = str(payload.get("name") or "tool")
                raw_input = str(payload.get("input") or "")
                coerced = _codex_coerce_custom(raw_input)
                tskills = skill_from_tool_call(name, coerced, record=record, call_id=call_id)
                index = self._add("tool_call", ts, record,
                                  name=name, call_id=call_id, raw_input=coerced, skills=tskills)
                if index is not None and call_id:
                    self.calls[call_id] = (index, name, coerced, lineno)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                output = payload.get("output")
                call_id = str(payload.get("call_id") or "")
                if _is_empty_output(output):
                    outcome = ToolResultOutcome("unknown", Evidence(_UNKNOWN))
                else:
                    outcome = ToolResultOutcome(
                        _heuristic_status(output),  # type: ignore[arg-type]
                        Evidence(_INFERRED, field="payload.output", record=record),
                    )
                self._add("tool_result", ts, record,
                          call_id=call_id, raw_output=output, result=outcome)
                self.results[call_id] = (output, outcome, lineno)
            elif kind == "task_complete":
                text = str(payload.get("last_agent_message") or "").strip()
                error: AgentError | None = None
                if not text:
                    err_text = scan_codex.task_complete_error_text(payload)
                    if err_text:
                        text = err_text
                        raw_err = payload.get("error")
                        code = (
                            str(raw_err.get("codex_error_info") or "").strip()
                            if isinstance(raw_err, dict) else None) or None
                        error_kind, retryable = classify_error(
                            runtime="codex", kind="provider", code=code,
                            message=err_text,
                        )
                        error = AgentError(
                            error_kind, err_text,
                            Evidence(_NATIVE, field="payload.error", record=record),
                            code, retryable, "turn",
                            native_http_status(code, err_text),
                        )
                if text:
                    self._add_dedup("assistant_message", ts, record, text, error,
                                    origin="human", turn_id=native_turn)
                    # Native turn-end boundary for typed consumers (typed-only):
                    # the final text card alone cannot distinguish normal
                    # completion from mid-turn commentary. Always record the
                    # boundary with its native turn id, mirroring the
                    # turn_aborted lifecycle above.
                    self._add("lifecycle", ts, record, text="task_complete",
                              stop_reason="task_complete", turn_id=native_turn,
                              error=error)
                else:
                    # Bare completion marker: no chat content, but native
                    # completion evidence for turn derivation (typed-only).
                    self._add("lifecycle", ts, record, text="task_complete",
                              stop_reason="task_complete")
        self._link_late()
        return self.events[before:]

    def finish_snapshot(self) -> list[ActivityEvent]:
        """End-of-history step for full loads; Codex rows need no end flush."""
        return []

    def _link_late(self) -> None:
        """Attach usage/interaction rows that have materialized so far.

        Idempotent: re-running over the full materialized maps only fills
        linkage whose row arrived after the linked event was emitted. The
        delivered poll stream is never rewritten; the materialized list
        converges toward the snapshot over the same bytes. Turn-context
        labels (source: native turn_context rows, same rule as
        usage.collect_codex_observations) arriving after their usage row
        backfill the stored usage (model/reasoning_level only; token
        counts never change here).
        """
        for turn_id, usage in list(self.usages.items()):
            enriched = usage
            if enriched.model is None and turn_id in self.turn_models:
                enriched = replace(enriched, model=self.turn_models[turn_id])
            if enriched.reasoning_level is None and turn_id in self.turn_efforts:
                enriched = replace(enriched, reasoning_level=self.turn_efforts[turn_id])
            if enriched is not usage:
                self.usages[turn_id] = enriched
                usage = enriched
            index = self.turn_event_index.get(turn_id)
            if index is None:
                continue
            event = self.events[index]
            if event.usage is not None:
                # Refresh a previously attached usage that missed its
                # turn-context labels (source: turn_context rows; same
                # record, labels arrived late).
                current = event.usage
                refreshed = current
                if refreshed.model is None and usage.model is not None:
                    refreshed = replace(refreshed, model=usage.model)
                if refreshed.reasoning_level is None and usage.reasoning_level is not None:
                    refreshed = replace(refreshed, reasoning_level=usage.reasoning_level)
                if refreshed is not current:
                    self.events[index] = replace(event, usage=refreshed)
                continue
            self.events[index] = replace(event, usage=usage)
        _attach_codex_interactions(self.events, self.calls, self.results, self.verified)


def _build_codex_events(rows: list[tuple[int, dict]]) -> list[ActivityEvent]:
    """Mirror ``transcript._parse_codex`` emission order, with typed evidence.

    Codex tool 输出没有原生成功/失败标记：一律按文本启发式记 inferred，
    空输出记 unknown；v1 投影时仍为 "ok"/启发式值，与旧发射一致。相邻重复
    的用户/助手/task_complete 文本去重规则与旧发射相同。
    """
    feed = _CodexFeed()
    feed.feed(rows)
    return feed.events


def _codex_question_items(raw_input: object) -> tuple[QuestionItem, ...] | None:
    """Normalize a request_user_input-shaped input; None when shape is absent."""
    if not isinstance(raw_input, dict):
        return None
    questions = raw_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    items: list[QuestionItem] = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        options: list[QuestionOption] = []
        raw_options = item.get("options")
        if isinstance(raw_options, list):
            for option in raw_options:
                if isinstance(option, dict):
                    label = str(option.get("label") or "").strip()
                    if not label:
                        continue
                    description = option.get("description")
                    options.append(QuestionOption(
                        label,
                        str(description) if isinstance(description, str) else None,
                    ))
                elif isinstance(option, str) and option.strip():
                    # async 形态：options 是纯字符串数组。
                    options.append(QuestionOption(option.strip()))
        prompt = item.get("question", item.get("title"))
        header = item.get("header")
        items.append(QuestionItem(
            prompt=str(prompt) if isinstance(prompt, str) else "",
            title=str(header) if isinstance(header, str) else None,
            options=tuple(options),
            multi_select=bool(item.get("multiSelect", item.get("multiple", False))),
            # 历史答案里出现 "None of the above" 与 "user_note:" 自由文本，
            # 该运行时存在 Other/备注路径；原始输入完整保留。
            free_text=True,
        ))
    return tuple(items) if items else None


def _codex_answers(output: object) -> dict[str, list[str]] | None:
    """Parse structured answers; an empty map still identifies this shape."""
    if not isinstance(output, str) or not output.strip():
        return None
    try:
        parsed = json.loads(output)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if "answers" not in parsed:
        return None
    answers = parsed.get("answers")
    if not isinstance(answers, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key, entry in answers.items():
        if isinstance(entry, dict) and isinstance(entry.get("answers"), list):
            normalized[str(key)] = [
                str(v) for v in entry["answers"]
                if v is not None and str(v).strip()
            ]
        elif isinstance(entry, list):
            normalized[str(key)] = [
                str(v) for v in entry if v is not None and str(v).strip()
            ]
        elif isinstance(entry, str):
            normalized[str(key)] = [entry] if entry.strip() else []
    return normalized


def _attach_codex_interactions(
    events: list[ActivityEvent],
    calls: dict[str, tuple[int, str, object, int]],
    results: dict[str, tuple[Any, ToolResultOutcome, int]],
    verified: dict[str, tuple[list[dict], int]] | None = None,
) -> None:
    """Attach ``InteractionRequest`` to request_user_input-shaped Codex calls.

    Purpose needs both the question classification and the native
    ``questions`` array shape, never the tool name alone. Structured answers
    resolve only when a non-empty answer matches a native question id. Async
    acceptance/receipt text is not an answer; async requests require the same
    explicit, id-linked answer record. A non-structured sync string keeps the
    single-question fallback unless it is an acceptance receipt. Native
    ``verified_answer`` rows (call-id linked question/answer pairs) resolve
    with ``decided_by="human"``; answered tool outputs are likewise human
    decisions.
    """
    verified = verified or {}
    for call_id, (index, name, raw, lineno) in calls.items():
        if classify_tool(name) != "question":
            continue
        items = _codex_question_items(raw)
        if items is None:
            continue
        record = _codex_record(lineno)
        outcome = results.get(call_id)
        answers: tuple[AnswerRecord, ...] = ()
        resolution = "unknown"
        resolution_evidence: Evidence = Evidence(_UNKNOWN)
        decided_by: str = "unknown"
        entry = verified.get(call_id)
        if entry is not None:
            rows, answer_lineno = entry
            linked: list[AnswerRecord] = []
            for position, row in enumerate(rows):
                answer = row.get("answer")
                if isinstance(answer, str) and answer.strip():
                    linked.append(AnswerRecord(position, answer.strip(), (answer.strip(),)))
            if linked:
                answers = tuple(linked)
                resolution = "answered"
                decided_by = "human"
                resolution_evidence = Evidence(
                    _NATIVE, field="payload.questions",
                    record=_codex_record(answer_lineno),
                )
        if resolution == "unknown" and outcome is not None and outcome[1].status == "ok":
            output = outcome[0]
            linked = _codex_answers(output)
            if linked is not None:
                raw_questions = raw.get("questions") if isinstance(raw, dict) else None
                records: list[AnswerRecord] = []
                question_ids = [
                    str(question["id"]) if question.get("id") is not None else None
                    for question in (raw_questions or [])
                    if isinstance(question, dict)
                ]
                for i, qid in enumerate(question_ids):
                    if qid is not None and qid in linked:
                        picked = linked[qid]
                        if picked:
                            records.append(AnswerRecord(
                                i, "; ".join(picked), tuple(picked)))
                answers = tuple(records)
                if answers:
                    resolution = "answered"
                    decided_by = "human"
                    resolution_evidence = outcome[1].evidence
            elif isinstance(output, str) and output.strip():
                # Async request receipts have no answer in this history row.
                is_async = name.strip().lower() == "request_user_input_async"
                try:
                    parsed = json.loads(output)
                except ValueError:
                    parsed = None
                is_receipt = isinstance(parsed, dict) and "accepted" in parsed
                if not is_async and not is_receipt:
                    resolution = "answered"
                    decided_by = "human"
                    resolution_evidence = outcome[1].evidence
                    if len(items) == 1:
                        answers = (AnswerRecord(0, output),)
        event = events[index]
        events[index] = replace(
            event,
            interaction=InteractionRequest(
                "question",
                Evidence(_NATIVE, field="payload.arguments", record=record),
                resolution=resolution,  # type: ignore[arg-type]
                resolution_evidence=resolution_evidence,
                tool_call_id=call_id or None,
                questions=items,
                answers=answers,
                decided_by=decided_by,  # type: ignore[arg-type]
            ),
        )


class _CodexOutcomeAcc:
    """Stateful turn-outcome accumulator; ``_codex_outcome`` runs it over rows.

    Readers feed one appended batch per poll and read ``result()`` after
    each batch, so the incremental outcome always equals the snapshot
    outcome function over the same materialized rows.
    """

    def __init__(self) -> None:
        self.last_kind: str | None = None
        self.last_record = "line:0"
        self.last_error_text = ""
        self.last_error_code: str | None = None
        self.issued_calls: set[str] = set()
        self.answered_calls: set[str] = set()

    def add(self, lineno: int, entry: dict) -> None:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        record = _codex_record(lineno)
        kind = payload.get("type")
        if scan_codex.user_message_text(entry):
            # Older and newer Codex records can both represent the same prompt.
            # Resetting twice is harmless; this keeps unanswered calls from an
            # earlier completed turn from contaminating the latest turn.
            self.issued_calls.clear()
            self.answered_calls.clear()
            self.last_kind = "user_message"
            self.last_record = record
            self.last_error_text = ""
            self.last_error_code = None
            return
        if kind in {"function_call", "custom_tool_call"}:
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if call_id:
                self.issued_calls.add(call_id)
            if self.last_kind in {"task_complete", "task_complete_error", "turn_aborted"}:
                self.last_kind = "tool_activity"
                self.last_record = record
                self.last_error_text = ""
                self.last_error_code = None
            return
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            if call_id:
                self.answered_calls.add(call_id)
            if self.last_kind in {"task_complete", "task_complete_error", "turn_aborted"}:
                self.last_kind = "tool_activity"
                self.last_record = record
                self.last_error_text = ""
                self.last_error_code = None
            return
        if scan_codex.assistant_message_text(entry):
            self.last_kind = "agent_message"
            self.last_record = record
            self.last_error_text = ""
            self.last_error_code = None
        elif entry.get("type") == "event_msg" and kind == "task_complete":
            err_text = scan_codex.task_complete_error_text(payload)
            if err_text:
                self.last_kind = "task_complete_error"
                self.last_error_text = err_text
                err = payload.get("error")
                self.last_error_code = (
                    str(err.get("codex_error_info") or "").strip()
                    if isinstance(err, dict) else None) or None
            else:
                self.last_kind = "task_complete"
                self.last_error_text = ""
                self.last_error_code = None
            # A terminal marker closes this turn. Its unresolved calls must
            # not leak into a later assistant-only turn in the same thread.
            self.issued_calls.clear()
            self.answered_calls.clear()
            self.last_record = record
        elif entry.get("type") == "event_msg" and kind == "turn_aborted":
            self.last_kind = "turn_aborted"
            self.issued_calls.clear()
            self.answered_calls.clear()
            self.last_record = record
            self.last_error_text = ""
            self.last_error_code = None

    def result(self) -> SessionOutcome:
        if self.last_kind in ("turn_aborted", "task_complete_error"):
            kind = "aborted" if self.last_kind == "turn_aborted" else "provider"
            detail = self.last_error_text or self.last_kind
            field = "payload.type" if self.last_kind == "turn_aborted" else "payload.error"
            return SessionOutcome(
                "aborted",
                Evidence(_NATIVE, field=field, record=self.last_record),
                error=AgentError(kind, detail,
                                 Evidence(_NATIVE, field=field, record=self.last_record),
                                 code=self.last_error_code),
            )
        if self.last_kind == "task_complete":
            return SessionOutcome(
                "done", Evidence(_NATIVE, field="payload.type", record=self.last_record))
        if self.last_kind == "user_message":
            if self.issued_calls - self.answered_calls:
                return SessionOutcome("unknown", Evidence(_UNKNOWN))
            return SessionOutcome(
                "pending", Evidence(_INFERRED, field="tail_role", record=self.last_record))
        if self.last_kind == "agent_message":
            if self.issued_calls - self.answered_calls:
                return SessionOutcome("unknown", Evidence(_UNKNOWN))
            return SessionOutcome(
                "done", Evidence(_INFERRED, field="tail_role", record=self.last_record))
        return SessionOutcome("unknown", Evidence(_UNKNOWN))


def _codex_outcome(rows: list[tuple[int, dict]]) -> SessionOutcome:
    """Infer the latest Codex turn outcome from its tail records.

    Tool calls are scoped to the latest user turn. An unmatched call keeps an
    otherwise pending/assistant tail unknown, but a later native
    ``task_complete`` is authoritative completion evidence for that turn.
    """
    acc = _CodexOutcomeAcc()
    for lineno, entry in rows:
        acc.add(lineno, entry)
    return acc.result()
