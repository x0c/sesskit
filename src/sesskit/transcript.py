"""Unified transcript event stream across coding-agent runtimes."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any

from sesskit.parsers import claude as scan_claude
from sesskit.parsers import codex as scan_codex
from sesskit.parsers import cursor as scan_cursor
from sesskit.parsers import kimi as scan_kimi
from sesskit.parsers import opencode as scan_opencode
from sesskit.parsers import pi as scan_pi
from sesskit.parsers.common import classify_tool, parse_timestamp

SCHEMA_ID = "sesskit.transcript/v1"
LEGACY_SCHEMA_IDS = ("corral.share/v1",)
EVENT_TYPES = (
    "user_message",
    "assistant_message",
    "thinking",
    "tool_call",
    "tool_result",
)

_CODEX_EXEC_CMD_RE = re.compile(
    r'exec_command\(\s*\{.*?"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.S,
)


def load_events(session: dict) -> list[dict]:
    """按会话 ``source`` 解析原始历史，返回统一事件列表（seq 从 1 起、文件序）。"""
    runtime_id = str(session.get("source") or "")
    parser = _PARSERS.get(runtime_id)
    if parser is None:
        return []
    try:
        return parser(session)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return []


def count_events(events: list[dict]) -> dict[str, int]:
    counts = {name: 0 for name in EVENT_TYPES}
    for event in events:
        name = event.get("type")
        if name in counts:
            counts[name] += 1
    return counts


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(self, event_type: str, ts: float | None = None, **fields: Any) -> None:
        if event_type in {"user_message", "assistant_message", "thinking"}:
            text = fields.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            fields["text"] = text
        if event_type == "tool_call":
            fields["id"] = str(fields.get("id") or "")
            fields["name"] = str(fields.get("name") or "tool")
            fields["kind"] = classify_tool(fields["name"])
            if "input" not in fields:
                fields["input"] = {}
        if event_type == "tool_result":
            fields["call_id"] = str(fields.get("call_id") or "")
            fields["status"] = fields.get("status") or "ok"
            if "output" not in fields:
                fields["output"] = ""
        event: dict[str, Any] = {
            "type": event_type,
            "seq": len(self.events) + 1,
            "ts": ts,
        }
        for key, value in fields.items():
            if value is not None:
                event[key] = value
        self.events.append(event)


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    return {}


def _json_args(raw: object) -> dict | str | list | None:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return raw
        return parsed
    return raw


def _opencode_ts(value: object) -> float | None:
    """OpenCode 的 time_created / time.created 一律是毫秒。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 1000


def _text_of(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


_FAILURE_RE = re.compile(
    r"^(?:exit code:?\s*[1-9]|error:|traceback \(most recent call last\)|command failed|fatal:)",
    re.I | re.M,
)


def _failed(value: object, explicit: bool | None = None) -> str:
    if explicit is True:
        return "error"
    if explicit is False:
        return "ok"
    text = _text_of(value)
    return "error" if _FAILURE_RE.search(text[:600]) else "ok"


def _adjacent_dup(sink: _Sink, event_type: str, text: str) -> bool:
    if not sink.events:
        return False
    last = sink.events[-1]
    return last.get("type") == event_type and last.get("text") == text


# --- Claude -----------------------------------------------------------------


def _parse_claude(session: dict) -> list[dict]:
    path = str(session.get("path") or "")
    sink = _Sink()
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict) or entry.get("isMeta") or entry.get("isSidechain"):
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            ts = scan_claude.entry_time(entry)
            entry_type = entry.get("type")
            content = message.get("content")
            if entry_type == "user":
                origin = entry.get("origin")
                origin_kind = origin.get("kind") if isinstance(origin, dict) else None
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "tool_result":
                            continue
                        sink.add(
                            "tool_result",
                            ts,
                            call_id=str(part.get("tool_use_id") or ""),
                            status="error" if part.get("is_error") else "ok",
                            output=part.get("content"),
                        )
                if origin_kind not in (None, "human"):
                    continue
                text = scan_claude.extract_text(content or "")
                if text and text != scan_claude.INTERRUPTED_MARKER:
                    sink.add("user_message", ts, text=text)
                continue
            if entry_type != "assistant" or not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "thinking":
                    sink.add("thinking", ts, text=str(part.get("thinking") or part.get("text") or "").strip())
                elif part_type == "text":
                    sink.add("assistant_message", ts, text=str(part.get("text") or "").strip())
                elif part_type == "tool_use":
                    sink.add(
                        "tool_call",
                        ts,
                        id=str(part.get("id") or ""),
                        name=str(part.get("name") or "tool"),
                        input=_json_args(part.get("input")),
                    )
    return sink.events


# --- Codex ------------------------------------------------------------------


