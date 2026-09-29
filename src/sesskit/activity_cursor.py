"""Typed activity adapter for Cursor's persisted chat-store blobs.

This module is deliberately independent from the transcript parser: it mirrors
the current Cursor v1 event stream while adding evidence and typed question
details where the native records support them. No incremental cursor is exposed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import replace
from typing import Any

from sesskit.models import (
    ActivityEvent,
    ActivitySnapshot,
    Evidence,
    InteractionRequest,
    QuestionItem,
    QuestionOption,
    SessionOutcome,
    ToolResultOutcome,
)
from sesskit.parsers import cursor as scan_cursor
from sesskit.parsers.common import classify_tool

_NATIVE = "native"
_INFERRED = "inferred"
_UNKNOWN = "unknown"
_FAILURE_RE = re.compile(
    r"^(?:exit code:?\s*[1-9]|error:|traceback \(most recent call last\)|command failed|fatal:)",
    re.IGNORECASE | re.MULTILINE,
)


def load_cursor_activity(session: dict) -> ActivitySnapshot:
    """Load typed Cursor activity for one scanned session dictionary.

    Cursor's stored rows contain no reliable turn-completion marker, so the
    outcome remains unknown rather than inferring success or pending from the
    last visible message. ``cursor`` and ``generation`` are intentionally None.
    """
    store_path, chat_dir = _paths(session)
    if not store_path:
        return _snapshot("unavailable", [])
    if os.path.isfile(store_path):
        loaded, rows = _read_store(store_path)
        if loaded:
            events = _build_events(rows)
            if events:
                return _snapshot("available", events)
            prompts = _prompt_events(chat_dir)
            if prompts:
                return _snapshot("available", prompts)
            return _snapshot("empty", [])
    prompts, prompt_state = _read_prompt_events(chat_dir)
    if prompts:
        return _snapshot("available", prompts)
    if prompt_state == "empty":
        return _snapshot("empty", [])
    return _snapshot("unavailable", [])


def _snapshot(state: str, events: list[ActivityEvent]) -> ActivitySnapshot:
    return ActivitySnapshot(
        state=state,  # type: ignore[arg-type]
        events=tuple(events),
        outcome=SessionOutcome("unknown", Evidence(_UNKNOWN)),
        cursor=None,
        generation=None,
    )


def _paths(session: dict) -> tuple[str, str]:
    path = str(session.get("path") or "")
    if not path:
        return "", ""
    if path.endswith("store.db"):
        return path, os.path.dirname(path)
    return os.path.join(path, "store.db"), path


def _read_store(store_path: str) -> tuple[bool, list[tuple[int, object]]]:
    conn = scan_cursor.connect_store_ro(store_path)
    if conn is None:
        return False, []
    try:
        rows = conn.execute("SELECT rowid, data FROM blobs WHERE substr(data, 1, 1) = X'7B' ORDER BY rowid").fetchall()
    except sqlite3.Error:
        return False, []
    finally:
        conn.close()
    return True, rows


def _read_prompt_events(chat_dir: str) -> tuple[list[ActivityEvent], str]:
    prompt_path = os.path.join(chat_dir, "prompt_history.json")
    if not os.path.isfile(prompt_path):
        return [], "unavailable"
    try:
        with open(prompt_path, encoding="utf-8") as handle:
            prompts = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [], "unavailable"
    if not isinstance(prompts, list):
        return [], "unavailable"
    texts = [str(item).strip() for item in reversed(prompts) if str(item or "").strip()]
    return _user_events(texts, "prompt_history.json"), "empty"


def _prompt_events(chat_dir: str) -> list[ActivityEvent]:
    events, _state = _read_prompt_events(chat_dir)
    return events


def _user_events(texts: list[str], record: str) -> list[ActivityEvent]:
    return [
        ActivityEvent(
            seq=index,
            type="user_message",
            text=text,
            evidence=Evidence(_NATIVE, field="prompt_history", record=record),
            origin="human",
        )
        for index, text in enumerate(texts, 1)
    ]


def _build_events(rows: list[tuple[int, object]]) -> list[ActivityEvent]:
    events: list[ActivityEvent] = []
    calls: list[tuple[int, str, object, int]] = []
    pending: dict[str, tuple[object, ToolResultOutcome, int]] = {}
    emitted_calls: set[str] = set()

    def add(event_type: str, rowid: int, **fields: Any) -> int | None:
        text = fields.get("text")
        if event_type in {"user_message", "assistant_message", "thinking"} and (
            not isinstance(text, str) or not text.strip()
        ):
            return None
        events.append(
            ActivityEvent(
                seq=len(events) + 1,
                type=event_type,  # type: ignore[arg-type]
                evidence=Evidence(_NATIVE, record=f"rowid:{rowid}"),
                **fields,
            )
        )
        return len(events) - 1

    def add_result(call_id: str, output: object, rowid: int) -> None:
        outcome = _tool_outcome(output, rowid)
        add(
            "tool_result",
            rowid,
            call_id=call_id,
            raw_output=output,
            result=outcome,
        )

    for rowid, data in rows:
        obj = _decode_blob(data)
        if obj is None:
            continue
        role = obj.get("role")
        content = obj.get("content")
        if role == "user":
            text = scan_cursor.user_text_from_blob(obj)
            if text:
                add("user_message", rowid, text=text, origin="human")
            else:
                raw = scan_cursor._text_from_content(content)
                if isinstance(raw, str) and raw.strip():
                    # The conversation view drops <user_info>/rules context
                    # blocks; they surface here as typed-only injected events.
                    add("user_message", rowid, text=raw.strip(), origin="injected")
            continue
        if role == "assistant":
            if not isinstance(content, list):
                text = content.strip() if isinstance(content, str) else ""
                add("assistant_message", rowid, text=text)
                continue
            _assistant_parts(
                content,
                rowid,
                add,
                calls,
                pending,
                emitted_calls,
                add_result,
            )
            continue
        if role == "tool" and isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool-result":
                    continue
                call_id = str(part.get("toolCallId") or "")
                output = part.get("result")
                outcome = _tool_outcome(output, rowid)
                payload = (output, outcome, rowid)
                if call_id and call_id not in emitted_calls:
                    pending[call_id] = payload
                else:
                    add_result(call_id, output, rowid)

    for call_id, (output, _outcome, rowid) in pending.items():
        add_result(call_id, output, rowid)
    _attach_interactions(events, calls)
    return events


def _assistant_parts(
    content: list[object],
    rowid: int,
    add: Any,
    calls: list[tuple[int, str, object, int]],
    pending: dict[str, tuple[object, ToolResultOutcome, int]],
    emitted_calls: set[str],
    add_result: Any,
) -> None:
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"thinking", "reasoning"}:
            text = str(part.get("text") or part.get("thinking") or "").strip()
            add("thinking", rowid, text=text)
        elif part_type == "text":
            add("assistant_message", rowid, text=str(part.get("text") or "").strip())
        elif part_type == "tool-call":
            call_id = str(part.get("toolCallId") or "")
            name = str(part.get("toolName") or "tool")
            raw_input = _json_args(part.get("args"))
            index = add(
                "tool_call",
                rowid,
                name=name,
                call_id=call_id,
                raw_input=raw_input,
            )
            if index is None:
                continue
            calls.append((index, call_id, raw_input, rowid))
            if call_id:
                emitted_calls.add(call_id)
                held = pending.pop(call_id, None)
                if held is not None:
                    output, _outcome, result_rowid = held
                    add_result(call_id, output, result_rowid)


def _decode_blob(data: object) -> dict | None:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    if not raw.startswith(b"{"):
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _json_args(raw: object) -> object:
    if raw is None:
        return {}
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _tool_outcome(output: object, rowid: int) -> ToolResultOutcome:
    if not isinstance(output, str) or not output.strip():
        return ToolResultOutcome("unknown", Evidence(_UNKNOWN))
    if _FAILURE_RE.search(output[:600]):
        return ToolResultOutcome(
            "error",
            Evidence(_INFERRED, field="content[].result", record=f"rowid:{rowid}"),
        )
    return ToolResultOutcome("unknown", Evidence(_UNKNOWN))


def _question_items(raw_input: object) -> tuple[QuestionItem, ...] | None:
    if not isinstance(raw_input, dict):
        return None
    raw_questions = raw_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None
    items: list[QuestionItem] = []
    title = raw_input.get("title")
    for question in raw_questions:
        if not isinstance(question, dict):
            return None
        question_id = question.get("id")
        prompt = question.get("prompt")
        if not isinstance(question_id, (str, int)) or not str(question_id).strip():
            return None
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        options: list[QuestionOption] = []
        raw_options = question.get("options", [])
        if not isinstance(raw_options, list):
            return None
        for option in raw_options:
            if not isinstance(option, dict):
                return None
            option_id = option.get("id")
            label = option.get("label")
            if not isinstance(option_id, (str, int)) or not str(option_id).strip():
                return None
            if not isinstance(label, str) or not label.strip():
                return None
            options.append(QuestionOption(label.strip()))
        multi = question.get("allow_multiple") is True
        items.append(
            QuestionItem(
                prompt=prompt.strip(),
                title=title.strip() if isinstance(title, str) and title.strip() else None,
                options=tuple(options),
                multi_select=multi,
            )
        )
    return tuple(items)


def _attach_interactions(
    events: list[ActivityEvent],
    calls: list[tuple[int, str, object, int]],
) -> None:
    for index, call_id, raw_input, rowid in calls:
        event = events[index]
        if classify_tool(event.name or "") != "question":
            continue
        questions = _question_items(raw_input)
        if questions is None:
            continue
        events[index] = replace(
            event,
            interaction=InteractionRequest(
                purpose="question",
                evidence=Evidence(_NATIVE, field="args.questions", record=f"rowid:{rowid}"),
                resolution="unknown",
                resolution_evidence=Evidence(_UNKNOWN),
                tool_call_id=call_id or None,
                questions=questions,
            ),
        )
