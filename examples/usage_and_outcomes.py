"""Inspect native usage observations and historical outcomes, without summing billing."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict

from sesskit import get_adapter, list_adapters, load_activity
from sesskit.registry import default_registry


def inspect_session(session: dict) -> dict:
    capabilities = get_adapter(session["source"]).capabilities
    snapshot = load_activity(session)
    observations = [
        {
            "seq": event.seq, "message_id": event.message_id,
            "turn_id": event.turn_id, "usage": asdict(event.usage),
        }
        for event in snapshot.events if event.usage is not None
    ]
    return {
        "session_id": session["id"],
        "state": snapshot.state,
        "outcome": snapshot.outcome.status,
        "outcome_evidence": asdict(snapshot.outcome.evidence),
        "terminal_error_kind": snapshot.outcome.error.kind if snapshot.outcome.error else None,
        "usage_state": (
            "unsupported" if not capabilities.usage_data
            else "recorded" if observations else "not_recorded"
        ),
        # Usage can repeat across text/thinking blocks and messages in one turn.
        # Keep the native observations and grouping ids; never sum them blindly.
        "usage_observations": observations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, choices=[a.id for a in list_adapters()])
    parser.add_argument("--limit", type=int, default=5, help="maximum discovered sessions (not a complete archive)")
    parser.add_argument("--include-missing-cwd", action="store_true")
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")

    try:
        sessions = default_registry().get(args.runtime).scan_sessions(
            limit=args.limit, include_missing_cwd=args.include_missing_cwd,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": "scan_failed", "exception_type": type(exc).__name__}), file=sys.stderr)
        return 1

    reports = []
    for session in sessions:
        try:
            reports.append(inspect_session(session))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            reports.append({
                "session_id": session["id"], "state": "unavailable",
                "exception_type": type(exc).__name__,
            })
    loaded = [row for row in reports if row["state"] in {"available", "empty"}]
    failed = sum(row["state"] == "unavailable" for row in reports)
    print(json.dumps({
        "runtime": args.runtime, "limit": args.limit,
        "include_missing_cwd": args.include_missing_cwd,
        "scanned": len(sessions), "loaded": len(loaded),
        "unsupported": sum(row["state"] == "unsupported" for row in reports),
        "failed": failed,
        "outcomes": dict(Counter(row["outcome"] for row in loaded)),
        "sessions": reports,
    }, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
