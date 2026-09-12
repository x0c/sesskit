#!/usr/bin/env python3
"""扫描 Cursor Agent CLI 会话历史（~/.cursor/chats/），输出统一会话结构。

Cursor CLI 的会话按「工作区哈希 / 会话 UUID」两级目录存放：

    ~/.cursor/chats/<workspace_hash>/<chat_id>/
        meta.json              标题、cwd、createdAtMs/updatedAtMs、hasConversation
        prompt_history.json    用户输入列表（可选；**最新在前**）
        store.db               完整对话（blobs 内容寻址；列表扫描不读，预览按需读）

列表扫描只碰 meta.json / prompt_history.json，保证首屏轻量。完整对话由
load_conversation() 只读打开 store.db，解析 JSON blob 里 role=user/assistant
的正文；二进制 DAG blob 跳过。用户正文优先取 <user_query>…</user_query>。

``isSubagent`` 的 Task 派生 chat 不得进入列表。判活时若 ``agent`` 进程实际
绑在这类子会话上（``--resume`` 或打开了它的 store.db），必须把 live 记到
``subagentInfo.rootParentAgentId`` / ``parentAgentId`` 指向的父会话——父进程
已回到空闲或不在跑时，子代理仍在干活，侧栏也必须是进行中。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

from sesskit import titles
from sesskit.cache import file_signature, get_cache
from sesskit.hosted import hosted_session_id
from sesskit.models import ConversationMessage, SessionInfo, effective_session_time, make_session_info
from sesskit.parsers.common import (
    is_ephemeral_agent_cwd,
    live_pid_snapshot,
    live_processes,
    process_command_line,
    process_environ,
    stat_signature,
)

CHATS_DIR = os.path.expanduser("~/.cursor/chats")

_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
# Cursor CLI：`agent --resume <uuid>` / `--resume=<uuid>`；`-1` 表示续最近一条，无法精确绑定。
_RESUME_ID_RE = re.compile(
    r"--resume(?:=|\s+)(?P<id>-1|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
# agent 打开的 ~/.cursor/chats/<workspace>/<chatId>/store.db（含 -wal/-shm）。
_OPEN_CHAT_STORE_RE = re.compile(
    r"/[.]cursor/chats/[^/]+/"
    r"(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/store[.]db"
)
# 判活时只收集 Cursor chat 相关 fd，避免读取 agent 打开的全部文件。
_CURSOR_FD_HINT = ".cursor/chats/"
# Per-session conversation cache must include the WAL: uncheckpointed writes do
# not bump store.db mtime. The *list* scan_signature must not — a live agent
# keeps rewriting store.db-wal, and folding that into the global signature forces
# a full rescan of every chat every few seconds while the UI is open.
_CHAT_LIST_SIG_FILES = ("meta.json", "prompt_history.json", "store.db")
_STORE_PATH_CACHE: tuple[tuple[int, ...], dict[int, list[str]]] | None = None


def _cursor_store_paths_for_pids(pids: list[int]) -> dict[int, list[str]]:
    """只读 agent 进程中与 Cursor chat store 相关的打开路径。"""
    if not pids:
        return {}
    key = tuple(sorted(pids))
    global _STORE_PATH_CACHE
    if _STORE_PATH_CACHE is not None and _STORE_PATH_CACHE[0] == key:
        return {pid: list(paths) for pid, paths in _STORE_PATH_CACHE[1].items()}
    result: dict[int, list[str]] = {}
    if sys.platform.startswith("linux"):
        for pid in pids:
            fd_dir = f"/proc/{pid}/fd"
            try:
                names = os.listdir(fd_dir)
            except OSError:
                continue
            paths: list[str] = []
            for name in names:
                try:
                    path = os.readlink(os.path.join(fd_dir, name))
                except OSError:
                    continue
                if _CURSOR_FD_HINT in path.replace("\\", "/"):
                    paths.append(path)
            if paths:
                result[pid] = paths
        _STORE_PATH_CACHE = (key, {pid: list(paths) for pid, paths in result.items()})
        return {pid: list(paths) for pid, paths in result.items()}
    try:
        out = subprocess.check_output(
            ["lsof", "-Fn", "-p", ",".join(str(pid) for pid in pids)],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return {}
    current: int | None = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                current = int(line[1:])
            except ValueError:
                current = None
            continue
        if current is None or current not in pids:
            continue
        if line.startswith("n"):
            path = line[1:]
            if _CURSOR_FD_HINT in path.replace("\\", "/"):
                result.setdefault(current, []).append(path)
    _STORE_PATH_CACHE = (key, {pid: list(paths) for pid, paths in result.items()})
    return result


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _prompt_history(chat_dir: str) -> list[str]:
    """返回 newest-first 的用户 prompt 列表；缺失或损坏时为空。"""
    data = _read_json(os.path.join(chat_dir, "prompt_history.json"))
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item or "").strip()]


def _ms_to_epoch(value) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / 1000.0
    return None


def _fallback_title(native_title: str | None, first_user: str, last_user: str) -> str:
    for candidate in (native_title, first_user, last_user):
        stripped = str(candidate or "").strip()
        line = stripped.splitlines()[0].strip() if stripped.splitlines() else ""
        if line:
            return line[:60] + "…" if len(line) > 60 else line
    return "(无消息)"


def _chat_size_bytes(chat_dir: str) -> int:
    total = 0
    for name in ("meta.json", "prompt_history.json", "store.db"):
        try:
            total += os.path.getsize(os.path.join(chat_dir, name))
        except OSError:
            continue
    return total


def _build_session_info(chat_dir: str, chat_id: str) -> dict | None:
    meta = _read_json(os.path.join(chat_dir, "meta.json"))
    if not isinstance(meta, dict):
        return None
    if meta.get("hasConversation") is False:
        return None
    if meta.get("isSubagent") is True:
        return None  # Cursor Task/subagent 派生的内部会话，不是用户发起的顶层 chat

    cwd = str(meta.get("cwd") or "").strip()
    native_title = str(meta.get("title") or "").strip() or None
    prompts = _prompt_history(chat_dir)
    # prompt_history 最新在前
    last_user_msg = prompts[0] if prompts else ""
    first_user_msg = prompts[-1] if prompts else ""

    if not first_user_msg and not native_title:
        return None

    updated = _ms_to_epoch(meta.get("updatedAtMs"))
    created = _ms_to_epoch(meta.get("createdAtMs"))
    try:
        file_mtime = os.path.getmtime(os.path.join(chat_dir, "meta.json"))
    except OSError:
        try:
            file_mtime = os.path.getmtime(chat_dir)
        except OSError:
            return None
    event_time = updated or created
    session_time, time_source = effective_session_time(file_mtime, event_time)

    if last_user_msg and not native_title:
        status_tag = titles.STATUS_PENDING
    elif native_title or last_user_msg:
        status_tag = titles.STATUS_DONE
    else:
        status_tag = titles.STATUS_NONE

    size_bytes = _chat_size_bytes(chat_dir)
    store_db = os.path.join(chat_dir, "store.db")
    history_path = store_db if os.path.isfile(store_db) else chat_dir
    return make_session_info(
        source="cursor",
        id=chat_id,
        short_id=chat_id.replace("-", "")[:12],
        cwd=cwd,
        mtime=session_time,
        time_source=time_source,
        event_time=event_time,
        file_mtime=file_mtime,
        size_bytes=size_bytes,
        native_title=native_title,
        fallback_title=_fallback_title(native_title, first_user_msg, last_user_msg),
        status_tag=status_tag,
        path=history_path,
        first_user_msg=first_user_msg,
        last_user_msg=last_user_msg,
        # Cursor 历史里没有独立的「助手最终答复」字段可提取，保持空串。
    )


def scan_signature() -> tuple | None:
    """逐 chat 列表级文件 stat + agent pid 快照；禁止用工作区目录 mtime。

    不含 ``store.db-wal``：流式写入只动 WAL 时列表元数据未变，应复用上一轮
    ``scan_sessions`` 结果。会话正文缓存仍通过 ``extra_version`` 带上 WAL。
    """
    paths: list[str] = []
    if os.path.isdir(CHATS_DIR):
        try:
            workspaces = os.listdir(CHATS_DIR)
        except OSError:
            workspaces = []
        for workspace_id in workspaces:
            workspace_dir = os.path.join(CHATS_DIR, workspace_id)
            if not os.path.isdir(workspace_dir):
                continue
            try:
                chat_ids = os.listdir(workspace_dir)
            except OSError:
                continue
            for chat_id in chat_ids:
                chat_dir = os.path.join(workspace_dir, chat_id)
                if not os.path.isdir(chat_dir):
                    continue
                for name in _CHAT_LIST_SIG_FILES:
                    paths.append(os.path.join(chat_dir, name))
    return (stat_signature(paths), live_pid_snapshot("agent"))


def scan_sessions(
    cwd_filter: str | None = None,
    limit: int = 50,
    *,
    include_missing_cwd: bool = False,
) -> list[SessionInfo]:
    """扫描 Cursor CLI 会话，按 mtime 降序；只读 meta/prompt_history，不打开 store.db。"""
    if not os.path.isdir(CHATS_DIR):
        return []

    candidates: list[tuple[float, str, str]] = []
    try:
        workspaces = os.listdir(CHATS_DIR)
    except OSError:
        return []
    for workspace_id in workspaces:
        workspace_dir = os.path.join(CHATS_DIR, workspace_id)
        if not os.path.isdir(workspace_dir):
            continue
        try:
            chat_ids = os.listdir(workspace_dir)
        except OSError:
            continue
        for chat_id in chat_ids:
            chat_dir = os.path.join(workspace_dir, chat_id)
            if not os.path.isdir(chat_dir):
                continue
            meta_path = os.path.join(chat_dir, "meta.json")
            try:
                mtime = os.stat(meta_path).st_mtime
            except OSError:
                continue
            candidates.append((mtime, chat_dir, chat_id))

    candidates.sort(key=lambda c: c[0], reverse=True)

    isdir_cache: dict[str, bool] = {}

    def cached_isdir(path: str) -> bool:
        cached = isdir_cache.get(path)
        if cached is None:
            cached = os.path.isdir(path)
            isdir_cache[path] = cached
        return cached

    results: list[dict] = []
    for _, chat_dir, chat_id in candidates:
        if len(results) >= limit:
            break
        meta_path = os.path.join(chat_dir, "meta.json")
        store_db = os.path.join(chat_dir, "store.db")
        extra_version = repr((
            file_signature(os.path.join(chat_dir, "prompt_history.json")),
            file_signature(store_db),
            # WAL 里的未 checkpoint 写入不会 bump 主库 mtime；签名必须带上。
            file_signature(store_db + "-wal"),
        ))
        cache = get_cache()
        info = cache.get_session("cursor", meta_path, extra_version)
        if info is None:
            info = _build_session_info(chat_dir, chat_id)
            if info is not None:
                cache.put_session("cursor", meta_path, info, extra_version)
        if info is None:
            continue
        # 标题生成用 `agent -p` 会落盘会话；用固定前缀拦掉自产噪音。
        first_user = str(info.get("first_user_msg") or "")
        fallback = str(info.get("fallback_title") or "")
        native = str(info.get("native_title") or "")
        if (
            titles.is_title_generation_prompt(first_user)
            or titles.is_title_generation_prompt(fallback)
            or titles.is_title_generation_prompt(native)
        ):
            continue
        if is_ephemeral_agent_cwd(info["cwd"]):
            continue  # OpenConductor 管家临时 cwd，目录复活会刷屏
        if info["cwd"] and not include_missing_cwd and not cached_isdir(info["cwd"]):
            continue
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        results.append(info)

    results.sort(key=lambda s: s["mtime"], reverse=True)
    results = results[:limit]
    if not results:
        return results

    _apply_live_flags(results)
    return results


def _resume_id_from_cmdline(cmdline: str) -> str | None:
    """从 agent 命令行解析 `--resume <chatId>`；无精确 ID（含 `-1`）时返回 None。"""
    match = _RESUME_ID_RE.search(cmdline or "")
    if not match:
        return None
    resume_id = match.group("id")
    if resume_id == "-1":
        return None
    return resume_id


def _chat_ids_from_open_paths(paths: list[str]) -> list[str]:
    """从打开的文件路径提取 Cursor chatId；同一会话的 db/wal/shm 去重且保持次序。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        parsed = _chat_dir_from_open_path(path)
        if parsed is None:
            continue
        chat_id, _chat_dir = parsed
        if chat_id in seen:
            continue
        seen.add(chat_id)
        ordered.append(chat_id)
    return ordered


