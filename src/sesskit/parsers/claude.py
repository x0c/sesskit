"""扫描 Claude Code 会话历史（~/.claude/projects/），输出统一会话结构。

相比 agentsync 的 claude-session-continue/scripts/list_sessions.py：
- 改整文件读取为 head+tail 快扫（对 TUI 友好）。
- 修正原生标题字段名：是 aiTitle，不是 title。
- 补充提取 cwd（用于回车后 cd 到正确目录）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping

from sesskit import titles
from sesskit.cache import get_cache
from sesskit.models import ConversationMessage, SessionInfo, effective_session_time, make_session_info
from sesskit.parsers.common import (
    HostExtension,
    ephemeral_prefixes_for,
    host_cache_tag,
    is_ephemeral_agent_cwd,
    preprocess_excerpt,
    stat_signature,
)
from sesskit.parsers.common import parse_timestamp as _parse_timestamp

PROJECTS_DIR = os.path.expanduser("~/.claude/projects/")

_SKIP_PREFIXES = (
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)


def extract_text(content) -> str | None:
    """从消息 content 中提取纯文本，跳过本地命令回显。"""
    if isinstance(content, str):
        t = content.strip()
        if not t:
            return None
        command_args = re.search(r"<command-args>(.*?)</command-args>", t, re.DOTALL)
        if command_args:
            args = command_args.group(1).strip()
            return args or None
        if t.startswith(_SKIP_PREFIXES):
            return None
        if re.match(r"^<\w+>.*</\w+>$", t, re.DOTALL):
            return None
        return t
    elif isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                # part.get("text", "") 的默认值只在 key 缺失时生效；key 存在但值是
                # JSON null 时会拿到 None，.strip() 直接 AttributeError，必须 `or ""` 兜底。
                t = (part.get("text") or "").strip()
                if t:
                    texts.append(t)
        return " ".join(texts) if texts else None
    return None


_extract_text = extract_text  # 旧私有名兼容：模块内部与测试仍引用


def entry_time(entry: dict) -> float | None:
    # entry.get("snapshot", {}) 的默认值只在 key 缺失时生效；key 存在但值是 JSON null
    # 时会拿到 None，再 .get("timestamp") 直接 AttributeError，必须 `or {}` 兜底。
    snapshot = entry.get("snapshot") or {}
    return _parse_timestamp(entry.get("timestamp")) or _parse_timestamp(snapshot.get("timestamp"))


_entry_time = entry_time  # 旧私有名兼容：模块内部与测试仍引用


def _read_head(path: str, max_lines: int = 300) -> list[dict]:
    """读取文件头部若干行，提取 cwd、首条用户消息和稍晚出现的 ai-title。"""
    entries: list[dict] = []
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
                except (json.JSONDecodeError, ValueError):
                    continue
                entries.append(obj)
    except OSError:
        pass
    return entries


def _read_tail(path: str, max_bytes: int = 65536) -> list[dict]:
    """读取文件尾部若干字节，解析 JSONL 条目（用于判末轮角色 + 补抓晚出现的 ai-title）。"""
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


_BACKFILL_WINDOWS = (512 * 1024, 4 * 1024 * 1024)


def _last_texts(entries: list[dict]) -> tuple[str | None, str | None]:
    """Newest real user text and newest assistant text, same rules as the tail loop."""
    user = agent = None
    for e in reversed(entries):
        t = e.get("type")
        if user is None and t == "user":
            text = _extract_text(e.get("message", {}).get("content", ""))
            if text and text != INTERRUPTED_MARKER:
                user = text
        elif user is None and t == "attachment":
            text = queued_command_text(e)
            if text:
                user = text
        elif agent is None and t == "assistant":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and (part.get("text") or "").strip():
                        agent = part["text"]
                        break
        if user is not None and agent is not None:
            break
    return user, agent


def _backfill_excerpts(fpath: str) -> tuple[str | None, str | None]:
    """Long tool-heavy turns leave no text in the 64 KB tail; read further back, bounded."""
    user = agent = None
    size = os.path.getsize(fpath)
    for window in _BACKFILL_WINDOWS:
        user, agent = _last_texts(_read_tail(fpath, max_bytes=window))
        if agent or window >= size:
            break
    return user, agent


INTERRUPTED_MARKER = "[Request interrupted by user]"
_INTERRUPTED_MARKER = INTERRUPTED_MARKER  # 旧私有名兼容：模块内部仍引用


def system_error_text(entry: object) -> str:
    """Claude 2.1+ `system` 条目里的上游报错（401/504/连接失败…）。

    真机实采（假 key 跑出 401、代理掐出 ECONNRESET）：`error` 对象自带
    `formatted` 人话（`'401 API key is invalid.'` / `'Connection dropped …'`）
    与 `status` HTTP 码；重试会连记多条，调用方取最后一条为准。
    """
    if not isinstance(entry, dict) or entry.get("type") != "system":
        return ""
    err = entry.get("error")
    if not isinstance(err, dict):
        return ""
    formatted = str(err.get("formatted") or "").strip()
    if formatted:
        return formatted
    return str(err.get("message") or "").strip()


def queued_command_text(entry: object) -> str | None:
    """Claude 中途追问：用户在助手工作时 typed 的新消息。

    这类输入不落成 `type == "user"` 行，而是 `type == "attachment"` +
    `attachment.type == "queued_command"`，正文在 `attachment.prompt`。
    同一条还会伴随 `queue-operation` enqueue/remove 行，那两行只管调度，
    不在这里读，避免一条算两次。
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("type") != "attachment":
        return None
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None
    attachment = entry.get("attachment")
    if not isinstance(attachment, dict):
        return None
    if attachment.get("type") != "queued_command":
        return None
    origin = attachment.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    # 必须是明确的人类证据：缺 origin 不能默认当人类。实测 hook 注入的
    # <task-notification> 有的 origin.kind 就是 "task-notification"，
    # 有的干脆没有 origin；两类都不能放行。
    if origin_kind not in ("human", "user") and attachment.get("humanTurn") is not True:
        return None
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str):
        return None
    text = prompt.strip()
    if not text or text == INTERRUPTED_MARKER:
        return None
    # 纵深防御：hook 通知正文本身就是系统标记，形状上直接排除。
    lowered = text.lower()
    if lowered.startswith("<task-notification") or "<task-notification" in lowered[:200]:
        return None
    return text

