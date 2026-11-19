"""Shared skill-invocation normalization, path matching, and snapshot API."""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sesskit.models import (
    Evidence,
    LoadState,
    SkillInvocation,
)
from sesskit.parsers.common import classify_tool

SKILLS_SCHEMA_ID = "sesskit.skills/v1"

# Canonical skill-root directories across supported runtimes (including
# Codex's hidden `.system/` built-in skills and plugin `skills/` layouts).
_KNOWN_ROOT_SKILL_RE = re.compile(
    r"(?:^|/)(?:"
    r"\.codex|\.claude|\.pi(?:/agent)?|\.agents|\.cursor|"
    r"\.config/agentsync|\.config/opencode|\.opencode|"
    r"\.gemini/config|\.gemini/antigravity-cli|\.gemini|\.kimi-code|"
    r"plugins/[^/\s\"'<>]+"
    r")/skills/(?:\.system/)?([^/\s\"'<>]+)/SKILL\.md(?:\Z|[\s\"'<>])",
    re.IGNORECASE,
)
_GENERIC_SKILL_PATH_RE = re.compile(
    r"(?:^|/)skills/(?:\.system/)?([^/\s\"'<>]+)/SKILL\.md(?:\Z|[\s\"'<>])",
    re.IGNORECASE,
)
_SKILL_MD_TOKEN_RE = re.compile(r"([^\s\"'<>`|;&()]+/SKILL\.md)", re.IGNORECASE)

_SHELL_SPLIT_RE = re.compile(r"(?:\s*(?:&&|\|\||;|\|)\s*)")
_MUTATING_SHELL_RE = re.compile(
    r"(?:>>?|<<|\b(?:tee|rm|mv|cp|chmod|chown|touch|truncate|ln|mkdir|rmdir|"
    r"rg|grep|egrep|fgrep|ag|fd|find|ls|tree|wc|stat|file|diff|git|"
    r"python|python3|node|ruby|perl|awk)\b|sed\s+-[^a-zA-Z]*i)",
    re.IGNORECASE,
)
_READ_COMMANDS = frozenset({"cat", "head", "tail", "less", "more", "bat", "nl"})

_READ_PATH_KEYS = (
    "path",
    "file_path",
    "filePath",
    "Path",
    "TargetFile",
    "target_file",
    "absolute_path",
    "filename",
)
_SHELL_CMD_KEYS = ("cmd", "command", "script")


def raw_skill_name(name: str) -> str:
    """Trim whitespace and leading invocation sigils while keeping namespace."""
    cleaned = str(name or "").strip()
    while cleaned and cleaned[0] in "/$@":
        cleaned = cleaned[1:].strip()
    return cleaned


def normalize_skill_name(name: str) -> str:
    """Return bare skill name (last colon segment) for fallback matching."""
    cleaned = raw_skill_name(name)
    if ":" in cleaned:
        return cleaned.rsplit(":", 1)[-1].strip()
    return cleaned


def normalize_skill_path(path: str) -> str:
    """Normalize a file path or file:// URI into clean POSIX form."""
    text = str(path or "").strip()
    text = text.removeprefix("file://")
    text = text.replace("\\", "/")
    if text.startswith("~/"):
        return "~/" + posixpath.normpath(text[2:])
    if text.startswith("/"):
        return posixpath.normpath(text)
    return posixpath.normpath(text)


def _is_valid_skill_segment(name: str) -> bool:
    if not name or name in {".", "..", ".system", "SKILL.md"}:
        return False
    if any(ch in name for ch in ("<", ">", "{", "}", "*", "?", "$", "`")):
        return False
    return len(name) <= 128


