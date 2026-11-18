"""SessKit CLI: language-agnostic JSON interface for local agent sessions.

History commands are read-only; ``upgrade`` writes only its own pip-managed
installation. Snapshot output is a JSON envelope; ``stream`` emits bare
versioned JSONL message observations. Snapshot envelope:
{"ok": true|false, "data": ..., "error": ..., "meta": {"version": 1, "tool_version": "X.Y.Z"}}

Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 5 ambiguous.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from sesskit import __version__, titles
from sesskit.adapters import list_adapters
from sesskit.cache import cache_dir
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
from sesskit.lifecycle_cli import cmd_upgrade, cmd_version
from sesskit.models import format_message_time, session_key
from sesskit.paths import assert_not_history_path, atomic_write_json
from sesskit.performance_cli import cmd_performance
from sesskit.registry import ConversationLoadError, ParserRegistry, default_registry
from sesskit.stats_cli import cmd_stats, stats_args
from sesskit.streaming_cli import StreamCommand, cmd_stream, print_stream_error
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
        if getattr(self, "_stream_errors", False) or self.prog.endswith(" stream"):
            print_stream_error(ApiError(
                "usage_error", message, EXIT_USAGE,
                hint="run sesskit describe stream", next_commands=["sesskit describe stream"],
            ), getattr(self, "_stream_ref", "unknown"))
            raise SystemExit(EXIT_USAGE)
        print_envelope(
            err(ApiError(
                "usage_error", message, EXIT_USAGE,
                hint="run sesskit describe or sesskit describe <command>",
                next_commands=["sesskit describe"],
            )),
            compact=bool(getattr(self, "_stream_errors", False) or self.prog.endswith(" stream")),
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
        "loop_count": session.get("loop_count"),
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


def _load_messages(runtime, session: dict, *, include_errors: bool = False):
    try:
        return runtime.load_conversation(session, include_errors=include_errors)
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
    messages = _load_messages(runtime, session, include_errors=bool(getattr(args, "include_errors", False)))
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
            dt = datetime.strptime(raw, fmt)  # noqa: DTZ007 - Local wall time; convert after end-of-day adjustment.
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
    include_errors = bool(getattr(args, "include_errors", False))
    for runtime, session in candidates:
        payload = session_payload(session)
        try:
            messages = _load_messages(runtime, session, include_errors=include_errors)
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


def cmd_verify(args, registry: ParserRegistry):
    """Run the conformance suite over local real sessions.

    Prints counts and timings only, never content or paths. Returns a
    ``(envelope, exit_code)`` pair so failures surface as non-zero exits
    while keeping the standard envelope.
    """
    from sesskit import conformance as _conformance
    from sesskit.envelope import EXIT_ERROR as _EXIT_ERROR
    from sesskit.envelope import EXIT_OK as _EXIT_OK

    requested = getattr(args, "runtime", None) or []
    runtime_ids = list(requested) if isinstance(requested, list) else [requested]
    runtime_ids = [r for r in runtime_ids if r] or list(registry.ids)
    raw_sample = getattr(args, "sample", 200)
    sample = 200 if raw_sample is None else int(raw_sample)
    if sample < 0:
        raise ApiError("usage_error", "--sample must be zero or greater", EXIT_USAGE)
    report = _conformance.verify_all(registry, runtime_ids, sample)
    code = _EXIT_OK if report["passed"] else _EXIT_ERROR
    return ok(report), code


def _usage_selection_models_supported(usage_mod) -> bool:
    """Whether this build's ``UsageSelection`` carries a native model filter."""
    try:
        fields = getattr(usage_mod.UsageSelection, "__dataclass_fields__", {}) or {}
    except (AttributeError, TypeError):
        return False
    return "models" in fields


def _filter_records_by_ids(records, *, turn_id=None, message_id=None):
    if turn_id:
        return [r for r in records if r.turn_id == turn_id]
    if message_id:
        return [r for r in records
                if r.message_id == message_id or r.response_id == message_id]
    return list(records)


def _record_matches_model(record, allowed: set) -> bool:
    """Model-or-requested exact match, mirroring ``UsageSelection.models``."""
    return (
        getattr(record, "model", None) in allowed
        or getattr(record, "requested_model", None) in allowed
    )


def _filter_records_by_model(records, wanted):
    """Exact-match native-model filter over model OR requested_model."""
    if not wanted:
        return list(records)
    allowed = set(wanted)
    return [r for r in records if _record_matches_model(r, allowed)]