_LOW_VALUE_PROMPTS = {
    "继续",
    "继续吧",
    "你继续",
    "在吗",
    "在？",
    "快点",
    "快点儿",
    "快点啊",
    "好的",
    "好",
    "ok",
    "yes",
    "no",
    "不用",
    "requestinterruptedbyuser",
    "continuefromwhereyouleftoff",
    "noresponserequested",
}

_GENERIC_NOISE_PROMPT_PREFIXES = (
    "API Error:",
    "Base directory for this skill:",
    "No response requested.",
    "This page isn't working",
    "This page isn’t working",
    "You've hit your session limit",
)

def _noise_prompt_prefixes(host: HostExtension | None) -> tuple[str, ...]:
    if host is None:
        return _GENERIC_NOISE_PROMPT_PREFIXES
    extra: list[str] = []
    if host.title_prompt_marker:
        extra.append(host.title_prompt_marker)
    extra.extend(host.title_noise_prefixes)
    if not extra:
        return _GENERIC_NOISE_PROMPT_PREFIXES
    return _GENERIC_NOISE_PROMPT_PREFIXES + tuple(extra)

_IMAGE_TITLE_PREFIX = re.compile(r"^(?:\[Image\s*#\d+\]\s*)+", re.IGNORECASE)
_SESSION_LIMIT_PREFIX = "You've hit your session limit"


def _title_line(text: str | None) -> str | None:
    if not text:
        return None
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line.startswith(("› ", "> ")):
            line = line[2:].strip()
        # Claude renders attached images before the person's text. A leading
        # image marker must not make a real prompt look like a JSON array.
        line = _IMAGE_TITLE_PREFIX.sub("", line).strip()
        if not line:
            continue
        if line.startswith(("http://", "https://")):
            continue
        return re.sub(r"\s+", " ", line)
    return None


def _normalize_title_line(line: str | None) -> str | None:
    return titles._normalize_title(line)


def _is_low_value_title(text: str | None, host: HostExtension | None = None) -> bool:
    line = _title_line(text)
    if not line:
        return True
    if line in {"...", "…"}:
        return True
    if line.startswith(("{", "[")):
        return True  # 结构化 JSON/数组片段；截断或被 pretty-print 折行后未必能 fullmatch 闭合括号
    compact = re.sub(r"[\s,，。.!！?？:：;；'\"`~～…\[\]()（）{}<>《》]+", "", line).lower()
    if compact in _LOW_VALUE_PROMPTS:
        return True
    if compact.startswith(("你测试了吗", "测试了吗", "快点继续")):
        return True
    if len(compact) <= 8 and compact.startswith(("继续", "快点")):
        return True
    return any(line.startswith(prefix) for prefix in _noise_prompt_prefixes(host))


def _short_title(text: str) -> str:
    line = _normalize_title_line(_title_line(text)) or ""
    return line[:60] + "…" if len(line) > 60 else line


