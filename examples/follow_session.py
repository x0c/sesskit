"""Follow one exact session id with a bounded number of incremental polls."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from dataclasses import asdict

from sesskit import IncrementalUnsupported, get_adapter, list_adapters, open_activity_reader, session_key
from sesskit.registry import default_registry


def follow(session: dict, polls: int, interval: float):
    reader = open_activity_reader(session)
    for index in range(polls):
        if index:
            time.sleep(interval)
        result = reader.poll()
        # reset replaces the old generation; events may be only a tail window.
        # page() retrieves older history; late linkage may enrich prior records.
        yield {"session": session_key(session), **asdict(result)}
        if result.state == "unavailable":
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, choices=[a.id for a in list_adapters()])
    parser.add_argument("--session-id", required=True, help="full native id, not a prefix")
    parser.add_argument("--limit", type=int, default=200, help="discovery bound; increase if an old id is not found")
    parser.add_argument("--include-missing-cwd", action="store_true")
    parser.add_argument("--polls", type=int, default=3, help="number of polls; no background process")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    args = parser.parse_args(argv)
    if args.limit <= 0 or args.polls <= 0:
        parser.error("--limit and --polls must be positive")
    if not math.isfinite(args.interval) or args.interval < 0:
        parser.error("--interval must be finite and nonnegative")
    if not get_adapter(args.runtime).capabilities.incremental_reading:
        print(json.dumps({
            "error": "incremental_unsupported", "runtime": args.runtime,
            "hint": "Inspect capabilities; use snapshots only if typed_activity is supported.",
        }), file=sys.stderr)
        return 1

    try:
        sessions = default_registry().get(args.runtime).scan_sessions(
            limit=args.limit, include_missing_cwd=args.include_missing_cwd,
        )
        session = next((s for s in sessions if s["id"] == args.session_id), None)
        if session is None:
            print(json.dumps({
                "error": "session_not_found", "runtime": args.runtime,
                "session_id": args.session_id, "scanned": len(sessions), "limit": args.limit,
                "hint": "Use a full id from sesskit list; increase --limit or use --include-missing-cwd.",
            }), file=sys.stderr)
            return 3
        for payload in follow(session, args.polls, args.interval):
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            if payload["state"] == "unavailable":
                return 1
    except (OSError, RuntimeError, ValueError, sqlite3.Error, IncrementalUnsupported) as exc:
        print(json.dumps({"error": "read_failed", "exception_type": type(exc).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
