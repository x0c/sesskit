"""读取 Pi coding agent 的 JSONL 会话（默认位于 ``~/.pi/agent/sessions``）。"""

from __future__ import annotations

import json
import os
import re

from sesskit import pi_claims as pi_identity, titles
from sesskit.hosted import (
    cache_dir as product_cache_dir,
)
from sesskit.hosted import (
    hosted_isolation_dirname,
    hosted_session_id,
    is_hosted_isolation_dir,
)
from sesskit.models import ConversationMessage, SessionInfo, effective_session_time, make_session_info
from sesskit.parsers.common import (
    live_pid_snapshot,
    live_processes,
    open_file_paths,
    parse_timestamp,
    process_command_line,
    process_environ,
    process_start_time,
    stat_signature,
)

PI_HOME = os.path.expanduser("~/.pi/agent")
SESSIONS_DIR = os.path.join(PI_HOME, "sessions")
# Pi 官方环境变量，覆盖会话落盘目录；进程标题改写成「pi」后 cmdline 里看不到
# `--session-dir`，判活只能靠这份初始 environ（与 CORRAL_SESSION_ID 同一条路）。
PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
# `{ISO时间戳把 :. 换成 -}_{sessionId}.jsonl`
_SESSION_BASENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_(.+)\.jsonl$",
    re.IGNORECASE,
)

# 会话头时间戳在进程启动后一两秒才写入；只允许「创建时间 ≥ 启动时间 - 这个余量」。
_CREATE_AFTER_START_SLACK = 2.0
_NON_TUI_SUBCOMMANDS = frozenset(
    ("install", "remove", "uninstall", "update", "list", "config", "auth")
)
_NON_TUI_FLAGS = frozenset({
    "-p", "--print", "--export", "--list-models", "--help", "-h", "--version", "-v",
})
_VALUE_FLAGS = frozenset(
    {
        "--session", "--session-id", "--fork", "--session-dir",
        "--provider", "--model", "--api-key", "--system-prompt",
        "--append-system-prompt", "--name", "-n", "--mode", "--models",
    }
)