def _choose_claude_fallback_title(
    candidates: list[tuple[str, str | None]],
    host: HostExtension | None = None,
) -> str:
    scored: list[tuple[int, str]] = []
    for source, text in candidates:
        if _is_low_value_title(text, host):
            continue
        title = _short_title(str(text))
        if not title:
            continue
        score = 10
        if source == "last_prompt":
            score = 40
        elif source == "last_user":
            score = 35
        elif source == "first_user":
            score = 25
        elif source == "last_agent":
            score = 20
        scored.append((score, title))

    if scored:
        return max(scored, key=lambda item: item[0])[1]
    return "(仅本地命令)"


_TEAM_LEAD_DISPATCH_PREFIX = '<teammate-message teammate_id="team-lead"'
_SESSION_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


def _is_claude_session_slug(title: str | None) -> bool:
    """Claude 2.1+ 给顶层会话写的 kebab-case 显示名，不是给人看的标题。"""
    return bool(title and _SESSION_SLUG_RE.fullmatch(title.strip()))


def _prefer_claude_native_title(current: str | None, incoming: str | None) -> str | None:
    """后写入的会话 slug 不得盖掉已经拿到的可读标题。"""
    if not incoming or incoming == "?":
        return current
    if current and _is_claude_session_slug(incoming) and not _is_claude_session_slug(current):
        return current
    return incoming


def _is_internal_claude_session(entries: list[dict], session_id: str | None = None) -> bool:
    """Teammates/subagent 会话：非用户直接发起的顶层 Claude 会话。

    只看会话开头的身份，见到首条非 meta 用户消息就停。Claude 2.1+ 会给
    顶层会话自己写入 ``type: agent-name``（会话显示名），出现在首条真人
    消息之后；若扫完整头部任意一处命中就当内部会话，正在用的真会话会
    从列表消失。Teammates 文件的 ``agent-name`` / ``isSidechain`` 出现在
    首条用户消息之前。

    后台 continuation 文件（2.1.284 `continued-in`）开头是一段盖着新 id
    的元数据块（ai-title/agent-name/last-prompt…），`agent-name` 落在首条
    用户消息之前，不能只凭早 agent-name 判内部会话。这类文件的记录自带
    蛇形 `session_id` 回指（与文件名 id 不同），见到即豁免。
    """
    if session_id:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            back = entry.get("session_id")
            if isinstance(back, str) and back.strip() and back.strip() != session_id:
                return False
    for entry in entries:
        if entry.get("isSidechain"):
            return True
        if entry.get("type") == "agent-name" and entry.get("agentName"):
            return True
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        text = _extract_text(entry.get("message", {}).get("content", ""))
        return bool(text and text.startswith(_TEAM_LEAD_DISPATCH_PREFIX))
    return False


# 终局文本之后出现执行活动记录的行类型。元数据行（ai-title / last-prompt /
# queue-operation / file-history-* / mode 等）只改文件 mtime/size，不代表
# turn 继续，必须排除在外——否则元数据追加会把同一真实结束误判成新一轮。
_CONTINUATION_TYPES = frozenset({"assistant", "user"})
# Attachments are mostly context metadata (``prompt_snapshot``, ``date``,
# ``deferred_tools_record`` …; Claude Code 2.1.291 writes a ``prompt_snapshot``
# after the final reply, before ``turn_duration``). Only a queued human prompt
# continues the turn; any real continuation also writes assistant/user rows.
_CONTINUATION_ATTACHMENTS = frozenset({"queued_command"})


def _continued_after(entries: list[dict], idx: int | None) -> bool:
    """终局候选文本行之后是否还有执行活动（工具调用、结果回执、新输入）。"""
    if idx is None:
        return False
    for later in entries[idx + 1:]:
        if not isinstance(later, dict):
            continue
        kind = later.get("type")
        if kind in _CONTINUATION_TYPES:
            return True
        if kind == "attachment":
            attachment = later.get("attachment")
            if isinstance(attachment, dict) and attachment.get("type") in _CONTINUATION_ATTACHMENTS:
                return True
    return False


