"""Standalone typed activity adapter for OpenCode's SQLite histories."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from typing import Any

from sesskit.errors import classify_error, native_http_status
from sesskit.models import (
    ActivityEvent,
    ActivitySnapshot,
    AgentError,
    AnswerRecord,
    CompactionInfo,
    Evidence,
    InteractionRequest,
    LoadState,
    QuestionItem,
    QuestionOption,
    SessionOutcome,
    ToolResultOutcome,
    Usage,
)
from sesskit.parsers import opencode as scan_opencode
from sesskit.parsers.common import classify_tool

_NATIVE = "native"
_UNKNOWN = "unknown"
_V1_ROWS = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
       p.id AS part_id, p.time_created AS part_time, p.data AS part_data
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC
"""
_V2_ROWS = """
SELECT type, seq, time_created, id, data
FROM session_message
WHERE session_id = ?
ORDER BY seq ASC, time_created ASC, id ASC
"""


def load_opencode_activity(session: dict) -> ActivitySnapshot:
    """Load typed events for one scanned OpenCode session dictionary.

    The adapter reads v1 ``message``/``part`` and v2 ``session_message``
    histories directly. It advertises no incremental cursor or generation.
    """
    if str(session.get("source") or "") != "opencode":
        return _snapshot("unsupported")
    path = str(session.get("path") or "")
    session_id = str(session.get("id") or "")
    if not path or not session_id or not os.path.isfile(path):
        return _snapshot("unavailable")
    conn = scan_opencode.connect_ro(path)
    if conn is None:
        return _snapshot("unavailable")
    try:
        family = _history_family(conn, session_id)
        if family is None:
            return _snapshot("unsupported")
        if family == "v1":
            rows = conn.execute(_V1_ROWS, (session_id,)).fetchall()
            events, tail = _build_v1(rows, session_id)
            outcome = _v1_outcome(tail)
        else:
            rows = conn.execute(_V2_ROWS, (session_id,)).fetchall()
            events, outcome = _build_v2(rows, session_id)
    except sqlite3.Error:
        return _snapshot("unavailable")
    finally:
        conn.close()
    if not events:
        return ActivitySnapshot("empty", (), outcome)
    return ActivitySnapshot(
        "available", tuple(events), outcome, cursor=None, generation=None,
    )


