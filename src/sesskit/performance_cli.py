"""SessKit performance command: read-only timing/rate projection over native history.

This module owns CLI command logic only. Calculation and serialization live in
:mod:`sesskit.performance` (locked core API); native feeds live in the runtime
adapters. Core imports stay inside the command handler to keep discovery light.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from sesskit.envelope import (
    EXIT_ERROR,
    EXIT_USAGE,
    ApiError,
    ok,
)

PERFORMANCE_SCHEMA_ID = "sesskit.performance/v1"
_DETAIL_DEFAULT = "summary"
_PAGE_DEFAULT = 100
_PAGE_MAX = 1000


def encode_performance_cursor(
    *,
    watermark: str,
    session_ref: str | None,
    history_view: str,
    detail: str,
    turn_id: str | None,
    message_id: str | None,
    offset: int,
) -> str:
    """Encode an opaque bounded page cursor bound to source + query scope."""
    raw = json.dumps(
        {
            "v": 1,
            "schema": PERFORMANCE_SCHEMA_ID,
            "gen": watermark,
            "session": session_ref,
            "view": history_view,
            "detail": detail,
            "turn": turn_id,
            "msg": message_id,
            "off": offset,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_performance_cursor(
    token: str,
    *,
    watermark: str | None,
    session_ref: str | None,
    history_view: str,
    detail: str,
    turn_id: str | None,
    message_id: str | None,
) -> int:
    """Return the page offset, or raise ValueError("stale"|"malformed").

    Any binding mismatch (source generation, session, view, detail or
    filters) is ``stale``: the cursor does not belong to this query, so the
    caller must fetch a fresh page. Unparseable tokens are ``malformed``.
    The cursor never carries source paths or message content.
    """
    if not isinstance(token, str) or len(token) > 4096:
        raise ValueError("malformed")
    try:
        raw = base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("malformed") from None
    if (not isinstance(payload, dict) or type(payload.get("v")) is not int
            or payload["v"] != 1 or payload.get("schema") != PERFORMANCE_SCHEMA_ID):
        raise ValueError("malformed")
    if type(payload.get("off")) is not int or payload["off"] < 0:
        raise ValueError("malformed")
    bindings_match = (
        watermark is not None
        and payload.get("gen") == watermark
        and payload.get("session") == session_ref
        and payload.get("view") == history_view
        and payload.get("detail") == detail
        and payload.get("turn") == turn_id
        and payload.get("msg") == message_id
    )
    if not bindings_match:
        raise ValueError("stale")
    return int(payload["off"])


def _filter_records(records: Any, *, turn_id: str | None, message_id: str | None) -> list:
    if turn_id:
        return [record for record in records if record.turn_id == turn_id]
    if message_id:
        return [record for record in records if record.message_id == message_id or record.response_id == message_id]
    return list(records)


def cmd_performance(args, registry) -> dict:
    """Show preserved native timing/rate evidence for one session.

    Bounded summary by default plus an opaque-cursor paged record list.
    Read-only. Summaries never average unlike intervals or sum overlapping
    durations; unknown stays unknown.
    """
    from sesskit import cli as _cli

    detail = getattr(args, "detail", _DETAIL_DEFAULT) or _DETAIL_DEFAULT
    if detail not in {"summary", "records"}:
        raise ApiError("usage_error", "--detail must be summary or records", EXIT_USAGE)
    raw_limit = getattr(args, "limit", None)
    cursor = getattr(args, "cursor", None)
    turn_id = getattr(args, "turn_id", None)
    message_id = getattr(args, "message_id", None)
    if detail == "summary" and (raw_limit is not None or cursor):
        raise ApiError("usage_error", "--limit/--cursor are only valid with --detail records", EXIT_USAGE)
    if turn_id and message_id:
        raise ApiError("usage_error", "--turn-id and --message-id are mutually exclusive", EXIT_USAGE)
    if detail != "summary" and raw_limit is not None:
        try:
            limit_value = int(raw_limit)
        except (TypeError, ValueError):
            raise ApiError("usage_error", "--limit must be an integer 1..1000", EXIT_USAGE) from None
        if limit_value < 1 or limit_value > _PAGE_MAX:
            raise ApiError("usage_error", "--limit must be an integer 1..1000", EXIT_USAGE) from None
        limit = limit_value
    else:
        limit = _PAGE_DEFAULT
    view = getattr(args, "view", "all-branches") or "all-branches"
    if view not in {"all-branches", "active-branch"}:
        raise ApiError("usage_error", "--view must be all-branches or active-branch", EXIT_USAGE)
    history_view = "active_branch" if view == "active-branch" else "all_branches"

    ref = getattr(args, "session", "")
    session = _cli.resolve_ref(
        registry,
        ref,
        _cli._RESOLVE_SCAN_LIMIT,
        include_missing_cwd=_cli._include_missing_cwd(args),
    )

    from sesskit import performance as _performance

    runtime = str(session.get("source") or "")
    capabilities = _performance.performance_capabilities(runtime)
    try:
        _performance.validate_performance_view(history_view, capabilities)
    except ValueError as exc:
        raise ApiError("usage_error", str(exc), EXIT_USAGE) from exc

    snapshot = _performance.load_performance(session, history_view=history_view)
    if snapshot.state == "unavailable":
        raise ApiError(
            "load_failed",
            f"history unreadable: {ref}",
            EXIT_ERROR,
            hint="the session may have been deleted; refresh with sesskit list",
            next_commands=["sesskit list"],
        )

    payload = _performance.performance_snapshot_to_dict(snapshot)
    records = _filter_records(snapshot.records, turn_id=turn_id, message_id=message_id)
    selectors: dict = {}
    if turn_id or message_id:
        import dataclasses

        selectors = {k: v for k, v in {"turn_id": turn_id, "message_id": message_id}.items() if v}
        narrowed = dataclasses.replace(
            snapshot, records=tuple(records),
            state=("available" if records else "empty") if snapshot.state == "available" else snapshot.state,
        )
        payload["state"] = narrowed.state
        payload["summary"] = _performance.performance_summary(narrowed)
        payload["selectors"] = selectors
        payload["matched_records"] = len(records)
        payload["unlinked_records"] = sum(
            1 for record in snapshot.records
            if (record.turn_id is None if turn_id
                else record.message_id is None and record.response_id is None)
        )
        payload["unmatched_records"] = len(snapshot.records) - len(records) - payload["unlinked_records"]
        if not records:
            payload.setdefault("diagnostics", []).append("selector_matched_zero")

    if detail == "summary":
        return ok(payload)

    total = len(records)
    offset = 0
    if cursor:
        try:
            offset = decode_performance_cursor(
                cursor,
                watermark=snapshot.watermark,
                session_ref=snapshot.session_ref,
                history_view=snapshot.history_view,
                detail=detail,
                turn_id=turn_id,
                message_id=message_id,
            )
        except ValueError as exc:
            if str(exc) == "stale":
                raise ApiError(
                    "stale_cursor",
                    "performance page cursor is stale; source changed, fetch a fresh page",
                    EXIT_ERROR,
                    next_commands=[f"sesskit performance {ref}"],
                ) from exc
            raise ApiError("usage_error", "malformed performance page cursor", EXIT_USAGE) from exc
    if offset > total:
        raise ApiError("usage_error", "cursor offset beyond available items", EXIT_USAGE)
    page = records[offset : offset + limit]
    has_more = offset + limit < total
    payload["detail"] = {
        "kind": "records",
        "items": [_performance.performance_record_to_dict(record) for record in page],
        "returned": len(page),
        "total": total,
        "has_more": has_more,
        "cursor": (
            encode_performance_cursor(
                watermark=snapshot.watermark or "",
                session_ref=snapshot.session_ref,
                history_view=snapshot.history_view,
                detail=detail,
                turn_id=turn_id,
                message_id=message_id,
                offset=offset + limit,
            )
            if has_more and snapshot.watermark
            else None
        ),
        "read_coverage": {
            "entries_read": snapshot.entries_read,
            "observed": snapshot.observed_operations,
            "records": len(snapshot.records),
        },
        "scope": {"history_view": snapshot.history_view, **selectors},
    }
    return ok(payload)
