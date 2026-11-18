"""SessKit stats command: windowed active-time ledger per session or project.

This module owns CLI command logic only (``cmd_stats`` plus the ``stats_args``
registration helper consumed by ``sesskit.cli.COMMANDS``). All business
aggregation lives in :mod:`sesskit.stats` behind the locked W2 API
(``ACTIVE_GAP``, ``EVENT_TAIL``, ``active_intervals``,
``clip_interval_to_window``, ``summarize_session``, ``rollup_projects``);
this file only orchestrates discovery, filtering, activity loading, and
composition of those primitives.
"""

from __future__ import annotations

import math
import time
from typing import Any

from sesskit.envelope import EXIT_USAGE, ApiError, ok
from sesskit.models import format_message_time
from sesskit.stats import (
    ACTIVE_GAP,
    EVENT_TAIL,
    active_intervals,
    clip_interval_to_window,
    rollup_projects,
    summarize_session,
)

STATS_SCHEMA_ID = "sesskit.stats/v1"

#: Fixed per-runtime scan depth (``--limit`` keeps the usage-command meaning:
#: detail page size, which v1 has no use for — see ``cmd_stats`` validation).
_STATS_SCAN_DEPTH = 200

#: Default window length in seconds when ``--since`` is omitted (7 days).
_STATS_DEFAULT_SINCE_SECONDS = 7 * 24 * 3600


def stats_args() -> list[dict]:
    """Return the ``stats`` arg-spec list for the ``cli.COMMANDS`` entry.

    Pure data, no aggregation: ``cli.py`` embeds the result in its single
    ``stats`` entry so the whole flag surface of this command lives here.
    ``--limit``/``--cursor`` keep the usage-command meaning (detail paging);
    v1 has no detail pages, so passing them is a usage error (same rule as
    ``cmd_usage``: only valid with a non-summary ``--detail``).
    """
    return [
        {
            "flags": ["--since"],
            "kwargs": {
                "default": "7d",
                "help": "window start (YYYY-MM-DD, 'YYYY-MM-DD HH:MM', 7d/24h/30m, or unix timestamp; default 7d ago)",
            },
        },
        {
            "flags": ["--until"],
            "kwargs": {
                "default": None,
                "help": "window end (same formats as --since; default now)",
            },
        },
        {
            "flags": ["--by"],
            "kwargs": {
                "choices": ["session", "project"],
                "default": "session",
                "help": "ledger rows per session or grouped by full project cwd",
            },
        },
        {
            "flags": ["--top"],
            "kwargs": {
                "type": int,
                "default": None,
                "help": "return at most N rows after sorting by active time",
            },
        },
        {"flags": ["--cwd"], "kwargs": {"help": "substring filter on session cwd (same as list)"}},
        {
            "flags": ["--detail"],
            "kwargs": {
                "choices": ["summary"],
                "default": "summary",
                "help": "v1 supports summary only",
            },
        },
        {
            "flags": ["--limit"],
            "kwargs": {
                "type": int,
                "default": None,
                "help": "detail page size (detail mode only; v1 has no detail pages)",
            },
        },
        {
            "flags": ["--cursor"],
            "kwargs": {"help": "opaque page cursor (detail mode only; v1 has no detail pages)"},
        },
        {
            "flags": ["--include-missing-cwd"],
            "kwargs": {
                "action": "store_true",
                "help": "keep sessions whose project directory was deleted (archive mode; default hides them)",
            },
        },
        {"flags": ["--compact"], "kwargs": {"action": "store_true"}},
        {
            "flags": ["--json"],
            "kwargs": {"action": "store_true", "help": "single-line JSON output (same as --compact)"},
        },
    ]


def _event_ts(event: Any) -> float | None:
    """One activity event's unix timestamp, or ``None`` when unusable."""
    ts = getattr(event, "ts", None)
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if not math.isfinite(ts):
        return None
    return float(ts)


def _clipped_intervals(stamps: list[float], start: float, end: float) -> list[tuple[float, float]]:
    """Window-clipped active intervals for in-window stamps (W2 caller pattern).

    Only ``start <= t <= end`` records seed intervals (out-of-window records
    are dropped, never clipped in); each merged interval is then clipped to
    ``[start, end]``. Touching the window edge is not an overlap.
    """
    in_window = [t for t in stamps if start <= t <= end]
    clipped: list[tuple[float, float]] = []
    for interval in active_intervals(in_window):
        piece = clip_interval_to_window(interval, start, end)
        if piece is not None:
            clipped.append(piece)
    return clipped


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    """Total time covered by at least one interval (touching merges freely).

    Generic interval math over already-clipped ranges (composition detail for
    ``totals.active_seconds_union``); mirrors the private ``stats`` helper,
    which is not part of the locked W2 names — candidate to move into
    ``sesskit.stats`` on a later pass.
    """
    ordered = sorted((a, b) for a, b in intervals if b > a)
    total = 0.0
    current_start: float | None = None
    current_end = 0.0
    for a, b in ordered:
        if current_start is None:
            current_start, current_end = a, b
        elif a <= current_end:
            current_end = max(current_end, b)
        else:
            total += current_end - current_start
            current_start, current_end = a, b
    if current_start is not None:
        total += current_end - current_start
    return total