def _effective_requested_model(observation):
    """Merged requested_model: observation wins, usage fills gaps.

    Mirrors ``usage._observation_labels`` (empty string counts as absent).
    Reuses the sibling helper when importable so the CLI filter and the
    summary selector never drift; otherwise falls back to the same inline
    OR logic.
    """
    try:
        from sesskit.usage import _observation_labels as _labels
    except Exception:  # noqa: BLE001 — sibling helper is optional
        _labels = None
    if _labels is not None:
        try:
            return _labels(observation).get("requested_model")
        except Exception:  # noqa: BLE001 — fall back to inline OR below
            _labels = None
    obs = getattr(observation, "requested_model", None)
    if isinstance(obs, str) and obs:
        return obs
    if obs is not None and not isinstance(obs, str):
        return obs
    usage = getattr(observation, "usage", None)
    fallback = getattr(usage, "requested_model", None)
    if isinstance(fallback, str) and not fallback:
        return None
    return fallback


def _filter_observations_by_model(observations, wanted):
    """Exact-match native-model filter over model OR merged requested_model.

    The requested side uses the merged observation-or-usage label (same
    caliber as the summary ``UsageSelection.models`` path), never a bare
    single-level field.
    """
    if not wanted:
        return list(observations)
    allowed = set(wanted)
    kept = []
    for observation in observations:
        usage = getattr(observation, "usage", None)
        if getattr(usage, "model", None) in allowed:
            kept.append(observation)
            continue
        if _effective_requested_model(observation) in allowed:
            kept.append(observation)
    return kept


def _hook_item_matches_model(item, allowed: set) -> bool:
    """Model filter for ``iter_message_models`` rows.

    Rows that already carry ``requested_model`` match on model OR requested
    directly; older sibling builds whose rows lack the key degrade to
    model-only. Accepts dict rows (normalized) and attribute rows
    (un-normalized dataclass) via getattr fallback, never raising.
    """
    try:
        model = item.get("model") if isinstance(item, dict) else getattr(item, "model", None)
    except Exception:  # noqa: BLE001 — a malformed row never fails filtering
        model = None
    if model in allowed:
        return True
    try:
        if isinstance(item, dict):
            requested = item.get("requested_model", None)
            if requested is None:
                # Sibling build without the key: getattr fallback, still no crash.
                requested = getattr(item, "requested_model", None)
        else:
            requested = getattr(item, "requested_model", None)
    except Exception:  # noqa: BLE001 — a malformed row never fails filtering
        requested = None
    return requested is not None and requested in allowed


def _bare_suffix(native_id: str) -> str:
    """Bare suffix of a namespaced native id; identity otherwise.

    Handles ``...:message:<bare>`` (opencode, pi position fallback),
    ``...:entry:<bare>`` (pi) and the bare ``entry:<bare>`` record form.
    """
    if ":message:" in native_id:
        return native_id.rsplit(":message:", 1)[-1]
    if ":entry:" in native_id:
        return native_id.rsplit(":entry:", 1)[-1]
    if native_id.startswith("entry:"):
        return native_id[len("entry:"):]
    return native_id


def _enrich_hook_seq(items: list[dict], activity) -> list[dict]:
    """Fill ``seq=None`` hook rows by exact native identity, plus a guarded suffix backfill.

    The sibling ``iter_message_models`` correlates on exact native
    message/response identity only. Some runtimes namespace activity
    ``message_id`` values (opencode ``...:message:<bare>``, pi
    ``...:entry:<bare>``) while usage records keep the bare id, so exact
    matching alone leaves ``seq`` empty even though the native identity is
    the same. This backfills ``seq`` from the activity snapshot by exact
    match first, then by bare-suffix match only when that bare suffix maps
    to a single distinct activity id in the session; ambiguous bares keep
    ``seq=None``. It never fabricates a seq: rows without a native id
    match keep ``seq=None``.
    """
    events = getattr(activity, "events", None) or ()
    if not events or not items or not any(i.get("seq") is None for i in items):
        return items
    exact: dict[str, int] = {}
    bare_to_fulls: dict[str, set[str]] = {}
    bare_first_seq: dict[str, int] = {}
    for event in events:
        seq = getattr(event, "seq", None)
        if not isinstance(seq, int):
            continue
        message_id = getattr(event, "message_id", None)
        if not isinstance(message_id, str) or not message_id:
            continue
        if message_id not in exact:
            exact[message_id] = seq
        bare = _bare_suffix(message_id)
        if not bare or bare == message_id:
            # Bare activity ids (e.g. claude) are covered by exact match;
            # only namespaced ids participate in suffix backfill.
            continue
        fulls = bare_to_fulls.setdefault(bare, set())
        fulls.add(message_id)
        if bare not in bare_first_seq:
            bare_first_seq[bare] = seq
    if not exact:
        return items
    unique_bare = {
        bare: seq for bare, seq in bare_first_seq.items()
        if len(bare_to_fulls.get(bare, ())) == 1
    }
    enriched = []
    for item in items:
        if item.get("seq") is not None:
            enriched.append(item)
            continue
        seq = None
        for key in (item.get("message_id"), item.get("response_id")):
            if not isinstance(key, str) or not key:
                continue
            if key in exact:
                seq = exact[key]
                break
            bare = _bare_suffix(key)
            if bare and bare in unique_bare:
                seq = unique_bare[bare]
                break
        if seq is None:
            enriched.append(item)
            continue
        filled = dict(item)
        filled["seq"] = seq
        enriched.append(filled)
    return enriched


