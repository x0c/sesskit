"""Status tags and title-noise filters shared by runtime parsers."""

from __future__ import annotations

import re

STATUS_ABORTED = "⚠️已中断"
STATUS_PENDING = "⏳待回复"
STATUS_DONE = "✅已完成"
STATUS_NONE = ""

# Deprecated host-owned title-generation marker (one downstream host's own
# automated prompts). Kept only so older direct callers keep their behavior;
# new scan paths pass an explicit marker per call (None without a host) and
# never read this constant. Do not add new host markers here.
PROMPT_MARKER = "你将看到一批编程助手会话的摘录"

# Neutral bound for list payloads. Host-specific wrapper peeling (a host's own
# handoff prompt layout) lives in the host's extension and is applied by
# callers before clipping; core clipping only truncates.
EXCERPT_LIMIT = 300

_LEGACY_MARKER = object()

_DOC_COMMAND_LABELS = {
    "readme": "README",
    "docs": "docs",
    "doc": "doc",
    "doc-init": "Init docs",
    "doc-update": "Update docs",
    "doc-compact": "Compact docs",
}


def is_title_generation_prompt(text: object, *, marker: str | None | object = _LEGACY_MARKER) -> bool:
    """True when history text comes from an automated title-generation request.

    ``marker`` is the host's own marker, supplied per scan. With
    ``marker=None`` (no host extension) this is always False so ordinary
    native prompts stay listable. Omitting ``marker`` keeps the deprecated
    legacy behavior for older direct callers only.
    """
    if not isinstance(text, str):
        return False
    if marker is None:
        return False
    if marker is _LEGACY_MARKER:
        return PROMPT_MARKER in text
    return str(marker) in text


def _title_line(text: str | None) -> str | None:
    if not text:
        return None
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if line.startswith(("› ", "> ")):
            line = line[2:].strip()
        if not line:
            continue
        if line.startswith(("http://", "https://")):
            continue
        return re.sub(r"\s+", " ", line)
    return None


def _normalize_title(text: str | None) -> str | None:
    """Light title normalization shared by scanners."""
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


def clip_user_excerpt(text: str | None, limit: int = EXCERPT_LIMIT) -> str:
    """Bound a user/assistant excerpt to ``limit`` characters (host-neutral).

    Hosts that wrap prompts in their own handoff layout apply their extension
    transform before this call; core clipping only truncates.
    """
    raw = str(text or "")
    if not raw:
        return ""
    return raw[:limit]
