"""Deterministic unit tests for the W2 stats reference (pure functions only)."""

import pytest

from sesskit.stats import (
    ACTIVE_GAP,
    EVENT_TAIL,
    active_intervals,
    clip_interval_to_window,
    rollup_projects,
    summarize_session,
)


def test_constants_locked():
    assert ACTIVE_GAP == 300.0
    assert EVENT_TAIL == 60.0


def test_empty_input():
    assert active_intervals([]) == []


def test_gap_boundary_merges_at_300_splits_past_it():
    # Mirrors aggregate.rs gap_boundary_and_tail.
    assert active_intervals([0.0, 300.0]) == [(0.0, 360.0)]
    assert active_intervals([0.0, 300.5]) == [(0.0, 60.0), (300.5, 360.5)]


def test_single_point_gets_tail():
    assert active_intervals([10.0]) == [(10.0, 70.0)]


def test_unsorted_input_sorted_internally_without_mutation():
    src = [300.5, 0.0, 150.0]
    assert active_intervals(src) == [(0.0, 360.5)]
    assert src == [300.5, 0.0, 150.0]


def test_chain_merge_uses_raw_gap_not_extended_end():
    # 0..300 merge, 300..700 gap 400 splits; tail applies after merging.
    assert active_intervals([0.0, 300.0, 700.0]) == [(0.0, 360.0), (700.0, 760.0)]


def test_rust_idle_gap_parity():
    a = active_intervals([0.0, 86400.0])
    b = active_intervals([43200.0, 43260.0])
    assert len(a) == 2
    assert b == [(43200.0, 43320.0)]


def test_clip_inside_partial_outside_touch():
    assert clip_interval_to_window((10.0, 20.0), 0.0, 100.0) == (10.0, 20.0)
    assert clip_interval_to_window((-50.0, 30.0), 0.0, 100.0) == (0.0, 30.0)
    assert clip_interval_to_window((80.0, 200.0), 0.0, 100.0) == (80.0, 100.0)
    assert clip_interval_to_window((-200.0, -100.0), 0.0, 100.0) is None
    assert clip_interval_to_window((150.0, 250.0), 0.0, 100.0) is None
    # Touching the edge is not an overlap.
    assert clip_interval_to_window((100.0, 160.0), 0.0, 100.0) is None
    assert clip_interval_to_window((-60.0, 0.0), 0.0, 100.0) is None
    # Degenerate inputs yield None, never a fabricated span.
    assert clip_interval_to_window((20.0, 20.0), 0.0, 100.0) is None
    assert clip_interval_to_window((10.0, 20.0), 100.0, 0.0) is None


def _summ(**kw):
    base = {
        "runtime": "claude",
        "id": "s1",
        "short_id": "s1",
        "cwd": "/repo/a",
        "cwd_display": "a",
        "activity": [],
        "start": 0.0,
        "end": 1000.0,
    }
    base.update(kw)
    return summarize_session(**base)


def test_summarize_keys_and_window_filter():
    s = _summ(activity=[-50.0, 0.0, 100.0, 2000.0])
    assert sorted(s.keys()) == sorted([
        "runtime", "id", "short_id", "cwd", "cwd_display",
        "activity_count", "interval_count", "active_seconds",
        "first_at", "last_at", "excluded", "coverage",
    ])
    assert s["activity_count"] == 2
    assert s["first_at"] == 0.0
    assert s["last_at"] == 100.0
    # One interval (0,60)+(100->100..160 merged: gap 100 <= 300) clipped fully.
    assert s["interval_count"] == 1
    assert s["active_seconds"] == pytest.approx(160.0)
    assert s["excluded"] is None
    assert s["coverage"] == "complete"


def test_summarize_empty_window_keeps_missing_missing():
    s = _summ(activity=[5000.0])
    assert s["activity_count"] == 0
    assert s["interval_count"] == 0
    assert s["active_seconds"] == 0.0
    assert s["first_at"] is None
    assert s["last_at"] is None


def test_summarize_clips_tail_past_end():
    s = _summ(activity=[990.0], start=0.0, end=1000.0)
    assert s["interval_count"] == 1
    assert s["active_seconds"] == pytest.approx(10.0)


def test_summarize_passthrough_and_live_truncation_by_caller():
    # Live truncation: caller bounds end by now before passing.
    s = _summ(activity=[900.0, 950.0], start=0.0, end=920.0,
              excluded="missing_cwd", coverage="partial-live")
    assert s["activity_count"] == 1
    assert s["first_at"] == 900.0
    assert s["active_seconds"] == pytest.approx(20.0)
    assert s["excluded"] == "missing_cwd"
    assert s["coverage"] == "partial-live"