def _build_session_info(
    fpath: str,
    proj: str,
    host: HostExtension | None = None,
) -> dict | None:
    session_id = os.path.basename(fpath).replace(".jsonl", "")
    head_entries = _read_head(fpath)
    if _is_internal_claude_session(head_entries, session_id):
        return None
    tail_entries = _read_tail(fpath)

    cwd = None
    first_user_msg = None
    last_prompt = None
    ai_title = None
    title_candidates: list[tuple[str, str | None]] = []

    for e in head_entries:
        if cwd is None and e.get("cwd"):
            cwd = e.get("cwd")
        if e.get("type") == "ai-title":
            ai_title = _prefer_claude_native_title(ai_title, e.get("aiTitle"))
        if e.get("type") == "user" and first_user_msg is None:
            text = _extract_text(e.get("message", {}).get("content", ""))
            if text:
                first_user_msg = text
                title_candidates.append(("first_user", text))
        if e.get("type") == "last-prompt" and e.get("lastPrompt"):
            last_prompt = e.get("lastPrompt")
            title_candidates.append(("last_prompt", last_prompt))

    last_user_msg = None
    last_agent_msg = None
    last_was_user = None
    event_time = None
    # 终局判定只认原生 turn 结束证据：assistant 文本行的 stop_reason。
    # end_turn = 本轮真正结束；无该字段是旧历史格式（天然无工具调用行），
    # 同样视为结束；tool_use / max_tokens / stop_sequence 等 = 执行中/截断，
    # 字段存在但值缺失（null）= 无终局证据，即使后面还跟着文本也不算结束。
    # 文本行之后若还有 assistant/user/attachment 行（工具调用、tool 结果回执、
    # 新一轮输入），说明 turn 在继续，之前那段文本只是 progress。
    last_text_stop: object = "unset"
    last_text_stop_present = False
    last_text_idx: int | None = None
    last_text_anchor = ""
    # Identity tail material is the text of the same terminal event that set
    # last_text_anchor — never window excerpts. A rolling-window boundary shift
    # (user rows pushed out of the 64 KB tail) must not re-key the id of the
    # same terminal event (2026-10-01 acceptance rejection: excerpt text in the
    # hash caused a double push for one completion).
    last_text_msg = ""
    # 报错只属于当前未收束的一轮：新用户消息/助手正文之后才算，旧轮的残留不算。
    last_content_idx = -1
    last_error_text = ""
    last_error_idx = -1
    last_error_anchor = ""
    # Identity tail material for the ABORTED path: the human-readable text of
    # the same error event that set last_error_anchor.
    last_error_msg = ""

    for idx, e in enumerate(tail_entries):
        entry_time = _entry_time(e)
        if entry_time is not None:
            event_time = entry_time
        t = e.get("type")
        if t == "ai-title":
            ai_title = _prefer_claude_native_title(ai_title, e.get("aiTitle"))
        elif t == "last-prompt" and e.get("lastPrompt"):
            last_prompt = e.get("lastPrompt")
            title_candidates.append(("last_prompt", last_prompt))
        elif t == "user":
            text = _extract_text(e.get("message", {}).get("content", ""))
            if text == _INTERRUPTED_MARKER:
                last_was_user = "aborted"  # 用户主动中断当前轮次，不是真实用户消息
                last_content_idx = idx
            elif text:
                last_user_msg = text
                title_candidates.append(("last_user", text))
                last_was_user = True
                last_content_idx = idx
        elif t == "attachment":
            text = queued_command_text(e)
            if text:
                last_user_msg = text
                title_candidates.append(("last_user", text))
                last_was_user = True
                last_content_idx = idx
        elif t == "assistant":
            message = e.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            if isinstance(content, list):
                for part in content:
                    # part.get("text") 可能是 JSON null（key 存在但值为 null），
                    # 裸 .get("text", "") 的默认值只在 key 缺失时生效，`or ""` 兜底。
                    if isinstance(part, dict) and part.get("type") == "text" and (part.get("text") or "").strip():
                        last_agent_msg = part["text"]
                        title_candidates.append(("last_agent", last_agent_msg))
                        last_was_user = False
                        last_content_idx = idx
                        last_text_stop = message.get("stop_reason") if isinstance(message, dict) else None
                        last_text_stop_present = isinstance(message, dict) and "stop_reason" in message
                        last_text_idx = idx
                        last_text_anchor = str(e.get("uuid") or e.get("timestamp") or "")
                        last_text_msg = part["text"]
                        break
        elif t == "system":
            err_text = system_error_text(e)
            if err_text:
                last_error_text = err_text
                last_error_idx = idx
                last_error_anchor = str(e.get("uuid") or e.get("timestamp") or "")
                last_error_msg = err_text

    stat = os.stat(fpath)
    for e in head_entries:
        entry_time = _entry_time(e)
        if entry_time is not None and (event_time is None or entry_time > event_time):
            event_time = entry_time
    session_time, time_source = effective_session_time(stat.st_mtime, event_time)
    fallback = _choose_claude_fallback_title(title_candidates, host)

    if last_error_text and last_error_idx > last_content_idx:
        # 上游报错（401/504/连接失败）且之后没有新的正文：本轮死在报错上。
        # 报错不进标题候选（标题仍用真实提问），但列表与通知要看到人话。
        status_tag = titles.STATUS_ABORTED
        last_agent_msg = last_error_text
        id_anchor = last_error_anchor
        id_tail_text = (last_error_msg or "")[:120]
    elif last_was_user == "aborted":
        status_tag = titles.STATUS_ABORTED
        id_anchor = ""
        id_tail_text = ""
    elif last_was_user is True:
        status_tag = titles.STATUS_PENDING
        id_anchor = ""
        id_tail_text = ""
    elif last_was_user is False and (last_agent_msg or "").startswith(_SESSION_LIMIT_PREFIX):
        status_tag = titles.STATUS_ABORTED
        id_anchor = last_text_anchor
        id_tail_text = (last_text_msg or "")[:120]
    elif last_was_user is False and (last_text_stop == "end_turn" or (last_text_stop is None and not last_text_stop_present)) and not _continued_after(tail_entries, last_text_idx):
        status_tag = titles.STATUS_DONE
        id_anchor = last_text_anchor
        id_tail_text = (last_text_msg or "")[:120]
    else:
        # progress 文本 + tool_use/截断/后续工具活动 = 执行中；无文本 = 未知。
        # 宁可 unknown 也不报假 DONE（假 DONE 会直接触发完成通知）。
        status_tag = titles.STATUS_NONE
        id_anchor = ""
        id_tail_text = ""

    from sesskit.models import completion_id_for

    # Identity tail material is the anchored terminal event's own text
    # (window-stable); window excerpts must never be mixed back in
    # (last_user_msg vanishes across the 64 KB boundary and would re-key
    # the same terminal event). Display excerpts still use the wider-window
    # backfill below; they just no longer feed identity.
    tail_text = id_tail_text
    if not last_agent_msg:
        # List excerpts only; status and completion_id above stay on the 64 KB window.
        wider_user, wider_agent = _backfill_excerpts(fpath)
        last_user_msg = last_user_msg or wider_user
        last_agent_msg = last_agent_msg or wider_agent
    return make_session_info(
        source="claude",
        id=session_id,
        short_id=session_id[:8],
        cwd=cwd or "",
        mtime=session_time,
        time_source=time_source,
        event_time=event_time,
        file_mtime=stat.st_mtime,
        size_bytes=stat.st_size,
        native_title=ai_title,
        fallback_title=fallback,
        status_tag=status_tag,
        path=fpath,
        first_user_msg=preprocess_excerpt(first_user_msg, host),
        last_user_msg=preprocess_excerpt(last_user_msg, host),
        last_agent_msg=preprocess_excerpt(last_agent_msg, host),
        completion_id=completion_id_for(
            status_tag=status_tag,
            anchor=id_anchor,
            tail_text=tail_text,
        ),
    )


