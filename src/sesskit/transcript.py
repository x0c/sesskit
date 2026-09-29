"""Unified transcript event stream across coding-agent runtimes."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from sesskit.adapters import get_adapter
from sesskit.parsers import kimi as scan_kimi
from sesskit.parsers.common import classify_tool

SCHEMA_ID = "sesskit.transcript/v1"
LEGACY_SCHEMA_IDS: tuple[str, ...] = ()
"""Legacy wire identifiers accepted without a host: none.

A host that must still read its own historical payloads supplies them per
call via :func:`accepted_schema_ids` (or the host extension's
``legacy_schema_ids``); neutral readers accept only :data:`SCHEMA_ID`.
"""


def accepted_schema_ids(
    *,
    legacy_ids: tuple[str, ...] | list[str] = (),
) -> tuple[str, ...]:
    """Schema ids a reader accepts: the neutral id plus host legacy ids."""
    seen = [SCHEMA_ID]
    for ident in legacy_ids or ():
        text = str(ident or "")
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)
EVENT_TYPES = (
    "user_message",
    "assistant_message",
    "thinking",
    "tool_call",
    "tool_result",
)

_CODEX_EXEC_CMD_RE = re.compile(
    r'exec_command\(\s*\{.*?"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"',
    re.DOTALL,
)


def load_events(session: dict) -> list[dict]:
    """按会话 ``source`` 解析原始历史，返回统一事件列表（seq 从 1 起、文件序）。

    Dispatch goes through the runtime adapter registry; each adapter owns
    its v1 projection. Unknown runtimes and unreadable histories yield an
    empty list at this layer.
    """
    runtime_id = str(session.get("source") or "")
    try:
        adapter = get_adapter(runtime_id)
    except KeyError:
        return []
    try:
        return adapter.load_events(session)
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
    re.IGNORECASE | re.MULTILINE,
)


def _failed(value: object, explicit: bool | None = None) -> str:
    if explicit is True:
        return "error"
    if explicit is False:
        return "ok"
    text = _text_of(value)
    return "error" if _FAILURE_RE.search(text[:600]) else "ok"


# --- Claude -----------------------------------------------------------------


def _parse_claude(session: dict) -> list[dict]:
    # 单一解析源：typed 快照是真相，v1 只是兼容投影。
    # to_v1_dicts 对 Claude 事件不加 message_id，与旧发射完全一致。
    from sesskit.activity import load_activity, to_v1_dicts

    snapshot = load_activity(session)
    if snapshot.state != "available":
        return []
    return to_v1_dicts(snapshot)


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
    # 单一解析源：typed 快照是真相，v1 只是兼容投影（199/199 真实历史一致后切换）。
    from sesskit.activity import load_activity, to_v1_dicts

    snapshot = load_activity(session)
    if snapshot.state != "available":
        return []
    return to_v1_dicts(snapshot)



# --- Kimi -------------------------------------------------------------------


def _parse_kimi(session: dict) -> list[dict]:
    path = str(session.get("path") or "")
    sink = _Sink()
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
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
    except OSError:
        return []
    return sink.events


# --- Cursor / OpenCode -------------------------------------------------------


def _parse_opencode(session: dict) -> list[dict]:
    # The typed adapter is the sole raw-history interpretation source.
    from sesskit.activity import load_activity, to_v1_dicts

    snapshot = load_activity(session)
    if snapshot.state != "available":
        return []
    return to_v1_dicts(snapshot)


def _parse_cursor(session: dict) -> list[dict]:
    # The typed adapter is the sole raw-history interpretation source.
    from sesskit.activity import load_activity, to_v1_dicts

    snapshot = load_activity(session)
    if snapshot.state != "available":
        return []
    return to_v1_dicts(snapshot)


# --- Pi ---------------------------------------------------------------------


def _parse_pi(session: dict) -> list[dict]:
    # 单一解析源：typed 快照是真相，v1 只是兼容投影（含 message_id 分组）。
    from sesskit.activity import load_activity, to_v1_dicts

    snapshot = load_activity(session)
    if snapshot.state != "available":
        return []
    return to_v1_dicts(snapshot)