def _clipped_intervals(activity, start, end):
    out = []
    for iv in active_intervals([t for t in activity if start <= t <= end]):
        c = clip_interval_to_window(iv, start, end)
        if c is not None:
            out.append(c)
    return out


def test_rollup_keys_grouping_and_missing_cwd():
    sessions = [
        _summ(runtime="claude", id="a", short_id="a", cwd="/repo/a",
              cwd_display="a", activity=[10.0]),
        _summ(runtime="codex", id="b", short_id="b", cwd="", cwd_display="",
              activity=[20.0]),
    ]
    rows = rollup_projects(sessions)
    assert {r["cwd"] for r in rows} == {"/repo/a", "__missing_cwd__"}
    for r in rows:
        assert sorted(r.keys()) == sorted([
            "cwd", "cwd_display", "session_count", "runtimes",
            "active_seconds_sum", "active_seconds_union",
            "peak_concurrency", "peak_at",
        ])
    by_cwd = {r["cwd"]: r for r in rows}
    assert by_cwd["/repo/a"]["session_count"] == 1
    assert by_cwd["/repo/a"]["runtimes"] == ["claude"]
    assert by_cwd["__missing_cwd__"]["runtimes"] == ["codex"]


def test_rollup_sum_vs_union_parallel_difference():
    # Two sessions active over the exact same minute: sum counts both,
    # union counts the minute once, peak is 2.
    start, end = 0.0, 1000.0
    s1 = _summ(id="s1", short_id="s1", activity=[100.0])
    s2 = _summ(id="s2", short_id="s2", activity=[100.0])
    s1["intervals"] = _clipped_intervals([100.0], start, end)
    s2["intervals"] = _clipped_intervals([100.0], start, end)
    (row,) = rollup_projects([s1, s2])
    assert row["session_count"] == 2
    assert row["runtimes"] == ["claude"]
    assert row["active_seconds_sum"] == pytest.approx(120.0)
    assert row["active_seconds_union"] == pytest.approx(60.0)
    assert row["peak_concurrency"] == 2
    assert row["peak_at"] == pytest.approx(100.0)


def test_rollup_touch_is_not_concurrent():
    s1 = _summ(id="s1", short_id="s1", activity=[])
    s2 = _summ(id="s2", short_id="s2", activity=[])
    s1["intervals"] = [(0.0, 100.0)]
    s2["intervals"] = [(100.0, 200.0)]
    (row,) = rollup_projects([s1, s2])
    assert row["peak_concurrency"] == 1
    assert row["active_seconds_union"] == pytest.approx(200.0)
    assert row["active_seconds_sum"] == pytest.approx(0.0)


def test_rollup_rust_peak_parity():
    s = _summ(id="s", short_id="s", activity=[])
    s["intervals"] = [(0.0, 100.0), (50.0, 150.0), (60.0, 70.0)]
    (row,) = rollup_projects([s])
    assert row["peak_concurrency"] == 3
    assert row["peak_at"] == pytest.approx(60.0)
    assert row["active_seconds_union"] == pytest.approx(150.0)


def test_rollup_no_intervals_stays_missing_not_zero_fabrication():
    (row,) = rollup_projects([_summ(activity=[])])
    assert row["active_seconds_sum"] == 0.0
    assert row["active_seconds_union"] == 0.0
    assert row["peak_concurrency"] == 0
    assert row["peak_at"] is None


def test_roundtrip_consistency_summarize_to_rollup():
    # Single session: union equals the session's clipped sum, peak is 1.
    start, end = 0.0, 10000.0
    activity = [100.0, 200.0, 5000.0]
    s = _summ(activity=activity, start=start, end=end)
    s["intervals"] = _clipped_intervals(activity, start, end)
    (row,) = rollup_projects([s])
    assert row["session_count"] == 1
    assert row["active_seconds_sum"] == pytest.approx(s["active_seconds"])
    assert row["active_seconds_union"] == pytest.approx(s["active_seconds"])
    assert row["peak_concurrency"] == 1
    assert row["peak_at"] == pytest.approx(100.0)
    # Group sum equals the sum of members over several sessions.
    s2 = _summ(id="s2", short_id="s2", activity=[6000.0], start=start, end=end)
    s2["intervals"] = _clipped_intervals([6000.0], start, end)
    (row2,) = rollup_projects([s, s2])
    assert row2["active_seconds_sum"] == pytest.approx(
        s["active_seconds"] + s2["active_seconds"]
    )