def _message_entry(event) -> dict:
    """One per-message model attribution entry from an activity event."""
    usage = getattr(event, "usage", None)
    model = getattr(usage, "model", None) if usage is not None else None
    provider = getattr(usage, "provider", None) if usage is not None else None
    usage_evidence = getattr(usage, "evidence", None) if usage is not None else None
    evidence = usage_evidence if usage_evidence is not None else getattr(event, "evidence", None)
    entry: dict = {
        "seq": getattr(event, "seq", None),
        "type": getattr(event, "type", None),
        "message_id": getattr(event, "message_id", None),
        "turn_id": getattr(event, "turn_id", None),
        "model": model,
        "provider": provider,
        "evidence": (
            {"origin": evidence.origin, "field": evidence.field, "record": evidence.record}
            if evidence is not None else None
        ),
    }
    return entry


def _hook_message_items(iter_fn, snapshot, activity):
    """Best-effort ``iter_message_models`` call; None on any shape mismatch."""
    for args in ((snapshot, activity), (snapshot,)):
        try:
            result = iter_fn(*args)
        except TypeError:
            continue
        except Exception:  # noqa: BLE001 — sibling helper is optional; always degrade
            return None
        try:
            items = list(result)
        except TypeError:
            return None
        normalized = []
        for item in items:
            if isinstance(item, dict):
                normalized.append(item)
                continue
            if hasattr(item, "__dataclass_fields__"):
                import dataclasses
                try:
                    normalized.append(dataclasses.asdict(item))
                except (TypeError, ValueError):
                    continue
                continue
            return None
        return normalized
    return None


def _message_model_items(adapter, session, snapshot, usage_mod, *, turn_id=None, message_id=None, models=()):
    """Per-message model attribution entries, newest-agnostic file order.

    Primary source is the sibling ``iter_message_models`` usage helper when
    this build provides one and it yields well-shaped dicts (the activity
    snapshot rides along so message rows carry their event ``seq``);
    otherwise the fallback groups the runtime's own activity events
    (assistant/thinking) by their natively attributed ``Usage.model``.
    Never raises for a missing helper: absence degrades to the activity
    fallback, and an unreadable activity degrades to an empty list.
    """
    try:
        runtime = getattr(adapter, "adapter", None) or adapter
        activity = runtime.load_activity(session)
    except Exception:  # noqa: BLE001 — introspection must not fail the usage page
        activity = None
    iter_fn = getattr(usage_mod, "iter_message_models", None)
    if callable(iter_fn):
        try:
            hooked = _hook_message_items(iter_fn, snapshot, activity)
        except Exception:  # noqa: BLE001 — sibling helper is optional; always degrade
            hooked = None
        if hooked:
            if turn_id:
                hooked = [i for i in hooked if i.get("turn_id") == turn_id]
            elif message_id:
                hooked = [i for i in hooked
                         if i.get("message_id") == message_id or i.get("response_id") == message_id]
            if models:
                allowed = set(models)
                hooked = [i for i in hooked if _hook_item_matches_model(i, allowed)]
            return _enrich_hook_seq(hooked, activity)
    events = getattr(activity, "events", None) or ()
    items = [
        _message_entry(event)
        for event in events
        if getattr(event, "type", None) in {"assistant_message", "thinking"}
    ]
    if turn_id:
        items = [i for i in items if i.get("turn_id") == turn_id]
    elif message_id:
        items = [i for i in items if i.get("message_id") == message_id]
    if models:
        allowed = set(models)
        items = [i for i in items if _hook_item_matches_model(i, allowed)]
    return items


