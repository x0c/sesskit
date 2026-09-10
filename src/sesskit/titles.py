"""Status tags and title-noise filters shared by runtime parsers."""

from __future__ import annotations

import re

STATUS_ABORTED = "⚠️已中断"
STATUS_PENDING = "⏳待回复"
STATUS_DONE = "✅已完成"
STATUS_NONE = ""

# Marker left by Corral's own title-generation prompts; filter those sessions out.
PROMPT_MARKER = "你将看到一批编程助手会话的摘录"

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
