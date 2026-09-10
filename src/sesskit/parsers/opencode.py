#!/usr/bin/env python3
"""扫描 OpenCode 会话历史（SQLite opencode.db），输出统一会话结构。

OpenCode v1.2.0 起把历史存进单个 SQLite 数据库（session/message/part 三表，
WAL 模式），更早版本的 JSON 文件存储不做兼容——官方升级会自动迁移，遗留用户
极少；本机没有 opencode.db 时该运行时的会话列表就是空的，不报错。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from itertools import groupby

from sesskit import titles
from sesskit.hosted import hosted_session_id
from sesskit.models import ConversationMessage, SessionInfo, make_session_info
from sesskit.parsers.common import (
    live_processes,
    process_command_line,
    process_environ,
    process_start_time,
)

DB_FILENAME = "opencode.db"

# 标题生成噪音按 time_updated 排在最前。SQL LIMIT 若等于展示条数，真实会话
# 会被挤出窗口（本机实测 top-50 里 45 条是标题生成一次性任务），随后在窗口
# 边界进进出出，侧边栏就会自己乱跳。多取再滤，滤完再截到调用方要的条数。
_SCAN_OVERFETCH = 8
_SCAN_OVERFETCH_MIN = 200
# 与 runtime.opencode.OpenCodeRuntime.SUBCOMMANDS 同步：这些词出现在 argv
# 里表示不是交互 TUI，不能拿来给会话列表标「运行中」。
_NON_TUI_SUBCOMMANDS = frozenset(
    (
        "completion", "acp", "mcp", "attach", "run", "debug", "providers",
        "agent", "upgrade", "uninstall", "serve", "web", "models", "stats",
        "export", "import", "github", "pr", "session", "plugin", "db",
    )
)
_VALUE_FLAGS = frozenset(
    {
        "-s", "--session", "-m", "--model", "--agent", "--port",
        "--hostname", "--prompt", "--log-level", "--cors",
        "--mdns-domain",
    }
)
# 会话创建可能晚于进程启动一两秒；只允许「创建时间 ≥ 启动时间 - 这个余量」。
_CREATE_AFTER_START_SLACK = 2.0

_SCAN_SQL = """
SELECT
  s.id, s.directory, s.title, s.time_created, s.time_updated,
  (SELECT m.data FROM message m WHERE m.session_id = s.id
     ORDER BY m.time_created DESC, m.id DESC LIMIT 1)              AS last_msg_data,
  (SELECT json_extract(p.data, '$.text')
     FROM part p JOIN message m ON m.id = p.message_id
     WHERE p.session_id = s.id
       AND json_extract(p.data, '$.type') = 'text'
       AND json_extract(p.data, '$.synthetic') IS NOT 1
       AND json_extract(m.data, '$.role') = 'user'
     ORDER BY m.time_created ASC, m.id ASC, p.id ASC LIMIT 1)      AS first_user_text,
  (SELECT json_extract(p.data, '$.text')
     FROM part p JOIN message m ON m.id = p.message_id
     WHERE p.session_id = s.id
       AND json_extract(p.data, '$.type') = 'text'
       AND json_extract(p.data, '$.synthetic') IS NOT 1
       AND json_extract(m.data, '$.role') = 'user'
     ORDER BY m.time_created DESC, m.id DESC, p.id DESC LIMIT 1)   AS last_user_text,
  (SELECT json_extract(p.data, '$.text')
     FROM part p JOIN message m ON m.id = p.message_id
     WHERE p.session_id = s.id
       AND json_extract(p.data, '$.type') = 'text'
       AND json_extract(p.data, '$.synthetic') IS NOT 1
       AND json_extract(m.data, '$.role') = 'assistant'
     ORDER BY m.time_created DESC, m.id DESC, p.id DESC LIMIT 1)   AS last_agent_text,
  (SELECT COALESCE(SUM(LENGTH(p.data)), 0) FROM part p
     WHERE p.session_id = s.id)                                     AS content_bytes
FROM session s
WHERE s.parent_id IS NULL
  AND s.time_archived IS NULL
ORDER BY s.time_updated DESC
LIMIT ?
"""

_CONVERSATION_SQL = """
SELECT m.id AS message_id, m.time_created, m.data AS msg_data, p.data AS part_data
FROM message m JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
  AND json_extract(p.data, '$.type') = 'text'
  AND json_extract(p.data, '$.synthetic') IS NOT 1
