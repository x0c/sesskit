"""Status tags and title-noise filters shared by runtime parsers."""

from __future__ import annotations

import re

STATUS_ABORTED = "⚠️已中断"
STATUS_PENDING = "⏳待回复"
STATUS_DONE = "✅已完成"
STATUS_NONE = ""

# Marker left by Corral's own title-generation prompts; filter those sessions out.
PROMPT_MARKER = "你将看到一批编程助手会话的摘录"

# List payloads stay bounded; extract Corral handoff digest *before* clipping
# so the 300-char window is task text, not pickup boilerplate.
EXCERPT_LIMIT = 300
_HANDOFF_TASK_RE = re.compile(r"^(?:Task|任务)\s*[:：]\s*(.+)$")
_HANDOFF_INTRO_MARKERS = (
    "You are picking up a session from",
    "你正在接力一个来自",
)
_DIGEST_HEADING_MARKERS = (
    "Below is a conversation excerpt automatically extracted",
    "以下是从原会话自动提取的对话摘录",
)
_ORIGINAL_REQUEST_MARKERS = (
    "[Original request]",
    "【原始需求】",
)
_NOISE_LINE_PREFIXES = (
    "Original session history file:",
    "Original working directory:",
    "History format hint:",
    "原会话历史文件：",
    "原工作目录：",
    "历史格式提示：",
    "Use the excerpt above as a clue",
    "请以上述摘录为线索",
    "Then inspect the actual workspace",
    "随后检查当前工作区",
    "Read the session history above first",
    "请先读取上述会话历史",
    *_DIGEST_HEADING_MARKERS,
)

_DOC_COMMAND_LABELS = {
    "readme": "README",
    "docs": "docs",
    "doc": "doc",
    "doc-init": "Init docs",
    "doc-update": "Update docs",
    "doc-compact": "Compact docs",
}


def is_title_generation_prompt(text: object) -> bool:
    """True when history text comes from an automated title-generation request."""
    return isinstance(text, str) and PROMPT_MARKER in text


def _title_line(text: str | None) -> str | None:
    if not text:
        return None
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line.startswith("› ") or line.startswith("> "):
            line = line[2:].strip()
        if not line:
            continue
        if line.startswith(("http://", "https://")):
            continue
        return re.sub(r"\s+", " ", line)
    return None


def _normalize_title(text: str | None) -> str | None:
    """Light title normalization (aligned with Corral scanner expectations)."""
    line = _title_line(text)
    if not line:
        return None

    for command, label in _DOC_COMMAND_LABELS.items():
        command_match = re.fullmatch(rf"[/\$]{command}\s+@?([\w.-]+?)/?", line, flags=re.IGNORECASE)
        if command_match:
            return f"{command_match.group(1)} {label}"
        if re.fullmatch(rf"[/\$]{command}", line, flags=re.IGNORECASE):
            return label

    line = re.sub(r"^(?:@[\w.-]+/?\s+)+", "", line).strip()
    return line or None


def _cut_at_intro(text: str) -> str:
    cut: int | None = None
    for marker in _HANDOFF_INTRO_MARKERS:
        idx = text.find(marker)
        if idx >= 0 and (cut is None or idx < cut):
            cut = idx
    if cut is None:
        return text.strip()
    return text[:cut].strip()


def _strip_original_request_prefix(line: str) -> str:
    stripped = line.strip()
    for marker in _ORIGINAL_REQUEST_MARKERS:
        if stripped.startswith(marker):
            return stripped[len(marker) :].strip()
    return stripped


def _task_from_line(line: str) -> str | None:
    match = _HANDOFF_TASK_RE.fullmatch(_strip_original_request_prefix(_cut_at_intro(line)))
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _peel_wrapper_lines(text: str) -> str:
    """Drop pickup intro, history-file lines, and flattened leftover wrappers."""
    kept: list[str] = []
    for raw_line in str(text).splitlines():
        line = _cut_at_intro(raw_line).strip()
        if not line:
            continue
        if line.startswith(_NOISE_LINE_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _looks_like_nested_handoff(text: str) -> bool:
    if any(marker in text for marker in _HANDOFF_INTRO_MARKERS):
        return True
    if any(marker in text for marker in _ORIGINAL_REQUEST_MARKERS) and _HANDOFF_TASK_RE.search(
        _strip_original_request_prefix(text.splitlines()[0]) if text else ""
    ):
        return True
    return False


def split_handoff_text(text: str | None) -> tuple[str | None, str]:
    """Return ``(inherited_task, digest)`` from a Corral handoff prompt.

    Digest text excludes the pickup intro and the excerpt heading. Nested
    handoffs (an earlier pickup flattened into ``[Original request]``) peel
    inward so the inner task wins. A payload that was already extracted
    (``Task: …`` plus body, no intro) keeps the inherited task and the body.
    """
    peeled = _peel_wrapper_lines(str(text or ""))
    if not peeled:
        return None, ""

    lines = peeled.splitlines()
    inherited = _task_from_line(lines[0])
    rest = "\n".join(lines[1:]).strip()
    if rest and _looks_like_nested_handoff(rest):
        inner_inherited, inner_digest = split_handoff_text(rest)
        if inner_inherited:
            return inner_inherited, inner_digest
        if inner_digest:
            return inherited, inner_digest
    if inherited:
        return inherited, rest
    return None, peeled


def clip_user_excerpt(text: str | None, limit: int = EXCERPT_LIMIT) -> str:
    """Bound a user/assistant excerpt, keeping handoff task text first."""
    raw = str(text or "")
    if not raw:
        return ""
    inherited, digest = split_handoff_text(raw)
    if digest or inherited:
        if inherited and digest:
            first = digest.splitlines()[0] if digest else ""
            if _task_from_line(first) == inherited:
                raw = digest
            else:
                raw = f"Task: {inherited}\n\n{digest}"
        elif digest:
            raw = digest
        else:
            raw = f"Task: {inherited}"
    return raw[:limit]