def _chat_dir_from_open_path(path: str) -> tuple[str, str] | None:
    """从打开的 store.db / -wal / -shm 路径取出 (chatId, 会话目录)。"""
    file_path = str(path or "").split("?", 1)[0]
    match = _OPEN_CHAT_STORE_RE.search(file_path.replace("\\", "/"))
    if not match:
        return None
    return match.group("id"), os.path.dirname(file_path)


def _find_chat_dir(chat_id: str) -> str | None:
    """在 CHATS_DIR 各工作区下定位某一 chatId 的目录。"""
    text = str(chat_id or "").strip()
    if not text or not os.path.isdir(CHATS_DIR):
        return None
    try:
        workspaces = os.listdir(CHATS_DIR)
    except OSError:
        return None
    for workspace_id in workspaces:
        chat_dir = os.path.join(CHATS_DIR, workspace_id, text)
        if os.path.isdir(chat_dir):
            return chat_dir
    return None


def _decode_store_meta_value(value) -> dict | None:
    """解析 store.db meta 表 ``key='0'`` 的 JSON；兼容明文与 hex。"""
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", "replace").strip()
    else:
        text = str(value or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        return obj
    try:
        obj = json.loads(bytes.fromhex(text).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _parent_id_from_store(chat_dir: str) -> str | None:
    """从 store.db 的 subagentInfo 取顶层父会话 id。"""
    store_path = os.path.join(chat_dir, "store.db")
    if not os.path.isfile(store_path):
        return None
    conn = connect_store_ro(store_path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    obj = _decode_store_meta_value(row[0])
    if not isinstance(obj, dict):
        return None
    info = obj.get("subagentInfo")
    if not isinstance(info, dict):
        return None
    root = str(info.get("rootParentAgentId") or "").strip()
    parent = str(info.get("parentAgentId") or "").strip()
    return root or parent or None


def _subagent_parent_id(chat_dir: str) -> str | None:
    """该目录若是 Task/subagent 派生 chat，返回应显示进行中的父会话 id。"""
    meta = _read_json(os.path.join(chat_dir, "meta.json"))
    is_subagent = isinstance(meta, dict) and meta.get("isSubagent") is True
    parent_id = _parent_id_from_store(chat_dir)
    if not is_subagent and not parent_id:
        return None
    return parent_id


def _session_for_agent_chat(
    by_id: dict[str, dict],
    chat_id: str,
    *,
    chat_dir: str | None = None,
) -> dict | None:
    """把进程绑到的 chatId 解析成列表里那张卡：顶层会话，或子代理的父会话。"""
    text = str(chat_id or "").strip()
    if not text:
        return None
    session = by_id.get(text)
    if session is not None:
        return session
    directory = chat_dir if chat_dir and os.path.isdir(chat_dir) else _find_chat_dir(text)
    if not directory:
        return None
    parent_id = _subagent_parent_id(directory)
    if not parent_id:
        return None
    return by_id.get(parent_id)


def _session_for_corral_ident(by_id: dict[str, dict], ident: str) -> dict | None:
    """用托管注入的 CORRAL_SESSION_ID / SC_SESSION_ID 匹配会话。

    原生恢复注入完整 chatId；空白新建/接力注入的是 8 位临时标识，
    与历史 chatId 无对应关系，不得拿来碰运气前缀匹配。
    """
    text = str(ident or "").strip()
    if not text:
        return None
    if text in by_id:
        return by_id[text]
    # 完整 UUID 才允许按无连字符形式或前缀对齐；短临时 id 直接放弃。
    if len(text) < 32 and "-" not in text:
        return None
    compact = text.replace("-", "")
    for session_id, session in by_id.items():
        if session_id.replace("-", "") == compact:
            return session
        if session_id.startswith(text) or session_id.replace("-", "").startswith(compact):
            return session
    return None


def _mark_live(session: dict, pid: int) -> bool:
    """若会话尚未标记存活则写入 live/pid，返回是否本次新标记。"""
    if session.get("live"):
        return False
    session["live"] = True
    session["pid"] = pid
    return True


def _apply_live_flags(sessions: list[dict]) -> None:
    """给 Cursor 会话列表就地标注 live/pid。

    同一工作目录常会同时跑多个 `agent`（旧会话 `--resume`、空白新建、跨助手接力）。
    旧实现在无 `--resume` 时按「cwd → mtime 最新未标记会话」兜底，会把空壳新建进程
    错绑到同目录里更早的真实历史（真机：标题「我想加个顶栏」却打开空白欢迎页）。

    绑定优先级（全部是正向证据，不再做 cwd 猜测）：
    1. 命令行 `--resume <chatId>`；
    2. 进程已打开的 `~/.cursor/chats/.../<chatId>/store.db`；
    3. 环境变量 `CORRAL_SESSION_ID` / `SC_SESSION_ID`（仅完整会话 id）。

    以上 chatId 若是被过滤的 Task/subagent 会话，改绑到
    ``subagentInfo.rootParentAgentId``（否则 ``parentAgentId``）对应的父会话。
    父进程已空闲或不在跑、只剩子代理进程时，父会话仍须标 live。

    同一 chat 若同时有「无 --resume 的原托管进程」和后来的 ``--resume`` 进程，
    必须优先绑前者：占位卡退役依赖 annotate 把真实会话标上原 tmux 名
    （``corral-cursor-<临时8位>``）；若先绑到二次 resume，占位卡会残留成双卡。
    """
    by_id = {str(session.get("id") or ""): session for session in sessions}
    agents = list(live_processes("agent"))
    if not agents:
        return

    open_paths = _cursor_store_paths_for_pids([pid for pid, _ in agents])
    cmdlines = {pid: process_command_line(pid) for pid, _ in agents}

    def bind_pid(pid: int, *, allow_resume: bool) -> None:
        cmdline = cmdlines.get(pid) or ""
        resume_id = _resume_id_from_cmdline(cmdline)
        if resume_id:
            if not allow_resume:
                return
            session = _session_for_agent_chat(by_id, resume_id)
            if session is not None:
                _mark_live(session, pid)
            return

        for path in open_paths.get(pid) or []:
            parsed = _chat_dir_from_open_path(path)
            if parsed is None:
                continue
            chat_id, chat_dir = parsed
            session = _session_for_agent_chat(by_id, chat_id, chat_dir=chat_dir)
            if session is not None and _mark_live(session, pid):
                return

        env = process_environ(pid)
        ident = hosted_session_id(env)
        session = _session_for_corral_ident(by_id, ident)
        if session is not None:
            _mark_live(session, pid)

    # 先无 --resume（原空白新建/接力托管），再 --resume（原生恢复或二次点开）。
    for pid, _cwd in agents:
        if _resume_id_from_cmdline(cmdlines.get(pid) or ""):
            continue
        bind_pid(pid, allow_resume=False)
    for pid, _cwd in agents:
        if not _resume_id_from_cmdline(cmdlines.get(pid) or ""):
            continue
        bind_pid(pid, allow_resume=True)


def _text_from_content(content) -> str:
    """把 message.content（字符串或 type==text 分片列表）压成纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in ("text", None):
            text = str(part.get("text") or "")
            if text:
                parts.append(text)
        elif isinstance(part, str) and part:
            parts.append(part)
    return "\n".join(parts)


def user_text_from_blob(obj: dict) -> str | None:
    if obj.get("role") != "user":
        return None
    raw = _text_from_content(obj.get("content"))
    if not raw:
        return None
    match = _USER_QUERY_RE.search(raw)
    if match:
        text = match.group(1).strip()
        return text or None
    # 无 user_query 包裹的短纯文本也认；大段 <user_info>/rules 上下文丢掉
    if "<" in raw[:80] or len(raw) > 2000:
        return None
    return raw.strip() or None


_user_text_from_blob = user_text_from_blob  # 旧私有名兼容：模块内部仍引用


def _assistant_text_from_blob(obj: dict) -> str | None:
    if obj.get("role") != "assistant":
        return None
    text = _text_from_content(obj.get("content")).strip()
    return text or None


def delete_session(path: str) -> None:
    """彻底删除单个 Cursor 会话，不可恢复。

    `path` 可能是 store.db 文件（存在时）或会话目录本身（不存在时，见
    `_build_session_info`），与 `load_conversation` 同款归一后整个会话目录一起删——
    `meta.json`/`prompt_history.json`/`store.db` 都在同一目录下，只删 store.db
    会留下其余两个文件。
    """
    chat_dir = path if os.path.isdir(path) else os.path.dirname(path)
    if os.path.isdir(chat_dir):
        shutil.rmtree(chat_dir)


def clone_session(session: SessionInfo) -> SessionInfo:
    """复制整个聊天目录并换新 UUID，原目录不动；返回新 SessionInfo。

    Cursor 没有官方 CLI 分叉参数，只能靠目录克隆。会话身份完全由目录名决定；
    样例 blob 里通常不嵌 chatId。同时拷贝 store.db 的 -wal/-shm，避免漏掉尚未
    checkpoint 的最近几轮。
    """
    import time
    import uuid

    from sesskit.i18n import t

    path = str(session.get("path") or "")
    if not path:
        raise ValueError("原会话未记录历史路径，无法复制")
    src_dir = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(src_dir):
        raise ValueError(f"原会话目录不存在：{src_dir}")
    parent = os.path.dirname(src_dir)
    new_id = str(uuid.uuid4())
    dst_dir = os.path.join(parent, new_id)
    if os.path.exists(dst_dir):
        raise ValueError(f"目标会话目录已存在：{dst_dir}")
    shutil.copytree(src_dir, dst_dir)
    meta_path = os.path.join(dst_dir, "meta.json")
    meta: dict = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, json.JSONDecodeError):
            meta = {}
    now_ms = int(time.time() * 1000)
    title = str(meta.get("title") or session.get("native_title") or session.get("fallback_title") or "")
    suffix = t("session.title.copy_suffix")
    if title and not title.endswith("（副本）") and not title.endswith(" (copy)"):
        meta["title"] = f"{title}{suffix}"
    meta["updatedAtMs"] = now_ms
    if "createdAtMs" not in meta:
        meta["createdAtMs"] = now_ms
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as exc:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise ValueError(f"写入复制会话元数据失败：{exc}") from exc
    info = _build_session_info(dst_dir, new_id)
    if info is None:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise ValueError("复制后的会话无法被扫描识别")
    return info


def connect_store_ro(store_path: str) -> sqlite3.Connection | None:
    """只读打开 Cursor store.db；必须能看见 WAL，不能加 immutable=1。

    Cursor 长期开着 WAL，最新轮次常常还停在 ``store.db-wal`` 里、主库尚未
    checkpoint。``immutable=1`` 会假设主文件不变并跳过 WAL，表现为预览和小窗
    都缺最后几条消息；主库几乎为空时甚至 ``no such table: blobs``。
    """
    try:
        uri = f"file:{os.path.abspath(store_path)}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=0.5)
    except sqlite3.Error:
        return None


_connect_store_ro = connect_store_ro  # 旧私有名兼容：模块内部仍引用


def load_conversation(path: str) -> list[ConversationMessage]:
    """按 store.db 中 JSON 消息 blob 的 rowid 顺序提取 user/assistant 正文。

    path 可以是会话目录或其中的 store.db。二进制 DAG blob 跳过。
    无 store.db 时回退 prompt_history（仅用户侧，oldest→newest）。
    """
    if os.path.isdir(path):
        chat_dir = path
        store_path = os.path.join(path, "store.db")
    else:
        store_path = path
        chat_dir = os.path.dirname(path)

    if os.path.isfile(store_path):
        messages: list[ConversationMessage] = []
        conn = _connect_store_ro(store_path)
        rows: list = []
        if conn is not None:
            try:
                # 跳过二进制 DAG blob，只读 JSON 消息行（大 store.db 预览提速）。
                rows = conn.execute(
                    "SELECT rowid, data FROM blobs "
                    "WHERE substr(data, 1, 1) = X'7B' "
                    "ORDER BY rowid"
                ).fetchall()
            except sqlite3.Error:
                rows = []
            finally:
                conn.close()
        for _, data in rows:
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
            user_text = _user_text_from_blob(obj)
            if user_text is not None:
                messages.append(ConversationMessage("user", user_text))
                continue
            agent_text = _assistant_text_from_blob(obj)
            if agent_text is not None:
                messages.append(ConversationMessage("assistant", agent_text))
        if messages:
            return messages

    prompts = _prompt_history(chat_dir)
    # newest-first → chronological
    return [
        ConversationMessage("user", text)
        for text in reversed(prompts)
    ]


if __name__ == "__main__":
    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Cursor CLI 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'运行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r}"
        )