def message_text(content: object) -> str:
    """只取 Pi message content 中可展示的 text 分片，忽略 thinking 和工具调用。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        value = part.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts)


def read_entries(path: str) -> list[dict]:
    entries: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(item, dict):
                    entries.append(item)
    except OSError:
        pass
    return entries


def active_messages(entries: list[dict]) -> list[dict]:
    """从当前叶子沿 parentId 回溯，避免预览已分叉出去的旧分支。

    Pi v1 jsonl 的 message 没有 ``id`` / ``parentId``（官方加载时才 migrate 成
    v2 树）。只读扫描不能等那次迁移落盘：没有 id 的 message 按文件顺序平铺，
    否则整段历史会被当成「没有活动分支」丢掉。

    官方 SessionManager 从磁盘恢复时，未指定 leafId 就取文件**最后一条**记录再
    沿 parentId 回溯（见 pi-mono ``buildSessionPath``：``leaf ??= entries.at(-1)``）。
    不能用 timestamp 挑叶子：时钟回拨时会错进旧分支。
    """
    messages = [
        item for item in entries
        if item.get("type") == "message" and isinstance(item.get("message"), dict)
    ]
    if not messages:
        return []
    if any(not isinstance(item.get("id"), str) for item in messages):
        return messages
    by_id = {str(item["id"]): item for item in entries if isinstance(item.get("id"), str)}
    leaf: dict | None = None
    if entries:
        last = entries[-1]
        if isinstance(last, dict) and isinstance(last.get("id"), str) and last["id"] in by_id:
            leaf = by_id[str(last["id"])]
    if leaf is None:
        for item in reversed(entries):
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] in by_id:
                leaf = by_id[str(item["id"])]
                break
    if leaf is None:
        return messages
    path: list[dict] = []
    while isinstance(leaf, dict):
        path.append(leaf)
        parent_id = leaf.get("parentId")
        leaf = by_id.get(parent_id) if isinstance(parent_id, str) else None
    path.reverse()
    return [item for item in path if item.get("type") == "message" and isinstance(item.get("message"), dict)]


# 以下旧私有名保留别名：transcript 等核心层已改用公共名，模块内部仍引用。
_text = message_text
_read_entries = read_entries
_active_messages = active_messages


def _build_session_info(path: str) -> tuple[SessionInfo, float] | None:
    entries = _read_entries(path)
    if not entries or entries[0].get("type") != "session":
        return None
    header = entries[0]
    session_id = str(header.get("id") or "")
    if not session_id:
        return None
    cwd = str(header.get("cwd") or "")
    branch = _active_messages(entries)
    first_user = last_user = last_agent = None
    last_role = None
    event_time = parse_timestamp(header.get("timestamp"))
    for item in branch:
        message = item["message"]
        role = message.get("role")
        text = _text(message.get("content"))
        timestamp = parse_timestamp(item.get("timestamp")) or parse_timestamp(message.get("timestamp"))
        if timestamp is not None:
            event_time = timestamp
        if role == "user" and text:
            first_user = first_user or text
            last_user = text
            last_role = "user"
        elif role == "assistant" and text:
            last_agent = text
            last_role = "assistant"
    if not first_user:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    native_title = next(
        (str(item.get("name")) for item in entries if item.get("type") == "session_info" and item.get("name")),
        None,
    )
    mtime, time_source = effective_session_time(stat.st_mtime, event_time)
    status = (
        titles.STATUS_PENDING
        if last_role == "user"
        else titles.STATUS_DONE
        if last_role == "assistant"
        else titles.STATUS_NONE
    )
    created = parse_timestamp(header.get("timestamp")) or 0.0
    info = make_session_info(
        source="pi", id=session_id, short_id=session_id[:12], cwd=cwd, mtime=mtime,
        time_source=time_source, event_time=event_time, file_mtime=stat.st_mtime,
        size_bytes=stat.st_size, native_title=native_title,
        fallback_title=(native_title or first_user)[:60], status_tag=status, path=path,
        first_user_msg=first_user, last_user_msg=last_user, last_agent_msg=last_agent,
    )
    return info, created


def scan_signature() -> tuple | None:
    """逐 JSONL stat + pi pid 快照；隔离目录里的新文件也会出现在 walk 里。"""
    paths: list[str] = []
    if os.path.isdir(SESSIONS_DIR):
        for root, _dirs, names in os.walk(SESSIONS_DIR):
            for name in names:
                if name.endswith(".jsonl"):
                    paths.append(os.path.join(root, name))
    return (stat_signature(paths), live_pid_snapshot("pi"))


def scan_sessions(
    cwd_filter: str | None = None,
    limit: int = 50,
    keep_ids: set[str] | None = None,
) -> list[SessionInfo]:
    if not os.path.isdir(SESSIONS_DIR):
        return []
    keep_ids = {str(item) for item in (keep_ids or ()) if item}
    candidates: list[tuple[float, str]] = []
    for root, _dirs, names in os.walk(SESSIONS_DIR):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            try:
                candidates.append((os.stat(path).st_mtime, path))
            except OSError:
                continue
    results: list[SessionInfo] = []
    created_ts: dict[str, float] = {}
    heap_kept = 0
    isolated_kept = 0
    seen_ids: set[str] = set()
    for _mtime, path in sorted(candidates, reverse=True):
        isolated = is_hosted_isolation_dir(os.path.dirname(path))
        over_quota = isolated_kept >= limit if isolated else heap_kept >= limit
        file_id = _session_id_from_path(path) or ""
        if over_quota and file_id not in keep_ids:
            if heap_kept >= limit and isolated_kept >= limit and not keep_ids:
                break
            continue
        built = _build_session_info(path)
        if built is None:
            continue
        info, created = built
        session_id = str(info["id"])
        if session_id in seen_ids:
            continue
        if over_quota and session_id not in keep_ids:
            continue
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        if created > 0:
            created_ts[session_id] = created
        seen_ids.add(session_id)
        results.append(info)
        if over_quota:
            continue
        if isolated:
            isolated_kept += 1
        else:
            heap_kept += 1
        if heap_kept >= limit and isolated_kept >= limit and not keep_ids:
            break
    _apply_live_flags(results, created_ts)
    return results


def load_conversation(path: str) -> list[ConversationMessage]:
    result: list[ConversationMessage] = []
    for item in _active_messages(_read_entries(path)):
        message = item["message"]
        role = message.get("role")
        text = _text(message.get("content"))
        if role not in ("user", "assistant") or not text:
            continue
        timestamp = parse_timestamp(item.get("timestamp")) or parse_timestamp(message.get("timestamp"))
        result.append(ConversationMessage(role, text, timestamp))
    return result


def delete_session(path: str) -> None:
    """彻底删除一条 Pi 会话的独立 JSONL 历史。

    Pi 每条会话各自对应一个 JSONL 文件，不与其他会话共享存储；删除该文件不会
    连带删除同一工作目录下的其他会话。文件已不存在时视为操作已经完成。
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _cmdline_parts_before_prompt(cmdline: str) -> list[str]:
    """第一个非旗标词起是提问；其后不能再当 argv 去取 ``--session`` / ``-c``。

    npm 包装后常见 ``node …/cli.js --approve --session <path>``：脚本路径是位置
    参数，但不是提问，必须跳过，否则恢复旗标会被裁掉。
    """
    parts = str(cmdline or "").split()
    if not parts:
        return []
    kept = [parts[0]]
    index = 1
    argv0 = os.path.basename(parts[0])
    if argv0 in {"node", "nodejs", "bun"} and index < len(parts) and not parts[index].startswith("-"):
        kept.append(parts[index])
        index += 1
    while index < len(parts):
        token = parts[index]
        if token.startswith("--system-prompt=") or token.startswith("--append-system-prompt="):
            kept.append(token)
            break
        if token in _VALUE_FLAGS:
            kept.append(token)
            if index + 1 < len(parts):
                kept.append(parts[index + 1])
                index += 2
            else:
                index += 1
            continue
        if token.startswith("-"):
            kept.append(token)
            index += 1
            continue
        break
    return kept