SESSIONS_DIR = os.path.expanduser("~/.claude/sessions/")


def _live_session_ids() -> dict[str, int]:
    """扫描 ~/.claude/sessions/{pid}.json，返回进程仍存活的 sessionId -> pid 映射。

    与 active-claude-sessions skill 同一判活思路：pid 文件是 Claude Code 自己
    维护的运行时状态，os.kill(pid, 0) 能确认进程是否还真实存在（而不是残留的
    陈旧文件）。文件名本身就是 pid，判活的同时顺手记下来，供 Agent 接口把
    「哪个会话在跑」精确到进程号。
    """
    live_ids: dict[str, int] = {}
    if not os.path.isdir(SESSIONS_DIR):
        return live_ids
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            pid = int(fname[: -len(".json")])
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fname)) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        session_id = data.get("sessionId")
        if session_id:
            live_ids[session_id] = pid
    return live_ids


def _peek_head_meta(path: str, max_lines: int = 40) -> tuple[str | None, str | None, bool]:
    """只读文件头部少量行，廉价探出 cwd、首条用户消息与 teammates/subagent 标记。

    对撞上首屏 1s 硬指标的两个根因做提前拦截：自产噪音会话（后台标题生成
    留下的、以宿主标记开头的会话）和 cwd 已删的会话，
    不必等 _build_session_info 读完整 300 行头 + 64KB 尾才发现能丢弃。
    只要拿到 cwd 和首条用户消息就早停；两者任一没探到时上层不跳过，照常走
    完整解析（避免误杀头部很长的真实会话）。
    """
    cwd: str | None = None
    first_user: str | None = None
    peeked: list[dict] = []
    own_id = os.path.basename(path).replace(".jsonl", "")
    foreign_pointer = False
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
                except (json.JSONDecodeError, ValueError):
                    continue
                peeked.append(obj)
                back = obj.get("session_id")
                if isinstance(back, str) and back.strip() and back.strip() != own_id:
                    foreign_pointer = True
                if cwd is None and obj.get("cwd"):
                    cwd = obj.get("cwd")
                if obj.get("type") == "user" and first_user is None:
                    text = _extract_text(obj.get("message", {}).get("content", ""))
                    if text:
                        first_user = text
                if cwd is not None and first_user is not None and (
                    foreign_pointer or not _peek_internal_markers(peeked)
                ):
                    break
    except OSError:
        pass
    return cwd, first_user, _is_internal_claude_session(peeked, own_id)