def _snapshot(state: LoadState) -> ActivitySnapshot:
    return ActivitySnapshot(state, (), SessionOutcome("unknown", Evidence(_UNKNOWN)))


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _has_id(conn: sqlite3.Connection, table: str, column: str, value: str) -> bool:
    try:
        return conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (value,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _history_family(conn: sqlite3.Connection, session_id: str) -> str | None:
    tables = _tables(conn)
    v1_ready = {"session", "message", "part"} <= tables
    v2_ready = "session_message" in tables
    if not v1_ready and not v2_ready:
        return None
    if v1_ready and _has_id(conn, "session", "id", session_id):
        return "v1"
    if v2_ready and _has_id(conn, "session_message", "session_id", session_id):
        return "v2"
    if v2_ready and _has_id(conn, "session_v2", "id", session_id):
        return "v2"
    if v1_ready:
        return "v1"
    return "v2"


def _object(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 1000


def _coerce_args(value: object) -> object:
    if value is None:
        return {}
    if not isinstance(value, str) or not value.strip():
        return {} if isinstance(value, str) else value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _error(msg: dict, record: str, field: str) -> AgentError | None:
    text = scan_opencode.error_text_from_msg(msg)
    if not text:
        return None
    raw_error = msg.get("error")
    name = ""
    code: str | None = None
    if isinstance(raw_error, dict):
        name = str(raw_error.get("type") or raw_error.get("name") or "").strip()
        detail = raw_error.get("data")
        if isinstance(detail, dict):
            value = detail.get("statusCode", detail.get("status"))
            if value is not None and not isinstance(value, bool):
                code = str(value).strip() or None
        if code is None:
            value = raw_error.get("statusCode", raw_error.get("status"))
            if value is not None and not isinstance(value, bool):
                code = str(value).strip() or None
    kind = name or ("aborted" if "abort" in text.lower() else "provider")
    error_kind, retryable = classify_error(
        runtime="opencode", kind=kind, code=code, message=text,
    )
    return AgentError(
        error_kind, text, Evidence(_NATIVE, field=field, record=record),
        code, retryable, "turn", native_http_status(code, text),
    )


def _add(
    events: list[ActivityEvent], event_type: str, ts: float | None,
    message_id: str, record: str, field: str, **values: Any,
) -> int | None:
    text = values.get("text")
    if event_type in {"user_message", "assistant_message", "thinking"} and (
        not isinstance(text, str) or not text.strip()
    ):
        return None
    events.append(ActivityEvent(
        seq=len(events) + 1,
        type=event_type,  # type: ignore[arg-type]
        evidence=Evidence(_NATIVE, field=field, record=record),
        ts=ts,
        message_id=message_id,
        **values,
    ))
    return len(events) - 1


def _message_key(session_id: str, native_id: object) -> str:
    return f"opencode:{session_id}:message:{native_id}"


def _legacy_v1_output(state: dict, status: str) -> object:
    if status == "error":
        return state.get("error")
    if status == "completed":
        return state.get("output")
    return ""


def _v2_text_output(state: dict, status: str) -> str:
    if status != "completed":
        return ""
    texts = state.get("content")
    if not isinstance(texts, list):
        return ""
    return "\n\n".join(
        item["text"].strip()
        for item in texts
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    )


def _question_items(raw_input: object) -> tuple[QuestionItem, ...] | None:
    if not isinstance(raw_input, dict):
        return None
    raw_items = raw_input.get("questions")
    if not isinstance(raw_items, list) or not raw_items:
        return None
    items: list[QuestionItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        options: list[QuestionOption] = []
        raw_options = raw.get("options")
        if isinstance(raw_options, list):
            for option in raw_options:
                if isinstance(option, str) and option.strip():
                    options.append(QuestionOption(option.strip()))
                elif isinstance(option, dict):
                    label = option.get("label")
                    if isinstance(label, str) and label.strip():
                        description = option.get("description")
                        options.append(QuestionOption(
                            label.strip(),
                            description if isinstance(description, str) else None,
                        ))
        prompt = raw.get("question")
        if not isinstance(prompt, str):
            prompt = raw.get("title")
        title = raw.get("header")
        multi_select = raw.get("multiple", raw.get("multiSelect", False))
        items.append(QuestionItem(
            prompt=prompt.strip() if isinstance(prompt, str) else "",
            title=title.strip() or None if isinstance(title, str) else None,
            options=tuple(options),
            multi_select=multi_select if isinstance(multi_select, bool) else False,
        ))
    return tuple(items) if items else None


def _interaction(
    name: str, raw_input: object, call_id: str, status: str,
    text_output: str, record: str, field: str,
) -> InteractionRequest | None:
    if classify_tool(name) != "question":
        return None
    questions = _question_items(raw_input)
    if questions is None:
        return None
    answered = status == "completed" and bool(text_output.strip())
    answers = (
        (AnswerRecord(0, text_output),)
        if answered and len(questions) == 1 else ()
    )
    return InteractionRequest(
        purpose="question",
        evidence=Evidence(_NATIVE, field=field, record=record),
        resolution="answered" if answered else "unknown",
        resolution_evidence=(Evidence(_NATIVE, field="state.status", record=record)
                             if answered else Evidence(_UNKNOWN)),
        tool_call_id=call_id or None,
        questions=questions,
        answers=answers,
    )


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _opencode_usage(data: dict, record: str) -> Usage | None:
    """Native per-message model/token/cost; None when absent or unparseable.

    Accepts both the v2 assistant shape (``model`` object plus ``tokens``
    with a nested ``cache`` object) and the legacy v1 shape (``modelID``
    plus the same ``tokens`` layout).
    """
    tokens = data.get("tokens")
    model = data.get("modelID")
    if isinstance(data.get("model"), dict):
        raw_id = data["model"].get("id")
        if isinstance(raw_id, str) and raw_id.strip():
            model = raw_id.strip()
    if not isinstance(tokens, dict) and not isinstance(model, str):
        return None
    cache = tokens.get("cache") if isinstance(tokens, dict) else None
    cache = cache if isinstance(cache, dict) else {}
    fields = {
        "input_tokens": _int(tokens.get("input")) if isinstance(tokens, dict) else None,
        "output_tokens": _int(tokens.get("output")) if isinstance(tokens, dict) else None,
        "cache_read_tokens": _int(cache.get("read")),
        "cache_write_tokens": _int(cache.get("write")),
        "reasoning_tokens": _int(tokens.get("reasoning")) if isinstance(tokens, dict) else None,
        "total_tokens": None,
    }
    cost = data.get("cost")
    cost_value = (
        float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
        else None
    )
    if all(value is None for value in fields.values()) and cost_value is None and not isinstance(model, str):
        return None
    return Usage(
        Evidence(_NATIVE, field="data.tokens", record=record),
        model=model.strip() if isinstance(model, str) and model.strip() else None,
        cost=cost_value,
        **fields,  # type: ignore[arg-type]
    )


def _v1_part(
    row: sqlite3.Row, message: dict, session_id: str,
    events: list[ActivityEvent], assistant_text_seen: set[str],
) -> tuple[str | None, str | None]:
    part = _object(row["part_data"])
    if not part or part.get("synthetic") in (True, 1):
        return None, None
    mid = str(row["message_id"] or "")
    record = f"part:{row['part_id']}"
    msg_key = _message_key(session_id, mid)
    created = (message.get("time") or {}).get("created") if isinstance(message.get("time"), dict) else None
    ts = _timestamp(created) or _timestamp(row["part_time"])
    kind = part.get("type")
    role = message.get("role")
    if kind == "text":
        text = str(part.get("text") or "").strip()
        if role == "user":
            _add(events, "user_message", ts, msg_key, record, "part.data",
                 text=text, origin="human")
        elif role == "assistant":
            error = _error(message, f"message:{mid}", "message.data.error")
            index = _add(events, "assistant_message", ts, msg_key, record,
                         "part.data", text=text, error=error,
                         usage=_opencode_usage(message, f"message:{mid}"))
            if text:
                assistant_text_seen.add(mid)
            return None, str(index) if index is not None else None
    elif kind == "reasoning":
        _add(events, "thinking", ts, msg_key, record, "part.data",
             text=str(part.get("text") or "").strip())
    elif kind == "compaction":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        text = str(part.get("text") or state.get("summary") or state.get("text") or "").strip()
        _add(events, "thinking", ts, msg_key, record, "part.data", text=text,
             compaction=CompactionInfo(
                 Evidence(_NATIVE, field="part.data.type", record=record)))
    elif kind == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        call_id = str(part.get("callID") or row["part_id"] or "")
        name = str(part.get("tool") or "tool")
        raw_input = _coerce_args(state.get("input"))
        status = str(state.get("status") or "")
        output = _legacy_v1_output(state, status)
        question_output = output if isinstance(output, str) else ""
        interaction = _interaction(
            name, raw_input, call_id, status, question_output, record,
            "part.data.state.input",
        )
        _add(events, "tool_call", ts, msg_key, record, "part.data",
             name=name, call_id=call_id, raw_input=raw_input, interaction=interaction)
        if status in {"completed", "error"}:
            result_status = "error" if status == "error" else "ok"
            _add(events, "tool_result", ts, msg_key, record,
                 "part.data.state.status", call_id=call_id, raw_output=output,
                 result=ToolResultOutcome(
                     result_status, Evidence(_NATIVE, field="state.status", record=record),
                 ))
        return call_id, status
    return None, None


def _build_v1(
    rows: list[sqlite3.Row], session_id: str,
) -> tuple[list[ActivityEvent], dict]:
    events: list[ActivityEvent] = []
    error_emitted: set[str] = set()
    assistant_text_seen: set[str] = set()
    pending_errors: dict[str, tuple[dict, float | None]] = {}
    tail: dict[str, Any] = {"role": None, "message": {}, "record": None, "unresolved": False}
    calls_in_turn: set[str] = set()
    resolved_in_turn: set[str] = set()
    for row in rows:
        message = _object(row["msg_data"])
        if not message:
            continue
        mid = str(row["message_id"] or "")
        role = message.get("role")
        if role == "user":
            calls_in_turn.clear()
            resolved_in_turn.clear()
        tail = {
            "role": role, "message": message,
            "record": f"message:{mid}", "unresolved": False,
        }
        if row["part_data"] is None:
            if role == "assistant" and mid not in error_emitted:
                error = _error(message, f"message:{mid}", "message.data.error")
                if error is not None:
                    created = (message.get("time") or {}).get("created") if isinstance(message.get("time"), dict) else None
                    _add(events, "assistant_message", _timestamp(created),
                         _message_key(session_id, mid), f"message:{mid}",
                         "message.data.error", text=error.message, error=error)
                    error_emitted.add(mid)
                elif message.get("finish") != "stop":
                    # Part-less assistant row with no error and no completion:
                    # native tail evidence with no chat content (typed-only).
                    created = (message.get("time") or {}).get("created") if isinstance(message.get("time"), dict) else None
                    _add(events, "lifecycle", _timestamp(created),
                         _message_key(session_id, mid), f"message:{mid}",
                         "message.data", text="empty assistant message")
            continue
        call_id, tool_status = _v1_part(row, message, session_id, events, assistant_text_seen)
        if role == "assistant" and call_id:
            calls_in_turn.add(call_id)
            if tool_status in {"completed", "error"}:
                resolved_in_turn.add(call_id)
        if role == "assistant" and mid in assistant_text_seen:
            tail["assistant_text"] = True
        if role == "assistant" and _error(message, f"message:{mid}", "message.data.error"):
            pending_errors[mid] = (message, _timestamp(
                (message.get("time") or {}).get("created")
                if isinstance(message.get("time"), dict) else None
            ))
    # Legacy v1 emits errors from messages with parts after processing all parts.
    for mid, (message, ts) in pending_errors.items():
        if mid in assistant_text_seen or mid in error_emitted:
            continue
        error = _error(message, f"message:{mid}", "message.data.error")
        if error is not None:
            _add(events, "assistant_message", ts, _message_key(session_id, mid),
                 f"message:{mid}", "message.data.error", text=error.message, error=error)
    tail["unresolved"] = bool(calls_in_turn - resolved_in_turn)
    return events, tail


def _v1_outcome(tail: dict) -> SessionOutcome:
    message = tail.get("message")
    if not isinstance(message, dict):
        return SessionOutcome("unknown", Evidence(_UNKNOWN))
    record = str(tail.get("record") or "message:tail")
    error = _error(message, record, "message.data.error")
    if error is not None:
        return SessionOutcome("aborted", error.evidence, error)
    if tail.get("role") == "user":
        return SessionOutcome("pending", Evidence("inferred", field="tail_role", record=record))
    if tail.get("role") == "assistant" and message.get("finish") == "stop":
        if tail.get("unresolved"):
            return SessionOutcome("unknown", Evidence(_UNKNOWN))
        return SessionOutcome("done", Evidence(_NATIVE, field="message.data.finish", record=record))
    return SessionOutcome("unknown", Evidence(_UNKNOWN))


def _v2_part(
    item: dict, msg: dict, row_id: str, session_id: str,
    ts: float | None, events: list[ActivityEvent],
) -> tuple[str | None, str | None, bool]:
    msg_key = _message_key(session_id, row_id)
    record = f"session_message:{row_id}"
    kind = item.get("type")
    if kind == "text":
        text = item.get("text")
        err = _error(msg, record, "data.error")
        _add(events, "assistant_message", ts, msg_key, record, "data.content",
             text=text if isinstance(text, str) else "", error=err,
             usage=_opencode_usage(msg, record))
        return None, None, isinstance(text, str) and bool(text.strip())
    if kind != "tool":
        return None, None, False
    state = item.get("state") if isinstance(item.get("state"), dict) else {}
    call_id = str(item.get("id") or "")
    name = str(item.get("name") or "tool")
    status = str(state.get("status") or "")
    raw_input = _coerce_args(state.get("input"))
    question_text = _v2_text_output(state, status)
    interaction = _interaction(
        name, raw_input, call_id, status, question_text, record,
        "data.content.state.input",
    )
    _add(events, "tool_call", ts, msg_key, record, "data.content",
         name=name, call_id=call_id, raw_input=raw_input, interaction=interaction)
    if status in {"completed", "error"}:
        result_status = "error" if status == "error" else "ok"
        raw_output = state.get("error") if status == "error" else state.get("content")
        _add(events, "tool_result", ts, msg_key, record,
             "data.content.state.status", call_id=call_id, raw_output=raw_output,
             result=ToolResultOutcome(
                 result_status, Evidence(_NATIVE, field="state.status", record=record),
             ))
    return call_id, status, False


def _build_v2(
    rows: list[sqlite3.Row], session_id: str,
) -> tuple[list[ActivityEvent], SessionOutcome]:
    events: list[ActivityEvent] = []
    tail_role: str | None = None
    tail_data: dict = {}
    tail_record = "session_message:unknown"
    idle_failed: tuple[str, str] | None = None
    calls_in_turn: set[str] = set()
    resolved_in_turn: set[str] = set()
    tail_unresolved = False
    for row in rows:
        row_type = str(row["type"] or "")
        data = _object(row["data"])
        row_id = str(row["id"] or "")
        record = f"session_message:{row_id}"
        if row_type == "idle" and data.get("outcome") == "failed":
            idle_failed = (record, "data.outcome")
        if row_type in {"system", "synthetic", "compaction", "idle"}:
            if row_type == "compaction":
                _add(events, "thinking", _timestamp(data.get("time", {}).get("created")
                     if isinstance(data.get("time"), dict) else None) or _timestamp(row["time_created"]),
                     _message_key(session_id, row_id), record, "data.summary",
                     text=data.get("summary") if isinstance(data.get("summary"), str) else "",
                     compaction=CompactionInfo(
                         Evidence(_NATIVE, field="data.summary", record=record)))
            continue
        idle_failed = None
        ts = (_timestamp(data.get("time", {}).get("created")
             if isinstance(data.get("time"), dict) else None)
              or _timestamp(row["time_created"]))
        if row_type == "user":
            calls_in_turn.clear()
            resolved_in_turn.clear()
            text = data.get("text")
            _add(events, "user_message", ts, _message_key(session_id, row_id),
                 record, "data.text", text=text if isinstance(text, str) else "",
                 origin="human")
            tail_role, tail_data, tail_record = "user", data, record
            tail_unresolved = False
            continue
        if row_type != "assistant":
            tail_role = row_type or None
            tail_data, tail_record = data, record
            tail_unresolved = False
            continue
        content = data.get("content")
        items = content if isinstance(content, list) else []
        text_seen = False
        assistant_indexes: list[int] = []
        row_events_before = len(events)
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "reasoning":
                continue
            before = len(events)
            call_id, status, is_text = _v2_part(
                item, data, row_id, session_id, ts, events,
            )
            if item.get("type") == "tool":
                outcome_key = call_id or f"{row_id}:tool:{item_index}"
                calls_in_turn.add(outcome_key)
                if status in {"completed", "error"}:
                    resolved_in_turn.add(outcome_key)
            if is_text:
                text_seen = True
                if len(events) > before:
                    assistant_indexes.append(len(events) - 1)
        error = _error(data, record, "data.error")
        if error is not None:
            if text_seen and assistant_indexes:
                index = assistant_indexes[-1]
                events[index] = _copy_with_error(events[index], error)
            elif not text_seen:
                _add(events, "assistant_message", ts, _message_key(session_id, row_id),
                     record, "data.error", text=error.message, error=error)
        elif (not text_seen and len(events) == row_events_before
                and data.get("finish") != "stop"):
            # Content-less assistant row with no error and no completion:
            # native tail evidence with no chat content (typed-only).
            _add(events, "lifecycle", ts, _message_key(session_id, row_id),
                 record, "data.content", text="empty assistant message")
        tail_role, tail_data, tail_record = "assistant", data, record
        tail_unresolved = bool(calls_in_turn - resolved_in_turn)
    outcome = _v2_outcome(
        tail_role, tail_data, tail_record, tail_unresolved, idle_failed,
    )
    return events, outcome


def _copy_with_error(event: ActivityEvent, error: AgentError) -> ActivityEvent:
    return replace(event, error=error)


def _v2_outcome(
    role: str | None, data: dict, record: str, unresolved: bool,
    idle_failed: tuple[str, str] | None,
) -> SessionOutcome:
    error = _error(data, record, "data.error")
    if role == "assistant" and error is not None:
        return SessionOutcome("aborted", error.evidence, error)
    if role == "assistant" and data.get("finish") == "stop":
        if unresolved:
            return SessionOutcome("unknown", Evidence(_UNKNOWN))
        return SessionOutcome("done", Evidence(_NATIVE, field="data.finish", record=record))
    if role == "user":
        if idle_failed:
            err_record, field = idle_failed
            return SessionOutcome("aborted", Evidence(_NATIVE, field=field, record=err_record))
        return SessionOutcome("pending", Evidence("inferred", field="tail_role", record=record))
    if idle_failed:
        err_record, field = idle_failed
        return SessionOutcome("aborted", Evidence(_NATIVE, field=field, record=err_record))
    return SessionOutcome("unknown", Evidence(_UNKNOWN))
