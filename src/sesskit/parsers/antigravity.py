"""Scan Google Antigravity (agy / Antigravity IDE) local history.

Native stores (all under ``~``; read-only, never written):

- CLI / desktop transcripts (rich conversation text)::

      <root>/brain/<conversation-uuid>/.system_generated/logs/transcript.jsonl
      <root>/brain/<conversation-uuid>/.system_generated/logs/transcript_full.jsonl

  where ``<root>`` is one of ``~/.gemini/antigravity``,
  ``~/.gemini/antigravity-cli``, ``~/.gemini/antigravity-ide``,
  ``~/.gemini/antigravity-backup`` or ``~/.gemini/antigravity-acp``.
  Each line is one step (``step_index`` ordering key, ``type`` record kind).
  ``transcript_full.jsonl`` (when present) is the untruncated sibling of
  ``transcript.jsonl`` (``truncated_fields`` names clipped fields there).

- Conversation databases (usage + list metadata)::

      <root>/conversations/<conversation-uuid>.db
      <root>/conversation_summaries.db

  SQLite with ``trajectory_meta`` / ``trajectory_metadata_blob`` /
  ``gen_metadata`` / ``steps`` tables. Empty databases (no ``gen_metadata``
  or ``steps`` rows) are still real sessions; they list with unknown status
  and empty excerpts, mirroring the Cursor store-only rule.

Resume: ``agy --conversation <uuid>`` (``agy --continue`` for the latest).

Status mapping: an idle, non-killed transcript ending on a non-tool
``PLANNER_RESPONSE`` with ``status == "DONE"``, non-empty assistant answer
text (or non-tool media), zero open tool calls, and no pending non-daemon
background tasks maps to ``STATUS_DONE`` (with ``completion_id`` anchored strictly
on the tail reply). Live engine activity (``CASCADE_RUN_STATUS_RUNNING`` or
``not_fully_idle`` in ``conversation_summaries.db``), pending async work, open
tool calls, tool-result rows (including mid-turn tool ``ERROR``/``INVALID``),
``SYSTEM_MESSAGE`` tails, and ``USER_INPUT`` tails map to ``STATUS_PENDING``.
``summary.get("killed")``, ``ERROR_MESSAGE`` rows, or non-tool terminal rows
with ``ERROR``/``CANCELED``/``CANCELLED`` status map to ``STATUS_ABORTED`` with
deterministic ``completion_id_for`` and ``terminal_error_evidence``; anything else
(including non-killed sqlite-only rows) maps to ``STATUS_NONE`` (never default
to success). Subagent conversations marked in ``conversation_summaries.db`` or
``conversation_parents`` are excluded from ``scan_sessions``.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import shutil
import sqlite3
from typing import Any

from sesskit import titles
from sesskit.models import ConversationMessage, SessionInfo, effective_session_time, make_session_info
from sesskit.parsers.common import (
    ephemeral_prefixes_for,
    is_ephemeral_agent_cwd,
    parse_timestamp,
    preprocess_excerpt,
    stat_signature,
    terminal_error_evidence,
)

ROOT_NAMES = (
    ".gemini/antigravity",
    ".gemini/antigravity-cli",
    ".gemini/antigravity-ide",
    ".gemini/antigravity-backup",
    ".gemini/antigravity-acp",
    ".config/antigravity",
)

_TOOL_RESULT_TYPES = frozenset(
    {
        "RUN_COMMAND",
        "VIEW_FILE",
        "LIST_DIRECTORY",
        "CODE_ACTION",
        "SEARCH_WEB",
        "GREP_SEARCH",
        "GENERIC",
    }
)
_NON_TOOL_TYPES = frozenset(
    {"PLANNER_RESPONSE", "USER_INPUT", "CHECKPOINT", "SYSTEM_MESSAGE", "ERROR_MESSAGE"}
)
_ABORT_STATUSES = frozenset({"ERROR", "CANCELED", "CANCELLED"})
_CLOSED_TOOL_STATUSES = frozenset({"DONE", "ERROR", "INVALID"})
_TERMINAL_TASK_STATUSES = frozenset(
    {"DONE", "COMPLETED", "CANCELLED", "CANCELED", "ERROR", "FAILED", "STOPPED"}
)

_MODEL_SELECTION_RE = re.compile(
    r"`Model Selection`\s+from\s+.+?\s+to\s+(.+?)(?:\.(?=\s|<|$)|(?=<)|(?=\n)|$)"
)
_PBTXT_TITLE_RE = re.compile(r'(?m)^title:\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\')')
_FILE_URI_RE = re.compile(r"file://([A-Za-z0-9_\-./~%]+)")
_BG_TASK_ID_RE = re.compile(
    r"(?:^|\n)Tool is running as a background task with task id:\s*([^\s\"'\\]+)"
)
_SYS_MESSAGE_HEADER_RE = re.compile(r"(?:^|\n)\[Message\]\s")
_SYS_SENDER_RE = re.compile(r"(?:^|\s)sender=([^\s\]]+)")
_SYS_TASK_DONE_RE = re.compile(
    r'Task id\s+"([^"]+)"\s+(?:was\s+cancell?ed|finished|failed|stopped|ended)',
    re.IGNORECASE,
)
_TASK_CANCELLED_RE = re.compile(
    r'(?:cannot kill task|Task)\s+"([^"]+)"\s*(?::\s*task is not running|cancelled|canceled|killed|stopped)',
    re.IGNORECASE,
)
_WAIT_CANCELLED_RE = re.compile(
    r"Wait cancelled\s*\(task\s+([^\s)]+)\s+has already finished\)",
    re.IGNORECASE,
)
_TASK_STATUS_RE = re.compile(r"Task:\s*(\S+)\s+Status:\s*([A-Z_]+)", re.IGNORECASE)
_ASYNC_SUBAGENT_PHRASES = (
    "running in the background",
    "running asynchronously",
    "background task",
    "will be notified",
    "launched in the background",
)


def _roots() -> list[str]:
    home = os.path.expanduser("~")
    found: list[str] = []
    for name in ROOT_NAMES:
        path = os.path.join(home, name)
        if os.path.isdir(path):
            found.append(path)
    return found


def _conversation_id_from_transcript(path: str) -> str | None:
    parts = path.replace(os.sep, "/").split("/")
    if "brain" in parts:
        idx = parts.index("brain")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    base = os.path.basename(path)
    if base.endswith(".jsonl"):
        return os.path.splitext(base)[0]
    return None


def unwrap_user_request(content: object) -> str:
    """Return the human prompt inside an Antigravity USER_INPUT record."""
    text = content if isinstance(content, str) else ""
    start = text.find("<USER_REQUEST>")
    end = text.find("</USER_REQUEST>")
    if start != -1 and end != -1 and end > start:
        return text[start + len("<USER_REQUEST>") : end].strip()
    out = text
    for marker in ("<ADDITIONAL_METADATA>", "<USER_SETTINGS_CHANGE>"):
        pos = out.find(marker)
        if pos != -1:
            out = out[:pos]
    return out.strip()


def model_name_from_user_input(content: object) -> str | None:
    """Recover the model name from a USER_SETTINGS_CHANGE block, if present."""
    text = content if isinstance(content, str) else ""
    open_tag = "<USER_SETTINGS_CHANGE>"
    pos = text.rfind(open_tag)
    if pos == -1:
        return None
    block = text[pos + len(open_tag) :]
    close = block.find("</USER_SETTINGS_CHANGE>")
    if close != -1:
        block = block[:close]
    match = _MODEL_SELECTION_RE.search(block)
    if not match:
        return None
    name = match.group(1).strip().rstrip(".").strip()
    return name or None


def _is_antigravity_record(item: object) -> bool:
    return isinstance(item, dict) and "step_index" in item and isinstance(item.get("type"), str)


def _iter_transcript_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if _is_antigravity_record(item):
                    rows.append(item)
    except OSError:
        return []
    rows.sort(key=lambda r: r.get("step_index", 0))
    return rows


def _row_error_text(row: dict) -> str:
    err = row.get("error")
    if isinstance(err, str) and err.strip():
        return err.strip()
    if str(row.get("status") or "").upper() in _ABORT_STATUSES:
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def normalize_media(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        item: dict[str, str] = {}
        mime = entry.get("mime_type")
        if isinstance(mime, str) and mime.strip():
            item["mime_type"] = mime.strip()
        uri = entry.get("uri")
        if isinstance(uri, str) and uri.strip():
            item["uri"] = uri.strip()
        if item:
            items.append(item)
    return items


def format_media_text(media: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for entry in media:
        mime = entry.get("mime_type") or "media"
        uri = entry.get("uri") or ""
        parts.append(f"[{mime}] {uri}".strip() if uri else f"[{mime}]")
    return "\n".join(parts)


def coerce_tool_args(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"raw": raw}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    return {}


def is_tool_result_row(row: dict) -> bool:
    kind = str(row.get("type") or "")
    source = str(row.get("source") or "")
    if kind in _TOOL_RESULT_TYPES:
        return True
    return source == "MODEL" and kind not in _NON_TOOL_TYPES


def match_pending_call(
    pending_by_step: dict[int, str],
    pending_fifo: list[str],
    row_step: object,
) -> str:
    if (
        isinstance(row_step, int)
        and not isinstance(row_step, bool)
        and row_step in pending_by_step
    ):
        call_id = pending_by_step.pop(row_step)
        if call_id in pending_fifo:
            pending_fifo.remove(call_id)
        return call_id
    if pending_fifo:
        call_id = pending_fifo.pop(0)
        stale_keys = [k for k, v in pending_by_step.items() if v == call_id]
        for k in stale_keys:
            del pending_by_step[k]
        return call_id
    return ""


def _extract_json_id(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return ""
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    for key in ("task_id", "taskId", "conversation_id", "conversationId", "agent_id", "workflow_id", "id"):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _maybe_async_launch_token(
    tool_name: str,
    content: str,
    call_id: str,
    lineno: int,
    status_upper: str,
) -> str:
    if status_upper == "RUNNING":
        bg_match = _BG_TASK_ID_RE.search(content)
        if bg_match:
            return bg_match.group(1)
        json_id = _extract_json_id(content)
        return json_id or f"call:{call_id or lineno}"
    if tool_name in {"invoke_subagent", "run_workflow"} and status_upper == "DONE":
        lower = content.lower()
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                st = str(parsed.get("status") or "").strip().lower()
                if st in {"running", "queued", "started", "pending", "async"}:
                    return _extract_json_id(content) or f"call:{call_id or lineno}"
        if any(phrase in lower for phrase in _ASYNC_SUBAGENT_PHRASES):
            return _extract_json_id(content) or f"call:{call_id or lineno}"
    return ""


class AntigravityTurnTracker:
    """Ordered turn, tool-call, and async-task finality tracker."""

    def __init__(self) -> None:
        self.issued_calls: set[str] = set()
        self.answered_calls: set[str] = set()
        self.pending_by_step: dict[int, str] = {}
        self.pending_fifo: list[str] = []
        self.call_meta: dict[str, tuple[str, dict[str, Any], bool]] = {}
        self.pending_async: dict[str, str] = {}
        self.timer_watches: dict[str, str] = {}
        self.completed_async: set[str] = set()
        self.tail_type: str = ""
        self.tail_status: str = ""
        self.tail_is_tool: bool = False
        self.tail_has_tool_calls: bool = False
        self.tail_reply: str = ""
        self.tail_error: str = ""
        self.tail_has_user_text: bool = False
        self.tail_step_index: object = None
        self.tail_record: str = ""
        self.model: str | None = None
        self.row_user_text: str = ""
        self.row_reply_text: str = ""

    @property
    def open_tool_calls(self) -> int:
        return len(self.issued_calls - self.answered_calls)

    def _reset_turn(self) -> None:
        self.issued_calls.clear()
        self.answered_calls.clear()
        self.pending_by_step.clear()
        self.pending_fifo.clear()
        self.call_meta.clear()
        self.pending_async.clear()
        self.timer_watches.clear()
        self.completed_async.clear()

    def _resolve_async_token(self, token: str) -> None:
        tok = token.strip()
        if not tok:
            return
        self.pending_async.pop(tok, None)
        self.timer_watches.pop(tok, None)
        self.completed_async.add(tok)
        expired_timers = [
            timer_id
            for timer_id, watch in self.timer_watches.items()
            if watch == tok
        ]
        for timer_id in expired_timers:
            self.pending_async.pop(timer_id, None)
            self.timer_watches.pop(timer_id, None)
            self.completed_async.add(timer_id)

    def _clear_async_kind(self, kinds: frozenset[str]) -> None:
        victims = [tok for tok, kind in self.pending_async.items() if kind in kinds]
        for tok in victims:
            self._resolve_async_token(tok)

    def observe(self, row: dict, *, lineno: int = 0) -> None:
        self.row_user_text = ""
        self.row_reply_text = ""
        if not _is_antigravity_record(row):
            return
        kind = str(row.get("type") or "")
        source = str(row.get("source") or "").upper()
        status_upper = str(row.get("status") or "").upper()
        media = normalize_media(row.get("media"))
        row_error = _row_error_text(row)
        record = f"line:{lineno}" if lineno else ""

        if kind == "USER_INPUT":
            raw_content = row.get("content") or ""
            picked = model_name_from_user_input(raw_content)
            if picked:
                self.model = picked
            text = unwrap_user_request(raw_content)
            user_text = text if (text and text != "None") else ""
            has_user_payload = bool(user_text or media)
            is_human = source != "SYSTEM"
            if is_human and (has_user_payload or status_upper in _ABORT_STATUSES):
                self._reset_turn()
            if not has_user_payload and status_upper not in _ABORT_STATUSES:
                return
            self.row_user_text = user_text or (format_media_text(media) if media else "")
            self.tail_type = kind
            self.tail_status = status_upper
            self.tail_is_tool = False
            self.tail_has_tool_calls = False
            self.tail_reply = ""
            self.tail_error = row_error
            self.tail_has_user_text = has_user_payload
            self.tail_step_index = row.get("step_index")
            self.tail_record = record
            return

        if kind == "PLANNER_RESPONSE":
            content = row.get("content")
            content_text = ""
            if isinstance(content, str) and content.strip():
                candidate = content.strip()
                if candidate != "None":
                    content_text = candidate
            raw_calls = row.get("tool_calls")
            valid_calls = (
                [c for c in raw_calls if isinstance(c, dict)]
                if isinstance(raw_calls, list)
                else []
            )
            reply = content_text
            if not reply and media and not valid_calls:
                reply = format_media_text(media)
            step_val = row.get("step_index")
            for index, call in enumerate(valid_calls):
                step_label = step_val if step_val is not None else index
                call_id = f"{step_label}-{index}"
                if call_id in self.issued_calls:
                    call_id = f"{call_id}:{lineno or len(self.issued_calls)}"
                self.issued_calls.add(call_id)
                self.pending_fifo.append(call_id)
                if isinstance(step_val, int) and not isinstance(step_val, bool):
                    self.pending_by_step[step_val + 1 + index] = call_id
                tool_name = str(call.get("name") or "")
                tool_args = coerce_tool_args(call.get("args"))
                is_daemon = tool_args.get("IsDaemon") is True
                self.call_meta[call_id] = (tool_name, tool_args, is_daemon)
            self.row_reply_text = reply
            self.tail_type = kind
            self.tail_status = status_upper
            self.tail_is_tool = False
            self.tail_has_tool_calls = bool(valid_calls)
            self.tail_reply = reply
            self.tail_error = row_error
            self.tail_has_user_text = False
            self.tail_step_index = step_val
            self.tail_record = record
            return

        if is_tool_result_row(row):
            call_id = match_pending_call(
                self.pending_by_step, self.pending_fifo, row.get("step_index")
            )
            tool_name, tool_args, is_daemon = self.call_meta.get(call_id, ("", {}, False))
            content_str = str(row.get("content") or "")
            bg_match = _BG_TASK_ID_RE.search(content_str) if status_upper == "RUNNING" else None
            if call_id and (
                status_upper in _CLOSED_TOOL_STATUSES
                or (status_upper == "RUNNING" and bg_match is not None)
            ):
                self.answered_calls.add(call_id)
            async_token = ""
            if not is_daemon:
                async_token = _maybe_async_launch_token(
                    tool_name, content_str, call_id, lineno, status_upper
                )
            if async_token:
                watch = str(tool_args.get("TimerCondition") or "").strip()
                if watch and watch.lower() != "never" and watch in self.completed_async:
                    self.completed_async.add(async_token)
                else:
                    async_kind = (
                        "timer"
                        if tool_name == "schedule"
                        else (tool_name if tool_name in {"invoke_subagent", "run_workflow"} else "bg_task")
                    )
                    self.pending_async[async_token] = async_kind
                    if watch and watch.lower() != "never":
                        self.timer_watches[async_token] = watch
            self._apply_tool_result_cancellations(tool_name, tool_args, content_str, status_upper)
            self.tail_type = kind
            self.tail_status = status_upper
            self.tail_is_tool = True
            self.tail_has_tool_calls = False
            self.tail_reply = ""
            self.tail_error = row_error
            self.tail_has_user_text = False
            self.tail_step_index = row.get("step_index")
            self.tail_record = record
            return

        if kind == "SYSTEM_MESSAGE":
            content_str = str(row.get("content") or "")
            for tok in _SYS_TASK_DONE_RE.findall(content_str):
                self._resolve_async_token(tok)
            for sender in _SYS_SENDER_RE.findall(content_str):
                if sender in self.pending_async or any(
                    w == sender for w in self.timer_watches.values()
                ):
                    self._resolve_async_token(sender)
            if _SYS_MESSAGE_HEADER_RE.search(content_str):
                any_timers = [
                    tid for tid, w in self.timer_watches.items() if w.lower() == "any"
                ]
                for tid in any_timers:
                    self._resolve_async_token(tid)
            self.tail_type = kind
            self.tail_status = status_upper
            self.tail_is_tool = False
            self.tail_has_tool_calls = False
            self.tail_reply = ""
            self.tail_error = row_error
            self.tail_has_user_text = False
            self.tail_step_index = row.get("step_index")
            self.tail_record = record
            return

        self.tail_type = kind
        self.tail_status = status_upper
        self.tail_is_tool = False
        self.tail_has_tool_calls = False
        self.tail_reply = ""
        self.tail_error = row_error
        self.tail_has_user_text = False
        self.tail_step_index = row.get("step_index")
        self.tail_record = record

    def _apply_tool_result_cancellations(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        content_str: str,
        status_upper: str,
    ) -> None:
        if tool_name in {"manage_task", "manage_subagents", ""}:
            for match in _TASK_CANCELLED_RE.finditer(content_str):
                tok = match.group(1)
                if tok:
                    self._resolve_async_token(tok)
            for tok, st in _TASK_STATUS_RE.findall(content_str):
                if st.upper() in _TERMINAL_TASK_STATUSES:
                    self._resolve_async_token(tok)
        if tool_name in {"schedule", ""}:
            for tok in _WAIT_CANCELLED_RE.findall(content_str):
                self._resolve_async_token(tok)
        if (
            tool_name in {"manage_task", ""}
            and "No background tasks are currently running." in content_str
        ):
            self._clear_async_kind(frozenset({"bg_task", "timer"}))
        action = str(tool_args.get("Action") or tool_args.get("action") or "").strip()
        if tool_name == "manage_task" and status_upper == "DONE":
            if action == "kill_all":
                self._clear_async_kind(frozenset({"bg_task", "timer"}))
            elif action == "kill":
                tid = tool_args.get("TaskId")
                if isinstance(tid, str) and tid.strip():
                    self._resolve_async_token(tid)
        elif tool_name == "manage_subagents" and status_upper == "DONE":
            if action == "kill_all":
                self._clear_async_kind(frozenset({"invoke_subagent"}))
            elif action in {"kill", "cancel"}:
                for key in ("id", "agent_id", "conversation_id", "TaskId"):
                    val = tool_args.get(key)
                    if isinstance(val, str) and val.strip():
                        self._resolve_async_token(val)


def _transcript_conversation(rows: list[dict]) -> tuple[list[ConversationMessage], dict]:
    """Project transcript rows to plain user/assistant turns plus list hints."""
    messages: list[ConversationMessage] = []
    tracker = AntigravityTurnTracker()
    first_user = ""
    last_user = ""
    last_agent = ""
    first_ts: float | None = None
    last_ts: float | None = None
    for idx, row in enumerate(rows, 1):
        created = row.get("created_at")
        ts = parse_timestamp(created) if isinstance(created, str) else None
        tracker.observe(row, lineno=idx)
        kind = str(row.get("type") or "")
        row_status = str(row.get("status") or "").upper()
        if kind == "USER_INPUT":
            if not tracker.row_user_text and row_status not in _ABORT_STATUSES:
                continue
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            if tracker.row_user_text:
                if not first_user:
                    first_user = tracker.row_user_text
                last_user = tracker.row_user_text
                messages.append(
                    ConversationMessage(role="user", text=tracker.row_user_text, timestamp=ts)
                )
        elif kind == "PLANNER_RESPONSE":
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            if tracker.row_reply_text:
                last_agent = tracker.row_reply_text
                messages.append(
                    ConversationMessage(
                        role="assistant", text=tracker.row_reply_text, timestamp=ts
                    )
                )
        else:
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
    meta = {
        "first_user": first_user,
        "last_user": last_user,
        "last_agent": last_agent,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "model": tracker.model,
        "tail_type": tracker.tail_type or None,
        "tail_status": tracker.tail_status or None,
        "tail_is_tool": tracker.tail_is_tool,
        "tail_has_tool_calls": tracker.tail_has_tool_calls,
        "tail_reply": tracker.tail_reply,
        "open_tool_calls": tracker.open_tool_calls,
        "pending_async": tuple(sorted(tracker.pending_async)),
        "tail_step_index": tracker.tail_step_index,
        "tail_error": tracker.tail_error,
    }
    return messages, meta


def _summary_is_active(summary: dict | None) -> bool:
    if not summary:
        return False
    status = str(summary.get("status") or "").strip().upper()
    return status == "CASCADE_RUN_STATUS_RUNNING" or bool(summary.get("not_fully_idle"))


def _status_for_transcript(meta: dict, summary: dict | None = None) -> str:
    summary_info = summary or {}
    if summary_info.get("killed"):
        return titles.STATUS_ABORTED
    if _summary_is_active(summary_info):
        return titles.STATUS_PENDING
    tail_status = str(meta.get("tail_status") or "").upper()
    tail_type = str(meta.get("tail_type") or "")
    tail_is_tool = bool(meta.get("tail_is_tool"))
    if tail_type == "ERROR_MESSAGE" or (not tail_is_tool and tail_status in _ABORT_STATUSES):
        return titles.STATUS_ABORTED
    if (
        tail_status == "RUNNING"
        or tail_is_tool
        or tail_type == "SYSTEM_MESSAGE"
        or meta.get("tail_has_tool_calls")
        or int(meta.get("open_tool_calls") or 0) > 0
        or bool(meta.get("pending_async"))
    ):
        return titles.STATUS_PENDING
    if (
        tail_type == "PLANNER_RESPONSE"
        and tail_status == "DONE"
        and not meta.get("tail_has_tool_calls")
        and int(meta.get("open_tool_calls") or 0) == 0
        and not meta.get("pending_async")
        and bool(str(meta.get("tail_reply") or "").strip())
    ):
        return titles.STATUS_DONE
    if tail_type == "USER_INPUT" and str(meta.get("last_user") or "").strip():
        return titles.STATUS_PENDING
    return titles.STATUS_NONE


def _connect_ro(path: str) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        return None
    return conn


def _read_summaries(root: str) -> dict[str, dict]:
    """Map conversation id -> list metadata from conversation_summaries.db."""
    db_path = os.path.join(root, "conversation_summaries.db")
    if not os.path.isfile(db_path):
        return {}
    conn = _connect_ro(db_path)
    if conn is None:
        return {}
    try:
        parent_map: dict[str, str] = {}
        try:
            parent_rows = conn.execute(
                "SELECT conversation_id, parent_id FROM conversation_parents"
            ).fetchall()
        except sqlite3.Error:
            parent_rows = []
        for prow in parent_rows:
            child_id = str(prow[0] or "").strip()
            parent_id = str(prow[1] or "").strip()
            if child_id and parent_id:
                parent_map[child_id] = parent_id

        has_parent_cols = True
        has_idle_col = True
        try:
            rows = conn.execute(
                "SELECT conversation_id, title, preview, step_count,"
                " last_modified_time, workspace_uris, status, killed,"
                " last_user_input_time, app_data_dir,"
                " parent_conversation_id, nesting_depth, not_fully_idle"
                " FROM conversation_summaries"
            ).fetchall()
        except sqlite3.Error:
            has_idle_col = False
            try:
                rows = conn.execute(
                    "SELECT conversation_id, title, preview, step_count,"
                    " last_modified_time, workspace_uris, status, killed,"
                    " last_user_input_time, app_data_dir,"
                    " parent_conversation_id, nesting_depth FROM conversation_summaries"
                ).fetchall()
            except sqlite3.Error:
                has_parent_cols = False
                try:
                    rows = conn.execute(
                        "SELECT conversation_id, title, preview, step_count,"
                        " last_modified_time, workspace_uris, status, killed,"
                        " last_user_input_time, app_data_dir FROM conversation_summaries"
                    ).fetchall()
                except sqlite3.Error:
                    rows = []

        out: dict[str, dict] = {}
        for row in rows:
            cid = str(row[0] or "").strip()
            if not cid:
                continue
            parent_cid = str(row[10] or "").strip() if has_parent_cols else ""
            try:
                depth = int(row[11] or 0) if has_parent_cols else 0
            except (TypeError, ValueError):
                depth = 0
            not_fully_idle = bool(row[12]) if has_idle_col else False
            linked_parent = parent_map.get(cid, "")
            out[cid] = {
                "title": str(row[1] or "").strip(),
                "preview": str(row[2] or "").strip(),
                "step_count": row[3] or 0,
                "last_modified_time": row[4],
                "workspace_uris": str(row[5] or ""),
                "status": str(row[6] or ""),
                "killed": bool(row[7]),
                "not_fully_idle": not_fully_idle,
                "last_user_input_time": row[8],
                "app_data_dir": str(row[9] or ""),
                "parent_conversation_id": parent_cid or linked_parent,
                "nesting_depth": depth,
                "is_subagent": bool(parent_cid) or depth > 0 or bool(linked_parent),
                "root": root,
            }
        for child_cid, parent_id in parent_map.items():
            if child_cid not in out:
                out[child_cid] = {
                    "title": "",
                    "preview": "",
                    "step_count": 0,
                    "last_modified_time": None,
                    "workspace_uris": "",
                    "status": "",
                    "killed": False,
                    "not_fully_idle": False,
                    "last_user_input_time": None,
                    "app_data_dir": "",
                    "parent_conversation_id": parent_id,
                    "nesting_depth": 1,
                    "is_subagent": True,
                    "root": root,
                }
        return out
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _read_all_summaries() -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for root in _roots():
        for cid, entry in _read_summaries(root).items():
            prev = merged.get(cid)
            if prev is None:
                merged[cid] = entry
            else:
                prev_sub = bool(prev.get("is_subagent"))
                merged[cid] = {
                    **prev,
                    **{k: v for k, v in entry.items() if v not in ("", None, False)},
                    "is_subagent": prev_sub or bool(entry.get("is_subagent")),
                    "killed": bool(prev.get("killed")) or bool(entry.get("killed")),
                    "not_fully_idle": (
                        bool(prev.get("not_fully_idle")) or bool(entry.get("not_fully_idle"))
                    ),
                }
    return merged


def _unescape_pbtxt_string(raw: str) -> str:
    return (
        raw.replace(r'\"', '"')
        .replace(r"\'", "'")
        .replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace(r"\r", "\r")
        .replace(r"\\", "\\")
    )


def _annotation_title(cid: str, candidate_roots: list[str] | tuple[str, ...] = ()) -> str | None:
    if not cid or "/" in cid or "\\" in cid or ".." in cid:
        return None
    seen: set[str] = set()
    roots: list[str] = []
    for root in (*candidate_roots, *_roots()):
        if root and root not in seen:
            seen.add(root)
            roots.append(root)
    for root in roots:
        path = os.path.join(root, "annotations", f"{cid}.pbtxt")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read(8192)
        except OSError:
            continue
        match = _PBTXT_TITLE_RE.search(text)
        if match:
            raw = match.group(1) if match.group(1) is not None else (match.group(2) or "")
            cleaned = _unescape_pbtxt_string(raw).strip()
            if cleaned:
                return cleaned
    return None


def _root_from_history_path(transcript_path: str | None, db_path: str | None) -> list[str]:
    roots: list[str] = []
    if transcript_path:
        # <root>/brain/<cid>/.system_generated/logs/transcript*.jsonl
        logs_dir = os.path.dirname(os.path.abspath(transcript_path))
        if os.path.basename(logs_dir) == "logs":
            sys_dir = os.path.dirname(logs_dir)
            if os.path.basename(sys_dir) == ".system_generated":
                conv_dir = os.path.dirname(sys_dir)
                brain_dir = os.path.dirname(conv_dir)
                if os.path.basename(brain_dir) == "brain":
                    roots.append(os.path.dirname(brain_dir))
    if db_path:
        # <root>/conversations/<cid>.db
        conv_dir = os.path.dirname(os.path.abspath(db_path))
        if os.path.basename(conv_dir) == "conversations":
            roots.append(os.path.dirname(conv_dir))
    return roots


def _workspace_to_cwd(blob: bytes, workspace_uris: str) -> str:
    # Prefer the clean JSON workspace list from conversation_summaries.db
    # (e.g. ["file:///Users/geraltgraham"]) over the protobuf blob, whose
    # file:// prefix is followed by binary framing bytes.
    try:
        parsed = json.loads(workspace_uris) if workspace_uris else None
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item.startswith("file://"):
                candidate = item[len("file://") :].strip()
                if candidate.startswith("/") and _looks_like_cwd(candidate):
                    return candidate
    candidates: list[str] = []
    try:
        text = blob.decode("utf-8", errors="replace")
    except (AttributeError, UnicodeDecodeError):
        text = ""
    for match in _FILE_URI_RE.finditer(text or ""):
        candidates.append(match.group(1))
    for match in _FILE_URI_RE.finditer(workspace_uris or ""):
        candidates.append(match.group(1))
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate.startswith("/") and _looks_like_cwd(candidate):
            return candidate
    return ""


def _looks_like_cwd(candidate: str) -> bool:
    if not candidate.startswith("/"):
        return False
    # Reject protobuf framing leakage: real cwds are printable paths.
    if not all(32 <= ord(ch) < 127 for ch in candidate):
        return False
    return len(candidate) <= 512


def _db_cwd_and_times(db_path: str, summary: dict | None) -> tuple[str, float | None]:
    cwd = ""
    event_time: float | None = None
    conn = _connect_ro(db_path)
    if conn is None:
        return cwd, event_time
    try:
        try:
            row = conn.execute(
                "SELECT data FROM trajectory_metadata_blob WHERE id='main' LIMIT 1"
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT data FROM trajectory_metadata_blob LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            row = None
        blob = b""
        if row is not None and row[0] is not None:
            raw = row[0]
            if isinstance(raw, str):
                raw = raw.encode("utf-8", errors="surrogateescape")
            blob = bytes(raw)
        workspace_uris = str((summary or {}).get("workspace_uris") or "")
        cwd = _workspace_to_cwd(blob, workspace_uris)
        if not cwd and workspace_uris:
            # Fall back to the raw JSON array (["file:///..."]) only when the
            # regex above found nothing but the field itself names a workspace.
            first = workspace_uris.strip().strip("[]")
            first = first.strip().strip('"').strip("'")
            if first.startswith("file://"):
                cwd = first[len("file://") :]
            elif first.startswith("/"):
                cwd = first
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    # Timestamps: summaries carry ISO datetimes; parse when usable.
    for key in ("last_modified_time", "last_user_input_time"):
        raw = (summary or {}).get(key)
        if isinstance(raw, str) and raw and not raw.startswith("0001-01-01"):
            parsed = parse_timestamp(raw)
            if parsed is not None and event_time is None:
                event_time = parsed
    return cwd, event_time


def _history_map() -> dict[str, dict]:
    """Map conversation id -> latest CLI history row (display text, time, cwd).

    ``<root>/history.jsonl`` records every CLI prompt (``display``) with an
    optional ``conversationId``. For sqlite-only sessions (no transcript) this
    is the only surviving user-text evidence; it fills list excerpts so the
    sessions stay searchable. Missing or unreadable files yield no rows.
    """
    mapped: dict[str, dict] = {}
    for root in _roots():
        path = os.path.join(root, "history.jsonl")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    cid = str(item.get("conversationId") or "").strip()
                    if not cid:
                        continue
                    text = str(item.get("display") or "").strip()
                    if not text:
                        continue
                    ts_raw = item.get("timestamp")
                    ts: float | None = None
                    if isinstance(ts_raw, (int, float)) and ts_raw > 0:
                        # Antigravity CLI stamps milliseconds.
                        ts = float(ts_raw) / 1000.0 if ts_raw > 1e12 else float(ts_raw)
                    prev = mapped.get(cid)
                    if prev is None or (ts is not None and ts >= float(prev.get("ts") or 0)):
                        mapped[cid] = {
                            "text": text,
                            "ts": ts,
                            "cwd": str(item.get("workspace") or "").strip(),
                        }
        except OSError:
            continue
    return mapped


def _transcript_candidates() -> dict[str, str]:
    """Map conversation id -> preferred transcript path (full wins)."""
    found: dict[str, tuple[str, bool]] = {}
    for root in _roots():
        brain = os.path.join(root, "brain")
        if not os.path.isdir(brain):
            continue
        try:
            names = sorted(os.listdir(brain))
        except OSError:
            continue
        for name in names:
            conv_dir = os.path.join(brain, name)
            if not os.path.isdir(conv_dir):
                continue
            logs = os.path.join(conv_dir, ".system_generated", "logs")
            full = os.path.join(logs, "transcript_full.jsonl")
            slim = os.path.join(logs, "transcript.jsonl")
            chosen = ""
            if os.path.isfile(full):
                chosen = full
            elif os.path.isfile(slim):
                chosen = slim
            if not chosen:
                continue
            prev = found.get(name)
            # Prefer the untruncated sibling; otherwise keep the newest file.
            if prev is None:
                found[name] = (chosen, chosen.endswith("transcript_full.jsonl"))
            else:
                prev_is_full = prev[1]
                cur_is_full = chosen.endswith("transcript_full.jsonl")
                if cur_is_full and not prev_is_full:
                    found[name] = (chosen, True)
                elif cur_is_full == prev_is_full:
                    try:
                        if os.path.getmtime(chosen) > os.path.getmtime(prev[0]):
                            found[name] = (chosen, cur_is_full)
                    except OSError:
                        pass
    return {cid: path for cid, (path, _full) in found.items()}


def _db_candidates() -> dict[str, str]:
    found: dict[str, str] = {}
    for root in _roots():
        conv_dir = os.path.join(root, "conversations")
        if not os.path.isdir(conv_dir):
            continue
        try:
            names = sorted(os.listdir(conv_dir))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".db"):
                continue
            if name.endswith(("-shm", "-wal")):
                continue
            cid = name[: -len(".db")]
            path = os.path.join(conv_dir, name)
            if cid not in found:
                found[cid] = path
            else:
                try:
                    if os.path.getmtime(path) > os.path.getmtime(found[cid]):
                        found[cid] = path
                except OSError:
                    pass
    return found


def _build_session_info(
    cid: str,
    transcript_path: str | None,
    db_path: str | None,
    summaries: dict[str, dict],
    history: dict[str, dict] | None = None,
    host=None,
) -> SessionInfo | None:
    summary = summaries.get(cid)
    if summary is None:
        summary = _read_all_summaries().get(cid)
    candidate_roots = [
        r for r in ((summary or {}).get("root"), *_root_from_history_path(transcript_path, db_path)) if r
    ]
    if transcript_path:
        rows = _iter_transcript_rows(transcript_path)
        if not rows:
            return None
        _messages, meta = _transcript_conversation(rows)
        # Transcript-only sessions still honor the summaries cwd when the
        # transcript itself carries no working directory (expected: the
        # transcript format has no cwd field).
        cwd = ""
        event_time = meta.get("last_ts") or meta.get("first_ts")
        if db_path and os.path.isfile(db_path):
            db_cwd, db_time = _db_cwd_and_times(db_path, summary)
            cwd = db_cwd
            if event_time is None:
                event_time = db_time
        elif summary is not None:
            db_cwd, db_time = _db_cwd_and_times(db_path or "", summary)
            cwd = db_cwd
            if event_time is None:
                event_time = db_time
        try:
            file_mtime = os.path.getmtime(transcript_path)
        except OSError:
            return None
        try:
            size_bytes = os.path.getsize(transcript_path)
        except OSError:
            size_bytes = 0
        native_title = ((summary or {}).get("title") or "").strip() or _annotation_title(cid, candidate_roots) or None
        first_user = preprocess_excerpt(str(meta.get("first_user") or ""), host) or ""
        last_user = preprocess_excerpt(str(meta.get("last_user") or ""), host) or ""
        last_agent = preprocess_excerpt(str(meta.get("last_agent") or ""), host) or ""
        fallback_title = first_user.splitlines()[0].strip() if first_user.strip() else cid
        if len(fallback_title) > 60:
            fallback_title = fallback_title[:60]
        if not fallback_title.strip():
            fallback_title = cid
        session_time, time_source = effective_session_time(file_mtime, event_time)
        tail = rows[-1] if rows else {}
        step = meta.get("tail_step_index")
        if step is None:
            step = tail.get("step_index")
        is_killed = bool((summary or {}).get("killed"))
        tail_status = str(meta.get("tail_status") or "").upper()
        tail_error = str(meta.get("tail_error") or "").strip()
        status_tag = _status_for_transcript(meta, summary)
        terminal_error: dict[str, object] = {}
        if status_tag == titles.STATUS_ABORTED:
            if tail_error:
                last_agent = preprocess_excerpt(tail_error, host) or tail_error
            anchor = str(step) if step is not None else f"row:{len(rows)}"
            id_tail = tail_error or last_agent or last_user
            fallback_msg = (
                "interrupted by user"
                if is_killed
                else ("canceled" if "CANCEL" in tail_status else "error")
            )
            fallback_kind = "user_interrupt" if is_killed else "error"
            terminal_error = terminal_error_evidence(
                kind="" if tail_error else fallback_kind,
                message=tail_error or fallback_msg,
            )
        elif status_tag == titles.STATUS_DONE:
            anchor = str(step) if step is not None else ""
            tail_reply_raw = str(meta.get("tail_reply") or "")
            id_tail = preprocess_excerpt(tail_reply_raw, host) or tail_reply_raw
        else:
            anchor = ""
            id_tail = ""
        from sesskit.models import completion_id_for

        completion_id = completion_id_for(
            status_tag=status_tag, anchor=anchor, tail_text=id_tail
        )
        return make_session_info(
            source="antigravity",
            id=cid,
            short_id=cid[:8],
            cwd=cwd,
            mtime=session_time,
            time_source=time_source,
            event_time=event_time,
            file_mtime=file_mtime,
            size_bytes=size_bytes,
            native_title=native_title,
            fallback_title=fallback_title,
            status_tag=status_tag,
            path=transcript_path,
            first_user_msg=first_user,
            last_user_msg=last_user,
            last_agent_msg=last_agent,
            completion_id=completion_id,
            **terminal_error,
        )
    # SQLite-only session: no conversation text, but the database itself is
    # the history. List with unknown status; never claim done.
    if db_path and os.path.isfile(db_path):
        cwd, event_time = _db_cwd_and_times(db_path, summary)
        try:
            file_mtime = os.path.getmtime(db_path)
        except OSError:
            return None
        try:
            size_bytes = os.path.getsize(db_path)
        except OSError:
            size_bytes = 0
        native_title = ((summary or {}).get("title") or "").strip() or _annotation_title(cid, candidate_roots) or None
        preview = str((summary or {}).get("preview") or "")
        hist = (history or {}).get(cid, {})
        hist_text = str(hist.get("text") or "").strip()
        # history.jsonl is the only surviving user-text for sqlite-only rows.
        first_user = hist_text
        last_user = hist_text
        if hist.get("cwd") and not cwd:
            cwd = str(hist["cwd"])
        if hist.get("ts") is not None and event_time is None:
            event_time = float(hist["ts"])
        # A bare slash-command probe (e.g. "/model") is not a real prompt;
        # keep the session listable but leave excerpts empty.
        if hist_text.startswith("/"):
            first_user = ""
            last_user = ""
        first_user = preprocess_excerpt(first_user, host) or ""
        last_user = preprocess_excerpt(last_user, host) or ""
        last_agent_disp = preprocess_excerpt(preview, host) or ""
        from sesskit.models import completion_id_for

        terminal_error = {}
        if (summary or {}).get("killed"):
            status_tag = titles.STATUS_ABORTED
            completion_id = completion_id_for(
                status_tag=status_tag,
                anchor=f"killed:{cid}:{event_time or ''}",
                tail_text=preview or hist_text,
            )
            terminal_error = terminal_error_evidence(
                kind="user_interrupt", message="interrupted by user"
            )
        else:
            status_tag = titles.STATUS_NONE
            completion_id = ""
        session_time, time_source = effective_session_time(file_mtime, event_time)
        fb = preview[:60] if preview.strip() else (hist_text[:60] if hist_text and not hist_text.startswith("/") else cid)
        return make_session_info(
            source="antigravity",
            id=cid,
            short_id=cid[:8],
            cwd=cwd,
            mtime=session_time,
            time_source=time_source,
            event_time=event_time,
            file_mtime=file_mtime,
            size_bytes=size_bytes,
            native_title=native_title,
            fallback_title=fb,
            status_tag=status_tag,
            path=db_path,
            first_user_msg=first_user,
            last_user_msg=last_user,
            last_agent_msg=last_agent_disp,
            completion_id=completion_id,
            **terminal_error,
        )
    return None


def scan_sessions(
    limit: int = 50,
    keep_ids: set[str] | None = None,
    *,
    include_missing_cwd: bool = False,
    host=None,
) -> list[SessionInfo]:
    """Scan Antigravity transcripts plus conversation databases."""
    transcripts = _transcript_candidates()
    dbs = _db_candidates()
    summaries = _read_all_summaries()
    history = _history_map()
    ids = sorted(
        set(transcripts) | set(dbs),
        key=lambda cid: _sort_key(cid, transcripts, dbs),
        reverse=True,
    )
    host_marker = host.title_prompt_marker if host is not None else None
    host_prefixes = ephemeral_prefixes_for(host)
    sessions: list[SessionInfo] = []
    overfetch = max(int(limit) * 3, int(limit) + 5) if int(limit) > 0 else 0
    for cid in ids:
        if keep_ids is not None and cid not in keep_ids:
            continue
        summary = summaries.get(cid)
        if summary and summary.get("is_subagent"):
            continue
        info = _build_session_info(
            cid, transcripts.get(cid), dbs.get(cid), summaries, history, host=host
        )
        if info is None:
            continue
        first_user = str(info.get("first_user_msg") or "")
        fallback = str(info.get("fallback_title") or "")
        native = str(info.get("native_title") or "")
        if (
            titles.is_title_generation_prompt(first_user, marker=host_marker)
            or titles.is_title_generation_prompt(fallback, marker=host_marker)
            or titles.is_title_generation_prompt(native, marker=host_marker)
        ):
            continue
        cwd = str(info.get("cwd") or "")
        if not cwd and not include_missing_cwd:
            continue
        if cwd and is_ephemeral_agent_cwd(cwd, extra_prefixes=host_prefixes):
            continue
        sessions.append(info)
        if overfetch and len(sessions) >= overfetch:
            break
    sessions.sort(key=lambda s: float(s.get("mtime") or 0), reverse=True)
    if int(limit) >= 0:
        sessions = sessions[: int(limit)]
    return sessions


def _sort_key(cid: str, transcripts: dict[str, str], dbs: dict[str, str]) -> float:
    for path in (transcripts.get(cid), dbs.get(cid)):
        if path:
            try:
                return os.path.getmtime(path)
            except OSError:
                continue
    return 0.0


def load_conversation(path: str, *, include_errors: bool = False) -> list[ConversationMessage]:
    """Load plain user/assistant turns from a transcript path or sqlite db path.

    SQLite-only histories carry usage metadata but no plain transcript; they
    load as an empty (readable, zero-turn) conversation rather than an error.
    Missing paths raise ``FileNotFoundError``; the adapter translates the
    SessKit-path case to ``ConversationLoadError``.
    """
    if not path:
        raise FileNotFoundError("session has no history path (runtime=antigravity)")
    if not os.path.exists(path):
        raise FileNotFoundError(f"history path not found: {path}")
    if path.endswith(".db"):
        return []
    from sesskit.activity_antigravity import load_antigravity_activity
    from sesskit.conversation import project_session_conversation

    cid = _conversation_id_from_transcript(path) or path
    snapshot = load_antigravity_activity({"source": "antigravity", "path": path, "id": cid})
    if snapshot.state == "unavailable":
        raise FileNotFoundError(f"history path not readable: {path}")
    return project_session_conversation(
        snapshot,
        include_errors=include_errors,
        history_ref=path,
    )


def _safe_cid(cid: object) -> str | None:
    text = str(cid or "").strip()
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or "\x00" in text
    ):
        return None
    return text


def _resolve_delete_target(path_or_id: str, root: str | None) -> tuple[str | None, list[str]]:
    raw = str(path_or_id or "").strip()
    if not raw:
        return None, []
    cid: str | None = None
    derived_root: str | None = None
    if os.sep in raw or "/" in raw:
        abs_path = os.path.abspath(raw)
        norm = abs_path.replace(os.sep, "/")
        parts = norm.split("/")
        if "brain" in parts:
            idx = len(parts) - 1 - parts[::-1].index("brain")
            if idx + 1 < len(parts):
                cid = _safe_cid(parts[idx + 1])
                derived_root = "/".join(parts[:idx]) or "/"
        elif norm.endswith(".db") and os.path.basename(os.path.dirname(abs_path)) == "conversations":
            cid = _safe_cid(os.path.basename(abs_path)[: -len(".db")])
            derived_root = os.path.dirname(os.path.dirname(abs_path))
        elif norm.endswith(".pbtxt") and os.path.basename(os.path.dirname(abs_path)) == "annotations":
            cid = _safe_cid(os.path.basename(abs_path)[: -len(".pbtxt")])
            derived_root = os.path.dirname(os.path.dirname(abs_path))
        elif norm.endswith(".lock") and os.path.basename(os.path.dirname(abs_path)) == "presence":
            cid = _safe_cid(os.path.basename(abs_path)[: -len(".lock")])
            derived_root = os.path.dirname(os.path.dirname(abs_path))
    else:
        cid = _safe_cid(raw)
    if cid is None:
        return None, []
    if root is not None:
        abs_root = os.path.abspath(root)
        return cid, ([abs_root] if os.path.isdir(abs_root) else [])
    if derived_root:
        return cid, ([derived_root] if os.path.isdir(derived_root) else [])
    return cid, _roots()


def _unlink_if_unlocked(path: str) -> None:
    if not os.path.lexists(path):
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                return
            raise
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def _delete_db_hierarchy(db_path: str, root_cid: str) -> list[str]:
    targets = {root_cid}
    if not os.path.isfile(db_path):
        return [root_cid]
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        has_summaries = "conversation_summaries" in tables
        has_parent_col = False
        if has_summaries:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
            }
            has_parent_col = "parent_conversation_id" in cols
        has_parents_table = "conversation_parents" in tables
        has_fts = "conversations_fts" in tables

        queue = [root_cid]
        while queue:
            cur = queue.pop(0)
            children: list[object] = []
            if has_summaries and has_parent_col:
                children.extend(
                    row[0]
                    for row in conn.execute(
                        "SELECT conversation_id FROM conversation_summaries "
                        "WHERE parent_conversation_id = ?",
                        (cur,),
                    ).fetchall()
                )
            if has_parents_table:
                children.extend(
                    row[0]
                    for row in conn.execute(
                        "SELECT conversation_id FROM conversation_parents WHERE parent_id = ?",
                        (cur,),
                    ).fetchall()
                )
            for child in children:
                safe_child = _safe_cid(child)
                if safe_child and safe_child not in targets:
                    targets.add(safe_child)
                    queue.append(safe_child)

        ordered = sorted(targets)
        placeholders = ",".join("?" for _ in ordered)
        if has_summaries:
            conn.execute(
                f"DELETE FROM conversation_summaries WHERE conversation_id IN ({placeholders})",
                ordered,
            )
        if has_parents_table:
            conn.execute(
                f"DELETE FROM conversation_parents "
                f"WHERE conversation_id IN ({placeholders}) OR parent_id IN ({placeholders})",
                ordered + ordered,
            )
        if has_fts:
            conn.execute(
                f"DELETE FROM conversations_fts WHERE conversation_id IN ({placeholders})",
                ordered,
            )
        conn.commit()
        return ordered
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_session(path_or_id: str, *, root: str | None = None) -> None:
    """Delete one Antigravity session and its descendant subagents.

    Removes ``brain/<cid>/``, ``annotations/<cid>.pbtxt``,
    ``conversations/<cid>.db`` (plus ``-wal``/``-shm``), unlocked
    ``presence/<cid>.lock`` files (preserving any ``flock``-held lock), and
    matching target + descendant rows in ``conversation_summaries.db``.
    Missing paths/databases are no-ops.
    """
    cid, roots = _resolve_delete_target(path_or_id, root)
    if not cid or not roots:
        return
    for r in roots:
        targets = _delete_db_hierarchy(os.path.join(r, "conversation_summaries.db"), cid)
        for target_id in targets:
            _unlink_if_unlocked(os.path.join(r, "presence", f"{target_id}.lock"))
            brain_dir = os.path.join(r, "brain", target_id)
            if os.path.isdir(brain_dir):
                shutil.rmtree(brain_dir, ignore_errors=True)
            for rel in (
                os.path.join("annotations", f"{target_id}.pbtxt"),
                os.path.join("conversations", f"{target_id}.db"),
                os.path.join("conversations", f"{target_id}.db-wal"),
                os.path.join("conversations", f"{target_id}.db-shm"),
            ):
                try:
                    os.unlink(os.path.join(r, rel))
                except FileNotFoundError:
                    pass


def refresh_session(session: dict, *, host=None) -> SessionInfo | None:
    """Re-derive one listed session from its native history."""
    cid = str(session.get("id") or "")
    path = str(session.get("path") or "")
    if not cid or not path:
        return None
    transcripts = _transcript_candidates()
    dbs = _db_candidates()
    summaries = _read_all_summaries()
    history = _history_map()
    tpath = transcripts.get(cid, path if path.endswith(".jsonl") else None)
    dpath = dbs.get(cid, path if path.endswith(".db") else None)
    # Prefer the exact stored path when it still exists.
    if path.endswith(".jsonl") and os.path.isfile(path):
        tpath = path
    if path.endswith(".db") and os.path.isfile(path):
        dpath = path
    return _build_session_info(cid, tpath, dpath, summaries, history, host=host)


def scan_signature() -> object | None:
    """Cheap list-level version: file stamps of transcripts, dbs, summaries."""
    parts: list[object] = []
    for root in _roots():
        for name in (
            "conversation_summaries.db",
            "conversation_summaries.db-wal",
            "history.jsonl",
        ):
            full = os.path.join(root, name)
            if os.path.isfile(full):
                parts.append((full, stat_signature(full)))
    for path in list(_transcript_candidates().values()) + list(_db_candidates().values()):
        parts.append((path, stat_signature(path)))
    # Include SQLite WAL alongside conversation_summaries.db so status flips
    # invalidate scan caches before checkpointing.
    parts.sort(key=lambda item: str(item[0]))
    return tuple(parts) if parts else None
