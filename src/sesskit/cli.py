#!/usr/bin/env python3
"""SessKit CLI: language-agnostic JSON interface for local agent sessions.

Commands are read-only. Output is always a JSON envelope:
{"ok": true|false, "data": ..., "error": ..., "meta": {"version": 1}}

Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 5 ambiguous.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from sesskit import titles
from sesskit.envelope import (
    EXIT_AMBIGUOUS,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    ApiError,
    err,
    ok,
    print_envelope,
)
from sesskit.hosted import cache_dir
from sesskit.models import format_message_time, session_key
from sesskit.paths import assert_not_history_path, atomic_write_json
from sesskit.registry import ConversationLoadError, ParserRegistry, default_registry
from sesskit.transcript import SCHEMA_ID, count_events, load_events

STATUS_LABELS = {
    titles.STATUS_DONE: "done",
    titles.STATUS_PENDING: "pending",
    titles.STATUS_ABORTED: "aborted",
    titles.STATUS_NONE: "unknown",
}

_RESOLVE_SCAN_LIMIT = 200
DEFAULT_LIST_FIELDS = (
    "id",
    "short_id",
    "runtime",
    "title",
    "status",
    "live",
    "mtime",
    "cwd_display",
    "last_user",
    "last_agent",
)
DEFAULT_SEARCH_FIELDS = DEFAULT_LIST_FIELDS + ("matched_via", "matched_fields", "score")
DEFAULT_SHOW_FIELDS = DEFAULT_LIST_FIELDS + ("messages", "message_count_shown", "message_count_total")
_SUMMARY_TRIM_LEN = 120
_REL_TIME_UNITS = {"d": 86400, "h": 3600, "m": 60}


def _trim(text: str | None, limit: int = _SUMMARY_TRIM_LEN) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _resolve_title(session: dict) -> str:
    native = (session.get("native_title") or "").strip()
    if native:
        return native
    fallback = (session.get("fallback_title") or "").strip()
    if fallback:
        return fallback
    first = (session.get("first_user_msg") or "").strip()
    return first[:60] if first else session.get("short_id") or session.get("id") or ""


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print_envelope(
            {
                "ok": False,
                "data": None,
                "error": {
                    "code": "usage_error",
                    "message": message,
                    "hint": "run sesskit describe or sesskit describe <command>",
                    "next_commands": ["sesskit describe"],
                },
                "meta": {"version": 1},
            }
        )
        raise SystemExit(EXIT_USAGE)


def _apply_fields(payload: dict, fields: list[str] | None) -> dict:
    if not fields:
        return payload
    return {k: v for k, v in payload.items() if k in fields}


def session_payload(session: dict, fields: list[str] | None = None) -> dict:
    title = _resolve_title(session)
    status_tag = session.get("status_tag") or ""
    payload = {
        "runtime": session.get("source"),
        "id": session.get("id"),
        "short_id": session.get("short_id"),
        "title": title,
        "cwd": session.get("cwd") or "",
        "cwd_display": session.get("cwd_display") or "",
        "time": session.get("display_time") or "",
        "mtime": session.get("mtime"),
        "size_kb": round(session.get("size_kb") or 0, 1),
        "status": STATUS_LABELS.get(status_tag, "unknown"),
        "status_tag": status_tag,
        "history_path": session.get("path") or "",
        "live": bool(session.get("live")),
        "pid": session.get("pid"),
        "last_user": _trim(session.get("last_user_msg")),
        "last_agent": _trim(session.get("last_agent_msg")),
    }
    return _apply_fields(payload, fields)


def _match_sessions(sessions: list[dict], ident: str) -> list[dict]:
    return [
        s
        for s in sessions
        if s.get("id") == ident
        or str(s.get("id") or "").startswith(ident)
        or s.get("short_id") == ident
    ]


def _parse_fields(raw: str | None, default: tuple[str, ...] | None = None) -> list[str] | None:
    if raw:
        return [f.strip() for f in raw.split(",") if f.strip()]
    if default:
        return list(default)
    return None


def _apply_top(items: list, top: int | None) -> list:
    if top is None:
        return items
    return items[: max(0, top)]


def _include_missing_cwd(args) -> bool:
    return bool(getattr(args, "include_missing_cwd", False))


def _scan_map(registry: ParserRegistry, args, limit: int | None = None) -> dict:
    depth = args.limit if limit is None else limit
    include_missing = _include_missing_cwd(args)
    if getattr(args, "runtime", None):
        runtime = registry.get(args.runtime)
        return {args.runtime: runtime.scan_sessions(depth, include_missing_cwd=include_missing)}
    return registry.scan_all(depth, include_missing_cwd=include_missing)


def _load_messages(runtime, session: dict):
    try:
        return runtime.load_conversation(session)
    except ConversationLoadError as exc:
        raise ApiError(
            "not_found",
            str(exc),
            EXIT_NOT_FOUND,
            hint="the session may have been deleted; refresh with sesskit list",
            next_commands=["sesskit list"],
        ) from exc


def _write_envelope_file(envelope: dict, out_path: str, *, compact: bool, protected: list[str]) -> str:
    output_path = os.path.abspath(out_path)
    try:
        assert_not_history_path(output_path, protected)
    except ValueError as exc:
        raise ApiError("usage_error", str(exc), EXIT_USAGE) from exc
    try:
        return atomic_write_json(output_path, envelope, compact=compact)
    except FileNotFoundError as exc:
        raise ApiError("usage_error", str(exc), EXIT_USAGE) from exc
    except IsADirectoryError as exc:
        raise ApiError("usage_error", str(exc), EXIT_USAGE) from exc


def resolve_ref(registry: ParserRegistry, ref: str, limit: int, *, include_missing_cwd: bool = False) -> dict:
    if not ref:
        raise ApiError("usage_error", "missing session reference", EXIT_USAGE)

    if ":" in ref:
        runtime_id, _, ident = ref.partition(":")
        try:
            runtime = registry.get(runtime_id)
        except KeyError as exc:
            raise ApiError("not_found", f"unregistered runtime: {runtime_id}", EXIT_NOT_FOUND) from exc
        matches = _match_sessions(runtime.scan_sessions(limit, include_missing_cwd=include_missing_cwd), ident)
    else:
        scanned = registry.scan_all(limit, include_missing_cwd=include_missing_cwd)
        matches = []
        for runtime in registry:
            matches.extend(_match_sessions(scanned[runtime.id], ref))

    if not matches:
        raise ApiError(
            "not_found",
            f"no session matches: {ref}",
            EXIT_NOT_FOUND,
            hint="check the id/prefix; try sesskit search or sesskit list",
            next_commands=[f"sesskit search {ref}", "sesskit list"],
        )

    exact = [s for s in matches if s.get("id") == ref or session_key(s) == ref]
    if len(exact) == 1:
        return exact[0]
    if len(matches) > 1:
        candidates = [session_key(s) for s in matches[:10]]
        raise ApiError(
            "ambiguous",
            f"ambiguous session reference: {ref}",
            EXIT_AMBIGUOUS,
            hint="use a longer prefix or full runtime:id",
            next_commands=[f"sesskit show {c}" for c in candidates],
        )
    return matches[0]


def _score_quick_match(session: dict, title: str, keywords: list[str]) -> tuple[int, list[str]]:
    sources = [
        ("title", title, 100),
        ("fallback_title", session.get("fallback_title"), 80),
        ("first_user_msg", session.get("first_user_msg"), 60),
        ("last_user_msg", session.get("last_user_msg"), 60),
        ("last_agent_msg", session.get("last_agent_msg"), 35),
        ("cwd", session.get("cwd"), 20),
        ("cwd_display", session.get("cwd_display"), 20),
    ]
    score = 0
    matched: list[str] = []
    for name, value, weight in sources:
        text = str(value or "").lower()
        if not text:
            continue
        hits = sum(1 for kw in keywords if kw in text)
        if hits:
            score += weight * hits
            matched.append(name)
    return score, matched


def _find_snippet(messages, keywords: list[str]) -> str | None:
    for message in messages:
        low = message.text.lower()
        for kw in keywords:
            idx = low.find(kw)
            if idx != -1:
                start = max(0, idx - 40)
                end = min(len(message.text), idx + len(kw) + 80)
                return message.text[start:end].strip()
    return None


def cmd_list(args, registry: ParserRegistry) -> dict:
    compact = getattr(args, "compact", False)
    top = getattr(args, "top", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_LIST_FIELDS if compact else None)
    runtimes = [registry.get(args.runtime)] if args.runtime else list(registry)
    scanned = _scan_map(registry, args)

    candidates = []
    for runtime in runtimes:
        for session in scanned[runtime.id]:
            if args.status and STATUS_LABELS.get(session.get("status_tag") or "", "unknown") != args.status:
                continue
            if args.cwd and args.cwd.lower() not in str(session.get("cwd") or "").lower():
                continue
            if getattr(args, "live", None) is True and not session.get("live"):
                continue
            candidates.append(session)

    candidates.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
    candidates = _apply_top(candidates, top)
    data = {
        "count": len(candidates),
        "scan_limit": args.limit,
        "include_missing_cwd": _include_missing_cwd(args),
        "top": top,
        "sessions": [session_payload(s, fields) for s in candidates],
    }
    if registry.last_scan_errors:
        data["scan_errors"] = registry.last_scan_errors
    return ok(data)


def cmd_search(args, registry: ParserRegistry) -> dict:
    keywords = [k.lower() for k in args.keywords]
    compact = getattr(args, "compact", False)
    top = getattr(args, "top", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_SEARCH_FIELDS if compact else None)
    runtimes = [registry.get(args.runtime)] if args.runtime else list(registry)
    scanned = _scan_map(registry, args)

    results = []
    for runtime in runtimes:
        for session in scanned[runtime.id]:
            if getattr(args, "live", None) is True and not session.get("live"):
                continue
            title = _resolve_title(session)
            quick_parts = [
                title,
                session.get("fallback_title"),
                session.get("first_user_msg"),
                session.get("last_user_msg"),
                session.get("last_agent_msg"),
                session.get("cwd"),
                session.get("cwd_display"),
            ]
            haystack = " ".join(filter(None, quick_parts)).lower()
            if all(kw in haystack for kw in keywords):
                score, matched_fields = _score_quick_match(session, title, keywords)
                results.append((score, "quick", matched_fields, runtime, session, None))
            elif args.deep:
                try:
                    messages = _load_messages(runtime, session)
                except ApiError:
                    continue
                full_text = "\n".join(m.text for m in messages).lower()
                if all(kw in full_text for kw in keywords):
                    score, matched_fields = _score_quick_match(session, title, keywords)
                    matched_fields = matched_fields + ["conversation"]
                    score += 10
                    snippet = _find_snippet(messages, keywords)
                    results.append((score, "deep", matched_fields, runtime, session, snippet))

    results.sort(key=lambda item: (item[0], item[4].get("mtime") or 0), reverse=True)
    results = _apply_top(results, top)
    sessions = []
    for score, matched_via, matched_fields, _runtime, session, snippet in results:
        payload = session_payload(session)
        payload["score"] = score
        payload["matched_via"] = matched_via
        payload["matched_fields"] = matched_fields
        if snippet:
            payload["snippet"] = snippet
        sessions.append(_apply_fields(payload, fields))

    data = {
        "query": args.keywords,
        "deep": args.deep,
        "count": len(sessions),
        "scan_limit": args.limit,
        "include_missing_cwd": _include_missing_cwd(args),
        "top": top,
        "sessions": sessions,
    }
    if registry.last_scan_errors:
        data["scan_errors"] = registry.last_scan_errors
    return ok(data)


def cmd_show(args, registry: ParserRegistry) -> dict:
    session = resolve_ref(
        registry,
        args.session,
        args.limit,
        include_missing_cwd=_include_missing_cwd(args),
    )
    compact = getattr(args, "compact", False)
    out = getattr(args, "out", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_SHOW_FIELDS if compact else None)
    runtime = registry.get(str(session.get("source") or ""))
    payload = session_payload(session)
    messages = _load_messages(runtime, session)
    total_messages = len(messages)
    if not args.full:
        n = args.messages if args.messages else 20
        messages = messages[-n:]

    payload["messages"] = [
        {
            "role": m.role,
            "text": m.text,
            "time": format_message_time(m.timestamp) if m.timestamp else None,
            "mtime": m.timestamp,
        }
        for m in messages
    ]
    payload["message_count_shown"] = len(payload["messages"])
    payload["message_count_total"] = total_messages

    if out:
        envelope = ok(payload)
        output_path = _write_envelope_file(
            envelope,
            out,
            compact=compact,
            protected=[str(session.get("path") or "")],
        )
        summary = session_payload(session, DEFAULT_LIST_FIELDS if compact else None)
        summary.update(
            {
                "output_path": output_path,
                "output_bytes": os.path.getsize(output_path),
                "message_count_written": len(payload["messages"]),
                "message_count_total": total_messages,
                "messages_omitted": True,
            }
        )
        return ok(summary)

    return ok(_apply_fields(payload, fields))


def _parse_time_bound(raw: str, *, is_until: bool) -> float:
    raw = raw.strip()
    if len(raw) >= 2 and raw[-1] in _REL_TIME_UNITS and raw[:-1].isdigit():
        return time.time() - int(raw[:-1]) * _REL_TIME_UNITS[raw[-1]]
    if raw.isdigit() and len(raw) >= 6:
        return float(raw)
    for fmt, date_only in (("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if date_only and is_until:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.timestamp()
    raise ApiError(
        "usage_error",
        f"cannot parse time: {raw} (use YYYY-MM-DD, 'YYYY-MM-DD HH:MM', 7d/24h/30m, or unix timestamp)",
        EXIT_USAGE,
    )


def cmd_export(args, registry: ParserRegistry) -> dict:
    since = _parse_time_bound(args.since, is_until=False) if args.since else None
    until = _parse_time_bound(args.until, is_until=True) if args.until else None
    if since is not None and until is not None and since > until:
        raise ApiError("usage_error", "since is later than until", EXIT_USAGE)

    compact = getattr(args, "compact", False)
    runtimes = [registry.get(args.runtime)] if args.runtime else list(registry)
    scanned = _scan_map(registry, args)

    candidates = []
    for runtime in runtimes:
        for session in scanned[runtime.id]:
            mtime = session.get("mtime")
            if since is not None and (mtime is None or mtime < since):
                continue
            if until is not None and (mtime is None or mtime > until):
                continue
            if args.status and STATUS_LABELS.get(session.get("status_tag") or "", "unknown") != args.status:
                continue
            if args.cwd and args.cwd.lower() not in str(session.get("cwd") or "").lower():
                continue
            candidates.append((runtime, session))

    candidates.sort(key=lambda item: item[1].get("mtime") or 0)
    sessions = []
    load_errors = []
    for runtime, session in candidates:
        payload = session_payload(session)
        try:
            messages = _load_messages(runtime, session)
        except ApiError as exc:
            load_errors.append({"id": session.get("id"), "runtime": runtime.id, "error": exc.message})
            continue
        payload["messages"] = [
            {
                "role": m.role,
                "text": m.text,
                "time": format_message_time(m.timestamp) if m.timestamp else None,
                "mtime": m.timestamp,
            }
            for m in messages
        ]
        payload["message_count_total"] = len(messages)
        sessions.append(payload)

    data = {
        "range": {
            "since": since,
            "until": until,
            "since_display": format_message_time(since) if since else None,
            "until_display": format_message_time(until) if until else None,
        },
        "count": len(sessions),
        "scan_limit": args.limit,
        "include_missing_cwd": _include_missing_cwd(args),
        "sessions": sessions,
    }
    if load_errors:
        data["load_errors"] = load_errors
    if registry.last_scan_errors:
        data["scan_errors"] = registry.last_scan_errors

    out = getattr(args, "out", None)
    if not out:
        return ok(data)

    envelope = ok(data)
    protected = [str(session.get("path") or "") for _, session in candidates]
    output_path = _write_envelope_file(envelope, out, compact=compact, protected=protected)
    return ok(
        {
            "output_path": output_path,
            "output_bytes": os.path.getsize(output_path),
            "session_count": len(sessions),
            "message_count_total": sum(s["message_count_total"] for s in sessions),
            "range": data["range"],
            "sessions_omitted": True,
            "load_error_count": len(load_errors),
        }
    )


def build_share_payload(session: dict, registry: ParserRegistry) -> dict:
    runtime = registry.get(str(session.get("source") or ""))
    # Surface missing history as an error instead of a successful empty transcript.
    _load_messages(runtime, session)
    events = load_events(session)
    payload = session_payload(session)
    payload["schema"] = SCHEMA_ID
    payload["runtime_name"] = runtime.display_name
    payload["events"] = events
    payload["event_count"] = len(events)
    payload["counts"] = count_events(events)
    return payload


def write_share_envelope(payload: dict, out_path: str, *, compact: bool = False, protected: list[str] | None = None) -> str:
    return _write_envelope_file(
        ok(payload),
        out_path,
        compact=compact,
        protected=list(protected or []) + [str(payload.get("history_path") or "")],
    )


def share_cache_path(session: dict) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = session_key(session)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw).strip("._") or "session"
    directory = cache_dir() / "share"
    os.makedirs(directory, exist_ok=True)
    path = directory / f"{safe}-{stamp}.json"
    if path.exists():
        path = directory / f"{safe}-{stamp}-{os.getpid()}.json"
    return str(path)


def export_share_to_cache(session: dict, registry: ParserRegistry, *, compact: bool = True) -> str:
    return write_share_envelope(
        build_share_payload(session, registry),
        share_cache_path(session),
        compact=compact,
        protected=[str(session.get("path") or "")],
    )


def cmd_share(args, registry: ParserRegistry) -> dict:
    session = resolve_ref(
        registry,
        args.session,
        args.limit,
        include_missing_cwd=_include_missing_cwd(args),
    )
    compact = getattr(args, "compact", False)
    out = getattr(args, "out", None)
    payload = build_share_payload(session, registry)
    if not out:
        return ok(payload)
    output_path = write_share_envelope(
        payload,
        out,
        compact=compact,
        protected=[str(session.get("path") or "")],
    )
    summary = session_payload(session, DEFAULT_LIST_FIELDS if compact else None)
    summary.update(
        {
            "schema": SCHEMA_ID,
            "output_path": output_path,
            "output_bytes": os.path.getsize(output_path),
            "event_count": payload["event_count"],
            "counts": payload["counts"],
            "events_omitted": True,
        }
    )
    return ok(summary)


COMMANDS = [
    {
        "name": "list",
        "help": "List recent local agent sessions as structured JSON",
        "handler": cmd_list,
        "args": [
            {"flags": ["--runtime"], "kwargs": {"help": "claude / codex / opencode / kimi / cursor / pi"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 50, "help": "scan depth per runtime"}},
            {"flags": ["--top"], "kwargs": {"type": int, "default": None, "help": "return at most N sessions after filter/sort"}},
            {"flags": ["--status"], "kwargs": {"choices": ["done", "pending", "aborted", "unknown"]}},
            {"flags": ["--cwd"], "kwargs": {"help": "substring filter on session cwd"}},
            {"flags": ["--live"], "kwargs": {"action": "store_true", "help": "only sessions currently live"}},
            {
                "flags": ["--include-missing-cwd"],
                "kwargs": {
                    "action": "store_true",
                    "help": "keep sessions whose project directory was deleted (archive mode; default hides them for resume-oriented lists)",
                },
            },
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
            {"flags": ["--fields"], "kwargs": {"help": "comma-separated field allowlist"}},
        ],
        "returns": {"count": "int", "sessions": "array of session objects"},
    },
    {
        "name": "search",
        "help": "Search sessions by keywords in titles/previews (optional --deep full text)",
        "handler": cmd_search,
        "args": [
            {"flags": ["keywords"], "kwargs": {"nargs": "+", "help": "keywords (AND)"}},
            {"flags": ["--deep"], "kwargs": {"action": "store_true"}},
            {"flags": ["--runtime"], "kwargs": {}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 50}},
            {"flags": ["--top"], "kwargs": {"type": int, "default": None}},
            {"flags": ["--live"], "kwargs": {"action": "store_true"}},
            {"flags": ["--include-missing-cwd"], "kwargs": {"action": "store_true"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
            {"flags": ["--fields"], "kwargs": {}},
        ],
        "returns": {"query": "keywords", "sessions": "ranked matches"},
    },
    {
        "name": "show",
        "help": "Show one session plus plain-text conversation messages",
        "handler": cmd_show,
        "args": [
            {"flags": ["session"], "kwargs": {"help": "id, prefix, or runtime:id"}},
            {"flags": ["--messages"], "kwargs": {"type": int, "default": None}},
            {"flags": ["--full"], "kwargs": {"action": "store_true"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": _RESOLVE_SCAN_LIMIT}},
            {"flags": ["--include-missing-cwd"], "kwargs": {"action": "store_true"}},
            {"flags": ["--out"], "kwargs": {"help": "write full envelope to a file"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
            {"flags": ["--fields"], "kwargs": {}},
        ],
        "returns": {"messages": "user/assistant text turns"},
    },
    {
        "name": "export",
        "help": "Export full plain-text conversations in a time range",
        "handler": cmd_export,
        "args": [
            {"flags": ["--since"], "kwargs": {}},
            {"flags": ["--until"], "kwargs": {}},
            {"flags": ["--runtime"], "kwargs": {}},
            {"flags": ["--status"], "kwargs": {"choices": ["done", "pending", "aborted", "unknown"]}},
            {"flags": ["--cwd"], "kwargs": {}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200}},
            {"flags": ["--include-missing-cwd"], "kwargs": {"action": "store_true"}},
            {"flags": ["--out"], "kwargs": {}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
        ],
        "returns": {"sessions": "array with full messages"},
    },
    {
        "name": "share",
        "help": "Export unified transcript events (thinking + tool calls)",
        "handler": cmd_share,
        "args": [
            {"flags": ["session"], "kwargs": {}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": _RESOLVE_SCAN_LIMIT}},
            {"flags": ["--include-missing-cwd"], "kwargs": {"action": "store_true"}},
            {"flags": ["--out"], "kwargs": {}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
        ],
        "returns": {"schema": SCHEMA_ID, "events": "event array"},
    },
    {
        "name": "describe",
        "help": "Machine-readable command/argument descriptions",
        "handler": None,
        "args": [{"flags": ["command"], "kwargs": {"nargs": "?", "default": None}}],
        "returns": {"commands": "specs"},
    },
]


def cmd_describe(args, _registry: ParserRegistry) -> dict:
    target = getattr(args, "command", None)
    if target:
        spec = next((c for c in COMMANDS if c["name"] == target), None)
        if spec is None:
            raise ApiError("not_found", f"unknown command: {target}", EXIT_NOT_FOUND)
        return ok(_describe_command(spec, full=True))
    return ok({"commands": [_describe_command(spec, full=False) for spec in COMMANDS]})


def _describe_command(spec: dict, full: bool) -> dict:
    out = {"name": spec["name"], "help": spec["help"]}
    if full:
        out["args"] = [{"flags": a["flags"], **{k: v for k, v in a.get("kwargs", {}).items() if k != "type"}} for a in spec.get("args", [])]
        out["returns"] = spec.get("returns")
    return out


COMMANDS[-1]["handler"] = cmd_describe


def build_parser() -> JSONArgumentParser:
    parser = JSONArgumentParser(prog="sesskit", description="Read/parse/export local coding-agent sessions")
    sub = parser.add_subparsers(dest="command", required=True)
    for spec in COMMANDS:
        parts = spec["name"].split()
        p = sub.add_parser(parts[0], help=spec["help"])
        for arg in spec.get("args", []):
            kwargs = dict(arg.get("kwargs") or {})
            typ = kwargs.pop("type", None)
            if typ is not None:
                kwargs["type"] = typ
            p.add_argument(*arg["flags"], **kwargs)
        p.set_defaults(_handler=spec["handler"])
    return parser


def dispatch(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    compact = bool(getattr(args, "compact", False))
    registry = default_registry()
    try:
        runtime_filter = getattr(args, "runtime", None)
        if runtime_filter:
            registry.get(runtime_filter)
    except KeyError as exc:
        print_envelope(err(ApiError("not_found", str(exc), EXIT_NOT_FOUND)), compact=compact)
        return EXIT_NOT_FOUND
    try:
        handler = args._handler
        payload = handler(args, registry)
        print_envelope(payload, compact=compact)
        return EXIT_OK
    except ApiError as exc:
        print_envelope(err(exc), compact=compact)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 — CLI top-level
        print_envelope(
            err(ApiError("error", str(exc), EXIT_ERROR)),
            compact=compact,
        )
        return EXIT_ERROR


def main() -> None:
    sys.exit(dispatch(sys.argv[1:]))


if __name__ == "__main__":
    main()
