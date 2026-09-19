"""Cross-runtime session data models for SessKit."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict


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
    # 终端态（已完成/已中断）才有值；进行中/未知为空串。供 Corral 完成通知去重。
    completion_id: str
    thread_source: str | None
    keepalive_name: str
    provisional: bool
    attention_kind: str
    attention_token: str | None
    attention_updated_at: float


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
    return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


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
