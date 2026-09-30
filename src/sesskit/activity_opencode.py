"""Standalone typed activity adapter for OpenCode's SQLite histories."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from typing import Any

from sesskit.activity_reader import (
    ActivityReader,
    PageResult,
    PollResult,
    _encode_cursor,
    decode_reader_cursor,
    page_window,
)
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

# Append-delta queries: warm polls must not re-read the whole per-session
# window. New rows sort strictly after the committed position (range scans
# over the per-session ordering indexes); the recheck window below catches
# in-place content updates via the writer-maintained ``time_updated``
# columns plus per-row lengths. v1 needs two delta queries: strictly newer
# messages, plus brand-new parts of the still-open last message. Count
# growth must exactly match the fetched rows, otherwise a backfilled older
# row is hiding outside the suffix and the poll takes the slow path.
_V1_NEW_MESSAGES = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
       p.id AS part_id, p.time_created AS part_time, p.data AS part_data,
       m.time_updated AS msg_updated, p.time_updated AS part_updated
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND (m.time_created > ? OR (m.time_created = ? AND m.id > ?))
ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC
"""
_V1_NEW_PARTS = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
       p.id AS part_id, p.time_created AS part_time, p.data AS part_data,
       m.time_updated AS msg_updated, p.time_updated AS part_updated
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND m.id = ?
  AND p.id IS NOT NULL
  AND (p.time_created > ? OR (p.time_created = ? AND p.id > ?))
ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC
"""
_V1_LAST_MSG_PARTS = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
       p.id AS part_id, p.time_created AS part_time, p.data AS part_data,
       m.time_updated AS msg_updated, p.time_updated AS part_updated
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND m.id = ?
  AND p.id IS NOT NULL
ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC
"""
_V1_TAIL_SIG = """
SELECT m.id, m.time_created, p.id, p.time_created,
       m.time_updated, p.time_updated, length(m.data), length(p.data)
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND (m.time_created < ? OR (m.time_created = ? AND m.id <= ?))
ORDER BY m.time_created DESC, m.id DESC, p.time_created DESC, p.id DESC
LIMIT ?
"""
_V1_COUNTS_SQL = (
    "SELECT (SELECT COUNT(*) FROM message WHERE session_id = ?),"
    " (SELECT COUNT(*) FROM part WHERE session_id = ?)"
)
_V2_DELTA_ROWS = """
SELECT type, seq, time_created, id, data
FROM session_message
WHERE session_id = ?
  AND (seq > ? OR (seq = ? AND id > ?))
ORDER BY seq ASC, time_created ASC, id ASC
"""
_V2_TAIL_SIG = """
SELECT id, seq, time_created, time_updated, length(data)
FROM session_message
WHERE session_id = ?
  AND seq <= ?
ORDER BY seq DESC, time_created DESC, id DESC
LIMIT ?
"""
_V2_COUNT_SQL = "SELECT COUNT(*) FROM session_message WHERE session_id = ?"

# Bounded recheck window: trailing rows whose signature is compared before
# accepting an append without a rebuild. Same-count middle swaps beyond the
# window are the documented residual blind spot.
_OPENCODE_RECHECK_ROWS = 20

# Legacy schemas (older databases) lack the writer-maintained time_updated
# columns. The primary queries above use them for exact in-place-update
# detection; when SQLite reports a missing column the reader retries with
# these length-only variants (weaker, documented) instead of failing.
_V1_NEW_MESSAGES_LEGACY = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
       p.id AS part_id, p.time_created AS part_time, p.data AS part_data,
       NULL AS msg_updated, NULL AS part_updated
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND (m.time_created > ? OR (m.time_created = ? AND m.id > ?))
ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC
"""
_V1_NEW_PARTS_LEGACY = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
       p.id AS part_id, p.time_created AS part_time, p.data AS part_data,
       NULL AS msg_updated, NULL AS part_updated
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND m.id = ?
  AND p.id IS NOT NULL
  AND (p.time_created > ? OR (p.time_created = ? AND p.id > ?))
ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC
"""
_V1_LAST_MSG_PARTS_LEGACY = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
       p.id AS part_id, p.time_created AS part_time, p.data AS part_data,
       NULL AS msg_updated, NULL AS part_updated
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND m.id = ?
  AND p.id IS NOT NULL
ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC
"""
_V1_TAIL_SIG_LEGACY = """
SELECT m.id, m.time_created, p.id, p.time_created,
       NULL, NULL, length(m.data), length(p.data)
FROM message m LEFT JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND (m.time_created < ? OR (m.time_created = ? AND m.id <= ?))
ORDER BY m.time_created DESC, m.id DESC, p.time_created DESC, p.id DESC
LIMIT ?
"""
_V2_TAIL_SIG_LEGACY = """
SELECT id, seq, time_created, NULL, length(data)
FROM session_message
WHERE session_id = ?
  AND seq <= ?
