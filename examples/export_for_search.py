"""Export one replaceable search document per discovered session as JSONL."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from sesskit import list_adapters, load_session_conversation, session_key
from sesskit.registry import default_registry


def document(session: dict) -> dict:
    messages = load_session_conversation(session, include_errors=True)
    return {
        "id": session_key(session),
        "runtime": session["source"],
        "session_id": session["id"],
        "activity_time": session["mtime"],
        "message_count": len(messages),
        "text": "\n\n".join(f"{message.role}: {message.text}" for message in messages),
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

    failed = 0
    for session in sessions:
        try:
            payload = document(session)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            failed += 1
            print(json.dumps({
                "error": "load_failed", "runtime": args.runtime,
                "session_id": session["id"], "exception_type": type(exc).__name__,
            }), file=sys.stderr)
            continue
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    print(json.dumps({
        "kind": "summary", "runtime": args.runtime, "scanned": len(sessions),
        "exported": len(sessions) - failed, "failed": failed, "limit": args.limit,
        "include_missing_cwd": args.include_missing_cwd,
    }), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
