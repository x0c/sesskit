#!/usr/bin/env python3
"""Parity harness: Rust `sesskit-stats` engine vs the Python reference.

Read-only over histories. Compares, per session:

1. token total parity: Rust ``known_total`` (``--full`` run) vs Python
   ``sesskit.load_usage(session).summary.total_tokens.known_total``.
2. session set: Rust listed (``excluded`` null, non-empty ``activity``) vs
   Python default listing (``include_missing_cwd=False``) with in-window
   ``load_activity`` events.
3. window pruning consistency: ``window_tokens`` pruned vs ``--full``.
4. activity counts (informational): Rust in-window activity count vs Python
   in-window ``load_activity`` event count.

Only stdlib + sesskit. No multiprocessing. Run with::

    ~/Codes/SessKit/.venv/bin/python scripts/parity.py --days 7,30 --out /tmp/parity.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter

ENGINE_RUNTIMES = ("claude", "codex", "opencode", "pi")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="sesskit-stats parity harness")
    parser.add_argument(
        "--bin",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "target",
            "release",
            "sesskit-stats",
        ),
        help="path to the sesskit-stats binary",
    )
    parser.add_argument(
        "--days",
        default="7,30",
        help="comma-separated day windows, e.g. '7,30'",
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=200000,
        help="limit for Python registry scans (must exceed session count)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="write full results JSON here (default: stdout)",
    )
    return parser.parse_args()


def run_engine(
    bin_path: str,
    start: float | None = None,
    end: float | None = None,
    full: bool = False,
    days: int | None = None,
) -> dict:
    cmd = [bin_path, "--sessions"]
    if start is not None and end is not None:
        cmd += ["--since", repr(start), "--until", repr(end)]
    elif days is not None:
        cmd += ["--days", str(days)]
    if full:
        cmd.append("--full")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"engine failed ({proc.returncode}): {proc.stderr[-2000:]}")
    payload = json.loads(proc.stdout)
    return {"cmd": cmd, "payload": payload}


def scan_python(
    include_missing_cwd: bool, limit: int
) -> tuple[dict[tuple[str, str], dict], dict[str, int]]:
    from sesskit.registry import default_registry

    registry = default_registry()
    sessions: dict[tuple[str, str], dict] = {}
    counts: dict[str, int] = {}
    for runtime in ENGINE_RUNTIMES:
        parser = registry.get(runtime)
        scanned = parser.scan_sessions(limit, include_missing_cwd=include_missing_cwd)
        counts[runtime] = len(scanned)
        for info in scanned:
            sessions[(runtime, str(info["id"]))] = dict(info)
    return sessions, counts


def python_usage(session: dict) -> dict:
    import sesskit

    try:
        snapshot = sesskit.load_usage(session)
    except Exception as exc:  # noqa: BLE001 - record, never crash the harness
        return {"known": "ERROR", "state": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    summary = snapshot.summary
    known = summary.total_tokens.known_total if summary is not None else None
    return {"known": known, "state": snapshot.state, "error": None}


def python_activity(session: dict) -> dict:
    import sesskit

    try:
        snapshot = sesskit.load_activity(session)
    except Exception as exc:  # noqa: BLE001 - record, never crash the harness
        return {"ts": "ERROR", "state": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    stamps = [event.ts for event in snapshot.events if event.ts is not None]
    return {"ts": stamps, "state": snapshot.state, "error": None}


def in_window(stamps: list[float], start: float, end: float) -> list[float]:
    return [ts for ts in stamps if start <= ts <= end]


def cwd_exists(cwd: str | None) -> bool | None:
    if not cwd:
        return None
    try:
        return os.path.isdir(cwd)
    except OSError:
        return None


def check_token_parity(
    rust_detail: list[dict],
    py_full: dict[tuple[str, str], dict],
    usage_cache: dict[tuple[str, str], dict],
) -> dict:
    mismatches: list[dict] = []
    matched = 0
    unmatched: list[dict] = []
    for entry in rust_detail:
        key = (str(entry["runtime"]), str(entry["id"]))
        rust_known = entry.get("known_total")
        session = py_full.get(key)
        if session is None:
            unmatched.append(
                {
                    "runtime": key[0],
                    "id": key[1],
                    "rust_known": rust_known,
                    "rust_excluded": entry.get("excluded"),
                    "reason_guess": (
                        "subagent/internal drop (Python scan excludes these by design)"
                        if entry.get("excluded") == "internal_agent"
                        else "absent from Python scan despite include_missing_cwd=True"
                    ),
                }
            )
            continue
        if key not in usage_cache:
            usage_cache[key] = python_usage(session)
        observed = usage_cache[key]
        py_known = observed["known"]
        if rust_known != py_known:
            delta = None
            if isinstance(rust_known, int) and isinstance(py_known, int):
                delta = rust_known - py_known
            mismatches.append(
                {
                    "runtime": key[0],
                    "id": key[1],
                    "rust": rust_known,
                    "python": py_known,
                    "delta": delta,
                    "py_state": observed["state"],
                    "py_error": observed["error"],
                    "rust_excluded": entry.get("excluded"),
                }
            )
        else:
            matched += 1
    mismatches.sort(key=lambda m: (m["runtime"], m["id"]))
    unmatched.sort(key=lambda m: (m["runtime"], m["id"]))
    return {
        "compared": matched + len(mismatches),
        "match": matched,
        "mismatches": mismatches,
        "unmatched": unmatched,
    }


def check_session_set(
    rust_detail: list[dict],
    py_default: dict[tuple[str, str], dict],
    py_full: dict[tuple[str, str], dict],
    activity_cache: dict[tuple[str, str], dict],
    start: float,
    end: float,
) -> dict:
    rust_listed = {
        (str(e["runtime"]), str(e["id"]))
        for e in rust_detail
        if e.get("excluded") is None and e.get("activity")
    }
    rust_by_key = {(str(e["runtime"]), str(e["id"])): e for e in rust_detail}
    py_listed: set[tuple[str, str]] = set()
    py_window_counts: dict[tuple[str, str], int] = {}
    for key, session in py_default.items():
        if key not in activity_cache:
            activity_cache[key] = python_activity(session)
        observed = activity_cache[key]
        stamps = observed["ts"]
        count = len(in_window(stamps, start, end)) if isinstance(stamps, list) else -1
        py_window_counts[key] = count
        if count > 0:
            py_listed.add(key)
    rust_only: list[dict] = []
    for key in sorted(rust_listed - py_listed):
        session_full = py_full.get(key)
        rust_entry = rust_by_key[key]
        if session_full is None:
            reason = "absent from Python scan entirely (scan filter: internal/title-noise/empty?)"
        elif key not in py_default:
            reason = "dropped by Python default listing (missing/deleted cwd)"
            try:
                if not cwd_exists(rust_entry.get("cwd")):
                    reason += "; cwd missing on disk"
            except OSError:
                pass
        else:
            reason = "in Python default listing but no in-window load_activity events"
        rust_only.append(
            {
                "runtime": key[0],
                "id": key[1],
                "rust_activity_n": len(rust_entry.get("activity") or []),
                "py_window_events": py_window_counts.get(key),
                "reason_guess": reason,
            }
        )
    py_only: list[dict] = []
    for key in sorted(py_listed - rust_listed):
        rust_entry = rust_by_key.get(key)
        session = py_default[key]
        detail: dict = {
            "runtime": key[0],
            "id": key[1],
            "py_window_events": py_window_counts.get(key),
            "py_path": str(session.get("path") or ""),
            "py_cwd": str(session.get("cwd") or ""),
        }
        if rust_entry is None:
            try:
                mtime = os.path.getmtime(str(session.get("path") or ""))
            except OSError:
                mtime = None
            detail["reason_guess"] = (
                "absent from Rust output entirely (discover mtime filter?)"
            )
            detail["py_file_mtime"] = mtime
            detail["window_start"] = start
        elif rust_entry.get("excluded") is not None:
            detail["reason_guess"] = f"Rust excluded: {rust_entry.get('excluded')}"
        else:
            detail["reason_guess"] = "Rust read it but found no in-window activity"
            detail["rust_activity_n"] = len(rust_entry.get("activity") or [])
        py_only.append(detail)
    return {
        "rust_listed": len(rust_listed),
        "python_listed": len(py_listed),
        "intersection": len(rust_listed & py_listed),
        "rust_only": rust_only,
        "python_only": py_only,
    }


def check_pruning(pruned_detail: list[dict], full_detail: list[dict]) -> dict:
    pruned = {(str(e["runtime"]), str(e["id"])): e for e in pruned_detail}
    full = {(str(e["runtime"]), str(e["id"])): e for e in full_detail}
    diffs: list[dict] = []
    for key in sorted(set(pruned) & set(full)):
        p_entry, f_entry = pruned[key], full[key]
        if p_entry.get("window_tokens") != f_entry.get("window_tokens") or len(
            p_entry.get("activity") or []
        ) != len(f_entry.get("activity") or []):
            diffs.append(
                {
                    "runtime": key[0],
                    "id": key[1],
                    "pruned_window_tokens": p_entry.get("window_tokens"),
                    "full_window_tokens": f_entry.get("window_tokens"),
                    "delta_tokens": (
                        (p_entry.get("window_tokens") or 0)
                        - (f_entry.get("window_tokens") or 0)
                    ),
                    "pruned_activity_n": len(p_entry.get("activity") or []),
                    "full_activity_n": len(f_entry.get("activity") or []),
                    "pruned_untimed": p_entry.get("untimed_tokens"),
                    "full_untimed": f_entry.get("untimed_tokens"),
                }
            )
    only_pruned = sorted(set(pruned) - set(full))
    only_full = sorted(set(full) - set(pruned))
    return {
        "compared": len(set(pruned) & set(full)),
        "diffs": diffs,
        "only_pruned": [{"runtime": r, "id": i} for r, i in only_pruned],
        "only_full": [{"runtime": r, "id": i} for r, i in only_full],
    }


def check_activity_counts(
    rust_detail: list[dict],
    py_default: dict[tuple[str, str], dict],
    activity_cache: dict[tuple[str, str], dict],
    start: float,
    end: float,
) -> dict:
    rust_counts = {
        (str(e["runtime"]), str(e["id"])): len(e.get("activity") or [])
        for e in rust_detail
        if e.get("excluded") is None
    }
    per_runtime: dict[str, dict] = {}
    outliers: list[dict] = []
    for key, rust_n in sorted(rust_counts.items()):
        session = py_default.get(key)
        if session is None:
            continue
        if key not in activity_cache:
            activity_cache[key] = python_activity(session)
        observed = activity_cache[key]
        stamps = observed["ts"]
        if not isinstance(stamps, list):
            continue
        py_n = len(in_window(stamps, start, end))
        bucket = per_runtime.setdefault(
            key[0],
            {
                "n_both": 0,
                "exact": 0,
                "close": 0,
                "rust_gt": 0,
                "py_gt": 0,
                "rust_zero_py_nonzero": 0,
                "py_zero_rust_nonzero": 0,
            },
        )
        bucket["n_both"] += 1
        if rust_n == py_n:
            bucket["exact"] += 1
        if rust_n == 0 and py_n > 0:
            bucket["rust_zero_py_nonzero"] += 1
        if py_n == 0 and rust_n > 0:
            bucket["py_zero_rust_nonzero"] += 1
        if rust_n > py_n:
            bucket["rust_gt"] += 1
        elif py_n > rust_n:
            bucket["py_gt"] += 1
        else:
            bucket["close"] += 1
        ratio: float | None
        if py_n == 0:
            ratio = None if rust_n == 0 else float("inf")
        else:
            ratio = rust_n / py_n
        is_outlier = (ratio is None) or (ratio == float("inf") and rust_n > 5) or (
            isinstance(ratio, float) and (ratio < 0.5 or ratio > 2.0)
        )
        if is_outlier and not (rust_n == 0 and py_n == 0):
            outliers.append(
                {
                    "runtime": key[0],
                    "id": key[1],
                    "rust_n": rust_n,
                    "py_n": py_n,
                    "ratio": ratio,
                    "py_state": observed["state"],
                }
            )
    outliers.sort(
        key=lambda o: (
            o["runtime"],
            -(abs(o["ratio"] or 0) if o["ratio"] not in (None, float("inf")) else 1e9),
        )
    )
    return {"per_runtime": per_runtime, "outliers": outliers}


def main() -> int:
    args = parse_args()
    days_list = [int(part) for part in args.days.split(",") if part.strip()]
    t0 = time.time()
    commands: list[list[str]] = []
    windows: dict[str, dict] = {}
    runs: dict[str, dict] = {}
    # Freeze one window per days value from the engine itself, then reuse it
    # via --since/--until so pruned/full runs are directly comparable.
    for days in days_list:
        probe = run_engine(args.bin, days=days)
        commands.append(probe["cmd"])
        window = probe["payload"]["window"]
        start, end = float(window["start"]), float(window["end"])
        windows[str(days)] = {"start": start, "end": end}
        log(f"days={days} window=[{start:.0f},{end:.0f}]")
        full = run_engine(args.bin, start, end, True)
        commands.append(full["cmd"])
        pruned = run_engine(args.bin, start, end, False)
        commands.append(pruned["cmd"])
        runs[str(days)] = {"full": full["payload"], "pruned": pruned["payload"]}
        log(
            f"days={days} full: read={full['payload']['sessions_read']} "
            f"listed={full['payload']['sessions']} "
            f"elapsed={full['payload']['elapsed_s']:.2f}s; "
            f"pruned: read={pruned['payload']['sessions_read']} "
            f"listed={pruned['payload']['sessions']} "
            f"elapsed={pruned['payload']['elapsed_s']:.2f}s"
        )
    log("scanning Python registry (include_missing_cwd=True) ...")
    py_full, full_counts = scan_python(True, args.scan_limit)
    log(f"python full scan: {full_counts} total={len(py_full)}")
    log("scanning Python registry (default listing) ...")
    py_default, default_counts = scan_python(False, args.scan_limit)
    log(f"python default scan: {default_counts} total={len(py_default)}")
    truncated = {
        rt: count for rt, count in {**full_counts, **default_counts}.items()
        if count >= args.scan_limit
    }
    usage_cache: dict[tuple[str, str], dict] = {}
    activity_cache: dict[tuple[str, str], dict] = {}
    load_errors: list[dict] = []
    checks: dict = {}
    for days in days_list:
        tag = str(days)
        window = windows[tag]
        start, end = window["start"], window["end"]
        full_detail = runs[tag]["full"]["detail"]
        pruned_detail = runs[tag]["pruned"]["detail"]
        log(f"check1 token parity days={days} over {len(full_detail)} rust sessions ...")
        c1 = check_token_parity(full_detail, py_full, usage_cache)
        log(f"  compared={c1['compared']} match={c1['match']} "
            f"mismatch={len(c1['mismatches'])} unmatched={len(c1['unmatched'])}")
        log(f"check2 session set days={days} ...")
        c2 = check_session_set(full_detail, py_default, py_full, activity_cache, start, end)
        log(f"  rust={c2['rust_listed']} python={c2['python_listed']} "
            f"both={c2['intersection']} rust_only={len(c2['rust_only'])} "
            f"py_only={len(c2['python_only'])}")
        log(f"check3 pruning days={days} ...")
        c3 = check_pruning(pruned_detail, full_detail)
        log(f"  compared={c3['compared']} diffs={len(c3['diffs'])}")
        log(f"check4 activity counts days={days} ...")
        c4 = check_activity_counts(full_detail, py_default, activity_cache, start, end)
        log(f"  per_runtime={c4['per_runtime']} outliers={len(c4['outliers'])}")
        checks[tag] = {"c1": c1, "c2": c2, "c3": c3, "c4": c4}
    for key, observed in {**usage_cache, **activity_cache}.items():
        if observed.get("error"):
            load_errors.append({"runtime": key[0], "id": key[1], "error": observed["error"]})
    summary_counts: dict = {}
    for tag in checks:
        c1, c2, c3, c4 = (checks[tag][k] for k in ("c1", "c2", "c3", "c4"))
        by_runtime = Counter(m["runtime"] for m in c1["mismatches"])
        by_runtime_un = Counter(m["runtime"] for m in c1["unmatched"])
        summary_counts[tag] = {
            "c1": {
                "compared": c1["compared"],
                "match": c1["match"],
                "mismatch": len(c1["mismatches"]),
                "mismatch_by_runtime": dict(by_runtime),
                "unmatched": len(c1["unmatched"]),
                "unmatched_by_runtime": dict(by_runtime_un),
            },
            "c2": {
                "rust": c2["rust_listed"],
                "python": c2["python_listed"],
                "both": c2["intersection"],
                "rust_only": len(c2["rust_only"]),
                "python_only": len(c2["python_only"]),
            },
            "c3": {"compared": c3["compared"], "diffs": len(c3["diffs"])},
            "c4": c4["per_runtime"],
        }
    results = {
        "engine": args.bin,
        "scan_limit": args.scan_limit,
        "scan_truncated": truncated,
        "commands": commands,
        "windows": windows,
        "python_scan_counts": {"full": full_counts, "default": default_counts},
        "summary_counts": summary_counts,
        "checks": checks,
        "python_load_errors": load_errors,
        "elapsed_s": time.time() - t0,
    }
    text = json.dumps(results, indent=1, sort_keys=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        log(f"wrote {args.out} ({len(text)} bytes)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