def _flag_value(cmdline: str, names: tuple[str, ...]) -> str | None:
    """取命令行里 ``--session <值>`` 这类「旗标 + 下一个非旗标参数」。"""
    parts = _cmdline_parts_before_prompt(cmdline)
    wanted = set(names)
    for index, part in enumerate(parts):
        if part not in wanted or index + 1 >= len(parts):
            continue
        value = parts[index + 1]
        if value.startswith("-"):
            return None
        return value
    return None


def encode_pi_session_cwd(cwd: str) -> str:
    """Pi 把 cwd 编成 ``~/.pi/agent/sessions/`` 下的子目录名。

    与官方 ``getDefaultSessionDirPath`` 一致：realpath 后去掉打头 ``/`` ``\\``，
    再把 ``/`` ``\\`` ``:`` 换成 ``-``，外包 ``--…--``。
    """
    resolved = os.path.realpath(os.path.expanduser(str(cwd or "")))
    stripped = resolved.lstrip("/\\")
    safe = re.sub(r"[/\\:]", "-", stripped)
    return f"--{safe}--"


def hosted_session_dir(cwd: str, ident: str) -> str:
    """托管新建/接力专用目录：同 cwd 各 pane 各写各的 jsonl，不再挤进默认堆。"""
    return os.path.join(SESSIONS_DIR, encode_pi_session_cwd(cwd), hosted_isolation_dirname(ident))


def session_file_dir(path: str) -> str:
    """会话 jsonl 所在目录的 realpath；空路径返回空串。"""
    text = str(path or "")
    if not text:
        return ""
    try:
        return os.path.realpath(os.path.dirname(os.path.expanduser(text)))
    except OSError:
        return os.path.dirname(text)


def normalize_session_dir(path: str) -> str:
    """把 ``--session-dir`` / 环境变量里的目录收成 realpath。"""
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return os.path.realpath(os.path.expanduser(text))
    except OSError:
        return os.path.expanduser(text)


def _session_id_from_path(path: str) -> str | None:
    """从 Pi JSONL 文件名取出 session id；认不出返回 None。"""
    match = _SESSION_BASENAME_RE.match(os.path.basename(path.replace("\\", "/")))
    return match.group(1) if match else None


def _is_continue_cmdline(cmdline: str) -> bool:
    return bool(set(_cmdline_parts_before_prompt(cmdline)).intersection({"-c", "--continue"}))


# 命令行里把进程钉在某条会话上的旗标：corral 托管启动（--session-id / --session）
# 与用户手动恢复（-c / --resume / --fork）都算；裸 `pi` 一个都没有。
_SESSION_PIN_FLAGS = frozenset({
    "--session", "--session-id", "--session-dir", "--continue", "-c", "--resume", "-r", "--fork",
})


def _cmdline_pins_session(cmdline: str) -> bool:
    """该进程命令行是否钉住了会话（corral 托管或手动恢复都会带）。"""
    return bool(_SESSION_PIN_FLAGS.intersection(_cmdline_parts_before_prompt(cmdline)))


def is_pi_tui_cmdline(cmdline: str) -> bool:
    """交互 TUI 才算会话进程；``-p`` 打印模式与 ``auth`` / ``install`` 等子命令排除。

    跨助手接力把说明写在位置参数里，正文常有 ``list`` / ``install`` 等词；进程
    命令行是空格拼接的，第一个非旗标词若不是子命令，后面整段都是提示词，
    不能再拿去撞子命令表。
    """
    parts = str(cmdline or "").split()
    if not parts:
        return False
    index = 1
    while index < len(parts):
        token = parts[index]
        if token in _NON_TUI_FLAGS or token.startswith("--print="):
            return False
        if token.startswith("--system-prompt=") or token.startswith("--append-system-prompt="):
            return True
        if token in _NON_TUI_SUBCOMMANDS:
            return False
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        # 位置参数是初始提问，不再往后扫子命令。
        return True
    return True


def _mark_live(session: dict, pid: int) -> bool:
    if session.get("live"):
        return False
    session["live"] = True
    session["pid"] = pid
    return True


# 进程内 ``/new`` / ``/resume`` / ``/fork`` 会换一份 jsonl，但启动时的
# ``--session-id`` 与 ``CORRAL_SESSION_ID`` 仍指向旧 ident。Pi 用
# ``appendFileSync`` 写完即关，扫描经常赶不上打开瞬间。记忆必须落到磁盘：
# corral 一重启内存表是空的，否则侧栏标题停在旧卡、新历史被标成 Ended。
_pid_session_override: dict[int, str] = {}
_pid_override_started: dict[int, float] = {}
_prev_write_bytes: dict[int, int] = {}
_prev_cpu_ticks: dict[int, int] = {}
_prev_file_mtimes: dict[str, float] = {}
_LIVE_MAP_NAME = "pi-live-pids.json"
# 空闲进程认领 /new：新文件创建距旧 ident 最后活动不超过这个间隔。
_IDLE_NEW_MAX_GAP = 90 * 60
# CPU 仍在跑、但绑着的旧 ident 已明显比另一条未绑定历史更旧，才跟过去。
_STALE_NEWER_SLACK = 60.0


