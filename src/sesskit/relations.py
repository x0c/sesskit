"""Native session relations: subagent/child sessions, forks, resume chains.

Each helper reports only what persisted history states, with evidence.
A link without stated semantics keeps kind ``unknown``; a delegation
without a persisted child id keeps ``target`` None. Unreadable history
yields no relations, never an error.
"""

from __future__ import annotations

import json
import os
import sqlite3

from sesskit.models import Evidence, SessionRelation

_NATIVE = "native"
_UNKNOWN = "unknown"


def session_relations(session: dict) -> list[SessionRelation]:
    """Return native relations for one scanned session dict."""
    runtime_id = str(session.get("source") or "")
    if runtime_id == "opencode":
        return _opencode_relations(session)
    if runtime_id == "pi":
        return _pi_relations(session)
    if runtime_id == "claude":
        return _claude_relations(session)
    if runtime_id == "codex":
        return _codex_relations(session)
    if runtime_id == "cursor":
        return _cursor_relations(session)
    return []


def _opencode_relations(session: dict) -> list[SessionRelation]:
    from sesskit.parsers import opencode as parser

    path = str(session.get("path") or "")
    session_id = str(session.get("id") or "")
    if not path or not session_id or not os.path.isfile(path):
        return []
    connection = parser.connect_ro(path)
    if connection is None:
        return []
    try:
        relations: list[SessionRelation] = []
        for table in ("session_v2", "session"):
            try:
                row = connection.execute(
                    f"SELECT parent_id FROM {table} WHERE id = ?",
                    (session_id,),
                ).fetchone()
            except sqlite3.Error:
                continue
            if row is None:
                continue
            parent_id = row["parent_id"]
            if isinstance(parent_id, str) and parent_id.strip():
                relations.append(SessionRelation(
                    "unknown",
                    Evidence(_NATIVE, field=f"{table}.parent_id",
                             record=f"session:{session_id}"),
                    target=parent_id.strip(),
                ))
            break
        try:
            row = connection.execute(
                "SELECT fork_session_id FROM session_v2 WHERE id = ?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            fork_id = row["fork_session_id"]
            if isinstance(fork_id, str) and fork_id.strip():
                relations.append(SessionRelation(
                    "fork",
                    Evidence(_NATIVE, field="session_v2.fork_session_id",
                             record=f"session:{session_id}"),
                    target=fork_id.strip(),
                ))
        try:
            part_rows = connection.execute(
                "SELECT id, data FROM part WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            part_rows = []
        for part_row in part_rows:
            part_id = part_row["id"]
            raw = part_row["data"]
            try:
                part = json.loads(raw) if isinstance(raw, str) else None
            except ValueError:
                part = None
            if isinstance(part, dict) and part.get("type") == "subtask":
                relations.append(SessionRelation(
                    "subagent",
                    Evidence(_NATIVE, field="part.data.type",
                             record=f"part:{part_id}"),
                ))
                break
        return relations
    finally:
        connection.close()


def _pi_relations(session: dict) -> list[SessionRelation]:
    from sesskit.parsers import pi as parser

    path = str(session.get("path") or "")
    if not path or not os.path.isfile(path):
        return []
    try:
        entries = parser.read_entries(path)
    except (OSError, ValueError):
        return []
    relations: list[SessionRelation] = []
    for item in entries:
        if not isinstance(item, dict) or item.get("customType") != "subagents:record":
            continue
        data = item.get("data")
        target = data.get("id") if isinstance(data, dict) else None
        record = f"entry:{item.get('id')}" if isinstance(item.get("id"), str) else "entry:?"
        relations.append(SessionRelation(
            "subagent",
            Evidence(_NATIVE, field="customType", record=record),
            target=str(target) if isinstance(target, str) and target.strip() else None,
        ))
    return relations


def _claude_relations(session: dict) -> list[SessionRelation]:
    path = str(session.get("path") or "")
    if not path or not os.path.isfile(path):
        return []
    relations: list[SessionRelation] = []
    continued_target: str | None = None
    subagent_seen = False
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                if '"continued-in"' in line:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and entry.get("type") == "continued-in":
                        target = entry.get("continuedInSessionId")
                        if isinstance(target, str) and target.strip():
                            continued_target = target.strip()
                    continue
                if subagent_seen:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if (isinstance(part, dict) and part.get("type") == "tool_use"
                            and str(part.get("name") or "") == "Agent"):
                        relations.append(SessionRelation(
                            "subagent",
                            Evidence(_NATIVE, field="message.content",
                                     record=f"line:{lineno}"),
                        ))
                        subagent_seen = True
                        break
    except OSError:
        return []
    if continued_target is not None:
        relations.insert(0, SessionRelation(
            "continuation",
            Evidence(_NATIVE, field="continuedInSessionId",
                     record=f"continued-in:{path}"),
            target=continued_target,
        ))
    return relations


def _codex_relations(session: dict) -> list[SessionRelation]:
    path = str(session.get("path") or "")
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                if str(payload.get("thread_source") or "") == "subagent":
                    return [SessionRelation(
                        "subagent",
                        Evidence(_NATIVE, field="payload.thread_source",
                                 record=f"line:{lineno}"),
                    )]
    except OSError:
        return []
    return []


def _cursor_relations(session: dict) -> list[SessionRelation]:
    from sesskit.parsers import cursor as parser

    path = str(session.get("path") or "")
    chat_dir = path if os.path.isdir(path) else os.path.dirname(path)
    if not chat_dir or not os.path.isdir(chat_dir):
        return []
    try:
        parent_id = parser._parent_id_from_store(chat_dir)
    except (OSError, ValueError):
        return []
    if isinstance(parent_id, str) and parent_id.strip():
        return [SessionRelation(
            "subagent",
            Evidence(_NATIVE, field="subagentInfo", record=f"store:{chat_dir}"),
            target=parent_id.strip(),
        )]
    return []


def claude_continuation_target(path: str) -> str | None:
    """Last `continued-in` target recorded in a Claude JSONL file.

    The pointer may sit outside the scanner's head/tail windows, so this
    sweeps the whole file but only JSON-parses lines containing the
    `continued-in` marker. Unreadable files and absent/blank pointers
    yield None, never an error.
    """
    if not path or not os.path.isfile(path):
        return None
    target: str | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"continued-in"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "continued-in":
                    continue
                candidate = entry.get("continuedInSessionId")
                if isinstance(candidate, str) and candidate.strip():
                    target = candidate.strip()
    except OSError:
        return None
    return target


_MAX_CONTINUATION_HOPS = 32


def resolve_continuation(session: dict) -> str:
    """Follow a Claude `continued-in` chain to the latest readable session id.

    Follows A→B→C through per-file forward pointers in the same project
    directory. Cycles, self-pointers, blank targets, and missing target
    files stop the walk at the last readable id; unknown runtimes and
    sessions without a history path return their own id unchanged.
    """
    session_id = str(session.get("id") or "")
    if str(session.get("source") or "") != "claude" or not session_id:
        return session_id
    path = str(session.get("path") or "")
    if not path or not os.path.isfile(path):
        return session_id
    project_dir = os.path.dirname(path)
    current_id = session_id
    current_path = path
    seen = {current_id}
    for _ in range(_MAX_CONTINUATION_HOPS):
        target = claude_continuation_target(current_path)
        if not target or target in seen:
            break
        next_path = os.path.join(project_dir, target + ".jsonl")
        if not os.path.isfile(next_path):
            break
        seen.add(target)
        current_id, current_path = target, next_path
    return current_id