def cmd_stats(args, registry) -> dict:
    """Show a windowed active-time ledger per session or per project.

    Read-only. Discovers sessions via ``registry.scan_all`` (default drops
    missing-``cwd``/ephemeral sessions; scanners own that filtering), keeps
    sessions with at least one ``load_activity`` event timestamp inside
    ``[since, until]`` (live sessions truncate the effective end at now),
    aggregates through the locked ``sesskit.stats`` API, and emits bounded
    numbers/identifiers/evidence only. Unreadable histories go to
    ``load_errors`` with exit 0 (export style), never a failed envelope;
    an empty window is a successful empty list. Totals cover the full window
    set, before any ``--top`` trim (same caliber as ``cmd_list`` counts).
    """
    from sesskit import cli as _cli

    detail = getattr(args, "detail", "summary") or "summary"
    if detail != "summary":
        raise ApiError("usage_error", "--detail v1 supports summary only", EXIT_USAGE)
    if getattr(args, "limit", None) is not None or getattr(args, "cursor", None):
        raise ApiError(
            "usage_error",
            "--limit/--cursor are only valid with --detail <kind>; v1 supports summary only",
            EXIT_USAGE,
        )
    by = getattr(args, "by", "session") or "session"
    if by not in {"session", "project"}:
        raise ApiError("usage_error", "--by must be session or project", EXIT_USAGE)
    top = getattr(args, "top", None)
    if top is not None and top < 0:
        raise ApiError("usage_error", "--top must be zero or greater", EXIT_USAGE)

    now = time.time()
    since_raw = getattr(args, "since", None)
    until_raw = getattr(args, "until", None)
    since = _cli._parse_time_bound(since_raw, is_until=False) if since_raw else now - _STATS_DEFAULT_SINCE_SECONDS
    until = _cli._parse_time_bound(until_raw, is_until=True) if until_raw else now
    if since > until:
        raise ApiError("usage_error", "since is later than until", EXIT_USAGE)

    scanned = registry.scan_all(_STATS_SCAN_DEPTH, include_missing_cwd=_cli._include_missing_cwd(args))
    cwd_filter = getattr(args, "cwd", None)

    summaries: list[dict] = []
    clipped_all: list[tuple[float, float]] = []
    load_errors: list[dict] = []
    for runtime in registry:
        for session in scanned.get(runtime.id, []):
            if cwd_filter and cwd_filter.lower() not in str(session.get("cwd") or "").lower():
                continue
            try:
                adapter = getattr(runtime, "adapter", None)
                if adapter is None:
                    raise RuntimeError("no runtime adapter")
                payload = session if session.get("source") == runtime.id else {**session, "source": runtime.id}
                snapshot = adapter.load_activity(payload)
            except Exception as exc:  # noqa: BLE001 — one bad history never fails the ledger
                load_errors.append(
                    {
                        "id": str(session.get("id") or ""),
                        "runtime": runtime.id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if getattr(snapshot, "state", None) == "unavailable":
                load_errors.append(
                    {
                        "id": str(session.get("id") or ""),
                        "runtime": runtime.id,
                        "error": "history unreadable",
                    }
                )
                continue
            events = getattr(snapshot, "events", None) or ()
            stamps = sorted(ts for ts in (_event_ts(e) for e in events) if ts is not None)
            live = bool(session.get("live"))
            # Live truncation: the caller bounds end by now before aggregating.
            end = min(until, now) if live else until
            summary = summarize_session(
                str(session.get("source") or ""),
                str(session.get("id") or ""),
                str(session.get("short_id") or ""),
                str(session.get("cwd") or ""),
                str(session.get("cwd_display") or ""),
                stamps,
                since,
                end,
            )
            if int(summary.get("activity_count") or 0) == 0:
                continue
            clipped = _clipped_intervals(stamps, since, end)
            if not clipped:
                continue
            summary["live"] = live
            # Caller-carried clipped intervals feed rollup union/peak only;
            # stripped from session-mode output below (bounded shape stays).
            summary["intervals"] = clipped
            summaries.append(summary)
            clipped_all.extend(clipped)

    summaries.sort(key=lambda e: (-float(e.get("active_seconds") or 0.0), str(e.get("id") or "")))
    if by == "project":
        rows = rollup_projects(summaries)
        rows.sort(key=lambda g: (-float(g.get("active_seconds_sum") or 0.0), str(g.get("cwd") or "")))
    else:
        rows = [{k: v for k, v in entry.items() if k != "intervals"} for entry in summaries]
    rows = _cli._apply_top(rows, top)

    data: dict[str, Any] = {
        "schema": STATS_SCHEMA_ID,
        "backend": "real",
        "window": {
            "since": since,
            "until": until,
            "since_display": format_message_time(since),
            "until_display": format_message_time(until),
        },
        "params": {"active_gap": ACTIVE_GAP, "event_tail": EVENT_TAIL},
        "by": by,
        "sessions" if by == "session" else "projects": rows,
        "totals": {
            "session_count": len(summaries),
            "active_seconds_sum": sum(float(e.get("active_seconds") or 0.0) for e in summaries),
            "active_seconds_union": _union_seconds(clipped_all),
        },
    }
    if load_errors:
        data["load_errors"] = load_errors
    if registry.last_scan_errors:
        data["scan_errors"] = registry.last_scan_errors
    return ok(data)
