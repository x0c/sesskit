#!/usr/bin/env python3
"""扫描 Kimi Code CLI 会话历史（~/.kimi-code/sessions/），输出统一会话结构。

Kimi Code 的会话按「工作区 / 会话」两级目录存放：

    ~/.kimi-code/sessions/<workspace_id>/<session_id>/
        state.json                  会话元数据（标题、工作目录、创建/更新时间、最后一条 prompt）
        agents/main/wire.jsonl      主 agent 的对话流水（协议事件逐行 JSON）
        agents/<other>/wire.jsonl   子 agent 的旁路对话，扫描与预览一律忽略

元数据优先取 state.json（小而权威）；用户/助手正文只能从 wire.jsonl 里解析。
wire.jsonl 里混着体量很大的系统提示（config.update）和工具快照（llm.tools_snapshot），
逐行 json.loads 会很慢，这里先按类型子串廉价过滤，只解析真正承载对话的两类事件：

- 用户消息：type == "context.append_message" 且 message.role == "user"，
  正文在 message.content 里 type=="text" 的分片；origin.kind 非 "user" 的系统注入事件丢弃。
- 助手正文：type == "context.append_loop_event" 且 event.type == "content.part"
  且 event.part.type == "text"（part.type == "think" 是思考过程，跳过）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys

from sesskit import titles
from sesskit.cache import get_cache
from sesskit.hosted import hosted_session_id
from sesskit.models import ConversationMessage, SessionInfo, effective_session_time, make_session_info
from sesskit.parsers.common import (
    is_ephemeral_agent_cwd,
    live_pid_snapshot,
    live_processes,
    process_command_line,
    process_environ,
    process_start_time,
    stat_signature,
)
from sesskit.parsers.common import parse_timestamp as _parse_iso

KIMI_HOME = os.path.expanduser("~/.kimi-code")
SESSIONS_DIR = os.path.join(KIMI_HOME, "sessions")

# 会话创建可能晚于进程启动一两秒；只允许「创建时间 ≥ 启动时间 - 这个余量」。
_CREATE_AFTER_START_SLACK = 2.0
_NON_TUI_SUBCOMMANDS = frozenset(
    ("server", "web", "login", "logout", "auth", "version", "export", "import")
)
_PROMPT_FLAGS = frozenset({"-p", "--prompt"})
_VALUE_FLAGS = frozenset(
    {
        "-S", "--session", "-m", "--model",
        "--add-dir", "--working-dir", "--port", "--host",
    }
)

# 只解析承载对话的事件行，跳过体量很大的系统提示 / 工具快照，避免整段 json.loads。
# 用带引号的类型值（不含冒号）做子串匹配，兼容紧凑与带空格两种 JSON 写法。
_USER_EVENT_MARKER = '"context.append_message"'
_LOOP_EVENT_MARKER = '"context.append_loop_event"'


def _session_id_from_cmdline(cmdline: str) -> str | None:
    """从 ``-S`` / ``--session`` 取出会话 ID；没有精确 ID 时返回 None。"""
    parts = str(cmdline or "").split()
    for index, part in enumerate(parts):
        if part.startswith("--session="):
            value = part.split("=", 1)[1].strip()
            return value or None
        if part in ("-S", "--session") and index + 1 < len(parts):
            value = parts[index + 1]
            if value.startswith("-"):
                return None
            return value
    return None


def _is_continue_cmdline(cmdline: str) -> bool:
    parts = set(str(cmdline or "").split())
    return bool(parts.intersection({"-c", "--continue"}))


def is_kimi_tui_cmdline(cmdline: str) -> bool:
    """交互 TUI 才算会话进程；``-p`` 打印模式与 ``server`` / ``web`` 等子命令排除。"""
    parts = str(cmdline or "").split()
    if not parts:
        return False
    index = 1
    while index < len(parts):
        token = parts[index]
        if token in _PROMPT_FLAGS or token.startswith("--prompt="):
            return False
        if token in _NON_TUI_SUBCOMMANDS:
            return False
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        index += 1
    return True


def _mark_live(session: dict, pid: int) -> bool:
    if session.get("live"):
        return False
    session["live"] = True
    session["pid"] = pid
    return True


def _session_for_corral_ident(by_id: dict[str, dict], ident: str) -> dict | None:
    """托管注入的 CORRAL_SESSION_ID 只有完整会话 ID 才绑定。

    空白新建注入的是 8 位临时标识，与 ``session_…`` 无对应关系，不得前缀猜测。
    """
    text = str(ident or "").strip()
    if not text:
        return None
    if text in by_id:
        return by_id[text]
    if len(text) < 16:
        return None
    matches = [item for item_id, item in by_id.items() if item_id.startswith(text)]
    if len(matches) == 1:
        return matches[0]
    return None


def _apply_live_flags(sessions: list[dict], created_ts: dict[str, float]) -> None:
    """给 Kimi 会话列表就地标注 live/pid。

    同一工作目录常会同时跑多个 TUI。旧实现按「cwd → 最新一条」猜测，
    会把仍在跑的会话标成已结束，多个新建还会把 pid 错绑到别人的历史上。

    绑定优先级（正向证据优先，禁止「同目录只留最新一条」）：
    1. 命令行 ``-S`` / ``--session``（原生恢复）；
    2. 环境变量 ``CORRAL_SESSION_ID`` / ``SC_SESSION_ID``（仅完整会话 id）；
    3. ``-c`` / ``--continue`` → 该 cwd 尚未标记的最新一条；
    4. 其余 TUI：同一 cwd 里，按「进程启动 ≤ 会话创建」一对一认领。
    """
    if not sessions:
        return
    processes = list(live_processes("kimi-code"))
    if not processes:
        return
    by_id = {str(session.get("id") or ""): session for session in sessions if session.get("id")}
    cmdlines = {pid: process_command_line(pid) for pid, _cwd in processes}
    tui_procs: list[tuple[int, str]] = []
    for pid, cwd in processes:
        cmdline = cmdlines.get(pid) or ""
        if not is_kimi_tui_cmdline(cmdline):
            continue
        tui_procs.append((pid, cwd))
    if not tui_procs:
        return

    bound_pids: set[int] = set()

    def bind_exact(pid: int) -> None:
        cmdline = cmdlines.get(pid) or ""
        session_id = _session_id_from_cmdline(cmdline)
        if session_id:
            if session_id in by_id:
                _mark_live(by_id[session_id], pid)
            bound_pids.add(pid)
            return
        env = process_environ(pid)
        ident = hosted_session_id(env)
        session = _session_for_corral_ident(by_id, ident)
        if session is not None:
            _mark_live(session, pid)
            bound_pids.add(pid)
            return
        if ident.startswith("session_") and len(ident) >= 16:
            bound_pids.add(pid)

    for pid, _cwd in tui_procs:
        bind_exact(pid)

    remaining = [(pid, cwd) for pid, cwd in tui_procs if pid not in bound_pids]
    if not remaining:
        return

    continue_by_cwd: dict[str, list[int]] = {}
    unmatched: list[tuple[int, str, float]] = []
    for pid, cwd in remaining:
        cmdline = cmdlines.get(pid) or ""
        if _is_continue_cmdline(cmdline):
            continue_by_cwd.setdefault(cwd, []).append(pid)
            continue
        started = process_start_time(pid)
        if started is None:
            continue
        unmatched.append((pid, cwd, started))

    unbound_by_cwd: dict[str, list[dict]] = {}
    for session in sessions:
        if session.get("live"):
            continue
        cwd = session.get("cwd") or ""
        if not cwd:
            continue
        try:
            real = os.path.realpath(cwd)
        except OSError:
            real = cwd
        unbound_by_cwd.setdefault(real, []).append(session)

    for cwd, pids in continue_by_cwd.items():
        candidates = unbound_by_cwd.get(cwd) or []
        candidates.sort(key=lambda item: item.get("mtime") or 0, reverse=True)
        for pid, session in zip(pids, candidates, strict=False):
            if _mark_live(session, pid):
                bound_pids.add(pid)

    unbound_by_cwd = {
        cwd: [item for item in items if not item.get("live")]
        for cwd, items in unbound_by_cwd.items()
    }
    procs_by_cwd: dict[str, list[tuple[int, float]]] = {}
    for pid, cwd, started in unmatched:
        if pid in bound_pids:
            continue
        procs_by_cwd.setdefault(cwd, []).append((pid, started))

    for cwd, items in unbound_by_cwd.items():
        procs = procs_by_cwd.get(cwd) or []
        if not procs:
            continue
        used: set[int] = set()
        items.sort(key=lambda item: created_ts.get(str(item.get("id") or ""), 0.0))
        for session in items:
            created = created_ts.get(str(session.get("id") or ""), 0.0)
            if created <= 0:
                continue
            eligible = [
                (pid, started) for pid, started in procs
                if pid not in used and started <= created + _CREATE_AFTER_START_SLACK
            ]
            if not eligible:
                continue
            pid, _started = max(eligible, key=lambda item: item[1])
            if _mark_live(session, pid):
                used.add(pid)


def event_time(entry: dict) -> float | None:
    """wire.jsonl 每行的 time 是毫秒 epoch，转成秒。"""
    t = entry.get("time")
    if isinstance(t, (int, float)):
        return t / 1000
    return None


_event_time = event_time  # 旧私有名兼容：模块内部仍引用


def _text_from_parts(parts) -> str:
    """从 message.content 分片列表里拼接 type=="text" 的正文。"""
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            t = str(part.get("text") or "").strip()
            if t:
                texts.append(t)
    return "\n\n".join(texts)


def user_text(entry: dict) -> str | None:
    """从 context.append_message 事件里取真人用户正文；系统注入事件返回 None。"""
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    origin = message.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    # origin.kind 为 "user" 才是真人输入；task-notification 等系统事件也走 user 轮次，丢弃。
    if origin_kind not in (None, "user"):
        return None
    text = _text_from_parts(message.get("content"))
    return text or None


_user_text = user_text  # 旧私有名兼容：模块内部仍引用


def _assistant_part_text(entry: dict) -> str | None:
    """从 context.append_loop_event 的 content.part 事件里取助手文本；思考分片返回 None。"""
    event = entry.get("event")
    if not isinstance(event, dict) or event.get("type") != "content.part":
        return None
    part = event.get("part")
    if not isinstance(part, dict) or part.get("type") != "text":
        return None
    text = str(part.get("text") or "").strip()
    return text or None


def iter_message_entries(lines):
    """从原始文本行里过滤并解析出对话事件（跳过系统提示 / 工具快照等大行）。"""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if _USER_EVENT_MARKER not in line and _LOOP_EVENT_MARKER not in line:
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue


_iter_message_entries = iter_message_entries  # 旧私有名兼容：模块内部仍引用


def _read_head_lines(path: str, max_lines: int = 400) -> list[str]:
    lines: list[str] = []
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                lines.append(line)
    except OSError:
        pass
    return lines


def _read_tail_lines(path: str, max_bytes: int = 131072) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            offset = max(0, size - max_bytes)
            f.seek(offset)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 首行可能被截断，丢弃
    return lines


def _wire_path(session_dir: str) -> str:
    return os.path.join(session_dir, "agents", "main", "wire.jsonl")


def _load_state(session_dir: str) -> dict:
    try:
        with open(os.path.join(session_dir, "state.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _build_session_info(session_dir: str, session_id: str) -> dict | None:
    state = _load_state(session_dir)
    wire_path = _wire_path(session_dir)
    try:
        stat = os.stat(wire_path)
    except OSError:
        return None

    cwd = str(state.get("workDir") or "")
    native_title = state.get("title") or None
    updated_at = _parse_iso(state.get("updatedAt"))

    # 头部取首条真人用户消息；尾部取末条用户 / 助手消息并判定末轮角色。
    first_user_msg = None
    for entry in _iter_message_entries(_read_head_lines(wire_path)):
        text = _user_text(entry)
        if text:
            first_user_msg = text
            break

    last_user_msg = None
    last_agent_msg = None
    last_role = None
    event_time = None
    pending_assistant: list[str] = []

    def flush_assistant():
        nonlocal last_agent_msg
        if pending_assistant:
            last_agent_msg = "\n\n".join(pending_assistant)
            pending_assistant.clear()

    for entry in _iter_message_entries(_read_tail_lines(wire_path)):
        t = _event_time(entry)
        if t is not None:
            event_time = t
        user_text = _user_text(entry)
        if user_text is not None:
            flush_assistant()
            last_user_msg = user_text
            last_role = "user"
            continue
        agent_text = _assistant_part_text(entry)
        if agent_text is not None:
            if last_role != "assistant":
                pending_assistant.clear()
            pending_assistant.append(agent_text)
            last_role = "assistant"
    flush_assistant()

    file_mtime = updated_at if updated_at is not None else stat.st_mtime
    session_time, time_source = effective_session_time(file_mtime, event_time)

    if last_role == "user":
        status_tag = titles.STATUS_PENDING
    elif last_role == "assistant":
        status_tag = titles.STATUS_DONE
    else:
        status_tag = titles.STATUS_NONE

    # 兜底标题：原生标题 > 首条用户消息 > 最后一条 prompt。
    # candidate 可能是纯空白（如 " "），strip() 后为空串时 splitlines() 返回空列表，
    # 取 [0] 会 IndexError；先判空再取首行，纯空白候选按"无标题"处理，尝试下一个来源。
    fallback = ""
    for candidate in (native_title, first_user_msg, state.get("lastPrompt")):
        stripped = str(candidate or "").strip()
        lines = stripped.splitlines()
        line = lines[0].strip() if lines else ""
        if line:
            fallback = line[:60] + "…" if len(line) > 60 else line
            break
    if not first_user_msg and not native_title and not fallback:
        return None  # 空会话（刚创建、还没任何用户消息），无展示价值

    return make_session_info(
        source="kimi",
        id=session_id,
        short_id=session_id.replace("session_", "")[:12],
        cwd=cwd,
        mtime=session_time,
        time_source=time_source,
        event_time=event_time,
        file_mtime=file_mtime,
        size_bytes=stat.st_size,
        native_title=native_title,
        fallback_title=fallback or "(无消息)",
        status_tag=status_tag,
        path=wire_path,
        first_user_msg=first_user_msg,
        last_user_msg=last_user_msg,
        last_agent_msg=last_agent_msg,
    )


def scan_signature() -> tuple | None:
    """逐会话 state/wire stat + kimi pid 快照；禁止用工作区目录 mtime。"""
    paths: list[str] = []
    if os.path.isdir(SESSIONS_DIR):
        try:
            workspaces = os.listdir(SESSIONS_DIR)
        except OSError:
            return ((), live_pid_snapshot("kimi"))
        for workspace_id in workspaces:
            workspace_dir = os.path.join(SESSIONS_DIR, workspace_id)
            if not os.path.isdir(workspace_dir):
                continue
            try:
                session_ids = os.listdir(workspace_dir)
            except OSError:
                continue
            for session_id in session_ids:
                session_dir = os.path.join(workspace_dir, session_id)
                if not os.path.isdir(session_dir):
                    continue
                paths.append(os.path.join(session_dir, "state.json"))
                paths.append(_wire_path(session_dir))
    return (stat_signature(paths), live_pid_snapshot("kimi"))


def scan_sessions(
    cwd_filter: str | None = None,
    limit: int = 50,
    *,
    include_missing_cwd: bool = False,
) -> list[SessionInfo]:
    """扫描所有 Kimi Code 会话，返回统一结构列表，按 mtime 降序。

    先用一次廉价的 os.stat（按 wire.jsonl 文件 mtime）排序，只对最可能入选的
    候选做完整解析，凑够 limit 条有效结果就停止；首屏 ≤1s 预算见 AGENTS.md。
    """
    if not os.path.isdir(SESSIONS_DIR):
        return []

    candidates: list[tuple[float, str, str]] = []
    for workspace_id in os.listdir(SESSIONS_DIR):
        workspace_dir = os.path.join(SESSIONS_DIR, workspace_id)
        if not os.path.isdir(workspace_dir):
            continue
        for session_id in os.listdir(workspace_dir):
            session_dir = os.path.join(workspace_dir, session_id)
            if not os.path.isdir(session_dir):
                continue
            try:
                mtime = os.stat(_wire_path(session_dir)).st_mtime
            except OSError:
                continue
            candidates.append((mtime, session_dir, session_id))

    candidates.sort(key=lambda c: c[0], reverse=True)

    isdir_cache: dict[str, bool] = {}

    def cached_isdir(path: str) -> bool:
        cached = isdir_cache.get(path)
        if cached is None:
            cached = os.path.isdir(path)
            isdir_cache[path] = cached
        return cached

    results: list[dict] = []
    created_ts: dict[str, float] = {}
    for _, session_dir, session_id in candidates:
        if len(results) >= limit:
            break
        wire_path = _wire_path(session_dir)
        cache = get_cache()
        info = cache.get_session("kimi", wire_path)
        if info is None:
            info = _build_session_info(session_dir, session_id)
            if info is not None:
                cache.put_session("kimi", wire_path, info)
        if info is None:
            continue
        # 标题生成用 `kimi -p` 会落盘会话；用固定前缀拦掉自产噪音。
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
            continue  # 工作目录已删除，无法原生恢复
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        created = _parse_iso(_load_state(session_dir).get("createdAt"))
        if created:
            created_ts[session_id] = created
        results.append(info)

    results.sort(key=lambda s: s["mtime"], reverse=True)
    results = results[:limit]
    _apply_live_flags(results, created_ts)
    return results


def delete_session(path: str) -> None:
    """彻底删除单个 Kimi Code 会话，不可恢复。

    `path` 是 `_wire_path()` 返回的 `.../<workspace_id>/<session_id>/agents/main/wire.jsonl`，
    只删这一个文件会留下 state.json、agents/ 及其他子 agent 目录；必须整个会话目录
    （wire.jsonl 往上数三级）一起删。
    """
    session_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir)


def clone_session(session: SessionInfo) -> SessionInfo:
    """整目录复制会话并换新 session id；重写 state.json 里的绝对路径与标题。

    Kimi CLI 没有官方分叉参数（仅会话内 /fork），复制是自动化路径。扫描走目录
    遍历，不依赖 session_index.jsonl；索引残留不影响识别。
    """
    import uuid
    from datetime import datetime, timezone

    from sesskit.i18n import t

    path = str(session.get("path") or "")
    if not path:
        raise ValueError("原会话未记录历史路径，无法复制")
    src_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    old_id = str(session.get("id") or os.path.basename(src_dir))
    if not os.path.isdir(src_dir):
        raise ValueError(f"原会话目录不存在：{src_dir}")
    parent = os.path.dirname(src_dir)
    new_uuid = str(uuid.uuid4())
    new_id = f"session_{new_uuid}" if old_id.startswith("session_") else new_uuid
    dst_dir = os.path.join(parent, new_id)
    if os.path.exists(dst_dir):
        raise ValueError(f"目标会话目录已存在：{dst_dir}")
    shutil.copytree(src_dir, dst_dir)
    state_path = os.path.join(dst_dir, "state.json")
    state = _load_state(dst_dir)
    # 把 state 里所有仍指向旧会话目录的绝对路径改到新目录。
    raw = json.dumps(state, ensure_ascii=False)
    if src_dir in raw:
        raw = raw.replace(src_dir, dst_dir)
    if old_id in raw:
        raw = raw.replace(old_id, new_id)
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        state = _load_state(dst_dir)
    title = str(state.get("title") or session.get("native_title") or session.get("fallback_title") or "")
    suffix = t("session.title.copy_suffix")
    if title and not title.endswith("（副本）") and not title.endswith(" (copy)"):
        state["title"] = f"{title}{suffix}"
    state["updatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as exc:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise ValueError(f"写入复制会话元数据失败：{exc}") from exc
    # 碰一下 mtime，让列表排序立刻看到副本。
    try:
        os.utime(_wire_path(dst_dir), None)
    except OSError:
        pass
    info = _build_session_info(dst_dir, new_id)
    if info is None:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise ValueError("复制后的会话无法被扫描识别")
    return info


def load_conversation(path: str) -> list[ConversationMessage]:
    """按时间顺序读取真人用户消息和助手每轮文本回复。

    助手一轮里可能穿插思考、多段文本和工具调用，思考（part.type=="think"）跳过，
    连续的文本分片合并成一条助手消息，遇到下一条用户消息即断开成新一轮。
    """
    messages: list[ConversationMessage] = []
    pending_assistant: list[str] = []
    pending_ts: float | None = None

    def flush_assistant():
        nonlocal pending_ts
        if pending_assistant:
            messages.append(ConversationMessage("assistant", "\n\n".join(pending_assistant), pending_ts))
            pending_assistant.clear()
            pending_ts = None

    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for entry in _iter_message_entries(file):
                user_text = _user_text(entry)
                if user_text is not None:
                    flush_assistant()
                    messages.append(ConversationMessage("user", user_text, _event_time(entry)))
                    continue
                agent_text = _assistant_part_text(entry)
                if agent_text is not None:
                    if not pending_assistant:
                        pending_ts = _event_time(entry)
                    pending_assistant.append(agent_text)
    except OSError:
        return []
    flush_assistant()
    return messages


if __name__ == "__main__":
    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Kimi Code 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'运行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r} "
            f"status={s['status_tag']!r}"
        )
