"""Inspect preserved native timing/rate evidence for local sessions (read-only).

Prints a bounded performance summary per session: coverage, record/rate
counts, interval kinds and the latest record. Never averages unlike
intervals, never sums overlapping durations, and never invents a rate.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter

from sesskit import list_adapters
from sesskit.registry import default_registry


def inspect_session(session: dict) -> dict:
    from sesskit import performance as _performance

    snapshot = _performance.load_performance(session)
    summary = _performance.performance_summary(snapshot)
    return {
        "session_id": session["id"],
        "state": snapshot.state,
        "coverage": summary["coverage"] if isinstance(summary, dict) else summary.coverage,
        "summary": summary if isinstance(summary, dict) else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime", choices=[a.id for a in list_adapters()], help="runtime to scan (or pass --ref for one session)"
    )
    parser.add_argument("--ref", default=None, help="single session reference: id, prefix, or runtime:id")
    parser.add_argument("--limit", type=int, default=5, help="maximum discovered sessions (not a complete archive)")
    parser.add_argument("--include-missing-cwd", action="store_true")
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if not args.runtime and not args.ref:
        parser.error("pass --runtime and/or --ref")
    try:
        from sesskit import performance as _performance  # noqa: F401
    except ImportError:
        print(json.dumps({"error": "dependency_not_ready", "detail": "sesskit.performance has not landed yet"}))
        return 1

    try:
        registry = default_registry()
        if args.ref:
            from sesskit.cli import resolve_ref

            sessions = [resolve_ref(registry, args.ref, 200, include_missing_cwd=args.include_missing_cwd)]
        else:
            assert args.runtime is not None
            sessions = registry.get(args.runtime).scan_sessions(
                limit=args.limit, include_missing_cwd=args.include_missing_cwd
            )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": "scan_failed", "exception_type": type(exc).__name__}), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- resolve_ref ApiError (missing/ambiguous)
        print(json.dumps({"error": getattr(exc, "code", "error"), "message": str(exc)}), file=sys.stderr)
        return int(getattr(exc, "exit_code", 1) or 1)

    reports = []
    for session in sessions:
        try:
            reports.append(inspect_session(session))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            reports.append(
                {
                    "session_id": session["id"],
                    "state": "unavailable",
                    "exception_type": type(exc).__name__,
                }
            )
    loaded = [row for row in reports if row["state"] in {"available", "empty"}]
    failed = sum(row["state"] == "unavailable" for row in reports)
    print(
        json.dumps(
            {
                "runtime": args.runtime,
                "ref": args.ref,
                "limit": args.limit,
                "include_missing_cwd": args.include_missing_cwd,
                "scanned": len(sessions),
                "loaded": len(loaded),
                "unsupported": sum(row["state"] == "unsupported" for row in reports),
                "failed": failed,
                "coverages": dict(Counter(row.get("coverage") for row in loaded)),
                "sessions": reports,
            },
            ensure_ascii=False,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