def _codex_custom_input(raw: str) -> dict | str:
    match = _CODEX_EXEC_CMD_RE.search(raw or "")
    if not match:
        parsed = _json_args(raw)
        return parsed if parsed else raw
    try:
        command = json.loads(f'"{match.group(1)}"')
    except ValueError:
        command = match.group(1)
    return {"cmd": command}


def _codex_reasoning_text(payload: dict) -> str:
    summary = payload.get("summary")
    parts: list[str] = []
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    elif isinstance(summary, list):
        for item in summary:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("summary") or item.get("content") or "").strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts)


def _parse_codex(session: dict) -> list[dict]:
    path = str(session.get("path") or "")
    sink = _Sink()
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            ts = scan_codex.entry_time(entry)
            kind = payload.get("type")
            user_text = scan_codex.user_message_text(entry)
            if user_text:
                if not _adjacent_dup(sink, "user_message", user_text):
                    sink.add("user_message", ts, text=user_text)
                continue
            assistant_text = scan_codex.assistant_message_text(entry)
            if assistant_text:
                if not _adjacent_dup(sink, "assistant_message", assistant_text):
                    sink.add("assistant_message", ts, text=assistant_text)
                continue
            if kind == "reasoning":
                sink.add("thinking", ts, text=_codex_reasoning_text(payload))
            elif kind == "function_call":
                sink.add(
                    "tool_call",
                    ts,
                    id=str(payload.get("call_id") or payload.get("id") or ""),
                    name=str(payload.get("name") or "tool"),
                    input=_json_args(payload.get("arguments")),
                )
            elif kind == "custom_tool_call":
                name = str(payload.get("name") or "tool")
                raw_input = str(payload.get("input") or "")
                sink.add(
                    "tool_call",
                    ts,
                    id=str(payload.get("call_id") or payload.get("id") or ""),
                    name=name,
                    input=_codex_custom_input(raw_input),
                )
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                output = payload.get("output")
                sink.add(
                    "tool_result",
                    ts,
                    call_id=str(payload.get("call_id") or ""),
                    status=_failed(output),
                    output=output,
                )
    return sink.events


# --- Kimi -------------------------------------------------------------------


def _parse_kimi(session: dict) -> list[dict]:
    path = str(session.get("path") or "")
    sink = _Sink()
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with handle:
        for entry in scan_kimi.iter_message_entries(handle):
            ts = scan_kimi.event_time(entry)
            user_text = scan_kimi.user_text(entry)
            if user_text is not None:
                sink.add("user_message", ts, text=user_text)
                continue
            event = entry.get("event")
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "content.part":
                part = event.get("part")
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "think":
                    sink.add("thinking", ts, text=str(part.get("think") or part.get("text") or "").strip())
                elif part.get("type") == "text":
                    sink.add("assistant_message", ts, text=str(part.get("text") or "").strip())
            elif event_type == "tool.call":
                sink.add(
                    "tool_call",
                    ts,
                    id=str(event.get("toolCallId") or event.get("uuid") or ""),
                    name=str(event.get("name") or "tool"),
                    input=_json_args(event.get("args") if event.get("args") is not None else event.get("arguments")),
                    description=event.get("description"),
                )
            elif event_type == "tool.result":
                result = event.get("result")
                output: object = result
                note = None
                if isinstance(result, dict):
                    output = result.get("output") if "output" in result else result
                    note = result.get("note")
                sink.add(
                    "tool_result",
                    ts,
                    call_id=str(event.get("toolCallId") or ""),
                    status=_failed(output),
                    output=output,
                    note=note,
                )
    return sink.events


# --- OpenCode ---------------------------------------------------------------


_OPENCODE_TRANSCRIPT_SQL = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data,
       p.id AS part_id, p.time_created AS part_time, p.data AS part_data
