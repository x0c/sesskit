"""扫描 Codex 会话历史（~/.codex/sessions/），输出统一会话结构。

移植自 agentsync 的 codex-session-continue/scripts/list_sessions.py，
去掉了 CLI/表格输出，只保留 scan_sessions() 供调用方消费。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import datetime

from sesskit import titles
from sesskit.cache import file_signature, get_cache
from sesskit.models import ConversationMessage, SessionInfo, effective_session_time, make_session_info
from sesskit.parsers.common import (
    HostExtension,
    ephemeral_prefixes_for,
    host_cache_tag,
    is_ephemeral_agent_cwd,
    live_pid_snapshot,
    preprocess_excerpt,
    stat_signature,
)
from sesskit.parsers.common import parse_timestamp as _parse_timestamp

SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
SESSION_INDEX = os.path.expanduser("~/.codex/session_index.jsonl")


def _load_index() -> dict[str, str]:
    """加载 session_index.jsonl，返回 id -> thread_name 映射。"""
    index: dict[str, str] = {}
    if not os.path.isfile(SESSION_INDEX):
        return index
    try:
        with open(SESSION_INDEX, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    sid = obj.get("id")
                    name = obj.get("thread_name")
                    if sid and name:
                        index[sid] = name
                except (json.JSONDecodeError, ValueError):
                    pass
    except OSError:
        pass
    return index


def _find_all_session_files() -> list[str]:
    """递归扫描 ~/.codex/sessions/ 下所有 .jsonl 文件，按文件名（时间戳）降序排列。"""
    files: list[str] = []
    if not os.path.isdir(SESSIONS_DIR):
        return files
    for root, dirs, fnames in os.walk(SESSIONS_DIR):
        dirs.sort()
        for fname in fnames:
            if fname.endswith(".jsonl"):
                files.append(os.path.join(root, fname))
    files.sort(reverse=True)
    return files


def _extract_uuid_from_filename(path: str) -> str | None:
    """从文件名中提取 UUID。格式: rollout-YYYY-MM-DDThh-mm-ss-<UUID>.jsonl"""
    fname = os.path.basename(path)
    m = re.search(
        r"rollout-[\d-]+T[\d-]+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
        fname,
    )
    return m.group(1) if m else None


def _extract_datetime_from_filename(path: str) -> datetime | None:
    """从文件名提取时间戳。格式: rollout-YYYY-MM-DDThh-mm-ss-..."""
    fname = os.path.basename(path)
    m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-", fname)
    if m:
        try:
            return datetime(*[int(x) for x in m.groups()])  # noqa: DTZ001 - Filename contains local wall time.
        except ValueError:
            pass
    return None


def entry_time(entry: dict) -> float | None:
    # entry.get("payload", {}) 的默认值只在 key 缺失时生效；key 存在但值是
    # JSON null 时会拿到 None，再 .get(...) 直接 AttributeError，必须 `or {}` 兜底。
    payload = entry.get("payload") or {}
    return _parse_timestamp(entry.get("timestamp")) or _parse_timestamp(payload.get("timestamp"))


_entry_time = entry_time  # 旧私有名兼容：模块内部与测试仍引用


def _native_event_id(entry: dict, payload: dict) -> str:
    """Native per-row event id when the record carries one.

    Newer `response_item` rows always have one; older `event_msg` rows may
    not. A missing id does NOT imply static legacy history: current native
    terminal records (`task_complete` / `turn_aborted`) carry `turn_id` and
    timestamps instead of `payload.id`, so terminal identity must fall back
    to that evidence (see `_terminal_fingerprint`) rather than collapsing to
    text alone. Message-row excerpts keep the legacy text fallback.
    """
    pid = payload.get("id") if isinstance(payload, dict) else None
    return str(pid) if pid else ""


def _terminal_fingerprint(kind: str, entry: dict, payload: dict, text: str) -> str:
    """Identity material for a genuine native terminal marker.

    Anchored to the event's own stable native evidence, in preference order:
    the native event id; else the native turn id plus a stable terminal
    timestamp (the record's own `completed_at`, else the row's outer
    timestamp). Terminal text alone never mints an identity: a marker with no
    event id, no turn id, and no terminal timestamp yields an empty anchor
    (conservative unknown), never fabricated uniqueness. Rows that already
    carry a native event id keep their established shape so existing
    identities do not rotate.
    """
    pid = str(payload.get("id") or "")
    if pid:
        if kind == "turn_aborted":
            return f"{kind}:{pid}"
        return f"{kind}:{pid}:{(text or '')[:120]}"
    turn = str(payload.get("turn_id") or "")
    stamp = str(payload.get("completed_at") or entry.get("timestamp") or "")
    short = (text or "")[:120]
    if kind == "turn_aborted":
        if turn or stamp:
            return f"{kind}:{turn}:{stamp}"
        return ""
    if turn or stamp:
        return f"{kind}:{turn}:{stamp}:{short}"
    return ""


def _response_message_text(payload: dict, role: str) -> str:
    """提取新版 response_item message 的指定角色文本，忽略框架注入上下文。"""
    if payload.get("type") != "message" or payload.get("role") != role:
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(part.get("text") or "").strip()
        for part in content
        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text")
    ]
    text = "\n".join(part for part in parts if part)
    if text.startswith(("# AGENTS.md instructions", "<environment_context>")):
        return ""
    return text


def user_message_text(entry: dict) -> str:
    """兼容旧 event_msg 与新版 response_item 的真实用户输入。"""
    payload = entry.get("payload") or {}
    if not isinstance(payload, dict):
        return ""
    if entry.get("type") == "event_msg" and payload.get("type") == "user_message":
        return str(payload.get("message") or "").strip()
    if entry.get("type") == "response_item":
        return _response_message_text(payload, "user")
    return ""


_user_message_text = user_message_text  # 旧私有名兼容：模块内部仍引用


def assistant_message_text(entry: dict) -> str:
    """兼容旧 event_msg 与新版 response_item 的助手文本。"""
    payload = entry.get("payload") or {}
    if not isinstance(payload, dict):
        return ""
    if entry.get("type") == "event_msg" and payload.get("type") == "agent_message":
        return str(payload.get("message") or "").strip()
    if entry.get("type") == "response_item":
        return _response_message_text(payload, "assistant")
    return ""


_assistant_message_text = assistant_message_text  # 旧私有名兼容：模块内部仍引用


def _read_session_head(path: str, max_lines: int = 128) -> list[dict]:
    """逐行读取文件头部，找到元数据和第一条真实用户输入后停止。"""
    entries: list[dict] = []
    found_meta = False
    found_user = False
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    entries.append(obj)
                    t = obj.get("type")
                    if t == "session_meta":
                        found_meta = True
                    if _user_message_text(obj):
                        found_user = True
                    if found_meta and found_user:
                        break
                except (json.JSONDecodeError, ValueError):
                    pass
    except OSError:
        pass
    return entries


def _read_session_tail(path: str, max_bytes: int = 8192) -> list[dict]:
    """读取文件尾部若干字节，解析 JSONL 条目。"""
    entries: list[dict] = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            offset = max(0, size - max_bytes)
            f.seek(offset)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if offset > 0:
            lines = lines[1:]  # 第一行可能截断，跳过
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                pass
    except OSError:
        pass
    return entries


_BACKFILL_WINDOWS = (256 * 1024, 4 * 1024 * 1024)


def _backfill_excerpts(path: str) -> tuple[str | None, str | None]:
    """Newest user prompt / assistant reply beyond the status tail, bounded."""
    user = agent = None
    size = os.path.getsize(path)
    for window in _BACKFILL_WINDOWS:
        user = agent = None
        for entry in reversed(_read_session_tail(path, max_bytes=window)):
            user = user or _user_message_text(entry) or None
            agent = agent or _assistant_message_text(entry) or None
            if user and agent:
                break
        if agent or window >= size:
            break
    return user, agent


def _status_tag(last_event_type: str | None) -> str:
    """末轮状态判定，与 scan_claude.py 共用 titles.py 里的统一枚举。"""
    if last_event_type in ("turn_aborted", "task_complete_error"):
        return titles.STATUS_ABORTED
    if last_event_type == "user_message":
        return titles.STATUS_PENDING
    if last_event_type in ("task_complete", "agent_message"):
        return titles.STATUS_DONE
    return titles.STATUS_NONE


# Modern turn/item stream markers. The official SDK collects per-item
# notifications and returns only after the matching turn-completed
# notification; a final-answer item only selects the response text, it never
# closes the running turn. When these markers are present, assistant message
# rows are mid-turn activity (commentary, reasoning, tool chatter), never
# terminal evidence on their own.
_MODERN_TURN_TYPES = frozenset({"task_started", "item_completed"})


def _has_modern_turn_framing(entries: list[dict]) -> bool:
    """True when the read window shows the modern turn/item stream."""
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload") or {}
        if isinstance(payload, dict) and payload.get("type") in _MODERN_TURN_TYPES:
            return True
    return False


def task_complete_error_text(payload: dict) -> str:
    """Human-readable summary from a Codex ``task_complete`` error payload."""
    err = payload.get("error")
    if not err:
        return ""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("codex_error_info") or "").strip()
    return str(err).strip()


# Keep the private name as an alias for in-module call sites.
_task_complete_error_text = task_complete_error_text


def _build_session_info(
    path: str,
    index: dict[str, str],
    host: HostExtension | None = None,
) -> dict | None:
    """从一个 session 文件中提取统一结构。"""
    uuid = _extract_uuid_from_filename(path)
    if not uuid:
        return None

    dt = _extract_datetime_from_filename(path)
    head_entries = _read_session_head(path)
    tail_entries = _read_session_tail(path)

    cwd = None
    thread_source = None
    first_user_msg = None
    last_user_msg = None
    last_agent_msg = None
    last_event_type = None
    event_time = None

    for e in head_entries:
        entry_time = _entry_time(e)
        if entry_time is not None:
            event_time = entry_time
        t = e.get("type")
        # e.get("payload", {}) 的默认值只在 key 缺失时生效；key 存在但值是
        # JSON null 时会拿到 None，后续 .get(...) 直接 AttributeError，`or {}` 兜底。
        payload = e.get("payload") or {}
        pt = payload.get("type", "")
        if t == "session_meta" and cwd is None:
            cwd = payload.get("cwd")
            thread_source = payload.get("thread_source")
        user_text = _user_message_text(e)
        if user_text and first_user_msg is None:
            first_user_msg = user_text

    # 尾事件指纹：本轮最后一组结束事件的稳定字段，供 completion_id 区分轮次。
    tail_fingerprint = ""
    # Ordered-evidence positions: earlier terminal marker vs newer turn
    # activity. In the modern turn stream, later turn activity invalidates an
    # earlier terminal verdict (the new turn is running); only a later genuine
    # terminal marker can close the new turn. Token counts and other metadata
    # rows are not turn activity and never invalidate.
    last_terminal_idx: int | None = None
    last_turn_activity_idx: int | None = None
    for idx, e in enumerate(tail_entries):
        entry_time = _entry_time(e)
        if entry_time is not None:
            event_time = entry_time
        t = e.get("type")
        payload = e.get("payload") or {}
        pt = payload.get("type", "")
        user_text = _user_message_text(e)
        assistant_text = _assistant_message_text(e)
        if t == "event_msg" and pt in ("task_started", "item_completed"):
            # Newer modern turn activity: ordered evidence for invalidating
            # an earlier terminal verdict below. Separate branch (not elif):
            # activity rows carry no excerpt text of their own.
            last_turn_activity_idx = idx
        if user_text:
            last_user_msg = user_text
            last_event_type = "user_message"
            tail_fingerprint = f"user:{_native_event_id(e, payload)}:{user_text[:120]}"
        elif assistant_text:
            last_agent_msg = assistant_text
            last_event_type = "agent_message"
            tail_fingerprint = f"agent:{_native_event_id(e, payload)}:{assistant_text[:120]}"
        elif t == "event_msg" and pt == "task_complete":
            msg = str(payload.get("last_agent_message") or "").strip()
            err_text = _task_complete_error_text(payload)
            if msg:
                last_agent_msg = msg
            elif err_text:
                # Quota / provider failures often complete with null last_agent_message.
                last_agent_msg = err_text
            last_event_type = "task_complete_error" if err_text else "task_complete"
            last_terminal_idx = idx
            tail_fingerprint = _terminal_fingerprint(
                last_event_type, e, payload, msg or err_text
            )
        elif t == "event_msg" and pt == "turn_aborted":
            last_event_type = "turn_aborted"
            last_terminal_idx = idx
            tail_fingerprint = _terminal_fingerprint(last_event_type, e, payload, "")

    mtime = os.path.getmtime(path)
    resolved_event_time = event_time or (dt.timestamp() if dt else None)
    session_time, time_source = effective_session_time(mtime, resolved_event_time)
    size_bytes = os.path.getsize(path)
    fallback = (first_user_msg or "").split("\n")[0].strip()
    if len(fallback) > 60:
        fallback = fallback[:60] + "…"
    if not fallback:
        fallback = "Codex 新会话"

    status = _status_tag(last_event_type)
    if (
        status == titles.STATUS_DONE
        and last_event_type == "agent_message"
        and (
            _has_modern_turn_framing(head_entries)
            or _has_modern_turn_framing(tail_entries)
        )
    ):
        # Modern turn in progress: the trailing assistant row is per-item
        # activity inside an unclosed turn, not completion. Genuine terminal
        # evidence (`task_complete` / `turn_aborted`) keeps its own branches
        # above; a user-owned tail stays pending. Missing terminal evidence
        # is unknown with an empty id — never a fake success that notifies.
        status = titles.STATUS_NONE
    if (
        status in (titles.STATUS_DONE, titles.STATUS_ABORTED)
        and last_turn_activity_idx is not None
        and (
            last_terminal_idx is None
            or last_turn_activity_idx > last_terminal_idx
        )
    ):
        # Ordered evidence: newer modern turn activity supersedes the earlier
        # terminal verdict — the new turn is still running, so the old DONE /
        # ABORTED and its completion identity must not survive. Only a later
        # genuine terminal marker can close the new turn. Token counts and
        # other metadata rows never count as activity, so a completed turn's
        # identity stays stable under metadata appends. The fingerprint is
        # cleared alongside the status so no stale identity leaks downstream.
        status = titles.STATUS_NONE
        tail_fingerprint = ""
    if last_event_type == "agent_message":
        # Weak assistant-text-only inference: no native turn-end evidence backs
        # this verdict, so it must never carry a notification identity — even
        # when every modern framing marker has fallen outside the bounded head
        # and tail windows (framing-eviction rejection). Legacy display keeps
        # the DONE status and excerpts, but the consumer's empty-ID gate stays
        # shut. Only genuine terminal markers (`task_complete` /
        # `turn_aborted`) mint notifiable identity from their own stable
        # native evidence.
        tail_fingerprint = ""
    if not last_agent_msg:
        # List excerpts only; status and completion identity stay on the 8 KB tail.
        wider_user, wider_agent = _backfill_excerpts(path)
        last_user_msg = last_user_msg or wider_user
        last_agent_msg = last_agent_msg or wider_agent
    from sesskit.models import completion_id_for

    return make_session_info(
        source="codex",
        id=uuid,
        short_id=uuid[:8],
        cwd=cwd or "",
        mtime=session_time,
        time_source=time_source,
        event_time=resolved_event_time,
        file_mtime=mtime,
        size_bytes=size_bytes,
        native_title=index.get(uuid),
        fallback_title=fallback,
        status_tag=status,
        path=path,
        first_user_msg=preprocess_excerpt(first_user_msg, host),
        last_user_msg=preprocess_excerpt(last_user_msg, host),
        last_agent_msg=preprocess_excerpt(last_agent_msg, host),
        thread_source=thread_source,  # 运行时私有字段，见 SessionInfo 的 total=False 部分
        completion_id=completion_id_for(
            status_tag=status,
            anchor=tail_fingerprint,
            tail_text=tail_fingerprint,
        ),
    )


def _live_uuids_from_proc_fd(pid_str: str) -> list[str]:
    """Linux：遍历 /proc/<pid>/fd 逐个 readlink，从打开的文件里抽 rollout UUID。

    不判断 fd 是读还是写模式（与改动前 lsof 实现的实际行为一致——旧实现同样
    没有过滤 "w" 模式，任何打开的 rollout 文件都算命中）；调用失败静默返回空。
    """
    try:
        fd_dir = f"/proc/{pid_str}/fd"
        fd_names = os.listdir(fd_dir)
    except OSError:
        return []
    uuids: list[str] = []
    for fd_name in fd_names:
        try:
            target = os.readlink(os.path.join(fd_dir, fd_name))
        except OSError:
            continue
        if "rollout-" not in target:
            continue
        uuid = _extract_uuid_from_filename(target)
        if uuid:
            uuids.append(uuid)
    return uuids


def _live_uuids_from_lsof(pids: list[str]) -> dict[str, int]:
    """macOS 等无 /proc 的平台：一次合并 lsof 调用取代逐 pid fork。

    `-n -P` 跳过 DNS 反解和端口名解析——本机实测这是 `lsof -p <单个pid>`
    单次耗时 ~500ms 的主因，多进程时线性叠加，是首屏卡顿的根因之一。
    `-Fpn` 只输出 pid 行（`p<pid>`）和文件名行（`n<name>`），逐行解析即可
    重建 pid -> 打开文件的对应关系，不需要解析完整的人类可读表格输出。
    """
    live_ids: dict[str, int] = {}
    try:
        out = subprocess.check_output(
            ["lsof", "-n", "-P", "-Fpn", "-p", ",".join(pids)],
            stderr=subprocess.DEVNULL,
        ).decode(errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return live_ids
    current_pid: int | None = None
    for line in out.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            try:
                current_pid = int(value)
            except ValueError:
                current_pid = None
        elif tag == "n" and current_pid is not None and "rollout-" in value:
            uuid = _extract_uuid_from_filename(value)
            if uuid:
                live_ids[uuid] = current_pid
    return live_ids


def _live_session_ids() -> dict[str, int]:
    """Map live Codex rollout UUIDs to PIDs using the shared process snapshot.

    macOS pgrep exact matching can miss a native binary launched through its
    long absolute path. The shared snapshot normalizes executable basenames.
    Open rollout files still establish the actual session identity.
    """
    live_ids: dict[str, int] = {}
    pids = [str(pid) for pid in live_pid_snapshot("codex")]
    if not pids:
        return live_ids

    if sys.platform.startswith("linux"):
        for pid_str in pids:
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            for uuid in _live_uuids_from_proc_fd(pid_str):
                live_ids[uuid] = pid
        return live_ids

    return _live_uuids_from_lsof(pids)


def scan_signature() -> tuple | None:
    """逐 rollout 文件 stat + codex pid 快照；禁止用 sessions 目录 mtime。"""
    files = _find_all_session_files()
    if os.path.isfile(SESSION_INDEX):
        files = [*files, SESSION_INDEX]
    return (stat_signature(files), live_pid_snapshot("codex"))


def scan_sessions(
    cwd_filter: str | None = None,
    limit: int = 50,
    *,
    include_missing_cwd: bool = False,
    host_claim_provider: Callable[[str], Mapping[str, int]] | None = None,
    host: HostExtension | None = None,
) -> list[SessionInfo]:
    """扫描 Codex 会话，返回统一结构列表，按 mtime 降序。

    历史会话可能有上千个，但调用方只要最近 limit 条。_build_session_info 要
    读文件头尾解析 JSONL 较慢，所以先用廉价的 os.stat 按真实文件 mtime（而非
    文件名里的创建时间——同一会话被续接会更新 mtime 但不改文件名）排好序，
    凑够 limit 条有效结果就提前停止，不必解析全部历史文件。

    ``include_missing_cwd``：见 Claude 扫描同名参数。

    首屏必须 ≤1s（见 AGENTS.md 验证要求）：cwd 判活按 cwd 记忆化，避免大量
    会话共享同一个 cwd 时重复 os.path.isdir——这个调用在同步/网络目录上很
    慢，实测是首屏卡顿主因之一（结果与逐次调用字节级一致）。
    """
    index = _load_index()
    # 新版记录把真实消息放进 response_item；提升解析缓存版本，让旧版误判为
    # “新会话”的派生结果自动重读，而不必让用户手动清缓存。
    index_version = repr((file_signature(SESSION_INDEX), "response-item-v2"))
    all_files = _find_all_session_files()
    live_ids = _live_session_ids()
    provider = host_claim_provider
    if provider is None and host is not None:
        provider = host.codex_claim_provider
    if provider is not None:
        # Explicit host ownership is more precise than the native process probe.
        live_ids.update(provider(SESSIONS_DIR))

    candidates: list[tuple[float, str]] = []
    for path in all_files:
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            continue
    candidates.sort(key=lambda c: c[0], reverse=True)

    isdir_cache: dict[str, bool] = {}

    def cached_isdir(path: str) -> bool:
        cached = isdir_cache.get(path)
        if cached is None:
            cached = os.path.isdir(path)
            isdir_cache[path] = cached
        return cached

    results: list[dict] = []
    host_marker = host.title_prompt_marker if host is not None else None
    host_prefixes = ephemeral_prefixes_for(host)
    session_cache = host.cache if (host is not None and host.cache is not None) else get_cache()
    cache_version = index_version + host_cache_tag(host)
    for _, path in candidates:
        cache = session_cache
        info = cache.get_session("codex", path, cache_version)
        if info is None:
            try:
                info = _build_session_info(path, index, host)
            except OSError:
                continue
            if info is not None:
                cache.put_session("codex", path, info, cache_version)
        if info is None:
            continue
        if info["thread_source"] == "subagent":
            continue  # Codex 自身多智能体拆出的子代理线程，不是用户发起的顶层会话，
            # 会与父会话共享同一段历史开头造成列表重复
        info["live"] = info["id"] in live_ids
        info["pid"] = live_ids.get(info["id"])
        if not info["first_user_msg"] and not info["live"]:
            continue  # 已结束且没有用户消息的空会话
        if not info["first_user_msg"] and info["fallback_title"] == "(无消息)":
            # 派生缓存可能来自更新前；让已有运行中空会话也立即获得可读标题。
            info["fallback_title"] = "Codex 新会话"
        if titles.is_title_generation_prompt(info["first_user_msg"], marker=host_marker):
            continue  # host's own background title-generation noise, filtered per scan
        if is_ephemeral_agent_cwd(info["cwd"], extra_prefixes=host_prefixes):
            continue  # host-declared automation workspaces whose dirs resurrect
        if info["cwd"] and not include_missing_cwd and not cached_isdir(info["cwd"]):
            continue  # cwd 已不存在（如子 agent 的临时 scratchpad 目录已被清理），无法 resume
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        results.append(info)
        if len(results) >= limit:
            break

    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results[:limit]


def delete_session(path: str) -> None:
    """彻底删除单个 Codex 会话（一个会话就是一个 rollout JSONL 文件），不可恢复。

    `session_index.jsonl` 里可能仍留有该会话的 id -> thread_name 索引条目，
    但会话文件已不存在，扫描结果里永远不会再出现它，索引残留无害；本版不
    额外清理该索引文件（追加写、无删除接口，清理成本与收益不成比例）。
    """
    if os.path.isfile(path):
        os.unlink(path)


def load_conversation(path: str) -> list[ConversationMessage]:
    """按时间顺序读取真实用户消息和 Codex 的助手消息（含任务执行中的过程叙述 commentary 和最终答复 final_answer）。"""
    messages: list[ConversationMessage] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                payload = entry.get("payload") or {}
                if not isinstance(payload, dict):
                    continue

                # payload 里的字段即使写了 key，值也可能是 JSON null（如任务无输出就结束的
                # task_complete）；`.get(key, "")` 只在 key 缺失时才用默认值，key 存在但值为
                # null 时会拿到 None，`str(None)` 变成字面量 "None" 混进正文，必须用 `or ""` 兜底。
                user_text = _user_message_text(entry)
                assistant_text = _assistant_message_text(entry)
                payload_type = payload.get("type")
                if user_text:
                    # 新版 Codex 同一句真人输入会各写一遍 response_item（role=user）
                    # 和 event_msg.user_message，预览 / Your prompts 会成对出现。
                    # 助手侧已经按相邻正文去重；用户侧同样只留先到的那条。
                    if (
                        not messages
                        or messages[-1].role != "user"
                        or messages[-1].text != user_text
                    ):
                        messages.append(ConversationMessage("user", user_text, _entry_time(entry)))
                elif assistant_text and (
                    not messages
                    or messages[-1].role != "assistant"
                    or messages[-1].text != assistant_text
                ):
                    messages.append(ConversationMessage("assistant", assistant_text, _entry_time(entry)))
                elif entry.get("type") == "event_msg" and payload_type == "task_complete":
                    text = str(payload.get("last_agent_message") or "").strip()
                    if not text:
                        text = _task_complete_error_text(payload)
                    if text and (not messages or messages[-1].role != "assistant" or messages[-1].text != text):
                        messages.append(ConversationMessage("assistant", text, _entry_time(entry)))
    except OSError:
        return []
    return messages


if __name__ == "__main__":
    import sys

    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Codex 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'运行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r}"
        )