ORDER BY seq DESC, time_created DESC, id DESC
LIMIT ?
"""


def _is_missing_column(exc: sqlite3.Error) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "no such column" in str(exc)


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
    """Snapshot path of ``_V1Feed``: identical output, one pass."""
    feed = _V1Feed(session_id)
    feed.feed(rows)
    tail, _flushed = feed.finish()
    return feed.events, tail


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


class _V1Feed:
    """Stateful row-sequence interpreter shared by snapshots and the reader.

    ``_build_v1`` feeds every row at once; the incremental reader feeds one
    appended batch per poll while this object carries the tail flags,
    per-turn call sets, text/error sets, and pending message errors across
    polls, so both paths run the same interpretation row for row.
    ``finish()`` emits message errors with no text yet (snapshot-of-prefix
    semantics, exactly the legacy end-of-history step). ``open_risk``
    records message ids whose interpretation later rows could still change
    (flushed errors, typed-only lifecycle rows). The reader takes the slow
    full re-read whenever delta rows touch ``open_risk``, arrive out of
    suffix order, fail count-growth accounting, or would follow previously
    flushed errors.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self.events: list[ActivityEvent] = []
        self.error_emitted: set[str] = set()
        self.assistant_text_seen: set[str] = set()
        self.pending_errors: dict[str, tuple[dict, float | None]] = {}
        self.tail: dict[str, Any] = {
            "role": None, "message": {}, "record": None, "unresolved": False}
        self.calls_in_turn: set[str] = set()
        self.resolved_in_turn: set[str] = set()
        self.open_risk: set[str] = set()
        self.flushed_total = 0

    def feed(self, rows: list) -> list[ActivityEvent]:
        """Interpret one batch of message/part rows, appending events.

        Batches are strictly newer than every previously fed row, so each
        row is interpreted exactly once. Returns the appended events.
        """
        before = len(self.events)
        for row in rows:
            message = _object(row["msg_data"])
            if not message:
                continue
            mid = str(row["message_id"] or "")
            role = message.get("role")
            if role == "user":
                self.calls_in_turn.clear()
                self.resolved_in_turn.clear()
            self.tail = {
                "role": role, "message": message,
                "record": f"message:{mid}", "unresolved": False,
            }
            if row["part_data"] is None:
                if role == "assistant" and mid not in self.error_emitted:
                    error = _error(message, f"message:{mid}", "message.data.error")
                    if error is not None:
                        created = ((message.get("time") or {}).get("created")
                                   if isinstance(message.get("time"), dict) else None)
                        _add(self.events, "assistant_message", _timestamp(created),
                             _message_key(self._session_id, mid), f"message:{mid}",
                             "message.data.error", text=error.message, error=error)
                        self.error_emitted.add(mid)
                        self.open_risk.add(mid)
                    elif message.get("finish") != "stop":
                        # Part-less assistant row with no error and no
                        # completion: native tail evidence with no chat
                        # content (typed-only).
                        created = ((message.get("time") or {}).get("created")
                                   if isinstance(message.get("time"), dict) else None)
                        _add(self.events, "lifecycle", _timestamp(created),
                             _message_key(self._session_id, mid), f"message:{mid}",
                             "message.data", text="empty assistant message")
                        self.open_risk.add(mid)
                continue
            call_id, tool_status = _v1_part(
                row, message, self._session_id, self.events, self.assistant_text_seen)
            if role == "assistant" and call_id:
                self.calls_in_turn.add(call_id)
                if tool_status in {"completed", "error"}:
                    self.resolved_in_turn.add(call_id)
            if role == "assistant" and mid in self.assistant_text_seen:
                self.tail["assistant_text"] = True
            if role == "assistant" and _error(
                    message, f"message:{mid}", "message.data.error"):
                self.pending_errors[mid] = (message, _timestamp(
                    (message.get("time") or {}).get("created")
                    if isinstance(message.get("time"), dict) else None
                ))
        return self.events[before:]

    def finish(self) -> tuple[dict, list[ActivityEvent]]:
        """Flush text-less message errors (the legacy end-of-history step).

        Matches the snapshot's end-of-history flush, so the materialized list
        always equals the snapshot over the rows seen so far. Unlike the
        retired inline loop, flushed ids join ``error_emitted`` so a later
        batch never emits the same error twice; later rows for a flushed
        message must take the slow path (tracked via ``open_risk``).
        Returns the tail dict plus the flushed events.
        """
        before = len(self.events)
        for mid, (message, ts) in self.pending_errors.items():
            if mid in self.assistant_text_seen or mid in self.error_emitted:
                continue
            error = _error(message, f"message:{mid}", "message.data.error")
            if error is not None:
                _add(self.events, "assistant_message", ts,
                     _message_key(self._session_id, mid),
                     f"message:{mid}", "message.data.error",
                     text=error.message, error=error)
                self.error_emitted.add(mid)
                self.open_risk.add(mid)
        flushed = self.events[before:]
        self.flushed_total += len(flushed)
        self.tail["unresolved"] = bool(self.calls_in_turn - self.resolved_in_turn)
        return self.tail, flushed


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
    """Snapshot path of ``_V2Feed``: identical output, one pass."""
    feed = _V2Feed(session_id)
    feed.feed(rows)
    return feed.events, feed.finish()


def _copy_with_error(event: ActivityEvent, error: AgentError) -> ActivityEvent:
    return replace(event, error=error)