ORDER BY m.time_created ASC, m.id ASC, p.id ASC
"""


def _cmdline_parts_before_prompt(cmdline: str) -> list[str]:
    """``--prompt`` 之后整段都是提问；空格拼接后值边界已丢失，不能再当 argv。"""
    parts = str(cmdline or "").split()
    cut: list[str] = []
    for token in parts:
        if token == "--prompt" or token.startswith("--prompt="):
            break
        cut.append(token)
    return cut


def _session_id_from_cmdline(cmdline: str) -> str | None:
    """从 ``-s`` / ``--session`` 取出会话 ID；没有精确 ID 时返回 None。

    只看 ``--prompt`` 之前的旗标。接力说明里常出现原会话的 ``-s ses_…`` 字样，
    若整行扫描会把新建的 TUI 错绑到被接力的那条历史上。
    """
    parts = _cmdline_parts_before_prompt(cmdline)
    for index, part in enumerate(parts):
        if part.startswith("--session="):
            value = part.split("=", 1)[1].strip()
            return value or None
        if part in ("-s", "--session") and index + 1 < len(parts):
            value = parts[index + 1]
            if value.startswith("-"):
                return None
            return value
    return None


def _is_continue_cmdline(cmdline: str) -> bool:
    parts = set(_cmdline_parts_before_prompt(cmdline))
    return bool(parts.intersection({"-c", "--continue"}))


def is_opencode_tui_cmdline(cmdline: str) -> bool:
    """交互 TUI 才算会话进程；``run`` / ``serve`` / ``session`` 等子命令排除。

    ``--prompt`` 后面整段都是初始提问。跨助手接力会塞进很长的说明，里面常有
    ``session`` / ``agent`` / ``run`` 等词；进程命令行又是空格拼接的，不能再拿
    提示词当 argv 去撞子命令表，否则接力过来、仍在跑的会话会被标成已结束，
    右栏变成历史消息预览。
    """
    parts = str(cmdline or "").split()
    if not parts:
        return False
    index = 1
    while index < len(parts):
        token = parts[index]
        if token == "--prompt" or token.startswith("--prompt="):
            return True
        if token in _NON_TUI_SUBCOMMANDS:
            return False
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        # 位置参数是项目路径或提示词残片，不再往后扫子命令。
        return True
    return True


def _mark_live(session: dict, pid: int) -> bool:
    if session.get("live"):
        return False
    session["live"] = True
    session["pid"] = pid
    return True


def _session_for_corral_ident(by_id: dict[str, dict], ident: str) -> dict | None:
    """托管注入的 CORRAL_SESSION_ID 只有完整会话 ID 才绑定。

    空白新建注入的是 8 位临时标识，与 ``ses_…`` 无对应关系，不得前缀猜测。
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


def _apply_live_flags(sessions: list[dict], created_ms: dict[str, int]) -> None:
    """给 OpenCode 会话列表就地标注 live/pid。

    同一工作目录常会同时跑多个 TUI（空白新建 + 原生恢复 + 另一格新建）。
    旧实现按「cwd → time_updated 最新一条」猜测，会把仍在跑的会话标成已结束，
    点进去变成历史消息预览；多个新建还会把 pid 错绑到别人的历史上。

    绑定优先级（正向证据优先，禁止「同目录只留最新一条」）：
    1. 命令行 ``-s`` / ``--session``（原生恢复）；
    2. 环境变量 ``CORRAL_SESSION_ID`` / ``SC_SESSION_ID``（仅完整会话 id）；
    3. ``-c`` / ``--continue`` → 该 cwd 尚未标记的最新一条；
    4. 其余 TUI：同一 cwd 里，把会话认领给「启动时间不晚于创建时间」且
       启动最晚的那个进程（进程先到、会话随后落盘）。
    """
    if not sessions:
        return
    processes = list(live_processes("opencode"))
    if not processes:
        return
    by_id = {str(session.get("id") or ""): session for session in sessions if session.get("id")}
    cmdlines = {pid: process_command_line(pid) for pid, _cwd in processes}
    tui_procs: list[tuple[int, str]] = []
    for pid, cwd in processes:
        cmdline = cmdlines.get(pid) or ""
        if not is_opencode_tui_cmdline(cmdline):
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
        # 完整会话 id 但不在本轮列表（被 limit 裁掉）：不要拿去撞其他会话。
        if ident.startswith("ses_") and len(ident) >= 16:
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
        items.sort(key=lambda item: created_ms.get(str(item.get("id") or ""), 0))
        for session in items:
            created = created_ms.get(str(session.get("id") or ""), 0) / 1000.0
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