def skill_from_path(
    path: object,
    *,
    catalog: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve ``(skill_name, normalized_path)`` from a SKILL.md path."""
    if not isinstance(path, str):
        return None
    raw = path.strip()
    if not raw or "SKILL.md" not in raw:
        return None
    norm = normalize_skill_path(raw)
    if catalog:
        for candidate in (raw, norm, os.path.expanduser(raw) if raw.startswith("~/") else raw):
            if candidate in catalog:
                return catalog[candidate], norm
        norm_expanded = normalize_skill_path(os.path.expanduser(raw)) if raw.startswith("~/") else norm
        if norm_expanded in catalog:
            return catalog[norm_expanded], norm
    match = _KNOWN_ROOT_SKILL_RE.search(norm) or _GENERIC_SKILL_PATH_RE.search(norm)
    if match is None:
        return None
    name = raw_skill_name(match.group(1))
    if not _is_valid_skill_segment(name):
        return None
    return name, norm


def _is_read_only_segment(segment: str) -> bool:
    stripped = segment.strip()
    if not stripped or "SKILL.md" not in stripped:
        return False
    if _MUTATING_SHELL_RE.search(stripped):
        return False
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    if not tokens:
        return False
    # Skip leading env assignments like `PAGER=cat`.
    index = 0
    while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith("-"):
        index += 1
    if index >= len(tokens):
        return False
    cmd = os.path.basename(tokens[index]).lower()
    if cmd in _READ_COMMANDS:
        return True
    if cmd == "sed":
        return any(tok == "-n" or tok.startswith("-n") for tok in tokens[index + 1 :])
    return False


def read_only_skill_path_from_shell(
    command: object,
    *,
    catalog: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Extract a SKILL.md read target from a strictly read-only shell command."""
    if not isinstance(command, str):
        return None
    text = command.strip()
    if not text or "SKILL.md" not in text:
        return None
    for segment in _SHELL_SPLIT_RE.split(text):
        if not _is_read_only_segment(segment):
            continue
        for match in _SKILL_MD_TOKEN_RE.finditer(segment):
            resolved = skill_from_path(match.group(1), catalog=catalog)
            if resolved is not None:
                return resolved
    return None


def _string_field(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = mapping.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def skill_from_tool_call(
    tool_name: str,
    raw_input: object,
    *,
    record: str | None = None,
    call_id: str | None = None,
    catalog: dict[str, str] | None = None,
    fallback_name: str | None = None,
    fallback_path: str | None = None,
) -> tuple[SkillInvocation, ...]:
    """Normalize passive skill invocations from a tool_call event.

    Returns ``()`` for authoring (edit/write), search (grep/find/ls), and
    mutating shell commands.
    """
    kind = classify_tool(tool_name)
    cid = str(call_id or "").strip() or None
    if kind == "skill":
        name: str | None = None
        args: str | None = None
        path: str | None = fallback_path
        if isinstance(raw_input, dict):
            raw_name = _string_field(raw_input, ("skill", "name", "id", "command"))
            if raw_name:
                name = raw_skill_name(raw_name)
            args = _string_field(raw_input, ("args", "arguments"))
            path = _string_field(raw_input, ("path", "dir", "directory", "location")) or path
        elif isinstance(raw_input, str) and raw_input.strip():
            name = raw_skill_name(raw_input)
        if not name and fallback_name:
            name = raw_skill_name(fallback_name)
        if not name:
            return ()
        return (
            SkillInvocation(
                name=name,
                trigger="auto",
                detection="skill_tool",
                evidence=Evidence("native", field="tool_call.input", record=record),
                arguments=args,
                path=path,
                call_id=cid,
            ),
        )

    if kind == "read":
        candidate: str | None = None
        if isinstance(raw_input, dict):
            candidate = _string_field(raw_input, _READ_PATH_KEYS)
        elif isinstance(raw_input, str):
            candidate = raw_input.strip()
        resolved = skill_from_path(candidate, catalog=catalog)
        if resolved is None:
            return ()
        name, norm_path = resolved
        return (
            SkillInvocation(
                name=name,
                trigger="auto",
                detection="file_read",
                evidence=Evidence("inferred", field="tool_call.input", record=record),
                path=norm_path,
                call_id=cid,
            ),
        )

    if kind == "shell":
        cmd: str | None = None
        if isinstance(raw_input, dict):
            cmd = _string_field(raw_input, _SHELL_CMD_KEYS)
        elif isinstance(raw_input, str):
            cmd = raw_input.strip()
        resolved = read_only_skill_path_from_shell(cmd, catalog=catalog)
        if resolved is None:
            return ()
        name, norm_path = resolved
        return (
            SkillInvocation(
                name=name,
                trigger="auto",
                detection="file_read",
                evidence=Evidence("inferred", field="tool_call.input", record=record),
                path=norm_path,
                call_id=cid,
            ),
        )

    return ()


@dataclass(frozen=True)
class SkillSnapshot:
    """Normalized skill-invocation dataset for a single session."""

    state: LoadState
    runtime: str
    session_id: str | None
    invocations: tuple[SkillInvocation, ...] = ()
    schema: str = SKILLS_SCHEMA_ID


def summarize_skills(
    invocations: list[SkillInvocation] | tuple[SkillInvocation, ...],
    *,
    session_key_fn: Any = None,
) -> list[dict[str, Any]]:
    """Aggregate skill invocations by canonical name across events or sessions."""
    groups: dict[str, dict[str, Any]] = {}
    sessions_by_skill: dict[str, set[str]] = defaultdict(set)
    runtimes_by_skill: dict[str, set[str]] = defaultdict(set)
    for index, item in enumerate(invocations):
        inv = item[1] if isinstance(item, tuple) else item
        sess_ref = item[0] if isinstance(item, tuple) else None
        key = inv.name
        entry = groups.get(key)
        if entry is None:
            entry = {
                "name": inv.name,
                "bare_name": inv.bare_name,
                "total": 0,
                "explicit_count": 0,
                "auto_count": 0,
                "unknown_count": 0,
                "detections": defaultdict(int),
                "last_ts": None,
            }
            groups[key] = entry
        entry["total"] += 1
        if inv.trigger == "explicit":
            entry["explicit_count"] += 1
        elif inv.trigger == "auto":
            entry["auto_count"] += 1
        else:
            entry["unknown_count"] += 1
        entry["detections"][inv.detection] += 1
        if inv.ts is not None and (entry["last_ts"] is None or inv.ts > entry["last_ts"]):
            entry["last_ts"] = inv.ts
        if sess_ref is not None:
            if callable(session_key_fn):
                s_id, rt = session_key_fn(sess_ref)
                if s_id:
                    sessions_by_skill[key].add(s_id)
                if rt:
                    runtimes_by_skill[key].add(rt)
            elif isinstance(sess_ref, str):
                sessions_by_skill[key].add(sess_ref)
    out: list[dict[str, Any]] = []
    for key, entry in sorted(
        groups.items(),
        key=lambda pair: (-pair[1]["total"], pair[0]),
    ):
        row = {
            "name": entry["name"],
            "bare_name": entry["bare_name"],
            "total": entry["total"],
            "explicit_count": entry["explicit_count"],
            "auto_count": entry["auto_count"],
            "unknown_count": entry["unknown_count"],
            "detections": dict(sorted(entry["detections"].items())),
            "last_ts": entry["last_ts"],
        }
        if sessions_by_skill[key]:
            row["session_count"] = len(sessions_by_skill[key])
        if runtimes_by_skill[key]:
            row["runtimes"] = sorted(runtimes_by_skill[key])
        out.append(row)
    return out


def skill_invocation_to_dict(inv: SkillInvocation) -> dict[str, Any]:
    """Serialize one ``SkillInvocation`` to JSON-safe dict matching skills.v1.json."""
    out: dict[str, Any] = {
        "name": inv.name,
        "bare_name": inv.bare_name,
        "trigger": inv.trigger,
        "detection": inv.detection,
        "evidence": {
            "origin": inv.evidence.origin,
            "field": inv.evidence.field,
            "record": inv.evidence.record,
        },
        "arguments": inv.arguments,
        "path": inv.path,
        "call_id": inv.call_id,
        "seq": inv.seq,
        "ts": inv.ts,
        "turn_id": inv.turn_id,
    }
    return out


def skill_snapshot_to_dict(snapshot: SkillSnapshot) -> dict[str, Any]:
    """Serialize a ``SkillSnapshot`` for JSON/CLI output."""
    return {
        "schema": snapshot.schema,
        "state": snapshot.state,
        "runtime": snapshot.runtime,
        "session_id": snapshot.session_id,
        "invocations": [skill_invocation_to_dict(inv) for inv in snapshot.invocations],
        "summary": summarize_skills(snapshot.invocations),
    }


def load_skills(session: dict) -> SkillSnapshot:
    """Load deduplicated, turn-correlated skill invocations for one session dict."""
    from sesskit.adapters import get_adapter

    runtime_id = str(session.get("source") or "")
    session_id = str(session.get("id") or "") or None
    try:
        adapter = get_adapter(runtime_id)
    except KeyError:
        return SkillSnapshot(
            state="unsupported",
            runtime=runtime_id or "unknown",
            session_id=session_id,
        )
    return adapter.load_skills(session)