def _peek_internal_markers(entries: list[dict]) -> bool:
    """Whether the peeked prefix already shows teammates/subagent markers.

    Only gates `_peek_head_meta`'s early exit: when markers are present but
    no foreign `session_id` back-pointer has been seen yet, the peek keeps
    reading (within its line budget) so a `continued-in` continuation file
    is not misclassified as internal.
    """
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("isSidechain"):
            return True
        if entry.get("type") == "agent-name" and entry.get("agentName"):
            return True
        if entry.get("type") == "user" and not entry.get("isMeta"):
            text = _extract_text(entry.get("message", {}).get("content", ""))
            if text and text.startswith(_TEAM_LEAD_DISPATCH_PREFIX):
                return True
    return False


def _jsonl_stat_signature() -> tuple[tuple[str, int, int], ...]:
    """逐会话 JSONL 的 stat，不用祖先目录 mtime（追加不会冒泡）。"""
    paths: list[str] = []
    if not os.path.isdir(PROJECTS_DIR):
        return ()
    try:
        projects = os.listdir(PROJECTS_DIR)
    except OSError:
        return ()
    for proj in projects:
        proj_base = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(proj_base):
            continue
        try:
            names = os.listdir(proj_base)
        except OSError:
            continue
        for fname in names:
            if fname.endswith(".jsonl"):
                paths.append(os.path.join(proj_base, fname))
    return stat_signature(paths)


def _live_pid_file_snapshot() -> tuple[int, ...]:
    """仍存活的 Claude pid 文件集合；进程退出会改变签名，避免 live 冻住。"""
    pids: list[int] = []
    if not os.path.isdir(SESSIONS_DIR):
        return ()
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return ()
    for fname in names:
        if not fname.endswith(".json"):
            continue
        try:
            pid = int(fname[: -len(".json")])
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        pids.append(pid)
    return tuple(sorted(pids))


def scan_signature() -> tuple | None:
    return (_jsonl_stat_signature(), _live_pid_file_snapshot())


def _cached_session_info(
    fpath: str,
    proj: str,
    host: HostExtension | None,
    cache,
    cache_tag: str,
) -> dict | None:
    """One history file's list record through the derived cache (no list filters)."""
    if cache_tag:
        info = cache.get_session("claude", fpath, cache_tag)
    else:
        info = cache.get_session("claude", fpath)
    if info is not None and (
        (
            info.get("fallback_title") == "(仅本地命令)"
            and _IMAGE_TITLE_PREFIX.match(str(info.get("first_user_msg") or ""))
        )
        or (
            info.get("status_tag") == titles.STATUS_DONE
            and str(info.get("last_agent_msg") or "").startswith(_SESSION_LIMIT_PREFIX)
        )
    ):
        # Reparse older cached image-first sessions and quota endings.
        info = None
    if info is not None:
        return info
    try:
        info = _build_session_info(fpath, proj, host)
    except OSError:
        return None
    if info is None:
        return None
    # Forward continuation pointer (`continued-in` may sit outside the
    # head/tail windows, hence the dedicated full-file sweep with a substring
    # pre-filter). Runs only on cache miss: the pointer arrives via a file
    # write, which already invalidates this path's cache entry.
    from sesskit.relations import claude_continuation_target

    target = claude_continuation_target(fpath)
    if (
        target
        and target != info["id"]
        and os.path.isfile(os.path.join(os.path.dirname(fpath), target + ".jsonl"))
    ):
        info["superseded_by"] = target
    if cache_tag:
        cache.put_session("claude", fpath, info, cache_tag)
    else:
        cache.put_session("claude", fpath, info)
    return info