def _db_paths() -> list[str]:
    """按 OPENCODE_DATA_DIR（可逗号分隔）→ XDG_DATA_HOME → 默认路径的次序解析 db 文件。"""
    data_dir = os.environ.get("OPENCODE_DATA_DIR", "").strip()
    if data_dir:
        dirs = [d.strip() for d in data_dir.split(",") if d.strip()]
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        base = xdg if xdg else os.path.expanduser("~/.local/share")
        dirs = [os.path.join(base, "opencode")]
    return [p for p in (os.path.join(d, DB_FILENAME) for d in dirs) if os.path.isfile(p)]


def connect_ro(db_path: str) -> sqlite3.Connection | None:
    """只读打开；WAL 库在极端情况下（需要恢复且无活跃写者）可能拒绝只读打开，静默降级。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


_connect_ro = connect_ro  # 旧私有名兼容：模块内部与测试仍引用


def _status_tag(last_msg_data: str | None) -> str:
    """末轮状态判定，与 scan_claude.py / scan_codex.py 共用 titles.py 里的统一枚举。

    OpenCode 没有 Codex 那样显式的中断事件；finish 为 tool-calls/unknown 时
    宁可不下判断（STATUS_NONE），只有消息里带非空 error 字段才判已中断。
    """
    if not last_msg_data:
        return titles.STATUS_NONE
    try:
        msg = json.loads(last_msg_data)
    except (json.JSONDecodeError, ValueError):
        return titles.STATUS_NONE
    if not isinstance(msg, dict):
        return titles.STATUS_NONE
    role = msg.get("role")
    if role == "user":
        return titles.STATUS_PENDING
    if role == "assistant":
        if msg.get("error"):
            return titles.STATUS_ABORTED
        if msg.get("finish") == "stop":
            return titles.STATUS_DONE
    return titles.STATUS_NONE



def _build_session_info(row: sqlite3.Row, db_path: str) -> dict | None:
    cwd = row["directory"] or ""
    first_user = str(row["first_user_text"] or "")
    native_title = row["title"] or None
    fallback = first_user.split("\n")[0].strip()
    if len(fallback) > 60:
        fallback = fallback[:60] + "…"
    if not fallback:
        fallback = "(无消息)"
    if not native_title and fallback == "(无消息)":
        return None  # 既无原生标题也无用户正文的空会话，无展示价值

    session_id = str(row["id"])
    mtime = row["time_updated"] / 1000
    size_bytes = int(row["content_bytes"] or 0)

    return make_session_info(
        source="opencode",
        id=session_id,
        short_id=session_id[:12],
        cwd=cwd,
        mtime=mtime,
        time_source="db_time_updated",
        event_time=mtime,
        file_mtime=mtime,
        size_bytes=size_bytes,
        native_title=native_title,
        fallback_title=fallback,
        status_tag=_status_tag(row["last_msg_data"]),
        path=db_path,
        first_user_msg=first_user,
        last_user_msg=str(row["last_user_text"] or ""),
        last_agent_msg=str(row["last_agent_text"] or ""),
    )


def scan_signature() -> tuple | None:
    """候选数据库元数据与存活进程快照，供后台重扫廉价判断是否需要重新查询。

    OpenCode 历史存在单个 SQLite 文件里，任何写入都会更新该文件自身（或其 -wal
    边车文件，WAL 模式下 checkpoint 前主文件 mtime 可能滞后）的 mtime，属于可靠的
    单文件场景，不像 Claude/Codex 那样有"祖先目录 mtime 不冒泡"的问题。

    `live`/`pid` 来自独立的进程探测，进程退出后数据库通常也不再变化；若签名只看
    文件 mtime，最后一次运行状态会永久冻结。因此签名同时带上排序后的
    ``(pid, cwd)`` 全量进程快照（不再按 cwd 折叠成单 pid），让进程启停、
    同目录多 TUI 和探活恢复都能触发一次完整扫描。
    """
    paths = _db_paths()
    if not paths:
        return None
    file_signature: list[tuple[str, float]] = []
    for path in paths:
        try:
            file_signature.append((path, os.stat(path).st_mtime))
        except OSError:
            return None
        wal_path = path + "-wal"
        try:
            file_signature.append((wal_path, os.stat(wal_path).st_mtime))
        except OSError:
            pass  # 没有 WAL 边车文件（未开 WAL 或已 checkpoint）是正常情况
    live_signature = tuple(sorted(live_processes("opencode")))
    return (tuple(file_signature), live_signature)


def scan_sessions(cwd_filter: str | None = None, limit: int = 50) -> list[SessionInfo]:
    """扫描所有 OpenCode 数据目录下的会话，返回统一结构列表，按 mtime 降序。

    每个数据目录一条 SQL 拿候选（已过滤子代理会话和已归档会话），条数按
    展示 limit 超额读取，Python 侧滤掉标题生成噪音后再截到 limit——否则噪音
    会占满 SQL 窗口，真实会话在边界进进出出，侧边栏自己乱跳。实测单条 SQL
    （含四个预览子查询）仍是个位数到几十毫秒，远在首屏 ≤1s 预算内。
    """
    db_paths = _db_paths()
    successful_queries = 0
    results: list[dict] = []
    created_ms: dict[str, int] = {}
    query_limit = max(limit * _SCAN_OVERFETCH, _SCAN_OVERFETCH_MIN, limit)
    for db_path in db_paths:
        conn = _connect_ro(db_path)
        if conn is None:
            continue
        try:
            rows = conn.execute(_SCAN_SQL, (query_limit,)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        successful_queries += 1
        for row in rows:
            info = _build_session_info(row, db_path)
            if info is None:
                continue
            # 标题生成用 `opencode run` 会真实落盘会话；用固定前缀拦掉自产噪音。
            # 原生标题也要查：OpenCode 常把生成结果写成 `{"runtime:id": "标题"}`，
            # 或把被总结的那条会话的标题套到这条一次性任务上。
            first_user = str(info.get("first_user_msg") or "")
            fallback = str(info.get("fallback_title") or "")
            native = str(info.get("native_title") or "")
            if (
                titles.is_title_generation_prompt(first_user)
                or titles.is_title_generation_prompt(fallback)
                or titles.is_title_generation_prompt(native)
            ):
                continue
            if cwd_filter and not info["cwd"].startswith(cwd_filter):
                continue
            results.append(info)
            created_ms[str(info["id"])] = int(row["time_created"] or 0)

    if db_paths and successful_queries == 0:
        # “库里确实没有会话”和“所有库都暂时打不开”必须区分；后者抛给 registry，
        # 由它保留最后一次成功缓存，不能把瞬时故障误当成全量删除。
        raise RuntimeError("所有 OpenCode 会话数据库均读取失败")

    results.sort(key=lambda s: s["mtime"], reverse=True)
    results = results[:limit]
    _apply_live_flags(results, created_ms)
    return results


def delete_session(db_path: str, session_id: str) -> None:
    """彻底删除单个 OpenCode 会话，不可恢复。

    OpenCode 所有会话共享一个 SQLite 库，删除必须按 session_id 精确删行，不能
    像其他运行时那样直接删文件（会连带删掉全部其他会话）。这是全仓第一处
    可写 SQLite 连接（其余所有 DB 访问，包括本文件的 `_connect_ro`，都是
    `mode=ro` 只读打开）；按外键依赖顺序在一个事务内删 part → message →
    session，成功后 commit，任何一步失败自动 rollback，不会留下半删状态。
    """
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute(
            "DELETE FROM part WHERE message_id IN "
            "(SELECT id FROM message WHERE session_id = ?)",
            (session_id,),
        )
        conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_conversation(db_path: str, session_id: str) -> list[ConversationMessage]:
    """按时间顺序读取用户消息和助手最终答复；同一消息的多个 text part 合并为一条。"""
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(_CONVERSATION_SQL, (session_id,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    messages: list[ConversationMessage] = []
    for _, group_iter in groupby(rows, key=lambda r: r["message_id"]):
        group = list(group_iter)
        try:
            msg = json.loads(group[0]["msg_data"]) or {}
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue

        texts = []
        for r in group:
            try:
                part = json.loads(r["part_data"]) or {}
            except (json.JSONDecodeError, ValueError):
                continue
            text = str(part.get("text") or "").strip()
            if text:
                texts.append(text)
        text = "\n\n".join(texts)
        if not text:
            continue

        created = (msg.get("time") or {}).get("created")
        timestamp = created / 1000 if isinstance(created, (int, float)) else None
        messages.append(ConversationMessage(role, text, timestamp))
    return messages


if __name__ == "__main__":
    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 OpenCode 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'运行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r} "
            f"status={s['status_tag']!r}"
        )