FROM message m JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
ORDER BY m.time_created ASC, m.id ASC, p.time_created ASC, p.id ASC
"""


def _parse_opencode(session: dict) -> list[dict]:
    db_path = str(session.get("path") or "")
    session_id = str(session.get("id") or "")
    sink = _Sink()
    conn = scan_opencode.connect_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(_OPENCODE_TRANSCRIPT_SQL, (session_id,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    for row in rows:
        try:
            msg = json.loads(row["msg_data"] or "{}") or {}
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(msg, dict):
            continue
        try:
            part = json.loads(row["part_data"] or "{}") or {}
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(part, dict) or part.get("synthetic") in (True, 1):
            continue
        role = msg.get("role")
        created = (msg.get("time") or {}).get("created") if isinstance(msg.get("time"), dict) else None
        ts = _opencode_ts(created) or _opencode_ts(row["part_time"])
        part_type = part.get("type")
        if part_type == "text":
            text = str(part.get("text") or "").strip()
            if role == "user":
                sink.add("user_message", ts, text=text)
            elif role == "assistant":
                sink.add("assistant_message", ts, text=text)
        elif part_type == "reasoning":
            sink.add("thinking", ts, text=str(part.get("text") or "").strip())
        elif part_type == "compaction":
            text = str(part.get("text") or "").strip()
            if not text and isinstance(part.get("state"), dict):
                text = str(part["state"].get("summary") or part["state"].get("text") or "").strip()
            sink.add("thinking", ts, text=text)
        elif part_type == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            call_id = str(part.get("callID") or row["part_id"] or "")
            sink.add(
                "tool_call",
                ts,
                id=call_id,
                name=str(part.get("tool") or "tool"),
                input=_json_args(state.get("input")),
            )
            status = str(state.get("status") or "")
            if status in {"completed", "error"}:
                output = state.get("output") if status == "completed" else state.get("error")
                sink.add(
                    "tool_result",
                    ts,
                    call_id=call_id,
                    status="error" if status == "error" else "ok",
                    output=output,
                )
    return sink.events


# --- Cursor -----------------------------------------------------------------


def _parse_cursor(session: dict) -> list[dict]:
    path = str(session.get("path") or "")
    store_path = path if path.endswith("store.db") else os.path.join(path, "store.db")
    sink = _Sink()
    if not os.path.isfile(store_path):
        return []
    conn = scan_cursor.connect_store_ro(store_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT rowid, data FROM blobs WHERE substr(data, 1, 1) = X'7B' ORDER BY rowid"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    pending_results: dict[str, dict] = {}
    emitted_calls: set[str] = set()

    def emit_call(ts, call_id: str, name: str, args) -> None:
        sink.add("tool_call", ts, id=call_id, name=name, input=_json_args(args))
        if call_id:
            emitted_calls.add(call_id)
            held = pending_results.pop(call_id, None)
            if held is not None:
                sink.add("tool_result", **held)

    def emit_result(call_id: str, output, status: str) -> None:
        payload = {"call_id": call_id, "status": status, "output": output}
        if call_id and call_id not in emitted_calls:
            pending_results[call_id] = payload
            return
        sink.add("tool_result", **payload)

    for _rowid, data in rows:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            continue
        raw = bytes(data)
        if not raw.startswith(b"{"):
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        role = obj.get("role")
        content = obj.get("content")
        if role == "user":
            text = scan_cursor.user_text_from_blob(obj)
            if text:
                sink.add("user_message", text=text)
            continue
        if role == "assistant":
            if not isinstance(content, list):
                text = str(content).strip() if isinstance(content, str) else ""
                sink.add("assistant_message", text=text)
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type in {"thinking", "reasoning"}:
                    sink.add("thinking", text=str(part.get("text") or part.get("thinking") or "").strip())
                elif part_type == "text":
                    sink.add("assistant_message", text=str(part.get("text") or "").strip())
                elif part_type == "tool-call":
                    emit_call(
                        None,
                        str(part.get("toolCallId") or ""),
                        str(part.get("toolName") or "tool"),
                        part.get("args"),
                    )
            continue
        if role == "tool" and isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool-result":
                    continue
                result = part.get("result")
                emit_result(str(part.get("toolCallId") or ""), result, _failed(result))
    for held in pending_results.values():
        sink.add("tool_result", **held)
    return sink.events


# --- Pi ---------------------------------------------------------------------


def _parse_pi(session: dict) -> list[dict]:
    path = str(session.get("path") or "")
    sink = _Sink()
    for item in scan_pi.active_messages(scan_pi.read_entries(path)):
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        ts = parse_timestamp(item.get("timestamp")) or parse_timestamp(message.get("timestamp"))
        role = message.get("role")
        if role == "user":
            sink.add("user_message", ts, text=scan_pi.message_text(message.get("content")))
            continue
        if role == "toolResult":
            output = message.get("content")
            explicit = message.get("isError")
            if explicit is None and message.get("error"):
                explicit = True
            sink.add(
                "tool_result",
                ts,
                call_id=str(message.get("toolCallId") or ""),
                status="error" if explicit else _failed(output, explicit if isinstance(explicit, bool) else None),
                output=output,
            )
            continue
        if role != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            sink.add("assistant_message", ts, text=content.strip())
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "thinking":
                sink.add("thinking", ts, text=str(part.get("thinking") or part.get("text") or "").strip())
            elif part_type == "text":
                sink.add("assistant_message", ts, text=str(part.get("text") or "").strip())
            elif part_type == "toolCall":
                sink.add(
                    "tool_call",
                    ts,
                    id=str(part.get("id") or ""),
                    name=str(part.get("name") or "tool"),
                    input=_json_args(
                        part.get("arguments") if part.get("arguments") is not None else part.get("input")
                    ),
                )
    return sink.events


_PARSERS = {
    "claude": _parse_claude,
    "codex": _parse_codex,
    "kimi": _parse_kimi,
    "opencode": _parse_opencode,
    "cursor": _parse_cursor,
    "pi": _parse_pi,
}