def scan_sessions(
    cwd_filter: str | None = None,
    limit: int = 50,
    *,
    include_missing_cwd: bool = False,
    host: HostExtension | None = None,
) -> list[SessionInfo]:
    """扫描所有项目下的 Claude Code 会话，返回统一结构列表，按 mtime 降序。

    历史会话可能有成百上千个，但调用方只要最近 limit 条。真正耗时的
    _build_session_info 会读取整个文件头尾并解析 JSONL，所以先用一次廉价的
    os.stat 按文件 mtime 排好序，只对最可能入选的候选文件做完整解析，凑够
    limit 条有效结果就停止。

    ``include_missing_cwd=False``（默认）适合恢复列表：项目目录已删的
    会话无法原生 resume，直接丢掉。SessKit 归档/检索可传 True，只要历史文件
    仍在就保留。

    首屏必须 ≤1s（见 AGENTS.md 验证要求），这里做两项针对性优化，改动前后
    结果字节级一致（已用真实会话数据核验 id 顺序、兜底标题、原生标题）：
    - cwd 判活按 cwd 记忆化（同一次扫描里大量会话共享极少数 cwd，重复
      os.path.isdir 在同步/网络目录上很慢，是首屏卡顿主因之一）；
    - 完整解析前先用 _peek_head_meta 廉价探测，提前跳过自产噪音会话和
      cwd 已删的会话，避免整文件解析后才发现能丢弃（另一大主因）。
    """
    if not os.path.isdir(PROJECTS_DIR):
        return []

    live_ids = _live_session_ids()
    candidates: list[tuple[float, str, str]] = []
    for proj in os.listdir(PROJECTS_DIR):
        proj_base = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(proj_base):
            continue
        for fname in os.listdir(proj_base):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(proj_base, fname)
            try:
                mtime = os.stat(fpath).st_mtime
            except OSError:
                continue
            candidates.append((mtime, fpath, proj))

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
    cache_tag = host_cache_tag(host)
    for _mtime, fpath, proj in candidates:
        if len(results) >= limit:
            break

        peek_cwd, peek_first_user, is_internal = _peek_head_meta(fpath)
        if is_internal:
            continue  # Teammates/subagent 内部会话，不是用户发起的顶层 chat
        if titles.is_title_generation_prompt(peek_first_user, marker=host_marker):
            continue  # 廉价探测已确认是自产噪音会话，跳过整文件解析
        if peek_cwd and is_ephemeral_agent_cwd(peek_cwd, extra_prefixes=host_prefixes):
            continue  # 托管声明的临时 cwd，目录复活会刷屏
        if peek_cwd and not include_missing_cwd and not cached_isdir(peek_cwd):
            continue  # 廉价探测已确认 cwd 不存在，跳过整文件解析

        info = _cached_session_info(fpath, proj, host, session_cache, cache_tag)
        if info is None:
            continue
        if not info["first_user_msg"] or info["fallback_title"] == "(仅本地命令)":
            continue  # 无用户消息的空会话
        if titles.is_title_generation_prompt(info["first_user_msg"], marker=host_marker):
            continue  # 自产标题噪音会话，跳过（廉价探测失手时的兜底）
        if is_ephemeral_agent_cwd(info["cwd"], extra_prefixes=host_prefixes):
            continue
        if info["cwd"] and not include_missing_cwd and not cached_isdir(info["cwd"]):
            continue  # cwd 已不存在（如子 agent 的临时 scratchpad 目录已被清理），无法 resume
        if cwd_filter and not info["cwd"].startswith(cwd_filter):
            continue
        info["live"] = info["id"] in live_ids
        info["pid"] = live_ids.get(info["id"])
        results.append(info)

    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results[:limit]


def refresh_session(
    session: Mapping[str, object],
    *,
    host: HostExtension | None = None,
) -> SessionInfo | None:
    """Re-derive one already listed session from its native history.

    Uses the same builder and derived cache as ``scan_sessions`` so status,
    ``completion_id`` and excerpts match the next scan exactly. List
    membership filters and liveness are skipped: the caller listed the
    session already and owns ``live``/``pid``. None when the history is gone
    or no longer yields a session.
    """
    fpath = str(session.get("path") or "")
    if not fpath.endswith(".jsonl") or not os.path.isfile(fpath):
        return None
    cache = host.cache if (host is not None and host.cache is not None) else get_cache()
    proj = os.path.basename(os.path.dirname(fpath))
    return _cached_session_info(fpath, proj, host, cache, host_cache_tag(host))


def delete_session(path: str) -> None:
    """彻底删除单个 Claude Code 会话（一个会话就是一个 JSONL 文件），不可恢复。"""
    if os.path.isfile(path):
        os.unlink(path)