def cmd_usage(args, registry: ParserRegistry) -> dict:
    """Show preserved native usage: a bounded summary plus paged records/observations/messages.

    Read-only. Summaries never invent totals: unknown stays unknown, and a
    session rollup only ever sets ``complete_total``, never joins the
    additive sums. Detail pages carry an opaque generation-bound cursor.
    ``--model`` is an exact-match native-model filter (model OR
    requested_model), repeatable and orthogonal to
    ``--turn-id``/``--message-id``; top-level ``models`` stays the
    full-session model table and never narrows with selectors.
    ``--detail messages`` lists one entry per assistant/thinking message
    with its natively attributed model (null when the native record
    carries none); messages items never carry ``native_subdivisions``,
    so ``omitted_native_subdivisions`` stays 0 for messages.
    """
    from sesskit import usage as _usage

    detail = getattr(args, "detail", "summary") or "summary"
    if detail not in {"summary", "records", "observations", "messages"}:
        raise ApiError("usage_error", "--detail must be summary, records, observations or messages", EXIT_USAGE)
    raw_limit = getattr(args, "limit", None)
    cursor = getattr(args, "cursor", None)
    turn_id = getattr(args, "turn_id", None)
    message_id = getattr(args, "message_id", None)
    raw_models = getattr(args, "model", None) or []
    models = [m for m in raw_models if isinstance(m, str)]
    include_native = bool(getattr(args, "include_native", False))
    if detail == "summary" and (raw_limit is not None or cursor):
        raise ApiError("usage_error", "--limit/--cursor are only valid with --detail", EXIT_USAGE)
    if detail != "summary" and raw_limit is not None and int(raw_limit) <= 0:
        raise ApiError("usage_error", "--limit must be positive", EXIT_USAGE)
    if turn_id and message_id:
        raise ApiError("usage_error", "--turn-id and --message-id are mutually exclusive", EXIT_USAGE)
    limit = 100 if raw_limit is None else int(raw_limit)
    view = getattr(args, "view", "all-branches") or "all-branches"
    history_view = "active_branch" if view == "active-branch" else "all_branches"

    ref = getattr(args, "session", "")
    session = resolve_ref(
        registry,
        ref,
        _RESOLVE_SCAN_LIMIT,
        include_missing_cwd=_include_missing_cwd(args),
    )
    snapshot = _usage.load_usage(session, history_view=history_view)
    if snapshot.state == "unavailable":
        raise ApiError(
            "load_failed",
            f"history unreadable: {ref}",
            EXIT_ERROR,
            hint="the session may have been deleted; refresh with sesskit list",
            next_commands=["sesskit list"],
        )

    payload = _usage.snapshot_to_dict(snapshot, include_native=include_native)
    if "models" not in payload:
        # Top-level ``models`` is the full-session model table and never
        # narrows with selectors (--turn-id/--message-id/--model only scope
        # ``summary``/``detail``); sibling ``snapshot_to_dict`` builds may
        # already project it, otherwise derive it from the full summary's
        # by_model slices.
        try:
            by_model = (payload.get("summary") or {}).get("by_model") or []
            payload["models"] = [e.get("model") for e in by_model if e.get("model") is not None]
        except (AttributeError, TypeError):
            payload["models"] = []
    summary = snapshot.summary
    selectors: dict = {}
    if turn_id or message_id or models:
        use_selection_models = bool(models) and _usage_selection_models_supported(_usage)
        sel_kwargs: dict = {}
        if turn_id:
            sel_kwargs["turn_ids"] = (turn_id,)
        elif message_id:
            sel_kwargs["message_ids"] = (message_id,)
        if use_selection_models:
            sel_kwargs["models"] = tuple(models)
        selection = _usage.UsageSelection(**sel_kwargs) if sel_kwargs else None
        if selection is not None and (not models or use_selection_models):
            summary = _usage.summarize_usage(snapshot, selection=selection)
        else:
            records = _filter_records_by_ids(snapshot.records, turn_id=turn_id, message_id=message_id)
            records = _filter_records_by_model(records, models)
            summary = _usage.summarize(
                records,
                session_ref=snapshot.session_ref,
                runtime=snapshot.runtime,
                history_view=snapshot.history_view,
                observed_count=snapshot.observed_count,
            )
        selectors = {k: v for k, v in {"turn_id": turn_id, "message_id": message_id}.items() if v}
        if models:
            selectors["models"] = list(models)
        assert summary is not None
        payload["summary"] = _usage.summary_to_dict(summary)
        payload["selectors"] = selectors
        full_summary = snapshot.summary
        assert full_summary is not None
        payload["matched_records"] = summary.record_count
        payload["unlinked_records"] = len(full_summary.ambiguous_ids)
        if summary.record_count == 0 and detail != "messages":
            # Messages mode judges match on attributed messages below;
            # record counts stay zero for runtimes without token usage.
            payload.setdefault("diagnostics", []).append("selector_matched_zero")
    else:
        assert summary is not None
        payload["summary"] = _usage.summary_to_dict(summary)

    if detail == "summary":
        return ok(payload)

    if turn_id:
        observations = [o for o in snapshot.observations if o.turn_id == turn_id]
        records = [r for r in snapshot.records if r.turn_id == turn_id]
    elif message_id:
        observations = [o for o in snapshot.observations
                        if o.message_id == message_id or o.response_id == message_id]
        records = [r for r in snapshot.records
                   if r.message_id == message_id or r.response_id == message_id]
    else:
        observations = list(snapshot.observations)
        records = list(snapshot.records)
    observations = _filter_observations_by_model(observations, models)
    records = _filter_records_by_model(records, models)
    cursor_gen = snapshot.watermark
    if detail == "messages":
        runtime_adapter = registry.get(str(session.get("source") or ""))
        items = _message_model_items(
            runtime_adapter, session, snapshot, _usage,
            turn_id=turn_id, message_id=message_id, models=models,
        )
        if cursor_gen is None:
            # Usage-less runtimes (e.g. Cursor) carry no snapshot watermark;
            # bind the page cursor to a fingerprint of the message list so
            # a changed source still reads as stale instead of misaligned.
            # The fingerprint joins seq/type/model with message/response ids
            # so distinct messages sharing one seq/type/model never collide.
            import hashlib
            import json as _json

            fingerprint = _json.dumps(
                [(i.get("seq"), i.get("type"), i.get("model"),
                  i.get("message_id"), i.get("response_id")) for i in items],
                sort_keys=True, default=str,
            )
            cursor_gen = "messages:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:32]
        if selectors and not items:
            payload.setdefault("diagnostics", []).append("selector_matched_zero")

        def serialize(item, include_native=False):
            # Messages detail intentionally carries no native_subdivisions
            # even with --include-native: per-message attribution only.
            return item
    else:
        items = (records if detail == "records" else observations)
        serialize = _usage.record_to_dict if detail == "records" else _usage.observation_to_dict
    total = len(items)
    offset = 0
    if cursor:
        try:
            offset = _usage.decode_usage_cursor(cursor, cursor_gen)
        except ValueError as exc:
            if str(exc) == "stale":
                raise ApiError(
                    "stale_cursor",
                    "usage page cursor is stale; source changed, fetch a fresh page",
                    EXIT_ERROR,
                    next_commands=[f"sesskit usage {ref}"],
                ) from exc
            raise ApiError("usage_error", "malformed usage page cursor", EXIT_USAGE) from exc
    if offset > total:
        raise ApiError("usage_error", "cursor offset beyond available items", EXIT_USAGE)
    page = items[offset:offset + limit]
    has_more = offset + limit < total
    if detail == "messages":
        # Messages items carry no native_subdivisions by design, so the
        # omitted count stays 0 in both modes -- the same caliber as
        # records/observations (0 when included, else subdivided rows
        # omitted), just with no subdivided message rows to omit.
        omitted_native_subdivisions = 0
    else:
        omitted_native_subdivisions = (
            0 if include_native else sum(1 for o in page
                                         if getattr(o, "native_subdivisions", ()))
        )
    payload["detail"] = {
        "kind": detail,
        "items": [serialize(item, include_native=include_native) for item in page],
        "returned": len(page),
        "total": total,
        "has_more": has_more,
        "cursor": _usage.encode_usage_cursor(cursor_gen or "", offset + limit)
        if has_more and cursor_gen else None,
        "read_coverage": {
            "entries_read": snapshot.entries_read,
            "observed": snapshot.observed_count,
            "records": len(snapshot.records),
        },
        "scope": {"history_view": snapshot.history_view, **selectors},
        "omitted_native_subdivisions": omitted_native_subdivisions,
    }
    return ok(payload)


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
            {"flags": ["--include-errors"], "kwargs": {"action": "store_true", "help": "also show error-only assistant turns; default hides them from the chat view"}},
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
            {"flags": ["--include-errors"], "kwargs": {"action": "store_true", "help": "also include error-only assistant turns; default hides them from the chat view"}},
            {"flags": ["--out"], "kwargs": {}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
        ],
        "returns": {"sessions": "array with full messages"},
    },
    {
        "name": "usage",
        "help": "Show preserved native token/cost usage for one session (bounded summary plus paged records/messages)",
        "handler": cmd_usage,
        "args": [
            {"flags": ["session"], "kwargs": {"help": "id, prefix, or runtime:id"}},
            {"flags": ["--detail"], "kwargs": {"choices": ["summary", "records", "observations", "messages"], "default": "summary", "help": "summary (default) or one paged detail list; messages shows one entry per assistant/thinking message with its natively attributed model (messages items never carry native_subdivisions; omitted_native_subdivisions stays 0 for messages)"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": None, "help": "detail page size (detail mode only)"}},
            {"flags": ["--cursor"], "kwargs": {"help": "opaque page cursor from a previous detail page (detail mode only)"}},
            {"flags": ["--view"], "kwargs": {"choices": ["all-branches", "active-branch"], "default": "all-branches", "help": "accounting history view"}},
            {"flags": ["--turn-id"], "kwargs": {"help": "restrict to one native turn id (mutually exclusive with --message-id)"}},
            {"flags": ["--message-id"], "kwargs": {"help": "restrict to one native message/response id (mutually exclusive with --turn-id)"}},
            {"flags": ["--model"], "kwargs": {"action": "append", "default": None, "help": "restrict to one native model (exact match over model OR requested_model); repeatable; orthogonal to --turn-id/--message-id; top-level models stays the full-session table and never narrows with selectors"}},
            {"flags": ["--include-native"], "kwargs": {"action": "store_true", "help": "include bounded native subdivisions and provider/cost-basis metadata (messages detail still carries no native_subdivisions)"}},
            {"flags": ["--include-missing-cwd"], "kwargs": {"action": "store_true"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
            {"flags": ["--json"], "kwargs": {"action": "store_true", "help": "single-line JSON output (same as --compact)"}},
        ],
        "returns": {"schema": "sesskit.usage/v1", "summary": "scoped totals with coverage", "detail": "optional paged records/observations/messages"},
    },
    {
        "name": "performance",
        "help": "Show preserved native timing/rate evidence for one session (bounded summary plus paged records)",
        "handler": cmd_performance,
        "args": [
            {"flags": ["session"], "kwargs": {"help": "id, prefix, or runtime:id"}},
            {"flags": ["--detail"], "kwargs": {"choices": ["summary", "records"], "default": "summary", "help": "summary (default) or paged record list"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": None, "help": "detail page size 1..1000 (detail mode only)"}},
            {"flags": ["--cursor"], "kwargs": {"help": "opaque page cursor from a previous detail page (detail mode only)"}},
            {"flags": ["--view"], "kwargs": {"choices": ["all-branches", "active-branch"], "default": "all-branches", "help": "performance history view"}},
            {"flags": ["--turn-id"], "kwargs": {"help": "restrict to one native turn id (mutually exclusive with --message-id)"}},
            {"flags": ["--message-id"], "kwargs": {"help": "restrict to one native message/response id (mutually exclusive with --turn-id)"}},
            {"flags": ["--include-missing-cwd"], "kwargs": {"action": "store_true"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
            {"flags": ["--json"], "kwargs": {"action": "store_true", "help": "single-line JSON output (same as --compact)"}},
        ],
        "returns": {"schema": "sesskit.performance/v1", "summary": "bounded timing/rate summary", "detail": "optional paged records"},
    },
    {
        "name": "stats",
        "help": "Show windowed active-time ledger per session or per project (read-only, 7-day default)",
        "handler": cmd_stats,
        "args": stats_args(),
        "returns": {"schema": "sesskit.stats/v1", "by": "session|project", "totals": "bounded active-time sums"},
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
        "name": "stream",
        "help": "Read-only JSONL message events with automatic history degradation. Default budget: 60s/1000 records/256KiB; --unbounded follows continuously. Example: sesskit stream pi:ID --once",
        "handler": cmd_stream,
        "args": [
            {"flags": ["session"], "kwargs": {"help": "existing runtime:id or unambiguous prefix"}},
            {"flags": ["--input"], "kwargs": {"help": "borrowed native event feed file, or - for stdin; does not launch an agent"}},
            {"flags": ["--input-format"], "kwargs": {"choices": ["ndjson", "sse"], "help": "native framing; requires --input"}},
            {"flags": ["--once"], "kwargs": {"action": "store_true", "help": "one bounded poll"}},
            {"flags": ["--unbounded"], "kwargs": {"action": "store_true", "help": "explicitly lift default time/record/byte budgets"}},
            {"flags": ["--duration"], "kwargs": {"type": float, "help": "observation time budget in seconds (default 60)"}},
            {"flags": ["--max-records"], "kwargs": {"type": int, "help": "total output record budget including end (default 1000)"}},
            {"flags": ["--max-bytes"], "kwargs": {"type": int, "help": "total output byte budget including end (default 262144)"}},
            {"flags": ["--polls"], "kwargs": {"type": int, "help": "bounded poll count; otherwise follow until interrupted"}},
            {"flags": ["--interval"], "kwargs": {"type": float, "default": 0.25}},
            {"flags": ["--max-events"], "kwargs": {"type": int, "default": 200, "help": "retained observation window, 1..200"}},
            {"flags": ["--cursor"], "kwargs": {"help": "opaque checkpoint; source changes explicitly reset the window"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": _RESOLVE_SCAN_LIMIT}},
            {"flags": ["--include-missing-cwd"], "kwargs": {"action": "store_true"}},
        ],
        "returns": {"schema": "sesskit.message-stream/v1", "framing": "one JSON event per line, including error end events",
                    "operations": ["state", "reset", "upsert", "remove", "checkpoint", "end"],
                    "side_effects": "read-only observation; never launches or interrupts an agent"},
    },
    {
        "name": "verify",
        "help": "Run conformance + real-history parity checks over local sessions (counts/timings only)",
        "handler": cmd_verify,
        "args": [
            {"flags": ["--runtime"], "kwargs": {"action": "append", "default": None, "help": "runtime id, repeatable; default all"}},
            {"flags": ["--sample"], "kwargs": {"type": int, "default": 200, "help": "recent sessions per runtime"}},
            {"flags": ["--json"], "kwargs": {"action": "store_true", "help": "single-line JSON output (same as --compact)"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
        ],
        "returns": {"passed": "bool", "runtimes": "per-runtime counts", "totals": "aggregate counts"},
    },
    {
        "name": "version",
        "help": "Show the installed SessKit package version (offline; no history scan)",
        "handler": cmd_version,
        "args": [
            {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
            {"flags": ["--json"], "kwargs": {"action": "store_true", "help": "single-line JSON output (same as --compact)"}},
        ],
        "returns": {"name": "sesskit", "version": "installed package version"},
    },
    {
        "name": "upgrade",
        "help": "Check or upgrade this pip-managed SessKit installation; writes no session history",
        "handler": cmd_upgrade,
        "risk": "write_local",
        "args": [
            {"flags": ["--dry-run"], "kwargs": {"action": "store_true", "help": "discover and compare versions without invoking pip or writing the installation"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "single-line JSON output"}},
            {"flags": ["--json"], "kwargs": {"action": "store_true", "help": "single-line JSON output (same as --compact)"}},
        ],
        "returns": {"status": "update_available|updated|unchanged|ahead", "current_version": "verified package version", "command": "absolute Python argv when an upgrade is available"},
    },
    {
        "name": "describe",
        "help": "Machine-readable command/argument descriptions",
        "handler": None,
        "args": [{"flags": ["command"], "kwargs": {"nargs": "?", "default": None}}],
        "returns": {"commands": "specs", "runtimes": "adapter ids with capabilities"},
    },
]


def cmd_describe(args, _registry: ParserRegistry) -> dict:
    target = getattr(args, "command", None)
    if target:
        spec = next((c for c in COMMANDS if c["name"] == target), None)
        if spec is None:
            raise ApiError("not_found", f"unknown command: {target}", EXIT_NOT_FOUND)
        return ok({"tool_version": __version__, **_describe_command(spec, full=True)})
    return ok({
        "tool_version": __version__,
        "commands": [_describe_command(spec, full=False) for spec in COMMANDS],
        "runtimes": [_describe_adapter(adapter) for adapter in list_adapters()],
    })


def _describe_adapter(adapter) -> dict:
    from dataclasses import asdict

    return {
        "id": adapter.id,
        "display_name": adapter.display_name,
        "capabilities": asdict(adapter.capabilities),
    }


def _describe_command(spec: dict, full: bool) -> dict:
    default_risk = "write_local" if any("--out" in a["flags"] for a in spec.get("args", [])) else "read"
    out = {"name": spec["name"], "help": spec["help"], "risk": spec.get("risk", default_risk)}
    if full:
        out["args"] = [{"flags": a["flags"], **{k: v for k, v in a.get("kwargs", {}).items() if k != "type"}} for a in spec.get("args", [])]
        out["returns"] = spec.get("returns")
    return out


COMMANDS[-1]["handler"] = cmd_describe


def _recover_stream_session_ref(argv: list[str], stream_parser) -> str:
    """Recover the session positional for pre-start stream errors.

    Scans ``argv`` (full dispatch argv starting with ``"stream"``) using the
    live stream subparser's registered option arity, so ``--max-events 10``,
    ``--input`` paths and opaque ``--cursor`` JSON are never mistaken for the
    session ref. Returns ``"unknown"`` when genuinely ambiguous.
    """
    if not argv or argv[0] != "stream":
        return "unknown"
    opt_arity: dict[str, int] = {}
    actions = getattr(stream_parser, "_actions", None) if stream_parser is not None else None
    if actions is None:
        return "unknown"
    for action in actions:
        option_strings = getattr(action, "option_strings", None) or []
        if not option_strings:
            continue
        nargs = getattr(action, "nargs", None)
        if nargs == 0:
            arity = 0
        elif nargs is None:
            arity = 1
        else:  # Variadic/unknown arity: refuse to guess.
            return "unknown"
        for opt in option_strings:
            opt_arity[opt] = arity
    candidates: list[str] = []
    uncertain = False
    tokens = list(argv[1:])
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            candidates.extend(tokens[i + 1:])
            break
        if tok.startswith("-") and tok != "-":
            if "=" in tok:
                # --option=value: value can never be the session ref.
                i += 1
                continue
            arity = opt_arity.get(tok)
            if arity is None:
                # Unknown option: skip a following non-flag token, which may
                # be its value, so it is never mistaken for the session ref.
                i += 1
                if i < len(tokens) and (tokens[i] == "-" or not tokens[i].startswith("-")):
                    i += 1
                    uncertain = True
                continue
            if arity == 0:
                i += 1
                continue
            # Known single-value option: consume its value unconditionally;
            # option values are never session candidates.
            i += 1
            if i < len(tokens):
                i += 1
            continue
        candidates.append(tok)
        i += 1
    if len(candidates) != 1:
        return "unknown"
    cand = candidates[0]
    if not cand or cand == "-" or cand.startswith("-"):
        return "unknown"
    if uncertain:
        return "unknown"
    return cand


def build_parser() -> JSONArgumentParser:
    parser = JSONArgumentParser(
        prog="sesskit",
        description=f"Read/parse/export local coding-agent sessions — sesskit {__version__}",
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}",
        help="show the installed package version and exit",
    )
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
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    parser._stream_errors = bool(argv and argv[0] == "stream")
    _stream_subparser = None
    for _action in parser._actions:
        if isinstance(_action, argparse._SubParsersAction):
            _stream_subparser = _action.choices.get("stream")
            break
    if argv and argv[0] == "stream":
        _stream_ref = _recover_stream_session_ref(argv, _stream_subparser)
    else:
        _stream_ref = "unknown"
    parser._stream_ref = _stream_ref
    if _stream_subparser is not None:
        _stream_subparser._stream_ref = _stream_ref
    args = parser.parse_args(argv)
    compact = bool(getattr(args, "compact", False) or getattr(args, "json", False) or args.command == "stream")
    registry = default_registry()
    try:
        runtime_filter = getattr(args, "runtime", None)
        filters = runtime_filter if isinstance(runtime_filter, list) else [runtime_filter]
        for runtime_id in filters:
            if runtime_id:
                registry.get(runtime_id)
    except KeyError as exc:
        print_envelope(err(ApiError("not_found", str(exc), EXIT_NOT_FOUND)), compact=compact)
        return EXIT_NOT_FOUND
    try:
        handler = args._handler
        result = handler(args, registry)
        if isinstance(result, StreamCommand):
            return result.run()
        if isinstance(result, tuple):
            payload, code = result
        else:
            payload, code = result, EXIT_OK
        print_envelope(payload, compact=compact)
        return code
    except ApiError as exc:
        if args.command == "stream":
            print_stream_error(exc, args.session)
        else:
            print_envelope(err(exc), compact=compact)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 — CLI top-level
        if args.command == "stream":
            print_stream_error(ApiError(
                "error", f"stream observation failed ({type(exc).__name__})", EXIT_ERROR,
                hint="check the selected history and native input; see sesskit describe stream",
                next_commands=["sesskit describe stream"],
            ), args.session)
            return EXIT_ERROR
        print_envelope(
            err(ApiError("error", str(exc), EXIT_ERROR)),
            compact=compact,
        )
        return EXIT_ERROR


def main() -> None:
    sys.exit(dispatch(sys.argv[1:]))


if __name__ == "__main__":
    main()