def _copy_with_error(event: ActivityEvent, error: AgentError) -> ActivityEvent:
    return replace(event, error=error)


# --- Incremental reader -----------------------------------------------------


def _num(value: object) -> int:
    """Non-negative int for ordering keys; ``-1`` for missing/non-numeric."""
    if isinstance(value, bool):
        return -1
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return -1


def _file_state(path: str) -> tuple[int, int, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _event_key(event: ActivityEvent) -> tuple:
    """Stable identity for prefix comparison across rebuilds (no content)."""
    result = getattr(event, "result", None)
    interaction = getattr(event, "interaction", None)
    return (
        event.type,
        event.text,
        event.call_id,
        event.name,
        json.dumps(event.raw_input, sort_keys=True, default=str)
        if event.raw_input is not None else None,
        json.dumps(event.raw_output, sort_keys=True, default=str)
        if event.raw_output is not None else None,
        result.status if result is not None else None,
        getattr(event, "origin", None),
        getattr(interaction, "resolution", None) if interaction is not None else None,
    )


class OpenCodeActivityReader(ActivityReader):
    """Incremental reader over one OpenCode session's SQLite rows.

    Both schema families are supported: v1 ``message``/``part`` rows keyed
    by ``(time_created, id)`` ordering, and v2 ``session_message`` rows keyed
    by ``(seq, id)``. The shared database is opened read-only with
    WAL-visible tails (never ``immutable=1``); every query filters on the
    session id, so opening never scans other sessions (writes for other
    sessions cost only two small index queries and report no change). The
    opaque cursor carries a database fingerprint (file identity plus WAL
    state plus SQLite ``data_version``), the schema family, the last
    committed row position (ordering keys plus boundary checksum plus row
    counts), the event count, and compact interpreter aux state (open-risk
    messages, flushed error totals, tail record). A no-change poll stats the
    main file and its ``-wal`` sidecar only and never opens the database. A
    warm append poll reads only rows after the committed position plus a
    bounded recheck window (``time_updated`` plus per-row lengths) and feeds
    them through the carried ``_V1Feed``/``_V2Feed``; it never re-reads or
    rebuilds the whole per-session window. A vacuum, rowid reuse, checkpoint
    reshuffle that moves the boundary, in-place edit inside the window,
    file replacement, family change, or invalid cursor starts a new
    generation: the returned events replace, not extend, the previous
    generation. Deltas feed the same row-sequence builders as the snapshot
    (no second interpreter), so ``seq`` stays stable within a generation and
    ``to_v1_dicts`` parity holds.
    """

    def __init__(self, session: dict, cursor: Any = None) -> None:
        self._db_path = str(session.get("path") or "")
        self._session_id = str(session.get("id") or "")
        saved = decode_reader_cursor(cursor, "opencode")
        if saved is not None and (
            saved.get("path") != self._db_path or saved.get("session") != self._session_id
        ):
            saved = None
        self._pending_cursor = saved
        self._gen = 0
        self._family: str | None = None
        self._events: list[ActivityEvent] = []
        self._feed: _V1Feed | _V2Feed | None = None
        self._previous_events: list[ActivityEvent] = []
        self._position = ""
        self._pos_key: tuple = ()
        self._boundary = hashlib.sha256(b"").hexdigest()
        self._sig: tuple = ()
        self._counts: tuple = ()
        self._outcome = SessionOutcome("unknown", Evidence(_UNKNOWN))
        self._state: LoadState = "unavailable"
        self._file: tuple[int, int, int, int] | None = None
        self._wal: tuple[int, int, int, int] | None = None
        self._data_version = 0
        self._initialized = False
        # Test/observability counters: database opens and session rows
        # JSON-decoded for event building (recheck signature reads excluded).
        self.db_opens = 0
        self.rows_parsed = 0

    # -- public protocol -------------------------------------------------

    def poll(self) -> PollResult:
        self._sync()
        return PollResult(
            events=tuple(self._new_events),
            reset=self._last_reset,
            generation=str(self._gen),
            cursor=self._export_cursor(),
            outcome=self._outcome,
            state=self._state,
        )

    def page(self, before: str | None = None, limit: int = 50) -> PageResult:
        self._sync()
        return page_window(self._events, self._gen, before, limit)

    # -- sync machinery --------------------------------------------------

    def _sync(self) -> None:
        self._new_events: list[ActivityEvent] = []
        self._last_reset = False
        if not self._db_path or not self._session_id:
            self._mark_unavailable()
            return
        file_state = _file_state(self._db_path)
        wal_state = _file_state(self._db_path + "-wal")
        if file_state is None:
            self._mark_unavailable()
            return
        if not self._initialized:
            self._cold_open(file_state, wal_state)
            return
        cached_file = self._file or (-1, -1, -1, -1)
        if file_state[0] != cached_file[0] or file_state[1] != cached_file[1]:
            self._rebuild(file_state, wal_state)
            return
        if file_state == self._file and wal_state == self._wal:
            return
        self._refresh(file_state, wal_state)

    def _open(self) -> sqlite3.Connection | None:
        conn = scan_opencode.connect_ro(self._db_path)
        if conn is not None:
            self.db_opens += 1
        return conn

    @staticmethod
    def _fetch_data_version(conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute("PRAGMA data_version").fetchone()
        except sqlite3.Error:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def _read_session_rows(
        self, conn: sqlite3.Connection, family: str,
    ) -> list[dict[str, Any]] | None:
        try:
            if family == "v1":
                fetched = conn.execute(_V1_ROWS, (self._session_id,)).fetchall()
                rows = [
                    {
                        "message_id": row["message_id"],
                        "time_created": row["time_created"],
                        "msg_data": row["msg_data"],
                        "part_id": row["part_id"],
                        "part_time": row["part_time"],
                        "part_data": row["part_data"],
                    }
                    for row in fetched
                ]
            else:
                fetched = conn.execute(_V2_ROWS, (self._session_id,)).fetchall()
                rows = [
                    {
                        "type": row["type"],
                        "seq": row["seq"],
                        "time_created": row["time_created"],
                        "id": row["id"],
                        "data": row["data"],
                    }
                    for row in fetched
                ]
        except sqlite3.Error:
            return None
        self.rows_parsed += len(rows)
        return rows

    def _rebuild_events(
        self, family: str, rows: list[dict[str, Any]],
    ) -> tuple[Any, list[ActivityEvent], SessionOutcome]:
        """Fresh feed over full rows (slow path); returns the feed for keeps."""
        if family == "v1":
            feed: Any = _V1Feed(self._session_id)
            feed.feed(rows)
            tail, _flushed = feed.finish()
            return feed, feed.events, _v1_outcome(tail)
        feed = _V2Feed(self._session_id)
        feed.feed(rows)
        return feed, feed.events, feed.finish()

    def _sig_for(
        self, conn: sqlite3.Connection, family: str, rows: list[dict[str, Any]],
    ) -> tuple | None:
        """Tail signature scoped through the last full-read row."""
        pos_key = self._pos_key_of(family, rows[-1]) if rows else ()
        return self._tail_sig(conn, family, pos_key)

    def _tail_sig(
        self, conn: sqlite3.Connection, family: str, pos_key: tuple,
    ) -> tuple | None:
        """Bounded recheck-window signature over trailing rows at or before
        ``pos_key`` (appends past the position never move it)."""
        if family == "v1":
            mt, mid = pos_key[:2] if len(pos_key) == 4 else (-1, "")
            primary = (_V1_TAIL_SIG, (self._session_id, mt, mt, mid,
                                      _OPENCODE_RECHECK_ROWS))
            legacy = (_V1_TAIL_SIG_LEGACY, (self._session_id, mt, mt, mid,
                                            _OPENCODE_RECHECK_ROWS))
        else:
            seq = pos_key[0] if len(pos_key) == 2 else -1
            primary = (_V2_TAIL_SIG, (self._session_id, seq, _OPENCODE_RECHECK_ROWS))
            legacy = (_V2_TAIL_SIG_LEGACY, (self._session_id, seq, _OPENCODE_RECHECK_ROWS))
        try:
            fetched = conn.execute(*primary).fetchall()
        except sqlite3.Error as exc:
            if not _is_missing_column(exc):
                return None
            try:
                fetched = conn.execute(*legacy).fetchall()
            except sqlite3.Error:
                return None
        return tuple(tuple(row) for row in fetched)

    def _row_counts(
        self, conn: sqlite3.Connection, family: str,
    ) -> tuple | None:
        try:
            if family == "v1":
                row = conn.execute(
                    _V1_COUNTS_SQL, (self._session_id, self._session_id)).fetchone()
                if row is None:
                    return None
                return (int(row[0]), int(row[1]))
            row = conn.execute(_V2_COUNT_SQL, (self._session_id,)).fetchone()
            if row is None:
                return None
            return (int(row[0]),)
        except (sqlite3.Error, TypeError, ValueError):
            return None

    @staticmethod
    def _v1_dict(row: Any) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "time_created": row["time_created"],
            "msg_data": row["msg_data"],
            "part_id": row["part_id"],
            "part_time": row["part_time"],
            "part_data": row["part_data"],
            "msg_updated": row["msg_updated"],
            "part_updated": row["part_updated"],
        }

    def _read_delta_v1(
        self, conn: sqlite3.Connection,
    ) -> list[dict[str, Any]] | None:
        """Strictly newer message rows plus brand-new parts of the still-open
        last message, in snapshot order (last-message parts first). ``None``
        on SQLite errors."""
        pos = self._pos_key if len(self._pos_key) == 4 else (-1, "", -1, "")
        mt, mid, pt, pid = pos
        legacy = False
        try:
            new_msgs = conn.execute(
                _V1_NEW_MESSAGES, (self._session_id, mt, mt, mid)).fetchall()
        except sqlite3.Error as exc:
            if not _is_missing_column(exc):
                return None
            legacy = True
            try:
                new_msgs = conn.execute(
                    _V1_NEW_MESSAGES_LEGACY, (self._session_id, mt, mt, mid)).fetchall()
            except sqlite3.Error:
                return None
        parts: list = []
        if mid:
            try:
                if (pt, pid) == (-1, ""):
                    parts = conn.execute(
                        _V1_LAST_MSG_PARTS if not legacy else _V1_LAST_MSG_PARTS_LEGACY,
                        (self._session_id, mid)).fetchall()
                else:
                    parts = conn.execute(
                        _V1_NEW_PARTS if not legacy else _V1_NEW_PARTS_LEGACY,
                        (self._session_id, mid, pt, pt, pid)).fetchall()
            except sqlite3.Error:
                return None
        return [self._v1_dict(row) for row in parts] + [
            self._v1_dict(row) for row in new_msgs]

    def _read_delta_v2(
        self, conn: sqlite3.Connection,
    ) -> list[dict[str, Any]] | None:
        seq, rid = self._pos_key if len(self._pos_key) == 2 else (-1, "")
        try:
            fetched = conn.execute(
                _V2_DELTA_ROWS, (self._session_id, seq, seq, rid)).fetchall()
        except sqlite3.Error:
            return None
        return [
            {
                "type": row["type"],
                "seq": row["seq"],
                "time_created": row["time_created"],
                "id": row["id"],
                "data": row["data"],
            }
            for row in fetched
        ]

    def _position_of(self, family: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
        if not rows:
            return "", hashlib.sha256(b"").hexdigest()
        if family == "v1":
            last = rows[-1]
            position = (
                f"{last.get('time_created')}\x00{last.get('message_id')}"
                f"\x00{last.get('part_time')}\x00{last.get('part_id')}"
            )
            return position, _checksum(
                f"{position}\x00{last.get('msg_data')}\x00{last.get('part_data')}")
        last = rows[-1]
        position = f"{last.get('seq')}\x00{last.get('id')}"
        return position, _checksum(f"{position}\x00{last.get('data')}")

    @staticmethod
    def _pos_key_of(family: str, row: dict[str, Any]) -> tuple:
        if family == "v1":
            return (_num(row.get("time_created")), str(row.get("message_id") or ""),
                    _num(row.get("part_time")), str(row.get("part_id") or ""))
        return (_num(row.get("seq")), str(row.get("id") or ""))

    def _cold_open(
        self,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
    ) -> None:
        conn = self._open()
        if conn is None:
            self._mark_unavailable()
            return
        try:
            family = _history_family(conn, self._session_id)
            if family is None:
                self._adopt_unsupported()
                return
            data_version = self._fetch_data_version(conn)
            rows = self._read_session_rows(conn, family)
            sig = self._sig_for(conn, family, rows)
            counts = self._row_counts(conn, family)
        finally:
            conn.close()
        if rows is None or sig is None or counts is None:
            self._mark_unavailable()
            return
        saved = self._pending_cursor
        self._pending_cursor = None
        position, boundary = self._position_of(family, rows)
        feed, events, outcome = self._rebuild_events(family, rows)
        self._adopt_state(family, feed, rows, position, boundary, sig, counts,
                          events, outcome, data_version, file_state, wal_state,
                          fresh_gen=1)
        self._initialized = True
        if saved is not None and self._cursor_matches(saved):
            try:
                self._gen = max(int(saved.get("gen", 1)), 1)
            except (TypeError, ValueError):
                self._gen = 1
            self._last_reset = False
            self._new_events = []
        else:
            if saved is not None:
                self._gen += 1
            self._last_reset = True
            self._new_events = list(self._events)

    def _refresh(
        self,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
    ) -> None:
        """Stat changed: append-only delta when possible, else a full re-read.

        The fast path reads only rows after the committed position plus a
        bounded recheck window and feeds them through the carried feed. Any
        missing evidence or detected change falls back to the slow full
        re-read on the same connection, which decides delta versus reset by
        event-prefix comparison.
        """
        conn = self._open()
        if conn is None:
            # Transient lock or checkpoint race: keep the cached generation
            # and report no change rather than flapping to unavailable.
            return
        try:
            family = _history_family(conn, self._session_id)
            if family is None:
                self._adopt_unsupported()
                return
            data_version = self._fetch_data_version(conn)
            if family != self._family or not isinstance(self._feed, (_V1Feed, _V2Feed)):
                rows = self._read_session_rows(conn, family)
                if rows is None:
                    return
                self._adopt_new_generation_on(
                    conn, family, rows, data_version, file_state, wal_state)
                return
            if family == "v1":
                handled = self._try_v1_delta(conn, data_version, file_state, wal_state)
            else:
                handled = self._try_v2_delta(conn, data_version, file_state, wal_state)
            if handled is True or handled is None:
                return
            self._slow_on(conn, family, data_version, file_state, wal_state)
        finally:
            conn.close()

    def _adopt_new_generation_on(
        self,
        conn: sqlite3.Connection,
        family: str,
        rows: list[dict[str, Any]],
        data_version: int,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
    ) -> None:
        """Adopt a new generation over an open connection (family change)."""
        sig = self._sig_for(conn, family, rows)
        counts = self._row_counts(conn, family)
        if sig is None or counts is None:
            return
        self._adopt_new_generation_on_data(
            family, rows, sig, counts, data_version, file_state, wal_state)

    def _try_v1_delta(
        self,
        conn: sqlite3.Connection,
        data_version: int,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
    ) -> bool | None:
        """v1 fast path. True when handled, False for slow, None on transient."""
        feed = self._feed
        assert isinstance(feed, _V1Feed)
        try:
            counts = self._row_counts(conn, "v1")
            if counts is None:
                return None
            if counts[0] < self._counts[0] or counts[1] < self._counts[1]:
                return False
            sig = self._tail_sig(conn, "v1", self._pos_key)
            if sig is None or sig != self._sig:
                return False
            if counts == self._counts:
                self._file = file_state
                self._wal = wal_state
                self._data_version = data_version
                self._last_reset = False
                self._new_events = []
                return True
            rows = self._read_delta_v1(conn)
            if rows is None:
                return None
        except sqlite3.Error:
            return None
        # Suffix plus growth accounting: every count increment must be
        # explained by a fetched row, otherwise an older row was backfilled
        # outside the suffix and only a full rebuild can order it.
        pos_msg = self._pos_key[:2] if len(self._pos_key) == 4 else (-1, "")
        new_mids: set[str] = set()
        new_pids = 0
        for row in rows:
            key = self._pos_key_of("v1", row)
            if (key[0], key[1]) < pos_msg:
                return False
            if (key[0], key[1]) > pos_msg:
                new_mids.add(key[1])
            if row.get("part_id") is not None:
                new_pids += 1
        if len(new_mids) != counts[0] - self._counts[0]:
            return False
        if new_pids != counts[1] - self._counts[1]:
            return False
        if any(str(row.get("message_id") or "") in feed.open_risk for row in rows):
            return False
        flushed_before = feed.flushed_total
        fresh = feed.feed(rows)
        tail, flushed = feed.finish()
        if flushed_before > 0 and (fresh or flushed):
            return False
        self.rows_parsed += len(rows)
        outcome = _v1_outcome(tail)
        if not rows:
            return False
        position, boundary = self._position_of("v1", [rows[-1]])
        pos_key = self._pos_key_of("v1", rows[-1])
        new_sig = self._tail_sig(conn, "v1", pos_key)
        if new_sig is None:
            return False
        self._previous_events = list(self._events)
        self._events = feed.events
        self._outcome = outcome
        self._state = "available" if feed.events else "empty"
        self._position = position
        self._pos_key = pos_key
        self._boundary = boundary
        self._sig = new_sig
        self._counts = counts
        self._file = file_state
        self._wal = wal_state
        self._data_version = data_version
        self._last_reset = False
        self._new_events = list(fresh) + list(flushed)
        return True

    def _try_v2_delta(
        self,
        conn: sqlite3.Connection,
        data_version: int,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
    ) -> bool | None:
        """v2 fast path. True when handled, False for slow, None on transient."""
        feed = self._feed
        assert isinstance(feed, _V2Feed)
        try:
            counts = self._row_counts(conn, "v2")
            if counts is None:
                return None
            if counts[0] < self._counts[0]:
                return False
            sig = self._tail_sig(conn, "v2", self._pos_key)
            if sig is None or sig != self._sig:
                return False
            if counts == self._counts:
                self._file = file_state
                self._wal = wal_state
                self._data_version = data_version
                self._last_reset = False
                self._new_events = []
                return True
            rows = self._read_delta_v2(conn)
            if rows is None:
                return None
        except sqlite3.Error:
            return None
        for row in rows:
            if self._pos_key_of("v2", row) <= self._pos_key:
                return False
        # Growth accounting: seq is unique per session, so every new row
        # must be fetched; a backfilled older seq hides outside the suffix.
        if len(rows) != counts[0] - self._counts[0]:
            return False
        if not rows:
            return False
        self.rows_parsed += len(rows)
        fresh = feed.feed(rows)
        outcome = feed.finish()
        position, boundary = self._position_of("v2", [rows[-1]])
        pos_key = self._pos_key_of("v2", rows[-1])
        new_sig = self._tail_sig(conn, "v2", pos_key)
        if new_sig is None:
            return False
        self._previous_events = list(self._events)
        self._events = feed.events
        self._outcome = outcome
        self._state = "available" if feed.events else "empty"
        self._position = position
        self._pos_key = pos_key
        self._boundary = boundary
        self._sig = new_sig
        self._counts = counts
        self._file = file_state
        self._wal = wal_state
        self._data_version = data_version
        self._last_reset = False
        self._new_events = list(fresh)
        return True

    def _slow_on(
        self,
        conn: sqlite3.Connection,
        family: str,
        data_version: int,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
    ) -> None:
        """Slow full re-read over an open connection, then delta-or-reset."""
        rows = self._read_session_rows(conn, family)
        if rows is None:
            return
        sig = self._sig_for(conn, family, rows)
        counts = self._row_counts(conn, family)
        if sig is None or counts is None:
            return
        position, boundary = self._position_of(family, rows)
        feed, events, outcome = self._rebuild_events(family, rows)
        self._previous_events = list(self._events)
        self._family = family
        self._feed = feed
        self._events = events
        self._outcome = outcome
        self._state = "available" if events else "empty"
        self._position = position
        self._pos_key = self._pos_key_of(family, rows[-1]) if rows else ()
        self._boundary = boundary
        self._sig = sig
        self._counts = counts
        self._file = file_state
        self._wal = wal_state
        self._data_version = data_version
        if self._events_prefix_match():
            self._last_reset = False
            self._new_events = list(self._events[len(self._previous_events):])
        else:
            self._gen += 1
            self._last_reset = True
            self._new_events = list(self._events)

    def _rebuild(
        self,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
    ) -> None:
        conn = self._open()
        if conn is None:
            self._mark_unavailable()
            return
        try:
            family = _history_family(conn, self._session_id)
            if family is None:
                self._adopt_unsupported()
                return
            data_version = self._fetch_data_version(conn)
            rows = self._read_session_rows(conn, family)
            sig = self._sig_for(conn, family, rows)
            counts = self._row_counts(conn, family)
        finally:
            conn.close()
        if rows is None or sig is None or counts is None:
            self._mark_unavailable()
            return
        self._adopt_new_generation_on_data(
            family, rows, sig, counts, data_version, file_state, wal_state)
        self._initialized = True

    def _adopt_new_generation_on_data(
        self,
        family: str,
        rows: list[dict[str, Any]],
        sig: tuple,
        counts: tuple,
        data_version: int,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
    ) -> None:
        position, boundary = self._position_of(family, rows)
        feed, events, outcome = self._rebuild_events(family, rows)
        self._adopt_state(family, feed, rows, position, boundary, sig, counts,
                          events, outcome, data_version, file_state, wal_state,
                          fresh_gen=self._gen + 1)
        self._initialized = True
        self._last_reset = True
        self._new_events = list(self._events)

    def _adopt_state(
        self,
        family: str,
        feed: Any,
        rows: list[dict[str, Any]],
        position: str,
        boundary: str,
        sig: tuple,
        counts: tuple,
        events: list[ActivityEvent],
        outcome: SessionOutcome,
        data_version: int,
        file_state: tuple[int, int, int, int],
        wal_state: tuple[int, int, int, int] | None,
        fresh_gen: int,
    ) -> None:
        self._previous_events = list(self._events)
        self._family = family
        self._feed = feed
        self._events = events
        self._outcome = outcome
        self._state = "available" if events else "empty"
        self._position = position
        self._pos_key = self._pos_key_of(family, rows[-1]) if rows else ()
        self._boundary = boundary
        self._sig = sig
        self._counts = counts
        self._file = file_state
        self._wal = wal_state
        self._data_version = data_version
        self._gen = fresh_gen

    def _adopt_unsupported(self) -> None:
        self._state = "unsupported"
        self._events = []
        self._outcome = SessionOutcome("unknown", Evidence(_UNKNOWN))
        self._initialized = True
        self._gen = 1
        self._last_reset = True
        self._new_events = []

    def _events_prefix_match(self) -> bool:
        """True when the rebuild only appended: the cached event prefix is
        unchanged, so the poll is a delta. Anything else (vacuum, reuse,
        replacement, reorder, in-place edit) starts a new generation.
        """
        old = self._previous_events
        if len(self._events) < len(old):
            return False
        old_keys = [_event_key(event) for event in old]
        new_keys = [_event_key(event) for event in self._events[: len(old)]]
        return old_keys == new_keys

    def _mark_unavailable(self) -> None:
        had_history = self._initialized and self._gen > 0 and self._state != "unavailable"
        self._state = "unavailable"
        self._outcome = SessionOutcome("unknown", Evidence(_UNKNOWN))
        self._new_events = []
        self._last_reset = had_history

    def _aux_state(self) -> dict[str, Any]:
        """Compact interpreter aux state carried in the cursor (like the
        JSONL readers): open-risk messages / tail record plus bounded
        overflow flags."""
        feed = self._feed
        if isinstance(feed, _V1Feed):
            risk = sorted(feed.open_risk)
            return {
                "open_risk": risk[:500],
                "open_risk_truncated": len(risk) > 500,
                "flushed": feed.flushed_total,
                "tail": feed.tail.get("record"),
            }
        if isinstance(feed, _V2Feed):
            return {
                "tail": feed.tail_record,
                "calls": len(feed.calls_in_turn),
                "resolved": len(feed.resolved_in_turn),
            }
        return {}

    def _cursor_matches(self, saved: dict[str, Any]) -> bool:
        try:
            if saved.get("family") != self._family:
                return False
            if int(saved.get("dev", -1)) != (self._file or (-1,))[0]:
                return False
            if int(saved.get("ino", -1)) != (self._file or (-1, -1))[1]:
                return False
            if int(saved.get("data_version", -1)) != self._data_version:
                return False
            if saved.get("position") != self._position:
                return False
            if saved.get("boundary") != self._boundary:
                return False
            if int(saved.get("events", -1)) != len(self._events):
                return False
            if list(saved.get("counts", [])) != list(self._counts):
                return False
            if saved.get("aux", {}) != self._aux_state():
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _export_cursor(self) -> str | None:
        if self._state in {"unavailable", "unsupported"}:
            return None
        dev, ino, size, mtime_ns = self._file or (0, 0, 0, 0)
        return _encode_cursor({
            "v": 1,
            "runtime": "opencode",
            "path": self._db_path,
            "session": self._session_id,
            "family": self._family,
            "dev": dev,
            "ino": ino,
            "size": size,
            "mtime_ns": mtime_ns,
            "wal_size": self._wal[2] if self._wal else None,
            "wal_mtime_ns": self._wal[3] if self._wal else None,
            "data_version": self._data_version,
            "position": self._position,
            "boundary": self._boundary,
            "counts": list(self._counts),
            "events": len(self._events),
            "gen": self._gen,
            "aux": self._aux_state(),
        })


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


class _V2Feed:
    """Stateful row-sequence interpreter shared by snapshots and the reader.

    ``_build_v2`` feeds every row at once; the incremental reader feeds one
    appended batch per poll while this object carries the tail role/data,
    the ``idle`` failure flag, and the per-turn call sets across polls, so
    both paths run the same interpretation row for row. Every row is decided
    independently (no deferred flush), so appended batches extend the stream
    directly; ``finish()`` purely recomputes the tail outcome.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self.events: list[ActivityEvent] = []
        self.tail_role: str | None = None
        self.tail_data: dict = {}
        self.tail_record = "session_message:unknown"
        self.tail_unresolved = False
        self.idle_failed: tuple[str, str] | None = None
        self.calls_in_turn: set[str] = set()
        self.resolved_in_turn: set[str] = set()

    def feed(self, rows: list) -> list[ActivityEvent]:
        """Interpret one batch of ``session_message`` rows, appending events."""
        before = len(self.events)
        for row in rows:
            row_type = str(row["type"] or "")
            data = _object(row["data"])
            row_id = str(row["id"] or "")
            record = f"session_message:{row_id}"
            if row_type == "idle" and data.get("outcome") == "failed":
                self.idle_failed = (record, "data.outcome")
            if row_type in {"system", "synthetic", "compaction", "idle"}:
                if row_type == "compaction":
                    _add(self.events, "thinking", _timestamp(data.get("time", {}).get("created")
                         if isinstance(data.get("time"), dict) else None) or _timestamp(row["time_created"]),
                         _message_key(self._session_id, row_id), record, "data.summary",
                         text=data.get("summary") if isinstance(data.get("summary"), str) else "",
                         compaction=CompactionInfo(
                             Evidence(_NATIVE, field="data.summary", record=record)))
                continue
            self.idle_failed = None
            ts = (_timestamp(data.get("time", {}).get("created")
                  if isinstance(data.get("time"), dict) else None)
                  or _timestamp(row["time_created"]))
            if row_type == "user":
                self.calls_in_turn.clear()
                self.resolved_in_turn.clear()
                text = data.get("text")
                _add(self.events, "user_message", ts, _message_key(self._session_id, row_id),
                     record, "data.text", text=text if isinstance(text, str) else "",
                     origin="human")
                self.tail_role, self.tail_data, self.tail_record = "user", data, record
                self.tail_unresolved = False
                continue
            if row_type != "assistant":
                self.tail_role = row_type or None
                self.tail_data, self.tail_record = data, record
                self.tail_unresolved = False
                continue
            content = data.get("content")
            items = content if isinstance(content, list) else []
            text_seen = False
            assistant_indexes: list[int] = []
            row_events_before = len(self.events)
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "reasoning":
                    continue
                before_item = len(self.events)
                call_id, status, is_text = _v2_part(
                    item, data, row_id, self._session_id, ts, self.events,
                )
                if item.get("type") == "tool":
                    outcome_key = call_id or f"{row_id}:tool:{item_index}"
                    self.calls_in_turn.add(outcome_key)
                    if status in {"completed", "error"}:
                        self.resolved_in_turn.add(outcome_key)
                if is_text:
                    text_seen = True
                    if len(self.events) > before_item:
                        assistant_indexes.append(len(self.events) - 1)
            error = _error(data, record, "data.error")
            if error is not None:
                if text_seen and assistant_indexes:
                    index = assistant_indexes[-1]
                    self.events[index] = _copy_with_error(self.events[index], error)
                elif not text_seen:
                    _add(self.events, "assistant_message", ts,
                         _message_key(self._session_id, row_id),
                         record, "data.error", text=error.message, error=error)
            elif (not text_seen and len(self.events) == row_events_before
                    and data.get("finish") != "stop"):
                # Content-less assistant row with no error and no completion:
                # native tail evidence with no chat content (typed-only).
                _add(self.events, "lifecycle", ts, _message_key(self._session_id, row_id),
                     record, "data.content", text="empty assistant message")
            self.tail_role, self.tail_data, self.tail_record = "assistant", data, record
            self.tail_unresolved = bool(self.calls_in_turn - self.resolved_in_turn)
        return self.events[before:]

    def finish(self) -> SessionOutcome:
        """Recompute the tail outcome over the rows seen so far (pure)."""
        return _v2_outcome(
            self.tail_role, self.tail_data, self.tail_record,
            self.tail_unresolved, self.idle_failed,
        )