def _live_map_path() -> str:
    return str(product_cache_dir() / _LIVE_MAP_NAME)


def _read_live_map() -> dict[str, str]:
    path = _live_map_path()
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if key and value}


def _write_live_map(data: dict[str, str]) -> None:
    path = _live_map_path()
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=0)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _persist_key(pid: int, started: float) -> str:
    return f"{pid}:{int(started)}"


def _switch_target_possible(
    pid: int, session_id: str, created_ts: dict[str, float], starts: dict[int, float],
) -> bool:
    """该 pid 是否可能「切换到」这条会话（/new、/fork 后的认领目标）。

    切换目标必然在进程启动之后才落盘：目标会话的创建时间早于进程启动，说明
    这条绑定是写错的记忆（真实事故：同目录两条分屏会话，扫描把 A 会话记到了
    B 进程名下，而 A 比 B 的进程早创建了十几分钟）。创建时间缺失时无法判定，
    保持旧行为信任它。
    """
    created = created_ts.get(str(session_id), 0.0)
    started = starts.get(pid)
    if created <= 0 or started is None:
        return True
    return created + _CREATE_AFTER_START_SLACK >= started


def _lookup_persisted_id(data: dict[str, str], pid: int, started: float) -> str | None:
    exact = data.get(_persist_key(pid, started))
    if exact:
        return exact
    prefix = f"{pid}:"
    best: str | None = None
    best_gap = 3.0
    for key, value in data.items():
        if not key.startswith(prefix):
            continue
        try:
            stored = int(key.split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        gap = abs(float(stored) - started)
        if gap < best_gap:
            best_gap = gap
            best = value
    return best


def _pid_write_bytes(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/io", encoding="utf-8") as file:
            for line in file:
                if line.startswith("write_bytes:"):
                    return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        return None
    return None


def _pid_cpu_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as file:
            parts = file.read().split()
        return int(parts[13]) + int(parts[14])
    except (OSError, ValueError, IndexError):
        return None


def _session_last_ts(session: dict) -> float:
    for key in ("event_time", "file_mtime", "mtime"):
        value = session.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 0.0


def _session_cwd(session: dict) -> str:
    cwd = str(session.get("cwd") or "")
    if not cwd:
        return ""
    try:
        return os.path.realpath(cwd)
    except OSError:
        return cwd


def _forget_live_session(pid: int) -> None:
    """彻底剔掉某进程的「正在写哪条会话」记忆（内存 + 磁盘两份）。

    只 pop 内存表的话，下一轮 `_load_persisted_overrides` 会把磁盘上的坏记忆
    灌回来，坏绑定每轮重演一次。
    """
    _pid_session_override.pop(pid, None)
    _pid_override_started.pop(pid, None)
    data = _read_live_map()
    prefix = f"{pid}:"
    if any(key.startswith(prefix) for key in data):
        data = {key: value for key, value in data.items() if not key.startswith(prefix)}
        _write_live_map(data)


def _remember_live_session(pid: int, session_id: str, started: float | None = None) -> None:
    if not session_id:
        return
    _pid_session_override[pid] = session_id
    if started is None:
        started = _pid_override_started.get(pid)
    if started is None:
        started = process_start_time(pid)
    if started is None:
        return
    _pid_override_started[pid] = started
    data = _read_live_map()
    prefix = f"{pid}:"
    data = {key: value for key, value in data.items() if not key.startswith(prefix)}
    data[_persist_key(pid, started)] = session_id
    _write_live_map(data)


def _load_persisted_overrides(
    pids: list[int], starts: dict[int, float], created_ts: dict[str, float],
) -> None:
    data = _read_live_map()
    if not data:
        return
    live = set(pids)
    keep: dict[str, str] = {}
    invalid_keys: list[str] = []
    for pid in pids:
        started = starts.get(pid)
        if started is None:
            continue
        session_id = _lookup_persisted_id(data, pid, started)
        if not session_id:
            continue
        if not _switch_target_possible(pid, session_id, created_ts, starts):
            # 目标会话比进程还早创建，不可能是 /new 切换结果：坏记忆直接剔掉。
            invalid_keys.append(_persist_key(pid, started))
            continue
        _pid_session_override.setdefault(pid, session_id)
        _pid_override_started[pid] = started
        keep[_persist_key(pid, started)] = session_id
    stale_keys = [
        key for key in data
        if key.split(":", 1)[0].isdigit() and int(key.split(":", 1)[0]) not in live
    ]
    if stale_keys or invalid_keys:
        for key in stale_keys:
            data.pop(key, None)
        for key in invalid_keys:
            data.pop(key, None)
        data.update(keep)
        _write_live_map(data)


def reset_live_session_overrides() -> None:
    """单测隔离：清掉进程内记住的 Pi 会话切换与跨轮 IO 快照。"""
    _pid_session_override.clear()
    _pid_override_started.clear()
    _prev_write_bytes.clear()
    _prev_cpu_ticks.clear()
    _prev_file_mtimes.clear()


def _pending_owner_exists(
    session: dict,
    tui_procs: list[tuple[int, str]],
    starts: dict[int, float],
    created_ts: dict[str, float],
    free_procs: set[int],
    live_pids: set[int],
) -> bool:
    """这条未绑定会话是否还有更可能的属主：一个没被任何启动证据钉住的裸 TUI 进程。

    真实事故：同目录先开了 corral 托管的 Pi 会话 A（空闲），用户又在别的终端裸
    `pi` 开了会话 B。B 的文件一出现，「空闲认领 / 相关性认领」就把 B 当成 A 进程
    /new 的结果抢走，A 被摘掉 live、B 挂上 A 的 pid；随后 annotate 把 A 那格的
    托管名贴到 B 头上，右栏 A 格的 Your prompts 小窗显示成 B 的提问。判定：一个
    存活 TUI 进程若命令行不带任何会话旗标、也没有托管注入的环境 ident、还没被
    记成在写别的会话，且早于该会话落盘启动，这条会话大概率是它自己的新会话。
    """
    created = created_ts.get(str(session.get("id") or ""), 0.0)
    if created <= 0:
        return False
    cwd = _session_cwd(session)
    for pid, proc_cwd in tui_procs:
        if pid not in free_procs or pid in live_pids:
            continue
        started = starts.get(pid)
        if started is None:
            continue
        if proc_cwd == cwd and started <= created + _CREATE_AFTER_START_SLACK:
            return True
    return False


def _follow_switched_sessions(
    sessions: list[dict],
    tui_procs: list[tuple[int, str]],
    created_ts: dict[str, float],
    starts: dict[int, float],
    free_procs: set[int],
    rebind_to,
    session_dir_pids: set[int] | None = None,
    owned_dirs: set[str] | None = None,
) -> None:
    """启动 ident 绑上之后，把进程内 /new 换出来的新文件认领回来。

    appendFileSync 太短，单轮经常看不到打开的 jsonl；corral 重启后内存表也是
    空的。用三层不靠猜「同目录最新一条」的证据：跨轮写字节对上刚更新的文件、
    仍在跑 CPU 且旧 ident 已明显更旧、空闲进程在 90 分钟窗口内一对一认领。

    带 ``corral-<ident>/`` 隔离目录的托管进程不走这里：它们的 /new 已经
    由「该目录最新 jsonl」钉死，再认领会把别人默认目录里的新文件抢走。
    """
    session_dir_pids = session_dir_pids or set()
    owned_dirs = owned_dirs or set()
    live_pids = {item.get("pid") for item in sessions if item.get("pid")}
    file_mtimes = {
        str(session.get("id") or ""): float(session.get("file_mtime") or session.get("mtime") or 0)
        for session in sessions
        if session.get("id")
    }
    write_now = {pid: _pid_write_bytes(pid) for pid, _cwd in tui_procs}
    cpu_now = {pid: _pid_cpu_ticks(pid) for pid, _cwd in tui_procs}

    grown_pids = [
        pid for pid, _cwd in tui_procs
        if write_now.get(pid) is not None
        and pid in _prev_write_bytes
        and int(write_now[pid] or 0) > _prev_write_bytes[pid]
    ]
    grown_sessions = [
        session for session in sessions
        if str(session.get("id") or "")
        and file_mtimes.get(str(session.get("id") or ""), 0)
        > _prev_file_mtimes.get(str(session.get("id") or ""), 0)
    ]
    if len(grown_pids) == 1 and len(grown_sessions) == 1:
        pid = grown_pids[0]
        session = grown_sessions[0]
        current = next((item for item in sessions if item.get("pid") == pid), None)
        target_id = str(session.get("id") or "")
        target_dir = session_file_dir(str(session.get("path") or ""))
        if (
            pid not in session_dir_pids
            and not (target_dir and target_dir in owned_dirs)
            and current is not session
            # 目标已绑在别的活进程上时不抢：写字节增长与文件 mtime 增长是两次
            # 独立采样，同目录多条会话同时活跃时可能错位对上（真实事故：同项目
            # 两条分屏，A 的文件增长被记到 B 进程名下）。
            and not (session.get("live") and session.get("pid") != pid)
            # 切换目标必须晚于进程启动才落盘，早于进程创建的会话不可能是 /new 结果。
            and _switch_target_possible(pid, target_id, created_ts, starts)
            # 同目录还有裸 pi 进程早于目标落盘时，目标是它的新会话，不是本进程的 /new。
            and not _pending_owner_exists(
                session, tui_procs, starts, created_ts, free_procs, live_pids,
            )
        ):
            rebind_to(pid, session)

    _prev_write_bytes.clear()
    _prev_write_bytes.update(
        {pid: value for pid, value in write_now.items() if value is not None}
    )
    _prev_file_mtimes.clear()
    _prev_file_mtimes.update(file_mtimes)

    live_by_pid = {item.get("pid"): item for item in sessions if item.get("pid")}
    cpu_active = [
        pid for pid, _cwd in tui_procs
        if cpu_now.get(pid) is not None
        and pid in _prev_cpu_ticks
        and int(cpu_now[pid] or 0) > _prev_cpu_ticks[pid]
    ]
    _prev_cpu_ticks.clear()
    _prev_cpu_ticks.update(
        {pid: value for pid, value in cpu_now.items() if value is not None}
    )

    def unbound_in_cwd(cwd: str) -> list[dict]:
        return [
            session for session in sessions
            if not session.get("live") and _session_cwd(session) == cwd
        ]

    for pid, cwd in tui_procs:
        if pid not in cpu_active or pid in session_dir_pids:
            continue
        current = live_by_pid.get(pid)
        if current is None:
            continue
        bound_last = _session_last_ts(current)
        started = starts.get(pid) or 0.0
        candidates = []
        for session in unbound_in_cwd(cwd):
            created = created_ts.get(str(session.get("id") or ""), 0.0)
            if created > 0 and started and created + _CREATE_AFTER_START_SLACK < started:
                continue
            target_dir = session_file_dir(str(session.get("path") or ""))
            if target_dir and target_dir in owned_dirs:
                continue
            if _pending_owner_exists(
                session, tui_procs, starts, created_ts, free_procs, live_pids,
            ):
                continue
            last_ts = _session_last_ts(session)
            if last_ts > bound_last + _STALE_NEWER_SLACK:
                candidates.append(session)
        if len(candidates) != 1 and candidates:
            candidates = [max(candidates, key=_session_last_ts)]
        if len(candidates) == 1:
            rebind_to(pid, candidates[0])
            live_by_pid[pid] = candidates[0]

    claimed: set[int] = set()
    newcomers = [
        session for session in sessions
        if not session.get("live")
    ]
    newcomers.sort(key=lambda item: created_ts.get(str(item.get("id") or ""), 0.0))
    for session in newcomers:
        created = created_ts.get(str(session.get("id") or ""), 0.0)
        if created <= 0:
            continue
        target_dir = session_file_dir(str(session.get("path") or ""))
        if target_dir and target_dir in owned_dirs:
            continue
        # 同目录有裸 pi 进程早于本会话落盘：它才是属主，不空闲认领。
        if _pending_owner_exists(
            session, tui_procs, starts, created_ts, free_procs, live_pids,
        ):
            continue
        cwd = _session_cwd(session)
        eligible: list[tuple[int, float]] = []
        for pid, proc_cwd in tui_procs:
            if pid in claimed or pid in session_dir_pids:
                continue
            if proc_cwd != cwd:
                continue
            started = starts.get(pid)
            if started is None or created + _CREATE_AFTER_START_SLACK < started:
                continue
            current = live_by_pid.get(pid)
            if current is None:
                continue
            bound_last = _session_last_ts(current)
            if bound_last <= 0 or created <= bound_last:
                continue
            gap = created - bound_last
            if gap > _IDLE_NEW_MAX_GAP:
                continue
            eligible.append((pid, gap))
        if not eligible:
            continue
        pid, _gap = min(eligible, key=lambda item: item[1])
        rebind_to(pid, session)
        live_by_pid[pid] = session
        claimed.add(pid)


def _apply_live_flags(sessions: list[dict], created_ts: dict[str, float]) -> None:
    """给 Pi 会话列表就地标注 live/pid。

    裸 ``pi`` 不长期持有 jsonl、命令行也不带会话参数，旧实现四条正向路径
    全部落空，侧边栏就把仍在跑的会话当成已结束历史。同目录又常会同时跑
    多个 TUI，禁止再按「cwd → 最新一条」猜测。

    绑定优先级（正向证据优先，禁止「同目录只留最新一条」）：

    0. 身份扩展 claim（``corral-session-identity``）：instance + 精确 session
       id，第一权威；托管进程没有有效 claim 时保持 provisional，不猜；
    1. 进程正打开的 ``*.jsonl``（若该进程有 session-dir，打开的文件还须在该目录内）；
    2. ``corral-<ident>/`` 隔离目录（``PI_CODING_AGENT_SESSION_DIR`` /
       ``--session-dir``）：该目录里 mtime 最新的一条（托管 /new 的属主，
       不再靠空闲认领）。指向 Pi 默认 cwd 堆的 session-dir 不算隔离，以免
       把 ``--session`` 恢复改绑到堆里别人的新文件；
    3. 本进程或磁盘记住的「该 pid 上次在写哪条」（目标须晚于进程启动才落盘）；
    4. ``--session <path|id>``（原生恢复）；
    5. ``--session-id <id>``（托管新建/分叉钉死的占位 ident）；
    6. 环境变量 ``CORRAL_SESSION_ID`` / ``SC_SESSION_ID`` **精确**等于会话 id；
    7. 无 session-dir 的进程才用跨轮写字节/CPU 与空闲窗口跟 /new；
    8. ``-c`` / ``--continue`` → 该 cwd 尚未标记的最新一条；
    9. 其余裸 TUI：同一 cwd 里，按「进程启动 ≤ 会话创建」一对一认领。
    """
    processes = list(live_processes("pi"))
    live_pid_set = {pid for pid, _cwd in processes}
    for stale in [pid for pid in _pid_session_override if pid not in live_pid_set]:
        _pid_session_override.pop(stale, None)
        _pid_override_started.pop(stale, None)
    if not sessions or not processes:
        return
    by_id = {str(session.get("id") or ""): session for session in sessions if session.get("id")}
    by_path: dict[str, dict] = {}
    for session in sessions:
        path = str(session.get("path") or "")
        if not path:
            continue
        try:
            by_path[os.path.realpath(path)] = session
        except OSError:
            by_path[path] = session
    cmdlines = {pid: process_command_line(pid) for pid, _cwd in processes}
    tui_procs: list[tuple[int, str]] = []
    for pid, cwd in processes:
        cmdline = cmdlines.get(pid) or ""
        if not is_pi_tui_cmdline(cmdline):
            continue
        tui_procs.append((pid, cwd))
    if not tui_procs:
        return
    starts = {
        pid: started
        for pid, started in ((pid, process_start_time(pid)) for pid, _cwd in tui_procs)
        if started is not None
    }
    _load_persisted_overrides([pid for pid, _cwd in tui_procs], starts, created_ts)
    open_paths = open_file_paths([pid for pid, _cwd in tui_procs])
    envs = {pid: process_environ(pid) for pid, _cwd in tui_procs}

    # 身份桥 claim（corral-session-identity 扩展）：有效 claim 是 live 归属的
    # 第一权威，按 claim 的精确 sessionId，对不上再按 sessionFile 路径绑定；
    # 同一 pid 取 sequence 最大的一条。托管进程的 claim instance 必须与 env
    # 注入值一致，裸 Pi 的 native claim 直接按 pid 对号。
    claims_by_pid: dict[int, list[dict]] = {}
    for claim in pi_identity.read_claims():
        if not pi_identity.claim_is_live(claim):
            continue
        try:
            claim_pid = int(claim.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if claim_pid <= 0:
            continue
        claims_by_pid.setdefault(claim_pid, []).append(claim)

    def _proc_session_dir(pid: int) -> str:
        env = envs.get(pid) or {}
        raw = env.get(PI_SESSION_DIR_ENV) or _flag_value(cmdlines.get(pid) or "", ("--session-dir",))
        return normalize_session_dir(raw or "")

    session_dirs = {
        pid: directory
        for pid, directory in ((_pid, _proc_session_dir(_pid)) for _pid, _cwd in tui_procs)
        if directory
    }
    # 只有 corral-<ident> 隔离目录是一对一属主；恢复旧文件时 --session-dir 可能
    # 指向 Pi 默认 cwd 堆，那里挤着别人的 jsonl，不能按「目录最新一条」绑。
    exclusive_dirs = {
        pid: directory
        for pid, directory in session_dirs.items()
        if is_hosted_isolation_dir(directory)
    }
    owned_dirs = set(exclusive_dirs.values())
    session_dir_pids = set(exclusive_dirs)

    bound_pids: set[int] = set()
    # 没被任何启动证据钉住的裸 TUI 进程：它们的新会话只能靠 cwd 配对（规则 7/8）
    # 绑定，是同目录新落盘会话的天然属主；相关性/空闲认领不得抢它们的目标。
    free_procs = {
        pid for pid, _cwd in tui_procs
        if pid not in _pid_session_override
        and pid not in session_dirs
        and not _cmdline_pins_session(cmdlines.get(pid) or "")
        and not hosted_session_id(envs.get(pid) or {})
    }

    def bind_by_id_or_path(pid: int, value: str) -> dict | None:
        text = str(value or "").strip()
        if not text:
            return None
        session = None
        if text in by_id:
            session = by_id[text]
        else:
            try:
                real = os.path.realpath(os.path.expanduser(text))
            except OSError:
                real = text
            session = by_path.get(real)
            if session is None:
                file_id = _session_id_from_path(text)
                if file_id and file_id in by_id:
                    session = by_id[file_id]
            if session is None:
                matches = [item for item_id, item in by_id.items() if item_id.startswith(text)]
                if len(matches) == 1:
                    session = matches[0]
        if session is None:
            return None
        if session.get("live") and session.get("pid") != pid:
            return None
        _mark_live(session, pid)
        return session

    def bind_and_stop(
        pid: int, session: dict | None, *, remember: bool = False, source: str = "",
    ) -> None:
        if remember and session is not None:
            _remember_live_session(pid, str(session.get("id") or ""), starts.get(pid))
        bound_pids.add(pid)

    def rebind_to(pid: int, session: dict) -> None:
        for item in sessions:
            if item.get("pid") == pid and item is not session:
                item["live"] = False
                item["pid"] = None
        session["live"] = True
        session["pid"] = pid
        bind_and_stop(pid, session, remember=True, source="follow")

    def bind_open_jsonl(pid: int) -> bool:
        directory = session_dirs.get(pid)
        for path in open_paths.get(pid) or []:
            if directory:
                opened_dir = session_file_dir(path)
                if opened_dir != directory:
                    continue
            session = bind_by_id_or_path(pid, path)
            if session is not None:
                bind_and_stop(pid, session, remember=True, source="open")
                return True
        return False

    def bind_session_dir(pid: int) -> bool:
        """托管隔离目录：这个进程只能认该目录里的 jsonl，live 钉最新一条。"""
        directory = exclusive_dirs.get(pid)
        if not directory:
            return False
        candidates = [
            session for session in sessions
            if session_file_dir(str(session.get("path") or "")) == directory
            and not (session.get("live") and session.get("pid") not in (None, pid))
        ]
        bound_pids.add(pid)
        if not candidates:
            return True
        session = max(
            candidates,
            key=lambda item: float(item.get("file_mtime") or item.get("mtime") or 0),
        )
        for item in sessions:
            if item.get("pid") == pid and item is not session:
                item["live"] = False
                item["pid"] = None
        session["live"] = True
        session["pid"] = pid
        return True

    def bind_exact(pid: int) -> None:
        env_instance = str((envs.get(pid) or {}).get(pi_identity.INSTANCE_ENV) or "").strip()
        candidates = claims_by_pid.get(pid) or []
        if env_instance:
            candidates = [
                claim for claim in candidates
                if str(claim.get("instanceId") or "") == env_instance
            ]
        claim = max(
            candidates,
            key=lambda item: int(item.get("sequence") or 0),
            default=None,
        )
        if claim is not None:
            session = bind_by_id_or_path(pid, str(claim.get("sessionId") or ""))
            if session is None:
                # header id 与 claim.sessionId 对不上时（占位 ident vs uuid），
                # 精确路径仍是正向证据，不能只查 by_id 然后把 pid 钉死不绑。
                session = bind_by_id_or_path(pid, str(claim.get("sessionFile") or ""))
            if session is not None:
                _remember_live_session(pid, str(session.get("id") or ""), starts.get(pid))
            # claim 指向的会话尚未落盘或不在扫描窗口：保持 provisional，
            # 绝不回落到 cwd/mtime 猜测。
            bound_pids.add(pid)
            return
        if bind_open_jsonl(pid):
            return
        if bind_session_dir(pid):
            return
        override_id = _pid_session_override.get(pid)
        if override_id and override_id in by_id:
            if not _switch_target_possible(pid, override_id, created_ts, starts):
                # 目标会话比进程还早创建，不可能是进程内切换：坏记忆剔除后走
                # 启动旗标 / 环境变量等更硬的证据。
                _pid_session_override.pop(pid, None)
            else:
                session = by_id[override_id]
                if session.get("live") and session.get("pid") != pid:
                    _pid_session_override.pop(pid, None)
                elif (
                    (_cmdline_pins_session(cmdlines.get(pid) or "")
                     or hosted_session_id(envs.get(pid) or {}))
                    and _pending_owner_exists(
                        session, tui_procs, starts, created_ts, free_procs,
                        {item.get("pid") for item in sessions if item.get("pid")},
                    )
                ):
                    # 本进程带着 --session / --session-id / 托管 env 硬证据，而记忆
                    # 指向的会话另有更可能的属主（还在跑、未被任何启动证据钉住的
                    # 裸 pi）：这份记忆多半是相关性/空闲认领抢来的，剔除（含磁盘）
                    # 后走硬证据。
                    _forget_live_session(pid)
                else:
                    _mark_live(session, pid)
                    bind_and_stop(pid, session, source="override")
                    return
        cmdline = cmdlines.get(pid) or ""
        session_arg = _flag_value(cmdline, ("--session",))
        if session_arg:
            bind_and_stop(pid, bind_by_id_or_path(pid, session_arg), source="session")
            return
        session_id_arg = _flag_value(cmdline, ("--session-id",))
        if session_id_arg:
            session = by_id.get(session_id_arg)
            if session is not None:
                _mark_live(session, pid)
            bind_and_stop(pid, session, source="session-id")
            return
        ident = hosted_session_id(envs.get(pid) or {})
        if ident in by_id:
            session = by_id[ident]
            _mark_live(session, pid)
            bind_and_stop(pid, session, source="env")
            return
        if ident or env_instance:
            # 托管占位 ident 尚未落盘、不在本轮扫描窗口，或身份扩展没给出
            # 有效 claim：宁可未关联/provisional，不要回落到 cwd 配对。
            bound_pids.add(pid)

    for pid, _cwd in tui_procs:
        bind_exact(pid)

    _follow_switched_sessions(
        sessions, tui_procs, created_ts, starts, free_procs, rebind_to,
        session_dir_pids=session_dir_pids,
        owned_dirs=owned_dirs,
    )

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
        started = starts.get(pid)
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
