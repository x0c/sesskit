"""Reference time aggregates over activity timestamps (W2, pure functions).

Shared semantics with ``crates/sesskit-stats/src/aggregate.rs``: consecutive
activity at most ``ACTIVE_GAP`` seconds apart merges into one active interval,
each interval extends ``EVENT_TAIL`` seconds past its last record, and
intervals that only touch are not concurrent. Consumers call these instead of
re-implementing the rules so every card and report agrees.

Pure functions only: no history reads, no I/O, no third-party dependencies.
Activity timestamps are passed in by the caller (record rows inside the
window, cf. docs/NATIVE_STATS_ENGINE_DESIGN.md §7; counts may differ from
typed activity events while times agree). Never sums performance durations.
Missing stays missing (``None``), never a fabricated zero.
"""

from __future__ import annotations

ACTIVE_GAP: float = 300.0
EVENT_TAIL: float = 60.0

#: Group key for sessions whose ``cwd`` is empty or absent.
MISSING_CWD_KEY = "__missing_cwd__"


def active_intervals(times: list[float]) -> list[tuple[float, float]]:
    """Merge activity timestamps into active intervals.

    Mirrors ``aggregate::active_intervals``: the input is treated as unordered
    and sorted ascending first (the Rust caller passes ascending slices; the
    reference sorts so callers need not). Consecutive records at most
    ``ACTIVE_GAP`` apart merge into one interval; each interval then extends
    ``EVENT_TAIL`` past its last record so a lone record still occupies time.
    Empty input yields ``[]``. The input list is never mutated.
    """
    ordered = sorted(times)
    merged: list[list[float]] = []
    for t in ordered:
        if merged and t - merged[-1][1] <= ACTIVE_GAP:
            merged[-1][1] = t
        else:
            merged.append([t, t])
    return [(start, end + EVENT_TAIL) for start, end in merged]


def clip_interval_to_window(
    iv: tuple[float, float], start: float, end: float
) -> tuple[float, float] | None:
    """Clip an interval to ``[start, end]``; ``None`` when disjoint.

    Touching the window edge (zero-width intersection) is not an overlap, the
    same touch rule ``aggregate::peak_concurrency`` uses. Degenerate inputs
    (empty interval, inverted window) also yield ``None``.
    """
    lo = max(iv[0], start)
    hi = min(iv[1], end)
    if hi > lo:
        return (lo, hi)
    return None


def summarize_session(
    runtime: str,
    id: str,
    short_id: str,
    cwd: str,
    cwd_display: str,
    activity: list[float],
    start: float,
    end: float,
    excluded: str | None = None,
    coverage: str = "complete",
) -> dict:
    """Summarize one session's in-window active time.

    Only activity with ``start <= t <= end`` (cf. ``Window::contains``) seeds
    intervals; out-of-window records are dropped, never clipped in. ``active``
    time is ``sum`` of each interval clipped to ``[start, end]``. A live
    session is truncated by the caller, which passes an ``end`` already
    bounded by now. Sessions with no in-window activity report ``0.0``
    seconds with ``first_at``/``last_at`` as ``None`` (missing stays
    missing).
    """
    in_window = [t for t in activity if start <= t <= end]
    intervals = active_intervals(in_window)
    clipped = [c for iv in intervals for c in [clip_interval_to_window(iv, start, end)] if c is not None]
    return {
        "runtime": runtime,
        "id": id,
        "short_id": short_id,
        "cwd": cwd,
        "cwd_display": cwd_display,
        "activity_count": len(in_window),
        "interval_count": len(intervals),
        "active_seconds": sum(hi - lo for lo, hi in clipped),
        "first_at": min(in_window) if in_window else None,
        "last_at": max(in_window) if in_window else None,
        "excluded": excluded,
        "coverage": coverage,
    }


def _peak_concurrency(intervals: list[tuple[float, float]]) -> tuple[int, float | None]:
    """Maximum intervals open at one instant and its first instant.

    Mirrors ``aggregate::peak_concurrency`` exactly: end points sort before
    start points at the same instant, so touching intervals are not
    concurrent. Degenerate (empty) intervals are skipped; with no intervals
    the result is ``(0, None)``.
    """
    points: list[tuple[float, int]] = []
    for a, b in intervals:
        if b > a:
            points.append((a, 1))
            points.append((b, -1))
    points.sort(key=lambda p: (p[0], p[1]))
    cur = 0
    peak = 0
    at: float | None = None
    for t, delta in points:
        cur += delta
        if cur > peak:
            peak = cur
            at = t
    return (peak, at)


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    """Total time covered by at least one interval (touching merges freely)."""
    ordered = sorted((a, b) for a, b in intervals if b > a)
    total = 0.0
    cur_start: float | None = None
    cur_end = 0.0
    for a, b in ordered:
        if cur_start is None:
            cur_start, cur_end = a, b
        elif a <= cur_end:
            cur_end = max(cur_end, b)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = a, b
    if cur_start is not None:
        total += cur_end - cur_start
    return total


def rollup_projects(sessions: list[dict]) -> list[dict]:
    """Roll up session summaries by full-path ``cwd``.

    Groups by the ``cwd`` full path; empty or absent ``cwd`` groups under
    ``__missing_cwd__``. ``active_seconds_sum`` adds the members'
    ``active_seconds``; ``active_seconds_union`` merges the clipped intervals
    members carry under the optional ``"intervals"`` key (``(start, end)``
    pairs, e.g. clipped via :func:`clip_interval_to_window` by the caller),
    so parallel sessions count once in the union but fully in the sum.
    ``peak_concurrency``/``peak_at`` sweep those intervals with the Rust
    end-before-start order (touch is not concurrent); with no intervals they
    are ``0``/``None`` (missing stays missing).
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for s in sessions:
        cwd = s.get("cwd") or MISSING_CWD_KEY
        if cwd not in groups:
            groups[cwd] = {
                "cwd": cwd,
                "cwd_display": s.get("cwd_display"),
                "session_count": 0,
                "runtimes": [],
                "active_seconds_sum": 0.0,
                "intervals": [],
            }
            order.append(cwd)
        g = groups[cwd]
        g["session_count"] += 1
        runtime = s.get("runtime")
        if runtime is not None and runtime not in g["runtimes"]:
            g["runtimes"].append(runtime)
        active = s.get("active_seconds")
        if isinstance(active, (int, float)):
            g["active_seconds_sum"] += active
        for iv in s.get("intervals") or []:
            try:
                a, b = float(iv[0]), float(iv[1])
            except (TypeError, ValueError, IndexError):
                continue
            if b > a:
                g["intervals"].append((a, b))
    out: list[dict] = []
    for cwd in sorted(order):
        g = groups[cwd]
        peak, at = _peak_concurrency(g["intervals"])
        out.append({
            "cwd": g["cwd"],
            "cwd_display": g["cwd_display"],
            "session_count": g["session_count"],
            "runtimes": sorted(g["runtimes"]),
            "active_seconds_sum": g["active_seconds_sum"],
            "active_seconds_union": _union_seconds(g["intervals"]),
            "peak_concurrency": peak,
            "peak_at": at,
        })
    return out