def load_conversation(path: str) -> list[ConversationMessage]:
    """按时间顺序读取真实用户消息和 Claude 的每段文本回复。

    注意：一次 assistant 轮次里 thinking/text/tool_use 各是独立的 JSONL 行，且共享同一个
    `stop_reason`（哪怕这行本身是纯文本、后面还接着工具调用，`stop_reason` 也是
    `tool_use`）。之前按 `stop_reason in (None, "end_turn")` 过滤会把工具调用前后夹带的文本
    说明整段丢掉，只保留触发了 `stop_reason=None` 分支的历史遗留格式和轮次末尾无工具调用
    的纯文本；这里只按内容是否为空文本过滤，不再看 `stop_reason`。
    """
    messages: list[ConversationMessage] = []
    pending_legacy_answer: str | None = None
    pending_legacy_ts: float | None = None
    # 上游报错（401/504/连接失败）：重试连记多条，只保留最后一条；之后若出现
    # 真实助手正文说明已恢复，丢弃它；新用户消息或文件结束才落盘它。
    pending_error: str | None = None
    pending_error_ts: float | None = None

    def flush_legacy_answer() -> None:
        nonlocal pending_legacy_answer, pending_legacy_ts
        if pending_legacy_answer and (
            not messages or messages[-1].role != "assistant" or messages[-1].text != pending_legacy_answer
        ):
            messages.append(ConversationMessage("assistant", pending_legacy_answer, pending_legacy_ts))
        pending_legacy_answer = None
        pending_legacy_ts = None

    def flush_error() -> None:
        nonlocal pending_error, pending_error_ts
        if pending_error and (
            not messages or messages[-1].role != "assistant" or messages[-1].text != pending_error
        ):
            messages.append(ConversationMessage("assistant", pending_error, pending_error_ts))
        pending_error = None
        pending_error_ts = None

    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("isMeta") or entry.get("isSidechain"):
                    continue

                entry_type = entry.get("type")
                message = entry.get("message", {})
                if not isinstance(message, dict):
                    continue

                if entry_type == "system":
                    err_text = system_error_text(entry)
                    if err_text:
                        pending_error = err_text
                        pending_error_ts = _entry_time(entry)
                    continue

                if entry_type == "user":
                    origin = entry.get("origin")
                    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
                    if origin_kind not in (None, "human"):
                        # task-notification 等系统注入事件也挂在 user 轮次下，但不是真人输入。
                        # 预览只展示 Agent 和真人的对话，这类系统事件价值很低，整条丢弃，不展示。
                        continue
                    text = _extract_text(message.get("content", ""))
                    if text and text != _INTERRUPTED_MARKER:
                        flush_legacy_answer()
                        flush_error()
                        messages.append(ConversationMessage("user", text, _entry_time(entry)))
                    continue

                if entry_type == "attachment":
                    text = queued_command_text(entry)
                    if text:
                        flush_legacy_answer()
                        flush_error()
                        messages.append(ConversationMessage("user", text, _entry_time(entry)))
                    continue

                if entry_type != "assistant":
                    continue
                content = message.get("content", [])
                if not isinstance(content, list):
                    continue
                # part.get("text") 可能是 JSON null（key 存在但值为 null）；裸
                # .get("text", "") 的默认值只在 key 缺失时生效，取到 null 时
                # str(None) 会产出字面量 "None" 混进正文，或在过滤条件里
                # .strip() 直接 AttributeError，统一改用 `or ""` 兜底。
                text_parts = [
                    (part.get("text") or "").strip()
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text" and (part.get("text") or "").strip()
                ]
                if text_parts:
                    text = "\n\n".join(text_parts)
                    if message.get("stop_reason") is None:
                        pending_legacy_answer = text
                        pending_legacy_ts = _entry_time(entry)
                    elif not messages or messages[-1].role != "assistant" or messages[-1].text != text:
                        pending_legacy_answer = None
                        pending_legacy_ts = None
                        pending_error = None  # 真实正文落地=本轮已恢复，丢掉之前攒的报错
                        pending_error_ts = None
                        messages.append(ConversationMessage("assistant", text, _entry_time(entry)))
    except OSError:
        return []
    flush_legacy_answer()
    flush_error()
    return messages


if __name__ == "__main__":
    import sys

    sessions = scan_sessions(limit=20)
    if not sessions:
        print("未找到 Claude 会话记录。", file=sys.stderr)
        sys.exit(1)
    for i, s in enumerate(sessions):
        print(
            f"{i+1:>2}. [{s['short_id']}] {s['cwd_display']:<24} {s['display_time']:<12} "
            f"{s['size_kb']:>7}KB {'运行中' if s['live'] else '已结束':<6} "
            f"native={s['native_title']!r} fallback={s['fallback_title']!r}"
        )
